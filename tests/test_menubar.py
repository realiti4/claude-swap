"""Tests for the menu bar module.

These tests never import or run rumps/AppKit. They exercise the pure helpers
(settings store, title/label formatting, usage/snapshot adapters, log parsing)
only — the auto-switch engine itself lives in ``claude_swap.autoswitch`` and is
tested there.
"""

from __future__ import annotations

import datetime as _dt
import json
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


# --- log tail ------------------------------------------------------------------

def test_read_log_tail_returns_whole_short_file(tmp_path: Path):
    log = tmp_path / "claude-swap.log"
    log.write_bytes(b"one\ntwo\n")
    assert menubar.read_log_tail(log, max_bytes=4096) == "one\ntwo\n"


def test_read_log_tail_drops_partial_first_line(tmp_path: Path):
    log = tmp_path / "claude-swap.log"
    log.write_bytes(b"aaaa\nbbbb\ncccc\n")
    # A window landing mid-"aaaa" must not yield a truncated line.
    assert menubar.read_log_tail(log, max_bytes=12) == "bbbb\ncccc\n"


def test_read_log_tail_missing_file_is_empty(tmp_path: Path):
    assert menubar.read_log_tail(tmp_path / "absent.log") == ""


# --- menu model ----------------------------------------------------------------

def _account(num, email, *, active=False, usage=None, alias=None,
             disabled=False, fetched_at=None):
    """One row of the menu bar's render dict (see ``_adapt_snapshot``)."""
    return (num, email, active, usage, usage, alias, disabled, fetched_at)


def _snapshot(*accounts, active_email=None, active_usage=None, active_alias=None):
    return {
        "accounts": list(accounts),
        "active_email": active_email,
        "active_usage": active_usage,
        "active_alias": active_alias,
    }


def _model(snapshot, settings=None, threshold=90, history=()):
    return menubar.build_menu_model(
        snapshot,
        settings or menubar.MenuBarSettings(),
        threshold=threshold,
        history=history,
    )


def test_menu_model_rows_track_accounts():
    model = _model(_snapshot(
        _account("1", "a@x.com", active=True, usage=_USAGE),
        _account("2", "b@x.com", alias="dev"),
    ))
    assert model.slots == ("1", "2")
    assert model.accounts[0].label == "1  a@x.com  5h 42% · 7d 18% · $ 30%"
    assert model.accounts[0].state == 1
    assert model.accounts[1].state == 0
    assert model.remove[1].label == "2  dev  (b@x.com)"


def test_menu_model_disable_row_state_tracks_disabled():
    model = _model(_snapshot(
        _account("1", "a@x.com"),
        _account("2", "b@x.com", disabled=True),
    ))
    assert [row.state for row in model.disable] == [0, 1]


def test_menu_model_equal_when_nothing_changed():
    """An unchanged tick must produce an equal model, so the app can skip it."""
    snapshot = _snapshot(_account("1", "a@x.com", active=True, usage=_USAGE))
    assert _model(snapshot) == _model(snapshot)


def test_menu_model_usage_change_keeps_shape():
    """The common case: percentages moved, so relabel in place — never rebuild.

    This is the property the leak fix rests on. Rebuilding here is what stranded
    a menu's worth of items in rumps' global callback map on every refresh.
    """
    before = _model(_snapshot(_account("1", "a@x.com", active=True, usage=_USAGE)))
    after = _model(_snapshot(_account(
        "1", "a@x.com", active=True,
        usage={"five_hour": {"pct": 99.0}, "seven_day": {"pct": 18.0}},
    )))
    assert before != after
    assert before.shape == after.shape


def test_menu_model_settings_change_keeps_shape():
    snapshot = _snapshot(_account("1", "a@x.com"))
    before = _model(snapshot, menubar.MenuBarSettings(auto_switch_enabled=False))
    after = _model(snapshot, menubar.MenuBarSettings(auto_switch_enabled=True))
    assert before != after
    assert before.shape == after.shape


def test_menu_model_added_account_changes_shape():
    before = _model(_snapshot(_account("1", "a@x.com")))
    after = _model(_snapshot(_account("1", "a@x.com"), _account("2", "b@x.com")))
    assert before.shape != after.shape


