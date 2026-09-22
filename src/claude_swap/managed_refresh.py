"""Where a managed session's access token comes from, and pushing it.

An account in use by managed sessions has exactly one lineage holder, and
the access token is always copied FROM that holder, never refreshed
elsewhere:

- the default login (lane 0) when the account is currently active there —
  Claude Code refreshes it; we copy its current access token read-only;
- a live ``cswap run N`` profile for the account — same, that Claude owns it;
- otherwise the slot's backup store, refreshed through
  ``consume_backup_grant`` (per-slot consume lock + fingerprint CAS) once
  it is within the freshen buffer.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from claude_swap import oauth
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.managed_sessions import (
    SOURCE_BACKUP,
    SOURCE_LANE0,
    SOURCE_RUN_PROFILE,
    AccountRef,
    ManagedEntry,
    ManagedSessionRegistry,
)
from claude_swap.session import (
    read_session_credentials,
    session_dir_for,
    session_identity_drifted,
)
from claude_swap.session_credentials import (
    WriteResult,
    build_access_credential,
    write_session_credential,
)

if TYPE_CHECKING:
    from claude_swap.switcher import ClaudeAccountSwitcher

_logger = logging.getLogger("claude-swap")

# What a store or lock read can raise on the way: cswap's own errors, and
# plain I/O failures from files read without a guard (a stored config, a
# profile). UnicodeDecodeError is a byte-corrupt file read as text.
_STORE_FAILURES = (ClaudeSwitchError, OSError, UnicodeDecodeError)


def slot_for_account(switcher: ClaudeAccountSwitcher, account: AccountRef) -> str | None:
    data = switcher._get_sequence_data() or {}
    return switcher._find_account_slot(data, account.email, account.organization_uuid)


def busy_by_slot(
    switcher: ClaudeAccountSwitcher, counts: Mapping[AccountRef, int]
) -> dict[str, int]:
    """``ManagedSessionRegistry.busy_counts`` re-keyed from account identity
    to slot number, for the balance policy's ``busy_sessions``.

    The registry counts by ``AccountRef`` because that is what a managed
    session row carries; both callers of the balance policy — the launch
    path's placement and the engine's lane-0 ranking — want the same counts
    per slot. They used to arrive there by opposite routes (the engine
    looking each account's slot up, the launch path walking its candidate
    identities), which is two places for the same mapping to drift. One
    roster read serves the whole mapping here, so it is also one read
    rather than one per account. Accounts no slot holds any more are
    dropped, and a slot with no busy session is simply absent — both
    callers' ``busy`` reads default to 0.

    Raises:
        ClaudeSwitchError/OSError/UnicodeDecodeError: The roster could not
            be read. Nothing is known about any account's slot then, so the
            caller decides what an uncounted load costs it.
    """
    data = switcher._get_sequence_data() or {}
    by_slot: dict[str, int] = {}
    for account, count in counts.items():
        number = switcher._find_account_slot(
            data, account.email, account.organization_uuid
        )
        if number is not None:
            by_slot[number] = by_slot.get(number, 0) + count
    return by_slot


def account_config(switcher: ClaudeAccountSwitcher, number: str, email: str) -> dict:
    """A slot's stored ``.claude.json`` snapshot; ``{}`` when missing or unusable."""
    text = switcher.read_account_config(number, email)
    try:
        config = json.loads(text) if text else {}
    except json.JSONDecodeError:
        return {}
    return config if isinstance(config, dict) else {}


def _oauth_account_for(switcher: ClaudeAccountSwitcher, number: str, email: str) -> dict | None:
    oauth_account = account_config(switcher, number, email).get("oauthAccount")
    if isinstance(oauth_account, dict) and oauth_account.get("emailAddress"):
        return oauth_account
    return None


def _config_names_account(oauth_account: dict, account: AccountRef) -> bool:
    """Whether a slot's stored ``oauthAccount`` names ``account`` itself.

    The predicate is ``write_session_credential``'s own. A snapshot naming
    a different address or organization — a blank-uuid row from an older
    version, a re-add, an org move — would otherwise resolve as usable and
    then be refused by the writer with ``identity-mismatch`` on every push,
    forever, and near-silently, since repeated push failures are demoted to
    debug. Refusing it here instead lets the status name the stored config
    as the file to repair.
    """
    return oauth_account.get("emailAddress") == account.email and (
        oauth_account.get("organizationUuid") or ""
    ) == account.organization_uuid


