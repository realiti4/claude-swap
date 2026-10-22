"""Desktop window for claude-swap (``cswap-desktop``).

A thin GUI shell over ``ClaudeAccountSwitcher`` and the core auto-switch engine
(``claude_swap.autoswitch``) — it never re-implements account, usage, or
auto-switch logic. Usage for display comes from ``switcher.accounts_snapshot()``
and is formatted with the TUI's shared helpers (``claude_swap.tui.data``).

The window is an HTML page (``claude_swap.desktop_page``) in a native webview
(``pywebview``, the optional ``desktop`` extra); ``DesktopApi`` is the bridge it
calls. Everything here is import-safe without pywebview so it can be unit-tested
headless; ``webview`` is imported lazily inside ``run``.
"""

from __future__ import annotations

import io
import queue
import re
import sys
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.models import AccountSnapshot
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.tui.data import (
    clock_stamp,
    format_age,
    sentinel_label,
    window_pct,
    window_reset_text,
)

_TITLE_MAX = 32
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_WINDOWS = (("5h", "five_hour"), ("7d", "seven_day"))

ALERT_KINDS = frozenset(
    {"all-exhausted", "account-quarantined", "config-warning", "error"}
)


@dataclass(frozen=True)
class WindowRow:
    label: str
    pct: float | None
    reset: str | None
    age: str | None


@dataclass(frozen=True)
class CardView:
    title: str
    tag: str
    active: bool
    disabled: bool
    switchable: bool
    rows: tuple[WindowRow, ...]
    message: str | None


def card_lines(acc: AccountSnapshot, now: float) -> CardView:
    """Everything one account card shows, as plain data."""
    title = f"#{acc.number}  {acc.alias or acc.email}"
    if len(title) > _TITLE_MAX:
        title = title[: _TITLE_MAX - 1] + "…"
    rows: tuple[WindowRow, ...] = ()
    message = None
    usage = acc.usage
    if usage.sentinel:
        message = sentinel_label(usage.sentinel)
    elif not usage.last_good:
        message = "no usage yet"
    else:
        age = format_age(usage.age_s)
        rows = tuple(
            WindowRow(
                label,
                window_pct(usage.last_good, key),
                window_reset_text(usage.last_good, key, now),
                age,
            )
            for label, key in _WINDOWS
        )
    return CardView(
        title=title,
        tag=acc.display_tag,
        active=acc.is_active,
        disabled=acc.disabled,
        switchable=acc.switchable,
        rows=rows,
        message=message,
    )


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def event_line(ev) -> tuple[str, bool]:
    """Event-log text and whether it deserves alert styling."""
    return f"{clock_stamp()} {ev.human()}", ev.kind in ALERT_KINDS


BG, FG = "#1b1b1f", "#f2f0e3"  # page palette lives in desktop_page.py; these tint the title bar


def colorref(hex_color: str) -> int:
    """'#rrggbb' -> Win32 COLORREF (0x00bbggrr)."""
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return b << 16 | g << 8 | r


def tint_title_bar(title: str) -> None:
    """Paint the Windows title bar of the window called ``title`` to match the page."""
    if sys.platform != "win32":
        return
    import ctypes

    hwnd = ctypes.windll.user32.FindWindowW(None, title)
    if not hwnd:
        return
    dwm = ctypes.windll.dwmapi.DwmSetWindowAttribute
    on = ctypes.c_int(1)
    dwm(hwnd, 20, ctypes.byref(on), ctypes.sizeof(on))  # DWMWA_USE_IMMERSIVE_DARK_MODE (Win10 fallback)
    for attr, color in ((35, BG), (36, FG)):  # DWMWA_CAPTION_COLOR / TEXT_COLOR, Win11 only
        ref = ctypes.c_int(colorref(color))
        dwm(hwnd, attr, ctypes.byref(ref), ctypes.sizeof(ref))


def card_dict(acc: AccountSnapshot, now: float) -> dict:
    """One account card as JSON-ready data for the page."""
    v = card_lines(acc, now)
    d = asdict(v)
    d["rows"] = list(d["rows"])
    d["num"] = acc.number
    return d


class _EngineCrash:
    """Minimal event stand-in so a crashed engine thread shows up in the log."""

    kind = "error"

    def __init__(self, msg: str):
        self._msg = msg

    def human(self) -> str:
        return f"auto-switch engine crashed: {self._msg}"


