"""Tests for the usage-history forecast maths (history_forecast.py).

The first group pins the core functions (spreading a rise over the time it
happened in, and stepping the accounts' weeks forward under each strategy).
The rest cover the analysis built on them: which accounts count, room in the
past, and when a pace is (not) available.
"""

from __future__ import annotations

import math

import pytest

from claude_swap.history import GAP_S, Account, Current, History, Reading
from claude_swap.history_forecast import (
    PACES,
    SimAccount,
    analyse,
    extra_accounts,
    path_at,
    rises_of,
    room_at,
    room_series,
    simulate_room,
    spread_used,
)

H = 3600.0
WEEK = 7 * 86400.0
INF = math.inf


def per_hour(points: float) -> float:
    """Points of a weekly limit an hour -> accounts' worth a second."""
    return points / 100 / H


def near(a: float | None, b: float, eps: float = 1e-9) -> None:
    assert a is not None and abs(a - b) <= eps, f"{a} != {b}"


def acct(n: int, u: float, r: float = INF, priority: int = 0) -> SimAccount:
    return SimAccount(n, u, r, priority)


class TestSpreadUsed:
    def test_a_rise_is_spread_over_the_time_since_the_previous_check(self):
        rises = [(1000.0, 1, 10.0, 0.0)]  # 10 points between checks at 0 and 1000
        near(spread_used(rises, 500, 1000), 0.05)
        near(spread_used(rises, 0, 1000), 0.10)
        near(spread_used(rises, 1000, 2000), 0)
        near(spread_used(rises, -500, 250), 0.025)

    def test_a_late_reading_after_a_gap_does_not_pile_the_gap_into_a_short_window(self):
        # cswap couldn't check the active account for 3 hours, then saw +30 points
        now = 100 * H
        rises = [(now, 1, 30.0, now - 3 * H)]
        near(spread_used(rises, now - 1800, now), 0.05)  # 30 x 0.5 h / 3 h

    def test_rows_without_a_previous_check_count_at_the_reading(self):
        rises = [(1000.0, 1, 10.0, 1000.0)]
        near(spread_used(rises, 999, 1000), 0.10)
        near(spread_used(rises, 1000, 1001), 0)


