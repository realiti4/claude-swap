"""MEU-ORD-03 - the per-account `order` tier inside the ranking sort key.

Covers AC-17 .. AC-27, including AC-18b and AC-21b.

**Observable split** (plan §Control binding, corrected in plan-review rounds 1
and 2). A blanket "always assert the emitted destination" rule is unsatisfiable
for four of these criteria, so each AC states which observable it owns:

* **Behavioural** - AC-18b, AC-19, AC-20, AC-21, AC-22, AC-23, AC-24, AC-25,
  AC-26 drive a **real tick** (`tick()` -> `_tick_inner` -> the `_rank`
  closure -> `_one_pass` -> `_rank_candidates` -> `qualifying.sort` ->
  `_perform` -> `SwitchEvent`) and assert on the emitted destination.
* **Structural** - AC-17 (purity + one resolution per tick), AC-18 (map
  totality) and AC-21b (the `ordered` tail) assert against the function or the
  map **under direct call**. None of the three is a field on any event: a
  call count, a `KeyError` that does not happen, and a reordering below
  position 1 are all invisible to a destination assertion.
* **AC-27** asserts on the **persisted poll plan** (`nextPollAt` /
  `pollIntervalS`, written through `_collect_scheduled_usage`) and on
  `_next_delay` under a patched `random`, plus a structural no-`orders`-
  parameter check. `PollEvent` carries no interval field, so it could not have
  carried this assertion either.

**The emitted destination** is `SwitchEvent.to_ref["number"]`. The plan calls
this field `SwitchEvent.to`; `to` is the name it serializes under
(`SwitchEvent._fields()`), while the attribute is `to_ref`. Same observable,
resolved to the real attribute name.

Synthetic fixtures only - `EngineHarness` seeds a throwaway store under
`temp_home`. Nothing here reads a real account store.
"""

from __future__ import annotations

import contextlib
import inspect
import os
import random as _random_module
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import autoswitch as _autoswitch_module
from claude_swap import poll_policy
from claude_swap.autoswitch import (
    NoSwitchEvent,
    SwitchEvent,
    TickOutcome,
)
from claude_swap.models import (
    ACCOUNT_ORDER_MAX,
    ACCOUNT_ORDER_MIN,
    ORDER_UNSET_RANK,
)
from claude_swap.settings import AutoSwitchSettings
from claude_swap.usage_store import UsageEntry

from tests.test_autoswitch import EngineHarness, _entry_for, _iso_at

GLOBAL = AutoSwitchSettings().threshold  # 90.0

_EMAILS = {
    1: "a@example.com",
    2: "b@example.com",
    3: "c@example.com",
    4: "d@example.com",
}


def _u(
    pct: float,
    *,
    five_reset: float | None = None,
    seven_reset: float | None = None,
    seven_pct: float = 0.0,
) -> dict:
    """A usage row with independently addressable 5h and 7d resets.

    `tests.test_autoswitch._usage` only reaches the five-hour window, but
    consume-first ranks on `seven_day` while the `all_above` recovery tier
    ranks on the *binding* window. Both are settable here so one fixture can
    make the headroom order, the recovery order and the weekly order three
    different permutations - which is what makes a key-shape regression move a
    destination instead of hiding behind an accidental agreement.
    """
    five: dict = {"pct": pct}
    if five_reset is not None:
        five["resets_at"] = _iso_at(five_reset)
    seven: dict = {"pct": seven_pct}
    if seven_reset is not None:
        seven["resets_at"] = _iso_at(seven_reset)
    return {"five_hour": five, "seven_day": seven}


def _fleet(temp_home, n: int = 4, **settings_kwargs) -> EngineHarness:
    """`n` seeded OAuth accounts, account 1 live and active."""
    h = EngineHarness(temp_home, **settings_kwargs)
    for num in range(1, n + 1):
        h.seed(num, _EMAILS[num])
    h.make_live(_EMAILS[1], 1)
    return h


def _pin(h: EngineHarness, **ranks: int) -> None:
    """`_pin(h, a2=5)` pins account 2 to rank 5 (kwargs cannot start with a digit)."""
    for name, rank in ranks.items():
        h.switcher.set_account_order(name.lstrip("a"), rank)


def _backup(h: EngineHarness, *nums: int) -> None:
    for num in nums:
        h.switcher.set_account_backup(str(num), True)


def _api_key(h: EngineHarness, *nums: int) -> None:
    data = h.switcher._get_sequence_data()
    for num in nums:
        data["accounts"][str(num)]["kind"] = "api_key"
    h.switcher._write_json(h.switcher.sequence_file, data)


@contextlib.contextmanager
def _isolated_home(tmp_path: Path, name: str):
    """A second, genuinely separate `temp_home` for the same test.

    `EngineHarness` only patches `Path.home()` for the duration of its own
    `__init__`, so two harnesses built on the SAME root share one store - the
    guarantee `TestEngineHarnessIsolation` documents applies to distinct
    subtrees, and a subtree harness cannot call `make_live()`. A test that
    needs two independent *runs* of the same fleet therefore has to supply two
    independent homes, with the ambient patches in place for the whole run and
    not merely for construction.
    """
    home = tmp_path / name
    (home / ".claude").mkdir(parents=True)
    with (
        patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)}),
        patch("pathlib.Path.home", return_value=home),
    ):
        yield home


