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
unified conversation history. POSIX-only: Windows shares by re-synced copy,
which would fork history rather than share it. If the profile already
accumulated its own history, it is merged into ``~/.claude`` first so nothing
disappears from ``--resume``.

This module must not import ``switcher`` (switcher imports us for the
session-aware guards); it receives a ``ClaudeAccountSwitcher`` instance.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from claude_swap import macos_keychain
from claude_swap.claude_locks import proper_lockfile
from claude_swap.exceptions import (
    ClaudeCodeLockTimeout,
    CredentialReadError,
    SessionError,
)
from claude_swap.fsutil import replace_with_retry
from claude_swap.locking import FileLock
from claude_swap.models import Platform
from claude_swap.paths import get_default_global_config_path
from claude_swap.printer import accent, dimmed, muted, warning
from claude_swap.process_detection import ClaudeSession, scan_sessions
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

# Conversation-history items linked additionally under --share-history.
# POSIX symlinks only: Windows copy-mode would fork history, not share it.
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


def stale_marker_for(session_dir: Path) -> Path:
    """Where a profile's stale marker is WRITTEN: a SIBLING of the profile dir.

    Not a child. One of the two writers is the fallback for a session-dir
    invalidation that just failed, and the faults it exists for — EACCES on
    the session dir, a read-only mount — are faults on that same directory.
    A child marker therefore asks the very directory that denied the unlink
    to accept a create; it refuses, ``mark_session_stale`` swallows the
    OSError, and the profile goes on serving a superseded token with nothing
    recording it. The parent (``<backup>/sessions/``) is ours and is not what
    a per-profile EACCES denied.

    Sibling of the DIR, so ``purge``'s ``iterdir()`` (dirs only) and
    ``_delete_session_profile``'s ``rmtree`` both still see what they expect.
    """
    return session_dir.parent / f".{session_dir.name}{STALE_MARKER}"


def is_session_stale(session_dir: Path) -> bool:
    """Whether a profile is flagged for re-bootstrap.

    Both locations: the child path is where the marker used to live, and a
    profile marked by an older cswap on this machine has a pending
    re-bootstrap that the move must not drop. Read, never written.
    """
    return (
        stale_marker_for(session_dir).exists()
        or (session_dir / STALE_MARKER).exists()
    )


def clear_session_stale(session_dir: Path) -> bool:
    """Drop a profile's stale flag from both marker locations.

    Both are unlinked under their own ``try/except OSError``, not just
    ``missing_ok=True``: that only covers ENOENT, not EACCES/EROFS, and a
    caller that just tolerated a denied dir (``_delete_session_profile``'s
    ``rmtree(ignore_errors=True)``) runs into the same denial again here --
    on the sibling location (``stale_marker_for``, a denied PARENT) as much
    as the legacy CHILD location. Each unlink still runs first, so the
    happy-path removal is unaffected.

    Returns whether every marker is gone, the way ``mark_session_stale``
    reports its own landing. Tolerating the fault is right; reporting a
    removal that did not happen is not -- the caller logs the profile as
    removed while a marker survives, and the next run's stale arm re-fires
    on it forever, POSTing a refresh grant each time.
    """
    cleared = True
    for marker in (stale_marker_for(session_dir), session_dir / STALE_MARKER):
        try:
            marker.unlink(missing_ok=True)
        except OSError:
            cleared = False
    return cleared


def mark_session_stale(session_dir: Path) -> bool:
    """Flag a live session profile for re-bootstrap once it exits.

    Returns whether the marker landed. It can still fail — a read-only mount
    denies the sibling as surely as the child — and the caller that reaches
    here BECAUSE an invalidation failed has no other record of it, so the
    answer is reported rather than swallowed.
    """
    try:
        stale_marker_for(session_dir).touch()
        return True
    except OSError:
        return False

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


