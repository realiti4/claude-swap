"""Session mode: run Claude Code as a stored account in one terminal.

``cswap run NUM|EMAIL`` launches Claude Code with ``CLAUDE_CONFIG_DIR``
pointing at a persistent per-account profile under
``<backup_dir>/sessions/<num>-<email-slug>/``, leaving the default
``~/.claude/`` login (and every other terminal, plus the VS Code extension)
untouched. ``CLAUDE_CONFIG_DIR`` fully isolates Claude Code's config and
credential lookup; on macOS, Claude hashes the (NFC-normalized) env var value
into its keychain service name, so each profile gets its own keychain entry.

Profiles are seeded with a plaintext ``.credentials.json`` — deliberate,
including on macOS: the plaintext fallback is Claude's only credential
mechanism on Linux (a stable contract), and Claude migrates it into its
hashed keychain entry on first write. Writing that keychain entry ourselves
would couple us to Claude's internal storage format and naming, where a
mismatch is a hard "logged out" failure instead of a harmless stale entry.

Sharing: by default the user's ``settings.json``, ``keybindings.json``,
``CLAUDE.md``, ``skills/``, ``commands/``, and ``agents/`` follow them into
the session profile — symlinks on macOS/Linux (Claude's settings writer
detects symlinks and writes through to the target, so in-session ``/config``
changes land in ``~/.claude``), copies re-synced on every launch on Windows.
A manifest records what cswap created so removal never touches user data.

History sharing (``--share-history``, opt-in): additionally links
``projects/`` (conversation transcripts — what ``claude --resume`` lists) and
``history.jsonl`` (prompt history) from ``~/.claude``, so all accounts see one
unified conversation history. On POSIX these are symlinks; on Windows — where
a re-synced copy would *fork* history on first write — ``projects/`` is shared
with a directory junction (no privilege required) and ``history.jsonl`` with a
file symlink (needs Developer Mode/admin; if that is unavailable it is skipped
with a warning while ``projects/`` still shares). Before the first Windows link
a one-time safety copy of the real history is taken under the backup dir. If
the profile already accumulated its own history, it is merged into ``~/.claude``
first so nothing disappears from ``--resume``.

Junctions are reparse points that ``os.path.islink`` does not report, so every
place that must distinguish a cswap-created link from real user data uses
:func:`_is_reparse_link`, and profile/backup deletion uses :func:`safe_rmtree`
to avoid ever recursing through a junction into the shared ``~/.claude``.

This module must not import ``switcher`` (switcher imports us for the
session-aware guards); it receives a ``ClaudeAccountSwitcher`` instance.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from claude_swap import macos_keychain
from claude_swap.claude_locks import proper_lockfile
from claude_swap.exceptions import ClaudeCodeLockTimeout, SessionError
from claude_swap.macos_keychain import KeychainError
from claude_swap.locking import FileLock
from claude_swap.models import Platform
from claude_swap.oauth import refresh_oauth_credentials
from claude_swap.paths import get_default_global_config_path
from claude_swap.printer import accent, dimmed, muted, warning
from claude_swap.process_detection import ClaudeSession, list_sessions
from claude_swap.settings import atomic_write_json

if TYPE_CHECKING:
    from claude_swap.switcher import ClaudeAccountSwitcher

# Items mirrored from ~/.claude into session profiles when sharing is on.
# Deliberately excludes anything account- or instance-scoped: plugins/,
# sessions/, ide/, .claude.json, .credentials.json, statsig/ and other
# telemetry. projects/ and history.jsonl are per-account by default and move
# to HISTORY_ITEMS sharing only with the opt-in --share-history flag.
# .claude.json stays excluded as a file, but its one user-scoped key —
# top-level mcpServers — is mirrored separately by _sync_mcp_servers.
SHARED_ITEMS = (
    "settings.json",
    "keybindings.json",
    "CLAUDE.md",
    "skills",
    "commands",
    "agents",
)

# Conversation-history items shared additionally under --share-history.
# Always linked, never copied, so history is genuinely shared rather than
# forked: POSIX symlinks, or on Windows a junction (projects/) + a file
# symlink (history.jsonl). See _desired_link_kind.
HISTORY_ITEMS = (
    "projects",
    "history.jsonl",
)

# Records which entries in a session profile cswap created (so --no-share and
# re-syncs only ever remove cswap-managed links/copies, never user data).
SHARE_MANIFEST = ".cswap-shared.json"

# Deferred-invalidation marker: backup credentials changed while a session was
# live (we never pull credentials out from under a running claude), so the
# profile must be re-bootstrapped on the next non-live `cswap run` even if it
# still passes the local reuse check.
STALE_MARKER = ".cswap-stale-credentials"

# The user-scope MCP key mirrored from the default profile's .claude.json.
MCP_KEY = "mcpServers"

# Adoption marker: this profile's mcpServers is (or was) cswap-mirrored. Gates
# both the one-time migration stash and --no-share's removal of the key, so
# pre-feature session-local definitions are never silently destroyed.
MCP_MIRROR_MARKER = ".cswap-mcp-mirror-v1"

# One-time migration stash: session-local MCP definitions displaced by the
# first mirror land here (write-once) instead of vanishing.
MCP_DISPLACED_STASH = ".cswap-mcp-displaced.json"

# One-time safety snapshot of the real ~/.claude history, taken under the
# backup dir before the first Windows share link, plus the marker recording it
# so the (potentially large) copy is never repeated.
HISTORY_BACKUP_DIRNAME = "history-backups"
HISTORY_BACKUP_MARKER = ".cswap-history-backup.json"

# Reparse tags meaning "this path redirects elsewhere": a Windows symlink or a
# junction (mount point). Empty off Windows. os.path.islink() reports symlinks
# but NOT junctions, so any junction-aware check must consult the reparse tag.
_REPARSE_TAGS = frozenset(
    tag
    for tag in (
        getattr(stat, "IO_REPARSE_TAG_SYMLINK", None),
        getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", None),
    )
    if tag is not None
)


def _fs_timestamp() -> str:
    """UTC timestamp safe for filenames (no ``:`` — illegal on Windows)."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _is_reparse_link(path: Path) -> bool:
    """True for a POSIX/Windows symlink OR a Windows junction.

    The sharing code must tell a cswap-created link from real user data
    everywhere it might delete or merge. ``Path.is_symlink()`` alone misses
    junctions (reparse points, not symlinks), which would let ``rmtree`` or the
    history merge traverse into the shared ``~/.claude`` target.
    """
    try:
        if path.is_symlink():
            return True
    except OSError:
        # Don't conclude "not a link" yet: fall through to the reparse-tag
        # probe. safe_rmtree's no-recurse-through-junction guarantee rests on
        # this never yielding a false negative for a junction.
        pass
    if not _REPARSE_TAGS:
        return False  # non-Windows: a symlink is the only kind of link
    try:
        tag = getattr(os.lstat(path), "st_reparse_tag", 0)
    except OSError:
        return False
    return tag in _REPARSE_TAGS


