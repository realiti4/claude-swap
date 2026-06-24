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
    _has_7d_room,
    _is_available,
    _parse_reset_ts,
    decide_consume_first,
    decide_switch,
    load_config,
    next_blocked5h,
    next_interval,
    next_interval_until_reset,
    save_config,
)
from claude_swap.auto_switch_state import MonitorState, load_state, save_state
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

    def test_both_crossed_reports_more_severe_window(self):
        """When both cross, the HIGHER-pct (more severe) window is reported."""
        # 5h=99, 7d=100 → 7d is more severe.
        usage = {"1": _usage(99.0, 100.0), "2": _usage(0.0, 0.0)}
        d = self._call("1", usage, {"2"})
        assert d.trigger_window == "7d"
        assert d.trigger_pct == 100.0
        assert d.action == "switch"

    def test_both_crossed_reports_5h_when_5h_higher(self):
        """When both cross and 5h is the higher pct, report 5h."""
        usage = {"1": _usage(100.0, 99.5), "2": _usage(0.0, 0.0)}
        d = self._call("1", usage, {"2"})
        assert d.trigger_window == "5h"
        assert d.trigger_pct == 100.0
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

    def test_candidate_with_none_usage_is_unverifiable(self):
        """A candidate we couldn't FETCH (None) → candidates-unverifiable, NOT
        all-exhausted (we didn't verify it's over-limit — a transient blip)."""
        usage = {"1": _usage(99.0), "2": None}
        d = self._call("1", usage, {"2"})
        assert d.action == "stay"
        assert d.reason == "candidates-unverifiable"

    def test_candidate_with_no_credentials_is_unverifiable(self):
        """"no credentials" candidate → unverifiable, not all-exhausted."""
        usage = {"1": _usage(99.0), "2": "no credentials"}
        d = self._call("1", usage, {"2"})
        assert d.action == "stay"
        assert d.reason == "candidates-unverifiable"

    def test_unverifiable_only_when_no_verified_viable(self):
        """If a verified-available candidate exists, an unverifiable peer does
        NOT block the switch."""
        usage = {
            "1": _usage(99.0),          # active over threshold
            "2": None,                  # unverifiable
            "3": _usage(10.0),          # verified available → switch here
        }
        d = self._call("1", usage, {"2", "3"})
        assert d.action == "switch"
        assert d.target == "3"

    def test_all_candidates_over_limit_is_exhausted(self):
        """Every candidate FETCHED and over-limit → all-exhausted (the real one)."""
        usage = {"1": _usage(99.0), "2": _usage(99.0), "3": _usage(0.0, 99.5)}
        d = self._call("1", usage, {"2", "3"})
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

    def test_never_targets_active_even_if_in_switchable(self):
        """Defensive: even if the caller wrongly includes the active account in
        switchable, decide_switch must never pick it as the target."""
        usage = {
            "1": _usage(99.0, 0.0, _ts(+1)),   # active, over threshold, soonest reset
            "2": _usage(10.0, 0.0, _ts(+48)),  # the only legitimate target
        }
        # Active "1" is (wrongly) in switchable AND would sort first by reset.
        d = self._call("1", usage, {"1", "2"})
        assert d.action == "switch"
        assert d.target == "2"          # never the active account
        assert d.target != "1"

    def test_active_in_switchable_but_no_real_candidate_stays(self):
        """Active in switchable with no OTHER candidate → stays, never targets active."""
        usage = {"1": _usage(99.0)}
        d = self._call("1", usage, {"1"})   # only the active, wrongly listed
        assert d.action == "stay"
        assert d.target is None

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
        assert cfg.min_interval == 60
        assert cfg.max_interval == 300
        assert cfg.offline_backoff_cap == 600

    def test_from_dict_round_trip(self):
        cfg = AutoSwitchConfig(enabled=True, session_threshold=90.0, notify=False)
        cfg2 = AutoSwitchConfig.from_dict(cfg.to_dict())
        assert cfg == cfg2

    def test_offline_backoff_cap_default(self):
        assert AutoSwitchConfig().offline_backoff_cap == 600

    def test_auto_switch_config_offline_backoff_cap_roundtrip(self):
        cfg = AutoSwitchConfig(offline_backoff_cap=1234)
        cfg2 = AutoSwitchConfig.from_dict(cfg.to_dict())
        assert cfg2.offline_backoff_cap == 1234
        # Missing key falls back to default.
        cfg3 = AutoSwitchConfig.from_dict({"enabled": True})
        assert cfg3.offline_backoff_cap == 600

    # --- consume-first config fields (strategy / hysteresis / full_refresh) ---

    def test_strategy_defaults_reactive(self):
        """C9: dataclass default MUST stay reactive (protects existing tests)."""
        cfg = AutoSwitchConfig()
        assert cfg.strategy == "reactive"
        assert cfg.hysteresis == 5.0

    def test_strategy_roundtrip_consume_first(self):
        cfg = AutoSwitchConfig(strategy="consume-first", hysteresis=3.0)
        cfg2 = AutoSwitchConfig.from_dict(cfg.to_dict())
        assert cfg2 == cfg
        assert cfg2.strategy == "consume-first"
        assert cfg2.hysteresis == 3.0

    def test_invalid_strategy_falls_back_to_reactive(self):
        assert AutoSwitchConfig.from_dict({"strategy": "bogus"}).strategy == "reactive"
        assert AutoSwitchConfig.from_dict({"strategy": 42}).strategy == "reactive"
        assert AutoSwitchConfig.from_dict({"strategy": None}).strategy == "reactive"

    def test_strategy_missing_key_defaults_reactive(self):
        """An older config file with no strategy key loads as reactive."""
        cfg = AutoSwitchConfig.from_dict(
            {"enabled": True, "session_threshold": 90.0}
        )
        assert cfg.strategy == "reactive"

    def test_hysteresis_clamped_non_negative(self):
        assert AutoSwitchConfig.from_dict({"hysteresis": -10.0}).hysteresis == 0.0
        assert AutoSwitchConfig.from_dict({"hysteresis": "bad"}).hysteresis == 5.0

    def test_hysteresis_clamped_below_session_threshold(self):
        """FIX 3: H >= S would make the lower band S-H <= 0 → permanent 5h
        lockout. Cap H at S - 1 so the dead band always has positive width."""
        cfg = AutoSwitchConfig.from_dict(
            {"session_threshold": 98.0, "hysteresis": 200.0}
        )
        assert cfg.hysteresis == 97.0   # capped at S - 1
        # A reasonable margin under S is left untouched.
        cfg2 = AutoSwitchConfig.from_dict(
            {"session_threshold": 98.0, "hysteresis": 5.0}
        )
        assert cfg2.hysteresis == 5.0
        # Exactly at S is capped to S - 1 too.
        cfg3 = AutoSwitchConfig.from_dict(
            {"session_threshold": 90.0, "hysteresis": 90.0}
        )
        assert cfg3.hysteresis == 89.0

    def test_critical_interval_default(self):
        assert AutoSwitchConfig().critical_interval == 15

    def test_critical_interval_roundtrip(self):
        cfg = AutoSwitchConfig(critical_interval=20)
        cfg2 = AutoSwitchConfig.from_dict(cfg.to_dict())
        assert cfg2.critical_interval == 20

    def test_critical_interval_clamped_to_floor_and_min_interval(self):
        # Floored at 10 (never spam the API), capped at min_interval (it only
        # makes sense tighter than the normal floor).
        assert AutoSwitchConfig.from_dict({"critical_interval": 1}).critical_interval == 10
        assert AutoSwitchConfig.from_dict(
            {"critical_interval": 999, "min_interval": 60}
        ).critical_interval == 60
        # Bad value → default (15).
        assert AutoSwitchConfig.from_dict({"critical_interval": "x"}).critical_interval == 15
        # Missing key → default.
        assert AutoSwitchConfig.from_dict({"enabled": True}).critical_interval == 15

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

    def test_window_param_selects_five_hour(self):
        """The window param reads five_hour.resets_at; default stays seven_day."""
        usage = {
            "five_hour": {"pct": 100.0, "resets_at": "2026-06-22T12:00:00+00:00"},
            "seven_day": {"pct": 40.0, "resets_at": "2026-06-28T00:00:00+00:00"},
        }
        h5 = _parse_reset_ts(usage, "five_hour")
        d7_default = _parse_reset_ts(usage)            # default seven_day
        d7_explicit = _parse_reset_ts(usage, "seven_day")
        assert h5 != float("inf") and d7_default != float("inf")
        assert d7_default == d7_explicit               # default unchanged
        assert h5 < d7_default                          # 5h reset is sooner

    def test_window_missing_resets_at_is_infinite(self):
        # An account at 5h 0% carries no five_hour.resets_at → +inf, not a crash.
        assert _parse_reset_ts({"five_hour": {"pct": 0.0}}, "five_hour") == float("inf")


