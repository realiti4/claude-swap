"""The model-window rules of the auto-switch engine (``autoswitch.model``).

Three rules, all inert without a configured model (the legacy suite in
``test_autoswitch.py`` is the byte-for-byte baseline for that case):

1. a candidate whose readable usage names no configured model window is
   ineligible for every trigger and shows up as ``model-window-missing``;
2. consume-first ranks on the configured model window's reset, not the
   account-wide weekly reset;
3. an active account that loses its model window on ``unhealthy_ticks``
   distinct fresh reads is treated as at its limit and left.

Measured origin (2026-09-09/10): a pool running ``cswap auto --model Fable
--strategy consume-first`` rotated onto seats whose usage payload had no
Fable window (``scoped: null``) while their 5h/7d windows read as room, and
onto seats whose Fable window vanished within a day when their organization's
credits ran out; each time Claude Code offered a fallback model.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import patch

import pytest

from claude_swap.autoswitch import (
    MODEL_WINDOW_MISSING,
    AllExhaustedEvent,
    ModelWindowLostEvent,
    NoSwitchEvent,
    PollEvent,
    SwitchEvent,
    TickOutcome,
    _model_window_reset_ts,
    _model_window_state,
    _seven_day_reset_ts,
)
from claude_swap.json_output import USAGE_RELOGIN_REQUIRED
from tests.test_autoswitch import EngineHarness, _entry_for, _iso_at

H = 3600.0


def _seat(
    five_h: float = 5.0,
    seven_d: float = 10.0,
    *,
    fable: float | None = None,
    fable_reset: float | None = None,
    seven_d_reset: float | None = None,
) -> dict:
    """A usage payload; ``fable=None`` means the seat reports NO Fable window
    (the shape ``cswap status --json`` showed as ``scoped: null``)."""
    seven: dict = {"pct": seven_d}
    if seven_d_reset is not None:
        seven["resets_at"] = _iso_at(seven_d_reset)
    usage: dict = {"five_hour": {"pct": five_h}, "seven_day": seven}
    if fable is not None:
        window: dict = {"name": "Fable", "pct": fable}
        if fable_reset is not None:
            window["resets_at"] = _iso_at(fable_reset)
        usage["scoped"] = [window]
    return usage


def _harness(temp_home, seats: int = 3, **settings) -> EngineHarness:
    (temp_home / ".claude").mkdir(parents=True, exist_ok=True)
    h = EngineHarness(temp_home, **settings)
    for n in range(1, seats + 1):
        h.seed(n, f"seat{n}@example.com")
    h.make_live("seat1@example.com", 1)
    return h


def _reasons(h: EngineHarness) -> list[str]:
    return [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]


def _poll(h: EngineHarness) -> PollEvent:
    return next(e for e in h.events if isinstance(e, PollEvent))


def _switch(h: EngineHarness) -> SwitchEvent:
    return next(e for e in h.events if isinstance(e, SwitchEvent))


class TestModelWindowState:
    """The predicate every rule reads: present / missing / unknown."""

    def test_named_model_absent_from_scoped_is_missing(self):
        assert _model_window_state(_seat(), ("Fable",)) == "missing"
        assert _model_window_state(
            {**_seat(), "scoped": [{"name": "Opus", "pct": 10.0}]}, ("Fable",)
        ) == "missing"

    def test_named_model_present_matches_case_insensitively(self):
        assert _model_window_state(_seat(fable=40), ("Fable",)) == "present"
        assert _model_window_state(_seat(fable=40), ("fable",)) == "present"
        # Any one of several configured names is enough.
        assert _model_window_state(_seat(fable=40), ("Opus", "Fable")) == "present"

    def test_absence_of_evidence_is_unknown_not_missing(self):
        # A fetch failure must never manufacture ineligibility.
        assert _model_window_state(None, ("Fable",)) == "unknown"
        assert _model_window_state(USAGE_RELOGIN_REQUIRED, ("Fable",)) == "unknown"
        assert _model_window_state({}, ("Fable",)) == "unknown"
        assert _model_window_state({"spend": {"pct": 3.0}}, ("Fable",)) == "unknown"

    def test_bare_all_names_no_model_so_nothing_is_missing(self):
        assert _model_window_state(_seat(), ("all",)) == "present"
        assert _model_window_state(None, ()) == "present"

    def test_reset_is_the_soonest_future_configured_window(self):
        now = 1_000_000.0
        usage = {
            **_seat(),
            "scoped": [
                {"name": "Fable", "pct": 10.0, "resets_at": _iso_at(now + 30 * H)},
                {"name": "Opus", "pct": 10.0, "resets_at": _iso_at(now + 2 * H)},
                {"name": "Sonnet", "pct": 10.0, "resets_at": _iso_at(now - H)},
            ],
        }
        assert _model_window_reset_ts(usage, ("Fable",), now) == now + 30 * H
        assert _model_window_reset_ts(usage, ("Fable", "Opus"), now) == now + 2 * H
        assert _model_window_reset_ts(usage, ("all",), now) == now + 2 * H
        assert _model_window_reset_ts(usage, ("Sonnet",), now) is None  # past
        assert _model_window_reset_ts(_seat(fable=10), ("Fable",), now) is None
        assert _model_window_reset_ts(_seat(), ("Fable",), now) is None


class TestMissingWindowIsIneligible:
    """Rule 1, on every trigger."""

    def test_proactive_skips_the_windowless_seat_and_names_it(self, temp_home):
        h = _harness(temp_home, model="Fable")
        outcome = h.tick_with_usage({
            "1": _seat(fable=95),                 # active: Fable over the bar
            "2": _seat(five_h=10, seven_d=20),    # lots of room, NO Fable window
            "3": _seat(fable=60),                 # less room, but serves Fable
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3
        poll = _poll(h)
        assert poll.skipped == {"2": MODEL_WINDOW_MISSING}
        assert poll.to_json()["skippedCandidates"] == {"2": MODEL_WINDOW_MISSING}
        assert "#2: 5h 10% · 7d 20% (skipped: model-window-missing)" in poll.human()
        assert "#3: 5h 5% · 7d 10% · Fable 60%" in poll.human()
        assert "skipped" not in poll.human().split("#3:")[1]

    def test_the_same_seat_with_a_window_and_room_is_eligible(self, temp_home):
        h = _harness(temp_home, model="Fable")
        outcome = h.tick_with_usage({
            "1": _seat(fable=95),
            "2": _seat(five_h=10, seven_d=20, fable=30),
            "3": _seat(fable=60),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2  # now the most Fable headroom
        assert _poll(h).skipped == {}
        assert "skippedCandidates" not in _poll(h).to_json()

    def test_at_limit_escape_still_refuses_the_windowless_seat(self, temp_home):
        h = _harness(temp_home, model="Fable")
        outcome = h.tick_with_usage({
            "1": _seat(fable=100),                # hard at the Fable wall
            "2": _seat(five_h=0, seven_d=0),      # pristine 5h/7d, no Fable
            "3": _seat(five_h=80, fable=70),      # only seat that serves Fable
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3
        assert _switch(h).trigger == "at-limit"

    def test_failover_still_refuses_the_windowless_seat(self, temp_home):
        h = _harness(temp_home, model="Fable")
        usage = {"1": None, "2": _seat(five_h=0, seven_d=0), "3": _seat(fable=70)}
        for _ in range(2):
            assert h.tick_with_usage(usage) is TickOutcome.NO_ACTION
        outcome = h.tick_with_usage(usage)
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3
        assert _switch(h).trigger == "failover"

    def test_every_candidate_windowless_blocks_and_says_why(self, temp_home):
        h = _harness(temp_home, model="Fable")
        outcome = h.tick_with_usage({
            "1": _seat(fable=95),
            "2": _seat(five_h=10, seven_d=20),
            "3": _seat(five_h=0, seven_d=0),
        })
        # Not "all exhausted": nothing here is exhausted, nothing can serve
        # the model, and a window can reappear at any moment — normal cadence.
        assert outcome is TickOutcome.BLOCKED
        assert h.active_number() == 1
        assert not any(isinstance(e, AllExhaustedEvent) for e in h.events)
        assert h.engine._blocked_wait_long is False
        stop = next(e for e in h.events if isinstance(e, NoSwitchEvent))
        assert stop.reason == "no-qualifying-candidate"
        assert "2 candidate(s) skipped: no Fable window (#2, #3)" in stop.detail

    def test_windowless_seats_do_not_count_as_fleet_quota(self, temp_home):
        # Active and the only Fable-serving peer are both over the bar on
        # Fable; a windowless seat with pristine 5h/7d must not make the fleet
        # look healthy (which would refuse the every-account-above escape and
        # park the engine), nor be the landing.
        h = _harness(temp_home, model="Fable")
        now = h.clock.now
        outcome = h.tick_with_usage({
            "1": _seat(fable=92, fable_reset=now + 80 * H),
            "2": _seat(five_h=0, seven_d=0),
            "3": _seat(fable=91, fable_reset=now + 1 * H),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3

    def test_an_unreadable_candidate_is_unknown_not_skipped(self, temp_home):
        h = _harness(temp_home, model="Fable")
        h.tick_with_usage({
            "1": _seat(fable=95),
            "2": None,
            "3": USAGE_RELOGIN_REQUIRED,
        })
        assert _poll(h).skipped == {}
        assert "skipped" not in _poll(h).human()

    def test_bare_all_never_makes_a_seat_ineligible(self, temp_home):
        h = _harness(temp_home, model="all")
        outcome = h.tick_with_usage({
            "1": {**_seat(), "scoped": [{"name": "Sonnet", "pct": 100.0}]},
            "2": _seat(five_h=10, seven_d=20),   # reports no scoped window
            "3": {**_seat(), "scoped": [{"name": "Sonnet", "pct": 60.0}]},
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2
        assert _poll(h).skipped == {}


class TestConsumeFirstRanksOnTheModelWindow:
    """Rule 2. Weekly (7d) resets are laid out in the OPPOSITE order to the
    Fable resets so the two axes give different answers."""

    def _usage(self, now: float) -> dict:
        return {
            "1": _seat(fable=30, fable_reset=now + 50 * H, seven_d_reset=now + 60 * H),
            "2": _seat(fable=20, fable_reset=now + 2 * H, seven_d_reset=now + 80 * H),
            "3": _seat(fable=20, fable_reset=now + 30 * H, seven_d_reset=now + 40 * H),
            "4": _seat(fable=20, fable_reset=now + 90 * H, seven_d_reset=now + 1 * H),
        }

    def test_picks_the_soonest_fable_reset_with_room(self, temp_home):
        h = _harness(temp_home, seats=4, strategy="consume-first", model="Fable")
        outcome = h.tick_with_usage(self._usage(h.clock.now))
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2
        assert _switch(h).trigger == "consume-first"

    def test_without_a_model_the_weekly_axis_is_unchanged(self, temp_home):
        # The identical fleet, no model configured: 7d ordering as today.
        h = _harness(temp_home, seats=4, strategy="consume-first")
        outcome = h.tick_with_usage(self._usage(h.clock.now))
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 4
        assert _poll(h).skipped == {}

    def test_a_sooner_fable_reset_without_room_is_not_picked(self, temp_home):
        h = _harness(temp_home, seats=5, strategy="consume-first", model="Fable")
        now = h.clock.now
        usage = self._usage(now)
        usage["5"] = _seat(fable=95, fable_reset=now + 1 * H)  # soonest, spent
        outcome = h.tick_with_usage(usage)
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2

    def test_active_already_soonest_on_fable_holds_and_says_so(self, temp_home):
        h = _harness(temp_home, strategy="consume-first", model="Fable")
        now = h.clock.now
        outcome = h.tick_with_usage({
            "1": _seat(fable=30, fable_reset=now + 2 * H, seven_d_reset=now + 90 * H),
            "2": _seat(fable=20, fable_reset=now + 30 * H, seven_d_reset=now + 1 * H),
            "3": _seat(fable=20, fable_reset=now + 90 * H, seven_d_reset=now + 5 * H),
        })
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        stop = next(e for e in h.events if isinstance(e, NoSwitchEvent))
        assert stop.reason == "already-consuming-soonest"
        assert "Fable window" in stop.detail

    def test_hold_detail_names_the_skipped_windowless_seats(self, temp_home):
        h = _harness(temp_home, strategy="consume-first", model="Fable")
        now = h.clock.now
        outcome = h.tick_with_usage({
            "1": _seat(fable=30, fable_reset=now + 2 * H),
            "2": _seat(five_h=0, seven_d=0, seven_d_reset=now + 1 * H),
            "3": _seat(fable=20, fable_reset=now + 30 * H),
        })
        assert outcome is TickOutcome.NO_ACTION
        stop = next(e for e in h.events if isinstance(e, NoSwitchEvent))
        assert stop.reason == "already-consuming-soonest"
        assert "1 candidate(s) skipped: no Fable window (#2)" in stop.detail

    def test_active_fable_reset_unknown_is_reported_on_that_axis(self, temp_home):
        h = _harness(temp_home, strategy="consume-first", model="Fable")
        now = h.clock.now
        outcome = h.tick_with_usage({
            "1": _seat(fable=30, seven_d_reset=now + 90 * H),  # window, no reset
            "2": _seat(fable=20, fable_reset=now + 2 * H),
            "3": _seat(fable=20, fable_reset=now + 30 * H),
        })
        assert outcome is TickOutcome.NO_ACTION
        stop = next(e for e in h.events if isinstance(e, NoSwitchEvent))
        assert stop.reason == "reset-unknown"
        assert "Fable window reset time is unknown" in stop.detail

    def test_legacy_weekly_wording_is_untouched(self, temp_home):
        h = _harness(temp_home, strategy="consume-first")
        now = h.clock.now
        h.tick_with_usage({
            "1": _seat(),
            "2": _seat(seven_d_reset=now + 2 * H),
            "3": _seat(seven_d_reset=now + 30 * H),
        })
        stop = next(e for e in h.events if isinstance(e, NoSwitchEvent))
        assert stop.reason == "reset-unknown"
        assert stop.detail.startswith("active account's weekly reset time is unknown")
        assert _seven_day_reset_ts(_seat(seven_d_reset=now + 2 * H), now) == now + 2 * H


class TestActiveLosesItsWindow:
    """Rule 3: N distinct fresh reads without the window, never one."""

    def _fleet(self, active: dict) -> dict:
        return {
            "1": active,
            "2": _seat(five_h=0, seven_d=0),      # pristine, no Fable window
            "3": _seat(five_h=40, fable=50),      # the only seat serving Fable
        }

    def _tick(self, h: EngineHarness, active: dict) -> TickOutcome:
        # Distinct fetched_at per tick, as distinct real fetches would carry.
        h.clock.advance(300)
        return h.tick_with_usage(self._fleet(active))

    def test_third_distinct_missing_read_leaves_for_a_windowed_seat(self, temp_home):
        h = _harness(temp_home, model="Fable")
        gone = _seat(five_h=52, seven_d=30)  # what the seat read once its window vanished
        for n in (1, 2):
            assert self._tick(h, gone) is TickOutcome.NO_ACTION
            stop = [e for e in h.events if isinstance(e, NoSwitchEvent)][-1]
            assert stop.reason == MODEL_WINDOW_MISSING
            assert f"{n}/3 reads" in stop.detail
            assert h.active_number() == 1
        assert self._tick(h, gone) is TickOutcome.SWITCHED
        assert h.active_number() == 3
        lost = next(e for e in h.events if isinstance(e, ModelWindowLostEvent))
        assert lost.reads == 3 and lost.model == "Fable" and lost.number == "1"
        assert lost.to_json()["event"] == "model-window-lost"
        assert "no Fable window on 3 consecutive reads" in lost.human()
        assert _switch(h).trigger == "at-limit"

    def test_unhealthy_ticks_setting_is_the_bar(self, temp_home):
        h = _harness(temp_home, model="Fable", unhealthy_ticks=5)
        gone = _seat(five_h=52, seven_d=30)
        for _ in range(4):
            assert self._tick(h, gone) is TickOutcome.NO_ACTION
        assert self._tick(h, gone) is TickOutcome.SWITCHED

    def test_an_unreadable_read_neither_counts_nor_resets(self, temp_home):
        h = _harness(temp_home, model="Fable")
        gone = _seat(five_h=52, seven_d=30)
        assert self._tick(h, gone) is TickOutcome.NO_ACTION            # 1
        assert self._tick(h, None) is TickOutcome.NO_ACTION            # unreadable
        assert _reasons(h)[-1] == "active-usage-unknown"
        assert self._tick(h, gone) is TickOutcome.NO_ACTION            # 2
        assert "2/3 reads" in [e for e in h.events if isinstance(e, NoSwitchEvent)][-1].detail
        assert self._tick(h, gone) is TickOutcome.SWITCHED             # 3

    def test_a_read_with_the_window_resets_the_count(self, temp_home):
        h = _harness(temp_home, model="Fable")
        gone = _seat(five_h=52, seven_d=30)
        assert self._tick(h, gone) is TickOutcome.NO_ACTION
        assert self._tick(h, gone) is TickOutcome.NO_ACTION
        assert self._tick(h, _seat(five_h=52, seven_d=30, fable=40)) is TickOutcome.NO_ACTION
        assert _reasons(h)[-1] == "below-threshold"
        assert self._tick(h, gone) is TickOutcome.NO_ACTION
        assert "1/3 reads" in [e for e in h.events if isinstance(e, NoSwitchEvent)][-1].detail

    def test_one_snapshot_served_three_ticks_is_one_read(self, temp_home):
        # Same fetched_at every tick (the clock does not move): the store is
        # serving one fetch, and one fetch must never be enough.
        h = _harness(temp_home, model="Fable")
        gone = _seat(five_h=52, seven_d=30)
        for _ in range(3):
            assert h.tick_with_usage(self._fleet(gone)) is TickOutcome.NO_ACTION
            assert "1/3 reads" in [e for e in h.events if isinstance(e, NoSwitchEvent)][-1].detail
        assert h.active_number() == 1

    def test_lost_window_ignores_cooldown_and_refetches_candidates(self, temp_home):
        h = _harness(temp_home, model="Fable")
        now = h.clock.now
        gone = _seat(five_h=52, seven_d=30)
        h.engine._mutate_state(lambda s: s.__setitem__("lastSwitchAt", now))  # inside cooldown
        for _ in range(2):
            self._tick(h, gone)
        h.clock.advance(300)
        entries = {n: _entry_for(v, h.clock.now) for n, v in self._fleet(gone).items()}
        with patch.object(
            h.switcher, "usage_entries_by_account", return_value=entries
        ) as fetch:
            assert h.engine.tick() is TickOutcome.SWITCHED
        assert h.active_number() == 3
        assert {"2", "3"} in [c.kwargs.get("fetch") for c in fetch.call_args_list]

    def test_without_a_windowed_candidate_it_blocks_at_normal_cadence(self, temp_home):
        h = _harness(temp_home, model="Fable")
        gone = _seat(five_h=52, seven_d=30)
        fleet = {"1": gone, "2": _seat(five_h=0, seven_d=0), "3": _seat(five_h=0, seven_d=0)}
        for _ in range(2):
            h.clock.advance(300)
            assert h.tick_with_usage(fleet) is TickOutcome.NO_ACTION
        h.clock.advance(300)
        assert h.tick_with_usage(fleet) is TickOutcome.BLOCKED
        assert h.active_number() == 1
        assert any(isinstance(e, ModelWindowLostEvent) for e in h.events)
        stop = [e for e in h.events if isinstance(e, NoSwitchEvent)][-1]
        assert stop.reason == "no-qualifying-candidate"
        assert "2 candidate(s) skipped" in stop.detail
        assert h.engine._blocked_wait_long is False

    def test_no_model_configured_never_counts(self, temp_home):
        h = _harness(temp_home)
        gone = _seat(five_h=52, seven_d=30)
        for _ in range(4):
            assert self._tick(h, gone) is TickOutcome.NO_ACTION
            assert _reasons(h)[-1] == "below-threshold"
        assert not any(isinstance(e, ModelWindowLostEvent) for e in h.events)


class TestPR321ScenarioIsNotWorse:
    """Every candidate over the bar ONLY on the model window, the active at
    its wall (the shape upstream PR #321 describes). This module does not fix
    it — that is #321's job — so pin today's behaviour and show that adding a
    windowless seat changes nothing about it: the seat is never the landing
    and never alters the verdict."""

    def _blocked_fleet(self, now: float) -> dict:
        return {
            "1": _seat(fable=100, fable_reset=now + 80 * H),   # active, at the wall
            "2": _seat(five_h=3, seven_d=5, fable=92, fable_reset=now + 6 * H),
            "3": _seat(five_h=3, seven_d=5, fable=95, fable_reset=now + 30 * H),
        }

    @pytest.mark.parametrize("strategy", ["best", "consume-first"])
    @pytest.mark.parametrize("windowless_seat", [False, True])
    def test_pinned_baseline_with_and_without_a_windowless_seat(
        self, temp_home, strategy, windowless_seat
    ):
        h = _harness(temp_home, seats=4 if windowless_seat else 3,
                     strategy=strategy, model="Fable")
        fleet = self._blocked_fleet(h.clock.now)
        if windowless_seat:
            fleet["4"] = _seat(five_h=0, seven_d=0)   # pristine 5h/7d, no Fable
        outcome = h.tick_with_usage(fleet)
        # Baseline as of v0.26.0 (identical with the seat absent): the
        # at-limit escape takes the candidate with the most model headroom
        # even though it is over the threshold — better than sitting at the
        # wall — and #2 is the better of the two. The windowless seat must
        # neither become the landing nor change that verdict.
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2
        assert _switch(h).trigger == "at-limit"
        assert _poll(h).skipped == ({"4": MODEL_WINDOW_MISSING} if windowless_seat else {})


class TestReviewRoundOne:
    """Findings from the first review round, each pinned."""

    def test_hard_limit_escape_is_not_delayed_by_the_confirmation(self, temp_home):
        # P1: the active is at 5h 100% AND reports no window on its FIRST
        # missing read. The at-limit escape is owed on the account-wide
        # measurement alone; the missing-window confirmation gates only its
        # own trigger.
        h = _harness(temp_home, model="Fable")
        outcome = h.tick_with_usage({
            "1": _seat(five_h=100, seven_d=30),
            "2": _seat(five_h=0, seven_d=0),      # no window: still refused
            "3": _seat(five_h=40, fable=50),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3
        assert _switch(h).trigger == "at-limit"
        assert MODEL_WINDOW_MISSING not in _reasons(h)
        assert not any(isinstance(e, ModelWindowLostEvent) for e in h.events)

    def test_proactive_switch_is_not_delayed_either(self, temp_home):
        h = _harness(temp_home, model="Fable")
        outcome = h.tick_with_usage({
            "1": _seat(five_h=92, seven_d=30),    # over the bar on 5h, no window
            "2": _seat(five_h=0, seven_d=0),
            "3": _seat(five_h=40, fable=50),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3
        assert _switch(h).trigger == "proactive"

    def test_missing_reads_do_not_carry_over_to_another_account(self, temp_home):
        # P2: two missing reads on #1, then a manual switch to #2 — #2's first
        # missing read is 1/3, not 3/3.
        h = _harness(temp_home, model="Fable")
        gone = _seat(five_h=52, seven_d=30)
        fleet = {"1": gone, "2": gone, "3": _seat(five_h=40, fable=50)}
        for _ in range(2):
            h.clock.advance(300)
            assert h.tick_with_usage(fleet) is TickOutcome.NO_ACTION
        assert "2/3 reads" in [e for e in h.events if isinstance(e, NoSwitchEvent)][-1].detail
        h.switcher.switch_to("2", json_output=True)
        assert h.active_number() == 2
        h.clock.advance(300)
        assert h.tick_with_usage(fleet) is TickOutcome.NO_ACTION
        assert h.active_number() == 2
        assert "1/3 reads" in [e for e in h.events if isinstance(e, NoSwitchEvent)][-1].detail

    def test_lost_window_refetch_honours_a_post_429_exhausted_plan(self, temp_home):
        # P2: an exhausted candidate whose persisted plan is wider than the
        # exhausted cadence and not yet due is left out of the refetch, as
        # the escalation fetch already leaves it out.
        from claude_swap import poll_policy

        h = _harness(temp_home, model="Fable")
        gone = _seat(five_h=52, seven_d=30)
        now = h.clock.now
        exhausted = replace(
            _entry_for(_seat(five_h=100, fable=100), now),
            next_poll_at=now + 5 * H,
            poll_interval_s=poll_policy.EXHAUSTED_INTERVAL_S * 4,
        )
        entries = {
            "1": _entry_for(gone, now),
            "2": exhausted,
            "3": _entry_for(_seat(five_h=40, fable=50), now),
        }
        for _ in range(2):
            h.clock.advance(300)
            entries["1"] = _entry_for(gone, h.clock.now)
            assert h.tick_with_entries(entries) is TickOutcome.NO_ACTION
        h.clock.advance(300)
        entries["1"] = _entry_for(gone, h.clock.now)
        with patch.object(
            h.switcher, "usage_entries_by_account", return_value=entries
        ) as fetch:
            assert h.engine.tick() is TickOutcome.SWITCHED
        assert h.active_number() == 3
        fetched = [c.kwargs.get("fetch") for c in fetch.call_args_list]
        assert {"3"} in fetched and {"2", "3"} not in fetched

    def test_scoped_only_payload_counts_as_present_and_resets(self, temp_home):
        # P2: a payload with the Fable window but no 5h/7d data is evidence
        # FOR the window — present, and it resets the count.
        scoped_only = {"scoped": [{"name": "Fable", "pct": 40.0}]}
        assert _model_window_state(scoped_only, ("Fable",)) == "present"
        h = _harness(temp_home, model="Fable")
        gone = _seat(five_h=52, seven_d=30)
        fleet = {"1": gone, "2": _seat(five_h=0, seven_d=0), "3": _seat(five_h=40, fable=50)}
        for _ in range(2):
            h.clock.advance(300)
            assert h.tick_with_usage(fleet) is TickOutcome.NO_ACTION
        h.clock.advance(300)
        assert h.tick_with_usage({**fleet, "1": scoped_only}) is TickOutcome.NO_ACTION
        assert _reasons(h)[-1] == "below-threshold"
        h.clock.advance(300)
        assert h.tick_with_usage(fleet) is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        assert "1/3 reads" in [e for e in h.events if isinstance(e, NoSwitchEvent)][-1].detail


class TestReviewRoundTwo:
    def test_lost_window_departure_is_recorded_as_spent(self, temp_home):
        # P2: the no-return release compares the left seat's later headroom
        # against what we recorded on leaving. A seat left for losing its
        # model window served nothing for the model, so it is recorded spent
        # — a window returning with a few points then reads as recovered.
        h = _harness(temp_home, model="Fable")
        gone = _seat(five_h=52, seven_d=30)   # 48 account-wide points
        fleet = {"1": gone, "2": _seat(five_h=0, seven_d=0), "3": _seat(five_h=40, fable=50)}
        for _ in range(3):
            h.clock.advance(300)
            outcome = h.tick_with_usage(fleet)
        assert outcome is TickOutcome.SWITCHED
        state = h.state()
        assert state["lastSwitchFrom"] == 1
        assert state["leftHeadroom"] == 0.0
        assert state["leftTrigger"] == "at-limit"

    def test_slot_replacement_starts_the_count_over(self, temp_home):
        # P2: `cswap add --slot 1` keeps the number but changes the account;
        # the two observations belonged to the previous login.
        h = _harness(temp_home, model="Fable")
        gone = _seat(five_h=52, seven_d=30)
        fleet = {"1": gone, "2": _seat(five_h=0, seven_d=0), "3": _seat(five_h=40, fable=50)}
        for _ in range(2):
            h.clock.advance(300)
            assert h.tick_with_usage(fleet) is TickOutcome.NO_ACTION
        assert "2/3 reads" in [e for e in h.events if isinstance(e, NoSwitchEvent)][-1].detail
        data = h.switcher._get_sequence_data()
        data["accounts"]["1"]["uuid"] = "uuid-replacement"
        h.switcher._write_json(h.switcher.sequence_file, data)
        h.clock.advance(300)
        assert h.tick_with_usage(fleet) is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        assert "1/3 reads" in [e for e in h.events if isinstance(e, NoSwitchEvent)][-1].detail
