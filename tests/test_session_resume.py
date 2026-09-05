"""Tests for resuming limit-stopped Claude Code sessions (session_resume.py).

The transcript entries below are ABRIDGED COPIES OF REAL ONES, captured from
Claude Code 2.1.232/2.1.228 transcripts: the terminal weekly-limit stop and
the retryable mid-turn 429 that must not be confused with it. Fields the
discriminator reads are verbatim; unread bulk (usage counters, uuids) is
dropped.
"""

from __future__ import annotations

import json
import logging
import os
import re
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
from claude_swap.settings import set_setting


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
    """Only the last TURN counts: a limit earlier in the transcript that was
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


# Bookkeeping Claude Code appends AFTER a turn ends. Captured from a real
# transcript (session 023e5bcb, 2026-08-18): the terminal limit stop was
# followed by these three, so the LAST entry was never the limit stop at all.
TRAILING_BOOKKEEPING = [
    {"type": "system", "subtype": "turn_duration", "durationMs": 3},
    {"type": "system", "subtype": "informational", "content": "..."},
    {"type": "bridge-session", "sessionId": "sess-1"},
]


def test_bookkeeping_appended_after_the_stop_does_not_hide_it(
    tmp_path: Path, monkeypatch
):
    """The limit stop is rarely the last LINE — it is the last TURN.

    Claude Code keeps writing non-turn entries after work stops (17 such
    types seen in one transcript set). Reading only the final line stranded
    exactly the sessions this feature exists for.
    """
    _write_transcript(tmp_path, [{"type": "user"}, LIMIT_STOP, *TRAILING_BOOKKEEPING])
    monkeypatch.setattr(
        session_resume, "list_sessions", lambda d=None: [_session(tmp_path)]
    )
    assert [s.session_id for s in find_stopped_sessions(tmp_path)] == ["sess-1"]


def test_a_new_user_turn_after_the_stop_means_it_already_resumed(
    tmp_path: Path, monkeypatch
):
    """Scanning back past bookkeeping must still stop at a real turn — a user
    who typed after the limit (or an earlier nudge) already restarted it."""
    _write_transcript(tmp_path, [
        LIMIT_STOP,
        {"type": "user"},
        {"type": "attachment"},
        *TRAILING_BOOKKEEPING,
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


# --- the envelope --------------------------------------------------------------

# Claude Code 2.1.232's own parse regex for the envelope, transcribed from the
# binary. Attribute order is FIXED and every group optional, so a wrapper built
# in any other order does not match at all — which is why these tests assert
# against the parser rather than against our own formatting.
_ADDR = r"[A-Za-z0-9%:_/.\\-]+"
_HEX24 = r"[0-9a-f]{24}"
PEER_ENVELOPE_RE = re.compile(
    r'^<cross-session-message'
    rf'(?: from="({_ADDR})")?'
    r'(?: from-session="([A-Za-z0-9_-]{1,80})")?'
    rf'(?: hop-chain="({_HEX24}(?:,{_HEX24})*)")?'
    r'(?: from-name="([^"<>\n\r]+)")?'
    r'(?: from-mode="(bypass|prompting)")?'
    # NEWLINES, not spaces — transcribed verbatim from the binary. An earlier
    # version of this pattern used spaces and happily matched an envelope the
    # real parser rejected, so it certified a wrapper that a live session
    # displayed as literal text.
    r'>\n([\s\S]*)\n</cross-session-message>$'
)


def test_the_envelope_matches_claude_codes_parser():
    """Without a matching envelope the message is delivered as coming from
    "an unidentified session" — measured on a real session, which then held it.
    """
    m = PEER_ENVELOPE_RE.match(session_resume.wrap_peer_body("hello"))
    assert m, "Claude Code's parser would not recognise this envelope"
    assert m.group(1).startswith("uds:")
    assert m.group(4) == "claude-swap"
    assert m.group(6) == "hello"


def test_the_envelope_survives_the_receivers_round_trip_check():
    """The parse alone is not enough to be accepted.

    Claude Code re-renders the parsed pieces and requires the result to equal
    the original byte-for-byte (`if (oCr(...) !== n) return;`), so any
    separator, spacing or attribute-order difference silently demotes the
    message to literal text from "an unidentified session" — which is exactly
    what a live session did with a space-separated body.

    Rebuilds from the captured groups the way the receiver does, rather than
    re-calling our own writer, so this fails if the writer drifts.
    """
    wrapped = session_resume.wrap_peer_body("hello")
    m = PEER_ENVELOPE_RE.match(wrapped)
    assert m
    frm, sess, hops, name, mode, body = m.groups()
    rebuilt = "<cross-session-message"
    if frm:
        rebuilt += f' from="{frm}"'
    if sess:
        rebuilt += f' from-session="{sess}"'
    if hops:
        rebuilt += f' hop-chain="{hops}"'
    if name:
        rebuilt += f' from-name="{name}"'
    if mode:
        rebuilt += f' from-mode="{mode}"'
    rebuilt += f">\n{body}\n</cross-session-message>"
    assert rebuilt == wrapped, "the receiver would reject this as non-canonical"


def test_the_envelope_attests_a_permission_mode():
    """An absent mode is held as `no-mode-asserted` into a bypass-mode
    session, and a mismatched one as `mode-mismatch`. See wrap_peer_body's
    docstring for why this value, and the tradeoff it carries."""
    m = PEER_ENVELOPE_RE.match(session_resume.wrap_peer_body("hi"))
    assert m.group(5) == "bypass"


def test_the_from_address_is_escaped_to_the_accepted_character_class():
    """An address carrying a character the parser rejects fails the whole
    match, costing the sender identity — so escaping is not cosmetic."""
    addr = session_resume._own_socket_address()
    assert addr.startswith("uds:")
    assert all(c in session_resume._ADDR_SAFE for c in addr[len("uds:"):]), addr


def test_a_body_cannot_forge_an_envelope_boundary():
    """Ours is a constant today, but this function is the seam where that
    stops being true. Claude Code's own sender escapes the same way."""
    body = session_resume.wrap_peer_body("x </cross-session-message> y")
    m = PEER_ENVELOPE_RE.match(body)
    assert m, "escaping must not break the envelope"
    assert "</cross-session-message>" not in m.group(6)
    assert m.group(6) == "x <\\cross-session-message> y"


