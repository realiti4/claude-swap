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
