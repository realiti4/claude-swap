"""Cross-platform desktop notifications for claude-swap events.

Zero external dependencies: uses OS-native notification mechanisms.
  Windows  — PowerShell WinRT toast (Windows 10/11)
  macOS    — osascript notification banner
  Linux    — notify-send (libnotify)

Designed to be called from the autoswitch event stream.  Each notify_*
function is silent on failure — a broken notification path must never
crash the rotation loop.
"""

from __future__ import annotations

import logging
import subprocess
import sys

_logger = logging.getLogger("claude-swap")

# App name shown in the Windows notification centre and macOS notification list.
_APP_NAME = "Claude Swap"


# ---------------------------------------------------------------------------
# Platform dispatch
# ---------------------------------------------------------------------------


def notify(title: str, message: str, *, urgency: str = "normal") -> None:
    """Send a desktop notification.  Never raises.

    ``urgency`` is a hint: ``"normal"`` for informational, ``"critical"``
    for all-exhausted/alarm conditions.  macOS and Linux honour it;
    Windows maps critical → Alarm sound.
    """
    try:
        if sys.platform == "win32":
            _notify_windows(title, message, urgency=urgency)
        elif sys.platform == "darwin":
            _notify_macos(title, message)
        else:
            _notify_linux(title, message, urgency=urgency)
    except Exception as exc:  # pragma: no cover
        _logger.debug("Notification failed (%s): %s", type(exc).__name__, exc)


# ---------------------------------------------------------------------------
# Windows — WinRT toast via PowerShell (no extra packages)
# ---------------------------------------------------------------------------

_WIN_TOAST_PS = r"""
param([string]$Title, [string]$Body, [string]$Sound)
[Windows.UI.Notifications.ToastNotificationManager,
 Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument,
 Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml(@"
<toast>
  <visual><binding template="ToastGenericImageAndText04">
    <text id="1">$Title</text>
    <text id="2">$Body</text>
  </binding></visual>
  <audio src="ms-winsoundevent:Notification.$Sound"/>
</toast>
"@)
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier(
    "Claude Swap"
).Show($toast)
"""


def _notify_windows(title: str, message: str, *, urgency: str = "normal") -> None:
    sound = "Alarm2" if urgency == "critical" else "Default"
    subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-WindowStyle", "Hidden",
            "-Command", _WIN_TOAST_PS,
            "-Title", title,
            "-Body", message,
            "-Sound", sound,
        ],
        timeout=10,
        capture_output=True,
        check=False,
    )


# ---------------------------------------------------------------------------
# macOS — osascript
# ---------------------------------------------------------------------------


def _notify_macos(title: str, message: str) -> None:
    script = (
        f'display notification {_osa_str(message)} '
        f'with title {_osa_str(_APP_NAME)} '
        f'subtitle {_osa_str(title)}'
    )
    subprocess.run(
        ["osascript", "-e", script],
        timeout=10,
        capture_output=True,
        check=False,
    )


def _osa_str(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


# ---------------------------------------------------------------------------
# Linux — notify-send (libnotify)
# ---------------------------------------------------------------------------


def _notify_linux(title: str, message: str, *, urgency: str = "normal") -> None:
    urgency_map = {"normal": "normal", "critical": "critical"}
    u = urgency_map.get(urgency, "normal")
    subprocess.run(
        [
            "notify-send",
            "--app-name", _APP_NAME,
            "--urgency", u,
            title,
            message,
        ],
        timeout=10,
        capture_output=True,
        check=False,
    )


# ---------------------------------------------------------------------------
# Event-level helpers (called from cli.py)
# ---------------------------------------------------------------------------


def notify_switch(from_email: str | None, to_email: str | None) -> None:
    """Account rotation happened."""
    src = from_email or "previous account"
    dst = to_email or "next account"
    notify(
        "Claude Account Switched",
        f"{src}\n→ {dst}",
        urgency="normal",
    )


def notify_all_exhausted(reset_at: str | None) -> None:
    """All accounts have hit their limits."""
    body = (
        f"All accounts exhausted.\nEarliest reset: {reset_at}"
        if reset_at
        else "All accounts exhausted. No reset time known."
    )
    notify("Claude Swap — No Tokens Left", body, urgency="critical")


def notify_quarantined(number: str, email: str, reason: str) -> None:
    """An account was quarantined (dead token)."""
    notify(
        "Claude Account Quarantined",
        f"{email} removed from rotation.\nReason: {reason}\n"
        f"Run: cswap add --slot {number}  to recover.",
        urgency="normal",
    )
