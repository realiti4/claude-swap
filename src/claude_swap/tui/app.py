"""The claude-swap Textual application.

Owns the snapshot poll loop and every mutating action (switch/add/remove),
so the dashboard, the auto view, and the command palette all drive the same
code paths. Blocking switcher work always runs in thread workers — the UI
loop never touches file locks, keychain subprocesses, or the network.
"""

from __future__ import annotations

from functools import partial

from textual.app import App
from textual.reactive import reactive
from textual.worker import WorkerState

from claude_swap.models import AccountsSnapshot
from claude_swap.settings import load_settings
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.tui.autoview import AutoScreen
from claude_swap.tui.dashboard import DashboardScreen, WatchScreen
from claude_swap.tui.data import ActionResult, SnapshotSource, run_action
from claude_swap.tui.modals import AddTokenModal, ConfirmModal, OutputModal, TokenForm
from claude_swap.tui.theme import CSWAP_DARK


class CswapApp(App):
    """claude-swap interactive dashboard."""

    TITLE = "claude-swap"
    CSS_PATH = "cswap.tcss"
    # No command palette: actions live in the dashboard's nested menu, in
    # their own context — not in a global searchable list. This also drops
    # Textual's system commands (theme picker included; there is one theme).
    ENABLE_COMMAND_PALETTE = False

    POLL_INTERVAL_S = 3.0  # matches the old watch view's recapture cadence

    snapshot: reactive[AccountsSnapshot | None] = reactive(None)
    busy: reactive[bool] = reactive(False)

    def __init__(
        self, switcher: ClaudeAccountSwitcher, *, start: str = "dashboard"
    ) -> None:
        super().__init__()
        self.switcher = switcher
        self._start = start  # "dashboard" | "watch" (`cswap watch`)
        self.source = SnapshotSource(switcher)
        self._store_only = False
        self._full_next = False
        self._refreshing = False
        self._last_refresh_error = ""
        # The auto-switch threshold, drawn as a tick on the status strip's
        # bars everywhere. Missing/invalid settings fall back to the default.
        try:
            self.threshold_pct: float | None = load_settings(
                switcher.backup_dir
            ).threshold
        except Exception:
            self.threshold_pct = None

    def on_mount(self) -> None:
        self.register_theme(CSWAP_DARK)
        self.theme = "cswap-dark"
        self.push_screen(DashboardScreen())
        if self._start == "watch":
            # Stacked over the dashboard so Esc lands there, not on exit.
            self.push_screen(WatchScreen())
        self.set_interval(self.POLL_INTERVAL_S, self._tick)
        self._tick()

    # -- snapshot poll loop ---------------------------------------------------

    def _tick(self) -> None:
        """Start a refresh pass unless one is already in flight."""
        if self._refreshing:
            return
        self._refreshing = True
        full, self._full_next = self._full_next, False
        self.run_worker(
            partial(self._refresh_blocking, full, self._store_only),
            thread=True,
            group="refresh",
            exit_on_error=False,
            name="snapshot-refresh",
        )

    def _refresh_blocking(self, full: bool, store_only: bool) -> None:
        snap = self.source.take(full=full, store_only=store_only)
        self.call_from_thread(self._apply_snapshot, snap)

    def _apply_snapshot(self, snap: AccountsSnapshot) -> None:
        self._refreshing = False
        self._last_refresh_error = ""
        self.snapshot = snap

    def request_refresh(self, *, full: bool = False) -> None:
        if full:
            self._full_next = True
        self._tick()

    def set_store_only(self, value: bool) -> None:
        """Auto screen: the engine fetches, the poller only reads the store."""
        self._store_only = value
        self.request_refresh()

    def on_worker_state_changed(self, event) -> None:
        if event.state is not WorkerState.ERROR:
            return
        if event.worker.group == "refresh":
            self._refreshing = False
            msg = str(event.worker.error)
            if msg != self._last_refresh_error:
                self._last_refresh_error = msg
                self.notify(
                    f"Refresh failed: {msg}", severity="warning", timeout=6
                )
        elif event.worker.group == "action":
            self.busy = False
            self.notify(f"Action failed: {event.worker.error}", severity="error")
        elif event.worker.group == "engine":
            self.notify(
                f"Auto-switch engine stopped: {event.worker.error}",
                severity="error",
            )

    # -- mutating actions (single-flight, captured, off-thread) ---------------

    def _start_action(self, label: str, fn, *, show_output: bool = False) -> None:
        if self.busy:
            self.notify("Another action is still running", severity="warning")
            return
        self.busy = True
        self.run_worker(
            partial(self._action_blocking, label, fn, show_output),
            thread=True,
            group="action",
            exit_on_error=False,
            name=label,
        )

    def _action_blocking(self, label: str, fn, show_output: bool) -> None:
        result = run_action(fn)
        self.call_from_thread(self._action_done, label, result, show_output)

    def _action_done(
        self, label: str, result: ActionResult, show_output: bool
    ) -> None:
        self.busy = False
        self.request_refresh()
        if not result.ok:
            self.push_screen(OutputModal(f"{label} — failed", result.output))
            return
        payload = result.payload or {}
        if "switched" in payload:
            if payload.get("switched"):
                to = payload.get("to") or {}
                target = to.get("email") or f"account {to.get('number')}"
                self.notify(f"Switched to {target}", title="Switch")
            else:
                reason = str(payload.get("reason") or "no switch performed")
                self.notify(reason, title="No switch", severity="warning")
            return
        if show_output and result.output.strip():
            self.push_screen(OutputModal(label, result.output))
        elif result.first_line:
            self.notify(result.first_line)

    # -- account operations ----------------------------------------------------

    def do_switch(self, number: str) -> None:
        self._start_action(
            f"Switch to account {number}",
            partial(self.switcher.switch_to, number, json_output=True),
        )

    def action_switch_best(self) -> None:
        self._start_action(
            "Switch (best)",
            partial(self.switcher.switch, strategy="best", json_output=True),
        )

    def do_toggle_disabled(self, number: str) -> None:
        """Hold the account out of auto-rotation, or return it — reads its
        current state from the live snapshot to pick the direction."""
        snap = self.snapshot
        acc = next(
            (a for a in (snap.accounts if snap else ()) if a.number == number), None
        )
        if acc is None:
            return
        target = not acc.disabled
        verb = "Disable" if target else "Enable"
        self._start_action(
            f"{verb} account {number}",
            partial(self.switcher.set_account_disabled, number, target),
        )

    def confirm_remove(self, number: str, email: str) -> None:
        self.push_screen(
            ConfirmModal(
                f"Remove account {number} ({email})?\n\n"
                "Its stored credentials and config backup are deleted.",
                title="Remove account",
                yes_label="Remove",
            ),
            partial(self._on_remove_confirm, number),
        )

    def _on_remove_confirm(self, number: str, confirmed: bool | None) -> None:
        if confirmed:
            self._start_action(
                f"Remove account {number}",
                partial(self.switcher.remove_account, number, assume_yes=True),
            )

    def action_add_current(self) -> None:
        self.push_screen(
            ConfirmModal(
                "Back up the current Claude Code login as a managed account?\n\n"
                "If this account is already managed, its stored credentials "
                "are refreshed in place.",
                title="Add account",
                yes_label="Add",
            ),
            self._on_add_confirm,
        )

    def _on_add_confirm(self, confirmed: bool | None) -> None:
        if confirmed:
            self._start_action(
                "Add current login",
                partial(self.switcher.add_account),
                show_output=True,
            )

    def action_add_token(self) -> None:
        self.push_screen(AddTokenModal(), self._on_token_form)

    def _on_token_form(self, form: TokenForm | None) -> None:
        if form is None:
            return
        run = partial(
            self._start_action,
            "Add account from token",
            partial(
                self.switcher.add_account_from_token,
                token=form.token,
                email=form.email,
                slot=form.slot,
                assume_yes=True,
            ),
            show_output=True,
        )
        occupant = self._slot_occupant(form.slot)
        if occupant is not None:
            self.push_screen(
                ConfirmModal(
                    f"Slot {form.slot} is occupied by {occupant}. Overwrite?",
                    title="Overwrite slot",
                    yes_label="Overwrite",
                ),
                lambda confirmed: run() if confirmed else None,
            )
        else:
            run()

    def _slot_occupant(self, slot: int | None) -> str | None:
        if slot is None or self.snapshot is None:
            return None
        for acc in self.snapshot.accounts:
            if acc.number == str(slot):
                return acc.email
        return None

    # -- navigation -------------------------------------------------------------

    def action_refresh_full(self) -> None:
        self.request_refresh(full=True)
        self.notify("Refreshing usage…", timeout=2)

    def action_open_auto(self) -> None:
        if isinstance(self.screen, AutoScreen):
            return
        self.push_screen(AutoScreen())

    def action_open_watch(self) -> None:
        if isinstance(self.screen, WatchScreen):
            return
        self.push_screen(WatchScreen())
