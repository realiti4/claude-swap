"""Access-token resolution and push refresh for managed sessions (Refs #382)."""

from __future__ import annotations

import json
import os
import sys
import time

import pytest

from claude_swap import claude_locks, oauth
from claude_swap import managed_refresh as mr
from claude_swap.credentials import ActiveCredentials
from claude_swap.exceptions import ClaudeSwitchError, LockError
from claude_swap.managed_sessions import ManagedSessionRegistry, create_managed_profile
from claude_swap.managed_sessions import AccountRef
from claude_swap.session import session_dir_for
from claude_swap.session_credentials import WriteResult, build_access_credential

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="managed sessions are POSIX-only (v1)"
)

B = AccountRef("b@example.com", "org-2")
BUFFER_MS = 10 * 60 * 1000
SYSTEMIC = ("store-unmirrored", "invalid_client", "stash-unreadable", "consume-busy")


def _now_ms() -> float:
    return time.time() * 1000


def _past_fixture_expiry() -> float:
    return _now_ms() + 7 * 3600 * 1000  # past the fixture's 6 h expiry


def _token(credential: str) -> str:
    return json.loads(credential)["claudeAiOauth"]["accessToken"]


def _gate(calls: list, outcome: oauth.RefreshOutcome):
    def gate(number, email, snapshot):
        calls.append(number)
        return outcome
    return gate


def _live_run_profile(
    switcher, num: str, email: str, credential: str | None, *, logged_in_as: str | None = None
) -> None:
    profile = session_dir_for(switcher.backup_dir, num, email)
    (profile / "sessions").mkdir(parents=True)
    (profile / "sessions" / f"{os.getpid()}.json").write_text(json.dumps({"pid": os.getpid()}))
    if credential is not None:
        (profile / ".credentials.json").write_text(credential)
    if logged_in_as is not None:
        (profile / ".claude.json").write_text(json.dumps({"oauthAccount": {
            "emailAddress": logged_in_as, "organizationUuid": f"org-{num}",
        }}))


def _raise(*a, **k):
    raise ClaudeSwitchError("store unreadable")


def _raise_os(*a, **k):
    raise PermissionError(13, "Permission denied")


