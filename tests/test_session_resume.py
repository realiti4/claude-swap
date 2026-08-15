"""Tests for resuming limit-stopped Claude Code sessions (session_resume.py).

The transcript entries below are ABRIDGED COPIES OF REAL ONES, captured from
Claude Code 2.1.232/2.1.228 transcripts: the terminal weekly-limit stop and
the retryable mid-turn 429 that must not be confused with it. Fields the
discriminator reads are verbatim; unread bulk (usage counters, uuids) is
dropped.
"""

from __future__ import annotations

import json
import shutil
import socket
import tempfile
import threading
from pathlib import Path

import pytest

from claude_swap import session_resume
from claude_swap.process_detection import ClaudeSession
from claude_swap.session_resume import (
    StoppedSession,
    find_stopped_sessions,
    is_limit_stop,
    send_peer_message,
    transcript_path,
)


# --- real captured shapes ------------------------------------------------------

# Terminal: the turn ended and only a new user turn restarts it.
LIMIT_STOP = {
    "type": "assistant",
    "message": {
        "model": "<synthetic>",
        "role": "assistant",
        "stop_reason": "stop_sequence",
        "content": [{
            "type": "text",
            "text": "You've hit your weekly limit · resets Aug 17 at 10pm (Asia/Saigon)",
        }],
    },
    "error": "rate_limit",
    "isApiErrorMessage": True,
    "apiErrorStatus": 429,
    "sessionId": "fd2d1271-1dbf-4fd8-8d8f-d89ecd81a78d",
}

# Retryable: Claude Code is still working on this turn (attempt 2 of 10).
RETRYABLE_429 = {
    "type": "system",
    "subtype": "api_error",
    "level": "error",
    "error": {
        "message": '429 {"type":"error","error":{"type":"rate_limit_error"}}',
        "status": 429,
        "formatted": "429 Rate limited",
    },
    "retryInMs": 1179.57,
    "retryAttempt": 2,
    "maxRetries": 10,
}


# --- the discriminator ---------------------------------------------------------

def test_terminal_limit_stop_is_detected():
    assert is_limit_stop(LIMIT_STOP)


def test_retryable_429_is_not_a_limit_stop():
    """The whole point of the discriminator.

    Claude Code retries these itself — a nudge here would inject a user turn
    into a turn that is still running. Measured on a real transcript: eight of
    these appeared mid-session and the session carried on afterwards.
    """
    assert not is_limit_stop(RETRYABLE_429)


def test_a_terminal_shape_carrying_retry_bookkeeping_is_not_a_stop():
    """Belt-and-braces: retry bookkeeping means Claude Code is still trying."""
    assert not is_limit_stop({**LIMIT_STOP, "retryAttempt": 1})


def test_an_ordinary_assistant_turn_is_not_a_limit_stop():
    assert not is_limit_stop({
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
    })


def test_a_non_rate_limit_api_error_is_not_a_limit_stop():
    """Only rate limits are resumable by switching accounts. An overloaded or
    auth error would come straight back on the new account."""
    assert not is_limit_stop({
        **LIMIT_STOP, "error": "overloaded", "apiErrorStatus": 529,
    })


def test_garbage_is_not_a_limit_stop():
    for value in (None, {}, [], "rate_limit", 0):
        assert not is_limit_stop(value)


# --- transcript location + tail reading ----------------------------------------

def _session(tmp_path: Path, **kw) -> ClaudeSession:
    defaults = dict(
        pid=4242,
        session_id="sess-1",
        cwd="/Users/x/my.project",
        started_at=0,
        kind="interactive",
        entrypoint="cli",
        status="idle",
        messaging_socket_path=str(tmp_path / "s.sock"),
        peer_protocol=session_resume.PEER_PROTOCOL,
    )
    defaults.update(kw)
    return ClaudeSession(**defaults)


def test_transcript_path_slugifies_the_cwd(tmp_path: Path):
    """Every non-alphanumeric becomes '-', including the dot in 'my.project'."""
    path = transcript_path(_session(tmp_path), tmp_path)
    assert path == tmp_path / "projects" / "-Users-x-my-project" / "sess-1.jsonl"


