"""Data models for Claude Swap."""

from __future__ import annotations

import json
import os
import platform as platform_module
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claude_swap.switcher import ClaudeAccountSwitcher


class Platform(Enum):
    """Supported platforms."""

    MACOS = auto()
    LINUX = auto()
    WSL = auto()
    WINDOWS = auto()
    UNKNOWN = auto()

    @classmethod
    def detect(cls) -> Platform:
        """Detect current platform."""
        system = platform_module.system()
        if system == "Darwin":
            return cls.MACOS
        elif system == "Windows":
            return cls.WINDOWS
        elif system == "Linux":
            if os.environ.get("WSL_DISTRO_NAME"):
                return cls.WSL
            return cls.LINUX
        return cls.UNKNOWN


@dataclass
class AccountInfo:
    """Information about a managed account."""

    email: str
    uuid: str
    added: str
    number: int

    @classmethod
    def from_dict(cls, number: int, data: dict) -> AccountInfo:
        """Create AccountInfo from dictionary."""
        return cls(
            email=data.get("email", ""),
            uuid=data.get("uuid", ""),
            added=data.get("added", ""),
            number=number,
        )

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "email": self.email,
            "uuid": self.uuid,
            "added": self.added,
        }


@dataclass
class SwitchTransaction:
    """Represents a switch operation that can be rolled back."""

    original_credentials: str
    original_config: str
    original_account_num: str
    original_email: str
    config_path: Path
    completed_steps: list[str] = field(default_factory=list)

    def record_step(self, step: str) -> None:
        """Record a completed step."""
        self.completed_steps.append(step)

    def rollback(self, switcher: ClaudeAccountSwitcher) -> bool:
        """Rollback all completed steps in reverse order.

        Returns:
            True if rollback successful, False if any step failed.
        """
        success = True
        for step in reversed(self.completed_steps):
            try:
                if step == "credentials_written":
                    switcher._write_credentials(self.original_credentials)
                elif step == "config_written":
                    self.config_path.write_text(self.original_config)
                    if sys.platform != "win32":
                        os.chmod(self.config_path, 0o600)
                elif step == "sequence_updated":
                    data = switcher._get_sequence_data()
                    if data:
                        data["activeAccountNumber"] = int(self.original_account_num)
                        data["lastUpdated"] = get_timestamp()
                        switcher._write_json(switcher.sequence_file, data)
                switcher._logger.info(f"Rolled back step: {step}")
            except Exception as e:
                switcher._logger.error(f"Failed to rollback step {step}: {e}")
                success = False
        return success


def get_timestamp() -> str:
    """Get current UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
