"""Tests for `cswap swap` (ClaudeAccountSwitcher.swap_accounts)."""

import contextlib
import errno
import copy
import os
import sys
from pathlib import Path

import pytest

from claude_swap.credentials import CredentialStore
from claude_swap.exceptions import (
    AccountNotFoundError,
    ConfigError,
    CredentialError,
    ValidationError,
)
from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher


def _refuse_write(self, num, email, creds):
    raise OSError("disk full (injected)")



@contextlib.contextmanager
def caplog_at_error():
    """Collect ERROR records from the switcher logger as plain strings."""
    import logging

    seen: list[str] = []

    class _Sink(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    logger = logging.getLogger("claude-swap")
    h = _Sink(level=logging.ERROR)
    logger.addHandler(h)
    try:
        yield seen
    finally:
        logger.removeHandler(h)


def _two_slots_one_address(s, data, e1, e2):
    s._setup_directories()
    d = dict(data)
    d["accounts"] = {
        "1": {"email": e1, "uuid": "u1", "organizationUuid": "", "organizationName": "",
              "added": "2024-01-01T00:00:00Z"},
        "2": {"email": e2, "uuid": "u2", "organizationUuid": "", "organizationName": "",
              "added": "2024-01-01T00:00:00Z"}}
    d["sequence"] = [1, 2]
    s._write_json(s.sequence_file, d)
    s._write_account_credentials("1", e1, "creds-A")
    s._write_account_credentials("2", e2, "creds-B")
    for num, email, tag in (("1", e1, "A"), ("2", e2, "B")):
        p = s._session_dir(num, email)
        p.mkdir(parents=True, exist_ok=True)
        (p / "marker").write_text(f"{tag}-HISTORY")
    return s


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
        self, temp_home: Path, sample_sequence_data_with_org: dict
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

        # Scoped context, not the fixture's shared `monkeypatch`: that
        # instance also carries the autouse colour/keychain/home scrubs, and
        # `.undo()` on it would unwind those too (H-1) — restoring whatever
        # FORCE_COLOR/NO_COLOR the developer's shell actually has exported
        # for the rest of this test.
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                ClaudeAccountSwitcher, "_write_account_credentials", failing_write
            )
            with pytest.raises(OSError):
                switcher.swap_accounts("1", "2")

        # Both originals are back under their pre-swap keys, and the account
        # table was never renumbered.
        assert switcher._read_account_credentials("1", email) == "creds-org"
        assert switcher._read_account_credentials("2", email) == "creds-personal"
        data = switcher._get_sequence_data()
        assert data["accounts"]["1"]["organizationUuid"] == "org-uuid-5678"
        assert data["activeAccountNumber"] == 1

    def _same_email_slots(self, switcher, data, profiles=("1", "2")) -> str:
        """Two slots sharing one email; each named slot gets a marked profile."""
        self._write(switcher, data)
        email = "user@example.com"
        switcher._write_account_credentials("1", email, "creds-org")
        switcher._write_account_credentials("2", email, "creds-personal")
        for num in profiles:
            profile = switcher._session_dir(num, email)
            profile.mkdir(parents=True, exist_ok=True)
            (profile / "marker").write_text(f"SLOT-{num}-HISTORY")
        return email

    def _marker(self, switcher, num: str, email: str) -> str:
        return (switcher._session_dir(num, email) / "marker").read_text()

    def test_a_rollback_restore_does_not_strip_the_home_profile(
        self, temp_home: Path, sample_sequence_data_with_org: dict
    ):
        """A restore puts back the value the profile was bootstrapped FROM.

        It can only make the backup older than the profile, never newer, so
        invalidating on it is a strict downgrade. With one shared email the
        forward writes displace the very keys the restores put back, so the
        value-equal skip cannot mask it.
        """
        from unittest.mock import patch

        switcher = ClaudeAccountSwitcher()
        email = self._same_email_slots(switcher, sample_sequence_data_with_org)
        for num in ("1", "2"):
            (switcher._session_dir(num, email) / ".credentials.json").write_text(
                f"profile-{num}-G1", encoding="utf-8")

        # Fail the ROSTER write: both forward credential writes have landed
        # by then, so the rollback restores two keys it really displaced.
        real_json = ClaudeAccountSwitcher._write_json
        calls = {"n": 0}

        def failing_json(self, path, data):
            if path == self.sequence_file:
                calls["n"] += 1
                raise OSError("disk full (injected)")
            return real_json(self, path, data)

        with patch.object(ClaudeAccountSwitcher, "_write_json", failing_json):
            with pytest.raises(Exception):
                switcher.swap_accounts("1", "2")

        # PREMISES: the failing step ran and the rollback really put both
        # keys back, so what follows is about the RESTORE.
        assert calls["n"] >= 1, "premise: the roster write must have failed"
        assert switcher.read_account_credentials("1", email) == "creds-org"
        assert switcher.read_account_credentials("2", email) == "creds-personal"

        for num in ("1", "2"):
            seed = switcher._session_dir(num, email) / ".credentials.json"
            assert seed.exists(), (
                f"DEFECT: the rollback stripped slot {num}'s home profile "
                "after restoring the key to the value that profile already "
                "descended from. The backup holds the consumed generation, "
                "so both accounts need a re-login after a swap that changed "
                "nothing"
            )
            assert seed.read_text(encoding="utf-8") == f"profile-{num}-G1"

    def test_swap_aborted_in_staging_touches_neither_slot(
        self, temp_home: Path, sample_sequence_data_with_org: dict
    ):
        """An abort before anything is mutated must leave both slots alone.

        Staging is the last step that can fail with nothing yet written, so it
        runs outside the rollback's reach: a reverse move would exchange two
        untouched profiles, and a credential restore would invalidate two live
        session profiles — both while the error says nothing was changed.
        """
        switcher = ClaudeAccountSwitcher()
        email = self._same_email_slots(switcher, sample_sequence_data_with_org)
        for num in ("1", "2"):
            (switcher._session_dir(num, email) / ".credentials.json").write_text("{}")

        # A leftover staging file is what makes staging refuse.
        (switcher.credentials_dir / ".swap-staging-creds-1.json").write_text("{}")

        with pytest.raises(ConfigError):
            switcher.swap_accounts("1", "2")

        assert self._marker(switcher, "1", email) == "SLOT-1-HISTORY"
        assert self._marker(switcher, "2", email) == "SLOT-2-HISTORY"
        for num in ("1", "2"):
            assert (
                switcher._session_dir(num, email) / ".credentials.json"
            ).exists(), f"slot {num}'s session credentials were invalidated"

    def test_swap_failing_after_the_move_still_restores_the_session_dirs(
        self, temp_home: Path, sample_sequence_data_with_org: dict
    ):
        """A failure after the forward move must still reverse it, or the gate
        on that reverse becomes a way to skip the rollback."""
        switcher = ClaudeAccountSwitcher()
        email = self._same_email_slots(switcher, sample_sequence_data_with_org)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                ClaudeAccountSwitcher, "_write_account_credentials", _refuse_write
            )
            with pytest.raises(OSError):
                switcher.swap_accounts("1", "2")

        assert self._marker(switcher, "1", email) == "SLOT-1-HISTORY"
        assert self._marker(switcher, "2", email) == "SLOT-2-HISTORY"

    def test_swap_does_not_reverse_a_forward_move_that_moved_nothing(
        self, temp_home: Path, sample_sequence_data_with_org: dict
    ):
        """``_swap_session_dirs`` swallows OSError, so reaching the end of it
        is not evidence that anything moved.

        A forward move that gave up leaves both profiles where they started,
        and reversing that exchanges two directories nobody touched.
        """
        switcher = ClaudeAccountSwitcher()
        email = self._same_email_slots(switcher, sample_sequence_data_with_org)

        real_replace = os.replace
        tripped: list[str] = []

        def fail_the_first_park(src, dst):
            # The forward move only; the rollback's reverse must run for real.
            if not tripped and str(dst).endswith(".swapping"):
                tripped.append(str(dst))
                raise OSError("cross-device link (injected)")
            return real_replace(src, dst)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(os, "replace", fail_the_first_park)
            mp.setattr(
                ClaudeAccountSwitcher, "_write_account_credentials", _refuse_write
            )
            with pytest.raises(OSError):
                switcher.swap_accounts("1", "2")

        assert tripped, "the injected move failure never fired"
        assert self._marker(switcher, "1", email) == "SLOT-1-HISTORY"
        assert self._marker(switcher, "2", email) == "SLOT-2-HISTORY"

    def test_swap_reverses_a_forward_move_that_was_interrupted(
        self, temp_home: Path, sample_sequence_data_with_org: dict
    ):
        """A move aborted part-way through still has to be undone.

        ``swap_accounts`` catches BaseException, so a Ctrl-C between the two
        halves of the exchange reaches the rollback with one profile already
        parked under the other slot's key.
        """
        switcher = ClaudeAccountSwitcher()
        email = self._same_email_slots(switcher, sample_sequence_data_with_org)

        real_replace = os.replace

        def interrupt_the_last_park(src, dst):
            if str(src).endswith(".swapping"):
                raise KeyboardInterrupt
            return real_replace(src, dst)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(os, "replace", interrupt_the_last_park)
            with pytest.raises(KeyboardInterrupt):
                switcher.swap_accounts("1", "2")

        slot_2 = switcher._session_dir("2", email) / "marker"
        assert slot_2.exists(), "account 2's profile was left under slot 1's key"
        assert slot_2.read_text() == "SLOT-2-HISTORY"

    def test_swap_interrupted_just_past_a_move_still_reverses_it(
        self, temp_home: Path, sample_sequence_data_with_org: dict
    ):
        """An abort landing just PAST a rename must still undo that rename.

        The rename and the record of it are two statements, and a signal is
        delivered between them. Recording after the call returns therefore
        loses a move that is already on disk, and the slot then serves the
        other account's session history while the swap reports it aborted.
        """
        switcher = ClaudeAccountSwitcher()
        email = self._same_email_slots(switcher, sample_sequence_data_with_org)

        real_replace = os.replace
        landed: list[str] = []

        def interrupt_just_past_the_move(src, dst):
            # One shot: the rollback's own reverse must run for real.
            real_replace(src, dst)
            if not landed and not str(dst).endswith(".swapping"):
                landed.append(str(dst))
                raise KeyboardInterrupt

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(os, "replace", interrupt_just_past_the_move)
            with pytest.raises(KeyboardInterrupt):
                switcher.swap_accounts("1", "2")

        assert landed, "the injected interrupt never fired"
        slot_2 = switcher._session_dir("2", email) / "marker"
        assert slot_2.exists(), "account 2's profile was left under slot 1's key"
        assert slot_2.read_text() == "SLOT-2-HISTORY"

    def test_swap_interrupted_before_the_first_move_is_not_reversed(
        self, temp_home: Path, sample_sequence_data_with_org: dict
    ):
        """Ctrl-C landing before the first rename must not reverse anything.

        Counterpart to the test above: the two together say the move has to
        report progress as it goes, since neither "assume it ran" nor "assume
        it did not" is right for an abort that can land on either side.
        """
        switcher = ClaudeAccountSwitcher()
        email = self._same_email_slots(switcher, sample_sequence_data_with_org)
        for num in ("1", "2"):
            (switcher._session_dir(num, email) / ".credentials.json").write_text("{}")

        real_replace = os.replace
        tripped: list[str] = []

        def interrupt_the_first_park(src, dst):
            if not tripped and str(dst).endswith(".swapping"):
                tripped.append(str(dst))
                raise KeyboardInterrupt
            return real_replace(src, dst)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(os, "replace", interrupt_the_first_park)
            with pytest.raises(KeyboardInterrupt):
                switcher.swap_accounts("1", "2")

        assert tripped, "the injected interrupt never fired"
        assert self._marker(switcher, "1", email) == "SLOT-1-HISTORY"
        assert self._marker(switcher, "2", email) == "SLOT-2-HISTORY"
        alive = {
            num: (switcher._session_dir(num, email) / ".credentials.json").exists()
            for num in ("1", "2")
        }
        assert alive == {"1": True, "2": True}, f"session creds destroyed: {alive}"

    def test_swap_records_the_move_when_only_one_slot_has_a_profile(
        self, temp_home: Path, sample_sequence_data_with_org: dict
    ):
        """The last rename needs its own record, and only this shape shows it.

        With a profile in both slots the earlier rename already fills the sink,
        so losing the record of the last one is invisible. With one profile it
        is the only rename there is.
        """
        switcher = ClaudeAccountSwitcher()
        email = self._same_email_slots(
            switcher, sample_sequence_data_with_org, profiles=("1",)
        )

        real_replace = os.replace
        landed: list[str] = []

        def interrupt_just_past_the_last_park(src, dst):
            # One shot: the rollback's own reverse must run for real.
            real_replace(src, dst)
            if not landed and str(src).endswith(".swapping"):
                landed.append(str(dst))
                raise KeyboardInterrupt

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(os, "replace", interrupt_just_past_the_last_park)
            with pytest.raises(KeyboardInterrupt):
                switcher.swap_accounts("1", "2")

        assert landed, "the injected interrupt never fired"
        slot_1 = switcher._session_dir("1", email) / "marker"
        assert slot_1.exists(), "account 1's profile was left under slot 2's key"
        assert slot_1.read_text() == "SLOT-1-HISTORY"
        assert not (switcher._session_dir("2", email) / "marker").exists()

    def test_swap_of_two_emails_still_reverses_the_move_on_failure(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """The gate exists for the same-email case, but it guards both.

        With two emails the four session-directory keys are distinct, so a
        reverse that never runs is silent: the profiles just stay under the
        keys the aborted swap handed them.
        """
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)
        emails = {
            num: sample_sequence_data["accounts"][num]["email"] for num in ("1", "2")
        }
        for num, email in emails.items():
            switcher._write_account_credentials(num, email, f"creds-{num}")
            profile = switcher._session_dir(num, email)
            profile.mkdir(parents=True, exist_ok=True)
            (profile / "marker").write_text(f"SLOT-{num}-HISTORY")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                ClaudeAccountSwitcher, "_write_account_credentials", _refuse_write
            )
            with pytest.raises(OSError):
                switcher.swap_accounts("1", "2")

        for num, email in emails.items():
            marker = switcher._session_dir(num, email) / "marker"
            assert marker.exists(), f"slot {num}'s profile was not moved back"
            assert marker.read_text() == f"SLOT-{num}-HISTORY"

    def test_swap_same_email_persistent_failure_keeps_staged_copy(
        self, temp_home: Path, sample_sequence_data_with_org: dict
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

        # Scoped context: see H-1 comment above.
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                ClaudeAccountSwitcher, "_write_account_credentials", failing_write
            )
            with pytest.raises(OSError):
                switcher.swap_accounts("1", "2")

        # Slot 1's stored copy was never touched; slot 2's store now holds
        # the wrong material (restore failed), but the staged copy has it.
        assert switcher._read_account_credentials("1", email) == "creds-org"
        staged = switcher.credentials_dir / ".swap-staging-creds-2.json"
        assert staged.read_text(encoding="utf-8") == "creds-personal"
        if sys.platform != "win32":
            assert staged.stat().st_mode & 0o777 == 0o600

    def test_swap_same_email_rollback_restores_empty_slot(
        self, temp_home: Path, sample_sequence_data_with_org: dict
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

        # Scoped context: see H-1 comment above.
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(ClaudeAccountSwitcher, "_write_json", failing_write_json)
            with pytest.raises(OSError):
                switcher.swap_accounts("1", "2")

        assert switcher._read_account_credentials("1", email) == "creds-org"
        assert switcher._read_account_credentials("2", email) == ""
        data = switcher._get_sequence_data()
        assert data["accounts"]["1"]["organizationUuid"] == "org-uuid-5678"
        # Clean rollback: no staged copies left behind either.
        assert not list(switcher.credentials_dir.glob(".swap-staging-*"))

    def test_write_json_publishes_only_after_chmod(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """chmod runs on the temp file, making the rename the final commit —
        a chmod failure must abort *without* publishing, otherwise callers
        would roll files back around already-committed metadata."""
        if sys.platform == "win32":
            pytest.skip("_write_json skips chmod on Windows (no POSIX file modes)")
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)
        before = switcher.sequence_file.read_text(encoding="utf-8")

        def failing_chmod(path, mode):
            raise OSError("chmod denied (injected)")

        # Scoped context: see H-1 comment above.
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("claude_swap.switcher.os.chmod", failing_chmod)
            with pytest.raises(OSError):
                switcher._write_json(switcher.sequence_file, {"x": 1})

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
        self, temp_home: Path, sample_sequence_data_with_org: dict
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

        # Scoped context: see H-1 comment above.
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(Path, "unlink", failing_unlink)
            with pytest.raises(CredentialError, match="aborting before commit"):
                switcher.swap_accounts("1", "2")

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

        # Through the STORE, not a `credentials_dir` glob: on macOS a usable
        # Keychain takes the write, so no `.enc.prev` file exists and the glob
        # is empty whether or not the purge ran.
        for num in ("1", "2"):
            assert switcher._store._read_previous_backup(num, email) == "", (
                f"the swap left the displaced credential as key {num}'s .prev "
                "-- recovery would resurrect it onto the slot's new owner"
            )

    def test_swap_clears_the_prev_under_the_DESTINATION_key(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """DISTINCT emails, which the same-email sibling cannot express.

        With one email the destination keys and the source keys are the same
        two pairs, so that case passes whichever pair the purge names. Here
        they differ: the writes retain their .prev under (2, ea) and (1, eb),
        and naming the source keys instead leaves another account's
        credential as the recovery generation of a key whose owner just
        changed.
        """
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)
        ea, eb = "account1@example.com", "account2@example.com"
        switcher._write_account_credentials("1", ea, "rt-a")
        switcher._write_account_credentials("2", eb, "rt-b")
        # A stale value under EACH destination key, which is the state the
        # purge's own comment says recovery must never reach. Both, because
        # a key with nothing under it retains no .prev, so asserting on one
        # of those says the same thing whatever the purge names.
        switcher._store._write_account_credentials("2", ea, "stale-foreign-a")
        switcher._store._write_account_credentials("1", eb, "stale-foreign-b")
        for num, email in (("2", ea), ("1", eb)):
            assert switcher._store._read_previous_backup(num, email) == "", (
                f"premise: destination key ({num}, {email}) has no .prev "
                "before the swap, so anything found after it was retained "
                "BY the swap"
            )

        switcher.swap_accounts("1", "2")

        assert switcher._store._read_previous_backup("2", ea) == "", (
            "DEFECT: the swap left another account's credential as the .prev "
            "of a key whose owner changed — recovery would resurrect it"
        )
        assert switcher._store._read_previous_backup("1", eb) == "", (
            "DEFECT: the same, on the other destination key"
        )

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


class TestSwapUnreadableSourceIsNotAbsent:
    """Same defect family as C1/C2/move: the plain reader's ``""`` means both
    "no backup" and "the backup exists but could not be read right now".

    The pre-swap read (:1063-1064) used the plain reader — a permission
    glitch on either slot's ``.enc`` read as "no backup", and the swap
    committed BOTH destination keys from that snapshot: the unreadable
    slot's live refresh token would be silently dropped and replaced with
    an empty credential at its new number. Fixed with
    ``_read_account_credentials_ex``, aborting BEFORE anything moves.
    """

    @pytest.fixture(autouse=True)
    def _file_mode(self, monkeypatch):
        """Force the FILE store: these cases read the file backend, and on
        macOS a usable Keychain takes the write instead -- no file to `chmod`,
        and the retained generation lands where `_prev_backup_path` cannot
        see it. Class-wide and autouse, so membership alone grants it.
        """
        monkeypatch.setattr(CredentialStore, "_use_keychain", lambda self: False)

    def _write(self, switcher, data):
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, data)

    @pytest.mark.skipif(
        sys.platform == "win32" or os.geteuid() == 0,
        reason="needs POSIX permission semantics (non-root)",
    )
    # THE MACOS ARM MAKES THE SHAPE REACHABLE ON THE UBUNTU JOB, which is
    # where it adds anything -- and the class fixture that forces the file
    # store is what makes it so. `_use_keychain` keys on the SWITCHER's
    # platform, not on the real OS, so this arm turns it True on Ubuntu too;
    # the `[None]` arm is already file-mode there and the fixture is inert,
    # but on this arm -- and on the whole macOS job -- the fixture is what
    # keeps the store on file so there is an `.enc` to chmod. Windows skips
    # this case entirely (the `skipif` above), and on macOS
    # `Platform.detect()` already answers MACOS, so the arm duplicates
    # `[None]` there.
    @pytest.mark.parametrize("as_platform", [None, Platform.MACOS])
    def test_unreadable_enc_aborts_the_swap_before_anything_changes(
        self, temp_home: Path, sample_sequence_data: dict, as_platform
    ):
        switcher = ClaudeAccountSwitcher()
        if as_platform is not None:
            switcher.platform = as_platform
        self._write(switcher, sample_sequence_data)
        switcher._write_account_credentials("1", "account1@example.com", "rt-1")
        switcher._write_account_credentials("2", "account2@example.com", "rt-2")

        # CONTROL: both readable, the swap lands cleanly (instrument says YES).
        switcher.swap_accounts("1", "2")
        assert (
            switcher._read_account_credentials("2", "account1@example.com")
            == "rt-1"
        )
        assert (
            switcher._read_account_credentials("1", "account2@example.com")
            == "rt-2"
        )
        # Swap back to the original layout for the probe below.
        switcher.swap_accounts("1", "2")

        enc = switcher._backup_enc_path("2", "account2@example.com")
        enc.chmod(0o000)
        try:
            with pytest.raises(ConfigError, match="could not be read"):
                switcher.swap_accounts("1", "2")
        finally:
            if enc.exists():
                enc.chmod(0o600)

        # Nothing committed: both accounts intact under their original
        # numbers, account 2 still holding its readable credential.
        data = switcher._get_sequence_data()
        assert data["accounts"]["1"]["email"] == "account1@example.com"
        assert data["accounts"]["2"]["email"] == "account2@example.com"
        assert (
            switcher._read_account_credentials("1", "account1@example.com")
            == "rt-1"
        )
        assert (
            switcher._read_account_credentials("2", "account2@example.com")
            == "rt-2"
        )

    def test_absent_source_still_swaps(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """Control in the other direction: genuinely unbacked slots (no
        .enc at all) are not mistaken for unreadable and still swap."""
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)

        num_a, num_b = switcher.swap_accounts("1", "2")

        assert (num_a, num_b) == ("1", "2")
        data = switcher._get_sequence_data()
        assert data["accounts"]["1"]["email"] == "account2@example.com"
        assert data["accounts"]["2"]["email"] == "account1@example.com"

    def test_a_rollback_keeps_prev_generations_it_never_contaminated(
        self, temp_home: Path, sample_sequence_data_with_org: dict
    ):
        """The purge is only correct when the two slots share one email.

        The forward pass writes `(num_b, email_a)` and `(num_a, email_b)`; the
        purge deletes `.prev` for `(num_a, email_a)` and `(num_b, email_b)`.
        With two emails those key sets are DISJOINT, so the purge destroys a
        generation the swap never touched — and the stored credentials are
        still correct afterwards, so nothing signals the loss.
        """
        switcher = ClaudeAccountSwitcher()
        # TWO EMAILS, which is the whole case: the shared-email fixture is the
        # one shape where the purge's key set and the forward pass's coincide.
        data = copy.deepcopy(sample_sequence_data_with_org)
        emails = {"1": "account1@example.com", "2": "account2@example.com"}
        for num, email in emails.items():
            data["accounts"][num]["email"] = email
            data["accounts"][num]["uuid"] = f"uuid-{num}"
        self._write(switcher, data)
        for num, email in emails.items():
            switcher._write_account_credentials(num, email, f"gen1-{num}")
            switcher._write_account_credentials(num, email, f"gen2-{num}")

        prev = {
            num: switcher._store._prev_backup_path(num, email)
            for num, email in emails.items()
        }
        assert all(p.exists() for p in prev.values()), (
            "the fixture never produced a .prev, so this test would pass "
            "however the purge behaves"
        )

        # THE CONTROL. Without it a green result cannot separate "the purge
        # spared them" from "the rollback never reached the purge".
        purged: list[tuple[str, str]] = []
        real_purge = switcher._store.delete_previous_backup
        switcher._store.delete_previous_backup = (
            lambda n, e: (purged.append((n, e)), real_purge(n, e))[1])

        def failing_write(*_a, **_kw):
            raise ConfigError("commit failed")

        original = switcher._write_json
        switcher._write_json = failing_write
        try:
            with pytest.raises(ConfigError):
                switcher.swap_accounts("1", "2")
        finally:
            switcher._write_json = original
            switcher._store.delete_previous_backup = real_purge
        assert purged, (
            "the rollback never reached the purge, so this case measures "
            "nothing about it"
        )

        alive = {num: p.exists() for num, p in prev.items()}
        assert alive == {"1": True, "2": True}, (
            f"the rollback purged .prev for slots the swap never wrote "
            f"through: {alive}; purge calls were {purged}"
        )

    def test_a_staging_abort_never_enters_the_rollback(
        self, temp_home: Path, sample_sequence_data_with_org: dict
    ):
        """The hoist's own property, which its two observables do not pin.

        Staging sits outside the `try` so an abort there cannot reach the
        rollback at all. The existing case asserts untouched markers and live
        session credentials, but the `moved` and `wrote_backups` gates produce
        both of those independently — measured, moving the staging block back
        INSIDE the `try` leaves the whole suite green. What only the hoist
        gives is that the rollback is never entered, so that is what this
        asserts.
        """
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data_with_org)
        email = "user@example.com"
        switcher._write_account_credentials("1", email, "creds-org")
        switcher._write_account_credentials("2", email, "creds-personal")
        # A leftover staging file is what makes staging refuse.
        (switcher.credentials_dir / ".swap-staging-creds-1.json").write_text("{}")

        entered: list[bool] = []
        real = switcher._rollback_swap
        switcher._rollback_swap = lambda *a, **kw: (
            entered.append(True), real(*a, **kw))[1]
        try:
            with pytest.raises(ConfigError):
                switcher.swap_accounts("1", "2")
        finally:
            switcher._rollback_swap = real

        assert entered == [], (
            "a staging abort reached the rollback — it reverses profiles and "
            "rewrites both slots' credentials for a failure that mutated "
            "nothing"
        )

    def test_a_rollback_that_restores_nothing_does_not_say_it_restored(
        self, temp_home: Path, sample_sequence_data_with_org: dict
    ):
        """The opening line is emitted before anything is decided.

        With `wrote_backups=False` and no moves, every step below it is
        skipped — the reverse, the restores, the cleanup and the purge — and
        the log still reads "restoring both slots". This PR opens by listing
        three pieces of text that told the user the opposite of what happened.
        """
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data_with_org)
        email = "user@example.com"
        switcher._write_account_credentials("1", email, "creds-org")
        switcher._write_account_credentials("2", email, "creds-personal")

        with caplog_at_error() as records:
            switcher._rollback_swap(
                "1", email, "creds-org", "{}",
                "2", email, "creds-personal", "{}",
                staging={}, moved=[], wrote_backups=False,
            )
        said = " ".join(records)
        assert "restoring both slots" not in said, (
            f"it announced a restore it then skipped entirely: {said!r}"
        )
        assert said, "nothing was logged at all — the rollback went silent"

    def test_a_rollback_keeps_prev_when_no_forward_write_landed(
        self, temp_home: Path, sample_sequence_data_with_org: dict
    ):
        """SAME email, failure on the FIRST forward write. The `overlap` gate
        does not reach this one.

        `wrote_backups` is armed one statement BEFORE the first write, and the
        comment prices that at "one needless restore". The restore is a no-op
        by construction — `_retain_previous_backup` short-circuits when the
        value it would displace equals the one going in — so no `.prev` is
        created, the purge's premise ("the restore writes pushed the
        half-written material into the retained generations") is false, and it
        deletes the generation that was there before the swap began.

        The condition that separates it from a legitimate purge is not the
        emails: it is whether a restore DISPLACED anything.
        """
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data_with_org)
        email = "user@example.com"
        for num in ("1", "2"):
            switcher._write_account_credentials(num, email, f"gen1-{num}")
            switcher._write_account_credentials(num, email, f"gen2-{num}")

        prev = {n: switcher._store._prev_backup_path(n, email) for n in ("1", "2")}
        assert all(p.exists() for p in prev.values()), (
            "the fixture produced no .prev, so this would pass however the "
            "purge behaves"
        )

        calls = {"n": 0}
        real_write = switcher._write_account_credentials

        def fail_first(num, mail, creds):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError(errno.EIO, "the first forward write failed")
            return real_write(num, mail, creds)

        switcher._write_account_credentials = fail_first
        try:
            # OSError, not ConfigError: the forward write is not wrapped, and
            # the rollback runs on the way out either way.
            with pytest.raises(OSError):
                switcher.swap_accounts("1", "2")
        finally:
            del switcher._write_account_credentials

        alive = {n: p.exists() for n, p in prev.items()}
        assert alive == {"1": True, "2": True}, (
            f"the rollback purged .prev after a swap that wrote nothing: {alive}"
        )

    def test_a_profile_only_rollback_does_not_say_it_restored_the_slots(
        self, temp_home: Path, sample_sequence_data_with_org: dict, caplog
    ):
        """The announcement had three states and only said two.

        `bool(moved) or wrote_backups` reads as "restoring both slots" when a
        rename landed and no credential write ever ran -- the fourth instance
        of the class this change opens by naming. The existing case covers
        only the `(False, [])` corner, where the text is already right.
        """
        import logging

        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data_with_org)

        def half_moved(a, ea, b, eb, moved):
            moved.append(temp_home / "one-profile-already-renamed")
            raise OSError(errno.EIO, "interrupted just past a rename")

        switcher._swap_session_dirs = half_moved
        try:
            with caplog.at_level(logging.ERROR, logger="claude-swap"):
                with pytest.raises(OSError):
                    switcher.swap_accounts("1", "2")
        finally:
            del switcher._swap_session_dirs

        # "failed; rolling back", not "failed mid-write": the `try` this unwinds
        # from BEGINS with the profile move, so a signal there wrote nothing
        # and "mid-write" was itself the class of wrong text this case guards.
        said = [r.message for r in caplog.records if "rolling back" in r.message]
        assert said, "premise: the rollback never announced anything"
        assert "restoring both slots" not in said[0], (
            f"no credential write ran, and the line says otherwise: {said[0]!r}"
        )

    def test_a_swap_that_stored_nothing_keeps_both_session_profiles(
        self, temp_home: Path, sample_sequence_data_with_org: dict
    ):
        """A restore that changes no value still invalidated a live profile.

        `wrote_backups` is armed one statement before the first write on
        purpose -- arming it after would let an abort skip a restore that was
        owed -- and the comment prices the gap at "one needless restore". It
        is not one and it is not free: all four restores run, each credential
        restore routes through `_post_backup_write`, and both slots lose their
        session credential material for a swap where zero writes landed. That
        is the same harm `..._touches_neither_slot` asserts against.

        The restore is a no-op by construction here: the key still holds
        exactly what the restore would write. A write that changes nothing
        should not cost a re-bootstrap.
        """
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data_with_org)
        email = "user@example.com"
        for num in ("1", "2"):
            switcher._write_account_credentials(num, email, f"gen1-{num}")

        seeded = {}
        for num in ("1", "2"):
            d = switcher._session_dir(num, email)
            d.mkdir(parents=True, exist_ok=True)
            f = d / ".credentials.json"
            f.write_text('{"claudeAiOauth": {"accessToken": "session-tok"}}')
            seeded[num] = f
        assert all(f.exists() for f in seeded.values()), "premise: nothing seeded"

        calls = {"n": 0}
        real_write = switcher._write_account_credentials

        def fail_first(num, mail, creds):
            # ONLY THE FORWARD WRITE. Replacing the method outright would
            # intercept the four RESTORE writes too, which is the behaviour
            # under test.
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError(errno.EIO, "the first forward write failed")
            return real_write(num, mail, creds)

        switcher._write_account_credentials = fail_first
        try:
            with pytest.raises(OSError):
                switcher.swap_accounts("1", "2")
        finally:
            del switcher._write_account_credentials

        gone = [n for n, f in seeded.items() if not f.exists()]
        assert gone == [], (
            f"slots {gone} lost their session credentials to a swap that "
            "stored nothing"
        )

    def test_one_reader_decides_whether_a_restore_displaced_anything(
        self, temp_home: Path, sample_sequence_data_with_org: dict
    ):
        """Two reads of one value can disagree, and the disagreement strands.

        The purge used to probe the stored credential itself and then let
        `_retain_previous_backup` read it again a moment later. Deny only the
        probe -- an unreadable Keychain that clears between the two calls is
        the ordinary way -- and `displaced` stays empty while a `.prev` IS
        written. Nothing then purges a retained generation that holds the
        other slot's half-written material, which is the state
        `delete_previous_backup` exists to remove.

        One email, so the restore keys and the forward keys coincide and the
        retained generation really is contamination; failure at the COMMIT, so
        all four forward writes landed. The store's retention verdict is the
        only reader now, so there is no second answer to disagree with.
        """
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data_with_org)
        email = "user@example.com"
        for num in ("1", "2"):
            switcher._write_account_credentials(num, email, f"gen1-{num}")
            switcher._write_account_credentials(num, email, f"gen2-{num}")
            switcher._store.delete_previous_backup(num, email)
        prev = {n: switcher._store._prev_backup_path(n, email) for n in ("1", "2")}
        assert not any(p.exists() for p in prev.values()), (
            "premise: a .prev already exists, so a survivor below proves nothing"
        )

        real_read = switcher._store._read_account_credentials_ex
        rolling_back = {"yet": False}
        seen: set[tuple[str, str]] = set()

        def first_rollback_read_is_unreadable(num, mail, *a, **kw):
            # ONLY DURING THE ROLLBACK. The forward pass reads the same keys to
            # find the material it is moving; denying those aborts the swap
            # before it reaches the state this case is about.
            if rolling_back["yet"] and (num, mail) not in seen:
                seen.add((num, mail))
                return "", True
            return real_read(num, mail, *a, **kw)

        def refuse_commit(path, data):
            rolling_back["yet"] = True
            raise OSError(errno.EIO, "the commit failed")

        switcher._store._read_account_credentials_ex = first_rollback_read_is_unreadable
        switcher._write_json = refuse_commit
        try:
            with pytest.raises(OSError):
                switcher.swap_accounts("1", "2")
        finally:
            del switcher._write_json
            del switcher._store._read_account_credentials_ex
        assert seen, "premise: no read was denied, so nothing could disagree"

        survivors = {
            n: switcher._store._read_previous_backup(n, email)
            for n in ("1", "2") if prev[n].exists()
        }
        foreign = {n: v for n, v in survivors.items()
                   if v and not v.endswith(f"-{n}")}
        assert foreign == {}, (
            "a recovery generation was left holding the other slot's "
            f"half-written material: {sorted(foreign)}"
        )

    def test_the_purge_drops_only_the_keys_a_restore_displaced(
        self, temp_home: Path, sample_sequence_data_with_org: dict
    ):
        """`displaced` is built per key and then acted on for both.

        Line 1481 only truth-tests the set; the loop below it had no
        membership check, so any single displaced key purged every key. Same
        email, failure on the SECOND forward credential write: one key was
        written through and one was not. The written key's retained generation
        really is the half-written forward material and may go; the untouched
        key's is the user's genuine pre-swap copy and its restore, being a
        value-for-value no-op, creates nothing to replace it.

        The discriminator is which keys a FORWARD write reached, which the
        injection below records rather than assumes.
        """
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data_with_org)
        email = "user@example.com"
        for num in ("1", "2"):
            switcher._write_account_credentials(num, email, f"gen1-{num}")
            switcher._write_account_credentials(num, email, f"gen2-{num}")

        prev = {n: switcher._store._prev_backup_path(n, email) for n in ("1", "2")}
        assert all(p.exists() for p in prev.values()), (
            "the fixture produced no .prev, so this would pass however the "
            "purge behaves"
        )

        calls = {"n": 0}
        forward: set[str] = set()
        real_write = switcher._write_account_credentials
        rolling_back = {"yet": False}

        def fail_second(num, mail, creds):
            calls["n"] += 1
            if calls["n"] == 2:
                rolling_back["yet"] = True
                raise OSError(errno.EIO, "the second forward write failed")
            if not rolling_back["yet"]:
                forward.add(num)
            return real_write(num, mail, creds)

        switcher._write_account_credentials = fail_second
        try:
            with pytest.raises(OSError):
                switcher.swap_accounts("1", "2")
        finally:
            del switcher._write_account_credentials

        untouched = sorted(set(prev) - forward)
        assert untouched, (
            "premise: every key took a forward write, so a blanket purge and "
            "a per-key one cannot be told apart here"
        )
        lost = [n for n in untouched if not prev[n].exists()]
        assert lost == [], (
            f"the purge dropped .prev for {lost}, which no forward write "
            "reached — that is the user's pre-swap generation, not "
            "contamination this swap created"
        )

    @pytest.mark.parametrize("retained", [True, False])
    @pytest.mark.parametrize("backend", ["file", "keychain"])
    def test_the_write_reports_what_it_retained_on_both_backends(
        self, temp_home: Path, monkeypatch, backend, retained
    ):
        """The verdict the rollback purge keys on, at its source.

        `_write_account_credentials`'s own docstring calls the type
        load-bearing and says `uv run pytest` cannot see it. Measured: with
        the Keychain arm's `return retained` replaced by `return False` the
        whole suite stayed green, while raising there instead failed 26 cases
        and errored 121 -- so the branch is entered constantly and nothing
        read what it answered. With the verdict lost the purge never runs and
        the contaminated `.prev` survives.
        """
        store = ClaudeAccountSwitcher()._store
        # BOTH DIRECTIONS. A constant stub pins only the half where the verdict
        # goes False. The other half -- a constant True -- makes the purge
        # delete a `.prev` the restore never created, which is the user's own
        # pre-swap copy, and it passed.
        monkeypatch.setattr(
            store, "_retain_previous_backup", lambda *a, **k: retained
        )
        monkeypatch.setattr(store, "_use_keychain", lambda: backend == "keychain")
        monkeypatch.setattr(store, "_kc_write_backup", lambda *a, **k: None)
        monkeypatch.setattr(
            store, "_reconcile_enc_after_keychain_write", lambda *a, **k: None
        )
        monkeypatch.setattr(store, "_write_backup_enc", lambda *a, **k: None)
        monkeypatch.setattr(
            store, "_delete_backup_keychain_quiet", lambda *a, **k: None
        )

        got = store._write_account_credentials("1", "u@example.com", "c")
        assert got is retained, (
            f"the {backend} arm answered {got} where the retention decided "
            f"{retained}; the rollback purge keys on this, so a lost False "
            "deletes a pre-swap copy nothing displaced and a lost True leaves "
            "the contaminated one in place"
        )

    def test_a_rollback_drops_prev_it_really_did_contaminate(
        self, temp_home: Path, sample_sequence_data_with_org: dict
    ):
        """The other half, and without it the purge is untested.

        Measured: with the purge disabled outright the file stays green, so
        every case here pins only that it does NOT fire. Same email, failure
        at the COMMIT so all four forward writes landed — the restores then
        push that half-written material into each key's retained generation,
        and those really are contamination.
        """
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data_with_org)
        email = "user@example.com"
        for num in ("1", "2"):
            switcher._write_account_credentials(num, email, f"gen1-{num}")
            switcher._write_account_credentials(num, email, f"gen2-{num}")

        prev = {n: switcher._store._prev_backup_path(n, email) for n in ("1", "2")}
        assert all(p.exists() for p in prev.values()), "no .prev to drop"

        def failing_commit(*_a, **_kw):
            raise ConfigError("commit failed")

        switcher._write_json = failing_commit
        try:
            with pytest.raises(ConfigError):
                switcher.swap_accounts("1", "2")
        finally:
            del switcher._write_json

        alive = {n: p.exists() for n, p in prev.items()}
        assert alive == {"1": False, "2": False}, (
            f"a generation the rollback itself contaminated was kept: {alive}"
        )
        assert switcher._read_account_credentials("1", email) == "gen2-1"
        assert switcher._read_account_credentials("2", email) == "gen2-2"