def _write_transcript(tmp_path: Path, entries: list[dict], cwd="/Users/x/my.project"):
    path = transcript_path(_session(tmp_path, cwd=cwd), tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    return path


def test_finds_a_session_stopped_on_a_limit(tmp_path: Path, monkeypatch):
    _write_transcript(tmp_path, [{"type": "user"}, LIMIT_STOP])
    monkeypatch.setattr(
        session_resume, "list_sessions", lambda d=None: [_session(tmp_path)]
    )
    found = find_stopped_sessions(tmp_path)
    assert [s.session_id for s in found] == ["sess-1"]
    assert found[0].message.startswith("You've hit your weekly limit")


def test_a_session_still_working_is_not_collected(tmp_path: Path, monkeypatch):
    """A retryable 429 as the LAST entry means the turn is still in flight."""
    _write_transcript(tmp_path, [LIMIT_STOP, RETRYABLE_429])
    monkeypatch.setattr(
        session_resume, "list_sessions", lambda d=None: [_session(tmp_path)]
    )
    assert find_stopped_sessions(tmp_path) == []


def test_a_session_that_recovered_is_not_collected(tmp_path: Path, monkeypatch):
    """Only the LAST entry counts: a limit earlier in the transcript that was
    followed by real work is history, not a stopped session."""
    _write_transcript(tmp_path, [
        LIMIT_STOP,
        {"type": "assistant",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}},
    ])
    monkeypatch.setattr(
        session_resume, "list_sessions", lambda d=None: [_session(tmp_path)]
    )
    assert find_stopped_sessions(tmp_path) == []


def test_a_partial_final_line_does_not_hide_the_limit_stop(tmp_path: Path, monkeypatch):
    """Claude Code appends while we read, so the tail can end mid-write."""
    path = _write_transcript(tmp_path, [{"type": "user"}, LIMIT_STOP])
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"type":"assist')
    monkeypatch.setattr(
        session_resume, "list_sessions", lambda d=None: [_session(tmp_path)]
    )
    assert [s.session_id for s in find_stopped_sessions(tmp_path)] == ["sess-1"]


def test_only_the_tail_is_read(tmp_path: Path, monkeypatch):
    """Real transcripts reach hundreds of MB (one measured 163MB); reading a
    whole one per tick would stall the auto-switch loop."""
    path = transcript_path(_session(tmp_path), tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    filler = json.dumps({"type": "user", "pad": "x" * 4096}) + "\n"
    with path.open("w", encoding="utf-8") as fh:
        for _ in range(400):  # ~1.6MB, comfortably over _TAIL_BYTES
            fh.write(filler)
        fh.write(json.dumps(LIMIT_STOP) + "\n")
    assert path.stat().st_size > session_resume._TAIL_BYTES
    monkeypatch.setattr(
        session_resume, "list_sessions", lambda d=None: [_session(tmp_path)]
    )
    assert [s.session_id for s in find_stopped_sessions(tmp_path)] == ["sess-1"]


def test_a_missing_transcript_is_inert(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        session_resume, "list_sessions", lambda d=None: [_session(tmp_path)]
    )
    assert find_stopped_sessions(tmp_path) == []


def test_a_session_with_no_socket_is_skipped(tmp_path: Path, monkeypatch):
    """Recording a session we could never nudge would promise a resume that
    cannot happen."""
    _write_transcript(tmp_path, [LIMIT_STOP])
    monkeypatch.setattr(
        session_resume,
        "list_sessions",
        lambda d=None: [_session(tmp_path, messaging_socket_path="")],
    )
    assert find_stopped_sessions(tmp_path) == []


def test_an_unknown_peer_protocol_is_skipped(tmp_path: Path, monkeypatch, caplog):
    """A Claude Code upgrade that bumps the wire format must disable resume,
    not send guessed frames into a live session."""
    _write_transcript(tmp_path, [LIMIT_STOP])
    monkeypatch.setattr(
        session_resume,
        "list_sessions",
        lambda d=None: [_session(tmp_path, peer_protocol=session_resume.PEER_PROTOCOL + 1)],
    )
    with caplog.at_level("WARNING"):
        assert find_stopped_sessions(tmp_path) == []
    assert "peerProtocol" in caplog.text


# --- the wire ------------------------------------------------------------------

# Binding a listener needs real Unix domain sockets. Windows has none (Claude
# Code uses a named pipe there, a transport this module does not speak), so
# these exercise POSIX behaviour only — `test_send_is_inert_without_unix_sockets`
# below covers what Windows actually does.
requires_af_unix = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="peer messaging is POSIX-only"
)