def test_menu_model_reassigned_slot_changes_shape():
    """A row's callback is bound to its slot, so a changed slot must rebuild.

    Same row count, different account behind row 2: relabelling would leave the
    row switching to the account that used to occupy it.
    """
    before = _model(_snapshot(_account("1", "a@x.com"), _account("2", "b@x.com")))
    after = _model(_snapshot(_account("1", "a@x.com"), _account("3", "c@x.com")))
    assert len(before.accounts) == len(after.accounts)
    assert before.shape != after.shape


def test_menu_model_empty_accounts_uses_placeholders():
    model = _model(_snapshot())
    assert model.slots == ()
    assert model.accounts == (menubar.MenuRow(menubar.NO_ACCOUNTS),)
    assert model.remove == (menubar.MenuRow(menubar.NO_ACCOUNTS),)
    assert model.disable == (menubar.MenuRow(menubar.NO_ACCOUNTS),)


def test_menu_model_first_account_changes_shape_from_empty():
    """Empty and one-account menus both show one row but need different callbacks."""
    assert _model(_snapshot()).shape != _model(_snapshot(_account("1", "a@x.com"))).shape


def test_menu_model_history_placeholder_when_empty():
    assert _model(_snapshot()).history == (menubar.NO_HISTORY,)


def test_menu_model_history_passthrough():
    model = _model(_snapshot(), history=["2 → 1   2026-06-27 02:06"])
    assert model.history == ("2 → 1   2026-06-27 02:06",)


def test_settings_rows_reflect_settings_and_threshold():
    rows = menubar.settings_rows(
        menubar.MenuBarSettings(show_account_name=True, title_pct="5h", refresh_interval=300),
        threshold=95,
    )
    checked = {row.label for row in rows if row.state}
    assert checked == {"Show account name in menu bar", "Session (5h)", "5 minutes", "95%"}


def test_settings_rows_length_is_fixed():
    """Settings never change the item count, so they never force a rebuild."""
    a = menubar.settings_rows(menubar.MenuBarSettings(), threshold=80)
    b = menubar.settings_rows(menubar.MenuBarSettings(title_pct="off"), threshold=98)
    assert len(a) == len(b)


# --- app-level menu behaviour --------------------------------------------------
# The fix lives in the app glue -- sync_menu / rebuild_menu / _apply_model and the
# rumps registry cleanup -- which the pure-helper tests above cannot reach. Rather
# than import rumps (which this suite must not do, and which needs macOS), stand
# in a fake that reproduces the semantics the fix depends on: menu items keyed by
# title, a duplicate title silently dropped, and every callback recorded in one
# process-global dict that rumps itself never prunes.

class _FakeNSApp:
    _ns_to_py_and_callback: dict = {}


class _FakeSeparator:
    """Stand-in for rumps.SeparatorMenuItem.

    Deliberately not a MenuItem and deliberately without ``values()`` --
    that is exactly why ``_forget_menu_items`` has to type-check before it
    recurses, and a fake that skipped separators could not catch the guard
    being removed. Never registered: rumps only records items with a
    callback.
    """

    def __init__(self):
        self._menuitem = object()


class _FakeMenuItem:
    def __init__(self, title="", callback=None):
        self.title = title
        self.state = 0
        self._menuitem = object()  # stands in for the NSMenuItem key
        self._children: dict = {}
        self._separators = 0
        _FakeNSApp._ns_to_py_and_callback[self._menuitem] = (self, callback)

    @property
    def callback(self):
        return _FakeNSApp._ns_to_py_and_callback[self._menuitem][1]

    def add(self, item):
        if item is None:
            self._separators += 1
            self._children[f"__sep{self._separators}__"] = _FakeSeparator()
            return
        if item.title not in self._children:  # rumps drops duplicate titles
            self._children[item.title] = item

    def values(self):
        return list(self._children.values())

    def clear(self):
        self._children.clear()

    def update(self, iterable):
        for item in iterable:
            self.add(item)


class _FakeApp:
    def __init__(self, title="", quit_button=None):
        self.title = title
        self._menu = _FakeMenuItem("__main__")

    @property
    def menu(self):
        return self._menu

    @menu.setter
    def menu(self, iterable):
        self._menu.update(iterable)

    def run(self):  # replaced per-test to capture the instance
        raise AssertionError("the fake app must not enter a run loop")


