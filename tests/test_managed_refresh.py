"""Access-token resolution and push refresh for managed sessions (Refs #382)."""

from __future__ import annotations

import json
import os
import sys
import time

import pytest

from claude_swap import oauth
from claude_swap import managed_refresh as mr
from claude_swap.credentials import ActiveCredentials
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.managed_sessions import AccountRef
from claude_swap.session import session_dir_for

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