@pytest.fixture
def short_sock(tmp_path_factory) -> Path:
    """A bindable socket path.

    ``AF_UNIX`` paths are capped near 104 bytes on macOS and pytest's
    ``tmp_path`` (which embeds the test name and an xdist worker id) blows
    past it. Real sockets live at ``/tmp/cc-socks/<pid>.sock``, comfortably
    inside the limit — so this is a harness constraint, not a property of
    the code under test.
    """
    base = Path(tempfile.mkdtemp(prefix="cs-"))
    path = base / "s.sock"
    assert len(str(path).encode()) < 104, "socket path must be bindable"
    yield path
    shutil.rmtree(base, ignore_errors=True)


def _listener(path: Path, received: list[bytes]):
    """A one-shot Unix socket server standing in for a session's inbox."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(1)

    def serve():
        conn, _ = srv.accept()
        with conn:
            chunks = []
            while chunk := conn.recv(65536):
                chunks.append(chunk)
            received.append(b"".join(chunks))
        srv.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return thread


@requires_af_unix
def test_send_writes_auth_then_message_as_ndjson(tmp_path: Path, short_sock: Path):
    """The frame shape Claude Code's inbox expects: an auth frame carrying the
    peer token first, then the user-role message, newline-delimited."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "4242.abc.key").write_text(json.dumps({"peerToken": "tok-123"}))
    sock_path = short_sock
    received: list[bytes] = []
    thread = _listener(sock_path, received)

    assert send_peer_message(str(sock_path), "hello", pid=4242, claude_dir=tmp_path)
    thread.join(timeout=5)

    frames = [json.loads(line) for line in received[0].splitlines() if line.strip()]
    assert frames[0] == {"type": "auth", "token": "tok-123"}
    assert frames[1]["type"] == "user"
    assert frames[1]["message"] == {"role": "user", "content": "hello"}
    assert frames[1]["priority"] == "next"
    assert frames[1]["msg_id"]


@requires_af_unix
def test_send_without_a_key_file_omits_the_auth_frame(tmp_path: Path, short_sock: Path):
    """Auth is optional on some platforms; the receiver decides. Sending a
    null token would be a malformed frame rather than an unauthenticated one."""
    (tmp_path / "sessions").mkdir()
    sock_path = short_sock
    received: list[bytes] = []
    thread = _listener(sock_path, received)

    assert send_peer_message(str(sock_path), "hi", pid=4242, claude_dir=tmp_path)
    thread.join(timeout=5)

    frames = [json.loads(line) for line in received[0].splitlines() if line.strip()]
    assert len(frames) == 1
    assert frames[0]["type"] == "user"


def test_send_to_a_dead_socket_reports_failure(tmp_path: Path):
    """A stale socket file outlives the process that bound it. The caller's
    alternative to a failed nudge is an un-resumed session, never a crash.

    Runs on EVERY platform on purpose: "returns False rather than raising" is
    the contract the auto-switch engine relies on, and Windows reaches it by
    a different route (no AF_UNIX at all) than POSIX (connect refused).
    """
    dead = tmp_path / "gone.sock"
    assert not send_peer_message(str(dead), "hi", pid=1, claude_dir=tmp_path)


def test_send_is_inert_without_unix_sockets(tmp_path: Path, monkeypatch):
    """Windows has no AF_UNIX — Claude Code binds a named pipe there, which
    this module does not speak. Reaching the `socket()` call would raise
    AttributeError out of a function documented never to raise, and that
    exception would surface inside an auto-switch tick.
    """
    monkeypatch.delattr(socket, "AF_UNIX", raising=False)
    assert not send_peer_message("/anything.sock", "hi", pid=1, claude_dir=tmp_path)


@requires_af_unix
def test_resume_reports_only_the_sessions_that_accepted(tmp_path: Path, short_sock: Path):
    sock_path = short_sock
    received: list[bytes] = []
    thread = _listener(sock_path, received)
    live = StoppedSession("a", 4242, "/x", str(sock_path), "limit")
    dead = StoppedSession("b", 4243, "/y", str(tmp_path / "gone.sock"), "limit")

    resumed = session_resume.resume_sessions([live, dead], tmp_path)
    thread.join(timeout=5)

    assert [s.session_id for s in resumed] == ["a"]
