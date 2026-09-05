"""Tests for `cswap icon` (per-account display icon, backlog item 3)."""

from pathlib import Path

import pytest

from claude_swap.exceptions import AccountNotFoundError, ValidationError
from claude_swap.switcher import ClaudeAccountSwitcher


def _switcher(temp_home, sample_sequence_data):
    switcher = ClaudeAccountSwitcher()
    switcher._setup_directories()
    switcher._write_json(switcher.sequence_file, sample_sequence_data)
    return switcher


class TestIcon:
    def test_set_and_unset(self, temp_home: Path, sample_sequence_data):
        switcher = _switcher(temp_home, sample_sequence_data)
        num, icon = switcher.set_icon("2", "🐉")
        assert (num, icon) == ("2", "🐉")
        assert switcher._get_sequence_data()["accounts"]["2"]["icon"] == "🐉"
        assert switcher.unset_icon("2") == "2"
        assert "icon" not in switcher._get_sequence_data()["accounts"]["2"]

    def test_unset_is_idempotent(self, temp_home: Path, sample_sequence_data):
        switcher = _switcher(temp_home, sample_sequence_data)
        assert switcher.unset_icon("1") == "1"  # never set — still succeeds

    def test_rejects_non_icons(self, temp_home: Path, sample_sequence_data):
        switcher = _switcher(temp_home, sample_sequence_data)
        for bad in ("", "ab", "dragon", "🐉 🔥", "x" * 20):
            with pytest.raises(ValidationError):
                switcher.set_icon("1", bad)

    def test_multi_codepoint_emoji_is_one_icon(
        self, temp_home: Path, sample_sequence_data
    ):
        switcher = _switcher(temp_home, sample_sequence_data)
        # ZWJ sequence (several code points, one glyph) must pass.
        switcher.set_icon("1", "👩‍💻")

    def test_unknown_account(self, temp_home: Path, sample_sequence_data):
        switcher = _switcher(temp_home, sample_sequence_data)
        with pytest.raises(AccountNotFoundError):
            switcher.set_icon("9", "🐉")

    def test_icon_rides_list_json(self, temp_home: Path, sample_sequence_data):
        from claude_swap.json_output import account_row

        row = account_row(2, "a@x.io", "", "", False, None, icon="🐉")
        assert row["icon"] == "🐉"
        row = account_row(2, "a@x.io", "", "", False, None)
        assert "icon" not in row
