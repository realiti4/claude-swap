"""The ``balance`` auto-switch strategy: plan-weighted load balancing.

The other strategies stay on one account until it nears a line, then move.
``balance`` instead keeps every account draining at a pace that lands it on
its weekly cap right when that cap resets, so no account's quota expires
unused while another one runs dry.

How an account is scored
------------------------
For each weekly window that gates the account (the 7-day window, plus any
per-model window named by ``autoswitch.model``), work out how much quota per
hour the account could still absorb and only just reach 100% at the reset::

    spare rate = plan weight x (fraction of the window still free)
                 / (hours until the window resets)

The account's score is the *lowest* spare rate over those windows, because
the tightest window is the one that will stop it first. The five-hour window
is left out: it resets too often to say anything about weekly pacing, and the
engine's ordinary lines still guard it.

A worked example. Two accounts, one Max 5x (weight 5) and one Pro (weight 1),
both with 3 days (72 h) until their weekly reset:

    Max 5x at 40% used:  5 x 0.60 / 72 = 0.0417  -> shown as 41.7
    Pro    at 10% used:  1 x 0.90 / 72 = 0.0125  -> shown as 12.5

The Max account scores higher even though it is more used, because it has
far more absolute quota left. Using an account lowers its score; an idle
account's score rises as its reset approaches. The engine stays on the
highest score and moves only when another account beats the active one by
``autoswitch.balanceMargin`` percent, so load rotates without flapping.

Plan weights
------------
:data:`TIER_WEIGHTS` maps Claude Code's ``rateLimitTier`` values to a weight
relative to Pro. The figures are Anthropic's published *per-session*
multiples for Max 5x and Max 20x. Whether weekly quota scales by the same
factor is an assumption, not something measured.
``autoswitch.balanceWeights`` (for example ``"1=5,2=1"``) overrides the
weight for a slot number when the assumption is wrong for someone.

Everything here is a pure function except :func:`plan_weights`. That one
reads the stored plan cache: no network and no credentials.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import NamedTuple

from claude_swap import oauth, poll_policy, subscription

# Weight of each plan relative to Pro, keyed by the ``rateLimitTier`` value
# that the plan cache (subscription.py) stores for an account.
TIER_WEIGHTS: dict[str, float] = {
    "default_claude_ai": 1.0,        # Pro
    "default_claude_max_5x": 5.0,    # Max 5x
    "default_claude_max_20x": 20.0,  # Max 20x
}

# Weight for an account whose plan is unknown (no cache entry yet, or a tier
# this table does not list). Treating it as Pro under-rates a bigger plan,
# which is the cautious direction: it is used less, not run dry.
DEFAULT_WEIGHT = 1.0

# Hours to assume when a window reports no reset time: one full week, the
# longest a weekly window can have left.
HOURS_IN_A_WEEK = 168.0

# Floor on "hours until reset". A window a few seconds from resetting would
# otherwise divide by almost zero and score absurdly high, pulling every
# move onto an account whose quota is about to refill anyway.
MIN_HOURS_TO_RESET = 0.5

# Largest weight a ``balanceWeights`` override may set. Anything above this
# is almost certainly a typo, and one huge weight would pin the engine to
# one account forever.
MAX_WEIGHT_OVERRIDE = 1000.0

# While Claude Code is carrying a large conversation, a move costs more,
# because the new account starts with a cold prompt cache and the whole
# context is sent again at full price. The margin a candidate must beat is
# multiplied by this factor while that is the case.
BIG_CONTEXT_MARGIN_FACTOR = 2.0

# How far back to look for "a session currently carrying a large context".
# Ten minutes is twice the Anthropic API's default five-minute prompt-cache
# lifetime, so a session that paused briefly still counts as warm.
BIG_CONTEXT_LOOKBACK_S = 600.0

# Scores are tiny fractions (quota per hour). Multiplying by this for display
# turns 0.0417 into 41.7, which is easier to compare at a glance.
DISPLAY_SCALE = 1000.0


class SpareRate(NamedTuple):
    """One account's balance score and the window that set it."""

    score: float
    """Plan-weighted fraction of quota per hour left; higher means more spare."""

    detail: str
    """Short human description of the binding window, e.g. ``"7d 40%/72h"``."""


def parse_weight_overrides(value: str | None) -> dict[str, float]:
    """Parse ``autoswitch.balanceWeights`` into ``{slot number: weight}``.

    The setting is a comma-separated list of ``slot=weight`` pairs, for
    example ``"1=5, 2=1"`` -> ``{"1": 5.0, "2": 1.0}``. A part that is not a
    pair, has a non-numeric slot, or has a weight outside
    ``(0, MAX_WEIGHT_OVERRIDE]`` is skipped. A typo in one pair should not
    throw away the rest, nor stop the engine.
    """
    overrides: dict[str, float] = {}
    if not value:
        return overrides
    for part in value.split(","):
        slot_text, separator, weight_text = part.partition("=")
        if not separator:
            continue  # no "=": not a pair
        slot = slot_text.strip()
        if not slot.isdigit():
            continue
        try:
            weight = float(weight_text)
        except ValueError:
            continue
        if 0 < weight <= MAX_WEIGHT_OVERRIDE:
            overrides[slot] = weight
    return overrides


