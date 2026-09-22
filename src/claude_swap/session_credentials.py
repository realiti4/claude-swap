"""Access-token-only credentials for managed session profiles.

A managed session never holds a refresh token: the slot's backup store is
the single holder of each account's lineage (refresh tokens are single-use,
and two stores holding one lineage is the #96/#164 stale-copy failure).
Claude Code supports a credential without ``refreshToken`` as an
inference-only mode — it never refreshes, never persists, never logs out
because of it — so cswap refreshes centrally and pushes the new access
token into every session of that account through
:func:`write_session_credential`.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from claude_swap import macos_keychain, oauth
from claude_swap.claude_locks import (
    claude_json_lock_dir,
    proper_lockfile,
    session_credential_locks,
)
from claude_swap.credentials import (
    merge_shared_credential_fields,
    shared_credential_fields,
)
from claude_swap.exceptions import ClaudeCodeLockTimeout, LockError
from claude_swap.fsutil import replace_with_retry
from claude_swap.managed_sessions import (
    AccountRef,
    ManagedEntry,
    ManagedSessionRegistry,
    entry_is_live,
)
from claude_swap.models import Platform
from claude_swap.session import (
    _keychain_account_name,
    keychain_service_name,
    read_session_identity,
)
from claude_swap.settings import atomic_write_json

_logger = logging.getLogger("claude-swap")

# Allowlist, not a denylist: anything Claude grows later (a new lineage
# field) must not leak a refresh-capable secret into a managed profile.
# subscriptionType/rateLimitTier are Claude's plan metadata, read from the
# credential itself.
ACCESS_CREDENTIAL_KEYS = (
    "accessToken",
    "expiresAt",
    "scopes",
    "subscriptionType",
    "rateLimitTier",
)


def build_access_credential(credentials: str) -> str:
    """The access-token-only projection of a full Claude OAuth credential.

    Raises:
        ValueError: ``credentials`` carries no ``claudeAiOauth.accessToken``
            (malformed JSON, a managed API key, a refresh-only blob).
    """
    data = oauth.extract_oauth_data(credentials)
    token = data.get("accessToken") if data else None
    if not isinstance(token, str) or not token:
        raise ValueError("credential has no claudeAiOauth.accessToken")
    access = {
        key: data[key] for key in ACCESS_CREDENTIAL_KEYS if data.get(key) is not None
    }
    return json.dumps({"claudeAiOauth": access})


# Claude clears its token memo + keychain cache when .credentials.json's
# mtime changes. Two writes inside one timestamp tick (coarse filesystems:
# 1 s) would be invisible, so a non-advancing mtime is pushed a full second
# past the previous one — enough for any granularity, harmless otherwise.
MTIME_BUMP_NS = 1_000_000_000

# The writer reads its registry row while already holding Claude's
# .storage-write and .claude.json locks for this profile, so it must not
# sit out the registry's own 10 s wait there: Claude gives up on a held
# storage lock after a few seconds of retries. The sweep holds the registry
# lock for one `ps` per entry, well inside this.
REGISTRY_CHECK_TIMEOUT_S = 2.0


WriteReason = Literal[
    "ok",
    "keychain-write-failed",
    "invalid-credential",
    "refresh-token-refused",
    "identity-mismatch",
    "no-session-dir",
    "config-unreadable",
    "no-registry-entry",
    "session-ended",
    "lock-timeout",
    "write-failed",
    "keychain-unreadable",
    "registry-changed",
    "registry-unverifiable",
    "post-check-mismatch",
]


@dataclass(frozen=True)
class WriteResult:
    """Outcome of :func:`write_session_credential`.

    ``ok`` is the contract — True means the profile now serves the
    credential this call was given. ``reason`` is diagnostic: it names what
    happened for a log line or a message, and callers must not branch on
    it. :data:`WriteReason` lists the values so they stay checkable.

    The one thing worth knowing beyond ``ok``: a success can carry a reason
    other than ``"ok"``. The two plaintext-only outcomes,
    ``"keychain-write-failed"`` and ``"keychain-unreadable"``, are
    successes exactly when the caller asked for them with
    ``require_keychain=False``, and ``keychain_written`` is then False.
    A caller deciding what to do about a plaintext-only write reads that
    flag, not the reason.

    ``fingerprint`` names the credential this call may have left live in
    the profile: set whenever a store was written, which includes the
    failures that write before failing (``post-check-mismatch``, the two
    plaintext-only outcomes under ``require_keychain``, and
    ``write-failed`` / ``registry-unverifiable`` when a store landed
    first); None when nothing of this call's can be live. The keychain
    item is read first, so the plaintext is live whenever there is no
    readable item.
    """

    ok: bool
    reason: WriteReason
    fingerprint: str | None = None
    keychain_written: bool = False


def _read_keychain(session_dir: Path) -> str | None:
    """The profile's keychain item; None when absent.

    Raises:
        KEYCHAIN_ERRORS: The item could not be read (locked, timed out).
    """
    return macos_keychain.get_password(
        keychain_service_name(session_dir), _keychain_account_name()
    )


def _read_plaintext(session_dir: Path) -> str | None:
    try:
        return (session_dir / ".credentials.json").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None


def _read_back_stores(session_dir: Path, platform: Platform) -> tuple[str | None, str | None]:
    """``(keychain item, plaintext file)`` of a managed profile, raw.

    An unreadable keychain is indistinguishable from an absent one here:
    both read as None. ``_post_check`` compares the item only when this
    call wrote it or read one back, so neither is checked — an item that
    could not be read is reported by its own ``keychain-unreadable``
    outcome instead.
    """
    keychain: str | None = None
    if platform == Platform.MACOS:
        try:
            keychain = _read_keychain(session_dir)
        except macos_keychain.KEYCHAIN_ERRORS:
            keychain = None
    return keychain, _read_plaintext(session_dir)


def _compose(
    session_dir: Path, access_credential: str, platform: Platform
) -> tuple[str, bool]:
    """``(payload, keychain readable)``: keep the profile's own
    machine-shared fields (``mcpOAuth`` etc.).

    Claude's storage ``mutate()`` (MCP OAuth) writes them into this
    profile's store; replacing only ``claudeAiOauth`` keeps them. When the
    keychain item cannot be read, the fields come from the plaintext alone
    and the caller must not write the keychain: treating the unreadable
    item as empty would overwrite it and drop its fields.
    """
    keychain: str | None = None
    readable = True
    if platform == Platform.MACOS:
        try:
            keychain = _read_keychain(session_dir)
        except macos_keychain.KEYCHAIN_ERRORS:
            readable = False
    shared = shared_credential_fields(keychain or _read_plaintext(session_dir))
    if not shared:
        return access_credential, readable
    return merge_shared_credential_fields(access_credential, shared), readable


def _write_keychain(session_dir: Path, payload: str) -> bool:
    service = keychain_service_name(session_dir)
    account = _keychain_account_name()
    try:
        macos_keychain.set_password(service, account, payload)
        return True
    except macos_keychain.KEYCHAIN_ERRORS as e:
        # Plaintext-only from here. A surviving older item would shadow the
        # plaintext (Claude reads the keychain first), so try to clear it;
        # the post-check reports a failure if it survived.
        _logger.warning(
            f"Keychain write for managed session {session_dir.name} failed "
            f"({e}); writing the plaintext credential only."
        )
        with suppress(*macos_keychain.KEYCHAIN_ERRORS):
            macos_keychain.delete_password(service, account)
        return False


def _write_plaintext(path: Path, payload: str) -> None:
    """Atomic 0600 replace whose mtime always advances (see MTIME_BUMP_NS)."""
    try:
        before = path.stat().st_mtime_ns
    except FileNotFoundError:
        before = None
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".credentials.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        replace_with_retry(tmp, path)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    if before is not None:
        after = path.stat().st_mtime_ns
        if after // 1_000_000 <= before // 1_000_000:
            bumped = before + MTIME_BUMP_NS
            os.utime(path, ns=(bumped, bumped))


def _read_config(config_path: Path) -> dict | None:
    """The profile's ``.claude.json`` as a dict; ``{}`` when absent; None
    when present but unreadable or not an object.

    Absent and unreadable are kept apart on purpose (as
    ``credentials._update_global_config`` does): the file carries the
    session's whole Claude state (projects, mcpServers), and splicing into
    ``{}`` would replace a torn file the writer never managed to read.
    """
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return None
    return config if isinstance(config, dict) else None


def _still_assigned(current: ManagedEntry | None, account: AccountRef) -> bool:
    """Whether the row still exists and still names the account just
    written (email + organization). Every other change to a live row is
    benign — a pid/procStart restamp, ``source``, ``last_assigned_at``,
    ``last_reason``, a previous writer's caller recording its
    ``access_fingerprint`` — and taking the write back over one of those
    would only strand a session that is still this account's."""
    return current is not None and current.account == account


