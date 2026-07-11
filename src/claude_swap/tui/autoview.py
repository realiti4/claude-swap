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

from dataclasses import replace
from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, RichLog, Static

from claude_swap.autoswitch import (
    AutoSwitchEngine,
    AutoSwitchEvent,
    binding_pct,
    pct_label,
)
from claude_swap.models import AccountsSnapshot
from claude_swap.settings import SETTING_SPECS, load_settings, parse_model_names
from claude_swap.tui import data
from claude_swap.tui.modals import ConfirmModal
from claude_swap.tui.theme import (
    ACCENT,
    FOREGROUND,
    MUTED,
    SEV_CRIT,
    SEV_WARN,
    severity_color,
)
from claude_swap.tui.widgets import AccountsPanel

if TYPE_CHECKING:
    from claude_swap.tui.app import CswapApp

_EVENT_STYLES = {
    "switch": ACCENT,
    "error": SEV_WARN,
    "account-quarantined": SEV_WARN,
    "all-exhausted": SEV_CRIT,
}
_QUIET_KINDS = {"poll", "no-switch", "sleep", "account-unquarantined"}


def event_text(event: AutoSwitchEvent) -> Text:
    """Log line for one engine event, styled like the CLI's human renderer."""
    style = _EVENT_STYLES.get(event.kind)
    if style is None:
        style = MUTED if event.kind in _QUIET_KINDS else FOREGROUND
    text = Text()
    text.append(f"{data.clock_stamp()}  ", style=MUTED)
    text.append(event.human(), style=style)
    return text


class AutoScreen(Screen):
    BINDINGS = [
        Binding("l", "toggle_live", "Go live / dry-run"),
        Binding("t", "adjust_threshold", "Threshold"),
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
        self._update_summary()
        self.watch(self.app, "snapshot", self._on_snapshot)
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
                style=MUTED,
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

    def _update_summary(self) -> None:
        text = Text()
        text.append("auto-switch · ")
        text.append(
            f"threshold {pct_label(self._settings.threshold)}%",
            style=ACCENT if self._adjusting else "",
        )
        if self._settings.threshold != self._configured_threshold:
            text.append(" (session)", style=MUTED)
        text.append(f" · poll every {self._settings.interval_seconds:.0f}s")
        if self._adjusting:
            text.append("   ← → adjust · enter done", style=MUTED)
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
        log.write(Text(f"— engine started: {mode} —", style=MUTED))

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
        self.query_one("#event-log", RichLog).write(event_text(event))
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
        """Switch targets ranked by remaining headroom (best first)."""
        # Same window set as the engine (autoswitch.model included), so the
        # displayed ranking can never disagree with the account it picks.
        models = parse_model_names(self._settings.model) if self._settings else ()
        ranked: list[tuple[float, str]] = []  # (sort key: pct used, number)
        lines: dict[str, Text] = {}
        for acc in snap.accounts:
            if acc.number == active_number or not acc.switchable:
                continue
            pct = binding_pct(acc.usage.last_good, models)
            entry = Text()
            entry.append(f"\n  {acc.number:>2}  ", style=FOREGROUND)
            entry.append(acc.email, style=FOREGROUND)
            if acc.usage.sentinel is not None:
                entry.append(
                    f"  {data.sentinel_label(acc.usage.sentinel)}", style=MUTED
                )
                ranked.append((998.0, acc.number))
            elif pct is None:
                entry.append("  usage unknown", style=MUTED)
                ranked.append((999.0, acc.number))
            else:
                entry.append(f"  {pct:3.0f}% used", style=severity_color(pct))
                ranked.append((pct, acc.number))
            lines[acc.number] = entry

        text = Text()
        text.append("Next best", style=MUTED)
        if not ranked:
            text.append("\n  no other switchable accounts", style=MUTED)
            return text
        for _pct, number in sorted(ranked):
            text.append(lines[number])
        return text
