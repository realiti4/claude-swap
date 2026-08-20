"""Merge several providers' snapshots into the one view a shell renders.

The dashboard and the menu bar each want a single ordered list of accounts, not
a list per provider. This module produces that list — Claude's accounts first,
then Codex's, each row already tagged with its provider — plus the lookup a
shell needs to send a keystroke back to the switcher that owns the row.

Two properties matter more than they look:

- **Order is stable and provider-major.** Rows are grouped, never interleaved by
  usage, so the row under the cursor does not move between refreshes and a
  keystroke aimed at one provider cannot land on the other.
- **One provider's failure must not blank the others.** A snapshot that raises
  is dropped with a log line; the surviving providers still render. A dashboard
  that goes empty because a secondary provider's store is corrupt is a much
  worse outcome than one missing section.

``active_number`` on the merged snapshot is the *Claude* active slot, because
every existing consumer of that field means Claude by it. Per-provider active
state is on each row's ``is_active``.
"""

from __future__ import annotations

import logging
import time

from claude_swap.models import AccountSnapshot, AccountsSnapshot
from claude_swap.providers.base import ProviderSwitcher

_logger = logging.getLogger(__name__)


def merged_snapshot(
    providers: list[ProviderSwitcher], *, fetch: set[str] | None = None
) -> tuple[AccountsSnapshot, dict[str, ProviderSwitcher]]:
    """Return the merged snapshot and a ``row key -> owning provider`` map.

    ``fetch`` carries the per-provider slot numbers to allow fetching, keyed the
    same way rows are (``"codex:2"``); None means "every account eligible", the
    Claude side's documented meaning.
    """
    rows: list[AccountSnapshot] = []
    owners: dict[str, ProviderSwitcher] = {}
    claude_active: str | None = None

    for provider in providers:
        pid = getattr(provider, "provider_id", "claude")
        per_provider = (
            None
            if fetch is None
            else {k.split(":", 1)[1] for k in fetch if k.startswith(f"{pid}:")}
        )
        try:
            snap = provider.accounts_snapshot(fetch=per_provider)
        except Exception as e:
            # One provider's bad day must not blank the whole dashboard.
            _logger.warning(
                "provider %s snapshot failed: %s", pid, type(e).__name__
            )
            continue

        if pid == "claude":
            claude_active = snap.active_number

        for account in snap.accounts:
            rows.append(account)
            owners[account.key] = provider

    return (
        AccountsSnapshot(
            active_number=claude_active,
            accounts=tuple(rows),
            taken_at=time.time(),
            provider="multi",
        ),
        owners,
    )


def group_by_provider(
    snapshot: AccountsSnapshot,
) -> list[tuple[str, list[AccountSnapshot]]]:
    """``[(provider_id, rows), ...]`` in the snapshot's own order.

    Preserves first-seen order rather than sorting, so the grouping matches what
    the shell is about to render row for row.
    """
    groups: list[tuple[str, list[AccountSnapshot]]] = []
    index: dict[str, list[AccountSnapshot]] = {}
    for account in snapshot.accounts:
        bucket = index.get(account.provider)
        if bucket is None:
            bucket = []
            index[account.provider] = bucket
            groups.append((account.provider, bucket))
        bucket.append(account)
    return groups


#: Short label shown on a row so two providers' accounts cannot be confused.
PROVIDER_LABELS = {"claude": "claude", "codex": "codex"}


def provider_label(provider_id: str) -> str:
    return PROVIDER_LABELS.get(provider_id, provider_id)


class MultiSnapshotSource:
    """One ``SnapshotSource`` per provider, merged into a single view.

    Reconciliation (rejecting per-account regressions between passes) is the
    reason each provider gets its own ``SnapshotSource`` rather than one shared
    one: that logic compares an account against *its own* previous state, and
    handing it a list that alternates between providers would make every pass
    look like a wholesale change.

    ``take`` returns the merged snapshot and the ``row key -> provider`` map a
    shell needs to send a keystroke back to the switcher that owns the row.
    """

    def __init__(self, providers: list[ProviderSwitcher]) -> None:
        from claude_swap.snapshot_source import SnapshotSource

        self._sources = [(p, SnapshotSource(p)) for p in providers]

    @property
    def providers(self) -> list[ProviderSwitcher]:
        return [p for p, _ in self._sources]

    def take(
        self, *, full: bool = False, store_only: bool = False
    ) -> tuple[AccountsSnapshot, dict[str, ProviderSwitcher]]:
        """Blocking snapshot pass across every provider; call from a worker."""
        rows: list[AccountSnapshot] = []
        owners: dict[str, ProviderSwitcher] = {}
        claude_active: str | None = None

        for provider, source in self._sources:
            pid = getattr(provider, "provider_id", "claude")
            try:
                snap = source.take(full=full, store_only=store_only)
            except Exception as e:
                _logger.warning(
                    "provider %s snapshot failed: %s", pid, type(e).__name__
                )
                continue
            if pid == "claude":
                claude_active = snap.active_number
            for account in snap.accounts:
                rows.append(account)
                owners[account.key] = provider

        return (
            AccountsSnapshot(
                active_number=claude_active,
                accounts=tuple(rows),
                taken_at=time.time(),
                provider="multi",
            ),
            owners,
        )
