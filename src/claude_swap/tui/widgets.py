"""Shared render widgets: usage bars, account cards, and the accounts panel.

``bar_cells``/``usage_bar`` are custom renderers rather than Textual's
``ProgressBar`` because the design needs three things the stock widget
doesn't do: a severity color ramp, an optional threshold tick mark (the
auto-switch trigger line), and stale-measurement dimming.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Callable, Iterable, NamedTuple

from rich.cells import cell_len
from rich.text import Text
from textual.widgets import ListItem, Static

from claude_swap import oauth, pace, subscription
from claude_swap.json_output import USAGE_API_KEY, USAGE_RELOGIN_REQUIRED
from claude_swap.models import AccountSnapshot
from claude_swap.oauth import FIVE_HOUR_LABEL
from claude_swap.printer import format_age
from claude_swap.switcher import ERROR_NOTES
from claude_swap.usage_store import STALE_OK_S
from claude_swap.tui import data
from claude_swap.tui.theme import Palette

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
    palette: Palette = Palette.DARK,
) -> Text:
    """Just the bar glyphs: severity-colored fill, track, optional tick."""
    text = Text()
    if pct is None:
        text.append(_BAR_EMPTY * width, style=palette.track)
        return text
    frac = min(max(pct, 0.0), 100.0) / 100.0
    cells = frac * width
    full = int(cells)
    half = (cells - full) >= 0.5 and full < width
    tick_at: int | None = None
    if threshold is not None:
        tick_at = min(width - 1, max(0, round(threshold / 100.0 * width)))
    color = palette.severity(pct)
    fill_style = f"{color} dim" if stale else color
    for i in range(width):
        if tick_at is not None and i == tick_at:
            text.append(_BAR_TICK, style=palette.sev_warn)
        elif i < full:
            text.append(_BAR_FILLED, style=fill_style)
        elif i == full and half:
            text.append(_BAR_HALF, style=fill_style)
        else:
            text.append(_BAR_EMPTY, style=palette.track)
    return text


def usage_bar(
    label: str,
    pct: float | None,
    suffix: str | None,
    width: int,
    *,
    stale: bool = False,
    threshold: float | None = None,
    palette: Palette = Palette.DARK,
) -> Text:
    """One full bar line: ``5h ━━━━╸────┃──  47%  resets 2h 13m · 20:39``."""
    text = Text()
    text.append(f"{label} ", style=palette.muted)
    text.append(bar_cells(pct, width, stale=stale, threshold=threshold, palette=palette))
    if pct is None:
        text.append("  usage unknown", style=palette.muted)
    else:
        color = palette.severity(pct)
        text.append(f" {pct:3.0f}%", style=f"{color} dim" if stale else color)
    if suffix:
        text.append(f"  {suffix}", style=palette.muted)
    return text


def _reset_parts(window: dict, now: float) -> tuple[str | None, str | None]:
    """Countdown suffix and its clock-extended variant for one window.

    ``("resets 2h 13m", "resets 2h 13m · 20:39")`` — the second form is what
    a row shows when it has the width for it. Equal when no clock is known.
    """
    reset = data.reset_text(window, now)
    if not reset:
        return None, None
    clock = data.reset_clock(window, now)
    return reset, f"{reset} · {clock}" if clock else reset


def _pace_suffix(window: dict, fetched_at: float | None) -> str:
    """"(ahead of pace)" when a weekly window is meaningfully ahead, else ""."""
    result = pace.compute_pace(window, fetched_at=fetched_at)
    return "(ahead of pace)" if result and result.ahead else ""


def usage_rows(
    last_good: dict | None, now: float, fetched_at: float | None = None
) -> list[tuple[str, float | None, str, str]]:
    """(label, pct, suffix, suffix_full) rows mirroring the CLI's
    ``_format_usage_lines``. ``pct`` is None for a 5h/7d window reported
    without a figure; ``usage_bar`` draws that row as ``usage unknown``.

    ``suffix_full`` extends the reset countdown with the absolute clock time
    (``resets 2h 13m · 20:39``) for rows that have room; otherwise it equals
    ``suffix``. Only windows the account actually has produce a row — an
    annual plan without a 7-day window simply has no 7d line. Order matches
    the CLI: spend, 5h, 7d, then per-model scoped windows (e.g. "Fable"),
    the latter marked ``(!)`` at/over their limit. The weekly (7d) and scoped
    rows also carry a "(ahead of pace)" marker when meaningfully ahead of the
    week's expected usage (issue #125) — never the 5h row.
    """
    if not isinstance(last_good, dict):
        return []
    rows: list[tuple[str, float | None, str, str]] = []
    spend = last_good.get("spend")
    if spend:
        amounts = f"${spend['used']:,.2f} / ${spend['limit']:,.2f}"
        reset, reset_full = _reset_parts(spend, now)
        suffix = f"{reset}  {amounts}" if reset else amounts
        suffix_full = f"{reset_full}  {amounts}" if reset_full else amounts
        rows.append(("$$", float(spend["pct"]), suffix, suffix_full))
    for key, label in (("five_hour", FIVE_HOUR_LABEL), ("seven_day", "7d")):
        window = last_good.get(key)
        if window:
            reset, reset_full = _reset_parts(window, now)
            suffix, suffix_full = reset or "", reset_full or ""
            if key == "seven_day":
                marker = _pace_suffix(window, fetched_at)
                if marker:
                    suffix = f"{suffix}  {marker}" if suffix else marker
                    suffix_full = f"{suffix_full}  {marker}" if suffix_full else marker
            pct = window.get("pct")
            rows.append((
                label,
                float(pct) if isinstance(pct, (int, float)) else None,
                suffix,
                suffix_full,
            ))
    for window in last_good.get("scoped") or []:
        pct = float(window["pct"])
        suffix, suffix_full = _reset_parts(window, now)
        suffix, suffix_full = suffix or "", suffix_full or ""
        if pct >= 100:
            suffix = f"{suffix}  (!)" if suffix else "(!)"
            suffix_full = f"{suffix_full}  (!)" if suffix_full else "(!)"
        else:
            marker = _pace_suffix(window, fetched_at)
            if marker:
                suffix = f"{suffix}  {marker}" if suffix else marker
                suffix_full = f"{suffix_full}  {marker}" if suffix_full else marker
        rows.append((window["name"], pct, suffix, suffix_full))
    rejected = last_good.get("rejected")
    if isinstance(rejected, dict):
        # A refused setup-token probe: at its limit until the refusal ends,
        # whatever the windows above say (auto-switching excludes it).
        reset, reset_full = _reset_parts(rejected, now)
        suffix = f"rejected (!)  {reset}" if reset else "rejected (!)"
        suffix_full = f"rejected (!)  {reset_full}" if reset_full else "rejected (!)"
        rows.append(("probe", 100.0, suffix, suffix_full))
    return rows


# Login-note forms, fullest first: "re-login in 5d 12h", "re-login in 5d",
# nothing. A row carries one only while its login is due
# (``oauth.login_time_left``), so most rows read as if there were no note.
LOGIN_FORMS = ("full", "short", None)


def login_segment(
    acc: AccountSnapshot, form: str | None, now: float | None = None, gap: str = "  "
) -> str:
    """The row's ``re-login in …`` note led by ``gap``, or ``""`` when
    ``form`` is None or the slot's login is not due."""
    if form is None:
        return ""
    quarantined = acc.usage.sentinel == USAGE_RELOGIN_REQUIRED
    note = oauth.format_login_expiry(
        acc.login_expires_at, quarantined, now, short=form == "short"
    )
    return f"{gap}{note}" if note else ""


