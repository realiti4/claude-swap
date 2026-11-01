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


def _fleet(temp_home, n: int, lines: dict[str, float] | None = None, **settings):
    """``n`` accounts under ``settings``, #1 active; ``lines`` sets accounts' own."""
    settings.setdefault("strategy", "consume-first")
    h = EngineHarness(temp_home, **settings)
    for num in range(1, n + 1):
        h.seed(num, f"{num}@example.com")
    h.make_live("1@example.com", 1)
    for num, line in (lines or {}).items():
        h.switcher.set_account_threshold(num, line)
    h.engine = h._make_engine()
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


class TestRoomIsCountedInEachWindowsOwnPoints:
    """``line_headroom - (100 - threshold)`` is the room on the 5h scale: exact on a
    line equal to the global one, ``threshold / line`` times the real points on any
    other. It under-reads a higher weekly line and over-reads a lower account line."""

    def test_ten_points_under_a_98_weekly_line_is_room(self, temp_home):
        # 7d 88% under a 98 line: 10 points, read as 90 * 10 / 98 = 9.18.
        h = _fleet(temp_home, 2, threshold=90.0, weekly_threshold=98.0)
        outcome = h.tick_with_usage({
            "1": _usage7(16, 2, _R_LATEST),
            "2": _usage7(0, 88, _R_SOON),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2

    def test_nine_points_under_it_is_not(self, temp_home):
        h = _fleet(temp_home, 2, threshold=90.0, weekly_threshold=98.0)
        outcome = h.tick_with_usage({
            "1": _usage7(16, 2, _R_LATEST),
            "2": _usage7(0, 89, _R_SOON),
        })
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1

    def test_eight_points_under_a_lower_account_line_is_not_room(self, temp_home):
        # Account 2 keeps its own line at 60 (global 90). At 52% it has 8 points,
        # read as 90 * 8 / 60 = 12: the opportunistic move lands short of the margin.
        h = _fleet(temp_home, 2, lines={"2": 60.0}, threshold=90.0)
        outcome = h.tick_with_usage({
            "1": _usage7(16, 2, _R_LATEST),
            "2": _usage7(0, 52, _R_SOON),
        })
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1

    def test_ten_points_under_it_is(self, temp_home):
        h = _fleet(temp_home, 2, lines={"2": 60.0}, threshold=90.0)
        outcome = h.tick_with_usage({
            "1": _usage7(16, 2, _R_LATEST),
            "2": _usage7(0, 50, _R_SOON),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2

    def test_a_forced_move_prefers_real_room(self, temp_home):
        # Over its line, the active must move. 2 resets soonest with 8 real points
        # under its own 60 line (read as 12); 3 has 30 points.
        h = _fleet(temp_home, 3, lines={"2": 60.0}, threshold=90.0)
        outcome = h.tick_with_usage({
            "1": _usage7(95, 20, _R_LATER),
            "2": _usage7(0, 52, _R_SOON),
            "3": _usage7(60, 10, _R_LATEST),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3


class TestRoomWithoutAWindow:
    def test_falls_back_to_the_single_line_against_raw_headroom(self, temp_home):
        # The ranking takes headroom and usage apart. With no window to read, room is
        # the account's line against raw headroom, as its line headroom and verdict
        # are: 2 has 50 points under 90, so a forced move takes it ahead of 3, which
        # resets sooner but is 5 under its line.
        h = _fleet(temp_home, 3, threshold=90.0)
        ordered, _, _ = h.engine._rank_candidates(
            trigger="proactive",
            consume_first=True,
            oauth_candidates=["2", "3"],
            no_return=None,
            usage={"1": _usage7(95, 50, _R_LATER), "2": None, "3": _usage7(0, 85, _R_SOON)},
            headroom={"1": 5.0, "2": 60.0, "3": 15.0},
            current="1",
            active_headroom=5.0,
            settings=h.settings,
            now=h.clock.now,
        )
        assert ordered == ["2", "3"]
