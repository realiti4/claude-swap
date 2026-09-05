"""Resume Claude Code sessions that stopped on a plan usage limit.

When every account is exhausted, work in progress stops with a terminal
rate-limit turn and nothing restarts it: the window reset arrives hours
later, cswap switches to the recovered account, and the session sits idle
until a human notices and presses enter. This module closes that gap.

Whom to wake is decided two ways, because the two callers know different
things. The auto-switch engine RECORDS the sessions it watched stop and
nudges exactly those once a switch lands. A manual switch has no such
record to draw on, so it SCANS at switch time
(:func:`resume_after_manual_switch`) and takes its conservatism from the
liveness filter instead.

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
import os
import re
import socket
import string
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from claude_swap.paths import get_claude_config_home
from claude_swap.process_detection import ClaudeSession, list_sessions
from claude_swap.settings import load_settings

# The shared logger, NOT a per-module one: `setup_logging` attaches the
# rotating file handler to "claude-swap", and a "claude_swap.*" name is a
# different tree — it propagates to root and is written nowhere.
_logger = logging.getLogger("claude-swap")

# The ``peerProtocol`` value in ~/.claude/sessions/<pid>.json this module was
# written against. A session advertising anything else is skipped rather than
# messaged: the frame shape below is reverse-engineered, so guessing across a
# version bump risks delivering garbage into someone's live session.
PEER_PROTOCOL = 1

# Frame-format version stamped on every message Claude Code's own sender
# emits. Distinct from PEER_PROTOCOL, which versions the session record.
PEER_MSG_VERSION = 1

# Characters the receiver's envelope parser accepts unescaped in a `from`
# address; everything else must be percent-encoded (Claude Code's own sender
# does the same). Mirrors its `[A-Za-z0-9%:_/.\-]` character class.
_ADDR_SAFE = frozenset(string.ascii_letters + string.digits + "%:_/.\\-")

# How much of a transcript's tail to read when classifying its last turn.
# Transcripts reach hundreds of MB (a real one measured 163MB), so they are
# never read whole. One JSONL entry carrying a large tool result can be a few
# hundred KB, so this holds the last handful of entries with margin.
_TAIL_BYTES = 512 * 1024

# Connect/response budget for one nudge. The engine sends these from its tick,
# so a hung socket must not stall auto-switching; Claude Code's own client
# uses 5s for the same exchange.
_SEND_TIMEOUT_S = 5.0

# Waking a session races Claude Code's in-process credential cache. Measured
# 2026-08-27: `cswap` wrote the new account's credentials at 13:30:50.622, the
# nudge landed 43ms later, and the turn it started was rejected at 13:30:52.792
# with a real 429 whose quotaLimits.resetsAt was the OLD account's reset — the
# session was still holding the previous token. The same session worked 25s
# later with no further help. So the first nudge can burn itself, and one
# burned nudge used to strand the session for good: the engine clears its
# record unconditionally and `cswap use` is a short-lived process.
#
# These pace a bounded retry across that window. The delays are spread rather
# than tight because detecting a burn is fast (~2s) — three back-to-back
# nudges would all land inside the same stale window and change nothing.
RESUME_RETRY_DELAYS_S: tuple[float, ...] = (5.0, 15.0)
RESUME_VERIFY_S = 10.0  # how long to watch one nudge for a verdict
RESUME_POLL_S = 1.0  # how often to re-read the tail while watching


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
    # The ``uuid`` of the limit-stop entry this session was found on. A nudge
    # that burns on stale credentials appends a NEW limit stop, so comparing
    # identities is what tells "burned, nudge again" from "held for review,
    # leave it alone" — two states that look identical from the sender's side.
    # Empty when the transcript entry carried no uuid, which disables
    # verification for that session rather than guessing between the two.
    stop_uuid: str = ""


def transcript_path(session: ClaudeSession, claude_dir: Path | None = None) -> Path:
    """Where Claude Code stores ``session``'s transcript.

    The project directory is the session's cwd with every character outside
    ``[A-Za-z0-9]`` replaced by ``-`` (so ``/Users/x/my.app`` becomes
    ``-Users-x-my-app``). Derived rather than searched: a glob over every
    project directory would be O(all transcripts) per tick, and two projects
    can share a session-id-shaped filename only if the id itself collides.
    """
    return _transcript_for(session.cwd, session.session_id, claude_dir)


def _transcript_for(cwd: str, session_id: str, claude_dir: Path | None) -> Path:
    """The transcript for a cwd/session-id pair (see :func:`transcript_path`).

    Split out because the retry loop re-reads a :class:`StoppedSession`'s tail,
    and that has no ``ClaudeSession`` to hand — only the two fields the path is
    actually derived from.
    """
    root = (claude_dir or get_claude_config_home()) / "projects"
    slug = "".join(c if c.isalnum() else "-" for c in cwd)
    return root / slug / f"{session_id}.jsonl"


def _decides_the_turn(entry: dict) -> bool:
    """Whether ``entry`` says anything about whether work has stopped.

    A transcript is mostly NOT turns: one real capture held 17 distinct
    non-turn types (``attachment``, ``bridge-session``, ``worktree-state``,
    ``system``/``turn_duration``, ``pr-link``, ...) against 3 that matter, and
    Claude Code keeps appending them after work stops. So the tail scan walks
    back past bookkeeping to the last entry that actually decides the
    question, of which there are two kinds:

    * a conversation turn (``user``/``assistant``) — the thing
      :func:`is_limit_stop` classifies;
    * a retryable mid-turn 429 (``system``/``api_error``) — not a turn, but it
      means one is still in flight, so the walk must STOP there rather than
      skip past it to the limit stop the retry is retrying.

    An allow-list rather than a list of types to skip: the bookkeeping set is
    open-ended and grows with every Claude Code release, while the entries
    that carry a turn have been these two shapes throughout.
    """
    if entry.get("type") in ("user", "assistant"):
        return True
    return entry.get("type") == "system" and entry.get("subtype") == "api_error"


def _last_turn_entry(path: Path) -> dict | None:
    """The last entry in ``path`` that decides whether work has stopped.

    Reads only the tail. The final line can be a partial write (Claude Code
    appends while we read), and the first line of a mid-file seek is almost
    always a fragment, so every line is tried and unparseable ones skipped
    rather than treated as an error. A tail holding nothing but bookkeeping
    yields None — conservative, and the session is simply not recorded.
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
        if isinstance(entry, dict) and _decides_the_turn(entry):
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
        entry = _last_turn_entry(transcript_path(session, claude_dir))
        if not is_limit_stop(entry):
            continue
        stopped.append(StoppedSession(
            session_id=session.session_id,
            pid=session.pid,
            cwd=session.cwd,
            socket_path=session.messaging_socket_path,
            message=_limit_text(entry),
            stop_uuid=str(entry.get("uuid") or ""),
        ))
    return stopped


