"""Turn Remote Control back on after the default login changes.

A swap (``cswap switch``, ``cswap auto``, or a plain ``/login``) changes the
account a running Claude Code session is signed in with. Claude Code notices,
stops Remote Control, and adds a notice to the conversation asking you to run
``/remote-control`` for the current account. It never reconnects on its own,
so with auto-switching every swap silently takes the session off your phone.

``cswap rc`` launches claude behind a pty relay that does that step for you. It
follows the session's own records rather than the screen:

- ``<config>/sessions/<pid>.json`` names the session's conversation and says
  whether a turn is running or a dialog is waiting on the user.
- The conversation's transcript (``<config>/projects/*/<sessionId>.jsonl``)
  receives the disconnect notice, and later the ``/remote-control is active``
  status, as ``type: system`` entries.

After a notice the relay waits 30s: run straight away, ``/remote-control`` finds
the old connection still closing and opens the Remote Control panel instead of
reconnecting. Once claude is idle, not waiting on a dialog, and the user hasn't
typed for a few seconds, it stashes any unsent draft with Claude Code's own
Ctrl+S (``chat:stash``) and runs ``/remote-control``; Claude Code puts the draft
back by itself once the command has run. The active status confirms the
reconnect. If the panel opened anyway, Esc closes it (which also restores the
draft) and the relay tries again later; if nothing confirms, likewise.

If the user stashed a prompt themselves and hasn't sent a message since, Ctrl+S
would pop that stash into the prompt instead, so the relay leaves the session
alone. Nothing here touches credentials or network traffic; the new Remote
Control session belongs to whichever account is signed in, exactly as if the
user had typed the command.

POSIX only: the relay needs a pseudo-terminal. Relies on the default ``ctrl+s``
binding for ``chat:stash`` and on the default (non-vim) input mode.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from claude_swap.exceptions import SessionError
from claude_swap.paths import get_claude_config_home

_logger = logging.getLogger("claude-swap")

# Notices worth rerunning /remote-control for. Remote Control ending from
# another device and "run /login" notices are left alone: the first was
# deliberate, the second needs a human before /remote-control can work.
RECONNECT_REASONS = (
    "account or organization changed on this machine",
    "could not verify the signed-in account",
    "Previous session is unavailable",
    "run /remote-control to reconnect",
)
ACTIVE_PREFIX = "/remote-control is active"

STASH = b"\x13"  # Ctrl+S: chat:stash in Claude Code's default keybindings
ESC = b"\x1b"
# (delay after the previous key, bytes): stash the draft, then run the command.
# Text and Enter go in separate writes: one chunk ending in "\r" reads as a
# paste, which inserts a newline instead of submitting. No key restores the
# draft: Claude Code does that itself once the command has run, and a second
# Ctrl+S would stash it again.
RUN_KEYS = ((0.0, STASH), (0.3, b"/remote-control"), (0.4, b"\r"))

RECONNECT_DELAY_S = 30.0  # after the notice, before the first try
RETRY_DELAY_S = 30.0
PANEL_CHECK_S = 3.0  # after Enter, look for the Remote Control panel
CONFIRM_WITHIN_S = 20.0
USER_IDLE_S = 5.0
MAX_TRIES = 3
TRY_WINDOW_S = 15 * 60
POLL_INTERVAL_S = 2.0
POLL_ACTIVE_S = 0.5  # while a reconnect is owed or in flight

_MANUAL_COMMANDS = (b"/rc", b"/remote-control")
_BRACKETED_PASTE_START = b"\x1b[200~"

# Transcript events
NOTICE = "notice"
CONNECTED = "connected"

# Reconnector outcomes
SENT = "sent"
BACK = "back"
PANEL = "panel"
UNCONFIRMED = "unconfirmed"
STASH_HELD = "stash_held"
RATE_LIMITED = "rate_limited"


def transcript_event(entry: object) -> tuple[str, str] | None:
    """Classify a transcript entry: ``(NOTICE | CONNECTED, text)``, or None."""
    if not isinstance(entry, dict) or entry.get("type") != "system":
        return None
    content = entry.get("content")
    if not isinstance(content, str):
        return None
    if entry.get("subtype") == "bridge_status" and content.startswith(ACTIVE_PREFIX):
        return CONNECTED, content
    if content.startswith("Remote Control") and any(r in content for r in RECONNECT_REASONS):
        return NOTICE, content
    return None


class SessionWatcher:
    """Follows one claude process's session record and transcript.

    Only entries written after the watcher first sees a conversation count:
    an existing transcript (``--resume``, ``/resume``) is read from its
    current end, while a fresh conversation's transcript, which appears with
    its first entry, is read from the start.
    """

    def __init__(self, pid: int, claude_dir: Path | None = None):
        self.pid = pid
        self.claude_dir = claude_dir or get_claude_config_home()
        self.state: dict = {}
        self.session_id: str | None = None
        self.transcript: str | None = None
        self._offset = 0
        self._partial = b""

    def _find_transcript(self, session_id: str) -> str | None:
        pattern = os.path.join(
            glob.escape(str(self.claude_dir)), "projects", "*",
            f"{glob.escape(session_id)}.jsonl",
        )
        matches = glob.glob(pattern)
        return matches[0] if matches else None

    def poll(self) -> list[tuple[str, str]]:
        """Refresh the session state; return new Remote Control events, oldest first."""
        record = self.claude_dir / "sessions" / f"{self.pid}.json"
        try:
            state = json.loads(record.read_text())
        except (OSError, ValueError):
            return []
        if not isinstance(state, dict):
            return []
        self.state = state

        session_id = state.get("sessionId")
        if not isinstance(session_id, str):
            session_id = None
        if session_id != self.session_id:
            self.session_id = session_id
            self.transcript = self._find_transcript(session_id) if session_id else None
            self._offset = _size(self.transcript) if self.transcript else 0
            self._partial = b""
        elif self.transcript is None and session_id:
            self.transcript = self._find_transcript(session_id)
        if not self.transcript:
            return []

        try:
            with open(self.transcript, "rb") as f:
                size = os.fstat(f.fileno()).st_size
                if size < self._offset:  # rewritten under us: resync at the end
                    self._offset, self._partial = size, b""
                f.seek(self._offset)
                data = f.read()
        except OSError:
            return []
        if not data:
            return []
        self._offset += len(data)
        lines = (self._partial + data).split(b"\n")
        self._partial = lines.pop()

        events = []
        for line in lines:
            if b"remote-control" not in line.lower() and b"Remote Control" not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            event = transcript_event(entry)
            if event:
                events.append(event)
        return events

    @property
    def idle(self) -> bool:
        """No turn running and no dialog open. A missing status means no turn has run yet."""
        return self.state.get("status") in (None, "idle") and not self.dialog_open

    @property
    def dialog_open(self) -> bool:
        return bool(self.state.get("waitingFor"))


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


class KeyTracker:
    """Best guess at what the user types, from the keystrokes relayed to claude.

    Two things matter: a manual ``/rc`` (the relay then stands down) and
    whether a stash of the user's own may be held (Ctrl+S since the last
    non-slash message), in which case the relay's Ctrl+S would pop it.
    Escape sequences (arrows, focus and mouse reports) are ignored.
    """

    def __init__(self) -> None:
        self._line = bytearray()
        self.stash_maybe_held = False

    def feed(self, data: bytes) -> bytes | None:
        """Track ``data``; return the line it submitted with Enter, if any."""
        if data.startswith(ESC):
            if data.startswith(_BRACKETED_PASTE_START):
                self._line.extend(b"paste")
            return None
        submitted = None
        for byte in data:
            if byte == 0x0D:  # Enter
                submitted = bytes(self._line).strip()
                # Sending a message restores a stashed prompt; slash commands don't.
                if submitted and not submitted.startswith(b"/"):
                    self.stash_maybe_held = False
                self._line.clear()
            elif byte == STASH[0]:
                self.stash_maybe_held = True
                self._line.clear()
            elif byte in (0x03, 0x15):  # Ctrl+C, Ctrl+U
                self._line.clear()
            elif byte in (0x7F, 0x08):  # Backspace
                if self._line:
                    self._line.pop()
            elif byte >= 0x20 or byte == 0x0A:
                self._line.append(byte)
        return submitted


@dataclass
class Attempt:
    sent: float  # when Enter is typed
    checked: bool = False  # looked for the panel
    connected: bool = False  # saw the active status


def schedule_keys(steps: tuple[tuple[float, bytes], ...], now: float) -> list[tuple[float, bytes]]:
    """Turn (delay after the previous key, bytes) steps into (due time, bytes)."""
    queued, due = [], now
    for delay, data in steps:
        due += delay
        queued.append((due, data))
    return queued


class Reconnector:
    """Owes, times, runs and confirms the reconnects for one session.

    Pure state: the relay feeds it transcript events, the session's state and
    the clock, and types the keys it hands back.
    """

    def __init__(
        self,
        *,
        reconnect_delay_s: float = RECONNECT_DELAY_S,
        retry_delay_s: float = RETRY_DELAY_S,
        panel_check_s: float = PANEL_CHECK_S,
        confirm_within_s: float = CONFIRM_WITHIN_S,
        user_idle_s: float = USER_IDLE_S,
        max_tries: int = MAX_TRIES,
        window_s: float = TRY_WINDOW_S,
    ):
        self.reconnect_delay_s = reconnect_delay_s
        self.retry_delay_s = retry_delay_s
        self.panel_check_s = panel_check_s
        self.confirm_within_s = confirm_within_s
        self.user_idle_s = user_idle_s
        self.max_tries = max_tries
        self.window_s = window_s
        self.pending = False  # a reconnect is owed
        self.due_at = 0.0  # earliest time to try it
        self.attempt: Attempt | None = None
        self._tries: list[float] = []

    @property
    def active(self) -> bool:
        return self.pending or self.attempt is not None

    def notice(self, now: float) -> None:
        self.pending, self.due_at = True, now + self.reconnect_delay_s

    def connected(self) -> bool:
        """Remote Control reported active; True when it came back without us."""
        if self.attempt is not None:
            self.attempt.connected = True
            return False
        if self.pending:
            self.pending = False
            return True
        return False

    def submitted(self, line: bytes) -> bool:
        """The user sent ``line``; True when it was their own /remote-control."""
        if self.pending and line.strip() in _MANUAL_COMMANDS:
            self.pending = False
            return True
        return False

    def step(
        self,
        now: float,
        *,
        session_idle: bool,
        dialog_open: bool,
        last_input: float,
        stash_maybe_held: bool,
    ) -> tuple[str | None, list[tuple[float, bytes]]]:
        """Advance one tick; return ``(outcome or None, keys to type)``.

        Call it only once the keys it handed back earlier have been typed.
        """
        attempt = self.attempt
        if attempt is not None:
            if not attempt.checked:
                if now >= attempt.sent + self.panel_check_s:
                    attempt.checked = True
                    if dialog_open and not attempt.connected:
                        # Too early: Claude Code still counts the old connection and
                        # showed its panel. Closing it also brings the draft back.
                        self.attempt, self.pending = None, True
                        self.due_at = now + self.retry_delay_s
                        return PANEL, [(now, ESC)]
            elif attempt.connected:
                self.attempt, self.pending = None, False
                return BACK, []
            elif now >= attempt.sent + self.confirm_within_s:
                self.attempt, self.pending = None, True
                self.due_at = now + self.retry_delay_s
                return UNCONFIRMED, []
            return None, []

        if (
            not self.pending
            or now < self.due_at
            or not session_idle
            or now - last_input < self.user_idle_s
        ):
            return None, []
        self.pending = False
        self._tries = [t for t in self._tries if now - t < self.window_s]
        if stash_maybe_held:
            return STASH_HELD, []
        if len(self._tries) >= self.max_tries:
            return RATE_LIMITED, []
        self._tries.append(now)
        keys = schedule_keys(RUN_KEYS, now)
        self.attempt = Attempt(sent=keys[-1][0])
        return SENT, keys


def _log_outcome(outcome: str, session_id: str | None, reconnector: Reconnector) -> None:
    retry = f"retrying in {reconnector.retry_delay_s:.0f}s"
    messages = {
        SENT: (logging.INFO, "running /remote-control"),
        BACK: (logging.INFO, "Remote Control is back"),
        PANEL: (logging.INFO, f"got the Remote Control panel instead; closed it, {retry}"),
        UNCONFIRMED: (logging.WARNING, f"Remote Control didn't come back; {retry}"),
        STASH_HELD: (
            logging.INFO,
            "a stashed prompt may be held, which Ctrl+S would bring back; "
            "leaving Remote Control off",
        ),
        RATE_LIMITED: (
            logging.WARNING,
            f"{reconnector.max_tries} tries in {reconnector.window_s // 60:.0f} min, "
            "leaving Remote Control off",
        ),
    }
    level, message = messages[outcome]
    _logger.log(level, "rc: session %s: %s", session_id, message)


def _write_all(fd: int, data: bytes) -> None:
    while data:
        data = data[os.write(fd, data):]


def run_relay(
    claude_bin: str,
    claude_args: list[str],
    claude_dir: Path | None = None,
    reconnector: Reconnector | None = None,
) -> int:
    """Run claude behind a pty relay that reconnects Remote Control; return its exit code."""
    import fcntl
    import pty
    import select
    import signal
    import termios
    import tty

    def winsize() -> bytes | None:
        try:
            return fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
        except OSError:
            return None

    stdin, stdout = sys.stdin.fileno(), sys.stdout.fileno()
    attrs = termios.tcgetattr(stdin)
    size = winsize()
    pid, master = pty.fork()
    if pid == 0:  # child: take the terminal's settings, then become claude
        try:
            termios.tcsetattr(0, termios.TCSANOW, attrs)
            if size:
                fcntl.ioctl(0, termios.TIOCSWINSZ, size)
            os.execv(claude_bin, [claude_bin, *claude_args])
        except OSError as e:
            os.write(2, f"cswap rc: {e}\n".encode())
        os._exit(127)

    def sync_winsize(*_: object) -> None:
        current = winsize()
        if current:
            try:
                fcntl.ioctl(master, termios.TIOCSWINSZ, current)
            except OSError:
                pass

    def forward(signum: int, _frame: object) -> None:
        try:
            os.kill(pid, signum)
        except ProcessLookupError:
            pass

    signal.signal(signal.SIGWINCH, sync_winsize)
    signal.signal(signal.SIGHUP, forward)
    signal.signal(signal.SIGTERM, forward)

    watcher = SessionWatcher(pid, claude_dir)
    reconnector = reconnector or Reconnector()
    keys = KeyTracker()
    queued: list[tuple[float, bytes]] = []  # (due time, bytes) still to type
    last_input = 0.0
    next_poll = 0.0
    inputs = [stdin, master]

    try:
        tty.setraw(stdin)
        while True:
            busy = bool(queued) or reconnector.attempt is not None
            readable, _, _ = select.select(inputs, [], [], 0.1 if busy else 0.5)

            if master in readable:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    data = b""
                if not data:
                    break  # claude exited
                try:
                    _write_all(stdout, data)
                except OSError:
                    pass

            if stdin in readable:
                try:
                    data = os.read(stdin, 65536)
                except OSError:
                    data = b""
                if not data:
                    # The terminal went away; don't leave claude running without one.
                    inputs = [master]
                    forward(signal.SIGHUP, None)
                else:
                    _write_all(master, data)
                    last_input = time.monotonic()
                    line = keys.feed(data)
                    if line is not None and reconnector.submitted(line):
                        _logger.info("rc: session %s: reconnected by hand", watcher.session_id)

            now = time.monotonic()
            while queued and now >= queued[0][0]:
                _write_all(master, queued.pop(0)[1])

            if now < next_poll:
                continue
            next_poll = now + (POLL_ACTIVE_S if busy or reconnector.active else POLL_INTERVAL_S)
            for kind, text in watcher.poll():
                if kind == NOTICE:
                    _logger.info("rc: session %s: %s", watcher.session_id, text)
                    reconnector.notice(now)
                elif reconnector.connected():
                    _logger.info(
                        "rc: session %s: Remote Control came back without cswap",
                        watcher.session_id,
                    )
            if queued:
                continue
            outcome, new_keys = reconnector.step(
                now,
                session_idle=watcher.idle,
                dialog_open=watcher.dialog_open,
                last_input=last_input,
                stash_maybe_held=keys.stash_maybe_held,
            )
            queued.extend(new_keys)
            if outcome:
                _log_outcome(outcome, watcher.session_id, reconnector)
    finally:
        try:
            termios.tcsetattr(stdin, termios.TCSADRAIN, attrs)
        except termios.error:
            pass

    _, status = os.waitpid(pid, 0)
    code = os.waitstatus_to_exitcode(status)
    return 128 - code if code < 0 else code


def launch(claude_args: list[str]) -> NoReturn:
    """Launch claude on the default login with Remote Control reconnection."""
    if sys.platform == "win32":
        raise SessionError("cswap rc needs a POSIX terminal (macOS, Linux or WSL).")
    claude_bin = shutil.which("claude")
    if not claude_bin:
        raise SessionError("'claude' was not found on PATH. Install Claude Code first.")
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        # Nothing to relay without a terminal (claude -p, pipes): run it plainly.
        os.execv(claude_bin, [claude_bin, *claude_args])
    sys.exit(run_relay(claude_bin, claude_args))
