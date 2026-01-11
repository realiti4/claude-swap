#!/usr/bin/env python3
"""
Multi-Account Switcher for Claude Code
Python implementation for managing multiple Claude Code accounts
"""

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class ClaudeAccountSwitcher:
    def __init__(self):
        self.home = Path.home()
        self.backup_dir = self.home / ".claude-switch-backup"
        self.sequence_file = self.backup_dir / "sequence.json"
        self.configs_dir = self.backup_dir / "configs"
        self.credentials_dir = self.backup_dir / "credentials"
        self.platform = self._detect_platform()

    def _detect_platform(self) -> str:
        """Detect the current platform."""
        system = platform.system()
        if system == "Darwin":
            return "macos"
        elif system == "Linux":
            if os.environ.get("WSL_DISTRO_NAME"):
                return "wsl"
            return "linux"
        return "unknown"

    def _is_running_in_container(self) -> bool:
        """Check if running inside a container."""
        # Check for Docker environment file
        if Path("/.dockerenv").exists():
            return True

        # Check cgroup for container indicators
        cgroup_path = Path("/proc/1/cgroup")
        if cgroup_path.exists():
            try:
                content = cgroup_path.read_text()
                if any(x in content for x in ["docker", "lxc", "containerd", "kubepods"]):
                    return True
            except PermissionError:
                pass

        # Check mount info
        mountinfo_path = Path("/proc/self/mountinfo")
        if mountinfo_path.exists():
            try:
                content = mountinfo_path.read_text()
                if any(x in content for x in ["docker", "overlay"]):
                    return True
            except PermissionError:
                pass

        # Check environment variables
        if os.environ.get("CONTAINER") or os.environ.get("container"):
            return True

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

    def _get_timestamp(self) -> str:
        """Get current UTC timestamp in ISO format."""
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _setup_directories(self):
        """Create backup directories with proper permissions."""
        for directory in [self.backup_dir, self.configs_dir, self.credentials_dir]:
            directory.mkdir(parents=True, exist_ok=True)
            os.chmod(directory, 0o700)

    def _read_json(self, path: Path) -> Optional[dict]:
        """Read and parse JSON file."""
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return None

    def _write_json(self, path: Path, data: dict):
        """Write JSON file with validation."""
        # Validate JSON can be serialized
        content = json.dumps(data, indent=2)
        
        # Write to temp file first
        temp_path = path.with_suffix(f".{os.getpid()}.tmp")
        temp_path.write_text(content)
        
        # Validate written content
        try:
            json.loads(temp_path.read_text())
        except json.JSONDecodeError:
            temp_path.unlink()
            raise ValueError("Generated invalid JSON")

        # Move to final location
        shutil.move(str(temp_path), str(path))
        os.chmod(path, 0o600)

    def _read_credentials(self) -> str:
        """Read credentials based on platform."""
        if self.platform == "macos":
            try:
                result = subprocess.run(
                    ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                    capture_output=True,
                    text=True,
                    check=True
                )
                return result.stdout.strip()
            except subprocess.CalledProcessError:
                return ""
        else:  # linux/wsl
            cred_file = self.home / ".claude" / ".credentials.json"
            if cred_file.exists():
                return cred_file.read_text()
            return ""

    def _write_credentials(self, credentials: str):
        """Write credentials based on platform."""
        if self.platform == "macos":
            subprocess.run(
                ["security", "add-generic-password", "-U", "-s", "Claude Code-credentials",
                 "-a", os.environ.get("USER", "user"), "-w", credentials],
                capture_output=True
            )
        else:  # linux/wsl
            cred_dir = self.home / ".claude"
            cred_dir.mkdir(parents=True, exist_ok=True)
            cred_file = cred_dir / ".credentials.json"
            cred_file.write_text(credentials)
            os.chmod(cred_file, 0o600)

    def _read_account_credentials(self, account_num: str, email: str) -> str:
        """Read account credentials from backup."""
        if self.platform == "macos":
            try:
                result = subprocess.run(
                    ["security", "find-generic-password",
                     "-s", f"Claude Code-Account-{account_num}-{email}", "-w"],
                    capture_output=True,
                    text=True,
                    check=True
                )
                return result.stdout.strip()
            except subprocess.CalledProcessError:
                return ""
        else:
            cred_file = self.credentials_dir / f".claude-credentials-{account_num}-{email}.json"
            if cred_file.exists():
                return cred_file.read_text()
            return ""

    def _write_account_credentials(self, account_num: str, email: str, credentials: str):
        """Write account credentials to backup."""
        if self.platform == "macos":
            subprocess.run(
                ["security", "add-generic-password", "-U",
                 "-s", f"Claude Code-Account-{account_num}-{email}",
                 "-a", os.environ.get("USER", "user"), "-w", credentials],
                capture_output=True
            )
        else:
            cred_file = self.credentials_dir / f".claude-credentials-{account_num}-{email}.json"
            cred_file.write_text(credentials)
            os.chmod(cred_file, 0o600)

    def _delete_account_credentials(self, account_num: str, email: str):
        """Delete account credentials from backup."""
        if self.platform == "macos":
            subprocess.run(
                ["security", "delete-generic-password",
                 "-s", f"Claude Code-Account-{account_num}-{email}"],
                capture_output=True
            )
        else:
            cred_file = self.credentials_dir / f".claude-credentials-{account_num}-{email}.json"
            if cred_file.exists():
                cred_file.unlink()

    def _read_account_config(self, account_num: str, email: str) -> str:
        """Read account config from backup."""
        config_file = self.configs_dir / f".claude-config-{account_num}-{email}.json"
        if config_file.exists():
            return config_file.read_text()
        return ""

    def _write_account_config(self, account_num: str, email: str, config: str):
        """Write account config to backup."""
        config_file = self.configs_dir / f".claude-config-{account_num}-{email}.json"
        config_file.write_text(config)
        os.chmod(config_file, 0o600)

    def _init_sequence_file(self):
        """Initialize sequence.json if it doesn't exist."""
        if not self.sequence_file.exists():
            init_data = {
                "activeAccountNumber": None,
                "lastUpdated": self._get_timestamp(),
                "sequence": [],
                "accounts": {}
            }
            self._write_json(self.sequence_file, init_data)

    def _get_sequence_data(self) -> Optional[dict]:
        """Get sequence data."""
        return self._read_json(self.sequence_file)

    def _get_next_account_number(self) -> int:
        """Get next account number."""
        data = self._get_sequence_data()
        if not data or not data.get("accounts"):
            return 1
        
        account_nums = [int(k) for k in data["accounts"].keys()]
        return max(account_nums, default=0) + 1

    def _get_current_account(self) -> str:
        """Get current account email from .claude.json."""
        config_path = self._get_claude_config_path()
        if not config_path.exists():
            return "none"
        
        data = self._read_json(config_path)
        if not data:
            return "none"

        email = data.get("oauthAccount", {}).get("emailAddress", "")
        return email if email else "none"

    def _account_exists(self, email: str) -> bool:
        """Check if account exists by email."""
        data = self._get_sequence_data()
        if not data:
            return False
        
        for account in data.get("accounts", {}).values():
            if account.get("email") == email:
                return True
        return False

    def _resolve_account_identifier(self, identifier: str) -> Optional[str]:
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

    def add_account(self):
        """Add current account to managed accounts."""
        self._setup_directories()
        self._init_sequence_file()

        current_email = self._get_current_account()
        if current_email == "none":
            print("Error: No active Claude account found. Please log in first.")
            sys.exit(1)

        if self._account_exists(current_email):
            print(f"Account {current_email} is already managed.")
            sys.exit(0)

        account_num = str(self._get_next_account_number())

        # Backup current credentials and config
        current_creds = self._read_credentials()
        config_path = self._get_claude_config_path()
        current_config = config_path.read_text()

        if not current_creds:
            print("Error: No credentials found for current account")
            sys.exit(1)

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
            "added": self._get_timestamp()
        }
        data["sequence"].append(int(account_num))
        data["activeAccountNumber"] = int(account_num)
        data["lastUpdated"] = self._get_timestamp()
        
        self._write_json(self.sequence_file, data)
        print(f"Added Account {account_num}: {current_email}")

    def remove_account(self, identifier: str):
        """Remove account from managed accounts."""
        if not self.sequence_file.exists():
            print("Error: No accounts are managed yet")
            sys.exit(1)

        # Resolve identifier
        if not identifier.isdigit():
            if not self._validate_email(identifier):
                print(f"Error: Invalid email format: {identifier}")
                sys.exit(1)

        account_num = self._resolve_account_identifier(identifier)
        if not account_num:
            print(f"Error: No account found with identifier: {identifier}")
            sys.exit(1)

        data = self._get_sequence_data()
        account_info = data.get("accounts", {}).get(account_num)
        
        if not account_info:
            print(f"Error: Account-{account_num} does not exist")
            sys.exit(1)

        email = account_info.get("email")
        active_account = data.get("activeAccountNumber")

        if str(active_account) == account_num:
            print(f"Warning: Account-{account_num} ({email}) is currently active")

        confirm = input(f"Are you sure you want to permanently remove Account-{account_num} ({email})? [y/N] ")
        if confirm.lower() != "y":
            print("Cancelled")
            sys.exit(0)

        # Remove backup files
        self._delete_account_credentials(account_num, email)
        config_file = self.configs_dir / f".claude-config-{account_num}-{email}.json"
        if config_file.exists():
            config_file.unlink()

        # Update sequence.json
        del data["accounts"][account_num]
        data["sequence"] = [n for n in data["sequence"] if n != int(account_num)]
        data["lastUpdated"] = self._get_timestamp()
        
        self._write_json(self.sequence_file, data)
        print(f"Account-{account_num} ({email}) has been removed")

    def list_accounts(self):
        """List all managed accounts."""
        if not self.sequence_file.exists():
            print("No accounts are managed yet.")
            self._first_run_setup()
            return

        data = self._get_sequence_data()
        current_email = self._get_current_account()

        # Find active account number by email
        active_num = None
        if current_email != "none":
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

    def _first_run_setup(self):
        """First-run setup workflow."""
        current_email = self._get_current_account()
        
        if current_email == "none":
            print("No active Claude account found. Please log in first.")
            return

        response = input(f"No managed accounts found. Add current account ({current_email}) to managed list? [Y/n] ")
        if response.lower() == "n":
            print(f"Setup cancelled. You can run 'python {sys.argv[0]} --add-account' later.")
            return

        self.add_account()

    def switch(self):
        """Switch to next account in sequence."""
        if not self.sequence_file.exists():
            print("Error: No accounts are managed yet")
            sys.exit(1)

        current_email = self._get_current_account()
        if current_email == "none":
            print("Error: No active Claude account found")
            sys.exit(1)

        # Check if current account is managed
        if not self._account_exists(current_email):
            print(f"Notice: Active account '{current_email}' was not managed.")
            self.add_account()
            data = self._get_sequence_data()
            account_num = data.get("activeAccountNumber")
            print(f"It has been automatically added as Account-{account_num}.")
            print("Please run the switch command again to switch to the next account.")
            sys.exit(0)

        data = self._get_sequence_data()
        sequence = data.get("sequence", [])
        active_account = data.get("activeAccountNumber")

        # Find current index and get next
        try:
            current_index = sequence.index(active_account)
        except ValueError:
            current_index = 0
        
        next_index = (current_index + 1) % len(sequence)
        next_account = str(sequence[next_index])

        self._perform_switch(next_account)

    def switch_to(self, identifier: str):
        """Switch to specific account."""
        if not self.sequence_file.exists():
            print("Error: No accounts are managed yet")
            sys.exit(1)

        # Resolve identifier
        if not identifier.isdigit():
            if not self._validate_email(identifier):
                print(f"Error: Invalid email format: {identifier}")
                sys.exit(1)

        target_account = self._resolve_account_identifier(identifier)
        if not target_account:
            print(f"Error: No account found with identifier: {identifier}")
            sys.exit(1)

        data = self._get_sequence_data()
        if target_account not in data.get("accounts", {}):
            print(f"Error: Account-{target_account} does not exist")
            sys.exit(1)

        self._perform_switch(target_account)

    def _perform_switch(self, target_account: str):
        """Perform the actual account switch."""
        data = self._get_sequence_data()
        current_account = str(data.get("activeAccountNumber"))
        target_email = data["accounts"][target_account]["email"]
        current_email = self._get_current_account()

        # Step 1: Backup current account
        current_creds = self._read_credentials()
        current_config = self._get_claude_config_path().read_text()
        
        self._write_account_credentials(current_account, current_email, current_creds)
        self._write_account_config(current_account, current_email, current_config)

        # Step 2: Retrieve target account
        target_creds = self._read_account_credentials(target_account, target_email)
        target_config = self._read_account_config(target_account, target_email)

        if not target_creds or not target_config:
            print(f"Error: Missing backup data for Account-{target_account}")
            sys.exit(1)

        # Step 3: Activate target account
        self._write_credentials(target_creds)

        # Extract and merge oauthAccount
        target_config_data = json.loads(target_config)
        oauth_section = target_config_data.get("oauthAccount")
        
        if not oauth_section:
            print("Error: Invalid oauthAccount in backup")
            sys.exit(1)

        config_path = self._get_claude_config_path()
        current_config_data = self._read_json(config_path)
        current_config_data["oauthAccount"] = oauth_section
        
        self._write_json(config_path, current_config_data)

        # Step 4: Update state
        data["activeAccountNumber"] = int(target_account)
        data["lastUpdated"] = self._get_timestamp()
        self._write_json(self.sequence_file, data)

        print(f"Switched to Account-{target_account} ({target_email})")
        self.list_accounts()
        print()
        print("Please restart Claude Code to use the new authentication.")
        print()


