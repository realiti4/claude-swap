"""Usage-bar panel for the menu bar dropdown: the model and its geometry.

The menu bar can draw each account as a small native panel (a header line and
one bar per usage window) instead of a single line of text. This module is the
pure half of that: it turns a snapshot account row into a panel *model* and
does the layout arithmetic. It imports neither rumps nor AppKit, so it is
unit-tested on any platform; ``menubar_panel_view`` draws the model with
AppKit.

Every number comes from the helpers the text row already uses (the live reset
countdown, the weekly roll-forward, ``pace.compute_pace``), and the colour
bands are the TUI's own ``WARN_PCT``/``CRIT_PCT``, so the bars, the text row
and the terminal dashboard always agree.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from claude_swap import pace
from claude_swap.menubar import (
    _live_countdown,
    _rolled_weekly_window,
    account_identity,
    format_account_label,
)
from claude_swap.tui.data import format_duration
from claude_swap.tui.theme import CRIT_PCT, WARN_PCT

# ---- layout constants (points) --------------------------------------------------

PANEL_WIDTH = 340.0
LEFT_INSET = 14.0  # native menu text inset
RIGHT_INSET = 12.0
DOT_COLUMN = 12.0  # the active dot, then the slot number / row labels
DOT_DIAMETER = 6.0
BAR_HEIGHT = 6.0
TICK_WIDTH = 1.5
TICK_OVERHANG = 2.0  # the tick stands proud of the bar so it reads over the fill
LABEL_GAP = 8.0  # label column -> bar
PCT_GAP = 8.0  # bar (or note) -> percent
NOTE_GAP = 4.0  # note -> percent
RESET_GAP = 10.0  # percent -> reset countdown
NUM_GAP = 6.0  # slot number -> name
META_GAP = 10.0  # name -> the right-hand state/age text
TOP_PAD = 4.0
HEADER_GAP = 2.0
BOTTOM_PAD = 6.0

UNAVAILABLE = "usage unavailable"  # the text row's wording
OVER_MARKER = "(!)"  # the text row's at/over-limit marker

STATES = ("ok", "warn", "hot", "over", "unknown")


# ---- model --------------------------------------------------------------------------

@dataclass(frozen=True)
class WindowRow:
    """One usage window as a bar row.

    ``fraction`` is the bar fill, clamped to 0..1. ``pace_fraction`` is where
    the pace tick goes: the share of the weekly window that had elapsed when
    the usage was measured, or None when pace does not apply (the 5h window,
    no pace data, or a window that has rolled over since it was measured).
    ``ahead`` is the text row's "(ahead)" signal for the same window. An
    ``unknown`` row has no bar; its ``note`` carries the text to show instead.
    """

    label: str
    fraction: float
    pct_text: str
    reset_text: str
    state: str
    pace_fraction: float | None
    note: str
    ahead: bool = False


@dataclass(frozen=True)
class AccountPanel:
    """One account's block: header fields plus its window rows."""

    num: str
    title: str  # the text row's identity: "alias  (email)" or the email
    email: str
    is_active: bool
    disabled: bool
    meta_text: str  # "active · 2m ago", "disabled", ... ("" when nothing to say)
    rows: tuple[WindowRow, ...]
    text_label: str  # the plain text row, for accessibility and type-select
    alias: str | None = None  # kept so the accessibility label can name the email


def usage_state(pct: float | None) -> str:
    """Colour band for a percentage, using the TUI's thresholds."""
    if pct is None:
        return "unknown"
    if pct >= 100.0:
        return "over"
    if pct >= CRIT_PCT:
        return "hot"
    if pct >= WARN_PCT:
        return "warn"
    return "ok"


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))


def _numeric_pct(window) -> float | None:
    if isinstance(window, dict) and isinstance(window.get("pct"), (int, float)):
        return float(window["pct"])
    return None


def _row(label: str, window: dict, pct: float, now: float, pace_result=None) -> WindowRow:
    state = usage_state(pct)
    pace_fraction = None
    if pace_result is not None and pace_result.period_s > 0:
        pace_fraction = _clamp01(pace_result.elapsed_s / pace_result.period_s)
    return WindowRow(
        label=label,
        fraction=_clamp01(pct / 100.0),
        pct_text=f"{pct:.0f}%",
        reset_text=_live_countdown(window, now) or "",
        state=state,
        pace_fraction=pace_fraction,
        note=OVER_MARKER if state == "over" else "",
        ahead=bool(pace_result is not None and pace_result.ahead and state != "over"),
    )


