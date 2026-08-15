"""Resume Claude Code sessions that stopped on a plan usage limit.

When every account is exhausted, work in progress stops with a terminal
rate-limit turn and nothing restarts it: the window reset arrives hours
later, cswap switches to the recovered account, and the session sits idle
until a human notices and presses enter. This module closes that gap —
it records which sessions stopped while cswap was watching, and nudges
exactly those once a switch has landed on an account with quota.

TWO Claude Code interfaces are used here, and NEITHER is a public API:

* the transcript (``~/.claude/projects/<slug>/<sessionId>.jsonl``), read
  only at its tail, to tell a terminal limit stop from a retryable 429;
* the per-session Unix socket in ``~/.claude/sessions/<pid>.json``, to
  deliver the nudge as a peer message.

Both are versioned by ``peerProtocol`` in the session record and can
change without notice, so every entry point here is written to degrade to
"do nothing" rather than to raise: a protocol bump must cost the user an
un-resumed session, never a crashed auto-switch loop. ``PEER_PROTOCOL``
below pins the version this implementation was written against.
"""

from __future__ import annotations

import json
import logging
import socket
import uuid
from dataclasses import dataclass
from pathlib import Path

from claude_swap.paths import get_claude_config_home
from claude_swap.process_detection import ClaudeSession, list_sessions

_logger = logging.getLogger(__name__)

# The ``peerProtocol`` value in ~/.claude/sessions/<pid>.json this module was
# written against. A session advertising anything else is skipped rather than
# messaged: the frame shape below is reverse-engineered, so guessing across a
# version bump risks delivering garbage into someone's live session.
PEER_PROTOCOL = 1

# How much of a transcript's tail to read when classifying its last turn.
# Transcripts reach hundreds of MB (a real one measured 163MB), so they are
# never read whole. One JSONL entry carrying a large tool result can be a few
# hundred KB, so this holds the last handful of entries with margin.
_TAIL_BYTES = 512 * 1024

# Connect/response budget for one nudge. The engine sends these from its tick,
# so a hung socket must not stall auto-switching; Claude Code's own client
# uses 5s for the same exchange.
_SEND_TIMEOUT_S = 5.0


@dataclass(frozen=True)
class StoppedSession:
    """A live session whose transcript ends in a plan-limit stop."""

    session_id: str
    pid: int
    cwd: str
    socket_path: str
    # The limit message as Claude Code rendered it, e.g. "You've hit your
    # weekly limit · resets Aug 17 at 10pm (Asia/Saigon)". Displayed to the
    # user; never parsed — the reset time comes from the usage API, which is
    # authoritative and already drives the engine's sleep.
    message: str


def transcript_path(session: ClaudeSession, claude_dir: Path | None = None) -> Path:
    """Where Claude Code stores ``session``'s transcript.

    The project directory is the session's cwd with every character outside
    ``[A-Za-z0-9]`` replaced by ``-`` (so ``/Users/x/my.app`` becomes
    ``-Users-x-my-app``). Derived rather than searched: a glob over every
    project directory would be O(all transcripts) per tick, and two projects
    can share a session-id-shaped filename only if the id itself collides.
    """
    root = (claude_dir or get_claude_config_home()) / "projects"
    slug = "".join(c if c.isalnum() else "-" for c in session.cwd)
    return root / slug / f"{session.session_id}.jsonl"


