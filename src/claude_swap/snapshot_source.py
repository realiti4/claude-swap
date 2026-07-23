"""Snapshot source — the supported read path for dashboards and GUI shells.

Pacing is store-governed: the usage store's persisted poll plans plus its
freshness/backoff/claim gates (decided atomically in ``UsageStore.reserve``)
cap every surface at the same per-token cadence, so a dashboard repainting
every few seconds and a one-shot ``cswap list`` produce identical network
behavior. This class therefore just runs the same on-demand pass as ``cswap
list`` (``fetch=None``) each take — the store decides which accounts, if
any, may actually be fetched — and offers ``store_only`` for shells that
host an auto engine (which already collects on its own schedule).

``take()`` is blocking (file locks, keychain subprocesses, network): call it
from a background thread, never a UI event loop.
"""

from __future__ import annotations

import threading
from dataclasses import replace

from claude_swap.json_output import USAGE_TOKEN_EXPIRED
from claude_swap.models import AccountSnapshot, AccountsSnapshot
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.usage_store import UsageEntry


class SnapshotSource:
    """Takes one coherent snapshot per call; the store paces the network.

    ``full=True`` (the user's explicit refresh) is accepted for API
    stability but is no faster than a normal pass: even an explicit refresh
    is capped by the store's serve TTL and poll plans. ``store_only=True``
    reads the store without any network eligibility.
    """

    def __init__(self, switcher: ClaudeAccountSwitcher) -> None:
        self.switcher = switcher
        self._last: AccountsSnapshot | None = None
        self._lock = threading.Lock()

    def take(
        self, *, full: bool = False, store_only: bool = False
    ) -> AccountsSnapshot:
        """Blocking snapshot pass; call from a thread worker."""
        fetch: set[str] | None = set() if store_only else None
        snap = self.switcher.accounts_snapshot(fetch=fetch)
        with self._lock:
            snap = self._reconcile(snap)
            self._last = snap
            return snap

    def _reconcile(self, snap: AccountsSnapshot) -> AccountsSnapshot:
        if self._last is None:
            return snap
        previous = {acc.number: acc for acc in self._last.accounts}
        accounts = tuple(
            self._reconcile_account(acc, previous.get(acc.number), snap.taken_at)
            for acc in snap.accounts
        )
        return replace(snap, accounts=accounts)

    def _reconcile_account(
        self,
        acc: AccountSnapshot,
        prev: AccountSnapshot | None,
        taken_at: float,
    ) -> AccountSnapshot:
        if prev is None or _account_identity(acc) != _account_identity(prev):
            return acc

        prev_fetched = prev.usage.fetched_at
        fetched = acc.usage.fetched_at
        if prev_fetched is not None and (fetched is None or fetched < prev_fetched):
            return replace(acc, usage=_with_current_age(prev.usage, taken_at))
        if acc.usage.sentinel is not None:
            return acc

        if (
            prev.usage.sentinel == USAGE_TOKEN_EXPIRED
            and fetched == prev_fetched
        ):
            return replace(acc, usage=replace(acc.usage, sentinel=USAGE_TOKEN_EXPIRED))
        return acc


def _account_identity(acc: AccountSnapshot) -> tuple[str, str, str]:
    return (acc.email, acc.org_uuid, acc.kind)


def _with_current_age(usage: UsageEntry, taken_at: float) -> UsageEntry:
    fetched = usage.fetched_at
    if fetched is None:
        return usage
    return replace(usage, age_s=max(0.0, taken_at - fetched))
