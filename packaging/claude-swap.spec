# PyInstaller spec for the claude-swap desktop build (Windows / macOS).
#
#   uv run pyinstaller packaging/claude-swap.spec --noconfirm
#
# Normally run through packaging/build.py, which also places cswap.cmd.
#
# Output (one folder, kept small on purpose):
#   Windows: dist/claude-swap/claude-swap.exe — the GUI, and the CLI behind
#            cswap.cmd (see packaging/entry.py).
#   macOS:   dist/claude-swap.app — one executable; with arguments it is the
#            CLI.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import copy_metadata

ROOT = Path(SPECPATH).parent
IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

datas = copy_metadata("claude-swap") + [
    (str(ROOT / "src/claude_swap/gui/web"), "claude_swap/gui/web"),
]

# Everything the desktop build never runs. The Textual TUI and its rendering
# stack are replaced by the GUI; pywebview ships backends for every platform
# and only one is used per OS.
excludes = [
    "textual", "rich", "pygments", "markdown_it", "mdit_py_plugins",
    "linkify_it", "uc_micro", "rumps",
    "tkinter", "_tkinter", "unittest", "pydoc", "doctest", "lib2to3",
    "sqlite3", "_sqlite3", "xmlrpc", "pdb", "test", "idlelib", "turtle",
    "asyncio", "multiprocessing", "curses", "_curses", "pkg_resources",
    "setuptools", "_zstd", "compression.zstd", "lzma", "_lzma", "bz2", "_bz2",
    "webview.platforms.qt", "webview.platforms.cef", "webview.platforms.gtk",
    "webview.platforms.mshtml", "webview.platforms.android",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "qtpy", "cefpython3", "gi",
]
if IS_WIN:
    excludes.append("webview.platforms.cocoa")
else:
    excludes += ["webview.platforms.winforms", "webview.platforms.edgechromium",
                 "webview.platforms.win32", "clr", "clr_loader", "pythonnet"]


def _keep(entry) -> bool:
    """Drop bundled files that this platform/arch never loads."""
    dest = entry[0].replace("\\", "/")
    name = dest.rsplit("/", 1)[-1].lower()
    if name in ("pywebview-android.jar", "python.runtime.xml"):
        return False
    if IS_WIN:
        # The Universal CRT ships with Windows 10+ (the minimum for Python 3.14).
        if name == "ucrtbase.dll" or name.startswith("api-ms-win-"):
            return False
        # x64 build: x86 interop/loader DLLs are dead weight. (The per-arch
        # WebView2Loader folders must all stay: pywebview requires each one.)
        if "/dlls/x86/" in dest:
            return False
        if name == "webbrowserinterop.x86.dll":
            return False
    return True


a = Analysis(
    [str(ROOT / "packaging/entry.py")],
    pathex=[str(ROOT / "src")],
    datas=datas,
    hiddenimports=["claude_swap.gui", "claude_swap.gui.server"],
    excludes=excludes,
    noarchive=False,
    optimize=2,
)
a.datas = [d for d in a.datas if _keep(d)]
a.binaries = [b for b in a.binaries if _keep(b)]

pyz = PYZ(a.pure)

icon = str(ROOT / "packaging" / ("icon.ico" if IS_WIN else "icon.icns"))
common = dict(
    exclude_binaries=True,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX breaks the .NET/WebView2 DLLs and trips antivirus
    icon=icon,
)

exe = EXE(pyz, a.scripts, [], name="claude-swap", console=False, **common)

coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="claude-swap")

if IS_MAC:
    from importlib.metadata import version

    _v = version("claude-swap")

    app = BUNDLE(
        coll,
        name="claude-swap.app",
        icon=icon,
        bundle_identifier="io.github.xutianhao6.claude-swap",
        version=_v.split("b")[0],
        info_plist={
            "CFBundleShortVersionString": _v,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
            "NSAppleEventsUsageDescription": "Open Terminal to run Claude Code as a chosen account.",
        },
    )
