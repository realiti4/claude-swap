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
        # Bare subcommands are the documented interface and lead the help.
        assert "cswap add" in result.stdout or "add " in result.stdout
        assert "switch <num|email>" in result.stdout
        assert "list " in result.stdout
        assert "status " in result.stdout
        # The legacy `--flag` spellings still work but are hidden from the
        # options section; only the "keep working" note may mention them.
        options_section = result.stdout.split("Flags combine with subcommands:")[0]
        assert "--add-account" not in options_section
        assert "--switch " not in options_section
        assert "--list" not in options_section
        assert "--status" not in options_section
        # ...and the note that they keep working is still present.
        assert "keep working" in result.stdout

    def test_no_args_shows_error(self):
        """Test that running without args (non-TTY) shows a clean no-command error."""
        result = subprocess.run(
            [sys.executable, "-m", "claude_swap"],
            capture_output=True,
            text=True,
            env=_subprocess_env(),
        )
        assert result.returncode == 2
        assert "no command given" in result.stderr
        # The now-hidden legacy flags must not leak into the error.
        assert "--add-account" not in result.stderr
        assert "one of the arguments" not in result.stderr

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
        assert "--token-status can only be used with 'list'" in capsys.readouterr().err

    def test_token_status_flag_is_forwarded_to_list(self):
        """--list --token-status should call list_accounts(show_token_status=True)."""
        with patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["claude-swap", "--list", "--token-status"]), \
             patch("os.geteuid", return_value=1000, create=True), \
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
        assert "--strategy can only be used with bare 'switch'" in capsys.readouterr().err

    def test_strategy_next_available_requires_switch(self, capsys):
        """--strategy next-available should only be accepted alongside --switch."""
        with patch.object(sys, "argv", ["claude-swap", "--strategy", "next-available", "--list"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()

        assert excinfo.value.code == 2
        assert "--strategy can only be used with bare 'switch'" in capsys.readouterr().err

    def test_strategy_rejects_unknown_value(self, capsys):
        """argparse rejects strategies outside the known choices."""
        with patch.object(sys, "argv", ["claude-swap", "--switch", "--strategy", "bogus"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()

        assert excinfo.value.code == 2

    def test_switch_strategy_forwarded(self):
        """--switch --strategy best forwards the strategy to switch()."""
        from claude_swap.settings import AutoSwitchSettings

        with patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["claude-swap", "--switch", "--strategy", "best"]), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch("claude_swap.settings.load_settings",
                   return_value=AutoSwitchSettings()), \
             patch("claude_swap.update_check.check_for_update", return_value=None):
            cli.main()

        switcher_cls.return_value.switch.assert_called_once_with(
            strategy="best", json_output=False, models=(), model_source=None
        )

    def test_switch_strategy_falls_back_to_configured_model(self):
        """Without --model, the persistent autoswitch.model steers the
        strategy — reported as coming from the setting, not the CLI."""
        from claude_swap.settings import AutoSwitchSettings

        with patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["claude-swap", "--switch", "--strategy", "best"]), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch("claude_swap.settings.load_settings",
                   return_value=AutoSwitchSettings(model="Fable")), \
             patch("claude_swap.update_check.check_for_update", return_value=None):
            cli.main()

        switcher_cls.return_value.switch.assert_called_once_with(
            strategy="best", json_output=False,
            models=("Fable",), model_source="autoswitch.model",
        )

    def test_switch_model_flag_overrides_setting(self):
        """--model beats autoswitch.model, is deduped, and reports 'cli'."""
        from claude_swap.settings import AutoSwitchSettings

        with patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", [
                 "claude-swap", "--switch", "--strategy", "next-available",
                 "--model", "Opus, opus,Fable",
             ]), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch("claude_swap.settings.load_settings",
                   return_value=AutoSwitchSettings(model="Sonnet")), \
             patch("claude_swap.update_check.check_for_update", return_value=None):
            cli.main()

        switcher_cls.return_value.switch.assert_called_once_with(
            strategy="next-available", json_output=False,
            models=("Opus", "Fable"), model_source="cli",
        )

    def test_switch_model_without_strategy_is_rejected(self, capsys):
        """--model is meaningless without a usage-aware strategy."""
        with patch.object(sys, "argv", ["claude-swap", "--switch", "--model", "Fable"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 2
        assert "--model can only be used with" in capsys.readouterr().err

    def test_plain_switch_passes_no_strategy(self):
        """Bare --switch forwards strategy=None."""
        with patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["claude-swap", "--switch"]), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch("claude_swap.update_check.check_for_update", return_value=None):
            cli.main()

        switcher_cls.return_value.switch.assert_called_once_with(
            strategy=None, json_output=False, models=(), model_source=None
        )

    def test_slot_flag_requires_add_account(self, capsys):
        """--slot should only be accepted alongside --add-account or --add-token."""
        with patch.object(sys, "argv", ["claude-swap", "--list", "--slot", "3"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()

        assert excinfo.value.code == 2
        assert "--slot can only be used with 'add' or 'add-token'" in capsys.readouterr().err

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
        assert "--account can only be used with 'export'" in capsys.readouterr().err

    def test_force_flag_requires_import_or_switch_to(self, capsys):
        """--force should only be accepted alongside --import or --switch-to."""
        with patch.object(sys, "argv", ["claude-swap", "--list", "--force"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 2
        assert (
            "--force can only be used with 'import' or 'switch <num|email>'"
            in capsys.readouterr().err
        )

    def test_switch_to_force_forwarded(self):
        """--switch-to 2 --force forwards force=True to switch_to()."""
        with patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["claude-swap", "--switch-to", "2", "--force"]), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch("claude_swap.update_check.check_for_update", return_value=None):
            cli.main()

        switcher_cls.return_value.switch_to.assert_called_once_with(
            "2", json_output=False, force=True
        )

    def test_switch_to_without_force_forwards_false(self):
        """Plain --switch-to forwards force=False."""
        with patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["claude-swap", "--switch-to", "2"]), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch("claude_swap.update_check.check_for_update", return_value=None):
            cli.main()

        switcher_cls.return_value.switch_to.assert_called_once_with(
            "2", json_output=False, force=False
        )

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
        """The export/import subcommands should appear in help output."""
        result = subprocess.run(
            [sys.executable, "-m", "claude_swap", "--help"],
            capture_output=True,
            text=True,
            env=_subprocess_env(),
        )
        assert "export <path>" in result.stdout
        assert "import <path>" in result.stdout

    def test_export_dispatch_calls_transfer(self):
        """--export dispatches into transfer.export_accounts."""
        with patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch("claude_swap.transfer.export_accounts") as export_fn, \
             patch.object(
                 sys, "argv", ["claude-swap", "--export", "/tmp/x", "--account", "2"]
             ), \
             patch("os.geteuid", return_value=1000, create=True), \
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
        assert "--full can only be used with 'export'" in capsys.readouterr().err

    def test_full_flag_dispatches_with_full_true(self):
        """--export --full should pass full=True into export_accounts."""
        with patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch("claude_swap.transfer.export_accounts") as export_fn, \
             patch.object(
                 sys, "argv", ["claude-swap", "--export", "/tmp/x", "--full"]
             ), \
             patch("os.geteuid", return_value=1000, create=True), \
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
             patch("os.geteuid", return_value=1000, create=True), \
             patch("claude_swap.update_check.check_for_update", return_value=None):
            cli.main()
        import_fn.assert_called_once_with(
            switcher_cls.return_value, "/tmp/x", force=True
        )

    def test_upgrade_in_help(self):
        """The upgrade subcommand should appear in help output."""
        result = subprocess.run(
            [sys.executable, "-m", "claude_swap", "--help"],
            capture_output=True,
            text=True,
            env=_subprocess_env(),
        )
        assert "upgrade " in result.stdout

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

    def test_menubar_flag_dispatches(self, monkeypatch):
        called = {}

        class _FakeSwitcher:
            def __init__(self, *a, **k):
                pass
            def _is_running_in_container(self):
                return False

        def _fake_run(switcher):
            called["ran"] = True
            return 0

        monkeypatch.setattr(cli, "ClaudeAccountSwitcher", _FakeSwitcher)
        monkeypatch.setattr(sys, "argv", ["cswap", "--menubar"])
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr("claude_swap.menubar.run", _fake_run, raising=False)
        # geteuid only exists on POSIX; ensure non-root path
        monkeypatch.setattr(cli.os, "geteuid", lambda: 1000, raising=False)

        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 0
        assert called.get("ran") is True

    def test_menubar_subcommand_dispatches(self, monkeypatch):
        """Bare `cswap menubar` should route exactly like `cswap --menubar`."""
        called = {}

        class _FakeSwitcher:
            def __init__(self, *a, **k):
                pass
            def _is_running_in_container(self):
                return False

        def _fake_run(switcher):
            called["ran"] = True
            return 0

        monkeypatch.setattr(cli, "ClaudeAccountSwitcher", _FakeSwitcher)
        monkeypatch.setattr(sys, "argv", ["cswap", "menubar"])
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr("claude_swap.menubar.run", _fake_run, raising=False)
        monkeypatch.setattr(cli.os, "geteuid", lambda: 1000, raising=False)

        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 0
        assert called.get("ran") is True


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
        assert "--email can only be used with 'add-token'" in capsys.readouterr().err

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
        """The add-token subcommand and the still-visible --email modifier appear."""
        result = subprocess.run(
            [sys.executable, "-m", "claude_swap", "--help"],
            capture_output=True,
            text=True,
            env=_subprocess_env(),
        )
        assert "add-token [TOKEN|-]" in result.stdout
        assert "--email" in result.stdout  # modifier flag stays visible


class TestRunCommand:
    """`cswap run` pre-dispatch: parsing, forwarding, and dispatch."""

    def _dispatch(self, argv: list[str]):
        """Run cli.main() with a fake SessionManager; returns recorded calls."""
        calls = []

        class FakeSessionManager:
            def __init__(self, switcher):
                calls.append(("init", switcher))

            def run(self, identifier, claude_args, share=True, share_history=False):
                calls.append(("run", identifier, claude_args, share, share_history))

        with patch("claude_swap.session.SessionManager", FakeSessionManager), \
             patch("claude_swap.cli.ClaudeAccountSwitcher"), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch.object(sys, "argv", ["claude-swap", *argv]):
            cli.main()
        return calls

    def test_run_dispatches_with_defaults(self):
        calls = self._dispatch(["run", "2"])
        assert ("run", "2", [], True, False) in calls

    def test_run_by_email(self):
        calls = self._dispatch(["run", "user@example.com"])
        assert ("run", "user@example.com", [], True, False) in calls

    def test_no_share_flag(self):
        calls = self._dispatch(["run", "2", "--no-share"])
        assert ("run", "2", [], False, False) in calls

    def test_share_history_flag(self):
        calls = self._dispatch(["run", "2", "--share-history"])
        assert ("run", "2", [], True, True) in calls

    def test_no_share_history_flag(self):
        calls = self._dispatch(["run", "2", "--no-share-history"])
        assert ("run", "2", [], True, False) in calls

    def test_tail_forwarded_verbatim(self):
        calls = self._dispatch(["run", "2", "--", "--resume", "--model", "x"])
        assert ("run", "2", ["--resume", "--model", "x"], True, False) in calls

    def test_tail_may_contain_run_flags(self):
        """Args after `--` are NOT parsed by cswap, even if they look like ours."""
        calls = self._dispatch(["run", "2", "--", "--no-share"])
        assert ("run", "2", ["--no-share"], True, False) in calls

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

    def test_main_help_mentions_alias(self):
        result = subprocess.run(
            [sys.executable, "-m", "claude_swap", "--help"],
            capture_output=True,
            text=True,
            env=_subprocess_env(),
        )
        assert "alias <num|email>" in result.stdout

    def test_session_error_exits_cleanly(self, capsys):
        class FailingSessionManager:
            def __init__(self, switcher):
                pass

            def run(self, identifier, claude_args, share=True, share_history=False):
                from claude_swap.exceptions import SessionError

                raise SessionError("boom")

        with patch("claude_swap.session.SessionManager", FailingSessionManager), \
             patch("claude_swap.cli.ClaudeAccountSwitcher"), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch.object(sys, "argv", ["claude-swap", "run", "2"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()

        assert excinfo.value.code == 1
        assert "boom" in capsys.readouterr().err


class TestSubcommandAliases:
    """Memorable subcommands (`cswap switch`, `cswap list`, ...) → classic flags."""

    def test_translate_is_noop_for_flags(self):
        """argv that already uses --flags is passed through untouched."""
        assert cli._translate_subcommand(["--list"]) == ["--list"]
        assert cli._translate_subcommand(["--switch", "--json"]) == ["--switch", "--json"]
        assert cli._translate_subcommand([]) == []

    def test_translate_bare_switch_rotates(self):
        assert cli._translate_subcommand(["switch"]) == ["--switch"]
        assert cli._translate_subcommand(["switch", "--strategy", "best"]) == [
            "--switch", "--strategy", "best",
        ]

    def test_translate_switch_with_target(self):
        assert cli._translate_subcommand(["switch", "2"]) == ["--switch-to", "2"]
        assert cli._translate_subcommand(["switch", "u@x.com", "--json"]) == [
            "--switch-to", "u@x.com", "--json",
        ]

    def test_translate_simple_verbs_and_aliases(self):
        assert cli._translate_subcommand(["list"]) == ["--list"]
        assert cli._translate_subcommand(["ls"]) == ["--list"]
        assert cli._translate_subcommand(["status"]) == ["--status"]
        assert cli._translate_subcommand(["add"]) == ["--add-account"]
        assert cli._translate_subcommand(["rm", "2"]) == ["--remove-account", "2"]
        assert cli._translate_subcommand(["upgrade"]) == ["--upgrade"]
        assert cli._translate_subcommand(["update"]) == ["--upgrade"]
        assert cli._translate_subcommand(["menubar"]) == ["--menubar"]

    def test_translate_value_verbs_pass_through_extra_flags(self):
        assert cli._translate_subcommand(["export", "b.cswap", "--full"]) == [
            "--export", "b.cswap", "--full",
        ]
        assert cli._translate_subcommand(["add-token", "sk-tok", "--slot", "3"]) == [
            "--add-token", "sk-tok", "--slot", "3",
        ]

    def test_translate_unknown_verb_unchanged(self):
        """An unrecognized first token is left for the parser to reject."""
        assert cli._translate_subcommand(["bogus"]) == ["bogus"]

    def test_switch_subcommand_dispatches_switch_to(self):
        """`cswap switch 2` reaches switch_to("2")."""
        with patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["claude-swap", "switch", "2"]), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch("claude_swap.update_check.check_for_update", return_value=None):
            cli.main()
        switcher_cls.return_value.switch_to.assert_called_once_with(
            "2", json_output=False, force=False
        )

    def test_bare_switch_subcommand_dispatches_switch(self):
        """`cswap switch` reaches switch() (rotate)."""
        with patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["claude-swap", "switch"]), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch("claude_swap.update_check.check_for_update", return_value=None):
            cli.main()
        switcher_cls.return_value.switch.assert_called_once_with(
            strategy=None, json_output=False, models=(), model_source=None
        )

    def test_list_subcommand_with_json(self):
        """`cswap list --json` reaches list_accounts(json_output=True)."""
        payload = {"schemaVersion": 1, "accounts": []}
        with patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["claude-swap", "list", "--json"]), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch("claude_swap.update_check.check_for_update", return_value=None):
            switcher_cls.return_value.list_accounts.return_value = payload
            cli.main()
        switcher_cls.return_value.list_accounts.assert_called_once_with(
            show_token_status=False, json_output=True,
        )

    def test_run_subcommand_still_dispatches(self):
        """`cswap run 2` keeps reaching the session pre-dispatch (not translated)."""
        calls = []

        class FakeSessionManager:
            def __init__(self, switcher):
                pass

            def run(self, identifier, claude_args, share=True, share_history=False):
                calls.append((identifier, claude_args, share))

        with patch("claude_swap.session.SessionManager", FakeSessionManager), \
             patch("claude_swap.cli.ClaudeAccountSwitcher"), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch.object(sys, "argv", ["claude-swap", "run", "2"]):
            cli.main()
        assert calls == [("2", [], True)]

    def test_help_subcommand_prints_help(self):
        """`cswap help` exits 0 and prints help (with subcommand docs)."""
        result = subprocess.run(
            [sys.executable, "-m", "claude_swap", "help"],
            capture_output=True,
            text=True,
            env=_subprocess_env(),
        )
        assert result.returncode == 0
        assert "Multi-Account Switcher" in result.stdout
        assert "Commands:" in result.stdout
        assert "keep working" in result.stdout


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
             patch("os.geteuid", return_value=1000, create=True), \
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
             patch("os.geteuid", return_value=1000, create=True), \
             patch("claude_swap.update_check.check_for_update", return_value=None):
            switcher_cls.return_value.switch.return_value = payload
            cli.main()

        switcher_cls.return_value.switch.assert_called_once_with(
            strategy=None, json_output=True, models=(), model_source=None,
        )
        assert json.loads(capsys.readouterr().out) == payload

    def test_switch_json_carries_model_fields_when_in_effect(self, capsys):
        """Additive models/modelSource fields make a model-steered pick
        auditable from scripts too."""
        payload = {"schemaVersion": 1, "switched": True}
        with patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", [
                 "claude-swap", "--switch", "--strategy", "best",
                 "--model", "Fable", "--json",
             ]), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch("claude_swap.update_check.check_for_update", return_value=None):
            switcher_cls.return_value.switch.return_value = payload
            cli.main()

        out = json.loads(capsys.readouterr().out)
        assert out["models"] == ["Fable"]
        assert out["modelSource"] == "cli"
        assert out["switched"] is True

    def test_error_envelope_on_stdout_with_exit_1(self, capsys):
        from claude_swap.exceptions import ConfigError

        with patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["claude-swap", "--status", "--json"]), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch("claude_swap.update_check.check_for_update", return_value=None):
            switcher_cls.return_value.status.side_effect = ConfigError("nope")
            with pytest.raises(SystemExit) as excinfo:
                cli.main()

        assert excinfo.value.code == 1
        captured = capsys.readouterr()
        envelope = json.loads(captured.out)  # error went to stdout as JSON
        assert envelope["error"] == {"type": "ConfigError", "message": "nope"}
        assert captured.err == ""  # nothing on stderr in JSON mode


