"""Tests for the CLI module."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_swap import __version__
from claude_swap import cli
from claude_swap.credentials import ActiveCredentials
from claude_swap.settings import set_setting
from claude_swap.switcher import ClaudeAccountSwitcher

# src layout: ensure subprocess can find claude_swap
_SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")

# A throwaway HOME for subprocesses. The in-process autouse Keychain/HOME guards
# do NOT reach child processes, so a spawned ``python -m claude_swap`` would
# otherwise resolve to the developer's real ``~/.claude-swap-backup`` and run the
# data migration against real accounts (touching the real Keychain on macOS). An empty,
# isolated HOME has no ``sequence.json`` → the migration skips before any Keychain
# access, and no ``.claude.json`` → no account to read.
# Allocated under pytest's basetemp so its own retention reclaims it, rather
# than a sweep at exit that a signal-killed worker never reaches.
_ISOLATED_HOME: str | None = None


@pytest.fixture(autouse=True, scope="session")
def _isolated_subprocess_home(tmp_path_factory):
    global _ISOLATED_HOME
    _ISOLATED_HOME = str(tmp_path_factory.mktemp("subproc-home"))


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
        # Falling through here would point the child at the real HOME.
        assert _ISOLATED_HOME is not None, "_isolated_subprocess_home did not run"
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
        # Plain `cswap menubar` runs the app only when it IS the launchd job.
        monkeypatch.setattr(
            "claude_swap.launch_agent.service_pid", lambda *a, **k: os.getpid()
        )
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
        # Plain `cswap menubar` runs the app only when it IS the launchd job.
        monkeypatch.setattr(
            "claude_swap.launch_agent.service_pid", lambda *a, **k: os.getpid()
        )
        monkeypatch.setattr(cli.os, "geteuid", lambda: 1000, raising=False)

        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 0
        assert called.get("ran") is True

    def _service_harness(self, monkeypatch, argv):
        """Drive `cswap menubar <service flag>` with launch_agent stubbed out."""
        seen = {"menubar_ran": False}

        class _FakeSwitcher:
            def __init__(self, *a, **k):
                pass

            def _is_running_in_container(self):
                return False

        def _fake_menubar(switcher):
            seen["menubar_ran"] = True
            return 0

        def _record(name, payload):
            def _call(*a, **k):
                seen["called"] = name
                return payload

            return _call

        monkeypatch.setattr(cli, "ClaudeAccountSwitcher", _FakeSwitcher)
        monkeypatch.setattr(sys, "argv", argv)
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr("claude_swap.menubar.run", _fake_menubar, raising=False)
        monkeypatch.setattr(cli.os, "geteuid", lambda: 1000, raising=False)
        monkeypatch.setattr(
            "claude_swap.launch_agent.install",
            _record(
                "install",
                {
                    "label": "com.cswap.menubar",
                    "plist": "/tmp/p.plist",
                    "program": ["/tmp/cswap", "menubar"],
                    "stdout_log": "/tmp/o.log",
                    "stderr_log": "/tmp/e.log",
                    "profile_changes": [],
                },
            ),
        )
        monkeypatch.setattr(
            "claude_swap.launch_agent.uninstall",
            _record(
                "uninstall",
                {
                    "label": "com.cswap.menubar",
                    "was_loaded": True,
                    "removed_plist": True,
                    "removed": ["/tmp/p.plist", "/Applications/claude-swap Menubar.app"],
                    "cleared_override": False,
                    "bundle_error": None,
                },
            ),
        )
        monkeypatch.setattr(
            "claude_swap.launch_agent.status",
            _record(
                "status",
                {
                    "label": "com.cswap.menubar",
                    "installed": True,
                    "start_at_login": True,
                    "session_plist": None,
                    "configured": True,
                    "loaded": True,
                    "state": "running",
                    "pid": 4242,
                    "plist": "/tmp/p.plist",
                    "switched_off": False,
                    "running_build": None,
                    "installed_build": "1.0.0",
                    "stale_build": False,
                },
            ),
        )
        monkeypatch.setattr("claude_swap.launch_agent.service_pid", lambda *a, **k: None)
        # The dev venv has no rumps; the service paths must see the extra as present.
        monkeypatch.setattr(cli, "_menubar_extra_missing", lambda: None)
        monkeypatch.setattr(
            "claude_swap.launch_agent.ensure",
            _record(
                "ensure",
                {
                    "label": "com.cswap.menubar",
                    "action": "first-setup",
                    "start_at_login": True,
                    "plist": "/tmp/p.plist",
                    "program": ["/tmp/cswap", "menubar", "--foreground"],
                    "stdout_log": "/tmp/o.log",
                    "stderr_log": "/tmp/e.log",
                    "pid": 4242,
                    "switched_off": False,
                    "stale_build": False,
                },
            ),
        )
        return seen

    def test_menubar_install_service_routes_to_launch_agent(self, monkeypatch, capsys):
        seen = self._service_harness(monkeypatch, ["cswap", "menubar", "--install-service"])

        with pytest.raises(SystemExit) as exc:
            cli.main()

        assert exc.value.code == 0
        assert seen["called"] == "install"
        # The service flags must not also start a foreground menu bar.
        assert seen["menubar_ran"] is False
        assert "installed" in capsys.readouterr().out

    def test_install_service_warns_when_the_interpreter_draws_nothing(
        self, monkeypatch, capsys
    ):
        # Installing a login service for a menu bar that cannot draw is the
        # worst case: it survives reboots and shows nothing. See issue #310.
        self._service_harness(monkeypatch, ["cswap", "menubar", "--install-service"])
        monkeypatch.setattr(
            "claude_swap.menubar.framework_build_warning", lambda *a: "3.14 draws nothing"
        )

        with pytest.raises(SystemExit):
            cli.main()

        captured = capsys.readouterr()
        assert "3.14 draws nothing" in (captured.out + captured.err)

    def test_install_service_stays_quiet_on_a_supported_interpreter(
        self, monkeypatch, capsys
    ):
        self._service_harness(monkeypatch, ["cswap", "menubar", "--install-service"])
        monkeypatch.setattr(
            "claude_swap.menubar.framework_build_warning", lambda *a: None
        )

        with pytest.raises(SystemExit):
            cli.main()

        captured = capsys.readouterr()
        assert "draws nothing" not in (captured.out + captured.err)

    def test_menubar_uninstall_service_routes_to_launch_agent(self, monkeypatch, capsys):
        seen = self._service_harness(monkeypatch, ["cswap", "menubar", "--uninstall-service"])

        with pytest.raises(SystemExit) as exc:
            cli.main()

        assert exc.value.code == 0
        assert seen["called"] == "uninstall"
        assert seen["menubar_ran"] is False
        assert "removed" in capsys.readouterr().out

    def test_menubar_service_status_reports_state_and_pid(self, monkeypatch, capsys):
        seen = self._service_harness(monkeypatch, ["cswap", "menubar", "--service-status"])

        with pytest.raises(SystemExit) as exc:
            cli.main()

        assert exc.value.code == 0
        assert seen["called"] == "status"
        out = capsys.readouterr().out
        assert "running" in out and "4242" in out

    def test_menubar_service_flags_still_refuse_off_macos(self, monkeypatch):
        self._service_harness(monkeypatch, ["cswap", "menubar", "--install-service"])
        monkeypatch.setattr(sys, "platform", "linux")

        with pytest.raises(SystemExit) as exc:
            cli.main()

        assert exc.value.code == 1

    def test_service_flags_are_rejected_outside_menubar(self, monkeypatch, capsys):
        # `--full` already guards this way; without a matching check
        # `cswap list --install-service` would be accepted and silently ignored.
        monkeypatch.setattr(sys, "argv", ["cswap", "list", "--install-service"])

        with pytest.raises(SystemExit) as exc:
            cli.main()

        assert exc.value.code == 2
        assert "can only be used with 'menubar'" in capsys.readouterr().err

    def _menubar_settings(self, monkeypatch, tmp_path):
        monkeypatch.setattr("claude_swap.paths.get_backup_root", lambda: tmp_path)
        return tmp_path / "menubar_settings.json"

    def test_install_service_auto_switch_turns_the_toggle_on_before_install(
        self, monkeypatch, tmp_path, capsys
    ):
        # install() restarts a running service and the menu bar reads the
        # toggle once at launch, so the write has to land first.
        path = self._menubar_settings(monkeypatch, tmp_path)
        seen = self._service_harness(
            monkeypatch, ["cswap", "menubar", "--install-service", "--auto-switch"]
        )
        at_install = {}

        def fake_install():
            at_install["enabled"] = json.loads(path.read_text())["auto_switch_enabled"]
            seen["called"] = "install"
            return {
                "label": "com.cswap.menubar",
                "plist": "/tmp/p.plist",
                "stderr_log": "/tmp/e.log",
                "profile_changes": [],
            }

        monkeypatch.setattr("claude_swap.launch_agent.install", fake_install)

        with pytest.raises(SystemExit) as exc:
            cli.main()

        assert exc.value.code == 0
        assert at_install == {"enabled": True}
        assert "auto-switch: on" in capsys.readouterr().out

    def test_install_service_says_when_auto_switch_is_off(
        self, monkeypatch, tmp_path, capsys
    ):
        # The toggle defaults to off and lives only in the menu: a service
        # installed from a terminal otherwise runs as a display for weeks.
        path = self._menubar_settings(monkeypatch, tmp_path)
        self._service_harness(monkeypatch, ["cswap", "menubar", "--install-service"])

        with pytest.raises(SystemExit):
            cli.main()

        captured = capsys.readouterr()
        assert "auto-switch: off" in captured.err
        assert "--auto-switch" in captured.err
        assert not path.exists()  # reporting never writes the setting

    def test_service_status_reports_auto_switch(self, monkeypatch, tmp_path, capsys):
        path = self._menubar_settings(monkeypatch, tmp_path)
        path.write_text(json.dumps({"auto_switch_enabled": True}))
        self._service_harness(monkeypatch, ["cswap", "menubar", "--service-status"])

        with pytest.raises(SystemExit):
            cli.main()

        assert "auto-switch: on" in capsys.readouterr().out

    @pytest.mark.parametrize(
        "argv",
        [
            ["cswap", "menubar", "--foreground", "--auto-switch"],
            ["cswap", "menubar", "--service-status", "--no-auto-switch"],
            ["cswap", "list", "--auto-switch"],
        ],
    )
    def test_auto_switch_is_rejected_where_nothing_would_read_it(self, monkeypatch, argv, capsys):
        monkeypatch.setattr(sys, "argv", argv)

        with pytest.raises(SystemExit) as exc:
            cli.main()

        assert exc.value.code == 2
        assert "--auto-switch/--no-auto-switch can only be used with" in capsys.readouterr().err

    def test_install_service_no_auto_switch_turns_the_toggle_off(
        self, monkeypatch, tmp_path, capsys
    ):
        path = self._menubar_settings(monkeypatch, tmp_path)
        path.write_text(json.dumps({"auto_switch_enabled": True, "title_pct": "5h"}))
        self._service_harness(
            monkeypatch, ["cswap", "menubar", "--install-service", "--no-auto-switch"]
        )

        with pytest.raises(SystemExit) as exc:
            cli.main()

        assert exc.value.code == 0
        saved = json.loads(path.read_text())
        assert saved["auto_switch_enabled"] is False
        assert saved["title_pct"] == "5h"  # other settings survive
        assert "auto-switch: off" in capsys.readouterr().err

    def test_install_service_failure_restores_the_toggle(self, monkeypatch, tmp_path):
        # A failed install must not leave auto-switch changed with no
        # service running behind it.
        path = self._menubar_settings(monkeypatch, tmp_path)
        self._service_harness(
            monkeypatch, ["cswap", "menubar", "--install-service", "--auto-switch"]
        )

        def failing_install():
            raise RuntimeError("bootstrap failed")

        monkeypatch.setattr("claude_swap.launch_agent.install", failing_install)

        with pytest.raises(BaseException):
            cli.main()

        assert json.loads(path.read_text())["auto_switch_enabled"] is False

    def _ensure_payload(self, **overrides):
        payload = {
            "label": "com.cswap.menubar",
            "action": "started",
            "start_at_login": True,
            "plist": "/tmp/p.plist",
            "program": ["/tmp/cswap", "menubar", "--foreground"],
            "stdout_log": "/tmp/o.log",
            "stderr_log": "/tmp/e.log",
            "pid": 4242,
            "switched_off": False,
            "stale_build": False,
        }
        payload.update(overrides)
        return payload

    def test_plain_menubar_starts_the_service_and_returns(self, monkeypatch, capsys):
        # The menu bar is launchd's to run: the command makes sure it is up and
        # comes back, instead of blocking this terminal for the life of the icon.
        seen = self._service_harness(monkeypatch, ["cswap", "menubar"])

        with pytest.raises(SystemExit) as exc:
            cli.main()

        assert exc.value.code == 0
        assert seen["called"] == "ensure"
        assert seen["menubar_ran"] is False
        out = capsys.readouterr().out
        assert "Menu bar started (pid 4242)" in out
        assert "starts at login" in out  # first setup says what it arranged

    def test_plain_menubar_inside_the_launchd_job_runs_the_app(self, monkeypatch):
        # launchd's own job is recognised by pid, so a plist written before the
        # --foreground flag existed (plain `cswap menubar`) still draws the icon
        # instead of trying to start the service it already is.
        seen = self._service_harness(monkeypatch, ["cswap", "menubar"])
        monkeypatch.setattr("claude_swap.launch_agent.service_pid", lambda *a, **k: os.getpid())

        with pytest.raises(SystemExit) as exc:
            cli.main()

        assert exc.value.code == 0
        assert seen["menubar_ran"] is True
        assert "called" not in seen

    def test_plain_menubar_reports_an_already_running_service(self, monkeypatch, capsys):
        self._service_harness(monkeypatch, ["cswap", "menubar"])
        monkeypatch.setattr(
            "claude_swap.launch_agent.ensure",
            lambda **k: self._ensure_payload(action="already-running"),
        )

        with pytest.raises(SystemExit) as exc:
            cli.main()

        assert exc.value.code == 0
        assert "already running (pid 4242)" in capsys.readouterr().out

    def test_plain_menubar_says_when_the_start_is_for_this_session_only(self, monkeypatch, capsys):
        self._service_harness(monkeypatch, ["cswap", "menubar"])
        monkeypatch.setattr(
            "claude_swap.launch_agent.ensure",
            lambda **k: self._ensure_payload(action="started", start_at_login=False),
        )

        with pytest.raises(SystemExit):
            cli.main()

        out = capsys.readouterr().out
        assert "Menu bar started (pid 4242)" in out
        assert "Start at login is off" in out

    def test_plain_menubar_says_when_the_login_items_switch_is_off(self, monkeypatch, capsys):
        # macOS lets the manual start through; the switch governs the login start.
        self._service_harness(monkeypatch, ["cswap", "menubar"])
        monkeypatch.setattr(
            "claude_swap.launch_agent.ensure",
            lambda **k: self._ensure_payload(action="started", switched_off=True),
        )

        with pytest.raises(SystemExit) as exc:
            cli.main()

        assert exc.value.code == 0
        captured = capsys.readouterr()
        assert "Menu bar started (pid 4242)" in captured.out
        assert "will not start it at login" in captured.err

    def test_service_status_says_when_the_login_items_switch_is_off(self, monkeypatch, capsys):
        self._service_harness(monkeypatch, ["cswap", "menubar", "--service-status"])
        status = {
            "label": "com.cswap.menubar", "installed": True, "start_at_login": True,
            "session_plist": None, "configured": True, "loaded": True,
            "state": "running", "pid": 4242, "plist": "/tmp/p.plist", "switched_off": True,
            "running_build": None, "installed_build": "1.0.0", "stale_build": False,
        }
        monkeypatch.setattr("claude_swap.launch_agent.status", lambda *a, **k: status)

        with pytest.raises(SystemExit):
            cli.main()

        assert "will not start it at login" in capsys.readouterr().err

    def test_foreground_runs_the_app_when_no_service_is_running(self, monkeypatch):
        seen = self._service_harness(monkeypatch, ["cswap", "menubar", "--foreground"])

        with pytest.raises(SystemExit) as exc:
            cli.main()

        assert exc.value.code == 0
        assert seen["menubar_ran"] is True
        assert "called" not in seen

    def test_foreground_refuses_while_the_service_is_running(self, monkeypatch, capsys):
        # Two instances would draw two icons over the same accounts.
        seen = self._service_harness(monkeypatch, ["cswap", "menubar", "--foreground"])
        monkeypatch.setattr("claude_swap.launch_agent.service_pid", lambda *a, **k: 4242)

        with pytest.raises(SystemExit) as exc:
            cli.main()

        assert exc.value.code == 1
        assert seen["menubar_ran"] is False
        assert "already running as a service (pid 4242)" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "argv",
        [
            ["cswap", "menubar", "--install-service", "--uninstall-service"],
            ["cswap", "menubar", "--foreground", "--service-status"],
            ["cswap", "menubar", "--install-service", "--foreground"],
        ],
    )
    def test_contradictory_service_flags_are_rejected(self, monkeypatch, argv, capsys):
        # Before, install + uninstall quietly chose install.
        self._service_harness(monkeypatch, argv)

        with pytest.raises(SystemExit) as exc:
            cli.main()

        assert exc.value.code == 2
        assert "not allowed with" in capsys.readouterr().err

    def test_foreground_is_rejected_outside_menubar(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["cswap", "list", "--foreground"])

        with pytest.raises(SystemExit) as exc:
            cli.main()

        assert exc.value.code == 2
        assert "can only be used with 'menubar'" in capsys.readouterr().err

    def test_plain_menubar_auto_switch_sets_the_toggle_then_restarts_a_running_service(
        self, monkeypatch, tmp_path, capsys
    ):
        # The menu bar reads the toggle once at launch: write first, then make
        # sure a running service is restarted so the new value applies.
        path = self._menubar_settings(monkeypatch, tmp_path)
        self._service_harness(monkeypatch, ["cswap", "menubar", "--auto-switch"])
        at_ensure = {}

        def fake_ensure(*, restart=False):
            at_ensure["enabled"] = json.loads(path.read_text())["auto_switch_enabled"]
            at_ensure["restart"] = restart
            return self._ensure_payload(action="restarted", pid=4243)

        monkeypatch.setattr("claude_swap.launch_agent.ensure", fake_ensure)

        with pytest.raises(SystemExit) as exc:
            cli.main()

        assert exc.value.code == 0
        assert at_ensure == {"enabled": True, "restart": True}
        out = capsys.readouterr().out
        assert "restarted (pid 4243) to apply the auto-switch setting" in out
        assert "auto-switch: on" in out

    def test_plain_menubar_auto_switch_already_on_does_not_ask_for_a_restart(
        self, monkeypatch, tmp_path
    ):
        path = self._menubar_settings(monkeypatch, tmp_path)
        path.write_text(json.dumps({"auto_switch_enabled": True}))
        self._service_harness(monkeypatch, ["cswap", "menubar", "--auto-switch"])
        seen_restart = {}

        def fake_ensure(*, restart=False):
            seen_restart["restart"] = restart
            return self._ensure_payload(action="already-running")

        monkeypatch.setattr("claude_swap.launch_agent.ensure", fake_ensure)

        with pytest.raises(SystemExit):
            cli.main()

        assert seen_restart == {"restart": False}

    def test_plain_menubar_failure_restores_the_toggle(self, monkeypatch, tmp_path):
        path = self._menubar_settings(monkeypatch, tmp_path)
        self._service_harness(monkeypatch, ["cswap", "menubar", "--auto-switch"])

        def failing_ensure(**k):
            raise RuntimeError("bootstrap failed")

        monkeypatch.setattr("claude_swap.launch_agent.ensure", failing_ensure)

        with pytest.raises(BaseException):
            cli.main()

        assert json.loads(path.read_text())["auto_switch_enabled"] is False

    @pytest.mark.parametrize("argv", [["cswap", "menubar"], ["cswap", "menubar", "--install-service"]])
    def test_the_service_is_never_installed_without_the_menubar_extra(
        self, monkeypatch, argv, capsys
    ):
        # A worker that dies on `import rumps` would crash-loop under launchd at
        # every login; refuse before anything is written or bootstrapped.
        seen = self._service_harness(monkeypatch, argv)
        monkeypatch.setattr(
            cli,
            "_menubar_extra_missing",
            lambda: "The menu bar needs the 'menubar' extra. Install it with: x",
        )

        with pytest.raises(SystemExit) as exc:
            cli.main()

        assert exc.value.code == 1
        assert "called" not in seen
        assert "needs the 'menubar' extra" in capsys.readouterr().err

    def test_menubar_extra_missing_names_the_installers_own_command(self, monkeypatch):
        import importlib.util

        monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
        monkeypatch.setattr("claude_swap.update_check._detect_install_method", lambda: "uv")
        assert "uv tool install --force 'claude-swap[menubar]'" in cli._menubar_extra_missing()
        monkeypatch.setattr("claude_swap.update_check._detect_install_method", lambda: "pipx")
        assert "pipx install --force" in cli._menubar_extra_missing()
        monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
        assert cli._menubar_extra_missing() is None

    def test_uninstall_service_lists_what_went(self, monkeypatch, capsys):
        self._service_harness(monkeypatch, ["cswap", "menubar", "--uninstall-service"])

        with pytest.raises(SystemExit) as exc:
            cli.main()

        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "Menu bar service removed." in out
        assert "removed /tmp/p.plist" in out
        assert "removed /Applications/claude-swap Menubar.app" in out

    def _purge_harness(self, monkeypatch, platform):
        calls = {}

        class _FakeSwitcher:
            backup_dir = Path("/tmp/backup-root")

            def __init__(self, *a, **k):
                pass

            def _is_running_in_container(self):
                return False

            def purge(self):
                calls["purged"] = True

        def fake_uninstall(**kwargs):
            calls["uninstall"] = kwargs
            return {
                "label": "com.cswap.menubar",
                "was_loaded": True,
                "removed_plist": True,
                "removed": ["/tmp/p.plist"],
                "cleared_override": False,
                "bundle_error": None,
            }

        monkeypatch.setattr(cli, "ClaudeAccountSwitcher", _FakeSwitcher)
        monkeypatch.setattr(sys, "argv", ["cswap", "purge"])
        monkeypatch.setattr(sys, "platform", platform)
        monkeypatch.setattr(cli.os, "geteuid", lambda: 1000, raising=False)
        monkeypatch.setattr("claude_swap.launch_agent.uninstall", fake_uninstall)
        monkeypatch.setattr("claude_swap.update_check.check_for_update", lambda *a, **k: None)
        return calls

    def test_purge_also_removes_the_menu_bar_service_on_macos(self, monkeypatch, capsys):
        # The LaunchAgent, the wrapper app and the logs live outside the data
        # directory; "remove all traces" has to reach them.
        calls = self._purge_harness(monkeypatch, "darwin")

        try:
            cli.main()
        except SystemExit as exc:
            assert exc.code in (0, None)

        assert calls["purged"] is True
        assert calls["uninstall"] == {"backup_dir": Path("/tmp/backup-root")}
        assert "Removed the menu bar service" in capsys.readouterr().out

    def test_purge_leaves_launchd_alone_off_macos(self, monkeypatch):
        calls = self._purge_harness(monkeypatch, "linux")

        try:
            cli.main()
        except SystemExit as exc:
            assert exc.code in (0, None)

        assert calls["purged"] is True
        assert "uninstall" not in calls

    def test_install_service_says_when_the_profile_moved(self, monkeypatch, capsys):
        self._service_harness(monkeypatch, ["cswap", "menubar", "--install-service"])
        monkeypatch.setattr(
            "claude_swap.launch_agent.install",
            lambda **k: {
                "label": "com.cswap.menubar", "plist": "/tmp/p.plist",
                "program": ["/tmp/cswap", "menubar", "--foreground"],
                "stdout_log": "/tmp/o.log", "stderr_log": "/tmp/e.log",
                "profile_changes": [("CLAUDE_CONFIG_DIR", None, "/new/profile")],
            },
        )

        with pytest.raises(SystemExit):
            cli.main()

        err = capsys.readouterr().err
        assert "now follows CLAUDE_CONFIG_DIR=/new/profile (was (unset))" in err

    def test_service_status_shows_the_running_build_and_a_stale_one(self, monkeypatch, capsys):
        self._service_harness(monkeypatch, ["cswap", "menubar", "--service-status"])
        status = {
            "label": "com.cswap.menubar", "installed": True, "start_at_login": True,
            "session_plist": None, "configured": True, "loaded": True,
            "state": "running", "pid": 4242, "plist": "/tmp/p.plist", "switched_off": False,
            "running_build": "1.0.0b1 (3fec656)", "installed_build": "1.0.0b1 (6a2a182)",
            "stale_build": True,
        }
        monkeypatch.setattr("claude_swap.launch_agent.status", lambda *a, **k: status)

        with pytest.raises(SystemExit):
            cli.main()

        captured = capsys.readouterr()
        assert "build: 1.0.0b1 (3fec656)" in captured.out
        assert "installed build is 1.0.0b1 (6a2a182)" in captured.err

    def test_service_status_reports_the_login_state(self, monkeypatch, capsys):
        self._service_harness(monkeypatch, ["cswap", "menubar", "--service-status"])

        with pytest.raises(SystemExit):
            cli.main()

        assert "starts at login: yes" in capsys.readouterr().out


class TestUpdateCheckSetting:
    """The passive update hint honours `updates.check` in settings.json."""

    def _run_list(self, backup_root: Path) -> MagicMock:
        check = MagicMock(return_value=None)
        with patch("claude_swap.cli.ClaudeAccountSwitcher"), \
             patch("claude_swap.paths.get_backup_root", return_value=backup_root), \
             patch.object(sys, "argv", ["claude-swap", "--list"]), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch("claude_swap.update_check.check_for_update", check):
            cli.main()
        return check

    def test_checks_by_default(self, tmp_path: Path):
        self._run_list(tmp_path).assert_called_once_with(__version__)

    def test_setting_false_skips_the_check(self, tmp_path: Path):
        from claude_swap.settings import set_setting

        set_setting(tmp_path, "updates.check", "false")
        self._run_list(tmp_path).assert_not_called()


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

    @pytest.fixture(autouse=True)
    def _backup_dir(self, tmp_path):
        """An empty backup root: nothing here may depend on settings.json."""
        self.backup_dir = tmp_path

    def _dispatch(self, argv: list[str], **switcher_attrs):
        """Run cli.main() with a fake SessionManager; returns recorded calls."""
        calls = []

        class FakeSessionManager:
            def __init__(self, switcher):
                calls.append(("init", switcher))

            def run(
                self,
                identifier,
                claude_args,
                share=True,
                share_history=None,
                require_session=False,
            ):
                calls.append((
                    "run", identifier, claude_args, share, share_history,
                    require_session,
                ))

        with patch("claude_swap.session.SessionManager", FakeSessionManager), \
             patch("claude_swap.cli.ClaudeAccountSwitcher",
                   return_value=MagicMock(backup_dir=self.backup_dir,
                                          **switcher_attrs)), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch.object(sys, "argv", ["claude-swap", *argv]):
            cli.main()
        return calls

    # share_history is forwarded as typed: None for "neither flag", which
    # SessionManager.run resolves against run.shareHistory (tested there).

    def test_run_dispatches_with_defaults(self):
        calls = self._dispatch(["run", "2"])
        assert ("run", "2", [], True, None, False) in calls

    def test_run_by_email(self):
        calls = self._dispatch(["run", "user@example.com"])
        assert ("run", "user@example.com", [], True, None, False) in calls

    def test_no_share_flag(self):
        calls = self._dispatch(["run", "2", "--no-share"])
        assert ("run", "2", [], False, None, False) in calls

    def test_share_history_flag(self):
        calls = self._dispatch(["run", "2", "--share-history"])
        assert ("run", "2", [], True, True, False) in calls

    def test_no_share_history_flag(self):
        calls = self._dispatch(["run", "2", "--no-share-history"])
        assert ("run", "2", [], True, False, False) in calls

    @pytest.mark.parametrize("stored", ["true", "false"])
    def test_share_history_setting_is_not_resolved_here(self, stored):
        """One reader for run.shareHistory, and it is not the CLI: a second
        one here is how a launcher ends up with its own idea of the default."""
        set_setting(self.backup_dir, "run.shareHistory", stored)
        calls = self._dispatch(["run", "2"])
        assert ("run", "2", [], True, None, False) in calls

    def test_require_session_flag(self):
        calls = self._dispatch(["run", "2", "--require-session"])
        assert ("run", "2", [], True, None, True) in calls

    def test_tail_forwarded_verbatim(self):
        calls = self._dispatch(["run", "2", "--", "--resume", "--model", "x"])
        assert ("run", "2", ["--resume", "--model", "x"], True, None, False) in calls

    def test_tail_may_contain_run_flags(self):
        """Args after `--` are NOT parsed by cswap, even if they look like ours."""
        calls = self._dispatch(["run", "2", "--", "--no-share"])
        assert ("run", "2", ["--no-share"], True, None, False) in calls

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
        # Sharing history is the default, so the help leads with the way out.
        assert "--share-history, --no-share-history" in out
        assert "This is the default" in out

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

            def run(
                self,
                identifier,
                claude_args,
                share=True,
                share_history=None,
                require_session=False,
            ):
                from claude_swap.exceptions import SessionError

                raise SessionError("boom")

        with patch("claude_swap.session.SessionManager", FailingSessionManager), \
             patch("claude_swap.cli.ClaudeAccountSwitcher",
                   return_value=MagicMock(backup_dir=self.backup_dir)), \
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

    def test_run_subcommand_still_dispatches(self, tmp_path):
        """`cswap run 2` keeps reaching the session pre-dispatch (not translated)."""
        calls = []

        class FakeSessionManager:
            def __init__(self, switcher):
                pass

            def run(
                self,
                identifier,
                claude_args,
                share=True,
                share_history=None,
                require_session=False,
            ):
                calls.append((identifier, claude_args, share))

        with patch("claude_swap.session.SessionManager", FakeSessionManager), \
             patch("claude_swap.cli.ClaudeAccountSwitcher",
                   return_value=MagicMock(backup_dir=tmp_path)), \
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

    def test_weekly_threshold_flag_overrides_settings_json(self, temp_home):
        from claude_swap.paths import get_backup_root

        backup = get_backup_root()
        backup.mkdir(parents=True, exist_ok=True)
        (backup / "settings.json").write_text(json.dumps({
            "schemaVersion": 1,
            "autoswitch": {"threshold": 90.0, "weeklyThreshold": 95.0},
        }))
        self._run(["--once", "--weekly-threshold", "97"], temp_home)
        engine = self.FakeEngine.instances[-1]
        assert engine.settings.weekly_threshold == 97.0  # CLI wins
        assert engine.settings.threshold == 90.0         # settings.json kept

    def test_threshold_flag_keeps_the_configured_weekly_line(self, temp_home):
        """Both lines come from the one settings object the engine holds —
        `--threshold 85` must not drop a configured weekly line (the anchor
        bug yunjeongiya hit on #355 came from two sources of truth)."""
        from claude_swap.paths import get_backup_root

        backup = get_backup_root()
        backup.mkdir(parents=True, exist_ok=True)
        (backup / "settings.json").write_text(json.dumps({
            "schemaVersion": 1,
            "autoswitch": {"threshold": 90.0, "weeklyThreshold": 97.0},
        }))
        self._run(["--once", "--threshold", "85"], temp_home)
        engine = self.FakeEngine.instances[-1]
        assert (engine.settings.threshold, engine.settings.weekly_threshold) == (85.0, 97.0)

    def test_auto_help_mentions_the_weekly_line(self, temp_home, capsys):
        with patch("os.geteuid", return_value=1000, create=True), \
             patch.object(sys, "argv", ["claude-swap", "auto", "--help"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 0
        out = capsys.readouterr().out
        assert "--weekly-threshold" in out
        assert "switch at 90% used" in out  # the epilog's % renders literally

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


class TestUnclaimedCommand:
    """Minor 2: the operator escape hatch for a stash row.

    ``--json`` emits bare entry ids, and the two conditions this branch
    introduces (a stranded row, a permanently unreadable one) both leave a row
    an operator must be able to see the slot/reason of, and drop.
    """

    def _stashed(self, temp_home):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._init_sequence_file()
        entry_id = switcher._store._write_unclaimed_credential(
            "creds-bytes",
            {"reason": "consume-gate-persist-lock-failed",
             "configSlot": "2",
             "consumedFp": "fp-old"},
        )
        return switcher, entry_id

    def test_list_shows_slot_and_reason_not_just_the_id(self, temp_home, capsys):
        _, entry_id = self._stashed(temp_home)
        with patch("os.geteuid", return_value=1000, create=True):
            cli._unclaimed_command([])
        out = capsys.readouterr().out
        assert entry_id in out
        assert "consume-gate-persist-lock-failed" in out
        assert "2" in out

    def test_purge_removes_bytes_and_row(self, temp_home, capsys):
        switcher, entry_id = self._stashed(temp_home)
        with patch("os.geteuid", return_value=1000, create=True):
            cli._unclaimed_command(["--purge", entry_id])
        assert switcher.list_unclaimed_credentials() == {}
        assert not switcher._store._stash_entry_path(entry_id).exists()

    def test_purging_an_unknown_id_fails_loudly(self, temp_home):
        self._stashed(temp_home)
        with patch("os.geteuid", return_value=1000, create=True), \
             pytest.raises(SystemExit) as exc:
            cli._unclaimed_command(["--purge", "no-such-entry"])
        assert exc.value.code == 1

    def test_dispatched_from_main(self, temp_home):
        with patch("claude_swap.cli._unclaimed_command") as fn, \
             patch.object(sys, "argv", ["claude-swap", "unclaimed", "--purge", "x"]):
            cli.main()
        fn.assert_called_once_with(["--purge", "x"])


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
             patch.object(ClaudeAccountSwitcher, "_read_active_credentials",
                          return_value=ActiveCredentials(fake_creds, False)), \
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

            def run(
                self,
                identifier,
                claude_args,
                share=True,
                share_history=None,
                require_session=False,
            ):
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
        assert ("run", "2", [], True, None) in calls

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
        assert ("run", "2", [], True, None) in calls

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
        assert ("run", "3", [], True, None) in calls

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
        assert ("run", "2", ["--resume"], True, None) in calls

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


class TestEmptyValueArguments:
    """A required value that arrives empty is a usage error, not a no-op.

    ``cswap export "$DEST"`` with an unset DEST used to match no branch of the
    dispatch chain and return from main() normally: exit 0, nothing printed,
    no file written, and no way for the calling script to notice.
    """

    def _run(self, argv, capsys):
        with patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["claude-swap", *argv]), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch("claude_swap.update_check.check_for_update", return_value=None):
            code = 0
            try:
                cli.main()
            except SystemExit as exc:
                code = exc.code or 0
        return code, capsys.readouterr(), switcher_cls

    @pytest.mark.parametrize(
        "argv",
        [
            ["remove", ""],
            ["disable", ""],
            ["enable", ""],
            ["switch", ""],
            ["export", ""],
            ["import", ""],
            ["import-usage", ""],
            ["--remove-account", ""],
            ["--disable-account", ""],
            ["--enable-account", ""],
            ["--switch-to", ""],
            ["--export", ""],
            ["--import", ""],
            ["--import-usage", ""],
        ],
    )
    def test_empty_value_is_rejected(self, argv, capsys):
        code, captured, switcher_cls = self._run(argv, capsys)
        assert code == 2, f"{argv} exited {code} having printed {captured.out!r}"
        assert "requires a non-empty" in captured.err
        switcher_cls.assert_not_called()

    def test_empty_value_is_blamed_before_its_modifiers(self, capsys):
        """The empty PATH is the error, not the --account that accompanied it."""
        _, captured, _ = self._run(["export", "", "--account", "1"], capsys)
        assert "'export' requires a non-empty PATH" in captured.err

    def test_add_token_still_accepts_an_empty_value(self, capsys):
        """``--add-token`` uses const="" to mean "prompt me", so it is exempt."""
        code, captured, switcher_cls = self._run(["--add-token"], capsys)
        assert code == 0, captured.err
        switcher_cls.return_value.add_account_from_token.assert_called_once_with(
            token="", email=None, slot=None
        )

    def test_a_whitespace_only_path_is_still_a_path(self, capsys):
        """A filename of spaces is legal on POSIX; only an empty value is rejected."""
        with patch("claude_swap.transfer.export_accounts") as export_accounts:
            code, captured, _ = self._run(["export", " "], capsys)
        assert code == 0, captured.err
        assert export_accounts.call_args.args[1] == " "


def test_importing_the_module_allocates_no_temp_dir(tmp_path, tmp_path_factory):
    """Import must allocate nothing; the fixture must allocate inside basetemp.

    The child gets a private TMPDIR of its own rather than watching the shared
    system one, which several checkouts write to concurrently. Both halves are
    needed: re-adding the module-level ``mkdtemp`` is caught only by the empty
    private tmp, and a fixture allocating outside basetemp only by containment.
    """
    child_tmp = tmp_path / "childtmp"
    child_tmp.mkdir()
    child = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, sys.argv[1]); import tests.test_cli",
         str(Path(__file__).resolve().parent.parent)],
        capture_output=True, text=True,
        env=_subprocess_env(TMPDIR=str(child_tmp)), timeout=120,
    )
    assert child.returncode == 0, child.stderr
    allocated = list(child_tmp.iterdir())
    assert not allocated, f"import allocated {[a.name for a in allocated]}"

    home = Path(_subprocess_env()["HOME"])
    assert home.is_dir(), f"the isolated HOME is not a real directory: {home}"
    assert home.is_relative_to(tmp_path_factory.getbasetemp()), f"{home} escapes basetemp"


