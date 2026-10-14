"""Tests for the menu bar's usage-bar panel model and geometry.

Pure layer only: no rumps, no AppKit. The model is built from the same
snapshot rows the text menu uses, through the same helpers (live countdown,
weekly roll-forward, pace), so both surfaces always agree.
"""

from __future__ import annotations

import datetime as _dt
import math

import pytest

from claude_swap import menubar, menubar_panel as mp
from claude_swap.tui.theme import CRIT_PCT, WARN_PCT

_NOW = 1_000_000.0


def _iso(delta_s):  # ISO-8601 for _NOW + delta_s, UTC
    return _dt.datetime.fromtimestamp(_NOW + delta_s, _dt.timezone.utc).isoformat()


def _entry(usage, *, num=1, email="a@x.com", active=False, alias=None,
           disabled=False, fetched_at=None):
    """A snapshot account row: (num, email, is_active, display, last_good,
    alias, disabled, fetched_at), as ``menubar._adapt_snapshot`` builds it."""
    last_good = usage if isinstance(usage, dict) else None
    return (num, email, active, usage, last_good, alias, disabled, fetched_at)


# --- usage state (the TUI's severity bands) ---------------------------------------

def test_state_bands_follow_the_tui_thresholds():
    assert mp.usage_state(None) == "unknown"
    assert mp.usage_state(0.0) == "ok"
    assert mp.usage_state(WARN_PCT - 0.01) == "ok"
    assert mp.usage_state(WARN_PCT) == "warn"
    assert mp.usage_state(CRIT_PCT - 0.01) == "warn"
    assert mp.usage_state(CRIT_PCT) == "hot"
    assert mp.usage_state(99.99) == "hot"
    assert mp.usage_state(100.0) == "over"
    assert mp.usage_state(130.0) == "over"


def test_state_thresholds_are_the_tui_constants():
    # Reused, not re-derived: the bar colour and the TUI bar colour agree.
    assert (mp.WARN_PCT, mp.CRIT_PCT) == (WARN_PCT, CRIT_PCT) == (70.0, 90.0)


# --- window rows ---------------------------------------------------------------

def test_rows_for_all_windows_in_text_order():
    usage = {
        "five_hour": {"pct": 46.0, "resets_at": _iso(3 * 3600 + 43 * 60)},
        "seven_day": {"pct": 73.0, "resets_at": _iso(4 * 86400 + 22 * 3600)},
        "scoped": [{"name": "Fable", "pct": 85.0, "resets_at": _iso(4 * 86400 + 22 * 3600)}],
        "spend": {"pct": 30.0},
    }
    rows = mp.window_rows(usage, _NOW, fetched_at=None)
    assert [r.label for r in rows] == ["5h", "7d", "Fable", "$"]
    five, seven, fable, spend = rows
    assert five == mp.WindowRow("5h", 0.46, "46%", "3h 43m", "ok", None, "")
    assert (seven.pct_text, seven.reset_text, seven.state) == ("73%", "4d 22h", "warn")
    assert (fable.pct_text, fable.state) == ("85%", "warn")
    assert (spend.pct_text, spend.reset_text, spend.state, spend.pace_fraction) == ("30%", "", "ok", None)


def test_rows_match_the_text_summary_numbers():
    # Same fixture the text row uses; the bars must show the same numbers.
    usage = {
        "five_hour": {"pct": 42.0, "resets_at": _iso(2 * 3600 + 33 * 60)},
        "seven_day": {"pct": 18.0, "resets_at": _iso(86400 + 19 * 3600)},
        "spend": {"pct": 30.0},
    }
    text = menubar.usage_summary(usage, _NOW)
    assert text == "5h 42% (2h 33m) · 7d 18% (1d 19h) · $ 30%"
    rows = mp.window_rows(usage, _NOW, fetched_at=None)
    assert [(r.label, r.pct_text, r.reset_text) for r in rows] == [
        ("5h", "42%", "2h 33m"), ("7d", "18%", "1d 19h"), ("$", "30%", ""),
    ]


