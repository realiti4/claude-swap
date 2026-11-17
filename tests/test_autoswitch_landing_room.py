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

from unittest.mock import patch

import pytest

from claude_swap.autoswitch import (
    AllExhaustedEvent,
    NoSwitchEvent,
    SwitchEvent,
    TickOutcome,
)
from tests.test_autoswitch import (
    _R_LATER,
    _R_LATEST,
    _R_SOON,
    EngineHarness,
    _header_usage,
    _usage7,
)


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


def _fable(pct5: float, pct7: float, fable: float, reset7: str) -> dict:
    """Usage with a per-model weekly window, as ``autoswitch.model Fable`` reads it."""
    return {
        "five_hour": {"pct": pct5},
        "seven_day": {"pct": pct7, "resets_at": reset7},
        "scoped": [{"name": "Fable", "pct": fable, "resets_at": reset7}],
    }


def _switches(h: EngineHarness) -> list[tuple[str, int]]:
    return [
        (e.trigger, (e.to_ref or {}).get("number"))
        for e in h.events
        if isinstance(e, SwitchEvent)
    ]


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


    def test_a_weekly_first_forced_move_prefers_room_too(self, temp_home):
        # weekly-first also lands a forced move on the soonest weekly reset (#541),
        # so the same trap applies: #2 resets first but sits 5 points under its line.
        h = _fleet(temp_home, 3, strategy="weekly-first", threshold=90.0)
        outcome = h.tick_with_usage({
            "1": _usage7(95, 20, _R_LATER),
            "2": _usage7(85, 40, _R_SOON),
            "3": _usage7(10, 10, _R_LATEST),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3

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


class TestIssue305:
    """#305's log (``consume-first``, ``model Fable``, threshold 90, nine accounts)::

        Account-9: usage unknown ... #1: 5h 1% · 7d 3% · Fable 2%,
                                     #4: 5h 0% · 7d 73% · Fable 94%, ...
        Switched Account-9 -> Account-4 (failover)
        Account-4: 94% used (switch at 90%)    no switch: cooldown   [x5 ticks]
        Switched Account-4 -> Account-8 (proactive)

    Account-4's Fable window is over its line, so nothing is all-above and #473's
    ranking does not apply: the escape sorted by weekly reset.
    """

    def test_fails_over_to_the_account_with_room(self, temp_home):
        # 2 plays Account-1, 3 plays Account-4 (weekly reset soonest).
        h = _fleet(temp_home, 3, threshold=90.0, model="Fable")
        usage = {
            "1": None,
            "2": _fable(1, 3, 2, _R_LATEST),
            "3": _fable(0, 73, 94, _R_SOON),
        }
        h.tick_with_usage(usage)
        h.tick_with_usage(usage)
        assert h.tick_with_usage(usage) is TickOutcome.SWITCHED
        assert _switches(h) == [("failover", 2)]


class TestIssue305MixedCase:
    """The 0.30.0 report on #305 (``--strategy consume-first --threshold 95
    --weekly-threshold 90``): the active account hit 100% on its 5h window, one
    candidate was at 7d 98% with the soonest weekly reset, another at 7d 4% with a
    later reset and a low 5h. The at-limit escape took the 98% account, past its 90
    weekly line, and moved again two minutes later. The report gives no other
    readings; the 5h and the active's 7d values below are this test's.
    """

    def test_escapes_to_the_account_under_every_line(self, temp_home):
        # 2 plays the report's account 1 (98%, soonest), 3 plays its account 5 (4%).
        h = _fleet(temp_home, 3, threshold=95.0, weekly_threshold=90.0)
        outcome = h.tick_with_usage({
            "1": _usage7(100, 50, _R_LATER),
            "2": _usage7(0, 98, _R_SOON),
            "3": _usage7(10, 4, _R_LATEST),
        })
        assert outcome is TickOutcome.SWITCHED
        assert _switches(h) == [("at-limit", 3)]

    def test_half_a_point_under_the_weekly_line_is_not_room(self, temp_home):
        # The same fleet with the soonest account just under its weekly line instead
        # of past it. Under its lines and resetting first, it would still win a ranking
        # that only puts under-line accounts ahead, and it crosses 90 within minutes.
        h = _fleet(temp_home, 3, threshold=95.0, weekly_threshold=90.0)
        outcome = h.tick_with_usage({
            "1": _usage7(100, 50, _R_LATER),
            "2": _usage7(0, 89.5, _R_SOON),
            "3": _usage7(10, 4, _R_LATEST),
        })
        assert outcome is TickOutcome.SWITCHED
        assert _switches(h) == [("at-limit", 3)]


class TestASecondLogSamePattern:
    """A second log (``threshold 98``, consume-first, no weekly line)::

        Switched A -> B (at-limit)   # A's 5h went 93% -> 100% between two checks;
                                     # B: 7d 98%, the soonest weekly reset
        Switched B -> C (at-limit)   # B hit 100% three minutes later; C: 7d 98%
        Switched C -> D (proactive)  # D: 7d 44%, free all along
    """

    def test_lands_on_the_account_with_room_not_the_soonest_reset(self, temp_home):
        # 2 and 3 sit at 7d 98% of a 98 line and reset soonest; 4 is at 44%.
        h = _fleet(temp_home, 4, threshold=98.0)
        outcome = h.tick_with_usage({
            "1": _usage7(100, 74, _R_LATER),
            "2": _usage7(0, 98, _R_SOON),
            "3": _usage7(0, 98, _R_SOON),
            "4": _usage7(0, 44, _R_LATEST),
        })
        assert outcome is TickOutcome.SWITCHED
        assert _switches(h) == [("at-limit", 4)]

    def test_a_tenth_under_the_line_is_not_room(self, temp_home):
        # The same escape a tenth of a point earlier: 7d 97.9% is under the 98 line,
        # so below-the-line-first still takes it over 44% on reset, and it runs out
        # within the cooldown.
        h = _fleet(temp_home, 3, threshold=98.0)
        outcome = h.tick_with_usage({
            "1": _usage7(100, 74, _R_LATER),
            "2": _usage7(0, 97.9, _R_SOON),
            "3": _usage7(0, 44, _R_LATEST),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3


class TestAConsumeFirstEscapeWantsRoom:
    """Under consume-first an escape target with room to spare (the hysteresis margin
    under every line) keeps the weekly-reset order; one without ranks by raw headroom,
    a target under its lines still ahead of one over a line."""

    def test_with_room_to_spare_the_strategy_still_decides(self, temp_home):
        # Both have the margin: the sooner reset, not the emptier account.
        h = _fleet(temp_home, 3, threshold=90.0, weekly_threshold=98.0)
        outcome = h.tick_with_usage({
            "1": _usage7(100, 74, _R_LATER),
            "2": _usage7(0, 80, _R_SOON),
            "3": _usage7(0, 10, _R_LATEST),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2

    def test_room_to_spare_beats_more_raw_headroom_without_it(self, temp_home):
        # 2 has 12 points under its weekly line (14 raw); 3 has 15 raw but only 5
        # under its 5h line.
        h = _fleet(temp_home, 3, threshold=90.0, weekly_threshold=98.0)
        outcome = h.tick_with_usage({
            "1": _usage7(100, 74, _R_LATER),
            "2": _usage7(0, 86, _R_SOON),
            "3": _usage7(85, 0, _R_LATEST),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2

    def test_without_room_the_most_raw_headroom(self, temp_home):
        # Neither has the margin: 3 has 6 raw points, 2 resets sooner with 3.
        h = _fleet(temp_home, 3, threshold=98.0)
        outcome = h.tick_with_usage({
            "1": _usage7(100, 74, _R_LATER),
            "2": _usage7(0, 97, _R_SOON),
            "3": _usage7(0, 94, _R_LATEST),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3

    def test_under_its_lines_still_beats_over_a_line(self, temp_home):
        # 90 / weekly 98. 3 has more raw room (10 to 2.1) but sits on its 5h line.
        h = _fleet(temp_home, 3, threshold=90.0, weekly_threshold=98.0)
        outcome = h.tick_with_usage({
            "1": _usage7(100, 74, _R_LATER),
            "2": _usage7(0, 97.9, _R_SOON),
            "3": _usage7(90, 5, _R_LATEST),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2

    def test_an_escape_over_a_line_would_cost_a_second_move(self, temp_home):
        # The same fleet over five minutes. Landing on 3, over its 5h line, the next
        # proactive check would leave it for 2 once the cooldown ends.
        h = _fleet(temp_home, 3, threshold=90.0, weekly_threshold=98.0)
        first = {
            "1": _usage7(100, 74, _R_LATER),
            "2": _usage7(0, 97.9, _R_SOON),
            "3": _usage7(90, 5, _R_LATEST),
        }
        later = {**first, "3": _usage7(92, 5, _R_LATEST)}
        h.tick_with_usage(first)
        for advance in (60, 301, 60):
            h.clock.advance(advance)
            h.tick_with_usage(later)
        assert _switches(h) == [("at-limit", 2)]

    def test_an_accounts_own_lower_line_is_still_a_line(self, temp_home):
        # 3 keeps its own line at 60 and is at 62%: more raw room than 2 at 97%, but
        # escapes prefer a target under its lines.
        h = _fleet(temp_home, 3, lines={"3": 60.0}, threshold=98.0)
        h.tick_with_usage({
            "1": _usage7(100, 50, _R_LATER),
            "2": _usage7(0, 97, _R_SOON),
            "3": _usage7(0, 62, _R_LATEST),
        })
        assert h.active_number() == 2

    def test_under_its_own_lower_line_it_is_the_roomier(self, temp_home):
        h = _fleet(temp_home, 3, lines={"3": 60.0}, threshold=98.0)
        h.tick_with_usage({
            "1": _usage7(100, 50, _R_LATER),
            "2": _usage7(0, 97, _R_SOON),
            "3": _usage7(0, 55, _R_LATEST),
        })
        assert h.active_number() == 3

    def test_with_no_hysteresis_a_target_at_its_line_is_not_room(self, temp_home):
        # hysteresisPct 0: 2 sits exactly on its 5h line, zero points to its line.
        h = _fleet(temp_home, 3, threshold=90.0, hysteresis_pct=0.0)
        h.tick_with_usage({
            "1": _usage7(100, 50, _R_LATER),
            "2": _usage7(90, 20, _R_SOON),
            "3": _usage7(0, 60, _R_LATEST),
        })
        assert h.active_number() == 3

    def test_still_escapes_when_nothing_has_room(self, temp_home):
        h = _fleet(temp_home, 3, threshold=98.0)
        outcome = h.tick_with_usage({
            "1": _usage7(100, 74, _R_LATER),
            "2": _usage7(0, 99.5, _R_SOON),
            "3": _usage7(0, 97, _R_LATEST),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3

    def test_failover_ranks_the_same_way(self, temp_home):
        # Active unreadable for three ticks: failover is the other escape.
        h = _fleet(temp_home, 3, threshold=98.0)
        usage = {
            "1": None,
            "2": _usage7(0, 97.9, _R_SOON),
            "3": _usage7(0, 44, _R_LATEST),
        }
        h.tick_with_usage(usage)
        h.tick_with_usage(usage)
        assert h.tick_with_usage(usage) is TickOutcome.SWITCHED
        assert _switches(h) == [("failover", 3)]


class TestABestEscapeKeepsItsKey:
    def test_line_headroom_not_a_room_bar(self, temp_home):
        # best already ranks by headroom. 2 has exactly the margin (7d 88% under 98,
        # 12 raw); 3 is 9.9 under its 5h line with 19.9 raw. A room bar would take 2.
        h = _fleet(
            temp_home, 3, threshold=90.0, weekly_threshold=98.0, strategy="best"
        )
        h.tick_with_usage({
            "1": _usage7(100, 50, _R_LATER),
            "2": _usage7(0, 88, _R_SOON),
            "3": _usage7(80.1, 2, _R_LATEST),
        })
        assert h.active_number() == 3

    def test_under_its_lines_beats_over_a_line(self, temp_home):
        h = _fleet(
            temp_home, 3, threshold=90.0, weekly_threshold=98.0, strategy="best"
        )
        h.tick_with_usage({
            "1": _usage7(100, 74, _R_LATER),
            "2": _usage7(0, 97.9, _R_SOON),
            "3": _usage7(90, 5, _R_LATEST),
        })
        assert h.active_number() == 2


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
        ordered, *_ = h.engine._rank_candidates(
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


def _fail_over(h: EngineHarness, usage: dict):
    """Tick ``unhealthy_ticks`` times with the active unreadable, the last one
    being the failover decision; ``h.events`` holds that tick's events only."""
    for i in range(h.settings.unhealthy_ticks):
        if i == h.settings.unhealthy_ticks - 1:
            h.events.clear()
        outcome = h.tick_with_usage(usage)
        h.clock.advance(60.0)
    return outcome


def _held_in_place(
    h: EngineHarness, outcome: TickOutcome, note: str = "",
    reason: str = "no-qualifying-candidate",
) -> None:
    """The designed hold: no switch, a BLOCKED reason, ordinary cadence."""
    assert outcome is TickOutcome.BLOCKED
    assert h.active_number() == 1
    assert not any(isinstance(e, SwitchEvent) for e in h.events)
    (event,) = [e for e in h.events if isinstance(e, NoSwitchEvent)]
    assert event.reason == reason
    assert note in event.detail
    # Not `all-exhausted`'s reset-length sleep, not the long blocked wait.
    assert not any(isinstance(e, AllExhaustedEvent) for e in h.events)
    assert h.engine._sleep_until_ts is None
    assert not h.engine._blocked_wait_long
    assert h.engine._next_delay(outcome) <= 1.1 * h.settings.interval_seconds


class TestFailoverNeverLandsOverTheLine:
    """A failover leaves an active it cannot read, and still never lands on a peer
    at or over its own line: its own switch threshold, else the strategy's bar (97
    under `dynamic`, whatever window and whatever weekly line). Measured 2026-11-16:
    a host egress loss read the active as `usage unknown (network)` for three ticks,
    the first peer to read again sat at 7d 99% with no threshold of its own, and the
    engine moved onto it. The veto is the candidate-selection one (T3465)."""

    def test_a_blind_active_holds_when_the_only_readable_peer_is_over_the_dynamic_bar(
        self, temp_home
    ):
        h = _fleet(temp_home, 2, strategy="dynamic", threshold=90.0)
        usage = {"1": None, "2": _usage7(10, 99, _R_LATER)}
        _held_in_place(
            h, _fail_over(h, usage), "Account-2 (line 97%) at or over its own threshold"
        )
        # The network returns: the active reads again and the counter clears.
        h.events.clear()
        assert h.tick_with_usage({**usage, "1": _usage7(50, 20, _R_LATER)}) is (
            TickOutcome.NO_ACTION
        )
        assert h.engine._unhealthy_ticks == 0

    def test_the_other_peers_being_unreadable_does_not_open_it(self, temp_home):
        # The incident's shape: one peer reads (over), the rest stay unreadable. That
        # exit is `no-comparison`, as T3465 pins it (every_hold_names_a_vetoed_peer).
        h = _fleet(temp_home, 3, strategy="dynamic", threshold=90.0)
        outcome = _fail_over(h, {"1": None, "2": _usage7(10, 99, _R_LATER), "3": None})
        _held_in_place(h, outcome, "Account-2 (line 97%)", reason="no-comparison")

    @pytest.mark.parametrize(
        "strategy, line", [("dynamic", 97.0), ("best", 90.0), ("consume-first", 90.0)]
    )
    @pytest.mark.parametrize("window", ["five_hour", "seven_day"])
    @pytest.mark.parametrize("below, taken", [(0.0, False), (1.0, True)])
    def test_a_peer_exactly_at_the_line_is_refused_and_just_under_it_is_taken(
        self, temp_home, strategy, line, window, below, taken
    ):
        h = _fleet(temp_home, 2, strategy=strategy, threshold=90.0)
        peer = {
            "five_hour": {"pct": 10.0},
            "seven_day": {"pct": 10.0, "resets_at": _R_LATER},
        }
        peer[window] = {"pct": line - below, "resets_at": _R_LATER}
        outcome = _fail_over(h, {"1": None, "2": peer})
        if taken:
            assert outcome is TickOutcome.SWITCHED
            assert _switches(h) == [("failover", 2)]
        else:
            _held_in_place(h, outcome, f"Account-2 (line {line:g}%)")

    def test_a_weekly_line_does_not_lift_dynamics_bar_for_the_seven_day_window(
        self, temp_home
    ):
        # `dynamic` ignores the weekly line (README): 99% on the 7d window is 1
        # point of room, over its 97 bar, whatever `weekly_threshold` says.
        h = _fleet(
            temp_home, 2, strategy="dynamic", threshold=90.0, weekly_threshold=99.5
        )
        outcome = _fail_over(h, {"1": None, "2": _usage7(10, 99, _R_LATER)})
        _held_in_place(h, outcome, "Account-2 (line 97%)")

    def test_a_peer_over_the_line_on_the_pinned_model_is_refused(self, temp_home):
        h = _fleet(temp_home, 2, strategy="best", threshold=90.0, model="Fable")
        outcome = _fail_over(h, {"1": None, "2": _fable(1, 3, 99, _R_LATER)})
        _held_in_place(h, outcome, "Account-2 (line 90%)")

    @pytest.mark.parametrize("seven", [98.0, 50.0])
    def test_a_fleet_wide_model_blackout_does_not_land_a_blind_active_on_a_walled_peer(
        self, temp_home, seven
    ):
        # Fable 100 binds the whole fleet, which is what lets `dynamic` rank on 5h/7d
        # alone elsewhere. A failover has not read the active at all, so it never
        # takes a peer that is spent on the model the host runs.
        h = _fleet(temp_home, 2, strategy="dynamic", threshold=99.0, model="Fable")
        outcome = _fail_over(h, {"1": None, "2": _fable(10, seven, 100, _R_LATER)})
        _held_in_place(h, outcome, "Account-2 (line 97%)")

    @pytest.mark.parametrize("model", ["Fable", "all"])
    def test_a_header_probe_peer_over_the_bar_on_5h_7d_is_refused_beside_a_walled_one(
        self, temp_home, model
    ):
        # #2 is walled only on the model window, which binds the whole fleet, so
        # `dynamic` retries the ranking on 5h/7d (`models=()`). #3 is a header probe
        # with no window for the model (unknown there, always so for `all`), but on
        # the retry's axis it reads 99% 7d: over the 97 bar, never a failover landing.
        h = _fleet(temp_home, 3, strategy="dynamic", threshold=90.0, model=model)
        usage = {
            "1": None,
            "2": _fable(10, 50, 100, _R_LATER),
            "3": _header_usage(
                five_hour={"pct": 10.0},
                seven_day={"pct": 99.0, "resets_at": _R_LATER},
            ),
        }
        _held_in_place(h, _fail_over(h, usage), "Account-3 (line 97%)")

    def test_a_peer_with_room_on_the_pinned_model_is_taken(self, temp_home):
        h = _fleet(temp_home, 2, strategy="dynamic", threshold=99.0, model="Fable")
        outcome = _fail_over(h, {"1": None, "2": _fable(10, 50, 50, _R_LATER)})
        assert outcome is TickOutcome.SWITCHED
        assert _switches(h) == [("failover", 2)]

    @pytest.mark.parametrize("strategy", ["dynamic", "best", "consume-first"])
    def test_every_readable_peer_spent_is_still_reported_as_an_exhausted_fleet(
        self, temp_home, strategy
    ):
        # A spent peer is the ranking's "at its limit" refusal, not the veto: the
        # active is unread, so no recovery margin admits it, whatever resets it holds.
        h = _fleet(temp_home, 3, strategy=strategy, threshold=90.0)
        usage = {
            "1": None,
            "2": _usage7(100, 100, _R_SOON),
            "3": _usage7(100, 100, _R_LATER),
        }
        outcome = _fail_over(h, usage)
        assert outcome is TickOutcome.BLOCKED
        assert h.active_number() == 1
        assert not any(isinstance(e, SwitchEvent) for e in h.events)
        assert any(isinstance(e, AllExhaustedEvent) for e in h.events)

    def test_a_spent_peer_returning_sooner_is_never_a_failover_target(self, temp_home):
        # `dynamic`'s 5h/7d retry reads a header-probe active (no Fable window, so
        # unread under the model) on the unmodeled axis, where it is spent with a
        # reset: the margin that admits a spent peer returning sooner opens there.
        # A failover never has a provable return for an active it cannot read.
        h = _fleet(temp_home, 3, strategy="dynamic", threshold=90.0, model="Fable")
        usage = {
            "1": _header_usage(
                five_hour={"pct": 100.0, "resets_at": _R_LATEST},
                seven_day={"pct": 100.0, "resets_at": _R_LATEST},
            ),
            "2": _fable(10, 50, 100, _R_LATER),
            "3": _usage7(100, 100, _R_SOON),
        }
        _held_in_place(h, _fail_over(h, usage), "Account-2 (line 97%)")

    @pytest.mark.parametrize(
        "strategy, line", [("dynamic", 97.0), ("best", 90.0), ("consume-first", 90.0)]
    )
    def test_a_spent_peer_beside_an_over_line_one_with_room_is_still_the_hold(
        self, temp_home, strategy, line
    ):
        h = _fleet(temp_home, 3, strategy=strategy, threshold=90.0)
        usage = {
            "1": None,
            "2": _usage7(100, 100, _R_SOON),
            "3": _usage7(10, 99, _R_LATER),
        }
        _held_in_place(h, _fail_over(h, usage), f"Account-3 (line {line:g}%)")

    @pytest.mark.parametrize("strategy", ["dynamic", "best", "consume-first"])
    @pytest.mark.parametrize("under_freshens", [True, False])
    def test_with_one_peer_over_and_one_under_only_the_under_one_is_tried(
        self, temp_home, strategy, under_freshens
    ):
        h = _fleet(temp_home, 3, strategy=strategy, threshold=90.0)
        tried = []

        def freshen(num, email):
            tried.append(num)
            return "ok" if under_freshens else "transient"

        with patch.object(h.engine, "_freshen_target", side_effect=freshen):
            outcome = _fail_over(h, {
                "1": None,
                "2": _usage7(10, 99, _R_SOON),
                "3": _usage7(10, 60, _R_LATER),
            })
        assert tried == ["3"]
        assert (outcome is TickOutcome.SWITCHED) is under_freshens
        assert h.active_number() == (3 if under_freshens else 1)