@dataclass(frozen=True)
class LimitStop:
    """A live session's terminal limit stop, read as evidence about quota.

    The account a session runs on is whichever one claude-swap has active, so
    a session hitting its plan limit says the ACTIVE account is spent — sooner
    and more cheaply than the usage API, which only reports it on the next
    poll (up to a full interval late) and costs a request to ask.
    """

    session_id: str
    stop_uuid: str
    # ``quotaLimits.rateLimitType`` ("five_hour", "seven_day", ...) and
    # ``quotaLimits.resetsAt`` (epoch seconds). Both empty/zero when the entry
    # carried no ``quotaLimits`` — the stop is still real, it just says less.
    window: str
    resets_at: float
    # The entry's own timestamp: when the session actually stopped, which is
    # what decides whether the evidence predates a switch that already fixed it.
    observed_at: float


def _entry_epoch(entry: dict) -> float:
    """An entry's ``timestamp`` as epoch seconds, or 0.0 if unusable."""
    stamp = entry.get("timestamp")
    if isinstance(stamp, str):
        try:
            return datetime.fromisoformat(stamp).timestamp()
        except ValueError:
            pass
    return 0.0


class LimitStopScanner:
    """Reports terminal limit stops that are news since the last scan.

    Built to run every few seconds between engine ticks, so it is careful
    about cost: a transcript is re-read only when its file mtime moved, which
    makes an idle machine one ``stat`` per live session. Stops already
    reported are remembered by uuid so a stop is news exactly once — an entry
    with no uuid is therefore never reported, since it could not be told apart
    from the next one.

    Unlike :func:`find_stopped_sessions` this does NOT require a messageable
    socket: what a transcript says about the account's quota is true whether
    or not that session can be nudged afterwards.
    """

    def __init__(self, claude_dir: Path | None = None) -> None:
        self._claude_dir = claude_dir
        self._mtimes: dict[Path, float] = {}
        self._seen: set[str] = set()

    def scan(self) -> list["LimitStop"]:
        found: list[LimitStop] = []
        for session in list_sessions(self._claude_dir):
            if not session.session_id:
                continue
            path = transcript_path(session, self._claude_dir)
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if self._mtimes.get(path) == mtime:
                continue  # untouched since the last look — nothing new to read
            self._mtimes[path] = mtime
            entry = _last_turn_entry(path)
            if not is_limit_stop(entry):
                continue
            stop_uuid = str(entry.get("uuid") or "")
            if not stop_uuid or stop_uuid in self._seen:
                continue
            self._seen.add(stop_uuid)
            quota = entry.get("quotaLimits")
            quota = quota if isinstance(quota, dict) else {}
            try:
                resets_at = float(quota.get("resetsAt") or 0.0)
            except (TypeError, ValueError):
                resets_at = 0.0
            found.append(LimitStop(
                session_id=session.session_id,
                stop_uuid=stop_uuid,
                window=str(quota.get("rateLimitType") or ""),
                resets_at=resets_at,
                observed_at=_entry_epoch(entry),
            ))
        return found


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