class TestSimulateRoom:
    def test_with_no_use_the_room_only_steps_up_at_resets(self):
        r = simulate_room([acct(1, 40, 2 * H), acct(2, 70)], 1, 0, 0, 10 * H)
        assert r.out is None
        near(path_at(r.path, H), 0.9)
        near(path_at(r.path, 2 * H), 1.3)  # #1 back to 0%: 1.0 + 0.3
        near(path_at(r.path, 10 * H), 1.3)
        near(r.low[1], 0.9)

    def test_use_stays_on_the_active_account_then_moves_to_most_room_then_runs_out(self):
        accts = [acct(1, 50), acct(2, 80), acct(3, 90)]
        r = simulate_room(accts, 1, per_hour(50), 0, 10 * H)
        near(path_at(r.path, H), 0.3)  # #1 full after an hour
        near(path_at(r.path, 1.4 * H), 0.1)  # then #2's 20 points in 0.4 h
        near(r.out, 1.6 * H, 1e-6)  # then #3's 10 points: everything full
        near(path_at(r.path, 5 * H), 0)
        assert [a.u for a in accts] == [50, 80, 90], "the caller's accounts are left alone"

    def test_a_reset_while_out_of_room_brings_room_back_which_is_used_again(self):
        r = simulate_room([acct(1, 100, 2 * H), acct(2, 100)], 1, per_hour(25), 0, 8 * H)
        near(r.out, 0)
        near(path_at(r.path, 2 * H - 1), 0)
        near(path_at(r.path, 2 * H), 1)  # the value after the step, at the reset itself
        near(path_at(r.path, 4 * H), 0.5)
        near(path_at(r.path, 6 * H), 0)

    def test_resets_at_the_same_moment_step_up_together_and_come_round_a_week_later(self):
        r = simulate_room([acct(1, 60, H), acct(2, 30, H)], 1, 0, 0, WEEK + 2 * H)
        at = [p for p in r.path if p[0] == H]
        assert len(at) == 2  # before and after one combined step
        near(at[1][1], 2)
        near(path_at(r.path, WEEK + 2 * H), 2)

    def test_consume_first_uses_the_week_that_resets_soonest(self):
        # #1 active, 20% used, back in 100 h; #2 60%, back in 2 h; #3 50%,
        # back in 50 h. 10 points an hour.
        def accts():
            return [acct(1, 20, 100 * H), acct(2, 60, 2 * H), acct(3, 50, 50 * H)]

        cf = simulate_room(accts(), 1, per_hour(10), 0, 10 * H, "consume-first")
        best = simulate_room(accts(), 1, per_hour(10), 0, 10 * H, "best")
        near(path_at(cf.path, 2 * H), 2.3)  # #2's last 20 points used just before its reset gave all 100 back
        near(path_at(best.path, 2 * H), 2.1)  # best stayed on #1, so #2 came back with only 40 more
        near(path_at(cf.path, 3 * H), 2.2)  # then the next soonest, #3, not the week that just reset

    def test_best_is_the_default_strategy(self):
        def accts():
            return [acct(1, 20, 100 * H), acct(2, 60, 2 * H)]

        assert (
            simulate_room(accts(), 1, per_hour(10), 0, 5 * H).path
            == simulate_room(accts(), 1, per_hour(10), 0, 5 * H, "best").path
        )

    def test_an_active_account_that_is_not_counted_hands_over_to_most_room(self):
        r = simulate_room([acct(2, 90), acct(3, 20)], 9, per_hour(10), 0, H)
        near(path_at(r.path, H), 0.8)  # the use went to #3, with the most room

    def test_readings_outside_0_100_are_clamped_and_the_loop_always_ends(self):
        r = simulate_room([acct(1, 130, 1), acct(2, -20, 2)], 1, 1e6, 0, 30 * WEEK)
        assert len(r.path) <= 20002, "bounded by its guard"
        near(r.path[0][1], 1)  # 0% + 100% used
        assert all(0 <= p[1] <= 2 for p in r.path)

    def test_the_end_of_the_forecast_is_reached(self):
        r = simulate_room([acct(1, 10, 5 * H)], 1, per_hour(1), 0, WEEK)
        assert r.path[-1][0] == WEEK

    # -- beyond the original suite --------------------------------------------

    def test_priority_consume_first_drains_the_highest_tier_first(self):
        # #1 low tier, resets soonest; #2 high tier, resets later. 10 points an hour.
        def accts():
            return [acct(1, 50, 3 * H, priority=0), acct(2, 50, 50 * H, priority=100)]

        prio = simulate_room(accts(), 1, per_hour(10), 0, 4 * H, "priority-consume-first")
        cf = simulate_room(accts(), 1, per_hour(10), 0, 4 * H, "consume-first")
        # Both use 20 points in 2 hours; only which account differs.
        near(path_at(prio.path, 2 * H), 0.8)
        near(path_at(cf.path, 2 * H), 0.8)
        # At #1's reset, consume-first had spent #1 (all of it comes back)...
        near(path_at(cf.path, 3 * H), 1.5)
        # ...while priority spent #2 (now 80%), so #1's reset frees only its own 50.
        near(path_at(prio.path, 3 * H), 1.2)

    def test_no_accounts_is_out_of_room_at_once(self):
        r = simulate_room([], None, per_hour(10), 0, H)
        assert r.out == 0
        assert r.path[0] == (0, 0)


def test_extra_accounts_to_keep_up_is_pace_times_7_less_the_accounts_there():
    assert extra_accounts(15 / 7, 15) == 0  # exactly the sustainable pace
    assert extra_accounts(1.0, 15) == 0  # slower than it
    assert extra_accounts(2.5, 15) == 3  # needs 17.5 accounts -> 3 more
    assert extra_accounts(16.01 / 7, 15) == 2  # a hair over 16 still needs a 17th
    assert extra_accounts(16 / 7, 15) == 1  # float noise doesn't add one
    assert extra_accounts(0.5, 0) == 4  # no counted accounts: 3.5 -> 4