@dataclass(frozen=True)
class AccessResolution:
    """What ``account``'s managed sessions should hold now, and why.

    ``status`` is ``"ok"`` (``credential`` and ``oauth_account`` set) or a
    refusal:

    - ``"account-gone"`` — no slot holds the account any more
    - ``"no-config"`` — the slot has no stored ``oauthAccount`` to splice
    - ``"stored-config-mismatch"`` — the slot's stored ``.claude.json``
      snapshot names a different address or organization than the slot
      itself does, so the writer would refuse every push with
      ``identity-mismatch``; the stored config is the thing to repair
      (re-add the account), and no retry can do it
    - ``"unavailable"`` — the lane-0 / run-profile holder has nothing to
      copy: an API key, no stored credential, or a run profile an in-session
      ``/login`` re-pointed at another account (its token is not this
      account's). Not an identity conflict: the slot's own lineage is fine,
      so nothing about the slot may be quarantined over it
    - ``"no-backup"`` — the slot's backup store holds no credential; not
      transient, because retrying cannot produce one — the account has to
      be re-added (or become the lane-0 / run-profile holder)
    - ``"invalid_grant"`` — the backup lineage is dead
    - ``"no-access-token"`` — even after a refresh (a missing access token
      counts as expired) the backup's OAuth data has no access token to
      copy; its refresh token may be alive, so not a dead lineage
    - ``"identity-conflict"`` — the backup grant authenticates as a
      different account
    - ``"store-unmirrored"`` / ``"invalid_client"`` / ``"stash-unreadable"`` /
      ``"consume-busy"`` — systemic refusals from the consume gate, named
      as the auto-switch engine names them
    - ``"transient"`` — network, lock or store-read trouble (a lane-0
      keychain that cannot be read included); try again later

    Store and lock failures (``ClaudeSwitchError``, ``OSError``, a
    byte-corrupt file) never raise out of :func:`resolve_access_credential`;
    they resolve to ``"transient"``.
    """

    account: AccountRef
    number: str | None
    status: str
    source: str = ""
    credential: str | None = None      # access-only JSON
    oauth_account: dict | None = None


def _resolve_from_backup(
    switcher: ClaudeAccountSwitcher,
    account: AccountRef,
    number: str,
    oauth_account: dict,
    *,
    now_ms: float,
    buffer_ms: int,
) -> AccessResolution:
    try:
        status, creds = switcher.freshen_backup_credential(
            number, account.email, now_ms=now_ms, buffer_ms=buffer_ms
        )
    except _STORE_FAILURES:
        # A store read or the consume gate failed outright (locked keychain,
        # lock timeout). Nothing was decided about the lineage.
        status, creds = "transient", None
    if status == "ok" and creds is None:
        # The helper promises credentials with "ok"; if that ever breaks,
        # serve nothing and retry rather than report success without a token.
        status = "transient"
    if status != "ok":
        return AccessResolution(account, number, status, SOURCE_BACKUP)
    try:
        access = build_access_credential(creds)
    except ValueError:
        # OAuth data without an access token. The refresh token may still be
        # alive, so this is not invalid_grant (which quarantines).
        return AccessResolution(account, number, "no-access-token", SOURCE_BACKUP)
    return AccessResolution(account, number, "ok", SOURCE_BACKUP, access, oauth_account)


def resolve_access_credential(
    switcher: ClaudeAccountSwitcher,
    account: AccountRef,
    *,
    now_ms: float,
    buffer_ms: int,
    number: str | None = None,
    number_known: bool = False,
) -> AccessResolution:
    """The access-only credential ``account``'s managed sessions should hold now.

    ``number``/``number_known`` let a caller that already looked up the
    account's slot (push_refresh, ahead of its ``skip_numbers`` check)
    pass that result straight through instead of this repeating the same
    sequence-data lookup. ``number_known=True`` must be paired with the
    looked-up value, a genuine ``None`` included — a caller that found no
    slot for the account reports that as ``number=None,
    number_known=True``, so this trusts it as "account-gone" rather than
    re-deriving the same answer with a second lookup. The default
    ``number_known=False`` means "not looked up yet" (``number`` is then
    ignored) and triggers the lookup here, exactly as before a caller
    could pass either one through.
    """
    if not number_known:
        try:
            number = slot_for_account(switcher, account)
        except _STORE_FAILURES:
            return AccessResolution(account, None, "transient")
    if number is None:
        return AccessResolution(account, None, "account-gone")
    try:
        return _resolve_for_slot(
            switcher, account, number, now_ms=now_ms, buffer_ms=buffer_ms
        )
    except _STORE_FAILURES:
        # A store or lock read failed (locked keychain, lock timeout, an
        # unreadable file); nothing was learned about the account.
        return AccessResolution(account, number, "transient")


