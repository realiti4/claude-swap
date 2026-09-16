"""Run ``cswap menubar`` as a launchd LaunchAgent instead of a foreground process.

``cswap menubar`` blocks the terminal that started it, so the status item dies
with that terminal — and never comes back after a logout or reboot. launchd is
the native macOS answer: a per-user LaunchAgent starts the menu bar at login,
restarts it if it crashes, and needs no ``.app`` bundle.

Two decisions here are worth stating, because both differ from the obvious
approach:

*The plist pins the console script, not ``sys.executable``.* A LaunchAgent
outlives upgrades, and the two paths age differently: ``uv tool upgrade`` (and
``cswap upgrade``) rebuilds the tool's virtualenv — ``sys.executable`` points
inside that virtualenv and can be replaced — while the console script keeps its
path across upgrades. Pinning the script means an upgraded cswap needs a
``launchctl kickstart``, not a reinstalled service. ``sys.executable -m
claude_swap`` stays as the fallback for installs that expose no console script.

*Logs go to ``~/Library/Logs``, not ``/tmp``.* ``/tmp`` is world-writable and
periodically purged, so a crash log can vanish before anyone reads it, and a
predictable world-writable path is a poor place to point a long-lived writer.

Everything here is macOS-only; callers guard on ``sys.platform`` and the public
functions refuse rather than half-work elsewhere.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

from claude_swap.exceptions import ClaudeSwitchError

LABEL = "com.cswap.menubar"

# launchd's default PATH is /usr/bin:/bin:/usr/sbin:/sbin, which covers
# `security` (Keychain reads) but not a Homebrew or ~/.local/bin `claude`. The
# menu bar shells out to detect running sessions, so seed a PATH that finds it.
_EXTRA_PATH_DIRS = ("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin")
_BASE_PATH_DIRS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")

# `launchctl bootout` can return before launchd has finished tearing the
# job down, and a `bootstrap` inside that window fails with "Operation
# already in progress". Poll until the job is really gone instead of
# assuming bootout was synchronous (Homebrew's services code does the same).
_UNLOAD_TIMEOUT_SECONDS = 5.0
_UNLOAD_POLL_SECONDS = 0.1


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise ClaudeSwitchError(
            "The menu bar service is only available on macOS."
        )


def plist_path(label: str = LABEL, home: Path | None = None) -> Path:
    """Absolute path of the LaunchAgent plist for ``label``."""
    return (home or Path.home()) / "Library" / "LaunchAgents" / f"{label}.plist"


def log_paths(label: str = LABEL, home: Path | None = None) -> tuple[Path, Path]:
    """``(stdout, stderr)`` log destinations for ``label``."""
    logs = (home or Path.home()) / "Library" / "Logs"
    return logs / f"{label}.log", logs / f"{label}.err"


def service_target(label: str = LABEL, uid: int | None = None) -> str:
    """launchd service target, e.g. ``gui/501/com.cswap.menubar``."""
    return f"gui/{os.getuid() if uid is None else uid}/{label}"


def domain_target(uid: int | None = None) -> str:
    """launchd domain target, e.g. ``gui/501``."""
    return f"gui/{os.getuid() if uid is None else uid}"


def resolve_program() -> list[str]:
    """Argv prefix that launchd should run, minus the subcommand.

    Prefers the installed console script (stable across upgrades, see the
    module docstring) and falls back to running the package through the
    interpreter that is executing right now.

    The path is made absolute but deliberately NOT resolved: a `uv tool
    install` puts a symlink at ``~/.local/bin/cswap`` pointing into the tool's
    virtualenv, and resolving it would write that virtualenv-internal path
    into the plist — the very path this module avoids pinning, since a
    reinstall recreates the virtualenv while the symlink keeps its name.
    """
    candidate = sys.argv[0] if sys.argv and sys.argv[0] else None
    if candidate is not None:
        absolute = Path(os.path.abspath(candidate))
        if absolute.name == "cswap" and absolute.is_file():
            return [str(absolute)]

    which = shutil.which("cswap")
    if which:
        return [str(Path(os.path.abspath(which)))]

    return [sys.executable, "-m", "claude_swap"]


def _path_env(program: list[str]) -> str:
    """PATH for the agent, with the program's own directory first."""
    dirs: list[str] = []
    first = Path(program[0]).parent
    if str(first) not in ("", "."):
        dirs.append(str(first))
    for extra in (*_EXTRA_PATH_DIRS, *_BASE_PATH_DIRS):
        expanded = os.path.expanduser(extra)
        if expanded not in dirs:
            dirs.append(expanded)
    return ":".join(dirs)


