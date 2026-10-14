"""The menu bar app's own glue, driven without its event loop (macOS + rumps).

``menubar.create_app`` builds the real ``MenuBarApp``; a fake switcher and a
fake snapshot source stand in for accounts and the network, so these tests
exercise ``rebuild_menu``, the sync tick and the switch path exactly as the
running app does, and nothing here can switch a real account.
"""

from __future__ import annotations

import logging
import sys
import threading
import time

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

    # The real store raises ValidationError / ConfigError (ClaudeSwitchError
    # subclasses) with the CLI's own messages; tests queue one to replay it.
    rename_error = None
    rename_delay = 0.0  # seconds a store call blocks, as under lock contention
    rename_lands_in = None  # the slot the store reports it changed (a swap moved it)

    def set_alias(self, num, name, expected_email=None, expected_org=None):
        self.renames = getattr(self, "renames", []) + [("set", num, name, expected_email, expected_org)]
        time.sleep(self.rename_delay)
        if self.rename_error is not None:
            raise self.rename_error
        return self.rename_lands_in or num, name.strip()  # the store keeps the typed case

    def unset_alias(self, num, expected_email=None, expected_org=None):
        self.renames = getattr(self, "renames", []) + [("unset", num, expected_email, expected_org)]
        time.sleep(self.rename_delay)
        if self.rename_error is not None:
            raise self.rename_error
        return self.rename_lands_in or num


class _NoFetch:
    def __init__(self, _switcher):
        pass

    def take(self, **_kw):
        # The app's startup refresh lands here. It takes a moment, as a real
        # read does under load, so a test that starts before it finished
        # fails every time instead of now and then.
        time.sleep(0.05)
        raise RuntimeError("tests never fetch")


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(rumps.rumps, "application_support", lambda _name: str(tmp_path))
    monkeypatch.setattr("claude_swap.snapshot_source.SnapshotSource", _NoFetch)
    monkeypatch.setattr(rumps, "notification", lambda *a, **k: None)
    monkeypatch.setattr(rumps, "alert", lambda *a, **k: 1)
    built = menubar.create_app(_FakeSwitcher(tmp_path))
    # The app starts a refresh as it opens. Tests that drive their own
    # refresh start from an idle worker: one still running would swallow it.
    deadline = time.monotonic() + 5
    while built._refreshing and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not built._refreshing
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


# --- rename -------------------------------------------------------------------------

def _entry_alias(num, email, alias, active=False):
    usage = {"five_hour": {"pct": 10.0}}
    return (num, email, active, usage, usage, alias, False, None)


class _Window:
    """Stands in for rumps.Window: records how it was built, returns a canned answer."""

    made = []
    answer = (1, "")

    def __init__(self, message="", title="", default_text="", ok=None, cancel=None, dimensions=None, **_):
        _Window.made.append({"message": message, "title": title, "default_text": default_text})

    def run(self):
        clicked, text = _Window.answer
        return type("Response", (), {"clicked": clicked, "text": text})()


@pytest.fixture
def window(monkeypatch):
    _Window.made = []
    monkeypatch.setattr(rumps, "Window", _Window)
    return _Window


def _submenu(app, title):
    for item in app.menu._menu.itemArray():
        if item.title() == title:
            return item.submenu()
    return None


def _root_titles(app):
    return [i.title() for i in app.menu._menu.itemArray()]


def test_rename_submenu_follows_disable_and_lists_every_account(app):
    app.snapshot = _snap(_entry_alias("1", "a@x.com", "work", active=True), _entry_alias("2", "b@x.com", None))
    app.rebuild_menu()
    titles = _root_titles(app)
    assert titles.index("Rename account") == titles.index("Disable / enable account") + 1
    assert [i.title() for i in _submenu(app, "Rename account").itemArray()] == [
        "1  work  (a@x.com)", "2  b@x.com",
    ]


def _choose_rename(app, index):
    item = _submenu(app, "Rename account").itemArray()[index]
    item.target().callback_(item)


