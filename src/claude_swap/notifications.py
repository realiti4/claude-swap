"""Native desktop notifications for the headless ``cswap auto`` loop.

The menu bar already surfaces engine events through ``rumps.notification``
(menubar.py); the foreground CLI loop had no equivalent — a user who parks
``cswap auto`` in a corner terminal only learns a switch happened when they
next look at it, by which time a rate-limit-parked Claude Code session has
often been sitting idle for minutes.

This module is that equivalent, with the same shape as everything else the
engine reports through:

- **Opt-in** — ``autoswitch.notify`` in settings.json (default off), or
  ``cswap auto --notify`` for a one-off. The menu bar notifies on its own;
  users running both would otherwise get duplicate popups.
- **Best-effort, never fatal** — a failed or missing notifier is logged at
  debug and forgotten; the engine's own reporting is unchanged.
- **No new dependencies** — macOS goes through ``osascript``, Linux through
  ``notify-send`` when present, Windows is a documented no-op.

The event set mirrors the menu bar's: ``switch``, ``account-quarantined``,
``all-exhausted``. Poll ticks and no-switch reasons stay in the terminal —
notifying once a minute about "below-threshold" would train the user to
dismiss everything.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from collections.abc import Callable

from claude_swap.autoswitch import AutoSwitchEvent

_logger = logging.getLogger(__name__)

# Same set menubar.py notifies on (switch / quarantined / all-exhausted).
# Anything quieter stays terminal-only.
NOTIFIED_KINDS = frozenset({"switch", "account-quarantined", "all-exhausted"})

_NOTIFICATION_TITLE = "claude-swap auto"

# The engine ticks can be frequent; a hung notifier must not stack up.
_NOTIFY_TIMEOUT_SECONDS = 5.0


def _applescript_escape(text: str) -> str:
    """Escape ``text`` for a double-quoted AppleScript string literal.

    Backslash first, then the quote itself; newlines become a literal
    ``\\n`` escape so a multi-line ``human()`` line cannot inject a second
    AppleScript statement.
    """
    return (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "")
    )


def _notify_darwin(title: str, body: str) -> None:
    script = (
        f'display notification "{_applescript_escape(body)}" '
        f'with title "{_applescript_escape(title)}"'
    )
    subprocess.run(
        ["osascript", "-e", script],
        timeout=_NOTIFY_TIMEOUT_SECONDS,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def _notify_linux(title: str, body: str) -> None:
    if shutil.which("notify-send") is None:
        _logger.debug("notify-send not found; skipping desktop notification")
        return
    subprocess.run(
        ["notify-send", "--app-name", "claude-swap", title, body],
        timeout=_NOTIFY_TIMEOUT_SECONDS,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def _notify_windows(title: str, body: str) -> None:  # pragma: no cover
    # No dependency-free channel worth its failure modes (win10toast pulls
    # in dependencies and needs a stub exe for the tray icon). The engine
    # keeps its terminal output; users on Windows lose only the popup.
    _logger.debug("desktop notifications are not implemented on Windows")


_DISPATCHERS = {
    "darwin": _notify_darwin,
    "linux": _notify_linux,
    "win32": _notify_windows,
}


def send_notification(title: str, body: str) -> None:
    """Show one native desktop notification, swallowing every failure.

    Delivery problems (missing notifier binary, timeout, OS refusal) are
    debug-logged, never raised — the engine must keep switching accounts
    even when the popup cannot be shown.
    """
    dispatcher = _DISPATCHERS.get(sys.platform)
    if dispatcher is None:  # e.g. cygwin, freebsd — same deal as Windows
        _logger.debug(
            "no desktop notification dispatcher for platform %r", sys.platform
        )
        return
    try:
        dispatcher(title, body)
    except (OSError, subprocess.SubprocessError) as exc:
        _logger.debug("desktop notification failed: %s", exc)


def make_notifying_emit(
    inner: Callable[[AutoSwitchEvent], None],
) -> Callable[[AutoSwitchEvent], None]:
    """Wrap an engine ``on_event`` callback with desktop notifications.

    The inner callback always runs first — terminal/JSONL output is the
    source of truth and must not depend on the notifier's health. Only the
    kinds in :data:`NOTIFIED_KINDS` pop up; a dry-run switch event says
    "would switch" in its own ``human()`` text, so it stays honest with no
    special case here.
    """

    def emit(event: AutoSwitchEvent) -> None:
        inner(event)
        if event.kind in NOTIFIED_KINDS:
            try:
                send_notification(_NOTIFICATION_TITLE, event.human())
            except Exception as exc:  # noqa: BLE001 — never kill the engine loop
                _logger.debug("desktop notification raised: %s", exc)

    return emit
