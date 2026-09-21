"""Access-token-only session credentials: builder and writer (Refs #382)."""

from __future__ import annotations

import json
import os
import shutil
import sys
import time

import pytest

from claude_swap import claude_locks, macos_keychain, oauth
from claude_swap import managed_sessions as ms
from claude_swap import session_credentials as sc
from claude_swap.exceptions import LockError
from claude_swap.locking import FileLock
from claude_swap.managed_sessions import AccountRef, ManagedSessionRegistry, create_managed_profile
from claude_swap.models import Platform
from claude_swap.session import keychain_service_name

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="managed sessions are POSIX-only (v1)"
)

BACKUP = json.dumps({"claudeAiOauth": {
    "accessToken": "at-2",
    "refreshToken": "rt-2",
    "expiresAt": 1_900_000_000_000,
    "scopes": ["user:inference", "user:profile"],
    "subscriptionType": "max",
    "rateLimitTier": "default_claude_max_20x",
}})


class TestBuildAccessCredential:
    def test_keeps_access_fields_and_drops_refresh_token(self):
        assert json.loads(sc.build_access_credential(BACKUP)) == {"claudeAiOauth": {
            "accessToken": "at-2",
            "expiresAt": 1_900_000_000_000,
            "scopes": ["user:inference", "user:profile"],
            "subscriptionType": "max",
            "rateLimitTier": "default_claude_max_20x",
        }}

    def test_drops_unknown_null_and_sibling_fields(self):
        raw = json.dumps({
            "claudeAiOauth": {
                "accessToken": "at", "refreshToken": "rt",
                "refreshTokenExpiresAt": 5, "subscriptionType": None,
            },
            "mcpOAuth": {"srv": {"accessToken": "mcp"}},
        })
        assert json.loads(sc.build_access_credential(raw)) == {
            "claudeAiOauth": {"accessToken": "at"}
        }

    @pytest.mark.parametrize("raw", [
        "", "not json", "sk-ant-api03-xyz", "[1, 2]",
        json.dumps({"claudeAiOauth": {"refreshToken": "rt"}}),
        json.dumps({"claudeAiOauth": {"accessToken": ""}}),
    ])
    def test_rejects_credentials_without_access_token(self, raw):
        with pytest.raises(ValueError):
            sc.build_access_credential(raw)


SID = "auto-0000beef"
ACCOUNT = AccountRef("b@example.com", "org-2")
OAUTH_ACCOUNT = {
    "emailAddress": "b@example.com", "accountUuid": "uuid-2", "organizationUuid": "org-2",
}


@pytest.fixture
def registry(tmp_path):
    return ManagedSessionRegistry(tmp_path / "backup")


@pytest.fixture
def session_dir(registry):
    registry.allocate(SID, lambda busy: (ACCOUNT, "backup"), pid=os.getpid(), proc_start=None)
    d = registry.session_dir(SID)
    create_managed_profile(d, theme="dark")
    return d


def _write(session_dir, registry, credential=None, *, platform=Platform.MACOS, **kw):
    return sc.write_session_credential(
        session_dir, ACCOUNT, credential or sc.build_access_credential(BACKUP),
        OAUTH_ACCOUNT, registry=registry, platform=platform, **kw,
    )


def _kc_key(session_dir):
    return (keychain_service_name(session_dir), macos_keychain.keychain_account_name())


def _plaintext_token(session_dir) -> str:
    return json.loads((session_dir / ".credentials.json").read_text())["claudeAiOauth"]["accessToken"]


