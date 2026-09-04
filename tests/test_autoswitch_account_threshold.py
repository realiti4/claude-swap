"""MEU-PAP-03 - per-account threshold across every gate call site.

Covers AC-16..AC-23 plus AC-41 (escalation band), AC-42 (`PollEvent.threshold`)
and AC-47 (the departure-gate detail string).

The engine resolves a threshold **map** once per tick and passes it down as a
parameter; `_rank_candidates` is documented pure and is called twice per tick
under the consume-first two-phase commit, so nothing here may read the store
from inside the ranking. Every integration test below drives a **real tick**
through `EngineHarness`, not a helper in isolation - the plan's AC-41 and AC-47
are specifically written that way because a helper-level assertion passes
against the defect each is meant to catch.

Synthetic fixtures only: `EngineHarness` seeds a throwaway store under
`temp_home`. Nothing here reads a real account store.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from claude_swap.autoswitch import (
    HORIZON_HEADROOM_RATIO,
    SPENT_HEADROOM_PCT,
    NoSwitchEvent,
    PollEvent,
    ThresholdMap,
    TickOutcome,
    _every_account_above_threshold,
    pct_label,
)
from claude_swap.settings import AutoSwitchSettings

from tests.test_autoswitch import EngineHarness, _entry_for, _iso_at, _usage

# The global default the engine ships with; every override below is chosen to
# sit on a different side of some gate than this value.
GLOBAL = AutoSwitchSettings().threshold  # 90.0


def _fleet(temp_home, **settings_kwargs) -> EngineHarness:
    """Three seeded OAuth accounts, account 1 live and active."""
    h = EngineHarness(temp_home, **settings_kwargs)
    h.seed(1, "a@example.com")
    h.seed(2, "b@example.com")
    h.seed(3, "c@example.com")
    h.make_live("a@example.com", 1)
    return h


def _reasons(h: EngineHarness) -> list[str]:
    return [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]


def _details(h: EngineHarness) -> list[str]:
    return [e.detail for e in h.events if isinstance(e, NoSwitchEvent)]


def _polls(h: EngineHarness) -> list[PollEvent]:
    return [e for e in h.events if isinstance(e, PollEvent)]


class TestThresholdMap:
    """The map is total by construction - the Boundary Inventory's one rule.

    A candidate absent from the map must read the global default rather than
    raise `KeyError`. Every call site subscripts it directly (`thresholds[num]`),
    so totality cannot be left to each caller to remember.
    """

    def test_a_present_key_returns_its_override(self):
        assert ThresholdMap({"2": 85.0}, GLOBAL)["2"] == pytest.approx(85.0)

    def test_an_absent_key_returns_the_global_default(self):
        assert ThresholdMap({}, GLOBAL)["7"] == pytest.approx(GLOBAL)

    def test_an_absent_key_does_not_raise(self):
        thresholds = ThresholdMap({"1": 60.0}, GLOBAL)
        # No KeyError, and no need for `.get(num, default)` at 10 call sites.
        assert thresholds["nonexistent"] == pytest.approx(GLOBAL)

    def test_it_is_a_mapping_of_str_to_float(self):
        thresholds = ThresholdMap({"2": 85.0}, GLOBAL)
        assert dict(thresholds) == {"2": 85.0}


class TestResolveThresholds:
    """`_resolve_thresholds()` is the single per-tick resolution point."""

    def test_no_overrides_yields_the_global_for_every_account(self, temp_home):
        h = _fleet(temp_home)
        thresholds = h.engine._resolve_thresholds()
        assert [thresholds[n] for n in ("1", "2", "3")] == [GLOBAL] * 3

    def test_an_override_wins_for_that_account_only(self, temp_home):
        h = _fleet(temp_home)
        h.switcher.set_account_threshold("2", 85.0)
        thresholds = h.engine._resolve_thresholds()
        assert thresholds["2"] == pytest.approx(85.0)
        assert thresholds["1"] == pytest.approx(GLOBAL)
        assert thresholds["3"] == pytest.approx(GLOBAL)

    def test_it_tracks_a_session_threshold_change(self, temp_home):
        """`apply_threshold` retargets the global; overrides must survive it."""
        h = _fleet(temp_home)
        h.switcher.set_account_threshold("2", 85.0)
        h.engine.apply_threshold(70.0)
        thresholds = h.engine._resolve_thresholds()
        assert thresholds["1"] == pytest.approx(70.0)
        assert thresholds["2"] == pytest.approx(85.0)


class TestDepartureGateUsesActiveThreshold:
    """AC-16 - the gate fires on the **active account's own** threshold."""

    def test_active_above_its_lower_override_switches(self, temp_home):
        h = _fleet(temp_home)
        h.switcher.set_account_threshold("1", 85.0)
        h.engine = h._make_engine()
        outcome = h.tick_with_usage({"1": _usage(88.0), "2": _usage(10.0), "3": _usage(10.0)})
        assert outcome is TickOutcome.SWITCHED, _reasons(h)
        assert h.active_number() != 1

    def test_the_same_utilization_holds_on_the_global(self, temp_home):
        """Identity control for the case above: 88 < 90, so today's engine holds."""
        h = _fleet(temp_home)
        outcome = h.tick_with_usage({"1": _usage(88.0), "2": _usage(10.0), "3": _usage(10.0)})
        assert outcome is TickOutcome.NO_ACTION
        assert "below-threshold" in _reasons(h)
        assert h.active_number() == 1

    def test_active_below_its_higher_override_holds(self, temp_home):
        h = _fleet(temp_home)
        h.switcher.set_account_threshold("1", 95.0)
        h.engine = h._make_engine()
        outcome = h.tick_with_usage({"1": _usage(92.0), "2": _usage(10.0), "3": _usage(10.0)})
        assert outcome is TickOutcome.NO_ACTION
        assert "below-threshold" in _reasons(h)
        assert h.active_number() == 1

    def test_the_same_utilization_switches_on_the_global(self, temp_home):
        """Identity control: 92 >= 90, so today's engine moves."""
        h = _fleet(temp_home)
        outcome = h.tick_with_usage({"1": _usage(92.0), "2": _usage(10.0), "3": _usage(10.0)})
        assert outcome is TickOutcome.SWITCHED, _reasons(h)
        assert h.active_number() != 1

    def test_another_accounts_override_does_not_move_the_active_gate(self, temp_home):
        """Only the active account's own line gates departure."""
        h = _fleet(temp_home)
        h.switcher.set_account_threshold("2", 50.0)
        h.engine = h._make_engine()
        outcome = h.tick_with_usage({"1": _usage(88.0), "2": _usage(10.0), "3": _usage(10.0)})
        assert outcome is TickOutcome.NO_ACTION
        assert "below-threshold" in _reasons(h)