def keychain_service_name(config_dir: Path | str) -> str:
    """Keychain service name Claude Code derives for this config dir.

    Claude hashes the raw ``CLAUDE_CONFIG_DIR`` env var value, NFC-normalized
    and unresolved (claude src ``envUtils.ts``/``macOsKeychainHelpers.ts``).
    Hash exactly the string we export — never a resolved/realpath variant.
    Accepts a ``str`` so a caller holding the raw env value can hash it without
    a ``Path`` round-trip, which would drop a trailing slash or ``./``.
    """
    normalized = unicodedata.normalize("NFC", str(config_dir))
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
    except macos_keychain.KEYCHAIN_ERRORS:
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
    return read_config_dir_credentials(str(session_dir))


def _may_have_credential_material(session_dir: Path) -> bool:
    """Whether a profile's credential material is anything but definitely absent.

    Existence test for the validation-timeout fallback, not a read: ``False``
    only when every store is *positively* empty — no readable plaintext seed
    and (on macOS) a keychain miss claude itself signals with rc 44. An
    unreadable keychain — locked, denied, ``security`` timing out under the
    same load that timed out the probe — is indeterminate, and leans present:
    the profile may hold the freshest generation of the account's token
    family, and re-bootstrapping over it would start by deleting that entry.
    ``read_session_credentials`` is unsuitable here by design — its
    error-to-plaintext fallback erases exactly this distinction.
    """
    try:
        if (session_dir / ".credentials.json").read_text(encoding="utf-8"):
            return True
    except (OSError, ValueError):
        pass  # no readable seed — the keychain may still hold the material
    if Platform.detect() != Platform.MACOS:
        return False
    try:
        material = macos_keychain.get_password(
            keychain_service_name(session_dir), _keychain_account_name()
        )
    except macos_keychain.KEYCHAIN_ERRORS:
        return True  # unreadable is not absent: preserve the profile
    return material is not None


def _artifacts_say_usable(session_dir: Path, email: str, org_uuid: str) -> bool:
    """What local artifacts can say about a profile without running the probe.

    Upstream's probe-timeout fallback (#224 follow-up), lifted to a function
    because two callers ask it and a copy in each is how they drift apart: the
    REUSE check (`_is_session_valid`) and the post-bootstrap check, which must
    not fail a launch over a probe that merely did not answer.

    Deliberately NOT consulted for "unreachable". There the question is
    whether `claude` can be run at all, and no file on disk answers it.
    """
    return _may_have_credential_material(session_dir) and (
        not session_identity_drifted(session_dir, email, org_uuid)
    )


# Bounded retry for the strict capture read, mirroring the active store's
# (credentials._ACTIVE_READ_ATTEMPTS / _ACTIVE_READ_RETRY_DELAY).
_STRICT_KEYCHAIN_ATTEMPTS = 2
_STRICT_KEYCHAIN_RETRY_DELAY = 0.3  # seconds between attempts


