"""Tests for the CLI module."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import __version__
from claude_swap import cli

# src layout: ensure subprocess can find claude_swap
_SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")


def _subprocess_env(**extra: str) -> dict[str, str]:
    """Build env dict with PYTHONPATH pointing at src/."""
    env = {**os.environ, **extra}
    env["PYTHONPATH"] = _SRC_DIR + os.pathsep + env.get("PYTHONPATH", "")
    return env


class TestCLI:
    """Test CLI argument parsing and execution."""

    def test_version_flag(self):
        """Test --version flag."""
        result = subprocess.run(
            [sys.executable, "-m", "claude_swap", "--version"],
            capture_output=True,
            text=True,
            env=_subprocess_env(),
        )
        assert result.returncode == 0
        assert __version__ in result.stdout

    def test_help_flag(self):
        """Test --help flag."""
        result = subprocess.run(
            [sys.executable, "-m", "claude_swap", "--help"],
            capture_output=True,
            text=True,
            env=_subprocess_env(),
        )
        assert result.returncode == 0
        assert "Multi-Account Switcher" in result.stdout
        assert "--add-account" in result.stdout
        assert "--switch" in result.stdout
        assert "--list" in result.stdout
        assert "--status" in result.stdout

    def test_no_args_shows_error(self):
        """Test that running without args shows error."""
        result = subprocess.run(
            [sys.executable, "-m", "claude_swap"],
            capture_output=True,
            text=True,
            env=_subprocess_env(),
        )
        assert result.returncode != 0
        assert "required" in result.stderr.lower() or "error" in result.stderr.lower()

    def test_mutually_exclusive_args(self):
        """Test that mutually exclusive args are enforced."""
        result = subprocess.run(
            [sys.executable, "-m", "claude_swap", "--list", "--status"],
            capture_output=True,
            text=True,
            env=_subprocess_env(),
        )
        assert result.returncode != 0
        assert "not allowed" in result.stderr.lower()

    def test_debug_flag_accepted(self):
        """Test that --debug flag is accepted."""
        result = subprocess.run(
            [sys.executable, "-m", "claude_swap", "--debug", "--status"],
            capture_output=True,
            text=True,
            env=_subprocess_env(),
        )
        # Should run (may fail due to no config, but flag should be accepted)
        assert "--debug" not in result.stderr or "unrecognized" not in result.stderr

    def test_token_status_flag_requires_list(self, capsys):
        """--token-status should only be accepted alongside --list."""
        with patch.object(sys, "argv", ["claude-swap", "--token-status", "--status"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()

        assert excinfo.value.code == 2
        assert "--token-status can only be used with --list" in capsys.readouterr().err

    def test_token_status_flag_is_forwarded_to_list(self):
        """--list --token-status should call list_accounts(show_token_status=True)."""
        with patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["claude-swap", "--list", "--token-status"]), \
             patch("os.geteuid", return_value=1000), \
             patch("claude_swap.update_check.check_for_update", return_value=None):
            cli.main()

        switcher_cls.return_value.list_accounts.assert_called_once_with(
            show_token_status=True,
        )

    def test_slot_flag_requires_add_account(self, capsys):
        """--slot should only be accepted alongside --add-account."""
        with patch.object(sys, "argv", ["claude-swap", "--list", "--slot", "3"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()

        assert excinfo.value.code == 2
        assert "--slot can only be used with --add-account" in capsys.readouterr().err

    def test_slot_flag_in_help(self):
        """--slot should appear in help output."""
        result = subprocess.run(
            [sys.executable, "-m", "claude_swap", "--help"],
            capture_output=True,
            text=True,
            env=_subprocess_env(),
        )
        assert "--slot" in result.stdout

    def test_account_flag_requires_export(self, capsys):
        """--account should only be accepted alongside --export."""
        with patch.object(
            sys, "argv", ["claude-swap", "--list", "--account", "1"]
        ):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 2
        assert "--account can only be used with --export" in capsys.readouterr().err

    def test_force_flag_requires_import(self, capsys):
        """--force should only be accepted alongside --import."""
        with patch.object(sys, "argv", ["claude-swap", "--list", "--force"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 2
        assert "--force can only be used with --import" in capsys.readouterr().err

    def test_export_and_import_are_mutually_exclusive(self):
        """--export and --import cannot be combined."""
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "claude_swap",
                "--export",
                "/tmp/x",
                "--import",
                "/tmp/x",
            ],
            capture_output=True,
            text=True,
            env=_subprocess_env(),
        )
        assert result.returncode != 0
        assert "not allowed" in result.stderr.lower()

    def test_export_in_help(self):
        """--export and --import should appear in help output."""
        result = subprocess.run(
            [sys.executable, "-m", "claude_swap", "--help"],
            capture_output=True,
            text=True,
            env=_subprocess_env(),
        )
        assert "--export" in result.stdout
        assert "--import" in result.stdout

    def test_export_dispatch_calls_transfer(self):
        """--export dispatches into transfer.export_accounts."""
        with patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch("claude_swap.transfer.export_accounts") as export_fn, \
             patch.object(
                 sys, "argv", ["claude-swap", "--export", "/tmp/x", "--account", "2"]
             ), \
             patch("os.geteuid", return_value=1000), \
             patch("claude_swap.update_check.check_for_update", return_value=None):
            cli.main()
        export_fn.assert_called_once_with(
            switcher_cls.return_value, "/tmp/x", account="2", full=False
        )

    def test_full_flag_requires_export(self, capsys):
        """--full should only be accepted alongside --export."""
        with patch.object(sys, "argv", ["claude-swap", "--list", "--full"]):
            with pytest.raises(SystemExit) as exc_info:
                cli.main()
        assert exc_info.value.code == 2
        assert "--full can only be used with --export" in capsys.readouterr().err

    def test_full_flag_dispatches_with_full_true(self):
        """--export --full should pass full=True into export_accounts."""
        with patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch("claude_swap.transfer.export_accounts") as export_fn, \
             patch.object(
                 sys, "argv", ["claude-swap", "--export", "/tmp/x", "--full"]
             ), \
             patch("os.geteuid", return_value=1000), \
             patch("claude_swap.update_check.check_for_update", return_value=None):
            cli.main()
        export_fn.assert_called_once_with(
            switcher_cls.return_value, "/tmp/x", account=None, full=True
        )

    def test_import_dispatch_calls_transfer(self):
        """--import dispatches into transfer.import_accounts."""
        with patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch("claude_swap.transfer.import_accounts") as import_fn, \
             patch.object(
                 sys, "argv", ["claude-swap", "--import", "/tmp/x", "--force"]
             ), \
             patch("os.geteuid", return_value=1000), \
             patch("claude_swap.update_check.check_for_update", return_value=None):
            cli.main()
        import_fn.assert_called_once_with(
            switcher_cls.return_value, "/tmp/x", force=True
        )

    def test_upgrade_in_help(self):
        """--upgrade should appear in help output."""
        result = subprocess.run(
            [sys.executable, "-m", "claude_swap", "--help"],
            capture_output=True,
            text=True,
            env=_subprocess_env(),
        )
        assert "--upgrade" in result.stdout

    def test_upgrade_dispatches_without_constructing_switcher(self):
        """--upgrade should call run_self_upgrade and skip switcher init."""
        with patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch(
                 "claude_swap.update_check.run_self_upgrade", return_value=0
             ) as upgrade_fn, \
             patch.object(sys, "argv", ["claude-swap", "--upgrade"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()

        assert excinfo.value.code == 0
        upgrade_fn.assert_called_once_with()
        switcher_cls.assert_not_called()


class TestCLICommands:
    """Test individual CLI commands."""

    def test_status_no_account(self, temp_home: Path):
        """Test status command with no account."""
        result = subprocess.run(
            [sys.executable, "-m", "claude_swap", "--status"],
            capture_output=True,
            text=True,
            env=_subprocess_env(HOME=str(temp_home)),
        )
        # Should succeed even with no account
        assert "No active Claude account" in result.stdout or result.returncode == 0

    def test_list_no_accounts(self, temp_home: Path):
        """Test list command with no accounts."""
        result = subprocess.run(
            [sys.executable, "-m", "claude_swap", "--list"],
            capture_output=True,
            text=True,
            input="n\n",  # Answer 'n' to first-run prompt
            env=_subprocess_env(HOME=str(temp_home)),
        )
        assert "No accounts" in result.stdout or "managed" in result.stdout.lower()
