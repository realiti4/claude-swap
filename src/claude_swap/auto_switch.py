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
    """Result of one call to ``decide_switch``."""

    action: str          # "switch" | "stay"
    target: str | None   # account num to switch to (when action=="switch")
    reason: str          # machine reason string
    trigger_window: str | None  # "5h" | "7d" | None
    trigger_pct: float | None   # the crossing utilisation %
    detail: str          # human-readable one-liner


# ---------------------------------------------------------------------------
# Pure decision function
# ---------------------------------------------------------------------------

_BIG = float("inf")


def _parse_reset_ts(usage: dict | None) -> float:
    """Extract seven_day.resets_at as a POSIX timestamp; +inf on any failure."""
    if not isinstance(usage, dict):
        return _BIG
    d7 = usage.get("seven_day")
    if not isinstance(d7, dict):
        return _BIG
    raw = d7.get("resets_at")
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

    Shared by ``decide_switch`` and ``AutoSwitcher._active_over_threshold`` so
    the threshold check is identical in both (the active-first short-circuit
    must agree with the pure decision function).
    """
    if not isinstance(usage, dict):
        return 0.0
    w = usage.get(key)
    if isinstance(w, dict) and isinstance(w.get("pct"), (int, float)):
        return float(w["pct"])
    return 0.0


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
    # 2. Threshold crossed?
    # ------------------------------------------------------------------
    h5_pct = _window_pct(active_usage, "five_hour")
    d7_pct = _window_pct(active_usage, "seven_day")

    h5_crossed = h5_pct >= config.session_threshold
    d7_crossed = d7_pct >= config.weekly_threshold

    if not h5_crossed and not d7_crossed:
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

    # Both crossed → 5h is the binding/faster window
    trigger_window = "5h" if h5_crossed else "7d"
    trigger_pct = h5_pct if h5_crossed else d7_pct

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
    # 4. Build viable candidates
    # ------------------------------------------------------------------
    viable: list[str] = []
    for n in switchable:
        if n in live_session_nums:
            continue
        cand_usage = usage_by_account.get(n)
        if not isinstance(cand_usage, dict):
            continue
        cand_h5_pct = _window_pct(cand_usage, "five_hour")
        cand_d7_pct = _window_pct(cand_usage, "seven_day")
        if (cand_h5_pct < config.session_threshold
                and cand_d7_pct < config.weekly_threshold):
            viable.append(n)

    if not viable:
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
    def sort_key(num: str) -> tuple[float, float, int]:
        u = usage_by_account.get(num)
        reset_ts = _parse_reset_ts(u)  # ascending: soonest first
        headroom = oauth.account_headroom(u)
        neg_headroom = -headroom if headroom is not None else _BIG  # more headroom → smaller
        idx = rotation_index.get(num, 0)
        return (reset_ts, neg_headroom, idx)

    target = min(viable, key=sort_key)

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
    usage API): back off exponentially so a real outage doesn't hammer the
    network, capped at ``config.offline_backoff_cap``::

        min(offline_backoff_cap, max_interval * 2 ** min(failures - 1, 4))

    When online (``consecutive_failures == 0``) use a stepped adaptive mapping
    over the binding window (max of 5h%, 7d%), floored at ``min_interval`` and
    ceilinged at ``max_interval``:
    - >= 95% → ``min_interval``                       (poll fast near a limit)
    - >= 85% → ``min(max_interval, 2 * min_interval)`` (e.g. 120s)
    - >= 50% → mid-band (~240s)
    - else   → ``max_interval``                       (far away → slow)
    Result is always clamped to ``[min_interval, max_interval]``.
    """
    if consecutive_failures > 0:
        # Cap the exponent at 4 (×16) so the multiplier never overflows.
        exp = min(consecutive_failures - 1, 4)
        backoff = config.max_interval * (2 ** exp)
        return min(config.offline_backoff_cap, backoff)

    if not isinstance(active_usage, dict):
        return config.max_interval

    pcts: list[float] = []
    for key in ("five_hour", "seven_day"):
        w = active_usage.get(key)
        if isinstance(w, dict) and isinstance(w.get("pct"), (int, float)):
            pcts.append(float(w["pct"]))

    if not pcts:
        return config.max_interval

    approach = max(pcts)

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


# ---------------------------------------------------------------------------
# Monitor: AutoSwitcher
# ---------------------------------------------------------------------------