def _tick(h: EngineHarness, snapshots: list[dict]):
    """One real tick, serving `snapshots` to successive collector calls.

    The last snapshot repeats, so a fixture lists only as many as it needs to
    distinguish. Returns `(outcome, mock)`.
    """
    seq = [
        {
            num: value
            if isinstance(value, UsageEntry)
            else _entry_for(value, h.clock.now)
            for num, value in snap.items()
        }
        for snap in snapshots
    ]
    calls = {"n": 0}

    def _fetch(**_kwargs):
        index = min(calls["n"], len(seq) - 1)
        calls["n"] += 1
        return seq[index]

    with patch.object(
        h.switcher, "usage_entries_by_account", side_effect=_fetch
    ) as mock:
        return h.engine.tick(), mock


def _switches(h: EngineHarness) -> list[SwitchEvent]:
    return [e for e in h.events if isinstance(e, SwitchEvent)]


def _dest(h: EngineHarness) -> list[int]:
    """The emitted destinations - `SwitchEvent.to_ref["number"]`."""
    return [e.to_ref["number"] for e in _switches(h) if e.to_ref]


def _rank_kwargs(h: EngineHarness, headroom: dict[str, float | None], **over):
    """A complete keyword set for a direct `_rank_candidates` call."""
    now = h.clock.now
    usage = {num: _u(100.0 - v) for num, v in headroom.items() if v is not None}
    kwargs = dict(
        trigger="proactive",
        consume_first=False,
        oauth_candidates=[n for n in headroom if n != "1"],
        no_return=None,
        usage=usage,
        headroom=headroom,
        current="1",
        active_headroom=headroom.get("1"),
        settings=h.settings,
        now=now,
    )
    kwargs.update(over)
    return kwargs


# ---------------------------------------------------------------------------
# AC-17 - structural: one resolution per tick, and the ranking stays pure
# ---------------------------------------------------------------------------


class TestOrderResolvedOncePerTick:
    """AC-17 (i). `_resolve_orders()` runs once in `_tick_inner` and the map is
    threaded down as a parameter.

    The oracle needs a tick that ranks **twice**. The backup two-pass is the
    deterministic way to get one: `_rank` runs pass 1 over the primaries and,
    finding nothing, re-enters `_one_pass` over the full pool
    (`autoswitch.py:1341-1346`). If `_rank_candidates` resolved the map itself
    the counter would move with it.

    A destination assertion cannot express this: "resolved once" and "resolved
    twice" produce the *same* destination whenever the store did not change
    mid-tick, which is exactly the case a store read makes fragile.
    """

    def test_orders_are_resolved_once_though_ranking_runs_twice(self, temp_home):
        h = _fleet(temp_home)
        _backup(h, 3, 4)
        _pin(h, a2=1)
        real_resolve = h.engine._resolve_orders
        real_rank = h.engine._rank_candidates
        with (
            patch.object(
                h.engine, "_resolve_orders", side_effect=real_resolve
            ) as resolve,
            patch.object(
                h.engine, "_rank_candidates", side_effect=real_rank
            ) as rank,
        ):
            _tick(h, [{
                "1": _u(95.0),   # active, above the line -> proactive
                "2": _u(100.0),  # the only primary, at its limit
                "3": _u(5.0),    # reserve
                "4": _u(4.0),    # reserve
            }])
        assert rank.call_count >= 2, (
            "the fixture must reach the backup two-pass, or the oracle is vacuous"
        )
        assert resolve.call_count == 1

    def test_every_rank_call_receives_the_same_map_object(self, temp_home):
        """The other half of "resolved once": threaded, not re-derived."""
        h = _fleet(temp_home)
        _backup(h, 3, 4)
        _pin(h, a2=1)
        seen: list[object] = []
        real_rank = h.engine._rank_candidates

        def _spy(**kw):
            seen.append(kw.get("orders"))
            return real_rank(**kw)

        with patch.object(h.engine, "_rank_candidates", side_effect=_spy):
            _tick(h, [{
                "1": _u(95.0), "2": _u(100.0), "3": _u(5.0), "4": _u(4.0),
            }])
        assert len(seen) >= 2
        assert all(m is seen[0] for m in seen)
        assert seen[0] is not None


class TestRankCandidatesStaysPure:
    """AC-17 (ii). `_rank_candidates` never reads the store.

    Its docstring is explicit that purity is what lets the consume-first
    two-phase commit run it twice per tick; a store read inside it could decide
    phase 1 and phase 2 on two different files. The test makes every store
    reader raise, so a read is a hard failure rather than a silent difference.
    """

    def test_ranking_returns_normally_with_every_store_reader_raising(
        self, temp_home
    ):
        h = _fleet(temp_home)
        kwargs = _rank_kwargs(h, {"1": 5.0, "2": 40.0, "3": 80.0})

        def _boom(*_a, **_kw):
            raise AssertionError("_rank_candidates read the store")

        with (
            patch.object(h.switcher, "_get_sequence_data", side_effect=_boom),
            patch.object(h.switcher, "account_orders", side_effect=_boom),
            patch.object(h.switcher, "account_policies", side_effect=_boom),
            patch.object(
                h.switcher, "backup_account_numbers", side_effect=_boom
            ),
        ):
            ordered, _known, _reset = h.engine._rank_candidates(**kwargs)
        assert ordered == ["3", "2"]

    def test_two_identical_calls_return_equal_results(self, temp_home):
        h = _fleet(temp_home)
        _pin(h, a2=1)
        orders = h.engine._resolve_orders()
        kwargs = _rank_kwargs(h, {"1": 5.0, "2": 40.0, "3": 80.0}, orders=orders)
        first = h.engine._rank_candidates(**kwargs)
        second = h.engine._rank_candidates(**kwargs)
        assert first == second
        assert first[0] == ["2", "3"], "the pin must actually be in force here"


