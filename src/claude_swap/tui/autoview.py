"""Live auto-switch screen: the real engine, visualized.

Runs :class:`AutoSwitchEngine` in a thread worker and renders its typed
events. Opens in **dry-run** — opening a view must never start switching
accounts on its own; going live is an explicit, confirmed action. The
engine's own state file semantics (shared cooldown, quarantine list, state
lock) make it safe to run alongside an external ``cswap auto``.

The active account's full card sits on top (same widget as the dashboard's
panel, with the threshold tick); this screen adds the engine badge, the
ranked switch candidates, and the decision log. While it is up, the app's
snapshot poller runs store-only: the engine is the only fetcher.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from rich.cells import cell_len
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, RichLog, Static

from claude_swap import oauth
from claude_swap.autoswitch import (
    CONSUME_FIRST_STRATEGIES,
    AutoSwitchEngine,
    AutoSwitchEvent,
    binding_pct,
    classify_candidate_block,
    consume_first_rank_key,
    pct_label,
)
from claude_swap.json_output import USAGE_API_KEY, USAGE_NO_CREDENTIALS
from claude_swap.lines import BindingWindow, binding_window
from claude_swap.models import AccountsSnapshot
from claude_swap.settings import SETTING_SPECS, load_settings, parse_model_names
from claude_swap.tui import data
from claude_swap.tui.modals import ConfirmModal
from claude_swap.tui.theme import Palette
from claude_swap.tui.widgets import AccountsPanel, login_segment, spend_row, usage_rows

if TYPE_CHECKING:
    from claude_swap.tui.app import CswapApp

_EVENT_ROLES = {
    "switch": "accent",
    "error": "sev_warn",
    "account-quarantined": "sev_warn",
    "all-exhausted": "sev_crit",
}
_QUIET_KINDS = {"poll", "no-switch", "sleep", "account-unquarantined"}


def event_text(event: AutoSwitchEvent, *, palette: Palette = Palette.DARK) -> Text:
    """Log line for one engine event, styled like the CLI's human renderer."""
    role = _EVENT_ROLES.get(event.kind)
    if role == "sev_crit" and getattr(event, "deliberate_wait", False):
        # The map keys on the KIND and this kind carries two states; the
        # critical colour overstates a hold whose gate proves every candidate
        # was READ and one still holds quota.
        role = "sev_warn"
    if role is not None:
        style = getattr(palette, role)
    else:
        style = palette.muted if event.kind in _QUIET_KINDS else palette.foreground
    text = Text()
    text.append(f"{data.clock_stamp()}  ", style=palette.muted)
    text.append(event.human(), style=style)
    return text


_STRATEGY_CYCLE = ("best", "consume-first", "dynamic")


def _line_note(binding: BindingWindow | None, label: str, noted: bool) -> str:
    """The line's pct ("97%") for the "limit 97%" status piece, once a weekly
    line or the account's own threshold makes the lines differ: the chip's raw
    pct stays, and the line it is judged against is named only where it is not
    the one number in the header. Empty unless `label` is the BINDING window's."""
    if not noted or binding is None or label != binding.label:
        return ""
    return f"{pct_label(binding.line)}%"


# Every per-account reason a Next best row is not a plain candidate, in the one
# order they read: the limits reached, the other reasons, auto-swap off. A new
# marker registers by adding its key here; only "block" and "threshold" have a
# producer on this branch.
STATUS_ORDER = (
    "capped", "at-line", "block", "walled", "threshold", "stale", "no-plan",
    "not-candidate", "disabled",
)

EMAIL_FLOOR = 12  # display columns the email slot is cut to, at the least


