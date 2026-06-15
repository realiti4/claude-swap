"""Tests for the auto-switch (Beta) feature.

Covers the switcher-side config/usage helpers and the TUI monitor logic.
Curses primitives are mocked exactly as in ``test_tui.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_swap import tui
from claude_swap.exceptions import ValidationError
from claude_swap.switcher import (
    DEFAULT_AUTO_SWITCH_THRESHOLD,
    DEFAULT_QUICK_START_COMMAND,
    ClaudeAccountSwitcher,
    _max_usage_pct,
)


def _stub_screen(rows: int = 30, cols: int = 100) -> MagicMock:
    screen = MagicMock()
    screen.getmaxyx.return_value = (rows, cols)
    return screen


def _login(temp_home: Path, email: str = "u@example.com") -> None:
    config = {"oauthAccount": {"emailAddress": email}}
    (temp_home / ".claude.json").write_text(json.dumps(config))


# --------------------------------------------------------------------------- #
# _max_usage_pct                                                               #
# --------------------------------------------------------------------------- #


class TestMaxUsagePct:
    def test_none_when_no_usage(self):
        assert _max_usage_pct(None) is None
        assert _max_usage_pct({}) is None
        assert _max_usage_pct("no credentials") is None

    def test_returns_highest_of_5h_7d(self):
        usage = {"five_hour": {"pct": 40}, "seven_day": {"pct": 95}}
        assert _max_usage_pct(usage) == 95.0

    def test_ignores_spend_entry(self):
        usage = {"five_hour": {"pct": 10}, "spend": {"pct": 99}}
        assert _max_usage_pct(usage) == 10.0

    def test_handles_missing_pct(self):
        assert _max_usage_pct({"five_hour": {}}) is None


# --------------------------------------------------------------------------- #
# Config persistence                                                          #
# --------------------------------------------------------------------------- #


class TestAutoSwitchConfig:
    def test_default_is_disabled(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        cfg = switcher.get_auto_switch_config()
        assert cfg == {
            "enabled": False,
            "threshold": DEFAULT_AUTO_SWITCH_THRESHOLD,
        }

    def test_enable_and_persist(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        switcher.set_auto_switch_config(enabled=True)
        # A fresh instance reads the persisted value.
        assert ClaudeAccountSwitcher().get_auto_switch_config()["enabled"] is True

    def test_set_threshold(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        cfg = switcher.set_auto_switch_config(threshold=80)
        assert cfg["threshold"] == 80
        assert switcher.get_auto_switch_config()["threshold"] == 80

    def test_partial_update_keeps_other_field(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        switcher.set_auto_switch_config(enabled=True, threshold=70)
        switcher.set_auto_switch_config(threshold=60)
        cfg = switcher.get_auto_switch_config()
        assert cfg == {"enabled": True, "threshold": 60}

    @pytest.mark.parametrize("bad", [0, -5, 101, 999])
    def test_invalid_threshold_rejected(self, temp_home: Path, bad: int):
        switcher = ClaudeAccountSwitcher()
        with pytest.raises(ValidationError):
            switcher.set_auto_switch_config(threshold=bad)

    def test_does_not_clobber_accounts(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._init_sequence_file()
        data = switcher._get_sequence_data()
        data["accounts"]["1"] = {"email": "a@x.com"}
        switcher._write_json(switcher.sequence_file, data)
        switcher.set_auto_switch_config(enabled=True)
        assert "1" in switcher._get_sequence_data()["accounts"]


# --------------------------------------------------------------------------- #
# get_active_usage_pct                                                        #
# --------------------------------------------------------------------------- #


class TestActiveUsagePct:
    def test_none_without_login(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        assert switcher.get_active_usage_pct() is None

    def test_none_without_credentials(self, temp_home: Path):
        _login(temp_home)
        switcher = ClaudeAccountSwitcher()
        with patch.object(switcher, "_read_credentials", return_value=""):
            assert switcher.get_active_usage_pct() is None

    def test_returns_pct_from_usage_api(self, temp_home: Path):
        _login(temp_home)
        switcher = ClaudeAccountSwitcher()
        creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})
        usage = {"five_hour": {"pct": 96}, "seven_day": {"pct": 20}}
        with patch.object(switcher, "_read_credentials", return_value=creds), \
             patch(
                 "claude_swap.oauth.fetch_usage_for_account",
                 return_value=usage,
             ):
            assert switcher.get_active_usage_pct() == 96.0

    def test_none_when_api_unavailable(self, temp_home: Path):
        _login(temp_home)
        switcher = ClaudeAccountSwitcher()
        creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})
        with patch.object(switcher, "_read_credentials", return_value=creds), \
             patch(
                 "claude_swap.oauth.fetch_usage_for_account",
                 return_value=None,
             ):
            assert switcher.get_active_usage_pct() is None


# --------------------------------------------------------------------------- #
# Load balancer TUI sub-flows                                                 #
# --------------------------------------------------------------------------- #


class TestDoBalancer:
    def test_toggle_enables(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        screen = _stub_screen()
        # Enter on "Enable" (idx 0), then Esc to leave the settings screen.
        screen.getch.side_effect = [10, 27]
        tui._do_balancer(screen, switcher)
        assert switcher.get_auto_balance_config()["enabled"] is True

    def test_back_does_nothing(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        screen = _stub_screen()
        screen.getch.side_effect = [27]  # Esc immediately
        tui._do_balancer(screen, switcher)
        assert switcher.get_auto_balance_config()["enabled"] is False

    def test_set_threshold_via_prompt(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        screen = _stub_screen()
        # Down to "Set threshold" (idx 1) + Enter, type "80" + Enter, then Esc.
        keys = [tui.curses.KEY_DOWN, 10]
        keys += [ord("8"), ord("0"), 10]
        keys += [27]
        screen.getch.side_effect = keys
        with patch("claude_swap.tui.curses.curs_set"):
            tui._do_balancer(screen, switcher)
        assert switcher.get_auto_balance_config()["threshold"] == 80

    def test_toggle_prime_idle_windows(self, temp_home: Path):
        # The "Prime idle 5h windows" item is at idx 3 (after Enable/threshold/
        # target). Default OFF; one toggle turns it ON.
        switcher = ClaudeAccountSwitcher()
        assert switcher.get_auto_balance_config()["primeIdleWindows"] is False
        screen = _stub_screen()
        keys = [tui.curses.KEY_DOWN] * 3 + [10, 27]
        screen.getch.side_effect = keys
        with patch("claude_swap.tui.curses.curs_set"):
            tui._do_balancer(screen, switcher)
        assert switcher.get_auto_balance_config()["primeIdleWindows"] is True


class TestPrimeIdleWindowsConfig:
    """The primeIdleWindows opt-in is default-OFF, round-trips, and is independent
    of `enabled` (review fix #3)."""

    def test_defaults_off(self, temp_home: Path):
        sw = ClaudeAccountSwitcher()
        assert sw.get_auto_balance_config()["primeIdleWindows"] is False

    def test_set_and_round_trip(self, temp_home: Path):
        sw = ClaudeAccountSwitcher()
        sw.set_auto_balance_config(prime_idle_windows=True)
        assert sw.get_auto_balance_config()["primeIdleWindows"] is True
        sw.set_auto_balance_config(prime_idle_windows=False)
        assert sw.get_auto_balance_config()["primeIdleWindows"] is False

    def test_independent_of_enabled(self, temp_home: Path):
        # Enabling the balancer must NOT turn priming on.
        sw = ClaudeAccountSwitcher()
        sw.set_auto_balance_config(enabled=True)
        cfg = sw.get_auto_balance_config()
        assert cfg["enabled"] is True
        assert cfg["primeIdleWindows"] is False


class TestEditPriorities:
    def _seed_account(self, switcher: ClaudeAccountSwitcher) -> None:
        switcher._setup_directories()
        switcher._init_sequence_file()
        data = switcher._get_sequence_data()
        data["accounts"]["1"] = {"email": "a@x.com"}
        data["sequence"] = [1]
        switcher._write_json(switcher.sequence_file, data)

    def test_set_priority_via_prompt(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        self._seed_account(switcher)
        screen = _stub_screen()
        # Select account 1 (idx 0, Enter), type "7" + Enter, then Back.
        screen.getch.side_effect = [10, ord("7"), 10, tui.curses.KEY_DOWN, 10]
        with patch("claude_swap.tui.curses.curs_set"):
            tui._edit_priorities(screen, switcher)
        assert switcher.get_account_priority("1") == 7


class TestQuickStartConfig:
    """The Quick-start alias config is default-OFF, round-trips, and self-heals."""

    def test_defaults_off_with_default_command(self, temp_home: Path):
        cfg = ClaudeAccountSwitcher().get_quick_start_config()
        assert cfg["enabled"] is False
        assert cfg["command"] == DEFAULT_QUICK_START_COMMAND

    def test_default_command_is_the_documented_one(self):
        assert DEFAULT_QUICK_START_COMMAND == (
            "cswap launch -- --dangerously-skip-permissions "
            "--model claude-opus-4-8 "
            "--settings '{\"ultracode\": true}'"
        )

    def test_enable_and_persist(self, temp_home: Path):
        ClaudeAccountSwitcher().set_quick_start_config(enabled=True)
        assert ClaudeAccountSwitcher().get_quick_start_config()["enabled"] is True

    def test_set_custom_command_round_trips(self, temp_home: Path):
        sw = ClaudeAccountSwitcher()
        sw.set_quick_start_config(command="cswap launch -- --model opus")
        assert sw.get_quick_start_config()["command"] == "cswap launch -- --model opus"

    def test_partial_update_keeps_other_field(self, temp_home: Path):
        sw = ClaudeAccountSwitcher()
        sw.set_quick_start_config(enabled=True, command="cswap launch")
        sw.set_quick_start_config(enabled=False)
        cfg = sw.get_quick_start_config()
        assert cfg == {"enabled": False, "command": "cswap launch"}

    def test_blank_command_rejected(self, temp_home: Path):
        sw = ClaudeAccountSwitcher()
        with pytest.raises(ValidationError):
            sw.set_quick_start_config(command="   ")

    def test_does_not_clobber_accounts(self, temp_home: Path):
        sw = ClaudeAccountSwitcher()
        sw._setup_directories()
        sw._init_sequence_file()
        data = sw._get_sequence_data()
        data["accounts"]["1"] = {"email": "a@x.com"}
        sw._write_json(sw.sequence_file, data)
        sw.set_quick_start_config(enabled=True)
        assert "1" in sw._get_sequence_data()["accounts"]


class TestDoQuickStart:
    def test_toggle_enables(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        screen = _stub_screen()
        # Enter on "Enable" (idx 0), then Esc to leave.
        screen.getch.side_effect = [10, 27]
        tui._do_quick_start(screen, switcher)
        assert switcher.get_quick_start_config()["enabled"] is True

    def test_reset_restores_default_command(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        switcher.set_quick_start_config(command="cswap launch")
        screen = _stub_screen()
        # Down to "Reset command to default" (idx 2) + Enter, then Esc.
        screen.getch.side_effect = [tui.curses.KEY_DOWN, tui.curses.KEY_DOWN, 10, 27]
        tui._do_quick_start(screen, switcher)
        assert switcher.get_quick_start_config()["command"] == DEFAULT_QUICK_START_COMMAND