def fit_login(build: Callable[[str | None], list[Text]], budget: int) -> list[Text]:
    """``build(form)`` for the fullest login form in which every row's first
    line fits ``budget`` columns; usage figures are never cut, so with no
    fitting form the login goes."""
    for form in LOGIN_FORMS:
        rows = build(form)
        if all(cell_len(row.plain.strip("\n").partition("\n")[0]) <= budget for row in rows):
            break
    return rows


def _append_plan(text: Text, acc: AccountSnapshot, gap: str, palette) -> None:
    """``· Ultra`` after the header — the same marker ``cswap list`` prints:
    a lapsed subscription is a highlighted ``Free`` rather than an appended
    "expired", and the age follows once the plan has gone two TTLs without
    refreshing."""
    record = acc.subscription_plan
    if not record:
        return
    plan = record["plan"]
    text.append(f"{gap}· ", style=palette.muted)
    text.append(
        subscription.plan_label(plan),
        style=palette.sev_warn if subscription.is_expired(plan) else palette.muted,
    )
    if time.time() - record["fetchedAt"] >= 2 * subscription.PLAN_TTL_S:
        age = format_age(int(record["fetchedAt"] * 1000))
        text.append(f" ({age})", style=palette.muted)


def _policy_badges(acc: AccountSnapshot, gap: str) -> list[str]:
    """Badge strings for the account's per-account policy, in display order.

    Returns an empty list for an account carrying no policy, which is every
    account in a store that has never used the feature — that is what keeps
    those rows byte-identical to their pre-policy render.

    ``gap`` is the leading separator the render path puts before its badges
    (three spaces on the card; none on the compact row, whose marker field
    joins its own).
    """
    badges: list[str] = []
    if acc.policy.threshold is not None:
        # ``:g`` so 75.0 reads as "75%" and 82.5 keeps its fraction — the store
        # accepts one decimal place and the row shows back exactly what was set.
        badges.append(f"{gap}th {acc.policy.threshold:g}%")
    return badges


