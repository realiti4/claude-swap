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

import math
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
    _seven_day_reset_ts,
    pct_label,
)
from claude_swap.exceptions import ConfigError
from claude_swap.lines import binding_window
from claude_swap.models import AccountsSnapshot
from claude_swap.settings import (
    AUTOSWITCH_STRATEGIES,
    SETTING_SPECS,
    load_settings,
    parse_model_names,
    set_setting,
)
from claude_swap.tui import data
from claude_swap.tui.modals import ConfirmModal
from claude_swap.tui.theme import Palette
from claude_swap.tui.widgets import AccountsPanel

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
    if role is not None:
        style = getattr(palette, role)
    else:
        style = palette.muted if event.kind in _QUIET_KINDS else palette.foreground
    text = Text()
    text.append(f"{data.clock_stamp()}  ", style=palette.muted)
    text.append(event.human(), style=style)
    return text


class AutoScreen(Screen):
    BINDINGS = [
        Binding("l", "toggle_live", "Go live / dry-run"),
        Binding("s", "cycle_strategy", "Strategy"),
        Binding("t", "adjust_threshold", "Threshold"),
        Binding("d", "save_defaults", "Save defaults"),
        Binding("left", "threshold_step(-1)", "-1%"),
        Binding("right", "threshold_step(1)", "+1%"),
        Binding("enter", "adjust_done", "Done"),
        Binding("escape,q", "back", "Back"),
    ]

    app: "CswapApp"

    def __init__(self) -> None:
        super().__init__()
        self._engine: AutoSwitchEngine | None = None
        self._settings = None
        # Session-only threshold adjustment (t, then arrows). Never written
        # to settings.json — same memory-only precedent as the dry-run
        # toggle. ``_configured_threshold`` is the mount-time file value the
        # screen reverts to on exit; ``_entry_threshold`` is the value when
        # adjust mode was entered (wake/log only on a net change).
        self._adjusting = False
        self._configured_threshold: float | None = None
        self._entry_threshold: float | None = None
        # Same session-only rule for the strategy (s cycles it). Both
        # ``_configured_*`` values are the settings.json baseline: the
        # "(session)" tags compare against them, and ``d`` (save defaults)
        # is the only thing that moves them after mount.
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
        self._configured_strategy = self._settings.strategy
        self.app.threshold_pct = self._settings.threshold
        # The weekly line is not session-adjustable, so this is only the same
        # sync-to-the-file correction; nothing to restore on unmount.
        self.app.weekly_threshold_pct = self._settings.weekly_threshold
        self._update_summary()
        self.watch(self.app, "snapshot", self._on_snapshot)
        self.watch(self.app, "theme", self._on_theme_change)
        self._start_engine(dry_run=True)

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
        self._refresh_candidates()

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
        self._note(
            f"— threshold set to {pct_label(self._settings.threshold)}% "
            "for this session —"
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
        # weekly line the order and the 5h rows' "(line N%)" both move.
        self._on_snapshot(self.app.snapshot)

    def _update_summary(self) -> None:
        palette = Palette.from_theme(self.app.current_theme)
        text = Text()
        text.append("auto-switch · ")
        text.append(f"strategy {self._settings.strategy}")
        if self._settings.strategy != self._configured_strategy:
            text.append(" (session)", style=palette.muted)
        text.append(" · ")
        text.append(
            f"threshold {pct_label(self._settings.threshold)}%",
            style=palette.accent if self._adjusting else "",
        )
        if self._settings.threshold != self._configured_threshold:
            text.append(" (session)", style=palette.muted)
        if self._settings.weekly_threshold is not None:
            text.append(
                f" · weekly {pct_label(self._settings.weekly_threshold)}%"
            )
        text.append(f" · poll every {self._settings.interval_seconds:.0f}s")
        if self._adjusting:
            text.append("   ← → adjust · enter done", style=palette.muted)
        self.query_one("#auto-summary", Static).update(text)

    # -- strategy (s) and save as defaults (d) --------------------------------

    def action_cycle_strategy(self) -> None:
        """Next strategy in AUTOSWITCH_STRATEGIES order, session-only like the
        threshold. The engine reads it on its next tick; wake it so the log
        shows a decision under the new strategy right away."""
        choices = AUTOSWITCH_STRATEGIES
        current = self._settings.strategy
        nxt = (
            choices[(choices.index(current) + 1) % len(choices)]
            if current in choices
            else choices[0]
        )
        self._settings = replace(self._settings, strategy=nxt)
        if self._engine is not None:
            self._engine.apply_strategy(nxt)
            self._engine.wake()
        self._update_summary()
        self._refresh_candidates()
        self._note(f"— strategy → {nxt} for this session —")

    def action_save_defaults(self) -> None:
        if self._adjusting:
            self._end_adjust()  # save what the user is looking at, mode closed
        s = self._settings
        if (s.strategy, s.threshold) == (
            self._configured_strategy,
            self._configured_threshold,
        ):
            self._note("— strategy and threshold are already the defaults —")
            return
        self.app.push_screen(
            ConfirmModal(
                "Save as defaults?\n\n"
                f"  strategy {s.strategy}\n"
                f"  threshold {pct_label(s.threshold)}%\n\n"
                "Written to settings.json. A `cswap auto` or menu-bar engine "
                "that is already running picks them up on its next start.",
                title="Save defaults",
                yes_label="Save",
            ),
            self._on_save_confirm,
        )

    def _on_save_confirm(self, confirmed: bool | None) -> None:
        if not confirmed:
            return
        s = self._settings
        root = self.app.switcher.backup_dir
        # Two single-key writes through `cswap config set`'s path: validated,
        # and every other key in the file survives. Each baseline moves only
        # once its own write landed, so after a partial failure the
        # "(session)" tags still tell the truth about what is on disk.
        try:
            set_setting(root, "autoswitch.strategy", s.strategy)
            self._configured_strategy = s.strategy
            set_setting(root, "autoswitch.threshold", str(s.threshold))
            self._configured_threshold = s.threshold
        except (ConfigError, OSError) as e:
            self._update_summary()
            self._note(f"— could not save defaults: {e} —", role="sev_warn")
            return
        self._update_summary()
        self._note(
            f"— saved as defaults: strategy {s.strategy}, "
            f"threshold {pct_label(s.threshold)}% —"
        )

    def _note(self, message: str, *, role: str = "muted") -> None:
        palette = Palette.from_theme(self.app.current_theme)
        self.query_one("#event-log", RichLog).write(
            Text(message, style=getattr(palette, role))
        )

    # -- engine -------------------------------------------------------------

    def _start_engine(self, *, dry_run: bool) -> None:
        engine = AutoSwitchEngine(
            self.app.switcher,
            self._settings,
            self._emit_from_thread,
            dry_run=dry_run,
        )
        self._engine = engine
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

    def _refresh_candidates(self) -> None:
        """Redraw from the current snapshot after a strategy/threshold/theme
        change, without waiting for the next usage refresh."""
        self._on_snapshot(self.app.snapshot)

    def _candidates_text(
        self, snap: AccountsSnapshot, active_number: str | None
    ) -> Text:
        """Switch targets in the order the session strategy ranks them.

        Same window set AND the same lines as the engine: autoswitch.model,
        each account's own line (its policy threshold, else the session
        threshold — `_settings` is what `_set_threshold` replaces), the weekly
        line, and the session threshold as the ruler every account is scored
        on. Then the engine's key for the session strategy, so the list never
        contradicts the account it picks:

        - ``best``: most policy headroom.
        - ``weekly-first`` / ``consume-first``: soonest weekly reset among
          accounts under their lines (ties to most policy headroom).
        - ``priority-consume-first``: highest account priority, then soonest
          weekly reset, among accounts under their lines.

        Accounts over their lines follow (the engine will not land on them
        proactively), by policy headroom; unread rows last. It is the
        strategy's order, not the engine's next move — anti-flap, cooldown and
        consume-first's "only for a sooner reset" gates still apply on top.
        """
        palette = Palette.from_theme(self.app.current_theme)
        settings = self._settings
        models = parse_model_names(settings.model) if settings else ()
        threshold = settings.threshold if settings else 90.0
        weekly = settings.weekly_threshold if settings else None
        strategy = settings.strategy if settings else "best"
        by_reset = strategy in ("weekly-first", "consume-first")
        by_priority = strategy == "priority-consume-first"
        now = time.time()
        # (tier, primary, secondary) ascending: tier 0 the strategy's own
        # order, 1 over-line, 2 unread (sentinels ahead of plain unknowns).
        ranked: list[tuple[tuple[int, float, float], str]] = []
        lines: dict[str, Text] = {}
        for acc in snap.accounts:
            if acc.number == active_number or not acc.switchable:
                continue
            last_good = acc.usage.last_good
            line = (
                acc.policy.threshold if acc.policy.threshold is not None else threshold
            )
            binding = binding_window(
                oauth.relevant_windows(last_good, models),
                line,
                weekly,
                scale=threshold,
            )
            entry = Text()
            entry.append(f"\n  {acc.number:>2}  ", style=palette.foreground)
            entry.append(acc.email, style=palette.foreground)
            if acc.usage.sentinel is not None:
                entry.append(
                    f"  {data.sentinel_label(acc.usage.sentinel)}", style=palette.muted
                )
                key = (2, 0.0, 998.0)
            elif binding is None:
                entry.append("  usage unknown", style=palette.muted)
                key = (2, 0.0, 999.0)
            else:
                # The raw pct of the window that binds, and — only once the
                # lines differ, globally or for this account — the line it is
                # judged against.
                entry.append(
                    f"  {binding.label} {binding.pct:3.0f}% used",
                    style=palette.severity(binding.pct),
                )
                if weekly is not None or acc.policy.threshold is not None:
                    entry.append(
                        f" (line {pct_label(binding.line)}%)", style=palette.muted
                    )
                if by_priority and acc.policy.priority:
                    entry.append(
                        f" · priority {acc.policy.priority}", style=palette.muted
                    )
                if by_reset or by_priority:
                    reset = data.window_reset_text(last_good, "seven_day", now)
                    if reset:
                        entry.append(f" · {reset}", style=palette.muted)
                lh = binding.line_headroom
                if binding.over and (by_reset or by_priority):
                    key = (1, -lh, 0.0)
                elif by_reset or by_priority:
                    reset_ts = _seven_day_reset_ts(last_good, now)
                    soonest = math.inf if reset_ts is None else reset_ts
                    key = (
                        (0, -acc.policy.priority, soonest)
                        if by_priority
                        else (0, soonest, -lh)
                    )
                else:
                    key = (0, -lh, 0.0)
            ranked.append((key, acc.number))
            lines[acc.number] = entry

        text = Text()
        heading = {
            "weekly-first": "Next (soonest weekly reset)",
            "consume-first": "Next (soonest weekly reset)",
            "priority-consume-first": "Next (priority, then soonest weekly reset)",
        }.get(strategy, "Next best")
        text.append(heading, style=palette.muted)
        if not ranked:
            text.append("\n  no other switchable accounts", style=palette.muted)
            return text
        for _key, number in sorted(ranked):
            text.append(lines[number])
        return text
