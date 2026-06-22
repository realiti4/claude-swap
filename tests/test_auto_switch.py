"""Tests for the auto_switch module.

Pure decision-function tests need no fixtures (no file I/O, no mocks).
AutoSwitcher tests use temp_home + Platform.LINUX mirroring TestUsageAwareSwitch.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_swap.auto_switch import (
    AutoSwitchConfig,
    AutoSwitcher,
    SwitchDecision,
    _parse_reset_ts,
    decide_switch,
    load_config,
    next_interval,
    save_config,
)
from claude_swap.models import Platform


# ---------------------------------------------------------------------------
# Helpers shared by decide_switch tests
# ---------------------------------------------------------------------------

def _make_config(**kw) -> AutoSwitchConfig:
    """Build a config with custom fields; rest from defaults."""
    defaults = dict(
        enabled=True,
        session_threshold=98.0,
        weekly_threshold=99.0,
        notify=True,
        min_interval=20,
        max_interval=300,
    )
    defaults.update(kw)
    return AutoSwitchConfig(**defaults)


def _usage(h5: float = 0.0, d7: float = 0.0, resets_at: str | None = None) -> dict:
    """Build a usage dict for tests."""
    d7_entry: dict = {"pct": d7}
    if resets_at is not None:
        d7_entry["resets_at"] = resets_at
    return {
        "five_hour": {"pct": h5},
        "seven_day": d7_entry,
    }


def _ts(delta_hours: float = 0.0) -> str:
    """ISO timestamp offset from now by delta_hours."""
    dt = datetime.now(timezone.utc) + timedelta(hours=delta_hours)
    return dt.isoformat()


# ---------------------------------------------------------------------------
# TestDecideSwitch — pure function table tests
# ---------------------------------------------------------------------------

class TestDecideSwitch:
    """Pure decide_switch tests; no fixtures, no file I/O."""

    CFG = _make_config()

    def _call(self, active_num, usage, switchable, live=None, idx=None):
        return decide_switch(
            active_num,
            usage,
            set(switchable) if not isinstance(switchable, set) else switchable,
            self.CFG,
            live or set(),
            idx,
        )

    # ------------------------------------------------------------------
    # stay — baseline
    # ------------------------------------------------------------------

    def test_under_both_thresholds_stays(self):
        d = self._call("1", {"1": _usage(50.0, 50.0)}, {"2"})
        assert d.action == "stay"
        assert d.reason == "under-threshold"
        assert d.trigger_window is None

    def test_just_below_5h_threshold_stays(self):
        """h5 = 97.9 (< 98.0) and d7 = 0 → must stay (boundary just under)."""
        d = self._call("1", {"1": _usage(97.9, 0.0), "2": _usage(0.0, 0.0)}, {"2"})
        assert d.action == "stay"
        assert d.reason == "under-threshold"
        assert d.trigger_window is None

    def test_exactly_at_5h_threshold_switches(self):
        """>= comparison: 98.0 == threshold must cross."""
        usage = {
            "1": _usage(98.0, 0.0),
            "2": _usage(0.0, 0.0),
        }
        d = self._call("1", usage, {"2"})
        assert d.action == "switch"
        assert d.reason == "5h-threshold"
        assert d.trigger_pct == 98.0

    def test_exactly_at_7d_threshold_switches(self):
        """7d exactly at 99.0 must cross."""
        usage = {
            "1": _usage(0.0, 99.0),
            "2": _usage(0.0, 0.0),
        }
        d = self._call("1", usage, {"2"})
        assert d.action == "switch"
        assert d.reason == "7d-threshold"
        assert d.trigger_pct == 99.0

    # ------------------------------------------------------------------
    # trigger window selection
    # ------------------------------------------------------------------

    def test_5h_triggers_reports_5h_window(self):
        usage = {"1": _usage(99.0, 0.0), "2": _usage(0.0, 0.0)}
        d = self._call("1", usage, {"2"})
        assert d.trigger_window == "5h"
        assert d.trigger_pct == 99.0

    def test_7d_triggers_reports_7d_window(self):
        usage = {"1": _usage(0.0, 99.5), "2": _usage(0.0, 0.0)}
        d = self._call("1", usage, {"2"})
        assert d.trigger_window == "7d"
        assert d.trigger_pct == 99.5

    def test_both_crossed_reports_5h_as_binding(self):
        """5h is the binding/faster window when both cross."""
        usage = {"1": _usage(99.0, 100.0), "2": _usage(0.0, 0.0)}
        d = self._call("1", usage, {"2"})
        assert d.trigger_window == "5h"
        assert d.trigger_pct == 99.0
        assert d.action == "switch"

    # ------------------------------------------------------------------
    # candidate selection
    # ------------------------------------------------------------------

    def test_switches_to_soonest_7d_reset(self):
        """Among two viable candidates, picks the one resetting soonest.

        Pass an explicit rotation_index so the assertion isolates the
        reset-field sort and does NOT depend on CPython set-iteration order.
        """
        usage = {
            "1": _usage(99.0, 0.0),
            "2": _usage(0.0, 0.0, resets_at=_ts(+48)),   # resets in 48h
            "3": _usage(0.0, 0.0, resets_at=_ts(+10)),   # resets in 10h → soonest
        }
        idx = {"2": 0, "3": 1}
        d = self._call("1", usage, {"2", "3"}, idx=idx)
        assert d.action == "switch"
        assert d.target == "3"

    def test_tie_on_reset_picks_more_headroom(self):
        """Same resets_at → pick account with more headroom (lower utilisation)."""
        ts = _ts(+24)
        usage = {
            "1": _usage(99.0),
            "2": _usage(50.0, 0.0, resets_at=ts),   # 50% headroom
            "3": _usage(10.0, 0.0, resets_at=ts),   # 90% headroom → wins
        }
        d = self._call("1", usage, {"2", "3"})
        assert d.target == "3"

    def test_tie_on_headroom_picks_lower_rotation_index(self):
        """Same reset and headroom → lower position in sequence wins."""
        ts = _ts(+24)
        usage = {
            "1": _usage(99.0),
            "2": _usage(20.0, 0.0, resets_at=ts),
            "3": _usage(20.0, 0.0, resets_at=ts),
        }
        idx = {"2": 0, "3": 1}   # "2" is earlier in sequence
        d = self._call("1", usage, {"2", "3"}, idx=idx)
        assert d.target == "2"

    def test_candidate_with_no_reset_sorts_last(self):
        """A candidate missing resets_at sorts after one with a known reset."""
        usage = {
            "1": _usage(99.0),
            "2": _usage(0.0, 0.0),               # no resets_at → +inf
            "3": _usage(0.0, 0.0, resets_at=_ts(+100)),  # known → wins
        }
        d = self._call("1", usage, {"2", "3"})
        assert d.target == "3"

    # ------------------------------------------------------------------
    # candidate exclusion
    # ------------------------------------------------------------------

    def test_candidate_over_own_5h_threshold_excluded(self):
        usage = {
            "1": _usage(99.0),
            "2": _usage(99.0, 0.0),   # candidate itself over threshold
        }
        d = self._call("1", usage, {"2"})
        assert d.action == "stay"
        assert d.reason == "all-exhausted"

    def test_candidate_over_own_7d_threshold_excluded(self):
        usage = {
            "1": _usage(0.0, 99.5),
            "2": _usage(0.0, 99.5),   # candidate itself over 7d threshold
        }
        d = self._call("1", usage, {"2"})
        assert d.action == "stay"
        assert d.reason == "all-exhausted"

    def test_candidate_with_none_usage_excluded(self):
        usage = {"1": _usage(99.0), "2": None}
        d = self._call("1", usage, {"2"})
        assert d.action == "stay"
        assert d.reason == "all-exhausted"

    def test_candidate_with_no_credentials_excluded(self):
        usage = {"1": _usage(99.0), "2": "no credentials"}
        d = self._call("1", usage, {"2"})
        assert d.action == "stay"
        assert d.reason == "all-exhausted"

    def test_candidate_with_live_session_excluded(self):
        usage = {"1": _usage(99.0), "2": _usage(0.0)}
        d = self._call("1", usage, {"2"}, live={"2"})
        assert d.action == "stay"
        assert d.reason == "all-exhausted"

    def test_only_viable_candidate_chosen_when_others_exhausted(self):
        """3 is viable; 2 is over threshold → choose 3."""
        usage = {
            "1": _usage(99.0),
            "2": _usage(99.0),   # over threshold
            "3": _usage(10.0),   # viable
        }
        d = self._call("1", usage, {"2", "3"})
        assert d.action == "switch"
        assert d.target == "3"

    # ------------------------------------------------------------------
    # no switchable accounts
    # ------------------------------------------------------------------

    def test_no_switchable_others_single_account(self):
        usage = {"1": _usage(99.0)}
        d = self._call("1", usage, set())
        assert d.action == "stay"
        assert d.reason == "single-account"

    def test_no_candidates_when_switchable_empty(self):
        """Empty switchable set → single-account even when threshold crossed."""
        usage = {"1": _usage(100.0, 100.0)}
        d = self._call("1", usage, [])
        assert d.reason == "single-account"

    # ------------------------------------------------------------------
    # active usage unknown
    # ------------------------------------------------------------------

    def test_active_usage_none_stays(self):
        usage = {"1": None, "2": _usage(0.0)}
        d = self._call("1", usage, {"2"})
        assert d.action == "stay"
        assert d.reason == "active-usage-unknown"

    def test_active_num_none_stays(self):
        usage = {"2": _usage(0.0)}
        d = self._call(None, usage, {"2"})
        assert d.action == "stay"
        assert d.reason == "active-usage-unknown"

    def test_active_string_sentinel_stays(self):
        usage = {"1": "no credentials", "2": _usage(0.0)}
        d = self._call("1", usage, {"2"})
        assert d.action == "stay"
        assert d.reason == "active-usage-unknown"

    # ------------------------------------------------------------------
    # malformed inputs
    # ------------------------------------------------------------------

    def test_missing_pct_treated_as_zero(self):
        """Missing pct in active usage → treated as 0 → stay."""
        usage = {
            "1": {"five_hour": {}, "seven_day": {}},
            "2": _usage(0.0),
        }
        d = self._call("1", usage, {"2"})
        assert d.action == "stay"
        assert d.reason == "under-threshold"

    def test_missing_windows_treated_as_zero(self):
        usage = {"1": {}, "2": _usage(0.0)}
        d = self._call("1", usage, {"2"})
        assert d.action == "stay"
        assert d.reason == "under-threshold"

    def test_empty_dict_usage_safe(self):
        usage = {"1": {}, "2": {}}
        d = self._call("1", usage, {"2"})
        assert d.action == "stay"   # both under threshold

    def test_non_numeric_pct_treated_as_zero(self):
        usage = {
            "1": {"five_hour": {"pct": "bad"}, "seven_day": {"pct": None}},
            "2": _usage(0.0),
        }
        d = self._call("1", usage, {"2"})
        assert d.action == "stay"

    def test_never_raises_on_wildly_malformed_input(self):
        """decide_switch must not raise regardless of input shape."""
        d = decide_switch(
            "xyz",
            {"xyz": [1, 2, 3], "abc": True},
            {"abc"},
            _make_config(),
            set(),
        )
        assert isinstance(d, SwitchDecision)

    # ------------------------------------------------------------------
    # return value integrity
    # ------------------------------------------------------------------

    def test_switch_decision_has_target(self):
        usage = {"1": _usage(99.0), "2": _usage(0.0)}
        d = self._call("1", usage, {"2"})
        assert d.action == "switch"
        assert d.target == "2"
        assert isinstance(d.detail, str)
        assert len(d.detail) > 0

    def test_decision_is_frozen_dataclass(self):
        usage = {"1": _usage(99.0), "2": _usage(0.0)}
        d = self._call("1", usage, {"2"})
        with pytest.raises((AttributeError, TypeError)):
            d.action = "mutate"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# TestAutoSwitchConfig
# ---------------------------------------------------------------------------

class TestAutoSwitchConfig:
    """Tests for AutoSwitchConfig serialisation and persistence."""

    def test_defaults(self):
        cfg = AutoSwitchConfig()
        assert cfg.enabled is False
        assert cfg.session_threshold == 98.0
        assert cfg.weekly_threshold == 99.0
        assert cfg.notify is True
        assert cfg.min_interval == 20
        assert cfg.max_interval == 300

    def test_from_dict_round_trip(self):
        cfg = AutoSwitchConfig(enabled=True, session_threshold=90.0, notify=False)
        cfg2 = AutoSwitchConfig.from_dict(cfg.to_dict())
        assert cfg == cfg2

    def test_from_dict_partial_uses_defaults(self):
        cfg = AutoSwitchConfig.from_dict({"enabled": True})
        assert cfg.enabled is True
        assert cfg.session_threshold == 98.0

    def test_from_dict_unknown_keys_ignored(self):
        cfg = AutoSwitchConfig.from_dict({"enabled": True, "future_key": "ignored"})
        assert cfg.enabled is True

    def test_from_dict_non_dict_returns_defaults(self):
        assert AutoSwitchConfig.from_dict(None) == AutoSwitchConfig()
        assert AutoSwitchConfig.from_dict([]) == AutoSwitchConfig()
        assert AutoSwitchConfig.from_dict("bad") == AutoSwitchConfig()

    def test_is_frozen(self):
        cfg = AutoSwitchConfig()
        with pytest.raises((AttributeError, TypeError)):
            cfg.enabled = True  # type: ignore[misc]

    def test_persist_and_load(self, tmp_path: Path):
        cfg = AutoSwitchConfig(enabled=True, session_threshold=90.0)
        save_config(cfg, backup_root=tmp_path)
        loaded = load_config(backup_root=tmp_path)
        assert loaded == cfg

    def test_load_missing_file_returns_defaults(self, tmp_path: Path):
        cfg = load_config(backup_root=tmp_path)
        assert cfg == AutoSwitchConfig()

    def test_load_bad_json_returns_defaults(self, tmp_path: Path):
        (tmp_path / "auto-switch.json").write_text("not-json")
        cfg = load_config(backup_root=tmp_path)
        assert cfg == AutoSwitchConfig()

    def test_save_sets_0o600_permissions(self, tmp_path: Path):
        import sys
        if sys.platform == "win32":
            pytest.skip("POSIX permissions not applicable on Windows")
        cfg = AutoSwitchConfig(enabled=True)
        save_config(cfg, backup_root=tmp_path)
        stat = (tmp_path / "auto-switch.json").stat()
        assert oct(stat.st_mode)[-3:] == "600"

    def test_to_dict_is_json_serialisable(self):
        cfg = AutoSwitchConfig(enabled=True, session_threshold=95.5)
        d = cfg.to_dict()
        json.dumps(d)  # must not raise


# ---------------------------------------------------------------------------
# TestParseResetTs — naive vs aware timestamp ordering
# ---------------------------------------------------------------------------

class TestParseResetTs:
    """Tests for _parse_reset_ts — naive ISO timestamps are treated as UTC."""

    def test_naive_and_aware_yield_same_timestamp(self):
        """A naive "...T12:00:00" and an aware "...T12:00:00+00:00" must match.

        Guarantees cross-account reset ordering is correct regardless of the
        API's tz format (naive values are assumed UTC, not local).
        """
        naive = _parse_reset_ts({"seven_day": {"resets_at": "2026-06-22T12:00:00"}})
        aware = _parse_reset_ts({"seven_day": {"resets_at": "2026-06-22T12:00:00+00:00"}})
        assert naive == aware
        assert naive != float("inf")

    def test_missing_resets_at_is_infinite(self):
        assert _parse_reset_ts({"seven_day": {"pct": 10.0}}) == float("inf")

    def test_non_dict_usage_is_infinite(self):
        assert _parse_reset_ts(None) == float("inf")
        assert _parse_reset_ts("bad") == float("inf")

    def test_unparseable_resets_at_is_infinite(self):
        assert _parse_reset_ts({"seven_day": {"resets_at": "not-a-date"}}) == float("inf")


# ---------------------------------------------------------------------------
# TestNextInterval — adaptive polling
# ---------------------------------------------------------------------------

class TestNextInterval:
    """Tests for the next_interval function."""

    CFG = AutoSwitchConfig(min_interval=20, max_interval=300)

    def test_none_usage_returns_max(self):
        assert next_interval(None, self.CFG) == 300

    def test_empty_usage_returns_max(self):
        assert next_interval({}, self.CFG) == 300

    def test_near_threshold_returns_min(self):
        usage = {"five_hour": {"pct": 96.0}, "seven_day": {"pct": 0.0}}
        assert next_interval(usage, self.CFG) == 20

    def test_mid_range_returns_between_min_and_max(self):
        usage = {"five_hour": {"pct": 85.0}, "seven_day": {"pct": 0.0}}
        result = next_interval(usage, self.CFG)
        assert self.CFG.min_interval <= result <= 60

    def test_low_usage_returns_large_interval(self):
        usage = {"five_hour": {"pct": 10.0}, "seven_day": {"pct": 5.0}}
        result = next_interval(usage, self.CFG)
        assert result > 200

    def test_clamped_to_min(self):
        cfg = AutoSwitchConfig(min_interval=100, max_interval=300)
        usage = {"five_hour": {"pct": 100.0}}
        assert next_interval(usage, cfg) >= 100

    def test_clamped_to_max(self):
        cfg = AutoSwitchConfig(min_interval=20, max_interval=300)
        usage = {"five_hour": {"pct": 0.0}}
        assert next_interval(usage, cfg) <= 300

    def test_uses_binding_window(self):
        """The window with the highest utilisation drives the interval."""
        usage_high_d7 = {"five_hour": {"pct": 10.0}, "seven_day": {"pct": 97.0}}
        usage_high_h5 = {"five_hour": {"pct": 97.0}, "seven_day": {"pct": 10.0}}
        assert next_interval(usage_high_d7, self.CFG) == next_interval(
            usage_high_h5, self.CFG
        )


# ---------------------------------------------------------------------------
# TestAutoSwitcherRunOnce — with a mocked switcher
# ---------------------------------------------------------------------------

class _FakeSwitcher:
    """Minimal fake switcher for AutoSwitcher unit tests."""

    def __init__(self, backup_dir: Path, accounts: list[dict]) -> None:
        self.backup_dir = backup_dir
        self.platform = Platform.LINUX
        self._accounts = accounts   # list of (num, email, usage_or_none)
        self.switched_to: list[str] = []

    def _build_accounts_info(self):
        return [
            (int(a["num"]), a["email"], "", "", False, "fake-creds")
            for a in self._accounts
        ]

    def _collect_usage(self, accounts_info):
        num_to_usage = {a["num"]: a.get("usage") for a in self._accounts}
        return [num_to_usage.get(str(info[0])) for info in accounts_info]

    def _get_sequence_data_migrated(self):
        return {
            "sequence": [int(a["num"]) for a in self._accounts],
            "accounts": {
                a["num"]: {"email": a["email"], "organizationUuid": ""}
                for a in self._accounts
            },
        }

    def _get_current_account(self):
        # First account is "active"
        if self._accounts:
            return (self._accounts[0]["email"], "")
        return None

    @staticmethod
    def _find_account_slot(data, email, org_uuid):
        for num, acc in data.get("accounts", {}).items():
            if acc.get("email") == email:
                return num
        return None

    def _account_is_switchable(self, num: str) -> bool:
        return any(a["num"] == num for a in self._accounts[1:])

    def _live_session_pids(self, num: str, email: str) -> list[int]:
        return []

    def auto_switch_to(self, target: str, quiet: bool = True) -> None:
        self.switched_to.append(target)


def _make_auto_switcher(backup_dir: Path, accounts: list[dict]) -> AutoSwitcher:
    cfg = AutoSwitchConfig(enabled=True)
    fs = _FakeSwitcher(backup_dir, accounts)
    return AutoSwitcher(switcher=fs, config=cfg)


class TestAutoSwitcherRunOnce:
    """AutoSwitcher.run_once tests (mocked switcher + mocked notify)."""

    def test_performs_switch_when_threshold_crossed(self, tmp_path: Path):
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": _usage(99.0)},
            {"num": "2", "email": "b@x.com", "usage": _usage(10.0)},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify") as mock_notify:
            decision = as_.run_once()
        assert decision.action == "switch"
        assert decision.target == "2"
        assert as_._switcher.switched_to == ["2"]
        mock_notify.assert_called_once()

    def test_no_switch_when_under_threshold(self, tmp_path: Path):
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": _usage(50.0)},
            {"num": "2", "email": "b@x.com", "usage": _usage(20.0)},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify") as mock_notify:
            decision = as_.run_once()
        assert decision.action == "stay"
        assert as_._switcher.switched_to == []
        mock_notify.assert_not_called()

    def test_notify_on_exhaustion(self, tmp_path: Path):
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": _usage(99.0)},
            {"num": "2", "email": "b@x.com", "usage": _usage(99.0)},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify") as mock_notify:
            decision = as_.run_once()
        assert decision.reason == "all-exhausted"
        mock_notify.assert_called_once()

    def test_exhaustion_notify_rate_limited(self, tmp_path: Path):
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": _usage(99.0)},
            {"num": "2", "email": "b@x.com", "usage": _usage(99.0)},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify") as mock_notify:
            as_.run_once()
            as_.run_once()   # second call — within gate → no second notify
        assert mock_notify.call_count == 1

    def test_notify_not_called_when_config_notify_false(self, tmp_path: Path):
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": _usage(99.0)},
            {"num": "2", "email": "b@x.com", "usage": _usage(10.0)},
        ]
        cfg = AutoSwitchConfig(enabled=True, notify=False)
        fs = _FakeSwitcher(tmp_path, accounts)
        as_ = AutoSwitcher(switcher=fs, config=cfg)
        with patch("claude_swap.auto_switch.notify") as mock_notify:
            as_.run_once()
        mock_notify.assert_not_called()

    def test_returns_stay_on_gather_error(self, tmp_path: Path):
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": _usage(99.0)},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        with patch.object(as_, "_gather", side_effect=RuntimeError("oops")):
            decision = as_.run_once()
        assert decision.action == "stay"
        assert decision.reason == "tick-error"

    def test_switch_error_returns_tick_error_stay(self, tmp_path: Path):
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": _usage(99.0)},
            {"num": "2", "email": "b@x.com", "usage": _usage(10.0)},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        as_._switcher.auto_switch_to = MagicMock(side_effect=RuntimeError("switch boom"))
        with patch("claude_swap.auto_switch.notify"):
            decision = as_.run_once()
        assert decision.reason == "tick-error"

    def test_run_once_gathers_exactly_once(self, tmp_path: Path):
        """run_once must call _gather once — the interval is sized from the
        stashed usage, not a second gather."""
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": _usage(50.0)},
            {"num": "2", "email": "b@x.com", "usage": _usage(10.0)},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        with patch.object(as_, "_gather", wraps=as_._gather) as spy:
            as_.run_once()
        assert spy.call_count == 1

    def test_run_once_stashes_active_usage(self, tmp_path: Path):
        """After run_once, _last_active_usage holds the ACTIVE account's usage."""
        active_usage = _usage(50.0, 30.0)
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": active_usage},
            {"num": "2", "email": "b@x.com", "usage": _usage(10.0)},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify"):
            as_.run_once()
        assert as_._last_active_usage == active_usage


