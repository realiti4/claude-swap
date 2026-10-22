"""Editor mode: open a VS Code window whose Claude extension runs as an account.

``cswap code NUM|EMAIL [PATH]`` opens PATH in a VS Code (or Cursor, Windsurf,
...) window whose Claude Code extension uses the same per-account session
profile as ``cswap run``. Other windows keep their own accounts, so several
windows can run different accounts side by side.

How it works, and why it is not a hack: the extension reads
``CLAUDE_CONFIG_DIR`` from its extension host's environment (Claude Code
2.1.284+), and VS Code gives a window opened by the ``code`` CLI the CLI's
environment, laid over the running instance's (``userEnv``: launchMainService
→ windowsMainService → preload → localProcessExtensionHost). Both halves are
public behaviour; cswap only picks the env and the thing to open.

Three properties of that hand-off shape everything here:

* An already-open folder is only focused: its window keeps the env it
  started with. To switch the window a command is typed in, cswap reopens
  it in two steps: the window first loads an empty placeholder workspace,
  then -- from a helper that outlives the window's terminal -- the folder
  again with the new env. The folder keeps its identity, so its title, open
  editors, Claude panel and chat history come back as they were. Both steps
  reuse the editor's last focused window (``-r``), so each is preceded by
  opening what that window already shows, which the editor answers by
  focusing exactly that window: a click elsewhere mid-switch cannot send a
  step to another window.
* ``userEnv`` overlays and cannot unset. If cswap started the editor itself
  with ``CLAUDE_CONFIG_DIR`` set, every window opened later from the editor's
  UI would inherit the account. So a cold editor is first started with the
  plain environment.
* A full editor restart restores windows without the env they were opened
  with: they come back on the default login. ``cswap code N`` in one puts it
  back.

The extension writes ``<config dir>/ide/<port>.lock`` for every window, under
the config dir that window runs on, and sets ``CLAUDE_CODE_SSE_PORT`` in that
window's integrated terminals. Those two tell cswap which windows are open on
which profile and which window a command is typed in.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from claude_swap.exceptions import SessionError
from claude_swap.fsutil import atomic_write_text
from claude_swap.mappings import normalize_path
from claude_swap.paths import get_default_claude_config_home
from claude_swap.process_detection import IdeInstance, list_ide_instances

# The extension's ``ideName`` → that editor's CLI launcher. An unknown editor
# falls back to ``code``; ``--editor`` names any other launcher directly.
EDITOR_LAUNCHERS = {
    "Visual Studio Code": "code",
    "Visual Studio Code - Insiders": "code-insiders",
    "Cursor": "cursor",
    "Windsurf": "windsurf",
    "VSCodium": "codium",
}
DEFAULT_LAUNCHER = "code"

# Set in a window opened on a session profile, so its terminals (which
# inherit CLAUDE_CONFIG_DIR) can tell cswap they are an editor window, not a
# `cswap run` session shell. See cli.main.
EDITOR_WINDOW_MARK = "CSWAP_EDITOR_WINDOW"

# Plumbing from the invoking shell that must not reach the new window. Run
# from an editor terminal or a Claude Code session, the shell carries that
# window's and that session's plumbing: the SSE port of the window it lives
# in, Claude's session id and messaging token, ELECTRON_RUN_AS_NODE, and
# VS Code's per-process VSCODE_* handles. Dropping them lets the new window
# take these from the editor itself, as a window opened from its UI does.
# Everything else -- including a user's own CLAUDE_CODE_* settings such as
# CLAUDE_CODE_USE_BEDROCK -- passes through, as with a plain `code .`.
_LEAK_VARS = frozenset({
    "ELECTRON_RUN_AS_NODE",
    "CLAUDECODE",
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_PID",
    "CLAUDE_EFFORT",
    "CLAUDE_AGENT_SDK_VERSION",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SSE_PORT",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_EMIT_STARTUP_TIMING",
    "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING",
    "CLAUDE_CODE_ENABLE_TASKS",
    "CLAUDE_CODE_QUESTION_PREVIEW_FORMAT",
    EDITOR_WINDOW_MARK,
})
# VSCODE_* variables a user sets on purpose (portable mode, data dirs).
_KEEP_VSCODE = frozenset({"VSCODE_PORTABLE", "VSCODE_APPDATA", "VSCODE_LOGS", "VSCODE_EXTENSIONS"})


def _leaks(name: str) -> bool:
    upper = name.upper()
    if upper.startswith("VSCODE_"):
        return upper not in _KEEP_VSCODE
    if upper.startswith("CLAUDE_CODE_") and (
        upper.endswith(("_TOKEN", "_SOCKET", "_FILE_DESCRIPTOR")) or "SESSION" in upper
    ):
        return True  # session plumbing and secrets a future Claude Code may add
    return upper in _LEAK_VARS


def which_no_cwd(name: str) -> str | None:
    """``shutil.which`` without the Windows current-directory lookup.

    On Windows, ``which`` searches the current directory before PATH unless
    ``NoDefaultCurrentDirectoryInExePath`` is set. `cswap code` runs inside
    a project folder, so a ``code.cmd`` checked into a repository would be
    run -- with the account's environment -- instead of the editor.
    """
    if sys.platform != "win32":
        return shutil.which(name)
    key = "NoDefaultCurrentDirectoryInExePath"
    old = os.environ.get(key)
    os.environ[key] = "1"
    try:
        return shutil.which(name)
    finally:
        if old is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = old

# Seconds to wait for a cold editor to come up, and for a new window's
# extension to report in, before carrying on without the confirmation.
EDITOR_START_TIMEOUT = 20.0
WINDOW_CONFIRM_TIMEOUT = 20.0
# How long the reopen helper waits for the window to let go of the folder.
# Generous: the editor first asks about unsaved files, and the user may take
# a while to answer. Cancelling leaves the window as it was, and the helper
# gives up quietly when this runs out.
REOPEN_TIMEOUT = 120.0
# The placeholder the window holds between the two steps of a reopen. Its
# name is what the title bar shows for that moment.
PLACEHOLDER_NAME = "Switching account.code-workspace"
# Keeps the placeholder out of File > Open Recent.
_NO_RECENT = "--skip-add-to-recently-opened"
_POLL_INTERVAL = 0.5


@dataclass
class EditorWindow:
    """An open editor window with the Claude extension, and its config dir."""

    instance: IdeInstance
    config_dir: Path

    @property
    def folders(self) -> list[str]:
        return [normalize_path(f) for f in self.instance.workspace_folders]


def editor_windows(sessions_dir: Path) -> list[EditorWindow]:
    """Every live editor window on the default login or a session profile."""
    dirs = [get_default_claude_config_home()]
    if sessions_dir.is_dir():
        dirs += sorted(d for d in sessions_dir.iterdir() if d.is_dir())
    return [
        EditorWindow(instance, d)
        for d in dirs
        for instance in list_ide_instances(d, require_listening=True)
    ]


def _contains(root: str, inner: str) -> bool:
    """Whether normalized path ``inner`` is ``root`` or inside it."""
    return inner == root or inner.startswith(root.rstrip(os.sep) + os.sep)


def current_window(
    windows: list[EditorWindow],
    env: os._Environ[str] | dict[str, str] | None = None,
    cwd: Path | None = None,
) -> EditorWindow | None:
    """The window whose integrated terminal this process runs in, if any.

    The extension marks its window's terminals with ``CLAUDE_CODE_SSE_PORT``,
    but only terminals opened after it activated: one opened earlier (a
    restored terminal, or one opened right after the window) lacks it. In an
    editor terminal without the mark, the window is the one whose folder
    holds ``cwd`` — the same match Claude Code itself falls back to — when
    exactly one window does.
    """
    env = os.environ if env is None else env
    port = env.get("CLAUDE_CODE_SSE_PORT", "")
    if port.isdigit():
        marked = next((w for w in windows if w.instance.port == int(port)), None)
        if marked is not None:
            return marked
        # A stale mark: the extension restarted since this terminal opened
        # (new port), and the terminal kept the old value. Match by folder.
    if cwd is None or env.get("TERM_PROGRAM") != "vscode":
        return None
    inner = normalize_path(cwd)
    matches = [w for w in windows if len(w.folders) == 1 and _contains(w.folders[0], inner)]
    return matches[0] if len(matches) == 1 else None


def default_folder(sessions_dir: Path, cwd: Path) -> Path:
    """The folder `cswap code` means when none is given.

    In a window's terminal that is the window's folder, even from a
    subdirectory of it, so the command acts on the window it is typed in.
    Elsewhere it is the current directory — except a home directory or a
    drive root, which is where a terminal of a window with no folder starts
    and never a project: opening it on an account was not what was meant.
    """
    here = current_window(editor_windows(sessions_dir), cwd=cwd)
    if here is not None and len(here.folders) == 1:
        if _contains(here.folders[0], normalize_path(cwd)):
            return Path(here.instance.workspace_folders[0])
    if (here is not None and not here.folders) or normalize_path(cwd) in (
        normalize_path(Path.home()),
        normalize_path(cwd.anchor),
    ):
        raise SessionError(
            "No project folder here: run this in the terminal of a window "
            "that has a folder open, or name the folder: cswap code N PATH"
        )
    return cwd


def resolve_launcher(editor: str | None, here: EditorWindow | None) -> str:
    """Path of the editor CLI: ``--editor``, else the current window's, else ``code``."""
    name = editor or (
        EDITOR_LAUNCHERS.get(here.instance.ide_name) if here is not None else None
    ) or DEFAULT_LAUNCHER
    path = which_no_cwd(name)
    if not path:
        raise SessionError(
            f"'{name}' was not found on PATH. Install the editor's shell "
            "command (in VS Code: Command Palette → \"Shell Command: Install "
            "'code' command in PATH\"), or pass --editor."
        )
    return path


