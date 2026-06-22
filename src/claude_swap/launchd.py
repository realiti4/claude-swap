"""macOS LaunchAgent integration for the claude-swap auto-switch daemon.

All public functions are macOS-only in practice but safe to import on other
platforms — the effectful paths are guarded and subprocess calls are always
done with ``timeout`` + ``check=False`` so nothing ever raises.

Usage::

    from claude_swap.launchd import install_agent, uninstall_agent, agent_status
"""

from __future__ import annotations

import logging
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from claude_swap.models import Platform
from claude_swap.paths import get_backup_root

_logger = logging.getLogger("claude-swap")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LABEL = "com.claude-swap.auto"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _log_path(backup_root: Path | None = None) -> Path:
    root = backup_root if backup_root is not None else get_backup_root()
    return root / "auto-switch.log"


# ---------------------------------------------------------------------------
# Pure helper
# ---------------------------------------------------------------------------


def _plist_dict(
    program_args: list[str],
    log_path: Path,
) -> dict:
    """Build the plist payload as a plain Python dict (pure, no I/O).

    Args:
        program_args: ``ProgramArguments`` list (e.g. ``["/usr/local/bin/cswap",
            "_auto-daemon"]``).
        log_path: Path for ``StandardOutPath`` / ``StandardErrorPath``.

    Returns:
        Dict ready to pass to ``plistlib.dumps``.
    """
    return {
        "Label": LABEL,
        "ProgramArguments": program_args,
        "RunAtLoad": True,
        # Restart only on a crash (non-zero exit). A clean exit — which is
        # what the daemon does when ``enabled`` becomes False — must NOT be
        # respawned, otherwise launchd would relaunch it in a tight loop.
        "KeepAlive": {"SuccessfulExit": False},
        # Guard against rapid crash-respawn: launchd waits at least this many
        # seconds between launches.
        "ThrottleInterval": 30,
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }


def _program_args() -> list[str]:
    """Return the ``ProgramArguments`` to use in the plist.

    Prefers the resolved ``cswap`` binary; falls back to
    ``python -m claude_swap`` (``__main__.py`` exists).
    """
    cswap = shutil.which("cswap")
    if cswap:
        return [cswap, "_auto-daemon"]
    return [sys.executable, "-m", "claude_swap", "_auto-daemon"]


# ---------------------------------------------------------------------------
# effectful helpers (launchctl)
# ---------------------------------------------------------------------------


def _uid() -> str:
    return str(os.getuid()) if hasattr(os, "getuid") else "0"


def _bootout(label: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["launchctl", "bootout", f"gui/{_uid()}/{label}"],
        timeout=10,
        check=False,
        capture_output=True,
        text=True,
    )


def _bootstrap(label: str, plist: Path) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{_uid()}", str(plist)],
        timeout=10,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # Fallback for older macOS that doesn't have bootstrap/bootout
        result = subprocess.run(
            ["launchctl", "load", "-w", str(plist)],
            timeout=10,
            check=False,
            capture_output=True,
            text=True,
        )
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def install_agent(backup_root: Path | None = None) -> str:
    """Write the LaunchAgent plist and load it.

    Idempotent: boots out any existing agent first, then loads the new plist.
    Returns a human-readable status string.

    Off macOS this is a pure no-op: no ``~/Library/LaunchAgents`` directory is
    created and no plist is written — launchd is macOS-only, so writing a plist
    that can never be loaded would only litter the filesystem.
    """
    if Platform.detect() is not Platform.MACOS:
        return "Auto-switch daemon is macOS-only (launchd); plist not written."

    log = _log_path(backup_root)
    args = _program_args()
    payload = _plist_dict(args, log)

    # Ensure the LaunchAgents dir exists.
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)

    with open(PLIST_PATH, "wb") as fh:
        plistlib.dump(payload, fh)

    _logger.info("launchd: wrote plist at %s", PLIST_PATH)

    # Unload silently (not found is OK).
    _bootout(LABEL)

    result = _bootstrap(LABEL, PLIST_PATH)
    if result.returncode == 0:
        _logger.info("launchd: agent loaded")
        return f"Auto-switch agent installed and started (label: {LABEL})."
    else:
        _logger.warning("launchd: load failed: %s", result.stderr)
        return (
            f"Plist written but agent load failed (rc={result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )


def uninstall_agent() -> str:
    """Unload and remove the LaunchAgent.  Idempotent (absent = success).

    Returns a human-readable status string.
    """
    if Platform.detect() is Platform.MACOS:
        _bootout(LABEL)

    if PLIST_PATH.exists():
        try:
            PLIST_PATH.unlink()
            _logger.info("launchd: removed plist at %s", PLIST_PATH)
        except OSError as exc:
            _logger.warning("launchd: could not remove plist: %r", exc)
            return f"Could not remove plist: {exc}"

    return "Auto-switch agent uninstalled."


def agent_status() -> str:
    """Return a human-readable status line for the LaunchAgent.

    Uses ``launchctl print gui/$UID/<label>`` first; falls back to
    ``launchctl list | grep`` on older macOS.
    """
    if not PLIST_PATH.exists():
        return "Not installed (plist absent)."

    if Platform.detect() is not Platform.MACOS:
        return f"Plist present at {PLIST_PATH} (launchctl not available on this platform)."

    result = subprocess.run(
        ["launchctl", "print", f"gui/{_uid()}/{LABEL}"],
        timeout=10,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        # Parse state from the output if possible.
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("state ="):
                state = stripped.split("=", 1)[1].strip()
                return f"Agent loaded. State: {state}."
        return "Agent loaded (state unknown)."

    # Fallback: list
    list_result = subprocess.run(
        ["launchctl", "list"],
        timeout=10,
        check=False,
        capture_output=True,
        text=True,
    )
    if LABEL in list_result.stdout:
        return "Agent is loaded (found in launchctl list)."

    return "Agent not loaded (not found in launchctl list)."
