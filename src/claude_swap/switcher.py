"""Core account switcher logic for Claude Code."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from claude_swap import macos_keychain

from claude_swap.exceptions import (
    AccountNotFoundError,
    ConfigError,
    CredentialReadError,
    SessionError,
    SwitchError,
    ValidationError,
)
from claude_swap import oauth
from claude_swap.claude_locks import claude_config_lock, claude_credentials_lock
from claude_swap.json_output import (
    SCHEMA_VERSION,
    USAGE_API_KEY,
    USAGE_KEYCHAIN_UNAVAILABLE,
    USAGE_NO_CREDENTIALS,
    USAGE_RELOGIN_REQUIRED,
    USAGE_TOKEN_EXPIRED,
    account_ref,
    account_row,
    usage_fields,
    usage_freshness_fields,
)
from claude_swap.credentials import (  # noqa: F401  (constants re-exported for migrations/tests)
    CLAUDE_CODE_KEYCHAIN_SERVICE,
    SECURITY_SERVICE,
    ActiveCredentials,
    CredentialStore,
    looks_like_api_key,
)
from claude_swap.locking import FileLock
from claude_swap.logging_config import setup_logging
from claude_swap.models import (
    AccountSnapshot,
    AccountsSnapshot,
    Platform,
    SwitchTransaction,
    get_timestamp,
    normalize_alias,
)
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
    get_backup_root,
    get_credentials_path,
    get_global_config_path,
    get_legacy_backup_root,
    migrate_legacy_backup_dir,
)
from claude_swap.process_detection import get_running_instances
from claude_swap import poll_policy
from claude_swap.settings import load_settings, parse_model_names, settings_path
from claude_swap.usage_store import (
    FetchRecord,
    UsageEntry,
    UsageStore,
    with_sentinel,
)

# Service name under which the legacy ``keyring`` backend stored per-account
# backup credentials on macOS (kept for the one-time keyring → security migration
# and for the Windows Credential Manager migration).
KEYRING_SERVICE = "claude-code"

# SECURITY_SERVICE and CLAUDE_CODE_KEYCHAIN_SERVICE now live in credentials.py
# (storage concerns); re-exported above for migrations.py and the test suite.

# Setup-tokens are inference-only server-side; wider scopes trigger 403s
# on profile endpoints. Matches Claude Code's CLAUDE_CODE_OAUTH_TOKEN path.
SETUP_TOKEN_SCOPES = ("user:inference",)

# Delay between successive usage-request launches in one collect pass, so N
# accounts never burst the shared usage endpoint from one IP in the same
# instant (request hygiene; see issue #85).
_FETCH_STAGGER_S = 0.25

# Show a "· Xm ago" age note on displayed usage older than this. Inside the
# serve TTL the data is current by design (that is the polling cadence), so
# an age note there would be permanent noise.
_USAGE_AGE_NOTE_S = poll_policy.SERVE_TTL_S


def _format_usage_lines(usage: dict) -> list[str]:
    # Collect (label, body) rows first, then pad every label to the widest one so
    # per-model names (e.g. "Fable") don't shift the columns of the other lines.
    rows: list[tuple[str, str]] = []
    spend = usage.get("spend")
    if spend:
        used = spend["used"]
        limit = spend["limit"]
        pct = spend["pct"]
        cell = oauth.fresh_reset_strings(spend)
        if cell:
            rows.append(("$$", f"{pct:>3.0f}%   resets {cell[1]:<12}  ${used:,.2f} / ${limit:,.2f}"))
        else:
            rows.append(("$$", f"{pct:>3.0f}%   ${used:,.2f} / ${limit:,.2f}"))
    for label, w in (("5h", usage.get("five_hour")), ("7d", usage.get("seven_day"))):
        if w:
            cell = oauth.fresh_reset_strings(w)
            if cell:
                countdown, clock = cell
                rows.append((label, f"{w['pct']:>3.0f}%   resets {clock:<12}  in {countdown}"))
            else:
                rows.append((label, f"{w['pct']:>3.0f}%"))
    for w in usage.get("scoped") or []:
        # Per-model weekly limits (e.g. Fable). Flag ones at/over the limit so a
        # maxed model — the usual reason to switch — stands out.
        marker = "  (!)" if w["pct"] >= 100 else ""
        cell = oauth.fresh_reset_strings(w)
        if cell:
            countdown, clock = cell
            rows.append((w["name"], f"{w['pct']:>3.0f}%   resets {clock:<12}  in {countdown}{marker}"))
        else:
            rows.append((w["name"], f"{w['pct']:>3.0f}%{marker}"))
    width = max((len(label) for label, _ in rows), default=0) + 1  # label + ':'
    return [f"{label + ':':<{width}} {body}" for label, body in rows]


# Human notes for sentinel usage states (fallback: the raw sentinel string).
# Public: the TUI renders the same wording so both surfaces describe a state
# identically (e.g. owned-and-expired means Claude Code will refresh, not that
# the user must re-login).
SENTINEL_NOTES = {
    USAGE_TOKEN_EXPIRED: "token expired — Claude Code refreshes the active account",
    USAGE_API_KEY: "API key (no quota)",
    USAGE_KEYCHAIN_UNAVAILABLE: "keychain unavailable — locked or in use; try again",
    USAGE_RELOGIN_REQUIRED: "re-login needed — refresh token dead; log in with Claude Code, then run: cswap add",
}


def last_seen_note(entry: UsageEntry) -> str | None:
    """"last seen 53% used · 12m ago" from an entry's last-good measurement.

    Public: the TUI renders the same note under sentinel states (see
    ``SENTINEL_NOTES``), so both surfaces stay word-for-word identical.
    """
    if entry.last_good is None or entry.fetched_at is None:
        return None
    headroom = oauth.account_headroom(entry.last_good)
    if headroom is None:
        return None
    return (
        f"last seen {100 - headroom:.0f}% used · "
        f"{format_age(int(entry.fetched_at * 1000))}"
    )


def _usage_entry_lines(entry: UsageEntry) -> list[str]:
    """Styled usage lines (sans indent) for one account's entry.

    Sentinel states render their note first, with a supplementary "last seen"
    line when an older measurement exists. Measurements render as usual, age-
    annotated once older than ``_USAGE_AGE_NOTE_S`` (stale-served); an account
    with no measurement at all shows "usage unavailable" plus the last fetch
    error, so a failing endpoint is visible instead of a silent blank.
    """
    if entry.sentinel is not None:
        out = [dimmed(SENTINEL_NOTES.get(entry.sentinel, entry.sentinel))]
        last_seen = last_seen_note(entry)
        if last_seen is not None and entry.sentinel != USAGE_API_KEY:
            out.append(f"{dimmed('└')} {muted(last_seen)}")
        return out
    if entry.last_good is not None:
        lines = _format_usage_lines(entry.last_good)
        if (
            lines
            and entry.age_s is not None
            and entry.age_s > _USAGE_AGE_NOTE_S
            and entry.fetched_at is not None
        ):
            lines[-1] += f" · {format_age(int(entry.fetched_at * 1000))}"
        return [
            f"{dimmed('└' if j == len(lines) - 1 else '├')} {muted(line)}"
            for j, line in enumerate(lines)
        ]
    detail = "usage unavailable"
    if entry.last_error:
        detail += f" ({entry.last_error})"
    return [dimmed(detail)]


def _sweep_legacy_keyring(usernames: list[str], removed_items: list[str]) -> None:
    """Best-effort purge of legacy ``KEYRING_SERVICE`` entries via ``keyring``.

    Used only during ``purge()`` to mop up entries a never-completed
    keyring → file/security migration left behind. Never raises: keyring being
    unavailable or an entry being absent just means nothing to clean up.
    """
    try:
        import keyring  # noqa: PLC0415 - legacy cleanup only

        for username in usernames:
            try:
                keyring.delete_password(KEYRING_SERVICE, username)
                removed_items.append(f"Legacy keyring credential: {username}")
            except Exception:
                pass  # Doesn't exist / other error — ignore
    except Exception:
        pass  # keyring unavailable — nothing to clean up


class ClaudeAccountSwitcher:
    """Multi-account switcher for Claude Code."""

    def __init__(self, debug: bool = False):
        self.home = Path.home()
        self.platform = Platform.detect()
        self.backup_dir = get_backup_root()

        # Migrate legacy ~/.claude-swap-backup to the new XDG path on Linux/WSL
        # before any logger or directory setup writes to the new location.
        # Migration is a no-op on macOS/Windows where backup_dir already
        # equals the legacy path. MigrationError on a genuine collision
        # propagates as a ClaudeSwitchError and is caught by the CLI.
        if migrate_legacy_backup_dir(self.backup_dir):
            legacy = get_legacy_backup_root()
            print(
                f"claude-swap: migrated data from {legacy} to {self.backup_dir}",
                file=sys.stderr,
            )

        self.sequence_file = self.backup_dir / "sequence.json"
        self.configs_dir = self.backup_dir / "configs"
        self.credentials_dir = self.backup_dir / "credentials"
        self.lock_file = self.backup_dir / ".lock"
        self._logger = setup_logging(self.backup_dir, debug=debug)
        self._usage_store = UsageStore(self.backup_dir / "cache")
        # (settings mtime, (threshold, models)) — see _poll_policy_inputs.
        self._poll_inputs_cache: tuple[float | None, tuple[float, tuple[str, ...]]] | None = None
        self._poll_inputs_override: tuple[float, tuple[str, ...]] | None = None

        # The credential storage layer (active + per-account backup stores, macOS
        # Keychain-vs-file routing, the per-process capability cache). Reads its
        # live config (platform, _logger, credentials_dir) back off this switcher.
        # Constructed BEFORE run_migrations(), which performs storage ops on macOS.
        # One store per switcher: the capability cache is per-process.
        self._store = CredentialStore(self)

        # Set by _build_accounts_info: True when the active account's OAuth
        # credential could not be read because the macOS Keychain was unavailable
        # (locked / denied / timeout) with no fallback — so the usage row shows
        # "keychain unavailable" instead of a misleading "no credentials".
        self._active_keychain_unavailable = False

        # Run any pending one-time data migrations (e.g. relocating Windows
        # backup credentials out of Credential Manager into files). Imported
        # lazily to avoid a circular import, and self-contained so it never
        # aborts construction. No-op on fresh installs / once recorded.
        from claude_swap.migrations import run_migrations

        run_migrations(self)

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

    # -- credential storage (delegates to CredentialStore) ----------------
    #
    # The active and per-account backup credential stores live in
    # ``CredentialStore`` (credentials.py). The methods below are thin delegators
    # kept so existing call sites (migrations, transfer, models, session, tests)
    # keep working unchanged. The store reads platform / _logger / credentials_dir
    # back off this switcher, but its sticky capability cache and last-active
    # backend live on the store — exposed here as proxy properties so callers that
    # poke them on the switcher (chiefly the test suite) still reach the real state.

    @property
    def _keychain_usable_cache(self) -> bool | None:
        return self._store._keychain_usable_cache

    @_keychain_usable_cache.setter
    def _keychain_usable_cache(self, value: bool | None) -> None:
        self._store._keychain_usable_cache = value

    @property
    def _keychain_disabled_until(self) -> float:
        return self._store._keychain_disabled_until

    @_keychain_disabled_until.setter
    def _keychain_disabled_until(self, value: float) -> None:
        self._store._keychain_disabled_until = value

    @property
    def _last_active_credentials_backend(self) -> str | None:
        return self._store._last_active_credentials_backend

    @_last_active_credentials_backend.setter
    def _last_active_credentials_backend(self, value: str | None) -> None:
        self._store._last_active_credentials_backend = value

    def _kc_call(self, fn, *args):
        return self._store._kc_call(fn, *args)

    def _use_keychain(self) -> bool:
        return self._store._use_keychain()

    def _read_credentials(self) -> str | None:
        return self._store._read_credentials()

    def _read_active_credentials(self) -> ActiveCredentials:
        return self._store._read_active_credentials()

    def _write_credentials(self, credentials: str) -> None:
        self._store._write_credentials(credentials)

    def _uses_file_backup_backend(self) -> bool:
        return self._store._uses_file_backup_backend()

    def _backup_enc_path(self, account_num: str, email: str) -> Path:
        return self._store._backup_enc_path(account_num, email)

    def _write_backup_enc(self, account_num: str, email: str, credentials: str) -> None:
        self._store._write_backup_enc(account_num, email, credentials)

    def _kc_read_backup(self, account_num: str, email: str) -> str:
        return self._store._kc_read_backup(account_num, email)

    def _kc_write_backup(self, account_num: str, email: str, credentials: str) -> None:
        self._store._kc_write_backup(account_num, email, credentials)

    def _delete_backup_keychain_quiet(self, account_num: str, email: str) -> None:
        self._store._delete_backup_keychain_quiet(account_num, email)

    def _post_backup_write(self, account_num: str, email: str) -> None:
        """Invalidate the slot's session profile after backup credentials change.

        Backup credentials changed (re-login via --add-account, --add-token,
        import, switch backing up, or a usage-refresh rotation): a session profile
        seeded from the old credentials may now hold a stale or rotated-out token
        that still passes the local reuse check. Drop the profile's credential
        material so the next `cswap run` re-bootstraps from this fresh backup
        (history is preserved). A LIVE session keeps its own copy untouched — claude
        manages it; pulling credentials out from under a running process would be
        worse than the drift caveat — but gets a stale marker so setup_session
        re-bootstraps it once it is no longer live.
        """
        if self._live_session_pids(account_num, email):
            from claude_swap.session import mark_session_stale

            mark_session_stale(self._session_dir(account_num, email))
        else:
            self._invalidate_session_credentials(account_num, email)

    def _read_account_credentials(self, account_num: str, email: str) -> str:
        return self._store._read_account_credentials(account_num, email)

    def _write_account_credentials(
        self, account_num: str, email: str, credentials: str
    ) -> None:
        """Write account credentials to backup, then invalidate the slot's session.

        The store performs the pure write and raises on failure *before* returning,
        so ``_post_backup_write`` (the session-invalidation chokepoint) runs exactly
        once and only after a successful write.
        """
        self._store._write_account_credentials(account_num, email, credentials)
        self._post_backup_write(account_num, email)

    def _delete_account_credentials(self, account_num: str, email: str) -> None:
        self._store._delete_account_credentials(account_num, email)

    def _delete_account_files(self, account_num: str, email: str) -> None:
        """Delete all backup files for an account (credentials + config).

        Single chokepoint for every path that removes or displaces a slot
        (remove_account, add_account/add_token slot overwrite & migration):
        refuses while a session-mode claude is live against the slot, and
        removes the slot's session profile alongside the backups so a stale
        profile can never outlive its account.

        Raises:
            SessionError: a live session-mode instance is using this account.
        """
        self._ensure_no_live_session(account_num, email, "the operation")
        self._delete_account_credentials(account_num, email)
        config_file = self.configs_dir / f".claude-config-{account_num}-{email}.json"
        if config_file.exists():
            config_file.unlink()
        self._delete_session_profile(account_num, email)

    def _prune_mappings(self, email: str, org_uuid: str) -> None:
        """Drop directory mappings for an identity that no longer has a slot.

        Called wherever an identity leaves the account table for good
        (remove_account, add_account/add_token slot overwrite). Slot
        *migration* and --import --force keep the (email, org) identity that
        mappings are keyed by, so they need no pruning.
        """
        from claude_swap.mappings import MappingStore

        pruned = MappingStore(self.backup_dir).prune_account(email, org_uuid or "")
        if pruned:
            print(dimmed(f"Removed {pruned} directory mapping(s) for this account"))

    def _read_account_config(self, account_num: str, email: str) -> str:
        """Read account config from backup."""
        config_file = self.configs_dir / f".claude-config-{account_num}-{email}.json"
        if config_file.exists():
            return config_file.read_text(encoding="utf-8")
        return ""

    def _account_is_switchable(self, account_num: str) -> bool:
        """Whether a slot has both stored credentials and config backups.

        Used by switch() and switch_to() to decide whether a target slot can
        be activated without re-adding the account. Tolerates stale sequence
        entries that reference a removed account record.
        """
        data = self._get_sequence_data() or {}
        record = data.get("accounts", {}).get(str(account_num))
        if not record:
            return False
        email = record.get("email", "")
        if not self._read_account_credentials(str(account_num), email):
            return False
        if not self._read_account_config(str(account_num), email):
            return False
        return True

    def _write_account_config(
        self, account_num: str, email: str, config: str
    ) -> None:
        """Write account config to backup."""
        config_file = self.configs_dir / f".claude-config-{account_num}-{email}.json"
        config_file.write_text(config, encoding="utf-8")
        if sys.platform != "win32":
            os.chmod(config_file, 0o600)

    # -- public accessors for session mode (claude_swap.session) ---------

    def resolve_account(self, identifier: str) -> tuple[str, str, str]:
        """Resolve NUM|EMAIL to (account_num, email, organizationUuid).

        Unlike switch_to/remove_account, ambiguity is a hard error rather
        than an interactive prompt: session mode ends in an exec, so callers
        need a deterministic resolution.

        Raises:
            AccountNotFoundError: identifier doesn't match any account.
            ConfigError: email matches multiple accounts.
        """
        self._get_sequence_data_migrated()
        account_num = self._resolve_account_identifier(identifier)
        if not account_num:
            raise AccountNotFoundError(
                f"No account found with identifier: {identifier}"
            )
        data = self._get_sequence_data() or {}
        record = data.get("accounts", {}).get(account_num)
        if not record:
            raise AccountNotFoundError(f"Account-{account_num} does not exist")
        return (
            account_num,
            record.get("email", ""),
            record.get("organizationUuid", "") or "",
        )

    def set_alias(self, identifier: str, alias: str) -> tuple[str, str]:
        """Set (or rename) the alias for the account matching identifier.

        ``identifier`` is a slot number, email, or existing alias (so a
        typo'd alias can be corrected with ``cswap alias <old> <new>`` as
        well as by number/email). Returns ``(account_num, normalized_alias)``.

        Raises:
            AccountNotFoundError: identifier doesn't match any account.
            ValidationError: alias format is invalid.
            ConfigError: the normalized alias is already used by another account.
        """
        try:
            normalized = normalize_alias(alias)
        except ValueError as e:
            raise ValidationError(str(e)) from e

        self._get_sequence_data_migrated()
        account_num = self._resolve_account_identifier(identifier)
        if not account_num:
            raise AccountNotFoundError(
                f"No account found with identifier: {identifier}"
            )
        data = self._get_sequence_data() or {}
        record = data.get("accounts", {}).get(account_num)
        if not record:
            raise AccountNotFoundError(f"Account-{account_num} does not exist")

        conflict = self._alias_in_use(normalized, exclude_num=account_num)
        if conflict is not None:
            raise ConfigError(f"Alias '{normalized}' is already used by account {conflict}")

        record["alias"] = normalized
        data["lastUpdated"] = get_timestamp()
        self._write_json(self.sequence_file, data)
        return account_num, normalized

    def unset_alias(self, identifier: str) -> str:
        """Clear the alias for the account matching identifier.

        Returns the account number. Idempotent: clearing an already-unset
        alias succeeds silently (no error), matching ``cswap config unset``'s
        posture of "the end state is what you asked for".

        Raises:
            AccountNotFoundError: identifier doesn't match any account.
        """
        self._get_sequence_data_migrated()
        account_num = self._resolve_account_identifier(identifier)
        if not account_num:
            raise AccountNotFoundError(
                f"No account found with identifier: {identifier}"
            )
        data = self._get_sequence_data() or {}
        record = data.get("accounts", {}).get(account_num)
        if not record:
            raise AccountNotFoundError(f"Account-{account_num} does not exist")

        if "alias" in record:
            del record["alias"]
            data["lastUpdated"] = get_timestamp()
            self._write_json(self.sequence_file, data)
        return account_num

    def list_aliases(self) -> list[tuple[str, str, str]]:
        """Every set alias as ``(account_num, alias, email)``, slot-number order."""
        data = self._get_sequence_data_migrated()
        accounts = (data or {}).get("accounts", {})
        rows = [
            (num, acc.get("alias"), acc.get("email", ""))
            for num, acc in accounts.items()
            if acc.get("alias")
        ]
        return sorted(rows, key=lambda r: int(r[0]))

    def slot_for_directory(self, directory: str | Path) -> tuple[str | None, str | None]:
        """Resolve a directory to its mapped account slot, for `cswap run`.

        Returns (slot, email): (None, None) when no mapping covers the
        directory, (None, email) when a mapping exists but its account was
        removed, and (slot, email) when the mapping resolves.
        """
        from claude_swap.mappings import MappingStore

        match = MappingStore(self.backup_dir).resolve(directory)
        if match is None:
            return None, None
        _, entry = match
        email = entry.get("email", "")
        seq = self._get_sequence_data_migrated() or {}
        slot = self._find_account_slot(
            seq, email, entry.get("organizationUuid", "") or ""
        )
        return slot, email

    def list_mappings(self) -> None:
        """Print all directory → account mappings (for `cswap map`)."""
        from claude_swap.mappings import MappingStore

        mappings = MappingStore(self.backup_dir).all()
        if not mappings:
            print(dimmed("No directory mappings yet."))
            print(muted("Map one with: cswap map <NUM|EMAIL> [PATH]"))
            return
        seq = self._get_sequence_data_migrated() or {}
        print(bolded("Directory mappings:"))
        for path in sorted(mappings):
            entry = mappings[path]
            email = entry.get("email", "")
            org_uuid = entry.get("organizationUuid", "") or ""
            slot = self._find_account_slot(seq, email, org_uuid)
            if slot:
                account = seq.get("accounts", {}).get(slot, {})
                tag = self._get_display_tag(
                    email, account.get("organizationName", ""), org_uuid
                )
                print(f"  {path} {dimmed('→')} {slot}: {email} {muted(f'[{tag}]')}")
            else:
                print(f"  {path} {dimmed('→')} {email} {muted('(account removed)')}")

    def read_account_credentials(self, account_num: str, email: str) -> str:
        """Public wrapper for session bootstrap. Empty string when missing."""
        return self._read_account_credentials(account_num, email)

    def write_account_credentials(
        self, account_num: str, email: str, credentials: str
    ) -> None:
        """Public wrapper for session bootstrap.

        Takes NO lock: the caller is expected to hold ``self.lock_file``
        already. Never combine with the locking persist callback in
        list_accounts() — FileLock is not re-entrant across instances in one
        process (see the v0.7.3 deadlock history).
        """
        self._write_account_credentials(account_num, email, credentials)

    def read_account_config(self, account_num: str, email: str) -> str:
        """Public wrapper for session bootstrap. Empty string when missing."""
        return self._read_account_config(account_num, email)

    # -- public accessors for the auto-switch engine -----------------------

    def usage_by_account(self) -> dict[str, dict | str | None]:
        """Public wrapper: account number → decision-grade usage value.

        Each value is a usage dict (last-good, trusted while ≤
        ``usage_store.STALE_OK_S`` old), a sentinel string, or ``None``
        (unknown).
        """
        return self._usage_by_account()

    def usage_entries_by_account(
        self, fetch: set[str] | None = None
    ) -> dict[str, UsageEntry]:
        """Store-backed usage entries (ages, errors, poll state) per account.

        ``fetch`` restricts which accounts *may* be fetched this pass (the
        auto engine's scheduler); ``None`` means every stale account is
        eligible (on-demand callers).
        """
        accounts_info = self._build_accounts_info()
        return self._collect_usage_entries(accounts_info, fetch=fetch)

    def accounts_snapshot(self, fetch: set[str] | None = None) -> AccountsSnapshot:
        """One-pass structured snapshot of every managed account, for the TUI.

        Metadata, active-slot detection, and usage entries all come from a
        single ``_build_accounts_info`` + ``_collect_usage_entries`` pass, so
        the view is coherent — two separate calls could interleave with other
        collectors and disagree about the active slot or freshness. ``fetch``
        has ``_collect_usage_entries`` semantics: ``None`` makes every stale
        account eligible; a set restricts which accounts *may* be fetched
        this pass.
        """
        accounts_info = self._build_accounts_info()
        entries = self._collect_usage_entries(accounts_info, fetch=fetch)
        seq_data = self._get_sequence_data() or {}
        active_number: str | None = None
        accounts: list[AccountSnapshot] = []
        for num, email, org_name, org_uuid, is_active, _creds, alias in accounts_info:
            n = str(num)
            if is_active:
                active_number = n
            accounts.append(
                AccountSnapshot(
                    number=n,
                    email=email,
                    org_name=org_name,
                    org_uuid=org_uuid,
                    is_active=is_active,
                    kind=self._account_kind(n),
                    switchable=self._account_is_switchable(n),
                    usage=entries[n],
                    alias=alias,
                    disabled=self._disabled_from_data(seq_data, n),
                )
            )
        return AccountsSnapshot(
            active_number=active_number,
            accounts=tuple(accounts),
            taken_at=self._usage_store.clock(),
        )

    def usage_fetch_stamps(self) -> dict[str, float | None]:
        """Per-slot ``fetchedAt`` snapshot from the usage store — a pure file
        read (no fetching, no credential access). The TUI watch view diffs
        consecutive snapshots to flash rows whose usage just refreshed.
        """
        data = self._get_sequence_data() or {}
        identities = {
            num: (info.get("email", ""), info.get("organizationUuid", "") or "")
            for num, info in data.get("accounts", {}).items()
        }
        return {
            num: entry.fetched_at
            for num, entry in self._usage_store.entries(identities).items()
        }

    def set_poll_policy_inputs(
        self, threshold: float, models: tuple[str, ...]
    ) -> None:
        """Pin the threshold/models poll planning keys on (set by a hosted
        auto engine so cadence follows its effective, CLI-merged settings
        instead of the settings file)."""
        self._poll_inputs_override = (threshold, models)

    def clear_poll_policy_inputs(self) -> None:
        """Drop the hosted engine's pin so poll planning falls back to the
        settings file — called when the engine's screen closes, or a TUI
        session threshold override would keep steering cadence after the
        engine it belonged to is gone."""
        self._poll_inputs_override = None

    def _poll_policy_inputs(self) -> tuple[float, tuple[str, ...]]:
        """Threshold + configured model names for poll planning: the hosting
        engine's pinned values when present, else the settings file (reloaded
        only when it changes — one stat per pass)."""
        if self._poll_inputs_override is not None:
            return self._poll_inputs_override
        path = settings_path(self.backup_dir)
        try:
            mtime: float | None = path.stat().st_mtime
        except OSError:
            mtime = None
        if self._poll_inputs_cache is not None and self._poll_inputs_cache[0] == mtime:
            return self._poll_inputs_cache[1]
        loaded = load_settings(self.backup_dir)
        inputs = (loaded.threshold, parse_model_names(loaded.model))
        self._poll_inputs_cache = (mtime, inputs)
        return inputs

    def switchable_account_numbers(self) -> list[str]:
        """Account numbers in rotation order eligible for automatic selection.

        Excludes slots without usable stored backups and slots the user has
        disabled (``cswap disable``). Disabled slots stay managed and remain
        valid explicit ``cswap switch <num|email>`` targets — they are only
        held out of automatic rotation and the usage-aware strategies.
        """
        data = self._get_sequence_data() or {}
        return [
            str(num)
            for num in data.get("sequence", [])
            if self._account_is_switchable(str(num))
            and not self._disabled_from_data(data, str(num))
        ]

    @staticmethod
    def _disabled_from_data(data: dict, account_num: str) -> bool:
        """Whether a slot is flagged out of rotation in already-loaded data."""
        record = data.get("accounts", {}).get(str(account_num))
        return bool(record and record.get("disabled"))

    def is_account_disabled(self, account_num: str) -> bool:
        """Whether a slot is currently held out of rotation."""
        data = self._get_sequence_data() or {}
        return self._disabled_from_data(data, str(account_num))

    def disabled_account_numbers(self) -> list[str]:
        """Managed slots the user has disabled, in sequence order."""
        data = self._get_sequence_data() or {}
        return [
            str(num)
            for num in data.get("sequence", [])
            if self._disabled_from_data(data, str(num))
        ]

    def set_account_disabled(self, identifier: str, disabled: bool) -> None:
        """Hold an account out of rotation (``disabled=True``) or return it.

        Disabling only affects automatic selection — the auto-switch engine,
        bare ``cswap switch`` rotation, and the ``best`` / ``next-available``
        strategies all skip disabled slots. The account stays managed and is
        still a valid explicit ``cswap switch <num|email>`` target, so you can
        park an account without losing its stored login. Re-enabling restores
        it to rotation in its original sequence position.

        Raises:
            ConfigError: no accounts are managed yet, or the email is ambiguous.
            AccountNotFoundError: identifier doesn't match any account.
        """
        if not self.sequence_file.exists():
            raise ConfigError("No accounts are managed yet")

        # resolve_account migrates org fields and hard-errors on ambiguity.
        account_num, email, _ = self.resolve_account(identifier)

        data = self._get_sequence_data() or {}
        record = data.get("accounts", {}).get(account_num)
        if not record:
            raise AccountNotFoundError(f"Account-{account_num} does not exist")

        verb = "disabled" if disabled else "enabled"
        if bool(record.get("disabled")) == disabled:
            print(dimmed(f"Account-{account_num} ({email}) is already {verb}."))
            return

        if disabled:
            record["disabled"] = True
        else:
            record.pop("disabled", None)
        data["lastUpdated"] = get_timestamp()
        self._write_json(self.sequence_file, data)
        self._logger.info(f"{verb.capitalize()} account {account_num}: {email}")

        print(f"{accent(verb.capitalize())} Account-{account_num} ({email}).")

        if disabled:
            active = data.get("activeAccountNumber")
            if str(active) == account_num:
                print(dimmed(
                    "  It is the active account — it stays live until you switch "
                    "away; it just won't be an automatic switch target."
                ))
            if not self.switchable_account_numbers():
                warning(
                    "  No accounts remain in rotation — auto-switch and bare "
                    "switch have nothing to pick. Re-enable one with "
                    "cswap enable <num|email>."
                )
        else:
            print(dimmed("  It is back in the rotation."))

    def account_kind_for(self, account_num: str) -> str:
        """Public wrapper: ``"api_key"`` or ``"oauth"`` (setup-tokens read as oauth)."""
        return self._account_kind(account_num)

    def account_email(self, account_num: str) -> str:
        """Stored email for a slot; empty string when unknown."""
        data = self._get_sequence_data() or {}
        return data.get("accounts", {}).get(str(account_num), {}).get("email", "")

    def current_account_number(self) -> str | None:
        """Slot of the live login; ``None`` when there is none or it's unmanaged.

        Deliberately no fallback to the recorded ``activeAccountNumber``: an
        unmanaged live login must return ``None`` — never a guessed slot — so
        the auto-switch engine can't evaluate the wrong account's usage and
        overwrite a login cswap doesn't own (``_perform_switch`` would take
        the no-backup direct-activation path). Use :meth:`has_live_login` to
        tell the two ``None`` cases apart.
        """
        identity = self._get_current_account()
        if identity is None:
            return None
        data = self._get_sequence_data() or {}
        email, org_uuid = identity
        return self._find_account_slot(data, email, org_uuid)

    def has_live_login(self) -> bool:
        """Whether ``~/.claude.json`` carries any live account identity."""
        return self._get_current_account() is not None

    def live_session_pids_for(self, account_num: str, email: str) -> list[int]:
        """Public wrapper: PIDs of live ``cswap run`` sessions for a slot."""
        return self._live_session_pids(account_num, email)

    def persist_backup_credentials(
        self, account_num: str, email: str, credentials: str
    ) -> None:
        """Persist rotated credentials to a slot's backup store, under the lock.

        For inactive accounts only — never routes to the active store. Mirrors
        the persist callback ``_fetch_account_usage`` uses. The caller must NOT
        hold ``self.lock_file`` (FileLock is non-reentrant).
        """
        with FileLock(self.lock_file):
            self._write_account_credentials(account_num, email, credentials)

    def account_identity(self, account_num: str) -> dict:
        """Stored identity for a slot: ``{"email", "organizationUuid", "uuid"}``."""
        data = self._get_sequence_data() or {}
        acct = data.get("accounts", {}).get(str(account_num), {})
        return {
            "email": acct.get("email", ""),
            "organizationUuid": acct.get("organizationUuid", "") or "",
            "uuid": (acct.get("uuid") or "").strip(),
        }

    def backfill_account_uuid(self, account_num: str, uuid: str) -> None:
        """Record a resolved account uuid on a slot that lacks one.

        Only ever fills an empty uuid (add-token placeholders) — an existing
        uuid is identity and is never rewritten here. Caller must NOT hold
        ``self.lock_file``.
        """
        if not uuid:
            return
        with FileLock(self.lock_file):
            data = self._get_sequence_data() or {}
            acct = data.get("accounts", {}).get(str(account_num))
            if acct is not None and not (acct.get("uuid") or "").strip():
                acct["uuid"] = uuid
                data["lastUpdated"] = get_timestamp()
                self._write_json(self.sequence_file, data)

    def list_unclaimed_credentials(self) -> dict[str, dict]:
        """Internal safety copies preserved at switch time (diagnostics only).

        Write-only storage: entries are created when a switch displaces live
        credential bytes it could not attribute to the outgoing slot, and are
        never consumed automatically — recovery from any such state is the
        documented ``/login`` + ``cswap add [--slot N]``.
        """
        return self._store._list_unclaimed_credentials()

    # -- session profile lifecycle ----------------------------------------

    def _session_dir(self, account_num: str, email: str) -> Path:
        from claude_swap.session import session_dir_for

        return session_dir_for(self.backup_dir, account_num, email)

    def _live_session_pids(self, account_num: str, email: str) -> list[int]:
        """PIDs of Claude instances running against an account's session profile."""
        from claude_swap.session import live_sessions_for

        return [s.pid for s in live_sessions_for(self._session_dir(account_num, email))]

    def _ensure_no_live_session(self, account_num: str, email: str, action: str) -> None:
        """Refuse a destructive operation while a session-mode claude is live."""
        pids = self._live_session_pids(account_num, email)
        if pids:
            raise SessionError(
                f"Account-{account_num} ({email}) has a live session-mode Claude "
                f"instance (PID {', '.join(map(str, pids))}). "
                f"Exit it first, then retry {action}."
            )

    def _invalidate_session_credentials(self, account_num: str, email: str) -> None:
        """Drop a session profile's credential material, keeping its history.

        The next `cswap run` fails the reuse check and re-bootstraps from
        backup; the bootstrap merges .claude.json, so the profile's own
        projects/history survive. Used when backup credentials change under
        an existing profile (e.g. --import --force).
        """
        from claude_swap.session import STALE_MARKER, delete_macos_keychain_entry

        session_dir = self._session_dir(account_num, email)
        if not session_dir.exists():
            return
        delete_macos_keychain_entry(session_dir)
        (session_dir / ".credentials.json").unlink(missing_ok=True)
        (session_dir / STALE_MARKER).unlink(missing_ok=True)
        self._logger.info(
            f"Invalidated session credentials for account {account_num}"
        )

    def _delete_session_profile(self, account_num: str, email: str) -> None:
        """Remove an account's session profile dir and its keychain entry.

        Keychain first: the hashed service name is derived from the dir path
        and can't be recomputed once the dir is gone.
        """
        from claude_swap.session import delete_macos_keychain_entry

        session_dir = self._session_dir(account_num, email)
        if not session_dir.exists():
            return
        delete_macos_keychain_entry(session_dir)
        shutil.rmtree(session_dir, ignore_errors=True)
        self._logger.info(
            f"Removed session profile for account {account_num} at {session_dir}"
        )

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

    @staticmethod
    def _find_account_slot(
        data: dict, email: str, organization_uuid: str
    ) -> str | None:
        """Return the slot key for the account matching (email, organizationUuid), else None."""
        for num, account in data.get("accounts", {}).items():
            if (account.get("email") == email and
                    account.get("organizationUuid", "") == organization_uuid):
                return num
        return None

    def _account_exists(self, email: str, organization_uuid: str) -> bool:
        """Check if account exists by (email, organizationUuid) composite key."""
        data = self._get_sequence_data()
        if not data:
            return False
        return self._find_account_slot(data, email, organization_uuid) is not None

    def _account_kind(self, account_num: str | None) -> str:
        """Stored kind for a managed slot: ``"api_key"`` or ``"oauth"`` (default).

        Slots added before this field existed have no ``kind`` and read as
        ``"oauth"`` (back-compat).
        """
        if account_num is None:
            return "oauth"
        data = self._get_sequence_data() or {}
        record = data.get("accounts", {}).get(str(account_num), {})
        return "api_key" if record.get("kind") == "api_key" else "oauth"

    def _reject_live_api_key_capture(self, creds: str) -> None:
        """Guard for ``add_account``: never capture a live managed key as OAuth.

        ``add_account`` snapshots the *live* active credential under an
        ``oauthAccount`` identity. Now that ``_read_credentials`` can return a raw
        ``sk-ant-api…`` key, a live ``/login`` key could be backed up as a kindless
        account, corrupting the session-guard / export / collision logic that keys
        off ``kind``. Reject with guidance toward the supported path instead.
        """
        if looks_like_api_key(creds):
            raise ValidationError(
                "Active login is an API-key account. Add it with "
                "'cswap --add-token sk-ant-api...' instead of --add-account."
            )

    def _reject_cross_kind_collision(self, email: str, is_api_key: bool) -> None:
        """Reject registering a token whose (email, personal-org) already exists as
        the *other* kind.

        Identity is matched on ``(email, organizationUuid)`` only, so two slots
        sharing an email across kinds (one OAuth, one API key) could not be told
        apart at switch time. Rather than thread ``kind`` through the whole identity
        system, refuse the collision and point the user at a distinct ``--email``.
        The default ``…@token.local`` labels never collide; this only guards a forced
        ``--email``.
        """
        data = self._get_sequence_data()
        if not data:
            return
        slot = self._find_account_slot(data, email, "")
        if slot is None:
            return
        existing_kind = self._account_kind(slot)
        new_kind = "api_key" if is_api_key else "oauth"
        if existing_kind != new_kind:
            existing_label = "API-key" if existing_kind == "api_key" else "OAuth"
            new_label = "API-key" if is_api_key else "OAuth"
            raise ValidationError(
                f"'{email}' already exists as an {existing_label} account "
                f"(slot {slot}); cannot add it as an {new_label} account. "
                f"Pass a distinct --email."
            )

    @staticmethod
    def _get_display_tag(email: str, org_name: str, org_uuid: str) -> str:
        """Return display tag for an account's org context."""
        return org_name if org_name else "personal"

    def _find_account_by_alias(self, alias: str) -> str | None:
        """Return the account number whose alias matches (case-insensitive), if any.

        An empty ``alias`` never matches: accounts without one store no
        ``alias`` key, and comparing against an empty string would otherwise
        match the first aliasless account.
        """
        if not alias:
            return None
        data = self._get_sequence_data()
        if not data:
            return None
        alias_key = alias.lower()
        for num, account in data.get("accounts", {}).items():
            if (account.get("alias") or "").lower() == alias_key:
                return num
        return None

    def _alias_in_use(self, alias: str, *, exclude_num: str | None = None) -> str | None:
        """Return the account number already using ``alias`` (other than ``exclude_num``), if any."""
        num = self._find_account_by_alias(alias)
        if num is not None and num == exclude_num:
            return None
        return num

    def _resolve_account_identifier(self, identifier: str) -> str | None:
        """Resolve account identifier (number, alias, or email) to account number.

        Resolution precedence: number -> alias -> email.

        Raises:
            ConfigError: if the email matches multiple accounts (ambiguous).
        """
        if identifier.isdigit():
            return identifier

        data = self._get_sequence_data()
        if not data:
            return None

        alias_match = self._find_account_by_alias(identifier)
        if alias_match is not None:
            return alias_match

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

    def add_account(
        self,
        slot: int | None = None,
        assume_yes: bool = False,
        alias: str | None = None,
    ) -> None:
        """Add current account to managed accounts.

        Args:
            slot: Specify the slot number to store the account in.
                  When None, auto-assigns the next available number.
                  When specified, prompts for confirmation if the slot
                  is already occupied by a different account.
            assume_yes: Skip that overwrite prompt (callers with their own
                  confirmation UI, e.g. the TUI, confirm before calling).
            alias: Optional short display alias to set on this account.
                  When omitted, an existing alias on the slot is preserved.
        """
        self._setup_directories()
        self._init_sequence_file()
        self._migrate_org_fields()

        if alias is not None:
            try:
                alias = normalize_alias(alias)
            except ValueError as e:
                raise ValidationError(str(e)) from e

        identity = self._get_current_account()
        if identity is None:
            raise ConfigError("No active Claude account found. Please log in first.")
        current_email, current_org_uuid = identity

        # When no slot specified and account already exists, refresh credentials in place
        if slot is None and self._account_exists(current_email, current_org_uuid):
            seq = self._get_sequence_data()
            account_num = self._find_account_slot(seq, current_email, current_org_uuid)
            matched_org_name = seq["accounts"][account_num].get("organizationName", "") if account_num else ""

            if alias is not None:
                conflict = self._alias_in_use(alias, exclude_num=account_num)
                if conflict is not None:
                    raise ValidationError(
                        f"Alias '{alias}' is already used by account {conflict}"
                    )

            current_creds = self._read_credentials()
            if current_creds is None:
                raise CredentialReadError("Failed to read credentials for current account")
            if not current_creds:
                raise CredentialReadError("No credentials found for current account")
            self._reject_live_api_key_capture(current_creds)

            config_path = self._get_claude_config_path()
            try:
                current_config = config_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                raise ConfigError("Claude config file not found")
            except PermissionError:
                raise ConfigError("Permission denied reading Claude config")

            self._write_account_credentials(account_num, current_email, current_creds)
            self._write_account_config(account_num, current_email, current_config)
            self._usage_store.clear_dead_token(
                [account_num], {account_num: (current_email, current_org_uuid)}
            )

            if alias is not None:
                seq["accounts"][account_num]["alias"] = alias

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
                old_num = self._find_account_slot(
                    data, current_email, current_org_uuid
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
                    if not assume_yes:
                        try:
                            answer = input(f"Overwrite slot {slot}? [y/N] ").strip().lower()
                        except (EOFError, KeyboardInterrupt):
                            print(f"\n{dimmed('Cancelled')}")
                            return
                        if answer not in ("y", "yes"):
                            print(dimmed("Cancelled"))
                            return
                    displace_slot = (
                        account_num,
                        existing_email,
                        existing.get("organizationUuid", "") or "",
                    )
        else:
            account_num = str(self._get_next_account_number())

        # Capture any alias to carry forward before destructive cleanup below
        # deletes the old record (same account moving slots, or refreshing in place).
        existing_alias = None
        if slot is not None:
            prior = data.get("accounts", {}).get(account_num) or {}
            if (
                prior.get("email") == current_email
                and prior.get("organizationUuid", "") == current_org_uuid
            ):
                existing_alias = prior.get("alias")
            if migrate_from:
                existing_alias = data["accounts"][migrate_from].get("alias") or existing_alias

        if alias is not None:
            conflict = self._alias_in_use(alias, exclude_num=account_num)
            if conflict is not None:
                raise ValidationError(
                    f"Alias '{alias}' is already used by account {conflict}"
                )

        # Read new account credentials BEFORE any destructive operations
        current_creds = self._read_credentials()
        if current_creds is None:
            raise CredentialReadError("Failed to read credentials for current account")
        if not current_creds:
            raise CredentialReadError("No credentials found for current account")
        self._reject_live_api_key_capture(current_creds)

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
            d_num, d_email, d_org = displace_slot
            self._delete_account_files(d_num, d_email)
            data = self._get_sequence_data()
            if int(d_num) in data["sequence"]:
                data["sequence"].remove(int(d_num))
            del data["accounts"][d_num]
            self._write_json(self.sequence_file, data)
            self._prune_mappings(d_email, d_org)

        if migrate_from:
            data = self._get_sequence_data()
            old_email = data["accounts"][migrate_from].get("email", "")
            self._delete_account_files(migrate_from, old_email)
            if int(migrate_from) in data["sequence"]:
                data["sequence"].remove(int(migrate_from))
            del data["accounts"][migrate_from]
            self._write_json(self.sequence_file, data)

        # Store backups
        self._write_account_credentials(account_num, current_email, current_creds)
        self._write_account_config(account_num, current_email, current_config)
        self._usage_store.clear_dead_token(
            [account_num], {account_num: (current_email, organization_uuid)}
        )

        # Update sequence.json
        data = self._get_sequence_data()
        data["accounts"][account_num] = {
            "email": current_email,
            "uuid": account_uuid,
            "organizationUuid": organization_uuid,
            "organizationName": organization_name,
            "added": get_timestamp(),
        }
        carried_alias = alias if alias is not None else existing_alias
        if carried_alias:
            data["accounts"][account_num]["alias"] = carried_alias
        if int(account_num) not in data["sequence"]:
            data["sequence"].append(int(account_num))
            data["sequence"].sort()
        data["activeAccountNumber"] = int(account_num)
        data["lastUpdated"] = get_timestamp()

        self._write_json(self.sequence_file, data)
        tag = self._get_display_tag(current_email, organization_name, organization_uuid)
        self._logger.info(f"Added account {account_num}: {current_email} (org: {organization_uuid or 'personal'})")
        if migrate_from:
            print(f"{dimmed(f'Moved from slot {migrate_from} → {slot}')}")
        print(f"{accent('Added')} Account {account_num}: {current_email} {muted(f'[{tag}]')}")

    def add_account_from_token(
        self,
        token: str,
        email: str | None = None,
        slot: int | None = None,
        assume_yes: bool = False,
    ) -> None:
        """Register a raw OAuth setup-token or managed API key as a new account.

        Useful for headless servers or when the token is received from another
        machine, without needing a prior Claude Code login on this machine. The
        token type is auto-detected: an ``sk-ant-api…`` value is a managed API key
        (stored raw, activated on Claude Code's API-key auth axis), anything else is
        treated as an OAuth setup-token. No Anthropic API calls are made.

        Args:
            token: Raw OAuth setup-token or ``sk-ant-api…`` key, or ``"-"`` to read
                   one line from stdin, or ``""`` to prompt securely via getpass.
            email: Email address to associate with the account. When omitted,
                   defaults to ``setup-token-{slot}@token.local`` (or
                   ``api-key-{slot}@token.local`` for API keys) since these tokens
                   carry no real email metadata.
            slot:  Slot number to use; auto-assigned when ``None``.
            assume_yes: Skip the occupied-slot overwrite prompt (callers with
                   their own confirmation UI, e.g. the TUI, confirm first).
        """
        import getpass

        if token == "-":
            token = sys.stdin.readline().rstrip("\n")
        elif not token:
            token = getpass.getpass("Token: ")

        token = token.strip()
        if not token:
            raise ValidationError("Token cannot be empty")

        is_api_key = looks_like_api_key(token)

        if email and not self._validate_email(email):
            raise ValidationError(f"Invalid email format: {email}")

        self._setup_directories()
        self._init_sequence_file()
        self._migrate_org_fields()

        # Synthesize a placeholder email when one isn't provided. These tokens
        # have no real email metadata, so requiring users to invent one is
        # noise; the slot number gives every default account a unique key.
        if not email:
            if slot is None:
                slot = self._get_next_account_number()
            label = "api-key" if is_api_key else "setup-token"
            email = f"{label}-{slot}@token.local"

        # Don't silently overwrite/convert an existing account of the other kind:
        # identity is matched on (email, org) only, so an api-key and an OAuth
        # account sharing an email would be indistinguishable at switch time.
        self._reject_cross_kind_collision(email, is_api_key)

        # Build the credential payload by kind: a managed key is stored raw; an
        # OAuth setup-token is wrapped in Claude Code's credential JSON. The
        # synthesized config is identical for both (no real org metadata).
        if is_api_key:
            credentials = token
        else:
            credentials = json.dumps({
                "claudeAiOauth": {
                    "accessToken": token,
                    "scopes": list(SETUP_TOKEN_SCOPES),
                }
            })
        config = json.dumps({
            "oauthAccount": {
                "emailAddress": email,
                "accountUuid": "",
                "organizationUuid": None,
                "organizationName": None,
            }
        })

        # If the account already exists (same email, personal), refresh in place.
        if slot is None and self._account_exists(email, ""):
            seq = self._get_sequence_data()
            account_num = self._find_account_slot(seq, email, "")
            if account_num is None:
                raise ConfigError(
                    f"Existing account metadata for {email} is inconsistent"
                )
            self._write_account_credentials(account_num, email, credentials)
            self._write_account_config(account_num, email, config)
            # A refreshed credential invalidates any dead-token quarantine on this
            # slot (mirrors ``add_account``); otherwise the stale strike row keeps
            # the account stuck at "re-login needed" and it never fetches the new
            # token. Token accounts are always personal, so org is "".
            self._usage_store.clear_dead_token(
                [account_num], {account_num: (email, "")}
            )
            seq["lastUpdated"] = get_timestamp()
            self._write_json(self.sequence_file, seq)
            kind_label = "API key" if is_api_key else "token"
            self._logger.info(f"Updated {kind_label} for account {account_num}: {email}")
            print(
                f"{accent(f'Updated {kind_label}')} for Account {account_num} "
                f"({email} {muted('[personal]')})."
            )
            return

        displace_slot = None
        migrate_from = None

        if slot is not None:
            if slot < 1:
                raise ConfigError("Slot number must be >= 1")
            account_num = str(slot)
            data = self._get_sequence_data()

            if self._account_exists(email, ""):
                old_num = self._find_account_slot(data, email, "")
                if old_num and old_num != account_num:
                    migrate_from = old_num

            if account_num in data.get("accounts", {}):
                existing = data["accounts"][account_num]
                existing_email = existing.get("email", "unknown")
                is_same = (
                    existing_email == email
                    and existing.get("organizationUuid", "") == ""
                )
                if not is_same:
                    existing_tag = self._get_display_tag(
                        existing_email,
                        existing.get("organizationName", ""),
                        existing.get("organizationUuid", ""),
                    )
                    warning(f"Slot {slot} already occupied")
                    print(f"{existing_email} {muted(f'[{existing_tag}]')}")
                    if not assume_yes:
                        try:
                            answer = input(f"Overwrite slot {slot}? [y/N] ").strip().lower()
                        except (EOFError, KeyboardInterrupt):
                            print(f"\n{dimmed('Cancelled')}")
                            return
                        if answer not in ("y", "yes"):
                            print(dimmed("Cancelled"))
                            return
                    displace_slot = (
                        account_num,
                        existing_email,
                        existing.get("organizationUuid", "") or "",
                    )
        else:
            account_num = str(self._get_next_account_number())

        if displace_slot:
            d_num, d_email, d_org = displace_slot
            self._delete_account_files(d_num, d_email)
            data = self._get_sequence_data()
            if int(d_num) in data["sequence"]:
                data["sequence"].remove(int(d_num))
            del data["accounts"][d_num]
            self._write_json(self.sequence_file, data)
            self._prune_mappings(d_email, d_org)

        if migrate_from:
            data = self._get_sequence_data()
            old_email = data["accounts"][migrate_from].get("email", "")
            self._delete_account_files(migrate_from, old_email)
            if int(migrate_from) in data["sequence"]:
                data["sequence"].remove(int(migrate_from))
            del data["accounts"][migrate_from]
            self._write_json(self.sequence_file, data)

        self._write_account_credentials(account_num, email, credentials)
        self._write_account_config(account_num, email, config)
        # Reusing/overwriting a slot with a fresh credential lifts any dead-token
        # quarantine carried by that slot's prior lineage (mirrors ``add_account``).
        self._usage_store.clear_dead_token(
            [account_num], {account_num: (email, "")}
        )

        data = self._get_sequence_data()
        record = {
            "email": email,
            "uuid": "",
            "organizationUuid": "",
            "organizationName": "",
            "added": get_timestamp(),
        }
        if is_api_key:
            record["kind"] = "api_key"
        data["accounts"][account_num] = record
        if int(account_num) not in data["sequence"]:
            data["sequence"].append(int(account_num))
            data["sequence"].sort()
        data["lastUpdated"] = get_timestamp()

        self._write_json(self.sequence_file, data)
        source_label = "API key" if is_api_key else "token"
        self._logger.info(f"Added account {account_num} from {source_label}: {email}")
        if migrate_from:
            print(f"{dimmed(f'Moved from slot {migrate_from} → {slot}')}")
        print(
            f"{accent('Added')} Account {account_num}: {email} "
            f"{muted('[personal]')} {muted(f'(from {source_label})')}"
        )

    def remove_account(self, identifier: str, assume_yes: bool = False) -> None:
        """Remove account from managed accounts.

        When ``assume_yes`` is True the confirmation prompt is skipped (used by
        the TUI, which collects confirmation before calling).
        """
        if not self.sequence_file.exists():
            raise ConfigError("No accounts are managed yet")

        # Ensure org fields are migrated before resolving accounts
        self._get_sequence_data_migrated()

        # Resolve identifier
        if not identifier.isdigit():
            is_alias = self._find_account_by_alias(identifier) is not None
            if not is_alias and not self._validate_email(identifier):
                raise ValidationError(f"Invalid account identifier: {identifier}")

            # For email identifiers, handle ambiguous matches interactively.
            # Aliases are unique by construction, so they never hit this.
            if not is_alias:
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

        # Check before the confirmation prompt (better UX); the chokepoint in
        # _delete_account_files re-checks as a safety net for all paths.
        self._ensure_no_live_session(account_num, email, "--remove-account")

        if str(active_account) == account_num:
            warning(f"Warning: Account-{account_num} ({email}) is currently active")

        if not assume_yes:
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

        self._prune_mappings(email, account_info.get("organizationUuid", ""))

    def _build_accounts_info(self) -> list[tuple[int, str, str, str, bool, str, str]]:
        """Build per-account (num, email, org_name, org_uuid, is_active, creds, alias).

        Shared by list_accounts and the usage-aware switch helpers so the active
        slot is detected and credentials are read in exactly one place. The
        active account's credentials come from Claude Code's live store; every
        other slot reads its backup copy.
        """
        data = self._get_sequence_data_migrated() or {}
        current_identity = self._get_current_account()

        # Find active account number by (email, organizationUuid) composite key
        active_num = None
        if current_identity is not None:
            current_email, current_org_uuid = current_identity
            active_num = self._find_account_slot(data, current_email, current_org_uuid)

        accounts_info: list[tuple[int, str, str, str, bool, str, str]] = []
        # Reset each build; set below only when the active slot's OAuth Keychain
        # read failed with no fallback. Read by _static_usage_sentinel (main
        # thread writes it here before the fetch pool starts → no data race).
        self._active_keychain_unavailable = False
        for num in data.get("sequence", []):
            account = data.get("accounts", {}).get(str(num), {})
            email = account.get("email", "unknown")
            org_name = account.get("organizationName", "") or ""
            org_uuid = account.get("organizationUuid", "") or ""
            alias = account.get("alias", "") or ""
            is_active = str(num) == active_num

            if is_active:
                active = self._read_active_credentials()
                creds = active.value or ""
                self._active_keychain_unavailable = active.keychain_unavailable
            else:
                creds = self._read_account_credentials(str(num), email)

            accounts_info.append((num, email, org_name, org_uuid, is_active, creds, alias))
        return accounts_info

    def _active_cc_running(self) -> bool:
        """Whether any default-profile Claude Code instance is running.

        Fails closed: if instance detection raises, assume an owner may exist so we
        never refresh the live credential out from under a running Claude Code.
        """
        try:
            sessions, ides = get_running_instances()
            return bool(sessions or ides)
        except Exception:
            self._logger.debug("Failed to detect running Claude instances", exc_info=True)
            return True

    def _fetch_active_usage(
        self, account_num: str, email: str, creds: str
    ) -> FetchRecord:
        """Usage fetch for the active/default account, refreshing its token only
        when safe.

        The active credential is the one Claude Code concurrently owns, so cswap
        normally leaves it alone (issue #62). But when no *owner* is detected —
        neither a default-profile Claude Code (``_active_cc_running``) nor a live
        ``cswap run`` session for this same account (``_live_session_pids``) — there
        is no concurrent refresher, so an expired token can be refreshed and written
        back to the **active** store (Claude Code reads the rotated credential on its
        next start).

        When an owner *is* present and the token is expired, returns the
        ``USAGE_TOKEN_EXPIRED`` sentinel so the UI shows an intentional line rather
        than a bare "usage unavailable".
        """
        oauth_data = oauth.extract_oauth_data(creds)
        if not oauth_data or not oauth_data.get("accessToken"):
            return FetchRecord(sentinel=USAGE_NO_CREDENTIALS)

        owned = self._active_cc_running() or bool(
            self._live_session_pids(account_num, email)
        )

        # Provenance guard (issue #117): the no-owner path below rotates the
        # live credential and writes it into this slot's backup — the same
        # config-chose-the-slot / bytes-came-from-the-store split the switch
        # guard closes. Only a lineage match against the slot's stored backup
        # proves the live bytes are actually this slot's. On mismatch, don't
        # consume a generation of a credential we can't attribute: read usage
        # with the token as-is and leave reconciliation to the switch-time
        # guard (which can resolve identity, or back up pre-fix style).
        unattributed = False
        if not owned:
            backup = self._read_account_credentials(account_num, email)
            unattributed = creds != backup and (
                oauth.credential_fingerprint(creds)
                != oauth.credential_fingerprint(backup)
            )
            if unattributed:
                self._logger.warning(
                    "Active credential does not match Account-%s's stored "
                    "backup; skipping its refresh (provenance unknown).",
                    account_num,
                )

        if owned or unattributed:
            if oauth.is_oauth_token_expired(oauth_data.get("expiresAt")):
                # The request would just 401 (an owned credential may not be
                # refreshed), so skip it — Claude Code's own /usage does the
                # same on a locally-expired token.
                return FetchRecord(sentinel=USAGE_TOKEN_EXPIRED)
            outcome = oauth.try_fetch_usage_for_account(
                account_num, email, creds, is_active=True,
            )
            if outcome.usage is None and oauth.is_oauth_token_expired(
                oauth_data.get("expiresAt")
            ):
                return FetchRecord(sentinel=USAGE_TOKEN_EXPIRED)
            return FetchRecord(
                usage=outcome.usage,
                error=outcome.error,
                retry_after_s=outcome.retry_after_s,
            )

        # No owner detected → safe to refresh the active token. Reuse the inactive
        # refresh machinery (proactive refresh + 401 retry), persisting the rotated
        # credential to BOTH the active store and the backup. Do NOT hold the lock
        # across the network refresh: FileLock is non-reentrant and persist_active
        # re-acquires it (regressing commit a07c767 would deadlock and silently drop
        # the refreshed token).
        original_refresh = oauth_data.get("refreshToken")
        persist_skipped = False

        def persist_active(num: str, acct_email: str, new_creds: str) -> None:
            nonlocal persist_skipped
            # Failing to acquire any lock means the rotated credential was NOT
            # persisted — mark it skipped (never show usage for it) before the
            # error propagates to oauth._persist's warning path. The Claude
            # Code locks matter here too: _write_credentials touches the
            # active store and (via _clear_managed_key) possibly ~/.claude.json,
            # and holding them closes the owner-check-to-write gap.
            try:
                with (
                    FileLock(self.lock_file),
                    claude_credentials_lock(),
                    claude_config_lock(),
                ):
                    live = self._read_credentials() or ""
                    live_oauth = oauth.extract_oauth_data(live) if live else None
                    live_refresh = (
                        live_oauth.get("refreshToken") if live_oauth else None
                    )
                    # Re-check owners + refresh-token lineage under the lock. If a Claude
                    # Code / session appeared, or an external write (e.g. a user /login)
                    # replaced the credential since we read it, skip rather than clobber a
                    # live process's newer credential. Best effort, not perfectly atomic.
                    if (
                        self._active_cc_running()
                        or self._live_session_pids(num, acct_email)
                        or live_refresh != original_refresh
                    ):
                        persist_skipped = True
                        self._logger.warning(
                            "Active-account refresh for %s (%s): owner appeared or refresh "
                            "token changed mid-refresh; discarding rotated credential.",
                            num, acct_email,
                        )
                        return
                    # A write failure leaves the live store holding the now-consumed
                    # original refresh token, so mark the persist as skipped (never show
                    # usage for it) and re-raise — oauth._persist swallows the exception
                    # but logs its "failed to persist" warning first.
                    self._write_credentials(new_creds)  # active store — Claude Code reads this
                    self._write_account_credentials(num, acct_email, new_creds)  # backup in sync
            except Exception:
                if not persist_skipped:
                    persist_skipped = True
                raise

        outcome = oauth.try_fetch_usage_for_account(
            account_num, email, creds,
            is_active=False, persist_credentials=persist_active,
        )
        # If we refreshed but discarded the rotated credential, never show usage for
        # a credential we didn't keep — surface the expired state and let Claude Code
        # settle it.
        if persist_skipped:
            return FetchRecord(sentinel=USAGE_TOKEN_EXPIRED)
        if outcome.usage is None and oauth.is_oauth_token_expired(
            oauth_data.get("expiresAt")
        ):
            return FetchRecord(sentinel=USAGE_TOKEN_EXPIRED)
        return FetchRecord(
            usage=outcome.usage,
            error=outcome.error,
            retry_after_s=outcome.retry_after_s,
        )

    def _static_usage_sentinel(
        self, account_info: tuple[int, str, str, str, bool, str, str]
    ) -> str | None:
        """Sentinel state derivable without any network call, or ``None``.

        Re-derived on every collect pass (never persisted), so it can't
        outlive the condition that produced it.
        """
        num, email, _, _, is_active, creds, _alias = account_info
        if looks_like_api_key(creds):
            # Managed API-key account: no subscription quota to fetch.
            return USAGE_API_KEY
        if not creds or not oauth.extract_access_token(creds):
            if is_active and self._active_keychain_unavailable:
                return USAGE_KEYCHAIN_UNAVAILABLE
            return USAGE_NO_CREDENTIALS
        if is_active:
            # Owned + locally expired must be visible even when the fetch is
            # gated (fresh entry, failure backoff, concurrent claim) — the
            # auto engine's idle-hold keys on this sentinel, and it is provable
            # locally: only an owner (Claude Code / live session) may refresh
            # this credential, and it hasn't. The expiry check gates the
            # process scan, so the common non-expired path pays nothing.
            oauth_data = oauth.extract_oauth_data(creds)
            if (
                oauth_data
                and oauth.is_oauth_token_expired(oauth_data.get("expiresAt"))
                and (
                    self._active_cc_running()
                    or self._live_session_pids(str(num), email)
                )
            ):
                return USAGE_TOKEN_EXPIRED
        return None

    def _fetch_account_usage(
        self, account_info: tuple[int, str, str, str, bool, str, str]
    ) -> FetchRecord:
        """One network fetch for one account. Never raises."""
        num, email, _, org_uuid, is_active, creds, _alias = account_info

        # The active/default account owns the live credential — route it through
        # the owner-aware path that refreshes only when no Claude Code/session is
        # running and writes the rotated credential back to the active store.
        if is_active:
            return self._fetch_active_usage(str(num), email, creds)

        def persist(acct_num: str, acct_email: str, new_creds: str) -> None:
            with FileLock(self.lock_file):
                self._write_account_credentials(acct_num, acct_email, new_creds)

        from claude_swap.session import (
            read_session_credentials,
            session_identity_drifted,
        )

        has_live_session = bool(self._live_session_pids(str(num), email))

        # A session profile supersedes the backup copy as this account's
        # credential truth: claude rotates the token family inside the profile
        # and nothing syncs it back, so once a session has run, the backup's
        # refresh token is a consumed generation the server 401s forever —
        # usage would silently freeze at the last pre-session measurement.
        # Fetch with the profile's newest credential, strictly read-only
        # (is_active=True: no refresh, no persist): rotating the profile's
        # family here would log the next `cswap run` out the same way.
        session_dir = self._session_dir(str(num), email)
        session_creds = read_session_credentials(session_dir)
        if session_creds and session_identity_drifted(session_dir, email, org_uuid):
            # An in-session /login re-pointed the profile at a different
            # account; fetching with its credential would record THAT
            # account's usage under this slot's label. The profile no longer
            # holds this slot's token family, so the backup below is both the
            # right identity and safe to refresh — treat the slot as not
            # session-owned for this fetch.
            self._logger.debug(
                f"Session profile for account {num} is logged in as a "
                f"different account; fetching usage from the backup credential"
            )
            session_creds = None
            has_live_session = False
        if session_creds:
            session_oauth = oauth.extract_oauth_data(session_creds)
            if session_oauth and session_oauth.get("accessToken"):
                if not oauth.is_oauth_token_expired(session_oauth.get("expiresAt")):
                    outcome = oauth.try_fetch_usage_for_account(
                        str(num), email, session_creds, is_active=True,
                    )
                    return FetchRecord(
                        usage=outcome.usage,
                        error=outcome.error,
                        retry_after_s=outcome.retry_after_s,
                    )
                if has_live_session:
                    # The live claude refreshes lazily on its next API call;
                    # requesting now would just 401 (same rule as the owned
                    # active account in _fetch_active_usage).
                    return FetchRecord(sentinel=USAGE_TOKEN_EXPIRED)
                # Expired profile credential and no live session: fall through
                # to the backup path — cswap must not rotate the profile's
                # family, but a backup family that is still alive (e.g. the
                # account was re-added after the profile last ran) can serve
                # and heal via the normal refresh machinery below.

        outcome = oauth.try_fetch_usage_for_account(
            str(num), email, creds,
            is_active=has_live_session,
            persist_credentials=persist,
        )
        return FetchRecord(
            usage=outcome.usage,
            error=outcome.error,
            retry_after_s=outcome.retry_after_s,
        )

    def _run_usage_fetches(
        self, infos: list[tuple[int, str, str, str, bool, str, str]]
    ) -> dict[str, FetchRecord]:
        """Fetch the given accounts in parallel, staggering request starts so
        N accounts never hit the endpoint in the same instant."""
        def fetch_one(
            idx_info: tuple[int, tuple[int, str, str, str, bool, str, str]]
        ) -> tuple[str, FetchRecord]:
            idx, info = idx_info
            if idx and _FETCH_STAGGER_S:
                time.sleep(idx * _FETCH_STAGGER_S)
            return str(info[0]), self._fetch_account_usage(info)

        with ThreadPoolExecutor() as executor:
            return dict(executor.map(fetch_one, enumerate(infos)))

    def _collect_usage_entries(
        self,
        accounts_info: list[tuple[int, str, str, str, bool, str, str]],
        fetch: set[str] | None = None,
    ) -> dict[str, UsageEntry]:
        """Store-backed usage collection: one :class:`UsageEntry` per account.

        ``fetch=None`` (on-demand callers: ``--list``/``--status``/switch
        strategies, dashboards) makes every account a candidate but respects
        the persisted poll plans; the auto engine passes an explicit set whose
        members may beat the serve TTL when their plan says so (urgent
        cadence) or when escalation needs them fresh. Final eligibility —
        freshness, backoff, claims, plans — is decided atomically by
        ``UsageStore.reserve``, so concurrent collectors can never
        double-fetch a slot. After each successful fetch the adapted cadence
        is persisted (``_persist_poll_plans``), making every surface inherit
        the same plan. A failed fetch only updates the entry's error/backoff
        fields, so the last-good measurement keeps being served
        (stale-on-error).
        """
        store = self._usage_store
        identities = {
            str(num): (email, org_uuid or "")
            for num, email, _org_name, org_uuid, _active, _creds, _alias in accounts_info
        }
        info_by_num = {str(info[0]): info for info in accounts_info}
        sentinels: dict[str, str] = {}
        for num, info in info_by_num.items():
            static = self._static_usage_sentinel(info)
            if static is not None:
                sentinels[num] = static

        entries = store.entries(identities)
        # Dead refresh-token lineage: quarantine. Surfacing the sentinel here both
        # drives the "re-login needed" display and (via ``num not in sentinels``
        # below) stops the endless fetch loop that would otherwise 401/429 forever.
        for num in info_by_num:
            if num not in sentinels and entries[num].token_dead():
                sentinels[num] = USAGE_RELOGIN_REQUIRED
        requested = [
            num
            for num in info_by_num
            if num not in sentinels and (fetch is None or num in fetch)
        ]
        to_fetch = store.reserve(
            requested, identities, respect_plans=fetch is None
        )

        if to_fetch:
            pre = entries
            records = self._run_usage_fetches(
                [info_by_num[num] for num in to_fetch]
            )
            store.record(records, identities)
            for num, record in records.items():
                if record.sentinel is not None:
                    sentinels[num] = record.sentinel
            entries = store.entries(identities)
            self._persist_poll_plans(
                records, pre, entries, info_by_num, identities
            )
            # A fetch that just returned invalid_grant advances the strike to the
            # dead threshold. The pre-fetch quarantine scan above couldn't see it,
            # so surface "re-login needed" in *this* pass instead of leaving the
            # slot looking merely refresh-failed until the next refresh notices.
            for num in to_fetch:
                if entries[num].token_dead():
                    sentinels[num] = USAGE_RELOGIN_REQUIRED

        return {
            num: with_sentinel(entries[num], sentinels.get(num))
            for num in info_by_num
        }

    def _persist_poll_plans(
        self,
        records: dict[str, FetchRecord],
        pre: dict[str, UsageEntry],
        post: dict[str, UsageEntry],
        info_by_num: dict[str, tuple],
        identities: dict[str, tuple[str, str]],
    ) -> None:
        """Adapt and persist the cadence of every slot just fetched
        successfully, so the next collector — whichever surface it runs in —
        inherits the plan. Failures are paced by the store's backoff instead
        and keep their (now past-due) plan for when the backoff lifts."""
        now = self._usage_store.clock()
        threshold, models = self._poll_policy_inputs()
        plans: dict[str, tuple[float | None, float | None]] = {}
        for num, rec in records.items():
            if rec.sentinel is not None or rec.error is not None:
                continue
            before, after = pre.get(num), post.get(num)
            if after is None or after.fetched_at is None:
                continue
            recent_429 = (
                before is not None
                and before.last_429_at is not None
                and (now - before.last_429_at) < poll_policy.RECENT_429_WINDOW_S
            )
            plans[num] = poll_policy.plan_after_fetch(
                prev_interval_s=before.poll_interval_s if before else None,
                prev_usage=before.last_good if before else None,
                new_usage=after.last_good,
                is_active=bool(info_by_num[num][4]),
                threshold=threshold,
                models=models,
                recent_429=recent_429,
                now=now,
            )
        if plans:
            self._usage_store.set_poll_plan(plans, identities)

    def _replan_new_active(self, number: str, email: str, org_uuid: str) -> None:
        """Pull the just-activated account's poll plan to the active floor.

        Its stored plan was computed while it was an idle candidate and may
        wait up to CANDIDATE_MAX_INTERVAL_S — too slow for the account whose
        usage is about to move. The deadline anchors on the last measurement
        (an already-old one comes due immediately, a never-measured account
        is left plan-less so nothing blocks its first fetch), and the next
        poll is only ever pulled earlier, never pushed later. Best-effort by
        contract: the switch this rides on has already committed, so a cache
        hiccup here must not surface as a switch failure."""
        try:
            identities = {number: (email, org_uuid or "")}
            now = self._usage_store.clock()
            entry = self._usage_store.entries(identities).get(number)
            if entry is None or entry.fetched_at is None:
                return
            next_poll = max(now, entry.fetched_at + poll_policy.MIN_INTERVAL_S)
            if entry.next_poll_at is not None and entry.next_poll_at <= next_poll:
                return
            self._usage_store.set_poll_plan(
                {number: (next_poll, poll_policy.MIN_INTERVAL_S)}, identities
            )
        except Exception as e:
            self._logger.warning(
                f"Post-switch poll re-plan failed (switch itself succeeded): {e}"
            )

    def _usage_by_account(self) -> dict[str, dict | str | None]:
        """Map account number → decision-grade usage value for managed accounts."""
        accounts_info = self._build_accounts_info()
        entries = self._collect_usage_entries(accounts_info)
        return {num: entry.decision_value() for num, entry in entries.items()}

    def _warn_inert_models(
        self,
        usage: dict,
        models: tuple[str, ...],
        json_output: bool,
        warnings: list[str],
    ) -> None:
        """One-shot typo guard for --model on the manual strategies.

        A configured name that no account reports gates nothing while looking
        active. Only claimed when every account's usage is readable (an
        unreadable account could be the one carrying the window)."""
        wanted = {m.lower(): m for m in models if m.lower() != "all"}
        if not wanted or not usage:
            return
        if any(not isinstance(v, dict) for v in usage.values()):
            return
        seen = {
            s["name"].lower()
            for v in usage.values()
            for s in (v.get("scoped") or [])
            if isinstance(s, dict) and isinstance(s.get("name"), str)
        }
        missing = [name for low, name in wanted.items() if low not in seen]
        if not missing:
            return
        msg = (
            f"model(s) {', '.join(missing)} match no account's usage windows "
            "(typo?)"
        )
        if json_output:
            warnings.append(msg)
        else:
            warning(msg)

    def _select_best_switchable(
        self,
        current_num: str | None,
        models: tuple[str, ...] = (),
        usage: dict | None = None,
    ) -> tuple[str | None, str]:
        """Decide the ``best`` strategy target relative to the current account.

        Compares the rate-limit headroom of every *other* switchable account
        against the current one and only recommends a switch it can *prove*
        lands on strictly more headroom — never onto an account worse than (or
        merely unverifiable against) where the user already is. When a switch
        can't be proven beneficial, it stays put; bare ``cswap --switch``
        remains the way to force a plain rotation. ``models`` folds the named
        per-model weekly windows into every headroom comparison (see
        ``oauth.account_headroom``). Returns ``(target, note)``:

        - ``(num, "")`` — switch to ``num`` (strictly more headroom than current)
        - ``(None, "current-unavailable")`` — current account's usage is unknown,
          so no comparison is possible → stay
        - ``(None, "no-comparison")`` — no other account has known usage → stay
        - ``(None, "incomplete-comparison")`` — current is best among the
          accounts we can measure, but some candidate's usage is unknown, so we
          can't claim it's the best or that everything is exhausted → stay
        - ``(None, "stay")`` — current account provably has the most headroom
        - ``(None, "exhausted")`` — current is the best and every account is at
          its limit (switching would not help) → stay
        - ``(None, "none")`` — no other switchable account exists

        Ties (including current-vs-other) resolve in favour of staying put.
        Never raises on network failure.
        """
        data = self._get_sequence_data() or {}
        others = [
            str(n) for n in data.get("sequence", [])
            if str(n) != str(current_num)
            and self._account_is_switchable(str(n))
            and not self._disabled_from_data(data, str(n))
        ]
        if not others:
            return None, "none"

        if usage is None:
            usage = self._usage_by_account()
        current_headroom = oauth.account_headroom(usage.get(str(current_num)), models)
        if current_headroom is None:
            # Can't measure where the user is → can't prove any target is
            # better. Stay rather than risk moving onto a worse account.
            return None, "current-unavailable"

        scored = [
            (oauth.account_headroom(usage.get(num), models), num) for num in others
        ]
        known = [(h, num) for h, num in scored if h is not None]
        if not known:
            return None, "no-comparison"

        # max() keeps the first maximal element; `known` preserves rotation
        # order, so ties resolve to the earliest slot.
        best_headroom, best_num = max(known, key=lambda t: t[0])
        if best_headroom > current_headroom:
            return best_num, ""

        # Current is at least as good as every account we can measure. Stay —
        # but only claim "all exhausted" when every candidate's usage is known.
        if any(h is None for h, _ in scored):
            return None, "incomplete-comparison"
        if current_headroom <= 0:
            return None, "exhausted"
        return None, "stay"

    def _duplicate_account_warnings(
        self, accounts_info: list[tuple[int, str, str, str, bool, str, str]]
    ) -> list[str]:
        """Slots that provably authenticate as the same account.

        Impossible by construction, so a collision means one slot's credential
        was overwritten with another's (issue #117's end state) or the same
        account was registered twice. Two offline signals:

        - identical credential fingerprint (same refresh-token lineage or
          identical raw token) across two slots;
        - the same non-empty ``uuid`` + org recorded for two slots (empty
          uuids — add-token placeholders — never match each other).

        Limitation: two *different generations* of the same account (the
        poisoned end state a pre-guard switch could produce) carry different
        fingerprints and untouched sequence.json identities, so they are not
        offline-detectable here — ``_lockstep_usage_warnings`` covers that
        case heuristically. The switch-time guard prevents new occurrences
        whenever the identity oracle answers.
        """
        data = self._get_sequence_data() or {}
        by_fp: dict[str, str] = {}
        by_identity: dict[tuple[str, str], str] = {}
        out: list[str] = []
        for num, email, _org_name, org_uuid, _is_active, creds, _alias in accounts_info:
            snum = str(num)
            fp = oauth.credential_fingerprint(creds) if creds else None
            if fp:
                other = by_fp.get(fp)
                if other:
                    out.append(
                        f"Account-{other} and Account-{snum} hold the same "
                        f"credential ({email}) — one slot's backup was "
                        "overwritten. Log in with the missing account and "
                        "re-add it: cswap add --slot N"
                    )
                else:
                    by_fp[fp] = snum
            uuid = (data.get("accounts", {}).get(snum, {}).get("uuid") or "").strip()
            if uuid:
                key = (uuid, org_uuid or "")
                other = by_identity.get(key)
                if other and other != snum:
                    out.append(
                        f"Account-{other} and Account-{snum} both authenticate "
                        f"as {email} — remove or re-login one of them."
                    )
                elif not other:
                    by_identity[key] = snum
        return out

    def _lockstep_usage_warnings(
        self,
        accounts_info: list[tuple[int, str, str, str, bool, str, str]],
        entries: dict[str, UsageEntry],
    ) -> list[str]:
        """Heuristic: slots whose usage moves in perfect lockstep.

        Two different *generations* of the same account (the poisoned end
        state a pre-guard switch could produce — issue #117) carry different
        fingerprints and untouched sequence.json identities, so
        ``_duplicate_account_warnings`` cannot see them. But both tokens
        report the same account's usage: identical 5h *and* 7d percentages
        with identical reset timestamps — the exact signal the issue's
        reporter had to reverse-engineer by hand, automated here from data
        ``list``/watch already fetched.

        Heuristic, not proof: it goes quiet once the older generation dies
        and stops producing comparable usage, and only rows where both
        windows carry a non-null ``resets_at`` are compared (two idle
        accounts at 0% with nothing scheduled are indistinguishable, never
        flagged; API-key slots have sentinel usage and never reach the
        comparison). Known benign false-positive source until PR #119 lands:
        a session profile that drifted to another account makes its slot
        report that account's usage — same lockstep signature, different
        cause.
        """
        seen: dict[tuple, str] = {}
        out: list[str] = []
        for num, _email, _org_name, _org_uuid, _is_active, _creds, _alias in accounts_info:
            snum = str(num)
            entry = entries.get(snum)
            usage = entry.decision_value() if entry else None
            if not isinstance(usage, dict):
                continue
            h5 = usage.get("five_hour")
            d7 = usage.get("seven_day")
            if not isinstance(h5, dict) or not isinstance(d7, dict):
                continue
            key = (
                h5.get("pct"), h5.get("resets_at"),
                d7.get("pct"), d7.get("resets_at"),
            )
            if key[1] is None or key[3] is None or key[0] is None or key[2] is None:
                continue
            other = seen.get(key)
            if other:
                out.append(
                    f"Account-{other} and Account-{snum} report identical "
                    "usage and reset times — they may be the same account "
                    "(issue #117). If it persists, log in with the missing "
                    "account and re-add it: cswap add --slot N"
                )
            else:
                seen[key] = snum
        return out

    def _build_list_payload(
        self,
        accounts_info: list[tuple[int, str, str, str, bool, str, str]],
        entries: dict[str, UsageEntry],
    ) -> dict:
        """Build the ``--list --json`` payload from gathered account + usage data."""
        active_num: int | None = None
        accounts = []
        seq_data = self._get_sequence_data() or {}
        for num, email, org_name, org_uuid, is_active, _, alias in accounts_info:
            if is_active:
                active_num = num
            entry = entries[str(num)]
            # JSON carries the decision-grade value: last-good only while it is
            # recent enough to act on (≤ STALE_OK_S), else unavailable. Showing
            # older measurements is a human-display affordance only — scripts
            # keying on usageStatus == "ok" must not act on arbitrarily old data.
            accounts.append(
                account_row(
                    num, email, org_name, org_uuid, is_active,
                    entry.decision_value(),
                    usage_fetched_at=entry.fetched_at,
                    usage_age_s=entry.age_s,
                    alias=alias,
                    disabled=self._disabled_from_data(seq_data, str(num)),
                )
            )
        payload = {
            "schemaVersion": SCHEMA_VERSION,
            "activeAccountNumber": active_num,
            "accounts": accounts,
        }
        # Additive fields (absent when clean) — never printed warnings; the
        # JSON contract keeps stdout a single machine-readable object.
        dup_warnings = self._duplicate_account_warnings(accounts_info)
        if dup_warnings:
            payload["duplicateAccountWarnings"] = dup_warnings
        lockstep_warnings = self._lockstep_usage_warnings(accounts_info, entries)
        if lockstep_warnings:
            payload["lockstepUsageWarnings"] = lockstep_warnings
        unclaimed = self._store._list_unclaimed_credentials()
        if unclaimed:
            payload["unclaimedCredentials"] = sorted(unclaimed)
        return payload

    def list_accounts(
        self,
        show_token_status: bool = False,
        json_output: bool = False,
        fetch: set[str] | None = None,
    ) -> dict | None:
        """List all managed accounts.

        In ``json_output`` mode, returns the schema-v1 payload (printing nothing)
        for the CLI to serialize; otherwise prints the human view and returns None.

        ``fetch`` restricts which accounts *may* be fetched this pass (the TUI
        watch view's adaptive set); ``None`` — the CLI default — leaves every
        stale account eligible.
        """
        if not self.sequence_file.exists():
            # JSON mode must never prompt — emit an empty list instead of the
            # interactive first-run setup.
            if json_output:
                return {
                    "schemaVersion": SCHEMA_VERSION,
                    "activeAccountNumber": None,
                    "accounts": [],
                }
            print(dimmed("No accounts are managed yet."))
            self._first_run_setup()
            return None

        accounts_info = self._build_accounts_info()
        entries = self._collect_usage_entries(accounts_info, fetch=fetch)

        if json_output:
            return self._build_list_payload(accounts_info, entries)

        seq_data = self._get_sequence_data() or {}
        print(bolded("Accounts:"))
        for i, (num, email, org_name, org_uuid, is_active, _, alias) in enumerate(accounts_info):
            tag = self._get_display_tag(email, org_name, org_uuid)
            label = f"{accent(alias)} ({email})" if alias else email
            markers = ""
            if is_active:
                markers += f" {bold_accent('(active)')}"
            if self._disabled_from_data(seq_data, str(num)):
                markers += f" {muted('(disabled)')}"
            print(f"  {num}: {label} {muted(f'[{tag}]')}{markers}")
            for line in _usage_entry_lines(entries[str(num)]):
                print(f"     {line}")

            if show_token_status:
                token_status = oauth.build_token_status(accounts_info[i][5])
                if token_status:
                    print(f"     {dimmed('•')} {muted(token_status)}")
            if i < len(accounts_info) - 1:
                print()

        # Safety copies (unclaimed credentials) are deliberately NOT surfaced
        # here: users can't act on them (recovery is always /login + cswap
        # add), and with no GC a one-time event would nag forever. They stay
        # in the JSON payload and logs for diagnostics.
        dup_warnings = self._duplicate_account_warnings(accounts_info)
        lockstep_warnings = self._lockstep_usage_warnings(accounts_info, entries)
        if dup_warnings or lockstep_warnings:
            print()
            for msg in dup_warnings:
                warning(msg)
            for msg in lockstep_warnings:
                warning(msg)

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

    def _active_account_usage(
        self, account_num: str, current_email: str, org_uuid: str
    ) -> UsageEntry:
        """Store-backed usage entry for just the active account.

        Builds a single-account info row instead of the full accounts list
        (``--status`` touches one slot) and runs it through the shared
        collector, so freshness/backoff/claim gating and the shared
        ``cache/usage.json`` table behave exactly as in ``--list``.
        """
        active = self._read_active_credentials()
        creds = active.value or ""
        self._active_keychain_unavailable = active.keychain_unavailable
        info = (int(account_num), current_email, "", org_uuid or "", True, creds, "")
        return self._collect_usage_entries([info])[str(account_num)]

    def _build_status_payload(self) -> dict:
        """Build the ``--status --json`` payload (no active / unmanaged / managed)."""
        identity = self._get_current_account()
        if identity is None:
            return {"schemaVersion": SCHEMA_VERSION, "active": None}
        current_email, current_org_uuid = identity

        data = self._get_sequence_data_migrated()
        if not data:
            return {
                "schemaVersion": SCHEMA_VERSION,
                "active": {"email": current_email, "managed": False},
            }

        account_num = self._find_account_slot(data, current_email, current_org_uuid)
        if not account_num:
            return {
                "schemaVersion": SCHEMA_VERSION,
                "active": {"email": current_email, "managed": False},
            }

        acct = data["accounts"][account_num]
        org_name = acct.get("organizationName", "") or ""
        org_uuid = acct.get("organizationUuid", "") or ""
        alias = acct.get("alias", "") or ""
        entry = self._active_account_usage(account_num, current_email, org_uuid)
        # Decision-grade projection, same rule as the --list payload: stale
        # beyond STALE_OK_S reports unavailable, not "ok" with old numbers.
        status, usage = usage_fields(entry.decision_value())
        active: dict = {
            "number": int(account_num),
            "email": current_email,
            "organizationName": org_name,
            "organizationUuid": org_uuid,
            "isOrganization": bool(org_uuid),
            "managed": True,
            "usageStatus": status,
            "usage": usage,
        }
        if alias:
            active["alias"] = alias
        if usage is not None:
            active.update(usage_freshness_fields(entry.fetched_at, entry.age_s))
        return {
            "schemaVersion": SCHEMA_VERSION,
            "active": active,
            "totalManagedAccounts": len(data.get("accounts", {})),
        }

    def status(self, json_output: bool = False) -> dict | None:
        """Display current account status (or return the schema-v1 payload)."""
        if json_output:
            return self._build_status_payload()

        identity = self._get_current_account()
        if identity is None:
            print(f"{bolded('Status:')} {dimmed('No active Claude account')}")
            return None
        current_email, current_org_uuid = identity

        data = self._get_sequence_data_migrated()
        if not data:
            print(f"{bolded('Status:')} {current_email} {dimmed('(not managed)')}")
            return None

        account_num = self._find_account_slot(data, current_email, current_org_uuid)
        org_name = ""
        if account_num is not None:
            org_name = data["accounts"][account_num].get("organizationName", "") or ""

        if account_num:
            tag = self._get_display_tag(current_email, org_name, current_org_uuid)
            total = len(data.get("accounts", {}))
            print(
                f"{bolded('Status:')} {accent(f'Account-{account_num}')} "
                f"({current_email} {muted(f'[{tag}]')})"
            )
            print(f"  {dimmed(f'Total managed accounts: {total}')}")
            entry = self._active_account_usage(
                account_num, current_email, current_org_uuid
            )
            for line in _usage_entry_lines(entry):
                print(f"  {line}")
        else:
            print(f"{bolded('Status:')} {current_email} {dimmed('(not managed)')}")
        return None

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

    def _switch_result_from_op(
        self, op: dict, strategy: str, extra_warnings: list[str] | None = None
    ) -> dict:
        """Build a switch result from a ``_perform_switch`` return value.

        ``switched`` is derived from whether the live identity actually changed
        (``from != to``) — covering recorded/live drift in plain rotation, not just
        ``switch_to`` onto the already-active account.
        """
        from_ref = op["from"]
        to_ref = op["to"]
        switched = from_ref != to_ref
        if switched:
            reason = "switched"
            message = f"Switched to Account-{to_ref['number']} ({to_ref['email']})"
        else:
            reason = "already-active"
            message = f"Already on Account-{to_ref['number']} ({to_ref['email']})"
        return {
            "schemaVersion": SCHEMA_VERSION,
            "switched": switched,
            "from": from_ref,
            "to": to_ref,
            "strategy": strategy,
            "reason": reason,
            "message": message,
            "warnings": (extra_warnings or []) + op["warnings"],
        }

    def _switch_noop(
        self,
        *,
        strategy: str,
        reason: str,
        message: str,
        from_ref: dict | None = None,
        to_ref: dict | None = None,
        warnings: list[str] | None = None,
    ) -> dict:
        """Build a no-op switch result (``switched: false``).

        For a no-op the user neither left nor arrived anywhere — ``from`` and
        ``to`` are both the current account. Callers pass ``to_ref`` (where they
        stayed); ``from_ref`` defaults to it so every ``switched: false`` payload
        reports ``from == to``.
        """
        if from_ref is None:
            from_ref = to_ref
        return {
            "schemaVersion": SCHEMA_VERSION,
            "switched": False,
            "from": from_ref,
            "to": to_ref,
            "strategy": strategy,
            "reason": reason,
            "message": message,
            "warnings": warnings or [],
        }

    def switch(
        self,
        strategy: str | None = None,
        json_output: bool = False,
        models: tuple[str, ...] = (),
        model_source: str | None = None,
    ) -> dict | None:
        """Switch to next account in sequence.

        Args:
            strategy: Usage-aware target selection. ``"best"`` jumps to the
                  switchable account with the most remaining 5h/7d quota instead
                  of advancing the rotation; ``"next-available"`` rotates to the
                  next account, skipping any currently at its 5h/7d limit. ``None``
                  (the default) performs a plain rotation.
            models: Per-model weekly windows folded into every usage
                  comparison of the usage-aware strategies (parsed display
                  names, or the ``all`` sentinel — see
                  ``oauth.relevant_windows``). Empty = 5h/7d only.
            model_source: Where ``models`` came from (``"cli"`` or
                  ``"autoswitch.model"``) — announced up front so a config
                  fallback silently steering the pick is impossible.

        ``"best"`` only switches when it can prove another account has more
        remaining quota; if usage can't be fetched or no candidate is provably
        better, it stays put (run a plain ``cswap --switch`` to rotate anyway).
        ``"next-available"`` rotates and skips accounts at their limit, falling
        back to plain rotation when usage is unavailable. Both apply only to the
        normal path (a live Claude login present); the fresh-machine path (no
        live login, e.g. right after --import) ignores them.
        """
        strategy_label = strategy if strategy in ("best", "next-available") else "rotation"
        warnings: list[str] = []
        if strategy_label == "rotation":
            models = ()  # model limits only steer the usage-aware strategies
        if models and not json_output:
            source = "--model" if model_source == "cli" else model_source
            print(dimmed(
                f"Using configured model limits: {', '.join(models)}"
                + (f" (from {source})" if source else "")
            ))

        if not self.sequence_file.exists():
            raise ConfigError("No accounts are managed yet")

        identity = self._get_current_account()

        # Ensure org fields are migrated before checking composite key
        self._get_sequence_data_migrated()

        # Fresh-machine path: no live Claude session, but we have managed accounts
        # (e.g. right after cswap --import). Activate the recorded
        # activeAccountNumber, or fall back to the first slot in sequence.
        # With no live state to capture, the target must have valid backups —
        # walk the sequence if the preferred target is broken.
        if identity is None:
            data = self._get_sequence_data() or {}
            sequence = data.get("sequence", [])
            preferred = data.get("activeAccountNumber")
            if not preferred and sequence:
                preferred = sequence[0]
            if not preferred:
                raise ConfigError("No accounts are managed yet")

            target = str(preferred)
            target_disabled = self._disabled_from_data(data, target)
            if target_disabled or not self._account_is_switchable(target):
                if target_disabled:
                    reason = console_reason = "(disabled)"
                else:
                    reason = "(no stored credentials/config)"
                    console_reason = (
                        "(no stored credentials/config, re-add with "
                        f"cswap --add-account --slot {target})"
                    )
                if json_output:
                    warnings.append(f"Skipped Account-{target} {reason}")
                else:
                    print(f"{accent('Skipping')} Account-{target} {console_reason}")
                fallback = next(
                    (str(num) for num in sequence
                     if str(num) != target
                     and not self._disabled_from_data(data, str(num))
                     and self._account_is_switchable(str(num))),
                    None,
                )
                if not fallback:
                    if any(
                        self._account_is_switchable(str(num)) for num in sequence
                    ):
                        raise ConfigError(
                            "No accounts remain in rotation. Re-enable one with: "
                            "cswap enable <num|email>"
                        )
                    raise ConfigError(
                        "No managed accounts have valid stored credentials/config. "
                        "Re-add a slot with: cswap --add-account --slot <number>"
                    )
                target = fallback
            op = self._perform_switch(target, emit_output=not json_output)
            return (
                self._switch_result_from_op(op, strategy_label, warnings)
                if json_output else None
            )

        current_email, current_org_uuid = identity

        # Check if current account is managed
        if not self._account_exists(current_email, current_org_uuid):
            # In JSON mode, don't silently auto-add (a surprising side effect in
            # automation) — report it as a structured no-op instead.
            if json_output:
                ref = account_ref(None, current_email)
                return self._switch_noop(
                    strategy=strategy_label,
                    reason="unmanaged-account",
                    from_ref=ref,
                    to_ref=ref,
                    message="Active account is not managed; run cswap --add-account",
                )
            print(f"{accent('Notice:')} Active account '{current_email}' was not managed.")
            self.add_account()
            data = self._get_sequence_data()
            account_num = data.get("activeAccountNumber")
            print(f"It has been automatically added as Account-{account_num}.")
            print(dimmed("Please run the switch command again to switch to the next account."))
            return None

        data = self._get_sequence_data()
        sequence = data.get("sequence", [])

        if len(sequence) < 2:
            if json_output:
                num = self._find_account_slot(data, current_email, current_org_uuid)
                return self._switch_noop(
                    strategy=strategy_label,
                    reason="only-one-account",
                    to_ref=account_ref(int(num), current_email) if num else None,
                    message="Only one account is managed. Add more accounts to switch between.",
                )
            print(dimmed("Only one account is managed. Add more accounts to switch between."))
            return None

        active_account = data.get("activeAccountNumber")
        # Where the user actually is right now (live identity), falling back to
        # the recorded active slot. Used so usage-aware switching never moves
        # them onto an account worse than their current one.
        current_num = self._find_account_slot(data, current_email, current_org_uuid)
        if current_num is None:
            current_num = str(active_account) if active_account is not None else None

        current_ref = (
            account_ref(int(current_num), current_email) if current_num else None
        )

        # Usage-aware "jump to most headroom". Only switches when another
        # account is provably better; otherwise stays put (never moves onto a
        # worse or unverifiable account). Bare `cswap --switch` rotates anyway.
        if strategy == "best":
            best_usage = self._usage_by_account()
            self._warn_inert_models(best_usage, models, json_output, warnings)
            target, note = self._select_best_switchable(
                current_num, models, best_usage
            )
            if target is not None:
                op = self._perform_switch(target, emit_output=not json_output)
                return (
                    self._switch_result_from_op(op, strategy_label, warnings)
                    if json_output else None
                )
            if note == "current-unavailable":
                if json_output:
                    return self._switch_noop(
                        strategy=strategy_label, reason="usage-unavailable",
                        to_ref=current_ref, warnings=warnings,
                        message=(
                            f"Current account usage is unavailable — staying on "
                            f"Account-{current_num}."
                        ),
                    )
                print(dimmed(
                    f"Current account usage is unavailable — staying on "
                    f"Account-{current_num}. Run cswap --switch to rotate."
                ))
                return None
            if note == "no-comparison":
                if json_output:
                    return self._switch_noop(
                        strategy=strategy_label, reason="usage-unavailable",
                        to_ref=current_ref, warnings=warnings,
                        message=(
                            f"No other account has usage data to compare — staying "
                            f"on Account-{current_num}."
                        ),
                    )
                print(dimmed(
                    f"No other account has usage data to compare — staying on "
                    f"Account-{current_num}. Run cswap --switch to rotate."
                ))
                return None
            if note == "incomplete-comparison":
                if json_output:
                    return self._switch_noop(
                        strategy=strategy_label, reason="usage-unavailable",
                        to_ref=current_ref, warnings=warnings,
                        message=(
                            f"No account with known usage has more remaining quota; "
                            f"some usage is unavailable — staying on Account-{current_num}."
                        ),
                    )
                print(dimmed(
                    f"No account with known usage has more remaining quota; some "
                    f"usage is unavailable — staying on Account-{current_num}."
                ))
                return None
            if note == "stay":
                if json_output:
                    return self._switch_noop(
                        strategy=strategy_label, reason="already-best",
                        to_ref=current_ref, warnings=warnings,
                        message=(
                            f"Already on the account with the most remaining quota "
                            f"(Account-{current_num})."
                        ),
                    )
                print(
                    f"{accent('Already on the account with the most remaining quota')} "
                    f"(Account-{current_num})."
                )
                return None
            if note == "exhausted":
                # With model limits in play the binding window may be scoped.
                limits_label = "usage limits" if models else "5h/7d limit"
                if json_output:
                    return self._switch_noop(
                        strategy=strategy_label, reason="candidates-exhausted",
                        to_ref=current_ref, warnings=warnings,
                        message=(
                            f"All accounts are at their {limits_label} — staying on "
                            f"Account-{current_num}."
                        ),
                    )
                warning(
                    f"All accounts are at their {limits_label} — staying on "
                    f"Account-{current_num}."
                )
                return None
            # note == "none": fall through; rotation reports the lack of targets.

        # Find current index and get next, skipping broken candidates.
        # The active slot is never checked here — _perform_switch captures
        # live state into a fresh backup before swapping, so the active
        # slot's stored backup may be stale or absent without blocking us.
        #
        # Usage-aware rotation anchors on the live account (current_num) so it
        # never lands a no-op on the slot you're already on when the live login
        # has drifted from the recorded activeAccountNumber. Plain rotation keeps
        # anchoring on active_account for byte-for-byte unchanged behavior.
        anchor = current_num if strategy == "next-available" else active_account
        try:
            current_index = sequence.index(int(anchor))
        except (TypeError, ValueError):
            try:
                current_index = sequence.index(active_account)
            except (TypeError, ValueError):
                current_index = 0

        # Only fetch usage when needed; an empty map means the headroom check
        # below is always None (skipped), preserving the non-usage-aware path.
        usage = self._usage_by_account() if strategy == "next-available" else {}
        if strategy == "next-available":
            self._warn_inert_models(usage, models, json_output, warnings)

        next_account: str | None = None
        skipped_exhausted: list[str] = []
        for offset in range(1, len(sequence)):
            candidate = str(sequence[(current_index + offset) % len(sequence)])
            if self._disabled_from_data(data, candidate):
                if json_output:
                    warnings.append(f"Skipped Account-{candidate} (disabled)")
                else:
                    print(f"{accent('Skipping')} Account-{candidate} (disabled)")
                continue
            if not self._account_is_switchable(candidate):
                if json_output:
                    warnings.append(
                        f"Skipped Account-{candidate} (no stored credentials/config)"
                    )
                else:
                    print(
                        f"{accent('Skipping')} Account-{candidate} "
                        f"(no stored credentials/config, re-add with "
                        f"cswap --add-account --slot {candidate})"
                    )
                continue
            if strategy == "next-available":
                headroom = oauth.account_headroom(usage.get(candidate), models)
                if headroom is not None and headroom <= 0:
                    skipped_exhausted.append(candidate)
                    label = "5h/7d"
                    if models:
                        # Name what actually binds ("Fable", "5h/Fable", ...)
                        # so a config-driven skip is never mysterious.
                        at = [
                            name
                            for name, pct, _ in oauth.relevant_windows(
                                usage.get(candidate), models
                            )
                            if pct >= 100.0
                        ]
                        if at:
                            label = "/".join(at)
                    if json_output:
                        warnings.append(
                            f"Skipped Account-{candidate} (at {label} limit)"
                        )
                    else:
                        print(f"{accent('Skipping')} Account-{candidate} (at {label} limit)")
                    continue
            next_account = candidate
            break

        # Every rotation target is at its limit. Switching onto an exhausted
        # account would not help, so stay on the current one instead.
        if next_account is None and skipped_exhausted:
            # With model limits in play the binding window may be a scoped
            # one (the per-skip lines name it), so don't claim "5h/7d".
            limits_label = "usage limits" if models else "5h/7d limit"
            if json_output:
                return self._switch_noop(
                    strategy=strategy_label, reason="candidates-exhausted",
                    to_ref=current_ref, warnings=warnings,
                    message=(
                        f"All other accounts are at their {limits_label} — staying on "
                        f"Account-{current_num}."
                    ),
                )
            warning(
                f"All other accounts are at their {limits_label} — staying on "
                f"Account-{current_num}."
            )
            return None

        if next_account is None:
            if json_output:
                return self._switch_noop(
                    strategy=strategy_label, reason="no-valid-target",
                    to_ref=current_ref, warnings=warnings,
                    message="No other accounts have valid stored credentials/config.",
                )
            print(dimmed(
                "No other accounts have valid stored credentials/config.\n"
                "Re-add a skipped slot with: cswap --add-account --slot <number>"
            ))
            return None

        # Rotation anchored on a drifted activeAccountNumber can land on the
        # slot the user is already on — a self-switch would pointlessly rewrite
        # the live credentials (issue #79's hazard, on the strategy path).
        # Provenance-aware: only a no-op when the live credential matches the
        # slot's backup (or the divergence can't be classified — pre-fix
        # behavior, silent); a resolved divergence falls through so
        # _perform_switch can reconcile it.
        provenance: dict | None = None
        if next_account == current_num:
            action, provenance = self._self_switch_action(
                next_account, current_email
            )
            if action != "reconcile":
                if json_output:
                    return self._switch_noop(
                        strategy=strategy_label,
                        reason="already-active",
                        from_ref=current_ref,
                        to_ref=current_ref,
                        warnings=warnings,
                        message=f"Already on Account-{next_account} ({current_email})",
                    )
                print(
                    f"{accent('Already on')} Account-{next_account} ({current_email})"
                )
                return None

        op = self._perform_switch(
            next_account, emit_output=not json_output, provenance=provenance
        )
        return (
            self._switch_result_from_op(op, strategy_label, warnings)
            if json_output else None
        )

    def switch_to(
        self, identifier: str, json_output: bool = False, force: bool = False
    ) -> dict | None:
        """Switch to specific account.

        ``force`` activates the target's stored credentials directly, skipping
        both the already-active no-op guard and the backup-current step —
        the recovery path for a live login gone stale (e.g. after --import).
        """
        if not self.sequence_file.exists():
            raise ConfigError("No accounts are managed yet")

        # Ensure org fields are migrated before resolving accounts
        self._get_sequence_data_migrated()

        # Resolve identifier
        if not identifier.isdigit():
            is_alias = self._find_account_by_alias(identifier) is not None
            if not is_alias and not self._validate_email(identifier):
                raise ValidationError(f"Invalid account identifier: {identifier}")

            # For email identifiers, handle ambiguous matches interactively —
            # except in JSON mode, where we never prompt. There we fall through
            # to _resolve_account_identifier, which raises a ConfigError listing
            # the matching slots (+ org labels) → structured error envelope.
            # Aliases are unique by construction, so they never hit this.
            if not json_output and not is_alias:
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
                        return None
                    identifier = choice

        target_account = self._resolve_account_identifier(identifier)
        if not target_account:
            raise AccountNotFoundError(
                f"No account found with identifier: {identifier}"
            )

        data = self._get_sequence_data()
        if target_account not in data.get("accounts", {}):
            raise AccountNotFoundError(f"Account-{target_account} does not exist")

        # Short-circuit a no-op before mutating (issue #79). A self-switch
        # would first back up the live credentials into the target slot —
        # destroying a freshly imported backup with a possibly stale login —
        # then read them straight back. It also re-writes credentials, takes
        # the lock, and (on macOS) touches the Keychain for nothing. --force
        # skips this guard on purpose: its job is to rewrite the live login
        # from the stored backup. Provenance-aware (issue #117): the no-op is
        # only taken when the live credential matches the slot's backup or
        # the divergence can't be classified — pre-fix behavior, silent — and
        # a *resolved* divergence falls through so _perform_switch can
        # reconcile it.
        provenance: dict | None = None
        if not force and data:
            identity = self._get_current_account()
            if identity is not None:
                cur_slot = self._find_account_slot(data, identity[0], identity[1])
                if cur_slot == target_account:
                    action, provenance = self._self_switch_action(
                        target_account, identity[0]
                    )
                if cur_slot == target_account and action != "reconcile":
                    email = (
                        data.get("accounts", {}).get(target_account, {}).get("email", "")
                    )
                    ref = account_ref(int(target_account), email)
                    if not json_output:
                        print(
                            f"{accent('Already on')} Account-{target_account} ({email})"
                        )
                        print(dimmed(
                            "To rewrite the live login from the stored backup "
                            "(e.g. after --import), run: "
                            f"cswap --switch-to {target_account} --force"
                        ))
                        return None
                    return self._switch_noop(
                        strategy="direct",
                        reason="already-active",
                        from_ref=ref,
                        to_ref=ref,
                        message=f"Already on Account-{target_account} ({email})",
                    )

        op = self._perform_switch(
            target_account,
            emit_output=not json_output,
            force_activate=force,
            provenance=provenance,
        )
        result = self._switch_result_from_op(op, "direct") if json_output else None
        # A forced self-activation really rewrote the live credentials from the
        # stored backup — "already-active" would misdescribe that mutation.
        # A cross-slot force stays "switched": reason reports the outcome, not
        # the skipped-backup mechanism.
        if result is not None and force and not result["switched"]:
            to = result["to"]
            result["reason"] = "activated"
            result["message"] = (
                f"Activated Account-{to['number']} ({to['email']}) from stored backup"
            )
        return result

    def _live_matches_slot_backup(self, slot: str, email: str) -> bool:
        """Whether the live credential is provably the slot's stored lineage.

        Byte or refresh-token-fingerprint equality against the slot's backup.
        Used to make self-switch short-circuits provenance-aware: a no-op is
        only safe when live state matches what the slot holds — when they
        have diverged, the switch should run so ``_perform_switch`` can
        classify the live bytes (re-sync or preserve) instead of silently
        leaving the divergence in place. Unreadable/empty live credentials
        return True (keep the no-op: forcing a switch on missing evidence
        would fail later anyway).
        """
        try:
            live = self._read_credentials()
        except Exception:
            return True
        if not live:
            return True
        backup = self._read_account_credentials(slot, email)
        if not backup:
            return False
        return live == backup or (
            oauth.credential_fingerprint(live)
            == oauth.credential_fingerprint(backup)
        )

    def _self_switch_action(self, slot: str, email: str) -> tuple[str, dict | None]:
        """How to treat a switch that targets the already-active slot.

        Returns ``(action, provenance)``:

        - ``("noop", None)`` — live matches the slot's backup; nothing to do
          (issue #79's short-circuit).
        - ``("reconcile", provenance)`` — live diverged and its owner was
          resolved: run the full switch so ``_perform_switch`` can classify
          (re-sync a legitimate rotation, or preserve foreign bytes and
          restore the slot's stored credential).
        - ``("noop-diverged", None)`` — live diverged but cannot be
          classified (offline / endpoint failure / no profile access). Exact
          pre-fix behavior: an ordinary already-active no-op, silent to the
          user — endpoint trouble must never surface on the self-switch path
          either. Leaving everything untouched is also the safe write:
          activating the stored backup over an unverified live credential
          could replace a freshly rotated token with its consumed ancestor.
        """
        if self._live_matches_slot_backup(slot, email):
            return "noop", None
        provenance = self._prefetch_live_identity()
        if provenance.get("resolved") is None:
            self._logger.info(
                "Live credential diverges from Account-%s's stored backup "
                "and ownership could not be verified; self-switch left "
                "everything untouched (pre-fix no-op).",
                slot,
            )
            return "noop-diverged", None
        return "reconcile", provenance

    def _prefetch_live_identity(self) -> dict:
        """Resolve the live credential's owner BEFORE the locks are taken.

        The switch-time backup copies live credential bytes into the slot named
        by ``~/.claude.json`` — two files with independent writers. When they
        agree (bytes or refresh-token lineage match the slot's stored backup)
        no network is needed. When they diverge, only the API can say whose
        token the live bytes are (the credential blob carries no identity), and
        "no network while locks are held" forces that call to happen here.

        Returns ``{"live": str|None, "resolved": dict|None}``. ``resolved`` is
        only trustworthy while the live bytes haven't moved — the under-lock
        classifier re-checks byte equality before using it.
        """
        result: dict = {"live": None, "resolved": None}
        try:
            live = self._read_credentials()
        except Exception as e:
            self._logger.debug(f"Pre-lock live credential read failed: {e!r}")
            return result
        result["live"] = live
        if not live:
            return result
        identity = self._get_current_account()
        if identity is None:
            return result
        data = self._get_sequence_data() or {}
        slot = self._find_account_slot(data, identity[0], identity[1])
        if slot is None:
            return result
        backup = self._read_account_credentials(slot, identity[0])
        if backup == live or (
            oauth.credential_fingerprint(backup)
            == oauth.credential_fingerprint(live)
        ):
            return result  # provenance already established locally
        access_token = oauth.extract_access_token(live)
        if not access_token:
            return result  # raw API key / garbled JSON — nothing to resolve
        try:
            result["resolved"] = oauth.fetch_oauth_profile(access_token)
        except Exception as e:
            # fetch_oauth_profile swallows its own failures; this belt keeps
            # the invariant structural — the oracle is advisory and must
            # never fail a switch.
            self._logger.debug(f"Profile resolution raised: {e!r}")
        return result

    def _classify_outgoing_credential(
        self,
        current_account: str,
        current_email: str,
        original_creds: str,
        provenance: dict,
        data: dict,
    ) -> tuple[str, str | None]:
        """Decide what the switch-time backup may do with the live credential.

        Returns ``(kind, foreign_slot)``:

        - ``"own-bytes"``      — byte-identical to the slot's stored backup;
          nothing changed, nothing to capture.
        - ``"own-family"``     — same refresh-token lineage (access token
          rotated); back up normally.
        - ``"own-rotated"``    — full rotation, but the profile endpoint
          resolved the live token to this slot's identity; back up normally
          (the live→backup re-sync that keeps slots alive across Claude
          Code's routine refresh-token rotations).
        - ``"foreign"``        — uuid-positively resolved to *another* managed
          slot (``foreign_slot``) holding a different lineage; backing it up
          here would destroy this slot's only refresh token (issue #117's
          poisoning). Preserved in a safety copy, never written into any
          slot: identity proves ownership, not generation freshness.
        - ``"foreign-synced"`` — resolved to another managed slot whose
          stored backup already holds this exact lineage; nothing needs
          preserving, nothing may be written.
        - ``"alien"``          — a *structurally complete* identity (uuid +
          email + organization) that matches no managed slot (unmanaged
          login, recycled email wearing a managed address, or an email+org
          match without uuid confirmation). Preserved in a safety copy.
        - ``"unresolved"``     — mismatch and identity could not be
          established (offline, endpoint failure, malformed response, no
          access token in the blob, bytes moved since the pre-lock read) —
          or was only *partially* established: a response missing email or
          organization matching nothing is indistinguishable from schema
          drift, and preserve-and-skip on drift would silently recreate the
          fail-closed behavior this design forbids. The caller falls back to
          the exact pre-fix backup: the identity oracle is advisory, and
          endpoint state must never change switch behavior beyond skipping
          the extra safety.
        """
        backup = self._read_account_credentials(current_account, current_email)
        if backup and backup == original_creds:
            return ("own-bytes", None)
        if backup and (
            oauth.credential_fingerprint(backup)
            == oauth.credential_fingerprint(original_creds)
        ):
            return ("own-family", None)
        resolved = provenance.get("resolved")
        if resolved is None or provenance.get("live") != original_creds:
            return ("unresolved", None)
        r_email = resolved.get("email") or ""
        r_org = resolved.get("organizationUuid") or ""
        r_uuid = (resolved.get("uuid") or "").strip()
        # Outgoing-slot uuid match first: robust to partial responses (a
        # drifted schema may drop email/organization) and to an account
        # whose email changed. Organization must agree only when both sides
        # record one — the codebase's usual leniency for org matching.
        own = data.get("accounts", {}).get(current_account, {})
        own_uuid = (own.get("uuid") or "").strip()
        own_org = own.get("organizationUuid", "") or ""
        if r_uuid and own_uuid and r_uuid == own_uuid and (
            not r_org or not own_org or r_org == own_org
        ):
            return ("own-rotated", None)
        slot = self._find_account_slot(data, r_email, r_org) if r_email else None
        if slot is not None and r_uuid:
            # When both sides carry a uuid it must agree: an email+org match
            # with a conflicting uuid is a *different* account wearing a
            # recycled email (e.g. deleted/recreated claude.ai account), and
            # treating it as the slot would poison the slot's backup.
            stored_uuid = (
                data.get("accounts", {}).get(slot, {}).get("uuid") or ""
            ).strip()
            if stored_uuid and stored_uuid != r_uuid:
                slot = None
        if slot is None and r_uuid:
            # Fall back to the account uuid (org-scoped) in case the slot's
            # stored email is stale or synthesized (add-token placeholder).
            for num, acct in data.get("accounts", {}).items():
                if (
                    acct.get("uuid")
                    and acct.get("uuid") == r_uuid
                    and (acct.get("organizationUuid", "") or "") == r_org
                ):
                    slot = num
                    break
        if slot == current_account:
            return ("own-rotated", None)
        if slot is None:
            # A positive "alien" needs a structurally complete identity —
            # email plus organization — matching nothing. A partial one is
            # indistinguishable from schema drift and must fail open like
            # any other oracle degradation, not preserve-and-skip.
            if r_email and resolved.get("organizationUuid") is not None:
                return ("alien", None)
            return ("unresolved", None)
        # A cross-slot attribution must be uuid-positive: an email+org match
        # against a slot with no recorded uuid (add-token placeholder) is not
        # evidence enough to name that slot in user output — treat as alien.
        stored_uuid = (
            data.get("accounts", {}).get(slot, {}).get("uuid") or ""
        ).strip()
        if not r_uuid or stored_uuid != r_uuid:
            return ("alien", None)
        foreign_email = data.get("accounts", {}).get(slot, {}).get("email", "")
        foreign_backup = self._read_account_credentials(slot, foreign_email)
        if foreign_backup and (
            foreign_backup == original_creds
            or oauth.credential_fingerprint(foreign_backup)
            == oauth.credential_fingerprint(original_creds)
        ):
            return ("foreign-synced", slot)
        return ("foreign", slot)

    def _stash_live_credential(
        self,
        original_creds: str,
        reason: str,
        current_account: str,
        resolved: dict | None,
    ) -> str:
        """Preserve an unowned live credential before it is overwritten.

        Raises on failure — a successful stash is the license to overwrite the
        live store (the bytes may be the only live copy of some account's
        refresh token). The logged evidence doubles as the instrumentation for
        identifying what wrote the credential (#117's writer is unidentified).
        """
        creds_mtime: str | None = None
        try:
            mtime = get_credentials_path().stat().st_mtime
            from datetime import datetime, timezone

            creds_mtime = datetime.fromtimestamp(
                mtime, tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        except OSError:
            pass  # Keychain backend or file absent
        live_oauth_account: dict | None = None
        try:
            config = self._read_json(self._get_claude_config_path())
            if isinstance(config, dict):
                live_oauth_account = config.get("oauthAccount")
        except Exception:
            pass
        entry_id = self._store._write_unclaimed_credential(
            original_creds,
            {
                "reason": reason,
                "configSlot": current_account,
                "fingerprint": oauth.credential_fingerprint(original_creds),
                "liveOauthAccount": live_oauth_account,
                "resolvedIdentity": resolved,
                "credentialsMtime": creds_mtime,
            },
        )
        self._logger.warning(
            "Live credential does not belong to Account-%s (%s): stashed as %s "
            "(credentials mtime %s). Something outside cswap rewrote the live "
            "login after the last switch.",
            current_account,
            reason,
            entry_id,
            creds_mtime or "unknown",
        )
        return entry_id

    def _perform_switch(
        self,
        target_account: str,
        emit_output: bool = True,
        force_activate: bool = False,
        provenance: dict | None = None,
    ) -> dict:
        """Perform the actual account switch with transaction support.

        Returns ``{"from": ref|None, "to": ref, "warnings": [...]}``, capturing the
        left/landed identities under the lock so callers don't reconstruct ``from``
        after the mutation. When ``emit_output`` is False (JSON mode) all human
        output is suppressed — the live-session warning, the "Switched"/"Activated"
        lines, the nested list_accounts() summary and the followup — and the
        live-session warning rides back in ``warnings`` instead.

        ``force_activate`` routes through the direct activation path even when a
        managed live login exists: the stored backup is written over the live
        credentials without backing the live ones up first (post-import recovery
        when the live login is stale).

        The post-switch display runs after the lock releases so that persist
        callbacks inside list_accounts() can re-acquire it.
        """
        warnings_out: list[str] = []
        # Session-mode drift warning (warn, never block): switching the
        # default login to an account that also has a live session profile
        # puts the same refresh token in two config dirs — if the server
        # rotates it, one copy goes stale.
        pre_data = self._get_sequence_data() or {}
        pre_email = (
            pre_data.get("accounts", {}).get(target_account, {}).get("email", "")
        )
        if pre_email:
            pids = self._live_session_pids(target_account, pre_email)
            if pids:
                msg = (
                    f"Account-{target_account} ({pre_email}) has a live session-mode "
                    f"Claude instance (PID {', '.join(map(str, pids))}). Running the "
                    "same account as both the default login and a session can make "
                    "one copy's token go stale if the server rotates it. If the "
                    "session later fails to authenticate, exit it and re-run "
                    f"'cswap run {target_account}'."
                )
                if emit_output:
                    warning(msg)
                else:
                    warnings_out.append(msg)

        # Pre-lock identity resolution (may hit the network — must happen
        # before the locks). Callers that already resolved (self-switch
        # reconciliation) pass it in; force activation never backs up the
        # live credential so it skips the lookup.
        if provenance is None:
            provenance = (
                {"live": None, "resolved": None}
                if force_activate
                else self._prefetch_live_identity()
            )

        # Beyond cswap's own lock, hold Claude Code's advisory locks for the
        # whole mutation (including rollback paths): its token refresh runs
        # under ~/.claude.lock and re-reads credentials there — holding it
        # means a mid-refresh Claude Code either finishes before our swap
        # (backup captures the rotated token) or re-checks after it and aborts.
        # ~/.claude.json.lock likewise keeps the oauthAccount splice from
        # interleaving with Claude Code's own config writes. Everything under
        # here is local I/O — no network while locks are held.
        with FileLock(self.lock_file), claude_credentials_lock(), claude_config_lock():
            data = self._get_sequence_data()
            active_account = data.get("activeAccountNumber")
            current_account = str(active_account) if active_account is not None else None
            target_email = data["accounts"][target_account]["email"]
            to_ref = account_ref(int(target_account), target_email)
            current_identity = self._get_current_account()
            if current_identity is not None:
                current_email, current_org_uuid = current_identity
                current_account = self._find_account_slot(
                    data, current_email, current_org_uuid
                )

            config_path = self._get_claude_config_path()

            # Direct activation path: there is no live Claude session yet
            # (e.g. right after import), claude-swap has no tracked active
            # account yet (e.g. purge -> add-token -> switch-to while a live
            # Claude credential still exists), or --force asked to rewrite the
            # live login from the stored backup. In all cases, skip the
            # back-up-current step: it would either write account-None-*
            # backups or (force) poison the stored backup with stale creds.
            if force_activate or current_identity is None or current_account is None:
                # Account left: None on a fresh machine (no live account at
                # all); an unnumbered ref for an unmanaged live account (slot
                # unknown to cswap); a numbered ref when --force ran with a
                # managed live login.
                if current_identity is None:
                    from_ref = None
                elif current_account is None:
                    from_ref = account_ref(None, current_identity[0])
                else:
                    from_ref = account_ref(int(current_account), current_identity[0])
                target_creds = self._read_account_credentials(
                    target_account, target_email
                )
                target_config = self._read_account_config(target_account, target_email)
                if not target_creds:
                    raise SwitchError(
                        f"Account-{target_account} has no stored credentials. "
                        f"Re-add with: cswap --add-account --slot {target_account}"
                    )
                if not target_config:
                    raise SwitchError(
                        f"Account-{target_account} has no stored config backup. "
                        f"Re-add with: cswap --add-account --slot {target_account}"
                    )
                try:
                    target_config_data = json.loads(target_config)
                except json.JSONDecodeError as exc:
                    raise SwitchError(f"Invalid backup config: {exc}")
                target_oauth = target_config_data.get("oauthAccount")
                if not target_oauth:
                    raise SwitchError("Invalid oauthAccount in backup")

                # Snapshot live state so a mid-operation failure can be undone.
                # When a live session exists, fail fast if the snapshot is
                # unreadable rather than proceeding to overwrite without a
                # safety net. The fresh-machine case has nothing to restore.
                rollback_creds: str | None = None
                rollback_config_text: str | None = None
                if current_identity is not None:
                    rollback_creds = self._read_credentials()
                    if rollback_creds is None:
                        raise CredentialReadError(
                            "Cannot snapshot live credentials before activation"
                        )
                    if config_path.exists():
                        try:
                            rollback_config_text = config_path.read_text(
                                encoding="utf-8"
                            )
                        except OSError as e:
                            raise ConfigError(
                                f"Cannot snapshot live config before activation: {e}"
                            )

                # Invariant II (issue #117): this path skips the backup step,
                # so the live credential it replaces would otherwise have no
                # surviving copy — stash it first. For an unmanaged login the
                # stash is the only copy anywhere; for --force it guards
                # against the "stale" live login actually being the fresher
                # generation. A failed stash aborts, except under --force
                # where the user explicitly asked for the overwrite.
                if (
                    rollback_creds
                    and rollback_creds != target_creds
                    and current_identity is not None
                ):
                    try:
                        self._stash_live_credential(
                            rollback_creds,
                            "displaced-live-login",
                            current_account or "unmanaged",
                            None,
                        )
                    except Exception as e:
                        if not force_activate:
                            raise SwitchError(
                                "Could not preserve the live credential before "
                                f"activation (safety-copy write failed: {e}); "
                                "aborting rather than destroying it"
                            )
                        msg = (
                            "Could not preserve the replaced live credential "
                            f"(safety-copy write failed: {e}) — proceeding "
                            "because --force explicitly rewrites the live login."
                        )
                        if emit_output:
                            warning(msg)
                        else:
                            warnings_out.append(msg)

                creds_written = False
                config_written = False
                try:
                    self._write_credentials(target_creds)
                    creds_written = True

                    # Mirror the normal switch path: preserve existing local
                    # settings/projects when ~/.claude.json already exists, only
                    # swapping in oauthAccount. Fall back to the full imported
                    # config when no usable local config exists.
                    existing_config = (
                        self._read_json(config_path) if config_path.exists() else None
                    )
                    if existing_config:
                        existing_config["oauthAccount"] = target_oauth
                        self._write_json(config_path, existing_config)
                    else:
                        self._write_json(config_path, target_config_data)
                    config_written = True

                    data["activeAccountNumber"] = int(target_account)
                    data["lastUpdated"] = get_timestamp()
                    self._write_json(self.sequence_file, data)
                except Exception:
                    if config_written and rollback_config_text is not None:
                        try:
                            config_path.write_text(
                                rollback_config_text, encoding="utf-8"
                            )
                            if sys.platform != "win32":
                                os.chmod(config_path, 0o600)
                        except Exception as e:
                            self._logger.error(
                                f"Failed to rollback config: {e}"
                            )
                    if creds_written and rollback_creds is not None:
                        try:
                            self._write_credentials(rollback_creds)
                        except Exception as e:
                            self._logger.error(
                                f"Failed to rollback credentials: {e}"
                            )
                    raise

                if force_activate and current_identity is not None:
                    self._logger.info(
                        f"Activated account {target_account} "
                        "(forced, backup of current login skipped)"
                    )
                else:
                    self._logger.info(
                        f"Activated account {target_account} (no prior live account)"
                    )
                if emit_output:
                    print(
                        f"{accent('Activated')} Account-{target_account} ({target_email})"
                    )
                    print()
                    self._print_switch_followup()
                    print()
                self._replan_new_active(
                    target_account,
                    target_email,
                    data["accounts"][target_account].get("organizationUuid", ""),
                )
                return {"from": from_ref, "to": to_ref, "warnings": warnings_out}

            current_email, _ = current_identity
            from_ref = account_ref(int(current_account), current_email)

            # Create transaction for rollback capability
            try:
                original_creds = self._read_credentials()
                if original_creds is None:
                    raise CredentialReadError("Failed to read current credentials")
                if not original_creds:
                    # An empty read (e.g. a macOS Keychain `security` timeout,
                    # which returns "" rather than raising) must NOT be written
                    # over the departing account's backup — that would destroy
                    # its stored credential. Fail the switch; the backup stays
                    # intact and the caller can retry once the Keychain settles.
                    raise CredentialReadError(
                        "Current account credential is empty (Keychain unreadable?); "
                        "refusing to overwrite its backup"
                    )
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
                # Step 1: Backup current account. Position in ~/.claude.json
                # says which slot is active; only the classification says who
                # owns the live bytes (issue #117: an external write here
                # used to destroy the outgoing slot's refresh token). The
                # identity oracle is strictly advisory — "unresolved" falls
                # back to the exact pre-fix backup, so endpoint state never
                # decides whether a switch completes.
                kind, foreign_slot = self._classify_outgoing_credential(
                    current_account, current_email, original_creds,
                    provenance, data,
                )
                if kind in ("foreign", "alien"):
                    # Positively not this slot's bytes: never into a slot;
                    # never silently destroyed. The safety copy (which raises
                    # on failure, aborting before the live store is
                    # overwritten) is the license to proceed.
                    self._stash_live_credential(
                        original_creds, kind, current_account,
                        provenance.get("resolved"),
                    )
                    if kind == "foreign":
                        msg = (
                            "Credential ownership mismatch detected. The live "
                            "credential was preserved and was not written "
                            f"into Account-{current_account}. If Account-"
                            f"{foreign_slot} later cannot authenticate, log "
                            "in as it and run: cswap add --slot "
                            f"{foreign_slot}"
                        )
                    else:
                        msg = (
                            "The live login does not match a managed "
                            "account. It was preserved and not written into "
                            f"Account-{current_account}. If you need that "
                            "account, log in as it and run: cswap add"
                        )
                    if emit_output:
                        warning(msg)
                    else:
                        warnings_out.append(msg)
                elif kind == "foreign-synced":
                    # Another managed account's bytes, and that slot already
                    # holds this lineage — nothing needs preserving, nothing
                    # may be written.
                    msg = (
                        "Credential ownership mismatch detected. The live "
                        f"credential already matches Account-{foreign_slot}'s "
                        "stored backup, so nothing was written into "
                        f"Account-{current_account}."
                    )
                    if emit_output:
                        warning(msg)
                    else:
                        warnings_out.append(msg)
                elif kind == "unresolved":
                    # Ownership could not be established (offline, endpoint
                    # failure, malformed response, non-OAuth blob). Fail
                    # open: exact pre-fix backup. Most such divergences are
                    # the account's own rotation — skipping the backup would
                    # leave the slot holding a consumed token — and the
                    # .prev retention inside the write gives even a wrong
                    # call a best-effort recovery cushion. Log only:
                    # indistinguishable from a legitimate rotation, so a
                    # warning would cry wolf.
                    self._write_account_credentials(
                        current_account, current_email, original_creds
                    )
                    self._write_account_config(
                        current_account, current_email, original_config
                    )
                    self._logger.info(
                        f"Backed up account {current_account} (lineage "
                        "differs from the stored backup and ownership could "
                        "not be verified — pre-fix backup)"
                    )
                elif kind == "own-bytes":
                    # Untouched since cswap wrote it — the slot already holds
                    # these bytes. Refresh only the config backup.
                    self._write_account_config(
                        current_account, current_email, original_config
                    )
                    self._logger.info(
                        f"Backed up account {current_account} (config only; "
                        "credentials unchanged)"
                    )
                else:  # own-family / own-rotated
                    self._write_account_credentials(
                        current_account, current_email, original_creds
                    )
                    self._write_account_config(
                        current_account, current_email, original_config
                    )
                    if kind == "own-rotated":
                        # The profile call proved the identity; backfill a
                        # missing slot uuid (add-token placeholder) while the
                        # sequence file is being rewritten anyway.
                        resolved = provenance.get("resolved") or {}
                        acct = data.get("accounts", {}).get(current_account, {})
                        if not acct.get("uuid") and resolved.get("uuid"):
                            acct["uuid"] = resolved["uuid"]
                    self._logger.info(f"Backed up account {current_account}")

                # Step 2: Retrieve target account
                target_creds = self._read_account_credentials(
                    target_account, target_email
                )
                target_config = self._read_account_config(target_account, target_email)

                if not target_creds:
                    raise SwitchError(
                        f"Account-{target_account} has no stored credentials. "
                        f"Re-add with: cswap --add-account --slot {target_account}"
                    )
                if not target_config:
                    raise SwitchError(
                        f"Account-{target_account} has no stored config backup. "
                        f"Re-add with: cswap --add-account --slot {target_account}"
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
        # re-acquire the lock from inside list_accounts(). All of this is display
        # only — suppressed in JSON mode (the nested list_accounts() would
        # otherwise leak human output onto the JSON stdout).
        if emit_output:
            print(f"{accent('Switched to')} Account-{target_account} ({target_email})")
            try:
                self.list_accounts()
            except Exception as e:
                self._logger.warning(f"Post-switch usage display failed: {e!r}")
                print(dimmed("  (usage display unavailable — run `cswap --list` to retry)"))
            print()
            self._print_switch_followup()
            print()
        self._replan_new_active(
            target_account,
            target_email,
            data["accounts"][target_account].get("organizationUuid", ""),
        )
        return {"from": from_ref, "to": to_ref, "warnings": warnings_out}

    def _print_switch_followup(self) -> None:
        """Print the note after a successful switch, keyed to where the active
        credential write actually landed.

        A restart is never required: Claude Code clears its cached OAuth token when
        ``.credentials.json`` changes (file storage — effective on the next message)
        or when the macOS Keychain cache TTL (~30s) expires. Both lines are dim
        hints, not warnings; the Keychain line adds that a restart skips the wait.
        The file line also covers macOS when the Keychain was unavailable and the
        switch fell back to the file.
        """
        backend = self._last_active_credentials_backend
        if backend is None:
            # No write happened this run; fall back to the routing hint.
            backend = "keychain" if self._use_keychain() else "file"
        if backend == "keychain":
            print(dimmed(
                "Restart Claude Code to apply immediately — otherwise the "
                "session can take up to ~30 seconds to pick up the new account."
            ))
        else:
            print(dimmed("New account is active on your next message — no restart needed."))

    def purge(self) -> None:
        """Remove all traces of claude-swap from the system.

        This removes:
        - All stored account credentials (``.enc`` files on Linux/WSL/Windows; on
          macOS both the Keychain items via ``security`` and any fallback ``.enc``
          files), plus a best-effort sweep of any pre-migration keyring / Windows
          Credential Manager entries left behind
        - The active backup directory (XDG path on Linux/WSL, ~/.claude-swap-backup elsewhere)
        - Any stale legacy ~/.claude-swap-backup directory left around from
          before the XDG migration
        """
        legacy = get_legacy_backup_root()
        legacy_distinct = legacy != self.backup_dir

        # Refuse while any session-mode claude is running: purging would pull
        # its profile (and keychain entry) out from under a live process.
        sessions_root = self.backup_dir / "sessions"
        session_dirs = (
            [d for d in sessions_root.iterdir() if d.is_dir()]
            if sessions_root.is_dir()
            else []
        )
        from claude_swap.session import live_sessions_for

        live = {}
        for d in session_dirs:
            pids = [s.pid for s in live_sessions_for(d)]
            if pids:
                live[d.name] = pids
        if live:
            details = "; ".join(
                f"{name} (PID {', '.join(map(str, pids))})"
                for name, pids in live.items()
            )
            raise SessionError(
                f"Live session-mode Claude instance(s) found: {details}. "
                "Exit them first, then retry --purge."
            )

        warning("This will remove ALL claude-swap data from your system:")
        print(f"  - Backup directory: {self.backup_dir}")
        if legacy_distinct and legacy.exists():
            print(f"  - Legacy backup directory: {legacy}")
        if self.platform == Platform.MACOS:
            print("  - All stored account credentials (macOS Keychain and/or files)")
        else:
            print("  - All stored account credential files")
        if session_dirs:
            print("  - All session profiles and their Keychain entries")
        print()
        print(dimmed("Note: This does NOT affect your current Claude Code login."))
        print()

        confirm = input("Are you sure you want to purge all data? [y/N] ")
        if confirm.lower() != "y":
            print(dimmed("Cancelled"))
            return

        removed_items = []

        # Remove credentials. On macOS backups may be in the Keychain and/or .enc
        # files (auto-fallback), so clean both; Linux/WSL/Windows are file-only.
        data = self._get_sequence_data()
        if data:
            for account_num, account_info in data.get("accounts", {}).items():
                email = account_info.get("email", "")
                nums = [account_num]
                if str(account_num) != "None":
                    nums.append("None")
                usernames = [f"account-{num}-{email}" for num in nums]

                # .enc files (Linux/WSL/Windows always; macOS fallback copies).
                for num in nums:
                    cred_file = self.credentials_dir / f".creds-{num}-{email}.enc"
                    try:
                        if cred_file.exists():
                            cred_file.unlink()
                            removed_items.append(f"Credential file: {cred_file.name}")
                    except Exception:
                        pass  # Ignore errors during purge

                # macOS Keychain items via `security` (current macOS backend).
                if self.platform == Platform.MACOS:
                    for username in usernames:
                        try:
                            macos_keychain.delete_password(SECURITY_SERVICE, username)
                            removed_items.append(f"Credential: {username}")
                        except Exception:
                            pass  # Ignore errors during purge

                # Best-effort sweep of any pre-migration keyring / Credential
                # Manager entries left behind by an incomplete keyring → files
                # (Windows) or keyring → security (macOS) migration. Linux/WSL
                # never used a keyring backend.
                if self.platform in (Platform.MACOS, Platform.WINDOWS):
                    _sweep_legacy_keyring(usernames, removed_items)

        # Session-profile keychain entries must go BEFORE the backup dir:
        # the hashed service names are derived from the dir paths and can't
        # be recomputed once the directories are deleted.
        if session_dirs:
            from claude_swap.session import delete_macos_keychain_entry

            for d in session_dirs:
                delete_macos_keychain_entry(d)
            removed_items.append(
                f"Session profiles: {', '.join(d.name for d in session_dirs)}"
            )

        # Remove backup directory
        if self.backup_dir.exists():
            # Close log handlers before deleting (required on Windows)
            for handler in self._logger.handlers[:]:
                handler.close()
                self._logger.removeHandler(handler)

            shutil.rmtree(self.backup_dir)
            removed_items.append(f"Directory: {self.backup_dir}")

        # Also clean a stale legacy directory if it somehow still exists
        # (e.g. a partial pre-migration state, or files re-created after init).
        if legacy_distinct and legacy.exists():
            try:
                shutil.rmtree(legacy)
                removed_items.append(f"Legacy directory: {legacy}")
            except OSError:
                pass

        if removed_items:
            print(f"\n{accent('Removed:')}")
            for item in removed_items:
                print(f"  {dimmed('-')} {item}")
        else:
            print(f"\n{dimmed('No claude-swap data found to remove.')}")

        print(f"\n{accent('Purge complete.')}")
