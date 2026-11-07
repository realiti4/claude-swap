"""AppKit layer of the menu bar usage bars (macOS with the menubar extra only).

The drawing itself is checked by eye (see the PR); these tests pin what can
be pinned without a screen: sizes, that both appearances render, that a
click, Return or an accessibility press reaches the stable account target,
that a failure anywhere leaves plain text rows, and that rebuilding the menu
does not leak views or rumps callback entries.
"""

from __future__ import annotations

import gc
import logging
import sys

import pytest

pytest.importorskip("AppKit")
rumps = pytest.importorskip("rumps")

import AppKit  # noqa: E402
import objc  # noqa: E402

from claude_swap import menubar, menubar_panel as mp  # noqa: E402
from claude_swap import menubar_panel_view as pv  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="AppKit drawing")

_NOW = 1_000_000.0


def _panels():
    usages = [
        {"five_hour": {"pct": 46.0}, "seven_day": {"pct": 73.0},
         "scoped": [{"name": "Fable", "pct": 85.0}]},
        {"five_hour": {"pct": 100.0}},
        "token expired",
    ]
    entries = [
        (str(i + 1), f"user{i}@example.com", i == 0, u, u if isinstance(u, dict) else None,
         None, False, _NOW - 60)
        for i, u in enumerate(usages)
    ]
    return [mp.build_account_panel(e, _NOW) for e in entries]


def _items(panels, active_first=True):
    items = [rumps.MenuItem(p.text_label, callback=lambda _s: None) for p in panels]
    if active_first:
        items[0].state = 1
    return items


def _text_state(items):
    return [(i._menuitem.view(), i._menuitem.title(), i._menuitem.state()) for i in items]


@pytest.fixture
def run_now(monkeypatch):
    monkeypatch.setattr(pv.AppHelper, "callAfter", lambda fn, *a, **k: fn(*a, **k))


class _Menu:
    """A real NSMenu whose panels are attached, plus the open flag the app keeps."""

    def __init__(self, panels, handler):
        self.open = True
        self.menu = AppKit.NSMenu.alloc().init()
        self.target = pv.make_account_target(handler, is_open=lambda: self.open, menu=self.menu)
        layout = pv.PanelLayout(panels)
        self.items, self.views = [], []
        for panel in panels:
            item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(panel.text_label, None, "")
            view = pv.make_panel_view(panel, layout, target=self.target)
            item.setView_(view)
            self.menu.addItem_(item)
            self.items.append(item)
            self.views.append(view)


@pytest.fixture
def switched():
    return []


@pytest.fixture
def on_screen(monkeypatch):
    # A view only has a window while AppKit displays the menu, which no unit
    # test can do; the live pass checks the real thing. Everything else in
    # the attachment check (item still in the app's current menu) is real.
    monkeypatch.setattr(pv, "_on_screen", lambda view: True)


@pytest.fixture
def target(switched):
    return pv.make_account_target(switched.append)


def _mouse_up(view, inside=True):
    b = view.bounds()
    # Window coordinates for a windowless view: y is measured from the bottom.
    x = b.size.width / 2 if inside else b.size.width + 40.0
    y = b.size.height / 2
    return AppKit.NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
        AppKit.NSEventTypeLeftMouseUp, (x, y), 0, 0, 0, None, 0, 1, 0.0
    )


def _render(view):
    rep = view.bitmapImageRepForCachingDisplayInRect_(view.bounds())
    view.cacheDisplayInRect_toBitmapImageRep_(view.bounds(), rep)
    return rep


# --- layout and rendering ------------------------------------------------------------

def test_layout_uses_the_fixed_width_and_model_heights():
    panels = _panels()
    layout = pv.PanelLayout(panels)
    assert layout.width == mp.PANEL_WIDTH
    for panel in panels:
        view = pv.make_panel_view(panel, layout)
        frame = view.frame()
        assert frame.size.width == mp.PANEL_WIDTH
        assert frame.size.height == mp.panel_height(len(panel.rows), layout.header_h, layout.row_h)


def test_layout_reserves_the_note_column_only_when_needed():
    with_note = pv.PanelLayout(_panels())
    without = pv.PanelLayout(_panels()[:1])
    assert with_note.columns.note_right is not None
    assert without.columns.note_right is None
    assert with_note.columns.bar_w < without.columns.bar_w


@pytest.mark.parametrize("appearance", ["aqua", "darkAqua"])
def test_offscreen_render_in_both_appearances(tmp_path, appearance):
    path = tmp_path / f"{appearance}.png"
    size = pv.render_png(_panels(), str(path), appearance)
    assert path.stat().st_size > 1000
    width, height = size
    assert width == mp.PANEL_WIDTH
    assert height > 0


