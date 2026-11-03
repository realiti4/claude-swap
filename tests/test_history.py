"""Tests for usage-history recording and reading (history.py), and its hooks
in the usage store."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from claude_swap import history
from claude_swap.history import (
    HEARTBEAT_S,
    NewReading,
    UsageHistory,
    iso_utc,
    load_history,
    usage_row,
)
from claude_swap.usage_store import FetchRecord, UsageEntry, UsageStore

T0 = 1_800_000_000.0  # 2027-01-15, a fixed clock for every test


class Clock:
    def __init__(self, now: float = T0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def usage(h5: float = 10.0, d7: float = 20.0, fable: float | None = None) -> dict:
    u = {
        "five_hour": {"pct": h5, "resets_at": "2027-01-15T12:00:00.123+00:00", "countdown": "x", "clock": "y"},
        "seven_day": {"pct": d7, "resets_at": "2027-01-20T07:59:59.9+00:00"},
    }
    if fable is not None:
        u["scoped"] = [{"name": "Fable", "pct": fable, "resets_at": "2027-01-20T08:00:00+00:00"}]
    return u


def rows(root: Path, kind: str = "usage") -> list[dict]:
    out = []
    for p in sorted((root / "history").glob(f"{kind}-*.jsonl")):
        out.extend(history.iter_jsonl(p))  # skips a torn line, as every reader does
    return out


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def recorder(tmp_path, clock):
    return UsageHistory(tmp_path, account_ids=lambda: {"1": "uuid-1"}, clock=clock)


def reading(num="1", t=T0, prev=None, **kw) -> NewReading:
    return NewReading(num, usage(**kw), t, prev)


class TestUsageRow:
    def test_shape_and_no_email(self):
        row = usage_row(3, usage(fable=12.5), T0, account_id="u-3")
        assert row == {
            "ts": iso_utc(T0),
            "acct": 3,
            "id": "u-3",
            "h5": 10,
            "h5_reset": "2027-01-15T12:00Z",
            "d7": 20,
            "d7_reset": "2027-01-20T08:00Z",  # rounded to the minute
            "models": {"Fable": {"pct": 12.5, "reset": "2027-01-20T08:00Z"}},
        }
        assert "email" not in json.dumps(row)

    def test_extra_usage_spend_is_kept(self):
        u = usage()
        u["spend"] = {"used": 1.5, "limit": 50.0, "pct": 3.0, "currency": "USD", "countdown": "z"}
        assert usage_row(1, u, T0)["spend"] == {"used": 1.5, "limit": 50.0, "pct": 3.0, "currency": "USD"}

    @pytest.mark.parametrize("value", [None, "api key", {}, {"spend": {"pct": 1}}])
    def test_no_window_no_row(self, value):
        assert usage_row(1, value, T0) is None


class TestRecording:
    def test_a_changed_reading_is_written_with_its_previous_check(self, recorder, tmp_path):
        assert recorder.record_readings([reading(t=T0)]) == 1
        assert recorder.record_readings([reading(t=T0 + 60, prev=T0)]) == 0  # unchanged
        assert recorder.record_readings([reading(t=T0 + 120, prev=T0 + 60, d7=21)]) == 1
        got = rows(tmp_path)
        assert [r["ts"] for r in got] == [iso_utc(T0), iso_utc(T0 + 120)]
        assert "prev" not in got[0]
        assert got[1]["prev"] == iso_utc(T0 + 60)  # the exact check before it
        assert got[1]["id"] == "uuid-1"

    def test_an_unchanged_reading_gets_a_heartbeat_row(self, recorder, tmp_path):
        recorder.record_readings([reading(t=T0)])
        recorder.record_readings([reading(t=T0 + HEARTBEAT_S - 1)])
        recorder.record_readings([reading(t=T0 + HEARTBEAT_S)])
        assert len(rows(tmp_path)) == 2

    def test_reset_jitter_is_not_a_change(self, recorder, tmp_path):
        recorder.record_readings([reading(t=T0)])
        jittered = usage()
        jittered["seven_day"]["resets_at"] = "2027-01-20T08:00:00.2+00:00"
        jittered["five_hour"]["countdown"] = "different"
        recorder.record_readings([NewReading("1", jittered, T0 + 60, T0)])
        assert len(rows(tmp_path)) == 1

    def test_a_reading_stamped_by_a_clock_running_ahead_does_not_block_the_slot(
        self, recorder, tmp_path, clock
    ):
        recorder.record_readings([reading(t=T0 + 86400, d7=30)])  # a day in the future
        recorder.record_readings([reading(t=T0 + 60, d7=31)])
        assert [r["d7"] for r in rows(tmp_path)] == [30, 31]
        # within the skew allowance a later stamp still wins, as an ordinary replay
        clock.now = T0 + 86400 - 200
        recorder.record_readings([reading(t=T0 + 120, d7=32)])
        assert [r["d7"] for r in rows(tmp_path)] == [30, 31, 32]

    def test_an_older_reading_arriving_late_is_skipped(self, recorder, tmp_path):
        recorder.record_readings([reading(t=T0, d7=30)])
        recorder.record_readings([reading(t=T0 - 600, d7=10)])
        assert [r["d7"] for r in rows(tmp_path)] == [30]

    def test_rows_go_to_the_month_of_their_reading(self, recorder, tmp_path):
        jan31 = 1_801_439_000.0  # 2027-01-31T21:43:20Z
        recorder.record_readings([reading(t=jan31), reading("2", t=jan31 + 86400)])
        names = sorted(p.name for p in (tmp_path / "history").glob("usage-*.jsonl"))
        assert names == ["usage-2027-01.jsonl", "usage-2027-02.jsonl"]

    def test_no_id_when_the_slot_has_no_uuid(self, recorder, tmp_path):
        recorder.record_readings([reading("2")])
        assert "id" not in rows(tmp_path)[0]

    def test_disabled_records_nothing(self, recorder, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({"history": {"enabled": False}}))
        assert recorder.record_readings([reading()]) == 0
        assert not (tmp_path / "history").exists()

    def test_never_raises(self, recorder, monkeypatch, caplog):
        def boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(history, "_append_lines", boom)
        monkeypatch.setattr(history, "_last_warning", 0.0)
        caplog.set_level("DEBUG", logger="claude-swap")
        assert recorder.record_readings([reading()]) == 0
        assert recorder.record_readings([reading(t=T0 + 60, d7=50)]) == 0
        # one warning, the repeat at debug level
        assert [r.levelname for r in caplog.records if "could not record" in r.message] == [
            "WARNING", "DEBUG"]

    def test_a_busy_lock_skips_the_write_rather_than_wait(self, recorder, tmp_path, monkeypatch):
        monkeypatch.setattr(history.FileLock, "acquire", lambda self, timeout=None: False)
        assert recorder.record_readings([reading()]) == 0
        assert rows(tmp_path) == []

    def test_a_torn_last_line_is_ended_before_the_next_append(self, recorder, tmp_path):
        recorder.record_readings([reading(t=T0)])
        path = next((tmp_path / "history").glob("usage-*.jsonl"))
        with open(path, "a") as f:
            f.write('{"ts": "2027-01-15T0')  # a crash mid-append
        recorder.record_readings([reading(t=T0 + 60, d7=40)])
        lines = path.read_text().splitlines()
        assert lines[1] == '{"ts": "2027-01-15T0'  # left for the reader to skip...
        assert [r["d7"] for r in rows(tmp_path)] == [20, 40]  # ...not merged into the next row

    def test_state_remembers_when_the_history_started(self, recorder, tmp_path):
        recorder.record_readings([reading(t=T0)])
        recorder.record_readings([reading(t=T0 + 600, d7=50)])
        state = json.loads((tmp_path / "history" / "state.json").read_text())
        assert state["since"] == iso_utc(T0)

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
    def test_files_are_owner_only(self, recorder, tmp_path):
        recorder.record_readings([reading()])
        directory = tmp_path / "history"
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        for p in directory.iterdir():
            if p.name.endswith((".jsonl", ".json")):
                assert stat.S_IMODE(p.stat().st_mode) == 0o600, p.name


class TestRetention:
    def _month(self, root: Path, name: str) -> Path:
        p = root / "history" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}\n")
        return p

    def test_whole_months_past_retention_are_deleted_once_a_day(self, tmp_path, clock):
        (tmp_path / "settings.json").write_text(json.dumps({"history": {"retentionDays": 60}}))
        old = self._month(tmp_path, "usage-2026-10.jsonl")  # ended Nov 1: 75 days ago
        edge = self._month(tmp_path, "usage-2026-11.jsonl")  # ended Dec 1: 45 days ago
        other = self._month(tmp_path, "notes.jsonl")
        rec = UsageHistory(tmp_path, clock=clock)
        rec.record_readings([reading()])
        assert not old.exists()
        assert edge.exists() and other.exists()
        # a month that ages out later the same day waits for tomorrow's check
        late = self._month(tmp_path, "usage-2026-09.jsonl")
        clock.now += 60
        rec.record_readings([reading(t=clock.now, d7=99)])
        assert late.exists()
        clock.now += 86400
        rec.record_readings([reading(t=clock.now, d7=98)])
        assert not late.exists()

    def test_retention_setting_is_validated(self, tmp_path):
        from claude_swap.settings import load_history_settings

        for bad in (5, "400", True, 99999):
            (tmp_path / "settings.json").write_text(json.dumps({"history": {"retentionDays": bad}}))
            assert load_history_settings(tmp_path).retention_days == 400


class TestUsageStoreHook:
    IDENT = {"1": ("a@x.com", ""), "2": ("b@x.com", "org-2")}

    def test_record_and_adopt_feed_the_history(self, tmp_path, clock):
        rec = UsageHistory(tmp_path, clock=clock)
        store = UsageStore(tmp_path / "cache", clock=clock, history=rec)
        store.record({"1": FetchRecord(usage=usage(d7=5))}, self.IDENT)
        clock.now += 300
        store.record(
            {"1": FetchRecord(usage=usage(d7=9)), "2": FetchRecord(error="http-429")},
            self.IDENT,
        )
        clock.now += 300
        store.adopt({"2": (usage(d7=50), 30.0)}, self.IDENT)
        got = rows(tmp_path)
        assert [(r["acct"], r["d7"]) for r in got] == [(1, 5), (1, 9), (2, 50)]
        assert got[1]["prev"] == iso_utc(T0)  # the store's previous fetchedAt
        assert got[2]["ts"] == iso_utc(T0 + 570)  # adopted: now - age
        assert "a@x.com" not in json.dumps(got)

    def test_a_reused_slot_has_no_previous_check(self, tmp_path, clock):
        rec = UsageHistory(tmp_path, clock=clock)
        store = UsageStore(tmp_path / "cache", clock=clock, history=rec)
        store.record({"1": FetchRecord(usage=usage(d7=5))}, self.IDENT)
        clock.now += 300
        store.record({"1": FetchRecord(usage=usage(d7=70))}, {"1": ("new@x.com", "")})
        assert "prev" not in rows(tmp_path)[1]

    def test_a_history_failure_never_reaches_the_store(self, tmp_path, clock, monkeypatch):
        rec = UsageHistory(tmp_path, clock=clock)
        monkeypatch.setattr(rec, "_record_readings", lambda r: 1 / 0)
        store = UsageStore(tmp_path / "cache", clock=clock, history=rec)
        assert store.record({"1": FetchRecord(usage=usage())}, self.IDENT) == {"1"}
        assert store.entries(self.IDENT)["1"].last_good == usage()


class TestLoadHistory:
    def seq(self, **accounts) -> dict:
        return {"activeAccountNumber": 1, "accounts": accounts}

    def write(self, root: Path, name: str, recs: list) -> None:
        d = root / "history"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text("".join(json.dumps(r) + "\n" for r in recs) + "{torn")

    def test_joins_rows_accounts_and_current_readings(self, tmp_path):
        self.write(tmp_path, "usage-2027-01.jsonl", [
            {"ts": iso_utc(T0 - 7200), "acct": 1, "id": "u1", "h5": 1, "d7": 10,
             "models": {"Fable": {"pct": 3}}},
            {"ts": iso_utc(T0 - 3600), "acct": 1, "id": "OLD", "h5": 1, "d7": 90},  # previous owner
            {"ts": iso_utc(T0 - 3600), "acct": 7, "h5": 0, "d7": 40},  # removed since
            {"ts": iso_utc(T0 - 3600), "acct": 2, "h5": 0, "d7": 5,
             "prev": iso_utc(T0 - 4000)},  # no id: goes by its slot
        ])
        entries = {"1": UsageEntry(last_good=usage(d7=12, fable=4), fetched_at=T0 - 60)}
        h = load_history(
            tmp_path,
            self.seq(**{"1": {"uuid": "u1"}, "2": {"disabled": True, "priority": 5}}),
            entries,
            now=T0,
            since=T0 - 86400,
        )
        assert [(a.number, a.removed, a.disabled, a.priority) for a in h.accounts] == [
            (1, False, False, 0), (2, False, True, 5), (7, True, False, 0)]
        assert h.hidden_rows == 1
        assert h.model_name == "Fable"
        assert [(r.d7, r.model) for r in h.readings[1]] == [(10, 3), (12, 4)]  # current closes it
        assert h.readings[2][0].prev == T0 - 4000
        assert h.current[1].d7 == 12 and h.current[1].model == 4
        assert h.recording_since == T0 - 7200

    def test_swap_and_move_carry_each_accounts_history_to_its_new_number(self, tmp_path):
        self.write(tmp_path, "usage-2027-01.jsonl", [
            {"ts": iso_utc(T0 - 7200), "acct": 1, "id": "ua", "h5": 0, "d7": 10},
            {"ts": iso_utc(T0 - 7200), "acct": 2, "id": "ub", "h5": 0, "d7": 70},
            {"ts": iso_utc(T0 - 7200), "acct": 3, "id": "uc", "h5": 0, "d7": 50},
        ])
        # `cswap swap 1 2`, then `cswap move 3 5`
        seq = self.seq(**{"1": {"uuid": "ub"}, "2": {"uuid": "ua"}, "5": {"uuid": "uc"}})
        h = load_history(tmp_path, seq, {}, now=T0, since=T0 - 86400)
        assert {n: [r.d7 for r in rows] for n, rows in h.readings.items()} == {1: [70], 2: [10], 5: [50]}
        assert h.hidden_rows == 0
        assert [a.number for a in h.accounts] == [1, 2, 5]

    def test_api_keys_and_readings_without_a_weekly_window_have_no_weekly_limit(self, tmp_path):
        no_week = {"five_hour": {"pct": 4.0}}
        h = load_history(
            tmp_path,
            self.seq(**{"1": {"uuid": "u1", "kind": "api_key"}, "2": {"uuid": "u2"}, "3": {"uuid": "u3"}}),
            {"2": UsageEntry(last_good=no_week, fetched_at=T0 - 60),
             "3": UsageEntry(last_good=usage(), fetched_at=T0 - 60)},
            now=T0,
            since=T0 - 86400,
        )
        assert [(a.number, a.weekly) for a in h.accounts] == [(1, False), (2, False), (3, True)]

    def test_reads_only_the_months_it_needs(self, tmp_path):
        self.write(tmp_path, "usage-2025-01.jsonl", [{"ts": "2025-01-02T00:00:00Z", "acct": 1, "d7": 1}])
        h = load_history(tmp_path, self.seq(), {}, now=T0, since=T0 - 86400)
        assert h.readings == {}

    def test_empty(self, tmp_path):
        h = load_history(tmp_path, None, {}, now=T0, since=T0 - 86400)
        assert h.accounts == [] and h.readings == {} and h.recording_since is None


class TestSwitcherIntegration:

    def test_switcher_wires_the_history_into_its_store(self, temp_home):
        from claude_swap.switcher import ClaudeAccountSwitcher

        sw = ClaudeAccountSwitcher()
        assert sw._usage_store.history is sw._history
        assert sw._history.dir == sw.backup_dir / "history"

    def test_account_ids_come_from_sequence_uuids(self, temp_home):
        from claude_swap.switcher import ClaudeAccountSwitcher

        sw = ClaudeAccountSwitcher()
        sw.sequence_file.parent.mkdir(parents=True, exist_ok=True)
        sw.sequence_file.write_text(json.dumps({"accounts": {
            "1": {"email": "a@x.com", "uuid": " u-1 "}, "2": {"email": "b@x.com", "uuid": ""}}}))
        assert sw._history_account_ids() == {"1": "u-1"}

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_umask_does_not_widen_new_files(tmp_path, clock):
    old = os.umask(0)
    try:
        UsageHistory(tmp_path, clock=clock).record_readings([reading()])
    finally:
        os.umask(old)
    p = next((tmp_path / "history").glob("usage-*.jsonl"))
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
