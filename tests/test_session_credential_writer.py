"""Access-token-only session credentials: builder and writer (Refs #382)."""

from __future__ import annotations

import json
import sys

import pytest

from claude_swap import session_credentials as sc

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
