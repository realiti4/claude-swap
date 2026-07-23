"""macOS menu bar app for claude-swap (``cswap --menubar``).

A thin GUI shell over ``ClaudeAccountSwitcher`` and the core auto-switch engine
(``claude_swap.autoswitch``) — it never re-implements account, usage, or
auto-switch logic. Usage for display comes from ``switcher.accounts_snapshot()``
(backed by the shared usage store); auto-switching, when enabled, runs the same
``AutoSwitchEngine`` the CLI's ``cswap auto`` drives, sharing
``autoswitch_state.json`` and the ``autoswitch.*`` settings. The menu bar keeps
only its own display preferences.

Built on ``rumps`` (an optional extra, macOS only). The pure helpers below
(settings, formatting, log parsing) are import-safe without rumps so they can be
unit-tested in CI; ``rumps`` is imported lazily inside the app glue.
"""

from __future__ import annotations

import json
import logging
import os
import plistlib
import re
import sys
import threading
import time
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime, timezone
from pathlib import Path

from claude_swap import pace
from claude_swap.exceptions import ClaudeSwitchError, CredentialReadError
from claude_swap.switcher import SENTINEL_NOTES

ICON = "⇄"
REFRESH_CHOICES: tuple[int, ...] = (30, 60, 300)
AUTO_THRESHOLD_CHOICES: tuple[int, ...] = (80, 90, 95, 98)
TITLE_PCT_CHOICES: tuple[str, ...] = ("off", "5h", "7d", "both")
# How each account row is laid out. "compact" is the original free-text line;
# the rest share one column grid across all accounts (see build_account_table),
# except "focus", which leads with each account's binding window.
ROW_STYLE_CHOICES: tuple[str, ...] = ("compact", "columns", "bars", "focus")
# Layouts built on the shared column grid; "show reset times" applies to these.
GRID_ROW_STYLES: tuple[str, ...] = ("columns", "bars")
SWITCH_HISTORY_LIMIT = 10
NOTIFICATION_BUNDLE_ID = "com.claude-swap.menubar"


def ensure_notification_identity(
    executable: Path | None = None,
    *,
    platform: str = sys.platform,
) -> Path | None:
    """Ensure rumps can resolve a bundle identifier for notifications.

    Command-line Python tools have no app bundle, so rumps looks for an
    ``Info.plist`` beside the interpreter. uv/pipx reinstalls can recreate that
    environment; repair the tiny plist on every launch when needed.
    """
    if platform != "darwin":
        return None
    path = (executable or Path(sys.executable)).parent / "Info.plist"
    data: dict = {}
    try:
        if path.exists():
            try:
                loaded = plistlib.loads(path.read_bytes())
            except Exception:
                loaded = None  # unreadable/corrupt — rebuild from scratch
            if isinstance(loaded, dict):
                data = loaded
        changed = False
        if not data.get("CFBundleIdentifier"):
            data["CFBundleIdentifier"] = NOTIFICATION_BUNDLE_ID
            changed = True
        if not data.get("CFBundleName"):
            data["CFBundleName"] = "claude-swap"
            changed = True
        if changed or not path.exists():
            # atomic: an interrupted write must not leave a half-written plist
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes(plistlib.dumps(data))
            os.replace(tmp, path)
    except (OSError, plistlib.InvalidFileException, ValueError) as exc:
        logging.getLogger("claude-swap").warning(
            "Could not prepare menu-bar notification identity: %s", exc
        )
        return None
    return path


@dataclass
class MenuBarSettings:
    """User-configurable menu bar display behavior, persisted as JSON.

    Only display preferences and the auto-switch on/off toggle live here.
    Auto-switch *policy* (threshold, cooldown, hysteresis, …) is core config,
    read/written through ``claude_swap.settings`` (the ``autoswitch.*`` keys),
    so the CLI and the menu bar share one source of truth.
    """

    show_account_name: bool = True
    title_pct: str = "both"  # one of TITLE_PCT_CHOICES
    title_scoped: bool = False  # append per-model weekly limits (e.g. Fable) to the title
    refresh_interval: int = 60
    auto_switch_enabled: bool = False
    row_style: str = "compact"  # one of ROW_STYLE_CHOICES
    show_resets: bool = False  # second, dimmed line with reset countdowns

    @classmethod
    def load(cls, path: Path) -> "MenuBarSettings":
        """Load settings, falling back to defaults on any problem.

        Unknown keys are ignored; a value whose type doesn't match the field
        default is dropped (that field keeps its default). A missing or
        unparseable file yields all-defaults.
        """
        defaults = cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return defaults
        if not isinstance(raw, dict):
            return defaults
        kwargs = {}
        for f in fields(cls):
            if f.name in raw and isinstance(raw[f.name], type(getattr(defaults, f.name))):
                kwargs[f.name] = raw[f.name]
        return cls(**kwargs)

    def save(self, path: Path) -> None:
        """Write settings as pretty JSON, creating parent directories."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


# ---- pure display helpers (operate on the usage-window dict shape produced by
# ---- oauth.build_usage_result / stored in UsageEntry.last_good) --------------

def tightest_pct(usage: dict | str | None) -> float | None:
    """Highest 5h/7d utilization percentage, or None if unknown.

    Surfaces the binding window's utilization for display. Spend is excluded —
    it isn't a rate-limit window.
    """
    if not isinstance(usage, dict):
        return None
    pcts = [
        window["pct"]
        for window in (usage.get("five_hour"), usage.get("seven_day"))
        if isinstance(window, dict) and isinstance(window.get("pct"), (int, float))
    ]
    return max(pcts) if pcts else None


def _window_pct(usage: dict | str | None, key: str) -> float | None:
    """Utilization pct for a usage window (``five_hour``/``seven_day``), or None."""
    if isinstance(usage, dict):
        window = usage.get(key)
        if isinstance(window, dict) and isinstance(window.get("pct"), (int, float)):
            return float(window["pct"])
    return None


def _resets_at_ts(window: dict | str | None) -> float:
    """POSIX timestamp of a usage window's ``resets_at``; inf if missing/bad."""
    if isinstance(window, dict):
        ra = window.get("resets_at")
        if isinstance(ra, str):
            try:
                return datetime.fromisoformat(ra).timestamp()
            except ValueError:
                pass
    return float("inf")


