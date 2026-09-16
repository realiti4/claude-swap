"""PTY wrapper: run ``claude``, detect mid-turn rate limits, switch and resume.

``cswap auto`` switches *proactively* between turns by polling the usage API,
but a turn that starts under the threshold can still burn through the limit
mid-flight: Claude Code aborts the turn with "You've hit your session limit"
(or weekly / per-model / spend variant) and sits idle at the prompt — even if
another account has quota, and even if ``cswap auto`` swaps the credentials
underneath it, because nothing tells the session to retry.

This module is the reactive safety net. It runs ``claude`` under a PTY,
proxies I/O transparently, and scans the output stream for the limit
messages. On a hit it:

1. Switches to the best account with headroom (``ClaudeAccountSwitcher``
   ``strategy="best"`` — freshening, quarantine and locking included). Live
   claude processes hot-reload ``~/.claude/.credentials.json`` by mtime, so
   the next request uses the new account.
2. Types ``continue`` at the prompt so the aborted turn resumes.
3. When EVERY account is exhausted, computes the earliest recovery across
   accounts — per account the latest reset among its exhausted windows
   (5h, 7d, and configured per-model scoped windows), then the minimum
   across accounts, mirroring ``AutoSwitchEngine._earliest_recovery`` —
   shows a countdown, sleeps, and switches + continues when quota returns.

The wrapper never parses or rewrites claude's arguments: everything after
``cswap wrap`` (optionally separated by ``--``) is passed through verbatim,
and claude's exit code becomes the wrapper's.
"""

from __future__ import annotations

import fcntl
import os
import re
import select
import signal
import sys
import termios
import threading
import time
import tty
from collections.abc import Callable, Sequence

from claude_swap import oauth, poll_policy
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.printer import accent, dimmed, yellowed
from claude_swap.switcher import ClaudeAccountSwitcher

# ---------------------------------------------------------------------------
# Limit-message detection
# ---------------------------------------------------------------------------

# ANSI/VT escape sequences: CSI (...\x1b[...letter), OSC (\x1b]...BEL or ST),
# and two-byte escapes. Stripped before matching so color/cursor codes
# splitting the message across a redraw can't hide it.
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"  # CSI
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC
    r"|\x1b[@-Z\\-_]"  # 2-byte
)

# Claude Code renders rate-limit exhaustion as "You've hit your <kind>", with
# kind in session limit (5h), weekly limit (7d), Opus/Sonnet/<scoped> limit
# (per-model weekly), fast limit, monthly (spend) limit, channel's monthly
# spend limit, team's shared budget. Match the prefix plus any short
# "<words> limit|budget" tail so future kinds are caught too.
_LIMIT_RE = re.compile(
    r"You've hit your [\w'’\- ]{0,48}?(?:limit|budget)", re.IGNORECASE
)

# Rolling stripped-output window scanned for the message. The notification
# renders near the bottom of the screen; a few KB of scrollback is plenty.
_SCAN_WINDOW_BYTES = 16384

# Minimum seconds between two recovery runs. Claude can re-print the notice
# on redraw/resize, and a resumed turn can hit the NEXT account's limit —
# each trigger must be a deliberate, serialized decision.
TRIGGER_DEBOUNCE_S = 15.0

# Delay between the credential swap and typing "continue": the live session
# reloads credentials on mtime, and the switch's own post-write settles fast,
# but the resumed request must not race the reload.
CONTINUE_DELAY_S = 2.0

# When no account advertises a provable reset time, re-check on this cadence
# (mirrors autoswitch.NO_RESET_FALLBACK_S).
NO_RESET_FALLBACK_S = 300.0

# Never sleep past a known reset in one go (mirrors autoswitch.MAX_SLEEP_S):
# providers can grant quota early, and the user may have re-enabled or
# re-added an account while we waited.
MAX_SLEEP_S = poll_policy.EXHAUSTED_INTERVAL_S


def strip_ansi(data: bytes) -> str:
    """Decode and de-escape a raw PTY output chunk for scanning."""
    text = data.decode("utf-8", errors="replace")
    return _ANSI_RE.sub("", text)


def find_limit_message(text: str) -> str | None:
    """The matched limit message in ``text``, or None."""
    match = _LIMIT_RE.search(text)
    return match.group(0) if match else None


