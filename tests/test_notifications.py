"""Tests for desktop-notification message content (notifications.py).

Only covers message formatting via the mocked `notify()` dispatch point —
platform-specific toast/osascript/notify-send calls are exercised manually,
not under test.
"""

from __future__ import annotations

from unittest.mock import patch

from claude_swap.notifications import notify_quarantined


class TestNotifyQuarantined:
    def test_message_includes_the_actual_slot_number(self):
        with patch("claude_swap.notifications.notify") as mock_notify:
            notify_quarantined("2", "user@example.com", "invalid_grant")
        (title, body), kwargs = mock_notify.call_args
        assert "user@example.com" in body
        assert "invalid_grant" in body
        assert "cswap add --slot 2" in body
        assert "cswap add --slot N" not in body
        assert kwargs.get("urgency") == "normal"
