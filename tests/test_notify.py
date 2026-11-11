"""Tests for notify.command dispatch (notify.py) and its settings section."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from claude_swap import notify
from claude_swap.autoswitch import (
    AllExhaustedEvent,
    ErrorEvent,
    PollEvent,
    SwitchEvent,
)
from claude_swap.notify import EventNotifier, parse_event_kinds
from claude_swap.settings import (
    NOTIFY_DEFAULT_EVENTS,
    NotifySettings,
    load_notify_settings,
    set_setting,
    settings_path,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, dict[str, str]]] = []

    def __call__(self, command: str, payload: str, env: dict[str, str]) -> int:
        self.calls.append((command, json.loads(payload), env))
        return 0


def _switch(**kw) -> SwitchEvent:
    return SwitchEvent(
        trigger="proactive",
        from_ref={"number": 1, "email": "a@example.com"},
        to_ref={"number": 2, "email": "b@example.com"},
        **kw,
    )


def _notifier(events: str = NOTIFY_DEFAULT_EVENTS, clock=None):
    rec = Recorder()
    n = EventNotifier(
        NotifySettings(command="notify-me", events=events),
        clock=clock or FakeClock(),
        runner=rec,
        background=False,
    )
    return n, rec


class TestSettings:
    def test_defaults_when_absent(self, tmp_path: Path):
        assert load_notify_settings(tmp_path) == NotifySettings()
        assert NotifySettings().command is None

    def test_reads_section(self, tmp_path: Path):
        settings_path(tmp_path).write_text(
            json.dumps({"notify": {"command": " say hi ", "events": "switch"}})
        )
        assert load_notify_settings(tmp_path) == NotifySettings(
            command="say hi", events="switch"
        )

    def test_bad_types_fall_back(self, tmp_path: Path):
        settings_path(tmp_path).write_text(
            json.dumps({"notify": {"command": 5, "events": ""}})
        )
        assert load_notify_settings(tmp_path) == NotifySettings()

    def test_config_set(self, tmp_path: Path):
        set_setting(tmp_path, "notify.command", "echo hi")
        assert load_notify_settings(tmp_path).command == "echo hi"

    def test_no_command_no_notifier(self, tmp_path: Path):
        assert EventNotifier.for_backup_dir(tmp_path) is None
        set_setting(tmp_path, "notify.command", "true")
        assert EventNotifier.for_backup_dir(tmp_path) is not None


class TestSelection:
    def test_parse_kinds(self):
        assert parse_event_kinds(" Switch , error,,") == {"switch", "error"}
        assert parse_event_kinds(None) == frozenset()

    def test_switch_sent_with_payload_and_env(self):
        n, rec = _notifier()
        n.handle(_switch())
        [(command, payload, env)] = rec.calls
        assert command == "notify-me"
        assert payload["event"] == "switch"
        assert payload["to"]["number"] == 2
        assert env["CSWAP_EVENT"] == "switch"
        assert env["CSWAP_TITLE"] == "Auto-switched account"
        assert env["CSWAP_MESSAGE"].startswith("Switched Account-1 -> Account-2")

    def test_unselected_kind_not_sent(self):
        n, rec = _notifier()
        n.handle(PollEvent(active=None, headroom={}, threshold=90.0))
        assert rec.calls == []

    def test_dry_run_switch_not_sent(self):
        n, rec = _notifier()
        n.handle(_switch(dry_run=True))
        assert rec.calls == []

    def test_events_setting_narrows(self):
        n, rec = _notifier(events="switch")
        n.handle(ErrorEvent(message="boom"))
        n.handle(_switch())
        assert [c[1]["event"] for c in rec.calls] == ["switch"]


class TestRepeats:
    def test_switch_never_held_back(self):
        n, rec = _notifier()
        n.handle(_switch())
        n.handle(_switch())
        assert len(rec.calls) == 2

    def test_recurring_event_held_back_until_window_passes(self):
        clock = FakeClock()
        n, rec = _notifier(clock=clock)
        n.handle(AllExhaustedEvent(earliest_reset_at="2026-11-11T10:00:00Z"))
        # A changed reset time is still the same condition.
        n.handle(AllExhaustedEvent(earliest_reset_at="2026-11-11T11:30:00Z"))
        assert len(rec.calls) == 1
        clock.now += notify.REPEAT_S
        n.handle(AllExhaustedEvent(earliest_reset_at="2026-11-11T12:00:00Z"))
        assert len(rec.calls) == 2

    def test_distinct_errors_each_sent(self):
        n, rec = _notifier()
        n.handle(ErrorEvent(message="timeout"))
        n.handle(ErrorEvent(message="http-500"))
        n.handle(ErrorEvent(message="timeout"))
        assert len(rec.calls) == 2


class TestFailures:
    def test_runner_exception_is_swallowed(self, caplog):
        def boom(*_):
            raise OSError("no shell")

        n = EventNotifier(
            NotifySettings(command="x"), runner=boom, background=False
        )
        n.handle(_switch())  # must not raise
        assert "notify.command failed" in caplog.text

    def test_unreadable_settings_disable_notifier(self, tmp_path: Path):
        settings_path(tmp_path).write_text("{not json")
        assert EventNotifier.for_backup_dir(tmp_path) is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell command")
class TestRealShell:
    def test_command_receives_stdin_and_env(self, tmp_path: Path):
        out = tmp_path / "out.txt"
        n = EventNotifier(
            NotifySettings(
                command=f'cat > "{out}"; printf "%s|%s" "$CSWAP_EVENT" "$CSWAP_TITLE" >> "{out}"'
            ),
            background=False,
        )
        n.handle(_switch())
        text = out.read_text()
        payload, tail = text.rsplit("}", 1)
        assert json.loads(payload + "}")["event"] == "switch"
        assert tail == "switch|Auto-switched account"

    def test_failing_command_is_logged(self, caplog):
        n = EventNotifier(
            NotifySettings(command="echo nope >&2; exit 3"), background=False
        )
        n.handle(_switch())
        assert "notify.command exited 3: nope" in caplog.text