class TestAutoCommand:
    """`cswap auto` pre-dispatch: parsing, settings merge, exit codes, JSONL."""

    class FakeEngine:
        instances: list = []
        tick_outcome = None  # set per test (TickOutcome)

        def __init__(self, switcher, settings, on_event, *, dry_run=False,
                     state_path=None, clock=None):
            self.switcher = switcher
            self.settings = settings
            self.on_event = on_event
            self.dry_run = dry_run
            type(self).instances.append(self)

        def tick(self):
            from claude_swap.autoswitch import TickOutcome

            return type(self).tick_outcome or TickOutcome.NO_ACTION

        def run_loop(self):
            return 0

        def stop(self):
            pass

    @pytest.fixture(autouse=True)
    def _fresh_fake(self):
        self.FakeEngine.instances = []
        self.FakeEngine.tick_outcome = None

    def _run(self, argv: list[str], temp_home):
        with patch("claude_swap.autoswitch.AutoSwitchEngine", self.FakeEngine), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch.object(sys, "argv", ["claude-swap", "auto", *argv]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        return excinfo.value.code

    def test_once_exit_code_switched(self, temp_home):
        from claude_swap.autoswitch import TickOutcome

        self.FakeEngine.tick_outcome = TickOutcome.SWITCHED
        assert self._run(["--once"], temp_home) == 0

    def test_once_exit_code_no_action(self, temp_home):
        from claude_swap.autoswitch import TickOutcome

        self.FakeEngine.tick_outcome = TickOutcome.NO_ACTION
        assert self._run(["--once"], temp_home) == 2

    def test_once_exit_code_blocked(self, temp_home):
        from claude_swap.autoswitch import TickOutcome

        self.FakeEngine.tick_outcome = TickOutcome.BLOCKED
        assert self._run(["--once"], temp_home) == 3

    def test_loop_mode_returns_loop_exit(self, temp_home):
        assert self._run([], temp_home) == 0
        assert self.FakeEngine.instances  # loop path constructed the engine

    def test_flags_override_settings_json(self, temp_home):
        from claude_swap.paths import get_backup_root

        backup = get_backup_root()
        backup.mkdir(parents=True, exist_ok=True)
        (backup / "settings.json").write_text(json.dumps({
            "schemaVersion": 1,
            "autoswitch": {"threshold": 80.0, "cooldownSeconds": 42.0},
        }))
        self._run(["--once", "--threshold", "60"], temp_home)
        engine = self.FakeEngine.instances[-1]
        assert engine.settings.threshold == 60.0     # CLI wins
        assert engine.settings.cooldown_seconds == 42.0  # settings.json kept

    def test_dry_run_forwarded(self, temp_home):
        self._run(["--once", "--dry-run"], temp_home)
        assert self.FakeEngine.instances[-1].dry_run is True

    def test_json_stdout_is_pure_jsonl(self, temp_home, capsys):
        from claude_swap.autoswitch import NoSwitchEvent, TickOutcome

        class EmittingEngine(self.FakeEngine):
            def tick(self):
                self.on_event(NoSwitchEvent(reason="below-threshold"))
                self.on_event(NoSwitchEvent(reason="cooldown"))
                return TickOutcome.NO_ACTION

        with patch("claude_swap.autoswitch.AutoSwitchEngine", EmittingEngine), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch.object(sys, "argv", ["claude-swap", "auto", "--once", "--json"]):
            with pytest.raises(SystemExit):
                cli.main()
        lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
        assert len(lines) == 2
        for line in lines:
            payload = json.loads(line)
            assert payload["event"] == "no-switch"
            assert payload["schemaVersion"] == 1

    def test_unknown_flag_errors(self, temp_home, capsys):
        with patch.object(sys, "argv", ["claude-swap", "auto", "--bogus"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 2

    def test_auto_help(self, capsys):
        with patch.object(sys, "argv", ["claude-swap", "auto", "--help"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 0
        out = capsys.readouterr().out
        assert "--once" in out
        assert "Exit codes" in out

    def test_main_help_mentions_auto(self):
        result = subprocess.run(
            [sys.executable, "-m", "claude_swap", "--help"],
            capture_output=True,
            text=True,
            env=_subprocess_env(),
        )
        assert "auto" in result.stdout

    def test_switcher_error_exits_1(self, temp_home, capsys):
        from claude_swap.exceptions import ConfigError

        with patch("claude_swap.cli.ClaudeAccountSwitcher",
                   side_effect=ConfigError("nope")), \
             patch.object(sys, "argv", ["claude-swap", "auto", "--once"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 1
        assert "nope" in capsys.readouterr().err  # printer.error -> stderr


class TestMapCommand:
    """`cswap map` / `cswap unmap` directory-mapping commands."""

    def _seeded_switcher_env(self, temp_home):
        """Build a real switcher with one managed account (slot 2)."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._init_sequence_file()
        data = switcher._get_sequence_data()
        data["accounts"]["2"] = {
            "email": "work@co.com",
            "uuid": "u2",
            "organizationUuid": "",
            "organizationName": "",
            "added": "2024-01-01T00:00:00Z",
        }
        data["sequence"] = [2]
        switcher._write_json(switcher.sequence_file, data)
        return switcher

    def test_map_account_to_path(self, temp_home, capsys):
        from claude_swap.mappings import MappingStore
        self._seeded_switcher_env(temp_home)
        target = temp_home / "proj"
        target.mkdir()

        with patch("os.geteuid", return_value=1000, create=True):
            cli._map_command(["2", str(target)])

        store = MappingStore(ClaudeAccountSwitcher().backup_dir)
        entry = store.get(target)
        assert entry is not None
        assert entry["email"] == "work@co.com"
        assert "Mapped" in capsys.readouterr().out

    def test_map_nonexistent_path_warns_but_maps(self, temp_home, capsys):
        from claude_swap.mappings import MappingStore
        self._seeded_switcher_env(temp_home)
        target = temp_home / "not-created-yet"

        with patch("os.geteuid", return_value=1000, create=True):
            cli._map_command(["2", str(target)])

        assert MappingStore(ClaudeAccountSwitcher().backup_dir).get(target) is not None
        assert "is not an existing directory" in capsys.readouterr().out

    def test_map_by_email_defaults_to_cwd(self, temp_home, monkeypatch, capsys):
        from claude_swap.mappings import MappingStore
        self._seeded_switcher_env(temp_home)
        cwd = temp_home / "here"
        cwd.mkdir()
        monkeypatch.chdir(cwd)

        with patch("os.geteuid", return_value=1000, create=True):
            cli._map_command(["work@co.com"])

        store = MappingStore(ClaudeAccountSwitcher().backup_dir)
        assert store.get(cwd) is not None

    def test_map_unknown_account_errors(self, temp_home, capsys):
        self._seeded_switcher_env(temp_home)
        with patch("os.geteuid", return_value=1000, create=True):
            with pytest.raises(SystemExit) as exc:
                cli._map_command(["999", str(temp_home)])
        assert exc.value.code == 1
        assert "Error" in capsys.readouterr().err

    def test_map_list_empty(self, temp_home, capsys):
        self._seeded_switcher_env(temp_home)
        with patch("os.geteuid", return_value=1000, create=True):
            cli._map_command([])
        assert "No directory mappings yet" in capsys.readouterr().out

    def test_map_list_shows_entries(self, temp_home, capsys):
        from claude_swap.mappings import MappingStore
        switcher = self._seeded_switcher_env(temp_home)
        target = temp_home / "proj"
        target.mkdir()
        MappingStore(switcher.backup_dir).set(target, "work@co.com", "")

        with patch("os.geteuid", return_value=1000, create=True):
            cli._map_command([])

        out = capsys.readouterr().out
        assert "Directory mappings" in out
        assert "work@co.com" in out
        assert "2:" in out  # slot number resolved

    def test_map_list_flags_removed_account(self, temp_home, capsys):
        from claude_swap.mappings import MappingStore
        switcher = self._seeded_switcher_env(temp_home)
        target = temp_home / "proj"
        target.mkdir()
        # Map an account identity that is not in the sequence.
        MappingStore(switcher.backup_dir).set(target, "ghost@co.com", "")

        with patch("os.geteuid", return_value=1000, create=True):
            cli._map_command([])

        assert "account removed" in capsys.readouterr().out

    def test_unmap_removes(self, temp_home, capsys):
        from claude_swap.mappings import MappingStore
        switcher = self._seeded_switcher_env(temp_home)
        target = temp_home / "proj"
        target.mkdir()
        store = MappingStore(switcher.backup_dir)
        store.set(target, "work@co.com", "")

        with patch("os.geteuid", return_value=1000, create=True):
            cli._unmap_command([str(target)])

        assert store.get(target) is None
        assert "Unmapped" in capsys.readouterr().out

    def test_unmap_nonexistent_notes(self, temp_home, capsys):
        self._seeded_switcher_env(temp_home)
        target = temp_home / "proj"
        target.mkdir()
        with patch("os.geteuid", return_value=1000, create=True):
            cli._unmap_command([str(target)])
        assert "No mapping for" in capsys.readouterr().out

    def test_map_dispatched_from_main(self, temp_home):
        """`cswap map` routes through main() to _map_command."""
        with patch("claude_swap.cli._map_command") as map_fn, \
             patch.object(sys, "argv", ["claude-swap", "map", "2", "/tmp/x"]):
            cli.main()
        map_fn.assert_called_once_with(["2", "/tmp/x"])

    def test_unmap_dispatched_from_main(self, temp_home):
        with patch("claude_swap.cli._unmap_command") as unmap_fn, \
             patch.object(sys, "argv", ["claude-swap", "unmap", "/tmp/x"]):
            cli.main()
        unmap_fn.assert_called_once_with(["/tmp/x"])

    @pytest.mark.skipif(sys.platform == "win32", reason="root guard is POSIX-only")
    def test_unmap_refuses_root(self, temp_home, capsys):
        self._seeded_switcher_env(temp_home)
        with patch("os.geteuid", return_value=0, create=True), \
             patch.object(ClaudeAccountSwitcher, "_is_running_in_container", return_value=False):
            with pytest.raises(SystemExit) as exc:
                cli._unmap_command([str(temp_home)])
        assert exc.value.code == 1
        assert "root" in capsys.readouterr().err

    @pytest.mark.skipif(sys.platform == "win32", reason="root guard is POSIX-only")
    def test_map_refuses_root(self, temp_home, capsys):
        self._seeded_switcher_env(temp_home)
        with patch("os.geteuid", return_value=0, create=True), \
             patch.object(ClaudeAccountSwitcher, "_is_running_in_container", return_value=False):
            with pytest.raises(SystemExit) as exc:
                cli._map_command(["2", str(temp_home)])
        assert exc.value.code == 1
        assert "root" in capsys.readouterr().err


class TestAliasCommand:
    """`cswap alias` — set/unset/list a short display alias for an account."""

    def _seeded_switcher_env(self, temp_home):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._init_sequence_file()
        data = switcher._get_sequence_data()
        data["accounts"]["2"] = {
            "email": "work@co.com",
            "uuid": "u2",
            "organizationUuid": "",
            "organizationName": "",
            "added": "2024-01-01T00:00:00Z",
        }
        data["sequence"] = [2]
        switcher._write_json(switcher.sequence_file, data)
        return switcher

    def test_set_alias_by_number(self, temp_home, capsys):
        self._seeded_switcher_env(temp_home)
        with patch("os.geteuid", return_value=1000, create=True):
            cli._alias_command(["2", "dev"])

        data = ClaudeAccountSwitcher()._get_sequence_data()
        assert data["accounts"]["2"]["alias"] == "dev"
        assert "dev" in capsys.readouterr().out

    def test_set_alias_by_email(self, temp_home, capsys):
        self._seeded_switcher_env(temp_home)
        with patch("os.geteuid", return_value=1000, create=True):
            cli._alias_command(["work@co.com", "dev"])

        data = ClaudeAccountSwitcher()._get_sequence_data()
        assert data["accounts"]["2"]["alias"] == "dev"

    def test_unset_alias(self, temp_home, capsys):
        switcher = self._seeded_switcher_env(temp_home)
        data = switcher._get_sequence_data()
        data["accounts"]["2"]["alias"] = "dev"
        switcher._write_json(switcher.sequence_file, data)

        with patch("os.geteuid", return_value=1000, create=True):
            cli._alias_command(["2", "--unset"])

        data = ClaudeAccountSwitcher()._get_sequence_data()
        assert "alias" not in data["accounts"]["2"]

    def test_list_aliases(self, temp_home, capsys):
        switcher = self._seeded_switcher_env(temp_home)
        data = switcher._get_sequence_data()
        data["accounts"]["2"]["alias"] = "dev"
        switcher._write_json(switcher.sequence_file, data)

        with patch("os.geteuid", return_value=1000, create=True):
            cli._alias_command([])

        out = capsys.readouterr().out
        assert "dev" in out

    def test_missing_name_errors(self, temp_home, capsys):
        self._seeded_switcher_env(temp_home)
        with patch("os.geteuid", return_value=1000, create=True):
            with pytest.raises(SystemExit):
                cli._alias_command(["2"])

    def test_unset_without_account_errors(self, temp_home, capsys):
        """`cswap alias --unset` with no target must error, not silently list."""
        self._seeded_switcher_env(temp_home)
        with patch("os.geteuid", return_value=1000, create=True):
            with pytest.raises(SystemExit):
                cli._alias_command(["--unset"])

    def test_unset_with_name_errors(self, temp_home, capsys):
        self._seeded_switcher_env(temp_home)
        with patch("os.geteuid", return_value=1000, create=True):
            with pytest.raises(SystemExit):
                cli._alias_command(["2", "dev", "--unset"])

    def test_invalid_alias_errors(self, temp_home, capsys):
        self._seeded_switcher_env(temp_home)
        with patch("os.geteuid", return_value=1000, create=True):
            with pytest.raises(SystemExit) as exc:
                cli._alias_command(["2", "123"])
        assert exc.value.code == 1
        assert "Error" in capsys.readouterr().err

    def test_unknown_account_errors(self, temp_home, capsys):
        self._seeded_switcher_env(temp_home)
        with patch("os.geteuid", return_value=1000, create=True):
            with pytest.raises(SystemExit) as exc:
                cli._alias_command(["999", "dev"])
        assert exc.value.code == 1

    def test_dispatched_from_main(self, temp_home):
        with patch("claude_swap.cli._alias_command") as alias_fn, \
             patch.object(sys, "argv", ["claude-swap", "alias", "2", "dev"]):
            cli.main()
        alias_fn.assert_called_once_with(["2", "dev"])

    @pytest.mark.skipif(sys.platform == "win32", reason="root guard is POSIX-only")
    def test_alias_refuses_root(self, temp_home, capsys):
        self._seeded_switcher_env(temp_home)
        with patch("os.geteuid", return_value=0, create=True), \
             patch.object(ClaudeAccountSwitcher, "_is_running_in_container", return_value=False):
            with pytest.raises(SystemExit) as exc:
                cli._alias_command(["2", "dev"])
        assert exc.value.code == 1
        assert "root" in capsys.readouterr().err

    def test_add_with_alias_flag(self, temp_home, mock_claude_config, capsys):
        fake_creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})
        with patch("os.geteuid", return_value=1000, create=True), \
             patch.object(ClaudeAccountSwitcher, "_read_credentials", return_value=fake_creds), \
             patch.object(ClaudeAccountSwitcher, "_write_account_credentials"), \
             patch.object(sys, "argv", ["claude-swap", "add", "--alias", "dev"]):
            cli.main()

        data = ClaudeAccountSwitcher()._get_sequence_data()
        assert data["accounts"]["1"]["alias"] == "dev"

    def test_alias_flag_without_add_errors(self, temp_home, capsys):
        with patch("os.geteuid", return_value=1000, create=True), \
             patch.object(sys, "argv", ["claude-swap", "list", "--alias", "dev"]):
            with pytest.raises(SystemExit) as exc:
                cli.main()
        assert exc.value.code == 2
        assert "--alias can only be used with 'add'" in capsys.readouterr().err


class TestRunAutoResolve:
    """`cswap run` with no account resolves the cwd's directory mapping."""

    def _fake_manager(self, calls):
        class FakeSessionManager:
            def __init__(self, switcher):
                pass

            def run(self, identifier, claude_args, share=True, share_history=False):
                calls.append(("run", identifier, claude_args, share, share_history))

            def exec_default(self, claude_args):
                calls.append(("exec_default", claude_args))

        return FakeSessionManager

    def _fake_switcher(self, backup, seq):
        sw = MagicMock()
        sw.backup_dir = backup
        sw._get_sequence_data_migrated.return_value = seq
        # Use the real resolvers, not MagicMock auto-attrs.
        sw._find_account_slot = ClaudeAccountSwitcher._find_account_slot
        sw.slot_for_directory = (
            lambda d: ClaudeAccountSwitcher.slot_for_directory(sw, d)
        )
        return sw

    def test_mapped_dir_runs_resolved_account(self, tmp_path, monkeypatch):
        from claude_swap.mappings import MappingStore

        repo = tmp_path / "work" / "client-app"
        repo.mkdir(parents=True)
        backup = tmp_path / "backup"
        backup.mkdir()
        MappingStore(backup).set(repo, "work@co.com", "org-1")
        seq = {
            "accounts": {
                "2": {
                    "email": "work@co.com",
                    "organizationUuid": "org-1",
                    "organizationName": "Co",
                }
            },
            "sequence": [2],
        }
        calls = []
        monkeypatch.chdir(repo)
        with patch("claude_swap.session.SessionManager", self._fake_manager(calls)), \
             patch("claude_swap.cli.ClaudeAccountSwitcher",
                   return_value=self._fake_switcher(backup, seq)), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch.object(sys, "argv", ["claude-swap", "run"]):
            cli.main()
        assert ("run", "2", [], True, False) in calls

    def test_mapped_subdir_inherits(self, tmp_path, monkeypatch):
        from claude_swap.mappings import MappingStore

        repo = tmp_path / "work"
        sub = repo / "client" / "src"
        sub.mkdir(parents=True)
        backup = tmp_path / "backup"
        backup.mkdir()
        MappingStore(backup).set(repo, "work@co.com", "")
        seq = {
            "accounts": {"2": {"email": "work@co.com", "organizationUuid": "",
                                "organizationName": ""}},
            "sequence": [2],
        }
        calls = []
        monkeypatch.chdir(sub)
        with patch("claude_swap.session.SessionManager", self._fake_manager(calls)), \
             patch("claude_swap.cli.ClaudeAccountSwitcher",
                   return_value=self._fake_switcher(backup, seq)), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch.object(sys, "argv", ["claude-swap", "run"]):
            cli.main()
        assert ("run", "2", [], True, False) in calls

    def test_unmapped_dir_falls_back_to_default(self, tmp_path, monkeypatch, capsys):
        backup = tmp_path / "backup"
        backup.mkdir()
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        calls = []
        monkeypatch.chdir(scratch)
        with patch("claude_swap.session.SessionManager", self._fake_manager(calls)), \
             patch("claude_swap.cli.ClaudeAccountSwitcher",
                   return_value=self._fake_switcher(backup, {"accounts": {}, "sequence": []})), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch.object(sys, "argv", ["claude-swap", "run"]):
            cli.main()
        assert ("exec_default", []) in calls
        assert "No account mapped" in capsys.readouterr().out

    def test_removed_account_falls_back_with_warning(self, tmp_path, monkeypatch, capsys):
        from claude_swap.mappings import MappingStore

        repo = tmp_path / "repo"
        repo.mkdir()
        backup = tmp_path / "backup"
        backup.mkdir()
        MappingStore(backup).set(repo, "ghost@co.com", "")
        calls = []
        monkeypatch.chdir(repo)
        with patch("claude_swap.session.SessionManager", self._fake_manager(calls)), \
             patch("claude_swap.cli.ClaudeAccountSwitcher",
                   return_value=self._fake_switcher(backup, {"accounts": {}, "sequence": []})), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch.object(sys, "argv", ["claude-swap", "run"]):
            cli.main()
        assert ("exec_default", []) in calls
        assert "no longer exists" in capsys.readouterr().out

    def test_explicit_account_still_runs(self, tmp_path, monkeypatch):
        """An explicit account argument bypasses mapping resolution."""
        backup = tmp_path / "backup"
        backup.mkdir()
        calls = []
        monkeypatch.chdir(tmp_path)
        with patch("claude_swap.session.SessionManager", self._fake_manager(calls)), \
             patch("claude_swap.cli.ClaudeAccountSwitcher",
                   return_value=self._fake_switcher(backup, {"accounts": {}, "sequence": []})), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch.object(sys, "argv", ["claude-swap", "run", "3"]):
            cli.main()
        assert ("run", "3", [], True, False) in calls

    def test_no_account_forwards_tail(self, tmp_path, monkeypatch):
        from claude_swap.mappings import MappingStore

        repo = tmp_path / "repo"
        repo.mkdir()
        backup = tmp_path / "backup"
        backup.mkdir()
        MappingStore(backup).set(repo, "work@co.com", "")
        seq = {
            "accounts": {"2": {"email": "work@co.com", "organizationUuid": "",
                                "organizationName": ""}},
            "sequence": [2],
        }
        calls = []
        monkeypatch.chdir(repo)
        with patch("claude_swap.session.SessionManager", self._fake_manager(calls)), \
             patch("claude_swap.cli.ClaudeAccountSwitcher",
                   return_value=self._fake_switcher(backup, seq)), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch.object(sys, "argv", ["claude-swap", "run", "--", "--resume"]):
            cli.main()
        assert ("run", "2", ["--resume"], True, False) in calls

    def test_no_account_forwards_share_history(self, tmp_path, monkeypatch):
        """--share-history survives the mapped-account resolution path."""
        from claude_swap.mappings import MappingStore

        repo = tmp_path / "repo"
        repo.mkdir()
        backup = tmp_path / "backup"
        backup.mkdir()
        MappingStore(backup).set(repo, "work@co.com", "")
        seq = {
            "accounts": {"2": {"email": "work@co.com", "organizationUuid": "",
                                "organizationName": ""}},
            "sequence": [2],
        }
        calls = []
        monkeypatch.chdir(repo)
        with patch("claude_swap.session.SessionManager", self._fake_manager(calls)), \
             patch("claude_swap.cli.ClaudeAccountSwitcher",
                   return_value=self._fake_switcher(backup, seq)), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch.object(sys, "argv", ["claude-swap", "run", "--share-history"]):
            cli.main()
        assert ("run", "2", [], True, True) in calls


class TestDisableEnableDispatch:
    """`cswap disable`/`cswap enable` (and the legacy --disable-account /
    --enable-account flags) forward to switcher.set_account_disabled."""

    def _run(self, argv):
        with patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["claude-swap", *argv]), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch("claude_swap.update_check.check_for_update", return_value=None):
            cli.main()
        return switcher_cls.return_value

    def test_disable_subcommand_forwards(self):
        switcher = self._run(["disable", "2"])
        switcher.set_account_disabled.assert_called_once_with("2", True)

    def test_enable_subcommand_forwards(self):
        switcher = self._run(["enable", "user@example.com"])
        switcher.set_account_disabled.assert_called_once_with("user@example.com", False)

    def test_legacy_disable_flag_forwards(self):
        switcher = self._run(["--disable-account", "3"])
        switcher.set_account_disabled.assert_called_once_with("3", True)

    def test_legacy_enable_flag_forwards(self):
        switcher = self._run(["--enable-account", "3"])
        switcher.set_account_disabled.assert_called_once_with("3", False)

    def test_disable_without_target_errors(self, capsys):
        with patch.object(sys, "argv", ["claude-swap", "disable"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 2


class TestAutostartCommand:
    """`cswap autostart [on|off]` — the menu bar's login item, headlessly."""

    def _run(self, argv, tmp_path, capsys, platform="darwin"):
        agent = tmp_path / "Library" / "LaunchAgents" / "com.claude-swap.menubar.plist"
        with patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch("claude_swap.autostart.launch_agent_path", return_value=agent), \
             patch("claude_swap.autostart.subprocess.run"), \
             patch.object(sys, "argv", ["claude-swap", *argv]), \
             patch.object(cli.sys, "platform", platform), \
             patch("claude_swap.update_check.check_for_update", return_value=None):
            switcher_cls.return_value.backup_dir = tmp_path / "backup"
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        return excinfo.value.code, capsys.readouterr(), agent

    def test_translate_subcommand(self):
        assert cli._translate_subcommand(["autostart"]) == ["--autostart"]
        assert cli._translate_subcommand(["autostart", "on"]) == ["--autostart", "on"]

    def test_status_when_disabled(self, tmp_path, capsys):
        code, out, agent = self._run(["autostart"], tmp_path, capsys)
        assert code == 0
        assert "off" in out.out
        assert not agent.exists()

    def test_on_writes_the_login_item(self, tmp_path, capsys):
        code, out, agent = self._run(["autostart", "on"], tmp_path, capsys)
        assert code == 0
        assert agent.exists()
        assert "next login" in out.out

    def test_off_removes_it(self, tmp_path, capsys):
        self._run(["autostart", "on"], tmp_path, capsys)
        code, out, agent = self._run(["autostart", "off"], tmp_path, capsys)
        assert code == 0
        assert not agent.exists()

    def test_off_when_never_enabled_is_not_an_error(self, tmp_path, capsys):
        code, out, _ = self._run(["autostart", "off"], tmp_path, capsys)
        assert code == 0
        assert "not enabled" in out.out

    def test_json_status(self, tmp_path, capsys):
        self._run(["autostart", "on"], tmp_path, capsys)
        code, out, _ = self._run(["autostart", "--json"], tmp_path, capsys)
        payload = json.loads(out.out)
        assert code == 0
        assert payload["enabled"] is True
        assert payload["label"] == "com.claude-swap.menubar"

    def test_rejects_unknown_mode(self, capsys):
        with patch.object(sys, "argv", ["claude-swap", "autostart", "sometimes"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 2

    def test_refuses_on_non_macos(self, tmp_path, capsys):
        code, out, agent = self._run(["autostart", "on"], tmp_path, capsys, platform="linux")
        assert code == 1
        assert "only available on macOS" in out.err
        assert not agent.exists()
