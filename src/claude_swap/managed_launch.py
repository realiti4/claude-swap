"""``cswap run --auto``: place a new Claude session on an account and launch it.

Placement uses the balance policy (``balance.rank_accounts``) with the
fleet's server-reported usage and the local busy count (live managed
sessions, reservations included). The default login's account (lane 0) is
held out — its Claude owns that lineage and is already spending that 5h
window — unless nothing else can take the session; then the session gets a
read-only copy of lane 0's current access token (never its refresh token).
``candidates`` is the caller's job to narrow: an account with a live
``cswap run N`` session (that Claude owns its lineage too) is excluded
before it ever reaches :func:`choose_placement`, which only ranks whatever
pool it is handed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from claude_swap.balance import AccountScore, BalanceParams, rank_accounts
from claude_swap.managed_sessions import SOURCE_BACKUP, SOURCE_LANE0, AccountRef


@dataclass(frozen=True)
class Placement:
    """Where a new managed session should run, and why."""

    number: str
    account: AccountRef
    source: str          # SOURCE_BACKUP or SOURCE_LANE0
    score: AccountScore


def choose_placement(
    *,
    candidates: Sequence[str],
    identities: Mapping[str, AccountRef],
    usage: Mapping[str, dict | None],
    lane0: str | None,
    busy: Mapping[str, int],
    now: float,
    models: Sequence[str],
    params: BalanceParams,
) -> Placement | None:
    """Rank ``candidates`` under the balance policy and place on the best one.

    Lane 0 is dropped from the ranked pool — its Claude owns that lineage
    and is already spending its 5h window — and only retried, as a last
    resort, when nothing else in ``candidates`` is eligible: it is then
    scored on its own and returned with ``source="lane0"``, but only if it
    is itself one of ``candidates``. Returns None when nothing (including
    lane 0) can take the session.
    """
    pool = [n for n in candidates if n != lane0 and n in identities]
    ranked = rank_accounts(
        {n: usage.get(n) for n in pool},
        now=now,
        models=models,
        busy_sessions={n: busy.get(n, 0) for n in pool},
        params=params,
    )
    if ranked:
        best = ranked[0]
        return Placement(best.account, identities[best.account], SOURCE_BACKUP, best)
    if lane0 is None or lane0 not in candidates or lane0 not in identities:
        return None
    lane = rank_accounts(
        {lane0: usage.get(lane0)},
        now=now,
        models=models,
        busy_sessions={lane0: busy.get(lane0, 0)},
        params=params,
    )
    if not lane:
        return None
    return Placement(lane0, identities[lane0], SOURCE_LANE0, lane[0])
