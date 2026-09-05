"""Tests for the ClaudeAccountSwitcher class."""

from __future__ import annotations

import base64
import contextlib
import errno
import json
import os
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
from claude_swap.models import Platform, normalize_alias
from claude_swap.paths import get_backup_root, get_credentials_path
from claude_swap.session import mark_session_stale
from claude_swap.credentials import ActiveCredentials
from claude_swap.switcher import (
    CLAUDE_CODE_KEYCHAIN_SERVICE,
    ClaudeAccountSwitcher,
    SECURITY_SERVICE,
    SETUP_TOKEN_SCOPES,
    _format_usage_lines,
    switch_off_at_limit_account,
)
from claude_swap import macos_keychain as _kc
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.paths import get_global_config_path
from claude_swap.usage_store import SERVE_TTL_S, _row_eligible
from claude_swap.json_output import USAGE_RELOGIN_REQUIRED


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
        # The state, and what to do about it. It used to print the bare
        # words "no credentials", which name the problem and stop there —
        # and the fix people reach for (/login on whatever account is
        # active) writes the login to the wrong slot.
        assert "no stored login" in output
        assert "switch here" in output

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


@pytest.fixture
def _ex_reads_what_the_plain_reader_returns(monkeypatch):
    """Keep the two backup-read seams agreeing for tests that patch one.

    `_fetch_active_usage` reads the slot backup through
    `_read_account_credentials_ex`, for the unreadable verdict the plain
    reader throws away. These classes stage a backup by patching the PLAIN
    reader, so without this their staging is bypassed: measured, 24 of them
    fail outright and 35 pass, some vacuously against an empty store. `(value,
    False)` is what each already assumes by supplying concrete bytes; a test
    about the UNREADABLE verdict patches `_ex` on the instance, which wins.
    """
    monkeypatch.setattr(
        ClaudeAccountSwitcher,
        "_read_account_credentials_ex",
        lambda self, num, email: (self._read_account_credentials(num, email), False),
    )


