"""Tests for `cswap move` (ClaudeAccountSwitcher.move_account)."""

import os
import sys
from pathlib import Path

import pytest

from claude_swap import macos_keychain
from claude_swap.exceptions import (
    AccountNotFoundError,
    ConfigError,
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
        self, temp_home: Path, sample_sequence_data: dict
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

        # Scoped context, not the fixture's shared `monkeypatch`: that
        # instance also carries the autouse colour/keychain/home scrubs, and
        # `.undo()` on it would unwind those too (H-1) — restoring whatever
        # FORCE_COLOR/NO_COLOR the developer's shell actually has exported
        # for the rest of this test.
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(Path, "unlink", failing_unlink)
            with pytest.raises(CredentialError, match="aborting before commit"):
                switcher.move_account("2", "5")

        # Metadata was never committed: the account is intact under its
        # original number and the stale key stays unreferenced.
        data = switcher._get_sequence_data()
        assert data["accounts"]["2"]["email"] == "account2@example.com"
        assert "5" not in data["accounts"]

    def test_move_strict_clear_fails_closed_on_unreadable_dir(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """`Path.exists()` raises on an inaccessible directory. A whole-dir
        permission fault makes every read through it fail, including the
        SOURCE account's own backup read at the top of `_relocate_locked` —
        `_read_backup_or_abort` now catches that first and aborts before any
        deletion is attempted (a C2 fix: the ``.exists()`` OSError arm used
        to swallow into "absent" without marking the read failed, so this
        exact scenario used to fall through and only abort later, by luck,
        when the target's strict clear also hit the same unreadable dir —
        a gap through which the source's own live refresh token could have
        been silently treated as absent and dropped)."""
        if sys.platform == "win32" or os.geteuid() == 0:
            pytest.skip("needs POSIX permission semantics (non-root)")
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)
        switcher._write_account_credentials(
            "5", "account2@example.com", "stale-foreign"
        )

        switcher.credentials_dir.chmod(0o000)
        try:
            with pytest.raises(ConfigError, match="could not be read"):
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
    ):
        """macOS with a locked Keychain: deletion raises and the normal
        verification read reports "" (unreadable == absent in the best-effort
        reader). The strict clear must fail closed — abort the move rather
        than commit with a stale Keychain item set to resurface on unlock.

        With the source-side ``_read_account_credentials_ex`` guard (same
        defect family, see ``TestMoveUnreadableSourceIsNotAbsent``), a
        globally locked Keychain is now caught even earlier — at account 2's
        OWN pre-move backup read, before the destination's strict clear is
        ever reached — and raises ``ConfigError`` instead. The invariant
        this test exists to pin (nothing committed, the stale item
        survives) is unchanged; only which guard catches it first is."""
        from claude_swap.credentials import SECURITY_SERVICE

        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)
        switcher.platform = Platform.MACOS
        # Stale item under the key account 2 will occupy after the move.
        stale_key = (SECURITY_SERVICE, "account-5-account2@example.com")
        block_real_keychain.data[stale_key] = "stale-keychain"

        def locked(*args, **kwargs):
            raise macos_keychain.KeychainError("keychain locked (injected)")

        # Scoped context: see the comment on the sibling test above (H-1) —
        # `monkeypatch.undo()` on the fixture's shared instance would also
        # unwind the autouse colour scrub.
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(macos_keychain, "get_password", locked)
            mp.setattr(macos_keychain, "delete_password", locked)
            with pytest.raises(ConfigError, match="could not be read"):
                switcher.move_account("2", "5")

        # Nothing committed; the stale item survived but stays unreferenced.
        data = switcher._get_sequence_data()
        assert data["accounts"]["2"]["email"] == "account2@example.com"
        assert "5" not in data["accounts"]
        assert block_real_keychain.data[stale_key] == "stale-keychain"

    def test_move_metadata_failure_leaves_account_intact(
        self, temp_home: Path, sample_sequence_data: dict
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

        # Scoped context: see H-1 comment above.
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(ClaudeAccountSwitcher, "_write_json", failing_write_json)
            with pytest.raises(OSError):
                switcher.move_account("2", "5")

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


class TestMoveUnreadableSourceIsNotAbsent:
    """Same defect family as C1/C2: the plain reader's ``""`` means both
    "no backup" and "the backup exists but could not be read right now".

    The pre-move read (:1495) used the plain reader — a locked Keychain or
    a permission glitch on the ``.enc`` read as "account 2 has no backup",
    and the move committed a slot key holding NOTHING while the source's
    live refresh token sat unread. Fixed with ``_read_account_credentials_ex``,
    aborting BEFORE anything moves (mirroring the strict-clear guards this
    same file already tests for the destination side).
    """

    def _write(self, switcher, data):
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, data)

    @pytest.mark.skipif(
        sys.platform == "win32" or os.geteuid() == 0,
        reason="needs POSIX permission semantics (non-root)",
    )
    def test_unreadable_enc_aborts_the_move_before_anything_changes(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)
        switcher._write_account_credentials("2", "account2@example.com", "live-rt")
        switcher._write_account_credentials("1", "account1@example.com", "rt-1")

        # CONTROL: a readable source moves cleanly (instrument says YES).
        num_src, num_target, swapped = switcher.move_account("1", "5")
        assert (num_src, num_target, swapped) == ("1", "5", False)
        assert (
            switcher._read_account_credentials("5", "account1@example.com")
            == "rt-1"
        )

        enc = switcher._backup_enc_path("2", "account2@example.com")
        enc.chmod(0o000)
        try:
            with pytest.raises(ConfigError, match="could not be read"):
                switcher.move_account("2", "6")
        finally:
            if enc.exists():
                enc.chmod(0o600)

        # Nothing committed: account 2 is intact under its original number,
        # holding its readable credential, and slot 6 was never claimed.
        data = switcher._get_sequence_data()
        assert data["accounts"]["2"]["email"] == "account2@example.com"
        assert "6" not in data["accounts"]
        assert (
            switcher._read_account_credentials("2", "account2@example.com")
            == "live-rt"
        )

    def test_absent_source_still_moves(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """Control in the other direction: a genuinely unbacked slot (no
        .enc at all) is not mistaken for unreadable and still moves."""
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)

        num_src, num_target, swapped = switcher.move_account("2", "5")

        assert (num_src, num_target, swapped) == ("2", "5", False)
        data = switcher._get_sequence_data()
        assert data["accounts"]["5"]["email"] == "account2@example.com"