# ---------------------------------------------------------------------------
# TestSingleFlight — concurrent lock acquisition
# ---------------------------------------------------------------------------

class TestSingleFlight:
    """Test that only one AutoSwitcher can acquire the lock at a time."""

    def test_second_watch_exits_when_lock_held(self, tmp_path: Path, capsys):
        """If the lock is held, watch() prints a message and returns without switching."""
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": _usage(10.0)},
        ]
        cfg = AutoSwitchConfig(enabled=True)
        fs1 = _FakeSwitcher(tmp_path, accounts)
        fs2 = _FakeSwitcher(tmp_path, accounts)
        as1 = AutoSwitcher(switcher=fs1, config=cfg)
        as2 = AutoSwitcher(switcher=fs2, config=cfg)

        from claude_swap.locking import FileLock

        # Acquire the lock as-if daemon is running.
        lock = FileLock(tmp_path / ".auto-switch.lock")
        assert lock.acquire(timeout=0)
        try:
            as2.watch()
        finally:
            lock.release()

        out = capsys.readouterr().out
        assert "already running" in out

    def test_run_daemon_exits_when_lock_held(self, tmp_path: Path):
        """run_daemon exits without switching when lock is already held."""
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": _usage(10.0)},
        ]
        cfg = AutoSwitchConfig(enabled=True)
        fs = _FakeSwitcher(tmp_path, accounts)
        as_ = AutoSwitcher(switcher=fs, config=cfg)

        from claude_swap.locking import FileLock

        lock = FileLock(tmp_path / ".auto-switch.lock")
        assert lock.acquire(timeout=0)
        try:
            as_.run_daemon()   # must return immediately
        finally:
            lock.release()

        assert fs.switched_to == []