class TestLandingGateIsPerCandidate:
    """AC-17 - the discriminating case: two candidates, one tick, opposite verdicts.

    Fixture shape, chosen so the assertion cannot pass by accident: candidate 2
    holds **more** headroom than candidate 3, so today's engine picks 2. Giving
    2 the *lower* threshold makes it an unacceptable landing spot while leaving
    3 acceptable, so a correct per-candidate gate must invert the winner. An
    implementation that applies one scalar to every candidate keeps picking 2
    (both accepted) or picks nothing (both rejected) - neither is 3.
    """

    ACTIVE = 99.0    # headroom 1.0 -> over every threshold in play
    CAND_2 = 88.0    # headroom 12.0 -> wins the ranking on headroom
    CAND_3 = 89.0    # headroom 11.0 -> clears hysteresis (11 - 1 == 10)

    def _usage_map(self) -> dict:
        return {
            "1": _usage(self.ACTIVE),
            "2": _usage(self.CAND_2),
            "3": _usage(self.CAND_3),
        }

    def test_the_candidate_over_its_own_line_is_rejected(self, temp_home):
        h = _fleet(temp_home)
        h.switcher.set_account_threshold("2", 85.0)   # 88 >= 85 -> rejected
        h.switcher.set_account_threshold("3", 95.0)   # 89 <  95 -> accepted
        h.engine = h._make_engine()
        outcome = h.tick_with_usage(self._usage_map())
        assert outcome is TickOutcome.SWITCHED, _reasons(h)
        assert h.active_number() == 3

    def test_at_the_global_default_the_headroom_winner_is_taken(self, temp_home):
        """Identity control: with no overrides both candidates are acceptable,
        so the ranking picks the one with more headroom - account 2."""
        h = _fleet(temp_home)
        outcome = h.tick_with_usage(self._usage_map())
        assert outcome is TickOutcome.SWITCHED, _reasons(h)
        assert h.active_number() == 2

    def test_both_over_their_own_lines_hands_the_tick_to_the_all_above_regime(
        self, temp_home
    ):
        """Rejecting *every* candidate does not produce NO_ACTION.

        The landing floor is the exact complement of
        `_every_account_above_threshold` (see the engine comment at
        `_left_account_recovered`): if the gate rejects every measured
        candidate while the active account is over its own line, then by
        definition every account is at/over its line, `all_above` is True, and
        the landing gate is bypassed by the soonest-recovery escape. So the
        per-account rewrite must feed **both** sides the same map - a version
        that made the landing gate per-account but left the all-above census on
        the global scalar would disagree with itself here and hold.
        """
        h = _fleet(temp_home)
        h.switcher.set_account_threshold("2", 85.0)
        h.switcher.set_account_threshold("3", 85.0)
        h.engine = h._make_engine()
        # Both candidates are over their own (lowered) lines, and so is 1.
        assert _every_account_above_threshold(
            ["2", "3"], {"2": 12.0, "3": 11.0}, 1.0,
            ThresholdMap({"2": 85.0, "3": 85.0}, GLOBAL), "1",
        ) is True
        outcome = h.tick_with_usage(self._usage_map())
        assert outcome is TickOutcome.SWITCHED, _reasons(h)
        assert h.active_number() != 1

    def test_the_rejected_candidate_is_not_merely_deprioritized(self, temp_home):
        """A rejected landing spot must be *excluded*, not sorted last.

        Only 2 is overridden here; 3 stays on the global default and is
        therefore still a legal landing spot. 2 holds strictly more headroom
        (12.0 vs 11.0), so an implementation that merely *sorts* a
        threshold-violating candidate downward - or one that never consults a
        per-candidate line at all - still switches to 2. Only exclusion
        produces 3.
        """
        h = _fleet(temp_home)
        h.switcher.set_account_threshold("2", 85.0)   # 88 >= 85 -> rejected
        h.engine = h._make_engine()                   # 3 at 89 < 90 -> kept
        outcome = h.tick_with_usage(self._usage_map())
        assert outcome is TickOutcome.SWITCHED, _reasons(h)
        assert h.active_number() == 3