def _session_token_of(num: str) -> str:
    return '{"claudeAiOauth": {"accessToken": "SESSION-TOKEN-OF-SLOT-%s"}}' % num


class TestTheReverseCanFailAndTheSkipMustSeeIt:
    """`_swap_session_dirs` swallows `OSError` by design, so the rollback's
    reverse can put back FEWER profiles than the forward move took.

    The value-equal skip then finds both backups already holding their
    originals, writes nothing, and `_post_backup_write` never invalidates the
    profiles that are still crossed -- so both slots keep serving each other's
    session token, live, with no warning that says so. Base was clean here
    only because its unconditional restore write masked it; this branch
    removed the masking without replacing it.
    """

    def _seed(self, switcher, data):
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, data)

    def test_a_leftover_swapping_dir_leaves_both_profiles_crossed(
        self, temp_home: Path, sample_sequence_data_with_org: dict
    ):
        """NO INJECTED FAILURE. The only seeded state is a leftover
        `.swapping` directory -- which nothing in the codebase removes and an
        interrupt on any earlier swap leaves behind. It makes the park step
        raise a real ENOTEMPTY from a real `os.replace`.
        """
        switcher = ClaudeAccountSwitcher()
        self._seed(switcher, sample_sequence_data_with_org)
        email = "user@example.com"
        for num in ("1", "2"):
            switcher._write_account_credentials(num, email, f"creds-{num}")
            d = switcher._session_dir(num, email)
            d.mkdir(parents=True, exist_ok=True)
            (d / "marker").write_text(f"SLOT-{num}-HISTORY")
            (d / ".credentials.json").write_text(_session_token_of(num))

        strand = switcher._session_dir("2", email)
        strand = strand.with_name(strand.name + ".swapping")
        strand.mkdir(parents=True)
        (strand / "leftover").write_text("from an earlier interrupt")

        calls = {"n": 0}
        real_write = switcher._write_account_credentials

        def fail_the_first(num, mail, creds):
            calls["n"] += 1
            if calls["n"] == 1:
                raise CredentialError("injected: the swap dies on its first write")
            return real_write(num, mail, creds)

        switcher._write_account_credentials = fail_the_first
        with pytest.raises((CredentialError, ConfigError)):
            switcher.swap_accounts("1", "2")
        switcher._write_account_credentials = real_write

        serving = {}
        for num in ("1", "2"):
            f = switcher._session_dir(num, email) / ".credentials.json"
            if f.exists():
                serving[num] = f.read_text()
        wrong = {
            num: text for num, text in serving.items()
            if text and f"SLOT-{num}" not in text
        }
        assert not wrong, (
            "a session profile was left serving another slot's token with no "
            f"invalidation: {wrong}"
        )


    def test_an_interrupt_before_the_first_write_still_uncrosses(
        self, temp_home: Path, sample_sequence_data_with_org: dict
    ):
        """`wrote_backups` alone cannot gate the repair.

        `moved` is appended from a `finally` precisely so a signal past a
        rename is recorded, and `KeyboardInterrupt` is not `OSError` -- so it
        leaves `_swap_session_dirs` with `moved` full while `wrote_backups` is
        still False. Gating `restores` on `wrote_backups` alone then skipped
        every restore, the repair never ran, and both profiles stayed crossed
        and live. Base was clean here because its restore was unconditional.
        """
        from claude_swap import switcher as switcher_mod

        switcher = ClaudeAccountSwitcher()
        self._seed(switcher, sample_sequence_data_with_org)
        email = "user@example.com"
        for num in ("1", "2"):
            switcher._write_account_credentials(num, email, f"creds-{num}")
            d = switcher._session_dir(num, email)
            d.mkdir(parents=True, exist_ok=True)
            (d / ".credentials.json").write_text(_session_token_of(num))

        strand = switcher._session_dir("2", email)
        strand = strand.with_name(strand.name + ".swapping")
        strand.mkdir(parents=True)
        (strand / "leftover").write_text("from an earlier interrupt")

        real_replace = switcher_mod.os.replace
        calls = {"n": 0}

        def interrupt_after_the_last_forward_move(src, dst, *a, **kw):
            out = real_replace(src, dst, *a, **kw)
            calls["n"] += 1
            if calls["n"] == 3:      # both profiles now under each other's keys
                raise KeyboardInterrupt
            return out

        switcher_mod.os.replace = interrupt_after_the_last_forward_move
        try:
            with pytest.raises(KeyboardInterrupt):
                switcher.swap_accounts("1", "2")
        finally:
            switcher_mod.os.replace = real_replace

        serving = {}
        for num in ("1", "2"):
            f = switcher._session_dir(num, email) / ".credentials.json"
            if f.exists():
                serving[num] = f.read_text()
        wrong = {num: v for num, v in serving.items()
                 if v and f"SLOT-{num}" not in v}
        assert not wrong, (
            "an abort before the first credential write left a profile serving "
            f"another slot's token with no invalidation: {wrong}"
        )


