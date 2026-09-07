"""Parsing ~/.codex/auth.json into an identity cswap can match to a slot."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from claude_swap.codex import auth_file
from tests.conftest_codex import make_auth_json, strip_account_claim

ACCOUNT = "2f4dac8f-f15f-4c58-a567-e96985d51cfd"
USER = "user-K6XCCWw4gcRpfaGR6VQKAFgA"


def test_account_key_joins_user_and_account():
    """codex-auth's format, kept byte-identical so imported keys match ours."""
    assert auth_file.account_key(USER, ACCOUNT) == f"{USER}::{ACCOUNT}"


def test_file_key_is_unpadded_base64url_of_the_account_key():
    """Verified against a real codex-auth snapshot filename."""
    key = auth_file.account_key(USER, ACCOUNT)
    assert auth_file.file_key(key) == (
        "dXNlci1LNlhDQ1d3NGdjUnBmYUdSNlZRS0FGZ0E6OjJmNGRhYzhmLWYxNWYtNGM1OC1hNTY3"
        "LWU5Njk4NWQ1MWNmZA"
    )


def test_identity_comes_from_the_jwt_claims():
    ident = auth_file.parse_identity(make_auth_json(email="me@example.com"))
    assert ident is not None
    assert ident.account_id == ACCOUNT
    assert ident.user_id == USER
    assert ident.email == "me@example.com"
    assert ident.plan == "pro"
    assert ident.account_key == f"{USER}::{ACCOUNT}"


def test_tokens_account_id_wins_over_the_jwt_claim():
    """The live file's ``tokens.account_id`` is what the codex CLI itself uses
    to pick a workspace, so it is the authority when the two disagree."""
    payload = make_auth_json()
    payload["tokens"]["account_id"] = "other-account"
    ident = auth_file.parse_identity(payload)
    assert ident.account_id == "other-account"


def test_organization_fallback_prefers_the_default_org():
    """Phone-login auth files carry neither tokens.account_id nor the account
    claim; codex-auth falls back to the default organization, and so do we."""
    payload = strip_account_claim(
        make_auth_json(
            organizations=[
                {"id": "org-first", "is_default": False},
                {"id": "org-default", "is_default": True},
            ]
        )
    )
    assert auth_file.parse_identity(payload).account_id == "org-default"


def test_organization_fallback_takes_the_first_when_none_is_default():
    payload = strip_account_claim(
        make_auth_json(organizations=[{"id": "org-first"}, {"id": "org-second"}])
    )
    assert auth_file.parse_identity(payload).account_id == "org-first"


def test_api_key_account_yields_an_identity_with_no_tokens():
    """auth_mode=apikey has no OAuth tokens: neither usage nor refresh applies,
    and it must not crash the parser."""
    payload = {"auth_mode": "apikey", "OPENAI_API_KEY": "sk-x", "tokens": None}
    ident = auth_file.parse_identity(payload)
    assert ident is not None
    assert ident.is_api_key is True
    assert ident.account_id == ""
    assert ident.is_identifiable is False


def test_unparseable_payload_yields_none():
    assert auth_file.parse_identity({"tokens": {"id_token": "not-a-jwt"}}) is None
    assert auth_file.parse_identity({}) is None
    assert auth_file.parse_identity("nonsense") is None


def test_read_live_identity_returns_none_when_the_file_is_absent(codex_home: Path):
    assert auth_file.read_live_identity() is None


def test_read_live_identity_reads_the_live_file(live_auth):
    live_auth()
    ident = auth_file.read_live_identity()
    assert ident is not None and ident.account_id == ACCOUNT


def test_read_live_identity_survives_a_torn_write(codex_home: Path):
    """A half-written auth.json is transient, not fatal — the next pass re-reads."""
    (codex_home / "auth.json").write_text('{"tokens": {')
    assert auth_file.read_live_identity() is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes are not meaningful")
def test_write_live_auth_preserves_the_existing_file_mode(codex_home: Path, live_auth):
    """codex-auth documents preserving the live file's mode on switch; a
    switch must not silently re-permission a file the codex CLI owns."""
    path = live_auth()
    os.chmod(path, 0o644)
    auth_file.write_live_auth(make_auth_json(email="b@example.com"))
    assert (os.stat(path).st_mode & 0o777) == 0o644


def test_write_live_auth_creates_a_private_file_when_absent(codex_home: Path):
    auth_file.write_live_auth(make_auth_json())
    path = codex_home / "auth.json"
    assert path.exists()
    if sys.platform != "win32":
        assert (os.stat(path).st_mode & 0o777) == 0o600


def test_write_live_auth_round_trips_the_payload(codex_home: Path):
    payload = make_auth_json(email="round@example.com")
    auth_file.write_live_auth(payload)
    assert auth_file.read_live_payload() == payload


def test_write_live_auth_leaves_no_temp_file_behind(codex_home: Path):
    auth_file.write_live_auth(make_auth_json())
    assert list(codex_home.glob("*.tmp")) == []


def test_token_expiry_is_read_from_the_access_token():
    assert auth_file.access_token_expiry(make_auth_json(exp=1_800_000_000)) == 1_800_000_000


def test_token_expiry_is_none_when_unreadable():
    assert auth_file.access_token_expiry({"tokens": {"access_token": "x"}}) is None
    assert auth_file.access_token_expiry({}) is None