def account_card_text(
    acc: AccountSnapshot,
    width: int,
    *,
    threshold: float | None = None,
    weekly_threshold: float | None = None,
    now: float | None = None,
    palette: Palette = Palette.DARK,
    login: str | None = "full",
) -> Text:
    """The full account card: header line + per-window bar rows. Each row's
    tick is the switch line that row is judged against: ``threshold`` on the
    5h (and spend) rows, ``weekly_threshold`` — when set — on the 7d and
    per-model rows, which reset weekly. ``login`` is the form of the re-login
    note that ends the header while the login is due (see ``LOGIN_FORMS``)."""
    now = now if now is not None else time.time()

    text = Text()
    text.append(f"{acc.number:>2}  ", style=f"bold {palette.foreground}")
    if acc.alias:
        text.append(acc.alias, style=f"bold {palette.accent}")
        text.append(f" ({acc.email})", style=palette.foreground)
    else:
        text.append(acc.email, style=palette.foreground)
    text.append(f"  [{acc.display_tag}]", style=palette.muted)
    if acc.is_active:
        text.append("   ● active", style=f"bold {palette.accent}")
    if acc.disabled:
        text.append("   (disabled)", style=palette.muted)
    for badge in _policy_badges(acc, "   "):
        text.append(badge, style=palette.muted)
    _append_plan(text, acc, "   ", palette)
    age = data.format_age(acc.usage.age_s)
    if age:
        text.append(f"   {age}", style=palette.muted)
    text.append(login_segment(acc, login, now, "   "), style=palette.muted)

    sentinel = acc.usage.sentinel
    if sentinel is not None:
        text.append("\n    ")
        style = palette.muted if sentinel == USAGE_API_KEY else palette.sev_warn
        marker = "·" if sentinel == USAGE_API_KEY else "⚠"
        text.append(f"{marker} {data.sentinel_label(sentinel)}", style=style)
        # Same supplementary line `cswap list` prints: the last good
        # measurement behind the sentinel (API-key accounts have no quota to
        # have "seen").
        if sentinel != USAGE_API_KEY:
            last_seen = data.last_seen_note(acc.usage)
            if last_seen is not None:
                text.append("\n    ")
                text.append(f"└ {last_seen}", style=palette.muted)
        return text

    rows = usage_rows(acc.usage.last_good, now, acc.usage.fetched_at)
    if not rows:
        text.append("\n    ")
        text.append("usage unavailable", style=palette.muted)
        if acc.usage.last_error:
            # Same wording as the CLI detail line: error KINDS with a
            # friendly note render it, so both surfaces describe the state
            # identically.
            note = ERROR_NOTES.get(acc.usage.last_error, acc.usage.last_error)
            text.append(f" · {note}", style=palette.muted)
        return text

    stale = acc.usage.age_s is not None and acc.usage.age_s > STALE_OK_S
    label_width = max(len(label) for label, _pct, _suffix, _full in rows)
    bar_width = max(12, min(30, width - 42 - label_width))
    for label, pct, suffix, suffix_full in rows:
        # everything on a row except the suffix: indent, label, bar, " NNN%"
        # (or "  usage unknown" on a row without a figure), gap
        row_overhead = 4 + label_width + 1 + bar_width + (15 if pct is None else 5) + 2
        # per-row: show the absolute clock only where it fits, so a long
        # spend row degrading doesn't cost the 5h/7d rows their clocks
        if suffix_full != suffix and row_overhead + len(suffix_full) <= width:
            suffix = suffix_full
        row_line = (
            weekly_threshold
            if weekly_threshold is not None and label not in ("$$", FIVE_HOUR_LABEL)
            else threshold
        )
        text.append("\n    ")
        text.append(
            usage_bar(
                f"{label:<{label_width}}",
                pct,
                suffix or None,
                bar_width,
                stale=stale,
                threshold=row_line,
                palette=palette,
            )
        )
    return text


