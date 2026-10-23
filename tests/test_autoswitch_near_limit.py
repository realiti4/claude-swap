"""``auto`` leaves an account that is about to run out, even when every peer is over a line.

Replays one machine's log (fork build with #501 + #504, ``--threshold 85 --strategy
consume-first``, no weekly line, so 85 applies to the 7-day windows too)::

    22:58:18  Account-2: 5h 85% used (switch at 85%) | others: #1: 5h 100% · 7d 52%,
              #3: 5h 80% · 7d 90%, #4: 5h 0% · 7d 90%, #5: 5h 2% · 7d 91%
    22:58:18  no switch: no-qualifying-candidate
    ...       (every minute, 86% ... 99%)
    23:09:44  sessions: "You've hit your session limit · resets 2:10am"
    23:10:21  Account-2: 5h 100% used
    23:10:21  Switched Account-2 -> Account-5 (at-limit)

Every peer was over one of its lines (#3/#4/#5 on the weekly window), so the all-above
ranking compared when each account returns BELOW ITS LINE: Account-2's five-hour window in
about 3 hours against the peers' weekly windows in days, so staying looked better. But
#3/#4/#5 each still had about 10 points of real quota, and Account-2 had 15, then 4, then
none: the session stopped while three accounts could have served it.

The fix: when the active account's raw headroom is down to ``PREEMPT_HEADROOM_PCT``, the
tick is a ``near-limit`` escape, ranked and cooldown-free like at-limit, onto a target with
more real headroom than that. A proactive move never lands on an account that close to
empty, so the escape cannot be undone on the next tick.
"""

from __future__ import annotations

from claude_swap.autoswitch import PREEMPT_HEADROOM_PCT, NoSwitchEvent, SwitchEvent, TickOutcome
from tests.test_autoswitch import EngineHarness, _iso_at

HOUR = 3600.0
DAY = 24 * HOUR


def _harness(temp_home, **kwargs) -> EngineHarness:
    h = EngineHarness(temp_home, threshold=85.0, strategy="consume-first", **kwargs)
    for num in range(1, 6):
        h.seed(num, f"{'abcde'[num - 1]}@example.com")
    h.make_live("a@example.com", 1)
    return h


def _u(h: EngineHarness, pct5: float, pct7: float, reset5_in: float, reset7_in: float) -> dict:
    now = h.clock.now
    return {
        "five_hour": {"pct": pct5, "resets_at": _iso_at(now + reset5_in)},
        "seven_day": {"pct": pct7, "resets_at": _iso_at(now + reset7_in)},
    }


def _fleet(h: EngineHarness, active_pct5: float) -> dict:
    """The log's fleet, with slot 1 playing Account-2 (the active one)."""
    return {
        "1": _u(h, active_pct5, 10, 3 * HOUR, 6 * DAY),   # Account-2
        "2": _u(h, 100, 52, 0.5 * HOUR, 4 * DAY),         # Account-1, spent
        "3": _u(h, 80, 90, 2 * HOUR, 3.5 * DAY),          # Account-3, 7d over 85
        "4": _u(h, 0, 90, 4 * HOUR, 2 * DAY),             # Account-4, 7d over 85
        "5": _u(h, 2, 91, 4 * HOUR, 1.2 * DAY),           # Account-5, 7d over 85, soonest
    }


def _switch(h: EngineHarness) -> SwitchEvent:
    return next(e for e in h.events if isinstance(e, SwitchEvent))


