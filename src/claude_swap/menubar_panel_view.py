"""AppKit drawing for the menu bar usage bars (macOS, menubar extra only).

Turns an ``AccountPanel`` model (``menubar_panel``) into a view that an
``NSMenuItem`` shows in place of its title. There is no data logic here: the
model decides every number and state, and ``menubar_panel`` decides every
position. This module only measures fonts, picks semantic colours and draws.

Only semantic ``NSColor``s and the system font are used, so light mode, dark
mode and Increase Contrast all follow the system with nothing hard-coded.
Imported lazily by the rumps glue, like rumps itself.
"""

from __future__ import annotations

import logging

import objc
from AppKit import (
    NSAccessibilityButtonRole,
    NSAppearance,
    NSAppearanceNameAqua,
    NSAppearanceNameDarkAqua,
    NSAttributedString,
    NSBezierPath,
    NSBitmapImageFileTypePNG,
    NSColor,
    NSCompositingOperationClear,
    NSFont,
    NSFontAttributeName,
    NSFontWeightRegular,
    NSFontWeightSemibold,
    NSForegroundColorAttributeName,
    NSLineBreakByTruncatingMiddle,
    NSLineBreakByTruncatingTail,
    NSMouseInRect,
    NSMutableParagraphStyle,
    NSObject,
    NSParagraphStyleAttributeName,
    NSRectFillUsingOperation,
    NSView,
    NSViewWidthSizable,
)
from Foundation import NSMakeRect
from PyObjCTools import AppHelper

from claude_swap import menubar_panel as mp

_LIVE_VIEWS = [0]  # views alive right now; see live_view_count()
_ACTIVATE_KEYS = (36, 76)  # Return, keypad Enter


def live_view_count() -> int:
    """How many panel views exist, for the rebuild leak check."""
    return _LIVE_VIEWS[0]


# ---- fonts and colours -------------------------------------------------------------

def _fonts() -> dict:
    menu = NSFont.menuFontOfSize_(0.0)
    small = NSFont.smallSystemFontSize()
    return {
        "title": menu,
        "title_active": NSFont.systemFontOfSize_weight_(menu.pointSize(), NSFontWeightSemibold),
        "meta": NSFont.systemFontOfSize_(small),
        "label": NSFont.systemFontOfSize_(small),
        "digits": NSFont.monospacedDigitSystemFontOfSize_weight_(small, NSFontWeightRegular),
    }


def _fill_color(state: str):
    # The TUI's three severity colours, mapped to their system equivalents.
    if state in ("hot", "over"):
        return NSColor.systemRedColor()
    if state == "warn":
        return NSColor.systemYellowColor()
    return NSColor.systemGreenColor()


def _line_h(font) -> float:
    return mp.line_height(font.ascender(), font.descender(), font.leading())


def _text(string: str, font, color, truncate=None):
    attrs = {NSFontAttributeName: font, NSForegroundColorAttributeName: color}
    if truncate is not None:
        style = NSMutableParagraphStyle.alloc().init()
        style.setLineBreakMode_(truncate)
        attrs[NSParagraphStyleAttributeName] = style
    return NSAttributedString.alloc().initWithString_attributes_(string, attrs)


def _width(string: str, font) -> float:
    return _text(string, font, NSColor.labelColor()).size().width


# ---- layout shared by every panel in one menu -----------------------------------------

class PanelLayout:
    """Fonts, heights and columns shared by all account panels in a menu.

    Built once per menu rebuild from every panel, so columns line up across
    accounts: the label column fits the widest label, and the "(!)" note
    column exists only when some row is over its limit.
    """

    LABEL_MAX = 64.0  # a long model name is truncated rather than eating the bar

    def __init__(self, panels):
        self.fonts = _fonts()
        self.width = mp.PANEL_WIDTH
        self.header_h = _line_h(self.fonts["title"])
        self.row_h = _line_h(self.fonts["digits"]) + 3.0
        rows = [row for panel in panels for row in panel.rows if row.state != "unknown"]
        label_w = max((_width(r.label, self.fonts["label"]) for r in rows), default=0.0)
        reset_w = max(
            [_width("00h 00m", self.fonts["digits"])]
            + [_width(r.reset_text, self.fonts["digits"]) for r in rows]
        )
        note_w = _width(mp.OVER_MARKER, self.fonts["digits"]) if any(r.note for r in rows) else 0.0
        self._widths = dict(
            label_w=min(self.LABEL_MAX, float(label_w)),
            pct_w=_width("100%", self.fonts["digits"]),
            reset_w=float(reset_w),
            note_w=float(note_w),
        )
        self.columns = self.columns_for(self.width)

    def columns_for(self, width: float):
        """Columns for a view ``width`` wide (the menu's width), never below the fixed width."""
        return mp.row_columns(width=max(self.width, float(width)), **self._widths)

    def height(self, panel) -> float:
        return mp.panel_height(len(panel.rows), self.header_h, self.row_h)


