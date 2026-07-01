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
        from claude_swap.mappings import MappingStore, normalize_path
        self._seeded_switcher_env(temp_home)
        target = temp_home / "proj"
        target.mkdir()

        with patch("os.geteuid", return_value=1000):
            cli._map_command(["2", str(target)])

        store = MappingStore(ClaudeAccountSwitcher().backup_dir)
        entry = store.get(target)
        assert entry is not None
        assert entry["email"] == "work@co.com"
        assert "Mapped" in capsys.readouterr().out

    def test_map_by_email_defaults_to_cwd(self, temp_home, monkeypatch, capsys):
        from claude_swap.mappings import MappingStore
        self._seeded_switcher_env(temp_home)
        cwd = temp_home / "here"
        cwd.mkdir()
        monkeypatch.chdir(cwd)

        with patch("os.geteuid", return_value=1000):
            cli._map_command(["work@co.com"])

        store = MappingStore(ClaudeAccountSwitcher().backup_dir)
        assert store.get(cwd) is not None

    def test_map_unknown_account_errors(self, temp_home, capsys):
        self._seeded_switcher_env(temp_home)
        with patch("os.geteuid", return_value=1000):
            with pytest.raises(SystemExit) as exc:
                cli._map_command(["999", str(temp_home)])
        assert exc.value.code == 1
        assert "Error" in capsys.readouterr().err

    def test_map_list_empty(self, temp_home, capsys):
        self._seeded_switcher_env(temp_home)
        with patch("os.geteuid", return_value=1000):
            cli._map_command([])
        assert "No directory mappings yet" in capsys.readouterr().out

    def test_map_list_shows_entries(self, temp_home, capsys):
        from claude_swap.mappings import MappingStore
        switcher = self._seeded_switcher_env(temp_home)
        target = temp_home / "proj"
        target.mkdir()
        MappingStore(switcher.backup_dir).set(target, "work@co.com", "")

        with patch("os.geteuid", return_value=1000):
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

        with patch("os.geteuid", return_value=1000):
            cli._map_command([])

        assert "account removed" in capsys.readouterr().out

    def test_unmap_removes(self, temp_home, capsys):
        from claude_swap.mappings import MappingStore
        switcher = self._seeded_switcher_env(temp_home)
        target = temp_home / "proj"
        target.mkdir()
        store = MappingStore(switcher.backup_dir)
        store.set(target, "work@co.com", "")

        with patch("os.geteuid", return_value=1000):
            cli._unmap_command([str(target)])

        assert store.get(target) is None
        assert "Unmapped" in capsys.readouterr().out

    def test_unmap_nonexistent_notes(self, temp_home, capsys):
        self._seeded_switcher_env(temp_home)
        target = temp_home / "proj"
        target.mkdir()
        with patch("os.geteuid", return_value=1000):
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

    def test_unmap_refuses_root(self, temp_home, capsys):
        self._seeded_switcher_env(temp_home)
        with patch("os.geteuid", return_value=0), \
             patch.object(ClaudeAccountSwitcher, "_is_running_in_container", return_value=False):
            with pytest.raises(SystemExit) as exc:
                cli._unmap_command([str(temp_home)])
        assert exc.value.code == 1
        assert "root" in capsys.readouterr().err

    def test_map_refuses_root(self, temp_home, capsys):
        self._seeded_switcher_env(temp_home)
        with patch("os.geteuid", return_value=0), \
             patch.object(ClaudeAccountSwitcher, "_is_running_in_container", return_value=False):
            with pytest.raises(SystemExit) as exc:
                cli._map_command(["2", str(temp_home)])
        assert exc.value.code == 1
        assert "root" in capsys.readouterr().err


class TestRunAutoResolve:
    """`cswap run` with no account resolves the cwd's directory mapping."""

    def _fake_manager(self, calls):
        class FakeSessionManager:
            def __init__(self, switcher):
                pass

            def run(self, identifier, claude_args, share=True):
                calls.append(("run", identifier, claude_args, share))

            def exec_default(self, claude_args):
                calls.append(("exec_default", claude_args))

        return FakeSessionManager

    def _fake_switcher(self, backup, seq):
        sw = MagicMock()
        sw.backup_dir = backup
        sw._get_sequence_data_migrated.return_value = seq
        # Use the real composite-key resolver, not a MagicMock auto-attr.
        sw._find_account_slot = ClaudeAccountSwitcher._find_account_slot
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
             patch("os.geteuid", return_value=1000), \
             patch.object(sys, "argv", ["claude-swap", "run"]):
            cli.main()
        assert ("run", "2", [], True) in calls

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
             patch("os.geteuid", return_value=1000), \
             patch.object(sys, "argv", ["claude-swap", "run"]):
            cli.main()
        assert ("run", "2", [], True) in calls

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
             patch("os.geteuid", return_value=1000), \
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
             patch("os.geteuid", return_value=1000), \
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
             patch("os.geteuid", return_value=1000), \
             patch.object(sys, "argv", ["claude-swap", "run", "3"]):
            cli.main()
        assert ("run", "3", [], True) in calls

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
             patch("os.geteuid", return_value=1000), \
             patch.object(sys, "argv", ["claude-swap", "run", "--", "--resume"]):
            cli.main()
        assert ("run", "2", ["--resume"], True) in calls