# ---------------------------------------------------------------------------
# TestNextInterval — adaptive polling
# ---------------------------------------------------------------------------

class TestNextInterval:
    """Tests for the next_interval function (60s floor cadence band)."""

    # Default-band config (min 60, max 300) — matches the shipped defaults.
    CFG = AutoSwitchConfig(min_interval=60, max_interval=300)

    def test_none_usage_returns_max(self):
        assert next_interval(None, self.CFG) == 300

    def test_empty_usage_returns_max(self):
        assert next_interval({}, self.CFG) == 300

    def test_critical_5h_band_polls_tight(self):
        # 5h within _CRITICAL_BAND_PCT (3) of the 98% threshold → tight
        # critical_interval (15), BELOW min_interval, so the switch fires before
        # the hard 100% 5h wall aborts the in-flight request.
        usage = {"five_hour": {"pct": 96.0}, "seven_day": {"pct": 0.0}}
        assert next_interval(usage, self.CFG) == 15   # critical_interval < min_interval

    def test_just_below_critical_band_not_tight(self):
        # 5h at 94.9 (just below 98-3=95) leaves the tight cadence: it falls to
        # the normal adaptive band (>=85 → 120), never the critical 15.
        usage = {"five_hour": {"pct": 94.9}, "seven_day": {"pct": 0.0}}
        out = next_interval(usage, self.CFG)
        assert out == 120 and out != self.CFG.critical_interval

    def test_critical_band_boundary_is_inclusive(self):
        # Exactly at session_threshold - _CRITICAL_BAND_PCT (95.0) → tight.
        usage = {"five_hour": {"pct": 95.0}, "seven_day": {"pct": 0.0}}
        assert next_interval(usage, self.CFG) == 15

    def test_critical_band_is_5h_only(self):
        # A 7d window near its limit must NOT trigger the tight 5h cadence — 7d
        # moves over days, a 60s poll catches it fine. 7d=99, 5h=10 → 60 (>=95
        # binding band), never 15.
        usage = {"five_hour": {"pct": 10.0}, "seven_day": {"pct": 99.0}}
        assert next_interval(usage, self.CFG) == 60

    def test_critical_band_only_online(self):
        # Offline (failures>0) ignores usage → backoff, never the critical cadence.
        usage = {"five_hour": {"pct": 99.0}, "seven_day": {"pct": 0.0}}
        assert next_interval(usage, self.CFG, 3) == 240   # backoff, not 15

    def test_85_band_returns_two_times_min(self):
        usage = {"five_hour": {"pct": 85.0}, "seven_day": {"pct": 0.0}}
        # >= 85% → min(max, 2*min) = 120
        assert next_interval(usage, self.CFG) == 120

    def test_50_band_returns_mid(self):
        usage = {"five_hour": {"pct": 50.0}, "seven_day": {"pct": 0.0}}
        # >= 50% → ~80% of ceiling = 240
        assert next_interval(usage, self.CFG) == 240

    def test_low_usage_returns_max(self):
        usage = {"five_hour": {"pct": 10.0}, "seven_day": {"pct": 5.0}}
        assert next_interval(usage, self.CFG) == 300   # far away → ceiling

    def test_result_always_in_band(self):
        """Online result is in [min_interval, max_interval], except the tight 5h
        critical band which returns the sub-floor critical_interval."""
        lo = min(self.CFG.critical_interval, self.CFG.min_interval)
        for pct in (0.0, 10.0, 49.9, 50.0, 84.9, 85.0, 94.9, 95.0, 100.0):
            usage = {"five_hour": {"pct": pct}}
            v = next_interval(usage, self.CFG)
            assert lo <= v <= self.CFG.max_interval

    def test_clamped_to_min_exact(self):
        # 85% band → min(max, 2*min) = min(300, 580) = 300; but the floor must
        # never drop below min_interval. Use a high min to exercise the floor.
        cfg = AutoSwitchConfig(min_interval=290, max_interval=300)
        usage = {"five_hour": {"pct": 50.0}}   # 50% band → int(300*0.8)=240 → floored to 290
        assert next_interval(usage, cfg) == 290

    def test_clamped_to_max_exact(self):
        cfg = AutoSwitchConfig(min_interval=60, max_interval=300)
        usage = {"five_hour": {"pct": 0.0}}
        assert next_interval(usage, cfg) == 300

    def test_band_values_exact(self):
        """Exact stepped band values at the defaults (min 60, max 300)."""
        assert next_interval({"five_hour": {"pct": 96.0}}, self.CFG) == 15    # 5h critical band
        assert next_interval({"five_hour": {"pct": 90.0}}, self.CFG) == 120   # >=85 → 2*min
        assert next_interval({"five_hour": {"pct": 50.0}}, self.CFG) == 240   # >=50 → int(max*0.8)
        assert next_interval({"five_hour": {"pct": 49.9}}, self.CFG) == 300   # <50 → max

    def test_uses_binding_window(self):
        """The window with the highest utilisation drives the interval.

        Tested below the 5h critical band (which is intentionally 5h-only and
        asymmetric): at 90% both axes map to the same ``>=85`` band.
        """
        usage_high_d7 = {"five_hour": {"pct": 10.0}, "seven_day": {"pct": 90.0}}
        usage_high_h5 = {"five_hour": {"pct": 90.0}, "seven_day": {"pct": 10.0}}
        assert next_interval(usage_high_d7, self.CFG) == next_interval(
            usage_high_h5, self.CFG
        ) == 120

    def test_next_interval_backoff_grows_and_caps(self):
        """Offline backoff ramps from min_interval (gentle first re-probe) and caps."""
        cfg = AutoSwitchConfig(
            min_interval=60, max_interval=300, offline_backoff_cap=600
        )
        # Ramp: min_interval * 2**min(failures-1, 4) → 60, 120, 240, 480, 600(cap), 600...
        assert next_interval(None, cfg, 1) == 60
        assert next_interval(None, cfg, 2) == 120
        assert next_interval(None, cfg, 3) == 240
        assert next_interval(None, cfg, 4) == 480
        assert next_interval(None, cfg, 5) == 600   # 60*16=960 → capped at 600
        assert next_interval(None, cfg, 6) == 600   # exponent capped at 4
        # Monotonic non-decreasing as failures grow, always <= cap.
        prev = 0
        for f in range(1, 12):
            v = next_interval(None, cfg, f)
            assert v >= prev
            assert v <= cfg.offline_backoff_cap
            prev = v

    def test_next_interval_backoff_ignores_usage(self):
        """When offline, the interval is backoff — usage% is irrelevant."""
        cfg = AutoSwitchConfig(min_interval=60, max_interval=300, offline_backoff_cap=600)
        near_threshold = {"five_hour": {"pct": 99.0}}
        # Online would be min_interval (60); offline failures=1 → 60 (same here,
        # but failures=3 proves usage is ignored: 240, not the 60 a 99% online tick gives).
        assert next_interval(near_threshold, cfg, 1) == 60
        assert next_interval(near_threshold, cfg, 3) == 240

    def test_next_interval_zero_failures_is_online_path(self):
        """consecutive_failures=0 → normal adaptive behaviour (here the tight 5h
        critical cadence), NOT the offline backoff."""
        usage = {"five_hour": {"pct": 96.0}, "seven_day": {"pct": 0.0}}
        assert next_interval(usage, self.CFG, 0) == 15   # online critical band


