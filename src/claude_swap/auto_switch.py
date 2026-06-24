"""Auto-switch decision engine and monitor loop for claude-swap.

Architecture
============
* ``AutoSwitchConfig`` — frozen config dataclass; persisted at
  ``<backup_root>/auto-switch.json``.
* ``SwitchDecision`` — frozen result of one decision tick.
* ``decide_switch`` — PURE function (no I/O, never raises).
* ``AutoSwitcher`` — wraps a ``ClaudeAccountSwitcher``, runs the monitor loop,
  acquires the single-flight lock, calls the switcher, sends notifications.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace as _state_replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from claude_swap import oauth
# AutoSwitchConfig + its load/save live in auto_switch_state (both are persisted
# engine state). Re-exported here so existing imports
# (``from claude_swap.auto_switch import AutoSwitchConfig`` / ``load_config`` /
# ``save_config``) keep working unchanged.
from claude_swap.auto_switch_state import (
    AutoSwitchConfig,
    MonitorState,
    load_config,
    load_state,
    save_config,
    save_state,
)
from claude_swap.locking import FileLock
from claude_swap.notify import notify

if TYPE_CHECKING:
    from claude_swap.switcher import ClaudeAccountSwitcher

# Public API of this module, including the re-exports from auto_switch_state
# (``AutoSwitchConfig`` / ``load_config`` / ``save_config`` / ``load_state`` /
# ``save_state`` / ``MonitorState``) which are imported for backward-compatible
# access via ``claude_swap.auto_switch`` and consumed by ``cli.py``. Declaring
# them here documents the surface and stops linters flagging the re-exports as
# unused imports.
__all__ = [
    "AutoSwitcher",
    "AutoSwitchConfig",
    "SwitchDecision",
    "decide_switch",
    "decide_consume_first",
    "next_interval",
    "next_interval_until_reset",
    "load_config",
    "save_config",
    "load_state",
    "save_state",
    "MonitorState",
]

_logger = logging.getLogger("claude-swap")


def _now_ts() -> float:
    """Wall-clock POSIX timestamp (survives restarts; renderable as local time).

    Distinct from ``time.monotonic()`` which is only valid for in-process
    durations and cannot be turned into a clock time for ``cswap auto status``.
    """
    return datetime.now(timezone.utc).timestamp()


# ``AutoSwitchConfig`` + ``load_config`` / ``save_config`` are defined in
# ``claude_swap.auto_switch_state`` and imported above (re-exported for
# backward compatibility).


# ---------------------------------------------------------------------------
# Decision model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SwitchDecision:
    """Result of one call to ``decide_switch`` / ``decide_consume_first``."""

    action: str          # "switch" | "stay"
    target: str | None   # account num to switch to (when action=="switch")
    reason: str          # machine reason string
    trigger_window: str | None  # "5h" | "7d" | None
    trigger_pct: float | None   # the crossing utilisation %
    detail: str          # human-readable one-liner
    # True when a SWITCHABLE peer's usage couldn't be read this tick (None /
    # "no credentials"), so the decision is based on incomplete information and
    # that peer might actually be the consume-first optimal. The monitor loop
    # uses this to retry sooner instead of waiting out the full cadence for a
    # transient usage-API miss. Always False for the reactive ``decide_switch``.
    incomplete: bool = False


# ---------------------------------------------------------------------------
# Pure decision function
# ---------------------------------------------------------------------------

_BIG = float("inf")

# How close (percentage points below ``session_threshold``) the 5h window must be
# before the daemon drops to the tight ``critical_interval`` cadence, so it
# observes the >= session_threshold crossing and switches BEFORE the hard 100%
# wall. 3pt → tight polling starts at 95% with the default 98% threshold.
_CRITICAL_BAND_PCT = 3.0

# Wake-from-sleep detection. The monitor sleeps in chunks of at most
# ``_WAKE_CHECK_CHUNK`` seconds; if a chunk's wall-clock overshoots what we asked
# for by more than ``_WAKE_GAP_THRESHOLD`` seconds, the process was suspended
# (the Mac slept), so we break the remaining sleep and re-poll immediately on
# wake — a scheduled re-rank (e.g. a blocked account's 5h reset) can't fire while
# suspended, and a long ``time.sleep`` would otherwise keep waiting out its
# remainder after wake. Normal scheduling jitter is sub-second, so 60s never
# false-positives; the chunks add only a handful of cheap timer checks (no API
# calls), so steady-state behaviour is unchanged.
_WAKE_CHECK_CHUNK = 30
_WAKE_GAP_THRESHOLD = 60


def _parse_reset_ts(usage: dict | str | None, window: str = "seven_day") -> float:
    """Extract ``<window>.resets_at`` as a POSIX timestamp; +inf on any failure.

    ``window`` is ``"seven_day"`` (default) or ``"five_hour"``. Accepts the full
    usage union (``dict | str | None``) since callers pass the ``"no
    credentials"`` sentinel too; the isinstance guard handles non-dicts.
    """
    if not isinstance(usage, dict):
        return _BIG
    win = usage.get(window)
    if not isinstance(win, dict):
        return _BIG
    raw = win.get("resets_at")
    if not isinstance(raw, str):
        return _BIG
    try:
        dt = datetime.fromisoformat(raw)
        # The API may emit naive ("...T12:00:00") or aware
        # ("...T12:00:00+00:00") timestamps. Treat naive values as UTC so the
        # cross-account ordering is identical regardless of the format.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return _BIG


def _window_pct(usage: dict | str | None, key: str) -> float:
    """Return ``usage[key]["pct"]`` as a float; 0.0 on any missing/bad value.

    Shared by ``decide_switch`` and ``AutoSwitcher`` so the threshold check is
    identical everywhere (the active-first short-circuit must agree with the
    pure decision function).
    """
    if not isinstance(usage, dict):
        return 0.0
    w = usage.get(key)
    if isinstance(w, dict) and isinstance(w.get("pct"), (int, float)):
        return float(w["pct"])
    return 0.0


def _crosses_threshold(
    usage: dict | str | None,
    config: AutoSwitchConfig,
) -> tuple[bool, str | None, float | None, float, float]:
    """Whether an account is at/over a switch threshold (PURE).

    SINGLE SOURCE OF TRUTH for the threshold check — used for BOTH the active
    account (in ``decide_switch`` and ``AutoSwitcher._active_over_threshold``)
    AND candidate viability, so the active-first short-circuit can never drift
    from the pure decision.

    Returns ``(crossed, trigger_window, trigger_pct, h5_pct, d7_pct)``:
    * ``crossed`` — True iff 5h% >= session_threshold OR 7d% >= weekly_threshold.
    * When BOTH cross, the binding window reported is the one with the HIGHER
      utilisation (severity), so logs/notifications reflect the worst window.
      (Target selection is unchanged — always soonest-7d-reset.)
    * ``h5_pct`` / ``d7_pct`` are the raw window utilisations (always returned,
      so callers don't recompute them).
    * Not crossed → ``(False, None, None, h5_pct, d7_pct)``.
    """
    h5 = _window_pct(usage, "five_hour")
    d7 = _window_pct(usage, "seven_day")
    h5_crossed = h5 >= config.session_threshold
    d7_crossed = d7 >= config.weekly_threshold

    if not h5_crossed and not d7_crossed:
        return (False, None, None, h5, d7)
    if h5_crossed and d7_crossed:
        # Both over — report the more severe (higher pct) window.
        if h5 >= d7:
            return (True, "5h", h5, h5, d7)
        return (True, "7d", d7, h5, d7)
    if h5_crossed:
        return (True, "5h", h5, h5, d7)
    return (True, "7d", d7, h5, d7)


def _consume_sort_key(
    num: str,
    usage_by_account: dict[str, dict | str | None],
    rotation_index: dict[str, int],
) -> tuple[float, float, int]:
    """Ranking key shared by decide_switch and decide_consume_first (PURE).

    Ascending: soonest 7d reset → most headroom → lowest rotation index. A single
    key function so the two strategies can never drift in their target ordering.
    """
    u = usage_by_account.get(num)
    reset_ts = _parse_reset_ts(u)
    headroom = oauth.account_headroom(u)
    neg_headroom = -headroom if headroom is not None else _BIG  # more headroom → smaller
    idx = rotation_index.get(num, 0)
    return (reset_ts, neg_headroom, idx)


def _has_7d_room(usage: dict | str | None, config: AutoSwitchConfig) -> bool:
    """True iff the 7-day window is strictly under the weekly threshold (PURE)."""
    return _window_pct(usage, "seven_day") < config.weekly_threshold


def _is_available(
    num: str,
    usage: dict | str | None,
    config: AutoSwitchConfig,
    blocked5h: frozenset[str],
) -> bool:
    """Usable right now: 5h room (with hysteresis) AND 7d room (PURE).

    5h availability is sticky: an account already 5h-blocked stays unavailable
    until its 5h% falls below ``session_threshold - hysteresis`` (anti-thrash
    dead band), so a value dithering around the threshold can't cause flip-flop.
    """
    if not isinstance(usage, dict):
        return False
    h5 = _window_pct(usage, "five_hour")
    if num in blocked5h:
        h5_ok = h5 < (config.session_threshold - config.hysteresis)
    else:
        h5_ok = h5 < config.session_threshold
    return h5_ok and _has_7d_room(usage, config)


def next_blocked5h(
    usage_by_account: dict[str, dict | str | None],
    config: AutoSwitchConfig,
    prev_blocked5h: frozenset[str],
) -> frozenset[str]:
    """Advance the sticky 5h-blocked set (PURE).

    Enter blocked at ``5h% >= session_threshold``; stay blocked through the dead
    band ``[session_threshold - hysteresis, session_threshold)``; clear below it.
    An account with unknown usage this tick CARRIES its prior state (a blip never
    flips the FSM).
    """
    nxt: set[str] = set()
    for num, usage in usage_by_account.items():
        if not isinstance(usage, dict):
            if num in prev_blocked5h:
                nxt.add(num)
            continue
        h5 = _window_pct(usage, "five_hour")
        if num in prev_blocked5h:
            if h5 >= (config.session_threshold - config.hysteresis):
                nxt.add(num)
        elif h5 >= config.session_threshold:
            nxt.add(num)
    return frozenset(nxt)


def decide_switch(
    active_num: str | None,
    usage_by_account: dict[str, dict | str | None],
    switchable: set[str],
    config: AutoSwitchConfig,
    live_session_nums: set[str],
    rotation_index: dict[str, int] | None = None,
) -> SwitchDecision:
    """Decide whether to switch accounts (PURE — no I/O, never raises).

    Args:
        active_num: Current account number as string, or None when unknown.
        usage_by_account: Map from account-num string to usage dict / sentinel.
        switchable: Account nums with valid creds+config backups (excl. active).
        config: Current auto-switch configuration.
        live_session_nums: Nums that have a live ``cswap run`` session.
        rotation_index: Optional mapping of num→position in the sequence list,
            used as a stable tie-break. Default (None) → all get position 0.

    Returns:
        A ``SwitchDecision`` (always — never raises).
    """
    if rotation_index is None:
        rotation_index = {}

    # ------------------------------------------------------------------
    # 1. Active usage known?
    # ------------------------------------------------------------------
    if active_num is None:
        return SwitchDecision(
            action="stay",
            target=None,
            reason="active-usage-unknown",
            trigger_window=None,
            trigger_pct=None,
            detail="Active account is unknown; cannot decide whether to switch.",
        )

    active_usage = usage_by_account.get(active_num)
    if not isinstance(active_usage, dict):
        return SwitchDecision(
            action="stay",
            target=None,
            reason="active-usage-unknown",
            trigger_window=None,
            trigger_pct=None,
            detail=(
                f"Active account {active_num} usage is unavailable; "
                "cannot decide whether to switch."
            ),
        )

    # ------------------------------------------------------------------
    # 2. Threshold crossed? (single source of truth via _crosses_threshold,
    #    which also returns the raw pcts so we don't recompute them).
    # ------------------------------------------------------------------
    crossed, trigger_window, trigger_pct, h5_pct, d7_pct = _crosses_threshold(
        active_usage, config
    )

    if not crossed:
        return SwitchDecision(
            action="stay",
            target=None,
            reason="under-threshold",
            trigger_window=None,
            trigger_pct=None,
            detail=(
                f"Active account {active_num}: 5h={h5_pct:.1f}% "
                f"7d={d7_pct:.1f}% — under threshold."
            ),
        )

    # ------------------------------------------------------------------
    # 3. No switchable accounts at all?
    # ------------------------------------------------------------------
    if not switchable:
        return SwitchDecision(
            action="stay",
            target=None,
            reason="single-account",
            trigger_window=trigger_window,
            trigger_pct=trigger_pct,
            detail="No other accounts are configured.",
        )

    # ------------------------------------------------------------------
    # 4. Build viable candidates. Distinguish two failure shapes:
    #   * over-limit  → candidate was FETCHED and is at/over its own threshold.
    #   * unverifiable → candidate usage is unknown (None / "no credentials").
    # Only declare "all-exhausted" when EVERY candidate was verified over-limit.
    # If any candidate is merely unverifiable, return a softer stay so we never
    # fire the alarming "all accounts exhausted" notification on a peer's
    # transient network blip — we just wait for the next tick.
    # ------------------------------------------------------------------
    viable: list[str] = []
    any_unverifiable = False
    for n in switchable:
        if n == active_num:          # defensive: never target the active account
            continue
        if n in live_session_nums:
            continue
        cand_usage = usage_by_account.get(n)
        if not isinstance(cand_usage, dict):
            any_unverifiable = True   # None / "no credentials" → couldn't verify
            continue
        cand_crossed, _cw, _cp, _ch5, _cd7 = _crosses_threshold(cand_usage, config)
        if not cand_crossed:
            viable.append(n)

    if not viable:
        if any_unverifiable:
            return SwitchDecision(
                action="stay",
                target=None,
                reason="candidates-unverifiable",
                trigger_window=trigger_window,
                trigger_pct=trigger_pct,
                detail=(
                    "No verified-available account to switch to "
                    "(some candidates' usage could not be read this tick). "
                    "Will retry."
                ),
            )
        return SwitchDecision(
            action="stay",
            target=None,
            reason="all-exhausted",
            trigger_window=trigger_window,
            trigger_pct=trigger_pct,
            detail="All other accounts are at or over their own limits.",
        )

    # ------------------------------------------------------------------
    # 5. Select target: soonest 7d reset → most headroom → lowest index
    # ------------------------------------------------------------------
    target = min(
        viable,
        key=lambda n: _consume_sort_key(n, usage_by_account, rotation_index),
    )

    return SwitchDecision(
        action="switch",
        target=target,
        reason=f"{trigger_window}-threshold",
        trigger_window=trigger_window,
        trigger_pct=trigger_pct,
        detail=(
            f"Active account {active_num} crossed {trigger_window} threshold "
            f"({trigger_pct:.1f}% >= "
            f"{config.session_threshold if trigger_window == '5h' else config.weekly_threshold:.1f}%). "
            f"Switching to account {target}."
        ),
    )


def decide_consume_first(
    active_num: str | None,
    usage_by_account: dict[str, dict | str | None],
    switchable: set[str],
    config: AutoSwitchConfig,
    live_session_nums: set[str],
    blocked5h: frozenset[str],
    rotation_index: dict[str, int] | None = None,
) -> SwitchDecision:
    """Proactive "consume-first" decision (PURE — no I/O, never raises).

    Keeps the user on the account to consume FIRST = the AVAILABLE account whose
    7-day window resets soonest (use-it-or-lose-it). Unlike ``decide_switch`` the
    ACTIVE account is itself a candidate for ``optimal`` (so an already-optimal
    active simply stays). Switches only when the optimal account CHANGES — a
    reset re-orders the queue, the active exhausts, or a 5h-blocked account
    clears and reclaims the soonest-reset slot.

    Args mirror ``decide_switch`` plus ``blocked5h`` (the sticky 5h-blocked set).
    """
    if rotation_index is None:
        rotation_index = {}

    # 1. Active usage known? (never switch blind — identical to decide_switch)
    if active_num is None:
        return SwitchDecision(
            action="stay", target=None, reason="active-usage-unknown",
            trigger_window=None, trigger_pct=None,
            detail="Active account is unknown; cannot decide whether to switch.",
            incomplete=True,
        )
    active_usage = usage_by_account.get(active_num)
    if not isinstance(active_usage, dict):
        return SwitchDecision(
            action="stay", target=None, reason="active-usage-unknown",
            trigger_window=None, trigger_pct=None,
            detail=(
                f"Active account {active_num} usage is unavailable; "
                "cannot decide whether to switch."
            ),
            incomplete=True,
        )

    # 2. Eligible = the active account (ALWAYS a candidate) + switchable peers
    #    without a live session. The active being a candidate is what lets an
    #    already-optimal active win and stay.
    eligible = [active_num] + [
        n for n in switchable if n != active_num and n not in live_session_nums
    ]
    candidates: list[str] = []
    any_7d_room = False
    any_unverifiable = False
    for num in eligible:
        u = usage_by_account.get(num)
        if isinstance(u, dict):
            if _has_7d_room(u, config):
                any_7d_room = True
        elif num != active_num:
            # A NON-active eligible peer whose usage we couldn't read (None /
            # "no credentials"). The active is handled by the unknown-usage
            # short-circuit above, so it's never unverifiable here.
            any_unverifiable = True
        if _is_available(num, u, config, blocked5h):
            candidates.append(num)

    crossed, trigger_window, trigger_pct, _h5, _d7 = _crosses_threshold(
        active_usage, config
    )

    # 3. Nothing usable right now — distinguish temporary vs real exhaustion.
    if not candidates:
        if any_7d_room:
            # Weekly quota remains somewhere, but every account is 5h-blocked
            # (or unverifiable) this moment — temporary, clears on a 5h reset.
            # Quiet stay (NOT routed to the exhaustion notification).
            return SwitchDecision(
                action="stay", target=None, reason="all-session-limited",
                trigger_window=trigger_window, trigger_pct=trigger_pct,
                detail=(
                    "No account is available right now (weekly limit and/or 5h "
                    "session limits); will resume as windows reset."
                ),
                incomplete=any_unverifiable,
            )
        if any_unverifiable:
            # No verified 7d room anywhere, but a peer's usage couldn't be read
            # — it may not actually be exhausted. Quiet stay (NOT the alarming
            # exhaustion notification) until the next tick can verify it.
            return SwitchDecision(
                action="stay", target=None, reason="candidates-unverifiable",
                trigger_window=trigger_window, trigger_pct=trigger_pct,
                detail=(
                    "No verified-available account to switch to "
                    "(some candidates' usage could not be read this tick). "
                    "Will retry."
                ),
                incomplete=True,
            )
        # No 7d room anywhere (all verified) → genuinely exhausted (notify path).
        return SwitchDecision(
            action="stay", target=None, reason="all-exhausted",
            trigger_window=trigger_window, trigger_pct=trigger_pct,
            detail="All accounts are at or over their weekly limit.",
        )

    # 4. Optimal = soonest-7d-reset available account (shared ranking key).
    optimal = min(
        candidates,
        key=lambda n: _consume_sort_key(n, usage_by_account, rotation_index),
    )

    # 5. Already on the consume-first account → stay; else move to consume it.
    if optimal == active_num:
        return SwitchDecision(
            action="stay", target=None, reason="optimal",
            trigger_window=trigger_window, trigger_pct=trigger_pct,
            detail=(
                f"Active account {active_num} is the consume-first account "
                "(soonest weekly reset among available)."
            ),
            # We are optimal among the accounts we could MEASURE — but if a peer
            # was unverifiable this tick it might actually reset sooner, so flag
            # the stay as incomplete to trigger a prompt re-check.
            incomplete=any_unverifiable,
        )
    return SwitchDecision(
        action="switch", target=optimal, reason="consume-first",
        trigger_window=trigger_window, trigger_pct=trigger_pct,
        detail=(
            f"Switching to account {optimal} to consume first "
            "(soonest weekly reset among available accounts)."
        ),
        incomplete=any_unverifiable,
    )


# ---------------------------------------------------------------------------
# Adaptive interval
# ---------------------------------------------------------------------------


def next_interval(
    active_usage: dict | None,
    config: AutoSwitchConfig,
    consecutive_failures: int = 0,
) -> int:
    """Return the next polling interval in seconds (adaptive, clamped). PURE.

    When ``consecutive_failures > 0`` the engine is OFFLINE (can't reach the
    usage API): back off exponentially from ``min_interval`` so the FIRST
    re-probe is quick (faster recovery detection) yet a sustained outage ramps
    up, capped at ``config.offline_backoff_cap``::

        min(offline_backoff_cap, min_interval * 2 ** min(failures - 1, 4))

    With the defaults (min 60, cap 600) the ramp is 60 → 120 → 240 → 480 → 600.

    When online (``consecutive_failures == 0``):
    - 5h >= ``session_threshold - _CRITICAL_BAND_PCT`` → ``critical_interval``
      (tight, BELOW ``min_interval``: catch the switch crossing before the hard
      100% 5h wall aborts the in-flight request). Checked first, on the 5h axis
      only; bypasses the ``min_interval`` floor.
    Otherwise a stepped adaptive mapping over the binding window (max of 5h%,
    7d%), floored at ``min_interval`` and ceilinged at ``max_interval``:
    - >= 95% → ``min_interval``                       (poll fast near a limit)
    - >= 85% → ``min(max_interval, 2 * min_interval)`` (e.g. 120s)
    - >= 50% → mid-band (~240s)
    - else   → ``max_interval``                       (far away → slow)
    Result is always clamped to ``[min_interval, max_interval]`` (except the
    sub-floor ``critical_interval`` early-return above).
    """
    if consecutive_failures > 0:
        # Cap the exponent at 4 (×16) so the multiplier never overflows.
        exp = min(consecutive_failures - 1, 4)
        backoff = config.min_interval * (2 ** exp)
        return min(config.offline_backoff_cap, backoff)

    if not isinstance(active_usage, dict):
        return config.max_interval

    h5 = _window_pct(active_usage, "five_hour")
    # Critical 5h band: the 5h window can spike within a single agentic turn, and
    # a hard 100% hit ABORTS the in-flight request (forcing a restart). While the
    # 5h window is within ``_CRITICAL_BAND_PCT`` of the switch threshold, poll
    # tight — below ``min_interval`` — so the daemon observes the
    # ``>= session_threshold`` crossing and flips the default login BEFORE 100%,
    # so the next message lands on a fresh account. Brief + self-terminating (the
    # moment it switches, the new active account is low-usage → slow cadence
    # again), so it does NOT raise the steady-state poll rate / spam the API. Only
    # the 5h axis triggers this; the slow-moving 7d window keeps the normal bands.
    if h5 >= config.session_threshold - _CRITICAL_BAND_PCT:
        return config.critical_interval

    # Binding window = the higher of the two utilisations. Missing/bad windows
    # read as 0.0 (→ far-away → max_interval), matching the old "no pct" case.
    approach = max(h5, _window_pct(active_usage, "seven_day"))

    if approach >= 95.0:
        interval = config.min_interval
    elif approach >= 85.0:
        interval = min(config.max_interval, 2 * config.min_interval)
    elif approach >= 50.0:
        # Mid-band: ~80% of the way to the ceiling (300 → 240).
        interval = int(config.max_interval * 0.8)
    else:
        interval = config.max_interval

    return max(config.min_interval, min(config.max_interval, interval))


def next_interval_until_reset(
    base_interval: int,
    active_usage: dict | str | None,
    peer_resets: list[float],
    now: float,
    config: AutoSwitchConfig,
) -> int:
    """Shorten the sleep so a tick lands shortly AFTER the soonest known reset.

    PURE. Consume-first only: a reset can change which account we should be on —
    a 7d reset re-orders the consumption queue, and a 5h reset un-blocks a
    previously-exhausted account — so we want to re-rank promptly rather than
    wait out the full ``base_interval``. The caller passes the active account's
    usage (its 7d reset is read here) plus ``peer_resets`` (every account's 7d
    reset and any blocked account's 5h reset); if the soonest of those falls
    within ``base_interval`` from ``now``, wake ~5s after it instead.

    Guarantees (never spins, never lengthens):
    * Result is clamped to ``[config.min_interval, base_interval]`` — it can
      ONLY shorten the base, never extend it, and never drop below the floor.
    * A reset already in the past, or none within the window, leaves the base
      unchanged.
    """
    soonest = _parse_reset_ts(active_usage)  # +inf when unknown
    for ts in peer_resets:
        if ts < soonest:
            soonest = ts

    # No usable future reset within the base window → keep the base.
    if soonest == _BIG or soonest <= now or (soonest - now) >= base_interval:
        return base_interval

    # Wake ~5s after the reset, clamped to the cadence band.
    wake_in = int((soonest - now) + 5)
    # Clamp into [min_interval, base_interval]. The outer min(base_interval, …)
    # is defense-in-depth: it guarantees we NEVER lengthen past base even in the
    # (production-unreachable) case where base_interval < min_interval.
    return min(base_interval, max(config.min_interval, wake_in))


# ---------------------------------------------------------------------------
# Monitor: AutoSwitcher
# ---------------------------------------------------------------------------


class AutoSwitcher:
    """Wraps a ``ClaudeAccountSwitcher`` to run the auto-switch monitor loop.

    Responsibilities:
    * Gather account/usage state via the switcher's private helpers.
    * Call ``decide_switch`` (pure).
    * Perform the switch via ``switcher.auto_switch_to`` when needed.
    * Send macOS notifications (mocked in tests).
    * Maintain the single-flight lock (``<backup>/.auto-switch.lock``).
    """

    def __init__(
        self,
        switcher: ClaudeAccountSwitcher,
        config: AutoSwitchConfig | None = None,
    ) -> None:
        self._switcher = switcher
        self._config = config if config is not None else load_config(
            switcher.backup_dir
        )
        self._lock_path = switcher.backup_dir / ".auto-switch.lock"
        # Active account's usage from the most recent ``run_once`` gather, used
        # by ``watch`` / ``run_daemon`` to size the next poll interval WITHOUT a
        # second gather (one gather per tick).
        self._last_active_usage: dict | None = None
        # Persistent monitor state (online/offline, backoff, last switch). Held
        # in memory across ticks and persisted once per tick. Defensive load:
        # a missing/corrupt file yields defaults, never crashes.
        self._state: MonitorState = load_state(switcher.backup_dir)

    @property
    def consecutive_failures(self) -> int:
        """Current consecutive offline-tick count (0 when online)."""
        return self._state.consecutive_failures

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _gather_meta(
        self,
    ) -> tuple[
        str | None,
        list[tuple],
        set[str],
        set[str],
        dict[str, int],
    ]:
        """Collect account METADATA needed to decide — NO network calls.

        This is the cheap, network-free half of a tick: it resolves the active
        account, the switchable set, live sessions, and rotation order from
        local state only. Usage is fetched separately (active-first) so a
        normal tick makes just ONE usage API call.

        Returns:
            (active_num, accounts_info, switchable, live_session_nums,
             rotation_index)
        """
        s = self._switcher

        # Build accounts info once (carries per-account credentials, is_active,
        # and is already in sequence order). No usage-API I/O here. Everything
        # below is derived from this single read — no redundant disk reads.
        accounts_info = s._build_accounts_info()

        # Active account number — from the is_active flag (index 4), not a
        # second _get_current_account()/_find_account_slot() round-trip.
        active_num: str | None = next(
            (str(r[0]) for r in accounts_info if r[4]), None
        )

        # Switchable set (excludes active). _account_is_switchable still runs
        # per-candidate — it validates the config+creds backups, which the row
        # alone doesn't guarantee.
        switchable: set[str] = {
            str(r[0])
            for r in accounts_info
            if str(r[0]) != active_num and s._account_is_switchable(str(r[0]))
        }

        # Live session nums (avoid switching onto these).
        live_session_nums: set[str] = {
            str(num)
            for (num, email, *_rest) in accounts_info
            if s._live_session_pids(str(num), email)
        }

        # Rotation index for stable tie-breaking — accounts_info is already in
        # sequence order.
        rotation_index: dict[str, int] = {
            str(r[0]): i for i, r in enumerate(accounts_info)
        }

        return active_num, accounts_info, switchable, live_session_nums, rotation_index

    def _fetch_one(self, row: tuple) -> dict | str | None:
        """Fetch ONE account's usage uncached (one API call); never raises.

        ``row`` is a ``_build_accounts_info`` tuple
        ``(num, email, org_name, org_uuid, is_active, creds)``.

        Returns the trichotomy sentinel set, mirroring ``_collect_usage``:
        * ``dict``             → reachable, got data
        * ``None``             → had a token, the HTTP call failed (outage)
        * ``"no credentials"`` → no usable token (we did NOT try)

        Deliberately does NOT touch the shared ``cache/usage.json`` (so
        ``cswap --list`` / ``cswap auto status`` still refetch the full set on
        demand) and does NOT go through ``_collect_usage`` (whose 15s cache can
        never help a daemon whose tick interval is >= 60s).

        ``allow_refresh=False``: the daemon NEVER refreshes an inactive
        account's expired token. A refresh rotates the one-time OAuth refresh
        token server-side, and the daemon runs under launchd where the Keychain
        may be locked (Mac asleep) — a rotation we cannot persist bricks the
        account until re-login. An expired peer therefore reads as ``None`` here
        and keeps its last-known usage; a foreground ``cswap --list`` / switch
        (unlocked session) refreshes and re-backs-up the token instead.
        """
        num, email, _org_name, _org_uuid, is_active, creds = row
        if not creds or not oauth.extract_access_token(creds):
            return "no credentials"
        try:
            return oauth.fetch_usage_for_account(
                str(num), email, creds, is_active=is_active, allow_refresh=False
            )
        except Exception as exc:  # never let a fetch take the tick down
            _logger.debug("auto-switch: usage fetch for %s failed: %r", num, exc)
            return None

    def _active_over_threshold(self, active_usage: dict) -> bool:
        """True iff the active account is at/over a switch threshold.

        Delegates to the shared pure ``_crosses_threshold`` so the active-first
        short-circuit can never disagree with ``decide_switch`` about whether a
        switch is even possible.
        """
        crossed, _window, _pct, _h5, _d7 = _crosses_threshold(
            active_usage, self._config
        )
        return crossed

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_once(self) -> SwitchDecision:
        """Execute one monitor tick.

        Connection-loss fallback (SAFE / VISIBLE / RECOVERABLE):
        * SAFE — when the usage API is unreachable (every fetch returned None)
          we NEVER switch. We deliberately do NOT switch on the last-known
          (stale) cached usage: switching onto a possibly-stale-exhausted
          target is risky and low value — a true network outage also blocks
          Claude Code itself, so no quota is burning while we're offline, and
          the next online tick self-corrects.
        * VISIBLE — offline ticks back off (see ``next_interval``) and send a
          one-shot "offline" notification; ``cswap auto status`` surfaces it.
        * RECOVERABLE — the first online tick resets the failure counter and
          (if we had notified) sends a one-shot "back online" notification.

        Anti-spam fetch strategy (key to staying under the rate limit):
        a NORMAL tick makes just ONE usage API call. We probe ONLY the active
        account first; the other accounts are fetched ONLY when the active
        account is at/over a threshold and we might actually switch (since
        ``decide_switch`` needs every candidate to pick the soonest-reset
        target). Under-threshold and offline ticks never fetch the others.

        Returns the decision; on gather error returns a stay/tick-error
        decision and logs the exception. Persists monitor state every tick.
        """
        try:
            (
                active_num,
                accounts_info,
                switchable,
                live_session_nums,
                rotation_index,
            ) = self._gather_meta()
        except Exception as exc:
            # A gather failure is NOT the same as "the usage API is offline":
            # it's a local/programming error. Treat it as a normal failed tick
            # (do not touch the offline state machine) and back off via the
            # max interval through the existing daemon/watch try/except.
            _logger.exception("auto-switch: gather failed: %r", exc)
            self._apply_exhaustion("tick-error")
            self._persist_state()
            return SwitchDecision(
                action="stay",
                target=None,
                reason="tick-error",
                trigger_window=None,
                trigger_pct=None,
                detail=f"Gather error: {exc!r}",
            )

        row_by_num: dict[str, tuple] = {str(r[0]): r for r in accounts_info}

        # ---- Phase 1: probe ONLY the active account (one API call). --------
        active_row = row_by_num.get(active_num) if active_num is not None else None
        active_usage: dict | str | None = (
            self._fetch_one(active_row) if active_row is not None else "no credentials"
        )

        # Stash the active account's usage so watch/run_daemon size the next
        # interval from this single probe (no extra gather).
        self._last_active_usage = (
            active_usage if isinstance(active_usage, dict) else None
        )

        # ---- Trichotomy on the ACTIVE probe (active-first) -----------------
        #   * dict             → ONLINE
        #   * None             → OFFLINE (don't fetch others — save calls in an
        #                        outage)
        #   * "no credentials" → NEITHER (no token / not managed) → stay, don't
        #                        fetch others; NOT a network outage
        if not isinstance(active_usage, dict):
            if active_usage is None:
                # Active had a token and the call failed → genuine outage.
                self._handle_offline_tick()
                detail = (
                    "Usage API unreachable (offline) — monitoring paused, "
                    "will resume automatically. Not switching on stale data."
                )
            else:
                # active_num is None / not managed / no usable credentials.
                detail = (
                    "Active account is unknown; cannot decide whether to switch."
                    if active_num is None
                    else "No usable credentials for the active account — can't "
                    "read usage (not a network outage)."
                )
            self._apply_exhaustion("active-usage-unknown")
            self._persist_state()
            return SwitchDecision(
                action="stay",
                target=None,
                reason="active-usage-unknown",
                trigger_window=None,
                trigger_pct=None,
                detail=detail,
            )

        # Past Phase 1 with a dict => active_num must be a real slot: if it were
        # None, active_row would be None → active_usage would be the
        # "no credentials" sentinel (not a dict) and we'd have returned above.
        # Narrow the type so the str-keyed usage maps below are well-typed.
        assert active_num is not None
        active_num_str: str = active_num

        # ---- ONLINE: active fetched OK. Recovery + refresh last-known-good. -
        # valid_nums = currently-managed accounts → prunes removed accounts
        # from last_usage while preserving peers not probed on this active-only
        # tick.
        managed_nums = set(row_by_num)
        self._handle_online_tick({active_num_str: active_usage}, managed_nums)

        # Proactive "consume-first" strategy diverges here: it must evaluate the
        # optimal account EVERY tick (not only when the active is over a
        # threshold), so it fetches all peers and ranks by soonest 7d reset.
        if self._config.strategy == "consume-first":
            return self._run_once_consume_first(
                active_num_str, active_usage, row_by_num, switchable,
                live_session_nums, rotation_index, managed_nums,
            )

        # ---- REACTIVE strategy (default) -----------------------------------
        # Is the active account at/over a threshold? Only then is a switch even
        # possible — and only then do we pay for the others' fetches.
        if not self._active_over_threshold(active_usage):
            # Under both thresholds → stay WITHOUT fetching the others (one API
            # call total this tick). Mirror decide_switch's under-threshold
            # decision exactly (it stays PURE and is simply not called here).
            self._apply_exhaustion("under-threshold")
            self._persist_state()
            return SwitchDecision(
                action="stay",
                target=None,
                reason="under-threshold",
                trigger_window=None,
                trigger_pct=None,
                detail=(
                    f"Active account {active_num} under threshold "
                    "(probed active only; others not fetched)."
                ),
            )

        # ---- Phase 2: active crossed → fetch the OTHER switchable accounts --
        # so decide_switch sees a COMPLETE usage map (active + all candidates)
        # and can pick the soonest-7d-reset target. (N-1 calls, rare path.)
        usage_by_account: dict[str, dict | str | None] = {
            active_num_str: active_usage
        }
        for num in switchable:
            row = row_by_num.get(num)
            usage_by_account[num] = (
                self._fetch_one(row) if row is not None else "no credentials"
            )

        # Record the candidates' fresh GOOD readings too (don't discard Phase-2
        # work) — last_usage now reflects the COMPLETE map, surfaced in status.
        self._merge_last_usage(usage_by_account, managed_nums)

        decision = decide_switch(
            active_num_str,
            usage_by_account,
            switchable,
            self._config,
            live_session_nums,
            rotation_index,
        )

        _logger.debug(
            "auto-switch tick: action=%s reason=%s detail=%s",
            decision.action,
            decision.reason,
            decision.detail,
        )

        err = self._apply_switch_decision(decision)
        if err is not None:
            return err

        # One-shot exhaustion notification (persisted; survives restart). Fires
        # ONLY on a real all-exhausted decision (every candidate verified
        # over-limit) — never on "candidates-unverifiable" (a peer's blip).
        self._apply_exhaustion(decision.reason)
        self._persist_state()
        return decision

    def _apply_switch_decision(self, decision: SwitchDecision) -> SwitchDecision | None:
        """Perform the switch in ``decision`` (if any). Shared by both strategies.

        Returns ``None`` when there is nothing to do or the switch succeeded
        (the caller proceeds with the original decision), or a ``tick-error``
        SwitchDecision when the switch raised (caller returns it). On success
        records ``last_switch`` and fires the switch notification.
        """
        if decision.action != "switch" or decision.target is None:
            return None
        try:
            self._switcher.auto_switch_to(decision.target, quiet=True)
            _logger.info(
                "auto-switch: switched to account %s (%s)",
                decision.target, decision.detail,
            )
            self._state = _state_replace(
                self._state,
                last_switch={
                    "account": decision.target,
                    "ts": _now_ts(),
                    "reason": decision.reason,
                },
            )
            if self._config.notify:
                notify(
                    "Claude Swap — Auto Switch",
                    f"Switched to account {decision.target}. {decision.detail}",
                )
            return None
        except Exception as exc:
            _logger.exception(
                "auto-switch: switch to %s failed: %r", decision.target, exc
            )
            # A failed switch is NOT exhaustion — clear the flag.
            self._apply_exhaustion("tick-error")
            self._persist_state()
            return SwitchDecision(
                action="stay",
                target=None,
                reason="tick-error",
                trigger_window=decision.trigger_window,
                trigger_pct=decision.trigger_pct,
                detail=f"Switch error: {exc!r}",
            )

    def _run_once_consume_first(
        self,
        active_num: str,
        active_usage: dict,
        row_by_num: dict[str, tuple],
        switchable: set[str],
        live_session_nums: set[str],
        rotation_index: dict[str, int],
        managed_nums: set[str],
    ) -> SwitchDecision:
        """Proactive consume-first tick: rank ALL accounts by soonest 7d reset.

        Needs every account's usage to know the consumption order, so it fetches
        all switchable peers this tick (design A — simple + robust; gentle at the
        60-300s cadence). Advances the sticky 5h-blocked FSM, decides via the
        pure ``decide_consume_first``, then switches through the shared apply
        path. ``decide_consume_first`` never switches on stale/unknown data.
        """
        # Design A: fetch all peers every tick. A cached/active-first "design B"
        # (poll the active, infer peer resets from cached usage, confirm-before-
        # switch, slow full-refresh sub-cadence) is a possible future
        # optimization but is intentionally NOT implemented here.
        usage_by_account: dict[str, dict | str | None] = {active_num: active_usage}
        for num in switchable:
            row = row_by_num.get(num)
            usage_by_account[num] = (
                self._fetch_one(row) if row is not None else "no credentials"
            )
        # Record fresh readings (prune removed accounts; never clobber good ones).
        self._merge_last_usage(usage_by_account, managed_nums)

        # Advance + persist the sticky 5h-blocked hysteresis state.
        prev_blocked = frozenset(self._state.blocked5h)
        blocked = next_blocked5h(usage_by_account, self._config, prev_blocked)
        self._state = _state_replace(self._state, blocked5h=sorted(blocked))

        decision = decide_consume_first(
            active_num, usage_by_account, switchable, self._config,
            live_session_nums, blocked, rotation_index,
        )
        _logger.debug(
            "auto-switch consume-first tick: action=%s reason=%s",
            decision.action, decision.reason,
        )

        err = self._apply_switch_decision(decision)
        if err is not None:
            return err
        self._apply_exhaustion(decision.reason)
        self._persist_state()
        return decision

    # ------------------------------------------------------------------
    # Offline / online state machine (mutates self._state, never raises)
    # ------------------------------------------------------------------

    def _handle_offline_tick(self) -> None:
        """Increment the failure counter; send a one-shot offline notice.

        Never switches. Logs the transition into offline at INFO exactly once
        (subsequent offline ticks stay quiet in the log).
        """
        failures = self._state.consecutive_failures + 1
        just_went_offline = self._state.consecutive_failures == 0
        notified = self._state.offline_notified

        # One-shot offline notification: after >= 2 consecutive failures, if we
        # haven't already told the user and notifications are enabled.
        if failures >= 2 and not notified and self._config.notify:
            notify(
                "Claude Swap — Auto-switch offline",
                "Can't reach the usage API — monitoring paused, will resume "
                "automatically.",
            )
            notified = True

        if just_went_offline:
            _logger.info(
                "auto-switch: usage API unreachable — entering offline mode "
                "(will back off and resume on reconnect)."
            )

        self._state = _state_replace(
            self._state,
            consecutive_failures=failures,
            offline_notified=notified,
        )

    def _handle_online_tick(
        self,
        usage_by_account: Mapping[str, object],
        valid_nums: set[str] | None = None,
    ) -> None:
        """Mark online, handle recovery, refresh last-known-good usage.

        We only "recover" LOUDLY (INFO log + notification) from a state we
        actually ANNOUNCED — i.e. ``offline_notified`` is True. A single
        transient failure that never crossed the >=2 notify gate clears
        silently, so there's never a "back online" with no prior "offline".

        ``valid_nums`` (currently-managed account nums) prunes removed accounts
        from ``last_usage`` — see ``merged_usage``.
        """
        announced_offline = self._state.offline_notified
        now = _now_ts()

        if announced_offline:
            _logger.info("auto-switch: usage API reachable again — back online.")
            if self._config.notify:
                notify(
                    "Claude Swap — Auto-switch back online",
                    "Usage monitoring resumed.",
                )

        # Merge fresh GOOD readings (never overwrite a good entry with None);
        # prune entries for accounts no longer managed.
        merged = self._state.merged_usage(usage_by_account, now, valid_nums)

        self._state = _state_replace(
            self._state,
            last_online_ts=now,
            consecutive_failures=0,
            offline_notified=False,
            last_usage=merged,
        )

    def _merge_last_usage(
        self,
        usage_by_account: Mapping[str, object],
        valid_nums: set[str] | None = None,
    ) -> None:
        """Merge fresh GOOD readings into ``last_usage`` (no recovery handling).

        Used after Phase 2 so the candidates' last-known usage is recorded (and
        rendered in ``cswap auto status``) rather than discarded. A None /
        non-dict fetch never clobbers a good entry (see ``merged_usage``).
        ``valid_nums`` prunes removed accounts.
        """
        merged = self._state.merged_usage(usage_by_account, _now_ts(), valid_nums)
        self._state = _state_replace(self._state, last_usage=merged)

    def _apply_exhaustion(self, reason: str) -> None:
        """Manage the PERSISTED one-shot 'all accounts exhausted' notification.

        Mirrors the offline one-shot pattern (survives daemon restarts — no
        in-memory monotonic gate):
        * reason == "all-exhausted" → fire the notification once (only when not
          already notified) and set ``exhaustion_notified`` True.
        * any other reason (recovery: a switch happened, the active is under
          threshold, candidates unverifiable, or we're offline) → clear the
          flag so the NEXT real exhaustion notifies again.
        """
        if reason == "all-exhausted":
            if not self._state.exhaustion_notified:
                _logger.info("auto-switch: all accounts exhausted")
                if self._config.notify:
                    notify(
                        "Claude Swap — All Accounts Exhausted",
                        "All managed accounts are at their rate-limit. "
                        "Consider adding another account.",
                    )
                self._state = _state_replace(
                    self._state, exhaustion_notified=True
                )
        elif self._state.exhaustion_notified:
            self._state = _state_replace(self._state, exhaustion_notified=False)

    def _persist_state(self) -> None:
        """Persist monitor state once (best-effort; never raises)."""
        save_state(self._state, self._switcher.backup_dir)

    def _known_peer_resets(self) -> list[float]:
        """7d reset timestamps from the last-known usage of every account.

        Used by the consume-first scheduler to wake shortly after the soonest
        reset. Unknown/unparseable resets are dropped (they'd be +inf anyway).
        """
        resets: list[float] = []
        for entry in self._state.last_usage.values():
            if isinstance(entry, dict):
                ts = _parse_reset_ts(entry.get("usage"))
                if ts != _BIG:
                    resets.append(ts)
        return resets

    def _blocked_peer_5h_resets(self) -> list[float]:
        """5h reset timestamps of the accounts currently in ``blocked5h``.

        A 5h-blocked account is unavailable until its 5-hour window resets — at
        which point it becomes switchable again and may be the new consume-first
        optimal (e.g. the soonest-7d-reset account that was temporarily blocked).
        The 7d-only ``_known_peer_resets`` never sees that moment, so without
        this the daemon only notices on its next normal-cadence tick (up to
        ``max_interval`` later). Feeding these resets into the scheduler makes it
        wake ~5s after the block clears and re-rank promptly. It only ADDS a
        single well-timed wake (the scheduler can only shorten, and is clamped to
        the ``min_interval`` floor) — it does not raise the steady-state poll
        rate, so it never overloads the usage API. Unknown/unparseable resets are
        dropped; an account at 5h 0% has no ``resets_at`` and is naturally absent.
        """
        resets: list[float] = []
        for num in self._state.blocked5h:
            entry = self._state.last_usage.get(str(num))
            if isinstance(entry, dict):
                ts = _parse_reset_ts(entry.get("usage"), "five_hour")
                if ts != _BIG:
                    resets.append(ts)
        return resets

    def _consume_first_interval(self, base_interval: int) -> int:
        """Reset-aware sleep for consume-first (reactive uses ``base_interval``).

        Shortens ``base_interval`` so a tick lands ~5s after the soonest known
        reset — every account's 7d reset (consumption order re-ranks) AND the 5h
        reset of any currently-blocked account (it becomes available again). Only
        shortens; clamped to ``[min_interval, base_interval]``, so it adds at most
        one well-timed wake and never raises the steady-state poll rate.
        """
        return next_interval_until_reset(
            base_interval,
            self._last_active_usage,
            self._known_peer_resets() + self._blocked_peer_5h_resets(),
            _now_ts(),
            self._config,
        )

    def _retry_interval(
        self, decision: SwitchDecision | None, interval: int
    ) -> int:
        """Shorten ``interval`` to ``min_interval`` when we STAYED on incomplete
        info (a switchable peer's usage couldn't be read this tick).

        A transient usage-API miss on a peer that might be the consume-first
        optimal otherwise costs up to ``max_interval`` before the next re-check;
        retrying at the floor (60s) catches it promptly without spamming the API
        (it only fires on a stay flagged ``incomplete``).
        """
        if decision is not None and decision.action == "stay" and decision.incomplete:
            return min(interval, self._config.min_interval)
        return interval

    def _sleep_with_wake_detection(self, interval: int) -> bool:
        """Sleep ``interval`` seconds in chunks; return True on a detected
        system-sleep gap so the caller re-polls immediately on wake.

        macOS suspends the process during system sleep: a scheduled re-rank (a
        blocked account's 5h reset) can't fire, and a single long ``time.sleep``
        keeps waiting out its remainder after wake. Sleeping in
        ``_WAKE_CHECK_CHUNK``-second chunks and watching for a chunk whose
        wall-clock overshoots by more than ``_WAKE_GAP_THRESHOLD`` detects the
        suspension and breaks out so the next tick re-fetches + re-ranks within
        seconds of wake. No API calls — steady-state behaviour is unchanged.
        """
        deadline = _now_ts() + interval
        while True:
            remaining = deadline - _now_ts()
            if remaining <= 0:
                return False
            chunk = remaining if remaining < _WAKE_CHECK_CHUNK else _WAKE_CHECK_CHUNK
            before = _now_ts()
            time.sleep(chunk)
            if (_now_ts() - before) - chunk > _WAKE_GAP_THRESHOLD:
                return True

    def watch(self) -> None:
        """Run the monitor loop in the foreground (Ctrl-C to stop)."""
        from claude_swap.printer import accent, dimmed, muted

        lock = FileLock(self._lock_path)
        if not lock.acquire(timeout=0):
            print(
                dimmed(
                    "auto-switch is already running (daemon or another watch). "
                    "Run `cswap auto status` to check."
                )
            )
            return

        print(accent("Auto-switch watch mode active") + "  " + dimmed("(Ctrl-C to stop)"))
        try:
            while True:
                decision = self.run_once()
                # run_once already gathered once this tick; reuse its result.
                failures = self._state.consecutive_failures
                interval = next_interval(
                    self._last_active_usage, self._config, failures
                )
                # consume-first only (and only when ONLINE): wake shortly after
                # the soonest known reset so the order re-ranks promptly. Never
                # lengthens; reactive cadence is untouched.
                if failures == 0 and self._config.strategy == "consume-first":
                    interval = self._consume_first_interval(interval)
                    interval = self._retry_interval(decision, interval)
                if failures > 0:
                    status = (
                        f"  offline — retry in {interval}s "
                        f"({failures} failure{'s' if failures != 1 else ''})"
                    )
                else:
                    status = (
                        f"  action={decision.action}  reason={decision.reason}  "
                        f"next poll in {interval}s"
                    )
                print(muted(status))
                if self._sleep_with_wake_detection(interval):
                    print(muted("  resumed after sleep — re-polling now"))
        except KeyboardInterrupt:
            print(f"\n{dimmed('Auto-switch stopped.')}")
        finally:
            lock.release()

    def run_daemon(self) -> None:
        """Run the monitor loop as a background daemon (launchd entry point).

        Re-reads config each tick so ``cswap auto off`` stops it without a
        restart.  Exits cleanly when ``enabled`` becomes False.
        """
        lock = FileLock(self._lock_path)
        if not lock.acquire(timeout=0):
            _logger.warning(
                "auto-switch daemon: lock already held — another instance running, exiting."
            )
            return

        _logger.info("auto-switch daemon: started")
        decision: SwitchDecision | None = None
        try:
            while True:
                # Re-read config each tick to honour live on/off changes.
                self._config = load_config(self._switcher.backup_dir)
                if not self._config.enabled:
                    _logger.info(
                        "auto-switch daemon: disabled in config, exiting."
                    )
                    break

                try:
                    decision = self.run_once()
                    # run_once gathered once this tick; reuse its result.
                    failures = self._state.consecutive_failures
                    interval = next_interval(
                        self._last_active_usage, self._config, failures,
                    )
                    # consume-first only (online only): land a tick shortly after
                    # the soonest reset to re-rank promptly. Reactive unchanged.
                    if failures == 0 and self._config.strategy == "consume-first":
                        interval = self._consume_first_interval(interval)
                        # Retry sooner if we stayed on incomplete info (a peer
                        # we couldn't read this tick might be the optimal).
                        interval = self._retry_interval(decision, interval)
                except Exception as exc:
                    _logger.exception("auto-switch daemon: tick error: %r", exc)
                    interval = self._config.max_interval

                _logger.debug(
                    "auto-switch daemon: sleeping %ds after %s/%s",
                    interval,
                    decision.action if decision is not None else "?",
                    decision.reason if decision is not None else "?",
                )
                if self._sleep_with_wake_detection(interval):
                    _logger.info(
                        "auto-switch: resumed after a sleep/suspend gap — "
                        "re-polling now (a blocked account may have reset while "
                        "suspended)."
                    )
        finally:
            lock.release()
            _logger.info("auto-switch daemon: stopped")
