"""Cross-platform system-tray app for claude-swap (``cswap tray``).

The Windows/Linux counterpart to the macOS menu bar (``cswap menubar``). Like
``menubar.py`` this is a *thin GUI shell* over ``ClaudeAccountSwitcher`` and the
core auto-switch engine — it never re-implements account, usage, or auto-switch
logic. Every string it shows comes from the pure, rumps-free display helpers in
``menubar.py`` (``format_title``, ``format_account_header``,
``account_detail_lines``, ``parse_switch_history`` …), which are reused verbatim,
and the settings/snapshot/engine wiring is identical to the menu bar's.

Structure mirrors ``menubar.py``: a top band of pure, import-safe helpers plus a
``_TrayController`` that holds all state and decisions (its GUI side effects —
notify / confirm / prompt / reveal / rebuild — are injected, so it is fully unit
testable without a display), and a lazily-imported ``pystray`` shell (``_TrayApp``
+ ``run()``) that supplies the real side effects and drives the event loop.

``pystray`` (Win32/AppIndicator/GTK/Xorg) and ``Pillow`` are an optional extra;
this module is import-safe without them (they are imported only inside ``run()``
and ``render_icon_image()``), so the CLI's ``from claude_swap.tray import run``
never fails for a missing extra — the failure surfaces from ``run()`` as a
``ClaudeSwitchError`` with an install hint, exactly like the menu bar.

Windows tray icons carry no always-visible text label (unlike the macOS
menu-bar title), so the live usage percentage appears in two places: rendered
into the tray-icon bitmap (``render_icon_image``) and mirrored in the hover
tooltip (``build_tooltip`` -> ``menubar.format_title``).
"""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass

from claude_swap import menubar
from claude_swap.exceptions import ClaudeSwitchError, CredentialReadError

# Draining-usage colour bands (remaining quota). Mirrors
# claude_swap.statusline.draining_usage_color — kept in sync there; duplicated
# here as a tiny pure helper so the tray icon renderer needs no import-time
# coupling to the statusline/printer module.
_BRICK, _GREEN, _ORANGE, _YELLOW, _RED = "a4343a", "3fb950", "e8890c", "e0c020", "d0322b"
_NEUTRAL = "6e7681"  # gray — usage unknown / no active account


def _draining_color_hex(remaining_pct: float) -> str:
    """Hex colour for *remaining* quota — greener with more left, red at the end."""
    if remaining_pct > 70:
        return _BRICK
    if remaining_pct > 50:
        return _GREEN
    if remaining_pct > 25:
        return _ORANGE
    if remaining_pct > 10:
        return _YELLOW
    return _RED


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    return int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)


# ---------------------------------------------------------------------------
# Pure menu model
# ---------------------------------------------------------------------------


@dataclass
class MenuNode:
    """A backend-agnostic description of one menu entry.

    ``action`` is a tuple ``(kind, *args)`` dispatched by ``_TrayController``
    (e.g. ``("switch_to", "3")``); ``None`` for a non-clickable row or a submenu
    parent. ``checked`` is ``None`` for a plain item, else the check/radio state.
    ``submenu`` (a list of ``MenuNode``) makes this a submenu parent.
    """

    label: str = ""
    action: tuple | None = None
    checked: bool | None = None
    radio: bool = False
    enabled: bool = True
    submenu: list["MenuNode"] | None = None
    separator: bool = False


_SEP = MenuNode(separator=True)

_ACCOUNT_PCT_LABELS = {
    "default": "Default (global)",
    "off": "None",
    "5h": "Session (5h)",
    "7d": "Weekly (7d)",
    "both": "Both (5h · 7d)",
}
_TITLE_PCT_LABELS = {
    "off": "None",
    "5h": "Session (5h)",
    "7d": "Weekly (7d)",
    "both": "Both (5h · 7d)",
}
_INTERVAL_LABELS = {30: "30 seconds", 60: "60 seconds", 300: "5 minutes"}


def _account_title_submenu(settings, email: str) -> MenuNode:
    current = settings.account_pct.get(email, "default")
    items = [
        MenuNode(
            label=_ACCOUNT_PCT_LABELS[choice],
            action=("account_pct", email, choice),
            radio=True,
            checked=(current == choice),
        )
        for choice in menubar.ACCOUNT_PCT_CHOICES
    ]
    return MenuNode(label="Show in menu-bar title", submenu=items)