class DesktopApi:
    """What the page calls through ``pywebview.api``.

    pywebview runs every call on its own thread, so blocking work is fine here.
    One switcher action runs at a time (``_action`` lock); snapshots are
    serialized by ``_snap`` so two refreshes never overlap.
    """

    def __init__(self, switcher, source=None, engine_factory=None):
        from claude_swap.snapshot_source import SnapshotSource

        self._switcher = switcher
        self._source = source or SnapshotSource(switcher)
        self._engine_factory = engine_factory or _default_engine
        self._action = threading.Lock()
        self._snap = threading.Lock()
        self._events: queue.Queue = queue.Queue()
        self._engine = None
        self._engine_thread: threading.Thread | None = None
        self._refresh_due = False
        self.closing = False
        self._close_announced = False
        self.on_idle = None  # set by run(): finishes a close deferred by a running action
        self.pick_file = None  # set by run(): ("save"|"open") -> path or None
        self.notifier = None  # set by run(): desktop_notify.Notifier

    # ---- reads ---------------------------------------------------------
    def snapshot(self) -> dict:
        with self._snap:
            try:
                snap = self._source.take()
            except Exception as e:
                return {"error": f"{type(e).__name__}: {e}"}
        return {"accounts": [card_dict(a, snap.taken_at) for a in snap.accounts]}

    def poll(self) -> dict:
        """Engine events since last poll, plus refresh/close/auto state for the page."""
        lines = []
        while True:
            try:
                ev = self._events.get_nowait()
            except queue.Empty:
                break
            text, alert = event_line(ev)
            lines.append({"text": text, "alert": alert})
            if ev.kind == "switch" and not getattr(ev, "dry_run", False):
                self._refresh_due = True
        if self.closing and not self._close_announced:
            self._close_announced = True
            lines.append({"text": "Waiting for the current action to finish…", "alert": True})
        refresh, self._refresh_due = self._refresh_due, False
        return {"lines": lines, "refresh": refresh, "auto": self._engine is not None}

    def busy(self) -> bool:
        return self._action.locked()

    # ---- actions -------------------------------------------------------
    def _act(self, fn) -> dict:
        from claude_swap.tui.data import run_action

        if not self._action.acquire(blocking=False):
            return {"ok": False, "output": "Another action is still running."}
        try:
            res = run_action(fn)
            return {"ok": res.ok, "output": strip_ansi(res.output).strip()}
        except Exception as e:
            return {"ok": False, "output": f"{type(e).__name__}: {e}"}
        finally:
            self._action.release()
            if self.closing and self.on_idle is not None:
                self.on_idle()

    def switch(self, num) -> dict:
        return self._act(lambda: self._switcher.switch_to(str(num)))

    def best(self) -> dict:
        return self._act(lambda: self._switcher.switch(strategy="best"))

    def add_current(self) -> dict:
        return self._act(lambda: self._switcher.add_account(assume_yes=True))

    def add_token(self, email, token) -> dict:
        email, token = str(email or "").strip(), str(token or "").strip()
        if not email or not token:
            return {"ok": False, "output": "Email and token are both required."}
        return self._act(lambda: self._switcher.add_account_from_token(token=token, email=email))

    def set_disabled(self, num, disabled) -> dict:
        return self._act(lambda: self._switcher.set_account_disabled(str(num), bool(disabled)))

    def export(self) -> dict:
        """Save every account to a file the user picks (plain-text credentials)."""
        path = self.pick_file("save") if self.pick_file else None
        if not path:
            return {"ok": True, "output": ""}  # cancelled

        def do():
            from claude_swap.transfer import export_accounts

            export_accounts(self._switcher, path)

        return self._act(do)

    def import_(self, replace=False) -> dict:
        """Import accounts from a file the user picks; ``replace`` overwrites matches."""
        path = self.pick_file("open") if self.pick_file else None
        if not path:
            return {"ok": True, "output": ""}  # cancelled

        def do():
            from claude_swap.transfer import import_accounts

            import_accounts(self._switcher, path, force=bool(replace))

        return self._act(do)

    def remove(self, num) -> dict:
        # The page asks for confirmation before calling this.
        return self._act(lambda: self._switcher.remove_account(str(num), assume_yes=True))

    # ---- auto-switch engine (mirrors menubar.py) -----------------------
    def auto(self, on) -> dict:
        if not on:
            self._stop_engine()
            return {"on": False}
        if self._engine is None:
            try:
                engine = self._engine_factory(self._switcher, self._on_event)
            except Exception as e:
                return {"on": False, "output": f"auto-switch failed to start: {e}"}
            self._engine = engine
            self._engine_thread = threading.Thread(target=self._run_engine, args=(engine,), daemon=True)
            self._engine_thread.start()
        return {"on": True}

    def _run_engine(self, engine):
        try:
            engine.run_loop()
        except Exception as e:
            self._on_event(_EngineCrash(str(e)))
            if self._engine is engine:
                self._engine = None

    def _on_event(self, ev):
        # Engine thread: queue for the page, and notify straight away (the page's
        # timers are throttled while the window is in the background).
        self._events.put(ev)
        if self.notifier is not None:
            self.notifier.handle(ev)

    def _stop_engine(self):
        if self._engine is not None:
            self._engine.stop()
            self._engine = None

    def shutdown(self):
        self._stop_engine()
        if self._engine_thread is not None:
            self._engine_thread.join(timeout=5)  # may be mid-switch


