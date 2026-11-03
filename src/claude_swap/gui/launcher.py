"""Desktop plumbing for the GUI: locate the CLI, open terminals and folders.

Session mode (``cswap run``) hands the terminal to Claude Code, so the GUI
cannot host it in its own window; it opens a new terminal running the CLI
instead. In a packaged Windows build the CLI is ``cswap.cmd`` next to the GUI
executable; on macOS it is the app executable itself (see
``packaging/entry.py``); from source it is ``python -m claude_swap``.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


class LaunchError(Exception):
    """A terminal or file manager could not be opened."""


def split_args(text: str) -> list[str]:
    """Split user-typed extra claude arguments the way the host shell would."""
    if not text.strip():
        return []
    if sys.platform == "win32":
        return text.split()
    return shlex.split(text)


def cli_command() -> list[str]:
    """argv prefix that runs the claude-swap CLI."""
    if getattr(sys, "frozen", False):
        if sys.platform != "win32":
            # One executable on macOS: given arguments, it runs as the CLI.
            return [sys.executable]
        cli = Path(sys.executable).resolve().parent / "cswap.cmd"
        if cli.exists():
            return [str(cli)]
        raise LaunchError(f"找不到命令行程序：{cli}")
    return [sys.executable, "-m", "claude_swap"]


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    # The GUI forces NO_COLOR for its captured output; the terminal wants color.
    if env.get("CSWAP_GUI_NO_COLOR") == "1":
        env.pop("NO_COLOR", None)
        env.pop("CSWAP_GUI_NO_COLOR", None)
    return env


def open_terminal(args: list[str], cwd: str | None = None) -> str:
    """Open a new terminal window running the CLI with ``args``.

    Returns the command line as shown to the user.
    """
    argv = cli_command() + args
    cwd = cwd or str(Path.home())
    if not os.path.isdir(cwd):
        raise LaunchError(f"目录不存在：{cwd}")
    env = _child_env()

    if sys.platform == "win32":
        shown = subprocess.list2cmdline(argv)
        # `cmd /k` keeps the window open after claude exits so errors stay
        # readable; `call` keeps cmd from stripping the quoted exe path.
        subprocess.Popen(
            f'cmd.exe /k call {shown}',
            cwd=cwd,
            env=env,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        return shown

    shown = shlex.join(argv)
    if sys.platform == "darwin":
        script = f"cd {shlex.quote(cwd)} && {shown}"
        applescript = (
            'tell application "Terminal"\n'
            f"  do script {_applescript_str(script)}\n"
            "  activate\n"
            "end tell"
        )
        try:
            subprocess.Popen(["osascript", "-e", applescript], env=env)
        except OSError as e:
            raise LaunchError(f"无法打开 Terminal：{e}") from e
        return shown

    # Linux / other POSIX: try the usual terminal emulators.
    hold = f'{shown}; echo; read -r -p "Press Enter to close..." _'
    inner = ["bash", "-c", hold]
    candidates: list[list[str]] = []
    preferred = os.environ.get("TERMINAL")
    if preferred:
        candidates.append([preferred, "-e", *inner])
    candidates += [
        ["x-terminal-emulator", "-e", *inner],
        ["gnome-terminal", "--", *inner],
        ["konsole", "-e", *inner],
        ["xfce4-terminal", "-x", *inner],
        ["kitty", *inner],
        ["alacritty", "-e", *inner],
        ["wezterm", "start", "--", *inner],
        ["xterm", "-e", *inner],
    ]
    for cand in candidates:
        if shutil.which(cand[0]):
            try:
                subprocess.Popen(cand, cwd=cwd, env=env, start_new_session=True)
                return shown
            except OSError:
                continue
    raise LaunchError("找不到可用的终端程序（可设置 TERMINAL 环境变量）。命令：" + shown)


def _applescript_str(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def reveal(path: str) -> None:
    """Open ``path`` in the platform file manager."""
    if not os.path.exists(path):
        raise LaunchError(f"路径不存在：{path}")
    try:
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606 - user-chosen local path
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path], start_new_session=True)
    except OSError as e:
        raise LaunchError(f"无法打开：{e}") from e