# ---------------------------------------------------------------------------
# AC-18 / AC-18b - the map is total, and the unset sentinel outranks nothing
# ---------------------------------------------------------------------------


class TestOrderMapTotality:
    """AC-18 - structural: `OrderMap.__missing__` returns `ORDER_UNSET_RANK`.

    Totality lives in the map, exactly as it does for `ThresholdMap`
    (`autoswitch.py:596-612`), so no call site needs `.get(num, default)` and
    no tick can raise `KeyError` mid-ranking. **No tick can reach a non-fleet
    key**, so this is unreachable through any event and is asserted under
    direct subscript.
    """

    def test_a_key_that_is_not_in_the_fleet_at_all_returns_the_sentinel(self):
        from claude_swap.autoswitch import OrderMap

        m = OrderMap({"2": 5})
        assert m["999"] == ORDER_UNSET_RANK

    def test_the_missing_key_is_not_inserted(self):
        """`__missing__` must not memoise, or the map would grow per tick."""
        from claude_swap.autoswitch import OrderMap

        m = OrderMap({})
        m["7"]
        assert "7" not in m

    def test_a_pinned_key_returns_its_own_rank(self):
        from claude_swap.autoswitch import OrderMap

        assert OrderMap({"2": 5})["2"] == 5

    def test_subscript_never_raises_key_error(self):
        from claude_swap.autoswitch import OrderMap

        m = OrderMap({})
        try:
            m["nope"]
        except KeyError:  # pragma: no cover - the failure this test exists for
            pytest.fail("OrderMap must be total; a KeyError here breaks a tick")

    def test_the_sentinel_sorts_after_every_legal_rank(self):
        """AC-6's arithmetic, restated where the map relies on it."""
        assert ORDER_UNSET_RANK > ACCOUNT_ORDER_MAX >= ACCOUNT_ORDER_MIN

    def test_the_sentinel_is_not_the_minimum_rank(self):
        """The direct unit statement of mutant M-6; AC-18b is its tick-level
        oracle."""
        assert ORDER_UNSET_RANK != ACCOUNT_ORDER_MIN

    def test_resolve_orders_returns_a_total_map_over_the_live_fleet(
        self, temp_home
    ):
        h = _fleet(temp_home)
        _pin(h, a2=7)
        orders = h.engine._resolve_orders()
        assert orders["2"] == 7
        assert orders["3"] == ORDER_UNSET_RANK
        assert orders["1"] == ORDER_UNSET_RANK


class TestUnsetSentinelOutranksNothing:
    """AC-18b - **mutant M-6's oracle**.

    An account pinned to a rank *above* `ACCOUNT_ORDER_MIN` still beats an
    unpinned peer the strategy would otherwise pick. Under the correct engine
    the unpinned peer carries `ORDER_UNSET_RANK` (1000) and loses to tier 5.
    Under M-6 - `__missing__` returning `ACCOUNT_ORDER_MIN` - the *unpinned*
    account is promoted to rank 1 and wins.

    The pin must be strictly greater than `ACCOUNT_ORDER_MIN` or the mutant is
    invisible, which is precisely why AC-21's all-unpinned fixtures cannot
    serve as this oracle.
    """

    PIN = 5

    def test_a_mid_rank_pin_beats_an_unpinned_strategy_winner(self, temp_home):
        assert self.PIN > ACCOUNT_ORDER_MIN, "or M-6 survives this test"
        h = _fleet(temp_home, n=3)
        _pin(h, a2=self.PIN)
        outcome, _ = _tick(h, [{
            "1": _u(95.0),  # active, above the line
            "2": _u(60.0),  # pinned, strictly worse on headroom
            "3": _u(20.0),  # unpinned, the strict `best` winner
        }])
        assert outcome is TickOutcome.SWITCHED
        assert _dest(h) == [2]
        assert h.active_number() == 2

    def test_the_same_fleet_unpinned_picks_the_strategy_winner(self, temp_home):
        """The identity half: the pin is the only thing that moved."""
        h = _fleet(temp_home, n=3)
        outcome, _ = _tick(h, [{
            "1": _u(95.0), "2": _u(60.0), "3": _u(20.0),
        }])
        assert outcome is TickOutcome.SWITCHED
        assert _dest(h) == [3]


# ---------------------------------------------------------------------------
# AC-19 - the tier: order is a primary selector, never a tie-break
# ---------------------------------------------------------------------------


