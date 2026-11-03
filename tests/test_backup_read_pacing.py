"""Backup Keychain reads are paced: a failure cooldown and a display read cache.

Issue #496: the TUI repaints every 3 s and every pass spawned ``security``
twice per idle slot (three times against a locked Keychain) — ~8 spawns/s at
11 accounts, and once the login keychain locked, a stream of unlock dialogs
each killed by the 5 s timeout. Two mechanisms in ``CredentialStore`` fix it,
and these tests pin their contract:

- ``_kc_read_backup`` honours ``_keychain_disabled_until``: no spawn inside the
  cooldown, pinned file mode exempt, the cache consulted first;
- ``cached=True`` reads are served from a per-slot cache (TTL; dropped after
  own writes/deletes and on any ``sequence.json`` change; failures never
  cached; a fill that raced a drop is discarded) and only display/eligibility
  sites opt in — in particular the strike scan re-reads fresh before it can
  heal a quarantine.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import pytest

from claude_swap import macos_keychain, oauth
from claude_swap.credentials import CLAUDE_CODE_KEYCHAIN_SERVICE, SECURITY_SERVICE
from claude_swap.json_output import (
    USAGE_KEYCHAIN_UNAVAILABLE,
    USAGE_RELOGIN_REQUIRED,
)
from claude_swap.macos_keychain import KeychainError
from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.usage_store import FetchRecord


def _cred(tag: str) -> str:
    return json.dumps({
        "claudeAiOauth": {
            "accessToken": f"sk-{tag}",
            "refreshToken": f"rt-{tag}",
            "expiresAt": 9999999999000,
        }
    })


def _email(num: int) -> str:
    return f"user{num}@example.com"


def _backup_key(num: int) -> tuple[str, str]:
    return (SECURITY_SERVICE, f"account-{num}-{_email(num)}")


class _Keychain:
    """Counts every ``get_password`` call and can fail selected ones."""

    def __init__(self, fake):
        self._fake = fake
        self.calls: list[tuple[str, str]] = []
        self.fail = None  # callable(service, account) -> bool

    def get_password(self, service: str, account: str):
        self.calls.append((service, account))
        if self.fail is not None and self.fail(service, account):
            raise KeychainError("locked (simulated)")
        return self._fake(service, account)

    def backup_calls(self, num: int) -> int:
        return self.calls.count(_backup_key(num))

    def live_calls(self) -> int:
        return sum(1 for s, _ in self.calls if s == CLAUDE_CODE_KEYCHAIN_SERVICE)


@pytest.fixture
def keychain(monkeypatch, block_real_keychain) -> _Keychain:
    kc = _Keychain(macos_keychain.get_password)  # the conftest in-memory fake
    monkeypatch.setattr(macos_keychain, "get_password", kc.get_password)
    return kc


def _switcher(temp_home: Path, block_real_keychain, n: int) -> ClaudeAccountSwitcher:
    """``n`` managed accounts, slot 1 active, Keychain-only backups."""
    (temp_home / ".claude.json").write_text(json.dumps({
        "oauthAccount": {
            "emailAddress": _email(1),
            "organizationUuid": "org-1",
            "accountUuid": "acct-1",
        }
    }))
    s = ClaudeAccountSwitcher()
    s.platform = Platform.MACOS
    s._setup_directories()
    s._write_json(s.sequence_file, {
        "activeAccountNumber": 1,
        "lastUpdated": "2024-01-01T00:00:00Z",
        "sequence": list(range(1, n + 1)),
        "accounts": {
            str(i): {
                "email": _email(i),
                "uuid": f"acct-{i}",
                "organizationUuid": f"org-{i}",
                "organizationName": "Org",
                "added": "2024-01-01T00:00:00Z",
            }
            for i in range(1, n + 1)
        },
    })
    for i in range(1, n + 1):
        (s.configs_dir / f".claude-config-{i}-{_email(i)}.json").write_text("{}")
        block_real_keychain.data[_backup_key(i)] = _cred(f"backup{i}")
    block_real_keychain.data[
        (CLAUDE_CODE_KEYCHAIN_SERVICE, macos_keychain.keychain_account_name())
    ] = _cred("live1")
    assert not list(s.credentials_dir.glob("*.enc")), "backups are Keychain-only"
    return s


class TestBackupReadCooldown:
    def test_a_failed_backup_read_backs_off_the_next_one(
        self, temp_home: Path, block_real_keychain, keychain: _Keychain
    ):
        """Inside the cooldown a backup read reports unreadable WITHOUT a spawn."""
        s = _switcher(temp_home, block_real_keychain, 2)
        store = s._store
        keychain.fail = lambda *_: True
        assert store._read_account_credentials_ex("2", _email(2)) == ("", True)
        assert keychain.backup_calls(2) == 1

        keychain.fail = None  # the Keychain recovers at once...
        assert store._read_account_credentials_ex("2", _email(2)) == ("", True)
        assert keychain.backup_calls(2) == 1, (
            "a backup read inside the cooldown spawned `security`"
        )

        store._keychain_disabled_until = time.monotonic() - 1  # ...cooldown lapses
        value, unreadable = store._read_account_credentials_ex("2", _email(2))
        assert (value, unreadable) == (_cred("backup2"), False)
        assert keychain.backup_calls(2) == 2

    def test_the_active_reads_failure_backs_off_backup_reads_too(
        self, temp_home: Path, block_real_keychain, keychain: _Keychain
    ):
        """One deadline for the whole Keychain, not one per item."""
        s = _switcher(temp_home, block_real_keychain, 2)
        keychain.fail = lambda service, _a: service == CLAUDE_CODE_KEYCHAIN_SERVICE
        assert s._store._read_active_credentials().keychain_unavailable is True
        keychain.fail = None
        assert s._store._read_account_credentials_ex("2", _email(2)) == ("", True)
        assert keychain.backup_calls(2) == 0

    @pytest.mark.parametrize("residual_cleared", [True, False])
    def test_pinned_file_mode_does_not_block_backup_reads(
        self, temp_home: Path, block_real_keychain, keychain: _Keychain,
        residual_cleared: bool,
    ):
        """A pin zeroes the deadline: the backup is Keychain-only by design."""
        s = _switcher(temp_home, block_real_keychain, 2)
        store = s._store
        store._pin_file_mode(residual_cleared=residual_cleared)
        assert store._keychain_disabled_until == 0.0
        assert store._read_account_credentials("2", _email(2)) == _cred("backup2")
        assert keychain.backup_calls(2) == 1

    def test_a_cooldown_is_announced_once_and_its_skips_are_silent(
        self, temp_home: Path, block_real_keychain, keychain: _Keychain, caplog
    ):
        """Skipped reads are expected, not failures: no per-lookup warning
        (31 per snapshot at 11 accounts, ~600/min on the 3 s tick), one info
        line per cooldown, and the verdict stays unreadable."""
        s = _switcher(temp_home, block_real_keychain, 5)
        keychain.fail = lambda *_: True
        with caplog.at_level(logging.INFO, logger="claude-swap"):
            for _ in range(2):
                snap = s.accounts_snapshot(fetch=set())
            idle = [a for a in snap.accounts if not a.is_active]
            assert [a.usage.sentinel for a in idle] == [USAGE_KEYCHAIN_UNAVAILABLE] * 4
            skipped_as_failures = [
                r for r in caplog.records
                if r.levelno >= logging.WARNING
                and "Failed to read credentials from Keychain" in r.getMessage()
            ]
            assert skipped_as_failures == [], (
                f"{len(skipped_as_failures)} cooldown skips were logged as failures"
            )
            announced = [
                r for r in caplog.records if "Keychain backed off" in r.getMessage()
            ]
            assert len(announced) == 1, "one announcement per cooldown"

            # The cooldown lapses, the re-probe fails again: a new cooldown,
            # announced again — once.
            s._store._keychain_disabled_until = time.monotonic() - 1
            s.accounts_snapshot(fetch=set())
            s.accounts_snapshot(fetch=set())
            announced = [
                r for r in caplog.records if "Keychain backed off" in r.getMessage()
            ]
            assert len(announced) == 2


class TestBackupReadCache:
    def test_cached_reads_spawn_once_per_slot_until_the_ttl(
        self, temp_home: Path, block_real_keychain, keychain: _Keychain, monkeypatch
    ):
        s = _switcher(temp_home, block_real_keychain, 2)
        store = s._store
        for _ in range(3):
            assert store._read_account_credentials(
                "2", _email(2), cached=True
            ) == _cred("backup2")
        assert keychain.backup_calls(2) == 1

        monkeypatch.setattr("claude_swap.credentials.BACKUP_READ_CACHE_TTL_S", 0.0)
        assert store._read_account_credentials("2", _email(2), cached=True) == (
            _cred("backup2")
        )
        assert keychain.backup_calls(2) == 2, "an expired entry must be re-read"

    def test_the_default_read_always_spawns(
        self, temp_home: Path, block_real_keychain, keychain: _Keychain
    ):
        """Consumers (gate, switch, resync, export…) never see the cache."""
        s = _switcher(temp_home, block_real_keychain, 2)
        store = s._store
        store._read_account_credentials("2", _email(2), cached=True)
        store._read_account_credentials("2", _email(2))
        store._read_account_credentials_ex("2", _email(2))
        assert keychain.backup_calls(2) == 3
        store._read_account_credentials("2", _email(2), cached=True)
        assert keychain.backup_calls(2) == 3

    def test_own_write_and_delete_drop_the_cache(
        self, temp_home: Path, block_real_keychain, keychain: _Keychain
    ):
        s = _switcher(temp_home, block_real_keychain, 2)
        store = s._store
        assert store._read_account_credentials("2", _email(2), cached=True) == (
            _cred("backup2")
        )
        store._write_account_credentials("2", _email(2), _cred("rotated"))
        before = keychain.backup_calls(2)
        assert store._read_account_credentials("2", _email(2), cached=True) == (
            _cred("rotated")
        ), "read-after-own-write served the superseded generation"
        assert keychain.backup_calls(2) == before + 1
        assert store._read_account_credentials("2", _email(2), cached=True) == (
            _cred("rotated")
        )
        assert keychain.backup_calls(2) == before + 1

        store._delete_account_credentials("2", _email(2))
        before = keychain.backup_calls(2)
        assert store._read_account_credentials("2", _email(2), cached=True) == ""
        assert keychain.backup_calls(2) == before + 1

    def test_a_registry_change_drops_the_cache(
        self, temp_home: Path, block_real_keychain, keychain: _Keychain
    ):
        """Any add/remove/swap/move/switch/import — from any process — rewrites
        ``sequence.json``; a cached read notices even when the rewrite is the
        same length and lands on the same coarse mtime tick (the atomic write
        still replaces the inode)."""
        s = _switcher(temp_home, block_real_keychain, 2)
        store = s._store
        store._read_account_credentials("2", _email(2), cached=True)
        store._read_account_credentials("2", _email(2), cached=True)
        assert keychain.backup_calls(2) == 1

        mtime_ns = s.sequence_file.stat().st_mtime_ns
        data = s._get_sequence_data()
        data["lastUpdated"] = "2024-06-01T00:00:00Z"
        s._write_json(s.sequence_file, data)
        os.utime(s.sequence_file, ns=(mtime_ns, mtime_ns))
        store._read_account_credentials("2", _email(2), cached=True)
        assert keychain.backup_calls(2) == 2

    def test_a_failed_read_is_never_cached(
        self, temp_home: Path, block_real_keychain, keychain: _Keychain
    ):
        s = _switcher(temp_home, block_real_keychain, 2)
        store = s._store
        keychain.fail = lambda *_: True
        assert store._read_account_credentials_ex("2", _email(2), cached=True) == (
            "", True
        )
        keychain.fail = None
        store._keychain_disabled_until = time.monotonic() - 1
        assert store._read_account_credentials_ex("2", _email(2), cached=True) == (
            _cred("backup2"), False
        ), "the failure was served from the cache as an absent backup"
        assert keychain.backup_calls(2) == 2

    def test_a_cached_hit_is_served_during_the_cooldown(
        self, temp_home: Path, block_real_keychain, keychain: _Keychain
    ):
        """A transient failure must not blank every idle row for a minute;
        a COLD slot, though, reports unreadable without a spawn."""
        s = _switcher(temp_home, block_real_keychain, 3)
        store = s._store
        assert store._read_account_credentials("2", _email(2), cached=True) == (
            _cred("backup2")
        )
        keychain.fail = lambda *_: True
        with pytest.raises(KeychainError):
            store._kc_read_backup("9", "nobody@example.com")  # arms the cooldown
        keychain.fail = None

        assert store._read_account_credentials_ex("2", _email(2), cached=True) == (
            _cred("backup2"), False
        )
        assert keychain.backup_calls(2) == 1
        assert store._read_account_credentials_ex("3", _email(3), cached=True) == (
            "", True
        )
        assert keychain.backup_calls(3) == 0

    def test_a_drop_during_the_fill_wins(
        self, temp_home: Path, block_real_keychain, keychain: _Keychain, monkeypatch
    ):
        """A value read before a concurrent drop landed must not be stored."""
        s = _switcher(temp_home, block_real_keychain, 2)
        store = s._store
        inner = macos_keychain.get_password

        def dropping(service, account):
            value = inner(service, account)
            store._drop_backup_read_cache()  # lands while `security` runs
            return value

        monkeypatch.setattr(macos_keychain, "get_password", dropping)
        assert store._read_account_credentials("2", _email(2), cached=True) == (
            _cred("backup2")
        )
        monkeypatch.setattr(macos_keychain, "get_password", inner)
        store._read_account_credentials("2", _email(2), cached=True)
        assert keychain.backup_calls(2) == 2, (
            "a fill that raced a drop was kept — it may predate the mutation"
        )

    def test_a_write_landing_after_a_racing_fill_is_not_shadowed(
        self, temp_home: Path, block_real_keychain, keychain: _Keychain, monkeypatch
    ):
        """The ordering a pre-op drop cannot see: drop → a miss reads the OLD
        item and stores it → the write lands. The drop runs after the op."""
        s = _switcher(temp_home, block_real_keychain, 2)
        store = s._store
        inner_set = macos_keychain.set_password

        def racing_set(service, account, password):
            # A display read slips in between the write's start and its
            # landing; it sees (and would cache) the old generation.
            assert store._read_account_credentials(
                "2", _email(2), cached=True
            ) == _cred("backup2")
            inner_set(service, account, password)

        monkeypatch.setattr(macos_keychain, "set_password", racing_set)
        store._kc_write_backup("2", _email(2), _cred("rotated"))
        assert store._read_account_credentials("2", _email(2), cached=True) == (
            _cred("rotated")
        ), "the cache shadowed a write that landed after a racing fill"


class TestDisplaySitesUseTheCache:
    def test_a_snapshot_pass_spawns_once_per_slot_then_only_the_live_read(
        self, temp_home: Path, block_real_keychain, keychain: _Keychain
    ):
        """The probe from #496 as a test: 2N+1 spawns per pass became N+1 on
        the first pass and 1 (the live item) on every pass within the TTL."""
        n = 5
        s = _switcher(temp_home, block_real_keychain, n)
        snap = s.accounts_snapshot(fetch=set())
        assert [a.switchable for a in snap.accounts] == [True] * n
        assert [a.usage.sentinel for a in snap.accounts] == [None] * n
        assert keychain.live_calls() == 1
        # N-1 idle backups (_build_accounts_info) + the active slot's backup
        # (_account_is_switchable). Not N+2: with no strike on the row the
        # strike scan reads nothing (it used to read the active backup every
        # pass).
        assert sum(keychain.backup_calls(i) for i in range(1, n + 1)) == n
        assert keychain.backup_calls(1) == 1

        keychain.calls.clear()
        snap = s.accounts_snapshot(fetch=set())
        assert [a.switchable for a in snap.accounts] == [True] * n
        assert keychain.calls == [
            (CLAUDE_CODE_KEYCHAIN_SERVICE, macos_keychain.keychain_account_name())
        ], "a repaint within the TTL must spawn for the live item only"

    def test_a_locked_keychain_is_probed_once_per_cooldown(
        self, temp_home: Path, block_real_keychain, keychain: _Keychain
    ):
        """Locked from the start (cold cache): the active read's two attempts
        arm the cooldown and nothing else spawns; every idle row says
        'keychain unavailable'; the next pass spawns nothing at all."""
        n = 5
        s = _switcher(temp_home, block_real_keychain, n)
        keychain.fail = lambda *_: True
        snap = s.accounts_snapshot(fetch=set())
        assert len(keychain.calls) <= 3, keychain.calls
        assert sum(keychain.backup_calls(i) for i in range(2, n + 1)) <= 1
        idle = [a for a in snap.accounts if not a.is_active]
        assert [a.usage.sentinel for a in idle] == [USAGE_KEYCHAIN_UNAVAILABLE] * (n - 1)
        assert [a.switchable for a in idle] == [False] * (n - 1)

        keychain.calls.clear()
        s.accounts_snapshot(fetch=set())
        assert keychain.calls == [], "inside the cooldown a pass must not spawn"


class TestStrikeBindingReadsFresh:
    """A display-cached fingerprint may over-hold a strike; it must never
    heal one — ``clear_dead_token`` is a persisted write that also lifts the
    backoff, so a stale heal re-arms a dead POST every pass."""

    def _struck(self, s: ClaudeAccountSwitcher, fp: str) -> dict:
        identities = {"2": (_email(2), "org-2")}
        s._usage_store.record(
            {"2": FetchRecord(error="invalid_grant", struck_fp=fp)}, identities
        )
        assert s._usage_store.entries(identities, [])["2"].token_dead()
        return identities

    def _spy_clear(self, s: ClaudeAccountSwitcher, monkeypatch) -> list:
        calls: list = []
        real = s._usage_store.clear_dead_token

        def spy(nums, identities):
            calls.append(list(nums))
            return real(nums, identities)

        monkeypatch.setattr(s._usage_store, "clear_dead_token", spy)
        return calls

    def test_a_stale_cached_fingerprint_cannot_heal_a_strike(
        self, temp_home: Path, block_real_keychain, keychain: _Keychain, monkeypatch
    ):
        s = _switcher(temp_home, block_real_keychain, 2)
        s.accounts_snapshot(fetch=set())  # the cache now holds generation A
        # Another process rotates the slot to B without touching the
        # registry, and B is struck (the shared usage store).
        block_real_keychain.data[_backup_key(2)] = _cred("B")
        identities = self._struck(s, oauth.credential_fingerprint(_cred("B")))
        cleared = self._spy_clear(s, monkeypatch)

        snap = s.accounts_snapshot(fetch=set())
        row = {a.number: a for a in snap.accounts}["2"]
        assert row.usage.sentinel == USAGE_RELOGIN_REQUIRED, (
            "a cached fingerprint healed a strike bound to a generation it "
            "cannot see"
        )
        assert cleared == []
        assert s._usage_store.entries(identities, [])["2"].auth_dead_strikes > 0

    def test_a_replaced_backup_heals_with_one_fresh_read(
        self, temp_home: Path, block_real_keychain, keychain: _Keychain, monkeypatch
    ):
        """Control: the strike is bound to neither the cached nor the stored
        generation — healed, on the strength of exactly one fresh read."""
        s = _switcher(temp_home, block_real_keychain, 2)
        s.accounts_snapshot(fetch=set())
        block_real_keychain.data[_backup_key(2)] = _cred("B")
        identities = self._struck(s, "sha256:neither-a-nor-b")
        cleared = self._spy_clear(s, monkeypatch)
        keychain.calls.clear()

        snap = s.accounts_snapshot(fetch=set())
        row = {a.number: a for a in snap.accounts}["2"]
        assert row.usage.sentinel is None
        assert cleared == [["2"]]
        assert s._usage_store.entries(identities, [])["2"].auth_dead_strikes == 0
        assert keychain.backup_calls(2) == 1, "healing takes one fresh read"

    def test_a_matching_cached_fingerprint_holds_without_a_read(
        self, temp_home: Path, block_real_keychain, keychain: _Keychain, monkeypatch
    ):
        """Control: holding needs no fresh read — the cached source still
        carries the struck generation."""
        s = _switcher(temp_home, block_real_keychain, 2)
        s.accounts_snapshot(fetch=set())
        self._struck(s, oauth.credential_fingerprint(_cred("backup2")))
        cleared = self._spy_clear(s, monkeypatch)
        keychain.calls.clear()

        snap = s.accounts_snapshot(fetch=set())
        row = {a.number: a for a in snap.accounts}["2"]
        assert row.usage.sentinel == USAGE_RELOGIN_REQUIRED
        assert cleared == []
        assert keychain.backup_calls(2) == 0