class TestEveryAccountAboveThreshold:
    """AC-18 - each account is compared against **its own** line."""

    HEADROOM = {"2": 12.0, "3": 11.0}     # utilization 88 / 89
    ACTIVE_HEADROOM = 1.0                 # utilization 99

    def test_a_fleet_uniformly_above_the_default_engages(self, temp_home):
        assert _every_account_above_threshold(
            ["2", "3"], {"2": 5.0, "3": 5.0}, self.ACTIVE_HEADROOM,
            ThresholdMap({}, GLOBAL), "1",
        ) is True

    def test_one_account_below_its_own_higher_line_does_not_engage(self, temp_home):
        """3 is at 89 but its own line is 95, so it is *not* above it."""
        assert _every_account_above_threshold(
            ["2", "3"], self.HEADROOM, self.ACTIVE_HEADROOM,
            ThresholdMap({"2": 85.0, "3": 95.0}, GLOBAL), "1",
        ) is False

    def test_every_account_above_its_own_line_engages(self, temp_home):
        assert _every_account_above_threshold(
            ["2", "3"], self.HEADROOM, self.ACTIVE_HEADROOM,
            ThresholdMap({"2": 85.0, "3": 85.0}, GLOBAL), "1",
        ) is True

    def test_the_active_is_measured_against_its_own_line(self, temp_home):
        """Active at 88 with a 95 line is below it, so the state cannot hold."""
        assert _every_account_above_threshold(
            ["2", "3"], {"2": 5.0, "3": 5.0}, 12.0,
            ThresholdMap({"1": 95.0}, GLOBAL), "1",
        ) is False

    def test_an_unreadable_active_still_returns_false(self, temp_home):
        assert _every_account_above_threshold(
            ["2"], {"2": 1.0}, None, ThresholdMap({}, GLOBAL), "1"
        ) is False

    def test_no_measured_candidate_still_returns_false(self, temp_home):
        assert _every_account_above_threshold(
            ["2"], {"2": None}, self.ACTIVE_HEADROOM,
            ThresholdMap({}, GLOBAL), "1",
        ) is False

    def test_a_uniform_default_fleet_matches_todays_verdict(self, temp_home):
        """Identity control - the default path must be bit-identical."""
        thresholds = ThresholdMap({}, GLOBAL)
        for active_h, cand_h, expected in [
            (1.0, 5.0, True),      # 99 and 95, both over 90
            (1.0, 50.0, False),    # candidate at 50 is well under
            (50.0, 5.0, False),    # active at 50 is well under
        ]:
            assert _every_account_above_threshold(
                ["2"], {"2": cand_h}, active_h, thresholds, "1"
            ) is expected


class TestAntiFlapUsesTheBarredAccountsThreshold:
    """AC-19 - the anti-flap machinery evaluates the **barred** account against
    *its own* line, so it cannot contradict the gate that barred it.

    Both reachable sites take the unreadable-active fallback leg, which asks
    "would the ranking accept this peer as a landing spot right now?" - the
    same question `_rank_candidates` answers per candidate, and therefore the
    same threshold.
    """

    STATE = {
        "lastSwitchFrom": 2,
        "lastSwitchTo": "1",
        "leftHeadroom": 4.0,
        "leftRecoveryAt": None,
        "leftTrigger": "proactive",
    }

    def test_no_return_releases_when_the_barred_account_clears_its_own_line(
        self, temp_home
    ):
        """Barred account at 88 (headroom 12). Its own line is 95, so
        `100 - 95 == 5` and 12 > 5 - landing-eligible, bar released."""
        h = _fleet(temp_home)
        assert h.engine._no_return_account(
            "proactive", dict(self.STATE), {"2": 12.0}, None, True,
            h.settings, "1", thresholds=ThresholdMap({"2": 95.0}, GLOBAL),
        ) is None

    def test_no_return_holds_when_the_barred_account_is_over_its_own_line(
        self, temp_home
    ):
        """Same 12.0 headroom, own line 85: `100 - 85 == 15` and 12 < 15."""
        h = _fleet(temp_home)
        assert h.engine._no_return_account(
            "proactive", dict(self.STATE), {"2": 12.0}, None, True,
            h.settings, "1", thresholds=ThresholdMap({"2": 85.0}, GLOBAL),
        ) == "2"

    def test_no_return_at_the_default_matches_todays_verdict(self, temp_home):
        """Identity control: `100 - 90 == 10`, and 12 > 10 releases today."""
        h = _fleet(temp_home)
        assert h.engine._no_return_account(
            "proactive", dict(self.STATE), {"2": 12.0}, None, True,
            h.settings, "1", thresholds=ThresholdMap({}, GLOBAL),
        ) is None

    def test_recovered_uses_the_barred_line_on_the_failover_leg(self, temp_home):
        """`_left_account_recovered`, failover snapshot: the landing leg reads
        the barred account's own threshold."""
        h = _fleet(temp_home)
        state = {
            "lastSwitchFrom": 2,
            "leftHeadroom": None,
            "leftRecoveryAt": None,
            "leftTrigger": "failover",
        }
        common = dict(
            usage={"1": _usage(99.0), "2": _usage(88.0)},
            headroom={"1": 1.0, "2": 12.0},
            active_headroom=None,
            settings=h.settings,
            now=h.clock.now,
            current="1",
        )
        assert h.engine._left_account_recovered(
            dict(state), thresholds=ThresholdMap({"2": 95.0}, GLOBAL), **common
        ) is True
        assert h.engine._left_account_recovered(
            dict(state), thresholds=ThresholdMap({"2": 85.0}, GLOBAL), **common
        ) is False

    def test_recovered_uses_the_barred_line_on_the_dominance_leg(self, temp_home):
        """The `active_headroom is None` fallback at the dominance leg reads the
        barred account's own threshold too - the second of the two sites."""
        h = _fleet(temp_home)
        state = {
            "lastSwitchFrom": 2,
            "lastSwitchTo": "1",
            "leftHeadroom": 4.0,
            "leftRecoveryAt": None,
            "leftTrigger": "proactive",
        }
        common = dict(
            usage={"1": _usage(99.0), "2": _usage(88.0)},
            headroom={"1": None, "2": 12.0},
            active_headroom=None,
            settings=h.settings,
            now=h.clock.now,
            current="1",
        )
        assert h.engine._left_account_recovered(
            dict(state), thresholds=ThresholdMap({"2": 95.0}, GLOBAL), **common
        ) is True

    def test_an_unrelated_accounts_override_changes_nothing(self, temp_home):
        """Only the barred account's own line is consulted."""
        h = _fleet(temp_home)
        assert h.engine._no_return_account(
            "proactive", dict(self.STATE), {"2": 12.0}, None, True,
            h.settings, "1",
            thresholds=ThresholdMap({"1": 50.0, "3": 99.0}, GLOBAL),
        ) is None