def _weekly(window, now: float, fetched_at: float | None):
    """A weekly window rolled past a passed reset, and its pace (or None).

    A window that rolled over was measured in the cycle that has ended; its
    elapsed share belongs to that cycle, so it gets no pace at all.
    """
    rolled = _rolled_weekly_window(window, now)
    if rolled is not window:
        return rolled, None
    return rolled, pace.compute_pace(rolled, fetched_at=fetched_at)


def _unknown(note: str) -> WindowRow:
    return WindowRow("", 0.0, "", "", "unknown", None, note)


def window_rows(usage, now: float, fetched_at: float | None) -> list[WindowRow]:
    """Bar rows for one account's display usage, in the text row's order.

    5h, 7d, each named per-model window, then spend. A sentinel string (the
    text row's note for an expired token and the like) becomes one ``unknown``
    row with that wording; no usable window at all reads "usage unavailable".
    Weekly windows are rolled forward past a passed reset and paced against
    the rolled window, exactly as ``menubar.usage_summary`` does.
    """
    if isinstance(usage, str):
        return [_unknown(usage)]
    if not isinstance(usage, dict):
        return [_unknown(UNAVAILABLE)]
    rows: list[WindowRow] = []
    five = usage.get("five_hour")
    pct = _numeric_pct(five)
    if pct is not None:
        rows.append(_row("5h", five, pct, now, None))  # 5h never gets pace
    seven, seven_pace = _weekly(usage.get("seven_day"), now, fetched_at)
    pct = _numeric_pct(seven)
    if pct is not None:
        rows.append(_row("7d", seven, pct, now, seven_pace))
    for window in usage.get("scoped") or []:
        window, window_pace = _weekly(window, now, fetched_at)
        pct = _numeric_pct(window)
        if pct is not None and window.get("name"):
            rows.append(_row(str(window["name"]), window, pct, now, window_pace))
    spend = usage.get("spend")
    pct = _numeric_pct(spend)
    if pct is not None:
        rows.append(_row("$", spend, pct, now, None))
    return rows or [_unknown(UNAVAILABLE)]


def age_text(fetched_at: float | None, now: float) -> str | None:
    """How old the measurement is ("2m ago"), or None when unknown."""
    if fetched_at is None:
        return None
    return f"{format_duration(max(0.0, now - fetched_at))} ago"


def build_account_panel(entry: tuple, now: float, names_only: bool = False) -> AccountPanel:
    """Panel model for one snapshot account row.

    ``entry`` is ``(num, email, is_active, display_usage, last_good, alias,
    disabled, fetched_at)`` as ``menubar._adapt_snapshot`` builds it; the
    panel reads the same display usage the text row reads. ``names_only``
    is Settings → Show names only, applied exactly as the text row applies it.
    """
    # Fields past the eighth are for other readers; a wider row still builds.
    num, email, is_active, display, _last_good, alias, disabled, fetched_at, *_extra = entry
    meta = []
    if is_active:
        meta.append("active")
    if disabled:
        meta.append("disabled")
    age = age_text(fetched_at, now)
    if age:
        meta.append(age)
    return AccountPanel(
        num=num,
        title=account_identity(email, alias, names_only=names_only),
        email=email,
        is_active=bool(is_active),
        disabled=bool(disabled),
        meta_text=" · ".join(meta),
        rows=tuple(window_rows(display, now, fetched_at)),
        text_label=format_account_label(
            num, email, display, now, alias=alias, disabled=disabled,
            fetched_at=fetched_at, names_only=names_only,
        ),
        alias=alias,
    )


# ---- geometry (flipped coordinates: y grows downward from the panel top) -------------

@dataclass(frozen=True)
class Columns:
    """Horizontal positions shared by every bar row in the menu."""

    label_x: float
    label_w: float
    bar_x: float
    bar_w: float
    note_right: float | None  # right edge of the "(!)" note, when reserved
    pct_right: float  # percent text is right-aligned to this
    reset_right: float  # reset countdown is right-aligned to this


def line_height(ascender: float, descender: float, leading: float) -> float:
    """Whole-point line height from a font's metrics (descender is negative)."""
    return float(math.ceil(ascender - descender + leading))


def row_columns(
    label_w: float,
    pct_w: float,
    reset_w: float,
    note_w: float = 0.0,
    width: float = PANEL_WIDTH,
) -> Columns:
    """Column layout: label, bar, optional note, percent, reset countdown.

    Widths are measured by the caller from the real fonts (the widest label in
    the menu, "100%" in monospaced digits, and so on), so every row in every
    account lines up. A note column is reserved only when some row needs it.
    """
    label_x = LEFT_INSET + DOT_COLUMN
    reset_right = width - RIGHT_INSET
    pct_right = reset_right - reset_w - RESET_GAP
    if note_w > 0:
        note_right = pct_right - pct_w - NOTE_GAP
        bar_right = note_right - note_w - PCT_GAP
    else:
        note_right = None
        bar_right = pct_right - pct_w - PCT_GAP
    bar_x = label_x + label_w + LABEL_GAP
    return Columns(
        label_x=label_x,
        label_w=label_w,
        bar_x=bar_x,
        bar_w=max(0.0, bar_right - bar_x),
        note_right=note_right,
        pct_right=pct_right,
        reset_right=reset_right,
    )


