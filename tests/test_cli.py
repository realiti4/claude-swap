"""Tests for the CLI module."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_swap import __version__
from claude_swap import cli
from claude_swap.switcher import ClaudeAccountSwitcher

# src layout: ensure subprocess can find claude_swap
_SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")

# A throwaway HOME for subprocesses. The in-process autouse Keychain/HOME guards
# do NOT reach child processes, so a spawned ``python -m claude_swap`` would
# otherwise resolve to the developer's real ``~/.claude-swap-backup`` and run the
# data migration against real accounts (touching the real Keychain on macOS). An empty,
# isolated HOME has no ``sequence.json`` → the migration skips before any Keychain
# access, and no ``.claude.json`` → no account to read.
_ISOLATED_HOME = tempfile.mkdtemp(prefix="cswap-subproc-home-")


def _subprocess_env(**extra: str) -> dict[str, str]:
    """Build env dict with PYTHONPATH pointing at src/ and an isolated HOME.

    HOME/USERPROFILE default to a throwaway dir so the spawned CLI never touches
    the developer's real backup dir or Keychain; callers may still override HOME
    explicitly (e.g. ``_subprocess_env(HOME=str(temp_home))``), in which case
    USERPROFILE mirrors it unless the caller set USERPROFILE too.
    """
    env = {**os.environ, **extra}
    env["PYTHONPATH"] = _SRC_DIR + os.pathsep + env.get("PYTHONPATH", "")
    if "HOME" not in extra:
        env["HOME"] = _ISOLATED_HOME
        env["USERPROFILE"] = _ISOLATED_HOME
    elif "USERPROFILE" not in extra:
        env["USERPROFILE"] = extra["HOME"]
    # CLAUDE_CONFIG_DIR / XDG_DATA_HOME bypass HOME in path resolution, so a
    # developer with either exported would otherwise point the spawned CLI back
    # at real config/backup paths (and on macOS, the real Keychain). Drop them
    # unless a caller set them deliberately.
    for var in ("CLAUDE_CONFIG_DIR", "XDG_DATA_HOME"):
        if var not in extra:
            env.pop(var, None)
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
            json_output=False,
        )

    def test_strategy_best_requires_switch(self, capsys):
        """--strategy should only be accepted alongside --switch."""
        with patch.object(sys, "argv", ["claude-swap", "--strategy", "best", "--list"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()

        assert excinfo.value.code == 2
        assert "--strategy can only be used with --switch" in capsys.readouterr().err

    def test_strategy_next_available_requires_switch(self, capsys):
        """--strategy next-available should only be accepted alongside --switch."""
        with patch.object(sys, "argv", ["claude-swap", "--strategy", "next-available", "--list"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()

        assert excinfo.value.code == 2
        assert "--strategy can only be used with --switch" in capsys.readouterr().err

    def test_strategy_rejects_unknown_value(self, capsys):
        """argparse rejects strategies outside the known choices."""
        with patch.object(sys, "argv", ["claude-swap", "--switch", "--strategy", "bogus"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()

        assert excinfo.value.code == 2

    def test_switch_strategy_forwarded(self):
        """--switch --strategy best forwards the strategy to switch()."""
        with patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["claude-swap", "--switch", "--strategy", "best"]), \
             patch("os.geteuid", return_value=1000), \
             patch("claude_swap.update_check.check_for_update", return_value=None):
            cli.main()

        switcher_cls.return_value.switch.assert_called_once_with(
            strategy="best", json_output=False
        )

    def test_plain_switch_passes_no_strategy(self):
        """Bare --switch forwards strategy=None."""
        with patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["claude-swap", "--switch"]), \
             patch("os.geteuid", return_value=1000), \
             patch("claude_swap.update_check.check_for_update", return_value=None):
            cli.main()

        switcher_cls.return_value.switch.assert_called_once_with(
            strategy=None, json_output=False
        )

    def test_slot_flag_requires_add_account(self, capsys):
        """--slot should only be accepted alongside --add-account or --add-token."""
        with patch.object(sys, "argv", ["claude-swap", "--list", "--slot", "3"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()

        assert excinfo.value.code == 2
        assert "--slot can only be used with --add-account or --add-token" in capsys.readouterr().err

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

    def test_add_token_without_email_dispatches_with_none(self, temp_home: Path, capsys):
        """--add-token without --email should dispatch with email=None (defaulted by switcher)."""
        from claude_swap.switcher import ClaudeAccountSwitcher

        with patch.object(
            sys, "argv", ["claude-swap", "--add-token", "sk-ant-oat01-abc"],
        ), patch.object(
            ClaudeAccountSwitcher, "add_account_from_token"
        ) as mock_add:
            cli.main()

        mock_add.assert_called_once_with(
            token="sk-ant-oat01-abc", email=None, slot=None
        )

    def test_email_without_add_token_errors(self, capsys):
        """--email without --add-token should exit with a clear error."""
        with patch.object(sys, "argv", ["claude-swap", "--list", "--email", "u@x.com"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 2
        assert "--email can only be used with --add-token" in capsys.readouterr().err

    def test_add_token_dispatches_to_switcher(self, temp_home: Path, capsys):
        """--add-token with --email should call add_account_from_token."""
        from claude_swap.switcher import ClaudeAccountSwitcher

        with patch.object(
            sys, "argv",
            ["claude-swap", "--add-token", "mytoken", "--email", "u@example.com"],
        ), patch.object(
            ClaudeAccountSwitcher, "add_account_from_token"
        ) as mock_add:
            cli.main()

        mock_add.assert_called_once_with(
            token="mytoken", email="u@example.com", slot=None
        )

    def test_add_token_with_slot(self, temp_home: Path, capsys):
        """--add-token --slot should forward slot to add_account_from_token."""
        from claude_swap.switcher import ClaudeAccountSwitcher

        with patch.object(
            sys, "argv",
            ["claude-swap", "--add-token", "tok", "--email", "u@example.com", "--slot", "3"],
        ), patch.object(
            ClaudeAccountSwitcher, "add_account_from_token"
        ) as mock_add:
            cli.main()

        mock_add.assert_called_once_with(
            token="tok", email="u@example.com", slot=3
        )

    def test_add_token_in_help(self):
        """--add-token should appear in help output."""
        result = subprocess.run(
            [sys.executable, "-m", "claude_swap", "--help"],
            capture_output=True,
            text=True,
            env=_subprocess_env(),
        )
        assert "--add-token" in result.stdout
        assert "--email" in result.stdout


class TestRunCommand:
    """`cswap run` pre-dispatch: parsing, forwarding, and dispatch."""

    def _dispatch(self, argv: list[str]):
        """Run cli.main() with a fake SessionManager; returns recorded calls."""
        calls = []

        class FakeSessionManager:
            def __init__(self, switcher):
                calls.append(("init", switcher))

            def run(self, identifier, claude_args, share=True):
                calls.append(("run", identifier, claude_args, share))

        with patch("claude_swap.session.SessionManager", FakeSessionManager), \
             patch("claude_swap.cli.ClaudeAccountSwitcher"), \
             patch("os.geteuid", return_value=1000), \
             patch.object(sys, "argv", ["claude-swap", *argv]):
            cli.main()
        return calls

    def test_run_dispatches_with_defaults(self):
        calls = self._dispatch(["run", "2"])
        assert ("run", "2", [], True) in calls

    def test_run_by_email(self):
        calls = self._dispatch(["run", "user@example.com"])
        assert ("run", "user@example.com", [], True) in calls

    def test_no_share_flag(self):
        calls = self._dispatch(["run", "2", "--no-share"])
        assert ("run", "2", [], False) in calls

    def test_tail_forwarded_verbatim(self):
        calls = self._dispatch(["run", "2", "--", "--resume", "--model", "x"])
        assert ("run", "2", ["--resume", "--model", "x"], True) in calls

    def test_tail_may_contain_run_flags(self):
        """Args after `--` are NOT parsed by cswap, even if they look like ours."""
        calls = self._dispatch(["run", "2", "--", "--no-share"])
        assert ("run", "2", ["--no-share"], True) in calls

    def test_run_without_account_errors(self, capsys):
        with patch.object(sys, "argv", ["claude-swap", "run"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 2
        assert "NUM|EMAIL" in capsys.readouterr().err

    def test_run_unknown_flag_errors(self, capsys):
        with patch.object(sys, "argv", ["claude-swap", "run", "2", "--bogus"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 2

    def test_run_help(self, capsys):
        with patch.object(sys, "argv", ["claude-swap", "run", "--help"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 0
        out = capsys.readouterr().out
        assert "--no-share" in out
        assert "this terminal only" in out

    def test_main_help_mentions_run(self):
        result = subprocess.run(
            [sys.executable, "-m", "claude_swap", "--help"],
            capture_output=True,
            text=True,
            env=_subprocess_env(),
        )
        assert "run 2" in result.stdout

class TestAutoCommand:
    """Tests for _auto_command dispatch and cswap auto/watch/_auto-daemon."""

    def _dispatch(self, argv: list[str]):
        """Call cli._auto_command directly."""
        from claude_swap import cli as _cli
        return _cli._auto_command(argv)

    # -----------------------------------------------------------------------
    # auto status
    # -----------------------------------------------------------------------

    def test_auto_status_default_prints_config(self, capsys, temp_home: Path):
        """``cswap auto`` with no subverb shows config fields."""
        from claude_swap.auto_switch import AutoSwitchConfig, save_config
        from claude_swap.models import Platform
        from claude_swap.paths import get_backup_root

        backup = get_backup_root()
        backup.mkdir(parents=True, exist_ok=True)
        save_config(AutoSwitchConfig(enabled=True), backup_root=backup)
        mock_sw = self._mock_switcher_with_backup(backup)

        with patch("claude_swap.cli.ClaudeAccountSwitcher", return_value=mock_sw), \
             patch("claude_swap.cli.Platform.detect", return_value=Platform.LINUX):
            self._dispatch(["auto"])

        out = capsys.readouterr().out
        assert "enabled" in out
        assert "session_threshold" in out
        assert "weekly_threshold" in out

    def test_auto_status_explicit_subverb(self, capsys, temp_home: Path):
        """``cswap auto status`` is equivalent to ``cswap auto``."""
        from claude_swap.models import Platform
        from claude_swap.paths import get_backup_root

        backup = get_backup_root()
        backup.mkdir(parents=True, exist_ok=True)
        mock_sw = self._mock_switcher_with_backup(backup)

        with patch("claude_swap.cli.ClaudeAccountSwitcher", return_value=mock_sw), \
             patch("claude_swap.cli.Platform.detect", return_value=Platform.LINUX):
            self._dispatch(["auto", "status"])

        out = capsys.readouterr().out
        assert "enabled" in out

    def test_auto_status_shows_monitoring_line(self, capsys, temp_home: Path):
        """status shows a 'monitoring:' line; offline state is surfaced."""
        from claude_swap.auto_switch_state import MonitorState, save_state
        from claude_swap.models import Platform
        from claude_swap.paths import get_backup_root

        backup = get_backup_root()
        backup.mkdir(parents=True, exist_ok=True)
        # Persist an offline state.
        save_state(
            MonitorState(
                last_online_ts=1.0, consecutive_failures=3, offline_notified=True
            ),
            backup_root=backup,
        )
        mock_sw = self._mock_switcher_with_backup(backup)

        with patch("claude_swap.cli.ClaudeAccountSwitcher", return_value=mock_sw), \
             patch("claude_swap.cli.Platform.detect", return_value=Platform.LINUX):
            self._dispatch(["auto", "status"])

        out = capsys.readouterr().out
        assert "monitoring:" in out
        assert "offline" in out.lower()

    def test_auto_status_shows_last_switch_when_present(self, capsys, temp_home: Path):
        """status shows a 'last switch:' line when state has one."""
        import time

        from claude_swap.auto_switch_state import MonitorState, save_state
        from claude_swap.models import Platform
        from claude_swap.paths import get_backup_root

        backup = get_backup_root()
        backup.mkdir(parents=True, exist_ok=True)
        save_state(
            MonitorState(
                last_online_ts=time.time(),
                last_switch={"account": "2", "ts": time.time(), "reason": "5h-threshold"},
            ),
            backup_root=backup,
        )
        mock_sw = self._mock_switcher_with_backup(backup)

        with patch("claude_swap.cli.ClaudeAccountSwitcher", return_value=mock_sw), \
             patch("claude_swap.cli.Platform.detect", return_value=Platform.LINUX):
            self._dispatch(["auto", "status"])

        out = capsys.readouterr().out
        assert "last switch:" in out
        assert "account-2" in out

    def test_auto_status_no_last_switch_line_when_absent(self, capsys, temp_home: Path):
        """No 'last switch:' line when state has never recorded one."""
        from claude_swap.models import Platform
        from claude_swap.paths import get_backup_root

        backup = get_backup_root()
        backup.mkdir(parents=True, exist_ok=True)
        mock_sw = self._mock_switcher_with_backup(backup)

        with patch("claude_swap.cli.ClaudeAccountSwitcher", return_value=mock_sw), \
             patch("claude_swap.cli.Platform.detect", return_value=Platform.LINUX):
            self._dispatch(["auto", "status"])

        out = capsys.readouterr().out
        assert "last switch:" not in out

    def test_auto_status_shows_last_known_usage(self, capsys, temp_home: Path):
        """status renders per-account last-known 5h/7d% from monitor state."""
        import time

        from claude_swap.auto_switch_state import MonitorState, save_state
        from claude_swap.models import Platform
        from claude_swap.paths import get_backup_root

        backup = get_backup_root()
        backup.mkdir(parents=True, exist_ok=True)
        save_state(
            MonitorState(
                last_online_ts=time.time(),
                last_usage={
                    "3": {
                        "usage": {"five_hour": {"pct": 25.0}, "seven_day": {"pct": 12.0}},
                        "fetched_at": time.time(),
                    },
                },
            ),
            backup_root=backup,
        )
        mock_sw = self._mock_switcher_with_backup(backup)

        with patch("claude_swap.cli.ClaudeAccountSwitcher", return_value=mock_sw), \
             patch("claude_swap.cli.Platform.detect", return_value=Platform.LINUX):
            self._dispatch(["auto", "status"])

        out = capsys.readouterr().out
        assert "last-known usage:" in out
        assert "acct 3" in out
        assert "5h 25%" in out
        assert "7d 12%" in out

    def test_auto_status_no_usage_block_when_empty(self, capsys, temp_home: Path):
        """No 'last-known usage:' header when state has no readings."""
        from claude_swap.models import Platform
        from claude_swap.paths import get_backup_root

        backup = get_backup_root()
        backup.mkdir(parents=True, exist_ok=True)
        mock_sw = self._mock_switcher_with_backup(backup)

        with patch("claude_swap.cli.ClaudeAccountSwitcher", return_value=mock_sw), \
             patch("claude_swap.cli.Platform.detect", return_value=Platform.LINUX):
            self._dispatch(["auto", "status"])

        out = capsys.readouterr().out
        assert "last-known usage:" not in out

    def test_auto_on_config_save_failure_clean_error(self, temp_home: Path, capsys):
        """If save_config raises (e.g. read-only dir), print a clean error and
        exit(1) — NOT a raw traceback."""
        from claude_swap.models import Platform
        from claude_swap.paths import get_backup_root

        backup = get_backup_root()
        backup.mkdir(parents=True, exist_ok=True)
        mock_sw = self._mock_switcher_with_backup(backup)

        with patch("claude_swap.cli.ClaudeAccountSwitcher", return_value=mock_sw), \
             patch("claude_swap.cli.Platform.detect", return_value=Platform.LINUX), \
             patch(
                 "claude_swap.cli._as_save_config",
                 side_effect=OSError("read-only file system"),
             ):
            with pytest.raises(SystemExit) as exc_info:
                self._dispatch(["auto", "on"])

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        # A clean one-line error was printed (no bare traceback text).
        assert "read-only file system" in combined or "Error" in combined

    # -----------------------------------------------------------------------
    # auto on / off
    # -----------------------------------------------------------------------

    def _mock_switcher_with_backup(self, backup: Path):
        """Return a mock ClaudeAccountSwitcher whose backup_dir is ``backup``."""
        mock = MagicMock(spec=ClaudeAccountSwitcher)
        mock.backup_dir = backup
        return mock

    def test_auto_on_saves_enabled(self, temp_home: Path, capsys):
        from claude_swap.auto_switch import load_config
        from claude_swap.models import Platform
        from claude_swap.paths import get_backup_root

        backup = get_backup_root()
        backup.mkdir(parents=True, exist_ok=True)
        mock_sw = self._mock_switcher_with_backup(backup)

        with (
            patch("claude_swap.cli.ClaudeAccountSwitcher", return_value=mock_sw),
            patch("claude_swap.cli.Platform.detect", return_value=Platform.LINUX),
        ):
            self._dispatch(["auto", "on"])

        cfg = load_config(backup_root=backup)
        assert cfg.enabled is True

    def test_auto_off_saves_disabled(self, temp_home: Path, capsys):
        from claude_swap.auto_switch import AutoSwitchConfig, load_config, save_config
        from claude_swap.models import Platform
        from claude_swap.paths import get_backup_root

        backup = get_backup_root()
        backup.mkdir(parents=True, exist_ok=True)
        save_config(AutoSwitchConfig(enabled=True), backup_root=backup)
        mock_sw = self._mock_switcher_with_backup(backup)

        with (
            patch("claude_swap.cli.ClaudeAccountSwitcher", return_value=mock_sw),
            patch("claude_swap.cli.Platform.detect", return_value=Platform.LINUX),
        ):
            self._dispatch(["auto", "off"])

        cfg = load_config(backup_root=backup)
        assert cfg.enabled is False

    def test_auto_on_with_thresholds(self, temp_home: Path):
        from claude_swap.auto_switch import load_config
        from claude_swap.models import Platform
        from claude_swap.paths import get_backup_root

        backup = get_backup_root()
        backup.mkdir(parents=True, exist_ok=True)
        mock_sw = self._mock_switcher_with_backup(backup)

        with (
            patch("claude_swap.cli.ClaudeAccountSwitcher", return_value=mock_sw),
            patch("claude_swap.cli.Platform.detect", return_value=Platform.LINUX),
        ):
            self._dispatch(["auto", "on", "--session-threshold", "90", "--weekly-threshold", "95"])

        cfg = load_config(backup_root=backup)
        assert cfg.session_threshold == 90.0
        assert cfg.weekly_threshold == 95.0

    def test_auto_on_no_notify_flag(self, temp_home: Path):
        from claude_swap.auto_switch import load_config
        from claude_swap.models import Platform
        from claude_swap.paths import get_backup_root

        backup = get_backup_root()
        backup.mkdir(parents=True, exist_ok=True)
        mock_sw = self._mock_switcher_with_backup(backup)

        with (
            patch("claude_swap.cli.ClaudeAccountSwitcher", return_value=mock_sw),
            patch("claude_swap.cli.Platform.detect", return_value=Platform.LINUX),
        ):
            self._dispatch(["auto", "on", "--no-notify"])

        cfg = load_config(backup_root=backup)
        assert cfg.notify is False

    def test_auto_on_macos_calls_install_agent(self, temp_home: Path, capsys):
        from claude_swap.models import Platform
        from claude_swap.paths import get_backup_root

        backup = get_backup_root()
        backup.mkdir(parents=True, exist_ok=True)
        mock_sw = self._mock_switcher_with_backup(backup)

        with patch("claude_swap.cli.ClaudeAccountSwitcher", return_value=mock_sw), \
             patch("claude_swap.cli.Platform.detect", return_value=Platform.MACOS), \
             patch("claude_swap.cli.install_agent", return_value="installed ok") as mock_install:
            self._dispatch(["auto", "on"])

        mock_install.assert_called_once()

    def test_auto_off_macos_calls_uninstall_agent(self, temp_home: Path, capsys):
        from claude_swap.auto_switch import AutoSwitchConfig, save_config
        from claude_swap.models import Platform
        from claude_swap.paths import get_backup_root

        backup = get_backup_root()
        backup.mkdir(parents=True, exist_ok=True)
        save_config(AutoSwitchConfig(enabled=True), backup_root=backup)
        mock_sw = self._mock_switcher_with_backup(backup)

        with patch("claude_swap.cli.ClaudeAccountSwitcher", return_value=mock_sw), \
             patch("claude_swap.cli.Platform.detect", return_value=Platform.MACOS), \
             patch("claude_swap.cli.uninstall_agent", return_value="uninstalled ok") as mock_uninstall:
            self._dispatch(["auto", "off"])

        mock_uninstall.assert_called_once()

    # -----------------------------------------------------------------------
    # consume-first strategy (CLI surface)
    # -----------------------------------------------------------------------

    def test_auto_on_shows_strategy_normal_visibility(self, temp_home: Path, capsys):
        """FIX 6: the active strategy is shown in a plain (non-dimmed) line."""
        from claude_swap.models import Platform
        from claude_swap.paths import get_backup_root

        backup = get_backup_root()
        backup.mkdir(parents=True, exist_ok=True)
        mock_sw = self._mock_switcher_with_backup(backup)

        with patch("claude_swap.cli.ClaudeAccountSwitcher", return_value=mock_sw), \
             patch("claude_swap.cli.Platform.detect", return_value=Platform.LINUX):
            self._dispatch(["auto", "on"])

        out = capsys.readouterr().out
        assert "Auto-switch enabled (strategy: " in out
        # New install (no prior config file) silently defaults to consume-first.
        assert "consume-first" in out

    def test_auto_on_new_install_defaults_consume_first(self, temp_home: Path, capsys):
        from claude_swap.auto_switch import load_config
        from claude_swap.models import Platform
        from claude_swap.paths import get_backup_root

        backup = get_backup_root()
        backup.mkdir(parents=True, exist_ok=True)
        # No auto-switch.json on disk → "new install".
        assert not (backup / "auto-switch.json").exists()
        mock_sw = self._mock_switcher_with_backup(backup)

        with patch("claude_swap.cli.ClaudeAccountSwitcher", return_value=mock_sw), \
             patch("claude_swap.cli.Platform.detect", return_value=Platform.LINUX):
            self._dispatch(["auto", "on"])

        assert load_config(backup_root=backup).strategy == "consume-first"

    def test_auto_on_existing_config_preserves_strategy(self, temp_home: Path, capsys):
        """An existing config (reactive) is preserved when --strategy is absent."""
        from claude_swap.auto_switch import AutoSwitchConfig, load_config, save_config
        from claude_swap.models import Platform
        from claude_swap.paths import get_backup_root

        backup = get_backup_root()
        backup.mkdir(parents=True, exist_ok=True)
        save_config(
            AutoSwitchConfig(enabled=False, strategy="reactive"), backup_root=backup
        )
        mock_sw = self._mock_switcher_with_backup(backup)

        with patch("claude_swap.cli.ClaudeAccountSwitcher", return_value=mock_sw), \
             patch("claude_swap.cli.Platform.detect", return_value=Platform.LINUX):
            self._dispatch(["auto", "on"])

        # Strategy NOT flipped to consume-first because a config already existed.
        assert load_config(backup_root=backup).strategy == "reactive"

    def test_auto_on_explicit_strategy_persisted(self, temp_home: Path, capsys):
        from claude_swap.auto_switch import load_config
        from claude_swap.models import Platform
        from claude_swap.paths import get_backup_root

        backup = get_backup_root()
        backup.mkdir(parents=True, exist_ok=True)
        mock_sw = self._mock_switcher_with_backup(backup)

        with patch("claude_swap.cli.ClaudeAccountSwitcher", return_value=mock_sw), \
             patch("claude_swap.cli.Platform.detect", return_value=Platform.LINUX):
            self._dispatch(["auto", "on", "--strategy", "reactive"])

        assert load_config(backup_root=backup).strategy == "reactive"

    def test_auto_status_shows_strategy_line(self, capsys, temp_home: Path):
        from claude_swap.auto_switch import AutoSwitchConfig, save_config
        from claude_swap.models import Platform
        from claude_swap.paths import get_backup_root

        backup = get_backup_root()
        backup.mkdir(parents=True, exist_ok=True)
        save_config(
            AutoSwitchConfig(enabled=True, strategy="consume-first"),
            backup_root=backup,
        )
        mock_sw = self._mock_switcher_with_backup(backup)

        with patch("claude_swap.cli.ClaudeAccountSwitcher", return_value=mock_sw), \
             patch("claude_swap.cli.Platform.detect", return_value=Platform.LINUX):
            self._dispatch(["auto", "status"])

        out = capsys.readouterr().out
        assert "strategy:" in out
        assert "consume-first" in out

    # -----------------------------------------------------------------------
    # watch
    # -----------------------------------------------------------------------

    def test_watch_non_macos_prints_notice(self, capsys):
        from claude_swap.models import Platform

        with patch("claude_swap.cli.Platform.detect", return_value=Platform.LINUX):
            with pytest.raises(SystemExit) as excinfo:
                self._dispatch(["watch"])
        assert excinfo.value.code == 0
        out = capsys.readouterr().out
        assert "macOS" in out

    def test_watch_macos_calls_autoswitch_watch(self, temp_home: Path):
        from claude_swap.models import Platform
        from claude_swap.paths import get_backup_root

        backup = get_backup_root()
        backup.mkdir(parents=True, exist_ok=True)
        mock_switcher = self._mock_switcher_with_backup(backup)
        mock_as = MagicMock()

        with patch("claude_swap.cli.Platform.detect", return_value=Platform.MACOS), \
             patch("claude_swap.cli.ClaudeAccountSwitcher", return_value=mock_switcher), \
             patch("claude_swap.cli.AutoSwitcher", return_value=mock_as):
            self._dispatch(["watch"])

        mock_as.watch.assert_called_once()

    # -----------------------------------------------------------------------
    # _auto-daemon
    # -----------------------------------------------------------------------

    def test_auto_daemon_calls_run_daemon(self, temp_home: Path):
        from claude_swap.paths import get_backup_root

        backup = get_backup_root()
        backup.mkdir(parents=True, exist_ok=True)
        mock_switcher = self._mock_switcher_with_backup(backup)
        mock_as = MagicMock()

        with patch("claude_swap.cli.ClaudeAccountSwitcher", return_value=mock_switcher), \
             patch("claude_swap.cli.AutoSwitcher", return_value=mock_as):
            self._dispatch(["_auto-daemon"])

        mock_as.run_daemon.assert_called_once()

    # -----------------------------------------------------------------------
    # main() pre-dispatch routing
    # -----------------------------------------------------------------------

    def test_main_dispatches_auto(self, temp_home: Path, capsys):
        from claude_swap.models import Platform
        from claude_swap.paths import get_backup_root

        backup = get_backup_root()
        backup.mkdir(parents=True, exist_ok=True)
        mock_sw = self._mock_switcher_with_backup(backup)

        with patch.object(sys, "argv", ["cswap", "auto"]), \
             patch("claude_swap.cli.ClaudeAccountSwitcher", return_value=mock_sw), \
             patch("claude_swap.cli.Platform.detect", return_value=Platform.LINUX):
            cli.main()

        out = capsys.readouterr().out
        assert "enabled" in out

    def test_main_dispatches_watch(self, capsys):
        from claude_swap.models import Platform

        with (
            patch.object(sys, "argv", ["cswap", "watch"]),
            patch("claude_swap.cli.Platform.detect", return_value=Platform.LINUX),
        ):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()

        assert excinfo.value.code == 0
        assert "macOS" in capsys.readouterr().out

    def test_main_dispatches_auto_daemon(self, temp_home: Path):
        from claude_swap.paths import get_backup_root

        backup = get_backup_root()
        backup.mkdir(parents=True, exist_ok=True)
        mock_switcher = self._mock_switcher_with_backup(backup)
        mock_as = MagicMock()

        with patch.object(sys, "argv", ["cswap", "_auto-daemon"]), \
             patch("claude_swap.cli.ClaudeAccountSwitcher", return_value=mock_switcher), \
             patch("claude_swap.cli.AutoSwitcher", return_value=mock_as):
            cli.main()

        mock_as.run_daemon.assert_called_once()

    def test_auto_daemon_not_in_help(self):
        """_auto-daemon is a hidden verb — must not appear in main --help."""
        result = subprocess.run(
            [sys.executable, "-m", "claude_swap", "--help"],
            capture_output=True,
            text=True,
            env=_subprocess_env(),
        )
        assert "_auto-daemon" not in result.stdout

    def test_auto_not_in_main_help_group(self):
        """auto/watch are pre-dispatched, not in the mutually-exclusive group."""
        result = subprocess.run(
            [sys.executable, "-m", "claude_swap", "--help"],
            capture_output=True,
            text=True,
            env=_subprocess_env(),
        )
        # They appear in the examples but NOT as top-level flags in the group.
        # The real check is that the main parser doesn't error on them.
        assert result.returncode == 0


# keep existing test:
    def test_session_error_exits_cleanly(self, capsys):
        class FailingSessionManager:
            def __init__(self, switcher):
                pass

            def run(self, identifier, claude_args, share=True):
                from claude_swap.exceptions import SessionError

                raise SessionError("boom")

        with patch("claude_swap.session.SessionManager", FailingSessionManager), \
             patch("claude_swap.cli.ClaudeAccountSwitcher"), \
             patch("os.geteuid", return_value=1000), \
             patch.object(sys, "argv", ["claude-swap", "run", "2"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()

        assert excinfo.value.code == 1
        assert "boom" in capsys.readouterr().err


class TestJsonOutputCli:
    """CLI wiring for ``--json``: validation, single serialization, error envelope."""

    def test_json_rejected_without_supported_command(self, capsys):
        """--purge --json is rejected (bare --json instead hits the required-group error)."""
        with patch.object(sys, "argv", ["claude-swap", "--purge", "--json"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 2
        assert "--json can only be used with" in capsys.readouterr().err

    def test_token_status_with_json_rejected(self, capsys):
        with patch.object(sys, "argv", ["claude-swap", "--list", "--token-status", "--json"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 2
        assert "--token-status cannot be combined with --json" in capsys.readouterr().err

    def test_list_json_serialized_to_stdout(self, capsys):
        payload = {"schemaVersion": 1, "activeAccountNumber": None, "accounts": []}
        with patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["claude-swap", "--list", "--json"]), \
             patch("os.geteuid", return_value=1000), \
             patch("claude_swap.update_check.check_for_update", return_value=None):
            switcher_cls.return_value.list_accounts.return_value = payload
            cli.main()

        switcher_cls.return_value.list_accounts.assert_called_once_with(
            show_token_status=False, json_output=True,
        )
        out = capsys.readouterr().out
        assert json.loads(out) == payload  # exactly one JSON object, no extra text

    def test_switch_json_forwarded_and_serialized(self, capsys):
        payload = {"schemaVersion": 1, "switched": True}
        with patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["claude-swap", "--switch", "--json"]), \
             patch("os.geteuid", return_value=1000), \
             patch("claude_swap.update_check.check_for_update", return_value=None):
            switcher_cls.return_value.switch.return_value = payload
            cli.main()

        switcher_cls.return_value.switch.assert_called_once_with(
            strategy=None, json_output=True,
        )
        assert json.loads(capsys.readouterr().out) == payload

    def test_error_envelope_on_stdout_with_exit_1(self, capsys):
        from claude_swap.exceptions import ConfigError

        with patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["claude-swap", "--status", "--json"]), \
             patch("os.geteuid", return_value=1000), \
             patch("claude_swap.update_check.check_for_update", return_value=None):
            switcher_cls.return_value.status.side_effect = ConfigError("nope")
            with pytest.raises(SystemExit) as excinfo:
                cli.main()

        assert excinfo.value.code == 1
        captured = capsys.readouterr()
        envelope = json.loads(captured.out)  # error went to stdout as JSON
        assert envelope["error"] == {"type": "ConfigError", "message": "nope"}
        assert captured.err == ""  # nothing on stderr in JSON mode
