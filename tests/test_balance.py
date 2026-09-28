"""Unit tests for the pure balance policy (balance.py, issue #382)."""

from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest

from claude_swap import balance, pace
from claude_swap.balance import AccountScore, BalanceParams
from claude_swap.settings import AutoSwitchSettings

NOW = 1_700_000_000.0
HOUR = 3600.0
DAY = 86400.0
WEEK = pace.WEEKLY_PERIOD_S
PARAMS = BalanceParams()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _usage(
    pct5: float,
    pct7: float,
    *,
    reset7_in: float | None = 3 * DAY,
    reset5_in: float | None = 2 * HOUR,
    scoped: list[dict] | None = None,
) -> dict:
    """Normalized store-shape usage; resets are offsets from NOW in seconds."""
    five: dict = {"pct": pct5}
    if reset5_in is not None:
        five["resets_at"] = _iso(NOW + reset5_in)
    seven: dict = {"pct": pct7}
    if reset7_in is not None:
        seven["resets_at"] = _iso(NOW + reset7_in)
    usage: dict = {"five_hour": five, "seven_day": seven}
    if scoped is not None:
        usage["scoped"] = scoped
    return usage


def _score(usage, *, busy: int = 0, models=(), params: BalanceParams = PARAMS) -> AccountScore:
    return balance.score_account(
        "1", usage, now=NOW, models=models, busy_sessions=busy, params=params
    )


class TestWeeklyTarget:
    def test_lead_shortens_the_schedule(self):
        # Reset in 4 days -> 3 days elapsed; lead 24h -> 6-day schedule.
        assert balance.weekly_target_pct(NOW + 4 * DAY, NOW, 24.0) == pytest.approx(50.0)

    def test_zero_lead_matches_pace_expected_pct(self):
        reset = NOW + 4 * DAY
        result = pace.compute_pace({"pct": 0.0, "resets_at": _iso(reset)}, fetched_at=NOW)
        assert result is not None
        assert balance.weekly_target_pct(reset, NOW, 0.0) == pytest.approx(result.expected_pct)

    def test_clamped_at_100_inside_the_lead(self):
        # 12h before reset with a 24h lead: past the schedule's end.
        assert balance.weekly_target_pct(NOW + 12 * HOUR, NOW, 24.0) == 100.0

    def test_exactly_at_schedule_end_is_100(self):
        assert balance.weekly_target_pct(NOW + DAY, NOW, 24.0) == pytest.approx(100.0)

    def test_fresh_window_is_zero(self):
        assert balance.weekly_target_pct(NOW + WEEK, NOW, 24.0) == 0.0

    def test_stale_reset_rolls_into_the_new_window(self):
        # Reported reset 2h in the past: the new window started 2h ago.
        expected = 100.0 * (2 * HOUR) / (WEEK - 24 * HOUR)
        assert balance.weekly_target_pct(NOW - 2 * HOUR, NOW, 24.0) == pytest.approx(expected)

    def test_monotonic_in_elapsed(self):
        reset = NOW + WEEK - 1.0
        targets = [
            balance.weekly_target_pct(reset, NOW + k * 6 * HOUR, 24.0) for k in range(28)
        ]
        assert targets == sorted(targets)

    def test_lead_beyond_the_period_is_already_late(self):
        assert balance.weekly_target_pct(NOW + 3 * DAY, NOW, 7 * 24.0) == 100.0