_EXHAUSTION_NOTIFY_GATE_SECS = 30 * 60  # 30 minutes


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
        self._last_exhaustion_notify_ts: float = 0.0
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

        # Build accounts info once (carries per-account credentials). No I/O to
        # the usage API here.
        accounts_info = s._build_accounts_info()

        # Active account number.
        data = s._get_sequence_data_migrated()
        ident = s._get_current_account()
        active_num: str | None = None
        if ident is not None and data is not None:
            email, org_uuid = ident
            active_num = s._find_account_slot(data, email, org_uuid)

        # Switchable set (excludes active).
        seq = (data or {}).get("sequence") or []
        switchable: set[str] = {
            str(n)
            for n in seq
            if str(n) != active_num and s._account_is_switchable(str(n))
        }

        # Live session nums (avoid switching onto these).
        live_session_nums: set[str] = {
            str(num)
            for (num, email, *_rest) in accounts_info
            if s._live_session_pids(str(num), email)
        }

        # Rotation index for stable tie-breaking.
        rotation_index: dict[str, int] = {str(n): i for i, n in enumerate(seq)}

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
        """
        num, email, _org_name, _org_uuid, is_active, creds = row
        if not creds or not oauth.extract_access_token(creds):
            return "no credentials"
        try:
            return oauth.fetch_usage_for_account(
                str(num), email, creds, is_active=is_active
            )
        except Exception as exc:  # never let a fetch take the tick down
            _logger.debug("auto-switch: usage fetch for %s failed: %r", num, exc)
            return None

    def _active_over_threshold(self, active_usage: dict) -> bool:
        """True iff the active account is at/over a switch threshold.

        Uses the SAME comparison as ``decide_switch`` (via ``_window_pct``) so
        the active-first short-circuit can never disagree with the pure decision
        function about whether a switch is even possible.
        """
        h5 = _window_pct(active_usage, "five_hour")
        d7 = _window_pct(active_usage, "seven_day")
        return (
            h5 >= self._config.session_threshold
            or d7 >= self._config.weekly_threshold
        )

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
            self._persist_state()
            return SwitchDecision(
                action="stay",
                target=None,
                reason="active-usage-unknown",
                trigger_window=None,
                trigger_pct=None,
                detail=detail,
            )

        # ---- ONLINE: active fetched OK. Recovery + refresh last-known-good. -
        self._handle_online_tick({active_num: active_usage})

        # Is the active account at/over a threshold? Only then is a switch even
        # possible — and only then do we pay for the others' fetches.
        if not self._active_over_threshold(active_usage):
            # Under both thresholds → stay WITHOUT fetching the others (one API
            # call total this tick). Mirror decide_switch's under-threshold
            # decision exactly (it stays PURE and is simply not called here).
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
        usage_by_account: dict[str, dict | str | None] = {active_num: active_usage}
        for num in switchable:
            row = row_by_num.get(num)
            usage_by_account[num] = (
                self._fetch_one(row) if row is not None else "no credentials"
            )

        decision = decide_switch(
            active_num,
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

        if decision.action == "switch" and decision.target is not None:
            try:
                self._switcher.auto_switch_to(decision.target, quiet=True)
                _logger.info(
                    "auto-switch: switched to account %s (%s)",
                    decision.target,
                    decision.detail,
                )
                # Record the switch in persistent state.
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
                        f"Switched to account {decision.target}. "
                        f"{decision.detail}",
                    )
            except Exception as exc:
                _logger.exception(
                    "auto-switch: switch to %s failed: %r", decision.target, exc
                )
                self._persist_state()
                return SwitchDecision(
                    action="stay",
                    target=None,
                    reason="tick-error",
                    trigger_window=decision.trigger_window,
                    trigger_pct=decision.trigger_pct,
                    detail=f"Switch error: {exc!r}",
                )

        elif decision.reason == "all-exhausted":
            now = time.monotonic()
            if now - self._last_exhaustion_notify_ts > _EXHAUSTION_NOTIFY_GATE_SECS:
                self._last_exhaustion_notify_ts = now
                _logger.info("auto-switch: all accounts exhausted")
                if self._config.notify:
                    notify(
                        "Claude Swap — All Accounts Exhausted",
                        "All managed accounts are at their rate-limit. "
                        "Consider adding another account.",
                    )

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

    def _handle_online_tick(self, usage_by_account: dict[str, object]) -> None:
        """Mark online, handle recovery, refresh last-known-good usage."""
        was_offline = (
            self._state.consecutive_failures > 0 or self._state.offline_notified
        )
        now = _now_ts()

        if was_offline:
            _logger.info("auto-switch: usage API reachable again — back online.")
            if self._state.offline_notified and self._config.notify:
                notify(
                    "Claude Swap — Auto-switch back online",
                    "Usage monitoring resumed.",
                )

        # Merge fresh GOOD readings (never overwrite a good entry with None).
        merged = self._state.merged_usage(usage_by_account, now)

        self._state = _state_replace(
            self._state,
            last_online_ts=now,
            consecutive_failures=0,
            offline_notified=False,
            last_usage=merged,
        )

    def _persist_state(self) -> None:
        """Persist monitor state once (best-effort; never raises)."""
        save_state(self._state, self._switcher.backup_dir)

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
                time.sleep(interval)
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
                    interval = next_interval(
                        self._last_active_usage,
                        self._config,
                        self._state.consecutive_failures,
                    )
                except Exception as exc:
                    _logger.exception("auto-switch daemon: tick error: %r", exc)
                    interval = self._config.max_interval

                _logger.debug(
                    "auto-switch daemon: sleeping %ds after %s/%s",
                    interval,
                    decision.action if decision is not None else "?",
                    decision.reason if decision is not None else "?",
                )
                time.sleep(interval)
        finally:
            lock.release()
            _logger.info("auto-switch daemon: stopped")