def _discard(session_dir: Path, payload: str, platform: Platform, keychain_written: bool) -> None:
    """Take back what this call wrote, and only that.

    Runs under Claude's storage lock, so a store still holding ``payload``
    was written by this call; anything else there is someone else's and is
    left alone. ``oauthAccount`` is not touched: it names the account, not
    a secret, and the next writer for this profile (if any) replaces it.
    """
    if platform == Platform.MACOS and keychain_written:
        service, account = keychain_service_name(session_dir), _keychain_account_name()
        with suppress(*macos_keychain.KEYCHAIN_ERRORS):
            if macos_keychain.get_password(service, account) == payload:
                macos_keychain.delete_password(service, account)
    path = session_dir / ".credentials.json"
    with suppress(OSError, ValueError):
        if path.read_text(encoding="utf-8") == payload:
            path.unlink()


def _post_check(
    session_dir: Path,
    expected: str | None,
    account: AccountRef,
    platform: Platform,
    keychain_written: bool,
) -> bool:
    keychain, plaintext = _read_back_stores(session_dir, platform)
    if plaintext is None or oauth.access_token_fingerprint(plaintext) != expected:
        return False
    if keychain_written or keychain is not None:
        # Written: must match. Not written but present: it shadows the
        # plaintext for Claude, so it must match too.
        if keychain is None or oauth.access_token_fingerprint(keychain) != expected:
            return False
    return read_session_identity(session_dir) == (account.email, account.organization_uuid)