class TestOrderIsAPrimarySelectorNotATieBreak:
    """AC-19 - **the discriminating oracle**.

    A pinned account with strictly less headroom **and** a strictly later
    weekly reset than an unpinned peer must still be selected, under `best`
    and under `consume-first`.

    This is the negative oracle for the superseded "pre-sort the candidate
    list" design (mutant M-1). Python's sort is stable, so list order survives
    only an exact tie of the strategy key - and neither `(-h,)` nor
    `(reset_ts, -h)` ties here by construction. A pre-sort implementation
    fails both of these; a leading tier passes both.
    """

    def test_best_prefers_the_pin_over_more_headroom(self, temp_home):
        h = _fleet(temp_home, n=3)
        _pin(h, a2=1)
        t = h.clock.now
        outcome, _ = _tick(h, [{
            "1": _u(95.0, seven_reset=t + 10_000),
            "2": _u(60.0, seven_reset=t + 400_000),  # less headroom, later reset
            "3": _u(20.0, seven_reset=t + 50_000),   # more headroom, sooner reset
        }])
        assert outcome is TickOutcome.SWITCHED
        assert _dest(h) == [2]

    def test_consume_first_prefers_the_pin_over_a_sooner_reset(self, temp_home):
        h = _fleet(temp_home, n=3, strategy="consume-first")
        _pin(h, a2=1)
        t = h.clock.now
        outcome, _ = _tick(h, [{
            "1": _u(50.0, seven_reset=t + 500_000),
            "2": _u(60.0, seven_reset=t + 400_000),
            "3": _u(20.0, seven_reset=t + 50_000),
        }])
        assert outcome is TickOutcome.SWITCHED
        assert _dest(h) == [2]

    def test_the_strategy_keys_do_not_tie_in_either_fixture(self, temp_home):
        """Proves the oracle above is discriminating rather than lucky.

        If the two candidates tied on the strategy key, a stable sort over a
        pre-sorted list would produce the same destination and mutant M-1
        would survive. They do not tie: headroom differs by 40 points and the
        weekly resets differ by 350 000 seconds.
        """
        h = _fleet(temp_home, n=3)
        ordered, _known, _reset = h.engine._rank_candidates(
            **_rank_kwargs(h, {"1": 5.0, "2": 40.0, "3": 80.0})
        )
        assert ordered == ["3", "2"], (
            "unpinned, the strategy strictly prefers 3 - so a pin that lands 2 "
            "first cannot have come from a tie"
        )


# ---------------------------------------------------------------------------
# AC-20 - the `fallback` list carries the tier too
# ---------------------------------------------------------------------------


class TestFallbackListCarriesTheTier:
    """AC-20 - **mutant M-2's oracle**.

    `_rank_candidates` builds two lists and then does
    `qualifying = qualifying or fallback` (`autoswitch.py:2266`). That
    **selects** one list; it never merges them. So prefixing the tier only at
    the `qualifying.append` site silently drops order on every tick that lands
    in the one-way fallback - and those are exactly the ticks of a nearly spent
    fleet, where a user who pinned an account most wants the pin honoured.

    Reaching the fallback needs all of its guards at once
    (`autoswitch.py:2196-2205`): `all_above` with `by_recovery` false, the
    candidate inside the 2x headroom ratio, the active at or under
    `SPENT_HEADROOM_PCT`, the candidate no worse than the active, and its
    binding window returning more than `RECOVERY_HYSTERESIS_S` sooner.

    The fixture is built so the two key shapes **disagree**: on the fallback
    key `(0, recovery_ts, -h)` account 3 wins on recovery, while on the
    ordinary `all_above` key `(1, -h, recovery_ts)` account 2 would win on
    headroom. So `ordered == ["3", "2"]` is itself the proof that the fallback
    list is what got sorted - no private state needs to be inspected.
    """

    @staticmethod
    def _snapshot(t: float) -> dict:
        return {
            # active: 3.0 points left (== SPENT_HEADROOM_PCT), window days out
            "1": _u(97.0, five_reset=t + 500_000),
            # 5.0 points - MORE headroom, but back later
            "2": _u(95.0, five_reset=t + 100_000),
            # 3.5 points - less headroom, back soonest: the fallback winner
            "3": _u(96.5, five_reset=t + 50_000),
        }

    def test_the_fixture_really_takes_the_fallback_path(self, temp_home):
        """Guards the oracle. If `qualifying` were non-empty the fallback list
        would never be consulted and this whole class would be vacuous - and
        the qualifying key shape would have put account 2 first."""
        h = _fleet(temp_home, n=3)
        t = h.clock.now
        ordered, _known, _reset = h.engine._rank_candidates(
            **_rank_kwargs(
                h,
                {"1": 3.0, "2": 5.0, "3": 3.5},
                usage=self._snapshot(t),
                oauth_candidates=["2", "3"],
            )
        )
        assert ordered == ["3", "2"], (
            "recovery-ordered, not headroom-ordered: anything else means the "
            "fixture stopped reaching the one-way fallback"
        )

    def test_a_pin_wins_on_the_fallback_path(self, temp_home):
        h = _fleet(temp_home, n=3)
        _pin(h, a2=1)
        t = h.clock.now
        outcome, _ = _tick(h, [self._snapshot(t)])
        assert outcome is TickOutcome.SWITCHED
        assert _dest(h) == [2]

    def test_the_same_fallback_fleet_unpinned_picks_the_sooner_recovery(
        self, temp_home
    ):
        h = _fleet(temp_home, n=3)
        t = h.clock.now
        outcome, _ = _tick(h, [self._snapshot(t)])
        assert outcome is TickOutcome.SWITCHED
        assert _dest(h) == [3]


# ---------------------------------------------------------------------------
# AC-21 / AC-21b - default identity: an unpinned fleet is untouched
# ---------------------------------------------------------------------------