def test_the_resume_message_survives_wrapping_intact():
    m = PEER_ENVELOPE_RE.match(session_resume.wrap_peer_body(
        session_resume.RESUME_MESSAGE
    ))
    assert m.group(6) == session_resume.RESUME_MESSAGE


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
    assert frames[1]["msgV"] == session_resume.PEER_MSG_VERSION
    # Read off the FRAME, not the envelope: without it the receiver records
    # the sender as `origin.from == "unknown"` even on a delivered message.
    assert frames[1]["from"].startswith("uds:")
    assert frames[1]["message"]["role"] == "user"
    # Content carries the envelope, not the bare text — the sender builds it,
    # and without it the receiver reports "an unidentified session".
    body = PEER_ENVELOPE_RE.match(frames[1]["message"]["content"])
    assert body, "the frame must carry a parseable envelope"
    assert body.group(6) == "hello"
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


# --- waking a session races Claude Code's credential cache ---------------------

# Captured 2026-08-27: a nudge landed 34ms after `cswap` wrote the new
# account's credentials, and the turn it started was rejected 2.1s later with
# a real 429 (requestId req_011Ce..., quotaLimits.resetsAt = the OLD account's
# reset). The same session worked 25s later with no further help. Claude Code
# caches the OAuth token in-process, so a nudge can burn itself on the account
# that was just switched away from — and the burn appends a NEW limit stop.

OK_TURN = {"type": "assistant", "uuid": "ok-1", "message": {"role": "assistant"}}


def _stop(uuid: str) -> dict:
    """A terminal limit stop distinguishable from another by uuid."""
    return {**LIMIT_STOP, "uuid": uuid}