def earliest_recovery_ts(
    usage_by_account: dict[str, dict | str | None],
    models: Sequence[str],
    now: float,
) -> float | None:
    """Epoch when the first exhausted account becomes usable again, or None.

    Per account, usability returns at the LATEST reset among its >=100%
    relevant windows (5h, 7d, configured scoped models) — an account blocked
    on both 5h and a scoped weekly limit isn't back when the 5h rolls over.
    The answer is the minimum across accounts. Returns None when no account
    is provably exhausted, or when some exhausted account carries no reset
    time (it could recover at any moment — don't oversleep on another
    account's later known reset). Same rule as the auto engine's blocked
    state.
    """
    earliest: float | None = None
    for usage in usage_by_account.values():
        if not isinstance(usage, dict):
            return None  # unknown usage -> recovery unprovable
        blocked = [
            True for _, pct, _ in oauth.relevant_windows(usage, models) if pct >= 100.0
        ]
        if not blocked:
            continue  # not exhausted — doesn't gate the wait
        usable_at = poll_policy.limiting_reset_ts(usage, tuple(models))
        if usable_at is None or usable_at <= now:
            return None
        if earliest is None or usable_at < earliest:
            earliest = usable_at
    return earliest


def _fmt_countdown(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


class WrapSession:
    """Run claude under a PTY and recover from mid-turn rate limits.

    ``clock``/``out`` are injectable for tests; in production they read wall
    time and write status lines to stdout.
    """

    def __init__(
        self,
        switcher: ClaudeAccountSwitcher,
        claude_args: Sequence[str],
        *,
        strategy: str = "best",
        models: Sequence[str] = (),
        auto_continue: bool = True,
        claude_bin: str = "claude",
        clock: Callable[[], float] = time.time,
        out: Callable[[str], None] | None = None,
    ):
        self.switcher = switcher
        self.claude_args = list(claude_args)
        self.strategy = strategy
        self.models = tuple(models)
        self.auto_continue = auto_continue
        self.claude_bin = claude_bin
        self.clock = clock
        # Wrapper status lines go to stdout, interleaved with claude's own
        # output; the terminal is in raw mode, so newlines need \r\n.
        self._out = out or (
            lambda line: print(line.replace("\n", "\r\n"), flush=True)
        )
        self._master_fd: int | None = None
        self._scan_tail = ""
        self._last_trigger = 0.0
        self._recovery_lock = threading.Lock()
        self._stop = threading.Event()

    # -- user-facing status -------------------------------------------------

    def _status(self, msg: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self._out(f"{dimmed(f'[cswap {stamp}] {msg}')}")

    # -- detection ----------------------------------------------------------

    def feed_output(self, data: bytes) -> None:
        """Scan a raw output chunk; trigger recovery on a limit message."""
        text = strip_ansi(data)
        self._scan_tail = (self._scan_tail + text)[-_SCAN_WINDOW_BYTES:]
        message = find_limit_message(self._scan_tail)
        if message is None:
            return
        now = self.clock()
        if now - self._last_trigger < TRIGGER_DEBOUNCE_S:
            return
        if self._recovery_lock.locked():
            return  # a recovery is already switching/waiting
        self._last_trigger = now
        threading.Thread(target=self._recover, args=(message,), daemon=True).start()

    # -- recovery -----------------------------------------------------------

    def _recover(self, message: str) -> None:
        with self._recovery_lock:
            self._status(
                yellowed(f'"{message}" — looking for an account with quota…')
            )
            if self._try_switch_and_continue():
                return
            while not self._stop.is_set():
                wait_s = self._compute_wait_s()
                if wait_s is None:
                    wait_s = NO_RESET_FALLBACK_S
                    self._status(
                        "all accounts exhausted and no reset time is known; "
                        f"re-checking every {_fmt_countdown(wait_s)}"
                    )
                else:
                    self._status(
                        "all accounts exhausted; first quota returns in "
                        f"{_fmt_countdown(wait_s)} — waiting"
                    )
                self._sleep(wait_s)
                if self._stop.is_set():
                    return
                if self._try_switch_and_continue():
                    return

    def _sleep(self, seconds: float) -> None:
        """Sleep up to ``seconds``, capped so quota granted early (or a
        re-enabled account) is picked up on the recheck cadence."""
        self._stop.wait(min(seconds, MAX_SLEEP_S))

    def _compute_wait_s(self) -> float | None:
        """Seconds until the first account's quota returns, or None."""
        try:
            accounts_info = self.switcher._build_accounts_info()
            entries = self.switcher._collect_usage_entries(accounts_info)
        except Exception as e:  # never die waiting
            self._status(yellowed(f"couldn't read usage data ({e!r}); will retry"))
            return None
        switchable = set(self.switcher.switchable_account_numbers())
        usage = {
            num: entry.decision_value()
            for num, entry in entries.items()
            if num in switchable
        }
        if not usage:
            return None
        now = self.clock()
        ts = earliest_recovery_ts(usage, self.models, now)
        if ts is None:
            return None
        return ts - now + poll_policy.RESET_SLACK_S

    def _try_switch_and_continue(self) -> bool:
        """Switch to the best account with headroom; resume the turn.

        Returns True when the active account changed and ``continue`` was
        (or, with --no-auto-continue, was not) queued at the prompt.
        """
        try:
            result = self.switcher.switch(
                strategy=self.strategy, json_output=True, models=self.models
            )
        except ClaudeSwitchError as e:
            self._status(yellowed(f"switch failed: {e}"))
            return False
        except Exception as e:  # the wrapper must outlive errors
            self._status(yellowed(f"switch error ({e!r}); will retry"))
            return False
        if not result or not result.get("switched"):
            reason = (result or {}).get("reason", "no viable account")
            self._status(f"no switch: {reason}")
            return False
        to = result.get("to") or {}
        self._status(
            accent(f"switched to Account-{to.get('number')} ({to.get('email')})")
        )
        if not self.auto_continue:
            self._status("auto-continue off — type 'continue' yourself to resume")
            return True
        if self._stop.wait(CONTINUE_DELAY_S):
            return True
        self._inject(b"continue\r")
        self._status("resumed (typed 'continue')")
        return True

    def _inject(self, data: bytes) -> None:
        """Type into claude as if the user pressed the keys."""
        if self._master_fd is None:
            return
        try:
            os.write(self._master_fd, data)
        except OSError:
            pass  # claude exited between the switch and the injection

    # -- PTY proxy ----------------------------------------------------------

    def run(self) -> int:
        """Run claude under a PTY; return its exit code."""
        import pty

        argv = [self.claude_bin, *self.claude_args]
        pid, master = pty.fork()
        if pid == 0:
            # Child: the PTY slave is now stdin/stdout/stderr and controlling
            # terminal. Replace the process image with claude.
            try:
                os.execvpe(argv[0], argv, os.environ)
            except OSError as e:
                os.write(2, f"cswap wrap: cannot exec {argv[0]}: {e}\n".encode())
                os._exit(127)

        self._master_fd = master
        self._sync_winsize(master)
        prev_handler = signal.signal(
            signal.SIGWINCH, lambda *_: self._sync_winsize(master)
        )

        try:
            stdin_fd: int | None = sys.stdin.fileno()
        except (OSError, ValueError):
            stdin_fd = None  # redirected/pseudofile stdin (e.g. test harness)
        saved_attrs = None
        if stdin_fd is not None and os.isatty(stdin_fd):
            saved_attrs = termios.tcgetattr(stdin_fd)
            tty.setraw(stdin_fd)

        try:
            return self._proxy_loop(master, stdin_fd, pid)
        finally:
            self._stop.set()
            if saved_attrs is not None:
                termios.tcsetattr(stdin_fd, termios.TCSADRAIN, saved_attrs)
            signal.signal(signal.SIGWINCH, prev_handler)
            try:
                os.close(master)
            except OSError:
                pass
            self._master_fd = None

    def _proxy_loop(self, master: int, stdin_fd: int | None, pid: int) -> int:
        """Shuttle bytes user<->claude until claude exits; scan output."""
        while True:
            readable = [master]
            if stdin_fd is not None:
                readable.append(stdin_fd)
            try:
                ready, _, _ = select.select(readable, [], [], 1.0)
            except InterruptedError:
                ready = []
            for fd in ready:
                if fd == master:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        chunk = b""  # EIO: child closed the PTY
                    if not chunk:
                        _, status = os.waitpid(pid, 0)
                        return os.waitstatus_to_exitcode(status)
                    self.feed_output(chunk)
                    self._write_stdout(chunk)
                else:
                    try:
                        chunk = os.read(stdin_fd, 65536)
                    except OSError:
                        chunk = b""
                    if not chunk:
                        stdin_fd = None  # user side closed (EOF)
                    else:
                        os.write(master, chunk)
            # Reap on timeout too, in case the PTY hung up without EIO.
            done, status = os.waitpid(pid, os.WNOHANG)
            if done == pid:
                # Drain whatever is still buffered in the PTY.
                try:
                    while True:
                        chunk = os.read(master, 65536)
                        if not chunk:
                            break
                        self.feed_output(chunk)
                        self._write_stdout(chunk)
                except OSError:
                    pass
                return os.waitstatus_to_exitcode(status)

    @staticmethod
    def _write_stdout(chunk: bytes) -> None:
        try:
            os.write(sys.stdout.fileno(), chunk)
        except (OSError, ValueError):
            sys.stdout.write(chunk.decode("utf-8", errors="replace"))
            sys.stdout.flush()

    def _sync_winsize(self, master: int) -> None:
        """Propagate the outer terminal's size into the PTY."""
        try:
            size = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
            fcntl.ioctl(master, termios.TIOCSWINSZ, size)
        except OSError:
            pass
