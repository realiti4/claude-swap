"""macOS notification helper for claude-swap.

Sends desktop notifications via ``osascript``.  No-op on non-macOS platforms
and never raises — the daemon must keep running even when notifications fail.
"""

from __future__ import annotations

import logging
import subprocess

from claude_swap.models import Platform

_logger = logging.getLogger("claude-swap")


def _osa_quote(text: str) -> str:
    """Escape a string for embedding in an AppleScript string literal.

    AppleScript strings are delimited by double-quotes; the only character
    that needs escaping inside them is the double-quote itself (via backslash).
    Backslash itself must also be escaped so ``\\n`` in the source stays
    literal rather than being interpreted as a newline by AppleScript.
    """
    return text.replace("\\", "\\\\").replace('"', '\\"')


def notify(title: str, message: str) -> None:
    """Send a macOS desktop notification.

    No-op on non-macOS platforms.  Never raises — log failures at DEBUG level
    so daemon ticks keep running.

    Args:
        title: Notification title shown in bold.
        message: Body text of the notification.
    """
    if Platform.detect() is not Platform.MACOS:
        return

    script = (
        f'display notification "{_osa_quote(message)}" '
        f'with title "{_osa_quote(title)}"'
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            timeout=5,
            check=False,
            capture_output=True,
        )
    except Exception as exc:  # pragma: no cover — subprocess errors on CI
        _logger.debug("notify: osascript failed: %r", exc)
