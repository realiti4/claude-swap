"""Terminal views of the usage history (``cswap history``), and their JSON.

The overview is one chart: weekly room left across every account, in
accounts' worth, over the chosen range; to the right of "now" a week ahead
with a dashed line per recent pace (30 minutes, 6 hours, 24 hours) and a
dotted line for the room that comes back as each account's weekly limit
resets with no more use. Under it, the pace table and each account's room
now.

Drawing is on a dot canvas: Unicode braille (2x4 dots a cell) where the
terminal's encoding can show it, plain ASCII markers (one per cell) where it
cannot or with ``--ascii``. A cell holds one character and one colour, so
where lines cross, the dots merge and the higher-priority line's colour wins
(the recorded room above the forecasts, the forecasts above the refill line).
Background layers (the area under the room line, the "now" rule) only show in
cells no line passes through.

Colours come from rich, which the views hand ``Text`` lines to; whether any
colour is emitted is decided once, by :func:`printer.colors_enabled` (so
``NO_COLOR``, ``FORCE_COLOR``, a pipe and ``TERM=dumb`` behave exactly as for
the rest of cswap). The mid-tone palette reads on dark and light backgrounds
alike.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo

from rich.text import Text

from claude_swap.history import iso_utc
from claude_swap.history_forecast import (
    DAY_S,
    FORECAST_AHEAD_S,
    HOUR_S,
    Analysis,
    Forecast,
    extra_accounts,
    path_at,
    room_series,
)
from claude_swap.json_output import SCHEMA_VERSION

# -- palette ------------------------------------------------------------------

ROOM = "#3987e5"
ROOM_FILL = "#1d4f8f"
PACE_STYLES = {"30m": "#e0a030", "6h": "#c264d0", "24h": "#2fb5a5"}
IDLE = "#898781"
AXIS = "#6e6d68"
ACTIVE = "#d08a5c"
MUTED = "#898781"

# Marker per series in ASCII mode, where colour may be absent too.
ASCII_MARKS = {"room": "*", "30m": "x", "6h": "o", "24h": "+", "idle": "."}

STRATEGY_NOTES = {
    "best": "Assumes cswap auto keeps its best strategy: use stays on one account "
            "until its week is used up, then moves to the one with the most room.",
    "consume-first": "Assumes cswap auto keeps its consume-first strategy: use goes "
                     "to the account whose week resets soonest.",
    "priority-consume-first": "Assumes cswap auto keeps its priority-consume-first "
                              "strategy: use goes to the highest priority tier with "
                              "room, soonest reset first.",
}


# -- formatting -----------------------------------------------------------------


def worth(v: float | None) -> str:
    """Accounts' worth: one decimal, two below 1 so a small amount still shows."""
    if v is None:
        return "-"
    return f"{v:.1f}" if v == 0 or v >= 1 else f"{v:.2f}"


def pct(v: float | None) -> str:
    return "-" if v is None else f"{round(v):d}%"


def dur(s: float) -> str:
    s = max(0, round(s))
    d, h, m = s // 86400, s % 86400 // 3600, s % 3600 // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def _local(t: float, tz: tzinfo | None) -> datetime:
    return datetime.fromtimestamp(t, tz) if tz is not None else datetime.fromtimestamp(t).astimezone()


def _clock(dt: datetime) -> str:
    hour = dt.hour % 12 or 12
    return f"{hour}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


def when(t: float, tz: tzinfo | None) -> str:
    """``Tue Nov 3, 4:10 PM`` -- built by hand: ``%-d`` is not portable."""
    dt = _local(t, tz)
    return f"{dt:%a %b} {dt.day}, {_clock(dt)}"


def day_label(t: float, tz: tzinfo | None) -> str:
    dt = _local(t, tz)
    return f"{dt:%b} {dt.day}"


def list_accounts(numbers: Sequence[int]) -> str:
    tags = [f"#{n}" for n in numbers]
    if len(tags) <= 1:
        return "".join(tags)
    if len(tags) <= 4:
        return ", ".join(tags[:-1]) + " and " + tags[-1]
    return ", ".join(tags[:3]) + f" and {len(tags) - 3} more"


