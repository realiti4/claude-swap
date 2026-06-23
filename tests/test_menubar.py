"""Tests for the menu bar module.

These tests never import or run rumps/AppKit. They exercise the pure helpers
(settings store, title/label formatting, plist rendering) only.
"""

from __future__ import annotations

import json
import plistlib
from pathlib import Path

import pytest

from claude_swap import menubar


def test_settings_defaults_when_file_missing(tmp_path: Path):
    s = menubar.MenuBarSettings.load(tmp_path / "nope.json")
    assert s.show_account_name is True
    assert s.show_quota_pct is True
    assert s.refresh_interval == 60
    assert s.launch_at_login is False


def test_settings_round_trip(tmp_path: Path):
    path = tmp_path / "menubar_settings.json"
    original = menubar.MenuBarSettings(
        show_account_name=False,
        show_quota_pct=True,
        refresh_interval=300,
        launch_at_login=True,
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
            {"refresh_interval": "fast", "bogus": 1, "show_quota_pct": False}
        ),
        encoding="utf-8",
    )
    s = menubar.MenuBarSettings.load(path)
    # bad-typed refresh_interval falls back to default; valid bool is kept
    assert s.refresh_interval == 60
    assert s.show_quota_pct is False


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


def test_format_title_both_segments():
    s = menubar.MenuBarSettings(show_account_name=True, show_quota_pct=True)
    assert menubar.format_title("loc@papaya.asia", _USAGE, s) == "⇄ loc · 42%"


def test_format_title_name_only():
    s = menubar.MenuBarSettings(show_account_name=True, show_quota_pct=False)
    assert menubar.format_title("loc@papaya.asia", _USAGE, s) == "⇄ loc"


def test_format_title_pct_only():
    s = menubar.MenuBarSettings(show_account_name=False, show_quota_pct=True)
    assert menubar.format_title("loc@papaya.asia", _USAGE, s) == "⇄ 42%"


def test_format_title_icon_only_when_all_off():
    s = menubar.MenuBarSettings(show_account_name=False, show_quota_pct=False)
    assert menubar.format_title("loc@papaya.asia", _USAGE, s) == "⇄"


def test_format_title_icon_only_when_no_active_account():
    s = menubar.MenuBarSettings(show_account_name=True, show_quota_pct=True)
    assert menubar.format_title(None, None, s) == "⇄"


def test_format_title_truncates_long_local_part():
    s = menubar.MenuBarSettings(show_account_name=True, show_quota_pct=False)
    title = menubar.format_title("averylonglocalpart@example.com", None, s)
    assert title == "⇄ averylonglo*"  # 12 chars: 11 letters + asterisk marker


def test_format_title_drops_pct_when_unavailable():
    s = menubar.MenuBarSettings(show_account_name=False, show_quota_pct=True)
    assert menubar.format_title("loc@x.com", "no credentials", s) == "⇄"


def test_render_launch_agent_contains_args_and_label():
    data = menubar.render_launch_agent(["/usr/bin/cswap", "--menubar"])
    parsed = plistlib.loads(data)
    assert parsed["Label"] == menubar.LAUNCH_AGENT_LABEL
    assert parsed["ProgramArguments"] == ["/usr/bin/cswap", "--menubar"]
    assert parsed["RunAtLoad"] is True


def test_set_launch_at_login_writes_then_removes(tmp_path, monkeypatch):
    plist = tmp_path / "agent.plist"
    monkeypatch.setattr(menubar, "launch_agent_path", lambda: plist)

    menubar.set_launch_at_login(True, ["/usr/bin/cswap", "--menubar"])
    assert plist.exists()
    assert plistlib.loads(plist.read_bytes())["RunAtLoad"] is True

    menubar.set_launch_at_login(False, ["/usr/bin/cswap", "--menubar"])
    assert not plist.exists()
    # idempotent: removing again does not raise
    menubar.set_launch_at_login(False, ["/usr/bin/cswap", "--menubar"])


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


# --- session % in menu-bar title (show_session_pct) ---------------------------

_USAGE_SESSION = {"five_hour": {"pct": 38.0}, "seven_day": {"pct": 71.0}}
# tightest (max) = 71; session (5h) = 38


def test_settings_show_session_pct_default_false():
    assert menubar.MenuBarSettings().show_session_pct is False


def test_format_title_session_pct_only():
    s = menubar.MenuBarSettings(
        show_account_name=False, show_quota_pct=False, show_session_pct=True
    )
    assert menubar.format_title("loc@x.com", _USAGE_SESSION, s) == "⇄ 38%"


def test_format_title_session_with_name():
    s = menubar.MenuBarSettings(
        show_account_name=True, show_quota_pct=False, show_session_pct=True
    )
    assert menubar.format_title("loc@x.com", _USAGE_SESSION, s) == "⇄ loc · 38%"


def test_format_title_quota_then_session_when_both_on():
    s = menubar.MenuBarSettings(
        show_account_name=False, show_quota_pct=True, show_session_pct=True
    )
    assert menubar.format_title("loc@x.com", _USAGE_SESSION, s) == "⇄ 71% · 38%"


def test_format_title_session_dropped_when_unavailable():
    s = menubar.MenuBarSettings(
        show_account_name=False, show_quota_pct=False, show_session_pct=True
    )
    assert menubar.format_title("loc@x.com", "no credentials", s) == "⇄"
