"""Run ``cswap auto`` as a systemd user service instead of a foreground process.

``cswap auto`` is a polling loop that blocks the terminal that started it, and
cswap has no built-in way to keep it running on Linux: it stops when that
terminal closes and does not come back after a logout or reboot. On macOS the
menu bar already runs the same engine and has its own LaunchAgent
(``claude_swap.launch_agent``); this module is the Linux counterpart for the
headless loop: a per-user unit that starts with the user's systemd instance and
restarts the loop if it crashes.

The unit runs plain ``cswap auto`` with no flags, so its behaviour comes from
the ``autoswitch.*`` settings (``cswap config set ...``) like any other run.
Settings are read when the loop starts; ``install()`` restarts a running
service, so re-running it after a config change or an upgrade applies both.

Decisions shared with the LaunchAgent, for the same reasons:

*The unit pins the console script, not ``sys.executable``* (see
:func:`claude_swap.launch_agent.resolve_program`): for an installed console
script (``uv tool``, ``pipx``) the script keeps its path across upgrades while
the virtualenv behind it is rebuilt.

*The unit file is written atomically at 0600*: forwarded proxy variables can
carry credentials, and a write cut short must not leave a truncated unit.

A user unit inherits the user manager's environment, not the installing
shell's, so the variables that decide *which* accounts and settings cswap uses
(``CLAUDE_CONFIG_DIR``, ``XDG_DATA_HOME``) are forwarded along with the proxy
and CA variables; otherwise the service could manage a different store than
the command that installed it.

Logs go to the journal (``journalctl --user -u cswap-auto``), the native place
for a user service's output. A user service stops when the user's last
session ends unless lingering is enabled (``loginctl enable-linger``);
:func:`status` reports it so the CLI can say so.

Everything here is Linux-only and needs a reachable user manager
(``systemctl --user``); the public functions refuse otherwise rather than
half-work, and refuse *before* writing anything.
"""

from __future__ import annotations

import os
import posixpath
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.fsutil import atomic_write_bytes
from claude_swap.launch_agent import resolve_program

UNIT = "cswap-auto.service"

