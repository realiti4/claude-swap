"""Native AppKit layout tests, skipped when the optional framework is absent."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from claude_swap import menubar_appkit
from claude_swap.menubar_layout import POPOVER_MAX_HEIGHT
from claude_swap.menubar_viewmodel import (
    CapacityState,
    FreshnessState,
    MenuBarPopoverViewModel,
    MenuBarSettings,
    PopoverAccountViewModel,
    UsageRowViewModel,
    UsageScope,
)


def _model(account_count: int, rows_per_account: int = 2) -> MenuBarPopoverViewModel:
    row = UsageRowViewModel(
        label="5h",
        scope=UsageScope.FIVE_HOUR,
        used_percent=40.0,
        available_percent=60.0,
        reset_text="2h 0m",
        state=CapacityState.AVAILABLE,
        state_label="Available",
    )
    accounts = tuple(
        PopoverAccountViewModel(
            number=str(index + 1),
            email=f"account-{index + 1}@example.test",
            alias="",
            display_name=f"account-{index + 1}",
            is_active=index == 0,
            disabled=False,
            freshness=FreshnessState.FRESH,
            freshness_detail="Updated just now",
            sentinel=None,
            sentinel_note=None,
            has_last_good=True,
            capacity_summary="60% minimum capacity",
            rows=(row,) * rows_per_account,
            session_available=index != 0,
        )
        for index in range(account_count)
    )
    return MenuBarPopoverViewModel(accounts, "1" if accounts else None)


def _intersects(first, second) -> bool:
    return (
        first.origin.x < second.origin.x + second.size.width
        and second.origin.x < first.origin.x + first.size.width
        and first.origin.y < second.origin.y + second.size.height
        and second.origin.y < first.origin.y + first.size.height
    )


@unittest.skipUnless(menubar_appkit.APPKIT_AVAILABLE, "requires macOS PyObjC/AppKit")
class NativeMenuBarAppKitSmokeTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls) -> None:
        cached = getattr(cls, "_native_host", None)
        if cached is None:
            return
        application, host = cached
        host.refresh_timer.invalidate()
        import AppKit

        AppKit.NSStatusBar.systemStatusBar().removeStatusItem_(host.status_item)
        application.setDelegate_(None)

    def _host(self, model: MenuBarPopoverViewModel):
        class FakeController:
            def __init__(self, *_args, **_kwargs) -> None:
                self.settings = MenuBarSettings()
                self.view_model = model
                self.title = "⇄"
                self.history = []

            def bind_ui(self, renderer, _message) -> None:
                renderer(self.view_model, self.title)

            def start(self) -> None:
                pass

            def stop(self) -> None:
                pass

            def auto_switch_threshold(self) -> int:
                return 90

        cached = getattr(self.__class__, "_native_host", None)
        if cached is not None:
            return cached
        with patch.object(menubar_appkit, "MenuBarController", FakeController):
            cached = menubar_appkit._create_native_menubar(object())
        self.__class__._native_host = cached
        return cached

    def _button_titles(self, view) -> list[str]:
        import AppKit

        titles: list[str] = []
        for child in view.subviews():
            if isinstance(child, AppKit.NSButton):
                titles.append(str(child.title()))
            titles.extend(self._button_titles(child))
        return titles

    def test_native_status_item_can_be_created_and_removed(self) -> None:
        import AppKit

        application = AppKit.NSApplication.sharedApplication()
        status_bar = AppKit.NSStatusBar.systemStatusBar()
        item = status_bar.statusItemWithLength_(AppKit.NSVariableStatusItemLength)
        try:
            item.button().setTitle_("⇄")
            self.assertEqual(item.button().title(), "⇄")
        finally:
            status_bar.removeStatusItem_(item)
        self.assertIsNotNone(application)

    def test_native_popover_constructs_fixed_chrome_and_flipped_accounts_document(self) -> None:
        """The main screen keeps only account groups inside its scroll view."""

        import AppKit

        application, host = self._host(_model(2, rows_per_account=3))
        host.content.renderModel_screen_(_model(2, rows_per_account=3), "main")
        try:
            root = host.content.view()
            self.assertLess(root.frame().size.height, POPOVER_MAX_HEIGHT)
            self.assertEqual(host.popover.contentSize().height, root.frame().size.height)
            scrolls = [
                child
                for child in root.subviews()
                if isinstance(child, AppKit.NSScrollView) and child.identifier() == "accounts"
            ]
            self.assertTrue(root.isFlipped())
            self.assertEqual(len(scrolls), 1)
            accounts_scroll = scrolls[0]
            document = accounts_scroll.documentView()
            self.assertTrue(document.isFlipped())
            self.assertEqual(accounts_scroll.documentVisibleRect().origin.y, 0.0)

            header = next(
                child
                for child in root.subviews()
                if isinstance(child, AppKit.NSTextField) and str(child.stringValue()) == "CLAUDE SWAP / CAPACITY"
            )
            self.assertIs(header.superview(), root)
            self.assertLessEqual(header.frame().origin.y + header.frame().size.height, accounts_scroll.frame().origin.y)

            root_buttons = [child for child in root.subviews() if isinstance(child, AppKit.NSButton)]
            self.assertEqual(
                {str(button.title()) for button in root_buttons},
                {"Rotate next", "Switch best", "Next available", "Refresh", "More…", "Settings"},
            )
            self.assertTrue(all(button.superview() is root for button in root_buttons))
            self.assertTrue(
                all(button.frame().origin.y >= accounts_scroll.frame().origin.y + accounts_scroll.frame().size.height for button in root_buttons)
            )
            self.assertTrue(
                all(not _intersects(accounts_scroll.frame(), button.frame()) for button in root_buttons)
            )

            groups = tuple(document.subviews())
            self.assertEqual(len(groups), 2)
            self.assertTrue(all(group.isFlipped() for group in groups))
            self.assertFalse(_intersects(groups[0].frame(), groups[1].frame()))
            titles = self._button_titles(document)
            self.assertEqual(titles.count("Make active"), 2)
            self.assertEqual(titles.count("Session"), 1)
            self.assertEqual(titles.count("Default profile"), 1)
            for group in groups:
                action_buttons = [
                    child
                    for child in group.subviews()
                    if isinstance(child, AppKit.NSButton)
                    and str(child.title()) in {"Make active", "Session", "Default profile"}
                ]
                self.assertEqual(len(action_buttons), 2)
                self.assertTrue(all(button.frame().size.width >= 150.0 for button in action_buttons))
            default_button = next(
                button
                for button in groups[0].subviews()
                if isinstance(button, AppKit.NSButton) and str(button.title()) == "Default profile"
            )
            self.assertFalse(default_button.isEnabled())

            host._message("Couldn't launch isolated session", "Terminal access was denied.")
            feedback = next(
                child
                for child in host.content.view().subviews()
                if isinstance(child, AppKit.NSTextField)
                and str(child.stringValue()).startswith("Couldn't launch isolated session:")
            )
            self.assertIs(feedback.superview(), host.content.view())
        finally:
            pass

    def test_long_account_list_scrolls_only_the_flipped_account_document(self) -> None:
        import AppKit

        application, host = self._host(_model(12, rows_per_account=4))
        host.content.renderModel_screen_(_model(12, rows_per_account=4), "main")
        try:
            root = host.content.view()
            self.assertEqual(root.frame().size.height, POPOVER_MAX_HEIGHT)
            self.assertEqual(host.popover.contentSize().height, POPOVER_MAX_HEIGHT)
            accounts_scroll = next(
                child
                for child in root.subviews()
                if isinstance(child, AppKit.NSScrollView) and child.identifier() == "accounts"
            )
            document = accounts_scroll.documentView()
            self.assertGreater(document.frame().size.height, accounts_scroll.frame().size.height)
            self.assertEqual(len([child for child in root.subviews() if isinstance(child, AppKit.NSScrollView)]), 1)
            self.assertTrue(all(group.frame().origin.y >= 0.0 for group in document.subviews()))
            self.assertTrue(
                all(
                    group.frame().origin.y + group.frame().size.height <= document.frame().size.height
                    for group in document.subviews()
                )
            )
            for first, second in zip(document.subviews(), document.subviews()[1:], strict=False):
                self.assertFalse(_intersects(first.frame(), second.frame()))
        finally:
            pass

    def test_refresh_keeps_account_scroll_but_popover_open_resets_it(self) -> None:
        import AppKit

        model = _model(12, rows_per_account=4)
        application, host = self._host(model)
        try:
            host.content.renderModel_screen_(model, "main")
            scroll = next(
                child
                for child in host.content.view().subviews()
                if isinstance(child, AppKit.NSScrollView) and child.identifier() == "accounts"
            )
            scroll.contentView().scrollToPoint_(AppKit.NSMakePoint(0.0, 100.0))
            scroll.reflectScrolledClipView_(scroll.contentView())
            host.content.renderModel_screen_(model, "main")
            refreshed_scroll = next(
                child
                for child in host.content.view().subviews()
                if isinstance(child, AppKit.NSScrollView) and child.identifier() == "accounts"
            )
            self.assertEqual(refreshed_scroll.documentVisibleRect().origin.y, 100.0)

            host.content.reset_main_scroll_on_next_render()
            host.content.renderModel_screen_(model, "main")
            reopened_scroll = next(
                child
                for child in host.content.view().subviews()
                if isinstance(child, AppKit.NSScrollView) and child.identifier() == "accounts"
            )
            self.assertEqual(reopened_scroll.documentVisibleRect().origin.y, 0.0)
        finally:
            pass

    def test_secondary_screens_use_flipped_top_down_scroll_documents(self) -> None:
        import AppKit

        model = _model(10, rows_per_account=1)
        application, host = self._host(model)
        try:
            host.content.renderModel_screen_(model, "overflow")
            scroll = next(child for child in host.content.view().subviews() if isinstance(child, AppKit.NSScrollView))
            document = scroll.documentView()
            self.assertTrue(document.isFlipped())
            self.assertGreaterEqual(document.frame().size.height, scroll.frame().size.height)
            self.assertTrue(all(child.frame().origin.y >= 0.0 for child in document.subviews()))

            host.controller.history = [f"{index} → {index + 1}" for index in range(40)]
            host.content.renderModel_screen_(model, "history")
            history_scroll = next(
                child for child in host.content.view().subviews() if isinstance(child, AppKit.NSScrollView)
            )
            self.assertTrue(history_scroll.documentView().isFlipped())
            self.assertGreater(
                history_scroll.documentView().frame().size.height, history_scroll.frame().size.height
            )
        finally:
            pass
