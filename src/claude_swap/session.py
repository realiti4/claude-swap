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
``CLAUDE.md``, ``skills/``, ``commands/``, ``agents/``, and ``rules/`` follow
them into the session profile as links — Claude's settings writer detects
a symlink and writes through to the target, so in-session ``/config``
changes land in ``~/.claude``. On macOS and Linux every item is a symlink.
On Windows a directory is a junction, which any user may create, and a
file is a symlink when the process may create one (Developer Mode, or an
elevated terminal) and otherwise a copy re-synced on every launch, as
before. A manifest records what cswap created so removal never touches
user data.

History sharing (on by default; ``--no-share-history`` or ``run.shareHistory
false`` turns it off): additionally links ``projects/`` (conversation
transcripts — what ``claude --resume`` lists) and ``history.jsonl`` (prompt
history) from ``~/.claude``, so all accounts see one unified conversation
history. By link only, never by copy, which would fork history rather than
share it: on Windows without a symlink privilege ``projects/`` is still
shared (a junction) and ``history.jsonl`` stays the account's own, which
the launch says once. If the profile already accumulated its own history,
it is merged into ``~/.claude`` first so nothing disappears from
``--resume``. A file that differs on both sides is never deleted: the copy
not kept is parked under ``<backup_dir>/history-conflicts/``.

Junctions are not symlinks to the stdlib: ``is_symlink()`` is False for one,
``is_dir()`` follows it, ``rglob`` walks into it. Every test for "is this a
link cswap made" is therefore ``fsutil.is_link``, and the merge walks a
profile's history with its own eyes (``_history_tree``).

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
import time
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from claude_swap import macos_keychain, spawn
from claude_swap.claude_locks import proper_lockfile
from claude_swap.exceptions import (
    ClaudeCodeLockTimeout,
    CredentialReadError,
    LockError,
    SessionError,
)
from claude_swap.fsutil import (
    atomic_write_text,
    can_junction,
    can_symlink,
    is_link,
    link_points_at,
    make_dir_link,
    remove_link,
    replace_with_retry,
)
from claude_swap.locking import FileLock
from claude_swap.models import Platform
from claude_swap.paths import get_default_global_config_path
from claude_swap.printer import accent, dimmed, muted, warning
from claude_swap.process_detection import ClaudeSession, scan_sessions
from claude_swap.settings import (
    atomic_write_json,
    explicit_share_history,
    load_run_settings,
)

if TYPE_CHECKING:
    from claude_swap.switcher import ClaudeAccountSwitcher

# Items mirrored from ~/.claude into session profiles when sharing is on.
# Deliberately excludes anything account- or instance-scoped: plugins/,
# sessions/, ide/, .claude.json, .credentials.json, statsig/ and other
# telemetry. projects/ and history.jsonl are shared too, but as HISTORY_ITEMS:
# they have their own switch (run.shareHistory) and their own rules.
# .claude.json stays excluded as a file, but its one user-scoped key —
# top-level mcpServers — is mirrored separately by _sync_mcp_servers.
SHARED_ITEMS = (
    "settings.json",
    "keybindings.json",
    "CLAUDE.md",
    "skills",
    "commands",
    "agents",
    "rules",
)

# Conversation-history items, linked unless history sharing is turned off.
# Always by link, never by copy: a copy would fork history, not share it.
HISTORY_ITEMS = (
    "projects",
    "history.jsonl",
)

# The shared names that are directories. Known by name rather than by looking
# at ``~/.claude`` because a history item may have to be linked before it
# exists there (``_seed_history_source``), and because the kind decides the
# link on Windows: a directory gets a junction, which needs no privilege; a
# file needs a symlink, which does.
_DIR_ITEMS = frozenset({"skills", "commands", "agents", "rules", "projects"})


# Records which entries in a session profile cswap created (so --no-share and
# re-syncs only ever remove cswap-managed links/copies, never user data).
SHARE_MANIFEST = ".cswap-shared.json"

# Where a losing copy of a same-named transcript is parked. Never a delete:
# a UUID collision after an un-share means two different halves of one
# session, not a duplicate.
QUARANTINE_DIRNAME = "history-conflicts"

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

# The one lock every history migration takes. It lives IN the shared
# destination (``~/.claude``), the only place every launch that can merge into
# that tree agrees on: on Linux the backup root follows ``XDG_DATA_HOME``, so
# two cswap stores can share one ``~/.claude``, and a lock inside either store
# is invisible to the other. Never deleted, not even by ``--purge``: unlinking
# a flock'd file lets a newcomer create a fresh one and hold "the" lock next
# to a process still holding the old inode. A named constant rather than an
# inline literal — after a rename, old and new launches would serialize on
# different files.
MIGRATION_LOCK = ".cswap-history-migration.lock"

# While a profile's own history is merged into ``~/.claude``, the link that
# will replace it already stands beside it under this suffix
# (``<item>.cswap-link-staging``), made before anything moves and renamed
# into place after. One left behind by an interrupted launch is a link and
# holds nothing; the next launch removes it.
_STAGED_LINK_SUFFIX = ".cswap-link-staging"

# How long a launch waits for the migration lock only to clear such a
# leftover. A busy lock means a migration is under way and the "leftover"
# may be its live staging link, so the wait is short and giving up is fine.
_STAGED_CLEANUP_LOCK_TIMEOUT = 5.0


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

# Migration moves every transcript in a profile, so it waits longer than the
# bootstrap for a peer to finish rather than bailing and leaving a half state.
_MIGRATION_LOCK_TIMEOUT = 30.0


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
    for service in _keychain_services(str(session_dir)):
        try:
            material = macos_keychain.get_password(service, _keychain_account_name())
        except macos_keychain.KEYCHAIN_ERRORS:
            return True  # unreadable is not absent: preserve the profile
        if material is not None:
            return True
    return False


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