def _remove_link(path: Path) -> None:
    """Remove a symlink or junction without following it. Best-effort.

    ``os.unlink`` handles POSIX symlinks and Windows file symlinks; a Windows
    junction is a directory reparse point that only ``os.rmdir`` removes. Try
    unlink first, fall back to rmdir — neither touches the link's target.
    """
    try:
        os.unlink(path)
        return
    except OSError:
        pass
    try:
        os.rmdir(path)
    except OSError:
        pass


def _create_junction(src: Path, dest: Path) -> None:
    """Create a Windows directory junction at *dest* pointing to *src*.

    Junctions need no privilege (unlike symlinks) and work on NTFS, so they
    back projects/ sharing. Isolated here so tests can stub it on any host; the
    ``_winapi`` import is Windows-only and deliberately lazy.
    """
    import _winapi

    _winapi.CreateJunction(str(src), str(dest))


def safe_rmtree(path: Path) -> None:
    """Recursively delete *path*, never traversing a reparse point.

    ``shutil.rmtree`` recurses into Windows junctions (they are not symlinks),
    so deleting a profile or purging the backup dir could delete the real
    ``~/.claude`` history a junction points at. This removes any reparse point
    as a link and only recurses into genuine directories. Best-effort.
    """
    if _is_reparse_link(path):
        _remove_link(path)
        return
    if not path.is_dir():
        try:
            path.unlink()
        except OSError:
            pass
        return
    try:
        for child in path.iterdir():
            if _is_reparse_link(child):
                _remove_link(child)
            elif child.is_dir():
                safe_rmtree(child)
            else:
                try:
                    child.unlink()
                except OSError:
                    pass
        path.rmdir()
    except OSError:
        pass


def mark_session_stale(session_dir: Path) -> None:
    """Flag a live session profile for re-bootstrap once it exits."""
    try:
        (session_dir / STALE_MARKER).touch()
    except OSError:
        pass  # best-effort; worst case the old reuse behavior applies

# Env vars that make claude bypass account OAuth entirely (verified against
# claude 2.1.175). Dropped from the auth-status probe (they'd fake "logged in"
# for the wrong reason) AND scrubbed from the session launch env with a
# warning: `cswap run N` is an explicit request for account N, so letting an
# exported API key silently hijack the session would defeat the command. The
# same-account fast path (plain claude, untouched env) does not scrub.
AUTH_OVERRIDE_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
    "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR",
)

# `claude auth status` is a local check (no API call) but spawns the full CLI.
_AUTH_STATUS_TIMEOUT = 10.0

# Bootstrap holds the backup-dir lock across one token refresh (10s network
# timeout) plus auth-status probes, so it needs more headroom than the
# default 10s acquire used by the switch paths.
_BOOTSTRAP_LOCK_TIMEOUT = 30.0


def slugify_email(email: str) -> str:
    """Filesystem-safe slug for an email address.

    Uniqueness comes from the ``<num>-`` slot prefix on the session dir, so
    this only needs to be safe (incl. Windows-forbidden chars), not injective.
    """
    normalized = unicodedata.normalize("NFC", email)
    return "".join(
        ch if (ch.isascii() and (ch.isalnum() or ch in "._-")) else "_"
        for ch in normalized
    )


def session_dir_for(backup_dir: Path, account_num: str, email: str) -> Path:
    """Session profile directory for an account.

    Note: the profile itself contains Claude's own ``sessions/<pid>.json``
    PID files, so full paths look like
    ``<backup>/sessions/2-user_x.com/sessions/1234.json`` — intentional.
    """
    return backup_dir / "sessions" / f"{account_num}-{slugify_email(email)}"


def keychain_service_name(session_dir: Path) -> str:
    """Keychain service name Claude Code derives for this config dir.

    Claude hashes the raw ``CLAUDE_CONFIG_DIR`` env var value, NFC-normalized
    and unresolved (claude src ``envUtils.ts``/``macOsKeychainHelpers.ts``).
    Hash exactly the string we export — never a resolved/realpath variant.
    """
    normalized = unicodedata.normalize("NFC", str(session_dir))
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8]
    return f"Claude Code-credentials-{digest}"


def _keychain_account_name() -> str:
    """Keychain account name, mirroring Claude's ``getUsername()``.

    Delegates to :func:`macos_keychain.keychain_account_name` so session profiles
    and the active store derive the account name identically.
    """
    return macos_keychain.keychain_account_name()


def delete_macos_keychain_entry(session_dir: Path) -> None:
    """Best-effort delete of a session profile's hashed keychain entry.

    No-op off macOS. Needed before seeding (Claude reads the keychain before
    the plaintext file, so a stale entry would shadow a fresh seed) and on
    profile removal (once the dir is gone the hashed name is unrecoverable).
    """
    if Platform.detect() != Platform.MACOS:
        return
    try:
        macos_keychain.delete_password(
            keychain_service_name(session_dir), _keychain_account_name()
        )
    except KeychainError:
        pass  # best-effort; absent entry is already success (rc 44)


