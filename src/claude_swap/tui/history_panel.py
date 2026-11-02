"""The weekly-room chart from ``cswap history``, kept current above the
watch screen's account cards.

It draws what ``cswap history`` draws (``history_render.render_overview``),
shortened to fit over the cards: the headline, the chart and the pace rows,
without the per-account list the cards already show. Building it is a file
read plus some arithmetic (tens of milliseconds), done in a thread worker so
the UI loop never touches the history files.

**Size.** It takes at most half the screen, so the cards keep the rest. On a
shorter terminal it shortens the chart, then drops the pace rows, then the
line about resets and the legend; only when even that won't fit does it say
so in one line.

**Freshness.** It rebuilds when an account's stored reading changes (at most
every ``REFRESH_S``), when its width or the screen's height changes, and
every ``TICK_S`` regardless, so the "now" line and the forecasts move even
while nothing is being checked. Size changes drive the first build and the
one after being shown again, so it is always drawn for the room it has.
"""

from __future__ import annotations

import time
from functools import partial
from typing import TYPE_CHECKING

from rich.text import Text
from textual.widgets import Static
from textual.worker import Worker, get_current_worker

from claude_swap.history_forecast import DAY_S, Analysis, analyse
from claude_swap.history_render import MIN_WIDTH, MUTED, render_overview
from claude_swap.models import AccountsSnapshot

if TYPE_CHECKING:
    from claude_swap.tui.app import CswapApp

REFRESH_S = 10.0  # a changed reading shows within this; snapshots arrive every 3 s
TICK_S = 60.0  # rebuild at least this often, so "now" keeps moving
DAYS = 7.0  # how far back the chart looks; the forecast always runs 7 days ahead
MIN_ROWS, MAX_ROWS = 4, 12  # chart height range
PACE_MIN_ROWS = 6  # shorten the chart this far before dropping the pace rows
SCREEN_SHARE = 0.5  # the most of the screen's height the panel may take