class TestWriter:
    def test_keychain_and_plaintext_hold_the_same_access_only_credential(
        self, session_dir, registry, block_real_keychain
    ):
        result = _write(session_dir, registry)
        assert (result.ok, result.reason, result.keychain_written) == (True, "ok", True)
        plaintext = (session_dir / ".credentials.json").read_text()
        assert block_real_keychain.data[_kc_key(session_dir)] == plaintext
        blob = json.loads(plaintext)["claudeAiOauth"]
        assert blob["accessToken"] == "at-2"
        assert "refreshToken" not in blob
        assert result.fingerprint == oauth.access_token_fingerprint(plaintext)
        assert (session_dir / ".credentials.json").stat().st_mode & 0o777 == 0o600

    def test_refuses_a_credential_carrying_a_refresh_token(
        self, session_dir, registry, block_real_keychain
    ):
        result = _write(session_dir, registry, BACKUP)
        assert (result.ok, result.reason) == (False, "refresh-token-refused")
        assert not (session_dir / ".credentials.json").exists()
        assert _kc_key(session_dir) not in block_real_keychain.data

    @pytest.mark.parametrize("raw", ["not json", "[1, 2]", json.dumps({"claudeAiOauth": {}})])
    def test_refuses_a_credential_without_access_token(self, session_dir, registry, raw):
        result = _write(session_dir, registry, raw)
        assert (result.ok, result.reason) == (False, "invalid-credential")
        assert not (session_dir / ".credentials.json").exists()

    def test_refuses_an_oauth_account_of_another_identity(self, session_dir, registry):
        result = sc.write_session_credential(
            session_dir, ACCOUNT, sc.build_access_credential(BACKUP),
            {**OAUTH_ACCOUNT, "emailAddress": "x@example.com"},
            registry=registry, platform=Platform.MACOS,
        )
        assert (result.ok, result.reason) == (False, "identity-mismatch")
        assert not (session_dir / ".credentials.json").exists()

    def test_mtime_changes_even_within_one_clock_tick(self, session_dir, registry):
        creds = session_dir / ".credentials.json"
        creds.write_text("{}")
        future = time.time_ns() + 50 * 1_000_000_000
        os.utime(creds, ns=(future, future))
        assert _write(session_dir, registry).ok
        first = creds.stat().st_mtime_ns
        assert first > future
        assert _write(session_dir, registry).ok
        assert creds.stat().st_mtime_ns > first

    def test_oauth_account_is_spliced_and_other_keys_kept(self, session_dir, registry):
        (session_dir / ".claude.json").write_text(json.dumps({
            "projects": {"/x": {"a": 1}}, "theme": "light",
            "oauthAccount": {"emailAddress": "old@example.com"},
        }))
        assert _write(session_dir, registry).ok
        config = json.loads((session_dir / ".claude.json").read_text())
        assert config["oauthAccount"] == OAUTH_ACCOUNT
        assert config["projects"] == {"/x": {"a": 1}}
        assert config["theme"] == "light"

    @pytest.mark.parametrize("content", [b'{"projects": {"/x"', b"\xff\xfe", b"[1, 2]"])
    def test_refuses_to_replace_an_unreadable_config(
        self, session_dir, registry, block_real_keychain, content
    ):
        """A torn/corrupt .claude.json holds the user's whole Claude state
        (projects, mcpServers); splicing into ``{}`` would erase it. Refused
        before any credential store is touched, so no half-write either."""
        config = session_dir / ".claude.json"
        config.write_bytes(content)
        result = _write(session_dir, registry)
        assert (result.ok, result.reason) == (False, "config-unreadable")
        assert config.read_bytes() == content
        assert not (session_dir / ".credentials.json").exists()
        assert _kc_key(session_dir) not in block_real_keychain.data

    def test_aborts_without_registry_entry(self, session_dir, registry):
        registry.remove(SID)
        assert (_write(session_dir, registry).reason) == "no-registry-entry"
        assert not (session_dir / ".credentials.json").exists()

    def test_aborts_when_pid_was_recycled(self, session_dir, registry, monkeypatch):
        monkeypatch.setattr(ms, "pid_matches_record", lambda pid, stamp: False)
        assert _write(session_dir, registry).reason == "session-ended"
        assert not (session_dir / ".credentials.json").exists()

    def test_aborts_without_recreating_a_removed_profile(self, session_dir, registry):
        shutil.rmtree(session_dir)
        assert _write(session_dir, registry).reason == "no-session-dir"
        assert not session_dir.exists()

    def test_post_check_mismatch_reports_failure_and_leaves_files(
        self, session_dir, registry, monkeypatch
    ):
        other = json.dumps({"claudeAiOauth": {"accessToken": "someone-else"}})
        monkeypatch.setattr(sc, "_read_back_stores", lambda d, p: (other, other))
        result = _write(session_dir, registry)
        assert (result.ok, result.reason) == (False, "post-check-mismatch")
        assert _plaintext_token(session_dir) == "at-2"

    @pytest.mark.parametrize("require_keychain, ok", [(True, False), (False, True)])
    def test_keychain_failure_writes_plaintext_and_clears_stale_item(
        self, session_dir, registry, block_real_keychain, monkeypatch,
        require_keychain, ok,
    ):
        """A stale item left behind would shadow the plaintext, so it is
        cleared either way. Whether a profile carrying no item of this
        call's counts as written is the caller's call: a refresh over an
        existing profile requires the item, seeding a new one does not."""
        block_real_keychain.data[_kc_key(session_dir)] = json.dumps(
            {"claudeAiOauth": {"accessToken": "stale"}}
        )

        def locked(service, account, password):
            raise macos_keychain.KeychainError("locked")

        monkeypatch.setattr(macos_keychain, "set_password", locked)
        result = _write(session_dir, registry, require_keychain=require_keychain)
        assert (result.ok, result.reason, result.keychain_written) == (
            ok, "keychain-write-failed", False,
        )
        assert _kc_key(session_dir) not in block_real_keychain.data
        assert _plaintext_token(session_dir) == "at-2"

    def test_session_owned_mcp_oauth_survives(self, session_dir, registry, block_real_keychain):
        block_real_keychain.data[_kc_key(session_dir)] = json.dumps({
            "claudeAiOauth": {"accessToken": "old"},
            "mcpOAuth": {"srv": {"accessToken": "mcp"}},
        })
        assert _write(session_dir, registry).ok
        stored = json.loads(block_real_keychain.data[_kc_key(session_dir)])
        assert stored["mcpOAuth"] == {"srv": {"accessToken": "mcp"}}
        assert stored["claudeAiOauth"]["accessToken"] == "at-2"
        assert "refreshToken" not in stored["claudeAiOauth"]

    def test_lock_held_by_claude_times_out_cleanly(
        self, session_dir, registry, block_real_keychain
    ):
        (session_dir / ".storage-write.lock").mkdir()
        result = _write(session_dir, registry, lock_timeout=0.2)
        assert (result.ok, result.reason) == (False, "lock-timeout")
        assert not (session_dir / ".credentials.json").exists()
        assert _kc_key(session_dir) not in block_real_keychain.data
        assert not (session_dir / ".oauth_refresh.lock").exists()

    def test_held_config_lock_times_out_before_any_store_changes(
        self, session_dir, registry, block_real_keychain
    ):
        """The .claude.json lock is taken with the credential locks, up
        front: timing out on it must not leave a new credential in the
        stores with the old oauthAccount beside it."""
        old = json.dumps({"claudeAiOauth": {"accessToken": "old"}})
        block_real_keychain.data[_kc_key(session_dir)] = old
        (session_dir / ".credentials.json").write_text(old)
        config_before = (session_dir / ".claude.json").read_text()
        (session_dir / ".claude.json.lock").mkdir()
        result = _write(session_dir, registry, lock_timeout=0.2)
        assert (result.ok, result.reason) == (False, "lock-timeout")
        assert block_real_keychain.data[_kc_key(session_dir)] == old
        assert (session_dir / ".credentials.json").read_text() == old
        assert (session_dir / ".claude.json").read_text() == config_before
        assert not (session_dir / ".oauth_refresh.lock").exists()
        assert not (session_dir / ".storage-write.lock").exists()

    def test_non_macos_writes_plaintext_only(self, session_dir, registry, block_real_keychain):
        result = _write(session_dir, registry, platform=Platform.LINUX)
        assert (result.ok, result.keychain_written) == (True, False)
        assert _kc_key(session_dir) not in block_real_keychain.data
        assert _plaintext_token(session_dir) == "at-2"


