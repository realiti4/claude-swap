"""Tests for `cswap swap` (ClaudeAccountSwitcher.swap_accounts)."""

import sys
from pathlib import Path

import pytest

from claude_swap.exceptions import (
    AccountNotFoundError,
    ConfigError,
    CredentialError,
    ValidationError,
)
from claude_swap.switcher import ClaudeAccountSwitcher


class TestSwapAccounts:
    """Test ClaudeAccountSwitcher.swap_accounts()."""

    def _write(self, switcher, data):
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, data)

    def test_swap_by_number(self, temp_home: Path, sample_sequence_data: dict):
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)

        num_a, num_b = switcher.swap_accounts("1", "2")

        assert (num_a, num_b) == ("1", "2")
        data = switcher._get_sequence_data()
        assert data["accounts"]["1"]["email"] == "account2@example.com"
        assert data["accounts"]["2"]["email"] == "account1@example.com"

    def test_swap_moves_active_number_with_account(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)
        assert sample_sequence_data["activeAccountNumber"] == 1

        switcher.swap_accounts("1", "2")

        data = switcher._get_sequence_data()
        # account1 was active and now lives in slot 2
        assert data["activeAccountNumber"] == 2
        assert data["accounts"]["2"]["email"] == "account1@example.com"

    def test_swap_keeps_sequence_sorted(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """Sequence stays sorted, so rotation and list order follow the new
        numbers — the accounts genuinely trade places in `cswap list`."""
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)

        switcher.swap_accounts("1", "2")

        data = switcher._get_sequence_data()
        assert data["sequence"] == [1, 2]

    def test_swap_by_email_and_alias(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        switcher = ClaudeAccountSwitcher()
        sample_sequence_data["accounts"]["2"]["alias"] = "dev"
        self._write(switcher, sample_sequence_data)

        num_a, num_b = switcher.swap_accounts("account1@example.com", "dev")

        assert (num_a, num_b) == ("1", "2")
        data = switcher._get_sequence_data()
        # The alias travels with its account into the new slot.
        assert data["accounts"]["1"].get("alias") == "dev"
        assert data["accounts"]["2"].get("alias") is None

    def test_swap_moves_credential_and_config_backups(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)
        switcher._write_account_credentials("1", "account1@example.com", "creds-one")
        switcher._write_account_config("1", "account1@example.com", "config-one")
        switcher._write_account_credentials("2", "account2@example.com", "creds-two")
        switcher._write_account_config("2", "account2@example.com", "config-two")

        switcher.swap_accounts("1", "2")

        assert (
            switcher._read_account_credentials("2", "account1@example.com")
            == "creds-one"
        )
        assert (
            switcher._read_account_config("2", "account1@example.com") == "config-one"
        )
        assert (
            switcher._read_account_credentials("1", "account2@example.com")
            == "creds-two"
        )
        assert (
            switcher._read_account_config("1", "account2@example.com") == "config-two"
        )
        # Old keys are gone.
        assert switcher._read_account_credentials("1", "account1@example.com") == ""
        assert switcher._read_account_credentials("2", "account2@example.com") == ""

    def test_swap_with_one_slot_missing_backups(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """A never-backed-up slot swaps cleanly and stays credential-less."""
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)
        switcher._write_account_credentials("1", "account1@example.com", "creds-one")

        switcher.swap_accounts("1", "2")

        assert (
            switcher._read_account_credentials("2", "account1@example.com")
            == "creds-one"
        )
        assert switcher._read_account_credentials("1", "account2@example.com") == ""

    def test_swap_same_account_rejected(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)

        with pytest.raises(ValidationError):
            switcher.swap_accounts("1", "1")

    def test_swap_unknown_identifier_rejected(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)

        with pytest.raises(AccountNotFoundError):
            switcher.swap_accounts("1", "nosuch@example.com")

    def test_swap_same_email_accounts(
        self, temp_home: Path, sample_sequence_data_with_org: dict
    ):
        """Same email, different orgs: the backup keys fully overlap."""
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data_with_org)
        email = "user@example.com"
        switcher._write_account_credentials("1", email, "creds-org")
        switcher._write_account_credentials("2", email, "creds-personal")

        switcher.swap_accounts("1", "2")

        assert switcher._read_account_credentials("1", email) == "creds-personal"
        assert switcher._read_account_credentials("2", email) == "creds-org"
        data = switcher._get_sequence_data()
        assert data["accounts"]["2"]["organizationUuid"] == "org-uuid-5678"
        # The durable staging copies are cleaned up after the commit.
        assert not list(switcher.credentials_dir.glob(".swap-staging-*"))

    def test_swap_same_email_partial_failure_rolls_back(
        self, temp_home: Path, sample_sequence_data_with_org: dict, monkeypatch
    ):
        """A write failure mid-swap must not destroy an overlapping backup.

        With a shared email the destination key IS the other account's key,
        so without a rollback the second account's credential would exist
        nowhere but in memory after the first write.
        """
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data_with_org)
        email = "user@example.com"
        switcher._write_account_credentials("1", email, "creds-org")
        switcher._write_account_credentials("2", email, "creds-personal")

        real_write = ClaudeAccountSwitcher._write_account_credentials
        calls = {"n": 0}

        def failing_write(self, num, email, creds):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("disk full (injected)")
            return real_write(self, num, email, creds)

        monkeypatch.setattr(
            ClaudeAccountSwitcher, "_write_account_credentials", failing_write
        )
        with pytest.raises(OSError):
            switcher.swap_accounts("1", "2")
        monkeypatch.undo()

        # Both originals are back under their pre-swap keys, and the account
        # table was never renumbered.
        assert switcher._read_account_credentials("1", email) == "creds-org"
        assert switcher._read_account_credentials("2", email) == "creds-personal"
        data = switcher._get_sequence_data()
        assert data["accounts"]["1"]["organizationUuid"] == "org-uuid-5678"
        assert data["activeAccountNumber"] == 1

    def test_swap_same_email_persistent_failure_keeps_staged_copy(
        self, temp_home: Path, sample_sequence_data_with_org: dict, monkeypatch
    ):
        """When the restore writes fail too (persistent backend outage), the
        pre-swap material must survive on disk in the staged copies — not
        only in the dying process's memory."""
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data_with_org)
        email = "user@example.com"
        switcher._write_account_credentials("1", email, "creds-org")
        switcher._write_account_credentials("2", email, "creds-personal")

        real_write = ClaudeAccountSwitcher._write_account_credentials
        calls = {"n": 0}

        def failing_write(self, num, email, creds):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise OSError("disk full (injected, persistent)")
            return real_write(self, num, email, creds)

        monkeypatch.setattr(
            ClaudeAccountSwitcher, "_write_account_credentials", failing_write
        )
        with pytest.raises(OSError):
            switcher.swap_accounts("1", "2")
        monkeypatch.undo()

        # Slot 1's stored copy was never touched; slot 2's store now holds
        # the wrong material (restore failed), but the staged copy has it.
        assert switcher._read_account_credentials("1", email) == "creds-org"
        staged = switcher.credentials_dir / ".swap-staging-creds-2.json"
        assert staged.read_text(encoding="utf-8") == "creds-personal"
        if sys.platform != "win32":
            assert staged.stat().st_mode & 0o777 == 0o600

    def test_swap_same_email_rollback_restores_empty_slot(
        self, temp_home: Path, sample_sequence_data_with_org: dict, monkeypatch
    ):
        """Slot 2 was never backed up: after a failed swap, the shared key
        must read empty again — not keep account 1's credential under
        account 2's slot."""
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data_with_org)
        email = "user@example.com"
        switcher._write_account_credentials("1", email, "creds-org")

        def failing_write_json(self, path, data):
            raise OSError("disk full (injected)")

        monkeypatch.setattr(ClaudeAccountSwitcher, "_write_json", failing_write_json)
        with pytest.raises(OSError):
            switcher.swap_accounts("1", "2")
        monkeypatch.undo()

        assert switcher._read_account_credentials("1", email) == "creds-org"
        assert switcher._read_account_credentials("2", email) == ""
        data = switcher._get_sequence_data()
        assert data["accounts"]["1"]["organizationUuid"] == "org-uuid-5678"
        # Clean rollback: no staged copies left behind either.
        assert not list(switcher.credentials_dir.glob(".swap-staging-*"))

    def test_write_json_publishes_only_after_chmod(
        self, temp_home: Path, sample_sequence_data: dict, monkeypatch
    ):
        """chmod runs on the temp file, making the rename the final commit —
        a chmod failure must abort *without* publishing, otherwise callers
        would roll files back around already-committed metadata."""
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)
        before = switcher.sequence_file.read_text(encoding="utf-8")

        def failing_chmod(path, mode):
            raise OSError("chmod denied (injected)")

        monkeypatch.setattr("claude_swap.switcher.os.chmod", failing_chmod)
        with pytest.raises(OSError):
            switcher._write_json(switcher.sequence_file, {"x": 1})
        monkeypatch.undo()

        assert switcher.sequence_file.read_text(encoding="utf-8") == before

    def test_swap_same_email_one_sided_clears_destination(
        self, temp_home: Path, sample_sequence_data_with_org: dict
    ):
        """Same email, only slot 1 backed up: after the swap, the unbacked
        account's new key must read empty — with fully overlapping keys the
        old key is never separately deleted, so it must be actively cleared,
        not skipped, or account 2 would serve account 1's credential."""
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data_with_org)
        email = "user@example.com"
        switcher._write_account_credentials("1", email, "creds-org")

        switcher.swap_accounts("1", "2")

        # Account 1 (backed) now lives in slot 2 with its credential;
        # account 2 (unbacked) now lives in slot 1 and must stay unbacked.
        assert switcher._read_account_credentials("2", email) == "creds-org"
        assert switcher._read_account_credentials("1", email) == ""
        data = switcher._get_sequence_data()
        assert data["accounts"]["2"]["organizationUuid"] == "org-uuid-5678"

    def test_swap_clears_stale_destination_key(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """Distinct emails, source unbacked: a stale file leaked under the
        destination key (e.g. by an earlier crash) must not be adopted."""
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)
        # Account 1 has no backup; plant a stale foreign file under the key
        # it will occupy after the swap: (slot 2, account1's email).
        switcher._write_account_credentials(
            "2", "account1@example.com", "stale-foreign"
        )

        switcher.swap_accounts("1", "2")

        assert switcher._read_account_credentials("2", "account1@example.com") == ""

    def test_swap_refuses_leftover_staging(
        self, temp_home: Path, sample_sequence_data_with_org: dict
    ):
        """Leftover staging from an interrupted swap may be the only copy of
        a credential: a retry must refuse loudly, never overwrite it."""
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data_with_org)
        email = "user@example.com"
        switcher._write_account_credentials("1", email, "creds-org")
        switcher._write_account_credentials("2", email, "creds-personal")
        leftover = switcher.credentials_dir / ".swap-staging-creds-1.json"
        leftover.write_text("only-surviving-copy", encoding="utf-8")

        with pytest.raises(ConfigError, match="interrupted swap"):
            switcher.swap_accounts("1", "2")

        # The leftover is untouched and nothing was swapped.
        assert leftover.read_text(encoding="utf-8") == "only-surviving-copy"
        assert switcher._read_account_credentials("1", email) == "creds-org"
        assert switcher._read_account_credentials("2", email) == "creds-personal"
        data = switcher._get_sequence_data()
        assert data["accounts"]["1"]["organizationUuid"] == "org-uuid-5678"

    def test_swap_failed_required_clear_aborts_commit(
        self, temp_home: Path, sample_sequence_data_with_org: dict, monkeypatch
    ):
        """Same-email one-sided swap where the required clear fails: the swap
        must abort pre-commit and roll back, instead of committing with
        account 1's credential still readable under the shared key."""
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data_with_org)
        email = "user@example.com"
        switcher._write_account_credentials("1", email, "creds-org")

        real_unlink = Path.unlink

        def failing_unlink(path, *args, **kwargs):
            if path.name.startswith(".creds-1-"):
                raise OSError("permission denied (injected)")
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", failing_unlink)
        with pytest.raises(CredentialError, match="aborting before commit"):
            switcher.swap_accounts("1", "2")
        monkeypatch.undo()

        # Table unrenumbered, slot 1's credential intact, and the rollback
        # reverted the half-written copy under the shared key.
        data = switcher._get_sequence_data()
        assert data["accounts"]["1"]["organizationUuid"] == "org-uuid-5678"
        assert switcher._read_account_credentials("1", email) == "creds-org"
        assert switcher._read_account_credentials("2", email) == ""

    def test_swap_same_email_clears_prev_generations(
        self, temp_home: Path, sample_sequence_data_with_org: dict
    ):
        """Writing through the overlapping keys retains the displaced
        account's credential as each key's .prev generation; after the commit
        those must be gone — recovery must never resurrect another account's
        token onto a slot."""
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data_with_org)
        email = "user@example.com"
        switcher._write_account_credentials("1", email, "creds-org")
        switcher._write_account_credentials("2", email, "creds-personal")

        switcher.swap_accounts("1", "2")

        assert not list(switcher.credentials_dir.glob("*.enc.prev"))

    def test_swap_holds_account_lock(
        self, temp_home: Path, sample_sequence_data: dict, monkeypatch
    ):
        """The whole mutation runs under the same lock switch/persist take."""
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
        switcher.swap_accounts("1", "2")

        assert entered == [switcher.lock_file]

    def test_swap_moves_session_profiles(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)
        session_a = switcher._session_dir("1", "account1@example.com")
        session_a.mkdir(parents=True)
        (session_a / "marker.txt").write_text("history-of-account-one")

        switcher.swap_accounts("1", "2")

        moved = switcher._session_dir("2", "account1@example.com")
        assert (moved / "marker.txt").read_text() == "history-of-account-one"
        assert not session_a.exists()
