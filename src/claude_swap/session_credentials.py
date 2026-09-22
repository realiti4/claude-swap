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
    "keychain-not-cleared",
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
    successes — the profile's keychain item was removed, so the plaintext
    this call wrote is the credential Claude reads — and
    ``keychain_written`` is then False. A caller deciding what to tell the
    user about a plaintext-only write reads that flag, not the reason.
    ``"keychain-not-cleared"`` is the failing half of that pair: an item
    that could neither be replaced nor removed goes on shadowing the
    plaintext, so the session keeps serving whatever that item holds and
    the caller has to retry. What this call wrote under that item is put
    back to what it was — see :func:`write_session_credential` on why the
    clear comes second, and why a newer plaintext must not be left there
    naming one account under an item serving another.

    ``fingerprint`` names the credential this call may have left live in
    the profile: set whenever a store was written, which includes the
    failures that write before failing (``post-check-mismatch``,
    ``keychain-not-cleared``, and ``write-failed`` /
    ``registry-unverifiable`` when a store landed first); None when
    nothing of this call's can be live. The keychain item is read first,
    so the plaintext is live whenever there is no readable item.
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


def _restore_plaintext(session_dir: Path, before: str | None) -> bool:
    """Put ``.credentials.json`` back the way it was, for a write whose
    keychain item survived to shadow it. True when the profile is back to
    what it held; False when it still holds this call's credential.

    ``before`` of None means there was no readable file to begin with —
    either absent, or unreadable, which cannot be told apart here and is
    treated the same way: the file this call created goes, and a file that
    was there but unreadable is replaced by nothing rather than by a guess.
    Best effort by construction: nothing here can raise into a write that
    has already failed for another reason.
    """
    path = session_dir / ".credentials.json"
    try:
        if before is None:
            path.unlink(missing_ok=True)
        else:
            _write_plaintext(path, before)
    except OSError as e:
        _logger.warning(
            f"Managed session {session_dir.name}: an item that could not be "
            f"cleared still shadows the credential, and the one written under "
            f"it could not be taken back ({e})."
        )
        return False
    return True


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
            try:
                # One retry on a fresh budget, as the registry re-read
                # below gets. A `security` spawn that times out says
                # nothing about the item, and the verdict here is not
                # cheap to be wrong about: "unreadable" makes the write
                # DELETE the item, taking the profile's own mcpOAuth
                # grants with it. One transient timeout must not cost
                # those.
                keychain = _read_keychain(session_dir)
            except macos_keychain.KEYCHAIN_ERRORS:
                readable = False
    shared = shared_credential_fields(keychain or _read_plaintext(session_dir))
    if not shared:
        return access_credential, readable
    return merge_shared_credential_fields(access_credential, shared), readable


def _write_keychain(session_dir: Path, payload: str) -> bool:
    try:
        macos_keychain.set_password(
            keychain_service_name(session_dir), _keychain_account_name(), payload
        )
        return True
    except macos_keychain.KEYCHAIN_ERRORS as e:
        _logger.warning(
            f"Keychain write for managed session {session_dir.name} failed "
            f"({e}); writing the plaintext credential only."
        )
        return False