def _own_socket_address() -> str:
    """This process's peer address, in the ``uds:<escaped path>`` form.

    cswap binds no inbox of its own, so there is no real socket to name — a
    reply would have nowhere to go, which is correct for a one-way nudge. The
    address still has to be well-formed or the receiver renders the sender as
    "an unidentified session": the parse accepts only ``[A-Za-z0-9%:_/.\\-]``,
    with anything else percent-encoded.
    """
    path = f"/tmp/cswap-{os.getpid()}.sock"
    escaped = "".join(
        c if c in _ADDR_SAFE else "".join(f"%{b:02X}" for b in c.encode("utf-8"))
        for c in path
    )
    return f"uds:{escaped}"


def wrap_peer_body(text: str) -> str:
    """Wrap ``text`` in the envelope Claude Code's inbox parses.

    The SENDER builds this envelope inside the message content; it is not
    assembled from JSON fields. A body with no envelope is delivered as coming
    from "an unidentified session".

    Attribute order is fixed by the receiver's parse regex
    (``from``, ``from-session``, ``hop-chain``, ``from-name``, ``from-mode``)
    and unrecognised orderings do not match at all.

    ``from-mode`` is an enum of exactly ``bypass`` | ``prompting``, and the
    receiver COMPARES it against its own mode: an absent mode is held into a
    bypass-mode session as ``no-mode-asserted``, and a mismatched one as
    ``mode-mismatch``. Neither value is literally true of a background daemon,
    which has no interactive permission posture at all. ``bypass`` is sent
    because it is the value that reaches a session in either mode, and because
    the alternative route to the same outcome is the user setting
    ``crossSessionInbound: accept``, which additionally accepts messages from
    every other local sender. This is a deliberate, documented tradeoff: the
    nudge is a fixed, non-instructional string (:data:`RESUME_MESSAGE`) sent by
    the user's own tool to the user's own sessions, at their explicit opt-in
    via ``autoswitch.resumeStoppedSessions``.
    """
    # Escape any closing-tag lookalike in the body, as Claude Code's own
    # sender does, so a body can never forge an envelope boundary. Ours is a
    # constant, but this function is the seam where that stops being true.
    safe = re.sub(r"</(?=cross-session-message)", "<\\\\", text)
    # NEWLINES around the body, not spaces. The receiver re-renders the parsed
    # pieces and requires the result to equal the original byte-for-byte
    # (`if (oCr(...) !== n) return;`), so a single wrong separator makes the
    # whole envelope fail to parse — and it is then shown to the user as
    # literal text from "an unidentified session". Measured against a live
    # session: a space-separated body was held exactly that way.
    return (
        f'<cross-session-message from="{_own_socket_address()}"'
        f' from-name="claude-swap" from-mode="bypass">\n{safe}\n'
        "</cross-session-message>"
    )


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
        # `msgV` is the frame version every real sender stamps; `from` is read
        # off the FRAME (not the envelope) and is what the receiver records as
        # `origin.from` — without it a delivered message is attributed to
        # "unknown". The name and mode come from the envelope instead; the two
        # layers are read separately.
        "msgV": PEER_MSG_VERSION,
        "msg_id": str(uuid.uuid4()),
        "type": "user",
        "from": _own_socket_address(),
        "message": {"role": "user", "content": wrap_peer_body(text)},
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