def _resolve_for_slot(
    switcher: ClaudeAccountSwitcher,
    account: AccountRef,
    number: str,
    *,
    now_ms: float,
    buffer_ms: int,
) -> AccessResolution:
    oauth_account = _oauth_account_for(switcher, number, account.email)
    if oauth_account is None:
        return AccessResolution(account, number, "no-config")
    if not _config_names_account(oauth_account, account):
        return AccessResolution(account, number, "stored-config-mismatch")
    if switcher.current_account_number() == number:
        source = SOURCE_LANE0
        active = switcher._read_active_credentials()
        if active.value is None or active.keychain_unavailable:
            # Read error, or a keychain that could not be read with nothing
            # covering it: the store may hold a token we cannot see.
            return AccessResolution(account, number, "transient", source)
        # A degraded read (keychain failed, plaintext covered it) may be a
        # superseded generation, but its access token is only copied, never
        # its refresh token consumed — the use ActiveCredentials allows.
        raw = active.value
    elif switcher.live_session_pids_for(number, account.email):
        source = SOURCE_RUN_PROFILE
        session_dir = session_dir_for(switcher.backup_dir, number, account.email)
        if session_identity_drifted(
            session_dir, account.email, account.organization_uuid
        ):
            # An in-session /login re-pointed the profile at another account.
            return AccessResolution(account, number, "unavailable", source)
        raw = read_session_credentials(session_dir)
    else:
        return _resolve_from_backup(
            switcher, account, number, oauth_account, now_ms=now_ms, buffer_ms=buffer_ms
        )
    # Lane 0 and a live run profile belong to the Claude running there, which
    # rotates the grant itself: never refresh here and never fall back to the
    # backup, whose copy of that grant consuming would fork the token family.
    # build_access_credential also rejects a managed API key (not JSON).
    try:
        access = build_access_credential(raw or "")
    except ValueError:
        return AccessResolution(account, number, "unavailable", source)
    return AccessResolution(account, number, "ok", source, access, oauth_account)


# AccessResolution statuses whose lineage another pass cannot fix by itself:
# the backup grant is dead (invalid_grant) or now authenticates as a
# different account (identity-conflict). Every other non-ok status —
# a missing config, an account no slot holds any more, a lane-0/run-profile
# holder with nothing to copy, a transient store or lock failure, any of the
# systemic consume-gate refusals — is left for a later pass instead: retrying
# can still produce a usable token, or nothing about this account's own
# lineage is known to be broken.
QUARANTINE_STATUSES = frozenset({"invalid_grant", "identity-conflict"})

# Per-lock budget for a pushed write. :func:`push_refresh` walks the live
# sessions serially inside one auto-switch tick, and each write takes three
# of Claude's locks in sequence, so the default 9 s budget would let a
# single wedged profile hold the tick — lane-0 rate-limit switching
# included — for 27 s, and N of them for N times that. A push has nobody
# waiting on it and repeats every tick, so it gives up early and retries.
# The interactive launch path keeps the generous default: a person who
# asked for a session would rather wait than be told to try again.
PUSH_LOCK_TIMEOUT_S = 3.0


@dataclass(frozen=True)
class PushResult:
    """The outcome of one account's push-refresh pass.

    ``status`` is ``"skipped"`` (the account's slot number is in
    ``skip_numbers`` — already quarantined, so this pass does not touch it)
    or any :class:`AccessResolution` status (``"ok"`` on success).

    ``quarantine`` is the engine's cue to act on this account now rather
    than just wait for the next pass: True exactly when ``status`` is in
    :data:`QUARANTINE_STATUSES`, False for ``"ok"``, ``"skipped"``, and
    every other refusal (see :data:`QUARANTINE_STATUSES` for why those are
    left alone).

    ``written``/``failed`` are only meaningful when ``status == "ok"``: the
    managed session ids whose stored token differed from the resolved one
    and were pushed successfully, and those a push was attempted for but
    either the writer refused it or its token landed while the registry
    update recording it did not — the latter is still reported failed, so
    a stuck lock or a rewrite error retries on the next pass instead of the
    registry silently drifting from what the session actually holds.
    """

    account: AccountRef
    number: str | None
    status: str
    source: str = ""
    written: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    quarantine: bool = False