def test_partial_windows_only_produce_their_rows():
    rows = mp.window_rows({"five_hour": {"pct": 5.0}}, _NOW, fetched_at=None)
    assert [(r.label, r.pct_text, r.reset_text) for r in rows] == [("5h", "5%", "")]


def test_scoped_without_name_or_pct_is_skipped_like_the_text():
    usage = {"scoped": [{"name": "Fable"}, {"pct": 3.0}, {"name": "Opus", "pct": 55.0}]}
    assert [r.label for r in mp.window_rows(usage, _NOW, None)] == ["Opus"]


def test_over_limit_is_red_with_the_marker_and_a_full_bar():
    usage = {"scoped": [{"name": "Fable", "pct": 104.0}], "five_hour": {"pct": 100.0}}
    five, fable = mp.window_rows(usage, _NOW, None)
    for row in (five, fable):
        assert row.state == "over"
        assert row.note == "(!)"
        assert row.fraction == 1.0
    assert fable.pct_text == "104%"


def test_fraction_is_clamped_to_the_bar():
    usage = {"five_hour": {"pct": -5.0}, "seven_day": {"pct": 250.0}}
    five, seven = mp.window_rows(usage, _NOW, None)
    assert five.fraction == 0.0 and seven.fraction == 1.0


def test_sentinel_string_is_one_unknown_row_with_the_text_wording():
    rows = mp.window_rows("token expired", _NOW, None)
    assert rows == [mp.WindowRow("", 0.0, "", "", "unknown", None, "token expired")]


def test_none_and_empty_usage_read_usage_unavailable():
    expected = [mp.WindowRow("", 0.0, "", "", "unknown", None, "usage unavailable")]
    assert mp.window_rows(None, _NOW, None) == expected
    assert mp.window_rows({}, _NOW, None) == expected
    assert mp.window_rows({"five_hour": {"pct": "n/a"}}, _NOW, None) == expected


# --- pace tick -------------------------------------------------------------------

def test_pace_tick_sits_at_the_elapsed_fraction_of_the_week():
    # One day into the week (reset in 6 days), measured now.
    usage = {"seven_day": {"pct": 50.0, "resets_at": _iso(6 * 86400)}}
    (row,) = mp.window_rows(usage, _NOW, fetched_at=_NOW)
    assert row.pace_fraction == pytest.approx(1 / 7)


def test_pace_tick_on_scoped_windows_too():
    usage = {"scoped": [{"name": "Fable", "pct": 10.0, "resets_at": _iso(2 * 86400)}]}
    (row,) = mp.window_rows(usage, _NOW, fetched_at=_NOW)
    assert row.pace_fraction == pytest.approx(5 / 7)


def test_no_pace_tick_on_the_five_hour_window():
    usage = {"five_hour": {"pct": 90.0, "resets_at": _iso(4 * 3600)}}
    (row,) = mp.window_rows(usage, _NOW, fetched_at=_NOW)
    assert row.pace_fraction is None


def test_no_pace_tick_without_fetched_at_or_resets_at():
    usage = {"seven_day": {"pct": 50.0, "resets_at": _iso(6 * 86400)}}
    assert mp.window_rows(usage, _NOW, fetched_at=None)[0].pace_fraction is None
    assert mp.window_rows({"seven_day": {"pct": 50.0}}, _NOW, _NOW)[0].pace_fraction is None


def test_no_pace_tick_right_after_a_reset():
    # pace.py suppresses the first day after a reset; so does the tick.
    usage = {"seven_day": {"pct": 5.0, "resets_at": _iso(6.5 * 86400)}}
    assert mp.window_rows(usage, _NOW, fetched_at=_NOW)[0].pace_fraction is None