def ide_names_for(launcher: str) -> set[str]:
    """The ``ideName`` values a launcher's windows report (empty if unknown)."""
    stem = Path(launcher).stem.lower()
    return {ide for ide, name in EDITOR_LAUNCHERS.items() if name == stem}


def placeholder_workspace(backup_dir: Path, port: int) -> Path:
    """The empty workspace window ``port`` holds while it is reopened.

    One per window: the editor keeps one window per workspace, so two
    windows switching at once must not share it. Restricted Mode in it is
    harmless: nothing runs there, and the window leaves it as soon as the
    editor has let go of the folder.
    """
    path = backup_dir / "workspaces" / str(port) / PLACEHOLDER_NAME
    content = json.dumps({"folders": []}) + "\n"
    try:
        if path.read_text(encoding="utf-8") == content:
            return path
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, content, follow_symlinks=False)
    return path


def spawn_reopen(
    launcher: str,
    folder: Path,
    port: int,
    sessions_dir: Path,
    placeholder: Path,
    env: dict[str, str],
) -> None:
    """Start the helper that finishes reopening window ``port``.

    Started before step one, and in a session of its own, because step one
    ends the terminal this command runs in. The env travels as the helper's
    own environment, never on its command line.
    """
    payload = json.dumps({
        "launcher": launcher,
        "folder": str(folder),
        "port": port,
        "sessions_dir": str(sessions_dir),
        "placeholder": str(placeholder),
    })
    # -P keeps the current directory -- the user's project -- off sys.path,
    # and the helper runs from cswap's own folder: a json.py (or a
    # claude_swap/) checked into a repository must never be imported by a
    # process holding the account's environment.
    argv = [sys.executable, "-P", "-m", "claude_swap.editor", "--reopen", payload]
    kwargs: dict = {
        "cwd": str(sessions_dir.parent),
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        # A hidden console of its own, not DETACHED_PROCESS: Windows lets a
        # process without a console no say over which window comes to the
        # front, so the helper's "focus the placeholder" would be ignored and
        # its -r would land on whatever window the user last clicked.
        flags = subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0  # SW_HIDE
        kwargs["startupinfo"] = startup
        try:
            # Also leave any job object the terminal's processes belong to,
            # in case closing it is set to kill them.
            subprocess.Popen(
                argv, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kwargs
            )
            return
        except OSError:
            pass  # the job forbids breakaway; a console of its own still survives
        try:
            subprocess.Popen(argv, creationflags=flags, **kwargs)
        except OSError as e:
            raise SessionError(f"Could not start the reopen helper: {e}") from e
        return
    try:
        subprocess.Popen(argv, start_new_session=True, **kwargs)
    except OSError as e:
        raise SessionError(f"Could not start the reopen helper: {e}") from e


def begin_reopen(
    launcher: str, folder: Path, placeholder: Path, env: dict[str, str]
) -> None:
    """Step one: move the window showing ``folder`` onto its placeholder.

    Opening ``folder`` first focuses the one window that shows it, so the
    ``-r`` right after lands on that window and no other.
    """
    launch(launcher, [str(folder)], env)
    launch(launcher, ["-r", _NO_RECENT, str(placeholder)], env)


def finish_reopen(
    launcher: str, folder: str, port: int, sessions_dir: Path, placeholder: str
) -> bool:
    """Step two, in the helper. Returns whether it ran.

    The folder is reopened only once the window has let go of it: its
    extension's lockfile is gone and no window lists the folder any more --
    until then the editor would just focus it again. (A lockfile alone also
    goes away when only the extension restarts.) A window that never lets go
    (the user cancelled at the unsaved-files prompt) is left alone.
    """
    env = dict(os.environ)
    wanted = normalize_path(folder)

    def let_go() -> bool:
        windows = editor_windows(sessions_dir)
        return all(w.instance.port != port and w.folders != [wanted] for w in windows)

    released = wait_for(let_go, REOPEN_TIMEOUT)
    if not released:
        return False
    time.sleep(1.0)  # let the window settle on the placeholder first
    launch(launcher, [_NO_RECENT, placeholder], env)  # focus that window
    launch(launcher, ["-r", folder], env)
    # The placeholder is per window port, so each switch makes a new one;
    # leave none behind once the window has moved off it. Only that one file
    # and then its folder if it is empty -- never a recursive delete: a wrong
    # path here must not be able to take anything else with it.
    time.sleep(2.0)
    remove_placeholder(Path(placeholder), sessions_dir.parent)
    return True


def remove_placeholder(placeholder: Path, backup_dir: Path) -> None:
    """Delete a placeholder written by :func:`placeholder_workspace`, nothing else.

    Only a file with the placeholder's name, in a ``<port>`` folder directly
    under ``<backup_dir>/workspaces``, neither of them a link -- then that
    folder, and only if it is empty.
    """
    folder = placeholder.parent
    root = os.path.normcase(os.path.realpath(backup_dir / "workspaces"))
    if (
        placeholder.name != PLACEHOLDER_NAME
        or not folder.name.isdigit()
        or placeholder.is_symlink()
        or folder.is_symlink()
        or folder.is_junction()
        or os.path.normcase(os.path.realpath(folder.parent)) != root
    ):
        return
    try:
        placeholder.unlink()
        folder.rmdir()  # only succeeds when the folder is empty
    except OSError:
        pass


def launch_env(config_dir: str | None, scrub: tuple[str, ...] = ()) -> dict[str, str]:
    """The environment to hand the editor CLI.

    ``config_dir`` is the session profile to pin, ``""`` to pin the default
    login explicitly (needed when reusing a window: its renderer keeps the
    env it was opened with, and an empty value is how Claude Code spells
    "unset"), or ``None`` to leave ``CLAUDE_CONFIG_DIR`` out.
    """
    env = {k: v for k, v in os.environ.items() if not _leaks(k) and k not in scrub}
    if config_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = config_dir
        # The mark names the profile it belongs to, so it cannot vouch for
        # any other CLAUDE_CONFIG_DIR (cli._drop_session_profile_env).
        env[EDITOR_WINDOW_MARK] = config_dir
    return env


# Characters cmd.exe treats specially even inside double quotes, or that end
# a command line; a path containing one is refused rather than mangled.
_CMD_REFUSE = frozenset('"%\r\n\x00')


def _windows_script_command(launcher: str, args: list[str]) -> str:
    """The command line for a Windows ``.cmd`` launcher, quoted for cmd.exe.

    A ``.cmd`` runs through cmd.exe, which splits an unquoted argument at
    ``&``, ``|``, ``<`` and ``>`` and runs the rest as a command -- and
    ``subprocess`` only quotes arguments that contain spaces (the
    "BatBadBut" class, CVE-2024-24576). So cmd.exe is run explicitly with
    AutoRun off (/d), delayed ``!`` expansion off (/v:off) and only the outer
    quotes stripped (/s), and every argument is double-quoted. Backslashes
    before a closing quote are doubled so ``C:\\`` reaches the program as
    ``C:\\`` rather than an escaped quote.
    """
    for part in (launcher, *args):
        if _CMD_REFUSE & set(part):
            raise SessionError(
                f"Cannot pass {part!r} to {Path(launcher).name}: cmd.exe would "
                "misread a \" or % (or a line break) in it. Rename the folder, "
                "or open it from the editor."
            )

    def quote(s: str) -> str:
        trailing = len(s) - len(s.rstrip("\\"))
        return '"' + s + "\\" * trailing + '"'

    inner = " ".join(quote(p) for p in (launcher, *args))
    cmd = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe")
    return f'"{cmd}" /d /v:off /e:on /s /c "{inner}"'


def launch(launcher: str, args: list[str], env: dict[str, str]) -> None:
    """Run the editor CLI. It hands off to the editor and returns at once."""
    command: str | list[str] = [launcher, *args]
    if sys.platform == "win32" and Path(launcher).suffix.lower() in (".cmd", ".bat"):
        command = _windows_script_command(launcher, args)
    try:
        result = subprocess.run(
            command,
            env=env,
            stdin=subprocess.DEVNULL,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SessionError(f"Could not run {launcher}: {e}") from e
    if result.returncode != 0:
        raise SessionError(
            f"{Path(launcher).name} exited with code {result.returncode}."
        )


def wait_for(predicate, timeout: float) -> bool:
    """Poll ``predicate`` until it is true or ``timeout`` seconds pass."""
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL_INTERVAL)


if __name__ == "__main__":
    # `python -m claude_swap.editor --reopen <json>`: the detached helper
    # started by spawn_reopen.
    if len(sys.argv) == 3 and sys.argv[1] == "--reopen":
        job = json.loads(sys.argv[2])
        try:
            finish_reopen(
                job["launcher"],
                job["folder"],
                int(job["port"]),
                Path(job["sessions_dir"]),
                job["placeholder"],
            )
        except SessionError:
            sys.exit(1)