def _account_threshold_submenu(email: str, per_account_thresholds: dict) -> MenuNode:
    current = per_account_thresholds.get(email)
    items = [
        MenuNode(
            label="Default (global)",
            action=("account_threshold", email, None),
            radio=True,
            checked=(current is None),
        )
    ]
    for pct in menubar.AUTO_THRESHOLD_CHOICES:
        items.append(
            MenuNode(
                label=f"{pct}%",
                action=("account_threshold", email, pct),
                radio=True,
                checked=(current == pct),
            )
        )
    return MenuNode(label="Auto-swap away at", submenu=items)


def _switch_submenu(switch_history: list[str]) -> MenuNode:
    if switch_history:
        hist = [MenuNode(label=line, enabled=False) for line in switch_history]
    else:
        hist = [MenuNode(label="No switches logged yet", enabled=False)]
    hist.append(_SEP)
    hist.append(MenuNode(label="Open full log…", action=("open_log",)))
    history_menu = MenuNode(label="Switch history", submenu=hist)
    items = [
        MenuNode(label="Rotate to next", action=("switch", "rotate")),
        MenuNode(label="Switch to best", action=("switch", "best")),
        MenuNode(label="Next available", action=("switch", "next-available")),
        _SEP,
        history_menu,
    ]
    return MenuNode(label="Switch", submenu=items)


def _manage_submenu(view: dict, has_add_token: bool) -> MenuNode:
    accounts = view.get("accounts") or []

    add_items = [MenuNode(label="From current login", action=("add", "login"))]
    if has_add_token:
        add_items.append(MenuNode(label="From setup-token…", action=("add", "token")))
    add = MenuNode(label="Add account", submenu=add_items)

    disable_items: list[MenuNode] = []
    if not accounts:
        disable_items.append(MenuNode(label="No managed accounts", enabled=False))
    for num, email, _ia, _disp, _lg, alias, disabled, _fa in accounts:
        name = f"{alias}  ({email})" if alias else email
        disable_items.append(
            MenuNode(
                label=f"{num}  {name}",
                action=("disable", str(num)),
                checked=bool(disabled),
            )
        )
    disable = MenuNode(label="Disable / enable account", submenu=disable_items)

    remove_items: list[MenuNode] = []
    if not accounts:
        remove_items.append(MenuNode(label="No managed accounts", enabled=False))
    for num, email, _ia, _disp, _lg, alias, _disabled, _fa in accounts:
        label = f"{num}  {alias}  ({email})" if alias else f"{num}  {email}"
        remove_items.append(MenuNode(label=label, action=("remove", str(num))))
    remove = MenuNode(label="Remove account", submenu=remove_items)

    items = [
        add,
        disable,
        remove,
        _SEP,
        MenuNode(label="Refresh credentials", action=("refresh", "creds")),
        MenuNode(label="Refresh usage now", action=("refresh", "now")),
    ]
    return MenuNode(label="Manage accounts", submenu=items)


def _settings_submenu(settings, global_threshold: int) -> MenuNode:
    items = [
        MenuNode(
            label="Show account name in menu bar",
            action=("toggle", "name"),
            checked=bool(settings.show_account_name),
        )
    ]

    tp_items = [
        MenuNode(
            label=_TITLE_PCT_LABELS[mode],
            action=("title_pct", mode),
            radio=True,
            checked=(settings.title_pct == mode),
        )
        for mode in menubar.TITLE_PCT_CHOICES
    ]
    items.append(MenuNode(label="Title percentage", submenu=tp_items))

    items.append(
        MenuNode(
            label="Show model limits in title",
            action=("toggle", "scoped"),
            checked=bool(settings.title_scoped),
        )
    )
    items.append(
        MenuNode(
            label="Battery gauge in title",
            action=("toggle", "battery"),
            checked=bool(settings.title_battery),
        )
    )
    items.append(
        MenuNode(
            label=f"Show swap icon ({menubar.ICON})",
            action=("toggle", "icon"),
            checked=bool(settings.show_icon),
        )
    )

    iv_items = [
        MenuNode(
            label=_INTERVAL_LABELS[secs],
            action=("interval", secs),
            radio=True,
            checked=(settings.refresh_interval == secs),
        )
        for secs in menubar.REFRESH_CHOICES
    ]
    items.append(MenuNode(label="Refresh interval", submenu=iv_items))

    items.append(
        MenuNode(
            label="Auto-switch accounts",
            action=("toggle", "auto"),
            checked=bool(settings.auto_switch_enabled),
        )
    )

    thr_items = [
        MenuNode(
            label=f"{pct}%",
            action=("set_threshold", pct),
            radio=True,
            checked=(global_threshold == pct),
        )
        for pct in menubar.AUTO_THRESHOLD_CHOICES
    ]
    items.append(MenuNode(label="Auto-switch threshold", submenu=thr_items))

    return MenuNode(label="Settings", submenu=items)


