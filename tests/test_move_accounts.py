"""Tests for `cswap move` (ClaudeAccountSwitcher.move_account)."""

import os
import sys
from pathlib import Path

import pytest

from claude_swap import macos_keychain
from claude_swap.exceptions import (
    AccountNotFoundError,
    CredentialError,
    ValidationError,
)
from claude_swap.models import Platform
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

    def test_move_keeps_sequence_sorted(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """Renumbering slot 1 past slot 2 must not leave sequence unsorted —
        rotation and list order follow the numbers."""
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)

        switcher.move_account("1", "5")

        data = switcher._get_sequence_data()
        assert data["sequence"] == [2, 5]

    def test_move_holds_account_lock(
        self, temp_home: Path, sample_sequence_data: dict, monkeypatch
    ):
        """The relocate path runs under the same lock switch/persist take."""
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)
        entered: list[object] = []

        class SpyLock:
            def __init__(self, path):
                self.path = path

            def __enter__(self):
                entered.append(self.path)
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr("claude_swap.switcher.FileLock", SpyLock)
        switcher.move_account("2", "5")

        assert entered == [switcher.lock_file]

    def test_relocate_rechecks_target(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """_relocate_locked refuses an occupied target as an invariant, even
        though move_account dispatches under the same lock."""
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)

        with pytest.raises(ValidationError, match="already occupied"):
            switcher._relocate_locked("1", "2")

    def test_move_occupied_path_takes_single_lock(
        self, temp_home: Path, sample_sequence_data: dict, monkeypatch
    ):
        """Resolution, dispatch, and the delegated swap all run inside one
        lock acquisition (FileLock is non-reentrant)."""
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)
        entered: list[object] = []

        class SpyLock:
            def __init__(self, path):
                self.path = path

            def __enter__(self):
                entered.append(self.path)
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr("claude_swap.switcher.FileLock", SpyLock)
        num_src, num_target, swapped = switcher.move_account("1", "2")

        assert (num_src, num_target, swapped) == ("1", "2", True)
        assert entered == [switcher.lock_file]

    def test_move_unbacked_account_clears_stale_target_key(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """An account with no stored backup must not adopt stale material
        leaked under its target key by an earlier crash."""
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)
        # Account 2 has no backup; plant a stale foreign file under the key
        # it will occupy after the move: (slot 5, account2's email).
        switcher._write_account_credentials(
            "5", "account2@example.com", "stale-foreign"
        )

        switcher.move_account("2", "5")

        assert switcher._read_account_credentials("5", "account2@example.com") == ""
        data = switcher._get_sequence_data()
        assert data["accounts"]["5"]["email"] == "account2@example.com"

    def test_move_failed_required_clear_aborts_commit(
        self, temp_home: Path, sample_sequence_data: dict, monkeypatch
    ):
        """A required clear of the target key is strict: if the stale material
        cannot actually be removed, the move must abort before committing
        metadata — never commit an account onto a key still serving foreign
        material."""
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)
        switcher._write_account_credentials(
            "5", "account2@example.com", "stale-foreign"
        )

        real_unlink = Path.unlink

        def failing_unlink(path, *args, **kwargs):
            if path.name.startswith(".creds-5-"):
                raise OSError("permission denied (injected)")
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", failing_unlink)
        with pytest.raises(CredentialError, match="aborting before commit"):
            switcher.move_account("2", "5")
        monkeypatch.undo()

        # Metadata was never committed: the account is intact under its
        # original number and the stale key stays unreferenced.
        data = switcher._get_sequence_data()
        assert data["accounts"]["2"]["email"] == "account2@example.com"
        assert "5" not in data["accounts"]

    def test_move_strict_clear_fails_closed_on_unreadable_dir(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """`Path.exists()` returns False on an inaccessible directory,
        conflating "missing" with "couldn't inspect" — the required clear
        must unlink unconditionally and let the permission error abort the
        move, not skip the delete and commit over a hidden stale key."""
        if sys.platform == "win32" or os.geteuid() == 0:
            pytest.skip("needs POSIX permission semantics (non-root)")
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)
        switcher._write_account_credentials(
            "5", "account2@example.com", "stale-foreign"
        )

        switcher.credentials_dir.chmod(0o000)
        try:
            with pytest.raises(CredentialError, match="aborting before commit"):
                switcher.move_account("2", "5")
        finally:
            switcher.credentials_dir.chmod(0o700)

        # Nothing committed; with permissions restored, the stale credential
        # is still present but remains unreferenced.
        data = switcher._get_sequence_data()
        assert data["accounts"]["2"]["email"] == "account2@example.com"
        assert "5" not in data["accounts"]
        assert (
            switcher._read_account_credentials("5", "account2@example.com")
            == "stale-foreign"
        )

    def test_move_strict_clear_fails_closed_on_locked_keychain(
        self,
        temp_home: Path,
        sample_sequence_data: dict,
        block_real_keychain,
        monkeypatch,
    ):
        """macOS with a locked Keychain: deletion raises and the normal
        verification read reports "" (unreadable == absent in the best-effort
        reader). The strict clear must fail closed — abort the move rather
        than commit with a stale Keychain item set to resurface on unlock."""
        from claude_swap.credentials import SECURITY_SERVICE

        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)
        switcher.platform = Platform.MACOS
        # Stale item under the key account 2 will occupy after the move.
        stale_key = (SECURITY_SERVICE, "account-5-account2@example.com")
        block_real_keychain.data[stale_key] = "stale-keychain"

        def locked(*args, **kwargs):
            raise macos_keychain.KeychainError("keychain locked (injected)")

        monkeypatch.setattr(macos_keychain, "get_password", locked)
        monkeypatch.setattr(macos_keychain, "delete_password", locked)

        with pytest.raises(CredentialError, match="aborting before commit"):
            switcher.move_account("2", "5")
        monkeypatch.undo()

        # Nothing committed; the stale item survived but stays unreferenced.
        data = switcher._get_sequence_data()
        assert data["accounts"]["2"]["email"] == "account2@example.com"
        assert "5" not in data["accounts"]
        assert block_real_keychain.data[stale_key] == "stale-keychain"

    def test_move_metadata_failure_leaves_account_intact(
        self, temp_home: Path, sample_sequence_data: dict, monkeypatch
    ):
        """The sequence.json write is the commit point: if it fails, the
        account must remain fully usable under its original number — the old
        keys are only cleared after the commit, and strays under the target
        key are cleaned up."""
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)
        switcher._write_account_credentials("2", "account2@example.com", "creds-two")

        real_write_json = ClaudeAccountSwitcher._write_json

        def failing_write_json(self, path, data):
            if path == self.sequence_file:
                raise OSError("disk full (injected)")
            return real_write_json(self, path, data)

        monkeypatch.setattr(ClaudeAccountSwitcher, "_write_json", failing_write_json)
        with pytest.raises(OSError):
            switcher.move_account("2", "5")
        monkeypatch.undo()

        assert (
            switcher._read_account_credentials("2", "account2@example.com")
            == "creds-two"
        )
        assert switcher._read_account_credentials("5", "account2@example.com") == ""
        data = switcher._get_sequence_data()
        assert data["accounts"]["2"]["email"] == "account2@example.com"
        assert "5" not in data["accounts"]

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

    def test_move_target_above_cap_rejected(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """`add` numbers from the max slot, so a huge target is refused."""
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)

        with pytest.raises(ValidationError, match="out of range"):
            switcher.move_account("1", "100")

    def test_move_cap_stretches_to_existing_max_slot(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """A table that already grew past 99 keeps its full range usable."""
        switcher = ClaudeAccountSwitcher()
        sample_sequence_data["accounts"]["150"] = {
            "email": "account150@example.com",
            "uuid": "uuid-150",
            "added": "2024-01-03T00:00:00Z",
        }
        sample_sequence_data["sequence"].append(150)
        self._write(switcher, sample_sequence_data)

        num_src, num_target, swapped = switcher.move_account("1", "120")

        assert (num_src, num_target, swapped) == ("1", "120", False)
        data = switcher._get_sequence_data()
        assert data["accounts"]["120"]["email"] == "account1@example.com"

        with pytest.raises(ValidationError, match="out of range"):
            switcher.move_account("2", "151")
