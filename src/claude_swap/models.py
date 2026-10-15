"""Data models for Claude Swap."""

from __future__ import annotations

import errno
import json
import os
import re
import secrets
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING

from claude_swap.fsutil import replace_with_retry
from claude_swap.usage_store import UsageEntry

if TYPE_CHECKING:
    from claude_swap.switcher import ClaudeAccountSwitcher


#: Alias validation: letters/digits/-/_/., non-empty, not purely digits (so an
#: alias can never collide with a slot number in _resolve_account_identifier),
#: and not leading with '-' (argparse would treat it as an option, making the
#: alias impossible to pass back into any command once set).
_ALIAS_RE = re.compile(r"^[a-z0-9_.-]+$")


def normalize_alias(name: str) -> str:
    """Lowercase and validate a proposed alias; raise ValueError if invalid.

    Shared by the CLI (``cswap alias``), ``cswap add --alias``, and import
    validation so every path enforces identical rules.
    """
    normalized = name.strip().lower()
    if not normalized:
        raise ValueError("alias cannot be empty")
    if normalized.isdigit():
        raise ValueError(f"alias '{name}' cannot be purely numeric (reserved for slot numbers)")
    if normalized.startswith("-"):
        raise ValueError(
            f"alias '{name}' cannot start with '-' (would be read as a command flag)"
        )
    if not _ALIAS_RE.match(normalized):
        raise ValueError(
            f"alias '{name}' may only contain letters, digits, '-', '_', and '.'"
        )
    return normalized


class Platform(Enum):
    """Supported platforms."""

    MACOS = auto()
    LINUX = auto()
    WSL = auto()
    WINDOWS = auto()
    UNKNOWN = auto()

    @classmethod
    def detect(cls) -> Platform:
        """Detect current platform.

        Uses sys.platform rather than platform.system() because the latter
        calls platform.uname() on Windows, which runs a WMI query that can
        hang indefinitely when the WMI service is slow or unresponsive.
        """
        if sys.platform == "darwin":
            return cls.MACOS
        elif sys.platform == "win32":
            return cls.WINDOWS
        elif sys.platform.startswith("linux"):
            if os.environ.get("WSL_DISTRO_NAME"):
                return cls.WSL
            return cls.LINUX
        return cls.UNKNOWN


@dataclass
class AccountInfo:
    """Information about a managed account."""

    email: str
    uuid: str
    organization_uuid: str
    organization_name: str
    added: str
    number: int

    @property
    def is_organization(self) -> bool:
        """Whether this is an organization account."""
        return bool(self.organization_uuid)

    @property
    def display_label(self) -> str:
        """Display label: 'email [OrgName]' or 'email [personal]'."""
        tag = self.organization_name if self.organization_name else "personal"
        return f"{self.email} [{tag}]"

    @classmethod
    def from_dict(cls, number: int, data: dict) -> AccountInfo:
        """Create AccountInfo from dictionary."""
        return cls(
            email=data.get("email", ""),
            uuid=data.get("uuid", ""),
            organization_uuid=data.get("organizationUuid", "") or "",
            organization_name=data.get("organizationName", "") or "",
            added=data.get("added", ""),
            number=number,
        )

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "email": self.email,
            "uuid": self.uuid,
            "organizationUuid": self.organization_uuid,
            "organizationName": self.organization_name,
            "added": self.added,
        }


@dataclass(frozen=True)
class AccountSnapshot:
    """One managed account as seen by interactive UIs (the TUI).

    ``usage`` is the store-backed :class:`UsageEntry` read model; display
    code reads ``usage.last_good``/``age_s`` directly (may show old data,
    annotated with its age), while ``usage.sentinel`` carries derived states
    ("api key", "token expired", ...) that replace the bars entirely.
    """

    number: str
    email: str
    org_name: str
    org_uuid: str
    is_active: bool
    kind: str  # "oauth" | "api_key"
    switchable: bool
    usage: UsageEntry
    alias: str = ""
    disabled: bool = False  # held out of auto-rotation (still a valid explicit target)

    @property
    def display_tag(self) -> str:
        """Org tag for display: the org name, or 'personal'."""
        return self.org_name if self.org_name else "personal"


