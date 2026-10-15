"""Tests for `cswap rc`: reconnecting Remote Control after a swap."""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import cli
from claude_swap import remote_control as rc
from claude_swap.exceptions import SessionError

# Entries as Claude Code 2.1.285 writes them into the transcript.
ACCOUNT_CHANGED = (
    "Remote Control disconnected — signed-in claude.ai account or organization "
    "changed on this machine — run /remote-control to start a session for the "
    "current account, or /login to switch back, then /remote-control"
)
UNVERIFIED = "Remote Control could not verify the signed-in account — run /remote-control to reconnect"
UNAVAILABLE = "Remote Control disconnected — Previous session is unavailable — run /remote-control to start a new one"
NETWORK = (
    "Remote Control disconnected — could not reach the Remote Control server "
    "for about 30 minutes — run /remote-control to reconnect"
)
ENDED_ELSEWHERE = "Remote Control disconnected — the session was ended from another device"
NEEDS_LOGIN = "Remote Control disconnected — OAuth token unavailable — run /login to restore Remote Control"
ACTIVE = "/remote-control is active · https://claude.ai/code/session_01abc"


def notice(content: object) -> dict:
    return {"type": "system", "subtype": "informational", "level": "warning", "content": content}


def active(content: str = ACTIVE) -> dict:
    return {"type": "system", "subtype": "bridge_status", "content": content}


class TestTranscriptEvent:
    @pytest.mark.parametrize("content", [ACCOUNT_CHANGED, UNVERIFIED, UNAVAILABLE, NETWORK])
    def test_reconnectable_notices(self, content):
        assert rc.transcript_event(notice(content)) == (rc.NOTICE, content)

    @pytest.mark.parametrize("content", [ENDED_ELSEWHERE, NEEDS_LOGIN])
    def test_deliberate_or_login_notices_are_left_alone(self, content):
        # Ended from another device was the user's choice; a /login notice
        # needs a human before /remote-control can work.
        assert rc.transcript_event(notice(content)) is None

    def test_active_status(self):
        assert rc.transcript_event(active()) == (rc.CONNECTED, ACTIVE)

    def test_active_status_needs_the_bridge_status_subtype(self):
        assert rc.transcript_event(notice(ACTIVE)) is None
        assert rc.transcript_event(active("/remote-control is connecting")) is None

    def test_only_system_entries(self):
        # The same words quoted in a user or assistant message are not events.
        for kind in ("user", "assistant"):
            assert rc.transcript_event({"type": kind, "content": ACCOUNT_CHANGED}) is None
            assert rc.transcript_event({"type": kind, "subtype": "bridge_status", "content": ACTIVE}) is None

    def test_notice_must_start_with_remote_control(self):
        assert rc.transcript_event(notice(f"quoted: {ACCOUNT_CHANGED}")) is None

    @pytest.mark.parametrize("entry", [None, "text", [], {"type": "system"}, notice(None), notice(42)])
    def test_malformed_entries(self, entry):
        assert rc.transcript_event(entry) is None


class Session:
    """A fake Claude Code session: its pid record and transcript on disk."""

    def __init__(self, claude_dir: Path, pid: int = 4242, session_id: str = "sid-1"):
        self.claude_dir = claude_dir
        self.pid = pid
        (claude_dir / "sessions").mkdir(parents=True, exist_ok=True)
        (claude_dir / "projects" / "-work-app").mkdir(parents=True, exist_ok=True)
        self.record(sessionId=session_id)

    def record(self, **fields) -> None:
        self.state = {"pid": self.pid, "kind": "interactive", **fields}
        (self.claude_dir / "sessions" / f"{self.pid}.json").write_text(json.dumps(self.state))

    def transcript(self, session_id: str | None = None) -> Path:
        sid = session_id or self.state["sessionId"]
        return self.claude_dir / "projects" / "-work-app" / f"{sid}.jsonl"

    def append(self, *entries: dict, session_id: str | None = None, raw: bytes = b"") -> None:
        with open(self.transcript(session_id), "ab") as f:
            for entry in entries:
                f.write(json.dumps(entry).encode() + b"\n")
            f.write(raw)


