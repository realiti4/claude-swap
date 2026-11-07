"""The menu bar app's own glue, driven without its event loop (macOS + rumps).

``menubar.create_app`` builds the real ``MenuBarApp``; a fake switcher and a
fake snapshot source stand in for accounts and the network, so these tests
exercise ``rebuild_menu``, the sync tick and the switch path exactly as the
running app does, and nothing here can switch a real account.
"""

from __future__ import annotations

import logging
import sys

import pytest

pytest.importorskip("AppKit")
rumps = pytest.importorskip("rumps")

import AppKit  # noqa: E402

from claude_swap import menubar  # noqa: E402
from claude_swap import menubar_panel_view as pv  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="AppKit menu bar")


class _FakeSwitcher:
    def __init__(self, backup_dir):
        self.backup_dir = backup_dir
        self._logger = logging.getLogger("claude-swap")
        self.switched: list[str] = []

    def _get_claude_config_path(self):
        return self.backup_dir / "no-such-config.json"

    def _get_current_account(self):
        return None

    def switch_to(self, num):
        self.switched.append(num)


class _NoFetch:
    def __init__(self, _switcher):
        pass

    def take(self, **_kw):
        raise RuntimeError("tests never fetch")


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(rumps.rumps, "application_support", lambda _name: str(tmp_path))
    monkeypatch.setattr("claude_swap.snapshot_source.SnapshotSource", _NoFetch)
    monkeypatch.setattr(rumps, "notification", lambda *a, **k: None)
    monkeypatch.setattr(rumps, "alert", lambda *a, **k: 1)
    built = menubar.create_app(_FakeSwitcher(tmp_path))
    yield built
    built.refresh_timer.stop()
    built.sync_timer.stop()


@pytest.fixture
def queued(monkeypatch):
    calls = []
    monkeypatch.setattr(pv.AppHelper, "callAfter", lambda fn, *a, **k: calls.append((fn, a)))
    return calls


def _entry(num, email, active=False, pct=10.0):
    usage = {"five_hour": {"pct": pct}}
    return (num, email, active, usage, usage, None, False, None)


def _snap(*entries):
    active = next((e for e in entries if e[2]), None)
    return {
        "accounts": list(entries),
        "active_email": active[1] if active else None,
        "active_usage": active[3] if active else None,
        "active_alias": None,
    }


def _account_items(app):
    out = []
    for item in app.menu._menu.itemArray():
        if item.isSeparatorItem():
            break
        out.append(item)
    return out


def _title_num(item):
    return item.title().split("  ", 1)[0]


# --- 1. one snapshot per rebuild, panels paired by account number ------------------

def test_rebuild_reads_the_snapshot_once_and_pairs_by_number(app, monkeypatch):
    reads = []

    def read(_self):
        # Every read sees a different account set, as if a worker refresh
        # replaced the snapshot between reads.
        k = len(reads)
        reads.append(k)
        return _snap(_entry(str(k + 1), f"a{k}@x.com", active=True), _entry(str(k + 2), f"b{k}@x.com"))

    monkeypatch.setattr(type(app), "snapshot", property(read, lambda _self, _v: None), raising=False)
    app.rebuild_menu()
    assert len(reads) == 1
    items = _account_items(app)
    assert [_title_num(i) for i in items] == ["1", "2"]
    for item in items:
        assert item.view() is not None
        assert item.view().panel.num == _title_num(item)


# --- 2a. activation: only from the open menu, and it survives a rebuild ---------------

@pytest.fixture
def on_screen(monkeypatch):
    # Panels have a window only while AppKit shows the menu; the live pass
    # checks that part. The rest of the attachment check runs for real.
    monkeypatch.setattr(pv, "_on_screen", lambda view: True)


def _open(app):
    app.menu._menu.delegate().menuWillOpen_(app.menu._menu)


def _close(app):
    app.menu._menu.delegate().menuDidClose_(app.menu._menu)


def _click(view):
    view.force_highlight = True
    b = view.bounds()
    event = AppKit.NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
        AppKit.NSEventTypeLeftMouseUp, (b.size.width / 2, b.size.height / 2), 0, 0, 0, None, 0, 1, 0.0
    )
    view.mouseUp_(event)