class TestResolve:
    def test_fresh_backup_is_used_without_refresh(self, managed_switcher, monkeypatch):
        calls: list = []
        monkeypatch.setattr(managed_switcher, "consume_backup_grant", _gate(calls, None))
        res = mr.resolve_access_credential(managed_switcher, B, now_ms=_now_ms(), buffer_ms=BUFFER_MS)
        assert (res.status, res.source, res.number) == ("ok", "backup", "2")
        assert _token(res.credential) == "at-2"
        assert "refreshToken" not in json.loads(res.credential)["claudeAiOauth"]
        assert res.oauth_account["emailAddress"] == "b@example.com"
        assert calls == []

    def test_near_expiry_backup_is_refreshed_through_the_gate(self, managed_switcher, monkeypatch):
        rotated = json.dumps({"claudeAiOauth": {
            "accessToken": "at-2-new", "refreshToken": "rt-2-new", "expiresAt": 9_999_999_999_999,
        }})
        calls: list = []
        monkeypatch.setattr(
            managed_switcher, "consume_backup_grant", _gate(calls, oauth.RefreshOutcome(rotated, None))
        )
        res = mr.resolve_access_credential(
            managed_switcher, B, now_ms=_past_fixture_expiry(), buffer_ms=BUFFER_MS
        )
        assert calls == ["2"]
        assert (res.status, _token(res.credential)) == ("ok", "at-2-new")
        assert "refreshToken" not in json.loads(res.credential)["claudeAiOauth"]

    @pytest.mark.parametrize("error", ["invalid_grant", "no_refresh_token"])
    def test_dead_grant_is_reported(self, managed_switcher, monkeypatch, error):
        monkeypatch.setattr(
            managed_switcher, "consume_backup_grant",
            _gate([], oauth.RefreshOutcome(None, error)),
        )
        res = mr.resolve_access_credential(
            managed_switcher, B, now_ms=_past_fixture_expiry(), buffer_ms=BUFFER_MS
        )
        assert (res.status, res.credential) == ("invalid_grant", None)

    def test_transient_refresh_failure(self, managed_switcher, monkeypatch):
        monkeypatch.setattr(
            managed_switcher, "consume_backup_grant",
            _gate([], oauth.RefreshOutcome(None, "transient")),
        )
        res = mr.resolve_access_credential(
            managed_switcher, B, now_ms=_past_fixture_expiry(), buffer_ms=BUFFER_MS
        )
        assert res.status == "transient"

    def test_gate_raising_is_transient(self, managed_switcher, monkeypatch):
        def gate(number, email, snapshot):
            raise ClaudeSwitchError("lock timeout")

        monkeypatch.setattr(managed_switcher, "consume_backup_grant", gate)
        res = mr.resolve_access_credential(
            managed_switcher, B, now_ms=_past_fixture_expiry(), buffer_ms=BUFFER_MS
        )
        assert (res.status, res.source, res.credential) == ("transient", "backup", None)

    @pytest.mark.parametrize("error", SYSTEMIC)
    def test_systemic_refusal_keeps_its_own_status(self, managed_switcher, monkeypatch, error):
        monkeypatch.setattr(
            managed_switcher, "consume_backup_grant",
            _gate([], oauth.RefreshOutcome(None, error)),
        )
        res = mr.resolve_access_credential(
            managed_switcher, B, now_ms=_past_fixture_expiry(), buffer_ms=BUFFER_MS
        )
        assert (res.status, res.credential) == (error, None)

    def test_missing_backup_is_not_transient(self, managed_switcher, monkeypatch):
        calls: list = []
        monkeypatch.setattr(managed_switcher, "consume_backup_grant", _gate(calls, None))
        managed_switcher._delete_account_credentials("2", "b@example.com")
        res = mr.resolve_access_credential(managed_switcher, B, now_ms=_now_ms(), buffer_ms=BUFFER_MS)
        assert (res.status, res.source, res.number, res.credential) == (
            "no-backup", "backup", "2", None
        )
        assert calls == []

    @pytest.mark.parametrize("token_account", [
        {"uuid": "uuid-somebody-else", "email": "b@example.com", "organizationUuid": ""},
        {"uuid": "uuid-2", "email": "b@example.com", "organizationUuid": "org-other"},
    ], ids=["uuid", "org"])
    def test_identity_conflict(self, managed_switcher, monkeypatch, token_account):
        rotated = json.dumps({"claudeAiOauth": {"accessToken": "x", "refreshToken": "y"}})
        monkeypatch.setattr(
            managed_switcher, "consume_backup_grant",
            _gate([], oauth.RefreshOutcome(rotated, None, token_account)),
        )
        res = mr.resolve_access_credential(
            managed_switcher, B, now_ms=_past_fixture_expiry(), buffer_ms=BUFFER_MS
        )
        assert (res.status, res.credential) == ("identity-conflict", None)

    def test_matching_token_identity_is_ok(self, managed_switcher, monkeypatch):
        rotated = json.dumps({"claudeAiOauth": {"accessToken": "x", "refreshToken": "y"}})
        monkeypatch.setattr(
            managed_switcher, "consume_backup_grant",
            _gate([], oauth.RefreshOutcome(
                rotated, None,
                {"uuid": "uuid-2", "email": "B@Example.com", "organizationUuid": "org-2"},
            )),
        )
        res = mr.resolve_access_credential(
            managed_switcher, B, now_ms=_past_fixture_expiry(), buffer_ms=BUFFER_MS
        )
        assert (res.status, _token(res.credential)) == ("ok", "x")

    def test_lane0_account_copies_live_access_token_and_never_refreshes(
        self, managed_switcher, monkeypatch
    ):
        calls: list = []
        monkeypatch.setattr(managed_switcher, "consume_backup_grant", _gate(calls, None))
        lane0 = AccountRef("lane0@example.com", "org-1")
        res = mr.resolve_access_credential(
            managed_switcher, lane0, now_ms=_past_fixture_expiry(), buffer_ms=BUFFER_MS
        )
        assert (res.status, res.source, _token(res.credential)) == ("ok", "lane0", "at-live-1")
        assert "refreshToken" not in json.loads(res.credential)["claudeAiOauth"]
        assert calls == []

    def test_lane0_holding_an_api_key_is_unavailable(self, managed_switcher, temp_home, monkeypatch):
        calls: list = []
        monkeypatch.setattr(managed_switcher, "consume_backup_grant", _gate(calls, None))
        (temp_home / ".claude" / ".credentials.json").write_text("sk-ant-api03-test")
        res = mr.resolve_access_credential(
            managed_switcher, AccountRef("lane0@example.com", "org-1"),
            now_ms=_now_ms(), buffer_ms=BUFFER_MS,
        )
        assert (res.status, res.source, res.credential) == ("unavailable", "lane0", None)
        assert calls == []

    def test_live_run_profile_is_the_source(self, managed_switcher, monkeypatch):
        _live_run_profile(managed_switcher, "3", "c@example.com", json.dumps({"claudeAiOauth": {
            "accessToken": "at-run-3", "refreshToken": "rt-run-3",
        }}))
        calls: list = []
        monkeypatch.setattr(managed_switcher, "consume_backup_grant", _gate(calls, None))
        res = mr.resolve_access_credential(
            managed_switcher, AccountRef("c@example.com", "org-3"),
            now_ms=_past_fixture_expiry(), buffer_ms=BUFFER_MS,
        )
        assert (res.status, res.source, _token(res.credential)) == ("ok", "run-profile", "at-run-3")
        assert "refreshToken" not in json.loads(res.credential)["claudeAiOauth"]
        assert calls == []

    def test_live_run_profile_without_a_credential_is_unavailable(
        self, managed_switcher, monkeypatch
    ):
        # The run profile owns the lineage while it lives, so the backup is
        # not a fallback: consuming it would fork the token family.
        _live_run_profile(managed_switcher, "3", "c@example.com", None)
        calls: list = []
        monkeypatch.setattr(managed_switcher, "consume_backup_grant", _gate(calls, None))
        res = mr.resolve_access_credential(
            managed_switcher, AccountRef("c@example.com", "org-3"),
            now_ms=_past_fixture_expiry(), buffer_ms=BUFFER_MS,
        )
        assert (res.status, res.source, res.credential) == ("unavailable", "run-profile", None)
        assert calls == []

    @pytest.mark.parametrize("config", [
        "",
        "{not json",
        json.dumps(["not", "an", "object"]),
        json.dumps({"theme": "light"}),
        json.dumps({"oauthAccount": {"accountUuid": "uuid-2"}}),
    ], ids=["missing", "garbled", "non-object", "no-oauth-account", "no-email"])
    def test_account_without_a_usable_config(self, managed_switcher, monkeypatch, config):
        calls: list = []
        monkeypatch.setattr(managed_switcher, "consume_backup_grant", _gate(calls, None))
        monkeypatch.setattr(managed_switcher, "read_account_config", lambda num, email: config)
        res = mr.resolve_access_credential(
            managed_switcher, B, now_ms=_past_fixture_expiry(), buffer_ms=BUFFER_MS
        )
        assert (res.status, res.number, res.credential) == ("no-config", "2", None)
        assert calls == []

    @pytest.mark.parametrize("stored", [
        {"emailAddress": "someone-else@example.com", "organizationUuid": "org-2"},
        {"emailAddress": "b@example.com", "organizationUuid": "org-9"},
        {"emailAddress": "b@example.com"},
        {"emailAddress": "b@example.com", "organizationUuid": ""},
    ], ids=["other-email", "other-org", "no-org", "blank-org"])
    def test_stored_config_naming_another_account_is_refused_by_name(
        self, managed_switcher, monkeypatch, stored
    ):
        """The writer refuses exactly this mismatch with identity-mismatch on
        every push, forever. Resolving it as usable would hide the real
        problem behind a repeating write failure, so it is refused here, with
        a status that names the stored config as the file to repair."""
        calls: list = []
        monkeypatch.setattr(managed_switcher, "consume_backup_grant", _gate(calls, None))
        monkeypatch.setattr(
            managed_switcher, "read_account_config",
            lambda num, email: json.dumps({"oauthAccount": stored}),
        )
        res = mr.resolve_access_credential(
            managed_switcher, B, now_ms=_past_fixture_expiry(), buffer_ms=BUFFER_MS
        )
        assert (res.status, res.number, res.credential) == (
            "stored-config-mismatch", "2", None
        )
        assert calls == []

    def test_run_profile_logged_in_as_another_account_is_unavailable(
        self, managed_switcher, monkeypatch
    ):
        # An in-session /login re-pointed the profile at somebody else: its
        # token is that account's, and must never be served as account 3's.
        _live_run_profile(managed_switcher, "3", "c@example.com", json.dumps({"claudeAiOauth": {
            "accessToken": "at-somebody-else", "refreshToken": "rt-x",
        }}), logged_in_as="evil@example.com")
        calls: list = []
        monkeypatch.setattr(managed_switcher, "consume_backup_grant", _gate(calls, None))
        res = mr.resolve_access_credential(
            managed_switcher, AccountRef("c@example.com", "org-3"),
            now_ms=_past_fixture_expiry(), buffer_ms=BUFFER_MS,
        )
        assert (res.status, res.source, res.credential) == ("unavailable", "run-profile", None)
        assert calls == []

    def test_run_profile_logged_in_as_its_own_account_is_served(
        self, managed_switcher, monkeypatch
    ):
        _live_run_profile(managed_switcher, "3", "c@example.com", json.dumps({"claudeAiOauth": {
            "accessToken": "at-run-3", "refreshToken": "rt-run-3",
        }}), logged_in_as="c@example.com")
        res = mr.resolve_access_credential(
            managed_switcher, AccountRef("c@example.com", "org-3"),
            now_ms=_now_ms(), buffer_ms=BUFFER_MS,
        )
        assert (res.status, _token(res.credential)) == ("ok", "at-run-3")

    def test_backup_without_an_access_token_is_not_a_dead_grant(self, managed_switcher, monkeypatch):
        # The refresh token may be perfectly alive; nothing here says the
        # lineage is dead, so this must not read as invalid_grant.
        # Even the refreshed generation carries no access token here.
        tokenless = json.dumps({"claudeAiOauth": {"refreshToken": "rt-2-new"}})
        monkeypatch.setattr(
            managed_switcher, "consume_backup_grant",
            _gate([], oauth.RefreshOutcome(tokenless, None)),
        )
        managed_switcher._write_account_credentials("2", "b@example.com", json.dumps({
            "claudeAiOauth": {"refreshToken": "rt-2", "expiresAt": 9_999_999_999_999},
        }))
        res = mr.resolve_access_credential(managed_switcher, B, now_ms=_now_ms(), buffer_ms=BUFFER_MS)
        assert (res.status, res.source, res.credential) == ("no-access-token", "backup", None)

    @pytest.mark.parametrize("access", [None, ""], ids=["missing", "empty"])
    def test_backup_without_an_access_token_is_refreshed(self, managed_switcher, monkeypatch, access):
        blob: dict = {"refreshToken": "rt-2"}
        if access is not None:
            blob["accessToken"] = access
        managed_switcher._write_account_credentials(
            "2", "b@example.com", json.dumps({"claudeAiOauth": blob})
        )
        rotated = json.dumps({"claudeAiOauth": {
            "accessToken": "at-2-new", "refreshToken": "rt-2-new", "expiresAt": 9_999_999_999_999,
        }})
        calls: list = []
        monkeypatch.setattr(
            managed_switcher, "consume_backup_grant", _gate(calls, oauth.RefreshOutcome(rotated, None))
        )
        res = mr.resolve_access_credential(managed_switcher, B, now_ms=_now_ms(), buffer_ms=BUFFER_MS)
        assert calls == ["2"]
        assert (res.status, _token(res.credential)) == ("ok", "at-2-new")

    def test_ok_without_credentials_is_not_served(self, managed_switcher, monkeypatch):
        monkeypatch.setattr(
            managed_switcher, "freshen_backup_credential", lambda *a, **k: ("ok", None)
        )
        res = mr.resolve_access_credential(managed_switcher, B, now_ms=_now_ms(), buffer_ms=BUFFER_MS)
        assert (res.status, res.credential) == ("transient", None)

    @pytest.mark.parametrize("active", [
        ActiveCredentials(None, False),
        ActiveCredentials("", True),
    ], ids=["read-error", "keychain-unavailable"])
    def test_unreadable_lane0_store_is_transient(self, managed_switcher, monkeypatch, active):
        monkeypatch.setattr(managed_switcher, "_read_active_credentials", lambda: active)
        res = mr.resolve_access_credential(
            managed_switcher, AccountRef("lane0@example.com", "org-1"),
            now_ms=_now_ms(), buffer_ms=BUFFER_MS,
        )
        assert (res.status, res.source, res.credential) == ("transient", "lane0", None)

    def test_degraded_lane0_read_is_still_served(self, managed_switcher, monkeypatch):
        live = json.dumps({"claudeAiOauth": {"accessToken": "at-plain-1", "refreshToken": "rt"}})
        monkeypatch.setattr(
            managed_switcher, "_read_active_credentials",
            lambda: ActiveCredentials(live, False, degraded=True),
        )
        res = mr.resolve_access_credential(
            managed_switcher, AccountRef("lane0@example.com", "org-1"),
            now_ms=_now_ms(), buffer_ms=BUFFER_MS,
        )
        assert (res.status, _token(res.credential)) == ("ok", "at-plain-1")

    @pytest.mark.parametrize("error", [_raise, _raise_os], ids=["switch-error", "os-error"])
    @pytest.mark.parametrize("attr", [
        "_get_sequence_data", "read_account_config", "current_account_number",
        "_read_active_credentials", "live_session_pids_for", "read_account_credentials",
    ])
    def test_store_failures_are_transient_not_raised(
        self, managed_switcher, monkeypatch, attr, error
    ):
        if attr in ("_read_active_credentials",):
            lane = AccountRef("lane0@example.com", "org-1")
        else:
            lane = B
        monkeypatch.setattr(managed_switcher, attr, error)
        res = mr.resolve_access_credential(managed_switcher, lane, now_ms=_now_ms(), buffer_ms=BUFFER_MS)
        assert (res.status, res.credential) == ("transient", None)

    @pytest.mark.parametrize("func", ["read_session_credentials", "session_identity_drifted"])
    def test_run_profile_read_failures_are_transient(self, managed_switcher, monkeypatch, func):
        _live_run_profile(managed_switcher, "3", "c@example.com", json.dumps({"claudeAiOauth": {
            "accessToken": "at-run-3",
        }}))
        monkeypatch.setattr(mr, func, _raise_os)
        res = mr.resolve_access_credential(
            managed_switcher, AccountRef("c@example.com", "org-3"),
            now_ms=_now_ms(), buffer_ms=BUFFER_MS,
        )
        assert (res.status, res.number, res.credential) == ("transient", "3", None)

    def test_removed_account(self, managed_switcher):
        res = mr.resolve_access_credential(
            managed_switcher, AccountRef("gone@example.com", ""), now_ms=_now_ms(), buffer_ms=BUFFER_MS
        )
        assert (res.status, res.number) == ("account-gone", None)


