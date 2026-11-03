"""Where ``auto`` lands: an opportunistic move needs room, an escape prefers a healthy target.

Replays one machine's log (0.29.0b1, ``--threshold 90 --strategy consume-first``, five
accounts, live usage from ``cswap proxy``)::

    21:22:56  Account-2: 5h 16% used | others: #1: 5h 85% · 7d 51%, ...
    21:22:58  Switched Account-2 -> Account-1 (consume-first)
    21:25:03  Account-1: 5h 92% used (switch at 90%)    no switch: cooldown
    21:26:58  Account-1: 5h 99% used (switch at 90%)    no switch: cooldown
    (21:27   sessions: "You've hit your session limit")
    21:28:00  Switched Account-1 -> Account-5 (at-limit)     # 7d 91%: over its line
    21:28:56  Account-5: 7d 91% used (switch at 90%)    no switch: cooldown   # #2 at 16% / 2%

Two separate faults:

1. consume-first moved onto an account five points under its line. The move is
   opportunistic (the active account was at 16%), and the cooldown it starts then holds
   ``auto`` on that account while it crosses the line and runs out.
2. the at-limit escape ranked by weekly reset alone and landed on an account already over
   its weekly line, while one with real room was available.
"""

from __future__ import annotations

from claude_swap.autoswitch import NoSwitchEvent, SwitchEvent, TickOutcome
from tests.test_autoswitch import _R_LATER, _R_LATEST, _R_SOON, EngineHarness, _usage7


def _harness(temp_home, n: int = 3) -> EngineHarness:
    h = EngineHarness(temp_home, strategy="consume-first")
    for num in range(1, n + 1):
        h.seed(num, f"{'abcde'[num - 1]}@example.com")
    h.make_live("a@example.com", 1)
    return h


class TestConsumeFirstNeedsRoom:
    def test_does_not_move_onto_an_account_a_few_points_under_its_line(self, temp_home):
        # Active #1 plays the log's Account-2 (16%, resets later); #2 plays Account-1
        # (85% of a 90 line, resets sooner). Landing there buys a few minutes, then the
        # cooldown pins auto to an account it must leave.
        h = _harness(temp_home, 2)
        outcome = h.tick_with_usage({
            "1": _usage7(16, 2, _R_LATEST),
            "2": _usage7(85, 51, _R_SOON),
        })
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        assert [e.reason for e in h.events if isinstance(e, NoSwitchEvent)] == [
            "already-consuming-soonest"
        ]

    def test_still_moves_onto_a_sooner_account_with_room(self, temp_home):
        # The strategy itself is unchanged: 20 points under the line is room.
        h = _harness(temp_home, 2)
        outcome = h.tick_with_usage({
            "1": _usage7(16, 2, _R_LATEST),
            "2": _usage7(70, 51, _R_SOON),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2

    def test_the_room_bar_is_the_hysteresis_margin(self, temp_home):
        # Exactly hysteresis_pct (10) under the line qualifies; one point less does not.
        h = _harness(temp_home, 2)
        assert h.tick_with_usage({
            "1": _usage7(16, 2, _R_LATEST),
            "2": _usage7(81, 51, _R_SOON),
        }) is TickOutcome.NO_ACTION
        h.events.clear()
        assert h.tick_with_usage({
            "1": _usage7(16, 2, _R_LATEST),
            "2": _usage7(80, 51, _R_SOON),
        }) is TickOutcome.SWITCHED

    def test_the_weekly_window_counts_too(self, temp_home):
        # Room is measured on every window against its own line: 7d 85% is as close to
        # a 90 line as 5h 85% is.
        h = _harness(temp_home, 2)
        outcome = h.tick_with_usage({
            "1": _usage7(16, 2, _R_LATEST),
            "2": _usage7(10, 85, _R_SOON),
        })
        assert outcome is TickOutcome.NO_ACTION


class TestForcedMovePrefersRoom:
    def test_over_its_line_it_prefers_a_target_with_room_to_a_sooner_one_without(self, temp_home):
        # Active over its line must move. #2 resets soonest but sits 5 points under its
        # line: landing there starts a cooldown it will need to break within minutes.
        # #3 has room, so it wins even though it resets later.
        h = _harness(temp_home, 3)
        outcome = h.tick_with_usage({
            "1": _usage7(95, 20, _R_LATER),
            "2": _usage7(85, 40, _R_SOON),
            "3": _usage7(10, 10, _R_LATEST),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3

    def test_with_room_everywhere_the_sooner_reset_still_wins(self, temp_home):
        h = _harness(temp_home, 3)
        h.tick_with_usage({
            "1": _usage7(95, 20, _R_LATER),
            "2": _usage7(50, 40, _R_SOON),
            "3": _usage7(10, 10, _R_LATEST),
        })
        assert h.active_number() == 2

    def test_with_no_target_that_has_room_it_still_moves(self, temp_home):
        # Must move: a target under its line, however close, beats staying over it.
        h = _harness(temp_home, 2)
        outcome = h.tick_with_usage({
            "1": _usage7(95, 20, _R_LATER),
            "2": _usage7(85, 40, _R_SOON),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2


class TestAtLimitEscapePrefersHealthy:
    def test_escapes_to_the_account_with_room_not_the_one_over_its_line(self, temp_home):
        # The log's 21:28: active at 100%; #2 resets soonest but is at 7d 91% (over its
        # line); #3 has real room. consume-first ranked by reset alone and chose #2.
        h = _harness(temp_home, 3)
        outcome = h.tick_with_usage({
            "1": _usage7(100, 52, _R_LATER),
            "2": _usage7(0, 91, _R_SOON),
            "3": _usage7(16, 2, _R_LATEST),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3
        sw = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert sw.trigger == "at-limit"

    def test_among_healthy_targets_the_strategy_still_decides(self, temp_home):
        # Two accounts below their lines: consume-first still takes the sooner reset.
        h = _harness(temp_home, 3)
        h.tick_with_usage({
            "1": _usage7(100, 52, _R_LATER),
            "2": _usage7(20, 40, _R_SOON),
            "3": _usage7(16, 2, _R_LATEST),
        })
        assert h.active_number() == 2

    def test_with_only_accounts_over_their_lines_it_still_escapes(self, temp_home):
        # An escape from a dead account never stalls for want of a healthy target.
        h = _harness(temp_home, 3)
        outcome = h.tick_with_usage({
            "1": _usage7(100, 52, _R_LATER),
            "2": _usage7(0, 91, _R_SOON),
            "3": _usage7(0, 95, _R_LATEST),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() in (2, 3)
