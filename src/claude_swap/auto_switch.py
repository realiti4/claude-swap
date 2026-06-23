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
    h5_window = active_usage.get("five_hour")
    d7_window = active_usage.get("seven_day")
    h5_pct: float = (
        h5_window.get("pct", 0.0)
        if isinstance(h5_window, dict) and isinstance(h5_window.get("pct"), (int, float))
        else 0.0
    )
    d7_pct: float = (
        d7_window.get("pct", 0.0)
        if isinstance(d7_window, dict) and isinstance(d7_window.get("pct"), (int, float))
        else 0.0
    )

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
        cand_h5 = cand_usage.get("five_hour")
        cand_d7 = cand_usage.get("seven_day")
        cand_h5_pct: float = (
            cand_h5.get("pct", 0.0)
            if isinstance(cand_h5, dict) and isinstance(cand_h5.get("pct"), (int, float))
            else 0.0
        )
        cand_d7_pct: float = (
            cand_d7.get("pct", 0.0)
            if isinstance(cand_d7, dict) and isinstance(cand_d7.get("pct"), (int, float))
            else 0.0
        )
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

    When online (``consecutive_failures == 0``) use the adaptive mapping —
    closer to threshold → shorter interval:
    - max(5h%, 7d%) >= 95% → ``min_interval``
    - max(5h%, 7d%) >= 85% → ~60s
    - else → scale linearly toward ``max_interval``
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
        return config.min_interval
    if approach >= 85.0:
        return min(60, config.max_interval)

    # Linear interpolation from 0% → max_interval to 85% → 60s
    frac = max(0.0, approach) / 85.0
    interpolated = config.max_interval - frac * (config.max_interval - 60)
    return max(config.min_interval, min(config.max_interval, int(interpolated)))


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

    def _gather(
        self,
    ) -> tuple[
        str | None,
        dict[str, dict | str | None],
        set[str],
        set[str],
        dict[str, int],
    ]:
        """Collect all state needed by ``decide_switch`` in one pass.

        Returns:
            (active_num, usage_by_account, switchable, live_session_nums,
             rotation_index)
        """
        s = self._switcher

        # Build accounts info once — shared with usage collection.
        accounts_info = s._build_accounts_info()
        usages = s._collect_usage(accounts_info)
        usage_by_account: dict[str, dict | str | None] = {
            str(info[0]): usage
            for info, usage in zip(accounts_info, usages)
        }

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

        return active_num, usage_by_account, switchable, live_session_nums, rotation_index

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

        Returns the decision; on gather error returns a stay/tick-error
        decision and logs the exception. Persists monitor state every tick.
        """
        try:
            active_num, usage_by_account, switchable, live_session_nums, rotation_index = (
                self._gather()
            )
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

        # Stash the active account's usage so watch/run_daemon can size the
        # next interval without gathering a second time.
        active_usage_now = (
            usage_by_account.get(active_num) if active_num is not None else None
        )
        self._last_active_usage = (
            active_usage_now if isinstance(active_usage_now, dict) else None
        )

        # Online/offline classification is a TRICHOTOMY, not a binary, because
        # ``_collect_usage`` returns one of three things per account:
        #   * dict              → reachable, got data            (online signal)
        #   * None              → had a token, the HTTP call FAILED (outage signal)
        #   * "no credentials"  → no token, we did NOT try        (no signal)
        # The offline state machine must fire ONLY when nothing got data AND at
        # least one token-bearing fetch actually failed. Otherwise (zero managed
        # accounts, or every account uncredentialed) we genuinely can't check —
        # but that is NOT a network outage, so we must not raise a false
        # "Can't reach the usage API" notification.
        got_data = any(isinstance(u, dict) for u in usage_by_account.values())
        attempted = any(u is None for u in usage_by_account.values())

        if not got_data:
            if attempted:
                # Genuine network/API outage: we tried and every attempt failed.
                self._handle_offline_tick()
                detail = (
                    "Usage API unreachable (offline) — monitoring paused, "
                    "will resume automatically. Not switching on stale data."
                )
            else:
                # Zero accounts OR all uncredentialed: can't check, but NOT a
                # network outage — do NOT touch the offline state machine (no
                # false offline notification, no failure increment).
                detail = (
                    "No managed accounts to check."
                    if not usage_by_account
                    else "No usable account credentials — can't read usage "
                    "(not a network outage)."
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

        # ---- ONLINE TICK ---------------------------------------------------
        # got_data is True → online. Recovery handling + refresh last-known-good
        # usage + persist.
        self._handle_online_tick(usage_by_account)

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