class TestDefaultIdentityObserved:
    """AC-21 - with nothing pinned, a real tick emits the **same destination**
    as the pre-PR2 engine, across all three key shapes.

    The three constants below are **golden destinations captured from the
    pre-change engine** - recorded by running these exact fixtures against
    `autoswitch.py` before `OrderMap` existed
    (receipt: `C:/Temp/cswap/ord21-probe.txt`, fixtures archived at
    `C:/Temp/cswap/ord21-probe-fixtures.py`). They are deliberately NOT
    re-derived from the new engine, which would make the test self-referential
    and unable to fail.

    Each fixture is built so the three key shapes pick three different
    accounts - headroom order, binding-recovery order and weekly-reset order
    are three different permutations - so a shape-dependent regression moves at
    least one destination instead of hiding behind an accidental agreement.
    """

    GOLDEN_ALL_ABOVE = 3    # soonest binding recovery
    GOLDEN_CONSUME_FIRST = 2  # soonest weekly reset
    GOLDEN_BEST = 4         # most headroom

    def test_all_above_tiered_key_is_unchanged(self, temp_home):
        h = _fleet(temp_home)
        t = h.clock.now
        outcome, _ = _tick(h, [{
            "1": _u(95.0, five_reset=t + 5_000, seven_reset=t + 300_000),
            "2": _u(92.0, five_reset=t + 3_600, seven_reset=t + 50_000),
            "3": _u(93.0, five_reset=t + 1_800, seven_reset=t + 200_000),
            "4": _u(91.0, five_reset=t + 7_200, seven_reset=t + 100_000),
        }])
        assert outcome is TickOutcome.SWITCHED
        assert _dest(h) == [self.GOLDEN_ALL_ABOVE]

    def test_consume_first_key_is_unchanged(self, temp_home):
        h = _fleet(temp_home, strategy="consume-first")
        t = h.clock.now
        outcome, _ = _tick(h, [{
            "1": _u(50.0, seven_reset=t + 500_000),
            "2": _u(40.0, seven_reset=t + 50_000),
            "3": _u(30.0, seven_reset=t + 100_000),
            "4": _u(20.0, seven_reset=t + 200_000),
        }])
        assert outcome is TickOutcome.SWITCHED
        assert _dest(h) == [self.GOLDEN_CONSUME_FIRST]

    def test_best_key_is_unchanged(self, temp_home):
        h = _fleet(temp_home)
        t = h.clock.now
        outcome, _ = _tick(h, [{
            "1": _u(95.0, five_reset=t + 5_000),
            "2": _u(40.0, seven_reset=t + 50_000),
            "3": _u(30.0, seven_reset=t + 200_000),
            "4": _u(20.0, seven_reset=t + 100_000),
        }])
        assert outcome is TickOutcome.SWITCHED
        assert _dest(h) == [self.GOLDEN_BEST]

    def test_the_three_goldens_are_distinct(self):
        """If two shapes agreed on a destination, one of the three tests above
        would pass under a regression that swapped them."""
        assert len({
            self.GOLDEN_ALL_ABOVE,
            self.GOLDEN_CONSUME_FIRST,
            self.GOLDEN_BEST,
        }) == 3


class TestDefaultIdentityStructural:
    """AC-21b - structural: with nothing pinned, the whole `ordered` list is
    element-for-element identical to the same call with `orders=None`.

    AC-21 covers the head of the list, which is the only part a destination can
    see. A regression that reorders positions 2..n is invisible to every event
    the engine emits, and it is not hypothetical: it is what the fleet lands on
    the moment the head is unreachable (quarantine, a failed `_perform`, the
    no-return bar on the next tick). Legitimate as a direct call here and only
    here, because AC-17 has already pinned that `_rank_candidates` is pure.
    """

    @pytest.mark.parametrize(
        "headroom",
        [
            {"1": 5.0, "2": 40.0, "3": 80.0, "4": 60.0},
            {"1": 5.0, "2": 80.0, "3": 40.0, "4": 60.0},
            {"1": 5.0, "2": 22.0, "3": 33.0, "4": 27.0},
        ],
    )
    def test_an_unpinned_fleet_ranks_identically_with_and_without_the_map(
        self, temp_home, headroom
    ):
        h = _fleet(temp_home)
        empty = h.engine._resolve_orders()
        without = h.engine._rank_candidates(**_rank_kwargs(h, headroom))
        with_map = h.engine._rank_candidates(
            **_rank_kwargs(h, headroom, orders=empty)
        )
        assert with_map == without
        assert len(with_map[0]) >= 2, "a one-element list cannot show a reorder"

    def test_the_tail_and_not_just_the_head_is_compared(self, temp_home):
        """Proves the assertion above has something to bite on: a pin visibly
        rewrites the tail as well as the head."""
        h = _fleet(temp_home)
        _pin(h, a4=1)
        headroom = {"1": 5.0, "2": 40.0, "3": 80.0, "4": 20.0}
        without = h.engine._rank_candidates(**_rank_kwargs(h, headroom))[0]
        pinned = h.engine._rank_candidates(
            **_rank_kwargs(h, headroom, orders=h.engine._resolve_orders())
        )[0]
        assert without == ["3", "2", "4"]
        assert pinned == ["4", "3", "2"]


# ---------------------------------------------------------------------------
# AC-22 - composition with `backup`: a rank must not defeat the reserve filter
# ---------------------------------------------------------------------------


