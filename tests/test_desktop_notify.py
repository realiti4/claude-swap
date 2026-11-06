"""Tests for which auto-switch events notify, and how they read."""

from __future__ import annotations

from claude_swap.autoswitch import (
    AllExhaustedEvent,
    ConfigWarningEvent,
    ErrorEvent,
    NoSwitchEvent,
    QuarantineEvent,
    SwitchEvent,
    UnquarantineEvent,
)
from claude_swap.desktop_notify import Notifier, notification_for, toast_xml


def _switch(trigger="at-limit", **kw):
    return SwitchEvent(
        trigger=trigger,
        from_ref={"number": 1, "email": "work@example.com"},
        to_ref={"number": 2, "email": "home@example.com"},
        **kw,
    )


def test_switch_reads_naturally():
    key, title, body = notification_for(_switch())
    assert title == "Switched to home@example.com"
    assert body == "work@example.com hit its limit."


def test_proactive_switch_says_running_low():
    assert notification_for(_switch("proactive"))[2] == "work@example.com was running low."


def test_dry_run_switch_is_silent():
    assert notification_for(_switch(dry_run=True)) is None


def test_quarantine_explains_recovery():
    _, title, body = notification_for(QuarantineEvent(number="3", email="a@x.com", reason="refresh token revoked"))
    assert title == "Account needs re-login"
    assert "a@x.com" in body and "cswap add --slot 3" in body


def test_unquarantine_notifies():
    assert notification_for(UnquarantineEvent(number="3", email="a@x.com"))[1] == "Account back in rotation"


def test_all_exhausted_without_reset_time():
    _, title, body = notification_for(AllExhaustedEvent(earliest_reset_at=None))
    assert title == "All accounts are at their limit"
    assert body == "No reset time is known yet."


def test_all_exhausted_shows_local_reset_time():
    body = notification_for(AllExhaustedEvent(earliest_reset_at="2026-10-12T09:00:00Z"))[2]
    assert body.startswith("Earliest reset at ") and ":" in body


def test_config_warning_notifies():
    _, title, body = notification_for(ConfigWarningEvent(message="no account reports model 'x'"))
    assert title == "Auto-switch setting ignored" and "model 'x'" in body


def test_transient_error_and_routine_events_are_silent():
    assert notification_for(ErrorEvent(message="timeout", transient=True)) is None
    assert notification_for(NoSwitchEvent(reason="below-threshold")) is None


def test_fatal_error_says_auto_switch_stopped():
    assert notification_for(ErrorEvent(message="boom", transient=False))[1] == "Auto-switch stopped"


def _notifier(focused=False, now=[0.0]):
    sent = []
    n = Notifier(send=lambda t, b: sent.append((t, b)), is_focused=lambda: focused, clock=lambda: now[0])
    return n, sent


def test_same_alert_not_repeated_within_an_hour():
    now = [0.0]
    n, _ = _notifier(now=now)
    assert n.handle(_switch()) is True
    now[0] = 600
    assert n.handle(_switch()) is False
    now[0] = 3601
    assert n.handle(_switch()) is True


def test_no_notification_while_window_focused():
    n, _ = _notifier(focused=True)
    assert n.handle(_switch()) is False


def test_send_failure_is_swallowed():
    def boom(title, body):
        raise OSError("no powershell")

    n = Notifier(send=boom, clock=lambda: 0.0)
    n._safe_send("t", "b")  # must not raise


def test_toast_xml_escapes_text():
    xml = toast_xml("A & B", "<b>x</b>")
    assert "A &amp; B" in xml and "&lt;b&gt;x&lt;/b&gt;" in xml
