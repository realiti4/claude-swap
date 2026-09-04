"""MEU-PAP-04 - backup ("last man standing") accounts and the `failback` trigger.

Covers AC-24..AC-32 (the two-pass candidate filter) plus AC-43..AC-46 and
AC-48..AC-52 (failback).

Every failback oracle drives a **real tick** - `_tick_inner` -> `_rank` ->
`_perform` - rather than calling `_rank_candidates` in isolation, and asserts on
the emitted **trigger string**, because a helper-level assertion passes against
the exact under-implementation each oracle exists to catch. Eleven oracles are
negative: ten have a named mutant in the plan's Mutation-discrimination gate and
are proved discriminating in H1-13b; AC-44's reference implementation is today's
unmodified engine.

Three outcomes all report "no switch" - `NO_ACTION`, `BLOCKED` and `ERROR` - so
every hold assertion names the **reason string and the `TickOutcome`**, never
merely that no switch occurred. The whole failback contract is that a tick which
returns `NO_ACTION` today must not return `BLOCKED` after this PR.

Synthetic fixtures only: `EngineHarness` seeds a throwaway store under
`temp_home`. Nothing here reads a real account store.
"""

from __future__ import annotations

import pytest
from unittest.mock import patch

from claude_swap import oauth
from claude_swap.autoswitch import _headroom_by_account
from claude_swap.autoswitch import (
    AllExhaustedEvent,
    ErrorEvent,
    NoSwitchEvent,
    QuarantineEvent,
    SwitchEvent,
    TickOutcome,
)
from claude_swap.settings import AutoSwitchSettings
from claude_swap.usage_store import SERVE_TTL_S, UsageEntry

from tests.test_autoswitch import EngineHarness, _entry_for, _iso_at, _usage

GLOBAL = AutoSwitchSettings().threshold  # 90.0
COOLDOWN = AutoSwitchSettings().cooldown_seconds  # 300.0

# `_collect_scheduled_usage` calls the collector twice before any escalation:
# a `fetch=set()` pre-read to build the poll plan, then the plan fetch. A
# `failback` tick's row-3 forced refetch is therefore the THIRD call, and an
# oracle that wants phase 1 and phase 2 to disagree must repeat its first
# snapshot twice. Asserting `== 1` here would be vacuous.
BASE_CALLS = 2

_EMAILS = {1: "a@example.com", 2: "b@example.com", 3: "c@example.com",
           4: "d@example.com"}


def _fleet(temp_home, n: int = 3, **settings_kwargs) -> EngineHarness:
    """`n` seeded OAuth accounts, account 1 live and active."""
    h = EngineHarness(temp_home, **settings_kwargs)
    for num in range(1, n + 1):
        h.seed(num, _EMAILS[num])
    h.make_live(_EMAILS[1], 1)
    return h


def _u(pct: float, *, seven_day_reset: float | None = None,
       seven_day_pct: float = 0.0, five_hour_reset: float | None = None) -> dict:
    """A usage row with an addressable **weekly** reset.

    `tests.test_autoswitch._usage` puts its `resets_at` on the five-hour
    window, but `_seven_day_reset_ts` - the axis consume-first and branch row
    11 rank on - reads `seven_day`. Both windows are settable here so a
    fixture can drive the weekly filter and the binding-recovery tier
    independently.
    """
    five: dict = {"pct": pct}
    if five_hour_reset is not None:
        five["resets_at"] = _iso_at(five_hour_reset)
    seven: dict = {"pct": seven_day_pct}
    if seven_day_reset is not None:
        seven["resets_at"] = _iso_at(seven_day_reset)
    return {"five_hour": five, "seven_day": seven}


def _stale(pct: float, now: float, age: float = 240.0) -> UsageEntry:
    """A readable but un-refreshable row: still decision-trusted, not `fresh`.

    The default age sits in the gap between `SERVE_TTL_S` (180 - the freshness
    bar branch row 7 applies) and `STALE_OK_S` (300 - the trust bar
    `decision_value` applies). Anything past 300 reads as unknown headroom, so
    the candidate would never rank and row 7 would never be reached.
    """
    assert SERVE_TTL_S < age < 300.0
    return UsageEntry(
        last_good=_u(pct), fetched_at=now - age, age_s=age,
    )


def _backup(h: EngineHarness, *nums: int) -> None:
    for num in nums:
        h.switcher.set_account_backup(str(num), True)


def _api_key(h: EngineHarness, *nums: int) -> None:
    data = h.switcher._get_sequence_data()
    for num in nums:
        data["accounts"][str(num)]["kind"] = "api_key"
    h.switcher._write_json(h.switcher.sequence_file, data)


def _reasons(h: EngineHarness) -> list[str]:
    return [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]


def _details(h: EngineHarness) -> list[str]:
    return [e.detail for e in h.events if isinstance(e, NoSwitchEvent)]


def _switches(h: EngineHarness) -> list[SwitchEvent]:
    return [e for e in h.events if isinstance(e, SwitchEvent)]


def _triggers(h: EngineHarness) -> list[str]:
    return [e.trigger for e in _switches(h)]