def weight_for_plan(plan_record: Mapping | None) -> float:
    """Return the weight for one account's stored plan record.

    ``plan_record`` is an entry from the plan cache (``subscription.PlanStore``),
    shaped like ``{"plan": {"tier": "default_claude_max_5x", ...}, ...}``.
    A missing record, or one with an unknown tier, gets :data:`DEFAULT_WEIGHT`.
    """
    if not isinstance(plan_record, Mapping):
        return DEFAULT_WEIGHT
    plan = plan_record.get("plan")
    if not isinstance(plan, Mapping):
        return DEFAULT_WEIGHT
    tier = plan.get("tier")
    if not isinstance(tier, str):
        return DEFAULT_WEIGHT
    return TIER_WEIGHTS.get(tier, DEFAULT_WEIGHT)


def plan_weights(switcher, overrides: str | None) -> dict[str, float]:
    """Return ``{slot number: weight}`` for every account in the sequence.

    Each account's weight comes from its plan in the stored plan cache. Then
    the ``autoswitch.balanceWeights`` overrides are applied on top. The cache
    is read from disk only: no network call and no credentials. An unreadable
    cache gives every account the default weight, so balance still works,
    just without the plan sizes.
    """
    sequence_data = switcher._get_sequence_data() or {}
    accounts = sequence_data.get("accounts", {})
    try:
        plan_records = switcher._subscription_store().load()
    except Exception:  # noqa: BLE001 - no cache only means default weights
        plan_records = {}

    weights: dict[str, float] = {}
    for num in sequence_data.get("sequence", []):
        account = accounts.get(str(num)) or {}
        key = subscription.identity_key(
            account.get("email", ""), account.get("organizationUuid")
        )
        weights[str(num)] = weight_for_plan(plan_records.get(key))
    weights.update(parse_weight_overrides(overrides))
    return weights


def spare_rate(
    usage: dict | str | None,
    models: tuple[str, ...],
    weight: float,
    now: float,
) -> SpareRate | None:
    """Score one account from its usage reading (see the module docstring).

    ``usage`` is the account's usage entry as the engine holds it, ``models``
    the per-model windows that count (``autoswitch.model``), ``weight`` the
    plan weight and ``now`` a Unix timestamp. Returns ``None`` when no weekly
    window can be read, for example an expired token or no reading yet. The
    caller then leaves the account out of balance ranking.
    """
    if not isinstance(usage, dict):
        return None

    tightest: SpareRate | None = None
    for label, pct_used, resets_at in oauth.relevant_windows(usage, models):
        if label == oauth.FIVE_HOUR_LABEL:
            continue  # weekly pacing only; see the module docstring

        reset_ts = poll_policy.parse_reset_ts(resets_at)
        if reset_ts is not None and reset_ts > now:
            hours_left = (reset_ts - now) / 3600.0
        else:
            # No reset time, or one already in the past (the reading is
            # older than the rollover): assume a full week, the cautious
            # choice, since it gives the lowest rate.
            hours_left = HOURS_IN_A_WEEK
        hours_left = max(hours_left, MIN_HOURS_TO_RESET)

        fraction_free = max(100.0 - pct_used, 0.0) / 100.0
        score = weight * fraction_free / hours_left

        if tightest is None or score < tightest.score:
            tightest = SpareRate(score, f"{label} {pct_used:.0f}%/{hours_left:.0f}h")
    return tightest


def margin_factor(recent_peak_context: int, big_context: float) -> float:
    """Return the multiplier for the balance margin right now.

    It is :data:`BIG_CONTEXT_MARGIN_FACTOR` while a recent Claude Code request
    carried at least ``big_context`` tokens, and ``1.0`` otherwise.
    ``big_context`` of 0 turns the doubling off.
    """
    if big_context > 0 and recent_peak_context >= big_context:
        return BIG_CONTEXT_MARGIN_FACTOR
    return 1.0


def required_score(active_score: float, margin_pct: float, factor: float) -> float:
    """Return the score a candidate must reach to take over from the active account.

    With a 25% margin and an active score of 40, a candidate needs 50. The
    margin is what keeps two nearly equal accounts from swapping back and
    forth on every tick.
    """
    return active_score * (1.0 + margin_pct * factor / 100.0)


def fmt(score: float | None) -> str:
    """Format a score for display: scaled by :data:`DISPLAY_SCALE`, ``-`` if unknown."""
    return "-" if score is None else f"{score * DISPLAY_SCALE:.1f}"