def _live_countdown(window: dict | str | None, now: float) -> str | None:
    """Time until a usage window resets, computed live from ``resets_at``.

    The cached usage dict's ``countdown`` string is frozen at fetch time, so a
    stale (e.g. last-known-good) entry would show a wrong remaining time. Deriving
    it from the absolute ``resets_at`` keeps it correct between/without refetches.
    Returns ``None`` when there's no ``resets_at`` or it has already passed.
    """
    ts = _resets_at_ts(window)
    if ts == float("inf"):
        return None
    remaining = int(ts - now)
    if remaining <= 0:
        return None
    days, rem = divmod(remaining, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


_WEEKLY_PERIOD_S = 7 * 86400  # weekly limits reset on a fixed 7-day cadence


def _rolled_weekly_window(window: dict | None, now: float) -> dict | None:
    """A weekly window with a passed reset advanced to its next 7-day boundary.

    Weekly limits reset on a fixed weekly cadence, so once the stored
    ``resets_at`` is in the past we know the window rolled over — the stored pct
    belongs to a window that no longer exists. Return a copy reflecting the reset
    state (``pct`` 0, ``resets_at`` advanced to the next future boundary) so the
    menu bar shows the reset from the static schedule alone, without waiting to
    spend tokens on a fresh fetch. Missing/future/unparseable windows are
    returned unchanged.
    """
    if not isinstance(window, dict):
        return window
    ts = _resets_at_ts(window)
    if ts == float("inf") or ts > now:
        return window
    missed = int((now - ts) // _WEEKLY_PERIOD_S) + 1
    new_ts = ts + missed * _WEEKLY_PERIOD_S
    rolled = dict(window)
    rolled["pct"] = 0.0
    rolled["resets_at"] = datetime.fromtimestamp(new_ts, tz=timezone.utc).isoformat()
    rolled.pop("countdown", None)  # recomputed live from the rolled resets_at
    rolled.pop("clock", None)
    return rolled


def usage_summary(
    usage: dict | str | None, now: float | None = None, fetched_at: float | None = None
) -> str:
    """One-line usage summary for an account row (reset countdown computed live).

    ``fetched_at`` is the underlying measurement's fetch time (may be older
    than ``now`` when serving last-good data) — used only to flag a weekly
    window that's meaningfully ahead of pace (issue #125), never the 5h one.
    """
    if isinstance(usage, str):
        return usage
    if usage is None:
        return "usage unavailable"
    if now is None:
        now = time.time()
    parts: list[str] = []
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        window = usage.get(key)
        pace_result = None
        if key == "seven_day":
            window = _rolled_weekly_window(window, now)  # reflect a passed weekly reset
            # Pace against the rolled window, not the raw one: a stale window
            # rolled to 0% has no current-cycle data to compare against, so
            # its (correctly zeroed) pct naturally never reads as "ahead" —
            # computing pace pre-roll would otherwise pair last cycle's high
            # pct with this cycle's freshly-reset 0% display.
            pace_result = pace.compute_pace(window, fetched_at=fetched_at)
        if isinstance(window, dict) and isinstance(window.get("pct"), (int, float)):
            seg = f"{label} {window['pct']:.0f}%"
            if key == "seven_day" and pace_result and pace_result.ahead:
                seg += " (ahead)"
            countdown = _live_countdown(window, now)
            if countdown:
                seg += f" ({countdown})"  # time until this window resets
            parts.append(seg)
    # Per-model weekly limits (e.g. Fable), from the usage API's ``limits`` array.
    for window in usage.get("scoped") or []:
        window = _rolled_weekly_window(window, now)  # weekly cadence, same roll-forward
        pace_result = pace.compute_pace(window, fetched_at=fetched_at)  # against the rolled window, see above
        if isinstance(window, dict) and isinstance(window.get("pct"), (int, float)) and window.get("name"):
            seg = f"{window['name']} {window['pct']:.0f}%"
            if window["pct"] >= 100:
                seg += " (!)"  # maxed model — the usual reason to switch
            elif pace_result and pace_result.ahead:
                seg += " (ahead)"
            countdown = _live_countdown(window, now)
            if countdown:
                seg += f" ({countdown})"
            parts.append(seg)
    spend = usage.get("spend")
    if isinstance(spend, dict) and isinstance(spend.get("pct"), (int, float)):
        parts.append(f"$ {spend['pct']:.0f}%")
    return " · ".join(parts) if parts else "usage unavailable"


def format_account_label(
    num,
    email: str,
    usage: dict | str | None,
    now: float | None = None,
    alias: str | None = None,
    disabled: bool = False,
    fetched_at: float | None = None,
) -> str:
    """Build one account row's menu label."""
    label = f"{alias}  ({email})" if alias else email
    marker = "  (disabled)" if disabled else ""
    return f"{num}  {label}{marker}  {usage_summary(usage, now, fetched_at)}"


# ---- aligned table rows (the "columns" / "detailed" row styles) --------------
#
# The compact style formats each row independently, so nothing lines up: a
# proportional menu font plus per-account text of different lengths means the
# reader has to parse every row to compare two numbers. These helpers instead
# lay every account out on one shared column grid — which requires looking at
# all accounts at once (a model column exists only if some account reports it),
# so this works on the account list, not on a single row.

MAXED_MARKER = "!"   # window at/over its limit — the usual reason to switch
AHEAD_MARKER = "\u2191"   # weekly window running ahead of an even burn-down pace
RESETS_CAPTION = "resets in"  # leads the dimmed second line of a detailed row
SPEND_HEADER = "$"

# Utilization thresholds behind the severity tint. Only the constrained end is
# tinted: a pool of mostly-idle accounts should stay monochrome, with colour
# reserved for the rows that are actually running out.
WARNING_PCT = 80.0
CRITICAL_PCT = 95.0

@dataclass(frozen=True)
class TableColumn:
    """One column of the aligned account table."""

    key: str            # "num" | "name" | "window"
    title: str          # header text; "" for the num/name columns
    right_aligned: bool


@dataclass(frozen=True)
class TableRow:
    """One account's cells, the countdowns beneath them, and their severities."""

    cells: tuple[str, ...]
    resets: tuple[str, ...]             # same length as cells; "" where there's nothing
    severities: tuple[str | None, ...]  # None | "warning" | "critical", per cell
    values: tuple[float | None, ...]    # the raw percentage behind a cell, for bars


def severity_for(pct: float | None) -> str | None:
    """Severity tint for a utilization percentage, or None to leave it plain."""
    if pct is None:
        return None
    if pct >= CRITICAL_PCT:
        return "critical"
    if pct >= WARNING_PCT:
        return "warning"
    return None


NAME_LIMIT = 26  # characters; one long address must not push every number right


def truncate_name(name: str, limit: int = NAME_LIMIT) -> str:
    """Shorten an over-long account name, keeping the part that identifies it.

    The name column is as wide as its widest entry, so a single long address
    would push every percentage on every row to the right. Drops the domain
    first (it's usually the redundant part), then hard-truncates.
    """
    if len(name) <= limit:
        return name
    local, _, domain = name.partition("@")
    if domain and len(local) <= limit:
        shortened = f"{local}@…"
        if len(shortened) <= limit:
            return shortened
    return name[: limit - 1] + "…"


def _pct_text(window: dict, *, ahead: bool = False, marker: bool = True) -> str:
    """A percentage cell, with the at-limit / ahead-of-pace marker appended.

    ``marker=False`` for spend: an exhausted budget caps cost, it doesn't block
    a request, so flagging it like a maxed rate-limit window would misread.
    """
    pct = window["pct"]
    if marker and pct >= 100:
        return f"{pct:.0f}%{MAXED_MARKER}"
    return f"{pct:.0f}%{AHEAD_MARKER if (marker and ahead) else ''}"


def _scoped_windows(usage: dict, now: float) -> dict[str, dict]:
    """Named per-model weekly windows of one account, keyed by model name."""
    out: dict[str, dict] = {}
    for window in usage.get("scoped") or []:
        window = _rolled_weekly_window(window, now)
        if (
            isinstance(window, dict)
            and isinstance(window.get("pct"), (int, float))
            and window.get("name")
        ):
            out[window["name"]] = window
    return out


def account_table_model_names(accounts, now: float | None = None) -> tuple[str, ...]:
    """Per-model window names in column order (deduplicated, first-seen order)."""
    if now is None:
        now = time.time()
    names: list[str] = []
    for _num, _email, _active, usage, *_rest in accounts:
        if isinstance(usage, dict):
            for name in _scoped_windows(usage, now):
                if name not in names:
                    names.append(name)
    return tuple(names)


def account_table_columns(accounts, now: float | None = None) -> tuple[TableColumn, ...]:
    """The column grid shared by every account row.

    One column per usage window, with the window's name carried in the column
    *header* rather than repeated on every row. Model and spend columns appear
    only when at least one account reports them, so a pool without per-model
    limits doesn't carry empty columns around.
    """
    if now is None:
        now = time.time()
    columns = [
        # The slot number leads the row, before the first tab, so it has no tab
        # stop of its own and is simply left-aligned.
        TableColumn("num", "", False),
        TableColumn("name", "", False),
        TableColumn("window", "5h", True),
        TableColumn("window", "7d", True),
    ]
    for name in account_table_model_names(accounts, now):
        columns.append(TableColumn("window", name, True))
    for _num, _email, _active, usage, *_rest in accounts:
        spend = usage.get("spend") if isinstance(usage, dict) else None
        if isinstance(spend, dict) and isinstance(spend.get("pct"), (int, float)):
            columns.append(TableColumn("window", SPEND_HEADER, True))
            break
    return tuple(columns)


def account_table_header(columns: tuple[TableColumn, ...]) -> tuple[str, ...]:
    """Header cells for the column grid (blank over the num/name columns)."""
    return tuple(column.title for column in columns)


def build_account_table(accounts, now: float | None = None) -> list[TableRow]:
    """Lay every account out on the shared column grid.

    Accounts whose usage is a sentinel string ("re-login needed", "no
    credentials", …) or missing carry that message in their *name* cell instead:
    the window columns are right-aligned percentage columns, and a sentence
    dropped into one would run backwards across the row.
    """
    if now is None:
        now = time.time()
    columns = account_table_columns(accounts, now)
    model_names = account_table_model_names(accounts, now)
    width = len(columns)
    rows: list[TableRow] = []

    for num, email, _is_active, usage, _last_good, alias, disabled, fetched_at in accounts:
        name = f"{alias} ({email})" if alias else truncate_name(email)
        if disabled:
            name += "  (disabled)"
        cells = [str(num), name] + [""] * (width - 2)
        resets = [""] * width
        severities: list[str | None] = [None] * width
        values: list[float | None] = [None] * width

        if not isinstance(usage, dict):
            message = usage if isinstance(usage, str) else "usage unavailable"
            cells[1] = f"{name} — {message}"
            rows.append(TableRow(tuple(cells), tuple(resets), tuple(severities),
                                 tuple(values)))
            continue

        def place(index: int, window: dict, *, ahead: bool = False, tint: bool = True,
                  marker: bool = True) -> None:
            cells[index] = _pct_text(window, ahead=ahead, marker=marker)
            values[index] = float(window["pct"])
            if tint:
                severities[index] = severity_for(window["pct"])
            countdown = _live_countdown(window, now)
            if countdown:
                resets[index] = countdown

        five = usage.get("five_hour")
        if isinstance(five, dict) and isinstance(five.get("pct"), (int, float)):
            place(2, five)

        seven = _rolled_weekly_window(usage.get("seven_day"), now)
        if isinstance(seven, dict) and isinstance(seven.get("pct"), (int, float)):
            pace_result = pace.compute_pace(seven, fetched_at=fetched_at)
            place(3, seven, ahead=bool(pace_result and pace_result.ahead))

        scoped = _scoped_windows(usage, now)
        for offset, model in enumerate(model_names):
            window = scoped.get(model)
            if window is None:
                continue
            pace_result = pace.compute_pace(window, fetched_at=fetched_at)
            place(4 + offset, window, ahead=bool(pace_result and pace_result.ahead))

        spend = usage.get("spend")
        if isinstance(spend, dict) and isinstance(spend.get("pct"), (int, float)):
            index = 4 + len(model_names)
            if index < width and columns[index].title == SPEND_HEADER:
                # Spend is a budget, not a rate-limit window: it never blocks a
                # request, so it carries neither tint nor at-limit marker.
                place(index, spend, tint=False, marker=False)

        rows.append(TableRow(tuple(cells), tuple(resets), tuple(severities),
                             tuple(values)))

    return rows


def build_focus_table(accounts, now: float | None = None) -> list[TableRow]:
    """Rows that lead with each account's *binding* window.

    Answers "which account can I use right now" directly: the window closest to
    its limit is what actually blocks you, so it comes first and carries the
    tint; everything else trails behind it as dimmed context.
    """
    if now is None:
        now = time.time()
    model_names = account_table_model_names(accounts, now)
    rows: list[TableRow] = []

    for num, email, _is_active, usage, _last_good, alias, disabled, fetched_at in accounts:
        name = f"{alias} ({email})" if alias else truncate_name(email)
        if disabled:
            name += "  (disabled)"
        if not isinstance(usage, dict):
            message = usage if isinstance(usage, str) else "usage unavailable"
            rows.append(TableRow((str(num), f"{name} — {message}", "", ""),
                                 ("", "", "", ""), (None,) * 4, (None,) * 4))
            continue

        windows: list[tuple[str, dict]] = []
        five = usage.get("five_hour")
        if isinstance(five, dict) and isinstance(five.get("pct"), (int, float)):
            windows.append(("5h", five))
        seven = _rolled_weekly_window(usage.get("seven_day"), now)
        if isinstance(seven, dict) and isinstance(seven.get("pct"), (int, float)):
            windows.append(("7d", seven))
        scoped = _scoped_windows(usage, now)
        for model in model_names:
            if model in scoped:
                windows.append((model, scoped[model]))

        if not windows:
            rows.append(TableRow((str(num), f"{name} — usage unavailable", "", ""),
                                 ("", "", "", ""), (None,) * 4, (None,) * 4))
            continue

        # Spend is excluded from the binding choice — it caps cost, not requests.
        label, window = max(windows, key=lambda item: item[1]["pct"])
        binding = f"{label} {_pct_text(window)}"
        rest = " · ".join(
            f"{other_label} {other['pct']:.0f}%"
            for other_label, other in windows
            if other_label != label
        )
        spend = usage.get("spend")
        if isinstance(spend, dict) and isinstance(spend.get("pct"), (int, float)):
            rest = f"{rest} · $ {spend['pct']:.0f}%" if rest else f"$ {spend['pct']:.0f}%"
        countdown = _live_countdown(window, now) or ""
        rows.append(TableRow(
            (str(num), name, binding, rest),
            ("", "", countdown, ""),
            (None, None, severity_for(window["pct"]), None),
            (None, None, float(window["pct"]), None),
        ))

    return rows


def focus_table_columns() -> tuple[TableColumn, ...]:
    """Column grid for the focus style: number, name, binding window, the rest."""
    return (
        TableColumn("num", "", False),
        TableColumn("name", "", False),
        TableColumn("window", "", False),
        TableColumn("rest", "", False),
    )


# ---- attributed-string layout (needs AppKit; passed in, never imported here) --

def _bar_image(AppKit, pct: float, font, fill_color, track_color):
    """A small rounded progress bar, drawn to fit one line of ``font``.

    Drawn rather than spelled with block glyphs (▁ █ ▌): those have no coverage
    in the menu font, so they fall back to whatever face does have them and come
    out at inconsistent widths — and ▁ renders as a hairline on the baseline, so
    an empty bar reads as an underscore. Sizing from the font's point size keeps
    the bar proportional at any system font size.
    """
    point = font.pointSize()
    height = max(4.0, round(point * 0.42))
    width = round(point * 4.6)
    image = AppKit.NSImage.alloc().initWithSize_((width, height))
    image.lockFocus()
    radius = height / 2.0
    track_color.setFill()
    AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        ((0, 0), (width, height)), radius, radius).fill()
    filled = max(0.0, min(100.0, pct)) / 100.0 * width
    if filled > 0:
        # Never thinner than the cap radius, or a few-percent bar disappears.
        filled = max(filled, height)
        fill_color.setFill()
        AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            ((0, 0), (filled, height)), radius, radius).fill()
    image.unlockFocus()
    return image