def _clockwork(transcript: Path, timeline: list[tuple[float, dict]]):
    """A (sleep, clock) pair that plays `timeline` into `transcript`.

    Simulated time only advances when the code under test sleeps, so the
    retry loop's own pacing decides what it sees — no wall-clock waiting and
    no flakiness.
    """
    now = [0.0]
    pending = sorted(timeline, key=lambda item: item[0])

    def _catch_up():
        while pending and pending[0][0] <= now[0]:
            _, entry = pending.pop(0)
            with transcript.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")

    def sleep(seconds):
        now[0] += seconds
        _catch_up()

    return sleep, (lambda: now[0])


def _nudge_recorder(monkeypatch, transcript: Path, on_send=None):
    """Replace the wire with a recorder; returns the list of nudged ids."""
    sent: list[str] = []

    def fake_send(socket_path, text, pid=None, claude_dir=None):
        sent.append(socket_path)
        if on_send is not None:
            on_send()
        return True

    monkeypatch.setattr(session_resume, "send_peer_message", fake_send)
    return sent


def test_stopped_session_records_which_stop_it_saw(tmp_path: Path, monkeypatch):
    """The retry logic needs to tell a NEW limit stop from the original one."""
    _write_transcript(tmp_path, [{"type": "user"}, _stop("stop-1")])
    monkeypatch.setattr(
        session_resume, "list_sessions", lambda d=None: [_session(tmp_path)]
    )
    assert find_stopped_sessions(tmp_path)[0].stop_uuid == "stop-1"


def test_a_nudge_burned_on_stale_credentials_is_retried(tmp_path: Path, monkeypatch):
    """The exact captured failure: nudge lands, a NEW limit stop appears."""
    path = _write_transcript(tmp_path, [_stop("stop-1")])
    stopped = StoppedSession("sess-1", 1, "/Users/x/my.project", "/s.sock", "limit", "stop-1")
    sent = _nudge_recorder(monkeypatch, path)
    # The first nudge burns at +2s; by the retry the account is live again.
    sleep, clock = _clockwork(path, [
        (2.0, {"type": "user"}),
        (2.5, _stop("stop-2")),
        (8.0, OK_TURN),
    ])

    resumed = session_resume.resume_sessions(
        [stopped], tmp_path, sleep=sleep, clock=clock
    )

    assert len(sent) == 2, "the burned nudge should have been sent again"
    assert [s.session_id for s in resumed] == ["sess-1"]


def test_a_nudge_held_for_review_is_never_retried(tmp_path: Path, monkeypatch):
    """A held message leaves the tail on the ORIGINAL stop.

    Claude Code can hold a cross-session message for the user instead of
    acting on it, and holds are invisible from this side. Retrying would
    queue duplicates in front of a user who has not looked yet.
    """
    path = _write_transcript(tmp_path, [_stop("stop-1")])
    stopped = StoppedSession("sess-1", 1, "/Users/x/my.project", "/s.sock", "limit", "stop-1")
    sent = _nudge_recorder(monkeypatch, path)
    sleep, clock = _clockwork(path, [])  # nothing ever changes

    session_resume.resume_sessions([stopped], tmp_path, sleep=sleep, clock=clock)

    assert len(sent) == 1


def test_our_own_nudge_landing_is_not_mistaken_for_recovery(
    tmp_path: Path, monkeypatch
):
    """The nudge IS a user turn, and it appears ~2s before the 429 it earns.

    Treating "tail is a user turn" as success would declare victory in the
    gap and strand exactly the session this fix exists for.
    """
    path = _write_transcript(tmp_path, [_stop("stop-1")])
    stopped = StoppedSession("sess-1", 1, "/Users/x/my.project", "/s.sock", "limit", "stop-1")
    sent = _nudge_recorder(monkeypatch, path)
    sleep, clock = _clockwork(path, [(0.5, {"type": "user"}), (3.0, _stop("stop-2"))])

    session_resume.resume_sessions([stopped], tmp_path, sleep=sleep, clock=clock)

    assert len(sent) == 2