def test_render_highlighted_does_not_crash(tmp_path):
    pv.render_png(_panels(), str(tmp_path / "hl.png"), "darkAqua", highlighted=True)


def test_very_long_identity_renders(tmp_path):
    email = "an.extremely.long.address.for.testing.truncation@some-very-long-domain.example.com"
    entry = ("12", email, True, {"five_hour": {"pct": 10.0}}, None, "work", False, _NOW - 60)
    panel = mp.build_account_panel(entry, _NOW)
    pv.render_png([panel], str(tmp_path / "long.png"), "darkAqua")


def test_view_stretches_to_the_menu_width():
    # NSMenu widens width-sizable item views to the menu's width; the panel
    # then lays its right-hand columns out against that width.
    panels = _panels()
    view = pv.make_panel_view(panels[0], pv.PanelLayout(panels))
    assert view.autoresizingMask() & AppKit.NSViewWidthSizable


def test_columns_follow_the_actual_width_with_a_floor():
    layout = pv.PanelLayout(_panels())
    wide = layout.columns_for(400.0)
    assert wide.reset_right == 400.0 - mp.RIGHT_INSET
    assert wide.bar_w == layout.columns.bar_w + 60.0  # the bar takes the extra width
    narrow = layout.columns_for(200.0)
    assert narrow == layout.columns  # never narrower than the fixed width


# --- activation: only a live, highlighted panel in the open menu can switch ---------

def test_click_inside_a_highlighted_panel_switches_that_account(run_now, on_screen, switched):
    m = _Menu(_panels(), switched.append)
    view = m.views[1]
    view.force_highlight = True
    view.mouseUp_(_mouse_up(view, inside=True))
    assert switched == ["2"]


def test_release_outside_the_panel_does_nothing(run_now, on_screen, switched):
    # Press, drag out of the row, release: the native "changed my mind".
    m = _Menu(_panels(), switched.append)
    view = m.views[1]
    view.force_highlight = True
    view.mouseUp_(_mouse_up(view, inside=False))
    assert switched == []


def test_release_on_a_panel_that_is_not_highlighted_does_nothing(run_now, on_screen, switched):
    m = _Menu(_panels(), switched.append)
    view = m.views[1]
    view.mouseUp_(_mouse_up(view, inside=True))
    assert switched == []


def test_release_without_an_event_does_nothing(run_now, on_screen, switched):
    m = _Menu(_panels(), switched.append)
    m.views[0].force_highlight = True
    m.views[0].mouseUp_(None)
    assert switched == []


def test_press_after_the_menu_closed_does_nothing(run_now, on_screen, switched):
    # An accessibility client can still hold the panel after dismissal.
    m = _Menu(_panels(), switched.append)
    m.open = False
    assert m.views[1].accessibilityPerformPress() is False
    assert switched == []


def test_press_from_a_detached_panel_does_nothing(run_now, on_screen, switched):
    m = _Menu(_panels(), switched.append)
    m.menu.removeItem_(m.items[1])
    m.views[1].accessibilityPerformPress()
    assert switched == []


def test_press_from_a_panel_that_is_not_on_screen_does_nothing(run_now, switched):
    m = _Menu(_panels(), switched.append)  # real _on_screen: no window in a test
    m.views[1].accessibilityPerformPress()
    assert switched == []


def test_press_from_an_earlier_rebuilds_panel_while_open_again_does_nothing(run_now, on_screen, switched):
    m = _Menu(_panels(), switched.append)
    stale = m.views[1]
    m.menu.removeAllItems()  # the next rebuild replaces every row
    layout = pv.PanelLayout(_panels())
    for panel in _panels():
        item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(panel.text_label, None, "")
        item.setView_(pv.make_panel_view(panel, layout, target=m.target))
        m.menu.addItem_(item)
    m.open = True
    stale.accessibilityPerformPress()
    assert switched == []
    m.menu.itemArray()[1].view().accessibilityPerformPress()  # the current panel still works
    assert switched == ["2"]


def test_press_from_a_panel_in_some_other_menu_does_nothing(run_now, on_screen, switched):
    m = _Menu(_panels(), switched.append)
    other = AppKit.NSMenu.alloc().init()
    m.menu.removeItem_(m.items[0])
    other.addItem_(m.items[0])  # attached and on screen, but not the app's menu
    m.views[0].accessibilityPerformPress()
    assert switched == []