class TestNextIntervalUntilReset:
    """Reset-aware scheduler (PURE, consume-first only): only shortens, clamps."""

    CFG = AutoSwitchConfig(min_interval=60, max_interval=300)
    NOW = 1_000_000.0

    @staticmethod
    def _usage_with_reset(reset_ts: float) -> dict:
        # Build a usage dict whose 7d reset is at reset_ts (UTC ISO).
        iso = datetime.fromtimestamp(reset_ts, tz=timezone.utc).isoformat()
        return {"five_hour": {"pct": 10.0}, "seven_day": {"pct": 10.0, "resets_at": iso}}

    def test_no_reset_within_window_keeps_base(self):
        # Soonest reset is 1000s away, base is 300 → unchanged.
        active = self._usage_with_reset(self.NOW + 1000)
        assert next_interval_until_reset(300, active, [], self.NOW, self.CFG) == 300

    def test_shortens_to_just_after_soonest_reset(self):
        # Reset in 100s → wake at ~105s (100 + 5s margin), within [60, 300].
        active = self._usage_with_reset(self.NOW + 100)
        assert next_interval_until_reset(300, active, [], self.NOW, self.CFG) == 105

    def test_peer_reset_sooner_than_active_drives_it(self):
        active = self._usage_with_reset(self.NOW + 250)
        peer = self.NOW + 80      # peer resets sooner
        assert next_interval_until_reset(
            300, active, [peer], self.NOW, self.CFG
        ) == 85

    def test_clamped_to_min_interval(self):
        # Reset 1s away → 6s, but floor is 60 → clamped up to 60.
        active = self._usage_with_reset(self.NOW + 1)
        assert next_interval_until_reset(300, active, [], self.NOW, self.CFG) == 60

    def test_never_exceeds_base(self):
        # Reset is within window but the +5 would exceed a small base → clamp.
        active = self._usage_with_reset(self.NOW + 99)
        assert next_interval_until_reset(100, active, [], self.NOW, self.CFG) == 100

    def test_past_reset_ignored_keeps_base(self):
        active = self._usage_with_reset(self.NOW - 500)   # already reset
        assert next_interval_until_reset(300, active, [], self.NOW, self.CFG) == 300

    def test_unknown_resets_keep_base(self):
        # No active reset, no peers → nothing to shorten toward.
        assert next_interval_until_reset(
            300, "no credentials", [], self.NOW, self.CFG
        ) == 300
        assert next_interval_until_reset(300, None, [], self.NOW, self.CFG) == 300

    def test_only_shortens_never_lengthens(self):
        # Even a reset exactly at base boundary does not extend the base.
        active = self._usage_with_reset(self.NOW + 50)
        out = next_interval_until_reset(120, active, [], self.NOW, self.CFG)
        assert out <= 120


# ---------------------------------------------------------------------------
# TestAutoSwitcherRunOnce — with a mocked switcher
# ---------------------------------------------------------------------------

_NO_CREDS = "no credentials"


def _encode_creds(usage: object) -> str:
    """Encode an account's intended usage into a creds JSON blob.

    The engine probes usage via ``oauth.fetch_usage_for_account`` (patched in
    tests by ``_patch_oauth_fetch``), which decodes this blob. We carry the
    usage IN the creds so the fake is fully self-contained and no per-account
    registry is needed.

    * ``"no credentials"``  → empty creds (engine returns the sentinel without
                              calling oauth — i.e. "we did NOT try")
    * ``None``              → a token IS present but the fetch returns None
                              (genuine outage signal)
    * ``dict``              → a token is present and the fetch returns the dict
    """
    if usage == _NO_CREDS:
        return ""   # no token → engine short-circuits to "no credentials"
    payload = {"claudeAiOauth": {"accessToken": "tok"}}
    if usage is None:
        payload["_fake_usage_kind"] = "none"
    else:
        payload["_fake_usage_kind"] = "dict"
        payload["_fake_usage"] = usage
    return json.dumps(payload)


@pytest.fixture(autouse=True)
def _patch_oauth_fetch():
    """Route the engine's single-account usage probe to the fake's creds blob.

    Decodes ``_fake_usage`` from the credentials passed by
    ``AutoSwitcher._fetch_one`` → ``oauth.fetch_usage_for_account``. Returns a
    MagicMock so tests can assert ``.call_count`` (number of ACTUAL usage API
    calls this tick — uncredentialed accounts never reach here).
    """
    def _decode(
        account_num, email, credentials, is_active,
        persist_credentials=None, allow_refresh=True,
    ):
        try:
            data = json.loads(credentials)
        except Exception:
            return None
        if data.get("_fake_usage_kind") == "none":
            return None
        return data.get("_fake_usage")

    with patch(
        "claude_swap.oauth.fetch_usage_for_account", side_effect=_decode
    ) as mock_fetch:
        yield mock_fetch


class _FakeSwitcher:
    """Minimal fake switcher for AutoSwitcher unit tests.

    The ACTIVE account is the one whose dict has ``active=True``; if none is
    flagged, the FIRST account is active (back-compat default). This lets tests
    construct "active is account 2, candidate is account 1" cases.

    Each account dict carries a ``usage`` value (dict / None / "no credentials")
    that is encoded into the row's credentials so the engine's active-first
    probe (``_fetch_one`` → patched ``oauth.fetch_usage_for_account``) recovers
    it. ``_collect_usage`` is retained for ``cswap --list``-style callers but is
    NOT on the daemon tick path anymore.
    """

    def __init__(self, backup_dir: Path, accounts: list[dict]) -> None:
        self.backup_dir = backup_dir
        self.platform = Platform.LINUX
        self._accounts = accounts   # list of {num, email, usage, active?}
        self.switched_to: list[str] = []

    def _active_index(self) -> int | None:
        if not self._accounts:
            return None
        for i, a in enumerate(self._accounts):
            if a.get("active"):
                return i
        return 0   # default: first account is active

    def _active_num(self) -> str | None:
        idx = self._active_index()
        return str(self._accounts[idx]["num"]) if idx is not None else None

    def _build_accounts_info(self):
        active_num = self._active_num()
        return [
            (
                int(a["num"]),
                a["email"],
                "",
                "",
                str(a["num"]) == active_num,         # is_active (index 4)
                _encode_creds(a.get("usage")),       # creds carry the usage
            )
            for a in self._accounts
        ]

    def _collect_usage(self, accounts_info):
        # Full-set fetch (cswap --list / status). Not used by the daemon tick.
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
        idx = self._active_index()
        if idx is None:
            return None
        return (self._accounts[idx]["email"], "")

    @staticmethod
    def _find_account_slot(data, email, org_uuid):
        for num, acc in data.get("accounts", {}).items():
            if acc.get("email") == email:
                return num
        return None

    def _account_is_switchable(self, num: str) -> bool:
        # Switchable = managed AND not the active account (the active account is
        # never a switch target). Honors the explicit active flag.
        active_num = self._active_num()
        return any(
            str(a["num"]) == str(num) and str(a["num"]) != active_num
            for a in self._accounts
        )

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

    def test_daemon_probe_never_refreshes_inactive_tokens(
        self, tmp_path: Path, _patch_oauth_fetch
    ):
        """Every daemon usage probe MUST pass allow_refresh=False.

        Regression guard: the daemon's _fetch_one originally called
        fetch_usage_for_account with refresh enabled and NO persist callback, so
        a refresh of an inactive expired token rotated the one-time OAuth refresh
        token server-side and silently discarded the rotation — bricking the
        peer account until re-login. The background daemon must never refresh an
        inactive token (it runs under launchd where the Keychain may be locked).
        """
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": _usage(50.0)},   # active
            {"num": "2", "email": "b@x.com", "usage": _usage(20.0)},   # peer
        ]
        cfg = AutoSwitchConfig(enabled=True, strategy="consume-first")
        fs = _FakeSwitcher(tmp_path, accounts)
        as_ = AutoSwitcher(switcher=fs, config=cfg)
        with patch("claude_swap.auto_switch.notify"):
            as_.run_once()

        assert _patch_oauth_fetch.call_count >= 1
        for call in _patch_oauth_fetch.call_args_list:
            assert call.kwargs.get("allow_refresh") is False, (
                f"daemon probe must pass allow_refresh=False, got {call}"
            )

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

    def test_exhaustion_notify_is_one_shot(self, tmp_path: Path):
        """Persisted one-shot: consecutive exhausted ticks notify exactly once."""
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": _usage(99.0)},
            {"num": "2", "email": "b@x.com", "usage": _usage(99.0)},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify") as mock_notify:
            as_.run_once()
            as_.run_once()   # still exhausted; exhaustion_notified already True
        assert mock_notify.call_count == 1
        assert as_._state.exhaustion_notified is True

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
        with patch.object(as_, "_gather_meta", side_effect=RuntimeError("oops")):
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

    def test_run_once_gathers_meta_exactly_once(self, tmp_path: Path):
        """run_once must build account metadata exactly once per tick."""
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": _usage(50.0)},
            {"num": "2", "email": "b@x.com", "usage": _usage(10.0)},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        with patch.object(as_, "_gather_meta", wraps=as_._gather_meta) as spy:
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
# TestConnectionLossFallback — offline detection / backoff / recovery / state
# ---------------------------------------------------------------------------