def test_a_session_that_wakes_is_not_nudged_again(tmp_path: Path, monkeypatch):
    path = _write_transcript(tmp_path, [_stop("stop-1")])
    stopped = StoppedSession("sess-1", 1, "/Users/x/my.project", "/s.sock", "limit", "stop-1")
    sent = _nudge_recorder(monkeypatch, path)
    sleep, clock = _clockwork(path, [(1.0, {"type": "user"}), (2.0, OK_TURN)])

    session_resume.resume_sessions([stopped], tmp_path, sleep=sleep, clock=clock)

    assert len(sent) == 1


def test_retries_are_bounded(tmp_path: Path, monkeypatch, caplog):
    """An account that never comes back must not be nudged forever."""
    path = _write_transcript(tmp_path, [_stop("stop-1")])
    stopped = StoppedSession("sess-1", 1, "/Users/x/my.project", "/s.sock", "limit", "stop-1")
    counter = {"n": 0}

    def burn():
        # Every nudge earns a fresh limit stop, exactly like a still-dead account.
        counter["n"] += 1
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(_stop(f"burn-{counter['n']}")) + "\n")

    sent = _nudge_recorder(monkeypatch, path, on_send=burn)
    sleep, clock = _clockwork(path, [])

    with caplog.at_level(logging.WARNING, logger="claude-swap"):
        session_resume.resume_sessions([stopped], tmp_path, sleep=sleep, clock=clock)

    assert len(sent) == 1 + len(session_resume.RESUME_RETRY_DELAYS_S)
    assert any("could not be woken" in r.message for r in caplog.records)


def test_a_session_the_socket_refused_is_not_watched(tmp_path: Path, monkeypatch):
    """No bytes delivered means nothing to verify — and nothing to retry."""
    path = _write_transcript(tmp_path, [_stop("stop-1")])
    stopped = StoppedSession("sess-1", 1, "/Users/x/my.project", "/s.sock", "limit", "stop-1")
    monkeypatch.setattr(
        session_resume, "send_peer_message", lambda *a, **k: False
    )
    sleep, clock = _clockwork(path, [])

    resumed = session_resume.resume_sessions(
        [stopped], tmp_path, sleep=sleep, clock=clock
    )

    assert resumed == []


def test_warnings_reach_the_claude_swap_log(tmp_path: Path, monkeypatch):
    """This module's records must land on the logger the log file listens to.

    `setup_logging` attaches the rotating file handler to "claude-swap".
    A module logger named after the package ("claude_swap.session_resume")
    is a different tree — underscore, not hyphen — so it propagates to root
    and is written nowhere. That silently defeated the peerProtocol warning,
    which exists precisely so a Claude Code upgrade disabling resume is
    discoverable from the log.
    """
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Capture()
    logging.getLogger("claude-swap").addHandler(handler)
    try:
        path = _write_transcript(tmp_path, [_stop("stop-1")])
        stopped = StoppedSession(
            "sess-1", 1, "/Users/x/my.project", "/s.sock", "limit", "stop-1"
        )
        counter = {"n": 0}

        def burn():
            counter["n"] += 1
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(_stop(f"burn-{counter['n']}")) + "\n")

        _nudge_recorder(monkeypatch, path, on_send=burn)
        sleep, clock = _clockwork(path, [])
        session_resume.resume_sessions([stopped], tmp_path, sleep=sleep, clock=clock)
    finally:
        logging.getLogger("claude-swap").removeHandler(handler)

    assert any("could not be woken" in r.getMessage() for r in records)


# --- limit stops as evidence about the active account -------------------------

# The usage API only reports an exhausted account on the next poll — up to a
# full interval late. The transcript says it immediately, and carries the two
# fields the engine needs. Captured verbatim 2026-08-27 alongside the 429 that
# stranded session 767f1fac.
QUOTA_LIMITS = {
    "status": "rejected",
    "resetsAt": 1787815200,
    "rateLimitType": "five_hour",
    "overageStatus": "rejected",
}


