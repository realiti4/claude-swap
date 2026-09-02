"""Tests for the ClaudeAccountSwitcher class."""

from __future__ import annotations

import builtins
import base64
import errno
import json
import logging
import os
import stat
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from claude_swap import macos_keychain
from claude_swap import oauth
from claude_swap.json_output import USAGE_FOREIGN_CREDENTIAL, USAGE_TOKEN_EXPIRED
from claude_swap.exceptions import (
    AccountNotFoundError,
    ConfigError,
    CredentialReadError,
    SessionError,
    SwitchError,
    ValidationError,
)
from claude_swap.usage_store import FetchRecord, UsageEntry, UsageStore
from claude_swap.macos_keychain import KeychainError
from claude_swap.models import Platform, _restore_atomically, normalize_alias
from claude_swap.paths import get_backup_root, get_credentials_path
from claude_swap.session import mark_session_stale
from claude_swap.credentials import ActiveCredentials
from claude_swap.switcher import (
    CLAUDE_CODE_KEYCHAIN_SERVICE,
    ClaudeAccountSwitcher,
    SECURITY_SERVICE,
    SETUP_TOKEN_SCOPES,
    _format_usage_lines,
)


def _raise_locked(*args, **kwargs):
    """Stand-in for a locked/unavailable Keychain operation."""
    raise KeychainError("locked")


class TestEmailValidation:
    """Test email validation."""

    def test_valid_emails(self, temp_home: Path):
        """Test that valid emails pass validation."""
        switcher = ClaudeAccountSwitcher()
        valid_emails = [
            "user@example.com",
            "user.name@example.co.uk",
            "user+tag@example.org",
            "user123@test.io",
        ]
        for email in valid_emails:
            assert switcher._validate_email(email), f"Expected {email} to be valid"

    def test_invalid_emails(self, temp_home: Path):
        """Test that invalid emails fail validation."""
        switcher = ClaudeAccountSwitcher()
        invalid_emails = [
            "not-an-email",
            "@example.com",
            "user@",
            "user@.com",
            "",
            "user@com",
        ]
        for email in invalid_emails:
            assert not switcher._validate_email(email), f"Expected {email} to be invalid"


class TestFindAccountSlot:
    """Test the (email, organizationUuid) -> slot composite-key lookup."""

    DATA = {
        "accounts": {
            "1": {"email": "user@example.com", "organizationUuid": ""},
            "2": {"email": "user@example.com", "organizationUuid": "org-123"},
            "3": {"email": "other@example.com"},  # legacy record, no org field
        }
    }

    def test_matches_composite_identity(self):
        assert (
            ClaudeAccountSwitcher._find_account_slot(
                self.DATA, "user@example.com", "org-123"
            )
            == "2"
        )

    def test_same_email_wrong_org_is_no_match(self):
        assert (
            ClaudeAccountSwitcher._find_account_slot(
                self.DATA, "user@example.com", "org-999"
            )
            is None
        )

    def test_absent_email_is_no_match(self):
        assert (
            ClaudeAccountSwitcher._find_account_slot(
                self.DATA, "nobody@example.com", ""
            )
            is None
        )

    def test_empty_org_matches_missing_or_empty_org_field(self):
        # Slot 1 has organizationUuid "", slot 3 omits the field entirely; both
        # are personal accounts and must match an empty org_uuid query.
        assert (
            ClaudeAccountSwitcher._find_account_slot(self.DATA, "user@example.com", "")
            == "1"
        )
        assert (
            ClaudeAccountSwitcher._find_account_slot(self.DATA, "other@example.com", "")
            == "3"
        )

    def test_empty_data_is_no_match(self):
        assert ClaudeAccountSwitcher._find_account_slot({}, "user@example.com", "") is None


class TestPlatformDetection:
    """Test platform detection."""

    @patch("sys.platform", "darwin")
    def test_macos_detection(self, temp_home: Path):
        """Test macOS platform detection."""
        assert Platform.detect() == Platform.MACOS

    @patch("sys.platform", "linux")
    def test_linux_detection(self, temp_home: Path):
        """Test Linux platform detection."""
        # Ensure WSL_DISTRO_NAME is not set
        env = os.environ.copy()
        env.pop("WSL_DISTRO_NAME", None)
        with patch.dict(os.environ, env, clear=True):
            assert Platform.detect() == Platform.LINUX

    @patch("sys.platform", "linux")
    @patch.dict(os.environ, {"WSL_DISTRO_NAME": "Ubuntu"})
    def test_wsl_detection(self, temp_home: Path):
        """Test WSL platform detection."""
        assert Platform.detect() == Platform.WSL

    @patch("sys.platform", "win32")
    def test_windows_detection(self, temp_home: Path):
        """Test Windows platform detection."""
        assert Platform.detect() == Platform.WINDOWS

    @patch("sys.platform", "freebsd")
    def test_unknown_platform(self, temp_home: Path):
        """Test unknown platform detection."""
        assert Platform.detect() == Platform.UNKNOWN


class TestJsonOperations:
    """Test JSON read/write operations."""

    def test_write_and_read_json(self, temp_home: Path):
        """Test writing and reading JSON files."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()

        test_path = switcher.backup_dir / "test.json"
        test_data = {"key": "value", "number": 42, "nested": {"a": 1}}

        switcher._write_json(test_path, test_data)
        result = switcher._read_json(test_path)

        assert result == test_data

    def test_read_nonexistent_json(self, temp_home: Path):
        """Test reading non-existent JSON file returns None."""
        switcher = ClaudeAccountSwitcher()
        result = switcher._read_json(Path("/nonexistent/path.json"))
        assert result is None

    def test_read_invalid_json(self, temp_home: Path):
        """Test reading invalid JSON file returns None."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()

        test_path = switcher.backup_dir / "invalid.json"
        test_path.write_text("not valid json {{{")

        result = switcher._read_json(test_path)
        assert result is None

    @pytest.mark.skipif(sys.platform == "win32", reason="File permissions work differently on Windows")
    def test_json_file_permissions(self, temp_home: Path):
        """Test that JSON files are written with correct permissions."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()

        test_path = switcher.backup_dir / "secure.json"
        switcher._write_json(test_path, {"secret": "data"})

        # Check file permissions (0o600 = owner read/write only)
        stat = test_path.stat()
        assert stat.st_mode & 0o777 == 0o600


class TestGetCurrentAccount:
    """Test getting current account."""

    def test_no_config_file(self, temp_home: Path):
        """Test when no config file exists."""
        switcher = ClaudeAccountSwitcher()
        assert switcher._get_current_account() is None

    def test_with_valid_config(self, temp_home: Path, mock_claude_config: Path):
        """Test reading email from valid config."""
        switcher = ClaudeAccountSwitcher()
        assert switcher._get_current_account() == ("test@example.com", "")

    def test_config_without_oauth(self, temp_home: Path):
        """Test config file without oauthAccount."""
        config_path = temp_home / ".claude.json"
        config_path.write_text(json.dumps({"other": "data"}))

        switcher = ClaudeAccountSwitcher()
        assert switcher._get_current_account() is None

    def test_config_with_empty_email(self, temp_home: Path):
        """Test config with empty email address."""
        config_path = temp_home / ".claude.json"
        config_path.write_text(
            json.dumps({"oauthAccount": {"emailAddress": "", "accountUuid": "uuid"}})
        )

        switcher = ClaudeAccountSwitcher()
        assert switcher._get_current_account() is None


class TestGetClaudeConfigPathUtf8:
    """Regression: Windows default encoding must not break UTF-8 Claude configs."""

    def test_fallback_config_with_unicode_punctuation(self, temp_home: Path):
        """~/.claude.json with non-ASCII (e.g. smart quotes) must be readable."""
        config = {
            "oauthAccount": {
                "emailAddress": "user@example.com",
                "accountUuid": "uuid-1",
                "displayName": "Name with \u201csmart\u201d quotes",
            }
        }
        fallback = temp_home / ".claude.json"
        fallback.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

        switcher = ClaudeAccountSwitcher()
        resolved = switcher._get_claude_config_path()
        assert resolved == fallback


class TestAccountExists:
    """Test account existence checking."""

    def test_account_exists(self, temp_home: Path, sample_sequence_data: dict):
        """Test checking if account exists."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        assert switcher._account_exists("account1@example.com", "") is True
        assert switcher._account_exists("nonexistent@example.com", "") is False

    def test_no_sequence_file(self, temp_home: Path):
        """Test account exists when no sequence file."""
        switcher = ClaudeAccountSwitcher()
        assert switcher._account_exists("any@example.com", "") is False


class TestResolveAccountIdentifier:
    """Test resolving account identifiers."""

    def test_resolve_by_number(self, temp_home: Path, sample_sequence_data: dict):
        """Test resolving account by number."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        assert switcher._resolve_account_identifier("1") == "1"
        assert switcher._resolve_account_identifier("2") == "2"

    def test_resolve_by_email(self, temp_home: Path, sample_sequence_data: dict):
        """Test resolving account by email."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        assert switcher._resolve_account_identifier("account1@example.com") == "1"
        assert switcher._resolve_account_identifier("account2@example.com") == "2"

    def test_resolve_nonexistent(self, temp_home: Path, sample_sequence_data: dict):
        """Test resolving non-existent account."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        assert switcher._resolve_account_identifier("nonexistent@example.com") is None
        assert switcher._resolve_account_identifier("999") == "999"  # Numbers pass through


class TestAliasValidation:
    """Test alias format validation (models.normalize_alias)."""

    def test_valid_aliases(self, temp_home: Path):
        for alias in ["dev", "work-1", "client_a", "team.b", "DEV"]:
            normalize_alias(alias)  # must not raise

    def test_invalid_aliases(self, temp_home: Path):
        for alias in ["123", "dev@work", "dev work", "", "dev/work", "-dev"]:
            with pytest.raises(ValueError):
                normalize_alias(alias)


class TestResolveByAlias:
    """Test resolving account identifiers via alias, and precedence."""

    def _write(self, switcher, data):
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, data)

    def test_empty_identifier_never_matches_aliasless_account(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """An empty identifier must not silently resolve to whichever account
        happens to have no alias set — it should just never match."""
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)

        assert switcher._find_account_by_alias("") is None
        assert switcher._resolve_account_identifier("") is None

    def test_resolve_by_alias(self, temp_home: Path, sample_sequence_data: dict):
        sample_sequence_data["accounts"]["2"]["alias"] = "dev"
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)

        assert switcher._resolve_account_identifier("dev") == "2"

    def test_resolve_by_alias_case_insensitive(self, temp_home: Path, sample_sequence_data: dict):
        sample_sequence_data["accounts"]["2"]["alias"] = "dev"
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)

        assert switcher._resolve_account_identifier("DEV") == "2"

    def test_number_takes_precedence_over_alias(self, temp_home: Path, sample_sequence_data: dict):
        # An alias that happens to be numeric-looking can't occur (validation
        # rejects it), but a plain numeric identifier must always resolve as
        # a slot number, never fall through to alias matching.
        sample_sequence_data["accounts"]["2"]["alias"] = "dev"
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)

        assert switcher._resolve_account_identifier("1") == "1"

    def test_alias_takes_precedence_over_unrelated_email(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        # Alias resolution happens before the email-match branch.
        sample_sequence_data["accounts"]["2"]["alias"] = "dev"
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)

        assert switcher._resolve_account_identifier("account1@example.com") == "1"
        assert switcher._resolve_account_identifier("dev") == "2"

    def test_resolve_account_public_wrapper_supports_alias(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """resolve_account (used by map/run) has no email-format gate, so it
        already supports aliases once _resolve_account_identifier does."""
        sample_sequence_data["accounts"]["2"]["alias"] = "dev"
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)

        num, email, org_uuid = switcher.resolve_account("dev")
        assert num == "2"
        assert email == "account2@example.com"


class TestAliasCommand:
    """Test ClaudeAccountSwitcher.set_alias()/unset_alias()/list_aliases()."""

    def _write(self, switcher, data):
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, data)

    def test_set_alias_by_number(self, temp_home: Path, sample_sequence_data: dict):
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)

        num, normalized = switcher.set_alias("2", "dev")

        assert num == "2"
        assert normalized == "dev"
        data = switcher._get_sequence_data()
        assert data["accounts"]["2"]["alias"] == "dev"

    def test_set_alias_normalizes_to_lowercase(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)

        _, normalized = switcher.set_alias("2", "DEV")

        assert normalized == "dev"
        data = switcher._get_sequence_data()
        assert data["accounts"]["2"]["alias"] == "dev"

    def test_set_alias_by_email(self, temp_home: Path, sample_sequence_data: dict):
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)

        num, _ = switcher.set_alias("account2@example.com", "dev")

        assert num == "2"
        data = switcher._get_sequence_data()
        assert data["accounts"]["2"]["alias"] == "dev"

    def test_rename_via_existing_alias_identifier(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """cswap alias <old> <new> — identifier can itself be an alias."""
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)
        switcher.set_alias("1", "dev")

        num, normalized = switcher.set_alias("dev", "prod")

        assert num == "1"
        assert normalized == "prod"
        assert switcher._resolve_account_identifier("prod") == "1"
        assert switcher._resolve_account_identifier("dev") is None

    def test_set_invalid_alias_raises(self, temp_home: Path, sample_sequence_data: dict):
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)

        with pytest.raises(ValidationError):
            switcher.set_alias("2", "123")

    def test_set_duplicate_alias_raises(self, temp_home: Path, sample_sequence_data: dict):
        sample_sequence_data["accounts"]["1"]["alias"] = "dev"
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)

        with pytest.raises(ConfigError):
            switcher.set_alias("2", "dev")

    def test_unset_alias(self, temp_home: Path, sample_sequence_data: dict):
        sample_sequence_data["accounts"]["2"]["alias"] = "dev"
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)

        num = switcher.unset_alias("2")

        assert num == "2"
        data = switcher._get_sequence_data()
        assert "alias" not in data["accounts"]["2"]

    def test_unset_alias_idempotent(self, temp_home: Path, sample_sequence_data: dict):
        """Clearing an already-unset alias must not raise."""
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)

        switcher.unset_alias("2")  # never set — should be a silent no-op
        switcher.unset_alias("2")

    def test_alias_unknown_account_raises(self, temp_home: Path, sample_sequence_data: dict):
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)

        with pytest.raises(AccountNotFoundError):
            switcher.set_alias("999", "dev")

    def test_list_aliases(self, temp_home: Path, sample_sequence_data: dict):
        sample_sequence_data["accounts"]["2"]["alias"] = "dev"
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)

        assert switcher.list_aliases() == [("2", "dev", "account2@example.com")]

    def test_list_aliases_sequence_order(self, temp_home: Path, sample_sequence_data: dict):
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)
        switcher.set_alias("2", "content")
        switcher.set_alias("1", "dev")

        assert switcher.list_aliases() == [
            ("1", "dev", "account1@example.com"),
            ("2", "content", "account2@example.com"),
        ]

    def test_list_aliases_empty(self, temp_home: Path, sample_sequence_data: dict):
        switcher = ClaudeAccountSwitcher()
        self._write(switcher, sample_sequence_data)

        assert switcher.list_aliases() == []


class TestDirectorySetup:
    """Test directory setup."""

    def test_creates_directories(self, temp_home: Path):
        """Test that setup creates required directories."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()

        assert switcher.backup_dir.exists()
        assert switcher.configs_dir.exists()
        assert switcher.credentials_dir.exists()

    @pytest.mark.skipif(sys.platform == "win32", reason="File permissions work differently on Windows")
    def test_directory_permissions(self, temp_home: Path):
        """Test that directories have correct permissions."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()

        for directory in [switcher.backup_dir, switcher.configs_dir, switcher.credentials_dir]:
            stat = directory.stat()
            assert stat.st_mode & 0o777 == 0o700


class TestAddAccountRefresh:
    """Test refreshing credentials for an existing account."""

    def test_readd_existing_account_updates_credentials(
        self, temp_home: Path, mock_claude_config: Path, capsys
    ):
        """Re-adding an existing account should update its credentials, not duplicate it."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._init_sequence_file()

        old_creds = json.dumps({"claudeAiOauth": {"accessToken": "old-token"}})
        new_creds = json.dumps({"claudeAiOauth": {"accessToken": "new-token"}})

        # Track what was written to credential storage
        stored = {}

        def mock_write_creds(num, email, creds):
            stored["creds"] = creds

        def mock_read_creds(num, email):
            return stored.get("creds", "")

        # First add
        with patch.object(switcher, "_read_active_credentials", return_value=ActiveCredentials(old_creds, False)), \
             patch.object(switcher, "_write_account_credentials", side_effect=mock_write_creds):
            switcher.add_account()

        # Verify first add
        data = switcher._get_sequence_data()
        assert len(data["accounts"]) == 1
        assert data["accounts"]["1"]["email"] == "test@example.com"
        assert "old-token" in stored["creds"]

        # Re-add same account with new credentials
        with patch.object(switcher, "_read_active_credentials", return_value=ActiveCredentials(new_creds, False)), \
             patch.object(switcher, "_write_account_credentials", side_effect=mock_write_creds):
            switcher.add_account()

        # Should still have only 1 account
        data = switcher._get_sequence_data()
        assert len(data["accounts"]) == 1
        assert len(data["sequence"]) == 1

        # Should have printed update message
        output = capsys.readouterr().out
        assert "Updated credentials" in output

        # Verify credentials were actually updated
        assert "new-token" in stored["creds"]


class TestGetNextAccountNumber:
    """Test getting next account number."""

    def test_first_account(self, temp_home: Path):
        """Test first account number is 1."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._init_sequence_file()

        assert switcher._get_next_account_number() == 1

    def test_with_existing_accounts(self, temp_home: Path, sample_sequence_data: dict):
        """Test next number after existing accounts."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        assert switcher._get_next_account_number() == 3


class TestStatus:
    """Test status command."""

    def test_status_no_account(self, temp_home: Path):
        """Test status when no account is logged in."""
        switcher = ClaudeAccountSwitcher()
        # Should not raise, just print
        switcher.status()

    def test_status_unmanaged_account(
        self, temp_home: Path, mock_claude_config: Path
    ):
        """Test status with unmanaged account."""
        switcher = ClaudeAccountSwitcher()
        switcher.status()

    def test_status_managed_account(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """Test status with managed account."""
        # Update sequence data to match mock config email
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        switcher.status()


class TestStatusCache:
    """status() shares the usage.json cache with list_accounts."""

    def test_status_uses_cached_usage(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict, capsys
    ):
        """A fresh store entry for the active account skips the API call."""
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        UsageStore(switcher.backup_dir / "cache").record(
            {"1": FetchRecord(usage={
                "five_hour": {"pct": 25, "clock": "Jan 1 03:00", "countdown": "1h"},
                "seven_day": {"pct": 60, "clock": "Jan 2 03:00", "countdown": "2d"},
            })},
            {"1": ("test@example.com", "")},
        )

        with patch.object(switcher, "_read_active_credentials",
                          return_value=ActiveCredentials(active_creds, False)), \
             patch("claude_swap.oauth.try_fetch_usage_for_account") as mock_fetch:
            switcher.status()

        mock_fetch.assert_not_called()
        output = capsys.readouterr().out
        assert "25%" in output
        assert "60%" in output

    def test_status_fetches_with_is_active_true_when_cc_running(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict, capsys
    ):
        """When Claude Code is running, fetch with is_active=True (never refresh live creds)."""
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        usage_result = {
            "five_hour": {"pct": 10, "clock": "Jan 1 03:00", "countdown": "0m"},
            "seven_day": {"pct": 50, "clock": "Jan 2 03:00", "countdown": "0m"},
        }

        with patch.object(switcher, "_read_active_credentials",
                          return_value=ActiveCredentials(active_creds, False)), \
             patch("claude_swap.oauth.try_fetch_usage_for_account",
                   return_value=oauth.UsageOutcome(usage_result)) as mock_fetch:
            switcher.status()

        mock_fetch.assert_called_once()
        assert mock_fetch.call_args.kwargs.get("is_active") is True

        output = capsys.readouterr().out
        assert "10%" in output

        entry = UsageStore(switcher.backup_dir / "cache").entries(
            {"1": ("test@example.com", "")}
        )["1"]
        assert entry.last_good == usage_result

    def test_status_preserves_other_accounts_in_cache(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """Fetching the active account merges into the store without clobbering others."""
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        # Store has only account "2"; status() runs for account "1"
        store = UsageStore(switcher.backup_dir / "cache")
        store.record(
            {"2": FetchRecord(usage={"five_hour": {"pct": 80}})},
            {"2": ("account2@example.com", "")},
        )

        usage_result = {"five_hour": {"pct": 10, "clock": "Jan 1 03:00", "countdown": "0m"}}

        with patch.object(switcher, "_read_active_credentials",
                          return_value=ActiveCredentials(active_creds, False)), \
             patch("claude_swap.oauth.try_fetch_usage_for_account",
                   return_value=oauth.UsageOutcome(usage_result)):
            switcher.status()

        entries = store.entries(
            {"1": ("test@example.com", ""), "2": ("account2@example.com", "")}
        )
        assert entries["1"].last_good == usage_result
        assert entries["2"].last_good == {"five_hour": {"pct": 80}}


def _oauth_creds(token: str, expires_in_s: float) -> str:
    """Credential JSON with an access token expiring ``expires_in_s`` from now."""
    return json.dumps({"claudeAiOauth": {
        "accessToken": token,
        "refreshToken": f"rt-{token}",
        "expiresAt": int((time.time() + expires_in_s) * 1000),
    }})


class TestFetchAccountUsageSessionProfile:
    """Inactive-account fetches source credentials from the session profile.

    Claude rotates the token family inside a session profile and nothing
    syncs it back, so the backup copy's refresh token is a consumed
    generation once a session has run — fetching with it 401s forever and
    usage silently freezes at the last pre-session measurement.
    """

    def _info(self, backup_creds: str) -> tuple:
        return (2, "test@example.com", "Org", "org-uuid", False, backup_creds, "")

    def test_fresh_session_credentials_fetch_read_only(self, temp_home: Path):
        """Profile creds are used with is_active=True (no refresh, no persist)."""
        switcher = ClaudeAccountSwitcher()
        backup = _oauth_creds("sk-backup", -3600)
        session = _oauth_creds("sk-session", 7200)

        with patch.object(switcher, "_live_session_pids", return_value=[123]), \
             patch("claude_swap.session.read_session_credentials",
                   return_value=session), \
             patch("claude_swap.oauth.try_fetch_usage_for_account",
                   return_value=oauth.UsageOutcome({"five_hour": {"pct": 5}})) as mock_fetch:
            record = switcher._fetch_account_usage(self._info(backup))

        assert record.usage == {"five_hour": {"pct": 5}}
        mock_fetch.assert_called_once()
        args, kwargs = mock_fetch.call_args
        assert args[2] == session
        assert kwargs.get("is_active") is True
        assert "persist_credentials" not in kwargs

    def test_expired_session_credentials_with_live_session_is_sentinel(
        self, temp_home: Path
    ):
        """Live claude refreshes lazily — don't burn a request that would 401."""
        switcher = ClaudeAccountSwitcher()
        backup = _oauth_creds("sk-backup", -3600)
        session = _oauth_creds("sk-session", -60)

        with patch.object(switcher, "_live_session_pids", return_value=[123]), \
             patch("claude_swap.session.read_session_credentials",
                   return_value=session), \
             patch("claude_swap.oauth.try_fetch_usage_for_account") as mock_fetch:
            record = switcher._fetch_account_usage(self._info(backup))

        assert record.sentinel == USAGE_TOKEN_EXPIRED
        mock_fetch.assert_not_called()

    def test_expired_session_credentials_without_live_session_falls_back(
        self, temp_home: Path
    ):
        """No live session, and the backup is the FRESHER credential (a
        re-login after the profile last ran): the backup path runs, refresh
        machinery included, and the older profile is not adopted over it."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        backup = _oauth_creds("sk-backup", 7200)
        session = _oauth_creds("sk-session", -60)
        switcher._write_account_credentials("2", "test@example.com", backup)

        with patch.object(switcher, "_live_session_pids", return_value=[]), \
             patch("claude_swap.session.read_session_credentials",
                   return_value=session), \
             patch("claude_swap.oauth.try_fetch_usage_for_account",
                   return_value=oauth.UsageOutcome({"five_hour": {"pct": 9}})) as mock_fetch:
            record = switcher._fetch_account_usage(self._info(backup))

        assert record.usage == {"five_hour": {"pct": 9}}
        args, kwargs = mock_fetch.call_args
        assert args[2] == backup
        assert kwargs.get("is_active") is False
        assert kwargs.get("refresh_via") is not None  # consume gate replaces persist
        assert switcher.read_account_credentials("2", "test@example.com") == backup

    def test_exited_session_ahead_of_backup_is_adopted(self, temp_home: Path):
        """No live session and the profile is the newer generation: its
        credential becomes the backup, and the fetch runs on it through the
        idle path (refresh allowed — nothing else rotates that family now)
        instead of POSTing the backup's dead grant."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        backup = _oauth_creds("sk-backup", -3600)
        session = _oauth_creds("sk-session", 7200)
        switcher._write_account_credentials("2", "test@example.com", backup)

        with patch.object(switcher, "_live_session_pids", return_value=[]), \
             patch("claude_swap.session.read_session_credentials",
                   return_value=session), \
             patch("claude_swap.oauth.try_fetch_usage_for_account",
                   return_value=oauth.UsageOutcome({"five_hour": {"pct": 9}})) as mock_fetch:
            record = switcher._fetch_account_usage(self._info(backup))

        assert record.usage == {"five_hour": {"pct": 9}}
        args, kwargs = mock_fetch.call_args
        assert args[2] == session
        assert kwargs.get("is_active") is False
        assert kwargs.get("refresh_via") is not None
        assert switcher.read_account_credentials("2", "test@example.com") == session

    def test_live_session_ahead_of_backup_is_not_adopted(self, temp_home: Path):
        """A live claude owns its profile's family: read-only fetch, backup untouched."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        backup = _oauth_creds("sk-backup", -3600)
        session = _oauth_creds("sk-session", 7200)
        switcher._write_account_credentials("2", "test@example.com", backup)

        with patch.object(switcher, "_live_session_pids", return_value=[123]), \
             patch("claude_swap.session.read_session_credentials",
                   return_value=session), \
             patch("claude_swap.oauth.try_fetch_usage_for_account",
                   return_value=oauth.UsageOutcome({"five_hour": {"pct": 5}})) as mock_fetch:
            switcher._fetch_account_usage(self._info(backup))

        args, kwargs = mock_fetch.call_args
        assert args[2] == session
        assert kwargs.get("is_active") is True
        assert switcher.read_account_credentials("2", "test@example.com") == backup

    def test_no_session_profile_uses_backup_path(self, temp_home: Path):
        """Accounts without a session profile behave exactly as before."""
        switcher = ClaudeAccountSwitcher()
        backup = _oauth_creds("sk-backup", 7200)

        with patch.object(switcher, "_live_session_pids", return_value=[]), \
             patch("claude_swap.session.read_session_credentials",
                   return_value=None), \
             patch("claude_swap.oauth.try_fetch_usage_for_account",
                   return_value=oauth.UsageOutcome({"five_hour": {"pct": 9}})) as mock_fetch:
            record = switcher._fetch_account_usage(self._info(backup))

        assert record.usage == {"five_hour": {"pct": 9}}
        args, kwargs = mock_fetch.call_args
        assert args[2] == backup
        assert kwargs.get("is_active") is False
        assert kwargs.get("refresh_via") is not None  # consume gate replaces persist

    def _write_profile_identity(self, switcher, email: str, org_uuid) -> None:
        session_dir = switcher._session_dir("2", "test@example.com")
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": email, "organizationUuid": org_uuid}
        }))

    def test_drifted_profile_email_falls_back_to_backup(self, temp_home: Path):
        """An in-session /login as another account must not feed that account's
        usage into this slot: the fetch ignores the drifted profile credential
        and uses the backup — refreshable, since the live session no longer
        holds this slot's token family."""
        switcher = ClaudeAccountSwitcher()
        backup = _oauth_creds("sk-backup", 7200)
        session = _oauth_creds("sk-session", 7200)
        self._write_profile_identity(switcher, "other@example.com", "org-other")

        with patch.object(switcher, "_live_session_pids", return_value=[123]), \
             patch("claude_swap.session.read_session_credentials",
                   return_value=session), \
             patch("claude_swap.oauth.try_fetch_usage_for_account",
                   return_value=oauth.UsageOutcome({"five_hour": {"pct": 9}})) as mock_fetch:
            record = switcher._fetch_account_usage(self._info(backup))

        assert record.usage == {"five_hour": {"pct": 9}}
        args, kwargs = mock_fetch.call_args
        assert args[2] == backup
        assert kwargs.get("is_active") is False
        assert kwargs.get("refresh_via") is not None  # consume gate replaces persist

    def test_drifted_profile_org_same_email_falls_back(self, temp_home: Path):
        """Same email, different org (the j@ck.gg merge-artifact shape) is a
        different subscription — still drift."""
        switcher = ClaudeAccountSwitcher()
        backup = _oauth_creds("sk-backup", 7200)
        session = _oauth_creds("sk-session", 7200)
        self._write_profile_identity(switcher, "test@example.com", "org-uuid-other")

        with patch.object(switcher, "_live_session_pids", return_value=[123]), \
             patch("claude_swap.session.read_session_credentials",
                   return_value=session), \
             patch("claude_swap.oauth.try_fetch_usage_for_account",
                   return_value=oauth.UsageOutcome({"five_hour": {"pct": 9}})) as mock_fetch:
            switcher._fetch_account_usage(self._info(backup))

        args, _kwargs = mock_fetch.call_args
        assert args[2] == backup

    def test_matching_profile_identity_uses_session_credentials(self, temp_home: Path):
        """The guard must not regress the normal case: matching identity keeps
        the profile-credential fast path (read-only)."""
        switcher = ClaudeAccountSwitcher()
        backup = _oauth_creds("sk-backup", -3600)
        session = _oauth_creds("sk-session", 7200)
        self._write_profile_identity(switcher, "test@example.com", "org-uuid")

        with patch.object(switcher, "_live_session_pids", return_value=[123]), \
             patch("claude_swap.session.read_session_credentials",
                   return_value=session), \
             patch("claude_swap.oauth.try_fetch_usage_for_account",
                   return_value=oauth.UsageOutcome({"five_hour": {"pct": 5}})) as mock_fetch:
            record = switcher._fetch_account_usage(self._info(backup))

        assert record.usage == {"five_hour": {"pct": 5}}
        args, kwargs = mock_fetch.call_args
        assert args[2] == session
        assert kwargs.get("is_active") is True

    def test_unreadable_profile_identity_trusts_session_credentials(
        self, temp_home: Path
    ):
        """A profile dir without a readable .claude.json is not treated as
        drift — the profile's token family stays the credential truth."""
        switcher = ClaudeAccountSwitcher()
        backup = _oauth_creds("sk-backup", -3600)
        session = _oauth_creds("sk-session", 7200)
        switcher._session_dir("2", "test@example.com").mkdir(parents=True)

        with patch.object(switcher, "_live_session_pids", return_value=[123]), \
             patch("claude_swap.session.read_session_credentials",
                   return_value=session), \
             patch("claude_swap.oauth.try_fetch_usage_for_account",
                   return_value=oauth.UsageOutcome({"five_hour": {"pct": 5}})) as mock_fetch:
            record = switcher._fetch_account_usage(self._info(backup))

        assert record.usage == {"five_hour": {"pct": 5}}
        args, kwargs = mock_fetch.call_args
        assert args[2] == session
        assert kwargs.get("is_active") is True


class TestAdoptSessionCredential:
    """An exited session's profile holds the slot's newest generation; the
    backup only learns about it through adoption."""

    EMAIL = "test@example.com"

    def _switcher(self, backup: str) -> ClaudeAccountSwitcher:
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_account_credentials("2", self.EMAIL, backup)
        return switcher

    def _seed_profile(
        self, switcher, creds: str, email: str = EMAIL, org: str = "org-uuid"
    ) -> Path:
        session_dir = switcher._session_dir("2", self.EMAIL)
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / ".credentials.json").write_text(creds)
        (session_dir / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": email, "organizationUuid": org}
        }))
        return session_dir

    def test_quiescent_profile_ahead_is_adopted(self, temp_home: Path):
        backup = _oauth_creds("sk-backup", -3600)
        profile = _oauth_creds("sk-session", 7200)
        switcher = self._switcher(backup)
        session_dir = self._seed_profile(switcher, profile)

        assert switcher._adopt_session_credential("2", self.EMAIL, "org-uuid") is True
        assert switcher.read_account_credentials("2", self.EMAIL) == profile
        # The profile is the source of that generation, not a stale seed.
        assert (session_dir / ".credentials.json").read_text() == profile

    def test_live_profile_is_not_adopted(self, temp_home: Path):
        backup = _oauth_creds("sk-backup", -3600)
        profile = _oauth_creds("sk-session", 7200)
        switcher = self._switcher(backup)
        session_dir = self._seed_profile(switcher, profile)
        records = session_dir / "sessions"
        records.mkdir()
        (records / f"{os.getpid()}.json").write_text(json.dumps({"pid": os.getpid()}))

        assert switcher._adopt_session_credential("2", self.EMAIL, "org-uuid") is False
        assert switcher.read_account_credentials("2", self.EMAIL) == backup

    def test_stale_marked_profile_is_not_adopted(self, temp_home: Path):
        """The backup moved under this profile while it was live; cswap has
        already decided the profile re-bootstraps, and the marker stays."""
        backup = _oauth_creds("sk-backup", -3600)
        profile = _oauth_creds("sk-session", 7200)
        switcher = self._switcher(backup)
        session_dir = self._seed_profile(switcher, profile)
        mark_session_stale(session_dir)

        assert switcher._adopt_session_credential("2", self.EMAIL, "org-uuid") is False
        assert switcher.read_account_credentials("2", self.EMAIL) == backup

    def test_profile_behind_a_fresh_relogin_is_not_adopted(self, temp_home: Path):
        backup = _oauth_creds("sk-backup", 7200)
        profile = _oauth_creds("sk-session", -60)
        switcher = self._switcher(backup)
        self._seed_profile(switcher, profile)

        assert switcher._adopt_session_credential("2", self.EMAIL, "org-uuid") is False
        assert switcher.read_account_credentials("2", self.EMAIL) == backup

    def test_profile_on_the_backup_generation_is_not_adopted(self, temp_home: Path):
        creds = _oauth_creds("sk-same", 7200)
        switcher = self._switcher(creds)
        self._seed_profile(switcher, creds)

        assert switcher._adopt_session_credential("2", self.EMAIL, "org-uuid") is False

    def test_profile_logged_in_as_another_account_is_not_adopted(
        self, temp_home: Path
    ):
        backup = _oauth_creds("sk-backup", -3600)
        profile = _oauth_creds("sk-session", 7200)
        switcher = self._switcher(backup)
        self._seed_profile(switcher, profile, email="other@example.com")

        assert switcher._adopt_session_credential("2", self.EMAIL, "org-uuid") is False
        assert switcher.read_account_credentials("2", self.EMAIL) == backup


class TestLiveSessionGuardOnAnUnreadableRecord:
    """A session record we could not READ must not answer "nobody there".

    ``list_sessions`` skips an unparseable record, which is right for a SCAN:
    one bad file must not kill a listing. ``_ensure_no_live_session`` is a
    GUARD on the same data, and it gates destruction — ``_bootstrap`` deletes
    the profile's Keychain entry and overwrites ``.credentials.json``, and
    ``remove_account`` deletes the slot. A shorter list reads there as "no
    live claude", so one unreadable record opens that gate underneath a
    running instance.

    Every other test of this guard patches ``_live_session_pids``, so none of
    them exercises the read that produces the collapse.
    """

    def _sessions_dir(self, switcher) -> Path:
        d = switcher._session_dir("2", "test@example.com") / "sessions"
        d.mkdir(parents=True)
        return d

    def test_unreadable_record_refuses_like_a_live_one(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()
        d = self._sessions_dir(switcher)
        (d / "9999.json").write_text("not json{{{", encoding="utf-8")

        with pytest.raises(SessionError, match="could not be read"):
            switcher._ensure_no_live_session("2", "test@example.com", "the operation")

    def test_readable_and_empty_still_permits(self, temp_home: Path):
        """The control. Without it the test above passes on a guard that
        refuses unconditionally, which is not the contract."""
        switcher = ClaudeAccountSwitcher()
        self._sessions_dir(switcher)

        switcher._ensure_no_live_session("2", "test@example.com", "the operation")


class TestListAccountsUsage:
    """Test list_accounts shows usage info."""

    def test_list_shows_usage(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict, capsys
    ):
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})
        backup_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-backup"}})

        usage_response = {
            "five_hour": {"utilization": 10.0, "resets_at": "2026-01-01T00:00:00Z"},
            "seven_day": {"utilization": 50.0, "resets_at": "2026-01-02T00:00:00Z"},
        }
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(usage_response).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        with patch.object(switcher, "_read_credentials", return_value=active_creds), \
             patch.object(switcher, "_read_account_credentials", return_value=backup_creds), \
             patch("claude_swap.oauth.urllib.request.urlopen", return_value=mock_response):
            switcher.list_accounts()

        output = capsys.readouterr().out
        assert "test@example.com [personal] (active)" in output
        assert "account2@example.com" in output
        assert "├ 5h:" in output
        assert "└ 7d:" in output
        assert "10%" in output
        assert "50%" in output

    def test_list_shows_alias_before_email(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict, capsys
    ):
        """An aliased account renders 'alias (email)', leading with the alias
        — the whole point is not having to read full addresses to tell
        similar-looking accounts apart."""
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        sample_sequence_data["accounts"]["1"]["alias"] = "dev"
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})
        backup_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-backup"}})

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        with patch.object(switcher, "_read_credentials", return_value=active_creds), \
             patch.object(switcher, "_read_account_credentials", return_value=backup_creds), \
             patch("claude_swap.oauth.try_fetch_usage_for_account", return_value=oauth.UsageOutcome(None)):
            switcher.list_accounts()

        output = capsys.readouterr().out
        assert "  1: dev (test@example.com) [personal] (active)" in output
        # unaliased account keeps rendering plain email, no spurious parens
        assert "  2: account2@example.com" in output
        assert "(account2@example.com)" not in output

    def test_list_shows_usage_null_reset(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict, capsys
    ):
        """When five_hour.resets_at is null and seven_day is at 100%, display both correctly."""
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})
        backup_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-backup"}})

        usage_response = {
            "five_hour": {"utilization": 0.0, "resets_at": None},
            "seven_day": {"utilization": 100.0, "resets_at": "2026-04-03T02:59:59Z"},
        }
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(usage_response).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        with patch.object(switcher, "_read_credentials", return_value=active_creds), \
             patch.object(switcher, "_read_account_credentials", return_value=backup_creds), \
             patch("claude_swap.oauth.urllib.request.urlopen", return_value=mock_response):
            switcher.list_accounts()

        output = capsys.readouterr().out
        assert "5h:   0%" in output
        assert "7d: 100%" in output
        assert "usage unavailable" not in output

    def test_list_no_credentials(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict, capsys
    ):
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        with patch.object(switcher, "_read_credentials", return_value=""), \
             patch.object(switcher, "_read_account_credentials", return_value=""):
            switcher.list_accounts()

        output = capsys.readouterr().out
        assert "no credentials" in output

    def test_list_never_writes_live_while_claude_code_running(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """While Claude Code owns the active account, list never writes live creds.

        Refreshing the live credential in parallel would race with Claude Code's own
        refresh (which coordinates via a ~/.claude/ lockfile cswap doesn't honor) and
        could trip refresh-token reuse detection. The active row stays hands-off
        (is_active=True) whenever an owner is detected; only inactive backups refresh.
        """
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})
        backup_creds = json.dumps({
            "claudeAiOauth": {"accessToken": "sk-backup", "refreshToken": "rt-orig"},
        })
        refreshed_creds = json.dumps({
            "claudeAiOauth": {"accessToken": "sk-new", "refreshToken": "rt-new"},
        })

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        def mock_fetch(account_num, email, credentials, is_active,
                       persist_credentials=None, refresh_via=None, **kwargs):
            # Simulate a refresh on the inactive account only — through the
            # consume gate (the persist seam was replaced by refresh_via).
            if not is_active and refresh_via is not None:
                refresh_via(account_num, email, credentials)
            return oauth.UsageOutcome(None)

        def mock_gate(account_num, email, snapshot):
            switcher._write_account_credentials(
                account_num, email, refreshed_creds
            )
            return oauth.RefreshOutcome(refreshed_creds, None)

        with patch.object(switcher, "_read_credentials", return_value=active_creds), \
             patch.object(switcher, "_read_account_credentials", return_value=backup_creds), \
             patch.object(switcher, "_write_credentials") as write_live, \
             patch.object(switcher, "_write_account_credentials") as write_backup, \
             patch.object(switcher, "consume_backup_grant", side_effect=mock_gate), \
             patch("claude_swap.oauth.try_fetch_usage_for_account", side_effect=mock_fetch):
            switcher.list_accounts()

        # Live creds must never be written while Claude Code is running.
        write_live.assert_not_called()
        # Backup was written for the inactive account (2) only.
        write_backup.assert_called_once_with("2", "account2@example.com", refreshed_creds)

    def test_list_shows_token_status_when_requested(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict, capsys
    ):
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})
        backup_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-backup"}})

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        with patch.object(switcher, "_read_credentials", return_value=active_creds), \
             patch.object(switcher, "_read_account_credentials", return_value=backup_creds), \
             patch("claude_swap.oauth.try_fetch_usage_for_account", return_value=oauth.UsageOutcome(None)), \
             patch("claude_swap.session.read_session_credentials", return_value=None), \
             patch("claude_swap.oauth.build_token_status", return_value="oauth: fresh, refresh token yes"):
            switcher.list_accounts(show_token_status=True)

        output = capsys.readouterr().out
        assert "active profile: fresh, refresh token yes" in output
        assert "stored backup: fresh, refresh token yes" in output

    def test_token_status_lines_for_active_account_use_active_profile(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()

        with patch("claude_swap.oauth.build_token_status", return_value="oauth: fresh, refresh token yes") as build:
            lines = switcher._token_status_lines(
                (1, "active@example.com", "", "", True, "active-creds", "")
            )

        assert lines == ["active profile: fresh, refresh token yes"]
        build.assert_called_once_with("active-creds")

    def test_token_status_lines_preserve_api_key_silence(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()

        with patch("claude_swap.session.read_session_credentials") as read_session, \
             patch("claude_swap.oauth.build_token_status") as build:
            lines = switcher._token_status_lines(
                (2, "key@example.com", "", "", False, "sk-ant-api03-test", "")
            )

        assert lines == []
        read_session.assert_not_called()
        build.assert_not_called()

    def test_token_status_lines_prefer_matching_session_profile_then_backup(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()

        def build_status(credentials: str) -> str | None:
            return {
                "session-creds": "oauth: fresh, refresh token yes",
                "backup-creds": "oauth: expired, refresh token yes",
            }.get(credentials)

        with patch("claude_swap.session.read_session_credentials", return_value="session-creds") as read_session, \
             patch("claude_swap.session.session_identity_drifted", return_value=False) as drifted, \
             patch("claude_swap.oauth.build_token_status", side_effect=build_status) as build:
            lines = switcher._token_status_lines(
                (2, "inactive@example.com", "", "org-2", False, "backup-creds", "")
            )

        assert lines == [
            "session profile: fresh, refresh token yes",
            "stored backup: expired, refresh token yes",
        ]
        read_session.assert_called_once()
        drifted.assert_called_once()
        assert build.call_args_list == [call("session-creds"), call("backup-creds")]

    def test_token_status_lines_ignore_drifted_session_profile(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()

        with patch("claude_swap.session.read_session_credentials", return_value="session-creds") as read_session, \
             patch("claude_swap.session.session_identity_drifted", return_value=True) as drifted, \
             patch(
                 "claude_swap.oauth.build_token_status",
                 return_value="oauth: expired, refresh token yes",
             ) as build:
            lines = switcher._token_status_lines(
                (2, "inactive@example.com", "", "org-2", False, "backup-creds", "")
            )

        assert lines == [
            "session profile: ignored (different account)",
            "stored backup: expired, refresh token yes",
        ]
        read_session.assert_called_once()
        drifted.assert_called_once()
        build.assert_called_once_with("backup-creds")

    def test_token_status_lines_without_session_show_only_backup(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()

        with patch("claude_swap.session.read_session_credentials", return_value=None) as read_session, \
             patch("claude_swap.session.session_identity_drifted") as drifted, \
             patch(
                 "claude_swap.oauth.build_token_status",
                 return_value="oauth: fresh, refresh token yes",
             ) as build:
            lines = switcher._token_status_lines(
                (2, "inactive@example.com", "", "org-2", False, "backup-creds", "")
            )

        assert lines == ["stored backup: fresh, refresh token yes"]
        read_session.assert_called_once()
        drifted.assert_not_called()
        build.assert_called_once_with("backup-creds")

    def test_token_status_lines_are_read_only(self, temp_home: Path):
        switcher = ClaudeAccountSwitcher()

        with patch("claude_swap.session.read_session_credentials", return_value="session-creds"), \
             patch("claude_swap.session.session_identity_drifted", return_value=False), \
             patch(
                 "claude_swap.oauth.build_token_status",
                 side_effect=["oauth: fresh, refresh token yes", "oauth: expired, refresh token yes"],
             ), \
             patch("claude_swap.oauth.try_refresh_oauth_credentials") as refresh, \
             patch("claude_swap.oauth.try_fetch_usage_for_account") as fetch, \
             patch.object(switcher, "_write_credentials") as write_live, \
             patch.object(switcher, "_write_account_credentials") as write_backup:
            switcher._token_status_lines(
                (2, "inactive@example.com", "", "org-2", False, "backup-creds", "")
            )

        refresh.assert_not_called()
        fetch.assert_not_called()
        write_live.assert_not_called()
        write_backup.assert_not_called()

    def test_list_uses_cached_usage(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict, capsys
    ):
        """When fresh store entries exist, list_accounts skips API calls."""
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})
        backup_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-backup"}})

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        # Pre-populate the store with fresh usage data for both accounts
        UsageStore(switcher.backup_dir / "cache").record(
            {
                "1": FetchRecord(usage={
                    "five_hour": {"pct": 25, "clock": "Jan 1 03:00", "countdown": "1h"},
                    "seven_day": {"pct": 60, "clock": "Jan 2 03:00", "countdown": "2d"},
                }),
                "2": FetchRecord(usage={
                    "five_hour": {"pct": 80, "clock": "Jan 1 04:00", "countdown": "30m"},
                    "seven_day": {"pct": 90, "clock": "Jan 3 03:00", "countdown": "3d"},
                }),
            },
            {"1": ("test@example.com", ""), "2": ("account2@example.com", "")},
        )

        with patch.object(switcher, "_read_active_credentials",
                          return_value=ActiveCredentials(active_creds, False)), \
             patch.object(switcher, "_read_account_credentials", return_value=backup_creds), \
             patch("claude_swap.oauth.try_fetch_usage_for_account") as mock_fetch:
            switcher.list_accounts()

        # API should NOT have been called — data came from the store
        mock_fetch.assert_not_called()
        output = capsys.readouterr().out
        assert "25%" in output
        assert "80%" in output

    def test_list_refetches_stale_entries(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict, capsys
    ):
        """Entries older than the serve TTL are refetched, not served."""
        import time as time_mod

        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})
        backup_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-backup"}})

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        # Store has a 400s-old entry for account "1" (past SERVE_TTL_S) and
        # nothing for "2" — both must be fetched live.
        backdated = UsageStore(
            switcher.backup_dir / "cache", clock=lambda: time_mod.time() - 400
        )
        backdated.record(
            {"1": FetchRecord(usage={"five_hour": {"pct": 25}})},
            {"1": ("test@example.com", "")},
        )

        usage_result = {
            "five_hour": {"pct": 10, "clock": "Jan 1 03:00", "countdown": "0m"},
            "seven_day": {"pct": 50, "clock": "Jan 2 03:00", "countdown": "0m"},
        }

        with patch.object(switcher, "_read_active_credentials",
                          return_value=ActiveCredentials(active_creds, False)), \
             patch.object(switcher, "_read_account_credentials", return_value=backup_creds), \
             patch("claude_swap.oauth.try_fetch_usage_for_account",
                   return_value=oauth.UsageOutcome(usage_result)) as mock_fetch:
            switcher.list_accounts()

        assert mock_fetch.call_count == 2
        output = capsys.readouterr().out
        # Should show live data (10%), not the stale 25%
        assert "10%" in output
        assert "25%" not in output

    def test_on_demand_pass_persists_poll_plans(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict, capsys
    ):
        """Every collector — not just auto — writes the adapted cadence, so
        all surfaces inherit one plan."""
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})
        backup_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-backup"}})

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        usage_result = {
            "five_hour": {"pct": 10, "clock": "Jan 1 03:00", "countdown": "0m"},
            "seven_day": {"pct": 50, "clock": "Jan 2 03:00", "countdown": "0m"},
        }
        with patch.object(switcher, "_read_active_credentials",
                          return_value=ActiveCredentials(active_creds, False)), \
             patch.object(switcher, "_read_account_credentials", return_value=backup_creds), \
             patch("claude_swap.oauth.try_fetch_usage_for_account",
                   return_value=oauth.UsageOutcome(usage_result)):
            switcher.list_accounts()
        capsys.readouterr()

        entries = switcher._usage_store.entries(
            {"1": ("test@example.com", ""), "2": ("account2@example.com", "")}
        )
        for num in ("1", "2"):
            assert entries[num].next_poll_at is not None
            assert entries[num].poll_interval_s is not None

    def test_on_demand_pass_respects_poll_plans(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict, capsys
    ):
        """A stale entry whose ``nextPollAt`` is in the future is served, not
        refetched — on-demand callers cannot out-poll the planned cadence."""
        import time as time_mod

        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})
        backup_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-backup"}})

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        ident1 = {"1": ("test@example.com", "")}
        backdated = UsageStore(
            switcher.backup_dir / "cache", clock=lambda: time_mod.time() - 400
        )
        backdated.record(
            {"1": FetchRecord(usage={"five_hour": {"pct": 25}})}, ident1
        )
        switcher._usage_store.set_poll_plan(
            {"1": (time_mod.time() + 600.0, 600.0)}, ident1
        )

        usage_result = {
            "five_hour": {"pct": 10, "clock": "Jan 1 03:00", "countdown": "0m"},
            "seven_day": {"pct": 50, "clock": "Jan 2 03:00", "countdown": "0m"},
        }
        with patch.object(switcher, "_read_active_credentials",
                          return_value=ActiveCredentials(active_creds, False)), \
             patch.object(switcher, "_read_account_credentials", return_value=backup_creds), \
             patch("claude_swap.oauth.try_fetch_usage_for_account",
                   return_value=oauth.UsageOutcome(usage_result)) as mock_fetch:
            switcher.list_accounts()

        # Only account "2" (no stored row, no plan) was fetch-eligible.
        assert mock_fetch.call_count == 1
        output = capsys.readouterr().out
        assert "25%" in output  # account 1 served from the store

    def test_on_demand_pass_repairs_reset_parked_exhausted_plan(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict, capsys
    ):
        """An exhausted plan from an older release must not suppress polling
        until a distant reset or let decision-grade status age unavailable."""
        import time as time_mod

        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})
        backup_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-backup"}})

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)
        ident1 = {"1": ("test@example.com", "")}
        backdated = UsageStore(
            switcher.backup_dir / "cache", clock=lambda: time_mod.time() - 400
        )
        exhausted = {
            "five_hour": {"pct": 25},
            "seven_day": {"pct": 100, "resets_at": "2099-01-01T00:00:00Z"},
        }
        backdated.record({"1": FetchRecord(usage=exhausted)}, ident1)
        switcher._usage_store.set_poll_plan(
            {"1": (time_mod.time() + 86_400.0, 300.0)}, ident1
        )

        refreshed = {
            "five_hour": {"pct": 5},
            "seven_day": {"pct": 10},
        }
        with patch.object(
            switcher,
            "_read_active_credentials",
            return_value=ActiveCredentials(active_creds, False),
        ), patch.object(
            switcher, "_read_account_credentials", return_value=backup_creds
        ), patch(
            "claude_swap.oauth.try_fetch_usage_for_account",
            return_value=oauth.UsageOutcome(refreshed),
        ) as mock_fetch:
            switcher.list_accounts()

        assert mock_fetch.call_count == 2  # repaired account 1 plus empty account 2
        output = capsys.readouterr().out
        assert "10%" in output
        entry = switcher._usage_store.entries(ident1)["1"]
        assert entry.next_poll_at is not None
        assert entry.next_poll_at < time_mod.time() + 86_400.0

    def test_replan_new_active_pulls_candidate_plan_to_floor(
        self, temp_home: Path, mock_claude_config: Path
    ):
        """A plan learned while the account idled as a candidate (up to 600s)
        must not gate it once it becomes active."""
        import time as time_mod

        from claude_swap import poll_policy

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        ident = {"1": ("a@x.com", "")}
        store = switcher._usage_store
        store.record({"1": FetchRecord(usage={"five_hour": {"pct": 10}})}, ident)
        store.set_poll_plan({"1": (time_mod.time() + 600.0, 600.0)}, ident)

        switcher._replan_new_active("1", "a@x.com", "")
        entry = store.entries(ident)["1"]
        assert entry.poll_interval_s == poll_policy.MIN_INTERVAL_S
        assert entry.next_poll_at <= time_mod.time() + poll_policy.MIN_INTERVAL_S + 1

        # An already-eager plan (urgent cadence) is never pushed later.
        store.set_poll_plan({"1": (time_mod.time() + 60.0, 60.0)}, ident)
        switcher._replan_new_active("1", "a@x.com", "")
        entry = store.entries(ident)["1"]
        assert entry.poll_interval_s == 60.0

        # A never-measured account gets no plan — a plan without a
        # measurement would block on-demand callers from the first fetch.
        ident2 = {"2": ("b@x.com", "")}
        switcher._replan_new_active("2", "b@x.com", "")
        assert store.entries(ident2)["2"].next_poll_at is None

        # An already-old measurement comes due immediately, not 180s from now.
        old_store = UsageStore(
            switcher.backup_dir / "cache", clock=lambda: time_mod.time() - 400
        )
        old_store.record(
            {"2": FetchRecord(usage={"five_hour": {"pct": 10}})}, ident2
        )
        store.set_poll_plan({"2": (time_mod.time() + 600.0, 600.0)}, ident2)
        switcher._replan_new_active("2", "b@x.com", "")
        entry = store.entries(ident2)["2"]
        assert entry.next_poll_at <= time_mod.time() + 1

    def test_replan_new_active_failure_is_logged_not_raised(
        self, temp_home: Path, mock_claude_config: Path, caplog
    ):
        """The switch this rides on has already committed — a cache failure
        here must never surface as a switch failure."""
        import logging

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        ident = {"1": ("a@x.com", "")}
        switcher._usage_store.record(
            {"1": FetchRecord(usage={"five_hour": {"pct": 10}})}, ident
        )
        with patch.object(
            switcher._usage_store, "set_poll_plan", side_effect=OSError("disk full")
        ), caplog.at_level(logging.WARNING, logger="claude-swap"):
            switcher._replan_new_active("1", "a@x.com", "")
        assert any(
            "switch itself succeeded" in r.getMessage() for r in caplog.records
        )

    def test_list_fetch_set_restricts_fetches(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict, capsys
    ):
        """``fetch`` caps which accounts may be fetched (the TUI watch view's
        adaptive set); the default ``None`` keeps every stale account eligible
        (covered by test_list_refetches_stale_entries)."""
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        active_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-active"}})
        backup_creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-backup"}})

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        usage_result = {
            "five_hour": {"pct": 10, "clock": "Jan 1 03:00", "countdown": "0m"},
            "seven_day": {"pct": 50, "clock": "Jan 2 03:00", "countdown": "0m"},
        }

        with patch.object(switcher, "_read_active_credentials",
                          return_value=ActiveCredentials(active_creds, False)), \
             patch.object(switcher, "_read_account_credentials", return_value=backup_creds), \
             patch("claude_swap.oauth.try_fetch_usage_for_account",
                   return_value=oauth.UsageOutcome(usage_result)) as mock_fetch:
            switcher.list_accounts(fetch=set())
        # Both accounts are stale (nothing stored) yet nobody may be fetched.
        mock_fetch.assert_not_called()

        with patch.object(switcher, "_read_active_credentials",
                          return_value=ActiveCredentials(active_creds, False)), \
             patch.object(switcher, "_read_account_credentials", return_value=backup_creds), \
             patch("claude_swap.oauth.try_fetch_usage_for_account",
                   return_value=oauth.UsageOutcome(usage_result)) as mock_fetch:
            switcher.list_accounts(fetch={"2"})
        # Only the allowed slot is fetched.
        assert mock_fetch.call_count == 1
        assert mock_fetch.call_args.args[0] == "2"


class TestUsageFetchStamps:
    def test_stamps_reflect_store_without_fetching(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        assert switcher.usage_fetch_stamps() == {"1": None, "2": None}

        UsageStore(switcher.backup_dir / "cache").record(
            {"1": FetchRecord(usage={"five_hour": {"pct": 25}})},
            {"1": ("account1@example.com", "")},
        )
        stamps = switcher.usage_fetch_stamps()
        assert stamps["1"] is not None
        assert stamps["2"] is None


class TestActiveAccountRefresh:
    """`_fetch_active_usage`: an expired active token is refreshed under
    Claude Code's own lock protocol — owner or no owner. CC 2.1.218 adopts an
    externally rotated credential (race-resolved re-read, 401 store recovery),
    so the locks make the rotation safe; what is never safe is discarding a
    consumed generation, so a successful grant persists unconditionally."""

    # Active credential with an already-expired access token (expiresAt in 1970).
    _EXPIRED = json.dumps({
        "claudeAiOauth": {
            "accessToken": "sk-active",
            "refreshToken": "rt-orig",
            "expiresAt": 1000,
        }
    })
    _REFRESHED = json.dumps({
        "claudeAiOauth": {
            "accessToken": "sk-new",
            "refreshToken": "rt-new",
            "expiresAt": 9999999999000,
        }
    })

    # What the profile oracle resolves for slot 1's own credential
    # (sequence uuid "uuid-1", no org).
    _PROFILE_SELF = {
        "uuid": "uuid-1", "email": "test@example.com", "organizationUuid": None
    }

    def _switcher(self, sample_sequence_data):
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)
        return switcher

    def _refresh_ok(self, credentials, **kw):
        return oauth.RefreshOutcome(self._REFRESHED, None)

    @pytest.fixture(autouse=True)
    def _no_profile_probe(self):
        """The oracle-checked resync probes ``fetch_oauth_profile`` on any
        backup-lineage mismatch — unpatched, that is a real HTTP call.
        Default to "probe failed" (resync skipped); tests exercising the
        oracle install their own patch inside this one's scope."""
        with patch(
            "claude_swap.oauth.fetch_oauth_profile", return_value=None
        ):
            yield

    def test_expired_refreshes_under_locks_and_persists_both_stores(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """Expired + attributable → refresh under CC's locks, persist to live
        + backup, then fetch usage with the rotated token."""
        from claude_swap.claude_locks import credentials_lock_dir, oauth_refresh_lock_dir

        switcher = self._switcher(sample_sequence_data)
        usage_result = {"five_hour": {"pct": 10}}
        locks_held_during_post = {}

        def mock_refresh(credentials, **kw):
            from claude_swap.claude_locks import config_lock_dir
            locks_held_during_post["primary"] = oauth_refresh_lock_dir().is_dir()
            locks_held_during_post["legacy"] = credentials_lock_dir().is_dir()
            # CC holds only the CREDENTIAL locks across its POST; the config
            # lock guards a local RMW with a ~10s retry budget on CC's side —
            # holding it through a slow POST could exhaust a concurrent CC
            # config save's retries.
            locks_held_during_post["config"] = config_lock_dir().is_dir()
            return oauth.RefreshOutcome(self._REFRESHED, None)

        def mock_fetch(account_num, email, credentials, is_active):
            from claude_swap.claude_locks import config_lock_dir
            assert is_active is True
            assert credentials == self._REFRESHED  # rotated token used for usage
            # All locks must be RELEASED before the usage fetch — only the
            # refresh exchange runs under them.
            assert not oauth_refresh_lock_dir().is_dir()
            assert not credentials_lock_dir().is_dir()
            assert not config_lock_dir().is_dir()
            return oauth.UsageOutcome(usage_result)

        with patch.object(switcher, "_read_credentials", return_value=self._EXPIRED), \
             patch.object(
                 switcher, "_read_account_credentials", return_value=self._EXPIRED
             ), \
             patch.object(switcher, "_write_credentials") as write_live, \
             patch.object(switcher, "_write_account_credentials") as write_backup, \
             patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   side_effect=mock_refresh), \
             patch("claude_swap.oauth.try_fetch_usage_for_account",
                   side_effect=mock_fetch):
            result = switcher._fetch_active_usage("1", "test@example.com", self._EXPIRED)

        assert result.usage == usage_result
        assert result.sentinel is None
        # The token POST ran while both CREDENTIAL locks were held and the
        # config lock was NOT (it is narrowed to the persist step).
        assert locks_held_during_post == {
            "primary": True, "legacy": True, "config": False,
        }
        write_live.assert_called_once_with(self._REFRESHED)
        write_backup.assert_called_once_with("1", "test@example.com", self._REFRESHED)

    def test_owner_present_no_longer_blocks_the_refresh(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """A running Claude Code does not veto the refresh — the locks
        serialize against it and it adopts the rotation (race_resolved)."""
        switcher = self._switcher(sample_sequence_data)

        with patch.object(switcher, "_read_credentials", return_value=self._EXPIRED), \
             patch.object(
                 switcher, "_read_account_credentials", return_value=self._EXPIRED
             ), \
             patch.object(switcher, "_live_session_pids", return_value=[4242]), \
             patch.object(switcher, "_write_credentials") as write_live, \
             patch.object(switcher, "_write_account_credentials"), \
             patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   side_effect=self._refresh_ok), \
             patch("claude_swap.oauth.try_fetch_usage_for_account",
                   return_value=oauth.UsageOutcome({"five_hour": {"pct": 5}})):
            result = switcher._fetch_active_usage("1", "test@example.com", self._EXPIRED)

        assert result.sentinel is None
        write_live.assert_called_once_with(self._REFRESHED)

    def test_lock_reread_adopts_a_fresher_live_credential(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """If the under-lock re-read finds a non-expired credential (CC beat us
        to the refresh), adopt it — no token POST, no generation consumed."""
        switcher = self._switcher(sample_sequence_data)
        cc_rotated = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-cc",
                "refreshToken": "rt-cc",
                "expiresAt": 9999999999000,
            }
        })

        def mock_fetch(account_num, email, credentials, is_active):
            assert credentials == cc_rotated
            return oauth.UsageOutcome({"five_hour": {"pct": 7}})

        with patch.object(switcher, "_read_credentials", return_value=cc_rotated), \
             patch.object(
                 switcher, "_read_account_credentials", return_value=self._EXPIRED
             ), \
             patch.object(switcher, "_write_credentials") as write_live, \
             patch("claude_swap.oauth.try_refresh_oauth_credentials") as mock_refresh, \
             patch("claude_swap.oauth.try_fetch_usage_for_account",
                   side_effect=mock_fetch):
            result = switcher._fetch_active_usage("1", "test@example.com", self._EXPIRED)

        assert result.sentinel is None
        mock_refresh.assert_not_called()   # adopted, not consumed
        write_live.assert_not_called()     # nothing to persist

    def test_lock_reread_never_adopts_a_wiped_live_credential(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """A CC ``invalid_grant`` wipe (tokens emptied, metadata kept) can land
        between the pre-lock read and the under-lock re-read. A wiped blob
        with a future ``expiresAt`` must not take the adopt branch — resyncing
        it would copy the empty tokens over the slot's backup. It is also not
        ours to consume (fingerprint can't match the backup) → defer."""
        switcher = self._switcher(sample_sequence_data)
        wiped_future = json.dumps({
            "claudeAiOauth": {
                "accessToken": "",
                "refreshToken": "",
                "expiresAt": 9999999999000,
            }
        })

        with patch.object(switcher, "_read_credentials", return_value=wiped_future), \
             patch.object(
                 switcher, "_read_account_credentials", return_value=self._EXPIRED
             ), \
             patch.object(switcher, "_write_account_credentials") as write_backup, \
             patch("claude_swap.oauth.try_refresh_oauth_credentials") as mock_refresh, \
             patch("claude_swap.oauth.try_fetch_usage_for_account") as mock_fetch:
            result = switcher._fetch_active_usage("1", "test@example.com", self._EXPIRED)

        assert result.sentinel == USAGE_TOKEN_EXPIRED
        mock_refresh.assert_not_called()   # nothing consumed
        write_backup.assert_not_called()   # the wipe never reaches the backup
        mock_fetch.assert_not_called()

    def test_non_oauth_live_with_moved_identity_is_never_clobbered(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """A switch to an API-key account landing between the pre-lock check
        and lock acquisition leaves a non-OAuth live blob (live_oauth None) —
        the identity guard must still run, or the consume path would POST our
        grant and clobber the other account's live store."""
        switcher = self._switcher(sample_sequence_data)

        with patch.object(
                 switcher, "_read_credentials",
                 return_value="sk-ant-api03-somekey",
             ), \
             patch.object(
                 switcher, "_read_account_credentials", return_value=self._EXPIRED
             ), \
             patch.object(
                 switcher, "_get_current_account",
                 return_value=("console-api@token.local", ""),
             ), \
             patch.object(switcher, "_write_credentials") as write_live, \
             patch("claude_swap.oauth.try_refresh_oauth_credentials") as mock_refresh, \
             patch("claude_swap.oauth.try_fetch_usage_for_account") as mock_fetch:
            result = switcher._fetch_active_usage("1", "test@example.com", self._EXPIRED)

        assert result.sentinel == USAGE_TOKEN_EXPIRED
        mock_refresh.assert_not_called()
        write_live.assert_not_called()
        mock_fetch.assert_not_called()

    def test_empty_live_with_matching_identity_still_recovers(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """An empty live store whose config identity is still ours (CC fully
        cleared the credential) is the recovery case: consume the backup's
        grant and restore the live store."""
        switcher = self._switcher(sample_sequence_data)

        with patch.object(switcher, "_read_credentials", return_value=""), \
             patch.object(
                 switcher, "_read_account_credentials", return_value=self._EXPIRED
             ), \
             patch.object(switcher, "_write_credentials") as write_live, \
             patch.object(switcher, "_write_account_credentials"), \
             patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   side_effect=self._refresh_ok), \
             patch("claude_swap.oauth.try_fetch_usage_for_account",
                   return_value=oauth.UsageOutcome({"five_hour": {"pct": 5}})):
            result = switcher._fetch_active_usage("1", "test@example.com", self._EXPIRED)

        assert result.sentinel is None
        write_live.assert_called_once_with(self._REFRESHED)

    def test_a_held_consume_lock_defers_the_active_refresh(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """The active path may POST the slot's BACKUP grant, so it owes the
        consume lock.

        refresh_input becomes `backup` when the live bytes moved or were
        cleared, which makes this a second backup-token POST outside
        consume_backup_grant. Measured before the fix: with .consume-N.lock
        held by another process, this path still POSTed the shared grant —
        one of the two POSTs wins, the loser gets invalid_grant, and the
        strike lands on a live account.
        """
        from claude_swap.locking import FileLock

        switcher = self._switcher(sample_sequence_data)
        holder = FileLock(switcher.credentials_dir / ".consume-1.lock")
        assert holder.acquire(), "could not seed the contended lock"
        try:
            with patch.object(
                switcher, "_read_credentials", return_value=self._EXPIRED
            ), patch.object(
                switcher, "_read_account_credentials", return_value=self._EXPIRED
            ), patch(
                "claude_swap.oauth.try_refresh_oauth_credentials"
            ) as mock_refresh, patch(
                "claude_swap.oauth.try_fetch_usage_for_account"
            ):
                result = switcher._fetch_active_usage(
                    "1", "test@example.com", self._EXPIRED
                )
        finally:
            holder.release()

        mock_refresh.assert_not_called(), (
            "POSTed a backup grant while another consume held its lock"
        )
        assert result.sentinel == USAGE_TOKEN_EXPIRED

    def test_filelock_contention_defers_instead_of_raising(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """cswap's own account FileLock contending (another cswap operation in
        flight) must defer like a CC lock timeout — _fetch_account_usage's
        never-raises contract is what keeps the collect pass alive."""
        from claude_swap.exceptions import LockError

        switcher = self._switcher(sample_sequence_data)

        with patch.object(switcher, "_read_credentials", return_value=self._EXPIRED), \
             patch.object(
                 switcher, "_read_account_credentials", return_value=self._EXPIRED
             ), \
             patch("claude_swap.switcher.FileLock",
                   side_effect=LockError("held elsewhere")), \
             patch("claude_swap.oauth.try_refresh_oauth_credentials") as mock_refresh, \
             patch("claude_swap.oauth.try_fetch_usage_for_account") as mock_fetch:
            result = switcher._fetch_active_usage("1", "test@example.com", self._EXPIRED)

        assert result.sentinel == USAGE_TOKEN_EXPIRED
        mock_refresh.assert_not_called()
        mock_fetch.assert_not_called()

    def test_unattributed_live_recovers_from_the_slots_backup(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """Provenance guard (issue #117), refined: an expired live credential
        whose lineage doesn't match the slot's backup is never itself
        consumed — but the slot's own backup grant IS consumable (it is the
        slot's credential by definition), healing the stale-sync and
        stranded-live shapes instead of stalling forever."""
        switcher = self._switcher(sample_sequence_data)
        slot_backup = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-b", "refreshToken": "rt-slot",
                "expiresAt": 1000,
            },
        })
        consumed = []

        def mock_refresh(credentials, **kw):
            consumed.append(credentials)
            return oauth.RefreshOutcome(self._REFRESHED, None)

        with patch.object(switcher, "_read_credentials", return_value=self._EXPIRED), \
             patch.object(
                 switcher, "_read_account_credentials", return_value=slot_backup
             ), \
             patch.object(switcher, "_write_credentials") as write_live, \
             patch.object(switcher, "_write_account_credentials"), \
             patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   side_effect=mock_refresh), \
             patch("claude_swap.oauth.try_fetch_usage_for_account",
                   return_value=oauth.UsageOutcome({"five_hour": {"pct": 5}})):
            result = switcher._fetch_active_usage("1", "test@example.com", self._EXPIRED)

        assert result.sentinel is None
        assert consumed == [slot_backup]  # backup's grant, never the live one
        write_live.assert_called_once_with(self._REFRESHED)

    def test_unattributed_live_with_unusable_backup_never_consumes(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """When the live lineage is unattributable AND the backup holds no
        usable grant, nothing may be consumed — bail with the sentinel."""
        switcher = self._switcher(sample_sequence_data)
        dead_backup = json.dumps({
            "claudeAiOauth": {"accessToken": "", "refreshToken": ""},
        })
        foreign_live = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-x", "refreshToken": "rt-foreign",
                "expiresAt": 1000,
            },
        })

        with patch.object(switcher, "_read_credentials", return_value=foreign_live), \
             patch.object(
                 switcher, "_read_account_credentials", return_value=dead_backup
             ), \
             patch("claude_swap.oauth.try_refresh_oauth_credentials") as mock_refresh, \
             patch("claude_swap.oauth.try_fetch_usage_for_account") as mock_fetch:
            result = switcher._fetch_active_usage("1", "test@example.com", foreign_live)

        assert result.sentinel == USAGE_TOKEN_EXPIRED
        mock_refresh.assert_not_called()
        mock_fetch.assert_not_called()

    def test_live_read_error_defers_instead_of_consuming(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """_read_credentials None means read ERROR (locked keychain), not
        absence — the store may hold a newer credential we cannot see, so
        nothing may be consumed; defer to the next pass."""
        switcher = self._switcher(sample_sequence_data)

        with patch.object(switcher, "_read_credentials", return_value=None), \
             patch.object(
                 switcher, "_read_account_credentials", return_value=self._EXPIRED
             ), \
             patch("claude_swap.oauth.try_refresh_oauth_credentials") as mock_refresh, \
             patch("claude_swap.oauth.try_fetch_usage_for_account") as mock_fetch:
            result = switcher._fetch_active_usage("1", "test@example.com", self._EXPIRED)

        assert result.sentinel == USAGE_TOKEN_EXPIRED
        mock_refresh.assert_not_called()
        mock_fetch.assert_not_called()

    def test_stranded_live_store_is_restored_from_the_backup(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """A prior refresh that persisted the successor to the backup but
        failed the live write leaves live = consumed generation, backup =
        healthy successor. The next pass must restore the backup to the live
        store — no POST, no generation consumed — instead of stalling on
        'provenance unknown' until CC POSTs the dead grant and wipes."""
        switcher = self._switcher(sample_sequence_data)
        successor = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-successor", "refreshToken": "rt-successor",
                "expiresAt": 9999999999000,
            },
        })

        with patch.object(switcher, "_read_credentials", return_value=self._EXPIRED), \
             patch.object(
                 switcher, "_read_account_credentials", return_value=successor
             ), \
             patch.object(switcher, "_write_credentials") as write_live, \
             patch.object(switcher, "_write_account_credentials") as write_backup, \
             patch("claude_swap.oauth.try_refresh_oauth_credentials") as mock_refresh, \
             patch("claude_swap.oauth.try_fetch_usage_for_account",
                   return_value=oauth.UsageOutcome({"five_hour": {"pct": 5}})) as mock_fetch:
            result = switcher._fetch_active_usage("1", "test@example.com", self._EXPIRED)

        assert result.sentinel is None
        mock_refresh.assert_not_called()          # restore, not a refresh
        write_live.assert_called_once_with(successor)
        write_backup.assert_not_called()          # backup already holds it
        assert mock_fetch.call_args[0][2] == successor

    def test_identity_check_compares_organization_too(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """Accounts are keyed on (email, organizationUuid) — a switch to a
        same-email different-org slot in the lock gap must be caught."""
        switcher = self._switcher(sample_sequence_data)
        cc_rotated = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-cc", "refreshToken": "rt-cc",
                "expiresAt": 9999999999000,
            }
        })

        with patch.object(switcher, "_read_credentials", return_value=cc_rotated), \
             patch.object(
                 switcher, "_read_account_credentials", return_value=self._EXPIRED
             ), \
             patch.object(
                 switcher, "_get_current_account",
                 return_value=("test@example.com", "org-OTHER"),
             ), \
             patch.object(switcher, "_write_account_credentials") as write_backup, \
             patch("claude_swap.oauth.try_refresh_oauth_credentials") as mock_refresh, \
             patch("claude_swap.oauth.try_fetch_usage_for_account") as mock_fetch:
            result = switcher._fetch_active_usage(
                "1", "test@example.com", self._EXPIRED, org_uuid=""
            )

        assert result.sentinel == USAGE_TOKEN_EXPIRED
        mock_refresh.assert_not_called()
        write_backup.assert_not_called()   # the other org's rotation is not adopted
        mock_fetch.assert_not_called()

    def test_locally_valid_but_server_rejected_token_refreshes(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """A non-expired token the usage endpoint 401s (revoked out-of-band —
        measured: a sibling machine rotating a synced lineage) must trigger
        the locked refresh instead of hours of backoff until local expiry."""
        switcher = self._switcher(sample_sequence_data)
        valid_but_revoked = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-revoked", "refreshToken": "rt-orig",
                "expiresAt": 9999999999000,
            }
        })
        fetch_calls = []

        def mock_fetch(account_num, email, credentials, is_active):
            fetch_calls.append(credentials)
            if credentials == valid_but_revoked:
                return oauth.UsageOutcome(None, error="http-401")
            return oauth.UsageOutcome({"five_hour": {"pct": 5}})

        with patch.object(
                 switcher, "_read_credentials", return_value=valid_but_revoked
             ), \
             patch.object(
                 switcher, "_read_account_credentials",
                 return_value=valid_but_revoked,
             ), \
             patch.object(switcher, "_write_credentials") as write_live, \
             patch.object(switcher, "_write_account_credentials"), \
             patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   side_effect=self._refresh_ok), \
             patch("claude_swap.oauth.try_fetch_usage_for_account",
                   side_effect=mock_fetch):
            result = switcher._fetch_active_usage(
                "1", "test@example.com", valid_but_revoked
            )

        assert result.sentinel is None
        assert result.usage == {"five_hour": {"pct": 5}}
        write_live.assert_called_once_with(self._REFRESHED)
        assert fetch_calls == [valid_but_revoked, self._REFRESHED]

    def test_server_rejected_token_with_no_recovery_surfaces_the_401(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """A locally-valid token the server 401s, with an unattributable live
        credential AND an unusable backup, has no recovery path — the 401
        must reach the store as an ERROR (backoff, strike accounting), not a
        'token expired' sentinel that mislabels an unexpired token and
        re-fetches every pass forever."""
        switcher = self._switcher(sample_sequence_data)
        valid_but_revoked = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-revoked", "refreshToken": "rt-foreign",
                "expiresAt": 9999999999000,
            }
        })
        dead_backup = json.dumps({
            "claudeAiOauth": {"accessToken": "", "refreshToken": ""},
        })

        with patch.object(
                 switcher, "_read_credentials", return_value=valid_but_revoked
             ), \
             patch.object(
                 switcher, "_read_account_credentials", return_value=dead_backup
             ), \
             patch("claude_swap.oauth.try_refresh_oauth_credentials") as mock_refresh, \
             patch("claude_swap.oauth.try_fetch_usage_for_account",
                   return_value=oauth.UsageOutcome(None, error="http-401")):
            result = switcher._fetch_active_usage(
                "1", "test@example.com", valid_but_revoked
            )

        assert result.error == "http-401"
        assert result.sentinel is None
        mock_refresh.assert_not_called()

    def test_server_rejected_token_lock_contention_still_surfaces_the_401(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """Every pre-consumption defer (lock contention here) of a 401'd but
        locally-valid token must surface the 401 record, not the 'token
        expired' sentinel — same contract as the no-recovery bail-out."""
        from claude_swap.claude_locks import oauth_refresh_lock_dir

        switcher = self._switcher(sample_sequence_data)
        valid_but_revoked = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-revoked", "refreshToken": "rt-orig",
                "expiresAt": 9999999999000,
            }
        })
        lock = oauth_refresh_lock_dir()
        lock.mkdir(parents=True)  # fresh mtime = live holder
        try:
            with patch.object(
                     switcher, "_read_credentials",
                     return_value=valid_but_revoked,
                 ), \
                 patch.object(
                     switcher, "_read_account_credentials",
                     return_value=valid_but_revoked,
                 ), \
                 patch("claude_swap.claude_locks.DEFAULT_TIMEOUT_S", 0.3), \
                 patch("claude_swap.oauth.try_refresh_oauth_credentials") as mock_refresh, \
                 patch("claude_swap.oauth.try_fetch_usage_for_account",
                       return_value=oauth.UsageOutcome(None, error="http-401")):
                result = switcher._fetch_active_usage(
                    "1", "test@example.com", valid_but_revoked
                )
        finally:
            lock.rmdir()

        assert result.error == "http-401"
        assert result.sentinel is None
        mock_refresh.assert_not_called()

    def test_no_refresh_token_is_permanent_not_transient(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """An expired credential with no refresh token can never be healed by
        retrying — surface a permanent auth error (strikes → quarantine), not
        the transient 'refresh-failed' backoff loop."""
        switcher = self._switcher(sample_sequence_data)
        no_rt = json.dumps({
            "claudeAiOauth": {"accessToken": "sk-only", "expiresAt": 1000},
        })

        with patch.object(switcher, "_read_credentials", return_value=no_rt), \
             patch.object(
                 switcher, "_read_account_credentials", return_value=no_rt
             ), \
             patch("claude_swap.oauth.try_fetch_usage_for_account") as mock_fetch:
            result = switcher._fetch_active_usage("1", "test@example.com", no_rt)

        assert result.error == "no_refresh_token"
        assert result.sentinel is None
        mock_fetch.assert_not_called()

    def test_unexpected_exception_defers_never_raises(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """_fetch_account_usage promises never to raise — an OSError from
        config/lock I/O inside the refresh must degrade to the sentinel, not
        kill the whole collect pass."""
        switcher = self._switcher(sample_sequence_data)

        with patch.object(switcher, "_read_credentials",
                          side_effect=PermissionError("config unreadable")), \
             patch.object(
                 switcher, "_read_account_credentials", return_value=self._EXPIRED
             ), \
             patch("claude_swap.oauth.try_fetch_usage_for_account") as mock_fetch:
            result = switcher._fetch_active_usage("1", "test@example.com", self._EXPIRED)

        assert result.sentinel == USAGE_TOKEN_EXPIRED
        mock_fetch.assert_not_called()

    def test_transient_refresh_failure_backs_off_via_the_store(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """A transient refresh failure surfaces as an ERROR (store-driven
        backoff), not a silent sentinel that would re-POST every pass."""
        switcher = self._switcher(sample_sequence_data)

        with patch.object(switcher, "_read_credentials", return_value=self._EXPIRED), \
             patch.object(
                 switcher, "_read_account_credentials", return_value=self._EXPIRED
             ), \
             patch.object(switcher, "_write_credentials") as write_live, \
             patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   return_value=oauth.RefreshOutcome(None, "transient")), \
             patch("claude_swap.oauth.try_fetch_usage_for_account") as mock_fetch:
            result = switcher._fetch_active_usage("1", "test@example.com", self._EXPIRED)

        assert result.error == "refresh-failed"
        assert result.sentinel is None
        mock_fetch.assert_not_called()
        write_live.assert_not_called()

    def test_lock_timeout_defers_to_the_holder(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """A held CC lock (live refresh in flight) → clean sentinel, no POST,
        no takeover; the holder's rotation lands on its own."""
        from claude_swap.claude_locks import oauth_refresh_lock_dir

        switcher = self._switcher(sample_sequence_data)
        lock = oauth_refresh_lock_dir()
        lock.mkdir(parents=True)  # fresh mtime = live holder
        try:
            with patch.object(switcher, "_read_credentials", return_value=self._EXPIRED), \
                 patch.object(
                     switcher, "_read_account_credentials", return_value=self._EXPIRED
                 ), \
                 patch("claude_swap.claude_locks.DEFAULT_TIMEOUT_S", 0.3), \
                 patch("claude_swap.oauth.try_refresh_oauth_credentials") as mock_refresh, \
                 patch("claude_swap.oauth.try_fetch_usage_for_account") as mock_fetch:
                result = switcher._fetch_active_usage(
                    "1", "test@example.com", self._EXPIRED
                )
        finally:
            lock.rmdir()

        assert result.sentinel == USAGE_TOKEN_EXPIRED
        mock_refresh.assert_not_called()
        mock_fetch.assert_not_called()

    def test_consumed_generation_survives_a_live_write_failure(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """Once the grant is consumed the successor must not be lost: a failing
        live-store write still persists the rotated credential to the slot
        backup (the lineage survives; never leave a consumed generation as the
        only copy in memory)."""
        switcher = self._switcher(sample_sequence_data)

        with patch.object(switcher, "_read_credentials", return_value=self._EXPIRED), \
             patch.object(
                 switcher, "_read_account_credentials", return_value=self._EXPIRED
             ), \
             patch.object(switcher, "_write_credentials",
                          side_effect=OSError("disk full")), \
             patch.object(switcher, "_write_account_credentials") as write_backup, \
             patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   side_effect=self._refresh_ok), \
             patch("claude_swap.oauth.try_fetch_usage_for_account") as mock_fetch:
            result = switcher._fetch_active_usage("1", "test@example.com", self._EXPIRED)

        write_backup.assert_called_once_with(
            "1", "test@example.com", self._REFRESHED
        )
        # Live store still holds the consumed token → report expired, not usage.
        assert result.sentinel == USAGE_TOKEN_EXPIRED
        mock_fetch.assert_not_called()

    def test_non_expired_fetches_without_refreshing(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """A live, non-expired token is fetched as-is (is_active=True) — no
        refresh machinery touched."""
        switcher = self._switcher(sample_sequence_data)
        fresh = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-live",
                "refreshToken": "rt-live",
                "expiresAt": 9999999999000,
            }
        })

        with patch("claude_swap.oauth.try_refresh_oauth_credentials") as mock_refresh, \
             patch("claude_swap.oauth.try_fetch_usage_for_account",
                   return_value=oauth.UsageOutcome({"five_hour": {"pct": 3}})) as mock_fetch:
            result = switcher._fetch_active_usage("1", "test@example.com", fresh)

        assert result.usage == {"five_hour": {"pct": 3}}
        mock_refresh.assert_not_called()
        assert mock_fetch.call_args.kwargs.get("is_active") is True

    def test_fresh_fetch_resyncs_backup_after_external_rotation(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """Rotation-before-collection, phase 1: CC rotated A→B during normal
        use; the fresh-token fast path serves usage off B while the slot
        backup still holds A (consumed). The fast path must resync the backup
        to B — otherwise at B's expiry the recovery branch POSTs A's dead
        grant and quarantines a healthy slot. The drifted lineage must first
        be attributed by the profile oracle (a foreign credential under a
        stale config looks identical locally); a matching probe licenses the
        write and is memoized, so a second pass never re-probes."""
        switcher = self._switcher(sample_sequence_data)

        with patch.object(
                 switcher, "_read_credentials", return_value=self._REFRESHED
             ), \
             patch.object(
                 switcher, "_read_account_credentials",
                 return_value=self._EXPIRED,   # stale lineage A
             ), \
             patch.object(switcher, "_write_account_credentials") as write_backup, \
             patch("claude_swap.oauth.fetch_oauth_profile",
                   return_value=self._PROFILE_SELF) as mock_probe, \
             patch("claude_swap.oauth.try_refresh_oauth_credentials") as mock_refresh, \
             patch("claude_swap.oauth.try_fetch_usage_for_account",
                   return_value=oauth.UsageOutcome({"five_hour": {"pct": 3}})):
            result = switcher._fetch_active_usage(
                "1", "test@example.com", self._REFRESHED
            )
            # Second pass with the backup still stale (mock never updates):
            # the memoized verdict must answer instead of a second probe.
            switcher._fetch_active_usage(
                "1", "test@example.com", self._REFRESHED
            )

        assert result.usage == {"five_hour": {"pct": 3}}
        mock_refresh.assert_not_called()   # nothing consumed — pure resync
        mock_probe.assert_called_once()
        assert write_backup.call_args_list == [
            call("1", "test@example.com", self._REFRESHED),
            call("1", "test@example.com", self._REFRESHED),
        ]

    def test_fresh_fetch_same_lineage_skips_the_resync(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """The steady state — backup already on the served lineage — takes no
        locks and writes nothing."""
        switcher = self._switcher(sample_sequence_data)

        with patch.object(
                 switcher, "_read_account_credentials",
                 return_value=self._REFRESHED,
             ), \
             patch.object(switcher, "_read_credentials") as read_live, \
             patch.object(switcher, "_write_account_credentials") as write_backup, \
             patch("claude_swap.oauth.try_fetch_usage_for_account",
                   return_value=oauth.UsageOutcome({"five_hour": {"pct": 3}})):
            switcher._fetch_active_usage(
                "1", "test@example.com", self._REFRESHED
            )

        write_backup.assert_not_called()
        read_live.assert_not_called()   # early fingerprint check short-circuits

    def _fresh_drift_pass(self, switcher, probe):
        """Drive the fresh fast path with a backup-lineage mismatch, with the
        oracle answering ``probe`` (a return_value or side_effect list)."""
        kwargs = (
            {"side_effect": probe} if isinstance(probe, list)
            else {"return_value": probe}
        )
        with patch.object(
                 switcher, "_read_credentials", return_value=self._REFRESHED
             ), \
             patch.object(
                 switcher, "_read_account_credentials",
                 return_value=self._EXPIRED,
             ), \
             patch.object(switcher, "_write_account_credentials") as write_backup, \
             patch("claude_swap.oauth.fetch_oauth_profile", **kwargs) as mock_probe, \
             patch("claude_swap.oauth.try_fetch_usage_for_account",
                   return_value=oauth.UsageOutcome({"five_hour": {"pct": 3}})):
            result = switcher._fetch_active_usage(
                "1", "test@example.com", self._REFRESHED
            )
        return result, write_backup, mock_probe

    def test_fresh_foreign_probe_mismatch_skips_resync_warns_once_and_caches(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict,
        caplog,
    ):
        """A drifted lineage the oracle resolves to a DIFFERENT account is
        never written into the slot backup (foreign credential under a stale
        config — the write would destroy the slot's only refresh token), and
        its usage is suppressed with the foreign sentinel instead of being
        recorded as this slot's (#117's mis-keying shape). The verdict is
        cached so the same foreign bytes neither re-probe nor re-warn."""
        import logging

        switcher = self._switcher(sample_sequence_data)
        foreign = {
            "uuid": "uuid-foreign", "email": "other@example.com",
            "organizationUuid": None,
        }

        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            first, write_backup, mock_probe = self._fresh_drift_pass(
                switcher, foreign
            )
            second, write_backup2, mock_probe2 = self._fresh_drift_pass(
                switcher, foreign
            )

        assert first.sentinel == USAGE_FOREIGN_CREDENTIAL
        assert first.usage is None
        assert second.sentinel == USAGE_FOREIGN_CREDENTIAL
        write_backup.assert_not_called()
        write_backup2.assert_not_called()
        mock_probe.assert_called_once()
        mock_probe2.assert_not_called()   # verdict cached, no re-probe
        warnings = [
            r for r in caplog.records
            if "resolves to a different account" in r.getMessage()
        ]
        assert len(warnings) == 1

    def test_fresh_probe_failure_skips_resync_and_retries_next_pass(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """An unreachable oracle is transient evidence-free failure: the
        resync is skipped (backup untouched) but nothing is cached — the next
        pass probes again and heals once the oracle answers."""
        switcher = self._switcher(sample_sequence_data)

        first, write_backup, mock_probe = self._fresh_drift_pass(
            switcher, None
        )
        second, write_backup2, mock_probe2 = self._fresh_drift_pass(
            switcher, self._PROFILE_SELF
        )

        assert first.usage == {"five_hour": {"pct": 3}}
        write_backup.assert_not_called()
        mock_probe.assert_called_once()
        # Next pass re-probes; a matching answer licenses the resync.
        mock_probe2.assert_called_once()
        write_backup2.assert_called_once_with(
            "1", "test@example.com", self._REFRESHED
        )

    def test_fresh_probe_unverifiable_not_cached(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """A slot with no stored uuid and a partial resolved identity cannot
        be condemned OR confirmed: skip the resync, cache nothing (a cached
        False on partial evidence would block a legitimate resync for the
        process lifetime), re-probe next pass."""
        sample_sequence_data["accounts"]["1"].pop("uuid")
        switcher = self._switcher(sample_sequence_data)
        partial = {"uuid": "uuid-x", "email": None, "organizationUuid": None}

        first, write_backup, mock_probe = self._fresh_drift_pass(
            switcher, partial
        )
        second, write_backup2, mock_probe2 = self._fresh_drift_pass(
            switcher, partial
        )

        write_backup.assert_not_called()
        write_backup2.assert_not_called()
        mock_probe.assert_called_once()
        mock_probe2.assert_called_once()   # nothing cached — probed again
        assert switcher._probe_verdicts == {}

    def test_uuid_less_slot_email_match_with_missing_org_is_unverifiable(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """Email match alone must not affirm ownership when the resolved org
        is absent: the same email legitimately exists across personal/org
        accounts, and a missing org is indistinguishable from a personal
        account. Unverifiable — no write, no backfill, nothing cached."""
        sample_sequence_data["accounts"]["1"].pop("uuid")
        switcher = self._switcher(sample_sequence_data)
        orgless = {
            "uuid": "uuid-x", "email": "test@example.com",
            "organizationUuid": None,
        }

        first, write_backup, mock_probe = self._fresh_drift_pass(
            switcher, orgless
        )

        write_backup.assert_not_called()
        assert switcher._probe_verdicts == {}
        assert switcher.account_identity("1")["uuid"] == ""   # no backfill
        assert first.usage == {"five_hour": {"pct": 3}}   # not condemned

    def test_uuid_less_personal_slot_rejects_same_email_under_foreign_org(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """The same email under a DIFFERENT org is a sibling account, not
        this slot: definitive foreign — no write, no backfill, verdict
        cached, usage suppressed."""
        sample_sequence_data["accounts"]["1"].pop("uuid")
        switcher = self._switcher(sample_sequence_data)
        sibling = {
            "uuid": "uuid-org-sibling", "email": "test@example.com",
            "organizationUuid": "org-B",
        }

        first, write_backup, mock_probe = self._fresh_drift_pass(
            switcher, sibling
        )
        second, write_backup2, mock_probe2 = self._fresh_drift_pass(
            switcher, sibling
        )

        write_backup.assert_not_called()
        write_backup2.assert_not_called()
        assert first.sentinel == USAGE_FOREIGN_CREDENTIAL
        mock_probe2.assert_not_called()   # False cached
        assert switcher.account_identity("1")["uuid"] == ""   # no backfill

    def test_uuid_less_slot_exact_email_org_match_resyncs_and_backfills(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """A structurally complete (email, org) match on a uuid-less slot is
        affirmative: resync fires and the resolved uuid is recorded so
        future verdicts go uuid-positive."""
        sample_sequence_data["accounts"]["1"].pop("uuid")
        switcher = self._switcher(sample_sequence_data)
        complete = {
            "uuid": "uuid-resolved", "email": "test@example.com",
            "organizationUuid": "",
        }

        first, write_backup, mock_probe = self._fresh_drift_pass(
            switcher, complete
        )

        assert first.usage == {"five_hour": {"pct": 3}}
        write_backup.assert_called_once_with(
            "1", "test@example.com", self._REFRESHED
        )
        assert switcher.account_identity("1")["uuid"] == "uuid-resolved"

    def test_lineage_key_binds_the_stored_email_too(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """Add-token records keep org and uuid blank, so a stored-email
        change is the only identity marker a re-creation moves there — the
        key must move with it, or a stale caller email could resurrect the
        predecessor's verdict."""
        sample_sequence_data["accounts"]["1"].pop("uuid")
        switcher = self._switcher(sample_sequence_data)
        before = switcher._lineage_key("1", "test@example.com", "fp")

        data = switcher._get_sequence_data()
        data["accounts"]["1"]["email"] = "new@example.com"
        switcher._write_json(switcher.sequence_file, data)

        assert switcher._lineage_key("1", "test@example.com", "fp") != before

    def test_verdict_does_not_survive_a_slot_identity_change(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """A verdict is bound to the slot's stored identity: a slot
        re-created for a different account (same number, same email — e.g.
        across orgs) must not inherit its predecessor's license to consume
        a lineage at expiry."""
        switcher = self._switcher(sample_sequence_data)
        live_b = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-B", "refreshToken": "rt-B",
                "expiresAt": 2000,
            }
        })
        backup_a = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-A", "refreshToken": "rt-A",
                "expiresAt": 1000,
            }
        })
        switcher._probe_verdicts[
            switcher._lineage_key(
                "1", "test@example.com", oauth.credential_fingerprint(live_b)
            )
        ] = True
        # The slot is re-created for a different account: same number and
        # email, new uuid.
        data = switcher._get_sequence_data()
        data["accounts"]["1"]["uuid"] = "uuid-recreated"
        switcher._write_json(switcher.sequence_file, data)

        with patch.object(switcher, "_read_credentials", return_value=live_b), \
             patch.object(
                 switcher, "_read_account_credentials", return_value=backup_a
             ), \
             patch.object(switcher, "_write_credentials") as write_live, \
             patch("claude_swap.oauth.try_refresh_oauth_credentials") as mock_refresh, \
             patch("claude_swap.oauth.try_fetch_usage_for_account") as mock_fetch:
            result = switcher._fetch_active_usage(
                "1", "test@example.com", live_b
            )

        assert result.sentinel == USAGE_TOKEN_EXPIRED
        mock_refresh.assert_not_called()   # stale verdict did not license B
        write_live.assert_not_called()
        mock_fetch.assert_not_called()

    def test_expiry_after_unresynced_rotation_defers_without_verified_lineage(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """Rotation-before-collection, phase 2, cold process: B reached its
        expiry with the backup still on A and no ownership verdict for B's
        lineage. Generation ordering alone cannot tell an unresynced own
        rotation from a foreign credential under a stale config — POSTing B
        could consume another machine's grant. Defer; never POST A either."""
        switcher = self._switcher(sample_sequence_data)
        live_b = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-B", "refreshToken": "rt-B",
                "expiresAt": 2000,        # expired, but newer than A
            }
        })
        backup_a = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-A", "refreshToken": "rt-A-consumed",
                "expiresAt": 1000,        # the older, consumed generation
            }
        })

        with patch.object(switcher, "_read_credentials", return_value=live_b), \
             patch.object(
                 switcher, "_read_account_credentials", return_value=backup_a
             ), \
             patch.object(switcher, "_write_credentials") as write_live, \
             patch.object(switcher, "_write_account_credentials") as write_backup, \
             patch("claude_swap.oauth.try_refresh_oauth_credentials") as mock_refresh, \
             patch("claude_swap.oauth.try_fetch_usage_for_account") as mock_fetch:
            result = switcher._fetch_active_usage(
                "1", "test@example.com", live_b
            )

        assert result.sentinel == USAGE_TOKEN_EXPIRED
        mock_refresh.assert_not_called()
        write_backup.assert_not_called()
        write_live.assert_not_called()
        mock_fetch.assert_not_called()

    def test_expiry_after_unresynced_rotation_consumes_live_when_verified(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """Same drift shape, but this process attributed B's lineage (a
        fresh-pass oracle match whose backup write failed): live B is NEWER
        than backup A and verified ours, so B's refresh token is the valid
        successor and A's grant is the consumed one. POST B; never POST A."""
        switcher = self._switcher(sample_sequence_data)
        live_b = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-B", "refreshToken": "rt-B",
                "expiresAt": 2000,        # expired, but newer than A
            }
        })
        backup_a = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-A", "refreshToken": "rt-A-consumed",
                "expiresAt": 1000,        # the older, consumed generation
            }
        })
        switcher._probe_verdicts[
            switcher._lineage_key(
                "1", "test@example.com", oauth.credential_fingerprint(live_b)
            )
        ] = True

        with patch.object(switcher, "_read_credentials", return_value=live_b), \
             patch.object(
                 switcher, "_read_account_credentials", return_value=backup_a
             ), \
             patch.object(switcher, "_write_credentials") as write_live, \
             patch.object(switcher, "_write_account_credentials") as write_backup, \
             patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   side_effect=self._refresh_ok) as mock_refresh, \
             patch("claude_swap.oauth.try_fetch_usage_for_account",
                   return_value=oauth.UsageOutcome({"five_hour": {"pct": 5}})) as mock_fetch:
            result = switcher._fetch_active_usage(
                "1", "test@example.com", live_b
            )

        assert result.sentinel is None
        mock_refresh.assert_called_once()
        assert mock_refresh.call_args[0][0] == live_b   # B, never A
        write_backup.assert_called_once_with(
            "1", "test@example.com", self._REFRESHED
        )
        write_live.assert_called_once_with(self._REFRESHED)
        assert mock_fetch.call_args[0][2] == self._REFRESHED

    def test_expiry_after_tolerated_backup_write_failure_consumes_live(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """The tolerated 'backup write failed, live write succeeded' case,
        end-to-end in one process: pass 1 refreshes and self-attributes the
        successor S even though the backup write fails; at S's expiry the
        memoized verdict licenses consuming S — no oracle probe, and never a
        re-POST of the old backup grant."""
        switcher = self._switcher(sample_sequence_data)
        # Pass 2's live store: successor lineage rt-new (from _REFRESHED),
        # its access token now expired.
        live_s = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-S", "refreshToken": "rt-new",
                "expiresAt": 3000,        # the successor, now expired
            }
        })
        successor2 = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-newer", "refreshToken": "rt-newer",
                "expiresAt": 9999999999000,
            }
        })
        refresh_results = [
            oauth.RefreshOutcome(self._REFRESHED, None),
            oauth.RefreshOutcome(successor2, None),
        ]

        with patch.object(switcher, "_read_credentials",
                          side_effect=[self._EXPIRED, live_s]), \
             patch.object(
                 switcher, "_read_account_credentials",
                 return_value=self._EXPIRED,   # backup never advances
             ), \
             patch.object(switcher, "_write_credentials"), \
             patch.object(switcher, "_write_account_credentials",
                          side_effect=Exception("disk full")), \
             patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   side_effect=refresh_results) as mock_refresh, \
             patch("claude_swap.oauth.try_fetch_usage_for_account",
                   return_value=oauth.UsageOutcome({"five_hour": {"pct": 5}})), \
             patch("claude_swap.oauth.fetch_oauth_profile") as mock_probe:
            first = switcher._fetch_active_usage(
                "1", "test@example.com", self._EXPIRED
            )
            second = switcher._fetch_active_usage(
                "1", "test@example.com", live_s
            )

        assert first.sentinel is None
        assert second.sentinel is None
        assert mock_refresh.call_count == 2
        assert mock_refresh.call_args_list[1][0][0] == live_s
        mock_probe.assert_not_called()   # self-attribution needs no oracle

    def test_stranded_backup_newer_still_restores_not_consumes_live(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """The discriminator's other arm is unchanged: when the BACKUP is the
        newer generation (a prior refresh persisted the successor to the
        backup but the live write failed), the live bytes are the consumed
        grant — restore the backup, POST nothing."""
        switcher = self._switcher(sample_sequence_data)
        # backup successor: newer AND non-expired → restore path
        successor = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-successor", "refreshToken": "rt-successor",
                "expiresAt": 9999999999000,
            },
        })

        with patch.object(switcher, "_read_credentials", return_value=self._EXPIRED), \
             patch.object(
                 switcher, "_read_account_credentials", return_value=successor
             ), \
             patch.object(switcher, "_write_credentials") as write_live, \
             patch("claude_swap.oauth.try_refresh_oauth_credentials") as mock_refresh, \
             patch("claude_swap.oauth.try_fetch_usage_for_account",
                   return_value=oauth.UsageOutcome({"five_hour": {"pct": 5}})):
            result = switcher._fetch_active_usage(
                "1", "test@example.com", self._EXPIRED
            )

        assert result.sentinel is None
        mock_refresh.assert_not_called()
        write_live.assert_called_once_with(successor)

    def test_no_token_returns_no_credentials(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """Missing access token short-circuits before any fetch."""
        from claude_swap.json_output import USAGE_NO_CREDENTIALS

        switcher = self._switcher(sample_sequence_data)
        with patch("claude_swap.oauth.try_fetch_usage_for_account") as mock_fetch:
            result = switcher._fetch_active_usage("1", "test@example.com", "")
        assert result.sentinel == USAGE_NO_CREDENTIALS
        mock_fetch.assert_not_called()

    def test_list_renders_token_expired_line_on_lock_contention(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict, capsys
    ):
        """End-to-end: --list shows the intentional line when the refresh was
        deferred (CC holds the lock) — the case the sentinel still covers."""
        from claude_swap.claude_locks import oauth_refresh_lock_dir

        switcher = self._switcher(sample_sequence_data)
        lock = oauth_refresh_lock_dir()
        lock.mkdir(parents=True)  # fresh mtime = live holder
        try:
            with patch.object(switcher, "_read_active_credentials",
                              return_value=ActiveCredentials(self._EXPIRED, False)), \
                 patch.object(switcher, "_read_account_credentials",
                              return_value=self._EXPIRED), \
                 patch.object(switcher, "_read_credentials",
                              return_value=self._EXPIRED), \
                 patch("claude_swap.claude_locks.DEFAULT_TIMEOUT_S", 0.3), \
                 patch("claude_swap.oauth.try_fetch_usage_for_account",
                       return_value=oauth.UsageOutcome(None)):
                switcher.list_accounts()
        finally:
            lock.rmdir()

        output = capsys.readouterr().out
        assert "token expired" in output

    def test_expired_active_is_no_longer_statically_gated(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """The owned+expired static sentinel is gone: the collect pass reaches
        the fetch path, which refreshes and returns usage."""
        switcher = self._switcher(sample_sequence_data)
        info = (1, "test@example.com", "", "", True, self._EXPIRED, "")

        with patch.object(switcher, "_read_credentials", return_value=self._EXPIRED), \
             patch.object(
                 switcher, "_read_account_credentials", return_value=self._EXPIRED
             ), \
             patch.object(switcher, "_live_session_pids", return_value=[]), \
             patch.object(switcher, "_write_credentials"), \
             patch.object(switcher, "_write_account_credentials"), \
             patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   side_effect=self._refresh_ok), \
             patch("claude_swap.oauth.try_fetch_usage_for_account",
                   return_value=oauth.UsageOutcome({"five_hour": {"pct": 25.0}})):
            entry = switcher._collect_usage_entries([info])["1"]

        assert entry.sentinel is None
        assert entry.last_good == {"five_hour": {"pct": 25.0}}

    def test_foreign_live_credential_under_the_lock_is_never_consumed(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """TOCTOU guard: a `cswap switch` completing between the pre-lock
        provenance check and lock acquisition replaces the live credential
        with another slot's. The under-lock re-read must re-verify lineage —
        POSTing the foreign grant would rotate the other slot's lineage and
        write its successor into THIS slot's backup (#117 poisoning)."""
        switcher = self._switcher(sample_sequence_data)
        foreign_live = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-other",
                "refreshToken": "rt-other-slot",
                "expiresAt": 1000,   # expired too — refresh path would trigger
            }
        })

        with patch.object(switcher, "_read_credentials", return_value=foreign_live), \
             patch.object(
                 switcher, "_read_account_credentials", return_value=self._EXPIRED
             ), \
             patch.object(switcher, "_write_credentials") as write_live, \
             patch.object(switcher, "_write_account_credentials") as write_backup, \
             patch("claude_swap.oauth.try_refresh_oauth_credentials") as mock_refresh, \
             patch("claude_swap.oauth.try_fetch_usage_for_account") as mock_fetch:
            result = switcher._fetch_active_usage("1", "test@example.com", self._EXPIRED)

        assert result.sentinel == USAGE_TOKEN_EXPIRED
        mock_refresh.assert_not_called()   # the foreign grant must not be consumed
        mock_fetch.assert_not_called()
        write_live.assert_not_called()
        write_backup.assert_not_called()

    def test_backup_write_failure_still_persists_live_and_never_raises(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """A raising backup write must not lose the consumed successor: the
        live write still runs (preserving the lineage where CC reads it), and
        the fetch path keeps its never-raises contract."""
        switcher = self._switcher(sample_sequence_data)

        with patch.object(switcher, "_read_credentials", return_value=self._EXPIRED), \
             patch.object(
                 switcher, "_read_account_credentials", return_value=self._EXPIRED
             ), \
             patch.object(switcher, "_write_account_credentials",
                          side_effect=OSError("disk full")), \
             patch.object(switcher, "_write_credentials") as write_live, \
             patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   side_effect=self._refresh_ok), \
             patch("claude_swap.oauth.try_fetch_usage_for_account",
                   return_value=oauth.UsageOutcome({"five_hour": {"pct": 4}})):
            result = switcher._fetch_active_usage("1", "test@example.com", self._EXPIRED)

        write_live.assert_called_once_with(self._REFRESHED)  # successor survives
        assert result.usage == {"five_hour": {"pct": 4}}     # and serves usage

    def test_dead_lineage_surfaces_invalid_grant_not_a_silent_sentinel(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """invalid_grant must reach the usage store as an ERROR so strikes /
        backoff / the relogin-required quarantine engage — a bare sentinel is
        a no-op to the store and re-POSTs the dead grant every pass."""
        switcher = self._switcher(sample_sequence_data)

        with patch.object(switcher, "_read_credentials", return_value=self._EXPIRED), \
             patch.object(
                 switcher, "_read_account_credentials", return_value=self._EXPIRED
             ), \
             patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   return_value=oauth.RefreshOutcome(None, "invalid_grant")), \
             patch("claude_swap.oauth.try_fetch_usage_for_account") as mock_fetch:
            result = switcher._fetch_active_usage("1", "test@example.com", self._EXPIRED)

        assert result.error == "invalid_grant"
        assert result.sentinel is None
        mock_fetch.assert_not_called()

    _CC_ROTATED = json.dumps({
        "claudeAiOauth": {
            "accessToken": "sk-cc",
            "refreshToken": "rt-cc-new",
            "expiresAt": 9999999999000,
        }
    })

    def _adopt_pass(self, switcher):
        """Drive the adopt branch: expired creds, fresh unrelated live."""
        with patch.object(
                 switcher, "_read_credentials", return_value=self._CC_ROTATED
             ), \
             patch.object(
                 switcher, "_read_account_credentials", return_value=self._EXPIRED
             ), \
             patch.object(switcher, "_get_current_account",
                          return_value=("test@example.com", "")), \
             patch.object(switcher, "_write_credentials") as write_live, \
             patch.object(switcher, "_write_account_credentials") as write_backup, \
             patch("claude_swap.oauth.try_refresh_oauth_credentials") as mock_refresh, \
             patch("claude_swap.oauth.try_fetch_usage_for_account",
                   return_value=oauth.UsageOutcome({"five_hour": {"pct": 7}})) as mock_fetch:
            result = switcher._fetch_active_usage(
                "1", "test@example.com", self._EXPIRED
            )
        return result, write_live, write_backup, mock_refresh, mock_fetch

    def test_adopting_a_cc_rotation_skips_the_resync_without_a_verdict(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """A fresh live credential of unknown lineage is adopted for usage
        (pre-existing behavior) but NOT written into the slot backup: a
        foreign credential under a stale config satisfies every local
        condition here, and network is forbidden under these locks. The next
        fresh pass attributes it via the oracle and heals the backup."""
        switcher = self._switcher(sample_sequence_data)
        result, write_live, write_backup, mock_refresh, _ = (
            self._adopt_pass(switcher)
        )

        assert result.sentinel is None
        mock_refresh.assert_not_called()          # adopted, not consumed
        write_live.assert_not_called()            # live already correct
        write_backup.assert_not_called()          # lineage unverified

    def test_adopting_a_cc_rotation_resyncs_the_slot_backup_when_verified(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """With an ownership verdict for the rotated lineage (fresh-pass
        oracle match earlier in this process), adoption must persist the
        adopted credential into the slot backup — otherwise the NEXT expiry's
        provenance guard would refuse every future refresh."""
        switcher = self._switcher(sample_sequence_data)
        switcher._probe_verdicts[
            switcher._lineage_key(
                "1", "test@example.com",
                oauth.credential_fingerprint(self._CC_ROTATED),
            )
        ] = True
        result, write_live, write_backup, mock_refresh, _ = (
            self._adopt_pass(switcher)
        )

        assert result.sentinel is None
        mock_refresh.assert_not_called()          # adopted, not consumed
        write_live.assert_not_called()            # live already correct
        write_backup.assert_called_once_with(     # lineage continuity restored
            "1", "test@example.com", self._CC_ROTATED
        )

    def test_adopting_a_known_foreign_credential_defers(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """A lineage the oracle already condemned is neither adopted nor
        served: usage fetched with it would be another account's, mislabeled
        as this slot's."""
        switcher = self._switcher(sample_sequence_data)
        switcher._probe_verdicts[
            switcher._lineage_key(
                "1", "test@example.com",
                oauth.credential_fingerprint(self._CC_ROTATED),
            )
        ] = False
        result, write_live, write_backup, mock_refresh, mock_fetch = (
            self._adopt_pass(switcher)
        )

        assert result.sentinel == USAGE_FOREIGN_CREDENTIAL
        mock_refresh.assert_not_called()
        write_live.assert_not_called()
        write_backup.assert_not_called()
        mock_fetch.assert_not_called()


class TestPerformSwitchPostDisplay:
    """Regression tests for the post-switch display running outside the lock."""

    def _setup_two_accounts(
        self,
        temp_home: Path,
        sample_sequence_data: dict,
    ) -> tuple[ClaudeAccountSwitcher, dict, dict]:
        """Set up a switcher with two managed accounts using in-memory
        credential and config stores.

        This bypasses the real macOS Keychain / Windows Credential Manager
        completely so tests never prompt the user for "restore to defaults"
        on macOS and never leak credentials into the developer's keyring.

        Returns (switcher, creds_store, configs_store). Live credentials for
        the active account are written to the temp-home credentials file
        (safe — that file lives in the test's tmp_path).
        """
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        # Live credentials for active account 1 (file under temp_home).
        live_creds = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-live-1",
                "refreshToken": "rt-live-1",
            },
        })
        (temp_home / ".claude" / ".credentials.json").write_text(live_creds)

        # Expired backup credentials for account 2 — forces refresh in
        # list_accounts() proactive path.
        expired_2 = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-stale-2",
                "refreshToken": "rt-orig-2",
                "expiresAt": 0,
                "scopes": ["user:profile"],
            },
        })

        # In-memory stores keyed by (num, email).
        creds_store: dict[tuple[str, str], str] = {
            ("2", "account2@example.com"): expired_2,
        }
        configs_store: dict[tuple[str, str], str] = {
            ("2", "account2@example.com"): json.dumps({
                "oauthAccount": {
                    "emailAddress": "account2@example.com",
                    "accountUuid": "uuid-2",
                },
            }),
        }
        return switcher, creds_store, configs_store

    @staticmethod
    def _install_store_patches(
        switcher: ClaudeAccountSwitcher,
        creds_store: dict[tuple[str, str], str],
        configs_store: dict[tuple[str, str], str],
        live_state: dict,
    ) -> list:
        """Patch credential/config read/write to use in-memory stores.

        Critically, this also stubs _read_credentials/_write_credentials so
        nothing touches the real macOS Keychain (which would prompt the user
        with "Claude wants to use the confidential information stored in your
        keychain" during the test run).
        """
        def read_creds(num, email):
            return creds_store.get((str(num), email), "")

        def read_creds_ex(num, email):
            # The in-memory store IS the backup here, so it is never
            # "unreadable" — but it must answer the strict reader too, or
            # every caller that asks absent-vs-unreadable bypasses the
            # double and reads the real (empty) store instead.
            return creds_store.get((str(num), email), ""), False

        def write_creds(num, email, creds):
            creds_store[(str(num), email)] = creds

        def read_cfg(num, email):
            return configs_store.get((str(num), email), "")

        def write_cfg(num, email, cfg):
            configs_store[(str(num), email)] = cfg

        def read_live():
            return live_state.get("creds", "")

        def write_live(creds):
            live_state["creds"] = creds

        patches = [
            patch.object(switcher, "_read_account_credentials", side_effect=read_creds),
            patch.object(
                switcher, "_read_account_credentials_ex", side_effect=read_creds_ex
            ),
            patch.object(switcher, "_write_account_credentials", side_effect=write_creds),
            patch.object(switcher, "_read_account_config", side_effect=read_cfg),
            patch.object(switcher, "_write_account_config", side_effect=write_cfg),
            patch.object(switcher, "_read_credentials", side_effect=read_live),
            patch.object(switcher, "_write_credentials", side_effect=write_live),
        ]
        for p in patches:
            p.start()
        return patches

    def test_switch_persists_rotated_refresh_token_to_backup(
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        """Regression: _perform_switch must persist refreshed credentials to backup.

        Prior to the fix, _perform_switch held the outer FileLock around
        list_accounts(). Inside list_accounts(), the persist closure tried to
        re-acquire the same file lock (different FD, so fcntl.flock is NOT
        re-entrant), spun to the 10s timeout, raised LockError, and the
        refreshed credentials were silently dropped at debug level. If
        Anthropic rotated the refresh token on that request, the backup
        retained the old (now-invalid) refresh token and the only recovery
        was a re-login.

        This test exercises the full _perform_switch path with account 2
        needing a refresh, and verifies the rotated refresh token actually
        landed on disk. Against main this fails; against the fix it passes.
        """
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        # The currently-active account 1's creds carry an expired expiresAt.
        # After the swap, account 1 becomes *inactive* and its just-backed-up
        # credentials are eligible for proactive refresh inside the
        # post-switch list_accounts() call. This is the scenario that
        # triggers the original deadlock bug.
        live_state = {"creds": json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-live-1",
                "refreshToken": "rt-orig-1",
                "expiresAt": 0,
                "scopes": ["user:profile"],
            },
        })}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )

        # Monkeypatch refresh_oauth_credentials to simulate a server-side
        # refresh-token rotation (rt-orig-1 -> rt-rotated-1).
        rotated_creds = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-rotated-1",
                "refreshToken": "rt-rotated-1",
                "expiresAt": 9_999_999_999_000,
                "scopes": ["user:profile"],
            },
        })

        try:
            with patch(
                # Patch the classifying base (refresh_oauth_credentials delegates
                # to it), so both the proactive and 401-retry paths see the
                # rotation regardless of which wrapper they call.
                "claude_swap.oauth.try_refresh_oauth_credentials",
                return_value=oauth.RefreshOutcome(rotated_creds, None),
            ), patch(
                "claude_swap.oauth.request_usage_data",
                return_value={
                    "five_hour": {"utilization": 12.0, "resets_at": None},
                    "seven_day": {"utilization": 34.0, "resets_at": None},
                },
            ), patch(
                # Account 1 has no stored backup, so the provenance guard
                # (issue #117) resolves the live credential's owner before
                # backing it into slot 1 — answer with slot 1's identity so
                # the capture proceeds as a legitimate own-credential backup.
                "claude_swap.oauth.fetch_oauth_profile",
                return_value={
                    "uuid": "uuid-1",
                    "email": "test@example.com",
                    "organizationUuid": "",
                },
            ):
                switcher._perform_switch("2")
        finally:
            for p in patches:
                p.stop()

        # After switch, backup for account 1 (now inactive) must contain the
        # rotated refresh token — confirming the persist inside list_accounts()
        # actually fired and didn't hit the lock deadlock.
        backup_after = creds_store.get(("1", "test@example.com"), "")
        assert backup_after, "backup credentials for account 1 are missing"
        backup_oauth = json.loads(backup_after)["claudeAiOauth"]
        assert backup_oauth["refreshToken"] == "rt-rotated-1", (
            f"Expected rotated refresh token on disk, got "
            f"{backup_oauth.get('refreshToken')!r} — lock deadlock regression"
        )
        assert backup_oauth["accessToken"] == "sk-rotated-1"

    def test_switch_refuses_to_overwrite_backup_with_empty_current_creds(
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        """A Keychain read that times out returns "" (not None); the switch must
        refuse to back up that empty credential over the departing account's
        good backup and fail instead — otherwise a transient Keychain hiccup
        destroys the stored credential. Regression for empty-backup cred loss."""
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        good_backup = json.dumps({
            "claudeAiOauth": {"accessToken": "sk-good-1", "refreshToken": "rt-good-1"},
        })
        creds_store[("1", "test@example.com")] = good_backup
        # Live read returns empty, exactly as a `security find-generic-password`
        # timeout does (Keychain fail → falls through to an absent file → "").
        live_state = {"creds": ""}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        try:
            with pytest.raises(CredentialReadError):
                switcher._perform_switch("2")
        finally:
            for p in patches:
                p.stop()
        # The departing account's good backup is untouched (not wiped to empty).
        assert creds_store[("1", "test@example.com")] == good_backup

    def test_switch_survives_post_display_failure(
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
        capsys,
    ):
        """Regression: a failure inside post-switch list_accounts() must not
        propagate as a switch failure. The swap already committed; the display
        is best-effort.
        """
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        live_state = {"creds": json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-live-1",
                "refreshToken": "rt-live-1",
            },
        })}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )

        # Pin platform so the post-switch followup message is deterministic
        # across hosts (macOS prints a different note).
        switcher.platform = Platform.LINUX

        try:
            with patch.object(
                switcher,
                "list_accounts",
                side_effect=RuntimeError("boom"),
            ):
                # Must not raise
                switcher._perform_switch("2")
        finally:
            for p in patches:
                p.stop()

        # Switch actually committed: sequence now points at account 2.
        data = switcher._get_sequence_data()
        assert data is not None
        assert data["activeAccountNumber"] == 2

        output = capsys.readouterr().out
        assert "Switched to" in output
        assert "usage display unavailable" in output
        assert "no restart needed" in output

    def test_switch_followup_macos(self, temp_home: Path, capsys):
        """macOS shows the ~30s cache note; a restart applies it instantly."""
        switcher = ClaudeAccountSwitcher()
        switcher.platform = Platform.MACOS

        switcher._print_switch_followup()

        out = capsys.readouterr().out
        assert "apply immediately" in out
        assert "30 seconds" in out
        assert "no restart needed" not in out

    def test_switch_followup_non_macos(self, temp_home: Path, capsys):
        """Linux/WSL/Windows show the immediate, no-restart note."""
        for plat in (Platform.LINUX, Platform.WSL, Platform.WINDOWS):
            switcher = ClaudeAccountSwitcher()
            switcher.platform = plat

            switcher._print_switch_followup()

            out = capsys.readouterr().out
            assert "no restart needed" in out, plat
            assert "30 seconds" not in out, plat

    def test_switch_with_unset_active_account_does_not_write_none_backup(
        self,
        temp_home: Path,
        mock_claude_config: Path,
    ):
        """purge -> add-token -> switch-to must not back up live creds as None."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, {
            "activeAccountNumber": None,
            "lastUpdated": "2024-01-01T00:00:00Z",
            "sequence": [1],
            "accounts": {
                "1": {
                    "email": "target@example.com",
                    "uuid": "",
                    "organizationUuid": "",
                    "organizationName": "",
                    "added": "2024-01-01T00:00:00Z",
                }
            },
        })
        creds_store = {
            ("1", "target@example.com"): json.dumps({
                "claudeAiOauth": {
                    "accessToken": "target-token",
                    "refreshToken": None,
                    "expiresAt": None,
                    "scopes": ["user:inference"],
                    "subscriptionType": None,
                    "rateLimitTier": None,
                }
            }),
        }
        configs_store = {
            ("1", "target@example.com"): json.dumps({
                "oauthAccount": {
                    "emailAddress": "target@example.com",
                    "accountUuid": "",
                    "organizationUuid": None,
                    "organizationName": None,
                }
            }),
        }
        live_state = {"creds": json.dumps({
            "claudeAiOauth": {
                "accessToken": "existing-live-token",
                "refreshToken": "existing-refresh",
            },
        })}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )

        try:
            switcher._perform_switch("1")
        finally:
            for p in patches:
                p.stop()

        assert not any(num == "None" for num, _ in creds_store)
        assert not any(num == "None" for num, _ in configs_store)
        assert json.loads(live_state["creds"])["claudeAiOauth"]["accessToken"] == (
            "target-token"
        )
        data = switcher._get_sequence_data()
        assert data["activeAccountNumber"] == 1

    def test_switch_uses_live_identity_for_current_backup_slot(
        self,
        temp_home: Path,
    ):
        """Do not trust stale activeAccountNumber when backing up live creds."""
        config_path = temp_home / ".claude.json"
        config_path.write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "realiti44@gmail.com",
                "accountUuid": "",
                "organizationUuid": None,
                "organizationName": None,
            }
        }))
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, {
            "activeAccountNumber": 3,
            "lastUpdated": "2024-01-01T00:00:00Z",
            "sequence": [3, 4],
            "accounts": {
                "3": {
                    "email": "onurcetinkol@gmail.com",
                    "uuid": "",
                    "organizationUuid": "",
                    "organizationName": "",
                    "added": "2024-01-01T00:00:00Z",
                },
                "4": {
                    "email": "realiti44@gmail.com",
                    "uuid": "",
                    "organizationUuid": "",
                    "organizationName": "",
                    "added": "2024-01-01T00:00:00Z",
                },
            },
        })
        target_creds = json.dumps({
            "claudeAiOauth": {
                "accessToken": "target-token",
                "refreshToken": "target-refresh",
            }
        })
        live_creds = json.dumps({
            "claudeAiOauth": {
                "accessToken": "realiti-live-token",
                "refreshToken": "realiti-live-refresh",
            }
        })
        creds_store = {
            ("3", "onurcetinkol@gmail.com"): target_creds,
            ("4", "realiti44@gmail.com"): "old-realiti-backup",
        }
        configs_store = {
            ("3", "onurcetinkol@gmail.com"): json.dumps({
                "oauthAccount": {
                    "emailAddress": "onurcetinkol@gmail.com",
                    "accountUuid": "",
                    "organizationUuid": None,
                    "organizationName": None,
                }
            }),
            ("4", "realiti44@gmail.com"): "old-realiti-config",
        }
        live_state = {"creds": live_creds}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )

        try:
            with patch.object(switcher, "list_accounts"), patch(
                # Slot 4's stored backup doesn't match the live bytes, so the
                # provenance guard resolves the live credential's owner —
                # answer with slot 4's identity so the backup is written as a
                # legitimate rotation of the live-identity slot.
                "claude_swap.oauth.fetch_oauth_profile",
                return_value={
                    "uuid": "",
                    "email": "realiti44@gmail.com",
                    "organizationUuid": "",
                },
            ):
                switcher._perform_switch("3")
        finally:
            for p in patches:
                p.stop()

        assert creds_store[("4", "realiti44@gmail.com")] == live_creds
        assert ("3", "realiti44@gmail.com") not in creds_store
        assert json.loads(live_state["creds"])["claudeAiOauth"]["accessToken"] == (
            "target-token"
        )

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="RLIMIT_FSIZE is the one fault that fails the write AND the "
               "restore; `resource` is POSIX-only, and no Windows API gives "
               "the same single-fault shape",
    )
    def test_a_rollback_does_not_truncate_a_destination_it_cannot_rewrite(
        self,
        temp_home: Path,
    ):
        """The rollback runs on failures where the write never opened the
        destination, and `Path.write_text` opens with O_TRUNC -- so the one
        fault that fails the write also destroys the intact original when
        the restore fails the same way.
        """
        import resource
        import signal

        from claude_swap.models import SwitchTransaction

        roster = temp_home / "sequence.json"
        original = json.dumps({
            "activeAccountNumber": 1,
            "accounts": {
                str(i): {"email": f"a{i}@example.com", "uuid": "",
                         "organizationUuid": "", "organizationName": "",
                         "added": "2024-01-01T00:00:00Z"}
                for i in range(1, 6)
            },
        }, indent=2)
        roster.write_text(original, encoding="utf-8")
        before = roster.stat().st_size

        class _Switcher:
            sequence_file = roster

            class _logger:
                @staticmethod
                def info(*_a, **_k):
                    pass

                @staticmethod
                def error(*_a, **_k):
                    pass

            def _write_credentials(self, *_a):
                pass

            def _get_sequence_data(self):
                return None

            def _write_json(self, *_a, **_k):
                raise AssertionError("the restore must not re-enter _write_json")

        config_path = temp_home / ".claude.json"
        config_path.write_text("{}", encoding="utf-8")
        tx = SwitchTransaction(
            original_credentials="", original_config="{}",
            original_account_num="1", original_email="a1@example.com",
            config_path=config_path, original_sequence=original,
        )
        tx.record_step("sequence_updated")

        # RESTORED, like its three siblings. A disposition is PROCESS-WIDE and
        # pytest runs the file in one process, so leaving SIGXFSZ at SIG_IGN
        # leaks into every later test in this worker -- a case that means to
        # be killed by the signal then sees an EFBIG it never asked for.
        old_sig = signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
        soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
        # PREMISE: a quota that cannot hold the roster, so the restore fails
        # for the same reason the write did.
        assert before // 2 < before
        resource.setrlimit(resource.RLIMIT_FSIZE, (before // 2, hard))
        try:
            tx.rollback(_Switcher())
        finally:
            resource.setrlimit(resource.RLIMIT_FSIZE, (soft, hard))
            signal.signal(signal.SIGXFSZ, old_sig)

        text = roster.read_text(encoding="utf-8")
        assert text == original, (
            "DEFECT: the rollback truncated a roster the failed write never "
            "touched. `_get_sequence_data` reads strictly, so a partial file "
            f"makes every later cswap invocation refuse to run ({before}B -> "
            f"{len(text)}B)"
        )
        assert not [p for p in temp_home.iterdir() if ".restore." in p.name], (
            "a failed restore must not strand its temp"
        )

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="a 0o500 parent and RLIMIT_FSIZE are POSIX shapes",
    )
    def test_a_restore_never_rewrites_a_destination_in_place(
        self,
        temp_home: Path,
    ) -> None:
        """A rename it cannot make is not a rewrite it may make.

        The only evidence available at that point is the errno from the
        TEMP, which is a fact about the PARENT DIRECTORY -- and the
        destination need not even be on the same filesystem. A rewrite
        authorised that way truncates a file whose own room nothing
        measured, which is the loss this helper exists to prevent.
        """
        import resource
        import signal

        dest = temp_home / "sub" / ".claude.json"
        dest.parent.mkdir()
        original = json.dumps({"projects": {f"/p/{i}": [1] * 20 for i in range(2500)}})
        dest.write_text(original, encoding="utf-8")
        cap = len(original) // 2
        # PREMISE: the parent refuses the temp, so the errno the helper sees
        # is EACCES -- nothing about the destination's own room.
        os.chmod(dest.parent, 0o500)
        old_sig = signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
        soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
        resource.setrlimit(resource.RLIMIT_FSIZE, (cap, hard))
        try:
            with pytest.raises(PermissionError):
                (dest.parent / "probe").touch()
            with pytest.raises(OSError) as caught:
                _restore_atomically(dest, original)
        finally:
            resource.setrlimit(resource.RLIMIT_FSIZE, (soft, hard))
            signal.signal(signal.SIGXFSZ, old_sig)
            os.chmod(dest.parent, 0o700)

        text = dest.read_text(encoding="utf-8")
        assert text == original, (
            "DEFECT: the restore rewrote a destination in place on the "
            "strength of an errno about a different filesystem, and the "
            f"rewrite ran out of room ({len(original)}B -> {len(text)}B)"
        )
        assert caught.value.errno == errno.EACCES, (
            "the refusal must carry the temp's own errno, not one from a "
            "rewrite it should never have attempted"
        )
        assert not [
            p for p in dest.parent.iterdir() if ".restore." in p.name
        ], "a refused restore must not strand its temp"

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="RLIMIT_FSIZE is POSIX-only",
    )
    def test_a_restore_refuses_rather_than_write_through_a_space_fault(
        self,
        temp_home: Path,
    ) -> None:
        """The write-through above must not swallow the fault it exists for.

        A full filesystem fails the rewrite too, and the rewrite truncates
        first — so on those errnos the intact destination is worth more than
        the attempt.
        """
        dest = temp_home / ".claude.json"
        original = json.dumps({"projects": {f"/p/{i}": [1] * 20 for i in range(2500)}})
        dest.write_text(original, encoding="utf-8")
        cap = len(original) // 2
        # PREMISE: the payload cannot fit under the cap, so the write faults.
        assert cap < len(original)

        import resource
        import signal

        old_sig = signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
        soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
        resource.setrlimit(resource.RLIMIT_FSIZE, (cap, hard))
        try:
            with pytest.raises(OSError) as caught:
                _restore_atomically(dest, original)
        finally:
            resource.setrlimit(resource.RLIMIT_FSIZE, (soft, hard))
            signal.signal(signal.SIGXFSZ, old_sig)

        assert caught.value.errno == errno.EFBIG
        assert dest.read_text(encoding="utf-8") == original, (
            "DEFECT: the write-through fallback truncated a destination the "
            f"failed write never touched ({len(original)}B -> "
            f"{dest.stat().st_size}B)"
        )

    def test_a_restore_refuses_a_publish_error_that_is_not_ebusy(
        self,
        temp_home: Path,
    ) -> None:
        """Only EBUSY authorises the in-place rewrite.

        EBUSY on the publish is a fact about the DESTINATION -- a bind-mounted
        file pins the inode, so no rename can ever land there and writing
        through is the only way. Every other publish errno says something
        else (an immutable destination answers EPERM), and rewriting on that
        reading opens the destination with O_TRUNC on nothing but a guess.
        Refusing costs the caller a restore it is told about; rewriting can
        cost it the file.

        The second half is the control: the same call, the same instrument,
        the one errno that DOES write through -- without it "no rewrite
        happened" is a claim the test has no power to make.
        """
        from claude_swap import models as models_mod

        dest = temp_home / ".claude.json"
        original = json.dumps({"userID": "u", "projects": {"/p": [1, 2, 3]}})
        dest.write_text(original, encoding="utf-8")

        def refuse(src, dst, *a, **k):
            raise OSError(errno.EPERM, "operation not permitted")

        with patch.object(models_mod, "replace_with_retry", side_effect=refuse):
            with pytest.raises(OSError) as caught:
                _restore_atomically(dest, original)

        assert caught.value.errno == errno.EPERM, (
            "DEFECT: a publish error that is not EBUSY was swallowed by an "
            "in-place rewrite, so the caller is told the restore landed when "
            f"nothing checked whether it could ({caught.value.errno})"
        )
        assert dest.read_text(encoding="utf-8") == original, (
            "the refused restore must leave the destination as it was"
        )

        # CONTROL: the one errno that IS about the destination. The rename
        # never succeeds under either patch, so a destination that comes back
        # holding `original` can only have been written through -- which is
        # what proves the case above measured a refusal and not an inert test.
        dest.write_text("", encoding="utf-8")

        def busy(src, dst, *a, **k):
            raise OSError(errno.EBUSY, "device or resource busy")

        with patch.object(models_mod, "replace_with_retry", side_effect=busy):
            _restore_atomically(dest, original)

        assert dest.read_text(encoding="utf-8") == original, (
            "premise: EBUSY must still write through, or the case above "
            "proves nothing about the narrowing"
        )

    def test_a_write_through_that_empties_the_roster_is_rolled_back(
        self,
        temp_home: Path,
    ):
        """The roster has the same exposure as the config, one write later.

        `_write_json`'s recovery empties the destination when the copy dies
        part-way and raises. A zero-byte `sequence.json` is not merely a lost
        active-account pointer: `_get_sequence_data` reads it strictly, so the
        next cswap invocation refuses to run at all.
        """
        from claude_swap import switcher as switcher_mod

        config_path = temp_home / ".claude.json"
        config_path.write_text(json.dumps({"oauthAccount": {
            "emailAddress": "current@example.com", "accountUuid": "",
            "organizationUuid": "", "organizationName": "",
        }}))
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        roster = {
            "activeAccountNumber": 2,
            "lastUpdated": "2024-01-01T00:00:00Z",
            "sequence": [1, 2],
            "accounts": {
                "1": {"email": "target@example.com", "uuid": "",
                      "organizationUuid": "", "organizationName": "",
                      "added": "2024-01-01T00:00:00Z"},
                "2": {"email": "current@example.com", "uuid": "",
                      "organizationUuid": "", "organizationName": "",
                      "added": "2024-01-01T00:00:00Z"},
            },
        }
        switcher._write_json(switcher.sequence_file, roster)
        creds_store = {
            ("1", "target@example.com"): json.dumps({
                "claudeAiOauth": {"accessToken": "t", "refreshToken": "r"}}),
            ("2", "current@example.com"): json.dumps({
                "claudeAiOauth": {"accessToken": "c", "refreshToken": "cr"}}),
        }
        configs_store = {
            ("1", "target@example.com"): json.dumps({"oauthAccount": {
                "emailAddress": "target@example.com", "accountUuid": "",
                "organizationUuid": "", "organizationName": "",
            }}),
        }
        live_state = {"creds": creds_store[("2", "current@example.com")]}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        real_copyfile = switcher_mod.shutil.copyfile
        calls = []

        def busy_replace(src, dst):
            if os.path.basename(str(dst)) == "sequence.json":
                calls.append("replace")
                raise OSError(errno.EBUSY, "device or resource busy")
            return switcher_mod.replace_with_retry.__wrapped__(src, dst) \
                if hasattr(switcher_mod.replace_with_retry, "__wrapped__") \
                else os.replace(src, dst)

        def dies_partway(source, dest, *a, **k):
            if os.path.basename(str(dest)) == "sequence.json":
                calls.append("copy")
                with open(dest, "wb") as handle:
                    handle.write(b"{par")
                raise OSError(errno.ENOSPC, "no space left on device")
            return real_copyfile(source, dest, *a, **k)

        try:
            with patch.object(
                switcher_mod, "replace_with_retry", side_effect=busy_replace,
            ), patch.object(
                switcher_mod.shutil, "copyfile", side_effect=dies_partway,
            ), pytest.raises(SwitchError):
                switcher._perform_switch("1")
        finally:
            for p in patches:
                p.stop()
            switcher_mod.shutil.copyfile = real_copyfile

        # PREMISE: the write-through really ran on the ROSTER, or this case
        # asserts the absence of damage nothing attempted.
        assert calls == ["replace", "copy"], (
            f"premise: the roster's write-through must run, got {calls}"
        )

        text = switcher.sequence_file.read_text(encoding="utf-8")
        assert text.strip(), (
            "DEFECT: the roster was emptied and nothing restored it. Every "
            "slot's email, uuid and org are gone, and `_get_sequence_data` "
            "reads strictly, so the next cswap invocation refuses to run."
        )
        back = json.loads(text)
        assert back["accounts"] == roster["accounts"], (
            "the roster survived but its accounts did not"
        )

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="RLIMIT_FSIZE is POSIX-only",
    )
    def test_the_transaction_rollback_does_not_truncate_an_intact_config(
        self,
        temp_home: Path,
        caplog,
    ):
        """The transaction's config arm, on a destination the write never
        opened.

        `record_step("config_written")` is armed BEFORE the write, so the arm
        runs on faults where `_write_json`'s temp write failed and
        `~/.claude.json` still holds the intact original. `Path.write_text`
        opens with O_TRUNC, so restoring that way destroys the very bytes it
        is holding when the rewrite hits the same fault.

        The direct-activation path has this witness; the transaction path is
        the sibling that did not.
        """
        import resource
        import signal

        config_path = temp_home / ".claude.json"
        config_path.write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "current@example.com", "accountUuid": "",
                "organizationUuid": "", "organizationName": "",
            },
            "userID": "user-abcdef",
            "mcpServers": {"a": {"command": "x"}},
            "projects": {
                f"/p/{i}": {"allowedTools": ["Bash", "Read"] * 8}
                for i in range(700)
            },
        }))
        original = config_path.read_text(encoding="utf-8")
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, {
            "activeAccountNumber": 2,
            "lastUpdated": "2024-01-01T00:00:00Z",
            "sequence": [1, 2],
            "accounts": {
                n: {"email": f"{w}@example.com", "uuid": f"u{n}",
                    "organizationUuid": "", "organizationName": "",
                    "added": "2024-01-01T00:00:00Z"}
                for n, w in (("1", "target"), ("2", "current"))
            },
        })
        for n, w in (("1", "target"), ("2", "current")):
            switcher._write_account_credentials(n, f"{w}@example.com", json.dumps(
                {"claudeAiOauth": {"accessToken": f"t-{w}", "refreshToken": "r"}}))
            switcher._write_account_config(n, f"{w}@example.com", json.dumps(
                {"oauthAccount": {"emailAddress": f"{w}@example.com",
                                  "accountUuid": f"u{n}"}}))
        (temp_home / ".claude" / ".credentials.json").write_text(json.dumps(
            {"claudeAiOauth": {"accessToken": "t-current", "refreshToken": "r"}}))

        cap = 100_000
        # PREMISE: the config cannot fit under the cap, so the write faults.
        assert len(original) > cap

        old_sig = signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
        soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
        resource.setrlimit(resource.RLIMIT_FSIZE, (cap, hard))
        try:
            # The backup step writes this SAME oversized config to the slot
            # store, which the cap also refuses -- that raise lands before any
            # step is recorded, so the rollback would never run and this case
            # would certify a branch it never entered.
            with patch.object(switcher, "_write_account_config"), patch.object(
                switcher, "list_accounts"
            ), pytest.raises(SwitchError):
                switcher._perform_switch("1", emit_output=False)
        finally:
            resource.setrlimit(resource.RLIMIT_FSIZE, (soft, hard))
            signal.signal(signal.SIGXFSZ, old_sig)

        # PREMISE: the arm really ran, or this asserts the absence of damage
        # nothing attempted. The step NAME, not its outcome -- the rollback
        # logs "Rolled back step" or "Failed to rollback step" for the same
        # step, and a restore that truncates takes the second, so keying on
        # success would fail the premise on exactly the defect under test.
        assert "config_written" in caplog.text, (
            f"premise: the config arm must run in the rollback, got "
            f"{caplog.text!r}"
        )
        text = config_path.read_text(encoding="utf-8")
        assert text == original, (
            "DEFECT: the transaction rollback truncated the config the failed "
            f"write never touched ({len(original)}B -> {len(text)}B) -- "
            "`oauthAccount`, `userID`, `mcpServers` and `projects` are gone"
        )

    def test_a_write_through_that_empties_the_config_is_rolled_back(
        self,
        temp_home: Path,
    ):
        """A destination that refuses the rename makes `_write_json` copy
        THROUGH it, and a copy that dies part-way empties it -- correctly,
        a partial credential is worse than none -- and then raises.

        The rollback token must already be armed at that point, or the
        caller skips a restore whose bytes it is holding and tells the user
        the switch "was rolled back" over an emptied config.
        """
        config_path = temp_home / ".claude.json"
        original_config_text = json.dumps({
            "oauthAccount": {
                "emailAddress": "current@example.com",
                "accountUuid": "",
                "organizationUuid": "",
                "organizationName": "",
            },
            "projects": {"/some/repo": {"allowedTools": []}},
        })
        config_path.write_text(original_config_text)
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, {
            "activeAccountNumber": 2,
            "lastUpdated": "2024-01-01T00:00:00Z",
            "sequence": [1, 2],
            "accounts": {
                "1": {
                    "email": "target@example.com",
                    "uuid": "",
                    "organizationUuid": "",
                    "organizationName": "",
                    "added": "2024-01-01T00:00:00Z",
                },
                "2": {
                    "email": "current@example.com",
                    "uuid": "",
                    "organizationUuid": "",
                    "organizationName": "",
                    "added": "2024-01-01T00:00:00Z",
                },
            },
        })
        creds_store = {
            ("1", "target@example.com"): json.dumps({
                "claudeAiOauth": {"accessToken": "t", "refreshToken": "r"}}),
            ("2", "current@example.com"): json.dumps({
                "claudeAiOauth": {"accessToken": "c", "refreshToken": "cr"}}),
        }
        configs_store = {
            ("1", "target@example.com"): json.dumps({
                "oauthAccount": {
                    "emailAddress": "target@example.com",
                    "accountUuid": "",
                    "organizationUuid": "",
                    "organizationName": "",
                }
            }),
        }
        live_state = {"creds": creds_store[("2", "current@example.com")]}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )

        from claude_swap import switcher as switcher_mod

        real_copyfile = switcher_mod.shutil.copyfile

        calls = []

        import claude_swap.fsutil as fsutil_mod

        real_replace = fsutil_mod.os.replace

        def busy_replace(src, dst, *a, **k):
            # AT THE SHARED CALLEE, not at one module's binding. `models`
            # holds its own `from claude_swap.fsutil import
            # replace_with_retry`, so patching switcher's name leaves the
            # RESTORE's rename real -- it succeeds, and this case then
            # certifies a branch it never entered.
            if os.path.basename(os.fspath(dst)) == ".claude.json":
                calls.append(("replace", str(dst)))
                raise OSError(errno.EBUSY, "device or resource busy")
            return real_replace(src, dst, *a, **k)

        def dies_partway(source, dest, *a, **k):
            # `copyfile` opens the destination 'wb' before the first source
            # byte, so the destination is already empty when this raises.
            calls.append(("copy", str(dest)))
            with open(dest, "wb") as handle:
                handle.write(b"{par")
            raise OSError(errno.ENOSPC, "no space left on device")

        try:
            with patch.object(
                fsutil_mod.os, "replace", side_effect=busy_replace,
            ), patch.object(
                switcher_mod.shutil, "copyfile", side_effect=dies_partway,
            ), pytest.raises(SwitchError) as excinfo:
                switcher._perform_switch("1")
        finally:
            for p in patches:
                p.stop()
            switcher_mod.shutil.copyfile = real_copyfile

        # PREMISES: the write-through really ran, or this case asserts the
        # absence of damage nothing attempted.
        assert [c[0] for c in calls] == ["replace", "copy", "replace"], (
            "premise: the write's EBUSY, its write-through, AND the "
            f"restore's own rename must all run, got {calls}"
        )
        assert "may now be truncated" in str(excinfo.value), (
            "premise: the failure must be the write-through's own"
        )
        assert config_path.read_text() != "{par", (
            "premise: the partial must not survive -- the recovery empties it"
        )
        assert config_path.read_text() == original_config_text, (
            "DEFECT: the write-through emptied the config and raised, and "
            "the rollback did not restore it -- the caller's token is armed "
            "only after the write returns, so a failure DURING the write "
            "leaves it False while the original bytes sit in memory. The "
            "user is told the switch was rolled back."
        )

    def test_direct_activation_rolls_back_live_creds_on_sequence_write_failure(
        self,
        temp_home: Path,
    ):
        """Live creds must be restored if a write fails after they were swapped."""
        config_path = temp_home / ".claude.json"
        original_config_text = json.dumps({
            "oauthAccount": {
                "emailAddress": "untracked@example.com",
                "accountUuid": "",
                "organizationUuid": None,
                "organizationName": None,
            }
        })
        config_path.write_text(original_config_text)
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, {
            "activeAccountNumber": None,
            "lastUpdated": "2024-01-01T00:00:00Z",
            "sequence": [1],
            "accounts": {
                "1": {
                    "email": "target@example.com",
                    "uuid": "",
                    "organizationUuid": "",
                    "organizationName": "",
                    "added": "2024-01-01T00:00:00Z",
                }
            },
        })
        original_live_creds = json.dumps({
            "claudeAiOauth": {
                "accessToken": "live-untracked-token",
                "refreshToken": "live-untracked-refresh",
            }
        })
        creds_store = {
            ("1", "target@example.com"): json.dumps({
                "claudeAiOauth": {
                    "accessToken": "target-token",
                    "refreshToken": "target-refresh",
                }
            }),
        }
        configs_store = {
            ("1", "target@example.com"): json.dumps({
                "oauthAccount": {
                    "emailAddress": "target@example.com",
                    "accountUuid": "",
                    "organizationUuid": None,
                    "organizationName": None,
                }
            }),
        }
        live_state = {"creds": original_live_creds}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )

        original_write_json = switcher._write_json

        def failing_write_json(path, data):
            if path == switcher.sequence_file and data.get(
                "activeAccountNumber"
            ) == 1:
                raise OSError("disk full")
            return original_write_json(path, data)

        try:
            with patch.object(
                switcher, "_write_json", side_effect=failing_write_json,
            ), pytest.raises(SwitchError, match=r"was rolled back.*disk full"):
                switcher._perform_switch("1")
        finally:
            for p in patches:
                p.stop()

        assert live_state["creds"] == original_live_creds
        assert config_path.read_text() == original_config_text

    def test_direct_activation_fails_fast_when_live_creds_unreadable(
        self,
        temp_home: Path,
    ):
        """Refuse to overwrite live creds we couldn't snapshot for rollback."""
        config_path = temp_home / ".claude.json"
        original_config_text = json.dumps({
            "oauthAccount": {
                "emailAddress": "untracked@example.com",
                "accountUuid": "",
                "organizationUuid": None,
                "organizationName": None,
            }
        })
        config_path.write_text(original_config_text)
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, {
            "activeAccountNumber": None,
            "lastUpdated": "2024-01-01T00:00:00Z",
            "sequence": [1],
            "accounts": {
                "1": {
                    "email": "target@example.com",
                    "uuid": "",
                    "organizationUuid": "",
                    "organizationName": "",
                    "added": "2024-01-01T00:00:00Z",
                }
            },
        })
        creds_store = {
            ("1", "target@example.com"): json.dumps({
                "claudeAiOauth": {
                    "accessToken": "target-token",
                    "refreshToken": "target-refresh",
                }
            }),
        }
        configs_store = {
            ("1", "target@example.com"): json.dumps({
                "oauthAccount": {
                    "emailAddress": "target@example.com",
                    "accountUuid": "",
                    "organizationUuid": None,
                    "organizationName": None,
                }
            }),
        }
        live_state = {"creds": "live-creds-that-we-cannot-read"}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )

        try:
            with patch.object(
                switcher, "_read_credentials", return_value=None,
            ), pytest.raises(CredentialReadError, match="snapshot"):
                switcher._perform_switch("1")
        finally:
            for p in patches:
                p.stop()

        assert live_state["creds"] == "live-creds-that-we-cannot-read"
        assert config_path.read_text() == original_config_text


class TestSwitchToSelfSlotAndForce:
    """Issue #79: --switch-to onto the active account must not back up the
    live credentials into the target slot (destroying a freshly imported
    backup); --force is the explicit stored-backup → live recovery path."""

    _install_store_patches = staticmethod(
        TestPerformSwitchPostDisplay._install_store_patches
    )

    IMPORTED_1 = json.dumps({
        "claudeAiOauth": {
            "accessToken": "sk-imported-1",
            "refreshToken": "rt-imported-1",
        },
    })
    LIVE_1 = json.dumps({
        "claudeAiOauth": {
            "accessToken": "sk-live-1",
            "refreshToken": "rt-live-1",
        },
    })

    def _post_import_state(self, temp_home, sample_sequence_data):
        """Accounts 1 (active, live) & 2, with slot 1's stored backup holding
        freshly imported credentials that differ from the (stale) live ones."""
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher.platform = Platform.LINUX
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        (temp_home / ".claude" / ".credentials.json").write_text(self.LIVE_1)

        creds_store = {
            ("1", "test@example.com"): self.IMPORTED_1,
            ("2", "account2@example.com"): json.dumps({
                "claudeAiOauth": {
                    "accessToken": "sk-2",
                    "refreshToken": "rt-2",
                },
            }),
        }
        configs_store = {
            ("1", "test@example.com"): json.dumps({
                "oauthAccount": {
                    "emailAddress": "test@example.com",
                    "accountUuid": "test-uuid-1234",
                },
            }),
            ("2", "account2@example.com"): json.dumps({
                "oauthAccount": {
                    "emailAddress": "account2@example.com",
                    "accountUuid": "uuid-2",
                },
            }),
        }
        live_state = {"creds": self.LIVE_1}
        return switcher, creds_store, configs_store, live_state

    def test_switch_to_current_slot_is_noop_preserving_backup(
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
        capsys,
    ):
        """Human-mode self-switch neither poisons the stored backup nor
        rewrites the live credentials. Against main this fails: the switch
        backed up the live creds into slot 1 before reading them back."""
        switcher, creds, configs, live = self._post_import_state(
            temp_home, sample_sequence_data,
        )
        patches = self._install_store_patches(switcher, creds, configs, live)
        try:
            result = switcher.switch_to("1")
        finally:
            for p in patches:
                p.stop()

        assert result is None
        assert creds[("1", "test@example.com")] == self.IMPORTED_1
        assert live["creds"] == self.LIVE_1
        out = capsys.readouterr().out
        assert "Already on" in out and "Account-1" in out
        assert "cswap --switch-to 1 --force" in out

    def test_force_self_activation_restores_imported_creds(
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
        capsys,
    ):
        """--switch-to 1 --force rewrites the live login from the stored
        backup without backing up the stale live creds first."""
        switcher, creds, configs, live = self._post_import_state(
            temp_home, sample_sequence_data,
        )
        patches = self._install_store_patches(switcher, creds, configs, live)
        try:
            result = switcher.switch_to("1", force=True)
        finally:
            for p in patches:
                p.stop()

        assert result is None
        assert live["creds"] == self.IMPORTED_1
        assert creds[("1", "test@example.com")] == self.IMPORTED_1
        data = switcher._get_sequence_data()
        assert data["activeAccountNumber"] == 1
        assert "Activated" in capsys.readouterr().out

    def test_force_cross_slot_skips_backup_of_current(
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        """--switch-to 2 --force lands on account 2 without writing the stale
        live creds into slot 1's freshly imported backup."""
        switcher, creds, configs, live = self._post_import_state(
            temp_home, sample_sequence_data,
        )
        patches = self._install_store_patches(switcher, creds, configs, live)
        try:
            switcher.switch_to("2", force=True)
        finally:
            for p in patches:
                p.stop()

        assert creds[("1", "test@example.com")] == self.IMPORTED_1
        assert json.loads(live["creds"])["claudeAiOauth"]["accessToken"] == "sk-2"
        data = switcher._get_sequence_data()
        assert data["activeAccountNumber"] == 2


# ── Task 1: AccountInfo org fields ───────────────────────────────────────────

class TestAccountInfoOrgFields:
    def test_account_info_includes_org_fields(self):
        """AccountInfo should store organization UUID and name."""
        from claude_swap.models import AccountInfo
        info = AccountInfo(
            email="user@example.com",
            uuid="user-uuid",
            organization_uuid="org-uuid-123",
            organization_name="Acme Corp",
            added="2024-01-01T00:00:00Z",
            number=1,
        )
        assert info.organization_uuid == "org-uuid-123"
        assert info.organization_name == "Acme Corp"

    def test_account_info_personal_account_has_empty_org(self):
        """Personal accounts should have empty string for organization fields."""
        from claude_swap.models import AccountInfo
        info = AccountInfo.from_dict(1, {
            "email": "user@example.com",
            "uuid": "user-uuid",
            "added": "2024-01-01T00:00:00Z",
        })
        assert info.organization_uuid == ""
        assert info.organization_name == ""

    def test_account_info_to_dict_includes_org_fields(self):
        """to_dict() should include organization fields."""
        from claude_swap.models import AccountInfo
        info = AccountInfo(
            email="user@example.com",
            uuid="user-uuid",
            organization_uuid="org-uuid",
            organization_name="Acme",
            added="2024-01-01T00:00:00Z",
            number=1,
        )
        d = info.to_dict()
        assert d["organizationUuid"] == "org-uuid"
        assert d["organizationName"] == "Acme"

    def test_account_info_is_organization_property(self):
        """is_organization should be determined by organizationUuid presence."""
        from claude_swap.models import AccountInfo
        org = AccountInfo.from_dict(1, {"email": "u@e.com", "uuid": "u", "added": "", "organizationUuid": "o"})
        personal = AccountInfo.from_dict(2, {"email": "u@e.com", "uuid": "u", "added": ""})
        assert org.is_organization is True
        assert personal.is_organization is False

    def test_account_info_display_label(self):
        """display_label should include org name or personal tag."""
        from claude_swap.models import AccountInfo
        org = AccountInfo(email="u@e.com", uuid="u", organization_uuid="o",
                          organization_name="Acme", added="", number=1)
        personal = AccountInfo(email="u@e.com", uuid="u", organization_uuid="",
                               organization_name="", added="", number=2)
        assert org.display_label == "u@e.com [Acme]"
        assert personal.display_label == "u@e.com [personal]"


# ── Task 3: _account_exists composite key ────────────────────────────────────

class TestAccountExistsCompositeKey:
    def test_distinguishes_org_and_personal(self, temp_home, mock_credentials_file):
        """Accounts with same email but different organizationUuid should be treated as distinct."""
        from claude_swap.switcher import ClaudeAccountSwitcher
        backup_dir = get_backup_root()
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / "sequence.json").write_text(json.dumps({
            "activeAccountNumber": 1,
            "lastUpdated": "2024-01-01T00:00:00Z",
            "sequence": [1],
            "accounts": {
                "1": {
                    "email": "user@example.com",
                    "uuid": "user-uuid",
                    "organizationUuid": "org-uuid-A",
                    "organizationName": "Acme",
                    "added": "2024-01-01T00:00:00Z",
                }
            },
        }))
        switcher = ClaudeAccountSwitcher()
        assert switcher._account_exists("user@example.com", "org-uuid-A") is True
        assert switcher._account_exists("user@example.com", "") is False
        assert switcher._account_exists("user@example.com", "org-uuid-B") is False


# ── Task 4: _get_current_account returns tuple ───────────────────────────────

class TestGetCurrentAccountOrgSupport:
    def test_returns_org_info(self, temp_home, mock_org_claude_config):
        """_get_current_account should return (email, organization_uuid) tuple."""
        from claude_swap.switcher import ClaudeAccountSwitcher
        switcher = ClaudeAccountSwitcher()
        result = switcher._get_current_account()
        assert result == ("user@example.com", "org-uuid-5678")

    def test_returns_empty_org_for_personal(self, temp_home, mock_personal_claude_config):
        """Personal account should return tuple with empty string for organization_uuid."""
        from claude_swap.switcher import ClaudeAccountSwitcher
        switcher = ClaudeAccountSwitcher()
        result = switcher._get_current_account()
        assert result == ("user@example.com", "")

    def test_returns_none_when_no_config(self, temp_home):
        """Should return None when config file does not exist."""
        from claude_swap.switcher import ClaudeAccountSwitcher
        switcher = ClaudeAccountSwitcher()
        result = switcher._get_current_account()
        assert result is None


# ── Task 5: add_account with org fields ──────────────────────────────────────

class TestDeadTokenQuarantine:
    """A dead refresh-token account is surfaced as re-login-needed and not fetched."""

    def _dead_creds(self):
        return json.dumps({"claudeAiOauth": {
            "accessToken": "at", "refreshToken": "rt", "expiresAt": 1,
        }})

    def _make_dead(self, switcher, num="2", identity=("test@example.com", "")):
        store = switcher._usage_store
        from claude_swap.usage_store import FetchRecord
        store.record({num: FetchRecord(error="invalid_grant")}, {num: identity})

    def test_collector_surfaces_relogin_sentinel_and_skips_fetch(self, temp_home):
        from claude_swap.json_output import USAGE_RELOGIN_REQUIRED
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        self._make_dead(switcher)
        info = [(2, "test@example.com", "Org", "", False, self._dead_creds(), "")]

        with patch("claude_swap.oauth.try_fetch_usage_for_account") as fetch:
            entries = switcher._collect_usage_entries(info)

        assert entries["2"].sentinel == USAGE_RELOGIN_REQUIRED
        fetch.assert_not_called()  # quarantined: no endless 401/429 loop

    def test_relogin_surfaces_same_pass_on_invalid_grant(self, temp_home):
        # A fetch that returns invalid_grant crosses the dead threshold this pass;
        # the pre-fetch quarantine scan couldn't see it, so the collector must
        # still render "re-login needed" now, not only on the next refresh.
        from claude_swap.json_output import USAGE_RELOGIN_REQUIRED
        from claude_swap.usage_store import FetchRecord
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        info = [(2, "test@example.com", "Org", "", False, self._dead_creds(), "")]

        with patch.object(
            switcher, "_run_usage_fetches",
            return_value={"2": FetchRecord(error="invalid_grant")},
        ) as run:
            entries = switcher._collect_usage_entries(info)

        run.assert_called_once()  # it was fetch-eligible, not pre-quarantined
        assert entries["2"].sentinel == USAGE_RELOGIN_REQUIRED

    def test_the_collector_hands_the_trust_bound_its_configured_models(
        self, temp_home
    ):
        """`models=models` at the record call site, not the default `()`.

        `entries()` honours the configured per-model windows when it bounds
        429 trust, so a scoped window that ENDS the trust must be visible
        wherever that trust is computed. Defaulted to `()` the window is
        invisible and the row keeps serving data its own reset already killed.

        Store-level tests pass `models=` themselves, so they hold with the
        wiring cut. Measured: mutating this call site to `models=()` left the
        whole suite green. This drives `_collect_usage_entries`, the only path
        production reaches.

        Unscoped windows sit far out; the Fable window resets in 30 minutes.
        Asserted on what the row SERVES, not on the wait: the wait is the
        server's deadline plus the margin either way. An earlier revision
        trimmed it back to the deadline when the trust expired first, which was
        measured wrong — see `test_the_trust_bound_never_shortens_a_429_wait`.
        """
        from datetime import datetime, timezone

        from claude_swap.usage_store import SERVE_TTL_S, FetchRecord

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._poll_inputs_override = (90.0, ("Fable",))
        store = switcher._usage_store
        now = time.time()

        def iso(ahead):
            return (
                datetime.fromtimestamp(now + ahead, tz=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )

        live = json.dumps({"claudeAiOauth": {
            "accessToken": "at", "refreshToken": "rt",
            "expiresAt": (now + 86400) * 1000,
        }})
        info = [(2, "test@example.com", "Org", "", False, live, "")]
        ident = {"2": ("test@example.com", "")}
        store.record({"2": FetchRecord(usage={
            "five_hour": {"pct": 25.0, "resets_at": iso(100 * 3600.0)},
            "seven_day": {"pct": 10.0, "resets_at": iso(100 * 3600.0)},
            "scoped": [{"name": "Fable", "pct": 90.0, "resets_at": iso(1800.0)}],
        })}, ident)
        # Age the row past the serve TTL so it is fetch-eligible, without
        # advancing the clock (which would move the scoped reset with it).
        with store.path.open() as fh:
            table = json.load(fh)
        table["accounts"]["2"]["fetchedAt"] -= SERVE_TTL_S * 4
        store.path.write_text(json.dumps(table))

        with patch.object(
            switcher, "_run_usage_fetches",
            return_value={"2": FetchRecord(error="http-429", retry_after_s=3600.0)},
        ) as run:
            switcher._collect_usage_entries(info, fetch={"2"})
        run.assert_called_once()   # premise: the 429 actually went through record()

        assert store.entries(ident, ("Fable",))["2"].backoff_until is not None, (
            "premise: a backoff was recorded"
        )
        # Move the scoped window's reset into the PAST — the state a real
        # clock reaches 30 minutes later — while the unscoped windows stay far
        # ahead. Only a bound that sees the configured models ends the trust.
        with store.path.open() as fh:
            table = json.load(fh)
        row = table["accounts"]["2"]
        row["lastGood"]["scoped"][0]["resets_at"] = iso(-60.0 / 3600.0)
        row["fetchedAt"] -= 1860.0
        store.path.write_text(json.dumps(table))

        # BOTH RETURN PATHS, and the FETCHING one first. `_collect_usage_entries`
        # has two `store.entries(identities, models)` sites: :3520 on the
        # no-fetch path and :3581 on the post-fetch re-read. On a tick that
        # actually fetched — every 429 tick, which is this one — :3520's value
        # is discarded and :3581 is what comes back. Asserting only on a
        # follow-up `fetch=set()` call covered the line that does NOT run:
        # measured, mutating :3581 alone left the whole suite green.
        #
        # `record()` is what makes this hard to reach. `models` only changes an
        # answer while `lastError == "http-429"`, and any second fetch rewrites
        # that row — a usage payload clears it, a fresh 429 resets `fetchedAt`
        # so the window is no longer past. Four earlier revisions of this
        # assertion each passed with the wiring cut, each for a different one
        # of those reasons.
        #
        # BACKOFF MUST BE PAST, or `reserve` refuses and `if claims:` never
        # opens — measured, `claims={}` on the second call and :3581 was
        # unreachable through six earlier shapes of this assertion. The row's
        # own backoff is cleared here rather than by advancing a clock the
        # store does not share with the test.
        with store.path.open() as fh:
            table = json.load(fh)
        table["accounts"]["2"]["backoffUntil"] = None
        store.path.write_text(json.dumps(table))

        # And the fetch answers with NOTHING: the slot is claimed so the block
        # opens and :3581 runs, but `record()` gets an empty `records`, touches
        # no row, and the re-read sees exactly the state staged above.
        with patch.object(
            switcher, "_run_usage_fetches", return_value={},
        ):
            fetched = switcher._collect_usage_entries(info, fetch={"2"})["2"]
        assert fetched.decision_value() is None, (
            f"the FETCHING path returned a row still serving last_good "
            f"(trust_extended={fetched.trust_extended}) although its scoped "
            "window has reset — :3581 never got the configured models"
        )

        returned = switcher._collect_usage_entries(info, fetch=set())["2"]
        assert returned.decision_value() is None, (
            f"the collector RETURNED a row still serving last_good "
            f"(trust_extended={returned.trust_extended}) although its scoped "
            "window has reset — it never handed its configured models to the "
            "bound"
        )

    def test_readd_clears_quarantine(self, temp_home):
        # Re-adding an account (fresh credential) must lift the quarantine, so
        # the disabled fetches don't leave it stuck at "re-login needed" forever.
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        identity = ("user@example.com", "org-A")
        self._make_dead(switcher, num="1", identity=identity)
        assert switcher._usage_store.entries({"1": identity})["1"].token_dead()

        fake_creds = json.dumps({"claudeAiOauth": {"accessToken": "fresh"}})
        config_path = temp_home / ".claude.json"
        config_path.write_text(json.dumps({"oauthAccount": {
            "emailAddress": "user@example.com", "accountUuid": "u",
            "organizationUuid": "org-A", "organizationName": "Acme",
        }}))
        with patch.object(switcher, "_read_active_credentials", return_value=ActiveCredentials(fake_creds, False)), \
             patch.object(switcher, "_write_account_credentials"):
            switcher.add_account()

        assert not switcher._usage_store.entries({"1": identity})["1"].token_dead()


class TestAddAccountOrgFields:
    def test_allows_same_email_different_org(self, temp_home):
        """Should allow adding same-email account if organizationUuid differs."""
        from claude_swap.switcher import ClaudeAccountSwitcher

        fake_creds = json.dumps({"claudeAiOauth": {"accessToken": "test-token"}})
        config_path = temp_home / ".claude.json"

        config_path.write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "user@example.com",
                "accountUuid": "user-uuid",
                "organizationUuid": "org-uuid-A",
                "organizationName": "Acme",
            }
        }))
        switcher = ClaudeAccountSwitcher()
        with patch.object(switcher, "_read_active_credentials", return_value=ActiveCredentials(fake_creds, False)), \
             patch.object(switcher, "_write_account_credentials"):
            switcher.add_account()

        config_path.write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "user@example.com",
                "accountUuid": "user-uuid",
            }
        }))
        with patch.object(switcher, "_read_active_credentials", return_value=ActiveCredentials(fake_creds, False)), \
             patch.object(switcher, "_write_account_credentials"):
            switcher.add_account()

        seq = json.loads((get_backup_root() / "sequence.json").read_text())
        assert len(seq["accounts"]) == 2
        assert seq["accounts"]["1"]["organizationUuid"] == "org-uuid-A"
        assert seq["accounts"]["2"]["organizationUuid"] == ""

    def test_blocks_true_duplicate(self, temp_home):
        """Should block adding an account with identical (email, organizationUuid) combination."""
        from claude_swap.switcher import ClaudeAccountSwitcher

        fake_creds = json.dumps({"claudeAiOauth": {"accessToken": "test-token"}})
        config_path = temp_home / ".claude.json"
        org_config = {
            "oauthAccount": {
                "emailAddress": "user@example.com",
                "accountUuid": "user-uuid",
                "organizationUuid": "org-uuid-A",
                "organizationName": "Acme",
            }
        }
        config_path.write_text(json.dumps(org_config))
        switcher = ClaudeAccountSwitcher()
        with patch.object(switcher, "_read_active_credentials", return_value=ActiveCredentials(fake_creds, False)), \
             patch.object(switcher, "_write_account_credentials"):
            switcher.add_account()

        import io
        from contextlib import redirect_stdout
        f = io.StringIO()
        config_path.write_text(json.dumps(org_config))
        with redirect_stdout(f), \
             patch.object(switcher, "_read_active_credentials", return_value=ActiveCredentials(fake_creds, False)), \
             patch.object(switcher, "_write_account_credentials"):
            switcher.add_account()
        assert "Updated credentials" in f.getvalue()

        seq = json.loads((get_backup_root() / "sequence.json").read_text())
        assert len(seq["accounts"]) == 1

    def test_stores_org_name_in_sequence(self, temp_home):
        """add_account should store organizationName in sequence.json."""
        from claude_swap.switcher import ClaudeAccountSwitcher

        fake_creds = json.dumps({"claudeAiOauth": {"accessToken": "test-token"}})
        config_path = temp_home / ".claude.json"
        config_path.write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "user@example.com",
                "accountUuid": "user-uuid",
                "organizationUuid": "org-uuid",
                "organizationName": "My Org",
            }
        }))
        switcher = ClaudeAccountSwitcher()
        with patch.object(switcher, "_read_active_credentials", return_value=ActiveCredentials(fake_creds, False)), \
             patch.object(switcher, "_write_account_credentials"):
            switcher.add_account()

        seq = json.loads((get_backup_root() / "sequence.json").read_text())
        assert seq["accounts"]["1"]["organizationName"] == "My Org"
        assert seq["accounts"]["1"]["organizationUuid"] == "org-uuid"


# ── Task 6: _resolve_account_identifier ambiguity ────────────────────────────

class TestResolveIdentifierAmbiguity:
    def test_by_number_always_works(self, temp_home, sample_sequence_data_with_org):
        """Account number identifier should always resolve correctly."""
        from claude_swap.switcher import ClaudeAccountSwitcher
        backup_dir = get_backup_root()
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / "sequence.json").write_text(json.dumps(sample_sequence_data_with_org))
        switcher = ClaudeAccountSwitcher()
        assert switcher._resolve_account_identifier("1") == "1"
        assert switcher._resolve_account_identifier("2") == "2"

    def test_raises_on_ambiguous_email(self, temp_home, sample_sequence_data_with_org):
        """Should raise ConfigError when email matches multiple accounts."""
        from claude_swap.switcher import ClaudeAccountSwitcher
        from claude_swap.exceptions import ConfigError
        backup_dir = get_backup_root()
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / "sequence.json").write_text(json.dumps(sample_sequence_data_with_org))
        switcher = ClaudeAccountSwitcher()
        with pytest.raises(ConfigError, match="ambiguous"):
            switcher._resolve_account_identifier("user@example.com")

    def test_unique_email_still_works(self, temp_home, sample_sequence_data):
        """Unique email should still resolve to the correct account number."""
        from claude_swap.switcher import ClaudeAccountSwitcher
        backup_dir = get_backup_root()
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / "sequence.json").write_text(json.dumps(sample_sequence_data))
        switcher = ClaudeAccountSwitcher()
        assert switcher._resolve_account_identifier("account1@example.com") == "1"


# ── Task 7: list_accounts org display ────────────────────────────────────────

class TestListAccountsOrgDisplay:
    def test_shows_org_name_and_personal(self, temp_home, mock_credentials_file,
                                         sample_sequence_data_with_org, capsys):
        """list_accounts should display org name and personal tag."""
        from claude_swap.switcher import ClaudeAccountSwitcher
        from unittest.mock import patch

        backup_dir = get_backup_root()
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / "sequence.json").write_text(json.dumps(sample_sequence_data_with_org))

        config_path = temp_home / ".claude.json"
        config_path.write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "user@example.com",
                "accountUuid": "user-uuid",
                "organizationUuid": "org-uuid-5678",
                "organizationName": "Acme Corp",
            }
        }))

        switcher = ClaudeAccountSwitcher()
        with patch("claude_swap.oauth.try_fetch_usage_for_account", return_value=oauth.UsageOutcome(None)):
            switcher.list_accounts()

        out = capsys.readouterr().out
        assert "Acme Corp" in out
        assert "personal" in out
        assert "(active)" in out

    def test_active_account_detected_by_org_uuid(self, temp_home, mock_credentials_file,
                                                   sample_sequence_data_with_org, capsys):
        """Only the account matching current org_uuid should be marked (active)."""
        from claude_swap.switcher import ClaudeAccountSwitcher
        from unittest.mock import patch

        backup_dir = get_backup_root()
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / "sequence.json").write_text(json.dumps(sample_sequence_data_with_org))

        config_path = temp_home / ".claude.json"
        config_path.write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "user@example.com",
                "accountUuid": "user-uuid",
            }
        }))

        switcher = ClaudeAccountSwitcher()
        with patch("claude_swap.oauth.try_fetch_usage_for_account", return_value=oauth.UsageOutcome(None)):
            switcher.list_accounts()

        out = capsys.readouterr().out
        lines = [ln for ln in out.splitlines() if "(active)" in ln]
        assert len(lines) == 1
        assert "personal" in lines[0]


# ── Task 8: backward compatibility ───────────────────────────────────────────

class TestBackwardCompatibility:
    def test_old_sequence_json_without_org_fields(self, temp_home, sample_sequence_data, capsys):
        """Old sequence.json without organizationUuid should work correctly."""
        from claude_swap.switcher import ClaudeAccountSwitcher
        from unittest.mock import patch

        backup_dir = get_backup_root()
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / "sequence.json").write_text(json.dumps(sample_sequence_data))

        config_path = temp_home / ".claude.json"
        config_path.write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "account1@example.com",
                "accountUuid": "uuid-1",
            }
        }))
        (temp_home / ".claude" / ".credentials.json").write_text('{"accessToken": "tok"}')

        switcher = ClaudeAccountSwitcher()
        with patch("claude_swap.oauth.try_fetch_usage_for_account", return_value=oauth.UsageOutcome(None)):
            switcher.list_accounts()

        out = capsys.readouterr().out
        assert "account1@example.com" in out
        assert "personal" in out

    def test_status_with_old_sequence_json(self, temp_home, sample_sequence_data, capsys):
        """status should display personal for old sequence.json entries."""
        from claude_swap.switcher import ClaudeAccountSwitcher

        backup_dir = get_backup_root()
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / "sequence.json").write_text(json.dumps(sample_sequence_data))

        config_path = temp_home / ".claude.json"
        config_path.write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "account1@example.com",
                "accountUuid": "uuid-1",
            }
        }))

        switcher = ClaudeAccountSwitcher()
        switcher.status()

        out = capsys.readouterr().out
        assert "account1@example.com" in out
        assert "personal" in out


class TestUpgradeMigration:
    """Test upgrade path from pre-v0.6.0 (no org fields) to v0.6.0+."""

    def _setup_pre_v06(self, temp_home, sequence_data, live_config):
        """Helper to set up pre-v0.6.0 state with a live config."""
        backup_dir = get_backup_root()
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / "sequence.json").write_text(json.dumps(sequence_data))

        config_path = temp_home / ".claude.json"
        config_path.write_text(json.dumps(live_config))

    def test_status_after_upgrade_with_org_uuid(
        self, temp_home, sample_sequence_data_pre_v06, capsys
    ):
        """status() should detect managed account after auto-migration."""
        self._setup_pre_v06(temp_home, sample_sequence_data_pre_v06, {
            "oauthAccount": {
                "emailAddress": "user@example.com",
                "accountUuid": "user-uuid-1234",
                "organizationUuid": "org-uuid-live",
                "organizationName": "Live Org",
            }
        })

        switcher = ClaudeAccountSwitcher()
        switcher.status()

        out = capsys.readouterr().out
        assert "Account-1" in out
        assert "not managed" not in out

    def test_list_after_upgrade_marks_active(
        self, temp_home, sample_sequence_data_pre_v06, capsys
    ):
        """list_accounts() should mark the active account after auto-migration."""
        self._setup_pre_v06(temp_home, sample_sequence_data_pre_v06, {
            "oauthAccount": {
                "emailAddress": "user@example.com",
                "accountUuid": "user-uuid-1234",
                "organizationUuid": "org-uuid-live",
                "organizationName": "Live Org",
            }
        })
        (temp_home / ".claude" / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "test-token"}})
        )

        switcher = ClaudeAccountSwitcher()
        with patch("claude_swap.oauth.try_fetch_usage_for_account", return_value=oauth.UsageOutcome(None)):
            switcher.list_accounts()

        out = capsys.readouterr().out
        assert "(active)" in out

    def test_migration_uses_live_config_over_backup(
        self, temp_home, sample_sequence_data_pre_v06
    ):
        """Migration should prefer live config org fields for the active account."""
        self._setup_pre_v06(temp_home, sample_sequence_data_pre_v06, {
            "oauthAccount": {
                "emailAddress": "user@example.com",
                "accountUuid": "user-uuid-1234",
                "organizationUuid": "org-uuid-live",
                "organizationName": "Live Org",
            }
        })

        switcher = ClaudeAccountSwitcher()
        data = switcher._get_sequence_data_migrated()

        assert data["accounts"]["1"]["organizationUuid"] == "org-uuid-live"
        assert data["accounts"]["1"]["organizationName"] == "Live Org"

    def test_migration_idempotent(
        self, temp_home, sample_sequence_data_pre_v06
    ):
        """Running migration twice should not change the result."""
        self._setup_pre_v06(temp_home, sample_sequence_data_pre_v06, {
            "oauthAccount": {
                "emailAddress": "user@example.com",
                "accountUuid": "user-uuid-1234",
                "organizationUuid": "org-uuid-live",
                "organizationName": "Live Org",
            }
        })

        switcher = ClaudeAccountSwitcher()
        data1 = switcher._get_sequence_data_migrated()
        data2 = switcher._get_sequence_data_migrated()

        assert data1["accounts"]["1"]["organizationUuid"] == data2["accounts"]["1"]["organizationUuid"]
        assert data1["accounts"]["2"]["organizationUuid"] == data2["accounts"]["2"]["organizationUuid"]

    def test_migration_skips_already_migrated(
        self, temp_home, sample_sequence_data_pre_v06
    ):
        """Accounts that already have org fields should not be changed."""
        sample_sequence_data_pre_v06["accounts"]["1"]["organizationUuid"] = "existing-org"
        sample_sequence_data_pre_v06["accounts"]["1"]["organizationName"] = "Existing Org"

        self._setup_pre_v06(temp_home, sample_sequence_data_pre_v06, {
            "oauthAccount": {
                "emailAddress": "user@example.com",
                "accountUuid": "user-uuid-1234",
                "organizationUuid": "different-org",
                "organizationName": "Different Org",
            }
        })

        switcher = ClaudeAccountSwitcher()
        data = switcher._get_sequence_data_migrated()

        assert data["accounts"]["1"]["organizationUuid"] == "existing-org"
        assert data["accounts"]["1"]["organizationName"] == "Existing Org"
        assert data["accounts"]["2"]["organizationUuid"] == ""

    def test_switch_after_upgrade_no_duplicate(
        self, temp_home, sample_sequence_data_pre_v06, capsys
    ):
        """switch() on pre-v0.6.0 data should not auto-add a duplicate account."""
        self._setup_pre_v06(temp_home, sample_sequence_data_pre_v06, {
            "oauthAccount": {
                "emailAddress": "user@example.com",
                "accountUuid": "user-uuid-1234",
                "organizationUuid": "org-uuid-live",
                "organizationName": "Live Org",
            }
        })
        (temp_home / ".claude" / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "test-token"}})
        )

        switcher = ClaudeAccountSwitcher()
        backup_dir = get_backup_root()
        creds_dir = backup_dir / "credentials"
        creds_dir.mkdir(exist_ok=True)
        import base64
        encoded = base64.b64encode(
            json.dumps({"claudeAiOauth": {"accessToken": "token-2"}}).encode()
        ).decode()
        (creds_dir / ".creds-2-other@example.com.enc").write_text(encoded)

        configs_dir = backup_dir / "configs"
        configs_dir.mkdir(exist_ok=True)
        (configs_dir / ".claude-config-2-other@example.com.json").write_text(
            json.dumps({"oauthAccount": {
                "emailAddress": "other@example.com",
                "accountUuid": "other-uuid-5678",
            }})
        )

        backup_creds = json.dumps({"claudeAiOauth": {"accessToken": "token-2"}})
        with patch.object(switcher, "_write_credentials"), \
             patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_read_account_credentials", return_value=backup_creds), \
             patch.object(switcher, "_read_account_config", return_value=json.dumps({
                 "oauthAccount": {
                     "emailAddress": "other@example.com",
                     "accountUuid": "other-uuid-5678",
                 }
             })):
            switcher.switch()

        data = switcher._get_sequence_data()
        assert len(data["accounts"]) == 2
        assert "auto" not in capsys.readouterr().out.lower()


# ── --slot option for add_account ──────────────────────────────────────────────

class TestAddAccountSlot:
    """Test add_account with --slot option."""

    def _make_switcher(self, temp_home, email="test@example.com", org_uuid="", org_name=""):
        """Helper: write a claude config and return a switcher instance."""
        config = {
            "oauthAccount": {
                "emailAddress": email,
                "accountUuid": "uuid-" + email,
                "organizationUuid": org_uuid,
                "organizationName": org_name,
            }
        }
        config_path = temp_home / ".claude.json"
        config_path.write_text(json.dumps(config))
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._init_sequence_file()
        return switcher

    def test_add_to_specific_empty_slot(self, temp_home, capsys):
        """Adding to an empty slot should place the account there."""
        fake_creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})
        switcher = self._make_switcher(temp_home)

        with patch.object(switcher, "_read_active_credentials", return_value=ActiveCredentials(fake_creds, False)), \
             patch.object(switcher, "_write_account_credentials"):
            switcher.add_account(slot=5)

        data = switcher._get_sequence_data()
        assert "5" in data["accounts"]
        assert data["accounts"]["5"]["email"] == "test@example.com"
        assert data["activeAccountNumber"] == 5
        assert 5 in data["sequence"]
        assert "Added" in capsys.readouterr().out

    def test_add_without_slot_auto_assigns(self, temp_home):
        """Without --slot, should auto-assign next number (original behavior)."""
        fake_creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})
        switcher = self._make_switcher(temp_home)

        with patch.object(switcher, "_read_active_credentials", return_value=ActiveCredentials(fake_creds, False)), \
             patch.object(switcher, "_write_account_credentials"):
            switcher.add_account()

        data = switcher._get_sequence_data()
        assert "1" in data["accounts"]

    def test_slot_occupied_cancel(self, temp_home, capsys):
        """When slot is occupied and user cancels, nothing should change."""
        fake_creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})

        # Add account A to slot 3
        switcher = self._make_switcher(temp_home, email="a@example.com")
        with patch.object(switcher, "_read_active_credentials", return_value=ActiveCredentials(fake_creds, False)), \
             patch.object(switcher, "_write_account_credentials"):
            switcher.add_account(slot=3)

        # Try to add account B to slot 3, answer "n"
        switcher = self._make_switcher(temp_home, email="b@example.com")
        with patch.object(switcher, "_read_active_credentials", return_value=ActiveCredentials(fake_creds, False)), \
             patch.object(switcher, "_write_account_credentials"), \
             patch("builtins.input", return_value="n"):
            switcher.add_account(slot=3)

        # Slot 3 should still be account A
        data = switcher._get_sequence_data()
        assert data["accounts"]["3"]["email"] == "a@example.com"
        assert "Cancelled" in capsys.readouterr().out

    def test_slot_occupied_overwrite(self, temp_home, capsys):
        """When slot is occupied and user confirms, should overwrite."""
        fake_creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})

        # Add account A to slot 3
        switcher = self._make_switcher(temp_home, email="a@example.com")
        with patch.object(switcher, "_read_active_credentials", return_value=ActiveCredentials(fake_creds, False)), \
             patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_delete_account_credentials"):
            switcher.add_account(slot=3)

        # Add account B to slot 3, answer "y"
        switcher = self._make_switcher(temp_home, email="b@example.com")
        with patch.object(switcher, "_read_active_credentials", return_value=ActiveCredentials(fake_creds, False)), \
             patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_delete_account_credentials"), \
             patch("builtins.input", return_value="y"):
            switcher.add_account(slot=3)

        data = switcher._get_sequence_data()
        assert data["accounts"]["3"]["email"] == "b@example.com"
        assert len(data["accounts"]) == 1
        assert "Added" in capsys.readouterr().out

    def test_migrate_account_to_different_slot(self, temp_home, capsys):
        """Moving an existing account to a new slot should clean up the old slot."""
        fake_creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})

        # Add account to slot 1 (auto)
        switcher = self._make_switcher(temp_home, email="user@example.com")
        with patch.object(switcher, "_read_active_credentials", return_value=ActiveCredentials(fake_creds, False)), \
             patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_delete_account_credentials"):
            switcher.add_account()

        data = switcher._get_sequence_data()
        assert "1" in data["accounts"]

        # Move to slot 5
        with patch.object(switcher, "_read_active_credentials", return_value=ActiveCredentials(fake_creds, False)), \
             patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_delete_account_credentials"):
            switcher.add_account(slot=5)

        data = switcher._get_sequence_data()
        assert "1" not in data["accounts"]
        assert "5" in data["accounts"]
        assert data["accounts"]["5"]["email"] == "user@example.com"
        assert 1 not in data["sequence"]
        assert 5 in data["sequence"]
        out = capsys.readouterr().out
        assert "Moved from slot 1" in out

    def test_migrate_with_occupied_target_cancel_preserves_old_slot(self, temp_home, capsys):
        """If migration target is occupied and user cancels, old slot must survive."""
        fake_creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})

        # Add account A to slot 1
        switcher = self._make_switcher(temp_home, email="a@example.com")
        with patch.object(switcher, "_read_active_credentials", return_value=ActiveCredentials(fake_creds, False)), \
             patch.object(switcher, "_write_account_credentials"):
            switcher.add_account(slot=1)

        # Add account B to slot 3
        switcher = self._make_switcher(temp_home, email="b@example.com")
        with patch.object(switcher, "_read_active_credentials", return_value=ActiveCredentials(fake_creds, False)), \
             patch.object(switcher, "_write_account_credentials"):
            switcher.add_account(slot=3)

        # Try to move A from slot 1 → slot 3, cancel
        switcher = self._make_switcher(temp_home, email="a@example.com")
        with patch.object(switcher, "_read_active_credentials", return_value=ActiveCredentials(fake_creds, False)), \
             patch.object(switcher, "_write_account_credentials"), \
             patch("builtins.input", return_value="n"):
            switcher.add_account(slot=3)

        # Both slots should be untouched
        data = switcher._get_sequence_data()
        assert data["accounts"]["1"]["email"] == "a@example.com"
        assert data["accounts"]["3"]["email"] == "b@example.com"
        assert "Cancelled" in capsys.readouterr().out

    def test_slot_must_be_positive(self, temp_home):
        """Slot number must be >= 1."""
        fake_creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})
        switcher = self._make_switcher(temp_home)

        with patch.object(switcher, "_read_active_credentials", return_value=ActiveCredentials(fake_creds, False)), \
             pytest.raises(ConfigError, match="must be >= 1"):
            switcher.add_account(slot=0)

    def test_sequence_stays_sorted(self, temp_home):
        """Sequence list should remain sorted when using --slot."""
        fake_creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})

        # Add to slot 5
        switcher = self._make_switcher(temp_home, email="a@example.com")
        with patch.object(switcher, "_read_active_credentials", return_value=ActiveCredentials(fake_creds, False)), \
             patch.object(switcher, "_write_account_credentials"):
            switcher.add_account(slot=5)

        # Add to slot 2
        switcher = self._make_switcher(temp_home, email="b@example.com")
        with patch.object(switcher, "_read_active_credentials", return_value=ActiveCredentials(fake_creds, False)), \
             patch.object(switcher, "_write_account_credentials"):
            switcher.add_account(slot=2)

        data = switcher._get_sequence_data()
        assert data["sequence"] == [2, 5]


class TestPurgeLegacyCleanup:
    """``purge`` must remove a stale legacy directory if it ever reappears.

    Migration normally consumes the legacy path on init, but a partial
    pre-migration state or external recreation could leave it behind.
    Purge is the user's last-resort "remove everything" hammer, so it must
    cover that case explicitly.
    """

    def _ensure_linux_layout(self, monkeypatch):
        # Tests must observe the post-migration two-path world. On macOS in
        # CI the backup root and the legacy root are the same directory, so
        # there's nothing distinct to clean — pin to LINUX semantics.
        monkeypatch.setattr(Platform, "detect", staticmethod(lambda: Platform.LINUX))

    def _make_switcher_then_recreate_legacy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[ClaudeAccountSwitcher, Path, Path]:
        """Construct a switcher with no legacy present, then recreate it.

        Mirrors the realistic state where migration completed (or never had
        anything to migrate) and a stale legacy directory subsequently
        reappeared — e.g. a user manually backing up to the old path, or a
        third-party tool restoring a snapshot.
        """
        from claude_swap.paths import get_backup_root, get_legacy_backup_root

        self._ensure_linux_layout(monkeypatch)
        backup_dir = get_backup_root()
        backup_dir.mkdir(parents=True, exist_ok=True)

        # Instantiate while legacy is absent → init succeeds.
        switcher = ClaudeAccountSwitcher()

        # Now legacy reappears after init.
        legacy = get_legacy_backup_root()
        legacy.mkdir(parents=True, exist_ok=True)
        return switcher, backup_dir, legacy

    def test_purge_removes_stale_legacy_directory(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        switcher, backup_dir, legacy = self._make_switcher_then_recreate_legacy(monkeypatch)
        (legacy / "ghost.txt").write_text("should be removed")

        with patch("builtins.input", return_value="y"):
            switcher.purge()

        assert not legacy.exists()
        assert not backup_dir.exists()

    def test_purge_prompt_lists_legacy_when_present(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ):
        switcher, backup_dir, legacy = self._make_switcher_then_recreate_legacy(monkeypatch)

        with patch("builtins.input", return_value="n"):
            switcher.purge()

        out = capsys.readouterr().out
        assert str(backup_dir) in out
        assert str(legacy) in out

    def test_purge_prompt_omits_legacy_when_absent(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ):
        from claude_swap.paths import get_backup_root, get_legacy_backup_root

        self._ensure_linux_layout(monkeypatch)
        backup_dir = get_backup_root()
        backup_dir.mkdir(parents=True, exist_ok=True)
        legacy = get_legacy_backup_root()
        assert not legacy.exists()

        switcher = ClaudeAccountSwitcher()
        with patch("builtins.input", return_value="n"):
            switcher.purge()

        out = capsys.readouterr().out
        assert "Legacy backup directory" not in out


class TestAddAccountFromToken:
    """Tests for add_account_from_token (--add-token flow)."""

    def _make_switcher(self, temp_home):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._init_sequence_file()
        return switcher

    def test_basic_add_stores_account(self, temp_home, capsys):
        """A valid token + email should store the account and print 'Added'."""
        switcher = self._make_switcher(temp_home)
        with patch.object(switcher, "_write_account_credentials") as mock_creds, \
             patch.object(switcher, "_write_account_config"):
            switcher.add_account_from_token("sk-ant-oat01-abc", "user@example.com")

        data = switcher._get_sequence_data()
        assert "1" in data["accounts"]
        assert data["accounts"]["1"]["email"] == "user@example.com"
        assert 1 in data["sequence"]
        out = capsys.readouterr().out
        assert "Added" in out
        assert "user@example.com" in out

    def test_credentials_blob_format(self, temp_home):
        """Stored credentials must wrap the token in claudeAiOauth and seed default scopes."""
        switcher = self._make_switcher(temp_home)
        stored_creds = None

        def capture_creds(num, email, creds):
            nonlocal stored_creds
            stored_creds = creds

        with patch.object(switcher, "_write_account_credentials", side_effect=capture_creds), \
             patch.object(switcher, "_write_account_config"):
            switcher.add_account_from_token("mytoken", "user@example.com")

        oauth_blob = json.loads(stored_creds)["claudeAiOauth"]
        assert oauth_blob["accessToken"] == "mytoken"
        assert oauth_blob["scopes"] == list(SETUP_TOKEN_SCOPES)

    def test_config_blob_contains_email(self, temp_home):
        """Stored config must contain oauthAccount.emailAddress."""
        switcher = self._make_switcher(temp_home)
        stored_config = None

        def capture_config(num, email, cfg):
            nonlocal stored_config
            stored_config = cfg

        with patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_write_account_config", side_effect=capture_config):
            switcher.add_account_from_token("mytoken", "user@example.com")

        cfg = json.loads(stored_config)
        assert cfg["oauthAccount"]["emailAddress"] == "user@example.com"

    def test_explicit_slot(self, temp_home):
        """--slot should place the account in the specified slot."""
        switcher = self._make_switcher(temp_home)
        with patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_write_account_config"):
            switcher.add_account_from_token("tok", "user@example.com", slot=7)

        data = switcher._get_sequence_data()
        assert "7" in data["accounts"]
        assert "1" not in data["accounts"]
        assert 7 in data["sequence"]

    def test_update_in_place_same_email(self, temp_home, capsys):
        """Calling add_account_from_token again for the same email refreshes in place."""
        switcher = self._make_switcher(temp_home)
        with patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_write_account_config"):
            switcher.add_account_from_token("token-v1", "user@example.com")
        with patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_write_account_config"):
            switcher.add_account_from_token("token-v2", "user@example.com")

        data = switcher._get_sequence_data()
        assert len(data["accounts"]) == 1
        out = capsys.readouterr().out
        assert "Updated token" in out

    def test_update_in_place_writes_scopes(self, temp_home):
        """Refreshing an existing account in place must also seed default scopes."""
        switcher = self._make_switcher(temp_home)
        with patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_write_account_config"):
            switcher.add_account_from_token("token-v1", "user@example.com")

        stored_creds = None

        def capture_creds(num, email, creds):
            nonlocal stored_creds
            stored_creds = creds

        with patch.object(switcher, "_write_account_credentials", side_effect=capture_creds), \
             patch.object(switcher, "_write_account_config"):
            switcher.add_account_from_token("token-v2", "user@example.com")

        oauth_blob = json.loads(stored_creds)["claudeAiOauth"]
        assert oauth_blob["accessToken"] == "token-v2"
        assert oauth_blob["scopes"] == list(SETUP_TOKEN_SCOPES)

    def test_update_in_place_clears_quarantine(self, temp_home):
        """Refreshing a token in place must lift the dead-token quarantine, so a
        stale strike doesn't leave the account stuck at 're-login needed' and
        never fetching the new token (mirrors add_account)."""
        from claude_swap.usage_store import FetchRecord
        switcher = self._make_switcher(temp_home)
        with patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_write_account_config"):
            switcher.add_account_from_token("token-v1", "user@example.com")

        identity = ("user@example.com", "")
        switcher._usage_store.record(
            {"1": FetchRecord(error="invalid_grant")}, {"1": identity}
        )
        assert switcher._usage_store.entries({"1": identity})["1"].token_dead()

        with patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_write_account_config"):
            switcher.add_account_from_token("token-v2", "user@example.com")

        assert not switcher._usage_store.entries({"1": identity})["1"].token_dead()

    def test_new_write_clears_stale_quarantine(self, temp_home):
        """Writing a fresh credential into a slot whose lingering usage row still
        carries a dead-token strike (same identity) must start it clean."""
        from claude_swap.usage_store import FetchRecord
        switcher = self._make_switcher(temp_home)
        identity = ("user@example.com", "")
        switcher._usage_store.record(
            {"5": FetchRecord(error="invalid_grant")}, {"5": identity}
        )
        assert switcher._usage_store.entries({"5": identity})["5"].token_dead()

        with patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_write_account_config"):
            switcher.add_account_from_token("tok", "user@example.com", slot=5)

        assert not switcher._usage_store.entries({"5": identity})["5"].token_dead()

    def test_update_in_place_rejects_inconsistent_metadata(self, temp_home):
        """Never write account-None-* credentials if sequence lookup is corrupt."""
        switcher = self._make_switcher(temp_home)
        with patch.object(switcher, "_account_exists", return_value=True), \
             patch.object(switcher, "_write_account_credentials") as write_creds, \
             pytest.raises(ConfigError, match="metadata.*inconsistent"):
            switcher.add_account_from_token("token-v2", "user@example.com")

        write_creds.assert_not_called()

    def test_invalid_email_raises(self, temp_home):
        """A malformed email should raise ValidationError."""
        switcher = self._make_switcher(temp_home)
        with pytest.raises(ValidationError, match="Invalid email"):
            switcher.add_account_from_token("tok", "not-an-email")

    def test_empty_token_raises(self, temp_home):
        """An empty token string should raise ValidationError."""
        switcher = self._make_switcher(temp_home)
        with pytest.raises(ValidationError, match="empty"):
            switcher.add_account_from_token("   ", "user@example.com")

    def test_stdin_token(self, temp_home, capsys):
        """Token='-' should read from stdin."""
        switcher = self._make_switcher(temp_home)
        import io
        fake_stdin = io.StringIO("stdin-token\n")
        with patch("sys.stdin", fake_stdin), \
             patch.object(switcher, "_write_account_credentials") as mock_creds, \
             patch.object(switcher, "_write_account_config"):
            switcher.add_account_from_token("-", "user@example.com")

        stored = mock_creds.call_args[0][2]
        oauth_blob = json.loads(stored)["claudeAiOauth"]
        assert oauth_blob["accessToken"] == "stdin-token"
        assert oauth_blob["scopes"] == list(SETUP_TOKEN_SCOPES)

    def test_slot_zero_raises(self, temp_home):
        """Slot 0 should raise ConfigError."""
        switcher = self._make_switcher(temp_home)
        with pytest.raises(ConfigError, match=">= 1"):
            switcher.add_account_from_token("tok", "user@example.com", slot=0)

    def test_sequence_sorted_after_add(self, temp_home):
        """Sequence must remain sorted when using an explicit slot."""
        switcher = self._make_switcher(temp_home)
        with patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_write_account_config"):
            switcher.add_account_from_token("tok", "a@example.com", slot=5)
        with patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_write_account_config"):
            switcher.add_account_from_token("tok", "b@example.com", slot=2)

        data = switcher._get_sequence_data()
        assert data["sequence"] == [2, 5]

    def test_default_email_when_omitted(self, temp_home, capsys):
        """Omitting email should synthesize setup-token-{slot}@token.local."""
        switcher = self._make_switcher(temp_home)
        with patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_write_account_config"):
            switcher.add_account_from_token("tok")

        data = switcher._get_sequence_data()
        assert data["accounts"]["1"]["email"] == "setup-token-1@token.local"
        out = capsys.readouterr().out
        assert "setup-token-1@token.local" in out

    def test_default_email_with_explicit_slot(self, temp_home):
        """Default email should derive from explicit --slot when one is given."""
        switcher = self._make_switcher(temp_home)
        with patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_write_account_config"):
            switcher.add_account_from_token("tok", slot=7)

        data = switcher._get_sequence_data()
        assert data["accounts"]["7"]["email"] == "setup-token-7@token.local"

    def test_default_email_writes_to_config_blob(self, temp_home):
        """Defaulted email must propagate into the oauthAccount.emailAddress field."""
        switcher = self._make_switcher(temp_home)
        stored_config = None

        def capture_config(num, email, cfg):
            nonlocal stored_config
            stored_config = cfg

        with patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_write_account_config", side_effect=capture_config):
            switcher.add_account_from_token("tok", slot=3)

        cfg = json.loads(stored_config)
        assert cfg["oauthAccount"]["emailAddress"] == "setup-token-3@token.local"

    def test_default_email_unique_per_slot(self, temp_home):
        """Two default-email registrations to different slots must coexist."""
        switcher = self._make_switcher(temp_home)
        with patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_write_account_config"):
            switcher.add_account_from_token("tok-a", slot=4)
        with patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_write_account_config"):
            switcher.add_account_from_token("tok-b", slot=8)

        data = switcher._get_sequence_data()
        emails = {data["accounts"][n]["email"] for n in ("4", "8")}
        assert emails == {
            "setup-token-4@token.local",
            "setup-token-8@token.local",
        }

    def test_explicit_email_not_overridden_by_default(self, temp_home):
        """Explicit --email must win over the auto-default."""
        switcher = self._make_switcher(temp_home)
        with patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_write_account_config"):
            switcher.add_account_from_token("tok", email="me@example.com", slot=2)

        data = switcher._get_sequence_data()
        assert data["accounts"]["2"]["email"] == "me@example.com"


class TestPurge:
    """Tests for purge cleanup."""

    def test_purge_removes_legacy_none_keychain_entry(self, temp_home):
        """Purge should clean account-None-* entries from older buggy runs — from
        the new security service and best-effort from the legacy keyring."""
        switcher = ClaudeAccountSwitcher()
        switcher.platform = Platform.MACOS
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, {
            "activeAccountNumber": 1,
            "lastUpdated": "2024-01-01T00:00:00Z",
            "sequence": [1],
            "accounts": {
                "1": {
                    "email": "user@example.com",
                    "uuid": "",
                    "organizationUuid": "",
                    "organizationName": "",
                    "added": "2024-01-01T00:00:00Z",
                }
            },
        })

        mock_keyring = MagicMock()
        with patch("builtins.input", return_value="y"), \
             patch("claude_swap.switcher.macos_keychain") as mock_kc, \
             patch.dict(sys.modules, {"keyring": mock_keyring}):
            switcher.purge()

        # New security service: account + legacy account-None both cleaned.
        mock_kc.delete_password.assert_has_calls([
            call("claude-swap", "account-1-user@example.com"),
            call("claude-swap", "account-None-user@example.com"),
        ])
        # Best-effort legacy keyring cleanup of the old claude-code service.
        mock_keyring.delete_password.assert_has_calls([
            call("claude-code", "account-1-user@example.com"),
            call("claude-code", "account-None-user@example.com"),
        ])


# ---------------------------------------------------------------------------
# Issue #41: tolerate broken slots in switch/switch_to
# ---------------------------------------------------------------------------


class TestSwitchSkipsBrokenSlots:
    """Issue #41: --switch must skip slots whose stored creds or config are
    missing rather than aborting. --switch-to N must keep failing but with an
    actionable, accurate message."""

    def _setup(self, temp_home: Path) -> ClaudeAccountSwitcher:
        s = ClaudeAccountSwitcher()
        s.platform = Platform.LINUX
        s._setup_directories()
        s._init_sequence_file()
        return s

    def _seed(
        self,
        s: ClaudeAccountSwitcher,
        num: int,
        email: str,
        creds: bool = True,
        config: bool = True,
    ) -> None:
        if creds:
            s._write_account_credentials(
                str(num),
                email,
                json.dumps({
                    "claudeAiOauth": {
                        "accessToken": f"sk-{num}",
                        "refreshToken": f"rt-{num}",
                    },
                }),
            )
        if config:
            s._write_account_config(
                str(num),
                email,
                json.dumps({
                    "oauthAccount": {
                        "emailAddress": email,
                        "accountUuid": f"uuid-{num}",
                    },
                }),
            )

        data = s._get_sequence_data() or {
            "activeAccountNumber": None,
            "lastUpdated": "",
            "sequence": [],
            "accounts": {},
        }
        data["accounts"][str(num)] = {
            "email": email,
            "uuid": f"uuid-{num}",
            "organizationUuid": "",
            "organizationName": "",
            "added": "2024-01-01T00:00:00Z",
        }
        if num not in data["sequence"]:
            data["sequence"].append(num)
            data["sequence"].sort()
        if data["activeAccountNumber"] is None:
            data["activeAccountNumber"] = num
        s._write_json(s.sequence_file, data)

    def test_account_is_switchable_helper(self, temp_home: Path):
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com", creds=False)
        self._seed(s, 3, "c@example.com", config=False)

        assert s._account_is_switchable("1") is True
        assert s._account_is_switchable("2") is False
        assert s._account_is_switchable("3") is False
        # Stale sequence reference to a missing account record.
        assert s._account_is_switchable("99") is False

    def test_rotation_skips_broken_next_slot(self, temp_home: Path, capsys):
        """Three accounts, active=1, slot 2 broken — rotation must land on 3."""
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com", creds=False)
        self._seed(s, 3, "c@example.com")

        # Active account 1 is the live identity.
        live_creds = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-live-1",
                "refreshToken": "rt-live-1",
            },
        })
        (temp_home / ".claude" / ".credentials.json").write_text(live_creds)
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "a@example.com",
                "accountUuid": "uuid-1",
            },
        }))

        with patch.object(s, "list_accounts"):
            s.switch()

        out = capsys.readouterr().out
        assert "Skipping Account-2" in out

        data = s._get_sequence_data()
        assert data["activeAccountNumber"] == 3

    def test_rotation_no_valid_targets_returns_without_error(
        self, temp_home: Path, capsys
    ):
        """All non-active slots are broken — print a message, no exception."""
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com", creds=False)

        live_creds = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-live-1",
                "refreshToken": "rt-live-1",
            },
        })
        (temp_home / ".claude" / ".credentials.json").write_text(live_creds)
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "a@example.com",
                "accountUuid": "uuid-1",
            },
        }))

        s.switch()  # must not raise

        out = capsys.readouterr().out
        assert "Skipping Account-2" in out
        assert "No other accounts have valid" in out

        # Active account unchanged.
        data = s._get_sequence_data()
        assert data["activeAccountNumber"] == 1

    def test_switch_to_missing_credentials_actionable_error(self, temp_home: Path):
        """switch_to a broken target raises with the new credentials message."""
        from claude_swap.exceptions import SwitchError

        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com", creds=False)

        live_creds = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-live-1",
                "refreshToken": "rt-live-1",
            },
        })
        (temp_home / ".claude" / ".credentials.json").write_text(live_creds)
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "a@example.com",
                "accountUuid": "uuid-1",
            },
        }))

        with pytest.raises(SwitchError, match="has no stored credentials"):
            s.switch_to("2")

    def test_switch_to_missing_config_actionable_error(self, temp_home: Path):
        """switch_to a target with creds but no config raises a distinct error."""
        from claude_swap.exceptions import SwitchError

        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com", config=False)

        live_creds = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-live-1",
                "refreshToken": "rt-live-1",
            },
        })
        (temp_home / ".claude" / ".credentials.json").write_text(live_creds)
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "a@example.com",
                "accountUuid": "uuid-1",
            },
        }))

        with pytest.raises(SwitchError, match="has no stored config backup"):
            s.switch_to("2")

    def test_fresh_machine_skips_broken_preferred_target(self, temp_home: Path, capsys):
        """No live session — picks first switchable slot if the recorded
        activeAccountNumber is broken (e.g., right after import)."""
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com", creds=False)
        self._seed(s, 2, "b@example.com")
        # Mark account 1 as the recorded active (broken) — simulates a stale
        # state after import + later corruption.
        data = s._get_sequence_data()
        data["activeAccountNumber"] = 1
        s._write_json(s.sequence_file, data)

        # No live config — fresh-machine branch.
        with patch.object(s, "list_accounts"):
            s.switch()

        out = capsys.readouterr().out
        assert "Skipping Account-1" in out

        data = s._get_sequence_data()
        assert data["activeAccountNumber"] == 2

    def test_fresh_machine_skips_disabled_preferred_target(
        self, temp_home: Path, capsys
    ):
        """No live session — a disabled recorded active slot stays out of rotation."""
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        s.set_account_disabled("1", True)
        capsys.readouterr()

        with patch.object(s, "list_accounts"):
            s.switch()

        assert "Skipping Account-1 (disabled)" in capsys.readouterr().out
        assert s._get_sequence_data()["activeAccountNumber"] == 2

    def test_fresh_machine_all_broken_raises(self, temp_home: Path):
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com", creds=False)
        self._seed(s, 2, "b@example.com", config=False)

        with pytest.raises(ConfigError, match="No managed accounts have valid"):
            s.switch()

    def test_fresh_machine_all_disabled_raises(self, temp_home: Path):
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        s.set_account_disabled("1", True)
        s.set_account_disabled("2", True)

        with pytest.raises(ConfigError, match="No accounts remain in rotation"):
            s.switch()


class TestUsageAwareSwitch:
    """--switch --strategy best / next-available pick targets by remaining 5h/7d
    quota. `best` only switches when another account is provably better and
    otherwise stays put; `next-available` rotates, skipping accounts at their
    limit (and anchors on the live account)."""

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
            json.dumps({
                "claudeAiOauth": {
                    "accessToken": f"sk-{num}",
                    "refreshToken": f"rt-{num}",
                },
            }),
        )
        s._write_account_config(
            str(num),
            email,
            json.dumps({
                "oauthAccount": {"emailAddress": email, "accountUuid": f"uuid-{num}"},
            }),
        )
        data = s._get_sequence_data()
        data["accounts"][str(num)] = {
            "email": email,
            "uuid": f"uuid-{num}",
            "organizationUuid": "",
            "organizationName": "",
            "added": "2024-01-01T00:00:00Z",
        }
        if num not in data["sequence"]:
            data["sequence"].append(num)
            data["sequence"].sort()
        if data["activeAccountNumber"] is None:
            data["activeAccountNumber"] = num
        s._write_json(s.sequence_file, data)

    def _make_live(self, temp_home: Path, email: str, num: int) -> None:
        """Make account `num` the live (active) Claude login."""
        (temp_home / ".claude" / ".credentials.json").write_text(json.dumps({
            "claudeAiOauth": {"accessToken": "sk-live", "refreshToken": "rt-live"},
        }))
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": email, "accountUuid": f"uuid-{num}"},
        }))

    @staticmethod
    def _usage(pct: float) -> dict:
        return {"five_hour": {"pct": pct}, "seven_day": {"pct": 0.0}}

    def test_best_switches_to_more_headroom(self, temp_home: Path):
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._seed(s, 3, "c@example.com")
        self._make_live(temp_home, "a@example.com", 1)

        # Current (1) has 50% headroom; 3 has 80% (best), 2 has 10%.
        usage = {"1": self._usage(50), "2": self._usage(90), "3": self._usage(20)}
        with patch.object(s, "_usage_by_account", return_value=usage), \
             patch.object(s, "list_accounts"):
            s.switch(strategy="best")

        assert s._get_sequence_data()["activeAccountNumber"] == 3

    def test_best_stays_when_current_is_already_best(self, temp_home: Path, capsys):
        """Regression: strategy "best" must NOT move you onto a worse account when you
        already hold the most headroom (real-world bug: 89% current vs 100% other)."""
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._make_live(temp_home, "a@example.com", 1)

        # Current (1) has 11% headroom; the only other (2) is maxed out.
        usage = {"1": self._usage(89), "2": self._usage(100)}
        with patch.object(s, "_usage_by_account", return_value=usage), \
             patch.object(s, "list_accounts") as mock_list:
            s.switch(strategy="best")

        assert "Already on the account with the most remaining quota" in capsys.readouterr().out
        assert s._get_sequence_data()["activeAccountNumber"] == 1  # unchanged
        mock_list.assert_not_called()  # no switch happened

    def test_best_all_exhausted_stays_put(self, temp_home: Path, capsys):
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._seed(s, 3, "c@example.com")
        self._make_live(temp_home, "a@example.com", 1)

        usage = {"1": self._usage(100), "2": self._usage(100), "3": self._usage(100)}
        with patch.object(s, "_usage_by_account", return_value=usage), \
             patch.object(s, "list_accounts"):
            s.switch(strategy="best")

        out = capsys.readouterr().out
        assert "All accounts are at their 5h/7d limit" in out
        assert "staying on Account-1" in out
        assert s._get_sequence_data()["activeAccountNumber"] == 1  # unchanged

    def test_best_current_usage_unavailable_stays(self, temp_home: Path, capsys):
        """Current account's usage is unknown → can't prove any target is better,
        so stay even if a candidate has known headroom (never auto-rotate)."""
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._make_live(temp_home, "a@example.com", 1)

        # Current (1) usage unknown; candidate 2 looks good (90% headroom).
        usage = {"1": None, "2": self._usage(10)}
        with patch.object(s, "_usage_by_account", return_value=usage), \
             patch.object(s, "list_accounts") as mock_list:
            s.switch(strategy="best")

        assert "Current account usage is unavailable" in capsys.readouterr().out
        assert s._get_sequence_data()["activeAccountNumber"] == 1  # unchanged
        mock_list.assert_not_called()

    def test_best_no_candidate_usage_stays(self, temp_home: Path, capsys):
        """Current known but no other account has usage data → no comparison is
        possible → stay (not rotation, not 'all exhausted')."""
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._make_live(temp_home, "a@example.com", 1)

        usage = {"1": self._usage(50), "2": None}
        with patch.object(s, "_usage_by_account", return_value=usage), \
             patch.object(s, "list_accounts") as mock_list:
            s.switch(strategy="best")

        out = capsys.readouterr().out
        assert "No other account has usage data to compare" in out
        assert "All accounts are at their 5h/7d limit" not in out
        assert s._get_sequence_data()["activeAccountNumber"] == 1
        mock_list.assert_not_called()

    def test_best_incomplete_comparison_stays(self, temp_home: Path, capsys):
        """Current known + a known *worse* candidate + an unknown candidate →
        stay, without claiming 'most remaining quota' or 'all exhausted' (the
        unknown one can't be ruled better)."""
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._seed(s, 3, "c@example.com")
        self._make_live(temp_home, "a@example.com", 1)

        # Current (1) 50% headroom; 2 worse (10%); 3 unknown.
        usage = {"1": self._usage(50), "2": self._usage(90), "3": None}
        with patch.object(s, "_usage_by_account", return_value=usage), \
             patch.object(s, "list_accounts") as mock_list:
            s.switch(strategy="best")

        out = capsys.readouterr().out
        assert "some usage is unavailable" in out
        assert "most remaining quota" not in out
        assert "All accounts are at their 5h/7d limit" not in out
        assert s._get_sequence_data()["activeAccountNumber"] == 1
        mock_list.assert_not_called()

    def test_best_current_exhausted_with_unknown_candidate_stays(
        self, temp_home: Path, capsys
    ):
        """Current known & exhausted + a known (also-exhausted) candidate + an
        unknown candidate → stay, but must NOT claim 'all exhausted' since the
        unknown account might have room."""
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._seed(s, 3, "c@example.com")
        self._make_live(temp_home, "a@example.com", 1)

        # Current (1) exhausted; 2 also exhausted (known, not better); 3 unknown.
        usage = {"1": self._usage(100), "2": self._usage(100), "3": None}
        with patch.object(s, "_usage_by_account", return_value=usage), \
             patch.object(s, "list_accounts") as mock_list:
            s.switch(strategy="best")

        out = capsys.readouterr().out
        assert "some usage is unavailable" in out
        assert "All accounts are at their 5h/7d limit" not in out
        assert s._get_sequence_data()["activeAccountNumber"] == 1
        mock_list.assert_not_called()

    def test_skip_exhausted_skips_limited_account(self, temp_home: Path, capsys):
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._seed(s, 3, "c@example.com")
        self._make_live(temp_home, "a@example.com", 1)

        usage = {"1": self._usage(0), "2": self._usage(100), "3": self._usage(20)}
        with patch.object(s, "_usage_by_account", return_value=usage), \
             patch.object(s, "list_accounts"):
            s.switch(strategy="next-available")

        out = capsys.readouterr().out
        assert "Skipping Account-2 (at 5h/7d limit)" in out
        assert s._get_sequence_data()["activeAccountNumber"] == 3

    @staticmethod
    def _model_usage(five_h: float, fable: float) -> dict:
        return {
            "five_hour": {"pct": five_h},
            "seven_day": {"pct": 0.0},
            "scoped": [{"name": "Fable", "pct": fable}],
        }

    def test_next_available_with_models_skips_and_names_the_window(
        self, temp_home: Path, capsys
    ):
        """A Fable-exhausted candidate is skipped, the skip names the binding
        window, and the config source is announced up front."""
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._seed(s, 3, "c@example.com")
        self._make_live(temp_home, "a@example.com", 1)

        usage = {
            "1": self._model_usage(0, 10),
            "2": self._model_usage(5, 100),
            "3": self._model_usage(20, 20),
        }
        with patch.object(s, "_usage_by_account", return_value=usage), \
             patch.object(s, "list_accounts"):
            s.switch(
                strategy="next-available",
                models=("Fable",),
                model_source="autoswitch.model",
            )

        out = capsys.readouterr().out
        assert "Using configured model limits: Fable (from autoswitch.model)" in out
        assert "Skipping Account-2 (at Fable limit)" in out
        assert s._get_sequence_data()["activeAccountNumber"] == 3

    def test_next_available_without_models_ignores_scoped(
        self, temp_home: Path, capsys
    ):
        """Default behavior unchanged: scoped windows are invisible."""
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._seed(s, 3, "c@example.com")
        self._make_live(temp_home, "a@example.com", 1)

        usage = {
            "1": self._model_usage(0, 10),
            "2": self._model_usage(5, 100),
            "3": self._model_usage(20, 20),
        }
        with patch.object(s, "_usage_by_account", return_value=usage), \
             patch.object(s, "list_accounts"):
            s.switch(strategy="next-available")

        out = capsys.readouterr().out
        assert "Using configured model limits" not in out
        assert "Skipping" not in out
        assert s._get_sequence_data()["activeAccountNumber"] == 2

    def test_best_noop_json_keeps_inert_model_warning(self, temp_home: Path):
        """The typo warning must survive into the JSON payload even when
        `best` decides to stay (already-best / exhausted no-ops)."""
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._make_live(temp_home, "a@example.com", 1)

        # Current already best → already-best no-op.
        usage = {"1": self._model_usage(0, 10), "2": self._model_usage(50, 10)}
        with patch.object(s, "_usage_by_account", return_value=usage):
            payload = s.switch(
                strategy="best", json_output=True,
                models=("Fabel",), model_source="cli",
            )
        assert payload["switched"] is False
        assert payload["reason"] == "already-best"
        assert any("Fabel" in w for w in payload["warnings"])

        # Everything exhausted → candidates-exhausted no-op.
        usage = {"1": self._model_usage(100, 10), "2": self._model_usage(100, 10)}
        with patch.object(s, "_usage_by_account", return_value=usage):
            payload = s.switch(
                strategy="best", json_output=True,
                models=("Fabel",), model_source="cli",
            )
        assert payload["reason"] == "candidates-exhausted"
        assert any("Fabel" in w for w in payload["warnings"])

    def test_manual_strategies_warn_on_inert_model_name(
        self, temp_home: Path, capsys
    ):
        """A --model name no account reports must not fail silently."""
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._make_live(temp_home, "a@example.com", 1)

        usage = {"1": self._model_usage(0, 10), "2": self._model_usage(5, 10)}
        with patch.object(s, "_usage_by_account", return_value=usage), \
             patch.object(s, "list_accounts"):
            s.switch(strategy="next-available", models=("Fabel",),
                     model_source="cli")
        assert "Fabel" in capsys.readouterr().out
        # ...but a matching name stays quiet.
        with patch.object(s, "_usage_by_account", return_value=usage), \
             patch.object(s, "list_accounts"):
            s.switch(strategy="best", models=("Fable",), model_source="cli")
        assert "typo" not in capsys.readouterr().out

    def test_best_with_models_folds_scoped_into_the_comparison(
        self, temp_home: Path, capsys
    ):
        """Fable binding flips the pick: on 5h alone nothing beats current,
        with Fable folded in account 2 provably has the most headroom."""
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._seed(s, 3, "c@example.com")
        self._make_live(temp_home, "a@example.com", 1)

        usage = {
            "1": self._model_usage(5, 90),
            "2": self._model_usage(5, 20),
            "3": self._model_usage(50, 80),
        }
        with patch.object(s, "_usage_by_account", return_value=usage), \
             patch.object(s, "list_accounts"):
            s.switch(strategy="best", models=("Fable",), model_source="cli")

        out = capsys.readouterr().out
        assert "Using configured model limits: Fable (from --model)" in out
        assert s._get_sequence_data()["activeAccountNumber"] == 2

    def test_skip_exhausted_all_limited_stays_put(self, temp_home: Path, capsys):
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._seed(s, 3, "c@example.com")
        self._make_live(temp_home, "a@example.com", 1)

        usage = {"1": self._usage(0), "2": self._usage(100), "3": self._usage(100)}
        with patch.object(s, "_usage_by_account", return_value=usage), \
             patch.object(s, "list_accounts") as mock_list:
            s.switch(strategy="next-available")

        out = capsys.readouterr().out
        assert "staying on Account-1" in out
        # No switch onto an exhausted account; stays on the current one.
        assert s._get_sequence_data()["activeAccountNumber"] == 1
        mock_list.assert_not_called()

    def test_skip_exhausted_unknown_usage_is_not_skipped(self, temp_home: Path):
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._make_live(temp_home, "a@example.com", 1)

        # Usage unknown for account 2 → must NOT be skipped (give it a chance).
        with patch.object(s, "_usage_by_account", return_value={"1": None, "2": None}), \
             patch.object(s, "list_accounts"):
            s.switch(strategy="next-available")

        assert s._get_sequence_data()["activeAccountNumber"] == 2

    def test_next_available_anchors_on_live_account_under_drift(
        self, temp_home: Path
    ):
        """Item 3: when the live login has drifted from activeAccountNumber,
        next-available rotates relative to the LIVE account (current_num), not
        the stale record — so it never no-ops onto the account you're already
        on."""
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._seed(s, 3, "c@example.com")
        # Recorded active is 1, but the user is actually live on account 2.
        data = s._get_sequence_data()
        data["activeAccountNumber"] = 1
        s._write_json(s.sequence_file, data)
        self._make_live(temp_home, "b@example.com", 2)

        # All healthy, so nothing is skipped for being at its limit.
        usage = {"1": self._usage(0), "2": self._usage(0), "3": self._usage(0)}
        with patch.object(s, "_usage_by_account", return_value=usage), \
             patch.object(s, "list_accounts"):
            s.switch(strategy="next-available")

        # Anchored on the live account (2) → next is 3, not 2 (a no-op).
        assert s._get_sequence_data()["activeAccountNumber"] == 3


class TestClaudeCodeLockCooperation:
    """_perform_switch must hold Claude Code's own advisory locks
    (~/.claude.lock and ~/.claude.json.lock) while mutating credentials/config,
    and fail cleanly — before any mutation — when Claude Code holds them."""

    _setup = TestUsageAwareSwitch._setup
    _seed = TestUsageAwareSwitch._seed
    _make_live = TestUsageAwareSwitch._make_live

    def test_switch_holds_both_cc_locks_at_write_time(self, temp_home: Path):
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._make_live(temp_home, "a@example.com", 1)

        creds_lock = temp_home / ".claude.lock"
        config_lock = temp_home / ".claude.json.lock"
        seen: list[tuple[bool, bool]] = []
        original_write = s._write_credentials

        def spying_write(credentials: str) -> None:
            seen.append((creds_lock.is_dir(), config_lock.is_dir()))
            original_write(credentials)

        with patch.object(s, "_write_credentials", side_effect=spying_write), \
             patch.object(s, "list_accounts"):
            s.switch_to("2")

        assert s._get_sequence_data()["activeAccountNumber"] == 2
        assert seen and all(pair == (True, True) for pair in seen)
        # Released after the switch.
        assert not creds_lock.exists()
        assert not config_lock.exists()

    def test_preheld_cc_lock_fails_cleanly_without_mutation(
        self, temp_home: Path, monkeypatch
    ):
        from claude_swap import claude_locks
        from claude_swap.exceptions import ClaudeCodeLockTimeout

        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._make_live(temp_home, "a@example.com", 1)

        monkeypatch.setattr(claude_locks, "DEFAULT_TIMEOUT_S", 0.3)
        (temp_home / ".claude.lock").mkdir()  # fresh mtime = live CC refresh

        live_creds_before = (
            temp_home / ".claude" / ".credentials.json"
        ).read_text()
        with pytest.raises(ClaudeCodeLockTimeout):
            s.switch_to("2")

        # Nothing was mutated: locks acquire before any write.
        assert s._get_sequence_data()["activeAccountNumber"] == 1
        live_creds_after = (
            temp_home / ".claude" / ".credentials.json"
        ).read_text()
        assert live_creds_after == live_creds_before
        # The holder's lock was left alone.
        assert (temp_home / ".claude.lock").is_dir()


class TestMacosKeychainFallback:
    """macOS auto-fallback to file storage when the Keychain is unusable, plus the
    ``.enc``-wins backup reconciliation.

    The autouse ``block_real_keychain`` fixture fakes a *working* in-memory
    Keychain; individual tests force failures by patching the ``macos_keychain``
    wrapper to raise ``KeychainError`` (``_raise_locked``).
    """

    def _macos_switcher(self) -> ClaudeAccountSwitcher:
        s = ClaudeAccountSwitcher()
        s.platform = Platform.MACOS
        s._setup_directories()
        return s

    # -- capability cache -------------------------------------------------

    def test_non_macos_never_uses_keychain(self, temp_home: Path):
        for plat in (Platform.LINUX, Platform.WSL, Platform.WINDOWS):
            s = ClaudeAccountSwitcher()
            s.platform = plat
            assert s._use_keychain() is False
            assert s._uses_file_backup_backend() is True

    def test_capability_cache_sticky_false(self, temp_home: Path, monkeypatch):
        s = self._macos_switcher()
        assert s._use_keychain() is True  # optimistic before any op

        # A failing op flips routing to unusable for the rest of the process...
        monkeypatch.setattr(macos_keychain, "get_password", _raise_locked)
        with pytest.raises(KeychainError):
            s._kc_call(macos_keychain.get_password, "svc", "acct")
        assert s._use_keychain() is False

        # ...and a later *success* must NOT flip it back (no split-brain).
        monkeypatch.setattr(macos_keychain, "get_password", lambda *a, **k: "ok")
        s._kc_call(macos_keychain.get_password, "svc", "acct")
        assert s._use_keychain() is False

    def test_kc_call_failure_schedules_a_recheck(self, temp_home: Path, monkeypatch):
        s = self._macos_switcher()
        monkeypatch.setattr(macos_keychain, "get_password", _raise_locked)
        before = time.monotonic()
        with pytest.raises(KeychainError):
            s._kc_call(macos_keychain.get_password, "svc", "acct")
        assert s._keychain_disabled_until > before  # a re-probe is scheduled

    def test_keychain_recovers_after_cooldown(self, temp_home: Path):
        # A long-running daemon (menu bar / TUI) must re-probe after the cooldown
        # so a transient `security` timeout doesn't disable the Keychain for the
        # whole process — the stuck-in-file-mode "no credentials" display bug.
        s = self._macos_switcher()
        s._keychain_usable_cache = False
        s._keychain_disabled_until = time.monotonic() - 1  # cooldown already elapsed
        assert s._use_keychain() is True             # re-probes
        assert s._keychain_usable_cache is None       # re-armed for a fresh op
        assert s._keychain_disabled_until == 0.0

    def test_keychain_stays_file_mode_during_cooldown(self, temp_home: Path):
        s = self._macos_switcher()
        s._keychain_usable_cache = False
        s._keychain_disabled_until = time.monotonic() + 100  # still within cooldown
        assert s._use_keychain() is False

    def test_write_keychain_failure_pins_file_mode(self, temp_home: Path, monkeypatch):
        # An active write whose Keychain attempt fails falls back to the file; the
        # stale-item delete is best-effort. Even though the failed op scheduled a
        # re-probe, the write must pin file mode so a later cooldown can't re-probe
        # onto the residual Keychain item and resurrect the wrong account.
        s = self._macos_switcher()
        store = s._store
        monkeypatch.setattr(macos_keychain, "set_password", _raise_locked)
        monkeypatch.setattr(macos_keychain, "delete_password", _raise_locked)
        monkeypatch.setattr(store, "_write_active_credentials_file", lambda creds: None)
        store._write_oauth_credentials('{"claudeAiOauth": {"accessToken": "x"}}')
        assert store._last_active_credentials_backend == "file"
        assert s._keychain_disabled_until == 0.0   # no re-probe scheduled
        assert s._use_keychain() is False          # pinned, stays file mode

    def test_write_fallback_clears_pending_read_reprobe(self, temp_home: Path, monkeypatch):
        # The owner's edge: already in file mode from a read timeout with a
        # re-probe still pending, then a write leaves a stale item behind. The
        # write must clear that pending re-probe (pin) so it never resurrects.
        s = self._macos_switcher()
        store = s._store
        s._keychain_usable_cache = False
        s._keychain_disabled_until = time.monotonic() + 100  # pending read re-probe
        monkeypatch.setattr(macos_keychain, "delete_password", _raise_locked)
        monkeypatch.setattr(store, "_write_active_credentials_file", lambda creds: None)
        store._write_oauth_credentials('{"claudeAiOauth": {"accessToken": "x"}}')
        assert store._last_active_credentials_backend == "file"
        assert s._keychain_disabled_until == 0.0   # pending re-probe cleared
        assert s._use_keychain() is False

    def test_managed_key_write_fallback_pins_file_mode(self, temp_home: Path, monkeypatch):
        # Managed API-key variant of the same guard: a failed Keychain write
        # falls back to plaintext primaryApiKey, and managed-key reads check the
        # Keychain first — so the fallback must pin file mode too, or a cooldown
        # re-probe could read a stale "Claude Code" Keychain item over the key.
        s = self._macos_switcher()
        store = s._store
        monkeypatch.setattr(macos_keychain, "set_password", _raise_locked)
        monkeypatch.setattr(store, "_update_global_config", lambda mutate: None)
        monkeypatch.setattr(store, "_clear_oauth_credential", lambda: None)
        store._write_managed_credentials("sk-ant-api03-" + "x" * 40)
        assert store._last_active_credentials_backend == "file"
        assert s._keychain_disabled_until == 0.0   # pinned, no re-probe
        assert s._use_keychain() is False

    def test_item_exists_is_capability_neutral(
        self, temp_home: Path, block_real_keychain
    ):
        s = self._macos_switcher()
        s._keychain_usable_cache = False  # already in file mode this run
        block_real_keychain.data[("svc", "acct")] = "x"
        # item_exists is NOT routed through _kc_call, so a True result must not
        # resurrect the keychain routing.
        assert macos_keychain.item_exists("svc", "acct") is True
        assert s._use_keychain() is False

    def test_capability_cache_is_process_local(self, temp_home: Path):
        s1 = self._macos_switcher()
        s1._keychain_usable_cache = False
        assert s1._use_keychain() is False
        # A fresh instance starts unknown and is optimistic again.
        s2 = self._macos_switcher()
        assert s2._keychain_usable_cache is None
        assert s2._use_keychain() is True

    def test_kc_call_propagates_programming_errors(self, temp_home: Path):
        # A bug (not a keychain failure) must propagate and leave the cache
        # untouched — it is not evidence the Keychain is unusable.
        s = self._macos_switcher()

        def boom(*a, **k):
            raise TypeError("bug")

        with pytest.raises(TypeError):
            s._kc_call(boom)
        assert s._keychain_usable_cache is None

    def test_active_write_does_not_swallow_programming_errors(
        self, temp_home: Path, monkeypatch
    ):
        # The narrowed fallback catch must let a real bug surface, not silently
        # route to file storage with the cache still claiming "usable".
        s = self._macos_switcher()

        def boom(*a, **k):
            raise TypeError("bug")

        monkeypatch.setattr(macos_keychain, "set_password", boom)
        with pytest.raises(TypeError):
            s._write_credentials('{"x":1}')

    # -- active store -----------------------------------------------------

    def test_active_write_keys_keychain_by_account_name(
        self, temp_home: Path, monkeypatch, block_real_keychain
    ):
        monkeypatch.delenv("USER", raising=False)
        s = self._macos_switcher()
        s._write_credentials('{"x":1}')
        acct = macos_keychain.keychain_account_name()
        assert (CLAUDE_CODE_KEYCHAIN_SERVICE, acct) in block_real_keychain.data
        # Never the legacy "user" default that mismatches Claude Code headless.
        assert (CLAUDE_CODE_KEYCHAIN_SERVICE, "user") not in block_real_keychain.data
        assert s._last_active_credentials_backend == "keychain"

    def test_active_read_prefers_keychain_then_file(
        self, temp_home: Path, block_real_keychain
    ):
        s = self._macos_switcher()
        acct = macos_keychain.keychain_account_name()
        block_real_keychain.data[(CLAUDE_CODE_KEYCHAIN_SERVICE, acct)] = "FROM-KC"
        cred = get_credentials_path()
        cred.parent.mkdir(parents=True, exist_ok=True)
        cred.write_text("FROM-FILE")
        # Keychain has data → wins (matches Claude Code's keychain-first read).
        assert s._read_credentials() == "FROM-KC"
        # Keychain empty → falls through to the plaintext file.
        del block_real_keychain.data[(CLAUDE_CODE_KEYCHAIN_SERVICE, acct)]
        assert s._read_credentials() == "FROM-FILE"

    def test_active_read_retries_transient_keychain_failure(
        self, temp_home: Path, monkeypatch, block_real_keychain
    ):
        # A single transient Keychain failure is retried; the second attempt
        # succeeds, so the read returns the credential rather than falling back.
        s = self._macos_switcher()
        acct = macos_keychain.keychain_account_name()
        block_real_keychain.data[(CLAUDE_CODE_KEYCHAIN_SERVICE, acct)] = "FROM-KC"
        monkeypatch.setattr("claude_swap.credentials._ACTIVE_READ_RETRY_DELAY", 0)

        calls = {"n": 0}
        real_get = macos_keychain.get_password

        def flaky_get(service, account):
            calls["n"] += 1
            if calls["n"] == 1:
                raise KeychainError("transient lock")
            return real_get(service, account)

        monkeypatch.setattr(macos_keychain, "get_password", flaky_get)

        result = s._read_active_credentials()
        assert result.value == "FROM-KC"
        assert result.keychain_unavailable is False
        assert calls["n"] == 2  # failed once, retried once, succeeded

    def test_active_read_keychain_unavailable_no_fallback(
        self, temp_home: Path, monkeypatch, block_real_keychain
    ):
        # OAuth Keychain unreadable on every attempt AND no file / managed-key
        # fallback → report keychain_unavailable, distinct from an empty slot.
        s = self._macos_switcher()
        monkeypatch.setattr(macos_keychain, "get_password", _raise_locked)
        monkeypatch.setattr("claude_swap.credentials._ACTIVE_READ_RETRY_DELAY", 0)
        assert not get_credentials_path().exists()

        result = s._read_active_credentials()
        assert result.value == ""
        assert result.keychain_unavailable is True
        # The legacy value-only contract still reads as empty.
        assert s._read_credentials() == ""

    def test_active_read_keychain_failure_covered_by_file(
        self, temp_home: Path, monkeypatch, block_real_keychain
    ):
        # A failed OAuth Keychain read covered by a plaintext file is NOT
        # "unavailable".
        s = self._macos_switcher()
        cred = get_credentials_path()
        cred.parent.mkdir(parents=True, exist_ok=True)
        cred.write_text("FROM-FILE")
        monkeypatch.setattr(macos_keychain, "get_password", _raise_locked)
        monkeypatch.setattr("claude_swap.credentials._ACTIVE_READ_RETRY_DELAY", 0)

        result = s._read_active_credentials()
        assert result.value == "FROM-FILE"
        assert result.keychain_unavailable is False

    def test_active_read_absent_item_is_not_keychain_unavailable(
        self, temp_home: Path, block_real_keychain
    ):
        # rc-44 "not found" (item genuinely absent, no raise) with no fallback is a
        # real empty slot → "no credentials", never "keychain unavailable". No
        # retry happens because nothing was raised.
        s = self._macos_switcher()
        assert not get_credentials_path().exists()

        result = s._read_active_credentials()
        assert result.value == ""
        assert result.keychain_unavailable is False

    def test_list_active_shows_keychain_unavailable(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict,
        monkeypatch, block_real_keychain, capsys
    ):
        # Regression: the active account rendered "no credentials" when the
        # Keychain was merely locked, nudging the user into an unnecessary
        # re-login. It must now read "keychain unavailable" instead.
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        s = self._macos_switcher()
        s._write_json(s.sequence_file, sample_sequence_data)
        monkeypatch.setattr(macos_keychain, "get_password", _raise_locked)
        monkeypatch.setattr("claude_swap.credentials._ACTIVE_READ_RETRY_DELAY", 0)
        assert not get_credentials_path().exists()

        s.list_accounts()
        out = capsys.readouterr().out
        assert "test@example.com" in out and "(active)" in out
        # The active row shows the intentional, actionable line — not the
        # misleading "no credentials" that prompted the re-login.
        assert "keychain unavailable — locked or in use; try again" in out

    def test_active_write_falls_back_to_file_and_clears_stale_keychain(
        self, temp_home: Path, monkeypatch, block_real_keychain
    ):
        s = self._macos_switcher()
        acct = macos_keychain.keychain_account_name()
        # A stale keychain entry that Claude Code's keychain-first read would
        # otherwise resurrect (#30337).
        block_real_keychain.data[(CLAUDE_CODE_KEYCHAIN_SERVICE, acct)] = "STALE"
        monkeypatch.setattr(macos_keychain, "set_password", _raise_locked)

        s._write_credentials('{"fresh":1}')

        assert s._last_active_credentials_backend == "file"
        assert get_credentials_path().read_text() == '{"fresh":1}'
        assert (CLAUDE_CODE_KEYCHAIN_SERVICE, acct) not in block_real_keychain.data

    def test_keychain_write_refreshes_existing_file(
        self, temp_home: Path, block_real_keychain
    ):
        # #86: an already-present shadow file must be rewritten (mtime bumped) so a
        # running Claude Code session invalidates its memoized token and hot-reloads.
        # #1414: it is rewritten, never deleted — a file-reading consumer stays valid.
        s = self._macos_switcher()
        cred = get_credentials_path()
        cred.parent.mkdir(parents=True, exist_ok=True)
        cred.write_text("OLD-CREDS")
        os.utime(cred, (1_000_000_000, 1_000_000_000))  # force an old mtime
        old_mtime_ns = cred.stat().st_mtime_ns

        s._write_credentials('{"fresh":1}')  # keychain usable → writes keychain

        assert s._last_active_credentials_backend == "keychain"
        assert cred.exists()  # never deleted (#1414)
        assert cred.read_text() == '{"fresh":1}'  # rewritten to the fresh account
        assert cred.stat().st_mtime_ns > old_mtime_ns  # the actual invalidation trigger

    def test_keychain_write_bumps_mtime_even_when_content_unchanged(
        self, temp_home: Path, block_real_keychain
    ):
        # The fix bumps mtime via atomic os.replace, so it fires even when the new
        # creds are byte-identical to the old — the purest test of the mechanism
        # (a content-only assertion would silently miss this).
        s = self._macos_switcher()
        cred = get_credentials_path()
        cred.parent.mkdir(parents=True, exist_ok=True)
        cred.write_text('{"same":1}')
        os.utime(cred, (1_000_000_000, 1_000_000_000))
        old_mtime_ns = cred.stat().st_mtime_ns

        s._write_credentials('{"same":1}')  # identical content

        assert cred.stat().st_mtime_ns > old_mtime_ns

    def test_keychain_write_does_not_create_absent_file(
        self, temp_home: Path, block_real_keychain
    ):
        # Keychain-only users keep their fileless posture: no .credentials.json is
        # created, so no plaintext credential lands on their disk (#86).
        s = self._macos_switcher()
        cred = get_credentials_path()
        assert not cred.exists()

        s._write_credentials('{"fresh":1}')  # keychain usable → writes keychain

        assert s._last_active_credentials_backend == "keychain"
        assert not cred.exists()

    def test_refresh_stale_file_is_best_effort(
        self, temp_home: Path, monkeypatch, block_real_keychain
    ):
        # The Keychain write is authoritative and already succeeded; a failure to
        # refresh the shadow file must warn, not fail the switch.
        s = self._macos_switcher()
        cred = get_credentials_path()
        cred.parent.mkdir(parents=True, exist_ok=True)
        cred.write_text("OLD-CREDS")

        def boom(_credentials):
            raise OSError("disk full")

        monkeypatch.setattr(s._store, "_write_active_credentials_file", boom)

        s._write_credentials('{"fresh":1}')  # must not raise

        assert s._last_active_credentials_backend == "keychain"

    # -- backup store: .enc-wins -----------------------------------------

    def _no_session(self, s):
        return (
            patch.object(s, "_live_session_pids", return_value=[]),
            patch.object(s, "_invalidate_session_credentials"),
        )

    def test_backup_read_enc_wins_over_stale_keychain(
        self, temp_home: Path, block_real_keychain
    ):
        s = self._macos_switcher()
        s._kc_write_backup("1", "a@example.com", "STALE-KC")
        s._write_backup_enc("1", "a@example.com", "FRESH-FILE")
        assert s._read_account_credentials("1", "a@example.com") == "FRESH-FILE"

    def test_backup_keychain_write_deletes_enc(
        self, temp_home: Path, block_real_keychain
    ):
        s = self._macos_switcher()
        s._write_backup_enc("1", "a@example.com", "OLD-FILE")
        p1, p2 = self._no_session(s)
        with p1, p2:
            s._write_account_credentials("1", "a@example.com", "NEW-KC")
        assert not s._backup_enc_path("1", "a@example.com").exists()
        assert s._read_account_credentials("1", "a@example.com") == "NEW-KC"

    def test_backup_enc_unlink_failure_rewrites_fresh(
        self, temp_home: Path, monkeypatch, block_real_keychain
    ):
        s = self._macos_switcher()
        s._write_backup_enc("1", "a@example.com", "OLD-FILE")
        enc = s._backup_enc_path("1", "a@example.com")

        orig_unlink = Path.unlink

        def flaky_unlink(self_path, *a, **k):
            if self_path == enc:
                raise OSError("cannot unlink")
            return orig_unlink(self_path, *a, **k)

        monkeypatch.setattr(Path, "unlink", flaky_unlink)
        p1, p2 = self._no_session(s)
        with p1, p2:
            s._write_account_credentials("1", "a@example.com", "NEW-KC")
        monkeypatch.setattr(Path, "unlink", orig_unlink)

        # Could not delete the .enc → it was rewritten fresh, so .enc-wins reads
        # still return the new creds (no stale shadow).
        assert base64.b64decode(enc.read_text()).decode() == "NEW-KC"
        assert s._read_account_credentials("1", "a@example.com") == "NEW-KC"

    def test_backup_file_mode_writes_enc_and_clears_keychain(
        self, temp_home: Path, monkeypatch, block_real_keychain
    ):
        s = self._macos_switcher()
        s._kc_write_backup("1", "a@example.com", "STALE-KC")  # seed keychain
        monkeypatch.setattr(macos_keychain, "set_password", _raise_locked)
        p1, p2 = self._no_session(s)
        with p1, p2:
            s._write_account_credentials("1", "a@example.com", "FILE-CREDS")
        assert s._read_account_credentials("1", "a@example.com") == "FILE-CREDS"
        # Stale keychain copy cleared (best-effort) so it can't resurface.
        assert (SECURITY_SERVICE, "account-1-a@example.com") not in block_real_keychain.data

    @pytest.mark.parametrize("bad", ["corrupt", "", "!!!!", "   ", "\n"])
    def test_backup_bad_enc_falls_back_to_keychain(
        self, temp_home: Path, block_real_keychain, bad
    ):
        # A corrupt / empty / whitespace .enc must not shadow a valid Keychain
        # backup. Permissive base64 would decode "!!!!"/"" to empty bytes and let
        # the junk file "win"; validate=True + a non-empty guard prevents that.
        s = self._macos_switcher()
        s._kc_write_backup("1", "a@example.com", "FROM-KC")
        s._backup_enc_path("1", "a@example.com").write_text(bad)
        assert s._read_account_credentials("1", "a@example.com") == "FROM-KC"

    def test_backup_delete_removes_both_backends(
        self, temp_home: Path, block_real_keychain
    ):
        s = self._macos_switcher()
        s._kc_write_backup("1", "a@example.com", "KC")
        s._write_backup_enc("1", "a@example.com", "FILE")
        s._delete_account_credentials("1", "a@example.com")
        assert not s._backup_enc_path("1", "a@example.com").exists()
        assert (SECURITY_SERVICE, "account-1-a@example.com") not in block_real_keychain.data

    # -- .prev retention on an unreadable current generation --------------

    def test_prev_keychain_item_retained_readable_control(
        self, temp_home: Path, block_real_keychain
    ):
        """CONTROL: a normal overwrite (current generation readable) must
        retain the outgoing bytes as a ``.prev`` Keychain item — the
        baseline against which the unreadable-path probe below is judged."""
        s = self._macos_switcher()
        s._kc_write_backup("1", "a@example.com", "gen-1")
        s._write_account_credentials("1", "a@example.com", "gen-2")
        assert (SECURITY_SERVICE, "account-1-a@example.com.prev") in block_real_keychain.data
        assert s._store._read_previous_backup("1", "a@example.com") == "gen-1"



    # -- healthy-Mac no-op guard & follow-up ------------------------------

    def test_healthy_mac_reads_create_no_files(
        self, temp_home: Path, block_real_keychain
    ):
        s = self._macos_switcher()
        s._kc_write_backup("1", "a@example.com", "KC")
        # Reading a backup must not materialize an .enc on a healthy keychain.
        assert s._read_account_credentials("1", "a@example.com") == "KC"
        assert not s._backup_enc_path("1", "a@example.com").exists()
        # Reading the (absent) active credential must not create the file.
        assert s._read_credentials() == ""
        assert not get_credentials_path().exists()

    def test_switch_followup_reflects_recorded_backend(
        self, temp_home: Path, capsys
    ):
        s = self._macos_switcher()
        s._last_active_credentials_backend = "file"
        s._print_switch_followup()
        assert "next message" in capsys.readouterr().out
        s._last_active_credentials_backend = "keychain"
        s._print_switch_followup()
        assert "30 seconds" in capsys.readouterr().out


class TestFormatUsageLines:
    """Test _format_usage_lines rendering, including per-model scoped windows."""

    def test_scoped_lines_render_per_model_with_at_limit_marker(self):
        usage = {
            "five_hour": {"pct": 7.0, "clock": "20:39", "countdown": "1h 30m"},
            "seven_day": {"pct": 72.0, "clock": "21:59", "countdown": "3h"},
            "scoped": [
                {"name": "Fable", "pct": 100.0, "clock": "21:59", "countdown": "3h"},
            ],
        }
        lines = _format_usage_lines(usage)
        assert lines[0].startswith("5h:")
        assert lines[1].startswith("7d:")
        fable = lines[2]
        assert fable.startswith("Fable:")
        assert "100%" in fable
        assert fable.rstrip().endswith("(!)")  # at/over limit marker

    def test_scoped_under_limit_has_no_marker(self):
        usage = {"scoped": [{"name": "Fable", "pct": 40.0, "clock": "21:59", "countdown": "3h"}]}
        lines = _format_usage_lines(usage)
        assert len(lines) == 1
        assert lines[0].startswith("Fable:")
        assert "40%" in lines[0]
        assert "resets 21:59" in lines[0]
        assert "in 3h" in lines[0]
        assert not lines[0].rstrip().endswith("(!)")

    def test_scoped_without_clock_renders_pct_only(self):
        usage = {"scoped": [{"name": "Fable", "pct": 100.0}]}
        lines = _format_usage_lines(usage)
        assert lines == ["Fable: 100%  (!)"]

    def test_countdown_recomputed_from_resets_at_not_cached_strings(self):
        # A measurement served from the store hours after its fetch still
        # carries the countdown frozen at fetch time; rendering must derive
        # the live value from resets_at instead (issue: "resets 15:59 in 17h"
        # printed when the reset was 15h away).
        from datetime import datetime, timedelta, timezone

        resets_at = (datetime.now(timezone.utc) + timedelta(hours=2, minutes=30)).isoformat()
        usage = {
            "seven_day": {
                "pct": 62.0,
                "resets_at": resets_at,
                "clock": "15:59",
                "countdown": "17h 0m",
            }
        }
        line = _format_usage_lines(usage)[0]
        assert "in 2h" in line
        assert "17h" not in line

    def test_reset_falls_back_to_cached_strings_without_resets_at(self):
        # Entries persisted by older versions have no resets_at — the
        # fetch-time strings are the best available then.
        usage = {"seven_day": {"pct": 62.0, "clock": "15:59", "countdown": "17h 0m"}}
        line = _format_usage_lines(usage)[0]
        assert "resets 15:59" in line
        assert "in 17h 0m" in line

    def test_reset_falls_back_on_unparseable_resets_at(self):
        usage = {
            "seven_day": {
                "pct": 62.0,
                "resets_at": "not-a-date",
                "clock": "15:59",
                "countdown": "17h 0m",
            }
        }
        line = _format_usage_lines(usage)[0]
        assert "resets 15:59" in line
        assert "in 17h 0m" in line

    def test_spend_clock_recomputed_from_resets_at(self):
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        resets_at = (now + timedelta(hours=2)).isoformat()
        expected_clock = oauth.format_reset(resets_at)[1]
        usage = {
            "spend": {
                "used": 1.0,
                "limit": 10.0,
                "pct": 10.0,
                "currency": "USD",
                "resets_at": resets_at,
                "clock": "stale-clock",
            }
        }
        line = _format_usage_lines(usage)[0]
        assert f"resets {expected_clock}" in line
        assert "stale-clock" not in line

    def test_no_scoped_key_renders_only_standard_windows(self):
        usage = {"five_hour": {"pct": 7.0}, "seven_day": {"pct": 72.0}}
        lines = _format_usage_lines(usage)
        assert all(not line.startswith("Fable:") for line in lines)

    def test_scoped_labels_align_columns_with_standard_windows(self):
        usage = {
            "five_hour": {"pct": 0.0},
            "seven_day": {"pct": 62.0, "clock": "Jul 5 08:59", "countdown": "1d 19h"},
            "scoped": [
                {"name": "Fable", "pct": 100.0, "clock": "Jul 5 08:59", "countdown": "1d 19h"},
            ],
        }
        lines = _format_usage_lines(usage)
        # Labels are padded to the widest ("Fable:"), so the % column lines up.
        assert lines[0] == "5h:      0%"
        assert lines[1].startswith("7d:     62%   resets Jul 5 08:59")
        assert lines[2].startswith("Fable: 100%   resets Jul 5 08:59")
        assert len({line.index("%") for line in lines}) == 1

    def test_standard_windows_alone_keep_legacy_layout(self):
        usage = {"five_hour": {"pct": 7.0, "clock": "20:39", "countdown": "1h 30m"}}
        lines = _format_usage_lines(usage)
        assert lines == ["5h:   7%   resets 20:39         in 1h 30m"]

    def test_seven_day_ahead_of_pace_marker(self):
        # 1 day elapsed of the week (resets_at 6 days out), 50% used -> far
        # ahead of the ~14% expected at that point (issue #125).
        from datetime import datetime, timedelta, timezone

        now = 1_700_000_000.0
        resets_at = datetime.fromtimestamp(now, tz=timezone.utc) + timedelta(days=6)
        usage = {"seven_day": {"pct": 50.0, "resets_at": resets_at.isoformat()}}
        line = _format_usage_lines(usage, now)[0]
        assert "(ahead of pace)" in line

    def test_five_hour_never_shows_pace_marker(self):
        # Pace applies only to weekly windows, never the 5h one (issue #125).
        from datetime import datetime, timedelta, timezone

        now = 1_700_000_000.0
        resets_at = datetime.fromtimestamp(now, tz=timezone.utc) + timedelta(hours=4)
        usage = {"five_hour": {"pct": 90.0, "resets_at": resets_at.isoformat()}}
        line = _format_usage_lines(usage, now)[0]
        assert "pace" not in line

    def test_scoped_ahead_of_pace_marker_when_under_limit(self):
        from datetime import datetime, timedelta, timezone

        now = 1_700_000_000.0
        resets_at = datetime.fromtimestamp(now, tz=timezone.utc) + timedelta(days=6)
        usage = {"scoped": [{"name": "Fable", "pct": 50.0, "resets_at": resets_at.isoformat()}]}
        line = _format_usage_lines(usage, now)[0]
        assert "(ahead of pace)" in line
        assert "(!)" not in line

    def test_no_pace_marker_without_fetched_at(self):
        # No fetched_at passed -> pace isn't computable, no marker (backward
        # compatible with callers that don't supply it).
        from datetime import datetime, timedelta, timezone

        resets_at = datetime.now(timezone.utc) + timedelta(days=6)
        usage = {"seven_day": {"pct": 50.0, "resets_at": resets_at.isoformat()}}
        line = _format_usage_lines(usage)[0]
        assert "pace" not in line

    def test_no_pace_marker_within_suppression_window_after_reset(self):
        # Just reset (elapsed ~0) -> suppressed even though pct looks "ahead".
        from datetime import datetime, timedelta, timezone

        now = 1_700_000_000.0
        resets_at = datetime.fromtimestamp(now, tz=timezone.utc) + timedelta(days=7, hours=-1)
        usage = {"seven_day": {"pct": 50.0, "resets_at": resets_at.isoformat()}}
        line = _format_usage_lines(usage, now)[0]
        assert "pace" not in line


def _read_safety_copy(switcher, entry_id: str) -> str:
    """Decode a preserved credential entry straight from its file (the store
    is write-only by design — no read helper exists in production code)."""
    path = switcher._store._stash_entry_path(entry_id)
    return base64.b64decode(path.read_text().strip(), validate=True).decode()


class TestProvenanceGuard:
    """Issue #117, fail-open hybrid: the identity oracle is advisory.

    Positively-foreign bytes are preserved and never written into a slot;
    an *unverifiable* divergence falls back to the exact pre-fix backup, so
    endpoint state never changes whether or how a switch completes.

    Two timelines, kept explicitly separate:

    - *normal operation* — Claude Code legitimately rotated the active
      account's token (same lineage, or resolved to the outgoing slot);
    - *fault injection* — the live store holds a foreign or unattributable
      credential (the poisoning precondition).
    """

    _setup_two_accounts = TestPerformSwitchPostDisplay._setup_two_accounts
    _install_store_patches = staticmethod(
        TestPerformSwitchPostDisplay._install_store_patches
    )

    _A1_BACKUP = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-stored-1", "refreshToken": "rt-1",
    }})

    def _run_switch(self, switcher, resolver=None, quiet=True):
        with patch.object(switcher, "list_accounts"), patch(
            "claude_swap.oauth.fetch_oauth_profile",
            side_effect=(lambda token: resolver) if resolver is not None
            else (lambda token: None),
        ):
            return switcher._perform_switch("2", emit_output=not quiet)

    # -- normal-operation timeline ---------------------------------------

    def test_byte_identical_live_skips_credential_backup(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        """Nothing rotated since cswap's own write → nothing to capture."""
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        creds_store[("1", "test@example.com")] = self._A1_BACKUP
        live_state = {"creds": self._A1_BACKUP}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        writes: list = []
        try:
            switcher._write_account_credentials = (
                lambda n, e, c: writes.append((n, e))
            )
            op = self._run_switch(switcher)
        finally:
            for p in patches:
                p.stop()
        assert writes == []  # credential backup skipped entirely
        assert op["warnings"] == []
        # Config backup still refreshed.
        assert configs_store.get(("1", "test@example.com"))

    def test_access_token_rotation_same_lineage_backs_up(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        """Same refresh token, new access token → provenance is local, no
        network needed, backup captures the rotation."""
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        creds_store[("1", "test@example.com")] = self._A1_BACKUP
        rotated = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-fresh-1", "refreshToken": "rt-1", "expiresAt": 9,
        }})
        live_state = {"creds": rotated}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        try:
            with patch(
                "claude_swap.oauth.fetch_oauth_profile",
            ) as profile:
                with patch.object(switcher, "list_accounts"):
                    op = switcher._perform_switch("2")
        finally:
            for p in patches:
                p.stop()
        profile.assert_not_called()
        assert creds_store[("1", "test@example.com")] == rotated
        assert op["warnings"] == []

    def test_full_rotation_resolved_to_outgoing_slot_backs_up(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        """Refresh-token rotation of the *same* account (the routine case a
        long-lived Claude Code session produces) re-syncs into the slot."""
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        creds_store[("1", "test@example.com")] = self._A1_BACKUP
        rotated = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-fresh-1", "refreshToken": "rt-1-rotated",
        }})
        live_state = {"creds": rotated}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        try:
            op = self._run_switch(switcher, resolver={
                "uuid": "uuid-1", "email": "test@example.com",
                "organizationUuid": "",
            })
        finally:
            for p in patches:
                p.stop()
        assert creds_store[("1", "test@example.com")] == rotated
        assert op["warnings"] == []
        assert switcher.list_unclaimed_credentials() == {}

    def test_resolution_backfills_empty_slot_uuid(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        sample_sequence_data["accounts"]["1"]["uuid"] = ""
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        creds_store[("1", "test@example.com")] = self._A1_BACKUP
        live_state = {"creds": json.dumps({"claudeAiOauth": {
            "accessToken": "sk-f", "refreshToken": "rt-1-rotated",
        }})}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        try:
            self._run_switch(switcher, resolver={
                "uuid": "uuid-resolved", "email": "test@example.com",
                "organizationUuid": "",
            })
        finally:
            for p in patches:
                p.stop()
        data = switcher._get_sequence_data()
        assert data["accounts"]["1"]["uuid"] == "uuid-resolved"

    # -- fault-injection timeline -----------------------------------------

    def test_foreign_credential_preserved_never_backed_into_any_slot(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        """The poisoning precondition: live bytes belong to another managed
        slot (uuid-positive). They must be preserved as a safety copy — not
        written into the outgoing slot, and not routed into the resolved slot
        either (identity proves ownership, not generation freshness). Here the
        foreign slot is also the switch target: the switch still activates the
        target's *stored* backup, never the displaced live bytes."""
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        creds_store[("1", "test@example.com")] = self._A1_BACKUP
        a2_backup = creds_store[("2", "account2@example.com")]
        foreign = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-2-rotated", "refreshToken": "rt-2-rotated",
        }})
        live_state = {"creds": foreign}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        try:
            op = self._run_switch(switcher, resolver={
                "uuid": "uuid-2", "email": "account2@example.com",
                "organizationUuid": "",
            })
        finally:
            for p in patches:
                p.stop()
        # Outgoing slot untouched; resolved slot untouched.
        assert creds_store[("1", "test@example.com")] == self._A1_BACKUP
        assert creds_store[("2", "account2@example.com")] == a2_backup
        # Foreign bytes preserved byte-exactly.
        entries = switcher.list_unclaimed_credentials()
        assert len(entries) == 1
        (entry_id,) = entries
        assert _read_safety_copy(switcher, entry_id) == foreign
        assert entries[entry_id]["resolvedIdentity"]["uuid"] == "uuid-2"
        assert any(
            "ownership mismatch" in w and "Account-2" in w
            for w in op["warnings"]
        )
        # The switch itself proceeded, onto the stored backup.
        assert json.loads(live_state["creds"])["claudeAiOauth"]["accessToken"] == "sk-stale-2"

    def test_foreign_synced_lineage_warns_without_any_write(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        """Foreign bytes whose lineage already sits in that slot's backup:
        nothing needs preserving, nothing may be written — warn only."""
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        creds_store[("1", "test@example.com")] = self._A1_BACKUP
        a2_backup = creds_store[("2", "account2@example.com")]
        live_state = {"creds": a2_backup}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        try:
            op = self._run_switch(switcher, resolver={
                "uuid": "uuid-2", "email": "account2@example.com",
                "organizationUuid": "",
            })
        finally:
            for p in patches:
                p.stop()
        assert creds_store[("1", "test@example.com")] == self._A1_BACKUP
        assert creds_store[("2", "account2@example.com")] == a2_backup
        assert switcher.list_unclaimed_credentials() == {}
        assert any("already matches Account-2" in w for w in op["warnings"])

    def test_alien_credential_preserved_and_skipped(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        """Resolved to no managed slot: preserve, warn, proceed — the message
        can't name a slot, so it recommends a plain `cswap add`."""
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        creds_store[("1", "test@example.com")] = self._A1_BACKUP
        alien = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-x", "refreshToken": "rt-x",
        }})
        live_state = {"creds": alien}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        try:
            op = self._run_switch(switcher, resolver={
                "uuid": "uuid-unmanaged", "email": "elsewhere@example.com",
                "organizationUuid": "",
            })
        finally:
            for p in patches:
                p.stop()
        assert creds_store[("1", "test@example.com")] == self._A1_BACKUP
        entries = switcher.list_unclaimed_credentials()
        assert len(entries) == 1
        (entry_id,) = entries
        assert _read_safety_copy(switcher, entry_id) == alien
        assert any(
            "does not match a managed account" in w for w in op["warnings"]
        )

    def test_blank_stored_uuid_email_match_is_alien_not_foreign(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        """A cross-slot attribution must be uuid-positive: an email+org match
        against a slot with no recorded uuid is preserved as alien, and that
        slot is never named or touched."""
        sample_sequence_data["accounts"]["2"]["uuid"] = ""
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        creds_store[("1", "test@example.com")] = self._A1_BACKUP
        a2_backup = creds_store[("2", "account2@example.com")]
        drifted = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-d", "refreshToken": "rt-d",
        }})
        live_state = {"creds": drifted}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        try:
            op = self._run_switch(switcher, resolver={
                "uuid": "uuid-2-real", "email": "account2@example.com",
                "organizationUuid": "",
            })
        finally:
            for p in patches:
                p.stop()
        assert creds_store[("1", "test@example.com")] == self._A1_BACKUP
        assert creds_store[("2", "account2@example.com")] == a2_backup
        assert len(switcher.list_unclaimed_credentials()) == 1
        assert any(
            "does not match a managed account" in w for w in op["warnings"]
        )

    def test_partial_identity_uuid_only_matching_outgoing_slot_backs_up(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        """A response that dropped email/organization but whose uuid equals
        the outgoing slot's must classify own-rotated: partial data must
        never turn a legitimate rotation into preserve-and-skip (that would
        recreate the fail-closed stale-slot behavior on schema drift)."""
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        creds_store[("1", "test@example.com")] = self._A1_BACKUP
        rotated = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-f", "refreshToken": "rt-1-rotated",
        }})
        live_state = {"creds": rotated}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        try:
            op = self._run_switch(switcher, resolver={
                "uuid": "uuid-1", "email": None, "organizationUuid": None,
            })
        finally:
            for p in patches:
                p.stop()
        assert creds_store[("1", "test@example.com")] == rotated
        assert switcher.list_unclaimed_credentials() == {}
        assert op["warnings"] == []

    def test_partial_identity_uuid_match_with_slot_org_recorded(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        """Same, with the outgoing slot recording an organization: org must
        agree only when *both* sides carry one, so a uuid-only response
        still resolves to the outgoing slot."""
        sample_sequence_data["accounts"]["1"]["organizationUuid"] = "org-1"
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        data = switcher._get_sequence_data()
        rotated = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-f", "refreshToken": "rt-1-rotated",
        }})
        with patch.object(
            switcher, "_read_account_credentials",
            return_value=self._A1_BACKUP,
        ):
            kind, foreign_slot = switcher._classify_outgoing_credential(
                "1", "test@example.com", rotated,
                {"live": rotated,
                 "resolved": {"uuid": "uuid-1", "email": None,
                              "organizationUuid": None}},
                data,
            )
        assert (kind, foreign_slot) == ("own-rotated", None)

    def test_partial_identity_matching_nothing_falls_open(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        """A response missing email/organization that matches no slot is
        indistinguishable from schema drift → unresolved (pre-fix backup),
        never alien (preserve-and-skip)."""
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        creds_store[("1", "test@example.com")] = self._A1_BACKUP
        mystery = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-x", "refreshToken": "rt-x",
        }})
        live_state = {"creds": mystery}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        try:
            op = self._run_switch(switcher, resolver={
                "uuid": "uuid-nobody", "email": None,
                "organizationUuid": None,
            })
        finally:
            for p in patches:
                p.stop()
        assert creds_store[("1", "test@example.com")] == mystery
        assert switcher.list_unclaimed_credentials() == {}
        assert op["warnings"] == []

    def test_foreign_attribution_survives_missing_email(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        """uuid+org is a positive cross-slot match even without an email —
        preserve-and-skip stays available where the evidence is complete
        enough to be positive."""
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        creds_store[("1", "test@example.com")] = self._A1_BACKUP
        a2_backup = creds_store[("2", "account2@example.com")]
        foreign = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-2-rotated", "refreshToken": "rt-2-rotated",
        }})
        live_state = {"creds": foreign}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        try:
            op = self._run_switch(switcher, resolver={
                "uuid": "uuid-2", "email": None, "organizationUuid": "",
            })
        finally:
            for p in patches:
                p.stop()
        assert creds_store[("1", "test@example.com")] == self._A1_BACKUP
        assert creds_store[("2", "account2@example.com")] == a2_backup
        assert len(switcher.list_unclaimed_credentials()) == 1
        assert any("Account-2" in w for w in op["warnings"])

    def test_unresolvable_mismatch_backs_up_pre_fix(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        """The fail-open core: offline / endpoint failure means the identity
        oracle is silent, and the switch behaves exactly pre-fix — the
        divergent bytes are backed into the outgoing slot (most such
        divergences are the account's own rotation; skipping would leave the
        slot holding a consumed token), with no safety copy and no
        user-facing warning."""
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        creds_store[("1", "test@example.com")] = self._A1_BACKUP
        mystery = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-x", "refreshToken": "rt-x",
        }})
        live_state = {"creds": mystery}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        try:
            op = self._run_switch(switcher, resolver=None)
        finally:
            for p in patches:
                p.stop()
        # Pre-fix backup happened; the switch completed quietly.
        assert creds_store[("1", "test@example.com")] == mystery
        assert switcher.list_unclaimed_credentials() == {}
        assert op["warnings"] == []
        assert json.loads(live_state["creds"])["claudeAiOauth"]["accessToken"] == "sk-stale-2"

    def test_cached_foreign_verdict_survives_a_failed_switch_time_probe(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        """A collect-pass probe already condemned this lineage (the verdict
        that routed autoswitch to this very switch). When the switch-time
        probe then fails transiently, the fail-open pre-fix backup must NOT
        run — that write is the poisoning the verdict proved. Instead the
        credential is stashed, the outgoing backup stays untouched, and the
        switch still completes onto the target."""
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        creds_store[("1", "test@example.com")] = self._A1_BACKUP
        foreign = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-foreign", "refreshToken": "rt-foreign",
        }})
        switcher._probe_verdicts[
            switcher._lineage_key(
                "1", "test@example.com",
                oauth.credential_fingerprint(foreign),
            )
        ] = False
        live_state = {"creds": foreign}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        try:
            op = self._run_switch(switcher, resolver=None)
        finally:
            for p in patches:
                p.stop()
        # Backup untouched, credential preserved, switch completed.
        assert creds_store[("1", "test@example.com")] == self._A1_BACKUP
        stash = switcher.list_unclaimed_credentials()
        assert len(stash) == 1
        assert next(iter(stash.values()))["reason"] == "known-foreign"
        assert any("previously identified" in w for w in op["warnings"])
        assert json.loads(live_state["creds"])["claudeAiOauth"]["accessToken"] == "sk-stale-2"

    def test_profile_exception_falls_back_to_pre_fix_backup(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        """A raising profile call must be indistinguishable from None: the
        switch completes with the pre-fix backup."""
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        creds_store[("1", "test@example.com")] = self._A1_BACKUP
        mystery = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-x", "refreshToken": "rt-x",
        }})
        live_state = {"creds": mystery}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        try:
            with patch.object(switcher, "list_accounts"), patch(
                "claude_swap.oauth.fetch_oauth_profile",
                side_effect=OSError("network down"),
            ):
                op = switcher._perform_switch("2", emit_output=False)
        finally:
            for p in patches:
                p.stop()
        assert creds_store[("1", "test@example.com")] == mystery
        assert switcher.list_unclaimed_credentials() == {}
        assert op["warnings"] == []

    def test_safety_copy_failure_aborts_before_live_overwrite(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        """Preservation is the safety boundary for positively-foreign bytes:
        no safety copy, no switch. (Never reachable from endpoint failure —
        the unresolved path writes no safety copy.)"""
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        creds_store[("1", "test@example.com")] = self._A1_BACKUP
        mystery = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-x", "refreshToken": "rt-x",
        }})
        live_state = {"creds": mystery}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        try:
            with patch.object(
                switcher._store, "_write_unclaimed_credential",
                side_effect=OSError("disk full"),
            ):
                with pytest.raises(Exception):
                    self._run_switch(switcher, resolver={
                        "uuid": "uuid-unmanaged",
                        "email": "elsewhere@example.com",
                        "organizationUuid": "",
                    })
        finally:
            for p in patches:
                p.stop()
        # Nothing moved: live store, outgoing backup both untouched.
        assert live_state["creds"] == mystery
        assert creds_store[("1", "test@example.com")] == self._A1_BACKUP

    def test_wiped_live_credential_never_overwrites_a_token_bearing_backup(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        """Claude Code reacts to ``invalid_grant`` by emptying the live
        store's token fields in place (observed on 2.1.181: wrapper and
        metadata kept, ``accessToken``/``refreshToken`` → ``""``). Such a
        blob resolves to nobody (no access token → oracle silent), so the
        unresolved fail-open used to copy it over the slot's backup —
        destroying the only surviving refresh token. A wiped live credential
        must never be written into a slot."""
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        creds_store[("1", "test@example.com")] = self._A1_BACKUP
        wiped = json.dumps({"claudeAiOauth": {
            "accessToken": "", "refreshToken": "",
            "expiresAt": 1000, "scopes": ["user:profile"],
            "subscriptionType": "max",
        }})
        live_state = {"creds": wiped}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        try:
            op = self._run_switch(switcher, resolver=None)
        finally:
            for p in patches:
                p.stop()
        # The backup survived; the switch itself completed onto account 2.
        assert creds_store[("1", "test@example.com")] == self._A1_BACKUP
        assert json.loads(live_state["creds"])["claudeAiOauth"][
            "accessToken"] == "sk-stale-2"
        # Nothing worth stashing in an empty blob.
        assert switcher.list_unclaimed_credentials() == {}
        # The user is told the slot needs a fresh login.
        assert any("log in" in w.lower() for w in op["warnings"])

    def test_wiped_live_matching_wiped_backup_stays_quiet(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        """Byte-identical wiped live and backup: the slot already lost its
        tokens before this switch — nothing left to protect, no new warning
        (the damage predates the switch and re-login surfaces elsewhere)."""
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        wiped = json.dumps({"claudeAiOauth": {
            "accessToken": "", "refreshToken": "", "expiresAt": 1000,
        }})
        creds_store[("1", "test@example.com")] = wiped
        live_state = {"creds": wiped}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        try:
            op = self._run_switch(switcher, resolver=None)
        finally:
            for p in patches:
                p.stop()
        assert creds_store[("1", "test@example.com")] == wiped
        assert op["warnings"] == []

    def test_moved_bytes_between_prefetch_and_lock_fall_to_unresolved(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        """A pre-lock resolution only binds to the bytes it resolved: when
        the live store moved in between, the stale answer is discarded and
        the switch falls back to the pre-fix backup of the current bytes."""
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        creds_store[("1", "test@example.com")] = self._A1_BACKUP
        provenance = {
            "live": "something-else-entirely",
            "resolved": {"uuid": "uuid-2", "email": "account2@example.com",
                         "organizationUuid": ""},
        }
        moved = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-m", "refreshToken": "rt-m",
        }})
        live_state = {"creds": moved}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        try:
            with patch.object(switcher, "list_accounts"):
                op = switcher._perform_switch(
                    "2", emit_output=False, provenance=provenance
                )
        finally:
            for p in patches:
                p.stop()
        # Stale resolution rejected → unresolved → pre-fix backup, no copy.
        assert creds_store[("1", "test@example.com")] == moved
        assert switcher.list_unclaimed_credentials() == {}
        assert op["warnings"] == []


class TestSelfSwitchProvenance:
    """The already-active short-circuits must not hide a diverged live login."""

    _setup_two_accounts = TestPerformSwitchPostDisplay._setup_two_accounts
    _install_store_patches = staticmethod(
        TestPerformSwitchPostDisplay._install_store_patches
    )

    def test_matching_self_switch_is_noop(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        backup = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-1", "refreshToken": "rt-1",
        }})
        creds_store[("1", "test@example.com")] = backup
        live_state = {"creds": backup}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        try:
            result = switcher.switch_to("1", json_output=True)
        finally:
            for p in patches:
                p.stop()
        assert result["switched"] is False
        assert result["reason"] == "already-active"

    def test_diverged_unresolvable_self_switch_noops_silently(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        """Endpoint trouble must be invisible on the self-switch path too:
        an unclassifiable divergence is an ordinary already-active no-op
        (exact pre-fix behavior — no mutation, no user-facing warning; the
        diagnostic goes to the log). Leaving everything untouched is also the
        safe write: activating the stored backup over an unverified live
        credential could replace a fresh rotated token with its consumed
        ancestor."""
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        backup = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-1", "refreshToken": "rt-1",
        }})
        diverged = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-1-new", "refreshToken": "rt-1-rotated",
        }})
        creds_store[("1", "test@example.com")] = backup
        live_state = {"creds": diverged}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        try:
            with patch("claude_swap.oauth.fetch_oauth_profile", return_value=None):
                result = switcher.switch_to("1", json_output=True)
        finally:
            for p in patches:
                p.stop()
        assert result["switched"] is False
        assert result["reason"] == "already-active"
        assert result.get("warnings", []) == []
        assert live_state["creds"] == diverged  # left untouched
        assert creds_store[("1", "test@example.com")] == backup

    def test_diverged_resolved_self_switch_reconciles(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        """A rotation proven to be the slot's own gets re-synced to backup."""
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        backup = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-1", "refreshToken": "rt-1",
        }})
        rotated = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-1-new", "refreshToken": "rt-1-rotated",
        }})
        creds_store[("1", "test@example.com")] = backup
        configs_store[("1", "test@example.com")] = json.dumps({
            "oauthAccount": {"emailAddress": "test@example.com", "accountUuid": "uuid-1"},
        })
        live_state = {"creds": rotated}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        try:
            with patch(
                "claude_swap.oauth.fetch_oauth_profile",
                return_value={"uuid": "uuid-1", "email": "test@example.com",
                              "organizationUuid": ""},
            ), patch.object(switcher, "list_accounts"):
                result = switcher.switch_to("1", json_output=True)
        finally:
            for p in patches:
                p.stop()
        # The rotation was captured into the slot's backup.
        assert creds_store[("1", "test@example.com")] == rotated
        assert result["switched"] is False or result["to"]["number"] == 1


class TestDuplicateAccountDetection:
    def _switcher(self, temp_home, sample_sequence_data):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)
        return switcher

    def test_same_fingerprint_across_slots_flagged(
        self, temp_home, sample_sequence_data,
    ):
        switcher = self._switcher(temp_home, sample_sequence_data)
        same = json.dumps({"claudeAiOauth": {
            "accessToken": "sk", "refreshToken": "rt-shared",
        }})
        info = [
            (1, "account1@example.com", "", "", True, same, ""),
            (2, "account2@example.com", "", "", False, same, ""),
        ]
        warnings = switcher._duplicate_account_warnings(info)
        assert len(warnings) == 1
        assert "Account-1 and Account-2" in warnings[0]

    def test_same_uuid_across_slots_flagged(
        self, temp_home, sample_sequence_data,
    ):
        sample_sequence_data["accounts"]["2"]["uuid"] = "uuid-1"
        switcher = self._switcher(temp_home, sample_sequence_data)
        info = [
            (1, "account1@example.com", "", "", True, "creds-a", ""),
            (2, "account2@example.com", "", "", False, "creds-b", ""),
        ]
        warnings = switcher._duplicate_account_warnings(info)
        assert len(warnings) == 1
        assert "both authenticate" in warnings[0]

    def test_empty_uuids_never_match_each_other(
        self, temp_home, sample_sequence_data,
    ):
        """add-token placeholders (uuid "") must not false-positive."""
        sample_sequence_data["accounts"]["1"]["uuid"] = ""
        sample_sequence_data["accounts"]["2"]["uuid"] = ""
        switcher = self._switcher(temp_home, sample_sequence_data)
        info = [
            (1, "setup-token-1@token.local", "", "", True, "creds-a", ""),
            (2, "setup-token-2@token.local", "", "", False, "creds-b", ""),
        ]
        assert switcher._duplicate_account_warnings(info) == []

    def test_clean_accounts_produce_no_warnings(
        self, temp_home, sample_sequence_data,
    ):
        switcher = self._switcher(temp_home, sample_sequence_data)
        info = [
            (1, "account1@example.com", "", "", True,
             json.dumps({"claudeAiOauth": {"refreshToken": "rt-1"}}), ""),
            (2, "account2@example.com", "", "", False,
             json.dumps({"claudeAiOauth": {"refreshToken": "rt-2"}}), ""),
        ]
        assert switcher._duplicate_account_warnings(info) == []


class TestLockstepUsageDetection:
    """Heuristic detector for the different-generation collapse (issue #117):
    fingerprints and sequence identities look distinct, but both slots report
    the same account's usage — identical percentages and reset instants."""

    def _switcher(self, temp_home, sample_sequence_data):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)
        return switcher

    @staticmethod
    def _info(n=2):
        return [
            (i, f"account{i}@example.com", "", "", i == 1, f"creds-{i}", "")
            for i in range(1, n + 1)
        ]

    @staticmethod
    def _entry(h5_pct, h5_reset, d7_pct, d7_reset):
        usage = {}
        if h5_pct is not None:
            usage["five_hour"] = {"pct": h5_pct}
            if h5_reset is not None:
                usage["five_hour"]["resets_at"] = h5_reset
        if d7_pct is not None:
            usage["seven_day"] = {"pct": d7_pct}
            if d7_reset is not None:
                usage["seven_day"]["resets_at"] = d7_reset
        return UsageEntry(last_good=usage, fetched_at=time.time(), age_s=0.0)

    def test_identical_usage_and_resets_flagged(
        self, temp_home, sample_sequence_data,
    ):
        switcher = self._switcher(temp_home, sample_sequence_data)
        entries = {
            "1": self._entry(25.0, "2026-07-10T12:00:00Z", 60.0, "2026-07-14T00:00:00Z"),
            "2": self._entry(25.0, "2026-07-10T12:00:00Z", 60.0, "2026-07-14T00:00:00Z"),
        }
        warnings = switcher._lockstep_usage_warnings(self._info(), entries)
        assert len(warnings) == 1
        assert "Account-1 and Account-2" in warnings[0]
        assert "may be the same account" in warnings[0]

    def test_differing_resets_not_flagged(self, temp_home, sample_sequence_data):
        switcher = self._switcher(temp_home, sample_sequence_data)
        entries = {
            "1": self._entry(25.0, "2026-07-10T12:00:00Z", 60.0, "2026-07-14T00:00:00Z"),
            "2": self._entry(25.0, "2026-07-10T13:00:00Z", 60.0, "2026-07-14T00:00:00Z"),
        }
        assert switcher._lockstep_usage_warnings(self._info(), entries) == []

    def test_idle_accounts_without_resets_not_flagged(
        self, temp_home, sample_sequence_data,
    ):
        """Two fresh accounts at 0% with no reset scheduled are
        indistinguishable — never flag them."""
        switcher = self._switcher(temp_home, sample_sequence_data)
        entries = {
            "1": self._entry(0.0, None, 0.0, None),
            "2": self._entry(0.0, None, 0.0, None),
        }
        assert switcher._lockstep_usage_warnings(self._info(), entries) == []

    def test_sentinel_usage_never_compared(self, temp_home, sample_sequence_data):
        """API-key slots (and other sentinel states) carry no comparable
        usage."""
        switcher = self._switcher(temp_home, sample_sequence_data)
        entries = {
            "1": UsageEntry(sentinel="api-key"),
            "2": UsageEntry(sentinel="api-key"),
        }
        assert switcher._lockstep_usage_warnings(self._info(), entries) == []

    def test_payload_carries_lockstep_warnings_additively(
        self, temp_home, sample_sequence_data,
    ):
        switcher = self._switcher(temp_home, sample_sequence_data)
        lockstep = {
            "1": self._entry(25.0, "2026-07-10T12:00:00Z", 60.0, "2026-07-14T00:00:00Z"),
            "2": self._entry(25.0, "2026-07-10T12:00:00Z", 60.0, "2026-07-14T00:00:00Z"),
        }
        payload = switcher._build_list_payload(self._info(), lockstep)
        assert len(payload["lockstepUsageWarnings"]) == 1
        clean = {
            "1": self._entry(25.0, "2026-07-10T12:00:00Z", 60.0, "2026-07-14T00:00:00Z"),
            "2": self._entry(30.0, "2026-07-10T13:00:00Z", 10.0, "2026-07-15T00:00:00Z"),
        }
        payload = switcher._build_list_payload(self._info(), clean)
        assert "lockstepUsageWarnings" not in payload


class TestStashAndRetentionStore:
    """CredentialStore: unclaimed stash + previous-generation retention."""

    def _switcher(self, temp_home):
        switcher = ClaudeAccountSwitcher()
        switcher.platform = Platform.LINUX
        switcher._setup_directories()
        switcher._init_sequence_file()
        return switcher

    def test_safety_copy_write_and_list(self, temp_home):
        """The store is write-only in production; bytes land as base64."""
        switcher = self._switcher(temp_home)
        store = switcher._store
        entry_id = store._write_unclaimed_credential(
            "secret-bytes", {"reason": "alien"},
        )
        assert _read_safety_copy(switcher, entry_id) == "secret-bytes"
        entries = store._list_unclaimed_credentials()
        assert entries[entry_id]["reason"] == "alien"
        assert entries[entry_id]["createdAt"]

    @pytest.mark.skipif(sys.platform == "win32", reason="File permissions work differently on Windows")
    def test_safety_copy_file_is_owner_only(self, temp_home):
        switcher = self._switcher(temp_home)
        store = switcher._store
        entry_id = store._write_unclaimed_credential("secret-bytes", {})
        mode = store._stash_entry_path(entry_id).stat().st_mode & 0o777
        assert mode == 0o600

    def test_two_snapshots_same_refresh_token_never_collide(self, temp_home):
        switcher = self._switcher(temp_home)
        store = switcher._store
        a = json.dumps({"claudeAiOauth": {"accessToken": "sk-a", "refreshToken": "rt"}})
        b = json.dumps({"claudeAiOauth": {"accessToken": "sk-b", "refreshToken": "rt"}})
        id_a = store._write_unclaimed_credential(a, {})
        id_b = store._write_unclaimed_credential(b, {})
        assert id_a != id_b
        assert _read_safety_copy(switcher, id_a) == a
        assert _read_safety_copy(switcher, id_b) == b

    def test_orphaned_entry_file_still_listed(self, temp_home):
        """Bytes without manifest metadata must stay visible, not vanish."""
        switcher = self._switcher(temp_home)
        store = switcher._store
        entry_id = store._write_unclaimed_credential("bytes", {})
        store._stash_manifest_path().unlink()
        entries = store._list_unclaimed_credentials()
        assert entry_id in entries

    def test_prev_generation_retained_on_overwrite(self, temp_home):
        switcher = self._switcher(temp_home)
        store = switcher._store
        store._write_account_credentials("1", "a@b.c", "gen-1")
        store._write_account_credentials("1", "a@b.c", "gen-2")
        assert store._read_account_credentials("1", "a@b.c") == "gen-2"
        assert store._read_previous_backup("1", "a@b.c") == "gen-1"
        # Same-value rewrite doesn't clobber the retained generation.
        store._write_account_credentials("1", "a@b.c", "gen-2")
        assert store._read_previous_backup("1", "a@b.c") == "gen-1"

    def test_prev_removed_with_account(self, temp_home):
        switcher = self._switcher(temp_home)
        store = switcher._store
        store._write_account_credentials("1", "a@b.c", "gen-1")
        store._write_account_credentials("1", "a@b.c", "gen-2")
        store._delete_account_credentials("1", "a@b.c")
        assert store._read_previous_backup("1", "a@b.c") == ""

    @pytest.mark.skipif(
        sys.platform == "win32" or os.geteuid() == 0,
        reason="needs POSIX permission semantics (non-root)",
    )
    def test_unreadable_current_backup_warns_instead_of_silent_no_op(
        self, temp_home, caplog,
    ):
        """I-2: ``_retain_previous_backup`` used the plain reader, so an
        UNREADABLE current backup (``.enc`` exists but cannot be read) and a
        genuinely ABSENT one both hit ``if not current: return`` — the same
        silent no-op. Retention is best-effort, but "the backup is intact
        and merely unreadable this instant" must not look identical in the
        logs to "there was never anything to retain".

        The WARNING is the whole contract here. Rounds 8-9 additionally
        checkpointed the incoming bytes as a ``.prev``; round 10 withdrew
        that (it shadowed a real Keychain ``.prev`` on a locked keychain),
        so this asserts only that the unreadable case is distinguishable
        from the absent one in the log.
        """
        import logging

        switcher = self._switcher(temp_home)
        store = switcher._store
        store._write_account_credentials("1", "a@b.c", "gen-1")
        enc = store._backup_enc_path("1", "a@b.c")

        caplog.clear()
        enc.chmod(0o000)
        try:
            with caplog.at_level(logging.WARNING, logger="claude-swap"):
                store._retain_previous_backup("1", "a@b.c", "gen-2")
        finally:
            enc.chmod(0o600)

        assert any(
            "could not be retained" in r.message.lower()
            or "could not be read" in r.message.lower()
            for r in caplog.records
            if "retain" in r.message.lower()
        ), (
            "DEFECT: an unreadable current backup at retention time produced "
            "no warning distinguishing it from a genuinely absent one"
        )

    @pytest.mark.skipif(
        sys.platform == "win32" or os.geteuid() == 0,
        reason="needs POSIX permission semantics (non-root)",
    )
    def test_strict_clear_final_belt_fails_closed_on_unreadable_enc(
        self, temp_home,
    ):
        """I-3: ``delete_account_credentials_strict``'s docstring says a
        read-back "cannot provide" the fail-closed guarantee because "the
        normal reader converts [read] errors to ``\"\"``, which conflates
        'absent' with 'unreadable'". The final belt then called exactly
        that normal reader. An unlink that appears to succeed (e.g. a stale
        fd, a filesystem quirk) but leaves an unreadable ``.enc`` behind
        must abort the commit, not report success.
        """
        from claude_swap.exceptions import CredentialError

        switcher = self._switcher(temp_home)
        store = switcher._store
        store._write_account_credentials("1", "a@b.c", "live-material")
        enc = store._backup_enc_path("1", "a@b.c")

        with patch.object(type(enc), "unlink", return_value=None):
            enc.chmod(0o000)
            try:
                with pytest.raises(CredentialError):
                    store.delete_account_credentials_strict("1", "a@b.c")
            finally:
                enc.chmod(0o600)

        assert enc.exists() and enc.stat().st_size > 0, (
            "premise: the no-op'd unlink left material behind"
        )


class TestActiveRefreshProvenance:
    """_fetch_active_usage must not rotate-and-persist an unattributed
    credential — same hazard class as the switch-time blind backup."""

    _LIVE = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-live", "refreshToken": "rt-live", "expiresAt": 1000,
    }})

    def _switcher(self, sample_sequence_data):
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)
        return switcher

    def test_unattributed_live_grant_is_never_consumed(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        """The live bytes' own grant must never be POSTed when its lineage
        can't be attributed to the slot — recovery goes through the slot's
        stored backup instead (whose grant IS the slot's by definition)."""
        switcher = self._switcher(sample_sequence_data)
        backup = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-stored", "refreshToken": "rt-stored",
            "expiresAt": 1000,
        }})
        refreshed = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-new", "refreshToken": "rt-new",
            "expiresAt": 9_999_999_999_000,
        }})
        consumed = []

        def mock_refresh(credentials, **kw):
            consumed.append(credentials)
            return oauth.RefreshOutcome(refreshed, None)

        with patch.object(switcher, "_read_credentials", return_value=self._LIVE), \
             patch.object(switcher, "_read_account_credentials", return_value=backup), \
             patch.object(switcher, "_live_session_pids", return_value=[]), \
             patch.object(switcher, "_write_credentials"), \
             patch.object(switcher, "_write_account_credentials"), \
             patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   side_effect=mock_refresh), \
             patch("claude_swap.oauth.try_fetch_usage_for_account",
                   return_value=oauth.UsageOutcome({"five_hour": {"pct": 1}})):
            switcher._fetch_active_usage("1", "test@example.com", self._LIVE)

        # Only the backup's grant may be consumed — never the live bytes'.
        assert consumed == [backup]

    def test_same_lineage_live_credential_still_refreshes(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        """Access-token-only drift from the backup is same-lineage → the
        normal no-owner refresh path stays available."""
        switcher = self._switcher(sample_sequence_data)
        backup = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-older", "refreshToken": "rt-live",
        }})
        refreshed = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-new", "refreshToken": "rt-new",
            "expiresAt": 9_999_999_999_000,
        }})

        def mock_fetch(account_num, email, credentials, is_active):
            assert is_active is True
            assert credentials == refreshed  # rotated under the locks
            return oauth.UsageOutcome({"five_hour": {"pct": 10}})

        with patch.object(switcher, "_read_credentials", return_value=self._LIVE), \
             patch.object(switcher, "_read_account_credentials", return_value=backup), \
             patch.object(switcher, "_write_credentials") as write_live, \
             patch.object(switcher, "_write_account_credentials") as write_backup, \
             patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   return_value=oauth.RefreshOutcome(refreshed, None)), \
             patch("claude_swap.oauth.try_fetch_usage_for_account", side_effect=mock_fetch):
            result = switcher._fetch_active_usage("1", "test@example.com", self._LIVE)

        assert result.usage == {"five_hour": {"pct": 10}}
        write_live.assert_called_once_with(refreshed)
        write_backup.assert_called_once_with("1", "test@example.com", refreshed)


class TestDirectActivationPreservation:
    """Direct activation replaces the live credential without a backup step —
    invariant II requires the displaced credential to be stashed first."""

    def _setup(self, temp_home, live_identity_email="untracked@example.com"):
        # live_identity_email=None leaves ~/.claude.json absent entirely: a
        # live credential without any config identity (wiped/crashed login).
        config_path = temp_home / ".claude.json"
        if live_identity_email is not None:
            config_path.write_text(json.dumps({
                "oauthAccount": {
                    "emailAddress": live_identity_email,
                    "accountUuid": "",
                    "organizationUuid": None,
                    "organizationName": None,
                }
            }))
        switcher = ClaudeAccountSwitcher()
        switcher.platform = Platform.LINUX
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, {
            "activeAccountNumber": None,
            "lastUpdated": "2024-01-01T00:00:00Z",
            "sequence": [1],
            "accounts": {
                "1": {
                    "email": "one@example.com",
                    "uuid": "uuid-one",
                    "organizationUuid": "",
                    "organizationName": "",
                    "added": "2024-01-01T00:00:00Z",
                },
            },
        })
        target_creds = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-one", "refreshToken": "rt-one",
        }})
        switcher._write_account_credentials("1", "one@example.com", target_creds)
        switcher._write_account_config("1", "one@example.com", json.dumps({
            "oauthAccount": {"emailAddress": "one@example.com", "accountUuid": "uuid-one"},
        }))
        # Unmanaged live credential in the (temp-home) live store.
        unmanaged = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-unmanaged", "refreshToken": "rt-unmanaged",
        }})
        (temp_home / ".claude" / ".credentials.json").write_text(unmanaged)
        return switcher, unmanaged

    def test_unmanaged_live_login_stashed_before_activation(self, temp_home):
        switcher, unmanaged = self._setup(temp_home)
        with patch.object(switcher, "list_accounts"):
            switcher._perform_switch("1", emit_output=False)
        entries = switcher.list_unclaimed_credentials()
        assert len(entries) == 1
        (entry_id,) = entries
        assert _read_safety_copy(switcher, entry_id) == unmanaged
        assert entries[entry_id]["reason"] == "displaced-live-login"

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="RLIMIT_FSIZE is POSIX-only",
    )
    def test_direct_activation_rollback_does_not_truncate_the_config(
        self, temp_home
    ):
        """The THIRD restore arm, on ~/.claude.json.

        `config_written` is armed before the write, so this arm runs on the
        faults where `_write_json` never opened the destination and those
        bytes are the intact original. A truncating rewrite there destroys
        `oauthAccount`, `projects`, `mcpServers` and `userID` — and the cap
        refuses the restore's own temp too, so this fault is a refused
        rollback as well, which the caller must hear about.
        """
        import resource
        import signal

        switcher, _ = self._setup(temp_home)
        config_path = temp_home / ".claude.json"
        config_path.write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "untracked@example.com", "accountUuid": "",
                "organizationUuid": None, "organizationName": None,
            },
            "userID": "user-abcdef",
            "mcpServers": {"a": {"command": "x"}},
            "projects": {
                f"/p/{i}": {"allowedTools": ["Bash", "Read"] * 8}
                for i in range(700)
            },
        }))
        original = config_path.read_text(encoding="utf-8")
        cap = 100_000
        # PREMISE: the config cannot fit under the cap, so the write faults.
        assert len(original) > cap

        old_sig = signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
        soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
        resource.setrlimit(resource.RLIMIT_FSIZE, (cap, hard))
        try:
            # `SwitchError`, not the write's own `OSError`. The cap refuses
            # the RESTORE's temp too, so this fault is also a refused
            # rollback -- and a refused rollback is now reported rather than
            # logged, exactly as the transaction path reports it. The report
            # is conservative on this shape: it says the bytes were not put
            # back, which is true, while the assertion below shows the
            # destination never lost them.
            with patch.object(switcher, "list_accounts"):
                with pytest.raises(SwitchError) as excinfo:
                    switcher._perform_switch("1", emit_output=False)
        finally:
            resource.setrlimit(resource.RLIMIT_FSIZE, (soft, hard))
            signal.signal(signal.SIGXFSZ, old_sig)

        text = config_path.read_text(encoding="utf-8")
        assert text == original, (
            "DEFECT: the direct-activation rollback truncated the config the "
            f"failed write never touched ({len(original)}B -> {len(text)}B)"
        )
        assert "rollback also failed" in str(excinfo.value), (
            "a restore the helper refused must reach the caller, not just "
            f"the log: {excinfo.value}"
        )

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="a 0o444 destination refusing the recovery's own emptying is "
               "a POSIX shape",
    )
    def test_direct_activation_rollback_restores_a_partial_roster(
        self, temp_home
    ):
        """The roster arm must restore, not inspect what it finds.

        `_write_json`'s write-through recovery empties a partial only when
        the emptying itself succeeds; refused, the destination keeps a few
        bytes of half-written JSON. An arm that restores only an EMPTY
        roster leaves that partial in place, and `_get_sequence_data` reads
        strictly -- so every later cswap invocation refuses to run.
        """
        from claude_swap import switcher as switcher_mod

        switcher, _ = self._setup(temp_home)
        original = switcher.sequence_file.read_text(encoding="utf-8")

        real_copyfile = switcher_mod.shutil.copyfile
        real_replace = switcher_mod.replace_with_retry
        real_write_json = ClaudeAccountSwitcher._write_json
        calls = []
        at_arm = {}

        def busy_publish(src, dst, *a, **k):
            if os.path.basename(os.fspath(dst)) == "sequence.json":
                calls.append("replace")
                raise OSError(errno.EBUSY, "device or resource busy")
            return real_replace(src, dst, *a, **k)

        def dies_partway(source, dest, *a, **k):
            if os.path.basename(os.fspath(dest)) == "sequence.json":
                calls.append("copy")
                fd = os.open(dest, os.O_WRONLY | os.O_TRUNC)
                os.write(fd, b"{par")
                os.close(fd)
                # THE SAME FAULT BLOCKS THE RECOVERY'S OWN EMPTYING, which
                # `_unnarrow` performs by opening the destination 'wb'. That
                # is the branch it documents: refused, the partial stays.
                os.chmod(dest, 0o444)
                raise OSError(errno.ENOSPC, "no space left on device")
            return real_copyfile(source, dest, *a, **k)

        def spy_write_json(self, path, data):
            # WHAT THE ARM IS HANDED, read independently of the arm -- an
            # arm that never runs must still fail on the defect assertion,
            # not on this premise.
            try:
                return real_write_json(self, path, data)
            except BaseException:
                if os.path.basename(os.fspath(path)) == "sequence.json":
                    at_arm["text"] = path.read_text(encoding="utf-8")
                raise

        with patch.object(
            switcher_mod, "replace_with_retry", side_effect=busy_publish,
        ), patch.object(
            switcher_mod.shutil, "copyfile", side_effect=dies_partway,
        ), patch.object(
            ClaudeAccountSwitcher, "_write_json", spy_write_json,
        ), patch.object(switcher, "list_accounts"), pytest.raises(SwitchError):
            switcher._perform_switch("1", emit_output=False)

        # PREMISES: the roster's write-through really ran, and the recovery
        # really could not empty what it left -- or this case asserts the
        # repair of damage nothing did.
        assert calls == ["replace", "copy"], (
            f"premise: the roster's write-through must run, got {calls}"
        )
        assert at_arm.get("text") == "{par", (
            "premise: the arm must be entered on a PARTIAL roster, got "
            f"{at_arm.get('text')!r}"
        )

        text = switcher.sequence_file.read_text(encoding="utf-8")
        assert text == original, (
            "DEFECT: the rollback left a partial roster in place. The arm "
            "restores only an EMPTY one, and a few bytes of half-written "
            f"JSON is not empty ({len(original)}B -> {len(text)}B, "
            f"{text[:16]!r})"
        )
        # The consequence, at the seam that suffers it.
        later = ClaudeAccountSwitcher()
        later.platform = Platform.LINUX
        assert (later._get_sequence_data() or {}).get("accounts", {}).keys() == {
            "1"
        }, "the roster came back but cswap still cannot read it"

    def test_direct_activation_rollback_restores_an_emptied_config(
        self, temp_home
    ):
        """The `config_written` token must be armed BEFORE the write.

        A destination that refuses the rename makes `_write_json` copy
        THROUGH it, and a copy that dies part-way empties it -- correctly, a
        partial credential is worse than none -- and then raises. Armed
        after the write returns, the token is still False there, so the arm
        skips a restore whose bytes it is holding and the user keeps an
        empty `~/.claude.json`.
        """
        from claude_swap import switcher as switcher_mod
        import claude_swap.fsutil as fsutil_mod

        switcher, _ = self._setup(temp_home)
        config_path = temp_home / ".claude.json"
        config_path.write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "untracked@example.com", "accountUuid": "",
                "organizationUuid": None, "organizationName": None,
            },
            "userID": "user-abcdef",
            "mcpServers": {"a": {"command": "x"}},
            "projects": {"/some/repo": {"allowedTools": ["Bash", "Read"]}},
        }))
        original = config_path.read_text(encoding="utf-8")

        real_copyfile = switcher_mod.shutil.copyfile
        real_replace = fsutil_mod.os.replace
        calls = []

        def busy_replace(src, dst, *a, **k):
            # AT THE SHARED CALLEE. `models` holds its own binding of
            # `replace_with_retry`, so patching switcher's name leaves the
            # RESTORE's rename real and this case certifies a branch it
            # never entered.
            if os.path.basename(os.fspath(dst)) == ".claude.json":
                calls.append("replace")
                raise OSError(errno.EBUSY, "device or resource busy")
            return real_replace(src, dst, *a, **k)

        def dies_partway(source, dest, *a, **k):
            if os.path.basename(os.fspath(dest)) == ".claude.json":
                calls.append("copy")
                # `copyfile` opens the destination 'wb' before the first
                # source byte, so it is already empty when this raises.
                with open(dest, "wb") as handle:
                    handle.write(b"{par")
                raise OSError(errno.ENOSPC, "no space left on device")
            return real_copyfile(source, dest, *a, **k)

        with patch.object(
            fsutil_mod.os, "replace", side_effect=busy_replace,
        ), patch.object(
            switcher_mod.shutil, "copyfile", side_effect=dies_partway,
        ), patch.object(switcher, "list_accounts"), pytest.raises(SwitchError):
            switcher._perform_switch("1", emit_output=False)

        # PREMISE: the write's EBUSY and its write-through must run, or this
        # asserts the repair of damage nothing did. ONLY THE FIRST TWO. The
        # restore's own rename is the third, and that one is the arm under
        # test -- asserting it here would make an unarmed token fail the
        # premise instead of the defect, which is the failure this case
        # exists to report.
        assert calls[:2] == ["replace", "copy"], (
            f"premise: the config's write-through must run, got {calls}"
        )
        assert config_path.read_text() != "{par", (
            "premise: the partial must not survive -- the recovery empties it"
        )
        assert config_path.read_text(encoding="utf-8") == original, (
            "DEFECT: the write-through emptied the config and raised, and "
            "the direct-activation rollback did not restore it. `userID`, "
            "`mcpServers` and `projects` are gone -- and an unarmed token "
            "attempts no restore, so nothing is reported either and the user "
            "is told only that the activation failed"
        )

    def test_direct_activation_reports_a_rollback_it_could_not_make(
        self, temp_home
    ):
        """A refused restore must reach the caller, not just the log.

        `_restore_atomically` REFUSES a publish error that is not EBUSY --
        that refusal is the point of the helper -- so the arm has a reachable
        failure branch. It used to be `_logger.error`d and nothing more, and
        the console handler exists only under debug, so the user was told the
        activation failed while `~/.claude.json` was empty. The transaction
        path is the control: it raises "rollback also failed".
        """
        from claude_swap import switcher as switcher_mod
        from claude_swap import models as models_mod

        switcher, _ = self._setup(temp_home)
        config_path = temp_home / ".claude.json"
        config_path.write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "untracked@example.com", "accountUuid": "",
                "organizationUuid": None, "organizationName": None,
            },
            "userID": "user-abcdef",
            "projects": {"/some/repo": {"allowedTools": ["Bash"]}},
        }))
        original = config_path.read_text(encoding="utf-8")

        real_copyfile = switcher_mod.shutil.copyfile
        real_replace = switcher_mod.replace_with_retry

        def busy_publish(src, dst, *a, **k):
            if os.path.basename(os.fspath(dst)) == ".claude.json":
                raise OSError(errno.EBUSY, "device or resource busy")
            return real_replace(src, dst, *a, **k)

        def dies_partway(source, dest, *a, **k):
            if os.path.basename(os.fspath(dest)) == ".claude.json":
                with open(dest, "wb") as handle:
                    handle.write(b"{par")
                raise OSError(errno.ENOSPC, "no space left on device")
            return real_copyfile(source, dest, *a, **k)

        def refuse_restore(src, dst, *a, **k):
            # THE RESTORE'S OWN PUBLISH, at `models`' binding so only
            # `_restore_atomically` sees it. A non-EBUSY refusal, so the
            # helper raises rather than rewriting in place.
            raise OSError(errno.EACCES, "permission denied")

        with patch.object(
            switcher_mod, "replace_with_retry", side_effect=busy_publish,
        ), patch.object(
            switcher_mod.shutil, "copyfile", side_effect=dies_partway,
        ), patch.object(
            models_mod, "replace_with_retry", side_effect=refuse_restore,
        ), patch.object(switcher, "list_accounts"), pytest.raises(
            SwitchError
        ) as excinfo:
            switcher._perform_switch("1", emit_output=False)

        # PREMISE: the config really was lost, or "the caller must be told"
        # is a message about nothing.
        assert config_path.read_text(encoding="utf-8") != original, (
            "premise: the restore must actually have been refused"
        )
        assert "rollback also failed" in str(excinfo.value), (
            "DEFECT: the restore was refused and `~/.claude.json` is empty, "
            "and the caller is told only that the activation failed. The "
            f"config is unrecoverable and nothing says so: {excinfo.value}"
        )

    def test_direct_activation_names_every_arm_it_could_not_restore(
        self, temp_home
    ):
        """All three arms, not just the one that happened to be covered.

        The report exists so a refused restore reaches the caller, and there
        are three destinations it can name. The credentials one matters most:
        its failure means the live OAuth credential was NOT put back, so the
        machine keeps the target's token while `~/.claude.json` says
        otherwise, and this message is the only thing that says so.

        One fault reaches all three: the roster write fails with every arm
        already armed, and every restore is then refused.
        """
        from claude_swap import models as models_mod

        switcher, _ = self._setup(temp_home)
        config_path = temp_home / ".claude.json"
        config_path.write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "untracked@example.com", "accountUuid": "",
                "organizationUuid": None, "organizationName": None,
            },
            "userID": "user-abcdef",
        }))

        real_write_json = ClaudeAccountSwitcher._write_json
        real_write_creds = switcher._write_credentials
        wrote = {"creds": 0}

        def fail_the_roster(self, path, data):
            # LAST write in the block, so `creds_written`, `config_written`
            # and `sequence_written` are all armed when it raises.
            if os.path.basename(os.fspath(path)) == "sequence.json" and \
                    data.get("activeAccountNumber") == 1:
                raise OSError(errno.ENOSPC, "no space left on device")
            return real_write_json(self, path, data)

        refused = []

        def refuse_every_restore(src, dst, *a, **k):
            # At `models`' own binding, so only `_restore_atomically` sees it.
            # EACCES is not EBUSY, so the helper refuses instead of rewriting.
            refused.append(os.path.basename(os.fspath(dst)))
            raise OSError(errno.EACCES, "permission denied")

        def creds_ok_then_refused(creds):
            wrote["creds"] += 1
            if wrote["creds"] == 1:
                return real_write_creds(creds)
            raise OSError(errno.EACCES, "permission denied")

        with patch.object(
            ClaudeAccountSwitcher, "_write_json", fail_the_roster,
        ), patch.object(
            models_mod, "replace_with_retry", side_effect=refuse_every_restore,
        ), patch.object(
            switcher, "_write_credentials", creds_ok_then_refused,
        ), patch.object(switcher, "list_accounts"), pytest.raises(
            SwitchError
        ) as excinfo:
            switcher._perform_switch("1", emit_output=False)

        # PREMISE: the rollback really attempted the credential restore, or
        # the assertion below is about an arm that never ran.
        assert wrote["creds"] == 2, (
            f"premise: the credentials arm must run, got {wrote['creds']} "
            "write(s)"
        )
        # PREMISE: the other two arms were entered and refused too. Without
        # this an arm that stopped RUNNING would fail below as though the
        # report had gone silent, which is a different defect.
        assert {"sequence.json", ".claude.json"} <= set(refused), (
            f"premise: both file arms must attempt a restore, got {refused}"
        )
        message = str(excinfo.value)
        for destination in (
            switcher.sequence_file.name, config_path.name, "the live credential",
        ):
            assert destination in message, (
                f"DEFECT: {destination!r} could not be restored and the "
                f"report does not name it. The user is told the activation "
                f"failed and nothing else: {message}"
            )

    def test_direct_activation_does_not_claim_a_rollback_it_never_made(
        self, temp_home
    ):
        """The tokens say an arm was ARMED, not that it restored anything.

        Each arm carries a second condition the report must mirror: there
        has to be a snapshot to put back. On a machine with no prior live
        login and no `~/.claude.json` -- fresh, post-purge, or just
        imported, which is the population this path exists for -- the
        target credential is written and then the config write fails. Every
        snapshot is None, so every arm is skipped and nothing is restored.

        Announcing "was rolled back" there is the same false sentence this
        branch treats as a defect everywhere else, and it is worse than
        silence: the target's token IS the live credential now, and the
        roster does not know it.
        """
        switcher, _ = self._setup(temp_home, live_identity_email=None)
        # No prior live login either: both snapshots are absent.
        (temp_home / ".claude" / ".credentials.json").unlink()
        config_path = temp_home / ".claude.json"
        assert not config_path.exists(), "premise: no prior config"

        real_write_json = ClaudeAccountSwitcher._write_json

        def fail_the_config(self, path, data):
            if os.path.basename(os.fspath(path)) == ".claude.json":
                raise OSError(errno.ENOSPC, "no space left on device")
            return real_write_json(self, path, data)

        with patch.object(
            ClaudeAccountSwitcher, "_write_json", fail_the_config,
        ), patch.object(switcher, "list_accounts"), pytest.raises(
            SwitchError
        ) as excinfo:
            switcher._perform_switch("1", emit_output=False)

        # PREMISE: nothing was restored, or there is no false claim to make.
        live = (temp_home / ".claude" / ".credentials.json").read_text()
        assert json.loads(live)["claudeAiOauth"]["accessToken"] == "sk-one", (
            "premise: the target credential must be live and unrestored"
        )

        assert "was rolled back" not in str(excinfo.value), (
            "DEFECT: the report claims a rollback that never happened. Every "
            "snapshot was absent so every arm was skipped, and the target's "
            "credential is the live login while the roster does not know it: "
            f"{excinfo.value}"
        )
        # And POSITIVELY, or any replacement text passes -- including none.
        assert "could NOT be rolled back" in str(excinfo.value), (
            f"the report must say what did not happen: {excinfo.value}"
        )

    def test_direct_activation_does_not_call_a_partial_rollback_a_whole_one(
        self, temp_home
    ):
        """One arm of three is not "was rolled back".

        The flag says SOME arm ran; the sentence speaks for all of them. On a
        machine with no `~/.claude.json` and no prior live login but a
        populated roster, the roster write fails with all three tokens armed
        -- and only the roster has a snapshot. It goes back; the credential
        and the config do not.

        The user is then told the switch was rolled back while the target's
        token is the live login and a config that never existed names the
        target: the orphaned-identity state the snapshot exists to prevent.
        """
        switcher, _ = self._setup(temp_home, live_identity_email=None)
        (temp_home / ".claude" / ".credentials.json").unlink()
        config_path = temp_home / ".claude.json"
        assert not config_path.exists(), "premise: no prior config"
        roster_before = switcher.sequence_file.read_text(encoding="utf-8")

        real_write_json = ClaudeAccountSwitcher._write_json

        def fail_the_roster(self, path, data):
            # LAST write in the block: all three tokens are armed, and only
            # the roster has anything to put back.
            if os.path.basename(os.fspath(path)) == "sequence.json" and \
                    data.get("activeAccountNumber") == 1:
                raise OSError(errno.ENOSPC, "no space left on device")
            return real_write_json(self, path, data)

        with patch.object(
            ClaudeAccountSwitcher, "_write_json", fail_the_roster,
        ), patch.object(switcher, "list_accounts"), pytest.raises(
            SwitchError
        ) as excinfo:
            switcher._perform_switch("1", emit_output=False)

        # PREMISES: exactly one arm restored, and the other two left the
        # machine changed -- or there is no partial to misdescribe.
        assert switcher.sequence_file.read_text(encoding="utf-8") == roster_before, (
            "premise: the roster arm must have restored"
        )
        live = (temp_home / ".claude" / ".credentials.json").read_text()
        assert json.loads(live)["claudeAiOauth"]["accessToken"] == "sk-one", (
            "premise: the target credential must still be live"
        )
        assert config_path.exists(), (
            "premise: a config that did not exist must now name the target"
        )

        assert "was rolled back" not in str(excinfo.value), (
            "DEFECT: one arm of three restored and the report calls it a "
            "rollback. The target's token is the live login and a config "
            "that never existed names the target, which is the orphaned "
            f"identity the snapshot exists to prevent: {excinfo.value}"
        )
        assert "PARTLY" in str(excinfo.value), (
            f"the report must say which way it was partial: {excinfo.value}"
        )

    def test_direct_activation_calls_a_whole_rollback_whole(
        self, temp_home
    ):
        """The denominator's other reachable value.

        Two arms armed, both with snapshots, both restoring, is a COMPLETE
        rollback -- the roster write is never reached, so it was never
        written and owes nothing. Counting a fixed three there downgrades a
        true rollback to a false partial and sends the user hunting for
        damage that does not exist.
        """
        switcher, unmanaged = self._setup(temp_home)
        config_path = temp_home / ".claude.json"
        original_config = config_path.read_text(encoding="utf-8")
        roster_before = switcher.sequence_file.read_text(encoding="utf-8")

        real_write_json = ClaudeAccountSwitcher._write_json

        def fail_the_config(self, path, data):
            if os.path.basename(os.fspath(path)) == ".claude.json":
                raise OSError(errno.ENOSPC, "no space left on device")
            return real_write_json(self, path, data)

        with patch.object(
            ClaudeAccountSwitcher, "_write_json", fail_the_config,
        ), patch.object(switcher, "list_accounts"), pytest.raises(
            SwitchError
        ) as excinfo:
            switcher._perform_switch("1", emit_output=False)

        # PREMISE: everything that was written really did come back, or
        # "whole" is not what this state is.
        assert config_path.read_text(encoding="utf-8") == original_config
        assert (temp_home / ".claude" / ".credentials.json").read_text() == (
            unmanaged
        )
        assert switcher.sequence_file.read_text(encoding="utf-8") == (
            roster_before
        ), "premise: the roster was never written, so it owes nothing"

        message = str(excinfo.value)
        assert "was rolled back" in message, (
            f"DEFECT: a complete rollback must say so, got: {message}"
        )
        assert "PARTLY" not in message, (
            "DEFECT: two arms armed and two restored is WHOLE. A fixed "
            "denominator calls it partial and sends the user looking for "
            f"damage that is not there: {message}"
        )

    def test_direct_activation_does_not_wrap_a_failure_before_any_write(
        self, temp_home
    ):
        """Nothing written is not a rollback of any kind.

        The wrap is gated on a write having happened. When the very first
        write fails, no token is set, nothing needs undoing, and the
        original error is the whole story -- wrapping it would invent a
        rollback that had nothing to act on.
        """
        switcher, unmanaged = self._setup(temp_home)

        def fail_first_write(_creds):
            raise OSError(errno.EACCES, "permission denied")

        with patch.object(
            switcher, "_write_credentials", fail_first_write,
        ), patch.object(switcher, "list_accounts"), pytest.raises(
            # BOTH CANDIDATES, so the assertion below is what discriminates.
            # Naming only `OSError` makes a wrap fail at this boundary and
            # the DEFECT message never reaches the log.
            (OSError, SwitchError)
        ) as excinfo:
            switcher._perform_switch("1", emit_output=False)

        # PREMISE: the live login is untouched, so there was nothing to undo.
        assert (temp_home / ".claude" / ".credentials.json").read_text() == (
            unmanaged
        )
        assert not isinstance(excinfo.value, SwitchError), (
            "DEFECT: no write happened, so the wrap must not fire and "
            f"rename the failure: {excinfo.value!r}"
        )
        assert "rolled back" not in str(excinfo.value), (
            f"DEFECT: nothing was written, so nothing was rolled back: "
            f"{excinfo.value}"
        )

    def test_direct_activation_counts_only_arms_that_had_a_snapshot(
        self, temp_home
    ):
        """An armed token with no snapshot must not count as restored.

        `_roster_snapshot` answers "" when its read fails -- its documented
        behaviour -- so the roster arm can be armed with nothing to put
        back, while the write that armed it EMPTIES the file. The counter
        has to mirror the arm's second conjunct, not just its token, or the
        roster is left unreadable under a sentence saying it came back.

        This is the conjunct the two earlier false sentences turned on, on
        the one arm where nothing else pins it.
        """
        from claude_swap import switcher as switcher_mod

        switcher, unmanaged = self._setup(temp_home)
        config_path = temp_home / ".claude.json"
        original_config = config_path.read_text(encoding="utf-8")

        real_copyfile = switcher_mod.shutil.copyfile
        real_replace = switcher_mod.replace_with_retry

        def busy_publish(src, dst, *a, **k):
            if os.path.basename(os.fspath(dst)) == "sequence.json":
                raise OSError(errno.EBUSY, "device or resource busy")
            return real_replace(src, dst, *a, **k)

        def dies_partway(source, dest, *a, **k):
            if os.path.basename(os.fspath(dest)) == "sequence.json":
                with open(dest, "wb") as handle:
                    handle.write(b"{par")
                raise OSError(errno.ENOSPC, "no space left on device")
            return real_copyfile(source, dest, *a, **k)

        with patch.object(
            # THE SNAPSHOT READ LOST THE RACE. `_get_sequence_data` had
            # already succeeded, so the switch runs; the read that follows
            # hit an OSError and answered "", exactly as documented.
            switcher_mod, "_roster_snapshot", return_value="",
        ), patch.object(
            switcher_mod, "replace_with_retry", side_effect=busy_publish,
        ), patch.object(
            switcher_mod.shutil, "copyfile", side_effect=dies_partway,
        ), patch.object(switcher, "list_accounts"), pytest.raises(
            SwitchError
        ) as excinfo:
            switcher._perform_switch("1", emit_output=False)

        # PREMISES: the roster really was destroyed and really was NOT put
        # back, while the other two arms did restore -- so exactly one arm
        # is outstanding and the sentence has something to be wrong about.
        assert not switcher.sequence_file.read_text(encoding="utf-8").strip(), (
            "premise: the write-through must have emptied the roster"
        )
        assert config_path.read_text(encoding="utf-8") == original_config
        assert (temp_home / ".claude" / ".credentials.json").read_text() == (
            unmanaged
        )

        assert "was rolled back" not in str(excinfo.value), (
            "DEFECT: the roster arm was armed with no snapshot, restored "
            "nothing, and the report counts it as though it had. "
            "`sequence.json` is empty -- every later cswap invocation "
            f"refuses to run -- and the user is told it came back: "
            f"{excinfo.value}"
        )

    def test_stash_failure_aborts_direct_activation(self, temp_home):
        switcher, unmanaged = self._setup(temp_home)
        with patch.object(
            switcher._store, "_write_unclaimed_credential",
            side_effect=OSError("disk full"),
        ):
            with pytest.raises(SwitchError, match="preserve the live credential"):
                switcher._perform_switch("1", emit_output=False)
        # Live store untouched.
        live = (temp_home / ".claude" / ".credentials.json").read_text()
        assert live == unmanaged

    def test_force_proceeds_with_warning_when_stash_fails(self, temp_home):
        switcher, unmanaged = self._setup(temp_home)
        with patch.object(
            switcher._store, "_write_unclaimed_credential",
            side_effect=OSError("disk full"),
        ), patch.object(switcher, "list_accounts"):
            op = switcher._perform_switch(
                "1", emit_output=False, force_activate=True
            )
        assert any("--force" in w for w in op["warnings"])
        live = (temp_home / ".claude" / ".credentials.json").read_text()
        assert json.loads(live)["claudeAiOauth"]["accessToken"] == "sk-one"

    def test_orphaned_live_login_without_config_identity_is_stashed(
        self, temp_home
    ):
        # ~/.claude.json is gone but a live login remains: there is no
        # config identity, yet the displaced credential still needs its
        # safety copy — previously the missing identity skipped both the
        # stash and the rollback snapshot.
        switcher, orphaned = self._setup(temp_home, live_identity_email=None)
        with patch.object(switcher, "list_accounts"):
            switcher._perform_switch("1", emit_output=False)
        entries = switcher.list_unclaimed_credentials()
        assert len(entries) == 1
        (entry_id,) = entries
        assert _read_safety_copy(switcher, entry_id) == orphaned
        assert entries[entry_id]["reason"] == "displaced-live-login"
        live = (temp_home / ".claude" / ".credentials.json").read_text()
        assert json.loads(live)["claudeAiOauth"]["accessToken"] == "sk-one"

    def test_unreadable_live_credentials_without_config_identity_abort(
        self, temp_home
    ):
        # None from _read_credentials means the credentials file exists but
        # could not be read — activation must not blind-overwrite state it
        # could not snapshot, config identity or not.
        switcher, orphaned = self._setup(temp_home, live_identity_email=None)
        with patch.object(switcher, "_read_credentials", return_value=None):
            with pytest.raises(CredentialReadError, match="snapshot"):
                switcher._perform_switch("1", emit_output=False)
        # Live store untouched.
        live = (temp_home / ".claude" / ".credentials.json").read_text()
        assert live == orphaned

    def test_mid_failure_restores_identityless_config(self, temp_home):
        # A settings-bearing ~/.claude.json without oauthAccount (the normal
        # post-logout state) must be restored when activation fails partway —
        # previously only an identity-bearing config was snapshotted, so the
        # credential rollback could leave mismatched halves behind.
        switcher, orphaned = self._setup(temp_home, live_identity_email=None)
        config_path = temp_home / ".claude.json"
        original_config = json.dumps({"projects": {"/home/x": {"history": []}}})
        config_path.write_text(original_config)

        real_write_json = switcher._write_json

        def fail_sequence_write(path, data):
            if path == switcher.sequence_file:
                raise OSError("disk full")
            return real_write_json(path, data)

        with patch.object(
            switcher, "_write_json", side_effect=fail_sequence_write
        ), pytest.raises(SwitchError, match=r"was rolled back.*disk full"):
            switcher._perform_switch("1", emit_output=False)

        # Both halves rolled back: the settings config and the orphaned login.
        assert config_path.read_text() == original_config
        live = (temp_home / ".claude" / ".credentials.json").read_text()
        assert live == orphaned


class TestSharedOAuthCredentialPreservation:
    """Activation must not overwrite live machine-shared OAuth state
    (mcpOAuth et al.) with the destination slot's stale snapshot (#135)."""

    API_KEY = "sk-ant-api03-" + "a1b2c3d4e5" * 4
    _setup_two_accounts = TestPerformSwitchPostDisplay._setup_two_accounts
    _install_store_patches = staticmethod(
        TestPerformSwitchPostDisplay._install_store_patches
    )

    def test_live_shared_keys_win_over_stale_target_copies(self, temp_home):
        switcher = ClaudeAccountSwitcher()
        target = json.dumps({
            "claudeAiOauth": {"accessToken": "target"},
            "mcpOAuth": {"server": {"refreshToken": "stale"}},
            "mcpOAuthClientConfig": {"server": {"clientId": "stale"}},
        })
        live = json.dumps({
            "claudeAiOauth": {"accessToken": "live"},
            "mcpOAuth": {"server": {"refreshToken": "current"}},
            "mcpOAuthClientConfig": {"server": {"clientId": "current"}},
        })

        composed = json.loads(
            switcher._prepare_credentials_for_activation(target, live)
        )

        assert composed == {
            "claudeAiOauth": {"accessToken": "target"},
            "mcpOAuth": {"server": {"refreshToken": "current"}},
            "mcpOAuthClientConfig": {"server": {"clientId": "current"}},
        }

    def test_account_bound_and_unknown_siblings_stay_target_owned(
        self, temp_home
    ):
        # trustedDeviceToken is enrolled per-account, and a field cswap does
        # not recognize could be too — neither may cross an account switch.
        # Only the SHARED_CREDENTIAL_KEYS allowlist is taken from live.
        switcher = ClaudeAccountSwitcher()
        target = json.dumps({
            "claudeAiOauth": {"accessToken": "target"},
            "trustedDeviceToken": "device-token-b",
            "someFutureField": {"value": "target-owned"},
        })
        live = json.dumps({
            "claudeAiOauth": {"accessToken": "live"},
            "trustedDeviceToken": "device-token-a",
            "someFutureField": {"value": "live"},
            "mcpOAuth": {"server": {"refreshToken": "current"}},
        })

        composed = json.loads(
            switcher._prepare_credentials_for_activation(target, live)
        )

        assert composed == {
            "claudeAiOauth": {"accessToken": "target"},
            "trustedDeviceToken": "device-token-b",
            "someFutureField": {"value": "target-owned"},
            "mcpOAuth": {"server": {"refreshToken": "current"}},
        }

    def test_shared_key_absent_from_live_is_not_resurrected(self, temp_home):
        # Shared keys are live-owned in absence too: if the machine no
        # longer holds an MCP session, the slot's stale copy must not
        # reintroduce it.
        switcher = ClaudeAccountSwitcher()
        target = json.dumps({
            "claudeAiOauth": {"accessToken": "target"},
            "mcpOAuth": {"server": {"refreshToken": "stale"}},
        })
        live = json.dumps({
            "claudeAiOauth": {"accessToken": "live"},
        })

        composed = json.loads(
            switcher._prepare_credentials_for_activation(target, live)
        )

        assert composed == {"claudeAiOauth": {"accessToken": "target"}}

    def test_direct_activation_without_config_identity_composes_live_state(
        self, temp_home
    ):
        # A wiped or half-written ~/.claude.json orphans the live credential:
        # no config identity, but the live item still holds the machine's
        # MCP state — direct activation must compose it in, not clobber it.
        switcher = ClaudeAccountSwitcher()
        switcher.platform = Platform.LINUX
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, {
            "activeAccountNumber": None,
            "lastUpdated": "2024-01-01T00:00:00Z",
            "sequence": [1],
            "accounts": {
                "1": {
                    "email": "one@example.com",
                    "uuid": "uuid-one",
                    "organizationUuid": "",
                    "organizationName": "",
                    "added": "2024-01-01T00:00:00Z",
                },
            },
        })
        switcher._write_account_credentials("1", "one@example.com", json.dumps({
            "claudeAiOauth": {"accessToken": "sk-one", "refreshToken": "rt-one"},
            "mcpOAuth": {"server": {"refreshToken": "stale"}},
        }))
        switcher._write_account_config("1", "one@example.com", json.dumps({
            "oauthAccount": {
                "emailAddress": "one@example.com", "accountUuid": "uuid-one",
            },
        }))
        live_path = temp_home / ".claude" / ".credentials.json"
        live_path.write_text(json.dumps({
            "mcpOAuth": {"server": {"refreshToken": "current"}},
        }))

        with patch.object(switcher, "list_accounts"):
            switcher._perform_switch("1", emit_output=False)

        assert json.loads(live_path.read_text()) == {
            "claudeAiOauth": {"accessToken": "sk-one", "refreshToken": "rt-one"},
            "mcpOAuth": {"server": {"refreshToken": "current"}},
        }

    def test_api_key_live_state_activates_target_verbatim(self, temp_home):
        # While a managed API key is active there is no live OAuth object to
        # take siblings from; the stored blob activates unchanged (the
        # pre-existing behavior — the API-key round trip is out of scope).
        switcher = ClaudeAccountSwitcher()
        target = json.dumps({
            "claudeAiOauth": {"accessToken": "target"},
            "mcpOAuth": {"server": {"refreshToken": "stale"}},
        })

        assert (
            switcher._prepare_credentials_for_activation(target, self.API_KEY)
            == target
        )

    def test_absent_live_state_activates_target_verbatim(self, temp_home):
        switcher = ClaudeAccountSwitcher()
        target = json.dumps({
            "claudeAiOauth": {"accessToken": "target"},
            "mcpOAuth": {"server": {"refreshToken": "old"}},
        })

        assert (
            switcher._prepare_credentials_for_activation(target, "") == target
        )
        assert (
            switcher._prepare_credentials_for_activation(target, None)
            == target
        )

    def test_api_key_target_is_never_composed(self, temp_home):
        switcher = ClaudeAccountSwitcher()
        live = json.dumps({
            "claudeAiOauth": {"accessToken": "live"},
            "mcpOAuth": {"server": {"refreshToken": "current"}},
        })

        assert (
            switcher._prepare_credentials_for_activation(self.API_KEY, live)
            == self.API_KEY
        )

    def test_opaque_target_credential_activates_verbatim(self, temp_home):
        # Opaque/legacy JSON shapes without a claudeAiOauth login are
        # activated byte-for-byte, as before.
        switcher = ClaudeAccountSwitcher()
        target = json.dumps({
            "accessToken": "legacy", "refreshToken": "legacy-rt",
        })
        live = json.dumps({
            "claudeAiOauth": {"accessToken": "live"},
            "mcpOAuth": {"server": {"refreshToken": "current"}},
        })

        assert (
            switcher._prepare_credentials_for_activation(target, live)
            == target
        )

    def test_normal_switch_preserves_live_shared_state(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        live = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-live-1", "refreshToken": "rt-live-1",
            },
            "mcpOAuth": {"server": {"refreshToken": "current"}},
        })
        target = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-target-2", "refreshToken": "rt-target-2",
            },
            "mcpOAuth": {"server": {"refreshToken": "stale"}},
        })
        creds_store[("1", "test@example.com")] = live
        creds_store[("2", "account2@example.com")] = target
        live_state = {"creds": live}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )

        try:
            with patch.object(switcher, "list_accounts"):
                switcher._perform_switch("2", emit_output=False)
        finally:
            for p in patches:
                p.stop()

        activated = json.loads(live_state["creds"])
        assert activated["claudeAiOauth"]["accessToken"] == "sk-target-2"
        assert activated["mcpOAuth"] == {
            "server": {"refreshToken": "current"}
        }

    def test_direct_activation_preserves_live_shared_state(self, temp_home):
        switcher, _ = TestDirectActivationPreservation()._setup(temp_home)
        live_path = temp_home / ".claude" / ".credentials.json"
        live = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-unmanaged", "refreshToken": "rt-unmanaged",
            },
            "mcpOAuth": {"server": {"refreshToken": "current"}},
        })
        live_path.write_text(live)
        target = json.loads(
            switcher._read_account_credentials("1", "one@example.com")
        )
        target["mcpOAuth"] = {"server": {"refreshToken": "stale"}}
        switcher._write_account_credentials(
            "1", "one@example.com", json.dumps(target)
        )

        with patch.object(switcher, "list_accounts"):
            switcher._perform_switch("1", emit_output=False)

        activated = json.loads(live_path.read_text())
        assert activated["claudeAiOauth"]["accessToken"] == "sk-one"
        assert activated["mcpOAuth"] == {
            "server": {"refreshToken": "current"}
        }


class TestUuidConflictClassification:
    """An email+org match with a conflicting uuid is a different account
    wearing a recycled email — never the slot."""

    _setup_two_accounts = TestPerformSwitchPostDisplay._setup_two_accounts
    _install_store_patches = staticmethod(
        TestPerformSwitchPostDisplay._install_store_patches
    )

    def test_email_match_with_conflicting_uuid_is_not_the_slot(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        a1_backup = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-1", "refreshToken": "rt-1",
        }})
        creds_store[("1", "test@example.com")] = a1_backup
        rotated = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-x", "refreshToken": "rt-x",
        }})
        live_state = {"creds": rotated}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        try:
            with patch.object(switcher, "list_accounts"), patch(
                # Same email/org as slot 1 — but a different, non-empty uuid
                # (slot 1 stores uuid-1). Must NOT classify as own-rotated.
                "claude_swap.oauth.fetch_oauth_profile",
                return_value={
                    "uuid": "uuid-recycled-email",
                    "email": "test@example.com",
                    "organizationUuid": "",
                },
            ):
                op = switcher._perform_switch("2", emit_output=False)
        finally:
            for p in patches:
                p.stop()
        # Slot 1 untouched; the conflicted credential was preserved as alien
        # (a positively-different account, so this is NOT the fail-open
        # path — a recycled email must never be backed into the slot).
        assert creds_store[("1", "test@example.com")] == a1_backup
        assert len(switcher.list_unclaimed_credentials()) == 1
        assert any(
            "does not match a managed account" in w for w in op["warnings"]
        )


class TestStashStorageHardening:
    """Round-2 review: append-only ids and manifest corruption handling."""

    def _store(self, temp_home):
        switcher = ClaudeAccountSwitcher()
        switcher.platform = Platform.LINUX
        switcher._setup_directories()
        switcher._init_sequence_file()
        return switcher._store

    def test_identical_bytes_same_second_get_distinct_ids(self, temp_home):
        store = self._store(temp_home)
        id_a = store._write_unclaimed_credential("same-bytes", {})
        id_b = store._write_unclaimed_credential("same-bytes", {})
        assert id_a != id_b
        for entry_id in (id_a, id_b):
            raw = store._stash_entry_path(entry_id).read_text().strip()
            assert base64.b64decode(raw, validate=True).decode() == "same-bytes"

    def test_corrupt_manifest_is_preserved_not_clobbered(self, temp_home):
        store = self._store(temp_home)
        entry_id = store._write_unclaimed_credential("bytes-1", {"reason": "x"})
        # Corrupt the manifest out-of-band.
        store._stash_manifest_path().write_text("{ not json !!!")
        new_id = store._write_unclaimed_credential("bytes-2", {"reason": "y"})
        # The corrupt file was set aside, not destroyed…
        corrupt = list(store._host.credentials_dir.glob(
            ".unclaimed-manifest.json.corrupt-*"
        ))
        assert len(corrupt) == 1
        assert "not json" in corrupt[0].read_text()
        # …the new manifest is valid, and the older entry's *bytes* are still
        # listed (as an orphan) even though its metadata row was lost.
        entries = store._list_unclaimed_credentials()
        assert new_id in entries and entries[new_id]["reason"] == "y"
        assert entry_id in entries
        raw = store._stash_entry_path(entry_id).read_text().strip()
        assert base64.b64decode(raw, validate=True).decode() == "bytes-1"


class TestStashManifestConcurrentMutation:
    """I-1: both manifest mutators are read-modify-write over ONE file.

    ``_write_unclaimed_credential`` (reached from the ``except LockError``
    stash path, which runs WITHOUT the slot lock by construction -- it is
    reached precisely because that lock was unavailable) and
    ``_remove_unclaimed_credential`` (reached from the retire, which runs
    UNDER it) both read the whole manifest, mutate their own snapshot, and
    rewrite the whole file. Run concurrently, each rewrite drops whatever the
    other wrote after its read.

    Driven with real concurrency, not a mocked interleaving. The CONTROL runs
    the identical workload with every mutation serialized on a lock the
    *test* holds: if the control also loses rows, the harness is measuring
    itself rather than the code.
    """

    ROWS = 40

    def _store(self, temp_home):
        switcher = ClaudeAccountSwitcher()
        switcher.platform = Platform.LINUX
        switcher._setup_directories()
        return switcher._store

    def _run(self, store, gate):
        """2 stashers + 1 stash-then-retire churner, concurrent.

        ``gate()`` wraps each mutation: a real lock for the control, a no-op
        for the arm under test. Returns the probe tags whose manifest rows
        did not survive.
        """
        import threading

        errors: list[Exception] = []

        def stash(tag):
            try:
                for i in range(self.ROWS):
                    with gate():
                        store._write_unclaimed_credential(
                            f"creds-{tag}{i}",
                            {
                                "reason": "consume-gate-persist-lock-failed",
                                "configSlot": "1",
                                "consumedFp": "fp",
                                "probe": f"{tag}{i}",
                            },
                        )
            except Exception as e:  # pragma: no cover - asserted below
                errors.append(e)

        def churn():
            try:
                for i in range(self.ROWS):
                    with gate():
                        entry_id = store._write_unclaimed_credential(
                            f"retire-{i}",
                            {"configSlot": "1", "consumedFp": "other"},
                        )
                    with gate():
                        store._remove_unclaimed_credential(entry_id)
            except Exception as e:  # pragma: no cover - asserted below
                errors.append(e)

        threads = [
            threading.Thread(target=stash, args=("a",)),
            threading.Thread(target=churn),
            threading.Thread(target=stash, args=("b",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        assert not any(t.is_alive() for t in threads), "worker deadlocked"
        assert not errors, errors
        probes = {
            meta.get("probe") for meta in store._read_stash_manifest().values()
        }
        expected = {f"{tag}{i}" for tag in ("a", "b") for i in range(self.ROWS)}
        return sorted(expected - probes)

    def test_concurrent_stash_and_retire_lose_no_manifest_row(self, temp_home):
        import contextlib

        store = self._store(temp_home)
        lost = self._run(store, contextlib.nullcontext)
        assert not lost, (
            f"{len(lost)}/{2 * self.ROWS} stash rows lost to a concurrent "
            "manifest rewrite. The entry BYTES survive (they are written "
            "first), but the row carrying consumedFp/configSlot does not -- "
            "and _adopt_stashed_successor iterates manifest rows only, so a "
            "row-less successor can never be adopted while "
            "_list_unclaimed_credentials' glob keeps listing it forever"
        )

    def test_control_serialized_mutation_loses_no_row(self, temp_home):
        """CONTROL: the same workload, externally serialized, loses nothing.

        Proves the loss above is the unsynchronized read-modify-write and not
        the harness.
        """
        import threading

        store = self._store(temp_home)
        lock = threading.Lock()
        lost = self._run(store, lambda: lock)
        assert not lost, (
            "control broken: the harness loses rows even when every mutation "
            "is serialized, so it cannot attribute the loss"
        )


class TestRemoveAccountPrunesMappings:
    """Removing an account drops any directory mappings pointing at it."""

    def test_remove_account_prunes_mappings(self, temp_home, monkeypatch):
        from claude_swap.mappings import MappingStore

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._init_sequence_file()
        data = switcher._get_sequence_data()
        data["accounts"]["1"] = {
            "email": "a@x.com",
            "uuid": "u1",
            "organizationUuid": "",
            "organizationName": "",
            "added": "2024-01-01T00:00:00Z",
        }
        data["sequence"] = [1]
        switcher._write_json(switcher.sequence_file, data)

        store = MappingStore(switcher.backup_dir)
        store.set(temp_home, "a@x.com", "")
        assert store.get(temp_home) is not None

        monkeypatch.setattr("builtins.input", lambda *a, **k: "y")
        switcher.remove_account("1")

        assert store.get(temp_home) is None

    def _config_switcher(self, temp_home, email):
        """Write a live claude config for ``email`` and return a switcher."""
        config = {
            "oauthAccount": {
                "emailAddress": email,
                "accountUuid": "uuid-" + email,
                "organizationUuid": "",
                "organizationName": "",
            }
        }
        (temp_home / ".claude.json").write_text(json.dumps(config))
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._init_sequence_file()
        return switcher

    def test_slot_overwrite_prunes_displaced_mappings(self, temp_home):
        """Overwriting a slot with a different account drops the old one's mappings."""
        from claude_swap.mappings import MappingStore

        fake_creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})

        switcher = self._config_switcher(temp_home, "a@x.com")
        with patch.object(switcher, "_read_active_credentials", return_value=ActiveCredentials(fake_creds, False)), \
             patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_delete_account_credentials"):
            switcher.add_account(slot=3)

        store = MappingStore(switcher.backup_dir)
        store.set(temp_home, "a@x.com", "")

        switcher = self._config_switcher(temp_home, "b@x.com")
        with patch.object(switcher, "_read_active_credentials", return_value=ActiveCredentials(fake_creds, False)), \
             patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_delete_account_credentials"), \
             patch("builtins.input", return_value="y"):
            switcher.add_account(slot=3)

        assert store.get(temp_home) is None

    def test_slot_migration_keeps_mappings(self, temp_home):
        """Moving an account to another slot keeps its identity-keyed mappings."""
        from claude_swap.mappings import MappingStore

        fake_creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})

        switcher = self._config_switcher(temp_home, "a@x.com")
        with patch.object(switcher, "_read_active_credentials", return_value=ActiveCredentials(fake_creds, False)), \
             patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_delete_account_credentials"):
            switcher.add_account()  # lands in slot 1

        store = MappingStore(switcher.backup_dir)
        store.set(temp_home, "a@x.com", "")

        with patch.object(switcher, "_read_active_credentials", return_value=ActiveCredentials(fake_creds, False)), \
             patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_delete_account_credentials"):
            switcher.add_account(slot=5)  # same identity, new slot

        assert store.get(temp_home) is not None
        assert switcher.slot_for_directory(str(temp_home)) == ("5", "a@x.com")


class TestSwitchRemoveGatesAcceptAlias:
    """Regression: switch_to/remove_account must accept an alias identifier
    instead of rejecting it with 'Invalid email format' before resolution."""

    def test_switch_to_by_alias_reaches_resolution(
        self, temp_home: Path, sample_sequence_data: dict,
    ):
        sample_sequence_data["accounts"]["2"]["alias"] = "dev"
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        with patch.object(switcher, "_perform_switch", return_value={
            "from": None, "to": {"number": 2}, "warnings": [],
        }) as perform:
            switcher.switch_to("dev")
        perform.assert_called_once_with(
            "2", emit_output=True, force_activate=False, provenance=None,
        )

    def test_switch_to_unknown_alias_raises_account_not_found_not_validation(
        self, temp_home: Path, sample_sequence_data: dict,
    ):
        """An identifier that isn't a digit, alias, or valid email must still
        raise ValidationError (format gate), but a well-formed alias that
        just doesn't match anything must fall through to resolution and
        raise AccountNotFoundError, not ValidationError."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        with pytest.raises(ValidationError):
            switcher.switch_to("not an email or alias!")

    def test_remove_account_by_alias(
        self, temp_home: Path, sample_sequence_data: dict, monkeypatch,
    ):
        sample_sequence_data["accounts"]["2"]["alias"] = "dev"
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        monkeypatch.setattr("builtins.input", lambda *a, **k: "y")
        with patch.object(switcher, "_delete_account_files"):
            switcher.remove_account("dev")

        data = switcher._get_sequence_data()
        assert "2" not in data["accounts"]
        assert "1" in data["accounts"]

    def test_remove_account_invalid_identifier_still_raises_validation(
        self, temp_home: Path, sample_sequence_data: dict,
    ):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        with pytest.raises(ValidationError):
            switcher.remove_account("not an email or alias!")


class TestAddAccountAlias:
    """Test the --alias convenience at add time, and preservation on re-add."""

    def _config_switcher(self, temp_home, email):
        config = {
            "oauthAccount": {
                "emailAddress": email,
                "accountUuid": "uuid-" + email,
                "organizationUuid": "",
                "organizationName": "",
            }
        }
        (temp_home / ".claude.json").write_text(json.dumps(config))
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._init_sequence_file()
        return switcher

    def test_add_account_sets_alias(self, temp_home: Path):
        fake_creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})
        switcher = self._config_switcher(temp_home, "a@x.com")
        with patch.object(switcher, "_read_active_credentials", return_value=ActiveCredentials(fake_creds, False)), \
             patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_delete_account_credentials"):
            switcher.add_account(alias="dev")

        data = switcher._get_sequence_data()
        assert data["accounts"]["1"]["alias"] == "dev"

    def test_readd_without_alias_preserves_existing(self, temp_home: Path):
        """Re-running `cswap add` (refresh-in-place) without --alias must not
        wipe a previously set alias."""
        fake_creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})
        switcher = self._config_switcher(temp_home, "a@x.com")
        with patch.object(switcher, "_read_active_credentials", return_value=ActiveCredentials(fake_creds, False)), \
             patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_delete_account_credentials"):
            switcher.add_account(alias="dev")
            switcher.add_account()  # refresh-in-place, no alias passed

        data = switcher._get_sequence_data()
        assert data["accounts"]["1"]["alias"] == "dev"

    def test_readd_refresh_in_place_applies_new_alias(self, temp_home: Path):
        fake_creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})
        switcher = self._config_switcher(temp_home, "a@x.com")
        with patch.object(switcher, "_read_active_credentials", return_value=ActiveCredentials(fake_creds, False)), \
             patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_delete_account_credentials"):
            switcher.add_account()
            switcher.add_account(alias="work")

        data = switcher._get_sequence_data()
        assert data["accounts"]["1"]["alias"] == "work"

    def test_explicit_slot_migration_preserves_alias(self, temp_home: Path):
        """Moving an account to another slot (explicit --slot) carries its
        alias forward instead of dropping it."""
        fake_creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})
        switcher = self._config_switcher(temp_home, "a@x.com")
        with patch.object(switcher, "_read_active_credentials", return_value=ActiveCredentials(fake_creds, False)), \
             patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_delete_account_credentials"):
            switcher.add_account(alias="dev")  # lands in slot 1
            switcher.add_account(slot=5)  # same identity, new slot, no alias passed

        data = switcher._get_sequence_data()
        assert "1" not in data["accounts"]
        assert data["accounts"]["5"]["alias"] == "dev"

    def test_add_account_duplicate_alias_raises(self, temp_home: Path):
        fake_creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})
        switcher = self._config_switcher(temp_home, "a@x.com")
        data = switcher._get_sequence_data()
        data["accounts"]["9"] = {
            "email": "other@x.com", "uuid": "u9", "alias": "dev",
            "added": "2024-01-01T00:00:00Z",
        }
        data["sequence"] = [9]
        switcher._write_json(switcher.sequence_file, data)

        with patch.object(switcher, "_read_active_credentials", return_value=ActiveCredentials(fake_creds, False)), \
             patch.object(switcher, "_write_account_credentials"), \
             patch.object(switcher, "_delete_account_credentials"):
            with pytest.raises(ValidationError):
                switcher.add_account(alias="dev")


# ---------------------------------------------------------------------------
# Disable / enable an account (hold it out of auto-rotation)
# ---------------------------------------------------------------------------


class TestDisableEnableAccount:
    """`cswap disable`/`cswap enable`: park a managed account out of automatic
    rotation without removing it. Disabled slots are skipped by the auto-switch
    engine, bare `switch` rotation, and the usage-aware strategies, but stay
    valid explicit `switch <num|email>` targets."""

    def _setup(self, temp_home: Path) -> ClaudeAccountSwitcher:
        s = ClaudeAccountSwitcher()
        s.platform = Platform.LINUX
        s._setup_directories()
        s._init_sequence_file()
        return s

    def _seed(self, s: ClaudeAccountSwitcher, num: int, email: str) -> None:
        """Add a fully switchable slot (creds + config backups present)."""
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

    def _make_live(self, temp_home: Path, email: str, num: int) -> None:
        """Point the live login at a seeded account."""
        (temp_home / ".claude" / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {
                "accessToken": f"sk-live-{num}", "refreshToken": f"rt-live-{num}"}})
        )
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": email, "accountUuid": f"uuid-{num}"}
        }))

    # -- flag storage + accessors ------------------------------------------

    def test_disable_sets_flag_and_excludes_from_rotation(self, temp_home, capsys):
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._seed(s, 3, "c@example.com")

        s.set_account_disabled("2", True)

        assert s.is_account_disabled("2") is True
        assert s.disabled_account_numbers() == ["2"]
        assert s.switchable_account_numbers() == ["1", "3"]
        data = s._get_sequence_data()
        assert data["accounts"]["2"]["disabled"] is True
        assert "Disabled Account-2" in capsys.readouterr().out

    def test_enable_clears_flag_and_restores_position(self, temp_home, capsys):
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._seed(s, 3, "c@example.com")

        s.set_account_disabled("2", True)
        capsys.readouterr()
        s.set_account_disabled("2", False)

        assert s.is_account_disabled("2") is False
        assert s.disabled_account_numbers() == []
        # Restored in original sequence position, not appended at the end.
        assert s.switchable_account_numbers() == ["1", "2", "3"]
        data = s._get_sequence_data()
        assert "disabled" not in data["accounts"]["2"]
        assert "Enabled Account-2" in capsys.readouterr().out

    def test_disable_by_email(self, temp_home):
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")

        s.set_account_disabled("b@example.com", True)

        assert s.is_account_disabled("2") is True

    def test_disable_and_enable_by_alias(self, temp_home):
        """An alias resolves the same as a number/email (issue: disable/enable
        must accept aliases now that the alias feature has landed)."""
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        s.set_alias("2", "dev")

        s.set_account_disabled("dev", True)
        assert s.is_account_disabled("2") is True
        assert s.switchable_account_numbers() == ["1"]

        s.set_account_disabled("dev", False)
        assert s.is_account_disabled("2") is False
        assert s.switchable_account_numbers() == ["1", "2"]

    def test_repeated_disable_is_noop(self, temp_home, capsys):
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")

        s.set_account_disabled("2", True)
        capsys.readouterr()
        s.set_account_disabled("2", True)  # already disabled

        assert "already disabled" in capsys.readouterr().out
        assert s.is_account_disabled("2") is True

    def test_enable_when_not_disabled_is_noop(self, temp_home, capsys):
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")

        s.set_account_disabled("1", False)

        assert "already enabled" in capsys.readouterr().out

    def test_disable_unknown_account_raises(self, temp_home):
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")

        with pytest.raises(AccountNotFoundError):
            s.set_account_disabled("99", True)

    # -- effect on rotation / strategies -----------------------------------

    def test_rotation_skips_disabled_slot(self, temp_home, capsys):
        """active=1, slot 2 disabled — bare switch must land on 3."""
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._seed(s, 3, "c@example.com")
        s.set_account_disabled("2", True)
        capsys.readouterr()
        self._make_live(temp_home, "a@example.com", 1)

        with patch.object(s, "list_accounts"):
            s.switch()

        out = capsys.readouterr().out
        assert "Skipping Account-2 (disabled)" in out
        assert s._get_sequence_data()["activeAccountNumber"] == 3

    def test_best_strategy_ignores_disabled_candidate(self, temp_home):
        """The only other switchable account is disabled → no candidate."""
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        s.set_account_disabled("2", True)

        target, note = s._select_best_switchable("1")

        assert target is None
        assert note == "none"

    def test_explicit_switch_to_disabled_still_works(self, temp_home):
        """Disabling only holds an account out of *automatic* selection;
        an explicit `switch <num>` must still activate it."""
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        s.set_account_disabled("2", True)
        self._make_live(temp_home, "a@example.com", 1)

        with patch.object(s, "list_accounts"):
            s.switch_to("2")

        assert s._get_sequence_data()["activeAccountNumber"] == 2

    def test_remove_then_readd_clears_disabled(self, temp_home):
        """A disabled slot re-added later must not inherit the stale flag."""
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        s.set_account_disabled("2", True)

        data = s._get_sequence_data()
        del data["accounts"]["2"]
        data["sequence"] = [n for n in data["sequence"] if n != 2]
        s._write_json(s.sequence_file, data)
        self._seed(s, 2, "b@example.com")  # re-add

        assert s.is_account_disabled("2") is False

    # -- warnings ----------------------------------------------------------

    def test_disable_active_account_warns_but_sets_flag(self, temp_home, capsys):
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")  # becomes active
        self._seed(s, 2, "b@example.com")

        s.set_account_disabled("1", True)

        out = capsys.readouterr().out
        assert "active account" in out
        assert s.is_account_disabled("1") is True

    def test_disable_last_rotation_account_warns(self, temp_home, capsys):
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")

        s.set_account_disabled("1", True)
        capsys.readouterr()
        s.set_account_disabled("2", True)  # now nothing left in rotation

        assert "No accounts remain in rotation" in capsys.readouterr().out

    # -- display -----------------------------------------------------------

    def test_list_shows_disabled_marker(self, temp_home, capsys):
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        s.set_account_disabled("2", True)
        capsys.readouterr()

        with patch.object(s, "_read_credentials", return_value=""), \
             patch.object(s, "_read_account_credentials", return_value=""):
            s.list_accounts()

        out = capsys.readouterr().out
        assert "(disabled)" in out
        # Marker attaches to the disabled row, not the enabled one.
        disabled_line = next(ln for ln in out.splitlines() if ln.strip().startswith("2:"))
        assert "(disabled)" in disabled_line

    def test_json_list_carries_disabled_field(self, temp_home):
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        s.set_account_disabled("2", True)

        with patch.object(s, "_read_credentials", return_value=""), \
             patch.object(s, "_read_account_credentials", return_value=""):
            payload = s.list_accounts(json_output=True)

        rows = {r["number"]: r for r in payload["accounts"]}
        assert rows[2].get("disabled") is True
        # Additive: absent (not False) on enabled rows.
        assert "disabled" not in rows[1]


class TestDegradedReadProvenance:
    """M1 (stale-credential robustness): a credential read that fell back
    after a Keychain failure carries ``degraded=True`` — the bytes may be a
    stale generation (CC rotates keychain-only on macOS), so they may be
    ADOPTED/served but never CONSUMED (their rt POSTed)."""

    def test_an_empty_slot_under_pinned_file_mode_reads_as_empty(
        self, temp_home: Path
    ):
        """"Nothing stored" and "could not look" must stay distinguishable.

        ``keychain_unavailable`` exists so the UI can tell a slot whose
        credential could not be READ from one that genuinely has none — the
        first says "try again", the second says "re-add". Deriving it from
        ``_use_keychain()`` alone collapsed them under a pinned file mode: the
        empty slot reported keychain-unavailable, so the user was told not to
        re-add the credential that was in fact missing, which is the one action
        that fixes it.

        Fixed by the same provenance split as the degraded flag; pinned
        separately because it is a different consumer and a different remedy.
        """
        from claude_swap.credentials import Platform

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher.platform = Platform.MACOS
        store = switcher._store
        # we chose file mode; nothing failed, and the delete confirmed no
        # residual can shadow the file.
        store._pin_file_mode(residual_cleared=True)

        got = store._read_active_credentials()
        assert got.value == ""          # the premise: nothing stored
        assert got.keychain_unavailable is False, (
            "an empty slot reads as 'keychain unavailable', so the UI steers "
            "the user away from the re-add that would fix it"
        )

    def test_pinned_file_mode_is_not_a_degraded_read(self, temp_home: Path):
        """We wrote the file ourselves; it is the authority, not a stale copy.

        ``degraded`` means "this file may be behind — Claude Code writes
        rotations keychain-only, so a FAILED keychain read leaves us stale", and
        consume paths refuse those bytes. After ``_pin_file_mode`` that premise
        is inverted: nothing failed, we deliberately wrote the credential to the
        file and deleted the keychain item, and the file is what CC reads too.

        Deriving degraded from ``_use_keychain()`` conflates the two. Since
        ``_pin_file_mode`` is permanent by design (a best-effort keychain delete
        may have left a residual, so re-probing could resurrect the wrong
        account), the conflation disables active-token refresh for the whole
        process: one file-mode write and every later collect pass defers.
        """
        from claude_swap.credentials import Platform

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher.platform = Platform.MACOS      # the store reads this live
        store = switcher._store
        # Claude Code's own plaintext fallback, which is what we pinned onto.
        creds = temp_home / ".claude" / ".credentials.json"
        creds.parent.mkdir(parents=True, exist_ok=True)
        creds.write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-live", "refreshToken": "rt-live",
                "expiresAt": 9_999_999_999_000,
            }
        }))
        store._pin_file_mode(residual_cleared=True)
        assert store._read_active_credentials().degraded is False, (
            "a self-pinned file mode reads as a degraded keychain read, so "
            "cswap stops refreshing the active token for this process"
        )

    def _macos_switcher(self) -> ClaudeAccountSwitcher:
        s = ClaudeAccountSwitcher()
        s.platform = Platform.MACOS
        s._setup_directories()
        return s

    def test_file_covered_keychain_failure_is_degraded(
        self, temp_home: Path, monkeypatch, block_real_keychain
    ):
        s = self._macos_switcher()
        cred = get_credentials_path()
        cred.parent.mkdir(parents=True, exist_ok=True)
        cred.write_text("FROM-FILE")
        monkeypatch.setattr(macos_keychain, "get_password", _raise_locked)
        monkeypatch.setattr("claude_swap.credentials._ACTIVE_READ_RETRY_DELAY", 0)
        result = s._read_active_credentials()
        assert result.value == "FROM-FILE"
        assert result.keychain_unavailable is False  # display contract intact
        assert result.degraded is True               # consume ban signal

    def test_healthy_keychain_read_is_not_degraded(
        self, temp_home: Path, block_real_keychain
    ):
        s = self._macos_switcher()
        acct = macos_keychain.keychain_account_name()
        block_real_keychain.data[(CLAUDE_CODE_KEYCHAIN_SERVICE, acct)] = "FROM-KC"
        result = s._read_active_credentials()
        assert result.value == "FROM-KC"
        assert result.degraded is False

    def test_linux_file_read_is_not_degraded(self, temp_home: Path):
        s = ClaudeAccountSwitcher()
        cred = get_credentials_path()
        cred.parent.mkdir(parents=True, exist_ok=True)
        cred.write_text("FROM-FILE")
        result = s._read_active_credentials()
        assert result.value == "FROM-FILE"
        assert result.degraded is False

    def test_degraded_active_read_never_consumes(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, monkeypatch,
    ):
        """The field incident: keychain unreadable, stale file+backup agree,
        token expired → the fetch path must NOT POST the (possibly superseded)
        rt. It defers with the keychain-unavailable sentinel; no strike."""
        from claude_swap.credentials import ActiveCredentials
        from claude_swap.json_output import USAGE_KEYCHAIN_UNAVAILABLE

        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)
        stale = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-stale", "refreshToken": "rt-stale",
                "expiresAt": 1000,
            }
        })
        monkeypatch.setattr(
            switcher._store, "_read_active_credentials",
            lambda: ActiveCredentials(stale, False, True),
        )
        # as _build_accounts_info would, through the per-thread seam
        switcher._record_active_verdict(ActiveCredentials("", False, True))
        with patch.object(
                 switcher, "_read_account_credentials", return_value=stale
             ), \
             patch("claude_swap.oauth.try_refresh_oauth_credentials") as mock_refresh, \
             patch("claude_swap.oauth.try_fetch_usage_for_account") as mock_fetch:
            result = switcher._fetch_active_usage(
                "1", "test@example.com", stale
            )
        mock_refresh.assert_not_called()   # the rt is never consumed
        assert result.sentinel == USAGE_KEYCHAIN_UNAVAILABLE
        assert result.error is None        # no strike-advancing error



    def test_status_path_sets_the_degraded_flag_too(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, monkeypatch,
    ):
        """`cswap --status` must arm the same guard the collect pass does.

        _build_accounts_info copies BOTH active.keychain_unavailable and
        active.degraded onto the switcher; _active_account_usage copies only
        the first. The guard in _fetch_active_usage reads _active_read_degraded,
        so on the status path it never fires and the stale rt is POSTed — the
        exact field incident this branch exists to prevent, reachable from a
        read-only command. Every test above sets the flag by hand ("as
        _build_accounts_info would"), so none of them notices it is not set.
        """
        from claude_swap.credentials import ActiveCredentials

        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        s._write_json(s.sequence_file, sample_sequence_data)
        stale = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-stale", "refreshToken": "rt-stale",
                "expiresAt": 1000,
            }
        })
        monkeypatch.setattr(
            s._store, "_read_active_credentials",
            lambda: ActiveCredentials(stale, False, True),   # degraded=True
        )
        assert s._active_read_degraded is False              # default

        with patch.object(s, "_read_account_credentials", return_value=stale), \
             patch("claude_swap.oauth.try_fetch_usage_for_account"):
            s._active_account_usage("1", "test@example.com", "")

        assert s._active_read_degraded is True, (
            "the status path leaves the degraded guard disarmed, so a stale "
            "refresh token is POSTed and a live account can be quarantined"
        )

class TestBackupReadTriState:
    """M1: a backup read that failed at the Keychain (not rc-44 absent) must
    be distinguishable from a genuinely absent backup — 'unreadable' shows
    keychain-unavailable, never 'no credentials / re-add'."""

    def _macos_switcher(self) -> ClaudeAccountSwitcher:
        s = ClaudeAccountSwitcher()
        s.platform = Platform.MACOS
        s._setup_directories()
        return s

    def test_keychain_error_reports_unreadable(
        self, temp_home: Path, monkeypatch, block_real_keychain
    ):
        s = self._macos_switcher()
        monkeypatch.setattr(
            macos_keychain, "get_password",
            lambda *a, **k: (_ for _ in ()).throw(KeychainError("locked")),
        )
        value, unreadable = s._store._read_account_credentials_ex(
            "1", "test@example.com"
        )
        assert value == ""
        assert unreadable is True

    def test_absent_backup_is_not_unreadable(
        self, temp_home: Path, block_real_keychain
    ):
        s = self._macos_switcher()
        value, unreadable = s._store._read_account_credentials_ex(
            "1", "test@example.com"
        )
        assert value == ""
        assert unreadable is False

    def test_enc_file_read_is_not_unreadable(self, temp_home: Path):
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        s._store._write_account_credentials("1", "test@example.com", "CREDS")
        value, unreadable = s._store._read_account_credentials_ex(
            "1", "test@example.com"
        )
        assert value == "CREDS"
        assert unreadable is False


class TestEncPermissionDeniedIsUnreadable:
    """C1: a ``.enc`` that EXISTS but cannot be READ (mode 000) must not
    report the same ``("", False)`` as a genuinely absent one.

    The ``.enc`` is the ONLY backend on Linux/WSL/Windows, and it wins over
    the Keychain on macOS. ``_read_account_credentials`` already logs the
    ``OSError`` and swallows it; before the fix nothing propagated that
    swallow to ``_read_account_credentials_ex``'s verdict, so a slot holding
    a live refresh token but momentarily unreadable (permissions, a
    mid-unmount) read as "there is no backup" on every platform.
    """

    @pytest.mark.skipif(
        sys.platform == "win32" or os.geteuid() == 0,
        reason="needs POSIX permission semantics (non-root)",
    )
    def test_unreadable_enc_is_not_absent_on_linux(self, temp_home: Path):
        s = ClaudeAccountSwitcher()
        s.platform = Platform.LINUX
        s._setup_directories()
        num, email = "3", "c@example.com"
        s._write_account_credentials(num, email, '{"access_token":"live"}')
        enc = s._backup_enc_path(num, email)
        assert enc.exists(), "premise: the backup landed in the .enc"

        # CONTROL A: readable -> value, not unreadable (instrument says YES)
        v_a, unread_a = s._store._read_account_credentials_ex(num, email)
        assert v_a and unread_a is False, f"control A broken: {(bool(v_a), unread_a)}"

        # CONTROL B: genuinely absent -> ("", False) (instrument says NO)
        v_b, unread_b = s._store._read_account_credentials_ex(
            "9", "nobody@example.com"
        )
        assert (v_b, unread_b) == ("", False), f"control B broken: {(v_b, unread_b)}"

        # THE PROBE: present but unreadable
        enc.chmod(0o000)
        try:
            v_c, unread_c = s._store._read_account_credentials_ex(num, email)
        finally:
            enc.chmod(0o600)

        assert unread_c is True, (
            f"({v_c!r}, {unread_c}) is byte-identical to the ABSENT control "
            f"({v_b!r}, {unread_b}). A backup that exists and holds a live "
            "refresh token read as 'there is no backup' on the only backend "
            "this platform has."
        )

    @pytest.mark.skipif(
        sys.platform == "win32" or os.geteuid() == 0,
        reason="needs POSIX permission semantics (non-root)",
    )
    def test_unreadable_enc_is_not_absent_on_macos(
        self, temp_home: Path, block_real_keychain
    ):
        """Same probe on macOS, where the ``.enc`` wins over the Keychain:
        the Keychain has nothing for this slot, so a masked ``.enc`` read
        failure must not fall through to a clean Keychain miss."""
        s = ClaudeAccountSwitcher()
        s.platform = Platform.MACOS
        s._setup_directories()
        num, email = "3", "c@example.com"
        s._write_backup_enc(num, email, '{"access_token":"live"}')
        enc = s._backup_enc_path(num, email)

        v_a, unread_a = s._store._read_account_credentials_ex(num, email)
        assert v_a and unread_a is False, f"control A broken: {(bool(v_a), unread_a)}"

        v_b, unread_b = s._store._read_account_credentials_ex(
            "9", "nobody@example.com"
        )
        assert (v_b, unread_b) == ("", False), f"control B broken: {(v_b, unread_b)}"

        enc.chmod(0o000)
        try:
            v_c, unread_c = s._store._read_account_credentials_ex(num, email)
        finally:
            enc.chmod(0o600)

        assert unread_c is True, (
            f"({v_c!r}, {unread_c}) is byte-identical to the ABSENT control "
            f"({v_b!r}, {unread_b}) — the .enc-wins ordering let a masked "
            "read failure fall through to a clean Keychain miss."
        )


    @pytest.mark.skipif(
        sys.platform == "win32" or os.geteuid() == 0,
        reason="needs POSIX permission semantics (non-root)",
    )
    def test_unsearchable_credentials_dir_is_not_absent(self, temp_home: Path):
        """C2: the third instance, six lines above C1's fix. The
        ``enc_file.exists()`` probe's own ``OSError`` arm (an unsearchable
        ``credentials/`` dir — permissions, an NFS/SMB blip, a mid-unmount)
        swallowed into ``enc_present = False`` without ever touching
        ``failed``, so it produced the byte-identical ``("", False)`` C1 just
        fixed for the file itself.
        """
        s = ClaudeAccountSwitcher()
        s.platform = Platform.LINUX
        s._setup_directories()
        num, email = "3", "c@example.com"
        s._write_account_credentials(num, email, '{"access_token":"live"}')
        cred_dir = s._store._host.credentials_dir

        # CONTROL A: readable -> value, not unreadable (instrument says YES)
        v_a, unread_a = s._store._read_account_credentials_ex(num, email)
        assert v_a and unread_a is False, f"control A broken: {(bool(v_a), unread_a)}"

        # CONTROL B (C1's fixed arm): .enc unreadable -> unread=True, proves
        # the instrument CAN say unreadable.
        enc = s._backup_enc_path(num, email)
        enc.chmod(0o000)
        try:
            v_b, unread_b = s._store._read_account_credentials_ex(num, email)
        finally:
            enc.chmod(0o600)
        assert unread_b is True, f"control B broken: {(v_b, unread_b)}"

        # THE PROBE: the dir itself is unsearchable.
        cred_dir.chmod(0o000)
        try:
            v_c, unread_c = s._store._read_account_credentials_ex(num, email)
        finally:
            cred_dir.chmod(0o700)

        assert unread_c is True, (
            f"({v_c!r}, {unread_c}) is byte-identical to a genuinely ABSENT "
            "backup — an unsearchable credentials/ dir must not be condemned "
            "as 'there is no backup'"
        )


class TestBackupUnreadableDisplay:
    """M1: an idle slot whose backup is keychain-unreadable shows
    'keychain unavailable', never 'no credentials' (which nudges re-add)."""

    def test_idle_slot_unreadable_backup_shows_keychain_unavailable(
        self, temp_home: Path, monkeypatch, block_real_keychain,
        sample_sequence_data: dict,
    ):
        from claude_swap.json_output import (
            USAGE_KEYCHAIN_UNAVAILABLE, USAGE_NO_CREDENTIALS,
        )
        s = ClaudeAccountSwitcher()
        s.platform = Platform.MACOS
        s._setup_directories()
        monkeypatch.setattr(
            macos_keychain, "get_password",
            lambda *a, **k: (_ for _ in ()).throw(KeychainError("locked")),
        )
        # idle slot (is_active=False), empty creds, keychain pinned unusable
        s._store._read_account_credentials_ex("2", "b@example.com")  # pins cache
        info = (2, "b@example.com", "", "", False, "", "")
        assert s._static_usage_sentinel(info) == USAGE_KEYCHAIN_UNAVAILABLE

    def test_idle_slot_absent_backup_still_no_credentials(
        self, temp_home: Path, block_real_keychain,
    ):
        from claude_swap.json_output import USAGE_NO_CREDENTIALS
        s = ClaudeAccountSwitcher()
        s.platform = Platform.MACOS
        s._setup_directories()
        info = (2, "b@example.com", "", "", False, "", "")
        assert s._static_usage_sentinel(info) == USAGE_NO_CREDENTIALS

    # -- I-1: the active-slot branch collapses the same tri-state on Linux --

    def test_active_slot_unreadable_credential_shows_keychain_unavailable_on_linux(
        self, temp_home: Path,
    ):
        """``_static_usage_sentinel``'s active branch used
        ``self._active_keychain_unavailable`` alone, which is False on
        Linux/WSL/Windows even when the plaintext ``.credentials.json`` read
        outright FAILED (``credentials.py:527`` sets
        ``keychain_failed = keychain_failed`` — always False off macOS).
        ``ActiveCredentials.value is None`` is the only surviving signal
        there; ``.value or ""`` at the call site discards it before it ever
        reaches here, so the info row's ``creds`` looks genuinely empty.
        Platform-independent reproduction: the read error comes from the
        plaintext file, not the Keychain.
        """
        from claude_swap.json_output import USAGE_KEYCHAIN_UNAVAILABLE

        s = ClaudeAccountSwitcher()
        s.platform = Platform.LINUX
        s._setup_directories()

        live = json.dumps({"claudeAiOauth": {"accessToken": "sk-live"}})
        cred_path = temp_home / ".claude" / ".credentials.json"
        cred_path.write_text(live, encoding="utf-8")

        # CONTROL: readable -> no sentinel (a real credential to fetch).
        active = s._store._read_active_credentials()
        s._record_active_verdict(active)
        info_ok = (1, "a@example.com", "", "", True, active.value or "", "")
        assert s._static_usage_sentinel(info_ok) is None, "control broken"

        # PROBE: the read FAILS -> keychain unavailable, not "no credentials"
        # (which would nudge the user into an unneeded re-add).
        #
        # The failure is injected at `read_text`, not via `chmod(0o000)`:
        # POSIX mode bits do not deny the owner a read on Windows, so the
        # chmod version READ THE FILE BACK there and the probe asserted
        # against a healthy value (measured: CI test-windows, `assert
        # '{"claudeAiOauth": ...}' is None`). The defect under test is
        # platform-independent — `credentials.py`'s file-read-error arm
        # returns `value=None` on every platform — so skipping Windows would
        # drop real coverage for a fault that exists there. Injecting the
        # OSError reproduces the same arm everywhere.
        real_read_text = Path.read_text

        def failing_read_text(self_path, *a, **kw):
            if self_path == cred_path:
                raise PermissionError(13, "Permission denied")
            return real_read_text(self_path, *a, **kw)

        with patch.object(Path, "read_text", failing_read_text):
            active_bad = s._store._read_active_credentials()
        assert active_bad.value is None, "premise: unreadable file gives value=None"
        assert active_bad.keychain_unavailable is False, (
            "premise: Linux never sets this True"
        )
        s._record_active_verdict(active_bad)
        info_bad = (1, "a@example.com", "", "", True, active_bad.value or "", "")
        assert s._static_usage_sentinel(info_bad) == USAGE_KEYCHAIN_UNAVAILABLE


class TestSwitchUnreadableBackup:
    """M1: switching to a slot whose backup is keychain-unreadable errors
    with 'keychain locked/unavailable', never the re-add instruction."""

    def test_switch_to_unreadable_backup_says_keychain(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, monkeypatch, block_real_keychain,
    ):
        s = ClaudeAccountSwitcher()
        s.platform = Platform.MACOS
        s._setup_directories()
        s._write_json(s.sequence_file, sample_sequence_data)
        monkeypatch.setattr(
            macos_keychain, "get_password",
            lambda *a, **k: (_ for _ in ()).throw(KeychainError("locked")),
        )
        with pytest.raises(SwitchError) as exc:
            s.switch_to("2")
        msg = str(exc.value).lower()
        assert "keychain" in msg
        assert "add-account" not in msg   # the remedy must not be a re-add

    def test_normal_path_switch_to_unreadable_backup_says_keychain(
        self, temp_home: Path, sample_sequence_data: dict, monkeypatch,
        block_real_keychain,
    ):
        """The SAME promise on the ordinary `cswap switch`.

        _perform_switch has two target-read sites. The M1 test above lands on
        the DIRECT-ACTIVATION branch, because its fixture's live identity
        (test@example.com) matches no slot, so `current_account is None`. That
        is the fresh-machine / post-import / --force path. Every ordinary
        switch on a working install — a live login that DOES resolve to a slot
        — takes the NORMAL branch, which still sends the user to a re-add.
        """
        s = ClaudeAccountSwitcher()
        s.platform = Platform.MACOS
        s._setup_directories()
        s._write_json(s.sequence_file, sample_sequence_data)
        # A live login that resolves to slot 1, so the switch takes the
        # normal (back-up-current-then-activate) branch.
        cfg = s._get_claude_config_path()
        cfg.parent.mkdir(parents=True, exist_ok=True)
        s._write_json(cfg, {"oauthAccount": {
            "emailAddress": "account1@example.com",
            "accountUuid": "uuid-1",
            "organizationUuid": "", "organizationName": "",
        }})
        # Readable from the plaintext fallback, so slot 1 resolves even with
        # every Keychain read denied.
        live = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-live", "refreshToken": "rt-live",
            "expiresAt": 9999999999000}})
        s._store._write_active_credentials_file(live)
        s._write_account_credentials("2", "account2@example.com", live)
        s._write_account_config("2", "account2@example.com", json.dumps(
            {"oauthAccount": {"emailAddress": "account2@example.com",
                              "accountUuid": "uuid-2",
                              "organizationUuid": "",
                              "organizationName": ""}}))
        assert s.current_account_number() == "1"

        monkeypatch.setattr(macos_keychain, "get_password", _raise_locked)
        with pytest.raises(SwitchError) as exc:
            s.switch_to("2")
        msg = str(exc.value).lower()
        assert "keychain" in msg
        assert "add-account" not in msg   # the remedy must not be a re-add


class TestConsumeGate:
    """M2: every backup-refresh-token POST is serialized by the consume lock —
    locked re-read → unlocked POST → reacquire-and-CAS persist. Two call sites
    take that lock (this gate and `_fetch_active_usage`'s recovery branch), so
    what is single is the serialization, not the call site."""

    _OLD = json.dumps({
        "claudeAiOauth": {
            "accessToken": "sk-old", "refreshToken": "rt-old",
            "expiresAt": 1000,
        }
    })
    _NEW = json.dumps({
        "claudeAiOauth": {
            "accessToken": "sk-new", "refreshToken": "rt-new",
            "expiresAt": 9999999999000,
        }
    })

    def _switcher(self, sample_sequence_data):
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        s._write_json(s.sequence_file, sample_sequence_data)
        return s

    def test_gate_rereads_under_lock_and_posts_rereread_bytes(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """The POST must consume the bytes RE-READ under the lock, not the
        caller's snapshot — a fresher backup written since the caller read
        means the caller's rt is the consumed predecessor."""
        s = self._switcher(sample_sequence_data)
        fresher = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-fresher", "refreshToken": "rt-fresher",
                "expiresAt": 2000,
            }
        })
        s._write_account_credentials("1", "test@example.com", fresher)
        posted = {}

        def mock_refresh(credentials, **kw):
            posted["creds"] = credentials
            return oauth.RefreshOutcome(self._NEW, None)

        with patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   side_effect=mock_refresh):
            # caller holds a STALE snapshot (_OLD); gate must ignore it
            result = s.consume_backup_grant("1", "test@example.com", self._OLD)

        assert posted["creds"] == fresher      # re-read bytes, not snapshot
        assert result.credentials == self._NEW
        assert s._read_account_credentials("1", "test@example.com") == self._NEW

    def test_gate_cas_persist_detects_racing_writer(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """If the backup's lineage moved while our POST was in flight,
        a writer won the race: the successor is stashed (never discarded),
        the store's newer lineage is adopted, nothing overwritten."""
        s = self._switcher(sample_sequence_data)
        s._write_account_credentials("1", "test@example.com", self._OLD)
        racer = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-racer", "refreshToken": "rt-racer",
                "expiresAt": 8888888888000,
            }
        })

        def mock_refresh(credentials, **kw):
            # while the POST is in flight, another writer replaces the backup
            s._store._write_account_credentials(
                "1", "test@example.com", racer
            )
            return oauth.RefreshOutcome(self._NEW, None)

        with patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   side_effect=mock_refresh):
            result = s.consume_backup_grant("1", "test@example.com", self._OLD)

        # racer's write survives; our successor was stashed, not written over it
        assert s._read_account_credentials("1", "test@example.com") == racer
        assert result.credentials == racer      # adopt the store's newer lineage
        unclaimed = s.list_unclaimed_credentials()
        assert unclaimed, "consumed successor must be stashed, never discarded"

    def test_gate_prefers_newer_session_profile_lineage(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """A dead session profile holding a NEWER lineage than the backup
        (claude rotated inside the profile — #96's shape) means the backup rt
        is the consumed predecessor: the gate must resync profile→backup and
        POST the profile's rt, never the backup's."""
        from claude_swap.session import session_dir_for
        s = self._switcher(sample_sequence_data)
        s._write_account_credentials("1", "test@example.com", self._OLD)
        profile_newer = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-prof", "refreshToken": "rt-prof",
                "expiresAt": 5000,   # newer generation than backup's 1000
            }
        })
        sdir = session_dir_for(s.backup_dir, "1", "test@example.com")
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / ".credentials.json").write_text(profile_newer)
        posted = {}

        def mock_refresh(credentials, **kw):
            posted["creds"] = credentials
            return oauth.RefreshOutcome(self._NEW, None)

        with patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   side_effect=mock_refresh), \
             patch.object(s, "_live_session_pids", return_value=[]):
            s.consume_backup_grant("1", "test@example.com", self._OLD)

        assert posted["creds"] == profile_newer

    def test_gate_invalid_grant_returns_error_without_persist(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        s = self._switcher(sample_sequence_data)
        s._write_account_credentials("1", "test@example.com", self._OLD)

        with patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   return_value=oauth.RefreshOutcome(None, "invalid_grant")):
            result = s.consume_backup_grant("1", "test@example.com", self._OLD)

        assert result.error == "invalid_grant"
        # the dead bytes stay put — no destructive write
        assert s._read_account_credentials("1", "test@example.com") == self._OLD

    def _stash_successor_of(self, s, credentials: str, consumed: str) -> None:
        """A prior gate consumed `consumed`'s grant and could not persist
        `credentials`; the store still holds `consumed`."""
        s._store._write_unclaimed_credential(credentials, {
            "reason": "consume-gate-persist-failed",
            "configSlot": "1",
            "consumedFp": oauth.credential_fingerprint(consumed),
            "fingerprint": oauth.credential_fingerprint(credentials),
        })

    def test_an_unreadable_stash_manifest_defers_instead_of_posting(
        self, temp_home: Path, sample_sequence_data: dict, monkeypatch
    ):
        """A manifest that cannot be READ is not a manifest with no rows.

        `_read_stash_manifest` collapses every failure to `{}`, and
        `_adopt_stashed_successor` iterates manifest rows only — so with a
        stash pending (store = the spent generation) and the manifest
        momentarily unreadable, the gate is blind to the successor and POSTs
        the generation whose grant is already spent. That POST is the one
        this whole gate exists to prevent, and the failure is CORRELATED: the
        stash exists because storage I/O already failed once.
        """
        s = self._switcher(sample_sequence_data)
        s._write_account_credentials("1", "test@example.com", self._OLD)
        self._stash_successor_of(s, self._NEW, self._OLD)

        # Both readers, so the simulation does not depend on WHICH one the
        # manifest reader happens to use — a patch that misses the real call
        # stops simulating anything, and the gate then reads a healthy
        # manifest while the test still claims to be testing an unreadable one.
        def _deny(real):
            def denied(self_path, *a, **kw):
                if self_path.name == ".unclaimed-manifest.json":
                    raise PermissionError(13, "Permission denied")
                return real(self_path, *a, **kw)
            return denied

        monkeypatch.setattr(Path, "read_bytes", _deny(Path.read_bytes))
        monkeypatch.setattr(Path, "read_text", _deny(Path.read_text))
        posted = []

        def mock_refresh(credentials, **kw):
            posted.append(credentials)
            return oauth.RefreshOutcome(self._NEW, None)

        with patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   side_effect=mock_refresh):
            result = s.consume_backup_grant("1", "test@example.com", self._OLD)

        assert posted == [], (
            "POSTed the spent generation while its successor sat in a stash "
            "the gate could not see"
        )
        assert result.error == "stash-unreadable"

    def test_a_stash_write_refuses_an_unreadable_manifest(
        self, temp_home: Path, sample_sequence_data: dict, monkeypatch
    ):
        """The mutator read the plain wrapper, which discards the verdict.

        A stash writer that reads an unreadable-but-VALID manifest as `{}`
        does not merely miss rows: `_write_stash_manifest` then finds the file
        unparseable too, renames the good manifest aside, and writes a fresh
        one holding only the new row. Every previously mapped successor is
        orphaned in one step — and this runs in exactly the correlated setting
        where a stash is being written because storage already misbehaved.

        `_write_unclaimed_credential`'s own contract is that a failed stash
        must be LOUD, because callers treat a successful one as the licence to
        overwrite the live store.
        """
        s = self._switcher(sample_sequence_data)
        self._stash_successor_of(s, self._NEW, self._OLD)
        path = s._store._stash_manifest_path()
        before = path.read_bytes()

        deny = [True]

        def _deny(real):
            def denied(self_path, *a, **kw):
                if deny[0] and self_path.name == ".unclaimed-manifest.json":
                    raise PermissionError(13, "Permission denied")
                return real(self_path, *a, **kw)
            return denied

        monkeypatch.setattr(Path, "read_bytes", _deny(Path.read_bytes))
        monkeypatch.setattr(Path, "read_text", _deny(Path.read_text))

        with pytest.raises(CredentialReadError):
            s._store._write_unclaimed_credential(self._OLD, {
                "reason": "consume-gate-persist-failed", "configSlot": "1",
            })

        deny[0] = False
        assert path.read_bytes() == before, (
            "renamed a healthy manifest aside and replaced it with one row — "
            "every other slot's stashed successor is now unmappable"
        )
        assert not list(
            s.credentials_dir.glob(".unclaimed-manifest.json.corrupt-*")
        ), "set a READABLE manifest aside as corrupt"

    def test_the_purge_exit_the_fail_closed_message_names_actually_works(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """A remedy named in an error message has to be a remedy.

        Failing closed on corrupt+orphans is only defensible because the
        operator has a way out, and the message names one: `cswap unclaimed`
        to see them, `--purge` to drop one. Both run against a CORRUPT
        manifest, so the mutator must NOT refuse there — which is why its
        refusal is scoped to `unreadable`. Walk the whole exit rather than
        asserting the sentence.
        """
        s = self._switcher(sample_sequence_data)
        s._write_account_credentials("1", "test@example.com", self._OLD)
        self._stash_successor_of(s, self._NEW, self._OLD)
        s._store._stash_manifest_path().write_text("{not json at all")

        # 1. the operator can SEE the orphan, by glob, with no readable rows
        listed = s.list_unclaimed_credentials()
        assert listed, "corrupt manifest hid the orphan from `cswap unclaimed`"

        # 2. and can DROP it — the mutator does not refuse on corrupt
        for entry_id in list(listed):
            s._store._remove_unclaimed_credential(entry_id)

        # 3. after which nothing is at risk and the slot moves again
        assert s._store._stash_entry_files_exist() is False
        posted = []

        def mock_refresh(credentials, **kw):
            posted.append(credentials)
            return oauth.RefreshOutcome(self._NEW, None)

        with patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   side_effect=mock_refresh):
            result = s.consume_backup_grant("1", "test@example.com", self._OLD)

        assert posted == [self._OLD], "the documented exit did not unblock it"
        assert result.error is None

    @pytest.mark.skipif(
        sys.platform == "win32" or os.geteuid() == 0,
        reason="needs POSIX permission semantics (non-root)",
    )
    def test_an_unlistable_dir_counts_as_entries_at_risk(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """The corrupt policy asks "is anything at risk"; not knowing is YES.

        `_stash_entry_files_exist` is what lets a corrupt manifest proceed, so
        a scan that FAILS must not read as "nothing on disk" — that is the
        empty-means-safe conflation this whole PR removes, one level down. The
        caller spends a grant on this answer.

        The REAL state, not a monkeypatched scan. The first version of this
        test patched `Path.glob` to raise — and passed against a detector
        whose except arm was dead code, because `glob` SUPPRESSES scan
        OSErrors (3.14: a searchable-but-unlistable dir answers `[]`, no
        raise, with the orphan sitting right there). A test that fakes the
        raise cannot see that the raise never happens; only the state itself
        can.
        """
        s = self._switcher(sample_sequence_data)
        self._stash_successor_of(s, self._NEW, self._OLD)
        assert s._store._stash_entry_files_exist() is True, "test premise"

        cred_dir = s._store._host.credentials_dir
        os.chmod(cred_dir, 0o311)  # searchable (files reachable), unlistable
        try:
            assert s._store._stash_entry_files_exist() is True
        finally:
            os.chmod(cred_dir, 0o700)

    def test_a_missing_credentials_dir_is_provably_nothing_stashed(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """The one scan failure that answers False: no dir, nothing ever
        stashed. Folding it into the at-risk arm would fail a corrupt-manifest
        slot closed on a machine that never stashed anything — permanently,
        since with no entries there is no purge exit to walk."""
        s = self._switcher(sample_sequence_data)
        cred_dir = s._store._host.credentials_dir
        if cred_dir.is_dir():
            import shutil as _shutil

            _shutil.rmtree(cred_dir)

        assert s._store._stash_entry_files_exist() is False

    def test_a_manifest_without_a_dict_entries_member_is_corrupt(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """Parseable-but-structurally-wrong is corrupt, not ok-with-no-rows.

        `{"entries": "bogus"}` decodes and parses, so it reaches neither the
        unreadable nor the unparseable arm — and reading it as `"ok"` with no
        rows bypasses the corrupt+orphans fail-closed condition exactly the
        way unparseable bytes used to. The rows are equally unestablishable in
        both shapes, so both get the corrupt verdict.
        """
        s = self._switcher(sample_sequence_data)
        for payload in ('{"schemaVersion": 1, "entries": "bogus"}',
                        '{"schemaVersion": 1}'):
            s._store._stash_manifest_path().write_text(payload)
            entries, verdict = s._store._read_stash_manifest_ex()
            assert (entries, verdict) == ({}, "corrupt"), payload

    def test_a_corrupt_manifest_with_orphan_entries_fails_closed(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """Corrupt + orphan bytes on disk: a successor may be pending.

        The `{}` reading costs a POST of the slot's spent generation, which
        does not "self-heal at the cost of one POST" — it returns
        invalid_grant, and the gate returns before any manifest write, so
        nothing is ever set aside. The exit is the operator's
        (`cswap unclaimed --purge`, which still lists orphans by glob), not a
        POST that strikes a live account.
        """
        s = self._switcher(sample_sequence_data)
        s._write_account_credentials("1", "test@example.com", self._OLD)
        self._stash_successor_of(s, self._NEW, self._OLD)
        s._store._stash_manifest_path().write_text("{not json at all")
        posted = []

        def mock_refresh(credentials, **kw):
            posted.append(credentials)
            return oauth.RefreshOutcome(None, "invalid_grant")

        with patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   side_effect=mock_refresh):
            result = s.consume_backup_grant("1", "test@example.com", self._OLD)

        assert posted == [], "POSTed a spent grant with a successor on disk"
        assert result.error == "stash-unreadable"

    def test_a_corrupt_manifest_still_posts_rather_than_deadlocking(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """CORRUPT with NOTHING to protect: set aside and proceed.

        No orphan entry files exist, so `{}` is not a guess about whether a
        successor is pending — there are provably no bytes on disk to lose.
        Failing closed here would deadlock for nothing: the repair is
        ``_write_stash_manifest`` renaming the bad file aside, and that runs
        only on a manifest WRITE, which deferring is precisely what prevents.

        The sibling test covers corrupt WITH orphan entries, where a pending
        successor may exist and the answer flips to fail-closed. The earlier
        version of THIS test stashed a successor and mocked the POST of the
        spent generation to succeed — a world that cannot happen, which is
        what made "self-heal at the cost of one POST" look true.
        """
        s = self._switcher(sample_sequence_data)
        s._write_account_credentials("1", "test@example.com", self._OLD)
        s._store._stash_manifest_path().write_text("{not json at all")
        posted = []

        def mock_refresh(credentials, **kw):
            posted.append(credentials)
            return oauth.RefreshOutcome(self._NEW, None)

        with patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   side_effect=mock_refresh):
            result = s.consume_backup_grant("1", "test@example.com", self._OLD)

        assert posted == [self._OLD], "a corrupt manifest froze the slot"
        assert result.error is None

    def test_a_manifest_of_invalid_utf8_is_corrupt_not_unreadable(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """Undecodable BYTES are corrupt, not inaccessible.

        `read_text` raises UnicodeDecodeError — a ValueError, not an OSError —
        so a verdict that splits on OSError alone lets it escape the reader
        entirely. The old single `except Exception` swallowed it to `{}`;
        losing that is a regression the unreadable/corrupt split can introduce
        silently, because the two named failure modes both still pass.
        """
        s = self._switcher(sample_sequence_data)
        s._write_account_credentials("1", "test@example.com", self._OLD)
        self._stash_successor_of(s, self._NEW, self._OLD)
        s._store._stash_manifest_path().write_bytes(b"\xff\xfe\x00not utf8")

        entries, verdict = s._store._read_stash_manifest_ex()

        assert (entries, verdict) == ({}, "corrupt")

    def test_a_readable_empty_manifest_still_posts(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """The control. Nothing stashed really does mean nothing to adopt —
        without this, the test above passes on a gate that never POSTs."""
        s = self._switcher(sample_sequence_data)
        s._write_account_credentials("1", "test@example.com", self._OLD)
        posted = []

        def mock_refresh(credentials, **kw):
            posted.append(credentials)
            return oauth.RefreshOutcome(self._NEW, None)

        with patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   side_effect=mock_refresh):
            result = s.consume_backup_grant("1", "test@example.com", self._OLD)

        assert posted == [self._OLD]
        assert result.error is None

    def test_a_removed_slot_defers_instead_of_posting_the_snapshot(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """`refresh_input = current or snapshot` resurrects a deleted account.

        A slot removed between the caller's read and this gate's locked
        re-read comes back ABSENT (not unreadable), and the `or` falls back to
        the caller's snapshot: the gate spends a grant and stashes the
        successor of an account the user just deleted. The CAS branch twenty
        lines later already refuses to write that successor back
        (`consume-gate-slot-removed`) — this is the same rule, one step
        earlier, before the grant is spent rather than after.

        All three production callers source their snapshot from the backup
        store, so an absent re-read really does mean removed.
        """
        s = self._switcher(sample_sequence_data)
        # No stored credential for slot 1: `cswap remove` landed first.
        posted = []

        def mock_refresh(credentials, **kw):
            posted.append(credentials)
            return oauth.RefreshOutcome(self._NEW, None)

        with patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   side_effect=mock_refresh):
            result = s.consume_backup_grant("1", "test@example.com", self._OLD)

        assert posted == [], "spent a grant for a slot that no longer exists"
        assert result.error == "transient"
        assert not s.list_unclaimed_credentials(), (
            "stashed a successor for an account the user deleted"
        )


    def test_an_absent_slot_defers_even_when_a_newer_profile_exists(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """The defer must not quietly regress into a heal.

        With `or snapshot` gone, the session-profile precedence block below is
        no longer reachable for an ABSENT slot — it used to resync that
        profile into the empty slot and POST it. That is the resurrect class
        this PR forbids everywhere else, and the same verdict the CAS branch
        reaches for a slot emptied mid-POST: a half-finished delete must not
        be undone by a background poll. The wiped-keychain case keeps its
        documented remedy (re-add).

        Pinned because it is defined behaviour on a path no test covered
        before, which is exactly the kind that gets "fixed" back.
        """
        from claude_swap.session import session_dir_for
        s = self._switcher(sample_sequence_data)
        # No stored credential for slot 1 — but a profile that outlived it.
        profile_newer = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-prof", "refreshToken": "rt-prof",
                "expiresAt": 9999999999000,
            }
        })
        sdir = session_dir_for(s.backup_dir, "1", "test@example.com")
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / ".credentials.json").write_text(profile_newer)
        posted = []

        def mock_refresh(credentials, **kw):
            posted.append(credentials)
            return oauth.RefreshOutcome(self._NEW, None)

        with patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   side_effect=mock_refresh), \
             patch.object(s, "_live_session_pids", return_value=[]):
            result = s.consume_backup_grant("1", "test@example.com", self._OLD)

        assert posted == [], "healed a removed slot from its surviving profile"
        assert result.error == "transient"
        assert not s._read_account_credentials("1", "test@example.com"), (
            "resurrected the slot's stored credential from the profile"
        )


class TestInactiveRefreshRoutesThroughGate:
    """M2: the collector's inactive-account refresh consumes via the gate,
    not via a direct POST of its own snapshot."""

    def test_expired_inactive_fetch_uses_gate(
        self, temp_home: Path, sample_sequence_data: dict, monkeypatch
    ):
        sample_sequence_data["accounts"]["2"] = {
            "email": "b@example.com", "uuid": "u2",
            "organizationUuid": "", "organizationName": "",
        }
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        s._write_json(s.sequence_file, sample_sequence_data)
        expired = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-old", "refreshToken": "rt-old",
                "expiresAt": 1000,
            }
        })
        s._write_account_credentials("2", "b@example.com", expired)
        fresh = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-new", "refreshToken": "rt-new",
                "expiresAt": 9999999999000,
            }
        })
        gate = {}

        def mock_gate(num, email, snapshot):
            gate["args"] = (num, email)
            s._store._write_account_credentials(num, email, fresh)
            return oauth.RefreshOutcome(fresh, None)

        monkeypatch.setattr(s, "consume_backup_grant", mock_gate)
        direct = {}

        def direct_post(*a, **k):
            direct["called"] = True
            return oauth.RefreshOutcome(None, "transient")

        with patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   side_effect=direct_post), \
             patch.object(s, "_live_session_pids", return_value=[]), \
             patch("claude_swap.oauth.request_usage_data",
                   return_value={"five_hour": {"utilization": 5}}):
            info = (2, "b@example.com", "", "", False, expired, "")
            record = s._fetch_account_usage(info)

        assert gate["args"] == ("2", "b@example.com")
        assert "called" not in direct
        assert record.error is None


class TestStrikeUnbindsInCollector:
    """M3: the collector's quarantine scan passes the stored credential's
    fingerprint — a replaced credential lifts 're-login needed' without a
    clear call."""

    def test_relogin_lifts_after_credential_replaced(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, monkeypatch
    ):
        from claude_swap.json_output import USAGE_RELOGIN_REQUIRED
        from claude_swap.usage_store import FetchRecord as StoreRecord
        sample_sequence_data["accounts"]["2"] = {
            "email": "b@example.com", "uuid": "u2",
            "organizationUuid": "", "organizationName": "",
        }
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        s._write_json(s.sequence_file, sample_sequence_data)
        dead = json.dumps({
            "claudeAiOauth": {"accessToken": "a", "refreshToken": "rt-dead",
                              "expiresAt": 1000}})
        s._write_account_credentials("2", "b@example.com", dead)
        # strike the dead generation (fp recorded)
        identities = {"2": ("b@example.com", "")}
        store = s._usage_store
        claims = store.reserve(["2"], identities, respect_plans=False)
        store.record(
            {"2": StoreRecord(error="invalid_grant",
                              struck_fp=oauth.credential_fingerprint(dead))},
            identities, claims,
        )
        info = [(2, "b@example.com", "", "", False, dead, "")]
        entries = s._collect_usage_entries(info, fetch=set())
        assert entries["2"].sentinel == USAGE_RELOGIN_REQUIRED
        # replace the credential (fresh lineage) — quarantine must lift
        fresh = json.dumps({
            "claudeAiOauth": {"accessToken": "b", "refreshToken": "rt-new",
                              "expiresAt": 1000}})
        s._write_account_credentials("2", "b@example.com", fresh)
        info = [(2, "b@example.com", "", "", False, fresh, "")]
        entries = s._collect_usage_entries(info, fetch=set())
        assert entries["2"].sentinel != USAGE_RELOGIN_REQUIRED


class TestStoreResolutionParity:
    """M4: when CC resolves its credential store somewhere cswap does not
    mirror, consuming/mutating operations refuse instead of operating on a
    store CC no longer uses."""

    def test_securestorage_env_refuses_consume(
        self, temp_home: Path, sample_sequence_data: dict, monkeypatch
    ):
        monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", "/tmp/other")
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        s._write_json(s.sequence_file, sample_sequence_data)
        creds = json.dumps({
            "claudeAiOauth": {"accessToken": "a", "refreshToken": "rt",
                              "expiresAt": 1000}})
        s._write_account_credentials("1", "test@example.com", creds)
        with patch("claude_swap.oauth.try_refresh_oauth_credentials") as mock_post:
            result = s.consume_backup_grant("1", "test@example.com", creds)
        mock_post.assert_not_called()
        assert result.error == "store-unmirrored"

    def test_session_shell_config_dir_refuses_switch(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, monkeypatch
    ):
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        s._write_json(s.sequence_file, sample_sequence_data)
        inside = s.backup_dir / "sessions" / "1-test-example-com"
        inside.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(inside))
        with pytest.raises(SwitchError) as exc:
            s.switch_to("2")
        assert "session" in str(exc.value).lower()

    def test_normal_env_unaffected(
        self, temp_home: Path, sample_sequence_data: dict, monkeypatch
    ):
        monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        s._write_json(s.sequence_file, sample_sequence_data)
        creds = json.dumps({
            "claudeAiOauth": {"accessToken": "a", "refreshToken": "rt",
                              "expiresAt": 1000}})
        s._write_account_credentials("1", "test@example.com", creds)
        fresh = json.dumps({
            "claudeAiOauth": {"accessToken": "b", "refreshToken": "rt2",
                              "expiresAt": 9999999999000}})
        with patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   return_value=oauth.RefreshOutcome(fresh, None)):
            result = s.consume_backup_grant("1", "test@example.com", creds)
        assert result.credentials == fresh

    def test_a_secure_store_miss_does_not_capture_the_other_profiles_key(
        self, temp_home: Path, monkeypatch
    ):
        """The API-key tail must stay inside the profile claude is reading.

        The secure-store branch refuses to fall back into the active store,
        because with the two vars diverged that captures a profile claude is
        not reading. The shared tail below it does exactly that: it reads
        ``primaryApiKey`` through ``get_global_config_path()``, which follows
        ``CLAUDE_CONFIG_DIR``. So a miss in the secure store — which claude
        sees as a logged-out environment — captures the OTHER profile's key.

        The tail cannot be reached by the secure profile's own key either:
        ``read_config_dir_credentials`` is OAuth-only (keychain +
        ``.credentials.json``) and never looks at ``primaryApiKey``.
        """
        active = temp_home / "profileA"
        secure = temp_home / "profileB"
        active.mkdir()
        secure.mkdir()
        (active / ".claude.json").write_text(
            json.dumps({"primaryApiKey": "sk-ant-api-PROFILE-A"})
        )
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(active))
        monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", str(secure))

        s = ClaudeAccountSwitcher()
        s._setup_directories()

        assert s._read_capture_credentials() != "sk-ant-api-PROFILE-A"

    def test_refuse_degraded_capture_is_not_a_toctou(
        self, temp_home: Path, monkeypatch, block_real_keychain
    ):
        """I-1: ``_refuse_degraded_capture`` reads the active credential to
        CHECK it, then the plain default path (``CLAUDE_CONFIG_DIR`` unset)
        reads it AGAIN via ``_read_credentials()`` to CAPTURE it. Those are
        two separate Keychain reads. A Keychain that answers on the guard's
        read and fails on the capture's read passes the guard and still
        captures the possibly-stale plaintext fallback -- exactly what the
        guard's docstring says it prevents.

        Driven without patching the seam itself: only
        ``macos_keychain.get_password`` is made flaky (healthy once, then
        locked), the wrapper's own documented failure mode.
        """
        s = ClaudeAccountSwitcher()
        s.platform = Platform.MACOS
        s._setup_directories()
        cred = get_credentials_path()
        cred.parent.mkdir(parents=True, exist_ok=True)
        cred.write_text("STALE-FALLBACK-PLAINTEXT")
        monkeypatch.setattr("claude_swap.credentials._ACTIVE_READ_RETRY_DELAY", 0)
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)

        calls = {"n": 0}
        healthy_value = '{"claudeAiOauth":{"refreshToken":"HEALTHY-RT"}}'

        def flaky_get_password(service, account):
            calls["n"] += 1
            if calls["n"] == 1:
                # first Keychain read: healthy, answers with a real
                # credential -- ActiveCredentials.degraded is False.
                return healthy_value
            raise KeychainError("locked")  # any SECOND read: now locked

        monkeypatch.setattr(macos_keychain, "get_password", flaky_get_password)

        captured = s._read_capture_credentials()
        assert captured == healthy_value, (
            "DEFECT: the guard-check and the capture-use must be the SAME "
            "read. A second, independent Keychain read that can fail after "
            "a healthy first read is a TOCTOU -- the captured value must be "
            f"the bytes the guard itself verified, not {captured!r} (a stale "
            "fallback file reached only because a SECOND read failed)"
        )
        assert calls["n"] == 1, (
            "premise: exactly ONE Keychain read backs both the guard's "
            "verdict and the captured value -- a second call means the "
            "TOCTOU window is still open"
        )

    def test_refuse_degraded_capture_control_persistently_locked(
        self, temp_home: Path, monkeypatch, block_real_keychain
    ):
        """CONTROL (opposite direction): a Keychain locked on EVERY read must
        still be refused -- the guard is armed on this construction, so I-1's
        finding is about the TOCTOU window, not a guard that never fires."""
        s = ClaudeAccountSwitcher()
        s.platform = Platform.MACOS
        s._setup_directories()
        cred = get_credentials_path()
        cred.parent.mkdir(parents=True, exist_ok=True)
        cred.write_text("STALE-FALLBACK-PLAINTEXT")
        monkeypatch.setattr("claude_swap.credentials._ACTIVE_READ_RETRY_DELAY", 0)
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)
        monkeypatch.setattr(macos_keychain, "get_password", _raise_locked)

        with pytest.raises(CredentialReadError):
            s._read_capture_credentials()


class TestConsumeGateLockFailures:
    """Review findings 1+2: LockError before the POST is a clean transient;
    LockError AFTER the POST must stash the consumed successor, never
    destroy it — and never raise out of the gate."""

    _OLD = json.dumps({
        "claudeAiOauth": {"accessToken": "sk-old", "refreshToken": "rt-old",
                          "expiresAt": 1000}})
    _NEW = json.dumps({
        "claudeAiOauth": {"accessToken": "sk-new", "refreshToken": "rt-new",
                          "expiresAt": 9999999999000}})

    def _switcher(self, sample_sequence_data):
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        s._write_json(s.sequence_file, sample_sequence_data)
        return s

    def test_lock_failure_before_post_is_transient(
        self, temp_home: Path, sample_sequence_data: dict, monkeypatch
    ):
        from claude_swap.exceptions import LockError as LE
        s = self._switcher(sample_sequence_data)
        s._write_account_credentials("1", "test@example.com", self._OLD)

        from claude_swap.locking import FileLock as real_lock

        class FailingLock:
            def __init__(self, *a, **k): pass
            # The per-slot consume lock acquires cleanly; the SLOT lock
            # (window 1) is the one held elsewhere.
            def acquire(self, *a, **k): return True
            def release(self): pass
            def __enter__(self): raise LE("held elsewhere")
            def __exit__(self, *a): return False

        monkeypatch.setattr("claude_swap.switcher.FileLock", FailingLock)
        with patch("claude_swap.oauth.try_refresh_oauth_credentials") as post:
            out = s.consume_backup_grant("1", "test@example.com", self._OLD)
        post.assert_not_called()          # nothing consumed
        assert out.error == "transient"   # clean defer, no raise

    def test_lock_failure_after_post_stashes_successor(
        self, temp_home: Path, sample_sequence_data: dict, monkeypatch
    ):
        from claude_swap.exceptions import LockError as LE
        s = self._switcher(sample_sequence_data)
        s._write_account_credentials("1", "test@example.com", self._OLD)
        from claude_swap.locking import FileLock as real_lock
        calls = {"n": 0}

        class SecondLockFails:
            def __init__(self, *a, **k):
                self._inner = real_lock(*a, **k)
            # consume lock (acquire/release) works; the second SLOT lock
            # window (the CAS persist) is the one that fails.
            def acquire(self, *a, **k):
                return self._inner.acquire(*a, **k)
            def release(self):
                self._inner.release()
            def __enter__(self):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise LE("held elsewhere")
                return self._inner.__enter__()
            def __exit__(self, *a):
                return self._inner.__exit__(*a)

        monkeypatch.setattr("claude_swap.switcher.FileLock", SecondLockFails)
        with patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   return_value=oauth.RefreshOutcome(self._NEW, None)):
            out = s.consume_backup_grant("1", "test@example.com", self._OLD)
        # successor survives: returned to the caller AND stashed
        assert out.credentials == self._NEW
        assert s.list_unclaimed_credentials(), "successor must be stashed"

    def test_a_stashed_successor_is_not_reported_as_freshened(
        self, temp_home: Path, sample_sequence_data: dict, monkeypatch
    ):
        """The stash leaves the CONSUMED grant in the store, so the slot is not
        safe to activate — and the gate must say so.

        autoswitch reads ``error is None`` as "the gate already persisted the
        successor" and switches to the slot. After a stash it has not: the
        backup still holds the generation whose refresh token was just spent,
        so activating it puts the user on an expired access token that can
        never refresh, and Claude Code logs the account out. Upstream raised
        LockError here, which aborted the tick — safe by accident. Stashing is
        the better behaviour; reporting it as success is not.
        """
        from claude_swap.exceptions import LockError as LE
        from claude_swap.locking import FileLock as real_lock

        s = self._switcher(sample_sequence_data)
        s._write_account_credentials("1", "test@example.com", self._OLD)
        calls = {"n": 0}

        class SecondLockFails:
            def __init__(self, *a, **k):
                self._inner = real_lock(*a, **k)
            def acquire(self, *a, **k):
                return self._inner.acquire(*a, **k)
            def release(self):
                self._inner.release()
            def __enter__(self):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise LE("held elsewhere")
                return self._inner.__enter__()
            def __exit__(self, *a):
                return self._inner.__exit__(*a)

        monkeypatch.setattr("claude_swap.switcher.FileLock", SecondLockFails)
        with patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   return_value=oauth.RefreshOutcome(self._NEW, None)):
            out = s.consume_backup_grant("1", "test@example.com", self._OLD)

        # The store still holds the spent generation — the premise of the bug.
        assert s.read_account_credentials("1", "test@example.com") == self._OLD
        # So the outcome must not read as a completed refresh.
        assert out.error is not None, (
            "a stashed successor reads as freshened; autoswitch will switch "
            "onto the consumed generation still in the store"
        )
        # The successor is still preserved for the next pass to adopt.
        assert s.list_unclaimed_credentials(), "successor must still be stashed"


class TestPermanentlyUnreadableStashRow:
    """Minor 1: a row that is unreadable on EVERY pass is never retired and
    the gate defers on it forever -- reported as ``transient``, i.e. the
    "(network?)" false alarm this branch fixes elsewhere.

    The deferral itself is correct and stays: the bytes are the sole copy of
    a generation the slot already consumed, and nothing on disk distinguishes
    "locked for a minute" from "locked forever", so retiring on a strike
    count would destroy a live refresh token whenever the cause was slow
    rather than permanent. What must change is the LABEL -- the condition is
    deterministic, local, and needs a human, so it must not be reported as
    network trouble.
    """

    _OLD = json.dumps({
        "claudeAiOauth": {"accessToken": "sk-old", "refreshToken": "rt-old",
                          "expiresAt": 1000}})
    _NEW = json.dumps({
        "claudeAiOauth": {"accessToken": "sk-new", "refreshToken": "rt-new",
                          "expiresAt": 9999999999000}})

    def _switcher(self, sample_sequence_data):
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        s._write_json(s.sequence_file, sample_sequence_data)
        return s

    @pytest.mark.skipif(
        sys.platform == "win32" or os.geteuid() == 0,
        reason="needs POSIX permission semantics (non-root)",
    )
    def test_ten_passes_name_the_condition_instead_of_network(
        self, temp_home: Path, sample_sequence_data: dict,
    ):
        s = self._switcher(sample_sequence_data)
        s._write_account_credentials("1", "test@example.com", self._OLD)
        entry_id = s._store._write_unclaimed_credential(
            self._NEW,
            {"reason": "consume-gate-persist-lock-failed",
             "configSlot": "1",
             "consumedFp": oauth.credential_fingerprint(self._OLD),
             "fingerprint": oauth.credential_fingerprint(self._NEW)},
        )
        entry_path = s._store._stash_entry_path(entry_id)
        entry_path.chmod(0o000)
        try:
            with patch("claude_swap.oauth.try_refresh_oauth_credentials") as post:
                errors = {
                    s.consume_backup_grant(
                        "1", "test@example.com", self._OLD
                    ).error
                    for _ in range(10)
                }
        finally:
            entry_path.chmod(0o600)

        assert not post.called, "the spent generation must never be POSTed"
        assert errors == {"stash-unreadable"}, (
            f"10 passes on a permanently unreadable row reported {errors}; "
            "'transient' routes the tick to 'could not freshen any candidate "
            "(network?)' and sends the operator to check a connection that "
            "is fine, on a condition only they can clear"
        )
        assert entry_id in s.list_unclaimed_credentials(), (
            "the row must survive: it is the sole copy of a generation the "
            "slot already consumed, and nothing distinguishes a lock that "
            "clears in a minute from one that never does"
        )

    def test_the_kind_carries_its_remedy_and_skips_the_doomed_fetch(self):
        """Two seams collapse an unknown kind back onto the generic path.

        ``ERROR_NOTES`` falls back to printing the bare kind, so the remedy
        the operator needs never renders; ``_DETERMINISTIC_REFRESH_ERRORS``
        decides whether to spend a guaranteed 401 per pass on a token already
        known to be expired. (The third seam, the tick's own message, has a
        behavioural test in ``tests/test_autoswitch.py``.)
        """
        from claude_swap.oauth import _DETERMINISTIC_REFRESH_ERRORS
        from claude_swap.switcher import ERROR_NOTES

        assert "cswap unclaimed" in ERROR_NOTES["stash-unreadable"]
        assert "stash-unreadable" in _DETERMINISTIC_REFRESH_ERRORS


class TestHealedStrikeUnblocksFetching:
    """Review finding 5: a fingerprint-healed strike must also unblock
    fetch eligibility, not just the display — the collector clears the
    stale strike when it observes the replacement credential."""

    def test_collector_clears_stale_strike_row(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        from claude_swap.usage_store import FetchRecord as StoreRecord
        sample_sequence_data["accounts"]["2"] = {
            "email": "b@example.com", "uuid": "u2",
            "organizationUuid": "", "organizationName": "",
        }
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        s._write_json(s.sequence_file, sample_sequence_data)
        dead = json.dumps({
            "claudeAiOauth": {"accessToken": "a", "refreshToken": "rt-dead",
                              "expiresAt": 1000}})
        identities = {"2": ("b@example.com", "")}
        store = s._usage_store
        claims = store.reserve(["2"], identities, respect_plans=False)
        store.record(
            {"2": StoreRecord(error="invalid_grant",
                              struck_fp=oauth.credential_fingerprint(dead))},
            identities, claims,
        )
        fresh = json.dumps({
            "claudeAiOauth": {"accessToken": "b", "refreshToken": "rt-new",
                              "expiresAt": 1000}})
        info = [(2, "b@example.com", "", "", False, fresh, "")]
        s._collect_usage_entries(info, fetch=set())
        # strike row cleared → account fetch-eligible again
        entry = store.entries(identities, [])["2"]
        assert entry.auth_dead_strikes == 0


class TestGateUltraReviewFixes:
    """Ultra-review hardening of the consume gate: exception containment
    after a consumed grant, consumed_fp on failure outcomes, per-slot
    consume serialization, stash adoption, empty-store CAS, STALE_MARKER
    precedence, and the unreadable-backup defer."""

    _OLD = json.dumps({
        "claudeAiOauth": {"accessToken": "sk-old", "refreshToken": "rt-old",
                          "expiresAt": 1000}})
    _NEW = json.dumps({
        "claudeAiOauth": {"accessToken": "sk-new", "refreshToken": "rt-new",
                          "expiresAt": 9999999999000}})

    def _switcher(self, sample_sequence_data):
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        s._write_json(s.sequence_file, sample_sequence_data)
        return s

    def test_a_cas_conflict_is_not_reported_as_a_failed_freshen(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """A racing writer's win leaves the slot FRESHENED, not broken.

        The transient demotion exists for the failed-persist case, where the
        slot still holds the generation whose grant was spent. A CAS conflict
        is the opposite: the winner already wrote a newer valid credential, so
        the slot is exactly what the caller wanted. Reporting it as an error
        makes ``_freshen_target`` skip a perfectly fresh candidate and the tick
        emit "could not freshen any candidate (network?)" — on every
        multi-surface race, which is the contention this gate exists for.
        """
        racer = json.dumps({
            "claudeAiOauth": {"accessToken": "sk-rac", "refreshToken": "rt-rac",
                              "expiresAt": 8888888888000}})
        s = self._switcher(sample_sequence_data)
        s._write_account_credentials("1", "test@example.com", self._OLD)

        def refresh_then_lose_the_race(credentials, **kw):
            s._store._write_account_credentials("1", "test@example.com", racer)
            return oauth.RefreshOutcome(self._NEW, None)

        with patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   side_effect=refresh_then_lose_the_race):
            out = s.consume_backup_grant("1", "test@example.com", self._OLD)

        assert s._read_account_credentials("1", "test@example.com") == racer, (
            "premise: the store holds the racer's newer lineage"
        )
        assert out.credentials == racer
        assert out.error is None, (
            "a freshened slot reported as a failure: the caller skips it"
        )

    def test_a_cas_conflict_stash_does_not_accumulate_forever(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """A CAS-conflict entry can never satisfy the adoption condition.

        _adopt_stashed_successor adopts when ``consumedFp`` equals the
        generation the slot currently stores. A CAS conflict is by definition
        the case where they differ — another writer replaced the lineage while
        our POST was in flight — and the store only moves forward, so it never
        returns to the generation we consumed. Every conflict therefore leaves
        a file that no path can consume or expire, and a busy multi-surface
        setup produces exactly these.

        The entry itself is worth keeping (it holds a real consumed successor
        and `--json` surfaces it as unclaimedCredentials for hand recovery);
        what must not happen is unbounded growth of entries indistinguishable
        from ones still pending adoption.
        """
        THIRD = json.dumps({
            "claudeAiOauth": {"accessToken": "sk-3rd", "refreshToken": "rt-3rd",
                              "expiresAt": 9999999999000}})
        s = self._switcher(sample_sequence_data)
        s._write_account_credentials("1", "test@example.com", self._OLD)

        # Both gate store reads ask the strict reader (absent must stay
        # distinguishable from unreadable). The pre-POST read sees the
        # generation we consume; a third party replaces the lineage while we
        # are in flight, so the post-POST CAS re-read sees THIRD.
        reads = iter([(self._OLD, False)])

        def read_ex(num, email):
            return next(reads, (THIRD, False))

        with patch.object(s, "_read_account_credentials", return_value=THIRD), \
             patch.object(s, "_read_account_credentials_ex",
                          side_effect=read_ex), \
             patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   return_value=oauth.RefreshOutcome(self._NEW, None)):
            out = s.consume_backup_grant("1", "test@example.com", self._OLD)

        # The gate adopts the newer store lineage, as designed.
        assert out.credentials == THIRD
        stashed = s.list_unclaimed_credentials()
        assert stashed, "the consumed successor must be preserved"

        # Now the defect: a later pass must be able to retire it. Adoption
        # runs against the store's CURRENT generation, which is THIRD.
        adopted = s._adopt_stashed_successor("1", "test@example.com", THIRD)
        assert adopted is None      # correctly not adopted (dead branch)
        assert not [
            e for e in s.list_unclaimed_credentials()
            if s._store._read_stash_manifest().get(e, {}).get("reason")
            == "consume-gate-cas-conflict"
        ], (
            "a CAS-conflict entry stays pending forever: it can never match "
            "the adoption condition, so every conflict leaks one file"
        )

    def test_no_store_read_happens_before_the_consume_lock(
        self, temp_home: Path, sample_sequence_data: dict, monkeypatch
    ):
        """Serialization is only real if nothing is read before the lock.

        A review claimed the gate bootstraps by reading the store before
        acquiring the per-slot consume lock, which would let two gates pick the
        same one-time-use grant. It does not — but that ordering is exactly the
        kind of thing a later refactor reintroduces without noticing, and the
        symptom (a burned generation, invalid_grant on the loser) surfaces far
        from the cause. Pin it.
        """
        s = self._switcher(sample_sequence_data)
        s._write_account_credentials("1", "test@example.com", self._OLD)

        order: list[str] = []
        real_lock = s._read_account_credentials

        def watched_read(*a, **k):
            order.append("read")
            return real_lock(*a, **k)

        from claude_swap.locking import FileLock as real_filelock

        class WatchedLock:
            def __init__(self, path, *a, **k):
                self._name = str(path)
                self._inner = real_filelock(path, *a, **k)
            def acquire(self, *a, **k):
                if ".consume-" in self._name:
                    order.append("consume-lock")
                return self._inner.acquire(*a, **k)
            def release(self):
                self._inner.release()
            def __enter__(self):
                return self._inner.__enter__()
            def __exit__(self, *a):
                return self._inner.__exit__(*a)

        monkeypatch.setattr("claude_swap.switcher.FileLock", WatchedLock)
        with patch.object(s, "_read_account_credentials", side_effect=watched_read), \
             patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   return_value=oauth.RefreshOutcome(self._NEW, None)):
            s.consume_backup_grant("1", "test@example.com", self._OLD)

        assert "consume-lock" in order, "the consume lock must be taken"
        assert order.index("consume-lock") == 0, (
            f"order was {order}: the store is read before the consume lock, so "
            "two gates can select the same one-time-use grant"
        )

    def test_contention_reports_its_own_kind_not_transient(
        self, temp_home: Path, sample_sequence_data: dict, monkeypatch
    ):
        """Losing the consume lock is local serialization, not a failure.

        The loser used to return "transient", which autoswitch renders as
        "could not freshen any candidate (network?)" — sending the user to
        check a connection that is fine, for a condition no network change can
        affect. On a machine where the collector and a manual `cswap switch`
        overlap this is routine, not an edge.
        """
        s = self._switcher(sample_sequence_data)
        s._write_account_credentials("1", "test@example.com", self._OLD)

        from claude_swap.locking import FileLock as real_filelock

        class ConsumeLockBusy:
            def __init__(self, path, *a, **k):
                self._busy = ".consume-" in str(path)
                self._inner = real_filelock(path, *a, **k)
            def acquire(self, *a, **k):
                return False if self._busy else self._inner.acquire(*a, **k)
            def release(self):
                if not self._busy:
                    self._inner.release()
            def __enter__(self):
                return self._inner.__enter__()
            def __exit__(self, *a):
                return self._inner.__exit__(*a)

        monkeypatch.setattr("claude_swap.switcher.FileLock", ConsumeLockBusy)
        out = s.consume_backup_grant("1", "test@example.com", self._OLD)

        assert out.error == "consume-busy", (
            f"got {out.error!r}: contention reads as a transient failure and "
            "surfaces as (network?)"
        )

    # -- exception containment (findings: window-3 persist raise) --------

    def test_persist_oserror_after_post_stashes_and_never_raises(
        self, temp_home: Path, sample_sequence_data: dict, monkeypatch
    ):
        """An OSError from the CAS persist (disk full) after the POST must
        neither raise out of the gate nor discard the consumed successor."""
        s = self._switcher(sample_sequence_data)
        s._write_account_credentials("1", "test@example.com", self._OLD)

        real_write = ClaudeAccountSwitcher._write_account_credentials
        state = {"post_done": False}

        def failing_write(self_s, num, email, creds):
            if state["post_done"]:
                raise OSError(28, "No space left on device")
            return real_write(self_s, num, email, creds)

        def mock_refresh(credentials, **kw):
            state["post_done"] = True
            return oauth.RefreshOutcome(self._NEW, None)

        monkeypatch.setattr(
            ClaudeAccountSwitcher, "_write_account_credentials", failing_write
        )
        with patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   side_effect=mock_refresh):
            out = s.consume_backup_grant("1", "test@example.com", self._OLD)

        # Never raises, never discards the successor — the two things this
        # test exists for. It does NOT report success: the persist failed, so
        # the slot still holds the consumed generation and activating it would
        # install a token that can never refresh.
        assert out.error == "transient"
        assert out.credentials == self._NEW      # survives in the outcome
        assert s.list_unclaimed_credentials(), "successor must be stashed"

    def test_persist_and_stash_both_failing_still_never_raises(
        self, temp_home: Path, sample_sequence_data: dict, monkeypatch
    ):
        """Worst case — persist AND stash writes fail (same-dir I/O error).

        No exception may reach the never-raises collect pass, AND the gate
        must not report success: the grant is spent, the store still holds
        the spent generation, and nothing was stashed. Reporting error=None
        made `_freshen_target` answer "ok" and the engine switched onto a
        slot whose credential can never refresh — the human-at-the-keyboard
        failure this gate exists to prevent. The successor still rides along
        so a caller that only needs a live token for THIS request works.
        """
        s = self._switcher(sample_sequence_data)
        s._write_account_credentials("1", "test@example.com", self._OLD)
        state = {"post_done": False}
        real_write = ClaudeAccountSwitcher._write_account_credentials

        def failing_write(self_s, num, email, creds):
            if state["post_done"]:
                raise OSError(28, "No space left on device")
            return real_write(self_s, num, email, creds)

        def failing_stash(creds, ctx):
            raise OSError(28, "No space left on device")

        def mock_refresh(credentials, **kw):
            state["post_done"] = True
            return oauth.RefreshOutcome(self._NEW, None)

        monkeypatch.setattr(
            ClaudeAccountSwitcher, "_write_account_credentials", failing_write
        )
        monkeypatch.setattr(
            s._store, "_write_unclaimed_credential", failing_stash
        )
        with patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   side_effect=mock_refresh):
            out = s.consume_backup_grant("1", "test@example.com", self._OLD)
        assert out.error == "transient", (
            "reported success on a spent grant with nothing stashed"
        )
        assert out.credentials == self._NEW

    # -- consumed_fp on failure outcomes ---------------------------------

    def test_failure_outcome_carries_consumed_fp_of_posted_bytes(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """invalid_grant after the gate substituted a fresher re-read must
        bind the strike to the POSTed bytes, not the caller's snapshot."""
        s = self._switcher(sample_sequence_data)
        fresher = json.dumps({
            "claudeAiOauth": {"accessToken": "sk-fresher",
                              "refreshToken": "rt-fresher",
                              "expiresAt": 2000}})
        s._write_account_credentials("1", "test@example.com", fresher)

        with patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   return_value=oauth.RefreshOutcome(None, "invalid_grant")):
            out = s.consume_backup_grant("1", "test@example.com", self._OLD)

        assert out.error == "invalid_grant"
        assert out.consumed_fp == oauth.credential_fingerprint(fresher)
        assert out.consumed_fp != oauth.credential_fingerprint(self._OLD)

    # -- per-slot consume serialization (TOCTOU) -------------------------

    def test_concurrent_gates_consume_only_one_grant(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """Two gates racing on one slot must POST exactly once: the loser
        serializes behind the consume lock and adopts the winner's fresh
        successor instead of re-consuming."""
        import threading
        s = self._switcher(sample_sequence_data)
        s._write_account_credentials("1", "test@example.com", self._OLD)
        posted = []
        in_post = threading.Event()
        release_post = threading.Event()

        def slow_refresh(credentials, **kw):
            posted.append(json.loads(credentials)["claudeAiOauth"]["refreshToken"])
            in_post.set()
            release_post.wait(timeout=5)
            return oauth.RefreshOutcome(self._NEW, None)

        results = {}

        def gate(tag):
            # each thread gets its own switcher (cross-process shape)
            s2 = ClaudeAccountSwitcher()
            results[tag] = s2.consume_backup_grant(
                "1", "test@example.com", self._OLD
            )

        with patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   side_effect=slow_refresh):
            t1 = threading.Thread(target=gate, args=("a",))
            t1.start()
            assert in_post.wait(timeout=5)
            t2 = threading.Thread(target=gate, args=("b",))
            t2.start()
            import time as _time
            _time.sleep(0.3)          # b must be parked on the consume lock
            release_post.set()
            t1.join(timeout=10)
            t2.join(timeout=10)

        assert posted == ["rt-old"], "one grant consumed exactly once"
        for tag in ("a", "b"):
            assert results[tag].error is None
            assert results[tag].credentials == self._NEW

    # -- stash adoption ---------------------------------------------------

    def test_next_gate_pass_adopts_stashed_successor(
        self, temp_home: Path, sample_sequence_data: dict, monkeypatch
    ):
        """A successor stranded in the stash by a failed persist is adopted
        by the next gate pass (the pending persist completes) without
        consuming another grant."""
        s = self._switcher(sample_sequence_data)
        s._write_account_credentials("1", "test@example.com", self._OLD)
        # simulate the strand: stash holds the successor of _OLD
        s._store._write_unclaimed_credential(
            self._NEW,
            {"reason": "consume-gate-persist-lock-failed",
             "configSlot": "1",
             "consumedFp": oauth.credential_fingerprint(self._OLD),
             "fingerprint": oauth.credential_fingerprint(self._NEW)},
        )

        with patch("claude_swap.oauth.try_refresh_oauth_credentials") as post:
            out = s.consume_backup_grant("1", "test@example.com", self._OLD)

        post.assert_not_called()               # no second grant consumed
        assert out.error is None
        assert out.credentials == self._NEW
        assert (
            s._read_account_credentials("1", "test@example.com") == self._NEW
        )
        assert not s.list_unclaimed_credentials(), "stash entry consumed"

    # -- empty-store CAS (remove-account race) ---------------------------

    def test_slot_removed_mid_post_stashes_instead_of_resurrecting(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """A slot emptied while the POST was in flight must not get its
        credential re-created; the successor parks in the stash."""
        s = self._switcher(sample_sequence_data)
        s._write_account_credentials("1", "test@example.com", self._OLD)

        def refresh_and_remove(credentials, **kw):
            s._store._delete_account_credentials("1", "test@example.com")
            return oauth.RefreshOutcome(self._NEW, None)

        with patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   side_effect=refresh_and_remove):
            out = s.consume_backup_grant("1", "test@example.com", self._OLD)

        assert out.error is None               # grant did rotate
        assert s._read_account_credentials("1", "test@example.com") == "", (
            "removed slot must stay empty"
        )
        assert s.list_unclaimed_credentials(), "successor parked in stash"

    # -- STALE_MARKER precedence ------------------------------------------

    def test_stale_marked_profile_never_supersedes_backup(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """A profile marked stale (backup changed under a live session —
        e.g. a deliberate re-add) must not clobber the backup even when its
        expiresAt is newer."""
        from claude_swap.session import STALE_MARKER, session_dir_for
        s = self._switcher(sample_sequence_data)
        reimported = json.dumps({
            "claudeAiOauth": {"accessToken": "sk-readd",
                              "refreshToken": "rt-readd",
                              "expiresAt": 1000}})
        s._write_account_credentials("1", "test@example.com", reimported)
        stale_profile = json.dumps({
            "claudeAiOauth": {"accessToken": "sk-stale",
                              "refreshToken": "rt-stale",
                              "expiresAt": 999999}})
        sdir = session_dir_for(s.backup_dir, "1", "test@example.com")
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / ".credentials.json").write_text(stale_profile)
        (sdir / STALE_MARKER).touch()
        posted = {}

        def mock_refresh(credentials, **kw):
            posted["creds"] = credentials
            return oauth.RefreshOutcome(self._NEW, None)

        with patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   side_effect=mock_refresh), \
             patch.object(s, "_live_session_pids", return_value=[]):
            s.consume_backup_grant("1", "test@example.com", reimported)

        assert posted["creds"] == reimported, (
            "the re-added backup, not the presumed-stale profile"
        )

    # -- the consume lock must not leak (M-8) ----------------------------

    def test_the_consume_lock_is_released_even_when_the_post_raises(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """A leaked consume lock wedges the slot: every later gate pass
        returns `consume-busy` for the rest of the process, so the collector
        silently stops refreshing that account.

        Deliberately the RAISING path, not the happy one. A happy-path
        version of this test passes with the `finally` removed — the
        `consume_lock` local goes out of scope, refcounting closes the fd,
        and flock releases on close. The raised exception keeps the frame
        (and the lock) alive, which is both the case that actually leaks and
        the only one that can see the guard.
        """
        s = self._switcher(sample_sequence_data)
        s._write_account_credentials("1", "test@example.com", self._OLD)
        with patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError):
                s.consume_backup_grant("1", "test@example.com", self._OLD)

        with patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   return_value=oauth.RefreshOutcome(self._NEW, None)):
            out = s.consume_backup_grant("1", "test@example.com", self._OLD)
        assert out.error != "consume-busy"

    # -- session-profile precedence: the other three conjuncts -----------

    def test_a_foreign_profile_never_supersedes_the_backup(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """#117 credential poisoning through the gate's resync.

        The precedence block writes the session profile INTO the slot
        (`_write_account_credentials`) before POSTing it. Only the
        STALE_MARKER conjunct was covered; drop the identity check and a
        profile whose own .claude.json names SOMEONE ELSE is written into
        this slot and its grant consumed — so the slot ends up holding a
        foreign lineage's successor.

        Measured with the guard off:
            POSTed rt       = rt-foreign   (baseline: rt-bk)
            backup rt after = rt-n         (the foreign lineage's successor)
        """
        from claude_swap.session import session_dir_for
        s = self._switcher(sample_sequence_data)
        backup = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-bk", "refreshToken": "rt-bk",
            "expiresAt": 1000}})
        s._write_account_credentials("1", "test@example.com", backup)
        # A profile on a NEWER generation, but logged in as another account.
        foreign = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-foreign", "refreshToken": "rt-foreign",
            "expiresAt": 999999}})
        sdir = session_dir_for(s.backup_dir, "1", "test@example.com")
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / ".credentials.json").write_text(foreign)
        s._write_json(sdir / ".claude.json", {"oauthAccount": {
            "emailAddress": "SOMEONE-ELSE@example.com",
            "accountUuid": "other-uuid",
            "organizationUuid": "", "organizationName": "",
        }})
        posted = {}

        def mock_refresh(credentials, **kw):
            posted["creds"] = credentials
            return oauth.RefreshOutcome(self._NEW, None)

        with patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   side_effect=mock_refresh), \
             patch.object(s, "_live_session_pids", return_value=[]):
            s.consume_backup_grant("1", "test@example.com", backup)

        assert posted["creds"] == backup, (
            "the gate POSTed a profile logged in as another account"
        )
        assert "rt-foreign" not in s._read_account_credentials(
            "1", "test@example.com"
        ), "a foreign lineage was written into the slot"

    def test_an_older_profile_never_supersedes_the_backup(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """The precedence exists for a profile holding the NEWER generation.

        An older one (lower expiresAt) is the spent predecessor: adopting it
        POSTs a generation the backup already superseded, which is the
        invalid_grant this gate exists to avoid.

        Measured with `prof_exp > cur_exp` off:
            POSTed rt = rt-pf-SPENT   (baseline: rt-bk)
        """
        from claude_swap.session import session_dir_for
        s = self._switcher(sample_sequence_data)
        backup = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-bk", "refreshToken": "rt-bk",
            "expiresAt": 5000}})
        s._write_account_credentials("1", "test@example.com", backup)
        older = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-pf", "refreshToken": "rt-pf-SPENT",
            "expiresAt": 1000}})
        sdir = session_dir_for(s.backup_dir, "1", "test@example.com")
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / ".credentials.json").write_text(older)
        posted = {}

        def mock_refresh(credentials, **kw):
            posted["creds"] = credentials
            return oauth.RefreshOutcome(self._NEW, None)

        with patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   side_effect=mock_refresh), \
             patch.object(s, "_live_session_pids", return_value=[]):
            s.consume_backup_grant("1", "test@example.com", backup)

        assert posted["creds"] == backup, (
            "the gate POSTed the profile's older, already-superseded "
            "generation instead of the backup"
        )

    # -- unreadable backup defers ----------------------------------------

    def test_unreadable_backup_defers_instead_of_posting_snapshot(
        self, temp_home: Path, sample_sequence_data: dict, monkeypatch
    ):
        """When the backup read is unreadable (keychain locked) — not
        absent — the gate must defer, never POST the caller's snapshot."""
        s = self._switcher(sample_sequence_data)
        monkeypatch.setattr(
            ClaudeAccountSwitcher, "_read_account_credentials_ex",
            lambda self_s, num, email: ("", True),
        )
        with patch("claude_swap.oauth.try_refresh_oauth_credentials") as post:
            out = s.consume_backup_grant("1", "test@example.com", self._OLD)
        post.assert_not_called()
        assert out.error == "transient"

    # -- fresh re-read short-circuit -------------------------------------

    def test_fresh_rotated_reread_adopted_without_second_consume(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """The store already holds a FRESH different generation (a racing
        gate rotated it): adopt it instead of consuming another grant."""
        s = self._switcher(sample_sequence_data)
        s._write_account_credentials("1", "test@example.com", self._NEW)
        with patch("claude_swap.oauth.try_refresh_oauth_credentials") as post:
            out = s.consume_backup_grant("1", "test@example.com", self._OLD)
        post.assert_not_called()
        assert out.error is None
        assert out.credentials == self._NEW

    # -- a failed retire must not unwind a completed adoption -------------

    @pytest.mark.parametrize("retire_fails", [False, True])
    def test_a_failed_retire_does_not_unwind_a_completed_adoption(
        self, temp_home: Path, sample_sequence_data: dict, retire_fails: bool
    ):
        """The retire is housekeeping; the adoption is the credential.

        ``_adopt_stashed_successor`` writes the backup and only THEN retires
        the stash row. The row's bytes are already unlinked at that point, so
        a raise out of the retire escapes a slot that has ALREADY been
        advanced: the caller is told the adoption failed while the store says
        it succeeded, and re-POSTs a generation this pass just consumed.

        Any ``OSError`` from the manifest's ``atomic_write_json`` triggers it
        (full disk, read-only mount) — no lock contention required.

        Parametrized so the succeeding-retire CONTROL runs beside the probe:
        if the control also lost the adoption, the probe would prove nothing.
        """
        s = self._switcher(sample_sequence_data)
        s._write_account_credentials("1", "test@example.com", self._OLD)
        s._store._write_unclaimed_credential(
            self._NEW,
            {"reason": "consume-gate-persist-lock-failed",
             "configSlot": "1",
             "consumedFp": oauth.credential_fingerprint(self._OLD),
             "fingerprint": oauth.credential_fingerprint(self._NEW)},
        )

        real_retire = s._store._remove_unclaimed_credential

        def retire(entry_id):
            if retire_fails:
                raise OSError(28, "No space left on device")
            real_retire(entry_id)

        with patch.object(s._store, "_remove_unclaimed_credential", retire):
            adopted = s._adopt_stashed_successor(
                "1", "test@example.com", self._OLD
            )

        assert s._read_account_credentials("1", "test@example.com") == self._NEW, (
            "premise: the adoption write lands in both arms"
        )
        assert adopted == self._NEW, (
            "the adoption completed but its credentials were not returned: "
            "the caller re-POSTs a generation this pass already consumed"
        )

    @pytest.mark.parametrize("dead_row_is_byteless", [False, True])
    def test_a_failed_housekeeping_retire_does_not_abort_the_scan(
        self, temp_home: Path, sample_sequence_data: dict,
        dead_row_is_byteless: bool
    ):
        """Housekeeping retires run mid-scan; a raise skips later rows.

        The scan retires two kinds of dead row as it iterates — CAS-conflict
        rows and rows whose bytes are gone. Both run BEFORE the adoptable row
        may be reached, so a raise there aborts the loop and the successor
        sitting right behind it is never adopted.

        Both dead-row shapes are covered; each case is its own control, since
        the same manifest with a working retire must adopt row B.
        """
        s = self._switcher(sample_sequence_data)
        s._write_account_credentials("1", "test@example.com", self._OLD)

        # Row A: dead, retired as housekeeping, written FIRST so the scan
        # reaches it before the adoptable row.
        if dead_row_is_byteless:
            # matching generation, bytes unlinked -> the `not creds` retire
            dead_meta = {"reason": "consume-gate-persist-failed",
                         "consumedFp": oauth.credential_fingerprint(self._OLD)}
        else:
            # non-matching generation -> the CAS-conflict retire
            dead_meta = {"reason": "consume-gate-cas-conflict",
                         "consumedFp": "sha256:gone"}
        dead_id = s._store._write_unclaimed_credential(
            self._NEW, {"configSlot": "1", **dead_meta})
        if dead_row_is_byteless:
            s._store._stash_entry_path(dead_id).unlink()

        # Row B: the adoptable successor, behind A in the manifest.
        live_id = s._store._write_unclaimed_credential(
            self._NEW,
            {"reason": "consume-gate-persist-lock-failed",
             "configSlot": "1",
             "consumedFp": oauth.credential_fingerprint(self._OLD),
             "fingerprint": oauth.credential_fingerprint(self._NEW)},
        )
        assert list(s._store._read_stash_manifest()) == [dead_id, live_id], (
            "premise: the dead row is scanned before the adoptable one"
        )

        real_retire = s._store._remove_unclaimed_credential

        def retire(entry_id):
            if entry_id == dead_id:
                raise OSError(28, "No space left on device")
            real_retire(entry_id)

        with patch.object(s._store, "_remove_unclaimed_credential", retire):
            adopted = s._adopt_stashed_successor(
                "1", "test@example.com", self._OLD
            )

        assert adopted == self._NEW, (
            "a failed housekeeping retire aborted the scan before the "
            "adoptable row behind it was reached"
        )
        assert s._read_account_credentials("1", "test@example.com") == self._NEW


    @pytest.mark.parametrize("bytes_survive", [True, False])
    def test_a_byteless_non_matching_row_is_retired(
        self, temp_home: Path, sample_sequence_data: dict, bytes_survive: bool
    ):
        """A row whose bytes are gone can never be adopted by anyone.

        This is the state a failed retire leaves behind:
        ``_remove_unclaimed_credential`` unlinks the bytes BEFORE rewriting
        the manifest, so an ``OSError`` from that rewrite orphans a row whose
        credential no longer exists. The adoption also moved the slot off the
        generation the row keys against, so it never matches ``consumedFp``
        again and the only non-match retire (CAS-conflict) does not apply —
        it is immortal junk in ``--json``'s unclaimedCredentials.

        The control is the whole point of drawing the line at the BYTES: a
        non-matching row that still HAS its bytes holds a real, superseded
        refresh token, and retiring that would destroy a credential an
        operator may still want. Only the byte-less ones are free to drop.
        """
        s = self._switcher(sample_sequence_data)
        s._write_account_credentials("1", "test@example.com", self._NEW)
        entry_id = s._store._write_unclaimed_credential(
            self._NEW,
            {"reason": "consume-gate-persist-lock-failed",
             "configSlot": "1",
             # keys against a generation the slot has already moved past
             "consumedFp": oauth.credential_fingerprint(self._OLD),
             "fingerprint": oauth.credential_fingerprint(self._NEW)},
        )
        if not bytes_survive:
            s._store._stash_entry_path(entry_id).unlink()

        adopted = s._adopt_stashed_successor("1", "test@example.com", self._NEW)

        assert adopted is None, "premise: a non-matching row is never adopted"
        listed = entry_id in s._store._read_stash_manifest()
        if bytes_survive:
            assert listed, (
                "a superseded row that still holds its bytes is a real "
                "credential; dropping it is the operator's call (--purge)"
            )
        else:
            assert not listed, (
                "a row whose bytes are gone can never be adopted by any "
                "pass, yet nothing retires it: permanent junk in --json"
            )

    @pytest.mark.parametrize("store_write_lands", [True, False])
    def test_a_failed_session_invalidation_does_not_discard_the_adoption(
        self, temp_home: Path, sample_sequence_data: dict,
        store_write_lands: bool
    ):
        """``_write_account_credentials`` is two steps, and the second can raise.

        It writes the store (which ADVANCES the slot) and only then runs
        ``_post_backup_write`` to invalidate the slot's session profile. The
        live-session arm of that already swallows ``OSError``; the other arm
        unlinks profile files and does not, so an EACCES/EIO on the session
        dir raises out of an adoption whose credential is already stored —
        the same shape as a failed retire, and the caller likewise re-POSTs a
        spent generation.

        The control is what keeps this from swallowing real failures: when the
        STORE write is what failed, nothing was advanced and the exception
        must still propagate, because returning credentials the store does not
        hold would be a lie.
        """
        s = self._switcher(sample_sequence_data)
        s._write_account_credentials("1", "test@example.com", self._OLD)
        s._store._write_unclaimed_credential(
            self._NEW,
            {"reason": "consume-gate-persist-lock-failed",
             "configSlot": "1",
             "consumedFp": oauth.credential_fingerprint(self._OLD),
             "fingerprint": oauth.credential_fingerprint(self._NEW)},
        )

        def boom(*a, **kw):
            raise OSError(13, "Permission denied")

        if store_write_lands:
            # only the post-write session invalidation fails
            ctx = patch.object(type(s), "_post_backup_write", boom)
        else:
            # the store write itself fails: the slot never advances
            ctx = patch.object(type(s._store), "_write_account_credentials", boom)

        raised = None
        with ctx:
            try:
                adopted = s._adopt_stashed_successor(
                    "1", "test@example.com", self._OLD
                )
            except Exception as e:
                adopted, raised = None, e

        stored = s._read_account_credentials("1", "test@example.com")
        if store_write_lands:
            assert stored == self._NEW, "premise: the store took the write"
            assert raised is None and adopted == self._NEW, (
                "a completed adoption was discarded because the session "
                "profile could not be invalidated"
            )
        else:
            assert stored == self._OLD, "premise: the store rejected the write"
            assert isinstance(raised, OSError), (
                "the store never advanced; claiming an adoption would "
                "return credentials the slot does not hold"
            )

    @pytest.mark.skipif(
        sys.platform == "win32" or os.geteuid() == 0,
        reason="needs POSIX permission semantics (non-root)",
    )
    @pytest.mark.parametrize(
        "denied", [None, "session_dir", "session_dir_and_parent"]
    )
    def test_a_denied_session_dir_still_leaves_the_stale_marker(
        self, temp_home: Path, sample_sequence_data: dict, denied: str | None,
        caplog,
    ):
        """C-1: the fallback must not write into the directory whose EACCES
        caused the raise.

        `_write_account_credentials` is two steps: the store write ADVANCES
        the slot, then `_post_backup_write` invalidates the slot's session
        profile and can fail on exactly the faults the wrapper's docstring
        names — EACCES on the session dir, a read-only mount. A raise escaping
        the wrapper is read by every caller as "the persist failed" for a slot
        that HOLDS the new credential, so the write is contained and the
        STALE_MARKER is what forces the re-bootstrap instead.

        But the marker was a CHILD of the session dir, so the very fault that
        denied the unlink also denied the marker's create, and
        `mark_session_stale` swallows the OSError. `_is_session_valid` is a
        LOCAL check (its own docstring), so a revoked-but-unexpired token
        still passes it: without the marker `setup_session` returns early and
        launches claude on the spent generation, with nothing recording it.

        A REAL kernel fault (`chmod 0500`), not an injected raise — the
        coupling between the failing unlink and the fallback's write target
        IS the defect, and an injected raise cannot see it.

        `denied=None` is the CONTROL: on a healthy dir the invalidation really
        happens (old `.credentials.json` gone) and no marker is needed.

        `session_dir_and_parent` is the CEILING of any marker location: the
        whole `<backup>/sessions/` root is read-only, so the sibling cannot
        land either (that is the docstring's "read-only mount"). No path fixes
        the launch there, so what is required is that it is not SILENT — an
        ERROR naming the account, never a warning implying the fallback worked.
        """
        import logging

        from claude_swap.session import is_session_stale

        s = self._switcher(sample_sequence_data)
        sess = s._session_dir("1", "test@example.com")
        sess.mkdir(parents=True, exist_ok=True)
        (sess / ".credentials.json").write_text(self._OLD)

        chmodded = []
        if denied in ("session_dir", "session_dir_and_parent"):
            chmodded.append(sess)
        if denied == "session_dir_and_parent":
            chmodded.append(sess.parent)
        caplog.clear()
        try:
            for d in chmodded:
                d.chmod(0o500)
            with caplog.at_level(logging.WARNING, logger="claude-swap"):
                s._write_account_credentials("1", "test@example.com", self._NEW)
        finally:
            for d in reversed(chmodded):
                d.chmod(0o700)

        assert s._read_account_credentials("1", "test@example.com") == self._NEW, (
            "premise: the store write ADVANCED the slot"
        )
        if denied is None:
            assert not (sess / ".credentials.json").exists(), (
                "CONTROL: the invalidation really ran on a healthy dir"
            )
            assert not is_session_stale(sess), (
                "CONTROL: nothing failed, so nothing to mark"
            )
            return

        assert (sess / ".credentials.json").exists(), (
            f"premise ({denied}): the unlink was denied, so the profile "
            "still holds the superseded credential"
        )
        if denied == "session_dir":
            assert is_session_stale(sess), (
                "DEFECT: the invalidation was denied and NO stale marker "
                "landed — the marker's own write target was the directory "
                "that denied it. The profile's token is unexpired, so the "
                "local reuse check passes and `cswap run` launches claude on "
                "the spent generation, silently"
            )
        else:
            # Ceiling: nothing under a read-only `sessions/` can be written.
            assert not is_session_stale(sess), (
                "premise: the whole sessions root is denied, so no marker "
                "location under it can land"
            )
            assert any(
                r.levelno >= logging.ERROR and "1" in r.getMessage()
                for r in caplog.records
            ), (
                "DEFECT: the profile keeps serving the superseded generation "
                "and nothing says so above WARNING — the fallback did not "
                "work, so a warning claiming it did is worse than silence"
            )

    def test_the_suites_real_store_guard_is_not_absorbed_by_the_wrapper(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """I-1: the containment must be `except OSError`, not `except Exception`.

        `tests/conftest.py`'s `RealStoreWriteBlocked` is deliberately NOT an
        `OSError` subclass (its own docstring) precisely so that no
        `except OSError` in this codebase can hide a real-store refusal. An
        `except Exception` on the credential-persist path catches it and
        disarms the guard for every write that routes through here.

        The wrapper's whole documented fault list is EACCES on the session dir
        and a read-only mount — both `OSError`. Nothing else belongs in it.
        """
        from tests import conftest

        s = self._switcher(sample_sequence_data)

        def blocked(*a, **kw):
            raise conftest.RealStoreWriteBlocked("refused: the REAL store")

        with patch.object(type(s), "_post_backup_write", blocked):
            with pytest.raises(conftest.RealStoreWriteBlocked):
                s._write_account_credentials("1", "test@example.com", self._NEW)

    @pytest.mark.parametrize("row_a_readable", [True, False])
    def test_an_unreadable_non_matching_row_does_not_abort_the_scan(
        self, temp_home: Path, sample_sequence_data: dict, row_a_readable: bool
    ):
        """Classifying a dead row must not raise on a row it cannot read.

        The byte-less retire has to decide whether a non-matching row still
        holds a credential. ``Path.exists()`` is the obvious way and the wrong
        one: it only swallows ENOENT-shaped errors, so an EACCES/EIO on the
        entry file RAISES out of the scan — aborting it before an adoptable
        sibling behind it is reached, which is exactly the failure this round
        exists to remove.

        ``_read_unclaimed_credential`` is the reader that already draws this
        distinction (absent/corrupt vs merely unreadable) and never raises for
        either. An unreadable row must also SURVIVE: its bytes are still there
        and may hold a real superseded token.
        """
        s = self._switcher(sample_sequence_data)
        s._write_account_credentials("1", "test@example.com", self._OLD)

        # Row A: non-matching (hits the dead-row classifier), bytes present.
        row_a = s._store._write_unclaimed_credential(
            self._NEW,
            {"reason": "consume-gate-persist-failed", "configSlot": "1",
             "consumedFp": "sha256:some-other-generation"},
        )
        # Row B: the adoptable successor, behind A.
        row_b = s._store._write_unclaimed_credential(
            self._NEW,
            {"reason": "consume-gate-persist-lock-failed",
             "configSlot": "1",
             "consumedFp": oauth.credential_fingerprint(self._OLD),
             "fingerprint": oauth.credential_fingerprint(self._NEW)},
        )
        assert list(s._store._read_stash_manifest()) == [row_a, row_b], (
            "premise: the dead row is classified before the adoptable one"
        )

        path_a = s._store._stash_entry_path(row_a)
        real_stat, real_read = Path.stat, Path.read_text

        def deny(self, *a, **kw):
            if not row_a_readable and self.name == path_a.name:
                raise PermissionError(13, "Permission denied")
            return real_stat(self, *a, **kw)

        def deny_read(self, *a, **kw):
            if not row_a_readable and self.name == path_a.name:
                raise PermissionError(13, "Permission denied")
            return real_read(self, *a, **kw)

        with patch.object(Path, "stat", deny), \
             patch.object(Path, "read_text", deny_read):
            adopted = s._adopt_stashed_successor(
                "1", "test@example.com", self._OLD
            )

        assert adopted == self._NEW, (
            "an unreadable dead row aborted the scan before the adoptable "
            "row behind it was reached"
        )
        assert row_a in s._store._read_stash_manifest(), (
            "a row whose bytes are merely unreadable still holds a real "
            "credential; only a byte-less one is free to retire"
        )

class TestActiveSlotStrikeParity:
    """Ultra-review: the active slot has TWO stored sources (live + backup).
    Strike heal/quarantine verdicts must consider both, and the active
    consume path honors the same M4 parity guard as the gate."""

    def test_active_strike_bound_to_backup_not_healed_by_live_fp(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, monkeypatch
    ):
        """A strike bound to the backup lineage must quarantine even though
        the LIVE credential's fingerprint differs (the strike/heal/re-POST
        loop the review demonstrated)."""
        from claude_swap.usage_store import FetchRecord as FR
        sample_sequence_data["accounts"]["2"]["email"] = "b@example.com"
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        s._write_json(s.sequence_file, sample_sequence_data)
        dead_backup = json.dumps({
            "claudeAiOauth": {"accessToken": "sk-dead",
                              "refreshToken": "rt-dead", "expiresAt": 1000}})
        live = json.dumps({
            "claudeAiOauth": {"accessToken": "sk-live",
                              "refreshToken": "rt-live", "expiresAt": 2000}})
        s._write_account_credentials("2", "b@example.com", dead_backup)
        identities = {"2": ("b@example.com", "")}
        s._usage_store.record(
            {"2": FR(error="invalid_grant",
                     struck_fp=oauth.credential_fingerprint(dead_backup))},
            identities,
        )
        # active slot: the stored source is the LIVE credential (different
        # lineage from the backup the strike is bound to)
        entry = s._usage_store.entries(identities, [])["2"]
        assert s._entry_token_dead(entry, "2", "b@example.com", live, True), (
            "backup-bound strike must hold while the backup still stores "
            "the condemned generation"
        )

    def test_idle_slot_keeps_live_fp_heal_semantics(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict
    ):
        """Idle slots (info[5] = backup) keep the original heal rule: a
        replaced stored credential heals the strike."""
        from claude_swap.usage_store import FetchRecord as FR
        sample_sequence_data["accounts"]["2"]["email"] = "b@example.com"
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        s._write_json(s.sequence_file, sample_sequence_data)
        replaced = json.dumps({
            "claudeAiOauth": {"accessToken": "sk-re",
                              "refreshToken": "rt-re", "expiresAt": 2000}})
        s._write_account_credentials("2", "b@example.com", replaced)
        identities = {"2": ("b@example.com", "")}
        s._usage_store.record(
            {"2": FR(error="invalid_grant", struck_fp="sha256:deadbeef")},
            identities,
        )
        entry = s._usage_store.entries(identities, [])["2"]
        assert not s._entry_token_dead(
            entry, "2", "b@example.com", replaced, False
        )

    def test_active_path_refuses_consume_under_securestorage_env(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, monkeypatch
    ):
        """M4 parity on the ACTIVE path: with the env var set, an expired
        active credential is not consumed; the distinct kind surfaces."""
        sample_sequence_data["accounts"]["2"]["email"] = "b@example.com"
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        s._write_json(s.sequence_file, sample_sequence_data)
        expired = json.dumps({
            "claudeAiOauth": {"accessToken": "sk-exp",
                              "refreshToken": "rt-exp", "expiresAt": 1000}})
        s._write_account_credentials("2", "b@example.com", expired)
        monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", "/tmp/redir")
        with patch("claude_swap.oauth.try_refresh_oauth_credentials") as post:
            rec = s._fetch_active_usage("2", "b@example.com", expired)
        post.assert_not_called()
        assert rec.error == "store-unmirrored"

    def test_degraded_read_never_feeds_resync_write(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, monkeypatch
    ):
        """M1 extension: a degraded active read (keychain fallback) must not
        drive _resync_rotated_backup's write — the plaintext bytes may be
        the consumed predecessor."""
        from claude_swap.oauth import UsageOutcome
        sample_sequence_data["accounts"]["2"]["email"] = "b@example.com"
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        s._write_json(s.sequence_file, sample_sequence_data)
        fresh = json.dumps({
            "claudeAiOauth": {"accessToken": "sk-a", "refreshToken": "rt-a",
                              "expiresAt": 9999999999000}})
        s._record_active_verdict(ActiveCredentials("", False, True))
        resync = MagicMock()
        monkeypatch.setattr(s, "_resync_rotated_backup", resync)
        with patch(
            "claude_swap.oauth.try_fetch_usage_for_account",
            return_value=UsageOutcome({"five_hour": {"utilization": 10}}),
        ):
            s._fetch_active_usage("2", "b@example.com", fresh)
        resync.assert_not_called()


class TestUltraReviewCoverageGaps:
    """Ultra-review test-coverage findings: demotion re-read branch,
    degraded+force_refresh, active-path struck_fp stamping, add-path
    session-shell guards, gate-writes-live regression guard."""

    _EXPIRED = json.dumps({
        "claudeAiOauth": {"accessToken": "sk-active",
                          "refreshToken": "rt-orig", "expiresAt": 1000}})

    def _switcher(self, sample_sequence_data):
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        s._write_json(s.sequence_file, sample_sequence_data)
        return s

    # -- demotion re-read branch (finding: entirely untested) -------------

    def test_demotion_downgrades_invalid_grant_when_source_moved(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict
    ):
        """A lineage that MOVED mid-POST means we consumed a superseded
        copy — evidence about our bytes, not the slot: invalid_grant
        demotes to transient refresh-failed."""
        s = self._switcher(sample_sequence_data)
        moved = json.dumps({
            "claudeAiOauth": {"accessToken": "sk-moved",
                              "refreshToken": "rt-moved",
                              "expiresAt": 5000}})
        reads = {"n": 0}

        def read_backup(num, email):
            reads["n"] += 1
            # first read (recovery input): the expired copy;
            # demotion re-read: the store moved to a different lineage
            return self._EXPIRED if reads["n"] <= 1 else moved

        with patch.object(s, "_read_credentials",
                          return_value=self._EXPIRED), \
             patch.object(s, "_read_account_credentials",
                          side_effect=read_backup), \
             patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   return_value=oauth.RefreshOutcome(None, "invalid_grant")), \
             patch("claude_swap.oauth.try_fetch_usage_for_account"):
            rec = s._fetch_active_usage("1", "test@example.com", self._EXPIRED)

        assert rec.error == "refresh-failed"     # demoted, no strike
        assert rec.struck_fp is None

    def test_demotion_reread_error_still_strikes(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict
    ):
        """The except-Exception fallback (re-read failed → moved=False)
        keeps the permanent verdict: a dead lineage must not escape
        quarantine because a local read glitched."""
        s = self._switcher(sample_sequence_data)
        reads = {"n": 0}

        def read_backup(num, email):
            reads["n"] += 1
            if reads["n"] <= 1:
                return self._EXPIRED
            raise OSError("transient read failure")

        with patch.object(s, "_read_credentials",
                          return_value=self._EXPIRED), \
             patch.object(s, "_read_account_credentials",
                          side_effect=read_backup), \
             patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   return_value=oauth.RefreshOutcome(None, "invalid_grant")), \
             patch("claude_swap.oauth.try_fetch_usage_for_account"):
            rec = s._fetch_active_usage("1", "test@example.com", self._EXPIRED)

        assert rec.error == "invalid_grant"

    # -- active-path struck_fp stamping (finding: never asserted) ---------

    def test_active_invalid_grant_binds_strike_to_posted_input(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict
    ):
        s = self._switcher(sample_sequence_data)
        with patch.object(s, "_read_credentials",
                          return_value=self._EXPIRED), \
             patch.object(s, "_read_account_credentials",
                          return_value=self._EXPIRED), \
             patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   return_value=oauth.RefreshOutcome(None, "invalid_grant")), \
             patch("claude_swap.oauth.try_fetch_usage_for_account"):
            rec = s._fetch_active_usage("1", "test@example.com", self._EXPIRED)

        assert rec.error == "invalid_grant"
        assert rec.struck_fp == oauth.credential_fingerprint(self._EXPIRED)

    # -- degraded + force_refresh (finding: only expired arm tested) ------

    def test_degraded_plus_server_401_defers_with_the_401_record(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict
    ):
        """Locally-valid token, server 401s, degraded read: no consume, and
        the 401 ERROR record (not the keychain sentinel) reaches the store
        so backoff paces retries."""
        from claude_swap.oauth import UsageOutcome
        s = self._switcher(sample_sequence_data)
        fresh = json.dumps({
            "claudeAiOauth": {"accessToken": "sk-f", "refreshToken": "rt-f",
                              "expiresAt": 9999999999000}})
        s._record_active_verdict(ActiveCredentials("", False, True))
        with patch(
            "claude_swap.oauth.try_fetch_usage_for_account",
            return_value=UsageOutcome(None, error="http-401"),
        ), patch(
            "claude_swap.oauth.try_refresh_oauth_credentials"
        ) as post:
            rec = s._fetch_active_usage("1", "test@example.com", fresh)
        post.assert_not_called()
        assert rec.error == "http-401"
        assert rec.sentinel is None

    # -- add-path session-shell guards (finding: unexercised) -------------

    def test_add_account_refuses_inside_session_shell(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, monkeypatch
    ):
        s = self._switcher(sample_sequence_data)
        inside = s.backup_dir / "sessions" / "1-test-example-com"
        inside.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(inside))
        with pytest.raises(SwitchError):
            s.add_account()

    def test_add_token_refuses_inside_session_shell(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, monkeypatch
    ):
        s = self._switcher(sample_sequence_data)
        inside = s.backup_dir / "sessions" / "1-test-example-com"
        inside.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(inside))
        with pytest.raises(SwitchError):
            s.add_account_from_token("sk-ant-oat01-xyz")

    # -- gate never writes the live store (finding: vacuous mock test) ----

    def test_gate_success_never_touches_live_store(
        self, temp_home: Path, sample_sequence_data: dict
    ):
        """The REAL gate's persist path writes the slot backup only — a
        regression that wrote the live store would race a running CC."""
        s = self._switcher(sample_sequence_data)
        old = json.dumps({
            "claudeAiOauth": {"accessToken": "a", "refreshToken": "rt",
                              "expiresAt": 1000}})
        new = json.dumps({
            "claudeAiOauth": {"accessToken": "b", "refreshToken": "rt2",
                              "expiresAt": 9999999999000}})
        s._write_account_credentials("1", "test@example.com", old)
        with patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   return_value=oauth.RefreshOutcome(new, None)), \
             patch.object(s, "_write_credentials") as write_live:
            out = s.consume_backup_grant("1", "test@example.com", old)
        assert out.credentials == new
        write_live.assert_not_called()
        assert s._read_account_credentials("1", "test@example.com") == new


class TestUnreadableBackupIsNotAbsent:
    """The three sites this PR added or modified that still read the PLAIN
    reader's ``""`` as ABSENT.

    ``_read_account_credentials`` answers ``""`` for both "this slot has no
    credential" and "the Keychain would not let me read it". Every decision
    below is destructive in one direction or the other, so an unreadable
    backup must license neither.
    """

    _OLD = json.dumps({
        "claudeAiOauth": {"accessToken": "sk-old", "refreshToken": "rt-old",
                          "expiresAt": 1000}})
    _NEW = json.dumps({
        "claudeAiOauth": {"accessToken": "sk-new", "refreshToken": "rt-new",
                          "expiresAt": 9999999999000}})

    def _macos_switcher(self, sample_sequence_data, email="test@example.com"):
        sample_sequence_data["accounts"]["1"]["email"] = email
        s = ClaudeAccountSwitcher()
        s.platform = Platform.MACOS
        s._setup_directories()
        s._write_json(s.sequence_file, sample_sequence_data)
        return s

    # -- C-1: the consume gate's POST-POST CAS re-read -------------------

    def test_backup_unreadable_after_post_is_not_a_removed_slot(
        self, temp_home: Path, sample_sequence_data: dict, monkeypatch,
        block_real_keychain,
    ):
        """A Keychain that locks during the ~1s refresh POST must not be
        reported as "freshened, safe to activate".

        The PRE-POST read at the top of the gate uses ``_ex`` and defers on
        unreadable. The POST-POST CAS re-read uses the plain reader, so the
        same lock reads as "the slot was emptied mid-POST (remove-account)"
        — a reason deliberately excluded from ``_DEMOTING_STASH_REASONS``
        because a REMOVED slot has nothing left to activate. The grant IS
        spent and the slot still holds the generation that spent it, so
        ``error=None`` sends ``autoswitch._freshen_target`` and
        ``session.setup_session`` onto a credential whose first refresh gets
        ``invalid_grant``.
        """
        s = self._macos_switcher(sample_sequence_data)
        s._write_account_credentials("1", "test@example.com", self._OLD)
        assert s._read_account_credentials("1", "test@example.com") == self._OLD

        def refresh_then_lock(credentials, **kw):
            # The screen locks while the POST is in flight.
            monkeypatch.setattr(macos_keychain, "get_password", _raise_locked)
            return oauth.RefreshOutcome(self._NEW, None)

        with patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   side_effect=refresh_then_lock):
            out = s.consume_backup_grant("1", "test@example.com", self._OLD)

        reasons = [
            m.get("reason") for m in s.list_unclaimed_credentials().values()
        ]
        assert reasons, "the consumed successor must still be stashed"
        assert "consume-gate-slot-removed" not in reasons, (
            "an unreadable Keychain is not a removed slot"
        )
        assert out.error is not None, (
            "the slot still holds the generation whose grant was just spent, "
            "so `error is None` tells every caller it is safe to activate"
        )

    # -- C-2: the dead-token second-source check -------------------------

    def test_unreadable_backup_never_erases_a_live_dead_token_strike(
        self, temp_home: Path, sample_sequence_data: dict, monkeypatch,
        block_real_keychain,
    ):
        """One transient lock must not zero a persisted quarantine.

        ``_entry_token_dead``'s second-source check reads the active slot's
        backup with the plain reader, so an unreadable backup is
        ``bool("") is False`` and the strike is dropped.
        ``_collect_usage_entries`` then takes its
        ``elif entry.auth_dead_strikes and entry.token_dead():`` branch and
        calls ``clear_dead_token``, which zeroes ``authDeadStrikes`` AND
        ``struckFingerprint`` in the PERSISTED store. A genuinely dead
        account is un-quarantined by one momentary lock and never says
        "re-login needed" again.
        """
        s = self._macos_switcher(sample_sequence_data, email="b@example.com")
        live = json.dumps({
            "claudeAiOauth": {"accessToken": "sk-live",
                              "refreshToken": "rt-live",
                              "expiresAt": 9999999999000}})
        dead_backup = self._OLD
        s._write_account_credentials("1", "b@example.com", dead_backup)
        identities = {"1": ("b@example.com", "")}
        # The strike is bound to the BACKUP generation; the live credential
        # has since rotated onto a different one.
        s._usage_store.record(
            {"1": FetchRecord(
                error="invalid_grant",
                struck_fp=oauth.credential_fingerprint(dead_backup))},
            identities,
        )
        for _ in range(5):
            s._usage_store.record(
                {"1": FetchRecord(
                    error="invalid_grant",
                    struck_fp=oauth.credential_fingerprint(dead_backup))},
                identities,
            )
        entry = s._usage_store.entries(identities, [])["1"]
        assert entry.token_dead(), "the row must be struck to begin with"

        info = [(1, "b@example.com", "", "", True, live, "")]
        assert s._entry_token_dead(entry, "1", "b@example.com", live, True), (
            "with a readable backup the backup-bound strike holds"
        )

        monkeypatch.setattr(macos_keychain, "get_password", _raise_locked)
        s._collect_usage_entries(info, fetch=set())

        after = s._usage_store.entries(identities, [])["1"]
        assert after.auth_dead_strikes > 0, (
            "one unreadable pass erased the persisted strike count"
        )
        assert after.struck_fingerprint is not None, (
            "one unreadable pass erased the persisted struck fingerprint"
        )

    # -- C-2b: the dead-token second-source check with NO strike to hold -

    def test_active_slot_with_zero_strikes_and_unreadable_backup_is_not_dead(
        self, temp_home: Path, sample_sequence_data: dict, monkeypatch,
        block_real_keychain,
    ):
        """A transient Keychain read must not manufacture a dead verdict
        for a row that was never struck.

        The ``unreadable`` branch's own comment justifies holding a strike
        because the row "already took AUTH_DEAD_STRIKES invalid_grants" —
        but the code returns ``True`` unconditionally, with no check that
        any strike actually exists. A healthy active account (zero
        ``auth_dead_strikes``) whose backup read merely times out must not
        be reported as "re-login needed".
        """
        s = self._macos_switcher(sample_sequence_data, email="b@example.com")
        live = json.dumps({
            "claudeAiOauth": {"accessToken": "sk-live",
                              "refreshToken": "rt-live",
                              "expiresAt": 9999999999000}})
        identities = {"1": ("b@example.com", "")}
        entry = s._usage_store.entries(identities, [])["1"]
        assert entry.auth_dead_strikes == 0, "the row must never have been struck"

        monkeypatch.setattr(macos_keychain, "get_password", _raise_locked)
        assert not s._entry_token_dead(entry, "1", "b@example.com", live, True), (
            "zero strikes plus an unreadable backup must not report dead"
        )

    # -- C-3: the import auto-heal's verdict -----------------------------

    def test_unreadable_backup_is_not_a_dead_slot_for_import(
        self, temp_home: Path, sample_sequence_data: dict, monkeypatch,
        block_real_keychain,
    ):
        """``cswap import`` must not overwrite a healthy slot it cannot read.

        ``_slot_token_dead``'s ``stored`` read is plain. Unreadable → ``""``
        → ``credential_fingerprint("")`` is None → ``token_dead`` skips the
        binding check entirely (``stored_fp is not None and …``) → True.
        ``import_accounts`` then takes the "quarantined: refresh token dead"
        branch and REPLACES the slot's credential without ``--force``.
        """
        s = self._macos_switcher(sample_sequence_data, email="b@example.com")
        # The strike is bound to a generation the slot no longer stores, so
        # the correct verdict is "healed / not dead".
        s._write_account_credentials("1", "b@example.com", self._NEW)
        identities = {"1": ("b@example.com", "")}
        for _ in range(6):
            s._usage_store.record(
                {"1": FetchRecord(error="invalid_grant",
                                  struck_fp="sha256:condemnedgen")},
                identities,
            )
        assert s._usage_store.entries(identities, [])["1"].token_dead()
        assert not s._slot_token_dead("1", "b@example.com"), (
            "with a readable backup the strike is healed by the fingerprint"
        )

        monkeypatch.setattr(macos_keychain, "get_password", _raise_locked)
        assert not s._slot_token_dead("1", "b@example.com"), (
            "an unreadable backup skips the fingerprint binding entirely, so "
            "a healthy slot reads as dead and a plain import replaces it"
        )

    def test_an_active_slot_with_an_unreadable_backup_is_not_dead_for_import(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, monkeypatch, block_real_keychain,
    ):
        """The ACTIVE half of the same question, which has the opposite
        safe default from the collectors'.

        On an active slot ``_slot_token_dead`` delegates to
        ``_entry_token_dead``, whose backup read is the SECOND source. There,
        an unreadable backup must HOLD the strike (C-2: erasing it zeroes the
        persisted quarantine). Here the same True means "replace this slot's
        credential without --force". So the import's own read has to answer
        before the delegation, or fixing C-2 re-opens C-3 on the active path.
        """
        s = self._macos_switcher(sample_sequence_data, email="b@example.com")
        live = json.dumps({
            "claudeAiOauth": {"accessToken": "sk-live",
                              "refreshToken": "rt-live",
                              "expiresAt": 9999999999000}})
        cfg = s._get_claude_config_path()
        cfg.parent.mkdir(parents=True, exist_ok=True)
        s._write_json(cfg, {"oauthAccount": {
            "emailAddress": "b@example.com", "accountUuid": "uuid-1",
            "organizationUuid": "", "organizationName": "",
        }})
        s._store._write_active_credentials_file(live)
        s._write_account_credentials("1", "b@example.com", self._NEW)
        assert s.current_account_number() == "1"

        identities = {"1": ("b@example.com", "")}
        # Struck on a generation NEITHER stored source holds — healed.
        for _ in range(6):
            s._usage_store.record(
                {"1": FetchRecord(error="invalid_grant",
                                  struck_fp="sha256:condemnedgen")},
                identities,
            )
        assert s._usage_store.entries(identities, [])["1"].token_dead()
        assert not s._slot_token_dead("1", "b@example.com")

        monkeypatch.setattr(macos_keychain, "get_password", _raise_locked)
        assert not s._slot_token_dead("1", "b@example.com"), (
            "the active path delegates to the collectors' rule, which holds "
            "an unprovable strike — correct there, destructive here"
        )

    # -- C-2: `.value or ""` collapses a read ERROR into ABSENT -----------

    def test_active_read_error_is_not_condemned_as_dead(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, monkeypatch,
    ):
        """``ActiveCredentials.value`` is tri-state: ``""`` is genuinely
        absent in every backend, ``None`` is a plaintext-file read ERROR.
        ``_slot_token_dead``'s active branch used ``.value or ""``, which
        collapses ``None`` into ``""``. ``credential_fingerprint("")`` is
        None, and ``token_dead(stored_fp=None)`` skips the binding check
        and binds UNCONDITIONALLY — so a live slot whose credential
        momentarily could not be read was condemned as refresh-token-dead,
        and ``import_accounts`` replaces it without ``--force``. Platform-
        independent: the read error comes from the plaintext credentials
        file, not the Keychain, so this is provable on Linux.
        """
        s = ClaudeAccountSwitcher()
        s.platform = Platform.LINUX
        s._setup_directories()
        sample_sequence_data["accounts"]["1"]["email"] = "b@example.com"
        s._write_json(s.sequence_file, sample_sequence_data)
        dead = json.dumps({
            "claudeAiOauth": {"accessToken": "sk-dead", "refreshToken": "rt-dead",
                              "expiresAt": 1000}})
        live = json.dumps({
            "claudeAiOauth": {"accessToken": "sk-live", "refreshToken": "rt-live",
                              "expiresAt": 9999999999000}})
        cfg = s._get_claude_config_path()
        cfg.parent.mkdir(parents=True, exist_ok=True)
        s._write_json(cfg, {"oauthAccount": {
            "emailAddress": "b@example.com", "accountUuid": "uuid-1",
            "organizationUuid": "", "organizationName": "",
        }})
        identities = {"1": ("b@example.com", "")}
        for _ in range(6):
            s._usage_store.record(
                {"1": FetchRecord(error="invalid_grant",
                                  struck_fp=oauth.credential_fingerprint(dead))},
                identities,
            )
        assert s._usage_store.entries(identities, [])["1"].token_dead(), (
            "premise: the row must be struck to begin with"
        )

        def verdict(active_value):
            monkeypatch.setattr(
                s._store, "_read_active_credentials",
                lambda: ActiveCredentials(active_value, False, False),
            )
            return s._slot_token_dead("1", "b@example.com")

        monkeypatch.setattr(s, "current_account_number", lambda: "1")

        assert verdict(dead) is True, (
            "control A broken: the instrument never says yes (matches struck)"
        )
        assert verdict(live) is False, (
            "control B broken: the instrument never says no (replaced since)"
        )
        assert verdict(None) is False, (
            "an active credential that could not be READ (plaintext-file "
            "error) was condemned as refresh-token-dead; "
            "credential_fingerprint('') is None, which token_dead() treats "
            "as 'binds unconditionally', and import_accounts replaces the "
            "slot's credential without --force"
        )


class TestStashReaderUnreadableVsAbsent:
    """I-1: ``_read_unclaimed_credential`` collapses UNREADABLE onto ABSENT
    (its own docstring says so), so ``_adopt_stashed_successor`` silently
    declines and the gate falls through to POSTing the store's spent
    generation instead of adopting the live successor sitting right there.

    Rows A/B/C mirror the review's measured table exactly. The corrupt-entry
    row is a fourth, distinct state: its bytes are genuinely unrecoverable
    (not merely inaccessible right now), so it must keep its CURRENT
    behaviour byte-identical before and after the fix.
    """

    _OLD = json.dumps({
        "claudeAiOauth": {"accessToken": "sk-old", "refreshToken": "rt-old",
                          "expiresAt": 1000}})
    _NEW = json.dumps({
        "claudeAiOauth": {"accessToken": "sk-new", "refreshToken": "rt-new",
                          "expiresAt": 9999999999000}})

    def _switcher(self, sample_sequence_data):
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        s._write_json(s.sequence_file, sample_sequence_data)
        return s

    def _stash_successor(self, s):
        return s._store._write_unclaimed_credential(
            self._NEW,
            {"reason": "consume-gate-persist-lock-failed",
             "configSlot": "1",
             "consumedFp": oauth.credential_fingerprint(self._OLD),
             "fingerprint": oauth.credential_fingerprint(self._NEW)},
        )

    def _post_rejects_spent(self, credentials, **kw):
        # The gate is only ever exercised here with the spent snapshot as
        # input; the adopted-successor path short-circuits before any POST.
        assert credentials == self._OLD, (
            f"posted something other than the spent snapshot: {credentials!r}"
        )
        return oauth.RefreshOutcome(None, "invalid_grant")

    @pytest.mark.skipif(
        sys.platform == "win32" or os.geteuid() == 0,
        reason="needs POSIX permission semantics (non-root)",
    )
    def test_row_a_unreadable_entry_does_not_post_the_spent_generation(
        self, temp_home: Path, sample_sequence_data: dict,
    ):
        """ROW A: the stash entry exists but is momentarily unreadable
        (locked Keychain, mid-unmount, transient EIO). Adoption must DEFER,
        not fall through to POSTing the store's already-spent generation."""
        s = self._switcher(sample_sequence_data)
        s._write_account_credentials("1", "test@example.com", self._OLD)
        entry_path = s._store._stash_entry_path(self._stash_successor(s))
        entry_path.chmod(0o000)
        try:
            with patch("claude_swap.oauth.try_refresh_oauth_credentials",
                       side_effect=self._post_rejects_spent) as post:
                out = s.consume_backup_grant("1", "test@example.com", self._OLD)
        finally:
            entry_path.chmod(0o600)

        assert not post.called, (
            "DEFECT: an unreadable stash entry made adoption fall through "
            "and POST the spent generation it was supposed to replace"
        )
        assert out.error == "stash-unreadable", (
            f"unreadable stash entry -> error={out.error!r}; the entry is "
            "the only copy of the live credential, so the gate must defer, "
            "never report the spent-generation POST's invalid_grant"
        )
        assert s.list_unclaimed_credentials(), (
            "the entry must survive for the next pass to adopt"
        )

    def test_row_b_control_readable_entry_adopts(
        self, temp_home: Path, sample_sequence_data: dict,
    ):
        """ROW B (control): a readable entry adopts normally. Must stay
        true before and after the fix -- the fix only touches the unreadable
        branch."""
        s = self._switcher(sample_sequence_data)
        s._write_account_credentials("1", "test@example.com", self._OLD)
        self._stash_successor(s)

        with patch("claude_swap.oauth.try_refresh_oauth_credentials") as post:
            out = s.consume_backup_grant("1", "test@example.com", self._OLD)

        assert not post.called, "adopted successor must short-circuit the POST"
        assert out.error is None
        assert out.credentials == self._NEW
        assert not s.list_unclaimed_credentials(), "stash entry consumed"

    def test_row_c_control_nothing_stashed_posts_the_snapshot(
        self, temp_home: Path, sample_sequence_data: dict,
    ):
        """ROW C (control): nothing stashed, so the gate has no successor to
        adopt and must POST the snapshot. Must stay true before and after
        the fix."""
        s = self._switcher(sample_sequence_data)
        s._write_account_credentials("1", "test@example.com", self._OLD)

        with patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   side_effect=self._post_rejects_spent) as post:
            out = s.consume_backup_grant("1", "test@example.com", self._OLD)

        assert post.called
        assert out.error == "invalid_grant"

    @pytest.mark.skipif(
        sys.platform == "win32" or os.geteuid() == 0,
        reason="needs POSIX permission semantics (non-root)",
    )
    def test_corrupt_entry_keeps_its_current_behaviour(
        self, temp_home: Path, sample_sequence_data: dict,
    ):
        """A CORRUPT (undecodable) stash entry is a different defect from an
        unreadable one: its bytes are genuinely gone, not just momentarily
        inaccessible. It must keep POSTing the spent snapshot exactly as it
        does today -- the fix must not change this row at all."""
        s = self._switcher(sample_sequence_data)
        s._write_account_credentials("1", "test@example.com", self._OLD)
        entry_path = s._store._stash_entry_path(self._stash_successor(s))
        entry_path.write_text("not-valid-base64!!!", encoding="utf-8")

        with patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   side_effect=self._post_rejects_spent) as post:
            out = s.consume_backup_grant("1", "test@example.com", self._OLD)

        assert post.called, (
            "a corrupt (undecodable) entry must behave like ABSENT, not "
            "like unreadable -- its bytes are unrecoverable, not just "
            "temporarily inaccessible"
        )
        assert out.error == "invalid_grant"
        assert s._read_account_credentials("1", "test@example.com") == self._OLD, (
            "an unreadable/undecodable stash entry must never be written into "
            "the slot: an empty adopt destroys the slot's credential, and "
            "`refresh_input = current or snapshot` hides it from every "
            "POST-side assertion"
        )

    def test_row_d_absent_entry_bytes_terminate_instead_of_deferring(
        self, temp_home: Path, sample_sequence_data: dict,
    ):
        """ROW D: the manifest row survives (a crash between the unlink and
        the manifest rewrite in ``_remove_unclaimed_credential``), but the
        bytes are GONE. Unlike row A's momentarily-unreadable entry, no
        retry can ever adopt them -- the gate must reach a terminal verdict
        (POST the spent snapshot and report invalid_grant), not defer
        forever the way an UNREADABLE entry correctly does."""
        s = self._switcher(sample_sequence_data)
        s._write_account_credentials("1", "test@example.com", self._OLD)
        s._store._stash_entry_path(self._stash_successor(s)).unlink()

        with patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   side_effect=self._post_rejects_spent) as post:
            out = s.consume_backup_grant("1", "test@example.com", self._OLD)

        assert post.called, (
            "an absent entry must not defer -- nothing can ever adopt bytes "
            "that are already gone"
        )
        assert out.error == "invalid_grant"

    def test_absent_entry_is_retired_not_rescanned_forever(
        self, temp_home: Path, sample_sequence_data: dict,
    ):
        """Minor 5: an ABSENT-bytes row has the same never-adoptable
        property as a CAS-conflict entry, which is already retired at the
        point of discovery (see the ``consume-gate-cas-conflict`` branch
        above). Left un-retired, it is re-scanned on every gate pass and
        leaks in ``--json``'s ``unclaimedCredentials`` forever,
        indistinguishable from an entry still pending adoption."""
        s = self._switcher(sample_sequence_data)
        s._write_account_credentials("1", "test@example.com", self._OLD)
        s._store._stash_entry_path(self._stash_successor(s)).unlink()

        with patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   side_effect=self._post_rejects_spent):
            s.consume_backup_grant("1", "test@example.com", self._OLD)

        assert not s.list_unclaimed_credentials(), (
            "an absent-bytes stash entry must be retired once the gate "
            "confirms it can never be adopted, the same way a CAS-conflict "
            "entry already is"
        )

    @pytest.mark.skipif(
        sys.platform == "win32" or os.geteuid() == 0,
        reason="needs POSIX permission semantics (non-root)",
    )
    def test_unreadable_row_does_not_starve_a_readable_sibling(
        self, temp_home: Path, sample_sequence_data: dict,
    ):
        """Minor 2: two stash rows can share a ``consumedFp`` (repeated
        persist-failures on the same generation stash more than once). An
        unreadable row must not abort the whole manifest scan before a
        later, readable, adoptable sibling on the same generation is
        tried."""
        s = self._switcher(sample_sequence_data)
        s._write_account_credentials("1", "test@example.com", self._OLD)
        unreadable_id = self._stash_successor(s)
        entry_path = s._store._stash_entry_path(unreadable_id)
        entry_path.chmod(0o000)
        try:
            # A second row on the SAME generation, readable, must still be
            # reached and adopted despite the first row being unreadable.
            second_id = s._store._write_unclaimed_credential(
                self._NEW,
                {"reason": "consume-gate-persist-lock-failed",
                 "configSlot": "1",
                 "consumedFp": oauth.credential_fingerprint(self._OLD),
                 "fingerprint": oauth.credential_fingerprint(self._NEW)},
            )
            with patch("claude_swap.oauth.try_refresh_oauth_credentials") as post:
                out = s.consume_backup_grant("1", "test@example.com", self._OLD)
        finally:
            entry_path.chmod(0o600)

        assert not post.called, (
            "a readable sibling on the same generation must be adopted "
            "instead of POSTing the spent snapshot"
        )
        assert out.credentials == self._NEW, (
            f"expected the readable sibling's credentials to be adopted, "
            f"got {out.credentials!r} (error={out.error!r}) -- the "
            "unreadable row starved the scan before the readable one was "
            "reached"
        )
        assert second_id not in s.list_unclaimed_credentials()


class TestSessionShellGuardCoversEveryMutator:
    """H-4: `_refuse_session_shell`'s docstring claims "a shared chokepoint
    so every entry point is covered once", but the chokepoint it sits on is
    `_perform_switch`, not the store. Measured with CLAUDE_CONFIG_DIR pointed
    at a session profile dir:

        switch_to:        refused, correct
        remove_account:   SUCCEEDED inside a session shell   (seq now: [1])
        swap_accounts:    SUCCEEDED inside a session shell
        purge:            reached the confirmation prompt, not the guard

    `remove_account` additionally deletes the session profile of the very
    shell it is running in, via `_delete_account_files`.
    """

    def _switcher(self, sample_sequence_data, monkeypatch):
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        s._write_json(s.sequence_file, sample_sequence_data)
        inside = s.backup_dir / "sessions" / "1-test-example-com"
        inside.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(inside))
        return s

    def test_remove_account_refuses_inside_a_session_shell(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, monkeypatch,
    ):
        s = self._switcher(sample_sequence_data, monkeypatch)
        with pytest.raises(SwitchError):
            s.remove_account("2", assume_yes=True)
        assert s._get_sequence_data()["sequence"] == [1, 2], (
            "the roster was mutated from inside a session shell"
        )

    def test_swap_accounts_refuses_inside_a_session_shell(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, monkeypatch,
    ):
        s = self._switcher(sample_sequence_data, monkeypatch)
        with pytest.raises(SwitchError):
            s.swap_accounts("1", "2")

    def test_move_account_refuses_inside_a_session_shell(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, monkeypatch,
    ):
        s = self._switcher(sample_sequence_data, monkeypatch)
        with pytest.raises(SwitchError):
            s.move_account("2", "5")

    def test_purge_refuses_inside_a_session_shell(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, monkeypatch,
    ):
        """Must refuse at the guard, BEFORE the confirmation prompt — a
        prompt is not a guard, and purge deletes everything."""
        s = self._switcher(sample_sequence_data, monkeypatch)
        with pytest.raises(SwitchError):
            s.purge()

    def test_set_alias_refuses_inside_a_session_shell(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, monkeypatch,
    ):
        s = self._switcher(sample_sequence_data, monkeypatch)
        with pytest.raises(SwitchError):
            s.set_alias("2", "work")

    def test_unset_alias_refuses_inside_a_session_shell(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, monkeypatch,
    ):
        s = self._switcher(sample_sequence_data, monkeypatch)
        with pytest.raises(SwitchError):
            s.unset_alias("2")


def test_a_ctrl_c_during_the_roster_move_leaves_no_temp_file(temp_home: Path, monkeypatch):
    """The roster's temp file must not survive an interrupt.

    It removed the temp only in the invalid-JSON branch, so an interrupt in
    the chmod or the move stranded `sequence.<pid>.tmp` forever.
    """
    from claude_swap import switcher as switcher_mod

    switcher = ClaudeAccountSwitcher()
    target = switcher.backup_dir / "sequence.json"
    target.parent.mkdir(parents=True, exist_ok=True)

    def interrupted(*_a, **_kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(switcher_mod, "replace_with_retry", interrupted)
    # `raises` is the guard: without it this passes when the interrupt never
    # fires, and the assertion below would certify nothing.
    with pytest.raises(KeyboardInterrupt):
        switcher._write_json(target, {"activeAccountNumber": 1, "accounts": {}})

    strays = list(target.parent.glob(f".{target.name}.*.tmp"))
    assert strays == [], f"left behind {[s.name for s in strays]}"


def test_a_failed_roster_write_leaves_no_temp_file(temp_home: Path, monkeypatch):
    """The call that CREATES the temp must be inside the guard too.

    `os.open` creates the file before a single byte is written; a failure
    anywhere after it strands the name. Guarding only the publish leaves the
    roster writer with the exact defect the guard was added to close. The
    injection sits at the first call after the create, which is where the
    file exists and is still empty — the widest version of the case.
    """
    if sys.platform == "win32":
        pytest.skip("fchmod is POSIX-only; the create path differs on Windows")
    from claude_swap import switcher as switcher_mod

    switcher = ClaudeAccountSwitcher()
    target = switcher.backup_dir / "sequence.json"
    target.parent.mkdir(parents=True, exist_ok=True)

    def failing_fchmod(fd, mode):
        raise OSError("injected: no space left on device")

    monkeypatch.setattr(switcher_mod.os, "fchmod", failing_fchmod)
    with pytest.raises(OSError):
        switcher._write_json(target, {"activeAccountNumber": 1, "accounts": {}})

    strays = list(target.parent.glob(f".{target.name}.*.tmp"))
    assert strays == [], f"left behind {[s.name for s in strays]}"


def test_a_published_roster_is_not_unlinked_by_its_own_cleanup(temp_home: Path, monkeypatch):
    """On success the move consumed the temp, so the name is not ours."""
    from claude_swap import switcher as switcher_mod

    switcher = ClaudeAccountSwitcher()
    target = switcher.backup_dir / "sequence.json"
    target.parent.mkdir(parents=True, exist_ok=True)

    unlinked = []
    real_unlink = Path.unlink
    monkeypatch.setattr(
        Path, "unlink",
        lambda self, *a, **kw: (unlinked.append(str(self)), real_unlink(self, *a, **kw))[1],
    )

    switcher._write_json(target, {"activeAccountNumber": 1, "accounts": {}})

    # Instrument guard: the roster must actually have been published.
    assert target.exists(), "premise: no sequence.json written"
    assert not [u for u in unlinked if u.endswith(".tmp")], (
        f"cleanup touched a name it no longer owns: {unlinked}"
    )


def test_an_invalid_roster_readback_leaves_no_temp_file(temp_home: Path, monkeypatch):
    """The validation branch is a failure path too, and it owns the temp.

    It raises before the move, so the name is still ours and the cleanup
    must take it. Nothing in the suite reached this branch before.
    """
    switcher = ClaudeAccountSwitcher()
    target = switcher.backup_dir / "sequence.json"
    target.parent.mkdir(parents=True, exist_ok=True)

    real_read = Path.read_text

    def truncating_read(self, *a, **kw):
        # Only the temp read-back: a corrupt file is what this branch is for.
        if self.name.endswith(".tmp"):
            return "{"
        return real_read(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", truncating_read)
    with pytest.raises(ConfigError, match="Generated invalid JSON"):
        switcher._write_json(target, {"activeAccountNumber": 1, "accounts": {}})

    assert not target.exists(), "premise: nothing may be published"
    strays = list(target.parent.glob(f".{target.name}.*.tmp"))
    assert strays == [], f"left behind {[s.name for s in strays]}"


def test_a_non_ebusy_publish_failure_never_truncates_the_live_roster(
    temp_home: Path, monkeypatch
):
    """Only EBUSY earns a write-through; every other error keeps the roster.

    `shutil.move` fell back to copying on ANY rename error, and that copy
    overwrites the live roster in place -- so a failure part-way truncated it
    while the cleanup removed the last complete copy.
    """
    from claude_swap import switcher as switcher_mod

    switcher = ClaudeAccountSwitcher()
    target = switcher.backup_dir / "sequence.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    switcher._write_json(target, {"activeAccountNumber": 1, "accounts": {}})

    copies: list[str] = []
    fired = {"publish": False}

    def refused(*_a, **_kw):
        fired["publish"] = True
        raise PermissionError(errno.EACCES, "Permission denied")

    def recording_copy(src, dst, *a, **kw):
        copies.append(str(dst))
        raise AssertionError("a non-EBUSY failure must not write through")

    monkeypatch.setattr(switcher_mod, "replace_with_retry", refused)
    monkeypatch.setattr(switcher_mod.shutil, "copyfile", recording_copy)

    with pytest.raises(OSError):
        switcher._write_json(target, {"activeAccountNumber": 2, "accounts": {}})

    # Instrument guard: patching the wrong name makes every assertion below
    # trivially true -- `os.replace` does NOT route through `os.rename`.
    assert fired["publish"], "premise: the injected publish failure never fired"
    assert copies == [], f"a non-EBUSY error wrote through: {copies}"
    assert json.loads(target.read_text(encoding="utf-8"))["activeAccountNumber"] == 1
    assert list(target.parent.glob(f".{target.name}.*.tmp")) == []


def test_a_destination_that_refuses_rename_is_written_through(
    temp_home: Path, monkeypatch
):
    """A bind-mounted destination refuses rename; it must still be writable.

    `-v ~/.claude.json:/root/.claude.json` pins the inode, so `os.replace`
    raises EBUSY and writing through the mount is the only way to update it.
    `shutil.move` did this implicitly, for any error; only EBUSY earns it.

    The same mount refuses `utime`, so that is injected here too: `copystat`
    does not guard the call, and `copy2` would fail the publish AFTER the
    content had landed, turning a switch that worked into one the caller
    rolls back.
    """
    from claude_swap import switcher as switcher_mod

    switcher = ClaudeAccountSwitcher()
    target = switcher.backup_dir / "sequence.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    switcher._write_json(target, {"activeAccountNumber": 1, "accounts": {}})

    fired = {"replace": False}

    def busy(*_a, **_kw):
        fired["replace"] = True
        raise OSError(errno.EBUSY, "Device or resource busy")

    def refused_utime(*_a, **_kw):
        raise PermissionError(errno.EPERM, "Operation not permitted")

    if sys.platform != "win32":
        os.chmod(target, 0o644)
    monkeypatch.setattr(switcher_mod, "replace_with_retry", busy)
    monkeypatch.setattr(os, "utime", refused_utime)
    switcher._write_json(target, {"activeAccountNumber": 2, "accounts": {}})

    assert fired["replace"], "premise: the injected EBUSY was never reached"
    assert json.loads(target.read_text(encoding="utf-8"))["activeAccountNumber"] == 2
    assert list(target.parent.glob(f".{target.name}.*.tmp")) == []
    if sys.platform != "win32":
        # The chmod-on-the-temp design exists because a 0644 ~/.claude.json
        # once published a key world-readable; the write-through must carry it.
        assert oct(target.stat().st_mode & 0o777) == "0o600"


def test_an_interrupt_at_the_mode_call_does_not_strand_the_temp(
    temp_home: Path, monkeypatch
):
    """The chmod has to sit ABOVE the disown, not merely above the copy.

    `_write_json` hands the temp's name to `source` and clears `temp_path` so a
    dying copy leaves the complete content somewhere named. Anything that
    raises AFTER that hand-off and BEFORE the copy is stranded instead: the
    outer cleanup no longer owns the name and nothing prints it.
    """
    if sys.platform == "win32":
        pytest.skip("POSIX modes only")
    from claude_swap import switcher as switcher_mod

    switcher = ClaudeAccountSwitcher()
    target = switcher.backup_dir / "sequence.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    switcher._write_json(target, {"activeAccountNumber": 1, "accounts": {}})

    def busy(*_a, **_kw):
        raise OSError(errno.EBUSY, "Device or resource busy")

    fired = {"chmod": False}

    def interrupt_at_chmod(*_a, **_kw):
        fired["chmod"] = True
        raise KeyboardInterrupt

    monkeypatch.setattr(switcher_mod, "replace_with_retry", busy)
    monkeypatch.setattr(switcher_mod.os, "chmod", interrupt_at_chmod)
    with pytest.raises(KeyboardInterrupt):
        switcher._write_json(target, {"primaryApiKey": "sk-ant-EXAMPLE"})

    assert fired["chmod"], "premise: the injected interrupt never fired"
    strays = list(target.parent.glob(f".{target.name}.*.tmp"))
    assert strays == [], f"an interrupt at the mode call stranded {strays}"


def test_an_unreadable_temp_does_not_disarm_the_whole_recovery(
    temp_home: Path, monkeypatch
):
    """The source digest has to be taken while the temp is known-good.

    Read inside the recovery, it shares an `except OSError` with the
    destination's read — and the temp is unreadable on exactly the population
    that ARRIVES there through a source-side error, which is not an
    independent failure. Both obligations then became no-ops on the very
    failures that need them: a partial destination stays partial, and the
    narrowing to 0600 is never undone.
    """
    from claude_swap import switcher as switcher_mod

    posix = sys.platform != "win32"
    switcher = ClaudeAccountSwitcher()
    target = switcher.backup_dir / "sequence.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    switcher._write_json(target, {"activeAccountNumber": 1, "accounts": {}})
    if posix:
        os.chmod(target, 0o644)

    def busy(*_a, **_kw):
        raise OSError(errno.EBUSY, "Device or resource busy")

    fired = {"copied": False}

    def partial_then_the_source_goes_away(src, dst, **kw):
        payload = Path(src).read_bytes()
        Path(dst).write_bytes(payload[:20])   # a genuine partial
        os.unlink(src)                        # ...and now the temp is gone
        fired["copied"] = True
        raise OSError(errno.EIO, "source went away mid-copy")

    monkeypatch.setattr(switcher_mod, "replace_with_retry", busy)
    monkeypatch.setattr(
        switcher_mod.shutil, "copyfile", partial_then_the_source_goes_away
    )
    with pytest.raises(ConfigError):
        switcher._write_json(target, {"primaryApiKey": "sk-ant-EXAMPLE"})

    assert fired["copied"], "premise: the copy never ran"
    assert target.read_bytes() == b"", (
        "the recovery left a PARTIAL destination on disk because it could no "
        f"longer read the temp: {target.read_bytes()[:40]!r}"
    )
    # ONLY THE MODE HALF IS POSIX. Emptying a partial is a CONTENT obligation
    # and applies on every platform -- gating both on one skip is the mistake
    # the production comment records, where Windows kept a truncated
    # credential at 0o666.
    if posix:
        assert stat.S_IMODE(os.stat(target).st_mode) == 0o644, (
            "the narrowing to 0600 was never undone, so another uid reading "
            "this config gets EACCES for ever with only the copy error to go on"
        )


def test_an_unreadable_destination_is_not_EMPTIED_by_the_recovery(
    temp_home: Path, monkeypatch
):
    """THE OTHER READER. Its sibling above covers an unreadable TEMP; this is
    the destination.

    The digest read's `except OSError: return` sits ABOVE the mode restore, so
    a destination that becomes unreadable between the `before` read and the
    recovery -- EIO or ESTALE on the network mount that produced the EBUSY in
    the first place -- keeps 0600 for ever. The production comment argues that
    the two obligations must not be gated on one another; an early return
    gates them just as surely as a shared `except` did.
    """
    from claude_swap import switcher as switcher_mod

    if sys.platform == "win32":
        pytest.skip("`prior_mode` is captured on POSIX only")

    switcher = ClaudeAccountSwitcher()
    target = switcher.backup_dir / "sequence.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    switcher._write_json(target, {"activeAccountNumber": 1, "accounts": {}})
    os.chmod(target, 0o644)

    state = {"copied": False}
    real_read = Path.read_bytes

    def unreadable_after_the_copy(self):
        if state["copied"] and os.fspath(self) == os.fspath(target):
            raise OSError(errno.EIO, "destination went unreadable")
        return real_read(self)

    def partial_copy(src, dst, **kw):
        Path(dst).write_bytes(Path(src).read_bytes()[:20])
        state["copied"] = True
        raise OSError(errno.EIO, "copy died mid-write")

    monkeypatch.setattr(switcher_mod, "replace_with_retry",
                        lambda *a, **k: (_ for _ in ()).throw(
                            OSError(errno.EBUSY, "Device or resource busy")))
    monkeypatch.setattr(switcher_mod.shutil, "copyfile", partial_copy)
    monkeypatch.setattr(Path, "read_bytes", unreadable_after_the_copy)

    with pytest.raises(ConfigError):
        switcher._write_json(target, {"primaryApiKey": "sk-ant-EXAMPLE"})

    monkeypatch.undo()
    assert state["copied"], "premise: the copy never ran"
    # AND IT MUST NOT EMPTY WHAT IT COULD NOT READ. Dropping the `return`
    # frees the mode restore; it must not also hand the emptying a digest of
    # None, which differs from everything and so looks like a partial every
    # time. This is the `before is None` reasoning on the other read: with no
    # comparand, a complete write and a partial one are the same picture.
    assert os.stat(target).st_size != 0, (
        "the recovery emptied a destination it could not read, so it cannot "
        "have known whether it was destroying a partial or a finished write"
    )
    # THE MODE IS NOT ASSERTED HERE, and that is the point. This case's copy
    # died LATE, past the destination open, so the bytes on disk may be half a
    # token -- and the recovery cannot read them to find out. Widening there
    # publishes whichever it is, which the narrowing refusal above ranks worse
    # than a stuck mode. The restore has its own case, on the one shape where
    # the destination is provably untouched:
    # `test_an_unreadable_destination_IS_restored_when_the_copy_never_opened_it`.


def test_an_unreadable_destination_is_not_WIDENED_over_a_late_failure(
    temp_home: Path, monkeypatch
):
    """Freeing the mode restore must not publish what we cannot read.

    When the destination read fails, `touched` is False and the recovery
    cannot tell a finished write from a partial one holding half a token. The
    file's own ranking, at the narrowing refusal above, is that continuing
    "writes a credential the destination need not have held before, at a mode
    other users can read" -- so in THAT state the narrow mode is the safe
    answer, not the stale one.
    """
    from claude_swap import switcher as switcher_mod

    if sys.platform == "win32":
        pytest.skip("`prior_mode` is captured on POSIX only")

    switcher = ClaudeAccountSwitcher()
    target = switcher.backup_dir / "sequence.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    switcher._write_json(target, {"activeAccountNumber": 1, "accounts": {}})
    os.chmod(target, 0o644)

    state = {"copied": False}
    real_read = Path.read_bytes

    def unreadable_after_the_copy(self):
        if state["copied"] and os.fspath(self) == os.fspath(target):
            raise OSError(errno.EIO, "destination went unreadable")
        return real_read(self)

    def partial_copy(src, dst, **kw):
        # LATE, past the destination open: the bytes are on disk.
        Path(dst).write_bytes(Path(src).read_bytes()[:20])
        state["copied"] = True
        raise OSError(errno.EIO, "copy died mid-write", os.fspath(dst))

    monkeypatch.setattr(switcher_mod, "replace_with_retry",
                        lambda *a, **k: (_ for _ in ()).throw(
                            OSError(errno.EBUSY, "Device or resource busy")))
    monkeypatch.setattr(switcher_mod.shutil, "copyfile", partial_copy)
    monkeypatch.setattr(Path, "read_bytes", unreadable_after_the_copy)

    with pytest.raises(ConfigError):
        switcher._write_json(target, {"primaryApiKey": "sk-ant-EXAMPLE"})

    monkeypatch.undo()
    assert state["copied"], "premise: the copy never ran"
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600, (
        "the recovery widened a destination it could NOT read, after a copy "
        "that had already written to it — so a truncated credential is now "
        "readable by every other uid on the box"
    )


def test_an_emptying_that_FAILED_must_not_be_followed_by_a_widen(
    temp_home: Path, monkeypatch
):
    """`touched` orders the emptying; a swallowed failure keeps the verdict.

    `touched` being True IS the finding that the destination holds neither the
    original nor the complete payload -- a partial, with half a token in it.
    The recovery then empties the file. If that write is REFUSED, the file
    still holds the partial, and restoring the mode publishes it to every
    other uid.

    Reachable because the emptying is a write to the destination whose write
    just failed, so one fault produces both: a mount that went read-only
    mid-write, EIO on a failing disk, an overlay refusing truncate.
    `comparable` says only "I read both digests", never "the emptying landed".
    """
    from claude_swap import switcher as switcher_mod

    if sys.platform == "win32":
        pytest.skip("`prior_mode` is captured on POSIX only")

    switcher = ClaudeAccountSwitcher()
    target = switcher.backup_dir / "sequence.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    switcher._write_json(target, {"activeAccountNumber": 1, "accounts": {}})
    os.chmod(target, 0o644)

    state = {"copied": False, "emptying_refused": 0}
    real_open = builtins.open

    def refusing_open(file, mode="r", *a, **kw):
        if "w" in mode and os.fspath(file) == os.fspath(target):
            state["emptying_refused"] += 1
            raise OSError(errno.EROFS, "read-only file system")
        return real_open(file, mode, *a, **kw)

    def midcopy(src, dst, **kw):
        # shutil's real field shape on a mid-copy failure: the destination was
        # opened and truncated, and BOTH names are set.
        whole = Path(src).read_bytes()
        # A GENUINE PARTIAL. A slice longer than the payload copies all of it,
        # `after == landed`, and `touched` never fires -- the premise assert
        # below is what catches that, and did.
        assert len(whole) > 20, "payload too small to truncate meaningfully"
        Path(dst).write_bytes(whole[:20])
        state["copied"] = True
        err = OSError(errno.ENOSPC, "No space left on device")
        err.filename, err.filename2 = os.fspath(src), os.fspath(dst)
        raise err

    monkeypatch.setattr(switcher_mod, "replace_with_retry",
                        lambda *a, **k: (_ for _ in ()).throw(
                            OSError(errno.EBUSY, "Device or resource busy")))
    monkeypatch.setattr(switcher_mod.shutil, "copyfile", midcopy)
    monkeypatch.setattr(builtins, "open", refusing_open)

    with pytest.raises(ConfigError):
        switcher._write_json(target, {"primaryApiKey": "sk-ant-EXAMPLE"})

    monkeypatch.undo()
    assert state["copied"], "premise: the copy never ran"
    assert state["emptying_refused"] >= 1, (
        "premise: the emptying was never attempted, so this says nothing "
        "about what happens when it fails"
    )
    body = os.stat(target)
    assert stat.S_IMODE(body.st_mode) == 0o600, (
        "the emptying was REFUSED and the recovery widened anyway, so the "
        f"partial payload ({body.st_size} bytes) is now readable by every "
        "other uid on the box"
    )


def test_a_real_midcopy_failure_names_the_SOURCE_and_must_not_widen(
    temp_home: Path, monkeypatch
):
    """`copyfile` NAMES THE SOURCE ON A LATE FAILURE TOO, so `filename` alone
    cannot say the destination was never opened.

    Every fast-copy helper does `err.filename = fsrc.name; err.filename2 =
    fdst.name` before re-raising -- AFTER `open(dst, 'wb')` has truncated and
    partly written it. Measured with real, unmocked `shutil.copyfile` under
    RLIMIT_FSIZE: errno EFBIG, `filename == src`, `filename2 == dst`, and the
    destination holding a fragment of the credential.

    `filename2` alone is NOT the discriminator -- measured, the `copyfileobj`
    fallback raises with BOTH names None and the destination truncated, so it
    is `filename == source` that excludes that row. Neither conjunct decides
    alone: None on every path that raises
    before the destination is opened, set on every one that raises after.
    """
    from claude_swap import switcher as switcher_mod

    if sys.platform == "win32":
        pytest.skip("`prior_mode` is captured on POSIX only")

    switcher = ClaudeAccountSwitcher()
    target = switcher.backup_dir / "sequence.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    switcher._write_json(target, {"activeAccountNumber": 1, "accounts": {}})
    os.chmod(target, 0o644)

    state = {"copied": False}
    real_read = Path.read_bytes

    def unreadable_after_the_copy(self):
        if state["copied"] and os.fspath(self) == os.fspath(target):
            raise OSError(errno.EIO, "destination went unreadable")
        return real_read(self)

    def midcopy(src, dst, **kw):
        Path(dst).write_bytes(Path(src).read_bytes()[:20])
        state["copied"] = True
        # EXACTLY WHAT shutil SETS: the source in `filename`, the destination
        # in `filename2`. An injected `filename=dst` is a shape real copyfile
        # never produces for a late failure, and testing that one is what let
        # this through.
        err = OSError(errno.EFBIG, "File too large")
        err.filename = os.fspath(src)
        err.filename2 = os.fspath(dst)
        raise err

    monkeypatch.setattr(switcher_mod, "replace_with_retry",
                        lambda *a, **k: (_ for _ in ()).throw(
                            OSError(errno.EBUSY, "Device or resource busy")))
    monkeypatch.setattr(switcher_mod.shutil, "copyfile", midcopy)
    monkeypatch.setattr(Path, "read_bytes", unreadable_after_the_copy)

    with pytest.raises(ConfigError):
        switcher._write_json(target, {"primaryApiKey": "sk-ant-EXAMPLE"})

    monkeypatch.undo()
    assert state["copied"], "premise: the copy never ran"
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600, (
        "a copy that DID open the destination was read as untouched because "
        "`filename` names the source on that path too — so a truncated "
        "credential was published at 0644"
    )


@pytest.mark.parametrize("shape", ["source-open", "destination-open", "same-file"])
def test_a_matching_destination_the_copy_never_opened_is_still_restored(
    temp_home: Path, monkeypatch, shape
):
    """The complete-copy short-circuit ranks ABOVE the predicate that separates
    a copy that FINISHED from one that never opened the destination.

    `after == landed` is read as "the destination already holds the whole new
    payload". It is equally true when nothing was written and the payload
    happens to equal what was already there -- a switch to the account that is
    already active re-serialises byte-identical content. The mode restore is
    then skipped for a file nothing touched, so a 0644 config comes back 0600
    and whoever else reads it loses access over a write that never happened.
    """
    import shutil as shutil_mod

    from claude_swap import switcher as switcher_mod

    if sys.platform == "win32":
        pytest.skip("`prior_mode` is captured on POSIX only")

    switcher = ClaudeAccountSwitcher()
    target = switcher.backup_dir / "sequence.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"activeAccountNumber": 1, "accounts": {}}
    switcher._write_json(target, payload)
    os.chmod(target, 0o644)

    state = {"tried": False}

    def source_open_failure(src, dst, **kw):
        state["tried"] = True
        # THREE ROWS OF THE (filename, filename2) TABLE LEAVE THE DESTINATION
        # UNTOUCHED, and `untouched` recognises only the first. The other two
        # reach the short-circuit below with `after == landed` true for the
        # ordinary reason -- the payload equals what was already there.
        if shape == "source-open":
            raise FileNotFoundError(errno.ENOENT, "No such file", os.fspath(src))
        if shape == "destination-open":
            raise PermissionError(errno.EACCES, "cannot open", os.fspath(dst))
        raise shutil_mod.SameFileError("source and destination are the same")

    monkeypatch.setattr(switcher_mod, "replace_with_retry",
                        lambda *a, **k: (_ for _ in ()).throw(
                            OSError(errno.EBUSY, "Device or resource busy")))
    monkeypatch.setattr(switcher_mod.shutil, "copyfile", source_open_failure)

    with pytest.raises(ConfigError):
        switcher._write_json(target, payload)  # byte-identical to what is there

    monkeypatch.undo()
    assert state["tried"], "premise: the copy never ran"
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o644, (
        "a write that never opened the destination narrowed it anyway, because "
        "the payload happened to match what was already on disk"
    )


def test_write_all_finishes_a_short_write():
    """The behaviour the structural guard above only points at.

    `os.write` accepting fewer bytes is the whole reason the helper exists,
    and nothing else in the suite makes it happen.
    """
    from claude_swap.fsutil import write_all

    got = bytearray()
    real_write = os.write

    def one_byte_at_a_time(fd, data):
        got.extend(bytes(data[:1]))
        return 1

    payload = b'{"refreshToken": "rt-EXAMPLE"}'
    with patch.object(os, "write", one_byte_at_a_time):
        write_all(-1, payload)
    assert bytes(got) == payload, (
        f"a short write lost bytes: wrote {bytes(got)!r} of {payload!r}"
    )

    # CAPPED, so a DELETED guard fails instead of hanging. With the guard gone
    # a stub that always returns 0 never advances the view and the loop spins
    # for ever -- measured, the run had to be SIGKILLed at 45s, which reads as
    # a stuck CI rather than as this assertion. After the cap the same
    # deletion completes the write and `pytest.raises` fails, naming the guard.
    stalls = {"n": 0}

    def stalls_then_completes(fd, data):
        stalls["n"] += 1
        if stalls["n"] <= 3:
            return 0
        return len(data)

    # THE ERRNO, NOT MERELY AN OSError. `write_all(-1, ...)` raises EBADF on
    # its own, so a patch that silently stopped taking effect would satisfy a
    # bare `raises(OSError)` with the guard never reached.
    with patch.object(os, "write", stalls_then_completes):
        with pytest.raises(OSError) as excinfo:
            write_all(-1, payload)
    assert excinfo.value.errno == errno.EIO, (
        "the raise came from somewhere other than the zero-progress guard: "
        f"errno {excinfo.value.errno}"
    )


def _os_names(tree) -> set[str]:
    """Names this module can reach the `os` module through.

    Matching the literal `os` leaves `import os as _o` invisible, and one
    alias hid a regression from every scan that keys on it.
    """
    import ast

    names = {"os"} | {
        (a.asname or a.name)
        for imp in ast.walk(tree) if isinstance(imp, ast.Import)
        for a in imp.names if a.name == "os"
    }
    # AND A PLAIN REBINDING. `import os as _o` is not the only way to get a
    # second name for the module -- `_o = os` does it too, and an
    # `_o.open(..., O_TRUNC)` writer was invisible to every scan keying on
    # this. A fixpoint, because `_p = _o` chains.
    while True:
        grown = names | {
            t.id
            for n in ast.walk(tree) if isinstance(n, ast.Assign)
            for t in n.targets if isinstance(t, ast.Name)
            if isinstance(n.value, ast.Name) and n.value.id in names
        }
        if grown == names:
            return names
        names = grown


def _flat(body):
    """Statements in this block's OWN scope -- a nested `def` is not it.

    Recursive rather than `ast.walk`, which cannot PRUNE: filtering its
    output drops the `def` node and still yields the body underneath it, so
    an assignment inside a function that never runs counted as a disown.
    """
    import ast

    for st in body:
        # A LITERAL-FALSE BRANCH DOES NOT RUN, so a `raise` inside one is not
        # a re-raise. Only a constant is folded here -- anything needing real
        # analysis is left to count, which is the loud direction.
        if isinstance(st, ast.If) and isinstance(st.test, ast.Constant) \
                and not st.test.value:
            yield from _flat(st.orelse)
            continue
        yield st
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef,
                           ast.ClassDef)):
            continue
        for child in ast.iter_child_nodes(st):
            if isinstance(child, ast.stmt):
                yield from _flat([child])
            else:
                for sub in ast.iter_child_nodes(child):
                    if isinstance(sub, ast.stmt):
                        yield from _flat([sub])


def _bound_names(st) -> set[str]:
    """Names this statement binds or unbinds."""
    import ast

    def _bare(t):
        # ONLY A NAME IS BOUND. `self.x = 1` and `d[k] = 1` READ their base;
        # collecting it made `self._last_error = e; raise` -- the archetypal
        # record-and-re-raise this predicate exists to refuse -- a disown.
        if isinstance(t, ast.Name):
            return {t.id}
        if isinstance(t, (ast.Tuple, ast.List)):
            return {n for e in t.elts for n in _bare(e)}
        return set()

    if isinstance(st, ast.Assign):
        return {n for t in st.targets for n in _bare(t)}
    if isinstance(st, (ast.AnnAssign, ast.AugAssign)):
        return _bare(st.target)
    if isinstance(st, ast.Delete):
        # A DELETE OF A SUBSCRIPT DISOWNS ITS CONTAINER. `del staged[key]`
        # drops the entry the discard walks, which is a real disown even
        # though the container itself stays bound.
        out = set()
        for t in st.targets:
            out |= _bare(t)
            out |= {n.id for n in ast.walk(t) if isinstance(n, ast.Name)}
        return out
    return set()


def _removes_a_file(node) -> bool:
    """Does this subtree call something that REMOVES a path."""
    import ast

    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
        if name in {"unlink", "remove", "rmtree", "rmdir"} or "discard" in name:
            return True
    return False


def _cleanup_reads(tries, this_handler) -> set[str]:
    """Names that reach the cleanup's REMOVAL, not everything it reads.

    "Something the cleanup reads" is too wide: every cleanup here is
    `if fd >= 0: os.close(fd)` followed by the unlink, so `fd` is in the read
    set at almost every site -- and `fd = -1` is already written inside the
    guarded functions. A handler that binds it disowns nothing and the temp
    is still removed.

    So only the removal counts: the statement that removes the path, and the
    condition of any `if` guarding it. The handler's own `except` clause is
    excluded, or it would answer about itself.
    """
    import ast

    out: set[str] = set()

    def collect(st):
        if isinstance(st, ast.If) and _removes_a_file(st):
            for n in ast.walk(st.test):
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                    out.add(n.id)
            for sub in st.body + st.orelse:
                collect(sub)
            return
        if not _removes_a_file(st):
            return
        for n in ast.walk(st):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                out.add(n.id)

    for t in tries:
        blocks = list(t.finalbody) + [
            st for h in t.handlers if h is not this_handler for st in h.body]
        for st in blocks:
            collect(st)
    return out



def _os_call_aliases(tree, func: str, os_names: set[str]) -> set[str]:
    """Bare names this module can call `os.<func>` through.

    `from os import open as _open` and `_open = os.open` both bind a NAME,
    which an attribute match cannot see at all.
    """
    import ast

    names = {
        (a.asname or a.name)
        for imp in ast.walk(tree) if isinstance(imp, ast.ImportFrom)
        and imp.module == "os"
        for a in imp.names if a.name == func
    }

    def targets_of(n):
        # ANNASSIGN AND TUPLES TOO. The hoist resolver in the flags scan
        # already reads `AnnAssign`; these two were written knowing only
        # `Assign` with a bare `Name`, which is the same hole one node type
        # over.
        if isinstance(n, ast.Assign):
            ts = n.targets
        elif isinstance(n, ast.AnnAssign):
            ts = [n.target]
        else:
            return []
        out = []
        for t in ts:
            out += ([e for e in t.elts]
                    if isinstance(t, (ast.Tuple, ast.List)) else [t])
        return [t.id for t in out if isinstance(t, ast.Name)]

    # A FIXPOINT, because `_a = os.open; _b = _a` chains -- `_os_names` was
    # given one and this was not, so the chain walked past both scans.
    # AND THE BASE MUST BE AN `os` NAME: `X = shutil.open` is not an alias,
    # and accepting it accuses an unrelated call of being a temp writer.
    while True:
        grown = names | {
            t
            for n in ast.walk(tree)
            for t in targets_of(n)
            if (isinstance(getattr(n, "value", None), ast.Attribute)
                and n.value.attr == func
                and isinstance(n.value.value, ast.Name)
                and n.value.value.id in os_names)
            or (isinstance(getattr(n, "value", None), ast.Name)
                and n.value.id in names)
            # `X = getattr(os, "open")` -- the sibling write scan already
            # reads this spelling at its call site, and neither open scan
            # could see it: the site left BOTH denominators and an `O_TRUNC`
            # weakening ran green.
            or (isinstance(getattr(n, "value", None), ast.Call)
                and isinstance(n.value.func, ast.Name)
                and n.value.func.id == "getattr" and 2 <= len(n.value.args) <= 3
                and isinstance(n.value.args[0], ast.Name)
                and n.value.args[0].id in os_names
                and isinstance(n.value.args[1], ast.Constant)
                and n.value.args[1].value == func)
        }
        if grown == names:
            return names
        names = grown


def _resolved_flags(tree, node) -> str:
    """The `os.open` flags of this call, with a single hoisted binding resolved.

    Both open scans key on the flag names, and a name is not a verdict: a
    hoist hid a writer from the flags scan's offender list once, and from the
    disown scan's site set entirely.
    """
    import ast

    kw = {k.arg: k.value for k in node.keywords}
    arg = node.args[1] if len(node.args) >= 2 else kw.get("flags")
    if arg is None:
        return ""
    flags = ast.unparse(arg)
    if not flags.isidentifier():
        return flags
    bound = [
        ast.unparse(a.value) for a in ast.walk(tree)
        if ((isinstance(a, ast.Assign) and len(a.targets) == 1
             and isinstance(a.targets[0], ast.Name)
             and a.targets[0].id == flags)
            or (isinstance(a, ast.AnnAssign)
                and isinstance(a.target, ast.Name)
                and a.target.id == flags))
        and a.value is not None
    ]
    return bound[0] if len(bound) == 1 else flags

def _is_os_call(node, func: str, os_names: set[str], aliases: set[str]) -> bool:
    """Does this `Call` reach `os.<func>` under ANY of its spellings."""
    import ast

    if not isinstance(node, ast.Call):
        return False
    f = node.func
    if isinstance(f, ast.Attribute):
        return (f.attr == func and isinstance(f.value, ast.Name)
                and f.value.id in os_names)
    # `getattr(os, "open")(...)` called DIRECTLY. The assigned form
    # (`X = getattr(os, "open")`) is already an alias; without this the
    # direct call leaves both scans -- offenders AND denominator -- so no
    # floor can notice it and `unreadable` never fires.
    if (isinstance(f, ast.Call) and isinstance(f.func, ast.Name)
            and f.func.id == "getattr" and 2 <= len(f.args) <= 3
            and isinstance(f.args[0], ast.Name) and f.args[0].id in os_names
            and isinstance(f.args[1], ast.Constant)
            and f.args[1].value == func):
        return True
    return isinstance(f, ast.Name) and f.id in aliases


def test_no_writer_calls_os_write_bare():
    """`os.write` is write(2): it may write FEWER bytes than it was given.

    These callers publish credentials, and the `replace_with_retry` that
    follows succeeds either way -- so a short write does not surface as a
    failed write, it surfaces as a corrupt account. The count is the only
    thing that says which happened and every site discarded it.

    Structural for the same reason as its two siblings above: the fix is one
    line per call site, so a per-site assertion goes stale the moment a
    seventh writer is added.
    """
    import ast

    src_dir = Path(__file__).resolve().parent.parent / "src" / "claude_swap"
    offenders = []
    for mod in sorted(src_dir.rglob("*.py")):
        tree = ast.parse(mod.read_text(encoding="utf-8"))
        # THE LOOP THAT EXISTS TO DO THIS IS THE ONE PLACE ALLOWED TO. Named
        # rather than exempting its module, so a second exemption has to be
        # written down here to take effect.
        # THE HELPER'S OWN MODULE, not any function of that name. A second
        # `def write_all` elsewhere in src/ would otherwise exempt itself.
        exempt = {
            id(n) for f in ast.walk(tree)
            if mod.name == "fsutil.py"
            and isinstance(f, ast.FunctionDef) and f.name == "write_all"
            for n in ast.walk(f)
        }
        # EVERY NAME THAT REACHES THE MODULE, not the literal `os`. Matching
        # `os.write` alone left `import os as _o; _o.write(...)` invisible --
        # and in a module with other `write_all` sites it did not even move
        # the denominator, so a live regression in the credential writer ran
        # green.
        os_names = _os_names(tree)
        # `from os import write as _w` binds a bare NAME, which an attribute
        # match cannot see; resolve the aliases this module actually created.
        aliases = _os_call_aliases(tree, "write", os_names)
        # A LOCAL BOUND TO THE FUNCTION IS THE FUNCTION. `w = os.write` then
        # `w(fd, ...)` is the same call under a name the scans above cannot
        # see, and it is the spelling a reader reaches for when the call is
        # in a loop.
        aliases |= {
            t.id
            for a in ast.walk(tree) if isinstance(a, ast.Assign)
            for t in a.targets if isinstance(t, ast.Name)
            if isinstance(a.value, ast.Attribute) and a.value.attr == "write"
            and isinstance(a.value.value, ast.Name)
            and a.value.value.id in os_names
        }
        # A STAR IMPORT MAKES THE QUESTION UNANSWERABLE, so it is the answer.
        # Nothing in this package does it, and the first thing that does would
        # otherwise silently turn every scan here into a pass.
        if any(isinstance(imp, ast.ImportFrom) and imp.module == "os"
               and any(a.name == "*" for a in imp.names)
               for imp in ast.walk(tree)):
            offenders.append(f"{mod.name}: `from os import *` hides every "
                             "bare write from this scan")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or id(node) in exempt:
                continue
            f = node.func
            bare = isinstance(f, ast.Name) and f.id in aliases
            dotted = (isinstance(f, ast.Attribute) and f.attr == "write"
                      and isinstance(f.value, ast.Name)
                      and f.value.id in os_names)
            # `getattr(os, "write")(fd, ...)` -- the call's func is itself a
            # `getattr` call, which neither branch above is shaped to see.
            fetched = (
                isinstance(f, ast.Call) and isinstance(f.func, ast.Name)
                and f.func.id == "getattr" and 2 <= len(f.args) <= 3
                and isinstance(f.args[0], ast.Name)
                and f.args[0].id in os_names
                and isinstance(f.args[1], ast.Constant)
                and f.args[1].value == "write"
            )
            if bare or dotted or fetched:
                offenders.append(f"{mod.name}:{node.lineno}")

    # THE SUBJECT FIRST. A denominator that runs ahead of it reports a code
    # regression as an instrument failure -- reverting one call site drops the
    # helper's user count below the floor, and "the instrument, not the code"
    # is then the wrong sentence about the right defect.
    assert not offenders, (
        "a writer discards `os.write`'s count, so a short write publishes a "
        f"truncated file and the rename still succeeds: {offenders}"
    )
    # THE DENOMINATOR THAT SURVIVES THE FIX. Counting bare `os.write` cannot
    # be one: it is zero once this passes, so a guard resting on it would
    # report clean over a package that had stopped writing anything.
    # CALL SITES, STRUCTURALLY, AND WITH SLACK. The substring form counted
    # MODULES -- three sites in one file counted once -- and broke on any
    # other variable name, so `write_all(owned, ...)` would not have matched.
    # It also sat exactly ON its floor, which makes the CORRECT refactor
    # (hand the fd to `os.fdopen` and let the buffered writer loop) fail RED
    # with "the instrument, not the code" -- the wrong sentence about a good
    # change. That is the refactor `_write_json` itself already uses.
    users = 0
    for mod in src_dir.rglob("*.py"):
        tree = ast.parse(mod.read_text(encoding="utf-8"))
        named = {
            (a.asname or a.name)
            for imp in ast.walk(tree) if isinstance(imp, ast.ImportFrom)
            and (imp.module or "").endswith("fsutil")
            for a in imp.names if a.name == "write_all"
        }
        users += sum(
            1 for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id in named
        )
    assert users >= 4, (
        f"the instrument, not the code: only {users} call site(s) of the "
        "checked helper, so this would pass over almost nothing"
    )


def test_no_writer_chmods_after_it_publishes():
    """The try block must end AT the publish, everywhere it was moved once.

    `replace_with_retry` is the commit point. A `chmod` on the TARGET after it
    can only fail, and the `except BaseException` around these writers then
    reports a write that LANDED as a failure — the caller rolls back or
    retries a file that is already correct. The mode is not what is at stake:
    `O_EXCL` opens at 0600 and a umask only clears bits, so these temps are
    never wider, which is why one site moved the call onto the fd and the
    others can too.

    Structural because it is an ORDERING, and orderings do not survive being
    asserted one site at a time: the round that moved the first of six left
    five behind and the suite stayed green.
    """
    import re
    import ast

    src_dir = Path(__file__).resolve().parent.parent / "src" / "claude_swap"
    offenders, publishes = [], 0
    for mod in sorted(src_dir.rglob("*.py")):
        tree = ast.parse(mod.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            pub, chmods = None, []
            for i, stmt in enumerate(node.body):
                text = ast.unparse(stmt)
                if "replace_with_retry(" in text and pub is None:
                    pub = i
                    publishes += 1
                # ANY RECEIVER, AND THE LOOKBEHIND WAS EXCLUDING THE ONES
                # THAT OCCUR: `(?<![\w.])` rejected the dot in
                # `self.path.chmod(`, the natural regression beside a
                # `replace_with_retry(tmp, self.path)` publish. `os.fchmod`
                # is deliberately NOT here -- it takes an fd, which is the
                # cure this scan exists to push writers towards.
                if re.search(r"\.(chmod|lchmod|copymode)\(|(?<!\w)chmod\(",
                             text):
                    chmods.append(i)
            # EVERY chmod, not the first. Keeping only the first one recorded
            # the `os.fchmod` CURE that runs before the publish and then
            # compared THAT index, so a real chmod after the publish could
            # not be reached -- the widening masked the offence it added.
            after = [i for i in chmods if pub is not None and i > pub]
            offenders += [f"{mod.name}:{node.body[i].lineno}" for i in after]

    # THE SUBJECT FIRST, like its two siblings. A refactor that consolidates
    # publishes takes the count under the floor, and a real post-publish
    # chmod then reports as "the instrument, not the code" with the offender
    # list never printed -- measured, 10 live against a floor of 5.
    assert not offenders, (
        "a chmod runs AFTER the publish, so its failure reports a landed "
        f"write as a failed one: {offenders}"
    )
    assert publishes >= 5, (
        f"the instrument, not the code: only {publishes} publish(es) were "
        "found inside a try block, so this would pass over almost nothing"
    )


def test_a_temp_name_already_taken_is_not_deleted(temp_home: Path, monkeypatch):
    """O_EXCL refuses the name; the cleanup must not then remove their file.

    The refusal means somebody else holds it. `temp_path` is still set when
    `os.open` raises, so the `finally` unlinks a file this writer never
    created -- and under O_TRUNC that case could not arise, so the conversion
    is what opened it. Both sibling writers in this file already guard it:
    `_salvage` tracks `created`, and `_stage_overlap_material` drops the key
    on EEXIST because it "lost the race; not ours to remove".
    """
    from claude_swap import switcher as switcher_mod

    switcher = ClaudeAccountSwitcher()
    target = switcher.backup_dir / "sequence.json"
    target.parent.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(switcher_mod.secrets, "token_hex", lambda n: "deadbeef")
    squatted = target.parent / f".{target.name}.{os.getpid()}.deadbeef.tmp"
    squatted.write_text("A PEER'S IN-PROGRESS FILE", encoding="utf-8")

    with pytest.raises(Exception):
        switcher._write_json(target, {"activeAccountNumber": 1})

    assert squatted.exists(), (
        "the cleanup removed a temp this writer refused to create; O_EXCL "
        "raised because somebody else holds that name"
    )
    assert squatted.read_text(encoding="utf-8") == "A PEER'S IN-PROGRESS FILE"


def test_every_O_EXCL_writer_disowns_a_name_it_refused_to_create():
    """`O_EXCL` is what makes `FileExistsError` reachable, so the conversion is
    what opened this. A writer that refuses the name and then unlinks it in a
    `finally` deletes the file the OTHER process is in the middle of writing --
    and two of them swallow the exception, so that happens with no error at all.

    Derived, because the guard was added at ONE of the twelve converted sites.
    A per-site test is right until the thirteenth writer, and the thirteenth is
    the one nobody checks -- the same argument the sibling scans make.

    THE OPEN'S OWN `try`, not the function's. Asking whether the FUNCTION
    mentions `FileExistsError` anywhere would pass a handler wrapped round the
    wrong statement, and it reported `_salvage_unreadable` -- which catches the
    create's `OSError` and clears its `created` claim -- as an offender. What
    has to exist is a branch on THIS call's failure, running before any
    cleanup: either spelling of the catch does the job, and neither is
    substitutable by a handler somewhere else in the function.
    """
    import ast

    src_dir = Path(__file__).resolve().parent.parent / "src" / "claude_swap"
    offenders, seen = [], 0
    for mod in sorted(src_dir.rglob("*.py")):
        tree = ast.parse(mod.read_text(encoding="utf-8"))
        # Every `Try` that lexically contains each node, innermost last.
        guarding: dict[int, list[ast.Try]] = {}
        def descend(node, stack):
            guarding[id(node)] = stack
            for child in ast.iter_child_nodes(node):
                deeper = stack
                if isinstance(node, ast.Try) and any(
                        child is st for st in node.body):
                    deeper = stack + [node]
                descend(child, deeper)
        descend(tree, [])

        os_names = _os_names(tree)
        open_aliases = _os_call_aliases(tree, "open", os_names)
        for node in ast.walk(tree):
            if not _is_os_call(node, "open", os_names, open_aliases):
                continue
            # THE SAME HOIST ITS SIBLING RESOLVES. A substring test on the
            # unparsed call drops `os.open(tmp, _EXCL_FLAGS, 0o600)` out of
            # this scan entirely -- measured, with the disown deleted and
            # both scans green -- while the flags scan still reads it.
            if "O_EXCL" not in _resolved_flags(tree, node):
                continue
            seen += 1
            # NOT "IS THERE A HANDLER" -- that is true of every site here,
            # so it discriminates nothing. What has to be true is that the
            # handler for THIS call DISOWNS the name before the cleanup can
            # reach it: it assigns something (the temp name, or the flag the
            # cleanup consults) and re-raises. A bare `except OSError: raise`
            # satisfies the weaker question and still deletes the winner's
            # file -- measured, with the suite green.
            name = (node.args[0].id if node.args
                    and isinstance(node.args[0], ast.Name) else None)
            ok = False
            t_stack = guarding.get(id(node), [])
            # THE INNERMOST `Try` ONLY. A disown in an enclosing handler runs
            # AFTER the inner cleanup has already unlinked the winner's file,
            # so crediting it certifies the exact harm this scan forbids.
            for t in t_stack[-1:]:
                for h in t.handlers:
                    if h.type is None:
                        continue
                    caught = ast.unparse(h.type)
                    if not ("FileExistsError" in caught or "OSError" in caught):
                        continue
                    # IT MUST CHANGE SOMETHING THE CLEANUP READS. "is
                    # there an Assign" is satisfied by `_msg = str(e)`, by
                    # `del payload`, and by an assignment inside a nested
                    # `def` that never runs -- each leaves the name in reach
                    # of the cleanup, measured with the suite green. The real
                    # disowns are not all rebindings of the temp name either:
                    # one clears the flag the cleanup guards its unlink with,
                    # another drops the entry the discard walks.
                    # THE CLEANUP'S OWN SUBJECT. Adding the `os.open` arg
                    # unconditionally accepted a disown of the wrong name:
                    # the staging cleanup walks `staged`, never the temp, so
                    # `path = None` satisfied it while the discard still
                    # unlinked the winner's file.
                    reads = _cleanup_reads(t_stack, h)
                    # THE HANDLER'S OWN STRAIGHT LINE. A scope walk asks
                    # whether the disown is PRESENT; what has to hold is that
                    # it RUNS. `if fd >= 0: tmp = None` is false on every
                    # EEXIST path -- `fd` is -1 there -- so the temp is back
                    # in the cleanup's reach, and nine of twelve sites stayed
                    # green with the suite byte-identical.
                    disowns = any(_bound_names(st) & reads for st in h.body)
                    reraises = any(isinstance(st, ast.Raise) for st in h.body)
                    if disowns and reraises:
                        ok = True
            if not ok:
                offenders.append(f"{mod.name}:{node.lineno}"
                                 + (f" ({name})" if name else ""))

    # THE SUBJECT FIRST. A denominator ahead of it reports a real regression
    # as a broken parser: reverting six writers to `O_TRUNC` DROPS the count,
    # so the compound regression came out as "the instrument, not the code"
    # and the offender list was never printed.
    assert not offenders, (
        "an `O_EXCL` open has no branch on its own failure, so the name it "
        "was REFUSED is still in reach of the cleanup below -- it deletes "
        f"whatever the holder is writing: {offenders}"
    )
    assert seen >= 8, (
        f"the instrument, not the code: only {seen} `O_EXCL` open(s) were "
        "found, so this would pass over almost nothing"
    )


def test_every_temp_writer_opens_with_O_EXCL():
    """The invariant `_write_json`'s own docstring states, checked structurally.

    "Temps ... are created with ``O_EXCL`` and never overwrite an existing
    file" -- and one writer of the dozen opened `O_TRUNC`, which does exactly
    what that sentence forbids. Derived from the source rather than listed:
    a per-site literal is right until the next writer is added, and the
    thirteenth is the one nobody checks.

    `O_EXCL` is not decoration here. The temp names carry a random token, so
    a collision means somebody else holds the name -- and the safe answer to
    that is to fail, not to truncate what they are writing. It also refuses
    to follow a symlink planted at the name.
    """
    import ast
    import re

    src_dir = Path(__file__).resolve().parent.parent / "src" / "claude_swap"
    offenders, unreadable, seen = [], [], 0
    for mod in sorted(src_dir.rglob("*.py")):
        tree = ast.parse(mod.read_text(encoding="utf-8"))
        # EVERY NAME THAT REACHES THE MODULE. Its sibling scan resolves
        # `import os as _o` and this one did not, so that one alias hid an
        # `O_TRUNC` regression from BOTH -- the disown scan filters on the
        # literal `O_EXCL`, which the aliased call no longer carries.
        # PER MODULE, not per node: both resolvers walk the whole tree, so
        # calling them inside the loop is quadratic on the larger modules.
        os_names = _os_names(tree)
        open_aliases = _os_call_aliases(tree, "open", os_names)
        for node in ast.walk(tree):
            if not _is_os_call(node, "open", os_names, open_aliases):
                continue
            kw = {k.arg: k.value for k in node.keywords}
            arg = (node.args[1] if len(node.args) >= 2
                   else kw.get("flags"))
            if arg is None:
                continue  # a read; no creation flags to judge
            flags = ast.unparse(arg)
            # A NAME IS NOT A VERDICT. Hoisting the flags into a local hid the
            # writer from the offender list AND from the denominator, so the
            # count fell and nothing complained. Resolve a single binding --
            # `ast.AnnAssign` too, which the first cut of this missed.
            if flags.isidentifier():
                # THE ISINSTANCE FIRST. `ast.walk` yields the Module before
                # anything else and a Module has no `.value`, so leading with
                # that test raises `AttributeError` the moment any site
                # spells its flags as a name -- and a real weakening then
                # reports as a crashed instrument instead of an offender.
                bound = [
                    ast.unparse(a.value) for a in ast.walk(tree)
                    if ((isinstance(a, ast.Assign) and len(a.targets) == 1
                         and isinstance(a.targets[0], ast.Name)
                         and a.targets[0].id == flags)
                        or (isinstance(a, ast.AnnAssign)
                            and isinstance(a.target, ast.Name)
                            and a.target.id == flags))
                    and a.value is not None
                ]
                flags = bound[0] if len(bound) == 1 else flags
            # UNREADABLE IS NOT SAFE, and this is where the previous cut let
            # six spellings through. A numeric literal, a partial hoist
            # (`_base | os.O_TRUNC`), a module alias, a tuple unpack -- each
            # one failed the `O_CREAT` substring test and took the `continue`
            # that means "not creating anything". "I could not read it" and
            # "it is safe" must not share a branch, so anything that is not a
            # plain `|` chain of `os.O_*` names is an offender.
            # ITS OWN VERDICT, because "I could not read it" is not "it can
            # overwrite an existing file" -- reported under the offenders'
            # sentence, a portable `| getattr(os, "O_NOFOLLOW", 0)` (strictly
            # safer, `O_EXCL` intact) is accused of the opposite of what it
            # does. A `getattr` with a literal name IS readable, so it is not
            # unreadable either.
            terms = [t.strip() for t in flags.split("|")]
            # THE SAME NAMES THE MATCHER RESOLVED. Hardcoding `os` here
            # made an aliased chain unreadable, so safe code (`O_EXCL`
            # intact under `import os as _o`) failed under the "can be
            # neither proven nor refuted" sentence, and a real aliased
            # `O_TRUNC` was filed there too instead of as an offender.
            readable = re.compile(
                r'(?:' + '|'.join(re.escape(n) for n in sorted(os_names))
                + r')\.O_[A-Z_]+'
                r'|getattr\(\s*\w+\s*,\s*[\'"]O_[A-Z_]+[\'"]\s*(?:,[^)]*)?\)')
            if not all(readable.fullmatch(t) for t in terms):
                unreadable.append(f"{mod.name}:{node.lineno} `{flags}`")
                continue
            if "O_CREAT" not in flags:
                continue  # not creating anything
            seen += 1
            if "O_EXCL" not in flags:
                offenders.append(f"{mod.name}:{node.lineno} {flags}")

    # THE SUBJECT FIRST, for the reason its sibling states: reverting writers
    # to `O_TRUNC` moves the count as well as the offender list, so a
    # denominator asserted ahead of it blames the parser for the regression.
    assert not offenders, (
        "a temp writer can overwrite an existing file, which is what the "
        f"`_write_json` docstring says none of them does: {offenders}"
    )
    assert not unreadable, (
        "a temp writer's open flags are not a plain `os.O_*` chain, so "
        f"`O_EXCL` can be neither proven nor refuted here: {unreadable}"
    )
    assert seen >= 10, (
        f"the instrument, not the code: only {seen} creating `os.open` call(s) "
        "were found, so this would pass over almost nothing"
    )


def test_an_unreadable_destination_IS_restored_when_the_copy_never_opened_it(
    temp_home: Path, monkeypatch
):
    """THE CONTROL, and the case the narrow answer must not swallow.

    `copyfile` raises on four paths BEFORE it opens the destination, and there
    the destination still holds its original bytes -- so there is nothing that
    could have been published and the mode must come back. `copy_err.filename`
    is the SOURCE on exactly those paths, which is how the two are told apart
    without reading a destination that will not answer.
    """
    from claude_swap import switcher as switcher_mod

    if sys.platform == "win32":
        pytest.skip("`prior_mode` is captured on POSIX only")

    switcher = ClaudeAccountSwitcher()
    target = switcher.backup_dir / "sequence.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    switcher._write_json(target, {"activeAccountNumber": 1, "accounts": {}})
    os.chmod(target, 0o644)

    state = {"tried": False, "src": None}
    real_read = Path.read_bytes

    def unreadable_destination(self):
        if state["tried"] and os.fspath(self) == os.fspath(target):
            raise OSError(errno.EIO, "destination went unreadable")
        return real_read(self)

    def source_open_failure(src, dst, **kw):
        state["tried"], state["src"] = True, os.fspath(src)
        # The destination is NEVER opened; filename names the SOURCE.
        raise FileNotFoundError(errno.ENOENT, "No such file", os.fspath(src))

    monkeypatch.setattr(switcher_mod, "replace_with_retry",
                        lambda *a, **k: (_ for _ in ()).throw(
                            OSError(errno.EBUSY, "Device or resource busy")))
    monkeypatch.setattr(switcher_mod.shutil, "copyfile", source_open_failure)
    monkeypatch.setattr(Path, "read_bytes", unreadable_destination)

    with pytest.raises(ConfigError):
        switcher._write_json(target, {"primaryApiKey": "sk-ant-EXAMPLE"})

    monkeypatch.undo()
    assert state["tried"], "premise: the copy never ran"
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o644, (
        "the narrowing was never undone on a destination the copy never "
        "opened — nothing could have been published, so leaving it at 0600 "
        "gives another uid EACCES for ever over a write that never happened"
    )


def test_a_completed_copy_is_not_emptied_by_the_recovery(
    temp_home: Path, monkeypatch
):
    """The recovery must empty only what the copy TRUNCATED.

    `copyfile` uses `sendfile` on Linux, and the last byte lands before the
    signal is delivered -- so the ordinary interrupt here is one where the
    destination is already complete and correct. Deciding on "differs from
    `before`" cannot tell that from a partial: a finished write differs too.
    """
    if sys.platform == "win32":
        pytest.skip("POSIX modes only")
    from claude_swap import switcher as switcher_mod

    switcher = ClaudeAccountSwitcher()
    target = switcher.backup_dir / "sequence.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    switcher._write_json(target, {"activeAccountNumber": 1, "accounts": {}})
    os.chmod(target, 0o644)

    def busy(*_a, **_kw):
        raise OSError(errno.EBUSY, "Device or resource busy")

    real_copyfile = switcher_mod.shutil.copyfile
    fired = {"copied": False}

    def copy_then_interrupt(src, dst, **kw):
        real_copyfile(src, dst, **kw)          # every byte lands
        fired["copied"] = True
        raise KeyboardInterrupt                # ...and then the signal

    payload = {"activeAccountNumber": 2, "accounts": {"2": {"email": "x@y.z"}}}
    monkeypatch.setattr(switcher_mod, "replace_with_retry", busy)
    monkeypatch.setattr(switcher_mod.shutil, "copyfile", copy_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        switcher._write_json(target, payload)

    assert fired["copied"], "premise: the copy never ran, so nothing completed"
    assert target.read_bytes(), (
        "the recovery emptied a destination the copy had COMPLETED, turning a "
        "benign interrupt into a destroyed config plus a manual restore"
    )
    assert json.loads(target.read_text()) == payload, (
        "the destination survived but does not hold the new payload"
    )
    # THE SAME AS A SUCCESS, and for the same reason. A completed copy leaves
    # the destination holding the new payload, so it now carries a credential
    # it need not have held before -- which is the state the narrowing refusal
    # above ranks as unacceptable. The clean success path skips the recovery
    # entirely and leaves 0600; an interrupt arriving one instant later must
    # not end MORE exposed than the run that finished.
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600, (
        "an interrupt after a COMPLETED copy left the destination wider than "
        "the identical successful run, publishing the new credential to every "
        "other uid"
    )


def test_an_interrupt_while_naming_the_survivor_does_not_strand_it(
    temp_home: Path, monkeypatch
):
    """`kept` and `def _unnarrow()` have to sit ABOVE the disown.

    Past `source, temp_path = temp_path, None` the outer cleanup no longer
    owns the temp and nothing prints where it went, so a signal there leaves
    the complete new payload -- for `~/.claude.json` a credential-bearing
    file -- stranded and unnamed. Building `kept` reads `PurePath.name`, a
    Python-level property whose RESUME is where a pending SIGINT lands, so
    the window is reachable rather than theoretical.
    """
    import pathlib as _pathlib

    from claude_swap import switcher as switcher_mod

    switcher = ClaudeAccountSwitcher()
    target = switcher.backup_dir / "sequence.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    switcher._write_json(target, {"activeAccountNumber": 1, "accounts": {}})

    def busy(*_a, **_kw):
        raise OSError(errno.EBUSY, "Device or resource busy")

    real_name = _pathlib.PurePath.name
    fired = {"named": False}

    def interrupt_when_the_temp_is_named(self):
        value = real_name.fget(self)
        if not fired["named"] and value.endswith(".tmp"):
            fired["named"] = True
            raise KeyboardInterrupt
        return value

    monkeypatch.setattr(switcher_mod, "replace_with_retry", busy)
    monkeypatch.setattr(
        _pathlib.PurePath, "name", property(interrupt_when_the_temp_is_named)
    )
    with pytest.raises(KeyboardInterrupt):
        switcher._write_json(target, {"primaryApiKey": "sk-ant-EXAMPLE"})
    monkeypatch.undo()

    assert fired["named"], "premise: the injected interrupt never fired"
    strays = list(target.parent.glob(f".{target.name}.*.tmp"))
    assert strays == [], (
        f"an interrupt while naming the survivor stranded {strays}, which "
        "holds the complete payload with nothing owning or printing it"
    )


def test_an_interrupt_at_the_digest_does_not_strand_the_temp(
    temp_home: Path, monkeypatch
):
    """The `before` digest has to sit above the disown too.

    Its own `except OSError` does not cover a signal or a MemoryError, and
    past `source, temp_path = temp_path, None` the outer cleanup no longer
    owns the name -- so the stray holds the COMPLETE new payload, which for
    `~/.claude.json` is a credential-bearing file, with nothing naming it.
    """
    if sys.platform == "win32":
        pytest.skip("POSIX modes only")
    from claude_swap import switcher as switcher_mod

    switcher = ClaudeAccountSwitcher()
    target = switcher.backup_dir / "sequence.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    switcher._write_json(target, {"activeAccountNumber": 1, "accounts": {}})

    def busy(*_a, **_kw):
        raise OSError(errno.EBUSY, "Device or resource busy")

    fired = {"digest": False}
    real_sha256 = switcher_mod.hashlib.sha256

    def interrupt_at_digest(*a, **kw):
        if not fired["digest"]:
            fired["digest"] = True
            raise KeyboardInterrupt
        return real_sha256(*a, **kw)

    monkeypatch.setattr(switcher_mod, "replace_with_retry", busy)
    monkeypatch.setattr(switcher_mod.hashlib, "sha256", interrupt_at_digest)
    with pytest.raises(KeyboardInterrupt):
        switcher._write_json(target, {"primaryApiKey": "sk-ant-EXAMPLE"})

    assert fired["digest"], "premise: the injected interrupt never fired"
    strays = list(target.parent.glob(f".{target.name}.*.tmp"))
    assert strays == [], f"an interrupt at the digest stranded {strays}"


def test_a_refused_mode_refuses_the_write_through(temp_home: Path, monkeypatch):
    """A destination that cannot be narrowed must not receive the payload.

    Nothing is committed at that point -- the chmod precedes the copy -- so
    refusing costs a switch, while continuing publishes a credential the
    destination did not previously hold, at a mode other users can read.
    """
    if sys.platform == "win32":
        pytest.skip("POSIX modes only")
    from claude_swap import switcher as switcher_mod

    switcher = ClaudeAccountSwitcher()
    target = switcher.backup_dir / "sequence.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    # Previous content carries NO credential, so "it was already exposed" is
    # false here: the write is what would expose one.
    switcher._write_json(target, {"activeAccountNumber": 1, "accounts": {}})
    os.chmod(target, 0o644)

    def busy(*_a, **_kw):
        raise OSError(errno.EBUSY, "Device or resource busy")

    def refused(*_a, **_kw):
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(switcher_mod, "replace_with_retry", busy)
    monkeypatch.setattr(switcher_mod.os, "chmod", refused)
    with pytest.raises(ConfigError):
        switcher._write_json(target, {"primaryApiKey": "sk-ant-EXAMPLE"})

    assert "primaryApiKey" not in target.read_text(encoding="utf-8"), (
        "the payload was written to a destination whose mode could not be narrowed"
    )


def test_the_salvage_copy_is_never_wider_than_0600(temp_home: Path, monkeypatch):
    """The unreadable-config copy carries the same payload and the same rule.

    `shutil.copy` creates at the umask default and the mode was narrowed after,
    so the copy held the content at 0644 for the width of that window -- and a
    refused chmod aborted the switch while LEAVING the 0644 copy on disk.
    """
    if sys.platform == "win32":
        pytest.skip("POSIX modes only")

    switcher = ClaudeAccountSwitcher()
    path = switcher.backup_dir / "sequence.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"primaryApiKey": "sk-ant-EXAMPLE"} not json', encoding="utf-8")
    # 0644 ON PURPOSE, and it is the realistic state: `shutil.copy` CARRIES the
    # source mode, so pinning the source at 0600 makes the copy 0600 whatever
    # the code does and the case proves nothing. A config another writer left
    # world-readable is exactly the one worth salvaging safely.
    os.chmod(path, 0o644)

    from claude_swap import switcher as switcher_mod

    seen: list[int] = []
    real_copy = switcher_mod.shutil.copyfile

    def sampling_copy(src, dst, *a, **kw):
        # Sampled between the copy and whatever narrows it: the widest point of
        # the window, with the payload fully on disk.
        result = real_copy(src, dst, *a, **kw)
        seen.append(os.stat(dst).st_mode & 0o777)
        return result

    monkeypatch.setattr(switcher_mod.shutil, "copyfile", sampling_copy)
    switcher._salvage_unreadable(path, emit_output=False, warnings_out=[])

    assert seen, "premise: no salvage file was created"
    assert all(m == 0o600 for m in seen), (
        f"the salvage copy existed at {[oct(m) for m in seen]} while it held the payload"
    )


def test_the_write_through_never_lands_the_secret_world_readable(
    temp_home: Path, monkeypatch
):
    """The chmod has to precede `copyfile`, not follow it.

    `copyfile` opens the destination `'wb'`: it truncates without touching the
    mode, so the payload lands at whatever Claude Code left there, which is
    0644. A copy that dies part-way never reaches a chmod placed after it, and
    a refused one leaves that mode permanently.

    What keeps this from being vacuous is the explicit 0644 below, not a
    umask: measured, the case reads the same at 022, 077 and 000.
    """
    if sys.platform == "win32":
        pytest.skip("POSIX modes only")
    from claude_swap import switcher as switcher_mod

    switcher = ClaudeAccountSwitcher()
    target = switcher.backup_dir / "sequence.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    switcher._write_json(target, {"activeAccountNumber": 1, "accounts": {}})
    os.chmod(target, 0o644)

    def busy(*_a, **_kw):
        raise OSError(errno.EBUSY, "Device or resource busy")

    seen: list[int] = []
    real_copyfile = switcher_mod.shutil.copyfile

    def dying_copy(src, dst, *a, **kw):
        real_copyfile(src, dst, *a, **kw)
        seen.append(os.stat(dst).st_mode & 0o777)
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(switcher_mod, "replace_with_retry", busy)
    monkeypatch.setattr(switcher_mod.shutil, "copyfile", dying_copy)
    with pytest.raises(ConfigError):
        switcher._write_json(target, {"primaryApiKey": "sk-ant-EXAMPLE"})

    assert seen, "premise: the write-through never ran"
    assert oct(seen[0]) == "0o600", (
        f"the payload landed at {oct(seen[0])} and the copy then died; a chmod "
        f"below the copy never runs at all"
    )


def test_the_temp_is_created_narrow_on_a_name_that_does_not_exist(
    temp_home: Path, monkeypatch
):
    """The 0600 in `os.open` is the ONLY thing narrowing a first-time temp.

    On a fresh name `O_CREAT` honours its mode argument, so nothing else is
    protecting the payload before `fchmod` runs -- and widening the literal
    leaves the whole suite green, because the sibling case pre-creates its temp
    at 0644 and measures a path where the argument is ignored.

    The umask IS load-bearing here, unlike in that sibling: 0o600 & ~0o077 is
    still 0o600, so a runner already at 077 cannot tell the literal apart from
    a wider one.
    """
    if sys.platform == "win32":
        pytest.skip("POSIX modes only")
    from claude_swap import switcher as switcher_mod

    switcher = ClaudeAccountSwitcher()
    target = switcher.backup_dir / "sequence.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    prefix = f".{target.name}.{os.getpid()}."
    assert not list(target.parent.glob(prefix + "*.tmp")), (
        "premise: the temp name must be fresh for O_CREAT to apply its mode"
    )

    seen: list[int] = []
    real_open = os.open

    def sampling_open(path, flags, *a, **kw):
        fd = real_open(path, flags, *a, **kw)
        # The writer draws a random suffix, so the name cannot be predicted.
        name = os.path.basename(os.fspath(path))
        if name.startswith(prefix) and name.endswith(".tmp"):
            seen.append(os.fstat(fd).st_mode & 0o777)
        return fd

    prev_umask = os.umask(0o000)
    try:
        monkeypatch.setattr(switcher_mod.os, "open", sampling_open)
        switcher._write_json(target, {"primaryApiKey": "sk-ant-EXAMPLE"})
    finally:
        os.umask(prev_umask)

    assert seen, "premise: the temp was never created through os.open"
    assert seen[0] == 0o600, (
        f"the temp was created at {oct(seen[0])}; on a fresh name the O_CREAT "
        f"mode is what narrows it"
    )


def test_the_temp_is_never_world_readable_while_it_holds_the_payload(
    temp_home: Path, monkeypatch
):
    """`~/.claude.json` routes through this writer and can carry
    `primaryApiKey` plus inline MCP credentials.

    The mode is sampled during the read-back — the widest point of the window,
    where the payload is fully on disk and the publish has not happened.

    The temp is WIDENED to 0644 the instant it is created, which is the only
    state `fchmod` covers and the one a reused name produces: the open carries
    no `O_EXCL`, so an existing name is reopened and `O_CREAT`'s mode argument
    is ignored. On a name that does not exist the open already yields 0600
    under any umask this would run with, and the case passes with the `fchmod`
    deleted. It widens rather than pre-creating because the writer's name now
    carries a random suffix and cannot be predicted.
    """
    if sys.platform == "win32":
        pytest.skip("POSIX modes only")
    from claude_swap import switcher as switcher_mod

    switcher = ClaudeAccountSwitcher()
    target = switcher.backup_dir / "sequence.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    prefix = f".{target.name}.{os.getpid()}."
    drawn: list[Path] = []
    real_open = os.open

    def widening_open(path, flags, *a, **kw):
        fd = real_open(path, flags, *a, **kw)
        name = os.path.basename(os.fspath(path))
        if name.startswith(prefix) and name.endswith(".tmp"):
            # 0644 the instant the file exists: the state a reused name leaves,
            # reached without predicting a name that now carries randomness.
            os.chmod(path, 0o644)
            drawn.append(Path(path))
        return fd

    seen: list[int] = []
    real_loads = json.loads

    def spy(s, *a, **kw):
        if drawn and drawn[0].exists():
            seen.append(drawn[0].stat().st_mode & 0o777)
        return real_loads(s, *a, **kw)

    monkeypatch.setattr(switcher_mod.os, "open", widening_open)
    monkeypatch.setattr(switcher_mod.json, "loads", spy)
    switcher._write_json(target, {"primaryApiKey": "sk-ant-EXAMPLE"})

    assert seen, "premise: the read-back never sampled the temp"
    assert [oct(m) for m in seen] == ["0o600"] * len(seen)


# `test_a_refused_mode_carry_does_not_fail_a_publish_that_landed` lived here. It
# asserted that a refused chmod warns rather than raises, on the reasoning that
# "once the bytes are through the mount the write is committed" -- true while the
# chmod sat BELOW the copy. It sits above it now, so a refusal happens with
# nothing committed and the rollback it feared cannot occur. The contract it
# guarded moved to `test_a_refused_mode_refuses_the_write_through`, which asserts
# the destination does not receive the payload at all.


def test_a_failed_write_through_names_the_copy_it_kept(temp_home: Path, monkeypatch):
    """A truncated destination is recoverable only if the user is told where.

    The write-through is the one publish that is not atomic, so a failure
    part-way leaves the destination short and the temp holding the only
    complete content. `_salvage_unreadable` in this same file sets the
    standard: raise with the path, do not leave a bare errno.
    """
    from claude_swap import switcher as switcher_mod

    switcher = ClaudeAccountSwitcher()
    target = switcher.backup_dir / "sequence.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    switcher._write_json(target, {"activeAccountNumber": 1, "accounts": {}})

    def busy(*_a, **_kw):
        raise OSError(errno.EBUSY, "Device or resource busy")

    def truncating_copy(src, dst, *a, **kw):
        Path(dst).write_text('{"activeAcc', encoding="utf-8")
        raise OSError("injected: no space left on device")

    monkeypatch.setattr(switcher_mod, "replace_with_retry", busy)
    monkeypatch.setattr(switcher_mod.shutil, "copyfile", truncating_copy)

    with pytest.raises(ConfigError) as exc:
        switcher._write_json(target, {"activeAccountNumber": 2, "accounts": {}})

    strays = list(target.parent.glob(f".{target.name}.*.tmp"))
    assert len(strays) == 1, "the only complete copy was removed"
    assert json.loads(strays[0].read_text(encoding="utf-8"))["activeAccountNumber"] == 2
    assert strays[0].name in str(exc.value), (
        f"the surviving copy was not named: {exc.value}"
    )


def test_a_ctrl_c_mid_write_through_still_names_the_copy_it_kept(
    temp_home: Path, monkeypatch, capsys
):
    """The interrupt this whole change is about, at the one non-atomic publish.

    `except OSError` around the copy is the same too-narrow handler the rest
    of this change exists to widen: a Ctrl-C leaves the destination short and
    the temp holding the only complete content, and nothing names it. The
    message cannot ride an exception here, because the interrupt has to stay
    an interrupt -- so it goes to stderr, which the `--json` envelope on
    stdout does not share.
    """
    from claude_swap import switcher as switcher_mod

    switcher = ClaudeAccountSwitcher()
    target = switcher.backup_dir / "sequence.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    switcher._write_json(target, {"activeAccountNumber": 1, "accounts": {}})

    def busy(*_a, **_kw):
        raise OSError(errno.EBUSY, "Device or resource busy")

    def interrupted_copy(src, dst, *a, **kw):
        Path(dst).write_text('{"activeAcc', encoding="utf-8")
        raise KeyboardInterrupt

    monkeypatch.setattr(switcher_mod, "replace_with_retry", busy)
    monkeypatch.setattr(switcher_mod.shutil, "copyfile", interrupted_copy)

    with pytest.raises(KeyboardInterrupt):
        switcher._write_json(target, {"activeAccountNumber": 2, "accounts": {}})

    strays = list(target.parent.glob(f".{target.name}.*.tmp"))
    assert len(strays) == 1, "the only complete copy was removed"
    assert json.loads(strays[0].read_text(encoding="utf-8"))["activeAccountNumber"] == 2

    out, err = capsys.readouterr()
    assert strays[0].name in err, f"the surviving copy was not named: {err!r}"
    assert out == "", f"a machine-readable channel was written to: {out!r}"


# EVERY atomic writer, not the three the first pass reached. A test named "no
# writer" needs the whole population behind it; parametrised over three of
# eight it was a true statement about a sample and a false one about its name.
_ALL_ATOMIC_WRITERS = [
    "settings", "mappings", "session",
    "global_config", "active_creds", "backup_enc", "write_json", "plist",
    # ENUMERATED BY STRUCTURE, not by memory: every site that publishes a
    # temp through `replace_with_retry`/`os.replace`. These two were missed by
    # the first sweep, and `transfer` writes the export payload -- live OAuth
    # refresh tokens, into a directory the user chose.
    "migrations", "transfer",
]


def _writer_site(site: str, temp_home: Path, tmp_path: Path):
    """-> (dir the temp is drawn in, a call that publishes, a stray temp name,
    the module whose `os` the writer uses).

    The stray is what a predecessor killed by a SIGKILL would have left: the
    same pid-derived name, without whatever randomness the writer adds.
    """
    pid = os.getpid()
    d = tmp_path / "d"
    d.mkdir(exist_ok=True)
    if site == "settings":
        from claude_swap import settings as mod
        from claude_swap.settings import atomic_write_json

        t = d / "s.json"
        return (d, (lambda: atomic_write_json(t, {"a": 1})),
                d / f".{t.name}.{pid}.tmp", mod)
    if site == "mappings":
        from claude_swap import mappings as mod
        from claude_swap.mappings import MappingStore

        store = MappingStore(d)
        return (d, (lambda: store._write({})),
                d / f".mappings-{pid}.tmp", mod)
    if site == "session":
        from claude_swap import session as mod
        from claude_swap.session import SessionManager

        mgr = SessionManager(ClaudeAccountSwitcher())
        return (d, (lambda: mgr._write_manifest(d / "m.json", [])),
                d / f".cswap-shared-{pid}.tmp", mod)
    if site == "plist":
        from claude_swap import menubar

        exe = d / "bin" / "python3"
        exe.parent.mkdir(parents=True, exist_ok=True)
        return (exe.parent,
                (lambda: menubar.ensure_notification_identity(
                    exe, platform="darwin")),
                exe.parent / f"Info.plist.{pid}.tmp", menubar)
    if site == "write_json":
        from claude_swap import switcher as mod

        sw = ClaudeAccountSwitcher()
        t = d / "seq.json"
        return (d, (lambda: sw._write_json(t, {"a": 1})),
                d / f".{t.name}.{pid}.tmp", mod)

    if site == "migrations":
        from claude_swap import migrations as mod

        sw = ClaudeAccountSwitcher()
        sw._setup_directories()
        t = mod._state_path(sw)
        t.parent.mkdir(parents=True, exist_ok=True)
        return (t.parent, (lambda: mod._mark_applied(sw, "probe")),
                t.parent / f".{t.name}.{pid}.tmp", mod)
    if site == "transfer":
        from claude_swap import transfer as mod

        t = d / "out.cswap"
        return (d, (lambda: mod._atomic_write_file(t, "{}")),
                d / f".{t.name}.{pid}.tmp", mod)

    from claude_swap import credentials as _cred_mod
    store = ClaudeAccountSwitcher()._store
    if site == "global_config":
        from claude_swap.credentials import get_global_config_path

        cfg = get_global_config_path()
        cfg.parent.mkdir(parents=True, exist_ok=True)
        return (cfg.parent,
                (lambda: store._update_global_config(
                    lambda c: c.__setitem__("primaryApiKey", "sk-ant-REDACTED"))),
                cfg.parent / f".{cfg.name}.{pid}.tmp", _cred_mod)
    if site == "active_creds":
        from claude_swap.credentials import get_claude_config_home

        cd = get_claude_config_home()
        cd.mkdir(parents=True, exist_ok=True)
        return (cd,
                (lambda: store._write_active_credentials_file("{}")),
                cd / f".credentials.json.{pid}.tmp", _cred_mod)
    assert site == "backup_enc", site
    cd = store._host.credentials_dir
    cd.mkdir(parents=True, exist_ok=True)
    t = cd / "1-probe.enc"
    return (cd, (lambda: store._atomic_b64_write(t, "{}")),
            cd / f".{t.name}.{pid}.tmp", _cred_mod)


@pytest.mark.parametrize("site", _ALL_ATOMIC_WRITERS)
def test_no_writer_mints_its_temp_name_inside_the_syscall(
    temp_home: Path, monkeypatch, tmp_path: Path, site: str
):
    """An interrupt inside the CREATE must still leave a name to unlink.

    `tempfile.mkstemp` picks the name internally and opens the file before it
    returns, so a `KeyboardInterrupt` in that window leaves a temp whose name
    never reached the caller — no handler can remove what it cannot name. The
    roster writer does not have this window: it computes the name first and
    calls `os.open` inside its own guard.

    Measured before the fix, same injection at each site:
    `settings.atomic_write_json` -> `tmp*.tmp`, `mappings._write` ->
    `.mappings-*.tmp`, `session._write_manifest` -> `.cswap-shared-*.tmp`;
    the roster writer -> nothing.
    """
    import tempfile as tempfile_mod

    def exploding(*_a, **_kw):
        raise AssertionError(
            "the temp name is minted inside mkstemp, so an interrupt there "
            "strands a file nothing can name"
        )

    monkeypatch.setattr(tempfile_mod, "mkstemp", exploding)

    d, write, _stray, _mod = _writer_site(site, temp_home, tmp_path)
    write()
    assert list(d.glob(".*tmp")) == [] and list(d.glob("*tmp*")) == [], (
        "a temp survived a completed write"
    )


def test_an_interrupted_salvage_leaves_no_partial_copy(
    temp_home: Path, monkeypatch, capsys
):
    """The `.unreadable-` name promises the bytes survived. A partial breaks it.

    `except OSError` does not catch `KeyboardInterrupt`, so a Ctrl-C inside
    `copyfile` left a truncated copy of the credential under a name a later
    restore trusts — and printed, logged, and returned nothing about it.
    Measured before the fix: 29 of 55 bytes, mode 0600, `warnings_out == []`.
    """
    from claude_swap import switcher as switcher_mod

    switcher = ClaudeAccountSwitcher()
    path = temp_home / ".claude.json"
    path.write_text('{"primaryApiKey": "sk-ant-REDACTED", "projects": {}}')

    def truncating_copy(src, dst, *_a, **_kw):
        Path(dst).write_text('{"primaryApiKey": "sk-ant-RED')
        raise KeyboardInterrupt

    monkeypatch.setattr(switcher_mod.shutil, "copyfile", truncating_copy)
    warnings_out: list[str] = []
    with pytest.raises(KeyboardInterrupt):
        switcher._salvage_unreadable(path, True, warnings_out)

    strays = list(path.parent.glob(f"{path.name}.unreadable-*"))
    assert strays == [], (
        f"a partial salvage survived as {[s.name for s in strays]} — the name "
        "says the bytes are there and they are not"
    )


def test_a_signal_between_the_create_and_the_record_strands_nothing(
    temp_home: Path, monkeypatch
):
    """A record made AFTER the call misses a file that exists.

    `created = True` sat below `os.open`, so a signal delivered in that gap
    left a 0-byte file under the `.unreadable-` name -- which the docstring
    calls a promise that the bytes survived -- with `created` False, nothing
    to remove it and nothing to announce it. `_stage_overlap_material` two
    thousand lines down already argues this ordering for its own create.
    """
    from claude_swap import switcher as switcher_mod

    switcher = ClaudeAccountSwitcher()
    path = temp_home / ".claude.json"
    path.write_text('{"primaryApiKey": "sk-ant-REDACTED", "projects": {}}')

    real_open = switcher_mod.os.open

    def interrupted_open(target, *a, **kw):
        fd = real_open(target, *a, **kw)   # the file now exists
        os.close(fd)
        raise KeyboardInterrupt("between the create and the record")

    monkeypatch.setattr(switcher_mod.os, "open", interrupted_open)
    with pytest.raises(KeyboardInterrupt):
        switcher._salvage_unreadable(path, False, [])

    strays = list(path.parent.glob(f"{path.name}.unreadable-*"))
    assert strays == [], (
        f"a 0-byte salvage survived as {[s.name for s in strays]} — the name "
        "says the bytes are there and the file is empty"
    )


def test_a_lost_salvage_race_does_not_delete_the_other_process_copy(
    temp_home: Path, monkeypatch
):
    """`exists()` is a check, not a claim on the name.

    Two switches hitting an unreadable config in the same second draw the same
    `.unreadable-<epoch>` name. The `while salvage.exists()` loop settles who
    goes first only until it returns; the winner creates the file in the gap,
    our `O_EXCL` fails, and the cleanup then unlinks THEIR complete copy of the
    credential — the one thing on disk that still had it.
    """
    from claude_swap import switcher as switcher_mod

    switcher = ClaudeAccountSwitcher()
    path = temp_home / ".claude.json"
    path.write_text('{"primaryApiKey": "sk-ant-REDACTED", "projects": {}}')

    real_open = switcher_mod.os.open
    theirs: list[Path] = []

    def losing_open(target, *a, **kw):
        # The other process wins the name between `exists()` and here.
        p = Path(target)
        p.write_text("their complete copy")
        theirs.append(p)
        raise FileExistsError(errno.EEXIST, "File exists")

    monkeypatch.setattr(switcher_mod.os, "open", losing_open)
    with pytest.raises(SwitchError):
        switcher._salvage_unreadable(path, False, [])
    monkeypatch.setattr(switcher_mod.os, "open", real_open)

    assert theirs and theirs[0].exists(), (
        f"the loser deleted the winner's salvage at {theirs[0].name}"
    )
    assert theirs[0].read_text() == "their complete copy"


def test_a_salvage_that_was_never_created_is_not_announced_as_a_partial(
    temp_home: Path, monkeypatch
):
    """A message naming a file that is not there sends the user after nothing.

    When the create itself fails there is no copy at all, but `unlink` then
    raises `FileNotFoundError` — an `OSError` — and the handler read that as
    "the partial could not be removed" and named it in the error the user gets.
    """
    from claude_swap import switcher as switcher_mod

    switcher = ClaudeAccountSwitcher()
    path = temp_home / ".claude.json"
    path.write_text('{"primaryApiKey": "sk-ant-REDACTED", "projects": {}}')

    def refusing_open(*_a, **_kw):
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(switcher_mod.os, "open", refusing_open)
    with pytest.raises(SwitchError) as excinfo:
        switcher._salvage_unreadable(path, False, [])

    assert list(path.parent.glob(f"{path.name}.unreadable-*")) == [], (
        "premise: this case is about a salvage that does not exist"
    )
    assert "PARTIAL" not in str(excinfo.value), (
        f"named a partial copy that was never created: {excinfo.value}"
    )


def test_a_failed_write_through_does_not_keep_the_narrowed_mode(
    temp_home: Path, monkeypatch
):
    """The 0600 is a committed side effect when the copy after it fails.

    The write-through path narrows the destination BEFORE the copy, because
    `copyfile` truncates without touching the mode and a chmod after it would
    publish the payload at whatever mode was there. When the copy then fails
    the narrowing stands, the old mode was never captured, and nothing can put
    it back — on the deployment this branch exists for (a bind-mounted
    `~/.claude.json` at 0644 so another uid can read it) a full disk narrows
    the host file permanently and reports only the copy error.

    `copyfile` has already truncated by then, so the destination is emptied
    before the mode goes back: an empty file at the old mode leaks nothing,
    and the complete content is at the temp the message names.
    """
    from claude_swap import switcher as switcher_mod

    import stat as stat_mod

    switcher = ClaudeAccountSwitcher()
    path = temp_home / ".claude.json"
    path.write_text('{"activeAccountNumber": 1}')
    os.chmod(path, 0o644)
    # READ IT BACK rather than assuming 0644 landed. Windows honours only the
    # write bit, so the same chmod yields 0666 there -- and the production
    # narrowing is POSIX-only, so on Windows the property under test is that
    # nothing moved at all. Comparing against what the file actually holds says
    # the same thing on both.
    before = stat_mod.S_IMODE(path.stat().st_mode)

    def busy(*_a, **_kw):
        raise OSError(errno.EBUSY, "Device or resource busy")

    def out_of_space(src, dst, *_a, **_kw):
        Path(dst).write_text("")          # copyfile truncates first
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(switcher_mod, "replace_with_retry", busy)
    monkeypatch.setattr(switcher_mod.shutil, "copyfile", out_of_space)
    with pytest.raises(ConfigError):
        switcher._write_json(path, {"activeAccountNumber": 2})

    mode = stat_mod.S_IMODE(path.stat().st_mode)
    assert mode == before, (
        f"the destination went {oct(before)} -> {oct(mode)} — the narrowing "
        "outlived the write it was for, and nothing recorded what to restore"
    )


def test_a_copy_that_never_opened_the_destination_leaves_it_alone(
    temp_home: Path, monkeypatch
):
    """`_unnarrow` may only empty what `copyfile` already truncated.

    `shutil.copyfile` raises before it opens the destination `'wb'` on four
    paths — `SameFileError`, `SpecialFileError`, any failure opening the
    SOURCE, and a signal anywhere in that prologue. In every one of them the
    destination still holds its original bytes, and emptying it destroys a
    live config the write never touched. At the merge base the copy fallback
    left it fully intact, so undoing the narrowing this way is a regression
    the narrowing-fix introduced.
    """
    import stat as stat_mod
    from claude_swap import switcher as switcher_mod

    switcher = ClaudeAccountSwitcher()
    path = temp_home / ".claude.json"
    original = '{"activeAccountNumber": 1}'
    path.write_text(original)
    os.chmod(path, 0o644)
    before = stat_mod.S_IMODE(path.stat().st_mode)

    def busy(*_a, **_kw):
        raise OSError(errno.EBUSY, "Device or resource busy")

    def never_opened_dst(_src, _dst, *_a, **_kw):
        # The source could not be read; `_dst` was never touched.
        raise OSError(errno.EMFILE, "Too many open files")

    monkeypatch.setattr(switcher_mod, "replace_with_retry", busy)
    monkeypatch.setattr(switcher_mod.shutil, "copyfile", never_opened_dst)
    with pytest.raises(ConfigError):
        switcher._write_json(path, {"activeAccountNumber": 2})

    assert path.read_text() == original, (
        f"the destination was emptied by a copy that never opened it: "
        f"{path.read_text()!r}"
    )
    assert stat_mod.S_IMODE(path.stat().st_mode) == before, (
        "the narrowing outlived the write it was for"
    )


@pytest.mark.parametrize("site", _ALL_ATOMIC_WRITERS)
def test_a_stranded_temp_from_a_recycled_pid_does_not_wedge_the_write(
    temp_home: Path, tmp_path: Path, site: str
):
    """`O_EXCL` on a pid-derived name is not the collision safety mkstemp gave.

    `mkstemp` RETRIES on EEXIST; `O_EXCL` on a fixed name can only fail. A
    SIGKILL (a container stop, an OOM) strands `.mappings-<pid>.tmp`, a later
    process draws the same pid, and the write dies — and the handler then
    unlinks a file it did not create, which is exactly what
    `_stage_overlap_material` refuses to do.

    A random suffix keeps I1's property (the name is known before the file
    exists) AND mkstemp's collision profile.
    """
    _d, write, stray, _mod = _writer_site(site, temp_home, tmp_path)
    stray.write_text("a predecessor died holding this")
    write()
    assert stray.read_text() == "a predecessor died holding this", (
        "the writer deleted a stranded file it did not create"
    )


#: The plist carries no credential, so its temp is created at the ordinary
#: mode. Everything else on this roster holds one at some point.
_WIDE_BY_DESIGN = {"plist"}


@pytest.mark.parametrize("site", _ALL_ATOMIC_WRITERS)
def test_the_temp_is_created_narrow_at_every_writer(
    temp_home: Path, tmp_path: Path, monkeypatch, site: str
):
    """The 0600 literal was pinned at TWO of the ten writers.

    `credentials.py` has no `os.fchmod` at all, so on those three the literal
    on `os.open` is the ONLY thing narrowing a temp that holds a live OAuth
    token for the whole write window -- and widening every create to 0o666
    failed just two cases, one of them pre-existing. On NINE of the ten the
    name is fresh (`O_EXCL`), so nothing else can have set the mode. The
    tenth opens `O_TRUNC` and would reopen an existing name with the mode
    argument ignored, which is why that one carries an `os.fchmod` as well.
    """
    if sys.platform == "win32":
        pytest.skip("POSIX modes only")
    d, write, _stray, mod = _writer_site(site, temp_home, tmp_path)
    seen: list[int] = []
    real_open = os.open

    def sampling_open(path, flags, *a, **kw):
        fd = real_open(path, flags, *a, **kw)
        name = os.path.basename(os.fspath(path))
        if name.endswith(".tmp"):
            seen.append(os.fstat(fd).st_mode & 0o777)
        return fd

    prev_umask = os.umask(0o000)          # permissive: 0o644 if nothing narrows
    try:
        monkeypatch.setattr(mod.os, "open", sampling_open)
        write()
    finally:
        os.umask(prev_umask)

    assert seen, "premise: no temp was created through os.open"
    ceiling = 0o644 if site in _WIDE_BY_DESIGN else 0o600
    wide = [oct(m) for m in seen if m & ~ceiling]
    assert wide == [], (
        f"{site}'s temp was created at {wide} against a {oct(ceiling)} "
        "ceiling — the payload is readable by another uid for the whole "
        "write window"
    )


@pytest.mark.parametrize("site", _ALL_ATOMIC_WRITERS)
def test_an_interrupt_at_the_create_strands_nothing(
    temp_home: Path, tmp_path: Path, monkeypatch, site: str
):
    """The BEHAVIOUR I1 is about, which its sibling case cannot see.

    `test_no_writer_mints_its_temp_name_inside_the_syscall` injects no
    interrupt: it makes `mkstemp` explode and calls each writer on its SUCCESS
    path, so it only asserts "this module does not call mkstemp". Measured —
    with `os.open` moved back outside the guard (the name still computed
    first, so that premise holds) it stays green on a tree that strands the
    temp. This one interrupts at the create and looks at the directory.
    """
    d, write, _stray, mod = _writer_site(site, temp_home, tmp_path)
    before = set(os.listdir(d))

    real_open = os.open

    def exploding(path, *a, **k):
        fd = real_open(path, *a, **k)   # the file now exists
        os.close(fd)
        raise KeyboardInterrupt("inside the create")

    monkeypatch.setattr(mod.os, "open", exploding)
    with pytest.raises(KeyboardInterrupt):
        write()

    strays = sorted(set(os.listdir(d)) - before)
    assert strays == [], f"an interrupt at the create left {strays}"


def test_a_partial_of_the_SAME_LENGTH_is_not_left_world_readable(
    temp_home: Path, monkeypatch
):
    """The mtime half of the pair, which nothing pinned.

    `_unnarrow` asks `(st_size, st_mtime_ns) != before`. Freezing
    `st_mtime_ns` degrades that back to the size proxy round 5 replaced, and
    the whole write-through group stays green: every case there changes the
    length, so the size half alone answers them all. This one does not —
    the partial is written at EXACTLY the prior byte count, so only the
    mtime can say the file was touched.
    """
    if sys.platform == "win32":
        pytest.skip("POSIX modes only")
    from claude_swap import switcher as switcher_mod

    switcher = ClaudeAccountSwitcher()
    path = temp_home / ".claude.json"
    original = '{"a": "000000000000000000000000000"}'
    path.write_text(original)
    os.chmod(path, 0o644)

    partial = '{"primaryApiKey": "sk-ant-SECRET-P"}'
    assert len(partial) == len(original), (
        "premise: the partial must be exactly the prior length, or the size "
        "half answers this and the mtime half is untested"
    )

    def same_length_partial(src, dst, *_a, **_kw):
        Path(dst).write_text(partial)
        raise OSError(errno.EIO, "the mount went away mid-copy")

    monkeypatch.setattr(switcher_mod.shutil, "copyfile", same_length_partial)
    monkeypatch.setattr(
        switcher_mod, "replace_with_retry",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError(errno.EBUSY, "bind mount")))

    with pytest.raises(ConfigError):
        switcher._write_json(path, {"primaryApiKey": "sk-ant-SECRET-PAYLOAD"})

    left = path.read_text()
    assert "SECRET" not in left, (
        f"a same-length partial credential survived at "
        f"mode {oct(path.stat().st_mode & 0o777)}: {left[:40]!r}"
    )


def test_a_partial_larger_than_the_original_is_not_left_world_readable(
    temp_home: Path, monkeypatch
):
    """The size proxy reads the ORDINARY direction as "never touched".

    `_unnarrow` inferred "the copy truncated it" from `st_size < before_size`.
    The mainline switch splices `oauthAccount` INTO an existing config, so the
    new payload is larger — and a copy dying past the old size then reads as
    untouched, leaving a partial `primaryApiKey` at the destination's old
    world-readable mode. That is the exposure the narrowing exists to prevent,
    re-opened by the guard added to stop it emptying an untouched file.

    Measured before this fix: mode 0o644 -> 0o644 with the secret on disk.
    """
    import stat as stat_mod
    from claude_swap import switcher as switcher_mod

    switcher = ClaudeAccountSwitcher()
    path = temp_home / ".claude.json"
    path.write_text('{"a": 1}')            # 8 bytes
    os.chmod(path, 0o644)

    def busy(*_a, **_kw):
        raise OSError(errno.EBUSY, "Device or resource busy")

    def dies_past_the_old_size(_src, dst, *_a, **_kw):
        # `copyfile` opens 'wb' (truncating) and then writes; it died after
        # writing MORE than the original held.
        Path(dst).write_text('{"primaryApiKey": "sk-ant-SECRET-PARTIAL')
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(switcher_mod, "replace_with_retry", busy)
    monkeypatch.setattr(switcher_mod.shutil, "copyfile", dies_past_the_old_size)
    with pytest.raises(ConfigError):
        switcher._write_json(path, {"activeAccountNumber": 2, "pad": "x" * 200})

    left = path.read_text()
    mode = stat_mod.S_IMODE(path.stat().st_mode)
    assert "SECRET" not in left, (
        f"a partial credential survived at mode {oct(mode)}: {left[:40]!r}"
    )


def test_a_partial_copy_is_emptied_not_left_at_the_prior_mode(
    temp_home: Path, monkeypatch
):
    """The state the recovery exists for, and the one it skipped.

    CPython's `copyfile` opens the destination `'wb'` -- truncating it --
    before reading a single source byte, so a copy that dies mid-stream leaves
    a PREFIX of the new payload behind. For `~/.claude.json` that prefix is a
    truncated credential, and the mode restore then puts the permissive prior
    mode back over it.

    It was skipped whenever the digest of what we published was unavailable,
    which the recovery used to obtain by re-READING the temp -- unreadable on
    exactly the population that reaches the recovery through a source-side
    error. Taking the digest from `content` removes that state entirely.
    """
    if sys.platform == "win32":
        pytest.skip("POSIX modes only")
    import shutil as _shutil

    from claude_swap import switcher as switcher_mod

    switcher = ClaudeAccountSwitcher()
    target = switcher.backup_dir / "sequence.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    switcher._write_json(target, {"activeAccountNumber": 1, "accounts": {}})
    os.chmod(target, 0o644)
    original = target.read_bytes()

    def busy(*_a, **_kw):
        raise OSError(errno.EBUSY, "Device or resource busy")

    fired = {"copy": False}

    def truncating_partial(src, dst, **_kw):
        fired["copy"] = True
        # EXACTLY CPython's order: the destination is opened 'wb' before the
        # source is read, so the truncation has already happened.
        with open(src, "rb") as s, open(dst, "wb") as d:
            d.write(s.read(18))
        raise OSError(errno.EIO, "injected mid-copy")

    real_read_bytes = Path.read_bytes

    def is_the_temp(name: str) -> bool:
        return name.startswith(f".{target.name}.") and name.endswith(".tmp")

    def unreadable_temp(self):
        if is_the_temp(self.name):
            raise OSError(errno.EIO, "the temp's medium answered EIO")
        return real_read_bytes(self)

    # THE MEDIUM THAT USED TO DISARM THIS, kept so the case still fails on a
    # WHOLE revert to reading the digest out of the temp. It does NOT catch
    # the half of that revert which re-points the digest and leaves `touched`
    # alone -- measured, 27 passed -- so it is INERT on correct code and
    # partial against a wrong one. Not a premise either way; the check below
    # is what keeps it from going stale in silence.
    #
    # DERIVED FROM PRODUCTION, not from the test's own spelling. Handing the
    # predicate a name this file constructs asks whether it matches itself,
    # which is true however the temp is really named -- so it would report a
    # healthy instrument after the writer moved to `tmpXXXXXXXX.tmp`.
    opened: list[str] = []
    real_os_open = os.open

    def recording_open(path, *a, **k):
        if not isinstance(path, int):
            opened.append(os.path.basename(os.fspath(path)))
        return real_os_open(path, *a, **k)

    monkeypatch.setattr(switcher_mod.os, "open", recording_open)
    monkeypatch.setattr(switcher_mod, "replace_with_retry", busy)
    monkeypatch.setattr(_shutil, "copyfile", truncating_partial)
    monkeypatch.setattr(Path, "read_bytes", unreadable_temp)
    with pytest.raises(ConfigError):
        switcher._write_json(target, {"primaryApiKey": "sk-ant-EXAMPLE-SECRET"})
    monkeypatch.undo()

    assert fired["copy"], "premise: the partial copy never ran"
    assert any(is_the_temp(n) for n in opened), (
        f"the injection matches none of the names the writer actually opened "
        f"({opened}) — it is inert for a reason that has nothing to do with "
        "the fix, and this case has stopped guarding the revert"
    )
    now = target.read_bytes()
    assert now != original, (
        "premise: the copy did not truncate, so there is no partial to judge"
    )
    assert now == b"", (
        f"a partial destination survived: {now[:40]!r} at mode "
        f"{oct(os.stat(target).st_mode & 0o777)}"
    )


def test_an_unreadable_destination_before_the_copy_is_left_alone(
    temp_home: Path, monkeypatch
):
    """THE `before is not None` TERM, which nothing else reaches.

    A partial copy is normally emptied. When the destination could not be READ
    before the copy, nothing here can tell that partial from bytes that were
    already there, so the conservative answer is to leave it: the alternative
    destroys a config this write never finished reaching.

    Measured before this case existed: deleting the term left the file
    byte-identical at 547 passed.
    """
    if sys.platform == "win32":
        pytest.skip("POSIX modes only")
    import shutil as _shutil

    from claude_swap import switcher as switcher_mod

    switcher = ClaudeAccountSwitcher()
    target = switcher.backup_dir / "sequence.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    switcher._write_json(target, {"activeAccountNumber": 1, "accounts": {}})
    os.chmod(target, 0o644)

    def busy(*_a, **_kw):
        raise OSError(errno.EBUSY, "Device or resource busy")

    fired = {"copy": False, "before": False}
    real_read_bytes = Path.read_bytes

    def unreadable_before(self):
        # ONLY the pre-copy read. The recovery reads the destination a second
        # time afterwards, and that one must succeed or the case proves
        # nothing about the term -- an early `return` would leave the file
        # alone for a completely different reason.
        if not fired["copy"] and os.fspath(self) == os.fspath(target):
            fired["before"] = True
            raise OSError(errno.EIO, "injected pre-copy read")
        return real_read_bytes(self)

    def truncating_partial(src, dst, **_kw):
        fired["copy"] = True
        with open(src, "rb") as s, open(dst, "wb") as d:
            d.write(s.read(18))
        raise OSError(errno.EIO, "injected mid-copy")

    monkeypatch.setattr(switcher_mod, "replace_with_retry", busy)
    monkeypatch.setattr(_shutil, "copyfile", truncating_partial)
    monkeypatch.setattr(Path, "read_bytes", unreadable_before)
    with pytest.raises(ConfigError):
        switcher._write_json(target, {"primaryApiKey": "sk-ant-EXAMPLE-SECRET"})
    monkeypatch.undo()

    assert fired["before"], "premise: the pre-copy read never failed"
    assert fired["copy"], "premise: the partial copy never ran"
    assert target.read_bytes() != b"", (
        "the recovery emptied a destination it could not read beforehand — it "
        "cannot tell a partial from what was already there, and emptying is "
        "the destructive answer to that question"
    )