class TestImportUsageCli:
    def _dispatch(self, argv):
        with patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch("claude_swap.transfer.import_usage") as import_fn, \
             patch.object(sys, "argv", argv), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch("claude_swap.update_check.check_for_update", return_value=None):
            cli.main()
        return switcher_cls, import_fn

    def test_subcommand_dispatches_with_its_hold(self):
        switcher_cls, import_fn = self._dispatch(
            ["cswap", "import-usage", "-", "--hold", "600"]
        )
        import_fn.assert_called_once_with(
            switcher_cls.return_value, "-", hold_s=600.0
        )

    def test_no_hold_leaves_holds_alone(self):
        switcher_cls, import_fn = self._dispatch(
            ["cswap", "import-usage", "/tmp/usage.json"]
        )
        import_fn.assert_called_once_with(
            switcher_cls.return_value, "/tmp/usage.json", hold_s=None
        )

    def test_zero_hold_reaches_the_import_to_lift_holds(self):
        switcher_cls, import_fn = self._dispatch(
            ["cswap", "import-usage", "-", "--hold", "0"]
        )
        import_fn.assert_called_once_with(
            switcher_cls.return_value, "-", hold_s=0.0
        )

    @pytest.mark.parametrize("argv,message", [
        (["cswap", "list", "--hold", "60"], "--hold can only be used with 'import-usage'"),
        (["cswap", "import-usage", "-", "--hold", "-1"], "--hold must be a non-negative"),
        (["cswap", "import-usage", "-", "--hold", "inf"], "--hold must be a non-negative"),
    ])
    def test_hold_is_validated(self, argv, message, capsys):
        with patch.object(sys, "argv", argv):
            with pytest.raises(SystemExit) as exc:
                cli.main()
        assert exc.value.code == 2
        assert message in capsys.readouterr().err