def _make_notifier(switcher):
    from claude_swap import desktop_notify

    if sys.platform == "win32":
        import ctypes

        def focused():
            user32 = ctypes.windll.user32
            return bool(user32.GetForegroundWindow()) and user32.GetForegroundWindow() == user32.FindWindowW(None, TITLE)
        try:
            # A stable copy of the icon: the onefile exe's own files vanish when it exits.
            icon = Path(switcher.backup_dir) / "cswap.ico"
            src = Path(__file__).with_name("cswap.ico")
            if src.is_file() and not icon.is_file():
                icon.write_bytes(src.read_bytes())
            desktop_notify.register_app_id(str(icon) if icon.is_file() else None)
        except Exception:
            pass  # toasts still show, just without our name/icon
    else:
        def focused():
            return False
    return desktop_notify.Notifier(is_focused=focused)


def _default_engine(switcher, on_event):
    from claude_swap.autoswitch import AutoSwitchEngine
    from claude_swap.settings import load_settings

    return AutoSwitchEngine(switcher, load_settings(switcher.backup_dir), on_event, dry_run=False)


TITLE = "cswap"


def run(switcher) -> int:
    """Open the window; returns 0 when it closes, 1 if pywebview is missing."""
    try:
        import webview
    except ImportError:
        print("cswap desktop needs pywebview: pip install 'claude-swap[desktop]'", file=sys.stderr)
        return 1
    from claude_swap.desktop_page import PAGE

    api = DesktopApi(switcher)
    window = webview.create_window(
        TITLE, html=PAGE, js_api=api, width=560, height=720, min_size=(440, 420),
        background_color=BG,
    )

    def on_closing():
        if api.busy():  # don't kill a credential write mid-way; the page shows why
            api.closing = True
            api.on_idle = window.destroy
            return False
        api.shutdown()
        return None

    def pick_file(kind):
        types = ("cswap export (*.cswap;*.json)", "All files (*.*)")
        if kind == "save":
            got = window.create_file_dialog(
                webview.FileDialog.SAVE, save_filename="accounts.cswap", file_types=types)
        else:
            got = window.create_file_dialog(webview.FileDialog.OPEN, file_types=types)
        if not got:
            return None
        return got if isinstance(got, str) else got[0]

    api.pick_file = pick_file
    api.notifier = _make_notifier(switcher)
    window.events.closing += on_closing
    window.events.shown += lambda: tint_title_bar(TITLE)
    # The packaged exe has no data files; there pywebview falls back to the exe's own icon.
    icon = Path(__file__).with_name("cswap.ico")
    webview.start(icon=str(icon) if icon.is_file() else None)
    return 0


def _show_error(text: str) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox
    except ImportError:
        print(text, file=sys.stderr)
        return
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror("cswap desktop", text)
    root.destroy()


def main() -> int:
    # --windowed exes have no console: sys.stdout/stderr are None.
    if sys.stdout is None:
        sys.stdout = io.StringIO()
    if sys.stderr is None:
        sys.stderr = io.StringIO()
    try:
        return run(ClaudeAccountSwitcher())
    except ClaudeSwitchError as e:
        _show_error(str(e))
    except Exception as e:  # windowed exe has no console: surface everything
        _show_error(f"{type(e).__name__}: {e}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
