"""Make Claude Desktop's Code tab bill the active account, on Windows.

The Code tab runs its CLI with the app's own login, so ``cswap switch`` never
reaches it. cswap-pin's proxy re-bills it on macOS and Linux but needs POSIX
file locks and FIFOs. This is the Windows route: a mitmproxy running the addon
in :mod:`claude_swap.desktop_follow_addon`, which swaps the bearer on
``/v1/messages`` for the active account's token.

``on`` does what ``cswap pin`` does: it starts the proxy, writes ``HTTPS_PROXY``
and ``NODE_EXTRA_CA_CERTS`` into the ``env`` block of Claude Code's global
config, and arranges for the proxy to start at logon. ``off`` undoes exactly
what ``on`` wrote. ``--ensure`` and ``--heal`` are for the logon task: they
restart a dead proxy, and when that fails they take the variables out again so
sessions fall back to a direct connection instead of failing.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from claude_swap import paths
from claude_swap.claude_locks import claude_config_lock
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.fsutil import atomic_write_text

DEFAULT_PORT = 8899
TASK_NAME = "cswap-desktop-follow"
ENTRYPOINT = "claude-desktop"
LISTEN_HOST = "127.0.0.1"
_STARTUP_WAIT_S = 15.0

# Where mitmproxy's CA lives, relative to the state directory.
_CONFDIR = "mitmproxy"
_CA_FILE = "mitmproxy-ca-cert.pem"


def state_dir() -> Path:
    return paths.get_backup_root() / "desktop-follow"


def _state_file() -> Path:
    return state_dir() / "state.json"


def marker_path() -> Path:
    """The ``inference-follows`` file cswap-pin keeps and the swap-meter band reads."""
    return paths.get_backup_root() / "pin-proxy" / "inference-follows"


def ca_path() -> Path:
    return state_dir() / _CONFDIR / _CA_FILE


def addon_path() -> Path:
    return Path(__file__).with_name("desktop_follow_addon.py")


def proxy_url(port: int) -> str:
    return f"http://{LISTEN_HOST}:{port}"


def wanted_env(port: int) -> dict[str, str]:
    return {"HTTPS_PROXY": proxy_url(port), "NODE_EXTRA_CA_CERTS": str(ca_path())}


def load_state() -> dict:
    try:
        data = json.loads(_state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict) -> None:
    state_dir().mkdir(parents=True, exist_ok=True)
    atomic_write_text(_state_file(), json.dumps(state, indent=2) + "\n")


def proxy_alive(port: int) -> bool:
    try:
        with socket.create_connection((LISTEN_HOST, port), timeout=0.5):
            return True
    except OSError:
        return False


# --- the env block of Claude Code's global config -------------------------

def apply_env(config: dict, wanted: dict[str, str]) -> tuple[dict, dict[str, str]]:
    """Add ``wanted`` to ``config['env']``; return the config and what was written.

    A variable the user already set to something else is left alone and not
    recorded, so ``off`` never removes a value that was not ours.
    """
    env = config.get("env")
    env = dict(env) if isinstance(env, dict) else {}
    wrote: dict[str, str] = {}
    for key, value in wanted.items():
        current = env.get(key)
        if current is None or current == value:
            env[key] = value
            wrote[key] = value
    config = dict(config)
    config["env"] = env
    return config, wrote


def remove_env(config: dict, wrote: dict[str, str]) -> dict:
    """Remove from ``config['env']`` the entries still holding what we wrote."""
    env = config.get("env")
    if not isinstance(env, dict):
        return config
    env = {k: v for k, v in env.items() if not (k in wrote and v == wrote[k])}
    config = dict(config)
    if env:
        config["env"] = env
    else:
        config.pop("env", None)
    return config


def _edit_global_config(edit) -> None:
    path = paths.get_global_config_path()
    with claude_config_lock(timeout=10):
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            config = {}
        except ValueError as e:
            raise ClaudeSwitchError(f"{path} is not valid JSON; not touching it: {e}")
        if not isinstance(config, dict):
            raise ClaudeSwitchError(f"{path} is not a JSON object; not touching it")
        atomic_write_text(path, json.dumps(edit(config), indent=2) + "\n")


# --- the proxy process ----------------------------------------------------

def find_mitmdump() -> str:
    found = shutil.which("mitmdump")
    if not found:
        raise ClaudeSwitchError(
            "mitmdump is not installed. Install it with: uv tool install mitmproxy"
        )
    return found


def proxy_command(mitmdump: str, port: int) -> list[str]:
    return [
        mitmdump,
        "--listen-host", LISTEN_HOST,
        "--listen-port", str(port),
        "--set", f"confdir={state_dir() / _CONFDIR}",
        "-s", str(addon_path()),
        "-q",
    ]


def start_proxy(port: int) -> None:
    """Start the proxy detached and wait until it listens and has made its CA."""
    if proxy_alive(port):
        return
    state_dir().mkdir(parents=True, exist_ok=True)
    flags = 0
    extra: dict = {}
    if sys.platform == "win32":
        # Not DETACHED_PROCESS: it makes Windows ignore CREATE_NO_WINDOW, and
        # the mitmdump launcher then starts python.exe in a visible console of
        # its own. A hidden console that the children inherit keeps them hidden.
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        extra["startupinfo"] = startup
    with open(state_dir() / "proxy.log", "ab") as log:
        proc = subprocess.Popen(
            proxy_command(find_mitmdump(), port),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            env={**os.environ, "CSWAP_DESKTOP_FOLLOW_MARKER": str(marker_path())},
            creationflags=flags,
            close_fds=True,
            **extra,
        )
    state = load_state()
    state["pid"] = proc.pid
    save_state(state)
    deadline = time.monotonic() + _STARTUP_WAIT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise ClaudeSwitchError(
                f"the proxy exited at once (code {proc.returncode}); see {state_dir() / 'proxy.log'}"
            )
        if proxy_alive(port) and ca_path().exists():
            return
        time.sleep(0.25)
    raise ClaudeSwitchError(
        f"the proxy did not start listening on port {port}; see {state_dir() / 'proxy.log'}"
    )


def stop_proxy() -> None:
    pid = load_state().get("pid")
    if isinstance(pid, int) and pid > 0 and sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
    marker_path().unlink(missing_ok=True)


# --- the logon task -------------------------------------------------------

def task_action() -> str:
    """The command the task runs: pythonw, so no console window flashes each minute."""
    exe = Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    python = windowless if windowless.exists() else exe
    return f'"{python}" -m claude_swap desktop-follow --ensure'


def install_task() -> None:
    if sys.platform != "win32":
        return
    # Every minute, like the macOS LaunchAgent: it starts the proxy after logon
    # and brings it back within a minute if it dies.
    result = subprocess.run(
        ["schtasks", "/Create", "/F", "/TN", TASK_NAME, "/SC", "MINUTE", "/MO", "1",
         "/TR", task_action()],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise ClaudeSwitchError(f"could not create the scheduled task: {result.stderr.strip() or result.stdout.strip()}")


def remove_task() -> None:
    if sys.platform != "win32":
        return
    subprocess.run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME], capture_output=True, check=False)


# --- commands -------------------------------------------------------------

def turn_on(port: int = DEFAULT_PORT) -> None:
    start_proxy(port)
    wanted = wanted_env(port)
    wrote: dict[str, str] = {}

    def edit(config: dict) -> dict:
        nonlocal wrote
        config, wrote = apply_env(config, wanted)
        return config

    _edit_global_config(edit)
    state = load_state()
    state.update(enabled=True, port=port, wrote=wrote)
    save_state(state)
    install_task()


def turn_off() -> None:
    state = load_state()
    remove_task()
    stop_proxy()
    wrote = state.get("wrote") if isinstance(state.get("wrote"), dict) else {}
    if wrote:
        _edit_global_config(lambda config: remove_env(config, wrote))
    save_state({"enabled": False})


def ensure(*, port: int | None = None) -> str:
    """Bring a wanted proxy back; give the wiring up when it cannot be.

    Returns ``"off"`` (not turned on), ``"ok"``, ``"restarted"`` or ``"healed"``
    (the variables were taken out so sessions go direct). Never raises.
    """
    state = load_state()
    if not state.get("enabled"):
        return "off"
    port = port or state.get("port") or DEFAULT_PORT
    if proxy_alive(port):
        return "ok"
    try:
        start_proxy(port)
        return "restarted"
    except Exception:
        wrote = state.get("wrote") if isinstance(state.get("wrote"), dict) else {}
        try:
            if wrote:
                _edit_global_config(lambda config: remove_env(config, wrote))
        except Exception:
            pass
        marker_path().unlink(missing_ok=True)
        state.update(enabled=False, wrote={})
        save_state(state)
        return "healed"


def state_word() -> tuple[str, str]:
    """``(OK|NOT-OK|OFF, detail)``."""
    state = load_state()
    if not state.get("enabled"):
        return "OFF", "desktop-follow is not turned on"
    port = state.get("port") or DEFAULT_PORT
    if not proxy_alive(port):
        return "NOT-OK", f"nothing is listening on {LISTEN_HOST}:{port}"
    try:
        config = json.loads(paths.get_global_config_path().read_text(encoding="utf-8"))
        env = config.get("env") or {}
    except (OSError, ValueError):
        env = {}
    missing = [k for k, v in (state.get("wrote") or {}).items() if env.get(k) != v]
    if missing:
        return "NOT-OK", f"{', '.join(missing)} missing from the global config env"
    return "OK", f"proxy on {LISTEN_HOST}:{port}, variables set"


def run(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="cswap desktop-follow",
        description=(
            "Bill Claude Desktop's Code tab to the active account (Windows). "
            "Needs mitmproxy: uv tool install mitmproxy"
        ),
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--on", action="store_true", help="Start the proxy, set the variables, start it at logon")
    group.add_argument("--off", action="store_true", help="Undo --on")
    group.add_argument("--ensure", action="store_true", help="Silently restart a dead proxy; for the logon task")
    group.add_argument("--heal", action="store_true", help="Like --ensure, and say what was done")
    group.add_argument("--state", action="store_true", help="Print OK, NOT-OK or OFF, detail on stderr")
    parser.add_argument("--port", type=int, default=None, help=f"Proxy port (default {DEFAULT_PORT})")
    args = parser.parse_args(argv)

    if sys.platform != "win32":
        print(
            "desktop-follow is the Windows route. On macOS and Linux use `cswap pin`.",
            file=sys.stderr,
        )
        return 1
    if args.ensure:
        ensure(port=args.port)
        return 0
    if args.heal:
        print(ensure(port=args.port))
        return 0
    if args.state:
        word, detail = state_word()
        print(word)
        print(detail, file=sys.stderr)
        return 0
    try:
        if args.off:
            turn_off()
            print("desktop-follow is off. New sessions go direct again.")
        elif args.on:
            turn_on(args.port or DEFAULT_PORT)
            print(
                "desktop-follow is on. Start a new Code session; running sessions keep their old connection."
            )
        else:
            parser.print_help()
    except ClaudeSwitchError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0