def _dated_stop(uuid_: str, when: str, **quota) -> dict:
    return {
        **LIMIT_STOP,
        "uuid": uuid_,
        "timestamp": when,
        "quotaLimits": {**QUOTA_LIMITS, **quota},
    }


def test_a_limit_stop_is_read_as_evidence_about_the_account(
    tmp_path: Path, monkeypatch
):
    _write_transcript(tmp_path, [
        {"type": "user"},
        _dated_stop("stop-1", "2026-08-27T06:30:35.659Z"),
    ])
    monkeypatch.setattr(
        session_resume, "list_sessions", lambda d=None: [_session(tmp_path)]
    )

    found = session_resume.LimitStopScanner(tmp_path).scan()

    assert [s.stop_uuid for s in found] == ["stop-1"]
    assert found[0].window == "five_hour"
    assert found[0].resets_at == 1787815200
    # 2026-08-27T06:30:35.659Z, the moment the session actually stopped.
    assert found[0].observed_at == pytest.approx(1787812235.659, abs=0.01)


def test_the_same_stop_is_only_reported_once(tmp_path: Path, monkeypatch):
    """The scanner runs every few seconds; a stop is news exactly once."""
    _write_transcript(tmp_path, [_dated_stop("stop-1", "2026-08-27T06:30:35Z")])
    monkeypatch.setattr(
        session_resume, "list_sessions", lambda d=None: [_session(tmp_path)]
    )
    scanner = session_resume.LimitStopScanner(tmp_path)

    assert len(scanner.scan()) == 1
    assert scanner.scan() == []


def test_a_later_stop_is_still_found_after_an_unchanged_scan(
    tmp_path: Path, monkeypatch
):
    """The mtime gate must skip re-reads without hiding real news."""
    path = _write_transcript(tmp_path, [_dated_stop("stop-1", "2026-08-27T06:30:35Z")])
    monkeypatch.setattr(
        session_resume, "list_sessions", lambda d=None: [_session(tmp_path)]
    )
    scanner = session_resume.LimitStopScanner(tmp_path)
    scanner.scan()
    scanner.scan()

    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_dated_stop("stop-2", "2026-08-27T07:00:00Z")) + "\n")
    os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 10))

    assert [s.stop_uuid for s in scanner.scan()] == ["stop-2"]


def test_a_session_that_cannot_be_messaged_is_still_evidence(
    tmp_path: Path, monkeypatch
):
    """Unlike a resume candidate, evidence does not need a socket.

    What the transcript says about the account's quota is true whether or not
    that session can be nudged afterwards.
    """
    _write_transcript(tmp_path, [_dated_stop("stop-1", "2026-08-27T06:30:35Z")])
    monkeypatch.setattr(
        session_resume,
        "list_sessions",
        lambda d=None: [_session(tmp_path, messaging_socket_path="", peer_protocol=0)],
    )

    assert len(session_resume.LimitStopScanner(tmp_path).scan()) == 1
    assert find_stopped_sessions(tmp_path) == []


def test_a_working_session_is_not_evidence(tmp_path: Path, monkeypatch):
    _write_transcript(tmp_path, [LIMIT_STOP, RETRYABLE_429])
    monkeypatch.setattr(
        session_resume, "list_sessions", lambda d=None: [_session(tmp_path)]
    )
    assert session_resume.LimitStopScanner(tmp_path).scan() == []


def test_a_stop_without_quota_fields_still_reports_what_it_has(
    tmp_path: Path, monkeypatch
):
    """Older Claude Code builds may omit quotaLimits; the stop is still real."""
    _write_transcript(tmp_path, [{**LIMIT_STOP, "uuid": "stop-1"}])
    monkeypatch.setattr(
        session_resume, "list_sessions", lambda d=None: [_session(tmp_path)]
    )
    found = session_resume.LimitStopScanner(tmp_path).scan()
    assert found[0].resets_at == 0.0 and found[0].window == ""