def _bar_attachment(AppKit, image, font):
    """The bar image as an attributed string, sitting on the text baseline."""
    attachment = AppKit.NSTextAttachment.alloc().init()
    cell = AppKit.NSTextAttachmentCell.alloc().initImageCell_(image)
    attachment.setAttachmentCell_(cell)
    return AppKit.NSAttributedString.attributedStringWithAttachment_(attachment)


def build_attributed_rows(
    AppKit,
    rows: list[TableRow],
    columns: tuple[TableColumn, ...],
    header_cells: tuple[str, ...] = (),
    *,
    with_resets: bool = False,
    bars: bool = False,
    font_size: float | None = None,
):
    """Lay the rows out as attributed strings. Returns ``(titles, header)``.

    AppKit does the alignment: one shared ``NSParagraphStyle`` whose tab stops
    are measured from the widest cell per column, so columns line up under the
    proportional menu font instead of needing a monospaced one. Every measure is
    taken in the font the text is actually drawn in and every gap is expressed
    in that font's own space width, so the whole grid scales with the system
    font size instead of assuming one screen's metrics.

    ``AppKit`` is a parameter rather than an import: this module stays
    import-safe (and unit-testable) without pyobjc, exactly like the rumps glue.
    """
    # ``font_size`` exists so the layout can be rendered at sizes other than
    # this machine's current one (previews, review at accessibility sizes).
    size = AppKit.NSFont.systemFontSize() if font_size is None else font_size
    small_size = (
        AppKit.NSFont.smallSystemFontSize() if font_size is None else max(9.0, font_size - 2)
    )
    font = AppKit.NSFont.monospacedDigitSystemFontOfSize_weight_(
        size, AppKit.NSFontWeightRegular
    )
    small_font = AppKit.NSFont.monospacedDigitSystemFontOfSize_weight_(
        small_size, AppKit.NSFontWeightRegular
    )
    colors = {
        "warning": AppKit.NSColor.systemOrangeColor(),
        "critical": AppKit.NSColor.systemRedColor(),
    }

    def width(text, f):
        if not text:
            return 0.0
        return AppKit.NSAttributedString.alloc().initWithString_attributes_(
            text, {AppKit.NSFontAttributeName: f}
        ).size().width

    # A bar is drawn art, not text: reserve its width (plus a space) on top of
    # the percentage it precedes.
    bar_width = (
        _bar_image(AppKit, 0.0, font,
                   AppKit.NSColor.labelColor(),
                   AppKit.NSColor.quaternaryLabelColor()).size().width + width(" ", font)
        if bars else 0.0
    )

    window_indexes = [i for i, c in enumerate(columns) if c.key == "window"]
    if bars and window_indexes:
        # Each window cell is right-aligned on its tab stop, so a wider
        # percentage ("37%" vs "0%") would push its bar further left and leave
        # the bars' left edges ragged. Pad the numbers to a common width with
        # FIGURE SPACE, which is exactly one digit wide in this font.
        pad_to = max(
            (len(row.cells[i]) for row in rows for i in window_indexes
             if row.values[i] is not None),
            default=0,
        )
        rows = [
            replace(row, cells=tuple(
                cell.rjust(pad_to, "\u2007")
                if (i in window_indexes and row.values[i] is not None) else cell
                for i, cell in enumerate(row.cells)
            ))
            for row in rows
        ]

    widths = []
    for index in range(len(columns)):
        widest = width(header_cells[index], small_font) if header_cells else 0.0
        extra = bar_width if (bars and columns[index].key == "window") else 0.0
        for row in rows:
            cell = width(row.cells[index], font)
            widest = max(widest, cell + (extra if row.values[index] is not None else 0.0))
            if with_resets:
                widest = max(widest, width(row.resets[index], small_font))
        widths.append(widest)
    if with_resets and len(widths) > 1:
        widths[1] = max(widths[1], width(RESETS_CAPTION, small_font))

    # Give every window column the same width — an even rhythm reads far better
    # than columns that each hug their own widest value, and it keeps a "0%"
    # column from collapsing next to a "100%!" one.
    if window_indexes and header_cells:
        uniform = max(widths[i] for i in window_indexes)
        for i in window_indexes:
            widths[i] = uniform

    gap = width("  ", font)            # after the slot number, and in focus rows
    column_gap = width("     ", font)  # between window columns: room to breathe
    paragraph = AppKit.NSMutableParagraphStyle.alloc().init()
    stops = []
    x = widths[0] + gap  # the slot number leads the row, before the first tab
    for index in range(1, len(columns)):
        if columns[index].right_aligned:
            x += widths[index]
            stops.append(AppKit.NSTextTab.alloc().initWithTextAlignment_location_options_(
                AppKit.NSTextAlignmentRight, x, {}))
        else:
            stops.append(AppKit.NSTextTab.alloc().initWithTextAlignment_location_options_(
                AppKit.NSTextAlignmentLeft, x, {}))
            x += widths[index]
        x += column_gap if columns[index].key == "window" else gap
    paragraph.setTabStops_(stops)
    # Without this, any text past the last stop falls back to the default 28pt
    # interval and the grid breaks.
    paragraph.setDefaultTabInterval_(gap)

    def line(cells, f, color=None):
        attributes = {
            AppKit.NSFontAttributeName: f,
            AppKit.NSParagraphStyleAttributeName: paragraph,
        }
        if color is not None:
            attributes[AppKit.NSForegroundColorAttributeName] = color
        return AppKit.NSMutableAttributedString.alloc().initWithString_attributes_(
            "\t".join(cells).rstrip("\t"), attributes)

    titles = []
    for row in rows:
        attributed = line(row.cells, font)
        # Ranges are located by walking the joined string, so a cell's own text
        # length (bar glyphs, markers) can't shift the offsets.
        # Cell start offsets in the joined string, walked forward once. Trailing
        # empty cells are stripped from the string, but that only shortens the
        # tail — every non-empty cell keeps the offset computed here.
        offsets = []
        offset = 0
        for cell in row.cells:
            offsets.append(offset)
            offset += len(cell) + 1  # + the tab that follows it
        for index, cell in enumerate(row.cells):
            if cell and row.severities[index] in colors:
                attributed.addAttribute_value_range_(
                    AppKit.NSForegroundColorAttributeName,
                    colors[row.severities[index]], (offsets[index], len(cell)))
        if bars:
            # Insert back-to-front so the earlier offsets stay valid.
            for index in reversed(range(len(row.cells))):
                value = row.values[index]
                if value is None or columns[index].key != "window":
                    continue
                severity = row.severities[index]
                fill = colors.get(severity, AppKit.NSColor.secondaryLabelColor())
                bar = AppKit.NSMutableAttributedString.alloc().initWithAttributedString_(
                    _bar_attachment(
                        AppKit,
                        _bar_image(AppKit, value, font, fill,
                                   AppKit.NSColor.quaternaryLabelColor()),
                        font,
                    )
                )
                bar.appendAttributedString_(
                    AppKit.NSAttributedString.alloc().initWithString_attributes_(
                        " ", {AppKit.NSFontAttributeName: font}))
                attributed.insertAttributedString_atIndex_(bar, offsets[index])
        if not header_cells and len(row.cells) == 4 and row.cells[3]:
            # Focus style: the trailing context recedes so the binding window,
            # which is the whole point of the row, carries the eye.
            start = len(row.cells[0]) + 1 + len(row.cells[1]) + 1 + len(row.cells[2]) + 1
            attributed.addAttribute_value_range_(
                AppKit.NSForegroundColorAttributeName,
                AppKit.NSColor.secondaryLabelColor(),
                (start, len(row.cells[3])))
        if with_resets and any(row.resets):
            resets = list(row.resets)
            resets[1] = RESETS_CAPTION
            attributed.appendAttributedString_(
                AppKit.NSAttributedString.alloc().initWithString_attributes_(
                    "\n", {AppKit.NSParagraphStyleAttributeName: paragraph}))
            attributed.appendAttributedString_(
                line(resets, small_font, AppKit.NSColor.secondaryLabelColor()))
        titles.append(attributed)

    header = None
    if header_cells and any(header_cells):
        header = line(header_cells, small_font, AppKit.NSColor.secondaryLabelColor())
    return titles, header


