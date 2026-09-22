"""Tests for the menu bar module.

These tests never import or run rumps/AppKit. They exercise the pure helpers
(settings store, title/label formatting, usage/snapshot adapters, log parsing)
only — the auto-switch engine itself lives in ``claude_swap.autoswitch`` and is
tested there.
"""

from __future__ import annotations

import datetime as _dt
import json
import pathlib
import plistlib
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

def test_the_account_row_shows_a_session_count():
    label = menubar.format_account_label(
        2, "b@example.com", {"five_hour": {"pct": 10.0}}, now=1_000_000.0, sessions=2,
    )
    assert "2 sessions" in label


def test_no_sessions_leaves_the_row_alone():
    label = menubar.format_account_label(
        2, "b@example.com", {"five_hour": {"pct": 10.0}}, now=1_000_000.0,
    )
    assert "session" not in label


def test_the_row_says_it_the_way_the_tui_does(monkeypatch):
    """One renderer, two surfaces. Comparing the menu bar's output to the
    shared renderer's output would pass just as well if the menu bar spelled
    the wording out itself, so the seam is what is checked: replace the
    renderer and the row must change with it."""
    monkeypatch.setattr(menubar, "session_count_label", lambda n: f"<{n} of them>")
    label = menubar.format_account_label(
        2, "b@example.com", None, now=1_000_000.0, sessions=1,
    )
    assert "<1 of them>" in label


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
    def __init__(
        self, number, email, is_active, usage, alias="", disabled=False,
        managed_sessions=0,
    ):
        self.number = number
        self.email = email
        self.is_active = is_active
        self.usage = usage
        self.alias = alias
        self.disabled = disabled
        self.managed_sessions = managed_sessions


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
        _FakeAcct(
            "1", "a@x.com", True, _FakeEntry(last_good=lg, fetched_at=123.0),
            managed_sessions=2,
        ),
        _FakeAcct("2", "b@x.com", False, _FakeEntry(sentinel=USAGE_API_KEY), disabled=True),
    ]
    snap = menubar._adapt_snapshot(_FakeSnap(accts))
    assert snap["active_email"] == "a@x.com"
    assert snap["active_usage"] == lg
    assert snap["active_alias"] == ""
    # (num, email, is_active, display_usage, last_good, alias, disabled,
    #  fetched_at, managed_sessions)
    assert snap["accounts"][0] == ("1", "a@x.com", True, lg, lg, "", False, 123.0, 2)
    # sentinel account: display is the human note, last_good/fetched_at are None; disabled carried through
    assert snap["accounts"][1] == (
        "2", "b@x.com", False, menubar.SENTINEL_NOTES[USAGE_API_KEY], None, "", True,
        None, 0,
    )


def test_the_row_can_be_read_by_name():
    """The menu's three consumers read these rows by name — they live inside
    the rumps app glue, which the suite cannot import, so nothing else would
    notice a field constructed in the wrong position."""
    lg = {"five_hour": {"pct": 10.0}}
    accts = [_FakeAcct("1", "a@x.com", True, _FakeEntry(last_good=lg, fetched_at=9.0),
                       alias="work", disabled=True, managed_sessions=3)]
    row = menubar._adapt_snapshot(_FakeSnap(accts))["accounts"][0]
    assert (row.num, row.email, row.is_active) == ("1", "a@x.com", True)
    assert (row.display_usage, row.last_good, row.fetched_at) == (lg, lg, 9.0)
    assert (row.alias, row.disabled, row.sessions) == ("work", True, 3)


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


# --- engine notifications --------------------------------------------------

class TestNotificationFor:
    """Which engine events interrupt the user, and what they say.

    ``run``'s drain loop hands the result straight to ``rumps.notification``,
    so everything about that decision that can be tested off macOS lives
    here.
    """

    @staticmethod
    def _switch(dry_run: bool = False):
        from claude_swap.autoswitch import SwitchEvent

        return SwitchEvent(
            trigger="proactive",
            from_ref={"number": 1, "email": "a@example.com"},
            to_ref={"number": 2, "email": "b@example.com"},
            dry_run=dry_run,
        )

    def test_a_reassignment_names_the_session_and_the_new_account(self):
        from claude_swap.autoswitch import SessionReassignedEvent

        event = SessionReassignedEvent(
            session_id="auto-aaaaaaaa", number="3",
            from_email="b@example.com", to_email="c@example.com", reason="idle",
        )

        title, body = menubar.notification_for(event)
        assert title == "Managed session moved"
        assert "auto-aaaaaaaa" in body and "c@example.com" in body

    def test_the_other_reported_kinds_keep_their_titles(self):
        from claude_swap.autoswitch import (
            AllExhaustedEvent,
            ConfigWarningEvent,
            QuarantineEvent,
        )

        events = [
            self._switch(),
            QuarantineEvent(number="2", email="b@example.com", reason="invalid_grant"),
            AllExhaustedEvent(earliest_reset_at=None),
            ConfigWarningEvent(message="no account reports model 'Fabel'"),
        ]

        assert [menubar.notification_for(e)[0] for e in events] == [
            "Auto-switched account",
            "Account quarantined",
            "All accounts exhausted",
            "Configuration warning",
        ]
        assert all(menubar.notification_for(e)[1] == e.human() for e in events)

    def test_a_previewed_switch_stays_quiet(self):
        # The TUI runs the engine dry; notifying would announce a switch that
        # never happened.
        assert menubar.notification_for(self._switch(dry_run=True)) is None

    def test_the_log_only_kinds_stay_quiet(self):
        from claude_swap.autoswitch import (
            ManagedSessionsRefreshedEvent,
            NoSwitchEvent,
            SleepEvent,
        )

        quiet = [
            NoSwitchEvent(reason="below threshold", detail=""),
            SleepEvent(seconds=30.0, until="2024-01-01T00:00:30Z"),
            ManagedSessionsRefreshedEvent(
                number="2", email="b@example.com", source="backup",
                sessions=("auto-000000b1",),
            ),
        ]
        assert [menubar.notification_for(e) for e in quiet] == [None, None, None]


