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
from claude_swap.locking import FileLock
from claude_swap.logging_config import setup_logging
from claude_swap.models import Platform, SwitchTransaction, get_timestamp

# Service name for keyring storage
KEYRING_SERVICE = "claude-code"
KEYRING_ACTIVE_USERNAME = "active-credentials"


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
        """Get Claude configuration file path with fallback."""
        primary_config = self.home / ".claude" / ".claude.json"
        fallback_config = self.home / ".claude.json"

        if primary_config.exists():
            try:
                data = json.loads(primary_config.read_text())
                if "oauthAccount" in data:
                    return primary_config
            except (json.JSONDecodeError, KeyError):
                pass

        return fallback_config

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
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            self._logger.warning(f"Invalid JSON in {path}")
            return None

    def _write_json(self, path: Path, data: dict) -> None:
        """Write JSON file with validation."""
        content = json.dumps(data, indent=2)

        # Write to temp file first
        temp_path = path.with_suffix(f".{os.getpid()}.tmp")
        temp_path.write_text(content)

        # Validate written content
        try:
            json.loads(temp_path.read_text())
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
            cred_file = self.home / ".claude" / ".credentials.json"
            if cred_file.exists():
                try:
                    return cred_file.read_text()
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
            cred_dir = self.home / ".claude"
            cred_dir.mkdir(parents=True, exist_ok=True)
            cred_file = cred_dir / ".credentials.json"
            try:
                cred_file.write_text(credentials)
                if sys.platform != "win32":
                    os.chmod(cred_file, 0o600)
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
                    encoded = cred_file.read_text()
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
                cred_file.write_text(encoded)
                os.chmod(cred_file, 0o600)
            except Exception as e:
                self._logger.warning(f"Failed to write credentials file: {e}")
        else:
            # Use keyring for macOS/Windows
            username = f"account-{account_num}-{email}"
            try:
                keyring.set_password(KEYRING_SERVICE, username, credentials)
            except Exception as e:
                self._logger.warning(f"Failed to write credentials to keyring: {e}")

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

    def _read_account_config(self, account_num: str, email: str) -> str:
        """Read account config from backup."""
        config_file = self.configs_dir / f".claude-config-{account_num}-{email}.json"
        if config_file.exists():
            return config_file.read_text()
        return ""

    def _write_account_config(
        self, account_num: str, email: str, config: str
    ) -> None:
        """Write account config to backup."""
        config_file = self.configs_dir / f".claude-config-{account_num}-{email}.json"
        config_file.write_text(config)
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

    def _get_current_account(self) -> str | None:
        """Get current account email from .claude.json.

        Returns:
            Email address if found, None otherwise.
        """
        config_path = self._get_claude_config_path()
        if not config_path.exists():
            return None

        data = self._read_json(config_path)
        if not data:
            return None

        email = data.get("oauthAccount", {}).get("emailAddress", "")
        return email if email else None

    def _account_exists(self, email: str) -> bool:
        """Check if account exists by email."""
        data = self._get_sequence_data()
        if not data:
            return False

        for account in data.get("accounts", {}).values():
            if account.get("email") == email:
                return True
        return False

    def _resolve_account_identifier(self, identifier: str) -> str | None:
        """Resolve account identifier (number or email) to account number."""
        if identifier.isdigit():
            return identifier

        data = self._get_sequence_data()
        if not data:
            return None

        for num, account in data.get("accounts", {}).items():
            if account.get("email") == identifier:
                return num
        return None

    def add_account(self) -> None:
        """Add current account to managed accounts."""
        self._setup_directories()
        self._init_sequence_file()

        current_email = self._get_current_account()
        if current_email is None:
            raise ConfigError("No active Claude account found. Please log in first.")

        if self._account_exists(current_email):
            print(f"Account {current_email} is already managed.")
            return

        account_num = str(self._get_next_account_number())

        # Backup current credentials and config
        current_creds = self._read_credentials()
        if current_creds is None:
            raise CredentialReadError("Failed to read credentials for current account")
        if not current_creds:
            raise CredentialReadError("No credentials found for current account")

        config_path = self._get_claude_config_path()
        try:
            current_config = config_path.read_text()
        except FileNotFoundError:
            raise ConfigError("Claude config file not found")
        except PermissionError:
            raise ConfigError("Permission denied reading Claude config")

        # Get account UUID
        config_data = self._read_json(config_path)
        account_uuid = config_data.get("oauthAccount", {}).get("accountUuid", "")

        # Store backups
        self._write_account_credentials(account_num, current_email, current_creds)
        self._write_account_config(account_num, current_email, current_config)

        # Update sequence.json
        data = self._get_sequence_data()
        data["accounts"][account_num] = {
            "email": current_email,
            "uuid": account_uuid,
            "added": get_timestamp(),
        }
        data["sequence"].append(int(account_num))
        data["activeAccountNumber"] = int(account_num)
        data["lastUpdated"] = get_timestamp()

        self._write_json(self.sequence_file, data)
        self._logger.info(f"Added account {account_num}: {current_email}")
        print(f"Added Account {account_num}: {current_email}")

    def remove_account(self, identifier: str) -> None:
        """Remove account from managed accounts."""
        if not self.sequence_file.exists():
            raise ConfigError("No accounts are managed yet")

        # Resolve identifier
        if not identifier.isdigit():
            if not self._validate_email(identifier):
                raise ValidationError(f"Invalid email format: {identifier}")

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
            print(f"Warning: Account-{account_num} ({email}) is currently active")

        confirm = input(
            f"Are you sure you want to permanently remove "
            f"Account-{account_num} ({email})? [y/N] "
        )
        if confirm.lower() != "y":
            print("Cancelled")
            return

        # Remove backup files
        self._delete_account_credentials(account_num, email)
        config_file = self.configs_dir / f".claude-config-{account_num}-{email}.json"
        if config_file.exists():
            config_file.unlink()

        # Update sequence.json
        del data["accounts"][account_num]
        data["sequence"] = [n for n in data["sequence"] if n != int(account_num)]
        data["lastUpdated"] = get_timestamp()

        self._write_json(self.sequence_file, data)
        self._logger.info(f"Removed account {account_num}: {email}")
        print(f"Account-{account_num} ({email}) has been removed")

    def list_accounts(self) -> None:
        """List all managed accounts."""
        if not self.sequence_file.exists():
            print("No accounts are managed yet.")
            self._first_run_setup()
            return

        data = self._get_sequence_data()
        current_email = self._get_current_account()

        # Find active account number by email
        active_num = None
        if current_email is not None:
            for num, account in data.get("accounts", {}).items():
                if account.get("email") == current_email:
                    active_num = num
                    break

        print("Accounts:")
        for num in data.get("sequence", []):
            account = data.get("accounts", {}).get(str(num), {})
            email = account.get("email", "unknown")
            if str(num) == active_num:
                print(f"  {num}: {email} (active)")
            else:
                print(f"  {num}: {email}")

    def status(self) -> None:
        """Display current account status."""
        current = self._get_current_account()
        if current is None:
            print("Status: No active Claude account")
            return

        data = self._get_sequence_data()
        if not data:
            print(f"Status: Active account: {current} (not managed)")
            return

        account_num = None
        for num, info in data.get("accounts", {}).items():
            if info.get("email") == current:
                account_num = num
                break

        if account_num:
            total = len(data.get("accounts", {}))
            print(f"Status: Account-{account_num} ({current})")
            print(f"  Total managed accounts: {total}")
        else:
            print(f"Status: Active account: {current} (not managed)")

    def _first_run_setup(self) -> None:
        """First-run setup workflow."""
        current_email = self._get_current_account()

        if current_email is None:
            print("No active Claude account found. Please log in first.")
            return

        response = input(
            f"No managed accounts found. Add current account "
            f"({current_email}) to managed list? [Y/n] "
        )
        if response.lower() == "n":
            print("Setup cancelled. You can run 'cswap --add-account' later.")
            return

        self.add_account()

    def switch(self) -> None:
        """Switch to next account in sequence."""
        if not self.sequence_file.exists():
            raise ConfigError("No accounts are managed yet")

        current_email = self._get_current_account()
        if current_email is None:
            raise ConfigError("No active Claude account found")

        # Check if current account is managed
        if not self._account_exists(current_email):
            print(f"Notice: Active account '{current_email}' was not managed.")
            self.add_account()
            data = self._get_sequence_data()
            account_num = data.get("activeAccountNumber")
            print(f"It has been automatically added as Account-{account_num}.")
            print("Please run the switch command again to switch to the next account.")
            return

        data = self._get_sequence_data()
        sequence = data.get("sequence", [])

        if len(sequence) < 2:
            print("Only one account is managed. Add more accounts to switch between.")
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

        # Resolve identifier
        if not identifier.isdigit():
            if not self._validate_email(identifier):
                raise ValidationError(f"Invalid email format: {identifier}")

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
        """Perform the actual account switch with transaction support."""
        with FileLock(self.lock_file):
            data = self._get_sequence_data()
            current_account = str(data.get("activeAccountNumber"))
            target_email = data["accounts"][target_account]["email"]
            current_email = self._get_current_account()

            if current_email is None:
                raise SwitchError("No current account to switch from")

            config_path = self._get_claude_config_path()

            # Create transaction for rollback capability
            try:
                original_creds = self._read_credentials()
                if original_creds is None:
                    raise CredentialReadError("Failed to read current credentials")
                original_config = config_path.read_text()
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
                print(f"Switched to Account-{target_account} ({target_email})")
                self.list_accounts()
                print()
                print("Please restart Claude Code to use the new authentication.")
                print()

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

    def purge(self) -> None:
        """Remove all traces of claude-swap from the system.

        This removes:
        - All stored account credentials (files on Linux, keyring on macOS/Windows)
        - The ~/.claude-swap-backup directory and all its contents
        """
        print("This will remove ALL claude-swap data from your system:")
        print(f"  - Backup directory: {self.backup_dir}")
        if self.platform in (Platform.LINUX, Platform.WSL):
            print("  - All stored account credential files")
        else:
            print("  - All stored account credentials from the system keyring")
        print()
        print("Note: This does NOT affect your current Claude Code login.")
        print()

        confirm = input("Are you sure you want to purge all data? [y/N] ")
        if confirm.lower() != "y":
            print("Cancelled")
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
            print("\nRemoved:")
            for item in removed_items:
                print(f"  - {item}")
        else:
            print("\nNo claude-swap data found to remove.")

        print("\nPurge complete.")
