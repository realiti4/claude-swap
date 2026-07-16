"""Cadence policy for the ``/api/oauth/usage`` endpoint — every number in one place.

The endpoint enforces a per-access-token budget on non-first-party clients:
a **rolling ~60-minute window of ~28-30 requests per token × UA-class**
(measured 2026-07-11, probe3, two runs: a rested token admitted 30 requests
before the first 429; the post-drain 429 oscillation ended exactly when the
drain burst aged 60 minutes; steady 1/180 s polling then ran 96 minutes from
a rested window with zero 429s). It is NOT a bucket with a refill rate:
capacity returns only as old requests age out of the trailing hour, so a
burst saturates the token for up to a full hour — pausing does not restore
headroom early, and earlier "refill rate" estimates were artifacts of
measuring while saturated. Error bars: the horizon is bracketed to ~55-64
minutes from a single transition event, the exact edge algorithm (likely a
Cloudflare sliding-window approximation) is undocumented, and Anthropic can
retune it any day — so the constants below lean only on the robust parts:
a sustained rate safely under the cap, and an ~hour recovery horizon. The
budget target is an **average of at most ~1 request / 3 minutes per token**
(20/hour vs the ~28-30/hour cap), leaving ~8-10 requests/hour of headroom
for manual commands, wake-from-sleep catch-up, and the bounded urgent mode
below. Health invariant to watch in the logs: steady state shows zero
http-429, and any post-burst 429 clears within ≤60 minutes — an episode
outlasting an hour at modest rates means this model needs revisiting.

Plans computed here are persisted per account in the usage store
(``nextPollAt``/``pollIntervalS``) by whichever collector fetched, so every
surface — ``cswap list``, the TUI, the menu bar, the auto engine — inherits
the same cadence no matter how often it repaints.

If a future probe revises the measured shape, adjust the constants in this
module only.
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
# to MIN_INTERVAL_S) — worst case margin/movement-delta ≈ 15 polls per
# episode, inside the measured ~28-30 request rolling-hour window; overshoot
# on top of steady traffic is absorbed by the post-429 floor below.
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

# Reaction to a 429 with ``Retry-After: 0`` (the saturated-window edge):
# probe at most every 5 minutes (≤12/hour) so aging-out — up to ~30/hour —
# outpaces the probing (used by the usage store's failure backoff)...
EDGE_BACKOFF_S = 300.0
# ...and while any 429 was seen on the token within this window, floor the
# planned cadence here so freed capacity accumulates instead of being
# re-spent. The window matches the saturation horizon: a full trailing hour
# takes up to 60 minutes to age out.
POST_429_MIN_INTERVAL_S = 360.0
RECENT_429_WINDOW_S = 3600.0

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


def window_threshold(
    label: str, threshold_5h: float, threshold_7d: float, base: float
) -> float:
    """The switch threshold for one window label.

    5h/7d get their own per-window thresholds; any other (folded model)
    window falls back to the account-wide ``base`` threshold.
    """
    if label == "5h":
        return threshold_5h
    if label == "7d":
        return threshold_7d
    return base


def account_triggered(
    usage: dict | None,
    models: tuple[str, ...],
    threshold_5h: float,
    threshold_7d: float,
    base: float,
) -> bool:
    """True if ANY relevant window is at/above its per-window threshold.

    The OR'd per-window switch trigger: 5h>=threshold_5h OR 7d>=threshold_7d
    OR any folded model window >= base. ``usage`` must be a decision-value
    dict (unknown/sentinel usage yields no windows → not triggered; the
    caller handles unknown-usage separately).
    """
    for label, pct, _ in oauth.relevant_windows(usage, models):
        if pct >= window_threshold(label, threshold_5h, threshold_7d, base):
            return True
    return False


def account_landing_ok(
    usage: dict | None,
    models: tuple[str, ...],
    threshold_5h: float,
    threshold_7d: float,
    base: float,
) -> bool:
    """True if a candidate is a healthy landing: usage is known AND no
    relevant window is at/above its threshold (so it won't re-trigger the
    switch on the very next tick). Unknown usage (no windows) is never a
    healthy landing.
    """
    if not oauth.relevant_windows(usage, models):
        return False
    return not account_triggered(
        usage, models, threshold_5h, threshold_7d, base
    )


def soonest_7d_reset_ts(usage: dict | None, now: float) -> float | None:
    """Future epoch when this account's 7-day window resets, or None.

    The ordering key for the ``soonest-reset`` strategy — burn the account
    whose weekly (7d) budget expires soonest first. A missing/past/unparseable
    reset yields None (sorted last by the caller, never chosen preferentially).
    """
    for label, _, resets_at in oauth.relevant_windows(usage):
        if label == "7d":
            ts = parse_reset_ts(resets_at)
            if ts is not None and ts > now:
                return ts
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
        # Floored so a sub-floor base (urgent mode's 60s) snaps straight back
        # to the normal cadence once movement stops, instead of decaying
        # through 90s/135s polls that the budget never intended.
        moving = False
        interval = min(ceiling, max(MIN_INTERVAL_S, base * 1.5))
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