class TestCompositionWithBackup:
    """AC-22 - **mutant M-3's oracle**.

    The reserve exclusion is a **two-pass filter** in the caller
    (`autoswitch.py:1341-1346`), not a sort key, precisely so that no ranking
    trick can promote a reserve while a primary is still usable. A pin is a
    ranking trick. `order: 1` on a backup account must therefore lose to a
    strictly worse primary in pass 1, and only order the reserves among
    themselves in pass 2.

    The mutant this kills is the collapse of the two-pass filter to
    `primaries = kw["oauth_candidates"]`, which would rank the full set once
    with the tier applied. (The plan's original wording - "apply the tier
    before the partition" - named a non-mutation: the partition builds its
    membership list in the caller, so nothing upstream of it can reintroduce a
    filtered member.)
    """

    def test_a_pinned_reserve_still_loses_to_a_worse_primary(self, temp_home):
        h = _fleet(temp_home, n=3)
        _backup(h, 3)
        _pin(h, a3=1)
        outcome, _ = _tick(h, [{
            "1": _u(95.0),  # active, above the line
            "2": _u(60.0),  # the only primary - strictly worse than the reserve
            "3": _u(5.0),   # reserve, pinned rank 1, and the most attractive
        }])
        assert outcome is TickOutcome.SWITCHED
        assert _dest(h) == [2]

    def test_the_pin_orders_the_reserves_among_themselves_in_pass_two(
        self, temp_home
    ):
        """The positive half - pass 2 is where a reserve's rank is allowed to
        matter, and it does."""
        h = _fleet(temp_home)
        _backup(h, 3, 4)
        _pin(h, a4=1)
        outcome, _ = _tick(h, [{
            "1": _u(95.0),   # active
            "2": _u(100.0),  # the only primary, at its limit -> pass 1 empty
            "3": _u(3.0),    # reserve, unpinned, strictly MORE headroom (97)
            "4": _u(5.0),    # reserve, pinned rank 1, strictly worse (95)
        }])
        assert outcome is TickOutcome.SWITCHED
        assert _dest(h) == [4]

    def test_without_the_pin_pass_two_picks_the_better_reserve(self, temp_home):
        h = _fleet(temp_home)
        _backup(h, 3, 4)
        outcome, _ = _tick(h, [{
            "1": _u(95.0), "2": _u(100.0), "3": _u(3.0), "4": _u(5.0),
        }])
        assert outcome is TickOutcome.SWITCHED
        assert _dest(h) == [3]


# ---------------------------------------------------------------------------
# AC-23 - composition with the consume-first two-phase commit
# ---------------------------------------------------------------------------


class TestCompositionWithTheTwoPhaseCommit:
    """AC-23 - both phases rank against the same map; a pin cannot change
    between them.

    The consume-first commit re-fetches and re-ranks before it switches
    (`autoswitch.py:1189`). The fixture below makes phase 2 disagree with phase
    1 on headroom, so the two phases genuinely rank different numbers - and the
    pinned account is still the destination.
    """

    def test_the_pin_holds_across_a_phase_two_that_moved(self, temp_home):
        h = _fleet(temp_home, n=3, strategy="consume-first")
        _pin(h, a2=1)
        t = h.clock.now
        phase1 = {
            "1": _u(50.0, seven_reset=t + 500_000),
            "2": _u(60.0, seven_reset=t + 400_000),
            "3": _u(20.0, seven_reset=t + 50_000),
        }
        phase2 = {
            "1": _u(55.0, seven_reset=t + 500_000),
            "2": _u(70.0, seven_reset=t + 400_000),  # burned further
            "3": _u(10.0, seven_reset=t + 50_000),   # recovered
        }
        outcome, mock = _tick(h, [phase1, phase1, phase2])
        assert outcome is TickOutcome.SWITCHED
        assert _dest(h) == [2]
        assert mock.call_count >= 2, "the two-phase commit must have re-fetched"

    def test_the_same_two_phase_fleet_unpinned_follows_the_strategy(
        self, temp_home
    ):
        h = _fleet(temp_home, n=3, strategy="consume-first")
        t = h.clock.now
        phase1 = {
            "1": _u(50.0, seven_reset=t + 500_000),
            "2": _u(60.0, seven_reset=t + 400_000),
            "3": _u(20.0, seven_reset=t + 50_000),
        }
        phase2 = {
            "1": _u(55.0, seven_reset=t + 500_000),
            "2": _u(70.0, seven_reset=t + 400_000),
            "3": _u(10.0, seven_reset=t + 50_000),
        }
        outcome, _ = _tick(h, [phase1, phase1, phase2])
        assert outcome is TickOutcome.SWITCHED
        assert _dest(h) == [3]


# ---------------------------------------------------------------------------
# AC-24 - one anchor: a pinned fleet reaches a fixed point, never a cycle
# ---------------------------------------------------------------------------


class TestOneAnchorAntiFlap:
    """AC-24 - four ticks over a pinned fleet reach a fixed point.

    Upstream PR #260 measured a real `1 -> 2 -> 1 -> 3 -> 1 -> 2` cycle from
    running two spend-order anchors at once. A static pin cannot disagree with
    itself, so the engine must settle. **Four ticks minimum** - a two-tick test
    cannot tell a fixed point from the first half of a period-2 cycle, which is
    the lesson PR 1's AC-45 recorded.
    """

    def test_four_ticks_over_a_pinned_fleet_settle(self, temp_home):
        h = _fleet(temp_home, n=3)
        _pin(h, a2=1)
        snapshot = {"1": _u(95.0), "2": _u(60.0), "3": _u(20.0)}
        seen: list[int | None] = []
        for _ in range(4):
            _tick(h, [snapshot])
            seen.append(h.active_number())
            h.clock.advance(AutoSwitchSettings().cooldown_seconds + 60.0)
        assert seen[0] == 2
        assert seen == [2, 2, 2, 2], f"expected a fixed point, saw {seen}"

    def test_the_run_emits_exactly_one_switch(self, temp_home):
        """The stronger statement: after landing on the pin the engine stops
        rewriting credentials, rather than switching 2 -> 2 four times."""
        h = _fleet(temp_home, n=3)
        _pin(h, a2=1)
        snapshot = {"1": _u(95.0), "2": _u(60.0), "3": _u(20.0)}
        for _ in range(4):
            _tick(h, [snapshot])
            h.clock.advance(AutoSwitchSettings().cooldown_seconds + 60.0)
        assert _dest(h) == [2]


# ---------------------------------------------------------------------------
# AC-25 - order affects SELECTION only; every gate is unchanged
# ---------------------------------------------------------------------------