class _FakeTimer:
    def __init__(self, callback, interval):
        self.callback, self.interval = callback, interval

    def start(self):
        pass

    def stop(self):
        pass


def _fake_rumps():
    import types

    module = types.ModuleType("rumps")
    module.App = _FakeApp
    module.MenuItem = _FakeMenuItem
    module.Timer = _FakeTimer
    module.separator = _FakeSeparator
    module.alert = lambda *a, **k: 1
    module.notification = lambda *a, **k: None
    module.Window = None
    inner = types.ModuleType("rumps.rumps")
    inner.NSApp = _FakeNSApp
    module.rumps = inner
    return module


class _StubSwitcher:
    def __init__(self, backup_dir: Path):
        self.backup_dir = backup_dir
        self._logger = __import__("logging").getLogger("test-menubar")
        self.disabled_calls: list = []
        self.switched_to: list = []

    def _get_claude_config_path(self):
        return self.backup_dir / "claude.json"

    def set_account_disabled(self, num, target):
        self.disabled_calls.append((num, target))

    def switch_to(self, num):
        self.switched_to.append(num)


def _build_app(monkeypatch, tmp_path: Path):
    """Construct the real MenuBarApp against the fake rumps, without a run loop."""
    import claude_swap.snapshot_source as snapshot_source

    _FakeNSApp._ns_to_py_and_callback = {}
    fake = _fake_rumps()
    monkeypatch.setitem(sys.modules, "rumps", fake)
    monkeypatch.setitem(sys.modules, "AppKit", _fake_appkit())
    monkeypatch.setattr(menubar, "ensure_notification_identity", lambda *a, **k: None)
    monkeypatch.setattr(snapshot_source, "SnapshotSource", _StubSnapshotSource)

    captured = {}
    monkeypatch.setattr(_FakeApp, "run", lambda self: captured.setdefault("app", self))
    switcher = _StubSwitcher(tmp_path)
    menubar.run(switcher)
    app = captured["app"]
    app.refresh_async = lambda full=False: None  # no background worker in tests
    return app, switcher, _FakeNSApp._ns_to_py_and_callback


def _fake_appkit():
    import types

    module = types.ModuleType("AppKit")
    module.NSApplicationActivationPolicyAccessory = 1
    shared = type("_App", (), {"setActivationPolicy_": lambda self, p: None})()
    module.NSApplication = type("_NSApplication", (), {"sharedApplication": staticmethod(lambda: shared)})
    return module


class _StubSnapshotSource:
    def __init__(self, switcher):
        pass

    def take(self, **kwargs):
        raise AssertionError("tests drive app.snapshot directly")


def _submenu(app, title):
    """Top-level submenu by title, skipping separators."""
    for item in app.menu.values():
        if getattr(item, "title", None) == title:
            return item
    return None


def _snap(*rows, active=0):
    """Build the render dict for the given (num, pct, disabled) rows."""
    accounts = []
    for index, (num, pct, disabled) in enumerate(rows):
        usage = {"five_hour": {"pct": pct}, "seven_day": {"pct": 20.0}}
        accounts.append((num, f"a{num}@x.com", index == active, usage, usage,
                         None, disabled, None))
    return {
        "accounts": accounts,
        "active_email": accounts[active][1] if accounts else None,
        "active_usage": accounts[active][3] if accounts else None,
        "active_alias": None,
    }


def test_sync_menu_does_not_grow_the_rumps_registry(monkeypatch, tmp_path: Path):
    """The regression this whole change exists for.

    rumps never prunes ``NSApp._ns_to_py_and_callback``, so recreating the menu
    on every refresh stranded a menu's worth of items per tick. Refreshes that
    only move percentages must not add a single entry.
    """
    app, _switcher, registry = _build_app(monkeypatch, tmp_path)
    app.snapshot = _snap(("1", 10.0, False), ("2", 20.0, False))
    app.rebuild_menu()
    baseline = len(registry)

    for tick in range(50):
        app.snapshot = _snap(("1", 10.0 + tick, False), ("2", 20.0, False))
        app.sync_menu()

    assert len(registry) == baseline


def test_rebuild_unregisters_the_items_it_discards(monkeypatch, tmp_path: Path):
    """Structural rebuilds must not leak either."""
    app, _switcher, registry = _build_app(monkeypatch, tmp_path)
    app.snapshot = _snap(("1", 10.0, False))
    app.rebuild_menu()
    after_first = len(registry)

    for _ in range(20):
        app.rebuild_menu()

    assert len(registry) == after_first


