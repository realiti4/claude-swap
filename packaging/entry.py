"""Entry point of the packaged desktop build (PyInstaller).

One frozen executable serves two roles, so the runtime ships only once:

- the GUI, when launched with no arguments;
- the CLI, when given arguments — on Windows through ``cswap.cmd``
  (``claude-swap.exe --cli %*``), on macOS by running the app executable
  directly. The GUI uses the same path to open session-mode terminals.

The Windows executable is a windowed (GUI-subsystem) program, so in CLI mode
it attaches to the console of the ``cswap.cmd`` that started it; batch files
wait for GUI programs, so the prompt returns only when the command is done.

The packaged build updates from GitHub Releases, not PyPI, and does not ship
the Textual TUI, so those CLI paths are redirected here.
"""

import sys

RELEASES_PAGE = "https://github.com/xutianhao6/claude-swap-desktop/releases"
GUI_FLAGS = {"--browser", "--debug"}
TUI_COMMANDS = {"tui", "watch", "--tui", "--watch"}


def _attach_parent_console() -> None:
    """Borrow the calling console (windowed build has no std streams).

    A stream the caller redirected to a file or pipe (``cswap list --json >
    out.json``) keeps that handle; the rest go to the console devices, where
    open() yields Unicode-capable console streams.
    """
    if sys.platform != "win32" or sys.stdout is not None:
        return
    import ctypes
    import msvcrt
    import os

    kernel32 = ctypes.windll.kernel32
    kernel32.GetStdHandle.restype = ctypes.c_void_p
    FILE_TYPE_DISK, FILE_TYPE_PIPE = 1, 3

    def redirected(std_id: int) -> int | None:
        handle = kernel32.GetStdHandle(std_id)
        if handle in (None, 0, ctypes.c_void_p(-1).value):
            return None
        if kernel32.GetFileType(ctypes.c_void_p(handle)) in (FILE_TYPE_DISK, FILE_TYPE_PIPE):
            return handle
        return None

    # Queried before attaching, which may replace the std handles.
    handles = {name: redirected(std_id) for name, std_id in (("in", -10), ("out", -11), ("err", -12))}
    kernel32.AttachConsole(-1)  # ATTACH_PARENT_PROCESS; claude children need it too

    def stream(name: str, mode: str, device: str):
        handle = handles[name]
        if handle is not None:
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY if mode == "r" else 0)
        else:
            fd = device
        buffering = 1 if mode == "w" else -1
        return open(fd, mode, encoding="utf-8", errors="replace", buffering=buffering)

    try:
        sys.stdin = stream("in", "r", "CONIN$")
        sys.stdout = stream("out", "w", "CONOUT$")
        sys.stderr = stream("err", "w", "CONOUT$")
    except OSError:
        pass  # no console and nothing redirected: run silently


def _run_cli(args: list[str]) -> None:
    from claude_swap import update_check

    def _self_upgrade(current_version=None) -> int:
        print(f"Download the latest desktop release from {RELEASES_PAGE}")
        return 0

    update_check.check_for_update = lambda current_version: None
    update_check.run_self_upgrade = _self_upgrade

    if args and args[0] in TUI_COMMANDS:
        print("The terminal dashboard is not included in the desktop build; open the claude-swap app instead.")
        sys.exit(1)
    sys.argv = [sys.argv[0], *(args or ["help"])]

    from claude_swap.cli import main

    main()


def _run():
    args = sys.argv[1:]
    if args[:1] == ["--cli"] or (args and not set(args) <= GUI_FLAGS):
        if args[:1] == ["--cli"]:
            args = args[1:]
        _attach_parent_console()
        _run_cli(args)
        return 0
    from claude_swap.gui import main

    return main(args)


if __name__ == "__main__":
    sys.exit(_run())