C = AccountRef("c@example.com", "org-3")


def _add(switcher, session_id, account, *, fingerprint=None):
    registry = ManagedSessionRegistry(switcher.backup_dir)
    registry.allocate(session_id, lambda busy: (account, "backup"), pid=os.getpid(), proc_start=None)
    create_managed_profile(registry.session_dir(session_id))
    if fingerprint:
        registry.update(session_id, access_fingerprint=fingerprint)
    return registry, registry.session_dir(session_id)


class TestPushRefresh:
    def test_every_session_of_the_account_and_none_of_others(self, managed_switcher, monkeypatch):
        registry, b1 = _add(managed_switcher, "auto-000000b1", B)
        _, b2 = _add(managed_switcher, "auto-000000b2", B)
        c_fp = oauth.access_token_fingerprint(
            build_access_credential(managed_switcher.read_account_credentials("3", "c@example.com"))
        )
        _, c1 = _add(managed_switcher, "auto-000000c1", C, fingerprint=c_fp)
        (c1 / ".credentials.json").write_text("untouched")

        results = mr.push_refresh(managed_switcher, registry, now_ms=_now_ms(), buffer_ms=BUFFER_MS)

        by_email = {r.account.email: r for r in results}
        assert sorted(by_email["b@example.com"].written) == ["auto-000000b1", "auto-000000b2"]
        assert by_email["c@example.com"].written == ()
        for d in (b1, b2):
            blob = json.loads((d / ".credentials.json").read_text())["claudeAiOauth"]
            assert blob["accessToken"] == "at-2" and "refreshToken" not in blob
        assert (c1 / ".credentials.json").read_text() == "untouched"
        assert registry.get("auto-000000b1").access_fingerprint == oauth.access_token_fingerprint(
            (b1 / ".credentials.json").read_text()
        )

    def test_unchanged_token_is_not_rewritten(self, managed_switcher):
        registry, b1 = _add(managed_switcher, "auto-000000b1", B)
        mr.push_refresh(managed_switcher, registry, now_ms=_now_ms(), buffer_ms=BUFFER_MS)
        before = (b1 / ".credentials.json").stat().st_mtime_ns
        results = mr.push_refresh(managed_switcher, registry, now_ms=_now_ms(), buffer_ms=BUFFER_MS)
        assert results[0].written == ()
        assert (b1 / ".credentials.json").stat().st_mtime_ns == before

    def test_quarantined_accounts_are_skipped_without_refresh(self, managed_switcher, monkeypatch):
        registry, b1 = _add(managed_switcher, "auto-000000b1", B)
        calls: list = []
        monkeypatch.setattr(managed_switcher, "consume_backup_grant", _gate(calls, None))
        [result] = mr.push_refresh(
            managed_switcher, registry, now_ms=_now_ms() + 7 * 3600 * 1000,
            buffer_ms=BUFFER_MS, skip_numbers={"2"},
        )
        assert (result.status, result.number, calls) == ("skipped", "2", [])
        assert not (b1 / ".credentials.json").exists()

    def test_failed_writes_are_reported_and_fingerprint_kept(self, managed_switcher):
        registry, _ = _add(managed_switcher, "auto-000000b1", B)

        def refusing_writer(*args, **kwargs):
            return WriteResult(False, "lock-timeout")

        [result] = mr.push_refresh(
            managed_switcher, registry, now_ms=_now_ms(), buffer_ms=BUFFER_MS,
            writer=refusing_writer,
        )
        assert (result.written, result.failed) == ((), ("auto-000000b1",))
        assert registry.get("auto-000000b1").access_fingerprint is None

    def test_dead_grant_status_is_surfaced(self, managed_switcher, monkeypatch):
        registry, b1 = _add(managed_switcher, "auto-000000b1", B)
        monkeypatch.setattr(
            managed_switcher, "consume_backup_grant",
            _gate([], oauth.RefreshOutcome(None, "invalid_grant")),
        )
        [result] = mr.push_refresh(
            managed_switcher, registry, now_ms=_now_ms() + 7 * 3600 * 1000, buffer_ms=BUFFER_MS
        )
        assert (result.status, result.number, result.written) == ("invalid_grant", "2", ())
        assert not (b1 / ".credentials.json").exists()

    # push_refresh works from the live set it is given, never a fresh one.

    def test_given_entries_are_used_instead_of_a_fresh_liveness_pass(self, managed_switcher, monkeypatch):
        registry, b1 = _add(managed_switcher, "auto-000000b1", B)

        def _boom():
            raise AssertionError("push_refresh must not call live_entries() when entries is given")

        monkeypatch.setattr(registry, "live_entries", _boom)
        entry = registry.get("auto-000000b1")

        results = mr.push_refresh(
            managed_switcher, registry, now_ms=_now_ms(), buffer_ms=BUFFER_MS, entries=[entry],
        )

        assert [r.account for r in results] == [B]
        blob = json.loads((b1 / ".credentials.json").read_text())["claudeAiOauth"]
        assert blob["accessToken"] == "at-2"

    def test_empty_entries_means_nothing_to_push_even_though_the_registry_has_live_rows(
        self, managed_switcher
    ):
        registry, b1 = _add(managed_switcher, "auto-000000b1", B)
        results = mr.push_refresh(
            managed_switcher, registry, now_ms=_now_ms(), buffer_ms=BUFFER_MS, entries=(),
        )
        assert results == []
        assert not (b1 / ".credentials.json").exists()

    # Only invalid_grant and identity-conflict are quarantine-worthy.

    @pytest.mark.parametrize("status", ["invalid_grant", "identity-conflict"])
    def test_quarantine_flag_set_for_dead_lineage_statuses(self, managed_switcher, monkeypatch, status):
        registry, _ = _add(managed_switcher, "auto-000000b1", B)
        monkeypatch.setattr(
            mr, "resolve_access_credential",
            lambda *a, **k: mr.AccessResolution(B, "2", status, "backup"),
        )
        [result] = mr.push_refresh(managed_switcher, registry, now_ms=_now_ms(), buffer_ms=BUFFER_MS)
        assert (result.status, result.quarantine) == (status, True)

    @pytest.mark.parametrize("status", [
        "unavailable", "no-access-token", "no-backup", "transient",
        "account-gone", "no-config", "stored-config-mismatch", *SYSTEMIC,
    ])
    def test_quarantine_flag_unset_for_every_other_refusal(self, managed_switcher, monkeypatch, status):
        registry, _ = _add(managed_switcher, "auto-000000b1", B)
        monkeypatch.setattr(
            mr, "resolve_access_credential",
            lambda *a, **k: mr.AccessResolution(B, "2", status, "backup"),
        )
        [result] = mr.push_refresh(managed_switcher, registry, now_ms=_now_ms(), buffer_ms=BUFFER_MS)
        assert (result.status, result.quarantine) == (status, False)

    def test_skipped_status_is_not_quarantine(self, managed_switcher):
        registry, _ = _add(managed_switcher, "auto-000000b1", B)
        [result] = mr.push_refresh(
            managed_switcher, registry, now_ms=_now_ms(), buffer_ms=BUFFER_MS, skip_numbers={"2"},
        )
        assert (result.status, result.quarantine) == ("skipped", False)

    def test_ok_status_is_not_quarantine(self, managed_switcher):
        registry, _ = _add(managed_switcher, "auto-000000b1", B)
        [result] = mr.push_refresh(managed_switcher, registry, now_ms=_now_ms(), buffer_ms=BUFFER_MS)
        assert (result.status, result.quarantine) == ("ok", False)

    # A WriteResult with ok=True and a non-"ok" reason counts as written.

    def test_ok_write_with_non_ok_reason_counts_as_written(self, managed_switcher):
        registry, _ = _add(managed_switcher, "auto-000000b1", B)

        def plaintext_only_writer(*args, **kwargs):
            return WriteResult(
                True, "keychain-write-failed", fingerprint="fp-plaintext-only"
            )

        [result] = mr.push_refresh(
            managed_switcher, registry, now_ms=_now_ms(), buffer_ms=BUFFER_MS,
            writer=plaintext_only_writer,
        )
        assert (result.written, result.failed) == (("auto-000000b1",), ())
        assert registry.get("auto-000000b1").access_fingerprint == "fp-plaintext-only"

    def test_pushed_writes_get_a_bounded_lock_budget(self, managed_switcher):
        """The push walks sessions serially inside one auto-switch tick and
        each write takes three of Claude's locks in sequence, so a wedged
        profile must not be able to hold the tick for three default waits."""
        registry, _ = _add(managed_switcher, "auto-000000b1", B)
        seen: list = []

        def spy_writer(*args, **kwargs):
            seen.append(kwargs.get("lock_timeout"))
            return WriteResult(True, "ok", fingerprint="fp")

        mr.push_refresh(
            managed_switcher, registry, now_ms=_now_ms(), buffer_ms=BUFFER_MS,
            writer=spy_writer,
        )
        assert seen == [mr.PUSH_LOCK_TIMEOUT_S]
        assert 0 < mr.PUSH_LOCK_TIMEOUT_S < claude_locks.DEFAULT_TIMEOUT_S

    def test_pushed_writes_require_the_keychain_item(self, managed_switcher):
        """These profiles already hold a token and claude reads the keychain
        item before the plaintext, so an item this push could not replace
        goes on serving the old one. Unlike a launch into a fresh profile,
        that is a failure to retry, not a plaintext-only success."""
        registry, _ = _add(managed_switcher, "auto-000000b1", B)
        seen: list = []

        def spy_writer(*args, **kwargs):
            seen.append(kwargs.get("require_keychain"))
            return WriteResult(True, "ok", fingerprint="fp")

        mr.push_refresh(
            managed_switcher, registry, now_ms=_now_ms(), buffer_ms=BUFFER_MS,
            writer=spy_writer,
        )
        assert seen == [True]

    # -- a failing registry.update for one session must not abort the --
    # -- whole fan-out.                                                --

    def test_registry_update_failure_for_one_session_is_failed_not_written(
        self, managed_switcher, monkeypatch
    ):
        registry, b1 = _add(managed_switcher, "auto-000000b1", B)
        _, b2 = _add(managed_switcher, "auto-000000b2", B)
        _, c1 = _add(managed_switcher, "auto-000000c1", C)

        real_update = registry.update

        def flaky_update(session_id, **changes):
            if session_id == "auto-000000b1":
                raise LockError("lock timeout")
            return real_update(session_id, **changes)

        monkeypatch.setattr(registry, "update", flaky_update)

        results = mr.push_refresh(managed_switcher, registry, now_ms=_now_ms(), buffer_ms=BUFFER_MS)

        by_email = {r.account.email: r for r in results}
        b_result = by_email["b@example.com"]
        assert b_result.written == ("auto-000000b2",)
        assert b_result.failed == ("auto-000000b1",)
        # The credential file itself was still written -- only the
        # bookkeeping failed -- so it must not be reported as untouched.
        blob = json.loads((b1 / ".credentials.json").read_text())["claudeAiOauth"]
        assert blob["accessToken"] == "at-2"
        # The registry row was never updated, so the next pass retries it
        # (comparing against the still-recorded old/absent fingerprint).
        assert registry.get("auto-000000b1").access_fingerprint is None
        # The fan-out did not abort: b2 (same account) and c1 (a wholly
        # different account processed afterwards) were both still pushed.
        assert (b2 / ".credentials.json").exists()
        assert by_email["c@example.com"].written == ("auto-000000c1",)

    @pytest.mark.parametrize("raw", ["null", "[]", '"a string"', "3"])
    def test_a_non_object_backup_store_does_not_abort_the_fan_out(
        self, managed_switcher, raw
    ):
        """One slot's backup credential being parseable JSON that is not an
        object must refuse that account alone. Letting it escape as an
        exception would cost every account its push, every tick."""
        registry, b1 = _add(managed_switcher, "auto-000000b1", B)
        _, c1 = _add(managed_switcher, "auto-000000c1", C)
        managed_switcher._write_account_credentials("2", "b@example.com", raw)

        results = mr.push_refresh(
            managed_switcher, registry, now_ms=_now_ms(), buffer_ms=BUFFER_MS
        )

        by_email = {r.account.email: r for r in results}
        assert by_email["b@example.com"].status == "invalid_grant"
        assert not (b1 / ".credentials.json").exists()
        assert by_email["c@example.com"].written == ("auto-000000c1",)
        assert (c1 / ".credentials.json").exists()

    def test_slot_lookup_failure_for_one_account_does_not_abort_the_fan_out(
        self, managed_switcher, monkeypatch
    ):
        registry, _ = _add(managed_switcher, "auto-000000b1", B)
        _, c1 = _add(managed_switcher, "auto-000000c1", C)

        real_slot_for_account = mr.slot_for_account

        def flaky_slot_for_account(switcher, account):
            if account.email == "b@example.com":
                raise ClaudeSwitchError("sequence store unreadable")
            return real_slot_for_account(switcher, account)

        monkeypatch.setattr(mr, "slot_for_account", flaky_slot_for_account)

        results = mr.push_refresh(managed_switcher, registry, now_ms=_now_ms(), buffer_ms=BUFFER_MS)

        by_email = {r.account.email: r for r in results}
        assert by_email["b@example.com"].status == "transient"
        assert by_email["c@example.com"].written == ("auto-000000c1",)

    # -- a genuine account-gone lookup must not be redone a second time --
    # -- inside resolve_access_credential.                              --

    def test_account_gone_path_looks_up_the_slot_only_once(self, managed_switcher, monkeypatch):
        gone = AccountRef("gone@example.com", "")
        registry, _ = _add(managed_switcher, "auto-0000000a", gone)

        calls: list = []
        real_slot_for_account = mr.slot_for_account

        def counting_slot_for_account(switcher, account):
            calls.append(account)
            return real_slot_for_account(switcher, account)

        monkeypatch.setattr(mr, "slot_for_account", counting_slot_for_account)

        [result] = mr.push_refresh(managed_switcher, registry, now_ms=_now_ms(), buffer_ms=BUFFER_MS)

        assert (result.status, result.number) == ("account-gone", None)
        assert calls == [gone]
