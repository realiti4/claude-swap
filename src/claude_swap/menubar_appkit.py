"""Native AppKit status item and compact account-capacity popover.

This module deliberately imports PyObjC lazily.  Importing ``claude_swap`` on
Linux, Windows, or a macOS Python without the menu-bar extra remains safe; only
``run_native_menubar`` requires AppKit.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.menubar_layout import (
    BUTTON_HEIGHT,
    POPOVER_MAX_HEIGHT,
    POPOVER_WIDTH,
    Rect,
    plan_main_popover,
    plan_scroll_screen,
)
from claude_swap.menubar_controller import MenuBarController
from claude_swap.menubar_viewmodel import (
    AUTO_THRESHOLD_CHOICES,
    ICON,
    REFRESH_CHOICES,
    TITLE_PCT_CHOICES,
    CapacityState,
    MenuBarPopoverViewModel,
    PopoverAccountViewModel,
    UsageRowViewModel,
)

if TYPE_CHECKING:
    from claude_swap.switcher import ClaudeAccountSwitcher

try:  # Do not make importing the CLI depend on PyObjC.
    import AppKit
    import Foundation
    import objc
except (ImportError, SystemError):
    # Some unsupported PyObjC/Python combinations raise SystemError while
    # loading the Objective-C bridge. Menu-bar mode remains optional.
    APPKIT_AVAILABLE = False
else:
    APPKIT_AVAILABLE = True


_native_host: tuple[object, object] | None = None


def require_appkit() -> None:
    """Raise the CLI-facing error when the optional native runtime is absent."""
    if not APPKIT_AVAILABLE:
        raise ClaudeSwitchError(
            "Menu bar mode requires PyObjC/AppKit. Install with: pip install "
            "'claude-swap[menubar]'"
        )


def _bar(percent: float) -> str:
    """Render a dense text meter for the intentionally TUI-like popover."""
    filled = min(12, max(0, round(percent * 12 / 100)))
    return "█" * filled + "░" * (12 - filled)


def _create_native_menubar(switcher: ClaudeAccountSwitcher):
    """Construct the native status-item app without entering its event loop."""
    global _native_host
    require_appkit()
    assert APPKIT_AVAILABLE  # narrows the optional module for type checkers
    if _native_host is not None:
        return _native_host

    class FlippedView(AppKit.NSView):
        """An AppKit view whose geometry reads from top to bottom."""

        def isFlipped(self) -> bool:
            return True

    class PopoverContentController(AppKit.NSViewController):
        """Compact native renderer for the controller's immutable models."""

        def initWithHost_(self, host):
            self = objc.super(PopoverContentController, self).init()
            if self is None:
                return None
            self.host = host
            self._reset_main_scroll_on_render = False
            self.setView_(
                FlippedView.alloc().initWithFrame_(
                    AppKit.NSMakeRect(0, 0, POPOVER_WIDTH, POPOVER_MAX_HEIGHT)
                )
            )
            return self

        @objc.python_method
        def _label(self, text: str, frame: Rect, *, dim: bool = False):
            field = AppKit.NSTextField.labelWithString_(text)
            field.setFrame_(AppKit.NSMakeRect(frame.x, frame.y, frame.width, frame.height))
            field.setFont_(AppKit.NSFont.monospacedSystemFontOfSize_weight_(11.0, 0.0))
            field.setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
            if dim:
                field.setTextColor_(AppKit.NSColor.secondaryLabelColor())
            return field

        @objc.python_method
        def _button(self, title: str, action: str, frame: Rect, *, slot: str | None = None):
            button = AppKit.NSButton.alloc().initWithFrame_(
                AppKit.NSMakeRect(frame.x, frame.y, frame.width, frame.height)
            )
            button.setTitle_(title)
            button.setBezelStyle_(AppKit.NSBezelStyleRounded)
            button.setFont_(AppKit.NSFont.systemFontOfSize_(12.0))
            button.setTarget_(self.host)
            button.setAction_(action)
            if slot is not None:
                button.setRepresentedObject_(slot)
            return button

        @objc.python_method
        def _separator(self, frame: Rect):
            separator = AppKit.NSView.alloc().initWithFrame_(
                AppKit.NSMakeRect(frame.x, frame.y, frame.width, frame.height)
            )
            separator.setWantsLayer_(True)
            separator.layer().setBackgroundColor_(AppKit.NSColor.separatorColor().CGColor())
            return separator

        @objc.python_method
        def _add(self, view, parent) -> None:
            parent.addSubview_(view)

        @objc.python_method
        def _local(self, frame: Rect, parent_frame: Rect) -> Rect:
            return Rect(frame.x - parent_frame.x, frame.y - parent_frame.y, frame.width, frame.height)

        @objc.python_method
        def _button_pair(self, frame: Rect) -> tuple[Rect, Rect]:
            gap = 8.0
            button_width = (frame.width - gap) / 2
            return (
                Rect(frame.x, frame.y, button_width, BUTTON_HEIGHT),
                Rect(frame.x + button_width + gap, frame.y, button_width, BUTTON_HEIGHT),
            )

        @objc.python_method
        def _account_group(
            self, account: PopoverAccountViewModel, layout, document, *, show_separator: bool
        ) -> None:
            group = FlippedView.alloc().initWithFrame_(
                AppKit.NSMakeRect(
                    layout.frame.x, layout.frame.y, layout.frame.width, layout.frame.height
                )
            )
            document.addSubview_(group)
            if show_separator:
                group.addSubview_(self._separator(Rect(0.0, 0.0, layout.frame.width, 1.0)))
            marker = "●" if account.is_active else "○"
            state = " DISABLED" if account.disabled else ""
            group.addSubview_(
                self._label(
                    f"{marker} #{account.number}  {account.display_name}{state}",
                    self._local(layout.title, layout.frame),
                )
            )
            group.addSubview_(
                self._label(
                    account.capacity_summary,
                    self._local(layout.capacity, layout.frame),
                    dim=True,
                )
            )
            group.addSubview_(
                self._label(account.email, self._local(layout.email, layout.frame), dim=True)
            )
            freshness = account.freshness_detail
            if account.sentinel_note:
                freshness = f"{freshness} · {account.sentinel_note}"
            group.addSubview_(
                self._label(freshness, self._local(layout.freshness, layout.frame), dim=True)
            )
            for row, frame in zip(account.rows, layout.usage_rows, strict=True):
                self._usage_row(row, self._local(frame, layout.frame), group)
            group.addSubview_(
                self._button(
                    "Make active",
                    "makeActive:",
                    self._local(layout.make_active, layout.frame),
                    slot=account.number,
                )
            )
            if account.session_available:
                session_button = self._button(
                    "Session",
                    "isolatedSession:",
                    self._local(layout.session, layout.frame),
                    slot=account.number,
                )
            else:
                session_button = self._button(
                    "Default profile" if account.is_active else "Session unsupported",
                    "showMain:",
                    self._local(layout.session, layout.frame),
                )
                session_button.setEnabled_(False)
            group.addSubview_(session_button)

        @objc.python_method
        def _usage_row(self, row: UsageRowViewModel, frame: Rect, parent) -> None:
            suffix = row.amount_text or f"{row.used_percent:.0f}%"
            if row.reset_text:
                suffix += f"  {row.reset_text}"
            marker = "!" if row.limit_reached else "+" if row.ahead_of_pace else " "
            field = self._label(f" {marker} {row.label:<11} {_bar(row.used_percent)} {suffix}", frame)
            if row.state is CapacityState.LIMIT_REACHED:
                field.setTextColor_(AppKit.NSColor.systemRedColor())
            elif row.state is CapacityState.NEAR_LIMIT:
                field.setTextColor_(AppKit.NSColor.systemOrangeColor())
            parent.addSubview_(field)

        @objc.python_method
        def _scroll_document(self, root, viewport: Rect, document_height: float, *, identifier: str):
            scroll = AppKit.NSScrollView.alloc().initWithFrame_(
                AppKit.NSMakeRect(viewport.x, viewport.y, viewport.width, viewport.height)
            )
            scroll.setIdentifier_(identifier)
            scroll.setHasVerticalScroller_(True)
            scroll.setAutohidesScrollers_(True)
            scroll.setDrawsBackground_(False)
            document = FlippedView.alloc().initWithFrame_(
                AppKit.NSMakeRect(0, 0, viewport.width, document_height)
            )
            scroll.setDocumentView_(document)
            root.addSubview_(scroll)
            return scroll, document

        @objc.python_method
        def reset_main_scroll_on_next_render(self) -> None:
            """Start the next popover opening at the top of the account list."""
            self._reset_main_scroll_on_render = True

        @objc.python_method
        def _resize_root(self, root, frame: Rect) -> None:
            root.setFrameSize_(AppKit.NSMakeSize(frame.width, frame.height))
            self.host.popover.setContentSize_(AppKit.NSMakeSize(frame.width, frame.height))

        @objc.python_method
        def _render_main(self, root, model: MenuBarPopoverViewModel, scroll_offset: float, layout) -> None:
            root.addSubview_(self._separator(Rect(0.0, layout.header.max_y - 1.0, POPOVER_WIDTH, 1.0)))
            root.addSubview_(self._label("CLAUDE SWAP / CAPACITY", layout.title))
            feedback = self.host.last_message or "Store-backed quota snapshot"
            root.addSubview_(
                self._label(feedback, Rect(layout.title.x, 33.0, 390.0, 18.0), dim=True)
            )
            scroll, document = self._scroll_document(
                root,
                layout.accounts_viewport,
                layout.accounts_document_height,
                identifier="accounts",
            )
            if model.accounts:
                for index, (account, account_layout) in enumerate(
                    zip(model.accounts, layout.accounts, strict=True)
                ):
                    self._account_group(
                        account, account_layout, document, show_separator=index > 0
                    )
            else:
                document.addSubview_(
                    self._label(
                        "No managed accounts. Add one from More…", Rect(14.0, 14.0, 390.0, 20.0), dim=True
                    )
                )
            root.addSubview_(self._separator(Rect(0.0, layout.footer.y, POPOVER_WIDTH, 1.0)))
            root.addSubview_(self._button("Rotate next", "rotateNext:", layout.rotate_next))
            root.addSubview_(self._button("Switch best", "switchBest:", layout.switch_best))
            root.addSubview_(self._button("Next available", "nextAvailable:", layout.next_available))
            root.addSubview_(self._button("Refresh", "refreshNow:", layout.refresh))
            root.addSubview_(self._button("More…", "showOverflow:", layout.more))
            root.addSubview_(self._button("Settings", "showSettings:", layout.settings))
            maximum_offset = max(0.0, layout.accounts_document_height - layout.accounts_viewport.height)
            scroll.contentView().scrollToPoint_(AppKit.NSMakePoint(0.0, min(scroll_offset, maximum_offset)))
            scroll.reflectScrolledClipView_(scroll.contentView())

        @objc.python_method
        def _render_secondary_header(self, document, layout, title: str, subtitle: str) -> None:
            document.addSubview_(self._label(title, layout.title))
            document.addSubview_(self._label(subtitle, layout.subtitle, dim=True))

        @objc.python_method
        def _overflow(self, document, model: MenuBarPopoverViewModel) -> None:
            item_heights = [34.0 for _ in model.accounts] + [BUTTON_HEIGHT] * 4
            layout = plan_scroll_screen(item_heights)
            self._render_secondary_header(
                document, layout, "MORE ACCOUNT CONTROLS", "Manage accounts and inspect the switch log"
            )
            for account, frame in zip(model.accounts, layout.items, strict=False):
                document.addSubview_(
                    self._label(f"#{account.number}  {account.display_name}", Rect(frame.x, frame.y + 4.0, 220.0, 20.0))
                )
                left, right = self._button_pair(Rect(245.0, frame.y + 4.0, 157.0, BUTTON_HEIGHT))
                state = "Enable" if account.disabled else "Disable"
                document.addSubview_(self._button(state, "toggleDisabled:", left, slot=account.number))
                document.addSubview_(self._button("Remove", "removeAccount:", right, slot=account.number))
            controls = layout.items[len(model.accounts):]
            add_current, add_token = self._button_pair(controls[0])
            refresh_creds, settings = self._button_pair(controls[1])
            history, reveal = self._button_pair(controls[2])
            quit_button, back = self._button_pair(controls[3])
            for title, action, frame in (
                ("Add current", "addCurrent:", add_current),
                ("Add token", "addToken:", add_token),
                ("Refresh creds", "refreshCredentials:", refresh_creds),
                ("Settings", "showSettings:", settings),
                ("Switch history", "showHistory:", history),
                ("Reveal full log", "revealLog:", reveal),
                ("Quit", "quit:", quit_button),
                ("Back", "showMain:", back),
            ):
                document.addSubview_(self._button(title, action, frame))

        @objc.python_method
        def _settings(self, document) -> None:
            layout = plan_scroll_screen((BUTTON_HEIGHT, 64.0, 64.0, 64.0, BUTTON_HEIGHT))
            self._render_secondary_header(document, layout, "DISPLAY AND AUTO-SWITCH", "Selections save immediately")
            toggle, scoped = self._button_pair(layout.items[0])
            settings = self.host.controller.settings
            toggle_title = "Hide account name" if settings.show_account_name else "Show account name"
            scoped_title = "Hide model limits" if settings.title_scoped else "Show model limits"
            document.addSubview_(self._button(toggle_title, "toggleAccountName:", toggle))
            document.addSubview_(self._button(scoped_title, "toggleScoped:", scoped))

            title_section = layout.items[1]
            document.addSubview_(self._label("Title percentage", Rect(title_section.x, title_section.y, 180.0, 18.0), dim=True))
            for index, mode in enumerate(TITLE_PCT_CHOICES):
                title = {"off": "Off", "5h": "5h", "7d": "7d", "both": "Both"}[mode]
                mark = "✓ " if settings.title_pct == mode else ""
                document.addSubview_(
                    self._button(
                        mark + title,
                        "setTitlePercent:",
                        Rect(14.0 + index * 99.0, title_section.y + 24.0, 91.0, BUTTON_HEIGHT),
                        slot=mode,
                    )
                )

            refresh_section = layout.items[2]
            document.addSubview_(self._label("Snapshot refresh", Rect(refresh_section.x, refresh_section.y, 180.0, 18.0), dim=True))
            for index, seconds in enumerate(REFRESH_CHOICES):
                mark = "✓ " if settings.refresh_interval == seconds else ""
                title = f"{seconds // 60} min" if seconds >= 60 else "30 sec"
                document.addSubview_(
                    self._button(
                        mark + title,
                        "setRefreshInterval:",
                        Rect(14.0 + index * 132.0, refresh_section.y + 24.0, 124.0, BUTTON_HEIGHT),
                        slot=str(seconds),
                    )
                )

            auto_section = layout.items[3]
            enabled = settings.auto_switch_enabled
            auto_title = "Auto-switch: on" if enabled else "Auto-switch: off"
            document.addSubview_(
                self._button(auto_title, "toggleAutoSwitch:", Rect(14.0, auto_section.y, 192.0, BUTTON_HEIGHT))
            )
            threshold = self.host.controller.auto_switch_threshold()
            document.addSubview_(
                self._label(f"Threshold: {threshold}%", Rect(216.0, auto_section.y, 120.0, 18.0), dim=True)
            )
            for index, percent in enumerate(AUTO_THRESHOLD_CHOICES):
                mark = "✓ " if threshold == percent else ""
                document.addSubview_(
                    self._button(
                        mark + f"{percent}%",
                        "setThreshold:",
                        Rect(14.0 + index * 99.0, auto_section.y + 32.0, 91.0, BUTTON_HEIGHT),
                        slot=str(percent),
                    )
                )
            document.addSubview_(self._button("Back", "showMain:", layout.items[4]))

        @objc.python_method
        def _history(self, document) -> None:
            entries = self.host.controller.history
            item_heights = [22.0 for _ in entries] if entries else [22.0]
            item_heights.append(BUTTON_HEIGHT)
            layout = plan_scroll_screen(item_heights)
            self._render_secondary_header(document, layout, "SWITCH HISTORY", "Most recent first")
            if entries:
                for entry, frame in zip(entries, layout.items, strict=False):
                    document.addSubview_(self._label(entry, frame))
            else:
                document.addSubview_(self._label("No switches logged yet", layout.items[0], dim=True))
            reveal, back = self._button_pair(layout.items[-1])
            document.addSubview_(self._button("Reveal full log", "revealLog:", reveal))
            document.addSubview_(self._button("Back", "showOverflow:", back))

        def renderModel_screen_(self, model, screen: str) -> None:
            root = self.view()
            scroll_offset = 0.0
            if screen == "main" and not self._reset_main_scroll_on_render:
                for child in root.subviews():
                    if isinstance(child, AppKit.NSScrollView) and child.identifier() == "accounts":
                        scroll_offset = child.documentVisibleRect().origin.y
                        break
            self._reset_main_scroll_on_render = False
            for child in tuple(root.subviews()):
                child.removeFromSuperview()
            if screen == "main":
                layout = plan_main_popover(tuple(len(account.rows) for account in model.accounts))
                self._resize_root(root, layout.root)
                self._render_main(root, model, scroll_offset, layout)
                return
            if screen == "overflow":
                item_heights = [34.0 for _ in model.accounts] + [BUTTON_HEIGHT] * 4
            elif screen == "settings":
                item_heights = [BUTTON_HEIGHT, 64.0, 64.0, 64.0, BUTTON_HEIGHT]
            else:
                entries = self.host.controller.history
                item_heights = [22.0 for _ in entries] if entries else [22.0]
                item_heights.append(BUTTON_HEIGHT)
            layout = plan_scroll_screen(item_heights)
            self._resize_root(root, layout.viewport)
            _scroll, document = self._scroll_document(
                root, layout.viewport, layout.document_height, identifier=screen
            )
            if screen == "overflow":
                self._overflow(document, model)
            elif screen == "settings":
                self._settings(document)
            else:
                self._history(document)

    class NativeMenuBarHost(Foundation.NSObject):
        """AppKit target object. It confirms intent, then delegates all work."""

        def initWithSwitcher_(self, initial_switcher):
            self = objc.super(NativeMenuBarHost, self).init()
            if self is None:
                return None
            self.screen = "main"
            self.last_message = ""
            self.controller = MenuBarController(
                initial_switcher,
                dispatch_main=self._dispatch_main,
            )
            self.status_item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(
                AppKit.NSVariableStatusItemLength
            )
            button = self.status_item.button()
            button.setTitle_(ICON)
            button.setTarget_(self)
            button.setAction_("togglePopover:")
            self.content = PopoverContentController.alloc().initWithHost_(self)
            self.popover = AppKit.NSPopover.alloc().init()
            self.popover.setBehavior_(AppKit.NSPopoverBehaviorTransient)
            self.popover.setContentViewController_(self.content)
            self.popover.setContentSize_(AppKit.NSMakeSize(POPOVER_WIDTH, POPOVER_MAX_HEIGHT))
            self.controller.bind_ui(self._render, self._message)
            self._install_refresh_timer()
            self._install_active_account_watcher()
            self.controller.start()
            return self

        @objc.python_method
        def _dispatch_main(self, callback: Callable[[], None]) -> None:
            AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(callback)

        @objc.python_method
        def _render(self, model: MenuBarPopoverViewModel, title: str) -> None:
            self.status_item.button().setTitle_(title)
            timer = getattr(self, "refresh_timer", None)
            if timer is not None and int(round(timer.timeInterval())) != self.controller.settings.refresh_interval:
                self._install_refresh_timer()
            self.content.renderModel_screen_(model, self.screen)

        @objc.python_method
        def _message(self, title: str, message: str) -> None:
            # Persist controller feedback in the primary view so a transient
            # popover never loses an error after it closes.
            self.last_message = f"{title}: {message}"
            self.screen = "main"
            self._render(self.controller.view_model, self.controller.title)

        @objc.python_method
        def _install_refresh_timer(self, seconds: int | None = None) -> None:
            old_timer = getattr(self, "refresh_timer", None)
            if old_timer is not None:
                old_timer.invalidate()
            interval = seconds if seconds is not None else self.controller.settings.refresh_interval
            self.refresh_timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                float(interval), self, "refreshTick:", None, True
            )

        def refreshTick_(self, _timer) -> None:
            self.controller.refresh_async()

        @objc.python_method
        def _install_active_account_watcher(self) -> None:
            self.active_account_timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                1.0, self, "activeAccountTick:", None, True
            )

        def activeAccountTick_(self, _timer) -> None:
            self.controller.detect_external_active_account()

        def togglePopover_(self, _sender) -> None:
            if self.popover.isShown():
                self.popover.performClose_(self)
                return
            self.screen = "main"
            self.content.reset_main_scroll_on_next_render()
            self._render(self.controller.view_model, self.controller.title)
            self.popover.showRelativeToRect_ofView_preferredEdge_(
                self.status_item.button().bounds(), self.status_item.button(), AppKit.NSMinYEdge
            )

        @objc.python_method
        def _confirm(self, title: str, message: str, action: str) -> bool:
            alert = AppKit.NSAlert.alloc().init()
            alert.setMessageText_(title)
            alert.setInformativeText_(message)
            alert.addButtonWithTitle_(action)
            alert.addButtonWithTitle_("Cancel")
            AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            return alert.runModal() == AppKit.NSAlertFirstButtonReturn

        def makeActive_(self, sender) -> None:
            slot = str(sender.representedObject())
            if self._confirm("Make account active", f"Make account {slot} the default Claude Code login?", "Make active"):
                # The controller worker calls only switcher.switch_to(slot, json_output=True).
                self.controller.make_active(slot)

        def isolatedSession_(self, sender) -> None:
            slot = str(sender.representedObject())
            if self._confirm("Launch isolated session", f"Open Terminal with cswap run {slot}?", "Launch"):
                # This deliberately hands the isolated session to the local CLI process.
                self.controller.launch_isolated_session(slot)

        def rotateNext_(self, _sender) -> None:
            self.controller.rotate(None)

        def switchBest_(self, _sender) -> None:
            self.controller.rotate("best")

        def nextAvailable_(self, _sender) -> None:
            self.controller.rotate("next-available")

        def addCurrent_(self, _sender) -> None:
            self.controller.add_current_login()

        def addToken_(self, _sender) -> None:
            alert = AppKit.NSAlert.alloc().init()
            alert.setMessageText_("Add account from setup token")
            alert.setInformativeText_("Enter the account email and setup token.")
            form = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 330, 52))
            email = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(0, 28, 330, 24))
            email.setPlaceholderString_("Email")
            token = AppKit.NSSecureTextField.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 330, 24))
            token.setPlaceholderString_("sk-ant-oat01-…")
            form.addSubview_(email)
            form.addSubview_(token)
            alert.setAccessoryView_(form)
            alert.addButtonWithTitle_("Add")
            alert.addButtonWithTitle_("Cancel")
            AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            if alert.runModal() == AppKit.NSAlertFirstButtonReturn and email.stringValue().strip() and token.stringValue().strip():
                self.controller.add_setup_token(email.stringValue().strip(), token.stringValue().strip())

        def refreshCredentials_(self, _sender) -> None:
            self.controller.refresh_current_credentials()

        def showOverflow_(self, _sender) -> None:
            self.screen = "overflow"
            self._render(self.controller.view_model, self.controller.title)

        def showSettings_(self, _sender) -> None:
            self.screen = "settings"
            self._render(self.controller.view_model, self.controller.title)

        def showHistory_(self, _sender) -> None:
            self.screen = "history"
            self.controller.load_history_async()
            self._render(self.controller.view_model, self.controller.title)

        def showMain_(self, _sender) -> None:
            self.screen = "main"
            self._render(self.controller.view_model, self.controller.title)

        def revealLog_(self, _sender) -> None:
            self.controller.reveal_log_async()

        def toggleDisabled_(self, sender) -> None:
            slot = str(sender.representedObject())
            account = next((item for item in self.controller.view_model.accounts if item.number == slot), None)
            if account is not None:
                self.controller.set_disabled(slot, not account.disabled)

        def removeAccount_(self, sender) -> None:
            slot = str(sender.representedObject())
            if self._confirm("Remove account", f"Remove account {slot}? This cannot be undone.", "Remove"):
                self.controller.remove_account(slot)

        def toggleAccountName_(self, _sender) -> None:
            self.controller.update_title_preferences(
                show_account_name=not self.controller.settings.show_account_name
            )

        def toggleScoped_(self, _sender) -> None:
            self.controller.update_title_preferences(title_scoped=not self.controller.settings.title_scoped)

        def setTitlePercent_(self, sender) -> None:
            self.controller.update_title_preferences(title_pct=str(sender.representedObject()))

        def setRefreshInterval_(self, sender) -> None:
            seconds = int(str(sender.representedObject()))
            self.controller.set_refresh_interval(seconds)
            self._install_refresh_timer(seconds)

        def toggleAutoSwitch_(self, _sender) -> None:
            self.controller.set_auto_switch_enabled(not self.controller.settings.auto_switch_enabled)

        def setThreshold_(self, sender) -> None:
            self.controller.set_auto_switch_threshold(int(str(sender.representedObject())))

        def refreshNow_(self, _sender) -> None:
            self.controller.refresh_async(full=True)

        def quit_(self, _sender) -> None:
            self._message("Quitting", "Finishing any active account operation before exit.")
            self.controller.stop(
                lambda: AppKit.NSApplication.sharedApplication().terminate_(self)
            )

    application = AppKit.NSApplication.sharedApplication()
    application.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    host = NativeMenuBarHost.alloc().initWithSwitcher_(switcher)
    # Retain the target object for the lifetime of the app; AppKit targets are weak.
    application.setDelegate_(host)
    _native_host = (application, host)
    return _native_host


def run_native_menubar(switcher: ClaudeAccountSwitcher) -> int:
    """Run the native status-item app until the user quits it."""
    application, _host = _create_native_menubar(switcher)
    application.run()
    return 0


__all__ = ["APPKIT_AVAILABLE", "require_appkit", "run_native_menubar"]