def _local_part(email: str, limit: int = 12) -> str:
    """Email text before '@', truncated with a trailing '*' marker."""
    local = email.split("@", 1)[0]
    if len(local) > limit:
        return local[: limit - 1] + "*"
    return local


def format_title(
    active_email: str | None,
    active_usage: dict | str | None,
    settings: MenuBarSettings,
    now: float | None = None,
    alias: str | None = None,
) -> str:
    """Build the menu-bar title from the active account and settings."""
    if active_email is None:
        return ICON
    if now is None:
        now = time.time()
    segments: list[str] = []
    if settings.show_account_name:
        segments.append(alias if alias else _local_part(active_email))
    if settings.title_pct in ("5h", "both"):
        p = _window_pct(active_usage, "five_hour")
        if p is not None:
            segments.append(f"{p:.0f}%")
    if settings.title_pct in ("7d", "both"):
        seven = active_usage.get("seven_day") if isinstance(active_usage, dict) else None
        seven = _rolled_weekly_window(seven, now)  # reflect a passed weekly reset
        p = seven["pct"] if isinstance(seven, dict) and isinstance(seven.get("pct"), (int, float)) else None
        if p is not None:
            segments.append(f"{p:.0f}%")
    if settings.title_scoped and isinstance(active_usage, dict):
        # Per-model weekly limits (e.g. Fable), same shape/roll-forward as the
        # dropdown rows; named so multiple scoped models stay distinguishable.
        for window in active_usage.get("scoped") or []:
            window = _rolled_weekly_window(window, now)
            if isinstance(window, dict) and isinstance(window.get("pct"), (int, float)) and window.get("name"):
                segments.append(f"{window['name']} {window['pct']:.0f}%")
    if not segments:
        return ICON
    return f"{ICON} " + " · ".join(segments)


