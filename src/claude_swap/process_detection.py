"""Detect running Claude Code instances.

Reads session PID files (~/.claude/sessions/{pid}.json) and IDE lockfiles
(~/.claude/ide/{port}.lock) to determine which Claude Code instances are
currently running. Uses the same mechanism Claude Code itself uses internally.
"""

from __future__ import annotations

import calendar
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from claude_swap.paths import get_claude_config_home

logger = logging.getLogger(__name__)

# A session record names a pid, and the OS recycles pids: once the claude
# that wrote the record is gone, the number can belong to anything. The
# record also carries claude's ``ps -o lstart`` reading of its own start, so
# a process that started later than that is not the recorded one. The slack
# absorbs the small wall-clock steps that, on Linux, move every ps start time.
PID_REUSE_SLACK_S = 120

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


@dataclass
class ClaudeSession:
    """A running Claude Code session from ~/.claude/sessions/{pid}.json."""

    pid: int
    session_id: str
    cwd: str
    started_at: int  # epoch milliseconds
    kind: str  # "interactive", "bg", "daemon", "daemon-worker"
    entrypoint: str  # "cli", "claude-vscode", "claude-desktop", "sdk-cli", "mcp"
    status: str | None = None  # "busy", "idle", "waiting"


@dataclass
class IdeInstance:
    """A running IDE instance from ~/.claude/ide/{port}.lock."""

    port: int  # from filename
    pid: int
    ide_name: str  # "Visual Studio Code", "Cursor", "Windsurf"
    workspace_folders: list[str] = field(default_factory=list)


def get_claude_dir() -> Path:
    """Return the Claude config directory, respecting CLAUDE_CONFIG_DIR."""
    return get_claude_config_home()


def is_pid_alive(pid: int) -> bool:
    """Check whether a process with the given PID is running.

    Cross-platform:
    - macOS/Linux/WSL: os.kill(pid, 0)
    - Windows: ctypes OpenProcess
    """
    if pid <= 1:
        return False

    if sys.platform == "win32":
        return _is_pid_alive_windows(pid)

    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        # EPERM means the process exists but we lack permission
        return True
    except OSError:
        return False


def _is_pid_alive_windows(pid: int) -> bool:
    """Windows-specific PID liveness check using ctypes."""
    try:
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    except Exception:
        return False


