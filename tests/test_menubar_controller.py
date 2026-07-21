"""Controller contract tests without importing AppKit."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from claude_swap.macos_terminal import TerminalLaunchResult
from claude_swap.menubar_controller import MenuBarController
from claude_swap.models import AccountSnapshot, AccountsSnapshot
from claude_swap.usage_store import UsageEntry


class _SnapshotSource:
    def __init__(self, snapshot: AccountsSnapshot) -> None:
        self.snapshot = snapshot
        self.calls: list[tuple[bool, bool]] = []

    def take(self, *, full: bool = False, store_only: bool = False) -> AccountsSnapshot:
        self.calls.append((full, store_only))
        return self.snapshot


class _Switcher:
    def __init__(self, backup_dir: Path) -> None:
        self.backup_dir = backup_dir
        self.calls: list[tuple[object, ...]] = []
        self.current_account: tuple[str, str] | None = ("one@example.test", "")

    def _get_current_account(self) -> tuple[str, str] | None:
        return self.current_account

    def switch_to(self, slot: str, *, json_output: bool = False) -> dict[str, bool]:
        self.calls.append(("switch_to", slot, json_output))
        return {"switched": True}

    def switch(self, *, strategy: str | None = None, json_output: bool = False) -> dict[str, bool]:
        self.calls.append(("switch", strategy, json_output))
        return {"switched": True}

    def add_account(self, *, slot: int | None = None) -> None:
        self.calls.append(("add_account", slot))

    def add_account_from_token(self, *, token: str, email: str, slot: int | None = None) -> None:
        self.calls.append(("add_account_from_token", token, email, slot))

    def set_account_disabled(self, slot: str, disabled: bool) -> None:
        self.calls.append(("set_account_disabled", slot, disabled))

    def remove_account(self, slot: str, *, assume_yes: bool) -> None:
        self.calls.append(("remove_account", slot, assume_yes))


def _snapshot() -> AccountsSnapshot:
    return AccountsSnapshot(
        active_number="1",
        accounts=(
            AccountSnapshot(
                number="1",
                email="one@example.test",
                org_name="",
                org_uuid="",
                is_active=True,
                kind="oauth",
                switchable=True,
                usage=UsageEntry(last_good={"five_hour": {"pct": 10.0}}, age_s=1.0),
            ),
        ),
        taken_at=1.0,
    )


def _wait_for(event: threading.Event) -> None:
    assert event.wait(1), "serialized worker did not complete"


def test_make_active_calls_only_direct_switch_to(tmp_path: Path) -> None:
    switcher = _Switcher(tmp_path)
    source = _SnapshotSource(_snapshot())
    rendered = threading.Event()
    controller = MenuBarController(switcher, snapshot_source=source)
    controller.bind_ui(lambda _model, _title: rendered.set(), lambda _title, _message: None)
    rendered.clear()

    controller.make_active("2")

    _wait_for(rendered)
    assert switcher.calls == [("switch_to", "2", True)]
    controller.stop()


def test_isolated_session_uses_terminal_launcher_not_session_manager(tmp_path: Path) -> None:
    switcher = _Switcher(tmp_path)
    source = _SnapshotSource(_snapshot())
    launched = threading.Event()
    slots: list[str] = []

    def terminal(slot: str) -> TerminalLaunchResult:
        slots.append(slot)
        launched.set()
        return TerminalLaunchResult(launched=True, slot=slot, command=f"cswap run {slot}")

    controller = MenuBarController(switcher, snapshot_source=source, terminal_launcher=terminal)
    controller.launch_isolated_session("4")

    _wait_for(launched)
    assert slots == ["4"]
    assert switcher.calls == []
    controller.stop()


def test_rotate_uses_json_output_without_implicitly_capturing_a_login(tmp_path: Path) -> None:
    switcher = _Switcher(tmp_path)
    source = _SnapshotSource(_snapshot())
    rendered = threading.Event()
    controller = MenuBarController(switcher, snapshot_source=source)
    controller.bind_ui(lambda _model, _title: rendered.set(), lambda _title, _message: None)
    rendered.clear()

    controller.rotate("next-available")

    _wait_for(rendered)
    assert ("switch", "next-available", True) in switcher.calls
    controller.stop()


def test_snapshot_refresh_uses_store_only_while_engine_is_running(tmp_path: Path) -> None:
    switcher = _Switcher(tmp_path)
    source = _SnapshotSource(_snapshot())
    rendered = threading.Event()
    controller = MenuBarController(switcher, snapshot_source=source)
    controller.bind_ui(lambda _model, _title: rendered.set(), lambda _title, _message: None)
    rendered.clear()
    # The controller only needs presence to choose the SnapshotSource read mode.
    controller._engine = object()  # type: ignore[assignment]

    controller.refresh_async(full=True)

    _wait_for(rendered)
    assert source.calls == [(True, True)]
    assert controller.view_model.active_number == "1"
    controller._engine = None
    controller.stop()


def test_external_active_account_change_requests_a_store_governed_refresh(tmp_path: Path) -> None:
    switcher = _Switcher(tmp_path)
    source = _SnapshotSource(_snapshot())
    rendered = threading.Event()
    controller = MenuBarController(switcher, snapshot_source=source)
    controller.bind_ui(lambda _model, _title: rendered.set(), lambda _title, _message: None)
    controller._worker_succeeded(_snapshot())
    rendered.clear()
    switcher.current_account = ("two@example.test", "")

    controller.detect_external_active_account()

    _wait_for(rendered)
    assert source.calls == [(False, False)]
    controller.stop()


def test_external_unmanaged_login_refreshes_once_without_a_watcher_loop(tmp_path: Path) -> None:
    switcher = _Switcher(tmp_path)
    switcher.current_account = ("outside@example.test", "")
    unmanaged = AccountsSnapshot(
        active_number=None,
        accounts=(
            AccountSnapshot(
                number="1",
                email="one@example.test",
                org_name="",
                org_uuid="",
                is_active=False,
                kind="oauth",
                switchable=True,
                usage=UsageEntry(),
            ),
        ),
        taken_at=1.0,
    )
    source = _SnapshotSource(unmanaged)
    rendered = threading.Event()
    controller = MenuBarController(switcher, snapshot_source=source)
    controller.bind_ui(lambda _model, _title: rendered.set(), lambda _title, _message: None)
    controller._worker_succeeded(unmanaged)
    rendered.clear()

    controller.detect_external_active_account()
    _wait_for(rendered)
    controller.detect_external_active_account()
    threading.Event().wait(0.05)

    assert source.calls == [(False, False)]
    controller.stop()


def test_explicit_refresh_surfaces_a_snapshot_error(tmp_path: Path) -> None:
    class FailingSnapshotSource:
        def take(self, *, full: bool = False, store_only: bool = False) -> AccountsSnapshot:
            raise RuntimeError("quota endpoint unavailable")

    switcher = _Switcher(tmp_path)
    messages: list[tuple[str, str]] = []
    controller = MenuBarController(switcher, snapshot_source=FailingSnapshotSource())
    controller.bind_ui(lambda _model, _title: None, lambda title, message: messages.append((title, message)))

    controller.refresh_async(full=True)

    for _ in range(20):
        if messages:
            break
        threading.Event().wait(0.05)
    assert messages == [("Couldn't refresh quota data", "quota endpoint unavailable")]
    controller.stop()


def test_stop_waits_for_the_auto_switch_thread_before_terminating(tmp_path: Path) -> None:
    switcher = _Switcher(tmp_path)
    controller = MenuBarController(switcher, snapshot_source=_SnapshotSource(_snapshot()))
    completed = threading.Event()
    with controller._state_lock:
        controller._engine_running = True

    controller.stop(completed.set)

    assert not completed.is_set()
    with controller._state_lock:
        controller._engine_running = False
    controller._notify_when_idle()
    assert completed.is_set()


def test_load_history_renders_updated_history_on_main_dispatch(tmp_path: Path) -> None:
    switcher = _Switcher(tmp_path)
    source = _SnapshotSource(_snapshot())
    log_path = tmp_path / "claude-swap.log"
    log_path.write_text(
        "2026-07-20 10:00:00 - Switched from account 1 to 2\n", encoding="utf-8"
    )
    dispatched = threading.Event()
    callbacks: list[Callable[[], None]] = []
    rendered_history: list[tuple[str, ...]] = []

    def dispatch_main(callback: Callable[[], None]) -> None:
        callbacks.append(callback)
        dispatched.set()

    controller = MenuBarController(
        switcher,
        snapshot_source=source,
        log_path=log_path,
        dispatch_main=dispatch_main,
    )
    controller.bind_ui(
        lambda _model, _title: rendered_history.append(controller.history),
        lambda _title, _message: None,
    )
    rendered_history.clear()

    controller.load_history_async()

    _wait_for(dispatched)
    assert rendered_history == []
    callbacks.pop()()
    assert controller.history
    assert rendered_history == [controller.history]
    controller.stop()
