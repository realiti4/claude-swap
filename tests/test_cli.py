"""Tests for the CLI module."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import __version__


class TestCLI:
    """Test CLI argument parsing and execution."""

    def test_version_flag(self):
        """Test --version flag."""
        result = subprocess.run(
            [sys.executable, "-m", "claude_swap", "--version"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert __version__ in result.stdout

    def test_help_flag(self):
        """Test --help flag."""
        result = subprocess.run(
            [sys.executable, "-m", "claude_swap", "--help"],
            capture_output=True,
            text=True,
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
        )
        assert result.returncode != 0
        assert "required" in result.stderr.lower() or "error" in result.stderr.lower()

    def test_mutually_exclusive_args(self):
        """Test that mutually exclusive args are enforced."""
        result = subprocess.run(
            [sys.executable, "-m", "claude_swap", "--list", "--status"],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert "not allowed" in result.stderr.lower()

    def test_debug_flag_accepted(self):
        """Test that --debug flag is accepted."""
        result = subprocess.run(
            [sys.executable, "-m", "claude_swap", "--debug", "--status"],
            capture_output=True,
            text=True,
        )
        # Should run (may fail due to no config, but flag should be accepted)
        assert "--debug" not in result.stderr or "unrecognized" not in result.stderr


class TestCLICommands:
    """Test individual CLI commands."""

    def test_status_no_account(self, temp_home: Path):
        """Test status command with no account."""
        with patch.dict("os.environ", {"HOME": str(temp_home)}):
            result = subprocess.run(
                [sys.executable, "-m", "claude_swap", "--status"],
                capture_output=True,
                text=True,
                env={**subprocess.os.environ, "HOME": str(temp_home)},
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
            env={**subprocess.os.environ, "HOME": str(temp_home)},
        )
        assert "No accounts" in result.stdout or "managed" in result.stdout.lower()