def test_request_closes_the_menu_before_the_switch_runs(on_screen, switched, monkeypatch):
    queued, cancelled = [], []
    monkeypatch.setattr(pv.AppHelper, "callAfter", lambda fn, *a, **k: queued.append((fn, a)))
    m = _Menu(_panels(), switched.append)
    monkeypatch.setattr(pv, "_cancel_tracking", lambda menu: cancelled.append(menu))
    assert m.views[0].accessibilityPerformPress() is True
    assert cancelled == [m.menu] and switched == []  # queued, not yet run
    fn, args = queued.pop()
    fn(*args)
    assert switched == ["1"]


def test_activation_without_a_target_does_nothing(run_now):
    panels = _panels()
    view = pv.make_panel_view(panels[0], pv.PanelLayout(panels))
    view.force_highlight = True
    view.mouseUp_(_mouse_up(view))  # must not raise


def test_switch_receives_the_number_as_a_string(run_now, on_screen, switched):
    entry = (7, "n@x.com", False, {"five_hour": {"pct": 1.0}}, None, None, False, None)
    panel = mp.build_account_panel(entry, _NOW)
    menu = AppKit.NSMenu.alloc().init()
    t = pv.make_account_target(switched.append, is_open=lambda: True, menu=menu)
    item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("x", None, "")
    view = pv.make_panel_view(panel, pv.PanelLayout([panel]), target=t)
    item.setView_(view)
    menu.addItem_(item)
    view.accessibilityPerformPress()
    assert switched == ["7"]


def test_switch_errors_are_logged_not_raised(run_now, on_screen, caplog):
    def boom(_num):
        raise RuntimeError("switch failed")

    m = _Menu(_panels(), boom)
    with caplog.at_level(logging.WARNING, logger="claude-swap"):
        m.views[0].accessibilityPerformPress()  # must not raise out of an AppKit callback
    assert any(r.exc_info for r in caplog.records)


# --- keyboard: Return on a highlighted panel acts like Return on a text row -------

def _key_event(code: int):
    return AppKit.NSEvent.keyEventWithType_location_modifierFlags_timestamp_windowNumber_context_characters_charactersIgnoringModifiers_isARepeat_keyCode_(
        AppKit.NSEventTypeKeyDown, (0, 0), 0, 0, 0, None, "\r", "\r", False, code
    )


def test_panel_takes_focus_only_when_the_menu_highlights_it():
    # A panel that always accepted first responder became the menu window's
    # initial focus: the menu opened with the first account highlighted, and
    # Return right after opening would have switched to it. Native menus open
    # with nothing selected, so focus is granted only on highlight.
    panels = _panels()
    layout = pv.PanelLayout(panels)
    menu = AppKit.NSMenu.alloc().init()
    delegate = pv.install_menu_delegate(menu)
    items, views = [], []
    for panel in panels[:2]:
        item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(panel.text_label, None, "")
        view = pv.make_panel_view(panel, layout)
        item.setView_(view)
        menu.addItem_(item)
        items.append(item)
        views.append(view)
    native = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Quit", None, "")
    menu.addItem_(native)

    assert [v.acceptsFirstResponder() for v in views] == [False, False]  # menu just opened
    delegate.menu_willHighlightItem_(menu, items[1])
    assert [v.acceptsFirstResponder() for v in views] == [False, True]
    delegate.menu_willHighlightItem_(menu, items[0])
    assert [v.acceptsFirstResponder() for v in views] == [True, False]
    delegate.menu_willHighlightItem_(menu, native)
    assert [v.acceptsFirstResponder() for v in views] == [False, False]
    delegate.menu_willHighlightItem_(menu, items[1])
    delegate.menuDidClose_(menu)
    assert [v.acceptsFirstResponder() for v in views] == [False, False]  # next open starts clean


@pytest.mark.parametrize("code", [36, 76])  # Return, keypad Enter
def test_return_on_the_highlighted_panel_activates_it(run_now, on_screen, switched, code):
    m = _Menu(_panels(), switched.append)
    m.views[0].force_highlight = True
    m.views[0].keyDown_(_key_event(code))
    assert switched == ["1"]


def test_return_on_a_panel_that_is_not_highlighted_does_nothing(run_now, on_screen, switched):
    # A stale first responder must never fire when another item is selected.
    m = _Menu(_panels(), switched.append)
    m.views[0].keyDown_(_key_event(36))
    assert switched == []


def test_other_keys_do_not_activate(run_now, on_screen, switched):
    m = _Menu(_panels(), switched.append)
    m.views[0].force_highlight = True
    m.views[0].keyDown_(_key_event(125))  # Down arrow
    assert switched == []


