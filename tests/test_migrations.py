"""Tests for one-time data migrations (claude_swap.migrations).

The headline migration relocates Windows backup credentials from Credential
Manager (keyring) to base64 files. Since ``migrations.py`` does ``import
keyring`` locally, patching ``claude_swap.switcher.keyring`` would NOT affect
it — these tests inject a fake ``keyring`` module via ``sys.modules`` so the
migration's own import picks it up.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_swap import migrations
from claude_swap.exceptions import MigrationIncomplete
from claude_swap.macos_keychain import KeychainError
from claude_swap.migrations import run_migrations
from claude_swap.models import Platform
from claude_swap.switcher import KEYRING_SERVICE, ClaudeAccountSwitcher


# ---------------------------------------------------------------------------
# Fakes / fixtures
# ---------------------------------------------------------------------------


class _FakeKeyringErrors:
    class PasswordDeleteError(Exception):
        pass


class FakeKeyring:
    """Minimal stand-in for the ``keyring`` module backed by a dict."""

    def __init__(self, store: dict | None = None):
        # store maps (service, username) -> password
        self.store: dict[tuple[str, str], str] = dict(store or {})
        self.errors = _FakeKeyringErrors
        self.get_calls: list[tuple[str, str]] = []
        self.deleted: list[tuple[str, str]] = []
        self.raise_get_for: set[str] = set()
        # keyring's macOS backend raises PasswordDeleteError when the user
        # denies the delete prompt — same class as "entry doesn't exist".
        self.deny_delete_for: set[str] = set()

    def get_password(self, service, username):
        self.get_calls.append((service, username))
        if username in self.raise_get_for:
            raise RuntimeError(f"boom reading {username}")
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        self.deleted.append((service, username))
        if username in self.deny_delete_for:
            raise self.errors.PasswordDeleteError(f"denied: {username}")
        if (service, username) in self.store:
            del self.store[(service, username)]
        else:
            raise self.errors.PasswordDeleteError(username)


def _make_windows_switcher(temp_home: Path) -> ClaudeAccountSwitcher:
    """A switcher whose platform is forced to WINDOWS, with dirs created."""
    switcher = ClaudeAccountSwitcher()
    switcher.platform = Platform.WINDOWS
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


def _patch_keyring(fake: FakeKeyring):
    return patch.dict(sys.modules, {"keyring": fake})


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestWindowsKeyringToFiles:
    def test_migrates_entries_to_files_and_records_state(self, temp_home):
        switcher = _make_windows_switcher(temp_home)
        _seed_sequence(
            switcher,
            {
                "1": {"email": "a@example.com"},
                "2": {"email": "b@example.com"},
            },
        )
        fake = FakeKeyring(
            {
                (KEYRING_SERVICE, "account-1-a@example.com"): "creds-A",
                (KEYRING_SERVICE, "account-2-b@example.com"): "creds-B",
            }
        )

        with _patch_keyring(fake):
            run_migrations(switcher)

        # Files written with the right (decoded) content.
        assert switcher._read_account_credentials("1", "a@example.com") == "creds-A"
        assert switcher._read_account_credentials("2", "b@example.com") == "creds-B"
        # Keyring entries cleaned up.
        assert fake.store == {}
        assert (KEYRING_SERVICE, "account-1-a@example.com") in fake.deleted
        # State recorded.
        state = json.loads((switcher.backup_dir / ".migrations.json").read_text())
        assert "windows_keyring_to_files" in state["applied"]
        assert state["version"] == 1

    def test_idempotent_second_run_touches_no_keyring(self, temp_home):
        switcher = _make_windows_switcher(temp_home)
        _seed_sequence(switcher, {"1": {"email": "a@example.com"}})
        fake = FakeKeyring({(KEYRING_SERVICE, "account-1-a@example.com"): "creds-A"})

        with _patch_keyring(fake):
            run_migrations(switcher)
        first_calls = list(fake.get_calls)

        # Second run: already applied → no keyring access at all.
        with _patch_keyring(fake):
            run_migrations(switcher)

        assert fake.get_calls == first_calls  # no new reads
        assert switcher._read_account_credentials("1", "a@example.com") == "creds-A"

    def test_creates_credentials_dir_if_missing(self, temp_home):
        """Existing keyring users may have sequence.json but no credentials/
        dir; the migration must create it rather than fail on write."""
        switcher = _make_windows_switcher(temp_home)
        _seed_sequence(switcher, {"1": {"email": "a@example.com"}})
        # Simulate the gap: remove the credentials dir created by setup.
        import shutil as _shutil

        _shutil.rmtree(switcher.credentials_dir)
        assert not switcher.credentials_dir.exists()
        fake = FakeKeyring({(KEYRING_SERVICE, "account-1-a@example.com"): "creds-A"})

        with _patch_keyring(fake):
            run_migrations(switcher)

        assert switcher.credentials_dir.exists()
        assert switcher._read_account_credentials("1", "a@example.com") == "creds-A"
        state = json.loads((switcher.backup_dir / ".migrations.json").read_text())
        assert "windows_keyring_to_files" in state["applied"]

    def test_completes_when_no_legacy_entries_present(self, temp_home):
        """A new-version Windows install with accounts but no keyring entries
        still marks the migration applied so we stop probing on every start."""
        switcher = _make_windows_switcher(temp_home)
        _seed_sequence(switcher, {"1": {"email": "a@example.com"}})
        # File already present (added on the new version); keyring empty.
        switcher._write_account_credentials("1", "a@example.com", "file-creds")
        fake = FakeKeyring()

        with _patch_keyring(fake):
            run_migrations(switcher)

        state = json.loads((switcher.backup_dir / ".migrations.json").read_text())
        assert "windows_keyring_to_files" in state["applied"]
        # The existing file is untouched.
        assert switcher._read_account_credentials("1", "a@example.com") == "file-creds"


# ---------------------------------------------------------------------------
# Skips (return False → unmarked)
# ---------------------------------------------------------------------------


class TestSkips:
    def test_non_windows_is_noop(self, temp_home):
        switcher = _make_windows_switcher(temp_home)
        switcher.platform = Platform.LINUX
        _seed_sequence(switcher, {"1": {"email": "a@example.com"}})
        fake = FakeKeyring({(KEYRING_SERVICE, "account-1-a@example.com"): "x"})

        with _patch_keyring(fake):
            assert migrations.migrate_windows_keyring_to_files(switcher) is False
        assert fake.get_calls == []
        assert not (switcher.backup_dir / ".migrations.json").exists()

    def test_no_sequence_file_skips_unmarked(self, temp_home):
        switcher = _make_windows_switcher(temp_home)
        # No sequence.json written.
        fake = FakeKeyring()
        with _patch_keyring(fake):
            assert migrations.migrate_windows_keyring_to_files(switcher) is False
        assert not (switcher.backup_dir / ".migrations.json").exists()

    def test_corrupt_sequence_not_marked(self, temp_home):
        switcher = _make_windows_switcher(temp_home)
        switcher.sequence_file.write_text("{ not json", encoding="utf-8")
        fake = FakeKeyring()
        with _patch_keyring(fake):
            run_migrations(switcher)
        # Unparseable sequence → never marked, so a later repair can migrate.
        assert not (switcher.backup_dir / ".migrations.json").exists()


# ---------------------------------------------------------------------------
# account-None disambiguation
# ---------------------------------------------------------------------------


class TestAccountNoneFallback:
    def test_canonical_wins_over_none(self, temp_home):
        switcher = _make_windows_switcher(temp_home)
        _seed_sequence(switcher, {"1": {"email": "a@example.com"}})
        fake = FakeKeyring(
            {
                (KEYRING_SERVICE, "account-1-a@example.com"): "canonical",
                (KEYRING_SERVICE, "account-None-a@example.com"): "stale-none",
            }
        )
        with _patch_keyring(fake):
            run_migrations(switcher)

        assert switcher._read_account_credentials("1", "a@example.com") == "canonical"
        # The stale None entry is cleaned up, not migrated into the slot.
        assert (KEYRING_SERVICE, "account-None-a@example.com") in fake.deleted
        assert fake.store == {}

    def test_none_used_as_fallback_when_email_unique(self, temp_home):
        switcher = _make_windows_switcher(temp_home)
        _seed_sequence(switcher, {"1": {"email": "solo@example.com"}})
        fake = FakeKeyring(
            {(KEYRING_SERVICE, "account-None-solo@example.com"): "from-none"}
        )
        with _patch_keyring(fake):
            run_migrations(switcher)
        assert (
            switcher._read_account_credentials("1", "solo@example.com") == "from-none"
        )

    def test_none_not_used_for_duplicate_email(self, temp_home):
        """Two org accounts share an email → account-None is ambiguous and must
        not be migrated into any slot."""
        switcher = _make_windows_switcher(temp_home)
        _seed_sequence(
            switcher,
            {
                "1": {"email": "dup@example.com", "organizationUuid": "org-1"},
                "2": {"email": "dup@example.com", "organizationUuid": "org-2"},
            },
        )
        fake = FakeKeyring(
            {(KEYRING_SERVICE, "account-None-dup@example.com"): "ambiguous"}
        )
        with _patch_keyring(fake):
            run_migrations(switcher)

        # Neither slot received the ambiguous value.
        assert switcher._read_account_credentials("1", "dup@example.com") == ""
        assert switcher._read_account_credentials("2", "dup@example.com") == ""
        # The ambiguous account-None is left alone (not deleted), to avoid
        # destroying possibly-only-copy data we can't attribute.
        assert (KEYRING_SERVICE, "account-None-dup@example.com") in fake.store


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


class TestFailures:
    def test_read_back_mismatch_keeps_keyring_and_unmarked(self, temp_home):
        switcher = _make_windows_switcher(temp_home)
        _seed_sequence(switcher, {"1": {"email": "a@example.com"}})
        fake = FakeKeyring({(KEYRING_SERVICE, "account-1-a@example.com"): "creds-A"})

        # Force the verify step to disagree with the source.
        with patch.object(
            switcher, "_read_account_credentials", return_value="tampered"
        ):
            with _patch_keyring(fake):
                run_migrations(switcher)

        # Source entry preserved, migration not recorded → retried next run.
        assert (KEYRING_SERVICE, "account-1-a@example.com") in fake.store
        assert fake.deleted == []
        assert not (switcher.backup_dir / ".migrations.json").exists()
        # The bad/partial file must not shadow the intact keyring entry.
        assert not (
            switcher.credentials_dir / ".creds-1-a@example.com.enc"
        ).exists()

    def test_partial_failure_migrates_rest_and_stays_unmarked(self, temp_home):
        switcher = _make_windows_switcher(temp_home)
        _seed_sequence(
            switcher,
            {
                "1": {"email": "ok@example.com"},
                "2": {"email": "bad@example.com"},
            },
        )
        fake = FakeKeyring(
            {
                (KEYRING_SERVICE, "account-1-ok@example.com"): "good",
                (KEYRING_SERVICE, "account-2-bad@example.com"): "never-read",
            }
        )
        fake.raise_get_for.add("account-2-bad@example.com")

        with _patch_keyring(fake):
            run_migrations(switcher)  # swallows MigrationIncomplete

        # The healthy account migrated; the failed one is untouched + unmarked.
        assert switcher._read_account_credentials("1", "ok@example.com") == "good"
        assert (KEYRING_SERVICE, "account-2-bad@example.com") in fake.store
        assert not (switcher.backup_dir / ".migrations.json").exists()

    def test_partial_failure_raises_from_migration_fn(self, temp_home):
        switcher = _make_windows_switcher(temp_home)
        _seed_sequence(switcher, {"1": {"email": "bad@example.com"}})
        fake = FakeKeyring()
        fake.raise_get_for.add("account-1-bad@example.com")
        with _patch_keyring(fake):
            with pytest.raises(MigrationIncomplete):
                migrations.migrate_windows_keyring_to_files(switcher)

    def test_inaccessible_backend_raises_incomplete(self, temp_home):
        switcher = _make_windows_switcher(temp_home)
        _seed_sequence(switcher, {"1": {"email": "a@example.com"}})
        # Make `import keyring` fail inside the migration.
        with patch.dict(sys.modules, {"keyring": None}):
            with pytest.raises(MigrationIncomplete):
                migrations.migrate_windows_keyring_to_files(switcher)
        # Runner swallows it and leaves it unmarked.
        with patch.dict(sys.modules, {"keyring": None}):
            run_migrations(switcher)
        assert not (switcher.backup_dir / ".migrations.json").exists()


# ---------------------------------------------------------------------------
# Runner guards
# ---------------------------------------------------------------------------


class TestRunner:
    def test_noop_when_backup_dir_absent(self, temp_home):
        switcher = ClaudeAccountSwitcher()
        switcher.platform = Platform.WINDOWS
        # Do NOT create directories — fresh-install invariant.
        if switcher.backup_dir.exists():
            pytest.skip("host already materialized backup dir")
        fake = FakeKeyring()
        with _patch_keyring(fake):
            run_migrations(switcher)
        assert not switcher.backup_dir.exists()


# ---------------------------------------------------------------------------
# Windows now uses the file backend (no keyring) for normal read/write/delete
# ---------------------------------------------------------------------------


class TestWindowsFileBackend:
    def test_round_trip_uses_files_not_keyring(self, temp_home):
        switcher = _make_windows_switcher(temp_home)
        assert switcher._uses_file_backup_backend() is True

        with patch("claude_swap.switcher.keyring", create=True) as mock_keyring:
            switcher._write_account_credentials("1", "a@example.com", "secret")
            assert switcher._read_account_credentials("1", "a@example.com") == "secret"
            switcher._delete_account_credentials("1", "a@example.com")
            assert switcher._read_account_credentials("1", "a@example.com") == ""
            mock_keyring.assert_not_called()
            mock_keyring.get_password.assert_not_called()
            mock_keyring.set_password.assert_not_called()

        # The on-disk file lives under credentials/ as a base64 .enc file.
        cred_file = switcher.credentials_dir / ".creds-1-a@example.com.enc"
        assert not cred_file.exists()  # deleted above


# ---------------------------------------------------------------------------
# purge() best-effort legacy keyring cleanup on Windows
# ---------------------------------------------------------------------------


class TestPurgeWindows:
    def test_purge_removes_files_and_legacy_keyring(self, temp_home):
        switcher = _make_windows_switcher(temp_home)
        _seed_sequence(switcher, {"1": {"email": "a@example.com"}})
        switcher._write_account_credentials("1", "a@example.com", "secret")
        cred_file = switcher.credentials_dir / ".creds-1-a@example.com.enc"
        assert cred_file.exists()

        # purge()'s Windows branch lazily `import keyring`, so inject the fake via
        # sys.modules (patching switcher.keyring would not intercept it).
        fake = FakeKeyring()
        with _patch_keyring(fake), patch("builtins.input", return_value="y"):
            switcher.purge()

        # File backend cleaned up.
        assert not cred_file.exists()
        # Best-effort legacy Credential Manager cleanup attempted.
        assert (KEYRING_SERVICE, "account-1-a@example.com") in fake.deleted


# ---------------------------------------------------------------------------
# macOS keyring → security service migration
# ---------------------------------------------------------------------------


def _make_macos_switcher(temp_home: Path) -> ClaudeAccountSwitcher:
    """A switcher forced to MACOS, with dirs created. Construction runs migrations
    while no sequence exists yet (a no-op); the test seeds the sequence after."""
    switcher = ClaudeAccountSwitcher()
    switcher.platform = Platform.MACOS
    switcher._setup_directories()
    return switcher


class TestMacosKeyringToSecurity:
    """``migrate_macos_keyring_to_security``: relocate per-account backup creds
    from the legacy keyring service (``claude-code``) to the ``security``-managed
    service (``claude-swap``). The autouse guard fakes the security backend
    in-memory; tests inject a ``FakeKeyring`` for the source."""

    def test_non_macos_skips(self, temp_home):
        switcher = _make_macos_switcher(temp_home)
        switcher.platform = Platform.LINUX
        assert migrations.migrate_macos_keyring_to_security(switcher) is False

    def test_no_sequence_skips(self, temp_home):
        switcher = _make_macos_switcher(temp_home)
        switcher.sequence_file.unlink(missing_ok=True)
        assert migrations.migrate_macos_keyring_to_security(switcher) is False

    def test_empty_accounts_completes(self, temp_home):
        switcher = _make_macos_switcher(temp_home)
        _seed_sequence(switcher, {})
        assert migrations.migrate_macos_keyring_to_security(switcher) is True

    def test_relocates_keyring_creds_to_security(self, temp_home):
        switcher = _make_macos_switcher(temp_home)
        _seed_sequence(switcher, {"1": {"email": "a@example.com"}})
        fake = FakeKeyring({(KEYRING_SERVICE, "account-1-a@example.com"): "sekret"})

        with _patch_keyring(fake):
            assert migrations.migrate_macos_keyring_to_security(switcher) is True

        # Now readable from the new (security) service…
        assert switcher._read_account_credentials("1", "a@example.com") == "sekret"
        # …and the old keyring entry was deleted only after a verified write.
        assert (KEYRING_SERVICE, "account-1-a@example.com") in fake.deleted

    def test_denied_legacy_delete_warns_but_completes(
        self, temp_home, block_real_keychain
    ):
        """A denied legacy-entry delete leaves a harmless orphan: the migration
        still completes (the copy is verified), but the leftover is logged —
        keyring masks the denial as PasswordDeleteError, so without the
        item_exists check it would be invisible."""
        switcher = _make_macos_switcher(temp_home)
        switcher._logger = MagicMock()
        _seed_sequence(switcher, {"1": {"email": "a@example.com"}})
        username = "account-1-a@example.com"
        fake = FakeKeyring({(KEYRING_SERVICE, username): "sekret"})
        fake.deny_delete_for = {username}
        # In production keyring and `security` see the same Keychain; mirror the
        # legacy entry into the security fake so item_exists finds the leftover.
        block_real_keychain.set_password(KEYRING_SERVICE, username, "sekret")

        with _patch_keyring(fake):
            assert migrations.migrate_macos_keyring_to_security(switcher) is True

        # Copy succeeded and is authoritative; the orphan was logged.
        assert switcher._read_account_credentials("1", "a@example.com") == "sekret"
        warnings = " ".join(
            str(c.args[0]) for c in switcher._logger.warning.call_args_list
        )
        assert "left behind" in warnings and username in warnings

    def test_precheck_skips_keyring_when_already_migrated(self, temp_home):
        switcher = _make_macos_switcher(temp_home)
        _seed_sequence(switcher, {"1": {"email": "a@example.com"}})
        # Already present in the new security service.
        switcher._write_account_credentials("1", "a@example.com", "already")

        fake = FakeKeyring()  # would record any access
        with _patch_keyring(fake):
            assert migrations.migrate_macos_keyring_to_security(switcher) is True

        # Pre-check short-circuited before importing/using keyring at all.
        assert fake.get_calls == []
        assert fake.deleted == []

    def test_no_keyring_item_is_benign_skip(self, temp_home):
        switcher = _make_macos_switcher(temp_home)
        _seed_sequence(switcher, {"1": {"email": "a@example.com"}})
        fake = FakeKeyring()  # empty: get returns None for the account

        with _patch_keyring(fake):
            # Nothing to move, but not a failure → completes.
            assert migrations.migrate_macos_keyring_to_security(switcher) is True
        assert switcher._read_account_credentials("1", "a@example.com") == ""

    def test_read_back_mismatch_keeps_keyring_and_raises(self, temp_home):
        switcher = _make_macos_switcher(temp_home)
        _seed_sequence(switcher, {"1": {"email": "a@example.com"}})
        fake = FakeKeyring({(KEYRING_SERVICE, "account-1-a@example.com"): "sekret"})

        # Force the security read-back to disagree with what was written. The
        # migration reads the security service via the keychain-only helper
        # (_kc_read_backup), not the transparent .enc-wins backup methods.
        with _patch_keyring(fake), patch.object(
            switcher, "_kc_read_backup", side_effect=["", "WRONG"]
        ):
            with pytest.raises(MigrationIncomplete):
                migrations.migrate_macos_keyring_to_security(switcher)

        # Keyring entry left intact (not deleted) for the retry.
        assert (KEYRING_SERVICE, "account-1-a@example.com") not in fake.deleted

    def test_fallback_to_security_when_keyring_unavailable(
        self, temp_home, block_real_keychain
    ):
        switcher = _make_macos_switcher(temp_home)
        _seed_sequence(switcher, {"1": {"email": "a@example.com"}})
        # Old item lives in the Keychain under the legacy service (here: the
        # in-memory security store seeded directly).
        block_real_keychain.data[(KEYRING_SERVICE, "account-1-a@example.com")] = "sekret"

        # `import keyring` fails → migration reads old items via the security CLI.
        with patch.dict(sys.modules, {"keyring": None}):
            assert migrations.migrate_macos_keyring_to_security(switcher) is True

        assert switcher._read_account_credentials("1", "a@example.com") == "sekret"
        # Data is safely in the new service; the legacy item is deliberately LEFT
        # behind in the keyring-unavailable fallback (deleting via security could
        # raise a second prompt). It's harmless cruft that purge can mop up.
        assert (KEYRING_SERVICE, "account-1-a@example.com") in block_real_keychain.data

    def test_locked_keyring_does_not_fall_back(self, temp_home, block_real_keychain):
        switcher = _make_macos_switcher(temp_home)
        _seed_sequence(switcher, {"1": {"email": "a@example.com"}})
        # The same item exists in the (security) Keychain store…
        block_real_keychain.data[(KEYRING_SERVICE, "account-1-a@example.com")] = "sekret"
        # …but keyring raises a *runtime* error (locked/denied), which security
        # would hit identically → must be a failure, not a security fallback.
        fake = FakeKeyring()
        fake.raise_get_for.add("account-1-a@example.com")

        with _patch_keyring(fake):
            with pytest.raises(MigrationIncomplete):
                migrations.migrate_macos_keyring_to_security(switcher)

        # No fallback read happened → nothing landed in the new service.
        assert switcher._read_account_credentials("1", "a@example.com") == ""

    def test_security_keychain_unusable_defers_and_writes_no_enc(self, temp_home):
        # The destination (security-service) Keychain is unusable: the keychain-only
        # pending pre-check raises, so the migration must defer rather than mistake
        # an .enc for "already migrated" or write legacy creds to a file while
        # claiming the security service.
        switcher = _make_macos_switcher(temp_home)
        _seed_sequence(switcher, {"1": {"email": "a@example.com"}})

        with patch.object(switcher, "_kc_read_backup", side_effect=KeychainError("locked")):
            run_migrations(switcher)  # swallows MigrationIncomplete → left unmarked

        # Deferred cleanly: no fallback .enc written, migration not recorded.
        assert not (switcher.credentials_dir / ".creds-1-a@example.com.enc").exists()
        state_file = switcher.backup_dir / ".migrations.json"
        if state_file.exists():
            applied = json.loads(state_file.read_text()).get("applied", {})
            assert "macos_keyring_to_security" not in applied

    def test_idempotent_via_runner_marks_applied(self, temp_home):
        switcher = _make_macos_switcher(temp_home)
        _seed_sequence(switcher, {"1": {"email": "a@example.com"}})
        fake = FakeKeyring({(KEYRING_SERVICE, "account-1-a@example.com"): "sekret"})

        with _patch_keyring(fake):
            run_migrations(switcher)
        state = json.loads((switcher.backup_dir / ".migrations.json").read_text())
        assert "macos_keyring_to_security" in state["applied"]