def build_menu_model(
    view: dict,
    settings,
    *,
    global_threshold: int,
    per_account_thresholds: dict,
    switch_history: list[str],
    has_add_token: bool,
) -> list[MenuNode]:
    """Build the tray's full menu as a tree of :class:`MenuNode` (pure).

    Mirrors ``menubar.MenuBarApp.rebuild_menu`` structure: one submenu per
    account (header parent + per-account actions/detail), then Switch / Manage
    accounts / Settings grouping submenus, then Quit.
    """
    nodes: list[MenuNode] = []
    accounts = view.get("accounts") or []
    for num, email, is_active, display, _last_good, alias, disabled, fetched_at in accounts:
        children = [
            MenuNode(label="Switch to this account", action=("switch_to", str(num))),
            _SEP,
        ]
        for line in menubar.account_detail_lines(num, email, display, fetched_at=fetched_at):
            children.append(MenuNode(label=line, enabled=False))
        children.append(_SEP)
        children.append(_account_title_submenu(settings, email))
        children.append(_account_threshold_submenu(email, per_account_thresholds))
        header = menubar.format_account_header(
            num, email, display, alias=alias, disabled=disabled, is_active=is_active
        )
        nodes.append(MenuNode(label=header, submenu=children))
    if not accounts:
        nodes.append(MenuNode(label="No managed accounts", enabled=False))

    nodes.append(_SEP)
    nodes.append(_switch_submenu(switch_history))
    nodes.append(_manage_submenu(view, has_add_token))
    nodes.append(_settings_submenu(settings, global_threshold))
    nodes.append(_SEP)
    nodes.append(MenuNode(label="Quit", action=("quit",)))
    return nodes


# ---------------------------------------------------------------------------
# Pure tooltip + icon rendering
# ---------------------------------------------------------------------------


def build_tooltip(view: dict, settings) -> str:
    """Tooltip string for the tray icon — the same text the macOS title shows."""
    active_email = view.get("active_email")
    return menubar.format_title(
        active_email,
        view.get("active_usage"),
        settings,
        alias=view.get("active_alias"),
        pct_override=menubar.account_title_pct(settings, active_email),
    )


def render_icon_image(util_pct: float | None, *, size: int = 64):
    """Draw a tray icon: the binding utilization ``%`` on a remaining-quota band.

    ``util_pct`` is utilization (0-100, higher = more used); ``None`` renders a
    neutral 'usage unknown' icon. Returns a ``PIL.Image`` (RGBA). Pillow is
    imported lazily so this module stays import-safe without the ``[tray]`` extra.
    """
    from PIL import Image, ImageDraw, ImageFont

    if util_pct is None:
        bg = _hex_to_rgb(_NEUTRAL)
        text = "–"
    else:
        util = max(0.0, min(100.0, float(util_pct)))
        bg = _hex_to_rgb(_draining_color_hex(100.0 - util))
        text = str(int(round(util)))

    img = Image.new("RGBA", (size, size), (*bg, 255))
    draw = ImageDraw.Draw(img)

    # Fit the text to the icon: try a TrueType font, fall back to the bitmap
    # default. Aim for ~60% of the icon height.
    font = None
    target = max(8, int(size * 0.6))
    for name in ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"):
        try:
            font = ImageFont.truetype(name, target)
            break
        except Exception:
            continue
    if font is None:
        try:
            font = ImageFont.load_default()
        except Exception:
            font = None

    try:
        if font is not None:
            bbox = draw.textbbox((0, 0), text, font=font)
        else:
            bbox = draw.textbbox((0, 0), text)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        pos = ((size - tw) / 2 - bbox[0], (size - th) / 2 - bbox[1])
        draw.text(pos, text, fill=(255, 255, 255, 255), font=font)
    except Exception:
        # Text is a nicety; a solid coloured badge still conveys the band.
        pass
    return img