@dataclass
class CandidateRow:
    """One Next best row as named slots. A producer fills a slot and never
    pads it: `render_candidate_rows` owns every column. `status` and `where`
    render as ONE trailing note, `where` last; the `login` note is the last slot."""

    number: Text
    email: Text
    usage: Text  # the chip block, the spend text, or the sentinel note
    status: dict[str, Text] = field(default_factory=dict)  # keys of STATUS_ORDER
    # The short form of a status piece, for a narrow panel; a key without one
    # keeps its long text.
    short: dict[str, Text] = field(default_factory=dict)
    where: Text = field(default_factory=Text)  # where-in-use markers, joined by " · "
    # The `re-login in 5d 12h` note that ends the row, empty unless the login is
    # due, and its short form for a narrow panel (a row without one keeps the long).
    login: Text = field(default_factory=Text)
    login_short: Text = field(default_factory=Text)
    note: bool = False  # usage is free text (a sentinel note): it sizes no column

    def __post_init__(self) -> None:
        for pieces in (self.status, self.short):
            if unknown := pieces.keys() - set(STATUS_ORDER):
                raise ValueError(f"status key not in STATUS_ORDER: {sorted(unknown)}")
        # A marker never pads itself; usage is exempt, its chip blanks pad on purpose.
        for cell in (self.login, self.login_short, self.where, *self.status.values(), *self.short.values()):
            if cell.plain != cell.plain.strip():
                raise ValueError(
                    f"marker has leading or trailing whitespace: {cell.plain!r}"
                )
        if orphan := self.short.keys() - self.status.keys():
            raise ValueError(f"short form without a status entry: {sorted(orphan)}")


def _cut(cell: Text, width: int) -> Text:
    """`cell` in at most `width` display columns, ending in `…` where it is cut."""
    cell = cell.copy()
    cell.truncate(width, overflow="ellipsis")
    return cell


def render_candidate_rows(
    rows: list[CandidateRow], palette: Palette, width: int | None = None
) -> Text:
    """The block's rows, each led by a newline, every slot starting in one
    display column on all of them. A slot is as wide as its widest cell over
    every row that fills it; a free-text usage note sizes nothing, and a slot
    empty on every row takes no column at all. The status pieces and `where`
    are one note cell, joined by a dot, `where` last; the login note ends the row.

    Over `width`, the block takes ONE form so its columns stay aligned. In
    this order it gives up: the login, to its short form; the email slot, cut
    to a shared width no narrower than EMAIL_FLOOR; the long status texts for
    their short ones; the login, the email taking back the room it frees;
    last, the note itself, cut to the room left. The usage slot is never cut.
    A free-text note row counts in the fit unless it overflows even with no
    login and the email uncut: that one never fits, so it is left out."""
    sep = Text(" \u00b7 ", style=palette.muted)

    def note(row: CandidateRow, short: bool) -> Text:
        pieces = [
            (row.short.get(key) if short else None) or row.status[key]
            for key in STATUS_ORDER if key in row.status
        ]
        if row.where:
            # Each marker's leading glyph (`○ cloud` -> `○`).
            pieces.append(Text(" ").join(
                marker[: len(marker.plain.partition(" ")[0])]
                for marker in row.where.split(" \u00b7 ")
            ) if short else row.where)
        out = Text()
        for piece in pieces:
            if out:
                out.append_text(sep)
            out.append_text(piece)
        return out

    def login_in(row: CandidateRow, login: str | None) -> Text:
        """The row's login in the `login` form: `"full"`, `"short"`, or none."""
        if login == "full":
            return row.login
        return (row.login_short or row.login) if login else Text()

    def lines(
        email: int, short: bool, login: str | None = None, room: int | None = None
    ) -> list[Text]:
        """Every row at an email slot of at most `email` columns, its login in
        the `login` form; with `room`, a note is cut to end by that column."""
        grid = [
            [row.number, _cut(row.email, email), row.usage, note(row, short), login_in(row, login)]
            for row in rows
        ]
        usage, notes = 2, 3
        widths = [2, 0, 0, 0, 0]  # the number is right-aligned, two wide at least
        used = [False] * len(widths)
        for row, cells in zip(rows, grid):
            for i, cell in enumerate(cells):
                used[i] |= bool(cell)
                if not (i == usage and row.note):
                    widths[i] = max(widths[i], cell.cell_len)
        out = []
        for cells in grid:
            line = Text()
            for i, cell in enumerate(cells):
                if i and not used[i]:
                    continue
                if room is not None and i == notes:
                    if (left := room - line.cell_len - 2) < 2:
                        continue
                    cell = _cut(cell, left)
                else:
                    cell = cell.copy()
                # A note wider than usage overflows on its own row; the next slot follows it.
                pad = max(0, widths[i] - cell.cell_len)
                if i:
                    cell.pad_right(pad)
                else:
                    cell.pad_left(pad)
                line.append("  ")  # the row's indent, then the gutter between slots
                line.append_text(cell)
            line.rstrip()
            out.append(line)
        return out

    limit = 10**9 if width is None else width

    widest = max((row.email.cell_len for row in rows), default=0)
    # A free-text note row that overflows without its login is left out of the
    # fit: its text is never cut, so only its login is the fit's to give up.
    fits = [
        not row.note or line.cell_len <= limit
        for row, line in zip(rows, lines(widest, True))
    ]

    def over(block: list[Text]) -> int:
        return max((line.cell_len for ok, line in zip(fits, block) if ok), default=0) - limit

    # (login form, short status texts, email cut): each step keeps what the one before gave up.
    for login, short, cut in (
        ("full", False, False),
        ("short", False, False),
        ("short", False, True),
        ("short", True, True),
        (None, True, True),
    ):
        shown = lines(widest, short, login)
        if cut and over(shown) > 0:
            shown = lines(max(EMAIL_FLOOR, widest - over(shown)), short, login)
        if over(shown) <= 0:
            break
    else:
        shown = lines(EMAIL_FLOOR, True, room=limit)
    out = Text()
    for line in shown:
        out.append("\n")
        out.append_text(line)
    return out


