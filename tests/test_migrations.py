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
from claude_swap.macos_keychain import KeychainError, KeychainTimeout
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


# ---------------------------------------------------------------------------
# Waiting for the Keychain permission window
# ---------------------------------------------------------------------------

EMAIL_B = "b@example.com"
EMAIL_C = "c@example.com"
LEGACY_USER_B = f"account-2-{EMAIL_B}"
LEGACY_USER_C = f"account-3-{EMAIL_C}"
THREE_ACCOUNTS = {
    "1": {"email": EMAIL},
    "2": {"email": EMAIL_B},
    "3": {"email": EMAIL_C},
}


def _legacy_timeouts(spy: MagicMock) -> list[float | None]:
    """The ``timeout`` each legacy read asked for; ``None`` is a read that passed
    none, so the wrapper's default bound applies."""
    return [
        c.kwargs.get("timeout")
        for c in spy.call_args_list
        if c.args[0] == KEYRING_SERVICE
    ]


class TestKeychainPromptWait:
    """A legacy read waits for the Keychain permission window only when a person
    is running the command and the legacy item is known to exist; every other
    read keeps the wrapper's default bound."""

    def test_person_at_terminal_gets_one_notice_and_the_long_wait(
        self, temp_home, block_real_keychain, keychain_reads, capsys
    ):
        switcher = _make_switcher(temp_home, Platform.MACOS)
        switcher.interactive = True
        _seed_sequence(switcher, {"1": {"email": EMAIL}, "2": {"email": EMAIL_B}})
        block_real_keychain.set_password(KEYRING_SERVICE, LEGACY_USER, "one")
        block_real_keychain.set_password(KEYRING_SERVICE, LEGACY_USER_B, "two")

        assert migrations.migrate_macos_keyring_to_security(switcher) is True

        assert _legacy_timeouts(keychain_reads) == [migrations._PROMPT_WAIT] * 2
        assert capsys.readouterr().err.count("Always Allow") == 1
        assert switcher._read_account_credentials("1", EMAIL) == "one"
        assert switcher._read_account_credentials("2", EMAIL_B) == "two"

    def test_other_callers_keep_the_default_bound_and_get_no_notice(
        self, temp_home, block_real_keychain, keychain_reads, capsys
    ):
        switcher = _make_switcher(temp_home, Platform.MACOS)
        assert switcher.interactive is False
        _seed_sequence(switcher, {"1": {"email": EMAIL}})
        block_real_keychain.set_password(KEYRING_SERVICE, LEGACY_USER, "sekret")

        assert migrations.migrate_macos_keyring_to_security(switcher) is True

        legacy_calls = [
            c for c in keychain_reads.call_args_list if c.args[0] == KEYRING_SERVICE
        ]
        assert [c.args[1] for c in legacy_calls] == [LEGACY_USER]
        assert all("timeout" not in c.kwargs for c in legacy_calls)
        assert "Always Allow" not in capsys.readouterr().err

    def test_no_legacy_item_means_no_notice_and_no_long_wait(
        self, temp_home, block_real_keychain, keychain_reads, capsys
    ):
        switcher = _make_switcher(temp_home, Platform.MACOS)
        switcher.interactive = True
        _seed_sequence(switcher, {"1": {"email": EMAIL}})

        assert migrations.migrate_macos_keyring_to_security(switcher) is True

        assert _legacy_reads(keychain_reads) == [LEGACY_USER, LEGACY_NONE_USER]
        assert _legacy_timeouts(keychain_reads) == [None, None]
        assert "Always Allow" not in capsys.readouterr().err

    def test_failed_lookup_still_reads_and_migrates_the_item(
        self, temp_home, block_real_keychain, keychain_reads, monkeypatch
    ):
        """``item_exists`` answers False for a failed lookup too, so it only picks
        the bound: the read itself decides whether the item is absent."""
        switcher = _make_switcher(temp_home, Platform.MACOS)
        switcher.interactive = True
        _seed_sequence(switcher, {"1": {"email": EMAIL}})
        block_real_keychain.set_password(KEYRING_SERVICE, LEGACY_USER, "sekret")
        monkeypatch.setattr(
            migrations.macos_keychain, "item_exists", lambda service, account: False
        )

        assert migrations.migrate_macos_keyring_to_security(switcher) is True

        assert _legacy_timeouts(keychain_reads) == [None]
        assert switcher._read_account_credentials("1", EMAIL) == "sekret"

    def test_unanswered_prompt_ends_the_attempt_and_keeps_earlier_work(
        self, temp_home, block_real_keychain, keychain_reads
    ):
        switcher = _make_switcher(temp_home, Platform.MACOS)
        switcher.interactive = True
        _seed_sequence(switcher, THREE_ACCOUNTS)
        block_real_keychain.set_password(KEYRING_SERVICE, LEGACY_USER, "one")
        block_real_keychain.set_password(KEYRING_SERVICE, LEGACY_USER_B, "two")
        block_real_keychain.set_password(KEYRING_SERVICE, LEGACY_USER_C, "three")

        def second_prompt_unanswered(service, account, timeout=None):
            if (service, account) == (KEYRING_SERVICE, LEGACY_USER_B):
                raise KeychainTimeout("timed out")
            return block_real_keychain.get_password(service, account)

        keychain_reads.side_effect = second_prompt_unanswered

        with pytest.raises(MigrationIncomplete, match="2 account.*cswap list"):
            migrations.migrate_macos_keyring_to_security(switcher)

        # The third account is never read: no second window, no second wait.
        assert _legacy_reads(keychain_reads) == [LEGACY_USER, LEGACY_USER_B]
        # The answered one moved; the other two are intact in the legacy service.
        assert switcher._read_account_credentials("1", EMAIL) == "one"
        assert block_real_keychain.get_password(KEYRING_SERVICE, LEGACY_USER) is None
        assert block_real_keychain.get_password(KEYRING_SERVICE, LEGACY_USER_B) == "two"
        assert block_real_keychain.get_password(KEYRING_SERVICE, LEGACY_USER_C) == "three"

    def test_unanswered_prompt_leaves_the_migration_unmarked(
        self, temp_home, block_real_keychain, keychain_reads
    ):
        switcher = _make_switcher(temp_home, Platform.MACOS)
        switcher.interactive = True
        _seed_sequence(switcher, {"1": {"email": EMAIL}})
        block_real_keychain.set_password(KEYRING_SERVICE, LEGACY_USER, "sekret")
        keychain_reads.side_effect = KeychainTimeout("timed out")

        run_migrations(switcher)  # logs the incomplete run, never raises

        state_file = switcher.backup_dir / ".migrations.json"
        if state_file.exists():
            applied = json.loads(state_file.read_text()).get("applied", {})
            assert "macos_keyring_to_security" not in applied
        assert block_real_keychain.get_password(KEYRING_SERVICE, LEGACY_USER) == "sekret"

    def test_denied_prompt_moves_on_to_the_next_account(
        self, temp_home, block_real_keychain, keychain_reads
    ):
        """Only a timeout ends the attempt: a refusal returns at once, so the
        next account is still tried, and the message names no window to approve."""
        switcher = _make_switcher(temp_home, Platform.MACOS)
        switcher.interactive = True
        _seed_sequence(switcher, {"1": {"email": EMAIL}, "2": {"email": EMAIL_B}})
        block_real_keychain.set_password(KEYRING_SERVICE, LEGACY_USER, "one")
        block_real_keychain.set_password(KEYRING_SERVICE, LEGACY_USER_B, "two")

        def first_denied(service, account, timeout=None):
            if (service, account) == (KEYRING_SERVICE, LEGACY_USER):
                raise KeychainError("denied")
            return block_real_keychain.get_password(service, account)

        keychain_reads.side_effect = first_denied

        with pytest.raises(MigrationIncomplete) as raised:
            migrations.migrate_macos_keyring_to_security(switcher)

        assert "cswap list" not in str(raised.value)
        assert switcher._read_account_credentials("2", EMAIL_B) == "two"

    def test_timeout_without_a_person_still_tries_every_account(
        self, temp_home, block_real_keychain, keychain_reads
    ):
        switcher = _make_switcher(temp_home, Platform.MACOS)
        _seed_sequence(switcher, {"1": {"email": EMAIL}, "2": {"email": EMAIL_B}})
        block_real_keychain.set_password(KEYRING_SERVICE, LEGACY_USER, "one")
        block_real_keychain.set_password(KEYRING_SERVICE, LEGACY_USER_B, "two")

        def legacy_times_out(service, account):
            if service == KEYRING_SERVICE:
                raise KeychainTimeout("timed out")
            return block_real_keychain.get_password(service, account)

        keychain_reads.side_effect = legacy_times_out

        with pytest.raises(MigrationIncomplete, match="2 account.*cswap list"):
            migrations.migrate_macos_keyring_to_security(switcher)

        assert _legacy_reads(keychain_reads) == [LEGACY_USER, LEGACY_USER_B]