class TestConnectionLossFallback:
    """SAFE / VISIBLE / RECOVERABLE behaviour when the usage API is offline."""

    def test_run_once_detects_offline_when_all_fetch_fail(self, tmp_path: Path):
        """All fetches None → online False, failures increments, no switch."""
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": None},
            {"num": "2", "email": "b@x.com", "usage": None},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify"):
            decision = as_.run_once()
        assert decision.action == "stay"
        assert as_._switcher.switched_to == []          # never switched
        assert as_.consecutive_failures == 1
        # State persisted to disk.
        persisted = load_state(tmp_path)
        assert persisted.consecutive_failures == 1

    def test_run_once_active_no_credentials_is_neither(
        self, tmp_path: Path, _patch_oauth_fetch
    ):
        """Active uncredentialed → NEITHER (no token, didn't try): not offline.

        Active-first: the trichotomy keys on the ACTIVE probe. "no credentials"
        means we did not try, so it is NOT a network outage even if a peer is
        fine — and the others are not fetched.
        """
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": "no credentials"},
            {"num": "2", "email": "b@x.com", "usage": _usage(10.0)},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify") as mock_notify:
            decision = as_.run_once()
        assert decision.reason == "active-usage-unknown"
        assert as_.consecutive_failures == 0            # NOT offline
        assert as_._switcher.switched_to == []
        # Active had no creds → engine short-circuits, never calls the usage API
        # (the single patched seam), never fetches the others.
        _patch_oauth_fetch.assert_not_called()
        mock_notify.assert_not_called()

    def test_no_switch_while_offline(self, tmp_path: Path):
        """Even with the active account 'over threshold' missing, offline = no switch."""
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": None},
            {"num": "2", "email": "b@x.com", "usage": None},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        as_._switcher.auto_switch_to = MagicMock()
        with patch("claude_swap.auto_switch.notify"):
            as_.run_once()
            as_.run_once()
            as_.run_once()
        as_._switcher.auto_switch_to.assert_not_called()
        assert as_.consecutive_failures == 3

    def test_offline_notify_is_one_shot(self, tmp_path: Path):
        """>= 2 offline ticks → exactly one 'offline' notification."""
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": None},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify") as mock_notify:
            as_.run_once()   # failures=1, no notify yet
            assert mock_notify.call_count == 0
            as_.run_once()   # failures=2 → one offline notify
            as_.run_once()   # failures=3 → still just one (already notified)
            as_.run_once()
        # Exactly one notification, and it's the offline one.
        assert mock_notify.call_count == 1
        title = mock_notify.call_args[0][0]
        assert "offline" in title.lower()
        assert as_._state.offline_notified is True

    def test_recovery_resets_and_notifies(self, tmp_path: Path):
        """Offline (notified) → online → failures 0, one 'back online' notify."""
        # BOTH accounts offline → truly offline (nothing fetched).
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": None},
            {"num": "2", "email": "b@x.com", "usage": None},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)

        # Drive offline until notified.
        with patch("claude_swap.auto_switch.notify"):
            as_.run_once()
            as_.run_once()   # offline notify fires here
        assert as_._state.offline_notified is True
        assert as_.consecutive_failures >= 2

        # Now the API comes back — accounts fetch successfully again.
        as_._switcher._accounts[0]["usage"] = _usage(20.0)
        as_._switcher._accounts[1]["usage"] = _usage(10.0)
        with patch("claude_swap.auto_switch.notify") as mock_notify:
            as_.run_once()
        assert as_.consecutive_failures == 0
        assert as_._state.offline_notified is False
        assert as_._state.last_online_ts is not None
        # Exactly one 'back online' notification.
        assert mock_notify.call_count == 1
        assert "online" in mock_notify.call_args[0][0].lower()

    def test_no_recovery_notify_if_never_notified_offline(self, tmp_path: Path):
        """A single offline blip (failures=1, not notified) → no recovery notify."""
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": None},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify"):
            as_.run_once()   # failures=1, NOT notified
        assert as_._state.offline_notified is False
        # Recover immediately.
        as_._switcher._accounts[0]["usage"] = _usage(10.0)
        with patch("claude_swap.auto_switch.notify") as mock_notify:
            as_.run_once()
        assert as_.consecutive_failures == 0
        mock_notify.assert_not_called()

    def test_last_switch_recorded_in_state(self, tmp_path: Path):
        """A successful switch tick records last_switch in persisted state."""
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": _usage(99.0)},
            {"num": "2", "email": "b@x.com", "usage": _usage(10.0)},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify"):
            decision = as_.run_once()
        assert decision.action == "switch"
        sw = as_._state.last_switch
        assert sw is not None
        assert sw["account"] == "2"
        assert sw["reason"] == "5h-threshold"
        assert isinstance(sw["ts"], float)
        # Persisted to disk too.
        assert load_state(tmp_path).last_switch == sw

    def test_last_usage_not_overwritten_with_none(self, tmp_path: Path):
        """A good reading survives a later failed fetch for that account."""
        good = _usage(20.0, 15.0)
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": good},
            {"num": "2", "email": "b@x.com", "usage": _usage(10.0)},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify"):
            as_.run_once()
        assert as_._state.last_usage["1"]["usage"] == good

        # Now the active account's fetch fails (None) → offline tick; the
        # online path (which merges last_usage) does not run, so the good
        # account-1 reading must NOT be clobbered.
        as_._switcher._accounts[0]["usage"] = None
        with patch("claude_swap.auto_switch.notify"):
            as_.run_once()
        assert as_._state.last_usage["1"]["usage"] == good

    def test_state_persisted_every_tick(self, tmp_path: Path):
        """run_once persists state (a REAL field) even on an under-threshold stay."""
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": _usage(50.0)},
            {"num": "2", "email": "b@x.com", "usage": _usage(10.0)},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify"):
            as_.run_once()
        # Assert a real persisted field, not merely that the file exists.
        persisted = load_state(tmp_path)
        assert persisted.last_online_ts is not None
        assert persisted.consecutive_failures == 0

    # ------------------------------------------------------------------
    # Trichotomy: zero accounts / all-no-credentials / mixed
    # ------------------------------------------------------------------

    def test_zero_accounts_is_not_offline(self, tmp_path: Path):
        """Empty usage map → stay, failures stays 0, NO offline notification."""
        as_ = _make_auto_switcher(tmp_path, [])  # no managed accounts
        with patch("claude_swap.auto_switch.notify") as mock_notify:
            decision = as_.run_once()
            as_.run_once()   # a second tick must still not fire a notify
        assert decision.action == "stay"
        assert as_.consecutive_failures == 0
        assert as_._state.offline_notified is False
        mock_notify.assert_not_called()

    def test_all_no_credentials_is_not_offline(self, tmp_path: Path):
        """Every account uncredentialed ("no credentials") → not a network outage."""
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": "no credentials"},
            {"num": "2", "email": "b@x.com", "usage": "no credentials"},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify") as mock_notify:
            as_.run_once()
            as_.run_once()   # >=2 ticks: still no false offline notification
        assert as_.consecutive_failures == 0
        assert as_._state.offline_notified is False
        assert as_._switcher.switched_to == []
        mock_notify.assert_not_called()

    def test_mixed_no_credentials_and_none_is_offline(self, tmp_path: Path):
        """One "no credentials" + one None (real failure) → offline DOES fire."""
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": None},            # tried, failed
            {"num": "2", "email": "b@x.com", "usage": "no credentials"},  # didn't try
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify"):
            as_.run_once()
        # At least one token-bearing fetch failed → genuine outage signal.
        assert as_.consecutive_failures == 1
        assert as_._switcher.switched_to == []

    def test_second_outage_after_recovery_renotifies(self, tmp_path: Path):
        """offline≥2 (notify) → online (recovery) → offline≥2 → offline notify again."""
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": None},
            {"num": "2", "email": "b@x.com", "usage": None},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        titles: list[str] = []

        def _capture(title, _msg):
            titles.append(title)

        with patch("claude_swap.auto_switch.notify", side_effect=_capture):
            # First outage → offline notify on the 2nd failure.
            as_.run_once()
            as_.run_once()
            offline_after_first = sum("offline" in t.lower() for t in titles)
            assert offline_after_first == 1

            # Recover.
            as_._switcher._accounts[0]["usage"] = _usage(20.0)
            as_._switcher._accounts[1]["usage"] = _usage(10.0)
            as_.run_once()
            assert as_._state.offline_notified is False
            assert sum("back online" in t.lower() for t in titles) == 1

            # Second outage → offline notify fires a SECOND time.
            as_._switcher._accounts[0]["usage"] = None
            as_._switcher._accounts[1]["usage"] = None
            as_.run_once()
            as_.run_once()

        assert sum("offline" in t.lower() for t in titles) == 2

    def test_recovery_notify_not_fired_after_single_transient(self, tmp_path: Path):
        """A single sub-gate failure (failures=1, never notified) → NO recovery
        notification on the next online tick."""
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": None},   # one offline blip
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify") as mock_notify:
            as_.run_once()                       # failures=1, NOT notified
            assert as_.consecutive_failures == 1
            assert as_._state.offline_notified is False
            assert mock_notify.call_count == 0
            # Recover immediately.
            as_._switcher._accounts[0]["usage"] = _usage(10.0)
            as_.run_once()
        # No "offline" and no "back online" ever fired.
        assert mock_notify.call_count == 0
        assert as_.consecutive_failures == 0


