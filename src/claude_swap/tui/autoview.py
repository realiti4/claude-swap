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

from claude_swap import balance, modelctl, oauth, tokens
from claude_swap.autoswitch import (
    AutoSwitchEngine,
    AutoSwitchEvent,
    pct_label,
)
from claude_swap.lines import binding_window
from claude_swap.models import AccountSnapshot, AccountsSnapshot
from claude_swap.settings import SETTING_SPECS, load_settings, parse_model_names
from claude_swap.tui import data
from claude_swap.tui.modals import ConfirmModal
from claude_swap.tui.theme import Palette
from claude_swap.tui.widgets import AccountsPanel, fit_login, login_segment

if TYPE_CHECKING:
    from claude_swap.tui.app import CswapApp

_EVENT_ROLES = {
    "switch": "accent",
    "model": "accent",
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
        # weekly line the order and the 5h rows' "(line N%)" both move.
        self._on_snapshot(self.app.snapshot)

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
        if self._settings.weekly_threshold is not None:
            text.append(
                f" · weekly {pct_label(self._settings.weekly_threshold)}%"
            )
        text.append(f" · poll every {self._settings.interval_seconds:.0f}s")
        # Name the strategy when it is not the default, so a screenshot
        # says how the engine is deciding. For balance, also show the margin
        # and the cooldown, which acts as the minimum time spent on an account.
        if self._settings.strategy != "best":
            text.append(f" · strategy {self._settings.strategy}", style=palette.accent)
        if self._settings.strategy == "balance":
            text.append(
                f" (margin {pct_label(self._settings.balance_margin)}%,"
                f" dwell {self._settings.cooldown_seconds / 60:.0f}m)"
            )
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

    def _candidates_text(
        self, snap: AccountsSnapshot, active_number: str | None
    ) -> Text:
        """Switch targets ranked by headroom against their own switch lines
        (best first) — the engine's own ordering."""
        # Same window set AND the same lines as the engine: autoswitch.model,
        # each account's own line (its policy threshold, else the session
        # threshold — `_settings` is what `_set_threshold` replaces), the
        # weekly line, and the session threshold as the ruler every account is
        # scored on, so the displayed ranking can never disagree with the
        # account it picks.
        palette = Palette.from_theme(self.app.current_theme)
        settings = self._settings
        models = parse_model_names(settings.model) if settings else ()
        # The balance strategy ranks on scores, not headroom, so it gets its
        # own panel instead of the candidate list below.
        if settings is not None and settings.strategy == "balance":
            return self._balance_text(
                snap, active_number, settings, models, palette
            ) + self._tokens_text(snap, active_number, settings, palette)
        threshold = settings.threshold if settings else 90.0
        weekly = settings.weekly_threshold if settings else None
        candidates = [
            acc for acc in snap.accounts
            if acc.number != active_number and acc.switchable
        ]
        ranked: list[tuple[float, str]] = []  # (sort key: -policy headroom, number)
        lines: dict[str, tuple[AccountSnapshot, Text, Text]] = {}  # (acc, name, the rest)
        for acc in candidates:
            line = (
                acc.policy.threshold if acc.policy.threshold is not None else threshold
            )
            binding = binding_window(
                oauth.relevant_windows(acc.usage.last_good, models),
                line,
                weekly,
                scale=threshold,
            )
            head, entry = Text(), Text()
            head.append(f"\n  {acc.number:>2}  ", style=palette.foreground)
            head.append(acc.email, style=palette.foreground)
            if acc.usage.sentinel is not None:
                entry.append(
                    f"  {data.sentinel_label(acc.usage.sentinel)}", style=palette.muted
                )
                ranked.append((998.0, acc.number))
            elif binding is None:
                entry.append("  usage unknown", style=palette.muted)
                ranked.append((999.0, acc.number))
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
                ranked.append((-binding.line_headroom, acc.number))
            lines[acc.number] = (acc, head, entry)

        text = Text()
        text.append("Next best", style=palette.muted)
        if not ranked:
            text.append("\n  no other switchable accounts", style=palette.muted)
            return text
        order = [lines[number] for _pct, number in sorted(ranked)]
        # The box has no width before its first layout; its CSS padding is 2 a side.
        budget = self.query_one("#candidates", Static).size.width or self.app.size.width - 4
        for row in fit_login(
            lambda login: [
                Text.assemble(head, tail, Text(login_segment(acc, login), style=palette.muted))
                for acc, head, tail in order
            ],
            budget,
        ):
            text.append(row)
        return text

    # -- balance strategy panel ------------------------------------------------
    #
    # With strategy "balance", the candidate list above is replaced by a
    # panel that shows every account's balance score: the same numbers the
    # engine ranks on, computed by the same functions (balance.py,
    # modelctl.py). Under it are token counts from this computer's Claude
    # Code logs (tokens.py).

    def _balance_text(self, snap, active_number, settings, models, palette) -> Text:
        """Every account with its balance score, highest first.

        One row per account, for example::

             ● 1  someone@example.com            w5  score   41.7  [7d 40%/72h]

        ``●`` marks the active account and ``w5`` is the plan weight. The
        bracket shows the window that set the score (used %, hours to reset).
        Rows also note an account over a line, per-model windows left out
        because the model is not in use, and disabled accounts. The header
        shows the score a candidate needs to take over.
        """
        weight_overrides = balance.parse_weight_overrides(settings.balance_weights)
        now = time.time()
        model_views, model_note = self._model_views(snap, settings, models)

        rows: list[tuple[float, str, Text]] = []  # (sort key, slot, rendered row)
        active_score: float | None = None
        for acc in snap.accounts:
            is_active = acc.number == active_number
            weight = weight_overrides.get(
                acc.number, balance.weight_for_plan(acc.subscription_plan)
            )
            usage_view, ignored_windows = model_views.get(
                acc.number, (acc.usage.last_good, [])
            )
            rate = balance.spare_rate(usage_view, models, weight, now)
            score = rate.score if rate else None
            if is_active:
                active_score = score

            row = Text()
            row.append(
                f"\n {'●' if is_active else ' '}{acc.number:>2}  ",
                style=palette.accent if is_active else palette.foreground,
            )
            row.append(f"{acc.email:<32}", style=palette.foreground)
            row.append(f" w{weight:g}", style=palette.muted)
            row.append(f"  score {balance.fmt(score):>6}", style=palette.foreground)
            if rate:
                row.append(f"  [{rate.detail}]", style=palette.muted)

            # Over a line? Same per-account line the engine uses: the
            # account's own policy threshold, else the session threshold.
            account_line = (
                acc.policy.threshold
                if acc.policy.threshold is not None
                else settings.threshold
            )
            binding = binding_window(
                oauth.relevant_windows(usage_view, models),
                account_line,
                settings.weekly_threshold,
                scale=settings.threshold,
            )
            if binding is not None and binding.over:
                row.append(
                    f"  over {binding.label} line", style=palette.severity(binding.pct)
                )
            if ignored_windows:
                row.append(
                    f"  ({', '.join(ignored_windows)} not in use)", style=palette.muted
                )
            if not acc.switchable and not is_active:
                row.append("  disabled", style=palette.muted)

            # Highest score first. Unknown scores sort last (as -1).
            sort_key = -(score if score is not None else -1.0)
            rows.append((sort_key, acc.number, row))

        text = Text()
        text.append(
            f"Balance (score = weighted quota/h left x{balance.DISPLAY_SCALE:g})",
            style=palette.muted,
        )
        if active_score is not None:
            ledger = tokens.ledger_for(self.app.switcher.backup_dir)
            factor = balance.margin_factor(
                ledger.recent_peak_context(balance.BIG_CONTEXT_LOOKBACK_S),
                settings.balance_big_context,
            )
            need = balance.required_score(active_score, settings.balance_margin, factor)
            text.append(f"  moves at >= {balance.fmt(need)}", style=palette.muted)
            if factor > 1:
                text.append(f" (x{factor:g} margin: large context)", style=palette.muted)
        for _sort_key, _slot, row in sorted(rows):
            text.append(row)
        if model_note:
            text.append(f"\nModel  {model_note}", style=palette.muted)
        return text

    def _model_views(self, snap, settings, models):
        """Apply the engine's model-control filter to each account for display.

        Returns ``(views, note)``:

        * ``views``: ``{slot: (filtered usage, ignored window names)}``, empty
          when model control is off, in which case callers use the raw usage;
        * ``note``: one line for the panel footer. It gives the configured
          default model, what the next tick will change it to, and which
          watched models are in use.

        Uses :func:`modelctl.view_usage` and :func:`modelctl.models_in_use`,
        the same calls the engine makes, so the screen cannot disagree with
        what the engine will do.
        """
        if not settings.model_control or not models:
            return {}, ""

        ledger = tokens.ledger_for(self.app.switcher.backup_dir)
        ledger.refresh_async()  # never block the UI thread on a scan
        in_use = modelctl.models_in_use(
            ledger, models, settings.model_grace_seconds, time.time()
        )
        _readable, configured = modelctl.read_model()
        model_map = modelctl.parse_model_map(settings.model_map)

        views = {}
        active_view = None
        for acc in snap.accounts:
            account_line = (
                acc.policy.threshold
                if acc.policy.threshold is not None
                else settings.threshold
            )
            view = modelctl.view_usage(
                acc.usage.last_good,
                acc.number,
                models=models,
                model_map=model_map,
                configured=configured,
                fallback=settings.fallback_model,
                line=account_line,
                weekly=settings.weekly_threshold,
                recent=in_use,
            )
            views[acc.number] = (view.usage, view.ignored_windows)
            if acc.number == snap.active_number:
                active_view = view

        configured_name = tokens.short_model(configured) if configured else "(not set)"
        note = f"Claude default model: {configured_name}"
        if active_view is not None:
            if active_view.model_to_set and active_view.model_to_set != configured:
                note += f" -> {tokens.short_model(active_view.model_to_set)} next tick"
            if active_view.using_fallback:
                note += " (fallback)"
        busy = [name for name, used in in_use.items() if used]
        if busy:
            note += f" · in use: {', '.join(busy)}"
        else:
            note += f" · in use: no {'/'.join(m.lower() for m in models)}"
        return views, note

    def _tokens_text(self, snap, active_number, settings, palette) -> Text:
        """Token counts from this computer's Claude Code logs, under the balance panel.

        Three parts:

        * totals per account for the last hour and since local midnight;
        * the last three prompts in the past three hours (time, account,
          model, API calls, tokens sent and received, peak context);
        * a note when a reply is being written right now, since balance
          moves wait for it (``autoswitch.balanceIdleSeconds``).
        """
        ledger = tokens.ledger_for(self.app.switcher.backup_dir)
        ledger.refresh_async()
        now = time.time()
        text = Text()

        text.append("\n\nTokens (this computer)  sent    recv  prompts", style=palette.muted)
        # Local midnight today: struct_time's date fields, with the time set
        # to 00:00:00 and DST left for mktime to work out (-1).
        midnight = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
        for label, since in (("last hour", now - 3600), ("today", midnight)):
            totals = ledger.totals(since, active_number)
            if not totals:
                text.append(f"\n {label:<10} none", style=palette.muted)
                continue
            for i, (slot, row) in enumerate(sorted(totals.items())):
                period = label if i == 0 else ""  # label only the first row
                text.append(
                    f"\n {period:<10} #{slot:<3} {tokens.human(row['sent']):>8} "
                    f"{tokens.human(row['received']):>7} {row['prompts']:>6}",
                    style=palette.foreground,
                )

        last_prompts = ledger.prompts(now - 3 * 3600, active_number)[-3:]
        if last_prompts:
            text.append("\nLast prompts", style=palette.muted)
            for p in reversed(last_prompts):  # newest first
                when = time.strftime("%H:%M", time.localtime(p.ts))
                models = ",".join(tokens.short_model(m) for m in p.models)
                text.append(
                    f"\n {when} #{p.account or '?'} {models:<9} "
                    f"{p.calls:>3} calls  sent {tokens.human(p.sent):>7}  "
                    f"recv {tokens.human(p.received):>6}  "
                    f"ctx {tokens.human(p.peak_context)}",
                    style=palette.foreground,
                )

        quiet_for = ledger.seconds_since_write(now)
        reply_in_progress = (
            quiet_for is not None
            and settings.balance_idle_seconds > 0
            and quiet_for < settings.balance_idle_seconds
        )
        if reply_in_progress:
            text.append("\n reply in progress: balance moves wait", style=palette.muted)
        return text