# --- accessibility ---------------------------------------------------------------------

def test_panel_is_an_accessible_button_with_a_spoken_label():
    panels = _panels()
    view = pv.make_panel_view(panels[0], pv.PanelLayout(panels))
    assert view.isAccessibilityElement()
    assert view.accessibilityRole() == AppKit.NSAccessibilityButtonRole
    assert view.accessibilityLabel() == mp.accessibility_label(panels[0])


def test_accessibility_press_in_the_open_menu_switches(run_now, on_screen, switched):
    m = _Menu(_panels(), switched.append)
    assert m.views[2].accessibilityPerformPress() is True
    assert switched == ["3"]


# --- attach: all or nothing ---------------------------------------------------------------

def test_attach_sets_views_and_keeps_text_titles():
    panels = _panels()
    items = _items(panels)
    pv.attach_usage_panels(list(zip(items, panels)))
    for item, panel in zip(items, panels):
        assert item._menuitem.view() is not None
        assert item._menuitem.title() == panel.text_label


def test_attached_items_drop_the_checkmark_state():
    # The accent dot marks the active account; a state column would push the
    # native items' text right of the panels' text.
    panels = _panels()
    items = _items(panels)
    pv.attach_usage_panels(list(zip(items, panels)))
    assert all(item._menuitem.state() == 0 for item in items)


def test_failure_at_the_second_item_rolls_every_item_back_to_text(monkeypatch):
    panels = _panels()
    items = _items(panels)
    before = _text_state(items)
    real = pv._set_item_view
    calls = []

    def flaky(nsitem, view):
        calls.append(nsitem)
        if len(calls) == 2:
            raise RuntimeError("setView failed")
        real(nsitem, view)

    monkeypatch.setattr(pv, "_set_item_view", flaky)
    with pytest.raises(RuntimeError):
        pv.attach_usage_panels(list(zip(items, panels)))
    assert _text_state(items) == before


def test_failure_after_attach_rolls_every_item_back_to_text():
    panels = _panels()
    items = _items(panels)
    before = _text_state(items)

    def install_fails():
        raise RuntimeError("delegate install failed")

    with pytest.raises(RuntimeError):
        pv.attach_usage_panels(list(zip(items, panels)), after_attach=install_fails)
    assert _text_state(items) == before


def test_views_are_built_before_any_item_is_touched(monkeypatch):
    panels = _panels()
    items = _items(panels)
    before = _text_state(items)
    real = pv.make_panel_view
    built = []
    touched_when_failing = []

    def flaky(panel, layout, **kw):
        built.append(panel)
        if len(built) == 3:
            # At the moment the last view fails to build, no item may have
            # been given a view yet: building happens before attaching.
            touched_when_failing.extend(i for i in items if i._menuitem.view() is not None)
            raise RuntimeError("measure failed")
        return real(panel, layout, **kw)

    monkeypatch.setattr(pv, "make_panel_view", flaky)
    with pytest.raises(RuntimeError):
        pv.attach_usage_panels(list(zip(items, panels)))
    assert touched_when_failing == []
    assert _text_state(items) == before


# --- drawing failures fall back to text and report once ---------------------------------

def test_draw_failure_draws_text_and_reports(monkeypatch):
    panels = _panels()
    reports = []
    view = pv.make_panel_view(panels[0], pv.PanelLayout(panels), on_draw_failure=lambda: reports.append(1))

    def boom(_view, _dirty):
        raise RuntimeError("draw failed")

    fallback = []
    real_fallback = pv._draw_fallback
    monkeypatch.setattr(pv, "_draw_panel", boom)
    monkeypatch.setattr(pv, "_draw_fallback", lambda v: fallback.append(v) or real_fallback(v))
    _render(view)  # must not raise
    assert reports == [1]
    assert fallback == [view]


def _png(view):
    rep = _render(view)
    return bytes(rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {}))


def _raise(*_a, **_k):
    raise RuntimeError("draw failed")


@pytest.mark.parametrize("highlighted", [False, True])
def test_draw_failure_mid_panel_leaves_only_the_fallback(monkeypatch, highlighted):
    # The header draws, then a row throws: the header's pixels must not show
    # through the fallback text. Compare with the fallback drawn on its own.
    panels = _panels()
    layout = pv.PanelLayout(panels)

    def view():
        v = pv.make_panel_view(panels[0], layout)
        v.force_highlight = highlighted
        return v

    with monkeypatch.context() as patch:
        patch.setattr(pv, "_draw_panel", _raise)  # nothing drawn before the fallback
        clean = _png(view())
    with monkeypatch.context() as patch:
        patch.setattr(pv, "_draw_panel", lambda *_a: None)
        blank = _png(view())
    with monkeypatch.context() as patch:
        patch.setattr(pv, "_draw_row", _raise)  # header drawn, first row throws
        broken = _png(view())
    assert clean != blank  # the reference really has the fallback text in it
    assert broken == clean