class TestSessionWatcher:
    def test_entries_already_in_transcript_are_ignored(self, tmp_path):
        session = Session(tmp_path)
        session.append({"type": "user", "content": "hi"}, notice(ACCOUNT_CHANGED), active())
        watcher = rc.SessionWatcher(session.pid, tmp_path)

        assert watcher.poll() == []
        session.append(notice(ACCOUNT_CHANGED))
        assert watcher.poll() == [(rc.NOTICE, ACCOUNT_CHANGED)]
        assert watcher.poll() == []  # each entry is reported once

    def test_events_come_in_order(self, tmp_path):
        session = Session(tmp_path)
        watcher = rc.SessionWatcher(session.pid, tmp_path)
        watcher.poll()
        session.append(notice(ACCOUNT_CHANGED), {"type": "user", "content": "x"}, active())
        assert watcher.poll() == [(rc.NOTICE, ACCOUNT_CHANGED), (rc.CONNECTED, ACTIVE)]

    def test_fresh_transcript_is_read_from_the_start(self, tmp_path):
        # A new conversation's transcript appears with its first entry, after
        # the watcher has already seen the session id.
        session = Session(tmp_path)
        watcher = rc.SessionWatcher(session.pid, tmp_path)
        assert watcher.poll() == []

        session.append({"type": "user", "content": "hi"}, notice(ACCOUNT_CHANGED))
        assert watcher.poll() == [(rc.NOTICE, ACCOUNT_CHANGED)]

    def test_resumed_conversation_is_read_from_its_end(self, tmp_path):
        session = Session(tmp_path)
        watcher = rc.SessionWatcher(session.pid, tmp_path)
        watcher.poll()
        session.append(notice(ACCOUNT_CHANGED), session_id="sid-2")

        session.record(sessionId="sid-2")  # /resume into an older conversation
        assert watcher.poll() == []
        assert watcher.session_id == "sid-2"
        session.append(notice(UNVERIFIED), session_id="sid-2")
        assert watcher.poll() == [(rc.NOTICE, UNVERIFIED)]

    def test_partial_line_waits_for_its_end(self, tmp_path):
        session = Session(tmp_path)
        watcher = rc.SessionWatcher(session.pid, tmp_path)
        session.append({"type": "user", "content": "hi"})
        watcher.poll()

        line = json.dumps(notice(ACCOUNT_CHANGED)).encode()
        session.append(raw=line[:40])
        assert watcher.poll() == []
        session.append(raw=line[40:] + b"\n")
        assert watcher.poll() == [(rc.NOTICE, ACCOUNT_CHANGED)]

    def test_truncated_transcript_resyncs(self, tmp_path):
        session = Session(tmp_path)
        session.append(*[{"type": "user", "content": "x" * 100}] * 5)
        watcher = rc.SessionWatcher(session.pid, tmp_path)
        watcher.poll()

        session.transcript().write_text("")
        assert watcher.poll() == []
        session.append(notice(ACCOUNT_CHANGED))
        assert watcher.poll() == [(rc.NOTICE, ACCOUNT_CHANGED)]

    def test_other_entries_are_skipped(self, tmp_path):
        session = Session(tmp_path)
        watcher = rc.SessionWatcher(session.pid, tmp_path)
        watcher.poll()
        session.append(
            notice(ENDED_ELSEWHERE),
            {"type": "system", "content": "Remote Control connected"},
            raw=b'{"type": "system", "content": "Remote Control disconnected \xe2\x80\x94 not json\n',
        )
        assert watcher.poll() == []

    def test_missing_or_torn_record(self, tmp_path):
        watcher = rc.SessionWatcher(999, tmp_path)
        assert watcher.poll() == []
        (tmp_path / "sessions").mkdir()
        (tmp_path / "sessions" / "999.json").write_text('{"pid": 99')
        assert watcher.poll() == []

    def test_defaults_to_claude_config_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        session = Session(tmp_path)
        watcher = rc.SessionWatcher(session.pid)
        watcher.poll()
        session.append(notice(ACCOUNT_CHANGED))
        assert watcher.poll() == [(rc.NOTICE, ACCOUNT_CHANGED)]

    @pytest.mark.parametrize(
        ("fields", "idle", "dialog_open"),
        [
            ({}, True, False),  # no turn has run yet
            ({"status": "idle"}, True, False),
            ({"status": "busy"}, False, False),
            ({"status": "shell"}, False, False),
            ({"status": "waiting", "waitingFor": "dialog open"}, False, True),  # the RC panel
            ({"status": "idle", "waitingFor": "permission"}, False, True),
        ],
    )
    def test_idle_and_dialog_open(self, tmp_path, fields, idle, dialog_open):
        session = Session(tmp_path)
        session.record(sessionId="sid-1", **fields)
        watcher = rc.SessionWatcher(session.pid, tmp_path)
        watcher.poll()
        assert watcher.idle is idle
        assert watcher.dialog_open is dialog_open