# ---- activation: one stable target, never rumps' per-item registry ----------------

class CswapAccountTarget(NSObject):
    """Where a panel's click, Return or accessibility press lands.

    rumps looks up a menu item's Python callback when the action is
    dispatched, and ``rebuild_menu`` purges those entries; an activation
    queued before a rebuild would find nothing and silently not switch.
    This target is created once per app and never purged. It accepts a
    request only from a panel that is on screen in the app's current menu
    while that menu is open, so a panel held by an accessibility client
    after dismissal, or left over from an earlier rebuild, cannot switch
    anything. The switch itself is queued with just the account number,
    which the handler checks against the snapshot current when it runs.
    """

    @objc.python_method
    def activate_panel(self, view) -> bool:
        log = logging.getLogger("claude-swap")
        is_open = getattr(self, "is_open", None)
        if is_open is None or not is_open():
            log.debug("usage panel press ignored: the menu is not open")
            return False
        menu = getattr(self, "menu", None)
        if view.panel is None or not _attached(view, menu):
            log.debug("usage panel press ignored: the panel is not in the open menu")
            return False
        _cancel_tracking(menu)
        # After tracking unwinds, so an alert raised by the switch is not
        # shown on top of a menu that is still closing.
        AppHelper.callAfter(self._dispatch, view.panel.num)
        return True

    @objc.python_method
    def _dispatch(self, number) -> None:
        handler = getattr(self, "handler", None)
        if handler is None:
            return
        try:
            handler(str(number))
        except Exception:  # never raise out of an AppKit callback
            logging.getLogger("claude-swap").warning(
                "Menu bar: switching from a usage panel failed", exc_info=True
            )


def make_account_target(handler, is_open=None, menu=None):
    """The app's long-lived activation target.

    ``handler(number)`` performs the switch; ``is_open()`` reports whether
    ``menu`` (the app's root menu) is on screen. Without both, every
    request is refused.
    """
    target = CswapAccountTarget.alloc().init()
    target.handler = handler
    target.is_open = is_open
    target.menu = menu
    return target


def _on_screen(view) -> bool:
    return view.window() is not None


def _attached(view, menu) -> bool:
    """Whether ``view`` is a panel currently shown in ``menu``."""
    if menu is None or not _on_screen(view):
        return False
    item = view.enclosingMenuItem()
    owner = item.menu() if item is not None else None  # None once removed
    return owner is not None and owner == menu


def _cancel_tracking(menu) -> None:
    if menu is not None:
        menu.cancelTracking()


# ---- the view ---------------------------------------------------------------------