def read_session_credentials(session_dir: Path) -> str | None:
    """Best-effort read of a session profile's *current* credential JSON.

    Once a session has run, the profile — not the backup store — holds the
    newest generation of the account's token family: claude rotates tokens
    in place, and nothing syncs them back to backup. On macOS the rotated
    credential lives in the profile's hashed keychain entry (which shadows
    the plaintext seed from the moment claude first writes it), elsewhere in
    the profile's ``.credentials.json``. Read-only by design: writing either
    location stays claude's job (see the module docstring on why cswap never
    writes the hashed entry). Returns ``None`` when the profile has no
    readable credential material.
    """
    if not session_dir.is_dir():
        return None
    if Platform.detect() == Platform.MACOS:
        try:
            creds = macos_keychain.get_password(
                keychain_service_name(session_dir), _keychain_account_name()
            )
            if creds:
                return creds
        except KeychainError:
            pass  # locked/denied/timeout — the plaintext seed is the next-best truth
    try:
        return (session_dir / ".credentials.json").read_text(encoding="utf-8")
    except (OSError, ValueError):
        # ValueError covers UnicodeDecodeError: a byte-corrupt file is "no
        # readable credential material", not an error to propagate.
        return None


def read_session_identity(session_dir: Path) -> tuple[str, str] | None:
    """Best-effort read of the account identity a session profile is logged in as.

    Claude records the logged-in account in the profile's ``.claude.json``
    ``oauthAccount`` and rewrites it on every (re-)login, so this reflects the
    profile's *current* identity — which an in-session ``/login`` can re-point
    at a different account than the slot the profile was created for. Returns
    ``(email, organization_uuid)`` with ``""`` for a missing org, or ``None``
    when no identity is readable (missing dir/file/field).
    """
    try:
        text = (session_dir / ".claude.json").read_text(encoding="utf-8")
        config = json.loads(text)
    except (OSError, ValueError):
        # ValueError covers JSONDecodeError and UnicodeDecodeError alike: a
        # byte-corrupt file is an unreadable identity, and the usage-fetch
        # path this feeds must never raise.
        return None
    if not isinstance(config, dict):
        return None
    oauth_account = config.get("oauthAccount") or {}
    if not isinstance(oauth_account, dict):
        return None
    email = oauth_account.get("emailAddress") or ""
    if not email:
        return None
    return email, oauth_account.get("organizationUuid") or ""


def session_identity_drifted(session_dir: Path, email: str, org_uuid: str) -> bool:
    """Whether the profile is logged in as a *different* account than its slot.

    An in-session ``/login`` (e.g. after the slot's account hit its rate limit
    mid-session) re-points the profile's credential at another account while
    the profile directory keeps claiming the original slot. Comparison mirrors
    ``_is_session_valid``: the email must match, the org only when both sides
    have a value. An unreadable identity is NOT drift — missing metadata
    degrades to trusting the profile (its token family is normally the slot's
    freshest) rather than abandoning it over a broken ``.claude.json``.
    """
    identity = read_session_identity(session_dir)
    if identity is None:
        return False
    profile_email, profile_org = identity
    if profile_email != email:
        return True
    return bool(profile_org and org_uuid and profile_org != org_uuid)


def live_sessions_for(session_dir: Path) -> list[ClaudeSession]:
    """Live Claude instances running against a session profile."""
    if not session_dir.exists():
        return []
    return list_sessions(claude_dir=session_dir)


def _mkdir_private(path: Path) -> None:
    """mkdir -p with 0o700 on every created level.

    ``Path.mkdir(parents=True, mode=...)`` applies the mode only to the leaf;
    history dirs must match Claude Code's own 0o700 at every level.
    """
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)


def _probe_env(session_dir: Path) -> dict[str, str]:
    """Env for the auth-status probe: session config dir, auth overrides dropped."""
    env = {k: v for k, v in os.environ.items() if k not in AUTH_OVERRIDE_ENV_VARS}
    env["CLAUDE_CONFIG_DIR"] = str(session_dir)
    return env


