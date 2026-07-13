"""Tests for Codex CLI credential read/write (claude_swap.codex_credentials)."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from unittest.mock import patch

from claude_swap.codex_credentials import (
    CodexCredentialStore,
    codex_auth_path,
    codex_home,
    decode_id_token_claims,
    get_current_codex_identity,
    get_live_api_key,
    read_live_auth,
    read_live_auth_text,
)
from claude_swap.models import Platform


def _b64url(data: dict) -> str:
    raw = json.dumps(data).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def make_id_token(claims: dict) -> str:
    """A syntactically valid (unsigned) JWT carrying ``claims`` as its payload."""
    header = _b64url({"alg": "none", "typ": "JWT"})
    payload = _b64url(claims)
    return f"{header}.{payload}.signature"


class TestCodexHome:
    def test_defaults_to_home_dot_codex(self, temp_home: Path):
        assert codex_home() == temp_home / ".codex"
        assert codex_auth_path() == temp_home / ".codex" / "auth.json"

    def test_respects_codex_home_env_var(self, temp_home: Path, monkeypatch):
        override = temp_home / "custom-codex"
        monkeypatch.setenv("CODEX_HOME", str(override))
        assert codex_home() == override


class TestReadLiveAuth:
    def test_missing_file_returns_none(self, temp_home: Path):
        assert read_live_auth() is None
        assert read_live_auth_text() is None

    def test_corrupt_json_returns_none(self, temp_home: Path):
        codex_home().mkdir(parents=True)
        codex_auth_path().write_text("not json", encoding="utf-8")
        assert read_live_auth() is None

    def test_valid_file_parses(self, temp_home: Path):
        codex_home().mkdir(parents=True)
        codex_auth_path().write_text(json.dumps({"auth_mode": "chatgpt"}), encoding="utf-8")
        assert read_live_auth() == {"auth_mode": "chatgpt"}
        assert json.loads(read_live_auth_text()) == {"auth_mode": "chatgpt"}


class TestDecodeIdTokenClaims:
    def test_valid_token_decodes(self):
        token = make_id_token({"email": "a@x.com", "sub": "google-oauth2|123"})
        claims = decode_id_token_claims(token)
        assert claims["email"] == "a@x.com"
        assert claims["sub"] == "google-oauth2|123"

    def test_malformed_token_returns_none(self):
        assert decode_id_token_claims("not-a-jwt") is None
        assert decode_id_token_claims("a.b") is None  # only 2 segments
        assert decode_id_token_claims("a.!!!notb64!!!.c") is None


class TestGetCurrentCodexIdentity:
    def test_no_live_auth_returns_none(self, temp_home: Path):
        assert get_current_codex_identity() is None

    def test_api_key_only_mode_returns_none(self, temp_home: Path):
        codex_home().mkdir(parents=True)
        codex_auth_path().write_text(
            json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "sk-abc", "tokens": None}),
            encoding="utf-8",
        )
        assert get_current_codex_identity() is None

    def test_oauth_mode_extracts_email_and_account_id(self, temp_home: Path):
        id_token = make_id_token({"email": "stephrajm@gmail.com", "sub": "google-oauth2|105286749399035119563"})
        codex_home().mkdir(parents=True)
        codex_auth_path().write_text(
            json.dumps({
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": "at",
                    "account_id": "acct-123",
                    "id_token": id_token,
                    "refresh_token": "rt",
                },
            }),
            encoding="utf-8",
        )
        identity = get_current_codex_identity()
        assert identity == ("stephrajm@gmail.com", "acct-123")

    def test_id_token_without_email_claim_returns_none(self, temp_home: Path):
        id_token = make_id_token({"sub": "google-oauth2|123"})  # no email
        assert get_current_codex_identity({
            "tokens": {"id_token": id_token, "account_id": "x"}
        }) is None


class TestGetLiveApiKey:
    def test_returns_key_when_present(self):
        assert get_live_api_key({"OPENAI_API_KEY": "sk-abc"}) == "sk-abc"

    def test_none_when_absent_or_empty(self):
        assert get_live_api_key({"OPENAI_API_KEY": ""}) is None
        assert get_live_api_key({}) is None
        assert get_live_api_key(None) is None


class TestCodexCredentialStoreFileBackend:
    def test_round_trip_linux(self, tmp_path: Path):
        store = CodexCredentialStore(tmp_path, Platform.LINUX)
        store.write("1", "a@x.com", '{"auth_mode":"chatgpt"}')
        assert store.read("1", "a@x.com") == '{"auth_mode":"chatgpt"}'

    def test_read_missing_returns_empty_string(self, tmp_path: Path):
        store = CodexCredentialStore(tmp_path, Platform.LINUX)
        assert store.read("99", "nope@x.com") == ""

    def test_delete_removes_file(self, tmp_path: Path):
        store = CodexCredentialStore(tmp_path, Platform.LINUX)
        store.write("1", "a@x.com", "blob")
        store.delete("1", "a@x.com")
        assert store.read("1", "a@x.com") == ""

    def test_enc_file_is_base64_not_plaintext(self, tmp_path: Path):
        store = CodexCredentialStore(tmp_path, Platform.LINUX)
        store.write("1", "a@x.com", "super-secret-blob")
        enc_path = tmp_path / ".codex-creds-1-a@x.com.enc"
        assert enc_path.exists()
        assert "super-secret-blob" not in enc_path.read_text(encoding="utf-8")


class TestCodexCredentialStoreMacKeychain:
    def test_round_trip_uses_keychain_not_file(self, tmp_path: Path):
        store = CodexCredentialStore(tmp_path, Platform.MACOS)
        with patch("claude_swap.codex_credentials.macos_keychain") as mock_kc:
            mock_kc.KEYCHAIN_ERRORS = ()
            written = {}

            def _set(service, account, value):
                written[(service, account)] = value

            def _get(service, account):
                return written.get((service, account))

            mock_kc.set_password.side_effect = _set
            mock_kc.get_password.side_effect = _get

            store.write("1", "a@x.com", "blob")
            assert store.read("1", "a@x.com") == "blob"

        enc_path = tmp_path / ".codex-creds-1-a@x.com.enc"
        assert not enc_path.exists()

    def test_keychain_failure_falls_back_to_file(self, tmp_path: Path):
        store = CodexCredentialStore(tmp_path, Platform.MACOS)
        with patch("claude_swap.codex_credentials.macos_keychain") as mock_kc:
            from claude_swap.macos_keychain import KeychainError

            mock_kc.KEYCHAIN_ERRORS = (KeychainError,)
            mock_kc.set_password.side_effect = KeychainError("locked")

            store.write("1", "a@x.com", "blob")

        enc_path = tmp_path / ".codex-creds-1-a@x.com.enc"
        assert enc_path.exists()