class TestPollPolicyMinimumThreshold:
    """AC-20 - `set_poll_policy_inputs` receives the fleet **minimum**.

    `poll_policy`'s urgent mode compares `new_pct >= threshold - 15`, so a
    minimum can only make collection *earlier*, never lazier - the safe
    direction when accounts disagree about where their line is.
    """

    def test_no_overrides_passes_the_global_unchanged(self, temp_home):
        h = _fleet(temp_home)
        h.engine = h._make_engine()
        assert h.switcher._poll_inputs_override[0] == pytest.approx(GLOBAL)

    def test_a_lower_override_lowers_the_pinned_value(self, temp_home):
        h = _fleet(temp_home)
        h.switcher.set_account_threshold("2", 60.0)
        h.engine = h._make_engine()
        assert h.switcher._poll_inputs_override[0] == pytest.approx(60.0)

    def test_a_higher_override_does_not_raise_the_pinned_value(self, temp_home):
        h = _fleet(temp_home)
        h.switcher.set_account_threshold("2", 99.0)
        h.engine = h._make_engine()
        assert h.switcher._poll_inputs_override[0] == pytest.approx(GLOBAL)

    def test_the_minimum_is_taken_across_the_whole_fleet(self, temp_home):
        h = _fleet(temp_home)
        h.switcher.set_account_threshold("1", 80.0)
        h.switcher.set_account_threshold("2", 60.0)
        h.switcher.set_account_threshold("3", 70.0)
        h.engine = h._make_engine()
        assert h.switcher._poll_inputs_override[0] == pytest.approx(60.0)

    def test_a_session_threshold_change_recomputes_the_minimum(self, temp_home):
        h = _fleet(temp_home)
        h.switcher.set_account_threshold("2", 60.0)
        h.engine = h._make_engine()
        h.engine.apply_threshold(55.0)
        assert h.switcher._poll_inputs_override[0] == pytest.approx(55.0)

    def test_a_disabled_account_does_not_drag_the_minimum_down(self, temp_home):
        """The minimum is over *switchable* accounts; a parked slot's line is
        not one the engine will ever gate on."""
        h = _fleet(temp_home)
        h.switcher.set_account_threshold("3", 55.0)
        h.switcher.set_account_disabled("3", True)
        h.engine = h._make_engine()
        assert h.switcher._poll_inputs_override[0] == pytest.approx(GLOBAL)