# --- manual switches -----------------------------------------------------------

# The engine remembers what it witnessed across ticks; a human-driven switch
# has no such memory (`cswap use` is a fresh process, and the menu bar's record
# lives inside an engine that may not be running at all). So the manual path
# SCANS at switch time, and leans on the liveness filter in
# `find_stopped_sessions` — process alive, socket published, transcript tail
# still ending in a terminal limit — for the safety the engine gets from
# having watched the stop happen.


class _StubSwitcher:
    """The two switcher members `resume_after_manual_switch` reads.

    A real `ClaudeAccountSwitcher` needs a seeded account store, credentials,
    and a platform backend — none of which this function touches. It asks the
    switcher exactly two things: where settings live, and which slot is live
    now.
    """

    def __init__(self, backup_dir: Path, account: str | None):
        self.backup_dir = backup_dir
        self._account = account

    def current_account_number(self) -> str | None:
        return self._account


def _resume_enabled(backup_dir: Path) -> None:
    set_setting(backup_dir, "autoswitch.resumeStoppedSessions", "true")


@requires_af_unix
def test_a_manual_switch_nudges_a_stopped_session(
    tmp_path: Path, short_sock: Path, monkeypatch
):
    """The whole point: a human switch reaches the same inbox the engine does."""
    _resume_enabled(tmp_path)
    _write_transcript(tmp_path, [{"type": "user"}, LIMIT_STOP])
    monkeypatch.setattr(
        session_resume,
        "list_sessions",
        lambda d=None: [_session(tmp_path, messaging_socket_path=str(short_sock))],
    )
    received: list[bytes] = []
    thread = _listener(short_sock, received)

    resumed = session_resume.resume_after_manual_switch(
        _StubSwitcher(tmp_path, "2"), "1", tmp_path
    )
    thread.join(timeout=5)

    assert [s.session_id for s in resumed] == ["sess-1"]
    assert session_resume.RESUME_MESSAGE.encode() in b"".join(received)


def test_a_manual_switch_that_landed_nowhere_nudges_nobody(
    tmp_path: Path, monkeypatch
):
    """`cswap use 2` while already on 2 changes no quota, so it wakes nothing.

    Both the CLI and the menu bar report success for that no-op, so the slot
    comparison — not the call's return value — is what tells them apart.
    """
    _resume_enabled(tmp_path)
    _write_transcript(tmp_path, [LIMIT_STOP])
    monkeypatch.setattr(
        session_resume, "list_sessions", lambda d=None: [_session(tmp_path)]
    )

    assert session_resume.resume_after_manual_switch(
        _StubSwitcher(tmp_path, "2"), "2", tmp_path
    ) == []


def test_a_manual_switch_does_not_nudge_when_resume_is_off(
    tmp_path: Path, monkeypatch
):
    """`resumeStoppedSessions` gates the manual path exactly as it gates the
    engine's — it is one opt-in, not two."""
    _write_transcript(tmp_path, [LIMIT_STOP])
    monkeypatch.setattr(
        session_resume, "list_sessions", lambda d=None: [_session(tmp_path)]
    )

    assert session_resume.resume_after_manual_switch(
        _StubSwitcher(tmp_path, "2"), "1", tmp_path
    ) == []


def test_a_broken_scan_never_fails_the_switch(tmp_path: Path, monkeypatch, caplog):
    """Scanning reads undocumented Claude Code state. A shape change there must
    cost the nudge, never the switch the user actually asked for."""
    _resume_enabled(tmp_path)

    def boom(_d=None):
        raise RuntimeError("transcript shape changed")

    monkeypatch.setattr(session_resume, "find_stopped_sessions", boom)

    with caplog.at_level(logging.WARNING):
        resumed = session_resume.resume_after_manual_switch(
            _StubSwitcher(tmp_path, "2"), "1", tmp_path
        )

    assert resumed == []
    assert "transcript shape changed" in caplog.text


