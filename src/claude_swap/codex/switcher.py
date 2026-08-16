"""CodexSwitcher — the Codex provider's verbs.

Small on purpose. It implements ``providers.base.ProviderSwitcher`` and nothing
else; the Claude switcher's provenance machinery has no analogue here and is not
imitated.

Two rules carry this module, and both exist because the codex CLI owns
``~/.codex/auth.json`` and writes to it whenever it likes:

1. **The live file decides who is active.** ``current_account_number`` reads the
   live file's identity and matches it to a slot. cswap's own ``activeAccountKey``
   records intent only. A session left open on account A can rewrite the live
   file minutes after a switch to B; a registry-derived answer would report B
   while every codex command runs as A.

2. **The active account is never refreshed from its snapshot.** The codex CLI
   holds the same refresh token and keeps the live file current. If the server
   rotates refresh tokens, two parties refreshing the same one means one of them
   is logged out. So: active account reads the live payload; inactive accounts
   refresh from their snapshots, and a rotated token is written back
   immediately, before it is used for anything else.

Both of those writes happen under ``FileLock`` on the Codex store: a TUI refresh
and a CLI switch in another terminal are concurrent by construction, and an
interleaved capture/write would put one account's tokens in another's slot. The
lock is the Codex store's own, so it never contends with a Claude switch.

Usage goes through ``CodexUsageCache``, which is this provider's adapter onto
the same ``UsageStore`` + ``poll_policy`` machinery the Claude side uses: serve
TTL, cross-process fetch leases, failure backoff, 429 handling and an adaptive
cadence. Two consecutive ``cswap codex list`` calls therefore cost one round of
requests, not two.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from claude_swap.codex import paths as cpaths
from claude_swap.codex.auth_file import (
    CodexIdentity,
    access_token_expiry,
    parse_identity,
    read_live_payload,
    write_live_auth,
)
from claude_swap.codex.oauth import needs_refresh, try_refresh
from claude_swap.codex.processes import running_codex_pids
from claude_swap.codex.store import CodexSlot, CodexStore
from claude_swap.codex.usage_cache import CodexUsageCache
from claude_swap.codex.workspaces import refresh_workspace_names
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.locking import FileLock, LockError
from claude_swap.models import AccountSnapshot, AccountsSnapshot, normalize_alias
from claude_swap.printer import dimmed, warning
from claude_swap.usage_store import UsageEntry

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CodexSwitchResult:
    """What a switch did, including anything the user must act on."""

    number: str
    email: str
    #: PIDs of codex sessions running at switch time. Non-empty means the user
    #: must restart them before the new account takes effect.
    running_pids: list[int] = field(default_factory=list)


class CodexSwitcher:
    """Multi-account switcher for the Codex CLI."""

    provider_id = "codex"

    def __init__(self, debug: bool = False) -> None:
        self._store = CodexStore()
        self._cache = CodexUsageCache(self._store)

    def _lock(self, timeout: float = 10.0) -> FileLock:
        """The Codex store's cross-process lock.

        Its own file, never the Claude one: the two switches touch disjoint
        state and must not be able to block each other.
        """
        return FileLock(cpaths.get_codex_lock_path(), timeout=timeout)

    # ---- identity ------------------------------------------------------

    def _live_identity(self) -> CodexIdentity | None:
        payload = read_live_payload()
        return parse_identity(payload) if payload is not None else None

    def current_account_number(self) -> str | None:
        """The slot the *live file* currently holds, or None if unmanaged."""
        ident = self._live_identity()
        if ident is None or not ident.is_identifiable:
            return None
        slot = self._store.slot_for_key(ident.account_key)
        return slot.number if slot else None

    def resolve_account(self, identifier: str) -> tuple[str, str, str]:
        """Resolve a slot number, email or alias to ``(number, email, label)``."""
        needle = (identifier or "").strip().lower()
        if needle:
            for slot in self._store.slots():
                candidates = {slot.number, slot.email.lower()}
                if slot.alias:
                    candidates.add(slot.alias.lower())
                if needle in candidates:
                    return slot.number, slot.email, slot.display_label
        raise ClaudeSwitchError(f"No Codex account matches '{identifier}'")

    def _slot_or_raise(self, identifier: str) -> CodexSlot:
        number, _email, _label = self.resolve_account(identifier)
        slot = next((s for s in self._store.slots() if s.number == number), None)
        if slot is None:  # pragma: no cover - resolve_account already raised
            raise ClaudeSwitchError(f"No Codex account '{identifier}'")
        return slot

    # ---- credentials ---------------------------------------------------

    def _capture_live(self) -> None:
        """Store the live login into the slot its own identity matches.

        Matching on the live file's identity rather than on the registry's idea
        of the active slot is what makes this repair a clobber instead of
        committing one.
        """
        payload = read_live_payload()
        if payload is None:
            return
        ident = parse_identity(payload)
        if ident is None or not ident.is_identifiable:
            return
        slot = self._store.slot_for_key(ident.account_key)
        if slot is None:
            return  # an unmanaged login: not ours to store
        self._store.write_snapshot(slot.account_key, payload)

    def _payload_for_usage(self, slot: CodexSlot, active_number: str | None) -> dict | None:
        """The payload to read this slot's usage from, refreshed if needed.

        The active slot is read from the live file and never refreshed here —
        see this module's docstring.
        """
        if slot.number == active_number:
            return read_live_payload()

        payload = self._store.read_snapshot(slot.account_key)
        if payload is None or slot.auth_mode == "apikey":
            return payload

        if not needs_refresh(payload):
            return payload

        # The lock is taken BEFORE the refresh, not around the persist alone.
        # If the server rotates the refresh token, the old one may die the
        # instant the response is issued — so a refresh we cannot persist is a
        # refresh we must not perform. Refusing to refresh costs a stale usage
        # row; refreshing without persisting can cost the account.
        try:
            with self._lock(timeout=15.0):
                outcome = try_refresh(payload)
                if outcome.payload is None:
                    _logger.debug("codex slot %s refresh: %s", slot.number, outcome.kind)
                    return payload
                self._store.write_snapshot(slot.account_key, outcome.payload)
                return outcome.payload
        except LockError:
            # Another cswap process holds the store. Skip this account's refresh
            # this pass; the caller renders whatever the stale token yields.
            _logger.debug("codex slot %s not refreshed: store busy", slot.number)
            return payload

    # ---- read model ----------------------------------------------------

    def _usage_for(
        self, slots: list[CodexSlot], fetch: set[str] | None, active: str | None
    ) -> dict[str, UsageEntry]:
        """Usage for every slot, refreshing only those asked for and due.

        The cache decides what a "due" fetch is (serve TTL, poll plan, backoff,
        cross-process lease). This method's only job is to hand it the payload
        rule — which is where the never-refresh-the-active-account invariant
        lives, so the cache never has to know about it.
        """
        if not slots:
            return {}

        wanted = [s for s in slots if fetch is not None and s.number in fetch]
        if not wanted:
            return self._cache.entries(slots)

        def payload_for(slot: CodexSlot) -> dict | None:
            return self._payload_for_usage(slot, active)

        refreshed = self._cache.refresh(
            wanted, payload_for=payload_for, active_number=active
        )

        # Workspace names ride along with a usage fetch rather than getting
        # their own pass: they change approximately never, and the grouped-scope
        # rules mean most users never issue this request at all.
        try:
            refresh_workspace_names(self._store, payload_for=payload_for)
        except Exception as e:  # pragma: no cover - defensive
            # A missing workspace name is cosmetic and must never fail a listing.
            _logger.debug("codex workspace refresh failed: %s", type(e).__name__)
        # Slots the caller did not ask about still render from cache.
        entries = self._cache.entries(slots)
        entries.update(refreshed)
        return entries

    def accounts_snapshot(self, fetch: set[str] | None = None) -> AccountsSnapshot:
        """One coherent pass over every managed Codex account."""
        active = self.current_account_number()
        usage = self._usage_for(self._store.slots(), fetch, active)
        # Re-read: a fetching pass may have filled in workspace names, and the
        # rows must show what the store holds now, not what it held before.
        slots = self._store.slots()
        rows: list[AccountSnapshot] = []

        for slot in slots:
            entry = usage.get(slot.number, UsageEntry())
            if slot.auth_mode == "apikey":
                # Derived every pass, never persisted: an API-key account has no
                # usage to fetch, and a stored sentinel would outlive the fact.
                entry = UsageEntry(sentinel="api key")
            rows.append(
                AccountSnapshot(
                    number=slot.number,
                    email=slot.email,
                    org_name=slot.workspace_name,
                    org_uuid="",
                    is_active=slot.number == active,
                    kind="api_key" if slot.auth_mode == "apikey" else "oauth",
                    switchable=slot.auth_mode != "apikey",
                    usage=entry,
                    alias=slot.alias,
                    disabled=slot.disabled,
                    provider="codex",
                )
            )

        return AccountsSnapshot(
            active_number=active,
            accounts=tuple(rows),
            taken_at=time.time(),
            provider="codex",
        )

    # ---- verbs ---------------------------------------------------------

    def add_account(self, alias: str = "") -> CodexSlot:
        """Capture whoever is currently logged in to the codex CLI."""
        payload = read_live_payload()
        ident = parse_identity(payload) if payload is not None else None
        if payload is None or ident is None:
            raise ClaudeSwitchError(
                "No Codex login found. Run 'cswap codex login' (or 'codex login') first."
            )
        if not ident.is_identifiable:
            raise ClaudeSwitchError(
                "The current Codex login carries no account id, so it cannot be "
                "told apart from another account. API-key logins are not "
                "switchable."
            )

        normalized = normalize_alias(alias) if alias else ""

        try:
            with self._lock():
                slot = self._store.upsert_slot(
                    ident.account_key,
                    email=ident.email,
                    plan=ident.plan,
                    auth_mode="apikey" if ident.is_api_key else "chatgpt",
                )
                self._store.write_snapshot(ident.account_key, payload)
                self._store.set_active(ident.account_key)
                if normalized:
                    self._store.set_alias(ident.account_key, normalized)
        except LockError as e:
            raise ClaudeSwitchError(
                "Another cswap process is using the Codex store; try again."
            ) from e
        return slot

    def switch_to(self, identifier: str) -> CodexSwitchResult:
        """Activate a stored account.

        Held under the store lock end to end: capture-then-write is exactly the
        sequence a concurrent switch or TUI refresh could interleave with, and
        an interleaving lands one account's tokens in another's slot.
        """
        slot = self._slot_or_raise(identifier)

        try:
            with self._lock():
                target = self._store.read_snapshot(slot.account_key)
                if target is None:
                    raise ClaudeSwitchError(
                        f"Codex account {slot.number} has no stored credentials"
                    )

                previous_live = read_live_payload()
                previous_active = self._store.active_key()

                self._capture_live()
                try:
                    write_live_auth(target)
                except OSError as e:
                    # Put the live file back exactly as it was; a half-switched
                    # login is worse than a failed one.
                    if previous_live is not None:
                        try:
                            write_live_auth(previous_live)
                        except OSError:
                            _logger.error("codex switch rollback failed")
                    self._store.set_active(previous_active)
                    raise ClaudeSwitchError(
                        f"Failed to activate Codex account: {e}"
                    ) from e

                self._store.set_active(slot.account_key)
        except LockError as e:
            raise ClaudeSwitchError(
                "Another cswap process is using the Codex store; try again."
            ) from e

        # Outside the lock: process detection is advisory and shells out.
        return CodexSwitchResult(
            number=slot.number, email=slot.email, running_pids=running_codex_pids()
        )

    def remove_account(self, identifier: str, assume_yes: bool = False) -> None:
        """Forget an account and delete its stored credentials.

        Prompts unless ``assume_yes``, mirroring the Claude side (whose TUI
        collects confirmation before calling and passes True). Removal deletes
        the Keychain item too, so an unprompted destructive verb here would
        diverge from its sibling for no reason.
        """
        slot = self._slot_or_raise(identifier)

        if slot.number == self.current_account_number():
            warning(
                f"Warning: Codex account {slot.number} ({slot.email}) is currently active"
            )

        if not assume_yes:
            confirm = input(
                f"Are you sure you want to permanently remove "
                f"Codex account {slot.number} ({slot.email})? [y/N] "
            )
            if confirm.lower() != "y":
                print(dimmed("Cancelled"))
                return

        self._store.remove_slot(slot.account_key)

    def set_alias(self, identifier: str, alias: str) -> tuple[str, str]:
        slot = self._slot_or_raise(identifier)
        normalized = normalize_alias(alias)
        self._store.set_alias(slot.account_key, normalized)
        return slot.number, normalized

    def unset_alias(self, identifier: str) -> str:
        slot = self._slot_or_raise(identifier)
        self._store.set_alias(slot.account_key, "")
        return slot.number

    def set_account_disabled(self, identifier: str, disabled: bool) -> None:
        slot = self._slot_or_raise(identifier)
        self._store.set_disabled(slot.account_key, disabled)

    def switchable_account_numbers(self) -> list[str]:
        """Slots eligible for automatic rotation."""
        return [
            s.number
            for s in self._store.slots()
            if not s.disabled and s.auth_mode != "apikey"
        ]

    def token_status(self, identifier: str) -> dict:
        """Diagnostics for one slot's stored token. Never returns token material.

        Deliberately reports only derived facts — when it expires, whether a
        refresh is due, when it last refreshed. The token itself is what these
        diagnostics exist to avoid making people paste into an issue.
        """
        slot = self._slot_or_raise(identifier)
        payload = self._store.read_snapshot(slot.account_key)
        if payload is None:
            return {"number": slot.number, "state": "no credentials"}
        if slot.auth_mode == "apikey":
            return {"number": slot.number, "state": "api key"}

        tokens = payload.get("tokens") or {}
        exp = access_token_expiry(payload)
        now = time.time()
        return {
            "number": slot.number,
            "state": "oauth",
            "expiresAt": exp,
            "expiresInSeconds": (exp - now) if exp is not None else None,
            "refreshDue": needs_refresh(payload, now=now),
            "hasRefreshToken": bool(tokens.get("refresh_token")),
            "lastRefresh": payload.get("last_refresh"),
        }

    def account_numbers(self) -> list[str]:
        """Every managed slot number, rotation-eligible or not.

        Public so callers that want "fetch usage for all of them" do not have to
        reach into the store — the CLI's ``list`` is the first such caller.
        """
        return [s.number for s in self._store.slots()]