class TestKeyTracker:
    def test_typing_and_submitting(self):
        keys = rc.KeyTracker()
        assert keys.feed(b"abc") is None
        assert keys.feed(b"\r") == b"abc"
        assert keys.feed(b"  /rc \r") == b"/rc"

    def test_backspace_ctrl_c_and_ctrl_u(self):
        keys = rc.KeyTracker()
        assert keys.feed(b"/rcx\x7f\r") == b"/rc"
        assert keys.feed(b"abc\x15/rc\r") == b"/rc"
        assert keys.feed(b"abc\x03/rc\r") == b"/rc"

    def test_escape_sequences_are_ignored(self):
        keys = rc.KeyTracker()
        keys.feed(b"/r")
        keys.feed(b"\x1b[A")  # arrow up
        keys.feed(b"\x1b[I")  # focus in
        keys.feed(b"\x1b[<0;10;5M")  # mouse report
        assert keys.feed(b"c\r") == b"/rc"

    def test_a_paste_is_not_a_manual_rc(self):
        keys = rc.KeyTracker()
        keys.feed(b"\x1b[200~/rc\x1b[201~")
        assert keys.feed(b"\r") != b"/rc"

    def test_ctrl_s_may_hold_a_stash(self):
        keys = rc.KeyTracker()
        assert keys.stash_maybe_held is False
        keys.feed(b"half a prompt\x13")
        assert keys.stash_maybe_held is True

    def test_sending_a_message_restores_the_stash(self):
        keys = rc.KeyTracker()
        keys.feed(b"draft\x13")
        keys.feed(b"/model\r")  # slash commands don't pop the stash
        keys.feed(b"\r")  # nor does an empty Enter
        assert keys.stash_maybe_held is True
        keys.feed(b"fix the tests\r")
        assert keys.stash_maybe_held is False


def test_run_keys_stash_then_run_without_a_restore_key():
    # Claude Code restores the stashed draft itself; a second Ctrl+S would
    # stash it again and the draft would vanish.
    queued = rc.schedule_keys(rc.RUN_KEYS, 10.0)
    assert [data for _, data in queued] == [b"\x13", b"/remote-control", b"\r"]
    due = [t for t, _ in queued]
    assert due[0] == 10.0 and due == sorted(due)
    assert due[2] > due[1]  # text and Enter never share a write