class TestManualSwitchNeedsQuota:
    """A slot change is not evidence of quota.

    Measured 2026-08-27 18:06: rotating from account 5 wrapped to account 1,
    whose weekly window was at 100%. The slot changed, so every stopped
    session was nudged straight into an account with nothing left and took
    its weekly limit — a reset four days out — on the first turn.
    """

    def test_no_nudge_when_the_account_landed_on_is_spent(
        self, tmp_path: Path, monkeypatch
    ):
        _resume_enabled(tmp_path)
        _write_transcript(tmp_path, [LIMIT_STOP])
        monkeypatch.setattr(
            session_resume, "list_sessions", lambda d=None: [_session(tmp_path)]
        )
        monkeypatch.setattr(session_resume, "_active_headroom", lambda _s: 0.0)
        sent: list[str] = []
        monkeypatch.setattr(
            session_resume,
            "send_peer_message",
            lambda *a, **k: sent.append(a) or True,
        )

        assert session_resume.resume_after_manual_switch(
            _StubSwitcher(tmp_path, "2"), "1", tmp_path
        ) == []
        assert sent == []

    def test_unknown_headroom_still_nudges(self, tmp_path: Path, monkeypatch):
        """An unreadable measurement is not evidence of an empty account.

        Same rule the engine applies: a locked keychain or an expired token
        must not silently disable the feature for the person it exists for.
        """
        _resume_enabled(tmp_path)
        _write_transcript(tmp_path, [LIMIT_STOP])
        monkeypatch.setattr(
            session_resume, "list_sessions", lambda d=None: [_session(tmp_path)]
        )
        monkeypatch.setattr(session_resume, "_active_headroom", lambda _s: None)
        monkeypatch.setattr(session_resume, "send_peer_message", lambda *a, **k: True)

        assert session_resume.resume_after_manual_switch(
            _StubSwitcher(tmp_path, "2"), "1", tmp_path
        ) != []


class TestRetriesAreDistinguishable:
    """Claude Code drops a peer message identical to the sender's previous one.

    Observed 2026-08-27: "Dropped a peer message from @claude-swap ...
    identical to the previous message from this sender." Every retry sent the
    same RESUME_MESSAGE constant, so attempts 2 and 3 were discarded by the
    receiver while send_peer_message still reported success — the retry could
    never have worked.
    """

    def test_each_attempt_sends_different_bytes(self, tmp_path: Path, monkeypatch):
        # The stop must carry a uuid: _watch_nudges re-baselines on it after a
        # burn, and a session without one stops being watchable (and so stops
        # being retried) after the first round.
        _write_transcript(tmp_path, [_stop("u2")])
        sent: list[str] = []

        def _send(_sock, message, **_kw):
            sent.append(message)
            return True

        monkeypatch.setattr(session_resume, "send_peer_message", _send)
        # Never satisfied, so the loop spends its full retry budget.
        monkeypatch.setattr(
            session_resume, "_nudge_verdict", lambda *a, **k: "burned"
        )

        stopped = StoppedSession(
            "sess-1", 1, "/Users/x/my.project", "/s.sock", "limit", "stop-1"
        )
        session_resume.resume_sessions(
            [stopped], tmp_path, sleep=lambda _s: None, clock=lambda: 0.0
        )

        assert len(sent) == 1 + len(session_resume.RESUME_RETRY_DELAYS_S)
        assert len(set(sent)) == len(sent), f"identical payloads: {sent}"
        assert sent[0] == session_resume.RESUME_MESSAGE


