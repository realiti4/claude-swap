"""Live readings (``cswap proxy``) on every display surface: list, TUI, snapshots."""

from __future__ import annotations

import time

import pytest

from claude_swap.snapshot_source import SnapshotSource
from claude_swap.switcher import _usage_entry_lines
from claude_swap.tui.widgets import account_card_text
from claude_swap.usage_store import STALE_OK_S, FetchRecord, UsageEntry
from tests.test_autoswitch import EngineHarness, _usage
from tests.test_tui import make_account

POLLED = {
    "five_hour": {"pct": 54.0},
    "seven_day": {"pct": 47.0},
    "scoped": [{"name": "Fable", "pct": 30.0}],
}
LIVE = {"five_hour": {"pct": 66.0}, "seven_day": {"pct": 48.0}}


def _entry(now: float, *, poll_age: float = 657.0, live_age: float | None = 6.0) -> UsageEntry:
    return UsageEntry(
        last_good=POLLED,
        fetched_at=now - poll_age,
        age_s=poll_age,
        live_usage=LIVE if live_age is not None else None,
        live_at=now - live_age if live_age is not None else None,
        live_age_s=live_age,
    )


class TestForDisplay:
    def test_a_newer_live_reading_is_what_the_user_sees(self):
        now = time.time()
        shown = _entry(now).for_display()
        assert shown.last_good["five_hour"] == {"pct": 66.0}
        assert shown.last_good["seven_day"] == {"pct": 48.0}
        assert shown.last_good["scoped"] == POLLED["scoped"]
        assert shown.fetched_at == now - 6.0
        assert shown.age_s == 6.0
        assert shown.shows_live

    def test_a_newer_poll_is_shown_as_it_is(self):
        now = time.time()
        entry = _entry(now, poll_age=2.0, live_age=30.0)
        assert entry.for_display() == entry
        assert not entry.for_display().shows_live

    def test_without_a_live_reading_nothing_changes(self):
        entry = _entry(time.time(), live_age=None)
        assert entry.for_display() == entry
        assert not entry.shows_live

    def test_an_old_live_reading_is_still_shown_with_its_age(self):
        # Display shows older data annotated with its age (decisions do not:
        # they drop it after STALE_OK_S). The newest measurement is the one
        # to show, whichever source took it.
        now = time.time()
        shown = _entry(now, poll_age=5000.0, live_age=STALE_OK_S + 600).for_display()
        assert shown.last_good["five_hour"] == {"pct": 66.0}
        assert shown.age_s == STALE_OK_S + 600

    def test_is_idempotent(self):
        shown = _entry(time.time()).for_display()
        assert shown.for_display() == shown
        assert shown.shows_live

    def test_live_alone_when_nothing_was_polled(self):
        now = time.time()
        entry = UsageEntry(live_usage=LIVE, live_at=now - 3, live_age_s=3.0)
        shown = entry.for_display()
        assert shown.last_good == LIVE
        assert shown.shows_live


class TestCswapList:
    def test_list_lines_show_the_live_numbers_and_say_so(self):
        lines = "\n".join(_usage_entry_lines(_entry(time.time())))
        assert "66%" in lines
        assert "54%" not in lines
        assert "live" in lines

    def test_a_polled_reading_is_not_called_live(self):
        lines = "\n".join(_usage_entry_lines(_entry(time.time(), live_age=None)))
        assert "54%" in lines
        assert "live" not in lines


class TestTuiCard:
    def test_card_shows_the_live_numbers_and_a_live_badge(self):
        entry = _entry(time.time()).for_display()
        card = account_card_text(make_account(1, active=True, entry=entry), 100).plain
        assert "66%" in card
        assert "live" in card

    def test_card_for_a_polled_reading_has_no_live_badge(self):
        entry = _entry(time.time(), live_age=None)
        card = account_card_text(make_account(1, active=True, entry=entry), 100).plain
        assert "live" not in card


def _harness(temp_home) -> EngineHarness:
    h = EngineHarness(temp_home)
    h.seed(1, "a@example.com")
    h.seed(2, "b@example.com")
    h.make_live("a@example.com", 1)
    ids = _ids(h)
    h.switcher._usage_store.record(
        {"1": FetchRecord(usage=_usage(54.0)), "2": FetchRecord(usage=_usage(0.0))}, ids
    )
    return h


def _ids(h: EngineHarness) -> dict:
    accounts = h.switcher._get_sequence_data()["accounts"]
    return {n: (i["email"], i.get("organizationUuid", "")) for n, i in accounts.items()}


def _pct(snap, num: str) -> float:
    acc = next(a for a in snap.accounts if a.number == num)
    return acc.usage.last_good["five_hour"]["pct"]


class TestSnapshots:
    def test_accounts_snapshot_carries_the_live_reading(self, temp_home):
        h = _harness(temp_home)
        h.clock.advance(30)
        h.switcher._usage_store.record_live({"1": LIVE}, {"1": _ids(h)["1"]})
        snap = h.switcher.accounts_snapshot(fetch=set())
        acc = next(a for a in snap.accounts if a.number == "1")
        assert acc.usage.last_good["five_hour"]["pct"] == 66.0
        assert acc.usage.shows_live
        assert _pct(snap, "2") == 0.0

    def test_the_tui_store_repaint_picks_up_each_new_live_reading(self, temp_home):
        # The TUI repaints from a store-only snapshot every few seconds; a
        # proxy writing new readings must move the bars without any fetch.
        h = _harness(temp_home)
        source = SnapshotSource(h.switcher)
        assert _pct(source.take(store_only=True), "1") == 54.0
        h.clock.advance(10)
        h.switcher._usage_store.record_live({"1": LIVE}, {"1": _ids(h)["1"]})
        first = source.take(store_only=True)
        assert _pct(first, "1") == 66.0
        h.clock.advance(10)
        h.switcher._usage_store.record_live(
            {"1": {"five_hour": {"pct": 71.0}, "seven_day": {"pct": 49.0}}},
            {"1": _ids(h)["1"]},
        )
        second = source.take(store_only=True)
        assert _pct(second, "1") == 71.0
        # A newer stamp is what makes the dashboard flash the row as refreshed.
        stamp = lambda s: next(a for a in s.accounts if a.number == "1").usage.fetched_at  # noqa: E731
        assert stamp(second) > stamp(first)

    def test_watch_stamps_move_with_live_readings(self, temp_home):
        h = _harness(temp_home)
        before = h.switcher.usage_fetch_stamps()["1"]
        h.clock.advance(10)
        h.switcher._usage_store.record_live({"1": LIVE}, {"1": _ids(h)["1"]})
        assert h.switcher.usage_fetch_stamps()["1"] > before


@pytest.mark.asyncio
class TestTuiRepaint:
    async def test_the_running_dashboard_moves_to_the_live_reading(self, temp_home):
        # The real app on a real switcher, refreshing store-only (as it does
        # while `cswap auto` owns fetching): a reading the proxy writes shows
        # on the next poll tick, with its live badge, and no fetch is needed.
        from claude_swap.tui.app import CswapApp
        from claude_swap.tui.widgets import AccountsPanel
        from tests.test_tui import settle

        h = _harness(temp_home)
        app = CswapApp(h.switcher)
        app._store_only = True
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            before = app.screen.query_one(AccountsPanel).render().plain
            assert "54%" in before
            assert "● live" not in before

            h.clock.advance(10)
            h.switcher._usage_store.record_live({"1": LIVE}, {"1": _ids(h)["1"]})
            app._tick()
            await settle(pilot)
            after = app.screen.query_one(AccountsPanel).render().plain
            assert "66%" in after
            assert "● live" in after