class TestScoreAccount:
    def test_slack_positive_when_behind_schedule(self):
        # target 50 (reset in 4d, lead 24h), used 20 -> slack +30.
        s = _score(_usage(10, 20, reset7_in=4 * DAY))
        assert s.slack == pytest.approx(30.0)
        assert s.eligible and s.reason == "ok"

    def test_slack_negative_when_ahead_of_schedule(self):
        s = _score(_usage(10, 70, reset7_in=4 * DAY))
        assert s.slack == pytest.approx(-20.0)

    def test_score_is_slack_minus_weighted_projection(self):
        s = _score(_usage(10, 20, reset7_in=4 * DAY), busy=2)
        assert s.projected_5h == pytest.approx(10 + 15.0 * 2)
        assert s.score == pytest.approx(30.0 - 0.5 * 40.0)

    def test_headroom_and_recovery_ts_follow_the_binding_window(self):
        s = _score(_usage(30, 60, reset7_in=4 * DAY, reset5_in=HOUR))
        assert s.headroom == pytest.approx(40.0)
        assert s.recovery_ts == pytest.approx(NOW + 4 * DAY)  # 7d binds

    @pytest.mark.parametrize(
        "usage, models",
        [
            (None, ()),
            ("token-expired", ()),
            ({}, ()),
            ({"five_hour": {"pct": 10.0}}, ()),
            ({"seven_day": {"pct": float("nan")}}, ()),
            ({"seven_day": {"pct": float("inf")}}, ()),
            ({"five_hour": {"pct": float("nan")}, "seven_day": {"pct": 20.0}}, ()),
            ({"five_hour": {"pct": float("inf")}, "seven_day": {"pct": 20.0}}, ()),
            (
                {
                    "seven_day": {"pct": 20.0},
                    "scoped": [{"name": "Fable", "pct": float("nan")}],
                },
                ("fable",),
            ),
        ],
        ids=[
            "none",
            "sentinel",
            "empty-dict",
            "missing-7d",
            "nan-pct",
            "inf-pct",
            "nan-5h",
            "inf-5h",
            "nan-scoped",
        ],
    )
    def test_unknown_usage(self, usage, models):
        s = _score(usage, models=models)
        assert s == AccountScore(
            account="1",
            eligible=False,
            score=None,
            slack=None,
            projected_5h=None,
            headroom=None,
            recovery_ts=math.inf,
            reason="unknown-usage",
        )

    def test_unstarted_weekly_window_has_zero_target(self):
        s = _score(_usage(0, 0, reset7_in=None, reset5_in=None))
        assert s.slack == 0.0
        assert s.eligible

    def test_unstarted_weekly_window_with_nonzero_pct(self):
        # Unstarted week (missing reset) with nonzero pct: slack = -pct.
        s = _score(_usage(0, 25, reset7_in=None, reset5_in=None))
        assert s.slack == pytest.approx(-25.0)
        assert s.eligible

    def test_missing_five_hour_window_counts_as_empty(self):
        s = _score({"seven_day": {"pct": 20.0, "resets_at": _iso(NOW + 4 * DAY)}})
        assert s.projected_5h == 0.0
        assert s.eligible

    def test_unparseable_resets_at_treated_as_unstarted(self):
        # Unparseable resets_at "garbage" → unstarted window → target = 0.
        s = _score({"seven_day": {"pct": 30.0, "resets_at": "garbage"}})
        assert s.slack == pytest.approx(-30.0)
        assert s.eligible

    def test_negative_busy_sessions_clamped_to_zero(self):
        # busy=-3 should behave like busy=0.
        s_neg = _score(_usage(60, 10), busy=-3)
        s_zero = _score(_usage(60, 10), busy=0)
        assert s_neg.projected_5h == pytest.approx(s_zero.projected_5h)
        assert s_neg.score == pytest.approx(s_zero.score)

    def test_at_limit(self):
        s = _score(_usage(100, 40))
        assert not s.eligible and s.reason == "at-limit"
        assert s.headroom == 0.0

    def test_at_limit_beats_weekly_threshold(self):
        # 5h at 100 (at-limit), 7d at 100 (over threshold): should report at-limit.
        s = _score(_usage(100, 100))
        assert not s.eligible and s.reason == "at-limit"
        assert s.score is not None
        assert s.slack is not None

    def test_five_hour_ceiling_gate(self):
        assert _score(_usage(84.9, 10)).eligible
        s = _score(_usage(85, 10))
        assert not s.eligible and s.reason == "five-hour-ceiling"
        assert s.score is not None
        assert s.slack is not None

    def test_busy_sessions_push_an_account_over_the_ceiling(self):
        # 60 + 15 * 1 = 75 < 85 is fine; 60 + 15 * 2 = 90 is not.
        assert _score(_usage(60, 10), busy=1).eligible
        s = _score(_usage(60, 10), busy=2)
        assert not s.eligible and s.reason == "five-hour-ceiling"

    def test_weekly_threshold_gate(self):
        assert _score(_usage(10, 89.9)).eligible
        s = _score(_usage(10, 90))
        assert not s.eligible and s.reason == "weekly-threshold"
        assert s.score is not None
        assert s.slack is not None

    def test_selected_model_window_gates_like_the_weekly_one(self):
        usage = _usage(10, 20, scoped=[{"name": "Fable", "pct": 95.0}])
        assert _score(usage).eligible  # model not selected: ignored
        s = _score(usage, models=("fable",))
        assert not s.eligible and s.reason == "weekly-threshold"

    def test_selected_model_at_limit_beats_weekly_threshold(self):
        # Model window at 100 (over threshold) but 5h at 100 (at-limit):
        # should report at-limit.
        usage = _usage(100, 20, scoped=[{"name": "Claude", "pct": 100.0}])
        s = _score(usage, models=("claude",))
        assert not s.eligible and s.reason == "at-limit"

    def test_weekly_gate_reported_before_the_five_hour_one(self):
        s = _score(_usage(88, 92))
        assert s.reason == "weekly-threshold"

    def test_score_decreases_as_weekly_usage_grows(self):
        scores = [_score(_usage(10, pct7, reset7_in=4 * DAY)).score for pct7 in range(0, 90, 5)]
        assert scores == sorted(scores, reverse=True)

    def test_params_from_settings(self):
        settings = AutoSwitchSettings(
            threshold=80.0,
            balance_lead_hours=12.0,
            balance_five_hour_ceiling=70.0,
            balance_five_hour_weight=1.0,
            balance_load_per_session=20.0,
        )
        assert balance.params_from_settings(settings) == BalanceParams(
            lead_hours=12.0,
            five_hour_ceiling=70.0,
            five_hour_weight=1.0,
            load_per_session=20.0,
            threshold=80.0,
        )

    def test_default_params_match_default_settings(self):
        assert balance.params_from_settings(AutoSwitchSettings()) == BalanceParams()