def _tick(h: EngineHarness, snapshots: list[dict]):
    """Drive one real tick, serving `snapshots` to successive collector calls.

    The last snapshot repeats, so a fixture only lists as many as it needs to
    distinguish. Returns `(outcome, mock)` - the call count is itself an
    oracle for branch row 3's forced refetch.
    """
    seq = [
        {
            num: value if isinstance(value, UsageEntry)
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


class TestBackupIsNeverPreferred:
    """AC-24 - a backup account is skipped while any non-backup qualifies.

    The fixture deliberately makes the backup the *most* attractive account on
    both ranking axes - strictly more headroom and a sooner reset - because
    that is the case an `order: 999` sort-key implementation gets wrong: under
    `best` and `consume-first` the order field is not the sort key, so the
    reserve burns exactly when it is freshest. A filter stage cannot be fooled
    that way; a sort tweak can.
    """

    def test_the_backup_loses_to_a_worse_non_backup_candidate(self, temp_home):
        h = _fleet(temp_home)
        _backup(h, 3)
        t = h.clock.now
        outcome, _ = _tick(h, [{
            "1": _u(95.0, seven_day_reset=t + 100_000),
            "2": _u(50.0, seven_day_reset=t + 90_000),
            "3": _u(5.0, seven_day_reset=t + 1_000),
        }])
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2
        assert _triggers(h) == ["proactive"]

    def test_without_the_mark_the_same_fleet_picks_that_account(self, temp_home):
        """The identity half: the mark is the only thing that moved."""
        h = _fleet(temp_home)
        t = h.clock.now
        outcome, _ = _tick(h, [{
            "1": _u(95.0, seven_day_reset=t + 100_000),
            "2": _u(50.0, seven_day_reset=t + 90_000),
            "3": _u(5.0, seven_day_reset=t + 1_000),
        }])
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3


class TestBackupIsPromotedWhenNothingElseQualifies:
    """AC-25 - the reserve is offered once no primary can be landed on."""

    def test_a_primary_at_its_limit_promotes_the_backup(self, temp_home):
        h = _fleet(temp_home)
        _backup(h, 3)
        outcome, _ = _tick(h, [{
            "1": _usage(95.0),   # active, above the line -> proactive
            "2": _usage(100.0),  # primary at its own limit -> never a target
            "3": _usage(5.0),    # the reserve
        }])
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3
        assert _triggers(h) == ["proactive"]

    def test_promotion_does_not_emit_all_exhausted(self, temp_home):
        """AC-28, positive half - the fleet is not exhausted while the
        reserve has quota, so the census must stay on the full OAuth set."""
        h = _fleet(temp_home)
        _backup(h, 3)
        outcome, _ = _tick(h, [{
            "1": _usage(95.0), "2": _usage(100.0), "3": _usage(5.0),
        }])
        assert outcome is TickOutcome.SWITCHED
        assert not [e for e in h.events if isinstance(e, AllExhaustedEvent)]


class TestExhaustionCensusStaysOnTheFullSet:
    """AC-28, negative half.

    `truly_exhausted` decides between `no-qualifying-candidate` and the
    bounded reset-aware `AllExhausted` sleep. Computed over primaries only, a
    fleet whose sole primary is spent looks exhausted while the reserve still
    holds 12 points - and the engine would sleep until a reset it does not
    need to wait for. Both legs return `BLOCKED`, so the oracle is the
    **event**, not the outcome.
    """

    def test_a_spent_primary_plus_a_live_backup_is_not_all_exhausted(
        self, temp_home
    ):
        h = _fleet(temp_home)
        _backup(h, 3)
        outcome, _ = _tick(h, [{
            "1": _usage(95.0),   # active, 5 points
            "2": _usage(100.0),  # spent primary
            "3": _usage(88.0),   # reserve: 12 points, but fails `best`
                                 # hysteresis (12 - 5 = 7 < 10), so nothing
                                 # qualifies and the census is reached
        }])
        assert outcome is TickOutcome.BLOCKED
        assert _reasons(h) == ["no-qualifying-candidate"]
        assert not [e for e in h.events if isinstance(e, AllExhaustedEvent)]


class TestAnAllBackupFleetStillSwitches:
    """AC-26 - degrade to "use them" rather than emitting `no-candidates`.

    Held-out-forever is the wrong failure mode: a user who marks every account
    backup has expressed a preference, not a prohibition.
    """

    def test_every_candidate_marked_backup_still_produces_a_switch(
        self, temp_home
    ):
        h = _fleet(temp_home)
        _backup(h, 2, 3)
        outcome, _ = _tick(h, [{
            "1": _usage(95.0), "2": _usage(50.0), "3": _usage(10.0),
        }])
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3          # most headroom, as usual
        assert "no-candidates" not in _reasons(h)


class TestAllAboveIsComputedOverPrimariesOnly:
    """AC-27 - pass 1's `all_above` census excludes the reserve.

    "Every non-backup account is above its own threshold" is the condition
    that should promote a reserve, and it is *not* "every account is above".
    A fresh backup inside the census makes `all_above` false, which re-arms
    the landing gate against a spent primary fleet and hands the tick to the
    reserve - the opposite of the policy, since a primary is still workable
    through its imminent reset.
    """

    def test_a_fresh_backup_does_not_suppress_the_recovery_path(
        self, temp_home
    ):
        h = _fleet(temp_home, n=4)
        _backup(h, 4)
        t = h.clock.now
        outcome, _ = _tick(h, [{
            # Active and both primaries above the global line -> all_above is
            # True over the primaries, so the recovery tier engages and picks
            # the account that can work again first.
            "1": _u(95.0, five_hour_reset=t + 40_000),
            "2": _u(92.0, five_hour_reset=t + 600),
            "3": _u(93.0, five_hour_reset=t + 50_000),
            "4": _u(5.0, five_hour_reset=t + 300),
        }])
        assert outcome is TickOutcome.SWITCHED
        # With the backup counted in the census `all_above` is False, the
        # landing gate rejects both primaries, and the tick lands on 4.
        assert h.active_number() == 2
        assert _triggers(h) == ["proactive"]


class TestTwoPassAppliesInBothCommitPhases:
    """AC-29 - the filter lives inside `_rank`, so the consume-first
    two-phase commit inherits it without duplication.

    Phase 1 sees a spent primary and promotes the reserve; the forced refetch
    shows the primary recovered. The reserve resets soonest of all three, so
    an unfiltered phase-2 ranking picks it - which is exactly the "promoted on
    stale data, disqualified on fresh data" case.
    """

    def test_the_reserve_promoted_on_stale_data_is_dropped_on_fresh_data(
        self, temp_home
    ):
        h = _fleet(temp_home, strategy="consume-first")
        _backup(h, 3)
        t = h.clock.now
        stale = {
            "1": _u(50.0, seven_day_reset=t + 100_000),
            "2": _u(100.0, seven_day_reset=t + 50_000),   # spent
            "3": _u(10.0, seven_day_reset=t + 10_000),    # reserve, soonest
        }
        fresh = {
            "1": _u(50.0, seven_day_reset=t + 100_000),
            "2": _u(20.0, seven_day_reset=t + 50_000),    # recovered
            "3": _u(10.0, seven_day_reset=t + 10_000),
        }
        outcome, mock = _tick(h, [stale, stale, fresh])
        assert outcome is TickOutcome.SWITCHED
        assert mock.call_count == BASE_CALLS + 1        # the forced refetch
        assert h.active_number() == 2
        assert _triggers(h) == ["consume-first"]


class TestApiKeyLastResortPrefersNonBackup:
    """AC-30 - the same filter, mirrored onto the last-resort list.

    PR #260 proved the API-key hole is real rather than theoretical: an
    asymmetry here means a fleet with one metered reserve burns it first.
    """

    def test_a_non_backup_api_key_account_wins(self, temp_home):
        h = _fleet(temp_home, include_api_key_accounts=True)
        _api_key(h, 2, 3)
        _backup(h, 2)
        outcome, _ = _tick(h, [{
            "1": _usage(95.0), "2": _usage(10.0), "3": _usage(10.0),
        }])
        assert outcome is TickOutcome.SWITCHED
        # Sequence order would take 2; the filter takes 3.
        assert h.active_number() == 3

    def test_a_backup_api_key_account_is_used_when_it_is_the_only_one(
        self, temp_home
    ):
        h = _fleet(temp_home, n=2, include_api_key_accounts=True)
        _api_key(h, 2)
        _backup(h, 2)
        outcome, _ = _tick(h, [{"1": _usage(95.0), "2": _usage(10.0)}])
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2


class TestDisabledWinsOverBackup:
    """AC-31 - `disabled` is applied first in `switchable_account_numbers()`,
    so a slot marked both appears in neither pass."""

    def test_a_disabled_backup_is_absent_from_the_backup_set(self, temp_home):
        h = _fleet(temp_home)
        h.switcher.set_account_disabled("2", True)
        _backup(h, 2)
        assert "2" not in h.switcher.backup_account_numbers()

    def test_a_disabled_backup_is_not_promoted_in_pass_two(self, temp_home):
        h = _fleet(temp_home)
        h.switcher.set_account_disabled("2", True)
        _backup(h, 2, 3)
        outcome, _ = _tick(h, [{
            "1": _usage(95.0), "2": _usage(1.0), "3": _usage(10.0),
        }])
        assert outcome is TickOutcome.SWITCHED
        # 2 holds far more headroom than 3 and would win any pass it entered.
        assert h.active_number() == 3


class TestDefaultIdentity:
    """AC-32 - with nothing marked backup the engine is bit-identical.

    The whole-suite proof is the MEU gate's `git diff tests/test_autoswitch.py`
    being empty while that file passes unchanged; these two assert the local
    half - no reserve exists, and no new trigger string is emitted.
    """

    def test_a_fresh_fleet_has_no_backup_accounts(self, temp_home):
        h = _fleet(temp_home)
        assert h.switcher.backup_account_numbers() == []

    def test_selection_and_trigger_are_unchanged(self, temp_home):
        h = _fleet(temp_home)
        outcome, _ = _tick(h, [{
            "1": _usage(95.0), "2": _usage(50.0), "3": _usage(5.0),
        }])
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3
        assert _triggers(h) == ["proactive"]
        assert "failback" not in _triggers(h)


class TestFailbackUnderBest:
    """AC-43 - a backup running as active departs for a healthy primary even
    though the comparative gates would refuse the move.

    This is the whole point of the trigger. The reserve was taken because
    nothing else qualified; once a primary recovers, the fleet must leave the
    reserve *without waiting for the reserve to fill up*. Under `best` that
    means branch row 12's hysteresis bar - here `30 - 80 = -50`, fifty points
    the wrong side of the 10-point margin - must not apply.
    """

    def _stage(self, temp_home):
        h = _fleet(temp_home, n=2)
        _backup(h, 1)
        h.switcher.set_account_threshold("2", 85.0)
        h.engine = h._make_engine()
        return h

    def test_it_departs_for_a_strictly_worse_primary(self, temp_home):
        h = self._stage(temp_home)
        outcome, _ = _tick(h, [{"1": _u(20.0), "2": _u(70.0)}])
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2
        assert _triggers(h) == ["failback"]

    def test_it_holds_when_the_primary_is_over_its_own_line(self, temp_home):
        """The landing gate still binds: 90 >= the account's own 85."""
        h = self._stage(temp_home)
        outcome, _ = _tick(h, [{"1": _u(20.0), "2": _u(90.0)}])
        assert outcome is TickOutcome.NO_ACTION
        assert _reasons(h) == ["below-threshold"]
        assert _details(h) == ["20% < 90%"]
        assert _switches(h) == []


class TestFailbackUnderConsumeFirst:
    """AC-44 - the trigger fires under `consume-first` too, and outranks that
    strategy's soonest-reset filter.

    The negative half is the reference implementation: today's engine, with no
    reserve marked, refuses exactly this move. That is why this AC has no
    named mutant in the discrimination gate - the unmodified engine *is* the
    mutant, and the two halves differ only by `set_account_backup`.
    """

    def _snapshot(self, h):
        t = h.clock.now
        return {
            "1": _u(20.0, seven_day_reset=t + 3_600),
            "2": _u(10.0, seven_day_reset=t + 36_000),
        }

    def test_failback_overrides_the_soonest_reset_filter(self, temp_home):
        h = _fleet(temp_home, n=2, strategy="consume-first")
        _backup(h, 1)
        outcome, _ = _tick(h, [self._snapshot(h)])
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2
        assert _triggers(h) == ["failback"]

    def test_without_a_reserve_the_same_fleet_stays_put(self, temp_home):
        h = _fleet(temp_home, n=2, strategy="consume-first")
        outcome, _ = _tick(h, [self._snapshot(h)])
        assert outcome is TickOutcome.NO_ACTION
        assert _reasons(h) == ["already-consuming-soonest"]
        assert _switches(h) == []


class TestFailbackDoesNotOscillate:
    """AC-45 - the trigger must fire once and then get out of the way.

    A failback that re-arms every tick would ping-pong the fleet between the
    reserve and a primary, which is worse than never leaving the reserve at
    all. All three oracles run >= 4 ticks or inspect the state the first tick
    wrote, because a single-tick assertion cannot see oscillation.
    """

    def test_four_quiet_ticks_produce_exactly_one_switch(self, temp_home):
        h = _fleet(temp_home, n=2)
        _backup(h, 1)
        h.switcher.set_account_threshold("2", 85.0)
        h.engine = h._make_engine()
        snap = {"1": _u(20.0), "2": _u(70.0)}
        for _ in range(4):
            _tick(h, [snap])
            h.clock.advance(COOLDOWN + 1)
        assert h.active_number() == 2
        assert _triggers(h) == ["failback"]
        assert _reasons(h) == ["below-threshold"] * 3

    def test_the_reserve_is_not_reclaimed_until_it_actually_recovers(
        self, temp_home
    ):
        """The no-return guard binds a failback departure like any other.

        The reserve's usage row is pinned byte-identical across both ticks, so
        it has not recovered on any axis the guard reads. The second tick's
        active must also stay close enough that the dominance leg
        (`h > active x HORIZON_HEADROOM_RATIO + SPENT_HEADROOM_PCT`) does not
        fire - that leg releases the bar because the ACTIVE burned down, which
        is correct engine behaviour and would make this oracle fail against
        correct code. Hence a 50% line and a 55% active rather than 95%.
        """
        h = _fleet(temp_home, n=2)
        _backup(h, 1)
        h.switcher.set_account_threshold("2", 50.0)
        h.engine = h._make_engine()
        reserve_row = _u(20.0)
        outcome, _ = _tick(h, [{"1": reserve_row, "2": _u(40.0)}])
        assert outcome is TickOutcome.SWITCHED
        # The store writes the slot number; normalise rather than assume a type.
        assert str(h.state()["lastSwitchFrom"]) == "1"
        assert h.state()["leftTrigger"] == "failback"
        assert h.state()["leftHeadroom"] == pytest.approx(80.0)

        h.clock.advance(COOLDOWN + 1)
        h.events.clear()
        usage = {"1": reserve_row, "2": _u(55.0)}
        headroom = _headroom_by_account(usage, h.engine._models)
        assert h.engine._left_account_recovered(
            h.state(), usage, headroom, headroom["2"], h.settings,
            h.clock(), "2", thresholds=h.engine._resolve_thresholds(h.settings),
        ) is False

        outcome, _ = _tick(h, [usage])
        assert outcome is TickOutcome.BLOCKED
        assert _reasons(h) == ["no-qualifying-candidate"]
        assert h.active_number() == 2

    def test_failback_is_not_added_to_the_no_return_trigger_set(
        self, temp_home
    ):
        """Branch row 8 - `_no_return_account` is keyed on the *previous*
        switch's epoch, so adding `failback` to its trigger tuple suppresses
        the very account the fleet should return to.

        Correct: 2 has the most headroom and wins. Defective: 2 is filtered
        out by the guard and the tick lands on 3 instead.
        """
        h = _fleet(temp_home)
        _backup(h, 1)
        h.engine._mutate_state(lambda s: s.update(
            lastSwitchFrom="2",
            lastSwitchTo="1",
            lastSwitchAt=h.clock() - 10_000,
            leftHeadroom=90.0,
            leftTrigger="failback",
        ))
        outcome, _ = _tick(h, [{
            "1": _u(20.0), "2": _u(10.0), "3": _u(60.0),
        }])
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2
        assert _triggers(h) == ["failback"]


class TestFailbackTriggerScope:
    """AC-46 - the predicate is store-only and narrow.

    It reads `backup`, `disabled`, `kind` and the quarantine set - never a
    usage number - so it is decidable at the departure gate before any
    ranking. Each sub-case below removes exactly one conjunct and asserts the
    tick collapses back to today's behaviour.
    """

    def test_a_no_reserve_marked(self, temp_home):
        h = _fleet(temp_home)
        outcome, _ = _tick(h, [{
            "1": _u(20.0), "2": _u(10.0), "3": _u(10.0),
        }])
        assert outcome is TickOutcome.NO_ACTION
        assert _reasons(h) == ["below-threshold"]
        assert _switches(h) == []

    def test_b_the_active_account_is_not_the_reserve(self, temp_home):
        h = _fleet(temp_home)
        _backup(h, 2)
        outcome, _ = _tick(h, [{
            "1": _u(20.0), "2": _u(10.0), "3": _u(10.0),
        }])
        assert outcome is TickOutcome.NO_ACTION
        assert _reasons(h) == ["below-threshold"]
        assert _switches(h) == []

    def test_c_every_account_is_a_reserve(self, temp_home):
        h = _fleet(temp_home, n=2)
        _backup(h, 1, 2)
        outcome, _ = _tick(h, [{"1": _u(20.0), "2": _u(10.0)}])
        assert outcome is TickOutcome.NO_ACTION
        assert _reasons(h) == ["below-threshold"]
        assert _switches(h) == []

    def test_c_the_only_primary_is_quarantined(self, temp_home):
        h = _fleet(temp_home, n=2)
        _backup(h, 1)
        h.engine._quarantine("2", _EMAILS[2], "invalid_grant")
        h.events.clear()
        outcome, _ = _tick(h, [{"1": _u(20.0), "2": _u(10.0)}])
        assert outcome is TickOutcome.NO_ACTION
        assert _reasons(h) == ["below-threshold"]
        assert _switches(h) == []

    def test_d_the_only_primary_is_an_api_key_account(self, temp_home):
        """The OAuth clause in the predicate is load-bearing.

        Without it the gate sets `failback`, the OAuth candidate list is
        empty, and the tick falls through to the `no-candidates` BLOCKED exit
        - turning a quiet hold into a hard block for every fleet whose
        reserve is its only OAuth account.
        """
        h = _fleet(temp_home, n=2)
        _backup(h, 1)
        _api_key(h, 2)
        outcome, _ = _tick(h, [{"1": _u(20.0), "2": _u(10.0)}])
        assert outcome is TickOutcome.NO_ACTION
        assert _reasons(h) == ["below-threshold"]
        assert outcome is not TickOutcome.BLOCKED

    def test_d_api_key_primary_with_the_flag_on_is_still_not_a_failback(
        self, temp_home
    ):
        """`include_api_key_accounts` widens the *candidate* list, not the
        predicate: the corrected predicate short-circuits at the departure
        gate, so `:1174`'s `no-candidates` exit is never reached either way.
        """
        h = _fleet(temp_home, n=2, include_api_key_accounts=True)
        _backup(h, 1)
        _api_key(h, 2)
        outcome, _ = _tick(h, [{"1": _u(20.0), "2": _u(10.0)}])
        assert outcome is TickOutcome.NO_ACTION
        assert _reasons(h) == ["below-threshold"]
        assert _switches(h) == []

    @pytest.mark.parametrize("strategy", ["best", "consume-first"])
    def test_e_the_trigger_fires_under_both_strategies(
        self, temp_home, strategy
    ):
        h = _fleet(temp_home, n=2, strategy=strategy)
        _backup(h, 1)
        outcome, _ = _tick(h, [{"1": _u(20.0), "2": _u(10.0)}])
        assert outcome is TickOutcome.SWITCHED
        assert _triggers(h) == ["failback"]

    def test_f_unreadable_primaries_hold_rather_than_report_no_comparison(
        self, temp_home
    ):
        """Placement oracle for branch row 5.

        The predicate is store-only, so it fires even when no primary has
        readable usage. The failback arm must sit ABOVE `if not any_known:`;
        below it, this tick reports `no-comparison`/BLOCKED - a regression
        from today's quiet `NO_ACTION`.
        """
        h = _fleet(temp_home, n=2)
        _backup(h, 1)
        outcome, _ = _tick(h, [{"1": _u(20.0), "2": None}])
        assert outcome is TickOutcome.NO_ACTION
        assert _reasons(h) == ["below-threshold"]
        assert "no-comparison" not in _reasons(h)


class TestFailbackRefetchesBeforeCommitting:
    """AC-48 - branch row 3 (the two-phase commit) and row 7 (the per-target
    freshness gate) ship together.

    A failback decision is taken on the *stored* snapshot, which may be
    minutes old. Acting on it without a forced refetch moves the fleet onto a
    primary that has since gone over its own line - the exact defect that
    makes an unconditional failback worse than no failback. The active
    reserve sits at 40%, well under `90 - 15`, so the escalation band stays
    shut and the third collector call can only be row 3's refetch.
    """

    def _stage(self, temp_home):
        h = _fleet(temp_home, n=2)
        _backup(h, 1)
        h.switcher.set_account_threshold("2", 85.0)
        h.engine = h._make_engine()
        return h

    def test_a_primary_that_went_over_its_line_is_dropped_on_refetch(
        self, temp_home
    ):
        h = self._stage(temp_home)
        phase1 = {"1": _u(40.0), "2": _u(50.0)}
        phase2 = {"1": _u(40.0), "2": _u(95.0)}
        outcome, mock = _tick(h, [phase1, phase1, phase2])
        assert mock.call_count == BASE_CALLS + 1
        assert outcome is TickOutcome.NO_ACTION
        assert _reasons(h) == ["below-threshold"]
        assert h.active_number() == 1

    def test_an_unrefreshable_target_holds_rather_than_commits(
        self, temp_home
    ):
        """Row 7: the refetch is best-effort, so a target that served its
        stored row must not be switched to on this tick."""
        h = self._stage(temp_home)
        now = h.clock.now
        phase1 = {"1": _u(40.0), "2": _u(50.0)}
        phase2 = {"1": _u(40.0), "2": _stale(50.0, now)}
        outcome, mock = _tick(h, [phase1, phase1, phase2])
        assert mock.call_count == BASE_CALLS + 1
        assert outcome is TickOutcome.NO_ACTION
        assert "stale-usage" in _reasons(h)
        assert _switches(h) == []


class TestFailbackRespectsCooldown:
    """AC-49 - both cooldown checks, not just the first.

    Branch rows 1 and 15 guard the same invariant at two depths: the tick
    entry point and `_perform`. Row 15 exists because a second engine
    instance - the TUI polling while the daemon runs - reaches `_perform`
    without passing row 1 in the same process.
    """

    def test_i_the_tick_entry_cooldown_suppresses_failback(self, temp_home):
        h = _fleet(temp_home, n=2)
        _backup(h, 1)
        h.engine._mutate_state(
            lambda s: s.update(lastSwitchAt=h.clock() - 1)
        )
        outcome, _ = _tick(h, [{"1": _u(20.0), "2": _u(10.0)}])
        assert outcome is TickOutcome.NO_ACTION
        assert _reasons(h) == ["cooldown"]
        assert _switches(h) == []

    def test_ii_a_second_engine_cannot_bypass_it_at_perform(self, temp_home):
        """The mutant that adds `failback` only at branch row 1 passes (i)
        and fails here."""
        h = _fleet(temp_home)
        _backup(h, 1)
        outcome, _ = _tick(h, [{
            "1": _u(20.0), "2": _u(10.0), "3": _u(10.0),
        }])
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2

        other = h._make_engine()
        h.events.clear()
        result = other._perform("3", _EMAILS[3], "failback", (80.0, 0.0))
        assert result is TickOutcome.NO_ACTION
        assert _reasons(h) == ["cooldown"]
        assert h.active_number() == 2


class TestFailbackKeepsTheLandingGate:
    """AC-50 - the reserve leaves only for a primary that is genuinely under
    its own line, and it never borrows the `all_above` relaxation.

    `all_above` exists so a fleet where *everything* is over its line still
    rotates rather than freezing. Under failback that relaxation is a bug: it
    would move the fleet off a quiet reserve onto an exhausted primary.
    Branch row 10's gate is therefore unconditional for this trigger.
    """

    def test_i_every_primary_over_its_own_line_means_no_landing_spot(
        self, temp_home
    ):
        h = _fleet(temp_home)
        _backup(h, 1)
        for num in ("2", "3"):
            h.switcher.set_account_threshold(num, 85.0)
        h.engine = h._make_engine()
        outcome, _ = _tick(h, [{
            "1": _u(20.0), "2": _u(90.0), "3": _u(88.0),
        }])
        assert outcome is TickOutcome.NO_ACTION
        assert _reasons(h) == ["below-threshold"]
        assert _switches(h) == []

    def test_ii_the_all_above_relaxation_is_not_borrowed(self, temp_home):
        """Phase 1 ranks a healthy primary, so the refetch happens; phase 2
        shows every account over its line.

        The mutant that keeps `and not all_above` on row 10's gate enters the
        relaxation here and switches. The reset timestamps below make the
        recovery sub-tier reachable rather than short-circuited; the exact
        tuple is settled in H1-13b against that named mutant, never against
        unmodified source.
        """
        h = _fleet(temp_home)
        _backup(h, 1)
        for num in ("2", "3"):
            h.switcher.set_account_threshold(num, 85.0)
        h.engine = h._make_engine()
        t = h.clock.now
        phase1 = {"1": _u(40.0), "2": _u(20.0), "3": _u(30.0)}
        phase2 = {
            "1": _u(95.0, five_hour_reset=t + 10_800),
            "2": _u(92.0, five_hour_reset=t + 1_800),
            "3": _u(91.0, five_hour_reset=t + 3_600),
        }
        outcome, mock = _tick(h, [phase1, phase1, phase2])
        assert mock.call_count == BASE_CALLS + 1
        assert outcome is TickOutcome.NO_ACTION
        assert _reasons(h) == ["below-threshold"]
        assert h.active_number() == 1


class TestFailbackNeverFallsBackToAnApiKeyAccount:
    """AC-51 - branch row 4, the API-key last resort, must exclude `failback`.

    Metered credit is the fleet's most expensive resource. A failback that
    cannot find a healthy OAuth primary has nothing to gain by burning it -
    the reserve is quiet, which is why the trigger fired at all. Case 2 is
    the companion that proves the exclusion is scoped to the trigger and not
    a blanket disabling of the last resort.
    """

    def _stage(self, temp_home):
        h = _fleet(temp_home, include_api_key_accounts=True)
        _backup(h, 1)
        _api_key(h, 3)
        h.switcher.set_account_threshold("2", 85.0)
        h.engine = h._make_engine()
        return h

    def test_a_quiet_reserve_does_not_spend_metered_credit(self, temp_home):
        h = self._stage(temp_home)
        outcome, _ = _tick(h, [{
            "1": _u(20.0), "2": _u(95.0), "3": _u(5.0),
        }])
        assert outcome is TickOutcome.NO_ACTION
        assert _reasons(h) == ["below-threshold"]
        assert h.active_number() == 1

    def test_the_last_resort_still_fires_for_an_exhausted_reserve(
        self, temp_home
    ):
        h = self._stage(temp_home)
        outcome, _ = _tick(h, [{
            "1": _u(95.0), "2": _u(95.0), "3": _u(5.0),
        }])
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3
        assert _triggers(h) == ["proactive"]


class TestFailbackSurvivesTheTargetLoop:
    """AC-52 - branch row 18: the target-freshening loop cannot turn a hold
    into a `BLOCKED` or an `ERROR`.

    The predicate reads the *store*; `_freshen_target` reads the
    *credential*. A primary can rank on a perfectly readable usage row and
    still fail to freshen, so the ranking being non-empty is no guarantee the
    loop commits. Today the same fleet returns `NO_ACTION` at the departure
    gate; after this PR it must still return `NO_ACTION` - the hold goes
    AFTER the loop, never in place of it, so the quarantine writes survive.
    """

    def _stage(self, temp_home, *, backup: bool, expires_at=None, n: int = 2):
        h = EngineHarness(temp_home)
        for num in range(1, n + 1):
            h.seed(num, _EMAILS[num],
                   expires_at=None if num == 1 else expires_at)
        h.make_live(_EMAILS[1], 1)
        if backup:
            _backup(h, 1)
        return h

    def _snapshot(self, n: int = 2, active: float = 20.0):
        snap = {"1": _u(active)}
        for num in range(2, n + 1):
            snap[str(num)] = _u(10.0)
        return snap

    def test_a_a_live_session_on_the_only_primary_holds(self, temp_home):
        h = self._stage(temp_home, backup=True,
                        expires_at=int(1_000_000.0 * 1000) + 3_600_000)
        with patch.object(
            h.switcher, "live_session_pids_for", return_value=[4242]
        ):
            outcome, _ = _tick(h, [self._snapshot()])
        assert outcome is TickOutcome.NO_ACTION
        assert _reasons(h) == ["below-threshold"]
        assert _details(h) == ["20% < 90%"]
        assert h.active_number() == 1

    def test_b_dead_credentials_hold_and_the_quarantine_survives(
        self, temp_home
    ):
        h = self._stage(temp_home, backup=True, expires_at=1, n=3)
        with patch(
            "claude_swap.autoswitch.oauth.try_refresh_oauth_credentials",
            return_value=oauth.RefreshOutcome(None, "invalid_grant"),
        ):
            outcome, _ = _tick(h, [self._snapshot(3)])
        assert outcome is TickOutcome.NO_ACTION
        assert "below-threshold" in _reasons(h)
        # The hold sits after the loop: both durable findings are on disk.
        quarantine = h.state().get("quarantine", {})
        assert "2" in quarantine
        assert "3" in quarantine
        assert {e.number for e in h.events if isinstance(e, QuarantineEvent)} == {
            "2", "3",
        }

    def test_c_a_transient_refresh_failure_holds_without_an_error_event(
        self, temp_home
    ):
        h = self._stage(temp_home, backup=True, expires_at=1)
        with patch(
            "claude_swap.autoswitch.oauth.try_refresh_oauth_credentials",
            return_value=oauth.RefreshOutcome(None, "transient"),
        ):
            outcome, _ = _tick(h, [self._snapshot()])
        assert outcome is TickOutcome.NO_ACTION
        assert "below-threshold" in _reasons(h)
        assert not any(isinstance(e, ErrorEvent) for e in h.events)
        assert not h.state().get("quarantine")

    def test_d_without_a_reserve_both_exits_behave_exactly_as_today(
        self, temp_home
    ):
        """The scope check: row 18 adds a `failback` arm, it does not remove
        the loop's exits."""
        live = self._stage(temp_home, backup=False,
                           expires_at=int(1_000_000.0 * 1000) + 3_600_000)
        # `ACCOUNT_THRESHOLD_MIN` is 50, so the no-reserve control departs by
        # crossing 50 rather than by the failback predicate.
        live.switcher.set_account_threshold("1", 50.0)
        live.engine = live._make_engine()
        with patch.object(
            live.switcher, "live_session_pids_for", return_value=[4242]
        ):
            outcome, _ = _tick(live, [self._snapshot(active=60.0)])
        assert outcome is TickOutcome.BLOCKED
        assert "no-viable-target" in _reasons(live)

    def test_d_transient_without_a_reserve_still_errors(self, temp_home):
        h = self._stage(temp_home, backup=False, expires_at=1)
        h.switcher.set_account_threshold("1", 50.0)
        h.engine = h._make_engine()
        with patch(
            "claude_swap.autoswitch.oauth.try_refresh_oauth_credentials",
            return_value=oauth.RefreshOutcome(None, "transient"),
        ):
            outcome, _ = _tick(h, [self._snapshot(active=60.0)])
        assert outcome is TickOutcome.ERROR
        assert any(isinstance(e, ErrorEvent) for e in h.events)


class TestTheActiveAccountCountsAsANonBackup:
    """AC-24, the half the original oracles could not see (review round 1, F-1).

    AC-24 reads "a backup account is skipped while any non-backup qualifies",
    and every oracle above supplies that non-backup as a *candidate*. But the
    candidate list is built with `num != current`, so the one non-backup the
    filter can never see is the account we are already sitting on. Under
    `consume-first` - the only trigger that departs an account which is still
    perfectly usable - a two-account fleet therefore fell through pass 1 (no
    primaries left to rank) into pass 2 and burned the reserve, with the
    healthy primary still active. The Spec's "only used once all the others
    are at their limit" is violated the moment the *active* account is one of
    those others.

    The bounce matters as much as the first move: the reserve becomes active,
    the new `failback` trigger sees a healthy primary, and the fleet ping-pongs
    once per cooldown forever.

    Each positive oracle is paired with the departure it must NOT suppress -
    deleting pass 2 outright would satisfy every hold below and break the
    "last man standing" contract itself.
    """

    def _reserve_resets_soonest(self, h) -> dict:
        """Active primary healthy but resetting LAST; reserve resetting first.

        This is the shape consume-first is built to move on, which is why it
        is the shape that exposed the hole: on the ranking axis the reserve is
        the correct answer, and only the filter can say no.
        """
        t = h.clock.now
        return {
            "1": _u(20.0, seven_day_reset=t + 36_000),
            "2": _u(10.0, seven_day_reset=t + 3_600),
        }

    def test_consume_first_holds_rather_than_spending_the_reserve(
        self, temp_home
    ):
        h = _fleet(temp_home, n=2, strategy="consume-first")
        _backup(h, 2)
        outcome, _ = _tick(h, [self._reserve_resets_soonest(h)])

        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        assert _switches(h) == []

    def test_the_hold_is_the_quiet_one_not_a_block(self, temp_home):
        """`NO_ACTION` alone is not enough: the plan's compatibility rule is
        that configuring a reserve never turns a quiet hold into `BLOCKED` or
        a `no-comparison` diagnosis. The reserve exists and is readable, so
        the fleet is not uncomparable - it is simply already where it should
        be."""
        h = _fleet(temp_home, n=2, strategy="consume-first")
        _backup(h, 2)
        _tick(h, [self._reserve_resets_soonest(h)])

        assert _reasons(h) == ["already-consuming-soonest"]

    def test_without_the_mark_the_same_fleet_moves(self, temp_home):
        """The identity half - the `backup` mark is the only thing that moved,
        so the hold above is the filter working, not a fixture that never
        qualified."""
        h = _fleet(temp_home, n=2, strategy="consume-first")
        outcome, _ = _tick(h, [self._reserve_resets_soonest(h)])

        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2
        assert _triggers(h) == ["consume-first"]

    def test_four_cooldown_spaced_ticks_never_bounce(self, temp_home):
        """The oscillation the first move sets up. A single-tick assertion
        cannot see it: the reserve->primary return is a *different* trigger
        (`failback`) on a *later* tick, so only a multi-tick oracle catches the
        ping-pong."""
        h = _fleet(temp_home, n=2, strategy="consume-first")
        _backup(h, 2)
        for _ in range(4):
            _tick(h, [self._reserve_resets_soonest(h)])
            h.clock.advance(COOLDOWN + 1)

        assert h.active_number() == 1
        assert _switches(h) == []

    def test_an_active_primary_over_its_line_still_promotes_the_reserve(
        self, temp_home
    ):
        """Negative half of the fix. The guard is scoped to the opportunistic
        trigger; once the active account is past its own threshold it is no
        longer a usable primary, every other account IS at its limit, and the
        reserve is exactly what the user marked it for."""
        h = _fleet(temp_home, n=2)
        _backup(h, 2)
        outcome, _ = _tick(h, [{"1": _usage(95.0), "2": _usage(10.0)}])

        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2
        assert _triggers(h) == ["proactive"]

    def test_consume_first_still_moves_to_a_healthier_primary(self, temp_home):
        """Negative half two. The guard must bite on the reserve only - a
        fleet that merely *contains* a reserve keeps its ordinary
        consume-first departures."""
        h = _fleet(temp_home, n=3, strategy="consume-first")
        _backup(h, 3)
        t = h.clock.now
        outcome, _ = _tick(h, [{
            "1": _u(30.0, seven_day_reset=t + 100_000),
            "2": _u(20.0, seven_day_reset=t + 50_000),
            "3": _u(5.0, seven_day_reset=t + 1_000),
        }])

        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2
        assert _triggers(h) == ["consume-first"]

    def test_failback_from_the_reserve_is_untouched(self, temp_home):
        """Negative half three. Under `failback` the active account IS the
        reserve, so the guard's "the active account is a usable primary"
        premise is false and the trigger must fire exactly as MEU-04 shipped
        it."""
        h = _fleet(temp_home, n=2, strategy="consume-first")
        _backup(h, 1)
        t = h.clock.now
        outcome, _ = _tick(h, [{
            "1": _u(20.0, seven_day_reset=t + 3_600),
            "2": _u(10.0, seven_day_reset=t + 36_000),
        }])

        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2
        assert _triggers(h) == ["failback"]