class TestReconnector:
    """Times are seconds on the relay's clock; the notice lands at t=0."""

    def make(self, **kwargs):
        return rc.Reconnector(
            reconnect_delay_s=30, retry_delay_s=30, panel_check_s=3,
            confirm_within_s=20, user_idle_s=5, max_tries=3, window_s=900, **kwargs,
        )

    def step(self, reconnector, now, **overrides):
        kwargs = {
            "session_idle": True, "dialog_open": False,
            "last_input": -100.0, "stash_maybe_held": False, **overrides,
        }
        return reconnector.step(now, **kwargs)

    def send(self, reconnector, now):
        outcome, keys = self.step(reconnector, now)
        assert outcome == rc.SENT
        return keys[-1][0]  # when Enter is typed

    def test_nothing_owed(self):
        assert self.step(self.make(), 100.0) == (None, [])

    def test_waits_for_the_reconnect_delay(self):
        reconnector = self.make()
        reconnector.notice(0.0)
        assert self.step(reconnector, 29.9) == (None, [])
        outcome, keys = self.step(reconnector, 30.0)
        assert outcome == rc.SENT
        assert keys == rc.schedule_keys(rc.RUN_KEYS, 30.0)

    @pytest.mark.parametrize(
        "overrides",
        [{"session_idle": False}, {"last_input": 27.0}],
        ids=["busy-or-dialog", "user-typing"],
    )
    def test_waits_until_safe(self, overrides):
        reconnector = self.make()
        reconnector.notice(0.0)
        assert self.step(reconnector, 30.0, **overrides) == (None, [])
        assert reconnector.pending
        assert self.step(reconnector, 31.0)[0] == rc.SENT

    def test_confirmed_reconnect_clears_it(self):
        reconnector = self.make()
        reconnector.notice(0.0)
        sent = self.send(reconnector, 30.0)
        assert reconnector.connected() is False  # ours, not "came back without us"
        assert self.step(reconnector, sent + 1) == (None, [])  # not checked for the panel yet
        assert self.step(reconnector, sent + 3) == (None, [])  # checked: no panel
        assert self.step(reconnector, sent + 3.5) == (rc.BACK, [])
        assert not reconnector.active
        assert self.step(reconnector, sent + 100) == (None, [])

    def test_the_panel_is_closed_and_retried(self):
        reconnector = self.make()
        reconnector.notice(0.0)
        sent = self.send(reconnector, 30.0)
        outcome, keys = self.step(reconnector, sent + 3, dialog_open=True)
        assert (outcome, keys) == (rc.PANEL, [(sent + 3, rc.ESC)])
        assert reconnector.attempt is None and reconnector.pending
        assert self.step(reconnector, sent + 3 + 29.9) == (None, [])
        assert self.step(reconnector, sent + 3 + 30)[0] == rc.SENT

    def test_a_dialog_after_connecting_is_not_the_panel(self):
        reconnector = self.make()
        reconnector.notice(0.0)
        sent = self.send(reconnector, 30.0)
        reconnector.connected()
        assert self.step(reconnector, sent + 3, dialog_open=True) == (None, [])
        assert self.step(reconnector, sent + 3.5)[0] == rc.BACK

    def test_no_confirmation_is_retried(self):
        reconnector = self.make()
        reconnector.notice(0.0)
        sent = self.send(reconnector, 30.0)
        assert self.step(reconnector, sent + 3) == (None, [])
        assert self.step(reconnector, sent + 19.9) == (None, [])
        assert self.step(reconnector, sent + 20) == (rc.UNCONFIRMED, [])
        assert reconnector.pending and reconnector.attempt is None
        assert self.step(reconnector, sent + 49.9) == (None, [])
        assert self.step(reconnector, sent + 50)[0] == rc.SENT

    def test_coming_back_on_its_own_clears_it(self):
        # The user or Claude reconnected before the relay tried.
        reconnector = self.make()
        reconnector.notice(0.0)
        assert reconnector.connected() is True
        assert not reconnector.active
        assert self.step(reconnector, 30.0) == (None, [])

    @pytest.mark.parametrize("line", [b"/rc", b"/remote-control", b"  /rc "])
    def test_user_reconnecting_by_hand_cancels(self, line):
        reconnector = self.make()
        reconnector.notice(0.0)
        assert reconnector.submitted(line) is True
        assert self.step(reconnector, 30.0) == (None, [])

    def test_other_input_does_not_cancel(self):
        reconnector = self.make()
        reconnector.notice(0.0)
        assert reconnector.submitted(b"/rcfoo") is False
        assert reconnector.submitted(b"fix the tests") is False
        assert self.step(reconnector, 30.0)[0] == rc.SENT

    def test_a_held_stash_drops_the_reconnect(self):
        # Ctrl+S on an empty prompt would pop the user's stash into it.
        reconnector = self.make()
        reconnector.notice(0.0)
        assert self.step(reconnector, 30.0, stash_maybe_held=True) == (rc.STASH_HELD, [])
        assert not reconnector.active
        reconnector.notice(40.0)
        assert self.step(reconnector, 70.0)[0] == rc.SENT  # a skip isn't a try

    def test_rate_limit(self):
        reconnector = self.make()
        reconnector.notice(0.0)
        now = 30.0
        for _ in range(3):  # three tries that never confirm
            sent = self.send(reconnector, now)
            self.step(reconnector, sent + 3)
            assert self.step(reconnector, sent + 20)[0] == rc.UNCONFIRMED
            now = sent + 50
        assert self.step(reconnector, now) == (rc.RATE_LIMITED, [])
        assert not reconnector.active  # dropped, not retried in a loop

        reconnector.notice(900.0)
        assert self.step(reconnector, 930.0)[0] == rc.SENT  # the first try aged out


