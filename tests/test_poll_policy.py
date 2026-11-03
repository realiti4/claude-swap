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


def _usage7(pct5: float, pct7: float) -> dict:
    return {"five_hour": {"pct": pct5}, "seven_day": {"pct": pct7}}


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

    def _converge_trajectory(self, recent_429: bool, rounds: int = 12):
        # Deterministic evolution of the interval under a sustained contended
        # token: each successful poll re-plans with the same recent_429 flag and
        # unmoved usage (movement decay would only shorten it — the worst case
        # for convergence is an unmoving account that just keeps 429ing). rng at
        # the midpoint so jitter is exactly 1.0 and the trajectory is exact.
        prev = None
        traj = []
        for _ in range(rounds):
            _, interval = _plan(
                recent_429=recent_429,
                prev_interval_s=prev,
                prev_usage=_usage(10),
                new_usage=_usage(10),
            )
            traj.append(interval)
            prev = interval
        return traj

    def test_sustained_429_grows_the_interval_to_the_wide_ceiling(self):
        # THE convergence property: while 429s recur, the interval must keep
        # growing (×MULT) until it reaches POST_429_MAX_INTERVAL_S. This is what
        # lets N machines sharing one token each back off far enough that their
        # combined rate drops under the budget — the deadlock cannot clear
        # without it.
        traj = self._converge_trajectory(recent_429=True)
        # strictly increasing until it saturates at the wide ceiling
        assert traj[-1] == poll_policy.POST_429_MAX_INTERVAL_S
        assert traj == sorted(traj)  # monotonic non-decreasing
        # actually reaches the ceiling within the simulated rounds
        assert max(traj) == poll_policy.POST_429_MAX_INTERVAL_S
        # each pre-ceiling step grew by the multiplier (AIMD multiplicative incr)
        for a, b in zip(traj, traj[1:]):
            if b < poll_policy.POST_429_MAX_INTERVAL_S:
                assert b == pytest.approx(a * poll_policy.POST_429_BACKOFF_MULT)

    def test_without_recency_the_interval_is_capped_at_the_narrow_ceiling(self):
        # The failure mode the recency bug caused: if recent_429 is False on the
        # post-block success (the pre-fix behavior after an honored hour-scale
        # Retry-After), the interval can never exceed CANDIDATE_MAX_INTERVAL_S.
        # N machines then jam at 600s each and their combined rate can sit above
        # the budget forever — a permanent deadlock the AIMD is meant to break.
        traj = self._converge_trajectory(recent_429=False)
        assert max(traj) == poll_policy.CANDIDATE_MAX_INTERVAL_S
        assert (
            poll_policy.CANDIDATE_MAX_INTERVAL_S
            < poll_policy.POST_429_MAX_INTERVAL_S
        )


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

    def test_at_limit_keeps_bounded_polling_before_distant_reset(self):
        reset_ts = NOW + 7_200.0
        next_poll, interval = _plan(new_usage=_usage(100, self._iso(reset_ts)))
        assert interval == poll_policy.EXHAUSTED_INTERVAL_S
        assert next_poll == pytest.approx(NOW + interval)
        assert next_poll < reset_ts

    def test_at_limit_poll_is_pulled_to_an_imminent_reset(self):
        reset_ts = NOW + 90.0
        next_poll, interval = _plan(new_usage=_usage(100, self._iso(reset_ts)))
        assert interval == poll_policy.EXHAUSTED_INTERVAL_S
        assert next_poll == pytest.approx(reset_ts + poll_policy.RESET_SLACK_S)

    @pytest.mark.parametrize("reset_ts", [NOW - 90.0, NOW])
    def test_at_limit_ignores_non_future_reset(self, reset_ts):
        next_poll, interval = _plan(new_usage=_usage(100, self._iso(reset_ts)))
        assert interval == poll_policy.EXHAUSTED_INTERVAL_S
        assert next_poll == pytest.approx(NOW + interval)

    def test_active_at_limit_uses_same_bounded_recovery_probe(self):
        reset_ts = NOW + 7_200.0
        next_poll, interval = _plan(
            new_usage=_usage(100, self._iso(reset_ts)), is_active=True
        )
        assert interval == poll_policy.EXHAUSTED_INTERVAL_S
        assert next_poll == pytest.approx(NOW + interval)


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