class SessionManager:
    """Bootstraps per-account session profiles and launches Claude into them."""

    def __init__(self, switcher: ClaudeAccountSwitcher):
        self.switcher = switcher
        self.sessions_dir = switcher.backup_dir / "sessions"
        self._logger = switcher._logger

    # -- launch ----------------------------------------------------------

    def run(
        self,
        identifier: str,
        claude_args: list[str],
        share: bool = True,
        share_history: bool = False,
    ) -> NoReturn:
        """Launch Claude Code as the given account in the current terminal."""
        claude_bin = shutil.which("claude")
        if not claude_bin:
            raise SessionError(
                "'claude' was not found on PATH. Install Claude Code first."
            )

        account_num, email, org_uuid = self.switcher.resolve_account(identifier)
        # Guard before the same-account direct-launch fast path below (which
        # _exec's claude and never returns) — and before setup_session.
        self._ensure_not_api_key(account_num, email)

        config_dir_preset = os.environ.get("CLAUDE_CONFIG_DIR")
        if config_dir_preset:
            # With CLAUDE_CONFIG_DIR set, "current default account" is
            # meaningless (we may already be inside a session terminal), so
            # the same-account fast path below must not trigger.
            warning(
                f"CLAUDE_CONFIG_DIR is already set ({config_dir_preset}); "
                "overriding it for this launch."
            )
        else:
            # Same-account fast path: never create a second credential copy
            # for the account that is already the active default login —
            # two copies of one account can drift if the server rotates the
            # refresh token.
            current = self.switcher._get_current_account()
            if current is not None and current == (email, org_uuid):
                print(
                    dimmed(
                        f"Account-{account_num} ({email}) is already the active "
                        "default login — launching claude directly."
                    )
                )
                self._exec(claude_bin, claude_args, env=dict(os.environ))

        scrubbed = [v for v in AUTH_OVERRIDE_ENV_VARS if os.environ.get(v)]
        if scrubbed:
            warning(
                f"Ignoring {', '.join(scrubbed)} for this session — it would "
                f"override the selected account inside Claude Code."
            )

        session_dir, account_num, email = self.setup_session(
            identifier, share, share_history
        )

        print(
            f"{accent('Launching')} Account-{account_num} ({email}) "
            f"{muted('[session mode]')}"
        )
        env = {
            k: v for k, v in os.environ.items() if k not in AUTH_OVERRIDE_ENV_VARS
        }
        env["CLAUDE_CONFIG_DIR"] = str(session_dir)
        self._exec(claude_bin, claude_args, env=env)

    def exec_default(self, claude_args: list[str]) -> NoReturn:
        """Launch plain Claude Code with the current default login.

        Used by `cswap run` (no account) when the cwd has no mapping, or its
        mapped account no longer exists. Equivalent to typing `claude`
        directly: the unmodified environment is passed through (no session
        profile, no auth-override scrubbing), so whatever the default login
        resolves to is what runs.
        """
        claude_bin = shutil.which("claude")
        if not claude_bin:
            raise SessionError(
                "'claude' was not found on PATH. Install Claude Code first."
            )
        self._exec(claude_bin, claude_args, env=dict(os.environ))

    def _exec(self, claude_bin: str, claude_args: list[str], env: dict[str, str]) -> NoReturn:
        """Hand the terminal over to claude. Never returns.

        POSIX: ``execvpe`` replaces the cswap process entirely (the lock is
        already released — an exec'd claude must never inherit a held flock).
        Windows: ``os.exec*`` detaches from the console confusingly, so stay
        resident as a thin wrapper and mirror claude's exit code.
        """
        argv = [claude_bin, *claude_args]
        if sys.platform == "win32":
            try:
                rc = subprocess.run(argv, env=env).returncode
            except KeyboardInterrupt:
                rc = 130  # Ctrl+C went to claude; just mirror the exit
            sys.exit(rc)
        os.execvpe(claude_bin, argv, env)
        raise AssertionError("unreachable")  # pragma: no cover

    def _ensure_not_api_key(self, account_num: str, email: str) -> None:
        """Reject API-key accounts in session mode (not supported yet).

        Session bootstrap is OAuth-shaped — it seeds ``.credentials.json`` and
        ``_is_session_valid`` requires ``authMethod == "claude.ai"`` — so an API-key
        account would otherwise fail validation opaquely. Raise early with guidance.
        """
        if self.switcher._account_kind(account_num) == "api_key":
            raise SessionError(
                f"Account-{account_num} ({email}) is an API-key account; "
                "'cswap run' (session mode) does not support API-key accounts yet. "
                "Use 'cswap --switch-to' to make it your default login instead."
            )

    # -- bootstrap -------------------------------------------------------

    def setup_session(
        self, identifier: str, share: bool, share_history: bool = False
    ) -> tuple[Path, str, str]:
        """Ensure a valid session profile exists; returns (dir, num, email)."""
        account_num, email, org_uuid = self.switcher.resolve_account(identifier)
        # Defense-in-depth: also guard here (run() guards before its fast path).
        self._ensure_not_api_key(account_num, email)
        session_dir = session_dir_for(self.switcher.backup_dir, account_num, email)

        # Deferred invalidation: backup credentials changed while this profile
        # was live, so its credentials are presumed stale even if they still
        # pass the local reuse check. Honored only when no session is live —
        # a second `cswap run` joining a live session must not invalidate
        # under the running claude (the marker survives for later).
        stale = (session_dir / STALE_MARKER).exists() and not live_sessions_for(
            session_dir
        )

        # Cheap reuse check without the lock: most launches hit this.
        if not stale and self._is_session_valid(session_dir, email, org_uuid):
            self._sync_sharing(session_dir, share, share_history)
            return session_dir, account_num, email

        with FileLock(self.switcher.lock_file, timeout=_BOOTSTRAP_LOCK_TIMEOUT):
            # Re-evaluate the marker under the lock, then re-check validity:
            # another `cswap run` may have bootstrapped while we waited.
            if (session_dir / STALE_MARKER).exists() and not live_sessions_for(
                session_dir
            ):
                self.switcher._invalidate_session_credentials(account_num, email)
                (session_dir / STALE_MARKER).unlink(missing_ok=True)
            if self._is_session_valid(session_dir, email, org_uuid):
                self._sync_sharing(session_dir, share, share_history)
                return session_dir, account_num, email

            self._bootstrap(session_dir, account_num, email, org_uuid)
            self._sync_sharing(session_dir, share, share_history)

            if not self._is_session_valid(session_dir, email, org_uuid):
                self._cleanup_failed_session(session_dir)
                raise SessionError(
                    f"Session profile for Account-{account_num} ({email}) failed "
                    f"validation. Log in with that account and re-add it: "
                    f"cswap --add-account --slot {account_num}"
                )
        # Lock released here, before any exec.

        return session_dir, account_num, email

    def _bootstrap(
        self, session_dir: Path, account_num: str, email: str, org_uuid: str
    ) -> None:
        """Seed the session profile from backup storage. Caller holds the lock."""
        # Claude reads the keychain before the plaintext file — a stale hashed
        # entry from an earlier profile at this path would shadow the seed.
        delete_macos_keychain_entry(session_dir)

        creds = self.switcher.read_account_credentials(account_num, email)
        if not creds:
            raise SessionError(
                f"Account-{account_num} has no stored credentials. "
                f"Re-add with: cswap --add-account --slot {account_num}"
            )

        # One refresh so the profile starts with a fresh access token; persist
        # a possibly-rotated refresh token back to backup so future switches
        # and runs see the latest. Failure is non-fatal: the stored token may
        # still be valid, and claude refreshes on its own at runtime.
        # Setup-token accounts (--add-token) have no refresh token by design —
        # skip silently instead of warning about a flow that can't happen.
        if self._has_refresh_token(creds):
            refreshed = refresh_oauth_credentials(creds)
            if refreshed:
                creds = refreshed
                self.switcher.write_account_credentials(account_num, email, creds)
            else:
                warning(
                    f"Could not refresh the token for Account-{account_num}; "
                    "continuing with the stored credentials."
                )

        config_text = self.switcher.read_account_config(account_num, email)
        try:
            config_data = json.loads(config_text) if config_text else {}
        except json.JSONDecodeError:
            config_data = {}
        oauth_account = config_data.get("oauthAccount")
        if not oauth_account:
            raise SessionError(
                f"Account-{account_num} has no stored config backup. "
                f"Re-add with: cswap --add-account --slot {account_num}"
            )

        session_dir.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            os.chmod(session_dir, 0o700)

        creds_path = session_dir / ".credentials.json"
        creds_path.write_text(creds, encoding="utf-8")
        if sys.platform != "win32":
            os.chmod(creds_path, 0o600)

        # Merge the identity seed into any existing .claude.json so a
        # re-bootstrap preserves the profile's own projects/history. The
        # `theme` key is load-bearing: claude shows onboarding when
        # `!config.theme || !config.hasCompletedOnboarding`.
        config_path = session_dir / ".claude.json"
        existing: dict = {}
        if config_path.exists():
            try:
                existing = json.loads(config_path.read_text(encoding="utf-8")) or {}
            except (json.JSONDecodeError, OSError):
                existing = {}
        existing["oauthAccount"] = oauth_account
        existing["hasCompletedOnboarding"] = True
        existing.setdefault("theme", config_data.get("theme") or "dark")
        config_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        if sys.platform != "win32":
            os.chmod(config_path, 0o600)

        self._logger.info(
            f"Bootstrapped session profile for account {account_num} at {session_dir}"
        )

    @staticmethod
    def _has_refresh_token(creds: str) -> bool:
        try:
            return bool(json.loads(creds).get("claudeAiOauth", {}).get("refreshToken"))
        except (json.JSONDecodeError, AttributeError):
            return True  # unknown shape — let the refresh attempt decide

    def _cleanup_failed_session(self, session_dir: Path) -> None:
        # Keychain first: claude may have partially migrated the seed, and the
        # hashed service name can't be recomputed once the dir is gone.
        delete_macos_keychain_entry(session_dir)
        # safe_rmtree, never shutil.rmtree: a share step may already have laid
        # down a projects/ junction, and a plain rmtree would recurse through
        # it and delete the real ~/.claude history.
        safe_rmtree(session_dir)

    # -- validation ------------------------------------------------------

    def _is_session_valid(self, session_dir: Path, email: str, org_uuid: str) -> bool:
        """Whether claude sees the profile as logged in with the right identity.

        Local check only (`claude auth status` makes no API call): a revoked
        but unexpired token still passes and fails on first real use.
        """
        if not session_dir.is_dir():
            return False
        # On Windows `claude` is a `.cmd` shim, and a bare "claude" passed to
        # subprocess won't resolve it (PATHEXT isn't applied) — it raises
        # FileNotFoundError, which the handler below turns into a false
        # "failed validation". shutil.which finds the shim.
        claude_bin = shutil.which("claude") or "claude"
        try:
            result = subprocess.run(
                [claude_bin, "auth", "status", "--json"],
                env=_probe_env(session_dir),
                capture_output=True,
                text=True,
                timeout=_AUTH_STATUS_TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if result.returncode != 0:
            return False
        try:
            status = json.loads(result.stdout)
        except json.JSONDecodeError:
            return False
        if status.get("loggedIn") is not True:
            return False
        # Verified against claude 2.1.175; an env API key reports a different
        # method, and the probe env already drops those vars anyway.
        if status.get("authMethod") != "claude.ai":
            return False
        if status.get("email") != email:
            return False
        # Lenient org check: only when both sides have a value, so schema
        # drift degrades to email-only validation instead of false negatives.
        status_org = status.get("orgId")
        if status_org and org_uuid and status_org != org_uuid:
            return False
        return True

    # -- sharing ---------------------------------------------------------

    def _sync_sharing(
        self, session_dir: Path, share: bool, share_history: bool = False
    ) -> None:
        """Mirror shared items from ~/.claude into the profile (or undo it).

        ``share`` governs SHARED_ITEMS (customizations) and the mcpServers
        mirror (see ``_sync_mcp_servers`` — its --no-share removal is gated
        on the adoption marker); ``share_history`` governs HISTORY_ITEMS
        (conversation history) — independent concerns, so ``--no-share
        --share-history`` gives a bare profile with unified history.
        Idempotent; runs on every launch. Deliberately sources from the
        default ``~/.claude`` (not ``get_claude_config_home()``): sharing
        always mirrors the default profile, even when ``CLAUDE_CONFIG_DIR``
        is set in the invoking environment. File/dir sharing is lock-free on
        the reuse path — concurrent runs with different flags are last-writer-
        wins and self-heal on the next launch; only the MCP mirror takes
        Claude's config lock, and only when it needs to write.
        """
        if not session_dir.is_dir():
            return
        self._sync_mcp_servers(session_dir, share)

        source_root = Path.home() / ".claude"

        # History sharing works on every platform now: POSIX via symlinks,
        # Windows via a junction (projects/) + file symlink (history.jsonl).
        # Before the first Windows link, take a one-time safety snapshot of
        # the real history; if that can't be made, decline history sharing for
        # this launch rather than risk it.
        if share_history and not self._ensure_history_backup(source_root):
            share_history = False

        active_items = (SHARED_ITEMS if share else ()) + (
            HISTORY_ITEMS if share_history else ()
        )
        manifest_path = session_dir / SHARE_MANIFEST
        managed = self._read_manifest(manifest_path)

        # A flag turned off since last launch: remove the links/copies we
        # created for it (never plain files/dirs the user accumulated). For
        # history items that holds even when the manifest claims them: a stale
        # manifest (lock-free launches race) must never delete real history —
        # only ever remove a link we own (symlink OR junction).
        for name in managed:
            if name not in active_items:
                dest = session_dir / name
                if (
                    name in HISTORY_ITEMS
                    and dest.exists()
                    and not _is_reparse_link(dest)
                ):
                    continue
                self._remove_managed(dest)
        if not active_items:
            manifest_path.unlink(missing_ok=True)
            return

        new_managed: list[str] = []

        for name in active_items:
            src = source_root / name
            dest = session_dir / name

            if name in HISTORY_ITEMS:
                # Gate the destructive merge behind link feasibility: never move
                # the profile's own history into ~/.claude if the link that
                # would replace it can't be created (default Windows: no file-
                # symlink privilege). Otherwise the account loses sight of its
                # own history to a global file it isn't linked to.
                if not self._history_link_feasible(name, src, dest, session_dir):
                    continue
                if not self._prepare_history_share(src, dest, session_dir):
                    continue

            if not src.exists():
                # Source vanished (or never existed): prune our own entry.
                if name in managed:
                    self._remove_managed(dest)
                continue

            existing_link = _is_reparse_link(dest)
            if existing_link and name not in managed:
                managed = [*managed, name]  # adopt: only cswap links here
            elif not existing_link and dest.exists() and name not in managed:
                # Pre-existing user data in the profile — never touch it.
                print(
                    dimmed(
                        f"Not sharing {name}: the session profile already has "
                        "its own copy."
                    )
                )
                continue

            try:
                self._materialize_share(name, src, dest)
            except OSError as e:
                if name == "history.jsonl" and (
                    self.switcher.platform == Platform.WINDOWS
                ):
                    # Backstop: _history_link_feasible already gated the common
                    # no-privilege case, so reaching here means the link failed
                    # after the probe said it would succeed (a race). projects/
                    # still shares; never fail the launch over history.jsonl.
                    warning(
                        "Not sharing history.jsonl: the file symlink could not "
                        "be created (see log). Your conversations (projects/) "
                        "are still shared; your prompt history is preserved in "
                        "~/.claude/history.jsonl and the safety backup."
                    )
                else:
                    self._logger.warning(
                        f"Failed to share {name} into session: {e}"
                    )
                continue
            new_managed.append(name)

        # Anything we managed before but no longer created gets removed above;
        # write the manifest atomically so a concurrent reader never sees a
        # truncated file.
        self._write_manifest(manifest_path, new_managed)

    def _desired_link_kind(self, name: str) -> str:
        """How a shared item is materialized on this platform.

        POSIX shares everything by symlink. Windows re-syncs customizations as
        copies (safe — the default profile is the source of truth) but *links*
        history so it is genuinely shared: a directory junction for
        ``projects/`` (no privilege) and a file symlink for ``history.jsonl``.
        """
        if self.switcher.platform != Platform.WINDOWS:
            return "symlink"
        if name in HISTORY_ITEMS:
            return "junction" if name == "projects" else "file_symlink"
        return "copy"

    def _materialize_share(self, name: str, src: Path, dest: Path) -> None:
        """Create/refresh ``dest`` as the platform-appropriate share of ``src``.

        Idempotent: an already-correct link is left in place; a copy is
        re-synced; a link pointing elsewhere (or a leftover from a
        cross-platform profile move) is replaced. Raises ``OSError`` on a
        failed filesystem operation for the caller to handle.
        """
        kind = self._desired_link_kind(name)
        if kind == "copy":
            if dest.exists() or _is_reparse_link(dest):
                self._remove_managed(dest)
            if src.is_dir():
                shutil.copytree(src, dest)
            else:
                shutil.copy2(src, dest)
            return
        # Link kinds: symlink | junction | file_symlink.
        if _is_reparse_link(dest):
            if self._link_points_to(dest, src, kind):
                return  # already correct — no-op
            self._remove_managed(dest)
        elif dest.exists():
            # A managed copy left by a Windows→POSIX profile move; replace it.
            self._remove_managed(dest)
        self._create_link(src, dest, kind)

    @staticmethod
    def _link_points_to(dest: Path, src: Path, kind: str) -> bool:
        """Whether the existing link at ``dest`` already targets ``src``."""
        if kind == "symlink":
            try:
                return dest.readlink() == src
            except OSError:
                return False
        # Junctions/file symlinks: a junction's readlink can carry a \\?\
        # prefix, so compare resolved targets rather than string-matching.
        try:
            return dest.resolve() == src.resolve()
        except OSError:
            return False

    def _create_link(self, src: Path, dest: Path, kind: str) -> None:
        """Create the requested link kind at ``dest`` → ``src``."""
        if kind == "junction":
            _create_junction(src, dest)
        else:  # symlink | file_symlink
            dest.symlink_to(src)

    def _history_link_feasible(
        self, name: str, src: Path, dest: Path, session_dir: Path
    ) -> bool:
        """Whether it is safe to run the destructive history merge for ``name``.

        Only ``history.jsonl`` on Windows can be blocked: its share is a file
        symlink, which needs Developer Mode/admin. If we can neither create one
        nor already have one, return False so the caller skips the item BEFORE
        merging — the account keeps its own prompt history rather than losing
        sight of it. ``projects/`` (a junction, no privilege) and every POSIX
        case are always feasible.
        """
        if name != "history.jsonl" or self.switcher.platform != Platform.WINDOWS:
            return True
        # An existing symlink keeps working without privilege (privilege is
        # only needed to *create* one), so a re-sync of an already-shared
        # profile stays feasible.
        if _is_reparse_link(dest) and self._link_points_to(
            dest, src, "file_symlink"
        ):
            return True
        if self._can_create_file_symlink(session_dir):
            return True
        warning(
            "Not sharing history.jsonl: creating a file symlink needs Windows "
            "Developer Mode or admin. Your conversations (projects/) are still "
            "shared; this account keeps its own prompt history."
        )
        return False

    def _can_create_file_symlink(self, session_dir: Path) -> bool:
        """Probe whether this process may create a file symlink in the profile.

        Always True off Windows. On Windows, creating a throwaway symlink is the
        only reliable check for the Developer-Mode/admin privilege; it is cheap
        and the probe is cleaned up either way.
        """
        if self.switcher.platform != Platform.WINDOWS:
            return True
        probe = session_dir / ".cswap-symlink-probe"
        try:
            if _is_reparse_link(probe) or probe.exists():
                _remove_link(probe)
            probe.symlink_to("cswap-probe-target")  # dangling target is fine
            _remove_link(probe)
            return True
        except OSError:
            _remove_link(probe)  # best-effort cleanup of a partial probe
            return False

    def _ensure_history_backup(self, source_root: Path) -> bool:
        """One-time safety snapshot of real history before the first share.

        Returns True when it is safe to proceed with history sharing (backup
        done, already taken, or not needed), False when a backup was required
        but failed — the caller then declines history sharing for this launch.

        POSIX linking is non-destructive and matches long-standing behavior, so
        the gate only binds on Windows, where junctions introduce the delete/
        merge footguns the snapshot protects against. The marker makes it a
        one-time cost.
        """
        if self.switcher.platform != Platform.WINDOWS:
            return True
        marker = self.switcher.backup_dir / HISTORY_BACKUP_MARKER
        if marker.exists():
            return True
        try:
            stamp = _fs_timestamp()
            dest_root = (
                self.switcher.backup_dir / HISTORY_BACKUP_DIRNAME / stamp
            )
            backed_up: list[str] = []
            for name in HISTORY_ITEMS:
                source = source_root / name
                if not source.exists() or _is_reparse_link(source):
                    continue  # nothing real to protect
                target = dest_root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                if source.is_dir():
                    shutil.copytree(source, target)
                else:
                    shutil.copy2(source, target)
                backed_up.append(name)
            atomic_write_json(
                marker,
                {
                    "schemaVersion": 1,
                    "backedUpAt": stamp,
                    "location": str(dest_root),
                    "items": backed_up,
                },
            )
            if backed_up:
                print(
                    dimmed(
                        f"Backed up your Claude history to {dest_root} before "
                        "enabling sharing."
                    )
                )
            return True
        except OSError as e:
            self._logger.warning(f"History backup failed: {e}")
            warning(
                "Not sharing history: couldn't back up your existing history "
                "first (see log)."
            )
            return False

    def _sync_mcp_servers(self, session_dir: Path, share: bool) -> None:
        """Mirror the default profile's user-scope ``mcpServers`` (issue #139).

        Pure mirror: the default profile is the single source of truth, so
        adds, edits, and deletions all propagate, and MCP changes made inside
        a session are overwritten the next time cswap prepares the profile.
        Nothing ever flows back into the default config, and per-project
        (``projects[…].mcpServers``) entries are untouched on both sides.

        ``share=False`` removes the mirrored key — but only from profiles
        that have adopted mirroring (MCP_MIRROR_MARKER), so ``--no-share``
        can never destroy pre-feature session-local definitions. The first
        mirror on an unadopted profile stashes any definitions it would
        displace into MCP_DISPLACED_STASH (write-once) before resetting.

        Fail-open throughout: an unreadable or malformed file on either side,
        a symlinked target, or a contended lock leaves the profile untouched
        and never blocks the launch. The adopted in-sync steady state takes
        no lock and writes nothing; first adoption always goes through the
        lock so the marker can never certify a state a concurrent claude
        changed between the read and the touch.
        """
        config_path = session_dir / ".claude.json"
        marker = session_dir / MCP_MIRROR_MARKER

        if share:
            source = self._read_mcp_source()
            if source is None:
                return
        elif marker.exists():
            source = {}  # remove what we mirrored; restored on a share run
        else:
            return  # never adopted: --no-share must not touch local data

        # Type-check before reading: reading a FIFO would hang the launch,
        # and a symlinked target must never be written through or replaced.
        if not config_path.exists():
            return  # bootstrap/validation owns a missing config
        if config_path.is_symlink() or not config_path.is_file():
            self._logger.warning(
                f"Not syncing MCP servers: {config_path} is not a regular file."
            )
            return

        existing = self._load_json_object(config_path)
        if existing is None:
            return  # bootstrap/validation owns a broken config
        target = existing.get(MCP_KEY, {})
        if not isinstance(target, dict):
            self._logger.warning(
                f"Not syncing MCP servers: the profile's {MCP_KEY} is not "
                "an object."
            )
            return
        if target == source and (not share or marker.exists()):
            return

        # The same lock a claude running in this profile takes for its own
        # .claude.json writes (CLAUDE_CONFIG_DIR is the session dir), so the
        # splice below never interleaves with its config writes.
        lock_dir = config_path.parent / (config_path.name + ".lock")
        try:
            with proper_lockfile(lock_dir):
                # Re-read both sides: a writer that waited here must not
                # clobber a newer mirror with its stale pre-lock snapshot.
                if share:
                    source = self._read_mcp_source()
                    if source is None:
                        return
                if config_path.is_symlink() or not config_path.is_file():
                    return
                existing = self._load_json_object(config_path)
                if existing is None:
                    return
                target = existing.get(MCP_KEY, {})
                if not isinstance(target, dict):
                    return
                if target == source:
                    if share:
                        self._ensure_mcp_marker(marker)
                    return
                if share and not marker.exists():
                    displaced = {
                        name: value
                        for name, value in target.items()
                        if name not in source or source[name] != value
                    }
                    if displaced and not self._stash_displaced_mcp(
                        session_dir, displaced
                    ):
                        return  # never destroy the only copy
                if source:
                    existing[MCP_KEY] = source
                else:
                    # Claude itself strips default-valued keys; match it.
                    existing.pop(MCP_KEY, None)
                try:
                    atomic_write_json(config_path, existing)
                except OSError as e:
                    self._logger.warning(f"Could not sync MCP servers: {e}")
                    return
                if share:
                    # Only after a successful write: an unadopted profile
                    # whose marker fails to land simply retries next launch,
                    # by then already in sync so nothing can be mis-stashed.
                    self._ensure_mcp_marker(marker)
        except (ClaudeCodeLockTimeout, OSError) as e:
            # OSError covers lock-machinery failures (mkdir on a read-only
            # or full filesystem); everything inside handles its own.
            self._logger.warning(
                f"Could not sync MCP servers ({e}) — skipping this launch."
            )

    @staticmethod
    def _read_mcp_source() -> dict | None:
        """The default profile's user-scope mcpServers, or None if unusable.

        ``{}`` and ``None`` are distinct: a readable config without the key
        genuinely has no user servers (``{}`` propagates the removal), while
        a missing/corrupt config or a non-dict key returns ``None`` so the
        caller leaves the profile untouched. Reads the default-home path
        (ignoring CLAUDE_CONFIG_DIR — a nested `cswap run` must not source
        from another session); no lock needed, claude's writes are atomic.
        """
        config = SessionManager._load_json_object(get_default_global_config_path())
        if config is None:
            return None
        value = config.get(MCP_KEY, {})
        return value if isinstance(value, dict) else None

    @staticmethod
    def _load_json_object(path: Path) -> dict | None:
        # ValueError covers both JSONDecodeError and UnicodeDecodeError.
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _ensure_mcp_marker(self, marker: Path) -> None:
        if marker.exists():
            return
        try:
            marker.touch()
        except OSError as e:
            self._logger.warning(f"Could not write {marker.name}: {e}")

    def _stash_displaced_mcp(self, session_dir: Path, displaced: dict) -> bool:
        """Save definitions the first mirror would displace; False aborts it.

        Write-once: a stash left by an earlier interrupted adoption is the
        pre-feature data and must not be overwritten with mirror noise. But
        only a *valid* stash counts as a saved copy — a directory, symlink,
        or unrelated file squatting on the name must block the reset, not
        green-light it.
        """
        stash = session_dir / MCP_DISPLACED_STASH
        if stash.is_symlink() or stash.exists():
            if self._is_valid_stash(stash):
                return True
            self._logger.warning(
                f"{stash.name} exists but is not a valid stash; leaving "
                "the profile's MCP servers in place."
            )
            return False
        try:
            atomic_write_json(
                stash, {"schemaVersion": 1, MCP_KEY: displaced}
            )
        except OSError as e:
            self._logger.warning(
                f"Could not stash the profile's MCP servers ({e}); "
                "leaving them in place."
            )
            return False
        print(
            dimmed(
                "Session MCP servers now mirror your default profile; the "
                f"profile's previous definitions were saved to {stash.name}."
            )
        )
        return True

    @staticmethod
    def _is_valid_stash(stash: Path) -> bool:
        if stash.is_symlink() or not stash.is_file():
            return False
        data = SessionManager._load_json_object(stash)
        return data is not None and isinstance(data.get(MCP_KEY), dict)

    def _prepare_history_share(
        self, src: Path, dest: Path, session_dir: Path
    ) -> bool:
        """Make a history item linkable; returns False to skip it this launch.

        Handles the two ways a history item differs from a plain shared item:
        the profile may already hold real history that must survive (merged
        into ``~/.claude``, never discarded — the generic loop would just
        refuse), and the share source may not exist yet on a fresh install
        (created empty so there is something to link). Real history is merged
        even when the manifest claims the entry is managed: a stale manifest
        (lock-free launches race) must never let the generic loop delete it.
        """
        if dest.exists() and not _is_reparse_link(dest):
            # Real per-account history accumulated before the flag existed.
            # (A junction/symlink is already-shared, not real data to merge —
            # _is_reparse_link catches junctions that is_symlink would miss.)
            # Merging moves files out from under any claude still running in
            # this profile, so only migrate when the profile is quiescent.
            if live_sessions_for(session_dir):
                print(
                    dimmed(
                        f"Not sharing {dest.name} yet: another session is "
                        "using this profile — retrying on the next launch."
                    )
                )
                return False
            try:
                self._merge_history_into_source(src, dest)
            except OSError as e:
                self._logger.warning(
                    f"Could not merge {dest.name} into {src}: {e}"
                )
                print(
                    dimmed(
                        f"Not sharing {dest.name}: merging the profile's "
                        "existing history failed (see log)."
                    )
                )
                return False
            print(
                dimmed(
                    f"Merged the profile's existing {dest.name} into "
                    f"{src} — conversation history is now shared."
                )
            )
        if not src.exists():
            # Fresh ~/.claude (or first run): seed an empty share target so
            # the generic loop below has something to link.
            try:
                # 0o600/0o700 to match Claude Code's own modes for history
                # data — its mode= applies only at creation, so a loose seed
                # here would stay world-readable forever.
                if dest.name.endswith(".jsonl"):
                    src.parent.mkdir(parents=True, exist_ok=True)
                    src.touch(mode=0o600)
                else:
                    _mkdir_private(src)
            except OSError as e:
                self._logger.warning(f"Could not create {src}: {e}")
                return False
        return True

    @staticmethod
    def _merge_history_into_source(src: Path, dest: Path) -> None:
        """Move the profile's own history at ``dest`` into ``src``.

        Directories merge file-by-file (transcript filenames are UUIDs, so
        collisions mean identical sessions — first writer wins and the
        duplicate is dropped). ``history.jsonl`` merges by appending lines
        not already present. ``dest`` is removed once empty; any failure
        raises OSError and leaves remaining files in place for the next try.
        """
        if dest.is_dir():
            _mkdir_private(src)
            for path in sorted(dest.rglob("*"), reverse=True):
                rel = path.relative_to(dest)
                target = src / rel
                if path.is_dir():
                    path.rmdir()  # children already moved (reverse walk)
                    continue
                if target.exists():
                    path.unlink()
                    continue
                _mkdir_private(target.parent)
                shutil.move(str(path), str(target))
            dest.rmdir()
        else:
            existing: set[str] = set()
            if src.exists():
                existing = set(src.read_text(encoding="utf-8").splitlines())
            lines = [
                line
                for line in dest.read_text(encoding="utf-8").splitlines()
                if line and line not in existing
            ]
            if lines:
                src.parent.mkdir(parents=True, exist_ok=True)
                if not src.exists():
                    src.touch(mode=0o600)  # match Claude Code's history mode
                with src.open("a", encoding="utf-8") as f:
                    f.write("\n".join(lines) + "\n")
            dest.unlink()

    @staticmethod
    def _read_manifest(manifest_path: Path) -> list[str]:
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            items = data.get("items", [])
            # Only ever act on names we could have created.
            return [i for i in items if i in SHARED_ITEMS + HISTORY_ITEMS]
        except (OSError, json.JSONDecodeError, AttributeError):
            return []

    def _write_manifest(self, manifest_path: Path, items: list[str]) -> None:
        mode = "symlink" if self.switcher.platform != Platform.WINDOWS else "copy"
        payload = json.dumps({"items": items, "mode": mode}, indent=2)
        fd, tmp = tempfile.mkstemp(
            dir=str(manifest_path.parent), prefix=".cswap-shared-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, manifest_path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    @staticmethod
    def _remove_managed(dest: Path) -> None:
        """Remove a cswap-created share entry (link, junction, or copy), never
        the data a link points at — callers guarantee `dest` is manifest-listed
        or a reparse link."""
        try:
            if _is_reparse_link(dest):
                # Symlink or junction: drop the link itself, never its target.
                _remove_link(dest)
            elif dest.is_file():
                dest.unlink(missing_ok=True)
            elif dest.is_dir():
                # A real managed copy (Windows SHARED_ITEMS); no junctions live
                # inside it, so a plain rmtree is safe here.
                shutil.rmtree(dest, ignore_errors=True)
        except OSError:
            pass