def _midnight(d: date, tz: tzinfo | None) -> float:
    # Naive local midnight goes through mktime, which knows the local DST
    # rules for that date; an explicit zone is attached directly.
    if tz is None:
        return datetime(d.year, d.month, d.day).timestamp()
    return datetime(d.year, d.month, d.day, tzinfo=tz).timestamp()


def time_ticks(t0: float, t1: float, max_ticks: int, tz: tzinfo | None) -> list[tuple[float, str]]:
    """Round local times between ``t0`` and ``t1`` to label an axis with."""
    span = t1 - t0
    hour_steps = [1, 2, 3, 6, 12]
    day_steps = [1, 2, 7, 14, 28]
    max_ticks = max(2, max_ticks)
    out: list[tuple[float, str]] = []
    for h in hour_steps:
        if span / (h * HOUR_S) <= max_ticks:
            d = _local(t0, tz).date()
            while True:
                base = _midnight(d, tz)
                if base > t1:
                    break
                for k in range(0, 24, h):
                    t = base + k * HOUR_S
                    if t0 <= t <= t1:
                        lt = _local(t, tz)
                        label = day_label(t, tz) if lt.hour == 0 else (
                            f"{lt.hour % 12 or 12}{'am' if lt.hour < 12 else 'pm'}")
                        out.append((t, label))
                d += timedelta(days=1)
            return out
    for step in day_steps:
        if span / (step * DAY_S) <= max_ticks or step == day_steps[-1]:
            d = _local(t0, tz).date()
            if step >= 7:
                d -= timedelta(days=d.weekday())  # weeks start on Monday
            while True:
                t = _midnight(d, tz)
                if t > t1:
                    break
                if t >= t0:
                    out.append((t, day_label(t, tz)))
                d += timedelta(days=step)
            return out
    return out


def _wrap(text: str, width: int, indent: str = "") -> list[str]:
    words, lines, cur = text.split(), [], indent
    for w in words:
        if len(cur) + len(w) + (0 if cur == indent else 1) > width and cur != indent:
            lines.append(cur)
            cur = indent + w
        else:
            cur = cur + ("" if cur == indent else " ") + w
    if cur.strip():
        lines.append(cur)
    return lines


# -- the canvas -------------------------------------------------------------------

# Braille dot bit for (row 0-3, column 0-1) inside a cell.
_BRAILLE = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))


@dataclass(frozen=True)
class Layer:
    key: str
    style: str
    priority: int
    pattern: tuple[int, int] | None = None  # (dots on, dots off) along the line
    background: bool = False
    mark: str = "*"