def _last_entry(path: Path) -> dict | None:
    """The last parseable JSON object in ``path``, or None.

    Reads only the tail. The final line can be a partial write (Claude Code
    appends while we read), and the first line of a mid-file seek is almost
    always a fragment, so every line is tried and unparseable ones skipped
    rather than treated as an error.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > _TAIL_BYTES:
                fh.seek(size - _TAIL_BYTES)
            blob = fh.read()
    except OSError as e:
        _logger.debug("Transcript unreadable (%s): %r", path, e)
        return None

    for line in reversed(blob.split(b"\n")):
        line = line.strip()
        if not line.startswith(b"{"):
            continue
        try:
            entry = json.loads(line.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(entry, dict):
            return entry
    return None


def is_limit_stop(entry: dict | None) -> bool:
    """Whether ``entry`` is the terminal plan-limit turn.

    Claude Code writes TWO different rate-limit shapes, and only one of them
    means work has stopped:

    * retryable, mid-turn — ``{"type": "system", "subtype": "api_error",
      "retryAttempt": 2, "maxRetries": 10, ...}``. Claude Code retries these
      itself; nudging would interrupt a turn that is still running.
    * terminal, end-of-turn — a synthetic assistant message carrying
      ``isApiErrorMessage`` with ``error == "rate_limit"``. Work has stopped
      and only a new user turn restarts it.

    Keyed on the structural fields rather than the rendered text ("You've hit
    your weekly limit · resets ...") because that string is user-facing,
    localized by the account's timezone, and differs per window (5-hour vs
    weekly). ``retryAttempt`` is checked as a belt-and-braces exclusion: a
    future build that adds retry bookkeeping to the terminal shape would mean
    Claude Code is still working on it.
    """
    if not isinstance(entry, dict):
        return False
    if entry.get("type") != "assistant":
        return False
    if not entry.get("isApiErrorMessage"):
        return False
    if entry.get("error") != "rate_limit":
        return False
    return "retryAttempt" not in entry


def _limit_text(entry: dict) -> str:
    """The rendered limit message from a terminal limit-stop entry."""
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    return text
    return "usage limit reached"


def find_stopped_sessions(claude_dir: Path | None = None) -> list[StoppedSession]:
    """Every live, messageable session whose transcript ends in a limit stop.

    Skips sessions that cannot be nudged at all — no socket path, or an
    unrecognised ``peerProtocol`` — so a caller never records a session it
    would later fail to resume.
    """
    stopped: list[StoppedSession] = []
    for session in list_sessions(claude_dir):
        if not session.messaging_socket_path or not session.session_id:
            continue
        if session.peer_protocol != PEER_PROTOCOL:
            # Refuse to speak a wire format this module was not written
            # against. Louder than debug on purpose: a Claude Code upgrade
            # silently disabling resume should be discoverable from the log
            # rather than looking like the feature never fired.
            _logger.warning(
                "Session %s advertises peerProtocol %s (expected %s); "
                "not resuming it — claude-swap may need updating",
                session.session_id, session.peer_protocol, PEER_PROTOCOL,
            )
            continue
        entry = _last_entry(transcript_path(session, claude_dir))
        if not is_limit_stop(entry):
            continue
        stopped.append(StoppedSession(
            session_id=session.session_id,
            pid=session.pid,
            cwd=session.cwd,
            socket_path=session.messaging_socket_path,
            message=_limit_text(entry),
        ))
    return stopped


def _peer_token(pid: int, claude_dir: Path | None = None) -> str | None:
    """The auth token for ``pid``'s inbox, from its sibling key file.

    Claude Code publishes ``<pid>.<hash>.key`` (mode 0600) next to the session
    record. The hash is over the socket path, so the file is found by prefix
    rather than recomputed. A session whose key is missing is treated as
    unauthenticated-only; the receiver decides whether to accept that.
    """
    sessions_dir = (claude_dir or get_claude_config_home()) / "sessions"
    try:
        for path in sessions_dir.glob(f"{pid}.*.key"):
            data = json.loads(path.read_text(encoding="utf-8"))
            token = data.get("peerToken")
            if isinstance(token, str) and token:
                return token
    except (OSError, json.JSONDecodeError, ValueError) as e:
        _logger.debug("Peer key unreadable for pid %s: %r", pid, e)
    return None


def send_peer_message(
    socket_path: str,
    text: str,
    *,
    pid: int,
    claude_dir: Path | None = None,
    timeout_s: float = _SEND_TIMEOUT_S,
) -> bool:
    """Deliver ``text`` to a session's inbox as a peer message.

    The wire format is newline-delimited JSON over the session's Unix socket:
    an auth frame carrying the peer token first, then the message frame. It
    arrives in the target as a user-role turn wrapped in
    ``<cross-session-message>``, which is what wakes an idle session.

    Returns True only when the whole exchange completed. Every failure mode —
    socket gone, permission denied, timeout, protocol change, unsupported
    platform — is logged and returns False, because the caller's alternative
    to a failed nudge is an un-resumed session, never a crash.
    """
    if not hasattr(socket, "AF_UNIX"):
        # Windows: no Unix domain sockets in Python's socket module, and
        # Claude Code does not use one there either — it binds a named pipe
        # (``\\.\pipe\...``) instead, a different transport this does not
        # speak. Refuse up front rather than at the `socket()` call, where it
        # would raise AttributeError out of a caller documented never to see
        # one. `find_stopped_sessions` still returns nothing to nudge on
        # Windows (no socket path in the session record), so in practice this
        # is the belt to that braces.
        _logger.debug("Peer messaging is POSIX-only; not resuming on this platform")
        return False

    frames: list[dict] = []
    token = _peer_token(pid, claude_dir)
    if token:
        frames.append({"type": "auth", "token": token})
    frames.append({
        "msg_id": str(uuid.uuid4()),
        "type": "user",
        "message": {"role": "user", "content": text},
        "priority": "next",
    })

    payload = b"".join(json.dumps(f).encode("utf-8") + b"\n" for f in frames)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout_s)
        sock.connect(socket_path)
        sock.sendall(payload)
        return True
    except (OSError, socket.timeout) as e:
        _logger.warning("Could not nudge session at %s: %r", socket_path, e)
        return False
    finally:
        sock.close()


# The nudge itself. Deliberately minimal and non-prescriptive: a session
# stopped mid-task resumes that task on ANY new turn (measured — a probe
# asking only for an unrelated file write was obeyed, and the session then
# returned to its own work unprompted). So this says what happened and gets
# out of the way, rather than issuing an instruction that would compete with
# whatever the user actually had running.
#
# It does NOT claim a switch happened: quota comes back two ways — cswap moved
# to another account, OR the account already in use had its own window roll
# over — and the nudge fires on either. Naming the wrong one would be a
# needless falsehood in a message the receiving session reads as context.
RESUME_MESSAGE = (
    "[claude-swap] Your account hit its usage limit and this session stopped. "
    "There is quota available again, so you can continue where you left off."
)


def resume_sessions(
    sessions: list[StoppedSession], claude_dir: Path | None = None
) -> list[StoppedSession]:
    """Nudge each stopped session. Returns the ones that accepted delivery."""
    resumed = []
    for stopped in sessions:
        if send_peer_message(
            stopped.socket_path,
            RESUME_MESSAGE,
            pid=stopped.pid,
            claude_dir=claude_dir,
        ):
            resumed.append(stopped)
    return resumed