@dataclass(frozen=True)
class AccountsSnapshot:
    """Coherent one-pass view of every managed account.

    Produced by ``ClaudeAccountSwitcher.accounts_snapshot``: metadata, active
    detection, and usage entries all come from the same collect pass, so a
    consumer never sees an account list and usage table that disagree.
    """

    active_number: str | None
    accounts: tuple[AccountSnapshot, ...]
    taken_at: float


def _restore_atomically(path: Path, text: str) -> None:
    """Put `text` back without truncating what is already there.

    `Path.write_text` opens with O_TRUNC, so it destroys the destination
    BEFORE it can fail -- and the rollback runs on failures where the write
    never opened the destination at all (a temp-write ENOSPC, an invalid
    payload, a non-EBUSY rename error), where those bytes are the intact
    original. The one fault that produces both halves is a full filesystem
    or an exhausted quota, which is the failure class this restore exists
    for. Name first, publish by rename, so a failed restore leaves the
    destination exactly as it was.

    A FAILURE TO NAME THE TEMP NEVER FALLS BACK. That errno is a fact
    about the parent directory, and a bind-mounted destination is not even
    on the same filesystem, so a rewrite authorised on that reading
    truncates a file whose own room nothing measured. Only EBUSY on the
    PUBLISH does, because that errno IS about the destination and says the
    one thing that makes a rewrite the only option.
    """
    tmp = path.parent / f".{path.name}.restore.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    fd = -1
    try:
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            # DISOWN A NAME WE DID NOT CREATE. `O_EXCL` refused it, so it
            # belongs to whoever is writing it; the cleanup below must not
            # reach for it.
            tmp = None
            raise
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(text)
        if sys.platform != "win32":
            os.chmod(tmp, 0o600)
        try:
            replace_with_retry(tmp, path)
            tmp = None
        except OSError as e:
            if e.errno != errno.EBUSY:
                raise
            # A BIND-MOUNTED DESTINATION PINS THE INODE, so no rename can
            # ever land there and writing through is the only way -- which
            # is exactly what `_write_json` does at this same destination,
            # and how it EMPTIED the file whose bytes this restore is
            # holding. Scoped to the PUBLISH: a temp that could not be
            # created says nothing about the destination's own room, and
            # rewriting on that reading is what truncated one.
            path.write_text(text, encoding="utf-8")
            if sys.platform != "win32":
                os.chmod(path, 0o600)
    finally:
        if fd >= 0:
            os.close(fd)
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass


@dataclass
class SwitchTransaction:
    """Represents a switch operation that can be rolled back."""

    original_credentials: str
    original_config: str
    original_account_num: str
    original_email: str
    config_path: Path
    # THE ROSTER'S OWN BYTES. The arm below used to re-read the file, which
    # answers nothing when the write that failed EMPTIED it -- and an empty
    # `sequence.json` is not a lost pointer, it is a cswap that refuses to
    # run, because `_get_sequence_data` reads it strictly.
    original_sequence: str = ""
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
                    _restore_atomically(self.config_path, self.original_config)
                elif step == "sequence_updated":
                    if self.original_sequence:
                        # PLAIN WRITE, like the config arm above. Restoring
                        # through `_write_json` re-enters the very recovery
                        # that emptied the file, so the restore empties it
                        # again. The snapshot already carries the original
                        # `activeAccountNumber`, so it goes back verbatim.
                        _restore_atomically(
                            switcher.sequence_file, self.original_sequence
                        )
                    else:
                        data = switcher._get_sequence_data()
                        if data:
                            data["activeAccountNumber"] = int(
                                self.original_account_num
                            )
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