def push_refresh(
    switcher: ClaudeAccountSwitcher,
    registry: ManagedSessionRegistry,
    *,
    now_ms: float,
    buffer_ms: int,
    skip_numbers: Collection[str] = (),
    writer: Callable[..., WriteResult] = write_session_credential,
    entries: Iterable[ManagedEntry] | None = None,
) -> list[PushResult]:
    """Bring every live managed session's token up to its holder's current one.

    One resolution (and at most one grant consumption) per account; only
    sessions whose recorded fingerprint differs are written, so a steady
    state costs a credential read per account and no writes. Each write
    waits at most :data:`PUSH_LOCK_TIMEOUT_S` per lock, so the sessions
    this walks serially cannot hold the caller's tick open.

    ``entries`` is the already-known live set (e.g. a prior
    :meth:`ManagedSessionRegistry.sweep`'s ``.live``); passing it means this
    call performs no liveness check of its own. When omitted, it defaults to
    ``registry.live_entries()`` — a fresh liveness pass — for callers (tests,
    one-off tooling) that have not already done one.
    """
    live = list(registry.live_entries() if entries is None else entries)
    grouped: dict[AccountRef, list[ManagedEntry]] = {}
    for entry in live:
        grouped.setdefault(entry.account, []).append(entry)
    results: list[PushResult] = []
    for account, group in grouped.items():
        try:
            number = slot_for_account(switcher, account)
            number_known = True
        except _STORE_FAILURES:
            # Unreadable sequence data for this account only. number_known
            # stays False (rather than treating this as a known "no slot"),
            # so resolve_access_credential retries the same lookup itself
            # and reports it as "transient" — an unresolved store failure,
            # not "account-gone" — instead of this call aborting every
            # other account's push over it.
            number = None
            number_known = False
        if number is not None and number in skip_numbers:
            results.append(PushResult(account, number, "skipped"))
            continue
        res = resolve_access_credential(
            switcher, account, now_ms=now_ms, buffer_ms=buffer_ms,
            number=number, number_known=number_known,
        )
        if res.status != "ok" or res.credential is None or res.oauth_account is None:
            results.append(PushResult(
                account, res.number, res.status, res.source,
                quarantine=res.status in QUARANTINE_STATUSES,
            ))
            continue
        fingerprint = oauth.access_token_fingerprint(res.credential)
        written: list[str] = []
        failed: list[str] = []
        for entry in group:
            if entry.access_fingerprint == fingerprint:
                continue
            outcome = writer(
                registry.session_dir(entry.session_id), account, res.credential,
                res.oauth_account, registry=registry,
                lock_timeout=PUSH_LOCK_TIMEOUT_S,
                # These profiles already hold an access token, and Claude
                # reads the keychain item before the plaintext: an item this
                # push could not replace goes on serving the old token, so
                # it is a failure to retry, not a plaintext-only success.
                require_keychain=True,
            )
            # A WriteResult can be ok=True with a reason other than "ok"
            # (the plaintext-only success): that is still a successful
            # push, so callers branch on .ok, never on the reason string.
            if not outcome.ok:
                _logger.warning(
                    f"Managed session {entry.session_id}: token push failed "
                    f"({outcome.reason})"
                )
                failed.append(entry.session_id)
                continue
            try:
                registry.update(
                    entry.session_id,
                    access_fingerprint=outcome.fingerprint,
                    source=res.source,
                )
            except _STORE_FAILURES as e:
                # The token is already live in the session; only recording
                # it in the registry failed (a lock timeout, a rewrite
                # error). Reported as failed, not written, so the next pass
                # retries it instead of the registry silently drifting from
                # what the session actually holds — comparing against the
                # still-unrecorded old fingerprint makes that retry a
                # harmless repeat of this same write, not a skip.
                #
                # update() can also raise ValueError, from its own row
                # validation, but never for this call: source is always
                # one of the SOURCE_* constants, and the entry's account
                # and pid are untouched fields already valid on its own
                # row. Not caught here — that would be a caller bug, not a
                # store or lock failure worth retrying.
                _logger.warning(
                    f"Managed session {entry.session_id}: pushed a new token "
                    f"but could not record it in the registry ({e})"
                )
                failed.append(entry.session_id)
                continue
            written.append(entry.session_id)
        results.append(PushResult(
            account, res.number, "ok", res.source, tuple(written), tuple(failed)
        ))
    return results