class TestTheRetentionVerdictIsTheOneReader:
    """The purge that deletes the user's only recovery generation keys on this
    single boolean, and flipping its unchanged-value arm to `True` left the
    whole suite green -- the three guards above it (`wrote_backups`, the
    value-equal skip, `displaced`) are mutually redundant, so any one of them
    can be deleted invisibly.
    """

    def test_an_unchanged_write_displaces_nothing(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        email = "user@example.com"
        assert switcher._write_account_credentials("1", email, "gen-1") is False, (
            "premise: the FIRST write has nothing to displace"
        )
        assert switcher._write_account_credentials("1", email, "gen-2") is True, (
            "positive control: replacing a different value must retain a .prev"
        )
        assert switcher._write_account_credentials("1", email, "gen-2") is False, (
            "a write of the value already stored claimed it displaced "
            "something, so the rollback purge would delete a .prev this "
            "write never created -- the user's only recovery generation"
        )


class TestTheRollbackSummaryReportsWhatRan:
    """`wrote_backups` is armed one statement BEFORE the first write, so
    "armed" and "wrote something" are different claims.

    The old opening line made the second claim from the first fact: with both
    credential restores skipped it still said "restoring both slots", and the
    reversal line fired before a reverse that then moved nothing. Both are
    reachable, and the existing cases only assert a string is ABSENT -- which
    a rewording satisfies without making the text true.
    """

    def test_a_rollback_that_restored_nothing_says_so(
        self, temp_home: Path, sample_sequence_data_with_org: dict, caplog
    ):
        import logging

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data_with_org)
        email = "user@example.com"
        for num in ("1", "2"):
            switcher._write_account_credentials(num, email, f"creds-{num}")

        real_write = switcher._write_account_credentials

        def die_on_the_first(num, mail, creds):
            raise CredentialError("injected: the swap dies on its first write")

        switcher._write_account_credentials = die_on_the_first
        with caplog.at_level(logging.ERROR, logger="claude-swap"):
            with pytest.raises((CredentialError, ConfigError)):
                switcher.swap_accounts("1", "2")
        switcher._write_account_credentials = real_write

        said = "\n".join(r.getMessage() for r in caplog.records)
        assert "rollback:" in said, (
            "premise: no summary was emitted at all, so this proves nothing"
        )
        assert "credentials were restored" not in said, (
            "the rollback reported restoring credentials while every restore "
            f"was skipped or refused:\n{said}"
        )
        assert "Reversed the session-profile exchange" not in said, (
            "a reversal that moved nothing was announced as having happened:"
            f"\n{said}"
        )


