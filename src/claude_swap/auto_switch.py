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

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from claude_swap import oauth
from claude_swap.locking import FileLock
from claude_swap.notify import notify
from claude_swap.paths import get_backup_root

if TYPE_CHECKING:
    from claude_swap.switcher import ClaudeAccountSwitcher

_logger = logging.getLogger("claude-swap")

# ---------------------------------------------------------------------------
# Config model
# ---------------------------------------------------------------------------

_CONFIG_FILENAME = "auto-switch.json"

_DEFAULTS: dict = {
    "enabled": False,
    "session_threshold": 98.0,
    "weekly_threshold": 99.0,
    "notify": True,
    "min_interval": 20,
    "max_interval": 300,
}


@dataclass(frozen=True)
class AutoSwitchConfig:
    """Persistent configuration for the auto-switch engine."""

    enabled: bool = False
    session_threshold: float = 98.0
    weekly_threshold: float = 99.0
    notify: bool = True
    min_interval: int = 20
    max_interval: int = 300

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    @classmethod
    def from_dict(cls, data: object) -> AutoSwitchConfig:
        """Build from an arbitrary object.  Unknown/missing keys → defaults."""
        if not isinstance(data, dict):
            return cls()
        return cls(
            enabled=bool(data.get("enabled", _DEFAULTS["enabled"])),
            session_threshold=float(
                data.get("session_threshold", _DEFAULTS["session_threshold"])
            ),
            weekly_threshold=float(
                data.get("weekly_threshold", _DEFAULTS["weekly_threshold"])
            ),
            notify=bool(data.get("notify", _DEFAULTS["notify"])),
            min_interval=int(
                data.get("min_interval", _DEFAULTS["min_interval"])
            ),
            max_interval=int(
                data.get("max_interval", _DEFAULTS["max_interval"])
            ),
        )

    def to_dict(self) -> dict:
        """Convert to a JSON-serialisable dict."""
        return {
            "enabled": self.enabled,
            "session_threshold": self.session_threshold,
            "weekly_threshold": self.weekly_threshold,
            "notify": self.notify,
            "min_interval": self.min_interval,
            "max_interval": self.max_interval,
        }


# ------------------------------------------------------------------
# Module-level load / save helpers
# ------------------------------------------------------------------


def _config_path(backup_root: Path | None = None) -> Path:
    root = backup_root if backup_root is not None else get_backup_root()
    return root / _CONFIG_FILENAME


def load_config(backup_root: Path | None = None) -> AutoSwitchConfig:
    """Load config from disk; return defaults when the file is absent or bad."""
    path = _config_path(backup_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return AutoSwitchConfig.from_dict(data)
    except FileNotFoundError:
        return AutoSwitchConfig()
    except Exception as exc:
        _logger.debug("auto-switch: config load failed: %r", exc)
        return AutoSwitchConfig()


def save_config(config: AutoSwitchConfig, backup_root: Path | None = None) -> None:
    """Atomically write config to disk at 0o600."""
    path = _config_path(backup_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(config.to_dict(), indent=2), encoding="utf-8")
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        tmp.replace(path)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise exc


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
) -> int:
    """Return the next polling interval in seconds (adaptive, clamped).

    Closer to threshold → shorter interval.  The mapping is:
    - max(5h%, 7d%) >= 95% → ``min_interval``
    - max(5h%, 7d%) >= 85% → ~60s
    - else → scale linearly toward ``max_interval``
    """
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

        Returns the decision; on I/O error returns a stay/tick-error decision
        and logs the exception.
        """
        try:
            active_num, usage_by_account, switchable, live_session_nums, rotation_index = (
                self._gather()
            )
        except Exception as exc:
            _logger.exception("auto-switch: gather failed: %r", exc)
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

        return decision

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
                interval = next_interval(self._last_active_usage, self._config)
                _action = decision.action
                _reason = decision.reason
                status = (
                    f"  action={_action}  reason={_reason}  "
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
                    interval = next_interval(self._last_active_usage, self._config)
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
