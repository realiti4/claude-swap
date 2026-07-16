"""Tests for `cswap move` (ClaudeAccountSwitcher.move_account)."""

from pathlib import Path

import pytest

from claude_swap.exceptions import (
    AccountNotFoundError,
    ValidationError,
)
from claude_swap.switcher import ClaudeAccountSwitcher


class TestMoveAccount:
    """Test ClaudeAccountSwitcher.move_account()."""

    def _write(self, switcher, data):
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, data)

    # -- relocation to an empty slot (what swap cannot do) ----------------

    def test_move_to_empty_slot_relocates(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)

        num_src, num_target, swapped = switcher.move_account("2", "5")

        assert (num_src, num_target, swapped) == ("2", "5", False)
        data = switcher._get_sequence_data()
        # Account 2 now lives in slot 5; its old slot is freed.
        assert data["accounts"]["5"]["email"] == "account2@example.com"
        assert "2" not in data["accounts"]
        assert data["accounts"]["1"]["email"] == "account1@example.com"

    def test_move_to_empty_slot_updates_rotation_sequence(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)

        switcher.move_account("2", "5")

        data = switcher._get_sequence_data()
        assert data["sequence"] == [1, 5]

    def test_move_active_account_to_empty_slot_follows_active(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)
        assert sample_sequence_data["activeAccountNumber"] == 1

        switcher.move_account("1", "9")

        data = switcher._get_sequence_data()
        assert data["activeAccountNumber"] == 9
        assert data["accounts"]["9"]["email"] == "account1@example.com"

    def test_move_by_email_and_alias(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        switcher = ClaudeAccountSwitcher()
        sample_sequence_data["accounts"]["2"]["alias"] = "dev"
        self._write(switcher, sample_sequence_data)

        num_src, num_target, swapped = switcher.move_account("dev", "7")

        assert (num_src, num_target, swapped) == ("2", "7", False)
        data = switcher._get_sequence_data()
        # The alias travels with its account into the new slot.
        assert data["accounts"]["7"].get("alias") == "dev"
        assert "2" not in data["accounts"]

    def test_move_relocates_credential_and_config_backups(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)
        switcher._write_account_credentials("2", "account2@example.com", "creds-two")
        switcher._write_account_config("2", "account2@example.com", "config-two")

        switcher.move_account("2", "5")

        assert (
            switcher._read_account_credentials("5", "account2@example.com")
            == "creds-two"
        )
        assert (
            switcher._read_account_config("5", "account2@example.com") == "config-two"
        )
        # Old slot key is gone.
        assert switcher._read_account_credentials("2", "account2@example.com") == ""
        assert switcher._read_account_config("2", "account2@example.com") == ""

    def test_move_relocates_session_profile(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)
        session = switcher._session_dir("2", "account2@example.com")
        session.mkdir(parents=True)
        (session / "marker.txt").write_text("history-of-account-two")

        switcher.move_account("2", "5")

        moved = switcher._session_dir("5", "account2@example.com")
        assert (moved / "marker.txt").read_text() == "history-of-account-two"
        assert not session.exists()

    def test_move_to_empty_slot_with_missing_backups(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """A never-backed-up slot relocates cleanly and stays credential-less."""
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)

        switcher.move_account("2", "5")

        data = switcher._get_sequence_data()
        assert data["accounts"]["5"]["email"] == "account2@example.com"
        assert switcher._read_account_credentials("5", "account2@example.com") == ""

    # -- occupied target behaves exactly like swap -----------------------

    def test_move_to_occupied_slot_swaps(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)

        num_src, num_target, swapped = switcher.move_account("1", "2")

        assert (num_src, num_target, swapped) == ("1", "2", True)
        data = switcher._get_sequence_data()
        assert data["accounts"]["2"]["email"] == "account1@example.com"
        assert data["accounts"]["1"]["email"] == "account2@example.com"

    def test_move_is_general_form_of_swap(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """`move a <b's slot>` lands the same state as `swap a b`."""
        move_switcher = ClaudeAccountSwitcher()
        self._write(move_switcher, sample_sequence_data)
        move_switcher.move_account("1", "2")
        moved = move_switcher._get_sequence_data()["accounts"]

        swap_switcher = ClaudeAccountSwitcher()
        self._write(swap_switcher, sample_sequence_data)
        swap_switcher.swap_accounts("1", "2")
        swapped = swap_switcher._get_sequence_data()["accounts"]

        assert moved["1"]["email"] == swapped["1"]["email"]
        assert moved["2"]["email"] == swapped["2"]["email"]

    # -- no-op and validation --------------------------------------------

    def test_move_to_same_slot_is_noop(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)

        num_src, num_target, swapped = switcher.move_account("1", "1")

        assert (num_src, num_target, swapped) == ("1", "1", False)
        data = switcher._get_sequence_data()
        assert data["accounts"]["1"]["email"] == "account1@example.com"
        assert data["accounts"]["2"]["email"] == "account2@example.com"

    def test_move_normalizes_padded_target(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)

        num_src, num_target, swapped = switcher.move_account("2", "05")

        assert num_target == "5"
        data = switcher._get_sequence_data()
        assert data["accounts"]["5"]["email"] == "account2@example.com"

    @pytest.mark.parametrize("bad", ["abc", "0", "-1", "1.5", ""])
    def test_move_invalid_target_rejected(
        self, temp_home: Path, sample_sequence_data: dict, bad: str
    ):
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)

        with pytest.raises(ValidationError):
            switcher.move_account("1", bad)

    def test_move_unknown_account_rejected(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)

        with pytest.raises(AccountNotFoundError):
            switcher.move_account("nosuch@example.com", "5")