class TestOrderAffectsSelectionOnly:
    """AC-25 - the tier is applied at the `append` sites, downstream of every
    filter, so no gate can be bought with a pin.

    A pin is a preference among *choosable* accounts. It is not a licence to
    land on an account that is over its own threshold, to skip the hysteresis
    margin, or to return to the account this tick just left.
    """

    def test_a_pinned_account_over_its_own_threshold_is_still_rejected(
        self, temp_home
    ):
        h = _fleet(temp_home, n=3)
        _pin(h, a2=1)
        h.switcher.set_account_threshold("2", 50.0)
        outcome, _ = _tick(h, [{
            "1": _u(95.0),
            "2": _u(60.0),  # over its OWN 50% line -> not a landing site
            "3": _u(20.0),
        }])
        assert outcome is TickOutcome.SWITCHED
        assert _dest(h) == [3]

    def test_a_pinned_account_is_absent_from_ordered_when_the_gate_rejects_it(
        self, temp_home
    ):
        """The list-level statement of the same rule: rejected means absent,
        not merely last."""
        h = _fleet(temp_home, n=3)
        _pin(h, a2=1)
        h.switcher.set_account_threshold("2", 50.0)
        kwargs = _rank_kwargs(
            h,
            {"1": 5.0, "2": 40.0, "3": 80.0},
            thresholds=h.engine._resolve_thresholds(),
            orders=h.engine._resolve_orders(),
        )
        ordered, _known, _reset = h.engine._rank_candidates(**kwargs)
        assert ordered == ["3"]

    def test_a_pin_does_not_buy_the_hysteresis_margin(self, temp_home):
        h = _fleet(temp_home, n=3)
        _pin(h, a2=1)
        outcome, _ = _tick(h, [{
            "1": _u(88.0),  # below the line -> `best` needs a real margin
            "2": _u(85.0),  # only 3 points better; hysteresis_pct is 10
            "3": _u(84.0),
        }])
        assert outcome is TickOutcome.NO_ACTION
        assert _dest(h) == []
        assert [e.reason for e in h.events if isinstance(e, NoSwitchEvent)] == [
            "below-threshold"
        ]


# ---------------------------------------------------------------------------
# AC-26 - API-key candidates are pre-sorted by rank before the last resort
# ---------------------------------------------------------------------------


class TestApiKeyCandidatesArePreSorted:
    """AC-26 - here order genuinely **is** a list pre-sort.

    The last-resort assignment at `autoswitch.py:1459-1460` performs no
    ranking at all: metered API-key accounts have no measurable headroom and no
    weekly window, so there is no sort key to prepend a tier to. Ordering them
    therefore means sorting the list itself - and an unpinned API-key fleet
    must keep today's sequence order exactly, which a stable sort guarantees.
    """

    def test_the_pinned_api_key_account_is_reached_first(self, temp_home):
        h = _fleet(temp_home, include_api_key_accounts=True)
        _api_key(h, 2, 3, 4)
        _pin(h, a3=1)
        outcome, _ = _tick(h, [{
            "1": _u(95.0), "2": _u(10.0), "3": _u(10.0), "4": _u(10.0),
        }])
        assert outcome is TickOutcome.SWITCHED
        assert _dest(h) == [3]

    def test_an_unpinned_api_key_fleet_keeps_sequence_order(self, temp_home):
        h = _fleet(temp_home, include_api_key_accounts=True)
        _api_key(h, 2, 3, 4)
        outcome, _ = _tick(h, [{
            "1": _u(95.0), "2": _u(10.0), "3": _u(10.0), "4": _u(10.0),
        }])
        assert outcome is TickOutcome.SWITCHED
        assert _dest(h) == [2]

    def test_ranks_order_the_api_key_fleet_among_themselves(self, temp_home):
        h = _fleet(temp_home, include_api_key_accounts=True)
        _api_key(h, 2, 3, 4)
        _pin(h, a4=1, a2=2)
        outcome, _ = _tick(h, [{
            "1": _u(95.0), "2": _u(10.0), "3": _u(10.0), "4": _u(10.0),
        }])
        assert outcome is TickOutcome.SWITCHED
        assert _dest(h) == [4]

    def test_a_pinned_api_key_reserve_is_still_reached_last(self, temp_home):
        """Composition with AC-22's rule on the API-key path: the same
        two-pass filter applies there (`autoswitch.py:1459`), so a rank must
        not promote a reserve here either."""
        h = _fleet(temp_home, include_api_key_accounts=True)
        _api_key(h, 2, 3)
        _backup(h, 3)
        _pin(h, a3=1)
        outcome, _ = _tick(h, [{
            "1": _u(95.0), "2": _u(10.0), "3": _u(10.0), "4": _u(100.0),
        }])
        assert outcome is TickOutcome.SWITCHED
        assert _dest(h) == [2]


# ---------------------------------------------------------------------------
# AC-27 - usage scheduling sees no order input at all
# ---------------------------------------------------------------------------