def test_panel_click_survives_a_rebuild_before_dispatch(app, queued, on_screen):
    app.snapshot = _snap(_entry("1", "a@x.com", active=True), _entry("2", "b@x.com"))
    app.rebuild_menu()
    _open(app)
    _click(_account_items(app)[1].view())  # queued, not yet dispatched
    _close(app)
    app.snapshot = _snap(_entry("3", "c@x.com"), _entry("2", "b@x.com"), _entry("1", "a@x.com", active=True))
    app.rebuild_menu()  # purges every rumps entry the clicked item had
    fn, args = queued.pop()
    fn(*args)
    assert app.switcher.switched == ["2"]


def test_activation_of_an_account_that_is_gone_does_not_switch(app, queued, on_screen, caplog):
    app.snapshot = _snap(_entry("1", "a@x.com", active=True), _entry("2", "b@x.com"))
    app.rebuild_menu()
    _open(app)
    _click(_account_items(app)[1].view())
    _close(app)
    app.snapshot = _snap(_entry("1", "a@x.com", active=True))
    app.rebuild_menu()
    fn, args = queued.pop()
    with caplog.at_level(logging.INFO, logger="claude-swap"):
        fn(*args)
    assert app.switcher.switched == []
    assert any("2" in r.getMessage() for r in caplog.records)


def test_accessibility_press_in_the_open_menu_switches(app, queued, on_screen):
    app.snapshot = _snap(_entry("1", "a@x.com", active=True), _entry("2", "b@x.com"))
    app.rebuild_menu()
    _open(app)
    assert _account_items(app)[0].view().accessibilityPerformPress()
    fn, args = queued.pop()
    fn(*args)
    assert app.switcher.switched == ["1"]


def test_accessibility_press_after_the_menu_closed_does_nothing(app, queued, on_screen):
    app.snapshot = _snap(_entry("1", "a@x.com", active=True), _entry("2", "b@x.com"))
    app.rebuild_menu()
    view = _account_items(app)[1].view()
    _open(app)
    _close(app)
    assert not view.accessibilityPerformPress()
    assert queued == []
    assert app.switcher.switched == []


def test_press_from_an_earlier_rebuilds_panel_while_open_again_does_nothing(app, queued, on_screen):
    app.snapshot = _snap(_entry("1", "a@x.com", active=True), _entry("2", "b@x.com"))
    app.rebuild_menu()
    stale = _account_items(app)[1].view()
    app.rebuild_menu()  # a refresh while closed: new rows, the old view is detached
    _open(app)
    assert not stale.accessibilityPerformPress()
    assert queued == []
    assert _account_items(app)[1].view().accessibilityPerformPress()  # the live one works
    fn, args = queued.pop()
    fn(*args)
    assert app.switcher.switched == ["2"]


# --- 2b. no rebuild while the menu is open ---------------------------------------------

def test_rebuild_waits_until_the_menu_closes(app):
    app.snapshot = _snap(_entry("1", "a@x.com", active=True), _entry("2", "b@x.com"))
    app.rebuild_menu()
    root = app.menu._menu
    before = list(root.itemArray())
    root.delegate().menuWillOpen_(root)
    app.snapshot = _snap(_entry("1", "a@x.com", active=True), _entry("3", "c@x.com"))
    app.rebuild_menu()
    app.on_sync_tick(None)
    assert list(root.itemArray()) == before  # nothing swapped out under the user
    root.delegate().menuDidClose_(root)
    app.on_sync_tick(None)
    assert [_title_num(i) for i in _account_items(app)] == ["1", "3"]


def test_text_rows_also_wait_while_the_menu_is_open(app):
    app.settings.show_usage_bars = False
    app.snapshot = _snap(_entry("1", "a@x.com", active=True))
    app.rebuild_menu()
    root = app.menu._menu
    before = list(root.itemArray())
    root.delegate().menuWillOpen_(root)
    app.snapshot = _snap(_entry("2", "b@x.com", active=True))
    app.rebuild_menu()
    assert list(root.itemArray()) == before


# --- 3. any failure leaves every account as a text row -----------------------------------

