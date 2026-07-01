"""Tests for claude_swap.notify — the macOS osascript notification helper.

All tests mock ``claude_swap.notify.subprocess.run`` so no real notification is
ever posted, and force ``Platform.detect`` to exercise both the macOS and
non-macOS paths.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from claude_swap.notify import _osa_quote, notify
from claude_swap.models import Platform


# ---------------------------------------------------------------------------
# _osa_quote — AppleScript string escaping (pure)
# ---------------------------------------------------------------------------

class TestOsaQuote:
    def test_plain_text_unchanged(self):
        assert _osa_quote("hello world") == "hello world"

    def test_double_quote_escaped(self):
        assert _osa_quote('say "hi"') == 'say \\"hi\\"'

    def test_backslash_escaped(self):
        assert _osa_quote("a\\b") == "a\\\\b"

    def test_backslash_escaped_before_quote(self):
        # Backslash is escaped first so the result is unambiguous.
        assert _osa_quote('\\"') == '\\\\\\"'


# ---------------------------------------------------------------------------
# notify — macOS path
# ---------------------------------------------------------------------------

class TestNotifyMacos:
    def test_calls_osascript_with_quoted_title_and_message(self):
        with (
            patch("claude_swap.notify.Platform.detect", return_value=Platform.MACOS),
            patch("claude_swap.notify.subprocess.run") as mock_run,
        ):
            notify("My Title", "My Message")

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "osascript"
        assert cmd[1] == "-e"
        script = cmd[2]
        # Both title and message present and correctly placed.
        assert 'display notification "My Message"' in script
        assert 'with title "My Title"' in script

    def test_title_with_double_quote_is_escaped(self):
        with (
            patch("claude_swap.notify.Platform.detect", return_value=Platform.MACOS),
            patch("claude_swap.notify.subprocess.run") as mock_run,
        ):
            notify('Account "2"', "switched")

        script = mock_run.call_args[0][0][2]
        # The inner double-quote is backslash-escaped via _osa_quote.
        assert 'with title "Account \\"2\\""' in script

    def test_message_with_double_quote_is_escaped(self):
        with (
            patch("claude_swap.notify.Platform.detect", return_value=Platform.MACOS),
            patch("claude_swap.notify.subprocess.run") as mock_run,
        ):
            notify("title", 'msg with "quotes"')

        script = mock_run.call_args[0][0][2]
        assert 'display notification "msg with \\"quotes\\""' in script

    def test_run_called_with_timeout_and_check_false(self):
        with (
            patch("claude_swap.notify.Platform.detect", return_value=Platform.MACOS),
            patch("claude_swap.notify.subprocess.run") as mock_run,
        ):
            notify("t", "m")

        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("timeout") == 5
        assert kwargs.get("check") is False
        assert kwargs.get("capture_output") is True


# ---------------------------------------------------------------------------
# notify — non-macOS path (no-op)
# ---------------------------------------------------------------------------

class TestNotifyNonMacos:
    def test_no_op_on_linux(self):
        with (
            patch("claude_swap.notify.Platform.detect", return_value=Platform.LINUX),
            patch("claude_swap.notify.subprocess.run") as mock_run,
        ):
            notify("title", "message")
        mock_run.assert_not_called()

    def test_no_op_on_windows(self):
        with (
            patch("claude_swap.notify.Platform.detect", return_value=Platform.WINDOWS),
            patch("claude_swap.notify.subprocess.run") as mock_run,
        ):
            notify("title", "message")
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# notify — never raises (the daemon must keep running)
# ---------------------------------------------------------------------------

class TestNotifyNeverRaises:
    def test_does_not_raise_on_timeout_expired(self):
        with (
            patch("claude_swap.notify.Platform.detect", return_value=Platform.MACOS),
            patch(
                "claude_swap.notify.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="osascript", timeout=5),
            ),
        ):
            # Must not propagate.
            notify("title", "message")

    def test_does_not_raise_on_os_error(self):
        with (
            patch("claude_swap.notify.Platform.detect", return_value=Platform.MACOS),
            patch(
                "claude_swap.notify.subprocess.run",
                side_effect=OSError("osascript not found"),
            ),
        ):
            notify("title", "message")

    def test_does_not_raise_on_called_process_error(self):
        with (
            patch("claude_swap.notify.Platform.detect", return_value=Platform.MACOS),
            patch(
                "claude_swap.notify.subprocess.run",
                side_effect=subprocess.CalledProcessError(1, "osascript"),
            ),
        ):
            notify("title", "message")