class TestSchedulingIsUntouched:
    """AC-27 - `_collect_scheduled_usage`, `poll_policy` and `_next_delay` see
    no order input.

    Cadence is threshold-driven. A pin says *where to go*, never *how often to
    look*, and the whole poll plan is computed before ranking even runs
    (`autoswitch.py:1050-1053`), so it must be identical between a pinned and
    an unpinned run of the same fleet - **even though the pin changes the
    destination**, which is what makes the comparison worth making.

    **The observable, resolved.** The plan named
    `UsageEntry.next_poll_at` / `.poll_interval_s`. Those fields are written by
    the *collector* (`switcher.usage_entries_by_account`), which every engine
    fixture must patch out to stay off the network, so under a synthetic
    fixture nothing is ever persisted and the comparison is `{} == {}` - a
    guard assertion caught exactly that. The one path that does persist a plan
    in-process is `_replan_after_switch`, and it writes for the account just
    switched TO, so it varies with the destination *by design* - that is a
    consequence of the switch, not an order input, and asserting it equal would
    be asserting something false.

    So (i) binds to the plan the engine **computed and handed the collector to
    persist**: the `fetch` set and `scheduled` flag of every collector call in
    the tick. That is `_collect_scheduled_usage`'s entire scheduling output,
    and it is what an `orders`-aware scheduler would have to change. No event
    can carry it either - `PollEvent` (`autoswitch.py:305-329`) has fields
    `active`, `headroom`, `threshold`, `fetch_errors` and `windows` and no
    interval field at all. Assertion (iii) is what makes (i) and (ii) more than
    a coincidence of one fixture.
    """

    SNAPSHOT_UTILIZATION = {"1": 95.0, "2": 60.0, "3": 20.0}

    def _run(self, tmp_path, *, pinned: bool):
        """One full tick on its OWN home, recording the collector call plan.

        Two harnesses on one `temp_home` share a store, which would leave the
        second run reading the first run's pin and active account - the
        comparison would then be between two different fleets rather than
        between two policies.
        """
        calls: list[tuple] = []
        with _isolated_home(tmp_path, "pinned" if pinned else "unpinned") as home:
            h = _fleet(home, n=3)
            if pinned:
                _pin(h, a2=1)
            entries = {
                num: _entry_for(_u(pct), h.clock.now)
                for num, pct in self.SNAPSHOT_UTILIZATION.items()
            }

            def _fetch(**kwargs):
                calls.append((
                    frozenset(kwargs.get("fetch") or ()),
                    kwargs.get("scheduled"),
                ))
                return entries

            with patch.object(
                h.switcher, "usage_entries_by_account", side_effect=_fetch
            ):
                outcome = h.engine.tick()
            return h, outcome, calls

    def test_the_pin_changes_the_destination_in_this_fixture(self, tmp_path):
        """Guards the comparisons below: if the pin were inert here they would
        compare two identical runs and prove nothing."""
        pinned, _o, _c = self._run(tmp_path, pinned=True)
        unpinned, _o2, _c2 = self._run(tmp_path, pinned=False)
        assert _dest(pinned) == [2]
        assert _dest(unpinned) == [3]

    def test_the_computed_poll_plan_is_identical(self, tmp_path):
        _h_p, outcome_p, calls_pinned = self._run(tmp_path, pinned=True)
        _h_u, outcome_u, calls_unpinned = self._run(tmp_path, pinned=False)
        assert outcome_p is TickOutcome.SWITCHED
        assert outcome_u is TickOutcome.SWITCHED
        assert calls_pinned, "the tick must have planned at least one fetch"
        assert calls_pinned == calls_unpinned

    def test_the_loop_delay_is_identical(self, tmp_path):
        """`_next_delay` applies a +/-10% jitter, so `random.random` is pinned
        to a constant - otherwise the two runs differ for a reason that has
        nothing to do with order."""
        with patch.object(_autoswitch_module.random, "random", return_value=0.5):
            pinned, outcome_p, _c = self._run(tmp_path, pinned=True)
            delay_pinned = pinned.engine._next_delay(outcome_p)
            unpinned, outcome_u, _c2 = self._run(tmp_path, pinned=False)
            delay_unpinned = unpinned.engine._next_delay(outcome_u)
        assert delay_pinned == delay_unpinned
        assert delay_pinned > 0.0

    def test_no_scheduling_entry_point_accepts_an_orders_parameter(self):
        engine_fns = [
            _autoswitch_module.AutoSwitchEngine._collect_scheduled_usage,
            _autoswitch_module.AutoSwitchEngine._next_delay,
            _autoswitch_module.AutoSwitchEngine._refresh_poll_policy_inputs,
        ]
        policy_fns = [
            obj
            for _name, obj in vars(poll_policy).items()
            if inspect.isfunction(obj) and obj.__module__ == poll_policy.__name__
        ]
        assert policy_fns, "poll_policy must expose functions to check"
        for fn in engine_fns + policy_fns:
            params = inspect.signature(fn).parameters
            assert "orders" not in params, f"{fn.__qualname__} took an orders param"

    def test_no_scheduling_source_references_the_order_map(self):
        for fn in (
            _autoswitch_module.AutoSwitchEngine._collect_scheduled_usage,
            _autoswitch_module.AutoSwitchEngine._next_delay,
            _autoswitch_module.AutoSwitchEngine._refresh_poll_policy_inputs,
        ):
            src = inspect.getsource(fn)
            assert "OrderMap" not in src, f"{fn.__qualname__} references OrderMap"
            assert "_resolve_orders" not in src, (
                f"{fn.__qualname__} resolves orders"
            )
        # `poll_policy.py` contains the substring "order" zero times today.
        # Keeping it that way is the cheapest possible statement that cadence
        # and order never met.
        assert "order" not in inspect.getsource(poll_policy), (
            "poll_policy must carry no order concept at all"
        )

    def test_random_is_the_module_the_jitter_comes_from(self):
        """Pins the patch target above: if `_next_delay` stopped using
        `autoswitch.random`, the jitter would silently return and the delay
        comparison would become flaky rather than failing."""
        assert _autoswitch_module.random is _random_module
        assert "random.random()" in inspect.getsource(
            _autoswitch_module.AutoSwitchEngine._next_delay
        )
