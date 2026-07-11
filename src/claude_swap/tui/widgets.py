"""Shared render widgets: usage bars, account cards, and the accounts panel.

``bar_cells``/``usage_bar`` are custom renderers rather than Textual's
``ProgressBar`` because the design needs three things the stock widget
doesn't do: a severity color ramp, an optional threshold tick mark (the
auto-switch trigger line), and stale-measurement dimming.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from rich.text import Text
from textual.widgets import ListItem, Static

from claude_swap.json_output import USAGE_API_KEY
from claude_swap.models import AccountSnapshot
from claude_swap.usage_store import STALE_OK_S
from claude_swap.tui import data
from claude_swap.tui.theme import current_theme_colors, severity_color

if TYPE_CHECKING:
    from claude_swap.tui.app import CswapApp

_BAR_FILLED = "━"
_BAR_HALF = "╸"
_BAR_EMPTY = "─"
_BAR_TICK = "┃"


def bar_cells(
    pct: float | None,
    width: int,
    *,
    stale: bool = False,
    threshold: float | None = None,
) -> Text:
    """Just the bar glyphs: severity-colored fill, track, optional tick."""
    colors = current_theme_colors()
    text = Text()
    if pct is None:
        text.append(_BAR_EMPTY * width, style=colors.track)
        return text
    frac = min(max(pct, 0.0), 100.0) / 100.0
    cells = frac * width
    full = int(cells)
    half = (cells - full) >= 0.5 and full < width
    tick_at: int | None = None
    if threshold is not None:
        tick_at = min(width - 1, max(0, round(threshold / 100.0 * width)))
    color = severity_color(pct)
    fill_style = f"{color} dim" if stale else color
    for i in range(width):
        if tick_at is not None and i == tick_at:
            text.append(_BAR_TICK, style=colors.sev_warn)
        elif i < full:
            text.append(_BAR_FILLED, style=fill_style)
        elif i == full and half:
            text.append(_BAR_HALF, style=fill_style)
        else:
            text.append(_BAR_EMPTY, style=colors.track)
    return text


def usage_bar(
    label: str,
    pct: float | None,
    suffix: str | None,
    width: int,
    *,
    stale: bool = False,
    threshold: float | None = None,
) -> Text:
    """One full bar line: ``5h ━━━━╸────┃──  47%  resets 2h 13m``."""
    colors = current_theme_colors()
    text = Text()
    text.append(f"{label} ", style=colors.muted)
    text.append(bar_cells(pct, width, stale=stale, threshold=threshold))
    if pct is None:
        text.append("  usage unknown", style=colors.muted)
    else:
        color = severity_color(pct)
        text.append(f" {pct:3.0f}%", style=f"{color} dim" if stale else color)
    if suffix:
        text.append(f"  {suffix}", style=colors.muted)
    return text


def usage_rows(last_good: dict | None, now: float) -> list[tuple[str, float, str]]:
    """(label, pct, suffix) rows mirroring the CLI's ``_format_usage_lines``.

    Only windows the account actually has produce a row — an annual plan
    without a 7-day window simply has no 7d line. Order matches the CLI:
    spend, 5h, 7d, then per-model scoped windows (e.g. "Fable"), the latter
    marked ``(!)`` at/over their limit.
    """
    if not isinstance(last_good, dict):
        return []
    rows: list[tuple[str, float, str]] = []
    spend = last_good.get("spend")
    if spend:
        suffix_parts = [f"${spend['used']:,.2f} / ${spend['limit']:,.2f}"]
        reset = data.reset_text(spend, now)
        if reset:
            suffix_parts.insert(0, reset)
        rows.append(("$$", float(spend["pct"]), "  ".join(suffix_parts)))
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        window = last_good.get(key)
        if window:
            rows.append((label, float(window["pct"]), data.reset_text(window, now) or ""))
    for window in last_good.get("scoped") or []:
        pct = float(window["pct"])
        suffix = data.reset_text(window, now) or ""
        if pct >= 100:
            suffix = f"{suffix}  (!)" if suffix else "(!)"
        rows.append((window["name"], pct, suffix))
    return rows


def account_card_text(
    acc: AccountSnapshot,
    width: int,
    *,
    threshold: float | None = None,
    now: float | None = None,
) -> Text:
    """The full account card: header line + per-window bar rows."""
    now = now if now is not None else time.time()
    colors = current_theme_colors()

    text = Text()
    text.append(f"{acc.number:>2}  ", style=f"bold {colors.foreground}")
    text.append(acc.email, style=colors.foreground)
    text.append(f"  [{acc.display_tag}]", style=colors.muted)
    if acc.is_active:
        text.append("   ● active", style=f"bold {colors.accent}")
    age = data.format_age(acc.usage.age_s)
    if age:
        text.append(f"   {age}", style=colors.muted)

    sentinel = acc.usage.sentinel
    if sentinel is not None:
        text.append("\n    ")
        style = colors.muted if sentinel == USAGE_API_KEY else colors.sev_warn
        marker = "·" if sentinel == USAGE_API_KEY else "⚠"
        text.append(f"{marker} {data.sentinel_label(sentinel)}", style=style)
        # Same supplementary line `cswap list` prints: the last good
        # measurement behind the sentinel (API-key accounts have no quota to
        # have "seen").
        if sentinel != USAGE_API_KEY:
            last_seen = data.last_seen_note(acc.usage)
            if last_seen is not None:
                text.append("\n    ")
                text.append(f"└ {last_seen}", style=colors.muted)
        return text

    rows = usage_rows(acc.usage.last_good, now)
    if not rows:
        text.append("\n    ")
        text.append("usage unavailable", style=colors.muted)
        if acc.usage.last_error:
            text.append(f" · {acc.usage.last_error}", style=colors.muted)
        return text

    stale = acc.usage.age_s is not None and acc.usage.age_s > STALE_OK_S
    label_width = max(len(label) for label, _pct, _suffix in rows)
    bar_width = max(12, min(30, width - 42 - label_width))
    for label, pct, suffix in rows:
        text.append("\n    ")
        text.append(
            usage_bar(
                f"{label:<{label_width}}",
                pct,
                suffix or None,
                bar_width,
                stale=stale,
                threshold=threshold,
            )
        )
    return text


def mini_account_text(acc: AccountSnapshot, now: float) -> Text:
    """One minimized line for an inactive account.

    ``2  work@acme.dev [personal]   5h 92% · 7d 63%`` — pcts only, severity
    colored; a window at/over 100% brings its reset countdown along, and a
    maxed per-model window shows as ``Fable (!)``. Sentinel states show
    their label instead.
    """
    colors = current_theme_colors()
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append(f"{acc.number:>2}  ", style=f"bold {colors.muted}")
    text.append(acc.email, style=colors.foreground)
    text.append(f"  [{acc.display_tag}]", style=colors.muted)
    text.append("   ")

    sentinel = acc.usage.sentinel
    if sentinel is not None:
        style = colors.muted if sentinel == USAGE_API_KEY else colors.sev_warn
        text.append(data.sentinel_label(sentinel), style=style)
        return text

    last_good = acc.usage.last_good
    stale = acc.usage.age_s is not None and acc.usage.age_s > STALE_OK_S
    parts = 0
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        window = last_good.get(key) if isinstance(last_good, dict) else None
        if not window:
            continue
        pct = float(window["pct"])
        if parts:
            text.append(" · ", style=colors.track)
        color = severity_color(pct)
        text.append(f"{label} ", style=colors.muted)
        text.append(f"{pct:.0f}%", style=f"{color} dim" if stale else color)
        if pct >= 100:
            reset = data.reset_text(window, now)
            if reset:
                text.append(f" ({reset})", style=colors.muted)
        parts += 1
    maxed = [
        w["name"]
        for w in (last_good.get("scoped") or [] if isinstance(last_good, dict) else [])
        if float(w["pct"]) >= 100
    ]
    for name in maxed:
        if parts:
            text.append(" · ", style=colors.track)
        text.append(f"{name} (!)", style=colors.sev_crit)
        parts += 1
    if not parts:
        text.append("usage unknown", style=colors.muted)
    return text


class AccountsPanel(Static):
    """Static account overview: the active account full-size, others as
    one-line minis (in slot order, expanded in place). The dashboard's — and
    with ``show_minis=False`` the auto screen's — always-visible monitor."""

    def __init__(self, *, show_minis: bool = True, id: str | None = None) -> None:
        super().__init__(id=id)
        self._show_minis = show_minis

    def on_mount(self) -> None:
        self.watch(self.app, "snapshot", lambda _snap: self.refresh(layout=True))

    def render(self) -> Text:
        app: "CswapApp" = self.app  # type: ignore[assignment]
        colors = current_theme_colors()
        snap = app.snapshot
        if snap is None:
            return Text("loading…", style=colors.muted)
        if not snap.accounts:
            return Text(
                "No managed accounts yet.\n"
                "Use the menu below: Add account — from your current "
                "Claude Code login, or from a setup-token / API key.",
                style=colors.muted,
            )
        now = time.time()
        width = (self.size.width or 80) - 2
        blocks: list[Text] = []
        for acc in snap.accounts:
            if acc.is_active:
                blocks.append(
                    account_card_text(
                        acc, width, threshold=app.threshold_pct, now=now
                    )
                )
            elif self._show_minis:
                blocks.append(mini_account_text(acc, now))
        if not blocks:
            return Text("no active managed login", style=colors.muted)
        text = Text()
        previous_multiline = False
        for i, block in enumerate(blocks):
            multiline = "\n" in block.plain
            if i:
                # breathe around the expanded active card
                text.append("\n\n" if (multiline or previous_multiline) else "\n")
            text.append(block)
            previous_multiline = multiline
        return text


class AccountCard(Static):
    """One account rendered full-size (used by the switch screen's list)."""

    def __init__(self, acc: AccountSnapshot, *, threshold: float | None = None) -> None:
        super().__init__()
        self._acc = acc
        self._threshold = threshold

    def set_account(self, acc: AccountSnapshot) -> None:
        self._acc = acc
        self.refresh(layout=True)

    def render(self) -> Text:
        return account_card_text(
            self._acc, self.size.width or 80, threshold=self._threshold
        )


class AccountItem(ListItem):
    """ListView row wrapping an :class:`AccountCard`; remembers its slot."""

    def __init__(self, acc: AccountSnapshot) -> None:
        super().__init__(AccountCard(acc))
        self.number = acc.number
        self.email = acc.email

    def set_account(self, acc: AccountSnapshot) -> None:
        self.number = acc.number
        self.email = acc.email
        self.query_one(AccountCard).set_account(acc)


class MenuItem(ListItem):
    """One menu row: a label plus an action id the screen dispatches on."""

    def __init__(self, label: str, action_id: str, *, muted: bool = False) -> None:
        colors = current_theme_colors()
        style = colors.muted if muted else colors.foreground
        super().__init__(Static(Text(label, style=style)))
        self.action_id = action_id
