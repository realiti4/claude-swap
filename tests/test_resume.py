"""Tests for auto-continue (resume.py): the readiness policy, the loop
guard, the idempotent hook handler, and the waiter loop end to end with the
session, inbox and clock faked out."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import resume as rs
from claude_swap import transcript as tx
from claude_swap.process_detection import ClaudeSession
from claude_swap.resume import LiveContext, ResumeRecord, Waiter, arm, evaluate, note_stall
from claude_swap.settings import ResumeSettings
from tests.test_transcript import RESETS_AT, assistant, human, limit_stop, write
from tests.test_transcript import _ts as iso_ts

SID = "41fad98a-27b8-499d-bf23-4588962b72a3"
SOCKET = r"\\.\pipe\LOCAL\cc-msg-test"
CHILD_TOKEN = "c" * 32
A = ("a@example.com", "")
B = ("b@example.com", "")


class FakeClock:
    def __init__(self, now: float):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _record(**kwargs) -> ResumeRecord:
    base = {
        "session_id": SID,
        "transcript_path": "t.jsonl",
        "config_home": "cfg",
        "global_config_path": "g.json",
        "default_profile": True,
        "armed_at": 1000.0,
        "identity_email": A[0],
        "identity_org": "",
        "stall_uuid": "s1",
        "stalled_at": 1000.0,
        "resets_at": 5000.0,
        "limit_type": "five_hour",
    }
    base.update(kwargs)
    return ResumeRecord(**base)


def _usage(pct: float, resets_at: str | None = None) -> dict:
    window: dict = {"pct": pct}
    if resets_at:
        window["resets_at"] = resets_at
    return {"five_hour": window, "seven_day": {"pct": 10.0}}


SETTINGS = ResumeSettings()


class TestEvaluate:
    def test_reset_plus_slack_is_ready(self):
        r = evaluate(_record(), 5000.0 + SETTINGS.slack_seconds, LiveContext(identity=A), SETTINGS)
        assert (r.ready, r.reason) == (True, "limit-reset")

    def test_before_reset_on_the_same_account_waits(self):
        r = evaluate(_record(), 2000.0, LiveContext(identity=A), SETTINGS)
        assert (r.ready, r.reason) == (False, "waiting-for-reset")
        assert 0 < r.wait_s <= rs.POLL_S

    def test_account_change_since_the_stop_is_ready(self):
        r = evaluate(_record(), 2000.0, LiveContext(identity=B), SETTINGS)
        assert (r.ready, r.reason) == (True, "account-switched")

    def test_auto_switch_after_the_stop_is_ready_even_if_identity_looks_same(self):
        # The hook can read the identity after a switch already landed.
        r = evaluate(_record(), 2000.0, LiveContext(identity=A, last_switch_at=1500.0), SETTINGS)
        assert r.ready and r.reason == "account-switched"

    def test_switch_before_the_stop_does_not_count(self):
        r = evaluate(_record(), 2000.0, LiveContext(identity=A, last_switch_at=900.0), SETTINGS)
        assert not r.ready

    def test_switched_onto_an_exhausted_account_waits(self):
        live = LiveContext(identity=B, usage=_usage(100.0, "2099-01-01T00:00:00Z"))
        r = evaluate(_record(), 2000.0, live, SETTINGS)
        assert (r.ready, r.reason) == (False, "switched-account-exhausted")

    def test_exhausted_reading_whose_window_already_reset_counts_as_free(self):
        # The fake clock reads 2000.0 (1970-01-01T00:33:20Z); this window reset before it.
        live = LiveContext(identity=B, usage=_usage(100.0, "1970-01-01T00:00:01Z"))
        assert evaluate(_record(), 2000.0, live, SETTINGS).ready

    def test_on_switch_off_ignores_switches(self):
        settings = ResumeSettings(on_switch=False)
        assert not evaluate(_record(), 2000.0, LiveContext(identity=B), settings).ready

    def test_session_profile_ignores_switches(self):
        record = _record(default_profile=False)
        assert not evaluate(record, 2000.0, LiveContext(identity=B), SETTINGS).ready
        assert evaluate(record, 5000.0 + 90.0, LiveContext(), SETTINGS).ready

    @pytest.mark.parametrize(
        ("pct", "fetched_at", "ready"),
        [
            (40.0, 1000.0 + rs.HEADROOM_EVIDENCE_AFTER_S, True),
            (40.0, 1010.0, False),  # fetched too soon after the stop to prove anything
            (99.0, 1500.0, False),  # still at the cap: the API is refusing
            (None, 1500.0, False),
        ],
    )
    def test_same_account_headroom_needs_fresh_evidence(self, pct, fetched_at, ready):
        usage = _usage(pct) if pct is not None else None
        live = LiveContext(identity=A, usage=usage, usage_fetched_at=fetched_at)
        r = evaluate(_record(), 2000.0, live, SETTINGS)
        assert r.ready is ready
        if ready:
            assert r.reason == "headroom"

    def test_unknown_reset_waits_for_capacity(self):
        r = evaluate(_record(resets_at=None), 2000.0, LiveContext(identity=A), SETTINGS)
        assert (r.ready, r.reason) == (False, "waiting-for-capacity")


def _stall(uuid: str, at: float) -> tx.Stall:
    return tx.Stall(uuid=uuid, at=at, resets_at=RESETS_AT, limit_type="five_hour", text="")


class TestNoteStall:
    def test_first_stop_starts_the_count_at_zero(self):
        record = _record(stall_uuid=None)
        out = note_stall(
            record, _stall("s1", 10.0), tx.TranscriptState(tx.STALLED), SETTINGS, A, 10.0
        )
        assert out.consecutive == 0 and out.state == rs.WAITING
        assert out.stall_uuid == "s1" and out.resets_at == RESETS_AT

    def test_stop_right_after_our_resume_counts_and_eventually_gives_up(self):
        record = _record(last_resume_at=100.0, resumed_stall_uuid="s1", consecutive=1)
        state = tx.TranscriptState(tx.STALLED)  # no progress since the resume
        out = note_stall(record, _stall("s2", 110.0), state, SETTINGS, A, 110.0)
        assert out.consecutive == 2 and out.state == rs.WAITING
        out = note_stall(
            replace(out, resumed_stall_uuid="s2"), _stall("s3", 120.0), state, SETTINGS, A, 120.0
        )
        assert out.consecutive == 3 and out.state == rs.GAVE_UP

    def test_progress_since_the_resume_resets_the_count(self):
        record = _record(last_resume_at=100.0, resumed_stall_uuid="s1", consecutive=2)
        state = tx.TranscriptState(tx.STALLED, progress_times=(150.0,))
        out = note_stall(record, _stall("s2", 200.0), state, SETTINGS, A, 200.0)
        assert out.consecutive == 0

    def test_a_prompt_typed_by_hand_resets_the_count(self):
        record = _record(last_resume_at=100.0, resumed_stall_uuid="s1", consecutive=2)
        state = tx.TranscriptState(tx.STALLED, human_prompt_times=(150.0,))
        out = note_stall(record, _stall("s2", 200.0), state, SETTINGS, A, 200.0)
        assert out.consecutive == 0


# ---------------------------------------------------------------------------
# arm + waiter
# ---------------------------------------------------------------------------


@pytest.fixture
def env_home(temp_home: Path) -> Path:
    (temp_home / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": A[0]}}))
    return temp_home


@pytest.fixture
def backup(tmp_path: Path) -> Path:
    path = tmp_path / "backup"
    path.mkdir()
    return path


def _hook_input(transcript: Path) -> dict:
    return {
        "session_id": SID,
        "transcript_path": str(transcript),
        "cwd": "C:/work/proj",
        "hook_event_name": "StopFailure",
        "error": "rate_limit",
    }


ENV = {"CLAUDE_CODE_MESSAGING_SOCKET": SOCKET, "CLAUDE_CODE_MESSAGING_TOKEN": CHILD_TOKEN}


class TestArm:
    def test_bad_input_does_nothing(self, backup):
        assert arm(backup, {"session_id": "../../etc"}, ENV) == "bad-input"
        assert rs.list_records(backup) == []

    def test_records_the_stop_and_spawns_one_waiter(self, env_home, backup, tmp_path):
        transcript = write(tmp_path / "t.jsonl", human("h1", 900.0), limit_stop("s1", 1000.0))
        spawned: list[str] = []
        outcome = arm(
            backup, _hook_input(transcript), ENV, clock=lambda: 1001.0, spawn=spawned.append
        )
        assert outcome == "armed"
        assert spawned == [SID]
        record = rs.load_record(backup, SID)
        assert record.state == rs.WAITING
        assert (record.stall_uuid, record.resets_at, record.limit_type) == (
            "s1",
            RESETS_AT,
            "five_hour",
        )
        assert record.identity == A
        assert record.socket_path == SOCKET
        assert record.default_profile is True
        # The token is never written to disk.
        assert CHILD_TOKEN not in json.dumps(record.to_json())

    def test_repeat_events_with_a_live_waiter_spawn_nothing(self, env_home, backup, tmp_path):
        transcript = write(tmp_path / "t.jsonl", limit_stop("s1", 1000.0))
        spawned: list[str] = []
        arm(backup, _hook_input(transcript), ENV, clock=lambda: 1001.0, spawn=spawned.append)
        lock = rs._waiter_lock(backup, SID)
        assert lock.acquire(timeout=0)  # stands in for the running waiter
        try:
            for _ in range(5):
                outcome = arm(
                    backup, _hook_input(transcript), ENV, clock=lambda: 1002.0, spawn=spawned.append
                )
                assert outcome == "already-waiting"
        finally:
            lock.release()
        assert spawned == [SID]

    def test_event_for_a_stop_already_resumed_is_ignored(self, env_home, backup, tmp_path):
        transcript = write(tmp_path / "t.jsonl", limit_stop("s1", 1000.0))
        rs.mutate_record(backup, SID, lambda _: _record(state=rs.RESUMED, resumed_stall_uuid="s1"))
        spawned: list[str] = []
        outcome = arm(
            backup, _hook_input(transcript), ENV, clock=lambda: 1001.0, spawn=spawned.append
        )
        assert outcome == "already-resumed"
        assert spawned == []

    def test_stop_not_flushed_yet_still_arms(self, env_home, backup, tmp_path):
        # The hook can beat the transcript write: arm anyway, the waiter
        # picks the stop up once it lands.
        transcript = write(tmp_path / "t.jsonl", human("h1", 900.0))
        spawned: list[str] = []
        assert (
            arm(backup, _hook_input(transcript), ENV, clock=lambda: 1001.0, spawn=spawned.append)
            == "armed"
        )
        record = rs.load_record(backup, SID)
        assert record.stall_uuid is None and record.state == rs.WAITING
        assert spawned == [SID]

    def test_loop_guard_gives_up_and_notifies(self, env_home, backup, tmp_path):
        transcript = write(tmp_path / "t.jsonl", limit_stop("s3", 1200.0))
        rs.mutate_record(
            backup,
            SID,
            lambda _: _record(
                state=rs.RESUMED,
                stall_uuid="s2",
                resumed_stall_uuid="s2",
                last_resume_at=1100.0,
                consecutive=SETTINGS.max_consecutive - 1,
            ),
        )
        spawned: list[str] = []
        stopped: list[tuple[str, str]] = []
        outcome = arm(
            backup,
            _hook_input(transcript),
            ENV,
            clock=lambda: 1201.0,
            spawn=spawned.append,
            on_gave_up=lambda name, detail: stopped.append((name, detail)),
        )
        assert outcome == "gave-up"
        assert spawned == []
        assert stopped and "automatic continues" in stopped[0][1]
        assert rs.load_record(backup, SID).state == rs.GAVE_UP

    def test_recent_moved_on_damps_helper_query_spam(self, env_home, backup, tmp_path):
        transcript = write(tmp_path / "t.jsonl", assistant("a1", 1000.0))
        rs.mutate_record(backup, SID, lambda _: _record(state=rs.MOVED_ON, updated_at=1000.0))
        spawned: list[str] = []
        outcome = arm(
            backup, _hook_input(transcript), ENV, clock=lambda: 1010.0, spawn=spawned.append
        )
        assert outcome == "recently-moved-on" and spawned == []

    def test_cswap_run_profile_is_not_a_default_profile(self, env_home, backup, tmp_path):
        profile = backup / "sessions" / "2-b-example-com"
        profile.mkdir(parents=True)
        transcript = write(tmp_path / "t.jsonl", limit_stop("s1", 1000.0))
        with patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": str(profile)}):
            arm(backup, _hook_input(transcript), ENV, spawn=lambda _: None)
        assert rs.load_record(backup, SID).default_profile is False


def _session(status: str | None = "idle", socket: str = SOCKET) -> ClaudeSession:
    return ClaudeSession(
        pid=4242,
        session_id=SID,
        cwd="C:/work/proj",
        started_at=0,
        kind="interactive",
        entrypoint="claude-vscode",
        status=status,
        messaging_socket_path=socket,
        name="proj-1",
        proc_start="123",
    )


class WaiterHarness:
    def __init__(self, backup: Path, transcript: Path, record: ResumeRecord, live: LiveContext):
        self.backup = backup
        self.transcript = transcript
        self.clock = FakeClock(record.armed_at)
        self.live = live
        self.sessions: list[ClaudeSession | None] = [_session()]
        self.posted: list[tuple[str, str, str | None]] = []
        self.resumed: list[tuple[str, str]] = []
        self.gave_up: list[tuple[str, str]] = []
        self.post_error: Exception | None = None
        self.records_delivery = True
        self.delivery_log = transcript  # where Claude Code records an accepted message
        rs.mutate_record(backup, SID, lambda _: replace(record, transcript_path=str(transcript)))
        self.waiter = Waiter(
            backup,
            SID,
            env=ENV,
            clock=self.clock,
            sleep=self.clock.sleep,
            live_context=lambda _r: self.live,
            on_resumed=lambda n, w: self.resumed.append((n, w)),
            on_gave_up=lambda n, d: self.gave_up.append((n, d)),
        )

    def _find(self, _home, _sid):
        return self.sessions[0] if len(self.sessions) == 1 else self.sessions.pop(0)

    def _post(self, address, text, token, timeout=5.0):
        if self.post_error is not None:
            err, self.post_error = self.post_error, None
            raise err
        self.posted.append((address, text, token))
        if self.records_delivery:
            # What Claude Code writes when it accepts the message: a meta user
            # turn carrying the text (nothing at all when it holds it).
            with self.delivery_log.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(peer_turn(f"m{len(self.posted)}", self.clock.now, text)) + "\n")

    def run(self) -> str:
        with (
            patch.object(rs, "find_session", side_effect=self._find),
            patch.object(rs, "post_message", side_effect=self._post),
        ):
            return self.waiter.run()

    def record(self) -> ResumeRecord:
        return rs.load_record(self.backup, SID)


def peer_turn(uuid: str, at: float, text: str) -> dict:
    return {
        "type": "user",
        "uuid": uuid,
        "timestamp": iso_ts(at),
        "isMeta": True,
        "origin": {"kind": "peer", "from": "unknown", "selfSent": True},
        "message": {"role": "user", "content": text},
    }


@pytest.fixture
def stalled_transcript(tmp_path: Path) -> Path:
    return write(tmp_path / "t.jsonl", human("h1", 900.0), limit_stop("s1", 1000.0))


class TestWaiter:
    def test_resumes_after_the_reset_with_the_own_child_token(self, backup, stalled_transcript):
        h = WaiterHarness(
            backup, stalled_transcript, _record(resets_at=1300.0), LiveContext(identity=A)
        )
        assert h.run() == rs.RESUMED
        assert h.clock.now >= 1300.0 + SETTINGS.slack_seconds
        [(address, text, token)] = h.posted
        assert address == SOCKET and token == CHILD_TOKEN
        assert "limit window reset" in text and "Continue the task" in text
        record = h.record()
        assert record.state == rs.RESUMED and record.resumed_stall_uuid == "s1"
        assert h.resumed == [("proj-1", rs.REASON_TEXT["limit-reset"])]

    def test_account_switch_resumes_without_waiting_for_the_reset(self, backup, stalled_transcript):
        h = WaiterHarness(
            backup, stalled_transcript, _record(resets_at=90_000.0), LiveContext(identity=B)
        )
        assert h.run() == rs.RESUMED
        assert h.clock.now < 1000.0 + rs.JITTER_S + 1  # only the jitter elapsed
        assert "switched" in h.posted[0][1]

    def test_waits_for_a_busy_session_to_go_idle(self, backup, stalled_transcript):
        h = WaiterHarness(backup, stalled_transcript, _record(), LiveContext(identity=B))
        h.sessions = [_session("busy"), _session("busy"), _session("idle")]
        assert h.run() == rs.RESUMED
        assert len(h.posted) == 1

    def test_stands_down_when_the_session_moves_on(self, backup, tmp_path):
        transcript = write(
            tmp_path / "t.jsonl",
            limit_stop("s1", 1000.0),
            human("h2", 1100.0),
            assistant("a2", 1110.0),
        )
        h = WaiterHarness(backup, transcript, _record(), LiveContext(identity=B))
        assert h.run() == rs.MOVED_ON
        assert h.posted == []

    def test_stands_down_when_the_session_closes(self, backup, stalled_transcript):
        h = WaiterHarness(backup, stalled_transcript, _record(), LiveContext(identity=A))
        h.sessions = [None]
        assert h.run() == rs.SESSION_GONE
        assert h.posted == [] and h.record().state == rs.SESSION_GONE

    def test_force_flag_delivers_immediately(self, backup, stalled_transcript):
        h = WaiterHarness(
            backup, stalled_transcript, _record(resets_at=90_000.0), LiveContext(identity=A)
        )
        rs.force_flag(backup, SID).touch()
        assert h.run() == rs.RESUMED
        assert h.clock.now == 1000.0  # no jitter, no waiting
        assert "you asked" in h.posted[0][1]
        assert not rs.force_flag(backup, SID).exists()

    def test_inbox_failure_keeps_waiting_then_delivers(self, backup, stalled_transcript):
        from claude_swap.inbox import InboxError

        h = WaiterHarness(backup, stalled_transcript, _record(), LiveContext(identity=B))
        h.post_error = InboxError("pipe busy")
        assert h.run() == rs.RESUMED
        assert len(h.posted) == 1

    def test_falls_back_to_the_peer_token_for_a_foreign_inbox(self, backup, tmp_path):
        # A session that prompts for permissions accepts peer messages.
        transcript = write(
            tmp_path / "t.jsonl", human("h1", 900.0, mode="acceptEdits"), limit_stop("s1", 1000.0)
        )
        h = WaiterHarness(backup, transcript, _record(), LiveContext(identity=B))
        h.sessions = [_session(socket=r"\\.\pipe\LOCAL\cc-msg-other")]
        with patch.object(rs, "peer_token", return_value="p" * 32) as mock_peer:
            assert h.run() == rs.RESUMED
        assert h.posted[0][2] == "p" * 32
        mock_peer.assert_called_once()

    def test_unflushed_stop_is_picked_up_when_it_lands(self, backup, tmp_path):
        transcript = write(tmp_path / "t.jsonl", assistant("a0", 990.0))
        h = WaiterHarness(
            backup,
            transcript,
            _record(stall_uuid=None, stalled_at=None, resets_at=None),
            LiveContext(identity=B),
        )
        sleeps = {"n": 0}
        original = h.clock.sleep

        def sleep_then_flush(seconds: float) -> None:
            original(seconds)
            sleeps["n"] += 1
            if sleeps["n"] == 2:
                write(transcript, assistant("a0", 990.0), limit_stop("s9", 1001.0))

        h.waiter.sleep = sleep_then_flush
        assert h.run() == rs.RESUMED
        assert h.record().resumed_stall_uuid == "s9"

    def test_no_stop_after_the_grace_period_stands_down(self, backup, tmp_path):
        transcript = write(tmp_path / "t.jsonl", assistant("a0", 990.0))
        h = WaiterHarness(
            backup,
            transcript,
            _record(stall_uuid=None, stalled_at=None, resets_at=None),
            LiveContext(identity=A),
        )
        assert h.run() == rs.MOVED_ON
        assert h.clock.now - 1000.0 > rs.ARM_GRACE_S

    def test_only_one_waiter_per_session(self, backup, stalled_transcript):
        h = WaiterHarness(backup, stalled_transcript, _record(), LiveContext(identity=A))
        lock = rs._waiter_lock(backup, SID)
        assert lock.acquire(timeout=0)
        try:
            assert h.run() == "already-waiting"
        finally:
            lock.release()


def test_finished_records_are_pruned_after_the_retention_window(backup):
    rs.mutate_record(backup, SID, lambda _: _record(state=rs.RESUMED, updated_at=0.0))
    rs.prune_records(backup, rs.RECORD_RETENTION_S - 1)
    assert rs.load_record(backup, SID) is not None
    rs.prune_records(backup, rs.RECORD_RETENTION_S + 1)
    assert rs.load_record(backup, SID) is None


def test_waiting_records_are_never_pruned(backup):
    rs.mutate_record(backup, SID, lambda _: _record(state=rs.WAITING, updated_at=0.0))
    rs.prune_records(backup, rs.RECORD_RETENTION_S * 10)
    assert rs.load_record(backup, SID) is not None


class TestOrphanedWaitingRecords:
    """A sign-out or reboot kills waiters mid-wait (2026-11-03: Windows'
    Fast Startup "shut down" at 10:18 left two records `waiting` for good)."""

    def _orphan(self, backup, tmp_path, *records: dict, **fields) -> None:
        transcript = write(tmp_path / "t.jsonl", human("h1", 900.0), *records)
        record = _record(transcript_path=str(transcript), **fields)
        rs.mutate_record(backup, SID, lambda _: record)

    def _reconcile(self, backup, sessions=()) -> ResumeRecord:
        with patch.object(rs, "list_sessions", return_value=list(sessions)):
            rs.reconcile_orphans(backup, 9000.0)
        record = rs.load_record(backup, SID)
        assert record is not None
        return record

    def test_a_session_that_carried_on_is_finished_as_moved_on(self, backup, tmp_path):
        self._orphan(backup, tmp_path, limit_stop("s1", 1000.0), assistant("a1", 2000.0))
        record = self._reconcile(backup, [_session("busy")])
        assert record.state == rs.MOVED_ON
        assert record.updated_at == 9000.0

    def test_a_session_no_longer_running_is_finished_as_gone(self, backup, tmp_path):
        self._orphan(backup, tmp_path, limit_stop("s1", 1000.0))
        assert self._reconcile(backup, []).state == rs.SESSION_GONE

    def test_a_running_session_still_stopped_keeps_waiting(self, backup, tmp_path):
        # Nothing to conclude: `resume status` flags the missing waiter.
        self._orphan(backup, tmp_path, limit_stop("s1", 1000.0))
        assert self._reconcile(backup, [_session()]).state == rs.WAITING

    def test_a_live_waiter_keeps_its_record(self, backup, tmp_path):
        # Mid-delivery: our own continue moved the session on; the waiter
        # records `resumed` once it confirms.
        self._orphan(backup, tmp_path, limit_stop("s1", 1000.0), assistant("a1", 2000.0))
        lock = rs._waiter_lock(backup, SID)
        assert lock.acquire(timeout=0)
        try:
            assert self._reconcile(backup, [_session("busy")]).state == rs.WAITING
        finally:
            lock.release()

    def test_a_live_stopped_session_never_has_its_waiter_lock_probed(self, backup, tmp_path):
        # A waiter just starting must never lose its lock to the probe.
        self._orphan(backup, tmp_path, limit_stop("s1", 1000.0))
        with (
            patch.object(rs, "list_sessions", return_value=[_session()]),
            patch.object(rs, "waiter_alive", side_effect=AssertionError("probed")),
        ):
            rs.reconcile_orphans(backup, 9000.0)

    def test_rehearsals_are_left_to_their_waiter(self, backup, tmp_path):
        self._orphan(
            backup, tmp_path, limit_stop("s1", 1000.0), assistant("a1", 2000.0), rehearsal=True
        )
        assert self._reconcile(backup, []).state == rs.WAITING

    def test_any_sessions_hook_finishes_another_sessions_orphan(self, backup, env_home, tmp_path):
        self._orphan(backup, tmp_path, limit_stop("s1", 1000.0), assistant("a1", 2000.0))
        other = "0000aaaa-1111-4222-8333-444455556666"
        stopped = write(tmp_path / "other.jsonl", human("h9", 5000.0), limit_stop("s9", 6000.0))
        hook = {**_hook_input(stopped), "session_id": other}
        with patch.object(rs, "list_sessions", return_value=[_session("busy")]):
            assert arm(backup, hook, ENV, clock=lambda: 6001.0, spawn=lambda _sid: None) == "armed"
        assert rs.load_record(backup, SID).state == rs.MOVED_ON
        assert rs.load_record(backup, other).state == rs.WAITING


class TestDeliveryConfirmation:
    def test_unrecorded_message_is_reported_undelivered(self, backup, stalled_transcript):
        h = WaiterHarness(backup, stalled_transcript, _record(), LiveContext(identity=B))
        h.records_delivery = False  # Claude Code held it: no trace in the transcript
        assert h.run() == rs.UNDELIVERED
        assert len(h.posted) == 1
        record = h.record()
        assert record.state == rs.UNDELIVERED and "never recorded" in record.detail
        assert h.resumed == [] and h.gave_up and "type 'continue'" in h.gave_up[0][1]

    def test_peer_fallback_refuses_a_bypass_session_without_posting(
        self, backup, stalled_transcript
    ):
        # stalled_transcript's last prompt ran in bypassPermissions.
        h = WaiterHarness(backup, stalled_transcript, _record(), LiveContext(identity=B))
        h.sessions = [_session(socket=r"\\.\pipe\LOCAL\cc-msg-other")]
        with patch.object(rs, "peer_token", return_value="p" * 32):
            assert h.run() == rs.UNDELIVERED
        assert h.posted == []
        assert "bypasses permissions" in h.record().detail

    def test_peer_fallback_is_allowed_when_the_user_accepts_inbound(
        self, backup, stalled_transcript, tmp_path
    ):
        config_home = tmp_path / "cfg"
        config_home.mkdir()
        (config_home / "settings.json").write_text(json.dumps({"crossSessionInbound": "accept"}))
        h = WaiterHarness(
            backup,
            stalled_transcript,
            _record(config_home=str(config_home)),
            LiveContext(identity=B),
        )
        h.sessions = [_session(socket=r"\\.\pipe\LOCAL\cc-msg-other")]
        with patch.object(rs, "peer_token", return_value="p" * 32):
            assert h.run() == rs.RESUMED

    def test_a_session_waiting_on_the_limit_dialog_still_gets_it(self, backup, stalled_transcript):
        h = WaiterHarness(backup, stalled_transcript, _record(), LiveContext(identity=B))
        h.sessions = [_session("waiting")]
        assert h.run() == rs.RESUMED

    def test_recorded_since_ignores_older_matches(self, tmp_path):
        transcript = write(tmp_path / "t.jsonl", peer_turn("m0", 500.0, rs.MARKER + " old"))
        assert tx.recorded_since(transcript, rs.MARKER, 400.0)
        assert not tx.recorded_since(transcript, rs.MARKER, 600.0)


class TestRehearsal:
    def _real_transcript(self, home: Path) -> Path:
        path = home / ".claude" / "projects" / "c--work-proj" / f"{SID}.jsonl"
        path.parent.mkdir(parents=True)
        write(path, human("h1", 900.0), assistant("a1", 950.0))
        return path

    def test_refuses_outside_a_session(self, env_home, backup):
        with pytest.raises(rs.RehearsalError, match="inside a Claude Code session"):
            rs.rehearse(backup, {})

    def test_arms_the_calling_session_without_touching_claude_files(self, env_home, backup):
        real = self._real_transcript(env_home)
        before = real.read_bytes()
        spawned: list[str] = []
        with patch.object(rs, "list_sessions", return_value=[_session()]):
            session, record = rs.rehearse(backup, ENV, clock=lambda: 1000.0, spawn=spawned.append)
        assert session.session_id == SID and spawned == [SID]
        assert record.rehearsal and record.confirm_path == str(real)
        assert record.transcript_path != str(real)
        assert tx.read_transcript_state(record.transcript_path).status == tx.STALLED
        assert real.read_bytes() == before  # Claude Code's transcript is never written
        assert rs.load_record(backup, SID).rehearsal is True

    def test_refuses_while_a_waiter_is_active(self, env_home, backup):
        self._real_transcript(env_home)
        lock = rs._waiter_lock(backup, SID)
        assert lock.acquire(timeout=0)
        try:
            with (
                patch.object(rs, "list_sessions", return_value=[_session()]),
                pytest.raises(rs.RehearsalError, match="already active"),
            ):
                rs.rehearse(backup, ENV, spawn=lambda _: None)
        finally:
            lock.release()

    def test_waiter_delivers_the_test_message_at_once_and_confirms_in_the_real_transcript(
        self, env_home, backup
    ):
        real = self._real_transcript(env_home)
        with patch.object(rs, "list_sessions", return_value=[_session()]):
            _session_obj, record = rs.rehearse(
                backup, ENV, clock=lambda: 1000.0, spawn=lambda _: None
            )
        stand_in = Path(record.transcript_path)
        h = WaiterHarness(backup, stand_in, record, LiveContext(identity=A))
        h.delivery_log = real
        assert h.run() == rs.RESUMED
        assert h.clock.now == 1000.0  # no reset wait, no jitter
        [(_address, text, token)] = h.posted
        assert "TEST" in text and "no task to continue" in text and token == CHILD_TOKEN
        assert h.resumed == [("proj-1", rs.REASON_TEXT["rehearsal"])]
        assert h.record().state == rs.RESUMED


# ---------------------------------------------------------------------------
# Intent controls (review 2026-11-02): held, paused, cancelled
# ---------------------------------------------------------------------------


class TestIntentPolicy:
    def test_stop_older_than_the_cap_is_held_not_continued(self):
        settings = ResumeSettings(max_wait_hours=24.0)
        old = 1000.0 + 24 * 3600 + 1
        # Even a reset that already passed doesn't override the hold.
        r = evaluate(_record(resets_at=1300.0), old, LiveContext(identity=B), settings)
        assert (r.ready, r.reason) == (False, "held")

    def test_zero_cap_means_no_cap(self):
        settings = ResumeSettings(max_wait_hours=0.0)
        late = 1000.0 + 30 * 24 * 3600
        assert evaluate(_record(resets_at=1300.0), late, LiveContext(identity=A), settings).ready

    def test_paused_never_auto_continues(self):
        settings = ResumeSettings(enabled=False)
        r = evaluate(_record(resets_at=1300.0), 5000.0, LiveContext(identity=B), settings)
        assert (r.ready, r.reason) == (False, "paused")

    def test_message_makes_a_wrong_continue_cheap(self):
        text = rs.MESSAGE.format(why="x")
        assert "re-check the current state" in text
        assert "stop without making further changes" in text
        assert "ask again briefly instead of guessing" in text


def _write_settings(backup: Path, **resume: object) -> None:
    (backup / "settings.json").write_text(json.dumps({"resume": resume}))


class TestIntentWaiter:
    def test_held_session_notifies_once_then_force_continues_it(self, backup, stalled_transcript):
        _write_settings(backup, maxWaitHours=1)
        record = _record(armed_at=1000.0, stalled_at=1000.0, resets_at=90_000.0)
        h = WaiterHarness(backup, stalled_transcript, record, LiveContext(identity=A))
        h.clock.now = 1000.0 + 2 * 3600  # already past the 1h cap
        sleeps = {"n": 0}
        original = h.clock.sleep

        def sleep_then_force(seconds: float) -> None:
            original(seconds)
            sleeps["n"] += 1
            if sleeps["n"] == 3:
                rs.force_flag(backup, SID).touch()

        h.waiter.sleep = sleep_then_force
        assert h.run() == rs.RESUMED
        held = [d for _, d in h.gave_up if d.startswith("held:")]
        assert len(held) == 1  # notified once, not every poll
        assert "you asked" in h.posted[0][1]

    def test_paused_waiter_holds_until_turned_back_on(self, backup, stalled_transcript):
        _write_settings(backup, enabled=False)
        h = WaiterHarness(backup, stalled_transcript, _record(), LiveContext(identity=B))
        sleeps = {"n": 0}
        original = h.clock.sleep

        def sleep_then_enable(seconds: float) -> None:
            original(seconds)
            sleeps["n"] += 1
            if sleeps["n"] == 2:
                assert h.posted == []  # nothing sent while paused
                _write_settings(backup, enabled=True)

        h.waiter.sleep = sleep_then_enable
        assert h.run() == rs.RESUMED
        assert len(h.posted) == 1

    def test_cancel_stands_the_waiter_down(self, backup, stalled_transcript):
        h = WaiterHarness(
            backup, stalled_transcript, _record(resets_at=90_000.0), LiveContext(identity=A)
        )
        original = h.clock.sleep

        def sleep_then_cancel(seconds: float) -> None:
            original(seconds)
            rs.mutate_record(backup, SID, lambda prev: replace(prev, state=rs.CANCELLED))

        h.waiter.sleep = sleep_then_cancel
        assert h.run() == rs.CANCELLED
        assert h.posted == []

    def test_cancel_during_the_pre_send_jitter_wins(self, backup, stalled_transcript):
        h = WaiterHarness(backup, stalled_transcript, _record(), LiveContext(identity=B))

        def jitter_sleep(seconds: float) -> None:
            h.clock.now += seconds
            rs.mutate_record(backup, SID, lambda prev: replace(prev, state=rs.CANCELLED))

        h.waiter.sleep = jitter_sleep
        assert h.run() == rs.CANCELLED
        assert h.posted == []


# ---------------------------------------------------------------------------
# Regressions from the 2026-11-03 episode: waiter windows, queued deliveries
# ---------------------------------------------------------------------------


class TestWaiterSpawnHasNoWindow:
    @pytest.mark.skipif(sys.platform != "win32", reason="Windows creation flags")
    def test_no_window_and_never_detached(self):
        import subprocess

        with patch.object(rs.subprocess, "Popen") as popen:
            rs.spawn_waiter(SID)
        flags = popen.call_args.kwargs["creationflags"]
        assert flags & subprocess.CREATE_NO_WINDOW
        # DETACHED_PROCESS leaves the pipx launcher's child to open a visible console.
        assert not flags & subprocess.DETACHED_PROCESS
        assert flags & subprocess.CREATE_NEW_PROCESS_GROUP

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows creation flags")
    def test_falls_back_without_breakaway_still_windowless(self):
        import subprocess

        calls = []

        def popen(cmd, **kwargs):
            calls.append(kwargs["creationflags"])
            if len(calls) == 1:
                raise OSError("job does not allow breakaway")

        with patch.object(rs.subprocess, "Popen", side_effect=popen):
            rs.spawn_waiter(SID)
        assert len(calls) == 2
        assert calls[1] & subprocess.CREATE_NO_WINDOW
        assert not calls[1] & subprocess.CREATE_BREAKAWAY_FROM_JOB

    def test_flags_are_zero_off_windows(self):
        with patch.object(rs.sys, "platform", "linux"):
            assert rs.windows_spawn_flags() == 0


class TestQueuedDeliveryConfirmation:
    def test_marker_recorded_late_while_busy_is_confirmed(self, tmp_path):
        transcript = write(tmp_path / "t.jsonl", limit_stop("s1", 1000.0))
        clock = FakeClock(1000.0)

        def sleep(seconds: float) -> None:
            clock.sleep(seconds)
            if clock.now >= 1000.0 + 75 and not getattr(sleep, "done", False):
                sleep.done = True  # type: ignore[attr-defined]
                with transcript.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(peer_turn("m1", clock.now, rs.MARKER + " x")) + "\n")

        assert rs.delivery_confirmed(
            str(transcript), 1000.0, clock=clock, sleep=sleep, still_busy=lambda: True
        )
        assert clock.now >= 1075.0  # it waited past the 60 s base timeout

    def test_idle_with_no_trace_is_still_undelivered(self, tmp_path):
        transcript = write(tmp_path / "t.jsonl", limit_stop("s1", 1000.0))
        clock = FakeClock(1000.0)
        assert not rs.delivery_confirmed(
            str(transcript), 1000.0, clock=clock, sleep=clock.sleep, still_busy=lambda: False
        )
        assert clock.now < 1000.0 + rs.CONFIRM_TIMEOUT_S + 5

    def test_busy_forever_gives_up_at_the_cap(self, tmp_path):
        transcript = write(tmp_path / "t.jsonl", limit_stop("s1", 1000.0))
        clock = FakeClock(1000.0)
        assert not rs.delivery_confirmed(
            str(transcript), 1000.0, clock=clock, sleep=clock.sleep, still_busy=lambda: True
        )
        assert clock.now >= 1000.0 + rs.CONFIRM_BUSY_MAX_S

    def test_waiter_counts_a_message_queued_behind_the_users_prompt(
        self, backup, stalled_transcript
    ):
        # The 2026-11-03 saas-template case: the user typed as we posted, so
        # our message was queued and only recorded ~70 s later.
        h = WaiterHarness(backup, stalled_transcript, _record(), LiveContext(identity=B))
        h.records_delivery = False
        h.sessions = [_session("idle"), _session("idle"), _session("busy")]
        original = h.clock.sleep
        posted = {"at": None}
        real_post = h._post

        def post(address, text, token, timeout=5.0):
            real_post(address, text, token, timeout)
            posted["at"] = h.clock.now

        h._post = post  # type: ignore[method-assign]

        def sleep(seconds: float) -> None:
            original(seconds)
            if (
                posted["at"] is not None
                and h.clock.now >= posted["at"] + 70
                and h.records_delivery is False
            ):
                h.records_delivery = None  # write once
                with stalled_transcript.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(peer_turn("q1", h.clock.now, rs.MARKER + " queued")) + "\n")

        h.waiter.sleep = sleep
        assert h.run() == rs.RESUMED
        assert h.gave_up == []  # no false "undelivered" alarm