def _keychain_services(config_dir: str, override: str | None = None) -> list[str]:
    """Keychain service names that may hold a profile's credential, in
    lookup order.

    Claude hashes the exported ``CLAUDE_CONFIG_DIR`` verbatim, so a profile
    that is a symlink was launched under one of two names: its own path, or
    the target's, when a tool launches claude at the target directly and
    keeps the per-account symlink as its bookkeeping (a lease). Reading only
    the link's name would miss every rotation claude wrote under the
    target's and serve the plaintext seed left behind, which is how a leased
    profile's usage silently froze at the last pre-rotation measurement. The
    profile's own name is tried first, then the target's. A relative target
    is joined onto the link's parent and tried in two spellings, likeliest
    first: the lexical form with ``..`` collapsed (``/x/space``, what a tool
    launching at the target usually exports), offered only when it names the
    link's own directory — under a parent that is itself a symlink elsewhere
    it can name an unrelated directory with its own entry — and then the raw
    join (``/x/sessions/../space``, what ``$(dirname link)/$(readlink link)``
    exports). ``override`` names the unsuffixed default-profile item and
    stands alone.
    """
    if override is not None:
        return [override]
    services = [keychain_service_name(config_dir)]
    try:
        target = os.readlink(config_dir)
    except OSError:
        return services
    if os.path.isabs(target):
        services.append(keychain_service_name(target))
        return services
    raw = os.path.join(os.path.dirname(config_dir), target)
    lexical = os.path.normpath(raw)
    try:
        if lexical != raw and os.path.samefile(lexical, raw):
            services.append(keychain_service_name(lexical))
    except OSError:
        pass  # nothing at the lexical path: not a spelling of this directory
    services.append(keychain_service_name(raw))
    return services


def _keychain_entry(service: str, strict: bool) -> str | None:
    """One service's item, or ``None`` when absent (rc 44). An unreadable
    keychain is retried once under ``strict`` and then raised as it stands."""
    attempts = _STRICT_KEYCHAIN_ATTEMPTS if strict else 1
    while True:
        try:
            return macos_keychain.get_password(service, _keychain_account_name())
        except macos_keychain.KEYCHAIN_ERRORS:
            attempts -= 1
            if not attempts:
                raise
            time.sleep(_STRICT_KEYCHAIN_RETRY_DELAY)


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
    way: absence is claude's own signal to read the file. For a symlinked
    profile that fallback includes the target's entry under both modes, on
    purpose: it is the newest generation claude wrote for that directory,
    and the seed it would otherwise capture is the one claude stopped
    updating when it first wrote the keychain.
    """
    directory = Path(config_dir)
    if not directory.is_dir():
        return None
    if Platform.detect() == Platform.MACOS:
        for service in _keychain_services(config_dir, keychain_service):
            try:
                creds = _keychain_entry(service, strict_keychain)
            except macos_keychain.KEYCHAIN_ERRORS as e:
                if strict_keychain:
                    raise CredentialReadError(
                        f"Keychain entry for profile {config_dir} is unreadable "
                        f"(locked or busy) — unlock the keychain and retry: {e}"
                    ) from e
                break  # best-effort read: the plaintext seed is the next-best truth
            if creds:
                return creds
            # entry absent (rc 44) — claude's own signal to look further
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
    freshest) rather than abandoning it over a broken ``.claude.json``. That
    is a read-side policy: ``_adopt_session_credential``, which writes the
    profile's credential into the backup, requires ``read_session_identity``
    to succeed on top of this.
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


def _history_tree(root: Path) -> list[Path]:
    """Every path under ``root``, deepest first, without looking through a link.

    ``rglob`` and ``os.walk`` descend into a Windows junction as into a plain
    directory, and the merge moves what it is shown: through a junction
    someone placed inside a profile's ``projects/`` it would carry off the
    contents of whatever that junction leads to. So the walk is spelled out
    here, and a link met on the way — junction or symlink, to a directory or
    a file — ends it with an error naming the path, before anything has
    moved. The merge is of this profile's own files; a link holds none.
    """
    found: list[Path] = []

    def visit(directory: Path) -> None:
        with os.scandir(directory) as entries:
            children = list(entries)
        for entry in children:
            path = Path(entry.path)
            if entry.is_symlink() or entry.is_junction():
                raise OSError(
                    f"{path} is a link, which a history merge never follows; "
                    "move it out of the profile to share the rest"
                )
            found.append(path)
            if entry.is_dir(follow_symlinks=False):
                visit(path)

    visit(root)
    return sorted(found, reverse=True)


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


def _last_timestamp(path: Path) -> str | None:
    """Last ISO-8601 ``timestamp`` in a JSONL transcript, scanning backwards.

    Claude closes nearly every transcript with untimestamped state lines
    (``last-prompt``, ``mode``, ``ai-title``, ``file-history-snapshot``), so
    reading only the final line finds a timestamp in about 1% of real files.
    Scanning back finds one within a line or two. Values are ISO-8601 Zulu
    with milliseconds, so lexical comparison is chronological.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(text.splitlines()):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            stamp = entry.get("timestamp")
            if isinstance(stamp, str) and stamp:
                return stamp
    return None


def _line_count(path: Path) -> int:
    """Lines in a file; 0 when it cannot be read (so it never wins a tiebreak)."""
    try:
        with path.open("rb") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return 0


def _profile_copy_wins(target: Path, candidate: Path) -> bool:
    """Whether the profile's ``candidate`` should replace the shared ``target``.

    A same-named pair is *not* the same session: a transcript that straddled
    an un-share event exists in both trees under one UUID with different
    content. Order, applied in full so the outcome is deterministic: later
    last-timestamp, then having a timestamp at all, then more lines, then the
    incumbent. The loser is quarantined either way, never dropped.
    """
    target_stamp = _last_timestamp(target)
    candidate_stamp = _last_timestamp(candidate)
    if target_stamp != candidate_stamp:
        if target_stamp is None:
            return True
        if candidate_stamp is None:
            return False
        return candidate_stamp > target_stamp
    return _line_count(candidate) > _line_count(target)


