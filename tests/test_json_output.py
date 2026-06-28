"""Tests for ``--json`` structured output (issue #63)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap.exceptions import ConfigError, SwitchError
from claude_swap.json_output import (
    SCHEMA_VERSION,
    error_envelope,
    usage_fields,
    usage_to_json,
)
from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher


# --------------------------------------------------------------------------- #
# Serialization helpers
# --------------------------------------------------------------------------- #
class TestJsonHelpers:
    def test_usage_to_json_maps_keys_and_preserves_raw_reset(self):
        usage = {
            "five_hour": {"pct": 25.0, "resets_at": "2026-06-22T23:00:00Z",
                          "countdown": "4h", "clock": "02:00"},
            "seven_day": {"pct": 16.0},
            "spend": {"used": 12.5, "limit": 300.0, "pct": 4.0, "currency": "USD",
                      "resets_at": "2026-07-01T00:00:00Z"},
        }
        out = usage_to_json(usage)
        assert out["fiveHour"] == {
            "pct": 25.0, "resetsAt": "2026-06-22T23:00:00Z",
            "countdown": "4h", "clock": "02:00",
        }
        # seven_day had no reset → only pct, camelCased key
        assert out["sevenDay"] == {"pct": 16.0}
        assert out["spend"]["used"] == 12.5
        assert out["spend"]["resetsAt"] == "2026-07-01T00:00:00Z"

    def test_usage_fields_variants(self):
        assert usage_fields({"five_hour": {"pct": 1.0}})[0] == "ok"
        assert usage_fields({"five_hour": {"pct": 1.0}})[1] == {"fiveHour": {"pct": 1.0}}
        assert usage_fields("no credentials") == ("no_credentials", None)
        assert usage_fields(None) == ("unavailable", None)

    def test_error_envelope_shape(self):
        env = error_envelope(SwitchError("boom"))
        assert env == {
            "schemaVersion": SCHEMA_VERSION,
            "error": {"type": "SwitchError", "message": "boom"},
        }


# --------------------------------------------------------------------------- #
# --list --json
# --------------------------------------------------------------------------- #
class TestListJson:
    def test_empty_list_no_prompt(self, temp_home: Path):
        """No accounts in JSON mode returns an empty payload — never prompts."""
        switcher = ClaudeAccountSwitcher()
        with patch.object(switcher, "_first_run_setup") as first_run, \
             patch("builtins.input") as fake_input:
            payload = switcher.list_accounts(json_output=True)
        first_run.assert_not_called()
        fake_input.assert_not_called()
        assert payload == {
            "schemaVersion": SCHEMA_VERSION,
            "activeAccountNumber": None,
            "accounts": [],
        }

    def test_list_payload(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, capsys,
    ):
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})
        backup_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-backup"}})
        usage = {
            "five_hour": {"pct": 10.0, "resets_at": "2026-01-01T00:00:00Z",
                          "countdown": "1h", "clock": "01:00"},
        }

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        with patch.object(switcher, "_read_credentials", return_value=active_creds), \
             patch.object(switcher, "_read_account_credentials", return_value=backup_creds), \
             patch("claude_swap.oauth.fetch_usage_for_account", return_value=usage):
            payload = switcher.list_accounts(json_output=True)

        # Method itself prints nothing — the CLI serializes.
        assert capsys.readouterr().out == ""
        assert payload["schemaVersion"] == SCHEMA_VERSION
        assert payload["activeAccountNumber"] == 1  # live-resolved active slot
        acct1 = next(a for a in payload["accounts"] if a["number"] == 1)
        assert acct1["active"] is True
        assert acct1["usageStatus"] == "ok"
        assert acct1["usage"]["fiveHour"]["resetsAt"] == "2026-01-01T00:00:00Z"

    def test_usage_status_no_credentials_and_unavailable(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        # Account 1 active with creds but the fetch fails (None → unavailable);
        # account 2 has no backup creds (→ no_credentials).
        with patch.object(switcher, "_read_credentials", return_value=active_creds), \
             patch.object(switcher, "_read_account_credentials", return_value=""), \
             patch("claude_swap.oauth.fetch_usage_for_account", return_value=None):
            payload = switcher.list_accounts(json_output=True)

        by_num = {a["number"]: a for a in payload["accounts"]}
        assert by_num[1]["usageStatus"] == "unavailable"
        assert by_num[1]["usage"] is None
        assert by_num[2]["usageStatus"] == "no_credentials"


# --------------------------------------------------------------------------- #
# --status --json
# --------------------------------------------------------------------------- #
class TestStatusJson:
    def test_status_no_active(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        assert switcher.status(json_output=True) == {
            "schemaVersion": SCHEMA_VERSION,
            "active": None,
        }

    def test_status_unmanaged(self, temp_home: Path, mock_claude_config: Path):
        switcher = ClaudeAccountSwitcher()
        payload = switcher.status(json_output=True)
        assert payload["active"] == {"email": "test@example.com", "managed": False}

    def test_status_managed(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, capsys,
    ):
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})
        usage = {"five_hour": {"pct": 25.0, "resets_at": "2026-01-01T00:00:00Z",
                               "countdown": "1h", "clock": "01:00"}}

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        with patch.object(switcher, "_read_credentials", return_value=active_creds), \
             patch("claude_swap.oauth.fetch_usage_for_account", return_value=usage):
            payload = switcher.status(json_output=True)

        assert capsys.readouterr().out == ""
        active = payload["active"]
        assert active["number"] == 1
        assert active["managed"] is True
        assert active["usageStatus"] == "ok"
        assert active["usage"]["fiveHour"]["resetsAt"] == "2026-01-01T00:00:00Z"
        assert payload["totalManagedAccounts"] == 2


# --------------------------------------------------------------------------- #
# --switch / --switch-to --json
# --------------------------------------------------------------------------- #
def _two_account_stores(temp_home: Path, sample_sequence_data: dict):
    """Switcher with accounts 1 (active) & 2, backed by in-memory cred/config stores."""
    sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
    switcher = ClaudeAccountSwitcher()
    switcher._setup_directories()
    switcher.platform = Platform.LINUX
    switcher._write_json(switcher.sequence_file, sample_sequence_data)

    live_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-1", "refreshToken": "rt-1"}})
    (temp_home / ".claude" / ".credentials.json").write_text(live_creds)

    creds_store = {
        ("1", "test@example.com"): live_creds,
        ("2", "account2@example.com"): json.dumps(
            {"claudeAiOauth": {"accessToken": "sk-2", "refreshToken": "rt-2"}}
        ),
    }
    configs_store = {
        ("1", "test@example.com"): json.dumps(
            {"oauthAccount": {"emailAddress": "test@example.com", "accountUuid": "test-uuid-1234"}}
        ),
        ("2", "account2@example.com"): json.dumps(
            {"oauthAccount": {"emailAddress": "account2@example.com", "accountUuid": "uuid-2"}}
        ),
    }
    return switcher, creds_store, configs_store, {"creds": live_creds}


def _install_patches(switcher, creds_store, configs_store, live_state):
    patches = [
        patch.object(switcher, "_read_account_credentials",
                     side_effect=lambda n, e: creds_store.get((str(n), e), "")),
        patch.object(switcher, "_write_account_credentials",
                     side_effect=lambda n, e, c: creds_store.__setitem__((str(n), e), c)),
        patch.object(switcher, "_read_account_config",
                     side_effect=lambda n, e: configs_store.get((str(n), e), "")),
        patch.object(switcher, "_write_account_config",
                     side_effect=lambda n, e, c: configs_store.__setitem__((str(n), e), c)),
        patch.object(switcher, "_read_credentials",
                     side_effect=lambda: live_state.get("creds", "")),
        patch.object(switcher, "_write_credentials",
                     side_effect=lambda c: live_state.__setitem__("creds", c)),
        # Don't make network calls from the (suppressed) post-switch usage path.
        patch("claude_swap.oauth.fetch_usage_for_account", return_value=None),
    ]
    for p in patches:
        p.start()
    return patches


class TestSwitchJson:
    def test_switch_to_result_no_leakage(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, capsys,
    ):
        switcher, creds, configs, live = _two_account_stores(temp_home, sample_sequence_data)
        patches = _install_patches(switcher, creds, configs, live)
        try:
            result = switcher.switch_to("2", json_output=True)
        finally:
            for p in patches:
                p.stop()

        # No human output leaked onto stdout — the method only returns the dict.
        assert capsys.readouterr().out == ""
        assert result["switched"] is True
        assert result["strategy"] == "direct"
        assert result["reason"] == "switched"
        assert result["from"] == {"number": 1, "email": "test@example.com"}
        assert result["to"] == {"number": 2, "email": "account2@example.com"}
        assert result["warnings"] == []

    def test_switch_to_already_active_short_circuits(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        """--switch-to onto the active account is a no-op: no mutation at all."""
        switcher, creds, configs, live = _two_account_stores(temp_home, sample_sequence_data)
        patches = _install_patches(switcher, creds, configs, live)
        try:
            with patch.object(switcher, "_perform_switch") as perform:
                result = switcher.switch_to("1", json_output=True)
        finally:
            for p in patches:
                p.stop()
        perform.assert_not_called()  # short-circuited before any write
        assert result["switched"] is False
        assert result["reason"] == "already-active"
        assert result["from"] == result["to"] == {"number": 1, "email": "test@example.com"}

    def test_noop_from_equals_to(
        self, temp_home: Path, mock_claude_config: Path,
    ):
        """Every switched:false payload reports from == to (the current account)."""
        single = {
            "activeAccountNumber": 1,
            "lastUpdated": "2024-01-01T00:00:00Z",
            "sequence": [1],
            "accounts": {"1": {"email": "test@example.com", "uuid": "u1",
                               "added": "2024-01-01T00:00:00Z"}},
        }
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, single)
        result = switcher.switch(json_output=True)
        assert result["switched"] is False
        assert result["from"] == result["to"] == {"number": 1, "email": "test@example.com"}

    def test_switch_to_from_unmanaged_account(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        """Current live account unmanaged → --switch-to proceeds, from.number is null."""
        # Managed accounts use other emails; the live account (test@example.com)
        # is not among them.
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher.platform = Platform.LINUX
        switcher._write_json(switcher.sequence_file, sample_sequence_data)
        live_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-x"}})
        (temp_home / ".claude" / ".credentials.json").write_text(live_creds)
        creds = {("2", "account2@example.com"): json.dumps(
            {"claudeAiOauth": {"accessToken": "sk-2"}})}
        configs = {("2", "account2@example.com"): json.dumps(
            {"oauthAccount": {"emailAddress": "account2@example.com", "accountUuid": "uuid-2"}})}
        patches = _install_patches(switcher, creds, configs, {"creds": live_creds})
        try:
            result = switcher.switch_to("2", json_output=True)
        finally:
            for p in patches:
                p.stop()
        assert result["switched"] is True
        assert result["from"] == {"number": None, "email": "test@example.com"}
        assert result["to"]["number"] == 2

    def test_switch_to_ambiguous_email_raises(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data_with_org: dict,
    ):
        """Ambiguous email in JSON mode raises (no interactive prompt)."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data_with_org)
        with patch("builtins.input") as fake_input:
            with pytest.raises(ConfigError, match="ambiguous"):
                switcher.switch_to("user@example.com", json_output=True)
        fake_input.assert_not_called()

    def test_switch_only_one_account(
        self, temp_home: Path, mock_claude_config: Path,
    ):
        single = {
            "activeAccountNumber": 1,
            "lastUpdated": "2024-01-01T00:00:00Z",
            "sequence": [1],
            "accounts": {"1": {"email": "test@example.com", "uuid": "u1",
                               "added": "2024-01-01T00:00:00Z"}},
        }
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, single)
        result = switcher.switch(json_output=True)
        assert result["switched"] is False
        assert result["reason"] == "only-one-account"

    def test_switch_unmanaged_account_is_noop_without_add(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        """Plain --switch from an unmanaged account: structured no-op, no auto-add."""
        # Live account (test@example.com) not in the managed set.
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)
        with patch.object(switcher, "add_account") as add:
            result = switcher.switch(json_output=True)
        add.assert_not_called()
        assert result["switched"] is False
        assert result["reason"] == "unmanaged-account"
        assert result["from"] == {"number": None, "email": "test@example.com"}