def test_window_rolled_to_zero_shows_zero_and_no_stale_tick():
    usage = {
        "seven_day": {"pct": 95.0, "resets_at": _iso(-3 * 86400)},
        "scoped": [{"name": "Fable", "pct": 95.0, "resets_at": _iso(-3 * 86400)}],
    }
    seven, fable = mp.window_rows(usage, _NOW, fetched_at=_NOW - 4 * 86400)
    for row in (seven, fable):
        assert (row.pct_text, row.fraction, row.state) == ("0%", 0.0, "ok")
        assert row.reset_text == "4d 0h"  # the rolled, next boundary
        # The reading belongs to the cycle that ended; its elapsed share says
        # nothing about the new one, so there is no tick at all.
        assert row.pace_fraction is None
        assert row.ahead is False


def test_unrolled_window_keeps_its_tick_and_ahead_flag():
    usage = {"seven_day": {"pct": 50.0, "resets_at": _iso(6 * 86400)}}
    (row,) = mp.window_rows(usage, _NOW, fetched_at=_NOW)
    assert row.pace_fraction == pytest.approx(1 / 7)
    assert row.ahead is True  # the text row shows "(ahead)" for this reading


def test_ahead_flag_follows_pace_like_the_text_row():
    usage = {"seven_day": {"pct": 20.0, "resets_at": _iso(3 * 86400)}}
    (row,) = mp.window_rows(usage, _NOW, fetched_at=_NOW)
    assert row.pace_fraction is not None
    assert row.ahead is False
    assert "(ahead)" not in menubar.usage_summary(usage, _NOW, fetched_at=_NOW)


# --- account panel -----------------------------------------------------------------

def test_panel_header_for_the_active_account_with_age():
    usage = {"five_hour": {"pct": 46.0}}
    panel = mp.build_account_panel(
        _entry(usage, num=3, email="me@x.com", active=True, fetched_at=_NOW - 120), _NOW
    )
    assert (panel.num, panel.title, panel.is_active, panel.disabled) == (3, "me@x.com", True, False)
    assert panel.meta_text == "active · 2m ago"
    assert [r.label for r in panel.rows] == ["5h"]


def test_panel_title_is_the_text_rows_identity_with_alias():
    usage = {"five_hour": {"pct": 1.0}}
    panel = mp.build_account_panel(_entry(usage, num=4, alias="work", disabled=True), _NOW)
    assert panel.title == "work  (a@x.com)"
    assert panel.email == "a@x.com"
    assert panel.meta_text == "disabled"
    text = menubar.format_account_label(4, "a@x.com", usage, _NOW, alias="work", disabled=True)
    assert text.startswith(f"4  {panel.title}")


@pytest.mark.parametrize("alias", [None, "work", "a very long alias name"])
def test_panel_title_matches_format_account_label(alias):
    usage = {"five_hour": {"pct": 1.0}}
    panel = mp.build_account_panel(_entry(usage, num=7, email="me@x.com", alias=alias), _NOW)
    text = menubar.format_account_label(7, "me@x.com", usage, _NOW, alias=alias)
    assert text.startswith(f"7  {panel.title}  ")
    assert panel.title == menubar.account_identity("me@x.com", alias)


def test_account_identity_is_the_text_rows_own_helper():
    assert menubar.account_identity("me@x.com", None) == "me@x.com"
    assert menubar.account_identity("me@x.com", "work") == "work  (me@x.com)"


def test_panel_meta_is_empty_without_state_or_age():
    panel = mp.build_account_panel(_entry({"five_hour": {"pct": 1.0}}), _NOW)
    assert panel.meta_text == ""


def test_panel_age_never_negative():
    panel = mp.build_account_panel(_entry({}, fetched_at=_NOW + 30), _NOW)
    assert panel.meta_text == "0s ago"


def test_panel_rows_use_the_display_usage_for_sentinels():
    entry = (2, "b@x.com", False, "token expired", {"five_hour": {"pct": 9.0}}, None, False, None)
    panel = mp.build_account_panel(entry, _NOW)
    assert [r.note for r in panel.rows] == ["token expired"]