class TestWriterRegistryRace:
    """The writer vs ``ManagedSessionRegistry.sweep``: the sweep drops a dead
    row under the registry lock and deletes the profile after releasing it,
    so the writer re-checks the row under that lock right before and right
    after writing, and takes back what it wrote if the row went away."""

    def test_entry_removed_while_waiting_for_claude_locks_is_seen(
        self, session_dir, registry, block_real_keychain, monkeypatch
    ):
        real = sc.session_credential_locks

        def locks_then_sweep(config_dir, *, timeout=None):
            cm = real(config_dir, timeout=timeout)

            class _Wrapped:
                def __enter__(self):
                    cm.__enter__()
                    registry.remove(SID)  # a sweep landing while we waited

                def __exit__(self, *exc):
                    return cm.__exit__(*exc)

            return _Wrapped()

        monkeypatch.setattr(sc, "session_credential_locks", locks_then_sweep)
        result = _write(session_dir, registry)
        assert (result.ok, result.reason) == (False, "no-registry-entry")
        assert not (session_dir / ".credentials.json").exists()
        assert _kc_key(session_dir) not in block_real_keychain.data

    def _during_write(self, monkeypatch, action):
        real = sc._write_plaintext

        def write_then(path, payload):
            real(path, payload)
            action()

        monkeypatch.setattr(sc, "_write_plaintext", write_then)

    def test_entry_removed_during_write_discards_what_was_written(
        self, session_dir, registry, block_real_keychain, monkeypatch
    ):
        self._during_write(monkeypatch, lambda: registry.remove(SID))
        result = _write(session_dir, registry)
        assert (result.ok, result.reason) == (False, "registry-changed")
        assert _kc_key(session_dir) not in block_real_keychain.data
        assert not (session_dir / ".credentials.json").exists()
        assert (session_dir / ".claude.json").exists()  # never anything else

    def test_entry_reassigned_during_write_discards_what_was_written(
        self, session_dir, registry, block_real_keychain, monkeypatch
    ):
        other = AccountRef("c@example.com", "org-3")
        self._during_write(monkeypatch, lambda: registry.update(SID, account=other))
        result = _write(session_dir, registry)
        assert (result.ok, result.reason) == (False, "registry-changed")
        assert _kc_key(session_dir) not in block_real_keychain.data
        assert not (session_dir / ".credentials.json").exists()

    def test_fingerprint_bookkeeping_during_write_is_not_a_change(
        self, session_dir, registry, monkeypatch
    ):
        """A previous writer's caller recording its fingerprint is not an
        ownership change; discarding over it would strand the session."""
        self._during_write(
            monkeypatch, lambda: registry.update(SID, access_fingerprint="sha256-at:x")
        )
        assert _write(session_dir, registry).ok
        assert _plaintext_token(session_dir) == "at-2"

    def test_same_account_restamp_during_write_is_not_a_change(
        self, session_dir, registry, block_real_keychain, monkeypatch
    ):
        """pid/procStart/source/reason changes on a row that still names the
        written account are benign: the credential stays."""
        self._during_write(monkeypatch, lambda: registry.update(
            SID, pid=os.getppid(), proc_start="Mon Jan  1 00:00:00 2024",
            source="lane0", last_reason="restamp", last_assigned_at="2026-01-01T00:00:00Z",
        ))
        result = _write(session_dir, registry)
        assert (result.ok, result.reason) == (True, "ok")
        assert _plaintext_token(session_dir) == "at-2"
        assert _kc_key(session_dir) in block_real_keychain.data

    @pytest.mark.parametrize("error", [
        LockError("registry lock held"), PermissionError("registry unreadable"),
    ])
    def test_unverifiable_registry_after_write_keeps_the_stores(
        self, session_dir, registry, block_real_keychain, monkeypatch, error
    ):
        """"Could not read the row" is not "the row changed". The sweep holds
        the registry lock across a `ps` per entry and runs immediately before
        the push, so contention here is ordinary; discarding over it would
        strip a healthy live session of both its stores."""
        real = registry.get_locked
        calls = []

        def re_read_always_fails(session_id, **kw):
            calls.append(session_id)
            if len(calls) > 1:
                raise error
            return real(session_id, **kw)

        monkeypatch.setattr(registry, "get_locked", re_read_always_fails)
        result = _write(session_dir, registry)
        assert (result.ok, result.reason) == (False, "registry-unverifiable")
        assert len(calls) == 3  # pre-check, then the re-read and its one retry
        assert _plaintext_token(session_dir) == "at-2"
        assert block_real_keychain.data[_kc_key(session_dir)] == (
            session_dir / ".credentials.json"
        ).read_text()
        assert result.fingerprint == oauth.access_token_fingerprint(
            (session_dir / ".credentials.json").read_text()
        )

    @pytest.mark.parametrize("error", [
        LockError("registry lock held"), PermissionError("registry unreadable"),
    ])
    def test_a_transient_registry_re_read_failure_is_retried_once(
        self, session_dir, registry, monkeypatch, error
    ):
        """The retry runs on its own budget, so a single contended read does
        not cost the session its credential."""
        real = registry.get_locked
        calls = []

        def second_call_only_fails(session_id, **kw):
            calls.append(kw.get("timeout"))
            if len(calls) == 2:
                raise error
            return real(session_id, **kw)

        monkeypatch.setattr(registry, "get_locked", second_call_only_fails)
        result = _write(session_dir, registry)
        assert (result.ok, result.reason) == (True, "ok")
        assert calls == [sc.REGISTRY_CHECK_TIMEOUT_S] * 3
        assert _plaintext_token(session_dir) == "at-2"

    def test_discard_leaves_stores_it_did_not_write(
        self, session_dir, registry, block_real_keychain, monkeypatch
    ):
        foreign = json.dumps({"claudeAiOauth": {"accessToken": "foreign"}})

        def replaced_then_removed():
            (session_dir / ".credentials.json").write_text(foreign)
            block_real_keychain.data[_kc_key(session_dir)] = foreign
            registry.remove(SID)

        self._during_write(monkeypatch, replaced_then_removed)
        result = _write(session_dir, registry)
        assert (result.ok, result.reason) == (False, "registry-changed")
        assert block_real_keychain.data[_kc_key(session_dir)] == foreign
        assert (session_dir / ".credentials.json").read_text() == foreign

    def test_profile_swept_mid_write_takes_back_the_keychain_item(
        self, session_dir, registry, block_real_keychain, monkeypatch
    ):
        """The sweep deletes the keychain item, then the directory. A writer
        whose keychain write lands after that delete would otherwise leave
        an item nothing can ever name again once the directory is gone."""
        real = sc._write_plaintext

        def swept_then_write(path, payload):
            registry.remove(SID)
            shutil.rmtree(session_dir)
            real(path, payload)

        monkeypatch.setattr(sc, "_write_plaintext", swept_then_write)
        result = _write(session_dir, registry)
        assert (result.ok, result.reason) == (False, "registry-changed")
        assert _kc_key(session_dir) not in block_real_keychain.data
        assert not session_dir.exists()


