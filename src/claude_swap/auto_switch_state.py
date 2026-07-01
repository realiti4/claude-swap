"""Persistent monitor state for the auto-switch connection-loss fallback.

Lives in a separate module so ``auto_switch.py`` stays focused on the decision
engine and monitor loop. The state file records whether the usage API was last
reachable, how many consecutive fetch failures we've seen, whether the user was
already told we're offline, the last switch we performed, and the last-known
GOOD usage reading per account.

File: ``<backup_root>/auto-switch-state.json`` (atomic, 0o600).

All reads are defensive: a missing or corrupt file yields defaults and never
raises, so a bad state file can never take the daemon down.

Note on timestamps: every ``*_ts`` field is a WALL-CLOCK POSIX timestamp
(``datetime.now(timezone.utc).timestamp()``), NOT ``time.monotonic()`` — they
have to survive across process restarts and be rendered as a local clock time
in ``cswap auto status``.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from claude_swap.paths import get_backup_root

_logger = logging.getLogger("claude-swap")

_STATE_FILENAME = "auto-switch-state.json"
_CONFIG_FILENAME = "auto-switch.json"

_VALID_STRATEGIES = ("reactive", "consume-first")

# Hard floor (seconds) for the near-limit "critical" poll cadence: even a
# misconfigured ``critical_interval`` never polls faster than this, to stay off
# the usage-API rate-limit radar.
_CRITICAL_INTERVAL_FLOOR = 10

_CONFIG_DEFAULTS: dict = {
    "enabled": False,
    "session_threshold": 98.0,
    "weekly_threshold": 99.0,
    "notify": True,
    # Network-polling cadence band (seconds). 60s floor keeps us off the
    # rate-limit radar (CodexBar defaults to a 5-min refresh; usage tools cache)
    # while still reacting promptly near a limit; 300s ceiling when far away.
    "min_interval": 60,
    "max_interval": 300,
    "offline_backoff_cap": 600,
    # Decision policy. "reactive" = switch only when the active account crosses
    # a threshold (the original behaviour, KEPT as the dataclass default so the
    # existing test suite is unaffected). "consume-first" = proactively keep the
    # user on the soonest-7d-reset account (use-it-or-lose-it).
    "strategy": "reactive",
    # Hysteresis margin (percentage points) on the 5h axis: a 5h-blocked account
    # only becomes available again below ``session_threshold - hysteresis``,
    # preventing available<->blocked thrash around the threshold.
    "hysteresis": 5.0,
    # Tight poll cadence (seconds) used ONLY while the 5h window is within reach
    # of ``session_threshold``, so the daemon catches the crossing and switches
    # the default login BEFORE the hard 100% wall (which aborts the in-flight
    # request). Below ``min_interval`` by design; clamped to
    # ``[_CRITICAL_INTERVAL_FLOOR, min_interval]``. Brief + self-terminating
    # (only near the 5h limit), so it does not raise the steady-state poll rate.
    "critical_interval": 15,
    # Path to a shared usage cache the daemon reads for the ACTIVE account, to
    # avoid a redundant (and rate-limit-contended) usage-API call on the account
    # Claude Code is actively using — its statusline already polls and caches the
    # same endpoint. Default = the CodexBar/statusline convention. The daemon
    # only trusts it when fresh AND its 7d reset matches the active account's
    # last-known 7d reset (account-identity guard); otherwise it falls back to a
    # direct fetch. Set to "" to disable.
    "usage_cache_file": "/tmp/claude/statusline-usage-cache.json",
}


# ---------------------------------------------------------------------------
# Persistent config model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AutoSwitchConfig:
    """Persistent configuration for the auto-switch engine.

    Lives next to ``MonitorState`` because it is also persisted engine state.
    Re-exported from ``claude_swap.auto_switch`` for backward compatibility.
    """

    enabled: bool = False
    session_threshold: float = 98.0
    weekly_threshold: float = 99.0
    notify: bool = True
    # Online polling cadence band (seconds): 60s floor (near a limit) → 300s
    # ceiling (far away). Stays in the researched 60-300s safe band.
    min_interval: int = 60
    max_interval: int = 300
    # Hard ceiling for the offline exponential backoff sleep (seconds).
    offline_backoff_cap: int = 600
    # Decision policy: "reactive" (default — KEEPS the existing test suite green)
    # or "consume-first" (proactive soonest-7d-reset ordering).
    strategy: str = "reactive"
    # 5h-axis hysteresis margin (percentage points); 0 <= hysteresis < session_threshold.
    hysteresis: float = 5.0
    # Tight near-limit poll cadence (seconds), used only while the 5h window is
    # within a few points of ``session_threshold`` so the switch fires before
    # 100%. Below ``min_interval`` by design; ``from_dict`` floors it at
    # ``_CRITICAL_INTERVAL_FLOOR`` and caps it at ``min_interval``.
    critical_interval: int = 15
    # Shared usage-cache path read for the ACTIVE account (the contended one) to
    # skip a redundant usage-API call; "" disables. See ``_CONFIG_DEFAULTS``.
    usage_cache_file: str = "/tmp/claude/statusline-usage-cache.json"

    @classmethod
    def from_dict(cls, data: object) -> AutoSwitchConfig:
        """Build from an arbitrary object.  Unknown/missing keys → defaults."""
        if not isinstance(data, dict):
            return cls()
        # Strategy must be a known value; anything else falls back to reactive.
        strategy = data.get("strategy", _CONFIG_DEFAULTS["strategy"])
        if strategy not in _VALID_STRATEGIES:
            strategy = _CONFIG_DEFAULTS["strategy"]
        session_threshold = float(
            data.get("session_threshold", _CONFIG_DEFAULTS["session_threshold"])
        )
        # Hysteresis is clamped to >= 0 (a negative margin is meaningless) AND
        # below session_threshold: if H >= S the lower band (S - H <= 0) would
        # make a 5h-blocked account NEVER clear (permanent lockout). Cap at
        # S - 1 so the dead band always has positive width.
        try:
            hysteresis = max(
                0.0, float(data.get("hysteresis", _CONFIG_DEFAULTS["hysteresis"]))
            )
        except (TypeError, ValueError):
            hysteresis = float(_CONFIG_DEFAULTS["hysteresis"])
        hysteresis = min(hysteresis, max(0.0, session_threshold - 1.0))
        min_interval = int(
            data.get("min_interval", _CONFIG_DEFAULTS["min_interval"])
        )
        max_interval = int(
            data.get("max_interval", _CONFIG_DEFAULTS["max_interval"])
        )
        # critical_interval is the tight near-limit cadence: it only makes sense
        # TIGHTER than the normal floor, and must never spam the API. Clamp to
        # [_CRITICAL_INTERVAL_FLOOR, min_interval].
        try:
            critical_interval = int(
                data.get("critical_interval", _CONFIG_DEFAULTS["critical_interval"])
            )
        except (TypeError, ValueError):
            critical_interval = int(_CONFIG_DEFAULTS["critical_interval"])
        critical_interval = max(
            _CRITICAL_INTERVAL_FLOOR, min(critical_interval, min_interval)
        )
        return cls(
            enabled=bool(data.get("enabled", _CONFIG_DEFAULTS["enabled"])),
            session_threshold=session_threshold,
            weekly_threshold=float(
                data.get("weekly_threshold", _CONFIG_DEFAULTS["weekly_threshold"])
            ),
            notify=bool(data.get("notify", _CONFIG_DEFAULTS["notify"])),
            min_interval=min_interval,
            max_interval=max_interval,
            offline_backoff_cap=int(
                data.get(
                    "offline_backoff_cap", _CONFIG_DEFAULTS["offline_backoff_cap"]
                )
            ),
            strategy=strategy,
            hysteresis=hysteresis,
            critical_interval=critical_interval,
            usage_cache_file=str(
                data.get("usage_cache_file", _CONFIG_DEFAULTS["usage_cache_file"])
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
            "offline_backoff_cap": self.offline_backoff_cap,
            "strategy": self.strategy,
            "hysteresis": self.hysteresis,
            "critical_interval": self.critical_interval,
            "usage_cache_file": self.usage_cache_file,
        }


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
# Model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MonitorState:
    """Persistent state for the auto-switch monitor loop.

    Attributes:
        last_online_ts: Wall-clock POSIX ts of the last tick where at least one
            account's usage was fetched successfully. ``None`` until first
            online tick.
        consecutive_failures: Number of consecutive offline ticks (reset to 0
            on any online tick). Drives the offline backoff.
        offline_notified: True once the one-shot "offline" notification has
            been sent; cleared on recovery so the next outage notifies again.
        exhaustion_notified: True once the one-shot "all accounts exhausted"
            notification has been sent; cleared on any non-exhausted tick so
            the next exhaustion notifies again. Persisted (unlike the old
            in-memory monotonic gate) so a daemon restart does NOT re-spam it.
        last_switch: ``{"account": str, "ts": float, "reason": str}`` of the
            most recent successful auto-switch, or ``None``.
        last_usage: ``{num: {"usage": <dict>, "fetched_at": <ts>}}`` — the
            last-known GOOD usage reading per account. Only updated with fresh
            dicts; a failed (None) fetch never overwrites a good entry.
        blocked5h: account nums currently 5h-blocked under the consume-first
            hysteresis FSM (persisted as a list since JSON has no set; the
            engine converts to a frozenset at its boundary). Only meaningful for
            the consume-first strategy.
        consecutive_incomplete: number of consecutive consume-first ticks that
            STAYED on incomplete info (a switchable peer's usage couldn't be read).
            Bounds the fast-retry: a transient miss retries quickly, but a peer
            that is persistently unreadable (no credentials, or an expired
            inactive token the daemon won't refresh) stops forcing the floor
            cadence after a few ticks. Reset to 0 on any complete/switch tick.
    """

    last_online_ts: float | None = None
    consecutive_failures: int = 0
    offline_notified: bool = False
    exhaustion_notified: bool = False
    last_switch: dict | None = None
    last_usage: dict = field(default_factory=dict)
    blocked5h: list[str] = field(default_factory=list)
    consecutive_incomplete: int = 0

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    @classmethod
    def from_dict(cls, data: object) -> MonitorState:
        """Build from an arbitrary object; missing/bad keys → defaults.

        Never raises — any malformed field falls back to its default.
        """
        if not isinstance(data, dict):
            return cls()

        def _opt_float(key: str) -> float | None:
            v = data.get(key)
            if isinstance(v, (int, float)):
                return float(v)
            return None

        def _int(key: str, default: int) -> int:
            v = data.get(key, default)
            try:
                return int(v)
            except (TypeError, ValueError):
                return default

        last_switch = data.get("last_switch")
        if not isinstance(last_switch, dict):
            last_switch = None

        last_usage = data.get("last_usage")
        if not isinstance(last_usage, dict):
            last_usage = {}
        else:
            # Keep only well-formed entries; drop anything malformed silently.
            clean: dict = {}
            for num, entry in last_usage.items():
                if (
                    isinstance(entry, dict)
                    and isinstance(entry.get("usage"), dict)
                ):
                    clean[str(num)] = {
                        "usage": entry["usage"],
                        "fetched_at": (
                            float(entry["fetched_at"])
                            if isinstance(entry.get("fetched_at"), (int, float))
                            else None
                        ),
                    }
            last_usage = clean

        failures = max(0, _int("consecutive_failures", 0))
        offline_notified = bool(data.get("offline_notified", False))
        # Normalise to match the runtime offline-notify gate (failures >= 2):
        # offline_notified can only legitimately be True once we've actually
        # declared offline, which requires >= 2 consecutive failures. A
        # corrupt/partial state with failures < 2 but offline_notified=True is
        # inconsistent and would emit a phantom "back online" at the first tick
        # after startup — force it consistent.
        if failures < 2:
            offline_notified = False

        raw_blocked = data.get("blocked5h")
        if isinstance(raw_blocked, list):
            # Keep only string entries (account nums); drop anything malformed.
            blocked5h = [str(x) for x in raw_blocked if isinstance(x, str)]
        else:
            blocked5h = []

        return cls(
            last_online_ts=_opt_float("last_online_ts"),
            consecutive_failures=failures,
            offline_notified=offline_notified,
            exhaustion_notified=bool(data.get("exhaustion_notified", False)),
            last_switch=last_switch,
            last_usage=last_usage,
            blocked5h=blocked5h,
            consecutive_incomplete=max(0, _int("consecutive_incomplete", 0)),
        )

    def to_dict(self) -> dict:
        """Convert to a JSON-serialisable dict."""
        return {
            "last_online_ts": self.last_online_ts,
            "consecutive_failures": self.consecutive_failures,
            "offline_notified": self.offline_notified,
            "exhaustion_notified": self.exhaustion_notified,
            "last_switch": self.last_switch,
            "last_usage": self.last_usage,
            "blocked5h": self.blocked5h,
            "consecutive_incomplete": self.consecutive_incomplete,
        }

    # ------------------------------------------------------------------
    # Convenience (returns a NEW instance — frozen/immutable)
    # ------------------------------------------------------------------

    def merged_usage(
        self,
        usage_by_account: Mapping[str, object],
        fetched_at: float,
        valid_nums: set[str] | None = None,
    ) -> dict:
        """Return a new ``last_usage`` map with fresh GOOD readings merged in.

        A None/non-dict fetch for an account is ignored so a good reading is
        never clobbered by a later failed fetch.

        When ``valid_nums`` is given (the set of CURRENTLY MANAGED account
        nums), entries for accounts NOT in that set are pruned — so a removed
        account's stale reading is garbage-collected rather than lingering
        forever and showing in ``cswap auto status``. Pruning is keyed on
        managed accounts (not "fetched this tick"), so a peer that simply wasn't
        probed on a normal active-only tick is preserved.
        """
        merged = dict(self.last_usage)
        if valid_nums is not None:
            merged = {k: v for k, v in merged.items() if k in valid_nums}
        for num, usage in usage_by_account.items():
            if isinstance(usage, dict):
                merged[str(num)] = {"usage": usage, "fetched_at": fetched_at}
        return merged


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------


def _state_path(backup_root: Path | None = None) -> Path:
    root = backup_root if backup_root is not None else get_backup_root()
    return root / _STATE_FILENAME


def load_state(backup_root: Path | None = None) -> MonitorState:
    """Load monitor state from disk; defaults when absent or corrupt."""
    path = _state_path(backup_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return MonitorState.from_dict(data)
    except FileNotFoundError:
        return MonitorState()
    except Exception as exc:
        _logger.debug("auto-switch: state load failed: %r", exc)
        return MonitorState()


def save_state(state: MonitorState, backup_root: Path | None = None) -> None:
    """Atomically write monitor state to disk at 0o600.

    Best-effort: a write failure is logged but never raised, so a tick can
    persist-or-not without ever taking the daemon down.
    """
    path = _state_path(backup_root)
    # Bind tmp BEFORE the try so the cleanup branch can't hit an undefined name
    # when mkdir/with_suffix raises before tmp is assigned.
    tmp: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        tmp.replace(path)
    except Exception as exc:
        _logger.debug("auto-switch: state save failed: %r", exc)
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass


__all__ = [
    "AutoSwitchConfig",
    "MonitorState",
    "load_config",
    "save_config",
    "load_state",
    "save_state",
]