def _write_locked(
    session_dir: Path,
    account: AccountRef,
    access_credential: str,
    oauth_account: dict,
    registry: ManagedSessionRegistry,
    platform: Platform,
    require_keychain: bool,
) -> WriteResult:
    """Steps 2-6 of :func:`write_session_credential`; every lock is held."""
    session_id = session_dir.name
    config_path = session_dir / ".claude.json"
    config = _read_config(config_path)
    if config is None:
        _logger.warning(
            f"Managed session {session_id}: {config_path} exists but could not "
            "be read; refusing to overwrite it."
        )
        return WriteResult(False, "config-unreadable")

    # Pre-check under the registry lock, right before the first write (see
    # ManagedSessionRegistry.sweep). Only the row read holds that lock; the
    # liveness check (a `ps` on macOS) runs after it is released, and the
    # lock is only ever taken while Claude's locks are already held, never
    # waited on the other way round, so it cannot deadlock with the sweep.
    try:
        stamped = registry.get_locked(session_id, timeout=REGISTRY_CHECK_TIMEOUT_S)
    except LockError:
        return WriteResult(False, "lock-timeout")
    if stamped is None:
        return WriteResult(False, "no-registry-entry")
    if not entry_is_live(stamped):
        return WriteResult(False, "session-ended")

    payload, keychain_readable = _compose(session_dir, access_credential, platform)
    plaintext_only: str | None = None
    expected = oauth.access_token_fingerprint(payload)
    keychain_written = False
    plaintext_written = False
    failure: OSError | None = None
    try:
        if platform == Platform.MACOS:
            if keychain_readable:
                keychain_written = _write_keychain(session_dir, payload)
                if not keychain_written:
                    plaintext_only = "keychain-write-failed"
            else:
                _logger.warning(
                    f"Managed session {session_id}: its keychain item could not "
                    "be read; leaving it untouched and writing the plaintext "
                    "credential only."
                )
                plaintext_only = "keychain-unreadable"
        _write_plaintext(session_dir / ".credentials.json", payload)
        plaintext_written = True
        config["oauthAccount"] = oauth_account
        atomic_write_json(config_path, config)
    except OSError as e:
        # Falls through to the registry re-check first: a sweep deleting
        # the profile mid-write is the likeliest cause, and the keychain
        # item written before the failure must not outlive the profile.
        failure = e

    written = keychain_written or plaintext_written
    try:
        current = registry.get_locked(session_id, timeout=REGISTRY_CHECK_TIMEOUT_S)
    except (LockError, OSError):
        try:
            # One retry on a fresh budget. The sweep holds the registry lock
            # for a `ps` per entry and runs immediately before the push, so
            # losing the first read to contention is ordinary.
            current = registry.get_locked(session_id, timeout=REGISTRY_CHECK_TIMEOUT_S)
        except (LockError, OSError) as e:
            # "Could not read the row" is not "the row changed": the
            # take-back below exists for a profile whose deletion makes the
            # hashed keychain name unrecoverable, and applying it to an
            # unread row would delete both stores of a healthy live session.
            # Leave them; the next pass re-reads and rewrites.
            _logger.warning(
                f"Managed session {session_id}: its registry entry could not "
                f"be re-read after writing ({e}); leaving the stores as they are."
            )
            return WriteResult(
                False, "registry-unverifiable", expected if written else None,
                keychain_written,
            )
    if not _still_assigned(current, account):
        _discard(session_dir, payload, platform, keychain_written)
        _logger.warning(
            f"Managed session {session_id}: its registry entry went away or "
            "was reassigned while writing; took the new credential back."
        )
        return WriteResult(False, "registry-changed")
    if failure is not None:
        _logger.warning(f"Managed session {session_id}: write failed ({failure})")
        # Whatever store was written is live whatever failed after it
        # (a later .claude.json splice, say); report it so the caller can
        # record it.
        return WriteResult(
            False, "write-failed", expected if written else None, keychain_written
        )
    if not _post_check(session_dir, expected, account, platform, keychain_written):
        _logger.warning(
            f"Managed session {session_id}: stores did not read back "
            "as written; leaving the session as is."
        )
        return WriteResult(False, "post-check-mismatch", expected, keychain_written)
    if plaintext_only is not None and require_keychain:
        # No item of this call's in the keychain, and the caller said one is
        # part of a successful write. Claude reads the item before the
        # plaintext, so an item that could not be read — or one that could
        # not be overwritten and could not be cleared either — may go on
        # serving an older access token. Reported as a failure so the caller
        # retries rather than recording a fingerprint the session may not be
        # serving, which would make every later pass skip it until the
        # account's token rotates again.
        return WriteResult(False, plaintext_only, expected, keychain_written)
    return WriteResult(True, plaintext_only or "ok", expected, keychain_written)


