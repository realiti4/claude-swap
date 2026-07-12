"""Unit tests for the shared usage-poll cadence policy."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from claude_swap import poll_policy

NOW = 1_000_000.0
HALF = lambda: 0.5  # noqa: E731 — rng midpoint: jitter factor exactly 1.0


def _usage(pct: float, resets_at: str | None = None) -> dict:
    window: dict = {"pct": pct}
    if resets_at:
        window["resets_at"] = resets_at
    return {"five_hour": window, "seven_day": {"pct": 0.0}}


def _plan(**overrides):
    kwargs = dict(
        prev_interval_s=None,
        prev_usage=None,
        new_usage=_usage(10),
        is_active=False,
        threshold=90.0,
        models=(),
        recent_429=False,
        now=NOW,
        rng=HALF,
    )
    kwargs.update(overrides)
    return poll_policy.plan_after_fetch(**kwargs)


class TestIntervalAdaptation:
    def test_first_fetch_uses_defaults(self):
        _, active = _plan(is_active=True)
        _, candidate = _plan(is_active=False)
        assert active == poll_policy.MIN_INTERVAL_S
        assert candidate == poll_policy.CANDIDATE_DEFAULT_INTERVAL_S

    def test_unmoved_decays_toward_the_ceiling(self):
        _, interval = _plan(prev_interval_s=300.0, prev_usage=_usage(10))
        assert interval == 450.0
        _, capped = _plan(prev_interval_s=500.0, prev_usage=_usage(10))
        assert capped == poll_policy.CANDIDATE_MAX_INTERVAL_S
        _, active_capped = _plan(
            prev_interval_s=250.0, prev_usage=_usage(10), is_active=True
        )
        assert active_capped == poll_policy.ACTIVE_MAX_INTERVAL_S

    def test_movement_halves_floored_at_min(self):
        _, interval = _plan(
            prev_interval_s=600.0, prev_usage=_usage(10), new_usage=_usage(15)
        )
        assert interval == 300.0
        _, floored = _plan(
            prev_interval_s=200.0, prev_usage=_usage(10), new_usage=_usage(15)
        )
        assert floored == poll_policy.MIN_INTERVAL_S

    def test_sub_delta_wiggle_is_not_movement(self):
        _, interval = _plan(
            prev_interval_s=300.0,
            prev_usage=_usage(10),
            new_usage=_usage(10.5),  # below MOVEMENT_DELTA_PCT
        )
        assert interval == 450.0

    def test_unknown_pct_uses_the_default(self):
        _, interval = _plan(prev_interval_s=600.0, new_usage=None)
        assert interval == poll_policy.CANDIDATE_DEFAULT_INTERVAL_S


class TestUrgentMode:
    def _urgent_kwargs(self, **overrides):
        kwargs = dict(
            prev_interval_s=poll_policy.MIN_INTERVAL_S,
            prev_usage=_usage(78),
            new_usage=_usage(82),  # moving, inside the 75..90 band
            is_active=True,
            threshold=90.0,
        )
        kwargs.update(overrides)
        return kwargs

    def test_active_moving_in_band_goes_urgent(self):
        _, interval = _plan(**self._urgent_kwargs())
        assert interval == poll_policy.URGENT_INTERVAL_S

    def test_candidate_never_goes_urgent(self):
        _, interval = _plan(**self._urgent_kwargs(is_active=False))
        assert interval == poll_policy.MIN_INTERVAL_S  # plain movement halving

    def test_no_movement_no_urgency(self):
        _, interval = _plan(**self._urgent_kwargs(new_usage=_usage(78)))
        assert interval > poll_policy.URGENT_INTERVAL_S

    def test_below_the_band_no_urgency(self):
        _, interval = _plan(
            **self._urgent_kwargs(prev_usage=_usage(40), new_usage=_usage(50))
        )
        assert interval == poll_policy.MIN_INTERVAL_S

    def test_recent_429_suppresses_urgency(self):
        _, interval = _plan(**self._urgent_kwargs(recent_429=True))
        assert interval == poll_policy.POST_429_MIN_INTERVAL_S

    def test_urgent_then_unmoved_snaps_back_to_the_floor(self):
        # Once movement stops, the next interval is the normal floor — never
        # a sub-floor decay chain (60 → 90 → 135 …) off the urgent base.
        _, interval = _plan(
            **self._urgent_kwargs(
                prev_interval_s=poll_policy.URGENT_INTERVAL_S,
                new_usage=_usage(78),  # unmoved
            )
        )
        assert interval == poll_policy.MIN_INTERVAL_S


class TestPost429Floor:
    def test_recent_429_floors_the_cadence(self):
        _, interval = _plan(recent_429=True, prev_usage=_usage(10))
        assert interval >= poll_policy.POST_429_MIN_INTERVAL_S

    def test_slower_learned_cadence_survives_the_floor(self):
        # A learned interval already above the floor is grown (×1.5), never
        # dropped back to the floor.
        _, interval = _plan(
            recent_429=True, prev_interval_s=590.0, prev_usage=_usage(10)
        )
        assert interval == pytest.approx(590.0 * poll_policy.POST_429_BACKOFF_MULT)
        assert interval > poll_policy.POST_429_MIN_INTERVAL_S


class TestPost429Aimd:
    """AIMD backoff on a contended token: while 429s recur, each successful
    poll multiplicatively increases the interval toward a wider 429 ceiling, so
    independent machines sharing one token each retreat and their combined poll
    rate converges under the endpoint budget (no cross-machine coordination)."""

    def test_recent_429_multiplicatively_increases_from_prev(self):
        # A prior 360s interval, still seeing 429s, is pushed up (×1.5), not
        # held flat at the floor.
        _, interval = _plan(
            recent_429=True,
            prev_interval_s=poll_policy.POST_429_MIN_INTERVAL_S,  # 360
            prev_usage=_usage(10),
        )
        assert interval > poll_policy.POST_429_MIN_INTERVAL_S
        assert interval == pytest.approx(
            poll_policy.POST_429_MIN_INTERVAL_S * poll_policy.POST_429_BACKOFF_MULT
        )

    def test_recent_429_ceiling_exceeds_normal_candidate_max(self):
        # The 429 ceiling is wider than the normal candidate ceiling so a
        # contended token can back off far enough for several machines to fit.
        assert (
            poll_policy.POST_429_MAX_INTERVAL_S
            > poll_policy.CANDIDATE_MAX_INTERVAL_S
        )
        _, interval = _plan(
            recent_429=True,
            prev_interval_s=poll_policy.POST_429_MAX_INTERVAL_S,  # already at ceiling
            prev_usage=_usage(10),
        )
        assert interval == poll_policy.POST_429_MAX_INTERVAL_S

    def test_no_429_uses_normal_ceiling(self):
        # Without recent 429s the wider ceiling never applies (normal cadence).
        _, interval = _plan(
            recent_429=False, prev_interval_s=590.0, prev_usage=_usage(10)
        )
        assert interval == poll_policy.CANDIDATE_MAX_INTERVAL_S


class TestResetCapping:
    def _iso(self, ts: float) -> str:
        return (
            datetime.fromtimestamp(ts, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )

    def test_poll_never_scheduled_past_a_future_reset(self):
        reset_ts = NOW + 90.0
        next_poll, interval = _plan(new_usage=_usage(40, self._iso(reset_ts)))
        assert next_poll == pytest.approx(reset_ts + poll_policy.RESET_SLACK_S)
        assert interval == poll_policy.CANDIDATE_DEFAULT_INTERVAL_S

    def test_at_limit_skips_straight_to_its_reset(self):
        reset_ts = NOW + 7_200.0
        next_poll, interval = _plan(new_usage=_usage(100, self._iso(reset_ts)))
        assert next_poll == pytest.approx(reset_ts)
        assert interval == poll_policy.CANDIDATE_DEFAULT_INTERVAL_S


class TestJitter:
    def test_jitter_bounds(self, monkeypatch):
        monkeypatch.setattr("claude_swap.poll_policy.JITTER_FRAC", 0.1)
        early, _ = _plan(rng=lambda: 0.0)
        late, _ = _plan(rng=lambda: 1.0)
        interval = poll_policy.CANDIDATE_DEFAULT_INTERVAL_S
        assert early == pytest.approx(NOW + interval * 0.9)
        assert late == pytest.approx(NOW + interval * 1.1)


class TestBudgetInvariants:
    """Relationships the measured rate limit demands of the constants.

    Measured 2026-07-11 (probe3): a rolling ~60-minute window of ~28-30
    requests per token × UA-class — not a refilling bucket. Capacity returns
    only as old requests age out of the trailing hour, so a saturated window
    needs up to 60 minutes to recover. These invariants lean only on the
    robust parts of that measurement (a safe sustained cadence and an
    hour-scale recovery horizon), not on the exact server algorithm.
    """

    def test_sustained_floor_stays_under_the_hourly_cap(self):
        # 3600/180 = 20 requests/hour vs the measured ~28-30/hour window.
        assert poll_policy.MIN_INTERVAL_S >= 180.0
        assert poll_policy.SERVE_TTL_S >= 180.0

    def test_edge_backoff_probes_slower_than_capacity_frees(self):
        # While saturated, capacity returns at up to ~30/hour as the old
        # burst ages out; probing at ≥300 s (≤12/hour) lets recovery win.
        assert poll_policy.EDGE_BACKOFF_S >= 300.0

    def test_post_429_floor_covers_the_saturation_horizon(self):
        # A 429 means the trailing hour is full, and it takes up to 60
        # minutes for the spending burst to age out entirely.
        assert poll_policy.RECENT_429_WINDOW_S >= 3600.0
        assert poll_policy.POST_429_MIN_INTERVAL_S >= poll_policy.MIN_INTERVAL_S

    def test_urgent_episode_alone_fits_inside_the_window_cap(self):
        # Urgent mode is bounded by construction: each further urgent poll
        # needs ≥ MOVEMENT_DELTA_PCT of movement, so the slowest qualifying
        # burn crosses the escalation band in margin/delta polls — inside the
        # ~28-30 request rolling-hour window even before the post-429 floor
        # (which absorbs any overshoot) is considered.
        polls = poll_policy.ESCALATION_MARGIN_PCT / poll_policy.MOVEMENT_DELTA_PCT
        assert polls < 27