# -- the analysis ------------------------------------------------------------------

NOW = 1_800_000_000.0


def make_history(**kw) -> History:
    base = dict(
        now=NOW,
        accounts=[Account(1), Account(2)],
        readings={},
        current={},
        active=1,
        recording_since=NOW - 10 * 86400,
    )
    base.update(kw)
    return History(**base)


class TestAnalysis:
    def test_room_now_counts_fresh_readings_of_managed_enabled_accounts(self):
        h = make_history(
            accounts=[Account(1), Account(2), Account(3, disabled=True), Account(4, removed=True), Account(5),
                      Account(6, weekly=False)],
            current={
                1: Current(t=NOW - 60, d7=40, d7_reset=NOW + 86400),
                2: Current(t=NOW - 60, d7=10, d7_reset=NOW - 5),  # reset since: a fresh week
                3: Current(t=NOW - 60, d7=0),  # disabled
                4: Current(t=NOW - 60, d7=0),  # removed
                5: Current(t=NOW - GAP_S - 1, d7=0),  # too old
            },
        )
        a = analyse(h)
        assert a.fleet == [1, 2, 5]
        near(a.room_now.room, 1.6)
        assert a.room_now.counted == 2
        near(a.sustainable_per_day, 2 / 7)
        by = {r.number: r for r in a.accounts_now}
        assert by[2].stale_reset and by[2].d7 == 0 and by[2].counted
        assert not by[3].counted and by[3].disabled
        assert 4 not in by
        assert not by[6].weekly and not by[6].counted  # an API key: no weekly room

    def test_a_fresh_reading_whose_week_has_reset_counts_as_0_percent_now_too(self):
        """Room now must continue the past line, which counts it from the reset."""
        h = make_history(
            readings={
                1: [Reading(NOW - 300, None, 50, None, d7_reset=NOW + 86400)],
                2: [Reading(NOW - 300, None, 90, None, d7_reset=NOW - 60)],
            },
            current={
                1: Current(t=NOW - 300, d7=50, d7_reset=NOW + 86400),
                2: Current(t=NOW - 300, d7=90, d7_reset=NOW - 60),
            },
        )
        a = analyse(h)
        near(room_at(h, [1, 2], NOW - 1), 1.5)
        near(a.room_now.room, 1.5)
        assert a.room_now.counted == 2
        by = {r.number: r for r in a.accounts_now}
        assert by[2].d7 == 0 and by[2].d7_reset == NOW - 60 + WEEK and by[2].counted
        assert [e.number for e in a.resets] == [1]  # #2 frees nothing at its next reset

    def test_resets_ahead_and_the_refill_line(self):
        h = make_history(current={
            1: Current(t=NOW, d7=40, d7_reset=NOW + 2 * H),
            2: Current(t=NOW, d7=70, d7_reset=NOW + 30 * H),
        })
        a = analyse(h)
        assert [(e.number, round(e.frees, 2)) for e in a.resets] == [(1, 0.4), (2, 0.7)]
        near(a.refill_at(NOW + H), 0.9)
        near(a.refill_at(NOW + 3 * H), 1.3)
        near(a.refill_at(NOW + 31 * H), 2.0)

    def test_room_in_the_past_needs_half_the_accounts_reporting(self):
        h = make_history(
            accounts=[Account(1), Account(2), Account(3)],
            readings={
                1: [Reading(NOW - 10 * H, None, 50, None)],
                2: [Reading(NOW - 10 * H, None, 20, None)],
            },
        )
        near(room_at(h, [1, 2, 3], NOW - 9 * H), 1.3)
        assert room_at(h, [1, 2, 3], NOW - 11 * H) is None  # nothing yet
        # a reading older than the gap no longer holds
        assert room_at(h, [1, 2, 3], NOW - 10 * H + GAP_S + 1) is None
        series = room_series(h, [1, 2, 3], NOW - 11 * H, NOW - 9 * H, 3)
        assert [v is None for _, v in series] == [True, False, False]

    def test_a_reading_from_before_its_reset_counts_as_a_fresh_week_from_the_reset(self):
        h = make_history(readings={
            1: [Reading(NOW - 5 * H, None, 90, None, d7_reset=NOW - 2 * H)],
            2: [Reading(NOW - 5 * H, None, 10, None)],
        })
        near(room_at(h, [1, 2], NOW - 3 * H), 1.0)
        near(room_at(h, [1, 2], NOW - H), 1.9)  # #1 stepped up at its reset, no dip
        assert [round(v, 2) for _, v in room_series(h, [1, 2], NOW - 3 * H, NOW - H, 3)] == [1.0, 1.9, 1.9]

    def test_rises_are_dated_from_the_previous_check(self):
        h = make_history(readings={1: [
            Reading(NOW - 5 * H, None, 10, None),
            Reading(NOW - 2 * H, None, 30, None, prev=NOW - 4 * H),
            Reading(NOW - H, None, 25, None, prev=NOW - 2 * H),  # a reset: no rise
            Reading(NOW, None, 35, None),  # no prev: counts at the reading
        ]})
        assert rises_of(h, [1]) == [
            (NOW - 2 * H, 1, 20, NOW - 4 * H),
            (NOW, 1, 10, NOW),
        ]

    def test_paces_use_the_rises_up_to_the_active_accounts_last_check(self):
        rows = [
            Reading(NOW - 25 * H, None, 0, None),
            Reading(NOW - 12 * H, None, 24, None, prev=NOW - 13 * H),
            Reading(NOW - 10 * 60, None, 30, None, prev=NOW - 40 * 60),
        ]
        h = make_history(
            readings={1: rows},
            current={
                1: Current(t=NOW - 10 * 60, d7=30, d7_reset=NOW + 3 * 86400),
                2: Current(t=NOW - 60, d7=0, d7_reset=NOW + 86400),
            },
        )
        a = analyse(h)
        by = {f.pace.key: f for f in a.forecasts}
        assert all(f.ok for f in a.forecasts)
        # The stretches end at the active account's last check, 10 minutes ago:
        # the 30-minute one holds all of the last rise, spread over 30 minutes.
        near(by["30m"].used, 0.06)
        near(by["24h"].used, 0.30)
        near(by["24h"].per_day, 0.30)
        assert by["30m"].sim is not None and by["30m"].sim.path[0] == (NOW, 1.7)

    @pytest.mark.parametrize(
        "kw, reason",
        [
            (dict(current={}), "no-room-now"),
            (dict(active=None, current={2: Current(t=NOW, d7=0)}), "no-active"),
            (dict(current={1: Current(t=NOW - 3 * H, d7=0)}), "stale-active"),
            (dict(recording_since=NOW - 2 * H, current={1: Current(t=NOW, d7=0)}), "warming-up"),
        ],
    )
    def test_a_pace_says_why_it_is_unavailable(self, kw, reason):
        a = analyse(make_history(**kw))
        f = next(f for f in a.forecasts if f.pace.key == "24h")
        assert not f.ok and f.reason == reason
        if reason == "warming-up":
            near(f.needs, 22 * H)
            assert next(x for x in a.forecasts if x.pace.key == "30m").ok

    def test_stale_active_allows_less_lag_for_the_short_pace(self):
        h = make_history(current={1: Current(t=NOW - 20 * 60, d7=0)})
        by = {f.pace.key: f for f in analyse(h).forecasts}
        assert by["30m"].reason == "stale-active"  # more than 15 minutes old
        assert by["6h"].ok and by["24h"].ok

    def test_paces_cover_30_minutes_6_hours_and_24_hours(self):
        assert [(p.key, p.span) for p in PACES] == [("30m", 1800), ("6h", 6 * H), ("24h", 24 * H)]
