"""Console output formatting for Claude Swap.

Provides subtle, modern terminal styling with a single warm accent color,
dim secondary text, and bold for structure. Inspired by Claude Code's
restrained aesthetic. Falls back to plain text when colors aren't supported.
"""

from __future__ import annotations

import os
import sys

# ANSI escape codes
_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_ACCENT = "\033[38;5;173m"  # Warm salmon/terracotta
_MUTED = "\033[38;5;250m"  # Soft gray -- readable, but quieter than normal

_colors_enabled: bool | None = None  # lazy-initialized


def _enable_windows_vt() -> bool:
    """Enable VT processing on Windows console."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_ulong()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return True
    except Exception:
        return False


def _detect_color_support() -> bool:
    """Detect whether the terminal supports ANSI colors."""
    # Respect NO_COLOR convention (https://no-color.org/)
    if os.environ.get("NO_COLOR") is not None:
        return False
    # Respect FORCE_COLOR for CI/testing
    if os.environ.get("FORCE_COLOR") is not None:
        return True
    # Not a TTY (piped output) -> no color
    if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
        return False
    # Windows: try to enable VT processing
    if sys.platform == "win32":
        return _enable_windows_vt()
    # POSIX: check TERM
    if os.environ.get("TERM", "") == "dumb":
        return False
    return True


def colors_enabled() -> bool:
    """Return whether color output is active. Caches on first call."""
    global _colors_enabled
    if _colors_enabled is None:
        _colors_enabled = _detect_color_support()
    return _colors_enabled


def _style(text: str, *codes: str) -> str:
    """Apply ANSI codes to text if colors are enabled."""
    if not colors_enabled():
        return text
    prefix = "".join(codes)
    return f"{prefix}{text}{_RESET}"


# --- Inline stylers (return styled strings for composing lines) ---


def accent(text: str) -> str:
    """Warm accent color for important elements."""
    return _style(text, _ACCENT)


def muted(text: str) -> str:
    """Slightly dimmer than normal -- for usage stats, org tags."""
    return _style(text, _MUTED)


def dimmed(text: str) -> str:
    """Dim for tertiary info -- tree connectors, hints."""
    return _style(text, _DIM)


def bolded(text: str) -> str:
    """Bold (no color) for structure."""
    return _style(text, _BOLD)


def bold_accent(text: str) -> str:
    """Bold + accent for key markers like (active)."""
    return _style(text, _BOLD, _ACCENT)


# --- Line printers (call print() internally) ---


def error(msg: str) -> None:
    """Print an error message (red) to stderr."""
    print(_style(msg, _RED), file=sys.stderr)


def warning(msg: str) -> None:
    """Print a warning message (yellow)."""
    print(_style(msg, _YELLOW))