def format_usage_log(email: str, usage: dict | str | None) -> str | None:
    """A log line of an account's session (5h) and weekly (7d) limits.

    Uses each window's absolute reset ``clock`` rather than a live countdown,
    since log lines are already timestamped. Returns ``None`` when no numeric
    window is available (sentinels, ``None``, or spend-only) so callers can skip
    logging nothing.
    """
    parts: list[str] = []
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        pct = _window_pct(usage, key)
        if pct is None:
            continue
        window = usage.get(key)  # a dict — _window_pct found a numeric pct in it
        clock = window.get("clock") if isinstance(window, dict) else None
        seg = f"{label} {pct:.0f}%"
        if clock:
            seg += f" (resets {clock})"
        parts.append(seg)
    if not parts:
        return None
    return f"usage {email}: " + " · ".join(parts)


def _usage_log_key(usage: dict | str | None) -> tuple[float | None, float | None]:
    """De-dupe key for usage logging: the (5h, 7d) percentages only.

    Reset clocks change every refresh; keying on the percentages means an idle
    account isn't re-logged every cycle.
    """
    return (_window_pct(usage, "five_hour"), _window_pct(usage, "seven_day"))


_SWITCH_LOG_RE = re.compile(r"Switched from account (\d+) to (\d+)")


def parse_switch_history(log_text: str, limit: int = SWITCH_HISTORY_LIMIT) -> list[str]:
    """Recent account switches from the log, most-recent first.

    Reads the ``Switched from account X to Y`` lines the switcher logs and pairs
    each with its timestamp (trimmed to the minute). Returns at most ``limit``
    entries like ``"3 → 1   2026-06-27 02:06"``. Any unparseable line is skipped.
    """
    out: list[str] = []
    for line in log_text.splitlines():
        m = _SWITCH_LOG_RE.search(line)
        if not m:
            continue
        stamp = line.split(" - ", 1)[0].strip()[:16]  # "YYYY-MM-DD HH:MM"
        out.append(f"{m.group(1)} → {m.group(2)}   {stamp}")
    return out[-limit:][::-1]


def _account_display_usage(entry) -> dict | str | None:
    """Menu-display usage for a ``UsageEntry``.

    A human-readable note for a sentinel state (token expired / API key /
    keychain unavailable), otherwise the last-good measurement dict, otherwise
    ``None``.
    """
    if entry.sentinel:
        return SENTINEL_NOTES.get(entry.sentinel, entry.sentinel)
    return entry.last_good


EMPTY_SNAPSHOT: dict = {
    "accounts": [],
    "active_email": None,
    "active_usage": None,
    "active_alias": None,
}


def _adapt_snapshot(snap) -> dict:
    """Adapt an ``AccountsSnapshot`` to the menu bar's render dict.

    Shape: ``{"accounts": [(num, email, is_active, display_usage, last_good, alias, disabled, fetched_at), ...],
    "active_email": str | None, "active_usage": dict | str | None,
    "active_alias": str | None}``. The snapshot itself is produced by
    ``SnapshotSource`` (the paced read path), so this is a pure transform — no
    fetching, no I/O. Per-account ``fetched_at`` is the underlying
    measurement's fetch time, used only for the pace marker (issue #125).
    """
    accounts = []
    active_email = None
    active_usage = None
    active_alias = None
    for acc in snap.accounts:
        display = _account_display_usage(acc.usage)
        accounts.append(
            (
                acc.number, acc.email, acc.is_active, display, acc.usage.last_good,
                acc.alias, acc.disabled, acc.usage.fetched_at,
            )
        )
        if acc.is_active:
            active_email, active_usage, active_alias = acc.email, display, acc.alias
    return {
        "accounts": accounts,
        "active_email": active_email,
        "active_usage": active_usage,
        "active_alias": active_alias,
    }


