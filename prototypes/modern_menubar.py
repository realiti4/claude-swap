#!/usr/bin/env python3
"""PROTOTYPE ONLY: native AppKit account-capacity menu bar comparison.

This file intentionally uses only the fixture data below. It has no project imports,
network calls, persistence, credential access, or account/session mutations.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

try:
    import objc

    from AppKit import (
        NSApplication,
        NSApplicationActivationPolicyAccessory,
        NSBezierPath,
        NSButton,
        NSColor,
        NSFont,
        NSImage,
        NSImageOnly,
        NSImageScaleProportionallyDown,
        NSLineBreakByTruncatingTail,
        NSMakeRect,
        NSMinYEdge,
        NSMakeSize,
        NSPopover,
        NSPopoverBehaviorTransient,
        NSScrollView,
        NSStatusBar,
        NSTextAlignmentRight,
        NSTextField,
        NSView,
        NSViewController,
    )
    from Foundation import NSObject
except (ImportError, SystemError):  # Lets fixture tests run without a supported PyObjC bridge.
    APPKIT_AVAILABLE = False
else:
    APPKIT_AVAILABLE = True


WINDOW_WIDTH: Final = 430
POPOVER_HEIGHT: Final = 640
CARD_INSET: Final = 16
CARD_WIDTH: Final = WINDOW_WIDTH - (CARD_INSET * 2)
STATUS_ITEM_LENGTH: Final = 28.0


class CapacityState(StrEnum):
    """The semantic state of a capacity reading, independent of its color."""

    AVAILABLE = "available"
    NEAR_LIMIT = "near_limit"
    LIMIT_REACHED = "limit_reached"
    UNAVAILABLE = "unavailable"


class FreshnessState(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class UsageFixture:
    """A single fixture-only usage reading expressed as percent used."""

    label: str
    used_percent: int | None
    reset_text: str
    scope: str


@dataclass(frozen=True)
class AccountFixture:
    """A fixture-only account card. These values never leave this process."""

    slot: int
    alias: str
    email: str
    is_active: bool
    is_disabled: bool
    freshness: FreshnessState
    freshness_detail: str
    usage: tuple[UsageFixture, ...]


@dataclass(frozen=True)
class UsageRowViewModel:
    label: str
    used_percent: int | None
    available_percent: int | None
    reset_text: str
    state: CapacityState
    state_label: str
    scope: str


@dataclass(frozen=True)
class AccountCardViewModel:
    slot: int
    alias: str
    email: str
    is_active: bool
    is_disabled: bool
    freshness: FreshnessState
    freshness_detail: str
    capacity_summary: str
    rows: tuple[UsageRowViewModel, ...]


def fixture_accounts() -> tuple[AccountFixture, ...]:
    """Return the fixed account set used by this prototype and its tests."""

    return (
        AccountFixture(
            1,
            "studio",
            "studio@example.test",
            True,
            False,
            FreshnessState.FRESH,
            "Updated just now",
            (
                UsageFixture("5h", 42, "Resets in 2h 14m", "rolling"),
                UsageFixture("Weekly", 57, "Resets Thu, 9:00 AM", "weekly"),
                UsageFixture("Fable", 18, "Resets Thu, 9:00 AM", "model"),
            ),
        ),
        AccountFixture(
            2,
            "research",
            "research@example.test",
            False,
            False,
            FreshnessState.FRESH,
            "Updated 2 min ago",
            (
                UsageFixture("5h", 68, "Resets in 1h 08m", "rolling"),
                UsageFixture("Weekly", 42, "Resets Wed, 6:00 PM", "weekly"),
                UsageFixture("Fable", 98, "Resets Wed, 6:00 PM", "model"),
            ),
        ),
        AccountFixture(
            3,
            "personal",
            "personal@example.test",
            False,
            False,
            FreshnessState.FRESH,
            "Updated 4 min ago",
            (
                UsageFixture("5h", 6, "Resets in 4h 31m", "rolling"),
                UsageFixture("Weekly", 24, "Resets Fri, 7:00 AM", "weekly"),
                UsageFixture("Fable", 12, "Resets Fri, 7:00 AM", "model"),
            ),
        ),
        AccountFixture(
            4,
            "agency",
            "agency@example.test",
            False,
            True,
            FreshnessState.FRESH,
            "Updated 6 min ago",
            (
                UsageFixture("5h", 84, "Resets in 46m", "rolling"),
                UsageFixture("Weekly", 91, "Resets Tue, 2:00 PM", "weekly"),
                UsageFixture("Fable", 100, "Resets Tue, 2:00 PM", "model"),
            ),
        ),
        AccountFixture(
            5,
            "night shift",
            "night@example.test",
            False,
            False,
            FreshnessState.STALE,
            "Last confirmed 19 min ago",
            (
                UsageFixture("5h", 11, "Reset estimate in 3h 41m", "rolling"),
                UsageFixture("Weekly", 16, "Reset estimate Fri, 9:00 AM", "weekly"),
                UsageFixture("Fable", 9, "Reset estimate Fri, 9:00 AM", "model"),
            ),
        ),
        AccountFixture(
            6,
            "archive",
            "archive@example.test",
            False,
            False,
            FreshnessState.UNAVAILABLE,
            "No fixture reading available",
            (
                UsageFixture("5h", None, "Usage unavailable", "rolling"),
                UsageFixture("Weekly", None, "Usage unavailable", "weekly"),
                UsageFixture("Fable", None, "Usage unavailable", "model"),
            ),
        ),
        AccountFixture(
            7,
            "automation",
            "automation@example.test",
            False,
            False,
            FreshnessState.FRESH,
            "Updated 8 min ago",
            (
                UsageFixture("5h", 36, "Resets in 2h 53m", "rolling"),
                UsageFixture("Weekly", 71, "Resets Thu, 1:00 PM", "weekly"),
                UsageFixture("Fable", 53, "Resets Thu, 1:00 PM", "model"),
            ),
        ),
        AccountFixture(
            8,
            "sandbox",
            "sandbox@example.test",
            False,
            False,
            FreshnessState.FRESH,
            "Updated 11 min ago",
            (
                UsageFixture("5h", 73, "Resets in 1h 27m", "rolling"),
                UsageFixture("Weekly", 38, "Resets Sat, 10:00 AM", "weekly"),
                UsageFixture("Fable", 64, "Resets Sat, 10:00 AM", "model"),
            ),
        ),
        AccountFixture(
            9,
            "travel",
            "travel@example.test",
            False,
            False,
            FreshnessState.FRESH,
            "Updated 14 min ago",
            (
                UsageFixture("5h", 21, "Resets in 3h 19m", "rolling"),
                UsageFixture("Weekly", 8, "Resets Mon, 8:00 AM", "weekly"),
                UsageFixture("Fable", 33, "Resets Mon, 8:00 AM", "model"),
            ),
        ),
    )


def capacity_state(used_percent: int | None) -> CapacityState:
    """Classify percent-used capacity into text-first semantic states."""

    if used_percent is None:
        return CapacityState.UNAVAILABLE
    if used_percent >= 90:
        return CapacityState.LIMIT_REACHED
    if used_percent >= 70:
        return CapacityState.NEAR_LIMIT
    return CapacityState.AVAILABLE


def state_label(state: CapacityState) -> str:
    """Return an accessible text label so state is never color-only."""

    return {
        CapacityState.AVAILABLE: "Available",
        CapacityState.NEAR_LIMIT: "Near limit",
        CapacityState.LIMIT_REACHED: "Limit reached",
        CapacityState.UNAVAILABLE: "Unavailable",
    }[state]


def shape_usage_row(usage: UsageFixture) -> UsageRowViewModel:
    """Turn a raw fixture reading into a stable, testable presentation model."""

    state = capacity_state(usage.used_percent)
    available = None if usage.used_percent is None else 100 - usage.used_percent
    return UsageRowViewModel(
        label=usage.label,
        used_percent=usage.used_percent,
        available_percent=available,
        reset_text=usage.reset_text,
        state=state,
        state_label=state_label(state),
        scope=usage.scope,
    )


def shape_account_card(account: AccountFixture) -> AccountCardViewModel:
    """Build one card's display state without importing AppKit."""

    rows = tuple(shape_usage_row(item) for item in account.usage)
    available = [row.available_percent for row in rows if row.available_percent is not None]
    if not available:
        summary = "Capacity unavailable"
    else:
        summary = f"{min(available)}% minimum capacity"
    return AccountCardViewModel(
        slot=account.slot,
        alias=account.alias,
        email=account.email,
        is_active=account.is_active,
        is_disabled=account.is_disabled,
        freshness=account.freshness,
        freshness_detail=account.freshness_detail,
        capacity_summary=summary,
        rows=rows,
    )