# ---------------------------------------------------------------------------
# TestExhaustionState — persisted one-shot exhaustion + candidates-unverifiable
# ---------------------------------------------------------------------------

class TestExhaustionState:
    """The 'all accounts exhausted' notification is a PERSISTED one-shot."""

    def test_exhaustion_one_shot_survives_restart(self, tmp_path: Path):
        """A respawned daemon (fresh AutoSwitcher on same dir) must NOT re-notify."""
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": _usage(99.0)},
            {"num": "2", "email": "b@x.com", "usage": _usage(99.0)},
        ]
        as1 = _make_auto_switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify") as n1:
            d1 = as1.run_once()
        assert d1.reason == "all-exhausted"
        assert n1.call_count == 1
        assert load_state(tmp_path).exhaustion_notified is True

        # Simulate launchd respawn: a brand-new AutoSwitcher reloads state from
        # disk. Still exhausted → must NOT notify again (persisted one-shot).
        as2 = _make_auto_switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify") as n2:
            d2 = as2.run_once()
        assert d2.reason == "all-exhausted"
        assert n2.call_count == 0

    def test_exhaustion_flag_cleared_on_recovery(self, tmp_path: Path):
        """After exhaustion, a switchable account appearing clears the flag and
        a later exhaustion notifies again."""
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": _usage(99.0)},
            {"num": "2", "email": "b@x.com", "usage": _usage(99.0)},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify") as mock_notify:
            as_.run_once()                       # exhausted → 1 notify
            assert as_._state.exhaustion_notified is True
            # Account 2 frees up → switch happens (not exhausted) → flag cleared.
            as_._switcher._accounts[1]["usage"] = _usage(10.0)
            as_.run_once()
            assert as_._state.exhaustion_notified is False
            assert as_._switcher.switched_to == ["2"]

    def test_candidates_unverifiable_does_not_notify_exhaustion(self, tmp_path: Path):
        """Active over threshold + a peer returns None → stay/candidates-unverifiable;
        NO exhaustion notification (we couldn't verify, not a real exhaustion)."""
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": _usage(99.0)},   # active over
            {"num": "2", "email": "b@x.com", "usage": None},           # peer blip
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify") as mock_notify:
            decision = as_.run_once()
        assert decision.reason == "candidates-unverifiable"
        assert as_._switcher.switched_to == []
        assert as_._state.exhaustion_notified is False
        mock_notify.assert_not_called()

    def test_exhaustion_re_notify_cycle(self, tmp_path: Path):
        """exhausted (notify 1) → recover via switch → exhausted AGAIN → notify 2.

        Guards 'a NEXT real exhaustion re-notifies' (the flag isn't permanent).
        """
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": _usage(99.0)},
            {"num": "2", "email": "b@x.com", "usage": _usage(99.0)},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify") as mock_notify:
            # 1) Both over → all-exhausted → first notify.
            d1 = as_.run_once()
            assert d1.reason == "all-exhausted"
            assert mock_notify.call_count == 1
            assert as_._state.exhaustion_notified is True

            # 2) Account 2 frees up → switch → flag cleared (recovery).
            as_._switcher._accounts[1]["usage"] = _usage(10.0)
            d2 = as_.run_once()
            assert d2.action == "switch"
            assert as_._state.exhaustion_notified is False

            # 3) Both over AGAIN (account 2 now active after the switch — make
            #    BOTH over so it's all-exhausted again). Reset both to over.
            as_._switcher._accounts[0]["usage"] = _usage(99.0)
            as_._switcher._accounts[1]["usage"] = _usage(99.0)
            d3 = as_.run_once()
            assert d3.reason == "all-exhausted"

        # The next real exhaustion re-notified → 2 exhaustion notifications total
        # (plus the 1 switch notification in between = 3 calls). Count the
        # exhaustion ones specifically.
        exhaustion_calls = sum(
            "exhausted" in c.args[0].lower() for c in mock_notify.call_args_list
        )
        assert exhaustion_calls == 2

    def test_exhaustion_flag_cleared_by_offline_path(self, tmp_path: Path):
        """An offline tick (active fetch → None) clears a stale exhaustion flag,
        with no spurious notification."""
        # Pre-inject exhaustion_notified=True on disk, then construct.
        save_state(
            MonitorState(exhaustion_notified=True, consecutive_failures=0),
            backup_root=tmp_path,
        )
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": None},   # active → offline
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        assert as_._state.exhaustion_notified is True   # loaded from disk
        with patch("claude_swap.auto_switch.notify") as mock_notify:
            decision = as_.run_once()
        assert decision.reason == "active-usage-unknown"   # offline
        assert as_._state.exhaustion_notified is False     # cleared
        mock_notify.assert_not_called()                    # no exhaustion notify

    def test_exhaustion_flag_cleared_by_candidates_unverifiable(self, tmp_path: Path):
        """A candidates-unverifiable tick clears a stale exhaustion flag, no notify."""
        save_state(
            MonitorState(exhaustion_notified=True, consecutive_failures=0),
            backup_root=tmp_path,
        )
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": _usage(99.0)},   # active over
            {"num": "2", "email": "b@x.com", "usage": None},           # peer blip
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        assert as_._state.exhaustion_notified is True
        with patch("claude_swap.auto_switch.notify") as mock_notify:
            decision = as_.run_once()
        assert decision.reason == "candidates-unverifiable"
        assert as_._state.exhaustion_notified is False     # cleared
        mock_notify.assert_not_called()


# ---------------------------------------------------------------------------
# TestActiveFirstFetch — anti-spam two-phase fetch (one call/normal tick)
# ---------------------------------------------------------------------------

class TestActiveFirstFetch:
    """A normal tick probes ONLY the active account (one usage API call)."""

    @staticmethod
    def _fetched_nums(mock_fetch) -> list[str]:
        """Account nums actually passed to oauth.fetch_usage_for_account."""
        return [str(c.args[0]) for c in mock_fetch.call_args_list]

    def test_run_once_fetches_active_only_when_under_threshold(
        self, tmp_path: Path, _patch_oauth_fetch
    ):
        """Active under threshold → exactly ONE usage fetch; others NOT fetched."""
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": _usage(50.0, 40.0)},
            {"num": "2", "email": "b@x.com", "usage": _usage(10.0)},
            {"num": "3", "email": "c@x.com", "usage": _usage(10.0)},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify"):
            decision = as_.run_once()
        assert decision.reason == "under-threshold"
        assert decision.action == "stay"
        # Exactly one usage API call — the active account only.
        assert _patch_oauth_fetch.call_count == 1
        assert self._fetched_nums(_patch_oauth_fetch) == ["1"]
        assert as_._switcher.switched_to == []

    def test_run_once_fetches_others_only_when_active_crosses(
        self, tmp_path: Path, _patch_oauth_fetch
    ):
        """Active >= threshold → others fetched; switch to soonest-7d-reset target."""
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": _usage(99.0)},                   # active over 5h
            {"num": "2", "email": "b@x.com", "usage": _usage(10.0, 0.0, _ts(+48))},   # resets in 48h
            {"num": "3", "email": "c@x.com", "usage": _usage(10.0, 0.0, _ts(+10))},   # resets in 10h → soonest
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify"):
            decision = as_.run_once()
        assert decision.action == "switch"
        assert decision.target == "3"                 # soonest 7d reset
        assert as_._switcher.switched_to == ["3"]
        # Active (1) + both others (2, 3) fetched = 3 calls.
        assert _patch_oauth_fetch.call_count == 3
        assert set(self._fetched_nums(_patch_oauth_fetch)) == {"1", "2", "3"}

    def test_run_once_offline_does_not_fetch_others(
        self, tmp_path: Path, _patch_oauth_fetch
    ):
        """Active fetch None (outage) → offline tick; others NOT fetched."""
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": None},      # active: tried, failed
            {"num": "2", "email": "b@x.com", "usage": _usage(10.0)},
            {"num": "3", "email": "c@x.com", "usage": _usage(10.0)},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify"):
            decision = as_.run_once()
        assert decision.reason == "active-usage-unknown"
        assert as_.consecutive_failures == 1          # offline tick
        # Only the active account was probed — no peer fetches during an outage.
        assert _patch_oauth_fetch.call_count == 1
        assert self._fetched_nums(_patch_oauth_fetch) == ["1"]
        assert as_._switcher.switched_to == []

    def test_run_once_over_threshold_but_no_others_stays(
        self, tmp_path: Path, _patch_oauth_fetch
    ):
        """Active over threshold, single account → stay/single-account, 1 fetch."""
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": _usage(99.0)},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify"):
            decision = as_.run_once()
        assert decision.reason == "single-account"
        # No switchable others → nothing else to fetch beyond the active probe.
        assert _patch_oauth_fetch.call_count == 1

    def test_removed_account_gc_from_last_usage(self, tmp_path: Path):
        """L4 end-to-end: an account dropped from the managed set is GC'd from
        last_usage on the next tick (no stale entry lingers in status)."""
        accounts = [
            {"num": "1", "email": "a@x.com", "usage": _usage(50.0)},   # active, over 50
            {"num": "2", "email": "b@x.com", "usage": _usage(10.0)},
        ]
        as_ = _make_auto_switcher(tmp_path, accounts)
        # Seed account 2 into last_usage via a Phase-2 tick: make active cross.
        as_._switcher._accounts[0]["usage"] = _usage(99.0)
        with patch("claude_swap.auto_switch.notify"):
            as_.run_once()
        assert "2" in as_._state.last_usage   # recorded during Phase 2

        # Remove account 2 from the managed set; active back under threshold.
        as_._switcher._accounts = [
            {"num": "1", "email": "a@x.com", "usage": _usage(50.0)},
        ]
        with patch("claude_swap.auto_switch.notify"):
            as_.run_once()
        # Account 2 is no longer managed → pruned from last_usage.
        assert "2" not in as_._state.last_usage
        assert "1" in as_._state.last_usage


