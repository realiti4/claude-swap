"""Tests for one-time data migrations (claude_swap.migrations).

The remaining migration relocates macOS backup credentials from the service the
third-party ``keyring`` library used (``claude-code``) into the ``security``-managed
service (``claude-swap``). Both are reached through the ``security`` CLI wrapper,
which the autouse ``block_real_keychain`` guard replaces with an in-memory store,
so the tests seed the legacy items there.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_swap import migrations
from claude_swap.exceptions import MigrationIncomplete
from claude_swap.macos_keychain import KeychainError
from claude_swap.migrations import run_migrations
from claude_swap.models import Platform
from claude_swap.switcher import KEYRING_SERVICE, ClaudeAccountSwitcher

EMAIL = "a@example.com"
LEGACY_USER = f"account-1-{EMAIL}"
LEGACY_NONE_USER = f"account-None-{EMAIL}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_switcher(temp_home: Path, platform: Platform) -> ClaudeAccountSwitcher:
    """A switcher forced to ``platform``, with dirs created. Construction runs the
    migrations while no sequence exists yet (a no-op); tests seed it after."""
    switcher = ClaudeAccountSwitcher()
    switcher.platform = platform
    switcher._setup_directories()
    return switcher


def _seed_sequence(switcher: ClaudeAccountSwitcher, accounts: dict) -> None:
    data = {
        "activeAccountNumber": None,
        "lastUpdated": "2024-01-01T00:00:00Z",
        "sequence": [int(k) for k in accounts if k.isdigit()],
        "accounts": accounts,
    }
    switcher._write_json(switcher.sequence_file, data)


def _legacy_reads(spy: MagicMock) -> list[str]:
    """The usernames read from the legacy service, in order."""
    return [c.args[1] for c in spy.call_args_list if c.args[0] == KEYRING_SERVICE]


@pytest.fixture
def keychain_reads(block_real_keychain, monkeypatch) -> MagicMock:
    """Spy on every Keychain read, still answered by the in-memory store."""
    spy = MagicMock(side_effect=block_real_keychain.get_password)
    monkeypatch.setattr(migrations.macos_keychain, "get_password", spy)
    return spy


# ---------------------------------------------------------------------------
# Runner guards
# ---------------------------------------------------------------------------


class TestRunner:
    def test_noop_when_backup_dir_absent(self, temp_home):
        switcher = ClaudeAccountSwitcher()
        switcher.platform = Platform.MACOS
        # Do NOT create directories — fresh-install invariant.
        if switcher.backup_dir.exists():
            pytest.skip("host already materialized backup dir")
        run_migrations(switcher)
        assert not switcher.backup_dir.exists()

    def test_retired_state_entries_are_kept(self, temp_home, block_real_keychain):
        """A state file from before the Windows keyring → files migration was
        retired still records it; marking the macOS one must not drop it."""
        switcher = _make_switcher(temp_home, Platform.MACOS)
        state_file = switcher.backup_dir / ".migrations.json"
        state_file.write_text(json.dumps({
            "version": 1,
            "applied": {"windows_keyring_to_files": "2026-01-01T00:00:00Z"},
        }))
        _seed_sequence(switcher, {"1": {"email": EMAIL}})

        run_migrations(switcher)

        applied = json.loads(state_file.read_text())["applied"]
        assert set(applied) == {"windows_keyring_to_files", "macos_keyring_to_security"}


# ---------------------------------------------------------------------------
# Windows uses the file backend for normal read/write/delete
# ---------------------------------------------------------------------------


class TestWindowsFileBackend:
    def test_round_trip_through_files(self, temp_home):
        switcher = _make_switcher(temp_home, Platform.WINDOWS)
        assert switcher._uses_file_backup_backend() is True

        switcher._write_account_credentials("1", EMAIL, "secret")
        cred_file = switcher.credentials_dir / f".creds-1-{EMAIL}.enc"
        assert cred_file.exists()
        assert switcher._read_account_credentials("1", EMAIL) == "secret"

        switcher._delete_account_credentials("1", EMAIL)
        assert switcher._read_account_credentials("1", EMAIL) == ""
        assert not cred_file.exists()


class TestPurgeWindows:
    def test_purge_removes_credential_files(self, temp_home):
        switcher = _make_switcher(temp_home, Platform.WINDOWS)
        _seed_sequence(switcher, {"1": {"email": EMAIL}})
        switcher._write_account_credentials("1", EMAIL, "secret")
        cred_file = switcher.credentials_dir / f".creds-1-{EMAIL}.enc"
        assert cred_file.exists()

        with patch("builtins.input", return_value="y"):
            switcher.purge()

        assert not cred_file.exists()


# ---------------------------------------------------------------------------
# macOS keyring → security service migration
# ---------------------------------------------------------------------------


class TestMacosKeyringToSecurity:
    """``migrate_macos_keyring_to_security``: relocate per-account backup creds
    from the legacy keyring service (``claude-code``) to the ``security``-managed
    service (``claude-swap``). Both live in the same Keychain, here the in-memory
    store the autouse guard installs."""

    def test_non_macos_skips(self, temp_home):
        switcher = _make_switcher(temp_home, Platform.LINUX)
        assert migrations.migrate_macos_keyring_to_security(switcher) is False

    def test_no_sequence_skips(self, temp_home):
        switcher = _make_switcher(temp_home, Platform.MACOS)
        switcher.sequence_file.unlink(missing_ok=True)
        assert migrations.migrate_macos_keyring_to_security(switcher) is False

    def test_empty_accounts_completes(self, temp_home):
        switcher = _make_switcher(temp_home, Platform.MACOS)
        _seed_sequence(switcher, {})
        assert migrations.migrate_macos_keyring_to_security(switcher) is True

    def test_relocates_legacy_item_and_removes_it(
        self, temp_home, block_real_keychain, capsys
    ):
        switcher = _make_switcher(temp_home, Platform.MACOS)
        _seed_sequence(switcher, {"1": {"email": EMAIL}})
        block_real_keychain.set_password(KEYRING_SERVICE, LEGACY_USER, "sekret")

        assert migrations.migrate_macos_keyring_to_security(switcher) is True

        # Readable from the new (security) service…
        assert switcher._read_account_credentials("1", EMAIL) == "sekret"
        # …and the legacy item is gone, deleted only after the verified copy.
        assert block_real_keychain.get_password(KEYRING_SERVICE, LEGACY_USER) is None
        assert "migrated 1 macOS credential" in capsys.readouterr().err

    def test_canonical_wins_and_the_stale_none_duplicate_is_removed(
        self, temp_home, block_real_keychain
    ):
        switcher = _make_switcher(temp_home, Platform.MACOS)
        _seed_sequence(switcher, {"1": {"email": EMAIL}})
        block_real_keychain.set_password(KEYRING_SERVICE, LEGACY_USER, "canonical")
        block_real_keychain.set_password(KEYRING_SERVICE, LEGACY_NONE_USER, "stale")

        assert migrations.migrate_macos_keyring_to_security(switcher) is True

        assert switcher._read_account_credentials("1", EMAIL) == "canonical"
        # The unique email attributes the account-None item to this slot: a stale
        # duplicate from an older buggy run, cleaned up rather than migrated.
        assert block_real_keychain.get_password(KEYRING_SERVICE, LEGACY_USER) is None
        assert block_real_keychain.get_password(KEYRING_SERVICE, LEGACY_NONE_USER) is None

    def test_failed_legacy_delete_warns_but_completes(
        self, temp_home, block_real_keychain, monkeypatch
    ):
        """The copy is verified before the delete, so a failed delete leaves a
        harmless orphan: the migration still completes (no retry loop) and the
        leftover is logged for purge to sweep up."""
        switcher = _make_switcher(temp_home, Platform.MACOS)
        switcher._logger = MagicMock()
        _seed_sequence(switcher, {"1": {"email": EMAIL}})
        block_real_keychain.set_password(KEYRING_SERVICE, LEGACY_USER, "sekret")

        def refuse_delete(service: str, account: str) -> None:
            raise KeychainError("security delete-generic-password failed (rc=1)")

        monkeypatch.setattr(migrations.macos_keychain, "delete_password", refuse_delete)

        assert migrations.migrate_macos_keyring_to_security(switcher) is True

        assert switcher._read_account_credentials("1", EMAIL) == "sekret"
        assert block_real_keychain.get_password(KEYRING_SERVICE, LEGACY_USER) == "sekret"
        warnings = " ".join(
            str(c.args[0]) for c in switcher._logger.warning.call_args_list
        )
        assert "left behind" in warnings and LEGACY_USER in warnings

    def test_account_none_item_is_used_when_the_email_is_unique(
        self, temp_home, block_real_keychain
    ):
        switcher = _make_switcher(temp_home, Platform.MACOS)
        _seed_sequence(switcher, {"1": {"email": EMAIL}})
        block_real_keychain.set_password(KEYRING_SERVICE, LEGACY_NONE_USER, "sekret")

        assert migrations.migrate_macos_keyring_to_security(switcher) is True
        assert switcher._read_account_credentials("1", EMAIL) == "sekret"
        # It was the source, so it is removed after the verified copy.
        assert block_real_keychain.get_password(KEYRING_SERVICE, LEGACY_NONE_USER) is None

    def test_account_none_item_is_ignored_for_a_duplicate_email(
        self, temp_home, block_real_keychain
    ):
        switcher = _make_switcher(temp_home, Platform.MACOS)
        _seed_sequence(switcher, {"1": {"email": EMAIL}, "2": {"email": EMAIL}})
        block_real_keychain.set_password(KEYRING_SERVICE, LEGACY_NONE_USER, "sekret")

        # Ambiguous: it could belong to either slot, so neither takes it, and it
        # is left alone rather than deleted (it may be the only copy).
        assert migrations.migrate_macos_keyring_to_security(switcher) is True
        assert switcher._read_account_credentials("1", EMAIL) == ""
        assert switcher._read_account_credentials("2", EMAIL) == ""
        assert block_real_keychain.get_password(KEYRING_SERVICE, LEGACY_NONE_USER) == "sekret"

    def test_shared_email_keeps_the_none_item_after_a_canonical_migration(
        self, temp_home, block_real_keychain
    ):
        switcher = _make_switcher(temp_home, Platform.MACOS)
        _seed_sequence(switcher, {"1": {"email": EMAIL}, "2": {"email": EMAIL}})
        block_real_keychain.set_password(KEYRING_SERVICE, LEGACY_USER, "canonical")
        block_real_keychain.set_password(KEYRING_SERVICE, LEGACY_NONE_USER, "whose?")

        assert migrations.migrate_macos_keyring_to_security(switcher) is True

        assert switcher._read_account_credentials("1", EMAIL) == "canonical"
        assert block_real_keychain.get_password(KEYRING_SERVICE, LEGACY_USER) is None
        # Slot 2 shares the email, so the account-None item cannot be attributed
        # to slot 1 as a stale duplicate: it stays for purge.
        assert block_real_keychain.get_password(KEYRING_SERVICE, LEGACY_NONE_USER) == "whose?"

    def test_precheck_skips_legacy_reads_when_already_migrated(
        self, temp_home, keychain_reads
    ):
        switcher = _make_switcher(temp_home, Platform.MACOS)
        _seed_sequence(switcher, {"1": {"email": EMAIL}})
        # Already present in the new security service.
        switcher._write_account_credentials("1", EMAIL, "already")

        assert migrations.migrate_macos_keyring_to_security(switcher) is True

        # Pre-check short-circuited before touching the legacy service at all.
        assert _legacy_reads(keychain_reads) == []

    def test_no_legacy_item_is_benign_skip(self, temp_home):
        switcher = _make_switcher(temp_home, Platform.MACOS)
        _seed_sequence(switcher, {"1": {"email": EMAIL}})

        # Nothing to move, but not a failure → completes.
        assert migrations.migrate_macos_keyring_to_security(switcher) is True
        assert switcher._read_account_credentials("1", EMAIL) == ""

    def test_read_back_mismatch_raises_and_keeps_the_legacy_item(
        self, temp_home, block_real_keychain
    ):
        switcher = _make_switcher(temp_home, Platform.MACOS)
        _seed_sequence(switcher, {"1": {"email": EMAIL}})
        block_real_keychain.set_password(KEYRING_SERVICE, LEGACY_USER, "sekret")

        # Force the security read-back to disagree with what was written. The
        # migration reads the security service via the keychain-only helper
        # (_kc_read_backup), not the transparent .enc-wins backup methods.
        with patch.object(switcher, "_kc_read_backup", side_effect=["", "WRONG"]):
            with pytest.raises(MigrationIncomplete):
                migrations.migrate_macos_keyring_to_security(switcher)

        # The bad copy was dropped for the retry; the legacy item is intact.
        assert switcher._read_account_credentials("1", EMAIL) == ""
        assert block_real_keychain.get_password(KEYRING_SERVICE, LEGACY_USER) == "sekret"

    def test_locked_legacy_read_defers(
        self, temp_home, block_real_keychain, keychain_reads
    ):
        switcher = _make_switcher(temp_home, Platform.MACOS)
        _seed_sequence(switcher, {"1": {"email": EMAIL}})
        block_real_keychain.set_password(KEYRING_SERVICE, LEGACY_USER, "sekret")

        def locked_legacy_service(service: str, account: str):
            if service == KEYRING_SERVICE:
                raise KeychainError("locked")
            return block_real_keychain.get_password(service, account)

        keychain_reads.side_effect = locked_legacy_service

        # A read failure is a real failure (retry), never "nothing to migrate".
        with pytest.raises(MigrationIncomplete):
            migrations.migrate_macos_keyring_to_security(switcher)
        assert switcher._read_account_credentials("1", EMAIL) == ""

    def test_security_keychain_unusable_defers_and_writes_no_enc(self, temp_home):
        # The destination (security-service) Keychain is unusable: the keychain-only
        # pending pre-check raises, so the migration must defer rather than mistake
        # an .enc for "already migrated" or write legacy creds to a file while
        # claiming the security service.
        switcher = _make_switcher(temp_home, Platform.MACOS)
        _seed_sequence(switcher, {"1": {"email": EMAIL}})

        with patch.object(switcher, "_kc_read_backup", side_effect=KeychainError("locked")):
            run_migrations(switcher)  # swallows MigrationIncomplete → left unmarked

        # Deferred cleanly: no fallback .enc written, migration not recorded.
        assert not (switcher.credentials_dir / f".creds-1-{EMAIL}.enc").exists()
        state_file = switcher.backup_dir / ".migrations.json"
        if state_file.exists():
            applied = json.loads(state_file.read_text()).get("applied", {})
            assert "macos_keyring_to_security" not in applied

    def test_runner_marks_applied_and_stops_reading_the_legacy_service(
        self, temp_home, block_real_keychain, keychain_reads
    ):
        switcher = _make_switcher(temp_home, Platform.MACOS)
        _seed_sequence(switcher, {"1": {"email": EMAIL}})
        block_real_keychain.set_password(KEYRING_SERVICE, LEGACY_USER, "sekret")

        run_migrations(switcher)
        state = json.loads((switcher.backup_dir / ".migrations.json").read_text())
        assert "macos_keyring_to_security" in state["applied"]
        assert _legacy_reads(keychain_reads) == [LEGACY_USER]

        # Recorded as applied: the next run never looks at the legacy service.
        keychain_reads.reset_mock()
        run_migrations(switcher)
        assert _legacy_reads(keychain_reads) == []
