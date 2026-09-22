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
from dataclasses import dataclass
from typing import TYPE_CHECKING

from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.managed_sessions import (
    SOURCE_BACKUP,
    SOURCE_LANE0,
    SOURCE_RUN_PROFILE,
    AccountRef,
)
from claude_swap.session import (
    read_session_credentials,
    session_dir_for,
    session_identity_drifted,
)
from claude_swap.session_credentials import build_access_credential

if TYPE_CHECKING:
    from claude_swap.switcher import ClaudeAccountSwitcher

# What a store or lock read can raise on the way: cswap's own errors, and
# plain I/O failures from files read without a guard (a stored config, a
# profile). UnicodeDecodeError is a byte-corrupt file read as text.
_STORE_FAILURES = (ClaudeSwitchError, OSError, UnicodeDecodeError)


def slot_for_account(switcher: ClaudeAccountSwitcher, account: AccountRef) -> str | None:
    data = switcher._get_sequence_data() or {}
    return switcher._find_account_slot(data, account.email, account.organization_uuid)


def _account_config(switcher: ClaudeAccountSwitcher, number: str, email: str) -> dict:
    """A slot's stored ``.claude.json`` snapshot; ``{}`` when missing or unusable."""
    text = switcher.read_account_config(number, email)
    try:
        config = json.loads(text) if text else {}
    except json.JSONDecodeError:
        return {}
    return config if isinstance(config, dict) else {}


def _oauth_account_for(switcher: ClaudeAccountSwitcher, number: str, email: str) -> dict | None:
    oauth_account = _account_config(switcher, number, email).get("oauthAccount")
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
) -> AccessResolution:
    """The access-only credential ``account``'s managed sessions should hold now."""
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
