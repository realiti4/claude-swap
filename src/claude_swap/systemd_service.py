"""Run ``cswap auto`` as a systemd user service instead of a foreground process.

``cswap auto`` is a polling loop that blocks the terminal that started it, so
on Linux the auto-switcher dies with that terminal and never comes back after a
logout or reboot. On macOS the menu bar already runs the same engine and has its
own LaunchAgent (``claude_swap.launch_agent``); this module is the Linux
counterpart for the headless loop: a per-user unit that starts with the user's
systemd instance and restarts the loop if it crashes.

The unit runs plain ``cswap auto`` with no flags, so its behaviour comes from
the ``autoswitch.*`` settings (``cswap config set ...``) like any other run.
Settings are read when the loop starts; ``install()`` restarts a running
service, so re-running it after a config change or an upgrade applies both.

Decisions shared with the LaunchAgent, for the same reasons:

*The unit pins the console script, not ``sys.executable``* (see
:func:`claude_swap.launch_agent.resolve_program`): the script keeps its path
across ``uv tool upgrade`` while the virtualenv behind it is rebuilt.

*The unit file is written atomically at 0600*: forwarded proxy variables can
carry credentials, and a write cut short must not leave a truncated unit.

Logs go to the journal (``journalctl --user -u cswap-auto``) rather than to a
file, which is the native place for a user service's output.

A user service stops when the user's last session ends unless lingering is
enabled (``loginctl enable-linger``); :func:`status` reports it so the CLI can
say so instead of leaving the user to discover it after a logout.

Everything here is Linux-only; the public functions refuse elsewhere rather
than half-work.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.fsutil import atomic_write_bytes
from claude_swap.launch_agent import resolve_program

UNIT = "cswap-auto.service"

# A user unit inherits the user manager's environment, not the installing
# shell's. Seed a PATH that finds `claude` (process detection shells out to it)
# and forward only the network variables a user behind a proxy needs, the same
# set the LaunchAgent forwards. An empty value is kept: urllib reads an empty
# lowercase name as cancelling its uppercase twin.
_EXTRA_PATH_DIRS = ("~/.local/bin", "/usr/local/bin")
_BASE_PATH_DIRS = ("/usr/bin", "/bin")
_FORWARDED_ENV_KEYS = (
    "https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY",
    "no_proxy", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR",
)


def _require_linux() -> None:
    if not sys.platform.startswith("linux"):
        raise ClaudeSwitchError(
            "The auto-switch service is only available on Linux (systemd). "
            "On macOS the menu bar runs auto-switching: cswap menubar --install-service"
        )
    if shutil.which("systemctl") is None:
        raise ClaudeSwitchError(
            "systemctl not found; the auto-switch service needs systemd. "
            "Run 'cswap auto' in a terminal or 'cswap auto --once' from cron instead."
        )


def unit_path(home: Path | None = None, env: Mapping[str, str] | None = None) -> Path:
    """Absolute path of the user unit file."""
    env = os.environ if env is None else env
    xdg = env.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg and home is None else (home or Path.home()) / ".config"
    return base / "systemd" / "user" / UNIT


def _quote(arg: str) -> str:
    """Quote one ExecStart/Environment word the way systemd parses it.

    systemd splits on whitespace, honours double quotes with backslash
    escapes, and expands ``%`` specifiers and ``$`` variables; a path like
    ``/home/a b/.local/bin/cswap`` or one containing ``%`` must survive that.
    """
    escaped = arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    if escaped != arg or any(c.isspace() for c in arg) or not arg:
        return f'"{escaped}"'
    return arg


def _path_env(program: list[str]) -> str:
    """PATH for the service, with the program's own directory first."""
    dirs: list[str] = []
    first = Path(program[0]).parent
    if str(first) not in ("", "."):
        dirs.append(str(first))
    for extra in (*_EXTRA_PATH_DIRS, *_BASE_PATH_DIRS):
        expanded = os.path.expanduser(extra)
        if expanded not in dirs:
            dirs.append(expanded)
    return ":".join(dirs)