class TestPerWindowUrgency:
    """Urgent mode is judged per window, rising only, against each window's
    own line. The halving/decay rule stays on the raw max, so the default
    cadence is unchanged outside the band."""

    def _active(self, prev, new, **overrides):
        kwargs = dict(
            prev_interval_s=poll_policy.MIN_INTERVAL_S,
            prev_usage=prev,
            new_usage=new,
            is_active=True,
            threshold=90.0,
        )
        kwargs.update(overrides)
        return _plan(**kwargs)[1]

    def test_the_existing_urgent_plan_is_unchanged_with_weekly_unset(self):
        assert self._active(_usage(78), _usage(82)) == poll_policy.URGENT_INTERVAL_S

    def test_a_non_max_window_rising_in_its_band_goes_urgent(self):
        # 5h 76 -> 78 under a flat 7d 85 max: the max rule read "no movement"
        # and decayed while the 5h closed on the line.
        assert (
            self._active(_usage7(76, 85), _usage7(78, 85.2))
            == poll_policy.URGENT_INTERVAL_S
        )

    def test_s10_the_5h_rising_under_a_flat_weekly_binding(self):
        # 85/97: the raw max (7d 94) is flat, so no halving — but the 5h rose
        # 4 points into its own band, which is the burn that matters.
        assert (
            self._active(
                _usage7(80, 94), _usage7(84, 94), threshold=85.0, weekly_threshold=97.0
            )
            == poll_policy.URGENT_INTERVAL_S
        )
        # A candidate never goes urgent; its unmoved max decays as before.
        _, interval = _plan(
            prev_interval_s=300.0,
            prev_usage=_usage7(80, 94),
            new_usage=_usage7(84, 94),
            threshold=85.0,
            weekly_threshold=97.0,
        )
        assert interval == 450.0

    def test_s5_the_weekly_window_burning_toward_its_own_line(self):
        assert (
            self._active(
                _usage7(20, 90.5), _usage7(20, 96.9), threshold=85.0, weekly_threshold=97.0
            )
            == poll_policy.URGENT_INTERVAL_S
        )
        _, halved = _plan(
            prev_interval_s=600.0,
            prev_usage=_usage7(20, 90.5),
            new_usage=_usage7(20, 96.9),
            threshold=85.0,
            weekly_threshold=97.0,
        )
        assert halved == 300.0

    def test_a_rise_outside_its_own_band_is_not_urgent(self):
        # 5h 60 -> 62 is 23 points from its line; the flat 7d max decays.
        assert (
            self._active(
                _usage7(60, 94), _usage7(62, 94), threshold=85.0, weekly_threshold=97.0
            )
            == 270.0
        )

    def test_a_window_dropping_into_the_band_is_not_urgent(self):
        # A drop is a rollover (or another machine's), not a burn: the max
        # moved, so the interval halves, but no 60s polls on an account that
        # just gained room.
        assert self._active(_usage(90), _usage(80)) == poll_policy.MIN_INTERVAL_S
        assert self._active(_usage7(90, 80), _usage7(60, 80)) == poll_policy.MIN_INTERVAL_S

    def test_a_binding_flip_is_not_movement(self):
        # 5h 84 -> 84.1 while the 7d sits at 95.9 under 85/97: by policy the
        # binding window flips to the 5h, but the raw max is flat (decays)
        # and the 5h rose 0.1 (no urgency). Reading the flip as 11.8 points
        # of movement would halve the interval on nothing.
        assert (
            self._active(
                _usage7(84, 95.9), _usage7(84.1, 95.9), threshold=85.0, weekly_threshold=97.0
            )
            == 270.0
        )

    def test_a_window_present_on_one_side_only_is_ignored(self):
        prev = {"five_hour": {"pct": 10.0}}  # no weekly window reported yet
        assert self._active(prev, _usage7(10, 85)) == poll_policy.MIN_INTERVAL_S

    def test_a_scoped_window_rises_against_the_weekly_line(self):
        def usage(fable):
            return {
                "five_hour": {"pct": 10.0},
                "seven_day": {"pct": 10.0},
                "scoped": [{"name": "Fable", "pct": fable}],
            }

        assert (
            self._active(
                usage(80.0), usage(83.0), threshold=85.0, weekly_threshold=97.0,
                models=("Fable",),
            )
            == poll_policy.URGENT_INTERVAL_S
        )
        # Unconfigured, the scoped window is invisible to the planner too.
        assert (
            self._active(usage(80.0), usage(83.0), threshold=85.0, weekly_threshold=97.0)
            == 270.0
        )
