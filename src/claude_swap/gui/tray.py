"""System tray: keep running in the background, switch accounts from the menu.

No extra dependency: Windows reuses the .NET WinForms that pywebview already
loads (``NotifyIcon``), macOS the pyobjc it already loads (``NSStatusItem``).
The menu is rebuilt each time it opens, from the same account data the page
shows, so it is never stale. Menu actions run off the UI thread; they are the
page's own actions, so a tray switch and a page switch cannot overlap.

A tray that cannot start is not fatal: closing the window then simply quits.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import traceback
from dataclasses import dataclass
from importlib import resources
from typing import Callable


@dataclass
class Item:
    label: str
    action: Callable[[], None] | None = None
    checked: bool = False
    enabled: bool = True
    submenu: list | None = None


SEP = Item("-")

_ICON_FILE = {"win32": "tray.ico", "darwin": "tray.png"}


def _icon_path() -> str:
    return str(resources.files("claude_swap.gui").joinpath("web", _ICON_FILE[sys.platform]))


class Tray:
    """Platform-neutral part: the menu and what its items do."""

    def __init__(self, api, show: Callable[[], None], quit: Callable[[], None]) -> None:
        self._api = api
        self._show = show
        self._quit = quit
        self.hinted = False

    # -- the menu ----------------------------------------------------------------

    def model(self) -> list[Item]:
        """``Desktop：<current>`` and ``CLI：<current>``, each opening onto its
        accounts, then the window and quit."""
        items = []
        desk = self._api._tray_desktop_accounts()
        if desk is not None:
            rows = [
                Item(a["label"], lambda u=a["uuid"], lbl=a["label"]: self._switch_desktop(u, lbl),
                     checked=a["live"], enabled=a["switchable"])
                for a in desk
            ]
            live = next((a["label"] for a in desk if a["live"]), None)
            unmanaged = None if live else self._api._tray_desktop_live()
            if unmanaged:
                live = f"{unmanaged['label']}（未添加）"
                rows += [SEP] if rows else []
                rows.append(Item("添加当前登录", self._import_desktop))
            items.append(Item(f"Desktop：{live or '未登录'}", submenu=rows or [Item("（还没有账号）", enabled=False)]))
        cli_accounts = self._api._tray_cli_accounts()
        cli = [
            Item(a["label"], lambda n=a["number"], lbl=a["label"]: self._switch_cli(n, lbl),
                 checked=a["active"], enabled=not a["disabled"] and not a["active"])
            for a in cli_accounts
        ]
        cli = cli or [Item("（还没有账号）", enabled=False)]
        cli += [SEP, Item("智能切换（剩余额度最多）", self._switch_best)]
        active = next((a["label"] for a in cli_accounts if a["active"]), "未登录")
        items.append(Item(f"CLI：{active}", submenu=cli))
        items += [SEP, Item("打开主窗口", self._show), Item("退出", self._quit)]
        return items

    def _switch_cli(self, number: str, label: str) -> None:
        result = self._api.switch_to(number)
        self._report(result, f"CLI 已切换到 {label}")

    def _switch_best(self) -> None:
        result = self._api.switch_strategy("best")
        self._report(result, result.get("output") or "已切换")

    def _switch_desktop(self, uuid: str, label: str) -> None:
        if not self.confirm("切换 Claude Desktop 账号", f"将强制重启 Claude Desktop 并切换到 {label}。\nDesktop 中未发送的内容会丢失。"):
            return
        result = self._api.desktop_switch(uuid)
        self._report(result, f"Desktop 已切换到 {label}")

    def _import_desktop(self) -> None:
        result = self._api.desktop_import_current()
        self._report(result, f"已添加 Desktop 账号 {result.get('email') or ''}".strip())

    def _report(self, result: dict, ok_text: str) -> None:
        if result.get("ok"):
            self.notify("claude-swap", ok_text)
        else:
            self.notify("切换失败", (result.get("output") or "未知错误")[-200:])

    def run_action(self, action: Callable[[], None]) -> None:
        def go():
            try:
                action()
            except Exception:
                traceback.print_exc()

        threading.Thread(target=go, daemon=True).start()

    # -- per platform ----------------------------------------------------------------

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def notify(self, title: str, text: str) -> None: ...
    def confirm(self, title: str, text: str) -> bool: ...


class _WinTray(Tray):
    def start(self) -> None:
        import clr

        clr.AddReference("System.Windows.Forms")
        clr.AddReference("System.Drawing")
        from System.Threading import ApartmentState, Thread, ThreadStart

        self._ready = threading.Event()
        thread = Thread(ThreadStart(self._loop))
        thread.SetApartmentState(ApartmentState.STA)
        thread.IsBackground = True
        thread.Start()
        if not self._ready.wait(10) or self._icon is None:
            raise RuntimeError("tray did not start")

    def _loop(self) -> None:
        self._icon = None
        try:
            from System.Drawing import Icon
            from System.Windows.Forms import Application, ContextMenuStrip, MouseButtons, NotifyIcon

            menu = ContextMenuStrip()
            menu.Opening += self._on_opening
            icon = NotifyIcon()
            icon.Icon = Icon(_icon_path())
            icon.Text = "claude-swap"
            icon.ContextMenuStrip = menu
            icon.MouseClick += lambda s, e: e.Button == MouseButtons.Left and self.run_action(self._show)
            icon.Visible = True
            self._menu, self._icon = menu, icon
        except Exception:
            traceback.print_exc()
        finally:
            self._ready.set()
        if self._icon is not None:
            Application.Run()

    def _on_opening(self, sender, args) -> None:
        try:
            self._fill(self._menu.Items, self.model())
        except Exception:
            traceback.print_exc()
        # An empty strip arrives cancelled; it has items now.
        args.Cancel = False

    def _fill(self, items, model: list[Item]) -> None:
        from System.Windows.Forms import ToolStripMenuItem, ToolStripSeparator

        items.Clear()
        for it in model:
            if it is SEP:
                items.Add(ToolStripSeparator())
                continue
            entry = ToolStripMenuItem(it.label)
            entry.Checked = it.checked
            entry.Enabled = it.enabled
            if it.submenu is not None:
                self._fill(entry.DropDownItems, it.submenu)
            elif it.action is not None:
                entry.Click += lambda s, e, a=it.action: self.run_action(a)
            items.Add(entry)

    def stop(self) -> None:
        if self._icon is not None:
            self._icon.Visible = False
            self._icon.Dispose()
            self._icon = None

    def notify(self, title: str, text: str) -> None:
        from System.Windows.Forms import ToolTipIcon

        if self._icon is not None:
            self._icon.ShowBalloonTip(3000, title, text, ToolTipIcon.Info)

    def confirm(self, title: str, text: str) -> bool:
        import ctypes

        # MB_YESNO | MB_ICONWARNING | MB_SETFOREGROUND | MB_TOPMOST
        return ctypes.windll.user32.MessageBoxW(None, text, title, 0x4 | 0x30 | 0x10000 | 0x40000) == 6


_MacTarget = None


def _mac_target_class():
    global _MacTarget
    if _MacTarget is None:
        from Foundation import NSObject

        class CswapTrayTarget(NSObject):
            def menuNeedsUpdate_(self, menu):
                self.tray._rebuild(menu)

            def fire_(self, sender):
                self.tray._fire(sender.tag())

        _MacTarget = CswapTrayTarget
    return _MacTarget


class _MacTray(Tray):
    def start(self) -> None:
        from PyObjCTools import AppHelper

        self._ready = threading.Event()
        self._error: BaseException | None = None
        AppHelper.callAfter(self._create)
        if not self._ready.wait(10) or self._error is not None:
            raise RuntimeError(f"tray did not start: {self._error}")

    def _create(self) -> None:  # main thread
        try:
            from AppKit import NSImage, NSMenu, NSStatusBar, NSVariableStatusItemLength

            self._actions: list[Callable[[], None]] = []
            self._target = _mac_target_class().alloc().init()
            self._target.tray = self
            item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
            image = NSImage.alloc().initWithContentsOfFile_(_icon_path())
            image.setSize_((18, 18))
            item.button().setImage_(image)
            item.button().setToolTip_("claude-swap")
            menu = NSMenu.alloc().init()
            menu.setAutoenablesItems_(False)
            menu.setDelegate_(self._target)
            item.setMenu_(menu)
            self._item = item
        except BaseException as e:  # noqa: BLE001 — reported to start()
            self._error = e
        finally:
            self._ready.set()

    def _rebuild(self, menu) -> None:  # main thread, as the menu opens
        menu.removeAllItems()
        self._actions = []
        try:
            self._fill(menu, self.model())
        except Exception:
            traceback.print_exc()

    def _fill(self, menu, model: list[Item]) -> None:
        from AppKit import NSMenu, NSMenuItem

        for it in model:
            if it is SEP:
                menu.addItem_(NSMenuItem.separatorItem())
                continue
            entry = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(it.label, None, "")
            entry.setState_(1 if it.checked else 0)
            entry.setEnabled_(it.enabled)
            if it.submenu is not None:
                sub = NSMenu.alloc().init()
                sub.setAutoenablesItems_(False)
                self._fill(sub, it.submenu)
                entry.setSubmenu_(sub)
            elif it.action is not None:
                entry.setTarget_(self._target)
                entry.setAction_("fire:")
                entry.setTag_(len(self._actions))
                self._actions.append(it.action)
            menu.addItem_(entry)

    def _fire(self, tag: int) -> None:
        if 0 <= tag < len(self._actions):
            self.run_action(self._actions[tag])

    def stop(self) -> None:
        from AppKit import NSStatusBar
        from PyObjCTools import AppHelper

        item = getattr(self, "_item", None)
        if item is not None:
            AppHelper.callAfter(NSStatusBar.systemStatusBar().removeStatusItem_, item)
            self._item = None

    def notify(self, title: str, text: str) -> None:
        script = f"display notification {_osa(text)} with title {_osa(title)}"
        subprocess.Popen(["osascript", "-e", script])

    def confirm(self, title: str, text: str) -> bool:
        script = (
            f"display dialog {_osa(text)} with title {_osa(title)} "
            'buttons {"取消", "强制重启并切换"} default button 2 with icon caution'
        )
        return subprocess.run(["osascript", "-e", script], capture_output=True).returncode == 0


def _osa(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def start_tray(api, show: Callable[[], None], quit: Callable[[], None]) -> Tray | None:
    """Start the tray for this platform, or None when it cannot run here."""
    cls = {"win32": _WinTray, "darwin": _MacTray}.get(sys.platform)
    if cls is None:
        return None
    tray = cls(api, show, quit)
    try:
        tray.start()
    except Exception:
        traceback.print_exc()
        return None
    return tray
