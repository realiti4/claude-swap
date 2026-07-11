"""Cadence policy for the ``/api/oauth/usage`` endpoint — every number in one place.

The endpoint enforces a per-access-token budget on non-first-party clients
(measured 2026-07-11: a rested token tolerates a burst of ~27 requests, then
refills at roughly one request per 2.5 minutes; n=1, treat as an estimate —
``HANDOFF-usage-cadence-probe.md`` describes the bracketing probe that pins it
down). Sustained polling above the refill rate drains the bucket and parks the
token on an oscillating-429 edge for as long as the traffic continues, so the
budget target is an **average of at most ~1 request / 3 minutes per token**,
with the burst capacity left as headroom for manual commands, wake-from-sleep
catch-up, and the bounded urgent mode below.

Plans computed here are persisted per account in the usage store
(``nextPollAt``/``pollIntervalS``) by whichever collector fetched, so every
surface — ``cswap list``, the TUI, the menu bar, the auto engine — inherits
the same cadence no matter how often it repaints.

If the probe revises the measured shape, adjust the constants in this module
only.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from datetime import datetime

from claude_swap import oauth

# Freshness floor shared by every collector: an entry younger than this is
# served from the store without any fetch, so the maximum sustained rate on
# one token is 1/SERVE_TTL_S regardless of how many surfaces are open.
SERVE_TTL_S = 180.0

# Normal cadence floor — movement can halve an interval down to this, never
# below.
MIN_INTERVAL_S = 180.0

# Urgent mode: the ACTIVE account, within ESCALATION_MARGIN_PCT of the
# switch threshold, with movement observed this poll (i.e. actually burning
# toward the limit). Bounded by construction: either the threshold is crossed
# (the engine switches away) or the movement stops (the next poll decays back
# to MIN_INTERVAL_S) — worst case ≈ margin/burn-rate minutes of 1/min polling,
# well inside the burst capacity.
URGENT_INTERVAL_S = 60.0

# Decay ceilings for an account whose usage is not moving: the active account
# stays reasonably fresh, an idle alternate drifts out to ten minutes.
ACTIVE_MAX_INTERVAL_S = 300.0
CANDIDATE_DEFAULT_INTERVAL_S = 300.0
CANDIDATE_MAX_INTERVAL_S = 600.0

# A window whose binding pct moved at least this much between polls is being
# consumed somewhere (this machine, another PC, session mode) → tighten; an
# unmoved one backs off toward its ceiling.
MOVEMENT_DELTA_PCT = 1.0

# ±fraction applied to each scheduled interval so independent processes
# (watch + menu bar + auto) drift apart instead of fetching in lockstep.
JITTER_FRAC = 0.1

# Reaction to a 429 with ``Retry-After: 0`` (the drained-bucket edge): wait at
# least one refill's worth before retrying at all (used by the usage store's
# failure backoff)...
EDGE_BACKOFF_S = 300.0
# ...and while any 429 was seen on the token within this window, floor the
# planned cadence here so the bucket refills instead of re-draining.
POST_429_MIN_INTERVAL_S = 360.0
RECENT_429_WINDOW_S = 1800.0

# The engine escalates to a full candidate refresh when the active account is
# within this margin of the threshold (decision policy, but the urgent-mode
# cadence keys on the same band, so it lives with the cadence numbers).
ESCALATION_MARGIN_PCT = 15.0

# Never schedule a poll later than a known window reset (+ slack): stored
# usage is obsolete the moment the window rolls over.
RESET_SLACK_S = 60.0


def binding_pct(usage: dict | None, models: tuple[str, ...] = ()) -> float | None:
    """Utilization of the binding (worst) relevant window, or None."""
    headroom = oauth.account_headroom(usage, models)
    return None if headroom is None else 100.0 - headroom


def limiting_reset_ts(
    usage: dict | None, models: tuple[str, ...] = ()
) -> float | None:
    """Epoch when the last of the ≥100% relevant windows resets (account
    usable again)."""
    latest: float | None = None
    for _, pct, resets_at in oauth.relevant_windows(usage, models):
        if pct < 100.0:
            continue
        ts = parse_reset_ts(resets_at)
        if ts is not None and (latest is None or ts > latest):
            latest = ts
    return latest


def earliest_future_reset_ts(
    usage: dict | None, now: float, models: tuple[str, ...] = ()
) -> float | None:
    """Epoch of the next relevant-window reset ahead of ``now``, any
    utilization."""
    earliest: float | None = None
    for _, _, resets_at in oauth.relevant_windows(usage, models):
        ts = parse_reset_ts(resets_at)
        if ts is not None and ts > now and (earliest is None or ts < earliest):
            earliest = ts
    return earliest


def parse_reset_ts(resets_at: str | None) -> float | None:
    if not resets_at:
        return None
    try:
        return datetime.fromisoformat(
            str(resets_at).replace("Z", "+00:00")
        ).timestamp()
    except ValueError:
        return None


def plan_after_fetch(
    *,
    prev_interval_s: float | None,
    prev_usage: dict | None,
    new_usage: dict | None,
    is_active: bool,
    threshold: float,
    models: tuple[str, ...],
    recent_429: bool,
    now: float,
    rng: Callable[[], float] = random.random,
) -> tuple[float, float]:
    """``(next_poll_at, interval_s)`` for an account just fetched successfully.

    Movement (binding pct changed ≥ ``MOVEMENT_DELTA_PCT`` since the previous
    poll) halves the interval, floored at ``MIN_INTERVAL_S`` — or drops to
    ``URGENT_INTERVAL_S`` when the active account is moving inside the
    escalation band. No movement backs off ×1.5 toward the account's ceiling;
    unknown utilization uses the default. A recent 429 on this token floors
    the cadence at ``POST_429_MIN_INTERVAL_S`` (and suppresses urgent mode)
    until ``RECENT_429_WINDOW_S`` has passed. The scheduled time gets
    ``JITTER_FRAC`` noise, is never later than the account's next window
    reset (+ ``RESET_SLACK_S``), and an at-limit account skips straight to
    the reset that frees it (the learned interval is kept for its return).
    """
    default = MIN_INTERVAL_S if is_active else CANDIDATE_DEFAULT_INTERVAL_S
    ceiling = ACTIVE_MAX_INTERVAL_S if is_active else CANDIDATE_MAX_INTERVAL_S
    base = prev_interval_s or default
    prev_pct = binding_pct(prev_usage, models)
    new_pct = binding_pct(new_usage, models)
    if prev_pct is None or new_pct is None:
        moving = False
        interval = default
    elif abs(new_pct - prev_pct) >= MOVEMENT_DELTA_PCT:
        moving = True
        interval = max(MIN_INTERVAL_S, base / 2)
    else:
        moving = False
        interval = min(ceiling, base * 1.5)
    if (
        is_active
        and moving
        and not recent_429
        and new_pct is not None
        and new_pct >= threshold - ESCALATION_MARGIN_PCT
    ):
        interval = URGENT_INTERVAL_S
    if recent_429:
        interval = max(interval, POST_429_MIN_INTERVAL_S)

    next_poll = now + interval * (1.0 + JITTER_FRAC * (2.0 * rng() - 1.0))
    headroom = oauth.account_headroom(new_usage, models)
    if headroom is not None and headroom <= 0:
        reset_ts = limiting_reset_ts(new_usage, models)
        if reset_ts is not None and reset_ts > next_poll:
            next_poll = reset_ts
    else:
        reset_ts = earliest_future_reset_ts(new_usage, now, models)
        if reset_ts is not None:
            next_poll = min(next_poll, reset_ts + RESET_SLACK_S)
    return next_poll, interval