# --- the account row's consumers ---------------------------------------------


def _row(num="1", email="a@x.com", *, last_good=None, sessions=0):
    """A row with its two usage fields DIFFERENT on purpose.

    `display_usage` is what the menu shows and `last_good` is the last real
    measurement, and for an account with a sentinel they are not the same
    kind of thing at all: the display is a note string while `last_good` is
    still a dict. A helper that put one object in both would let a reader of
    the wrong field pass every test here and then quietly stop logging
    usage for exactly those accounts.
    """
    return menubar.AccountRow(
        num, email, False, menubar.SENTINEL_NOTES[USAGE_API_KEY], last_good,
        "", False, None, sessions,
    )


def test_the_usage_log_reads_rows_by_name():
    """The row shape this reads is the one `_adapt_snapshot` produces, and it
    used to be read by position inside the rumps app glue — where the suite
    cannot reach it. It lives out here now, because a row it cannot take
    apart is not a cosmetic failure: the refresh worker logs the snapshot
    before it assigns it, so a raise here leaves the menu bar on its empty
    snapshot for good."""
    usage = {"five_hour": {"pct": 35.0}, "seven_day": {"pct": 10.0}}
    seen: dict = {}
    lines = menubar.usage_log_lines([_row(last_good=usage, sessions=2)], seen)
    assert lines == [menubar.format_usage_log("a@x.com", usage)]
    assert seen == {"1": menubar._usage_log_key(usage)}


def test_the_usage_log_says_a_thing_once():
    usage = {"five_hour": {"pct": 35.0}, "seven_day": {"pct": 10.0}}
    seen: dict = {}
    rows = [_row(last_good=usage)]
    assert menubar.usage_log_lines(rows, seen)
    assert menubar.usage_log_lines(rows, seen) == []
    moved = {"five_hour": {"pct": 40.0}, "seven_day": {"pct": 10.0}}
    assert menubar.usage_log_lines([_row(last_good=moved)], seen)


def test_the_usage_log_skips_an_account_with_nothing_to_say():
    seen: dict = {}
    assert menubar.usage_log_lines([_row(last_good=None)], seen) == []
    assert seen == {}


def _row_takedowns(source: str, *, lists=()) -> list[tuple[int, int | None]]:
    """Every place ``source`` takes an account row apart.

    One entry per site, as ``(line, names)``: ``names`` is None when the row
    is bound to a single name (and therefore read by field), and the number
    of names when it is unpacked positionally. Follows the accounts list and
    individual rows through assignments — plain and annotated, to a fixed
    point, so an alias of an alias is still an alias — and looks at ``for``
    loops, comprehensions and tuple assignments alike.

    ``lists`` seeds the set of names holding the accounts list, for a
    consumer that is handed the rows as an argument instead of reading them
    out of the snapshot itself.

    The one shape it does NOT follow is a call: ``for a, b in
    list(self.snapshot["accounts"])`` is invisible to it. Closing that means
    deciding which calls pass a list through unchanged, which is a judgement
    this does not have; the tripwire on the number of sites found is what
    covers it, since a new consumer written that way leaves the count short.
    """
    import ast

    def is_accounts(node):
        return (
            isinstance(node, ast.Subscript)
            and isinstance(node.slice, ast.Constant)
            and node.slice.value == "accounts"
        )

    tree = ast.parse(source)
    assignments = [
        (node.targets if isinstance(node, ast.Assign) else [node.target], node.value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None
    ]
    loops = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension))
    ]

    def is_list(node):
        return is_accounts(node) or (
            isinstance(node, ast.Name) and node.id in lists
        )

    def is_row(node):
        # `accounts[i]`, or a name already known to hold a row.
        return (isinstance(node, ast.Subscript) and is_list(node.value)) or (
            isinstance(node, ast.Name) and node.id in rows
        )

    # Names are file-wide rather than per-function: a name shadowed in
    # another scope would only make this look at one site too many, which is
    # the safe direction for a guard.
    lists: set[str] = set(lists)
    rows: set[str] = set()
    for _ in range(len(assignments) + len(loops) + 1):
        before = (len(lists), len(rows))
        for targets, value in assignments:
            for target in targets:
                if isinstance(target, ast.Name):
                    if is_list(value):
                        lists.add(target.id)
                    elif is_row(value):
                        rows.add(target.id)
        for loop in loops:
            if is_list(loop.iter) and isinstance(loop.target, ast.Name):
                rows.add(loop.target.id)
        if (len(lists), len(rows)) == before:
            break

    found: dict[int, tuple[int, int | None]] = {}

    def record(target, source_node):
        line = getattr(source_node, "lineno", None) or target.lineno
        if isinstance(target, ast.Tuple):
            # A starred element counts as positional too: it survives a
            # field being added, but it still reads fields by position.
            found[line] = (line, len(target.elts))
        elif isinstance(target, ast.Name):
            found[line] = (line, None)

    for loop in loops:
        if is_list(loop.iter):
            record(loop.target, getattr(loop, "target", loop))
    for targets, value in assignments:
        if is_row(value):
            for target in targets:
                record(target, target)
    return sorted(found.values())


