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


class TestPost429Floor:
    def test_recent_429_floors_the_cadence(self):
        _, interval = _plan(recent_429=True, prev_usage=_usage(10))
        assert interval >= poll_policy.POST_429_MIN_INTERVAL_S

    def test_slower_learned_cadence_survives_the_floor(self):
        _, interval = _plan(
            recent_429=True, prev_interval_s=590.0, prev_usage=_usage(10)
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
    """Relationships the measured rate limit demands of the constants."""

    def test_sustained_floor_is_at_most_one_per_three_minutes(self):
        assert poll_policy.MIN_INTERVAL_S >= 180.0
        assert poll_policy.SERVE_TTL_S >= 180.0

    def test_edge_backoff_waits_at_least_a_refill(self):
        assert poll_policy.EDGE_BACKOFF_S >= 150.0  # ~1 request / 2.5 min

    def test_urgent_mode_fits_inside_the_burst_bucket(self):
        # Worst case: the escalation band crossed at URGENT_INTERVAL_S until a
        # switch — the band is 15 pts, heavy burn ~5 pts/min → ~15 urgent
        # polls, under the measured ~27-request burst capacity.
        band_minutes = poll_policy.ESCALATION_MARGIN_PCT / 5.0
        polls = band_minutes * 60.0 / poll_policy.URGENT_INTERVAL_S
        assert polls < 27
