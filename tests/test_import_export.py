"""Tests for import and export functionality."""

from __future__ import annotations

import json
import zipfile
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

try:
    import keyring
except ImportError:
    keyring = None

from claude_swap.exceptions import ValidationError
from claude_swap.switcher import ClaudeAccountSwitcher


class TestImportExport:
    """Test import and export functionality."""

    @pytest.fixture(autouse=True)
    def setup_keyring(self):
        """Mock keyring for all tests to avoid keychain access on macOS."""
        if sys.platform != "linux":
            with patch("keyring.get_password", return_value='{"token": "test-token"}'), \
                 patch("keyring.set_password"), \
                 patch("keyring.delete_password"):
                yield
        else:
            yield

    def test_export_import_cycle(self, temp_home: Path, mock_claude_config: Path, mock_credentials_file: Path):
        """Test a full export and import cycle."""
        switcher = ClaudeAccountSwitcher()
        
        # 1. Add current account to managed accounts
        with patch.object(switcher, "_read_credentials", return_value='{"token": "test-token"}'):
            switcher.add_account()
        
        # Verify it was added
        data = switcher._get_sequence_data()
        assert "1" in data["accounts"]
        email = data["accounts"]["1"]["email"]
        
        # 2. Export the account
        export_path = temp_home / "export.zip"
        switcher.export_account("1", str(export_path))
        
        assert export_path.exists()
        
        # Verify zip contents
        with zipfile.ZipFile(export_path, 'r') as zf:
            assert "config.json" in zf.namelist()
            assert "credentials.txt" in zf.namelist()
            
        # 3. Remove the account
        with patch("builtins.input", return_value="y"):
            switcher.remove_account("1")
            
        data = switcher._get_sequence_data()
        assert "1" not in data["accounts"]
        
        # 4. Import the account back
        switcher.import_account(str(export_path))
        
        # 5. Verify restored
        data = switcher._get_sequence_data()
        # It might get a different number if sequence was cleared, but here it should likely be "1" or "2"
        # Since we max() + 1, and we removed "1", it might be "1" again if sequence empty or next available
        found = False
        for acc in data["accounts"].values():
            if acc["email"] == email:
                found = True
                break
        assert found, f"Account {email} was not restored"

    def test_import_missing_file(self, temp_home: Path):
        """Test import from non-existent file."""
        switcher = ClaudeAccountSwitcher()
        with pytest.raises(ValidationError, match="Archive not found"):
            switcher.import_account("nonexistent.zip")

    def test_import_invalid_archive(self, temp_home: Path):
        """Test import from archive missing required files."""
        switcher = ClaudeAccountSwitcher()
        invalid_zip = temp_home / "invalid.zip"
        
        with zipfile.ZipFile(invalid_zip, 'w') as zf:
            zf.writestr("random.txt", "content")
            
        with pytest.raises(ValidationError, match="must contain config.json and credentials.txt"):
            switcher.import_account(str(invalid_zip))

    def test_import_invalid_json(self, temp_home: Path):
        """Test import with malformed config.json."""
        switcher = ClaudeAccountSwitcher()
        invalid_zip = temp_home / "invalid_json.zip"
        
        with zipfile.ZipFile(invalid_zip, 'w') as zf:
            zf.writestr("config.json", "{invalid json")
            zf.writestr("credentials.txt", "creds")
            
        with pytest.raises(ValidationError, match="Invalid config.json"):
            switcher.import_account(str(invalid_zip))

    def test_import_missing_email(self, temp_home: Path):
        """Test import with config.json missing email."""
        switcher = ClaudeAccountSwitcher()
        invalid_zip = temp_home / "no_email.zip"
        
        # Config without email
        config = {"oauthAccount": {"accountUuid": "uuid"}}
        
        with zipfile.ZipFile(invalid_zip, 'w') as zf:
            zf.writestr("config.json", json.dumps(config))
            zf.writestr("credentials.txt", "creds")
            
        with pytest.raises(ValidationError, match="Could not find email address"):
            switcher.import_account(str(invalid_zip))

    def test_import_duplicate_account(self, temp_home: Path, mock_claude_config: Path, mock_credentials_file: Path):
        """Test importing an account that already exists."""
        switcher = ClaudeAccountSwitcher()
        
        # 1. Add current account
        with patch.object(switcher, "_read_credentials", return_value='{"token": "test-token"}'):
            switcher.add_account()
            
        # 2. Create an export of it
        export_path = temp_home / "export.zip"
        switcher.export_account("1", str(export_path))
        
        # 3. Try to import it again
        with patch("builtins.print") as mock_print:
            switcher.import_account(str(export_path))
            mock_print.assert_any_call("Account test@example.com is already managed.")
            
        # Verify no new account was added
        data = switcher._get_sequence_data()
        assert len(data["accounts"]) == 1