def _ps(pid: int, *columns: str) -> str | None:
    """``ps -o`` ``columns`` for ``pid``, or None when unknowable.

    POSIX only, under ``LC_ALL=C TZ=UTC`` like claude's own reading. Windows
    and every failure answer None: not knowing must never be read as "not
    the recorded process".
    """
    if sys.platform == "win32":
        return None
    try:
        proc = subprocess.run(
            ["ps", "-o", ",".join(f"{c}=" for c in columns), "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = proc.stdout.strip()
    if proc.returncode != 0 or not text:
        return None
    return text


def process_started_at(pid: int) -> int | None:
    """Epoch seconds at which ``pid`` started, or None when unknowable.

    Read the way claude stamps ``procStart`` into its record, ``ps -o
    lstart=`` under ``LC_ALL=C TZ=UTC``, so the two agree to the second for
    the same process.
    """
    text = _ps(pid, "lstart")
    if text is None:
        return None
    try:
        return _lstart_seconds(text)
    except ValueError:
        return None


def _lstart_seconds(text: str) -> int:
    """``Wed Sep  2 20:35:59 2026``, the ``ps -o lstart`` format under
    ``LC_ALL=C TZ=UTC``, as epoch seconds. Parsed by hand because ``strptime``
    reads month names in the process locale."""
    parts = text.split()
    if len(parts) != 5 or parts[1] not in _MONTHS:
        raise ValueError(text)
    _, month, day, clock, year = parts
    hours, minutes, seconds = (int(p) for p in clock.split(":"))
    return calendar.timegm(
        (int(year), _MONTHS.index(month) + 1, int(day), hours, minutes, seconds, 0, 0, 0)
    )


def process_is_claude(pid: int) -> bool | None:
    """Does the process at ``pid`` look like a claude, or None when unknowable.

    Judged from ``ps -o comm=,args=``: the native binary and the symlink to
    it are named ``claude``, and an npm install runs ``cli.js`` out of a
    ``claude-code`` package directory.
    """
    text = _ps(pid, "comm", "args")
    if text is None:
        return None
    return "claude" in text.lower()


def pid_matches_record(pid: int, proc_start: str | None) -> bool:
    """Is the live process at ``pid`` the one that wrote a record stamped
    ``proc_start``, claude's ``ps -o lstart`` reading of itself?

    A recycled pid belongs to a process that started after the recorded
    claude did. Only that direction disqualifies, and only a stranger: on
    Linux ``ps`` builds every start time from the boot time in
    ``/proc/stat``, which the kernel moves with each wall-clock step (a WSL2
    resume re-syncing the clock steps it by the whole sleep), so a live
    session can read as younger than its own record. A claude at the pid is
    kept either way; a pid genuinely recycled by another claude lingers only
    for that process's lifetime, which is what happened before this check.
    Everything unknowable (Windows, ``ps`` unavailable, an unstamped or
    unparseable record) passes, because "cannot tell" must never turn a live
    session into "nobody there".
    """
    if not proc_start:
        return True
    try:
        recorded = _lstart_seconds(proc_start)
    except ValueError:
        return True
    started = process_started_at(pid)
    if started is None or started <= recorded + PID_REUSE_SLACK_S:
        return True
    return process_is_claude(pid) is not False


def scan_sessions(claude_dir: Path | None = None) -> tuple[list[ClaudeSession], int]:
    """Live sessions, and how many records could NOT be read.

    A record counts as live only when its pid is alive AND still belongs to
    the claude that wrote it (see ``pid_matches_record``): a crashed claude
    leaves its record behind, and the OS can hand the number to something
    else.

    Two kinds of caller read this directory and they need opposite things from
    an unparseable record:

    - A SCAN (a listing, a status display) wants it skipped. One bad file must
      not take out the whole listing.
    - A GUARD wants to know. ``0 live`` and ``0 readable`` are the same list,
      and only the first is safe to act on -- the callers gate ``_bootstrap``
      (which deletes a profile's Keychain entry and overwrites
      ``.credentials.json``) and account removal, so reading "could not tell"
      as "nobody there" runs them underneath a live instance.

    So the count is returned rather than swallowed, and ``list_sessions``
    below is the scan-shaped view that drops it.
    """
    sessions_dir = (claude_dir or get_claude_dir()) / "sessions"
    if not sessions_dir.is_dir():
        return [], 0

    sessions = []
    unreadable = 0
    for path in sessions_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            pid = data["pid"]
            if not is_pid_alive(pid):
                continue
            if not pid_matches_record(pid, data.get("procStart")):
                logger.debug(
                    "Skipping session file %s: pid %s was recycled", path, pid
                )
                continue
            sessions.append(ClaudeSession(
                pid=pid,
                session_id=data.get("sessionId", ""),
                cwd=data.get("cwd", ""),
                started_at=data.get("startedAt", 0),
                kind=data.get("kind", ""),
                entrypoint=data.get("entrypoint", ""),
                status=data.get("status"),
            ))
        except (
            json.JSONDecodeError,   # malformed JSON
            KeyError,               # required field missing
            TypeError,              # field has the wrong type (e.g. pid not an int)
            AttributeError,         # valid JSON that is not an object: a
                                    # top-level array reaches `.get` as a list.
                                    # Also how a too-deep nesting lands where
                                    # the parser's recursion limit is high
                                    # enough not to raise -- it differs per
                                    # machine, so BOTH outcomes must be inert.
            ValueError,             # includes UnicodeDecodeError from read_text
            OverflowError,          # pid too large for os.kill's C long (is_pid_alive)
            RecursionError,         # pathologically nested JSON in json.loads
            OSError,
        ) as exc:
            unreadable += 1
            logger.debug("Skipping session file %s: %s", path, exc)
    return sessions, unreadable


def list_sessions(claude_dir: Path | None = None) -> list[ClaudeSession]:
    """Live sessions. A record that cannot be read is SKIPPED.

    SCAN USE ONLY. The returned list cannot distinguish "no live sessions"
    from "no readable records", so anything gating a destructive step must
    call :func:`scan_sessions` and treat a non-zero count as live.
    """
    return scan_sessions(claude_dir)[0]


def list_ide_instances(claude_dir: Path | None = None) -> list[IdeInstance]:
    """Read IDE lockfiles and return only those with alive processes."""
    ide_dir = (claude_dir or get_claude_dir()) / "ide"
    if not ide_dir.is_dir():
        return []

    instances = []
    for path in ide_dir.glob("*.lock"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            pid = data.get("pid")
            if pid is None or not is_pid_alive(pid):
                continue
            port = int(path.stem)
            instances.append(IdeInstance(
                port=port,
                pid=pid,
                ide_name=data.get("ideName", "Unknown IDE"),
                workspace_folders=data.get("workspaceFolders", []),
            ))
        except (
            json.JSONDecodeError,   # malformed JSON
            KeyError,               # required field missing
            TypeError,              # field has the wrong type (e.g. pid not an int)
            AttributeError,         # valid JSON that is not an object: a
                                    # top-level array reaches `.get` as a list.
                                    # Also how a too-deep nesting lands where
                                    # the parser's recursion limit is high
                                    # enough not to raise -- it differs per
                                    # machine, so BOTH outcomes must be inert.
            ValueError,             # includes UnicodeDecodeError from read_text
            OverflowError,          # pid too large for os.kill's C long (is_pid_alive)
            RecursionError,         # pathologically nested JSON in json.loads
            OSError,
        ) as exc:
            logger.debug("Skipping IDE lockfile %s: %s", path, exc)
    return instances


def get_running_instances(
    claude_dir: Path | None = None,
) -> tuple[list[ClaudeSession], list[IdeInstance]]:
    """Return all running Claude Code sessions and IDE instances."""
    resolved = claude_dir or get_claude_dir()
    return list_sessions(resolved), list_ide_instances(resolved)