class TestWriterHardening:
    @pytest.mark.parametrize("oauth_fields", [
        {"refreshToken": ""},
        {"refreshToken": None},
        {"refreshToken": "rt-2"},
    ])
    def test_any_refresh_token_key_is_refused(
        self, session_dir, registry, block_real_keychain, oauth_fields
    ):
        raw = json.dumps({"claudeAiOauth": {"accessToken": "at-2", **oauth_fields}})
        result = _write(session_dir, registry, raw)
        assert (result.ok, result.reason) == (False, "refresh-token-refused")
        assert not (session_dir / ".credentials.json").exists()
        assert _kc_key(session_dir) not in block_real_keychain.data

    @pytest.mark.parametrize("raw", [
        json.dumps({
            "claudeAiOauth": {"accessToken": "at-2", "refreshTokenExpiresAt": 5},
            "trustedDeviceToken": "tdt",
        }),
        json.dumps({
            "claudeAiOauth": {"accessToken": "at-2", "someFutureLineageKey": "x"},
            "mcpOAuth": {"caller": {"accessToken": "not-this-profiles"}},
        }),
    ])
    def test_only_the_access_allowlist_reaches_the_stores(
        self, session_dir, registry, block_real_keychain, raw
    ):
        assert _write(session_dir, registry, raw).ok
        expected = {"claudeAiOauth": {"accessToken": "at-2"}}
        assert json.loads(block_real_keychain.data[_kc_key(session_dir)]) == expected
        assert json.loads((session_dir / ".credentials.json").read_text()) == expected

    @pytest.mark.parametrize("require_keychain, ok", [(True, False), (False, True)])
    def test_unreadable_keychain_is_left_alone_and_the_caller_judges_it(
        self, session_dir, registry, block_real_keychain, monkeypatch,
        require_keychain, ok,
    ):
        """A read error is not an empty item: writing over it would drop the
        profile's own mcpOAuth, so the item is not touched at all, and the
        plaintext's shared fields come from the plaintext alone.

        Whether that counts as a success is the caller's call, because it
        turns on what was in the item already. Claude reads it BEFORE the
        plaintext, so a refresh over an existing profile requires it
        (ok=False, retry next pass) while seeding a brand-new profile does
        not (ok=True, plaintext-only)."""
        stored = json.dumps({
            "claudeAiOauth": {"accessToken": "old"},
            "mcpOAuth": {"srv": {"accessToken": "mcp"}},
        })
        block_real_keychain.data[_kc_key(session_dir)] = stored
        (session_dir / ".credentials.json").write_text(json.dumps({
            "claudeAiOauth": {"accessToken": "old"},
            "pluginSecrets": {"p": "s"},
        }))

        def unreadable(service, account):
            raise macos_keychain.KeychainError("timed out")

        monkeypatch.setattr(macos_keychain, "get_password", unreadable)
        result = _write(session_dir, registry, require_keychain=require_keychain)
        assert (result.ok, result.reason, result.keychain_written) == (
            ok, "keychain-unreadable", False,
        )
        assert block_real_keychain.data[_kc_key(session_dir)] == stored
        raw = (session_dir / ".credentials.json").read_text()
        plaintext = json.loads(raw)
        assert plaintext["claudeAiOauth"] == json.loads(
            sc.build_access_credential(BACKUP)
        )["claudeAiOauth"]
        assert plaintext["pluginSecrets"] == {"p": "s"}  # from the plaintext
        assert "mcpOAuth" not in plaintext  # the unreadable item was not consulted
        assert result.fingerprint == oauth.access_token_fingerprint(raw)

    def test_registry_lock_held_times_out_quickly_without_writing(
        self, session_dir, registry, block_real_keychain, monkeypatch
    ):
        """The registry read runs while Claude's storage/config locks are
        held, so it waits a short, writer-specific time, not the registry's
        10 s default."""
        monkeypatch.setattr(sc, "REGISTRY_CHECK_TIMEOUT_S", 0.2)
        blocker = FileLock(registry.root / ms.REGISTRY_LOCK_FILENAME)
        assert blocker.acquire()
        try:
            started = time.monotonic()
            result = _write(session_dir, registry)
            elapsed = time.monotonic() - started
        finally:
            blocker.release()
        assert (result.ok, result.reason) == (False, "lock-timeout")
        assert elapsed < 5
        assert not (session_dir / ".credentials.json").exists()
        assert _kc_key(session_dir) not in block_real_keychain.data

    def test_both_registry_reads_use_the_short_timeout(
        self, session_dir, registry, monkeypatch
    ):
        timeouts = []
        real = registry.get_locked

        def spy(session_id, **kw):
            timeouts.append(kw.get("timeout"))
            return real(session_id, **kw)

        monkeypatch.setattr(registry, "get_locked", spy)
        assert _write(session_dir, registry).ok
        assert timeouts == [sc.REGISTRY_CHECK_TIMEOUT_S] * 2
        assert sc.REGISTRY_CHECK_TIMEOUT_S <= 5

    def test_write_failure_with_unchanged_row_reports_the_live_keychain_item(
        self, session_dir, registry, block_real_keychain, monkeypatch
    ):
        def disk_full(path, payload):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(sc, "_write_plaintext", disk_full)
        result = _write(session_dir, registry)
        assert (result.ok, result.reason, result.keychain_written) == (
            False, "write-failed", True,
        )
        live = block_real_keychain.data[_kc_key(session_dir)]  # not discarded
        assert result.fingerprint == oauth.access_token_fingerprint(live)
        assert result.fingerprint is not None

    def test_identity_not_reading_back_is_a_post_check_mismatch(
        self, session_dir, registry, monkeypatch
    ):
        monkeypatch.setattr(sc, "read_session_identity", lambda d: ("x@example.com", "org-2"))
        result = _write(session_dir, registry)
        assert (result.ok, result.reason) == (False, "post-check-mismatch")

    def test_profile_swept_before_the_locks_is_not_resurrected(
        self, session_dir, registry, block_real_keychain, monkeypatch
    ):
        """A sweep between the existence check and the lock mkdirs would
        otherwise leave a husk directory recreated by the lock helper."""
        real = sc.session_credential_locks

        def swept_first(config_dir, *, timeout=None):
            registry.remove(SID)
            shutil.rmtree(session_dir)
            return real(config_dir, timeout=timeout)

        monkeypatch.setattr(sc, "session_credential_locks", swept_first)
        result = _write(session_dir, registry)
        assert (result.ok, result.reason) == (False, "no-session-dir")
        assert not session_dir.exists()
        assert _kc_key(session_dir) not in block_real_keychain.data

    def test_config_write_failure_after_plaintext_reports_the_live_plaintext(
        self, session_dir, registry, monkeypatch
    ):
        """No keychain on Linux: the plaintext is the live store, so a
        failure after it was written must still name what is live."""
        def config_fails(path, data):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(sc, "atomic_write_json", config_fails)
        result = _write(session_dir, registry, platform=Platform.LINUX)
        assert (result.ok, result.reason, result.keychain_written) == (
            False, "write-failed", False,
        )
        live = (session_dir / ".credentials.json").read_text()
        assert result.fingerprint == oauth.access_token_fingerprint(live)
        assert result.fingerprint is not None

    def test_profile_removed_during_lock_acquisition_is_no_session_dir(
        self, session_dir, registry, block_real_keychain, monkeypatch, caplog
    ):
        """E.g. another writer's husk cleanup: the lock helper's mkdir then
        hits a missing parent. That is a gone profile, not a write failure."""
        real_mkdir = os.mkdir

        def gone_first(path, *args, **kwargs):
            if str(path).endswith(".oauth_refresh.lock"):
                shutil.rmtree(session_dir, ignore_errors=True)
            return real_mkdir(path, *args, **kwargs)

        monkeypatch.setattr(claude_locks.os, "mkdir", gone_first)
        with caplog.at_level("WARNING", logger="claude-swap"):
            result = _write(session_dir, registry)
        assert (result.ok, result.reason) == (False, "no-session-dir")
        assert "write failed" not in caplog.text
        assert not session_dir.exists()
        assert _kc_key(session_dir) not in block_real_keychain.data

    def test_write_error_on_an_intact_profile_is_a_write_failure(
        self, session_dir, registry, block_real_keychain, monkeypatch, caplog
    ):
        """The held fd proves the directory intact, so an OSError is a real
        write failure even if a path check would (wrongly) say it is gone."""
        real_mkdir = os.mkdir

        def denied(path, *args, **kwargs):
            if str(path).endswith(".oauth_refresh.lock"):
                raise PermissionError(13, "Permission denied", str(path))
            return real_mkdir(path, *args, **kwargs)

        monkeypatch.setattr(claude_locks.os, "mkdir", denied)
        monkeypatch.setattr(os.path, "lexists", lambda p: False)
        with caplog.at_level("WARNING", logger="claude-swap"):
            result = _write(session_dir, registry)
        assert (result.ok, result.reason) == (False, "write-failed")
        assert "write failed" in caplog.text
        assert session_dir.is_dir()
        assert _kc_key(session_dir) not in block_real_keychain.data

    def test_unstattable_path_with_intact_fd_is_a_write_failure(
        self, session_dir, registry, block_real_keychain, monkeypatch, caplog
    ):
        """A path stat failing with anything but "not found" is ambiguous,
        so it must not turn a real write failure into a missing profile."""
        real_mkdir, real_stat = os.mkdir, os.stat

        def denied(path, *args, **kwargs):
            if str(path).endswith(".oauth_refresh.lock"):
                raise PermissionError(13, "Permission denied", str(path))
            return real_mkdir(path, *args, **kwargs)

        def unstattable(path, *args, **kwargs):
            if isinstance(path, (str, os.PathLike)) and os.fspath(path) == str(session_dir):
                raise PermissionError(13, "Permission denied", str(path))
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(claude_locks.os, "mkdir", denied)
        monkeypatch.setattr(os, "stat", unstattable)
        with caplog.at_level("WARNING", logger="claude-swap"):
            result = _write(session_dir, registry)
        monkeypatch.undo()
        assert (result.ok, result.reason) == (False, "write-failed")
        assert "write failed" in caplog.text
        assert session_dir.is_dir()
        assert _kc_key(session_dir) not in block_real_keychain.data
