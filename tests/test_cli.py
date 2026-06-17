"""Tests for the CLI module."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

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


class TestQuickStart:
    """The configurable bare-`cswap` alias (quickStart): default OFF, alias-with-args."""

    def _run_launch(self, argv: list[str]) -> dict:
        """Invoke main() and capture the argv that reaches the launch handler."""
        captured: dict = {}

        def fake_launch(a):
            captured["argv"] = list(a)

        with patch("claude_swap.cli._launch_command", side_effect=fake_launch), \
             patch.object(sys, "argv", ["claude-swap", *argv]):
            cli.main()
        return captured

    def test_disabled_by_default_bare_cswap_errors(self, temp_home, capsys):
        # No quickStart config => bare `cswap` falls through to argparse's
        # required-argument error, exactly as before the feature existed.
        with patch.object(sys, "argv", ["claude-swap"]):
            with pytest.raises(SystemExit) as e:
                cli.main()
        assert e.value.code == 2

    def test_bare_runs_default_command_when_enabled(self, temp_home):
        ClaudeAccountSwitcher().set_quick_start_config(enabled=True)
        cap = self._run_launch([])
        # `cswap launch -- <claude args>` => launch handler sees everything after
        # the subcommand, i.e. starting at the `--` separator.
        assert cap["argv"][0] == "--"
        assert "--dangerously-skip-permissions" in cap["argv"]
        assert cap["argv"][-2:] == ["--settings", '{"ultracode": true}']

    def test_extra_args_are_appended(self, temp_home):
        ClaudeAccountSwitcher().set_quick_start_config(enabled=True)
        cap = self._run_launch(["--resume", "abc123"])
        assert "--dangerously-skip-permissions" in cap["argv"]
        assert cap["argv"][-2:] == ["--resume", "abc123"]

    def test_reserved_flag_is_not_hijacked(self, temp_home):
        ClaudeAccountSwitcher().set_quick_start_config(enabled=True)
        with patch("claude_swap.cli._launch_command") as launch, \
             patch("claude_swap.switcher.ClaudeAccountSwitcher.status") as status, \
             patch("os.geteuid", return_value=1000), \
             patch.object(sys, "argv", ["claude-swap", "--status"]):
            cli.main()
        launch.assert_not_called()
        status.assert_called_once()

    def test_custom_command_can_target_another_subcommand(self, temp_home):
        calls: list = []

        class FakeSessionManager:
            def __init__(self, switcher):
                pass

            def run(self, identifier, claude_args, share=True):
                calls.append((identifier, claude_args, share))

        sw = ClaudeAccountSwitcher()
        sw.set_quick_start_config(enabled=True, command="cswap run 2")
        with patch("claude_swap.session.SessionManager", FakeSessionManager), \
             patch("os.geteuid", return_value=1000), \
             patch.object(sys, "argv", ["claude-swap"]):
            cli.main()
        assert calls == [("2", [], True)]

    def test_double_dash_separator_is_stripped(self, temp_home):
        # `cswap -- <args>` means "everything after -- is extra" — it must NOT
        # leave a stray `--` on top of the default command's own separator (which
        # would make claude treat --resume as a positional prompt).
        ClaudeAccountSwitcher().set_quick_start_config(enabled=True)
        cap = self._run_launch(["--", "--resume", "abc123"])
        assert cap["argv"][-2:] == ["--resume", "abc123"]
        # Only the configured command's own leading `--` survives (no stray one).
        assert cap["argv"].count("--") == 1

    def test_abbreviated_flag_is_rejected_not_silently_expanded(self, temp_home):
        # allow_abbrev=False keeps argparse's matching in lockstep with the
        # exact-match reserved set: an abbreviated flag is no longer silently
        # expanded (and so can never mean one thing with quick-start off and
        # another with it on). Quick-start is OFF here (the default).
        with patch("claude_swap.cli._launch_command") as launch, \
             patch("os.geteuid", return_value=1000), \
             patch.object(sys, "argv", ["claude-swap", "--lis"]):
            with pytest.raises(SystemExit) as e:
                cli.main()
        assert e.value.code == 2
        launch.assert_not_called()
