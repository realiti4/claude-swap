"""Tests for the HTML history report (history_report.py, history_report.html)."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from importlib import resources
from unittest.mock import patch

import pytest

from claude_swap import cli, history_report
from claude_swap.history import Account, Current, History, Reading
from claude_swap.history_demo import demo_history
from claude_swap.history_forecast import analyse
from claude_swap.history_render import overview_json
from claude_swap.history_report import (
    DATA_PLACEHOLDER,
    DEMO_PLACEHOLDER,
    FAILED_MARKER,
    LIVE_MIN_INTERVAL_S,
    TEMPLATE_NAME,
    default_report_path,
    refresh_live_report,
    render_report,
    report_payload,
    write_report,
)

NOW = 1_793_700_000.0


def template() -> str:
    return resources.files("claude_swap").joinpath(TEMPLATE_NAME).read_text(encoding="utf-8")


def payloads(html: str) -> tuple[object, object]:
    """The two JSON payloads embedded in a built page."""
    found = re.findall(r'<script id="(payload|demo-payload)" type="application/json">(.*?)</script>', html, re.S)
    by = {name: json.loads(body) for name, body in found}
    return by["payload"], by["demo-payload"]


class TestTemplate:
    def test_ships_as_package_data_with_both_placeholders(self):
        text = template()
        assert text.count(DATA_PLACEHOLDER) == 1 and text.count(DEMO_PLACEHOLDER) == 1

    def test_loads_nothing_and_runs_nothing_but_itself(self):
        text = template()
        assert not re.search(r'\b(src|href)\s*=\s*"(https?:)?//', text)
        assert ("<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; "
                "script-src 'unsafe-inline'; style-src 'unsafe-inline'\">") in text

    def test_draws_cswaps_numbers_rather_than_working_them_out(self):
        text = template()
        for name in ("simulateRoom", "spreadUsed", "freshReading", "P.readings", "P.current"):
            assert name not in text


class TestPayload:
    def test_demo_payload_shape(self):
        p = report_payload(demo_history(NOW), days=None)
        assert p["demo"] is True and p["live"] is False and p["generatedAt"] == int(NOW)
        assert len(p["accounts"]) == 15 and all("label" in a for a in p["accounts"])
        assert p["fleet"] == list(range(1, 16)) and p["counted"] == 14
        assert set(p["room"]["series"]) == {"1d", "7d", "30d", "all"}
        for rows in p["room"]["series"].values():
            assert len(rows) == history_report.SAMPLES and all(len(r) == 4 for r in rows)
            assert rows[-1] == [int(NOW), p["room"]["now"]["d7"], p["room"]["now"]["m"], p["room"]["now"]["k"]]
        assert p["room"]["resets"]["d7"] and p["room"]["resets"]["m"]
        assert {f["id"] for f in p["forecasts"]} == {"30m", "6h", "24h"}
        for f in p["forecasts"]:
            if f["ok"]:
                assert f["path"][0][0] == int(NOW) and isinstance(f["extra"], int)
        assert "readings" not in p and "switches" not in p and "current" not in p
        json.dumps(p, allow_nan=False)

    def test_matches_the_terminal_view(self):
        """Forecasts, room now and the account list are the same numbers."""
        h = demo_history(NOW)
        a = analyse(h)
        page = report_payload(h, days=7, analysis=a)
        term = overview_json(a, days=7)
        by = {f["pace"]: f for f in term["forecasts"]}
        for f in page["forecasts"]:
            assert f["ok"] == by[f["id"]]["available"]
            if f["ok"]:
                assert round(f["rate"] * 86400, 3) == by[f["id"]]["usedPerDay"]
                assert f["extra"] == by[f["id"]]["extraAccountsToKeepUp"]
        assert page["room"]["now"]["d7"] == round(term["roomNow"], 4)
        assert page["room"]["now"]["k"] == term["countedAccounts"]
        assert [r["n"] for r in page["board"]] == [r["number"] for r in term["accountsNow"]]
        assert [r["counted"] for r in page["board"]] == [r["counted"] for r in term["accountsNow"]]

    def test_ranges_follow_days_and_start_where_the_data_does(self):
        h = History(
            now=NOW, accounts=[Account(1, alias="work"), Account(2, disabled=True), Account(3, weekly=False)],
            readings={1: [Reading(NOW - 30 * 86400, 1, 2, None), Reading(NOW - 3600, 5, 9, None, prev=NOW - 7200)]},
            current={1: Current(t=NOW - 3600, d7=9)}, active=1,
        )
        p = report_payload(h, days=7)
        assert set(p["room"]["series"]) == {"1d", "7d", "all"}  # no 30-day button for 7 days of data
        assert p["dataStart"] == int(NOW - 7 * 86400)
        assert p["room"]["series"]["7d"][0][0] == int(NOW - 7 * 86400)
        assert p["accounts"][0]["label"] == "work"
        board = {r["n"]: r for r in p["board"]}
        assert board[2]["disabled"] and not board[2]["counted"]  # shown, marked, not counted
        assert not board[3]["weekly"]
        assert p["fleet"] == [1]

    def test_html_in_data_cannot_close_the_script(self):
        h = History(now=NOW, accounts=[Account(1, alias="</script><b>x")], readings={}, current={}, active=1)
        html = render_report(report_payload(h, days=None), None)
        assert "</script><b>" not in html
        real, demo = payloads(html)
        assert real["accounts"][0]["label"] == "</script><b>x" and demo is None

    def test_the_demo_is_built_once_per_process(self, monkeypatch):
        monkeypatch.setattr(history_report, "_demo_cache", None)
        first = history_report.demo_payload(NOW)
        assert history_report.demo_payload(NOW + 3600) is first


def seed_switcher(temp_home):
    from claude_swap.switcher import ClaudeAccountSwitcher

    sw = ClaudeAccountSwitcher()
    sw.sequence_file.parent.mkdir(parents=True, exist_ok=True)
    sw.sequence_file.write_text(json.dumps({
        "activeAccountNumber": 1, "sequence": [1],
        "accounts": {"1": {"email": "a@x.com", "uuid": "u1", "alias": "main"}},
    }))
    return sw


class TestWriting:
    def test_live_copy_is_30_days_and_rebuilds_at_most_once_a_minute(self, temp_home):
        sw = seed_switcher(temp_home)
        path, payload = write_report(sw, days=None, now=NOW)
        assert path == default_report_path(sw.backup_dir) and payload["live"] is True
        assert payload["rangeDays"] == 30
        if sys.platform != "win32":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        html = path.read_text()
        real, demo = payloads(html)
        assert real["accounts"][0]["label"] == "main" and demo["demo"] is True
        assert "a@x.com" not in html
        write_report(sw, days=10, now=NOW)
        os.utime(path, (NOW, NOW))
        assert refresh_live_report(sw, now=NOW + 30) is False  # too soon
        assert refresh_live_report(sw, now=NOW + 61) is True
        real, _ = payloads(path.read_text())
        assert real["rangeDays"] == 10  # the rebuild keeps the days asked for

    def test_nothing_until_someone_builds_it(self, temp_home):
        sw = seed_switcher(temp_home)
        assert refresh_live_report(sw, now=NOW) is False
        assert not default_report_path(sw.backup_dir).exists()

    def test_a_failed_rebuild_waits_like_a_success_and_never_raises(self, temp_home, monkeypatch, caplog):
        sw = seed_switcher(temp_home)
        path, _ = write_report(sw, days=None, now=NOW)
        os.utime(path, (NOW - 600, NOW - 600))
        calls = []
        monkeypatch.setattr(history_report, "render_report", lambda *a: calls.append(1) or 1 / 0)
        assert refresh_live_report(sw, now=NOW) is False
        assert "could not rebuild" in caplog.text
        assert (path.parent / FAILED_MARKER).exists()
        assert refresh_live_report(sw, now=NOW + 10) is False  # backed off: no new attempt
        assert len(calls) == 1
        assert refresh_live_report(sw, now=NOW + LIVE_MIN_INTERVAL_S + 1) is False
        assert len(calls) == 2

    def test_recording_never_rebuilds_it(self, temp_home):
        """Only cswap auto's loop does: list, switch, the TUI and the menu bar
        record readings too, and must not pay for a page."""
        from claude_swap.usage_store import FetchRecord

        sw = seed_switcher(temp_home)
        path, _ = write_report(sw, days=None)
        os.utime(path, (0, 0))
        sw._usage_store.record(
            {"1": FetchRecord(usage={"five_hour": {"pct": 3.0}, "seven_day": {"pct": 44.0}})},
            {"1": ("a@x.com", "")},
        )
        assert path.stat().st_mtime == 0

    def test_the_engine_runs_its_after_tick_hook_after_each_tick(self, temp_home, caplog):
        from claude_swap.autoswitch import AutoSwitchEngine, TickOutcome
        from claude_swap.settings import AutoSwitchSettings

        sw = seed_switcher(temp_home)
        calls = []
        engine = AutoSwitchEngine(sw, AutoSwitchSettings(), lambda e: None,
                                  after_tick=lambda: calls.append("after") or 1 / 0)

        def tick():
            calls.append("tick")
            engine.stop()
            return TickOutcome.NO_ACTION

        engine.tick = tick
        engine._next_delay = lambda outcome: 0
        caplog.set_level("DEBUG", logger="claude-swap")
        assert engine.run_loop() == 0
        assert calls == ["tick", "after"]  # after the tick, and its error never stops the loop
        assert "after-tick hook failed" in caplog.text

    def test_cswap_auto_keeps_the_live_copy_current(self, temp_home):
        """`cswap auto` hands the engine a hook that refreshes the live copy for
        its own switcher."""
        captured = {}

        class FakeEngine:
            def __init__(self, switcher, settings, on_event, *, dry_run=False,
                         state_path=None, clock=None, after_tick=None):
                captured.update(switcher=switcher, after_tick=after_tick)

            def run_loop(self):
                return 0

            def stop(self):
                pass

        with patch("claude_swap.autoswitch.AutoSwitchEngine", FakeEngine), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch.object(sys, "argv", ["cswap", "auto", "--json"]), \
             pytest.raises(SystemExit):
            cli.main()
        with patch.object(history_report, "refresh_live_report") as refresh:
            captured["after_tick"]()
        refresh.assert_called_once_with(captured["switcher"])

    def test_out_is_a_one_off_copy_written_over_a_planted_link(self, temp_home, tmp_path):
        sw = seed_switcher(temp_home)
        victim = tmp_path / "victim.txt"
        victim.write_text("keep")
        out = tmp_path / "copy.html"
        try:
            out.symlink_to(victim)
        except OSError:
            pytest.skip("no symlinks here")
        path, payload = write_report(sw, days=None, out=out)
        assert payload["live"] is False and payload["rangeDays"] is None
        assert victim.read_text() == "keep"
        assert not out.is_symlink() and "Weekly room left" in out.read_text()


def run_cli(argv, capsys):
    with patch("os.geteuid", return_value=1000, create=True), \
         patch.object(sys, "argv", ["cswap", "history", *argv]):
        code = 0
        try:
            cli.main()
        except SystemExit as e:
            code = e.code or 0
    out = capsys.readouterr()
    return code, out.out, out.err


class TestCommand:
    def test_html_writes_the_live_copy_and_opens_it(self, temp_home, capsys):
        seed_switcher(temp_home)
        with patch("webbrowser.open") as opened:
            code, out, _ = run_cli(["--html", "--open"], capsys)
        assert code == 0 and "Wrote" in out and "last 30 days" in out and "cswap auto" in out
        assert opened.call_args[0][0].startswith("file://")

    def test_demo_html_writes_the_demo_alone(self, temp_home, capsys, tmp_path):
        out_file = tmp_path / "demo.html"
        code, out, _ = run_cli(["--html", "--demo", "--out", str(out_file)], capsys)
        assert code == 0
        real, demo = payloads(out_file.read_text())
        assert real is None and demo["demo"] is True

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
    def test_demo_html_default_goes_in_the_private_history_dir(self, temp_home, capsys):
        from claude_swap import paths

        code, out, _ = run_cli(["--html", "--demo"], capsys)
        assert code == 0
        directory = paths.get_backup_root() / "history"
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert (directory / history_report.DEMO_REPORT_FILENAME).exists()

    @pytest.mark.parametrize("argv", [["--open"], ["--out", "x.html"], ["--html", "--json"]])
    def test_bad_combinations(self, temp_home, capsys, argv):
        code, _, err = run_cli(argv, capsys)
        assert code == 2 and "--" in err