class TestRankCandidatesStaysPure:
    """AC-21 - thresholds arrive as a parameter; the ranking never reads the store.

    `consume-first` calls `_rank_candidates` **twice per tick** under a
    two-phase commit, so a store read inside it would re-resolve mid-tick and
    could decide phase 1 and phase 2 on different lines.
    """

    def _kwargs(self, h, thresholds):
        return dict(
            trigger="proactive",
            consume_first=False,
            oauth_candidates=["2", "3"],
            no_return=None,
            usage={"1": _usage(99.0), "2": _usage(50.0), "3": _usage(60.0)},
            headroom={"1": 1.0, "2": 50.0, "3": 40.0},
            current="1",
            active_headroom=1.0,
            settings=h.settings,
            now=h.clock.now,
            thresholds=thresholds,
        )

    def test_it_runs_against_a_switcher_whose_reads_raise(self, temp_home):
        h = _fleet(temp_home)
        kwargs = self._kwargs(h, ThresholdMap({"2": 85.0}, GLOBAL))

        def _boom(*args, **kw):
            raise AssertionError("_rank_candidates read the store")

        for name in (
            "account_policies",
            "_get_sequence_data",
            "switchable_account_numbers",
            "backup_account_numbers",
        ):
            monkey = getattr(h.switcher, name, None)
            if monkey is not None:
                setattr(h.switcher, name, _boom)

        ordered_a, _, _ = h.engine._rank_candidates(**kwargs)
        ordered_b, _, _ = h.engine._rank_candidates(**kwargs)
        assert ordered_a == ordered_b

    def test_two_calls_on_different_snapshots_use_the_passed_map(self, temp_home):
        """The two-phase commit's whole point: same map, different snapshot."""
        h = _fleet(temp_home)
        thresholds = ThresholdMap({"2": 45.0, "3": 95.0}, GLOBAL)
        phase1 = self._kwargs(h, thresholds)
        ordered1, _, _ = h.engine._rank_candidates(**phase1)
        # 2 is at 50 utilization, over its own 45 line -> not a landing spot.
        assert "2" not in ordered1
        assert ordered1 == ["3"]

        phase2 = dict(phase1)
        phase2["usage"] = {"1": _usage(99.0), "2": _usage(20.0), "3": _usage(60.0)}
        phase2["headroom"] = {"1": 1.0, "2": 80.0, "3": 40.0}
        ordered2, _, _ = h.engine._rank_candidates(**phase2)
        # Same map, fresher snapshot: 2 is now at 20, under its own line.
        assert ordered2[0] == "2"

    def test_it_emits_nothing(self, temp_home):
        h = _fleet(temp_home)
        h.events.clear()
        h.engine._rank_candidates(**self._kwargs(h, ThresholdMap({}, GLOBAL)))
        assert h.events == []


class TestRawHeadroomGuardsDoNotMove:
    """AC-22 - `HORIZON_HEADROOM_RATIO` and `SPENT_HEADROOM_PCT` stay on raw
    headroom (upstream #262 reasoned this out and it applies identically).

    A ratio against distance-to-100 means something; a ratio against a policy
    line every account is already past does not.
    """

    def test_the_constants_are_untouched(self):
        assert HORIZON_HEADROOM_RATIO == pytest.approx(2.0)
        assert SPENT_HEADROOM_PCT == pytest.approx(3.0)

    def test_an_override_does_not_move_the_ratio_gate(self, temp_home):
        """`_no_return_account`'s ratio leg reads a *readable* active, so the
        threshold never enters it - an override on the barred account must not
        change which side of `left >= active * RATIO` it lands on."""
        h = _fleet(temp_home)
        state = {
            "lastSwitchFrom": 2,
            "lastSwitchTo": "1",
            "leftHeadroom": 4.0,
            "leftRecoveryAt": None,
            "leftTrigger": "proactive",
        }
        # left 12.0 vs active 5.0: 12 >= 5 * 2.0 -> beats us outright.
        for override in ({}, {"2": 50.0}, {"2": 99.0}):
            assert h.engine._no_return_account(
                "proactive", dict(state), {"2": 12.0}, 5.0, True,
                h.settings, "1", thresholds=ThresholdMap(override, GLOBAL),
            ) is None
        # left 9.0 vs active 5.0: 9 < 10 -> the ratio does not release.
        for override in ({}, {"2": 50.0}, {"2": 99.0}):
            assert h.engine._no_return_account(
                "proactive", dict(state), {"2": 9.0}, 5.0, True,
                h.settings, "1", thresholds=ThresholdMap(override, GLOBAL),
            ) == "2"

    def test_an_override_does_not_move_the_spent_gate(self, temp_home):
        """`_left_account_recovered`'s dominance leg is
        `h > active * RATIO + SPENT_HEADROOM_PCT` on raw headroom."""
        h = _fleet(temp_home)
        state = {
            "lastSwitchFrom": 2,
            "leftHeadroom": 4.0,
            "leftRecoveryAt": None,
            "leftTrigger": "proactive",
        }
        common = dict(
            usage={"1": _usage(95.0), "2": _usage(80.0)},
            headroom={"1": 5.0, "2": 20.0},
            active_headroom=5.0,
            settings=h.settings,
            now=h.clock.now,
            current="1",
        )
        # 20 > 5 * 2.0 + 3.0 == 13.0 -> dominates, regardless of any line.
        for override in ({}, {"2": 50.0}, {"2": 99.0}):
            assert h.engine._left_account_recovered(
                dict(state), thresholds=ThresholdMap(override, GLOBAL), **common
            ) is True