def reveal_command(path) -> list[str]:
    """Command to reveal ``path`` in the OS file manager (platform-specific)."""
    p = str(path)
    if sys.platform == "win32":
        # explorer's /select needs the trailing comma form; the path selects the
        # file within its folder.
        return ["explorer", "/select,", p]
    if sys.platform == "darwin":
        return ["open", "-R", p]
    from pathlib import Path

    return ["xdg-open", str(Path(path).parent)]


# ---------------------------------------------------------------------------
# Controller (state + decisions; GUI side effects injected)
# ---------------------------------------------------------------------------


class _TrayController:
    """All tray state and actions, with GUI side effects injected.

    The pystray/tkinter shell supplies ``notify(title, message)``,
    ``confirm(message) -> bool``, ``prompt(message) -> str | None``,
    ``reveal(path)``, ``on_quit()`` and ``on_menu_changed()``; everything else
    here is platform-independent and unit-testable.
    """

    def __init__(self, switcher, *, notify, confirm, prompt, reveal, on_quit, on_menu_changed):
        self.switcher = switcher
        self.settings_path = switcher.backup_dir / "menubar_settings.json"
        self.log_path = switcher.backup_dir / "claude-swap.log"
        self.settings = menubar.MenuBarSettings.load(self.settings_path)
        self.view = dict(menubar.EMPTY_SNAPSHOT)
        self._snapshot_source = None
        self._refreshing = False
        self._dirty = False
        self._config_path = switcher._get_claude_config_path()
        self._config_mtime = 0.0
        self._last_usage_log: dict = {}
        self._engine = None
        self._engine_events: list = []
        self._event_lock = threading.Lock()
        self._notify = notify
        self._confirm = confirm
        self._prompt = prompt
        self._reveal = reveal
        self._on_quit = on_quit
        self._on_menu_changed = on_menu_changed

    # ---- menu model ------------------------------------------------------
    def build_model(self) -> list[MenuNode]:
        from claude_swap.settings import load_per_account_thresholds, load_settings

        try:
            global_threshold = int(load_settings(self.switcher.backup_dir).threshold)
        except Exception:
            global_threshold = 0
        try:
            per_account = load_per_account_thresholds(self.switcher.backup_dir)
        except Exception:
            per_account = {}
        try:
            history = menubar.parse_switch_history(self.log_path.read_text(encoding="utf-8"))
        except OSError:
            history = []
        return build_menu_model(
            self.view,
            self.settings,
            global_threshold=global_threshold,
            per_account_thresholds=per_account,
            switch_history=history,
            has_add_token=hasattr(self.switcher, "add_account_from_token"),
        )

    def active_icon_pct(self) -> float | None:
        """Binding utilization % of the active account (for the icon), or None."""
        usage = self.view.get("active_usage")
        if not isinstance(usage, dict):
            return None
        return menubar._binding_pct(usage, time.time())

    # ---- dispatch --------------------------------------------------------
    def dispatch(self, action: tuple) -> None:
        if not action:
            return
        kind = action[0]
        if kind == "switch":
            strategy = {"rotate": None, "best": "best", "next-available": "next-available"}.get(
                action[1], None
            )
            if self._guard(lambda: self.switcher.switch(strategy=strategy)):
                self._notify_switched()
                self._request_refresh()
        elif kind == "switch_to":
            if self._guard(lambda: self.switcher.switch_to(str(action[1]))):
                self._notify_switched()
                self._request_refresh()
        elif kind == "remove":
            num = action[1]
            if self._confirm(f"Remove account {num}?"):
                if self._guard(
                    lambda: self.switcher.remove_account(str(num), assume_yes=True)
                ):
                    self._request_refresh()
        elif kind == "disable":
            num = action[1]
            target = not self._is_disabled(num)
            if self._guard(lambda: self.switcher.set_account_disabled(str(num), target)):
                self._request_refresh()
        elif kind == "add":
            if action[1] == "login":
                if self._guard(self.switcher.add_account):
                    self._request_refresh()
            else:
                self._add_from_token()
        elif kind == "refresh":
            if action[1] == "now":
                self._request_refresh(full=True)
            else:
                self._refresh_creds()
        elif kind == "open_log":
            target = self.log_path if self.log_path.exists() else self.log_path.parent
            self._reveal(target)
        elif kind == "toggle":
            self._toggle(action[1])
        elif kind == "title_pct":
            self.settings.title_pct = action[1]
            self._save_and_rebuild()
        elif kind == "account_pct":
            _, email, choice = action
            if choice == "default":
                self.settings.account_pct.pop(email, None)
            else:
                self.settings.account_pct[email] = choice
            self._save_and_rebuild()
        elif kind == "interval":
            self.settings.refresh_interval = action[1]
            self._apply_interval(action[1])
            self._save_and_rebuild()
        elif kind == "set_threshold":
            self._set_global_threshold(action[1])
        elif kind == "account_threshold":
            _, email, pct = action
            self._set_account_threshold(email, pct)
        elif kind == "quit":
            self._stop_engine()
            self._on_quit()

    # ---- dispatch helpers ------------------------------------------------
    def _toggle(self, name: str) -> None:
        field = {
            "name": "show_account_name",
            "scoped": "title_scoped",
            "battery": "title_battery",
            "icon": "show_icon",
        }.get(name)
        if field is not None:
            setattr(self.settings, field, not getattr(self.settings, field))
            self._save_and_rebuild()
        elif name == "auto":
            self.settings.auto_switch_enabled = not self.settings.auto_switch_enabled
            self.settings.save(self.settings_path)
            if self.settings.auto_switch_enabled:
                self._start_engine()
            else:
                self._stop_engine()
            self._on_menu_changed()

    def _add_from_token(self) -> None:
        email = self._prompt("Email for this token:")
        if not email or not email.strip():
            return
        token = self._prompt("Setup token (sk-ant-oat01-…):")
        if not token or not token.strip():
            return
        if self._guard(
            lambda: self.switcher.add_account_from_token(
                token=token.strip(), email=email.strip(), slot=None
            )
        ):
            self._request_refresh()

    def _refresh_creds(self) -> None:
        if self.switcher._get_current_account() is None:
            self._notify("claude-swap", "No active Claude Code login detected. Log in first.")
            return
        try:
            self.switcher.add_account(slot=None)
        except CredentialReadError:
            self._notify(
                "claude-swap",
                "Couldn't read the active credential. If the tray is running as a "
                "background/service session, the OS credential store may block access — "
                "quit and relaunch it from your own terminal with: cswap tray",
            )
            return
        except ClaudeSwitchError as e:
            self._notify("claude-swap", str(e))
            return
        self._request_refresh()

    def _set_global_threshold(self, pct: int) -> None:
        from claude_swap.settings import set_setting

        try:
            set_setting(self.switcher.backup_dir, "autoswitch.threshold", str(pct))
        except Exception as e:
            self._notify("claude-swap", f"Couldn't set threshold: {e}")
            return
        self._restart_engine()
        self._on_menu_changed()

    def _set_account_threshold(self, email: str, pct) -> None:
        from claude_swap.settings import set_per_account_threshold, unset_per_account_threshold

        try:
            if pct is None:
                unset_per_account_threshold(self.switcher.backup_dir, email)
            else:
                set_per_account_threshold(self.switcher.backup_dir, email, pct)
        except Exception as e:
            self._notify("claude-swap", f"Couldn't set limit: {e}")
            return
        self._restart_engine()
        self._on_menu_changed()

    def _is_disabled(self, num) -> bool:
        for row in self.view.get("accounts", []):
            if str(row[0]) == str(num):
                return bool(row[6])
        return False

    def _guard(self, fn) -> bool:
        try:
            fn()
            return True
        except ClaudeSwitchError as e:
            self._notify("claude-swap", str(e))
            return False

    def _notify_switched(self) -> None:
        self._notify(
            "Account switched",
            "Switch takes effect within ~30s — restart Claude Code to apply immediately.",
        )

    def _save_and_rebuild(self) -> None:
        self.settings.save(self.settings_path)
        self._on_menu_changed()

    def _apply_interval(self, secs: int) -> None:
        """Hook for the GUI shell to re-time its refresh loop; base is a no-op."""

    # ---- display refresh (background worker) -----------------------------
    def _request_refresh(self, full: bool = False) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        threading.Thread(target=self._worker, args=(full,), daemon=True).start()

    def _worker(self, full: bool) -> None:
        try:
            try:
                if self._snapshot_source is None:
                    from claude_swap.snapshot_source import SnapshotSource

                    self._snapshot_source = SnapshotSource(self.switcher)
                raw = self._snapshot_source.take(
                    full=full, store_only=self._engine is not None
                )
            except Exception:
                self.switcher._logger.debug("tray snapshot failed", exc_info=True)
                return
            snap = menubar._adapt_snapshot(raw)
            self._log_usage(snap)
            self.view = snap
            self._dirty = True  # applied by the app's sync loop
        finally:
            self._refreshing = False

    def _log_usage(self, snap: dict) -> None:
        for num, email, _ia, _disp, last_good, _alias, _disabled, _fa in snap["accounts"]:
            key = menubar._usage_log_key(last_good)
            if key == (None, None) or self._last_usage_log.get(num) == key:
                continue
            line = menubar.format_usage_log(email, last_good)
            if line:
                self.switcher._logger.info(line)
                self._last_usage_log[num] = key

    def _detect_active_change(self) -> None:
        if self._refreshing:
            return
        try:
            mtime = self._config_path.stat().st_mtime
        except OSError:
            return
        if mtime == self._config_mtime:
            return
        self._config_mtime = mtime
        current = self.switcher._get_current_account()
        email = current[0] if current else None
        if email and email != self.view.get("active_email"):
            self._request_refresh()

    def apply_pending(self) -> None:
        """Called on the app's ~1s tick: apply a fresh snapshot + engine events."""
        if self._dirty:
            self._dirty = False
            self._on_menu_changed()
        self._detect_active_change()
        self._drain_engine_events()

    # ---- auto-switch engine ---------------------------------------------
    def _start_engine(self) -> None:
        if self._engine is not None:
            return
        from claude_swap.autoswitch import AutoSwitchEngine
        from claude_swap.settings import load_settings

        try:
            engine = AutoSwitchEngine(
                self.switcher,
                load_settings(self.switcher.backup_dir),
                self._on_engine_event,
                dry_run=False,
            )
        except Exception as e:
            self.switcher._logger.warning("auto-switch engine failed to start: %s", e)
            self._notify("Auto-switch failed to start", str(e))
            return
        self._engine = engine
        threading.Thread(target=self._run_engine, args=(engine,), daemon=True).start()

    def _run_engine(self, engine) -> None:
        try:
            engine.run_loop()
        except Exception:
            self.switcher._logger.debug("auto-switch engine crashed", exc_info=True)

    def _stop_engine(self) -> None:
        if self._engine is not None:
            self._engine.stop()
            self._engine = None

    def _restart_engine(self) -> None:
        if self._engine is not None:
            self._stop_engine()
            self._start_engine()

    def _on_engine_event(self, event) -> None:
        with self._event_lock:
            self._engine_events.append(event)

    def _drain_engine_events(self) -> None:
        with self._event_lock:
            events, self._engine_events = self._engine_events, []
        for ev in events:
            if ev.kind == "switch" and not getattr(ev, "dry_run", False):
                self._notify("Auto-switched account", ev.human())
                self._request_refresh()
            elif ev.kind == "account-quarantined":
                self._notify("Account quarantined", ev.human())
            elif ev.kind == "all-exhausted":
                self._notify("All accounts exhausted", ev.human())
            elif ev.kind == "config-warning":
                self._notify("Configuration warning", ev.human())


