"""Tests for the ``cswap history`` views (history_render.py), the demo data
behind them, and the command itself.

The snapshot tests render the demo history at a fixed clock, width and time
zone and compare the plain text with ``tests/fixtures/history/*.txt``. After a
deliberate change to the drawing, regenerate them with::

    CSWAP_UPDATE_SNAPSHOTS=1 uv run pytest tests/test_history_render.py

and read the diff: it is the picture.
"""

from __future__ import annotations

import io
import json
import os
import sys
from datetime import timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from rich.console import Console

from claude_swap import cli, printer
from claude_swap.history import Account, Current, History
from claude_swap.history_demo import demo_history
from claude_swap.history_forecast import analyse
from claude_swap.history_render import (
    Canvas,
    Layer,
    dur,
    overview_json,
    render_overview,
    time_ticks,
    when,
    worth,
)

SNAPSHOTS = Path(__file__).parent / "fixtures" / "history"
NOW = 1_793_700_000.0  # Tue 2026-11-03T10:00:00Z
UTC = timezone.utc


def plain(lines, width: int) -> str:
    """The lines as the terminal shows them, colour stripped. Nothing is
    cropped: a line too wide for ``width`` stays too wide, for the width
    tests to catch."""
    buf = io.StringIO()
    console = Console(file=buf, width=10_000, color_system=None, highlight=False)
    for line in lines:
        console.print(line, no_wrap=True, crop=False)
    return "\n".join(line.rstrip() for line in buf.getvalue().splitlines()) + "\n"


def widest(lines) -> int:
    return max(len(line.plain) for line in lines)