class TestEscalationBandUsesActiveThreshold:
    """AC-41 - the escalation band asks about the **active** account, so it
    reads that account's own line.

    Driven through a real tick, never `_collect_scheduled_usage` in isolation:
    the defect this discriminates against is a *stale-snapshot switch*, which
    only exists once ranking runs on what the collector returned.

    Discriminating tuple from the plan: global `99.9`, active override `50`,
    active utilization `60`. The departure gate fires at 50, but a collector
    still holding the global tests `60 >= 99.9 - 15` (i.e. `>= 84.9`), does
    not escalate, and the proactive selection that follows runs on the
    pre-escalation snapshot.
    """

    # Baseline collector calls per tick, before any escalation: the
    # `fetch=set()` pre-read that builds the poll plan, then the plan fetch
    # itself. The escalation refetch is the third. Asserting `>= 2` would be
    # true on every tick ever taken and would discriminate nothing.
    BASE_CALLS = 2

    def _tick(self, h, snapshots: list[dict]):
        """Return `(outcome, mock)`; call *i* serves `snapshots[i]`, last repeats."""
        from unittest.mock import patch

        from tests.test_autoswitch import _entry_for

        built = [
            {n: _entry_for(v, h.clock.now) for n, v in snap.items()}
            for snap in snapshots
        ]

        calls = {"n": 0}

        def _side_effect(*args, **kwargs):
            i = min(calls["n"], len(built) - 1)
            calls["n"] += 1
            return built[i]

        with patch.object(
            h.switcher, "usage_entries_by_account", side_effect=_side_effect
        ) as m:
            outcome = h.engine.tick()
        return outcome, m

    def test_it_escalates_on_the_active_accounts_own_line(self, temp_home):
        h = _fleet(temp_home, threshold=99.9)
        h.switcher.set_account_threshold("1", 50.0)
        h.engine = h._make_engine()
        pre = {"1": _usage(60.0), "2": _usage(5.0), "3": _usage(10.0)}
        post = {"1": _usage(60.0), "2": _usage(100.0), "3": _usage(10.0)}
        # `post` is served only from the third call on, so it can reach the
        # ranking *only* through the escalation refetch.
        outcome, mock = self._tick(h, [pre, pre, post])
        assert mock.call_count == self.BASE_CALLS + 1
        assert outcome is TickOutcome.SWITCHED, _reasons(h)
        # The decision was taken on the POST-escalation snapshot: on `pre`,
        # account 2 has the most headroom and wins; on `post` it is at limit.
        assert h.active_number() == 3

    def test_the_mirror_case_does_not_escalate(self, temp_home):
        """Global `50`, active override `99.9`, utilization `60`: the band must
        stay shut, so the plan fetch is the only collector call."""
        h = _fleet(temp_home, threshold=50.0)
        h.switcher.set_account_threshold("1", 99.9)
        h.engine = h._make_engine()
        snap = {"1": _usage(60.0), "2": _usage(5.0), "3": _usage(10.0)}
        outcome, mock = self._tick(h, [snap])
        assert mock.call_count == self.BASE_CALLS
        assert outcome is TickOutcome.NO_ACTION
        assert "below-threshold" in _reasons(h)

    def test_with_no_overrides_the_band_is_todays(self, temp_home):
        """Identity control: global 90, utilization 80 -> `80 >= 75` escalates."""
        h = _fleet(temp_home)
        snap = {"1": _usage(80.0), "2": _usage(5.0), "3": _usage(10.0)}
        _outcome, mock = self._tick(h, [snap])
        assert mock.call_count == self.BASE_CALLS + 1

    def test_with_no_overrides_a_quiet_active_does_not_escalate(self, temp_home):
        """Identity control: utilization 60 against global 90 -> `60 >= 75` false."""
        h = _fleet(temp_home)
        snap = {"1": _usage(60.0), "2": _usage(5.0), "3": _usage(10.0)}
        _outcome, mock = self._tick(h, [snap])
        assert mock.call_count == self.BASE_CALLS


class TestPollEventCarriesTheActiveThreshold:
    """AC-42 - the number the UI renders equals the number the gate used."""

    def test_it_carries_the_active_accounts_override(self, temp_home):
        h = _fleet(temp_home)
        h.switcher.set_account_threshold("1", 55.0)
        h.engine = h._make_engine()
        h.tick_with_usage({"1": _usage(40.0), "2": _usage(10.0), "3": _usage(10.0)})
        assert _polls(h)[0].threshold == pytest.approx(55.0)

    def test_a_peers_override_does_not_appear(self, temp_home):
        h = _fleet(temp_home)
        h.switcher.set_account_threshold("2", 55.0)
        h.engine = h._make_engine()
        h.tick_with_usage({"1": _usage(40.0), "2": _usage(10.0), "3": _usage(10.0)})
        assert _polls(h)[0].threshold == pytest.approx(GLOBAL)

    def test_with_no_overrides_it_is_todays_value(self, temp_home):
        h = _fleet(temp_home)
        h.tick_with_usage({"1": _usage(40.0), "2": _usage(10.0), "3": _usage(10.0)})
        assert _polls(h)[0].threshold == pytest.approx(GLOBAL)

    def test_the_no_active_account_path_keeps_the_global(self, temp_home):
        """`:911` is the deliberate non-move in the inventory: with no active
        account there is no account whose line could be read."""
        h = EngineHarness(temp_home, threshold=77.0)
        outcome = h.tick_with_usage({})
        assert outcome is TickOutcome.NO_ACTION
        polls = _polls(h)
        assert polls and polls[0].active is None
        assert polls[0].threshold == pytest.approx(77.0)

    def test_the_rendered_number_equals_the_gate_that_held(self, temp_home):
        """The property the AC exists for, asserted as one statement."""
        h = _fleet(temp_home, threshold=85.0)
        h.switcher.set_account_threshold("1", 95.0)
        h.engine = h._make_engine()
        h.tick_with_usage({"1": _usage(90.0), "2": _usage(10.0), "3": _usage(10.0)})
        rendered = _polls(h)[0].threshold
        assert rendered == pytest.approx(95.0)
        assert f"< {pct_label(rendered)}%" in _details(h)[0]