def chart_rows_for(screen_height: int) -> int:
    """The chart height to aim for on a terminal ``screen_height`` rows tall:
    about a fifth of it. :func:`fit_overview` shortens it if the rest of the
    panel doesn't fit."""
    return max(MIN_ROWS, min(MAX_ROWS, screen_height // 5))


def fit_overview(a: Analysis, *, width: int, budget: int, rows: int) -> list[Text] | None:
    """The short overview in at most ``budget`` lines. In order, the first
    that fits: ``rows`` of chart with the pace rows, or a shorter chart (down
    to ``PACE_MIN_ROWS``) with them; the chart without them (down to
    ``MIN_ROWS``); the compact form, without the line about resets and the
    legend (down to ``MIN_ROWS``). None when not even that fits."""

    def render(chart_rows: int, paces: bool, compact: bool) -> list[Text]:
        return render_overview(
            a, days=DAYS, width=width, unicode=True, color=True, chart_rows=chart_rows,
            pace_table=paces, pace_notes=False, accounts_now=False, compact=compact,
        )

    for paces, compact, floor in (
        (True, False, PACE_MIN_ROWS),
        (False, False, MIN_ROWS),
        (False, True, MIN_ROWS),
    ):
        lines = render(rows, paces, compact)
        if len(lines) <= budget:
            return lines
        # Every chart row is one line, so the overshoot is how many to drop.
        shorter = rows - (len(lines) - budget)
        if shorter >= floor:
            lines = render(shorter, paces, compact)
            if len(lines) <= budget:
                return lines
    return None


class HistoryPanel(Static):
    """Weekly room left across every account, with forecasts at recent paces."""

    app: "CswapApp"

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(Text("loading usage history…", style=MUTED), id=id)
        self._built_at = 0.0
        self._built_for: tuple[int, int] | None = None  # (width, screen height)
        self._built_stamps: tuple | None = None  # the readings it was built from

    @property
    def supported(self) -> bool:
        """False for a switcher without a usage history (nothing to draw)."""
        return getattr(self.app.switcher, "usage_history", None) is not None

    def on_mount(self) -> None:
        if not self.supported:
            self.display = False
            return
        # init=False: the first build waits for the first layout (on_resize).
        self.watch(self.app, "snapshot", self._on_snapshot, init=False)
        self.set_interval(TICK_S, self.rebuild)

    def toggle(self) -> None:
        """Hide or show the chart. Showing it lays it out again, and that
        resize rebuilds it, so a chart hidden for an hour comes back current."""
        if not self.supported:
            return
        self.display = not self.display
        if not self.display:
            self._built_for = None

    @staticmethod
    def _stamps(snap: AccountsSnapshot) -> tuple:
        return tuple((acc.number, acc.usage.fetched_at) for acc in snap.accounts)

    def _on_snapshot(self, snap: AccountsSnapshot | None) -> None:
        # Every poll brings a new snapshot; only a changed reading is news.
        if snap is None or self._stamps(snap) == self._built_stamps:
            return
        if time.time() - self._built_at >= REFRESH_S:
            self.rebuild()
        # Inside the gap the stamps stay unequal, so a later poll picks it up.

    def on_resize(self) -> None:
        self.check_layout()

    def check_layout(self) -> None:
        """Rebuild if its width or the screen's height changed. The screen
        calls this too: a taller terminal means a taller chart, which this
        widget's own size wouldn't show until it is rebuilt."""
        if self.content_size.width and self._built_for != self._layout():
            self.rebuild()

    def _layout(self) -> tuple[int, int]:
        width = self.content_size.width or (self.app.size.width - self.styles.gutter.width)
        return width, self.app.size.height

    def _budget(self, screen_height: int) -> int:
        """Lines of content it may use: its share of the screen, less its
        own margin and border."""
        return int(screen_height * SCREEN_SHARE) - self.styles.gutter.height - self.styles.margin.height

    def rebuild(self) -> None:
        """Rebuild the chart in a worker; a newer request replaces an older one."""
        if not self.display or not self.supported:
            return
        self._built_at = time.time()
        width, height = self._layout()
        self._built_for = (width, height)
        snap = self.app.snapshot
        self._built_stamps = self._stamps(snap) if snap is not None else None
        gutter = self.styles.gutter.width  # style reads stay on the UI thread
        self.run_worker(
            partial(
                self._build_blocking, width, self._budget(height), chart_rows_for(height), gutter
            ),
            thread=True,
            exclusive=True,
            group="history-panel",
            exit_on_error=False,
            name="history-panel",
        )

    def _build_blocking(self, width: int, budget: int, rows: int, gutter: int) -> None:
        worker = get_current_worker()
        text = self._render_text(width, budget, rows, gutter)
        self.app.call_from_thread(self._apply, worker, text)

    def _apply(self, worker: Worker, text: Text) -> None:
        # Checked here, on the UI thread where cancelling happens, so an
        # older build finishing after a newer one never overwrites it.
        if not worker.is_cancelled:
            self.update(text)

    def _render_text(self, width: int, budget: int, rows: int, gutter: int) -> Text:
        if width < MIN_WIDTH:
            return Text(
                f"Widen the window to {MIN_WIDTH + gutter} columns "
                "to see the usage chart.",
                style=MUTED,
            )
        try:
            now = time.time()
            history = self.app.switcher.usage_history(since=now - DAYS * DAY_S, now=now)
            if not history.accounts:
                return Text("No accounts yet.", style=MUTED)
            lines = fit_overview(analyse(history), width=width, budget=budget, rows=rows)
        except Exception as e:  # a torn file must never take the watch screen down
            return Text(f"Usage chart unavailable: {e}", style=MUTED)
        if lines is None:
            return Text(
                "Make the window taller to see the usage chart (h hides this line).",
                style=MUTED,
            )
        text = Text("\n").join(lines)
        text.no_wrap = True
        text.overflow = "crop"
        return text