def read_config_dir_credentials(
    config_dir: str,
    *,
    strict_keychain: bool = False,
    keychain_service: str | None = None,
) -> str | None:
    """Same read for an arbitrary ``CLAUDE_CONFIG_DIR`` value.

    Takes the raw string rather than a ``Path``: claude derives the keychain
    service name from the exported value verbatim, so a ``Path`` round-trip —
    which drops a trailing slash or a leading ``./`` — would look up a service
    name claude never wrote and fall back to the (possibly stale) plaintext
    seed instead.

    ``keychain_service`` overrides the derived hashed name for the one profile
    whose item is *unsuffixed*: the default profile, which claude selects with
    ``CLAUDE_SECURESTORAGE_CONFIG_DIR`` defined-but-empty.

    ``strict_keychain`` is for capture (``add``): an *unreadable* keychain —
    locked, denied, timed out — retries once and then raises
    :class:`CredentialReadError` instead of falling back to the plaintext seed.
    The seed may predate an in-profile ``/login``, and capturing it would file
    one account's email against another's token — the very mismatch the capture
    path exists to prevent. An *absent* entry (rc 44) still falls back either
    way: absence is claude's own signal to read the file.
    """
    directory = Path(config_dir)
    if not directory.is_dir():
        return None
    if Platform.detect() == Platform.MACOS:
        attempts = _STRICT_KEYCHAIN_ATTEMPTS if strict_keychain else 1
        for attempt in range(attempts):
            try:
                creds = macos_keychain.get_password(
                    keychain_service or keychain_service_name(config_dir),
                    _keychain_account_name(),
                )
            except macos_keychain.KEYCHAIN_ERRORS as e:
                if attempt + 1 < attempts:
                    time.sleep(_STRICT_KEYCHAIN_RETRY_DELAY)
                    continue
                if strict_keychain:
                    raise CredentialReadError(
                        f"Keychain entry for profile {config_dir} is unreadable "
                        f"(locked or busy) — unlock the keychain and retry: {e}"
                    ) from e
                break  # best-effort read: the plaintext seed is the next-best truth
            if creds:
                return creds
            break  # entry absent (rc 44) — claude's own signal to read the file
    try:
        return (directory / ".credentials.json").read_text(encoding="utf-8")
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


def scan_live_sessions(session_dir: Path) -> tuple[list[ClaudeSession], int]:
    """Live Claude instances for a profile, and records that could not be read.

    Every caller of this gates a destructive step, so the unreadable count
    travels with the list: a record we could not read is not evidence that
    nothing is running. Renamed from ``live_sessions_for`` deliberately -- the
    old name returned a bare list, and a call site left on it would read a
    tuple as unconditionally truthy.
    """
    if not session_dir.exists():
        return [], 0
    return scan_sessions(claude_dir=session_dir)


