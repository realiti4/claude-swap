"""Detect running ``codex`` processes.

A Codex switch rewrites ``auth.json``, but a codex session already running has
its tokens in memory — it keeps using the old account until restarted. Silently
switching under a live session is this feature's biggest trap, so a switch that
finds running processes says so and names their PIDs.

Unlike ``process_detection`` on the Claude side, this cannot read session PID
files: Claude Code writes ``~/.claude/sessions/{pid}.json`` and codex writes no
equivalent liveness record (``~/.codex/sessions/`` holds rollout transcripts,
which outlive the process that wrote them). So the process table is the only
honest source.

Matching is on the executable's *name*, never a substring of the whole path: a
directory called ``codex-notes`` would otherwise produce a restart warning on
every switch, and a warning that fires wrongly is one users learn to ignore.
"""

from __future__ import annotations

import logging
import subprocess
import sys

_logger = logging.getLogger(__name__)

#: Executable names that mean "a Codex session is running". ``codext`` is the
#: seamless-switching fork; it is still a session worth reporting.
_CODEX_EXECUTABLES = frozenset({"codex", "codext"})

#: Process listing is advisory — a switch must not stall on it.
_TIMEOUT_S = 5


def _list_processes() -> list[tuple[int, str]]:
    """Return ``(pid, command path)`` for every visible process."""
    if sys.platform == "win32":
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
        rows: list[tuple[int, str]] = []
        for line in out.splitlines():
            parts = [p.strip('"') for p in line.split('","')]
            if len(parts) >= 2 and parts[1].isdigit():
                rows.append((int(parts[1]), parts[0]))
        return rows

    out = subprocess.run(
        ["ps", "-axo", "pid=,comm="],
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_S,
    ).stdout
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        pid, _, comm = line.partition(" ")
        if pid.isdigit():
            rows.append((int(pid), comm.strip()))
    return rows


def _executable_name(command: str) -> str:
    """The bare executable name of a command path, without a ``.exe`` suffix."""
    name = command.replace("\\", "/").rsplit("/", 1)[-1]
    return name[:-4] if name.lower().endswith(".exe") else name


def running_codex_pids() -> list[int]:
    """PIDs of running codex sessions. Never raises."""
    try:
        rows = _list_processes()
    except Exception as e:
        # If the listing fails we lose a warning, not a switch.
        _logger.debug("codex process detection failed: %s", type(e).__name__)
        return []
    return [pid for pid, cmd in rows if _executable_name(cmd) in _CODEX_EXECUTABLES]
