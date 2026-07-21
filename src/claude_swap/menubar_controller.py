"""Serialized, AppKit-agnostic controller for the native macOS menu-bar app.

The controller owns the asynchronous boundary between AppKit and claude-swap's
blocking core.  It never reads credentials, ``.claude.json``, Keychain, or
account metadata itself: coherent account state arrives only from
:class:`~claude_swap.snapshot_source.SnapshotSource`.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from collections.abc import Callable
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from claude_swap.autoswitch import AutoSwitchEngine, AutoSwitchEvent
from claude_swap.exceptions import CredentialReadError
from claude_swap.macos_terminal import TerminalLaunchResult, launch_terminal
from claude_swap.models import AccountsSnapshot
from claude_swap.menubar_viewmodel import (
    MenuBarPopoverViewModel,
    MenuBarSettings,
    _adapt_snapshot,
    format_title,
    format_usage_log,
    parse_switch_history,
    popover_view_model,
)
from claude_swap.settings import load_settings, set_setting
from claude_swap.snapshot_source import SnapshotSource

if TYPE_CHECKING:
    from claude_swap.switcher import ClaudeAccountSwitcher


RenderCallback = Callable[[MenuBarPopoverViewModel, str], None]
MessageCallback = Callable[[str, str], None]
MainDispatcher = Callable[[Callable[[], None]], None]
TerminalLauncher = Callable[[str], TerminalLaunchResult]
RevealLog = Callable[[Path], None]


@dataclass(frozen=True)
class ActiveAccountProbe:
    """The active global identity read by the lightweight external-switch watcher."""

    identity: tuple[str, str] | None


def _run_reveal_log(path: Path) -> None:
    """Reveal the log or its parent in Finder from a background worker."""
    target = path if path.exists() else path.parent
    subprocess.run(["open", "-R", str(target)], check=False)


class MenuBarController:
    """Coordinate native menu-bar state without blocking AppKit's main thread.

    The one-worker executor serializes snapshots and every user mutation.  The
    auto-switch engine has its own long-lived thread, but display snapshots use
    ``store_only=True`` whenever that engine is present so the two collectors do
    not race for a usage poll.
    """

    def __init__(
        self,
        switcher: ClaudeAccountSwitcher,
        *,
        settings_path: Path | None = None,
        log_path: Path | None = None,
        snapshot_source: SnapshotSource | None = None,
        terminal_launcher: TerminalLauncher = launch_terminal,
        reveal_log: RevealLog = _run_reveal_log,
        dispatch_main: MainDispatcher | None = None,
        executor: Executor | None = None,
    ) -> None:
        self.switcher = switcher
        self.settings_path = settings_path or switcher.backup_dir / "menubar_settings.json"
        self.log_path = log_path or switcher.backup_dir / "claude-swap.log"
        # Disk-backed preferences load on the serialized worker in start().
        # Defaults keep the initial status item responsive before that completes.
        self.settings = MenuBarSettings()
        self._auto_switch_threshold = 90
        self.snapshot_source = snapshot_source or SnapshotSource(switcher)
        self.terminal_launcher = terminal_launcher
        self.reveal_log = reveal_log
        self._dispatch_main = dispatch_main or (lambda callback: callback())
        self._executor = executor or ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="claude-swap-menubar"
        )
        self._owns_executor = executor is None
        self._state_lock = threading.Lock()
        self._refreshing = False
        self._active_probe_in_flight = False
        self._active_identity: tuple[str, str] | None = None
        self._pending_work = 0
        self._stop_when_idle: Callable[[], None] | None = None
        self._stopped = False
        self._engine: AutoSwitchEngine | None = None
        self._engine_thread: threading.Thread | None = None
        self._engine_running = False
        self._engine_starting = False
        self._snapshot: AccountsSnapshot | None = None
        self._view_model = MenuBarPopoverViewModel((), None)
        self._title = format_title(None, None, self.settings)
        self._history: tuple[str, ...] = ()
        self._renderer: RenderCallback | None = None
        self._message: MessageCallback | None = None
        self._last_usage_log: dict[str, tuple[float | None, float | None]] = {}
        self._logger = logging.getLogger("claude-swap")

    @property
    def view_model(self) -> MenuBarPopoverViewModel:
        """Last complete store-backed popover model."""
        with self._state_lock:
            return self._view_model

    @property
    def title(self) -> str:
        """Last formatted status-item title."""
        with self._state_lock:
            return self._title

    @property
    def history(self) -> tuple[str, ...]:
        """Last asynchronously loaded switch-history entries."""
        with self._state_lock:
            return self._history

    def bind_ui(self, renderer: RenderCallback, message: MessageCallback) -> None:
        """Bind main-thread UI callbacks and immediately render known state."""
        self._renderer = renderer
        self._message = message
        renderer(self.view_model, self.title)

    def start(self) -> None:
        """Load preferences then schedule the initial store-backed snapshot."""
        self._submit(self._load_settings_worker, self._settings_failed)
        self.refresh_async()

    def _load_settings_worker(self) -> None:
        settings = MenuBarSettings.load(self.settings_path)
        core_settings = load_settings(self.switcher.backup_dir)
        self.settings = settings
        self._auto_switch_threshold = int(core_settings.threshold)
        if settings.auto_switch_enabled:
            self._start_engine_worker()

    def stop(self, when_idle: Callable[[], None] | None = None) -> None:
        """Stop background activity and invoke ``when_idle`` after work is safe to end.

        Account mutations are transactional, so Quit must not terminate the
        process in the middle of a worker operation. The completion callback is
        dispatched on the UI thread after queued work has finished or cancelled.
        """
        with self._state_lock:
            self._stopped = True
            self._stop_when_idle = when_idle
            engine, self._engine = self._engine, None
        if engine is not None:
            engine.stop()
        if self._owns_executor and isinstance(self._executor, ThreadPoolExecutor):
            self._executor.shutdown(wait=False, cancel_futures=True)
        self._notify_when_idle()

    def refresh_async(self, *, full: bool = False) -> None:
        """Take a store-governed snapshot on the serialized worker."""
        with self._state_lock:
            if self._stopped or self._refreshing:
                return
            self._refreshing = True
        self._submit(
            lambda: self._refresh_worker(full),
            lambda error: self._refresh_failed(error, user_requested=full),
        )

    def detect_external_active_account(self) -> None:
        """Refresh when another process changes the global Claude Code login."""
        with self._state_lock:
            if self._stopped or self._active_probe_in_flight:
                return
            self._active_probe_in_flight = True
        self._submit(
            lambda: ActiveAccountProbe(self.switcher._get_current_account()),
            self._active_probe_failed,
        )

    def make_active(self, slot: str) -> None:
        """Explicitly activate a selected slot using only ``switch_to``."""
        self._submit(lambda: self.switcher.switch_to(slot, json_output=True), self._switch_failed)

    def launch_isolated_session(self, slot: str) -> None:
        """Launch the local CLI session command in a separate Terminal window."""
        self._submit(lambda: self.terminal_launcher(slot), self._terminal_complete)

    def rotate(self, strategy: str | None) -> None:
        """Run one legacy rotate strategy on the serialized worker."""
        self._submit(
            lambda: self.switcher.switch(strategy=strategy, json_output=True), self._switch_failed
        )

    def add_current_login(self) -> None:
        """Capture the current login through the core switcher on a worker."""
        self._submit(lambda: self.switcher.add_account(slot=None), self._mutation_failed)

    def add_setup_token(self, email: str, token: str) -> None:
        """Add a setup token through the core switcher on a worker."""
        self._submit(
            lambda: self.switcher.add_account_from_token(token=token, email=email, slot=None),
            self._mutation_failed,
        )

    def set_disabled(self, slot: str, disabled: bool) -> None:
        """Set a slot's rotation eligibility on the serialized worker."""
        self._submit(
            lambda: self.switcher.set_account_disabled(slot, disabled), self._mutation_failed
        )

    def remove_account(self, slot: str) -> None:
        """Remove an already-confirmed slot on the serialized worker."""
        self._submit(
            lambda: self.switcher.remove_account(slot, assume_yes=True), self._mutation_failed
        )

    def refresh_current_credentials(self) -> None:
        """Re-capture the active login through the switcher's supported API."""
        self._submit(lambda: self.switcher.add_account(slot=None), self._credentials_failed)

    def load_history_async(self) -> None:
        """Read and parse the switch log away from the AppKit main thread."""
        self._submit(self._history_worker, self._history_failed)

    def reveal_log_async(self) -> None:
        """Reveal the log in Finder on the serialized worker."""
        self._submit(lambda: self.reveal_log(self.log_path), self._mutation_failed)

    def update_title_preferences(
        self,
        *,
        show_account_name: bool | None = None,
        title_pct: str | None = None,
        title_scoped: bool | None = None,
    ) -> None:
        """Persist display preferences and update the native title asynchronously."""
        def save() -> None:
            if show_account_name is not None:
                self.settings.show_account_name = show_account_name
            if title_pct is not None:
                self.settings.title_pct = title_pct
            if title_scoped is not None:
                self.settings.title_scoped = title_scoped
            self.settings.save(self.settings_path)

        self._submit(save, self._settings_failed)

    def set_refresh_interval(self, seconds: int) -> None:
        """Persist a validated refresh cadence; the UI owns timer replacement."""
        def save() -> None:
            self.settings.refresh_interval = seconds
            self.settings.save(self.settings_path)

        self._submit(save, self._settings_failed)

    def set_auto_switch_enabled(self, enabled: bool) -> None:
        """Persist and start or stop the auto engine without blocking AppKit."""
        def update() -> None:
            self.settings.auto_switch_enabled = enabled
            self.settings.save(self.settings_path)
            if enabled:
                self._start_engine_worker()
            else:
                self._stop_engine_worker()

        self._submit(update, self._settings_failed)

    def set_auto_switch_threshold(self, percent: int) -> None:
        """Persist an auto-switch threshold and apply it to a running engine."""
        def update() -> None:
            set_setting(self.switcher.backup_dir, "autoswitch.threshold", str(percent))
            self._auto_switch_threshold = percent
            with self._state_lock:
                engine = self._engine
            if engine is not None:
                engine.apply_threshold(float(percent))
                engine.wake()

        self._submit(update, self._settings_failed)

    def auto_switch_threshold(self) -> int:
        """Return the worker-loaded core threshold for renderer-only display."""
        return self._auto_switch_threshold

    def _submit(
        self,
        work: Callable[[], object],
        error_handler: Callable[[BaseException], None],
    ) -> Future[object] | None:
        with self._state_lock:
            if self._stopped:
                return None
            self._pending_work += 1
        try:
            future = self._executor.submit(work)
        except BaseException:
            self._work_finished()
            raise
        future.add_done_callback(lambda done: self._complete(done, error_handler))
        return future

    def _complete(
        self, future: Future[object], error_handler: Callable[[BaseException], None]
    ) -> None:
        try:
            result = future.result()
        except BaseException as error:
            with self._state_lock:
                stopped = self._stopped
            if not stopped:
                self._dispatch_main(lambda: error_handler(error))
        else:
            self._dispatch_main(lambda: self._worker_succeeded(result))
        finally:
            self._work_finished()

    def _work_finished(self) -> None:
        with self._state_lock:
            self._pending_work -= 1
        self._notify_when_idle()

    def _notify_when_idle(self) -> None:
        with self._state_lock:
            if (
                not self._stopped
                or self._pending_work != 0
                or self._engine_running
                or self._stop_when_idle is None
            ):
                return
            callback, self._stop_when_idle = self._stop_when_idle, None
        self._dispatch_main(callback)

    def _worker_succeeded(self, result: object) -> None:
        if isinstance(result, AccountsSnapshot):
            active = next((account for account in result.accounts if account.is_active), None)
            with self._state_lock:
                self._snapshot = result
                # A snapshot has no managed active account when the user logged
                # in externally. Preserve the watcher identity in that case so
                # its next one-second probe does not trigger another refresh.
                if active is not None:
                    self._active_identity = (active.email, active.org_uuid)
                self._refreshing = False
            self._render()
            return
        if isinstance(result, ActiveAccountProbe):
            with self._state_lock:
                changed = result.identity != self._active_identity
                self._active_probe_in_flight = False
                self._active_identity = result.identity
            if changed:
                self.refresh_async()
            return
        if isinstance(result, tuple) and all(isinstance(item, str) for item in result):
            with self._state_lock:
                self._history = result
            self._render()
            return
        if isinstance(result, TerminalLaunchResult):
            if result.launched:
                self._notify("Isolated session launched", f"Terminal is running cswap run {result.slot}.")
            else:
                self._notify("Couldn't launch isolated session", result.error_message or "Terminal launch failed.")
            return
        if isinstance(result, dict) and result.get("switched"):
            self._notify("Account switched", "Switch takes effect within about 30 seconds.")
        self._render()
        self.refresh_async()

    def _refresh_worker(self, full: bool) -> AccountsSnapshot:
        with self._state_lock:
            store_only = self._engine is not None or self._engine_starting
        snapshot = self.snapshot_source.take(full=full, store_only=store_only)
        self._log_usage(snapshot)
        return snapshot

    def _refresh_failed(self, error: BaseException, *, user_requested: bool = False) -> None:
        with self._state_lock:
            self._refreshing = False
        self._logger.debug("menubar snapshot failed", exc_info=error)
        if user_requested:
            self._notify("Couldn't refresh quota data", str(error))

    def _active_probe_failed(self, error: BaseException) -> None:
        with self._state_lock:
            self._active_probe_in_flight = False
        self._logger.debug("menubar active-account watcher failed", exc_info=error)

    def _history_worker(self) -> tuple[str, ...]:
        try:
            text = self.log_path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        return tuple(parse_switch_history(text))

    def _history_failed(self, error: BaseException) -> None:
        self._notify("Switch history unavailable", str(error))

    def _terminal_complete(self, error: BaseException) -> None:
        self._notify("Couldn't launch isolated session", str(error))

    def _switch_failed(self, error: BaseException) -> None:
        self._notify("Couldn't switch account", str(error))

    def _mutation_failed(self, error: BaseException) -> None:
        self._notify("claude-swap action failed", str(error))

    def _credentials_failed(self, error: BaseException) -> None:
        if isinstance(error, CredentialReadError):
            self._notify(
                "Couldn't refresh credentials",
                "macOS blocked Keychain access. Quit and relaunch cswap --menubar from Terminal.",
            )
            return
        self._mutation_failed(error)

    def _settings_failed(self, error: BaseException) -> None:
        self._notify("Couldn't save menu-bar settings", str(error))

    def _start_engine_worker(self) -> None:
        with self._state_lock:
            if self._engine is not None or self._engine_starting:
                return
            self._engine_starting = True
        try:
            engine = AutoSwitchEngine(
                self.switcher,
                load_settings(self.switcher.backup_dir),
                self._on_engine_event,
                dry_run=False,
            )
            thread = threading.Thread(
                target=self._run_engine, args=(engine,), daemon=True, name="claude-swap-auto"
            )
            with self._state_lock:
                if self._stopped:
                    return
                self._engine = engine
                self._engine_thread = thread
                self._engine_running = True
            thread.start()
        finally:
            with self._state_lock:
                self._engine_starting = False

    def _run_engine(self, engine: AutoSwitchEngine) -> None:
        try:
            engine.run_loop()
        except BaseException:
            self._logger.debug("menubar auto-switch engine crashed", exc_info=True)
        finally:
            with self._state_lock:
                if self._engine is engine:
                    self._engine = None
                self._engine_thread = None
                self._engine_running = False
            self._notify_when_idle()

    def _stop_engine_worker(self) -> None:
        with self._state_lock:
            engine, self._engine = self._engine, None
        if engine is not None:
            engine.stop()

    def _on_engine_event(self, event: AutoSwitchEvent) -> None:
        self._dispatch_main(lambda: self._handle_engine_event(event))

    def _handle_engine_event(self, event: AutoSwitchEvent) -> None:
        if event.kind == "switch" and not getattr(event, "dry_run", False):
            self._notify("Auto-switched account", event.human())
            self.refresh_async()
        elif event.kind in {"account-quarantined", "all-exhausted", "config-warning"}:
            self._notify("claude-swap", event.human())

    def _log_usage(self, snapshot: AccountsSnapshot) -> None:
        for account in snapshot.accounts:
            usage = account.usage.last_good
            key = (
                self._usage_pct(usage, "five_hour"),
                self._usage_pct(usage, "seven_day"),
            )
            if key == (None, None) or self._last_usage_log.get(account.number) == key:
                continue
            line = format_usage_log(account.email, usage)
            if line:
                self._logger.info(line)
                self._last_usage_log[account.number] = key

    @staticmethod
    def _usage_pct(usage: object, key: str) -> float | None:
        if not isinstance(usage, dict):
            return None
        window = usage.get(key)
        if not isinstance(window, dict):
            return None
        value = window.get("pct")
        return float(value) if isinstance(value, int | float) else None

    def _render(self) -> None:
        with self._state_lock:
            snapshot = self._snapshot
        if snapshot is not None:
            model = popover_view_model(snapshot)
            legacy = _adapt_snapshot(snapshot)
            title = format_title(
                legacy["active_email"],
                legacy["active_usage"],
                self.settings,
                alias=legacy["active_alias"] if isinstance(legacy["active_alias"], str) else None,
            )
            with self._state_lock:
                self._view_model, self._title = model, title
        if self._renderer is not None:
            self._renderer(self.view_model, self.title)

    def _notify(self, title: str, message: str) -> None:
        if self._message is not None:
            self._message(title, message)