def _profile_gone(session_dir: Path, dir_fd: int) -> bool:
    """Whether the directory the writer holds open has been deleted.

    The held fd decides whenever it can be checked, and an ambiguous answer
    means "not gone", so a real write failure on an intact profile is never
    reported as a missing one. A zero link count (Linux) is proof of
    deletion, but APFS keeps reporting a deleted directory's link count
    as 2, so the fd is also compared with a strict ``stat`` of the path:
    the same inode there proves the profile intact, while a different
    inode (a recreated husk) or nothing there means it is gone. ``lexists``,
    which swallows errors, is only consulted when the fd itself cannot be
    checked.
    """
    try:
        held = os.fstat(dir_fd)
    except OSError:
        return not os.path.lexists(session_dir)
    if held.st_nlink == 0:
        return True
    try:
        at_path = os.stat(session_dir)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return not os.path.samestat(held, at_path)


def write_session_credential(
    session_dir: Path,
    account: AccountRef,
    access_credential: str,
    oauth_account: dict,
    *,
    registry: ManagedSessionRegistry,
    platform: Platform | None = None,
    lock_timeout: float | None = None,
    require_keychain: bool = True,
) -> WriteResult:
    """Install ``access_credential`` for ``account`` into a managed profile.

    In order: (1) Claude's own locks for this dir — ``.oauth_refresh``,
    ``.storage-write`` and ``.claude.json``, all taken before anything is
    written so a timeout on any of them leaves every store untouched;
    (2) pre-check: ``.claude.json`` is readable, and under the registry
    lock the entry exists and its PID is still the stamped process;
    (3) keychain item ``keychain_service_name(session_dir)`` on macOS,
    skipped when that item cannot be read (falls back to plaintext only);
    (4) atomic plaintext with a guaranteed mtime change (Claude re-reads
    both stores on the next request); (5) ``oauthAccount`` splice;
    (6) re-check the registry entry under its lock — if it went away or
    now names another account, take back what this call wrote; if it
    cannot be read at all, retry the read once and then leave the stores
    alone (``registry-unverifiable``) — then post-check both stores and
    the identity.

    A sweep that deletes the profile before the locks are taken is detected
    (the directory held open beforehand is no longer the one at the path)
    and the husk the lock helpers recreated is removed again. One that
    deletes it mid-write can still leave an empty profile directory behind
    (the lock and config writers recreate their parent); the keychain item
    is still taken back, and the orphan sweep reclaims the directory after
    ``ORPHAN_GRACE_S``.

    ``access_credential`` may be any Claude OAuth credential without a
    ``refreshToken`` key; only its ``ACCESS_CREDENTIAL_KEYS`` are written.

    ``session_dir`` must be the exact ``CLAUDE_CONFIG_DIR`` string the
    session was launched with: Claude hashes it, unresolved, into the
    keychain service name.

    ``require_keychain`` says whether a keychain item of this call's is
    part of a successful write on macOS, and it is the caller's judgement
    because the answer depends on what was there before. Refreshing an
    existing profile requires it: the item Claude reads first already holds
    the previous access token, so failing to replace it leaves the session
    serving that one, and the write has to be retried. Seeding a new
    profile does not: there is no item to shadow anything, Claude finds
    none and reads the plaintext, so a locked login keychain (an ssh or
    headless session) still yields a working profile. A permissive caller
    reads ``keychain_written`` to tell the user the token is plaintext-only.

    Never raises for expected failures; apart from the ``registry-changed``
    take-back, a failure leaves the session as is and reports the reason.
    Does not modify the registry — callers record ``fingerprint``. Seeding
    a new profile and refreshing an existing one both go through here.
    """
    # The writer itself guarantees a managed profile never holds a
    # refresh-capable secret, whatever the caller passes: any refreshToken
    # key (even empty) is refused as a caller bug, and everything else is
    # reduced to the access allowlist — no lineage keys inside
    # claudeAiOauth, no top-level siblings (trustedDeviceToken, a caller's
    # mcpOAuth). The profile's own shared fields are added back by _compose
    # from the profile's store, never from the caller.
    data = oauth.extract_oauth_data(access_credential)
    if data is not None and "refreshToken" in data:
        return WriteResult(False, "refresh-token-refused")
    try:
        access_credential = build_access_credential(access_credential)
    except ValueError:
        return WriteResult(False, "invalid-credential")
    if not isinstance(oauth_account, dict) or (
        oauth_account.get("emailAddress") != account.email
        or (oauth_account.get("organizationUuid") or "") != account.organization_uuid
    ):
        return WriteResult(False, "identity-mismatch")
    # Hold the profile directory open across lock acquisition: the lock
    # helpers mkdir their parent, so a sweep deleting the profile between
    # here and the first lock would have it silently recreated as an empty
    # husk. While this fd is open the old directory's inode cannot be
    # reused, so "same inode after the locks" proves it is the same one.
    try:
        dir_fd = os.open(session_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return WriteResult(False, "no-session-dir")
    platform = platform or Platform.detect()
    husk: os.stat_result | None = None
    try:
        with (
            session_credential_locks(session_dir, timeout=lock_timeout),
            proper_lockfile(claude_json_lock_dir(session_dir), timeout=lock_timeout),
        ):
            now = os.stat(session_dir)
            if not os.path.samestat(os.fstat(dir_fd), now):
                husk = now
                return WriteResult(False, "no-session-dir")
            return _write_locked(
                session_dir, account, access_credential, oauth_account,
                registry, platform, require_keychain,
            )
    except ClaudeCodeLockTimeout:
        return WriteResult(False, "lock-timeout")
    except OSError as e:
        if _profile_gone(session_dir, dir_fd):
            # Removed while the locks were being taken (a sweep, or another
            # writer's husk cleanup): the lock mkdir or the stat above hit a
            # missing directory. Nothing was written.
            return WriteResult(False, "no-session-dir")
        _logger.warning(f"Managed session {session_dir.name}: write failed ({e})")
        return WriteResult(False, "write-failed")
    finally:
        os.close(dir_fd)
        if husk is not None:
            # The locks are released (their dirs removed), so the husk is
            # empty unless something else wrote there meanwhile; rmdir
            # removes only an empty directory, and only the one we made.
            with suppress(OSError):
                if os.path.samestat(os.stat(session_dir), husk):
                    os.rmdir(session_dir)