def profile_is_quiescent(session_dir: Path) -> bool:
    """Nothing is running against this profile, AND we could read every record.

    The predicate every "is it safe to rewrite/remove this profile" site
    wants. False when a record is unreadable: not knowing is not the same as
    knowing nothing is there, and the step behind these callers cannot be
    undone.
    """
    sessions, unreadable = scan_live_sessions(session_dir)
    return not sessions and unreadable == 0


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
        if share_history and self.switcher.platform == Platform.WINDOWS:
            raise SessionError(
                "--share-history is not supported on Windows yet: sharing uses "
                "re-synced copies there, which would fork the history instead "
                "of sharing it."
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
        stale = is_session_stale(session_dir) and profile_is_quiescent(session_dir)

        # Cheap reuse check without the lock: most launches hit this.
        if not stale and self._is_session_valid(session_dir, email, org_uuid):
            self._sync_sharing(session_dir, share, share_history)
            return session_dir, account_num, email

        # One refresh so the profile starts with a fresh access token —
        # BEFORE the bootstrap lock (the gate takes the same non-reentrant
        # FileLock and POSTs over the network, which must never run under a
        # held lock). The gate re-reads the freshest copy itself and
        # persists via fingerprint CAS; _bootstrap then reads the rotated
        # backup. Failure is non-fatal: the stored token may still be
        # valid, and claude refreshes on its own at runtime. Setup-token
        # accounts (--add-token) have no refresh token by design — skip
        # silently instead of warning about a flow that can't happen.
        pre_creds = self.switcher.read_account_credentials(account_num, email)
        if pre_creds and self._has_refresh_token(pre_creds):
            outcome = self.switcher.consume_backup_grant(
                account_num, email, pre_creds
            )
            if outcome.error is not None and outcome.credentials:
                # `error is None` is the gate's own "the slot is freshened
                # and safe to activate" signal, and it is NOT implied by
                # credentials being present: a failed persist returns the
                # successor AND `transient`, with the backup still holding
                # the generation whose grant was just spent.
                #
                # WARNING ABOUT IT IS NOT ENOUGH. `_bootstrap` re-reads the
                # backup, so continuing seeds the profile from that spent
                # generation and claude's first refresh gets invalid_grant —
                # the warning scrolls past and the session is broken anyway.
                #
                # Refuse — but the two demoted shapes need OPPOSITE advice,
                # and `error`/`credentials` cannot tell them apart, which is
                # why the gate carries `stashed`.
                #
                # Stashed: retrying is right. The next run's gate adopts the
                # successor without consuming anything and bootstraps
                # normally, so the recovery machinery already built here does
                # the rest.
                #
                # NOT stashed (`consume-gate-unpersisted`, where the persist
                # AND the stash both failed): retrying POSTs the spent
                # predecessor and earns a strike. The successor survived only
                # in this return value, which the raise discards — saying
                # "nothing was lost" there would be false on both counts. The
                # remedy is the storage failure, which is what the gate's own
                # ERROR log at that site already says.
                #
                # This shape is distinguishable at the call site: a genuine
                # network-transient carries `credentials=None` and falls to
                # the warning below, where continuing on the stored
                # credentials is correct because nothing was spent.
                if outcome.stashed:
                    raise SessionError(
                        f"Account-{account_num}'s refreshed credential could "
                        f"not be stored, so the backup still holds a spent "
                        f"grant. The successor is stashed — please retry, and "
                        f"the next run adopts it automatically."
                    )
                raise SessionError(
                    f"Account-{account_num}'s refreshed credential could "
                    f"neither be stored nor stashed, so the backup holds a "
                    f"spent grant and the successor is gone. Fix the storage "
                    f"failure first; retrying before that spends nothing but "
                    f"earns a strike. If the slot strikes, log in again and "
                    f"re-add it: cswap --add-account --slot {account_num}"
                )
            if outcome.error is not None:
                warning(
                    f"Could not refresh the token for Account-{account_num}; "
                    "continuing with the stored credentials."
                )

        with FileLock(self.switcher.lock_file, timeout=_BOOTSTRAP_LOCK_TIMEOUT):
            # Re-evaluate the marker under the lock, then re-check validity:
            # another `cswap run` may have bootstrapped while we waited.
            if is_session_stale(session_dir) and profile_is_quiescent(session_dir):
                self.switcher._invalidate_session_credentials(account_num, email)
                clear_session_stale(session_dir)
            if self._is_session_valid(session_dir, email, org_uuid):
                # Valid, but possibly not on the generation WE just paid for.
                # The consume above runs outside this lock (it POSTs), so a
                # peer `cswap run` can bootstrap while we wait — and its
                # profile predates our rotation. Its refresh token is the one
                # our gate consumed, so claude's own refresh would get
                # invalid_grant on first use: a spent grant, silently.
                # Validity is an identity check, not a generation check, so it
                # cannot see this. Re-seed instead of returning early.
                #
                # NEVER under a live claude. _bootstrap deletes the profile's
                # Keychain entry and overwrites .credentials.json, and the peer
                # that bootstrapped ahead of us has already exec'd into that
                # profile — rewriting it beneath a running instance is the
                # failure every other invalidation site in this file guards
                # against (the STALE_MARKER branch above, and
                # _post_backup_write's). This branch fires precisely when the
                # profile is VALID, which is the live case, so it needed the
                # guard most and had it least. Deferring costs the peer a
                # generation it can still refresh from; rewriting costs it the
                # session.
                if not self._profile_matches_backup(
                    session_dir, account_num, email
                ) and profile_is_quiescent(session_dir):
                    self._bootstrap(session_dir, account_num, email, org_uuid)
                self._sync_sharing(session_dir, share, share_history)
                return session_dir, account_num, email

            self._bootstrap(session_dir, account_num, email, org_uuid)
            self._sync_sharing(session_dir, share, share_history)

            verdict = self._session_validity(session_dir, email, org_uuid)
            # NEITHER probe-failure verdict may reach `_cleanup_failed_session`
            # below: it deletes the Keychain entry and the whole directory, and
            # running it here would destroy a profile we never managed to look
            # at — on nothing worse than a 10s timeout on a loaded machine.
            #
            # But "do not delete" is not "do not proceed". We have just
            # bootstrapped from the backup credentials, so the same local
            # artifacts that answer the reuse path answer this one, and a
            # timeout here must not fail a launch that is fine (#224
            # follow-up). Only "unreachable" still stops: no local file can
            # tell us whether `claude` runs, and a session we cannot exec into
            # is not a session.
            if verdict == "unknown" and _artifacts_say_usable(
                session_dir, email, org_uuid
            ):
                verdict = "valid"
            if verdict in ("unknown", "unreachable"):
                raise SessionError(
                    f"Session profile for Account-{account_num} ({email}) could "
                    f"not be verified: `claude auth status` did not run or did "
                    f"not answer. The profile is left in place — check that "
                    f"`claude` is on PATH, then retry."
                )
            if verdict != "valid":
                self._cleanup_failed_session(session_dir)
                raise SessionError(
                    f"Session profile for Account-{account_num} ({email}) failed "
                    f"validation. Log in with that account and re-add it: "
                    f"cswap --add-account --slot {account_num}"
                )
        # Lock released here, before any exec.

        return session_dir, account_num, email

    def _profile_matches_backup(
        self, session_dir: Path, account_num: str, email: str
    ) -> bool:
        """Is the profile on the same credential generation as the backup?

        Compared by fingerprint, because that is what identifies a generation;
        the identity (email/org) is the same across all of them, which is why
        ``_is_session_valid`` cannot answer this.

        Unknowable answers are TRUE — an unreadable backup or profile is not
        evidence of a mismatch, and re-bootstrapping on one would throw away a
        working profile over a read error.
        """
        from claude_swap import oauth as _oauth

        profile = read_session_credentials(session_dir)
        backup = self.switcher.read_account_credentials(account_num, email)
        if not profile or not backup:
            return True
        return _oauth.credential_fingerprint(
            profile
        ) == _oauth.credential_fingerprint(backup)

    def _bootstrap(
        self, session_dir: Path, account_num: str, email: str, org_uuid: str
    ) -> None:
        """Seed the session profile from backup storage. Caller holds the lock."""
        # Claude reads the keychain before the plaintext file — a stale hashed
        # entry from an earlier profile at this path would shadow the seed.
        delete_macos_keychain_entry(session_dir)

        creds, unreadable = self.switcher._read_account_credentials_ex(
            account_num, email
        )
        if not creds:
            if unreadable:
                # Third copy of the switch path's message; same reason not to
                # send the user to a re-add over a locked Keychain.
                raise SessionError(
                    f"Account-{account_num}'s backup is in the macOS Keychain "
                    f"but it is unreadable right now (locked or no GUI "
                    f"session). Retry from a GUI terminal; do not re-add."
                )
            raise SessionError(
                f"Account-{account_num} has no stored credentials. "
                f"Re-add with: cswap --add-account --slot {account_num}"
            )

        # The pre-lock refresh (see run(): the consume gate must not run
        # under this lock — its POST is network and the FileLock is
        # non-reentrant) may have already rotated the backup; the read
        # above picked that successor up. No POST happens here.

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
        # hashed service name can't be recomputed once the dir is gone. The
        # stale marker is a sibling, so rmtree does not take it.
        delete_macos_keychain_entry(session_dir)
        shutil.rmtree(session_dir, ignore_errors=True)
        clear_session_stale(session_dir)

    # -- validation ------------------------------------------------------

    def _session_validity(
        self, session_dir: Path, email: str, org_uuid: str
    ) -> str:
        """``"valid"`` / ``"invalid"`` / ``"unknown"`` / ``"unreachable"``.

        Local check only (`claude auth status` makes no API call): a revoked
        but unexpired token still passes and fails on first real use.

        The last two are both "the PROBE failed, not the profile", and neither
        may share a return value with ``"invalid"`` — the caller that acts on
        ``"invalid"`` deletes the profile's Keychain entry and rmtree's the
        directory, and a probe we could not run is not evidence about the
        profile. They are split because REUSE wants opposite answers:

        ``"unknown"`` — the probe ran and did not answer (timeout, output that
        will not parse). Nothing suggests the profile is bad, so reuse leans
        valid; on a loaded machine `claude`'s cold start exceeds the 10s
        timeout and forcing a re-bootstrap there fails a launch that would
        have worked (upstream #224/#225).

        ``"unreachable"`` — `claude` could not be spawned at all. Reuse leans
        invalid: a binary we cannot run cannot serve the session either way,
        so the honest outcome is the bootstrap path and its "check that
        `claude` is on PATH" error, not a silent reuse.
        """
        if not session_dir.is_dir():
            return "invalid"
        # On Windows `claude` is a `.cmd` shim, and a bare "claude" passed to
        # subprocess won't resolve it (PATHEXT isn't applied) — it raises
        # FileNotFoundError. `shutil.which` finds the shim; when it finds
        # nothing at all the run below still raises, and that is "unknown".
        claude_bin = shutil.which("claude") or "claude"
        try:
            result = subprocess.run(
                [claude_bin, "auth", "status", "--json"],
                env=_probe_env(session_dir),
                capture_output=True,
                text=True,
                timeout=_AUTH_STATUS_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            # The verdict layer answers what the PROBE established, and a
            # timeout established nothing. Upstream's artifact fallback is a
            # REUSE judgement, not a verdict, so it lives in
            # `_is_session_valid` where its answer is what gets consumed —
            # putting it here would make "the probe timed out" and "the
            # profile is bad" the same value again, which is the conflation
            # this whole tri-state exists to undo.
            return "unknown"
        except OSError:
            return "unreachable"
        if result.returncode != 0:
            return "invalid"
        try:
            status = json.loads(result.stdout)
        except json.JSONDecodeError:
            return "unknown"
        if not isinstance(status, dict):
            return "unknown"
        if status.get("loggedIn") is not True:
            return "invalid"
        # Verified against claude 2.1.175; an env API key reports a different
        # method, and the probe env already drops those vars anyway.
        if status.get("authMethod") != "claude.ai":
            return "invalid"
        if status.get("email") != email:
            return "invalid"
        # Lenient org check: only when both sides have a value, so schema
        # drift degrades to email-only validation instead of false negatives.
        status_org = status.get("orgId")
        if status_org and org_uuid and status_org != org_uuid:
            return "invalid"
        return "valid"

    def _is_session_valid(self, session_dir: Path, email: str, org_uuid: str) -> bool:
        """Whether the profile is usable as far as we can tell.

        Reuse-shaped view of :meth:`_session_validity`, and best-effort by
        design. ``"unknown"`` — the probe ran and did not answer — is decided
        from local artifacts instead of blanket-True, because "the probe timed
        out" is not by itself a reason to reuse: credential material must not
        be definitely absent (keychain-aware — claude migrates a macOS
        profile's credential into the keychain and deletes the plaintext seed,
        while stale invalidation deletes both) and the recorded identity must
        not have drifted (in-session /login to another account). Anything
        subtler still fails on first real use.

        ``"unreachable"`` answers False without consulting artifacts: the
        question there is not whether the profile holds a credential but
        whether `claude` can be run at all, and no local file answers that.

        Callers that DESTROY on a negative must use the full verdict instead.
        For them the difference between "invalid" and "could not tell" is the
        difference between a correct cleanup and deleting a working profile,
        and this boolean cannot express it.
        """
        verdict = self._session_validity(session_dir, email, org_uuid)
        if verdict == "unknown":
            return _artifacts_say_usable(session_dir, email, org_uuid)
        return verdict == "valid"

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
        # History links are POSIX-only (run() rejects the flag on Windows;
        # this also drops any links left by a POSIX→Windows profile move).
        if self.switcher.platform == Platform.WINDOWS:
            share_history = False
        active_items = (SHARED_ITEMS if share else ()) + (
            HISTORY_ITEMS if share_history else ()
        )
        source_root = Path.home() / ".claude"
        manifest_path = session_dir / SHARE_MANIFEST
        managed = self._read_manifest(manifest_path)

        # A flag turned off since last launch: remove the links we created
        # for it (never plain files/dirs the user accumulated themselves).
        # For history items that holds even when the manifest claims them:
        # a stale manifest (lock-free launches race) must never be able to
        # delete real conversation history — only ever unlink symlinks.
        for name in managed:
            if name not in active_items:
                dest = session_dir / name
                if name in HISTORY_ITEMS and dest.exists() and not dest.is_symlink():
                    continue
                self._remove_managed(dest)
        if not active_items:
            manifest_path.unlink(missing_ok=True)
            return

        use_symlinks = self.switcher.platform != Platform.WINDOWS
        new_managed: list[str] = []

        for name in active_items:
            src = source_root / name
            dest = session_dir / name

            if name in HISTORY_ITEMS and not self._prepare_history_share(
                src, dest, session_dir
            ):
                continue

            if not src.exists():
                # Source vanished (or never existed): prune our own entry.
                if name in managed:
                    self._remove_managed(dest)
                continue

            # Link to the fully resolved target. When ~/.claude/<name> is itself
            # a symlink (a dotfiles setup), linking to the unresolved path makes
            # a link-to-a-link, and Claude Code's atomic settings write — which
            # resolves only one hop — renames its temp file over the intermediate
            # link, silently replacing it with a regular file
            # (anthropics/claude-code#78162).
            link_target = src.resolve() if src.is_symlink() else src

            if dest.is_symlink():
                if name not in managed:
                    managed = [*managed, name]  # adopt: only cswap links here
                if use_symlinks:
                    try:
                        if dest.readlink() != link_target:
                            dest.unlink()
                            dest.symlink_to(link_target)
                    except OSError:
                        continue
                    new_managed.append(name)
                    continue
                # Platform moved POSIX → Windows: replace link with a copy.
                dest.unlink()
            elif dest.exists() and name not in managed:
                # Pre-existing user data in the profile — never touch it.
                print(
                    dimmed(
                        f"Not sharing {name}: the session profile already has "
                        "its own copy."
                    )
                )
                continue

            try:
                if use_symlinks:
                    if dest.exists():
                        self._remove_managed(dest)
                    dest.symlink_to(link_target)
                else:
                    if dest.exists():
                        self._remove_managed(dest)
                    if src.is_dir():
                        shutil.copytree(src, dest)
                    else:
                        shutil.copy2(src, dest)
            except OSError as e:
                self._logger.warning(f"Failed to share {name} into session: {e}")
                continue
            new_managed.append(name)

        # Anything we managed before but no longer created gets removed above;
        # write the manifest atomically so a concurrent reader never sees a
        # truncated file.
        self._write_manifest(manifest_path, new_managed)

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
        if dest.exists() and not dest.is_symlink():
            # Real per-account history accumulated before the flag existed.
            # Merging moves files out from under any claude still running in
            # this profile, so only migrate when the profile is quiescent.
            if not profile_is_quiescent(session_dir):
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
            replace_with_retry(tmp, manifest_path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    @staticmethod
    def _remove_managed(dest: Path) -> None:
        """Remove a cswap-created share entry (link or copy), never user data
        beyond it — callers guarantee `dest` is manifest-listed or a symlink."""
        try:
            if dest.is_symlink() or dest.is_file():
                dest.unlink(missing_ok=True)
            elif dest.is_dir():
                shutil.rmtree(dest, ignore_errors=True)
        except OSError:
            pass