class TestDepartureGateDetailString:
    """AC-47 - `:989` quotes the *active account's own* effective threshold.

    The plan is explicit that a hold-only assertion is insufficient: it passes
    against the defect, because row 1 already holds correctly. The emitted
    string is the oracle. Leaving this site on the global scalar makes the
    engine hold on one number and display another - `90% < 85%` is not merely
    misleading but arithmetically false, and invites the operator to conclude
    the engine is broken.
    """

    def test_the_detail_quotes_the_active_override(self, temp_home):
        h = _fleet(temp_home, threshold=85.0)
        h.switcher.set_account_threshold("1", 95.0)
        h.engine = h._make_engine()
        outcome = h.tick_with_usage(
            {"1": _usage(90.0), "2": _usage(10.0), "3": _usage(10.0)}
        )
        assert outcome is TickOutcome.NO_ACTION
        assert _details(h)[0] == "90% < 95%"

    def test_it_never_quotes_the_global_when_an_override_exists(self, temp_home):
        h = _fleet(temp_home, threshold=85.0)
        h.switcher.set_account_threshold("1", 95.0)
        h.engine = h._make_engine()
        h.tick_with_usage({"1": _usage(90.0), "2": _usage(10.0), "3": _usage(10.0)})
        assert "< 85%" not in _details(h)[0]

    def test_the_mirror_case_reads_the_lower_override(self, temp_home):
        """Gate fires on the override, but no candidate survives its landing
        gate - the reader must not be told the account is comfortably under."""
        h = EngineHarness(temp_home, threshold=85.0, strategy="consume-first")
        h.seed(1, "a@example.com")
        h.make_live("a@example.com", 1)
        h.switcher.set_account_threshold("1", 95.0)
        h.engine = h._make_engine()
        h.tick_with_usage({"1": _usage(90.0)})
        # The consume-first no-OAuth-peer diagnostic - a distinct site from the
        # departure gate and the second of the two `pct_label` emits. It is
        # guarded by `not oauth_candidates`, so the fleet must hold exactly one
        # account: with peers present but unreadable the engine emits
        # `no-comparison` instead and this site is never reached.
        assert _reasons(h) == ["below-threshold"]
        assert _details(h)[0] == "90% < 95%"

    def test_with_no_override_the_detail_is_todays(self, temp_home):
        h = _fleet(temp_home, threshold=85.0)
        outcome = h.tick_with_usage(
            {"1": _usage(80.0), "2": _usage(10.0), "3": _usage(10.0)}
        )
        assert outcome is TickOutcome.NO_ACTION
        assert _details(h)[0] == "80% < 85%"

    def test_both_sides_go_through_pct_label(self, temp_home):
        """`99.9` must never render as a lying `100`."""
        h = _fleet(temp_home, threshold=85.0)
        h.switcher.set_account_threshold("1", 99.9)
        h.engine = h._make_engine()
        h.tick_with_usage({"1": _usage(99.5), "2": _usage(10.0), "3": _usage(10.0)})
        assert _details(h)[0] == "99.5% < 99.9%"


class TestDefaultIdentity:
    """AC-23 - with no per-account thresholds anywhere, nothing changes.

    The whole-suite form of this AC is the phase gate (`2056 passed, 75
    skipped, 3 warnings` baseline, and `tests/test_autoswitch.py` unchanged).
    What is asserted here is the property that makes that outcome expected
    rather than lucky: on a fleet with no overrides, every resolved value is
    the global scalar, so each rewritten call site receives exactly the
    argument it receives today.
    """

    def test_no_policy_keys_exist_on_a_fresh_fleet(self, temp_home):
        from claude_swap.models import AccountPolicy

        h = _fleet(temp_home)
        policies = h.switcher.account_policies()
        # One entry per managed account, every one of them the default.
        assert set(policies) == {"1", "2", "3"}
        assert all(p == AccountPolicy() for p in policies.values())
        # And so the map the engine builds carries no overrides at all.
        thresholds = h.engine._resolve_thresholds()
        assert dict(thresholds) == {}

    def test_every_resolved_value_is_the_global(self, temp_home):
        h = _fleet(temp_home, threshold=77.5)
        thresholds = h.engine._resolve_thresholds()
        for num in ("1", "2", "3", "unknown"):
            assert thresholds[num] == pytest.approx(77.5)

    def test_the_pinned_poll_input_is_the_global(self, temp_home):
        h = _fleet(temp_home, threshold=77.5)
        h.engine = h._make_engine()
        assert h.switcher._poll_inputs_override[0] == pytest.approx(77.5)

    @pytest.mark.parametrize(
        "utilization,expected",
        [(80.0, TickOutcome.NO_ACTION), (92.0, TickOutcome.SWITCHED)],
    )
    def test_the_gate_is_todays_gate(self, temp_home, utilization, expected):
        h = _fleet(temp_home)
        outcome = h.tick_with_usage(
            {"1": _usage(utilization), "2": _usage(10.0), "3": _usage(10.0)}
        )
        assert outcome is expected, _reasons(h)

    def test_a_cleared_override_returns_to_the_global(self, temp_home):
        h = _fleet(temp_home)
        h.switcher.set_account_threshold("1", 55.0)
        h.switcher.set_account_threshold("1", None)
        h.engine = h._make_engine()
        assert dict(h.engine._resolve_thresholds()) == {}
        outcome = h.tick_with_usage(
            {"1": _usage(80.0), "2": _usage(10.0), "3": _usage(10.0)}
        )
        assert outcome is TickOutcome.NO_ACTION


