"""Tests for the headless ``cswap auto`` desktop notifications."""

from __future__ import annotations

import argparse
import subprocess
from unittest import mock

from claude_swap.autoswitch import (
    AllExhaustedEvent,
    NoSwitchEvent,
    PollEvent,
    QuarantineEvent,
    SwitchEvent,
)
from claude_swap.notifications import (
    _applescript_escape,
    _NOTIFY_TIMEOUT_SECONDS,
    make_notifying_emit,
    send_notification,
)
from claude_swap.settings import AutoSwitchSettings, merged_with_cli


def _switch_event() -> SwitchEvent:
    return SwitchEvent(
        trigger="proactive",
        from_ref={"number": "1", "email": "a@example.com"},
        to_ref={"number": "2", "email": "b@example.com"},
    )


class TestMakeNotifyingEmit:
    def test_inner_callback_always_runs(self):
        inner = mock.Mock()
        with mock.patch("claude_swap.notifications.send_notification") as sent:
            emit = make_notifying_emit(inner)
            emit(NoSwitchEvent(reason="below-threshold", detail="87% < 90%"))
        inner.assert_called_once()
        sent.assert_not_called()

    def test_switch_quarantine_exhausted_notify(self):
        inner = mock.Mock()
        events = [
            _switch_event(),
            QuarantineEvent(number="3", email="c@example.com", reason="dead-refresh-token"),
            AllExhaustedEvent(earliest_reset_at="2026-08-30T17:00:00Z"),
        ]
        with mock.patch("claude_swap.notifications.send_notification") as sent:
            emit = make_notifying_emit(inner)
            for event in events:
                emit(event)
        assert inner.call_count == 3
        assert sent.call_count == 3
        bodies = [call.args[1] for call in sent.call_args_list]
        assert bodies == [event.human() for event in events]

    def test_poll_and_no_switch_stay_silent(self):
        inner = mock.Mock()
        poll = PollEvent(
            active={"number": "1", "email": "a@example.com"},
            headroom={"1": 20.0},
            threshold=90.0,
        )
        with mock.patch("claude_swap.notifications.send_notification") as sent:
            emit = make_notifying_emit(inner)
            emit(poll)
            emit(NoSwitchEvent(reason="below-threshold"))
        assert inner.call_count == 2
        sent.assert_not_called()

    def test_dispatch_failure_does_not_break_the_stream(self):
        inner = mock.Mock()
        with mock.patch(
            "claude_swap.notifications.send_notification",
            side_effect=OSError("notification daemon gone"),
        ):
            emit = make_notifying_emit(inner)
            emit(_switch_event())  # must not raise
        inner.assert_called_once()


class TestAppleScriptEscaping:
    def test_quotes_and_backslashes(self):
        assert _applescript_escape('say "hi" \\ done') == 'say \\"hi\\" \\\\ done'

    def test_newline_becomes_literal_escape(self):
        assert _applescript_escape("line1\nline2") == "line1\\nline2"


class TestSendNotification:
    def test_dispatcher_exception_is_swallowed(self, caplog):
        with mock.patch.dict(
            "claude_swap.notifications._DISPATCHERS",
            {"darwin": mock.Mock(side_effect=OSError("no osascript"))},
        ):
            send_notification("t", "b")  # must not raise

    def test_subprocess_timeout_is_swallowed(self):
        boom = subprocess.TimeoutExpired(cmd="osascript", timeout=1)
        with mock.patch.dict(
            "claude_swap.notifications._DISPATCHERS",
            {"darwin": mock.Mock(side_effect=boom)},
        ):
            send_notification("t", "b")  # must not raise

    def test_unknown_platform_is_a_debug_noop(self):
        with mock.patch.dict(
            "claude_swap.notifications._DISPATCHERS", {}, clear=True
        ):
            send_notification("t", "b")  # must not raise

    def test_darwin_dispatcher_passes_escaped_script(self):
        with mock.patch("claude_swap.notifications.subprocess.run") as run:
            with mock.patch("sys.platform", "darwin"):
                send_notification('ti"tle', 'bo\ndy')
        run.assert_called_once()
        argv = run.call_args.args[0]
        assert argv[:2] == ["osascript", "-e"]
        assert '\\"' in argv[2] and "\\n" in argv[2]
        assert run.call_args.kwargs["timeout"] == _NOTIFY_TIMEOUT_SECONDS
        assert run.call_args.kwargs["check"] is False


class TestSettingsWiring:
    def test_notify_defaults_off_and_clamps_bool(self):
        assert AutoSwitchSettings().notify is False
        coerced = merged_with_cli(AutoSwitchSettings(), argparse.Namespace())
        assert coerced.notify is False

    def test_cli_flag_overrides(self):
        args = argparse.Namespace(notify=True)
        assert merged_with_cli(AutoSwitchSettings(), args).notify is True

    def test_cli_none_leaves_setting_untouched(self):
        settings = AutoSwitchSettings(notify=True)
        args = argparse.Namespace(notify=None)
        assert merged_with_cli(settings, args).notify is True
