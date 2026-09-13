"""Audit F04 regressions: account mutations are serialized transactions.

The canonical account lock (``switcher.lock_file``) serializes switching
and refresh; add/remove/import must join the same transaction instead of
read-modify-writing the roster around it. The two reproduced failures:

* a removal completing while another writer held the canonical lock, and
* a concurrent alias update landing during the removal's confirmation
  wait being silently overwritten by the stale roster rewrite.

Both are pinned here: removal blocks while the lock is foreign-held, and
a roster change that lands during the confirmation window survives.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from claude_swap.locking import FileLock
from claude_swap.switcher import ClaudeAccountSwitcher

from tests.test_transfer import SAMPLE_CREDS, _linux_switcher, _seed_account


def _seeded(home: Path, count: int = 3) -> ClaudeAccountSwitcher:
    sw = _linux_switcher(home)
    for i in range(1, count + 1):
        _seed_account(sw, i, f"acct{i}@example.com", alias=f"acct{i}")
    data = sw._get_sequence_data()
    data["activeAccountNumber"] = 1
    sw._write_json(sw.sequence_file, data)
    return sw


class TestRemoveSerialization:
    def test_remove_blocks_while_canonical_lock_is_held(self, temp_home: Path):
        sw = _seeded(temp_home)
        with FileLock(sw.lock_file):
            done = threading.Event()
            error: list = []

            def worker():
                try:
                    sw.remove_account("2", assume_yes=True)
                except Exception as e:  # noqa: BLE001
                    error.append(e)
                finally:
                    done.set()

            t = threading.Thread(target=worker)
            t.start()
            # The removal must NOT complete under a foreign-held lock.
            # FileLock waits up to its timeout, so give it a moment.
            assert not done.wait(timeout=2.0), (
                "remove completed while the canonical account lock was "
                "held by another writer"
            )
            # roster untouched while blocked
            assert "2" in sw._get_sequence_data()["accounts"]
        t.join(timeout=15)
        assert not error, error
        assert done.wait(timeout=5)
        assert "2" not in sw._get_sequence_data()["accounts"]

    def test_concurrent_alias_change_survives_removal(
            self, temp_home: Path, monkeypatch):
        """The audit's lost update: another frontend renames account 3's
        alias while the removal of account 2 waits at its confirmation.
        The stale pre-confirmation snapshot must never overwrite it."""
        sw = _seeded(temp_home)

        real_input = input

        def confirming_input(_prompt):
            # The concurrent writer lands here — inside the confirmation
            # window, before the removal takes the lock.
            data = sw._get_sequence_data()
            data["accounts"]["3"]["alias"] = "renamed-during-confirm"
            sw._write_json(sw.sequence_file, data)
            return "y"

        monkeypatch.setattr("builtins.input", confirming_input)
        _ = real_input

        sw.remove_account("2")

        final = sw._get_sequence_data()
        assert "2" not in final["accounts"]
        assert final["accounts"]["3"]["alias"] == "renamed-during-confirm", (
            "the concurrent alias update was overwritten by the removal's "
            "stale roster snapshot"
        )

    def test_removal_aborts_when_slot_identity_changed_during_confirm(
            self, temp_home: Path, monkeypatch):
        sw = _seeded(temp_home)

        def swapping_input(_prompt):
            # Slot 2 is replaced by a different account during the
            # confirmation window: the user approved removing acct2, not
            # whoever occupies the slot now.
            data = sw._get_sequence_data()
            data["accounts"]["2"] = {
                "email": "someone-else@example.com", "uuid": "other-uuid",
                "organizationUuid": "", "organizationName": "",
                "added": "2026-09-12T00:00:00Z",
            }
            sw._write_json(sw.sequence_file, data)
            return "y"

        monkeypatch.setattr("builtins.input", swapping_input)
        with pytest.raises(Exception, match="changed while"):
            sw.remove_account("2")
        # The new occupant survives untouched.
        final = sw._get_sequence_data()
        assert final["accounts"]["2"]["email"] == "someone-else@example.com"


class TestAddImportSerialization:
    def test_token_add_runs_under_canonical_lock(self, temp_home: Path):
        """A concurrent roster write during the add's write phase must not
        be lost: the tail (credential/config writes + roster rewrite)
        re-reads the roster inside the lock."""
        sw = _seeded(temp_home)
        token = "sk-ant-api03-synthetic-token-for-audit-f04"

        original_write_creds = sw._write_account_credentials

        def interleaving_write_creds(num, email, creds):
            # A concurrent writer lands between the add's credential write
            # and its roster rewrite (the old unlocked window).
            data = sw._get_sequence_data()
            data["accounts"]["3"]["alias"] = "renamed-during-add"
            sw._write_json(sw.sequence_file, data)
            return original_write_creds(num, email, creds)

        sw._write_account_credentials = interleaving_write_creds
        try:
            sw.add_account_from_token(token=token, email="new@example.com")
        finally:
            sw._write_account_credentials = original_write_creds

        final = sw._get_sequence_data()
        assert final["accounts"]["3"]["alias"] == "renamed-during-add", (
            "the add's roster rewrite lost a concurrent alias update"
        )
        assert "new@example.com" in [
            a["email"] for a in final["accounts"].values()
        ]

    def test_import_write_pass_holds_canonical_lock(self, temp_home: Path):
        """The import's multi-step writes hold the canonical lock: a
        mutation attempted inside the window (from another thread) blocks
        until the import commits, so no partial interleaving is possible."""
        sw = _seeded(temp_home)
        bundle = temp_home / "bundle.cswap"
        bundle.write_text(json.dumps({
            "version": 1,
            "exportedAt": "2026-09-12T00:00:00Z",
            "accounts": [{
                "email": "imported@example.com", "number": 9, "uuid": "u-9",
                "organizationUuid": "", "added": "2026-09-12T00:00:00Z",
                "credentials": SAMPLE_CREDS,
                "config": {"oauthAccount": {
                    "emailAddress": "imported@example.com"}},
            }],
        }), encoding="utf-8")

        from claude_swap.transfer import import_accounts

        saw_lock_free: list[bool] = []
        original_write_config = sw._write_account_config

        def probing_write_config(num, email, config):
            # Inside the import's write pass: try a non-blocking lock
            # grab — it must fail because the import holds it.
            lock = FileLock(sw.lock_file, timeout=0.1)
            acquired = lock.acquire(timeout=0.1)
            saw_lock_free.append(acquired)
            if acquired:
                lock.release()
            return original_write_config(num, email, config)

        sw._write_account_config = probing_write_config
        try:
            import_accounts(sw, str(bundle))
        finally:
            sw._write_account_config = original_write_config

        assert saw_lock_free and not any(saw_lock_free), (
            "the import write pass ran without holding the canonical lock"
        )
        final = sw._get_sequence_data()
        assert "imported@example.com" in [
            a["email"] for a in final["accounts"].values()
        ]