def _raise_config_error(*_a, **_k):
    raise ConfigError("injected restore failure")


class TestTheRollbackDecidesPerKey:
    """Which SLOT is still crossed, and which report the summary owes."""

    @pytest.fixture(autouse=True)
    def _file_mode(self, monkeypatch):
        """Force the FILE store: these cases read the file backend, and on
        macOS a usable Keychain takes the write instead -- no `.enc` written,
        so the retained `.enc.prev` these cases purge or preserve never
        exists. Class-wide and autouse, so membership alone grants it.
        """
        monkeypatch.setattr(CredentialStore, "_use_keychain", lambda self: False)

    def _write(self, switcher, data):
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, data)

    def _one_crossed_one_home(self, switcher, mail_a, mail_b):
        """Disk state where exactly ONE of the two profiles is still crossed.

        Slot B's profile sits in its crossed location and its home is
        occupied, so the reverse refuses to move it back. Slot A never had a
        profile, so nothing of A's ever left home.
        """
        crossed_b = switcher._session_dir("1", mail_b)
        crossed_b.mkdir(parents=True, exist_ok=True)
        switcher._session_dir("2", mail_b).mkdir(parents=True, exist_ok=True)
        return [crossed_b]

    def test_a_reverse_that_moved_nothing_does_not_announce_one(
        self, temp_home: Path, sample_sequence_data_with_org: dict, caplog
    ):
        """`if undone:` had no witness — announcing a reversal that never ran.

        The guard exists because the line fired before a reverse that then put
        nothing back. Mutating it to `if True:` was measured green on this
        branch and on the integrated tree; the existing case cannot see it
        because it runs with `moved == []`, so the outer `if moved:` short
        circuits and its negative assert can never fail from this guard.
        """
        import logging

        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data_with_org)
        mail_a, mail_b = "a@example.com", "b@example.com"
        switcher._write_account_credentials("1", mail_a, "creds-a")
        switcher._write_account_credentials("2", mail_b, "creds-b")
        moved = self._one_crossed_one_home(switcher, mail_a, mail_b)
        assert moved, "premise: nothing moved, so the outer `if moved:` skips"

        # THE REVERSE RUNS AND PUTS NOTHING BACK — `undone` stays empty.
        switcher._swap_session_dirs = lambda *a, **k: None
        with caplog.at_level(logging.ERROR, logger="claude-swap"):
            switcher._rollback_swap(
                "1", mail_a, "creds-a", "{}",
                "2", mail_b, "creds-b", "{}",
                staging={}, moved=moved, wrote_backups=True,
            )
        said = "\n".join(r.getMessage() for r in caplog.records)
        assert said, "premise: the rollback said nothing at all"
        assert "Reversed the session-profile exchange" not in said, (
            "the rollback announced a reversal although the reverse put "
            f"nothing back — both profiles are still crossed:\n{said}"
        )

    def test_a_reverse_that_DID_move_is_named_in_the_summary(
        self, temp_home: Path, sample_sequence_data_with_org: dict, caplog
    ):
        """The `elif undone:` arm, which had no witness on either tree.

        Every arm above it describes CREDENTIALS, so a run whose only work was
        putting profiles back falls into none of them. Deleting the arm was
        measured green; it then reports "no credential needed restoring",
        which is the opposite report — it says nothing happened.
        """
        import logging

        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data_with_org)
        mail_a, mail_b = "a@example.com", "b@example.com"
        switcher._write_account_credentials("1", mail_a, "creds-a")
        switcher._write_account_credentials("2", mail_b, "creds-b")
        # A REVERSE THAT SUCCEEDS. `_one_crossed_one_home` deliberately blocks
        # it by occupying the home, which leaves `undone` empty and takes the
        # `else` arm correctly. Here B's profile is crossed and its home is
        # FREE, so the reverse puts it back and `undone` is non-empty.
        crossed_b = switcher._session_dir("1", mail_b)
        crossed_b.mkdir(parents=True, exist_ok=True)
        (crossed_b / ".credentials.json").write_text("B")
        home_b = switcher._session_dir("2", mail_b)
        assert not home_b.exists(), (
            "premise: B's home is occupied, so the reverse refuses and this "
            "case takes the `else` arm rather than the one under test"
        )
        moved = [crossed_b]

        with caplog.at_level(logging.ERROR, logger="claude-swap"):
            switcher._rollback_swap(
                "1", mail_a, "", "",
                "2", mail_b, "", "",
                staging={}, moved=moved, wrote_backups=False,
            )
        said = "\n".join(r.getMessage() for r in caplog.records)
        assert "rollback:" in said, "premise: no summary was emitted"
        assert "no credential needed restoring" not in said, (
            "a run that reversed a profile exchange was summarised as one "
            f"where nothing needed doing:\n{said}"
        )
        assert "session-profile exchange was reversed" in said, (
            f"the reversal is absent from the summary:\n{said}"
        )

    def test_a_partial_failure_is_named_beside_the_success_it_hides_behind(
        self, temp_home: Path, sample_sequence_data_with_org: dict
    ):
        """Arm 1 and the `kept` clause, asserted POSITIVELY.

        Every other assertion in this file about the summary is negative — it
        checks a string is ABSENT — so both the partial-failure arm and the
        staged-copies clause can be deleted in silence. Measured: collapsing
        arm 1 back to a bare "credentials were restored", and separately
        emptying `kept`, each left the suite byte-identical.

        SAME EMAIL, so `staging` is real: the two slots' keys are each
        other's destinations, which is the only shape where pre-swap copies
        are staged and therefore the only one where "the staged copies are
        kept" can be true.
        """
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data_with_org)
        mail = "user@example.com"
        # THE STORE HOLDS SOMETHING ELSE, so both restores are genuinely OWED
        # and the value-equal skip cannot swallow them. This case is about
        # the SUMMARY, not about which key is crossed, so it does not lean on
        # `crossed_keys` — which is empty here anyway, since with one email
        # neither key's crossed location is a move DESTINATION.
        switcher._write_account_credentials("1", mail, "stale-1")
        switcher._write_account_credentials("2", mail, "stale-2")

        real = switcher._write_account_credentials
        seen: list[str] = []

        def one_lands_then_one_raises(num, email, creds):
            seen.append(num)
            if len(seen) > 1:
                raise CredentialError("injected: every later restore fails")
            return real(num, email, creds)

        staged = {"creds-1": switcher.credentials_dir / ".swap-staging-creds-1.json"}
        staged["creds-1"].parent.mkdir(parents=True, exist_ok=True)
        staged["creds-1"].write_text("{}", encoding="utf-8")

        switcher._write_account_credentials = one_lands_then_one_raises
        try:
            with caplog_at_error() as records:
                switcher._rollback_swap(
                    "1", mail, "creds-1", "{}",
                    "2", mail, "creds-2", "{}",
                    staging=staged, moved=[], wrote_backups=True,
                )
        finally:
            switcher._write_account_credentials = real

        summary = " ".join(records).split("rollback:")[-1]
        assert len(seen) > 1, (
            f"premise: only {len(seen)} restore was attempted, so there is "
            "no partial failure for this case to be about"
        )
        assert "credentials were restored" in summary, (
            f"premise: no restore landed, so arm 1 is not the arm under test: {summary!r}"
        )
        assert "failed" in summary, (
            "a restore that RAISED is hidden behind the success line, so the "
            f"reader is told the rollback went cleanly: {summary!r}"
        )
        assert "staged copies are kept" in summary, (
            "these two slots share an email, so pre-swap copies really were "
            f"staged and kept — and the summary is where that is said: {summary!r}"
        )

    def test_a_landed_staging_move_is_not_re_attempted(
        self, temp_home: Path, sample_sequence_data_with_org: dict, monkeypatch
    ):
        """`staging` names a path that stops existing once its move lands.

        Left set, the outer `finally`'s recovery `os.replace` runs on every
        SUCCESSFUL exchange and is carried by its own `except OSError` — so
        the happy path leans on an error handler rather than on there being
        no error, and a real ENOENT there is indistinguishable from that.
        """
        from claude_swap import switcher as switcher_mod

        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data_with_org)
        mail_a, mail_b = "a@example.com", "b@example.com"
        switcher._session_dir("1", mail_a).mkdir(parents=True, exist_ok=True)
        switcher._session_dir("2", mail_b).mkdir(parents=True, exist_ok=True)

        real_replace = os.replace
        from_staging: list[str] = []

        def recording(src, dst, *a, **k):
            if str(src).endswith(".swapping"):
                from_staging.append(str(dst))
            return real_replace(src, dst, *a, **k)

        monkeypatch.setattr(switcher_mod.os, "replace", recording)
        moved: list = []
        switcher._swap_session_dirs("1", mail_a, "2", mail_b, moved)

        assert moved, "premise: the forward exchange moved nothing"
        assert len(from_staging) == 1, (
            "the staging name was replaced twice on a successful exchange — "
            "the strand recovery re-ran and only its `except OSError` hid "
            f"it: {from_staging}"
        )

    def test_only_the_slot_still_crossed_is_forced_through_a_write(
        self, temp_home: Path, sample_sequence_data_with_org: dict
    ):
        """`moved` is not a per-key fact, and neither is "some move happened".

        A reverse that put back one of two leaves the OTHER slot's profile
        crossed. Marking BOTH keys crossed forces a credential write on a
        slot whose profile never moved, and every credential write routes
        through `_post_backup_write` -- so a correctly-untouched slot loses
        its session credentials for a swap that did nothing to it.
        """
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data_with_org)
        mail_a, mail_b = "a@example.com", "b@example.com"
        switcher._write_account_credentials("1", mail_a, "creds-a")
        switcher._write_account_credentials("2", mail_b, "creds-b")
        moved = self._one_crossed_one_home(switcher, mail_a, mail_b)

        wrote: list[str] = []
        real = switcher._write_account_credentials

        def record(num, mail, creds):
            wrote.append(num)
            return real(num, mail, creds)

        switcher._write_account_credentials = record
        try:
            switcher._rollback_swap(
                "1", mail_a, "creds-a", "{}",
                "2", mail_b, "creds-b", "{}",
                staging={}, moved=moved, wrote_backups=True,
            )
        finally:
            switcher._write_account_credentials = real

        assert "2" in wrote, (
            "premise: the slot that IS still crossed was not forced through a "
            f"write, so this case cannot see the other one either: {wrote}"
        )
        assert "1" not in wrote, (
            "a slot whose profile never left home was forced through a "
            "credential restore of the value already under it, which costs "
            f"it its session profile: writes were {wrote}"
        )

    def test_a_rollback_whose_every_restore_raised_does_not_say_nothing_ran(
        self, temp_home: Path, sample_sequence_data_with_org: dict
    ):
        """`wrote_any` is set AFTER the write, so a raising restore leaves it
        False -- and the summary then read that as "nothing was written".

        Those are opposite reports. Nothing-was-written needs no action; every
        restore failing is what keeps the staged copies on disk for manual
        recovery, and the line is the only place that says so.
        """
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data_with_org)
        mail_a, mail_b = "a@example.com", "b@example.com"
        switcher._write_account_credentials("1", mail_a, "creds-a")
        switcher._write_account_credentials("2", mail_b, "creds-b")
        moved = self._one_crossed_one_home(switcher, mail_a, mail_b)

        attempted: list[str] = []

        def die(num, mail, creds):
            attempted.append(num)
            raise CredentialError("injected: the restore cannot land")

        real = switcher._write_account_credentials
        switcher._write_account_credentials = die
        try:
            with caplog_at_error() as records:
                switcher._rollback_swap(
                    "1", mail_a, "creds-a", "{}",
                    "2", mail_b, "creds-b", "{}",
                    staging={}, moved=moved, wrote_backups=True,
                )
        finally:
            switcher._write_account_credentials = real

        said = " ".join(records)
        assert attempted, (
            "premise: no restore was even attempted, so there was nothing "
            "that could have failed"
        )
        assert "rollback:" in said, "premise: no summary was emitted at all"
        summary = said.split("rollback:")[-1]
        assert "credentials were restored" not in summary, (
            f"a restore that raised was reported as a restore: {said!r}"
        )
        # THE ARM'S OWN CONTENT, not the absence of a word from a superseded
        # draft. Keying on "nothing" was satisfied by the arm AND by its
        # deletion, because the fallback wording carries no "nothing" either.
        assert "failed" in summary, (
            "every restore failed and the summary does not say so, so a "
            f"reader cannot tell it from a rollback with nothing to do: {said!r}"
        )
        # THE NOUN, and it is the whole content of this arm. `failures` counts
        # config restores and the overlap deletes as well as credential
        # writes, while `wrote_any` is credential-only -- so "no RESTORE
        # landed" is false in this very state, where both config restores
        # landed and wrote files. "failed" alone is satisfied by either
        # wording, which is how the correction shipped unwitnessed.
        assert "no credential restore landed" in summary, (
            "the arm denies restores that landed: `wrote_any` is set only by "
            f"the credential branch, and two config restores wrote: {said!r}"
        )
        assert "staged copies are kept" not in summary, (
            "these two slots have distinct emails, so nothing was staged — "
            f"the line names a recovery that does not exist: {said!r}"
        )


    def test_a_rollback_that_left_the_profiles_crossed_says_so(
        self, temp_home: Path, sample_sequence_data_with_org: dict
    ):
        """The summary describes the CREDENTIAL axis and nothing else.

        Its five arms cover restored / partly failed / none landed / reversed
        / nothing owed. A reverse that could not put a profile back falls in
        none of them: `elif undone` needs `wrote_any` False AND `failures`
        zero, so "credentials were restored" is printed at ERROR level while a
        slot's session profile is still living under the other slot's key.

        No injected failure is needed. A leftover directory at the home is
        enough -- the park step raises ENOTEMPTY, `_swap_session_dirs`
        swallows it by design, and the state is exactly what
        `crossed_keys` is computed from four lines above the summary.
        """
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data_with_org)
        mail_a, mail_b = "a@example.com", "b@example.com"
        switcher._write_account_credentials("1", mail_a, "creds-a")
        switcher._write_account_credentials("2", mail_b, "creds-b")
        moved = self._one_crossed_one_home(switcher, mail_a, mail_b)

        with caplog_at_error() as records:
            switcher._rollback_swap(
                "1", mail_a, "creds-a", "{}",
                "2", mail_b, "creds-b", "{}",
                staging={}, moved=moved, wrote_backups=True,
            )

        said = " ".join(records)
        assert "rollback:" in said, "premise: no summary was emitted at all"
        summary = said.split("rollback:")[-1]
        crossed = switcher._session_dir("1", mail_b)
        assert crossed.exists(), (
            "premise: the profile is not crossed, so there is nothing for the "
            "summary to have left out"
        )
        assert "session profile" in summary, (
            "a session profile is still under the other slot's key and the "
            f"summary does not mention it: {said!r}"
        )

    def test_a_rollback_with_nothing_crossed_does_not_claim_otherwise(
        self, temp_home: Path, sample_sequence_data_with_org: dict
    ):
        """THE CONTROL for the clause above, and it was missing.

        Measured: with the clause fired unconditionally the whole file stayed
        at 48 passed. Every rollback would then report crossed profiles, which
        is the same defect in the other direction -- a reader who acts on it
        goes looking for two directories that are exactly where they belong.
        """
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data_with_org)
        mail_a, mail_b = "a@example.com", "b@example.com"
        switcher._write_account_credentials("1", mail_a, "creds-a")
        switcher._write_account_credentials("2", mail_b, "creds-b")

        with caplog_at_error() as records:
            switcher._rollback_swap(
                "1", mail_a, "creds-a", "{}",
                "2", mail_b, "creds-b", "{}",
                staging={}, moved=[], wrote_backups=True,
            )

        said = " ".join(records)
        assert "rollback:" in said, "premise: no summary was emitted at all"
        summary = said.split("rollback:")[-1]
        # THE ARM DOES NOT MATTER; the clause is appended to whichever fired,
        # which is the point of appending it once instead of per arm. What
        # matters is that a summary was produced at all and that it does not
        # name a crossing.
        assert summary.strip(), f"premise: the summary is empty: {said!r}"
        assert "session profile" not in summary, (
            f"nothing moved, so nothing can be crossed: {said!r}"
        )

    def test_the_forced_write_reaches_the_profile_that_is_actually_crossed(
        self, temp_home: Path, sample_sequence_data_with_org: dict
    ):
        """`crossed_keys` forces a write so the value-equal skip cannot leave a
        slot serving the other account's token. The write invalidates
        `_session_dir(num, email)` -- the key's HOME -- and a key is in
        `crossed_keys` PRECISELY because its profile is not there.

        With one email the two coincide (`home(A) == crossed(B)`) and the
        repair lands by accident. With two they are disjoint, which is the
        half of the population the guard was written for and the half it
        cannot reach.
        """
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data_with_org)
        mail_a, mail_b = "a@example.com", "b@example.com"
        switcher._write_account_credentials("1", mail_a, "creds-a")
        switcher._write_account_credentials("2", mail_b, "creds-b")
        moved = self._one_crossed_one_home(switcher, mail_a, mail_b)

        crossed = switcher._session_dir("1", mail_b)
        home = switcher._session_dir("2", mail_b)
        for d, token in ((crossed, "B-REAL-TOKEN"), (home, "BLOCKER-JUNK")):
            d.mkdir(parents=True, exist_ok=True)
            (d / ".credentials.json").write_text(token)

        with caplog_at_error():
            switcher._rollback_swap(
                "1", mail_a, "creds-a", "{}",
                "2", mail_b, "creds-b", "{}",
                staging={}, moved=moved, wrote_backups=True,
            )

        assert crossed.exists(), (
            "premise: the crossed profile is gone, so there is nothing left "
            "for the repair to have missed"
        )
        assert not (crossed / ".credentials.json").exists(), (
            "the crossed profile still holds its credential material — the "
            "forced write invalidated the key's HOME, and the profile is "
            "under the OTHER slot's key, which is what put it in crossed_keys"
        )

    def test_an_unwritable_crossed_profile_is_not_a_credential_failure(
        self, temp_home: Path, sample_sequence_data_with_org: dict
    ):
        """The repair touches a SESSION directory from inside the `except`
        that counts CREDENTIAL restore failures.

        EACCES on the crossed profile -- the fault
        `_write_account_credentials`'s own docstring names -- then reports a
        failure for a restore that LANDED, naming a session path in a message
        about credentials. `failures` also disables the `.prev` purge and
        prints "Rollback incomplete ... kept for manual recovery", so one
        unwritable directory turns a clean rollback into a recovery notice.

        The chokepoint one function above already solved this: its own
        docstring says the invalidation is contained and its failure LEAVES
        THE MARKER instead.
        """
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data_with_org)
        mail_a, mail_b = "a@example.com", "b@example.com"
        switcher._write_account_credentials("1", mail_a, "creds-a")
        switcher._write_account_credentials("2", mail_b, "creds-b")
        moved = self._one_crossed_one_home(switcher, mail_a, mail_b)

        crossed = switcher._session_dir("1", mail_b)
        crossed.mkdir(parents=True, exist_ok=True)
        (crossed / ".credentials.json").write_text("B-REAL-TOKEN")

        # INJECTED, NOT chmod'd. A mode bit denies nothing on Windows, so the
        # fault never fired there and the three negative asserts below passed
        # because NOTHING had happened -- the shape they exist to catch.
        real_inval = switcher._invalidate_session_credentials
        denied = {"n": 0}

        def denying(num, email, *a, **k):
            if (num, email) == ("1", mail_b):
                denied["n"] += 1
                raise PermissionError(errno.EACCES, "injected")
            return real_inval(num, email, *a, **k)

        switcher._invalidate_session_credentials = denying
        with caplog_at_error() as records:
            switcher._rollback_swap(
                "1", mail_a, "creds-a", "{}",
                "2", mail_b, "creds-b", "{}",
                staging={}, moved=moved, wrote_backups=True,
            )

        assert denied["n"] == 1, (
            "premise: the crossed profile was never reached, so nothing was "
            f"denied and the asserts below judge an empty run ({denied['n']})"
        )
        said = " ".join(records)
        assert switcher._read_account_credentials("2", mail_b) == "creds-b", (
            "premise: the credential restore did NOT land, so a reported "
            "failure would be true and this asserts nothing"
        )
        assert "creds restore failed for slot 2" not in said, (
            "an unwritable SESSION directory was reported as a failed "
            f"CREDENTIAL restore, for a restore that landed: {said!r}"
        )
        # THE RECOVERY NOTICE IS NOT ASSERTED HERE. It lives under
        # `if staging:`, and `staging` is only ever populated when the two
        # slots share an email -- so with the distinct emails this case needs,
        # the string cannot appear whatever `failures` is, and asserting its
        # absence would be a check that cannot fail. Both arms of that branch
        # are witnessed by `test_staged_copies_are_kept_on_a_partial_rollback`
        # and its sibling below.
        from claude_swap.session import stale_marker_for

        assert stale_marker_for(crossed).exists(), (
            "the crossed profile was left with neither its credential "
            "dropped nor a stale marker, so it keeps serving the superseded "
            "generation with nothing to say so. The marker is a SIBLING of "
            "the profile dir for exactly this fault -- the directory that "
            "denied the unlink is not asked to accept a create"
        )

    def test_a_crossed_profile_that_could_not_be_marked_either_is_reported(
        self, temp_home: Path, sample_sequence_data_with_org: dict
    ):
        """The one state nothing else in the rollback can say.

        Contained the failure, then dropped the answer: with the invalidation
        denied AND the marker denied, the profile keeps the superseded
        credential, carries no marker, and `setup_session` reuses it -- its
        validity check is an identity check, not a generation check. The
        crossed clause in the summary cannot stand in for this: it is computed
        from `crossed_keys` BEFORE the repair and prints identically when the
        repair succeeds.

        The chokepoint one function above logs exactly this at ERROR. Removing
        the wrong words from this arm removed the signal with them.
        """
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data_with_org)
        mail_a, mail_b = "a@example.com", "b@example.com"
        switcher._write_account_credentials("1", mail_a, "creds-a")
        switcher._write_account_credentials("2", mail_b, "creds-b")
        moved = self._one_crossed_one_home(switcher, mail_a, mail_b)

        crossed = switcher._session_dir("1", mail_b)
        crossed.mkdir(parents=True, exist_ok=True)
        (crossed / ".credentials.json").write_text("B-REAL-TOKEN")

        real_inval = switcher._invalidate_session_credentials

        def denying(num, email, *a, **k):
            if (num, email) == ("1", mail_b):
                raise PermissionError(errno.EACCES, "injected")
            return real_inval(num, email, *a, **k)

        switcher._invalidate_session_credentials = denying
        import claude_swap.session as _session
        monkeypatch_target = _session.mark_session_stale
        _session.mark_session_stale = lambda _d: False
        try:
            with caplog_at_error() as records:
                switcher._rollback_swap(
                    "1", mail_a, "creds-a", "{}",
                    "2", mail_b, "creds-b", "{}",
                    staging={}, moved=moved, wrote_backups=True,
                )
        finally:
            _session.mark_session_stale = monkeypatch_target

        said = " ".join(records)
        assert (crossed / ".credentials.json").exists(), (
            "premise: the credential was dropped after all, so there is no "
            "unrecorded state for this to report"
        )
        # THE EXCLUSIVE FRAGMENT. Both halves of the old disjunction appear
        # verbatim in the sibling ERROR one function up, which fires on the
        # HOME profile -- and the two faults co-occur, because one denied
        # parent directory produces both. Only "crossed" names this arm.
        assert "crossed session profile" in said, (
            "the crossed profile kept its superseded credential AND got no "
            f"marker, and nothing said so: {said!r}"
        )

    def test_a_refused_overlap_clear_is_not_called_a_restore(
        self, temp_home: Path, sample_sequence_data_with_org: dict
    ):
        """The overlap arm CLEARS a key; nothing was restored there.

        `original` is falsy in that branch, so no write is attempted -- and
        the per-key handler said `restore failed` about a step that never
        ran. The verb is chosen from `original` now, and this is what makes
        the choice observable: without it, pinning the word back to "restore"
        changes nothing the suite can see.
        """
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data_with_org)
        email = "user@example.com"
        switcher._write_account_credentials("1", email, "creds-org")
        switcher._write_account_credentials("2", email, "creds-personal")

        def refusing(num, mail, *a, **k):
            raise PermissionError(errno.EACCES, "injected: strict delete")

        switcher._delete_account_credentials_strict = refusing
        with caplog_at_error() as records:
            switcher._rollback_swap(
                "1", email, "", "",
                "2", email, "", "",
                staging={}, moved=[], wrote_backups=True,
            )
        said = " ".join(records)
        assert "clear failed" in said, (
            "the overlap arm CLEARS a key and no restore was attempted, so "
            f"naming it a restore describes a step that never ran: {said!r}"
        )
        assert "restore failed" not in said, (
            f"a delete was reported as a failed restore: {said!r}"
        )

    def test_a_refused_credential_restore_IS_called_a_restore(
        self, temp_home: Path, sample_sequence_data_with_org: dict
    ):
        """The other direction of the same verb, which nothing witnessed.

        Its sibling pins the falsy-`original` branch. With only that one,
        pinning the verb to the constant "clear" passes the whole file -- so
        the mirror defect (every genuine credential-restore failure named a
        clear) had no witness at all, and the sibling's own negative assert
        `"creds restore failed" not in said` goes vacuous under that pinning.
        """
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data_with_org)
        mail_a, mail_b = "a@example.com", "b@example.com"
        # DIFFERENT FROM WHAT THE ROLLBACK RESTORES, or the value-equal skip
        # above means no write is attempted and the verb is never chosen.
        switcher._write_account_credentials("1", mail_a, "creds-a-HALFWRITTEN")
        switcher._write_account_credentials("2", mail_b, "creds-b")

        real_write = switcher._write_account_credentials
        refused = {"n": 0}

        def refusing(num, mail, value, *a, **k):
            if (num, mail) == ("1", mail_a):
                refused["n"] += 1
                raise PermissionError(errno.EACCES, "injected: restore")
            return real_write(num, mail, value, *a, **k)

        switcher._write_account_credentials = refusing
        with caplog_at_error() as records:
            switcher._rollback_swap(
                "1", mail_a, "creds-a", "{}",
                "2", mail_b, "creds-b", "{}",
                staging={}, moved=[], wrote_backups=True,
            )
        assert refused["n"] >= 1, (
            "premise: the restore was never attempted, so the verb below was "
            "chosen for some other branch"
        )
        said = " ".join(records)
        assert "creds restore failed" in said, (
            "a credential restore was attempted with material to restore and "
            f"its failure was not named a restore: {said!r}"
        )

    def test_a_refused_marker_still_purges_the_contaminated_prev(
        self, temp_home: Path, sample_sequence_data_with_org: dict
    ):
        """The point of splitting the counter, and nothing pinned it.

        Counting a refused session marker into `failures` gates the `.prev`
        purge, and the retained generation is the OTHER account's material
        under this key -- what the purge block's own comment calls pure
        contamination. The staging half of that split IS witnessed; this
        half was not, so re-adding `and not repairs` to the purge gate left
        the whole file green.
        """
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data_with_org)
        mail_a, mail_b = "a@example.com", "b@example.com"
        switcher._write_account_credentials("1", mail_a, "creds-a")
        switcher._write_account_credentials("2", mail_b, "creds-b")
        # A SECOND WRITE, so a `.prev` generation exists for the restore to
        # displace. Without it `displaced` stays empty and the purge is inert
        # whatever the counter says.
        switcher._write_account_credentials("2", mail_b, "creds-b-HALFWRITTEN")
        prev = switcher._store._prev_backup_path("2", mail_b)
        assert prev.exists(), (
            "premise: no retained generation exists, so the purge below has "
            "nothing to remove and this case cannot see its subject"
        )
        moved = self._one_crossed_one_home(switcher, mail_a, mail_b)

        crossed = switcher._session_dir("1", mail_b)
        crossed.mkdir(parents=True, exist_ok=True)
        (crossed / ".credentials.json").write_text("B-REAL-TOKEN")
        switcher._live_session_pids = (
            lambda num, email, *a, **k:
            [4242] if (num, email) == ("1", mail_b) else []
        )
        import claude_swap.session as _session
        real_mark = _session.mark_session_stale
        _session.mark_session_stale = lambda _d: False
        try:
            with caplog_at_error() as records:
                switcher._rollback_swap(
                    "1", mail_a, "creds-a", "{}",
                    "2", mail_b, "creds-b", "{}",
                    staging={}, moved=moved, wrote_backups=True,
                )
        finally:
            _session.mark_session_stale = real_mark

        said = " ".join(records)
        assert "could not be invalidated or marked stale" in said, (
            f"premise: the repair did not fail, so nothing was counted: {said!r}"
        )
        assert not prev.exists(), (
            "a refused SESSION MARKER kept this key's retained generation, "
            "which holds the other account's material -- the marker lives in "
            "a sibling tree of the credential store and says nothing about "
            "whether the restores landed"
        )

    def test_a_LIVE_crossed_profile_neither_lever_reached_is_reported_too(
        self, temp_home: Path, sample_sequence_data_with_org: dict
    ):
        """The same end state through the door the sibling case cannot see.

        Its sibling makes the chokepoint RAISE. When the crossed profile is
        LIVE the chokepoint does not raise at all: it takes its marker branch,
        logs a denied marker and returns normally -- so the rollback learns
        nothing, `failures` stays 0, the staged pre-swap copies are discarded
        and the summary reports success. Identical state, and this is the
        likelier door: the crossed profile is the one MOST likely to be live.
        """
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data_with_org)
        mail_a, mail_b = "a@example.com", "b@example.com"
        switcher._write_account_credentials("1", mail_a, "creds-a")
        switcher._write_account_credentials("2", mail_b, "creds-b")
        moved = self._one_crossed_one_home(switcher, mail_a, mail_b)

        crossed = switcher._session_dir("1", mail_b)
        crossed.mkdir(parents=True, exist_ok=True)
        (crossed / ".credentials.json").write_text("B-REAL-TOKEN")

        switcher._live_session_pids = (
            lambda num, email, *a, **k:
            [4242] if (num, email) == ("1", mail_b) else []
        )
        staged = switcher.backup_dir / ".swap-staging-probe"
        staged.write_text("pre-swap-copy", encoding="utf-8")
        import claude_swap.session as _session
        real_mark = _session.mark_session_stale
        _session.mark_session_stale = lambda _d: False
        try:
            with caplog_at_error() as records:
                switcher._rollback_swap(
                    "1", mail_a, "creds-a", "{}",
                    "2", mail_b, "creds-b", "{}",
                    staging={"creds-1": staged}, moved=moved,
                    wrote_backups=True,
                )
        finally:
            _session.mark_session_stale = real_mark

        said = " ".join(records)
        # THE SUMMARY'S CLAUSE, which only the COUNT produces. The arm also
        # logs, and a log line is satisfied whether or not the rollback
        # learned anything -- keyed on it, dropping the count passed.
        assert "could not be invalidated or marked stale" in said, (
            "premise: the live marker branch did not fire, so the staged "
            f"copies below survive or vanish for another reason: {said!r}"
        )
        # AND NOT BY KEEPING CREDENTIAL MATERIAL. The refusal is on a session
        # marker, a sibling tree of the credential store, so it is no reason
        # to doubt the restores -- and a staged plaintext copy cannot repair
        # an unmarked profile.
        assert not staged.exists(), (
            "a refused session marker kept the staged pre-swap credential, "
            "which cannot repair a profile and blocks the next same-email swap"
        )

    def test_a_crossed_profile_neither_lever_reached_is_reported(
        self, temp_home: Path, sample_sequence_data_with_org: dict
    ):
        """The worse of the two repair failures was the uncounted one.

        Its `except Exception` sibling further down counts, and says why:
        without the count the staged copies are discarded. The `OSError` arm
        did not, so in the one state where a profile keeps serving a
        superseded credential with NO marker, the rollback threw away every
        remaining recovery copy and reported success -- the summary reads
        `credentials were restored` with no failure clause, `_discard_staging`
        deletes the staged pre-swap copies, and the purge gate
        `wrote_backups and displaced and not failures` then drops both slots'
        `.prev` generations.

        Its sibling case passes `staging={}`, so the branch is inert there and
        the accounting has never been observed.
        """
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data_with_org)
        mail_a, mail_b = "a@example.com", "b@example.com"
        switcher._write_account_credentials("1", mail_a, "creds-a")
        switcher._write_account_credentials("2", mail_b, "creds-b")
        moved = self._one_crossed_one_home(switcher, mail_a, mail_b)

        crossed = switcher._session_dir("1", mail_b)
        crossed.mkdir(parents=True, exist_ok=True)
        (crossed / ".credentials.json").write_text("B-REAL-TOKEN")

        real_post = switcher._post_backup_write
        refused = {"n": 0}

        def denying(num, email, *a, **k):
            if (num, email) == ("1", mail_b):
                refused["n"] += 1
                raise PermissionError(errno.EACCES, "injected")
            return real_post(num, email, *a, **k)

        switcher._post_backup_write = denying
        staged = switcher.backup_dir / ".swap-staging-probe"
        staged.write_text("pre-swap-copy", encoding="utf-8")
        import claude_swap.session as _session
        real_mark = _session.mark_session_stale
        _session.mark_session_stale = lambda _d: False
        try:
            with caplog_at_error() as records:
                switcher._rollback_swap(
                    "1", mail_a, "creds-a", "{}",
                    "2", mail_b, "creds-b", "{}",
                    staging={"creds-1": staged}, moved=moved,
                    wrote_backups=True,
                )
        finally:
            _session.mark_session_stale = real_mark

        assert refused["n"] >= 1, (
            "premise: the repair never reached the denied call, so this says "
            "nothing about what it accounts for"
        )
        said = " ".join(records)
        # THE FRAGMENT ONLY THIS ARM SAYS. Its `except Exception` sibling
        # emits "crossed session profile" too AND counts, so that phrase is
        # satisfied when the injected error falls through to the sibling --
        # measured: retyping this arm's `except` leaves this case green. Nor
        # is "OR mark it stale" enough: `_write_account_credentials` says it
        # too, and the sibling arm reaches that emitter.
        assert "crossed session profile OR mark it stale" in said, (
            "premise: the arm under test did not fire, so the staged copies "
            f"below survive for an unrelated reason: {said!r}"
        )
        assert "could not be invalidated or marked stale" in said, (
            "the refused repair was contained and never reported — the "
            f"summary claims a clean restore: {said!r}"
        )
        # AND NOT BY KEEPING CREDENTIAL MATERIAL. The marker lives in a
        # sibling tree of the credential store, so its refusal is no reason to
        # doubt the restores -- and the staged copy cannot repair an unmarked
        # profile, it only leaves a second plaintext credential on disk.
        assert not staged.exists(), (
            "a refused session marker kept the staged pre-swap credential, "
            "which cannot repair a profile and blocks the next same-email swap"
        )

    def test_a_refused_repair_is_contained_but_counted_not_silent(
        self, temp_home: Path, sample_sequence_data_with_org: dict
    ):
        """The repair's comment claims the chokepoint's containment; it does
        not have it, and cannot.

        The chokepoint catches `OSError` alone, so anything else — including
        the suite's own `RealStoreWriteBlocked`, deliberately not an `OSError`
        — propagates and can never be hidden. The repair sits inside the
        per-key loop, whose `except Exception` counts a failure and carries on
        so one key cannot abort the rest of the rollback. Measured: the block
        does NOT propagate. Widening this arm to `Exception` is detectable and
        must not be done: measured, `failures` then stays 0, the staged copies
        are discarded and this case fails on `staged.exists()`.

        So the property to hold here is not propagation but ACCOUNTING: a
        contained failure must keep the staged copies and say so, or a refused
        write leaves no trace.
        """
        from tests.conftest import RealStoreWriteBlocked

        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data_with_org)
        mail_a, mail_b = "a@example.com", "b@example.com"
        switcher._write_account_credentials("1", mail_a, "creds-a")
        switcher._write_account_credentials("2", mail_b, "creds-b")
        moved = self._one_crossed_one_home(switcher, mail_a, mail_b)

        crossed = switcher._session_dir("1", mail_b)
        crossed.mkdir(parents=True, exist_ok=True)
        (crossed / ".credentials.json").write_text("B-REAL-TOKEN")

        real_post = switcher._post_backup_write
        blocked = {"n": 0}

        def blocking(num, email, *a, **k):
            if (num, email) == ("1", mail_b):
                blocked["n"] += 1
                raise RealStoreWriteBlocked(
                    "refused: this write would land in the REAL account store"
                )
            return real_post(num, email, *a, **k)

        switcher._post_backup_write = blocking
        staged = switcher.backup_dir / ".swap-staging-probe"
        staged.write_text("pre-swap-copy", encoding="utf-8")
        with caplog_at_error() as records:
            switcher._rollback_swap(
                "1", mail_a, "creds-a", "{}",
                "2", mail_b, "creds-b", "{}",
                staging={"creds-1": staged}, moved=moved, wrote_backups=True,
            )
        assert blocked["n"] >= 1, (
            "premise: the repair never reached the blocked call, so this says "
            "nothing about what it contains"
        )
        # CONTAINED, BUT NEVER SILENT. The per-key loop's `except Exception`
        # counts this as a failure and carries on, which is the rollback's
        # design -- one key must not abort the rest. So the chokepoint's
        # rationale does NOT transfer here. What must hold instead is that the
        # key is COUNTED, so the staged copy survives and the operator is told,
        # and that the failure is named for what actually failed.
        said = " ".join(records)
        # THE ARM'S OWN SENTENCE. Deleting the whole `except Exception` that
        # names the repair leaves every count, the summary and the staged copy
        # identical -- the wording is the only observable, so nothing else can
        # witness it.
        assert "could not invalidate slot" in said, (
            f"the repair failure was not named as one: {said!r}"
        )
        assert "creds restore failed" not in said, (
            "the repair failure was reported as a credential restore failure, "
            f"about a restore that landed: {said!r}"
        )
        # THE ACCOUNTING LINE, not merely SOME error. A bare `assert said` is
        # satisfied by any record the rollback happens to emit, so it stays
        # green when the contained failure itself goes quiet.
        assert "could not be invalidated or marked stale" in said, (
            "the refused repair was contained and never carried into the "
            f"summary, which claims a clean restore: {said!r}"
        )
        assert not staged.exists(), (
            "a refused repair kept the staged pre-swap credential; the "
            "restores landed, so it is a redundant plaintext copy"
        )

    def test_a_crossed_profile_the_marker_DID_reach_is_not_reported(
        self, temp_home: Path, sample_sequence_data_with_org: dict
    ):
        """THE OTHER HALF OF THE GUARD, which nothing exercised.

        The case above pins that the ERROR fires when the marker is denied.
        Nothing pinned that it stays quiet when the marker lands: dropping the
        `if not` so it fires on both outcomes left the whole suite green on
        this branch and on the integrated tree. An operator who sees this
        line on every contained rollback stops reading it.
        """
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data_with_org)
        mail_a, mail_b = "a@example.com", "b@example.com"
        switcher._write_account_credentials("1", mail_a, "creds-a")
        switcher._write_account_credentials("2", mail_b, "creds-b")
        moved = self._one_crossed_one_home(switcher, mail_a, mail_b)

        crossed = switcher._session_dir("1", mail_b)
        crossed.mkdir(parents=True, exist_ok=True)
        (crossed / ".credentials.json").write_text("B-REAL-TOKEN")

        real_inval = switcher._invalidate_session_credentials

        def denying(num, email, *a, **k):
            if (num, email) == ("1", mail_b):
                raise PermissionError(errno.EACCES, "injected")
            return real_inval(num, email, *a, **k)

        switcher._invalidate_session_credentials = denying
        import claude_swap.session as _session
        real_mark = _session.mark_session_stale
        marked = {"n": 0}

        def landing(_d):
            marked["n"] += 1
            return True

        _session.mark_session_stale = landing
        try:
            with caplog_at_error() as records:
                switcher._rollback_swap(
                    "1", mail_a, "creds-a", "{}",
                    "2", mail_b, "creds-b", "{}",
                    staging={}, moved=moved, wrote_backups=True,
                )
        finally:
            _session.mark_session_stale = real_mark

        assert marked["n"] >= 1, (
            "premise: the marker path was never reached, so this says nothing "
            "about what happens when it succeeds"
        )
        said = " ".join(records)
        assert "crossed session profile" not in said, (
            "the marker landed, so the crossed profile IS recorded as "
            f"superseded, and the rollback reported it as unrecorded: {said!r}"
        )
        # THE CLEAN ARM'S OWN SENTENCE. Every other case here asserts a
        # NEGATIVE of it, and the one positive sits in the arm above, whose
        # string is a superset -- so replacing this arm's text with the
        # opposite arm's left the whole suite green.
        assert "credentials were restored" in said, (
            f"a clean rollback did not say what it did: {said!r}"
        )
        assert "step(s) failed" not in said, (
            f"a clean rollback reported a restore failure: {said!r}"
        )

    def test_staged_copies_are_kept_on_a_partial_rollback(
        self, temp_home: Path, sample_sequence_data_with_org: dict
    ):
        """The kept arm's REPORT had no witness -- not the arm itself.

        Both arms were already reached end-to-end from `swap_accounts`: two
        same-email cases in this file assert the staged file's survival and
        its removal on disk, and they catch a swap of the arms and a deleted
        discard. What nothing caught is deleting this arm's ERROR and its
        user-facing warning, which leaves the last preserved material after a
        half-written swap sitting on disk with nobody told where.
        """
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data_with_org)
        mail = "shared@example.com"
        switcher._write_account_credentials("1", mail, "creds-a")
        switcher._write_account_credentials("2", mail, "creds-b")
        staged = temp_home / "staged-pre-swap.json"
        staged.write_text("PRE-SWAP-MATERIAL")

        discarded: list = []
        switcher._discard_staging = lambda st: discarded.append(st)
        # One restore fails, so `failures` is non-zero and the copies stay.
        switcher._write_account_config = _raise_config_error

        with caplog_at_error() as records:
            switcher._rollback_swap(
                "1", mail, "creds-a", "{}",
                "2", mail, "creds-b", "{}",
                staging={"a": staged}, moved=[], wrote_backups=True,
            )

        said = " ".join(records)
        assert "step(s) failed" in said, (
            f"premise: nothing failed, so the kept arm is not the one under "
            f"test: {said!r}"
        )
        assert "manual recovery" in said and str(staged) in said, (
            f"a partial rollback did not name where the pre-swap copies are: "
            f"{said!r}"
        )
        assert discarded == [], (
            "the staged copies were discarded after a PARTIAL rollback — they "
            "are the only remaining pre-swap material"
        )

    def test_a_live_session_on_the_crossed_profile_is_spared_like_its_home(
        self, temp_home: Path, sample_sequence_data_with_org: dict
    ):
        """The crossed profile is the one MOST likely to be live.

        `_ensure_no_live_session` runs before the forward swap; a `cswap run`
        starting in the window between it and the rollback has its pid files
        renamed to the crossed path by the forward move. The forward write
        then SPARES that profile through the chokepoint -- a running claude
        manages its own copy, so it gets a marker, not a scrub. Reaching
        `_invalidate_session_credentials` directly opts out of that policy on
        exactly that profile.
        """
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data_with_org)
        mail_a, mail_b = "a@example.com", "b@example.com"
        switcher._write_account_credentials("1", mail_a, "creds-a")
        switcher._write_account_credentials("2", mail_b, "creds-b")
        moved = self._one_crossed_one_home(switcher, mail_a, mail_b)

        crossed = switcher._session_dir("1", mail_b)
        crossed.mkdir(parents=True, exist_ok=True)
        (crossed / ".credentials.json").write_text("B-REAL-TOKEN")
        switcher._live_session_pids = lambda *a, **k: [4242]

        with caplog_at_error():
            switcher._rollback_swap(
                "1", mail_a, "creds-a", "{}",
                "2", mail_b, "creds-b", "{}",
                staging={}, moved=moved, wrote_backups=True,
            )

        assert (crossed / ".credentials.json").exists(), (
            "the rollback scrubbed credentials out from under a RUNNING "
            "claude on the crossed profile, which the chokepoint spares"
        )
        from claude_swap.session import stale_marker_for

        assert stale_marker_for(crossed).exists(), (
            "spared but not marked: setup_session will reuse the superseded "
            "generation once the live session exits"
        )

    @pytest.mark.parametrize("cleanup_fails", [False, True])
    def test_a_cleanup_failure_keeps_the_retained_generation(
        self, temp_home: Path, sample_sequence_data_with_org: dict,
        cleanup_fails,
    ):
        """`not failures` in the purge gate, and the counter that feeds it.

        The cleanup block is the ONLY writer of `failures` after the summary
        is logged, so the purge gate is its only reader. Neither had a
        witness: dropping `not failures` from the gate left the suite green,
        and so did deleting the cleanup's `failures += 1`.

        A partial rollback must preserve the maximum material -- the retained
        generation is the user's recovery copy when anything about the
        rollback went wrong, and only a rollback that ran clean may call it
        contamination.
        """
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data_with_org)
        mail_a, mail_b = "a@example.com", "b@example.com"
        switcher._write_account_credentials("1", mail_a, "creds-a")
        switcher._write_account_credentials("1", mail_a, "creds-a-DRIFTED")
        switcher._write_account_credentials("2", mail_b, "creds-b")
        prev = switcher._store._prev_backup_path("1", mail_a)
        assert prev.exists(), "premise: no retained generation to preserve"

        if cleanup_fails:
            real_del = switcher._store._delete_account_credentials

            def refusing(num, email, *a, **k):
                if (num, email) == ("2", mail_a):
                    raise OSError(errno.EIO, "injected: cleanup refused")
                return real_del(num, email, *a, **k)

            switcher._store._delete_account_credentials = refusing

        moved = self._one_crossed_one_home(switcher, mail_a, mail_b)
        with caplog_at_error() as records:
            switcher._rollback_swap(
                "1", mail_a, "creds-a", "{}",
                "2", mail_b, "creds-b", "{}",
                staging={}, moved=moved, wrote_backups=True,
            )

        if cleanup_fails:
            assert "cleanup failed" in " ".join(records), (
                "premise: the injected cleanup failure never fired, so the "
                "counter under test was never incremented"
            )
            assert prev.exists(), (
                "a rollback whose cleanup failed purged the retained "
                "generation anyway — the only recovery copy after a partial "
                "rollback"
            )
        else:
            assert not prev.exists(), (
                "CONTROL: a clean rollback must purge the generation it "
                "contaminated, or the arm above passes for the wrong reason"
            )

    def test_only_a_crossed_key_reaches_into_the_other_slot(
        self, temp_home: Path, sample_sequence_data_with_org: dict
    ):
        """THE CONTROL for the repair above: WHICH keys it reaches for.

        Every one of the four (slot, email) directories is either a home or a
        crossed location for one of the two keys, and the reverse legitimately
        relocates some of them -- so "a file survived" cannot separate the arm
        firing from the reverse moving it. What can is the set of keys the arm
        selects, recorded from the real calls.

        Firing for a key that is not crossed costs a correctly-restored slot
        its session credentials, which is the price the value-equal skip
        exists to avoid.
        """
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data_with_org)
        mail_a, mail_b = "a@example.com", "b@example.com"
        # STORED != ORIGINAL for slot A, so its key is forced through the
        # write without ever being crossed; the value-equal skip would
        # otherwise keep it away from the arm entirely.
        switcher._write_account_credentials("1", mail_a, "creds-a-DRIFTED")
        switcher._write_account_credentials("2", mail_b, "creds-b")
        moved = self._one_crossed_one_home(switcher, mail_a, mail_b)

        reached: list[tuple[str, str]] = []
        real = switcher._invalidate_session_credentials

        def recording(num, email):
            reached.append((num, email))
            return real(num, email)

        switcher._invalidate_session_credentials = recording
        with caplog_at_error():
            switcher._rollback_swap(
                "1", mail_a, "creds-a", "{}",
                "2", mail_b, "creds-b", "{}",
                staging={}, moved=moved, wrote_backups=True,
            )

        assert switcher._read_account_credentials("1", mail_a) == "creds-a", (
            "premise: slot A was not restored, so its key never reached the "
            "forced write and this control tests nothing"
        )
        # A's crossed location is slot 2. It is not in `crossed_keys`, so the
        # arm must never name it.
        assert ("2", mail_a) not in reached, (
            f"the arm reached into the other slot for a key that was never "
            f"crossed: {reached}"
        )