def fitted_card_text(acc: AccountSnapshot, width: int, budget: int, **kwargs) -> Text:
    """``account_card_text`` with the login-note form its header has room for in ``budget`` columns."""
    (card,) = fit_login(
        lambda login: [account_card_text(acc, width, login=login, **kwargs)], budget
    )
    return card


def _end_mini(text: Text, acc: AccountSnapshot, form: str | None, now: float, palette: Palette) -> Text:
    """Close a mini row with its ``re-login in …`` note, while the login is due."""
    note = login_segment(acc, form, now, gap="")
    if note:
        text.append(" · ", style=palette.track)
        text.append(note, style=palette.muted)
    return text


_MINI_TAG_CAP = 24  # widest "[tag]" a compact row shows, brackets included
_MINI_NOTE_CAP = 30  # widest note that widens the body column; longer is ellipsized
_MINI_GAP = "  "


class MiniWidths(NamedTuple):
    """Column widths the compact rows share, from :func:`mini_widths`."""

    name: int
    tag: int
    cells: dict[str, int]  # window label -> cell width, in display order
    note: int  # widest sentinel / "usage unknown" note shown, capped
    marker: int  # widest "(ahead) (disabled)" marker field shown


def _fit(text: Text, width: int) -> Text:
    text.truncate(width, overflow="ellipsis", pad=True)
    return text


def _mini_name(acc: AccountSnapshot, palette: Palette) -> Text:
    name = Text()
    if acc.alias:
        name.append(acc.alias, style=f"bold {palette.accent}")
        name.append(f" ({acc.email})", style=palette.foreground)
    else:
        name.append(acc.email, style=palette.foreground)
    return name


def _mini_tag(acc: AccountSnapshot, palette: Palette) -> Text:
    tag = Text(acc.display_tag)
    tag.truncate(_MINI_TAG_CAP - 2, overflow="ellipsis")
    return Text.assemble("[", tag, "]", style=palette.muted)


def _mini_marks(acc: AccountSnapshot, ahead: bool, palette: Palette) -> Text:
    """The row's one trailing marker field: ``(ahead)``, ``(disabled)``, the
    account's policy badges, then its subscription plan."""
    marks = [Text("(ahead)", style=palette.sev_warn)] if ahead else []
    if acc.disabled:
        marks.append(Text("(disabled)", style=palette.muted))
    marks.extend(Text(b, style=palette.muted) for b in _policy_badges(acc, ""))
    plan = Text()
    _append_plan(plan, acc, "", palette)
    if plan:
        marks.append(plan)
    return Text(" ").join(marks)