def test_fallback_keeps_the_selection_when_highlighted(monkeypatch):
    # Sample a pixel between the selection's inset and the text: the accent
    # selection when highlighted, cleared (transparent) otherwise.
    panels = _panels()
    layout = pv.PanelLayout(panels)
    monkeypatch.setattr(pv, "_draw_panel", _raise)
    alphas = {}
    for highlighted in (False, True):
        view = pv.make_panel_view(panels[0], layout)
        view.force_highlight = highlighted
        rep = _render(view)
        scale = rep.pixelsWide() / view.bounds().size.width
        x = int((pv.CswapUsagePanelView.HIGHLIGHT_INSET + 3.0) * scale)
        y = int(view.bounds().size.height / 2 * scale)
        alphas[highlighted] = rep.colorAtX_y_(x, y).alphaComponent()
    assert alphas[True] > 0.9
    assert alphas[False] < 0.1


def test_draw_failure_without_a_handler_does_not_raise(monkeypatch):
    panels = _panels()
    view = pv.make_panel_view(panels[0], pv.PanelLayout(panels))
    monkeypatch.setattr(pv, "_draw_panel", lambda *_a: (_ for _ in ()).throw(RuntimeError("x")))
    _render(view)


# --- menu delegate: open/close for deferred rebuilds, focus for Return -----------------

def test_menu_delegate_reports_open_and_close():
    events = []
    menu = AppKit.NSMenu.alloc().init()
    pv.install_menu_delegate(menu, on_open=lambda: events.append("open"), on_close=lambda: events.append("close"))
    menu.delegate().menuWillOpen_(menu)
    menu.delegate().menuDidClose_(menu)
    assert events == ["open", "close"]


def test_menu_delegate_is_retained_and_idempotent():
    menu = AppKit.NSMenu.alloc().init()
    first = pv.install_menu_delegate(menu)
    gc.collect()
    assert menu.delegate() is not None  # NSMenu holds its delegate weakly
    assert pv.install_menu_delegate(menu) is first


def test_delegate_tolerates_native_and_missing_items():
    menu = AppKit.NSMenu.alloc().init()
    pv.install_menu_delegate(menu)
    native = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Quit", None, "")
    menu.addItem_(native)
    menu.delegate().menu_willHighlightItem_(menu, native)
    menu.delegate().menu_willHighlightItem_(menu, None)


# --- rebuild leak check ----------------------------------------------------------------------

def _rebuild(root, registry, panels, target):
    """What rebuild_menu does to the account rows: purge, clear, re-add."""
    menubar.purge_menu_callbacks(registry, root._menu)
    root.clear()
    items = [rumps.MenuItem(p.text_label, callback=lambda _s: None) for p in panels]
    pv.attach_usage_panels(list(zip(items, panels)), target=target)
    root.update(items)


def test_purge_covers_view_backed_items(target):
    registry = rumps.rumps.NSApp._ns_to_py_and_callback
    root = rumps.rumps.Menu()
    panels = _panels()
    _rebuild(root, registry, panels, target)
    natives = list(root._menu.itemArray())
    assert all(n in registry for n in natives)
    assert all(n.view() is not None for n in natives)
    menubar.purge_menu_callbacks(registry, root._menu)
    assert not any(n in registry for n in natives)


def test_no_growth_across_200_rebuilds(target):
    registry = rumps.rumps.NSApp._ns_to_py_and_callback
    root = rumps.rumps.Menu()
    panels = _panels()
    with objc.autorelease_pool():
        _rebuild(root, registry, panels, target)
    gc.collect()
    base_views, base_registry = pv.live_view_count(), len(registry)
    for _ in range(200):
        with objc.autorelease_pool():
            _rebuild(root, registry, panels, target)
    gc.collect()
    assert pv.live_view_count() == base_views
    assert len(registry) == base_registry
    # Tear down: nothing of ours stays alive once the menu is gone.
    with objc.autorelease_pool():
        menubar.purge_menu_callbacks(registry, root._menu)
        root.clear()
    gc.collect()
    assert pv.live_view_count() == base_views - len(panels)