def _nudge_text(attempt: int) -> str:
    """The nudge to send on one attempt — distinct per attempt.

    Claude Code discards a peer message identical to the sender's previous
    one ("Dropped a peer message ... identical to the previous message from
    this sender", observed 2026-08-27), which silently made every retry a
    no-op: the bytes were written, ``send_peer_message`` reported success,
    and the inbox threw them away. The suffix only has to differ; it stays
    non-instructional like the base message.
    """
    if not attempt:
        return RESUME_MESSAGE
    return f"{RESUME_MESSAGE} (nudge {attempt + 1})"


def _active_headroom(switcher) -> float | None:
    """Measured headroom of the account that is live right now, or ``None``.

    Store-only — no network and no keychain refresh. This runs on the path of
    a switch the user just made by hand, so it must not add latency to it.

    ``None`` means "could not measure", never "empty": an unreadable row is
    not evidence of an exhausted account, the same rule the engine applies in
    ``_rank_candidates``.
    """
    try:
        from claude_swap import oauth
        from claude_swap.snapshot_source import SnapshotSource

        raw = load_settings(switcher.backup_dir).model or ""
        models = tuple(m.strip() for m in raw.split(",") if m.strip())
        for account in SnapshotSource(switcher).take(store_only=True).accounts:
            if account.is_active:
                return oauth.account_headroom(account.usage.last_good, models)
    except Exception as e:
        _logger.debug("Could not read the active account's headroom: %r", e)
    return None



def _nudge_verdict(stopped: StoppedSession, claude_dir: Path | None) -> str:
    """What the transcript says about a nudge already delivered to ``stopped``.

    Four tails, three meanings:

    * the ORIGINAL limit stop, unchanged — either Claude Code has not picked
      the message up yet, or it is HOLDING it for the user's review. Holds are
      invisible from this side (see the module docstring on
      ``crossSessionInbound``), so this stays ``"waiting"`` and, if it never
      changes, the nudge is simply left alone. Retrying a held message would
      queue duplicates in front of someone who has not looked yet.
    * a DIFFERENT limit stop — the nudge landed, started a turn, and that turn
      was rejected. That is the stale-credential burn, and it is worth another
      nudge once the switched-to account has propagated.
    * a user turn — our own nudge, sitting in the ~2s gap before the 429 it
      earns. Calling this recovery would declare victory in exactly the window
      the failure lives in.
    * anything else (a real assistant turn, a retryable mid-turn 429) — work
      is happening. Done.

    A missing or unreadable transcript yields ``"done"``: nothing can be
    verified, and a retry would be guesswork.
    """
    entry = _last_turn_entry(
        _transcript_for(stopped.cwd, stopped.session_id, claude_dir)
    )
    if entry is None:
        return "done"
    if is_limit_stop(entry):
        if str(entry.get("uuid") or "") == stopped.stop_uuid:
            return "waiting"
        return "burned"
    if entry.get("type") == "user":
        return "waiting"
    return "done"


