"""Install the macOS menu bar as a per-user LaunchAgent."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from claude_swap.exceptions import ClaudeSwitchError

LABEL = "com.claude-swap.menubar"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _service() -> str:
    return f"{_domain()}/{LABEL}"


def _launchctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["/bin/launchctl", *args],
            check=check,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise ClaudeSwitchError(f"launchctl failed: {detail.strip()}") from exc


def install() -> Path:
    """Write and immediately load the per-user LaunchAgent."""
    if sys.platform != "darwin":
        raise ClaudeSwitchError("Menu bar startup is only available on macOS.")

    path = plist_path()
    log_dir = Path.home() / "Library" / "Logs" / "ClaudeSwap"
    path.parent.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    # Do not resolve the virtualenv symlink: its base interpreter cannot see
    # packages installed in the uv/pipx environment when launchd starts it.
    python = str(Path(sys.executable).absolute())
    payload = {
        "Label": LABEL,
        "ProgramArguments": [python, "-m", "claude_swap", "--menubar"],
        "RunAtLoad": True,
        "ProcessType": "Interactive",
        "StandardOutPath": str(log_dir / "menubar.log"),
        "StandardErrorPath": str(log_dir / "menubar-error.log"),
    }
    temporary = path.with_suffix(".plist.tmp")
    temporary.write_bytes(plistlib.dumps(payload, sort_keys=True))
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)

    _launchctl("bootout", _service(), check=False)
    _launchctl("bootstrap", _domain(), str(path))
    return path


def uninstall() -> None:
    """Stop and remove the LaunchAgent, if present."""
    if sys.platform != "darwin":
        raise ClaudeSwitchError("Menu bar startup is only available on macOS.")
    _launchctl("bootout", _service(), check=False)
    plist_path().unlink(missing_ok=True)


def is_installed() -> bool:
    """Return whether launchd currently knows the installed service."""
    if sys.platform != "darwin" or not plist_path().exists():
        return False
    return _launchctl("print", _service(), check=False).returncode == 0