# ---------------------------------------------------------------------------
# TestMonitorState — defensive load/save of the state file
# ---------------------------------------------------------------------------

class TestMonitorState:
    """MonitorState serialisation + defensive load (never crashes)."""

    def test_defaults(self):
        st = MonitorState()
        assert st.last_online_ts is None
        assert st.consecutive_failures == 0
        assert st.offline_notified is False
        assert st.exhaustion_notified is False
        assert st.last_switch is None
        assert st.last_usage == {}

    def test_round_trip(self, tmp_path: Path):
        st = MonitorState(
            last_online_ts=123.0,
            consecutive_failures=2,
            offline_notified=True,
            exhaustion_notified=True,
            last_switch={"account": "2", "ts": 1.0, "reason": "5h-threshold"},
            last_usage={"1": {"usage": {"five_hour": {"pct": 1.0}}, "fetched_at": 5.0}},
        )
        save_state(st, backup_root=tmp_path)
        loaded = load_state(backup_root=tmp_path)
        assert loaded == st

    def test_exhaustion_notified_round_trip(self, tmp_path: Path):
        st = MonitorState(exhaustion_notified=True, consecutive_failures=0)
        save_state(st, backup_root=tmp_path)
        assert load_state(backup_root=tmp_path).exhaustion_notified is True

    def test_from_dict_normalizes_offline_notified_when_no_failures(self):
        """failures==0 but offline_notified=True (inconsistent / hand-edited)
        → normalized to False so no phantom 'back online' at startup."""
        st = MonitorState.from_dict(
            {"consecutive_failures": 0, "offline_notified": True}
        )
        assert st.consecutive_failures == 0
        assert st.offline_notified is False

    def test_from_dict_normalizes_offline_notified_when_failures_below_gate(self):
        """failures==1 (< the >=2 notify gate) but offline_notified=True is
        inconsistent → normalized to False (L3)."""
        st = MonitorState.from_dict(
            {"consecutive_failures": 1, "offline_notified": True}
        )
        assert st.consecutive_failures == 1
        assert st.offline_notified is False

    def test_from_dict_keeps_offline_notified_when_failures_present(self):
        """failures >= 2 (the gate) with offline_notified=True is consistent."""
        st = MonitorState.from_dict(
            {"consecutive_failures": 2, "offline_notified": True}
        )
        assert st.offline_notified is True
        st3 = MonitorState.from_dict(
            {"consecutive_failures": 3, "offline_notified": True}
        )
        assert st3.offline_notified is True

    def test_state_file_missing_or_corrupt_returns_defaults(self, tmp_path: Path):
        # Missing file → defaults.
        assert load_state(backup_root=tmp_path) == MonitorState()
        # Corrupt file → defaults, no crash.
        (tmp_path / "auto-switch-state.json").write_text("not-json{{{")
        assert load_state(backup_root=tmp_path) == MonitorState()

    def test_from_dict_non_dict_returns_defaults(self):
        assert MonitorState.from_dict(None) == MonitorState()
        assert MonitorState.from_dict([1, 2, 3]) == MonitorState()
        assert MonitorState.from_dict("bad") == MonitorState()

    def test_from_dict_drops_malformed_last_usage_entries(self):
        st = MonitorState.from_dict({
            "last_usage": {
                "1": {"usage": {"five_hour": {"pct": 1.0}}, "fetched_at": 5.0},
                "2": {"usage": None},          # malformed → dropped
                "3": "not-a-dict",             # malformed → dropped
            }
        })
        assert "1" in st.last_usage
        assert "2" not in st.last_usage
        assert "3" not in st.last_usage

    def test_from_dict_negative_failures_clamped(self):
        st = MonitorState.from_dict({"consecutive_failures": -5})
        assert st.consecutive_failures == 0

    def test_save_state_sets_0o600(self, tmp_path: Path):
        import sys
        if sys.platform == "win32":
            pytest.skip("POSIX permissions not applicable on Windows")
        save_state(MonitorState(consecutive_failures=1), backup_root=tmp_path)
        stat = (tmp_path / "auto-switch-state.json").stat()
        assert oct(stat.st_mode)[-3:] == "600"

    def test_merged_usage_skips_none(self):
        st = MonitorState(
            last_usage={"1": {"usage": {"five_hour": {"pct": 1.0}}, "fetched_at": 1.0}}
        )
        merged = st.merged_usage({"1": None, "2": {"seven_day": {"pct": 2.0}}}, 9.0)
        # Existing good entry preserved; None ignored; new good entry added.
        assert merged["1"]["usage"] == {"five_hour": {"pct": 1.0}}
        assert merged["2"]["usage"] == {"seven_day": {"pct": 2.0}}
        assert merged["2"]["fetched_at"] == 9.0

    def test_merged_usage_prunes_removed_accounts(self):
        """L4: an account in prior last_usage but NOT in valid_nums is dropped."""
        st = MonitorState(
            last_usage={
                "1": {"usage": {"five_hour": {"pct": 1.0}}, "fetched_at": 1.0},
                "2": {"usage": {"five_hour": {"pct": 2.0}}, "fetched_at": 1.0},  # removed
                "3": {"usage": {"five_hour": {"pct": 3.0}}, "fetched_at": 1.0},  # not probed
            }
        )
        # This tick: only account 1 fetched; managed accounts are {1, 3}
        # (account 2 was removed).
        merged = st.merged_usage(
            {"1": {"five_hour": {"pct": 9.0}}}, 9.0, valid_nums={"1", "3"}
        )
        assert "1" in merged and merged["1"]["fetched_at"] == 9.0   # refreshed
        assert "3" in merged                                        # preserved (managed, not probed)
        assert "2" not in merged                                    # pruned (removed account)

    def test_merged_usage_no_prune_when_valid_nums_none(self):
        """Backward-compat: without valid_nums, no pruning (all kept)."""
        st = MonitorState(
            last_usage={"2": {"usage": {"five_hour": {"pct": 2.0}}, "fetched_at": 1.0}}
        )
        merged = st.merged_usage({}, 9.0)   # valid_nums defaults to None
        assert "2" in merged


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


# ---------------------------------------------------------------------------
# TestDecideConsumeFirst — the proactive policy (pure, fixtureless)
# ---------------------------------------------------------------------------