def _mini_body(
    acc: AccountSnapshot, now: float, palette: Palette
) -> tuple[dict[str, Text], Text | None, bool]:
    """A compact row's window cells (label -> cell), or the note that stands in
    for them when it has no window to show, and whether the weekly window is
    ahead of pace (a row state, shown in the marker field, not in a cell)."""
    sentinel = acc.usage.sentinel
    if sentinel is not None:
        style = palette.muted if sentinel == USAGE_API_KEY else palette.sev_warn
        return {}, Text(data.sentinel_label(sentinel), style=style), False
    if acc.usage.last_error == "oauth_not_allowed_for_organization":  # lapsed plan
        return {}, Text("no plan", style=palette.sev_warn), False
    last_good = acc.usage.last_good
    if not isinstance(last_good, dict):
        last_good = {}
    stale = acc.usage.age_s is not None and acc.usage.age_s > STALE_OK_S
    cells: dict[str, Text] = {}
    ahead = False
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        window = last_good.get(key)
        if not window:
            continue
        if not isinstance(window.get("pct"), (int, float)):
            # No figure: unknown, not 0%. Padded to the width of "NNN%".
            cells[label] = Text.assemble(
                (f"{label} ", palette.muted), (f"{'?':>4}", palette.muted)
            )
            continue
        pct = float(window["pct"])
        color = palette.severity(pct)
        cell = Text(f"{label} ", style=palette.muted)
        cell.append(f"{pct:3.0f}%", style=f"{color} dim" if stale else color)
        # a window at/over 100% shows its reset; below it, 7d may be ahead of pace
        if pct >= 100:
            reset = data.reset_text(window, now)
            if reset:
                cell.append(f" ({reset})", style=palette.muted)
        elif key == "seven_day":
            result = pace.compute_pace(window, fetched_at=acc.usage.fetched_at)
            ahead = bool(result and result.ahead)
        cells[label] = cell
    for window in last_good.get("scoped") or []:
        if float(window["pct"]) >= 100:
            name = window["name"]
            cells[name] = Text(f"{name} (!)", style=palette.sev_crit)
    if isinstance(last_good.get("rejected"), dict):  # a refused setup-token probe
        cells["rejected"] = Text("rejected (!)", style=palette.sev_crit)
    return cells, None if cells else Text("usage unknown", style=palette.muted), ahead


def mini_widths(accs: Iterable[AccountSnapshot], now: float) -> MiniWidths:
    """Column widths that fit every one of ``accs``, the compact rows shown."""
    palette = Palette.DARK  # widths do not depend on style
    name = tag = note = marker = 0
    cells = {"5h": 0, "7d": 0}  # fixed order; scoped windows follow as seen
    for acc in accs:
        name = max(name, _mini_name(acc, palette).cell_len)
        tag = max(tag, _mini_tag(acc, palette).cell_len)
        row_cells, row_note, ahead = _mini_body(acc, now, palette)
        for label, cell in row_cells.items():
            cells[label] = max(cells.get(label, 0), cell.cell_len)
        if row_note:
            note = max(note, min(row_note.cell_len, _MINI_NOTE_CAP))
        marker = max(marker, _mini_marks(acc, ahead, palette).cell_len)
    return MiniWidths(name, tag, {k: w for k, w in cells.items() if w}, note, marker)