def test_forget_menu_items_keeps_live_items_registered(monkeypatch, tmp_path: Path):
    """Over-pruning would break clicks: rumps looks callbacks up by NSMenuItem."""
    app, _switcher, registry = _build_app(monkeypatch, tmp_path)
    app.snapshot = _snap(("1", 10.0, False), ("2", 20.0, False))
    app.rebuild_menu()

    def walk(item):
        yield item
        for child in getattr(item, "values", list)():
            yield from walk(child)

    live = [i for i in walk(app.menu)
            if i is not app.menu and not isinstance(i, _FakeSeparator)]
    assert live and all(i._menuitem in registry for i in live)


def test_sync_menu_updates_in_place_without_replacing_items(monkeypatch, tmp_path: Path):
    app, _switcher, _registry = _build_app(monkeypatch, tmp_path)
    app.snapshot = _snap(("1", 10.0, False), ("2", 20.0, False))
    app.rebuild_menu()
    identities = [id(i) for i in app._items["accounts"]]

    app.snapshot = _snap(("1", 77.0, False), ("2", 88.0, True), active=1)
    app.sync_menu()

    assert [id(i) for i in app._items["accounts"]] == identities
    assert [i.state for i in app._items["accounts"]] == [0, 1]
    assert "5h 77%" in app._items["accounts"][0].title
    assert "(disabled)" in app._items["accounts"][1].title
    assert [i.state for i in app._items["disable"]] == [0, 1]


def test_sync_menu_rebuilds_and_rebinds_when_slots_change(monkeypatch, tmp_path: Path):
    """A row's callback belongs to its slot, so a changed slot set must rebuild."""
    app, switcher, _registry = _build_app(monkeypatch, tmp_path)
    app.snapshot = _snap(("1", 10.0, False), ("2", 20.0, False))
    app.rebuild_menu()
    identities = [id(i) for i in app._items["accounts"]]

    app.snapshot = _snap(("1", 10.0, False), ("3", 20.0, False))
    app.sync_menu()

    assert [id(i) for i in app._items["accounts"]] != identities
    app._items["accounts"][1].callback(None)
    assert switcher.switched_to == ["3"]


def test_placeholder_row_is_not_clickable(monkeypatch, tmp_path: Path):
    app, _switcher, _registry = _build_app(monkeypatch, tmp_path)
    app.snapshot = _snap()
    app.rebuild_menu()
    assert [i.title for i in app._items["accounts"]] == [menubar.NO_ACCOUNTS]
    assert app._items["accounts"][0].callback is None


def test_duplicate_history_lines_keep_items_and_menu_aligned(monkeypatch, tmp_path: Path):
    """rumps drops a duplicate title, so the model must not list one.

    Two identical switches inside one minute render as the same string (the
    stamp is trimmed to the minute). If the model kept both, ``_items`` would
    outnumber the rows actually in the menu -- permanently, because the shape
    would still match and no rebuild would ever correct it.
    """
    log = tmp_path / "claude-swap.log"
    line = "2026-08-12 02:06:01 - claude-swap - INFO - Switched from account 1 to 2"
    log.write_bytes(("\n".join([line, line, line.replace("02:06:01", "02:07:30")]) + "\n").encode())

    app, _switcher, _registry = _build_app(monkeypatch, tmp_path)
    app.snapshot = _snap(("1", 10.0, False))
    app.rebuild_menu()

    history_menu = _submenu(app, "Switch history")
    rendered = [i for i in history_menu.values() if not isinstance(i, _FakeSeparator)]
    # Identity, not title: a dropped duplicate has the same text as the row
    # that did make it in, so comparing labels would pass either way.
    orphans = [i for i in app._items["history"] if not any(r is i for r in rendered)]
    assert orphans == []
    assert len(app._model.history) == len(set(app._model.history))