def test_panel_accessibility_label_is_the_text_row():
    usage = {"five_hour": {"pct": 42.0}}
    entry = _entry(usage, num=4, alias="w", disabled=True)
    panel = mp.build_account_panel(entry, _NOW)
    assert panel.text_label == menubar.format_account_label(
        4, "a@x.com", usage, _NOW, alias="w", disabled=True, fetched_at=None
    )


# --- geometry ------------------------------------------------------------------------

def test_line_height_from_font_metrics():
    assert mp.line_height(ascender=10.2, descender=-2.6, leading=0.0) == 13.0
    assert mp.line_height(ascender=10.0, descender=-3.0, leading=1.0) == 14.0


def test_columns_fill_the_fixed_width_between_the_insets():
    cols = mp.row_columns(label_w=30.0, pct_w=28.0, reset_w=44.0)
    assert cols.label_x == mp.LEFT_INSET + mp.DOT_COLUMN
    assert cols.reset_right == mp.PANEL_WIDTH - mp.RIGHT_INSET
    assert cols.pct_right == cols.reset_right - 44.0 - mp.RESET_GAP
    assert cols.bar_x == cols.label_x + 30.0 + mp.LABEL_GAP
    assert cols.bar_x + cols.bar_w == cols.pct_right - 28.0 - mp.PCT_GAP
    assert cols.note_right is None


def test_columns_reserve_the_note_between_bar_and_percent():
    plain = mp.row_columns(label_w=30.0, pct_w=28.0, reset_w=44.0)
    noted = mp.row_columns(label_w=30.0, pct_w=28.0, reset_w=44.0, note_w=16.0)
    assert noted.note_right == noted.pct_right - 28.0 - mp.NOTE_GAP
    assert noted.bar_x + noted.bar_w == noted.note_right - 16.0 - mp.PCT_GAP
    assert noted.bar_w == plain.bar_w - 16.0 - mp.NOTE_GAP


def test_bar_width_never_negative():
    cols = mp.row_columns(label_w=400.0, pct_w=28.0, reset_w=44.0)
    assert cols.bar_w == 0.0


def test_bar_rect_is_vertically_centred_and_six_points_tall():
    cols = mp.row_columns(label_w=30.0, pct_w=28.0, reset_w=44.0)
    x, y, w, h = mp.bar_rect(cols, row_top=40.0, row_height=14.0)
    assert mp.BAR_HEIGHT == 6.0
    assert (x, w, h) == (cols.bar_x, cols.bar_w, 6.0)
    assert y == 40.0 + (14.0 - 6.0) / 2


@pytest.mark.parametrize(
    "fraction, expected",
    [(0.0, 0.0), (-1.0, 0.0), (0.5, 50.0), (1.0, 100.0), (2.0, 100.0), (0.01, 6.0)],
)
def test_fill_width(fraction, expected):
    # A sliver of usage still shows a full round cap (bar height) rather than
    # a sub-radius blob; zero shows nothing.
    assert mp.fill_width(100.0, fraction) == expected


@pytest.mark.parametrize(
    "fraction, centre",
    [(0.5, 50.0), (1 / 7, 100 / 7), (0.25, 25.0)],
)
def test_tick_is_centred_on_the_elapsed_fraction(fraction, centre):
    cols = mp.Columns(label_x=0, label_w=0, bar_x=10.0, bar_w=100.0, note_right=None,
                      pct_right=0, reset_right=0)
    left = mp.tick_x(cols, fraction)
    assert left + mp.TICK_WIDTH / 2 == pytest.approx(10.0 + centre)


@pytest.mark.parametrize("fraction, left", [(0.0, 10.0), (-0.3, 10.0), (1.0, 108.5), (1.4, 108.5)])
def test_tick_stays_inside_the_bar(fraction, left):
    cols = mp.Columns(label_x=0, label_w=0, bar_x=10.0, bar_w=100.0, note_right=None,
                      pct_right=0, reset_right=0)
    assert mp.tick_x(cols, fraction) == pytest.approx(left)
    assert mp.TICK_WIDTH == 1.5


