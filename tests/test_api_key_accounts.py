"""Tests for managed API-key (``/login`` key) account support.

Covers kind detection, ``--add-token`` auto-detection, the cross-kind collision
guard, the ``add_account`` live-key guard, kind+platform-aware active credential
read/write with OAuth↔API-key mutual exclusion, the "API key — no quota" usage
display, the ``cswap run`` session guard, and export/import of raw keys.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_swap import macos_keychain
from claude_swap import session as session_mod
from claude_swap.credentials import (
    CLAUDE_CODE_KEYCHAIN_SERVICE,
    CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE,
    approved_form,
    looks_like_api_key,
)
from claude_swap.exceptions import SessionError, ValidationError
from claude_swap.json_output import USAGE_API_KEY, usage_fields
from claude_swap.models import Platform
from claude_swap.paths import get_credentials_path, get_global_config_path
from claude_swap.session import SessionManager
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.transfer import export_accounts, import_accounts

API_KEY = "sk-ant-api03-" + "a1b2c3d4e5" * 4  # 53 chars
OTHER_KEY = "sk-ant-api03-" + "z9y8x7w6v5" * 4
OAUTH_JSON = json.dumps(
    {"claudeAiOauth": {"accessToken": "tok", "refreshToken": "rtok", "expiresAt": 9}}
)


def _linux_switcher() -> ClaudeAccountSwitcher:
    s = ClaudeAccountSwitcher()
    s.platform = Platform.LINUX
    s._setup_directories()
    s._init_sequence_file()
    return s


def _macos_switcher() -> ClaudeAccountSwitcher:
    s = ClaudeAccountSwitcher()
    s.platform = Platform.MACOS
    s._setup_directories()
    s._init_sequence_file()
    return s


def _read_global_config() -> dict:
    return json.loads(get_global_config_path().read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Kind detection helpers
# ---------------------------------------------------------------------------


class TestKindDetection:
    def test_api_key_detected(self):
        assert looks_like_api_key(API_KEY) is True

    @pytest.mark.parametrize(
        "value",
        [
            "",
            None,
            "sk-ant-oat01-abcdef",  # setup-token, not a key
            OAUTH_JSON,  # OAuth JSON blob
            '{"x": "sk-ant-api03-inside-json"}',  # JSON that merely contains a key
        ],
    )
    def test_non_api_key(self, value):
        assert looks_like_api_key(value) is False

    def test_approved_form_is_last_20(self):
        assert approved_form(API_KEY) == API_KEY[-20:]
        assert len(approved_form(API_KEY)) == 20


# ---------------------------------------------------------------------------
# --add-token auto-detection
# ---------------------------------------------------------------------------


class TestAddTokenApiKey:
    def test_adds_api_key_account(self, temp_home: Path, capsys):
        s = _linux_switcher()
        s.add_account_from_token(API_KEY)

        assert s._account_kind("1") == "api_key"
        # default synthesized label
        data = s._get_sequence_data()
        assert data["accounts"]["1"]["email"] == "api-key-1@token.local"
        # the raw key is stored verbatim as the backup credential
        assert s._read_account_credentials("1", "api-key-1@token.local") == API_KEY
        out = capsys.readouterr().out
        assert "Added" in out and "API key" in out

    def test_setup_token_stays_oauth(self, temp_home: Path):
        s = _linux_switcher()
        s.add_account_from_token("sk-ant-oat01-abc")
        assert s._account_kind("1") == "oauth"
        email = s._get_sequence_data()["accounts"]["1"]["email"]
        assert email == "setup-token-1@token.local"
        blob = json.loads(s._read_account_credentials("1", email))
        assert blob["claudeAiOauth"]["accessToken"] == "sk-ant-oat01-abc"

    def test_refresh_in_place_same_api_key_account(self, temp_home: Path):
        s = _linux_switcher()
        s.add_account_from_token(API_KEY, email="me@example.com")
        s.add_account_from_token(OTHER_KEY, email="me@example.com")
        data = s._get_sequence_data()
        assert len(data["accounts"]) == 1
        assert s._read_account_credentials("1", "me@example.com") == OTHER_KEY


class TestCrossKindCollision:
    def test_api_key_rejected_when_email_is_oauth(self, temp_home: Path):
        s = _linux_switcher()
        s.add_account_from_token("sk-ant-oat01-abc", email="dup@example.com")
        with pytest.raises(ValidationError, match="already exists as an OAuth account"):
            s.add_account_from_token(API_KEY, email="dup@example.com")

    def test_oauth_rejected_when_email_is_api_key(self, temp_home: Path):
        s = _linux_switcher()
        s.add_account_from_token(API_KEY, email="dup@example.com")
        with pytest.raises(ValidationError, match="already exists as an API-key account"):
            s.add_account_from_token("sk-ant-oat01-abc", email="dup@example.com")


# ---------------------------------------------------------------------------
# Active credential read/write + mutual exclusion
# ---------------------------------------------------------------------------


class TestWriteCredentialsLinux:
    def test_activate_key_then_oauth(self, temp_home: Path):
        s = _linux_switcher()
        cred_file = get_credentials_path()
        cred_file.parent.mkdir(parents=True, exist_ok=True)
        cred_file.write_text(OAUTH_JSON, encoding="utf-8")

        # Activate the API key: primaryApiKey + approved set, OAuth file cleared.
        s._write_credentials(API_KEY)
        cfg = _read_global_config()
        assert cfg["primaryApiKey"] == API_KEY
        assert API_KEY[-20:] in cfg["customApiKeyResponses"]["approved"]
        assert not cred_file.exists()

        # Switch back to OAuth: file restored, primaryApiKey dropped, approved kept.
        s._write_credentials(OAUTH_JSON)
        assert cred_file.read_text(encoding="utf-8") == OAUTH_JSON
        cfg = _read_global_config()
        assert "primaryApiKey" not in cfg
        assert API_KEY[-20:] in cfg["customApiKeyResponses"]["approved"]

    def test_read_credentials_returns_active_key(self, temp_home: Path):
        s = _linux_switcher()
        get_global_config_path().write_text(
            json.dumps({"primaryApiKey": API_KEY}), encoding="utf-8"
        )
        assert s._read_credentials() == API_KEY

    def test_oauth_file_not_misread_as_key(self, temp_home: Path):
        s = _linux_switcher()
        cred_file = get_credentials_path()
        cred_file.parent.mkdir(parents=True, exist_ok=True)
        cred_file.write_text(OAUTH_JSON, encoding="utf-8")
        # primaryApiKey also present, but the OAuth file wins (read first).
        get_global_config_path().write_text(
            json.dumps({"primaryApiKey": API_KEY}), encoding="utf-8"
        )
        assert s._read_credentials() == OAUTH_JSON


class TestWriteCredentialsMacOS:
    def test_activate_key_uses_keychain_not_config(self, temp_home, block_real_keychain):
        store = block_real_keychain
        s = _macos_switcher()
        acct = macos_keychain.keychain_account_name()
        store.set_password(CLAUDE_CODE_KEYCHAIN_SERVICE, acct, OAUTH_JSON)

        s._write_credentials(API_KEY)

        # Key in the managed keychain service; OAuth keychain item cleared.
        assert store.get_password(CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE, acct) == API_KEY
        assert store.get_password(CLAUDE_CODE_KEYCHAIN_SERVICE, acct) is None
        # approved recorded, but the full key stays OUT of plaintext config.
        cfg = _read_global_config()
        assert API_KEY[-20:] in cfg["customApiKeyResponses"]["approved"]
        assert "primaryApiKey" not in cfg

    def test_switch_back_to_oauth_clears_key(self, temp_home, block_real_keychain):
        store = block_real_keychain
        s = _macos_switcher()
        s._write_credentials(API_KEY)
        acct = macos_keychain.keychain_account_name()
        assert store.get_password(CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE, acct) == API_KEY

        s._write_credentials(OAUTH_JSON)
        # managed keychain cleared, OAuth keychain populated, approved kept.
        assert store.get_password(CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE, acct) is None
        assert store.get_password(CLAUDE_CODE_KEYCHAIN_SERVICE, acct) == OAUTH_JSON
        cfg = _read_global_config()
        assert API_KEY[-20:] in cfg["customApiKeyResponses"]["approved"]

    def test_read_credentials_from_managed_keychain(self, temp_home, block_real_keychain):
        store = block_real_keychain
        s = _macos_switcher()
        acct = macos_keychain.keychain_account_name()
        store.set_password(CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE, acct, API_KEY)
        assert s._read_credentials() == API_KEY


# ---------------------------------------------------------------------------
# Usage display ("API key — no quota")
# ---------------------------------------------------------------------------


class TestUsageDisplay:
    def test_usage_fields_maps_api_key(self):
        assert usage_fields(USAGE_API_KEY) == ("api_key", None)

    def test_collect_usage_short_circuits(self, temp_home: Path):
        s = _linux_switcher()
        info = [(2, "api-key-2@token.local", "", "", False, API_KEY)]
        entries = s._collect_usage_entries(info)
        assert entries["2"].sentinel == USAGE_API_KEY
        assert entries["2"].decision_value() == USAGE_API_KEY

    def test_active_account_usage_short_circuits(self, temp_home: Path):
        s = _linux_switcher()
        get_global_config_path().write_text(
            json.dumps({"primaryApiKey": API_KEY}), encoding="utf-8"
        )
        entry = s._active_account_usage("2", "api-key-2@token.local", "")
        assert entry.sentinel == USAGE_API_KEY
        assert entry.decision_value() == USAGE_API_KEY


class TestStrategyBehaviour:
    """API-key accounts are never *rate-limited* (next-available can fall back to
    them), but `best` must NOT auto-prefer them — they have no measurable quota and
    jumping to one would silently spend paid per-token credits."""

    def test_api_key_headroom_is_unknown(self):
        # None headroom == "unknown" == never auto-skipped by next-available.
        from claude_swap import oauth

        assert oauth.account_headroom(USAGE_API_KEY) is None

    def test_best_does_not_jump_to_api_key_even_when_exhausted(
        self, temp_home: Path, monkeypatch
    ):
        s = _linux_switcher()
        s.add_account_from_token("sk-ant-oat01-x", slot=1)  # OAuth, switchable
        s.add_account_from_token(API_KEY, slot=2)  # API key, switchable
        # Current OAuth account (1) is fully exhausted; the only other account is
        # the no-quota API key. `best` must stay put rather than burn API credits.
        monkeypatch.setattr(
            s,
            "_usage_by_account",
            lambda: {"1": {"five_hour": {"pct": 100.0}}, "2": USAGE_API_KEY},
        )
        target, _ = s._select_best_switchable("1")
        assert target is None


# ---------------------------------------------------------------------------
# add_account guard against capturing a live API-key login
# ---------------------------------------------------------------------------


class TestAddAccountGuard:
    def test_rejects_live_api_key_login(self, temp_home: Path):
        s = _linux_switcher()
        # Lingering oauthAccount identity + an active managed key in config.
        get_global_config_path().write_text(
            json.dumps(
                {
                    "oauthAccount": {"emailAddress": "stale@example.com"},
                    "primaryApiKey": API_KEY,
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ValidationError, match="Active login is an API-key account"):
            s.add_account()


# ---------------------------------------------------------------------------
# Session-mode guard
# ---------------------------------------------------------------------------


class TestSessionGuard:
    def _seed_api_key_account(self) -> ClaudeAccountSwitcher:
        s = _linux_switcher()
        s.add_account_from_token(API_KEY, slot=2)
        return s

    def test_setup_session_rejects(self, temp_home: Path):
        mgr = SessionManager(self._seed_api_key_account())
        with pytest.raises(SessionError, match="does not support API-key accounts"):
            mgr.setup_session("2", share=True)

    def test_run_rejects_before_exec(self, temp_home: Path, monkeypatch):
        mgr = SessionManager(self._seed_api_key_account())
        monkeypatch.setattr(session_mod.shutil, "which", lambda name: "/fake/claude")
        with pytest.raises(SessionError, match="does not support API-key accounts"):
            mgr.run("2", [], share=True)


# ---------------------------------------------------------------------------
# Export / import of raw keys
# ---------------------------------------------------------------------------


class TestExportImport:
    def test_round_trip_preserves_key_and_kind(self, tmp_path: Path):
        src_home = tmp_path / "src"
        (src_home / ".claude").mkdir(parents=True)
        with _patched_home(src_home):
            src = _linux_switcher()
            src.add_account_from_token(API_KEY, slot=1)
            out = tmp_path / "b.cswap"
            export_accounts(src, str(out))
            payload = json.loads(out.read_text(encoding="utf-8"))
            # exported as a raw string, tagged api_key — not a JSON object.
            assert payload["accounts"][0]["credentials"] == API_KEY
            assert payload["accounts"][0]["kind"] == "api_key"

        dst_home = tmp_path / "dst"
        (dst_home / ".claude").mkdir(parents=True)
        with _patched_home(dst_home):
            dst = _linux_switcher()
            import_accounts(dst, str(out))
            assert dst._account_kind("1") == "api_key"
            assert dst._read_account_credentials("1", "api-key-1@token.local") == API_KEY


class _patched_home:
    """Redirect HOME/Path.home() to ``home`` for export/import on two homes."""

    def __init__(self, home: Path):
        self.home = home
        self._patches: list = []

    def __enter__(self):
        import os
        from unittest.mock import patch

        self._patches = [
            patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)}),
            patch("pathlib.Path.home", return_value=self.home),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False
