"""Tests for the menu bar module.

These tests never import or run rumps/AppKit. They exercise the pure helpers
(settings store, title/label formatting, usage/snapshot adapters, log parsing)
only — the auto-switch engine itself lives in ``claude_swap.autoswitch`` and is
tested there.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import plistlib
import shlex
import shutil
import stat
import subprocess
import threading
import sys
from pathlib import Path

import pytest

from claude_swap import menubar
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.switcher import USAGE_API_KEY


# --- notification identity -----------------------------------------------------

def test_notification_identity_creates_and_preserves_info_plist(tmp_path: Path):
    executable = tmp_path / "bin" / "python3"
    executable.parent.mkdir()
    info = executable.parent / "Info.plist"
    info.write_bytes(plistlib.dumps({"ExistingKey": "kept"}))

    result = menubar.ensure_notification_identity(executable, platform="darwin")

    assert result == info
    data = plistlib.loads(info.read_bytes())
    assert data["CFBundleIdentifier"] == "com.claude-swap.menubar"
    assert data["CFBundleName"] == "claude-swap"
    assert data["ExistingKey"] == "kept"


def test_notification_identity_heals_corrupt_info_plist(tmp_path: Path):
    executable = tmp_path / "bin" / "python3"
    executable.parent.mkdir()
    info = executable.parent / "Info.plist"
    # truncated XML plist: plistlib raises ExpatError, not InvalidFileException
    info.write_bytes(
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b'<plist version="1.0"><dict><key>CFBundle'
    )

    result = menubar.ensure_notification_identity(executable, platform="darwin")

    assert result == info
    data = plistlib.loads(info.read_bytes())
    assert data["CFBundleIdentifier"] == "com.claude-swap.menubar"
    assert data["CFBundleName"] == "claude-swap"
    assert not (executable.parent / "Info.plist.tmp").exists()


def test_notification_identity_is_noop_off_macos(tmp_path: Path):
    executable = tmp_path / "bin" / "python3"
    assert menubar.ensure_notification_identity(
        executable, platform="linux"
    ) is None
    assert not (executable.parent / "Info.plist").exists()


# --- settings ------------------------------------------------------------------

def test_settings_defaults_when_file_missing(tmp_path: Path):
    s = menubar.MenuBarSettings.load(tmp_path / "nope.json")
    assert s.show_account_name is True
    assert s.title_pct == "both"
    assert s.refresh_interval == 60
    assert s.auto_switch_enabled is False


def test_settings_round_trip(tmp_path: Path):
    path = tmp_path / "menubar_settings.json"
    original = menubar.MenuBarSettings(
        show_account_name=False,
        title_pct="5h",
        refresh_interval=300,
        auto_switch_enabled=True,
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


# --- usage display helpers -----------------------------------------------------

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


def test_usage_summary_includes_scoped_model_limits():
    # Per-model weekly limits (e.g. Fable) come through as usage["scoped"], after
    # 5h/7d and before spend.
    usage = {
        "five_hour": {"pct": 82.0},
        "seven_day": {"pct": 12.0},
        "scoped": [{"name": "Fable", "pct": 4.0}],
        "spend": {"pct": 30.0},
    }
    assert menubar.usage_summary(usage) == "5h 82% · 7d 12% · Fable 4% · $ 30%"


def test_usage_summary_scoped_over_limit_marker():
    usage = {"scoped": [{"name": "Fable", "pct": 100.0}]}
    assert menubar.usage_summary(usage) == "Fable 100% (!)"


def test_usage_summary_scoped_multiple_and_countdown():
    usage = {
        "scoped": [
            {"name": "Fable", "pct": 4.0, "resets_at": _iso(2 * 3600)},
            {"name": "Opus", "pct": 55.0},
        ],
    }
    assert menubar.usage_summary(usage, _NOW) == "Fable 4% (2h 0m) · Opus 55%"


def test_usage_summary_string_sentinel_passthrough():
    assert menubar.usage_summary("no credentials") == "no credentials"


def test_usage_summary_none():
    assert menubar.usage_summary(None) == "usage unavailable"


def test_usage_summary_seven_day_ahead_of_pace_marker():
    # 1 day elapsed of the week, 50% used -> far ahead of the ~14% expected.
    usage = {"seven_day": {"pct": 50.0, "resets_at": _iso(6 * 86400)}}
    out = menubar.usage_summary(usage, _NOW, fetched_at=_NOW)
    assert out == "7d 50% (ahead) (6d 0h)"


def test_usage_summary_five_hour_never_shows_pace_marker():
    usage = {"five_hour": {"pct": 90.0, "resets_at": _iso(4 * 3600)}}
    out = menubar.usage_summary(usage, _NOW, fetched_at=_NOW)
    assert "ahead" not in out


def test_usage_summary_scoped_ahead_of_pace_marker():
    usage = {"scoped": [{"name": "Fable", "pct": 50.0, "resets_at": _iso(6 * 86400)}]}
    out = menubar.usage_summary(usage, _NOW, fetched_at=_NOW)
    assert out == "Fable 50% (ahead) (6d 0h)"


def test_usage_summary_maxed_scoped_marker_wins_over_pace():
    # At/over the limit shows "(!)" — the more urgent signal — not "(ahead)".
    usage = {"scoped": [{"name": "Fable", "pct": 100.0, "resets_at": _iso(6 * 86400)}]}
    out = menubar.usage_summary(usage, _NOW, fetched_at=_NOW)
    assert "(!)" in out
    assert "ahead" not in out


def test_usage_summary_no_pace_marker_without_fetched_at():
    usage = {"seven_day": {"pct": 50.0, "resets_at": _iso(6 * 86400)}}
    out = menubar.usage_summary(usage, _NOW)
    assert "ahead" not in out


def test_usage_summary_no_pace_marker_on_window_rolled_to_zero():
    # A weekly window whose resets_at has already passed (stale cache, not
    # refetched since the actual reset) is rolled to a display pct of 0% —
    # pace must be computed against that rolled 0%, not the raw stale pct,
    # or the display would show "7d 0% (ahead)" (a marker paired with a
    # percentage it doesn't correspond to).
    usage = {"seven_day": {"pct": 95.0, "resets_at": _iso(-3 * 86400)}}
    out = menubar.usage_summary(usage, _NOW, fetched_at=_NOW - 4 * 86400)
    assert "ahead" not in out
    assert "7d 0%" in out


def test_usage_summary_scoped_no_pace_marker_on_window_rolled_to_zero():
    usage = {"scoped": [{"name": "Fable", "pct": 95.0, "resets_at": _iso(-3 * 86400)}]}
    out = menubar.usage_summary(usage, _NOW, fetched_at=_NOW - 4 * 86400)
    assert "ahead" not in out
    assert "Fable 0%" in out


def test_format_account_label():
    label = menubar.format_account_label(2, "loc@papaya.asia", _USAGE)
    assert label == "2  loc@papaya.asia  5h 42% · 7d 18% · $ 30%"


def test_format_account_label_with_alias():
    label = menubar.format_account_label(2, "loc@papaya.asia", _USAGE, alias="dev")
    assert label == "2  dev  (loc@papaya.asia)  5h 42% · 7d 18% · $ 30%"


def test_format_account_label_disabled_marker():
    label = menubar.format_account_label(2, "loc@papaya.asia", _USAGE, disabled=True)
    assert label == "2  loc@papaya.asia  (disabled)  5h 42% · 7d 18% · $ 30%"


# --- usage logging -------------------------------------------------------------

def test_format_usage_log_full():
    usage = {
        "five_hour": {"pct": 35.0, "clock": "06:59"},
        "seven_day": {"pct": 55.0, "clock": "Jun 29 21:59"},
    }
    assert menubar.format_usage_log("a@x.com", usage) == (
        "usage a@x.com: 5h 35% (resets 06:59) · 7d 55% (resets Jun 29 21:59)"
    )


def test_format_usage_log_without_clock():
    usage = {"five_hour": {"pct": 0.0}, "seven_day": {"pct": 12.0}}
    assert menubar.format_usage_log("a@x.com", usage) == "usage a@x.com: 5h 0% · 7d 12%"


def test_format_usage_log_partial_window():
    usage = {"seven_day": {"pct": 12.0, "clock": "Jul 3"}}
    assert menubar.format_usage_log("a@x.com", usage) == "usage a@x.com: 7d 12% (resets Jul 3)"


def test_format_usage_log_none_when_no_numeric_window():
    assert menubar.format_usage_log("a@x.com", None) is None
    assert menubar.format_usage_log("a@x.com", "rate limited") is None
    assert menubar.format_usage_log("a@x.com", {"spend": {"pct": 5.0}}) is None


def test_usage_log_key_ignores_clock_tracks_pct():
    u1 = {"five_hour": {"pct": 35.0, "clock": "06:59"}, "seven_day": {"pct": 55.0}}
    u2 = {"five_hour": {"pct": 35.0, "clock": "07:59"}, "seven_day": {"pct": 55.0}}
    u3 = {"five_hour": {"pct": 36.0}, "seven_day": {"pct": 55.0}}
    assert menubar._usage_log_key(u1) == menubar._usage_log_key(u2)  # clock-only change
    assert menubar._usage_log_key(u1) != menubar._usage_log_key(u3)  # pct change
    assert menubar._usage_log_key(None) == (None, None)


# --- title ---------------------------------------------------------------------

def test_format_title_name_and_5h():
    s = menubar.MenuBarSettings(show_account_name=True, title_pct="5h")
    assert menubar.format_title("loc@papaya.asia", _USAGE, s) == "⇄ loc · 42%"


def test_format_title_prefers_alias_over_local_part():
    s = menubar.MenuBarSettings(show_account_name=True, title_pct="off")
    assert menubar.format_title("loc@papaya.asia", _USAGE, s, alias="dev") == "⇄ dev"


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


def test_format_title_scoped_appends_model_limits():
    # title_pct="off" + title_scoped gives a title tracking only the scoped model
    s = menubar.MenuBarSettings(show_account_name=True, title_pct="off", title_scoped=True)
    usage = {**_USAGE, "scoped": [{"name": "Fable", "pct": 55.0}]}
    assert menubar.format_title("loc@papaya.asia", usage, s) == "⇄ loc · Fable 55%"


def test_format_title_scoped_after_windows_multiple_models():
    s = menubar.MenuBarSettings(show_account_name=False, title_pct="both", title_scoped=True)
    usage = {
        **_USAGE,
        "scoped": [{"name": "Fable", "pct": 55.0}, {"name": "Opus", "pct": 7.0}],
    }
    assert menubar.format_title("loc@papaya.asia", usage, s) == "⇄ 42% · 18% · Fable 55% · Opus 7%"


def test_format_title_scoped_off_by_default():
    # default settings ignore scoped windows entirely
    s = menubar.MenuBarSettings(show_account_name=False, title_pct="off")
    usage = {**_USAGE, "scoped": [{"name": "Fable", "pct": 55.0}]}
    assert not s.title_scoped
    assert menubar.format_title("loc@papaya.asia", usage, s) == "⇄"


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


# --- reset-time helpers --------------------------------------------------------

def test_resets_at_ts_orders_and_handles_missing():
    early = {"resets_at": "2026-06-24T07:00:00+00:00"}
    late = {"resets_at": "2026-06-26T07:00:00+00:00"}
    assert menubar._resets_at_ts(early) < menubar._resets_at_ts(late)
    assert menubar._resets_at_ts({"pct": 5.0}) == float("inf")   # no resets_at
    assert menubar._resets_at_ts({"resets_at": "garbage"}) == float("inf")
    assert menubar._resets_at_ts(None) == float("inf")


_NOW = 1_000_000.0


def _iso(delta_s):  # ISO-8601 for _NOW + delta_s, UTC
    return _dt.datetime.fromtimestamp(_NOW + delta_s, _dt.timezone.utc).isoformat()


def test_live_countdown_formats_from_resets_at():
    assert menubar._live_countdown({"resets_at": _iso(9 * 3600 + 5 * 60)}, _NOW) == "9h 5m"
    assert menubar._live_countdown({"resets_at": _iso(86400 + 19 * 3600)}, _NOW) == "1d 19h"
    assert menubar._live_countdown({"resets_at": _iso(34 * 60)}, _NOW) == "34m"


def test_live_countdown_none_when_passed_or_missing():
    assert menubar._live_countdown({"resets_at": _iso(-60)}, _NOW) is None   # already reset
    assert menubar._live_countdown({"pct": 5.0}, _NOW) is None               # no resets_at
    assert menubar._live_countdown("no credentials", _NOW) is None


def test_usage_summary_live_countdown_from_resets_at():
    usage = {
        "five_hour": {"pct": 42.0, "resets_at": _iso(2 * 3600 + 33 * 60)},
        "seven_day": {"pct": 18.0, "resets_at": _iso(86400 + 19 * 3600)},
        "spend": {"pct": 30.0},
    }
    assert menubar.usage_summary(usage, _NOW) == "5h 42% (2h 33m) · 7d 18% (1d 19h) · $ 30%"


def test_usage_summary_omits_countdown_when_passed_or_missing():
    # 5h reset already passed (stale data) -> omit; 7d has no resets_at -> omit
    usage = {"five_hour": {"pct": 53.0, "resets_at": _iso(-60)}, "seven_day": {"pct": 8.0}}
    assert menubar.usage_summary(usage, _NOW) == "5h 53% · 7d 8%"


# --- switch-history log parsing ------------------------------------------------

_SWITCH_LOG = (
    "2026-06-27 00:57:50,178 - INFO - Switched from account 1 to 3\n"
    "2026-06-27 02:06:21,302 - INFO - usage a@x.com: 5h 10%\n"
    "2026-06-27 02:10:00,000 - INFO - Switched from account 3 to 1\n"
)


def test_parse_switch_history_most_recent_first():
    assert menubar.parse_switch_history(_SWITCH_LOG) == [
        "3 → 1   2026-06-27 02:10",
        "1 → 3   2026-06-27 00:57",
    ]


def test_parse_switch_history_respects_limit():
    lines = "\n".join(
        f"2026-06-27 0{i}:00:00,000 - INFO - Switched from account 1 to 2"
        for i in range(1, 6)
    )
    out = menubar.parse_switch_history(lines, limit=2)
    assert len(out) == 2
    assert out[0] == "1 → 2   2026-06-27 05:00"  # newest first


def test_parse_switch_history_empty_or_no_matches():
    assert menubar.parse_switch_history("") == []
    assert menubar.parse_switch_history("nothing relevant here") == []


# --- snapshot adapter (fakes for AccountsSnapshot / UsageEntry) -----------------

class _FakeEntry:
    def __init__(self, sentinel=None, last_good=None, fetched_at=None):
        self.sentinel = sentinel
        self.last_good = last_good
        self.fetched_at = fetched_at


class _FakeAcct:
    def __init__(self, number, email, is_active, usage, alias="", disabled=False):
        self.number = number
        self.email = email
        self.is_active = is_active
        self.usage = usage
        self.alias = alias
        self.disabled = disabled


class _FakeSnap:
    def __init__(self, accounts):
        self.accounts = accounts


def test_account_display_usage_sentinel_note_last_good_or_none():
    assert menubar._account_display_usage(
        _FakeEntry(sentinel=USAGE_API_KEY)
    ) == menubar.SENTINEL_NOTES[USAGE_API_KEY]
    lg = {"five_hour": {"pct": 5.0}}
    assert menubar._account_display_usage(_FakeEntry(last_good=lg)) == lg
    assert menubar._account_display_usage(_FakeEntry()) is None


def test_adapt_snapshot_shape_and_active_selection():
    # _adapt_snapshot is a pure transform of an AccountsSnapshot (the fetch
    # pacing now lives in SnapshotSource, tested separately).
    lg = {"five_hour": {"pct": 10.0}, "seven_day": {"pct": 20.0}}
    accts = [
        _FakeAcct("1", "a@x.com", True, _FakeEntry(last_good=lg, fetched_at=123.0)),
        _FakeAcct("2", "b@x.com", False, _FakeEntry(sentinel=USAGE_API_KEY), disabled=True),
    ]
    snap = menubar._adapt_snapshot(_FakeSnap(accts))
    assert snap["active_email"] == "a@x.com"
    assert snap["active_usage"] == lg
    assert snap["active_alias"] == ""
    # (num, email, is_active, display_usage, last_good, alias, disabled, fetched_at)
    assert snap["accounts"][0] == ("1", "a@x.com", True, lg, lg, "", False, 123.0)
    # sentinel account: display is the human note, last_good/fetched_at are None; disabled carried through
    assert snap["accounts"][1] == (
        "2", "b@x.com", False, menubar.SENTINEL_NOTES[USAGE_API_KEY], None, "", True, None,
    )


def test_adapt_snapshot_empty():
    assert menubar._adapt_snapshot(_FakeSnap([])) == menubar.EMPTY_SNAPSHOT


# --- weekly reset roll-forward (static 7-day cadence) --------------------------

def test_rolled_weekly_window_advances_passed_reset():
    w = {"pct": 95.0, "resets_at": _iso(-3 * 86400), "countdown": "stale", "clock": "old"}
    rolled = menubar._rolled_weekly_window(w, _NOW)
    assert rolled["pct"] == 0.0  # the window objectively rolled over
    assert abs(menubar._resets_at_ts(rolled) - (_NOW + 4 * 86400)) < 1
    assert "countdown" not in rolled and "clock" not in rolled  # stale strings dropped


def test_rolled_weekly_window_advances_multiple_missed_weeks():
    w = {"pct": 80.0, "resets_at": _iso(-10 * 86400)}  # two boundaries crossed
    rolled = menubar._rolled_weekly_window(w, _NOW)
    assert abs(menubar._resets_at_ts(rolled) - (_NOW + 4 * 86400)) < 1


def test_rolled_weekly_window_leaves_future_or_unknown_untouched():
    future = {"pct": 42.0, "resets_at": _iso(2 * 86400)}
    assert menubar._rolled_weekly_window(future, _NOW) is future
    no_reset = {"pct": 42.0}
    assert menubar._rolled_weekly_window(no_reset, _NOW) is no_reset
    assert menubar._rolled_weekly_window(None, _NOW) is None


def test_usage_summary_reflects_passed_weekly_reset():
    # 7d reset a day ago: show it as reset (0%) with the next weekly boundary,
    # from the static schedule alone. 5h is untouched (dynamic session window).
    usage = {
        "five_hour": {"pct": 10.0},
        "seven_day": {"pct": 95.0, "resets_at": _iso(-86400)},
    }
    assert menubar.usage_summary(usage, _NOW) == "5h 10% · 7d 0% (6d 0h)"


def test_usage_summary_scoped_reflects_passed_weekly_reset():
    usage = {"scoped": [{"name": "Fable", "pct": 100.0, "resets_at": _iso(-86400)}]}
    # rolled to 0% → the over-limit "(!)" marker is gone too
    assert menubar.usage_summary(usage, _NOW) == "Fable 0% (6d 0h)"


def test_format_title_reflects_passed_weekly_reset():
    s = menubar.MenuBarSettings(show_account_name=False, title_pct="7d")
    usage = {"seven_day": {"pct": 95.0, "resets_at": _iso(-86400)}}
    assert menubar.format_title("a@x.com", usage, s, _NOW) == "⇄ 0%"


# --- run() app glue ------------------------------------------------------------

def test_run_without_rumps_raises_clean_error(monkeypatch):
    """A missing menubar extra surfaces as ClaudeSwitchError, not a traceback.

    The module is import-safe without rumps, so the CLI's ImportError guard
    around ``from claude_swap.menubar import run`` can never fire — the import
    failure happens inside ``run()``. Blocking the import (a ``None`` entry in
    ``sys.modules`` makes ``import rumps`` raise) checks that ``run()`` turns
    it into the error type the CLI renders with the install hint.
    """
    monkeypatch.setitem(sys.modules, "rumps", None)
    with pytest.raises(ClaudeSwitchError, match=r"claude-swap\[menubar\]"):
        menubar.run(switcher=None)


class TestFrameworkBuildWarning:
    """The menu bar draws nothing from a framework build on macOS 26.

    See #310. The interpreter *version* is not the variable — measured with the
    same bare rumps app, Homebrew 3.14.6 and 3.10.21 (both framework) draw
    nothing while uv-managed 3.14.7 and 3.13.15 (neither framework) draw.
    Nothing here can fix that; these tests are about the warning, not the icon.
    """

    def test_silent_on_a_build_that_draws(self):
        assert menubar.framework_build_warning("", "uv", "26.6.2") is None

    def test_silent_on_macos_where_framework_builds_still_draw(self):
        # The evidence is a framework build *on macOS 26*. Warning on 14 or 15
        # would nag every Homebrew user on every launch, and in the service log
        # on every restart, about something that works there.
        assert menubar.framework_build_warning("Python", "uv", "15.7") is None
        assert menubar.framework_build_warning("Python", "uv", "14.2") is None

    def test_warns_from_macos_26_onwards(self):
        assert menubar.framework_build_warning("Python", "uv", "26.0") is not None
        assert menubar.framework_build_warning("Python", "uv", "27.1") is not None

    def test_unreadable_macos_version_stays_quiet(self):
        assert menubar.framework_build_warning("Python", "uv", "") is None

    def test_the_wording_does_not_overclaim(self):
        # An observation on one machine that has also been seen to behave
        # otherwise; "observed" is what the evidence supports.
        msg = menubar.framework_build_warning("Python", "uv", "26.6.2")
        assert "observed" in msg

    def test_warns_on_a_framework_build(self):
        assert menubar.framework_build_warning("Python", "uv", "26.6.2") is not None

    def test_the_version_is_not_the_gate(self):
        # A framework build warns whatever the version; a non-framework one
        # never does. Keying on version_info was the original mistake here.
        assert menubar.framework_build_warning("Python", None, "26.6.2") is not None
        assert menubar.framework_build_warning("", None, "26.6.2") is None

    def test_uv_gets_a_uv_remedy(self):
        msg = menubar.framework_build_warning("Python", "uv", "26.6.2")
        assert "--managed-python" in msg

    def test_pipx_is_not_handed_a_uv_command(self):
        # `uv tool install --force` would overwrite pipx's own executable.
        msg = menubar.framework_build_warning("Python", "pipx", "26.6.2")
        assert "uv tool install" not in msg
        assert "pipx install" in msg

    def test_unknown_install_method_still_says_what_to_aim_for(self):
        msg = menubar.framework_build_warning("Python", None, "26.6.2")
        assert "non-framework" in msg

    def test_the_warning_names_the_silence(self):
        # The symptom is that everything looks healthy, so say so.
        msg = menubar.framework_build_warning("Python", "uv", "26.6.2")
        assert "logs nothing" in msg


# --- open dashboard -------------------------------------------------------------

def _no_which(_name):
    return None


def test_dashboard_executable_uses_launching_cswap(tmp_path: Path):
    exe = tmp_path / "venv" / "bin" / "cswap"
    cmd = menubar.dashboard_executable(
        argv0=str(exe), which=lambda _n: "/elsewhere/cswap", python="/py"
    )
    assert cmd == [str(exe), "watch"]


def test_dashboard_executable_accepts_claude_swap_name(tmp_path: Path):
    exe = tmp_path / "bin" / "claude-swap"
    cmd = menubar.dashboard_executable(argv0=str(exe), which=_no_which, python="/py")
    assert cmd == [str(exe), "watch"]


def test_dashboard_executable_makes_relative_argv0_absolute(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cmd = menubar.dashboard_executable(argv0="bin/cswap", which=_no_which, python="/py")
    assert cmd == [str(tmp_path / "bin" / "cswap"), "watch"]


def test_dashboard_executable_module_launch_beats_competing_path_install(tmp_path: Path):
    # `/venv-A/bin/python -m claude_swap menubar` with venv B's cswap on PATH:
    # the dashboard must stay on install A, so PATH is never consulted.
    main_py = tmp_path / "venv-A" / "lib" / "site-packages" / "claude_swap" / "__main__.py"
    seen = []

    def which(name):
        seen.append(name)
        return "/venv-B/bin/cswap"

    cmd = menubar.dashboard_executable(
        argv0=str(main_py), which=which, python="/venv-A/bin/python"
    )
    assert cmd == ["/venv-A/bin/python", "-m", "claude_swap", "watch"]
    assert seen == []


def test_dashboard_executable_other_packages_main_uses_path():
    # Only this package's __main__.py is a module launch of cswap; another
    # tool's __main__.py (e.g. a test runner) falls through to PATH.
    seen = []

    def which(name):
        seen.append(name)
        return "/opt/bin/cswap" if name == "cswap" else None

    cmd = menubar.dashboard_executable(
        argv0="/usr/lib/python3/site-packages/pytest/__main__.py", which=which, python="/py"
    )
    assert cmd == [os.path.abspath("/opt/bin/cswap"), "watch"]
    assert seen == ["cswap"]


def test_dashboard_executable_path_lookup_is_made_absolute(tmp_path: Path, monkeypatch):
    # PATH=.venv/bin:... makes which() return a relative path, which breaks
    # once Terminal starts the command in $HOME.
    monkeypatch.chdir(tmp_path)
    cmd = menubar.dashboard_executable(
        argv0="/x/python", which=lambda n: ".venv/bin/cswap" if n == "cswap" else None, python="/py"
    )
    assert cmd == [str(tmp_path / ".venv" / "bin" / "cswap"), "watch"]


def test_dashboard_executable_bare_argv0_lookup_is_made_absolute(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cmd = menubar.dashboard_executable(
        argv0="cswap", which=lambda n: "bin/cswap" if n == "cswap" else None, python="/py"
    )
    assert cmd == [str(tmp_path / "bin" / "cswap"), "watch"]


def test_dashboard_executable_tries_claude_swap_after_cswap():
    def which(name):
        return "/opt/bin/claude-swap" if name == "claude-swap" else None

    cmd = menubar.dashboard_executable(argv0="/x/python", which=which, python="/py")
    assert cmd == [os.path.abspath("/opt/bin/claude-swap"), "watch"]


def test_dashboard_executable_falls_back_to_python_module():
    cmd = menubar.dashboard_executable(argv0="/x/python", which=_no_which, python="/v/bin/python")
    assert cmd == ["/v/bin/python", "-m", "claude_swap", "watch"]


def test_dashboard_executable_defaults_to_sys_argv0(monkeypatch, tmp_path: Path):
    exe = tmp_path / "cswap"
    monkeypatch.setattr(sys, "argv", [str(exe)])
    assert menubar.dashboard_executable(which=_no_which, python="/py") == [str(exe), "watch"]


class _Proc:
    def __init__(self, returncode, stderr=""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = ""


_ENV_NONE = {"CLAUDE_CONFIG_DIR": None, "CLAUDE_SECURESTORAGE_CONFIG_DIR": None,
             "XDG_DATA_HOME": None, "PYTHONPATH": None}


def _open(script_dir: Path, run, create=None, cwd="/w", env=None):
    kwargs = {} if create is None else {"create": create}
    return menubar.open_dashboard_terminal(
        ["/bin/cswap", "watch"], script_dir, cwd=cwd,
        env=_ENV_NONE if env is None else env, run=run, **kwargs,
    )


def test_open_dashboard_terminal_writes_script_then_opens_it_in_terminal(tmp_path: Path):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return _Proc(0)

    ok, _msg = _open(tmp_path, run)
    assert ok is True
    argv, kwargs = calls[0]
    # By bundle id, so a renamed Terminal.app still resolves.
    assert argv[:3] == ["open", "-b", "com.apple.Terminal"]
    script = Path(argv[3])
    assert script.parent == tmp_path
    assert script.name.startswith("open-dashboard.") and script.name.endswith(".command")
    assert script.read_text() == menubar.dashboard_script(["/bin/cswap", "watch"], env=_ENV_NONE, cwd="/w")
    assert kwargs.get("check") is False
    assert kwargs.get("timeout") == 30


def test_open_dashboard_terminal_uses_a_fresh_file_per_launch(tmp_path: Path):
    # Two menu bars (different profiles, same backup dir) must never share a
    # script: B's write could otherwise replace A's before Terminal reads it.
    opened = []
    run = lambda argv, **kw: opened.append(Path(argv[3])) or _Proc(0)  # noqa: E731
    env_a = dict(_ENV_NONE, CLAUDE_CONFIG_DIR="/profile/a")
    env_b = dict(_ENV_NONE, CLAUDE_CONFIG_DIR="/profile/b")
    assert _open(tmp_path, run, cwd="/cwd/a", env=env_a)[0]
    assert _open(tmp_path, run, cwd="/cwd/b", env=env_b)[0]
    a, b = opened
    assert a != b
    assert a.read_text() == menubar.dashboard_script(["/bin/cswap", "watch"], env=env_a, cwd="/cwd/a")
    assert b.read_text() == menubar.dashboard_script(["/bin/cswap", "watch"], env=env_b, cwd="/cwd/b")


def _no_files(directory: Path) -> bool:
    return list(directory.iterdir()) == []


def test_open_dashboard_terminal_missing_terminal_reports_open_error(tmp_path: Path):
    def run(argv, **kwargs):
        return _Proc(1, stderr="Unable to find application with bundle identifier com.apple.Terminal\n")

    ok, msg = _open(tmp_path, run)
    assert ok is False
    assert "Unable to find application" in msg
    assert _no_files(tmp_path)  # nobody will ever run that script


def test_open_dashboard_terminal_nonzero_exit_without_stderr(tmp_path: Path):
    ok, msg = _open(tmp_path, lambda argv, **kw: _Proc(1))
    assert ok is False
    assert msg
    assert _no_files(tmp_path)


def test_open_dashboard_terminal_missing_open_command(tmp_path: Path):
    def run(argv, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "open")

    ok, msg = _open(tmp_path, run)
    assert ok is False
    assert "open" in msg
    assert _no_files(tmp_path)


def test_open_dashboard_terminal_timeout(tmp_path: Path):
    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout") or 0)

    ok, msg = _open(tmp_path, run)
    assert ok is False
    assert msg
    assert _no_files(tmp_path)


def test_open_dashboard_terminal_create_failure_never_runs_open(tmp_path: Path):
    ran = []

    def create(_directory, _text):
        raise PermissionError(13, "Permission denied")

    ok, msg = _open(tmp_path, lambda argv, **kw: ran.append(argv) or _Proc(0), create=create)
    assert ok is False
    assert "Permission denied" in msg
    assert ran == []


def test_open_dashboard_terminal_missing_backup_dir_fails_cleanly(tmp_path: Path):
    ran = []
    ok, msg = _open(tmp_path / "gone", lambda argv, **kw: ran.append(argv) or _Proc(0))
    assert ok is False
    assert "launcher script" in msg
    assert ran == []


def test_dashboard_executable_resolves_bare_argv0_on_path():
    # A bare name has no directory to anchor it; abspath would invent one
    # under the cwd, so it must be looked up on PATH instead.
    cmd = menubar.dashboard_executable(
        argv0="cswap", which=lambda n: "/opt/bin/cswap" if n == "cswap" else None, python="/py"
    )
    assert cmd == [os.path.abspath("/opt/bin/cswap"), "watch"]


# --- dashboard environment ------------------------------------------------------

_KEYS = ("CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR", "XDG_DATA_HOME", "PYTHONPATH", "HOME")


def test_dashboard_env_keys_are_exactly_the_profile_and_import_keys():
    assert menubar.DASHBOARD_ENV_KEYS == _KEYS


def test_dashboard_env_marks_every_key_set_or_unset():
    environ = {
        "CLAUDE_CONFIG_DIR": "/profiles/work",
        "PATH": "/usr/bin",
        "HOME": "/Users/x",
        "TERM": "xterm-256color",
    }
    assert menubar.dashboard_env(environ) == {
        "CLAUDE_CONFIG_DIR": "/profiles/work",
        "CLAUDE_SECURESTORAGE_CONFIG_DIR": None,
        "XDG_DATA_HOME": None,
        "PYTHONPATH": None,
        "HOME": "/Users/x",
    }


def test_dashboard_env_forwards_home():
    # The account store (~/.claude-swap-backup) and the default profile both
    # resolve through HOME, so the dashboard must use the menu bar's.
    assert menubar.dashboard_env({"HOME": "/Users/menu"})["HOME"] == "/Users/menu"
    assert menubar.dashboard_env({})["HOME"] is None


def test_dashboard_env_keeps_empty_value_as_set():
    # An empty CLAUDE_SECURESTORAGE_CONFIG_DIR selects the default keychain
    # service, which differs from it being unset.
    env = menubar.dashboard_env({"CLAUDE_SECURESTORAGE_CONFIG_DIR": ""})
    assert env["CLAUDE_SECURESTORAGE_CONFIG_DIR"] == ""


def test_dashboard_env_forwards_pythonpath():
    env = menubar.dashboard_env({"PYTHONPATH": "src"})
    assert env["PYTHONPATH"] == "src"


def test_dashboard_env_defaults_to_process_environment(monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/from/process")
    for key in _KEYS[1:]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", "/from/process/home")
    assert menubar.dashboard_env() == {
        "CLAUDE_CONFIG_DIR": "/from/process",
        "CLAUDE_SECURESTORAGE_CONFIG_DIR": None,
        "XDG_DATA_HOME": None,
        "PYTHONPATH": None,
        "HOME": "/from/process/home",
    }


def _exec_argv(script: str) -> list[str]:
    # The command line between "then" and the status capture; quoted values
    # may themselves contain newlines, so this is not a line-based split.
    body = script.split("; then\n    ", 1)[1].split("\n    dashboard_status=$?", 1)[0]
    return shlex.split(body)


def test_dashboard_cwd_returns_current_directory():
    assert menubar.dashboard_cwd(getcwd=lambda: "/work dir") == "/work dir"


def test_dashboard_cwd_failure_returns_none():
    def gone():
        raise FileNotFoundError(2, "No such file or directory")

    assert menubar.dashboard_cwd(getcwd=gone) is None


def test_dashboard_script_exact_text_for_awkward_cwd():
    cwd = "/Users/o'brien/My \"50%\" dir\nété"
    script = menubar.dashboard_script(["/bin/cswap", "watch"], env=_ENV_NONE, cwd=cwd)
    quoted_cwd = "'/Users/o'\"'\"'brien/My \"50%\" dir\nété'"
    assert script == (
        "#!/bin/sh\n"
        'rm -f -- "$0"\n'
        f"if cd {quoted_cwd}; then\n"
        "    /usr/bin/env -u CLAUDE_CONFIG_DIR -u CLAUDE_SECURESTORAGE_CONFIG_DIR"
        " -u XDG_DATA_HOME -u PYTHONPATH /bin/cswap watch\n"
        "    dashboard_status=$?\n"
        '    if [ "$dashboard_status" -ne 0 ]; then\n'
        "        printf '%s\\n' \"cswap: the dashboard exited with status $dashboard_status."
        " Run: cswap watch\" >&2\n"
        "        printf '%s\\n' 'Press Return to close this window.' >&2\n"
        "        read -r _ || :\n"
        "    fi\n"
        "else\n"
        "    printf '%s\\n' 'cswap: cannot open the dashboard from "
        "/Users/o'\"'\"'brien/My \"50%\" dir\nété "
        "(Terminal may not have access to it). Run: cswap watch' >&2\n"
        "    printf '%s\\n' 'Press Return to close this window.' >&2\n"
        "    read -r _ || :\n"
        "fi\n"
    )


def test_dashboard_script_has_no_exit_or_exec():
    # Nothing may end the window early, and the script must outlive the
    # dashboard so it can report a failed start.
    words = menubar.dashboard_script(["/bin/cswap", "watch"], env=_ENV_NONE, cwd="/w").split()
    assert "exit" not in words
    assert "exec" not in words


def test_dashboard_script_unsets_missing_keys_and_sets_present_ones():
    env = {"CLAUDE_CONFIG_DIR": "/p", "CLAUDE_SECURESTORAGE_CONFIG_DIR": None,
           "XDG_DATA_HOME": "", "PYTHONPATH": None}
    argv = _exec_argv(menubar.dashboard_script(["/bin/cswap", "watch"], env=env, cwd="/w"))
    assert argv == [
        "/usr/bin/env",
        "-u", "CLAUDE_SECURESTORAGE_CONFIG_DIR",
        "-u", "PYTHONPATH",
        "CLAUDE_CONFIG_DIR=/p",
        "XDG_DATA_HOME=",
        "/bin/cswap", "watch",
    ]


def test_dashboard_script_routes_exe_with_equals_sign_through_sh():
    # env takes its first NAME=value operand as an assignment, so a program
    # path containing "=" must not be env's utility operand.
    argv = _exec_argv(menubar.dashboard_script(["/opt/a=b/cswap", "watch"], env=_ENV_NONE, cwd="/w"))
    assert argv[-5:] == ["/bin/sh", "-c", 'exec "$0" "$@"', "/opt/a=b/cswap", "watch"]


@pytest.mark.parametrize(
    "value, path",
    [
        ('/p "q" \\x', '/Users/a b/"q"/cswap'),
        ("/it's 100%", "/Users/o'brien/cswap"),
        ("/line1\nline2", "/tmp/new\nline/cswap"),
        ("/Profils/été 日本", "/Users/üñî/\U0001f600/cswap"),
        ("", "/bin/cswap"),
    ],
)
def test_dashboard_script_quotes_values_and_paths(value, path):
    env = dict(_ENV_NONE, CLAUDE_CONFIG_DIR=value)
    argv = _exec_argv(menubar.dashboard_script([path, "watch"], env=env, cwd="/w"))
    assert argv == [
        "/usr/bin/env",
        "-u", "CLAUDE_SECURESTORAGE_CONFIG_DIR", "-u", "XDG_DATA_HOME", "-u", "PYTHONPATH",
        f"CLAUDE_CONFIG_DIR={value}", path, "watch",
    ]


def test_create_dashboard_script_is_owner_only_executable(tmp_path: Path):
    path = menubar.create_dashboard_script(tmp_path, "#!/bin/sh\necho hi\n")
    assert path.parent == tmp_path
    assert path.name.startswith("open-dashboard.") and path.name.endswith(".command")
    assert path.read_text() == "#!/bin/sh\necho hi\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o700


def test_create_dashboard_script_never_reuses_a_name(tmp_path: Path):
    paths = {menubar.create_dashboard_script(tmp_path, f"{i}\n") for i in range(20)}
    assert len(paths) == 20
    assert all(p.read_text() == f"{i}\n" for i, p in enumerate(sorted(paths, key=lambda p: int(p.read_text()))))


def test_create_dashboard_script_write_failure_leaves_no_file(tmp_path: Path, monkeypatch):
    real_fdopen = os.fdopen

    class _Broken:
        def __init__(self, f):
            self._f = f

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._f.close()
            return False

        def write(self, _text):
            raise OSError(5, "Input/output error")

    monkeypatch.setattr(menubar.os, "fdopen", lambda fd, *a, **k: _Broken(real_fdopen(fd, *a, **k)))
    with pytest.raises(OSError):
        menubar.create_dashboard_script(tmp_path, "x\n")
    assert _no_files(tmp_path)


def test_create_dashboard_script_chmod_failure_leaves_no_file(tmp_path: Path, monkeypatch):
    def broken_chmod(*_a, **_k):
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(menubar.os, "chmod", broken_chmod)
    with pytest.raises(OSError):
        menubar.create_dashboard_script(tmp_path, "x\n")
    assert _no_files(tmp_path)


def test_create_dashboard_script_missing_directory_raises(tmp_path: Path):
    with pytest.raises(OSError):
        menubar.create_dashboard_script(tmp_path / "gone", "x\n")


def _fake_exe(directory: Path, exit_code: int = 0) -> Path:
    directory.mkdir(parents=True)
    exe = directory / "cswap"
    exe.write_text(
        "#!/bin/sh\n"
        'printf "cwd=%s\\n" "$(pwd -P)"\n'
        'printf "args=%s\\n" "$*"\n'
        "env\n"
        f"exit {exit_code}\n"
    )
    exe.chmod(0o755)
    return exe


def _shell(shell: str) -> str:
    if sys.platform == "win32":
        pytest.skip("POSIX shells and /usr/bin/env")
    path = shutil.which(shell)
    if path is None:
        pytest.skip(f"{shell} not installed")
    return path


# The environment Terminal's shell would have: its own HOME, and rc files that
# export a different profile and a secure-storage override the menu bar never
# had.
def _rc_environment(home: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": "/rc/profile",
        "CLAUDE_SECURESTORAGE_CONFIG_DIR": "/rc/secure",
        "PYTHONPATH": "/rc/pythonpath",
    }


_MENU_HOME = "/Users/menu bar's home"
_MENU_ENV = {"CLAUDE_CONFIG_DIR": "profiles/work", "CLAUDE_SECURESTORAGE_CONFIG_DIR": None,
             "XDG_DATA_HOME": "", "PYTHONPATH": "src", "HOME": _MENU_HOME}
_AWKWARD_CWD = "menu bar's \"50%\" cwd é"


def _create_script(tmp_path: Path, exe: Path, cwd: Path, env=None) -> Path:
    backup = tmp_path / "backup dir"
    backup.mkdir(exist_ok=True)
    return menubar.create_dashboard_script(
        backup,
        menubar.dashboard_script([str(exe), "watch"], env=_MENU_ENV if env is None else env, cwd=str(cwd)),
    )


def _run_script(shell_path, script, tmp_path, stdin=subprocess.DEVNULL):
    return subprocess.run(
        [shell_path, str(script)], env=_rc_environment(tmp_path), cwd=str(tmp_path),
        stdin=stdin, capture_output=True, text=True, check=False,
    )


@pytest.mark.parametrize("shell", ["sh", "bash", "zsh", "dash"])
@pytest.mark.parametrize("exe_dir", ["bin dir", "a=b dir"])
def test_dashboard_script_file_runs_with_menu_bar_profile_and_cwd(tmp_path: Path, shell, exe_dir):
    shell_path = _shell(shell)
    exe = _fake_exe(tmp_path / exe_dir)
    cwd = tmp_path / _AWKWARD_CWD
    cwd.mkdir()
    script = _create_script(tmp_path, exe, cwd)
    proc = _run_script(shell_path, script, tmp_path)
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert f"cwd={os.path.realpath(cwd)}" in lines
    assert "args=watch" in lines
    assert "CLAUDE_CONFIG_DIR=profiles/work" in lines
    assert "XDG_DATA_HOME=" in lines
    assert "PYTHONPATH=src" in lines
    assert not any(l.startswith("CLAUDE_SECURESTORAGE_CONFIG_DIR=") for l in lines)
    assert f"HOME={_MENU_HOME}" in lines  # the menu bar's HOME, not Terminal's
    assert "HOME=" + str(tmp_path) not in lines
    assert "PATH=" + _rc_environment(tmp_path)["PATH"] in lines  # the rest passes through
    assert proc.stderr == ""  # a clean exit reports nothing
    assert not script.exists()  # the script removed itself


@pytest.mark.parametrize("shell", ["sh", "bash", "zsh", "dash"])
def test_dashboard_script_file_removes_home_the_menu_bar_lacked(tmp_path: Path, shell):
    shell_path = _shell(shell)
    exe = _fake_exe(tmp_path / "bin dir")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    script = _create_script(tmp_path, exe, cwd, env=dict(_MENU_ENV, HOME=None))
    proc = _run_script(shell_path, script, tmp_path)
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert "args=watch" in lines
    assert not any(l.startswith("HOME=") for l in lines)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shebang")
def test_dashboard_script_file_runs_directly_by_its_shebang(tmp_path: Path):
    # Terminal executes the file itself, so the shebang and mode must work.
    exe = _fake_exe(tmp_path / "bin dir")
    cwd = tmp_path / _AWKWARD_CWD
    cwd.mkdir()
    script = _create_script(tmp_path, exe, cwd)
    proc = subprocess.run(
        [str(script)], env=_rc_environment(tmp_path), cwd=str(tmp_path),
        stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "args=watch" in proc.stdout.splitlines()
    assert not script.exists()


@pytest.mark.parametrize("shell", ["sh", "bash", "zsh", "dash"])
@pytest.mark.parametrize("failure", ["missing", "no access"])
def test_dashboard_script_file_reports_unenterable_cwd(tmp_path: Path, shell, failure):
    shell_path = _shell(shell)
    if failure == "no access" and hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root enters any directory")
    exe = _fake_exe(tmp_path / "bin dir")
    cwd = tmp_path / _AWKWARD_CWD
    script = _create_script(tmp_path, exe, cwd)
    try:
        if failure == "no access":
            cwd.mkdir()
            cwd.chmod(0)  # inside the try, so the finally always restores it
        proc = _run_script(shell_path, script, tmp_path)
    finally:
        if cwd.exists():
            cwd.chmod(0o755)
    assert f"cswap: cannot open the dashboard from {cwd}" in proc.stderr
    assert "Run: cswap watch" in proc.stderr
    assert "Press Return to close this window." in proc.stderr
    assert "args=" not in proc.stdout  # the dashboard never started
    assert not script.exists()


@pytest.mark.parametrize("shell", ["sh", "bash", "zsh", "dash"])
@pytest.mark.parametrize("case", ["exits 3", "missing exe", "not executable"])
def test_dashboard_script_file_reports_failed_dashboard(tmp_path: Path, shell, case):
    shell_path = _shell(shell)
    if case == "exits 3":
        exe, expected = _fake_exe(tmp_path / "bin dir", exit_code=3), 3
    elif case == "missing exe":
        exe, expected = tmp_path / "gone" / "cswap", 127
    else:
        exe = _fake_exe(tmp_path / "bin dir")
        exe.chmod(0o644)
        expected = 126
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    script = _create_script(tmp_path, exe, cwd)
    proc = _run_script(shell_path, script, tmp_path)
    assert f"cswap: the dashboard exited with status {expected}. Run: cswap watch" in proc.stderr
    assert "Press Return to close this window." in proc.stderr
    assert not script.exists()


def _pauses(tmp_path: Path, script: Path) -> tuple[bool, str]:
    """Whether the script stops for Return, and its stderr once answered.

    No clock decides it: stderr is read to the end, and the script counts as
    waiting when it prints the prompt and then blocks on its read, which
    this answers. A script that exits cleanly just closes stderr. The
    watchdog only bounds a hang (a read with no prompt), and a hang fails.
    """
    proc = subprocess.Popen(
        ["/bin/sh", str(script)], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE, text=True, env=_rc_environment(tmp_path),
    )
    watchdog = threading.Timer(10, proc.kill)
    watchdog.start()
    waited, err = False, []
    try:
        for line in proc.stderr:
            err.append(line)
            if "Press Return to close this window." in line:
                waited = True
                proc.stdin.write("\n")
                proc.stdin.close()
        proc.wait(timeout=10)
    finally:
        watchdog.cancel()
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode >= 0, "the script hung and was killed"
    return waited, "".join(err)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shells")
def test_dashboard_script_waits_for_return_after_unenterable_cwd(tmp_path: Path):
    # Terminal runs "<file> ; exit;", and a profile may close the window when
    # the shell exits, so the message must stay up until the user answers.
    script = _create_script(tmp_path, _fake_exe(tmp_path / "bin dir"), tmp_path / "missing")
    waited, err = _pauses(tmp_path, script)
    assert waited
    assert "Press Return to close this window." in err


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shells")
@pytest.mark.parametrize("exit_code", [3, 127])
def test_dashboard_script_waits_for_return_after_failed_dashboard(tmp_path: Path, exit_code):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    exe = _fake_exe(tmp_path / "bin dir", exit_code=3) if exit_code == 3 else tmp_path / "gone" / "cswap"
    waited, err = _pauses(tmp_path, _create_script(tmp_path, exe, cwd))
    assert waited
    assert f"exited with status {exit_code}" in err


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shells")
def test_dashboard_script_does_not_wait_after_clean_exit(tmp_path: Path):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    waited, err = _pauses(tmp_path, _create_script(tmp_path, _fake_exe(tmp_path / "bin dir"), cwd))
    assert not waited
    assert err == ""


def test_launcher_default_open_uses_process_env_cwd_and_script_dir(monkeypatch, tmp_path: Path):
    seen = {}

    def fake_open(cmd, script_dir, cwd, env=None):
        seen.update(cmd=cmd, script_dir=script_dir, env=env, cwd=cwd)
        return True, "ok"

    monkeypatch.setattr(menubar, "open_dashboard_terminal", fake_open)
    monkeypatch.setattr(menubar, "dashboard_executable", lambda: ["/bin/cswap", "watch"])
    monkeypatch.setattr(menubar, "dashboard_env", lambda: {"CLAUDE_CONFIG_DIR": "/x"})
    monkeypatch.setattr(menubar, "dashboard_cwd", lambda: "/menu/cwd")
    launcher = menubar.DashboardLauncher(_Logger(), tmp_path, start_thread=lambda target: target())
    launcher.start()
    assert seen == {
        "cmd": ["/bin/cswap", "watch"],
        "script_dir": tmp_path,
        "env": {"CLAUDE_CONFIG_DIR": "/x"},
        "cwd": "/menu/cwd",
    }


def test_launcher_refuses_to_open_without_a_working_directory(monkeypatch, tmp_path: Path):
    # Without the cd, relative profile and PYTHONPATH values would resolve
    # against Terminal's directory: a different profile or package. Fail closed.
    opened = []
    monkeypatch.setattr(menubar, "open_dashboard_terminal", lambda *a, **k: opened.append(1) or (True, "ok"))
    monkeypatch.setattr(menubar, "dashboard_executable", lambda: ["/bin/cswap", "watch"])
    monkeypatch.setattr(menubar, "dashboard_env", lambda: {"CLAUDE_CONFIG_DIR": "rel"})
    monkeypatch.setattr(menubar, "dashboard_cwd", lambda: None)
    logger = _Logger()
    launcher = menubar.DashboardLauncher(logger, tmp_path, start_thread=lambda target: target())
    launcher.start()
    assert opened == []
    error = launcher.take_error()
    assert error == menubar.DASHBOARD_CWD_GONE
    assert "working directory" in error and "restart the menu bar" in error
    assert logger.warnings == [f"Could not open dashboard: {error}"]


def test_launcher_refuses_when_the_script_cannot_be_written(monkeypatch, tmp_path: Path):
    ran = []
    monkeypatch.setattr(menubar.subprocess, "run", lambda *a, **k: ran.append(a) or _Proc(0))
    monkeypatch.setattr(menubar, "dashboard_executable", lambda: ["/bin/cswap", "watch"])
    monkeypatch.setattr(menubar, "dashboard_env", lambda: dict(_ENV_NONE))
    monkeypatch.setattr(menubar, "dashboard_cwd", lambda: "/menu/cwd")
    logger = _Logger()
    launcher = menubar.DashboardLauncher(
        logger, tmp_path / "no such dir", start_thread=lambda target: target()
    )
    launcher.start()
    assert ran == []  # Terminal was never asked to open anything
    error = launcher.take_error()
    assert error and "launcher script" in error
    assert logger.warnings == [f"Could not open dashboard: {error}"]


# --- stale launcher sweep ---------------------------------------------------------

def _dead_pid() -> int:
    """A pid that belonged to a process which has exited and been reaped."""
    proc = subprocess.Popen(["sleep", "30"])
    proc.terminate()
    proc.wait(timeout=5)
    return proc.pid


def _launcher(directory: Path, pid, rest: str = "x1y2z3") -> Path:
    path = directory / f"open-dashboard.{pid}.{rest}.command"
    path.write_text("x\n")
    return path


def test_create_dashboard_script_name_carries_the_creating_pid(tmp_path: Path):
    path = menubar.create_dashboard_script(tmp_path, "x\n")
    assert path.name.startswith(f"open-dashboard.{os.getpid()}.")
    assert path.name.endswith(".command")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pids and sleep")
def test_sweep_removes_launchers_of_dead_instances(tmp_path: Path):
    dead = _launcher(tmp_path, _dead_pid())
    removed = menubar.sweep_stale_dashboard_scripts(tmp_path)
    assert removed == 1
    assert not dead.exists()


def test_sweep_keeps_this_instances_launchers(tmp_path: Path):
    mine = _launcher(tmp_path, os.getpid())
    assert menubar.sweep_stale_dashboard_scripts(tmp_path) == 0
    assert mine.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pids and sleep")
def test_sweep_keeps_launchers_of_another_live_instance(tmp_path: Path):
    other = subprocess.Popen(["sleep", "30"])
    try:
        theirs = _launcher(tmp_path, other.pid)
        assert menubar.sweep_stale_dashboard_scripts(tmp_path) == 0
        assert theirs.exists()
    finally:
        other.terminate()
        other.wait(timeout=5)


def test_sweep_keeps_launchers_of_a_pid_it_may_not_signal(tmp_path: Path, monkeypatch):
    # kill(pid, 0) raising PermissionError means the process exists.
    def kill(pid, sig):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(menubar.os, "kill", kill)
    theirs = _launcher(tmp_path, 424242)
    assert menubar.sweep_stale_dashboard_scripts(tmp_path) == 0
    assert theirs.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pids and sleep")
def test_sweep_keeps_names_without_a_parsable_pid(tmp_path: Path):
    names = [
        "open-dashboard.abc123.command",   # no pid segment (older naming)
        "open-dashboard.12ab.x.command",
        "open-dashboard..x.command",
        "open-dashboard.-5.x.command",
        "open-dashboard. 7.x.command",
    ]
    for name in names:
        (tmp_path / name).write_text("x\n")
    assert menubar.sweep_stale_dashboard_scripts(tmp_path) == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(names)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pids and sleep")
def test_sweep_keeps_unrelated_names_even_with_a_dead_pid(tmp_path: Path):
    pid = _dead_pid()
    unrelated = [
        tmp_path / "accounts.json",
        tmp_path / f"backup.{pid}.x.command",           # right suffix, wrong prefix
        # Same length as the real prefix, so slicing the prefix off would
        # still find the dead pid: only the prefix check keeps it.
        tmp_path / f"other-launcher.{pid}.x.command",
        tmp_path / f"open-dashboard.{pid}.x.txt",       # right prefix, wrong suffix
    ]
    for p in unrelated:
        p.write_text("x\n")
    assert menubar.sweep_stale_dashboard_scripts(tmp_path) == 0
    assert all(p.exists() for p in unrelated)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pids and sleep")
def test_sweep_skips_non_regular_entries(tmp_path: Path):
    pid = _dead_pid()
    target = tmp_path / "keep-me.txt"
    target.write_text("x\n")
    link = tmp_path / f"open-dashboard.{pid}.link.command"
    link.symlink_to(target)
    folder = tmp_path / f"open-dashboard.{pid}.dir.command"
    folder.mkdir()
    menubar.sweep_stale_dashboard_scripts(tmp_path)
    assert link.is_symlink() and target.exists()
    assert folder.is_dir()


def test_sweep_missing_directory_is_a_no_op(tmp_path: Path):
    assert menubar.sweep_stale_dashboard_scripts(tmp_path / "gone") == 0


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pids and sleep")
def test_sweep_unlink_error_does_not_stop_the_others(tmp_path: Path, monkeypatch):
    pid = _dead_pid()
    stuck = _launcher(tmp_path, pid, "stuck")
    others = [_launcher(tmp_path, pid, f"o{i}") for i in range(3)]
    real_unlink = os.unlink

    def unlink(path, *a, **k):
        if os.fspath(path) == str(stuck):
            raise PermissionError(13, "Permission denied")
        return real_unlink(path, *a, **k)

    monkeypatch.setattr(menubar.os, "unlink", unlink)
    assert menubar.sweep_stale_dashboard_scripts(tmp_path) == 3
    assert stuck.exists()
    assert not any(p.exists() for p in others)


def test_launcher_sweeps_stale_scripts_before_creating_a_new_one(monkeypatch, tmp_path: Path):
    order = []
    monkeypatch.setattr(
        menubar, "sweep_stale_dashboard_scripts",
        lambda directory, **k: order.append(("sweep", directory)) or 0,
    )
    monkeypatch.setattr(
        menubar, "open_dashboard_terminal",
        lambda cmd, script_dir, cwd, env=None: order.append(("open", script_dir)) or (True, "ok"),
    )
    monkeypatch.setattr(menubar, "dashboard_executable", lambda: ["/bin/cswap", "watch"])
    monkeypatch.setattr(menubar, "dashboard_env", lambda: dict(_ENV_NONE))
    monkeypatch.setattr(menubar, "dashboard_cwd", lambda: "/menu/cwd")
    launched_on = []
    launcher = menubar.DashboardLauncher(
        _Logger(), tmp_path,
        start_thread=lambda target: launched_on.append(1) or target(),
    )
    launcher.start()
    assert order == [("sweep", tmp_path), ("open", tmp_path)]
    assert launched_on == [1]  # it ran inside the worker, not on the click


def test_launcher_real_sweep_keeps_a_pending_launch(monkeypatch, tmp_path: Path):
    # A launch Terminal has not picked up yet belongs to a live instance (this
    # one) and must survive the next click's sweep, however the clock moves.
    pending = menubar.create_dashboard_script(tmp_path, "x\n")
    old = 1_000_000_000
    os.utime(pending, (old, old))
    monkeypatch.setattr(menubar, "open_dashboard_terminal", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(menubar, "dashboard_executable", lambda: ["/bin/cswap", "watch"])
    monkeypatch.setattr(menubar, "dashboard_env", lambda: dict(_ENV_NONE))
    monkeypatch.setattr(menubar, "dashboard_cwd", lambda: "/menu/cwd")
    menubar.DashboardLauncher(_Logger(), tmp_path, start_thread=lambda t: t()).start()
    assert pending.exists()


def test_launcher_pid_accepts_up_to_pid_t_max():
    assert menubar._launcher_pid("open-dashboard.2147483647.x.command") == 2**31 - 1
    assert menubar._launcher_pid("open-dashboard.1.x.command") == 1


def test_launcher_pid_rejects_values_above_pid_t_max():
    assert menubar._launcher_pid("open-dashboard.2147483648.x.command") is None
    assert menubar._launcher_pid("open-dashboard.99999999999999999999.x.command") is None


def test_pid_alive_treats_unrepresentable_pid_as_alive():
    # os.kill raises OverflowError past C int range, outside OSError.
    assert menubar._pid_alive(2**31) is True
    assert menubar._pid_alive(2**64) is True


def test_sweep_keeps_oversized_pid_file_and_continues(tmp_path: Path):
    oversized = _launcher(tmp_path, 2**31)
    mine = _launcher(tmp_path, os.getpid())
    assert menubar.sweep_stale_dashboard_scripts(tmp_path) == 0
    assert oversized.exists() and mine.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pids and sleep")
def test_sweep_survives_an_unexpected_error_on_one_entry(tmp_path: Path, monkeypatch, caplog):
    pid = _dead_pid()
    broken = _launcher(tmp_path, 7777777)
    others = [_launcher(tmp_path, pid, f"o{i}") for i in range(2)]
    real_alive = menubar._pid_alive

    def alive(p):
        if p == 7777777:
            raise RuntimeError("unexpected")
        return real_alive(p)

    monkeypatch.setattr(menubar, "_pid_alive", alive)
    with caplog.at_level(logging.DEBUG, logger="claude-swap"):
        assert menubar.sweep_stale_dashboard_scripts(tmp_path) == 2
    assert broken.exists()
    assert not any(p.exists() for p in others)
    assert any("unexpected" in r.getMessage() or r.exc_info for r in caplog.records)


def test_sweep_never_raises_even_if_listing_breaks(tmp_path: Path, monkeypatch):
    def scandir(_d):
        raise RuntimeError("listing broke")

    monkeypatch.setattr(menubar.os, "scandir", scandir)
    assert menubar.sweep_stale_dashboard_scripts(tmp_path) == 0


def _launch_with_real_sweep(monkeypatch, tmp_path: Path):
    opened = []
    monkeypatch.setattr(
        menubar, "open_dashboard_terminal",
        lambda *a, **k: opened.append(1) or (True, "ok"),
    )
    monkeypatch.setattr(menubar, "dashboard_executable", lambda: ["/bin/cswap", "watch"])
    monkeypatch.setattr(menubar, "dashboard_env", lambda: dict(_ENV_NONE))
    monkeypatch.setattr(menubar, "dashboard_cwd", lambda: "/menu/cwd")
    launcher = menubar.DashboardLauncher(_Logger(), tmp_path, start_thread=lambda t: t())
    launcher.start()
    return opened, launcher.take_error()


def test_oversized_pid_file_never_blocks_the_launch(monkeypatch, tmp_path: Path):
    oversized = _launcher(tmp_path, 2**31)
    for _ in range(2):  # and not on the next click either
        opened, error = _launch_with_real_sweep(monkeypatch, tmp_path)
        assert opened == [1] and error is None
    assert oversized.exists()


def test_broken_entry_never_blocks_the_launch(monkeypatch, tmp_path: Path):
    _launcher(tmp_path, 7777777)

    def alive(_p):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(menubar, "_pid_alive", alive)
    opened, error = _launch_with_real_sweep(monkeypatch, tmp_path)
    assert opened == [1] and error is None


def test_sweep_takes_no_clock():
    import inspect

    params = inspect.signature(menubar.sweep_stale_dashboard_scripts).parameters
    assert "max_age_s" not in params and "now" not in params
    assert not hasattr(menubar, "DASHBOARD_SCRIPT_MAX_AGE")


def test_launcher_requires_a_script_dir_without_open_fn():
    with pytest.raises(ValueError):
        menubar.DashboardLauncher(_Logger())


# --- dashboard launcher (off the main thread) -----------------------------------

class _Logger:
    def __init__(self):
        self.warnings = []

    def warning(self, fmt, *args):
        self.warnings.append(fmt % args)


class _ManualThreads:
    """Captures thread targets so a test decides when the worker runs."""

    def __init__(self):
        self.targets = []

    def __call__(self, target):
        self.targets.append(target)

    def run_all(self):
        targets, self.targets = self.targets, []
        for t in targets:
            t()


def test_launcher_runs_open_off_the_calling_thread():
    opened_on = []
    main = threading.get_ident()

    def open_fn():
        opened_on.append(threading.get_ident())
        return True, "ok"

    launcher = menubar.DashboardLauncher(_Logger(), open_fn=open_fn)
    assert launcher.start() is True
    deadline = __import__("time").monotonic() + 5
    while launcher.in_flight and __import__("time").monotonic() < deadline:
        __import__("time").sleep(0.01)
    assert opened_on and opened_on[0] != main
    assert launcher.take_error() is None


def test_launcher_start_returns_before_open_finishes():
    threads = _ManualThreads()
    calls = []
    launcher = menubar.DashboardLauncher(
        _Logger(), open_fn=lambda: calls.append(1) or (True, "ok"), start_thread=threads
    )
    launcher.start()
    assert calls == []  # the click returned before Terminal was asked
    threads.run_all()
    assert calls == [1]


def test_launcher_ignores_second_click_while_in_flight():
    threads = _ManualThreads()
    calls = []
    launcher = menubar.DashboardLauncher(
        _Logger(), open_fn=lambda: calls.append(1) or (True, "ok"), start_thread=threads
    )
    assert launcher.start() is True
    assert launcher.start() is False
    assert len(threads.targets) == 1
    threads.run_all()
    assert calls == [1]
    assert launcher.in_flight is False
    assert launcher.start() is True  # a later click works again


def test_launcher_failure_is_logged_and_handed_to_main_thread_once():
    threads = _ManualThreads()
    logger = _Logger()
    launcher = menubar.DashboardLauncher(
        logger, open_fn=lambda: (False, "Not authorized (-1743)"), start_thread=threads
    )
    launcher.start()
    assert launcher.take_error() is None  # nothing until the worker finishes
    threads.run_all()
    assert logger.warnings == ["Could not open dashboard: Not authorized (-1743)"]
    assert launcher.take_error() == "Not authorized (-1743)"
    assert launcher.take_error() is None


def test_launcher_success_hands_back_nothing():
    threads = _ManualThreads()
    logger = _Logger()
    launcher = menubar.DashboardLauncher(logger, open_fn=lambda: (True, "ok"), start_thread=threads)
    launcher.start()
    threads.run_all()
    assert launcher.take_error() is None
    assert logger.warnings == []


def test_launcher_worker_exception_is_reported_and_clears_flag():
    threads = _ManualThreads()

    def boom():
        raise RuntimeError("boom")

    launcher = menubar.DashboardLauncher(_Logger(), open_fn=boom, start_thread=threads)
    launcher.start()
    threads.run_all()  # must not raise
    assert launcher.in_flight is False
    assert launcher.take_error() == "boom"


def test_launcher_thread_start_failure_does_not_raise_or_wedge():
    def no_threads(_target):
        raise RuntimeError("can't start new thread")

    launcher = menubar.DashboardLauncher(
        _Logger(), open_fn=lambda: (True, "ok"), start_thread=no_threads
    )
    launcher.start()
    assert launcher.in_flight is False
    assert launcher.take_error() == "can't start new thread"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shells")
def test_a_slow_clean_dashboard_does_not_count_as_waiting(tmp_path: Path):
    # A busy machine (a parallel test run) can make a clean exit slow: that
    # must still read as "did not wait", so no clock may decide it.
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    exe = _fake_exe(tmp_path / "bin dir")
    exe.write_text("#!/bin/sh\nsleep 1\nexit 0\n")
    waited, err = _pauses(tmp_path, _create_script(tmp_path, exe, cwd))
    assert not waited
    assert err == ""