def test_panel_height_adds_header_rows_and_padding():
    h = mp.panel_height(n_rows=3, header_h=17.0, row_h=14.0)
    assert h == mp.TOP_PAD + 17.0 + mp.HEADER_GAP + 3 * 14.0 + mp.BOTTOM_PAD
    assert h == math.ceil(h)


def test_fixed_panel_constants():
    assert (mp.PANEL_WIDTH, mp.LEFT_INSET, mp.RIGHT_INSET) == (340.0, 14.0, 12.0)


# --- setting ---------------------------------------------------------------------------

def test_show_usage_bars_defaults_on():
    assert menubar.MenuBarSettings().show_usage_bars is True


def test_show_usage_bars_persists(tmp_path):
    path = tmp_path / "menubar_settings.json"
    menubar.MenuBarSettings(show_usage_bars=False).save(path)
    assert menubar.MenuBarSettings.load(path).show_usage_bars is False


def test_show_usage_bars_missing_from_old_file_defaults_on(tmp_path):
    path = tmp_path / "menubar_settings.json"
    path.write_text('{"show_account_name": false}')
    assert menubar.MenuBarSettings.load(path).show_usage_bars is True


def test_tick_rect_overhangs_the_bar_evenly():
    cols = mp.Columns(label_x=0, label_w=0, bar_x=10.0, bar_w=100.0, note_right=None,
                      pct_right=0, reset_right=0)
    x, y, w, h = mp.tick_rect(cols, 0.5, bar_y=20.0)
    assert (x, w) == (mp.tick_x(cols, 0.5), mp.TICK_WIDTH)
    assert y == 20.0 - mp.TICK_OVERHANG
    assert h == mp.BAR_HEIGHT + 2 * mp.TICK_OVERHANG
    assert mp.TICK_OVERHANG == 2.0


# --- header geometry: long names truncate, the number never goes ----------------

def test_header_name_span_leaves_the_number_and_the_meta_alone():
    cols = mp.row_columns(label_w=30.0, pct_w=28.0, reset_w=44.0, width=340.0)
    name_x, name_w = mp.header_name_span(cols, num_w=8.0, meta_w=80.0)
    assert name_x == cols.label_x + 8.0 + mp.NUM_GAP
    assert name_x + name_w == cols.reset_right - 80.0 - mp.META_GAP


def test_header_name_span_without_meta_runs_to_the_right_edge():
    cols = mp.row_columns(label_w=30.0, pct_w=28.0, reset_w=44.0, width=340.0)
    name_x, name_w = mp.header_name_span(cols, num_w=8.0, meta_w=0.0)
    assert name_x + name_w == cols.reset_right


def test_header_name_span_never_negative():
    cols = mp.row_columns(label_w=30.0, pct_w=28.0, reset_w=44.0, width=340.0)
    _x, name_w = mp.header_name_span(cols, num_w=8.0, meta_w=400.0)
    assert name_w == 0.0


def test_very_long_identity_is_kept_whole_in_the_model():
    email = "an.extremely.long.address.for.testing.truncation@some-very-long-domain.example.com"
    panel = mp.build_account_panel(_entry({"five_hour": {"pct": 1.0}}, num=12, email=email, alias="work"), _NOW)
    # The view truncates in the middle at draw time; the model keeps the text.
    assert panel.title == f"work  ({email})"
    assert panel.num == 12


# --- accessibility ------------------------------------------------------------------

@pytest.mark.parametrize(
    "text, spoken",
    [
        ("3h 43m", "3 hours 43 minutes"),
        ("4d 22h", "4 days 22 hours"),
        ("4d 0h", "4 days"),
        ("1d 1h", "1 day 1 hour"),
        ("34m", "34 minutes"),
        ("1m", "1 minute"),
        ("45s", "45 seconds"),
        ("2h", "2 hours"),
    ],
)
def test_spoken_duration(text, spoken):
    assert mp.spoken_duration(text) == spoken


