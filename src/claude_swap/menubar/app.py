"""PyObjC shell for the claude-swap macOS menu bar (``cswap menubar``).

The v2 shell: an NSStatusItem (SF Symbol + live pct title) whose click opens
a menu, a background snapshot loop driving display state, the real
``AutoSwitchEngine`` hosted in a thread when enabled, and notifications via
``osascript`` (no bundle id required, unlike the old rumps path).

Import-safety mirrors the old module: this file imports no PyObjC at module
level, so every platform (and CI without the ``menubar`` extra) can import
it for the pure pieces (``MenuBarSettings``, ``framework_build_warning``);
AppKit is imported lazily inside ``run()``. The popover panel wiring lives
here as well once the bridge lands.

Display/refresh threading follows the legacy rumps app's proven pattern:
a worker thread takes a paced snapshot (``SnapshotSource``) and rebinds
plain attributes — atomic in CPython — while a 1s main-thread sync tick
applies them, detects active-account changes by config mtime, and drains
engine events. The AppKit main thread never touches locks, Keychain, or
the network.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.menubar.bridge import Bridge
from claude_swap.menubar.viewmodel import (
    EMPTY_SNAPSHOT,
    _adapt_snapshot,
    _usage_log_key,
    build,
    format_account_label,
    format_title,
    format_usage_log,
    parse_switch_history,
)

REFRESH_CHOICES: tuple[int, ...] = (30, 60, 300)
AUTO_THRESHOLD_CHOICES: tuple[int, ...] = (80, 90, 95, 98)
TITLE_PCT_CHOICES: tuple[str, ...] = ("off", "5h", "7d", "both")
THEME_CHOICES: tuple[str, ...] = ("system", "light", "dark")


@dataclass
class MenuBarSettings:
    """User-configurable menu bar display behavior, persisted as JSON.

    Only display preferences and the auto-switch on/off toggle live here.
    Auto-switch *policy* (threshold, cooldown, hysteresis, …) is core config,
    read/written through ``claude_swap.settings`` (the ``autoswitch.*`` keys),
    so the CLI and the menu bar share one source of truth.
    """

    show_account_name: bool = True
    title_pct: str = "both"  # one of TITLE_PCT_CHOICES
    title_scoped: bool = False  # append per-model weekly limits (e.g. Fable) to the title
    refresh_interval: int = 60
    auto_switch_enabled: bool = False
    theme: str = "system"

    @classmethod
    def load(cls, path: Path) -> "MenuBarSettings":
        """Load settings, falling back to defaults on any problem.

        Unknown keys are ignored; a value whose type doesn't match the field
        default is dropped (that field keeps its default). A missing or
        unparseable file yields all-defaults.
        """
        defaults = cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return defaults
        if not isinstance(raw, dict):
            return defaults
        kwargs = {}
        for f in fields(cls):
            default = getattr(defaults, f.name)
            value = raw.get(f.name)
            # bool is an int subclass: `"refresh_interval": true` would load
            # as 1 (a 1-second timer) without this exclusion.
            if (
                isinstance(value, type(default))
                and not (isinstance(value, bool) and not isinstance(default, bool))
            ):
                kwargs[f.name] = value
        if kwargs.get("theme") not in THEME_CHOICES:
            kwargs.pop("theme", None)
        return cls(**kwargs)

    def save(self, path: Path) -> None:
        """Write settings as JSON via the shared atomic-write helper.

        A torn plain write silently reverts every pref to defaults on the
        next load; atomic_write_json (temp file + rename) can't tear.
        """
        from claude_swap.settings import atomic_write_json

        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, asdict(self))


# ---- notifications (no app bundle required) ---------------------------------

def _osa_quote(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def notify(title: str, message: str) -> None:
    """Post a macOS notification via osascript (banner-only, like rumps was).

    Deliberately fire-and-forget: a notification failure must never affect
    the app loop. A plain ``display notification`` works from non-bundled
    CLI processes, which removes the old requirement to maintain an
    ``Info.plist`` beside the interpreter.
    """
    script = (
        f'display notification "{_osa_quote(message)}" '
        f'with title "{_osa_quote(title)}" sound name "Glass"'
    )

    def run_osascript() -> None:
        try:
            subprocess.run(
                ["/usr/bin/osascript", "-e", script], check=False, timeout=5
            )
        except (OSError, subprocess.SubprocessError):
            pass

    # Fire-and-forget: osascript can take seconds, and notify() is called
    # from the 1s sync tick — it must never block the AppKit main thread.
    threading.Thread(target=run_osascript, daemon=True).start()


# ---- environment gating (unchanged from the legacy module) ------------------
#
# macOS 26 stopped drawing status items for processes launched through an
# exec trampoline, and a CPython *framework* build is exactly that: its
# ``bin/python3.x`` is a stub that posix_spawns into ``Python.app``
# (Mac/Tools/pythonw.c). Homebrew and python.org ship framework builds;
# uv-managed and most other interpreters do not.
#
# Measured on macOS 26.6.2 with rumps 0.4.0, same bare app throughout:
#
#   Homebrew 3.14.6   sys._framework 'Python'   no status item
#   Homebrew 3.10.21  sys._framework 'Python'   no status item
#   uv 3.14.7         sys._framework ''         status item drawn
#   uv 3.13.15        sys._framework ''         status item drawn
#
# The interpreter version is not the variable; the build is.

MIN_AFFECTED_MACOS = 26


def _macos_major(mac_ver: str | None = None) -> int | None:
    """Major version of the running macOS, or None if it cannot be read."""
    raw = platform.mac_ver()[0] if mac_ver is None else mac_ver
    head = raw.split(".")[0]
    return int(head) if head.isdigit() else None


def framework_build_warning(
    framework=None, install_method=None, mac_ver: str | None = None
) -> str | None:
    """Text to show when this interpreter cannot draw a status item.

    Returns None wherever the menu bar is known to work. Both halves of the
    condition matter: the evidence is a framework build *on macOS 26*, and
    framework builds draw fine on earlier releases — gating on the build alone
    would nag every Homebrew user on macOS 14 or 15, on every launch and in
    the service log on every restart.

    Nothing here can fix the incompatibility. The point is that it fails
    silently, with a healthy process and empty logs, so it is worth one line.
    """
    fw = getattr(sys, "_framework", "") if framework is None else framework
    if not fw:
        return None

    major = _macos_major(mac_ver)
    if major is None or major < MIN_AFFECTED_MACOS:
        return None

    if install_method is None:
        from claude_swap.update_check import _detect_install_method

        install_method = _detect_install_method()

    if install_method == "uv":
        remedy = (
            "  uv tool install --managed-python --force 'claude-swap[menubar]'"
        )
    elif install_method == "pipx":
        remedy = (
            "  Reinstall against a non-framework interpreter, e.g. one from "
            "`uv python install 3.13`:\n"
            "  pipx install --force --python <that python> 'claude-swap[menubar]'"
        )
    else:
        remedy = (
            "  Reinstall against a non-framework interpreter "
            "(uv-managed ones are; Homebrew and python.org are not)."
        )

    return (
        "This is a framework build of Python, which on macOS 26 has been "
        "observed not to draw the menu bar icon: the process runs and logs "
        "nothing, but no status item appears.\n" + remedy
    )


# ---- entry point -------------------------------------------------------------

def run(switcher) -> int:
    """Run the menu bar app on the AppKit main loop; returns an exit code."""
    try:
        import AppKit  # noqa: F401
        import WebKit  # noqa: F401
    except ImportError as e:
        # A missing extra — the failure lands here at call time. Raise the
        # error type the CLI already renders cleanly instead of a traceback.
        raise ClaudeSwitchError(
            "Menu bar mode requires PyObjC (pyobjc-framework-Cocoa/-WebKit). "
            "Install with: pip install 'claude-swap[menubar]'"
        ) from e

    from PyObjCTools import AppHelper

    from claude_swap.autoswitch import AutoSwitchEngine
    from claude_swap.settings import load_settings, set_setting
    from claude_swap.snapshot_source import SnapshotSource

    settings_path = switcher.backup_dir / "menubar_settings.json"
    log_path = switcher.backup_dir / "claude-swap.log"

    class MenuBarShell:
        """Owns the status item, menu, timers, and engine thread."""

        def __init__(self) -> None:
            self.switcher = switcher
            self.settings = MenuBarSettings.load(settings_path)
            # The supported paced read path (same as the legacy app): fetch
            # only the active account plus at most one stale alternate per
            # refresh, holding pacing state across refreshes.
            self._snapshot_source = SnapshotSource(switcher)
            self.snapshot: dict = dict(EMPTY_SNAPSHOT)
            self._dirty = False
            self._refreshing = False
            self._config_path = switcher._get_claude_config_path()
            self._config_mtime = 0.0
            self._last_usage_log: dict = {}
            self._engine = None
            self._engine_events: list = []
            self._event_lock = threading.Lock()
            self._callbacks: dict[int, callable] = {}  # tag -> handler (keeps refs)
            self._next_tag = 1
            self._vm: dict | None = None
            self._menu = None
            self._install_status_item()
            self._install_panel()
            self.rebuild_menu()
            self._refresh_timer = AppKit.NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
                self.settings.refresh_interval,
                self._target,
                "onRefreshTick:",
                None,
                True,
            )
            AppKit.NSRunLoop.mainRunLoop().addTimer_forMode_(
                self._refresh_timer, AppKit.NSRunLoopCommonModes
            )
            self._sync_timer = AppKit.NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
                1.0, self._target, "onSyncTick:", None, True
            )
            AppKit.NSRunLoop.mainRunLoop().addTimer_forMode_(
                self._sync_timer, AppKit.NSRunLoopCommonModes
            )
            self.refresh_async()  # first display fetch
            if self.settings.auto_switch_enabled:
                self._start_engine()

        # ---- status item + target ------------------------------------------

        def _install_status_item(self) -> None:
            self._target = self._make_target()
            self._status_item = AppKit.NSStatusBar.systemStatusBar(
            ).statusItemWithLength_(AppKit.NSVariableStatusItemLength)
            button = self._status_item.button()
            image = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                "arrow.left.arrow.right", "claude-swap"
            )
            if image is not None:
                image.setTemplate_(True)
                button.cell().setImage_(image)
            button.setToolTip_("claude-swap")
            button.setTarget_(self._target)
            button.setAction_("onStatusClick:")
            # The default action mask is left-mouse-only; without right-mouse
            # masks the fallback-menu branch in on_status_click is dead code
            # (verified: the cell's default mask is NSEventMaskLeftMouseUp).
            button.cell().sendActionOn_(
                AppKit.NSEventMaskLeftMouseUp | AppKit.NSEventMaskRightMouseUp
            )
            self._button = button

        def _make_target(self):
            shell = self

            class ShellTarget(AppKit.NSObject):
                def onRefreshTick_(self, _timer):
                    shell.refresh_async()

                def onSyncTick_(self, _timer):
                    shell.on_sync_tick()

                def onAction_(self, sender):
                    tag = sender.tag()
                    cb = shell._callbacks.get(tag)
                    if cb is not None:
                        cb()

                def onStatusClick_(self, _sender):
                    shell.on_status_click()

                # WKScriptMessageHandler: the panel's only inbound channel.
                def userContentController_didReceiveScriptMessage_(
                    self, _ucc, message
                ):
                    body = message.body()
                    if isinstance(body, str):
                        shell.handle_webview_message(body)

                # Navigation lockdown: the bundled file load is allowed;
                # everything else (redirects, drag-navigation) is cancelled.
                def webView_decidePolicyForNavigationAction_decisionHandler_(
                    self, _webview, navigationAction, handler
                ):
                    url = navigationAction.request().URL()
                    try:
                        allowed = (
                            url is not None
                            and url.isFileURL()
                            and Path(str(url.path())).resolve()
                            .is_relative_to(shell.web_dir.resolve())
                        )
                    except OSError:
                        allowed = False
                    handler(1 if allowed else 0)  # Allow / Cancel

            return ShellTarget.new()

        def _register(self, cb) -> int:
            tag = self._next_tag
            self._next_tag += 1
            self._callbacks[tag] = cb
            return tag

        def _menu_item(self, title, cb=None, state=0, indent=0):
            item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                title, "onAction:" if cb else "", ""
            )
            if cb:
                item.setTarget_(self._target)
                item.setTag_(self._register(cb))
            item.setState_(state)
            item.setIndentationLevel_(indent)
            return item

        def _separator(self):
            return AppKit.NSMenuItem.separatorItem()

        # ---- menu ------------------------------------------------------------

        def rebuild_menu(self) -> None:
            # The old NSMenu is discarded with this rebuild; its tag->closure
            # entries must go with it or the registry grows forever in a
            # launchd-resident process (a smaller echo of the rumps leak the
            # legacy module documented).
            self._callbacks.clear()
            self._next_tag = 1
            self._button.setTitle_(format_title(
                self.snapshot["active_email"],
                self.snapshot["active_usage"],
                self.settings,
                alias=self.snapshot.get("active_alias"),
            ))
            menu = AppKit.NSMenu.alloc().init()
            accounts = self.snapshot["accounts"]
            if not accounts:
                menu.addItem_(self._menu_item("No managed accounts — add below"))
            for num, email, is_active, display, _lg, alias, disabled, fetched_at in accounts:
                menu.addItem_(self._menu_item(
                    format_account_label(
                        num, email, display, alias=alias, disabled=disabled,
                        fetched_at=fetched_at,
                    ),
                    cb=self._make_switch_to(num),
                    state=1 if is_active else 0,
                ))
            menu.addItem_(self._separator())
            menu.addItem_(self._menu_item("Rotate to next", cb=self._switch(None)))
            menu.addItem_(self._menu_item("Switch to best", cb=self._switch("best")))
            menu.addItem_(self._menu_item("Next available", cb=self._switch("next-available")))
            menu.addItem_(self._separator())
            menu.addItem_(self._menu_item("Add account from current login…", cb=self.on_add_login))
            menu.addItem_(self._separator())
            auto = self._menu_item(
                "Auto-switch accounts", cb=self.on_toggle_autoswitch,
                state=1 if self.settings.auto_switch_enabled else 0,
            )
            menu.addItem_(auto)
            threshold = AppKit.NSMenu.alloc().init()
            current = self._threshold()
            th_item = self._menu_item("Auto-switch threshold")
            th_item.setSubmenu_(threshold)
            for pct in AUTO_THRESHOLD_CHOICES:
                threshold.addItem_(self._menu_item(
                    f"{pct}%", cb=self._make_threshold(pct),
                    state=1 if current == pct else 0, indent=1,
                ))
            menu.addItem_(th_item)
            menu.addItem_(self._separator())
            settings = AppKit.NSMenu.alloc().init()
            st_item = self._menu_item("Settings")
            st_item.setSubmenu_(settings)
            settings.addItem_(self._menu_item(
                "Show account name in menu bar", cb=self.on_toggle_name,
                state=1 if self.settings.show_account_name else 0, indent=1))
            tp = AppKit.NSMenu.alloc().init()
            tp_item = self._menu_item("Title percentage")
            tp_item.setSubmenu_(tp)
            labels = {"off": "None", "5h": "Session (5h)",
                      "7d": "Weekly (7d)", "both": "Both (5h · 7d)"}
            for mode in TITLE_PCT_CHOICES:
                tp.addItem_(self._menu_item(
                    labels[mode], cb=self._make_title_pct(mode),
                    state=1 if self.settings.title_pct == mode else 0, indent=1))
            settings.addItem_(tp_item)
            settings.addItem_(self._menu_item(
                "Show model limits in title", cb=self.on_toggle_scoped,
                state=1 if self.settings.title_scoped else 0, indent=1))
            interval = AppKit.NSMenu.alloc().init()
            iv_item = self._menu_item("Refresh interval")
            iv_item.setSubmenu_(interval)
            iv_labels = {30: "30 seconds", 60: "60 seconds", 300: "5 minutes"}
            for secs in REFRESH_CHOICES:
                interval.addItem_(self._menu_item(
                    iv_labels[secs], cb=self._make_interval(secs),
                    state=1 if self.settings.refresh_interval == secs else 0, indent=1))
            settings.addItem_(iv_item)
            menu.addItem_(st_item)
            menu.addItem_(self._menu_item("Refresh now", cb=self.on_refresh_now))
            menu.addItem_(self._menu_item("Quit", cb=self.on_quit))
            menu.setAutoenablesItems_(False)
            self._menu = menu

        # ---- popover panel ----------------------------------------------------

        def _install_panel(self) -> None:
            web_dir = Path(__file__).resolve().parent / "web"
            self.web_dir = web_dir
            config = WebKit.WKWebViewConfiguration.new()
            config.userContentController().addScriptMessageHandler_name_(
                self._target, "cswap"
            )
            self._webview = WebKit.WKWebView.alloc().initWithFrame_configuration_(
                ((0.0, 0.0), (360.0, 560.0)), config
            )
            self._webview.setNavigationDelegate_(self._target)
            controller = AppKit.NSViewController.alloc().init()
            controller.setView_(self._webview)
            self._popover = AppKit.NSPopover.alloc().init()
            self._popover.setContentViewController_(controller)
            self._popover.setContentSize_((360.0, 560.0))
            self._popover.setBehavior_(AppKit.NSPopoverBehaviorTransient)
            self._popover.setAppearance_(None)  # follow system light/dark
            self._bridge = Bridge(
                self._panel_handlers(),
                send_js=self._send_js,
                payload_specs={
                    "switch": {"required": {"slot": str}},
                    "disable": {"required": {"slot": str}},
                    "enable": {"required": {"slot": str}},
                    "remove": {"required": {"slot": str}},
                    "addFromToken": {"required": {"token": str},
                                     "optional": {"email": str}},
                    "setAutoSwitch": {"required": {"enabled": bool}},
                    "setAlias": {"required": {"slot": str, "alias": str}},
                    "unsetAlias": {"required": {"slot": str}},
                    "setPrefs": {"optional": {"refreshInterval": int,
                                              "titlePct": str, "theme": str}},
                },
            )
            self._webview.loadFileURL_allowingReadAccessToURL_(
                AppKit.NSURL.fileURLWithPath_(str(web_dir / "index.html")),
                AppKit.NSURL.fileURLWithPath_(str(web_dir)),
            )

        def _panel_handlers(self) -> dict:
            sw = self.switcher

            def do_switch(payload):
                target = str(payload["slot"])
                sw.switch_to(target)
                self.refresh_async()
                return {"switchedTo": target}

            def do_strategy(strategy):
                def handler(payload):
                    sw.switch(strategy=strategy)
                    self.refresh_async()
                    return {"switched": True}
                return handler

            return {
                "getSnapshot": lambda payload: self.current_vm(),
                "refresh": lambda payload: (self.current_vm(full=True), self.push_vm())[1] or {},
                "switch": do_switch,
                "rotate": do_strategy(None),
                "best": do_strategy("best"),
                "disable": lambda payload: (
                    sw.set_account_disabled(str(payload["slot"]), True),
                    self.refresh_async(),
                )[1] or {},
                "enable": lambda payload: (
                    sw.set_account_disabled(str(payload["slot"]), False),
                    self.refresh_async(),
                )[1] or {},
                "remove": lambda payload: (
                    sw.remove_account(str(payload["slot"]), assume_yes=True),
                    self.refresh_async(),
                )[1] or {},
                "addFromLogin": lambda payload: (
                    sw.add_account(slot=None), self.refresh_async(),
                )[1] or {},
                "addFromToken": self._add_from_token,
                "setAutoSwitch": lambda payload: (
                    AppHelper.callAfter(self._set_auto, payload["enabled"]),
                )[0] or {"scheduled": True},
                "getPrefs": lambda payload: {
                    "theme": self.settings.theme,
                    "refreshInterval": self.settings.refresh_interval,
                    "titlePct": self.settings.title_pct,
                },
                "setPrefs": self._set_prefs,
                "setAlias": self._set_alias,
                "unsetAlias": self._unset_alias,
                "quit": lambda payload: (
                    AppHelper.callAfter(self.on_quit),
                )[0] or {"scheduled": True},
            }

        def _add_from_token(self, payload):
            if not hasattr(self.switcher, "add_account_from_token"):
                raise ClaudeSwitchError("adding from a setup token is not supported")
            if not str(payload.get("token", "")).strip():
                # The switcher falls back to getpass for a missing token —
                # on a bridge daemon thread that would wedge invisibly.
                raise ClaudeSwitchError("token is required")
            self.switcher.add_account_from_token(
                token=payload["token"], email=payload.get("email") or "", slot=None,
            )
            self.refresh_async()
            return {"added": True}

        def _set_alias(self, payload):
            slot, alias = self.switcher.set_alias(
                str(payload["slot"]), str(payload["alias"])
            )
            self._push_alias_update()
            return {"slot": slot, "alias": alias}

        def _unset_alias(self, payload):
            slot = self.switcher.unset_alias(str(payload["slot"]))
            self._push_alias_update()
            return {"slot": slot, "alias": None}

        def _push_alias_update(self) -> None:
            """Reflect an alias change everywhere, without the usage API.

            Aliases live in local config, so the view model is rebuilt from
            the store (``store_only`` never spends request budget), pushed
            to the open panel, and the native menu labels rebuilt on the
            main thread. The active credential and other accounts are
            untouched — renaming never switches anything.
            """
            raw = self._snapshot_source.take(store_only=True)
            self.snapshot = _adapt_snapshot(raw)
            core = load_settings(self.switcher.backup_dir)
            self._vm = build(
                raw,
                auto_enabled=self.settings.auto_switch_enabled,
                auto_threshold=core.threshold,
                auto_strategy=core.strategy,
                history=self._history(),
            )
            self._dirty = True
            self.push_vm()
            AppHelper.callAfter(self.rebuild_menu)

        def _set_auto(self, enabled: bool) -> None:
            # Main thread: touches engine threads, settings, and the menu.
            if self.settings.auto_switch_enabled != enabled:
                self.on_toggle_autoswitch()
            self.rebuild_menu()
            self.refresh_async()  # the open panel's toggle must not go stale

        def _set_prefs(self, payload):
            interval = payload.get("refreshInterval")
            title_pct = payload.get("titlePct")
            theme = payload.get("theme")
            if theme is not None and theme not in THEME_CHOICES:
                raise ClaudeSwitchError(f"theme must be one of {THEME_CHOICES}")
            if interval is not None and interval not in REFRESH_CHOICES:
                raise ClaudeSwitchError(f"refresh interval must be one of {REFRESH_CHOICES}")
            if title_pct is not None and title_pct not in TITLE_PCT_CHOICES:
                raise ClaudeSwitchError(f"title percentage must be one of {TITLE_PCT_CHOICES}")

            # Save on the main thread with the other preference writers, and
            # acknowledge only after persistence succeeds (the bridge is a worker).
            finished = threading.Event()
            errors = []

            def apply():
                try:
                    updated = replace(self.settings)
                    if interval is not None:
                        updated.refresh_interval = interval
                    if title_pct is not None:
                        updated.title_pct = title_pct
                    if theme is not None:
                        updated.theme = theme
                    updated.save(settings_path)
                    self.settings = updated
                    if interval is not None:
                        self._restart_refresh_timer(interval)
                    self.rebuild_menu()
                except Exception as exc:
                    errors.append(exc)
                finally:
                    finished.set()
            AppHelper.callAfter(apply)
            finished.wait()
            if errors:
                raise errors[0]
            return {"saved": True, "theme": self.settings.theme}

        def handle_webview_message(self, raw: str) -> None:
            self._bridge.handle_message(raw)

        def _send_js(self, js: str) -> None:
            # evaluateJavaScript must run on the main thread.
            AppHelper.callAfter(
                lambda: self._webview.evaluateJavaScript_completionHandler_(js, None)
            )

        def push_vm(self) -> None:
            if self._vm is not None:
                self._bridge.push("vm", self._vm)

        def _history(self) -> list[str]:
            try:
                return parse_switch_history(log_path.read_text(encoding="utf-8"))
            except OSError:
                return []

        def current_vm(self, full: bool = False) -> dict:
            """Take a paced snapshot (blocking — bridge thread) and build the vm."""
            raw = self._snapshot_source.take(
                full=full, store_only=self._engine is not None
            )
            self.snapshot = _adapt_snapshot(raw)
            self._log_usage(self.snapshot)
            core = load_settings(self.switcher.backup_dir)
            self._vm = build(
                raw,
                auto_enabled=self.settings.auto_switch_enabled,
                auto_threshold=core.threshold,
                auto_strategy=core.strategy,
                history=self._history(),
            )
            self._dirty = True
            return self._vm

        def on_status_click(self) -> None:
            ev = AppKit.NSApplication.sharedApplication().currentEvent()
            if ev is not None and ev.type() in (
                AppKit.NSEventTypeRightMouseUp, AppKit.NSEventTypeRightMouseDown,
            ):
                if self._menu is not None:
                    self._menu.popUpMenuPositioningItem_atLocation_inView_(
                        None, (0.0, 0.0), self._button
                    )
                return
            if self._popover.isShown():
                self._popover.performClose_(None)
                return
            self.push_vm()  # instant paint from the last vm before freshening
            self._popover.showRelativeToRect_ofView_preferredEdge_(
                self._button.bounds(), self._button, 3  # NSMaxYEdge: below the item
            )
            self.refresh_async()

        def _save_and_rebuild(self) -> None:
            self.settings.save(settings_path)
            self.rebuild_menu()

        # ---- callbacks --------------------------------------------------------

        def _guard(self, fn, on_success=None) -> None:
            """Run a switcher action on a worker thread, alerting on error.

            ``on_success`` (if given) runs on the main thread only when the
            action actually succeeded — the outcome is only known when the
            worker finishes, so success side effects (notifications, refresh)
            belong in the completion callback, not the caller.
            """
            def run_action():
                try:
                    fn()
                    ok, err = True, None
                except ClaudeSwitchError as e:
                    ok, err = False, str(e)
                AppHelper.callAfter(self._action_done, ok, err, on_success)
            threading.Thread(target=run_action, daemon=True).start()

        def _action_done(self, ok: bool, err: str | None, on_success=None) -> None:
            if not ok:
                AppKit.NSRunAlertPanel("claude-swap", err or "Action failed",
                                       "OK", None, None)
                return
            if on_success is not None:
                on_success()
            self.refresh_async()

        def _notify_switched(self) -> None:
            notify(
                "Account switched",
                "Switch takes effect within ~30s — restart Claude Code to apply immediately.",
            )

        def _make_switch_to(self, num):
            def cb():
                self._guard(lambda: self.switcher.switch_to(str(num)),
                            on_success=self._notify_switched)
            return cb

        def _switch(self, strategy):
            def cb():
                self._guard(lambda: self.switcher.switch(strategy=strategy),
                            on_success=self._notify_switched)
            return cb

        def on_add_login(self) -> None:
            def do_add():
                try:
                    self.switcher.add_account(slot=None)
                    AppHelper.callAfter(lambda: (self.refresh_async(),))
                except ClaudeSwitchError as e:
                    AppHelper.callAfter(self._action_done, False, str(e))
            threading.Thread(target=do_add, daemon=True).start()

        def on_refresh_now(self) -> None:
            self.refresh_async(full=True)

        def on_quit(self) -> None:
            self._stop_engine()
            AppKit.NSApplication.sharedApplication().stop_(None)

        def on_toggle_name(self) -> None:
            self.settings.show_account_name = not self.settings.show_account_name
            self._save_and_rebuild()

        def on_toggle_scoped(self) -> None:
            self.settings.title_scoped = not self.settings.title_scoped
            self._save_and_rebuild()

        def _make_title_pct(self, mode):
            def cb():
                self.settings.title_pct = mode
                self._save_and_rebuild()
            return cb

        def _make_interval(self, secs):
            def cb():
                self.settings.refresh_interval = secs
                self._restart_refresh_timer(secs)
                self._save_and_rebuild()
            return cb

        def _restart_refresh_timer(self, secs: float) -> None:
            """A refresh-interval change needs a fresh NSTimer: the interval
            is fixed at creation, and setFireDate_ only delays the next fire
            before the old cadence resumes."""
            if self._refresh_timer is not None:
                self._refresh_timer.invalidate()
            self._refresh_timer = AppKit.NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
                secs, self._target, "onRefreshTick:", None, True
            )
            AppKit.NSRunLoop.mainRunLoop().addTimer_forMode_(
                self._refresh_timer, AppKit.NSRunLoopCommonModes
            )

        def on_toggle_autoswitch(self) -> None:
            self.settings.auto_switch_enabled = not self.settings.auto_switch_enabled
            self.settings.save(settings_path)
            if self.settings.auto_switch_enabled:
                self._start_engine()
            else:
                self._stop_engine()
            self.rebuild_menu()

        def _make_threshold(self, pct):
            def cb():
                try:
                    set_setting(self.switcher.backup_dir, "autoswitch.threshold", str(pct))
                except Exception as e:
                    self._action_done(False, f"Couldn't set threshold: {e}")
                    return
                self._restart_engine()
                self.rebuild_menu()
            return cb

        # ---- display refresh plumbing ----------------------------------------

        def refresh_async(self, full: bool = False) -> None:
            if self._refreshing:
                return  # in-flight guard: one worker at a time (SnapshotSource
                        # pacing state is only touched by this single worker)
            self._refreshing = True
            threading.Thread(target=self._worker, args=(full,), daemon=True).start()

        def _worker(self, full: bool) -> None:
            # Lock-free handoff: worker only rebinds plain attributes (atomic
            # in CPython); the main-thread sync tick reads them. While the
            # engine runs it already paces all fetching, so the display reads
            # store-only.
            try:
                try:
                    self.current_vm(full=full)
                except Exception:
                    # Keep the last good snapshot rather than blanking the menu.
                    self.switcher._logger.debug("menubar snapshot failed", exc_info=True)
                else:
                    self._dirty = True  # picked up by the sync tick on the main thread
            finally:
                self._refreshing = False

        def _log_usage(self, snap: dict) -> None:
            for num, email, _a, _d, last_good, _al, _dis, _f in snap["accounts"]:
                key = _usage_log_key(last_good)
                if key == (None, None) or self._last_usage_log.get(num) == key:
                    continue
                line = format_usage_log(email, last_good)
                if line:
                    self.switcher._logger.info(line)
                    self._last_usage_log[num] = key

        def on_sync_tick(self) -> None:
            if self._dirty:
                self._dirty = False
                self.rebuild_menu()
                if self._popover.isShown():
                    self.push_vm()
            self._detect_active_change()
            self._drain_engine_events()

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
            if email and email != self.snapshot.get("active_email"):
                self.refresh_async()

        # ---- auto-switch engine ----------------------------------------------

        def _start_engine(self) -> None:
            if self._engine is not None:
                return
            try:
                engine = AutoSwitchEngine(
                    self.switcher,
                    load_settings(self.switcher.backup_dir),
                    self._on_engine_event,
                    dry_run=False,
                )
            except Exception as e:  # never let a bad start crash the menu bar
                self.switcher._logger.warning("auto-switch engine failed to start: %s", e)
                notify("Auto-switch failed to start", str(e))
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
                human = ev.human()
                if ev.kind == "switch" and not getattr(ev, "dry_run", False):
                    notify("Auto-switched account", human)
                    self._bridge.push("engine", {"event": "switched", "text": human})
                    self.refresh_async()
                elif ev.kind == "account-quarantined":
                    notify("Account quarantined", human)
                    self._bridge.push("engine", {"event": "quarantined", "text": human})
                elif ev.kind == "all-exhausted":
                    notify("All accounts exhausted", human)
                    self._bridge.push("engine", {"event": "exhausted", "text": human})
                else:
                    # config-warning and anything the engine adds later
                    notify("Configuration warning", human)
                    self._bridge.push("engine", {"event": "warning", "text": human})

        def _threshold(self) -> int:
            try:
                return int(load_settings(self.switcher.backup_dir).threshold)
            except Exception:
                return 0

    MenuBarShell()
    AppHelper.runEventLoop()
    return 0
