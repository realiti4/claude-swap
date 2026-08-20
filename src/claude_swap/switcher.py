"""Core account switcher logic for Claude Code."""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import shutil
import threading
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from claude_swap import macos_keychain

from claude_swap.exceptions import (
    AccountNotFoundError,
    ConfigError,
    CredentialReadError,
    LockError,
    SessionError,
    SwitchError,
    ValidationError,
)
from claude_swap import oauth, pace
from claude_swap.claude_locks import claude_config_lock, claude_credentials_lock
from claude_swap.json_output import (
    SCHEMA_VERSION,
    USAGE_API_KEY,
    USAGE_FOREIGN_CREDENTIAL,
    USAGE_KEYCHAIN_UNAVAILABLE,
    USAGE_NO_CREDENTIALS,
    USAGE_RELOGIN_REQUIRED,
    USAGE_TOKEN_EXPIRED,
    account_ref,
    account_row,
    last_good_usage_fields,
    usage_fields,
    usage_freshness_fields,
)
from claude_swap.credentials import (  # noqa: F401  (constants re-exported for migrations/tests)
    CLAUDE_CODE_KEYCHAIN_SERVICE,
    SECURITY_SERVICE,
    ActiveCredentials,
    CredentialStore,
    looks_like_api_key,
    merge_shared_credential_fields,
    shared_credential_fields,
)
from claude_swap.fsutil import read_text_with_retry
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
    get_default_claude_config_home,
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


def _pace_marker(window: dict, fetched_at: float | None) -> str:
    """"  (ahead of pace)" when a weekly window is meaningfully ahead of pace, else ""."""
    result = pace.compute_pace(window, fetched_at=fetched_at)
    return "  (ahead of pace)" if result and result.ahead else ""


def _format_usage_lines(usage: dict, fetched_at: float | None = None) -> list[str]:
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
            # Pace only applies to the weekly (7d) window, never 5h (issue #125).
            marker = _pace_marker(w, fetched_at) if label == "7d" else ""
            cell = oauth.fresh_reset_strings(w)
            if cell:
                countdown, clock = cell
                rows.append((label, f"{w['pct']:>3.0f}%   resets {clock:<12}  in {countdown}{marker}"))
            else:
                rows.append((label, f"{w['pct']:>3.0f}%{marker}"))
    for w in usage.get("scoped") or []:
        # Per-model weekly limits (e.g. Fable). Flag ones at/over the limit so a
        # maxed model — the usual reason to switch — stands out.
        marker = "  (!)" if w["pct"] >= 100 else _pace_marker(w, fetched_at)
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
# Friendly text for error KINDS that deserve an explanation beyond their
# identifier (rendered in the "usage unavailable (…)" detail line).
# Stash reasons that mean the slot was NOT freshened, so `error is None`
# would lie to the caller. The other two are excluded deliberately: a REMOVED
# slot has nothing left to activate, and a CAS CONFLICT left the slot holding
# a racing writer's newer valid lineage — freshened, which is the opposite of
# what this demotion denies. An UNREADABLE store is neither: the CAS could not
# be evaluated at all, so the slot may still hold the spent generation.
_DEMOTING_STASH_REASONS = (
    "consume-gate-persist-failed",
    "consume-gate-persist-lock-failed",
    "consume-gate-unpersisted",
    "consume-gate-store-unreadable",
)

ERROR_NOTES = {
    "store-unmirrored": (
        "CLAUDE_SECURESTORAGE_CONFIG_DIR set — unset it or run from a "
        "normal shell"
    ),
    "invalid_client": (
        "cswap's OAuth client was rejected — systemic, not this account"
    ),
    "consume-busy": (
        "another cswap surface holds the slot — retries next pass"
    ),
    "stash-unreadable": (
        "this slot's stashed successor is unreadable — unlock the keychain "
        "or fix the file, then retry; `cswap unclaimed` inspects it"
    ),
}