def mini_account_text(
    acc: AccountSnapshot,
    now: float,
    *,
    palette: Palette = Palette.DARK,
    widths: MiniWidths | None = None,
    width: int | None = None,
    login: str | None = "full",
) -> Text:
    """One minimized line for an inactive account, in fixed columns.

    ``2  work@acme.dev  [personal]  5h  92%  7d  63%  Fable (!)  (ahead) (disabled)``
    — slot, name, tag, one cell per window, then one marker field:
    ``(ahead)`` (the weekly window is ahead of pace), ``(disabled)`` and
    ``th 75%`` (a per-account threshold) and ``· Max 20x`` (the subscription
    plan), any of them together.
    Pcts only, severity colored; a window at/over 100% brings its reset
    countdown along, a maxed per-model window shows as ``Fable (!)`` and a
    refused setup-token probe as ``rejected (!)``. A
    window the account lacks is a blank cell, and a sentinel state shows its
    label in place of the cells (ellipsized past ``_MINI_NOTE_CAP`` and the
    cells' width; the expanded card and the CLI carry the full text). Every
    row's body is as wide as the wider of the cells and the longest such
    note, so the marker field keeps one column.
    ``widths`` (from :func:`mini_widths` over the rows shown) lines the
    columns up across rows; alone, a row fits itself. A row wider than
    ``width`` is ellipsized before the marker field, which stays whole and
    starts where the widest field shown would, so the markers still share
    one column. ``login`` is the form of the ``· re-login in …`` note that
    ends the row while the login is due (see ``LOGIN_FORMS``); it follows
    the marker field, padded so the notes share one column, and is cut with
    it, never before it.
    """
    widths = widths or mini_widths([acc], now)
    cells, note, ahead = _mini_body(acc, now, palette)
    marks = _mini_marks(acc, ahead, palette)
    grid = Text(_MINI_GAP).join(
        _fit(cells.get(label) or Text(), width) for label, width in widths.cells.items()
    )
    fields = [
        _fit(_mini_name(acc, palette), widths.name),
        _fit(_mini_tag(acc, palette), widths.tag),
        _fit(note or grid, max(grid.cell_len, widths.note)),
    ]
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append(f"{acc.number:>2}  ", style=f"bold {palette.muted}")
    text.append(Text(_MINI_GAP).join(fields))
    end = _end_mini(Text(), acc, login, now, palette)
    marker_w = max(widths.marker, marks.cell_len)
    if end:
        marks = _fit(marks, marker_w)  # pad the marker field: the notes share a column
    tail = Text.assemble(_MINI_GAP, marks) if marks else Text()
    tail.append(end)
    if not tail:
        text.rstrip()
    # a cut row reserves the widest marker field shown, so cut markers align too
    reserve = (len(_MINI_GAP) + marker_w if marks else 0) + end.cell_len
    if width is not None and text.cell_len + reserve > width:
        room = width - reserve
        if room < 1:  # no room for the marker: cut the whole row
            tail, room = Text(), width
        # blank padding is no content: the ellipsis marks a real cut only
        text.rstrip()
        text.truncate(room, overflow="ellipsis", pad=True)
    return text.append(tail)


class AccountsPanel(Static):
    """Static account overview: the active account full-size, others as
    one-line minis (in slot order, expanded in place). The dashboard's — and
    with ``show_minis=False`` the auto screen's — always-visible monitor."""

    def __init__(self, *, show_minis: bool = True, id: str | None = None) -> None:
        super().__init__(id=id)
        self._show_minis = show_minis

    def on_mount(self) -> None:
        self.watch(self.app, "snapshot", lambda _snap: self.refresh(layout=True))
        self.watch(self.app, "theme", lambda _t: self.refresh(layout=True))

    def render(self) -> Text:
        app: "CswapApp" = self.app  # type: ignore[assignment]
        palette = Palette.from_theme(app.current_theme)
        snap = app.snapshot
        if snap is None:
            return Text("loading…", style=palette.muted)
        if not snap.accounts:
            return Text(
                "No managed accounts yet.\n"
                "Use the menu below: Add account — from your current "
                "Claude Code login, or from a setup-token / API key.",
                style=palette.muted,
            )
        now = time.time()
        panel_width = self.size.width or 80
        width = panel_width - 2
        inactive = [a for a in snap.accounts if not a.is_active and self._show_minis]
        widths = mini_widths(inactive, now)
        # One login-note form for all the minis, so the rows that carry one
        # read alike; only the row without a note is cut to the width, any
        # other form fits whole or steps down, so usage is never cut for it.
        minis = iter(
            fit_login(
                lambda login: [
                    mini_account_text(
                        acc, now, palette=palette, widths=widths, login=login,
                        width=None if login else width,
                    )
                    for acc in inactive
                ],
                panel_width,
            )
        )
        blocks: list[Text] = []
        for acc in snap.accounts:
            if acc.is_active:
                blocks.append(
                    fitted_card_text(
                        acc, width, panel_width, threshold=app.threshold_pct,
                        weekly_threshold=app.weekly_threshold_pct, now=now,
                        palette=palette,
                    )
                )
            elif self._show_minis:
                blocks.append(next(minis))  # never wraps
        if not blocks:
            return Text("no active managed login", style=palette.muted)
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
        width = self.size.width or 80
        return fitted_card_text(
            self._acc, width, width, threshold=self._threshold,
            palette=Palette.from_theme(self.app.current_theme),
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
        item = Static(label, markup=False)
        if muted:
            item.add_class("menu-item-muted")
        super().__init__(item)
        self.action_id = action_id
