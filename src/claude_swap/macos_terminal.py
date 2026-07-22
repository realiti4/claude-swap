"""Launch a separate macOS Terminal window for a stored cswap session.

This module deliberately only hands ``cswap run <slot>`` to Terminal. Session
bootstrap and account switching remain the CLI's responsibility once Terminal
runs that existing command.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

_OSASCRIPT = "/usr/bin/osascript"
_LAUNCH_TIMEOUT_S = 5.0


class TerminalLaunchErrorCode(str, Enum):
    """Reasons Terminal launch can fail before the session command runs."""

    INVALID_SLOT = "invalid_slot"
    INVALID_EXECUTABLE = "invalid_executable"
    OSASCRIPT_UNAVAILABLE = "osascript_unavailable"
    OSASCRIPT_TIMED_OUT = "osascript_timed_out"
    APPLESCRIPT_FAILED = "applescript_failed"


@dataclass(frozen=True)
class TerminalLaunchResult:
    """Outcome of asking Terminal to start a cswap session command.

    ``launched`` means macOS accepted the AppleScript request. It does not mean
    that the subsequently started ``cswap run`` command completed successfully.
    """

    launched: bool
    slot: str | None
    command: str | None
    error_code: TerminalLaunchErrorCode | None = None
    error_message: str | None = None


def validate_slot(slot: int | str) -> str:
    """Return a canonical positive numeric account slot.

    Slots come from menu selection, but validation remains at this process
    boundary so malformed values cannot alter the shell command Terminal runs.
    """
    if isinstance(slot, bool):
        raise ValueError("slot must be a positive integer")
    if isinstance(slot, int):
        value = str(slot)
    elif isinstance(slot, str):
        value = slot
    else:
        raise ValueError("slot must be a positive integer")

    if not value or any(character not in "0123456789" for character in value):
        raise ValueError("slot must be a positive integer")

    normalized = value.lstrip("0") or "0"
    if normalized == "0":
        raise ValueError("slot must be greater than zero")
    return normalized


def resolve_cswap_executable(cswap_executable: str | Path = "cswap") -> Path:
    """Resolve a runnable cswap command to an absolute executable path.

    Terminal starts a login shell whose ``PATH`` can differ from the menu-bar
    process. Resolving here prevents a successful AppleScript handoff from being
    reported as a successful launch when that shell cannot find ``cswap``.
    """
    if not isinstance(cswap_executable, (str, Path)):
        raise ValueError("cswap executable must be a non-empty path")
    executable = str(cswap_executable)
    if not executable or executable == "." or "\x00" in executable:
        raise ValueError("cswap executable must be a non-empty path")

    expanded = os.path.expanduser(executable)
    if isinstance(cswap_executable, Path):
        candidates = [str(Path(expanded).absolute())]
    elif os.path.isabs(expanded) or os.sep in executable:
        candidates = [expanded]
    else:
        candidates = [executable]
        # A Login Item can run an absolute cswap shim with a stripped PATH.
        # Preserve that known-good executable before refusing an otherwise valid
        # Terminal launch.
        argv0 = os.path.expanduser(sys.argv[0]) if sys.argv else ""
        if argv0 and (os.path.isabs(argv0) or os.sep in argv0):
            candidates.append(argv0)

    resolved = next((path for candidate in candidates if (path := shutil.which(candidate))), None)
    if resolved is None:
        raise ValueError(f"cswap executable was not found or is not executable: {executable}")
    try:
        path = Path(resolved).resolve(strict=True)
    except OSError as error:
        raise ValueError(f"cswap executable could not be resolved: {executable}") from error
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError(f"cswap executable was not found or is not executable: {executable}")
    return path


def build_run_command(slot: int | str, *, cswap_executable: str | Path = "cswap") -> str:
    """Build a shell-safe isolated-session command for Terminal.

    ``cswap_executable`` is trusted application configuration, rather than a
    menu value. It is still shell-quoted because Terminal executes this command
    through its shell; the separately validated slot is quoted as well.
    """
    normalized_slot = validate_slot(slot)
    if not isinstance(cswap_executable, (str, Path)):
        raise ValueError("cswap executable must be a non-empty path")
    executable = str(cswap_executable)
    if not executable or executable == "." or "\x00" in executable:
        raise ValueError("cswap executable must be a non-empty path")
    return f"{shlex.quote(executable)} run {shlex.quote(normalized_slot)}"


def _terminal_script(command: str) -> str:
    """Encode a shell command as an AppleScript string literal."""
    escaped_command = command.replace("\\", "\\\\").replace('"', '\\"')
    return f'tell application "Terminal" to do script "{escaped_command}"'


def launch_terminal(
    slot: int | str, *, cswap_executable: str | Path = "cswap"
) -> TerminalLaunchResult:
    """Ask macOS Terminal to open a new window running ``cswap run <slot>``.

    The function returns a value instead of raising expected launch failures so
    a future menu-bar controller can choose how to display them. No credentials
    are read, written, or included in the command.
    """
    try:
        normalized_slot = validate_slot(slot)
    except ValueError as error:
        return TerminalLaunchResult(
            launched=False,
            slot=None,
            command=None,
            error_code=TerminalLaunchErrorCode.INVALID_SLOT,
            error_message=str(error),
        )

    try:
        executable = resolve_cswap_executable(cswap_executable)
        command = build_run_command(normalized_slot, cswap_executable=executable)
    except ValueError as error:
        return TerminalLaunchResult(
            launched=False,
            slot=normalized_slot,
            command=None,
            error_code=TerminalLaunchErrorCode.INVALID_EXECUTABLE,
            error_message=str(error),
        )

    try:
        completed = subprocess.run(
            [_OSASCRIPT, "-e", _terminal_script(command)],
            capture_output=True,
            text=True,
            check=False,
            timeout=_LAUNCH_TIMEOUT_S,
        )
    except FileNotFoundError as error:
        return TerminalLaunchResult(
            launched=False,
            slot=normalized_slot,
            command=command,
            error_code=TerminalLaunchErrorCode.OSASCRIPT_UNAVAILABLE,
            error_message=str(error),
        )
    except subprocess.TimeoutExpired:
        return TerminalLaunchResult(
            launched=False,
            slot=normalized_slot,
            command=command,
            error_code=TerminalLaunchErrorCode.OSASCRIPT_TIMED_OUT,
            error_message=f"osascript timed out after {_LAUNCH_TIMEOUT_S:g}s",
        )
    except OSError as error:
        return TerminalLaunchResult(
            launched=False,
            slot=normalized_slot,
            command=command,
            error_code=TerminalLaunchErrorCode.OSASCRIPT_UNAVAILABLE,
            error_message=str(error),
        )

    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        message = detail or f"osascript exited with status {completed.returncode}"
        return TerminalLaunchResult(
            launched=False,
            slot=normalized_slot,
            command=command,
            error_code=TerminalLaunchErrorCode.APPLESCRIPT_FAILED,
            error_message=message,
        )

    return TerminalLaunchResult(
        launched=True,
        slot=normalized_slot,
        command=command,
    )