def _watch_nudges(
    sent: list[StoppedSession], claude_dir: Path | None, sleep, clock
) -> list[StoppedSession]:
    """Watch delivered nudges until each resolves; return the burned ones.

    Only sessions carrying a stop identity are watched — without one a second
    limit stop is indistinguishable from the first, so there is no verdict to
    reach and no safe retry to make.
    """
    watching = [s for s in sent if s.stop_uuid]
    burned: list[StoppedSession] = []
    deadline = clock() + RESUME_VERIFY_S
    while watching and clock() < deadline:
        sleep(RESUME_POLL_S)
        still: list[StoppedSession] = []
        for stopped in watching:
            verdict = _nudge_verdict(stopped, claude_dir)
            if verdict == "waiting":
                still.append(stopped)
            elif verdict == "burned":
                # Re-baseline on the stop we just saw, so the next round
                # compares against it rather than the original.
                entry = _last_turn_entry(
                    _transcript_for(stopped.cwd, stopped.session_id, claude_dir)
                )
                burned.append(
                    replace(stopped, stop_uuid=str((entry or {}).get("uuid") or ""))
                )
        watching = still
    return burned


_CAPTURE_KEEP = 40


def capture_limit_screens(
    sessions: list[StoppedSession], backup_dir: Path | None
) -> None:
    """Save what each limit-stopped session's screen shows right now.

    6b groundwork: the limit dialog captures input, so the future nudge flow
    must recognize and dismiss it before typing — but its exact on-screen
    markers are unknown, and guessing them means typing at a live session
    blind. These captures (``<backup_dir>/limit-screens/``) are the corpus
    that recognizer gets written from. Read-only via cmux, best-effort,
    never raises; sessions outside cmux simply aren't captured. Bounded:
    only the newest ``_CAPTURE_KEEP`` files are kept.
    """
    if backup_dir is None:
        return
    try:
        from claude_swap import cmux_control

        capture_dir = Path(backup_dir) / "limit-screens"
        for stopped in sessions:
            got = cmux_control.capture_screen_for_pid(stopped.pid)
            if got is None:
                continue
            ref, screen = got
            capture_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%dT%H%M%S")
            path = capture_dir / f"{stamp}-{stopped.session_id[:8]}.txt"
            path.write_text(
                f"# session {stopped.session_id} pid {stopped.pid} "
                f"surface {ref}\n{screen}"
            )
        if capture_dir.is_dir():
            files = sorted(capture_dir.glob("*.txt"))
            for old in files[:-_CAPTURE_KEEP]:
                old.unlink(missing_ok=True)
    except Exception as e:
        _logger.debug("limit-screen capture failed: %r", e)


def _nudge_one(
    stopped: StoppedSession, text: str, claude_dir: Path | None
) -> bool:
    """Deliver one nudge — the PTY first, the peer socket as fallback.

    cmux is the PRIMARY channel (decision 2026-08-27): typing into the PTY
    has no dedup and no hold, and the screen read-back is real delivery
    confirmation. The socket covers what the PTY can't reach:

    * ``no-surface`` — the session isn't hosted in cmux;
    * ``captured-input`` — a dialog holds the input and wouldn't dismiss;
      the inbox message at least queues behind it.

    A typed delivery — verified or not — must NEVER also go to the socket:
    the message was submitted as a user turn, and a socket copy on top
    queues a duplicate. ``running`` gets nothing at all: the session is
    mid-turn, so it isn't stopped anymore and the transcript watch will
    reach its own verdict.
    """
    try:
        from claude_swap import cmux_control

        status = cmux_control.nudge_via_cmux(stopped.pid, text)
    except Exception as e:
        _logger.debug("cmux nudge unavailable for %s: %r", stopped.session_id, e)
        status = "no-surface"
    if status in ("delivered", "typed-unverified"):
        return True
    if status == "running":
        return False
    return send_peer_message(
        stopped.socket_path, text, pid=stopped.pid, claude_dir=claude_dir
    )