def test_accessibility_label_reads_identity_state_and_each_window():
    usage = {
        "five_hour": {"pct": 46.0, "resets_at": _iso(3 * 3600 + 43 * 60)},
        "seven_day": {"pct": 50.0, "resets_at": _iso(5.5 * 86400)},  # 1.5 days in
        "scoped": [{"name": "Fable", "pct": 104.0}],
        "spend": {"pct": 30.0},
    }
    panel = mp.build_account_panel(
        _entry(usage, num=3, email="me@x.com", alias="work", active=True, fetched_at=_NOW - 120), _NOW
    )
    assert mp.accessibility_label(panel) == (
        "Account 3, work  (me@x.com), active, 2 minutes ago. "
        "5 hour, 46 percent, resets in 3 hours 43 minutes. "
        "7 day, 50 percent, ahead of pace, resets in 5 days 12 hours. "
        "Fable, 104 percent, over limit. "
        "Spend, 30 percent."
    )


def test_accessibility_label_for_a_sentinel_row():
    panel = mp.build_account_panel(_entry("token expired", num=2, disabled=True), _NOW)
    assert mp.accessibility_label(panel) == "Account 2, a@x.com, disabled. token expired."


def test_a_wider_row_still_builds_its_panel():
    # Rows may grow (another change adds login expiry and quarantine as
    # fields 9 and 10): the panel reads the fields it knows and ignores the rest.
    usage = {"five_hour": {"pct": 40.0}}
    panel = mp.build_account_panel(
        _entry(usage, num=3, alias="work") + (1_900_000_000.0, False), _NOW
    )
    assert str(panel.num) == "3" and "work" in panel.title


# --- names only --------------------------------------------------------------------

def test_account_identity_names_only_uses_the_alias_alone():
    assert menubar.account_identity("me@x.com", "work", names_only=True) == "work"
    assert menubar.account_identity("me@x.com", None, names_only=True) == "me@x.com"
    assert menubar.account_identity("me@x.com", "work") == "work  (me@x.com)"  # default unchanged


def test_format_account_label_names_only():
    usage = {"five_hour": {"pct": 1.0}}
    assert menubar.format_account_label(4, "me@x.com", usage, _NOW, alias="work", names_only=True) == "4  work  5h 1%"
    assert menubar.format_account_label(4, "me@x.com", usage, _NOW, alias=None, names_only=True) == "4  me@x.com  5h 1%"


def test_panel_names_only_title_and_text_row_follow_the_setting():
    usage = {"five_hour": {"pct": 1.0}}
    entry = _entry(usage, num=4, email="me@x.com", alias="work")
    panel = mp.build_account_panel(entry, _NOW, names_only=True)
    assert panel.title == "work"
    assert panel.text_label == menubar.format_account_label(4, "me@x.com", usage, _NOW, alias="work", names_only=True)
    assert mp.build_account_panel(entry, _NOW).title == "work  (me@x.com)"


def test_accessibility_label_always_includes_the_email():
    entry = _entry({"five_hour": {"pct": 1.0}}, num=4, email="me@x.com", alias="work")
    for names_only in (False, True):
        label = mp.accessibility_label(mp.build_account_panel(entry, _NOW, names_only=names_only))
        assert label.startswith("Account 4, work  (me@x.com)")


def test_show_names_only_defaults_off_and_persists(tmp_path):
    assert menubar.MenuBarSettings().show_names_only is False
    path = tmp_path / "menubar_settings.json"
    menubar.MenuBarSettings(show_names_only=True).save(path)
    assert menubar.MenuBarSettings.load(path).show_names_only is True


def test_title_prefers_the_alias():
    s = menubar.MenuBarSettings(show_account_name=True, title_pct="off")
    assert menubar.format_title("longname@x.com", {"five_hour": {"pct": 5.0}}, s, alias="work") == "⇄ work"
    assert menubar.format_title("longname@x.com", {"five_hour": {"pct": 5.0}}, s) == "⇄ longname"