class CswapUsagePanelView(NSView):
    """One account block: header line and one bar per usage window."""

    HIGHLIGHT_INSET = 5.0  # matches the native selection's inset from the menu edge
    HIGHLIGHT_RADIUS = 8.0

    def initWithFrame_(self, frame):
        self = objc.super(CswapUsagePanelView, self).initWithFrame_(frame)
        if self is None:
            return None
        _LIVE_VIEWS[0] += 1
        self.panel = None
        self.layout = None
        self.account_target = None  # CswapAccountTarget
        self.on_draw_failure = None  # called when drawing raises
        self.focus_allowed = False  # granted by the menu delegate on highlight
        self.force_highlight = False  # offscreen rendering only
        return self

    def dealloc(self):
        _LIVE_VIEWS[0] -= 1
        objc.super(CswapUsagePanelView, self).dealloc()

    def isFlipped(self):
        return True

    def _highlighted(self) -> bool:
        if self.force_highlight:
            return True
        item = self.enclosingMenuItem()
        return bool(item is not None and item.isHighlighted())

    # -- activation: behave like the plain item the panel replaces ---------------
    def _activate(self) -> bool:
        """Ask the app's target to switch to this panel's account."""
        target = self.account_target
        return bool(target is not None and target.activate_panel(self))

    def mouseUp_(self, event):
        # Like a native item: a release outside the row (press, drag away,
        # let go) or on a row the menu no longer highlights chooses nothing.
        if event is None:
            return
        point = self.convertPoint_fromView_(event.locationInWindow(), None)
        if not NSMouseInRect(point, self.bounds(), self.isFlipped()):
            return
        if not self._highlighted():
            return
        self._activate()

    def acceptsFirstResponder(self):
        # Only while the menu has this panel highlighted. Accepting always
        # made the first panel the menu window's initial focus, so the menu
        # opened with an account selected and Return would switch to it.
        return bool(self.focus_allowed)

    def keyDown_(self, event):
        # AppKit does not send a view item's action on Return; the menu
        # delegate makes the highlighted panel first responder so it can.
        # Only the highlighted panel acts. Every other key is left alone: menu
        # tracking moves the selection and handles Escape itself (the same
        # with or without forwarding, checked live), and a key forwarded up
        # the responder chain would end at NSResponder's default, a beep.
        if event.keyCode() in _ACTIVATE_KEYS and self._highlighted():
            self._activate()

    def accessibilityPerformPress(self):
        return self._activate()

    # -- drawing ----------------------------------------------------------------
    def drawRect_(self, dirty):
        # Drawing runs outside any caller's error handling, so a failure here
        # is caught here: show the plain text row and tell the app, which
        # switches the next rebuild to text rows.
        try:
            _draw_panel(self, dirty)
        except Exception:
            try:
                _draw_fallback(self)
            except Exception:
                pass
            handler = self.on_draw_failure
            if handler is not None:
                try:
                    handler()
                except Exception:
                    pass


def _draw_panel(view, dirty) -> None:
    panel, layout = view.panel, view.layout
    if panel is None or layout is None:
        return
    highlighted = view._highlighted()
    bounds = view.bounds()
    fonts, cols = layout.fonts, layout.columns_for(bounds.size.width)

    if highlighted:
        _draw_selection(bounds)
        primary = secondary = NSColor.selectedMenuItemTextColor()
    else:
        primary = NSColor.labelColor()
        secondary = NSColor.secondaryLabelColor()

    # Header: dot, slot number, identity, then state and age on the right.
    top = mp.TOP_PAD
    h = layout.header_h
    if panel.is_active:
        dot_y = top + (h - mp.DOT_DIAMETER) / 2
        (primary if highlighted else NSColor.controlAccentColor()).setFill()
        NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(mp.LEFT_INSET, dot_y, mp.DOT_DIAMETER, mp.DOT_DIAMETER)
        ).fill()
    num = _text(str(panel.num), fonts["title"], secondary)
    num.drawAtPoint_((cols.label_x, top))
    meta_w = 0.0
    if panel.meta_text:
        meta = _text(panel.meta_text, fonts["meta"], secondary)
        meta_w = meta.size().width
        meta_y = top + (h - _line_h(fonts["meta"])) / 2
        meta.drawAtPoint_((cols.reset_right - meta_w, meta_y))
    name_font = fonts["title_active"] if panel.is_active else fonts["title"]
    name_color = secondary if panel.disabled else primary
    name = _text(panel.title, name_font, name_color, NSLineBreakByTruncatingMiddle)
    name_x, name_w = mp.header_name_span(cols, num.size().width, meta_w)
    name.drawInRect_(NSMakeRect(name_x, top, name_w, h))

    # Rows.
    y = top + h + mp.HEADER_GAP
    for row in panel.rows:
        _draw_row(row, y, layout, cols, highlighted, primary, secondary)
        y += layout.row_h


def _draw_row(row, y, layout, cols, highlighted, primary, secondary) -> None:
    fonts, row_h = layout.fonts, layout.row_h
    text_y = y + (row_h - _line_h(fonts["digits"])) / 2
    if row.state == "unknown":
        note = _text(row.note, fonts["label"], secondary, NSLineBreakByTruncatingTail)
        note.drawInRect_(NSMakeRect(cols.label_x, text_y, cols.reset_right - cols.label_x, row_h))
        return

    label = _text(row.label, fonts["label"], secondary, NSLineBreakByTruncatingTail)
    label.drawInRect_(NSMakeRect(cols.label_x, text_y, cols.label_w, row_h))

    bx, by, bw, bh = mp.bar_rect(cols, y, row_h)
    track = (
        NSColor.selectedMenuItemTextColor().colorWithAlphaComponent_(0.3)
        if highlighted else NSColor.quaternaryLabelColor()
    )
    track.setFill()
    NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        NSMakeRect(bx, by, bw, bh), bh / 2, bh / 2
    ).fill()
    fw = mp.fill_width(bw, row.fraction)
    if fw > 0:
        _fill_color(row.state).setFill()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(bx, by, fw, bh), bh / 2, bh / 2
        ).fill()
    if row.pace_fraction is not None:
        tx, ty, tw, th = mp.tick_rect(cols, row.pace_fraction, by)
        primary.setFill()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(tx, ty, tw, th), tw / 2, tw / 2
        ).fill()

    if row.note and cols.note_right is not None:
        note_color = primary if highlighted else NSColor.systemRedColor()
        note = _text(row.note, fonts["digits"], note_color)
        note.drawAtPoint_((cols.note_right - note.size().width, text_y))
    pct = _text(row.pct_text, fonts["digits"], primary)
    pct.drawAtPoint_((cols.pct_right - pct.size().width, text_y))
    if row.reset_text:
        reset = _text(row.reset_text, fonts["digits"], secondary)
        reset.drawAtPoint_((cols.reset_right - reset.size().width, text_y))