def resume_sessions(
    sessions: list[StoppedSession],
    claude_dir: Path | None = None,
    *,
    sleep=time.sleep,
    clock=time.monotonic,
    capture_dir: Path | None = None,
) -> list[StoppedSession]:
    """Nudge each stopped session, retrying one burned on stale credentials.

    Returns the sessions the socket accepted at least once. "Accepted" means
    the bytes were written to the inbox, NOT that Claude Code acted on them —
    it may still HOLD the message for the user's review, and holds are
    invisible from this side.

    A nudge sent moments after a switch can be rejected by the account the
    switch moved AWAY from, because Claude Code caches its token in-process
    (see ``RESUME_RETRY_DELAYS_S``). So each delivery is watched and, if it
    burned, sent again after a pause — bounded, because an account that never
    comes back must not be nudged forever.

    ``sleep``/``clock`` are injected so tests can drive the retry pacing
    without waiting on a wall clock.
    """
    accepted: dict[str, StoppedSession] = {}
    pending = list(sessions)
    # Before anything is typed at them: what do these screens show? (6b
    # corpus — see capture_limit_screens.)
    capture_limit_screens(pending, capture_dir)
    for attempt in range(1 + len(RESUME_RETRY_DELAYS_S)):
        if attempt:
            sleep(RESUME_RETRY_DELAYS_S[attempt - 1])
        sent = []
        for stopped in pending:
            if _nudge_one(stopped, _nudge_text(attempt), claude_dir):
                accepted.setdefault(stopped.session_id, stopped)
                sent.append(stopped)
        pending = _watch_nudges(sent, claude_dir, sleep, clock)
        if not pending:
            break
    if pending:
        _logger.warning(
            "%d session(s) could not be woken after %d nudges — the account "
            "they are on may still be at its limit: %s",
            len(pending),
            1 + len(RESUME_RETRY_DELAYS_S),
            ", ".join(s.session_id for s in pending),
        )
    return list(accepted.values())


def resume_after_manual_switch(
    switcher, previous_account: str | None, claude_dir: Path | None = None
) -> list[StoppedSession]:
    """Nudge stopped sessions after a human-driven switch. Never raises.

    The engine's rule is "only resume what I witnessed stopping", which it can
    afford because it runs continuously and accumulates a record across ticks.
    A manual switch has no such record: ``cswap use`` is a fresh process, and
    the menu bar's copy lives inside an engine the user may have turned off —
    which is exactly the case that stranded a session and prompted this.

    So this SCANS instead of remembering, and takes its conservatism from
    :func:`find_stopped_sessions` structurally rather than historically: the
    process must still be running, it must still publish a socket this module
    can speak to, and its transcript must still END in a terminal limit. A
    session the user abandoned is not running; one they already answered by
    hand no longer ends in a limit. Deliberately NOT time-bounded — a session
    stopped days ago by a weekly limit is the case this feature exists for.

    ``previous_account`` is the slot that was live before the switch. Both the
    CLI and the menu bar report success for a switch onto the already-active
    account, so comparing slots — not the call's return value — is what tells
    a real landing from that no-op. Nothing changed means no quota appeared,
    which means nothing to wake.
    """
    try:
        if switcher.current_account_number() == previous_account:
            return []
        if not load_settings(switcher.backup_dir).resume_stopped_sessions:
            return []
        # A slot change is not evidence of quota. Rotating off the last account
        # wraps to the first, which may be just as spent: measured 2026-08-27,
        # a wrap onto an account at 100% weekly woke every stopped session
        # straight into a limit four days from resetting.
        headroom = _active_headroom(switcher)
        if headroom is not None and headroom <= 0:
            _logger.info(
                "Not nudging after the switch: the account now live has no "
                "headroom left, so a nudge would spend each session on a "
                "limit it cannot clear."
            )
            return []
        resumed = resume_sessions(
            find_stopped_sessions(claude_dir), claude_dir,
            capture_dir=switcher.backup_dir,
        )
    except Exception as e:
        # Reading Claude Code's undocumented transcript/session state must
        # cost the nudge, never the switch the user actually asked for.
        _logger.warning("Could not resume stopped sessions: %r", e)
        return []
    if resumed:
        _logger.info(
            "Nudged %d stopped session(s) after the switch: %s",
            len(resumed), ", ".join(s.session_id for s in resumed),
        )
    return resumed
