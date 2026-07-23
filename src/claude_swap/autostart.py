"""Login-item management for the macOS menu bar app ("Start at login").

macOS runs per-user background jobs through launchd, so "start at login" is one
LaunchAgent plist in ``~/Library/LaunchAgents``. launchd loads that directory on
its own at every login, which is what lets both operations here stay minimal:

- Enabling only *writes* the plist. It deliberately does not
  ``launchctl bootstrap`` the job: the only ways to reach this are a menu bar
  app that is already running, or a CLI call that shouldn't spawn a GUI, so
  loading the job now would either duplicate the running app or start one
  behind the user's back. The plist takes effect at the next login.
- Disabling only *removes* it, and boots the job out when the caller isn't the
  job itself (booting yourself out is suicide, and the running app should
  survive the toggle). ``KeepAlive`` is scoped to non-zero exits, so a clean
  Quit from the menu is never resurrected.

The plist builder and the path helpers are pure, so the whole shape can be
tested without touching the real LaunchAgents directory or launchd.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from claude_swap.exceptions import ClaudeSwitchError

LAUNCH_AGENT_LABEL = "com.claude-swap.menubar"

# The agent inherits almost nothing from a login shell, so spell out the PATH it
# needs: the tool's own bin dir (uv/pipx console scripts), Homebrew (where the
# `claude` CLI usually lives), and the system defaults for `security`/`open`.
_PATH_ENTRIES = (
    "{home}/.local/bin",
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
)

_LAUNCHCTL_TIMEOUT = 10


class AutostartError(ClaudeSwitchError):
    """Raised when the login item can't be written or removed.

    A ``ClaudeSwitchError`` so the CLI's existing handler renders it as a clean
    error line (and a JSON error envelope under ``--json``) instead of a traceback.
    """


def launch_agent_path(home: Path | None = None) -> Path:
    """Path of the menu bar's LaunchAgent plist."""
    base = home if home is not None else Path.home()
    return base / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


def running_as_login_item() -> bool:
    """True when this process was started by our own LaunchAgent.

    launchd exports the job label as ``XPC_SERVICE_NAME``; a process started
    from a shell has no such value (or a placeholder). Used to avoid booting
    ourselves out when the login item is switched off from inside the app.
    """
    return os.environ.get("XPC_SERVICE_NAME") == LAUNCH_AGENT_LABEL


def menubar_command(executable: str | Path | None = None) -> list[str]:
    """Absolute command that relaunches the menu bar, for ``ProgramArguments``.

    ``sys.executable`` is the interpreter of the install (uv tool, pipx, venv);
    the ``claude-swap`` console script sits beside it. Prefer that script: it
    survives interpreter upgrades within the environment and shows up under its
    own name in Activity Monitor. Fall back to ``-m claude_swap`` for installs
    that expose no console script (e.g. a bare checkout).
    """
    exe = Path(executable) if executable is not None else Path(sys.executable)
    script = exe.parent / "claude-swap"
    if script.exists():
        return [str(script), "menubar"]
    return [str(exe), "-m", "claude_swap", "menubar"]


def build_agent_plist(
    command: list[str],
    *,
    home: Path,
    log_path: Path,
) -> dict:
    """The LaunchAgent definition, as a plist-ready dict.

    ``KeepAlive`` is deliberately restricted to ``SuccessfulExit: False`` —
    restart a crash, but respect the menu's Quit. ``ProcessType: Interactive``
    keeps macOS from throttling a foreground-facing UI process.
    """
    return {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": list(command),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": _LAUNCHCTL_TIMEOUT,
        "ProcessType": "Interactive",
        "WorkingDirectory": str(home),
        "EnvironmentVariables": {
            "PATH": ":".join(entry.format(home=home) for entry in _PATH_ENTRIES),
            "HOME": str(home),
        },
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }


def is_enabled(path: Path | None = None) -> bool:
    """True when the login item is installed."""
    return (path if path is not None else launch_agent_path()).exists()


def installed_command(path: Path | None = None) -> list[str] | None:
    """``ProgramArguments`` of the installed login item, or None if absent/unreadable."""
    target = path if path is not None else launch_agent_path()
    try:
        data = plistlib.loads(target.read_bytes())
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    args = data.get("ProgramArguments") if isinstance(data, dict) else None
    return list(args) if isinstance(args, list) else None


def is_stale(path: Path | None = None, *, executable: str | Path | None = None) -> bool:
    """True when an installed login item points at a different executable.

    A ``uv tool upgrade`` or a reinstall onto another Python can move the
    console script, leaving a plist that launches nothing. Callers refresh the
    plist rather than silently keeping a dead login item.
    """
    target = path if path is not None else launch_agent_path()
    if not target.exists():
        return False
    return installed_command(target) != menubar_command(executable)


def enable(
    *,
    log_path: Path,
    home: Path | None = None,
    path: Path | None = None,
    executable: str | Path | None = None,
) -> Path:
    """Write (or refresh) the login item. Returns the plist path.

    Writes atomically: an interrupted write must not leave launchd a truncated
    plist to choke on at the next login.
    """
    base = home if home is not None else Path.home()
    target = path if path is not None else launch_agent_path(base)
    data = build_agent_plist(menubar_command(executable), home=base, log_path=log_path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_bytes(plistlib.dumps(data))
        os.replace(tmp, target)
    except OSError as exc:
        raise AutostartError(f"Couldn't write the login item ({target}): {exc}") from exc
    return target


def disable(path: Path | None = None, *, uid: int | None = None) -> bool:
    """Remove the login item. Returns False when it wasn't installed.

    Also boots out an already-loaded job so the setting takes effect without a
    logout — unless *this* process is that job, in which case booting out would
    kill the app the user is still using.
    """
    target = path if path is not None else launch_agent_path()
    existed = target.exists()
    try:
        target.unlink(missing_ok=True)
    except OSError as exc:
        raise AutostartError(f"Couldn't remove the login item ({target}): {exc}") from exc
    if existed and not running_as_login_item():
        _bootout(uid if uid is not None else os.getuid())
    return existed


def _bootout(uid: int) -> None:
    """Unload the job from the current GUI session, ignoring "not loaded"."""
    try:
        subprocess.run(
            ["/bin/launchctl", "bootout", f"gui/{uid}/{LAUNCH_AGENT_LABEL}"],
            capture_output=True,
            timeout=_LAUNCHCTL_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        # Best-effort: the plist is already gone, so the next login is correct
        # either way. Not worth failing the toggle over.
        pass