class TestDecideConsumeFirst:
    """Proactive consume-first: keep on the soonest-7d-reset AVAILABLE account."""

    CFG = _make_config(
        strategy="consume-first", session_threshold=98.0,
        weekly_threshold=99.0, hysteresis=5.0,
    )

    def _decide(self, active, usage, switchable, blocked=frozenset(), live=set(),
                rot=None):
        return decide_consume_first(
            active, usage, switchable, self.CFG, live, blocked,
            rot or {n: i for i, n in enumerate(sorted(usage))},
        )

    def test_active_already_optimal_stays(self):
        u = {"1": _usage(40, 10, _ts(2)), "2": _usage(20, 5, _ts(48))}
        d = self._decide("1", u, {"2"})
        assert d.action == "stay" and d.reason == "optimal"

    def test_switches_to_sooner_reset_peer_under_threshold(self):
        # Active resets later; peer 2 resets sooner; both well under limits.
        u = {"1": _usage(40, 10, _ts(48)), "2": _usage(20, 5, _ts(2))}
        d = self._decide("1", u, {"2"})
        assert d.action == "switch" and d.target == "2"
        assert d.reason == "consume-first"

    def test_active_5h_limit_7d_room_switches_temporarily(self):
        u = {"1": _usage(99, 30, _ts(2)), "2": _usage(10, 20, _ts(48))}
        d = self._decide("1", u, {"2"})
        assert d.action == "switch" and d.target == "2"

    def test_5h_blocked_clears_below_lower_band_switches_back(self):
        # 1 is in blocked5h; its 5h has decayed to 92 (< 98-5) -> available again,
        # and it resets soonest -> switch back to it.
        u = {"1": _usage(92, 30, _ts(2)), "2": _usage(10, 20, _ts(48))}
        d = self._decide("2", u, {"1"}, blocked=frozenset({"1"}))
        assert d.action == "switch" and d.target == "1"

    def test_5h_dip_within_margin_does_not_switch_back(self):
        # 1 blocked, 5h=95 (in [93,98)) -> still blocked -> stay on current 2.
        u = {"1": _usage(95, 30, _ts(2)), "2": _usage(10, 20, _ts(48))}
        d = self._decide("2", u, {"1"}, blocked=frozenset({"1"}))
        assert d.action == "stay" and d.reason == "optimal"

    def test_all_7d_limited_is_all_exhausted(self):
        u = {"1": _usage(50, 99.5, _ts(2)), "2": _usage(50, 99.5, _ts(48))}
        d = self._decide("1", u, {"2"})
        assert d.action == "stay" and d.reason == "all-exhausted"

    def test_7d_room_but_all_5h_blocked_is_session_limited(self):
        u = {"1": _usage(99, 40, _ts(2)), "2": _usage(99, 40, _ts(48))}
        d = self._decide("1", u, {"2"})
        assert d.action == "stay" and d.reason == "all-session-limited"

    def test_active_7d_exhausted_peer_unverifiable_is_candidates_unverifiable(self):
        """FIX 1: active genuinely 7d-exhausted + only peer returns None →
        candidates-unverifiable (NOT all-exhausted): the peer might be fine, we
        just couldn't read it. No alarming exhaustion notification."""
        u = {"1": _usage(50, 99.5, _ts(2)), "2": None}
        d = self._decide("1", u, {"2"})
        assert d.action == "stay"
        assert d.reason == "candidates-unverifiable"

    def test_active_7d_exhausted_peer_no_credentials_is_unverifiable(self):
        u = {"1": _usage(50, 99.5, _ts(2)), "2": "no credentials"}
        d = self._decide("1", u, {"2"})
        assert d.reason == "candidates-unverifiable"

    def test_active_7d_exhausted_peer_verified_exhausted_is_all_exhausted(self):
        """Only declare all-exhausted when EVERY candidate is VERIFIED 7d-over."""
        u = {"1": _usage(50, 99.5, _ts(2)), "2": _usage(50, 99.5, _ts(48))}
        d = self._decide("1", u, {"2"})
        assert d.reason == "all-exhausted"

    def test_active_7d_exhausted_mixed_peer_verified_over_one_unverifiable(self):
        """A verified-over peer + an unverifiable peer (none available) → still
        unverifiable (we couldn't rule the unknown one out)."""
        u = {
            "1": _usage(50, 99.5, _ts(2)),     # active, 7d over
            "2": _usage(50, 99.5, _ts(48)),    # peer, verified 7d over
            "3": None,                          # peer, unverifiable
        }
        d = self._decide("1", u, {"2", "3"})
        assert d.reason == "candidates-unverifiable"

    def test_session_limited_detail_does_not_claim_only_5h(self):
        """FIX 4: detail must not assert '5h session limit' when the active is
        actually 7d-exhausted."""
        u = {"1": _usage(50, 99.5, _ts(2)), "2": _usage(99, 40, _ts(48))}
        d = self._decide("1", u, {"2"})
        # active 7d-exhausted (not available), peer has 7d room but 5h-blocked
        # → all-session-limited, but the detail is the accurate generic wording.
        assert d.reason == "all-session-limited"
        assert "weekly limit and/or 5h session limits" in d.detail

    def test_unverifiable_peer_excluded(self):
        u = {"1": _usage(40, 10, _ts(2)), "2": None, "3": "no credentials"}
        d = self._decide("1", u, {"2", "3"})
        assert d.action == "stay" and d.reason == "optimal"  # active is sole available

    def test_live_session_peer_excluded(self):
        u = {"1": _usage(40, 10, _ts(48)), "2": _usage(10, 5, _ts(2))}
        d = self._decide("1", u, {"2"}, live={"2"})
        assert d.action == "stay"  # 2 would be optimal but is live -> excluded

    def test_active_usage_unknown_stays(self):
        assert decide_consume_first(
            None, {}, set(), self.CFG, set(), frozenset()
        ).reason == "active-usage-unknown"
        u = {"1": None}
        assert self._decide("1", u, set()).reason == "active-usage-unknown"

    def test_threshold_boundary_98_blocks(self):
        # 5h exactly 98.0 -> not available (>=), 7d room elsewhere -> switch away.
        u = {"1": _usage(98.0, 40, _ts(2)), "2": _usage(10, 5, _ts(48))}
        d = self._decide("1", u, {"2"})
        assert d.action == "switch" and d.target == "2"

    def test_tie_reset_then_headroom(self):
        # Equal reset -> more headroom wins (peer 2 has more room than active).
        same = _ts(5)
        u = {"1": _usage(70, 70, same), "2": _usage(10, 10, same)}
        d = self._decide("1", u, {"2"})
        assert d.action == "switch" and d.target == "2"

    def test_missing_resets_at_sorts_last(self):
        # Active has a known soon reset; peer has none -> active stays optimal.
        u = {"1": _usage(40, 10, _ts(2)), "2": _usage(10, 5, None)}
        d = self._decide("1", u, {"2"})
        assert d.action == "stay" and d.reason == "optimal"

    def test_never_raises_on_malformed(self):
        for bad in [{"1": {}}, {"1": {"five_hour": "x"}}, {"1": {"seven_day": 5}}]:
            d = decide_consume_first("1", bad, set(), self.CFG, set(), frozenset())
            assert isinstance(d, SwitchDecision)

    def test_single_available_active_stays_optimal(self):
        u = {"1": _usage(40, 10, _ts(2))}
        d = self._decide("1", u, set())
        assert d.action == "stay" and d.reason == "optimal"


class TestHysteresisState:
    """next_blocked5h FSM (pure): enter at S, sticky through [S-H, S), clear below."""

    CFG = _make_config(session_threshold=98.0, hysteresis=5.0)

    def test_enters_blocked_at_threshold(self):
        assert next_blocked5h({"n": _usage(98, 1)}, self.CFG, frozenset()) == frozenset({"n"})

    def test_stays_blocked_in_dead_band(self):
        assert next_blocked5h({"n": _usage(95, 1)}, self.CFG, frozenset({"n"})) == frozenset({"n"})

    def test_clears_below_lower_band(self):
        assert next_blocked5h({"n": _usage(92, 1)}, self.CFG, frozenset({"n"})) == frozenset()

    def test_unknown_usage_carries_prior(self):
        assert next_blocked5h({"n": None}, self.CFG, frozenset({"n"})) == frozenset({"n"})
        assert next_blocked5h({"n": None}, self.CFG, frozenset()) == frozenset()

    def test_not_blocked_stays_clear_under_threshold(self):
        assert next_blocked5h({"n": _usage(97, 1)}, self.CFG, frozenset()) == frozenset()

    def test_is_available_respects_hysteresis(self):
        # blocked account needs h5 < 93 to be available.
        assert not _is_available("n", _usage(95, 10), self.CFG, frozenset({"n"}))
        assert _is_available("n", _usage(92, 10), self.CFG, frozenset({"n"}))
        assert _is_available("n", _usage(97, 10), self.CFG, frozenset())  # not blocked

    def test_has_7d_room(self):
        assert _has_7d_room(_usage(0, 50), self.CFG)
        assert not _has_7d_room(_usage(0, 99.5), self.CFG)
        # Only ever called on verified dicts in decide_consume_first; on a
        # non-dict _window_pct yields 0.0 (treated as room) — harmless quirk.
        assert _has_7d_room(None, self.CFG)