def test_failed_rebuild_recovers_on_the_next_tick(monkeypatch, tmp_path: Path):
    """A rebuild that dies partway must not strand the menu forever.

    rumps swallows exceptions raised inside a timer callback, so a failure here
    is invisible. Leaving ``_model`` populated would route every later tick into
    an in-place update against items that were never built.
    """
    app, _switcher, _registry = _build_app(monkeypatch, tmp_path)
    app.snapshot = _snap(("1", 10.0, False))
    app.rebuild_menu()

    boom = {"raise": True}
    original = app._settings_menu

    def flaky(model):
        if boom["raise"]:
            raise RuntimeError("simulated failure mid-rebuild")
        return original(model)

    app._settings_menu = flaky
    app.snapshot = _snap(("1", 10.0, False), ("2", 20.0, False))
    with pytest.raises(RuntimeError):
        app.sync_menu()
    assert app._model is None  # forces a rebuild rather than an in-place update

    boom["raise"] = False
    app.sync_menu()
    assert _submenu(app, "Settings") is not None
    assert len(app._items["accounts"]) == 2


def test_sync_tick_applies_a_dirty_snapshot(monkeypatch, tmp_path: Path):
    """The refresh timer is the only caller in production."""
    app, _switcher, _registry = _build_app(monkeypatch, tmp_path)
    app.snapshot = _snap(("1", 10.0, False))
    app.rebuild_menu()

    app.snapshot = _snap(("1", 66.0, False))
    app._dirty = True
    app.on_sync_tick(None)

    assert "5h 66%" in app._items["accounts"][0].title
    assert app._dirty is False


def test_unchanged_snapshot_leaves_the_menu_untouched(monkeypatch, tmp_path: Path):
    app, _switcher, _registry = _build_app(monkeypatch, tmp_path)
    app.snapshot = _snap(("1", 10.0, False))
    app.rebuild_menu()
    before = app._model
    app.sync_menu()
    assert app._model is before  # same object: no model swap, no UI work


def test_toggle_disabled_reads_state_at_click_time(monkeypatch, tmp_path: Path):
    """The row outlives the state change now, so the flag cannot be captured."""
    app, switcher, _registry = _build_app(monkeypatch, tmp_path)
    app.snapshot = _snap(("1", 10.0, False))
    app.rebuild_menu()
    toggle = app._items["disable"][0].callback

    toggle(None)
    assert switcher.disabled_calls[-1] == ("1", True)

    app.snapshot = _snap(("1", 10.0, True))
    app.sync_menu()
    toggle(None)
    assert switcher.disabled_calls[-1] == ("1", False)


def test_log_tail_window_covers_a_full_rotation():
    """The window must never be the reason a switch is missing from the menu.

    Switch lines are sparse among usage lines: a 64KB window found 2 of 10 on a
    real 514KB log. Asserted against the rotation constant rather than a literal
    so the two cannot drift apart.
    """
    from claude_swap.logging_config import LOG_MAX_BYTES

    assert menubar.LOG_TAIL_BYTES >= LOG_MAX_BYTES


def test_read_log_tail_at_exactly_the_window_size(tmp_path: Path):
    """Boundary: the whole file fits, so no line may be dropped."""
    log = tmp_path / "claude-swap.log"
    log.write_bytes(b"abcd\nefgh\n")
    assert menubar.read_log_tail(log, max_bytes=10) == "abcd\nefgh\n"
    assert menubar.read_log_tail(log, max_bytes=9) == "efgh\n"


def test_switch_history_dedupes_before_applying_the_limit():
    """A repeated line must not consume a slot an older distinct switch could fill."""
    stamps = [f"2026-06-27 02:{minute:02d}:00" for minute in range(12)]
    lines = [f"{s} - claude-swap - INFO - Switched from account 1 to 2" for s in stamps]
    lines.insert(0, lines[0])  # an exact repeat of the oldest entry
    out = menubar.parse_switch_history("\n".join(lines), limit=10)
    assert len(out) == len(set(out)) == 10


def test_build_menu_model_dedupes_history_independently():
    """Held separately from the parser: the model's own invariant is that every
    row it lists can exist in the menu, whatever the caller hands it."""
    model = _model(_snapshot(), history=["a", "a", "b"])
    assert model.history == ("a", "b")