def test_no_row_is_taken_apart_by_position():
    """The structural guard for a file the suite cannot import.

    The rows are consumed in four places. Three of them live inside
    `MenuBarApp`, which is defined inside the function that imports rumps —
    an optional extra this suite never installs — so nothing here can catch
    a bad unpack by running them. The ninth field (session counts) turned
    every positional unpack into a `ValueError` at runtime, so this reads
    the module's own source instead: a row may be bound to one name and read
    by field, or unpacked into exactly as many names as the row has.

    Following the list through an alias is the point, not a detail: a search
    for `self.snapshot["accounts"]` cannot see `accounts =
    self.snapshot["accounts"]` two lines earlier, and the consumer that
    shipped broken read a local `snap["accounts"]` instead.
    """
    width = len(menubar.AccountRow._fields)
    source = pathlib.Path(menubar.__file__).read_text(encoding="utf-8")
    # `rows` is the parameter the fourth consumer takes its list under: it
    # is handed the rows rather than reading them out of the snapshot, so it
    # has to be named for the guard to see it. That one is ordinary tested
    # code (above) and is held to the same rule anyway, since its positional
    # unpack is the one that shipped.
    takedowns = _row_takedowns(source, lists=("rows",))
    for line, names in takedowns:
        assert names is None or names == width, (
            f"{menubar.__file__}:{line} reads {names} fields of a "
            f"{width}-field AccountRow by position"
        )
    assert len(takedowns) == 4, (
        f"expected the four known row consumers, found {len(takedowns)} — if "
        "one was added, moved, or written through a call this cannot follow, "
        "this guard has to be pointed at it"
    )


@pytest.mark.parametrize("snippet, expected", [
    ('for a, b in self.snapshot["accounts"]:\n    pass', [(1, 2)]),
    ('for row in self.snapshot["accounts"]:\n    pass', [(1, None)]),
    ('accounts = self.snapshot["accounts"]\nfor a, b in accounts:\n    pass',
     [(2, 2)]),
    # An alias of an alias, and an annotated one: both were holes.
    ('accounts = self.snapshot["accounts"]\nlater = accounts\n'
     'for a, b in later:\n    pass', [(3, 2)]),
    ('rows: list = self.snapshot["accounts"]\nfor a, b in rows:\n    pass',
     [(2, 2)]),
    # A single row, pulled out and unpacked rather than looped over.
    ('accounts = self.snapshot["accounts"]\nfirst, second = accounts[0]',
     [(2, 2)]),
    ('row = self.snapshot["accounts"][0]\nnum, email = row', 
     [(1, None), (2, 2)]),
    # A starred read is still positional.
    ('for a, b, *rest in self.snapshot["accounts"]:\n    pass', [(1, 3)]),
    # Comprehensions: the branch that used to raise on a missing lineno.
    ('x = [a for a, b in self.snapshot["accounts"]]', [(1, 2)]),
    ('x = [r.num for r in self.snapshot["accounts"]]', [(1, None)]),
    # Nothing to do with the rows.
    ('for a, b in other["things"]:\n    pass', []),
    # The documented blind spot.
    ('for a, b in list(self.snapshot["accounts"]):\n    pass', []),
])
def test_the_guard_sees_what_it_claims_to(snippet, expected):
    """The guard is a parser, so it gets tests of its own — every shape here
    is one a reader could plausibly write, and all but the last one were
    holes at some point."""
    assert _row_takedowns(snippet) == expected
