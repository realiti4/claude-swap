"""The weekly-room chart above the watch screen's account cards.

Pilot tests run the real app against ``FakeSwitcher`` (see test_tui.py); the
history-capable fake serves the made-up demo history, so the chart has real
shape without any files.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from datetime import timezone

import pytest
from rich.text import Text

from claude_swap.history_demo import demo_history
from claude_swap.history_forecast import analyse
from claude_swap.history_render import render_overview
from claude_swap.tui import history_panel
from claude_swap.tui.history_panel import HistoryPanel, chart_rows_for, fit_overview
from tests.test_history_render import plain
from tests.test_tui import FakeSwitcher, make_account, make_app, make_entry, settle

UTC = timezone.utc


class HistoryFakeSwitcher(FakeSwitcher):
    """FakeSwitcher plus ``usage_history``, serving the demo history."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.history_calls = 0

    def usage_history(self, *, since: float, now: float | None = None):
        self.history_calls += 1
        return demo_history(time.time() if now is None else now)


def _fake(tmp_path, cls=HistoryFakeSwitcher):
    return cls([make_account(1, active=True), make_account(2)], tmp_path)


def _panel_text(app) -> str:
    panel = app.screen.query_one(HistoryPanel)
    content = panel.content
    return content.plain if isinstance(content, Text) else str(content)


async def _open_watch(pilot) -> None:
    await settle(pilot)
    await pilot.press("w")
    await pilot.pause()
    await settle(pilot)


# -- the renderer's short form ------------------------------------------------


class TestShortOverview:
    def _lines(self, **kw):
        a = analyse(demo_history(1_793_000_000.0))
        return plain(render_overview(a, days=7, width=100, unicode=False, tz=UTC, color=False, **kw), 100)

    def test_full_form_unchanged_by_default(self):
        text = self._lines()
        assert "Room on each account now" in text
        assert "A day = accounts' worth" in text

    def test_short_form_drops_the_account_list_and_pace_notes(self):
        text = self._lines(pace_notes=False, accounts_now=False)
        assert "Weekly room left" in text
        assert "Last 24 hours" in text  # the pace rows stay
        assert "Room on each account now" not in text
        assert "A day = accounts' worth" not in text

    def test_chart_rows_sets_the_chart_height(self):
        a = analyse(demo_history(1_793_000_000.0))

        def count(rows: int) -> int:
            return len(render_overview(a, days=7, width=100, unicode=False, tz=UTC, chart_rows=rows))

        assert count(12) - count(6) == 6  # only the chart grows

    def test_without_the_pace_table(self):
        text = self._lines(pace_table=False, accounts_now=False)
        assert "Last 24 hours" not in text
        assert "if no more use" in text  # the legend stays


class TestFitOverview:
    a = analyse(demo_history(1_793_000_000.0))

    def _fit(self, budget, rows=10):
        lines = fit_overview(self.a, width=100, budget=budget, rows=rows)
        return None if lines is None else len(lines), lines

    def _full_height(self, rows=10):
        return len(fit_overview(self.a, width=100, budget=1000, rows=rows))

    @staticmethod
    def _has_paces(lines) -> bool:
        return "Last 24 hours" in plain(lines, 100)

    def test_room_to_spare_gets_the_chart_asked_for_and_the_paces(self):
        n, lines = self._fit(1000)
        assert self._has_paces(lines)
        assert n == self._full_height()

    def test_a_little_short_shortens_the_chart_and_keeps_the_paces(self):
        budget = self._full_height() - 3
        n, lines = self._fit(budget)
        assert self._has_paces(lines)
        assert n <= budget

    def test_shorter_still_drops_the_paces(self):
        budget = self._full_height(history_panel.PACE_MIN_ROWS) - 1
        n, lines = self._fit(budget)
        assert lines is not None and not self._has_paces(lines)
        assert n <= budget

    def test_then_the_compact_form(self):
        chart_alone_at_floor = len(render_overview(
            self.a, days=7, width=100, unicode=True, chart_rows=history_panel.MIN_ROWS,
            pace_table=False, accounts_now=False,
        ))
        n, lines = self._fit(chart_alone_at_floor - 1)
        assert lines is not None and n <= chart_alone_at_floor - 1
        text = plain(lines, 100)
        assert "if no more use" not in text  # no legend
        assert "comes back in the next 24 hours" not in text
        assert "6h " in text  # the right-edge labels still name the paces

    def test_too_short_for_any_chart(self):
        compact_at_floor = len(render_overview(
            self.a, days=7, width=100, unicode=True, chart_rows=history_panel.MIN_ROWS,
            pace_table=False, accounts_now=False, compact=True,
        ))
        assert self._fit(compact_at_floor)[1] is not None
        assert self._fit(compact_at_floor - 1)[1] is None