class TestLaunch:
    def test_refuses_on_windows(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        with pytest.raises(SessionError, match="POSIX"):
            rc.launch([])

    def test_claude_missing(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(rc.shutil, "which", lambda _name: None)
        with pytest.raises(SessionError, match="not found"):
            rc.launch([])

    def test_without_a_terminal_runs_claude_plainly(self, monkeypatch):
        # pytest's captured stdio is not a tty, like `claude -p` in a pipe.
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(rc.shutil, "which", lambda _name: "/bin/claude")
        calls = []

        def fake_execv(path, argv):
            calls.append((path, argv))
            raise SystemExit(0)

        monkeypatch.setattr(rc.os, "execv", fake_execv)
        monkeypatch.setattr(rc, "run_relay", lambda *a, **k: pytest.fail("no relay without a tty"))
        with pytest.raises(SystemExit):
            rc.launch(["-p", "hi"])
        assert calls == [("/bin/claude", ["/bin/claude", "-p", "hi"])]


class TestCli:
    def test_forwards_args_after_double_dash(self):
        with patch("claude_swap.remote_control.launch") as launch, \
             patch.object(sys, "argv", ["cswap", "rc", "--", "--resume", "--remote-control"]):
            cli.main()
        launch.assert_called_once_with(["--resume", "--remote-control"])

    def test_error_exits_1(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "platform", "win32")
        with patch.object(sys, "argv", ["cswap", "rc"]), pytest.raises(SystemExit) as excinfo:
            cli.main()
        assert excinfo.value.code == 1
        assert "POSIX" in capsys.readouterr().err

    def test_unknown_option_before_double_dash(self):
        with patch.object(sys, "argv", ["cswap", "rc", "--resume"]), pytest.raises(SystemExit) as excinfo:
            cli.main()
        assert excinfo.value.code == 2


# A stand-in for claude: keeps a session record and transcript like Claude
# Code, answers the first /remote-control with the Remote Control panel and
# the second with the active status, and reports every key it received.
FAKE_CLAUDE = textwrap.dedent(
    """
    import json, os, select, sys, time, tty
    claude_dir, out_path = sys.argv[1], sys.argv[2]
    pid, sid = os.getpid(), "sid-e2e"
    projects = os.path.join(claude_dir, "projects", "-work-app")
    os.makedirs(projects, exist_ok=True)
    os.makedirs(os.path.join(claude_dir, "sessions"), exist_ok=True)
    transcript = os.path.join(projects, sid + ".jsonl")
    notice = {"type": "system", "subtype": "informational", "content": %r}
    active = {"type": "system", "subtype": "bridge_status", "content": %r}

    def record(**fields):
        with open(os.path.join(claude_dir, "sessions", f"{pid}.json"), "w") as f:
            json.dump({"pid": pid, "sessionId": sid, **fields}, f)

    def append(entry):
        with open(transcript, "a") as f:
            f.write(json.dumps(entry) + "\\n")

    append(notice)  # from before the relay started: must not count
    record(status="idle")
    tty.setraw(0)
    start = time.monotonic()
    received, keys, runs, notice_at, done_at = b"", [], 0, None, None
    while time.monotonic() - start < 25:
        now = time.monotonic() - start
        if notice_at is None and now > 0.5:
            append(notice)
            notice_at = now
        if done_at is not None and now - done_at > 2.5:  # any stray keys by now?
            break
        if select.select([0], [], [], 0.05)[0]:
            data = os.read(0, 1024)
            received += data
            keys.append([round(now, 2), data.decode("latin-1")])
            if data == b"\\x1b":
                record(status="idle")  # Esc closed the panel
            elif received.endswith(b"/remote-control\\r"):
                runs += 1
                if runs == 1:
                    record(status="waiting", waitingFor="dialog open")  # too early: the panel
                else:
                    append(active)
                    done_at = now
    with open(out_path, "w") as f:
        json.dump({"notice_at": notice_at, "keys": keys, "received": received.decode("latin-1")}, f)
    sys.exit(7 if done_at is not None else 3)
    """
) % (ACCOUNT_CHANGED, ACTIVE)

FAKE_CLAUDE_WAITING_FOR_HUP = textwrap.dedent(
    """
    import signal, sys, time
    def hup(*_):
        open(sys.argv[1], "w").write("hup")
        sys.exit(9)
    signal.signal(signal.SIGHUP, hup)
    time.sleep(20)
    sys.exit(3)
    """
)


def _run_driver(driver: str, stdin: int, stdout: int) -> subprocess.Popen:
    """Start `driver` in its own session with the given terminal fds."""
    env = {**os.environ, "PYTHONPATH": str(Path(rc.__file__).parents[1])}
    return subprocess.Popen(
        [sys.executable, "-c", driver],
        stdin=stdin, stdout=stdout, stderr=stdout, env=env, start_new_session=True,
    )


@pytest.mark.skipif(sys.platform == "win32", reason="needs a pty")
def test_relay_reconnect_lifecycle(tmp_path):
    """The whole loop through a real pty: fake claude, real relay, short timings."""
    import pty

    fake = tmp_path / "fake_claude.py"
    fake.write_text(FAKE_CLAUDE)
    out = tmp_path / "out.json"
    claude_dir = tmp_path / "claude"
    driver = textwrap.dedent(
        f"""
        import sys
        from pathlib import Path
        from claude_swap import remote_control as rc
        rc.POLL_INTERVAL_S = 0.2
        rc.POLL_ACTIVE_S = 0.2
        reconnector = rc.Reconnector(
            reconnect_delay_s=1.5, retry_delay_s=1.0, panel_check_s=0.6, confirm_within_s=5,
        )
        sys.exit(rc.run_relay(
            sys.executable, [{str(fake)!r}, {str(claude_dir)!r}, {str(out)!r}],
            Path({str(claude_dir)!r}), reconnector,
        ))
        """
    )
    master, slave = pty.openpty()
    try:
        proc = _run_driver(driver, slave, slave)
        os.close(slave)
        deadline = time.monotonic() + 40
        while proc.poll() is None and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    os.read(master, 65536)  # keep the relay's output flowing
                except OSError:
                    time.sleep(0.05)
        if proc.poll() is None:
            proc.kill()
            pytest.fail("relay did not exit")
    finally:
        os.close(master)

    report = json.loads(out.read_text()) if out.exists() else {}
    assert proc.returncode == 7, report
    # Stash + command, Esc to close the panel, stash + command again: no
    # restore key, no third try, nothing else typed into the session.
    assert report["received"] == "\x13/remote-control\r\x1b\x13/remote-control\r", report
    first_key_at = report["keys"][0][0]
    assert first_key_at - report["notice_at"] >= 1.5, f"tried before the reconnect delay: {report}"


@pytest.mark.skipif(sys.platform == "win32", reason="needs a pty")
def test_relay_hangs_up_claude_when_the_terminal_goes(tmp_path):
    """stdin EOF (the terminal closed) forwards SIGHUP instead of orphaning claude."""
    import pty

    fake = tmp_path / "fake_claude.py"
    fake.write_text(FAKE_CLAUDE_WAITING_FOR_HUP)
    out = tmp_path / "out.txt"
    driver = textwrap.dedent(
        f"""
        import sys
        from pathlib import Path
        from claude_swap import remote_control as rc
        sys.exit(rc.run_relay(sys.executable, [{str(fake)!r}, {str(out)!r}], Path({str(tmp_path)!r})))
        """
    )
    in_master, in_slave = pty.openpty()
    out_master, out_slave = pty.openpty()
    try:
        proc = _run_driver(driver, in_slave, out_slave)
        os.close(in_slave)
        os.close(out_slave)
        time.sleep(1.5)  # let claude start
        os.close(in_master)  # the terminal goes away
        in_master = -1
        deadline = time.monotonic() + 15
        while proc.poll() is None and time.monotonic() < deadline:
            if select.select([out_master], [], [], 0.1)[0]:
                try:
                    os.read(out_master, 65536)
                except OSError:
                    time.sleep(0.05)
        if proc.poll() is None:
            proc.kill()
            pytest.fail("relay kept running after its terminal closed")
    finally:
        if in_master != -1:
            os.close(in_master)
        os.close(out_master)

    assert out.exists() and out.read_text() == "hup"
    assert proc.returncode == 9