# Seed a PATH that finds `claude` (process detection shells out to it).
_EXTRA_PATH_DIRS = ("~/.local/bin", "/usr/local/bin")
_BASE_PATH_DIRS = ("/usr/bin", "/bin")
# Network variables, the same set the LaunchAgent forwards. An empty value is
# kept: urllib reads an empty lowercase name as cancelling its uppercase twin.
_FORWARDED_ENV_KEYS = (
    "https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY",
    "no_proxy", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR",
    # Store location: which Claude config and which cswap backup root.
    "CLAUDE_CONFIG_DIR", "XDG_DATA_HOME",
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


def _require_user_manager() -> None:
    """Fail before touching anything when ``systemctl --user`` cannot be reached.

    Containers, WSL without systemd, and ssh sessions without a user bus all
    have a ``systemctl`` binary but no user manager to talk to.
    """
    probe = _systemctl("show-environment")
    if probe.returncode != 0:
        detail = (probe.stderr or probe.stdout or "").strip()
        raise ClaudeSwitchError(
            "No systemd user manager is reachable (systemctl --user failed"
            + (f": {detail}" if detail else "")
            + "). Run 'cswap auto' in a terminal or 'cswap auto --once' from cron instead."
        )


def unit_path(home: Path | None = None, env: Mapping[str, str] | None = None) -> Path:
    """Absolute path of the user unit file.

    ``$XDG_CONFIG_HOME`` is used only when its literal value is absolute: that
    is the rule systemd's user manager applies when it builds its unit search
    path, so a ``~/...`` or relative value (which systemd ignores) falls back to
    ``~/.config`` here too and the unit lands where systemd will look for it.
    """
    env = os.environ if env is None else env
    if home is None:
        xdg = env.get("XDG_CONFIG_HOME", "")
        if xdg.startswith("/"):
            return Path(xdg) / "systemd" / "user" / UNIT
    return (home or Path.home()) / ".config" / "systemd" / "user" / UNIT


def _check_word(value: str) -> None:
    if "\n" in value or "\r" in value:
        raise ClaudeSwitchError("cannot write a newline into a systemd unit setting")


def _quote_exec(arg: str) -> str:
    """Quote one ExecStart word: systemd expands ``%`` specifiers and ``$`` variables there."""
    _check_word(arg)
    escaped = arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    if escaped != arg or not arg or any(c.isspace() for c in arg):
        return f'"{escaped}"'
    return arg


def _quote_env(assignment: str) -> str:
    """Quote one ``Environment=`` assignment: ``%`` is a specifier, ``$`` is literal."""
    _check_word(assignment)
    escaped = assignment.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if escaped != assignment or any(c.isspace() for c in assignment):
        return f'"{escaped}"'
    return assignment


def _path_env(program: list[str]) -> str:
    """PATH for the service, with the program's own directory first.

    Built with ``posixpath`` because the result is a Linux PATH whatever
    platform renders the unit (the tests render it everywhere).
    """
    dirs: list[str] = []
    first = posixpath.dirname(program[0])
    if first not in ("", "."):
        dirs.append(first)
    home = Path.home().as_posix()
    for extra in (*_EXTRA_PATH_DIRS, *_BASE_PATH_DIRS):
        expanded = home + extra[1:] if extra.startswith("~") else extra
        if expanded not in dirs:
            dirs.append(expanded)
    return ":".join(dirs)


def build_unit(program: list[str] | None = None, env: Mapping[str, str] | None = None) -> str:
    """Render the unit file."""
    program = program or resolve_program()
    env = os.environ if env is None else env
    variables = {"PATH": _path_env(program)}
    variables.update({k: env[k] for k in _FORWARDED_ENV_KEYS if k in env})
    environment = "\n".join(f"Environment={_quote_env(f'{k}={v}')}" for k, v in variables.items())
    exec_start = " ".join(_quote_exec(a) for a in [*program, "auto"])
    return (
        "[Unit]\n"
        "Description=claude-swap auto-switch (cswap auto)\n"
        "Documentation=https://github.com/realiti4/claude-swap\n"
        "Wants=network-online.target\n"
        "After=network-online.target\n"
        "\n"
        "[Service]\n"
        # exec, not simple: `systemctl start` fails if the program cannot be
        # executed, instead of reporting a start that immediately died.
        "Type=exec\n"
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


def _current_user() -> str | None:
    """Login name of the effective uid; ``$USER`` can be unset or stale."""
    try:
        import pwd

        return pwd.getpwuid(os.geteuid()).pw_name
    except (ImportError, KeyError, OSError):
        return os.environ.get("USER") or None


def _lingering() -> bool | None:
    """Whether the user manager outlives the last session (None if unknown)."""
    user = _current_user()
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
    if out.returncode != 0:
        return None
    return {"yes": True, "no": False}.get(out.stdout.strip().lower())


def status(home: Path | None = None) -> dict:
    """Installed / enabled / active state, plus the pid when there is one."""
    _require_linux()
    _require_user_manager()
    shown = _systemctl(
        "show", UNIT, "--property=LoadState,ActiveState,SubState,UnitFileState,MainPID"
    )
    _check(shown, f"show {UNIT}")
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
        "active": props.get("ActiveState") in ("active", "activating", "reloading"),
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
    _require_user_manager()
    program = program or resolve_program()
    unit = build_unit(program)  # validates before anything is written
    path = unit_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, unit.encode())
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
    unit whose file is gone): the point is to arrive at "gone". Each step's
    failure is checked against the state it was meant to change, so a real
    failure is reported instead of leaving a running or enabled service behind.
    """
    _require_linux()
    before = status(home)
    path = unit_path(home)
    if before["active"]:
        stopped = _systemctl("stop", UNIT)
        if stopped.returncode != 0 and status(home)["active"]:
            _check(stopped, f"stop {UNIT}")
    if before["enabled"]:
        disabled = _systemctl("disable", UNIT)
        if disabled.returncode != 0 and status(home)["enabled"]:
            _check(disabled, f"disable {UNIT}")
    existed = path.exists()
    if existed:
        path.unlink()
    _check(_systemctl("daemon-reload"), "daemon-reload")
    return {"unit": UNIT, "was_active": before["active"], "removed_unit": existed}