def main():
    parser = argparse.ArgumentParser(
        description="Multi-Account Switcher for Claude Code",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --add-account
  %(prog)s --list
  %(prog)s --switch
  %(prog)s --switch-to 2
  %(prog)s --switch-to user@example.com
  %(prog)s --remove-account user@example.com
        """
    )
    
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--add-account", action="store_true",
                       help="Add current account to managed accounts")
    group.add_argument("--remove-account", metavar="NUM|EMAIL",
                       help="Remove account by number or email")
    group.add_argument("--list", action="store_true",
                       help="List all managed accounts")
    group.add_argument("--switch", action="store_true",
                       help="Rotate to next account in sequence")
    group.add_argument("--switch-to", metavar="NUM|EMAIL",
                       help="Switch to specific account number or email")

    args = parser.parse_args()

    # Check for root (unless in container)
    switcher = ClaudeAccountSwitcher()
    if os.geteuid() == 0 and not switcher._is_running_in_container():
        print("Error: Do not run this script as root (unless running in a container)")
        sys.exit(1)

    if args.add_account:
        switcher.add_account()
    elif args.remove_account:
        switcher.remove_account(args.remove_account)
    elif args.list:
        switcher.list_accounts()
    elif args.switch:
        switcher.switch()
    elif args.switch_to:
        switcher.switch_to(args.switch_to)


if __name__ == "__main__":
    main()