class Canvas:
    """A grid of terminal cells drawn at dot resolution."""

    def __init__(self, cols: int, rows: int, unicode: bool):
        self.cols, self.rows, self.unicode = cols, rows, unicode
        self.xres, self.yres = (2, 4) if unicode else (1, 1)
        self.w, self.h = cols * self.xres, rows * self.yres
        self._fg = [[0] * cols for _ in range(rows)]
        self._bg = [[0] * cols for _ in range(rows)]
        self._fg_layer: list[list[Layer | None]] = [[None] * cols for _ in range(rows)]
        self._bg_layer: list[list[Layer | None]] = [[None] * cols for _ in range(rows)]
        self._steps: dict[str, int] = {}

    def dot(self, x: int, y: int, layer: Layer) -> None:
        if not (0 <= x < self.w and 0 <= y < self.h):
            return
        c, r = x // self.xres, y // self.yres
        bit = _BRAILLE[y % 4][x % 2] if self.unicode else 1
        bits, owners = (self._bg, self._bg_layer) if layer.background else (self._fg, self._fg_layer)
        bits[r][c] |= bit
        cur = owners[r][c]
        if cur is None or layer.priority >= cur.priority:
            owners[r][c] = layer

    def line(self, x0: int, y0: int, x1: int, y1: int, layer: Layer) -> None:
        """Bresenham from (x0, y0) to (x1, y1). A dash pattern counts dots
        along the whole series, so it runs on unbroken across segments."""
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
        err = dx + dy
        step = self._steps.get(layer.key, 0)
        on, off = layer.pattern if layer.pattern and self.unicode else (1, 0)
        first = True
        while True:
            if not first or step == 0:
                if off == 0 or step % (on + off) < on:
                    self.dot(x0, y0, layer)
                step += 1
            first = False
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy
        self._steps[layer.key] = step

    def polyline(self, points: Sequence[tuple[float, float] | None], layer: Layer) -> None:
        """Join dot-space points; None breaks the line."""
        prev = None
        for p in points:
            if p is None:
                prev = None
                continue
            q = (round(p[0]), round(p[1]))
            if prev is None:
                self.dot(q[0], q[1], layer)
                self._steps[layer.key] = self._steps.get(layer.key, 0) + 1
            else:
                self.line(prev[0], prev[1], q[0], q[1], layer)
            prev = q

    def fill_below(self, points: Sequence[tuple[float, float] | None], layer: Layer, density: int = 2) -> None:
        """Shade from each point down to the bottom (every ``density``-th dot
        row, staggered, for a light texture)."""
        for p in points:
            if p is None:
                continue
            x, top = round(p[0]), round(p[1])
            for y in range(max(0, top + 1), self.h):
                if (x + y) % density == 0:
                    self.dot(x, y, layer)

    def text_rows(self) -> list[Text]:
        out = []
        for r in range(self.rows):
            line = Text()
            for c in range(self.cols):
                fg, layer = self._fg[r][c], self._fg_layer[r][c]
                if not fg:
                    fg, layer = self._bg[r][c], self._bg_layer[r][c]
                if not fg or layer is None:
                    line.append(" ")
                elif self.unicode:
                    line.append(chr(0x2800 + fg), style=layer.style)
                else:
                    line.append(layer.mark, style=layer.style)
            out.append(line)
        return out


@dataclass(frozen=True)
class Glyphs:
    unicode: bool
    # Lines drawn with per-series markers rather than braille: without colour,
    # braille lines all look alike, and only a marker tells them apart.
    marks: bool = False

    @property
    def vbar(self) -> str:
        return "│" if self.unicode else "|"

    @property
    def tick_y(self) -> str:
        return "┤" if self.unicode else "+"

    @property
    def corner(self) -> str:
        return "└" if self.unicode else "+"

    @property
    def hbar(self) -> str:
        return "─" if self.unicode else "-"

    @property
    def tick_x(self) -> str:
        return "┴" if self.unicode else "+"

    @property
    def now_tick(self) -> str:
        return "┼" if self.unicode else "+"

    @property
    def full(self) -> str:
        return "█" if self.unicode else "#"

    @property
    def empty(self) -> str:
        return "░" if self.unicode else "."

    @property
    def arrow(self) -> str:
        return "→" if self.unicode else "->"

    def key(self, kind: str, series: str) -> str:
        """A two-cell legend key for a line style."""
        if self.marks or not self.unicode:
            mark = ASCII_MARKS.get(series, "*")
            return mark * 2
        return {"solid": "──", "dash": "╌╌", "dot": "┈┈"}[kind]


# -- chart frame ------------------------------------------------------------------


