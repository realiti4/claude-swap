"""Background login sync: a new login for a MANAGED account is stored into its
slot by the auto-switch engine, so a /login no longer needs a manual `cswap add`.

The sync never adds an account, honours a per-account ``loginSync: false``,
and only writes when the profile endpoint positively confirms the token's
owner (stricter than the manual add, which registers when the check cannot
answer).
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from claude_swap import oauth
from claude_swap.autoswitch import LoginSyncEvent
from tests.test_autoswitch import EngineHarness

OWNER_A = {"uuid": "uuid-1", "email": "a@example.com", "organizationUuid": ""}
OWNER_OTHER = {"uuid": "uuid-9", "email": "z@example.com", "organizationUuid": ""}

NEW_LOGIN = json.dumps({"claudeAiOauth": {
    "accessToken": "sk-new", "refreshToken": "rt-new",
    "expiresAt": 99999999999000,
}})


@pytest.fixture
def h(temp_home) -> EngineHarness:
    h = EngineHarness(temp_home)
    h.seed(1, "a@example.com")
    h.seed(2, "b@example.com")
    _set_live(h, "a@example.com", 1, NEW_LOGIN)
    return h


def _set_live(h: EngineHarness, email: str, num: int, creds: str) -> None:
    (h.temp_home / ".claude" / ".credentials.json").write_text(creds)
    (h.temp_home / ".claude.json").write_text(json.dumps({
        "oauthAccount": {"emailAddress": email, "accountUuid": f"uuid-{num}"},
    }))


def _backup_fp(h: EngineHarness, num: int, email: str) -> str | None:
    return oauth.credential_fingerprint(
        h.switcher.read_account_credentials(str(num), email)
    )


def _sync(h: EngineHarness, profile, now: float = 1000.0):
    with patch("claude_swap.oauth.fetch_oauth_profile", return_value=profile) as m:
        result = h.switcher.sync_active_login(now=now)
    return result, m


class TestSyncActiveLogin:
    def test_new_login_for_managed_account_is_stored(self, h):
        result, _ = _sync(h, OWNER_A)
        assert result is not None and result.updated
        assert (result.number, result.email) == ("1", "a@example.com")
        assert _backup_fp(h, 1, "a@example.com") == oauth.credential_fingerprint(NEW_LOGIN)

    def test_unchanged_login_does_nothing_and_skips_the_network(self, h):
        _sync(h, OWNER_A)
        result, probe = _sync(h, OWNER_A)
        assert result is None
        probe.assert_not_called()

    def test_unmanaged_login_is_never_added(self, h):
        _set_live(h, "new@example.com", 7, NEW_LOGIN)
        before = h.switcher._get_sequence_data()["accounts"].keys()
        result, probe = _sync(h, {"uuid": "uuid-7", "email": "new@example.com"})
        assert result is None
        probe.assert_not_called()
        assert h.switcher._get_sequence_data()["accounts"].keys() == before

    def test_removed_active_account_is_not_re_added(self, h):
        h.switcher.remove_account("1", assume_yes=True)
        result, _ = _sync(h, OWNER_A)
        assert result is None
        assert "1" not in h.switcher._get_sequence_data()["accounts"]

    def test_disabled_per_account_is_left_alone(self, h, capsys):
        h.switcher.set_account_login_sync("1", False)
        assert h.switcher._get_sequence_data()["accounts"]["1"]["loginSync"] is False
        old_fp = _backup_fp(h, 1, "a@example.com")
        result, probe = _sync(h, OWNER_A)
        assert result is None
        probe.assert_not_called()
        assert _backup_fp(h, 1, "a@example.com") == old_fp

    def test_login_sync_states_lists_every_slot(self, h, capsys):
        assert h.switcher.login_sync_states() == {"1": True, "2": True}
        h.switcher.set_account_login_sync("2", False)
        assert h.switcher.login_sync_states() == {"1": True, "2": False}

    def test_re_enabling_pops_the_key(self, h, capsys):
        h.switcher.set_account_login_sync("1", False)
        h.switcher.set_account_login_sync("1", True)
        assert "loginSync" not in h.switcher._get_sequence_data()["accounts"]["1"]
        assert h.switcher.login_sync_enabled("1")

    def test_unverifiable_owner_defers_without_writing(self, h):
        old_fp = _backup_fp(h, 1, "a@example.com")
        result, probe = _sync(h, None, now=1000.0)
        assert result is None
        assert _backup_fp(h, 1, "a@example.com") == old_fp
        # Backed off: no second probe inside the retry window...
        result, probe = _sync(h, OWNER_A, now=1000.0 + 10)
        assert result is None
        probe.assert_not_called()
        # ...and retried after it.
        retry = 1000.0 + h.switcher.LOGIN_SYNC_RETRY_S + 1
        result, _ = _sync(h, OWNER_A, now=retry)
        assert result is not None and result.updated

    def test_expired_access_token_defers(self, h):
        expired = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-old", "refreshToken": "rt-new", "expiresAt": 1,
        }})
        _set_live(h, "a@example.com", 1, expired)
        old_fp = _backup_fp(h, 1, "a@example.com")
        result, probe = _sync(h, OWNER_A)
        assert result is None
        probe.assert_not_called()
        assert _backup_fp(h, 1, "a@example.com") == old_fp

    def test_foreign_token_is_refused_once_and_not_retried(self, h):
        old_fp = _backup_fp(h, 1, "a@example.com")
        result, _ = _sync(h, OWNER_OTHER)
        assert result is not None and not result.updated
        assert result.detail
        assert _backup_fp(h, 1, "a@example.com") == old_fp
        result, probe = _sync(h, OWNER_A, now=10**9)
        assert result is None
        probe.assert_not_called()

    def test_lineage_change_after_refusal_is_checked_afresh(self, h):
        _sync(h, OWNER_OTHER)
        _set_live(h, "a@example.com", 1, json.dumps({"claudeAiOauth": {
            "accessToken": "sk-newer", "refreshToken": "rt-newer",
            "expiresAt": 99999999999000,
        }}))
        result, _ = _sync(h, OWNER_A)
        assert result is not None and result.updated

    def test_api_key_slot_is_skipped(self, h):
        data = h.switcher._get_sequence_data()
        data["accounts"]["1"]["kind"] = "api_key"
        h.switcher._write_json(h.switcher.sequence_file, data)
        result, probe = _sync(h, OWNER_A)
        assert result is None
        probe.assert_not_called()

    def test_held_lock_defers(self, h):
        """A switch (or any writer) holding lock_file: skip, never wait it out."""
        old_fp = _backup_fp(h, 1, "a@example.com")
        with patch("claude_swap.switcher.FileLock.acquire", return_value=False):
            result, _ = _sync(h, OWNER_A)
        assert result is None
        assert _backup_fp(h, 1, "a@example.com") == old_fp

    def test_user_settings_survive_the_update(self, h, capsys):
        h.switcher.set_account_disabled("1", True)
        h.switcher.set_account_priority("1", 5)
        result, _ = _sync(h, OWNER_A)
        assert result is not None and result.updated
        record = h.switcher._get_sequence_data()["accounts"]["1"]
        assert record.get("disabled") is True
        assert record.get("priority") == 5


class TestEngineHook:
    def test_tick_stores_the_login_and_emits_an_event(self, h):
        with patch("claude_swap.oauth.fetch_oauth_profile", return_value=OWNER_A):
            h.tick_with_usage({"1": None, "2": None})
        synced = [e for e in h.events if isinstance(e, LoginSyncEvent)]
        assert len(synced) == 1 and synced[0].updated
        assert synced[0].to_json()["event"] == "login-synced"
        assert _backup_fp(h, 1, "a@example.com") == oauth.credential_fingerprint(NEW_LOGIN)

    def test_dry_run_never_writes(self, h):
        h.engine = h._make_engine(dry_run=True)
        old_fp = _backup_fp(h, 1, "a@example.com")
        with patch("claude_swap.oauth.fetch_oauth_profile", return_value=OWNER_A):
            h.tick_with_usage({"1": None, "2": None})
        assert not [e for e in h.events if isinstance(e, LoginSyncEvent)]
        assert _backup_fp(h, 1, "a@example.com") == old_fp

    def test_a_failing_sync_does_not_break_the_tick(self, h):
        with patch.object(h.switcher, "sync_active_login", side_effect=RuntimeError("boom")):
            outcome = h.tick_with_usage({"1": None, "2": None})
        assert outcome.name != "ERROR"

    def test_relogin_releases_a_quarantined_active_account_same_tick(self, h):
        h.engine._quarantine("1", "a@example.com", "invalid_grant")
        with patch("claude_swap.oauth.fetch_oauth_profile", return_value=OWNER_A):
            h.tick_with_usage({"1": None, "2": None})
        assert "account-unquarantined" in h.kinds()
        assert "1" not in h.state().get("quarantine", {})


class TestDisplay:
    def test_snapshot_carries_the_login_sync_state(self, h, capsys):
        h.switcher.set_account_login_sync("2", False)
        snap = h.switcher.accounts_snapshot(fetch=set())
        states = {a.number: a.login_sync for a in snap.accounts}
        assert states == {"1": True, "2": False}

    def test_card_badge_only_when_asked_and_only_for_oauth(self):
        from claude_swap.tui.widgets import account_card_text
        from tests.test_tui import make_account

        on = make_account(1)
        off = make_account(2, login_sync=False)
        key = make_account(3, kind="api_key")
        assert "login sync" not in account_card_text(on, 100).plain
        st = make_account(4, setup_token=True)
        assert "login sync on" in account_card_text(on, 100, show_login_sync=True).plain
        # off shows nothing; API keys and setup-tokens never carry the badge
        assert "login sync" not in account_card_text(off, 100, show_login_sync=True).plain
        assert "login sync" not in account_card_text(key, 100, show_login_sync=True).plain
        assert "login sync" not in account_card_text(st, 100, show_login_sync=True).plain


SETUP_TOKEN = json.dumps({"claudeAiOauth": {
    "accessToken": "sk-ant-oat01-SETUP", "scopes": list(oauth.SETUP_TOKEN_SCOPES),
}})


def _make_setup_token_slot(h: EngineHarness, num: int, email: str) -> None:
    h.seed(num, email)
    h.switcher._write_account_credentials(str(num), email, SETUP_TOKEN)
    data = h.switcher._get_sequence_data()
    data["accounts"][str(num)]["uuid"] = ""
    h.switcher._write_json(h.switcher.sequence_file, data)


class TestSetupTokens:
    """Login sync never applies to a setup-token: it has no refresh token, so
    it never changes in use, and there is nothing to re-store."""

    def test_sync_skips_a_setup_token_slot_without_any_request(self, h):
        _make_setup_token_slot(h, 4, "st@example.com")
        # live: the same setup-token, but the file bytes differ (e.g. an MCP
        # login stored beside it) -- a whole-file fingerprint would differ
        live = json.dumps({**json.loads(SETUP_TOKEN), "mcpOAuth": {"x": 1}})
        _set_live(h, "st@example.com", 4, live)
        result, probe = _sync(h, OWNER_A)
        assert result is None
        probe.assert_not_called()
        assert h.switcher.read_account_credentials("4", "st@example.com") == SETUP_TOKEN

    def test_states_report_not_applicable(self, h):
        _make_setup_token_slot(h, 4, "st@example.com")
        data = h.switcher._get_sequence_data()
        data["accounts"]["2"]["kind"] = "api_key"
        h.switcher._write_json(h.switcher.sequence_file, data)
        assert h.switcher.login_sync_states() == {"1": True, "2": None, "4": None}

    def test_toggling_a_setup_token_is_refused_and_writes_nothing(self, h):
        from claude_swap.exceptions import ValidationError

        _make_setup_token_slot(h, 4, "st@example.com")
        with pytest.raises(ValidationError, match="does not apply"):
            h.switcher.set_account_login_sync("4", False)
        assert "loginSync" not in h.switcher._get_sequence_data()["accounts"]["4"]

    def test_snapshot_flags_setup_tokens(self, h):
        _make_setup_token_slot(h, 4, "st@example.com")
        snap = h.switcher.accounts_snapshot(fetch=set())
        flags = {a.number: a.setup_token for a in snap.accounts}
        assert flags == {"1": False, "2": False, "4": True}
