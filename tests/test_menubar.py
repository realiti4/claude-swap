"""Tests for the menu bar module.

These tests never import or run rumps/AppKit. They exercise the pure helpers
(settings store, title/label formatting, plist rendering) only.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_swap import menubar


def test_settings_defaults_when_file_missing(tmp_path: Path):
    s = menubar.MenuBarSettings.load(tmp_path / "nope.json")
    assert s.show_account_name is True
    assert s.title_pct == "both"
    assert s.refresh_interval == 60


def test_settings_round_trip(tmp_path: Path):
    path = tmp_path / "menubar_settings.json"
    original = menubar.MenuBarSettings(
        show_account_name=False,
        title_pct="5h",
        refresh_interval=300,
    )
    original.save(path)
    loaded = menubar.MenuBarSettings.load(path)
    assert loaded == original


def test_settings_corrupt_file_falls_back_to_defaults(tmp_path: Path):
    path = tmp_path / "menubar_settings.json"
    path.write_text("{ this is not json", encoding="utf-8")
    s = menubar.MenuBarSettings.load(path)
    assert s == menubar.MenuBarSettings()


def test_settings_ignores_unknown_and_bad_types(tmp_path: Path):
    path = tmp_path / "menubar_settings.json"
    path.write_text(
        json.dumps(
            {"refresh_interval": "fast", "bogus": 1, "show_account_name": False}
        ),
        encoding="utf-8",
    )
    s = menubar.MenuBarSettings.load(path)
    # bad-typed refresh_interval falls back to default; valid bool is kept
    assert s.refresh_interval == 60
    assert s.show_account_name is False


_USAGE = {
    "five_hour": {"pct": 42.0},
    "seven_day": {"pct": 18.0},
    "spend": {"pct": 30.0, "used": 3.0, "limit": 10.0},
}


def test_tightest_pct_uses_max_window():
    assert menubar.tightest_pct(_USAGE) == 42.0


def test_tightest_pct_none_for_non_dict_or_empty():
    assert menubar.tightest_pct("no credentials") is None
    assert menubar.tightest_pct(None) is None
    assert menubar.tightest_pct({"spend": {"pct": 90.0}}) is None  # no 5h/7d


def test_usage_summary_dict():
    assert menubar.usage_summary(_USAGE) == "5h 42% · 7d 18% · $ 30%"


def test_usage_summary_partial_windows():
    assert menubar.usage_summary({"five_hour": {"pct": 5.0}}) == "5h 5%"


def test_usage_summary_string_sentinel_passthrough():
    assert menubar.usage_summary("no credentials") == "no credentials"


def test_usage_summary_none():
    assert menubar.usage_summary(None) == "usage unavailable"


def test_format_account_label():
    label = menubar.format_account_label(2, "loc@papaya.asia", _USAGE)
    assert label == "2  loc@papaya.asia  5h 42% · 7d 18% · $ 30%"


def test_format_title_name_and_5h():
    s = menubar.MenuBarSettings(show_account_name=True, title_pct="5h")
    assert menubar.format_title("loc@papaya.asia", _USAGE, s) == "⇄ loc · 42%"


def test_format_title_name_only_when_pct_off():
    s = menubar.MenuBarSettings(show_account_name=True, title_pct="off")
    assert menubar.format_title("loc@papaya.asia", _USAGE, s) == "⇄ loc"


def test_format_title_5h_only():
    s = menubar.MenuBarSettings(show_account_name=False, title_pct="5h")
    assert menubar.format_title("loc@papaya.asia", _USAGE, s) == "⇄ 42%"


def test_format_title_7d_only():
    s = menubar.MenuBarSettings(show_account_name=False, title_pct="7d")
    assert menubar.format_title("loc@papaya.asia", _USAGE, s) == "⇄ 18%"


def test_format_title_both_windows():
    s = menubar.MenuBarSettings(show_account_name=False, title_pct="both")
    assert menubar.format_title("loc@papaya.asia", _USAGE, s) == "⇄ 42% · 18%"


def test_format_title_both_windows_with_name():
    s = menubar.MenuBarSettings(show_account_name=True, title_pct="both")
    assert menubar.format_title("loc@papaya.asia", _USAGE, s) == "⇄ loc · 42% · 18%"


def test_format_title_icon_only_when_off():
    s = menubar.MenuBarSettings(show_account_name=False, title_pct="off")
    assert menubar.format_title("loc@papaya.asia", _USAGE, s) == "⇄"


def test_format_title_icon_only_when_no_active_account():
    s = menubar.MenuBarSettings(show_account_name=True, title_pct="both")
    assert menubar.format_title(None, None, s) == "⇄"


def test_format_title_truncates_long_local_part():
    s = menubar.MenuBarSettings(show_account_name=True, title_pct="off")
    title = menubar.format_title("averylonglocalpart@example.com", None, s)
    assert title == "⇄ averylonglo*"  # 12 chars: 11 letters + asterisk marker


def test_format_title_both_drops_unavailable_windows():
    s = menubar.MenuBarSettings(show_account_name=False, title_pct="both")
    assert menubar.format_title("loc@x.com", "no credentials", s) == "⇄"


def test_format_title_both_keeps_available_window():
    s = menubar.MenuBarSettings(show_account_name=False, title_pct="both")
    # only 5h present -> 7d dropped, no trailing separator
    assert menubar.format_title("loc@x.com", {"five_hour": {"pct": 9.0}}, s) == "⇄ 9%"


def test_settings_auto_switch_defaults(tmp_path: Path):
    s = menubar.MenuBarSettings.load(tmp_path / "missing.json")
    assert s.auto_switch_enabled is False
    assert s.auto_switch_threshold == 95
    assert s.auto_switch_cooldown == 600
    assert s.auto_switch_interval == 0


def test_settings_auto_switch_round_trip(tmp_path: Path):
    path = tmp_path / "settings.json"
    orig = menubar.MenuBarSettings(
        auto_switch_enabled=True,
        auto_switch_threshold=80,
        auto_switch_cooldown=300,
        auto_switch_interval=180,
    )
    orig.save(path)
    assert menubar.MenuBarSettings.load(path) == orig


def test_state_defaults(tmp_path: Path):
    st = menubar.MenuBarState.load(tmp_path / "missing.json")
    assert st.last_switch_at == 0.0
    assert st.last_noswap_notify_at == 0.0


def test_state_round_trip(tmp_path: Path):
    path = tmp_path / "state.json"
    st = menubar.MenuBarState(last_switch_at=1750000000.5, last_noswap_notify_at=1750000123.0)
    st.save(path)
    assert menubar.MenuBarState.load(path) == st


def test_state_corrupt_falls_back(tmp_path: Path):
    path = tmp_path / "state.json"
    path.write_text("not json {", encoding="utf-8")
    assert menubar.MenuBarState.load(path) == menubar.MenuBarState()


def test_state_accepts_int_timestamps(tmp_path: Path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"last_switch_at": 1750000000, "last_noswap_notify_at": 0}),
                    encoding="utf-8")
    st = menubar.MenuBarState.load(path)
    assert st.last_switch_at == 1750000000.0
    assert isinstance(st.last_switch_at, float)


def _acct(num, pct5, pct7, active=False):
    return (num, f"a{num}@x.com", active,
            {"five_hour": {"pct": pct5}, "seven_day": {"pct": pct7}})


def test_decide_active_has_headroom():
    accts = [_acct(1, 50, 10, active=True), _acct(2, 5, 5)]
    assert menubar.decide_auto_switch(accts, 95) == ("none", None)


def test_decide_active_over_5h_picks_best():
    accts = [_acct(1, 96, 10, active=True), _acct(2, 40, 30), _acct(3, 10, 80)]
    assert menubar.decide_auto_switch(accts, 95) == ("switch", 2)


def test_decide_active_over_7d():
    accts = [_acct(1, 10, 97, active=True), _acct(2, 50, 20)]
    assert menubar.decide_auto_switch(accts, 95) == ("switch", 2)


def test_decide_skips_saturated_candidates():
    accts = [_acct(1, 99, 10, active=True), _acct(2, 96, 5), _acct(3, 97, 99)]
    assert menubar.decide_auto_switch(accts, 95) == ("no_candidate", None)


def test_decide_tie_break_by_7d_then_5h():
    # both candidates worst=40; lower 7d wins -> acct 2 (7d 30 < 7d 40)
    accts = [_acct(1, 99, 10, active=True), _acct(2, 40, 30), _acct(3, 20, 40)]
    assert menubar.decide_auto_switch(accts, 95) == ("switch", 2)


def test_decide_tie_break_by_5h_when_worst_and_7d_equal():
    # Both candidates: worst=40, 7d=40; differ only on 5h -> lower 5h wins.
    accts = [_acct(1, 99, 10, active=True), _acct(2, 30, 40), _acct(3, 20, 40)]
    # acct2 key=(40,40,30), acct3 key=(40,40,20) -> acct3 (lower 5h)
    assert menubar.decide_auto_switch(accts, 95) == ("switch", 3)


def test_decide_unknown_active():
    accts = [(1, "a@x", True, "no credentials"), _acct(2, 5, 5)]
    assert menubar.decide_auto_switch(accts, 95) == ("unknown_active", None)


def test_decide_active_missing_one_window_is_unknown():
    accts = [(1, "a@x", True, {"five_hour": {"pct": 99}}), _acct(2, 5, 5)]
    assert menubar.decide_auto_switch(accts, 95) == ("unknown_active", None)


def test_decide_excludes_unknown_candidate():
    accts = [_acct(1, 99, 10, active=True), (2, "b@x", False, None), _acct(3, 50, 50)]
    assert menubar.decide_auto_switch(accts, 95) == ("switch", 3)


def test_decide_no_other_accounts():
    accts = [_acct(1, 99, 10, active=True)]
    assert menubar.decide_auto_switch(accts, 95) == ("no_candidate", None)


def test_decide_no_active_account():
    accts = [_acct(1, 50, 10), _acct(2, 5, 5)]
    assert menubar.decide_auto_switch(accts, 95) == ("none", None)


def test_plan_switch_outside_cooldown():
    st = menubar.MenuBarState(last_switch_at=0.0)
    s = menubar.MenuBarSettings(auto_switch_cooldown=600)
    assert menubar.plan_auto_switch(("switch", 2), st, s, 1000.0) == ("switch", 2)


def test_plan_switch_within_cooldown():
    st = menubar.MenuBarState(last_switch_at=900.0)
    s = menubar.MenuBarSettings(auto_switch_cooldown=600)
    assert menubar.plan_auto_switch(("switch", 2), st, s, 1000.0) == ("cooldown", None)


def test_plan_no_candidate_past_rate_limit():
    st = menubar.MenuBarState(last_noswap_notify_at=0.0)
    s = menubar.MenuBarSettings()
    assert menubar.plan_auto_switch(("no_candidate", None), st, s, 5000.0) == ("notify_noswap", None)


def test_plan_no_candidate_within_rate_limit():
    st = menubar.MenuBarState(last_noswap_notify_at=4000.0)
    s = menubar.MenuBarSettings()
    assert menubar.plan_auto_switch(("no_candidate", None), st, s, 5000.0) == ("noop", None)


def test_plan_none_and_unknown_are_noop():
    st, s = menubar.MenuBarState(), menubar.MenuBarSettings()
    assert menubar.plan_auto_switch(("none", None), st, s, 1e9) == ("noop", None)
    assert menubar.plan_auto_switch(("unknown_active", None), st, s, 1e9) == ("noop", None)


def test_snapshot_full_fetches_all(monkeypatch):
    seen = {}
    class _SW:
        _logger = type("L", (), {"debug": staticmethod(lambda *a, **k: None)})()
        def _build_accounts_info(self):
            creds = ""
            return [(1, "a@x", "", "", True, creds), (2, "b@x", "", "", False, creds)]
        def _collect_usage(self, info, only=None):
            seen["only"] = only
            return [None, None]
    menubar._snapshot(_SW(), full=True)
    assert seen["only"] is None  # full -> all accounts


def test_snapshot_incremental_fetches_active_only():
    seen = {}
    class _SW:
        _logger = type("L", (), {"debug": staticmethod(lambda *a, **k: None)})()
        def _build_accounts_info(self):
            return [(1, "a@x", "", "", False, ""), (2, "b@x", "", "", True, "")]
        def _collect_usage(self, info, only=None):
            seen["only"] = only
            return [None, None]
    menubar._snapshot(_SW(), full=False)
    assert seen["only"] == {"2"}  # incremental -> only the active account


def test_settings_strategy_default():
    assert menubar.MenuBarSettings().auto_switch_strategy == "reactive"


def test_settings_strategy_round_trip(tmp_path: Path):
    path = tmp_path / "s.json"
    s = menubar.MenuBarSettings(auto_switch_strategy="consume-first")
    s.save(path)
    assert menubar.MenuBarSettings.load(path).auto_switch_strategy == "consume-first"


def test_state_blocked_round_trip(tmp_path: Path):
    path = tmp_path / "state.json"
    st = menubar.MenuBarState(last_switch_at=1.0, blocked=["2", "3"])
    st.save(path)
    loaded = menubar.MenuBarState.load(path)
    assert loaded.blocked == ["2", "3"]
    assert loaded.last_switch_at == 1.0


def test_state_blocked_defaults_when_malformed(tmp_path: Path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"blocked": [1, 2]}), encoding="utf-8")  # non-str elems
    assert menubar.MenuBarState.load(path).blocked == []
    path.write_text(json.dumps({"blocked": "nope"}), encoding="utf-8")
    assert menubar.MenuBarState.load(path).blocked == []


def test_next_blocked_enter_stay_exit():
    prev = frozenset()
    # enter at >= threshold
    assert menubar.next_blocked({"1": 96.0}, 95, 5, prev) == frozenset({"1"})
    # stay blocked within the dead band (95-5=90 .. 95)
    assert menubar.next_blocked({"1": 92.0}, 95, 5, frozenset({"1"})) == frozenset({"1"})
    # exit only below threshold - hysteresis
    assert menubar.next_blocked({"1": 89.0}, 95, 5, frozenset({"1"})) == frozenset()
    # not blocked and below threshold -> stays out
    assert menubar.next_blocked({"1": 92.0}, 95, 5, frozenset()) == frozenset()


def test_next_blocked_unknown_carries_prev():
    assert menubar.next_blocked({"1": None}, 95, 5, frozenset({"1"})) == frozenset({"1"})
    assert menubar.next_blocked({"1": None}, 95, 5, frozenset()) == frozenset()


def test_resets_at_ts_orders_and_handles_missing():
    early = {"resets_at": "2026-06-24T07:00:00+00:00"}
    late = {"resets_at": "2026-06-26T07:00:00+00:00"}
    assert menubar._resets_at_ts(early) < menubar._resets_at_ts(late)
    assert menubar._resets_at_ts({"pct": 5.0}) == float("inf")   # no resets_at
    assert menubar._resets_at_ts({"resets_at": "garbage"}) == float("inf")
    assert menubar._resets_at_ts(None) == float("inf")


def _ra(num, pct5, pct7, active=False):
    return (num, f"a{num}@x.com", active,
            {"five_hour": {"pct": pct5}, "seven_day": {"pct": pct7}})


def test_decide_reactive_hysteresis_excludes_blocked_candidate():
    # active over limit; only peer (#2) is at 92 — within the 90..95 dead band.
    accts = [_ra(1, 99, 10, active=True), _ra(2, 92, 20)]
    # not blocked -> 92 < 95 -> eligible -> switch
    assert menubar.decide_auto_switch(accts, 95, frozenset()) == ("switch", 2)
    # blocked -> must clear 90 -> 92 >= 90 -> ineligible -> no candidate
    assert menubar.decide_auto_switch(accts, 95, frozenset({"2"})) == ("no_candidate", None)


def test_decide_reactive_unverifiable_when_only_peer_unreadable():
    accts = [_ra(1, 99, 10, active=True), (2, "b@x", False, "no credentials")]
    assert menubar.decide_auto_switch(accts, 95, frozenset()) == ("no_candidate_unverifiable", None)


def test_decide_reactive_exhausted_stays_no_candidate():
    accts = [_ra(1, 99, 10, active=True), _ra(2, 96, 50)]  # peer over limit, readable
    assert menubar.decide_auto_switch(accts, 95, frozenset()) == ("no_candidate", None)


def test_plan_silent_outcomes_are_noop():
    st, s = menubar.MenuBarState(), menubar.MenuBarSettings()
    assert menubar.plan_auto_switch(("no_candidate_unverifiable", None), st, s, 1e9) == ("noop", None)
    assert menubar.plan_auto_switch(("all_session_limited", None), st, s, 1e9) == ("noop", None)


def _cf(num, pct5, pct7, reset7, active=False):
    return (num, f"a{num}@x.com", active,
            {"five_hour": {"pct": pct5},
             "seven_day": {"pct": pct7, "resets_at": reset7}})

_R_EARLY = "2026-06-24T07:00:00+00:00"
_R_MID = "2026-06-25T07:00:00+00:00"
_R_LATE = "2026-06-26T07:00:00+00:00"


def test_consume_first_picks_soonest_weekly_reset():
    # active #1 resets late; #2 resets early -> switch to #2 (consume it first).
    accts = [_cf(1, 10, 20, _R_LATE, active=True), _cf(2, 10, 20, _R_EARLY)]
    assert menubar.decide_consume_first(accts, 95, frozenset()) == ("switch", 2)


def test_consume_first_stays_when_active_is_optimal():
    accts = [_cf(1, 10, 20, _R_EARLY, active=True), _cf(2, 10, 20, _R_LATE)]
    assert menubar.decide_consume_first(accts, 95, frozenset()) == ("none", None)


def test_consume_first_tie_break_headroom_then_rotation():
    # equal reset -> more headroom (lower worst) wins; then rotation order.
    accts = [_cf(1, 99, 99, _R_LATE, active=True),
             _cf(2, 40, 30, _R_EARLY), _cf(3, 10, 80, _R_EARLY)]
    # #2 worst=40, #3 worst=80 -> #2
    assert menubar.decide_consume_first(accts, 95, frozenset()) == ("switch", 2)


def test_consume_first_tie_break_by_rotation_index():
    # #2 and #3 share reset time AND worst pct -> lower snapshot index (#2) wins.
    accts = [_cf(1, 99, 99, _R_LATE, active=True),
             _cf(2, 40, 40, _R_EARLY), _cf(3, 40, 40, _R_EARLY)]
    assert menubar.decide_consume_first(accts, 95, frozenset()) == ("switch", 2)


def test_consume_first_all_session_limited_is_silent():
    # everyone 5h-saturated but weekly has room -> temporary, silent stay.
    accts = [_cf(1, 99, 10, _R_EARLY, active=True), _cf(2, 98, 20, _R_LATE)]
    assert menubar.decide_consume_first(accts, 95, frozenset()) == ("all_session_limited", None)


def test_consume_first_exhausted_notifies():
    accts = [_cf(1, 99, 99, _R_EARLY, active=True), _cf(2, 98, 99, _R_LATE)]
    assert menubar.decide_consume_first(accts, 95, frozenset()) == ("no_candidate", None)


def test_consume_first_unverifiable_is_silent():
    accts = [_cf(1, 99, 99, _R_EARLY, active=True), (2, "b@x", False, None)]
    assert menubar.decide_consume_first(accts, 95, frozenset()) == ("no_candidate_unverifiable", None)


def test_consume_first_unknown_active():
    accts = [(1, "a@x", True, "no credentials"), _cf(2, 10, 20, _R_EARLY)]
    assert menubar.decide_consume_first(accts, 95, frozenset()) == ("unknown_active", None)


def test_limiting_pct_by_account_per_strategy():
    accts = [_ra(1, 80, 50, active=True), (2, "b@x", False, None)]
    assert menubar.limiting_pct_by_account(accts, "reactive") == {"1": 80.0, "2": None}
    assert menubar.limiting_pct_by_account(accts, "consume-first") == {"1": 80.0, "2": None}


def test_evaluate_strategy_dispatch():
    accts = [_cf(1, 10, 20, _R_LATE, active=True), _cf(2, 10, 20, _R_EARLY)]
    assert menubar.evaluate_strategy("consume-first", accts, 95, frozenset()) == ("switch", 2)
    # reactive: active not over limit -> none
    assert menubar.evaluate_strategy("reactive", accts, 95, frozenset()) == ("none", None)



# --- reset countdown in account-row usage summary -----------------------------

def test_usage_summary_includes_countdown():
    usage = {
        "five_hour": {"pct": 42.0, "countdown": "2h 33m", "clock": "14:50"},
        "seven_day": {"pct": 18.0, "countdown": "1d 19h"},
        "spend": {"pct": 30.0},
    }
    assert menubar.usage_summary(usage) == "5h 42% (2h 33m) · 7d 18% (1d 19h) · $ 30%"


def test_usage_summary_countdown_per_window_presence():
    # countdown shown only for the window that has it; spend never gets one
    usage = {"five_hour": {"pct": 5.0, "countdown": "1h"}, "seven_day": {"pct": 8.0}}
    assert menubar.usage_summary(usage) == "5h 5% (1h) · 7d 8%"