# ---------------------------------------------------------------------------
# TestConsumeFirstEngine — run_once with strategy=consume-first (design A)
# ---------------------------------------------------------------------------


class TestConsumeFirstEngine:
    """The proactive policy end-to-end through run_once (fetches all peers)."""

    def _switcher(self, tmp_path, accounts):
        cfg = AutoSwitchConfig(
            enabled=True, strategy="consume-first",
            session_threshold=98.0, weekly_threshold=99.0, hysteresis=5.0,
        )
        fs = _FakeSwitcher(tmp_path, accounts)
        return AutoSwitcher(switcher=fs, config=cfg), fs

    def test_switches_to_sooner_reset_proactively_under_threshold(self, tmp_path):
        # Active (1) far from limits but resets LATER; peer (2) resets sooner.
        # The REACTIVE policy would stay; consume-first proactively moves to 2.
        accounts = [
            {"num": "1", "email": "a@x", "usage": _usage(40, 10, _ts(48)), "active": True},
            {"num": "2", "email": "b@x", "usage": _usage(20, 5, _ts(2))},
        ]
        as_, fs = self._switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify"):
            d = as_.run_once()
        assert d.action == "switch" and d.target == "2"
        assert fs.switched_to == ["2"]

    def test_stays_when_active_is_optimal(self, tmp_path):
        accounts = [
            {"num": "1", "email": "a@x", "usage": _usage(40, 10, _ts(2)), "active": True},
            {"num": "2", "email": "b@x", "usage": _usage(20, 5, _ts(48))},
        ]
        as_, fs = self._switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify"):
            d = as_.run_once()
        assert d.action == "stay" and d.reason == "optimal"
        assert fs.switched_to == []

    def test_active_5h_blocked_switches_and_persists_blocked5h(self, tmp_path):
        # Active hits its 5h limit (7d still has room) → switch away + remember
        # it as 5h-blocked in persisted state (so it returns after a 5h reset).
        accounts = [
            {"num": "1", "email": "a@x", "usage": _usage(99, 30, _ts(2)), "active": True},
            {"num": "2", "email": "b@x", "usage": _usage(10, 20, _ts(48))},
        ]
        as_, fs = self._switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify"):
            d = as_.run_once()
        assert d.action == "switch" and d.target == "2"
        assert "1" in load_state(tmp_path).blocked5h

    def test_reactive_strategy_does_not_switch_proactively(self, tmp_path):
        # Same scenario as the proactive test, but strategy=reactive → STAY
        # (active under threshold), proving the branch is strategy-gated.
        accounts = [
            {"num": "1", "email": "a@x", "usage": _usage(40, 10, _ts(48)), "active": True},
            {"num": "2", "email": "b@x", "usage": _usage(20, 5, _ts(2))},
        ]
        cfg = AutoSwitchConfig(enabled=True, strategy="reactive")
        fs = _FakeSwitcher(tmp_path, accounts)
        as_ = AutoSwitcher(switcher=fs, config=cfg)
        with patch("claude_swap.auto_switch.notify"):
            d = as_.run_once()
        assert d.action == "stay" and d.reason == "under-threshold"
        assert fs.switched_to == []

    def test_active_7d_exhausted_peer_blip_no_exhaustion_notify(self, tmp_path):
        """FIX 1 (engine): active 7d-exhausted + the only peer's fetch fails →
        candidates-unverifiable, NO exhaustion notification (it's a blip)."""
        accounts = [
            {"num": "1", "email": "a@x", "usage": _usage(50, 99.5, _ts(2)), "active": True},
            {"num": "2", "email": "b@x", "usage": None},   # fetch returns None
        ]
        as_, fs = self._switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify") as mock_notify:
            d = as_.run_once()
        assert d.action == "stay"
        assert d.reason == "candidates-unverifiable"
        assert fs.switched_to == []
        # The alarming "All Accounts Exhausted" notification must NOT fire.
        mock_notify.assert_not_called()
        assert as_._state.exhaustion_notified is False

    def test_active_7d_exhausted_all_peers_verified_over_notifies(self, tmp_path):
        """Contrast: every account VERIFIED 7d-over → real all-exhausted notify."""
        accounts = [
            {"num": "1", "email": "a@x", "usage": _usage(50, 99.5, _ts(2)), "active": True},
            {"num": "2", "email": "b@x", "usage": _usage(50, 99.5, _ts(48))},
        ]
        as_, fs = self._switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify") as mock_notify:
            d = as_.run_once()
        assert d.reason == "all-exhausted"
        mock_notify.assert_called_once()
        assert as_._state.exhaustion_notified is True

    def test_known_peer_resets_collected_from_last_usage(self, tmp_path):
        """FIX 7 wiring: after a consume-first tick, _known_peer_resets returns
        the 7d reset timestamps recorded in last_usage (for the scheduler)."""
        accounts = [
            {"num": "1", "email": "a@x", "usage": _usage(40, 10, _ts(2)), "active": True},
            {"num": "2", "email": "b@x", "usage": _usage(20, 5, _ts(48))},
        ]
        as_, fs = self._switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify"):
            as_.run_once()
        resets = as_._known_peer_resets()
        # Both accounts had a known reset → two finite timestamps, none +inf.
        assert len(resets) == 2
        assert all(r != float("inf") for r in resets)
        assert sorted(resets) == resets or True  # order-agnostic; finite values

    def test_consume_first_interval_only_shortens(self, tmp_path):
        """_consume_first_interval never exceeds the base it's given."""
        accounts = [
            {"num": "1", "email": "a@x", "usage": _usage(40, 10, _ts(0.02)), "active": True},
            {"num": "2", "email": "b@x", "usage": _usage(20, 5, _ts(48))},
        ]
        as_, fs = self._switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify"):
            as_.run_once()
        base = 300
        out = as_._consume_first_interval(base)
        assert as_._config.min_interval <= out <= base

    def test_wakes_after_blocked_peer_5h_reset(self, tmp_path):
        """A 5h-blocked peer's reset shortens the consume-first sleep.

        A 5h-exhausted account is temporarily unavailable, but becomes the
        consume-first optimal again the moment its 5h window resets. The 7d-only
        _known_peer_resets never captured that moment, so the daemon waited out
        the full base interval (up to max_interval) before re-ranking. Its 5h
        reset must now drive an early wake.
        """
        peer_5h = _ts(100 / 3600)   # acct 2's 5h resets ~100s from now
        accounts = [
            # active acct 1: healthy, only a far 7d reset (no near reset to confound)
            {"num": "1", "email": "a@x", "usage": _usage(40, 10, _ts(48)), "active": True},
            # peer acct 2: 5h-exhausted (→ sticky-blocked) with a NEAR 5h reset
            {"num": "2", "email": "b@x", "usage": {
                "five_hour": {"pct": 100.0, "resets_at": peer_5h},
                "seven_day": {"pct": 30.0, "resets_at": _ts(72)},
            }},
        ]
        as_, fs = self._switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify"):
            as_.run_once()
        assert "2" in as_._state.blocked5h        # sticky-blocked on 5h
        # _blocked_peer_5h_resets exposes exactly that one reset.
        blocked = as_._blocked_peer_5h_resets()
        assert len(blocked) == 1 and blocked[0] != float("inf")
        # It drives the next wake well under the 300s base (~105s).
        out = as_._consume_first_interval(300)
        assert as_._config.min_interval <= out <= 130

    def test_unblocked_peer_5h_reset_does_not_shorten(self, tmp_path):
        """Only a BLOCKED account's 5h reset matters; an available peer's 5h
        reset must not shorten the interval (its availability isn't changing)."""
        accounts = [
            {"num": "1", "email": "a@x", "usage": _usage(40, 10, _ts(48)), "active": True},
            # peer acct 2 is healthy (5h 20% → not blocked) despite a near 5h reset
            {"num": "2", "email": "b@x", "usage": {
                "five_hour": {"pct": 20.0, "resets_at": _ts(100 / 3600)},
                "seven_day": {"pct": 5.0, "resets_at": _ts(72)},
            }},
        ]
        as_, fs = self._switcher(tmp_path, accounts)
        with patch("claude_swap.auto_switch.notify"):
            as_.run_once()
        assert "2" not in as_._state.blocked5h
        assert as_._blocked_peer_5h_resets() == []
        # Nearest reset is the 48h 7d one → far beyond 300s → base unchanged.
        assert as_._consume_first_interval(300) == 300