def run(switcher) -> int:
    """Entry point for ``cswap --menubar``. Blocks until the user quits."""
    ensure_notification_identity()
    try:
        import rumps  # lazy: optional dependency, imported only when launching
        import AppKit  # ships with rumps (pyobjc-framework-Cocoa), never fails alone
    except ImportError as e:
        # This module is import-safe without rumps by design, so the CLI's
        # guard around ``from claude_swap.menubar import run`` can never see a
        # missing extra — the failure lands here at call time. Raise the
        # error type the CLI already renders cleanly instead of a traceback.
        raise ClaudeSwitchError(
            "Menu bar mode requires 'rumps'. "
            "Install with: pip install 'claude-swap[menubar]'"
        ) from e

    # rumps never sets an activation policy, so under a framework Python the
    # process launches as a regular app and parks a "Python" icon in the Dock
    # for as long as the menu bar runs. Accessory keeps the status item and
    # dialog windows but stays out of the Dock and the Cmd-Tab switcher.
    AppKit.NSApplication.sharedApplication().setActivationPolicy_(
        AppKit.NSApplicationActivationPolicyAccessory
    )

    from claude_swap.autoswitch import AutoSwitchEngine
    from claude_swap.settings import load_settings, set_setting
    from claude_swap.snapshot_source import SnapshotSource

    settings_path = switcher.backup_dir / "menubar_settings.json"
    log_path = switcher.backup_dir / "claude-swap.log"

    class MenuBarApp(rumps.App):
        def __init__(self):
            super().__init__(ICON, quit_button=None)
            self.switcher = switcher
            self.settings = MenuBarSettings.load(settings_path)
            # The supported paced read path: per refresh it fetches only the
            # active account plus (at most once per freshness window) one stale
            # alternate, so an open menu costs O(1) requests per window instead
            # of a full pass per tick — which kept every token at its per-account
            # rate-limit edge. Reused across refreshes to hold its pacing state.
            self._snapshot_source = SnapshotSource(switcher)
            self.snapshot = dict(EMPTY_SNAPSHOT)
            self._dirty = False
            self._snapshot_at = 0.0
            self._refreshing = False
            self._config_path = switcher._get_claude_config_path()
            self._config_mtime = 0.0
            self._last_usage_log: dict = {}  # account num -> last-logged (5h, 7d) key
            # Auto-switch engine (the same one `cswap auto` runs), hosted in a
            # background thread while enabled.
            self._engine = None
            self._engine_events: list = []
            self._event_lock = threading.Lock()
            self.rebuild_menu()
            # Background display refresh on the user's interval, plus a fast
            # UI-sync tick that applies snapshots + engine events on the main thread.
            self.refresh_timer = rumps.Timer(self.on_refresh_tick, self.settings.refresh_interval)
            self.refresh_timer.start()
            self.sync_timer = rumps.Timer(self.on_sync_tick, 1)
            self.sync_timer.start()
            self.refresh_async()  # first display fetch
            if self.settings.auto_switch_enabled:
                self._start_engine()

        # ---- display refresh plumbing ----------------------------------------
        def refresh_async(self, full=False):
            if self._refreshing:
                return  # in-flight guard: one worker at a time (SnapshotSource
                        # pacing state is only touched by this single worker)
            self._refreshing = True
            threading.Thread(target=self._worker, args=(full,), daemon=True).start()

        def _worker(self, full):
            # Lock-free handoff: worker only rebinds plain attributes (atomic in
            # CPython); the main-thread sync tick reads them. While the engine
            # runs it already paces all fetching, so the display reads store-only.
            try:
                try:
                    raw = self._snapshot_source.take(
                        full=full, store_only=self._engine is not None
                    )
                except Exception:
                    # Keep the last good snapshot rather than blanking the menu.
                    self.switcher._logger.debug("menubar snapshot failed", exc_info=True)
                    return
                snap = _adapt_snapshot(raw)
                self._log_usage(snap)
                self.snapshot = snap
                self._snapshot_at = time.time()
                self._dirty = True  # picked up by on_sync_tick on the main thread
            finally:
                self._refreshing = False

        def _log_usage(self, snap):
            """Log each account's session/weekly limits when they change.

            Runs on every refresh (background thread; the logger is thread-safe)
            but de-dupes per account on the (5h, 7d) percentages so an idle
            machine doesn't churn the rotating log with identical lines.
            """
            for num, email, _is_active, _display, last_good, _alias, _disabled, _fetched_at in snap["accounts"]:
                key = _usage_log_key(last_good)
                if key == (None, None) or self._last_usage_log.get(num) == key:
                    continue
                line = format_usage_log(email, last_good)
                if line:
                    self.switcher._logger.info(line)
                    self._last_usage_log[num] = key

        def on_refresh_tick(self, _timer):
            self.refresh_async()

        def on_sync_tick(self, _timer):
            if self._dirty:
                self._dirty = False
                self.rebuild_menu()
            self._detect_active_change()
            self._drain_engine_events()

        def _detect_active_change(self):
            # Reflect account switches from any source (menu, CLI, auto engine)
            # within ~1s. Detecting *which* account is active is a cheap local
            # read of ~/.claude.json -- no Keychain or usage API -- so we can do
            # it on every tick. We gate the read on the file's mtime (a cheap
            # stat) so a large config isn't parsed each second, and only kick a
            # refresh when the active email actually changed (Claude Code rewrites
            # this file often for unrelated reasons).
            if self._refreshing:
                return  # a worker is already in-flight; it refreshes the marker
            try:
                mtime = self._config_path.stat().st_mtime
            except OSError:
                return
            if mtime == self._config_mtime:
                return
            self._config_mtime = mtime
            current = self.switcher._get_current_account()
            email = current[0] if current else None
            if email and email != self.snapshot.get("active_email"):
                self.refresh_async()

        # ---- auto-switch engine ----------------------------------------------
        def _start_engine(self):
            """Run the core AutoSwitchEngine (live) in a background thread."""
            if self._engine is not None:
                return
            try:
                engine = AutoSwitchEngine(
                    self.switcher,
                    load_settings(self.switcher.backup_dir),
                    self._on_engine_event,
                    dry_run=False,
                )
            except Exception as e:  # never let a bad start crash the menu bar
                self.switcher._logger.warning("auto-switch engine failed to start: %s", e)
                rumps.notification("claude-swap", "Auto-switch failed to start", str(e))
                return
            self._engine = engine
            threading.Thread(target=self._run_engine, args=(engine,), daemon=True).start()

        def _run_engine(self, engine):
            try:
                engine.run_loop()
            except Exception:
                self.switcher._logger.debug("auto-switch engine crashed", exc_info=True)

        def _stop_engine(self):
            if self._engine is not None:
                self._engine.stop()
                self._engine = None

        def _restart_engine(self):
            """Apply changed core settings by restarting the running engine."""
            if self._engine is not None:
                self._stop_engine()
                self._start_engine()

        def _on_engine_event(self, event):
            # Runs on the engine thread; must not raise. Queue for the main
            # thread, which surfaces notifications and reacts on the sync tick.
            with self._event_lock:
                self._engine_events.append(event)

        def _drain_engine_events(self):
            with self._event_lock:
                events, self._engine_events = self._engine_events, []
            for ev in events:
                if ev.kind == "switch" and not getattr(ev, "dry_run", False):
                    rumps.notification("claude-swap", "Auto-switched account", ev.human())
                    self.refresh_async()  # reflect the switch promptly
                elif ev.kind == "account-quarantined":
                    rumps.notification("claude-swap", "Account quarantined", ev.human())
                elif ev.kind == "all-exhausted":
                    rumps.notification("claude-swap", "All accounts exhausted", ev.human())
                elif ev.kind == "config-warning":
                    # e.g. an autoswitch.model name no account reports — the
                    # engine emits it once per run; dropping it would leave a
                    # menu-bar user with a silently inert filter.
                    rumps.notification("claude-swap", "Configuration warning", ev.human())

        def _threshold(self) -> int:
            """Current auto-switch threshold from core settings (for the menu)."""
            try:
                return int(load_settings(self.switcher.backup_dir).threshold)
            except Exception:
                return 0

        # ---- menu construction -----------------------------------------------
        def rebuild_menu(self):
            self.title = format_title(
                self.snapshot["active_email"],
                self.snapshot["active_usage"],
                self.settings,
                alias=self.snapshot.get("active_alias"),
            )
            self.menu.clear()
            accounts = self.snapshot["accounts"]
            aligned, header = self._aligned_titles(accounts)
            account_items = []
            if header is not None:
                # A disabled item: it names the columns, it isn't a target.
                head = rumps.MenuItem("  ", callback=None)
                head._menuitem.setAttributedTitle_(header)
                account_items.append(head)
            for index, (num, email, is_active, display, _last_good, alias, disabled, fetched_at) in enumerate(accounts):
                item = rumps.MenuItem(
                    format_account_label(
                        num, email, display, alias=alias, disabled=disabled, fetched_at=fetched_at
                    ),
                    callback=self._make_switch_to(num),
                )
                # The plain label above stays the item's rumps key (unique per
                # account); the attributed title only changes what's drawn.
                if aligned is not None:
                    item._menuitem.setAttributedTitle_(aligned[index])
                item.state = 1 if is_active else 0
                account_items.append(item)
            if not accounts:
                account_items = [rumps.MenuItem("No managed accounts", callback=None)]

            self.menu = [
                *account_items,
                None,
                rumps.MenuItem("Rotate to next", callback=self._switch(None)),
                rumps.MenuItem("Switch to best", callback=self._switch("best")),
                rumps.MenuItem("Next available", callback=self._switch("next-available")),
                None,
                self._add_menu(rumps),
                self._disable_menu(rumps),
                self._remove_menu(rumps),
                rumps.MenuItem("Refresh current credentials", callback=self.on_refresh_creds),
                self._history_menu(rumps),
                None,
                self._settings_menu(rumps),
                rumps.MenuItem("Refresh now", callback=self.on_refresh_now),
                rumps.MenuItem("Quit", callback=self.on_quit),
            ]

        def _aligned_titles(self, accounts):
            """Attributed titles for the account rows, plus an optional header.

            Returns ``(titles, header)``; both ``None`` for the compact style,
            which stays plain text. The layout itself lives in
            :func:`build_attributed_rows` so it can be rendered — and reviewed —
            without a running menu.
            """
            style_name = self.settings.row_style
            if style_name not in GRID_ROW_STYLES + ("focus",) or not accounts:
                return None, None
            now = time.time()
            if style_name == "focus":
                columns = focus_table_columns()
                rows = build_focus_table(accounts, now)
                header_cells = ()
            else:
                columns = account_table_columns(accounts, now)
                rows = build_account_table(accounts, now)
                header_cells = account_table_header(columns)
            return build_attributed_rows(
                AppKit, rows, columns, header_cells,
                with_resets=self.settings.show_resets and style_name in GRID_ROW_STYLES,
                bars=style_name == "bars",
            )

        def _add_menu(self, rumps):
            menu = rumps.MenuItem("Add account")
            menu.add(rumps.MenuItem("From current login", callback=self.on_add_login))
            if hasattr(self.switcher, "add_account_from_token"):
                menu.add(rumps.MenuItem("From setup-token…", callback=self.on_add_token))
            return menu

        def _remove_menu(self, rumps):
            menu = rumps.MenuItem("Remove account")
            accounts = self.snapshot["accounts"]
            if not accounts:
                menu.add(rumps.MenuItem("No managed accounts", callback=None))
            for num, email, _is_active, _display, _last_good, alias, _disabled, _fetched_at in accounts:
                label = f"{num}  {alias}  ({email})" if alias else f"{num}  {email}"
                menu.add(rumps.MenuItem(label, callback=self._make_remove(num)))
            return menu

        def _disable_menu(self, rumps):
            menu = rumps.MenuItem("Disable / enable account")
            accounts = self.snapshot["accounts"]
            if not accounts:
                menu.add(rumps.MenuItem("No managed accounts", callback=None))
            for num, email, _is_active, _display, _last_good, alias, disabled, _fetched_at in accounts:
                name = f"{alias}  ({email})" if alias else email
                item = rumps.MenuItem(
                    f"{num}  {name}", callback=self._make_toggle_disabled(num, disabled)
                )
                # A check-mark reads as "held out of rotation" — same glyph the
                # active row uses, but here it means disabled, not selected.
                item.state = 1 if disabled else 0
                menu.add(item)
            return menu

        def _history_menu(self, rumps):
            menu = rumps.MenuItem("Switch history")
            try:
                text = log_path.read_text(encoding="utf-8")
            except OSError:
                text = ""
            entries = parse_switch_history(text)
            if entries:
                for line in entries:
                    menu.add(rumps.MenuItem(line, callback=None))
            else:
                menu.add(rumps.MenuItem("No switches logged yet", callback=None))
            menu.add(None)
            menu.add(rumps.MenuItem("Open full log…", callback=self.on_open_log))
            return menu

        def _settings_menu(self, rumps):
            menu = rumps.MenuItem("Settings")

            rows = rumps.MenuItem("Account rows")
            row_labels = {
                "compact": "Compact (one line)",
                "columns": "Columns",
                "bars": "Columns with bars",
                "focus": "Binding limit first",
            }
            for style in ROW_STYLE_CHOICES:
                ch = rumps.MenuItem(row_labels[style], callback=self._make_row_style(style))
                ch.state = 1 if self.settings.row_style == style else 0
                rows.add(ch)
            rows.add(None)
            resets_item = rumps.MenuItem("Show reset times", callback=self.on_toggle_resets)
            resets_item.state = 1 if self.settings.show_resets else 0
            rows.add(resets_item)
            menu.add(rows)

            name_item = rumps.MenuItem("Show account name in menu bar", callback=self.on_toggle_name)
            name_item.state = 1 if self.settings.show_account_name else 0
            menu.add(name_item)

            title_pct = rumps.MenuItem("Title percentage")
            tp_labels = {"off": "None", "5h": "Session (5h)",
                         "7d": "Weekly (7d)", "both": "Both (5h · 7d)"}
            for mode in TITLE_PCT_CHOICES:
                ch = rumps.MenuItem(tp_labels[mode], callback=self._make_title_pct(mode))
                ch.state = 1 if self.settings.title_pct == mode else 0
                title_pct.add(ch)
            menu.add(title_pct)

            scoped_item = rumps.MenuItem(
                "Show model limits in title", callback=self.on_toggle_scoped
            )
            scoped_item.state = 1 if self.settings.title_scoped else 0
            menu.add(scoped_item)

            interval = rumps.MenuItem("Refresh interval")
            labels = {30: "30 seconds", 60: "60 seconds", 300: "5 minutes"}
            for secs in REFRESH_CHOICES:
                choice = rumps.MenuItem(labels[secs], callback=self._make_interval(secs))
                choice.state = 1 if self.settings.refresh_interval == secs else 0
                interval.add(choice)
            menu.add(interval)

            auto_item = rumps.MenuItem("Auto-switch accounts", callback=self.on_toggle_autoswitch)
            auto_item.state = 1 if self.settings.auto_switch_enabled else 0
            menu.add(auto_item)

            threshold_menu = rumps.MenuItem("Auto-switch threshold")
            current = self._threshold()
            for pct in AUTO_THRESHOLD_CHOICES:
                ch = rumps.MenuItem(f"{pct}%", callback=self._make_threshold(pct))
                ch.state = 1 if current == pct else 0
                threshold_menu.add(ch)
            menu.add(threshold_menu)

            return menu

        # ---- callbacks --------------------------------------------------------
        def _save_and_rebuild(self):
            self.settings.save(settings_path)
            self.rebuild_menu()

        def _guard(self, fn):
            """Run a switcher action, surfacing ClaudeSwitchError via an alert."""
            try:
                fn()
                return True
            except ClaudeSwitchError as e:
                rumps.alert(title="claude-swap", message=str(e))
                return False

        def _notify_switched(self):
            rumps.notification(
                "claude-swap",
                "Account switched",
                "Switch takes effect within ~30s — restart Claude Code to apply immediately.",
            )

        def _make_switch_to(self, num):
            def cb(_sender):
                if self._guard(lambda: self.switcher.switch_to(str(num))):
                    self._notify_switched()
                    self.refresh_async()
            return cb

        def _switch(self, strategy):
            def cb(_sender):
                if self._guard(lambda: self.switcher.switch(strategy=strategy)):
                    self._notify_switched()
                    self.refresh_async()
            return cb

        def _make_remove(self, num):
            def cb(_sender):
                if rumps.alert(
                    title="Remove account",
                    message=f"Remove account {num}?",
                    ok="Remove",
                    cancel="Cancel",
                ) == 1:  # 1 == OK
                    if self._guard(lambda: self.switcher.remove_account(str(num), assume_yes=True)):
                        self.refresh_async()
            return cb

        def _make_toggle_disabled(self, num, disabled):
            # `disabled` is this row's current state; selecting it flips it.
            target = not disabled
            def cb(_sender):
                if self._guard(
                    lambda: self.switcher.set_account_disabled(str(num), target)
                ):
                    self.refresh_async()
            return cb

        def on_add_login(self, _sender):
            if self._guard(self.switcher.add_account):
                self.refresh_async()

        def on_add_token(self, _sender):
            # A menu-bar (accessory) app isn't the active app, so a modal
            # rumps.Window can render black/blank until we bring the app
            # forward. Activate before showing the input dialogs.
            import AppKit
            AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            email_win = rumps.Window(
                title="Add account from setup-token",
                message="Email for this token:",
                ok="Next", cancel="Cancel", dimensions=(320, 24),
            )
            email_resp = email_win.run()
            if email_resp.clicked != 1 or not email_resp.text.strip():
                return
            token_win = rumps.Window(
                title="Add account from setup-token",
                message="Setup token (sk-ant-oat01-…):",
                ok="Add", cancel="Cancel", dimensions=(320, 24),
            )
            token_resp = token_win.run()
            if token_resp.clicked != 1 or not token_resp.text.strip():
                return
            if self._guard(lambda: self.switcher.add_account_from_token(
                token=token_resp.text.strip(), email=email_resp.text.strip(), slot=None,
            )):
                self.refresh_async()

        def on_open_log(self, _sender):
            import subprocess
            # Reveal the log in Finder (-R); if it doesn't exist yet, open the dir.
            target = log_path if log_path.exists() else log_path.parent
            subprocess.run(["open", "-R", str(target)], check=False)

        def on_refresh_creds(self, _sender):
            if self.switcher._get_current_account() is None:
                rumps.alert(title="claude-swap",
                            message="No active Claude Code login detected. Log in first.")
                return
            try:
                self.switcher.add_account(slot=None)
            except CredentialReadError:
                # Almost always a launchd/login-agent Keychain block: the active
                # credential lives in the macOS Keychain, which a background agent
                # can't read (the security call times out). Point at the fix.
                rumps.alert(
                    title="claude-swap",
                    message="Couldn't read the active credential. If the menu bar is running "
                            "as a background/login agent, macOS blocks its Keychain access — "
                            "quit and relaunch it from a Terminal with: cswap --menubar",
                )
                return
            except ClaudeSwitchError as e:
                rumps.alert(title="claude-swap", message=str(e))
                return
            self.refresh_async()

        def on_refresh_now(self, _sender):
            self.refresh_async(full=True)  # explicit user refresh → full pass

        def on_quit(self, _sender):
            self._stop_engine()
            rumps.quit_application()

        def on_toggle_name(self, _sender):
            self.settings.show_account_name = not self.settings.show_account_name
            self._save_and_rebuild()

        def on_toggle_scoped(self, _sender):
            self.settings.title_scoped = not self.settings.title_scoped
            self._save_and_rebuild()

        def on_toggle_resets(self, _sender):
            self.settings.show_resets = not self.settings.show_resets
            self._save_and_rebuild()

        def _make_row_style(self, style):
            def cb(_sender):
                self.settings.row_style = style
                self._save_and_rebuild()
            return cb

        def _make_title_pct(self, mode):
            def cb(_sender):
                self.settings.title_pct = mode
                self._save_and_rebuild()
            return cb

        def _make_interval(self, secs):
            def cb(_sender):
                self.settings.refresh_interval = secs
                # rumps 0.4.0's Timer.interval setter is a no-op while running
                # unless a full interval has elapsed; stop/start forces the new
                # cadence to take effect immediately.
                self.refresh_timer.stop()
                self.refresh_timer.interval = secs
                self.refresh_timer.start()
                self._save_and_rebuild()
            return cb

        def on_toggle_autoswitch(self, _sender):
            self.settings.auto_switch_enabled = not self.settings.auto_switch_enabled
            self.settings.save(settings_path)
            if self.settings.auto_switch_enabled:
                self._start_engine()
            else:
                self._stop_engine()
            self.rebuild_menu()

        def _make_threshold(self, pct):
            def cb(_sender):
                try:
                    set_setting(self.switcher.backup_dir, "autoswitch.threshold", str(pct))
                except Exception as e:
                    rumps.alert(title="claude-swap", message=f"Couldn't set threshold: {e}")
                    return
                self._restart_engine()  # apply immediately if running
                self.rebuild_menu()
            return cb

    MenuBarApp().run()
    return 0