def _draw_selection(bounds) -> None:
    """The native menu selection: accent colour, inset from the menu edge."""
    inset, radius = CswapUsagePanelView.HIGHLIGHT_INSET, CswapUsagePanelView.HIGHLIGHT_RADIUS
    NSColor.controlAccentColor().setFill()
    NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        NSMakeRect(inset, 0.0, bounds.size.width - 2 * inset, bounds.size.height), radius, radius
    ).fill()


def _draw_fallback(view) -> None:
    """The plain text row, drawn inside a panel whose own drawing failed.

    Whatever the failed attempt already drew (a header, half a bar) is
    cleared first, so the text never sits on top of partial output.
    """
    bounds = view.bounds()
    NSRectFillUsingOperation(bounds, NSCompositingOperationClear)
    highlighted = view._highlighted()
    if highlighted:
        _draw_selection(bounds)
    panel = view.panel
    if panel is None:
        return
    font = view.layout.fonts["title"] if view.layout is not None else NSFont.menuFontOfSize_(0.0)
    color = NSColor.selectedMenuItemTextColor() if highlighted else NSColor.labelColor()
    text = _text(panel.text_label, font, color, NSLineBreakByTruncatingTail)
    text.drawInRect_(NSMakeRect(
        mp.LEFT_INSET, mp.TOP_PAD,
        max(0.0, bounds.size.width - mp.LEFT_INSET - mp.RIGHT_INSET), bounds.size.height,
    ))


# ---- the menu delegate ----------------------------------------------------------------

class CswapPanelMenuDelegate(NSObject):
    """Reports the menu opening and closing, and routes Return to panels.

    ``on_open``/``on_close`` let the app hold rebuilds while the menu is on
    screen. During tracking, key events go to the menu window's first
    responder: making the highlighted panel first responder lets Return
    reach it, and when a native item is highlighted focus is taken back so
    Return on that item is left entirely to the menu.
    """

    @objc.python_method
    def _call(self, name):
        callback = getattr(self, name, None)
        if callback is not None:
            try:
                callback()
            except Exception:
                logging.getLogger("claude-swap").debug("menu delegate %s failed", name, exc_info=True)

    def menuWillOpen_(self, menu):
        self._call("on_open")

    def menuDidClose_(self, menu):
        for other in _panel_views(menu):
            other.focus_allowed = False  # the next open starts with nothing selected
        self._call("on_close")

    def menu_willHighlightItem_(self, menu, item):
        view = item.view() if item is not None else None
        target = view if isinstance(view, CswapUsagePanelView) else None
        for other in _panel_views(menu):
            if other is not target:
                other.focus_allowed = False
        if target is not None:
            target.focus_allowed = True
            window = target.window()
            if window is not None:
                window.makeFirstResponder_(target)
            return
        # A native item is highlighted: take focus back from any panel, so
        # Return on that item is left entirely to the menu. All panels share
        # the menu's window.
        for other in _panel_views(menu):
            window = other.window()
            if window is not None:
                if isinstance(window.firstResponder(), CswapUsagePanelView):
                    window.makeFirstResponder_(None)
                return


def _panel_views(menu):
    return [
        item.view() for item in menu.itemArray()
        if isinstance(item.view(), CswapUsagePanelView)
    ]


_MENU_DELEGATES: dict = {}  # NSMenu delegates are weak references; keep ours alive