class TestNearLimitEscape:
    def test_with_peers_over_their_lines_it_holds_while_the_active_has_room(self, temp_home):
        # Unchanged: above the threshold with no peer below its lines, and real room
        # left on the active, staying is still the right call.
        h = _harness(temp_home)
        assert h.tick_with_usage(_fleet(h, 90)) is not TickOutcome.SWITCHED
        assert h.active_number() == 1

    def test_it_escapes_before_the_active_runs_out(self, temp_home):
        # 96%: four points left, a peer has ten. The log stayed here until 100%.
        h = _harness(temp_home)
        assert h.tick_with_usage(_fleet(h, 96)) is TickOutcome.SWITCHED
        assert h.active_number() != 1
        assert _switch(h).trigger == "near-limit"

    def test_the_line_is_preempt_headroom_pct(self, temp_home):
        h = _harness(temp_home)
        assert h.tick_with_usage(_fleet(h, 100 - PREEMPT_HEADROOM_PCT - 1)) is not TickOutcome.SWITCHED
        h.events.clear()
        assert h.tick_with_usage(_fleet(h, 100 - PREEMPT_HEADROOM_PCT)) is TickOutcome.SWITCHED

    def test_the_strategy_picks_among_the_targets(self, temp_home):
        # All three usable peers are over a line: consume-first takes the soonest weekly
        # reset (#5), which still has 9 points of real quota.
        h = _harness(temp_home)
        h.tick_with_usage(_fleet(h, 97))
        assert h.active_number() == 5

    def test_a_target_below_its_lines_still_wins(self, temp_home):
        h = _harness(temp_home)
        fleet = _fleet(h, 97)
        fleet["4"] = _u(h, 0, 40, 4 * HOUR, 2 * DAY)  # now healthy
        h.tick_with_usage(fleet)
        assert h.active_number() == 4

    def test_it_ignores_the_cooldown(self, temp_home):
        # A switch a minute ago must not hold an account that is about to stop working.
        h = _harness(temp_home)
        h.engine._mutate_state(lambda s: s.update(lastSwitchAt=h.clock() - 60))
        assert h.tick_with_usage(_fleet(h, 97)) is TickOutcome.SWITCHED
        assert not any(
            isinstance(e, NoSwitchEvent) and e.reason == "cooldown" for e in h.events
        )

    def test_it_never_escapes_onto_an_account_with_less_room(self, temp_home):
        # Active at 97% (3 points); every peer has 3 or fewer: moving gains nothing.
        h = _harness(temp_home)
        fleet = _fleet(h, 97)
        for num in ("3", "4", "5"):
            fleet[num] = _u(h, 97, 97, 4 * HOUR, 2 * DAY)
        assert h.tick_with_usage(fleet) is not TickOutcome.SWITCHED
        assert h.active_number() == 1

    def test_a_target_must_have_more_than_the_preempt_margin(self, temp_home):
        # Active at 99% (1 point), one peer at 97% (3 points): more, but itself about to
        # run out, so it would escape straight back.
        h = _harness(temp_home)
        fleet = _fleet(h, 99)
        for num in ("3", "4", "5"):
            fleet[num] = _u(h, 100 - PREEMPT_HEADROOM_PCT, 100 - PREEMPT_HEADROOM_PCT, 4 * HOUR, 2 * DAY)
        assert h.tick_with_usage(fleet) is not TickOutcome.SWITCHED

    def test_an_exhausted_active_is_still_at_limit(self, temp_home):
        h = _harness(temp_home)
        h.tick_with_usage(_fleet(h, 100))
        assert _switch(h).trigger == "at-limit"


class TestNoBounceBack:
    def test_it_does_not_return_to_the_account_it_just_escaped(self, temp_home):
        # After the escape, #5 (7d 91%, over its line) is active and the old account is
        # at 97% but returns below its line in 3 hours. The all-above recovery ranking
        # would move back onto it; with only 3 points it would then escape again.
        h = _harness(temp_home)
        h.tick_with_usage(_fleet(h, 97))
        assert h.active_number() == 5
        h.events.clear()
        h.clock.advance(60)
        h.make_live("e@example.com", 5)
        fleet = _fleet(h, 97)
        assert h.tick_with_usage(fleet) is not TickOutcome.SWITCHED
        assert h.active_number() == 5


class TestEscapeMustBeWorthIt:
    def test_a_trivial_gain_does_not_move(self, temp_home):
        # 95% against a 94% peer (one point more): the escape needs PREEMPT_HEADROOM_PCT
        # more real headroom than the active, or it trades a switch for a minute.
        h = _harness(temp_home)
        fleet = _fleet(h, 95)
        for num in ("3", "4"):
            fleet[num] = _u(h, 100, 100, 4 * HOUR, 2 * DAY)
        fleet["5"] = _u(h, 94, 40, 4 * HOUR, 2 * DAY)
        assert h.tick_with_usage(fleet) is not TickOutcome.SWITCHED
        assert h.active_number() == 1

    def test_an_active_back_within_minutes_is_left_to_reset(self, temp_home):
        # The active's own limit returns in 8 minutes: waiting is upstream's #202 choice
        # and the escape does not override it.
        h = _harness(temp_home)
        fleet = _fleet(h, 97)
        fleet["1"] = _u(h, 97, 10, 8 * 60, 6 * DAY)
        assert h.tick_with_usage(fleet) is not TickOutcome.SWITCHED
        assert h.active_number() == 1

    def test_a_peer_back_within_minutes_may_still_be_landed_on(self, temp_home):
        # #202 from the other side: an active with room may move onto a nearly spent peer
        # whose window returns in minutes; the bounce guard is only for long waits.
        h = _harness(temp_home)
        fleet = {
            "1": _u(h, 91, 10, 2 * HOUR, 6 * DAY),
            "2": _u(h, 94, 10, 0.5 * HOUR, 6 * DAY),
            "3": _u(h, 98, 10, 8 * 60, 6 * DAY),
            "4": _u(h, 100, 100, 4 * HOUR, 2 * DAY),
            "5": _u(h, 100, 100, 4 * HOUR, 2 * DAY),
        }
        h.tick_with_usage(fleet)
        assert h.active_number() == 3

    def test_in_cooldown_an_active_back_within_minutes_keeps_the_cooldown(self, temp_home):
        h = _harness(temp_home)
        h.engine._mutate_state(lambda s: s.update(lastSwitchAt=h.clock() - 60))
        fleet = _fleet(h, 97)
        fleet["1"] = _u(h, 97, 10, 8 * 60, 6 * DAY)
        assert h.tick_with_usage(fleet) is TickOutcome.NO_ACTION
        assert [e.reason for e in h.events if isinstance(e, NoSwitchEvent)] == ["cooldown"]
