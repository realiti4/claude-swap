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
from dataclasses import replace
from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, RichLog, Static

from claude_swap import oauth
from claude_swap.autoswitch import (
    AutoSwitchEngine,
    AutoSwitchEvent,
    binding_pct,
    classify_candidate_block,
    model_block_label,
    pct_label,
    proactive_switch_bar_pct,
)
from claude_swap.json_output import USAGE_API_KEY, USAGE_NO_CREDENTIALS
from claude_swap.models import AccountsSnapshot
from claude_swap.settings import (
    SETTING_SPECS,
    AutoSwitchSettings,
    load_settings,
    parse_model_names,
)
from claude_swap.tui import data
from claude_swap.tui.modals import ConfirmModal
from claude_swap.tui.theme import Palette
from claude_swap.tui.widgets import AccountsPanel, spend_row, usage_rows

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

# Trigger names keyed to WHY the ranking pass never ran -- never "no
# candidate qualifies", the claim only a real, empty pass earns.
_UNMODELED_TEXT = {
    "below-threshold": "not previewed (active below threshold)",
    "unreadable-active": "not previewed (active status unknown)",
}


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
        # threshold_pct AND auto_settings are loaded once at app startup;
        # sync both to the fresh file value (unmount reverts only the
        # session threshold adjustment, never this correction).
        self._configured_threshold = self._settings.threshold
        self.app.threshold_pct = proactive_switch_bar_pct(
            self._settings.strategy, self._settings.threshold
        )
        self.app.auto_settings = self._settings
        self._configured_strategy = self._settings.strategy
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
            self.app.threshold_pct = proactive_switch_bar_pct(
                self._configured_strategy, self._configured_threshold
            )
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
        self.app.threshold_pct = proactive_switch_bar_pct(
            self._settings.strategy, value
        )
        self.query_one("#auto-active-panel", AccountsPanel).refresh()
        self._update_summary()

    def action_cycle_strategy(self) -> None:
        # Session-only, exactly like `t`/threshold above: never written to
        # settings.json, reverted to the file value on unmount.
        current = _STRATEGY_CYCLE.index(self._settings.strategy)
        value = _STRATEGY_CYCLE[(current + 1) % len(_STRATEGY_CYCLE)]
        self._settings = replace(self._settings, strategy=value)
        self.app.threshold_pct = proactive_switch_bar_pct(
            value, self._settings.threshold
        )
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
        bar = proactive_switch_bar_pct(
            self._settings.strategy, self._settings.threshold
        )
        if bar != self._settings.threshold:
            text.append(f" · switch at {pct_label(bar)}%")
        text.append(f" · {self._settings.strategy}")
        if self._settings.strategy != self._configured_strategy:
            text.append(" (session)", style=palette.muted)
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
        self._last_active_at = data.read_last_active_at(self.app.switcher.backup_dir)
        self.query_one("#candidates", Static).update(
            self._candidates_text(snap, active_number=snap.active_number)
        )

    def _candidates_text(
        self, snap: AccountsSnapshot, active_number: str | None
    ) -> Text:
        """Switch targets ranked the way the engine's strategy would rank them."""
        # Same window set as the engine (autoswitch.model included), so the
        # displayed ranking can never disagree with the account it picks.
        palette = Palette.from_theme(self.app.current_theme)
        models = parse_model_names(self._settings.model) if self._settings else ()
        settings = self._settings or AutoSwitchSettings()
        # Same strategy the engine ticks on, so the panel's order can never
        # disagree with the account a tick would actually switch to. Off
        # `settings` (never `self._settings` directly, which can be `None`
        # before `on_mount` loads it) so this can never read a DIFFERENT
        # strategy than the trigger/ranking below run on.
        ranked: list[tuple[tuple, str]] = []  # (sort key, number)
        lines: dict[str, Text] = {}
        now = time.time()
        # THE engine's own admission and order -- `ordered_accounts` (data.py)
        # reads off this exact same call for every other listing screen.
        # `getattr` (never plain `self._last_active_at`): a test instance
        # built via `AutoScreen.__new__` skips `__init__`/`_on_snapshot`.
        last_active_at = getattr(self, "_last_active_at", None) or {}
        ordered, rank_axis, trigger, unmodeled = data.rank_switch_candidates(
            snap, settings, now, active_number, last_active_at
        )
        ordered_rank = {num: i for i, num in enumerate(ordered)}
        # Captured before the loop rebinds `now` below (per-row, for the
        # window chips) -- the fallback key must agree with the SAME `now`
        # the admission pass above just ran on, not a fresh read.
        admission_now = now
        for acc in snap.accounts:
            if acc.number == active_number:
                continue
            # A slot with no stored login is still a place you can GO — that
            # is now how you fill one. It used to be dropped from this list
            # entirely, so a machine with the roster but not the credentials
            # showed two accounts here and five in the engine's own log. A row
            # that says why it cannot be picked beats a row that isn't there.
            if not acc.switchable:
                entry = Text()
                entry.append(f"\n  {acc.number:>2}  ", style=palette.muted)
                entry.append(acc.email, style=palette.muted)
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
                entry.append(f"  {note}", style=palette.sev_warn)
                lines[acc.number] = entry
                ranked.append(((1000.0,), acc.number))   # last: never a target
                continue
            pct = binding_pct(acc.usage.last_good, models)
            entry = Text()
            entry.append(f"\n  {acc.number:>2}  ", style=palette.foreground)
            entry.append(acc.email, style=palette.foreground)
            if acc.usage.sentinel is not None:
                entry.append(
                    f"  {data.sentinel_label(acc.usage.sentinel)}", style=palette.muted
                )
                # `ordered_rank` first: the API-key last resort (data.rank_
                # switch_candidates) can name THIS row as the engine's actual
                # next pick, and it must not sort behind every refused row.
                key = (
                    (0, ordered_rank[acc.number])
                    if acc.number in ordered_rank else (998.0,)
                )
                ranked.append((key, acc.number))
            elif pct is None:
                # An extra-usage (pay-as-you-go) account has no 5h/7d window,
                # so binding_pct answers None — but it is not unknown, it has
                # a SPEND budget, and the watch screen already renders it.
                # `relevant_windows` excludes spend on purpose (a separate
                # axis from a rate-limit window), so this row was the only
                # place the same account read two different ways.
                spend = spend_row(
                    usage_rows(acc.usage.last_good, time.time())
                )
                if spend is not None:
                    _label, spend_pct, spend_suffix, _full = spend
                    entry.append("  $$ ", style=palette.muted)
                    entry.append(f"{spend_pct:.0f}%",
                                 style=palette.severity(spend_pct))
                    entry.append(f" · {spend_suffix}", style=palette.muted)
                else:
                    entry.append("  usage unknown", style=palette.muted)
                # A spend-axis account is never a ranking target regardless
                # of `acc.disabled` (`relevant_windows` excludes spend, so
                # `_rank` can't see it) -- but every OTHER row here says why
                # it is never chosen, and this was the one silent exception.
                if acc.disabled:
                    entry.append("  auto-swap disabled", style=palette.muted)
                # RANKED LAST, UNLESS THE API-KEY LAST RESORT NAMED IT:
                # spend is not headroom, so folding it into the sort key
                # would change which account the engine picks -- but when
                # `ordered_rank` already names this row (never derived here),
                # it IS the engine's pick and must not sort behind a row the
                # engine refused.
                key = (
                    (0, ordered_rank[acc.number])
                    if acc.number in ordered_rank else (999.0,)
                )
                ranked.append((key, acc.number))
            else:
                # Per-window chips, from the same helper the dashboard uses
                # (data.chip_label) so one account cannot read two ways. The
                # SAME relevant_windows call feeds the label below, so the
                # chips and the label can never disagree on which windows
                # exist for this account.
                now = time.time()
                windows = oauth.relevant_windows(acc.usage.last_good, models)
                for i, (label, wpct, resets_at) in enumerate(windows):
                    entry.append("  " if i == 0 else " · ", style=palette.muted)
                    entry.append(
                        data.chip_label(label, data.reset_text({"resets_at": resets_at}, now)),
                        style=palette.muted,
                    )
                    entry.append(f"{wpct:.0f}%", style=palette.severity(wpct))
                if not windows:  # no window data at all — keep the old reading
                    entry.append(f"  {pct:3.0f}% used", style=palette.severity(pct))
                # A candidate whose LAST poll failed (an active backoff, a
                # run of failures; a success clears both) must not present
                # its cached figures as live. Not `fresh()`'s 180 s TTL: a
                # healthy row's `fetched_at` is older than that for most of
                # every poll cycle, and the engine lands on it happily.
                if acc.usage.in_backoff(now) or acc.usage.consecutive_failures:
                    entry.append("  stale", style=palette.sev_warn)
                # WHAT blocks this candidate, not just the raw chips: a 5h/7d
                # window (no model choice escapes it) reads differently from
                # a model-only block (the engine's fallback ranks around it)
                # — classified here and in the decision log through the SAME
                # helper, `classify_candidate_block`, though the two print
                # `kind == "full"` in different words on purpose (the elif
                # below): the log always says "full", this panel reserves
                # "full" for a window actually at or over 100 and names the
                # bar it was judged against otherwise. Always on `models`,
                # the full pinned set: this label explains why the row is
                # not simply "open" on the criteria the user actually
                # configured, independent of whether the pass above retried
                # on the 5h/7d-only axis for ORDERING purposes.
                kind = "open"
                if self._settings:
                    # The engine's own landing bar, not the raw setting: under
                    # `dynamic` they diverge (`proactive_switch_bar_pct`), and
                    # a row between the two would read "full" here while the
                    # engine itself still ranks it.
                    bar = proactive_switch_bar_pct(
                        self._settings.strategy, self._settings.threshold
                    )
                    kind, blocked_model = classify_candidate_block(
                        ((label, p) for label, p, _ in windows), bar,
                    )
                    if kind == "model":
                        entry.append(
                            f"  {model_block_label(blocked_model)}",
                            style=palette.muted,
                        )
                    elif kind == "full":
                        # "full" is reserved for actual exhaustion (the
                        # window's own pct at or over 100); a window merely
                        # at or over the bar names the bar it was judged
                        # against instead.
                        window_pct = next(
                            p for label, p, _ in windows if label == blocked_model
                        )
                        if window_pct >= 100.0:
                            entry.append(
                                f"  {blocked_model} full", style=palette.muted
                            )
                        else:
                            entry.append(
                                f"  {blocked_model} {pct_label(window_pct)}%"
                                f" >= {pct_label(bar)}%",
                                style=palette.muted,
                            )
                if acc.disabled:
                    entry.append("  auto-swap disabled", style=palette.muted)
                elif (
                    acc.number not in ordered_rank
                    and kind == "open"
                    and not unmodeled
                ):
                    # "open": nothing per-window blocks it, yet the engine's
                    # own pass still refused it -- a weekly reset later than
                    # the active's own, losing the hysteresis margin to a
                    # healthier peer, or ranking behind a sooner recovery.
                    # Every other excluded row already has a reason above.
                    # Never when `unmodeled`: this pass never ran, so
                    # "refused" is not a claim this row can support.
                    entry.append("  not a candidate", style=palette.muted)
                # Position from `ordered_rank` (the engine's own pass, called
                # once above), never a locally re-derived key -- but a row
                # the pass never ranked at all still needs a DETERMINISTIC
                # order among its peers: soonest BINDING-window recovery
                # first, unknown last (`+inf`, `_binding_recovery_ts`'s
                # reading -- the window that actually blocks the account,
                # not always the 7-day one). Only two unranked rows sharing
                # that same recovery still fall to `sorted(ranked)`'s own
                # residual tie-break, the account number as a string --
                # never touching a row the pass DID admit. A disabled row
                # shares the sentinel branch's fixed tier (998.0) above,
                # never this reset-ordered one: the engine will not land
                # here automatically however soon it recovers.
                key = (
                    (0, ordered_rank[acc.number]) if acc.number in ordered_rank
                    else (998.0,) if acc.disabled
                    else (1,) + data.waiting_tail_key(acc.usage.last_good, models, admission_now)
                )
                ranked.append((key, acc.number))
            lines[acc.number] = entry

        text = Text()
        # `rank_axis`: the engine's own report, never re-derived here.
        if rank_axis is not None:
            text.append(f"Next best ({rank_axis})", style=palette.muted)
        else:
            text.append("Next best", style=palette.muted)
        if not ranked:
            # Reached only when this is the sole account. Slots that cannot be
            # switched to are listed above with the reason, so "no other
            # accounts" is now literal rather than a filter's side effect.
            text.append("\n  no other accounts", style=palette.muted)
            return text
        if not ordered:
            reason = _UNMODELED_TEXT.get(trigger)
            if reason is not None:
                # The honest claim for WHY the pass never ran -- never
                # "no candidate qualifies", which claims one did.
                text.append(f"\n  {reason}", style=palette.muted)
            else:
                # DIFFERENT from "no other accounts": rows are still listed
                # below, each naming why -- but none is something a tick would
                # switch to, and ranking one anyway would name a top row the
                # engine could never pick.
                text.append("\n  no candidate qualifies", style=palette.muted)
        for _key, number in sorted(ranked):
            text.append(lines[number])
        return text