def bar_rect(cols: Columns, row_top: float, row_height: float) -> tuple[float, float, float, float]:
    """(x, y, w, h) of a row's bar track, vertically centred in the row."""
    return (cols.bar_x, row_top + (row_height - BAR_HEIGHT) / 2, cols.bar_w, BAR_HEIGHT)


def fill_width(bar_w: float, fraction: float) -> float:
    """Width of the filled part. Any usage shows at least one round cap."""
    fraction = _clamp01(fraction)
    if fraction <= 0.0:
        return 0.0
    return min(bar_w, max(BAR_HEIGHT, fraction * bar_w))


def tick_x(cols: Columns, pace_fraction: float) -> float:
    """Left edge of the pace tick, centred on the elapsed fraction, kept in the bar."""
    centre = cols.bar_x + _clamp01(pace_fraction) * cols.bar_w
    left = centre - TICK_WIDTH / 2
    return min(max(left, cols.bar_x), cols.bar_x + cols.bar_w - TICK_WIDTH)


def tick_rect(cols: Columns, pace_fraction: float, bar_y: float) -> tuple[float, float, float, float]:
    """(x, y, w, h) of the pace tick for a bar whose top is ``bar_y``."""
    return (
        tick_x(cols, pace_fraction),
        bar_y - TICK_OVERHANG,
        TICK_WIDTH,
        BAR_HEIGHT + 2 * TICK_OVERHANG,
    )


def header_name_span(cols: Columns, num_w: float, meta_w: float) -> tuple[float, float]:
    """(x, width) for the header's name: after the number, before the meta text.

    The number is drawn on its own and always fits; the name gets whatever is
    left and is truncated in the middle by the view when it is longer.
    """
    name_x = cols.label_x + num_w + NUM_GAP
    right = cols.reset_right - (meta_w + META_GAP if meta_w > 0 else 0.0)
    return name_x, max(0.0, right - name_x)


def panel_height(n_rows: int, header_h: float, row_h: float) -> float:
    """Total height of one account block, in whole points."""
    return float(math.ceil(TOP_PAD + header_h + HEADER_GAP + n_rows * row_h + BOTTOM_PAD))


# ---- accessibility ---------------------------------------------------------------

_UNITS = {"d": "day", "h": "hour", "m": "minute", "s": "second"}
_SPOKEN_LABELS = {"5h": "5 hour", "7d": "7 day", "$": "Spend"}


def spoken_duration(text: str) -> str:
    """"4d 22h" -> "4 days 22 hours"; zero parts are dropped ("4d 0h" -> "4 days")."""
    parts = []
    for token in text.split():
        value, unit = token[:-1], _UNITS.get(token[-1:])
        if not value.isdigit() or unit is None:
            return text
        if int(value) == 0 and parts:
            continue
        parts.append(f"{int(value)} {unit}{'' if int(value) == 1 else 's'}")
    return " ".join(parts) or text


def _spoken_meta(meta_text: str) -> str:
    out = []
    for part in meta_text.split(" · "):
        if part.endswith(" ago"):
            part = f"{spoken_duration(part[: -len(' ago')])} ago"
        out.append(part)
    return ", ".join(out)


def accessibility_label(panel: AccountPanel) -> str:
    """What VoiceOver reads for a panel: who, their state, then every window."""
    # Always the full identity, whatever the display setting: a listener
    # has no other way to tell which email an alias belongs to.
    head = f"Account {panel.num}, {account_identity(panel.email, panel.alias)}"
    if panel.meta_text:
        head += f", {_spoken_meta(panel.meta_text)}"
    sentences = [head]
    for row in panel.rows:
        if row.state == "unknown":
            sentences.append(row.note)
            continue
        parts = [_SPOKEN_LABELS.get(row.label, row.label), f"{row.pct_text.rstrip('%')} percent"]
        if row.state == "over":
            parts.append("over limit")
        elif row.ahead:
            parts.append("ahead of pace")
        if row.reset_text:
            parts.append(f"resets in {spoken_duration(row.reset_text)}")
        sentences.append(", ".join(parts))
    return ". ".join(sentences) + "."
