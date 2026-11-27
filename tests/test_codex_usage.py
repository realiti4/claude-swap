"""Tests for ``claude_swap.codex_usage``: Codex's rate limits, read from its session logs."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from claude_swap import codex_usage


@pytest.fixture
def codex_home(tmp_path: Path, monkeypatch) -> Path:
    """An empty Codex home that ``CODEX_HOME`` points at; the real one is never read."""
    home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


def _stamp(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _record(
    at: float,
    five: float | None = 1.0,
    week: float | None = 2.0,
    *,
    five_reset: float | None = None,
    week_reset: float | None = None,
    limit_id: str | None = "codex",
    plan: str | None = "plus",
    swap_windows: bool = False,
) -> str:
    """One ``token_count`` log line in the shape Codex writes."""
    five_reset = at + 3 * 3600 if five_reset is None else five_reset
    week_reset = at + 6 * 86400 if week_reset is None else week_reset
    primary = None if five is None else {
        "used_percent": five, "window_minutes": 300, "resets_at": five_reset,
    }
    secondary = None if week is None else {
        "used_percent": week, "window_minutes": 10080, "resets_at": week_reset,
    }
    if swap_windows:
        primary, secondary = secondary, primary
    limits = {"limit_name": None, "primary": primary, "secondary": secondary, "plan_type": plan}
    if limit_id is not None:
        limits["limit_id"] = limit_id
    return json.dumps({
        "timestamp": _stamp(at),
        "type": "event_msg",
        "payload": {"type": "token_count", "info": {"total": 1}, "rate_limits": limits},
    })


def _log(home: Path, name: str, lines: list[str], *, day: str = "2026/11/21", mtime: float | None = None) -> Path:
    path = home / "sessions" / day / f"rollout-{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


NOW = time.time()


class TestRead:
    def test_none_without_a_codex_directory(self, codex_home: Path):
        assert codex_usage.read() is None

    def test_none_with_logs_but_no_reading(self, codex_home: Path):
        _log(codex_home, "a", ['{"type": "session_meta"}', '{"type": "event_msg", "payload": {"type": "agent_message"}}'])
        assert codex_usage.read() is None

    def test_reads_the_last_reading_of_a_log(self, codex_home: Path):
        _log(codex_home, "a", [_record(NOW - 600, 5.0, 7.0), _record(NOW - 60, 9.0, 8.0)])
        reading = codex_usage.read()
        assert reading is not None
        assert reading.plan == "plus" and reading.plan_label == "Plus"
        assert reading.windows["five_hour"][0] == 9.0
        assert reading.windows["seven_day"][0] == 8.0
        assert reading.read_at == pytest.approx(NOW - 60, abs=0.01)

    def test_reading_is_found_behind_later_unrelated_lines(self, codex_home: Path):
        _log(codex_home, "a", [_record(NOW - 60, 4.0, 4.0)] + ['{"type": "event_msg", "payload": {"type": "agent_message"}}'] * 50)
        assert codex_usage.read().windows["five_hour"][0] == 4.0

    def test_skips_a_torn_last_line_and_other_junk(self, codex_home: Path):
        _log(codex_home, "a", [
            _record(NOW - 120, 6.0, 6.0),
            '{"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {"primary": ',  # cut off mid-write
            "not json but mentions \"rate_limits\"",
            '{"payload": "x", "rate_limits": 1}',
            '[{"rate_limits": 2}]',
        ])
        assert codex_usage.read().windows["five_hour"][0] == 6.0

    def test_ignores_other_limit_ids(self, codex_home: Path):
        _log(codex_home, "a", [
            _record(NOW - 120, 3.0, 3.0),
            _record(NOW - 60, None, None, limit_id="premium"),
            _record(NOW - 30, 90.0, 90.0, limit_id="codex_other"),
        ])
        assert codex_usage.read().windows["five_hour"][0] == 3.0

    def test_none_when_only_other_limit_ids_exist(self, codex_home: Path):
        _log(codex_home, "a", [_record(NOW - 60, 50.0, 50.0, limit_id="premium")])
        assert codex_usage.read() is None

    def test_a_missing_limit_id_counts_as_codex(self, codex_home: Path):
        _log(codex_home, "a", [_record(NOW - 60, 12.0, 13.0, limit_id=None)])
        assert codex_usage.read().windows["five_hour"][0] == 12.0

    def test_a_record_without_windows_is_not_a_reading(self, codex_home: Path):
        _log(codex_home, "a", [_record(NOW - 120, 8.0, 8.0), _record(NOW - 60, None, None)])
        assert codex_usage.read().windows["five_hour"][0] == 8.0

    def test_windows_are_told_apart_by_minutes_not_position(self, codex_home: Path):
        # The 5-hour window in the secondary slot is still the 5-hour window...
        _log(codex_home, "a", [_record(NOW - 60, 10.0, None, swap_windows=True)])
        assert codex_usage.read().windows == {"five_hour": (10.0, pytest.approx(NOW + 3 * 3600 - 60, abs=1))}
        # ...and a weekly window alone in the primary slot is the weekly one.
        _log(codex_home, "b", [_record(NOW - 30, None, 20.0, swap_windows=True)])
        assert set(codex_usage.read().windows) == {"seven_day"}

    def test_unknown_window_lengths_are_ignored(self, codex_home: Path):
        line = json.loads(_record(NOW - 60, 1.0, 2.0))
        line["payload"]["rate_limits"]["primary"]["window_minutes"] = 60
        _log(codex_home, "a", [json.dumps(line)])
        assert set(codex_usage.read().windows) == {"seven_day"}

    @pytest.mark.parametrize("bad", [None, "5", True, float("nan"), float("inf"), [1]])
    def test_a_bad_figure_drops_its_window(self, codex_home: Path, bad):
        line = json.loads(_record(NOW - 60, 1.0, 2.0))
        line["payload"]["rate_limits"]["primary"]["used_percent"] = bad
        _log(codex_home, "a", [json.dumps(line)])
        assert set(codex_usage.read().windows) == {"seven_day"}

    def test_an_absurd_reset_time_is_dropped_not_raised(self, codex_home: Path):
        _log(codex_home, "a", [_record(NOW - 60, 1.0, 2.0, five_reset=1e30, week_reset=-5)])
        reading = codex_usage.read()
        assert reading.windows == {"five_hour": (1.0, None), "seven_day": (2.0, None)}
        assert reading.usage() == {"five_hour": {"pct": 1.0}, "seven_day": {"pct": 2.0}}

    def test_the_newest_record_wins_across_logs(self, codex_home: Path):
        # The log written last holds the older record: two sessions interleaved.
        _log(codex_home, "old", [_record(NOW - 600, 1.0, 1.0)], mtime=NOW - 600)
        _log(codex_home, "new", [_record(NOW - 300, 2.0, 2.0)], mtime=NOW - 300)
        _log(codex_home, "last", [_record(NOW - 900, 3.0, 3.0)], mtime=NOW - 10)
        assert codex_usage.read().windows["five_hour"][0] == 2.0

    def test_falls_back_to_an_older_log_when_the_newest_has_no_record(self, codex_home: Path):
        _log(codex_home, "a", [_record(NOW - 600, 7.0, 7.0)], mtime=NOW - 600)
        _log(codex_home, "b", ['{"type": "session_meta"}'], mtime=NOW - 5)
        assert codex_usage.read().windows["five_hour"][0] == 7.0

    def test_only_the_newest_day_directories_are_searched(self, codex_home: Path, monkeypatch):
        monkeypatch.setattr(codex_usage, "_MAX_DAY_DIRS", 2)
        _log(codex_home, "old", [_record(NOW - 9000, 1.0, 1.0)], day="2026/11/01")
        _log(codex_home, "mid", ['{"type": "session_meta"}'], day="2026/11/20")
        _log(codex_home, "new", ['{"type": "session_meta"}'], day="2026/11/21")
        assert codex_usage.read() is None
        _log(codex_home, "mid", [_record(NOW - 7200, 4.0, 4.0)], day="2026/11/20")
        assert codex_usage.read().windows["five_hour"][0] == 4.0

    def test_days_sort_across_months_and_years(self, codex_home: Path, monkeypatch):
        monkeypatch.setattr(codex_usage, "_MAX_DAY_DIRS", 1)
        _log(codex_home, "dec", [_record(NOW - 600, 1.0, 1.0)], day="2025/12/31")
        _log(codex_home, "jan", [_record(NOW - 300, 2.0, 2.0)], day="2026/01/02")
        assert codex_usage.read().windows["five_hour"][0] == 2.0

    def test_newer_logs_without_a_reading_do_not_hide_an_older_one(self, codex_home: Path):
        _log(codex_home, "reading", [_record(NOW - 9000, 6.0, 6.0)], mtime=NOW - 9000)
        for i in range(8):
            _log(codex_home, f"empty{i}", ['{"type": "session_meta"}'], mtime=NOW - 100 + i)
        assert codex_usage.read().windows["five_hour"][0] == 6.0

    def test_the_number_of_logs_tested_is_capped(self, codex_home: Path, monkeypatch):
        monkeypatch.setattr(codex_usage, "_MAX_FILES", 2)
        _log(codex_home, "reading", [_record(NOW - 900, 1.0, 1.0)], mtime=NOW - 900)
        _log(codex_home, "x", ['{"type": "session_meta"}'], mtime=NOW - 20)
        _log(codex_home, "y", ['{"type": "session_meta"}'], mtime=NOW - 10)
        assert codex_usage.read() is None

    def test_the_search_stops_after_a_few_readings(self, codex_home: Path, monkeypatch):
        monkeypatch.setattr(codex_usage, "_MAX_READINGS", 3)
        scanned = []
        real = codex_usage._scan
        monkeypatch.setattr(codex_usage, "_scan", lambda path, size: scanned.append(path.name) or real(path, size))
        # Newest write first; the freshest record sits in the fourth log, past the stop.
        for i, (written, taken, pct) in enumerate([
            (NOW - 10, NOW - 1000, 1.0), (NOW - 20, NOW - 1100, 2.0),
            (NOW - 30, NOW - 1200, 3.0), (NOW - 40, NOW - 500, 4.0),
        ]):
            _log(codex_home, f"l{i}", [_record(taken, pct, pct)], mtime=written)
        reading = codex_usage.read()
        assert len(scanned) == 3
        assert reading.windows["five_hour"][0] == 1.0  # newest record among the three looked at

    def test_other_files_in_the_codex_home_are_never_read(self, codex_home: Path):
        codex_home.mkdir(parents=True)
        (codex_home / "auth.json").write_text(_record(NOW, 99.0, 99.0))  # would parse, if read
        (codex_home / "sessions").mkdir()
        (codex_home / "sessions" / "note.jsonl").write_text(_record(NOW, 99.0, 99.0))
        _log(codex_home, "a", [_record(NOW - 60, 1.0, 1.0)])
        assert codex_usage.read().windows["five_hour"][0] == 1.0

    def test_unreadable_log_reads_as_no_reading(self, codex_home: Path, monkeypatch):
        _log(codex_home, "a", [_record(NOW - 60, 1.0, 1.0)])

        def boom(*args, **kwargs):
            raise PermissionError("denied")

        monkeypatch.setattr(codex_usage, "_scan", boom)
        assert codex_usage.read() is None

    def test_unexpected_failure_reads_as_no_reading(self, codex_home: Path, monkeypatch):
        _log(codex_home, "a", [_record(NOW - 60, 1.0, 1.0)])
        monkeypatch.setattr(codex_usage, "_newest_logs", lambda root: 1 / 0)
        assert codex_usage.read() is None

    def test_without_codex_home_uses_dot_codex_under_home(self):
        # No CODEX_HOME here (conftest clears it); $HOME is the test's own.
        _log(Path.home() / ".codex", "a", [_record(NOW - 60, 11.0, 12.0)])
        assert codex_usage.read().windows["five_hour"][0] == 11.0


class TestBadRecords:
    """A record Codex wrote oddly is skipped; it never costs the reader the good ones."""

    @pytest.mark.parametrize("stamp", [
        None, "garbage", 123, "1970-01-01T00:00:00Z", "2500-01-01T00:00:00Z", "0001-01-01T00:00:00Z",
    ])
    def test_a_record_without_a_usable_timestamp_is_skipped(self, codex_home: Path, stamp):
        bad = json.loads(_record(NOW - 30, 90.0, 90.0))
        if stamp is None:
            del bad["timestamp"]
        else:
            bad["timestamp"] = stamp
        _log(codex_home, "a", [_record(NOW - 60, 5.0, 5.0), json.dumps(bad)])
        assert codex_usage.read().windows["five_hour"][0] == 5.0

    def test_a_lone_record_without_a_timestamp_is_no_reading(self, codex_home: Path):
        bad = json.loads(_record(NOW - 30, 90.0, 90.0))
        del bad["timestamp"]
        _log(codex_home, "a", [json.dumps(bad)])
        assert codex_usage.read() is None

    @pytest.mark.parametrize("plan, kept", [
        ("plus", True), ("p" * 40, True), ("p" * 41, False), ("pl\nus", False),
        ("pl\x1b[31mus", False), (7, False),
    ])
    def test_a_plan_name_must_be_short_and_printable(self, codex_home: Path, plan, kept):
        line = json.loads(_record(NOW - 60, 1.0, 1.0))
        line["payload"]["rate_limits"]["plan_type"] = plan
        _log(codex_home, "a", [json.dumps(line)])
        reading = codex_usage.read()
        assert reading.windows["five_hour"][0] == 1.0  # the figures survive either way
        assert (reading.plan == plan) is kept
        assert (reading.plan_label is not None) is kept

    def test_an_integer_beyond_float_range_drops_its_window_not_the_reader(self, codex_home: Path):
        huge = "1" + "0" * 400
        text = _record(NOW - 60, 1.0, 2.0).replace('"used_percent": 1.0', f'"used_percent": {huge}')
        text = text.replace('"window_minutes": 10080', f'"window_minutes": {huge}')
        assert huge in text
        _log(codex_home, "a", [_record(NOW - 600, 9.0, 9.0), text])
        # The newer record has no usable window left, so the older one is the reading.
        assert codex_usage.read().windows["five_hour"][0] == 9.0
        reset_text = _record(NOW - 30, 4.0, 5.0).replace(f'"resets_at": {NOW - 30 + 3 * 3600}', f'"resets_at": {huge}')
        _log(codex_home, "b", [reset_text])
        assert codex_usage.read().windows["five_hour"] == (4.0, None)


class TestCacheSafety:
    def test_a_file_that_failed_to_read_is_tried_again(self, codex_home: Path, monkeypatch):
        _log(codex_home, "a", [_record(NOW - 60, 1.0, 1.0)])
        real = codex_usage._scan
        calls = []

        def flaky(path, size):
            calls.append(path)
            if len(calls) == 1:
                raise PermissionError("denied")
            return real(path, size)

        monkeypatch.setattr(codex_usage, "_scan", flaky)
        assert codex_usage.read() is None
        assert codex_usage._cache == {}  # the failure was not remembered
        assert codex_usage.read().windows["five_hour"][0] == 1.0
        assert len(calls) == 2

    def test_another_thread_swapping_the_cache_mid_read_changes_nothing(self, codex_home: Path, monkeypatch):
        _log(codex_home, "a", [_record(NOW - 60, 8.0, 8.0)])
        codex_usage.read()  # fills the cache

        class _Swapped(dict):
            """Any lookup is followed by another thread replacing the global."""

            def get(self, key, default=None):
                value = super().get(key, default)
                codex_usage._cache = {}
                return value

            def __contains__(self, key):
                found = super().__contains__(key)
                codex_usage._cache = {}
                return found

        monkeypatch.setattr(codex_usage, "_cache", _Swapped(codex_usage._cache))
        monkeypatch.setattr(codex_usage, "_scan", lambda *a: pytest.fail("rescanned a cached log"))
        assert codex_usage.read().windows["five_hour"][0] == 8.0


class TestScanCap:
    def test_a_reading_deeper_than_the_cap_is_not_looked_for(self, codex_home: Path, monkeypatch):
        monkeypatch.setattr(codex_usage, "_CHUNK", 512)
        monkeypatch.setattr(codex_usage, "_SCAN_CAP", 2048)
        filler = '{"type": "event_msg", "payload": {"type": "agent_message", "text": "%s"}}' % ("x" * 100)
        _log(codex_home, "a", [_record(NOW - 60, 1.0, 1.0)] + [filler] * 40)
        assert codex_usage.read() is None

    def test_a_reading_inside_the_cap_is_found_across_chunk_boundaries(self, codex_home: Path, monkeypatch):
        monkeypatch.setattr(codex_usage, "_CHUNK", 64)  # the record is several chunks long
        monkeypatch.setattr(codex_usage, "_SCAN_CAP", 4096)
        filler = '{"type": "event_msg", "payload": {"type": "agent_message", "text": "y"}}'
        _log(codex_home, "a", [_record(NOW - 600, 2.0, 2.0), _record(NOW - 60, 3.0, 3.0), filler])
        assert codex_usage.read().windows["five_hour"][0] == 3.0

    def test_a_single_line_longer_than_the_cap_does_not_hang_or_raise(self, codex_home: Path, monkeypatch):
        monkeypatch.setattr(codex_usage, "_CHUNK", 128)
        monkeypatch.setattr(codex_usage, "_SCAN_CAP", 1024)
        _log(codex_home, "a", [_record(NOW - 60, 1.0, 1.0), "z" * 10_000])
        assert codex_usage.read() is None

    def test_the_first_line_of_a_small_file_is_read(self, codex_home: Path):
        _log(codex_home, "a", [_record(NOW - 60, 5.0, 6.0)])  # the only line, no newline before it
        assert codex_usage.read().windows["seven_day"][0] == 6.0


class TestCache:
    def test_unchanged_logs_are_not_scanned_again(self, codex_home: Path, monkeypatch):
        _log(codex_home, "a", [_record(NOW - 60, 1.0, 1.0)])
        calls = []
        real = codex_usage._scan
        monkeypatch.setattr(codex_usage, "_scan", lambda *a: calls.append(a) or real(*a))
        first = codex_usage.read()
        second = codex_usage.read()
        assert len(calls) == 1
        assert second == first

    def test_a_changed_log_is_scanned_again(self, codex_home: Path, monkeypatch):
        path = _log(codex_home, "a", [_record(NOW - 600, 1.0, 1.0)])
        calls = []
        real = codex_usage._scan
        monkeypatch.setattr(codex_usage, "_scan", lambda *a: calls.append(a) or real(*a))
        assert codex_usage.read().windows["five_hour"][0] == 1.0
        with path.open("a", encoding="utf-8") as f:
            f.write(_record(NOW - 30, 2.0, 2.0) + "\n")
        assert codex_usage.read().windows["five_hour"][0] == 2.0
        assert len(calls) == 2

    def test_a_log_without_a_reading_is_remembered_too(self, codex_home: Path, monkeypatch):
        _log(codex_home, "a", ['{"type": "session_meta"}'])
        calls = []
        real = codex_usage._scan
        monkeypatch.setattr(codex_usage, "_scan", lambda *a: calls.append(a) or real(*a))
        assert codex_usage.read() is None
        assert codex_usage.read() is None
        assert len(calls) == 1

    def test_the_cache_forgets_logs_that_left_view(self, codex_home: Path):
        path = _log(codex_home, "a", [_record(NOW - 60, 1.0, 1.0)])
        codex_usage.read()
        assert len(codex_usage._cache) == 1
        path.unlink()
        assert codex_usage.read() is None
        assert codex_usage._cache == {}


class TestUsage:
    def _reading(self, five=(10.0, None), week=(20.0, None)):
        windows = {}
        if five:
            windows["five_hour"] = five
        if week:
            windows["seven_day"] = week
        return codex_usage.CodexUsage("plus", windows, NOW - 60)

    def test_future_resets_carry_their_iso_time(self):
        usage = self._reading((10.0, NOW + 3600), (20.0, NOW + 86400)).usage(NOW)
        assert usage["five_hour"]["pct"] == 10.0
        assert datetime.fromisoformat(usage["five_hour"]["resets_at"]).timestamp() == pytest.approx(NOW + 3600, abs=1)
        assert datetime.fromisoformat(usage["seven_day"]["resets_at"]).timestamp() == pytest.approx(NOW + 86400, abs=1)

    def test_a_passed_reset_reads_as_zero_with_no_reset_time(self):
        usage = self._reading((50.0, NOW - 1), (60.0, NOW + 86400)).usage(NOW)
        assert usage["five_hour"] == {"pct": 0.0}
        assert usage["seven_day"]["pct"] == 60.0

    def test_a_passed_weekly_reset_is_not_rolled_forward(self):
        # No newer reading exists, so no next reset time is known.
        usage = self._reading((1.0, NOW + 60), (60.0, NOW - 5 * 86400)).usage(NOW)
        assert usage["seven_day"] == {"pct": 0.0}

    def test_no_reset_time_keeps_the_figure(self):
        assert self._reading((10.0, None), None).usage(NOW) == {"five_hour": {"pct": 10.0}}

    def test_the_usage_shape_is_the_one_the_claude_formatters_read(self):
        from claude_swap import menubar

        usage = self._reading((10.0, NOW + 3 * 3600 + 90), (20.0, NOW + 2 * 86400 + 90)).usage(NOW)
        assert menubar.usage_summary(usage, NOW) == "5h 10% (3h 1m) · 7d 20% (2d 0h)"

    @pytest.mark.parametrize("plan, label", [("plus", "Plus"), ("pro", "Pro"), ("self_serve_business", "Self Serve Business"), (None, None), ("", None)])
    def test_plan_label(self, plan, label):
        assert codex_usage.CodexUsage(plan, {}, NOW).plan_label == label

    def test_read_clock_is_the_local_time_today_and_the_date_before(self, monkeypatch):
        # Absolute instants, so a daylight-saving change in the local zone cannot
        # move them; 12:07 UTC is never within a minute of a local midnight.
        taken = datetime(2026, 6, 15, 12, 7, tzinfo=timezone.utc).timestamp()
        local = datetime.fromtimestamp(taken)

        class _Clock(datetime):
            now_ts = taken + 60

            @classmethod
            def now(cls, tz=None):
                return datetime.fromtimestamp(cls.now_ts, tz)

        monkeypatch.setattr(codex_usage, "datetime", _Clock)
        reading = codex_usage.CodexUsage("plus", {}, taken)
        assert reading.read_clock() == local.strftime("%H:%M")
        _Clock.now_ts = taken + 3 * 86400
        assert reading.read_clock() == f"{local.strftime('%b')} {local.day} {local.strftime('%H:%M')}"