def _settle(app, timeout=5.0):
    """Tick until every rename worker has reported back (as the sync timer would)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.on_sync_tick(None)
        if not app._renames_in_flight:
            app.on_sync_tick(None)
            return
        time.sleep(0.02)
    raise AssertionError("rename never reported back")


def test_rename_prompt_is_prefilled_and_ok_stores_the_alias(app, window):
    app.snapshot = _snap(_entry_alias("1", "a@x.com", "work", active=True), _entry_alias("2", "b@x.com", None))
    app.rebuild_menu()
    window.answer = (1, "Home")
    _choose_rename(app, 0)
    assert window.made[0]["default_text"] == "work"
    _settle(app)  # the change shows on the next tick
    assert app.switcher.renames == [("set", "1", "Home", "a@x.com", None)]
    assert _account_items(app)[0].title().startswith("1  Home  (a@x.com)")  # as typed


def test_rename_prompt_for_an_account_without_alias_starts_empty(app, window):
    app.snapshot = _snap(_entry_alias("1", "a@x.com", None, active=True))
    app.rebuild_menu()
    window.answer = (0, "ignored")
    _choose_rename(app, 0)
    assert window.made[0]["default_text"] == ""


def test_empty_name_clears_the_alias(app, window):
    app.snapshot = _snap(_entry_alias("1", "a@x.com", "work", active=True))
    app.rebuild_menu()
    window.answer = (1, "   ")
    _choose_rename(app, 0)
    _settle(app)
    assert app.switcher.renames == [("unset", "1", "a@x.com", None)]
    assert _account_items(app)[0].title().startswith("1  a@x.com  ")


def test_cancel_changes_nothing(app, window):
    app.snapshot = _snap(_entry_alias("1", "a@x.com", "work", active=True))
    app.rebuild_menu()
    window.answer = (0, "other")
    _choose_rename(app, 0)
    assert getattr(app.switcher, "renames", []) == []


def test_rejected_name_shows_the_stores_own_message(app, window, monkeypatch):
    from claude_swap.exceptions import ConfigError

    alerts = []
    monkeypatch.setattr(rumps, "alert", lambda **kw: alerts.append(kw) or 1)
    app.snapshot = _snap(_entry_alias("1", "a@x.com", None, active=True), _entry_alias("2", "b@x.com", "work"))
    app.rebuild_menu()
    app.switcher.rename_error = ConfigError("Alias 'work' is already used by account 2")
    window.answer = (1, "work")
    _choose_rename(app, 0)
    _settle(app)
    assert [a["message"] for a in alerts] == ["Alias 'work' is already used by account 2"]
    assert _account_items(app)[0].title().startswith("1  a@x.com  ")  # unchanged


# --- names only ---------------------------------------------------------------------

def test_names_only_shows_aliases_alone_in_rows_and_panels(app, monkeypatch):
    monkeypatch.setattr(app.settings, "save", lambda _p: None)
    app.snapshot = _snap(_entry_alias("1", "a@x.com", "work", active=True), _entry_alias("2", "b@x.com", None))
    app.on_toggle_names_only(None)
    assert app.settings.show_names_only is True
    items = _account_items(app)
    assert items[0].title().startswith("1  work  5h")
    assert items[1].title().startswith("2  b@x.com  5h")
    assert items[0].view().panel.title == "work"
    assert "a@x.com" in items[0].view().accessibilityLabel()
    app.settings.show_usage_bars = False
    app.rebuild_menu()
    assert _account_items(app)[0].title().startswith("1  work  5h")


def test_names_only_is_in_the_settings_menu(app):
    app.rebuild_menu()
    titles = [i.title() for i in _submenu(app, "Settings").itemArray()]
    assert "Show names only" in titles


def test_names_only_text_rows_still_name_the_email_to_assistive_tech(app, monkeypatch):
    # AXTitle (what assistive technology reads for a menu item) must carry the
    # email even when the visible row shows only the alias.
    monkeypatch.setattr(app.settings, "save", lambda _p: None)
    app.settings.show_usage_bars = False
    app.snapshot = _snap(_entry_alias("1", "a@x.com", "work", active=True), _entry_alias("2", "b@x.com", None))
    app.on_toggle_names_only(None)
    first, second = _account_items(app)
    assert first.title().startswith("1  work  5h")
    assert first.accessibilityTitle().startswith("1  work  (a@x.com)  5h")
    assert second.accessibilityTitle().startswith("2  b@x.com  5h")  # nothing hidden, nothing added
    app.on_toggle_names_only(None)  # off again: the plain title is the accessible name
    assert _account_items(app)[0].accessibilityTitle() == _account_items(app)[0].title()


def test_names_only_rows_keep_the_email_for_assistive_tech_after_a_text_fallback(app, monkeypatch):
    real = pv._set_item_view
    calls = []

    def flaky(nsitem, view):
        calls.append(nsitem)
        if len(calls) == 2:
            raise RuntimeError("setView failed")
        real(nsitem, view)

    monkeypatch.setattr(pv, "_set_item_view", flaky)
    app.settings.show_names_only = True
    app.snapshot = _snap(_entry_alias("1", "a@x.com", "work", active=True), _entry_alias("2", "b@x.com", "dev"))
    app.rebuild_menu()
    items = _account_items(app)
    assert all(i.view() is None for i in items)  # fell back to text rows
    assert items[1].accessibilityTitle().startswith("2  dev  (b@x.com)")


# --- rename: the account, off the main thread, not undone by a stale refresh --------

def test_rename_follows_the_account_after_a_swap(app, window):
    # The prompt showed slot 1 = a@x.com; before submit a swap moved it to slot 2.
    app.snapshot = _snap(_entry_alias("1", "a@x.com", None, active=True), _entry_alias("2", "b@x.com", None))
    app.rebuild_menu()
    app.switcher.rename_lands_in = "2"
    window.answer = (1, "home")
    _choose_rename(app, 0)
    app.snapshot = _snap(_entry_alias("1", "b@x.com", None), _entry_alias("2", "a@x.com", None, active=True))
    _settle(app)
    assert app.switcher.renames == [("set", "1", "home", "a@x.com", None)]  # pinned by email
    titles = [i.title() for i in _account_items(app)]
    assert titles[0].startswith("1  b@x.com  ")
    assert titles[1].startswith("2  home  (a@x.com)")


def test_rename_of_an_account_that_is_gone_is_cancelled(app, window, monkeypatch):
    from claude_swap.exceptions import AccountNotFoundError

    alerts = []
    monkeypatch.setattr(rumps, "alert", lambda **kw: alerts.append(kw) or 1)
    app.snapshot = _snap(_entry_alias("1", "a@x.com", "keep", active=True))
    app.rebuild_menu()
    app.switcher.rename_error = AccountNotFoundError("Account a@x.com is no longer in slot 1")
    window.answer = (1, "home")
    _choose_rename(app, 0)
    _settle(app)
    assert [a["message"] for a in alerts] == ["Account changed, rename cancelled"]
    assert _account_items(app)[0].title().startswith("1  keep  (a@x.com)")


def test_rename_never_blocks_the_main_thread(app, window, monkeypatch):
    from claude_swap.exceptions import LockError

    notes = []
    monkeypatch.setattr(rumps, "notification", lambda *a, **k: notes.append(a))
    app.snapshot = _snap(_entry_alias("1", "a@x.com", None, active=True))
    app.rebuild_menu()
    app.switcher.rename_delay = 2.0
    app.switcher.rename_error = LockError("Failed to acquire lock - another instance may be running")
    window.answer = (1, "home")
    started = time.monotonic()
    _choose_rename(app, 0)
    assert time.monotonic() - started < 0.5  # the prompt closed and the menu is free
    assert notes == []  # nothing yet: the store is still busy
    _settle(app)
    assert notes == [("claude-swap", "Could not rename", "Another cswap process is busy, try again")]


def test_a_refresh_started_before_a_rename_cannot_undo_it(app, window, monkeypatch):
    monkeypatch.setattr(menubar, "_adapt_snapshot", lambda raw: raw)
    gate, calls = threading.Event(), []
    old = _snap(_entry_alias("1", "a@x.com", "old", active=True))
    fresh = _snap(_entry_alias("1", "a@x.com", "home", active=True))

    class Source:
        def take(self, **_kw):
            calls.append(1)
            if len(calls) == 1:
                gate.wait(5)  # an in-flight refresh, still reading the old roster
                return old
            return fresh

    app._snapshot_source = Source()
    app.snapshot = old
    app.rebuild_menu()
    app.refresh_async()  # 1. refresh starts
    window.answer = (1, "home")
    _choose_rename(app, 0)
    _settle(app)  # 2. the rename commits and shows
    assert app.snapshot["accounts"][0][5] == "home"
    gate.set()  # 3. the old refresh completes
    deadline = time.monotonic() + 5
    while app._refreshing and time.monotonic() < deadline:
        time.sleep(0.02)
    assert app.snapshot["accounts"][0][5] == "home"  # its stale result was discarded
    for _ in range(100):  # the follow-up refresh runs once, from the tick
        app.on_sync_tick(None)
        if len(calls) >= 2 and not app._refreshing:
            break
        time.sleep(0.02)
    app.on_sync_tick(None)
    assert len(calls) == 2
    assert _account_items(app)[0].title().startswith("1  home  (a@x.com)")


def test_follow_up_refresh_runs_even_if_the_in_flight_one_fails(app, window, monkeypatch):
    monkeypatch.setattr(menubar, "_adapt_snapshot", lambda raw: raw)
    gate, calls = threading.Event(), []
    fresh = _snap(_entry_alias("1", "a@x.com", "home", active=True))

    class Source:
        def take(self, **_kw):
            calls.append(1)
            if len(calls) == 1:
                gate.wait(5)
                raise RuntimeError("network down")  # the in-flight refresh fails
            return fresh

    app._snapshot_source = Source()
    app.snapshot = _snap(_entry_alias("1", "a@x.com", "old", active=True))
    app.rebuild_menu()
    app.refresh_async()
    window.answer = (1, "home")
    _choose_rename(app, 0)
    _settle(app)
    gate.set()
    for _ in range(150):
        app.on_sync_tick(None)
        if len(calls) >= 2 and not app._refreshing:
            break
        time.sleep(0.02)
    assert len(calls) == 2  # the store is still read back after the rename


def test_rename_pins_the_rows_organization_too(app, window):
    snap = _snap(_entry_alias("1", "a@x.com", None, active=True), _entry_alias("2", "a@x.com", None))
    snap["orgs"] = {"1": "org-a", "2": "org-b"}
    app.snapshot = snap
    app.rebuild_menu()
    window.answer = (1, "home")
    _choose_rename(app, 1)
    _settle(app)
    assert app.switcher.renames == [("set", "2", "home", "a@x.com", "org-b")]


# --- P2-3: compare-generation-and-publish is one step ----------------------------

def test_refresh_cannot_publish_between_its_check_and_a_rename(app, window, monkeypatch):
    monkeypatch.setattr(menubar, "_adapt_snapshot", lambda raw: raw)
    old = _snap(_entry_alias("1", "a@x.com", "old", active=True))

    class Source:
        def take(self, **_kw):
            return old

    gate, entered = threading.Event(), threading.Event()
    real_log = app._log_usage

    def gated_log(snap):
        # The worker has read the old roster and passed any early check.
        if threading.current_thread() is not threading.main_thread() and not entered.is_set():
            entered.set()
            gate.wait(5)
        real_log(snap)

    app._log_usage = gated_log
    app._snapshot_source = Source()
    app.snapshot = old
    app.rebuild_menu()
    app.refresh_async()
    assert entered.wait(5)
    window.answer = (1, "home")
    _choose_rename(app, 0)
    _settle(app)  # the rename commits while the refresh sits in _log_usage
    gate.set()
    deadline = time.monotonic() + 5
    while app._refreshing and time.monotonic() < deadline:
        time.sleep(0.02)
    assert app.snapshot["accounts"][0][5] == "home"


# --- P2-4: one rename at a time, in order -----------------------------------------

def test_renames_run_one_at_a_time_in_order(app):
    app.snapshot = _snap(_entry_alias("1", "a@x.com", None, active=True))
    app.rebuild_menu()
    order = []
    real_set = app.switcher.set_alias

    def slow_set(num, name, **kw):
        order.append(("start", name))
        time.sleep(0.3 if name == "first" else 0.0)
        order.append(("end", name))
        return real_set(num, name, **kw)

    app.switcher.set_alias = slow_set
    app._rename_account("1", "a@x.com", "first")
    app._rename_account("1", "a@x.com", "second")
    _settle(app)
    assert order == [("start", "first"), ("end", "first"), ("start", "second"), ("end", "second")]
    assert app.snapshot["accounts"][0][5] == "second"


def test_rename_submenu_is_disabled_while_a_rename_is_saving(app, window):
    app.snapshot = _snap(_entry_alias("1", "a@x.com", None, active=True), _entry_alias("2", "b@x.com", None))
    app.rebuild_menu()
    app.switcher.rename_delay = 1.0
    window.answer = (1, "first")
    _choose_rename(app, 0)
    app.on_sync_tick(None)  # rebuilds with the rename pending
    items = _submenu(app, "Rename account").itemArray()
    assert all(not i.isEnabled() or i.action() is None for i in items)
    assert all(i.title().endswith(" (saving…)") for i in items)
    made_before = len(window.made)
    item = items[1]
    item.target().callback_(item) if item.action() else None  # a stale click
    app._make_rename("2", "b@x.com", None)(None)  # or a direct call: no prompt either
    assert len(window.made) == made_before
    _settle(app)
    items = _submenu(app, "Rename account").itemArray()
    assert all(i.action() is not None and not i.title().endswith("(saving…)") for i in items)


# --- P2-5: quitting with a rename pending ------------------------------------------

def _quit(app, monkeypatch):
    quits, alerts = [], []
    monkeypatch.setattr(rumps, "quit_application", lambda: quits.append(1))
    monkeypatch.setattr(rumps, "alert", lambda **kw: alerts.append(kw) or 1)
    started = time.monotonic()
    app.on_quit(None)
    return quits, alerts, time.monotonic() - started


def test_quit_waits_for_a_pending_rename_that_finishes(app, monkeypatch):
    app.snapshot = _snap(_entry_alias("1", "a@x.com", None, active=True))
    app.switcher.rename_delay = 1.0
    app._rename_account("1", "a@x.com", "home")
    quits, alerts, took = _quit(app, monkeypatch)
    assert quits == [1] and alerts == []
    assert 0.5 < took < 3.5
    assert app.switcher.renames[-1][:3] == ("set", "1", "home")


def test_quit_warns_when_a_pending_rename_cannot_finish(app, monkeypatch):
    from claude_swap.locking import FileLock

    app.snapshot = _snap(_entry_alias("1", "a@x.com", None, active=True))
    app.switcher.lock_file = app.switcher.backup_dir / ".lock"
    holder = FileLock(app.switcher.lock_file)  # another cswap process, busy past the quit
    assert holder.acquire()
    try:
        app._rename_account("1", "a@x.com", "home")
        quits, alerts, took = _quit(app, monkeypatch)
    finally:
        holder.release()
    assert quits == [1]
    assert [a["message"] for a in alerts] == ["Rename not saved: another cswap process was busy"]
    assert 2.5 < took < 4.5
    assert getattr(app.switcher, "renames", []) == []  # abandoned before the store was called


@pytest.fixture
def real_app(temp_home, tmp_path, monkeypatch):
    """The app over a real switcher and roster, so "saved" means on disk."""
    import json

    from claude_swap.switcher import ClaudeAccountSwitcher

    monkeypatch.setattr(rumps.rumps, "application_support", lambda _name: str(tmp_path))
    monkeypatch.setattr("claude_swap.snapshot_source.SnapshotSource", _NoFetch)
    monkeypatch.setattr(rumps, "notification", lambda *a, **k: None)
    switcher = ClaudeAccountSwitcher()
    switcher._setup_directories()
    switcher.sequence_file.write_text(json.dumps({
        "activeAccountNumber": 1, "lastUpdated": "t", "sequence": [1],
        "accounts": {"1": {"email": "a@x.com", "uuid": "u", "organizationUuid": "",
                           "organizationName": "", "added": "t"}},
    }))
    built = menubar.create_app(switcher)
    deadline = time.monotonic() + 5
    while built._refreshing and time.monotonic() < deadline:
        time.sleep(0.01)
    yield built, switcher
    built.refresh_timer.stop()
    built.sync_timer.stop()


def _alias_on_disk(switcher):
    import json

    return json.loads(switcher.sequence_file.read_text())["accounts"]["1"].get("alias")


def _lock_held_for(switcher, seconds):
    from claude_swap.locking import FileLock

    holder = FileLock(switcher.lock_file)
    assert holder.acquire()
    releaser = threading.Timer(seconds, holder.release)
    releaser.start()
    return releaser


def test_quit_abandons_a_rename_the_lock_holds_past_the_wait(real_app, monkeypatch):
    app, switcher = real_app
    releaser = _lock_held_for(switcher, 4.0)
    app._rename_account("1", "a@x.com", "home")
    _quits, alerts, _took = _quit(app, monkeypatch)
    releaser.join()
    time.sleep(0.5)  # past the release: a rename still alive would write now
    assert [a["message"] for a in alerts] == ["Rename not saved: another cswap process was busy"]
    assert _alias_on_disk(switcher) is None
    assert not app._renames_in_flight


def test_quit_keeps_a_rename_the_lock_lets_through_in_time(real_app, monkeypatch):
    app, switcher = real_app
    releaser = _lock_held_for(switcher, 1.0)
    app._rename_account("1", "a@x.com", "home")
    _quits, alerts, _took = _quit(app, monkeypatch)
    releaser.join()
    assert alerts == []
    assert _alias_on_disk(switcher) == "home"


def test_rename_after_a_swap_tags_the_row_with_the_same_organization(app, window):
    # Same email in two orgs. The prompt showed slot 1 = a@x.com in org-a; a
    # swap then put org-a's account in slot 2. The store renames org-a's
    # account and reports slot 2, but the menu still shows the old snapshot,
    # where slot 2 is org-b's account: the alias belongs on the org-a row.
    snap = _snap(_entry_alias("1", "a@x.com", None, active=True), _entry_alias("2", "a@x.com", None))
    snap["orgs"] = {"1": "org-a", "2": "org-b"}
    app.snapshot = snap
    app.rebuild_menu()
    app.switcher.rename_lands_in = "2"
    window.answer = (1, "home")
    _choose_rename(app, 0)
    _settle(app)
    assert app.switcher.renames == [("set", "1", "home", "a@x.com", "org-a")]
    titles = [i.title() for i in _account_items(app)]
    assert titles[0].startswith("1  home  (a@x.com)")
    assert titles[1].startswith("2  a@x.com  ")


def test_quit_right_after_a_rejected_rename_shows_the_rejection(app, monkeypatch):
    from claude_swap.exceptions import ValidationError

    app.snapshot = _snap(_entry_alias("1", "a@x.com", None, active=True))
    app.switcher.rename_delay = 0.3  # still saving when Quit is chosen
    app.switcher.rename_error = ValidationError("Alias 'work' is already used by account 2")
    app._rename_account("1", "a@x.com", "work")
    quits, alerts, _took = _quit(app, monkeypatch)
    assert quits == [1]
    assert [a["message"] for a in alerts] == ["Alias 'work' is already used by account 2"]


def test_quit_before_the_tick_drained_a_finished_rejection_shows_it(app, monkeypatch):
    from claude_swap.exceptions import ValidationError

    app.snapshot = _snap(_entry_alias("1", "a@x.com", None, active=True))
    app.switcher.rename_error = ValidationError("Alias 'work' is already used by account 2")
    app._rename_account("1", "a@x.com", "work")
    deadline = time.monotonic() + 5
    while app._renames_in_flight and time.monotonic() < deadline:
        time.sleep(0.02)  # finished, but no sync tick has run
    quits, alerts, _took = _quit(app, monkeypatch)
    assert quits == [1]
    assert [a["message"] for a in alerts] == ["Alias 'work' is already used by account 2"]


def test_rename_landing_between_the_refresh_check_and_publish_waits_for_it(app, monkeypatch):
    # The refresh worker's generation check and its publish are one step
    # under the snapshot lock: a rename cannot land in between.
    monkeypatch.setattr(menubar, "_adapt_snapshot", lambda raw: raw)
    old = _snap(_entry_alias("1", "a@x.com", "old", active=True))
    stored = _snap(_entry_alias("1", "a@x.com", "home", active=True))
    reads = []

    class Source:
        def take(self, **_kw):
            # First read predates the rename; later reads see it stored.
            reads.append(1)
            return old if len(reads) == 1 else stored

    holder = {"value": 0, "fired": False}

    def get_gen(self):
        value = holder["value"]
        if threading.current_thread() is not threading.main_thread() and not holder["fired"] \
                and threading.current_thread().name != "rename-helper":
            holder["fired"] = True
            helper = threading.Thread(
                target=app._apply_rename, args=("1", "a@x.com", None, "home"), name="rename-helper"
            )
            helper.start()
            helper.join(0.5)  # a rename tries to land right after the check read
        return value

    def set_gen(self, v):
        holder["value"] = v

    monkeypatch.setattr(type(app), "_snapshot_gen", property(get_gen, set_gen), raising=False)
    app._snapshot_source = Source()
    app.snapshot = old
    app.refresh_async()
    deadline = time.monotonic() + 5
    while (app._refreshing or app.snapshot["accounts"][0][5] != "home") and time.monotonic() < deadline:
        time.sleep(0.02)
    assert holder["fired"]
    assert app.snapshot["accounts"][0][5] == "home"


def test_rename_submenu_is_usable_again_after_a_failed_rename(app, window, monkeypatch):
    from claude_swap.exceptions import ValidationError

    monkeypatch.setattr(rumps, "alert", lambda **kw: 1)
    app.snapshot = _snap(_entry_alias("1", "a@x.com", None, active=True))
    app.rebuild_menu()
    app.switcher.rename_error = ValidationError("alias 'x@y' may only contain letters")
    app.switcher.rename_delay = 0.3
    window.answer = (1, "x@y")
    _choose_rename(app, 0)
    app.on_sync_tick(None)  # rebuilt while saving
    assert all(i.title().endswith("(saving…)") for i in _submenu(app, "Rename account").itemArray())
    _settle(app)  # the failure comes back: nothing else would rebuild the menu
    items = _submenu(app, "Rename account").itemArray()
    assert all(i.action() is not None and not i.title().endswith("(saving…)") for i in items)


def test_a_quit_during_the_lock_check_still_abandons(app, monkeypatch):
    # Quit gives up on the rename in the moment the lock is found free:
    # the rename must not go on to the store.
    from claude_swap.locking import FileLock

    app.switcher.lock_file = app.switcher.backup_dir / ".lock"
    real_acquire = FileLock.acquire

    def acquire(self, timeout=None):
        got = real_acquire(self, timeout)
        app._renames_abandoned = True
        return got

    monkeypatch.setattr(FileLock, "acquire", acquire)
    outcome = app._run_rename("1", "a@x.com", "home", None)
    assert outcome == ("abandoned",)
    assert getattr(app.switcher, "renames", []) == []


def test_an_unwritable_lock_file_fails_the_rename_and_frees_the_menu(app, window, monkeypatch):
    import os

    alerts = []
    monkeypatch.setattr(rumps, "alert", lambda **kw: alerts.append(kw) or 1)
    lock = app.switcher.backup_dir / ".lock"
    lock.write_text("")
    os.chmod(lock, 0o444)  # opening it for writing raises PermissionError
    app.switcher.lock_file = lock
    app.snapshot = _snap(_entry_alias("1", "a@x.com", None, active=True))
    app.rebuild_menu()
    window.answer = (1, "home")
    _choose_rename(app, 0)
    _settle(app)
    assert [a["message"] for a in alerts] == [
        f"Could not rename: [Errno 13] Permission denied: '{lock}'"
    ]
    items = _submenu(app, "Rename account").itemArray()
    assert all(i.action() is not None and not i.title().endswith("(saving…)") for i in items)
    quits, quit_alerts, took = _quit(app, monkeypatch)
    assert quits == [1] and quit_alerts == [] and took < 1.0


def test_a_probe_that_fails_once_fails_only_that_rename(app, monkeypatch):
    from claude_swap.locking import FileLock

    alerts = []
    monkeypatch.setattr(rumps, "alert", lambda **kw: alerts.append(kw) or 1)
    app.switcher.lock_file = app.switcher.backup_dir / ".lock"
    real_acquire = FileLock.acquire
    calls = []

    def acquire(self, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            raise OSError(5, "Input/output error")
        return real_acquire(self, timeout)

    monkeypatch.setattr(FileLock, "acquire", acquire)
    app.snapshot = _snap(_entry_alias("1", "a@x.com", None, active=True))
    app._rename_account("1", "a@x.com", "home")
    app._rename_account("1", "a@x.com", "work")
    _settle(app)
    assert [a["message"] for a in alerts] == ["Could not rename: [Errno 5] Input/output error"]
    assert [r[:3] for r in app.switcher.renames] == [("set", "1", "work")]
    assert not app._renames_in_flight


def test_the_prompt_keeps_a_capitalised_name(real_app):
    # Owner report: "I cannot capitalise the first letter of the name".
    app, switcher = real_app
    app.snapshot = _snap(_entry_alias("1", "a@x.com", None, active=True))
    app._rename_account("1", "a@x.com", "Personal")
    _settle(app)
    assert _alias_on_disk(switcher) == "Personal"
    assert _account_items(app)[0].title().startswith("1  Personal  (a@x.com)")


def test_a_name_with_a_space_is_refused_with_the_reason(app, window, monkeypatch):
    alerts = []
    monkeypatch.setattr(rumps, "alert", lambda **kw: alerts.append(kw) or 1)
    app.snapshot = _snap(_entry_alias("1", "a@x.com", None, active=True))
    app.rebuild_menu()
    window.answer = (1, "My Work")
    _choose_rename(app, 0)
    _settle(app)
    assert [a["message"] for a in alerts] == [
        "Names cannot contain spaces (they are used on the command line); use - or _"
    ]
    assert getattr(app.switcher, "renames", []) == []



def test_the_prompt_drops_invisible_characters(app, window):
    # Owner report: "Personal" was refused; the text carried a zero width space.
    app.snapshot = _snap(_entry_alias("1", "a@x.com", None, active=True))
    app.rebuild_menu()
    window.answer = (1, "Personal\u200b")
    _choose_rename(app, 0)
    _settle(app)
    assert app.switcher.renames == [("set", "1", "Personal", "a@x.com", None)]


def test_the_prompt_turns_off_text_substitutions():
    # Replacement, correction, completion and inline prediction can all put
    # text into the field that the user did not type.
    AppKit.NSApplication.sharedApplication()
    prompt = rumps.Window(title="t", message="m", default_text="", ok="OK", dimensions=(320, 24))
    menubar.plain_text_entry(prompt)
    field, win = prompt._textfield, prompt._alert.window()
    win.makeFirstResponder_(field)
    editor = win.fieldEditor_forObject_(False, field)
    assert not editor.isAutomaticTextReplacementEnabled()
    assert not editor.isAutomaticSpellingCorrectionEnabled()
    assert not editor.isAutomaticTextCompletionEnabled()
    assert not editor.isAutomaticQuoteSubstitutionEnabled()
    assert not editor.isAutomaticDashSubstitutionEnabled()
    if editor.respondsToSelector_("inlinePredictionType"):  # macOS 14 and later
        assert editor.inlinePredictionType() == AppKit.NSTextInputTraitTypeNo



def test_the_rename_prompt_is_a_plain_text_entry(app, monkeypatch):
    AppKit.NSApplication.sharedApplication()
    shown = []

    def run(self):
        shown.append(self)
        return rumps.rumps.Response(0, "")  # cancelled

    monkeypatch.setattr(rumps.rumps.Window, "run", run)
    app.snapshot = _snap(_entry_alias("1", "a@x.com", None, active=True))
    app.rebuild_menu()
    _choose_rename(app, 0)
    (prompt,) = shown
    field, win = prompt._textfield, prompt._alert.window()
    win.makeFirstResponder_(field)
    editor = win.fieldEditor_forObject_(False, field)
    assert not editor.isAutomaticTextReplacementEnabled()
    if editor.respondsToSelector_("inlinePredictionType"):  # macOS 14 and later
        assert editor.inlinePredictionType() == AppKit.NSTextInputTraitTypeNo


@pytest.mark.parametrize("missing", ["editor", "window"])
def test_a_prompt_without_a_field_editor_still_opens(missing):
    # A nil editor (or window) leaves the field as it is: the prompt must open.
    class Window:
        def fieldEditor_forObject_(self, create, field):
            return None

    class Alert:
        def window(self):
            return None if missing == "window" else Window()

    class Prompt:
        _textfield = object()
        _alert = Alert()

    menubar.plain_text_entry(Prompt())  # returns without raising
