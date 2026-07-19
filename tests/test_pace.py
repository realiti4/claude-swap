"""Unit tests for the weekly usage pace helper (issue #125)."""

from __future__ import annotations

from datetime import datetime, timezone

from claude_swap import pace

NOW = 1_700_000_000.0  # a realistic epoch; keeps NOW-minus-several-weeks positive
                        # (datetime.fromtimestamp rejects negative timestamps on Windows)
DAY = 86400.0
WEEK = pace.WEEKLY_PERIOD_S


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _window(pct: float, resets_at_ts: float | None) -> dict:
    window: dict = {"pct": pct}
    if resets_at_ts is not None:
        window["resets_at"] = _iso(resets_at_ts)
    return window


class TestComputePaceElapsed:
    def test_one_day_into_the_week(self):
        # Reset is 6 days away -> 1 day has elapsed since the window started.
        window = _window(20.0, NOW + 6 * DAY)
        result = pace.compute_pace(window, fetched_at=NOW)
        assert result is not None
        assert result.elapsed_s == DAY
        assert result.expected_pct == (DAY / WEEK) * 100.0

    def test_right_at_reset_boundary_is_suppressed(self):
        # resets_at exactly one period ahead of fetched_at -> elapsed == 0.
        window = _window(5.0, NOW + WEEK)
        assert pace.compute_pace(window, fetched_at=NOW) is None

    def test_stale_resets_at_multiple_cycles_in_the_past_still_resolves(self):
        # resets_at is 2 full weeks + 1 day behind fetched_at (never rolled
        # forward by a caller) -> current window still started 1 day ago.
        window = _window(20.0, NOW - 2 * WEEK - DAY)
        result = pace.compute_pace(window, fetched_at=NOW)
        assert result is not None
        assert result.elapsed_s == DAY

    def test_missing_fields_return_none(self):
        assert pace.compute_pace(None, fetched_at=NOW) is None
        assert pace.compute_pace({"pct": 10.0}, fetched_at=NOW) is None  # no resets_at
        assert pace.compute_pace({"resets_at": _iso(NOW + DAY)}, fetched_at=NOW) is None  # no pct
        assert pace.compute_pace(_window(10.0, NOW + DAY), fetched_at=None) is None
        assert pace.compute_pace({"pct": 10.0, "resets_at": "not-a-date"}, fetched_at=NOW) is None


class TestSuppressionWindow:
    def test_just_inside_suppression_window_is_none(self):
        elapsed = pace.SUPPRESS_AFTER_RESET_S - 1.0
        window = _window(50.0, NOW + WEEK - elapsed)
        assert pace.compute_pace(window, fetched_at=NOW) is None

    def test_just_outside_suppression_window_is_not_none(self):
        elapsed = pace.SUPPRESS_AFTER_RESET_S
        window = _window(50.0, NOW + WEEK - elapsed)
        result = pace.compute_pace(window, fetched_at=NOW)
        assert result is not None
        assert result.elapsed_s == elapsed


class TestAheadThreshold:
    def test_meaningfully_ahead_flags_true(self):
        # 1 day elapsed (~14.3% expected); 50% actual is far ahead.
        window = _window(50.0, NOW + 6 * DAY)
        result = pace.compute_pace(window, fetched_at=NOW)
        assert result is not None
        assert result.ahead is True

    def test_within_threshold_flags_false_but_still_returns(self):
        # 1 day elapsed (~14.3% expected); 20% actual is close, not "ahead".
        window = _window(20.0, NOW + 6 * DAY)
        result = pace.compute_pace(window, fetched_at=NOW)
        assert result is not None
        assert result.ahead is False

    def test_behind_pace_flags_false(self):
        window = _window(5.0, NOW + 6 * DAY)
        result = pace.compute_pace(window, fetched_at=NOW)
        assert result is not None
        assert result.ahead is False


class TestProjectedExhaustionTs:
    def test_linear_projection(self):
        # 1 day elapsed, 50% used -> burn rate implies 100% at day 2.
        window = _window(50.0, NOW + 6 * DAY)
        result = pace.compute_pace(window, fetched_at=NOW)
        assert result is not None
        eta = pace.projected_exhaustion_ts(result, fetched_at=NOW)
        assert eta is not None
        assert eta == NOW + DAY  # one more day at the same rate exhausts it

    def test_already_at_or_over_100_returns_fetched_at(self):
        window = _window(120.0, NOW + 6 * DAY)
        result = pace.compute_pace(window, fetched_at=NOW)
        assert result is not None
        assert pace.projected_exhaustion_ts(result, fetched_at=NOW) == NOW

    def test_zero_usage_has_no_projection(self):
        window = _window(0.0, NOW + 6 * DAY)
        result = pace.compute_pace(window, fetched_at=NOW)
        assert result is not None
        assert pace.projected_exhaustion_ts(result, fetched_at=NOW) is None


class TestWillLastToReset:
    def test_sustainable_rate_will_last(self):
        # 1 day elapsed, 10% used -> extrapolated to a full week is 70%, fine.
        window = _window(10.0, NOW + 6 * DAY)
        result = pace.compute_pace(window, fetched_at=NOW)
        assert result is not None
        assert pace.will_last_to_reset(result) is True

    def test_unsustainable_rate_will_not_last(self):
        # 1 day elapsed, 50% used -> extrapolated to a full week is 350%.
        window = _window(50.0, NOW + 6 * DAY)
        result = pace.compute_pace(window, fetched_at=NOW)
        assert result is not None
        assert pace.will_last_to_reset(result) is False

    def test_zero_usage_will_last(self):
        window = _window(0.0, NOW + 6 * DAY)
        result = pace.compute_pace(window, fetched_at=NOW)
        assert result is not None
        assert pace.will_last_to_reset(result) is True

    def test_comfortably_sustainable_rate_will_last(self):
        # 1 day elapsed, actual slightly under the ~14.3% expected -> stays
        # comfortably under 100% extrapolated across the full week.
        window = _window(12.0, NOW + 6 * DAY)
        result = pace.compute_pace(window, fetched_at=NOW)
        assert result is not None
        assert pace.will_last_to_reset(result) is True