def test_repeated_syncs_keep_updating_in_place(monkeypatch, tmp_path: Path):
    """Two ticks, not one.

    A single sync after a rebuild proves little: `_model` is still whatever the
    rebuild set. Only the second tick shows the in-place path restored it, and a
    regression here silently reverts to rebuilding on every refresh.
    """
    app, _switcher, registry = _build_app(monkeypatch, tmp_path)
    app.snapshot = _snap(("1", 10.0, False))
    app.rebuild_menu()
    identities = [id(i) for i in app._items["accounts"]]
    baseline = len(registry)

    for pct in (30.0, 60.0, 90.0):
        app.snapshot = _snap(("1", pct, False))
        app.sync_menu()
        assert [id(i) for i in app._items["accounts"]] == identities
        assert f"5h {pct:.0f}%" in app._items["accounts"][0].title
        assert app.title.endswith(f"{pct:.0f}% · 20%")

    assert len(registry) == baseline


def test_settings_toggle_moves_its_check_mark_in_place(monkeypatch, tmp_path: Path):
    app, _switcher, _registry = _build_app(monkeypatch, tmp_path)
    app.snapshot = _snap(("1", 10.0, False))
    app.rebuild_menu()
    name_item = app._items["settings"][0]
    before = name_item.state

    name_item.callback(None)  # the menu entry the user would click

    assert name_item.state != before
    assert app._items["settings"][0] is name_item  # updated, not rebuilt


def test_history_rows_follow_the_log_between_syncs(monkeypatch, tmp_path: Path):
    log = tmp_path / "claude-swap.log"

    def write(*pairs):
        log.write_bytes("\n".join(
            f"2026-08-12 02:{m:02d}:00 - claude-swap - INFO - "
            f"Switched from account {a} to {b}" for a, b, m in pairs
        ).encode() + b"\n")

    write(("1", "2", 1), ("2", "1", 2))
    app, _switcher, _registry = _build_app(monkeypatch, tmp_path)
    app.snapshot = _snap(("1", 10.0, False))
    app.rebuild_menu()
    items = list(app._items["history"])
    assert [i.title for i in items] == ["2 → 1   2026-08-12 02:02", "1 → 2   2026-08-12 02:01"]

    write(("3", "1", 5), ("1", "3", 6))
    app.sync_menu()

    assert [id(i) for i in app._items["history"]] == [id(i) for i in items]
    assert [i.title for i in app._items["history"]] == [
        "1 → 3   2026-08-12 02:06", "3 → 1   2026-08-12 02:05",
    ]


def test_build_menu_model_title_honours_now():
    """`now` reaches the title, not just the rows -- the model is fully pure.

    Without this the docstring's "no I/O" claim is false for the title, and a
    future test pinning a countdown would be reading the wall clock.
    """
    # A weekly window rolls to 0% once its reset passes, so the same data reads
    # differently either side of it -- the one title component `now` governs.
    usage = {"seven_day": {"pct": 95.0, "resets_at": _iso(1800)}}
    settings = menubar.MenuBarSettings(show_account_name=False, title_pct="7d")
    snapshot = _snapshot(_account("1", "a@x.com", active=True, usage=usage),
                         active_email="a@x.com", active_usage=usage)
    before = menubar.build_menu_model(snapshot, settings, threshold=90, now=_NOW)
    after = menubar.build_menu_model(snapshot, settings, threshold=90, now=_NOW + 3600)
    assert before.title == "⇄ 95%"
    assert after.title == "⇄ 0%"


class _ExplodingItem:
    """A menu item that refuses to be updated, to fail an in-place pass midway."""

    _menuitem = object()

    def __setattr__(self, name, value):
        raise RuntimeError("simulated failure mid-apply")


def test_failed_in_place_update_recovers_on_the_next_tick(monkeypatch, tmp_path: Path):
    """An in-place pass dies partway, leaving rows half-updated.

    No model describes that mixed state, so `_model` must not survive it --
    otherwise the next tick takes the in-place path again and the menu stays
    wrong forever. Clearing it forces a rebuild, which is the only way back to
    a known state.
    """
    app, _switcher, _registry = _build_app(monkeypatch, tmp_path)
    app.snapshot = _snap(("1", 10.0, False))
    app.rebuild_menu()
    good_history = app._items["history"]
    app._items["history"] = [_ExplodingItem()]

    app.snapshot = _snap(("1", 55.0, False))
    with pytest.raises(RuntimeError):
        app.sync_menu()
    assert app._model is None

    app._items["history"] = good_history
    app.sync_menu()
    assert app._model is not None
    assert "5h 55%" in app._items["accounts"][0].title