# ---------------------------------------------------------------------------
# tkinter dialogs (glue)
# ---------------------------------------------------------------------------


def _tk_confirm(message: str) -> bool:
    try:
        import tkinter as tk
        from tkinter import messagebox
    except Exception:
        return False
    root = tk.Tk()
    root.withdraw()
    try:
        return bool(messagebox.askokcancel("claude-swap", message))
    finally:
        try:
            root.destroy()
        except Exception:
            pass


def _tk_prompt(message: str) -> str | None:
    try:
        import tkinter as tk
        from tkinter import simpledialog
    except Exception:
        return None
    root = tk.Tk()
    root.withdraw()
    try:
        return simpledialog.askstring("claude-swap", message)
    finally:
        try:
            root.destroy()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# pystray shell (glue)
# ---------------------------------------------------------------------------


class _TrayApp:
    def __init__(self, switcher, pystray):
        self._pystray = pystray
        self._stop = threading.Event()
        self.controller = _TrayController(
            switcher,
            notify=self._notify,
            confirm=self._confirm,
            prompt=self._prompt,
            reveal=self._reveal,
            on_quit=self._quit,
            on_menu_changed=self._rebuild,
        )
        self.icon = pystray.Icon(
            "claude-swap",
            icon=render_icon_image(self.controller.active_icon_pct()),
            title=build_tooltip(self.controller.view, self.controller.settings),
            menu=self._build_menu(),
        )

    # ---- menu wiring -----------------------------------------------------
    def _build_menu(self):
        model = self.controller.build_model()
        return self._pystray.Menu(*[self._to_item(n) for n in model])

    def _to_item(self, node: MenuNode):
        p = self._pystray
        if node.separator:
            return p.Menu.SEPARATOR
        if node.submenu is not None:
            return p.MenuItem(node.label, p.Menu(*[self._to_item(c) for c in node.submenu]))
        checked = None
        if node.checked is not None:
            checked = (lambda n: (lambda _item: n.checked))(node)
        action = None
        if node.action is not None:
            action = (lambda a: (lambda _icon, _item: self.controller.dispatch(a)))(node.action)
        try:
            return p.MenuItem(
                node.label, action, checked=checked, radio=node.radio, enabled=node.enabled
            )
        except TypeError:
            # Older pystray without an ``enabled`` kwarg: a None action already
            # renders the row as non-actionable.
            return p.MenuItem(node.label, action, checked=checked, radio=node.radio)

    def _rebuild(self):
        try:
            self.icon.menu = self._build_menu()
            self.icon.title = build_tooltip(self.controller.view, self.controller.settings)
            self.icon.icon = render_icon_image(self.controller.active_icon_pct())
            self.icon.update_menu()
        except Exception:
            pass

    # ---- injected side effects ------------------------------------------
    def _notify(self, title, message=None):
        try:
            self.icon.notify(message or "", title)
        except Exception:
            pass

    def _confirm(self, message):
        return _tk_confirm(message)

    def _prompt(self, message):
        return _tk_prompt(message)

    def _reveal(self, path):
        import subprocess

        try:
            subprocess.run(reveal_command(path), check=False)
        except Exception:
            pass

    def _quit(self):
        self._stop.set()
        try:
            self.icon.stop()
        except Exception:
            pass

    # ---- run loop --------------------------------------------------------
    def _loop(self):
        last = 0.0
        while not self._stop.is_set():
            now = time.time()
            if now - last >= self.controller.settings.refresh_interval:
                last = now
                self.controller._request_refresh()
            self.controller.apply_pending()
            self._stop.wait(1.0)

    def run(self) -> int:
        def setup(icon):
            icon.visible = True
            self.controller._request_refresh()
            if self.controller.settings.auto_switch_enabled:
                self.controller._start_engine()
            threading.Thread(target=self._loop, daemon=True).start()

        self.icon.run(setup=setup)
        return 0


def run(switcher) -> int:
    """Entry point for ``cswap tray``. Blocks until the user quits."""
    try:
        import pystray  # lazy: optional dependency, imported only when launching
        from PIL import Image  # noqa: F401 — ensure Pillow is present for the icon
    except ImportError as e:
        # Import-safe without the extra by design, so the CLI's guard around
        # ``from claude_swap.tray import run`` never sees a missing extra — the
        # failure lands here at call time. Raise the type the CLI renders cleanly.
        raise ClaudeSwitchError(
            "System tray mode requires 'pystray' and 'Pillow'. "
            "Install with: pip install 'claude-swap[tray]'"
        ) from e

    return _TrayApp(switcher, pystray).run()