SENTINEL_NOTES = {
    USAGE_TOKEN_EXPIRED: "token expired — refresh deferred this pass; retries automatically",
    USAGE_FOREIGN_CREDENTIAL: "live credential belongs to another account — a switch repairs it",
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
        lines = _format_usage_lines(entry.last_good, entry.fetched_at)
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
        detail += f" ({ERROR_NOTES.get(entry.last_error, entry.last_error)})"
    return [dimmed(detail)]


def _label_token_status(source: str, credentials: str) -> str | None:
    """Return ``oauth.build_token_status`` relabelled by credential source."""
    status = oauth.build_token_status(credentials)
    if status is None:
        return None
    prefix = "oauth: "
    if status.startswith(prefix):
        return f"{source}: {status.removeprefix(prefix)}"
    return f"{source}: {status}"


def _same_directory(left: Path, right: Path) -> bool:
    """Whether two paths name the same directory, symlinks and ``..`` included.

    Resolved rather than compared as strings: a ``$HOME`` reached through a
    symlink spells the same directory two ways, and the caller is deciding
    which profile a path belongs to — not deriving a keychain service name,
    where claude hashes the raw value and resolving would be wrong.
    """
    try:
        return left.resolve() == right.resolve()
    except OSError:  # unreadable mount / permission — compare as written
        return left == right


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

        # The active read's verdict, PER THREAD. Set by _build_accounts_info
        # from the active slot's own read; consumed later by the usage
        # sentinel, the rotation resync and the consume gate.
        #
        # Thread-local: a fact about one READ, and the TUI runs two lanes on
        # one switcher (`tui/app.py` starts a store refresh while a normal one
        # is in flight). A build's unconditional reset erased the other lane's
        # verdict — measured against a 4ms window, 60 of 60 lost, after which
        # the consume gate POSTs a possibly-spent grant.
        self._active_verdict_tls = threading.local()

        # Accounts already warned about a provenance problem with the active
        # credential — each condition persists across collect passes and
        # would otherwise log every tick. Cleared when its condition clears.
        # Keyed by (slot, email, reason) so a slot reused for a different
        # account in a long-lived process warns afresh and distinct
        # conditions don't suppress each other's warning.
        self._provenance_warned: set[tuple[str, str, str]] = set()

        # Definitive ownership verdicts for credential lineages, keyed by
        # _lineage_key (slot, caller email, stored email, org, uuid,
        # refresh-lineage fingerprint — the full slot identity, so a slot
        # re-created for a different account never inherits its
        # predecessor's verdicts):
        # True when the profile oracle resolved the lineage to the slot's
        # identity (or we produced it ourselves with a refresh POST), False
        # when it resolved to a foreign identity. Probe failures and
        # unverifiable results are never cached — a False on partial
        # evidence would permanently block a legitimate resync in a
        # long-lived process. In-memory only; the locked refresh paths
        # consult it because network under locks is forbidden, and it keeps
        # a persistent drift state from re-probing the profile endpoint
        # every collect pass.
        self._probe_verdicts: dict[
            tuple[str, str, str, str, str, str], bool
        ] = {}

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

    def _read_json(self, path: Path, *, strict: bool = False) -> dict | None:
        """Read and parse a JSON file. None when the file is ABSENT.

        With ``strict=True``, raises ``ConfigError`` when the file is THERE
        but unreadable — the distinction ``_read_global_config``'s callers
        keep having to make, and the one ~25 ``or {}`` call sites here were
        silently collapsing. Default False so the reader stays a reader:
        upstream's import path DELIBERATELY replaces a malformed config it is
        about to seed (`test_clean_switch_fallback_when_local_config_malformed`),
        and a blanket refusal would flip that intent.

        Measured on the plain switch path: a torn ``~/.claude.json`` read as
        None, fell to the `else` branch at :5954, and the 1-key backup config
        was written over the user's whole file — `projects`, `mcpServers`,
        `userID` gone, `switched: True` returned. Absent is a genuine empty
        start; unreadable is a file we must not overwrite unread.

        Also rejects a non-dict payload. ``json.loads`` happily returns a str
        or an int for a file holding `"hello"` or `123`, and every caller then
        fails on `.get` with a raw AttributeError that escapes
        ``ClaudeSwitchError``. ``_read_global_config`` already ends with the
        same isinstance check; the two readers of the same file disagreed.
        """
        if not path.exists():
            return None
        try:
            data = json.loads(read_text_with_retry(path))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            self._logger.warning(f"Invalid JSON in {path}")
            if strict:
                raise ConfigError(
                    f"{path} exists but could not be parsed ({e}). Repair or "
                    "move it, then retry — refusing to overwrite it unread."
                ) from e
            return None
        except OSError as e:
            self._logger.warning(f"Could not read {path}: {e}")
            if strict:
                raise ConfigError(
                    f"{path} exists but could not be read ({e}). Fix what is "
                    "blocking the read, then retry."
                ) from e
            return None
        if not isinstance(data, dict):
            self._logger.warning(
                f"{path} holds {type(data).__name__}, not a JSON object"
            )
            if strict:
                raise ConfigError(
                    f"{path} holds {type(data).__name__}, not a JSON object. "
                    "Repair or move it, then retry."
                )
            return None
        return data

    def _salvage_unreadable(
        self, path: Path, emit_output: bool, warnings_out: list[str]
    ) -> Path:
        """Copy an unreadable file aside before it is replaced. Returns the copy.

        Three things the first cut got wrong, all of them the same promise —
        THE BYTES SURVIVE AND THE USER KNOWS:

        MODE. `shutil.copy2` preserves the source mode. Measured on a 0644
        `~/.claude.json` holding `primaryApiKey`: the replacement got 0600 from
        `_write_json` and the salvage stayed 0644, so the secret ended up
        world-readable in a file cswap created. Copied without metadata and
        chmod'ed 0600 explicitly.

        COLLISION. The stamp is second-resolution and `copy2` onto an existing
        name overwrites. Two failed switches inside one second left ONE file —
        measured, the first user's data unrecoverable. The retry is exactly
        what a user does next, so the guard lost the bytes precisely when it
        was needed. A counter suffix makes each copy its own file.

        NAME. The first cut stamped with `get_timestamp()`, whose ISO form
        carries `:` — forbidden in a Windows filename. Measured on CI (run
        30774451162): five tests died with `[Errno 22] Invalid argument`, the
        copy raised, and the switch ABORTED, which is worse than the data loss
        this guard exists to prevent. `int(time.time())` is what
        `credentials.py`'s sibling `.corrupt-` aside already uses; reusing it
        keeps one convention rather than inventing a third.

        VISIBILITY. `warnings_out` is only rendered by the JSON envelope. In
        human mode the user saw "Activated Account-1" and nothing else while
        their `projects`/`mcpServers` were gone from the live config — every
        other `warnings_out.append` in `_perform_switch` is paired with an
        `if emit_output: warning(msg)`; this one was not.
        """
        stem = f"{path.name}.unreadable-{int(time.time())}"
        salvage = path.with_name(stem)
        n = 1
        while salvage.exists():
            salvage = path.with_name(f"{stem}.{n}")
            n += 1
        try:
            shutil.copy(path, salvage)          # NOT copy2: mode is set below
            if sys.platform != "win32":
                os.chmod(salvage, 0o600)
        except OSError as e:
            raise SwitchError(
                f"{path} could not be parsed and the salvage copy failed "
                f"({e}); aborting rather than destroying it"
            )
        msg = (
            f"{path.name} could not be parsed — a copy was kept at "
            f"{salvage.name}"
        )
        self._logger.warning(f"{path} could not be parsed; a copy was kept at "
                             f"{salvage} before it was replaced")
        warnings_out.append(msg)
        if emit_output:
            warning(msg)
        return salvage

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

        # Permissions go on the temp file so the rename below is the final,
        # atomic commit: nothing can fail after the file is published (a
        # chmod on the final path could raise with the write already live,
        # making callers roll back around committed metadata).
        if sys.platform != "win32":
            os.chmod(temp_path, 0o600)
        shutil.move(str(temp_path), str(path))

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

    def _read_default_profile_credentials(self) -> ActiveCredentials:
        return self._store._read_default_profile_credentials()

    def _refuse_degraded_capture(self) -> str | None:
        """Refuse to CAPTURE bytes a degraded read produced.

        Both env-var branches of :meth:`_read_capture_credentials` pass
        ``strict_keychain=True`` — an unreadable (not absent) Keychain raises
        rather than silently capturing the plaintext seed, which on macOS may
        be the consumed predecessor because Claude Code rotates keychain-only.

        The DEFAULT path is the most-used one and had no such guard: it reads
        through ``_read_credentials``, which is ``_read_active_credentials()``
        with ``degraded`` discarded. So a locked-keychain ``cswap add``
        captured the possibly-spent fallback into the slot backup, and
        ``add_account`` then cleared the dead-token strike — re-creating on the
        common path exactly the stale-consume this PR exists to prevent.

        I-1 (round 9): returns the value THIS read produced so the caller
        captures those exact bytes instead of reading again. The check-read
        and a separate use-read are two independent Keychain reads — a
        Keychain that answers the first and fails the second passes the
        guard and then captures the possibly-stale plaintext fallback
        anyway, which is precisely the outcome this guard exists to prevent.
        """
        active = self._read_active_credentials()
        if active.degraded:
            raise CredentialReadError(
                "The macOS Keychain is unreadable right now (locked or no GUI "
                "session), so the only readable credential is a plaintext "
                "fallback that may be a superseded generation — capturing it "
                "would file a spent refresh token against this slot. Retry "
                "from a GUI terminal."
            )
        return active.value

    def _read_capture_credentials(self) -> str | None:
        """Read the credential of the profile the environment points at.

        ``add_account`` fills one slot from two reads: the identity out of
        ``.claude.json`` and the credential out of the active store. The active
        store's file backend follows ``CLAUDE_CONFIG_DIR`` (through
        ``get_claude_config_home``), but its macOS Keychain backend is pinned to
        the unsuffixed ``CLAUDE_CODE_KEYCHAIN_SERVICE``. So on macOS the two
        reads land in different profiles and the slot ends up holding one
        account's email against another account's token.

        Read the OAuth credential the way claude resolves it for the same
        environment, so the slot's email and token come from one profile.
        Claude (2.1.220 ``getMacOsKeychainStorageServiceName``/storage-path
        resolution) sources secure storage from ``CLAUDE_SECURESTORAGE_CONFIG_DIR``
        when that is *defined*, else ``CLAUDE_CONFIG_DIR`` — with defined-but-empty
        meaning the *default secure store* (unsuffixed Keychain item,
        ``~/.claude/.credentials.json``). Not the active store: its file backend
        follows ``CLAUDE_CONFIG_DIR``, which may point elsewhere. Identity stays
        on ``CLAUDE_CONFIG_DIR`` either way; only the credential read moves.

        Two fallbacks stay inside that profile. An env var naming the default
        profile means the active store, since a user exporting
        ``CLAUDE_CONFIG_DIR=~/.claude`` may only have the unsuffixed item.
        A managed API key sits outside any OAuth store, and
        :meth:`_reject_live_api_key_capture` still has to answer for one.

        Strict on the keychain: an unreadable (not absent) entry raises
        :class:`CredentialReadError` rather than silently capturing the
        profile's possibly-stale plaintext seed — and rather than reaching the
        fallbacks below, which belong to other stores entirely.

        Read-only. cswap does not write claude's hashed keychain entry — see
        the ``session`` module docstring for why.
        """
        from claude_swap.session import read_config_dir_credentials

        secure_env = os.environ.get("CLAUDE_SECURESTORAGE_CONFIG_DIR")
        config_dir = os.environ.get("CLAUDE_CONFIG_DIR")

        if secure_env is not None:
            # A defined override names the *only* store claude will read for
            # this environment — defined-but-empty pins the default profile
            # (unsuffixed keychain item, ``~/.claude/.credentials.json``). On
            # a miss claude sees a logged-out environment, so no falling back
            # into the active store: its file backend follows
            # ``CLAUDE_CONFIG_DIR``, and with the two vars diverged that would
            # capture a profile claude is not reading (cross-profile leak).
            creds = read_config_dir_credentials(
                secure_env or str(get_default_claude_config_home()),
                strict_keychain=True,
                keychain_service=CLAUDE_CODE_KEYCHAIN_SERVICE if not secure_env else None,
            )
            if creds:
                return creds
            # The tail below is not a continuation of this branch: it reads
            # ``primaryApiKey`` through ``get_global_config_path()``, which
            # follows ``CLAUDE_CONFIG_DIR``. With the two vars diverged that
            # is the cross-profile capture this branch just refused — and it
            # cannot be reached by the secure profile's OWN key either, since
            # ``read_config_dir_credentials`` is OAuth-only and never looks at
            # ``primaryApiKey``. A miss here is what claude sees: logged out.
            return ""
        elif not config_dir:
            return self._refuse_degraded_capture()
        else:
            creds = read_config_dir_credentials(config_dir, strict_keychain=True)
            if creds:
                return creds
            if _same_directory(Path(config_dir), get_default_claude_config_home()):
                # Safe only on this legacy path: the active store's env-following
                # file backend and the default profile coincide here.
                return self._refuse_degraded_capture()
        # Only this profile's own ``primaryApiKey`` — never the unsuffixed
        # "Claude Code" Keychain item, which belongs to the default profile
        # and would answer for a login that is not the one being added.
        key = (self._read_json(get_global_config_path()) or {}).get("primaryApiKey")
        return key if isinstance(key, str) else ""

    def _write_credentials(self, credentials: str) -> None:
        self._store._write_credentials(credentials)

    def _prepare_credentials_for_activation(
        self, target_credentials: str, live_credentials: str | None
    ) -> str:
        """Compose the credential to activate from its two owners.

        The machine-shared OAuth integrations (the ``SHARED_CREDENTIAL_KEYS``
        allowlist, notably ``mcpOAuth``) are frozen in the slot at backup
        time and may hold rotated-out refresh tokens, while the live
        credential's copies are by definition the current generation — so
        for those keys the live credential wins, absence included. Every
        other field the destination slot stored travels with the slot:
        account-bound state such as ``trustedDeviceToken`` — and any field
        cswap does not recognize — must not leak across an account switch.

        When there is no live JSON credential object to take shared fields
        from (fresh machine, or a managed API key is active), the stored
        blob activates unchanged, exactly as before.
        """
        live_shared = shared_credential_fields(live_credentials)
        if live_shared is None:
            return target_credentials
        return merge_shared_credential_fields(target_credentials, live_shared)

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

            if not mark_session_stale(self._session_dir(account_num, email)):
                self._logger.error(
                    "Account %s's backup credentials changed but its live "
                    "session profile could not be marked stale; it may keep "
                    "serving the superseded generation once it exits.",
                    account_num,
                )
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

        PAST THE STORE WRITE, NOTHING MAY RAISE. The write ADVANCES the slot,
        and every caller reads an exception from this method as "the persist
        failed" — so a raise here reports a failure for a slot that holds the
        new credential. At the post-POST call site that is worse than losing
        the invalidation: the grant is already spent, the handler stashes a
        "successor" byte-identical to what the store now holds, and a
        successful refresh is demoted to ``transient`` (measured: the tick then
        emits "could not freshen any candidate (network?)" forever over a
        healthy slot, and ``cswap run`` prints "Could not refresh the token").

        So the invalidation is contained, and its failure LEAVES THE MARKER
        instead. That is not a downgrade: a profile whose access token is still
        unexpired passes the local reuse check, so simply skipping the
        invalidation would let it keep serving a superseded generation until it
        expires. ``STALE_MARKER`` is what forces the re-bootstrap regardless,
        and it is the same mechanism the live-session branch already relies on.

        ``OSError``, not ``Exception``. EACCES on the session dir and a
        read-only mount is the whole fault list above, and both are
        ``OSError``; the suite's own real-store guard is deliberately NOT an
        ``OSError`` subclass (``tests/conftest.py``) so that no containment in
        this codebase can hide a write into the REAL store. Widening this to
        ``Exception`` disarmed exactly that guard for every write routing
        through here.
        """
        self._store._write_account_credentials(account_num, email, credentials)
        try:
            self._post_backup_write(account_num, email)
        except OSError:
            from claude_swap.session import mark_session_stale

            if mark_session_stale(self._session_dir(account_num, email)):
                self._logger.warning(
                    "Stored account %s's credential but could not invalidate "
                    "its session profile; marked it stale so the next run "
                    "re-bootstraps.", account_num, exc_info=True,
                )
            else:
                # Nothing recorded the superseded profile: the marker is what
                # forces the re-bootstrap, and the local reuse check cannot
                # see a revoked-but-unexpired token. Say so at ERROR rather
                # than let a silent warning imply the fallback worked.
                self._logger.error(
                    "Stored account %s's credential but could NOT invalidate "
                    "its session profile OR mark it stale; the profile may "
                    "keep serving the superseded generation until its token "
                    "expires.", account_num, exc_info=True,
                )

    def _delete_account_credentials(self, account_num: str, email: str) -> None:
        self._store._delete_account_credentials(account_num, email)

    def _delete_account_credentials_strict(self, account_num: str, email: str) -> None:
        """Pre-commit clear that raises when the key still reads non-empty."""
        self._store.delete_account_credentials_strict(account_num, email)

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

    def recover(self, identifier: str) -> dict:
        """Ask the credential-owning Claude profile to refresh an expired token."""
        from claude_swap.recovery import recover_account

        account_num, email, org_uuid = self.resolve_account(identifier)
        return recover_account(self, account_num, email, org_uuid)

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
        self._refuse_session_shell()
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
        self._refuse_session_shell()
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

    def swap_accounts(self, first: str, second: str) -> tuple[str, str]:
        """Exchange two accounts' slot numbers (list order / numeric targets).

        Everything keyed by the slot number moves with the swap: the
        sequence records (including aliases, which belong to the account),
        the per-slot credential and config backups, membership in
        ``sequence`` (kept sorted, so rotation and ``cswap list`` order
        follow the new numbers), ``activeAccountNumber``, and each slot's
        session profile directory (history preserved). Directory mappings key on
        (email, org) and are unaffected. Usage-cache rows key on the slot
        number but carry the account identity, so a swapped row fails the
        identity check and self-heals on the next poll. Auto-switch
        quarantine entries also key on the slot number and are not moved,
        but self-heal on the next pass: the stale entry fails its
        email/fingerprint check and is released, and a dead account under
        its new number is re-caught by freshen-before-activate.

        The whole resolve-validate-mutate span runs under the account lock
        (like switch and the usage-refresh persist). The ``sequence.json``
        write is the commit point: a failure before it rolls both slots back
        (via durable staged copies when the backup keys overlap), and after
        it only best-effort cleanup of stale keys remains.

        Returns the two resolved slot numbers ``(first_num, second_num)``.
        """
        if not self.sequence_file.exists():
            raise ConfigError("No accounts are managed yet")

        # Local I/O only from here on, so the account lock can span the whole
        # resolve-validate-mutate sequence — a concurrent switch or usage-
        # refresh persist (which take the same lock) can never interleave
        # with the relocation.
        self._refuse_session_shell()
        with FileLock(self.lock_file):
            return self._swap_accounts_locked(first, second)

    def _read_backup_or_abort(self, account_num: str, email: str) -> str:
        """Backup read for swap/move's pre-mutation snapshot; raises on an
        unreadable (not absent) backup.

        Nothing has moved yet at the call sites, so an unreadable verdict
        aborts here rather than committing a swap/move that silently drops
        the slot's live refresh token in favor of an empty destination.
        """
        creds, unreadable = self._read_account_credentials_ex(account_num, email)
        if unreadable:
            raise ConfigError(
                f"Account-{account_num}'s stored credential could not be "
                "read (keychain unavailable?); nothing was changed. Retry "
                "once it is readable again."
            )
        return creds

    def _swap_accounts_locked(self, first: str, second: str) -> tuple[str, str]:
        """Body of :meth:`swap_accounts`; the caller holds ``self.lock_file``.

        Split out so ``move_account`` can resolve identifiers and dispatch
        inside one lock acquisition (FileLock is non-reentrant): a slot
        number resolved outside the lock could be renumbered by a concurrent
        swap/move and target the wrong account.
        """
        self._get_sequence_data_migrated()

        num_a = self._resolve_account_identifier(first)
        if not num_a:
            raise AccountNotFoundError(f"No account found with identifier: {first}")
        num_b = self._resolve_account_identifier(second)
        if not num_b:
            raise AccountNotFoundError(f"No account found with identifier: {second}")
        if num_a == num_b:
            raise ValidationError("Cannot swap an account with itself")

        data = self._get_sequence_data() or {}
        record_a = data.get("accounts", {}).get(num_a)
        record_b = data.get("accounts", {}).get(num_b)
        if not record_a:
            raise AccountNotFoundError(f"Account-{num_a} does not exist")
        if not record_b:
            raise AccountNotFoundError(f"Account-{num_b} does not exist")

        email_a = record_a.get("email", "")
        email_b = record_b.get("email", "")

        # Backups and session profiles are keyed by (slot, email); relocating
        # them under a live session-mode claude would pull state out from
        # under a running process.
        self._ensure_no_live_session(num_a, email_a, "--swap-accounts")
        self._ensure_no_live_session(num_b, email_b, "--swap-accounts")

        # Read both slots' backup material up front so a read failure aborts
        # before anything has been moved. Missing material reads as "" (an
        # api-key or never-backed-up slot) and stays missing after the swap
        # — but the plain reader answers that same "" for a backup that
        # EXISTS and simply could not be read right now (locked Keychain,
        # a permission glitch). See _read_backup_or_abort.
        creds_a = self._read_backup_or_abort(num_a, email_a)
        creds_b = self._read_backup_or_abort(num_b, email_b)
        config_a = self._read_account_config(num_a, email_a)
        config_b = self._read_account_config(num_b, email_b)

        staging: dict[str, Path] = {}
        try:
            if email_a == email_b:
                # Same email: the two slots' backup keys fully overlap, so
                # every write below overwrites the other account's material.
                # Park durable copies first — a failure mid-write can then
                # never leave a credential existing only in this process's
                # memory. (Staging fails -> abort before anything changed.)
                staging = self._stage_overlap_material(
                    {num_a: (creds_a, config_a), num_b: (creds_b, config_b)}
                )

            # Move each session profile to its owner's new slot key. When both
            # accounts share an email the two paths swap directly, so stage the
            # first through a temporary name.
            self._swap_session_dirs(num_a, email_a, num_b, email_b)

            # Set each destination key to its owner's exact state: write
            # material that exists, actively clear what doesn't. An empty
            # source must never leave the destination serving leftover
            # material — the other account's (same-email overlap, where no
            # separate old-key cleanup runs) or a stale file leaked by an
            # earlier crash. The old keys are cleared only after the commit
            # below, so the records never point at missing material.
            if creds_a:
                self._write_account_credentials(num_b, email_a, creds_a)
            else:
                self._delete_account_credentials_strict(num_b, email_a)
            if config_a:
                self._write_account_config(num_b, email_a, config_a)
            else:
                self._delete_config_backup(num_b, email_a)
            if creds_b:
                self._write_account_credentials(num_a, email_b, creds_b)
            else:
                self._delete_account_credentials_strict(num_a, email_b)
            if config_b:
                self._write_account_config(num_a, email_b, config_b)
            else:
                self._delete_config_backup(num_a, email_b)

            data["accounts"][num_a], data["accounts"][num_b] = record_b, record_a
            int_a, int_b = int(num_a), int(num_b)
            # Renumber, then sort: sequence is kept sorted everywhere (add
            # sorts on insert), so rotation and list order follow the new
            # slot numbers instead of preserving the old visual positions.
            data["sequence"] = [
                int_b if n == int_a else int_a if n == int_b else n
                for n in data.get("sequence", [])
            ]
            data["sequence"].sort()
            active = data.get("activeAccountNumber")
            if active == int_a:
                data["activeAccountNumber"] = int_b
            elif active == int_b:
                data["activeAccountNumber"] = int_a
            data["lastUpdated"] = get_timestamp()
            # The commit point: _write_json's rename publishes the swap.
            self._write_json(self.sequence_file, data)
        except BaseException:
            self._rollback_swap(
                num_a, email_a, creds_a, config_a,
                num_b, email_b, creds_b, config_b,
                staging,
            )
            raise

        # Post-commit cleanup, all best-effort: the records already reference
        # the new keys only. A failure here leaks a stale file, never a wrong
        # read — logged loudly because a stale key under a freed slot would
        # poison a future same-email account landing on that number.
        if email_a != email_b:
            for num, email in ((num_a, email_a), (num_b, email_b)):
                try:
                    self._delete_account_files(num, email)
                except Exception as e:
                    self._logger.error(
                        f"Stale backup left under old key {num} ({email}): {e}"
                    )
        # The .prev generations retained while writing the destination keys
        # hold the displaced material — another account's credential (or a
        # stale one) that recovery must never resurrect onto the key's new
        # owner. Cleared destinations already dropped theirs.
        if creds_a:
            self._store.delete_previous_backup(num_b, email_a)
        if creds_b:
            self._store.delete_previous_backup(num_a, email_b)
        self._discard_staging(staging)

        self._logger.info(
            f"Swapped slots: {num_a} ({email_a}) <-> {num_b} ({email_b})"
        )
        return num_a, num_b

    def _delete_config_backup(self, account_num: str, email: str) -> None:
        """Delete one slot key's config backup file, if present.

        Unconditional unlink: ``exists()`` returns False on an inaccessible
        directory, which would fail open in the required-clear paths.
        Missing is fine (``missing_ok``); permission/I/O errors propagate —
        every caller either needs the abort (write-or-clear) or already
        wraps and counts the failure (rollback, stray cleanup).
        """
        config_file = self.configs_dir / f".claude-config-{account_num}-{email}.json"
        config_file.unlink(missing_ok=True)

    def _discard_staging(self, staging: dict[str, Path]) -> None:
        """Remove staged pre-swap copies, telling the user about survivors.

        A staging file that cannot be removed holds plaintext credentials, so
        a silent leak is not acceptable — and a leftover also blocks the next
        same-email swap (staging refuses to overwrite existing files).
        """
        for path in staging.values():
            try:
                path.unlink()
            except OSError as e:
                self._logger.error(f"Could not remove swap staging copy: {e}")
                warning(
                    f"Could not remove swap staging file {path} — it holds "
                    f"pre-swap credentials; please delete it manually."
                )

    def _stage_overlap_material(
        self, material: dict[str, tuple[str, str]]
    ) -> dict[str, Path]:
        """Park slots' backup material in temp files before overlapping writes.

        Used by same-email swaps, where each slot's write destroys the other
        slot's stored material. File-based on every platform — durability
        across a process death is the point, so the files (0600 from
        creation, in the credentials directory, normally alive for
        milliseconds) are created with ``O_EXCL`` and never overwrite an
        existing staging file: a leftover from an interrupted swap may be
        the only surviving copy of a credential, so the swap refuses and
        points at it instead of retrying over it. A failure *here* aborts
        the swap before anything has been overwritten.

        Deliberately NOT built: a manifest-based auto-recovery (a leftover
        cannot cheaply be told apart from post-commit cleanup residue, and
        restoring credentials on a wrong guess is worse than stopping), and
        Keychain-backed staging on macOS (the Keychain is the very backend
        whose mid-write failures this protects against).
        """
        staged: dict[str, Path] = {}
        try:
            for num, (creds, config) in material.items():
                for kind, content in (("creds", creds), ("config", config)):
                    if not content:
                        continue
                    path = self.credentials_dir / f".swap-staging-{kind}-{num}.json"
                    if path.exists():
                        raise ConfigError(
                            f"Found leftover staging from an interrupted swap: "
                            f"{path}. It holds that slot's pre-swap credentials "
                            f"and may be the only surviving copy. Verify both "
                            f"accounts still work (`cswap list`), then delete "
                            f"the file and retry."
                        )
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        fh.write(content)
                    staged[f"{kind}-{num}"] = path
        except ConfigError:
            # Leftover found: remove only what THIS call created.
            self._discard_staging(staged)
            raise
        except OSError as e:
            self._discard_staging(staged)
            raise ConfigError(
                f"Could not stage swap material, nothing was changed: {e}"
            )
        return staged

    def _swap_session_dirs(
        self, num_a: str, email_a: str, num_b: str, email_b: str
    ) -> None:
        """Exchange two slots' session profile directories, best effort.

        A profile that cannot be moved is not rescued: the caller prunes the
        old slot keys afterwards (``_delete_account_files``, which removes
        session profiles too), and setup_session re-bootstraps a missing
        profile from the relocated backups, so a skipped move costs at most
        that slot's session history.
        """
        dir_a = self._session_dir(num_a, email_a)
        dir_b = self._session_dir(num_b, email_b)
        new_a = self._session_dir(num_b, email_a)  # account A's new home
        new_b = self._session_dir(num_a, email_b)  # account B's new home

        staging = None
        try:
            if dir_a.exists():
                staging = dir_a.with_name(dir_a.name + ".swapping")
                os.replace(dir_a, staging)
            if dir_b.exists() and not new_b.exists():
                os.replace(dir_b, new_b)
            if staging is not None and not new_a.exists():
                os.replace(staging, new_a)
                staging = None
        except OSError as e:
            self._logger.warning(f"Session profile move skipped during swap: {e}")
        finally:
            if staging is not None:
                # Never strand a profile under the staging name.
                try:
                    if not dir_a.exists():
                        os.replace(staging, dir_a)
                except OSError:
                    pass

    def _rollback_swap(
        self,
        num_a: str,
        email_a: str,
        creds_a: str,
        config_a: str,
        num_b: str,
        email_b: str,
        creds_b: str,
        config_b: str,
        staging: dict[str, "Path"],
    ) -> None:
        """Best-effort restore of both slots after a failed swap mutation.

        Runs only before the metadata commit, so restoring means putting the
        *old* keys back. Matters most when the two accounts share an email:
        their backup keys fully overlap, so a half-written swap has already
        overwritten one account's material — and a key whose original was
        empty must go back to empty rather than keep the other account's
        credential. Every step is attempted independently; if any fails, the
        staged pre-swap copies are kept on disk for manual recovery instead
        of being deleted.
        """
        self._logger.error(
            f"Swap {num_a} <-> {num_b} failed mid-write; restoring both slots"
        )
        failures = 0
        # Undo the session-profile exchange (same staging trick, reversed).
        self._swap_session_dirs(num_b, email_a, num_a, email_b)
        overlap = email_a == email_b
        for kind, num, email, original in (
            ("creds", num_a, email_a, creds_a),
            ("config", num_a, email_a, config_a),
            ("creds", num_b, email_b, creds_b),
            ("config", num_b, email_b, config_b),
        ):
            try:
                if original:
                    if kind == "creds":
                        self._write_account_credentials(num, email, original)
                    else:
                        self._write_account_config(num, email, original)
                elif overlap:
                    # The overlapping key may now hold the *other* account's
                    # material — an originally-empty slot must read empty
                    # again, not serve someone else's credential. Strict: a
                    # suppressed failure here must count as a failure, so
                    # the staged copies are kept and reported.
                    if kind == "creds":
                        self._delete_account_credentials_strict(num, email)
                    else:
                        self._delete_config_backup(num, email)
            except Exception as e:
                failures += 1
                self._logger.error(
                    f"Rollback {kind} restore failed for slot {num}: {e}"
                )
        if email_a != email_b:
            # Drop half-written copies under the new keys; the records still
            # point at the old slots. (When the emails match, the "new" keys
            # are the keys just restored — nothing stale exists.)
            for num, email in ((num_b, email_a), (num_a, email_b)):
                try:
                    self._delete_account_credentials(num, email)
                    self._delete_config_backup(num, email)
                except Exception as e:
                    failures += 1
                    self._logger.error(f"Rollback cleanup failed for slot {num}: {e}")
        if not failures:
            # The restore writes above pushed the half-written material into
            # the keys' retained .prev generations; both keys now hold their
            # exact originals, so those generations are pure contamination.
            # (On a partial rollback everything is left in place — maximum
            # material preserved for manual recovery.)
            for num, email, original in (
                (num_a, email_a, creds_a),
                (num_b, email_b, creds_b),
            ):
                if original:
                    self._store.delete_previous_backup(num, email)
        if staging:
            if failures:
                kept = ", ".join(str(p) for p in staging.values())
                self._logger.error(
                    f"Rollback incomplete — staged pre-swap copies kept for "
                    f"manual recovery: {kept}"
                )
                warning(
                    f"Swap rollback was incomplete; your pre-swap credentials "
                    f"are preserved in: {kept}"
                )
            else:
                self._discard_staging(staging)

    def move_account(self, account: str, target: str) -> tuple[str, str, bool]:
        """Assign ``account`` to slot number ``target`` (the general form of swap).

        ``account`` is any ``NUM|EMAIL|ALIAS``; ``target`` is the destination
        slot number. Three cases:

        - target is the account's current slot -> no-op.
        - target slot is empty -> the account is relocated there and its old
          slot is freed. ``swap`` cannot express this (it needs two accounts).
        - target slot is occupied -> the two accounts trade places, exactly
          like ``swap account <occupant>``; the displaced account takes the
          vacated slot, so nothing is ever lost.

        Slot numbers may be sparse (``remove`` leaves gaps, ``add`` grows from
        the max), so any positive number up to 99 — or the current highest
        slot, if a table already grew past that — is a legal target. The cap
        exists because ``add`` numbers from the max: a stray huge target would
        inflate every future account number.

        Returns ``(source_num, target_num, swapped)`` where ``swapped`` is True
        when an occupant was displaced.
        """
        self._refuse_session_shell()
        if not self.sequence_file.exists():
            raise ConfigError("No accounts are managed yet")

        target = target.strip()
        if not target.isdigit() or int(target) < 1:
            raise ValidationError(
                f"Target slot must be a positive slot number, got: {target!r} "
                f"(use `swap` to trade two accounts by identifier)"
            )
        target = str(int(target))  # normalize "01" -> "1"

        # Resolution and dispatch happen inside the same lock acquisition as
        # the mutation (via the *_locked helpers — FileLock is non-reentrant):
        # a slot number resolved outside the lock could be renumbered by a
        # concurrent swap/move and end up moving the wrong account.
        with FileLock(self.lock_file):
            self._get_sequence_data_migrated()

            num_src = self._resolve_account_identifier(account)
            if not num_src:
                raise AccountNotFoundError(
                    f"No account found with identifier: {account}"
                )

            data = self._get_sequence_data() or {}
            if not data.get("accounts", {}).get(num_src):
                raise AccountNotFoundError(f"Account-{num_src} does not exist")

            # `add` numbers new accounts from the highest slot, so a stray huge
            # target would inflate every future account number.
            max_slot = max(
                (int(n) for n in data.get("accounts", {}) if n.isdigit()), default=0
            )
            cap = max(99, max_slot)
            if int(target) > cap:
                raise ValidationError(
                    f"Target slot {target} is out of range (1-{cap}): new accounts "
                    f"are numbered from the highest slot, so a large target would "
                    f"inflate future account numbers"
                )

            if num_src == target:
                return num_src, target, False

            if data.get("accounts", {}).get(target):
                # Occupied target: trade places, exactly `swap num_src target`.
                self._swap_accounts_locked(num_src, target)
                return num_src, target, True

            self._relocate_locked(num_src, target)
            return num_src, target, False

    def _relocate_locked(self, num_src: str, target: str) -> None:
        """Move one account from ``num_src`` to the empty slot ``target``.

        The caller holds ``self.lock_file``. The one-way counterpart of
        :meth:`_swap_accounts_locked`: everything keyed by the slot number
        (credential and config backups, session profile, membership in
        ``sequence`` — kept sorted — and ``activeAccountNumber``) follows the
        account to its new number, and ``num_src`` is left empty. The caller
        checks ``target`` is unoccupied; it is re-checked here as an
        invariant. No rollback is needed: the ``sequence.json`` write is the
        commit point — before it the old keys are untouched (strays under
        the target key are cleaned on failure), after it only best-effort
        cleanup of the old keys remains.
        """
        data = self._get_sequence_data() or {}
        record = data.get("accounts", {}).get(num_src)
        if not record:
            raise AccountNotFoundError(f"Account-{num_src} does not exist")
        if data.get("accounts", {}).get(target):
            raise ValidationError(
                f"Slot {target} is already occupied — retry the move"
            )
        email = record.get("email", "")

        # Relocating backups/session under a live session-mode claude would
        # pull state out from under a running process.
        self._ensure_no_live_session(num_src, email, "--move-account")

        # Read backup material up front so a read failure aborts before any
        # move. Missing material reads as "" (api-key or never-backed-up
        # slot) — but the plain reader answers that same "" for a backup
        # that EXISTS and simply could not be read right now. See
        # _read_backup_or_abort.
        creds = self._read_backup_or_abort(num_src, email)
        config = self._read_account_config(num_src, email)

        src_dir = self._session_dir(num_src, email)
        dst_dir = self._session_dir(target, email)
        try:
            # Move the session profile to the account's new slot key, best
            # effort: a profile that cannot be moved is pruned below with the
            # old slot's backups, and setup_session re-bootstraps a missing
            # one from the relocated backups — a skipped move costs at most
            # this slot's history.
            if src_dir.exists() and not dst_dir.exists():
                try:
                    os.replace(src_dir, dst_dir)
                except OSError as e:
                    self._logger.warning(
                        f"Session profile move skipped during move: {e}"
                    )

            # Set the target key to the account's exact state: write material
            # that exists, actively clear what doesn't — an unbacked account
            # must not adopt stale material leaked under the target key by an
            # earlier crash. The old key is cleared only after the commit
            # below, so the records never point at missing material.
            if creds:
                self._write_account_credentials(target, email, creds)
            else:
                self._delete_account_credentials_strict(target, email)
            if config:
                self._write_account_config(target, email, config)
            else:
                self._delete_config_backup(target, email)

            data["accounts"][target] = record
            del data["accounts"][num_src]
            int_src, int_target = int(num_src), int(target)
            # Renumber, then sort: sequence is kept sorted everywhere (add
            # sorts on insert), so rotation and list order follow the new
            # slot number.
            data["sequence"] = [
                int_target if n == int_src else n for n in data.get("sequence", [])
            ]
            data["sequence"].sort()
            if data.get("activeAccountNumber") == int_src:
                data["activeAccountNumber"] = int_target
            data["lastUpdated"] = get_timestamp()
            # The commit point: _write_json's rename publishes the move.
            self._write_json(self.sequence_file, data)
        except BaseException:
            # Pre-commit failure: the records still point at num_src and its
            # keys are untouched — drop any strays written under the target
            # key and put the session profile back, best effort.
            try:
                self._delete_account_credentials(target, email)
                self._delete_config_backup(target, email)
                if dst_dir.exists() and not src_dir.exists():
                    os.replace(dst_dir, src_dir)
            except Exception as e:
                self._logger.error(f"Cleanup after failed move incomplete: {e}")
            raise

        # Post-commit: clear the old keys, best effort — the records now
        # reference the target slot only. _delete_account_files drops the
        # stale (num_src, email) backups and whatever session profile is
        # still under the old key (nothing, unless the move above was
        # skipped). A failure leaks a stale backup under the freed number
        # (logged loudly: it would poison a future same-email account
        # landing on that slot).
        try:
            self._delete_account_files(num_src, email)
        except Exception as e:
            self._logger.error(
                f"Stale backup left under old key {num_src} ({email}): {e}"
            )
        if creds:
            # Any .prev retained while overwriting a stale target key holds
            # that stale material, not this account's history.
            self._store.delete_previous_backup(target, email)

        self._logger.info(f"Moved slot: {num_src} ({email}) -> {target}")

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
        self, fetch: set[str] | None = None, *, scheduled: bool = False
    ) -> dict[str, UsageEntry]:
        """Store-backed usage entries (ages, errors, poll state) per account.

        ``fetch`` restricts which accounts *may* be fetched this pass (the
        auto engine's scheduler); ``None`` means every stale account is
        eligible (on-demand callers). ``scheduled=True`` preserves valid
        future plans while still allowing due plans to beat the serve TTL.
        """
        accounts_info = self._build_accounts_info()
        return self._collect_usage_entries(
            accounts_info, fetch=fetch, scheduled=scheduled
        )

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
        # No models needed: only fetched_at is read, never scoped-window trust.
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

    def backfill_account_uuid(
        self,
        account_num: str,
        uuid: str,
        expected_email: str | None = None,
        expected_org: str | None = None,
    ) -> None:
        """Record a resolved account uuid on a slot that lacks one.

        Only ever fills an empty uuid (add-token placeholders) — an existing
        uuid is identity and is never rewritten here. When ``expected_email``
        / ``expected_org`` are given, the fill additionally requires the slot
        to still hold that identity under the lock (a remove/re-add landing
        in the gap — even one keeping the email but changing the org — must
        not get the predecessor's uuid stamped on it). Caller must NOT hold
        ``self.lock_file``.
        """
        if not uuid:
            return
        with FileLock(self.lock_file):
            data = self._get_sequence_data() or {}
            acct = data.get("accounts", {}).get(str(account_num))
            if (
                acct is not None
                and not (acct.get("uuid") or "").strip()
                and (
                    expected_email is None
                    or acct.get("email") == expected_email
                )
                and (
                    expected_org is None
                    or (acct.get("organizationUuid", "") or "") == expected_org
                )
            ):
                acct["uuid"] = uuid
                data["lastUpdated"] = get_timestamp()
                self._write_json(self.sequence_file, data)

    def consume_backup_grant(
        self, account_num: str, email: str, snapshot: str
    ) -> "oauth.RefreshOutcome":
        """The gate through which a backup refresh token is consumed.

        Not the only site that POSTs one: ``_fetch_active_usage``'s recovery
        branch can POST the slot's backup grant too, when the live bytes moved
        or were cleared. It is not an escape — it takes this same per-slot
        *consume lock*, in this same order, and its own comment at that site
        records why. What is single here is the SERIALIZATION, not the call
        site, and saying "the single place" instead sends the next reader
        looking for a violation rather than for the second lock holder.

        A refresh token is one-time-use, so the POST must consume the
        provably-freshest copy of the slot's grant — never a caller's
        snapshot, which may be a superseded generation. The whole sequence
        runs under that consume lock (re-read → POST → CAS) so two consumers
        can never POST the same grant; the slot ``FileLock`` itself never
        covers the network call.

        The body below is the sequence: adopt a stashed successor and re-read
        under the slot lock, POST outside it, then CAS on the refresh-token
        fingerprint and either persist or stash. A consumed generation is
        never discarded — a stash is adopted by the next pass — and the gate
        never raises after the grant is consumed, since callers run in the
        never-raises collect pass.

        Returns a ``RefreshOutcome``: ``credentials`` is the slot's
        now-current credential on success (ours, or a racing writer's adopted
        newer lineage); ``error`` carries the refresh failure unchanged. Every
        outcome carries ``consumed_fp`` — strike binding must follow the bytes
        the gate actually POSTed, which may differ from the caller's snapshot.

        The caller must NOT hold ``self.lock_file`` (non-reentrant).
        """
        # Store-resolution parity: CC ≥2.1.220 honors
        # CLAUDE_SECURESTORAGE_CONFIG_DIR for its credential store. cswap
        # mirrors that resolution on the CAPTURE path (#205 —
        # `_read_capture_credentials` reads the store CC would read), but
        # the consume and switch paths still resolve the DEFAULT store.
        # Consuming a grant read from the default store while CC
        # reads/writes the redirected one is the stale-copy failure class by
        # construction — refuse (transient, so nothing strikes) rather than
        # operate on a store CC left behind.
        if os.environ.get("CLAUDE_SECURESTORAGE_CONFIG_DIR"):
            self._logger.warning(
                "CLAUDE_SECURESTORAGE_CONFIG_DIR is set; cswap mirrors it "
                "when capturing a credential but not when consuming one, "
                "so refusing to consume account %s's refresh token "
                "(unset the variable or run from a normal shell).",
                account_num,
            )
            # Distinct kind: deterministic and self-inflicted (an env var),
            # so it must SURFACE — a transient would fall through to a
            # guaranteed-401 usage call every pass and read as generic
            # network trouble forever.
            return oauth.RefreshOutcome(None, "store-unmirrored")

        # Consume serialization: one in-flight consume per slot, held across
        # re-read → POST → CAS. The slot FileLock cannot cover the POST
        # (network never runs under a lock others contend on), which left a
        # window where a second gate re-read the unchanged backup and POSTed
        # the same one-time-use grant (freshen vs collector). This dedicated
        # lock is contended ONLY by other gates — waiting on it is exactly
        # the serialization wanted, and the POST is bounded (10 s), so a
        # loser waits briefly or defers.
        consume_lock = FileLock(
            self.credentials_dir / f".consume-{account_num}.lock"
        )
        if not consume_lock.acquire():
            self._logger.info(
                "Another consume is in flight for account %s; deferring to "
                "the next pass.", account_num,
            )
            # Distinct from "transient": nothing failed and nothing is remote.
            # Another gate holds the slot and will finish; this pass simply
            # yields. Reported as its own kind so the tick error does not
            # blame the network for local serialization working as designed.
            return oauth.RefreshOutcome(None, "consume-busy")
        try:
            return self._consume_backup_grant_locked(
                account_num, email, snapshot
            )
        finally:
            consume_lock.release()

    def _consume_backup_grant_locked(
        self, account_num: str, email: str, snapshot: str
    ) -> "oauth.RefreshOutcome":
        """Body of ``consume_backup_grant``; caller holds the consume lock."""
        from claude_swap.session import (
            is_session_stale,
            read_session_credentials,
            session_dir_for,
            session_identity_drifted,
        )

        try:
            with FileLock(self.lock_file):
                current, unreadable = self._read_account_credentials_ex(
                    account_num, email
                )
                if unreadable:
                    # The backup may exist but cannot be seen (macOS
                    # keychain locked/denied): the snapshot is exactly the
                    # possibly-superseded copy this gate exists to never
                    # consume. Defer; nothing consumed.
                    self._logger.info(
                        "Backup for account %s unreadable (keychain); "
                        "deferring the refresh.", account_num,
                    )
                    return oauth.RefreshOutcome(None, "transient")
                # Adopt a stashed successor from a prior gate whose persist
                # failed: if the store still holds the generation that
                # successor superseded, writing it back IS the pending
                # persist — and saves consuming a grant at all.
                try:
                    adopted_creds = self._adopt_stashed_successor(
                        account_num, email, current
                    )
                except CredentialReadError:
                    # The slot's only successor is unreadable. Deferring is
                    # right -- the bytes are the SOLE copy of a generation
                    # this slot already consumed, and nothing on disk tells
                    # "locked for a minute" from "locked forever", so
                    # retiring on a strike count or an age bound would
                    # destroy a live refresh token every time the cause was
                    # merely slow. What must not stay is the LABEL: the
                    # generic handler below degrades this to "transient",
                    # which the tick renders as "could not freshen any
                    # candidate (network?)" -- sending the operator to check
                    # a connection that is fine, forever, on a condition
                    # only they can clear (unlock the Keychain, fix the
                    # mode, remount the volume) or drop
                    # (`cswap unclaimed --purge`).
                    self._logger.info(
                        "Account %s's stashed successor is unreadable; "
                        "deferring the refresh.", account_num, exc_info=True,
                    )
                    return oauth.RefreshOutcome(None, "stash-unreadable")
                if adopted_creds is not None:
                    current = adopted_creds
                if not current:
                    # ABSENT, not unreadable (that branch returned above): the
                    # slot was removed between the caller's read and this
                    # locked re-read. Falling back to the caller's snapshot
                    # spends a grant for an account the user just deleted and
                    # stashes a successor keyed to a generation no slot holds
                    # — `_adopt_stashed_successor` returns early on an empty
                    # store fingerprint, so nothing can ever adopt it. The CAS
                    # branch below already refuses to WRITE that successor
                    # (`consume-gate-slot-removed`); this is the same rule one
                    # step earlier, before the grant is spent rather than
                    # after. Every production caller reads its snapshot from
                    # the backup store, so absent here really does mean gone.
                    self._logger.info(
                        "Account %s's stored credential is gone; deferring "
                        "the refresh rather than consuming a grant for a "
                        "slot that no longer exists.", account_num,
                    )
                    return oauth.RefreshOutcome(None, "transient")
                refresh_input = current
                input_oauth = oauth.extract_oauth_data(refresh_input)
                # Session-profile precedence: only when no live session owns
                # the profile (a live claude rotates its own tokens — #97's
                # rule) and the profile identity — org included: two slots
                # may share an email across orgs — still matches the slot.
                org_uuid = (
                    (self._get_sequence_data() or {})
                    .get("accounts", {})
                    .get(account_num, {})
                    .get("organizationUuid", "")
                    or ""
                )
                if not self._live_session_pids(account_num, email):
                    sdir = session_dir_for(self.backup_dir, account_num, email)
                    profile = read_session_credentials(sdir)
                    if (
                        profile
                        # A marked profile's credentials are presumed stale
                        # (backup changed under the live session — e.g. a
                        # deliberate re-add/import): never let it supersede
                        # the backup it is presumed stale against.
                        and not is_session_stale(sdir)
                        and not session_identity_drifted(sdir, email, org_uuid)
                    ):
                        prof_oauth = oauth.extract_oauth_data(profile)
                        cur_exp = (input_oauth or {}).get("expiresAt") or 0
                        prof_exp = (prof_oauth or {}).get("expiresAt") or 0
                        if (
                            prof_oauth
                            and prof_oauth.get("accessToken")
                            and prof_oauth.get("refreshToken")
                            and oauth.credential_fingerprint(profile)
                            != oauth.credential_fingerprint(refresh_input)
                            and prof_exp > cur_exp
                        ):
                            # The profile holds the newer generation: the
                            # backup rt is already consumed. Resync so the
                            # slot's stored credential is the live lineage,
                            # then consume THAT.
                            self._write_account_credentials(
                                account_num, email, profile
                            )
                            refresh_input = profile
                            input_oauth = prof_oauth
                consumed_fp = oauth.credential_fingerprint(refresh_input)
        except LockError:
            # Nothing consumed yet — a holder (switch, collector, CC) owns
            # the slot; defer cleanly rather than raise through callers
            # that promise never to (the collect pass thread-pools us).
            # "Nothing consumed" is about the POST, not the store -- see
            # the generic handler below.
            self._logger.info(
                "Slot lock held elsewhere; deferring account %s's backup "
                "refresh to the next pass.", account_num,
            )
            return oauth.RefreshOutcome(None, "transient")
        except Exception:
            # No POST has been issued yet, so no grant of ours is
            # outstanding — degrade to transient instead of raising through
            # the never-raises collect pass.
            #
            # "Nothing consumed" is about the POST, NOT about the store. The
            # resync and the adoption both WRITE before this point, and an
            # earlier version of this comment claimed they raise first; they
            # do not.
            #
            # So this handler IS reachable with the slot already advanced:
            # `_adopt_stashed_successor` makes everything past its own store
            # write non-fatal, but the adopt is not the last thing in this
            # `try` — `_get_sequence_data` reads with `strict=True` and comes
            # AFTER it, so a torn `sequence.json` raises to here over a slot
            # that adoption already freshened.
            #
            # `transient` is still the right answer there, and for the reason
            # the first line gives rather than an unadvanced slot: no grant of
            # ours is outstanding, so deferring costs a pass and spends
            # nothing. The cost of the advanced case is only that the next
            # pass re-reads a slot that is already fresh.
            self._logger.warning(
                "Pre-consume window failed for account %s; deferring.",
                account_num, exc_info=True,
            )
            return oauth.RefreshOutcome(None, "transient")

        input_oauth = input_oauth or {}
        snap_at = (oauth.extract_oauth_data(snapshot) or {}).get("accessToken")
        if (
            input_oauth.get("accessToken")
            and snap_at
            and input_oauth.get("accessToken") != snap_at
            and not oauth.is_oauth_token_expired(input_oauth.get("expiresAt"))
        ):
            # The world already moved past the caller's snapshot AND the
            # current generation is fresh: the refresh the caller wanted
            # has effectively happened (a racing gate's rotation, an
            # adopted stash, a live profile's newer family). Adopt it —
            # consuming another grant on top burns a generation for
            # nothing. When the re-read equals the snapshot (the 401-retry
            # shape: the server just rejected these exact bytes), this
            # never fires and the POST proceeds.
            return oauth.RefreshOutcome(refresh_input, None, None, consumed_fp)

        result = oauth.try_refresh_oauth_credentials(refresh_input)
        if result.error is not None or not result.credentials:
            # Strike binding must follow the POSTed bytes: the gate may have
            # substituted a locked re-read or the session profile for the
            # caller's snapshot, and failures are the only outcomes that
            # strike.
            return dataclasses.replace(result, consumed_fp=consumed_fp)

        stashed_reason = ""

        def stash_successor(reason: str, note: str) -> None:
            # A consumed generation is never discarded: park the successor
            # where the next gate pass adopts it (see
            # ``_adopt_stashed_successor``; ``consumedFp`` is the adoption
            # key — the generation this successor superseded).
            self._store._write_unclaimed_credential(
                result.credentials,
                {
                    "reason": reason,
                    "configSlot": account_num,
                    "consumedFp": consumed_fp,
                    "fingerprint": oauth.credential_fingerprint(
                        result.credentials
                    ),
                },
            )
            nonlocal stashed_reason
            stashed_reason = reason
            self._logger.warning(note, account_num)

        outcome_creds = result.credentials
        try:
            try:
                with FileLock(self.lock_file):
                    store_now, store_unreadable = (
                        self._read_account_credentials_ex(account_num, email)
                    )
                    if store_unreadable:
                        # The Keychain locked during the POST (macOS screen
                        # lock is ~1s away at any moment). The CAS cannot be
                        # evaluated: writing back could clobber a racing
                        # writer, and the plain reader's `""` would otherwise
                        # be read as "the slot was emptied", whose reason is
                        # deliberately NOT demoting. The grant IS spent and
                        # the slot may still hold the generation that spent
                        # it, so this must stash AND demote.
                        stash_successor(
                            "consume-gate-store-unreadable",
                            "Account %s's stored credential was unreadable "
                            "(keychain) after a refresh POST; successor "
                            "stashed, nothing rewritten.",
                        )
                    elif not store_now:
                        # The slot was emptied mid-POST (remove-account):
                        # writing the successor back would resurrect
                        # credentials the user just deleted. Park it
                        # instead.
                        stash_successor(
                            "consume-gate-slot-removed",
                            "Account %s's stored credential disappeared "
                            "during a refresh POST; successor stashed, "
                            "nothing rewritten.",
                        )
                    elif (
                        oauth.credential_fingerprint(store_now) != consumed_fp
                    ):
                        # A writer replaced the lineage while our POST was in
                        # flight: stash our successor, adopt the store's
                        # newer credential.
                        stash_successor(
                            "consume-gate-cas-conflict",
                            "Backup lineage for account %s moved during a "
                            "refresh POST; successor stashed, adopting the "
                            "newer store credential.",
                        )
                        outcome_creds = store_now
                    else:
                        self._write_account_credentials(
                            account_num, email, result.credentials
                        )
            except LockError:
                # The grant IS consumed — the successor must survive even
                # though the persist lock is unavailable. The token works;
                # the next gate pass adopts from the stash.
                stash_successor(
                    "consume-gate-persist-lock-failed",
                    "Slot lock unavailable after consuming account %s's "
                    "grant; successor stashed for the next pass.",
                )
        except Exception:
            # The grant IS consumed and callers (the thread-pooled collect
            # pass) promise never to raise: a persist OR stash failure must
            # not escape. Last resort is the stash; if that also fails
            # (same-dir I/O error, e.g. disk full), the successor survives
            # only in this return value — say so loudly.
            self._logger.warning(
                "Persisting account %s's refreshed credential failed; "
                "stashing instead.", account_num, exc_info=True,
            )
            try:
                stash_successor(
                    "consume-gate-persist-failed",
                    "Persist failed after consuming account %s's grant; "
                    "successor stashed for the next pass.",
                )
            except Exception:
                # Both the persist and the stash failed. stash_successor sets
                # stashed_reason after its write, so a raising write left it
                # empty and the guard below reported success on a spent grant
                # with nothing stashed.
                stashed_reason = "consume-gate-unpersisted"
                self._logger.error(
                    "Account %s's consumed successor could not be persisted "
                    "or stashed — it survives only for this pass. Fix the "
                    "storage failure, then re-login and `cswap add` if the "
                    "slot strikes.", account_num, exc_info=True,
                )
        if stashed_reason in _DEMOTING_STASH_REASONS:
            # The successor is parked, not persisted: the slot still holds the
            # generation whose grant we just spent. Callers read `error is
            # None` as "the slot is freshened and safe to activate" — after a
            # failed persist it is the opposite, and activating it installs an
            # expired access token that can never refresh, so Claude Code logs
            # the account out. Report transient so the caller defers; the next
            # pass adopts the stash and succeeds normally. The credentials
            # still ride along, so a caller that only needs a live token for
            # THIS request keeps working.
            #
            # Two stash reasons are excluded, for opposite reasons.
            #
            # A REMOVED slot: there is nothing left to activate or retry, so
            # deferring would only turn a completed user action into a
            # recurring error.
            #
            # A CAS CONFLICT: the slot is FRESHENED, which is the condition
            # this demotion exists to deny. A racing writer won and wrote a
            # newer valid lineage; we adopted it and it is what the caller
            # asked for. Reporting it as an error made `_freshen_target` skip
            # a healthy candidate and the tick emit "could not freshen any
            # candidate (network?)" on every multi-surface race — the exact
            # contention this gate was built for, turned into a false alarm.
            return oauth.RefreshOutcome(
                outcome_creds, "transient", result.token_account, consumed_fp,
                # `consume-gate-unpersisted` is set precisely WHEN the stash
                # write raised, so it is the one demoting reason that did not
                # park anything. Every other one wrote the entry first.
                stashed=stashed_reason != "consume-gate-unpersisted",
            )
        return oauth.RefreshOutcome(
            outcome_creds, None, result.token_account, consumed_fp
        )

    def _retire_stash_entry(self, entry_id: str, account_num: str) -> None:
        """Drop one stash entry as housekeeping. Never fatal.

        Every call site is inside the adopt scan, and the one that matters
        runs AFTER ``_write_account_credentials`` has already advanced the
        slot. ``_remove_unclaimed_credential`` can raise there -- its manifest
        rewrite ends in ``atomic_write_json`` (``OSError`` on a full disk or a
        read-only mount) under a lock that can time out (``LockError``) -- and
        a raise escaping a COMPLETED adoption is read by
        ``_consume_backup_grant_locked`` as a failed refresh, so the caller
        re-POSTs a generation this pass already consumed.

        The two costs differ by an order of magnitude. Losing a retire leaves
        one stale row, which the next pass retries and `cswap unclaimed
        --purge` drops by hand. Losing the adoption discards a live credential
        already written to the store. So: log and continue.
        """
        try:
            self._store._remove_unclaimed_credential(entry_id)
        except Exception:
            self._logger.warning(
                "Could not retire account %s's stash entry %s; leaving it for "
                "the next pass (`cswap unclaimed --purge` drops it by hand).",
                account_num, entry_id, exc_info=True,
            )

    def _adopt_stashed_successor(
        self, account_num: str, email: str, current: str
    ) -> str | None:
        """Complete a prior gate's failed persist from the unclaimed stash.

        A stash entry records ``consumedFp`` — the generation its credential
        superseded. When the slot still stores exactly that generation, the
        stored rt is already consumed and the stash holds its live
        successor: write it back (the pending persist) and drop the entry.
        Returns the adopted credentials, or None when nothing applies.
        Caller holds the slot FileLock.
        """
        cur_fp = oauth.credential_fingerprint(current)
        if not cur_fp:
            return None
        # A row that is merely unreadable THIS instant (locked keychain,
        # transient EIO) must not abort the scan before a later, readable
        # sibling on the same generation is tried (repeated persist-failures
        # can stash more than one row against the same consumedFp). Remember
        # it and keep scanning; only defer via CredentialReadError once no
        # row adopted.
        deferred_entry_id: str | None = None
        manifest, manifest_verdict = self._store._read_stash_manifest_ex()
        if manifest_verdict == "unreadable" or (
            manifest_verdict == "corrupt"
            and self._store._stash_entry_files_exist()
        ):
            # Not "nothing stashed": the rows cannot be established, and entry
            # bytes are at risk. Every row this scan would have read is the
            # sole record of a generation some pass already consumed, so an
            # empty scan makes the caller POST the slot's spent generation.
            #
            # That POST does not cost "one retry". The generation is spent by
            # construction, so it returns invalid_grant, and the gate returns
            # before any manifest write — nothing is set aside, nothing
            # self-heals, and at AUTH_DEAD_STRIKES=1 a live account is
            # quarantined while its successor sits orphaned on disk.
            #
            # CORRUPT with no entry files falls through instead: `{}` is then
            # not a guess about a pending successor, there provably is none,
            # and proceeding lets ``_write_stash_manifest`` set the bad file
            # aside — the only repair, and it only runs on a write.
            #
            # Fail-closed still has an exit: ``_list_unclaimed_credentials``
            # globs the entry files, so `cswap unclaimed` lists the orphans by
            # id and `--purge` drops them even with the manifest unreadable.
            raise CredentialReadError(
                f"the unclaimed manifest is {manifest_verdict} and stashed "
                f"entry files exist; deferring account {account_num}'s "
                "adoption rather than POSTing a generation a stashed "
                "successor may already have superseded (`cswap unclaimed` "
                "lists them, `--purge` drops one)"
            )
        for entry_id, meta in manifest.items():
            if meta.get("configSlot") != account_num:
                continue
            if meta.get("consumedFp") != cur_fp:
                if meta.get("reason") == "consume-gate-cas-conflict":
                    # A CAS-conflict entry can NEVER match: the conflict is by
                    # definition "the store moved off the generation we
                    # consumed", and the store only moves forward, so it never
                    # returns. Left alone these accumulate one file per
                    # conflict — the common outcome on a busy multi-surface
                    # setup — each indistinguishable from an entry still
                    # awaiting adoption. Retire it here, where the slot lock is
                    # already held and the current generation is in hand.
                    #
                    # Retiring is safe: the gate adopted the store's newer
                    # lineage in the same breath, so this successor branches
                    # off a generation that lineage already superseded. It is
                    # not the pending persist it looks like.
                    self._retire_stash_entry(entry_id, account_num)
                    self._logger.info(
                        "Retired account %s's CAS-conflict stash entry: its "
                        "generation was superseded by the writer that won the "
                        "race, so no pass can ever adopt it.", account_num,
                    )
                elif not any(self._store._read_unclaimed_credential(entry_id)):
                    # No bytes and no matching generation: nothing can ever
                    # adopt this row, and no other reason retires it. This is
                    # the state a FAILED retire leaves -- the bytes are
                    # unlinked before the manifest rewrite, so an OSError
                    # there orphans the row while the adoption that preceded
                    # it moved the slot off the generation it keys against.
                    #
                    # The READER, not `exists()`: `Path.exists()` swallows
                    # only ENOENT-shaped errors, so an EACCES/EIO would raise
                    # straight out of the scan and strand an adoptable sibling
                    # behind this row. `any(...)` is false only for
                    # ("", False) -- absent or corrupt. A merely UNREADABLE
                    # row is ("", True) and survives: its bytes may hold a
                    # real superseded token, so dropping it stays the
                    # operator's call (`cswap unclaimed --purge`).
                    self._retire_stash_entry(entry_id, account_num)
                    self._logger.info(
                        "Retired account %s's byte-less stash entry: its "
                        "credential is gone and its generation has passed, "
                        "so no pass could ever adopt it.", account_num,
                    )
                continue
            creds, unreadable = self._store._read_unclaimed_credential(entry_id)
            if unreadable:
                # This entry is the SOLE copy of a generation a prior gate
                # pass already consumed and could not persist. Falling
                # through here would make the caller POST the slot's
                # spent generation -- an unrecoverable invalid_grant on an
                # account whose live credential is sitting right here,
                # merely unreadable this instant. Remember it and keep
                # looking for a readable sibling on the same generation
                # before giving up.
                if deferred_entry_id is None:
                    deferred_entry_id = entry_id
                continue
            if not creds:
                # ABSENT (unlinked bytes, matching manifest row) or CORRUPT
                # (undecodable) -- either way the bytes are permanently gone,
                # not merely inaccessible right now, so nothing can ever
                # adopt this row. Retire it now: left alone it is rescanned
                # on every gate pass and leaks in --json's
                # unclaimedCredentials forever, the same accumulation the
                # CAS-conflict branch above already retires on sight.
                self._retire_stash_entry(entry_id, account_num)
                self._logger.info(
                    "Retired account %s's unreadable-bytes stash entry: its "
                    "generation is gone, so no pass could ever adopt it.",
                    account_num,
                )
                continue
            # The WRAPPER, not the store method plus a private repeat of its
            # tail: `_write_account_credentials` already contains the
            # invalidation and leaves STALE_MARKER when it cannot run, so it
            # cannot raise past its own store write. Open-coding the split
            # here made this one call site safe and left the other two — the
            # resync and the post-POST persist — carrying the defect.
            self._write_account_credentials(account_num, email, creds)
            # Housekeeping, and non-fatal for the same reason: the slot is
            # advanced, so a raise would report a failed refresh for a
            # credential the store holds. A stale row is retried next pass or
            # dropped with `cswap unclaimed --purge`.
            self._retire_stash_entry(entry_id, account_num)
            self._logger.info(
                "Adopted account %s's stashed successor (%s): the stored "
                "generation was already consumed by the gate pass that "
                "stashed it.", account_num, meta.get("reason", "unknown"),
            )
            return creds
        if deferred_entry_id is not None:
            # No row on this generation adopted; the deferred one is the
            # only copy of a generation this slot already consumed. Raise
            # into the caller's existing pre-consume exception handling,
            # which already degrades to "transient" and defers to the next
            # pass rather than discarding that generation.
            raise CredentialReadError(
                f"stash entry {deferred_entry_id} for account {account_num} "
                "is unreadable; deferring adoption rather than discarding "
                "its generation"
            )
        return None

    def _record_active_verdict(self, active) -> None:
        """Record THIS thread's active-read verdict (see `_active_verdict_tls`)."""
        self._active_verdict_tls.value = active

    def _with_active_verdict(self, fn):
        """Wrap `fn` so a worker thread inherits THIS thread's verdict.

        Thread-local keeps two TUI lanes from erasing each other's, but
        `_fetch_active_usage` always runs on a pool worker that never read —
        measured 30/30 verdicts lost, and the consume gate never fired.
        """
        verdict = self._active_verdict()

        def _inherit(*args, **kwargs):
            self._record_active_verdict(verdict)
            return fn(*args, **kwargs)

        return _inherit

    def _active_verdict(self):
        """This thread's active-read verdict; a clean one if it never read."""
        from claude_swap.credentials import ActiveCredentials

        return getattr(self._active_verdict_tls, "value", None) or ActiveCredentials(
            "", False, False
        )

    @property
    def _active_keychain_unavailable(self) -> bool:
        return self._active_verdict().keychain_unavailable

    @property
    def _active_read_unreadable(self) -> bool:
        """Whether THIS thread's active-credential read outright FAILED
        (plaintext-file OSError), as opposed to a genuinely absent slot.

        ``keychain_unavailable`` alone misses this off macOS:
        ``_read_active_credentials``'s file-read-error arm returns
        ``ActiveCredentials(None, keychain_failed, keychain_failed)``, and
        ``keychain_failed`` stays False on Linux/WSL/Windows (there is no
        Keychain to fail there) — ``value is None`` is the only surviving
        signal of the three states (readable / genuinely absent / could not
        be read). Mirrors the ``is None`` guard ``_slot_token_dead`` already
        uses for the same tri-state on the backup side.
        """
        return self._active_verdict().value is None

    @property
    def _active_read_degraded(self) -> bool:
        return self._active_verdict().degraded

    def _read_account_credentials_ex(
        self, account_num: str, email: str
    ) -> tuple[str, bool]:
        return self._store._read_account_credentials_ex(account_num, email)

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

    def _token_status_lines(
        self, account_info: tuple[int, str, str, str, bool, str, str]
    ) -> list[str]:
        """Source-labelled token-status lines for one account's display row."""
        num, email, _org_name, org_uuid, is_active, creds, _alias = account_info
        if looks_like_api_key(creds):
            return []
        if is_active:
            line = _label_token_status("active profile", creds)
            return [line] if line is not None else []

        from claude_swap.session import (
            read_session_credentials,
            session_identity_drifted,
        )

        lines: list[str] = []
        session_dir = self._session_dir(str(num), email)
        session_creds = read_session_credentials(session_dir)
        if session_creds:
            if session_identity_drifted(session_dir, email, org_uuid):
                lines.append("session profile: ignored (different account)")
            else:
                line = _label_token_status("session profile", session_creds)
                if line is not None:
                    lines.append(line)
        backup_line = _label_token_status("stored backup", creds)
        if backup_line is not None:
            lines.append(backup_line)
        return lines

    def _live_session_pids(self, account_num: str, email: str) -> list[int]:
        """PIDs of Claude instances running against an account's session profile.

        Scan-shaped: an unreadable record contributes no PID. Fine for the
        usage heuristics that read this; a destructive guard must use
        ``_ensure_no_live_session``, which asks the readability question too.
        """
        from claude_swap.session import scan_live_sessions

        sessions, _ = scan_live_sessions(self._session_dir(account_num, email))
        return [s.pid for s in sessions]

    def _ensure_no_live_session(self, account_num: str, email: str, action: str) -> None:
        """Refuse a destructive operation while a session-mode claude is live.

        "We could not read the records" refuses too, and says so in its own
        words. This gates ``_bootstrap`` (Keychain entry deleted,
        ``.credentials.json`` overwritten) and slot removal, so treating an
        unreadable record as an absent one runs them under a live instance.
        """
        from claude_swap.session import scan_live_sessions

        pids = self._live_session_pids(account_num, email)
        if pids:
            raise SessionError(
                f"Account-{account_num} ({email}) has a live session-mode Claude "
                f"instance (PID {', '.join(map(str, pids))}). "
                f"Exit it first, then retry {action}."
            )
        session_dir = self._session_dir(account_num, email)
        _, unreadable = scan_live_sessions(session_dir)
        if unreadable:
            raise SessionError(
                f"Account-{account_num} ({email}) has {unreadable} session "
                f"record(s) that could not be read, so whether a Claude "
                f"instance is live cannot be determined. Inspect "
                f"{session_dir / 'sessions'} and remove or repair them, then "
                f"retry {action}."
            )

    def _invalidate_session_credentials(self, account_num: str, email: str) -> None:
        """Drop a session profile's credential material, keeping its history.

        The next `cswap run` fails the reuse check and re-bootstraps from
        backup; the bootstrap merges .claude.json, so the profile's own
        projects/history survive. Used when backup credentials change under
        an existing profile (e.g. --import --force).
        """
        from claude_swap.session import (
            clear_session_stale,
            delete_macos_keychain_entry,
        )

        session_dir = self._session_dir(account_num, email)
        if not session_dir.exists():
            return
        delete_macos_keychain_entry(session_dir)
        (session_dir / ".credentials.json").unlink(missing_ok=True)
        clear_session_stale(session_dir)
        self._logger.info(
            f"Invalidated session credentials for account {account_num}"
        )

    def _delete_session_profile(self, account_num: str, email: str) -> None:
        """Remove an account's session profile dir and its keychain entry.

        Keychain first: the hashed service name is derived from the dir path
        and can't be recomputed once the dir is gone.

        The stale marker is a SIBLING of the dir, so ``rmtree`` does not take
        it: clear it explicitly, or the next profile created for this same
        slot+email inherits a re-bootstrap flag nothing set for it.
        """
        from claude_swap.session import (
            clear_session_stale,
            delete_macos_keychain_entry,
        )

        session_dir = self._session_dir(account_num, email)
        if session_dir.exists():
            delete_macos_keychain_entry(session_dir)
            shutil.rmtree(session_dir, ignore_errors=True)
        # NOT under that `if`. The marker lives OUTSIDE the dir, so it
        # outlives it: `purge` removes profile dirs (`iterdir()` + `is_dir()`)
        # and leaves the dot-file beside them by design. Early-returning on
        # the missing dir left that marker for the next profile in this slot,
        # which then re-bootstraps on a flag nothing set for it.
        cleared = clear_session_stale(session_dir)
        if session_dir.exists() or not cleared:
            # Both removals tolerate a denied dir, which is right -- the
            # caller has already deleted the credentials and must reach the
            # roster write. But it arrives there with the profile still on
            # disk, so an INFO saying it was removed is the only record, and
            # it is wrong. The surviving stale marker also makes the next
            # run's stale arm re-fire on this slot forever.
            self._logger.warning(
                "Could not fully remove account %s's session profile at %s; "
                "credentials are gone but the profile dir and/or its stale "
                "marker survive (check permissions on it and its parent).",
                account_num, session_dir,
            )
            return
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
        """Get sequence data. None ONLY when the roster does not exist yet.

        `strict=True` because ~59 call sites read this and 27 of them write
        the result back through `or {}` — so a torn or unreadable
        `sequence.json` read as "no accounts" and the next write rebuilt the
        roster from nothing. Measured on a torn file with a resident slot 1:
        `add_account` collapsed `_get_next_account_number` to 1, overwrote the
        live credential backup at :2934, and THEN died at :2941 with a raw
        TypeError that `cli.py`'s `except ClaudeSwitchError` does not catch —
        so `--json` emitted no envelope at all.

            before  sha256:296e3
            raised  TypeError    is_ClaudeSwitchError=False
            after   sha256:6aabc    DESTROYED
            with strict: ConfigError, backup unchanged

        Guarding each caller was the alternative and it is 27 edits that the
        28th forgets. This is the reader; the distinction belongs here."""
        return self._read_json(self.sequence_file, strict=True)

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

    def _live_identity_matches(self, email: str, org_uuid: str) -> bool:
        """Whether the live config identity is (email, org_uuid) right now.

        The under-lock TOCTOU identity re-check shared by the locked refresh
        and the rotated-backup resync: a switch or /login landing between a
        caller's pre-lock read and its lock acquisition changes this identity,
        and a mismatch means the live store is no longer the caller's account
        — nothing there is its to adopt, consume, or overwrite. Compares the
        organization too: two managed slots may share an email across orgs.
        """
        identity = self._get_current_account()
        return identity is not None and identity == (email, org_uuid or "")

    def _resolved_matches_slot_identity(
        self, account_num: str, resolved: dict
    ) -> bool | None:
        """Whether an oracle-resolved identity is this slot's account.

        ``resolved`` is a ``fetch_oauth_profile`` result (non-empty
        ``uuid``; ``email``/``organizationUuid`` possibly None). Uuid-first,
        like ``_classify_outgoing_credential``: uuids are stable where an
        email can be recycled across accounts. Tri-state:

        - True: same account (uuid match with a compatible org, or —
          when the slot has no stored uuid — an exact (email, org) match
          with the resolved org structurally present).
        - False: definitively another account (uuid conflict, a matching
          email under a different org — sibling accounts share emails
          across orgs — or a structurally complete resolved identity
          that matches neither field). Safe to cache.
        - None: unverifiable (slot has no uuid and the resolved identity
          is too partial to condemn or affirm). Must be treated like a
          probe failure — never cached.
        """
        own = self.account_identity(account_num)
        r_uuid = (resolved.get("uuid") or "").strip()
        r_email = resolved.get("email")
        r_org = resolved.get("organizationUuid")
        if r_uuid and own["uuid"]:
            # uuid is globally unique; the org only corroborates, so a
            # missing org on either side is tolerated (mirrors
            # _classify_outgoing_credential's own-rotated check).
            return r_uuid == own["uuid"] and (
                not r_org or not own["organizationUuid"]
                or r_org == own["organizationUuid"]
            )
        # Slot predates uuid tracking (add-token placeholder). Email alone
        # cannot affirm ownership — the same email legitimately exists
        # across personal/org accounts (the reason _live_identity_matches
        # compares the org too). Affirm only an exact (email, org) match
        # with the resolved org structurally present; a missing resolved
        # org is indistinguishable from a personal account, so it is
        # unverifiable — never affirmative, never condemning. On a match,
        # record the resolved uuid so future verdicts are uuid-positive.
        if r_email and r_email == own["email"]:
            if r_org is None:
                return None
            if (r_org or "") == own["organizationUuid"]:
                self.backfill_account_uuid(
                    account_num, r_uuid,
                    expected_email=r_email, expected_org=r_org or "",
                )
                return True
            return False
        if r_email and r_org is not None:
            return False
        return None

    def _lineage_key(
        self, account_num: str, email: str, fingerprint: str
    ) -> tuple[str, str, str, str, str, str]:
        """``_probe_verdicts`` key for a credential lineage, bound to the
        caller's account email AND the slot's full stored identity (email,
        org, uuid): a slot re-created for a different account — same
        number, same email across orgs, even an add-token record whose
        stored email changed while org and uuid stayed blank — must not
        inherit verdicts issued for its predecessor. Any mismatch on any
        component makes the lookup MISS (conservative: re-probe). Built
        fresh at every consult; slot mutations hold the account FileLock,
        so a consult under that lock also revalidates the identity the
        verdict was issued against.

        Accepted identity-model limit, not closed here: a uuid-less,
        org-less record removed and re-added with the SAME email is
        indistinguishable from its predecessor — the (email, org)
        composite IS identity for such records throughout the codebase
        (the switch-path classifier shares the property), so a slot
        generation counter would add state without adding evidence."""
        own = self.account_identity(account_num)
        return (
            account_num, email, own["email"], own["organizationUuid"],
            own["uuid"], fingerprint,
        )

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
        self._refuse_session_shell()
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

            current_creds = self._read_capture_credentials()
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
        current_creds = self._read_capture_credentials()
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
        self._refuse_session_shell()
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
        self._refuse_session_shell()
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
        self._record_active_verdict(None)
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
                self._record_active_verdict(active)
            else:
                creds = self._read_account_credentials(str(num), email)

            accounts_info.append((num, email, org_name, org_uuid, is_active, creds, alias))
        return accounts_info

    def _fetch_active_usage(
        self, account_num: str, email: str, creds: str, org_uuid: str = ""
    ) -> FetchRecord:
        """Usage fetch for the active/default account, refreshing an expired
        token under Claude Code's own lock protocol.

        Claude Code 2.1.218 is built to *adopt* an externally rotated
        credential rather than collide with it: its refresh takes the
        ``.oauth_refresh.lock`` + legacy ``.claude.lock`` pair, re-reads the
        store under the lock, and skips the network call when the token
        already changed (race-resolved); its 401 path re-reads the store
        before forcing re-auth. So a rotation performed under those same
        locks — re-check, POST, persist, release, all inside — is serialized
        against a live Claude Code and then adopted by it. An owner being
        present is therefore no longer a reason to leave an expired token
        dead (the old behavior stranded idle machines: the owner never
        refreshed, and the dead token 401'd the identity probe, cascading
        into ``unresolved`` switch bounces).

        Two invariants:

        - **Provenance (issue #117)**: a live credential is only CONSUMED
          (its grant POSTed) or WRITTEN into the slot backup when its
          lineage is attributed to the slot — backup-lineage match, a
          profile-oracle verdict from the fresh pass (see
          ``_resync_rotated_backup``), or a refresh POST of our own (memoed
          in ``_probe_verdicts``). Unattributable live bytes are never
          consumed or persisted — but when they are *dead* (expired) and
          the slot's own backup still holds a usable credential, the backup
          is restored to the live store: the backup is by definition the
          slot's credential, so no foreign lineage can be poisoned by it
          (measured field case: a stale cross-machine sync landing an
          already-superseded credential).
        - **Never discard a consumed generation**: once the refresh grant is
          POSTed, the successor is persisted unconditionally (active store +
          slot backup; backup even survives a failing live write). A consumed
          generation left as the live credential is the account-death shape —
          the token endpoint rejects its reuse (verified: invalid_grant on
          re-presentation, siblings unaffected).
        """
        oauth_data = oauth.extract_oauth_data(creds)
        if not oauth_data or not oauth_data.get("accessToken"):
            return FetchRecord(sentinel=USAGE_NO_CREDENTIALS)

        # Every defer before the grant is consumed routes through this: a
        # genuinely expired token earns the sentinel, but a locally-valid
        # server-401'd one (force_refresh set) must surface its 401 record —
        # the store then paces retries with backoff/strike accounting instead
        # of "token expired" mislabeling an unexpired token.
        def _defer(record: "FetchRecord | None") -> "FetchRecord":
            return record or FetchRecord(sentinel=USAGE_TOKEN_EXPIRED)

        # The CONSUME LOCK, taken in the gate's own order (consume -> global).
        # This path can POST the slot's BACKUP refresh token — refresh_input
        # becomes `backup` when the live bytes moved or were cleared — so it
        # is a second backup-token POST outside consume_backup_grant, and the
        # gate's mutual exclusion was not total. Measured: with
        # .consume-N.lock held by another process, this still POSTed.
        #
        # The interleaving it closes: is_active is decided once per collect
        # pass, so a pass that started before a `cswap switch` routes slot N
        # through the gate while a later pass treats N as active and arrives
        # here. The gate releases the global lock across its POST by design,
        # so this path could take it, read the same lineage and POST it too —
        # one wins, the loser gets invalid_grant and strikes a live account.
        force_refresh: FetchRecord | None = None
        if not oauth.is_oauth_token_expired(oauth_data.get("expiresAt")):
            outcome = oauth.try_fetch_usage_for_account(
                account_num, email, creds, is_active=True,
            )
            if outcome.error != "http-401":
                if outcome.usage is not None:
                    # The server just accepted this credential. If its
                    # lineage differs from the slot backup, CC rotated during
                    # normal use and nothing resynced the backup
                    # (rotation-before-collection): the backup holds the
                    # consumed predecessor, and at the next expiry the
                    # recovery branch would POST that dead grant —
                    # invalid_grant, and a healthy slot quarantined. Resync
                    # now, under the same guards as the adopt branch — but
                    # never from a DEGRADED read: the keychain-fallback
                    # plaintext may itself be the consumed predecessor
                    # (still server-valid for its access-token tail), and
                    # writing it would clobber a fresher backup. Serving is
                    # fine; writing waits for a non-degraded pass.
                    if not self._active_read_degraded:
                        self._resync_rotated_backup(
                            account_num, email, org_uuid, creds
                        )
                    if self._probe_verdicts and self._probe_verdicts.get(
                        self._lineage_key(
                            account_num, email,
                            oauth.credential_fingerprint(creds) or "",
                        )
                    ) is False:
                        # The probe just proved the served credential is
                        # another account's: its quota is not this slot's,
                        # and recording it would poison history and switch
                        # decisions (#117's mis-keying shape). The sentinel
                        # reads as unknown headroom to autoswitch, whose
                        # failover switch stashes the foreign credential
                        # and restores the slot's backup — the repair.
                        return FetchRecord(
                            sentinel=USAGE_FOREIGN_CREDENTIAL
                        )
                return FetchRecord(
                    usage=outcome.usage,
                    error=outcome.error,
                    retry_after_s=outcome.retry_after_s,
                )
            # A locally-valid token the server rejects: revoked out-of-band
            # (measured: a sibling machine rotating a synced lineage kills
            # the predecessor access token before its expiresAt) or clock
            # skew. Mirror CC's own 401 reaction — refresh — instead of
            # letting the store's failure backoff loop a dead token for
            # hours until it expires locally. Kept as the fallback record:
            # when no recovery path exists the 401 must reach the store as
            # an ERROR (backoff, strike accounting), not a "token expired"
            # sentinel mislabeling an unexpired token.
            force_refresh = FetchRecord(
                error=outcome.error, retry_after_s=outcome.retry_after_s,
            )

        # Expired (or server-rejected). Before any recovery that would
        # CONSUME a refresh token: a degraded read (the OAuth Keychain
        # failed and a fallback covered it) may be serving a stale
        # generation — on macOS Claude Code rotates keychain-only, so the
        # plaintext file and the slot backup can both hold the consumed
        # predecessor and AGREE with each other. POSTing that rt would
        # yield invalid_grant and a false dead-token strike on a live
        # account (measured field incident). Adopt/serve stays allowed
        # above; consumption is refused until the keychain reads again —
        # CC refreshes on its own next use, exactly the pre-#167 shape.
        if self._active_read_degraded:
            return _defer(
                force_refresh
                or FetchRecord(sentinel=USAGE_KEYCHAIN_UNAVAILABLE)
            )

        # Store-resolution parity (M4), same refusal as the consume gate:
        # with CLAUDE_SECURESTORAGE_CONFIG_DIR set, CC reads/writes a
        # redirected store while this path resolves the default one (capture
        # mirrors it since #205; this one does not) — the copy about to be
        # consumed is the stale predecessor by construction.
        # Serving usage on a still-valid token (above) is fine; consuming
        # or persisting against the left-behind store is not. The distinct
        # kind surfaces the remedy (ERROR_NOTES) instead of striking a
        # healthy account.
        if os.environ.get("CLAUDE_SECURESTORAGE_CONFIG_DIR"):
            self._logger.warning(
                "CLAUDE_SECURESTORAGE_CONFIG_DIR is set; cswap mirrors it "
                "when capturing a credential but not when refreshing one, "
                "so refusing to refresh account %s's active credential "
                "(unset the variable or run from a normal shell).",
                account_num,
            )
            return FetchRecord(error="store-unmirrored")

        # Attribution against the slot's
        # stored backup decides HOW to recover, never whether to give up
        # outright: attributable live → refresh it; unattributable live but
        # usable backup → restore the backup (the slot's own credential —
        # the stranded-live and stale-sync shapes both heal here).
        backup = self._read_account_credentials(account_num, email)
        backup_fp = oauth.credential_fingerprint(backup)
        backup_oauth = oauth.extract_oauth_data(backup)
        backup_usable = bool(
            backup_oauth
            and backup_oauth.get("accessToken")
            and backup_oauth.get("refreshToken")
        )
        attributable = creds == backup or (
            oauth.credential_fingerprint(creds) == backup_fp
        )
        if not attributable and not backup_usable:
            # Nothing safe to consume and nothing to restore from. Warn once
            # per condition, not per collect pass.
            if (account_num, email, "unattributable") not in self._provenance_warned:
                self._provenance_warned.add((account_num, email, "unattributable"))
                self._logger.warning(
                    "Active credential does not match Account-%s's stored "
                    "backup and the backup is unusable; cannot refresh "
                    "(provenance unknown).",
                    account_num,
                )
            return _defer(force_refresh)
        self._provenance_warned.discard((account_num, email, "unattributable"))

        # Claude Code's own sequence: locks → re-read → decide → POST →
        # persist unconditionally → release. A concurrently refreshing CC is
        # serialized here and adopts our rotation on its next locked re-read.
        try:
            # Lock order matches the switch path (switch_to): cswap's own
            # account lock first, then Claude Code's. FileLock excludes
            # concurrent swap/move relocations (their docstring relies on
            # usage-refresh persists taking this lock); the CC pair excludes
            # a concurrently refreshing Claude Code. Nothing inside
            # re-acquires FileLock, so the a07c767 non-reentrancy hazard
            # does not apply.
            # The config lock is NOT taken here: CC holds only the
            # credential locks across its POST, and the config lock guards a
            # local ~/.claude.json RMW with a ~10s retry budget on CC's side
            # — holding it through a slow POST could exhaust a concurrent CC
            # config save's retries. It is narrowed to the live-store write
            # below (the one step that can touch ~/.claude.json).
            with (
                FileLock(self.credentials_dir / f".consume-{account_num}.lock"),
                FileLock(self.lock_file),
                claude_credentials_lock(),
            ):
                live = self._read_credentials()
                if live is None:
                    # Read ERROR (locked keychain, unreadable store) — not
                    # absence. The store may hold a newer credential we
                    # cannot see; guessing here could consume a superseded
                    # grant. Defer to the next pass.
                    return _defer(force_refresh)
                live_oauth = oauth.extract_oauth_data(live) if live else None
                # Under-lock TOCTOU guards. A `cswap switch` or `/login`
                # completing between the pre-lock attribution and lock
                # acquisition replaces the live credential (and the config
                # identity). Two independent checks, because rotation and
                # switching move different markers:
                # - identity (config oauthAccount, email AND organization —
                #   two managed slots may share an email across orgs): a
                #   switch/login changes it; a CC token rotation does not.
                #   Mismatch → the live store now belongs to another account
                #   — nothing here is ours to adopt, consume, or overwrite.
                #   Runs even when the live blob is empty or non-OAuth
                #   (live_oauth None): a switch to an API-key account landing
                #   in the gap leaves exactly that shape. An empty live WITH
                #   our identity (CC cleared the credential) still passes —
                #   that is a recovery case.
                # - lineage (refresh-token fingerprint): decides whether the
                #   live bytes may be CONSUMED or must be replaced from the
                #   backup.
                if not self._live_identity_matches(email, org_uuid):
                    return _defer(force_refresh)
                if (
                    live_oauth
                    and live != creds
                    # A CC invalid_grant wipe empties the token fields in
                    # place but keeps metadata (observed on 2.1.181), and an
                    # external writer can land an accessToken-only blob —
                    # either way a "non-expired" look without a full token
                    # pair must not be adopted (the resync would replace the
                    # backup's only refresh token).
                    and live_oauth.get("accessToken")
                    and live_oauth.get("refreshToken")
                    and not oauth.is_oauth_token_expired(
                        live_oauth.get("expiresAt")
                    )
                ):
                    # Someone (a live CC) already rotated it — adopt, consume
                    # nothing. Mirrors CC's race-resolved path. Resync the
                    # slot backup so the rotated lineage stays attributable
                    # at the NEXT expiry — but only when the lineage is
                    # attributable NOW (backup lineage, or an oracle/memo
                    # verdict): a foreign fresh credential under a stale
                    # config satisfies every local condition here, and
                    # writing it would destroy the slot's refresh token. An
                    # unverified lineage is adopted for usage only (network
                    # is forbidden under these locks); the next collect pass
                    # reads it fresh and the oracle-checked resync heals the
                    # backup one pass late.
                    live_verdict = self._probe_verdicts.get(
                        self._lineage_key(
                            account_num, email,
                            oauth.credential_fingerprint(live) or "",
                        )
                    )
                    if live_verdict is False:
                        # Known-foreign: don't adopt, don't serve usage
                        # mislabeled as this slot's. The foreign sentinel
                        # (not a defer) so autoswitch fails over instead of
                        # idle-holding — the switch is what repairs the
                        # drift.
                        return FetchRecord(
                            sentinel=USAGE_FOREIGN_CREDENTIAL
                        )
                    working = live
                    if live_verdict or (
                        oauth.credential_fingerprint(live) == backup_fp
                    ):
                        try:
                            self._write_account_credentials(
                                account_num, email, live
                            )
                        except Exception:
                            self._logger.warning(
                                "Backup resync after adopting a rotated "
                                "credential failed for account %s; the next "
                                "expiry may refuse to refresh until a "
                                "switch resyncs it.", account_num,
                            )
                    else:
                        self._logger.debug(
                            "Adopted a rotated live credential for account "
                            "%s without a lineage verdict; backup resync "
                            "deferred to the next fresh pass's oracle "
                            "check.", account_num,
                        )
                else:
                    # Pick the credential whose grant may be consumed:
                    # - live, when its lineage matches the backup (rotated /
                    #   drifted bytes of this slot);
                    # - the backup itself, when the live bytes moved to a
                    #   foreign-but-dead lineage or were cleared (restore);
                    # - what the collector read, as the last resort.
                    # Foreign live bytes that appeared only mid-flight (live
                    # differs from what the collector read AND from the
                    # backup lineage) mean an actor is mutating the store
                    # right now — defer rather than fight it.
                    restore_source = None
                    if live_oauth is not None and (
                        oauth.credential_fingerprint(live) == backup_fp
                    ):
                        # Live is the slot's own lineage (possibly drifted) —
                        # its bytes are the freshest copy of the grant.
                        refresh_input = (
                            live if live_oauth.get("refreshToken") else
                            (backup if backup_usable else creds)
                        )
                    elif not live:
                        # CC cleared the live store — recover from the
                        # backup's grant.
                        refresh_input = backup if backup_usable else creds
                    elif live == creds:
                        # Nothing moved since the collector read, but the
                        # bytes don't match the backup's lineage. The
                        # generation ordering (expiresAt moves forward on
                        # every rotation) SELECTS a candidate, but only an
                        # ownership verdict LICENSES consuming it:
                        # - backup newer → live is a stranded consumed
                        #   generation or a stale external sync; the backup
                        #   is the slot's real credential — restore or
                        #   refresh from it, never POST the dead live grant.
                        # - live newer AND this process attributed the
                        #   lineage (own refresh POST, or a fresh-pass
                        #   oracle match whose backup write failed) — POST
                        #   live, the valid successor.
                        # - live newer but unattributed → could equally be a
                        #   foreign credential under a stale config; POSTing
                        #   would consume another machine's grant. Defer to
                        #   CC's next use (pre-#167 behavior for this shape
                        #   — the slept-through-rotation and cross-process
                        #   drift subcases give up auto-heal by design).
                        live_exp = (live_oauth or {}).get("expiresAt") or 0
                        backup_exp = (
                            backup_oauth.get("expiresAt") or 0
                            if backup_oauth else 0
                        )
                        if (
                            live_oauth
                            and live_oauth.get("refreshToken")
                            and live_exp > backup_exp
                        ):
                            if self._probe_verdicts.get(
                                self._lineage_key(
                                    account_num, email,
                                    oauth.credential_fingerprint(live) or "",
                                )
                            ):
                                refresh_input = live
                            else:
                                key = (account_num, email,
                                       "expiry-unattributed")
                                if key not in self._provenance_warned:
                                    self._provenance_warned.add(key)
                                    self._logger.warning(
                                        "Live credential is newer than "
                                        "Account-%s's backup but its "
                                        "ownership is unverified; refresh "
                                        "deferred to Claude Code's next "
                                        "use.", account_num,
                                    )
                                return _defer(force_refresh)
                        else:
                            refresh_input = backup if backup_usable else creds
                    else:
                        # Live moved mid-flight to bytes that are neither
                        # what the collector read nor the backup's lineage —
                        # another actor is mutating the store; defer.
                        return _defer(force_refresh)
                    input_oauth = oauth.extract_oauth_data(refresh_input)
                    if (
                        refresh_input == backup
                        and backup_usable
                        and not force_refresh
                        and input_oauth
                        and not oauth.is_oauth_token_expired(
                            input_oauth.get("expiresAt")
                        )
                    ):
                        # The backup already holds a live, non-expired
                        # credential (a prior locked refresh persisted it but
                        # the live write failed, stranding the live store on
                        # the consumed generation). Restore it — no POST, no
                        # generation consumed.
                        restore_source = backup
                        working = backup
                    else:
                        # The POST runs while holding the account FileLock
                        # (contended by `cswap switch` with a 10s acquire
                        # budget) and CC's credential locks. Bound it well
                        # inside that budget so a slow network can't make a
                        # concurrent switch's acquire expire — the switch
                        # then waits out the tail instead of erroring.
                        result = oauth.try_refresh_oauth_credentials(
                            refresh_input, timeout_s=6.0
                        )
                        if result.error in (
                            "invalid_grant", "no_refresh_token"
                        ) or (
                            result.error is None and not result.credentials
                        ):
                            # Permanently unrefreshable: dead lineage or a
                            # credential with no refresh token at all.
                            # Demotion check before condemning: re-read the
                            # SOURCE the POSTed bytes came from — a lineage
                            # that moved while our POST was in flight means
                            # we consumed a superseded copy (a writer raced
                            # us), which is evidence about OUR bytes, not
                            # the slot. Compare like with like: live-sourced
                            # input against the live store, backup-sourced
                            # input against the backup (comparing a backup
                            # input to the live store reads "moved" on every
                            # pass by construction — a permanent false
                            # negative that would keep a dead lineage out
                            # of quarantine forever). Record transient; the
                            # next pass consumes the newer lineage. (#121
                            # discipline: local reads only, no network,
                            # failure degrades to today.)
                            try:
                                if refresh_input == backup:
                                    source_now = (
                                        self._read_account_credentials(
                                            account_num, email
                                        )
                                    )
                                else:
                                    source_now = (
                                        self._read_credentials() or ""
                                    )
                                moved = (
                                    bool(source_now)
                                    and oauth.credential_fingerprint(
                                        source_now
                                    )
                                    != oauth.credential_fingerprint(
                                        refresh_input
                                    )
                                )
                            except Exception:
                                moved = False
                            if moved:
                                return FetchRecord(error="refresh-failed")
                            # Surface as an ERROR so the store advances auth
                            # strikes, applies backoff, and the quarantine
                            # scan flips the account to "re-login needed" —
                            # a bare sentinel is a no-op to the store and
                            # would re-POST every pass. The strike binds to
                            # the consumed generation's fingerprint.
                            return FetchRecord(
                                error=result.error or "invalid_grant",
                                struck_fp=oauth.credential_fingerprint(
                                    refresh_input
                                ),
                            )
                        if result.error is not None:
                            # Transient (network) failure: backoff via store.
                            return FetchRecord(error="refresh-failed")
                        working = result.credentials
                        # Our own POST produced this lineage — self-attributed,
                        # no oracle needed. The verdict is what lets the next
                        # expiry consume it if the backup write below fails.
                        self._probe_verdicts[
                            self._lineage_key(
                                account_num, email,
                                oauth.credential_fingerprint(working or "")
                                or "",
                            )
                        ] = True
                        self._provenance_warned.discard(
                            (account_num, email, "expiry-unattributed")
                        )
                    # The credential must reach the stores — after a POST the
                    # grant is consumed and the successor MUST survive in at
                    # least one of them. Attempt both; tolerate either
                    # failing alone. (For a restore, the backup already holds
                    # it; only the live store needs the write.)
                    backup_ok = live_ok = True
                    if restore_source is None:
                        try:
                            self._write_account_credentials(
                                account_num, email, working
                            )
                        except Exception:
                            backup_ok = False
                            self._logger.warning(
                                "Backup write failed after a consumed "
                                "refresh for account %s; attempting the "
                                "active store.",
                                account_num,
                            )
                    try:
                        # _write_credentials can touch ~/.claude.json (via
                        # _clear_managed_key) — the config lock covers just
                        # this write. A timeout here is a live-write failure
                        # (the grant is already consumed), not a defer.
                        with claude_config_lock():
                            self._write_credentials(working)  # active store — CC reads this
                    except Exception:
                        live_ok = False
                        self._logger.warning(
                            "Active-store write failed after a %s for "
                            "account %s%s.",
                            "backup restore" if restore_source is not None
                            else "consumed refresh",
                            account_num,
                            "" if backup_ok
                            else "; the rotated credential was NOT persisted "
                                 "anywhere — re-login may be required",
                        )
                    if not live_ok:
                        # Live still holds the dead token — don't serve
                        # usage for a credential CC can't currently use.
                        return FetchRecord(sentinel=USAGE_TOKEN_EXPIRED)
        except LockError:
            # A live holder — Claude Code mid-refresh (ClaudeCodeLockTimeout)
            # or another cswap operation holding the account FileLock. Either
            # way the credential is being handled; try again next tick rather
            # than steal, wait unboundedly, or raise through the never-raises
            # fetch contract.
            self._logger.info(
                "Credential locks held elsewhere; deferring the "
                "active-token refresh for account %s to the next pass.",
                account_num,
            )
            return _defer(force_refresh)
        except Exception:
            # _fetch_account_usage promises never to raise into the collect
            # pass (a raising worker would kill the whole pass for every
            # account). Config/lock-file I/O errors land here.
            self._logger.warning(
                "Active-token refresh for account %s failed unexpectedly; "
                "deferring to the next pass.", account_num, exc_info=True,
            )
            return _defer(force_refresh)

        outcome = oauth.try_fetch_usage_for_account(
            account_num, email, working, is_active=True,
        )
        return FetchRecord(
            usage=outcome.usage,
            error=outcome.error,
            retry_after_s=outcome.retry_after_s,
        )

    def _resync_rotated_backup(
        self, account_num: str, email: str, org_uuid: str, creds: str
    ) -> None:
        """Resync the slot backup after a rotation that completed elsewhere.

        The fresh-token fast path serves usage off a credential the server
        just accepted. When that credential's lineage differs from the slot
        backup, Claude Code rotated during normal use and nothing resynced
        the backup (rotation-before-collection): the backup still holds the
        consumed predecessor, and the next expiry's recovery branch would
        POST that dead grant — invalid_grant on a healthy slot. This is the
        adopt-branch resync extended to a rotation that already completed:
        same identity re-check, same full-token-pair guard, same locks.

        The config identity alone cannot attribute the drifted bytes: a
        foreign credential can occupy the live store while ``~/.claude.json``
        still names this slot (partial cross-machine sync of the credential
        store; a poll landing inside ``/login``'s non-atomic write), and
        writing those bytes would destroy the slot's only surviving refresh
        token. So the drifted lineage — including the empty-backup *seeding*
        case — must be attributed by the profile oracle before it is
        persisted: probed here, before any lock (network under locks is
        forbidden), while the access token is known-fresh (the usage
        endpoint just accepted it). Definitive verdicts are memoized per
        lineage so a persistent drift state doesn't re-probe every pass.

        Best-effort: any failure (lock contention, read error, identity
        moved, oracle unreachable) just leaves the backup stale — the
        recovery branch consumes nothing it cannot attribute. Never raises.
        """
        try:
            creds_oauth = oauth.extract_oauth_data(creds)
            if not (
                creds_oauth
                and creds_oauth.get("accessToken")
                and creds_oauth.get("refreshToken")
            ):
                return  # never seed a backup with a partial token pair
            backup = self._read_account_credentials(account_num, email)
            if backup and (
                oauth.credential_fingerprint(creds)
                == oauth.credential_fingerprint(backup)
            ):
                return  # same lineage — nothing drifted
            fp = oauth.credential_fingerprint(creds) or ""
            verdict = self._probe_verdicts.get(
                self._lineage_key(account_num, email, fp)
            )
            if verdict is False:
                return  # known-foreign lineage; already warned
            if verdict is not True:
                resolved = oauth.fetch_oauth_profile(
                    oauth.extract_access_token(creds) or ""
                )
                if resolved is None:
                    self._logger.debug(
                        "Ownership probe for account %s's drifted live "
                        "credential failed; resync skipped this pass.",
                        account_num,
                    )
                    return
                match = self._resolved_matches_slot_identity(
                    account_num, resolved
                )
                if match is None:
                    self._logger.debug(
                        "Ownership of account %s's drifted live credential "
                        "is unverifiable (no stored uuid, partial profile); "
                        "resync skipped this pass.",
                        account_num,
                    )
                    return
                # Key built AFTER the match: an email-path affirmation just
                # backfilled the slot uuid, and the verdict must live under
                # the identity consults will rebuild from now on.
                self._probe_verdicts[
                    self._lineage_key(account_num, email, fp)
                ] = match
                if not match:
                    key = (account_num, email, "resync")
                    if key not in self._provenance_warned:
                        self._provenance_warned.add(key)
                        self._logger.warning(
                            "Live credential resolves to a different "
                            "account than Account-%s's identity; backup "
                            "left untouched (foreign credential under a "
                            "stale config).",
                            account_num,
                        )
                    return
                self._provenance_warned.discard((account_num, email, "resync"))
            with (
                FileLock(self.lock_file),
                claude_credentials_lock(),
            ):
                # Identity re-check under the lock: a switch/login landing in
                # the gap means the live store is no longer this account's.
                if not self._live_identity_matches(email, org_uuid):
                    return
                # Verdict re-check under the lock: slot mutations hold this
                # FileLock, so rebuilding the key revalidates that the slot
                # still IS the account the oracle affirmed.
                if not self._probe_verdicts.get(
                    self._lineage_key(account_num, email, fp)
                ):
                    return
                # Re-read live under the lock and require it to still carry
                # the served (and oracle-attributed) credential's lineage
                # with a full pair — the fingerprint covers the refresh
                # token, so the bytes written share the probed lineage even
                # if the access token moved since the probe.
                live = self._read_credentials()
                if not live:
                    return
                live_oauth = oauth.extract_oauth_data(live)
                if not (
                    live_oauth
                    and live_oauth.get("accessToken")
                    and live_oauth.get("refreshToken")
                    and oauth.credential_fingerprint(live)
                    == oauth.credential_fingerprint(creds)
                ):
                    return
                self._write_account_credentials(account_num, email, live)
                self._logger.info(
                    "Resynced account %s's backup to the rotated live "
                    "credential (rotation completed outside a collect pass).",
                    account_num,
                )
        except LockError:
            return  # holder is mid-operation; the next pass retries
        except Exception:
            self._logger.warning(
                "Backup resync for account %s failed; the recovery branch's "
                "newer-generation check still guards the next expiry.",
                account_num, exc_info=True,
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
            if is_active and (
                self._active_keychain_unavailable or self._active_read_unreadable
            ):
                return USAGE_KEYCHAIN_UNAVAILABLE
            if not is_active and self._read_account_credentials_ex(
                str(num), email
            )[1]:
                # THIS slot's own read, not the process flag — which one
                # slot's clean read erased for every other slot. Measured with
                # every read denied and a real backup on slot 2:
                #
                #     before   slot2='no credentials'   slot9='no credentials'
                #     after    slot2='keychain unavailable'
                #
                # "no credentials" sends the user to re-add a slot that has one
                # — the dead end 41313b9 removed from three other sites.
                return USAGE_KEYCHAIN_UNAVAILABLE
            return USAGE_NO_CREDENTIALS
        # An expired active token is no longer a static state: the fetch path
        # refreshes it under Claude Code's own lock protocol (owner or not),
        # so the collect pass must reach it rather than short-circuit here.
        # USAGE_TOKEN_EXPIRED now only surfaces from the fetch path itself
        # (unattributable lineage, dead lineage, lock contention, failed
        # persist) — states that genuinely need the autoswitch ladder.
        return None

    def _fetch_account_usage(
        self, account_info: tuple[int, str, str, str, bool, str, str]
    ) -> FetchRecord:
        """One network fetch for one account. Never raises."""
        num, email, _, org_uuid, is_active, creds, _alias = account_info

        # The active/default account owns the live credential — route it
        # through the locked-refresh path (refreshes an expired token under
        # Claude Code's own lock protocol, owner or not).
        if is_active:
            return self._fetch_active_usage(str(num), email, creds, org_uuid)

        from claude_swap.session import (
            read_session_credentials,
            read_session_identity,
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
                # A provably matching profile remains the credential owner
                # while idle. Its expired access token needs Claude's native
                # refresh; falling back to the backup can test a consumed
                # generation and falsely quarantine the whole account. A live
                # process already proves profile ownership; an idle profile
                # must carry exact identity plus refresh material.
                if session_oauth.get("refreshToken") and (
                    has_live_session
                    or read_session_identity(session_dir)
                    == (email, org_uuid or "")
                ):
                    return FetchRecord(sentinel=USAGE_TOKEN_EXPIRED)
                session_creds = None

        outcome = oauth.try_fetch_usage_for_account(
            str(num), email, creds,
            is_active=has_live_session,
            refresh_via=(
                None if has_live_session else self.consume_backup_grant
            ),
        )
        return FetchRecord(
            usage=outcome.usage,
            error=outcome.error,
            retry_after_s=outcome.retry_after_s,
            struck_fp=outcome.struck_fp,
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
            return dict(
                executor.map(self._with_active_verdict(fetch_one), enumerate(infos))
            )

    def _collect_usage_entries(
        self,
        accounts_info: list[tuple[int, str, str, str, bool, str, str]],
        fetch: set[str] | None = None,
        *,
        scheduled: bool = False,
    ) -> dict[str, UsageEntry]:
        """Store-backed usage collection: one :class:`UsageEntry` per account.

        ``fetch=None`` (on-demand callers: ``--list``/``--status``/switch
        strategies, dashboards) makes every account a candidate but respects
        the persisted poll plans; the auto engine passes an explicit set whose
        members may beat the serve TTL when their plan says so (urgent
        cadence) or, unless ``scheduled`` is set, when escalation needs them
        fresh. Final eligibility —
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
        # Scoped-window models so the 429-stale trust bound honors per-model
        # (e.g. Fable) resets, matching the poll planner's window view.
        _threshold, models = self._poll_policy_inputs()
        sentinels: dict[str, str] = {}
        for num, info in info_by_num.items():
            static = self._static_usage_sentinel(info)
            if static is not None:
                sentinels[num] = static

        entries = store.entries(identities, models)
        # Dead refresh-token lineage: quarantine. Surfacing the sentinel here both
        # drives the "re-login needed" display and (via ``num not in sentinels``
        # below) stops the endless fetch loop that would otherwise 401/429 forever.
        for num in info_by_num:
            if num in sentinels:
                continue
            entry = entries[num]
            _i = info_by_num[num]
            if self._entry_token_dead(entry, num, _i[1], _i[5], _i[4]):
                sentinels[num] = self._dead_lineage_sentinel(num, _i)
            elif entry.auth_dead_strikes and entry.token_dead():
                # Struck, but no stored source still matches the condemned
                # generation — the fingerprint healed the verdict.
                # Clear the stale strike ROW too: display and fetch
                # eligibility (_row_eligible gates on the raw count) must
                # agree, or the slot silently freezes at last-good.
                self._usage_store.clear_dead_token(
                    [num], {num: identities[num]}
                )
                entries = store.entries(identities, models)
        requested = [
            num
            for num in info_by_num
            if num not in sentinels and (fetch is None or num in fetch)
        ]
        if fetch is None:
            # Repair reset-parked plans written by releases that stopped
            # polling exhausted accounts until their advertised reset. The
            # store recognizes that impossible deadline shape under the same
            # lock that installs the claim, so a concurrent valid replan is
            # never bypassed.
            claims = store.reserve(
                requested,
                identities,
                respect_plans=True,
                repair_overslept=True,
            )
        else:
            claims = store.reserve(
                requested,
                identities,
                respect_plans=False,
                repair_overslept=scheduled,
            )
        # An expired ACTIVE credential that cannot reach the fetch path (and
        # its locked refresh) this tick — failure backoff, a concurrent
        # collector's claim, poll-plan gate — must still surface the expired
        # state so the auto engine idle-holds instead of counting the gap
        # toward a spurious failover (Finding 2). When the gate lifts, the
        # fetch path refreshes the token and the sentinel clears itself.
        for num, info in info_by_num.items():
            if num in sentinels or not info[4]:  # info[4] = is_active
                continue
            if num in claims:
                continue  # the fetch path will handle (or sentinel) it now
            active_oauth = oauth.extract_oauth_data(info[5])
            if active_oauth and oauth.is_oauth_token_expired(
                active_oauth.get("expiresAt")
            ):
                sentinels[num] = USAGE_TOKEN_EXPIRED

        if claims:
            pre = entries
            records = self._run_usage_fetches(
                [info_by_num[num] for num in claims]
            )
            plans = self._plans_after_fetch(records, pre, info_by_num)
            accepted = store.record(records, identities, claims, plans)
            accepted_records = {
                num: record for num, record in records.items() if num in accepted
            }
            for num, record in accepted_records.items():
                if record.sentinel is not None:
                    sentinels[num] = record.sentinel
            entries = store.entries(identities, models)
            # A fetch that just returned invalid_grant advances the strike to the
            # dead threshold. The pre-fetch quarantine scan above couldn't see it,
            # so surface "re-login needed" in *this* pass instead of leaving the
            # slot looking merely refresh-failed until the next refresh notices.
            for num in accepted:
                _i = info_by_num[num]
                if self._entry_token_dead(
                    entries[num], num, _i[1], _i[5], _i[4]
                ):
                    sentinels[num] = USAGE_RELOGIN_REQUIRED

        return {
            num: with_sentinel(entries[num], sentinels.get(num))
            for num in info_by_num
        }

    def _slot_token_dead(self, num: str, email: str) -> bool:
        """Is this slot quarantined as refresh-token-dead, right now?

        The same question :meth:`_entry_token_dead` answers for the collectors,
        reachable from a caller that has only a slot number — `cswap import`'s
        auto-heal, which must agree with them: the heal exists to release a
        quarantine the collectors imposed, so a different verdict means the
        remedy the "re-login needed" message names silently does nothing.

        In particular the ACTIVE slot has two stored sources, and a strike may
        be bound to either (see :meth:`_entry_token_dead`). Comparing only the
        backup — as the import used to — leaves an active slot struck on its
        live generation unhealable, and that is the slot most likely to be
        quarantined in the first place.
        """
        # The org uuid is part of the row identity (UsageStore._matches
        # compares it for EQUALITY), so an empty one silently matches nothing:
        # every real account carries a non-empty org, and the lookup would
        # return a blank entry whose token_dead() is always False.
        data = self._get_sequence_data() or {}
        org = (
            (data.get("accounts", {}).get(num) or {})
            .get("organizationUuid", "") or ""
        )
        ident = {num: (email, org)}
        entry = self._usage_store.entries(ident).get(num)
        if entry is None:
            return False
        is_active = num == self.current_account_number()
        # The backup is a stored source on BOTH paths — directly when idle,
        # and as _entry_token_dead's second source when active — and each
        # turns an unreadable one into "dead" by a different route: an empty
        # fingerprint skips token_dead's binding check, and the active path
        # deliberately HOLDS a strike it cannot disprove (right for the
        # collectors, wrong here). Both end in `import_accounts` replacing a
        # healthy slot without --force. We cannot see it, so we cannot
        # condemn it.
        backup, unreadable = self._read_account_credentials_ex(num, email)
        if unreadable:
            return False
        # The stored source, as _build_accounts_info reports it: the LIVE
        # credential for the active slot, the backup otherwise. `.value` is
        # tri-state (`""` genuinely absent, `None` a read ERROR) — collapsing
        # it with `or ""` fed `credential_fingerprint("")` (None) into
        # `token_dead`, which treats a None stored_fp as "binds
        # unconditionally" and condemned a slot whose live credential simply
        # could not be read this instant. Same "we cannot see it, we cannot
        # condemn it" rule as the backup guard above.
        if is_active:
            active_value = self._store._read_active_credentials().value
            if active_value is None:
                return False
            stored = active_value
        else:
            stored = backup
        return self._entry_token_dead(entry, num, email, stored, is_active)

    def _entry_token_dead(
        self,
        entry: UsageEntry,
        num: str,
        email: str,
        stored: str,
        is_active: bool,
    ) -> bool:
        """Fingerprint-bound dead verdict against EVERY stored source.

        For an idle slot ``info[5]`` is the backup, the only source a strike
        can bind to. The ACTIVE slot has two stored sources — ``info[5]`` is
        the LIVE credential, but ``_fetch_active_usage``'s recovery branch
        legitimately POSTs (and binds the strike to) the slot BACKUP.
        Comparing the strike only against the live bytes mis-heals it on
        every pass whenever the two lineages differ — the strike/heal/re-POST
        loop that keeps a dead backup out of quarantine forever. The strike
        holds while ANY stored source still matches the struck generation.
        """
        if entry.token_dead(stored_fp=oauth.credential_fingerprint(stored)):
            return True
        if not is_active:
            return False
        backup, unreadable = self._read_account_credentials_ex(num, email)
        if unreadable:
            # The second source cannot be seen, so "no stored source matches
            # the struck generation" is unproven — and the caller's `elif`
            # spends that answer on `clear_dead_token`, which zeroes
            # authDeadStrikes AND struckFingerprint in the PERSISTED store.
            # One momentary lock would un-quarantine a genuinely dead account
            # permanently and resume POSTing its dead grant. Holding the
            # strike costs one pass of "re-login needed" on a row that
            # already took AUTH_DEAD_STRIKES invalid_grants; erasing it costs
            # the quarantine itself.
            return True
        return bool(backup) and entry.token_dead(
            stored_fp=oauth.credential_fingerprint(backup)
        )

    def _dead_lineage_sentinel(self, num: str, info: tuple) -> str:
        """Name the quarantine a dead stored lineage actually earns.

        A matching session profile supersedes the backup: an ``invalid_grant``
        recorded from that consumed backup generation says nothing about the
        owner-held session lineage. When an idle slot's session profile still
        holds this account's identity and an expired-but-refreshable OAuth
        credential, surface it as recoverable (``cswap recover``) rather than
        falsely demanding a browser re-login.

        The ACTIVE slot is excluded: its live credential is the one the strike
        was bound against, so there is no second lineage to appeal to.
        """
        from claude_swap.session import (
            read_session_credentials,
            read_session_identity,
        )

        account_num, email, _org_name, org_uuid, is_active, _creds, _alias = info
        if is_active:
            return USAGE_RELOGIN_REQUIRED
        session_dir = self._session_dir(str(account_num), email)
        session_oauth = oauth.extract_oauth_data(
            read_session_credentials(session_dir) or ""
        )
        if (
            session_oauth
            and session_oauth.get("accessToken")
            and session_oauth.get("refreshToken")
            and read_session_identity(session_dir) == (email, org_uuid or "")
            and oauth.is_oauth_token_expired(session_oauth.get("expiresAt"))
        ):
            return USAGE_TOKEN_EXPIRED
        return USAGE_RELOGIN_REQUIRED

    def _plans_after_fetch(
        self,
        records: dict[str, FetchRecord],
        pre: dict[str, UsageEntry],
        info_by_num: dict[str, tuple],
    ) -> dict[str, tuple[float | None, float | None]]:
        """Build successful-fetch cadence updates for atomic outcome commit.

        Failures are paced by the store's backoff and keep their past-due plan
        for when the backoff lifts.
        """
        now = self._usage_store.clock()
        threshold, models = self._poll_policy_inputs()
        plans: dict[str, tuple[float | None, float | None]] = {}
        for num, rec in records.items():
            if rec.sentinel is not None or rec.error is not None:
                continue
            before = pre.get(num)
            recent_429 = before is not None and before.recent_429(now)
            plans[num] = poll_policy.plan_after_fetch(
                prev_interval_s=before.poll_interval_s if before else None,
                prev_usage=before.last_good if before else None,
                new_usage=rec.usage,
                is_active=bool(info_by_num[num][4]),
                threshold=threshold,
                models=models,
                recent_429=recent_429,
                now=now,
            )
        return plans

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
            # No models needed: only fetched_at/next_poll_at is read here.
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
                    last_good_usage=entry.last_good,
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
                for line in self._token_status_lines(accounts_info[i]):
                    print(f"     {dimmed('•')} {muted(line)}")
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
        self._record_active_verdict(active)
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
        status, usage = usage_fields(entry.decision_value(), entry.fetched_at)
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
        else:
            active.update(
                last_good_usage_fields(
                    entry.last_good, entry.fetched_at, entry.age_s
                )
            )
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
        - ``"wiped"``          — an OAuth blob whose token fields are all
          empty: Claude Code's ``invalid_grant`` reaction empties
          ``accessToken``/``refreshToken`` in place, keeping the wrapper and
          metadata (observed live on 2.1.181). No token → the identity
          oracle is structurally silent, so this used to fall to
          ``"unresolved"`` and the fail-open backup copied the empty tokens
          over the slot's only surviving refresh token. Never written into
          any slot; nothing worth preserving either.
        - ``"alien"``          — a *structurally complete* identity (uuid +
          email + organization) that matches no managed slot (unmanaged
          login, recycled email wearing a managed address, or an email+org
          match without uuid confirmation). Preserved in a safety copy.
        - ``"known-foreign"``  — the switch-time oracle failed, but a
          collect-pass probe in this process already condemned this exact
          lineage (a cached definitive verdict, revalidated against the
          slot's current identity). Routed like ``"alien"``: preserved,
          never written — a transient probe failure must not let the
          fail-open backup poison the slot with bytes we have already
          proven foreign (this switch may BE the repair that verdict
          triggered).
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
        live_oauth = oauth.extract_oauth_data(original_creds)
        if live_oauth is not None and not (
            live_oauth.get("accessToken") or live_oauth.get("refreshToken")
        ):
            return ("wiped", None)
        resolved = provenance.get("resolved")
        if resolved is None or provenance.get("live") != original_creds:
            if self._probe_verdicts.get(
                self._lineage_key(
                    current_account, current_email,
                    oauth.credential_fingerprint(original_creds) or "",
                )
            ) is False:
                return ("known-foreign", None)
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

    def _read_target_credentials(self, account_num: str, email: str) -> str:
        """The switch target's stored credential, or a SwitchError naming why.

        One helper because `_perform_switch` reads the target twice — the
        direct-activation branch (fresh machine, post-import, --force) and
        the normal branch (every ordinary switch on a working install) — and
        only the first carried the unreadable check. The normal branch sent
        every ordinary `cswap switch` to "Re-add with: cswap --add-account",
        which burns the stored grant of a slot whose backup is merely behind
        a locked Keychain. `session.py`'s `_bootstrap` carried a third copy.
        """
        creds, unreadable = self._read_account_credentials_ex(
            account_num, email
        )
        if creds:
            return creds
        if unreadable:
            # The backup may exist but the Keychain cannot be read right now
            # (locked / non-GUI session) — a re-add would needlessly burn the
            # stored grant.
            raise SwitchError(
                f"Account-{account_num}'s backup is in the macOS Keychain "
                f"but it is unreadable right now (locked or no GUI "
                f"session). Retry from a GUI terminal; do not re-add."
            )
        raise SwitchError(
            f"Account-{account_num} has no stored credentials. "
            f"Re-add with: cswap --add-account --slot {account_num}"
        )

    def _refuse_session_shell(self) -> None:
        """Refuse live-store mutation from inside a ``cswap run`` shell.

        A ``CLAUDE_CONFIG_DIR`` pointing inside a session profile means this
        shell IS a session — its "live store" is the profile, not the
        default login; a switch/add here would splice the default sequence
        against the wrong live store (mirrors SessionManager's own guard).
        Called by every entry point that mutates the live store or the
        roster. There is no single chokepoint to hang this on: the one it
        used to claim was `_perform_switch`, which covers the switch family
        only, so `remove_account`, `swap_accounts`, `move_account`, `purge`
        and the alias setters all ran happily inside a session shell —
        `remove_account` deleting the session profile of the very shell it
        was running in. Nine call sites is the honest cost of that.
        """
        cfg_dir = os.environ.get("CLAUDE_CONFIG_DIR")
        if not cfg_dir:
            return
        try:
            Path(cfg_dir).resolve().relative_to(
                (self.backup_dir / "sessions").resolve()
            )
        except ValueError:
            return
        raise SwitchError(
            "This shell is inside a cswap run session profile "
            "(CLAUDE_CONFIG_DIR points at it). Mutating accounts here would "
            "operate on the wrong live store — unset CLAUDE_CONFIG_DIR "
            "or run from a normal shell."
        )

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
        self._refuse_session_shell()
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
                target_creds = self._read_target_credentials(
                    target_account, target_email
                )
                target_config = self._read_account_config(target_account, target_email)
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

                # Snapshot live state so a mid-operation failure can be
                # undone, config identity or not: a wiped or half-written
                # ~/.claude.json can orphan a live credential whose
                # machine-shared MCP state must still reach the composer
                # below (#135) — and the rollback, should activation fail
                # partway. Fail fast when the snapshot is unreadable (None:
                # the credentials file exists but could not be read) rather
                # than overwrite state that has no safety copy; "" means
                # absent in every backend and composes/restores nothing.
                rollback_config_text: str | None = None
                rollback_creds: str | None = self._read_credentials()
                if rollback_creds is None:
                    raise CredentialReadError(
                        "Cannot snapshot live credentials before activation"
                    )
                if current_identity is None:
                    # Fresh machine: normalize "" so the stash, composer, and
                    # rollback all see "nothing to preserve".
                    rollback_creds = rollback_creds or None
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
                # surviving copy — stash it first. For an unmanaged or
                # config-orphaned live login the stash is the only copy
                # anywhere; for --force it guards against the "stale" live
                # login actually being the fresher generation. A failed stash
                # aborts, except under --force where the user explicitly
                # asked for the overwrite.
                if rollback_creds and rollback_creds != target_creds:
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
                    self._write_credentials(
                        self._prepare_credentials_for_activation(
                            target_creds, rollback_creds
                        )
                    )
                    creds_written = True

                    # Mirror the normal switch path: preserve existing local
                    # settings/projects when ~/.claude.json already exists, only
                    # swapping in oauthAccount. Fall back to the full imported
                    # config when no usable local config exists.
                    # `_read_json` answers None for ABSENT and for TORN alike,
                    # so a torn ~/.claude.json fell to the else branch and the
                    # 1-key backup config was written over the user's whole
                    # file — measured through the public `switch_to`:
                    # `switched: True` returned with `projects`, `mcpServers`
                    # and `userID` gone.
                    #
                    # Back it up before replacing it, rather than refusing.
                    # Upstream REPLACES a malformed config here on purpose
                    # (`test_clean_switch_fallback_when_local_config_malformed`
                    # — a machine being seeded by import, where the leftover
                    # file is noise), and nothing in scope separates that from
                    # a working install whose config just tore: measured, both
                    # reach this line with `current_account` set and
                    # `_get_current_account()` None. So keep upstream's
                    # behaviour and stop it being LOSSY: the bytes survive next
                    # to the config, named, and the switch still lands.
                    existing_config = (
                        self._read_json(config_path) if config_path.exists() else None
                    )
                    if existing_config is not None:
                        # `is not None`, not truthiness. A VALID but empty `{}`
                        # is readable and loses nothing by being spliced; the
                        # falsy form sent it down the salvage branch and told
                        # the user it "could not be parsed", which is the same
                        # ""-vs-None conflation this branch exists to separate.
                        existing_config["oauthAccount"] = target_oauth
                        self._write_json(config_path, existing_config)
                    else:
                        if config_path.exists():
                            salvage = self._salvage_unreadable(
                                config_path, emit_output, warnings_out
                            )
                            del salvage
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
                if kind in ("foreign", "alien", "known-foreign"):
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
                    elif kind == "known-foreign":
                        msg = (
                            "The live credential was previously identified "
                            "as another account's. It was preserved and not "
                            f"written into Account-{current_account}. If the "
                            "owning account later cannot authenticate, log "
                            "in as it and run: cswap add"
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
                elif kind == "wiped":
                    # Claude Code emptied the live token fields in place
                    # (its invalid_grant reaction). The blob carries nothing
                    # to preserve and writing it would replace the slot's
                    # only surviving refresh token with empty strings — the
                    # exact destruction chain observed in the field. Config
                    # backup only; the slot's credential backup is the
                    # recovery path.
                    self._write_account_config(
                        current_account, current_email, original_config
                    )
                    msg = (
                        "The live credential's tokens were wiped (Claude "
                        "Code clears them when a refresh is rejected). "
                        f"Account-{current_account}'s stored backup was "
                        "kept. If the account cannot authenticate after "
                        "switching back, log in with Claude Code and run: "
                        "cswap add"
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
                    # these bytes. Refresh only the config backup. (Rare since
                    # #145: activation composes live shared MCP state into the
                    # written credential, so live bytes match the slot's only
                    # when nothing was composed in.)
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
                target_creds = self._read_target_credentials(
                    target_account, target_email
                )
                target_config = self._read_account_config(target_account, target_email)

                if not target_config:
                    raise SwitchError(
                        f"Account-{target_account} has no stored config backup. "
                        f"Re-add with: cswap --add-account --slot {target_account}"
                    )

                # Step 3: Activate target account - credentials
                self._write_credentials(
                    self._prepare_credentials_for_activation(
                        target_creds, original_creds
                    )
                )
                transaction.record_step("credentials_written")
                self._logger.info("Wrote target credentials")

                # Step 4: Update config with target oauthAccount
                target_config_data = json.loads(target_config)
                oauth_section = target_config_data.get("oauthAccount")

                if not oauth_section:
                    raise SwitchError("Invalid oauthAccount in backup")

                # `is not None`, not truthiness — same conflation the direct-
                # activation branch above (:6148-6165) already guards
                # against. A torn ~/.claude.json reads as None here too;
                # `current_config_data["oauthAccount"] = ...` on that None
                # raised `'NoneType' object does not support item
                # assignment` with no salvage copy, losing the user's torn
                # config for good. Absent/unreadable both fall to the same
                # salvage-then-replace the direct-activation branch uses.
                current_config_data = self._read_json(config_path)
                if current_config_data is not None:
                    current_config_data["oauthAccount"] = oauth_section
                    self._write_json(config_path, current_config_data)
                else:
                    if config_path.exists():
                        self._salvage_unreadable(
                            config_path, emit_output, warnings_out
                        )
                    self._write_json(config_path, target_config_data)
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
        self._refuse_session_shell()
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
        from claude_swap.session import scan_live_sessions

        live = {}
        unreadable = {}
        for d in session_dirs:
            sessions, bad = scan_live_sessions(d)
            if sessions:
                live[d.name] = [s.pid for s in sessions]
            elif bad:
                unreadable[d.name] = bad
        if live:
            details = "; ".join(
                f"{name} (PID {', '.join(map(str, pids))})"
                for name, pids in live.items()
            )
            raise SessionError(
                f"Live session-mode Claude instance(s) found: {details}. "
                "Exit them first, then retry --purge."
            )
        if unreadable:
            details = "; ".join(
                f"{name} ({n} record(s))" for name, n in unreadable.items()
            )
            raise SessionError(
                f"Session records that could not be read: {details}. Whether a "
                "Claude instance is live cannot be determined, and purging "
                "would pull a live profile out from under it. Repair or remove "
                "them, then retry --purge."
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