def build_unit(program: list[str] | None = None, env: Mapping[str, str] | None = None) -> str:
    """Render the unit file."""
    program = program or resolve_program()
    env = os.environ if env is None else env
    variables = {"PATH": _path_env(program)}
    variables.update({k: env[k] for k in _FORWARDED_ENV_KEYS if k in env})
    environment = "\n".join(f"Environment={_quote(f'{k}={v}')}" for k, v in variables.items())
    exec_start = " ".join(_quote(a) for a in [*program, "auto"])
    return (
        "[Unit]\n"
        "Description=claude-swap auto-switch (cswap auto)\n"
        "Documentation=https://github.com/realiti4/claude-swap\n"
        "Wants=network-online.target\n"
        "After=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={exec_start}\n"
        f"{environment}\n"
        # `cswap auto` exits cleanly on SIGTERM (systemctl stop); restart only
        # a crash, and back off so a persistent failure does not spin.
        "Restart=on-failure\n"
        "RestartSec=30\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as e:  # pragma: no cover - guarded by _require_linux
        raise ClaudeSwitchError("systemctl not found; is systemd running?") from e


def _check(result: subprocess.CompletedProcess, what: str) -> None:
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise ClaudeSwitchError(
            f"systemctl --user {what} failed (exit {result.returncode})"
            + (f": {detail}" if detail else "")
        )


def _lingering(user: str | None = None) -> bool | None:
    """Whether the user manager outlives the last session (None if unknown)."""
    user = user or os.environ.get("USER") or ""
    if not user or shutil.which("loginctl") is None:
        return None
    try:
        out = subprocess.run(
            ["loginctl", "show-user", user, "--property=Linger", "--value"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    value = out.stdout.strip().lower()
    return {"yes": True, "no": False}.get(value)


def status(home: Path | None = None) -> dict:
    """Installed / enabled / active state, plus the pid when there is one."""
    _require_linux()
    shown = _systemctl(
        "show", UNIT, "--property=LoadState,ActiveState,SubState,UnitFileState,MainPID"
    )
    props: dict[str, str] = {}
    for line in shown.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            props[key] = value
    pid = props.get("MainPID", "0")
    path = unit_path(home)
    return {
        "unit": UNIT,
        "installed": path.exists(),
        "loaded": props.get("LoadState") == "loaded",
        "enabled": props.get("UnitFileState") == "enabled",
        "active": props.get("ActiveState") == "active",
        "state": props.get("SubState") or None,
        "pid": int(pid) if pid.isdigit() and pid != "0" else None,
        "unit_path": str(path),
        "linger": _lingering(),
    }


def install(home: Path | None = None, program: list[str] | None = None) -> dict:
    """Write the unit, enable it and (re)start it.

    Idempotent: a running service is restarted, so re-running this after an
    upgrade or a ``cswap config set autoswitch.*`` change picks up both.
    """
    _require_linux()
    program = program or resolve_program()
    path = unit_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, build_unit(program).encode())
    _check(_systemctl("daemon-reload"), "daemon-reload")
    _check(_systemctl("enable", UNIT), f"enable {UNIT}")
    _check(_systemctl("restart", UNIT), f"restart {UNIT}")
    return {
        "unit": UNIT,
        "unit_path": str(path),
        "program": [*program, "auto"],
        "linger": _lingering(),
    }


def uninstall(home: Path | None = None) -> dict:
    """Stop and disable the service and delete its unit file.

    Tolerates partial states (a unit file that was never enabled, an enabled
    unit whose file is gone): the point is to arrive at "gone".
    """
    _require_linux()
    path = unit_path(home)
    before = status(home)
    was_active = before["active"]
    if before["loaded"] or before["enabled"] or was_active:
        disabled = _systemctl("disable", "--now", UNIT)
        if disabled.returncode != 0 and status(home)["active"]:
            _check(disabled, f"disable --now {UNIT}")
    existed = path.exists()
    if existed:
        path.unlink()
    _systemctl("daemon-reload")
    return {"unit": UNIT, "was_active": was_active, "removed_unit": existed}
