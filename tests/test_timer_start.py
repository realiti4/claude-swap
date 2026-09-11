"""Tests for the isolated, non-interactive five-hour timer prompt."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

from claude_swap.timer_start import TimerStartResult, start_five_hour_timer


class FakeSwitcher:
    def __init__(self, backup_dir: Path, current: str = "1"):
        self.backup_dir = backup_dir
        self.current = current
        self.live_pids: list[int] = []

    def current_account_number(self) -> str:
        return self.current

    def live_session_pids_for(self, number: str, email: str) -> list[int]:
        return self.live_pids

    def account_identity(self, number: str) -> dict:
        return {"organizationUuid": "org-2"}


def _success() -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout='{"is_error": false}', stderr=""
    )


def test_active_account_uses_current_profile_and_scrubs_auth_overrides(tmp_path):
    switcher = FakeSwitcher(tmp_path, current="2")
    with (
        patch("claude_swap.timer_start.shutil.which", return_value="/bin/claude"),
        patch("claude_swap.timer_start.subprocess.run", return_value=_success()) as run,
        patch.dict(
            os.environ,
            {
                "CLAUDE_CONFIG_DIR": "/active-profile",
                "ANTHROPIC_API_KEY": "must-not-leak",
                "CLAUDE_CODE_OAUTH_TOKEN": "must-not-leak-either",
            },
        ),
    ):
        result = start_five_hour_timer(switcher, "2", "two@example.com")

    assert result == TimerStartResult(True)
    argv = run.call_args.args[0]
    env = run.call_args.kwargs["env"]
    assert "--print" in argv
    assert "--no-session-persistence" in argv
    assert argv[argv.index("--tools") + 1] == ""
    assert env["CLAUDE_CONFIG_DIR"] == "/active-profile"
    assert "ANTHROPIC_API_KEY" not in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


def test_inactive_account_uses_private_session_without_changing_sharing(tmp_path):
    switcher = FakeSwitcher(tmp_path, current="1")
    profile = tmp_path / "sessions" / "2-two_example.com"
    with (
        patch("claude_swap.timer_start.shutil.which", return_value="/bin/claude"),
        patch("claude_swap.timer_start.subprocess.run", return_value=_success()) as run,
        patch("claude_swap.timer_start.SessionManager") as manager_cls,
        patch.dict(
            os.environ,
            {"CLAUDE_SECURESTORAGE_CONFIG_DIR": "/wrong-profile"},
        ),
    ):
        manager_cls.return_value.setup_session.return_value = (
            profile,
            "2",
            "two@example.com",
        )
        result = start_five_hour_timer(switcher, "2", "two@example.com")

    assert result.success is True
    manager_cls.return_value.setup_session.assert_called_once_with(
        "2",
        share=False,
        share_history=False,
        sync_sharing=False,
    )
    env = run.call_args.kwargs["env"]
    assert env["CLAUDE_CONFIG_DIR"] == str(profile)
    assert "CLAUDE_SECURESTORAGE_CONFIG_DIR" not in env


def test_live_profile_identity_drift_refuses_to_message_wrong_account(tmp_path):
    switcher = FakeSwitcher(tmp_path, current="1")
    switcher.live_pids = [123]
    with (
        patch("claude_swap.timer_start.shutil.which", return_value="/bin/claude"),
        patch(
            "claude_swap.timer_start.session_identity_drifted", return_value=True
        ),
        patch("claude_swap.timer_start.SessionManager") as manager_cls,
        patch("claude_swap.timer_start.subprocess.run") as run,
    ):
        result = start_five_hour_timer(switcher, "2", "two@example.com")

    assert result.success is False
    assert "different account" in (result.error or "")
    manager_cls.assert_not_called()
    run.assert_not_called()


def test_nonzero_exit_and_json_error_are_failures(tmp_path):
    switcher = FakeSwitcher(tmp_path, current="2")
    failed = subprocess.CompletedProcess(args=[], returncode=7, stdout="", stderr="no")
    api_error = subprocess.CompletedProcess(
        args=[], returncode=0, stdout='{"is_error": true}', stderr=""
    )
    with (
        patch("claude_swap.timer_start.shutil.which", return_value="/bin/claude"),
        patch(
            "claude_swap.timer_start.subprocess.run",
            side_effect=[failed, api_error],
        ),
    ):
        first = start_five_hour_timer(switcher, "2", "two@example.com")
        second = start_five_hour_timer(switcher, "2", "two@example.com")

    assert first == TimerStartResult(False, "Claude Code exited with status 7")
    assert second == TimerStartResult(False, "Claude Code reported an API error")


def test_concurrent_default_switch_leaves_claim_retryable(tmp_path):
    switcher = FakeSwitcher(tmp_path, current="2")

    def switch_during_launch(*args, **kwargs):
        switcher.current = "1"
        return _success()

    with (
        patch("claude_swap.timer_start.shutil.which", return_value="/bin/claude"),
        patch(
            "claude_swap.timer_start.subprocess.run",
            side_effect=switch_during_launch,
        ),
    ):
        result = start_five_hour_timer(switcher, "2", "two@example.com")

    assert result.success is False
    assert "active account changed" in (result.error or "")


def test_missing_claude_is_actionable_and_does_not_spawn(tmp_path):
    with (
        patch("claude_swap.timer_start.shutil.which", return_value=None),
        patch("claude_swap.timer_start.subprocess.run") as run,
    ):
        result = start_five_hour_timer(
            FakeSwitcher(tmp_path, current="2"), "2", "two@example.com"
        )

    assert result.success is False
    assert "PATH" in (result.error or "")
    run.assert_not_called()
