"""macOS Claude Desktop account switching with shared local session history.

Claude Code transcripts live under ``~/.claude/projects`` and are independent
of the logged-in account. Claude Desktop keeps a separate session index under
``~/Library/Application Support/Claude/claude-code-sessions/<account-uuid>/``
and filters the sidebar by the active account. This module copies only missing,
resumable local-session index entries into the target account's index. Source
metadata and transcripts are never changed.

Claude Desktop's web login is stored in Electron's user-data directory. It is
independent from Claude Code's credential store, so changing only the global
Claude Code login does not switch the Desktop account. Each account therefore
gets one persistent Desktop profile. Managed profiles symlink only their
``claude-code-sessions`` directory to the default profile, keeping web auth
isolated while sharing the local-session index.

The Desktop app must be closed while the index is reconciled because it caches
the index in memory. ``DesktopManager`` refuses to close an app with a non-idle
local task, switches through the existing ``ClaudeAccountSwitcher`` authority,
then launches the target account's authenticated Desktop profile.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from claude_swap.exceptions import DesktopError
from claude_swap.json_output import SCHEMA_VERSION
from claude_swap.paths import (
    get_default_claude_config_home,
    get_default_global_config_path,
)
from claude_swap.process_detection import list_sessions
from claude_swap.settings import atomic_write_json

if TYPE_CHECKING:
    from claude_swap.switcher import ClaudeAccountSwitcher


CLAUDE_DESKTOP_BUNDLE_ID = "com.anthropic.claudefordesktop"
CLAUDE_DESKTOP_EXECUTABLE = Path(
    "/Applications/Claude.app/Contents/MacOS/Claude"
)
DESKTOP_PROFILES_SCHEMA_VERSION = 1
_LOCAL_SESSION_RE = re.compile(r"^local_[A-Za-z0-9_-]+\.json$")
_STALE_ACCOUNT_FIELDS = ("bridgeSessionIds", "error", "errorAt")


def _is_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value.lower()
    except (ValueError, AttributeError):
        return False


def default_user_data_dir() -> Path:
    """Return the Claude Desktop profile selected for this invocation."""
    override = os.environ.get("CLAUDE_USER_DATA_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Application Support" / "Claude"


@dataclass(frozen=True)
class DesktopSyncResult:
    copied: int = 0
    existing: int = 0
    unavailable: int = 0
    invalid: int = 0

    def to_json(self) -> dict:
        return {
            "copied": self.copied,
            "existing": self.existing,
            "unavailable": self.unavailable,
            "invalid": self.invalid,
        }


@dataclass(frozen=True)
class DesktopProfile:
    account_uuid: str
    email: str
    path: Path
    is_default: bool
    initialized: bool

    def to_json(self) -> dict:
        return {
            "initialized": self.initialized,
            "isDefault": self.is_default,
        }


class DesktopProfileStore:
    """Own the account-to-Electron-profile mapping and shared-session link."""

    def __init__(
        self,
        backup_dir: Path,
        *,
        default_profile_dir: Path | None = None,
    ) -> None:
        self.root = backup_dir / "desktop"
        self.registry_path = self.root / "profiles.json"
        self.default_profile_dir = default_profile_dir or default_user_data_dir()
        self.shared_sessions = self.default_profile_dir / "claude-code-sessions"

    def _load(self) -> dict:
        if not self.registry_path.exists():
            return {
                "schemaVersion": DESKTOP_PROFILES_SCHEMA_VERSION,
                "profiles": {},
            }
        try:
            data = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DesktopError(
                f"Could not read Desktop profile registry: {exc}"
            ) from exc
        if (
            not isinstance(data, dict)
            or data.get("schemaVersion") != DESKTOP_PROFILES_SCHEMA_VERSION
            or not isinstance(data.get("profiles"), dict)
        ):
            raise DesktopError("Desktop profile registry has an unsupported format.")
        return data

    def _save(self, data: dict) -> None:
        atomic_write_json(self.registry_path, data)

    def _managed_profile_path(self, account_uuid: str) -> Path:
        return self.root / account_uuid

    def _profile_from_record(self, account_uuid: str, record: dict) -> DesktopProfile:
        path_value = record.get("path")
        email = record.get("email")
        if not isinstance(path_value, str) or not isinstance(email, str):
            raise DesktopError("Desktop profile registry contains an invalid record.")
        return DesktopProfile(
            account_uuid=account_uuid,
            email=email,
            path=Path(path_value),
            is_default=bool(record.get("isDefault")),
            initialized=bool(record.get("initialized")),
        )

    def _record(self, profile: DesktopProfile) -> dict:
        return {
            "email": profile.email,
            "path": str(profile.path),
            "isDefault": profile.is_default,
            "initialized": profile.initialized,
        }

    def _ensure_managed_profile(self, profile: DesktopProfile) -> None:
        profile.path.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            os.chmod(profile.path, 0o700)
        if not self.shared_sessions.is_dir():
            raise DesktopError(
                f"Shared Claude Desktop session index not found at "
                f"{self.shared_sessions}"
            )
        link = profile.path / "claude-code-sessions"
        if link.is_symlink():
            if link.resolve() != self.shared_sessions.resolve():
                raise DesktopError(
                    f"Desktop profile has an unexpected session link at {link}"
                )
            return
        if link.exists():
            raise DesktopError(
                f"Desktop profile already has private session data at {link}; "
                "refusing to replace it."
            )
        link.symlink_to(self.shared_sessions, target_is_directory=True)

    def resolve(
        self,
        account_uuid: str,
        email: str,
        *,
        current_account_uuid: str | None,
        current_email: str,
        current_profile_dir: Path,
        dry_run: bool = False,
    ) -> DesktopProfile:
        """Resolve a profile, adopting the current profile and creating targets."""
        data = self._load()
        profiles = data["profiles"]
        changed = False

        if current_account_uuid and current_account_uuid not in profiles:
            current = DesktopProfile(
                account_uuid=current_account_uuid,
                email=current_email,
                path=current_profile_dir,
                is_default=current_profile_dir == self.default_profile_dir,
                initialized=True,
            )
            profiles[current_account_uuid] = self._record(current)
            changed = True

        record = profiles.get(account_uuid)
        if record is not None:
            profile = self._profile_from_record(account_uuid, record)
            if profile.email != email:
                profile = DesktopProfile(
                    account_uuid=profile.account_uuid,
                    email=email,
                    path=profile.path,
                    is_default=profile.is_default,
                    initialized=profile.initialized,
                )
                profiles[account_uuid] = self._record(profile)
                changed = True
        elif current_account_uuid == account_uuid:
            profile = DesktopProfile(
                account_uuid=account_uuid,
                email=email,
                path=current_profile_dir,
                is_default=current_profile_dir == self.default_profile_dir,
                initialized=True,
            )
            profiles[account_uuid] = self._record(profile)
            changed = True
        else:
            profile = DesktopProfile(
                account_uuid=account_uuid,
                email=email,
                path=self._managed_profile_path(account_uuid),
                is_default=False,
                initialized=False,
            )
            profiles[account_uuid] = self._record(profile)
            changed = True

        if not dry_run:
            if not profile.is_default:
                self._ensure_managed_profile(profile)
            if changed:
                self._save(data)
        return profile

    def mark_initialized(self, profile: DesktopProfile) -> DesktopProfile:
        return self._set_initialized(profile, True)

    def mark_uninitialized(self, profile: DesktopProfile) -> DesktopProfile:
        return self._set_initialized(profile, False)

    def _set_initialized(
        self, profile: DesktopProfile, initialized: bool
    ) -> DesktopProfile:
        data = self._load()
        updated = DesktopProfile(
            account_uuid=profile.account_uuid,
            email=profile.email,
            path=profile.path,
            is_default=profile.is_default,
            initialized=initialized,
        )
        data["profiles"][profile.account_uuid] = self._record(updated)
        self._save(data)
        return updated


class DesktopSessionSync:
    """Reconcile resumable Desktop index entries into one account root."""

    def __init__(
        self,
        user_data_dir: Path | None = None,
        claude_config_dir: Path | None = None,
    ) -> None:
        self.user_data_dir = user_data_dir or default_user_data_dir()
        self.sessions_root = self.user_data_dir / "claude-code-sessions"
        self.claude_config_dir = claude_config_dir or get_default_claude_config_home()

    def _transcript_ids(self) -> set[str]:
        projects = self.claude_config_dir / "projects"
        if not projects.is_dir():
            return set()
        return {path.stem for path in projects.rglob("*.jsonl") if path.is_file()}

    @staticmethod
    def _read_entry(path: Path) -> dict | None:
        if not _LOCAL_SESSION_RE.fullmatch(path.name):
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or data.get("sessionId") != path.stem:
            return None
        return data

    @staticmethod
    def _write_new_entry(path: Path, data: dict) -> bool:
        """Atomically create ``path`` without replacing an existing entry."""
        path.parent.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            os.chmod(path.parent, 0o700)
        fd, temp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temp_name, path)
            except FileExistsError:
                return False
            if sys.platform != "win32":
                os.chmod(path, 0o600)
            return True
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    def sync(
        self,
        target_account_uuid: str,
        target_organization_uuid: str,
        *,
        dry_run: bool = False,
    ) -> DesktopSyncResult:
        """Copy missing resumable entries into the active account and org.

        A session is resumable only when its metadata has a ``cliSessionId``
        and the matching transcript exists under ``projects/``. Existing entries
        in the active target org win by session ID.
        """
        if not _is_uuid(target_account_uuid):
            raise DesktopError(
                "The target account has no valid account UUID; re-add it with "
                "`cswap add --slot N` before using Desktop switching."
            )
        if not _is_uuid(target_organization_uuid):
            raise DesktopError(
                "The target account has no valid organization UUID; re-add it "
                "with `cswap add --slot N` before using Desktop switching."
            )
        if not self.sessions_root.is_dir():
            raise DesktopError(
                f"Claude Desktop session index not found at {self.sessions_root}"
            )

        transcript_ids = self._transcript_ids()
        target_root = self.sessions_root / target_account_uuid
        target_org_root = target_root / target_organization_uuid
        existing_ids: set[str] = set()
        if target_org_root.is_dir():
            for path in target_org_root.glob("*.json"):
                entry = self._read_entry(path)
                if entry is not None:
                    existing_ids.add(entry["sessionId"])

        candidates: dict[str, dict] = {}
        unavailable = 0
        invalid = 0
        for account_root in sorted(self.sessions_root.iterdir()):
            if not account_root.is_dir() or not _is_uuid(account_root.name):
                continue
            for org_root in sorted(account_root.iterdir()):
                if not org_root.is_dir() or not _is_uuid(org_root.name):
                    continue
                if (
                    account_root.name == target_account_uuid
                    and org_root.name == target_organization_uuid
                ):
                    continue
                for path in sorted(org_root.glob("local_*.json")):
                    entry = self._read_entry(path)
                    if entry is None:
                        invalid += 1
                        continue
                    cli_session_id = entry.get("cliSessionId")
                    if (
                        entry.get("transcriptUnavailable") is True
                        or not isinstance(cli_session_id, str)
                        or cli_session_id not in transcript_ids
                    ):
                        unavailable += 1
                        continue
                    candidates.setdefault(entry["sessionId"], entry)

        copied = 0
        existing = 0
        for session_id, entry in sorted(candidates.items()):
            if session_id in existing_ids:
                existing += 1
                continue
            cleaned = dict(entry)
            for field in _STALE_ACCOUNT_FIELDS:
                cleaned.pop(field, None)
            destination = target_org_root / f"{session_id}.json"
            if not dry_run and not self._write_new_entry(destination, cleaned):
                existing += 1
                existing_ids.add(session_id)
                continue
            copied += 1
            existing_ids.add(session_id)

        return DesktopSyncResult(
            copied=copied,
            existing=existing,
            unavailable=unavailable,
            invalid=invalid,
        )


class DesktopManager:
    """Switch auth, reconcile history, and launch the account's web profile."""

    def __init__(
        self,
        switcher: "ClaudeAccountSwitcher",
        *,
        syncer: DesktopSessionSync | None = None,
        profile_store: DesktopProfileStore | None = None,
        quit_timeout: float = 10.0,
        launch_timeout: float = 15.0,
        identity_timeout: float = 15.0,
    ) -> None:
        self.switcher = switcher
        self.syncer = syncer or DesktopSessionSync()
        self.profile_store = profile_store or DesktopProfileStore(
            switcher.backup_dir
        )
        self.quit_timeout = quit_timeout
        self.launch_timeout = launch_timeout
        self.identity_timeout = identity_timeout

    @staticmethod
    def _desktop_pids() -> list[int]:
        result = subprocess.run(
            ["pgrep", "-x", "Claude"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return []
        return [int(pid) for pid in result.stdout.split() if pid.isdigit()]

    @classmethod
    def _desktop_is_running(cls) -> bool:
        return bool(cls._desktop_pids())

    @staticmethod
    def _console_is_unlocked() -> bool:
        """Return whether the console can provide encrypted web storage."""
        result = subprocess.run(
            ["ioreg", "-n", "Root", "-d1", "-a"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            return False
        try:
            data = plistlib.loads(result.stdout)
            root = data[0] if isinstance(data, list) and data else data
            return (
                isinstance(root, dict)
                and root.get("IOConsoleLocked") is False
            )
        except (plistlib.InvalidFileException, TypeError):
            return False

    def _running_profile_dir(self) -> Path:
        """Return the user-data directory selected by the running main process."""
        for pid in reversed(self._desktop_pids()):
            result = subprocess.run(
                ["ps", "eww", "-p", str(pid), "-o", "command="],
                capture_output=True,
                text=True,
                check=False,
            )
            match = re.search(
                r"(?:^|\s)CLAUDE_USER_DATA_DIR=(\S+)", result.stdout
            )
            if match:
                return Path(match.group(1))
        return self.profile_store.default_profile_dir

    @staticmethod
    def _live_identity() -> tuple[str | None, str]:
        path = get_default_global_config_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None, ""
        account = data.get("oauthAccount")
        if not isinstance(account, dict):
            return None, ""
        account_uuid = account.get("accountUuid")
        email = account.get("emailAddress") or account.get("email") or ""
        return (
            account_uuid if isinstance(account_uuid, str) else None,
            email if isinstance(email, str) else "",
        )

    def _blocking_sessions(self) -> list:
        return [
            session
            for session in list_sessions(self.syncer.claude_config_dir)
            if session.entrypoint == "claude-desktop" and session.status != "idle"
        ]

    def _quit_desktop(self) -> None:
        result = subprocess.run(
            [
                "osascript",
                "-e",
                f'tell application id "{CLAUDE_DESKTOP_BUNDLE_ID}" to quit',
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or "AppleScript quit failed"
            raise DesktopError(f"Could not close Claude Desktop: {detail}")
        deadline = time.monotonic() + self.quit_timeout
        while self._desktop_is_running():
            if time.monotonic() >= deadline:
                raise DesktopError(
                    "Claude Desktop did not quit within 10 seconds; account unchanged."
                )
            time.sleep(0.1)

    def _launch_desktop(self, profile: DesktopProfile) -> None:
        if not CLAUDE_DESKTOP_EXECUTABLE.is_file():
            raise DesktopError(
                f"Claude Desktop executable not found at {CLAUDE_DESKTOP_EXECUTABLE}"
            )
        command = [
            "/usr/bin/open",
            "-n",
            "-b",
            CLAUDE_DESKTOP_BUNDLE_ID,
        ]
        if not profile.is_default:
            command.extend(
                ["--env", f"CLAUDE_USER_DATA_DIR={profile.path}"]
            )
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            raise DesktopError(f"Could not launch Claude Desktop: {exc}") from exc
        if result.returncode != 0:
            detail = result.stderr.strip() or "LaunchServices failed"
            raise DesktopError(f"Could not launch Claude Desktop: {detail}")
        deadline = time.monotonic() + self.launch_timeout
        while not self._desktop_is_running():
            if time.monotonic() >= deadline:
                raise DesktopError("Claude Desktop did not start within 15 seconds.")
            time.sleep(0.1)

    @staticmethod
    def _profile_matches_account(
        profile: DesktopProfile, account_uuid: str
    ) -> bool:
        """Verify the account identity cached in this Electron profile."""
        needle = account_uuid.encode("ascii")
        indexed_db = profile.path / "IndexedDB"
        if not indexed_db.is_dir():
            return False
        try:
            files = (path for path in indexed_db.rglob("*") if path.is_file())
            for path in files:
                with path.open("rb") as handle:
                    overlap = b""
                    while chunk := handle.read(1024 * 1024):
                        data = overlap + chunk
                        if needle in data:
                            return True
                        overlap = data[-len(needle) :]
        except OSError:
            return False
        return False

    def _wait_for_identity(
        self, profile: DesktopProfile, account_uuid: str
    ) -> bool:
        deadline = time.monotonic() + self.identity_timeout
        matching_since: float | None = None
        profile_matches = False
        next_profile_check = 0.0
        while time.monotonic() < deadline:
            live_uuid, _ = self._live_identity()
            now = time.monotonic()
            if not profile_matches and now >= next_profile_check:
                profile_matches = self._profile_matches_account(
                    profile, account_uuid
                )
                next_profile_check = now + 1.0
            if (
                profile_matches and live_uuid == account_uuid
            ):
                if matching_since is None:
                    matching_since = now
                elif now - matching_since >= 2.0:
                    return True
            else:
                matching_since = None
            if not self._desktop_is_running():
                return False
            time.sleep(0.25)
        return False

    def _restore_original(
        self,
        original_identifier: str | None,
        original_profile: DesktopProfile | None,
        *,
        was_running: bool,
        switch_completed: bool,
    ) -> None:
        if self._desktop_is_running():
            self._quit_desktop()
        if switch_completed and original_identifier:
            self.switcher.switch_to(original_identifier, json_output=True)
        if was_running and original_profile:
            self._launch_desktop(original_profile)

    def run(
        self,
        identifier: str,
        *,
        dry_run: bool = False,
        json_output: bool = False,
        confirm_login: bool = False,
    ) -> dict:
        if sys.platform != "darwin":
            raise DesktopError("Claude Desktop switching is currently macOS-only.")

        account_num, email, organization_uuid = self.switcher.resolve_account(
            identifier
        )
        account_uuid = self.switcher.account_identity(account_num).get("uuid", "")
        if not _is_uuid(account_uuid):
            raise DesktopError(
                "The target account has no valid account UUID; re-add it with "
                "`cswap add --slot N` before using Desktop switching."
            )

        was_running = self._desktop_is_running()
        current_uuid, current_email = self._live_identity()
        current_profile_dir = self._running_profile_dir()
        running_uuid = current_uuid if was_running else None
        original_profile = None
        if running_uuid:
            original_profile = self.profile_store.resolve(
                running_uuid,
                current_email,
                current_account_uuid=running_uuid,
                current_email=current_email,
                current_profile_dir=current_profile_dir,
                dry_run=dry_run,
            )
        profile = self.profile_store.resolve(
            account_uuid,
            email,
            current_account_uuid=running_uuid,
            current_email=current_email,
            current_profile_dir=current_profile_dir,
            dry_run=dry_run,
        )

        preview = self.syncer.sync(
            account_uuid, organization_uuid, dry_run=True
        )
        already_active = (
            was_running
            and running_uuid == account_uuid
            and current_profile_dir.resolve() == profile.path.resolve()
        )
        if dry_run:
            if confirm_login:
                raise DesktopError("--confirm-login cannot be used with --dry-run.")
            return self._payload(
                account_num,
                email,
                preview,
                profile,
                dry_run=True,
                login_required=not profile.initialized,
                already_active=already_active,
            )

        if already_active:
            if confirm_login:
                profile = self.profile_store.mark_initialized(profile)
                identity_matches = True
            elif profile.initialized:
                identity_matches = True
            else:
                identity_matches = self._wait_for_identity(profile, account_uuid)
                if identity_matches:
                    profile = self.profile_store.mark_initialized(profile)
            result = self.syncer.sync(account_uuid, organization_uuid)
            return self._payload(
                account_num,
                email,
                result,
                profile,
                dry_run=False,
                login_required=not identity_matches,
                already_active=True,
            )

        blocking = self._blocking_sessions()
        if blocking:
            raise DesktopError(
                "Claude Desktop has an active local task. Let it finish or stop it "
                "before switching accounts."
            )

        if confirm_login:
            raise DesktopError(
                "--confirm-login requires Claude Desktop to be open on the "
                "selected profile."
            )

        if not self._console_is_unlocked():
            raise DesktopError(
                "The Mac is locked. Unlock it before switching Claude Desktop "
                "accounts so encrypted login storage remains available."
            )

        original_identifier = None
        if running_uuid:
            data = self.switcher._get_sequence_data() or {}
            for number, record in data.get("accounts", {}).items():
                if record.get("uuid") == running_uuid:
                    original_identifier = str(number)
                    break
            if original_identifier is None:
                raise DesktopError(
                    "The current Claude Desktop account is not stored in cswap; "
                    "add it before switching so rollback remains possible."
                )

        if was_running:
            self._quit_desktop()

        switch_completed = False
        try:
            self.switcher.switch_to(identifier, json_output=json_output)
            switch_completed = True
            result = self.syncer.sync(account_uuid, organization_uuid)
            self._launch_desktop(profile)
            identity_matches = profile.initialized
            if not identity_matches and self._profile_matches_account(
                profile, account_uuid
            ):
                identity_matches = self._wait_for_identity(profile, account_uuid)
            if identity_matches:
                profile = self.profile_store.mark_initialized(profile)
            return self._payload(
                account_num,
                email,
                result,
                profile,
                dry_run=False,
                login_required=not identity_matches,
                already_active=False,
            )
        except Exception as exc:
            try:
                self._restore_original(
                    original_identifier,
                    original_profile,
                    was_running=was_running,
                    switch_completed=switch_completed,
                )
            except Exception as rollback_exc:
                raise DesktopError(
                    f"Desktop switch failed ({exc}); rollback also failed "
                    f"({rollback_exc})."
                ) from exc
            raise

    @staticmethod
    def _payload(
        account_num: str,
        email: str,
        result: DesktopSyncResult,
        profile: DesktopProfile,
        *,
        dry_run: bool,
        login_required: bool,
        already_active: bool,
    ) -> dict:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "dryRun": dry_run,
            "account": {"number": int(account_num), "email": email},
            "profile": profile.to_json(),
            "loginRequired": login_required,
            "alreadyActive": already_active,
            "sessions": result.to_json(),
        }
