"""Tests for the CLAUDE_SWAP_ROTATE_ONLY auto-rotation allowlist.

The env var narrows *automatic* selection (`switchable_account_numbers`) to a
named pool without touching the per-account `cswap disable` blocklist or
explicit `cswap switch` targets.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_swap.models import Platform
from claude_swap.switcher import ROTATE_ONLY_ENV, ClaudeAccountSwitcher


class TestRotateOnlyAllowlist:
    def _setup(self, temp_home: Path) -> ClaudeAccountSwitcher:
        s = ClaudeAccountSwitcher()
        s.platform = Platform.LINUX
        s._setup_directories()
        s._init_sequence_file()
        return s

    def _seed(self, s: ClaudeAccountSwitcher, num: int, email: str) -> None:
        s._write_account_credentials(
            str(num),
            email,
            json.dumps({"claudeAiOauth": {
                "accessToken": f"sk-{num}", "refreshToken": f"rt-{num}"}}),
        )
        s._write_account_config(
            str(num),
            email,
            json.dumps({"oauthAccount": {
                "emailAddress": email, "accountUuid": f"uuid-{num}"}}),
        )
        data = s._get_sequence_data() or {
            "activeAccountNumber": None, "lastUpdated": "",
            "sequence": [], "accounts": {},
        }
        data["accounts"][str(num)] = {
            "email": email, "uuid": f"uuid-{num}",
            "organizationUuid": "", "organizationName": "",
            "added": "2024-01-01T00:00:00Z",
        }
        if num not in data["sequence"]:
            data["sequence"].append(num)
            data["sequence"].sort()
        if data["activeAccountNumber"] is None:
            data["activeAccountNumber"] = num
        s._write_json(s.sequence_file, data)

    def _seed_three(self, temp_home: Path) -> ClaudeAccountSwitcher:
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._seed(s, 3, "c@example.com")
        return s

    def test_unset_env_no_restriction(self, temp_home, monkeypatch):
        monkeypatch.delenv(ROTATE_ONLY_ENV, raising=False)
        s = self._seed_three(temp_home)
        assert s.switchable_account_numbers() == ["1", "2", "3"]

    def test_blank_env_no_restriction(self, temp_home, monkeypatch):
        monkeypatch.setenv(ROTATE_ONLY_ENV, "   ")
        s = self._seed_three(temp_home)
        assert s.switchable_account_numbers() == ["1", "2", "3"]

    def test_allowlist_by_number(self, temp_home, monkeypatch):
        monkeypatch.setenv(ROTATE_ONLY_ENV, "1,3")
        s = self._seed_three(temp_home)
        assert s.switchable_account_numbers() == ["1", "3"]

    def test_allowlist_by_email(self, temp_home, monkeypatch):
        monkeypatch.setenv(ROTATE_ONLY_ENV, "b@example.com")
        s = self._seed_three(temp_home)
        assert s.switchable_account_numbers() == ["2"]

    def test_allowlist_by_alias(self, temp_home, monkeypatch):
        s = self._seed_three(temp_home)
        s.set_alias("3", "prod")
        monkeypatch.setenv(ROTATE_ONLY_ENV, "prod")
        assert s.switchable_account_numbers() == ["3"]

    def test_whitespace_and_comma_separators_mix(self, temp_home, monkeypatch):
        monkeypatch.setenv(ROTATE_ONLY_ENV, " 1 ,\tc@example.com ")
        s = self._seed_three(temp_home)
        assert s.switchable_account_numbers() == ["1", "3"]

    def test_preserves_sequence_order(self, temp_home, monkeypatch):
        # Listed out of order, but the result keeps sequence order.
        monkeypatch.setenv(ROTATE_ONLY_ENV, "3,1")
        s = self._seed_three(temp_home)
        assert s.switchable_account_numbers() == ["1", "3"]

    def test_unknown_token_ignored(self, temp_home, monkeypatch):
        monkeypatch.setenv(ROTATE_ONLY_ENV, "1,does-not-exist")
        s = self._seed_three(temp_home)
        assert s.switchable_account_numbers() == ["1"]

    def test_all_tokens_unknown_yields_empty(self, temp_home, monkeypatch):
        # A pool of only typos rotates across nothing — distinct from "unset".
        monkeypatch.setenv(ROTATE_ONLY_ENV, "typo1,typo2")
        s = self._seed_three(temp_home)
        assert s.switchable_account_numbers() == []

    def test_allowlist_intersects_with_disabled(self, temp_home, monkeypatch):
        # disable is still honored: an allowlisted-but-disabled slot stays out.
        monkeypatch.setenv(ROTATE_ONLY_ENV, "1,2")
        s = self._seed_three(temp_home)
        s.set_account_disabled("2", True)
        assert s.switchable_account_numbers() == ["1"]