class TestCaptureLimitScreens:
    """capture_limit_screens — the 6b dialog-recognizer corpus builder."""

    def _stopped(self, sid="sess-cap", pid=99):
        return StoppedSession(sid, pid, "/w", "/s.sock", "limit", "u1")

    def test_writes_one_capture_per_session(self, tmp_path, monkeypatch):
        from claude_swap import cmux_control

        monkeypatch.setattr(
            cmux_control, "capture_screen_for_pid",
            lambda pid, **kw: ("surface:3", "the dialog\n"),
        )
        session_resume.capture_limit_screens([self._stopped()], tmp_path)
        files = list((tmp_path / "limit-screens").glob("*.txt"))
        assert len(files) == 1
        text = files[0].read_text()
        assert "session sess-cap" in text and "surface surface:3" in text
        assert "the dialog" in text

    def test_session_outside_cmux_writes_nothing(self, tmp_path, monkeypatch):
        from claude_swap import cmux_control

        monkeypatch.setattr(
            cmux_control, "capture_screen_for_pid", lambda pid, **kw: None
        )
        session_resume.capture_limit_screens([self._stopped()], tmp_path)
        assert not (tmp_path / "limit-screens").exists()

    def test_corpus_is_bounded(self, tmp_path, monkeypatch):
        from claude_swap import cmux_control

        d = tmp_path / "limit-screens"
        d.mkdir()
        for i in range(session_resume._CAPTURE_KEEP + 5):
            (d / f"20260101T00000{i:02d}-old.txt").write_text("x")
        monkeypatch.setattr(
            cmux_control, "capture_screen_for_pid",
            lambda pid, **kw: ("surface:1", "s"),
        )
        session_resume.capture_limit_screens([self._stopped()], tmp_path)
        assert len(list(d.glob("*.txt"))) == session_resume._CAPTURE_KEEP

    def test_never_raises(self, tmp_path, monkeypatch):
        from claude_swap import cmux_control

        def boom(pid, **kw):
            raise RuntimeError("cmux exploded")
        monkeypatch.setattr(cmux_control, "capture_screen_for_pid", boom)
        session_resume.capture_limit_screens([self._stopped()], tmp_path)  # no raise

    def test_none_backup_dir_is_a_noop(self):
        session_resume.capture_limit_screens([self._stopped()], None)


class TestNudgeChannelSelection:
    """_nudge_one — cmux-primary, socket fallback, no double delivery."""

    def _stopped(self):
        return StoppedSession("sess-n", 77, "/w", "/s.sock", "limit", "u1")

    def _run(self, monkeypatch, status, socket_result=True):
        from claude_swap import cmux_control

        socket_calls = []
        monkeypatch.setattr(
            cmux_control, "nudge_via_cmux", lambda pid, text, **kw: status
        )
        monkeypatch.setattr(
            session_resume, "send_peer_message",
            lambda *a, **kw: socket_calls.append(a) or socket_result,
        )
        ok = session_resume._nudge_one(self._stopped(), "text", None)
        return ok, socket_calls

    def test_delivered_skips_the_socket(self, monkeypatch):
        ok, socket_calls = self._run(monkeypatch, "delivered")
        assert ok and socket_calls == []

    def test_typed_unverified_also_skips_the_socket(self, monkeypatch):
        # The text WAS submitted as a user turn — a socket copy on top
        # would queue a duplicate.
        ok, socket_calls = self._run(monkeypatch, "typed-unverified")
        assert ok and socket_calls == []

    def test_no_surface_falls_back_to_the_socket(self, monkeypatch):
        ok, socket_calls = self._run(monkeypatch, "no-surface")
        assert ok and len(socket_calls) == 1

    def test_captured_input_falls_back_to_the_socket(self, monkeypatch):
        ok, socket_calls = self._run(monkeypatch, "captured-input")
        assert ok and len(socket_calls) == 1

    def test_running_gets_nothing(self, monkeypatch):
        ok, socket_calls = self._run(monkeypatch, "running")
        assert not ok and socket_calls == []

    def test_cmux_blowup_degrades_to_the_socket(self, monkeypatch):
        from claude_swap import cmux_control

        def boom(pid, text, **kw):
            raise RuntimeError("cmux exploded")
        socket_calls = []
        monkeypatch.setattr(cmux_control, "nudge_via_cmux", boom)
        monkeypatch.setattr(
            session_resume, "send_peer_message",
            lambda *a, **kw: socket_calls.append(a) or True,
        )
        assert session_resume._nudge_one(self._stopped(), "text", None)
        assert len(socket_calls) == 1
