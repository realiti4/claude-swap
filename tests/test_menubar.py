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