@dataclass
class Frame:
    """Maps data to dots and lays out the gutters around a canvas."""

    canvas: Canvas
    t0: float
    t1: float
    top: float  # the value at the top edge
    left: int  # gutter width, axis included

    def x(self, t: float) -> float:
        return (t - self.t0) / (self.t1 - self.t0) * (self.canvas.w - 1)

    def y(self, v: float) -> float:
        return (1 - v / self.top) * (self.canvas.h - 1)

    def col(self, t: float) -> int:
        return int(self.x(t)) // self.canvas.xres

    def row(self, v: float) -> int:
        return min(self.canvas.rows - 1, max(0, int(round(self.y(v))) // self.canvas.yres))


def _frame_rows(
    frame: Frame,
    g: Glyphs,
    y_ticks: Sequence[tuple[float, str]],
    right: dict[int, Text],
    right_w: int,
) -> list[Text]:
    """The canvas rows with the y axis on the left and labels on the right."""
    tick_rows = {frame.row(v): label for v, label in y_ticks}
    out = []
    for r, body in enumerate(frame.canvas.text_rows()):
        label = tick_rows.get(r)
        line = Text()
        if label is not None:
            line.append(label.rjust(frame.left - 1), style=MUTED)
            line.append(g.tick_y, style=AXIS)
        else:
            line.append(" " * (frame.left - 1))
            line.append(g.vbar, style=AXIS)
        line.append_text(body)
        if right_w:
            line.append(" ")
            line.append_text(right.get(r, Text("")))
        out.append(line)
    return out


def _x_axis(frame: Frame, g: Glyphs, ticks: Sequence[tuple[float, str]], now_col: int | None) -> list[Text]:
    cols = frame.canvas.cols
    axis = [g.hbar] * cols
    labels = [" "] * (cols + 8)
    taken: list[tuple[int, int]] = []
    for t, label in ticks:
        c = frame.col(t)
        if not 0 <= c < cols:
            continue
        start = c - len(label) // 2
        start = max(0, min(start, cols - len(label)))
        if any(start < b + 2 and a < start + len(label) + 2 for a, b in taken):
            continue
        axis[c] = g.tick_x
        labels[start : start + len(label)] = list(label)
        taken.append((start, start + len(label)))
    if now_col is not None and 0 <= now_col < cols:
        axis[now_col] = g.now_tick
    line = Text(" " * (frame.left - 1))
    line.append(g.corner + "".join(axis), style=AXIS)
    lab = Text(" " * frame.left)
    lab.append("".join(labels).rstrip(), style=MUTED)
    return [line, lab]


def _room_ticks(scale: float) -> list[tuple[float, str]]:
    step = next((s for s in (1, 2, 5, 10, 20, 50, 100) if scale / s <= 4), 200)
    ticks = []
    v = 0.0
    while v < scale - step * 0.4:
        ticks.append((v, f"{v:g}"))
        v += step
    ticks.append((scale, f"{scale:g}"))
    return ticks


# -- the overview -------------------------------------------------------------------


# Narrowest terminal the view is laid out for; below NARROW_W the pace table
# and the account list fold onto more lines.
MIN_WIDTH = 50
NARROW_W = 78


def chart_size(width: int) -> tuple[int, int]:
    """(width, chart rows) for a terminal of ``width`` columns."""
    width = max(MIN_WIDTH, min(width, 200))
    return width, max(8, min(16, width // 7))


def reason_text(f: Forecast, tz: tzinfo | None) -> str:
    if f.reason == "no-room-now":
        return "No current readings to start from"
    if f.reason == "no-active":
        return "No reading of the active account yet"
    if f.reason == "stale-active" and f.since is not None:
        return f"No reading of the active account since {when(f.since, tz)}"
    if f.reason == "warming-up" and f.needs is not None:
        return f"After {dur(f.needs)} more of history"
    return "Not available"


def outlook(f: Forecast, a: Analysis, tz: tzinfo | None) -> str:
    """What happens at this pace over the next 7 days, in words."""
    if not f.ok or f.sim is None:
        return reason_text(f, tz)
    sim, now = f.sim, a.now
    if sim.out is not None:
        return "Out of room now" if sim.out <= now + 60 else f"Runs out {when(sim.out, tz)}"
    low_t, low_v = sim.low
    if low_t >= a.forecast_end - 60:
        return f"Down to {worth(low_v)} by {when(low_t, tz)}"
    if low_t > now + 60 and worth(low_v) != worth(sim.path[0][1]):
        return f"Lowest {worth(low_v)}, {when(low_t, tz)}"
    return "Never lower than now"


def render_overview(
    a: Analysis,
    *,
    days: float,
    width: int,
    unicode: bool,
    tz: tzinfo | None = None,
    color: bool = True,
    settings_note: str | None = None,
) -> list[Text]:
    """The weekly-room chart, the pace table and each account's room now.

    ``color`` only decides whether the area under the room line is shaded:
    without colour the shading would be as loud as the lines.
    """
    # Braille only with colour: in one colour three dashed braille lines are
    # indistinguishable, so without it the chart draws markers (x, o, +).
    chart_unicode = unicode and color
    g = Glyphs(unicode, marks=not chart_unicode)
    width, rows = chart_size(width)
    now = a.now
    out: list[Text] = []
    title = Text("Weekly room left", style="bold")
    span = Text(f"last {_days_words(days)} {g.arrow} next 7 days", style=MUTED)
    title.append(" " * max(2, width - len(title) - len(span)))
    title.append_text(span)
    out.append(title)

    room = a.room_now.room
    if a.history.demo:
        out.append(Text("Made-up demo data: 15 accounts over four weeks.", style=f"italic {MUTED}"))
    fig = Text()
    if room is None:
        fig.append("No current reading to total", style="bold")
    else:
        fig.append(worth(room), style=f"bold {ROOM}")
        n = a.fleet_size
        owner = "account's" if n == 1 else "accounts'"
        fig.append(f" of {n} {owner} worth left")
    out.append(fig)
    notes = []
    if room is not None:
        soon = [e for e in a.resets if e.t <= now + DAY_S]
        if soon:
            notes.append(
                f"{worth(sum(e.frees for e in soon))} more comes back in the next 24 hours as "
                f"{list_accounts([e.number for e in soon])} reset{'s' if len(soon) == 1 else ''}"
            )
        else:
            notes.append("No weekly limit resets in the next 24 hours")
    missing = a.fleet_size - a.room_now.counted
    if missing > 0 and a.fleet_size:
        notes.append(
            f"{missing} account{' has' if missing == 1 else 's have'} no recent reading, "
            f"so {'it is' if missing == 1 else 'they are'}n't counted"
        )
    if notes:
        for line in _wrap(". ".join(notes) + ".", width):
            out.append(Text(line, style=MUTED))
    out.append(Text())

    # -- the chart --
    fcs = [f for f in a.forecasts if f.ok and f.sim is not None]
    right_w = 9
    scale = a.scale
    y_ticks = _room_ticks(scale)
    left = max(len(lab) for _, lab in y_ticks) + 2
    cols = max(20, width - left - right_w - 1)
    canvas = Canvas(cols, rows, chart_unicode)
    t0, t1 = now - days * DAY_S, now + FORECAST_AHEAD_S
    frame = Frame(canvas, t0, t1, scale, left)
    xn = frame.x(now)

    now_layer = Layer("now", AXIS, 0, pattern=(1, 1), background=True, mark=":")
    canvas.line(round(xn), 0, round(xn), canvas.h - 1, now_layer)

    # recorded room, sampled once per dot column
    samples = room_series(a.history, a.fleet, t0, now, max(2, int(xn) + 1))
    if room is not None:
        samples[-1] = (now, room)  # end exactly on the headline figure
    pts = [(frame.x(t), frame.y(v)) if v is not None else None for t, v in samples]
    room_layer = Layer("room", ROOM, 5, mark=ASCII_MARKS["room"])
    if chart_unicode:
        canvas.fill_below(pts, Layer("fill", ROOM_FILL, 1, background=True))
    # refill: steps up at each reset (drawn first: forecasts sit on top)
    right: dict[int, Text] = {}
    labels: list[tuple[float, Text]] = []
    if room is not None:
        idle_pts: list[tuple[float, float] | None] = [(xn, frame.y(room))]
        v = room
        for e in a.resets:
            v = min(scale, v + e.frees)
            idle_pts.append((frame.x(e.t), idle_pts[-1][1]))
            idle_pts.append((frame.x(e.t), frame.y(v)))
        idle_pts.append((frame.x(t1), frame.y(v)))
        canvas.polyline(idle_pts, Layer("idle", IDLE, 2, pattern=(1, 2), mark=ASCII_MARKS["idle"]))
        if not fcs:
            labels.append((v, Text(worth(v), style=IDLE)))
    for prio, f in enumerate(fcs):
        assert f.sim is not None
        path = [p for p in f.sim.path if p[0] <= t1] + [(t1, path_at(f.sim.path, t1))]
        style = PACE_STYLES[f.pace.key]
        canvas.polyline(
            [(frame.x(t), frame.y(v)) for t, v in path],
            Layer(f.pace.key, style, 3 + prio, pattern=(6, 2), mark=ASCII_MARKS[f.pace.key]),
        )
        end = path_at(f.sim.path, t1)
        labels.append((end, Text(f"{f.pace.key} ", style=style).append(worth(end), style=f"bold {style}")))
    canvas.polyline(pts, room_layer)

    # right-edge labels, nudged onto distinct rows
    placed = sorted(((frame.row(v), lab) for v, lab in labels), key=lambda p: p[0])
    for i in range(1, len(placed)):
        if placed[i][0] <= placed[i - 1][0]:
            placed[i] = (placed[i - 1][0] + 1, placed[i][1])
    overflow = (placed[-1][0] - (rows - 1)) if placed else 0
    if overflow > 0:
        placed = [(r - overflow, lab) for r, lab in placed]
    right = {r: lab for r, lab in placed}

    now_col = frame.col(now)
    head = Text(" " * left)
    if now_col >= 3:
        marker = Text(" " * (now_col - 3)).append("now", style="bold")
    else:
        marker = Text("")
    if cols - now_col > 10:
        marker.append(" " * (now_col + 2 - len(marker)))
        marker.append(f"ahead {g.arrow}", style=MUTED)
    head.append_text(marker)
    out.append(head)
    out.extend(_frame_rows(frame, g, y_ticks, right, right_w))
    ticks = time_ticks(t0, t1, max(3, cols // 9), tz)
    out.extend(_x_axis(frame, g, ticks, now_col))

    items = [Text(g.key("solid", "room"), style=ROOM) + Text(" room left")]
    paces = Text()
    for f in a.forecasts:
        paces.append(g.key("dash", f.pace.key), style=PACE_STYLES[f.pace.key] if f.ok else MUTED)
        paces.append(f" {f.pace.key}  ", style=None if f.ok else MUTED)
    items.append(paces + Text("pace"))
    items.append(Text(g.key("dot", "idle"), style=IDLE) + Text(" if no more use"))
    out.extend(_pack(items, width, "  ", "   "))
    out.append(Text())

    # -- pace table --
    out.extend(_pace_table(a, g, width, tz))
    out.append(Text())
    out.extend(_accounts_now(a, g, width, tz))
    if settings_note:
        out.append(Text())
        out.extend(Text(line, style=MUTED) for line in _wrap(settings_note, width))
    return out


def _pack(items: Sequence[Text], width: int, indent: str, sep: str) -> list[Text]:
    """Lay ``items`` out left to right, starting a new line where the next
    one would pass ``width``."""
    lines: list[Text] = []
    cur = Text(indent)
    for item in items:
        if len(cur) > len(indent) and len(cur) + len(sep) + len(item) > width:
            lines.append(cur)
            cur = Text(indent)
        if len(cur) > len(indent):
            cur.append(sep)
        cur.append_text(item)
    lines.append(cur)
    return lines


def _days_words(days: float) -> str:
    if days == 1:
        return "24 hours"
    if days < 1:
        return f"{dur(days * DAY_S)}"
    return f"{days:g} days"


def more_accounts_words(n: int | None) -> str:
    """The pace table's "More accounts" cell."""
    if n is None:
        return "-"
    if n == 0:
        return "0 (sustainable)"
    return f"+{n} account{'s' if n != 1 else ''}"


def _pace_table(a: Analysis, g: Glyphs, width: int, tz: tzinfo | None) -> list[Text]:
    # Wide: one row a pace, the outlook in its own column. Narrow (under
    # NARROW_W): the outlook goes on the line under its pace.
    narrow = width < NARROW_W
    name_w, num_w, more_w, gap = (19 if narrow else 27), 5, 17, "  "
    head = f"  {'Pace':<{name_w}}{'A day':>{num_w}}{'More accounts':>{more_w}}"
    out = [Text(head if narrow else head + gap + "Next 7 days at that pace", style="bold")]
    k = a.room_now.counted

    def tail(line: Text, text: str, style: str | None) -> list[Text]:
        if narrow:
            return [line] + [Text(" " * 6 + c, style=style) for c in _wrap(text, width - 6)]
        line.append(gap)
        return _hang(line, text, width, style)

    for f in a.forecasts:
        line = Text("  ")
        style = PACE_STYLES[f.pace.key] if f.ok else MUTED
        line.append(g.key("dash", f.pace.key)[0], style=style)
        line.append(f" {f.pace.name:<{name_w - 2}}")
        line.append(f"{worth(f.per_day) if f.ok else '-':>{num_w}}", style="bold" if f.ok else MUTED)
        more = extra_accounts(f.per_day, k) if f.ok else None
        line.append(f"{more_accounts_words(more):>{more_w}}", style="bold" if more else MUTED)
        out.extend(tail(line, outlook(f, a, tz), None if f.ok else MUTED))
    sus = a.sustainable_per_day
    if sus is not None:
        name = f"Sustainable ({k})" if narrow else f"Sustainable ({k} account{'s' if k != 1 else ''})"
        line = Text(f"    {name:<{name_w - 2}}")
        line.append(f"{worth(sus):>{num_w}}", style="bold")
        line.append(f"{'0':>{more_w}}", style=MUTED)
        out.extend(tail(line, "Faster than this runs out sooner or later", MUTED))
    out.extend(Text(s, style=MUTED) for s in _wrap(
        "A day = accounts' worth of weekly limit used a day. More accounts = how many "
        "more than are counted now it would take to keep up with that pace for good "
        "(pace x 7 days, less the accounts counted).", width, "  "))
    for line in _wrap(STRATEGY_NOTES.get(a.history.strategy, STRATEGY_NOTES["best"]), width, "  "):
        out.append(Text(line, style=MUTED))
    return out


def _hang(head: Text, text: str, width: int, style: str | None) -> list[Text]:
    """``head`` followed by ``text``, wrapped under its own first column."""
    indent = len(head)
    chunks = _wrap(text, max(12, width - indent)) or [""]
    first = head.copy()
    first.append(chunks[0], style=style)
    return [first] + [Text(" " * indent + c, style=style) for c in chunks[1:]]


def _accounts_now(a: Analysis, g: Glyphs, width: int, tz: tzinfo | None) -> list[Text]:
    rows = a.accounts_now
    if not rows:
        return []
    out = [Text("Room on each account now", style="bold") + Text("  (7-day limit used)", style=MUTED)]
    num_w = max(len(f"#{r.number}") for r in rows)
    narrow = width < 64
    now = a.now
    # The tail after the bar: "  5h  41%  22h 40m", or "   5h  41%   resets in
    # 22h 40m" when wide, sized to the longest reset label actually shown.
    reset_w = max(
        (len(dur(r.d7_reset - now)) for r in rows
         if r.d7 is not None and r.d7_reset is not None and r.d7_reset > now),
        default=0,
    )
    tail_w = (9 + (2 + reset_w if reset_w else 0)) if narrow else (10 + (13 + reset_w if reset_w else 0))
    bar_w = max(6, min(30, width - (2 + num_w + 10) - 5 - tail_w))
    for r in rows:
        line = Text("  ")
        line.append(f"#{r.number}".ljust(num_w), style="bold")
        tag = "active" if r.active else ("disabled" if r.disabled else "")
        line.append(f" {tag:<8} ", style=ACTIVE if r.active else MUTED)
        if r.d7 is None:
            if not r.weekly:
                why = "no weekly limit"
            elif r.fetched_at is None:
                why = "no reading yet"
            elif r.stale_reset:
                why = f"last reading {dur(now - r.fetched_at)} ago, from before its weekly reset"
            else:
                why = f"no reading for {dur(now - r.fetched_at)}"
            if r.error:
                why += f" (last check: {r.error})"
            room_left = width - len(line)
            if len(why) > room_left:
                why = why[: max(0, room_left - 3)].rstrip() + "..."
            line.append(why, style=MUTED)
            out.append(line)
            continue
        filled = round(min(100.0, max(0.0, r.d7)) / 100 * bar_w)
        line.append(g.full * filled, style=ROOM)
        line.append(g.empty * (bar_w - filled), style=AXIS)
        line.append(f" {pct(r.d7):>4}")
        sp = "  " if narrow else "   "
        line.append(f"{sp}5h {pct(r.h5):>4}", style=MUTED)
        if r.d7_reset is not None and r.d7_reset > now:
            line.append(f"{sp}{'' if narrow else 'resets in '}{dur(r.d7_reset - now)}", style=MUTED)
        if not r.counted and len(line) + 14 <= width:
            line.append(f"{sp}not counted", style=MUTED)
        out.append(line)
    return out


# -- JSON -----------------------------------------------------------------------------


def _ts(t: float | None) -> str | None:
    return None if t is None or not math.isfinite(t) else iso_utc(t)


def _r(v: float | None, digits: int = 3) -> float | None:
    return None if v is None else round(v, digits)


def overview_json(a: Analysis, *, days: float) -> dict:
    now = a.now
    t0 = now - days * DAY_S
    n = max(2, min(500, int(days * 24 * 4) + 1))  # every 15 minutes, at most 500 points
    series = room_series(a.history, a.fleet, t0, now, n)
    if a.room_now.room is not None:
        series[-1] = (now, a.room_now.room)
    forecasts = []
    for f in a.forecasts:
        item: dict = {"pace": f.pace.key, "windowSeconds": int(f.pace.span), "available": f.ok}
        if f.ok and f.sim is not None:
            item.update({
                "usedPerDay": _r(f.per_day),
                "extraAccountsToKeepUp": extra_accounts(f.per_day, a.room_now.counted),
                "runsOutAt": _ts(f.sim.out),
                "low": {"at": _ts(f.sim.low[0]), "room": _r(f.sim.low[1])},
                "path": [{"at": _ts(t), "room": _r(v)} for t, v in f.sim.path],
            })
        else:
            item["reason"] = f.reason
            if f.since is not None:
                item["activeCheckedAt"] = _ts(f.since)
            if f.needs is not None:
                item["needsSeconds"] = int(f.needs)
        forecasts.append(item)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": _ts(now),
        "rangeDays": days,
        "demo": a.history.demo,
        "strategy": a.history.strategy,
        "accounts": a.fleet_size,
        "countedAccounts": a.room_now.counted,
        "roomNow": _r(a.room_now.room),
        "sustainablePerDay": _r(a.sustainable_per_day),
        "room": [{"at": _ts(t), "room": _r(v)} for t, v in series],
        "resets": [{"at": _ts(e.t), "account": e.number, "frees": _r(e.frees)} for e in a.resets],
        # The dotted line: today's room plus every reset by then, if no more use.
        "refill": [] if a.room_now.room is None else (
            [{"at": _ts(now), "room": _r(a.room_now.room)}]
            + [{"at": _ts(e.t), "room": _r(a.refill_at(e.t))} for e in a.resets]
        ),
        "forecasts": forecasts,
        "accountsNow": [
            {
                "number": r.number,
                "active": r.active,
                "disabled": r.disabled,
                "weeklyLimit": r.weekly,
                "counted": r.counted,
                "sevenDayPct": r.d7,
                "sevenDayResetsAt": _ts(r.d7_reset),
                "fiveHourPct": r.h5,
                "fiveHourResetsAt": _ts(r.h5_reset),
                "modelPct": r.model,
                "fetchedAt": _ts(r.fetched_at),
                "lastError": r.error,
            }
            for r in a.accounts_now
        ],
        "modelName": a.history.model_name,
        "hiddenRows": a.history.hidden_rows,
    }