class AutoScreen(Screen):
    BINDINGS = [
        Binding("l", "toggle_live", "Go live / dry-run"),
        Binding("t", "adjust_threshold", "Threshold"),
        Binding("s", "cycle_strategy", "Strategy"),
        Binding("left", "threshold_step(-1)", "-1%"),
        Binding("right", "threshold_step(1)", "+1%"),
        Binding("enter", "adjust_done", "Done"),
        Binding("escape,q", "back", "Back"),
    ]

    app: "CswapApp"

    def __init__(self, *, start_live: bool = False) -> None:
        super().__init__()
        self._engine: AutoSwitchEngine | None = None
        self._settings = None
        # `cswap tui --auto` only. The engine starts LIVE without the modal
        # because the flag IS the consent, for that launch alone.
        self._start_live = start_live
        # Session-only threshold adjustment (t, then arrows). Never written
        # to settings.json — same memory-only precedent as the dry-run
        # toggle. ``_configured_threshold`` is the mount-time file value the
        # screen reverts to on exit; ``_entry_threshold`` is the value when
        # adjust mode was entered (wake/log only on a net change).
        self._adjusting = False
        self._configured_threshold: float | None = None
        self._entry_threshold: float | None = None
        # Session-only strategy override (s cycles best -> consume-first ->
        # dynamic). Same precedent as the threshold above: never written to
        # settings.json. ``_configured_strategy`` is the mount-time file
        # value the screen reverts to on exit.
        self._configured_strategy: str | None = None

    def compose(self) -> ComposeResult:
        yield AccountsPanel(show_minis=False, id="auto-active-panel")
        with Vertical(id="auto-top"):
            with Horizontal(id="auto-title-row"):
                yield Static(" DRY-RUN ", id="mode-badge", classes="dry")
                yield Static("", id="auto-summary")
            yield Static("", id="candidates")
        yield RichLog(id="event-log", highlight=False, markup=False, wrap=True)
        yield Footer()

    # -- lifecycle ----------------------------------------------------------

    def on_mount(self) -> None:
        self.app.set_store_only(True)
        self._settings = load_settings(self.app.switcher.backup_dir)
        # The bar tick everywhere reads app.threshold_pct, loaded once at app
        # startup — sync it to the fresh file value so bars and engine agree,
        # and remember that value: unmount restores it (only the session
        # adjustment reverts, not this correction).
        self._configured_threshold = self._settings.threshold
        self.app.threshold_pct = self._settings.threshold
        self._configured_strategy = self._settings.strategy
        # The weekly line is not session-adjustable, so this is only the same
        # sync-to-the-file correction; nothing to restore on unmount.
        self.app.weekly_threshold_pct = self._settings.weekly_threshold
        self._update_summary()
        self.watch(self.app, "snapshot", self._on_snapshot)
        self.watch(self.app, "theme", self._on_theme_change)
        # ONLY `cswap tui --auto` starts LIVE. Entering the view from the
        # menu always starts dry-run, because opening a view must never
        # begin switching accounts — and a persisted "yes" is not consent
        # for a launch nobody asked to be live. A setting used to be read
        # here too, so one confirmed "Go live" made every later menu visit
        # switch accounts unasked, on every machine sharing settings.json.
        self._start_engine(dry_run=not self._start_live)

    def on_unmount(self) -> None:
        if self._engine is not None:
            self._engine.stop()
        # A session threshold must not outlive the engine it steered: unpin
        # the poll planner and put the bar tick back on the file value.
        self.app.switcher.clear_poll_policy_inputs()
        if self._configured_threshold is not None:
            self.app.threshold_pct = self._configured_threshold
        self.app.set_store_only(False)

    def _on_theme_change(self, _theme: str) -> None:
        self._update_summary()
        self._update_badge()
        snap = self.app.snapshot
        if snap is not None:
            self._on_snapshot(snap)

    def action_back(self) -> None:
        if self._adjusting:
            self._end_adjust()
            return
        self.app.pop_screen()

    # -- threshold adjust mode ------------------------------------------------

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action in ("threshold_step", "adjust_done") and not self._adjusting:
            return False  # hidden and inert until adjust mode is armed
        return True

    def action_adjust_threshold(self) -> None:
        if self._adjusting:
            self._end_adjust()
            return
        self._adjusting = True
        self._entry_threshold = self._settings.threshold
        self._update_summary()
        self.refresh_bindings()

    def action_adjust_done(self) -> None:
        if self._adjusting:
            self._end_adjust()

    def action_threshold_step(self, delta: float) -> None:
        if not self._adjusting:
            return
        spec = SETTING_SPECS["autoswitch.threshold"]
        value = min(spec.hi, max(spec.lo, self._settings.threshold + delta))
        self._set_threshold(value)

    def _end_adjust(self) -> None:
        self._adjusting = False
        self._update_summary()
        self.refresh_bindings()
        if self._settings.threshold == self._entry_threshold:
            return  # no net change: nothing to announce, no tick to force
        if self._engine is not None:
            self._engine.wake()  # show a decision at the new value now
        self.query_one("#event-log", RichLog).write(
            Text(
                f"— threshold set to {pct_label(self._settings.threshold)}% "
                "for this session —",
                style=Palette.from_theme(self.app.current_theme).muted,
            )
        )

    def _set_threshold(self, value: float) -> None:
        if value == self._settings.threshold:
            return
        self._settings = replace(self._settings, threshold=value)
        if self._engine is not None:
            self._engine.apply_threshold(value)
        self.app.threshold_pct = value
        self.query_one("#auto-active-panel", AccountsPanel).refresh()
        self._update_summary()
        # The Next best list ranks by the lines in force, so it must follow
        # the session value now rather than at the next snapshot: under a
        # weekly line the order and the 5h rows' "threshold N%" both move.
        self._on_snapshot(self.app.snapshot)

    def action_cycle_strategy(self) -> None:
        # Session-only, exactly like `t`/threshold above: never written to
        # settings.json, reverted to the file value on unmount.
        # A configured strategy the cycle does not offer (priority-consume-first)
        # starts it over.
        strategy = self._settings.strategy
        current = _STRATEGY_CYCLE.index(strategy) if strategy in _STRATEGY_CYCLE else -1
        value = _STRATEGY_CYCLE[(current + 1) % len(_STRATEGY_CYCLE)]
        self._settings = replace(self._settings, strategy=value)
        if self._engine is not None:
            self._engine.apply_strategy(value)
            self._engine.wake()  # show a decision under the new strategy now
        self.query_one("#auto-active-panel", AccountsPanel).refresh()
        self._update_summary()
        self.query_one("#event-log", RichLog).write(
            Text(
                f"— strategy set to {value} for this session —",
                style=Palette.from_theme(self.app.current_theme).muted,
            )
        )

    def _update_summary(self) -> None:
        palette = Palette.from_theme(self.app.current_theme)
        text = Text()
        text.append("auto-switch · ")
        text.append(
            f"threshold {pct_label(self._settings.threshold)}%",
            style=palette.accent if self._adjusting else "",
        )
        if self._settings.threshold != self._configured_threshold:
            text.append(" (session)", style=palette.muted)
        text.append(f" · {self._settings.strategy}")
        if self._settings.strategy != self._configured_strategy:
            text.append(" (session)", style=palette.muted)
        if self._settings.weekly_threshold is not None:
            text.append(
                f" · weekly {pct_label(self._settings.weekly_threshold)}%"
            )
        text.append(f" · poll every {self._settings.interval_seconds:.0f}s")
        if self._adjusting:
            text.append("   ← → adjust · enter done", style=palette.muted)
        self.query_one("#auto-summary", Static).update(text)

    # -- engine -------------------------------------------------------------

    def _start_engine(self, *, dry_run: bool) -> None:
        engine = AutoSwitchEngine(
            self.app.switcher,
            self._settings,
            self._emit_from_thread,
            dry_run=dry_run,
        )
        self._engine = engine
        # A LIVE request the engine could not honor: another LIVE engine holds
        # the machine's lock. Report what actually started, not what was asked
        # for — the badge reads engine.dry_run, so it is already right.
        dry_run = engine.dry_run
        self.run_worker(
            engine.run_loop,
            thread=True,
            group="engine",
            exit_on_error=False,
            name=f"auto-engine-{'dry' if dry_run else 'live'}",
        )
        self._update_badge()
        log = self.query_one("#event-log", RichLog)
        mode = "DRY-RUN (watching only)" if dry_run else "LIVE (will switch accounts)"
        log.write(
            Text(
                f"— engine started: {mode} —",
                style=Palette.from_theme(self.app.current_theme).muted,
            )
        )

    def _emit_from_thread(self, event: AutoSwitchEvent) -> None:
        """Engine ``on_event`` callback — runs on the worker thread."""
        try:
            self.app.call_from_thread(self._on_engine_event, event)
        except Exception:
            # App/screen tearing down mid-tick; the event has nowhere to go.
            pass

    def _on_engine_event(self, event: AutoSwitchEvent) -> None:
        if not self.is_attached:
            return
        palette = Palette.from_theme(self.app.current_theme)
        self.query_one("#event-log", RichLog).write(event_text(event, palette=palette))
        # The engine can PROMOTE itself mid-run: a demotion is a contention
        # answer, and the holder eventually exits. Nothing else re-reads
        # `dry_run` after mount, so the badge would keep saying DRY-RUN over a
        # live engine — worse than the stuck-dry-run it fixes, because now the
        # display disagrees with what is actually switching accounts.
        self._update_badge()
        if event.kind == "switch":
            self.app.request_refresh()

    def action_toggle_live(self) -> None:
        if self._engine is None:
            return
        if self._engine.dry_run:
            self.app.push_screen(
                ConfirmModal(
                    "Go live? claude-swap will switch your active account "
                    "automatically when the threshold is reached.\n\n"
                    "(Same behavior as running `cswap auto` in a terminal.)",
                    title="Go live",
                    yes_label="Go live",
                ),
                self._on_live_confirm,
            )
        else:
            self._restart_engine(dry_run=True)

    def _on_live_confirm(self, confirmed: bool | None) -> None:
        if confirmed:
            self._restart_engine(dry_run=False)

    def _restart_engine(self, *, dry_run: bool) -> None:
        if self._engine is not None:
            self._engine.stop()
        self._start_engine(dry_run=dry_run)

    def _update_badge(self) -> None:
        badge = self.query_one("#mode-badge", Static)
        if self._engine is not None and not self._engine.dry_run:
            badge.update(" LIVE ")
            badge.set_classes("live")
        else:
            badge.update(" DRY-RUN ")
            badge.set_classes("dry")

    # -- candidates -----------------------------------------------------------

    def _on_snapshot(self, snap: AccountsSnapshot | None) -> None:
        if snap is None:
            return
        self.query_one("#candidates", Static).update(
            self._candidates_text(snap, active_number=snap.active_number)
        )

    def _candidates_text(
        self, snap: AccountsSnapshot, active_number: str | None
    ) -> Text:
        """Switch targets ranked the way the engine's strategy would rank them."""
        # Same window set AND the same lines as the engine: autoswitch.model,
        # each account's own line (its policy threshold, else the session
        # threshold — `_settings` is what `_set_threshold` replaces), the
        # weekly line, and the session threshold as the ruler every account is
        # scored on, so the displayed ranking can never disagree with the
        # account it picks.
        palette = Palette.from_theme(self.app.current_theme)
        settings = self._settings
        models = parse_model_names(settings.model) if settings else ()
        threshold = settings.threshold if settings else 90.0
        weekly = settings.weekly_threshold if settings else None

        def line_of(acc) -> float:
            return acc.policy.threshold if acc.policy.threshold is not None else threshold

        def noted(acc) -> bool:
            return weekly is not None or acc.policy.threshold is not None

        # Same strategy the engine ticks on, so the panel's order can never
        # disagree with the account a tick would actually switch to.
        consume_first = bool(
            self._settings and self._settings.strategy in CONSUME_FIRST_STRATEGIES
        )
        # THE AXIS THE ENGINE WILL ACTUALLY RANK ON THIS TICK, not always
        # `models`: `_rank_candidates` (autoswitch.py) drops the model set
        # and retries on 5h/7d alone when the model-gated pass finds no
        # healthy candidate — so a panel that always ranks on `models` can
        # name a top row the engine would never pick (still model-gated
        # ranking a fleet the engine has already dropped it for). But the
        # engine only runs that retry under `strategy == "dynamic"`
        # (autoswitch.py's `_rank_candidates`) — gated the same way here, or
        # `best`/`consume-first` rank on an axis the engine is forbidden to
        # use. Same predicate as the model-gated pass's own health filter
        # (`classify_candidate_block` — "open" is exactly what that filter
        # lets through): any candidate reading "open" means the model-gated
        # pass has something to work with, so keep `models`; none reading
        # "open" means it would come back empty, so rank on the retry's
        # axis instead — a "model"-only block clears once `models` drops,
        # and a "full" block stays blocked either way.
        rank_models = models
        if models and self._settings and self._settings.strategy == "dynamic":
            for acc in snap.accounts:
                if (
                    acc.number == active_number
                    or not acc.switchable
                    or acc.usage.sentinel is not None
                    or binding_pct(acc.usage.last_good, models) is None
                ):
                    continue
                windows = (
                    (label, p)
                    for label, p, _ in oauth.relevant_windows(
                        acc.usage.last_good, models
                    )
                )
                kind, _ = classify_candidate_block(windows, line_of(acc), weekly)
                if kind == "open":
                    break
            else:
                rank_models = ()
        ranked: list[tuple[tuple, str]] = []  # (sort key, number)
        rows: dict[str, CandidateRow] = {}
        now = time.time()
        # Chip columns are keyed by WINDOW NAME, never by position: two rows
        # can have different-length window lists built from that account's
        # own payload (`relevant_windows`), so "chip 1" is not the same
        # window across rows. Computed once, before any row is drawn, over
        # exactly the accounts that will reach the chip branch below
        # (switchable, no sentinel, a known binding pct).
        row_windows: dict[str, list[tuple[str, float, str | None]]] = {}
        for acc in snap.accounts:
            if (
                acc.number == active_number
                or not acc.switchable
                or acc.usage.sentinel is not None
                or binding_pct(acc.usage.last_good, models) is None
            ):
                continue
            row_windows[acc.number] = oauth.relevant_windows(acc.usage.last_good, models)
        # The column set is the union of window names over every chip row, in
        # first-seen order; a row without a window gets a blank cell, so no
        # later column moves. (label text, pct text) widths per column; the
        # threshold note is the row's last cell, so it takes no column.
        chip_width: dict[str, tuple[int, int]] = {}
        for acc in snap.accounts:
            windows = row_windows.get(acc.number)
            if windows is None:
                continue
            for label, wpct, resets_at in windows:
                label_w, pct_w = chip_width.get(label, (0, 0))
                chip_width[label] = (
                    max(label_w, cell_len(data.chip_label(
                        label, data.reset_text({"resets_at": resets_at}, now)
                    ))),
                    max(pct_w, cell_len(f"{wpct:.0f}%")),
                )
        # 5h and 7d lead whatever row came first (sorted is stable, so the
        # rest keep first-seen order).
        chip_width = dict(sorted(
            chip_width.items(), key=lambda kv: {"5h": 0, "7d": 1}.get(kv[0], 2)
        ))
        for acc in snap.accounts:
            if acc.number == active_number:
                continue
            # A slot with no stored login is still a place you can GO — that
            # is now how you fill one. It used to be dropped from this list
            # entirely, so a machine with the roster but not the credentials
            # showed two accounts here and five in the engine's own log. A row
            # that says why it cannot be picked beats a row that isn't there.
            if not acc.switchable:
                # From SENTINEL_NOTES, not written here: an API-key slot has no
                # login to restore, and the switch screen reads the same table,
                # so both surfaces must describe a slot identically.
                # The slot's OWN sentinel first: unswitchable is not always
                # "nothing stored". A backup that exists but could not be READ
                # reads USAGE_KEYCHAIN_UNAVAILABLE, and sending that slot to
                # `cswap add` overwrites a working stored grant. `kind` still
                # wins for api_key — the sentinel diverges from it behind a
                # locked keychain.
                note = data.sentinel_label(
                    USAGE_API_KEY if acc.kind == "api_key"
                    else acc.usage.sentinel or USAGE_NO_CREDENTIALS
                )
                rows[acc.number] = CandidateRow(
                    Text(acc.number, style=palette.muted),
                    Text(acc.email, style=palette.muted),
                    Text(note, style=palette.sev_warn),
                    note=True,
                )
                ranked.append(((1000.0,), acc.number))   # last: never a target
                continue
            line = line_of(acc)
            binding = binding_window(
                oauth.relevant_windows(acc.usage.last_good, models),
                line,
                weekly,
                scale=threshold,
            )
            usage = Text()
            status: dict[str, Text] = {}
            short: dict[str, Text] = {}
            free_text = False
            if acc.usage.sentinel is not None:
                free_text = True
                usage.append(
                    data.sentinel_label(acc.usage.sentinel), style=palette.muted
                )
                ranked.append(((998.0,), acc.number))
            elif binding is None:
                # An extra-usage (pay-as-you-go) account has no 5h/7d window,
                # so binding_window answers None — but it is not unknown, it has
                # a SPEND budget, and the watch screen already renders it.
                # `relevant_windows` excludes spend on purpose (a separate
                # axis from a rate-limit window), so this row was the only
                # place the same account read two different ways.
                spend = spend_row(
                    usage_rows(acc.usage.last_good, time.time())
                )
                if spend is not None:
                    _label, spend_pct, spend_suffix, _full = spend
                    usage.append("$$ ", style=palette.muted)
                    usage.append(f"{spend_pct:.0f}%",
                                 style=palette.severity(spend_pct))
                    usage.append(f" · {spend_suffix}", style=palette.muted)
                else:
                    free_text = True
                    usage.append("usage unknown", style=palette.muted)
                # RANKED LAST EITHER WAY. Spend is not headroom: folding it
                # into the sort key would change which account the engine
                # picks, and the ranking axis is not this row's to move.
                ranked.append(((999.0,), acc.number))
            else:
                # Per-window chips, from the same helper the dashboard uses
                # (data.chip_label) so one account cannot read two ways. The
                # SAME relevant_windows call feeds the label below, so the
                # chips and the label can never disagree on which windows
                # exist for this account.
                windows = row_windows[acc.number]
                have = {label: (wpct, resets_at) for label, wpct, resets_at in windows}
                shown = False
                for i, (label, (label_w, pct_w)) in enumerate(
                    chip_width.items() if windows else ()
                ):
                    sep = "" if i == 0 else " · " if shown else "   "
                    if label not in have:
                        usage.append(" " * (len(sep) + label_w + pct_w))
                        continue
                    wpct, resets_at = have[label]
                    label_text = data.chip_label(
                        label, data.reset_text({"resets_at": resets_at}, now)
                    )
                    pct_text = f"{wpct:.0f}%"
                    usage.append(sep, style=palette.muted)
                    # Label right-aligned so the `:` shares a column (owner,
                    # 2026-09-15); pct right-aligned so the `%` does.
                    usage.append(
                        " " * (label_w - cell_len(label_text)) + label_text
                        + " " * (pct_w - cell_len(pct_text)),
                        style=palette.muted,
                    )
                    usage.append(pct_text, style=palette.severity(wpct))
                    shown = True
                # WHAT blocks this candidate, not just the raw chips: a 5h/7d
                # window (no model choice escapes it) reads differently from
                # a model-only block (the engine's fallback ranks around it).
                # The decision log classifies with the same helper,
                # `classify_candidate_block`, but words it its own way; the
                # panel words it plainly. Always on `models`, the full pinned
                # set: this label explains why the row is not simply "open" on
                # the criteria the user actually configured, independent of
                # whether `rank_models` below has dropped to the retry's axis
                # for ORDERING purposes.
                kind, blocked_model = "open", None
                if self._settings:
                    kind, blocked_model = classify_candidate_block(
                        ((label, p) for label, p, _ in windows), line, weekly
                    )
                if kind in ("model", "full"):
                    status["block"] = Text(f"{blocked_model} limit reached", style=palette.muted)
                    short["block"] = Text(f"{blocked_model} full", style=palette.muted)
                # Only when the binding window's chip was drawn above.
                if binding.label in have:
                    pct = _line_note(binding, binding.label, noted(acc))
                    if pct:
                        status["threshold"] = Text(f"limit {pct}", style=palette.muted)
                        short["threshold"] = Text(f"lim {pct}", style=palette.muted)
                rank_binding = binding_window(
                    oauth.relevant_windows(acc.usage.last_good, rank_models),
                    line,
                    weekly,
                    scale=threshold,
                ) or binding
                key = (
                    consume_first_rank_key(
                        acc.usage.last_good,
                        line,
                        now,
                        rank_models,
                        weekly_threshold=weekly,
                        scale=threshold,
                    )
                    if consume_first
                    else (-rank_binding.line_headroom,)
                )
                ranked.append((key, acc.number))
            rows[acc.number] = CandidateRow(
                Text(acc.number, style=palette.foreground),
                Text(acc.email, style=palette.foreground),
                usage,
                status=status,
                short=short,
                login=Text(login_segment(acc, "full", gap=""), style=palette.muted),
                login_short=Text(login_segment(acc, "short", gap=""), style=palette.muted),
                note=free_text,
            )

        text = Text()
        text.append("Next best", style=palette.muted)
        if not ranked:
            # Reached only when this is the sole account. Slots that cannot be
            # switched to are listed above with the reason, so "no other
            # accounts" is now literal rather than a filter's side effect.
            text.append("\n  no other accounts", style=palette.muted)
            return text
        # The box has no width before its first layout (the app's less its
        # padding), and a screen built with `__new__` has no `_nodes` to query.
        try:
            box = self.query_one("#candidates", Static)
        except AttributeError:
            width = None
        else:
            width = box.size.width or self.app.size.width - 4
        text.append_text(render_candidate_rows(
            [rows[number] for _key, number in sorted(ranked)], palette, width
        ))
        return text