def check_snapshot(name: str, text: str) -> None:
    path = SNAPSHOTS / name
    if os.environ.get("CSWAP_UPDATE_SNAPSHOTS"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert text == path.read_text(encoding="utf-8"), f"{name} changed; see the module docstring"


@pytest.fixture(scope="module")
def demo():
    return demo_history(NOW)


class TestSnapshots:
    def test_overview_braille_100_columns(self, demo):
        # Braille is drawn with colour on (the styles are stripped here).
        lines = render_overview(analyse(demo), days=7, width=100, unicode=True, tz=UTC, color=True)
        assert widest(lines) <= 100
        check_snapshot("overview-braille-100.txt", plain(lines, 100))

    def test_overview_ascii_80_columns(self, demo):
        lines = render_overview(analyse(demo), days=7, width=80, unicode=False, tz=UTC, color=False)
        text = plain(lines, 80)
        text.encode("ascii")  # nothing outside ASCII
        assert widest(lines) <= 80
        check_snapshot("overview-ascii-80.txt", text)

    def test_overview_ascii_50_columns(self, demo):
        lines = render_overview(analyse(demo), days=7, width=50, unicode=False, tz=UTC, color=False)
        assert widest(lines) <= 50
        check_snapshot("overview-ascii-50.txt", plain(lines, 50))

    @pytest.mark.parametrize("width", [50, 60, 64, 72, 77, 78, 80, 100, 120, 200])
    def test_every_width_from_50_to_200_fits(self, demo, width):
        a = analyse(demo)
        for unicode in (True, False):
            for color in (True, False):
                lines = render_overview(a, days=30, width=width, unicode=unicode, tz=UTC, color=color)
                assert widest(lines) <= width, (width, unicode, color)

    @pytest.mark.parametrize("width", range(50, 80))
    def test_account_rows_fit_with_the_longest_reset_labels(self, width):
        """A reset 10-23 hours out reads "22h 40m": the widest label a row carries."""
        h = History(
            now=NOW, accounts=[Account(n) for n in (1, 2, 13)],
            readings={}, active=1,
            current={
                1: Current(t=NOW - 60, d7=41, h5=41, d7_reset=NOW + 22 * 3600 + 40 * 60),
                2: Current(t=NOW - 60, d7=100, h5=100, d7_reset=NOW + 6 * 86400 + 18 * 3600),
                13: Current(t=NOW - 60, d7=5, h5=0, d7_reset=NOW + 9 * 3600 + 5 * 60),
            },
        )
        for unicode in (True, False):
            lines = render_overview(analyse(h), days=7, width=width, unicode=unicode, tz=UTC)
            assert widest(lines) <= width, (width, unicode)
            assert "22h 40m" in plain(lines, width)

    def test_without_colour_the_paces_draw_distinct_marks(self, demo):
        text = plain(render_overview(analyse(demo), days=7, width=100, unicode=True, tz=UTC, color=False), 100)
        chart = text.split("now")[1].split("room left")[0]
        assert not any(0x2800 <= ord(ch) <= 0x28FF for ch in chart)  # no braille
        for mark in "xo+*":
            assert mark in chart
        assert "xx 30m" in text and "oo 6h" in text and "++ 24h" in text  # the legend says which

    def test_colour_only_shades_with_colour(self, demo):
        a = analyse(demo)
        shaded = plain(render_overview(a, days=7, width=100, unicode=True, tz=UTC, color=True), 100)
        flat = plain(render_overview(a, days=7, width=100, unicode=True, tz=UTC, color=False), 100)
        assert shaded != flat  # the area under the room line
        assert shaded.count("⢕") > flat.count("⢕")


class TestPieces:
    def test_worth_shows_small_amounts(self):
        assert [worth(v) for v in (None, 0, 0.456, 1, 12.34)] == ["-", "0.0", "0.46", "1.0", "12.3"]

    def test_dur(self):
        assert [dur(s) for s in (59, 3600, 3 * 3600 + 120, 2 * 86400 + 3600)] == ["0m", "1h 0m", "3h 2m", "2d 1h"]

    def test_when_is_portable(self):
        assert when(NOW, UTC) == "Tue Nov 3, 10:00 AM"
        assert when(NOW + 3 * 3600 + 300, UTC) == "Tue Nov 3, 1:05 PM"
        assert when(NOW - 10 * 3600, UTC) == "Tue Nov 3, 12:00 AM"

    def test_time_ticks_pick_round_days_or_hours(self):
        days = time_ticks(NOW - 7 * 86400, NOW + 7 * 86400, 8, UTC)
        assert [label for _, label in days] == ["Oct 29", "Oct 31", "Nov 2", "Nov 4", "Nov 6", "Nov 8", "Nov 10"]
        hours = time_ticks(NOW - 12 * 3600, NOW, 6, UTC)
        # every 2 hours; midnight carries the date
        assert [label for _, label in hours] == ["10pm", "Nov 3", "2am", "4am", "6am", "8am", "10am"]
        assert all(t % 3600 == 0 for t, _ in hours)

    def test_crossing_lines_keep_both_dots_and_the_higher_colour(self):
        c = Canvas(2, 1, unicode=True)
        low, high = Layer("a", "red", 1), Layer("b", "blue", 5)
        c.line(0, 0, 3, 0, low)
        c.line(0, 3, 3, 0, high)
        cells = c.text_rows()[0]
        assert cells.plain[0] == chr(0x2800 | 0x01 | 0x08 | 0x40 | 0x04 | 0x02 | 0x10) or cells.plain[0] != " "
        assert all(span.style == "blue" for span in cells.spans)

    def test_background_only_shows_where_no_line_is(self):
        c = Canvas(2, 1, unicode=True)
        c.dot(0, 0, Layer("bg", "grey", 0, background=True))
        c.dot(1, 0, Layer("bg", "grey", 0, background=True))
        c.dot(2, 0, Layer("line", "blue", 5))
        cells = c.text_rows()[0]
        assert cells.plain == "⠉⠁"  # cell 0: both bg dots; cell 1: the line's only

    def test_dash_pattern_runs_on_across_segments(self):
        c = Canvas(10, 1, unicode=True)
        dashed = Layer("d", "x", 1, pattern=(2, 2))
        c.polyline([(0, 0), (9, 0), (19, 0)], dashed)
        assert c.text_rows()[0].plain == "⠉ " * 5

    def test_more_accounts_wording(self):
        from claude_swap.history_render import more_accounts_words

        assert [more_accounts_words(n) for n in (None, 0, 1, 3)] == [
            "-", "0 (sustainable)", "+1 account", "+3 accounts"]

    def test_pace_table_shows_more_accounts_per_pace_and_0_for_sustainable(self, demo):
        text = plain(render_overview(analyse(demo), days=7, width=120, unicode=False, tz=UTC), 120)
        head = next(line for line in text.splitlines() if line.lstrip().startswith("Pace"))
        assert "More accounts" in head
        payload = overview_json(analyse(demo), days=7)
        for f in payload["forecasts"]:
            if f["available"]:
                n = f["extraAccountsToKeepUp"]
                want = "0 (sustainable)" if n == 0 else f"+{n} account" + ("s" if n != 1 else "")
                assert any(want in line for line in text.splitlines() if "Last" in line), want
        sustainable = next(line for line in text.splitlines() if "Sustainable (" in line)
        assert sustainable.split()[4] == "0"  # Sustainable (14 accounts) 2.0 0 ...

class TestViews:
    def test_overview_with_no_readings_yet(self):
        h = History(now=NOW, accounts=[Account(1)], readings={}, current={}, active=1)
        text = plain(render_overview(analyse(h), days=7, width=90, unicode=False, tz=UTC), 90)
        assert "No current reading to total" in text
        assert "No current readings to start from" in text
        assert "no reading yet" in text

    def test_overview_says_why_paces_are_missing(self):
        h = History(
            now=NOW, accounts=[Account(1)], readings={},
            current={1: Current(t=NOW - 60, d7=50, d7_reset=NOW + 86400)},
            active=1, recording_since=NOW - 3 * 3600,
        )
        text = plain(render_overview(analyse(h), days=7, width=90, unicode=True, tz=UTC), 90)
        assert "After 21h 1m more of history" in text  # the stretch ends at the last check
        assert "0.50 of 1 account's worth left" in text
        assert "0.50 more comes back in the next 24 hours as #1 resets" in text

    def test_overview_json(self, demo):
        payload = overview_json(analyse(demo), days=7)
        assert payload["schemaVersion"] == 1 and payload["demo"] is True
        assert payload["accounts"] == 15 and payload["countedAccounts"] == 14
        assert [f["pace"] for f in payload["forecasts"]] == ["30m", "6h", "24h"]
        ok = [f for f in payload["forecasts"] if f["available"]]
        assert ok and all(f["path"][0]["at"] == payload["generatedAt"] for f in ok)
        assert payload["room"][-1]["room"] == payload["roomNow"]
        refill = [r["room"] for r in payload["refill"]]
        assert refill[0] == payload["roomNow"] and refill == sorted(refill)
        assert len(refill) == len(payload["resets"]) + 1
        assert len(payload["room"]) <= 500
        json.dumps(payload)  # serialisable: no inf, no datetimes

class TestDemo:
    def test_is_deterministic_and_complete(self):
        a, b = demo_history(NOW), demo_history(NOW)
        assert a.readings == b.readings and a.active == b.active
        assert len(a.accounts) == 15
        assert a.current[6].error == "http-403"
        for rows in a.readings.values():
            assert all(r.prev is None or r.prev < r.t for r in rows)


def run_cli(argv, capsys, env=None):
    env = env or {}
    with patch("os.geteuid", return_value=1000, create=True), \
         patch.object(sys, "argv", ["cswap", "history", *argv]), \
         patch.dict(os.environ, env):
        code = 0
        try:
            cli.main()
        except SystemExit as e:
            code = e.code or 0
    out = capsys.readouterr()
    return code, out.out, out.err


class TestCommand:
    def test_demo_draws_the_chart(self, temp_home, capsys):
        code, out, _ = run_cli(["--demo", "--width", "90"], capsys)
        assert code == 0
        assert "Weekly room left" in out and "Last 24 hours" in out
        assert "\x1b[" not in out  # captured stdout is not a terminal

    def test_force_color_colours_and_no_color_wins(self, temp_home, capsys, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        # conftest pins TERM=dumb, which (like printer) rich reads as "no colour"
        term = {"TERM": "xterm-256color"}
        code, out, _ = run_cli(["--demo", "--width", "90"], capsys, {"FORCE_COLOR": "1", **term})
        assert code == 0 and "\x1b[" in out
        printer._colors_enabled = None
        code, out, _ = run_cli(["--demo", "--width", "90"], capsys, {"FORCE_COLOR": "1", "NO_COLOR": "1", **term})
        assert code == 0 and "\x1b[" not in out

    def test_ascii_when_the_terminal_cannot_show_braille(self, temp_home, capsys, monkeypatch):
        monkeypatch.setattr(printer, "_native_stdout_encoding", "ascii")
        code, out, _ = run_cli(["--demo", "--width", "90"], capsys)
        assert code == 0
        out.encode("ascii")

    def test_json(self, temp_home, capsys):
        code, out, _ = run_cli(["--demo", "--json"], capsys)
        assert code == 0
        assert json.loads(out)["schemaVersion"] == 1

    @pytest.mark.parametrize("argv", [["--days", "0"], ["--days", "5000"], ["--width", "20"]])
    def test_bad_options(self, temp_home, capsys, argv):
        code, _, err = run_cli(argv, capsys)
        assert code == 2 and "must be" in err

    def test_a_terminal_under_50_columns_is_refused_clearly(self, temp_home, capsys):
        import shutil

        with patch.object(shutil, "get_terminal_size", return_value=os.terminal_size((40, 24))):
            code, out, err = run_cli(["--demo"], capsys)
        assert code == 1 and "at least 50 columns" in err and "has 40" in err
        assert out == ""

    def test_no_accounts(self, temp_home, capsys):
        code, out, _ = run_cli([], capsys)
        assert code == 0 and "No accounts yet" in out

    def test_real_history_and_the_off_note(self, temp_home, capsys):
        from claude_swap.switcher import ClaudeAccountSwitcher

        sw = ClaudeAccountSwitcher()
        sw.sequence_file.parent.mkdir(parents=True, exist_ok=True)
        sw.sequence_file.write_text(json.dumps({
            "activeAccountNumber": 1, "sequence": [1, 2],
            "accounts": {"1": {"email": "a@x.com", "uuid": "u1"}, "2": {"email": "b@x.com", "uuid": "u2"}},
        }))
        (sw.backup_dir / "settings.json").write_text(json.dumps({"history": {"enabled": False}}))
        code, out, _ = run_cli(["--width", "90"], capsys)
        assert code == 0
        assert "Weekly room left" in out
        assert "Recording is off" in out
        assert "a@x.com" not in out