def _assert_all_text(app):
    items = _account_items(app)
    assert items
    for item in items:
        assert item.view() is None
    snap = app.snapshot
    expected = [
        menubar.format_account_label(num, email, display, alias=alias, disabled=disabled, fetched_at=f)
        for num, email, _a, display, _lg, alias, disabled, f in snap["accounts"]
    ]
    # Same text as the text rows (the countdown part can tick; compare the prefix).
    for item, text in zip(items, expected):
        assert item.title().split("  5h")[0] == text.split("  5h")[0]
    assert [i.state() for i in items] == [1 if e[2] else 0 for e in snap["accounts"]]


def test_failure_attaching_the_second_panel_leaves_all_text(app, monkeypatch):
    real = pv._set_item_view
    calls = []

    def flaky(nsitem, view):
        calls.append(nsitem)
        if len(calls) == 2:
            raise RuntimeError("setView failed")
        real(nsitem, view)

    monkeypatch.setattr(pv, "_set_item_view", flaky)
    app.snapshot = _snap(_entry("1", "a@x.com", active=True), _entry("2", "b@x.com"), _entry("3", "c@x.com"))
    app.rebuild_menu()
    _assert_all_text(app)


def test_failure_installing_the_delegate_leaves_all_text(app, monkeypatch):
    def fails(*_a, **_k):
        raise RuntimeError("delegate install failed")

    monkeypatch.setattr(pv, "install_menu_delegate", fails)
    app.snapshot = _snap(_entry("1", "a@x.com", active=True), _entry("2", "b@x.com"))
    app.rebuild_menu()
    _assert_all_text(app)


def test_draw_failure_switches_the_next_rebuild_to_text(app, monkeypatch, caplog):
    app.snapshot = _snap(_entry("1", "a@x.com", active=True), _entry("2", "b@x.com"))
    app.rebuild_menu()
    views = [i.view() for i in _account_items(app)]
    assert all(v is not None for v in views)
    monkeypatch.setattr(pv, "_draw_panel", lambda *_a: (_ for _ in ()).throw(RuntimeError("draw failed")))
    with caplog.at_level(logging.WARNING, logger="claude-swap"):
        for view in views:
            rep = view.bitmapImageRepForCachingDisplayInRect_(view.bounds())
            view.cacheDisplayInRect_toBitmapImageRep_(view.bounds(), rep)
    assert sum("usage bars" in r.getMessage() for r in caplog.records) == 1  # logged once
    app.on_sync_tick(None)  # the menu is closed: the pending rebuild runs now
    _assert_all_text(app)
    app.rebuild_menu()  # and stays text for the rest of the session
    _assert_all_text(app)


# --- the setting ------------------------------------------------------------------------

def test_setting_off_gives_the_text_rows(app):
    app.settings.show_usage_bars = False
    app.snapshot = _snap(_entry("1", "a@x.com", active=True), _entry("2", "b@x.com"))
    app.rebuild_menu()
    _assert_all_text(app)


def test_setting_on_gives_panels_without_checkmarks(app):
    app.snapshot = _snap(_entry("1", "a@x.com", active=True), _entry("2", "b@x.com"))
    app.rebuild_menu()
    items = _account_items(app)
    assert all(i.view() is not None for i in items)
    assert all(i.state() == 0 for i in items)
    assert AppKit.NSApplication.sharedApplication() is not None


def test_turning_bars_back_on_retries_after_a_draw_failure(app, monkeypatch, tmp_path):
    app._bars_broken = True
    app.settings.show_usage_bars = False
    monkeypatch.setattr(app.settings, "save", lambda _path: None)  # never touch a real file
    app.snapshot = _snap(_entry("1", "a@x.com", active=True))
    app.on_toggle_usage_bars(None)
    assert app.settings.show_usage_bars is True
    assert app._bars_broken is False
    assert _account_items(app)[0].view() is not None


@pytest.mark.parametrize("bars", [True, False])
def test_wider_rows_still_build_the_menu(app, bars):
    # Rows may grow (another change adds login expiry and quarantine as
    # fields 9 and 10): every row reader takes the fields it knows.
    app.settings.show_usage_bars = bars
    app.snapshot = _snap(
        _entry("1", "a@x.com", active=True) + (1_900_000_000.0, False),
        _entry("2", "b@x.com") + (None, True),
    )
    app.rebuild_menu()
    assert [_title_num(i) for i in _account_items(app)] == ["1", "2"]
    app._log_usage(app.snapshot)