def _rank(usage_by_account: dict, busy: dict | None = None, models=()) -> list[str]:
    return [
        s.account
        for s in balance.rank_accounts(
            usage_by_account,
            now=NOW,
            models=models,
            busy_sessions=busy or {},
            params=PARAMS,
        )
    ]


class TestRankAccounts:
    def test_behind_schedule_beats_most_headroom(self):
        # "2": reset in 1d -> target 100, used 60 -> slack 40, score 35.
        # "3": reset in 6d -> target 16.7, used 5 -> slack 11.7, score 11.7.
        # "3" has far more headroom (95 vs 40) but is ahead of where "2" is.
        assert _rank({
            "2": _usage(10, 60, reset7_in=DAY),
            "3": _usage(0, 5, reset7_in=6 * DAY),
        }) == ["2", "3"]

    def test_score_ties_keep_input_order(self):
        same = _usage(10, 20)
        assert _rank({"1": same, "2": same, "3": same}) == ["1", "2", "3"]
        assert _rank({"3": same, "1": same, "2": same}) == ["3", "1", "2"]

    def test_busy_sessions_lower_the_score(self):
        same = _usage(10, 20)
        assert _rank({"1": same, "2": same}, busy={"1": 1}) == ["2", "1"]

    def test_ineligible_omitted_when_any_is_eligible(self):
        assert _rank({
            "1": _usage(90, 10),           # five-hour-ceiling
            "2": _usage(10, 95),           # weekly-threshold
            "3": _usage(10, 50),           # eligible
        }) == ["3"]

    def test_unknown_and_at_limit_are_always_omitted(self):
        assert _rank({
            "1": None,
            "2": "token-expired",
            "3": _usage(100, 10),
            "4": _usage(10, 100),
        }) == []

    def test_fallback_orders_by_soonest_recovery_when_none_eligible(self):
        ranked = balance.rank_accounts(
            {
                # 5h binds for all three (90+ > 7d); recoveries 3h / 1h / 2h.
                "1": _usage(95, 10, reset5_in=3 * HOUR),
                "2": _usage(97, 10, reset5_in=HOUR),
                "3": _usage(90, 10, reset5_in=2 * HOUR),
                "4": _usage(100, 10, reset5_in=10.0),   # at-limit: omitted
                "5": None,                              # unknown: omitted
            },
            now=NOW,
            models=(),
            busy_sessions={},
            params=PARAMS,
        )
        assert [s.account for s in ranked] == ["2", "3", "1"]
        assert all(not s.eligible for s in ranked)
        assert [s.reason for s in ranked] == ["five-hour-ceiling"] * 3

    def test_fallback_never_refuses_while_headroom_remains(self):
        # One account, over every gate but not at its limit: still offered.
        assert _rank({"1": _usage(99, 99)}) == ["1"]

    def test_fallback_unknown_recovery_sorts_last(self):
        assert _rank({
            "1": _usage(95, 10, reset5_in=None),
            "2": _usage(95, 10, reset5_in=4 * HOUR),
        }) == ["2", "1"]

    def test_eligible_list_carries_scores(self):
        ranked = balance.rank_accounts(
            {"1": _usage(10, 20, reset7_in=4 * DAY)},
            now=NOW,
            models=(),
            busy_sessions={},
            params=PARAMS,
        )
        assert len(ranked) == 1
        assert ranked[0].eligible
        assert ranked[0].score == pytest.approx(30.0 - 0.5 * 10.0)
