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
    """argv prefix that runs the claude-swap CLI in a terminal."""
    if getattr(sys, "frozen", False):
        if sys.platform != "win32":
            # One executable on macOS: given arguments, it runs as the CLI.
            return [sys.executable]
        cli = Path(sys.executable).resolve().parent / "cswap.cmd"
        if cli.exists():
            return [str(cli)]
        raise LaunchError(f"找不到命令行程序：{cli}")
    return [sys.executable, "-m", "claude_swap"]


def cli_capture_command() -> list[str]:
    """argv prefix that runs the CLI as a captured, windowless child."""
    if getattr(sys, "frozen", False):
        # Straight to the executable: no cmd.exe in between to quote around.
        return [sys.executable, "--cli"] if sys.platform == "win32" else [sys.executable]
    return [sys.executable, "-m", "claude_swap"]


def _claude_candidates() -> list[Path]:
    home = Path.home()
    if sys.platform == "win32":
        appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        return [home / ".local" / "bin" / "claude.exe", appdata / "npm" / "claude.cmd"]
    return [
        home / ".local" / "bin" / "claude",
        home / ".claude" / "local" / "claude",
        Path("/opt/homebrew/bin/claude"),
        Path("/usr/local/bin/claude"),
        home / ".npm-global" / "bin" / "claude",
    ]


def find_claude() -> str | None:
    """The claude executable, also when the app was started without a shell.

    A macOS app opened from Finder gets launchd's bare PATH, so the usual
    install locations are tried after PATH.
    """
    found = shutil.which("claude")
    if found:
        return found
    for candidate in _claude_candidates():
        if candidate.is_file():
            return str(candidate)
    return None


def tool_path(claude: str, path: str) -> str:
    """PATH for running ``claude``: its own folder first (an npm install
    needs the node beside it), then the common tool folders a Finder-started
    app does not inherit."""
    extra = [str(Path(claude).parent)]
    if sys.platform == "darwin":
        extra += ["/opt/homebrew/bin", "/usr/local/bin"]
    parts = [p for p in path.split(os.pathsep) if p]
    return os.pathsep.join(dict.fromkeys(extra + parts))


def child_env(set_env: dict[str, str] | None = None, unset_env: tuple[str, ...] = ()) -> dict[str, str]:
    env = dict(os.environ)
    # The GUI forces NO_COLOR for its captured output; terminals want color.
    if env.get("CSWAP_GUI_NO_COLOR") == "1":
        env.pop("NO_COLOR", None)
        env.pop("CSWAP_GUI_NO_COLOR", None)
    for key in unset_env:
        env.pop(key, None)
    env.update(set_env or {})
    return env


def open_terminal(
    argv: list[str],
    cwd: str | None = None,
    *,
    set_env: dict[str, str] | None = None,
    unset_env: tuple[str, ...] = (),
    keep_open: bool = True,
) -> str:
    """Open a new terminal window running ``argv``.

    ``keep_open`` leaves the window open after the command exits so its
    output stays readable; otherwise the window closes on success and waits
    for a key only on failure. Returns the command line as shown to the user.
    """
    cwd = cwd or str(Path.home())
    if not os.path.isdir(cwd):
        raise LaunchError(f"目录不存在：{cwd}")

    if sys.platform == "win32":
        shown = subprocess.list2cmdline(argv)
        # `call` keeps cmd from stripping the quoted exe path (and runs .cmd
        # shims such as npm's claude.cmd).
        line = f"cmd.exe /k call {shown}" if keep_open else f"cmd.exe /c call {shown} || pause"
        subprocess.Popen(
            line,
            cwd=cwd,
            env=child_env(set_env, unset_env),
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        return shown

    if sys.platform == "darwin":
        shown = shlex.join(argv)
        # Terminal.app starts a fresh login shell, so the environment travels
        # inside the script rather than through Popen.
        parts = [f"cd {shlex.quote(cwd)}"]
        parts += [f"unset {key}" for key in unset_env]
        parts += [f"export {key}={shlex.quote(value)}" for key, value in (set_env or {}).items()]
        parts.append(shown if keep_open else f"{shown} && exit")
        applescript = (
            'tell application "Terminal"\n'
            f"  do script {_applescript_str('; '.join(parts))}\n"
            "  activate\n"
            "end tell"
        )
        try:
            subprocess.Popen(["osascript", "-e", applescript])
        except OSError as e:
            raise LaunchError(f"无法打开 Terminal：{e}") from e
        return shown

    raise LaunchError("桌面版仅支持 Windows 和 macOS")


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