@pytest.mark.usefixtures("_ex_reads_what_the_plain_reader_returns")
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

    # Live bytes belonging to no managed slot: unattributable on every path
    # that reads them, which is the precondition all four users share.
    _FOREIGN_LIVE = json.dumps({
        "claudeAiOauth": {
            "accessToken": "sk-x", "refreshToken": "rt-foreign",
            "expiresAt": 1000,
        },
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

        with patch.object(switcher, "_read_credentials", return_value=self._FOREIGN_LIVE), \
             patch.object(
                 switcher, "_read_account_credentials", return_value=dead_backup
             ), \
             patch("claude_swap.oauth.try_refresh_oauth_credentials") as mock_refresh, \
             patch("claude_swap.oauth.try_fetch_usage_for_account") as mock_fetch:
            result = switcher._fetch_active_usage("1", "test@example.com", self._FOREIGN_LIVE)

        assert result.sentinel == USAGE_TOKEN_EXPIRED
        mock_refresh.assert_not_called()
        mock_fetch.assert_not_called()

    def test_an_UNREADABLE_backup_defers_without_claiming_a_mismatch(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict,
        caplog,
    ):
        """A read that FAILED compared nothing. The warning on this arm names
        a mismatch AND an unusable backup; with the backup unread neither was
        observed -- a backup cswap could not read says nothing about whether
        the slot is healthy. The defer is right either way; only the sentence
        is wrong."""
        switcher = self._switcher(sample_sequence_data)
        with patch.object(switcher, "_read_credentials", return_value=self._FOREIGN_LIVE), \
             patch.object(
                 switcher, "_read_account_credentials_ex", return_value=("", True)
             ), \
             patch("claude_swap.oauth.try_refresh_oauth_credentials") as mock_refresh, \
             patch("claude_swap.oauth.try_fetch_usage_for_account") as mock_fetch, \
             caplog.at_level("WARNING", logger="claude-swap"):
            result = switcher._fetch_active_usage("1", "test@example.com", self._FOREIGN_LIVE)

        assert result.sentinel == USAGE_TOKEN_EXPIRED
        mock_refresh.assert_not_called()
        mock_fetch.assert_not_called()
        assert "does not match" not in caplog.text, (
            "an unreadable backup was reported as a provenance mismatch: "
            f"{caplog.text}"
        )

    def test_a_SILENT_pass_does_not_spend_the_one_shot_warning(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict,
        caplog,
    ):
        """The marker is set INSIDE the readable branch, and that placement is
        the whole of requirement 6 across passes. Consume it on a failed read
        -- one hoisted line -- and the real provenance dead end that follows is
        silent forever, because the slot has already "been warned" about a
        condition nobody ever observed."""
        switcher = self._switcher(sample_sequence_data)
        dead_backup = json.dumps({
            "claudeAiOauth": {"accessToken": "", "refreshToken": ""},
        })

        def _pass(ex_value):
            caplog.clear()
            with patch.object(
                     switcher, "_read_credentials", return_value=self._FOREIGN_LIVE
                 ), \
                 patch.object(
                     switcher, "_read_account_credentials_ex", return_value=ex_value
                 ), \
                 patch("claude_swap.oauth.try_refresh_oauth_credentials"), \
                 patch("claude_swap.oauth.try_fetch_usage_for_account"), \
                 caplog.at_level("WARNING", logger="claude-swap"):
                switcher._fetch_active_usage("1", "test@example.com", self._FOREIGN_LIVE)
            return caplog.text

        assert "does not match" not in _pass(("", True)), "the failed read spoke"
        assert "does not match" in _pass((dead_backup, False)), (
            "the failed read spent the one-shot marker, so the REAL dead end "
            "that followed it stayed silent"
        )

    def test_CONTROL_a_READ_but_unusable_backup_still_warns(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict,
        caplog,
    ):
        """THE CONTROL THAT STOPS THIS BEING A DELETION. A backup that was
        genuinely read and genuinely carries no grant is a real provenance
        dead end, and must still say so -- narrowing the warning must not
        silence it."""
        switcher = self._switcher(sample_sequence_data)
        dead_backup = json.dumps({
            "claudeAiOauth": {"accessToken": "", "refreshToken": ""},
        })
        with patch.object(switcher, "_read_credentials", return_value=self._FOREIGN_LIVE), \
             patch.object(
                 switcher, "_read_account_credentials_ex",
                 return_value=(dead_backup, False),
             ), \
             patch("claude_swap.oauth.try_refresh_oauth_credentials"), \
             patch("claude_swap.oauth.try_fetch_usage_for_account"), \
             caplog.at_level("WARNING", logger="claude-swap"):
            result = switcher._fetch_active_usage("1", "test@example.com", self._FOREIGN_LIVE)

        assert result.sentinel == USAGE_TOKEN_EXPIRED
        assert "does not match" in caplog.text, (
            "the real provenance dead end stopped warning: " + caplog.text
        )

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
        never written into THIS slot's backup (foreign credential under a
        stale config — the write would destroy the slot's only refresh
        token): it is given a slot of its own, once, and this slot's usage is
        suppressed with the foreign sentinel instead of being recorded as its
        (#117's mis-keying shape). The verdict is cached so the same bytes
        neither re-probe nor re-register."""
        import logging

        switcher = self._switcher(sample_sequence_data)
        foreign = {
            "uuid": "uuid-foreign", "email": "other@example.com",
            "organizationUuid": None,
        }

        with caplog.at_level(logging.INFO, logger="claude-swap"):
            first, write_backup, mock_probe = self._fresh_drift_pass(
                switcher, foreign
            )
            second, write_backup2, mock_probe2 = self._fresh_drift_pass(
                switcher, foreign
            )

        assert first.sentinel == USAGE_FOREIGN_CREDENTIAL
        assert first.usage is None
        assert second.sentinel == USAGE_FOREIGN_CREDENTIAL
        write_backup.assert_called_once()
        assert write_backup.call_args[0][0] != "1", "written into THIS slot"
        assert write_backup.call_args[0][1] == "other@example.com"
        write_backup2.assert_not_called()
        mock_probe.assert_called_once()
        mock_probe2.assert_not_called()   # verdict cached, no re-probe
        registered = [
            r for r in caplog.records
            if "Registered a login as Account-" in r.getMessage()
        ]
        assert len(registered) == 1, [r.getMessage() for r in caplog.records]

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
        this slot: definitive foreign — nothing written into THIS slot, no
        backfill, verdict cached, usage suppressed. The sibling gets a slot
        of its own, once."""
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

        write_backup.assert_called_once()
        assert write_backup.call_args[0][0] != "1", "written into THIS slot"
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

    def test_a_held_credential_lock_defers_the_active_refresh(
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
        # TWO THINGS WERE WRONG HERE AND THE SECOND HID BEHIND THE FIRST.
        #
        # 1. It paid the FileLock default (10s) in full — the slowest test in
        #    the suite by 5x, 10.01s of a 61s run.
        # 2. It never reached the gate it names. `consume_lock` lives in
        #    `consume_backup_grant`; this drives `_fetch_active_usage`, which
        #    hits an EARLIER gate first. Instrumented: the branch under test
        #    was reached 0 times, and mutating `if not consume_lock.acquire()`
        #    to `if False` left the test PASSING — on the original 10s version
        #    too, so this is not a speedup artefact. It was spending ten
        #    seconds proving something else.
        #
        # What it actually exercised is worth keeping (a held credential lock
        # defers rather than POSTs), so that is what it now says, asserted
        # through the log line that names it. The consume gate gets its own
        # test below, driving `consume_backup_grant` directly.
        real_init = FileLock.__init__
        try:
            def fast_init(self, lock_path, timeout=0.05):
                real_init(self, lock_path, timeout)

            with patch.object(FileLock, "__init__", fast_init), patch.object(
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

    def test_a_held_consume_lock_defers_the_grant_post(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """THE gate the test above only claimed to cover.

        `consume_backup_grant` owns `.consume-N.lock`, and the whole point is
        that a second gate must not POST the same one-time-use grant while
        another holds it: one POST wins, the loser gets invalid_grant, and the
        strike lands on a live account.

        Driven at `consume_backup_grant` directly. Reaching it through
        `_fetch_active_usage` is what made the sibling test above miss —
        an earlier credential-lock gate defers first, so the consume branch
        was never entered at all (instrumented: 0 hits).
        """
        from claude_swap.locking import FileLock

        switcher = self._switcher(sample_sequence_data)
        switcher._write_account_credentials("1", "test@example.com", self._EXPIRED)
        holder = FileLock(switcher.credentials_dir / ".consume-1.lock")
        assert holder.acquire(), "could not seed the contended lock"

        real_init = FileLock.__init__

        def fast_init(self, lock_path, timeout=0.05):
            real_init(self, lock_path, timeout)

        try:
            with patch.object(FileLock, "__init__", fast_init), patch(
                "claude_swap.oauth.try_refresh_oauth_credentials"
            ) as mock_refresh:
                out = switcher.consume_backup_grant(
                    "1", "test@example.com", self._EXPIRED
                )
        finally:
            holder.release()

        mock_refresh.assert_not_called(), (
            "POSTed the shared one-time grant while another consume held its lock"
        )
        assert out.error == "consume-busy", (
            f"a contended consume must report its own kind, got {out.error!r}"
        )


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
            ), pytest.raises(OSError, match="disk full"):
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

    def test_post_fetch_invalid_grant_on_active_slot_does_not_condemn_when_unreadable(
        self, temp_home, monkeypatch
    ):
        """C1 (round 9) at the SECOND `_entry_token_dead` call site: a fetch
        that just returned invalid_grant, on an ACTIVE slot, with the
        Keychain locked, and the strike bound to a DIFFERENT generation than
        the live credential (i.e. this exact fetch's failure doesn't confirm
        the live bytes are the condemned ones -- the active-slot backup
        might already hold a fresher, healthy generation we simply can't
        see). Must not get USAGE_RELOGIN_REQUIRED -- same ambiguity as the
        pre-fetch scan, just reached from the post-fetch branch."""
        from claude_swap.usage_store import FetchRecord
        switcher = ClaudeAccountSwitcher()
        switcher.platform = Platform.MACOS
        switcher._setup_directories()
        live = self._dead_creds()  # the live credential the fetch POSTed
        other_gen = json.dumps({"claudeAiOauth": {
            "accessToken": "at-other", "refreshToken": "rt-other",
            "expiresAt": 1}})
        info = [(2, "test@example.com", "Org", "", True, live, "")]

        monkeypatch.setattr(macos_keychain, "get_password", _raise_locked)
        with patch.object(
            switcher, "_run_usage_fetches",
            return_value={"2": FetchRecord(
                error="invalid_grant",
                struck_fp=oauth.credential_fingerprint(other_gen),
            )},
        ):
            entries = switcher._collect_usage_entries(info)

        assert entries["2"].sentinel != USAGE_RELOGIN_REQUIRED, (
            "C1 regression at the post-fetch call site: an ambiguous "
            "unreadable read on an active slot was condemned, "
            f"sentinel={entries['2'].sentinel!r}"
        )


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
        """The CREDENTIAL is the axis; the config is rebuildable.

        Slot 3 asserted False under #41, when a missing config aborted the
        switch. `_target_config` now rebuilds it from the sequence record,
        so the slot is recoverable and the predicate must say so — see
        test_rotation_reaches_a_config_less_slot_now_that_it_rebuilds for
        what disagreeing cost.
        """
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com", creds=False)
        self._seed(s, 3, "c@example.com", config=False)

        assert s._account_is_switchable("1") is True
        assert s._account_is_switchable("2") is False
        assert s._account_is_switchable("3") is True
        # Stale sequence reference to a missing account record.
        assert s._account_is_switchable("99") is False

    def test_rotation_reaches_a_config_less_slot_now_that_it_rebuilds(
        self, temp_home: Path, capsys
    ):
        """#41 skipped a config-less slot because it could not be activated.

        It can now: the config backup is only ``oauthAccount``, and
        ``_target_config`` rebuilds it from the sequence record — which
        ``_account_is_switchable`` already requires to exist. Leaving the
        predicate at False made ``switch_to N`` succeed on a slot that bare
        rotation, ``best``, ``next-available``, auto-switch and the TUI all
        called unreachable.

        The TUI half is the damaging one: an unswitchable row with no
        sentinel renders ``USAGE_NO_CREDENTIALS`` — "no stored login, switch
        here then log in" — on a slot whose credential is fine, and
        following that advice overwrites it.
        """
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com", config=False)

        assert s._account_is_switchable("2") is True, (
            "a slot whose config is rebuildable is not a broken slot"
        )

        (temp_home / ".claude" / ".credentials.json").write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-live-1",
                "refreshToken": "rt-live-1",
            },
        }))
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "a@example.com",
                "accountUuid": "uuid-1",
            },
        }))

        with patch.object(s, "list_accounts"):
            s.switch()

        assert "Skipping Account-2" not in capsys.readouterr().out
        assert s._get_sequence_data()["activeAccountNumber"] == 2

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
        """Both slots lack the CREDENTIAL, which is the only unrecoverable
        half. Slot 2 used to carry `config=False` here, which no longer makes
        a slot broken — `_target_config` rebuilds that from the roster.
        """
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com", creds=False)
        self._seed(s, 2, "b@example.com", creds=False)

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

    def test_switch_to_credential_less_slot_lands_logged_out(self, temp_home: Path):
        """An empty slot is a destination, not an error.

        A roster import syncs the account LIST but never credentials, so the
        slot you need to log into is exactly the one with nothing stored. The
        old behaviour refused it and suggested `--add-account`, which cannot
        help — there is nothing to add until a login exists. Now the switch
        lands, logged out, so `/login` writes to this slot.
        """
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

        result = s.switch_to("2", json_output=True)

        assert result["switched"] is True
        assert result["needsLogin"] is True
        assert any("logged out" in w for w in result["warnings"])
        # The slot is now active, so a login lands here...
        assert s._get_sequence_data()["activeAccountNumber"] == 2
        # ...and the previous account's token is NOT still serving under it.
        assert not (temp_home / ".claude" / ".credentials.json").exists()
        # The login it replaced was captured first, so nothing was lost.
        assert s._read_account_credentials("1", "a@example.com")

    def test_the_human_switch_says_it_landed_logged_out(
        self, temp_home: Path, capsys
    ):
        """Human mode must not clear the login in silence.

        Every other `_perform_switch` outcome prints under `emit_output`; the
        empty-slot landing returns before that block, and `switch_to` builds
        no result object off JSON, so the one switch that logs the machine out
        was the one that said nothing at all.
        """
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com", creds=False)
        (temp_home / ".claude" / ".credentials.json").write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-live-1",
                "refreshToken": "rt-live-1",
            },
        }))
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "a@example.com",
                "accountUuid": "uuid-1",
            },
        }))

        s.switch_to("2")

        out = capsys.readouterr().out
        assert "Account-2" in out, (
            "the switch that logs the machine out printed nothing: "
            f"{out!r}"
        )
        assert "/login" in out, (
            "the user is now logged out and was not told how to recover: "
            f"{out!r}"
        )

    def test_a_real_landing_on_an_empty_slot_still_reads_as_active(self, temp_home: Path):
        """Landing logged-out must not make the slot invisible.

        ``_build_accounts_info`` derives the active slot from the LIVE
        credential's identity, which is exactly what an empty-slot landing
        clears. So the roster records slot 2 as active while every account
        reports ``is_active=False`` — the TUI shows no active mark anywhere,
        on the one slot the user just deliberately moved to.

        The roster is the authority on WHICH slot is active; the live store is
        the authority on what that slot is holding. Conflating them made the
        second answer erase the first.
        """
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com", creds=False)
        (temp_home / ".claude" / ".credentials.json").write_text(json.dumps({
            "claudeAiOauth": {"accessToken": "sk-1", "refreshToken": "rt-1"},
        }))
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "a@example.com",
                             "accountUuid": "uuid-1"},
        }))

        s.switch_to("2", json_output=True)

        assert s._get_sequence_data()["activeAccountNumber"] == 2, "premise"
        info = s._build_accounts_info()
        active = [num for num, _e, _o, _u, is_active, *_r in info if is_active]
        assert active == [2], (
            f"roster says slot 2 is active, _build_accounts_info says {active}"
        )

    def test_an_unreadable_live_credential_is_not_deleted_unstashed(
        self, temp_home: Path
    ):
        """The stash is the license to clear, so no-stash must mean no-clear.

        `_read_active_credentials().value` is `None` — not `""` — when the file
        EXISTS but cannot be read (mode 000, root-owned after a sudo/container
        run, an ACL). `if live:` is False for both, so the stash is skipped;
        but `_clear_oauth_credential()` unlinks anyway, and unlink needs only a
        writable directory, not a readable file.

        The two states are not the same and must not take the same branch:

            ""    nothing anywhere        -> nothing to preserve, clear freely
            None  present but unreadable  -> the only copy, and we cannot read it

        Measured on a real 0-mode file: the credential is gone and the stash
        directory is empty. That refresh token existed nowhere else — the slot
        was roster-imported, so there is no backup either.

        Reached through the direct-activation path, which calls this method
        BEFORE its rollback snapshot, so nothing downstream can put it back.
        """
        s = self._setup(temp_home)
        self._seed(s, 2, "b@example.com", creds=False)
        data = s._get_sequence_data()
        data["activeAccountNumber"] = None
        s.sequence_file.write_text(json.dumps(data))

        cred = get_credentials_path()
        cred.parent.mkdir(parents=True, exist_ok=True)
        cred.write_text(json.dumps({
            "claudeAiOauth": {"refreshToken": "ONLY-COPY-REFRESH"},
        }))

        # The READ fails, however the platform arranges that. Mode 000 is the
        # POSIX shape and was the original repro, but Windows ignores it (the
        # file reads fine there and the premise assert flipped on CI), so the
        # failure is injected at the read instead. What is under test is the
        # branch taken when the value is None with the file still present, not
        # any particular way of getting there.
        # Only the READ is refused; `unlink` is left alone deliberately, because
        # the whole defect is that unlink SUCCEEDS where read cannot — it needs
        # a writable directory, not a readable file. Patching both would make
        # the file survive for the wrong reason and the test would pass with
        # the guard removed (measured: it did).
        real_read_text = type(cred).read_text

        def refuse_read(self, *a, **kw):
            if self.name == ".credentials.json":
                raise PermissionError(13, "Permission denied")
            return real_read_text(self, *a, **kw)

        with patch.object(type(cred), "read_text", refuse_read):
            assert s._store._read_active_credentials().value is None, (
                "premise: unreadable reads as None, not empty"
            )

            with pytest.raises(CredentialReadError):
                s._switch_to_empty_slot(
                    "2", "b@example.com", None, {"number": 2},
                    s._get_sequence_data(),
                )

            assert cred.exists(), (
                "the only copy of a live refresh token was deleted unstashed"
            )

    def test_an_unreadable_keychain_is_not_read_as_an_empty_live_slot(
        self, temp_home: Path, monkeypatch
    ):
        """`""` from a FAILED read is not `""` from an empty store.

        The refusal above reads `.value` alone, so it catches `None` (a file
        present but unreadable) and misses the Keychain's shape: when the OAuth
        read fails and nothing else covers it,
        `_read_active_credentials` returns `("", keychain_unavailable=True)`.
        Both spellings mean "a credential may be live and we could not see
        it"; only one was refused.

        The delete is not the read. `find-generic-password -w` DECRYPTS;
        `delete-generic-password` is attribute-only, and
        `_delete_active_keychain_entry` calls it directly rather than through
        `_use_keychain`. So a read that times out under the statusline
        contention this module documents by name, followed by a delete that
        succeeds once the contention clears, is an ordinary sequence.

        Measured end to end through `_switch_to_empty_slot`, unmanaged live
        login, Keychain read raising and delete succeeding:

            RAISED SwitchError "...The credential is preserved in the stash..."
            keychain item survived: False
            stash entries: []

        The refresh token was the only copy. `if live:` skipped the stash
        because `""` is falsy, the clear then removed the item, and the raise
        told the user it was preserved.
        """
        from claude_swap import macos_keychain as _kc
        from claude_swap.exceptions import CredentialReadError

        s = self._setup(temp_home)
        s.platform = Platform.MACOS
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com", creds=False)
        data = s._get_sequence_data()
        data["activeAccountNumber"] = 1
        s.sequence_file.write_text(json.dumps(data))

        item = ("Claude Code-credentials", _kc.keychain_account_name())
        store = {
            item: json.dumps({"claudeAiOauth": {"refreshToken": "ONLY-COPY"}})
        }

        def get_password(service, account):
            if "credentials" in service:
                raise _kc.KeychainError("timed out")     # statusline contention
            return None

        def delete_password(service, account):
            store.pop((service, account), None)          # the delete SUCCEEDS
            return None

        monkeypatch.setattr(_kc, "get_password", get_password)
        monkeypatch.setattr(_kc, "delete_password", delete_password)
        monkeypatch.setattr(_kc, "set_password", lambda *a, **k: None)
        s._store._keychain_usable_cache = True

        with pytest.raises(CredentialReadError):
            s._switch_to_empty_slot(
                "2", "b@example.com", {"number": 1}, {"number": 2},
                s._get_sequence_data(),
            )

        assert store.get(item) is not None, (
            "the live credential was deleted after a read that could not see "
            "it — the stash never ran and there is no other copy"
        )

    def test_a_failed_clear_does_not_hand_the_slot_a_live_credential(
        self, temp_home: Path
    ):
        """A clear that did not clear must not be followed by the identity pop.

        Both clears are best-effort by design (a down Keychain, a missing file
        — warn and continue), but the ``oauthAccount`` pop three lines later
        was unconditional. So a failed clear produced exactly the state the
        landed-empty fallback is built to trust: no live identity, roster says
        slot N. The fallback then marks slot N active, and slot N reads the
        LIVE store — which still holds the DEPARTED account's token.

        Measured with the unlink failing (read-only mount / immutable bit)::

            live still='{"claudeAiOauth": {"refreshToken": "DEPARTED-REFRESH"}}'
            identity=None
            SLOT 2 b@example.com creds='...DEPARTED-REFRESH' <== ACTIVE

        The likelier shape is the macOS one: `_delete_active_keychain_entry`
        swallows every exception, and that path already documents a residual.

        Re-read rather than trusted, for the same reason the `--clear` verdict
        elsewhere is re-read: `had_pin` measured before the action answers what
        was true a moment ago. A clear that failed is reported as a failure,
        and the identity stays put so the roster and the live store keep naming
        the SAME account rather than disagreeing silently.
        """
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com", creds=False)
        data = s._get_sequence_data()
        data["activeAccountNumber"] = 1
        s.sequence_file.write_text(json.dumps(data))

        cred = get_credentials_path()
        cred.parent.mkdir(parents=True, exist_ok=True)
        cred.write_text(json.dumps({
            "claudeAiOauth": {"refreshToken": "DEPARTED-REFRESH"},
        }))
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "a@example.com",
                             "accountUuid": "uuid-1"},
        }))

        real_unlink = type(cred).unlink

        def refuse(self, *a, **kw):
            if self.name == ".credentials.json":
                raise OSError(30, "Read-only file system")
            return real_unlink(self, *a, **kw)

        with patch.object(type(cred), "unlink", refuse):
            with pytest.raises(ClaudeSwitchError):
                s._switch_to_empty_slot(
                    "2", "b@example.com", {"number": 1}, {"number": 2},
                    s._get_sequence_data(),
                )

        # Slot 1 legitimately holds it — it is that account's own. What must
        # not happen is slot 2 wearing the active mark over it, which is what
        # the unconditional identity pop produced.
        for num, _e, _o, _u, is_active, creds, _al in s._build_accounts_info():
            if num == "2":
                assert "DEPARTED-REFRESH" not in str(creds), (
                    "the landed slot was handed the departed account's live "
                    "credential"
                )
            assert not (is_active and num == "2"), (
                "the landed slot is marked active while the previous account's "
                "login is still live"
            )
        assert s._get_current_account() is not None, (
            "the identity was popped over a credential that is still live, so "
            "the roster and the live store now name different accounts"
        )

    def test_the_post_clear_check_does_not_re_collapse_none_and_empty(
        self, temp_home: Path
    ):
        """The re-read must use the distinction the refusal above establishes.

        `if ...value:` is falsy for BOTH `""` (nothing there, the clear worked)
        and `None` (a credential is present and unreadable, i.e. the clear did
        NOT work). Twenty lines are spent above establishing that those differ,
        and this line collapsed them again at the one point that acts on the
        answer.

        Reachable with no race and nothing failing on the Keychain: the live
        credential is in the Keychain, and `_write_oauth_credentials` also
        keeps a `.credentials.json` shadow file (#86, so running sessions
        hot-reload). The PRE-clear read short-circuits at the Keychain and
        never touches that file, so the earlier refusal never sees `None`. The
        Keychain delete then succeeds, the file unlink fails, and the re-read
        reaches the file for the first time — `None`, falsy, guard passes. The
        landing completes while `.credentials.json` still holds the departed
        account's token, which is exactly what Claude Code falls back to.
        """
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com", creds=False)
        data = s._get_sequence_data()
        data["activeAccountNumber"] = 1
        s.sequence_file.write_text(json.dumps(data))
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "a@example.com",
                             "accountUuid": "uuid-1"},
        }))

        cred = get_credentials_path()
        cred.parent.mkdir(parents=True, exist_ok=True)
        cred.write_text(json.dumps({
            "claudeAiOauth": {"refreshToken": "A-LIVE-REFRESH"},
        }))

        # The clear runs, but the file survives AND becomes unreadable — so the
        # re-read sees `None`, not `""`.
        real_unlink = type(cred).unlink
        real_read = type(cred).read_text
        state = {"cleared": False}

        def refuse_unlink(self, *a, **kw):
            if self.name == ".credentials.json":
                state["cleared"] = True
                raise OSError(30, "Read-only file system")
            return real_unlink(self, *a, **kw)

        def read_after_clear(self, *a, **kw):
            if self.name == ".credentials.json" and state["cleared"]:
                raise PermissionError(13, "Permission denied")
            return real_read(self, *a, **kw)

        with patch.object(type(cred), "unlink", refuse_unlink), \
                patch.object(type(cred), "read_text", read_after_clear):
            with pytest.raises(ClaudeSwitchError):
                s._switch_to_empty_slot(
                    "2", "b@example.com", {"number": 1}, {"number": 2},
                    s._get_sequence_data(),
                )

        assert cred.exists(), "premise: the credential survived the clear"

    def test_a_genuinely_logged_out_machine_can_still_land(
        self, temp_home: Path, monkeypatch
    ):
        """The guard must PERMIT the one case it is written around.

        `value == ""` with `keychain_unavailable=False` is a real logged-out
        machine: nothing is there and clearing costs nothing. Measured,
        replacing the whole condition with `if not live:` — which subsumes both
        refusal cases — leaves 430/430 green, because no test lands on an empty
        slot from a logged-out machine. The same over-reach class as treating
        `cfg is None` as failure.
        """
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com", creds=False)
        data = s._get_sequence_data()
        data["activeAccountNumber"] = 1
        s.sequence_file.write_text(json.dumps(data))
        (temp_home / ".claude.json").write_text(json.dumps({}))

        monkeypatch.setattr(
            s._store,
            "_read_active_credentials",
            lambda: ActiveCredentials("", keychain_unavailable=False,
                                      degraded=False),
        )

        s._switch_to_empty_slot(
            "2", "b@example.com", {"number": 1}, {"number": 2},
            s._get_sequence_data(),
        )
        assert str(s._get_sequence_data()["activeAccountNumber"]) == "2", (
            "a logged-out machine could not land on an empty slot; the guard "
            "refuses the case it exists to permit"
        )

    def test_a_degraded_read_is_not_a_licence_to_clear(
        self, temp_home: Path, monkeypatch
    ):
        """`degraded` is the third axis, and this method never read it.

        The guard tests `value is None` and `value == "" and
        keychain_unavailable`. A DEGRADED read passes both: the value is real
        bytes, so it is neither. But `degraded` means those bytes came from the
        plaintext file AFTER the Keychain read failed, and on macOS Claude Code
        writes rotations Keychain-only — so the file can be a superseded
        generation while the current one is in the Keychain we could not read.

        The clear then deletes the Keychain item, because `delete-generic-
        password` is attribute-only and does not decrypt: the same asymmetry
        this method's own docstring states for the `""` case. What lands in the
        stash is the stale generation, and the live one exists nowhere.

        Asserts the CURRENT generation survives, not the exception type: a
        refusal that still deleted the Keychain item would satisfy a
        `pytest.raises` and lose the token anyway.
        """
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com", creds=False)
        data = s._get_sequence_data()
        data["activeAccountNumber"] = 1
        s.sequence_file.write_text(json.dumps(data))
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "a@example.com",
                             "accountUuid": "uuid-1"},
        }))

        # The file holds the SUPERSEDED generation; the Keychain holds the
        # current one and cannot be read. That is what `degraded` reports.
        cred = get_credentials_path()
        cred.parent.mkdir(parents=True, exist_ok=True)
        cred.write_text(json.dumps({
            "claudeAiOauth": {"refreshToken": "STALE-GEN-7"},
        }))
        keychain = {"current": "CURRENT-GEN-9"}

        stale = cred.read_text()
        monkeypatch.setattr(
            s._store,
            "_read_active_credentials",
            lambda: ActiveCredentials(
                stale, keychain_unavailable=False, degraded=True
            ),
        )
        monkeypatch.setattr(
            s._store,
            "_delete_active_keychain_entry",
            lambda: (keychain.pop("current", None), True)[1],
        )

        with pytest.raises((CredentialReadError, SwitchError)):
            s._switch_to_empty_slot(
                "2", "b@example.com", {"number": 1}, {"number": 2},
                s._get_sequence_data(),
            )

        assert keychain.get("current") == "CURRENT-GEN-9", (
            "the current generation was deleted from the Keychain on a read "
            "we already knew was degraded; only the stale one is preserved"
        )

    def test_a_keychain_residual_is_not_read_as_a_successful_clear(
        self, temp_home: Path, monkeypatch
    ):
        """A Keychain that goes unreadable mid-clear leaves the token behind.

        `_delete_active_keychain_entry` calls `delete_password` DIRECTLY and
        swallows every exception, so a failed delete does not flip the routing
        cache. The re-read's own Keychain attempt does fail, and after its
        retries it falls through to the (already-cleared) file and returns
        `("", keychain_unavailable=True)`. Empty and falsy — while the Keychain
        still holds the departed account's token, which Claude Code reads
        BEFORE the file.

        No race required beyond the login keychain auto-locking between the
        pre-switch probe and the clear, which is the ordinary macOS posture:
        the backup is Keychain-only there, since
        `_reconcile_enc_after_keychain_write` deletes the `.enc`.

        `keychain_unavailable` is the only witness — the value is `""` either
        way, so the guard must read the flag.
        """
        from claude_swap import macos_keychain as _kc

        s = self._setup(temp_home)
        s.platform = Platform.MACOS
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com", creds=False)
        data = s._get_sequence_data()
        data["activeAccountNumber"] = 1
        s.sequence_file.write_text(json.dumps(data))

        # The keychain answers for the pre-clear read, then locks.
        state = {"locked": False}
        stash: dict = {}

        def get_password(service, account):
            if state["locked"]:
                raise _kc.KeychainError("locked")
            return stash.get((service, account))

        def delete_password(service, account):
            state["locked"] = True          # locks DURING the clear
            raise _kc.KeychainError("locked")

        stash[("Claude Code-credentials", _kc.keychain_account_name())] = (
            json.dumps({"claudeAiOauth": {"refreshToken": "DEPARTED-REFRESH"}})
        )
        monkeypatch.setattr(_kc, "get_password", get_password)
        monkeypatch.setattr(_kc, "delete_password", delete_password)
        monkeypatch.setattr(_kc, "set_password", lambda *a, **k: None)
        s._store._keychain_usable_cache = True

        with pytest.raises(SwitchError):
            s._switch_to_empty_slot(
                "2", "b@example.com", {"number": 1}, {"number": 2},
                s._get_sequence_data(),
            )

    def test_a_pinned_file_mode_does_not_blind_the_post_clear_check(
        self, temp_home: Path, monkeypatch
    ):
        """A readable Keychain the check never asks is the same as no check.

        The residual test above covers a Keychain that goes UNREADABLE, where
        `keychain_unavailable` is the witness. This is the other shape and it
        has no witness at all: an earlier write fell back and pinned file mode,
        so `_use_keychain()` is False and `_keychain_unreadable` is False by
        construction — nothing failed, we chose the file. The post-clear read
        therefore never asks the Keychain, and a surviving item is invisible to
        the check that exists to catch it. Claude Code reads it first.

        `_pin_file_mode`'s own docstring names this residual: it is entered
        from a write that fell back, and its best-effort delete may have
        failed. Measured on this branch, one process, no hand-set state:

            after the write   use_kc=False  unreadable=False  file_ours=True
            post-clear read   value=''      unavailable=False -> GUARD PASSES
            Keychain residual still present: True

        The delete already knows. It returns whether an item can still shadow
        the file, and the clear passes that up rather than the check trying to
        infer it from a backend it is not using.

        Alongside #196 a second witness also fires here — that PR records
        `_keychain_op_failed` at the same write, so `keychain_unavailable`
        becomes True and with it `degraded`. Measured in the merged tree the
        DEGRADED guard wins and raises `CredentialReadError` before the
        post-clear check is reached; alone on this branch it is `SwitchError`
        from that check. Two refusals for one state, from two branches that
        closed it independently.

        So this asserts `ClaudeSwitchError` — the REFUSAL, which is the
        contract either way. Naming the narrower `SwitchError` made both green
        branches fail on merge with nothing actually wrong: measured, 1959
        passed and this one red purely on the exception class. Dropping
        `residual_gone` from the guard still turns it red on this branch,
        where that is the only witness.
        """
        from claude_swap import macos_keychain as _kc

        s = self._setup(temp_home)
        s.platform = Platform.MACOS
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com", creds=False)
        data = s._get_sequence_data()
        data["activeAccountNumber"] = 1
        s.sequence_file.write_text(json.dumps(data))

        residual = json.dumps(
            {"claudeAiOauth": {"refreshToken": "DEPARTED-REFRESH"}}
        )
        item = ("Claude Code-credentials", _kc.keychain_account_name())
        store: dict = {item: residual}

        def get_password(service, account):
            return store.get((service, account))

        def delete_password(service, account):
            raise _kc.KeychainError("delete denied")   # the residual SURVIVES

        def set_password(service, account, value):
            raise _kc.KeychainError("write denied")    # forces the fallback

        monkeypatch.setattr(_kc, "get_password", get_password)
        monkeypatch.setattr(_kc, "delete_password", delete_password)
        monkeypatch.setattr(_kc, "set_password", set_password)
        s._store._keychain_usable_cache = True

        # A write falls back to the file and pins — the state the guard is
        # blind in. Driven through the production path rather than by calling
        # `_pin_file_mode` directly, whose signature differs across branches.
        # Reads SUCCEED throughout.
        s._store._write_oauth_credentials(
            json.dumps({"claudeAiOauth": {"refreshToken": "A-LIVE-REFRESH"}})
        )
        assert s._store._use_keychain() is False, "premise: file mode is pinned"
        assert get_password(*item) is not None, "premise: the residual survived"

        with pytest.raises(ClaudeSwitchError):
            s._switch_to_empty_slot(
                "2", "b@example.com", {"number": 1}, {"number": 2},
                s._get_sequence_data(),
            )

    def test_an_unreadable_config_is_not_a_cleared_managed_key(
        self, temp_home: Path
    ):
        """`_read_global_config` answers None on ANY failure, and None skipped
        the drop while leaving the verdict True.

        The re-read is not independent: `_read_active_credentials` reaches
        `_read_managed_key`, which reads the SAME file through the SAME
        swallowing reader and answers `""`. So all three refusal terms pass
        over a live `primaryApiKey`.

        Reproduced through the public `switch_to` with a truncated
        `~/.claude.json` (an interrupted write, a full disk):

            switch_to -> switched=True  needsLogin=True
            primaryApiKey survived = True
            activeAccountNumber = 2

        The landing announces "you are now logged out" while
        `sk-ant-api03-...` keeps authenticating and billing. The OAuth half
        fails loudly; only the managed half was blind.
        """
        s = self._setup(temp_home)
        s.platform = Platform.LINUX          # no Keychain: the config is all
        cfg = get_global_config_path()
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text('{"primaryApiKey": "sk-ant-api03-SURVIVOR"')  # truncated

        assert s._store._read_global_config() is None, "premise: unreadable"
        assert s._store._clear_managed_key() is False, (
            "an unreadable config reported a cleared managed key — the drop "
            "never ran and a live API key survives the landing"
        )

    def test_a_surviving_managed_key_is_not_read_as_a_cleared_slot(
        self, temp_home: Path, monkeypatch
    ):
        """The managed axis was blind exactly the way OAuth was.

        `_clear_oauth_credential` reports whether an item can still shadow the
        file; `_clear_managed_key` swallowed its delete in a bare
        `except Exception: pass` and returned nothing. Under a pinned file
        mode the post-clear read never asks the Keychain, so a surviving
        managed item is invisible to both the pre-clear refusal and the
        post-clear check — and Claude Code reads the "Claude Code" Keychain
        item BEFORE `primaryApiKey`, so the survivor wins.

        Measured through the production write path, nothing hand-set: a
        managed write falls back (per-item ACL denies the write), pinning file
        mode, and the earlier key survives because the fallback path never
        deletes the item.

            PRE-clear   value_fp=<new>  unavail=False  degraded=False
            LANDED      switched=True  needsLogin=True  active=2
            KEYCHAIN    survivor present=True

        The user is told "you are now logged out" while the surviving key
        keeps authenticating and billing per token.

        The existing test one below covers the READ half (a denied `-w`).
        This is the CLEAR half.
        """
        from claude_swap import macos_keychain as _kc

        s = self._setup(temp_home)
        s.platform = Platform.MACOS
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com", creds=False)
        data = s._get_sequence_data()
        data["activeAccountNumber"] = 1
        s.sequence_file.write_text(json.dumps(data))

        managed = ("Claude Code", _kc.keychain_account_name())
        store = {managed: "sk-ant-api03-SURVIVOR"}

        def get_password(service, account):
            return store.get((service, account))

        def delete_password(service, account):
            if service == "Claude Code":
                raise _kc.KeychainError("denied")   # the managed item SURVIVES
            store.pop((service, account), None)
            return None

        monkeypatch.setattr(_kc, "get_password", get_password)
        monkeypatch.setattr(_kc, "delete_password", delete_password)
        monkeypatch.setattr(_kc, "set_password", lambda *a, **k: None)
        s._store._keychain_usable_cache = True
        # File mode through the production path (an OAuth write whose Keychain
        # write fails), so this holds on either branch's `_pin_file_mode`
        # signature.
        real_set = _kc.set_password

        def set_denied(*_a, **_kw):
            raise _kc.KeychainError("write denied")

        monkeypatch.setattr(_kc, "set_password", set_denied)
        s._store._write_oauth_credentials(
            json.dumps({"claudeAiOauth": {"refreshToken": "LIVE"}})
        )
        monkeypatch.setattr(_kc, "set_password", real_set)
        assert s._store._use_keychain() is False, "premise: file mode is pinned"

        with pytest.raises(ClaudeSwitchError):
            s._switch_to_empty_slot(
                "2", "b@example.com", {"number": 1}, {"number": 2},
                s._get_sequence_data(),
            )

        assert store.get(managed) is not None, "premise: the key survived"

    def test_a_managed_key_keychain_failure_is_not_read_as_an_empty_slot(
        self, temp_home: Path, monkeypatch
    ):
        """`keychain_unavailable` must cover BOTH credential axes.

        `_read_active_credentials` sets `keychain_failed` only from the OAuth
        Keychain read. `_read_managed_key` catches `KEYCHAIN_ERRORS` itself,
        warns, and falls through to `primaryApiKey` — so the failure never
        reaches the tuple and the guard sees `('', False, False)`, which is
        indistinguishable from a genuinely empty slot.

        The two axes fail ASYMMETRICALLY with no race, which is why this is not
        covered by the OAuth-side residual test. On an API-key account there is
        no OAuth Keychain item at all, so `find-generic-password` answers rc-44
        WITHOUT decrypting and does not raise; only the managed item exists,
        and its `-w` read must decrypt, so only that one is denied.

        Measured through `switch_to`: slot 3 an API-key account, slot 2
        roster-imported empty, the login keychain locking at the delete —

            [2] ActiveCredentials(value='', keychain_unavailable=False, ...)
            guard verdict: PROCEED
            activeAccountNumber = 2
            KEYCHAIN STILL HOLDS: sk-ant-api03-SECRETKEY

        The user is told "logged out" while account 3's key keeps
        authenticating and billing per token. Same defect the API-key fix
        closed on the config half, still open on the Keychain half.
        """
        from claude_swap import macos_keychain as _kc

        s = self._setup(temp_home)
        s.platform = Platform.MACOS
        store = s._store
        store._keychain_usable_cache = True
        real_get = _kc.get_password

        def only_the_managed_item_is_denied(service, account):
            if service == "Claude Code":
                raise _kc.KeychainError("errSecAuthFailed")
            return real_get(service, account)

        _kc.set_password(
            "Claude Code", _kc.keychain_account_name(), "sk-ant-api03-SECRETKEY"
        )
        monkeypatch.setattr(_kc, "get_password", only_the_managed_item_is_denied)

        active = store._read_active_credentials()
        assert active.value == "", "premise: the read cannot see the key"
        assert active.keychain_unavailable, (
            "a denied managed-key Keychain read reported a healthy Keychain — "
            "the landing proceeds over a live billing key"
        )

    def test_an_api_key_does_not_survive_the_empty_slot_landing(
        self, temp_home: Path
    ):
        """Landing logged-out must clear BOTH credential axes, not just OAuth.

        `_read_active_credentials()` answers for OAuth *and* a managed API key,
        so the stash branch is entered for either — but only
        `_clear_oauth_credential()` ran. A managed key therefore survived the
        landing that just announced "you are now logged out", and Claude Code
        kept authenticating as the account that key belongs to.

        Measured: slot 3 an API-key account, slot 2 roster-imported with no
        credentials. After landing on 2, `primaryApiKey` is still in
        `~/.claude.json` and `_build_accounts_info` marks slot 2 active
        carrying `sk-ant-api03-...`. `_static_usage_sentinel` then stamps a
        slot with no credentials at all as a working `api key` account, and
        `cswap --list` raises its own collision warning claiming a backup was
        overwritten — which never happened.

        This violates the method's own stated invariant: an active slot that
        keeps serving the previous account's credential lies about whose quota
        is burning.
        """
        s = self._setup(temp_home)
        self._seed(s, 2, "b@example.com", creds=False)
        self._seed(s, 3, "api-key-3@token.local", creds=False)
        data = s._get_sequence_data()
        data["activeAccountNumber"] = 3
        s.sequence_file.write_text(json.dumps(data))
        (temp_home / ".claude.json").write_text(json.dumps({
            "primaryApiKey": "sk-ant-api03-SECRETKEY",
            "oauthAccount": {"emailAddress": "api-key-3@token.local",
                             "accountUuid": "uuid-3"},
        }))
        assert s._store._read_active_credentials().value, "premise: a key is live"

        s._switch_to_empty_slot(
            "2", "b@example.com", {"number": 3}, {"number": 2},
            s._get_sequence_data(),
        )

        # The stash is the license to clear, and it ran — so the clear must too.
        stash = sorted(s._store._host.credentials_dir.glob(".unclaimed-*.enc"))
        assert stash, "premise: the live credential was preserved first"

        assert not s._store._read_active_credentials().value, (
            "an API key survived a landing that announced logged-out"
        )
        info = s._build_accounts_info()
        for num, _e, _o, _u, _a, creds, _al in info:
            assert "sk-ant-api03" not in str(creds), (
                f"slot {num} was handed the departed account's API key"
            )

    def test_an_unmanaged_live_login_does_not_steal_the_active_mark(
        self, temp_home: Path
    ):
        """A live login the roster has never seen belongs to NO slot.

        ``_find_account_slot`` answers ``None`` for two different reasons: no
        live identity at all (the landed-empty case the fallback exists for),
        and a live identity that is not in the roster — someone ran ``/login``
        with an account never ``cswap add``ed. Keying the fallback on the
        RESULT rather than on the CAUSE conflated them, so the roster's slot
        was marked active and handed the stranger's live credential.

        Measured on the first cut: roster active = 2, live login
        ``stranger@example.com``, and ``_build_accounts_info`` reported slot 2
        (``b@example.com``) active carrying ``STRANGER-TOKEN``. The TUI then
        shows the stranger's usage under b@'s name, and with the ownership
        oracle unreachable that foreign utilization is recorded into the usage
        store keyed to slot 2, where it outlives the condition.
        """
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        data = s._get_sequence_data()
        data["activeAccountNumber"] = 2
        s.sequence_file.write_text(json.dumps(data))
        # /login with an account that is in no slot.
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "stranger@example.com",
                             "accountUuid": "uuid-stranger"},
        }))
        (temp_home / ".claude" / ".credentials.json").write_text(json.dumps({
            "claudeAiOauth": {"accessToken": "STRANGER-TOKEN",
                              "refreshToken": "STRANGER-REFRESH"},
        }))

        info = s._build_accounts_info()
        active = [num for num, _e, _o, _u, is_active, *_r in info if is_active]
        assert active == [], (
            f"a login in no slot marked {active} active on the roster's word"
        )
        for num, _e, _o, _u, _a, creds, _al in info:
            assert "STRANGER" not in str(creds), (
                f"slot {num} was served the unmanaged login's credential"
            )

    def test_a_login_after_landing_empty_clears_relogin_required(
        self, temp_home: Path
    ):
        """`/login` writes the LIVE store — the slot must read from there.

        A slot quarantined as refresh-token-dead keeps that verdict while its
        BACKUP holds the condemned generation. `/login` does not touch the
        backup, so the strike survived a real re-login and the TUI kept saying
        "re-login needed" — until the user switched away and back, which
        copies live into the backup.

        The same defect as the test above: the slot is not recognised as
        active, so its credentials are read from the stale backup instead of
        the live store the login just wrote.
        """
        from claude_swap.usage_store import FetchRecord

        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com", creds=False)
        (temp_home / ".claude" / ".credentials.json").write_text(json.dumps({
            "claudeAiOauth": {"accessToken": "sk-1", "refreshToken": "rt-1"},
        }))
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "a@example.com",
                             "accountUuid": "uuid-1"},
        }))
        s.switch_to("2", json_output=True)

        # The user runs /login: Claude Code writes the LIVE store only.
        fresh = json.dumps({
            "claudeAiOauth": {"accessToken": "sk-fresh",
                              "refreshToken": "rt-fresh"},
        })
        (temp_home / ".claude" / ".credentials.json").write_text(fresh)
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "b@example.com",
                             "accountUuid": "uuid-2"},
        }))

        info = s._build_accounts_info()
        creds_for_2 = next(
            c for num, _e, _o, _u, _a, c, _al in info if num == 2
        )
        assert creds_for_2 == fresh, (
            "slot 2 served a stale backup after a real login wrote the live "
            "store; the re-login verdict outlives the re-login"
        )

    def test_locked_keychain_is_not_mistaken_for_an_empty_slot(
        self, temp_home: Path, monkeypatch
    ):
        """macOS: an unreadable Keychain reports "no credentials" too.

        Landing logged-out on that guess would destroy a working login to
        reach a slot whose backup was never actually missing — and on macOS
        the live credential lives in the same Keychain, so it is not
        recoverable from disk. Refuse instead, and keep the live login.
        """
        from claude_swap.exceptions import SwitchError

        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com", creds=False)
        live_creds = json.dumps({
            "claudeAiOauth": {"accessToken": "sk-live-1", "refreshToken": "rt-live-1"},
        })
        (temp_home / ".claude" / ".credentials.json").write_text(live_creds)
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "a@example.com", "accountUuid": "uuid-1"},
        }))

        s.platform = Platform.MACOS
        # A Keychain that ACTUALLY raises, not a routing flag standing in for
        # one. `_keychain_usable_cache` is where ops should be ROUTED, and
        # `_pin_file_mode` sets it deliberately with nothing having failed — so
        # #196 split the observation ("an op raised") out of the routing
        # decision, and a test that sets only the routing flag no longer
        # describes a failure. It passed here in isolation and failed on the
        # merged tree, which is exactly what the merged-suite gate is for.
        from claude_swap import macos_keychain as _kc

        def locked(*_a, **_kw):
            raise _kc.KeychainError("locked")

        for fn in ("get_password", "set_password", "delete_password"):
            monkeypatch.setattr(_kc, fn, locked)
        with pytest.raises(SwitchError, match="Keychain"):
            s.switch_to("2")

        # Nothing moved: still on 1, still logged in.
        assert s._get_sequence_data()["activeAccountNumber"] == 1
        assert (temp_home / ".claude" / ".credentials.json").exists()

    def test_a_missing_config_backup_does_not_cost_the_login(
        self, temp_home: Path
    ):
        """Credentials present, config backup missing — land LOGGED IN.

        Treating this as an empty slot logged the user out and told them
        "Account-2 has no stored credentials" while Account-2's credentials
        sat right there. The config backup is only ``oauthAccount``, and
        every field of it is in the sequence record, so it is rebuilt.
        """
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

        result = s.switch_to("2", json_output=True)

        assert result["switched"] is True
        assert not result.get("needsLogin"), (
            "logged the user out over a missing config backup, while the "
            "slot's credentials were readable"
        )
        assert s._get_sequence_data()["activeAccountNumber"] == 2
        live = json.loads(
            (temp_home / ".claude" / ".credentials.json").read_text()
        )
        assert live["claudeAiOauth"]["refreshToken"] == "rt-2", (
            "landed on the slot without installing its credential"
        )
        cfg = json.loads((temp_home / ".claude.json").read_text())
        assert cfg["oauthAccount"]["emailAddress"] == "b@example.com"

    def test_a_plain_logout_still_reads_the_active_slot_from_its_backup(
        self, temp_home: Path
    ):
        """The roster fallback is licensed by the empty-slot landing only.

        Keyed on `current_identity is None`, it also catches every ordinary
        logged-out state — `/logout`, a deleted `.credentials.json`, a fresh
        machine carrying an imported roster. There the roster slot becomes
        `is_active`, so `_build_accounts_info` reads it from the LIVE store
        instead of its backup: a slot with a perfectly good stored credential
        goes from showing usage to reading as having no login at all.

        The landing case is distinguishable and it is the one that needs the
        fallback: after it, the roster slot has no stored credential either.
        """
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        data = s._get_sequence_data()
        data["activeAccountNumber"] = 1
        s._write_json(s.sequence_file, data)

        # A plain logout: the live store is empty, the backups are intact.
        (temp_home / ".claude" / ".credentials.json").unlink(missing_ok=True)
        (temp_home / ".claude.json").write_text(json.dumps({}))

        info = s._build_accounts_info()
        creds_for_1 = next(c for num, _e, _o, _u, _a, c, _al in info if num == 1)
        assert creds_for_1, (
            "the active slot was read from the empty live store, so its own "
            "stored credential became invisible after an ordinary logout"
        )

    def test_a_landed_empty_slot_still_reads_as_active(self, temp_home: Path):
        """The case the fallback exists for must keep working.

        After a logged-out landing the live store is empty AND the slot has
        no backup, so nothing else can name the active slot.
        """
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com", creds=False, config=False)
        data = s._get_sequence_data()
        data["activeAccountNumber"] = 2
        s._write_json(s.sequence_file, data)

        (temp_home / ".claude" / ".credentials.json").unlink(missing_ok=True)
        (temp_home / ".claude.json").write_text(json.dumps({}))

        info = s._build_accounts_info()
        active = [num for num, _e, _o, _u, a, _c, _al in info if a]
        assert active == [2], (
            "a landed empty slot lost its active mark, which is the whole "
            "reason the roster fallback exists"
        )

    def test_a_blind_keychain_refuses_rather_than_rebuilding_the_config(
        self, temp_home: Path, monkeypatch
    ):
        """A config backup that is merely UNREADABLE must not be rebuilt.

        The rebuild above is licensed by the backup being genuinely absent.
        Behind a locked Keychain "absent" and "there but out of reach" read
        the same, and rebuilding on that guess installs a config synthesized
        from the roster over one that was never actually missing. Refuse.

        Neither of the two switch paths had a test that fails when this
        guard is removed: with `if False and ...` in front of it the whole
        suite still passed.
        """
        from claude_swap.exceptions import SwitchError

        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com", config=False)
        (temp_home / ".claude" / ".credentials.json").write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-live-1",
                "refreshToken": "rt-live-1",
            },
        }))
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "a@example.com",
                "accountUuid": "uuid-1",
            },
        }))

        s.platform = Platform.MACOS
        from claude_swap import macos_keychain as _kc

        def locked(*_a, **_kw):
            raise _kc.KeychainError("locked")

        for fn in ("get_password", "set_password", "delete_password"):
            monkeypatch.setattr(_kc, fn, locked)

        with pytest.raises(SwitchError, match="Keychain"):
            s.switch_to("2")

        assert s._get_sequence_data()["activeAccountNumber"] == 1, (
            "landed on the slot on a config rebuilt from the roster while "
            "the real backup was merely unreadable"
        )

    def test_force_onto_the_active_slot_with_no_backup_keeps_the_login(
        self, temp_home: Path
    ):
        """`--force` on the slot you are ALREADY on must not log you out.

        `--force` skips the already-active short-circuit on purpose: its job
        is to rewrite the live login FROM the stored backup. With no backup
        there is nothing to rewrite from, and landing "logged out" reaches
        nothing — the slot is already the active one, which is the whole
        state the empty-slot landing exists to produce.

        Measured before the guard: the live credential was cleared, and
        because `from == to` makes `switched` False, `switch_to`'s forced
        self-activation block overwrote the payload with
        `reason="activated"` / "Activated Account-1 from stored backup" —
        a restore reported over a backup that does not exist. The TUI then
        read `switched=False` and notified "No switch: activated" after
        clearing the login.
        """
        from claude_swap.exceptions import SwitchError

        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com", creds=False)
        self._seed(s, 2, "b@example.com")
        data = s._get_sequence_data()
        data["activeAccountNumber"] = 1
        s._write_json(s.sequence_file, data)

        live = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-live-1",
                "refreshToken": "rt-live-1",
            },
        })
        (temp_home / ".claude" / ".credentials.json").write_text(live)
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "a@example.com",
                "accountUuid": "uuid-1",
            },
        }))

        with pytest.raises(SwitchError, match="Already on"):
            s.switch_to("1", json_output=True, force=True)

        assert (temp_home / ".claude" / ".credentials.json").read_text() == live, (
            "--force cleared the live login to land on the slot it was "
            "already on, and there is no backup to restore it from"
        )

    def test_an_empty_slot_landing_does_not_cry_corruption(
        self, temp_home: Path, caplog
    ):
        """Issue #117's signature belongs to ownership VERDICTS, not to a
        displacement.

        The landing stashes the live credential before clearing it, and the
        stash logged "does not belong to Account-1 (displaced-by-empty-slot)
        ... Something outside cswap rewrote the live login after the last
        switch" — every clause of which is false here. Firing the corruption
        signature on an ordinary switch is how it stops being read.
        """
        import logging

        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com", creds=False)
        data = s._get_sequence_data()
        data["activeAccountNumber"] = 1
        s._write_json(s.sequence_file, data)

        # The slot's OWN bytes, so Step 1 classifies this as an ordinary
        # backup and the only stash left is the landing's.
        (temp_home / ".claude" / ".credentials.json").write_text(json.dumps({
            "claudeAiOauth": {"accessToken": "sk-1", "refreshToken": "rt-1"},
        }))
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "a@example.com",
                "accountUuid": "uuid-1",
            },
        }))

        with caplog.at_level(logging.INFO, logger="claude-swap"):
            result = s.switch_to("2", json_output=True)

        assert result["needsLogin"] is True
        assert not [
            r for r in caplog.records
            if "Something outside cswap" in r.getMessage()
        ], "the landing raised issue #117's corruption signature on a clean switch"
        preserved = [
            r for r in caplog.records
            if "preserved before it was replaced" in r.getMessage()
        ]
        assert preserved, "the stash stopped saying it happened at all"
        assert preserved[0].levelno == logging.INFO, (
            "a routine preservation must not carry a WARNING"
        )

    def test_an_unmanaged_live_login_survives_landing_on_an_empty_slot(
        self, temp_home: Path
    ):
        """The direct-activation call site must preserve what it clears.

        It returns BEFORE the rollback snapshot and before
        _stash_live_credential, so trusting the caller destroyed the only
        copy of a live refresh token — measured, nothing survived anywhere in
        the home. Every other empty-slot test seeds a MANAGED live account,
        which takes the other call site, so none of them could fail on this.
        """
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com", creds=False, config=False)

        # A live login cswap does not manage: ~/.claude.json names an account
        # that is not in the roster, so current_account resolves to None and
        # the switch takes the direct-activation path.
        (temp_home / ".claude" / ".credentials.json").write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-UNMANAGED",
                "refreshToken": "rt-UNMANAGED-PRECIOUS",
            },
        }))
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "stranger@example.com",
                "accountUuid": "uuid-stranger",
            },
        }))

        result = s.switch_to("2", json_output=True)
        assert result["needsLogin"] is True

        entries = s._store._list_unclaimed_credentials()
        assert entries, "cleared a live credential with nothing stashed"
        import base64

        bodies = [
            base64.b64decode(
                s._store._stash_entry_path(eid).read_text().strip()
            ).decode()
            for eid in entries
        ]
        assert any("rt-UNMANAGED-PRECIOUS" in b for b in bodies), (
            "stashed an entry, but not the credential it was meant to save"
        )

    def test_landing_on_an_empty_slot_does_not_wedge_the_next_switch(
        self, temp_home: Path
    ):
        """You must be able to get back OUT of an empty slot.

        Clearing the credential while leaving ~/.claude.json naming the
        account you left made the machine incoherent: sequence.json said one
        slot, the config said another, and every later switch died on the
        "empty read must not overwrite the departing backup" guard with a
        misleading "Keychain unreadable?". A LIVE engine emitted that every
        tick forever; the only escape was an undocumented --force.
        """
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com", creds=False, config=False)
        self._seed(s, 3, "c@example.com")
        (temp_home / ".claude" / ".credentials.json").write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-live-1", "refreshToken": "rt-live-1",
            },
        }))
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "a@example.com", "accountUuid": "uuid-1",
            },
        }))

        assert s.switch_to("2", json_output=True)["needsLogin"] is True
        assert s.current_account_number() is None, (
            "still names the account we left; the next switch will fail on "
            "its own write"
        )

        out = s.switch_to("3", json_output=True)
        assert out["switched"] is True
        assert s._get_sequence_data()["activeAccountNumber"] == 3


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
            "ownership mismatch" in w and "belongs to Account-2" in w
            and "cswap add --slot 2" in w
            for w in op["warnings"]
        ), op["warnings"]
        # The switch itself proceeded, onto the stored backup.
        assert json.loads(live_state["creds"])["claudeAiOauth"]["accessToken"] == "sk-stale-2"

    def test_the_adopt_warning_does_not_claim_a_quarantine_that_never_happened(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        """ONE MESSAGE SERVES BOTH ADOPT DOORS, so it must be true on both.
        The later-login door fires on a HEALTHY slot, where the endpoint
        condemned nothing -- saying it did sends the reader hunting a strike
        that was never recorded."""
        _DAY = 86_400_000
        now_ms = int(time.time() * 1000)
        sample_sequence_data["accounts"]["2"]["organizationUuid"] = "org-2"
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        creds_store[("1", "test@example.com")] = self._A1_BACKUP
        creds_store[("2", "account2@example.com")] = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-2-old", "refreshToken": "rt-2-old",
                "refreshTokenExpiresAt": now_ms + 5 * _DAY,
            }
        })
        assert not switcher._slot_token_dead("2", "account2@example.com"), (
            "premise: slot 2 is HEALTHY -- the later-login door, not the "
            "quarantine one"
        )
        foreign = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-2-relogin", "refreshToken": "rt-2-relogin",
            "refreshTokenExpiresAt": now_ms + 30 * _DAY,
        }})
        live_state = {"creds": foreign}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        try:
            op = self._run_switch(switcher, resolver={
                "uuid": "uuid-2", "email": "account2@example.com",
                "organizationUuid": "org-2",
            })
        finally:
            for p in patches:
                p.stop()
        assert creds_store[("2", "account2@example.com")] == foreign, (
            "premise: the later login was adopted, so the warning branch ran"
        )
        # THE ADOPT MUST PRECEDE THE TARGET READ. Activating the pre-adopt
        # bytes hands Claude the credential this login already revoked.
        assert json.loads(live_state["creds"])["claudeAiOauth"][
            "refreshToken"] == "rt-2-relogin", (
            "the switch activated something other than the login it just "
            "adopted into the target slot"
        )
        joined = " ".join(op.get("warnings", []))
        assert "belongs to Account-2" in joined, joined
        assert "condemn" not in joined, (
            "the warning says the endpoint had already condemned slot 2's "
            f"stored token, but slot 2 was healthy: {joined}"
        )

    def test_a_foreign_credential_heals_a_slot_whose_stored_token_is_dead(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        """A foreign credential is refused a slot because identity proves
        ownership, not generation freshness. When the resolved slot's stored
        generation is QUARANTINED as refresh-token-dead there is no freshness
        left to protect: its stored token can mint nothing, so live bytes the
        oracle resolved to it are strictly better than what it holds.

        `cswap import` already heals exactly this case (issue #136,
        `_slot_token_dead`). The switch-time stash did not, so a user who
        logged the account back in had the fresh credential preserved into a
        safety copy that nothing adopts, while the slot kept the dead token
        and asked for a re-login again on the next pass.
        """
        from claude_swap import oauth
        from claude_swap.usage_store import FetchRecord

        sample_sequence_data["accounts"]["2"]["organizationUuid"] = "org-2"
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        creds_store[("1", "test@example.com")] = self._A1_BACKUP
        a2_backup = creds_store[("2", "account2@example.com")]
        ident = {"2": ("account2@example.com", "org-2")}
        switcher._usage_store.record(
            {"2": FetchRecord(
                error="invalid_grant",
                struck_fp=oauth.credential_fingerprint(a2_backup),
            )},
            ident,
        )
        assert switcher._slot_token_dead("2", "account2@example.com"), (
            "premise: slot 2 is quarantined ON THE GENERATION IT STORES"
        )
        foreign = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-2-relogin", "refreshToken": "rt-2-relogin",
        }})
        live_state = {"creds": foreign}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        try:
            self._run_switch(switcher, resolver={
                "uuid": "uuid-2", "email": "account2@example.com",
                "organizationUuid": "org-2",
            })
            # THE ROW'S IDENTITY TOO. `_mutate` REPLACES a row whose
            # identity does not match rather than clearing it, so a wrong
            # org uuid also lands `authDeadStrikes == 0` -- on a blank row
            # that has lost the slot's history, backoff and claim.
            _r = switcher._usage_store._read_rows()["2"]
            assert (_r.get("organizationUuid"), _r["authDeadStrikes"],
                    _r.get("struckFingerprint"), _r.get("claimId"),
                    _r.get("backoffUntil")) == ("org-2", 0, None, None, None), (
                "DEFECT: the adoption did not lift the quarantine on THIS "
                f"slot's row -- it reads {_r}"
            )
            # Inside the patched store: `_slot_token_dead` READS the backup to
            # bind the verdict to a generation, and an unreadable one answers
            # dead whatever the slot holds.
            assert not switcher._slot_token_dead("2", "account2@example.com"), (
                "DEFECT: the slot holds a live credential and is still "
                "quarantined, so it keeps reporting re-login needed"
            )
            # The adoption races a usage pass that already read the DEAD bytes:
            # its POST fails and strikes the slot we just healed. The strike is
            # bound to the generation it POSTed, which the slot no longer
            # stores, so the fingerprint binding heals it with no extra lock.
            switcher._usage_store.record(
                {"2": FetchRecord(
                    error="invalid_grant",
                    struck_fp=oauth.credential_fingerprint(a2_backup),
                )},
                ident,
            )
            assert not switcher._slot_token_dead("2", "account2@example.com"), (
                "DEFECT: a strike bound to the generation the adoption "
                "REPLACED re-quarantines the slot, undoing the heal"
            )
            # CONTROL: the same question CAN still report a presence. A
            # strike bound to the generation the slot now stores is the one
            # verdict the binding must not heal.
            switcher._usage_store.record(
                {"2": FetchRecord(
                    error="invalid_grant",
                    struck_fp=oauth.credential_fingerprint(foreign),
                )},
                ident,
            )
            assert switcher._slot_token_dead("2", "account2@example.com"), (
                "CONTROL: a strike on the STORED generation must still read "
                "dead, or the assertion above proves nothing"
            )
        finally:
            for p in patches:
                p.stop()
        assert creds_store[("1", "test@example.com")] == self._A1_BACKUP, (
            "the outgoing slot is still never written"
        )
        assert creds_store[("2", "account2@example.com")] == foreign, (
            "DEFECT: the dead slot did not adopt the credential the oracle "
            "resolved to it, so it still cannot authenticate"
        )

    def test_a_heal_that_cannot_write_does_not_abort_the_switch(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        """The stash is the license to proceed; the heal is a bonus.

        `_stash_live_credential` runs first and raises on failure, so past it
        the live bytes are preserved and the switch has nothing left to lose.
        A raise from the heal would abort a switch that was already safe, and
        it would leave `switch_account` as a bare OSError -- which the CLI
        renders as a traceback rather than an error envelope, because it
        routes `ClaudeSwitchError` only.
        """
        from claude_swap import oauth
        from claude_swap.usage_store import FetchRecord

        sample_sequence_data["accounts"]["2"]["organizationUuid"] = "org-2"
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        creds_store[("1", "test@example.com")] = self._A1_BACKUP
        a2_backup = creds_store[("2", "account2@example.com")]
        ident = {"2": ("account2@example.com", "org-2")}
        switcher._usage_store.record(
            {"2": FetchRecord(
                error="invalid_grant",
                struck_fp=oauth.credential_fingerprint(a2_backup),
            )},
            ident,
        )
        assert switcher._slot_token_dead("2", "account2@example.com"), (
            "premise: the heal would otherwise fire on this slot"
        )
        foreign = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-2-relogin", "refreshToken": "rt-2-relogin",
        }})
        live_state = {"creds": foreign}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        real_write = switcher._write_account_credentials
        refused = []

        def refusing(num, email, creds):
            if str(num) == "2":
                refused.append(num)
                raise OSError(errno.EACCES, "the slot's store is read-only")
            return real_write(num, email, creds)

        switcher._write_account_credentials = refusing
        try:
            op = self._run_switch(switcher, resolver={
                "uuid": "uuid-2", "email": "account2@example.com",
                "organizationUuid": "org-2",
            })
        finally:
            switcher._write_account_credentials = real_write
            for p in patches:
                p.stop()

        assert refused, (
            "premise: the heal never attempted the write it was meant to fail"
        )
        assert op is not None, (
            "DEFECT: a heal that could not write aborted a switch the stash "
            "had already made safe"
        )
        entries = switcher.list_unclaimed_credentials()
        assert len(entries) == 1 and _read_safety_copy(
            switcher, next(iter(entries))) == foreign, (
            "the live bytes must still be preserved when the heal fails"
        )

    def test_the_heal_does_not_swallow_the_real_store_guard(
        self, temp_home, mock_claude_config, sample_sequence_data,
    ):
        """The sibling above needs the containment; this one bounds it.

        `RealStoreWriteBlocked` is deliberately not an `OSError` subclass so
        that no containment in this codebase can hide a write into the real
        account store, and `_write_account_credentials` says so at its own
        raise site. The heal wraps that method, so a catch wide enough to
        swallow it disarms the guard for this whole path -- which is what
        `except Exception` did here once already.
        """
        from claude_swap import oauth
        from claude_swap.usage_store import FetchRecord
        from tests import conftest as _conftest

        sample_sequence_data["accounts"]["2"]["organizationUuid"] = "org-2"
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        creds_store[("1", "test@example.com")] = self._A1_BACKUP
        a2_backup = creds_store[("2", "account2@example.com")]
        ident = {"2": ("account2@example.com", "org-2")}
        switcher._usage_store.record(
            {"2": FetchRecord(
                error="invalid_grant",
                struck_fp=oauth.credential_fingerprint(a2_backup),
            )},
            ident,
        )
        assert switcher._slot_token_dead("2", "account2@example.com"), (
            "premise: the heal would otherwise fire on this slot"
        )
        foreign = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-2-relogin", "refreshToken": "rt-2-relogin",
        }})
        live_state = {"creds": foreign}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        real_write = switcher._write_account_credentials
        refused = []

        def refusing(num, email, creds):
            if str(num) == "2":
                refused.append(num)
                raise _conftest.RealStoreWriteBlocked(
                    "the heal leaked into the REAL store")
            return real_write(num, email, creds)

        switcher._write_account_credentials = refusing
        try:
            with pytest.raises(_conftest.RealStoreWriteBlocked):
                self._run_switch(switcher, resolver={
                    "uuid": "uuid-2", "email": "account2@example.com",
                    "organizationUuid": "org-2",
                })
        finally:
            switcher._write_account_credentials = real_write
            for p in patches:
                p.stop()

        assert refused, (
            "premise: the heal never attempted the write, so nothing here "
            "says what the containment does with a guard violation"
        )

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
            "does not match a managed account and was not written into" in w
            and "preserved" not in w
            for w in op["warnings"]
        ), (
            "same false 'preserved' claim as its two sibling branches: all "
            f"three route through the sweeping stash: {op['warnings']}")

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
        assert any(
            "previously identified" in w and "was not written into" in w
            and "preserved" not in w
            for w in op["warnings"]
        ), (
            "the stash sweeps this row when the credential is spent, so a "
            f"'preserved' claim here is false in that case: {op['warnings']}")
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
class TestUnclaimedStashSweep:
    """Nothing ever dropped a stash entry, so the pile only grew.

    ``cswap unclaimed --purge`` takes one id at a time and nobody runs it.
    """

    def _switcher(self, temp_home):
        switcher = ClaudeAccountSwitcher()
        switcher.platform = Platform.LINUX
        switcher._setup_directories()
        switcher._init_sequence_file()
        return switcher

    def _ms(self, days):
        return int((time.time() + days * 86400) * 1000)

    def _creds(
        self, refresh_token, refresh_expires_at=None, access="sk-a",
        access_expires_at=None,
    ):
        payload = {"accessToken": access, "refreshToken": refresh_token}
        if refresh_expires_at is not None:
            payload["refreshTokenExpiresAt"] = refresh_expires_at
        if access_expires_at is not None:
            payload["expiresAt"] = access_expires_at
        return json.dumps({"claudeAiOauth": payload})

    def _add_slot(self, switcher, num, email, creds, uuid=None):
        data = switcher._get_sequence_data()
        row = {"email": email, "organizationUuid": ""}
        if uuid is not None:
            row["uuid"] = uuid
        data.setdefault("accounts", {})[num] = row
        switcher._write_json(switcher.sequence_file, data)
        switcher._store._write_account_credentials(num, email, creds)

    def test_sweep_drops_an_expired_refresh_token(self, temp_home, caplog):
        """Arm A: no login can revive it, so keeping it protects nothing."""
        import logging

        switcher = self._switcher(temp_home)
        entry_id = switcher._store._write_unclaimed_credential(
            self._creds("dead-rt", self._ms(-1), access_expires_at=self._ms(-1)),
            {"reason": "foreign"},
        )
        with caplog.at_level(logging.INFO, logger="claude-swap"):
            switcher._sweep_unclaimed_stash()
        assert entry_id not in switcher.list_unclaimed_credentials()
        assert any(
            entry_id in r.getMessage() and "expired" in r.getMessage()
            for r in caplog.records
        ), "a drop must say which entry and why"

    def test_sweep_arm_a_requires_both_tokens_expired(self, temp_home):
        """A refresh token past its own expiry is not enough by itself: an
        access token with life left can still mint requests until it too
        expires, so arm A must require both."""
        switcher = self._switcher(temp_home)
        kept_id = switcher._store._write_unclaimed_credential(
            self._creds("dead-rt", self._ms(-1), access_expires_at=self._ms(1)),
            {"reason": "foreign"},
        )
        switcher._sweep_unclaimed_stash()
        assert kept_id in switcher.list_unclaimed_credentials(), (
            "an unexpired access token can still mint requests"
        )

    def test_sweep_drops_a_superseded_identity_with_a_newer_login(
        self, temp_home, caplog,
    ):
        """Arm C: the row's identity is confirmed elsewhere with a newer
        login, and the slot was measured live this pass."""
        import logging

        switcher = self._switcher(temp_home)
        self._add_slot(
            switcher, "4", "id@x.co", self._creds("slot-rt", self._ms(30)),
            uuid="uuid-4",
        )
        entry_id = switcher._store._write_unclaimed_credential(
            self._creds("row-rt", self._ms(10)),
            {"reason": "foreign", "resolvedIdentity": {"uuid": "uuid-4"}},
        )
        with caplog.at_level(logging.INFO, logger="claude-swap"):
            switcher._sweep_unclaimed_stash(live_slots={"4"})
        assert entry_id not in switcher.list_unclaimed_credentials()
        assert any(
            "Account-4" in r.getMessage() and "newer login" in r.getMessage()
            for r in caplog.records
        ), "the drop must name the slot and the newer-login reason"

    def test_sweep_drops_a_superseded_identity_same_generation_window(
        self, temp_home, caplog,
    ):
        """Arm C: stamps within the jitter window and a different
        fingerprint is a newer generation of the same login, not a newer
        login itself. The slot's own ``expiresAt`` (the access token's mint)
        is the LATER of the two here, so it is the later rotation and the
        row's grant is the one already spent."""
        import logging

        switcher = self._switcher(temp_home)
        base = self._ms(10)
        self._add_slot(
            switcher, "4", "id@x.co",
            self._creds("slot-rt", base + 2000, access_expires_at=self._ms(2)),
            uuid="uuid-4",
        )
        entry_id = switcher._store._write_unclaimed_credential(
            self._creds("row-rt", base, access_expires_at=self._ms(1)),
            {"reason": "foreign", "resolvedIdentity": {"uuid": "uuid-4"}},
        )
        with caplog.at_level(logging.INFO, logger="claude-swap"):
            switcher._sweep_unclaimed_stash(live_slots={"4"})
        assert entry_id not in switcher.list_unclaimed_credentials()
        assert any(
            "Account-4" in r.getMessage() and "newer generation" in r.getMessage()
            for r in caplog.records
        ), "stamps within the jitter window must read as a rotation, not a login"

    def test_sweep_keeps_a_same_generation_row_when_its_own_expiry_is_later(
        self, temp_home, caplog,
    ):
        """Arm C, within jitter: ``expiresAt`` orders generations of one
        lineage (``credentials._fresher_plaintext_login``'s own rule). A row
        whose ``expiresAt`` is LATER than the live slot's backup is the later
        rotation and the only unspent refresh grant for it — dropping it on
        sign-blind stamp order alone would destroy that sole surviving copy."""
        import logging

        switcher = self._switcher(temp_home)
        base = self._ms(10)
        self._add_slot(
            switcher, "4", "id@x.co",
            self._creds("slot-rt", base + 2000, access_expires_at=self._ms(1)),
            uuid="uuid-4",
        )
        entry_id = switcher._store._write_unclaimed_credential(
            self._creds("row-rt", base, access_expires_at=self._ms(2)),
            {"reason": "foreign", "resolvedIdentity": {"uuid": "uuid-4"}},
        )
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            switcher._sweep_unclaimed_stash(live_slots={"4"})
        assert entry_id in switcher.list_unclaimed_credentials(), (
            "the row's expiresAt is later than the slot's: it is the newer "
            "rotation and the only unspent refresh grant"
        )
        assert any(
            "unclaimed credential" in r.getMessage() for r in caplog.records
        ), "a kept row within the jitter window still joins the surfaced kept set"

    def test_sweep_keeps_a_superseded_identity_when_the_slot_is_not_live(
        self, temp_home,
    ):
        """Arm C needs the slot MEASURED LIVE this pass; a slot not in the
        live set (dead is None or True) must not condemn the row."""
        switcher = self._switcher(temp_home)
        self._add_slot(
            switcher, "4", "id@x.co", self._creds("slot-rt", self._ms(30)),
            uuid="uuid-4",
        )
        entry_id = switcher._store._write_unclaimed_credential(
            self._creds("row-rt", self._ms(10)),
            {"reason": "foreign", "resolvedIdentity": {"uuid": "uuid-4"}},
        )
        switcher._sweep_unclaimed_stash()  # no live slots passed
        assert entry_id in switcher.list_unclaimed_credentials(), (
            "a slot not confirmed live this pass must not condemn the row"
        )

    def test_sweep_keeps_a_superseded_identity_when_the_matched_slot_is_not_live(
        self, temp_home, caplog,
    ):
        """``live_slots`` non-empty is not enough: it must contain the SLOT
        THE ROW MATCHES. Slot 5 is live and unrelated; slot 4, which holds
        the row's identity, was not measured live this pass."""
        import logging

        switcher = self._switcher(temp_home)
        self._add_slot(
            switcher, "4", "id@x.co", self._creds("slot-rt", self._ms(30)),
            uuid="uuid-4",
        )
        self._add_slot(
            switcher, "5", "other@x.co", self._creds("other-rt", self._ms(30)),
            uuid="uuid-5",
        )
        entry_id = switcher._store._write_unclaimed_credential(
            self._creds("row-rt", self._ms(10)),
            {"reason": "foreign", "resolvedIdentity": {"uuid": "uuid-4"}},
        )
        with caplog.at_level(logging.INFO, logger="claude-swap"):
            switcher._sweep_unclaimed_stash(live_slots={"5"})
        assert entry_id in switcher.list_unclaimed_credentials(), (
            "slot 5 being live is irrelevant; slot 4, the row's matched "
            "identity, was not confirmed live this pass"
        )
        assert not any(
            "Dropped" in r.getMessage() for r in caplog.records
        ), "nothing was positively condemned"

    def test_sweep_surfaces_a_newer_login_only_when_the_kept_set_changes(
        self, temp_home, caplog,
    ):
        """The row IS the newer login, so it is kept — but the kept-pile
        warning must fire once, stay silent while the kept set is unchanged,
        and fire again once it changes."""
        import logging

        switcher = self._switcher(temp_home)
        self._add_slot(
            switcher, "4", "id@x.co", self._creds("slot-rt", self._ms(5)),
            uuid="uuid-4",
        )
        switcher._store._write_unclaimed_credential(
            self._creds("row-rt", self._ms(30)),
            {"reason": "foreign", "resolvedIdentity": {"uuid": "uuid-4"}},
        )
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            switcher._sweep_unclaimed_stash(live_slots={"4"})
        warnings = [
            r for r in caplog.records if "unclaimed credential" in r.getMessage()
        ]
        assert len(warnings) == 1

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            switcher._sweep_unclaimed_stash(live_slots={"4"})
        assert not any(
            "unclaimed credential" in r.getMessage() for r in caplog.records
        ), "an unchanged kept set must not warn again"

        switcher._store._write_unclaimed_credential(
            self._creds("no-expiry-rt"), {"reason": "displaced-live-login"},
        )
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            switcher._sweep_unclaimed_stash(live_slots={"4"})
        assert any(
            "unclaimed credential" in r.getMessage() for r in caplog.records
        ), "a changed kept set must warn again"

    def test_sweep_keeps_a_row_with_no_resolved_identity_regardless_of_configslot(
        self, temp_home,
    ):
        """``configSlot`` names where a row was written FROM, not an identity
        it carries — a displaced file could be another account's login, so
        only ``resolvedIdentity`` licenses arm C."""
        switcher = self._switcher(temp_home)
        self._add_slot(
            switcher, "4", "id@x.co", self._creds("slot-rt", self._ms(30)),
            uuid="uuid-4",
        )
        entry_id = switcher._store._write_unclaimed_credential(
            self._creds("row-rt", self._ms(10)),
            {"reason": "foreign", "configSlot": "4"},
        )
        switcher._sweep_unclaimed_stash(live_slots={"4"})
        assert entry_id in switcher.list_unclaimed_credentials(), (
            "configSlot names no identity; only resolvedIdentity licenses a drop"
        )

    def test_sweep_never_lets_arm_c_drop_a_consume_gate_row(self, temp_home):
        """Consumer: `_adopt_stashed_successor`. A consumedFp row names its
        owner (``configSlot``) and the generation it succeeds — the only
        writer meant to touch it is the adopt, never arm C's identity
        match, even when the row sits in the jitter window of a live slot
        that would otherwise read as the same-generation drop case.

        Both sides carry an ``expiresAt``, with the slot's strictly later,
        so the within-jitter drop WOULD fire here if the ``consumedFp``
        guard did not bail out before ever reaching that comparison — a row
        with no ``expiresAt`` on either side proves nothing about the guard,
        since the comparison bails on its own regardless of it.
        """
        switcher = self._switcher(temp_home)
        base = self._ms(10)
        self._add_slot(
            switcher, "4", "id@x.co",
            self._creds("slot-rt", base + 2000, access_expires_at=self._ms(2)),
            uuid="uuid-4",
        )
        entry_id = switcher._store._write_unclaimed_credential(
            self._creds("row-rt", base, access_expires_at=self._ms(1)),
            {
                "reason": "consume-gate-cas-conflict",
                "configSlot": "4",
                "consumedFp": "some-prior-generation-fp",
                "resolvedIdentity": {"uuid": "uuid-4"},
            },
        )
        switcher._sweep_unclaimed_stash(live_slots={"4"})
        assert entry_id in switcher.list_unclaimed_credentials(), (
            "a consumedFp row is _adopt_stashed_successor's; arm C must "
            "never drop it even when the identity match would otherwise fire"
        )

    def test_sweep_keeps_a_row_on_an_exact_expires_at_tie_within_jitter(
        self, temp_home,
    ):
        """`credentials._fresher_plaintext_login` uses strict ``>`` for this
        same comparison (``file_exp > kc_exp``): an exact ``expiresAt`` tie
        names two unordered generations, so the row must be kept, not
        dropped on a ``>=``."""
        switcher = self._switcher(temp_home)
        base = self._ms(10)
        tie = self._ms(2)
        self._add_slot(
            switcher, "4", "id@x.co",
            self._creds("slot-rt", base + 2000, access_expires_at=tie),
            uuid="uuid-4",
        )
        entry_id = switcher._store._write_unclaimed_credential(
            self._creds("row-rt", base, access_expires_at=tie),
            {"reason": "foreign", "resolvedIdentity": {"uuid": "uuid-4"}},
        )
        switcher._sweep_unclaimed_stash(live_slots={"4"})
        assert entry_id in switcher.list_unclaimed_credentials(), (
            "an exact expiresAt tie is unordered; the row must be kept"
        )

    def test_sweep_keeps_a_row_with_a_non_numeric_expires_at_instead_of_crashing(
        self, temp_home,
    ):
        """A stash row's bytes are foreign by construction: a row whose
        ``expiresAt`` is a string reaching arm C's within-jitter branch must
        reach no verdict, not raise ``TypeError`` out of the whole sweep and
        strand every entry ordered after it in the same pass."""
        switcher = self._switcher(temp_home)
        base = self._ms(10)
        self._add_slot(
            switcher, "4", "id@x.co",
            self._creds("slot-rt", base + 2000, access_expires_at=self._ms(2)),
            uuid="uuid-4",
        )
        bad = switcher._store._write_unclaimed_credential(
            json.dumps({"claudeAiOauth": {
                "accessToken": "sk-a", "refreshToken": "row-rt",
                "refreshTokenExpiresAt": base, "expiresAt": "not-a-number",
            }}),
            {"reason": "foreign", "resolvedIdentity": {"uuid": "uuid-4"}},
        )
        doomed = switcher._store._write_unclaimed_credential(
            self._creds("dead-rt", self._ms(-1), access_expires_at=self._ms(-1)),
            {"reason": "foreign"},
        )
        switcher._sweep_unclaimed_stash(live_slots={"4"})
        assert bad in switcher.list_unclaimed_credentials(), (
            "a non-numeric expiresAt must reach no verdict, not a crash"
        )
        assert doomed not in switcher.list_unclaimed_credentials(), (
            "a row after the malformed one must still be swept in the same pass"
        )

    def test_sweep_drops_a_credential_a_slot_already_stores(
        self, temp_home, caplog,
    ):
        """Arm 2: a duplicate. The fingerprint is the refresh-token hash, so a
        rotated access token on the same lineage still reads as the same
        credential — byte equality would miss it."""
        import logging

        switcher = self._switcher(temp_home)
        self._add_slot(
            switcher, "3", "a@b.c",
            self._creds("shared-rt", self._ms(20), access="sk-stored"),
        )
        entry_id = switcher._store._write_unclaimed_credential(
            self._creds("shared-rt", self._ms(20), access="sk-stashed"),
            {"reason": "foreign"},
        )
        with caplog.at_level(logging.INFO, logger="claude-swap"):
            switcher._sweep_unclaimed_stash()
        assert entry_id not in switcher.list_unclaimed_credentials()
        assert any(
            entry_id in r.getMessage() and "Account-3" in r.getMessage()
            for r in caplog.records
        ), "a drop must name the slot that made it redundant"

    def test_sweep_keeps_an_unattributable_credential_and_says_so(
        self, temp_home, caplog,
    ):
        """Arm 3, the one that matters. The bytes name no owner, so a guess
        destroys the only copy of some account's login — and keeping silently
        rebuilds the state this sweep exists to end."""
        import logging

        switcher = self._switcher(temp_home)
        self._add_slot(
            switcher, "1", "a@b.c", self._creds("slot-1-rt", self._ms(20)),
        )
        entry_id = switcher._store._write_unclaimed_credential(
            self._creds("nobody-knows-rt", self._ms(20)), {"reason": "foreign"},
        )
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            switcher._sweep_unclaimed_stash()
        assert entry_id in switcher.list_unclaimed_credentials()
        assert any(
            "1 unclaimed credential" in r.getMessage()
            for r in caplog.records
        ), "a kept pile nobody knows about is the state we are in now"

    def test_sweep_keeps_a_credential_with_no_refresh_expiry(self, temp_home):
        """An OAuth blob whose ``refreshTokenExpiresAt`` is absent — the shape
        every undated vintage and every setup token shares. Arm 1 reaches no
        verdict on it, and no verdict is KEEP."""
        switcher = self._switcher(temp_home)
        entry_id = switcher._store._write_unclaimed_credential(
            self._creds("no-expiry-rt"), {"reason": "displaced-live-login"},
        )
        switcher._sweep_unclaimed_stash()
        assert entry_id in switcher.list_unclaimed_credentials()

    def test_stashing_a_credential_sweeps_the_pile(self, temp_home):
        """WHERE it runs: the pile grows here and only here, so this is the
        seam that bounds it without costing every tick a scan."""
        switcher = self._switcher(temp_home)
        stale = switcher._store._write_unclaimed_credential(
            self._creds(
                "dead-rt", self._ms(-1), access_expires_at=self._ms(-1),
            ), {"reason": "foreign"},
        )
        fresh = switcher._stash_live_credential(
            self._creds("fresh-rt", self._ms(20)), "foreign", "1", None,
        )
        assert stale not in switcher.list_unclaimed_credentials()
        assert fresh in switcher.list_unclaimed_credentials(), (
            "the stash swept away the entry it had just created — that entry "
            "is the licence the caller overwrites the live store on"
        )

    def test_a_stashed_newer_login_survives_its_own_stash_and_the_next_collect_pass(
        self, temp_home,
    ):
        """Consumer: the switch path, `_stash_live_credential` (inside
        `_perform_switch`'s lock). The row it just wrote IS the newer login
        of its identity, so it must survive two sweeps: its own (arm C gets
        no ``live_slots`` there, so it cannot fire at all) and the very next
        collector-pass sweep, whose ``live_slots`` now includes the row's
        own ``configSlot`` — the one case this whole design surfaces to a
        human instead of purging."""
        switcher = self._switcher(temp_home)
        self._add_slot(
            switcher, "1", "a@b.c", self._creds("slot-rt", self._ms(5)),
            uuid="uuid-1",
        )
        fresh = switcher._stash_live_credential(
            self._creds("fresh-rt", self._ms(30)), "foreign", "1",
            {"uuid": "uuid-1"},
        )
        assert fresh in switcher.list_unclaimed_credentials(), (
            "arm C must not fire during the stash's own sweep: it was "
            "handed no live_slots to compare against"
        )

        # The next collect pass measures slot 1 live and hands it to the
        # sweep — the row is still the NEWER login, so it must stay.
        switcher._sweep_unclaimed_stash(live_slots={"1"})
        assert fresh in switcher.list_unclaimed_credentials(), (
            "the row is the newer login of its identity; arm C only drops "
            "a row the slot has already superseded, never this one"
        )

    def test_a_failed_sweep_never_fails_the_stash(self, temp_home):
        """A successful stash is the licence to overwrite the live store, so
        housekeeping raising here would abort the switch it just protected."""
        switcher = self._switcher(temp_home)
        with patch.object(
            switcher, "_sweep_unclaimed_stash", side_effect=OSError("disk full"),
        ):
            entry_id = switcher._stash_live_credential(
                self._creds("fresh-rt", self._ms(20)), "foreign", "1", None,
            )
        assert entry_id in switcher.list_unclaimed_credentials()

    def test_the_containment_does_not_swallow_the_real_store_guard(
        self, temp_home,
    ):
        """`RealStoreWriteBlocked` is deliberately not an OSError so that no
        handler in the source can absorb it (conftest states this). Assert on
        THAT class, not a stand-in: "every non-OSError propagates" is a
        different and much stronger property, and pinning it forbids
        containing the ordinary failures this method must contain."""
        from tests.conftest import RealStoreWriteBlocked

        switcher = self._switcher(temp_home)
        with patch.object(
            switcher, "_sweep_unclaimed_stash",
            side_effect=RealStoreWriteBlocked("guard"),
        ), pytest.raises(RealStoreWriteBlocked):
            switcher._stash_live_credential(
                self._creds("fresh-rt", self._ms(20)), "foreign", "1", None,
            )

    def test_a_torn_roster_during_the_sweep_never_fails_the_stash(
        self, temp_home, caplog,
    ):
        """The sweep reads the roster to build its fingerprint map, and
        `_get_sequence_data` is `strict=True` — a torn or unreadable
        sequence.json raises `ConfigError`, which is NOT an OSError. Letting
        it escape reports a failed stash for an entry that is already on
        disk, and every caller aborts the switch on that; the retry then
        writes another row, growing the pile this sweep exists to bound."""
        switcher = self._switcher(temp_home)
        switcher._store._write_unclaimed_credential(
            self._creds("older-rt", self._ms(20)), {"reason": "foreign"},
        )
        switcher.sequence_file.write_text("{ this is not json")

        import logging

        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            entry_id = switcher._stash_live_credential(
                self._creds("fresh-rt", self._ms(20)), "foreign", "1", None,
            )
        assert entry_id in switcher.list_unclaimed_credentials(), (
            "a torn roster turned a successful stash into a failed one"
        )
        # Pins that the SWEEP ran and its containment fired. Without this,
        # deleting the sweep call entirely leaves this test green.
        assert any(
            "sweeping the unclaimed stash" in r.getMessage()
            for r in caplog.records
        ), "the containment arm was never reached"

    def test_a_consume_gate_entry_is_not_called_ownerless(self, temp_home, caplog):
        """The stash namespace is shared: the consume gate stores a minted
        successor here with `configSlot` and `consumedFp`, and
        `_adopt_stashed_successor` writes it back. Counting those in the
        "nothing in them names an owner" warning is false, and the `--purge`
        it recommends destroys the sole copy of a consumed generation."""
        import logging

        switcher = self._switcher(temp_home)
        switcher._store._write_unclaimed_credential(
            self._creds("successor-rt", self._ms(20)),
            {"configSlot": "1", "consumedFp": "sha256:abc"},
        )
        # THE CONTROL. Without a row the warning DOES fire for, the absence
        # asserted below passes for free the moment anyone rewords it.
        ownerless = switcher._store._write_unclaimed_credential(
            self._creds("nobody-knows-rt", self._ms(20)), {"reason": "foreign"},
        )
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            switcher._sweep_unclaimed_stash()
        owner_lines = [
            r.getMessage() for r in caplog.records
            if "names an owner" in r.getMessage()
        ]
        assert len(owner_lines) == 1 and "1 unclaimed credential" in owner_lines[0], (
            f"the consume-gate row was counted as ownerless: {owner_lines}"
        )
        assert ownerless in switcher.list_unclaimed_credentials()

    def test_a_consume_gate_entry_with_unreadable_bytes_is_still_owned(
        self, temp_home, caplog,
    ):
        """`_read_unclaimed_credential` returns "" for a locked volume, a
        mid-unmount, or a manifest row whose bytes the previous unlink already
        took — so a consume-gate row can reach the sweep with no fingerprint.
        Its MANIFEST still names `configSlot` and `consumedFp`, so calling it
        ownerless is false and the `--purge` that warning suggests destroys
        the only copy of a spent grant's successor."""
        import logging

        switcher = self._switcher(temp_home)
        entry_id = switcher._store._write_unclaimed_credential(
            self._creds("successor-rt", self._ms(20)),
            {"reason": "consume-gate-successor", "configSlot": "1",
             "consumedFp": "sha256:abc", "fingerprint": "sha256:def"},
        )
        switcher._store._stash_entry_path(entry_id).unlink()
        assert switcher._store._read_unclaimed_credential(entry_id) == ("", False), (
            "premise: the bytes must be gone while the manifest row remains"
        )
        doomed = switcher._store._write_unclaimed_credential(
            self._creds(
                "dead-rt", self._ms(-1), access_expires_at=self._ms(-1),
            ), {"reason": "foreign"},
        )
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            switcher._sweep_unclaimed_stash()
        assert doomed not in switcher.list_unclaimed_credentials(), (
            "PREMISE: the sweep reaches and drops an expired entry here"
        )
        assert not any(
            "names an owner" in r.getMessage() for r in caplog.records
        ), "a row naming configSlot was counted as ownerless"
        # The absence above is satisfied just as well by DROPPING the row, so
        # it cannot stand alone: this is the requirement the test is named for.
        assert entry_id in switcher.list_unclaimed_credentials(), (
            "the sweep dropped a row it could not positively condemn"
        )

    def test_collect_usage_entries_survives_an_undecodable_unclaimed_credential(
        self, temp_home,
    ):
        """`_read_unclaimed_credential`'s ``read_text`` raised
        ``UnicodeDecodeError`` (a ``ValueError``) on non-UTF-8 bytes,
        escaping the sweep's per-entry loop AND both callers' containment
        (neither tuple carried ``ValueError``) -- so a single bad
        ``.unclaimed-*.enc`` took `cswap list`/`--status`/the TUI's 3s
        snapshot down with it."""
        switcher = self._switcher(temp_home)
        bad = switcher._store._write_unclaimed_credential(
            self._creds("row-rt", self._ms(20)), {"reason": "foreign"},
        )
        switcher._store._stash_entry_path(bad).write_bytes(b"\xff\xfe\x00bad")
        doomed = switcher._store._write_unclaimed_credential(
            self._creds("dead-rt", self._ms(-1), access_expires_at=self._ms(-1)),
            {"reason": "foreign"},
        )
        entries = switcher._collect_usage_entries([], fetch=set())
        assert entries == {}
        remaining = switcher.list_unclaimed_credentials()
        assert doomed not in remaining, "the doomed row must still drop"
        assert bad in remaining, "an undecodable row has no verdict: KEEP"
        creds, _ = switcher._store._read_unclaimed_credential(bad)
        assert creds == "", "undecodable bytes must read as empty, not raise"

    def test_collect_usage_entries_survives_a_non_finite_refreshTokenExpiresAt(
        self, temp_home,
    ):
        """``1e400`` is a legal JSON number that ``json.loads`` turns into
        ``inf``; arm A's own expiry check must not raise on it and take
        every row sorted after it down with it."""
        switcher = self._switcher(temp_home)
        inf_id = switcher._store._write_unclaimed_credential(
            self._creds("row-rt", self._ms(10)), {"reason": "foreign"},
        )
        doomed_id = switcher._store._write_unclaimed_credential(
            self._creds("dead-rt", self._ms(-1), access_expires_at=self._ms(-1)),
            {"reason": "foreign"},
        )
        first_id, second_id = sorted([inf_id, doomed_id])
        switcher._store._atomic_b64_write(
            switcher._store._stash_entry_path(first_id),
            '{"claudeAiOauth": {"accessToken": "sk-a", "refreshToken": '
            '"row-rt", "refreshTokenExpiresAt": 1e400}}',
        )
        switcher._store._atomic_b64_write(
            switcher._store._stash_entry_path(second_id),
            self._creds("dead-rt", self._ms(-1), access_expires_at=self._ms(-1)),
        )
        entries = switcher._collect_usage_entries([], fetch=set())
        assert entries == {}
        remaining = switcher.list_unclaimed_credentials()
        assert first_id in remaining, "a non-finite stamp has no positive verdict: KEEP"
        assert second_id not in remaining, "the row sorted after it must still drop"

    def test_collect_usage_entries_survives_an_oversized_int_refreshTokenExpiresAt(
        self, temp_home,
    ):
        """An oversized JSON integer stamp is legal JSON that ``json.loads``
        keeps as a Python ``int``; arm A's own expiry check must not raise on
        it and take every row sorted after it down with it."""
        switcher = self._switcher(temp_home)
        huge_id = switcher._store._write_unclaimed_credential(
            self._creds("row-rt", self._ms(10)), {"reason": "foreign"},
        )
        doomed_id = switcher._store._write_unclaimed_credential(
            self._creds("dead-rt", self._ms(-1), access_expires_at=self._ms(-1)),
            {"reason": "foreign"},
        )
        first_id, second_id = sorted([huge_id, doomed_id])
        switcher._store._atomic_b64_write(
            switcher._store._stash_entry_path(first_id),
            '{"claudeAiOauth": {"accessToken": "sk-a", "refreshToken": '
            '"row-rt", "refreshTokenExpiresAt": 1' + "0" * 400 + '}}',
        )
        switcher._store._atomic_b64_write(
            switcher._store._stash_entry_path(second_id),
            self._creds("dead-rt", self._ms(-1), access_expires_at=self._ms(-1)),
        )
        entries = switcher._collect_usage_entries([], fetch=set())
        assert entries == {}
        remaining = switcher.list_unclaimed_credentials()
        assert first_id in remaining, "an oversized int stamp has no positive verdict: KEEP"
        assert second_id not in remaining, "the row sorted after it must still drop"

    @pytest.mark.parametrize("accounts", [
        None, "not-a-map", ["1"], {"1": None}, {"2": "not-a-dict"},
    ], ids=["null-map", "str-map", "list-map", "null-row", "wrong-type-row"])
    def test_a_malformed_roster_never_fails_the_stash(
        self, temp_home, caplog, accounts,
    ):
        """Each of these is valid JSON AND the top level is a dict, so it
        sails past `_read_json`'s type check and only blows up further down.
        The containment catches that, but being CAUGHT is not being HANDLED:
        a sweep that dies on entry one leaves every later entry unswept while
        the stash still reports success."""
        import logging

        switcher = self._switcher(temp_home)
        doomed = switcher._store._write_unclaimed_credential(
            self._creds(
                "dead-rt", self._ms(-1), access_expires_at=self._ms(-1),
            ), {"reason": "foreign"},
        )
        switcher._write_json(switcher.sequence_file, {"accounts": accounts})

        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            entry_id = switcher._stash_live_credential(
                self._creds("fresh-rt", self._ms(20)), "foreign", "1", None,
            )
        assert entry_id in switcher.list_unclaimed_credentials(), (
            "a malformed roster turned a successful stash into a failed one"
        )
        # ORDER-INDEPENDENT. Entry ids sort by a sha256 of their own bytes, so
        # "the expired sibling was dropped" only proves the sweep finished on
        # the ~half of runs where that sibling sorts second. The containment
        # is silent exactly when nothing aborted, whatever the order.
        assert not any(
            "sweeping the unclaimed stash" in r.getMessage()
            for r in caplog.records
        ), "the sweep aborted and was merely contained"
        assert doomed not in switcher.list_unclaimed_credentials(), (
            "the sweep did not reach the expired entry"
        )

    def test_a_malformed_row_does_not_abandon_the_rest_of_the_roster(
        self, temp_home,
    ):
        """The shape guard SKIPS a bad row; it must not stop reading. Every
        other malformed-roster case here has one row, so `continue` and
        `break` are indistinguishable in them — and a `break` would silently
        disable the duplicate arm for every slot ordered after the bad row,
        leaving real duplicates in the stash forever."""
        switcher = self._switcher(temp_home)
        creds = self._creds("shared-rt", self._ms(20), access="sk-stored")
        data = switcher._get_sequence_data()
        data.setdefault("accounts", {})["1"] = None          # skipped
        data["accounts"]["2"] = {"email": "a@b.c", "organizationUuid": ""}
        switcher._write_json(switcher.sequence_file, data)
        switcher._store._write_account_credentials("2", "a@b.c", creds)

        entry_id = switcher._store._write_unclaimed_credential(
            self._creds("shared-rt", self._ms(20), access="sk-stashed"),
            {"reason": "foreign"},
        )
        switcher._sweep_unclaimed_stash()
        assert entry_id not in switcher.list_unclaimed_credentials(), (
            "the roster read stopped at the malformed row, so slot 2's "
            "fingerprint never entered the map and its duplicate was kept"
        )

    def test_the_sweep_reads_no_slot_when_nothing_can_be_dropped(
        self, temp_home,
    ):
        """It runs inside `_perform_switch`'s three-lock block, whose comment
        forbids anything slow. Only the duplicate arm needs the roster's
        fingerprints, and on macOS each one is a `security` subprocess — so a
        sweep with nothing to compare must not pay for them."""
        switcher = self._switcher(temp_home)
        self._add_slot(switcher, "1", "a@b.c", self._creds("s1", self._ms(20)))
        switcher._store._write_unclaimed_credential(
            self._creds("dead-rt", self._ms(-1), access_expires_at=self._ms(-1)),
            {"reason": "foreign"},
        )
        with patch.object(
            switcher, "_read_account_credentials",
            side_effect=AssertionError("read a slot backup with nothing to compare"),
        ):
            switcher._sweep_unclaimed_stash()

    def test_a_row_written_after_the_sweeps_snapshot_is_never_touched(
        self, temp_home,
    ):
        """Consumer: the store (`_write_unclaimed_credential` /
        `_remove_unclaimed_credential`, credentials.py). The sweep decides
        off the LIST it takes once at the top; a row a concurrent writer
        lands AFTER that list was read is not in the snapshot the loop
        iterates, so it survives even though the same arm would drop it on
        the very next pass. Each row's own read-modify-write cycle is
        already serialized by `_mutate_stash_manifest`'s manifest lock —
        this pins that the sweep never widens that to "the whole sweep",
        which would instead have to notice and drop the late row too."""
        switcher = self._switcher(temp_home)
        doomed = switcher._store._write_unclaimed_credential(
            self._creds("dead-rt", self._ms(-1), access_expires_at=self._ms(-1)),
            {"reason": "foreign"},
        )
        real_list = switcher._store._list_unclaimed_credentials
        late = {}

        def _list_then_a_concurrent_writer_lands_a_row(*a, **kw):
            entries = real_list(*a, **kw)
            # Fires once: a mock's side_effect re-runs on every call the
            # sweep makes, and re-arming it on a later (fault-injected) call
            # would land yet another row that never entered ANY snapshot,
            # masking a widened re-list instead of being caught by it.
            if "id" not in late:
                late["id"] = switcher._store._write_unclaimed_credential(
                    self._creds(
                        "dead-rt-2", self._ms(-1), access_expires_at=self._ms(-1),
                    ),
                    {"reason": "foreign"},
                )
            return entries

        with patch.object(
            switcher._store, "_list_unclaimed_credentials",
            side_effect=_list_then_a_concurrent_writer_lands_a_row,
        ):
            switcher._sweep_unclaimed_stash()

        assert doomed not in switcher.list_unclaimed_credentials()
        assert late["id"] in switcher.list_unclaimed_credentials(), (
            "a row written after the sweep's snapshot must not be touched "
            "by that sweep, even though the drop arm it qualifies for fired "
            "on a sibling this same pass"
        )

    def test_sweep_makes_no_network_call_across_arms_a_b_and_c(self, temp_home):
        """The sweep's own design: liveness comes from the collector's
        THIS-PASS verdict, never a probe of its own — a 401 on a stashed
        bearer is never positive evidence (arm A needs BOTH tokens expired
        by their own stamps), so the sweep must never mint a refresh or GET
        anything, on any of its three drop arms."""
        switcher = self._switcher(temp_home)
        self._add_slot(
            switcher, "1", "b@x.co", self._creds("slot-1-rt", self._ms(20)),
        )
        self._add_slot(
            switcher, "4", "id@x.co", self._creds("slot-4-rt", self._ms(30)),
            uuid="uuid-4",
        )
        # Arm A: both tokens of its own expired.
        switcher._store._write_unclaimed_credential(
            self._creds("dead-rt", self._ms(-1), access_expires_at=self._ms(-1)),
            {"reason": "foreign"},
        )
        # Arm B: fingerprint a managed slot already stores.
        switcher._store._write_unclaimed_credential(
            self._creds("slot-1-rt", self._ms(20)), {"reason": "foreign"},
        )
        # Arm C: resolvedIdentity names a live slot with a newer generation.
        switcher._store._write_unclaimed_credential(
            self._creds("row-rt", self._ms(10)),
            {"reason": "foreign", "resolvedIdentity": {"uuid": "uuid-4"}},
        )
        with patch(
            "urllib.request.urlopen",
            side_effect=AssertionError("the sweep must never touch the network"),
        ):
            switcher._sweep_unclaimed_stash(live_slots={"4"})
        assert switcher.list_unclaimed_credentials() == {}, (
            "all three drop arms must have fired with no network call"
        )

    def test_collect_usage_entries_sweeps_the_stash_with_this_pass_live_slots(
        self, temp_home,
    ):
        """The stash-time call at ``_stash_live_credential`` sees no live
        slots at all; only the collector, which measures liveness every
        tick, can hand arm C a slot to compare against."""
        switcher = self._switcher(temp_home)
        live_creds = self._creds("live-rt", self._ms(30))
        live_info = [(2, "live@x.co", "Org", "", False, live_creds, "")]
        with patch.object(switcher, "_sweep_unclaimed_stash") as sweep:
            switcher._collect_usage_entries(live_info, fetch=set())
        sweep.assert_called_once_with(
            live_slots={"2"}, slot_creds={"2": ("live@x.co", live_creds)},
        )

        # A pass where the slot's own verdict came back dead: the live set
        # is empty, but the sweep still runs — arm A still drains, arm C
        # simply drains nothing this pass.
        from claude_swap.usage_store import FetchRecord

        switcher._usage_store.record(
            {"3": FetchRecord(error="invalid_grant")}, {"3": ("dead@x.co", "")},
        )
        dead_creds = json.dumps({"claudeAiOauth": {
            "accessToken": "at", "refreshToken": "rt", "expiresAt": 1,
        }})
        dead_info = [(3, "dead@x.co", "Org", "", False, dead_creds, "")]
        with patch.object(switcher, "_sweep_unclaimed_stash") as sweep2:
            switcher._collect_usage_entries(dead_info, fetch=set())
        sweep2.assert_called_once_with(
            live_slots=set(), slot_creds={"3": ("dead@x.co", dead_creds)},
        )

    def test_sweep_uses_this_passs_already_read_credentials_when_given(
        self, temp_home,
    ):
        """The collect site already read every slot's credentials via
        `_build_accounts_info`; handing them to the sweep as `slot_creds`
        must save `_slot_fingerprints` a second per-slot store read."""
        switcher = self._switcher(temp_home)
        self._add_slot(
            switcher, "1", "a@b.c", self._creds("shared-rt", self._ms(20)),
        )
        self._add_slot(
            switcher, "4", "id@x.co", self._creds("slot-rt", self._ms(30)),
            uuid="uuid-4",
        )
        dup = switcher._store._write_unclaimed_credential(
            self._creds("shared-rt", self._ms(20)), {"reason": "foreign"},
        )
        identity_row = switcher._store._write_unclaimed_credential(
            self._creds("row-rt", self._ms(10)),
            {"reason": "foreign", "resolvedIdentity": {"uuid": "uuid-4"}},
        )
        slot_creds = {
            "1": ("a@b.c", self._creds("shared-rt", self._ms(20))),
            "4": ("id@x.co", self._creds("slot-rt", self._ms(30))),
        }
        with patch.object(
            switcher, "_read_account_credentials",
            side_effect=AssertionError(
                "must not re-read a slot the caller already handed in"
            ),
        ):
            switcher._sweep_unclaimed_stash(
                live_slots={"4"}, slot_creds=slot_creds,
            )
        assert dup not in switcher.list_unclaimed_credentials(), (
            "arm B (duplicate fingerprint) must still fire off slot_creds"
        )
        assert identity_row not in switcher.list_unclaimed_credentials(), (
            "arm C (newer login) must still fire off slot_creds"
        )

    def test_active_account_usage_never_sweeps(self, temp_home):
        """A single-slot pass must not sweep: a one-slot `slot_creds` map
        would make arms B/C keep rows a full pass drops, flickering the
        kept-set warning."""
        from claude_swap.credentials import ActiveCredentials

        switcher = self._switcher(temp_home)
        with patch.object(
            switcher, "_read_active_credentials",
            return_value=ActiveCredentials("", False, False),
        ), patch.object(
            switcher, "_sweep_unclaimed_stash",
            side_effect=AssertionError("the single-slot pass must never sweep"),
        ):
            switcher._active_account_usage("1", "a@b.c", "")

    def test_a_failed_sweep_never_fails_the_collect_pass(self, temp_home, caplog):
        """Contained exactly like the stash-time call
        (``test_a_failed_sweep_never_fails_the_stash``): the collector calls
        the sweep on every tick, so a torn roster or store must not take
        usage collection down with it."""
        import logging

        switcher = self._switcher(temp_home)
        live_creds = self._creds("live-rt", self._ms(30))
        live_info = [(2, "live@x.co", "Org", "", False, live_creds, "")]
        with patch.object(
            switcher, "_sweep_unclaimed_stash", side_effect=OSError("disk full"),
        ), caplog.at_level(logging.WARNING, logger="claude-swap"):
            entries = switcher._collect_usage_entries(live_info, fetch=set())
        assert "2" in entries, "usage collection must survive a failed sweep"
        assert any(
            "sweeping the unclaimed stash" in r.getMessage()
            for r in caplog.records
        ), "the containment arm was never reached"


