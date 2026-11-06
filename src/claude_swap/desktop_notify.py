"""Desktop notifications for the desktop window's auto-switch events.

Only events that happen unattended and need the user's attention notify:
switches, quarantines (and recoveries), every account exhausted, and the
engine stopping or ignoring a setting. Polls, sleeps, "no switch" and errors
the engine will retry never do. Delivery uses what the OS already has —
a WinRT toast via PowerShell on Windows, ``osascript`` on macOS,
``notify-send`` on Linux — and a failure to notify is never an error.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from xml.sax.saxutils import escape

APP_ID = "cswap.desktop"  # Windows AppUserModelID the toasts are attributed to
REPEAT_WINDOW_S = 3600  # the same alert at most once an hour

_WHY = {
    "at-limit": "hit its limit",
    "proactive": "was running low",
    "failover": "stopped working",
    "consume-first": "using the window that resets soonest first",
}


def local_time(iso: str | None) -> str | None:
    """'2026-10-12T09:00:00Z' -> '14:30' (local), or 'Oct 13 14:30' if not today."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None
    return dt.strftime("%H:%M" if dt.date() == datetime.now().astimezone().date() else "%b %d %H:%M")


def notification_for(ev) -> tuple[str, str, str] | None:
    """(dedupe key, title, body) for an event worth a notification, else None."""
    kind = getattr(ev, "kind", "")
    if kind == "switch":
        if getattr(ev, "dry_run", False) or not ev.to_ref:
            return None
        to = ev.to_ref.get("email") or f"Account-{ev.to_ref.get('number')}"
        why = _WHY.get(ev.trigger, ev.trigger)
        if ev.from_ref:
            src = ev.from_ref.get("email") or f"Account-{ev.from_ref.get('number')}"
            body = f"{src} {why}." if ev.trigger != "consume-first" else f"From {src}: {why}."
        else:
            body = "Auto-switch picked this account."
        return f"switch:{to}", f"Switched to {to}", body
    if kind == "account-quarantined":
        return (
            f"quarantine:{ev.number}",
            "Account needs re-login",
            f"{ev.email} was taken out of rotation ({ev.reason}). "
            f"Log in with it, then run: cswap add --slot {ev.number}",
        )
    if kind == "account-unquarantined":
        return f"unquarantine:{ev.number}", "Account back in rotation", ev.email
    if kind == "all-exhausted":
        at = local_time(getattr(ev, "earliest_reset_at", None))
        body = f"Earliest reset at {at}." if at else "No reset time is known yet."
        return f"exhausted:{at}", "All accounts are at their limit", body
    if kind == "config-warning":
        return f"config:{ev.message}", "Auto-switch setting ignored", ev.message
    if kind == "error":
        if getattr(ev, "transient", False):
            return None  # the engine retries on its own
        msg = getattr(ev, "message", None) or ev.human()
        return f"error:{msg}", "Auto-switch stopped", msg
    return None


class Notifier:
    """Decides whether an event notifies, and sends it off the caller's thread."""

    def __init__(self, send=None, is_focused=lambda: False, clock=time.monotonic):
        self._send = send or send_notification
        self.is_focused = is_focused
        self._clock = clock
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def handle(self, ev) -> bool:
        """Notify for ``ev`` if appropriate; True when a notification was sent."""
        note = notification_for(ev)
        if note is None:
            return False
        key, title, body = note
        try:
            if self.is_focused():  # the open window's output panel already shows it
                return False
        except Exception:
            pass
        now = self._clock()
        with self._lock:
            last = self._last.get(key)
            if last is not None and now - last < REPEAT_WINDOW_S:
                return False
            self._last[key] = now
        threading.Thread(target=self._safe_send, args=(title, body), daemon=True).start()
        return True

    def _safe_send(self, title, body):
        try:
            self._send(title, body)
        except Exception:
            pass  # notifications are best-effort


# ---- delivery -------------------------------------------------------------

_PS_TOAST = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime] | Out-Null
$x = New-Object Windows.Data.Xml.Dom.XmlDocument
$x.LoadXml($env:CSWAP_TOAST_XML)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:CSWAP_TOAST_APP).Show(
  [Windows.UI.Notifications.ToastNotification]::new($x))
"""


def toast_xml(title: str, body: str) -> str:
    return (
        "<toast><visual><binding template=\"ToastGeneric\">"
        f"<text>{escape(title)}</text><text>{escape(body)}</text>"
        "</binding></visual></toast>"
    )


def register_app_id(icon_path: str | None) -> None:
    """Make Windows show toasts as "cswap" (with our icon) instead of an unknown app."""
    if sys.platform != "win32":
        return
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\AppUserModelId\{APP_ID}") as k:
        winreg.SetValueEx(k, "DisplayName", 0, winreg.REG_SZ, "cswap")
        if icon_path:
            winreg.SetValueEx(k, "IconUri", 0, winreg.REG_SZ, icon_path)


def send_notification(title: str, body: str) -> None:
    if sys.platform == "win32":
        env = {**os.environ, "CSWAP_TOAST_XML": toast_xml(title, body), "CSWAP_TOAST_APP": APP_ID}
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_TOAST],
            env=env, timeout=20, capture_output=True,
            creationflags=0x08000000,  # CREATE_NO_WINDOW: no console flash from the windowed exe
        )
    elif sys.platform == "darwin":
        script = f"display notification {_osa(body)} with title {_osa(title)}"
        subprocess.run(["osascript", "-e", script], timeout=20, capture_output=True)
    elif shutil.which("notify-send"):
        subprocess.run(["notify-send", "--app-name=cswap", title, body], timeout=20, capture_output=True)


def _osa(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
