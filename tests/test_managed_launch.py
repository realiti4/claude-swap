"""Placement and `cswap run --auto` (Refs #382)."""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone

import pytest

from claude_swap import managed_launch as ml
from claude_swap.balance import AccountScore, BalanceParams
from claude_swap.managed_sessions import AccountRef

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="managed sessions are POSIX-only (v1)"
)

USAGE = {"five_hour": {"pct": 10.0}, "seven_day": {"pct": 10.0}}
IDS = {
    "1": AccountRef("lane0@example.com", "org-1"),
    "2": AccountRef("b@example.com", "org-2"),
    "3": AccountRef("c@example.com", "org-3"),
}


def fake_rank(usage_by_account, *, now, models, busy_sessions, params):
    """Deterministic stand-in: every known account eligible, fewer busy wins,
    ties keep input order (as the contract's rank_accounts does)."""
    rows = [
        AccountScore(
            account=n, eligible=True, score=-float(busy_sessions.get(n, 0)),
            slack=0.0, projected_5h=0.0, headroom=90.0,
            recovery_ts=float("inf"), reason="ok",
        )
        for n, u in usage_by_account.items() if u is not None
    ]
    return sorted(rows, key=lambda r: -r.score)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


class TestChoosePlacement:
    @pytest.fixture(autouse=True)
    def _fake_rank(self, monkeypatch):
        monkeypatch.setattr(ml, "rank_accounts", fake_rank)

    @staticmethod
    def _choose(*, usage=None, lane0="1", busy=None, candidates=("1", "2", "3")):
        return ml.choose_placement(
            candidates=list(candidates), identities=IDS,
            usage=usage if usage is not None else {n: USAGE for n in IDS},
            lane0=lane0, busy=busy or {}, now=1_000_000.0, models=(),
            params=BalanceParams(),
        )

    def test_lane0_is_excluded_when_others_have_room(self):
        placement = self._choose()
        assert (placement.number, placement.source, placement.account) == ("2", "backup", IDS["2"])

    def test_busy_sessions_steer_placement(self):
        assert self._choose(busy={"2": 2}).number == "3"

    def test_lane0_is_the_last_resort(self):
        placement = self._choose(usage={"1": USAGE, "2": None, "3": None})
        assert (placement.number, placement.source) == ("1", "lane0")

    def test_lane0_outside_candidates_is_no_fallback(self):
        assert self._choose(usage={"1": USAGE, "2": None, "3": None}, candidates=("2", "3")) is None

    def test_no_known_usage_places_nothing(self):
        assert self._choose(usage={"1": None, "2": None, "3": None}) is None

    def test_an_empty_candidate_pool_places_nothing(self):
        assert self._choose(candidates=()) is None

    def test_without_a_lane0_account_every_candidate_competes(self):
        assert self._choose(lane0=None, busy={"2": 1, "3": 1}).number == "1"


class TestChoosePlacementWithRealPolicy:
    def test_account_over_the_weekly_threshold_loses(self):
        now = time.time()
        week_reset, five_reset = _iso(now + 2 * 86400), _iso(now + 3600)
        over = {"five_hour": {"pct": 5.0, "resets_at": five_reset},
                "seven_day": {"pct": 99.0, "resets_at": week_reset}}
        fine = {"five_hour": {"pct": 5.0, "resets_at": five_reset},
                "seven_day": {"pct": 40.0, "resets_at": week_reset}}
        placement = ml.choose_placement(
            candidates=["1", "2", "3"], identities=IDS,
            usage={"1": fine, "2": over, "3": fine}, lane0="1", busy={},
            now=now, models=(), params=BalanceParams(),
        )
        assert placement.number == "3"
