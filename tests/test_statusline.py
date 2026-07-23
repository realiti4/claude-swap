"""Tests for `cswap statusline` and the active_account_display accessor."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_swap import cli
from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher


class TestActiveAccountDisplay:
    def _switcher(self, temp_home: Path) -> ClaudeAccountSwitcher:
        s = ClaudeAccountSwitcher()
        s.platform = Platform.LINUX
        s._setup_directories()
        s._init_sequence_file()
        return s

    def _seed(self, s, num, email, alias=None):
        data = s._get_sequence_data() or {
            "activeAccountNumber": None, "lastUpdated": "",
            "sequence": [], "accounts": {},
        }
        rec = {
            "email": email, "uuid": f"uuid-{num}",
            "organizationUuid": "", "organizationName": "",
            "added": "2024-01-01T00:00:00Z",
        }
        if alias:
            rec["alias"] = alias
        data["accounts"][str(num)] = rec
        if num not in data["sequence"]:
            data["sequence"].append(num)
        s._write_json(s.sequence_file, data)

    def test_managed_returns_email_and_alias(self, temp_home, monkeypatch):
        s = self._switcher(temp_home)
        self._seed(s, 1, "a@example.com", "prod")
        monkeypatch.setattr(s, "current_account_number", lambda: "1")
        assert s.active_account_display() == ("a@example.com", "prod")

    def test_managed_without_alias(self, temp_home, monkeypatch):
        s = self._switcher(temp_home)
        self._seed(s, 1, "a@example.com")
        monkeypatch.setattr(s, "current_account_number", lambda: "1")
        assert s.active_account_display() == ("a@example.com", None)

    def test_unmanaged_live_login(self, temp_home, monkeypatch):
        s = self._switcher(temp_home)
        monkeypatch.setattr(s, "current_account_number", lambda: None)
        monkeypatch.setattr(s, "_get_current_account", lambda: ("live@x.com", ""))
        assert s.active_account_display() == ("live@x.com", None)

    def test_no_login(self, temp_home, monkeypatch):
        s = self._switcher(temp_home)
        monkeypatch.setattr(s, "current_account_number", lambda: None)
        monkeypatch.setattr(s, "_get_current_account", lambda: None)
        assert s.active_account_display() == (None, None)

    def test_error_degrades_to_none(self, temp_home, monkeypatch):
        s = self._switcher(temp_home)

        def boom():
            raise RuntimeError("transient read failure")

        monkeypatch.setattr(s, "current_account_number", boom)
        assert s.active_account_display() == (None, None)


class TestStatuslineCommand:
    def _run(self, argv, display, capsys):
        sw = MagicMock()
        if isinstance(display, Exception):
            sw.active_account_display.side_effect = display
        else:
            sw.active_account_display.return_value = display
        with patch("claude_swap.cli.ClaudeAccountSwitcher", return_value=sw), \
             patch.object(sys, "argv", ["cswap", *argv]):
            cli.main()
        return capsys.readouterr().out

    def test_prints_alias_with_icon(self, capsys):
        out = self._run(["statusline"], ("a@example.com", "prod"), capsys)
        assert out.strip() == "⇄ prod"

    def test_local_part_when_no_alias(self, capsys):
        out = self._run(["statusline"], ("work@co.com", None), capsys)
        assert out.strip() == "⇄ work"

    def test_email_flag_shows_full_email(self, capsys):
        out = self._run(["statusline", "--email"], ("work@co.com", "prod"), capsys)
        assert out.strip() == "⇄ work@co.com"

    def test_no_icon(self, capsys):
        out = self._run(["statusline", "--no-icon"], ("work@co.com", "prod"), capsys)
        assert out.strip() == "prod"

    def test_custom_icon(self, capsys):
        out = self._run(["statusline", "--icon", "★"], ("work@co.com", None), capsys)
        assert out.strip() == "★ work"

    def test_no_login_prints_nothing(self, capsys):
        out = self._run(["statusline"], (None, None), capsys)
        assert out.strip() == ""

    def test_switcher_error_prints_nothing(self, capsys):
        out = self._run(["statusline"], RuntimeError("boom"), capsys)
        assert out.strip() == ""