@pytest.mark.usefixtures("_ex_reads_what_the_plain_reader_returns")
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
        ), pytest.raises(OSError, match="disk full"):
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

    def test_status_path_does_not_condemn_a_healed_slot_on_degraded_read(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, monkeypatch,
    ):
        """Round 11 C1, `:4768` variant: `_active_account_usage` (the
        `--status` single-slot path) sets `self._active_read_degraded` and
        then calls the shared `_collect_usage_entries`, so it must inherit
        the same fix as the `--list`/collector path above. Same scenario as
        `test_collector_own_active_read_does_not_condemn_a_healed_slot_on_degraded_read`
        (TestActiveSlotStrikeParity), driven through `_active_account_usage`
        instead of `_build_accounts_info`."""
        from claude_swap.usage_store import FetchRecord as FR
        sample_sequence_data["accounts"]["2"]["email"] = "b@example.com"
        s = ClaudeAccountSwitcher()
        s.platform = Platform.MACOS
        s._setup_directories()
        s._write_json(s.sequence_file, sample_sequence_data)
        old_gen = json.dumps({
            "claudeAiOauth": {"accessToken": "sk-old",
                              "refreshToken": "rt-old", "expiresAt": 1000}})
        new_gen = json.dumps({
            "claudeAiOauth": {"accessToken": "sk-new", "refreshToken": "rt-new",
                              "expiresAt": 99999999999000}})
        idents = {"2": ("b@example.com", "")}
        s._usage_store.record(
            {"2": FR(error="invalid_grant",
                     struck_fp=oauth.credential_fingerprint(old_gen))},
            idents,
        )
        s._write_account_credentials("2", "b@example.com", new_gen)  # healed
        monkeypatch.setattr(
            s, "_read_active_credentials",
            lambda: ActiveCredentials(old_gen, False, True),  # degraded, stale
        )
        entry = s._active_account_usage("2", "b@example.com", "")
        assert entry.sentinel != USAGE_RELOGIN_REQUIRED, (
            "C1 (round 11) regression on the --status path: an already-"
            f"healed slot was condemned on a degraded read, sentinel={entry.sentinel!r}"
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

    def test_a_lock_free_heal_does_not_void_a_concurrent_live_claim(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        """MAJOR-1/2 repro: the auto view's steady state, every 3s, no stop
        involved.

        Sequence measured by the round-4 review (`test_probe_C`):

            T (TUI store-only poll)  entries() lock-free -> sees strikes
            E (engine collector)     clear_dead_token -> strikes=0;
                                      reserve() -> wins claim C_E
            E                        ...on the network...
            T                        clear_dead_token on its STALE read ->
                                      claimId=None, wiping C_E
            E                        record(C_E) -> fenced out; the fetch is
                                      DISCARDED

        T's decision to heal is made on its own `entries()` read, captured
        BEFORE E's claim exists on disk; T's WRITE (`clear_dead_token`) lands
        AFTER E has already claimed. Reproduced by intercepting T's write
        call and running E's actions inside it -- exactly the ordering the
        review measured, not a race that merely looks similar.
        """
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
        identities = {"2": ("b@example.com", "")}
        store = s._usage_store
        struck_claims = store.reserve(["2"], identities, respect_plans=False)
        store.record(
            {"2": StoreRecord(error="invalid_grant",
                              struck_fp=oauth.credential_fingerprint(dead))},
            identities, struck_claims,
        )
        # The credential heals (fresh lineage) -- T's read (below) sees the
        # strike as fingerprint-healed, so it takes the `elif` heal branch
        # rather than the `if` re-login-required branch.
        fresh = json.dumps({
            "claudeAiOauth": {"accessToken": "b", "refreshToken": "rt-new",
                              "expiresAt": 1000}})
        s._write_account_credentials("2", "b@example.com", fresh)

        engine_claims: dict[str, str] = {}
        real_clear = s._usage_store.clear_dead_token

        def engine_claims_then_clear(nums, idents, **kw):
            # T's write is about to land. Before it runs, E completes its
            # own heal+claim on the (still-struck, on-disk) row -- the
            # ordering the review measured: E acted between T's read and
            # T's write. E's own heal must not itself revoke anything (no
            # claim exists yet), so it goes straight to the store, not
            # through the method under test.
            struck = idents == identities and list(nums) == ["2"]
            assert struck, "premise: this is the row under test"
            store._mutate(idents, nums, lambda _n, row: row.update(
                authDeadStrikes=0, struckFingerprint=None,
                consecutiveFailures=0, lastError=None, backoffUntil=None,
            ))
            engine_claims.update(
                store.reserve(list(nums), idents, respect_plans=False)
            )
            assert engine_claims, "premise: E won a live claim before T wrote"
            # T's own write proceeds now, exactly as the unpatched call
            # would -- whatever kwargs the switcher itself passes (none
            # before the fix, `revoke_claim=False` after).
            return real_clear(nums, idents, **kw)

        s._usage_store.clear_dead_token = engine_claims_then_clear
        try:
            # T: the TUI's lock-free, no-network store-only poll.
            info = [(2, "b@example.com", "", "", False, fresh, "")]
            s._collect_usage_entries(info, fetch=set())
        finally:
            s._usage_store.clear_dead_token = real_clear

        # E returns from the network and records its outcome, fenced by the
        # claim it won above.
        accepted = store.record(
            {"2": StoreRecord(usage={"five_hour": {"pct": 12.0}})},
            identities, engine_claims,
        )
        assert accepted == {"2"}, (
            f"accepted={accepted} -- the engine's live claim was voided by "
            "T's lock-free heal, so record() fenced out its own measurement"
        )
        assert store.entries(identities)["2"].last_good == {
            "five_hour": {"pct": 12.0}
        }

    def test_lock_free_heal_revoke_claim_false_preserves_a_live_claim(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        """Isolates `revoke_claim=False` at the collector's own call site
        (brief I-list item 3 / review IMPORTANT-2, mutation L): a live claim
        pre-existing on the row BEFORE the lock-free heal runs must survive
        the heal. Unlike the concurrent-claim repro above, `struckFingerprint`
        never changes between the collector's read and its write here, so
        `expected_fingerprints` matches trivially either way -- this isolates
        `revoke_claim` specifically, which the other test's ordering could
        not (E's own raw mutation there breaks the fingerprint match first,
        so a `revoke_claim` mutation was masked)."""
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
        identities = {"2": ("b@example.com", "")}
        store = s._usage_store

        # A DIFFERENT collector (E) wins a live claim FIRST, while the row
        # is still unstruck -- `reserve()` refuses a struck row outright
        # (`_row_eligible` gates on authDeadStrikes), so the claim must
        # predate the strike, exactly as it would in the field (E started
        # fetching before the strike landed).
        live_claims = store.reserve(["2"], identities, respect_plans=False)
        assert live_claims, "premise: E wins a live claim before the strike"

        # The strike lands (a DIFFERENT fetch, or the same one failing) --
        # write it directly under the row so E's still-live claim is left
        # untouched by this step, mirroring `record()`'s own field-level
        # writes without consuming E's claim.
        store._mutate(identities, ["2"], lambda _n, row: row.update(
            authDeadStrikes=1,
            struckFingerprint=oauth.credential_fingerprint(dead),
            backoffUntil=None, consecutiveFailures=0, lastError="invalid_grant",
        ))
        # Heal the credential (fresh lineage) so the collector's read sees a
        # fingerprint-healed strike -- takes the `elif` heal branch.
        fresh = json.dumps({
            "claudeAiOauth": {"accessToken": "b", "refreshToken": "rt-new",
                              "expiresAt": 1000}})
        s._write_account_credentials("2", "b@example.com", fresh)

        info = [(2, "b@example.com", "", "", False, fresh, "")]
        s._collect_usage_entries(info, fetch=set())

        # The pre-existing claim must still be honorable: record() using it
        # must not be fenced out.
        accepted = store.record(
            {"2": StoreRecord(usage={"five_hour": {"pct": 7.0}})},
            identities, live_claims,
        )
        assert accepted == {"2"}, (
            f"accepted={accepted} -- revoke_claim mutated to True voided a "
            "live claim the lock-free heal has no credential change of its "
            "own to justify fencing"
        )

    def test_lock_free_heal_expected_fingerprints_skips_a_moved_row(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        """Isolates `expected_fingerprints` at the collector's own call site
        (brief I-list item 2 / review IMPORTANT-2, mutation K3): a strike
        that changed generation BETWEEN the collector's lock-free `entries()`
        read and its own `clear_dead_token` write must not be silently
        overwritten by that now-stale decision. Simulated by mutating the
        row's `struckFingerprint` to a DIFFERENT value directly on the
        store, inside a patched `_read_account_credentials_ex` that fires
        exactly once the collector's read has already happened -- so the
        write sees a row whose fingerprint no longer matches what the
        collector's decision was based on."""
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
        identities = {"2": ("b@example.com", "")}
        store = s._usage_store
        store.record(
            {"2": StoreRecord(error="invalid_grant",
                              struck_fp=oauth.credential_fingerprint(dead))},
            identities,
        )
        # Heal the credential (fresh lineage) so the collector's OWN read
        # sees a fingerprint-healed strike and decides to clear it.
        fresh = json.dumps({
            "claudeAiOauth": {"accessToken": "b", "refreshToken": "rt-new",
                              "expiresAt": 1000}})
        s._write_account_credentials("2", "b@example.com", fresh)

        # Between the collector's decision (made on its own lock-free
        # `entries()` read, captured above) and its write, A DIFFERENT
        # collector strikes the row again on a NEW generation -- landing in
        # the exact gap `expected_fingerprints` exists to close. Intercept
        # the write call itself (mirrors the concurrent-claim test's own
        # pattern above) so the race lands exactly where the real one would.
        new_strike_fp = "sha256:" + "f" * 64
        real_clear = store.clear_dead_token

        def strike_then_clear(nums, idents, **kw):
            store._mutate(idents, list(nums), lambda _n, row: row.update(
                authDeadStrikes=1, struckFingerprint=new_strike_fp,
            ))
            return real_clear(nums, idents, **kw)

        store.clear_dead_token = strike_then_clear
        try:
            info = [(2, "b@example.com", "", "", False, fresh, "")]
            s._collect_usage_entries(info, fetch=set())
        finally:
            store.clear_dead_token = real_clear

        # The row must still carry the NEW strike -- the collector's
        # stale-read heal must not have overwritten it.
        post = store.entries(identities)["2"]
        assert post.auth_dead_strikes >= 1, (
            "expected_fingerprints dropped: a fresh strike landing between "
            "the collector's read and its write was silently overwritten"
        )
        assert post.struck_fingerprint == new_strike_fp, (
            f"struck_fingerprint={post.struck_fingerprint!r}, expected "
            f"{new_strike_fp!r} to survive the stale-read heal"
        )

    def test_the_heal_does_not_erase_a_live_server_backoff(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        """C1: the collector's fingerprint-healed strike-clear must not wipe
        the server's 429 ``backoffUntil`` -- reachable from a no-network read
        the TUI performs every 3 seconds, re-opening a throttled token
        inside its own block.

        Reproduced through the real store, via the real
        ``_collect_usage_entries`` call path, with a CONTROL at each offset
        (a separate, un-healed row on the same clock) so the eligibility
        flip is attributed to the heal and not to the backoff's own expiry.

        Corrected boundary: ``_row_eligible`` also requires ``stale``
        (``now - fetchedAt > SERVE_TTL_S == 180``), so the earliest the
        control's own backoff-oblivious staleness could flip eligible is
        +181s, not +120s -- the reviewer's original number. At every offset
        the throttle itself (``backoffUntil``) must survive the heal
        unchanged; only display/fetch ELIGIBILITY may legitimately track
        staleness once trust_extended can no longer paper over it (a
        question for I2, not this test).
        """
        from claude_swap.usage_store import FetchRecord as StoreRecord

        assert SERVE_TTL_S == 180.0, "premise: brief's +181s boundary"

        class FakeClock:
            def __init__(self, now: float = 1_000_000.0):
                self.now = now

            def __call__(self) -> float:
                return self.now

            def advance(self, seconds: float) -> None:
                self.now += seconds

        sample_sequence_data["accounts"]["2"] = {
            "email": "b@example.com", "uuid": "u2",
            "organizationUuid": "", "organizationName": "",
        }
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        s._write_json(s.sequence_file, sample_sequence_data)
        clock = FakeClock()
        s._usage_store.clock = clock

        dead = json.dumps({
            "claudeAiOauth": {"accessToken": "a", "refreshToken": "rt-dead",
                              "expiresAt": 1000}})
        s._write_account_credentials("2", "b@example.com", dead)
        identities = {"2": ("b@example.com", "")}
        store = s._usage_store
        # A prior success, so `fetchedAt` is set and staleness is measured
        # from a real anchor rather than None (which is unconditionally
        # stale -- the point is to test the BOUNDARY, not the None case).
        store.record({"2": StoreRecord(usage={"five_hour": {"pct": 3.0}})},
                      identities)
        # Strike + a long server-side block (retry_after_s=1800), exactly
        # the shape a 429-adjacent invalid_grant leaves on the row. Unfenced
        # (no claim) -- the row was just successfully fetched a moment ago
        # and is neither stale nor poll-due, so `reserve()` would not win it
        # a claim; a real collector's own failed fetch writes through
        # `record()` fenced by ITS OWN prior claim, which is exactly this
        # unfenced shape once that claim has already been consumed by the
        # success above.
        accepted = store.record(
            {"2": StoreRecord(error="invalid_grant", retry_after_s=1800.0,
                               struck_fp=oauth.credential_fingerprint(dead))},
            identities,
        )
        assert accepted == {"2"}, "premise: the strike was recorded"
        row0 = store._read_rows()["2"]
        backoff_at_strike = row0["backoffUntil"]
        assert backoff_at_strike is not None
        assert backoff_at_strike - clock.now == pytest.approx(1800.0)

        # The credential heals (fresh lineage) -- the collector's read below
        # takes the fingerprint-healed `elif` branch, not re-login-required.
        fresh = json.dumps({
            "claudeAiOauth": {"accessToken": "b", "refreshToken": "rt-new",
                              "expiresAt": 1000}})
        s._write_account_credentials("2", "b@example.com", fresh)

        for advance_s, label in (
            (120.0, "+120s (still inside SERVE_TTL_S)"),
            (61.0, "+181s (past SERVE_TTL_S, still inside the 1800s block)"),
            (1519.0, "+1700s (still inside the 1800s block)"),
        ):
            clock.advance(advance_s)
            now = clock.now

            # CONTROL: an identically-clocked row that is never healed --
            # same struck state, same age. Read directly (no heal call).
            ctrl_row = dict(store._read_rows()["2"])
            ctrl_backoff = ctrl_row["backoffUntil"]
            ctrl_eligible = _row_eligible(ctrl_row, now, respect_plans=False)

            info = [(2, "b@example.com", "", "", False, fresh, "")]
            s._collect_usage_entries(info, fetch=set())

            heal_row = store._read_rows()["2"]
            heal_backoff = heal_row["backoffUntil"]
            heal_eligible = _row_eligible(heal_row, now, respect_plans=False)

            assert ctrl_backoff == backoff_at_strike, (
                f"{label}: control backoffUntil moved on its own -- the "
                "clock-offset premise is broken"
            )
            assert heal_backoff == backoff_at_strike, (
                f"{label}: the heal erased backoffUntil "
                f"({backoff_at_strike} -> {heal_backoff}) -- a throttled "
                "token was re-opened inside its own server-side block"
            )
            assert heal_eligible == ctrl_eligible, (
                f"{label}: heal eligibility ({heal_eligible}) diverged from "
                f"the un-healed control ({ctrl_eligible}) -- the heal must "
                "not itself flip fetch eligibility via the throttle field"
            )


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

    def test_a_failed_session_invalidation_never_unwinds_a_stored_credential(
        self, tmp_path
    ):
        """`_write_account_credentials` is two steps: the store write ADVANCES
        the slot, then session invalidation runs and can raise (EACCES on the
        session dir, a read-only mount). A raise escaping the wrapper is read
        by every caller as "the persist failed" for a slot that HOLDS the new
        credential — so the post-POST arm stashes a row byte-identical to the
        slot's live credential and demotes a successful refresh to `transient`,
        and `cswap run` prints "Could not refresh the token" for a refresh that
        worked.

        The invalidation is housekeeping: its job is to stop a session profile
        serving a superseded token, and `mark_session_stale` already exists for
        the case where the profile cannot be touched now. Losing it costs a
        re-bootstrap; losing the write's RETURN costs a live credential.

        Parametrized against the succeeding control so the row that matters
        cannot pass alone.
        """
        from claude_swap.session import STALE_MARKER
        from claude_swap.session import stale_marker_for

        for invalidation_raises in (False, True):
            sw = ClaudeAccountSwitcher.__new__(ClaudeAccountSwitcher)
            wrote = {}
            sw._store = type("S", (), {
                "_write_account_credentials":
                    lambda self, n, e, c: wrote.__setitem__("creds", c),
            })()
            sess = tmp_path / f"sess-{invalidation_raises}"
            sess.mkdir()
            sw._session_dir = lambda n, e: sess
            sw._live_session_pids = lambda n, e: []

            def _invalidate(n, e, _raise=invalidation_raises):
                if _raise:
                    raise PermissionError(13, "Permission denied")
                wrote["invalidated"] = True

            sw._invalidate_session_credentials = _invalidate
            sw._logger = type("L", (), {"info": lambda *a, **k: None,
                                        "warning": lambda *a, **k: None})()

            sw._write_account_credentials("2", "a@b.c", "NEW-GENERATION")

            assert wrote.get("creds") == "NEW-GENERATION", (
                f"the store write did not happen "
                f"(invalidation_raises={invalidation_raises})"
            )
            if invalidation_raises:
                # The profile could not be invalidated now, so it must be
                # MARKED — otherwise a profile whose access token is still
                # unexpired keeps serving a spent refresh token and nothing
                # forces the re-bootstrap.
                assert stale_marker_for(sess).exists(), (
                    "a swallowed invalidation left no stale marker: the "
                    "profile will keep serving the superseded generation"
                )
            else:
                assert wrote.get("invalidated"), "CONTROL: invalidation skipped"

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

    @pytest.mark.parametrize("jitter_ms", [427, -427])
    def test_a_same_lineage_stamp_jitter_does_not_release_the_strike(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, jitter_ms: int
    ):
        """The backup is the struck generation; the live bytes are the SAME
        lineage re-minted `jitter_ms` later (sign arbitrary, sub-second in
        practice). That is a rotation, not a newer login, on either sign — the
        strike must hold both ways."""
        from claude_swap.usage_store import FetchRecord as FR
        sample_sequence_data["accounts"]["2"]["email"] = "b@example.com"
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        s._write_json(s.sequence_file, sample_sequence_data)
        base = 9_999_999_999_000
        dead_backup = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-dead", "refreshToken": "rt-dead",
            "expiresAt": 1000, "refreshTokenExpiresAt": base}})
        live = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-live", "refreshToken": "rt-live",
            "expiresAt": 2000, "refreshTokenExpiresAt": base + jitter_ms}})
        s._write_account_credentials("2", "b@example.com", dead_backup)
        identities = {"2": ("b@example.com", "")}
        s._usage_store.record(
            {"2": FR(error="invalid_grant",
                     struck_fp=oauth.credential_fingerprint(dead_backup))},
            identities,
        )
        entry = s._usage_store.entries(identities, [])["2"]
        assert s._entry_token_dead(entry, "2", "b@example.com", live, True), (
            f"a {jitter_ms}ms stamp jitter released a strike still bound to "
            "the stored generation"
        )

    def test_CONTROL_a_stamp_8s_later_is_a_real_newer_login_and_heals(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict
    ):
        """8s is well past the lineage jitter — a real newer login must still
        release a strike bound to the superseded backup."""
        from claude_swap.usage_store import FetchRecord as FR
        sample_sequence_data["accounts"]["2"]["email"] = "b@example.com"
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        s._write_json(s.sequence_file, sample_sequence_data)
        base = 9_999_999_999_000
        dead_backup = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-dead", "refreshToken": "rt-dead",
            "expiresAt": 1000, "refreshTokenExpiresAt": base}})
        live = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-live", "refreshToken": "rt-live",
            "expiresAt": 2000, "refreshTokenExpiresAt": base + 8_000}})
        s._write_account_credentials("2", "b@example.com", dead_backup)
        identities = {"2": ("b@example.com", "")}
        s._usage_store.record(
            {"2": FR(error="invalid_grant",
                     struck_fp=oauth.credential_fingerprint(dead_backup))},
            identities,
        )
        entry = s._usage_store.entries(identities, [])["2"]
        assert not s._entry_token_dead(entry, "2", "b@example.com", live, True), (
            "a real newer login (8s later) must still release the strike"
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

    def test_active_strike_healed_by_absent_backup_not_unreadable(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict
    ):
        """Mutation guard (review IMPORTANT-1 / brief I-list item 1): the
        ABSENT direction of `bool(backup) and ...` must heal, not fail
        closed. A GENUINELY ABSENT backup (never written -- rc-44 'not
        found', `unreadable=False`) is not the same fact as an UNREADABLE
        one; only the latter must survive the strike. Dropping
        `bool(backup) and` collapses ABSENT into the same fail-closed path
        as UNREADABLE -- this pins the opposite, correct behavior."""
        from claude_swap.usage_store import FetchRecord as FR
        sample_sequence_data["accounts"]["2"]["email"] = "b@example.com"
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        s._write_json(s.sequence_file, sample_sequence_data)
        old_gen = json.dumps({
            "claudeAiOauth": {"accessToken": "sk-old",
                              "refreshToken": "rt-old", "expiresAt": 1000}})
        live = json.dumps({
            "claudeAiOauth": {"accessToken": "sk-live",
                              "refreshToken": "rt-live", "expiresAt": 2000}})
        # No backup ever written for slot 2 -- genuinely absent, not merely
        # unreadable (Linux platform: _read_account_credentials_ex's
        # `unreadable` axis is always False off macOS).
        identities = {"2": ("b@example.com", "")}
        s._usage_store.record(
            {"2": FR(error="invalid_grant",
                     struck_fp=oauth.credential_fingerprint(old_gen))},
            identities,
        )
        entry = s._usage_store.entries(identities, [])["2"]
        result = s._entry_token_dead(entry, "2", "b@example.com", live, True)
        assert result is False, (
            "a genuinely ABSENT backup must heal the strike (fp(stored) "
            f"already differs from struck_fingerprint), got {result}"
        )

    def test_active_strike_survives_unreadable_backup_not_absent(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, monkeypatch
    ):
        """An UNREADABLE backup (locked/denied macOS Keychain) must not
        SILENTLY HEAL an active slot's dead-token strike the way a
        genuinely ABSENT backup does. `_read_account_credentials` collapses
        both to `""`; `_entry_token_dead` must ask
        `_read_account_credentials_ex`, which distinguishes them.

        Round 9 correction: an unreadable backup is not, on its own,
        evidence the strike still holds either -- it is equally consistent
        with a backup that was ALREADY healed by a re-login whose new bytes
        we simply can't see right now (round 9's C1 finding: guessing
        `True` here condemned a healed slot). `_entry_token_dead` therefore
        returns `None` (cannot determine) for this shape rather than
        `True`. The property this test pins is the one round 8 actually
        needs: unreadable must not silently HEAL (return `False`, the value
        that would let the collector's healed-strike-clear branch erase the
        strike) -- `None` is not `False`, so that never happens. Nothing
        about the backup changes between the control and the probe below --
        only whether the read succeeds."""
        from claude_swap.usage_store import FetchRecord as FR
        sample_sequence_data["accounts"]["2"]["email"] = "b@example.com"
        s = ClaudeAccountSwitcher()
        s.platform = Platform.MACOS
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
        entry = s._usage_store.entries(identities, [])["2"]

        # CONTROL: the backup is readable and still stores the struck
        # generation -- the strike must hold.
        assert s._entry_token_dead(entry, "2", "b@example.com", live, True), (
            "control: a readable, still-struck backup must read dead"
        )

        # PROBE: the identical backup, but the Keychain now raises
        # (locked/denied/timeout) instead of answering. Nothing about the
        # backup changed -- only whether the read succeeded. This shape
        # (unreadable + struck fp differing from `stored`) is
        # OBSERVATIONALLY IDENTICAL to a genuinely healed slot, so the
        # correct answer is "cannot determine" (None), not a guessed True.
        monkeypatch.setattr(macos_keychain, "get_password", _raise_locked)
        result = s._entry_token_dead(entry, "2", "b@example.com", live, True)
        assert result is not False, (
            "an UNREADABLE backup must not silently HEAL the strike -- "
            f"unreadable is not evidence the dead generation was replaced, got {result}"
        )

    def test_collector_does_not_condemn_healed_active_slot_on_locked_keychain(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, monkeypatch
    ):
        """Collector-level guard for C1 (round 9): with the Keychain locked,
        an active slot whose strike is bound to a generation that no longer
        exists anywhere (already healed by a re-login) must NOT get
        USAGE_RELOGIN_REQUIRED from `_collect_usage_entries` -- that
        sentinel is what stops polling (`due_candidate`), marks the pin
        broken (`pin_is_broken`), and authorizes `cswap import` to
        overwrite without `--force`."""
        from claude_swap.usage_store import FetchRecord as FR
        sample_sequence_data["accounts"]["2"]["email"] = "b@example.com"
        s = ClaudeAccountSwitcher()
        s.platform = Platform.MACOS
        s._setup_directories()
        s._write_json(s.sequence_file, sample_sequence_data)
        old_gen = json.dumps({
            "claudeAiOauth": {"accessToken": "sk-old",
                              "refreshToken": "rt-old", "expiresAt": 1000}})
        new_gen = json.dumps({
            "claudeAiOauth": {"accessToken": "sk-new",
                              "refreshToken": "rt-new", "expiresAt": 2000}})
        identities = {"2": ("b@example.com", "")}
        s._usage_store.record(
            {"2": FR(error="invalid_grant",
                     struck_fp=oauth.credential_fingerprint(old_gen))},
            identities,
        )
        # Healed: backup rewritten to the new generation, bypassing
        # clear_dead_token (mirrors a re-login outside cswap's own heal
        # paths).
        s._write_account_credentials("2", "b@example.com", new_gen)
        monkeypatch.setattr(macos_keychain, "get_password", _raise_locked)
        # info tuple: (num, email, org_name, org_uuid, is_active, creds, alias)
        # is_active=True; creds = the LIVE credential (also new_gen here).
        info = [(2, "b@example.com", "", "", True, new_gen, "")]
        with patch.object(s, "current_account_number", return_value="2"):
            entries = s._collect_usage_entries(info, fetch=set())
        assert entries["2"].sentinel != USAGE_RELOGIN_REQUIRED, (
            "C1 regression: collector condemned an already-healed active "
            f"slot on a locked Keychain, sentinel={entries['2'].sentinel!r}"
        )

    def test_collector_own_active_read_does_not_condemn_a_healed_slot_on_degraded_read(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, monkeypatch
    ):
        """Round 11 C1: the test above hand-builds the info tuple and feeds
        it the ALREADY-HEALED bytes (`new_gen`) as `creds` -- it never
        exercises `_build_accounts_info`'s own active read, the third
        collapse site rounds 9/10 did not touch. `_build_accounts_info`
        does `creds = active.value or ""` and records `_active_read_degraded`
        one line later, but the collector's quarantine scan never consulted
        it -- a degraded read serving the STALE (still-struck) generation
        fingerprint-matched and condemned an already-healed slot.

        This test drives the REAL `_build_accounts_info` +
        `_collect_usage_entries` (not a hand-built info row), with the
        degraded read serving the OLD, struck generation while the backup
        already holds the healed NEW one -- exactly what "degraded" means:
        the Keychain read failed and a lagging plaintext fallback covered
        it.
        """
        from claude_swap.usage_store import FetchRecord as FR
        sample_sequence_data["accounts"]["2"]["email"] = "b@example.com"
        s = ClaudeAccountSwitcher()
        s.platform = Platform.MACOS
        s._setup_directories()
        s._write_json(s.sequence_file, sample_sequence_data)
        old_gen = json.dumps({
            "claudeAiOauth": {"accessToken": "sk-old",
                              "refreshToken": "rt-old", "expiresAt": 1000}})
        new_gen = json.dumps({
            "claudeAiOauth": {"accessToken": "sk-new", "refreshToken": "rt-new",
                              "expiresAt": 99999999999000}})
        identities = {"2": ("b@example.com", "")}
        s._usage_store.record(
            {"2": FR(error="invalid_grant",
                     struck_fp=oauth.credential_fingerprint(old_gen))},
            identities,
        )
        # HEALED: backup now holds the new generation; the struck fp
        # matches nothing there.
        s._write_account_credentials("2", "b@example.com", new_gen)
        # The active read itself is DEGRADED and serves the STALE (struck)
        # generation -- a lagging plaintext fallback covering a failed
        # Keychain read, which is what "degraded" means.
        monkeypatch.setattr(
            s, "_read_active_credentials",
            lambda: ActiveCredentials(old_gen, False, True),
        )
        # _build_accounts_info derives active_num from the live IDENTITY
        # (_get_current_account), not current_account_number.
        monkeypatch.setattr(s, "_get_current_account",
                             lambda: ("b@example.com", ""))
        with patch.object(s, "current_account_number", return_value="2"):
            info = s._build_accounts_info()
            entries = s._collect_usage_entries(info, fetch=set())
        assert entries["2"].sentinel != USAGE_RELOGIN_REQUIRED, (
            "C1 (round 11) regression: the collector's OWN active read "
            "condemned an already-healed slot on a degraded read, "
            f"sentinel={entries['2'].sentinel!r}"
        )

    def test_collector_does_not_silently_heal_an_unresolved_strike_on_locked_keychain(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, monkeypatch
    ):
        """Mutation guard for the `dead is None` branch: an ambiguous read
        (unreadable backup, struck active slot, NOT actually healed -- the
        live credential still matches the struck generation as far as
        `stored` can tell, i.e. round 8's own scenario) must not be silently
        auto-healed by falling through to the collector's fingerprint-healed
        `elif` (which calls `clear_dead_token`). If it fell through, the
        strike row would be wiped even though nothing confirmed the backup
        actually changed -- reopening round 8's exact bug. Assert on the
        STORE ROW surviving a second read, not just the sentinel (the
        fallthrough sets no sentinel either, so a sentinel-only assertion
        cannot see this)."""
        from claude_swap.usage_store import FetchRecord as FR
        sample_sequence_data["accounts"]["2"]["email"] = "b@example.com"
        s = ClaudeAccountSwitcher()
        s.platform = Platform.MACOS
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
        monkeypatch.setattr(macos_keychain, "get_password", _raise_locked)
        info = [(2, "b@example.com", "", "", True, live, "")]
        with patch.object(s, "current_account_number", return_value="2"):
            s._collect_usage_entries(info, fetch=set())
        # Re-read the row directly from the store: the strike must survive.
        post = s._usage_store.entries(identities, [])["2"]
        assert post.auth_dead_strikes >= 1, (
            "C1/round-8 regression: an ambiguous (unreadable, unresolved) "
            "strike was silently healed by the collector -- "
            f"auth_dead_strikes={post.auth_dead_strikes}"
        )

    def test_active_strike_survives_unreadable_backup_already_healed(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, monkeypatch
    ):
        """Round 9 C1: round 8's fail-closed guard must not condemn a
        HEALED slot. Same struck generation as before, but the backup (and
        live credential) have ALREADY been rewritten to a NEW generation by
        a re-login -- the strike is bound to a generation that no longer
        exists anywhere. A Keychain read failure on THIS process must not
        turn that into "re-login needed" (`_entry_token_dead` returning
        `True`): the slot is healthy and the strike is stale.

        This is the mutation guard for round 9: reverting the fix (letting
        `unreadable` return the raw `entry.token_dead()` -- i.e. `True`
        whenever raw strikes are at threshold, exactly round 8's shipped
        code) must fail this assertion."""
        from claude_swap.usage_store import FetchRecord as FR
        sample_sequence_data["accounts"]["2"]["email"] = "b@example.com"
        s = ClaudeAccountSwitcher()
        s.platform = Platform.MACOS
        s._setup_directories()
        s._write_json(s.sequence_file, sample_sequence_data)
        old_gen = json.dumps({
            "claudeAiOauth": {"accessToken": "sk-old",
                              "refreshToken": "rt-old", "expiresAt": 1000}})
        new_gen = json.dumps({
            "claudeAiOauth": {"accessToken": "sk-new",
                              "refreshToken": "rt-new", "expiresAt": 2000}})
        # Strike bound to the OLD generation.
        identities = {"2": ("b@example.com", "")}
        s._usage_store.record(
            {"2": FR(error="invalid_grant",
                     struck_fp=oauth.credential_fingerprint(old_gen))},
            identities,
        )
        # HEAL: a re-login rewrote the backup to the NEW generation --
        # bypassing `clear_dead_token` (mirrors a re-login through Claude
        # Code itself, or `_resync_rotated_backup`'s write path, neither of
        # which clears the strike row).
        s._write_account_credentials("2", "b@example.com", new_gen)
        entry = s._usage_store.entries(identities, [])["2"]

        monkeypatch.setattr(macos_keychain, "get_password", _raise_locked)
        result = s._entry_token_dead(entry, "2", "b@example.com", new_gen, True)
        assert result is not True, (
            "C1 regression: an already-healed slot was condemned by an "
            f"unreadable Keychain read, got {result}"
        )

    def test_slot_token_dead_coerces_ambiguous_to_not_dead(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, monkeypatch
    ):
        """`_slot_token_dead`'s only caller (`cswap import`'s auto-heal) uses
        it as a plain boolean gate: an ambiguous (`None`) verdict from
        `_entry_token_dead` must coerce to `False` here, not `True` --
        `True` would let `cswap import` silently overwrite a slot's
        credentials without `--force`, on nothing more than a locked
        Keychain."""
        from claude_swap.usage_store import FetchRecord as FR
        sample_sequence_data["accounts"]["2"]["email"] = "b@example.com"
        s = ClaudeAccountSwitcher()
        s.platform = Platform.MACOS
        s._setup_directories()
        s._write_json(s.sequence_file, sample_sequence_data)
        old_gen = json.dumps({
            "claudeAiOauth": {"accessToken": "sk-old",
                              "refreshToken": "rt-old", "expiresAt": 1000}})
        new_gen = json.dumps({
            "claudeAiOauth": {"accessToken": "sk-new",
                              "refreshToken": "rt-new", "expiresAt": 2000}})
        identities = {"2": ("b@example.com", "")}
        s._usage_store.record(
            {"2": FR(error="invalid_grant",
                     struck_fp=oauth.credential_fingerprint(old_gen))},
            identities,
        )
        s._write_account_credentials("2", "b@example.com", new_gen)
        cfg = s._get_claude_config_path()
        cfg.parent.mkdir(parents=True, exist_ok=True)
        s._write_json(cfg, {"oauthAccount": {
            "emailAddress": "b@example.com", "accountUuid": "acct-2",
            "organizationUuid": "", "organizationName": "",
        }})
        s._store._write_active_credentials_file(new_gen)
        monkeypatch.setattr(macos_keychain, "get_password", _raise_locked)
        assert s.current_account_number() == "2"
        result = s._slot_token_dead("2", "b@example.com")
        assert result is False, (
            f"an ambiguous verdict must coerce to False (not dead), got {result}"
        )


    def test_a_refused_active_item_does_not_condemn_the_slot(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, monkeypatch, block_real_keychain
    ):
        """The refusal's two FLAGS, not its `.value is None` arm, are what
        stop a degraded active read condemning a healed slot.

        Keychain ACLs are per ITEM, so Claude Code's credential can refuse a
        read while cswap's own backup answers -- the state an ssh-driven
        session reaches routinely. The active read then falls back to the
        plaintext file, which on macOS LAGS (Claude Code rotates
        keychain-only), so it serves the STRUCK generation while the healed
        one is unreadable. Answering "dead" from those bytes lets `cswap
        import` replace the slot without --force and switch-time adoption
        overwrite it with a foreign credential.

        The sibling arms cannot stand in: the backup is readable here, and
        `value` is the struck generation rather than None.
        """
        from claude_swap.usage_store import FetchRecord as FR
        sample_sequence_data["accounts"]["2"]["email"] = "b@example.com"
        s = ClaudeAccountSwitcher()
        s.platform = Platform.MACOS
        s._setup_directories()
        s._write_json(s.sequence_file, sample_sequence_data)
        struck_gen = json.dumps({
            "claudeAiOauth": {"accessToken": "sk-struck",
                              "refreshToken": "rt-struck", "expiresAt": 1000}})
        healed_gen = json.dumps({
            "claudeAiOauth": {"accessToken": "sk-healed",
                              "refreshToken": "rt-healed", "expiresAt": 2000}})
        s._write_account_credentials("2", "b@example.com", healed_gen)
        identities = {"2": ("b@example.com", "")}
        s._usage_store.record(
            {"2": FR(error="invalid_grant",
                     struck_fp=oauth.credential_fingerprint(struck_gen))},
            identities,
        )
        cfg = s._get_claude_config_path()
        cfg.parent.mkdir(parents=True, exist_ok=True)
        s._write_json(cfg, {"oauthAccount": {
            "emailAddress": "b@example.com", "accountUuid": "acct-2",
            "organizationUuid": "", "organizationName": "",
        }})
        s._store._write_active_credentials_file(struck_gen)
        assert s.current_account_number() == "2"

        real_get = block_real_keychain.get_password

        def only_the_active_item_is_refused(service, account):
            if service == CLAUDE_CODE_KEYCHAIN_SERVICE:
                raise KeychainError("locked")
            return real_get(service, account)

        monkeypatch.setattr(
            macos_keychain, "get_password", only_the_active_item_is_refused)

        # PREMISES. Without these the case can pass on an arm that is
        # already covered: an unreadable BACKUP returns False several lines
        # earlier, and a `None` value is the third arm of this same refusal.
        backup, unreadable = s._read_account_credentials_ex(
            "2", "b@example.com")
        assert not unreadable and backup == healed_gen, (
            "premise: the backup must stay readable, or the earlier "
            "unreadable-backup arm decides this and the flags decide nothing"
        )
        active = s._store._read_active_credentials()
        assert active.value is not None, (
            "premise: the fallback must supply bytes, or `.value is None` "
            "produces the refusal"
        )
        assert active.keychain_unavailable or active.degraded, (
            "premise: the read must report the refused item"
        )
        entry = s._usage_store.entries(identities).get("2")
        assert entry is not None and entry.token_dead(
            stored_fp=oauth.credential_fingerprint(active.value)), (
            "premise: those served bytes must be the struck generation, or "
            "the fingerprint compare answers not-dead without the guard"
        )

        assert s._slot_token_dead("2", "b@example.com") is False, (
            "DEFECT: a slot was condemned on a generation the Keychain "
            "refused to let us disprove"
        )

    def test_an_empty_live_credential_does_not_confirm_a_strike(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, monkeypatch, block_real_keychain
    ):
        """An active slot whose live credential is cleanly ABSENT confirms
        nothing: `credential_fingerprint("")` is None, and `token_dead`
        skips the binding check on a None fingerprint and answers on the
        raw strike count. The backup is the source that can answer.

        Both directions in one case, because a blanket refusal would pass
        the first assert and lose the second.
        """
        from claude_swap.usage_store import FetchRecord as FR
        sample_sequence_data["accounts"]["2"]["email"] = "b@example.com"
        s = ClaudeAccountSwitcher()
        s.platform = Platform.MACOS
        s._setup_directories()
        s._write_json(s.sequence_file, sample_sequence_data)
        struck_gen = json.dumps({
            "claudeAiOauth": {"accessToken": "sk-struck",
                              "refreshToken": "rt-struck", "expiresAt": 1000}})
        healed_gen = json.dumps({
            "claudeAiOauth": {"accessToken": "sk-healed",
                              "refreshToken": "rt-healed", "expiresAt": 2000}})
        identities = {"2": ("b@example.com", "")}
        s._usage_store.record(
            {"2": FR(error="invalid_grant",
                     struck_fp=oauth.credential_fingerprint(struck_gen))},
            identities,
        )
        cfg = s._get_claude_config_path()
        cfg.parent.mkdir(parents=True, exist_ok=True)
        s._write_json(cfg, {"oauthAccount": {
            "emailAddress": "b@example.com", "accountUuid": "acct-2",
            "organizationUuid": "", "organizationName": "",
        }})
        assert s.current_account_number() == "2"

        # PREMISE: the live read is CLEANLY empty -- no failure on either
        # axis, so neither flag nor the `.value is None` arm can refuse, and
        # only the emptiness itself is left to decide.
        active = s._store._read_active_credentials()
        assert active == ("", False, False), (
            f"premise: a clean empty active read, got {active!r}"
        )

        # The healed backup: no source matches the struck generation.
        s._write_account_credentials("2", "b@example.com", healed_gen)
        assert s._slot_token_dead("2", "b@example.com") is False, (
            "DEFECT: an active slot was condemned on nothing -- the live "
            "bytes are absent and the backup is a different generation, so "
            "no stored source matches the strike. `cswap import` then "
            "replaces the healthy backup without --force"
        )

        # CONTROL: the backup DOES match the struck generation, so the
        # strike is confirmed by a source that was actually read. A blanket
        # refusal on an empty live value would lose this.
        s._write_account_credentials("2", "b@example.com", struck_gen)
        assert s._slot_token_dead("2", "b@example.com") is True, (
            "control: a matching backup must still confirm dead"
        )

@pytest.mark.usefixtures("_ex_reads_what_the_plain_reader_returns")
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

class TestALoginLandsInItsOwnSlot:
    """A login for a MANAGED account must reach that account's slot.

    `_resync_rotated_backup` asks the server whose the live credential is,
    gets the answer, compares it to ONE slot, and when it does not match it
    logs "foreign credential under a stale config" and DISCARDS the answer.

    Measured on the personal mac: the owner ran `/login` and signed in as
    slot 5 while slot 1 was active. A switch to slot 5 then stashed that
    credential as foreign and installed slot 5's stored one — which was dead
    — one log line later. The stash was proven to be slot 5's own credential
    by asking the server with its access token. The owner's recovery path,
    `cswap add --slot 5`, then refused because the live credential had been
    replaced by a third account's.

    The identity is already in hand at the moment it is thrown away. Nothing
    else needs to be built: no new command, no stash to adopt, no --slot for
    the owner to type.
    """

    def test_the_resolver_names_the_owning_slot(self, temp_home):
        sw = ClaudeAccountSwitcher()
        data = {"accounts": {
            "1": {"email": "a@x", "organizationUuid": "o1", "uuid": "u-1"},
            "5": {"email": "b@y", "organizationUuid": "o5", "uuid": "u-5"},
        }}
        got = sw._slot_owning_resolved_identity(
            data, {"uuid": "u-5", "email": "b@y", "organizationUuid": "o5"})
        assert got == "5", got

    def test_an_identity_no_slot_owns_is_None(self, temp_home):
        sw = ClaudeAccountSwitcher()
        data = {"accounts": {"1": {"email": "a@x", "organizationUuid": "o1",
                                   "uuid": "u-1"}}}
        assert sw._slot_owning_resolved_identity(
            data, {"uuid": "u-9", "email": "z@z", "organizationUuid": "o9"}) is None

    def test_a_partial_profile_owns_nothing(self, temp_home):
        """No uuid and no (email, org) pair is not an identity. Guessing an
        owner here would write a credential into a slot on a coincidence."""
        sw = ClaudeAccountSwitcher()
        data = {"accounts": {"1": {"email": "a@x", "organizationUuid": "o1",
                                   "uuid": "u-1"}}}
        assert sw._slot_owning_resolved_identity(data, {}) is None
        assert sw._slot_owning_resolved_identity(
            data, {"email": "a@x"}) is None

    def test_uuid_beats_a_recycled_address(self, temp_home):
        """Addresses are recycled across accounts; uuids are not. A stored
        uuid that disagrees must lose to nothing — the row is not that slot's
        even when the address matches."""
        sw = ClaudeAccountSwitcher()
        # SLOT 2 CARRIES NO UUID, so the address fallback is live and would
        # claim the row on the recycled address alone. Without it the early
        # `return None` is unreachable and the mutation survives.
        data = {"accounts": {
            "1": {"email": "same@x", "organizationUuid": "o1", "uuid": "u-1"},
            "2": {"email": "same@x", "organizationUuid": "o1"},
        }}
        assert sw._slot_owning_resolved_identity(
            data, {"uuid": "u-OTHER", "email": "same@x",
                   "organizationUuid": "o1"}) is None

    def test_a_login_for_another_managed_slot_lands_in_that_slot(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, monkeypatch
    ):
        """THE SLICE. The live credential belongs to slot 1, the resync runs
        for slot 2, and slot 1's backup is empty. Today it logs "foreign
        credential under a stale config" and returns, so the owner's login is
        lost. It must land in slot 1."""
        accs = sample_sequence_data["accounts"]
        accs["2"].update(email="b@example.com", uuid="u-2",
                         organizationUuid="o-2")
        accs["1"].update(email="c@example.com", uuid="u-1",
                         organizationUuid="o-1")
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        s._write_json(s.sequence_file, sample_sequence_data)
        live = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-live", "refreshToken": "rt-live",
            "expiresAt": 99_999_999_999_999}})
        s._write_account_credentials("2", "b@example.com", json.dumps(
            {"claudeAiOauth": {"accessToken": "sk-2", "refreshToken": "rt-2",
                               "expiresAt": 99_999_999_999_999}}))
        with patch("claude_swap.oauth.fetch_oauth_profile",
                   return_value={"uuid": "u-1", "email": "c@example.com",
                                 "organizationUuid": "o-1"}):
            s._resync_rotated_backup("2", "b@example.com", "o-2", live)
        got = s._read_account_credentials("1", "c@example.com")
        assert got, "slot 1 has no credential — the login was discarded"
        assert json.loads(got)["claudeAiOauth"]["refreshToken"] == "rt-live"

    def test_an_older_login_does_not_overwrite_a_fresher_stored_one(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, monkeypatch
    ):
        """THE RISK THIS FIX CARRIES. Adopting writes over whatever the slot
        holds. On the measured incident the stored one was dead and the live
        one fresh, but a stale live credential must not clobber a fresher
        backup — the refresh token is the one thing that cannot be re-derived.
        Same lineage is not the question; RECENCY is."""
        accs = sample_sequence_data["accounts"]
        accs["2"].update(email="b@example.com", uuid="u-2",
                         organizationUuid="o-2")
        accs["1"].update(email="c@example.com", uuid="u-1",
                         organizationUuid="o-1")
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        s._write_json(s.sequence_file, sample_sequence_data)
        fresher = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-keep", "refreshToken": "rt-keep",
            "expiresAt": 99_999_999_999_999,
            "refreshTokenExpiresAt": 99_999_999_999_999}})
        s._write_account_credentials("1", "c@example.com", fresher)
        # THE SLOT MUST BE DEAD OR THIS NEVER REACHES THE RECENCY GUARD: the
        # deadness check returns first for a healthy slot, so without this the
        # case passes on a DIFFERENT guard and the mutation survives.
        from claude_swap.usage_store import FetchRecord, Identity
        s._usage_store.record(
            {"1": FetchRecord(error="invalid_grant")},
            {"1": Identity(("c@example.com", "o-1"))})
        older = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-old", "refreshToken": "rt-old",
            "expiresAt": 99_999_999_999_999,
            "refreshTokenExpiresAt": 1_000}})
        with patch("claude_swap.oauth.fetch_oauth_profile",
                   return_value={"uuid": "u-1", "email": "c@example.com",
                                 "organizationUuid": "o-1"}):
            s._resync_rotated_backup("2", "b@example.com", "o-2", older)
        got = json.loads(s._read_account_credentials("1", "c@example.com"))
        assert got["claudeAiOauth"]["refreshToken"] == "rt-keep", (
            "an older login overwrote a fresher stored credential")

    def test_a_healthy_owner_slot_is_left_alone(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, monkeypatch
    ):
        """THE REGRESSION THE FIRST SLICE CAUSED. An ordinary switch leaves a
        transient identity mismatch behind, and adopting on that alone
        overwrote a rotated refresh token the switch had just persisted —
        `test_switch_persists_rotated_refresh_token_to_backup` went red.

        The measured incident is narrower than "the identity differs": the
        owner slot's stored credential was DEAD, so its login had nowhere to
        live. A slot holding a working credential is not that case and must
        not be written."""
        accs = sample_sequence_data["accounts"]
        accs["2"].update(email="b@example.com", uuid="u-2",
                         organizationUuid="o-2")
        accs["1"].update(email="c@example.com", uuid="u-1",
                         organizationUuid="o-1")
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        s._write_json(s.sequence_file, sample_sequence_data)
        healthy = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-healthy", "refreshToken": "rt-healthy",
            "expiresAt": 99_999_999_999_999,
            "refreshTokenExpiresAt": 99_999_999_999_999}})
        s._write_account_credentials("1", "c@example.com", healthy)
        live = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-live", "refreshToken": "rt-live",
            "expiresAt": 99_999_999_999_999,
            "refreshTokenExpiresAt": 99_999_999_999_999}})
        with patch("claude_swap.oauth.fetch_oauth_profile",
                   return_value={"uuid": "u-1", "email": "c@example.com",
                                 "organizationUuid": "o-1"}):
            s._resync_rotated_backup("2", "b@example.com", "o-2", live)
        got = json.loads(s._read_account_credentials("1", "c@example.com"))
        assert got["claudeAiOauth"]["refreshToken"] == "rt-healthy", (
            "a slot with a working credential was overwritten")

    @pytest.mark.parametrize("jitter_ms", [427, -427])
    def test_a_same_lineage_stamp_jitter_does_not_overwrite_a_healthy_slot(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, jitter_ms: int
    ):
        """The stored (slot 1) credential is the current generation; the live
        bytes are the SAME lineage re-minted `jitter_ms` later (sign
        arbitrary). Neither sign is a newer login, so the healthy backup must
        keep its own credential both ways."""
        accs = sample_sequence_data["accounts"]
        accs["2"].update(email="b@example.com", uuid="u-2",
                         organizationUuid="o-2")
        accs["1"].update(email="c@example.com", uuid="u-1",
                         organizationUuid="o-1")
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        s._write_json(s.sequence_file, sample_sequence_data)
        base = 99_999_999_999_000
        current = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-current", "refreshToken": "rt-current",
            "expiresAt": 99_999_999_999_999,
            "refreshTokenExpiresAt": base}})
        s._write_account_credentials("1", "c@example.com", current)
        live = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-previous", "refreshToken": "rt-previous",
            "expiresAt": 99_999_999_999_999,
            "refreshTokenExpiresAt": base + jitter_ms}})
        with patch("claude_swap.oauth.fetch_oauth_profile",
                   return_value={"uuid": "u-1", "email": "c@example.com",
                                 "organizationUuid": "o-1"}):
            s._resync_rotated_backup("2", "b@example.com", "o-2", live)
        got = json.loads(s._read_account_credentials("1", "c@example.com"))
        assert got["claudeAiOauth"]["refreshToken"] == "rt-current", (
            f"a {jitter_ms}ms stamp jitter overwrote a healthy backup with "
            "the previous generation"
        )

    def test_a_dead_owner_slot_receives_the_login(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, monkeypatch
    ):
        """THE MEASURED CASE ITSELF, and the branch an empty slot cannot
        reach. Slot 1 HOLDS a credential and the collector has condemned it
        on invalid_grant; the owner's fresh login must replace it.

        This is also the control on the deadness probe: the first slice
        passed with an EMPTY slot, so `_slot_credential_is_dead` was never
        consulted and could return False forever without a test noticing."""
        accs = sample_sequence_data["accounts"]
        accs["2"].update(email="b@example.com", uuid="u-2",
                         organizationUuid="o-2")
        accs["1"].update(email="c@example.com", uuid="u-1",
                         organizationUuid="o-1")
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        s._write_json(s.sequence_file, sample_sequence_data)
        dead = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-dead", "refreshToken": "rt-dead",
            "expiresAt": 99_999_999_999_999,
            "refreshTokenExpiresAt": 99_999_999_999_999}})
        s._write_account_credentials("1", "c@example.com", dead)
        from claude_swap.usage_store import FetchRecord, Identity
        ids = {"1": Identity(("c@example.com", "o-1"))}
        s._usage_store.record({"1": FetchRecord(error="invalid_grant")}, ids)
        live = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-live", "refreshToken": "rt-live",
            "expiresAt": 99_999_999_999_999,
            "refreshTokenExpiresAt": 99_999_999_999_999}})
        with patch("claude_swap.oauth.fetch_oauth_profile",
                   return_value={"uuid": "u-1", "email": "c@example.com",
                                 "organizationUuid": "o-1"}):
            s._resync_rotated_backup("2", "b@example.com", "o-2", live)
        got = json.loads(s._read_account_credentials("1", "c@example.com"))
        assert got["claudeAiOauth"]["refreshToken"] == "rt-live", (
            "a quarantined slot did not receive its owner's fresh login")

    @pytest.mark.parametrize("delta_ms", [427, -427])
    def test_a_dead_slot_heals_through_stamp_jitter_either_sign(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, delta_ms: int
    ):
        """THE MEASURED INCIDENT: a same-lineage rotation whose re-minted
        stamp lands within the jitter window of the dead stored generation's
        must still heal the slot and clear its strike, regardless of which
        way the jitter fell. The -427ms case is red on 9e702934 — that sign
        read as an OLDER login and the dead slot's own cure was refused."""
        accs = sample_sequence_data["accounts"]
        accs["2"].update(email="b@example.com", uuid="u-2",
                         organizationUuid="o-2")
        accs["1"].update(email="c@example.com", uuid="u-1",
                         organizationUuid="o-1")
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        s._write_json(s.sequence_file, sample_sequence_data)
        base = 99_999_999_999_000
        dead = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-dead", "refreshToken": "rt-dead",
            "expiresAt": 99_999_999_999_999,
            "refreshTokenExpiresAt": base}})
        s._write_account_credentials("1", "c@example.com", dead)
        from claude_swap.usage_store import Identity
        ids = {"1": Identity(("c@example.com", "o-1"))}
        s._usage_store.record(
            {"1": FetchRecord(error="invalid_grant",
                               struck_fp=oauth.credential_fingerprint(dead))},
            ids)
        live = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-live", "refreshToken": "rt-live",
            "expiresAt": 99_999_999_999_999,
            "refreshTokenExpiresAt": base + delta_ms}})
        with patch("claude_swap.oauth.fetch_oauth_profile",
                   return_value={"uuid": "u-1", "email": "c@example.com",
                                 "organizationUuid": "o-1"}):
            s._resync_rotated_backup("2", "b@example.com", "o-2", live)
        got = json.loads(s._read_account_credentials("1", "c@example.com"))
        assert got["claudeAiOauth"]["refreshToken"] == "rt-live", (
            f"a dead slot refused its own cure at a {delta_ms}ms stamp "
            "jitter")
        assert not s._slot_token_dead("1", "c@example.com"), (
            f"a dead slot's strike was not cleared by its own cure at a "
            f"{delta_ms}ms stamp jitter")

    def test_CONTROL_a_dead_slot_keeps_a_genuinely_older_login(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict
    ):
        """CONTROL for the jitter cases above: 8s past the jitter window is
        an unambiguous older login (not a re-mint), so a dead slot's own
        stored credential and strike must both survive it."""
        accs = sample_sequence_data["accounts"]
        accs["2"].update(email="b@example.com", uuid="u-2",
                         organizationUuid="o-2")
        accs["1"].update(email="c@example.com", uuid="u-1",
                         organizationUuid="o-1")
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        s._write_json(s.sequence_file, sample_sequence_data)
        base = 99_999_999_999_000
        dead = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-dead", "refreshToken": "rt-dead",
            "expiresAt": 99_999_999_999_999,
            "refreshTokenExpiresAt": base}})
        s._write_account_credentials("1", "c@example.com", dead)
        from claude_swap.usage_store import Identity
        ids = {"1": Identity(("c@example.com", "o-1"))}
        s._usage_store.record(
            {"1": FetchRecord(error="invalid_grant",
                               struck_fp=oauth.credential_fingerprint(dead))},
            ids)
        live = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-old", "refreshToken": "rt-old",
            "expiresAt": 99_999_999_999_999,
            "refreshTokenExpiresAt": base - 8_000}})
        with patch("claude_swap.oauth.fetch_oauth_profile",
                   return_value={"uuid": "u-1", "email": "c@example.com",
                                 "organizationUuid": "o-1"}):
            s._resync_rotated_backup("2", "b@example.com", "o-2", live)
        got = json.loads(s._read_account_credentials("1", "c@example.com"))
        assert got["claudeAiOauth"]["refreshToken"] == "rt-dead", (
            "a genuinely older login overwrote a dead slot's stored "
            "credential")
        assert s._slot_token_dead("1", "c@example.com"), (
            "a genuinely older login cleared a dead slot's strike")

    def _owner_slot_fixture(self, sample_sequence_data):
        """Slots 1 and 2 as the adopt path sees them: the resync runs for 2,
        the server says the live credential is 1's."""
        accs = sample_sequence_data["accounts"]
        accs["2"].update(email="b@example.com", uuid="u-2",
                         organizationUuid="o-2")
        accs["1"].update(email="c@example.com", uuid="u-1",
                         organizationUuid="o-1")
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        s._write_json(s.sequence_file, sample_sequence_data)
        return s

    @staticmethod
    def _blob(refresh, refresh_expires_at=99_999_999_999_999, access="sk-x"):
        return json.dumps({"claudeAiOauth": {
            "accessToken": access, "refreshToken": refresh,
            "expiresAt": 99_999_999_999_999,
            "refreshTokenExpiresAt": refresh_expires_at}})

    def _resync_as_slot_2(self, s, live):
        with patch("claude_swap.oauth.fetch_oauth_profile",
                   return_value={"uuid": "u-1", "email": "c@example.com",
                                 "organizationUuid": "o-1"}):
            s._resync_rotated_backup("2", "b@example.com", "o-2", live)

    @pytest.mark.skipif(
        sys.platform == "win32" or os.geteuid() == 0,
        reason="chmod(0o000) does not deny root or Windows",
    )
    def test_an_unreadable_owner_backup_is_not_overwritten(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        """A read that FAILED is not an empty slot. ``_read_account_credentials``
        flattens a locked Keychain / denied ``.enc`` to ``""``, which the
        emptiness arm reads as "nowhere to put a login" — so the one state
        where the slot's only refresh token cannot be seen is the state that
        authorizes overwriting it."""
        s = self._owner_slot_fixture(sample_sequence_data)
        # Pin the file backend so the probe denies the served copy on macOS
        # too, where a Keychain write would leave the .enc absent.
        s.platform = Platform.LINUX
        s._write_account_credentials("1", "c@example.com", self._blob("rt-hidden"))
        enc = s._store._backup_enc_path("1", "c@example.com")
        assert enc.exists(), "premise: slot 1 has a backup on disk"
        before = enc.read_bytes()
        enc.chmod(0o000)
        try:
            self._resync_as_slot_2(s, self._blob("rt-live"))
        finally:
            enc.chmod(0o600)
        assert enc.read_bytes() == before, (
            "an unreadable backup was overwritten — the slot's only refresh "
            "token is gone and .prev could not be written either"
        )

    def test_a_strike_the_slot_has_already_outlived_is_not_deadness(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        """A strike condemns the GENERATION it POSTed, not the slot. Once the
        slot stores a different credential the verdict no longer applies —
        ``token_dead(stored_fp=...)`` is where the whole codebase asks this.
        Reading the raw count instead overwrites a healthy credential on a
        quarantine the collector has not yet swept."""
        s = self._owner_slot_fixture(sample_sequence_data)
        s._write_account_credentials("1", "c@example.com", self._blob("rt-current"))
        s._usage_store.record(
            {"1": FetchRecord(
                error="invalid_grant",
                struck_fp=oauth.credential_fingerprint(self._blob("rt-gone")),
            )},
            {"1": ("c@example.com", "o-1")},
        )
        self._resync_as_slot_2(s, self._blob("rt-live"))
        got = json.loads(s._read_account_credentials("1", "c@example.com"))
        assert got["claudeAiOauth"]["refreshToken"] == "rt-current", (
            "a strike bound to a generation the slot no longer stores "
            "authorized an overwrite"
        )

    def test_a_strike_this_pr_itself_doubts_is_not_deadness(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        """``_strike_is_suspected_race`` says a first strike right after a
        success is probably a concurrent rotation, not a dead token — the
        fetch gate honours it. The adopt gate must agree, or the same row is
        "retry it" to one reader and "overwrite its refresh token" to the
        other."""
        s = self._owner_slot_fixture(sample_sequence_data)
        s._write_account_credentials("1", "c@example.com", self._blob("rt-current"))
        ids = {"1": ("c@example.com", "o-1")}
        s._usage_store.record({"1": FetchRecord(usage={"five_hour": {}})}, ids)
        s._usage_store.record({"1": FetchRecord(error="invalid_grant")}, ids)
        self._resync_as_slot_2(s, self._blob("rt-live"))
        got = json.loads(s._read_account_credentials("1", "c@example.com"))
        assert got["claudeAiOauth"]["refreshToken"] == "rt-current", (
            "a strike the fetch gate keeps eligible was read as a verdict"
        )

    def test_a_slot_with_no_recorded_address_is_not_written(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        """The backup is keyed by the roster's email. Falling back to the
        profile's address writes the credential to a path every reader of
        that slot computes differently — stored, reported as adopted, and
        unreachable."""
        s = self._owner_slot_fixture(sample_sequence_data)
        s.platform = Platform.LINUX   # .enc is the served backend everywhere
        data = s._get_sequence_data()
        data["accounts"]["1"]["email"] = ""
        s._write_json(s.sequence_file, data)
        self._resync_as_slot_2(s, self._blob("rt-live"))
        # Any address at all, not just the profile's: the slot carries none,
        # so every path the write could pick is one no reader recomputes.
        assert not list(s.credentials_dir.glob(".creds-1-*.enc")), (
            "the login was written under an address the slot does not carry"
        )

    def test_the_adopt_holds_the_account_lock_across_its_write(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        """The guards above decide whether a refresh token may be replaced.
        Outside the lock every slot mutation holds they are advisory only: a
        switch persisting a rotated token between the verdict and the write is
        overwritten by a decision taken before it existed.

        flock binds to the open file description, so a second ``open`` of the
        same path contends even in this process — that is the probe."""
        from claude_swap.locking import FileLock

        s = self._owner_slot_fixture(sample_sequence_data)
        seen = []

        def _probe(*a, **kw):
            seen.append(FileLock(s.lock_file, timeout=0).acquire())

        with patch.object(s, "_write_account_credentials", side_effect=_probe):
            self._resync_as_slot_2(s, self._blob("rt-live"))
        assert seen == [False], (
            f"the account lock was free during the adopt's write ({seen})"
        )

    def test_a_uuid_shared_across_orgs_resolves_to_the_matching_org(
        self, temp_home,
    ):
        """One account in two orgs is two slots carrying the SAME account uuid
        (``sample_sequence_data_with_org`` is exactly that shape). Matching on
        the uuid alone returns whichever slot iterates first, so the login is
        written into the wrong org's slot half the time."""
        sw = ClaudeAccountSwitcher()
        data = {"accounts": {
            "1": {"email": "u@x", "organizationUuid": "o-a", "uuid": "u-1"},
            "2": {"email": "u@x", "organizationUuid": "o-b", "uuid": "u-1"},
        }}
        assert sw._slot_owning_resolved_identity(
            data, {"uuid": "u-1", "email": "u@x",
                   "organizationUuid": "o-b"}) == "2"

    def test_a_uuid_shared_across_orgs_is_ambiguous_without_one(
        self, temp_home,
    ):
        """No org in the profile leaves both records equally plausible, and
        the credential is the one thing that cannot be re-derived. Ambiguity
        is not a tiebreak."""
        sw = ClaudeAccountSwitcher()
        data = {"accounts": {
            "1": {"email": "u@x", "organizationUuid": "o-a", "uuid": "u-1"},
            "2": {"email": "u@x", "organizationUuid": "o-b", "uuid": "u-1"},
        }}
        assert sw._slot_owning_resolved_identity(
            data, {"uuid": "u-1", "email": "u@x"}) is None

    def test_the_slot_that_just_disowned_the_credential_is_not_written(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        """The adopt branch runs only after the per-slot verdict said these
        bytes are NOT this slot's. Whatever the roster scan answers, writing
        them into that same slot contradicts the verdict that got us here."""
        s = self._owner_slot_fixture(sample_sequence_data)
        with patch.object(s, "_resolved_matches_slot_identity",
                          return_value=False), \
             patch.object(s, "_slot_owning_resolved_identity",
                          return_value="2"), \
             patch.object(s, "_write_account_credentials") as write:
            self._resync_as_slot_2(s, self._blob("rt-live"))
        write.assert_not_called()

    def test_a_field_shaped_quarantine_reaches_the_adopt(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        """THE SHAPE EVERY OTHER TEST HERE MISSES. A slot that has ever polled
        successfully carries `fetchedAt`, so its FIRST invalid_grant is
        doubted by `token_dead` — correctly, the
        credential may still be alive. The adopt must refuse on that pass and
        then RUN once the retry confirms the death, instead of spending its
        one attempt while the verdict was still in doubt.

        Rows built by `record` on a fresh row have `fetchedAt=None`, which is
        never doubted — that is why the other tests never reach this."""
        s = self._owner_slot_fixture(sample_sequence_data)
        s._write_account_credentials("1", "c@example.com", self._blob("rt-dead"))
        ids = {"1": ("c@example.com", "o-1")}
        clock = [1_000_000.0]
        s._usage_store.clock = lambda: clock[0]
        s._usage_store.record({"1": FetchRecord(usage={"five_hour": {}})}, ids)
        clock[0] += 310
        s._usage_store.record({"1": FetchRecord(error="invalid_grant")}, ids)
        assert not s._slot_token_dead("1", "c@example.com"), (
            "PREMISE: a first strike 310s after a success must be doubted"
        )

        self._resync_as_slot_2(s, self._blob("rt-live"))
        assert json.loads(s._read_account_credentials(
            "1", "c@example.com"))["claudeAiOauth"]["refreshToken"] == "rt-dead", (
            "a credential whose death was still in doubt was overwritten")

        # The retry confirms it. The adopt must still be reachable.
        clock[0] += 310
        s._usage_store.record({"1": FetchRecord(error="invalid_grant")}, ids)
        assert s._slot_token_dead("1", "c@example.com"), "PREMISE: now dead"
        self._resync_as_slot_2(s, self._blob("rt-live"))
        assert json.loads(s._read_account_credentials(
            "1", "c@example.com"))["claudeAiOauth"]["refreshToken"] == "rt-live", (
            "the memo spent the adopt's only attempt on a pass where the "
            "verdict was still in doubt, so the login is lost for the life "
            "of the process")

    def test_an_exact_org_match_beats_a_blank_org_sibling(self, temp_home):
        """A half-migrated roster carries org-less records beside real ones.
        Treating the blank as a peer makes an otherwise unambiguous match read
        as a tie, and the login goes back to the stash instead of home.

        The last case is the one the preference must NOT swallow: two blanks
        are not an exact match, they are two absences. Promoting that to
        evidence contradicts the tolerance it sits beside and turns an honest
        tie into a pick made by iteration order — and the adopt settles, so
        the wrong record keeps it for the life of the process."""
        sw = ClaudeAccountSwitcher()
        data = {"accounts": {
            "1": {"email": "u@x", "organizationUuid": "ORG-A", "uuid": "u-1"},
            "2": {"email": "u@x", "organizationUuid": "", "uuid": "u-1"},
            "3": {"email": "v@x", "organizationUuid": "ORG-A", "uuid": "u-OTHER"},
        }}
        assert sw._slot_owning_resolved_identity(
            data, {"uuid": "u-1", "email": "u@x",
                   "organizationUuid": "ORG-A"}) == "1", "exact org wins"
        assert sw._slot_owning_resolved_identity(
            data, {"uuid": "u-1", "email": "u@x",
                   "organizationUuid": None}) is None, (
            "a blank profile org matched a blank roster org as if the two "
            "absences corroborated each other")

    def test_two_records_of_one_account_and_org_are_ambiguous(self, temp_home):
        """THE BRANCH `len(hits) == 1` EXISTS FOR. Once the org is compared
        outright, two slots differing by org no longer both hit — so the only
        way to reach ambiguity is a duplicate record, same uuid AND same org.
        Nothing distinguishes them, and an arbitrary first hit writes an
        unre-derivable credential into a slot on iteration order."""
        sw = ClaudeAccountSwitcher()
        data = {"accounts": {
            "1": {"email": "u@x", "organizationUuid": "o-a", "uuid": "u-1"},
            "4": {"email": "u@x", "organizationUuid": "o-a", "uuid": "u-1"},
        }}
        assert sw._slot_owning_resolved_identity(
            data, {"uuid": "u-1", "email": "u@x",
                   "organizationUuid": "o-a"}) is None

    def test_a_slot_re_pointed_under_the_adopt_is_not_written(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        """A remove-and-re-add, or `move_account`, re-points a slot NUMBER at
        a different account while the lock is waited out. The address check
        cannot see it — the new occupant has an address of its own — so the
        identity the server resolved has to be re-checked against the roster
        the lock holder actually sees."""
        from claude_swap.locking import FileLock as _RealFileLock

        s = self._owner_slot_fixture(sample_sequence_data)
        s.platform = Platform.LINUX

        class RePointWhileWeWait(_RealFileLock):
            def __enter__(self):
                data = s._get_sequence_data()
                data["accounts"]["1"] = {
                    "email": "someone-else@example.com",
                    "organizationUuid": "o-9", "uuid": "u-9",
                }
                s._write_json(s.sequence_file, data)
                return super().__enter__()

        with patch("claude_swap.switcher.FileLock", RePointWhileWeWait):
            self._resync_as_slot_2(s, self._blob("rt-live"))
        assert not list(s.credentials_dir.glob(".creds-1-*.enc")), (
            "another account's slot was handed a login the server attributed "
            "to the account that slot number used to hold"
        )

    @pytest.mark.skipif(
        sys.platform == "win32" or os.geteuid() == 0,
        reason="chmod(0o000) does not deny root or Windows",
    )
    def test_an_unreadable_pass_does_not_spend_the_only_attempt(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        """THE OTHER HALF OF THE CONTRACT. Refusing on an unreadable backup is
        only half a fix: the memo short-circuits the next pass, so if that
        refusal is reported as SETTLED the login is dropped for the life of
        the process — the same one-shot failure the field-shaped quarantine
        had. A locked Keychain clears; the answer must be retried."""
        s = self._owner_slot_fixture(sample_sequence_data)
        s.platform = Platform.LINUX
        s._write_account_credentials("1", "c@example.com", self._blob("rt-dead"))
        s._usage_store.record(
            {"1": FetchRecord(error="invalid_grant")},
            {"1": ("c@example.com", "o-1")},
        )
        enc = s._store._backup_enc_path("1", "c@example.com")
        enc.chmod(0o000)
        try:
            self._resync_as_slot_2(s, self._blob("rt-live"))
        finally:
            enc.chmod(0o600)
        assert json.loads(s._read_account_credentials(
            "1", "c@example.com"))["claudeAiOauth"]["refreshToken"] == "rt-dead", (
            "PREMISE: the unreadable pass must refuse to write")

        with patch("claude_swap.oauth.fetch_oauth_profile",
                   return_value={"uuid": "u-1", "email": "c@example.com",
                                 "organizationUuid": "o-1"}) as probe:
            s._resync_rotated_backup("2", "b@example.com", "o-2",
                                     self._blob("rt-live"))
        assert json.loads(s._read_account_credentials(
            "1", "c@example.com"))["claudeAiOauth"]["refreshToken"] == "rt-live", (
            "the unreadable pass was reported settled, so the retry never "
            "ran and the owner's login is lost until the process restarts")
        assert probe.call_count == 0, (
            "the retry re-probed the endpoint; the memo exists to carry the "
            "resolved owner so it does not have to")

    def test_a_settled_adopt_stops_being_retried(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        """The drift persists after a successful adopt — the live store still
        holds the other slot's credential — so the memo would hand the retry
        path the same profile on every collect pass forever, each one taking
        the account lock and re-reading the slot. Once the answer is settled
        the profile goes."""
        s = self._owner_slot_fixture(sample_sequence_data)
        s.platform = Platform.LINUX
        live = self._blob("rt-live")
        self._resync_as_slot_2(s, live)
        assert json.loads(s._read_account_credentials(
            "1", "c@example.com"))["claudeAiOauth"]["refreshToken"] == "rt-live", (
            "PREMISE: the first pass must actually adopt")

        with patch.object(s, "_adopt_login_into_slot") as again:
            self._resync_as_slot_2(s, live)
        again.assert_not_called()
        # States the property directly, so an unrelated early return upstream
        # cannot make the absence above pass for free.
        assert not s._resolved_owners

    def test_an_account_that_moved_slots_is_still_adopted(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        """`move_account` re-homes an account under a different slot NUMBER
        while the lock is waited out. The owner resolved before the lock is
        then stale, and comparing the roster's answer against it refuses the
        adopt for good — the memo makes that refusal permanent. Re-deriving
        is not just a safety check; it is where the owner comes from."""
        from claude_swap.locking import FileLock as _RealFileLock

        s = self._owner_slot_fixture(sample_sequence_data)
        s.platform = Platform.LINUX

        class MoveWhileWeWait(_RealFileLock):
            moved = False

            def __enter__(self):
                if not MoveWhileWeWait.moved:
                    MoveWhileWeWait.moved = True
                    data = s._get_sequence_data()
                    data["accounts"]["7"] = data["accounts"].pop("1")
                    s._write_json(s.sequence_file, data)
                return super().__enter__()

        with patch("claude_swap.switcher.FileLock", MoveWhileWeWait):
            self._resync_as_slot_2(s, self._blob("rt-live"))
        got = s._read_account_credentials("7", "c@example.com")
        assert got and json.loads(got)["claudeAiOauth"][
            "refreshToken"] == "rt-live", (
            "the account moved slots and its login was dropped instead of "
            "following it"
        )

    def test_a_lone_uuid_match_is_claimed_whatever_the_org_says(
        self, temp_home,
    ):
        """A blank org on either side is no evidence, and it does not need to
        be: the uuid names the ACCOUNT, so one record for it IS that account's
        slot. Refusing here would only send the login back to the stash."""
        sw = ClaudeAccountSwitcher()
        for slot_org, profile_org in (("ORG-A", None), ("", "ORG-B")):
            data = {"accounts": {
                "5": {"email": "u@x", "organizationUuid": slot_org,
                      "uuid": "u-1"},
            }}
            assert sw._slot_owning_resolved_identity(
                data, {"uuid": "u-1", "email": "u@x",
                       "organizationUuid": profile_org}) == "5", (
                f"slot org {slot_org!r} vs profile org {profile_org!r}")

    def test_a_slot_removed_under_the_adopt_is_not_resurrected(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        """The owner is resolved from a roster snapshot read BEFORE the lock,
        and `remove_account` holds no lock at all. A removal landing in the
        gap leaves an absent backup — which bypasses both remaining guards,
        because they are conditional on `stored` — and the write puts a live
        refresh token back on disk for a slot the user deleted."""
        from claude_swap.locking import FileLock as _RealFileLock

        s = self._owner_slot_fixture(sample_sequence_data)
        s.platform = Platform.LINUX   # .enc is the served backend everywhere

        class RemoveWhileWeWait(_RealFileLock):
            """The removal lands in the window the adopt waits out: after the
            owner was resolved, before the lock is held."""

            def __enter__(self):
                data = s._get_sequence_data()
                data["accounts"].pop("1", None)
                s._write_json(s.sequence_file, data)
                return super().__enter__()

        with patch("claude_swap.switcher.FileLock", RemoveWhileWeWait):
            self._resync_as_slot_2(s, self._blob("rt-live"))
        assert "1" not in (s._get_sequence_data() or {}).get("accounts", {}), (
            "PREMISE: the probe must actually have removed the slot"
        )
        assert not list(s.credentials_dir.glob(".creds-1-*.enc")), (
            "a removed slot was resurrected with a live refresh token"
        )

    @pytest.mark.parametrize("bind_strike", [False, True],
                             ids=["unbound-strike", "bound-strike"])
    def test_the_adopt_lifts_the_quarantine_it_wrote_over(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, bind_strike,
    ):
        """The strike condemned the generation this write just replaced.
        ``_row_eligible`` gates the fetch on the RAW count, so a strike left
        standing keeps a slot that now holds a working login out of every
        collect pass — and an unbound strike (no ``struckFingerprint``) never
        heals itself, because the collector's heal branch needs a fingerprint
        to disagree with. The switch-time foreign-credential heal pairs its
        write with ``clear_dead_token`` for exactly this reason."""
        s = self._owner_slot_fixture(sample_sequence_data)
        s._write_account_credentials("1", "c@example.com", self._blob("rt-dead"))
        ids = {"1": ("c@example.com", "o-1")}
        s._usage_store.record({"1": FetchRecord(
            error="invalid_grant",
            struck_fp=oauth.credential_fingerprint(self._blob("rt-dead"))
            if bind_strike else None,
        )}, ids)
        self._resync_as_slot_2(s, self._blob("rt-live"))
        got = json.loads(s._read_account_credentials("1", "c@example.com"))
        assert got["claudeAiOauth"]["refreshToken"] == "rt-live", "premise"
        assert s._usage_store.entries(ids)["1"].auth_dead_strikes == 0, (
            "the slot holds a fresh login and is still quarantined: "
            "_row_eligible refuses to fetch it"
        )

    def test_an_adopted_login_becomes_the_active_slot(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        """THE ROSTER FOLLOWS THE LOGIN. The live store holds slot 1's
        credential, so slot 1 is the active account. Until the roster says
        so, `_live_login_identity` falls back to the config's account the
        moment the pin splice returns the config to the pin.

        Measured on both Macs 2026-09-02 06:15-06:24Z after a /login as
        slot 2 with slot 3 recorded: nine minutes of "Account-1: usage
        unknown", the old stored token struck again, and the login reached
        its slot only through the failover's stash."""
        sample_sequence_data["activeAccountNumber"] = 2
        s = self._owner_slot_fixture(sample_sequence_data)
        live = self._blob("rt-live")
        s._write_credentials(live)
        self._resync_as_slot_2(s, live)
        assert s._get_sequence_data()["activeAccountNumber"] == 1
        assert json.loads(s._read_account_credentials(
            "1", "c@example.com"))["claudeAiOauth"]["refreshToken"] == "rt-live"

    def test_a_live_store_holding_a_slots_own_credential_moves_the_roster_to_it(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        """MEASURED: a recovery had restored slot 1's stored grant into the
        live store while the roster named another slot. Every pass read
        "same lineage, nothing drifted" and returned, so the roster never
        followed and row 12 of the gate stayed red on a machine whose login
        was exactly what the engine reported."""
        sample_sequence_data["activeAccountNumber"] = 2
        s = self._owner_slot_fixture(sample_sequence_data)
        live = self._blob("rt-own")
        s._write_account_credentials("1", "c@example.com", live)
        s._write_credentials(live)
        with patch("claude_swap.oauth.fetch_oauth_profile") as oracle:
            s._resync_rotated_backup("1", "c@example.com", "o-1", live)
        oracle.assert_not_called()          # same lineage: nothing to attribute
        assert s._get_sequence_data()["activeAccountNumber"] == 1

    def test_CONTROL_the_roster_does_not_follow_bytes_the_live_store_no_longer_holds(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        sample_sequence_data["activeAccountNumber"] = 2
        s = self._owner_slot_fixture(sample_sequence_data)
        own = self._blob("rt-own")
        s._write_account_credentials("1", "c@example.com", own)
        s._write_credentials(self._blob("rt-elsewhere"))
        with patch("claude_swap.oauth.fetch_oauth_profile") as oracle:
            s._resync_rotated_backup("1", "c@example.com", "o-1", own)
        oracle.assert_not_called()
        assert s._get_sequence_data()["activeAccountNumber"] == 2

    def test_the_roster_stays_when_the_live_store_moved_on(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        """CONTROL for the move: the oracle was asked about a credential a
        switch has since replaced. That switch moved the roster to its own
        target; naming slot 1 active now would point the roster at a slot
        whose credential is not in the live store."""
        sample_sequence_data["activeAccountNumber"] = 2
        s = self._owner_slot_fixture(sample_sequence_data)
        s._write_credentials(self._blob("rt-other"))
        self._resync_as_slot_2(s, self._blob("rt-live"))
        assert s._get_sequence_data()["activeAccountNumber"] == 2

    def test_a_later_login_replaces_a_healthy_stored_credential(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        """A LATER LOGIN DOES NOT WAIT FOR THE SLOT TO DIE, the rule the
        switch-time heal (`_adopt_into_dead_slot`) already applies.

        Measured 2026-09-02 06:15Z on the work Mac: the /login's first pass
        cleared slot 2's strike because the live credential was fresh, which
        made the slot look healthy, which made this refuse, which let the old
        stored token strike again two minutes later."""
        s = self._owner_slot_fixture(sample_sequence_data)
        s._write_account_credentials(
            "1", "c@example.com", self._blob("rt-old", refresh_expires_at=1_000))
        # 8s past the lineage-rotation jitter: unambiguously a later login,
        # not the sub-second re-mint of the stored generation.
        live = self._blob("rt-new", refresh_expires_at=1_000 + 8_000)
        s._write_credentials(live)
        self._resync_as_slot_2(s, live)
        assert json.loads(s._read_account_credentials(
            "1", "c@example.com"))["claudeAiOauth"]["refreshToken"] == "rt-new"

    def test_an_older_login_leaves_a_healthy_stored_credential(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        """CONTROL: the ordering is one-directional. A live credential from an
        EARLIER login (a stale cross-machine copy) must not replace a healthy
        later one, and the roster must not move to it either."""
        sample_sequence_data["activeAccountNumber"] = 2
        s = self._owner_slot_fixture(sample_sequence_data)
        s._write_account_credentials(
            "1", "c@example.com", self._blob("rt-cur", refresh_expires_at=2_000))
        live = self._blob("rt-old", refresh_expires_at=1_000)
        s._write_credentials(live)
        self._resync_as_slot_2(s, live)
        assert json.loads(s._read_account_credentials(
            "1", "c@example.com"))["claudeAiOauth"]["refreshToken"] == "rt-cur"
        assert s._get_sequence_data()["activeAccountNumber"] == 2

    def _resync_as_slot_2_with(self, s, live, profile):
        with patch("claude_swap.oauth.fetch_oauth_profile",
                   return_value=profile):
            s._resync_rotated_backup("2", "b@example.com", "o-2", live)

    def test_a_login_for_an_unmanaged_account_gets_a_slot_and_becomes_active(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        """A /login is the whole registration. An account no slot owns used
        to be logged as "no managed slot owns it; backup left untouched" and
        the login survived only until the next switch replaced it."""
        sample_sequence_data["activeAccountNumber"] = 2
        s = self._owner_slot_fixture(sample_sequence_data)
        live = self._blob("rt-new")
        s._write_credentials(live)
        self._resync_as_slot_2_with(s, live, {
            "uuid": "u-9", "email": "z@example.com", "organizationUuid": "o-9"})
        data = s._get_sequence_data()
        row = data["accounts"].get("3")
        assert row and row["email"] == "z@example.com" and row["uuid"] == "u-9", data
        assert 3 in data["sequence"]
        assert data["activeAccountNumber"] == 3
        assert json.loads(s._read_account_credentials(
            "3", "z@example.com"))["claudeAiOauth"]["refreshToken"] == "rt-new"
        assert s._read_account_config("3", "z@example.com"), (
            "the new slot has no config backup")

    def test_a_registered_slot_exports_under_its_own_identity(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, tmp_path: Path,
    ):
        """`export` reads the slot's STORED config and treats a slot without
        one as broken -- a named account raises, a bulk export skips it -- so
        a registered slot needs one even though a switch could rebuild it.

        And it must be built from the resolved profile, never copied from the
        live `~/.claude.json`: this path is reached BECAUSE the live
        credential does not resolve to the slot the roster names, so that file
        may still describe a different account, and both a switch and
        `_slim_config` carry whatever is stored here verbatim.

        BOTH DEFECTS, measured against this one case: copying the live config
        back fails on the identity asserts, deleting the write fails on the
        export itself. A case that reads `_target_config` instead sees only
        the first -- the rebuild answers correctly when nothing is stored.
        """
        from claude_swap import transfer

        sample_sequence_data["activeAccountNumber"] = 2
        s = self._owner_slot_fixture(sample_sequence_data)
        live = self._blob("rt-new")
        s._write_credentials(live)
        # PREMISE: the live config names neither the account being registered
        # nor the slot the resync ran for, so a copy of it is visibly foreign.
        assert json.loads(mock_claude_config.read_text(encoding="utf-8"))[
            "oauthAccount"]["emailAddress"] == "test@example.com"
        self._resync_as_slot_2_with(s, live, {
            "uuid": "u-9", "email": "z@example.com", "organizationUuid": "o-9"})

        dest = tmp_path / "bundle-199.json"
        transfer.export_accounts(s, str(dest), account="z@example.com")

        exported = json.loads(dest.read_text(encoding="utf-8"))["accounts"]
        assert [e["email"] for e in exported] == ["z@example.com"], exported
        assert exported[0]["config"]["oauthAccount"]["emailAddress"] == (
            "z@example.com"), exported[0]["config"]
        assert exported[0]["config"]["oauthAccount"]["accountUuid"] == "u-9"

    def test_a_login_the_live_store_no_longer_holds_registers_nothing(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        """CONTROL: the server was asked about bytes a switch has since
        replaced. A slot for them would be a phantom."""
        sample_sequence_data["activeAccountNumber"] = 2
        s = self._owner_slot_fixture(sample_sequence_data)
        s._write_credentials(self._blob("rt-other"))
        self._resync_as_slot_2_with(s, self._blob("rt-new"), {
            "uuid": "u-9", "email": "z@example.com", "organizationUuid": "o-9"})
        data = s._get_sequence_data()
        assert "3" not in data["accounts"], data["accounts"].keys()
        assert data["activeAccountNumber"] == 2

    def test_a_partial_profile_registers_nothing(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        """CONTROL: a uuid with no address names no account the roster can
        show, and a row nobody can read is worse than no row."""
        sample_sequence_data["activeAccountNumber"] = 2
        s = self._owner_slot_fixture(sample_sequence_data)
        live = self._blob("rt-new")
        s._write_credentials(live)
        self._resync_as_slot_2_with(s, live, {"uuid": "u-9"})
        data = s._get_sequence_data()
        assert "3" not in data["accounts"], data["accounts"].keys()
        assert data["activeAccountNumber"] == 2

    def test_a_restored_credential_the_slot_already_holds_still_becomes_active(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        """The slot already holds this exact lineage, so nothing is written,
        but the live store holds it and the roster does not say so. A restore
        that moved nothing else is a login for this purpose."""
        sample_sequence_data["activeAccountNumber"] = 2
        s = self._owner_slot_fixture(sample_sequence_data)
        same = self._blob("rt-same")
        s._write_account_credentials("1", "c@example.com", same)
        s._write_credentials(same)
        with patch.object(s, "_write_account_credentials") as write:
            self._resync_as_slot_2(s, same)
        write.assert_not_called()
        assert s._get_sequence_data()["activeAccountNumber"] == 1


class TestLineageJitterNeverAdoptsIntoAHealthySlot:
    """`_adopt_into_dead_slot`'s healthy-slot door: a credential whose refresh
    lifetime ends LATER than the slot's own is treated as a later login and
    adopted without --force. The server re-mints `refreshTokenExpiresAt` on
    every refresh of the same lineage with sub-second jitter, sign arbitrary,
    so that must not read as a later login."""

    @staticmethod
    def _blob(refresh, refresh_expires_at):
        return json.dumps({"claudeAiOauth": {
            "accessToken": "sk-" + refresh, "refreshToken": refresh,
            "expiresAt": 99_999_999_999_999,
            "refreshTokenExpiresAt": refresh_expires_at}})

    def _fixture(self, sample_sequence_data):
        sample_sequence_data["accounts"]["2"]["email"] = "b@example.com"
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        s._write_json(s.sequence_file, sample_sequence_data)
        return s

    @pytest.mark.parametrize("jitter_ms", [427, -427])
    def test_a_same_lineage_stamp_jitter_does_not_adopt(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict, jitter_ms: int
    ):
        s = self._fixture(sample_sequence_data)
        base = 99_999_999_999_000
        stored = self._blob("rt-stored", base)
        s._write_account_credentials("2", "b@example.com", stored)
        incoming = self._blob("rt-incoming", base + jitter_ms)
        adopted = s._adopt_into_dead_slot("2", incoming, sample_sequence_data)
        assert adopted is False, (
            f"a {jitter_ms}ms stamp jitter adopted a foreign credential into "
            "a healthy slot"
        )
        got = json.loads(s._read_account_credentials("2", "b@example.com"))
        assert got["claudeAiOauth"]["refreshToken"] == "rt-stored"

    def test_CONTROL_a_stamp_8s_later_adopts(
        self, temp_home: Path, mock_claude_config: Path,
        sample_sequence_data: dict
    ):
        s = self._fixture(sample_sequence_data)
        base = 99_999_999_999_000
        stored = self._blob("rt-stored", base)
        s._write_account_credentials("2", "b@example.com", stored)
        incoming = self._blob("rt-incoming", base + 8_000)
        adopted = s._adopt_into_dead_slot("2", incoming, sample_sequence_data)
        assert adopted is True, "a real newer login (8s later) must adopt"
        got = json.loads(s._read_account_credentials("2", "b@example.com"))
        assert got["claudeAiOauth"]["refreshToken"] == "rt-incoming"


class TestCurrentAtLimitOverridesTheFrozenPct:
    """`current_at_limit=True` — a caller measured the limit off the poll.

    The account under load is the one the usage endpoint 429s, so the slot
    needing a current number has the oldest, and `decision_value()` serves a
    frozen `last_good` as a valid lower bound. A usage lower bound is a
    HEADROOM UPPER bound: right for a destination, wrong for the account being
    left, which then outranks every candidate and `best` reports `stay`.

    The pin proxy sees a 429 on `/v1/messages` — earlier and stronger evidence
    than any percentage, and the one signal nothing consumes today.
    """

    def _setup(self, temp_home: Path) -> ClaudeAccountSwitcher:
        s = ClaudeAccountSwitcher()
        s.platform = Platform.LINUX
        s._setup_directories()
        s._init_sequence_file()
        return s

    def _seed(self, s: ClaudeAccountSwitcher, num: int, email: str) -> None:
        s._write_account_credentials(
            str(num), email,
            json.dumps({"claudeAiOauth": {
                "accessToken": f"sk-{num}", "refreshToken": f"rt-{num}"}}),
        )
        s._write_account_config(
            str(num), email,
            json.dumps({"oauthAccount": {
                "emailAddress": email, "accountUuid": f"uuid-{num}"}}),
        )
        data = s._get_sequence_data()
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

    @staticmethod
    def _usage(pct: float) -> dict:
        return {"five_hour": {"pct": pct}, "seven_day": {"pct": 0.0}}

    def test_a_frozen_low_active_still_loses_when_told_it_is_at_limit(
        self, temp_home
    ):
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        # The live shape: the 429'd slot reads BETTER than the healthy one.
        usage = {"1": self._usage(20.0), "2": self._usage(56.0)}

        target, note = s._select_best_switchable(
            "1", usage=usage, current_at_limit=True
        )

        assert (target, note) == ("2", "")

    def test_the_same_call_without_the_flag_stays_put(self, temp_home):
        # The control: 20% really does beat 56%, so only the flag can move it.
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        usage = {"1": self._usage(20.0), "2": self._usage(56.0)}

        assert s._select_best_switchable("1", usage=usage) == (None, "stay")

    def test_every_candidate_at_its_limit_is_still_exhausted(self, temp_home):
        # The flag says "leave", never "leave for somewhere no better". With
        # nowhere to go the caller must relay the 429 rather than burn a swap.
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        usage = {"1": self._usage(20.0), "2": self._usage(100.0)}

        assert s._select_best_switchable(
            "1", usage=usage, current_at_limit=True
        ) == (None, "exhausted")

    def test_an_unreadable_active_no_longer_blocks_the_escape(self, temp_home):
        # Without the flag an unmeasurable active returns "current-unavailable"
        # and stays, because nothing can be proven better. The flag IS the
        # measurement, so the escape must not need a second one.
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        usage = {"2": self._usage(10.0)}

        assert s._select_best_switchable("1", usage=usage) == (
            None, "current-unavailable"
        )
        assert s._select_best_switchable(
            "1", usage=usage, current_at_limit=True
        ) == ("2", "")


class TestSwitchOffAtLimitAccount:
    """The seam the pin proxy calls when it sees a 429 on `/v1/messages`."""

    def test_it_switches_and_names_the_account_it_landed_on(self, temp_home):
        s = TestCurrentAtLimitOverridesTheFrozenPct()._setup(temp_home)
        seed = TestCurrentAtLimitOverridesTheFrozenPct()._seed
        seed(s, 1, "a@example.com")
        seed(s, 2, "b@example.com")
        (temp_home / ".claude" / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "sk-live"}})
        )
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "a@example.com",
                             "accountUuid": "uuid-1"}
        }))
        usage = {"1": {"five_hour": {"pct": 20.0}, "seven_day": {"pct": 0.0}},
                 "2": {"five_hour": {"pct": 56.0}, "seven_day": {"pct": 0.0}}}

        with patch.object(s, "_usage_by_account", return_value=usage):
            result = switch_off_at_limit_account(s)

        assert result["switched"] is True
        assert result["to"]["email"] == "b@example.com"
        assert s._get_sequence_data()["activeAccountNumber"] == 2

    def test_nowhere_to_go_reports_it_rather_than_switching(self, temp_home):
        s = TestCurrentAtLimitOverridesTheFrozenPct()._setup(temp_home)
        seed = TestCurrentAtLimitOverridesTheFrozenPct()._seed
        seed(s, 1, "a@example.com")
        seed(s, 2, "b@example.com")
        # A live login is load-bearing: without one, switch() takes the
        # fresh-machine path, which ignores the strategy entirely and rotates.
        (temp_home / ".claude" / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "sk-live"}})
        )
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "a@example.com",
                             "accountUuid": "uuid-1"}
        }))
        usage = {"1": {"five_hour": {"pct": 20.0}, "seven_day": {"pct": 0.0}},
                 "2": {"five_hour": {"pct": 100.0}, "seven_day": {"pct": 0.0}}}

        with patch.object(s, "_usage_by_account", return_value=usage):
            result = switch_off_at_limit_account(s)

        assert result["switched"] is False
        assert result["reason"] == "candidates-exhausted"
        assert s._get_sequence_data()["activeAccountNumber"] == 1

    def test_it_does_not_build_an_engine(self, temp_home):
        """A LIVE engine holds `.auto-live.lock` for its whole lifetime, so a
        second engine demotes itself to dry-run and switches nothing. This
        seam must therefore never route through one — and `at-limit` does not
        need to, since it already skips cooldown, the no-return bar and
        hysteresis. Measured on the live host: the TUI held the lock, so an
        engine-tick version of this would have relayed every 429 untouched
        while passing every test that did not hold the lock first.

        AT RUNTIME, not by reading the source for a name. The text form was
        defeated by anything that moved the construction one call away --
        a helper, or `getattr(mod, "Auto" + "SwitchEngine")` -- and `switch()`
        is 391 lines delegating to 14 private helpers a substring never
        follows into.
        """
        from claude_swap import autoswitch as autoswitch_mod

        built: list[int] = []
        real_init = autoswitch_mod.AutoSwitchEngine.__init__

        def spy(self, *a, **kw):
            built.append(1)
            return real_init(self, *a, **kw)

        s = TestCurrentAtLimitOverridesTheFrozenPct()._setup(temp_home)
        seed = TestCurrentAtLimitOverridesTheFrozenPct()._seed
        seed(s, 1, "a@example.com")
        seed(s, 2, "b@example.com")
        (temp_home / ".claude" / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "sk-live"}})
        )
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "a@example.com",
                             "accountUuid": "uuid-1"}
        }))
        usage = {"1": {"five_hour": {"pct": 100.0}, "seven_day": {"pct": 0.0}},
                 "2": {"five_hour": {"pct": 5.0}, "seven_day": {"pct": 0.0}}}

        with patch.object(autoswitch_mod.AutoSwitchEngine, "__init__", spy):
            # THE CONTROL, first: an instrument that cannot see a construction
            # reports the same empty list as a seam that makes none.
            with contextlib.suppress(Exception):
                autoswitch_mod.AutoSwitchEngine(s, None, None)
            assert built, "the spy never fired — it cannot answer this"
            built.clear()

            with patch.object(s, "_usage_by_account", return_value=usage):
                switch_off_at_limit_account(s)
            assert built == [], "at-limit routed through an engine"

            with contextlib.suppress(Exception):
                s.switch()
            assert built == [], "switch() routed through an engine"


class TestAnEmptySlotLandingKeepsTheWarningsAlreadyEarned:
    """`_switch_to_empty_slot` returned `"warnings": [note]`, replacing whatever
    `_perform_switch` had already accumulated.

    Step 1 classifies the OUTGOING credential and may append the ownership
    mismatch warning -- the one naming the slot whose credential was stashed and
    the command that puts it back. Step 2 then finds the target empty and takes
    this early return, which dropped it.

    Human callers still saw it: `_warn` printed at the time. JSON callers did
    not, and the TUI and the menu bar are JSON callers -- so the recovery step
    for a stashed credential reached nobody exactly where a person is least able
    to reconstruct it.
    """

    def _setup(self, temp_home: Path) -> ClaudeAccountSwitcher:
        s = ClaudeAccountSwitcher()
        s.platform = Platform.LINUX
        s._setup_directories()
        s._init_sequence_file()
        return s

    def _land(self, s, earned):
        return s._switch_to_empty_slot(
            "2", "b@example.com", None, {"number": 2},
            s._get_sequence_data(), False, warnings_out=earned,
        )

    def test_the_ownership_warning_survives_the_landing(self, temp_home: Path):
        s = self._setup(temp_home)
        earned = ["Credential ownership mismatch detected — run: cswap add --slot 2"]
        out = self._land(s, list(earned))
        joined = " ".join(out.get("warnings", []))
        assert "cswap add --slot 2" in joined, (
            "the landing replaced the accumulated warnings with its own note, "
            f"so the stashed credential's recovery step is gone: {out}"
        )

    def test_its_own_note_is_still_there(self, temp_home: Path):
        """THE CONTROL. Appending is not enough if it drops the note this
        return exists to deliver."""
        s = self._setup(temp_home)
        out = self._land(s, ["earlier"])
        joined = " ".join(out.get("warnings", []))
        assert "no stored credentials" in joined, f"the landing note is gone: {out}"
        assert out.get("needsLogin") is True

    def test_no_earned_warnings_is_just_the_note(self, temp_home: Path):
        s = self._setup(temp_home)
        out = self._land(s, [])
        assert len(out.get("warnings", [])) == 1, out


def test_keychain_blind_probes_rather_than_reading_an_unset_cache(
    temp_home: Path, monkeypatch
):
    """The probe IS the guard, and deleting it left the suite green.

    `_keychain_blind` answers "macOS cannot read the Keychain right now",
    which is what stops a logout from clearing a working login to reach a
    slot that was never empty. Only a real `security` call fills the cache,
    and the paths that matter never make one — `--force` skips the
    live-identity prefetch and an `.enc`-satisfied read short-circuits before
    the keychain — so without the probe a locked keychain is indistinguishable
    from a probed one and the answer is False either way.

    Measured before this: removing the two probe lines left 2300 passed.
    """
    switcher = ClaudeAccountSwitcher()
    switcher.platform = Platform.MACOS
    switcher._store._keychain_usable_cache = None

    probed: list[int] = []

    def locked_read():
        probed.append(1)
        # What a declined `security` leaves: the cache says unusable.
        switcher._store._keychain_usable_cache = False
        return ActiveCredentials("", True)

    monkeypatch.setattr(switcher._store, "_read_active_credentials", locked_read)
    switcher._keychain_blind()

    assert probed == [1], (
        "the cache was unset and nothing probed it, so the answer came from "
        "'nobody has asked yet' rather than from the keychain — and a logout "
        "here clears a working login to reach a slot that is not empty"
    )


def test_keychain_blind_does_not_reprobe_a_cache_already_filled(
    temp_home: Path, monkeypatch
):
    """THE CONTROL. Probing unconditionally would pass the case above and put
    a `security` call on every read, including the ones that short-circuited
    precisely to avoid it."""
    switcher = ClaudeAccountSwitcher()
    switcher.platform = Platform.MACOS
    switcher._store._keychain_usable_cache = True

    probed: list[int] = []
    monkeypatch.setattr(
        switcher._store, "_read_active_credentials",
        lambda: probed.append(1) or ActiveCredentials("", False))

    switcher._keychain_blind()
    assert probed == [], "a filled cache was probed again"


def test_a_vetoed_landing_leaves_no_stash_entry_behind(
    temp_home: Path, monkeypatch
):
    """"Nothing was changed" is said one statement after something was.

    The veto sits BELOW `_stash_live_credential`, so every refusal writes a
    consume-gate entry and its manifest first and then reports that nothing
    happened. Retrying accumulates them — one `.enc` per attempt, each holding
    the live credential, none of them claimed by anything.

    The veto reads `~/.claude.json` and nothing else; the stash cannot make
    that readable, so there is no ordering reason for it to run first.
    """
    switcher = ClaudeAccountSwitcher()
    cred = temp_home / ".claude" / ".credentials.json"
    cred.parent.mkdir(parents=True, exist_ok=True)
    cred.write_text('{"claudeAiOauth": {"accessToken": "live"}}')
    (temp_home / ".claude.json").write_text("{ this is not json")

    data = {"activeAccountNumber": 1,
            "accounts": {"1": {"email": "a@example.com"},
                         "2": {"email": "b@example.com"}}}

    def land():
        with pytest.raises(SwitchError):
            switcher._switch_to_empty_slot(
                "2", "b@example.com", None, {"num": "2"}, data,
                emit_output=False, warnings_out=[])

    cred_dir = switcher._store._host.credentials_dir
    land()
    after_one = sorted(p.name for p in cred_dir.glob(".unclaimed-*"))
    land()
    after_two = sorted(p.name for p in cred_dir.glob(".unclaimed-*"))

    assert after_one == [], (
        f"a refusal that says 'Nothing was changed' stashed {after_one}"
    )
    # THE ACCUMULATION, which is what makes it more than a wording problem:
    # a per-attempt copy of a live credential nothing will ever claim.
    assert after_two == after_one, (
        f"a second refusal added {sorted(set(after_two) - set(after_one))}"
    )


def test_a_torn_config_vetoes_the_landing_before_anything_is_destroyed(
    temp_home: Path, monkeypatch
):
    """The veto is a pure READ, so it must run before the unlink.

    `_clear_oauth_credential()` removes `.credentials.json` first; only then
    does `_clear_managed_key()` report False for a present-but-unreadable
    `~/.claude.json`, and the refusal fires with the live credential already
    gone. The command reports failure and has logged the user out — and no
    rollback runs on this call site.

    Nothing about the torn config becomes knowable by destroying the
    credential, so the order is the whole defect.
    """
    from claude_swap import switcher as switcher_mod

    switcher = ClaudeAccountSwitcher()
    cred = temp_home / ".claude" / ".credentials.json"
    cred.parent.mkdir(parents=True, exist_ok=True)
    cred.write_text('{"claudeAiOauth": {"accessToken": "live"}}')
    # Present and unreadable: `_read_global_config` collapses this to None.
    (temp_home / ".claude.json").write_text("{ this is not json")

    warnings_out: list[str] = []
    data = {"activeAccountNumber": 1,
            "accounts": {"1": {"email": "a@example.com"},
                         "2": {"email": "b@example.com"}}}
    with pytest.raises(SwitchError):
        switcher._switch_to_empty_slot(
            "2", "b@example.com", None, {"num": "2"}, data,
            emit_output=False, warnings_out=warnings_out)

    assert cred.exists(), (
        "the landing refused AND deleted the live credential — the fact it "
        "refused on was readable before the unlink"
    )
