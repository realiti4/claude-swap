"""Codex usage fetching, behind the same cache the Claude side uses.

Stage 1 fetched live on every call: one request per account per ``cswap codex
list``. Fine for a human at a terminal, unacceptable for an auto loop that ticks
every few minutes. This module puts Codex behind ``UsageStore`` +
``poll_policy``, which already implement — and have tests for — serve TTL,
cross-process fetch leases, failure backoff, 429 handling with ``Retry-After``,
and an adaptive cadence that speeds up when usage is moving.

None of that is reimplemented here. ``UsageStore`` stores an opaque ``lastGood``
dict guarded by an identity tuple, so it was already provider-neutral; this
module is the adapter that decides *which* slots need a request, performs those,
and records the outcomes.

**Identity for Codex is ``(email, chatgpt_account_id)``.** The store's guard
exists so a slot reused for a different account never serves its predecessor's
usage; the account id is exactly what distinguishes two workspaces of one user,
so it takes the ``organizationUuid`` position the Claude side uses.

**The active-account rule lives in the caller.** ``payload_for`` is supplied by
``CodexSwitcher``, which is what enforces "never refresh the active account from
its snapshot". Keeping it a callback means this module never needs to know that
rule, and the rule stays in one place.
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from typing import Callable

from claude_swap.codex import paths as cpaths
from claude_swap.codex.store import CodexSlot, CodexStore
from claude_swap.codex.usage import fetch_usage
from claude_swap.poll_policy import plan_after_fetch
from claude_swap.usage_store import FetchRecord, UsageEntry, UsageStore

_logger = logging.getLogger(__name__)

#: Sentinels are derived state, re-computed every pass and never persisted.
#: Anything in this set is recorded as a no-op rather than as a failure, so a
#: structurally unusable account does not accrue backoff it can never clear.
_NON_FAILURE_SENTINELS = frozenset({"MissingAuth", "api key", "no credentials"})


class CodexUsageCache:
    """Decides which Codex slots need a request, fetches those, records them."""

    def __init__(self, store: CodexStore, clock: Callable[[], float] = time.time) -> None:
        self._store = store
        self._clock = clock
        self._usage = UsageStore(cpaths.get_codex_cache_dir(), clock=clock)

    @staticmethod
    def identity_for(slot: CodexSlot) -> tuple[str, str]:
        """The store's identity guard for one slot."""
        return (slot.email, slot.account_key.rpartition("::")[2])

    def identities(self, slots: list[CodexSlot]) -> dict[str, tuple[str, str]]:
        return {s.number: self.identity_for(s) for s in slots}

    def entries(self, slots: list[CodexSlot]) -> dict[str, UsageEntry]:
        """Cached read model for these slots. Never touches the network."""
        return self._usage.entries(self.identities(slots))

    def refresh(
        self,
        slots: list[CodexSlot],
        *,
        payload_for: Callable[[CodexSlot], dict | None],
        threshold: float = 100.0,
        active_number: str | None = None,
    ) -> dict[str, UsageEntry]:
        """Fetch whichever of ``slots`` is genuinely due, then return them all.

        A slot that is fresh, in backoff, or already leased by another process
        is served from cache without a request — that is the entire point.
        """
        if not slots:
            return {}

        by_number = {s.number: s for s in slots}
        identities = self.identities(slots)

        # respect_plans=True: the on-demand caller contract. Fetch only when the
        # entry is both stale and poll-due, so a second `cswap codex list`
        # seconds after the first costs nothing.
        claims = self._usage.reserve(
            list(by_number), identities, respect_plans=True
        )

        sentinels: dict[str, str] = {}

        if claims:
            before = self._usage.entries(identities)
            outcomes: dict[str, FetchRecord] = {}
            plans: dict[str, tuple[float | None, float | None]] = {}

            for number in claims:
                slot = by_number[number]
                outcomes[number], plan = self._fetch_one(
                    slot,
                    payload_for(slot),
                    prev=before.get(number),
                    threshold=threshold,
                    is_active=number == active_number,
                )
                if plan is not None:
                    plans[number] = plan

            self._usage.record(outcomes, identities, claims=claims, plans=plans)

            # A sentinel is a live overlay, re-derived every pass and never
            # persisted (see UsageEntry). The store therefore cannot hand it
            # back, so this pass's sentinels are laid over the read here —
            # otherwise "api key" or a 401 would render as a blank row.
            sentinels = {
                num: (rec.sentinel or rec.error)
                for num, rec in outcomes.items()
                if rec.sentinel is not None or rec.error is not None
            }

        entries = self._usage.entries(identities)
        for number, sentinel in sentinels.items():
            entries[number] = replace(entries[number], sentinel=sentinel)
        return entries

    def _fetch_one(
        self,
        slot: CodexSlot,
        payload: dict | None,
        *,
        prev: UsageEntry | None,
        threshold: float,
        is_active: bool,
    ) -> tuple[FetchRecord, tuple[float | None, float | None] | None]:
        """One account's fetch, as a record the store can merge."""
        if slot.auth_mode == "apikey":
            return FetchRecord(sentinel="api key"), None
        if payload is None:
            return FetchRecord(sentinel="no credentials"), None

        tokens = payload.get("tokens") or {}
        result = fetch_usage(
            tokens.get("access_token") or "",
            tokens.get("account_id") or slot.account_key.rpartition("::")[2],
        )

        if result.usage is not None:
            now = self._clock()
            plan = plan_after_fetch(
                prev_interval_s=prev.poll_interval_s if prev else None,
                prev_usage=prev.last_good if prev else None,
                new_usage=result.usage,
                is_active=is_active,
                threshold=threshold,
                models=(),
                recent_429=prev.recent_429(now) if prev else False,
                now=now,
            )
            return FetchRecord(usage=result.usage), plan

        sentinel = result.sentinel or "bad-response"
        if sentinel in _NON_FAILURE_SENTINELS:
            # Derived state, not a server failure: recording it as an error
            # would accrue backoff the account can never clear by itself.
            return FetchRecord(sentinel=sentinel), None

        return FetchRecord(error=sentinel, retry_after_s=result.retry_after_s), None