def build_plist(
    program: list[str] | None = None,
    label: str = LABEL,
    home: Path | None = None,
) -> bytes:
    """Serialize the LaunchAgent plist.

    Built with :mod:`plistlib` rather than a formatted XML string so paths
    containing ``&`` or ``<`` cannot produce a plist launchd refuses to parse.
    """
    program = program or resolve_program()
    out_log, err_log = log_paths(label, home)
    return plistlib.dumps(
        {
            "Label": label,
            "ProgramArguments": [*program, "menubar"],
            "RunAtLoad": True,
            # Restart a crash, but respect a deliberate Quit. The menu bar's
            # quit handler calls rumps.quit_application(), a clean exit(0);
            # under a bare `KeepAlive: True` launchd would relaunch it at once
            # and the Quit item would do nothing the user can see.
            "KeepAlive": {"SuccessfulExit": False},
            # A menu bar owner is a UI process; Background would have launchd
            # apply throttled I/O and CPU bands to it.
            "ProcessType": "Interactive",
            "EnvironmentVariables": {"PATH": _path_env(program)},
            "StandardOutPath": str(out_log),
            "StandardErrorPath": str(err_log),
        }
    )


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["launchctl", *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as e:  # pragma: no cover - launchctl is in the base OS
        raise ClaudeSwitchError("launchctl not found; is this macOS?") from e


def _wait_until_unloaded(
    label: str = LABEL,
    uid: int | None = None,
    timeout: float = _UNLOAD_TIMEOUT_SECONDS,
) -> bool:
    """Block until launchd has dropped the job. True if it went away in time."""
    deadline = time.monotonic() + timeout
    while is_loaded(label, uid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(_UNLOAD_POLL_SECONDS)
    return True


def is_loaded(label: str = LABEL, uid: int | None = None) -> bool:
    """Whether launchd currently knows about the service."""
    return _launchctl("print", service_target(label, uid)).returncode == 0


def status(label: str = LABEL, uid: int | None = None, home: Path | None = None) -> dict:
    """Installed / loaded / running state, plus the pid when there is one."""
    _require_macos()
    printed = _launchctl("print", service_target(label, uid))
    loaded = printed.returncode == 0
    state: str | None = None
    pid: int | None = None
    if loaded:
        for line in printed.stdout.splitlines():
            # `launchctl print` nests sub-dictionaries — pid-local endpoints,
            # inherited environment — and those repeat keys the job itself
            # uses, `state` among them. The job's own fields are the ones at a
            # single tab, so anything deeper is a different object's field.
            if not line.startswith("\t") or line.startswith("\t\t"):
                continue
            stripped = line.strip()
            if state is None and stripped.startswith("state = "):
                state = stripped.removeprefix("state = ").strip()
            elif pid is None and stripped.startswith("pid = "):
                raw = stripped.removeprefix("pid = ").strip()
                if raw.isdigit():
                    pid = int(raw)
    return {
        "label": label,
        "installed": plist_path(label, home).exists(),
        "loaded": loaded,
        "state": state,
        "pid": pid,
        "plist": str(plist_path(label, home)),
    }


def install(
    label: str = LABEL,
    home: Path | None = None,
    program: list[str] | None = None,
    uid: int | None = None,
) -> dict:
    """Write the plist and hand the service to launchd.

    Idempotent: an already-loaded service is booted out first, so running this
    after an upgrade re-reads the plist instead of failing with launchd's
    "service already loaded" (EEXIST, code 5).
    """
    _require_macos()
    program = program or resolve_program()
    target_plist = plist_path(label, home)
    out_log, err_log = log_paths(label, home)

    target_plist.parent.mkdir(parents=True, exist_ok=True)
    out_log.parent.mkdir(parents=True, exist_ok=True)
    target_plist.write_bytes(build_plist(program, label, home))

    settled = True
    if is_loaded(label, uid):
        _launchctl("bootout", service_target(label, uid))
        settled = _wait_until_unloaded(label, uid)

    booted = _launchctl("bootstrap", domain_target(uid), str(target_plist))
    if booted.returncode != 0:
        detail = (booted.stderr or booted.stdout or "").strip()
        if not settled:
            detail = f"{detail}; the previous instance was still shutting down".lstrip("; ")
        raise ClaudeSwitchError(
            f"launchctl bootstrap failed (exit {booted.returncode})"
            + (f": {detail}" if detail else "")
        )

    return {
        "label": label,
        "plist": str(target_plist),
        "program": [*program, "menubar"],
        "stdout_log": str(out_log),
        "stderr_log": str(err_log),
    }


def uninstall(
    label: str = LABEL,
    home: Path | None = None,
    uid: int | None = None,
) -> dict:
    """Stop the service and delete its plist.

    Tolerates every partial state — loaded without a plist, a plist that was
    never bootstrapped, neither — because the point of an uninstall is to
    arrive at "gone", not to insist on the path taken to get there.
    """
    _require_macos()
    target_plist = plist_path(label, home)
    was_loaded = is_loaded(label, uid)
    if was_loaded:
        booted_out = _launchctl("bootout", service_target(label, uid))
        if booted_out.returncode != 0 and is_loaded(label, uid):
            detail = (booted_out.stderr or booted_out.stdout or "").strip()
            raise ClaudeSwitchError(
                f"launchctl bootout failed (exit {booted_out.returncode})"
                + (f": {detail}" if detail else "")
            )

    existed = target_plist.exists()
    if existed:
        target_plist.unlink()

    return {"label": label, "was_loaded": was_loaded, "removed_plist": existed}
