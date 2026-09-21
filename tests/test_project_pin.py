"""Tests for project-local account pins (.claude-account / .env) and their
wiring into `cswap run`."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_swap import cli
from claude_swap.models import Platform
from claude_swap.project_pin import (
    AccountPin,
    find_account_pin,
    _read_dotenv_account,
    _read_pin_file,
)
from claude_swap.switcher import ClaudeAccountSwitcher


class TestFindAccountPin:
    def test_reads_claude_account_file(self, tmp_path):
        (tmp_path / ".claude-account").write_text("work@co.com\n")
        pin = find_account_pin(tmp_path)
        assert pin is not None
        assert pin.identifier == "work@co.com"
        assert pin.mechanism == "file"
        assert pin.source == tmp_path / ".claude-account"

    def test_walks_up_to_ancestor(self, tmp_path):
        (tmp_path / ".claude-account").write_text("2\n")
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        pin = find_account_pin(deep)
        assert pin is not None and pin.identifier == "2"

    def test_nearest_directory_wins(self, tmp_path):
        (tmp_path / ".claude-account").write_text("root@co.com\n")
        child = tmp_path / "child"
        child.mkdir()
        (child / ".claude-account").write_text("child@co.com\n")
        pin = find_account_pin(child)
        assert pin is not None and pin.identifier == "child@co.com"

    def test_claude_account_wins_over_dotenv_same_dir(self, tmp_path):
        (tmp_path / ".claude-account").write_text("dedicated@co.com\n")
        (tmp_path / ".env").write_text("CLAUDE_SWAP_ACCOUNT=dotenv@co.com\n")
        pin = find_account_pin(tmp_path)
        assert pin is not None
        assert pin.identifier == "dedicated@co.com"
        assert pin.mechanism == "file"

    def test_reads_dotenv_when_no_pin_file(self, tmp_path):
        (tmp_path / ".env").write_text(
            "OTHER=1\nexport CLAUDE_SWAP_ACCOUNT='env@co.com'  # inline\n"
        )
        pin = find_account_pin(tmp_path)
        assert pin is not None
        assert pin.identifier == "env@co.com"
        assert pin.mechanism == "dotenv"

    def test_dotenv_last_assignment_wins(self, tmp_path):
        (tmp_path / ".env").write_text(
            "CLAUDE_SWAP_ACCOUNT=first@co.com\nCLAUDE_SWAP_ACCOUNT=second@co.com\n"
        )
        pin = find_account_pin(tmp_path)
        assert pin is not None and pin.identifier == "second@co.com"

    def test_dotenv_without_key_is_not_a_pin(self, tmp_path):
        (tmp_path / ".env").write_text("DATABASE_URL=postgres://x\n")
        assert find_account_pin(tmp_path) is None

    def test_no_pin_returns_none(self, tmp_path):
        deep = tmp_path / "x" / "y"
        deep.mkdir(parents=True)
        assert find_account_pin(deep) is None

    def test_comment_and_blank_lines_skipped(self, tmp_path):
        (tmp_path / ".claude-account").write_text(
            "# which account this repo uses\n\n  prod  # trailing\n"
        )
        assert _read_pin_file(tmp_path / ".claude-account") == "prod"

    def test_dotenv_double_quotes_and_comment(self, tmp_path):
        (tmp_path / ".env").write_text('CLAUDE_SWAP_ACCOUNT="1"  # main\n')
        assert _read_dotenv_account(tmp_path / ".env") == "1"

    def test_display_source(self, tmp_path):
        file_pin = AccountPin("2", tmp_path / ".claude-account", "file")
        assert file_pin.display_source() == str(tmp_path / ".claude-account")
        env_pin = AccountPin("2", tmp_path / ".env", "dotenv")
        assert env_pin.display_source() == f"CLAUDE_SWAP_ACCOUNT in {tmp_path / '.env'}"


class TestSlotForIdentifier:
    def _seed(self, s: ClaudeAccountSwitcher, num: int, email: str) -> None:
        s._write_account_credentials(
            str(num), email,
            json.dumps({"claudeAiOauth": {
                "accessToken": f"sk-{num}", "refreshToken": f"rt-{num}"}}),
        )
        s._write_account_config(
            str(num), email,
            json.dumps({"oauthAccount": {
                "emailAddress": email, "accountUuid": f"uuid-{num}"}}),
        )
        data = s._get_sequence_data() or {
            "activeAccountNumber": None, "lastUpdated": "",
            "sequence": [], "accounts": {},
        }
        data["accounts"][str(num)] = {
            "email": email, "uuid": f"uuid-{num}",
            "organizationUuid": "", "organizationName": "",
            "added": "2024-01-01T00:00:00Z",
        }
        if num not in data["sequence"]:
            data["sequence"].append(num)
            data["sequence"].sort()
        if data["activeAccountNumber"] is None:
            data["activeAccountNumber"] = num
        s._write_json(s.sequence_file, data)

    def _switcher(self, temp_home: Path) -> ClaudeAccountSwitcher:
        s = ClaudeAccountSwitcher()
        s.platform = Platform.LINUX
        s._setup_directories()
        s._init_sequence_file()
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        return s

    def test_by_number(self, temp_home):
        s = self._switcher(temp_home)
        assert s.slot_for_identifier("2") == ("2", "b@example.com")

    def test_by_email(self, temp_home):
        s = self._switcher(temp_home)
        assert s.slot_for_identifier("a@example.com") == ("1", "a@example.com")

    def test_by_alias(self, temp_home):
        s = self._switcher(temp_home)
        s.set_alias("2", "dev")
        assert s.slot_for_identifier("dev") == ("2", "b@example.com")

    def test_unknown_is_soft_miss(self, temp_home):
        s = self._switcher(temp_home)
        assert s.slot_for_identifier("nobody@example.com") == (None, None)

    def test_empty_is_soft_miss(self, temp_home):
        s = self._switcher(temp_home)
        assert s.slot_for_identifier("   ") == (None, None)

    def test_nonexistent_slot_number_is_soft_miss(self, temp_home):
        s = self._switcher(temp_home)
        assert s.slot_for_identifier("9") == (None, None)


class TestRunUsesProjectPin:
    """`cswap run` (no account) launches the project-pinned account, ahead of
    the global mapping, and falls back cleanly when the pin is unresolvable."""

    def _fake_manager(self, calls):
        class FakeSessionManager:
            def __init__(self, switcher):
                pass

            def run(self, identifier, claude_args, share=True, share_history=False):
                calls.append(("run", identifier, claude_args, share, share_history))

            def exec_default(self, claude_args):
                calls.append(("exec_default", claude_args))

        return FakeSessionManager

    def _switcher(self, *, pin_result, dir_result=(None, None)):
        sw = MagicMock()
        sw.slot_for_identifier.return_value = pin_result
        sw.slot_for_directory.return_value = dir_result
        return sw

    def _run(self, calls, sw, cwd, monkeypatch):
        monkeypatch.chdir(cwd)
        with patch("claude_swap.session.SessionManager", self._fake_manager(calls)), \
             patch("claude_swap.cli.ClaudeAccountSwitcher", return_value=sw), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch.object(sys, "argv", ["claude-swap", "run"]):
            cli.main()

    def test_pin_file_launches_pinned_account(self, tmp_path, monkeypatch, capsys):
        (tmp_path / ".claude-account").write_text("work@co.com\n")
        calls = []
        sw = self._switcher(pin_result=("2", "work@co.com"))
        self._run(calls, sw, tmp_path, monkeypatch)
        assert ("run", "2", [], True, False) in calls
        assert "pinned by" in capsys.readouterr().out

    def test_pin_wins_over_mapping(self, tmp_path, monkeypatch):
        (tmp_path / ".claude-account").write_text("pinned@co.com\n")
        calls = []
        # A directory mapping exists (slot 5) but the pin (slot 2) takes it.
        sw = self._switcher(pin_result=("2", "pinned@co.com"), dir_result=("5", "m@co.com"))
        self._run(calls, sw, tmp_path, monkeypatch)
        assert ("run", "2", [], True, False) in calls
        sw.slot_for_directory.assert_not_called()

    def test_unresolvable_pin_warns_and_falls_back(self, tmp_path, monkeypatch, capsys):
        (tmp_path / ".claude-account").write_text("ghost@co.com\n")
        calls = []
        # Pin doesn't resolve; no mapping either -> default.
        sw = self._switcher(pin_result=(None, None), dir_result=(None, None))
        self._run(calls, sw, tmp_path, monkeypatch)
        assert ("exec_default", []) in calls
        out = capsys.readouterr().out
        assert "ghost@co.com" in out and "falling back" in out

    def test_no_pin_uses_mapping(self, tmp_path, monkeypatch):
        # No pin files present -> slot_for_identifier never consulted.
        calls = []
        sw = self._switcher(pin_result=("9", "x"), dir_result=("3", "map@co.com"))
        self._run(calls, sw, tmp_path, monkeypatch)
        assert ("run", "3", [], True, False) in calls
        sw.slot_for_identifier.assert_not_called()
