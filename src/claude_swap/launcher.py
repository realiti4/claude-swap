"""Launch one Claude desktop app per cswap account, all at once (macOS only).

Ports ``~/.local/bin/claude-app``: the Claude desktop app is Electron and
takes Chromium's ``--user-data-dir``, and it does NOT call
``requestSingleInstanceLock``. Giving each account its own profile directory
under ``~/Library/Application Support`` therefore lets several signed-in
Claude apps run side by side. Account identity is resolved through cswap's
own switcher (the same path ``cswap run <num|email>`` uses) so this never
drifts from ``cswap list`` and aliases keep working.

This module has no platform guard of its own — its logic is plain path/
subprocess plumbing that is fully testable on any OS via mocked
``subprocess.run``. The "macOS only" refusal lives in the CLI dispatcher
(``cli.py``), which is the one place that should ever exit the process.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from claude_swap.exceptions import LauncherError

APP_PATH = Path("/Applications/Claude.app")
_APP_BINARY_MARKER = "Claude.app/Contents/MacOS/Claude"

# Copied into a new profile so a fresh instance is not bare. Cookies,
# config.json and ant-device-registry.json are deliberately NOT copied here —
# each profile signs in on its own, which is the whole point.
SEED_NAMES = (
    "claude_desktop_config.json",
    "Claude Extensions",
    "Claude Extensions Settings",
)


def support_dir() -> Path:
    """``~/Library/Application Support``."""
    return Path(os.path.expanduser("~")) / "Library" / "Application Support"


def default_profile_dir() -> Path:
    """The plain (un-swapped) Claude.app profile — seed source for new profiles."""
    return support_dir() / "Claude"


def profile_dir(account_num: str) -> Path:
    """Per-account profile directory, e.g. ``Claude-a2`` for slot 2."""
    return support_dir() / f"Claude-a{account_num}"


@dataclass
class LaunchOutcome:
    """Result of launching (or focusing) one account's desktop app."""

    account_num: str
    email: str
    focused: bool  # True: was already running, we focused it instead of relaunching
    fresh: bool  # True: this was a brand-new profile (not signed in yet)
    pid: int | None


@dataclass
class StopOutcome:
    """Result of stopping one account's desktop app."""

    account_num: str
    email: str
    stopped: bool
    pid: int | None


def running_pid(profile_path: Path) -> int | None:
    """PID of the Claude main process using this profile, or None.

    Scans ``ps`` for the app's main binary (excluding Electron helper
    processes, which carry ``--type=``) with a matching
    ``--user-data-dir=<profile_path>`` argument.
    """
    result = subprocess.run(
        ["ps", "-axo", "pid=,command="],
        capture_output=True,
        text=True,
        check=False,
    )
    needle = f"--user-data-dir={profile_path}"
    for line in result.stdout.splitlines():
        if _APP_BINARY_MARKER not in line or "--type=" in line:
            continue
        if needle in line:
            return int(line.split()[0])
    return None


def focus(pid: int) -> None:
    """Bring the given process's window frontmost via System Events."""
    subprocess.run(
        [
            "osascript",
            "-e",
            'tell application "System Events" to set frontmost of '
            f'(first process whose unix id is {pid}) to true',
        ],
        capture_output=True,
        check=False,
    )


def seed_profile(path: Path, account_uuid: str) -> None:
    """Create the profile directory and copy across non-secret parts.

    Idempotent: an item already present in ``path`` is left untouched, so
    calling this on an existing, already-signed-in profile is a no-op.
    """
    path.mkdir(parents=True, exist_ok=True)
    default = default_profile_dir()
    for name in SEED_NAMES:
        src = default / name
        dst = path / name
        if src.exists() and not dst.exists():
            subprocess.run(["cp", "-R", str(src), str(dst)], check=True)

    if account_uuid:
        src = default / "claude-code-sessions" / account_uuid
        dst = path / "claude-code-sessions" / account_uuid
        if src.is_dir() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["cp", "-R", str(src), str(dst)], check=True)


def _require_app_installed() -> None:
    if not APP_PATH.exists():
        raise LauncherError(
            f"Claude.app not found at {APP_PATH} — install it from "
            "https://claude.ai/download before running `cswap launch`."
        )


def launch_account(switcher, identifier: str) -> LaunchOutcome:
    """Launch (or focus) the desktop app for one account.

    ``identifier`` is resolved through ``switcher.resolve_account`` — the
    same resolver ``cswap run`` uses — so a slot number, email, or alias all
    work identically here.
    """
    account_num, email, _org_uuid = switcher.resolve_account(identifier)
    path = profile_dir(account_num)

    pid = running_pid(path)
    if pid is not None:
        focus(pid)
        return LaunchOutcome(account_num, email, focused=True, fresh=False, pid=pid)

    _require_app_installed()

    fresh = not path.exists()
    identity = switcher.account_identity(account_num)
    seed_profile(path, identity["uuid"])
    subprocess.run(
        ["open", "-na", str(APP_PATH), "--args", f"--user-data-dir={path}"],
        check=True,
    )
    return LaunchOutcome(account_num, email, focused=False, fresh=fresh, pid=None)


def stop_account(switcher, identifier: str) -> StopOutcome:
    """Quit the desktop app for one account, if running."""
    account_num, email, _org_uuid = switcher.resolve_account(identifier)
    path = profile_dir(account_num)
    pid = running_pid(path)
    if pid is None:
        return StopOutcome(account_num, email, stopped=False, pid=None)
    subprocess.run(["kill", str(pid)], check=False)
    return StopOutcome(account_num, email, stopped=True, pid=pid)


def _account_numbers(switcher) -> list[str]:
    """All managed slot numbers, in ascending order."""
    data = switcher._get_sequence_data() or {}
    return sorted(data.get("accounts", {}), key=int)


def launch_all(switcher) -> list[LaunchOutcome]:
    """Launch (or focus) every managed account."""
    return [launch_account(switcher, num) for num in _account_numbers(switcher)]


def stop_all(switcher) -> list[StopOutcome]:
    """Quit every managed account's desktop app that is running."""
    return [stop_account(switcher, num) for num in _account_numbers(switcher)]
