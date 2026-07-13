"""Tests for Codex account add/list/remove (task 0.1.3), the switcher-level
integration on top of claude_swap.codex_credentials."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap.codex_credentials import codex_auth_path, codex_home
from claude_swap.exceptions import ConfigError, CredentialReadError
from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher
from tests.test_codex_credentials import make_id_token


def _linux_switcher() -> ClaudeAccountSwitcher:
    s = ClaudeAccountSwitcher()
    s.platform = Platform.LINUX
    s._setup_directories()
    s._init_sequence_file()
    return s


def _write_live_codex_auth(email: str, account_id: str = "acct-1") -> None:
    id_token = make_id_token({"email": email, "sub": f"google-oauth2|{account_id}"})
    codex_home().mkdir(parents=True, exist_ok=True)
    codex_auth_path().write_text(
        json.dumps({
            "auth_mode": "chatgpt",
            "tokens": {
                "access_token": "at",
                "account_id": account_id,
                "id_token": id_token,
                "refresh_token": "rt",
            },
        }),
        encoding="utf-8",
    )


class TestAddCodexAccount:
    def test_no_live_login_raises(self, temp_home: Path):
        switcher = _linux_switcher()
        with pytest.raises(ConfigError):
            switcher.add_codex_account()

    def test_api_key_only_mode_raises_config_error(self, temp_home: Path):
        codex_home().mkdir(parents=True)
        codex_auth_path().write_text(
            json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "sk-x", "tokens": None}),
            encoding="utf-8",
        )
        switcher = _linux_switcher()
        with pytest.raises(ConfigError):
            switcher.add_codex_account()

    def test_happy_path_adds_account(self, temp_home: Path):
        _write_live_codex_auth("codex-user@example.com")
        switcher = _linux_switcher()
        switcher.add_codex_account()

        data = switcher._get_sequence_data()
        assert len(data["accounts"]) == 1
        num = next(iter(data["accounts"]))
        record = data["accounts"][num]
        assert record["email"] == "codex-user@example.com"
        assert record["provider"] == "codex"
        assert record["uuid"] == "acct-1"

        stored = switcher._read_codex_account_credentials(num, "codex-user@example.com")
        assert json.loads(stored)["tokens"]["account_id"] == "acct-1"

    def test_refresh_in_place_when_already_added(self, temp_home: Path):
        _write_live_codex_auth("codex-user@example.com")
        switcher = _linux_switcher()
        switcher.add_codex_account()
        data = switcher._get_sequence_data()
        num = next(iter(data["accounts"]))

        # Re-run with a "refreshed" token — should update in place, not add a 2nd slot.
        _write_live_codex_auth("codex-user@example.com", account_id="acct-1-refreshed")
        switcher.add_codex_account()

        data = switcher._get_sequence_data()
        assert len(data["accounts"]) == 1
        stored = switcher._read_codex_account_credentials(num, "codex-user@example.com")
        assert json.loads(stored)["tokens"]["account_id"] == "acct-1-refreshed"

    def test_explicit_slot(self, temp_home: Path):
        _write_live_codex_auth("codex-user@example.com")
        switcher = _linux_switcher()
        switcher.add_codex_account(slot=5)
        data = switcher._get_sequence_data()
        assert "5" in data["accounts"]
        assert data["accounts"]["5"]["provider"] == "codex"

    def test_slot_occupied_by_claude_account_prompts_and_displaces(self, temp_home: Path):
        switcher = _linux_switcher()
        data = switcher._get_sequence_data()
        data["accounts"]["3"] = {
            "email": "claude-user@x.com", "uuid": "u", "organizationUuid": "",
            "organizationName": "", "added": "2024-01-01T00:00:00Z", "provider": "claude",
        }
        data["sequence"] = [3]
        switcher._write_json(switcher.sequence_file, data)

        _write_live_codex_auth("codex-user@example.com")
        with patch("builtins.input", return_value="y") as mock_input:
            switcher.add_codex_account(slot=3)

        mock_input.assert_called_once()
        data = switcher._get_sequence_data()
        assert data["accounts"]["3"]["provider"] == "codex"
        assert data["accounts"]["3"]["email"] == "codex-user@example.com"

    def test_missing_auth_file_after_identity_check_raises_credential_error(
        self, temp_home: Path
    ):
        # Identity decode succeeds from the passed-in dict, but the live file
        # read (read_live_auth_text) is what add_codex_account actually uses
        # for storage — simulate it vanishing between checks.
        _write_live_codex_auth("codex-user@example.com")
        switcher = _linux_switcher()
        with patch(
            "claude_swap.codex_credentials.read_live_auth_text", return_value=None
        ):
            with pytest.raises(CredentialReadError):
                switcher.add_codex_account()


class TestAddCodexAccountFromToken:
    def test_raw_api_key_synthesizes_wrapper_and_placeholder_email(self, temp_home: Path):
        switcher = _linux_switcher()
        switcher.add_codex_account_from_token("sk-raw-openai-key", slot=1)

        data = switcher._get_sequence_data()
        record = data["accounts"]["1"]
        assert record["provider"] == "codex"
        assert record["email"] == "codex-api-key-1@token.local"

        stored = json.loads(switcher._read_codex_account_credentials("1", record["email"]))
        assert stored["auth_mode"] == "apikey"
        assert stored["OPENAI_API_KEY"] == "sk-raw-openai-key"

    def test_raw_api_key_with_explicit_email(self, temp_home: Path):
        switcher = _linux_switcher()
        switcher.add_codex_account_from_token(
            "sk-raw-openai-key", email="dev@example.com", slot=1
        )
        data = switcher._get_sequence_data()
        assert data["accounts"]["1"]["email"] == "dev@example.com"

    def test_full_auth_json_blob_extracts_identity(self, temp_home: Path):
        id_token = make_id_token({"email": "pasted@example.com", "sub": "google-oauth2|9"})
        blob = json.dumps({
            "auth_mode": "chatgpt",
            "tokens": {
                "access_token": "at", "account_id": "acct-9",
                "id_token": id_token, "refresh_token": "rt",
            },
        })
        switcher = _linux_switcher()
        switcher.add_codex_account_from_token(blob, slot=1)

        data = switcher._get_sequence_data()
        assert data["accounts"]["1"]["email"] == "pasted@example.com"
        stored = json.loads(switcher._read_codex_account_credentials("1", "pasted@example.com"))
        assert stored["tokens"]["account_id"] == "acct-9"

    def test_empty_token_raises(self, temp_home: Path):
        from claude_swap.exceptions import ValidationError

        switcher = _linux_switcher()
        with pytest.raises(ValidationError):
            switcher.add_codex_account_from_token("   ", slot=1)

    def test_invalid_email_format_raises(self, temp_home: Path):
        from claude_swap.exceptions import ValidationError

        switcher = _linux_switcher()
        with pytest.raises(ValidationError):
            switcher.add_codex_account_from_token("sk-x", email="not-an-email", slot=1)


class TestListShowsCodexDistinctly:
    def test_json_payload_includes_provider(self, temp_home: Path, capsys):
        _write_live_codex_auth("codex-user@example.com")
        switcher = _linux_switcher()
        switcher.add_codex_account()

        payload = switcher.list_accounts(json_output=True)
        assert len(payload["accounts"]) == 1
        assert payload["accounts"][0]["provider"] == "codex"

    def test_human_output_labels_codex_account(self, temp_home: Path, capsys):
        _write_live_codex_auth("codex-user@example.com")
        switcher = _linux_switcher()
        switcher.add_codex_account()

        switcher.list_accounts()
        out = capsys.readouterr().out
        assert "(codex)" in out

    def test_mixed_fleet_claude_not_labeled(self, temp_home: Path, capsys):
        switcher = _linux_switcher()
        data = switcher._get_sequence_data()
        data["accounts"]["1"] = {
            "email": "claude-user@x.com", "uuid": "u", "organizationUuid": "",
            "organizationName": "", "added": "2024-01-01T00:00:00Z", "provider": "claude",
        }
        data["sequence"] = [1]
        switcher._write_json(switcher.sequence_file, data)

        payload = switcher.list_accounts(json_output=True)
        assert payload["accounts"][0]["provider"] == "claude"


class TestRemoveCodexAccount:
    def test_remove_deletes_codex_store_not_claude_store(self, temp_home: Path, monkeypatch):
        _write_live_codex_auth("codex-user@example.com")
        switcher = _linux_switcher()
        switcher.add_codex_account()
        data = switcher._get_sequence_data()
        num = next(iter(data["accounts"]))

        assert switcher._read_codex_account_credentials(num, "codex-user@example.com")

        monkeypatch.setattr("builtins.input", lambda *a, **k: "y")
        switcher.remove_account(num)

        assert switcher._read_codex_account_credentials(num, "codex-user@example.com") == ""
        data = switcher._get_sequence_data()
        assert num not in data["accounts"]

    def test_remove_by_email_works_for_codex_account(self, temp_home: Path, monkeypatch):
        _write_live_codex_auth("codex-user@example.com")
        switcher = _linux_switcher()
        switcher.add_codex_account()

        monkeypatch.setattr("builtins.input", lambda *a, **k: "y")
        switcher.remove_account("codex-user@example.com")

        data = switcher._get_sequence_data()
        assert data["accounts"] == {}

    def test_removing_codex_account_does_not_touch_claude_slot(self, temp_home: Path, monkeypatch):
        switcher = _linux_switcher()
        data = switcher._get_sequence_data()
        data["accounts"]["1"] = {
            "email": "claude-user@x.com", "uuid": "u", "organizationUuid": "",
            "organizationName": "", "added": "2024-01-01T00:00:00Z", "provider": "claude",
        }
        data["sequence"] = [1]
        switcher._write_json(switcher.sequence_file, data)
        switcher._write_account_credentials("1", "claude-user@x.com", "claude-secret")

        _write_live_codex_auth("codex-user@example.com")
        switcher.add_codex_account(slot=2)

        monkeypatch.setattr("builtins.input", lambda *a, **k: "y")
        switcher.remove_account("2")

        assert switcher._read_account_credentials("1", "claude-user@x.com") == "claude-secret"