def fixture_view_models() -> tuple[AccountCardViewModel, ...]:
    """Shape all fixture cards for the native view."""

    return tuple(shape_account_card(account) for account in fixture_accounts())


if APPKIT_AVAILABLE:

    def _label(text: str, font: object, color: object, alignment: int = 0) -> NSTextField:
        field = NSTextField.labelWithString_(text)
        field.setFont_(font)
        field.setTextColor_(color)
        field.setAlignment_(alignment)
        field.setLineBreakMode_(NSLineBreakByTruncatingTail)
        field.setUsesSingleLineMode_(True)
        return field


    def _symbol(name: str, description: str) -> NSImage:
        image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, description)
        if image is None:
            image = NSImage.imageNamed_("NSRefreshTemplate")
        image.setTemplate_(True)
        return image


    def _state_color(state: CapacityState) -> NSColor:
        return {
            CapacityState.AVAILABLE: NSColor.systemGreenColor(),
            CapacityState.NEAR_LIMIT: NSColor.systemOrangeColor(),
            CapacityState.LIMIT_REACHED: NSColor.systemRedColor(),
            CapacityState.UNAVAILABLE: NSColor.tertiaryLabelColor(),
        }[state]


    class CapacityMeter(NSView):
        """A semantic, text-backed horizontal percent-used meter."""

        def initWithPercent_state_(self, percent: int | None, state: CapacityState) -> "CapacityMeter":
            self = objc.super(CapacityMeter, self).initWithFrame_(NSMakeRect(0, 0, 100, 7))
            if self is None:
                return self
            self.percent = percent
            self.state = state
            self.setToolTip_("Fixture-only capacity indicator")
            return self

        def drawRect_(self, _dirty_rect: object) -> None:
            bounds = self.bounds()
            radius = bounds.size.height / 2
            NSColor.quaternaryLabelColor().setFill()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(bounds, radius, radius).fill()
            if self.percent is None or self.percent <= 0:
                return
            fill_width = max(3, bounds.size.width * self.percent / 100)
            fill = NSMakeRect(bounds.origin.x, bounds.origin.y, fill_width, bounds.size.height)
            _state_color(self.state).setFill()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(fill, radius, radius).fill()


    class UsageRow(NSView):
        def initWithModel_(self, model: UsageRowViewModel) -> "UsageRow":
            self = objc.super(UsageRow, self).initWithFrame_(NSMakeRect(0, 0, CARD_WIDTH - 28, 49))
            if self is None:
                return self
            self.model = model
            self.title = _label(model.label, NSFont.systemFontOfSize_weight_(12, 0.0), NSColor.labelColor())
            self.value = _label(
                "—" if model.used_percent is None else f"{model.used_percent}% used",
                NSFont.monospacedDigitSystemFontOfSize_weight_(12, 0.0),
                NSColor.secondaryLabelColor(),
                NSTextAlignmentRight,
            )
            self.detail = _label(
                f"{model.state_label} · {model.reset_text}",
                NSFont.systemFontOfSize_(10),
                _state_color(model.state) if model.state != CapacityState.UNAVAILABLE else NSColor.tertiaryLabelColor(),
            )
            self.meter = CapacityMeter.alloc().initWithPercent_state_(model.used_percent, model.state)
            for view in (self.title, self.value, self.detail, self.meter):
                self.addSubview_(view)
            return self

        def layout(self) -> None:
            width = self.bounds().size.width
            self.title.setFrame_(NSMakeRect(0, 29, 92, 16))
            self.value.setFrame_(NSMakeRect(width - 84, 29, 84, 16))
            self.meter.setFrame_(NSMakeRect(0, 19, width, 6))
            self.detail.setFrame_(NSMakeRect(0, 2, width, 14))


    class Badge(NSView):
        def initWithText_style_(self, text: str, style: str) -> "Badge":
            self = objc.super(Badge, self).initWithFrame_(NSMakeRect(0, 0, 56, 20))
            if self is None:
                return self
            self.text = text
            self.style = style
            color = NSColor.systemBlueColor() if style == "active" else NSColor.secondaryLabelColor()
            self.label = _label(text, NSFont.systemFontOfSize_weight_(10, 0.4), color, NSTextAlignmentRight)
            self.addSubview_(self.label)
            self.setToolTip_("Fixture state badge")
            return self

        def layout(self) -> None:
            self.label.setFrame_(self.bounds())

        def drawRect_(self, _dirty_rect: object) -> None:
            bounds = self.bounds()
            color = NSColor.systemBlueColor() if self.style == "active" else NSColor.quaternaryLabelColor()
            color.colorWithAlphaComponent_(0.14).setFill()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(bounds, 8, 8).fill()


    class FlippedDocumentView(NSView):
        """Makes the first fixture card the initial scroll position."""

        def isFlipped(self) -> bool:
            return True


    class AccountCard(NSView):
        def initWithModel_target_(self, model: AccountCardViewModel, target: object) -> "AccountCard":
            height = 249
            self = objc.super(AccountCard, self).initWithFrame_(NSMakeRect(0, 0, CARD_WIDTH, height))
            if self is None:
                return self
            self.model = model
            self.setWantsLayer_(True)
            self.alias = _label(model.alias, NSFont.systemFontOfSize_weight_(15, 0.2), NSColor.labelColor())
            self.email = _label(model.email, NSFont.systemFontOfSize_(11), NSColor.secondaryLabelColor())
            self.summary = _label(
                model.capacity_summary,
                NSFont.monospacedDigitSystemFontOfSize_weight_(11, 0.0),
                NSColor.secondaryLabelColor(),
                NSTextAlignmentRight,
            )
            self.freshness = _label(
                self._freshness_text(),
                NSFont.systemFontOfSize_(10),
                self._freshness_color(),
            )
            for view in (self.alias, self.email, self.summary, self.freshness):
                self.addSubview_(view)
            self.badge = None
            if model.is_active:
                self.badge = Badge.alloc().initWithText_style_("ACTIVE", "active")
            elif model.is_disabled:
                self.badge = Badge.alloc().initWithText_style_("HELD", "disabled")
            if self.badge is not None:
                self.addSubview_(self.badge)

            self.rows = [UsageRow.alloc().initWithModel_(row) for row in model.rows]
            for row in self.rows:
                self.addSubview_(row)

            self.activate_button = NSButton.buttonWithTitle_target_action_("Make Active", target, "makeActive:")
            self.activate_button.setTag_(model.slot)
            self.activate_button.setToolTip_(f"Prototype feedback for {model.alias}; does not change accounts")
            self.activate_button.setAccessibilityLabel_(f"Make {model.alias} active, prototype only")
            self.session_button = NSButton.buttonWithTitle_target_action_("Launch Isolated Session", target, "launchSession:")
            self.session_button.setTag_(model.slot)
            self.session_button.setToolTip_(f"Prototype feedback for {model.alias}; does not launch a session")
            self.session_button.setAccessibilityLabel_(f"Launch an isolated session for {model.alias}, prototype only")
            self.addSubview_(self.activate_button)
            self.addSubview_(self.session_button)
            return self

        def _freshness_text(self) -> str:
            prefix = {
                FreshnessState.FRESH: "Fresh",
                FreshnessState.STALE: "Stale",
                FreshnessState.UNAVAILABLE: "Unavailable",
            }[self.model.freshness]
            return f"{prefix} · {self.model.freshness_detail}"

        def _freshness_color(self) -> NSColor:
            if self.model.freshness == FreshnessState.FRESH:
                return NSColor.secondaryLabelColor()
            if self.model.freshness == FreshnessState.STALE:
                return NSColor.systemOrangeColor()
            return NSColor.systemRedColor()

        def layout(self) -> None:
            width = self.bounds().size.width
            self.alias.setFrame_(NSMakeRect(14, 219, 130, 19))
            self.email.setFrame_(NSMakeRect(14, 202, 230, 14))
            self.summary.setFrame_(NSMakeRect(width - 160, 219, 146, 16))
            self.freshness.setFrame_(NSMakeRect(14, 184, width - 28, 14))
            if self.badge is not None:
                self.badge.setFrame_(NSMakeRect(width - 62, 198, 48, 18))
            y = 132
            for row in self.rows:
                row.setFrame_(NSMakeRect(14, y, width - 28, 49))
                y -= 49
            self.activate_button.setFrame_(NSMakeRect(14, 4, 108, 28))
            self.session_button.setFrame_(NSMakeRect(width - 222, 4, 208, 28))

        def drawRect_(self, _dirty_rect: object) -> None:
            bounds = self.bounds()
            background = NSColor.controlBackgroundColor()
            background.setFill()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(bounds, 11, 11).fill()
            border = NSColor.separatorColor().colorWithAlphaComponent_(0.65)
            border.setStroke()
            path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(0.5, 0.5, bounds.size.width - 1, bounds.size.height - 1), 11, 11
            )
            path.setLineWidth_(1)
            path.stroke()


    class PrototypeViewController(NSViewController):
        """Owns the popover view and provides non-mutating prototype feedback."""

        def init(self) -> "PrototypeViewController":
            self = objc.super(PrototypeViewController, self).init()
            if self is None:
                return self
            self.models = fixture_view_models()
            self.models_by_slot = {model.slot: model for model in self.models}
            self.root_view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, WINDOW_WIDTH, POPOVER_HEIGHT))
            self.setView_(self.root_view)
            self._build_view()
            return self

        def _build_view(self) -> None:
            title = _label("Account capacity", NSFont.systemFontOfSize_weight_(18, 0.3), NSColor.labelColor())
            subtitle = _label(
                f"Fixture data only · {len(self.models)} accounts",
                NSFont.systemFontOfSize_(11),
                NSColor.secondaryLabelColor(),
            )
            auto = _label(
                "Automatic rotation  ·  On at 85%", NSFont.systemFontOfSize_(11), NSColor.secondaryLabelColor()
            )
            self.feedback = _label(
                "Select an action to see prototype-only feedback.",
                NSFont.systemFontOfSize_(11),
                NSColor.secondaryLabelColor(),
            )
            self.refresh_button = NSButton.buttonWithTitle_target_action_("", self, "refreshFixture:")
            self.refresh_button.setImage_(_symbol("arrow.clockwise", "Refresh fixture display"))
            self.refresh_button.setImagePosition_(NSImageOnly)
            self.refresh_button.setBezelStyle_(1)
            self.refresh_button.setToolTip_("Refresh fixture display. No request is made.")
            self.refresh_button.setAccessibilityLabel_("Refresh fixture display, prototype only")
            self.quit_button = NSButton.buttonWithTitle_target_action_("Quit Prototype", self, "quitPrototype:")
            self.quit_button.setToolTip_("Close this fixture-only prototype")
            self.quit_button.setAccessibilityLabel_("Quit account capacity prototype")
            for view in (title, subtitle, auto, self.feedback, self.quit_button, self.refresh_button):
                self.root_view.addSubview_(view)
            title.setFrame_(NSMakeRect(16, 602, 200, 24))
            subtitle.setFrame_(NSMakeRect(16, 582, 260, 16))
            auto.setFrame_(NSMakeRect(16, 557, 250, 17))
            self.feedback.setFrame_(NSMakeRect(16, 536, 360, 16))
            self.quit_button.setFrame_(NSMakeRect(WINDOW_WIDTH - 160, 594, 100, 26))
            self.refresh_button.setFrame_(NSMakeRect(WINDOW_WIDTH - 48, 588, 32, 32))

            scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, WINDOW_WIDTH, 522))
            scroll.setHasVerticalScroller_(True)
            scroll.setAutohidesScrollers_(True)
            scroll.setDrawsBackground_(False)
            cards_height = len(self.models) * 261 + 12
            cards = FlippedDocumentView.alloc().initWithFrame_(NSMakeRect(0, 0, WINDOW_WIDTH, cards_height))
            y = 12
            for model in self.models:
                card = AccountCard.alloc().initWithModel_target_(model, self)
                card.setFrame_(NSMakeRect(CARD_INSET, y, CARD_WIDTH, 249))
                cards.addSubview_(card)
                y += 261
            scroll.setDocumentView_(cards)
            self.root_view.addSubview_(scroll)

        def makeActive_(self, sender: NSButton) -> None:
            account = self.models_by_slot[sender.tag()]
            self.feedback.setStringValue_(
                f"Prototype only: would make {account.alias} active. Nothing changed."
            )

        def launchSession_(self, sender: NSButton) -> None:
            account = self.models_by_slot[sender.tag()]
            self.feedback.setStringValue_(
                f"Prototype only: would launch an isolated session for {account.alias}."
            )

        def refreshFixture_(self, _sender: NSButton) -> None:
            self.feedback.setStringValue_("Fixture display refreshed locally. No request was made.")

        def quitPrototype_(self, _sender: NSButton) -> None:
            NSApplication.sharedApplication().terminate_(None)


    class AppDelegate(NSObject):
        def applicationDidFinishLaunching_(self, _notification: object) -> None:
            if getattr(self, "status_item", None) is not None:
                return
            self.controller = PrototypeViewController.alloc().init()
            self.popover = NSPopover.alloc().init()
            self.popover.setBehavior_(NSPopoverBehaviorTransient)
            self.popover.setContentSize_(NSMakeSize(WINDOW_WIDTH, POPOVER_HEIGHT))
            self.popover.setContentViewController_(self.controller)
            self.status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(STATUS_ITEM_LENGTH)
            button = self.status_item.button()
            button.setImage_(_symbol("arrow.triangle.2.circlepath", "Account capacity prototype"))
            button.setImageScaling_(NSImageScaleProportionallyDown)
            button.setImagePosition_(NSImageOnly)
            button.setToolTip_("Open account capacity prototype")
            button.setAccessibilityLabel_("Open account capacity prototype")
            button.setTarget_(self)
            button.setAction_("togglePopover:")

        def togglePopover_(self, _sender: NSButton) -> None:
            button = self.status_item.button()
            if self.popover.isShown():
                self.popover.performClose_(None)
                return
            self.popover.showRelativeToRect_ofView_preferredEdge_(button.bounds(), button, NSMinYEdge)
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)


    _app_delegate: AppDelegate | None = None


    def setup_application() -> AppDelegate:
        """Configure and retain native objects before the application event loop runs."""

        global _app_delegate
        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        if _app_delegate is None:
            _app_delegate = AppDelegate.alloc().init()
        app.setDelegate_(_app_delegate)
        return _app_delegate


def main() -> int:
    """Launch the fixture-only AppKit prototype."""

    if not APPKIT_AVAILABLE:
        print(
            "This prototype needs a macOS Python environment with PyObjC/AppKit installed. "
            "No dependency was installed automatically."
        )
        return 1
    delegate = setup_application()
    delegate.applicationDidFinishLaunching_(None)
    NSApplication.sharedApplication().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