def _adds_nothing(target: Path, candidate: Path) -> bool:
    """Whether the shared ``target`` already holds every byte of ``candidate``.

    True for a byte-identical pair, and for a target that merely continues
    the candidate — an older snapshot of an append-only transcript is a
    prefix of the newer one. Either way the profile's copy is a true
    duplicate, the one collision that is safe to drop rather than
    quarantine. Unreadable means unknown, which is never "safe to drop".
    """
    try:
        if candidate.stat().st_size > target.stat().st_size:
            return False
        with candidate.open("rb") as profile, target.open("rb") as shared:
            while chunk := profile.read(1 << 20):
                if shared.read(len(chunk)) != chunk:
                    return False
        return True
    except OSError:
        return False


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
        # Filled by the most recent merge so the launch can report counts
        # without re-deriving them from the filesystem.
        self._last_merge_counts: tuple[int, int, Path | None] = (0, 0, None)
        # A deferred migration is explained once per launch, not once per
        # history item.
        self._deferral_reported = False
        # Whether this launch met history the profile holds on its own (and
        # so tried to merge it), whatever came of the attempt.
        self._own_history_found = False
        # A history item that cannot be linked on this machine is explained
        # once per launch (``_report_history_unlinkable``), as loudly as
        # sharing was asked for: the flag on the command line counts as
        # asking, ``run.shareHistory`` in the settings file too.
        self._unlinkable_reported = False
        self._share_history_asked = False

    # -- launch ----------------------------------------------------------

    def run(
        self,
        identifier: str,
        claude_args: list[str],
        share: bool = True,
        share_history: bool | None = None,
        require_session: bool = False,
    ) -> NoReturn:
        """Launch Claude Code as the given account in the current terminal.

        ``share_history`` is the launch's own choice (``--share-history`` /
        ``--no-share-history``); ``None`` leaves it to ``run.shareHistory``
        (see ``_resolve_share_history``).

        ``require_session`` turns the same-account fast path (a plain claude
        launch on the default login) into a refusal: a wrapper that hands a
        terminal to one account per session needs the isolation guaranteed,
        and a session on the default login is the one thing an account
        switch can later pull out from under it.
        """
        claude_bin = shutil.which("claude")
        if not claude_bin:
            raise SessionError(
                "'claude' was not found on PATH. Install Claude Code first."
            )
        # Resolved here for the fast path below, which never reaches
        # setup_session. setup_session is then handed the request, not the
        # answer: it resolves it the same way, and only the request says
        # whether sharing was asked for or defaulted (``_share_history_asked``
        # — an answer of True would read as asked for).
        requested_history = share_history
        share_history = self._resolve_share_history(requested_history)

        account_num, email, org_uuid = self.switcher.resolve_account(identifier)
        # Guard before the same-account direct-launch fast path below (which
        # _exec's claude and never returns) — and before setup_session.
        self._ensure_not_api_key(account_num, email)

        config_dir_preset = os.environ.get("CLAUDE_CONFIG_DIR")
        # Same-account fast path: never create a second credential copy
        # for the account that is already the active default login —
        # two copies of one account can drift if the server rotates the
        # refresh token. With CLAUDE_CONFIG_DIR preset (we may be inside
        # another session's terminal) the *default* login is still what a
        # session for this account would copy, so it is read from the
        # default profile rather than from the preset directory.
        if config_dir_preset:
            current = self._default_login_identity()
        else:
            current = self.switcher._get_current_account()
        if current is not None and current == (email, org_uuid):
            if require_session:
                raise SessionError(
                    f"Account-{account_num} ({email}) is the active default "
                    "login, so this launch would run plain claude on the "
                    "default login rather than in a session profile (a "
                    "second copy of the active credential would drift). "
                    "Switch the default login to another account first, "
                    "or run `claude` directly."
                )
            print(
                dimmed(
                    f"Account-{account_num} ({email}) is already the active "
                    "default login — launching claude directly."
                )
            )
            self._history_on_the_fast_path(
                session_dir_for(self.switcher.backup_dir, account_num, email),
                share_history,
            )
            env = dict(os.environ)
            # Run on the default login, not on the preset profile.
            env.pop("CLAUDE_CONFIG_DIR", None)
            self._exec(claude_bin, claude_args, env=env)
        if config_dir_preset:
            warning(
                f"CLAUDE_CONFIG_DIR is already set ({config_dir_preset}); "
                "overriding it for this launch."
            )

        scrubbed = [v for v in AUTH_OVERRIDE_ENV_VARS if os.environ.get(v)]
        if scrubbed:
            warning(
                f"Ignoring {', '.join(scrubbed)} for this session — it would "
                f"override the selected account inside Claude Code."
            )

        session_dir, account_num, email = self.setup_session(
            identifier, share, requested_history
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

    def _history_on_the_fast_path(
        self, session_dir: Path, share_history: bool
    ) -> None:
        """Deal with history the profile holds, on the launch that skips it.

        The fast path is plain claude on ``~/.claude``, so what the account's
        sessions wrote into its profile is not in front of the user (#239).
        With sharing on it is merged now, by the steps a session launch
        takes: an account that stays the default login would otherwise never
        be migrated, however often it is run. With sharing off it stays
        apart, and the launch says which history it is about to use.

        History only. Nothing here seeds, validates or refreshes the
        profile's credentials — a second live copy of the default login's
        credential is what the fast path exists to avoid. A profile with no
        history of its own (never used, or already linked) is left alone.
        """
        if not any(
            (session_dir / name).exists() and not is_link(session_dir / name)
            for name in HISTORY_ITEMS
        ):
            return
        if share_history:
            self._sync_items(session_dir, HISTORY_ITEMS, scope=HISTORY_ITEMS)
        else:
            print(
                dimmed(
                    "   This launch uses the default login's chat history "
                    "(~/.claude), not the account's own."
                )
            )

    def _resolve_share_history(self, requested: bool | None) -> bool:
        """Whether this launch shares chat history across accounts.

        ``requested`` is the launch's own choice and wins. ``None`` defers to
        ``run.shareHistory``, which is on unless the user turned it off.
        Resolved here rather than by the caller so every launcher gets the
        same answer from the same ``None``.

        Whether each history item can then actually be linked on this
        machine is decided per item, in ``_sync_items``: Windows without a
        symlink privilege keeps ``history.jsonl`` apart. The answer here is
        remembered as asked-for or defaulted, so that report can be as loud
        as the asking was.
        """
        self._share_history_asked = requested is True
        if requested is not None:
            return requested
        return load_run_settings(self.switcher.backup_dir).share_history

    @staticmethod
    def _default_login_identity() -> tuple[str, str] | None:
        """``(email, org_uuid)`` of the default profile's login, if any.

        Ignores ``CLAUDE_CONFIG_DIR``, like ``_read_mcp_source``.
        """
        config = SessionManager._load_json_object(get_default_global_config_path())
        oauth = config.get("oauthAccount") if config else None
        if not isinstance(oauth, dict) or not oauth.get("emailAddress"):
            return None
        return oauth["emailAddress"], oauth.get("organizationUuid", "") or ""

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
        self, identifier: str, share: bool, share_history: bool | None = None
    ) -> tuple[Path, str, str]:
        """Ensure a valid session profile exists; returns (dir, num, email).

        ``share_history`` is read like ``run``'s: ``None`` means
        ``run.shareHistory`` decides.
        """
        share_history = self._resolve_share_history(share_history)
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
        atomic_write_text(creds_path, creds)

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
        atomic_write_text(config_path, json.dumps(existing, indent=2))

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
            result = spawn.run(
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
        (conversation history) — independent concerns, so ``--no-share``
        alone gives a bare profile that still has the unified history. Both
        arrive here already decided (``_resolve_share_history`` for history).
        Idempotent; runs on every launch. Deliberately sources from the
        default ``~/.claude`` (not ``get_claude_config_home()``): sharing
        always mirrors the default profile, even when ``CLAUDE_CONFIG_DIR``
        is set in the invoking environment. File/dir sharing is lock-free on
        the reuse path — concurrent runs with different flags are last-writer-
        wins and self-heal on the next launch. The MCP mirror takes Claude's
        config lock, and only when it needs to write; merging a profile's own
        history into ``~/.claude`` takes ``MIGRATION_LOCK``.
        """
        if not session_dir.is_dir():
            return
        self._sync_mcp_servers(session_dir, share)
        self._sync_items(
            session_dir,
            (SHARED_ITEMS if share else ())
            + (HISTORY_ITEMS if share_history else ()),
        )

    def _sync_items(
        self,
        session_dir: Path,
        active_items: tuple[str, ...],
        scope: tuple[str, ...] = SHARED_ITEMS + HISTORY_ITEMS,
    ) -> None:
        """Link (or copy) ``active_items`` into the profile; undo the others.

        ``scope`` is what this call decides about. An entry in it that is not
        active is removed, and a managed entry outside it stays exactly as it
        is, on disk and in the manifest. Everything by default; the fast
        path passes the history items alone (``_history_on_the_fast_path``).
        """
        # Reset per call: _prepare_history_share accumulates into this as it
        # runs (once per HISTORY_ITEMS entry), so a launch with nothing to
        # migrate must not report counts left over from a previous one.
        self._last_merge_counts = (0, 0, None)
        self._deferral_reported = False
        self._own_history_found = False
        self._unlinkable_reported = False
        source_root = Path.home() / ".claude"
        manifest_path = session_dir / SHARE_MANIFEST
        managed = self._read_manifest(manifest_path)
        out_of_scope = [name for name in managed if name not in scope]
        self._clear_staged_links(session_dir, source_root, scope)

        # A flag turned off since last launch: remove the links we created
        # for it (never plain files/dirs the user accumulated themselves).
        # For history items that holds even when the manifest claims them:
        # a stale manifest (lock-free launches race) must never be able to
        # delete real conversation history — only ever unlink symlinks.
        for name in managed:
            if name in scope and name not in active_items:
                dest = session_dir / name
                if name in HISTORY_ITEMS and dest.exists() and not is_link(dest):
                    continue
                self._remove_managed(dest)
        if not active_items and not out_of_scope:
            manifest_path.unlink(missing_ok=True)
            return

        new_managed: list[str] = list(out_of_scope)

        for name in active_items:
            src = source_root / name
            dest = session_dir / name
            # Whether a link can be made here: always off Windows; on Windows
            # a junction for a directory (any user), a symlink for a file
            # (Developer Mode or an elevated terminal). Decided before the
            # history merge below, which must never move a profile's history
            # into a tree the profile then cannot be linked to.
            linkable = self._can_link(name, session_dir, source_root)

            # Link to the fully resolved target. When ~/.claude/<name> is itself
            # a symlink (a dotfiles setup), linking to the unresolved path makes
            # a link-to-a-link, and Claude Code's atomic settings write — which
            # resolves only one hop — renames its temp file over the intermediate
            # link, silently replacing it with a regular file
            # (anthropics/claude-code#78162).
            link_target = src.resolve() if src.is_symlink() else src

            if name in HISTORY_ITEMS and not linkable:
                # History is shared by link or not at all. A link already
                # here keeps working — only making one takes the privilege
                # — so a profile linked in an elevated terminal, or before
                # Developer Mode was turned off, stays shared.
                if is_link(dest) and link_points_at(dest, link_target):
                    new_managed.append(name)
                    continue
                self._report_history_unlinkable(name)
                continue

            if name in HISTORY_ITEMS and not self._prepare_history_share(
                src, dest, session_dir
            ):
                continue

            if not src.exists():
                # Source vanished (or never existed): prune our own entry.
                if name in managed:
                    self._remove_managed(dest)
                continue

            if is_link(dest):
                in_place = link_points_at(dest, link_target)
                ours = in_place or name in managed or link_points_at(dest, src)
                if not ours:
                    # A link cswap never made, leading somewhere else: the
                    # user's own redirect, left alone like a directory of
                    # their own — and what it leads to is never touched.
                    print(
                        dimmed(
                            f"Not sharing {name}: the session profile already "
                            "has its own link."
                        )
                    )
                    continue
                if in_place:
                    # Ours, or made by hand to the same place: adopted. Kept
                    # even where no link could be made now (Windows without
                    # the privilege): only making one takes the privilege.
                    new_managed.append(name)
                    continue
                # Ours, but not pointing where it should: at the intermediate
                # symlink of a dotfiles setup, or at a ~/.claude that moved.
                # Remade below — as a link where one can be made, else as a
                # copy.
                try:
                    remove_link(dest)
                except OSError:
                    continue
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
                if dest.exists():
                    self._remove_managed(dest)
                if not linkable:
                    self._copy_item(src, dest)
                else:
                    try:
                        self._make_link(link_target, dest)
                    except OSError as e:
                        if name in HISTORY_ITEMS:
                            raise  # history is shared by link or not at all
                        # The probe said yes and this link still failed (a
                        # target the volume will not link to, say). The copy
                        # of old, refreshed on every launch, beats nothing.
                        self._logger.warning(
                            f"Could not link {name} into the session ({e}); "
                            "copying it instead."
                        )
                        self._copy_item(src, dest)
            except OSError as e:
                self._logger.warning(f"Failed to share {name} into session: {e}")
                continue
            new_managed.append(name)

        # Anything we managed before but no longer created gets removed above;
        # write the manifest atomically so a concurrent reader never sees a
        # truncated file.
        self._write_manifest(manifest_path, new_managed)
        self._report_quarantine()
        self._report_sharing_is_the_default()

    def _clear_staged_links(
        self, session_dir: Path, source_root: Path, scope: tuple[str, ...]
    ) -> None:
        """Remove a staging link that an interrupted launch left behind.

        ``_migrate_under_lock`` makes the link that will replace a profile's
        history under a staging name first and renames it into place last.
        A launch killed in between leaves that link there, and nothing else
        looks at the staging name: the next launch links ``dest`` afresh and
        would walk past the leftover forever. It is a link and holds nothing,
        so removing it costs nothing — except that a peer launch may be in
        the middle of a migration with its own staging link live. So the
        lock every migration holds is taken here too, briefly: a busy lock
        means the "leftover" may be someone's work in progress, and it is
        left for the next launch. Nothing but a link is ever removed.
        """
        leftovers = [
            session_dir / (name + _STAGED_LINK_SUFFIX)
            for name in HISTORY_ITEMS
            if name in scope
        ]
        if not any(is_link(staged) for staged in leftovers):
            return
        try:
            _mkdir_private(source_root)
            with FileLock(
                source_root / MIGRATION_LOCK, timeout=_STAGED_CLEANUP_LOCK_TIMEOUT
            ):
                for staged in leftovers:
                    if is_link(staged):
                        remove_link(staged)
                        self._logger.info(
                            f"Removed the staging link an interrupted launch "
                            f"left at {staged}"
                        )
        except (LockError, OSError) as e:
            self._logger.warning(
                f"Could not clear a staging link in {session_dir}: {e}"
            )

    def _can_link(self, name: str, session_dir: Path, source_root: Path) -> bool:
        """Whether ``name`` can be shared into ``session_dir`` by link.

        Always off Windows. On Windows a directory takes a junction, which
        needs no privilege but a volume with reparse points and a target
        Windows accepts; a file takes a symlink, which needs Developer Mode
        or an elevated terminal. Both are probed once per process in the
        profile directory itself — the junction to ``source_root``, the
        parent of every target — so the answer holds for the link made a
        moment later in the same place. A probe is still not the link: the
        callers keep a copy fallback and a link-before-merge order for the
        case where the link itself fails.
        """
        if self.switcher.platform != Platform.WINDOWS:
            return True
        if name in _DIR_ITEMS:
            return can_junction(session_dir, source_root)
        return can_symlink(session_dir)

    @staticmethod
    def _copy_item(src: Path, dest: Path) -> None:
        """The copy of old: ``dest`` becomes a snapshot of ``src``."""
        if src.is_dir():
            shutil.copytree(src, dest)
        else:
            shutil.copy2(src, dest)

    def _make_link(self, target: Path, dest: Path) -> None:
        """Put the link for ``target`` at ``dest``: a symlink, except that a
        directory on Windows gets a junction (``fsutil.make_dir_link``)."""
        if self.switcher.platform == Platform.WINDOWS and target.is_dir():
            make_dir_link(target, dest)
        else:
            dest.symlink_to(target)

    def _report_history_unlinkable(self, name: str) -> None:
        """Say that ``name`` stays the account's own: no link can be made here.

        The common case is Windows without Developer Mode: ``projects/`` is
        shared through a junction, which needs nothing, but a file can only
        be linked with a symlink, and a copy of ``history.jsonl`` would fork
        the prompt history. Printed once per launch, as a warning when
        sharing was asked for (the flag, or the setting written by hand) and
        dimmed when only the built-in default could not be applied; the log
        gets every item.
        """
        self._logger.warning(
            f"share-history: {name} cannot be linked in this profile "
            "(Windows without a symlink privilege); the account keeps its own."
        )
        if self._unlinkable_reported:
            return
        self._unlinkable_reported = True
        asked = (
            self._share_history_asked
            or explicit_share_history(self.switcher.backup_dir) is True
        )
        line = f"share-history: {name} stays per-account on this Windows machine."
        if asked:
            warning(line)
        else:
            print(dimmed(line))
        if name in _DIR_ITEMS:
            print(
                dimmed(
                    "   A directory junction could not be made in the profile "
                    "folder (not an NTFS volume?) — see the log."
                )
            )
        else:
            print(
                dimmed(
                    "   Windows lets cswap link a file only with Developer Mode "
                    "on (Settings > System > For developers) or in an elevated "
                    "terminal. Conversations (projects/) are shared through a "
                    "junction, which needs neither."
                )
            )

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

        A merge runs under ``MIGRATION_LOCK`` together with everything it
        depends on: the path and liveness checks are read again inside the
        lock, and the link is installed before it is released (see
        ``_migrate_under_lock``). A profile with nothing to merge never
        takes it.
        """
        if dest.exists() and not is_link(dest):
            # Real per-account history: the profile kept its own while
            # sharing was off (before it became the default, or by choice).
            self._own_history_found = True
            # Merging moves files out from under any claude still running in
            # this profile, so only migrate when the profile is quiescent.
            # This first look is a cheap early out — a profile that is always
            # busy should not queue on the lock every launch for a merge that
            # cannot run. The look that counts is taken under the lock.
            if not profile_is_quiescent(session_dir):
                self._report_deferral(session_dir, dest)
                return False
            try:
                # The lock sits beside the shared history, so that directory
                # has to exist first — and with Claude Code's own mode, not
                # the default one FileLock would create it with.
                _mkdir_private(src.parent)
                with FileLock(
                    src.parent / MIGRATION_LOCK,
                    timeout=_MIGRATION_LOCK_TIMEOUT,
                ):
                    if not self._migrate_under_lock(src, dest, session_dir):
                        return False
            except LockError:
                # A peer is still merging — the timeout means "still busy",
                # not "broken", so this degrades exactly like every other
                # deferral instead of aborting the launch.
                self._report_deferral(session_dir, dest, lock_busy=True)
                return False
            except OSError as e:
                # The lock file could not be created or opened (a read-only
                # ~/.claude, say). No lock means no safe merge, and a history
                # problem must not cost the launch.
                self._logger.warning(
                    f"Could not lock {src.parent} to merge {dest.name}: {e}"
                )
                print(
                    dimmed(
                        f"Not sharing {dest.name}: the shared history could "
                        "not be locked (see log)."
                    )
                )
                return False
        return self._seed_history_source(src, dest)

    def _seed_history_source(self, src: Path, dest: Path) -> bool:
        """Create an empty share target when ``~/.claude`` has none yet."""
        if src.exists():
            return True
        # Fresh ~/.claude (or first run): seed an empty share target so
        # there is something to link.
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

    def _migrate_under_lock(
        self, src: Path, dest: Path, session_dir: Path
    ) -> bool:
        """Merge the profile's history into ``src`` and link it, as one step.

        Runs with ``MIGRATION_LOCK`` held. That lock is keyed on the shared
        destination — it is a file in it — rather than on the profile or the
        backup store, because the destination is what two migrations contend
        for: with a lock per profile (or per store), two DIFFERENT profiles
        can each find a shared path missing, and the second move lands on
        top of the first. It is its own file, so it cannot deadlock against
        ``setup_session``'s bootstrap lock — still held across this call on
        some paths, and never taken from in here.

        Nothing decided before the wait is trusted. The peer we queued
        behind may have merged this very item (``dest`` is gone: merging it
        raises FileNotFoundError and reports a peer's success as our
        failure), or linked it (``dest.is_dir()`` follows the link, so the
        walk would file every shared transcript against itself), or a claude
        may have started in the profile. Each is read again here.

        The link goes in before the lock is released, so no peer ever finds
        the profile in between — history moved out, nothing linked yet — and
        takes that for a profile with nothing to share.

        And it is made before the merge, under a staging name beside the
        history — the real kind, to the real target, in the real directory —
        and only renamed into place once the merge is done. A probe that
        said yes is not the link itself: a link that cannot be made is found
        out while the profile's history is still exactly where it was.
        """
        if not dest.exists() or is_link(dest):
            return True  # a peer finished this item; the generic loop links
        if not profile_is_quiescent(session_dir):
            self._report_deferral(session_dir, dest)
            return False
        if not self._seed_history_source(src, dest):
            print(
                dimmed(
                    f"Not sharing {dest.name}: {src} could not be created "
                    "(see log)."
                )
            )
            return False
        # The same target the generic loop links to (see its note on a
        # ~/.claude entry that is itself a symlink).
        link_target = src.resolve() if src.is_symlink() else src
        staged = dest.with_name(dest.name + _STAGED_LINK_SUFFIX)
        try:
            if is_link(staged):
                remove_link(staged)  # left by an interrupted launch
            elif staged.exists():
                raise OSError(f"{staged} exists and is not a link")
            self._make_link(link_target, staged)
        except OSError as e:
            self._logger.warning(f"Could not link {dest.name} into the session: {e}")
            print(
                dimmed(
                    f"Not sharing {dest.name}: it could not be linked here "
                    "(see log) — the account keeps its own."
                )
            )
            return False
        try:
            self._merge_history_into_source(src, dest)
        except (OSError, ValueError) as e:
            # ValueError covers UnicodeDecodeError: a byte-corrupt history
            # file is "nothing readable to merge", and losing a launch to a
            # stray byte in ~/.claude/history.jsonl would strand every
            # account that has not migrated yet.
            self._discard_staged_link(staged)
            self._logger.warning(f"Could not merge {dest.name} into {src}: {e}")
            print(
                dimmed(
                    f"Not sharing {dest.name}: merging the profile's "
                    "existing history failed (see log)."
                )
            )
            return False
        try:
            # The merge has emptied and removed ``dest``; the link slides
            # into its place by rename, which moves the link, not its target.
            replace_with_retry(staged, dest)
        except OSError as rename_error:
            # The link beside it stands, so making one is possible here; the
            # rename is what failed (a hold on the directory, say). One more
            # try, made at ``dest`` directly, before the launch goes on with
            # the merged history in ~/.claude and the profile unlinked until
            # the next launch, which links it afresh.
            try:
                self._make_link(link_target, dest)
            except OSError as e:
                self._discard_staged_link(staged)
                self._logger.warning(
                    f"Failed to share {dest.name} into session: {rename_error}; "
                    f"then {e}"
                )
                print(
                    dimmed(
                        f"Merged the profile's existing {dest.name} into {src}, "
                        "but linking it back failed (see log) — retrying on "
                        "the next launch."
                    )
                )
                return False
            self._discard_staged_link(staged)
        print(
            dimmed(
                f"Merged the profile's existing {dest.name} into "
                f"{src} — conversation history is now shared."
            )
        )
        return True

    def _discard_staged_link(self, staged: Path) -> None:
        """Best effort: a staging link that will not be used must not linger."""
        try:
            remove_link(staged)
        except OSError as e:
            self._logger.warning(f"Could not remove {staged}: {e}")

    def _report_deferral(
        self, session_dir: Path, dest: Path, lock_busy: bool = False
    ) -> None:
        """Explain a skipped migration loudly enough to be noticed.

        The old one-line dimmed print was scrolled away by Claude's startup
        banner and never logged, so a profile could sit unshared for days
        with sharing asked for on every launch and no visible signal. Printed
        once per launch: both history items defer for the same reason, and a
        second copy of the block only buries the first.

        ``lock_busy`` is the one deferral that has nothing to do with this
        profile's own sessions: a peer cswap launch held ``MIGRATION_LOCK``
        past the timeout, merging some profile's history (not necessarily
        this one's) into the same shared tree.
        """
        account = session_dir.name
        slot = account.split("-")[0]
        blockers: list[ClaudeSession] = []
        if lock_busy:
            why = "the migration lock is held by a peer process"
        else:
            blockers, _ = scan_live_sessions(session_dir)
            why = (
                f"{len(blockers)} live session(s): "
                + ", ".join(f"pid {b.pid} ({b.cwd})" for b in blockers)
                if blockers
                else "a session record could not be read"
            )
        self._logger.warning(
            f"History migration of {dest.name} deferred for {account}: {why}"
        )
        if self._deferral_reported:
            return
        self._deferral_reported = True
        warning(f"share-history: {account} keeps its own {dest.name} for now.")
        retry = self._sharing_relaunch_command(slot)
        if lock_busy:
            # Nothing to close; the only useful action is to retry once the
            # peer is done.
            print(
                dimmed(
                    "   Another cswap launch is merging history into "
                    "~/.claude right now."
                )
            )
            print(dimmed(f"   Once it finishes, relaunch:  {retry}"))
        elif blockers:
            for live in blockers:
                started = datetime.fromtimestamp(
                    live.started_at / 1000, tz=UTC
                ).astimezone()
                print(
                    dimmed(
                        f"   pid {live.pid}  {live.cwd}  "
                        f"(since {started:%Y-%m-%d %H:%M})"
                    )
                )
            print(
                dimmed(
                    "   Sessions started here will not appear in "
                    "`claude --resume` under other accounts."
                )
            )
            print(dimmed(f"   Close them, then relaunch:  {retry}"))
        else:
            # Not quiescent, yet nobody to name: a record in the profile's
            # sessions/ could not be read, and "cannot tell" is never
            # treated as "nobody there".
            print(
                dimmed(
                    "   A session record in this profile could not be read, "
                    "so it may still be in use."
                )
            )
            print(dimmed(f"   Close anything using it, then relaunch:  {retry}"))

    def _sharing_relaunch_command(self, slot: str) -> str:
        """The ``cswap run`` that shares history next time, as the user would
        type it: the flag only where their settings turn sharing off."""
        if load_run_settings(self.switcher.backup_dir).share_history:
            return f"cswap run {slot}"
        return f"cswap run {slot} --share-history"

    def _report_sharing_is_the_default(self) -> None:
        """Name the opt-out when the default reaches history a profile holds.

        Sharing no longer has to be asked for, and for a profile with history
        of its own that is not a silent change: this launch merged it into
        ``~/.claude``, or explained why it could not yet. One line, and only
        for a user who never chose — with ``run.shareHistory`` in the file
        either way, there is nothing to tell them. It goes quiet by itself:
        once the profile is linked there is no history of its own to meet.
        """
        if not self._own_history_found:
            return
        if explicit_share_history(self.switcher.backup_dir) is not None:
            return
        print(
            dimmed(
                "Chat history is shared between accounts by default now "
                "(turn off: cswap config set run.shareHistory false)."
            )
        )

    def _note_merge_progress(
        self, moved: int, quarantined: int, run_dir: Path | None
    ) -> None:
        """Add one merge's counts to this launch's total.

        HISTORY_ITEMS holds two entries (projects, history.jsonl), so a
        launch merges up to twice — accumulate rather than overwrite, and
        keep the first quarantine dir a run produces (the file-merge branch
        never quarantines, so at most one of the two sets it).
        """
        prev_moved, prev_quarantined, prev_run_dir = self._last_merge_counts
        self._last_merge_counts = (
            prev_moved + moved,
            prev_quarantined + quarantined,
            prev_run_dir if prev_run_dir is not None else run_dir,
        )

    def _report_quarantine(self) -> None:
        """Say where a merge parked the copies it did not keep.

        One line per launch, after both history items. Without it the only
        trace of a conflict is an INFO log line per file, and nothing tells
        the user a second copy of anything exists. Printed after a merge that
        failed halfway too: what it parked before failing is still parked.
        """
        _, quarantined, run_dir = self._last_merge_counts
        if not quarantined or run_dir is None:
            return
        noun = "file" if quarantined == 1 else "files"
        print(
            dimmed(
                f"Kept {quarantined} conflicting history {noun} aside in "
                f"{run_dir} — nothing was deleted."
            )
        )
        self._logger.warning(
            f"History merge kept {quarantined} conflicting {noun} in {run_dir}"
        )

    def _shared_side_sessions(self, shared_root: Path) -> tuple[set[str], bool]:
        """Ids of the sessions that appear to be writing the shared history.

        A claude on the default login writes ``shared_root``'s history
        directly, and one in a linked profile writes it through the link, so
        ``shared_root`` and every profile of THIS store are read. The flag is
        False when the answer cannot be trusted — a record that could not be
        read, or a live session that names no transcript — and the caller
        must then treat every shared file as in use: not knowing is not the
        same as knowing nothing is there.

        Best effort, not a guarantee. What is read is the ``sessionId``
        Claude Code keeps in ``sessions/<pid>.json``. It follows ``--resume``
        and ``/clear`` (read from the 2.1.319 bundle), but it is rewritten in
        the background after the switch and a failed rewrite is only logged,
        so a record read a moment ago can already be behind. Two kinds of
        writer are not seen at all: a process that never registers a record
        (a teammate or subagent process), and a session running in a profile
        of ANOTHER cswap store that shares this ``~/.claude`` (a second
        ``XDG_DATA_HOME``), whose records are kept where this store cannot
        find them. Such a session goes undetected, and a transcript it is
        writing can be replaced by a later profile copy — the copy it was
        writing is then in the quarantine, not gone.
        """
        roots = [shared_root]
        try:
            roots.extend(p for p in self.sessions_dir.iterdir() if p.is_dir())
        except FileNotFoundError:
            pass
        except OSError:
            return set(), False
        ids: set[str] = set()
        complete = True
        for root in roots:
            sessions, unreadable = scan_live_sessions(root)
            if unreadable:
                complete = False
            for live in sessions:
                if live.session_id:
                    ids.add(live.session_id)
                else:
                    complete = False
        return ids, complete

    def _shared_copy_in_use(self, shared_root: Path, rel: Path) -> bool:
        """Whether a live session appears to be writing the shared ``rel``.

        Read fresh on every call and never kept for the rest of a merge: a
        session can ``/resume`` another conversation while the merge is busy
        with other files, and a snapshot taken at the first collision would
        wave the newly live transcript through. What remains open is the
        moment between this read and the rename, and the limits of the
        records themselves (see ``_shared_side_sessions``).
        """
        live_ids, known = self._shared_side_sessions(shared_root)
        if not known:
            return True
        # ``<slug>/<id>.jsonl`` is a session's transcript and ``<slug>/<id>/``
        # holds its subagents: the id is a path component either way. Claude
        # appends to both by path, so a file swapped in under a live session
        # gets its next message.
        names = {*rel.parts[:-1], rel.name.removesuffix(".jsonl")}
        return not names.isdisjoint(live_ids)

    def _quarantine(self, path: Path, rel: Path, run_dir: Path, side: str) -> Path:
        """Move a losing copy under ``run_dir``, never over an existing file.

        ``side`` is which tree the loser came from — ``"shared"`` or ``"profile"`` —
        because both sides otherwise land at the same relative path and the operator
        reconciling two disjoint halves of one session would have no way to tell which
        half is which.
        """
        dest = run_dir / side / rel
        _mkdir_private(dest.parent)
        if dest.exists():
            counter = 2
            while True:
                candidate = dest.parent / f"{dest.stem}.{counter}{dest.suffix}"
                if not candidate.exists():
                    dest = candidate
                    break
                counter += 1
        shutil.move(str(path), str(dest))
        self._logger.info(f"Quarantined colliding history file to {dest}")
        return dest

    def _merge_history_into_source(
        self, src: Path, dest: Path
    ) -> tuple[int, int, Path | None]:
        """Move the profile's own history at ``dest`` into ``src``.

        Directories merge file-by-file, and a name collision does *not* mean
        the same content: stable names (``memory/MEMORY.md``) diverge between
        accounts, and a transcript that straddled an un-share event exists in
        both trees under one UUID. Neither side's copy is ever deleted unless
        the other side holds every byte of it:

        - The shared copy already contains the profile's (identical, or it
          continues it): the profile's is a true duplicate and is dropped.
        - A transcript whose profile copy is the later one
          (``_profile_copy_wins``) replaces the shared copy, which is
          quarantined — unless a live session appears to be writing that
          shared copy, in which case it stays where the session expects it.
          That check is best effort (``_shared_side_sessions`` says what it
          cannot see).
        - Everything else keeps the shared copy and quarantines the
          profile's. That covers every non-transcript file: nothing ties a
          ``MEMORY.md`` to one session, so it can never be shown to be idle,
          and a claude rewriting it in place would write straight over a
          copy swapped in beneath it.

        ``history.jsonl`` merges by appending lines not already present.
        ``dest`` is removed once empty; any failure — an OSError, or a
        UnicodeDecodeError on a byte-corrupt history file — propagates and
        leaves remaining files in place for the next try. Returns
        ``(moved, quarantined, run_dir)``: ``moved`` counts files relocated
        into ``src`` — a plain (non-colliding) move, or the winning half of a
        collision; ``quarantined`` counts losers parked instead of dropped. A
        collision where the profile copy wins increments *both* for the one
        file pair (the winner is moved, the incumbent is quarantined), so
        callers must not sum the two counters as "files handled". The same
        counts are added to ``_last_merge_counts`` however the walk ends,
        so a merge that raises halfway still leaves what it parked on record.
        """
        # Never merge the shared history into itself. ``is_dir()`` and
        # ``rglob`` both follow a link, so a ``dest`` that is one — or that
        # reaches ``src`` any other way — collides every shared file with
        # itself. The caller already checks under the lock; it is repeated
        # here because being wrong costs the whole shared history.
        if is_link(dest) or (src.exists() and os.path.samefile(src, dest)):
            raise OSError(f"{dest} is the shared history, not the profile's own")

        run_dir: Path | None = None
        moved = 0
        quarantined = 0

        def quarantine_root() -> Path:
            nonlocal run_dir
            if run_dir is None:
                # Stamped per merge and named for the profile: several
                # profiles migrate on the same day, and "profile" alone does
                # not say whose MEMORY.md a parked copy is.
                stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%S")
                run_dir = (
                    self.switcher.backup_dir
                    / QUARANTINE_DIRNAME
                    / f"{stamp}-{dest.parent.name}"
                )
                _mkdir_private(run_dir)
            return run_dir

        if dest.is_dir():
            _mkdir_private(src)
            try:
                for path in _history_tree(dest):
                    rel = path.relative_to(dest)
                    target = src / rel
                    if path.is_dir():
                        path.rmdir()  # children already moved (reverse walk)
                        continue
                    if target.exists():
                        if _adds_nothing(target, path):
                            path.unlink()
                            continue
                        if (
                            path.suffix == ".jsonl"
                            and target.is_file()
                            and _profile_copy_wins(target, path)
                            and not self._shared_copy_in_use(src.parent, rel)
                        ):
                            # Park the incumbent first: a crash between the
                            # two leaves the shared path empty, and the next
                            # run moves the profile copy in cleanly. Counted
                            # as parked right here, not after the move: if
                            # the move fails, this is the copy the user has
                            # to be pointed at.
                            self._quarantine(
                                target, rel, quarantine_root(), "shared"
                            )
                            quarantined += 1
                            _mkdir_private(target.parent)
                            shutil.move(str(path), str(target))
                            moved += 1
                        else:
                            self._quarantine(
                                path, rel, quarantine_root(), "profile"
                            )
                            quarantined += 1
                        continue
                    _mkdir_private(target.parent)
                    shutil.move(str(path), str(target))
                    moved += 1
            finally:
                # However the walk ends. A merge that stops halfway has
                # still parked what it parked, and the launch must be able
                # to say where.
                self._note_merge_progress(moved, quarantined, run_dir)
            dest.rmdir()
        else:
            existing_text = ""
            if src.exists():
                existing_text = src.read_text(encoding="utf-8")
            existing = set(existing_text.splitlines())
            lines = [
                line
                for line in dest.read_text(encoding="utf-8").splitlines()
                if line and line not in existing
            ]
            if lines:
                src.parent.mkdir(parents=True, exist_ok=True)
                if not src.exists():
                    src.touch(mode=0o600)  # match Claude Code's history mode
                # A history.jsonl torn by a crashed append ends mid-line, and
                # appending onto it would weld that tail to the first merged
                # entry — one line that parses as neither, with no second
                # copy to recover from once dest is unlinked below.
                torn = bool(existing_text) and not existing_text.endswith("\n")
                terminator = "\n" if torn else ""
                with src.open("a", encoding="utf-8") as f:
                    f.write(terminator + "\n".join(lines) + "\n")
            dest.unlink()
        return moved, quarantined, run_dir

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
        # ``mode`` is informational — nothing reads it back — and says how
        # files are shared: by symlink, or (Windows without the privilege)
        # by copy. Directories are links on every platform.
        if self.switcher.platform != Platform.WINDOWS:
            mode = "symlink"
        else:
            mode = "symlink" if can_symlink(manifest_path.parent) else "copy"
        payload = json.dumps({"items": items, "mode": mode}, indent=2)
        try:
            atomic_write_text(manifest_path, payload)
        except OSError:
            pass  # best effort: a failed manifest must not fail a launch

    @staticmethod
    def _remove_managed(dest: Path) -> None:
        """Remove a cswap-created share entry (link or copy), never user data
        beyond it — callers guarantee `dest` is manifest-listed or a link."""
        try:
            if is_link(dest):
                remove_link(dest)  # the link itself, never what it leads to
            elif dest.is_file():
                dest.unlink(missing_ok=True)
            elif dest.is_dir():
                shutil.rmtree(dest, ignore_errors=True)
        except OSError:
            pass