class TestPollCadenceFollowsALivePolicyChange:
    """VR-1 - the pinned minimum is refreshed every tick, not only at birth.

    `cswap threshold 1 50` is written by a **different process** while
    `cswap auto` is already running - that is the ordinary way a threshold
    gets changed. `_resolve_thresholds()` re-reads the store every tick, so
    the switch DECISION picks the new line up immediately. But
    `set_poll_policy_inputs` was called in exactly two places, `__init__` and
    `apply_threshold()`, and a persisted write goes through neither. Cadence
    therefore stayed pinned to the minimum as it stood when the engine booted.

    That contradicts AC-20's rationale, which is not "the minimum is a tidy
    value" but "the collector is then never lazier than the earliest possible
    trigger, so no switch is missed". `poll_policy`'s urgent mode fires at
    `new_pct >= threshold - 15`, so a stale 90 against a live 50 goes urgent
    at 75% when it should go urgent at 35% - lazier, in the one direction the
    AC-20 rationale rules out, for every tick until the engine restarts.

    `TestPollPolicyMinimumThreshold` above cannot catch this: every case there
    writes the policy and *then* builds the engine, except the `apply_threshold`
    case, which goes through the one path that already refreshed.
    """

    def test_a_persisted_override_lowers_the_pinned_value_on_the_next_tick(
        self, temp_home
    ):
        h = _fleet(temp_home)
        h.engine = h._make_engine()
        assert h.switcher._poll_inputs_override[0] == pytest.approx(GLOBAL)

        # The out-of-process write the running engine must notice.
        h.switcher.set_account_threshold("1", 50.0)
        h.tick_with_usage({"1": _usage(40.0), "2": _usage(40.0), "3": _usage(40.0)})

        assert h.switcher._poll_inputs_override[0] == pytest.approx(50.0)

    def test_the_refresh_lands_before_the_collector_reads_it(self, temp_home):
        """Ordering, not just eventual consistency.

        A refresh applied *after* `_collect_scheduled_usage` would satisfy the
        test above and still plan this tick's collection on the stale number -
        the tick where it matters most, because it is the one that discovered
        the change.
        """
        h = _fleet(temp_home)
        h.engine = h._make_engine()
        h.switcher.set_account_threshold("1", 50.0)

        seen: list[float] = []
        entries = {
            num: _entry_for(_usage(40.0), h.clock.now) for num in ("1", "2", "3")
        }

        def _capture(*_a, **_k):
            seen.append(h.switcher._poll_inputs_override[0])
            return entries

        with patch.object(
            h.switcher, "usage_entries_by_account", side_effect=_capture
        ):
            h.engine.tick()

        assert seen, "the collector never ran, so the ordering was not observed"
        assert seen[0] == pytest.approx(50.0)

    def test_clearing_the_override_raises_the_pinned_value_back(self, temp_home):
        """The negative half. A refresh that only ever ratchets downward would
        pass every positive case and leave cadence over-tight forever."""
        h = _fleet(temp_home)
        h.switcher.set_account_threshold("1", 50.0)
        h.engine = h._make_engine()
        assert h.switcher._poll_inputs_override[0] == pytest.approx(50.0)

        h.switcher.set_account_threshold("1", None)
        h.tick_with_usage({"1": _usage(40.0), "2": _usage(40.0), "3": _usage(40.0)})

        assert h.switcher._poll_inputs_override[0] == pytest.approx(GLOBAL)

    def test_a_session_override_is_not_clobbered_by_the_tick_refresh(self, temp_home):
        """`apply_threshold()` retargets the session global. The per-tick
        refresh recomputes from the tick's own settings snapshot, so it must
        land on 55, not revert to the 90 the engine was constructed with."""
        h = _fleet(temp_home)
        h.engine = h._make_engine()
        h.engine.apply_threshold(55.0)
        h.tick_with_usage({"1": _usage(40.0), "2": _usage(40.0), "3": _usage(40.0)})

        assert h.switcher._poll_inputs_override[0] == pytest.approx(55.0)

    def test_a_disabled_account_still_does_not_drag_the_minimum_down(self, temp_home):
        """The switchable filter has to survive the move into the tick."""
        h = _fleet(temp_home)
        h.engine = h._make_engine()
        h.switcher.set_account_threshold("3", 55.0)
        h.switcher.set_account_disabled("3", True)
        h.tick_with_usage({"1": _usage(40.0), "2": _usage(40.0), "3": _usage(40.0)})

        assert h.switcher._poll_inputs_override[0] == pytest.approx(GLOBAL)

    def test_a_fleet_with_no_overrides_pins_exactly_the_global_every_tick(
        self, temp_home
    ):
        """Default-identity. Ticking must not perturb the pinned value on a
        fleet that never used the feature."""
        h = _fleet(temp_home)
        h.engine = h._make_engine()
        for _ in range(3):
            h.tick_with_usage(
                {"1": _usage(40.0), "2": _usage(40.0), "3": _usage(40.0)}
            )
            assert h.switcher._poll_inputs_override[0] == pytest.approx(GLOBAL)
        assert h.switcher._poll_inputs_override[1] == h.engine._models