def install_menu_delegate(nsmenu, on_open=None, on_close=None):
    """Give ``nsmenu`` its (single, retained) delegate; returns it.

    Idempotent: the menu keeps one delegate across rebuilds; callbacks given
    here replace the previous ones.
    """
    delegate = _MENU_DELEGATES.get(id(nsmenu))
    if delegate is None or nsmenu.delegate() is not delegate:
        delegate = CswapPanelMenuDelegate.alloc().init()
        _MENU_DELEGATES[id(nsmenu)] = delegate
        nsmenu.setDelegate_(delegate)
    if on_open is not None:
        delegate.on_open = on_open
    if on_close is not None:
        delegate.on_close = on_close
    return delegate


# ---- building and attaching views ---------------------------------------------------

def make_panel_view(panel, layout: PanelLayout, target=None, on_draw_failure=None):
    view = CswapUsagePanelView.alloc().initWithFrame_(
        NSMakeRect(0.0, 0.0, layout.width, layout.height(panel))
    )
    view.panel = panel
    view.layout = layout
    view.account_target = target
    view.on_draw_failure = on_draw_failure
    view.setAutoresizingMask_(NSViewWidthSizable)  # the menu widens it to its own width
    # An activatable element, read as the account and one sentence per window.
    view.setAccessibilityElement_(True)
    view.setAccessibilityRole_(NSAccessibilityButtonRole)
    view.setAccessibilityLabel_(mp.accessibility_label(panel))
    return view


def _set_item_view(nsitem, view) -> None:
    nsitem.setView_(view)


def attach_usage_panels(pairs, target=None, on_draw_failure=None, after_attach=None) -> None:
    """Give each ``(rumps.MenuItem, AccountPanel)`` its panel view, or none at all.

    Every view is built and measured before any item is touched. They are
    then attached in one pass, and ``after_attach`` (the menu delegate
    install) runs last. If anything raises, every item gets back exactly
    what it had (no view, its text title, its checkmark) and the error is
    re-raised, so the menu is never a mix of panels and text rows.
    """
    pairs = list(pairs)
    layout = PanelLayout([panel for _item, panel in pairs])
    views = [
        make_panel_view(panel, layout, target=target, on_draw_failure=on_draw_failure)
        for _item, panel in pairs
    ]
    natives = [item._menuitem for item, _panel in pairs]
    originals = [(native.view(), native.title(), native.state()) for native in natives]
    try:
        for native, view in zip(natives, views):
            _set_item_view(native, view)
            # The accent dot marks the active account. A checkmark state would
            # also reserve a state column that shifts every native item's text.
            native.setState_(0)
        if after_attach is not None:
            after_attach()
    except Exception:
        for native, (view, title, state) in zip(natives, originals):
            try:
                native.setView_(view)
                native.setTitle_(title)
                native.setState_(state)
            except Exception:
                pass
        raise


# ---- offscreen rendering (visual checks) ------------------------------------------

def render_png(panels, path: str, appearance: str = "darkAqua", highlighted: bool = False):
    """Draw ``panels`` stacked as in the menu into a PNG; returns (width, height).

    ``appearance`` is "aqua" or "darkAqua". The menu's own material is not
    available offscreen, so the image is backed with the window background
    colour of that appearance.
    """
    name = NSAppearanceNameDarkAqua if appearance == "darkAqua" else NSAppearanceNameAqua
    look = NSAppearance.appearanceNamed_(name)
    layout = PanelLayout(panels)
    heights = [layout.height(p) for p in panels]
    total = sum(heights)
    container = NSView.alloc().initWithFrame_(NSMakeRect(0.0, 0.0, layout.width, total))
    container.setAppearance_(look)
    y = total
    for panel, h in zip(panels, heights):
        y -= h
        view = make_panel_view(panel, layout)
        view.setFrameOrigin_((0.0, y))
        view.force_highlight = highlighted
        container.addSubview_(view)

    rep = container.bitmapImageRepForCachingDisplayInRect_(container.bounds())
    previous = NSAppearance.currentDrawingAppearance()
    NSAppearance.setCurrentAppearance_(look)
    try:
        from AppKit import NSGraphicsContext

        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.setCurrentContext_(NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep))
        NSColor.windowBackgroundColor().setFill()
        NSBezierPath.fillRect_(container.bounds())
        NSGraphicsContext.restoreGraphicsState()
        container.cacheDisplayInRect_toBitmapImageRep_(container.bounds(), rep)
    finally:
        NSAppearance.setCurrentAppearance_(previous)
    data = rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, {})
    data.writeToFile_atomically_(path, True)
    return (layout.width, total)