class TestASwapKeepsEachProfilesOwnGeneration:
    """A swap moves a KEY. The credential does not change, and the matching
    session profile moves with it, so nothing about it went stale."""

    def test_a_move_to_an_empty_slot_does_not_strip_the_moved_profile(
        self, temp_home: Path
    ):
        """`cswap move` is the same key move as a swap, one command over."""
        import json

        from claude_swap.session import session_dir_for

        s = ClaudeAccountSwitcher()
        s._setup_directories()
        a = "a@example.com"
        s._write_json(s.sequence_file, {
            "activeAccountNumber": None,
            "lastUpdated": "2024-01-01T00:00:00Z",
            "sequence": [1],
            "accounts": {
                "1": {"email": a, "uuid": "", "organizationUuid": "",
                      "organizationName": "", "added": "2024-01-01T00:00:00Z"},
            }})

        def gen(g):
            return json.dumps({"claudeAiOauth": {
                "accessToken": f"at-{g}", "refreshToken": f"rt-{g}"}})

        s._write_account_credentials("1", a, gen("G0"))
        src = session_dir_for(s.backup_dir, "1", a)
        src.mkdir(parents=True, exist_ok=True)
        (src / ".credentials.json").write_text(gen("G1"), encoding="utf-8")

        assert json.loads(s.read_account_credentials("1", a))[
            "claudeAiOauth"]["refreshToken"] == "rt-G0", (
            "premise: the backup must differ from the profile"
        )

        s.move_account("1", "5")

        seed = session_dir_for(s.backup_dir, "5", a) / ".credentials.json"
        assert seed.exists(), (
            "DEFECT: `cswap move` stripped the moved profile. The backup "
            "holds the generation claude already rotated past, so the next "
            "run POSTs a consumed refresh token"
        )
        assert json.loads(seed.read_text())[
            "claudeAiOauth"]["refreshToken"] == "rt-G1"

    def test_a_swap_carries_each_profile_s_stale_flag_with_it(
        self, temp_home: Path, sample_sequence_data_with_org: dict
    ):
        """The flag is a SIBLING of the directory the swap renames.

        With one shared email the two marker names are each other's
        destination, so a rename of the directories alone leaves the flag
        pointing at whichever profile arrived. `setup_session` acts on it:
        it invalidates the credential and re-bootstraps from a backup that
        profile has already rotated past, which costs a re-login.
        """
        from claude_swap.session import is_session_stale, mark_session_stale

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(
            switcher.sequence_file, sample_sequence_data_with_org
        )
        email = "user@example.com"
        switcher._write_account_credentials("1", email, "creds-org")
        switcher._write_account_credentials("2", email, "creds-personal")
        dir_1 = switcher._session_dir("1", email)
        dir_2 = switcher._session_dir("2", email)
        for num, d in (("1", dir_1), ("2", dir_2)):
            d.mkdir(parents=True, exist_ok=True)
            (d / "marker").write_text(f"SLOT-{num}-HISTORY")
        # PREMISE: exactly one of the two profiles is flagged.
        assert mark_session_stale(dir_1)
        assert is_session_stale(dir_1) and not is_session_stale(dir_2)

        switcher.swap_accounts("1", "2")

        # PREMISE: the profiles really did exchange keys.
        assert (dir_1 / "marker").read_text() == "SLOT-2-HISTORY"
        assert (dir_2 / "marker").read_text() == "SLOT-1-HISTORY"

        assert not is_session_stale(dir_1), (
            "DEFECT: the profile that arrived at this key inherited a flag "
            "set for the other account, so its next launch re-bootstraps it "
            "from a backup it has already rotated past"
        )
        assert is_session_stale(dir_2), (
            "DEFECT: the flagged profile moved without its flag, so the "
            "re-bootstrap it was flagged for never happens"
        )

    def test_a_swap_that_moved_nothing_leaves_each_flag_where_it_was(
        self, temp_home: Path, sample_sequence_data_with_org: dict
    ):
        """"Where did this profile end up" cannot be asked by existence.

        With one shared email the destination names ARE the sources, so
        `os.path.exists(new_a)` is true when A landed there AND when B
        simply never left. On every path that skips the renames the two
        profiles then resolve to each other and the flags swap onto the
        wrong ones -- the same two-part defect, on the failure path.
        """
        from unittest.mock import patch

        from claude_swap.session import is_session_stale, mark_session_stale

        s = ClaudeAccountSwitcher()
        s._setup_directories()
        s._write_json(s.sequence_file, sample_sequence_data_with_org)
        email = "user@example.com"
        d1, d2 = s._session_dir("1", email), s._session_dir("2", email)
        for num, d in (("1", d1), ("2", d2)):
            d.mkdir(parents=True, exist_ok=True)
            (d / "marker").write_text(f"SLOT-{num}-HISTORY")
        assert mark_session_stale(d1)
        # PREMISE: exactly one profile is flagged, and it is slot 1's.
        assert is_session_stale(d1) and not is_session_stale(d2)
        # PREMISE: with one shared email the destination names ARE the sources.
        assert s._session_dir("2", email) == d2 and s._session_dir("1", email) == d1

        calls = []

        def refuse(src, dst, *a, **k):
            calls.append((str(src), str(dst)))
            raise OSError(13, "Permission denied")

        moved = []
        with patch("claude_swap.switcher.os.replace", side_effect=refuse):
            s._swap_session_dirs("1", email, "2", email, moved)

        # PREMISE: nothing moved, so both profiles are exactly where they were.
        assert len(calls) >= 1, "premise: a rename must have been attempted"
        assert (d1 / "marker").read_text() == "SLOT-1-HISTORY"
        assert (d2 / "marker").read_text() == "SLOT-2-HISTORY"

        assert is_session_stale(d1), (
            "DEFECT: slot 1's profile never moved and its flag was deleted, so "
            "the re-bootstrap it was flagged for never happens"
        )
        assert not is_session_stale(d2), (
            "DEFECT: slot 2's profile never moved and INHERITED slot 1's flag, so "
            "its next launch re-bootstraps it from a backup it has rotated past"
        )

    def test_a_one_sided_swap_leaves_each_flag_on_its_own_profile(
        self, temp_home: Path, sample_sequence_data_with_org: dict
    ):
        """A profile that did not land is not necessarily at its old name.

        It is parked under the staging name first, and the recovery
        refuses to put it back when the old name is occupied -- which is
        what the other profile landing there does under one shared email.
        Read as "still at `dir_a`", its flag is written onto the arrived
        profile and its own is dropped.
        """
        import errno
        import os
        from unittest.mock import patch

        from claude_swap.session import is_session_stale, mark_session_stale

        e = "user@example.com"
        s = _two_slots_one_address(ClaudeAccountSwitcher(), sample_sequence_data_with_org, e, e)
        d1, d2 = s._session_dir("1", e), s._session_dir("2", e)
        assert mark_session_stale(d1)
        assert is_session_stale(d1) and not is_session_stale(d2)

        real = os.replace

        def refuse_a_second_leg(src, dst, *a, **k):
            if str(src).endswith(".swapping"):
                raise OSError(errno.EACCES, "refused")
            return real(src, dst, *a, **k)

        moved = []
        with patch("claude_swap.switcher.os.replace", side_effect=refuse_a_second_leg):
            s._swap_session_dirs("1", e, "2", e, moved)

        stranded = d1.with_name(d1.name + ".swapping")
        # PREMISE: B landed at slot 1's key and A is stranded under .swapping.
        assert stranded.exists() and (stranded / "marker").read_text() == "A-HISTORY"
        assert d1.exists() and (d1 / "marker").read_text() == "B-HISTORY"

        assert not is_session_stale(d1), (
            "DEFECT: B's profile arrived at slot 1's key and INHERITED A's flag"
        )
        assert is_session_stale(stranded), (
            "DEFECT: A's profile is stranded and its flag was dropped"
        )

    def test_two_addresses_with_one_slug_keep_their_profiles(
        self, temp_home: Path, sample_sequence_data_with_org: dict
    ):
        """`slugify_email` is documented non-injective, and says uniqueness
        comes from the `<num>-` prefix -- which a swap exchanges. The old
        keys' profile paths are then the two NEW homes, and the post-commit
        prune deletes both accounts' profiles."""

        e1, e2 = "a+x@b.com", "a_x@b.com"
        s = _two_slots_one_address(ClaudeAccountSwitcher(), sample_sequence_data_with_org, e1, e2)
        # PREMISES: the addresses differ and their slugs collide.
        assert e1 != e2
        assert s._session_dir("1", e1).name == s._session_dir("2", e2).name.replace("2-", "1-")
        before_txt = sorted((p / "marker").read_text() for p in (s.backup_dir / "sessions").iterdir())
        assert len(before_txt) == 2, f"premise: two profiles on disk, got {before_txt}"

        s.swap_accounts("1", "2")

        now = sorted((p / "marker").read_text() for p in (s.backup_dir / "sessions").iterdir()
                     if (p / "marker").exists())
        assert now == before_txt, (
            f"DEFECT: the swap DESTROYED session profiles. before={before_txt} after={now}"
        )

    def test_a_move_carries_the_profile_s_stale_flag_with_it(
        self, temp_home: Path
    ):
        """Same sibling problem, one command over — and here the old key's
        flag is deleted by the post-commit prune, so it is simply lost."""
        from claude_swap.session import (
            is_session_stale,
            mark_session_stale,
            session_dir_for,
        )

        s = ClaudeAccountSwitcher()
        s._setup_directories()
        a = "a@example.com"
        s._write_json(s.sequence_file, {
            "activeAccountNumber": None,
            "lastUpdated": "2024-01-01T00:00:00Z",
            "sequence": [1],
            "accounts": {
                "1": {"email": a, "uuid": "", "organizationUuid": "",
                      "organizationName": "", "added": "2024-01-01T00:00:00Z"},
            }})
        s._write_account_credentials("1", a, "creds")
        src = session_dir_for(s.backup_dir, "1", a)
        src.mkdir(parents=True, exist_ok=True)
        (src / ".credentials.json").write_text("seed", encoding="utf-8")
        # PREMISE: the profile is flagged before the move.
        assert mark_session_stale(src)
        assert is_session_stale(src)

        s.move_account("1", "5")

        moved = session_dir_for(s.backup_dir, "5", a)
        # PREMISE: the profile itself did move.
        assert (moved / ".credentials.json").exists()
        assert is_session_stale(moved), (
            "DEFECT: `cswap move` left the flag at the old key, where the "
            "post-commit prune deletes it, so the re-bootstrap it was "
            "flagged for never happens"
        )

    def test_a_successful_swap_does_not_strip_the_moved_profiles(
        self, temp_home: Path
    ):
        import json

        from claude_swap.session import session_dir_for

        s = ClaudeAccountSwitcher()
        s._setup_directories()
        a, b = "a@example.com", "b@example.com"
        s._write_json(s.sequence_file, {
            "activeAccountNumber": None,
            "lastUpdated": "2024-01-01T00:00:00Z",
            "sequence": [1, 2],
            "accounts": {
                "1": {"email": a, "uuid": "", "organizationUuid": "",
                      "organizationName": "", "added": "2024-01-01T00:00:00Z"},
                "2": {"email": b, "uuid": "", "organizationUuid": "",
                      "organizationName": "", "added": "2024-01-01T00:00:00Z"},
            }})

        def gen(who, g):
            return json.dumps({"claudeAiOauth": {
                "accessToken": f"at-{who}-{g}", "refreshToken": f"rt-{who}-{g}"}})

        s._write_account_credentials("1", a, gen("A", "G0"))
        s._write_account_credentials("2", b, gen("B", "G0"))
        # Both profiles have RUN, so claude rotated inside them and nothing
        # syncs that back: the profile holds the only usable generation.
        for num, email, who in (("1", a, "A"), ("2", b, "B")):
            d = session_dir_for(s.backup_dir, num, email)
            d.mkdir(parents=True, exist_ok=True)
            (d / ".credentials.json").write_text(gen(who, "G1"), encoding="utf-8")

        # PREMISE: the seeded generations really differ from the backups, or
        # losing one would cost nothing and this case measures nothing.
        assert json.loads(s.read_account_credentials("1", a))[
            "claudeAiOauth"]["refreshToken"] == "rt-A-G0"

        s.swap_accounts("1", "2")

        for num, email, who in (("2", a, "A"), ("1", b, "B")):
            seed = session_dir_for(s.backup_dir, num, email) / ".credentials.json"
            assert seed.exists(), (
                f"DEFECT: the swap stripped account {who}'s session profile. "
                "The backup holds the generation claude already rotated past, "
                "so the next run POSTs a consumed refresh token and the "
                "account needs a re-login -- both slots, from one command "
                "that reported success"
            )
            assert json.loads(seed.read_text())[
                "claudeAiOauth"]["refreshToken"] == f"rt-{who}-G1"
