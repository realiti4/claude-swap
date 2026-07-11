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

from claude_swap.models import AccountsSnapshot
from claude_swap.switcher import ClaudeAccountSwitcher


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

    def take(
        self, *, full: bool = False, store_only: bool = False
    ) -> AccountsSnapshot:
        """Blocking snapshot pass; call from a thread worker."""
        fetch: set[str] | None = set() if store_only else None
        snap = self.switcher.accounts_snapshot(fetch=fetch)
        self._last = snap
        return snap
