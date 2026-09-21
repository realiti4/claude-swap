"""Balance policy: spend every weekly window on schedule.

``consume-first`` drains one account's weekly (7d) window before touching the
next, so by mid-week most accounts sit in the 90s and only one has room — one
5-hour window of capacity, too little for parallel work. ``balance`` instead
asks, per account, how far *behind* an even weekly schedule it is, and uses
the account that is furthest behind. Every account then finishes its week at
roughly the same time (``lead_hours`` before its reset, so nothing is wasted
to a late burst), and many accounts keep 5h headroom at once.

The schedule: ``target(t) = min(100, 100 * elapsed / (period - lead))``. The
window start comes from ``pace.window_elapsed_s`` — the same derivation the
pace marker uses, so the two can never disagree about where a week starts.
``slack = target - used_7d`` is positive when an account is behind schedule
(it *should* be used) and negative when it is ahead.

The 5h window gates and penalises: ``projected_5h = pct_5h + load_per_session
* busy_sessions`` estimates where the 5h window lands once the sessions
already placed there keep working. An account whose projection reaches
``five_hour_ceiling`` is not eligible; otherwise ``score = slack - weight *
projected_5h``, so of two equally behind accounts the emptier 5h wins.

``score`` is always computed as ``slack - weight * projected_5h`` for known
accounts. Eligibility additionally requires: headroom > 0 (not at-limit), then
no weekly/model window >= threshold (weekly-threshold), then projected_5h <
ceiling (five-hour-ceiling).

Pure, like ``pace``: no fetching, no state, no clock. One function serves the
lane-0 ``autoswitch.strategy = balance`` ranking and (later) managed-session
placement and reassignment targets.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from claude_swap import oauth, pace, poll_policy
from claude_swap.settings import AutoSwitchSettings

REASON_OK = "ok"
REASON_UNKNOWN_USAGE = "unknown-usage"
REASON_FIVE_HOUR_CEILING = "five-hour-ceiling"
REASON_WEEKLY_THRESHOLD = "weekly-threshold"
REASON_AT_LIMIT = "at-limit"


@dataclass(frozen=True)
class BalanceParams:
    lead_hours: float = 24.0          # autoswitch.balanceLeadHours
    five_hour_ceiling: float = 85.0   # autoswitch.balanceFiveHourCeiling
    five_hour_weight: float = 0.5     # autoswitch.balanceFiveHourWeight
    load_per_session: float = 15.0    # autoswitch.balanceLoadPerSession
    threshold: float = 90.0           # autoswitch.threshold (gates 7d + model windows)


@dataclass(frozen=True)
class AccountScore:
    """One account's balance score. ``reason`` is one of the ``REASON_*``
    constants above."""

    account: str                 # slot number as str (engine's candidate key)
    eligible: bool
    score: float | None          # None when usage unknown
    slack: float | None          # target - used_7d
    projected_5h: float | None   # pct_5h + load_per_session * busy
    headroom: float | None       # oauth.account_headroom(usage, models)
    recovery_ts: float           # binding-window recovery epoch, inf if unknown
    reason: str


def params_from_settings(settings: AutoSwitchSettings) -> BalanceParams:
    """The balance knobs of an (already clamped) ``AutoSwitchSettings``."""
    return BalanceParams(
        lead_hours=settings.balance_lead_hours,
        five_hour_ceiling=settings.balance_five_hour_ceiling,
        five_hour_weight=settings.balance_five_hour_weight,
        load_per_session=settings.balance_load_per_session,
        threshold=settings.threshold,
    )


def weekly_target_pct(resets_at_ts: float, now: float, lead_hours: float) -> float:
    """On-schedule weekly utilization at ``now``, finishing ``lead_hours``
    before the reset. ``resets_at_ts`` may be stale by whole cycles (see
    ``pace.window_elapsed_s``); the result is clamped to 100 for the last
    ``lead_hours`` of the window."""
    effective_s = pace.WEEKLY_PERIOD_S - lead_hours * 3600.0
    if effective_s <= 0:
        # Unreachable through settings (lead is bounded to 96h); a direct
        # caller asking to finish before the week starts is already late.
        return 100.0
    elapsed = pace.window_elapsed_s(resets_at_ts, now)
    return min(100.0, 100.0 * elapsed / effective_s)


def _unknown(account: str) -> AccountScore:
    """The score for an account whose usage cannot be read or trusted."""
    return AccountScore(
        account=account,
        eligible=False,
        score=None,
        slack=None,
        projected_5h=None,
        headroom=None,
        recovery_ts=math.inf,
        reason=REASON_UNKNOWN_USAGE,
    )


def score_account(
    account: str,
    usage: dict | None,
    *,
    now: float,
    models: Sequence[str],
    busy_sessions: int,
    params: BalanceParams,
) -> AccountScore:
    """Score one account; see the module docstring for the formula.

    ``usage`` is the engine's decision value — anything but a dict (``None``,
    a sentinel string for an expired or foreign credential) is untrusted and
    reads as unknown. The 7d window is required: without it there is no
    schedule to be behind. A 7d window without a usable ``resets_at`` has not
    started (the API omits the reset until first use), so its target is 0 and
    its slack is ``-used_7d``. A missing 5h window likewise counts as 0.

    Snapshots are accepted as reported by the store; decision freshness is the
    engine's job (consistent with account_headroom and binding_recovery_ts).
    """
    if not isinstance(usage, dict):
        return _unknown(account)
    # The same window set account_headroom gates on below.
    windows = oauth.relevant_windows(usage, models)
    if any(not math.isfinite(pct) for _, pct, _ in windows):
        return _unknown(account)
    # 7d is required; a missing 5h window counts as 0.
    used_7d = None
    pct_5h = 0.0
    reset_ts_7d = None
    for label, pct, resets_at in windows:
        if label == "7d":
            used_7d = pct
            reset_ts_7d = poll_policy.parse_reset_ts(resets_at)
        elif label == "5h":
            pct_5h = pct
    if used_7d is None:
        return _unknown(account)
    target = (
        weekly_target_pct(reset_ts_7d, now, params.lead_hours)
        if reset_ts_7d is not None
        else 0.0
    )
    slack = target - used_7d
    safe_busy = max(0, busy_sessions)
    projected_5h = pct_5h + params.load_per_session * safe_busy
    score = slack - params.five_hour_weight * projected_5h
    headroom = oauth.account_headroom(usage, models)
    assert headroom is not None  # a 7d window is known, so account_headroom has a window to reduce
    # Gate precedence: headroom <= 0 (at-limit) → weekly/model threshold → 5h ceiling.
    weekly_pcts = [pct for label, pct, _ in windows if label != "5h"]
    if headroom <= 0:
        reason = REASON_AT_LIMIT
    elif any(pct >= params.threshold for pct in weekly_pcts):
        reason = REASON_WEEKLY_THRESHOLD
    elif projected_5h >= params.five_hour_ceiling:
        reason = REASON_FIVE_HOUR_CEILING
    else:
        reason = REASON_OK
    return AccountScore(
        account=account,
        eligible=reason == REASON_OK,
        score=score,
        slack=slack,
        projected_5h=projected_5h,
        headroom=headroom,
        recovery_ts=poll_policy.binding_recovery_ts(usage, models, now),
        reason=reason,
    )
