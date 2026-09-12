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
import platform
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.menubar.viewmodel import (
    EMPTY_SNAPSHOT,
    _adapt_snapshot,
    _usage_log_key,
    format_account_label,
    format_title,
    format_usage_log,
)

REFRESH_CHOICES: tuple[int, ...] = (30, 60, 300)
AUTO_THRESHOLD_CHOICES: tuple[int, ...] = (80, 90, 95, 98)
TITLE_PCT_CHOICES: tuple[str, ...] = ("off", "5h", "7d", "both")


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
            if f.name in raw and isinstance(raw[f.name], type(getattr(defaults, f.name))):
                kwargs[f.name] = raw[f.name]
        return cls(**kwargs)

    def save(self, path: Path) -> None:
        """Write settings as pretty JSON, creating parent directories."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


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
    try:
        subprocess.run(
            ["/usr/bin/osascript", "-e", script], check=False, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        pass


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
    except ImportError as e:
        # A missing extra — the failure lands here at call time. Raise the
        # error type the CLI already renders cleanly instead of a traceback.
        raise ClaudeSwitchError(
            "Menu bar mode requires PyObjC (pyobjc-framework-Cocoa). "
            "Install with: pip install 'claude-swap[menubar]'"
        ) from e

    from PyObjCTools import AppHelper

    from claude_swap.autoswitch import AutoSwitchEngine
    from claude_swap.settings import load_settings, set_setting
    from claude_swap.snapshot_source import SnapshotSource

    settings_path = switcher.backup_dir / "menubar_settings.json"

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
            self._install_status_item()
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
            self._button = button

            # One NSObject target bridging every menu/timer action to Python.
            Target = AppKit.NSObject  # subclassed lazily; see _make_target
            self._target = self._make_target()

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
            self._status_item.setMenu_(menu)

        def _save_and_rebuild(self) -> None:
            self.settings.save(settings_path)
            self.rebuild_menu()

        # ---- callbacks --------------------------------------------------------

        def _guard(self, fn) -> bool:
            """Run a switcher action on the main thread, alerting on error."""
            # Blocking work stays off the main thread: the action runs in a
            # worker, the alert (if any) is marshalled back.
            def run_action():
                try:
                    fn()
                    ok, err = True, None
                except ClaudeSwitchError as e:
                    ok, err = False, str(e)
                AppHelper.callAfter(self._action_done, ok, err)
            threading.Thread(target=run_action, daemon=True).start()
            return True

        def _action_done(self, ok: bool, err: str | None) -> None:
            if not ok:
                AppKit.NSRunAlertPanel("claude-swap", err or "Action failed",
                                       "OK", None, None)

        def _notify_switched(self) -> None:
            notify(
                "Account switched",
                "Switch takes effect within ~30s — restart Claude Code to apply immediately.",
            )

        def _make_switch_to(self, num):
            def cb():
                if self._guard(lambda: self.switcher.switch_to(str(num))):
                    self._notify_switched()
                    self.refresh_async()
            return cb

        def _switch(self, strategy):
            def cb():
                if self._guard(lambda: self.switcher.switch(strategy=strategy)):
                    self._notify_switched()
                    self.refresh_async()
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
                self._refresh_timer.setFireDate_(
                    AppKit.NSDate.dateWithTimeIntervalSinceNow_(secs)
                )
                self._save_and_rebuild()
            return cb

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
                    raw = self._snapshot_source.take(
                        full=full, store_only=self._engine is not None
                    )
                except Exception:
                    # Keep the last good snapshot rather than blanking the menu.
                    self.switcher._logger.debug("menubar snapshot failed", exc_info=True)
                    return
                snap = _adapt_snapshot(raw)
                self._log_usage(snap)
                self.snapshot = snap
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
                if ev.kind == "switch" and not getattr(ev, "dry_run", False):
                    notify("Auto-switched account", ev.human())
                    self.refresh_async()
                elif ev.kind == "account-quarantined":
                    notify("Account quarantined", ev.human())
                elif ev.kind == "all-exhausted":
                    notify("All accounts exhausted", ev.human())
                elif ev.kind == "config-warning":
                    notify("Configuration warning", ev.human())

        def _threshold(self) -> int:
            try:
                return int(load_settings(self.switcher.backup_dir).threshold)
            except Exception:
                return 0

    MenuBarShell()
    AppHelper.runEventLoop()
    return 0