def _clear_keychain(session_dir: Path) -> bool:
    """Remove the profile's keychain item, so Claude reads the plaintext.

    The degrade for a login keychain this process cannot use: Claude reads
    the item BEFORE ``.credentials.json``, so an item left behind would go
    on serving whatever token it already holds. Deleting it is what makes
    the plaintext written next to it authoritative — a locked or
    unreadable keychain then costs the profile its stored ``mcpOAuth``
    (which could not be read anyway) instead of costing the session its
    ability to work at all.

    True when the item is gone, deletion and "there was none" alike (the
    wrapper folds ``errSecItemNotFound`` into success). False only when
    the item may still be there, which is the one genuine failure.
    """
    try:
        macos_keychain.delete_password(
            keychain_service_name(session_dir), _keychain_account_name()
        )
        return True
    except macos_keychain.KEYCHAIN_ERRORS as e:
        _logger.warning(
            f"Managed session {session_dir.name}: its keychain item could "
            f"neither be replaced nor removed ({e}); it would go on serving "
            "an older token, so nothing was written."
        )
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
    not_cleared = False
    failure: OSError | None = None
    try:
        if platform == Platform.MACOS:
            if keychain_readable:
                keychain_written = _write_keychain(session_dir, payload)
                if not keychain_written:
                    plaintext_only = "keychain-write-failed"
            else:
                # An item this process cannot READ it almost certainly
                # cannot write either, and a write that fails leaves the
                # old token in place still shadowing the plaintext. So the
                # item is not written -- it is deleted, which is the only
                # way to stop it shadowing. That costs the profile the
                # mcpOAuth the item held, which is the same thing writing
                # over it would have cost and is what `_clear_keychain`'s
                # own docstring says; the alternative is a session Claude
                # cannot log in to at all.
                _logger.warning(
                    f"Managed session {session_id}: its keychain item could not "
                    "be read; removing it and writing the plaintext credential "
                    "only."
                )
                plaintext_only = "keychain-unreadable"
        # The order is plaintext, then the item, then the config, and each
        # step only runs once the one before it has landed. Clearing before
        # the plaintext would leave a profile whose write then failed with
        # no item, a stale plaintext and the item's mcpOAuth gone — strictly
        # weaker than not having run. Splicing the config before the item is
        # gone would be worse still: a surviving item goes on serving the
        # account this call is moving away FROM, while .claude.json would
        # already name the new one, so Claude would read one account's token
        # under another's name. This way the worst intermediate state is the
        # one this call started from: a write that cannot finish puts the
        # plaintext back the way it found it.
        before = _read_plaintext(session_dir)
        _write_plaintext(session_dir / ".credentials.json", payload)
        plaintext_written = True
        if plaintext_only is not None:
            not_cleared = not _clear_keychain(session_dir)
        if not_cleared:
            # The item survived and goes on shadowing the plaintext, so what
            # this call wrote underneath it is never read — until the item is
            # replaced, cleared, or the profile is moved to a keychain-less
            # platform, any of which would hand Claude a token for a DIFFERENT
            # account than the one .claude.json names (the splice below is
            # skipped for exactly that reason). Putting the old bytes back
            # costs nothing while the item shadows them and leaves the profile
            # as internally consistent as it was before the call.
            plaintext_written = not _restore_plaintext(session_dir, before)
        else:
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
    if not_cleared:
        # Ahead of the post-check, which would report the surviving item
        # as a plain mismatch (or, when it cannot be read back either, not
        # notice it at all) and lose the one detail worth acting on: the
        # item is what the session is still serving from. Its account is
        # also still the one .claude.json names, the splice having been
        # skipped and the plaintext put back, so the profile is left as
        # Claude already read it and the caller can simply try again. The
        # fingerprint rides along only when the restore itself failed,
        # which is the one case where this call's credential is still down
        # there under the item.
        return WriteResult(
            False, "keychain-not-cleared",
            expected if plaintext_written else None, keychain_written,
        )
    if not _post_check(session_dir, expected, account, platform, keychain_written):
        _logger.warning(
            f"Managed session {session_id}: stores did not read back "
            "as written; leaving the session as is."
        )
        return WriteResult(False, "post-check-mismatch", expected, keychain_written)
    # A plaintext-only outcome is a success: getting this far means the item
    # that would have shadowed this write was cleared (a clear that failed
    # returns above), so the plaintext is what Claude reads.
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
) -> WriteResult:
    """Install ``access_credential`` for ``account`` into a managed profile.

    In order: (1) Claude's own locks for this dir — ``.oauth_refresh``,
    ``.storage-write`` and ``.claude.json``, all taken before anything is
    written so a timeout on any of them leaves every store untouched;
    (2) pre-check: ``.claude.json`` is readable, and under the registry
    lock the entry exists and its PID is still the stamped process;
    (3) keychain item ``keychain_service_name(session_dir)`` on macOS;
    (4) atomic plaintext with a guaranteed mtime change (Claude re-reads
    both stores on the next request), and then — for an item that could
    not be written, or could not be read and so must not be written over
    — a DELETE of that item, so Claude falls through to the plaintext
    just written (see :func:`_clear_keychain`); a deletion that fails is
    the one keychain outcome that fails the whole write. The plaintext
    leads deliberately: clearing first and then failing to write would
    leave the profile with no item, a stale plaintext and the item's
    ``mcpOAuth`` gone, which is worse than the call never having run,
    while this order's worst intermediate state is the starting one plus
    a newer plaintext the surviving item still shadows;
    (5) ``oauthAccount`` splice, but only once (4) has left the profile
    with nothing shadowing the plaintext: a ``keychain-not-cleared`` write
    leaves ``.claude.json`` naming the account it named before the call,
    which is the account the surviving item still serves;
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

    A keychain this process cannot use is therefore a degrade, not a hard
    failure: the profile ends up plaintext-only (``keychain_written`` is
    False, and the reason names which way it got there) and goes on
    working. That matters most for the per-prompt hook, which would
    otherwise re-fail on a locked login keychain — an ssh or headless
    session — on every prompt with no way out.

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
                registry, platform,
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