def test_chart_rows_scale_with_the_terminal_and_stay_in_range():
    assert chart_rows_for(10) == history_panel.MIN_ROWS
    assert chart_rows_for(50) == 10
    assert chart_rows_for(300) == history_panel.MAX_ROWS


# -- the panel on the watch screen ------------------------------------------


@pytest.mark.asyncio
class TestWatchScreenChart:
    async def test_chart_sits_above_the_cards(self, tmp_path):
        app = make_app(_fake(tmp_path))
        async with app.run_test(size=(110, 50)) as pilot:
            await _open_watch(pilot)
            text = _panel_text(app)
            assert "Weekly room left" in text
            assert "Last 6 hours" in text
            assert "Room on each account now" not in text  # the cards show that
            panel = app.screen.query_one(HistoryPanel)
            cards = app.screen.query_one("#accounts")
            assert panel.display and panel.region.y < cards.region.y

    async def test_h_hides_and_shows_it(self, tmp_path):
        app = make_app(_fake(tmp_path))
        async with app.run_test(size=(110, 50)) as pilot:
            await _open_watch(pilot)
            panel = app.screen.query_one(HistoryPanel)
            await pilot.press("h")
            await pilot.pause()
            assert not panel.display
            calls = app.switcher.history_calls
            await pilot.press("h")
            await settle(pilot)
            assert panel.display
            assert app.switcher.history_calls == calls + 1  # rebuilt on show

    async def test_only_a_changed_reading_rebuilds_it(self, tmp_path, monkeypatch):
        fake = _fake(tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(110, 50)) as pilot:
            await _open_watch(pilot)
            monkeypatch.setattr(history_panel, "REFRESH_S", 0.0)
            calls = fake.history_calls
            assert calls >= 1
            app._tick()  # a poll with the same readings: nothing new to draw
            await settle(pilot)
            assert fake.history_calls == calls
            fake._accounts = [
                dataclasses.replace(acc, usage=make_entry(pct7=55.0)) for acc in fake._accounts
            ]
            app._tick()  # a new reading
            await settle(pilot)
            assert fake.history_calls == calls + 1

    async def test_a_changed_reading_waits_out_the_refresh_gap(self, tmp_path):
        fake = _fake(tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(110, 50)) as pilot:
            await _open_watch(pilot)
            calls = fake.history_calls
            fake._accounts = [
                dataclasses.replace(acc, usage=make_entry(pct7=55.0)) for acc in fake._accounts
            ]
            app._tick()  # inside the 10 s gap since the first build
            await settle(pilot)
            assert fake.history_calls == calls
            panel = app.screen.query_one(HistoryPanel)
            panel._built_at -= history_panel.REFRESH_S  # the gap has passed
            app._tick()  # the next poll picks the change up
            await settle(pilot)
            assert fake.history_calls == calls + 1

    async def test_resize_redraws_at_the_new_size(self, tmp_path):
        app = make_app(_fake(tmp_path))
        async with app.run_test(size=(110, 50)) as pilot:
            await _open_watch(pilot)
            calls = app.switcher.history_calls
            await pilot.resize_terminal(140, 60)
            await settle(pilot)
            assert app.switcher.history_calls > calls
            widest = max(len(line) for line in _panel_text(app).splitlines())
            assert 110 < widest <= 134  # the new width, less the panel's padding

    async def test_a_taller_terminal_gets_a_taller_chart(self, tmp_path):
        app = make_app(_fake(tmp_path))
        async with app.run_test(size=(110, 44)) as pilot:
            await _open_watch(pilot)
            short = len(_panel_text(app).splitlines())
            await pilot.resize_terminal(110, 70)  # same width, taller
            await settle(pilot)
            assert len(_panel_text(app).splitlines()) > short

    @pytest.mark.parametrize(
        "size",
        [(80, 24), (80, 18), (100, 30), (110, 50), (200, 60), (56, 24), (57, 24), (56, 40), (57, 40)],
    )
    async def test_it_never_takes_more_than_half_the_screen(self, tmp_path, size):
        app = make_app(_fake(tmp_path))
        async with app.run_test(size=size) as pilot:
            await _open_watch(pilot)
            panel = app.screen.query_one(HistoryPanel)
            cards = app.screen.query_one("#accounts")
            width, height = size
            assert panel.outer_size.height + 1 <= height // 2  # its margin too
            assert cards.region.y + 2 < height  # the cards start on screen
            assert app.screen.max_scroll_y == 0  # the screen itself never scrolls

    # 56-57 columns at 40 rows: the chart draws at its narrowest width, where a
    # screen scrollbar would flip it to the "Widen" note and back forever.
    @pytest.mark.parametrize("size", [(56, 24), (57, 24), (56, 40), (57, 40), (80, 24)])
    async def test_settles_instead_of_rebuilding_forever(self, tmp_path, size):
        fake = _fake(tmp_path)
        app = make_app(fake)
        async with app.run_test(size=size) as pilot:
            await _open_watch(pilot)
            calls = fake.history_calls
            await asyncio.sleep(0.5)
            await settle(pilot)
            assert fake.history_calls == calls

    async def test_a_standard_80x24_terminal_still_gets_a_chart(self, tmp_path):
        app = make_app(_fake(tmp_path))
        async with app.run_test(size=(80, 24)) as pilot:
            await _open_watch(pilot)
            text = _panel_text(app)
            assert "Weekly room left" in text and "now" in text
            assert "Make the window taller" not in text

    async def test_a_short_terminal_shows_one_line_instead(self, tmp_path):
        app = make_app(_fake(tmp_path))
        async with app.run_test(size=(110, 18)) as pilot:
            await _open_watch(pilot)
            assert _panel_text(app).startswith("Make the window taller")

    async def test_too_narrow_says_so(self, tmp_path):
        app = make_app(_fake(tmp_path))
        async with app.run_test(size=(52, 40)) as pilot:
            await _open_watch(pilot)
            assert "Widen the window" in _panel_text(app)

    async def test_a_failing_history_read_shows_a_note_not_a_crash(self, tmp_path):
        class Broken(HistoryFakeSwitcher):
            def usage_history(self, *, since, now=None):
                raise OSError("torn file")

        app = make_app(_fake(tmp_path, Broken))
        async with app.run_test(size=(110, 50)) as pilot:
            await _open_watch(pilot)
            assert "Usage chart unavailable" in _panel_text(app)
            assert app.screen.query_one("#accounts")  # cards still there

    async def test_switcher_without_history_hides_it(self, tmp_path):
        app = make_app(_fake(tmp_path, FakeSwitcher))
        async with app.run_test(size=(110, 50)) as pilot:
            await _open_watch(pilot)
            panel = app.screen.query_one(HistoryPanel)
            assert not panel.display
            await pilot.press("h")  # nothing to show: stays hidden
            await pilot.pause()
            assert not panel.display
            assert app.screen.check_action("toggle_chart", ()) is False  # not in the footer
