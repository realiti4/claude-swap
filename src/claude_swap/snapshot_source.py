"""Snapshot source — the supported read path for dashboards and GUI shells.

Pacing is store-governed: the usage store's persisted poll plans plus its
freshness/backoff/claim gates (decided atomically in ``UsageStore.reserve``)
cap every surface at the same per-account cadence, so a dashboard repainting
every few seconds and a one-shot ``cswap list`` produce identical network
behavior. This class therefore just runs the same on-demand pass as ``cswap
list`` (``fetch=None``) each take — the store decides which accounts, if
any, may actually be fetched — and offers ``store_only`` for shells that
host an auto engine (which already collects on its own schedule).

``take()`` is blocking (file locks, keychain subprocesses, network): call it
from a background thread, never a UI event loop.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import replace

from claude_swap import macos_keychain
from claude_swap.json_output import USAGE_TOKEN_EXPIRED
from claude_swap.models import AccountSnapshot, AccountsSnapshot
from claude_swap.paths import get_credentials_path
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.usage_store import UsageEntry

# ponytail: the floor between two store reads of a store-only poll while no
# tracked stamp has moved. Every read of a macOS store costs `security` execs that
# endpoint software scans; raise this to cut them, lower it to notice a change the
# stamps do not track (the live identity in ~/.claude.json, a badge that flips
# with the clock) sooner.
STORE_REREAD_S = 30.0


def _stat(path) -> tuple | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_ino, st.st_mtime_ns, st.st_size)


class SnapshotSource:
    """Takes one coherent snapshot per call; the store paces the network.

    ``full=True`` (the user's explicit refresh) is no faster than a normal
    pass: even an explicit refresh is capped by the store's serve TTL and poll
    plans. It does make a ``store_only`` poll read. ``store_only=True`` reads
    the store without any network eligibility.

    On macOS a ``store_only`` poll reads the store again only when the
    keychain stamp, the plaintext credentials file or the state directories
    have moved since the last read, or ``STORE_REREAD_S`` has passed, or the
    caller ``invalidate()``d; otherwise it returns the last snapshot, aged to
    now. Where there is no keychain stamp every poll reads.
    """

    def __init__(
        self, switcher: ClaudeAccountSwitcher, clock=time.time
    ) -> None:
        self.switcher = switcher
        self._clock = clock
        self._last: AccountsSnapshot | None = None
        self._read_stamp: tuple | None = None  # what the last read started under
        self._read_at = 0.0
        self._lock = threading.Lock()

    def invalidate(self) -> None:
        """The next ``take`` reads the store, whatever the stamps say."""
        with self._lock:
            self._read_stamp = None

    def _stamp(self) -> tuple | None:
        kc = macos_keychain.keychain_stamp()
        if kc is None:
            return None
        root = self.switcher.backup_dir
        paths = (get_credentials_path(), root, root / "cache", root / "credentials")
        return (kc, *map(_stat, paths))

    def take(
        self, *, full: bool = False, store_only: bool = False
    ) -> AccountsSnapshot:
        """Blocking snapshot pass; call from a thread worker."""
        stamp = self._stamp()  # before the read: a write during it moves the next one
        now = self._clock()
        with self._lock:
            if (
                store_only and not full and stamp is not None
                and stamp == self._read_stamp and self._last is not None
                and 0 <= now - self._read_at < STORE_REREAD_S
            ):
                return replace(self._last, taken_at=now, accounts=tuple(
                    replace(acc, usage=_with_current_age(acc.usage, now))
                    for acc in self._last.accounts
                ))
        fetch: set[str] | None = set() if store_only else None
        snap = self.switcher.accounts_snapshot(fetch=fetch)
        with self._lock:
            snap = self._reconcile(snap)
            self._last, self._read_stamp, self._read_at = snap, stamp, now
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
        if prev is None or account_identity(acc) != account_identity(prev):
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
            and acc.access_token_fp is not None
            and acc.access_token_fp == prev.access_token_fp
        ):
            return replace(acc, usage=replace(acc.usage, sentinel=USAGE_TOKEN_EXPIRED))
        return acc


def account_identity(acc: AccountSnapshot) -> tuple[str, str, str]:
    """The identity discriminator for a slot across snapshots.

    The single definition of "same account": snapshot reconciliation here and
    the TUI's snapshot merge must agree, or an identity change (e.g. a slot
    re-used by another login) is detected by one and missed by the other.
    """
    return (acc.email, acc.org_uuid, acc.kind)


def _with_current_age(usage: UsageEntry, taken_at: float) -> UsageEntry:
    fetched = usage.fetched_at
    if fetched is None:
        return usage
    return replace(usage, age_s=max(0.0, taken_at - fetched))
