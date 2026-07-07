"""Paced snapshot source — the supported read path for dashboards and GUI shells.

Anything that displays every account's usage on a timer (the TUI, the menu
bar, any future shell) must not run a full on-demand fetch pass per refresh:
the usage store's freshness window dedupes bursts but is not a sustained-rate
governor, and the usage endpoint rate-limits per account (a full pass per
minute keeps every token at its sustained-limit edge). ``SnapshotSource``
owns *when* a pass may hit the network: per refresh, only the active account
plus — at most once per ``SERVE_TTL_S`` — the stalest due alternate is
eligible, so an open dashboard costs O(1) requests per TTL window regardless
of account count. The store's own gates (freshness, backoff/Retry-After,
claims) apply on top, and this class never writes poll plans — ``cswap
auto`` stays the cadence learner.

``take()`` is blocking (file locks, keychain subprocesses, network): call it
from a background thread, never a UI event loop.
"""

from __future__ import annotations

import time

from claude_swap import usage_store
from claude_swap.models import AccountsSnapshot
from claude_swap.switcher import ClaudeAccountSwitcher


class SnapshotSource:
    """Plans each pass's fetch set and takes one coherent snapshot.

    The first ``take()`` is a full on-demand pass (``fetch=None``, every
    stale account eligible — what a user opening a dashboard expects, and
    exactly what ``cswap list`` does); afterwards the pacing discipline above
    applies. ``full=True`` (the user's explicit refresh) repeats the full
    pass; ``store_only=True`` (when an auto-switch engine is running and
    already paces all fetching) reads the store without any network
    eligibility.
    """

    def __init__(self, switcher: ClaudeAccountSwitcher) -> None:
        self.switcher = switcher
        self._first_pass = True
        self._next_alt_mono = 0.0
        self._last: AccountsSnapshot | None = None

    def take(
        self, *, full: bool = False, store_only: bool = False
    ) -> AccountsSnapshot:
        """Blocking snapshot pass; call from a thread worker."""
        if store_only:
            fetch: set[str] | None = set()
        elif full or self._first_pass:
            fetch = None
            self._next_alt_mono = time.monotonic() + usage_store.SERVE_TTL_S
        else:
            fetch = self._disciplined_fetch_set()
        self._first_pass = False
        snap = self.switcher.accounts_snapshot(fetch=fetch)
        self._last = snap
        return snap

    def _disciplined_fetch_set(self) -> set[str]:
        """Active account + at most one due alternate per ``SERVE_TTL_S``.

        The alternate is picked from the *previous* snapshot's entries — a
        few-second-stale nomination is harmless: the collector re-checks
        freshness/backoff/claims before actually fetching, so a bad pick
        simply fetches nothing.
        """
        active = self.switcher.current_account_number()
        fetch = {active} if active else set()
        now = time.monotonic()
        if now >= self._next_alt_mono:
            self._next_alt_mono = now + usage_store.SERVE_TTL_S
            if self._last is not None:
                candidates = [
                    acc.number
                    for acc in self._last.accounts
                    if acc.switchable and acc.number != active
                ]
                entries = {acc.number: acc.usage for acc in self._last.accounts}
                pick = usage_store.due_candidate(candidates, entries, time.time())
                if pick is not None:
                    fetch.add(pick)
        return fetch
