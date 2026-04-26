"""Core account switcher logic for Claude Code."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Only import keyring on non-Linux platforms
if sys.platform != "linux":
    import keyring

from claude_swap.exceptions import (
    AccountNotFoundError,
    ConfigError,
    CredentialReadError,
    CredentialWriteError,
    SwitchError,
    ValidationError,
)
from claude_swap import oauth
from claude_swap.cache import MISSING, read_cache, write_cache
from claude_swap.locking import FileLock
from claude_swap.logging_config import setup_logging
from claude_swap.models import Platform, SwitchTransaction, get_timestamp
from claude_swap.printer import (
    abbreviate_path,
    accent,
    bold_accent,
    bolded,
    dimmed,
    entrypoint_label,
    error,
    format_age,
    ide_short_name,
    muted,
    warning,
)
from claude_swap.paths import (
    get_claude_config_home,
    get_credentials_path,
    get_global_config_path,
)
from claude_swap.process_detection import get_running_instances

# Service name for keyring storage
KEYRING_SERVICE = "claude-code"
KEYRING_ACTIVE_USERNAME = "active-credentials"

# Usage cache
_USAGE_CACHE_TTL = 15  # seconds

class ClaudeAccountSwitcher:
    """Multi-account switcher for Claude Code."""

    def __init__(self, debug: bool = False):
        self.home = Path.home()
        self.backup_dir = self.home / ".claude-swap-backup"
        self.sequence_file = self.backup_dir / "sequence.json"
        self.configs_dir = self.backup_dir / "configs"
        self.credentials_dir = self.backup_dir / "credentials"
        self.lock_file = self.backup_dir / ".lock"
        self.platform = Platform.detect()
        self._logger = setup_logging(self.backup_dir, debug=debug)

    def _is_running_in_container(self) -> bool:
        """Check if running inside a container."""
        # Check environment variables (works on all platforms)
        if os.environ.get("CONTAINER") or os.environ.get("container"):
            return True

        # Windows doesn't have the same container indicators
        if self.platform == Platform.WINDOWS:
            return False

        # Check for Docker environment file (Linux/macOS)
        if Path("/.dockerenv").exists():
            return True

        # Check cgroup for container indicators (Linux)
        cgroup_path = Path("/proc/1/cgroup")
        if cgroup_path.exists():
            try:
                content = cgroup_path.read_text()
                if any(
                    x in content
                    for x in ["docker", "lxc", "containerd", "kubepods"]
                ):
                    return True
            except PermissionError:
                pass

        # Check mount info (Linux)
        mountinfo_path = Path("/proc/self/mountinfo")
        if mountinfo_path.exists():
            try:
                content = mountinfo_path.read_text()
                if any(x in content for x in ["docker", "overlay"]):
                    return True
            except PermissionError:
                pass

        return False

    def _get_claude_config_path(self) -> Path:
        """Get the Claude configuration file path, mirroring claude-code."""
        return get_global_config_path()

    def _validate_email(self, email: str) -> bool:
        """Validate email format."""
        pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
        return bool(re.match(pattern, email))

    def _setup_directories(self) -> None:
        """Create backup directories with proper permissions."""
        for directory in [self.backup_dir, self.configs_dir, self.credentials_dir]:
            directory.mkdir(parents=True, exist_ok=True)
            if sys.platform != "win32":
                os.chmod(directory, 0o700)

    def _read_json(self, path: Path) -> dict | None:
        """Read and parse JSON file."""
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._logger.warning(f"Invalid JSON in {path}")
            return None

    def _write_json(self, path: Path, data: dict) -> None:
        """Write JSON file with validation."""
        content = json.dumps(data, indent=2)

        # Write to temp file first
        temp_path = path.with_suffix(f".{os.getpid()}.tmp")
        temp_path.write_text(content, encoding="utf-8")

        # Validate written content
        try:
            json.loads(temp_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            temp_path.unlink()
            raise ConfigError("Generated invalid JSON")

        # Move to final location
        shutil.move(str(temp_path), str(path))
        if sys.platform != "win32":
            os.chmod(path, 0o600)

    def _read_credentials(self) -> str | None:
        """Read credentials from Claude Code's storage.

        Claude Code stores credentials in:
        - macOS: Keychain with service "Claude Code-credentials"
        - Linux/WSL/Windows: File at ~/.claude/.credentials.json

        Returns:
            Credentials string if found, empty string if not found, None on error.
        """
        if self.platform == Platform.MACOS:
            try:
                result = subprocess.run(
                    [
                        "security",
                        "find-generic-password",
                        "-a",
                        os.environ.get("USER", "user"),
                        "-s",
                        "Claude Code-credentials",
                        "-w",
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                return result.stdout.strip()
            except subprocess.CalledProcessError as e:
                if e.returncode == 44:  # Item not found
                    return ""
                self._logger.error(f"Failed to read credentials: {e}")
                return None
            except Exception as e:
                self._logger.error(f"Unexpected error reading credentials: {e}")
                return None
        else:  # Linux/WSL/Windows - credentials stored in file
            cred_file = get_credentials_path()
            if cred_file.exists():
                try:
                    return cred_file.read_text(encoding="utf-8")
                except Exception as e:
                    self._logger.error(f"Failed to read credentials file: {e}")
                    return None
            return ""

    def _write_credentials(self, credentials: str) -> None:
        """Write credentials to Claude Code's storage.

        Claude Code stores credentials in:
        - macOS: Keychain with service "Claude Code-credentials"
        - Linux/WSL/Windows: File at ~/.claude/.credentials.json

        Raises:
            CredentialWriteError: If writing credentials fails.
        """
        if self.platform == Platform.MACOS:
            result = subprocess.run(
                [
                    "security",
                    "add-generic-password",
                    "-U",
                    "-s",
                    "Claude Code-credentials",
                    "-a",
                    os.environ.get("USER", "user"),
                    "-w",
                    credentials,
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise CredentialWriteError(
                    f"Failed to write credentials: {result.stderr}"
                )
        else:  # Linux/WSL/Windows - credentials stored in file
            cred_dir = get_claude_config_home()
            cred_dir.mkdir(parents=True, exist_ok=True)
            cred_file = cred_dir / ".credentials.json"
            try:
                import tempfile
                fd, tmp_path = tempfile.mkstemp(dir=str(cred_dir), suffix=".tmp")
                try:
                    os.write(fd, credentials.encode("utf-8"))
                    os.close(fd)
                    fd = -1
                    os.replace(tmp_path, str(cred_file))
                    if sys.platform != "win32":
                        os.chmod(str(cred_file), 0o600)
                except BaseException:
                    if fd >= 0:
                        os.close(fd)
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                    raise
            except Exception as e:
                raise CredentialWriteError(f"Failed to write credentials: {e}")

    def _read_account_credentials(self, account_num: str, email: str) -> str:
        """Read account credentials from backup.

        On Linux/WSL: Uses file-based storage to avoid keyring backend issues.
        On macOS/Windows: Uses system keyring.
        """
        if self.platform in (Platform.LINUX, Platform.WSL):
            cred_file = self.credentials_dir / f".creds-{account_num}-{email}.enc"
            if cred_file.exists():
                try:
                    encoded = cred_file.read_text(encoding="utf-8")
                    return base64.b64decode(encoded).decode("utf-8")
                except Exception as e:
                    self._logger.warning(f"Failed to read credentials file: {e}")
                    return ""
            return ""
        else:
            # Use keyring for macOS/Windows
            username = f"account-{account_num}-{email}"
            try:
                creds = keyring.get_password(KEYRING_SERVICE, username)
                return creds if creds else ""
            except Exception as e:
                self._logger.warning(f"Failed to read credentials from keyring: {e}")
                return ""

    def _write_account_credentials(
        self, account_num: str, email: str, credentials: str
    ) -> None:
        """Write account credentials to backup.

        On Linux/WSL: Uses file-based storage to avoid keyring backend issues.
        On macOS/Windows: Uses system keyring.
        """
        if self.platform in (Platform.LINUX, Platform.WSL):
            cred_file = self.credentials_dir / f".creds-{account_num}-{email}.enc"
            try:
                encoded = base64.b64encode(credentials.encode("utf-8")).decode("utf-8")
                cred_file.write_text(encoded, encoding="utf-8")
                os.chmod(cred_file, 0o600)
            except Exception as e:
                self._logger.warning(f"Failed to write credentials file: {e}")
                raise
        else:
            # Use keyring for macOS/Windows
            username = f"account-{account_num}-{email}"
            try:
                keyring.set_password(KEYRING_SERVICE, username, credentials)
            except Exception as e:
                self._logger.warning(f"Failed to write credentials to keyring: {e}")
                raise

    def _delete_account_credentials(self, account_num: str, email: str) -> None:
        """Delete account credentials from backup.

        On Linux/WSL: Deletes file-based credential storage.
        On macOS/Windows: Removes from system keyring.
        """
        if self.platform in (Platform.LINUX, Platform.WSL):
            cred_file = self.credentials_dir / f".creds-{account_num}-{email}.enc"
            try:
                if cred_file.exists():
                    cred_file.unlink()
            except Exception as e:
                self._logger.warning(f"Failed to delete credentials file: {e}")
        else:
            # Use keyring for macOS/Windows
            username = f"account-{account_num}-{email}"
            try:
                keyring.delete_password(KEYRING_SERVICE, username)
            except keyring.errors.PasswordDeleteError:
                pass  # Credential doesn't exist, that's fine
            except Exception as e:
                self._logger.warning(f"Failed to delete credentials from keyring: {e}")

    def _delete_account_files(self, account_num: str, email: str) -> None:
        """Delete all backup files for an account (credentials + config)."""
        self._delete_account_credentials(account_num, email)
        config_file = self.configs_dir / f".claude-config-{account_num}-{email}.json"
        if config_file.exists():
            config_file.unlink()

    def _read_account_config(self, account_num: str, email: str) -> str:
        """Read account config from backup."""
        config_file = self.configs_dir / f".claude-config-{account_num}-{email}.json"
        if config_file.exists():
            return config_file.read_text(encoding="utf-8")
        return ""

    def _write_account_config(
        self, account_num: str, email: str, config: str
    ) -> None:
        """Write account config to backup."""
        config_file = self.configs_dir / f".claude-config-{account_num}-{email}.json"
        config_file.write_text(config, encoding="utf-8")
        if sys.platform != "win32":
            os.chmod(config_file, 0o600)

    def _init_sequence_file(self) -> None:
        """Initialize sequence.json if it doesn't exist."""
        if not self.sequence_file.exists():
            init_data = {
                "activeAccountNumber": None,
                "lastUpdated": get_timestamp(),
                "sequence": [],
                "accounts": {},
            }
            self._write_json(self.sequence_file, init_data)

    def _get_sequence_data(self) -> dict | None:
        """Get sequence data."""
        return self._read_json(self.sequence_file)

    def _get_next_account_number(self) -> int:
        """Get next account number."""
        data = self._get_sequence_data()
        if not data or not data.get("accounts"):
            return 1

        account_nums = [int(k) for k in data["accounts"].keys()]
        return max(account_nums, default=0) + 1

    def _get_current_account(self) -> tuple[str, str] | None:
        """Get current account identity (email, organization_uuid) from .claude.json.

        Returns:
            (email, organization_uuid) tuple if found, None otherwise.
            organization_uuid is "" for personal accounts.
        """
        config_path = self._get_claude_config_path()
        if not config_path.exists():
            return None

        data = self._read_json(config_path)
        if not data:
            return None

        oauth = data.get("oauthAccount", {})
        email = oauth.get("emailAddress", "")
        if not email:
            return None

        organization_uuid = oauth.get("organizationUuid", "") or ""
        return (email, organization_uuid)

    def _account_exists(self, email: str, organization_uuid: str) -> bool:
        """Check if account exists by (email, organizationUuid) composite key."""
        data = self._get_sequence_data()
        if not data:
            return False

        for account in data.get("accounts", {}).values():
            if (account.get("email") == email and
                    account.get("organizationUuid", "") == organization_uuid):
                return True
        return False

    @staticmethod
    def _get_display_tag(email: str, org_name: str, org_uuid: str) -> str:
        """Return display tag for an account's org context."""
        return org_name if org_name else "personal"

    def _resolve_account_identifier(self, identifier: str) -> str | None:
        """Resolve account identifier (number or email) to account number.

        Raises:
            ConfigError: if the email matches multiple accounts (ambiguous).
        """
        if identifier.isdigit():
            return identifier

        data = self._get_sequence_data()
        if not data:
            return None

        matches = [
            num for num, account in data.get("accounts", {}).items()
            if account.get("email") == identifier
        ]

        if len(matches) == 0:
            return None
        if len(matches) == 1:
            return matches[0]

        details = ", ".join(
            f"{num} [{data['accounts'][num].get('organizationName') or 'personal'}]"
            for num in matches
        )
        raise ConfigError(
            f"Email '{identifier}' is ambiguous — matches accounts: {details}. "
            f"Use account number instead (e.g., cswap --switch-to 1)."
        )

    def _get_sequence_data_migrated(self) -> dict | None:
        """Get sequence data, ensuring org-field migration has run."""
        data = self._get_sequence_data()
        if not data:
            return data
        needs_migration = any(
            "organizationUuid" not in acc
            for acc in data.get("accounts", {}).values()
        )
        if needs_migration:
            self._migrate_org_fields()
            data = self._get_sequence_data()  # Re-read after migration
        return data

    def _migrate_org_fields(self) -> None:
        """Backfill organizationUuid/Name for accounts added before org support.

        For the currently active account, reads org info from the live config
        (which is authoritative). For inactive accounts, falls back to backup
        configs. Writes updated fields back to sequence.json.
        """
        data = self._get_sequence_data()
        if not data:
            return

        # Read live config for the currently active account
        live_email = ""
        live_org_uuid = ""
        live_org_name = ""
        config_path = self._get_claude_config_path()
        if config_path.exists():
            try:
                config_data = self._read_json(config_path)
                if config_data:
                    oauth = config_data.get("oauthAccount", {})
                    live_email = oauth.get("emailAddress", "")
                    live_org_uuid = oauth.get("organizationUuid", "") or ""
                    live_org_name = oauth.get("organizationName", "") or ""
            except Exception:
                pass

        updated = False
        for num, account in data.get("accounts", {}).items():
            if "organizationUuid" in account:
                continue  # Already migrated

            email = account.get("email", "")

            # For the active account, prefer live config (backup may lack org fields)
            if email == live_email and live_email:
                account["organizationUuid"] = live_org_uuid
                account["organizationName"] = live_org_name
                updated = True
                continue

            # For inactive accounts, fall back to backup config
            config_text = self._read_account_config(num, email)
            if config_text:
                try:
                    config_data = json.loads(config_text)
                    oauth = config_data.get("oauthAccount", {})
                    account["organizationUuid"] = oauth.get("organizationUuid", "") or ""
                    account["organizationName"] = oauth.get("organizationName", "") or ""
                except (json.JSONDecodeError, AttributeError):
                    account["organizationUuid"] = ""
                    account["organizationName"] = ""
            else:
                account["organizationUuid"] = ""
                account["organizationName"] = ""
            updated = True

        if updated:
            data["lastUpdated"] = get_timestamp()
            self._write_json(self.sequence_file, data)

    def add_account(self, slot: int | None = None) -> None:
        """Add current account to managed accounts.

        Args:
            slot: Specify the slot number to store the account in.
                  When None, auto-assigns the next available number.
                  When specified, prompts for confirmation if the slot
                  is already occupied by a different account.
        """
        self._setup_directories()
        self._init_sequence_file()
        self._migrate_org_fields()

        identity = self._get_current_account()
        if identity is None:
            raise ConfigError("No active Claude account found. Please log in first.")
        current_email, current_org_uuid = identity

        # When no slot specified and account already exists, refresh credentials in place
        if slot is None and self._account_exists(current_email, current_org_uuid):
            seq = self._get_sequence_data()
            account_num = next(
                (num for num, acc in seq.get("accounts", {}).items()
                 if acc.get("email") == current_email and
                 acc.get("organizationUuid", "") == current_org_uuid),
                None,
            )
            matched_org_name = seq["accounts"][account_num].get("organizationName", "") if account_num else ""

            current_creds = self._read_credentials()
            if current_creds is None:
                raise CredentialReadError("Failed to read credentials for current account")
            if not current_creds:
                raise CredentialReadError("No credentials found for current account")

            config_path = self._get_claude_config_path()
            try:
                current_config = config_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                raise ConfigError("Claude config file not found")
            except PermissionError:
                raise ConfigError("Permission denied reading Claude config")

            self._write_account_credentials(account_num, current_email, current_creds)
            self._write_account_config(account_num, current_email, current_config)

            seq["activeAccountNumber"] = int(account_num)
            seq["lastUpdated"] = get_timestamp()
            self._write_json(self.sequence_file, seq)

            tag = self._get_display_tag(current_email, matched_org_name, current_org_uuid)
            self._logger.info(f"Updated credentials for account {account_num}: {current_email}")
            print(
                f"{accent('Updated credentials')} for Account {account_num} "
                f"({current_email} {muted(f'[{tag}]')})."
            )
            return

        # Determine slot number and collect confirmation decisions
        # (no destructive operations until new account is verified readable)
        displace_slot = None  # slot to clean up (occupied by different account)
        migrate_from = None   # old slot to clean up (same account, different slot)

        if slot is not None:
            if slot < 1:
                raise ConfigError("Slot number must be >= 1")
            account_num = str(slot)
            data = self._get_sequence_data()

            # Find if current account already exists in a different slot
            if self._account_exists(current_email, current_org_uuid):
                old_num = next(
                    (num for num, acc in data.get("accounts", {}).items()
                     if acc.get("email") == current_email and
                     acc.get("organizationUuid", "") == current_org_uuid),
                    None,
                )
                if old_num and old_num != account_num:
                    migrate_from = old_num

            # Check if target slot is occupied by a different account
            if account_num in data.get("accounts", {}):
                existing = data["accounts"][account_num]
                existing_email = existing.get("email", "unknown")
                is_same = (existing_email == current_email
                           and existing.get("organizationUuid", "") == current_org_uuid)
                if not is_same:
                    existing_tag = self._get_display_tag(
                        existing_email,
                        existing.get("organizationName", ""),
                        existing.get("organizationUuid", ""),
                    )
                    warning(f"Slot {slot} already occupied")
                    print(
                        f"{existing_email} {muted(f'[{existing_tag}]')}"
                    )
                    try:
                        answer = input(f"Overwrite slot {slot}? [y/N] ").strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        print(f"\n{dimmed('Cancelled')}")
                        return
                    if answer not in ("y", "yes"):
                        print(dimmed("Cancelled"))
                        return
                    displace_slot = (account_num, existing_email)
        else:
            account_num = str(self._get_next_account_number())

        # Read new account credentials BEFORE any destructive operations
        current_creds = self._read_credentials()
        if current_creds is None:
            raise CredentialReadError("Failed to read credentials for current account")
        if not current_creds:
            raise CredentialReadError("No credentials found for current account")

        config_path = self._get_claude_config_path()
        try:
            current_config = config_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise ConfigError("Claude config file not found")
        except PermissionError:
            raise ConfigError("Permission denied reading Claude config")

        # Get account UUID and org fields
        config_data = self._read_json(config_path)
        oauth_data = config_data.get("oauthAccount", {})
        account_uuid = oauth_data.get("accountUuid", "")
        organization_uuid = oauth_data.get("organizationUuid", "") or ""
        organization_name = oauth_data.get("organizationName", "") or ""

        # Now safe to perform destructive cleanup (new account data is in memory)
        if displace_slot:
            d_num, d_email = displace_slot
            self._delete_account_files(d_num, d_email)
            data = self._get_sequence_data()
            if int(d_num) in data["sequence"]:
                data["sequence"].remove(int(d_num))
            del data["accounts"][d_num]
            self._write_json(self.sequence_file, data)

        if migrate_from:
            data = self._get_sequence_data()
            old_email = data["accounts"][migrate_from].get("email", "")
            self._delete_account_files(migrate_from, old_email)
            if int(migrate_from) in data["sequence"]:
                data["sequence"].remove(int(migrate_from))
            del data["accounts"][migrate_from]
            self._write_json(self.sequence_file, data)
            print(f"{dimmed(f'Moved from slot {migrate_from} → {slot}')}")

        # Store backups
        self._write_account_credentials(account_num, current_email, current_creds)
        self._write_account_config(account_num, current_email, current_config)

        # Update sequence.json
        data = self._get_sequence_data()
        data["accounts"][account_num] = {
            "email": current_email,
            "uuid": account_uuid,
            "organizationUuid": organization_uuid,
            "organizationName": organization_name,
            "added": get_timestamp(),
        }
        if int(account_num) not in data["sequence"]:
            data["sequence"].append(int(account_num))
            data["sequence"].sort()
        data["activeAccountNumber"] = int(account_num)
        data["lastUpdated"] = get_timestamp()

        self._write_json(self.sequence_file, data)
        tag = self._get_display_tag(current_email, organization_name, organization_uuid)
        self._logger.info(f"Added account {account_num}: {current_email} (org: {organization_uuid or 'personal'})")
        print(f"{accent('Added')} Account {account_num}: {current_email} {muted(f'[{tag}]')}")

    def remove_account(self, identifier: str) -> None:
        """Remove account from managed accounts."""
        if not self.sequence_file.exists():
            raise ConfigError("No accounts are managed yet")

        # Ensure org fields are migrated before resolving accounts
        self._get_sequence_data_migrated()

        # Resolve identifier
        if not identifier.isdigit():
            if not self._validate_email(identifier):
                raise ValidationError(f"Invalid email format: {identifier}")

            # For email identifiers, handle ambiguous matches interactively
            data = self._get_sequence_data()
            matches = [
                num for num, acc in (data or {}).get("accounts", {}).items()
                if acc.get("email") == identifier
            ]
            if len(matches) > 1:
                print(f"Multiple accounts found for '{identifier}':")
                for num in matches:
                    acc = data["accounts"][num]
                    tag = self._get_display_tag(
                        acc.get("email", ""),
                        acc.get("organizationName", ""),
                        acc.get("organizationUuid", ""),
                    )
                    print(f"  {num}: {identifier} {muted(f'[{tag}]')}")
                choice = input("Enter account number to remove: ").strip()
                if not choice.isdigit() or choice not in matches:
                    print(dimmed("Cancelled"))
                    return
                identifier = choice

        account_num = self._resolve_account_identifier(identifier)
        if not account_num:
            raise AccountNotFoundError(
                f"No account found with identifier: {identifier}"
            )

        data = self._get_sequence_data()
        account_info = data.get("accounts", {}).get(account_num)

        if not account_info:
            raise AccountNotFoundError(f"Account-{account_num} does not exist")

        email = account_info.get("email")
        active_account = data.get("activeAccountNumber")

        if str(active_account) == account_num:
            warning(f"Warning: Account-{account_num} ({email}) is currently active")

        confirm = input(
            f"Are you sure you want to permanently remove "
            f"Account-{account_num} ({email})? [y/N] "
        )
        if confirm.lower() != "y":
            print(dimmed("Cancelled"))
            return

        # Remove backup files
        self._delete_account_files(account_num, email)

        # Update sequence.json
        del data["accounts"][account_num]
        data["sequence"] = [n for n in data["sequence"] if n != int(account_num)]
        data["lastUpdated"] = get_timestamp()

        self._write_json(self.sequence_file, data)
        self._logger.info(f"Removed account {account_num}: {email}")
        print(f"{accent('Removed')} Account-{account_num} ({email})")

    def list_accounts(
        self,
        show_token_status: bool = False,
    ) -> None:
        """List all managed accounts."""
        if not self.sequence_file.exists():
            print(dimmed("No accounts are managed yet."))
            self._first_run_setup()
            return

        data = self._get_sequence_data_migrated()
        current_identity = self._get_current_account()

        # Find active account number by (email, organizationUuid) composite key
        active_num = None
        if current_identity is not None:
            current_email, current_org_uuid = current_identity
            for num, account in data.get("accounts", {}).items():
                if (account.get("email") == current_email and
                        account.get("organizationUuid", "") == current_org_uuid):
                    active_num = num
                    break

        accounts_info = []
        for num in data.get("sequence", []):
            account = data.get("accounts", {}).get(str(num), {})
            email = account.get("email", "unknown")
            org_name = account.get("organizationName", "") or ""
            org_uuid = account.get("organizationUuid", "") or ""
            is_active = str(num) == active_num

            if is_active:
                creds = self._read_credentials() or ""
            else:
                creds = self._read_account_credentials(str(num), email)

            accounts_info.append((num, email, org_name, org_uuid, is_active, creds))

        def fetch(
            account_info: tuple[int, str, str, str, bool, str]
        ) -> dict | str | None:
            num, email, _, _, is_active, creds = account_info
            if not creds or not oauth.extract_access_token(creds):
                return "no credentials"

            def persist(acct_num: str, acct_email: str, new_creds: str) -> None:
                with FileLock(self.lock_file):
                    self._write_account_credentials(acct_num, acct_email, new_creds)

            return oauth.fetch_usage_for_account(
                str(num), email, creds,
                is_active=is_active,
                persist_credentials=persist,
            )

        usage_cache_path = self.backup_dir / "cache" / "usage.json"
        cached = read_cache(usage_cache_path, _USAGE_CACHE_TTL)
        account_keys = {str(info[0]) for info in accounts_info}
        if cached is not MISSING and isinstance(cached, dict) and cached.keys() == account_keys:
            usages = [cached.get(str(info[0])) for info in accounts_info]
        else:
            with ThreadPoolExecutor() as executor:
                usages = list(executor.map(fetch, accounts_info))
            write_cache(usage_cache_path, {
                str(info[0]): usage
                for info, usage in zip(accounts_info, usages)
            })

        print(bolded("Accounts:"))
        for i, ((num, email, org_name, org_uuid, is_active, _), usage) in enumerate(zip(accounts_info, usages)):
            tag = self._get_display_tag(email, org_name, org_uuid)
            if is_active:
                marker = f" {bold_accent('(active)')}"
                print(f"  {num}: {email} {muted(f'[{tag}]')}{marker}")
            else:
                print(f"  {num}: {email} {muted(f'[{tag}]')}")
            if isinstance(usage, str):
                print(f"     {dimmed(usage)}")
            elif usage is None:
                print(f"     {dimmed('usage unavailable')}")
            else:
                h5 = usage.get("five_hour")
                d7 = usage.get("seven_day")
                lines = []
                if h5:
                    if "clock" in h5:
                        lines.append(f"5h: {h5['pct']:>3.0f}%   resets {h5['clock']:<12}  in {h5['countdown']}")
                    else:
                        lines.append(f"5h: {h5['pct']:>3.0f}%")
                if d7:
                    if "clock" in d7:
                        lines.append(f"7d: {d7['pct']:>3.0f}%   resets {d7['clock']:<12}  in {d7['countdown']}")
                    else:
                        lines.append(f"7d: {d7['pct']:>3.0f}%")
                for j, line in enumerate(lines):
                    connector = "└" if j == len(lines) - 1 else "├"
                    print(f"     {dimmed(connector)} {muted(line)}")

            if show_token_status:
                token_status = oauth.build_token_status(accounts_info[i][5])
                if token_status:
                    print(f"     {dimmed('•')} {muted(token_status)}")
            if i < len(accounts_info) - 1:
                print()

        # Running instances
        try:
            sessions, ide_instances = get_running_instances()

            if sessions or ide_instances:
                # Group by (label, folder) to avoid repetitive lines
                groups: dict[tuple[str, str], dict[str, int]] = {}
                for session in sessions:
                    label = entrypoint_label(session.entrypoint)
                    cwd = abbreviate_path(session.cwd)
                    key = (label, cwd)
                    counts = groups.setdefault(key, {"sessions": 0, "ide": 0})
                    counts["sessions"] += 1
                for ide in ide_instances:
                    name = ide_short_name(ide.ide_name)
                    for folder in ide.workspace_folders:
                        key = (name, abbreviate_path(folder))
                        counts = groups.setdefault(key, {"sessions": 0, "ide": 0})
                        counts["ide"] += 1

                print()
                print(bolded("Running instances:"))
                for (label, cwd), counts in groups.items():
                    parts = []
                    s = counts["sessions"]
                    if s:
                        parts.append(f"{s} session{'s' if s > 1 else ''}")
                    if counts["ide"]:
                        parts.append("IDE")
                    print(f"  {dimmed('●')} {muted(label)}   {muted(cwd)}  {dimmed(f'({", ".join(parts)})')}")
        except Exception:
            self._logger.debug("Failed to detect running instances", exc_info=True)

    def status(self) -> None:
        """Display current account status."""
        identity = self._get_current_account()
        if identity is None:
            print(f"{bolded('Status:')} {dimmed('No active Claude account')}")
            return
        current_email, current_org_uuid = identity

        data = self._get_sequence_data_migrated()
        if not data:
            print(f"{bolded('Status:')} {current_email} {dimmed('(not managed)')}")
            return

        account_num = None
        org_name = ""
        for num, info in data.get("accounts", {}).items():
            if (info.get("email") == current_email and
                    info.get("organizationUuid", "") == current_org_uuid):
                account_num = num
                org_name = info.get("organizationName", "") or ""
                break

        if account_num:
            tag = self._get_display_tag(current_email, org_name, current_org_uuid)
            total = len(data.get("accounts", {}))
            print(
                f"{bolded('Status:')} {accent(f'Account-{account_num}')} "
                f"({current_email} {muted(f'[{tag}]')})"
            )
            print(f"  {dimmed(f'Total managed accounts: {total}')}")
        else:
            print(f"{bolded('Status:')} {current_email} {dimmed('(not managed)')}")

    def _first_run_setup(self) -> None:
        """First-run setup workflow."""
        identity = self._get_current_account()

        if identity is None:
            print(dimmed("No active Claude account found. Please log in first."))
            return
        current_email, _ = identity

        response = input(
            f"No managed accounts found. Add current account "
            f"({current_email}) to managed list? [Y/n] "
        )
        if response.lower() == "n":
            print(dimmed("Setup cancelled. You can run 'cswap --add-account' later."))
            return

        self.add_account()

    def switch(self) -> None:
        """Switch to next account in sequence."""
        if not self.sequence_file.exists():
            raise ConfigError("No accounts are managed yet")

        identity = self._get_current_account()

        # Ensure org fields are migrated before checking composite key
        self._get_sequence_data_migrated()

        # Fresh-machine path: no live Claude session, but we have managed accounts
        # (e.g. right after cswap --import). Activate the recorded
        # activeAccountNumber, or fall back to the first slot in sequence.
        if identity is None:
            data = self._get_sequence_data()
            target = data.get("activeAccountNumber") if data else None
            if not target:
                sequence = (data or {}).get("sequence", [])
                target = sequence[0] if sequence else None
            if not target:
                raise ConfigError("No accounts are managed yet")
            self._perform_switch(str(target))
            return

        current_email, current_org_uuid = identity

        # Check if current account is managed
        if not self._account_exists(current_email, current_org_uuid):
            print(f"{accent('Notice:')} Active account '{current_email}' was not managed.")
            self.add_account()
            data = self._get_sequence_data()
            account_num = data.get("activeAccountNumber")
            print(f"It has been automatically added as Account-{account_num}.")
            print(dimmed("Please run the switch command again to switch to the next account."))
            return

        data = self._get_sequence_data()
        sequence = data.get("sequence", [])

        if len(sequence) < 2:
            print(dimmed("Only one account is managed. Add more accounts to switch between."))
            return

        active_account = data.get("activeAccountNumber")

        # Find current index and get next
        try:
            current_index = sequence.index(active_account)
        except ValueError:
            current_index = 0

        next_index = (current_index + 1) % len(sequence)
        next_account = str(sequence[next_index])

        self._perform_switch(next_account)

    def switch_to(self, identifier: str) -> None:
        """Switch to specific account."""
        if not self.sequence_file.exists():
            raise ConfigError("No accounts are managed yet")

        # Ensure org fields are migrated before resolving accounts
        self._get_sequence_data_migrated()

        # Resolve identifier
        if not identifier.isdigit():
            if not self._validate_email(identifier):
                raise ValidationError(f"Invalid email format: {identifier}")

            # For email identifiers, handle ambiguous matches interactively
            data = self._get_sequence_data()
            matches = [
                num for num, acc in (data or {}).get("accounts", {}).items()
                if acc.get("email") == identifier
            ]
            if len(matches) > 1:
                print(f"Multiple accounts found for '{identifier}':")
                for num in matches:
                    acc = data["accounts"][num]
                    tag = self._get_display_tag(
                        acc.get("email", ""),
                        acc.get("organizationName", ""),
                        acc.get("organizationUuid", ""),
                    )
                    print(f"  {num}: {identifier} {muted(f'[{tag}]')}")
                choice = input("Enter account number to switch to: ").strip()
                if not choice.isdigit() or choice not in matches:
                    print(dimmed("Cancelled"))
                    return
                identifier = choice

        target_account = self._resolve_account_identifier(identifier)
        if not target_account:
            raise AccountNotFoundError(
                f"No account found with identifier: {identifier}"
            )

        data = self._get_sequence_data()
        if target_account not in data.get("accounts", {}):
            raise AccountNotFoundError(f"Account-{target_account} does not exist")

        self._perform_switch(target_account)

    def _perform_switch(self, target_account: str) -> None:
        """Perform the actual account switch with transaction support.

        The post-switch display runs after the lock releases so that persist
        callbacks inside list_accounts() can re-acquire it.
        """
        with FileLock(self.lock_file):
            data = self._get_sequence_data()
            current_account = str(data.get("activeAccountNumber"))
            target_email = data["accounts"][target_account]["email"]
            current_identity = self._get_current_account()

            config_path = self._get_claude_config_path()

            # Fresh-machine path: no live Claude session yet (e.g. right after
            # cswap --import on a new machine). Skip the back-up-current step
            # since there's nothing to back up, and install the target directly.
            if current_identity is None:
                target_creds = self._read_account_credentials(
                    target_account, target_email
                )
                target_config = self._read_account_config(target_account, target_email)
                if not target_creds or not target_config:
                    raise SwitchError(
                        f"Missing backup data for Account-{target_account}"
                    )
                try:
                    target_config_data = json.loads(target_config)
                except json.JSONDecodeError as exc:
                    raise SwitchError(f"Invalid backup config: {exc}")
                target_oauth = target_config_data.get("oauthAccount")
                if not target_oauth:
                    raise SwitchError("Invalid oauthAccount in backup")

                self._write_credentials(target_creds)

                # Mirror the normal switch path: preserve existing local
                # settings/projects when ~/.claude.json already exists, only
                # swapping in oauthAccount. Fall back to the full imported
                # config when no usable local config exists.
                existing_config = self._read_json(config_path) if config_path.exists() else None
                if existing_config:
                    existing_config["oauthAccount"] = target_oauth
                    self._write_json(config_path, existing_config)
                else:
                    self._write_json(config_path, target_config_data)

                data["activeAccountNumber"] = int(target_account)
                data["lastUpdated"] = get_timestamp()
                self._write_json(self.sequence_file, data)
                self._logger.info(
                    f"Activated account {target_account} (no prior live account)"
                )
                print(
                    f"{accent('Activated')} Account-{target_account} ({target_email})"
                )
                print()
                warning("Please restart Claude Code to use the new authentication.")
                print()
                return

            current_email, _ = current_identity

            # Create transaction for rollback capability
            try:
                original_creds = self._read_credentials()
                if original_creds is None:
                    raise CredentialReadError("Failed to read current credentials")
                original_config = config_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                raise ConfigError("Claude config file not found")
            except PermissionError:
                raise ConfigError("Permission denied reading Claude config")

            transaction = SwitchTransaction(
                original_credentials=original_creds,
                original_config=original_config,
                original_account_num=current_account,
                original_email=current_email,
                config_path=config_path,
            )

            try:
                # Step 1: Backup current account
                self._write_account_credentials(
                    current_account, current_email, original_creds
                )
                self._write_account_config(
                    current_account, current_email, original_config
                )
                self._logger.info(f"Backed up account {current_account}")

                # Step 2: Retrieve target account
                target_creds = self._read_account_credentials(
                    target_account, target_email
                )
                target_config = self._read_account_config(target_account, target_email)

                if not target_creds or not target_config:
                    raise SwitchError(
                        f"Missing backup data for Account-{target_account}"
                    )

                # Step 3: Activate target account - credentials
                self._write_credentials(target_creds)
                transaction.record_step("credentials_written")
                self._logger.info("Wrote target credentials")

                # Step 4: Update config with target oauthAccount
                target_config_data = json.loads(target_config)
                oauth_section = target_config_data.get("oauthAccount")

                if not oauth_section:
                    raise SwitchError("Invalid oauthAccount in backup")

                current_config_data = self._read_json(config_path)
                current_config_data["oauthAccount"] = oauth_section

                self._write_json(config_path, current_config_data)
                transaction.record_step("config_written")
                self._logger.info("Updated config file")

                # Step 5: Update sequence state
                data["activeAccountNumber"] = int(target_account)
                data["lastUpdated"] = get_timestamp()
                self._write_json(self.sequence_file, data)
                transaction.record_step("sequence_updated")

                self._logger.info(
                    f"Switched from account {current_account} to {target_account}"
                )

            except Exception as e:
                self._logger.error(f"Switch failed: {e}, attempting rollback")
                if transaction.completed_steps:
                    success = transaction.rollback(self)
                    if success:
                        self._logger.info("Rollback successful")
                        raise SwitchError(
                            f"Switch failed and was rolled back: {e}"
                        )
                    else:
                        self._logger.error("Rollback failed!")
                        raise SwitchError(
                            f"Switch failed and rollback also failed: {e}. "
                            f"Manual recovery may be needed."
                        )
                raise

        # Lock released. Safe to do network I/O and let persist callbacks
        # re-acquire the lock from inside list_accounts().
        print(f"{accent('Switched to')} Account-{target_account} ({target_email})")
        try:
            self.list_accounts()
        except Exception as e:
            self._logger.warning(f"Post-switch usage display failed: {e!r}")
            print(dimmed("  (usage display unavailable — run `cswap --list` to retry)"))
        print()
        warning("Please restart Claude Code to use the new authentication.")
        print()

    def purge(self) -> None:
        """Remove all traces of claude-swap from the system.

        This removes:
        - All stored account credentials (files on Linux, keyring on macOS/Windows)
        - The ~/.claude-swap-backup directory and all its contents
        """
        warning("This will remove ALL claude-swap data from your system:")
        print(f"  - Backup directory: {self.backup_dir}")
        if self.platform in (Platform.LINUX, Platform.WSL):
            print("  - All stored account credential files")
        else:
            print("  - All stored account credentials from the system keyring")
        print()
        print(dimmed("Note: This does NOT affect your current Claude Code login."))
        print()

        confirm = input("Are you sure you want to purge all data? [y/N] ")
        if confirm.lower() != "y":
            print(dimmed("Cancelled"))
            return

        removed_items = []

        # Remove credentials
        data = self._get_sequence_data()
        if data:
            for account_num, account_info in data.get("accounts", {}).items():
                email = account_info.get("email", "")
                if self.platform in (Platform.LINUX, Platform.WSL):
                    # Remove credential files on Linux
                    cred_file = (
                        self.credentials_dir / f".creds-{account_num}-{email}.enc"
                    )
                    try:
                        if cred_file.exists():
                            cred_file.unlink()
                            removed_items.append(f"Credential file: {cred_file.name}")
                    except Exception:
                        pass  # Ignore errors during purge
                else:
                    # Remove from keyring on macOS/Windows
                    username = f"account-{account_num}-{email}"
                    try:
                        keyring.delete_password(KEYRING_SERVICE, username)
                        removed_items.append(f"Credential: {username}")
                    except keyring.errors.PasswordDeleteError:
                        pass  # Credential doesn't exist
                    except Exception:
                        pass  # Ignore other errors during purge

        # Remove backup directory
        if self.backup_dir.exists():
            # Close log handlers before deleting (required on Windows)
            for handler in self._logger.handlers[:]:
                handler.close()
                self._logger.removeHandler(handler)

            shutil.rmtree(self.backup_dir)
            removed_items.append(f"Directory: {self.backup_dir}")

        if removed_items:
            print(f"\n{accent('Removed:')}")
            for item in removed_items:
                print(f"  {dimmed('-')} {item}")
        else:
            print(f"\n{dimmed('No claude-swap data found to remove.')}")

        print(f"\n{accent('Purge complete.')}")
