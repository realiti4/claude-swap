"""Subscription plan per account, for display only (#350).

The plan comes from ``GET /api/oauth/profile`` — the same endpoint
:func:`oauth.fetch_oauth_profile` calls to resolve identity. That function
returns only the identity it needs; the response carries a good deal more, of
which four ``organization`` fields are kept here.

Measured shapes, one account each:

    =====================  ==========  ======================  ===================
    state                  status      rate_limit_tier         billing_type
    =====================  ==========  ======================  ===================
    active Max             active      default_claude_max_20x  stripe_subscription
    cancelled, in period   active      default_claude_max_20x  stripe_subscription
    lapsed                 canceled    default_claude_ai       none
    =====================  ==========  ======================  ===================

``subscription_status`` does not move at cancellation, only at lapse, so a
cancelled-but-still-running subscription is indistinguishable from one that
will renew. That is why renewal is not shown. Expired is ``canceled`` together
with the free tier — both, so neither field alone can mark an account.

Paced on its own clock and stored in its own file, never through the usage
store. The usage endpoint is rate limited and backs off per account; a plan
refresh that rode on it would freeze an old label behind exactly the accounts
most likely to have changed plan — the ones whose usage calls are failing.

A refresh only ever uses an access token that is already valid. This module
never refreshes a token itself: rotating an inactive slot's refresh token
without persisting the result would leave that slot holding a consumed token.
The usage collector refreshes tokens on its own schedule, and a due plan whose
token is not valid yet simply waits for the next pass.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from claude_swap import oauth
from claude_swap.settings import atomic_write_json

_logger = logging.getLogger("claude-swap")

PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
PLAN_FILENAME = "subscription_plans.json"

# Plans change on a billing cadence, not a usage one. Six hours keeps a lapse
# visible the same day without adding meaningful traffic: one profile call per
# account four times a day.
PLAN_TTL_S = 6 * 3600

FREE_TIER = "default_claude_ai"

# Only values that have actually been observed. An unknown tier falls back to
# the raw string rather than a guessed label — a wrong "Pro" is worse than an
# honest "default_claude_pro".
_TIER_LABELS = {
    "default_claude_ai": "Free",
    "default_claude_max_5x": "Max 5x",
    "default_claude_max_20x": "Max 20x",
}

# A seat on a Team organization is not a personal subscription, whatever rate
# tier the seat carries, so the organization type wins where it is known.
_ORG_TYPE_LABELS = {
    "claude_team": "Team",
}

_KEPT_FIELDS = (
    ("organization_type", "organizationType"),
    ("billing_type", "billingType"),
    ("subscription_status", "subscriptionStatus"),
)


def fetch_plan(access_token: str, timeout: float = 5.0) -> dict | None:
    """The plan fields of ``/api/oauth/profile``, or None on any failure.

    Requires a non-empty ``rate_limit_tier``, the field every label derives
    from; a response without one is treated as no answer rather than as a plan
    with nothing in it. Must not be called while a credential or config lock is
    held, the same rule as every other network call here.
    """
    req = urllib.request.Request(
        PROFILE_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "User-Agent": "claude-swap/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:  # noqa: BLE001 — display-only; never let it surface
        _logger.debug("Plan fetch failed: %r", e)
        return None
    organization = data.get("organization") if isinstance(data, dict) else None
    if not isinstance(organization, dict):
        return None
    tier = organization.get("rate_limit_tier")
    if not isinstance(tier, str) or not tier:
        return None
    plan = {"tier": tier}
    for source, key in _KEPT_FIELDS:
        value = organization.get(source)
        if isinstance(value, str) and value:
            plan[key] = value
    return plan


def plan_label(plan: dict | None) -> str | None:
    """Human label: organization type where known, else tier, else raw tier."""
    if not plan:
        return None
    org_label = _ORG_TYPE_LABELS.get(plan.get("organizationType"))
    if org_label:
        return org_label
    tier = plan.get("tier")
    return _TIER_LABELS.get(tier, tier)


def is_expired(plan: dict | None) -> bool:
    """A subscription that has lapsed: ``canceled`` AND back on the free tier."""
    return (
        bool(plan)
        and plan.get("subscriptionStatus") == "canceled"
        and plan.get("tier") == FREE_TIER
    )


def identity_key(email: str, org_uuid: str | None) -> str:
    """Store key. Identity, not slot number — ``swap``/``move`` renumber slots."""
    return f"{email}\n{org_uuid or ''}"


def usable_access_token(credentials: str | None) -> str | None:
    """The access token in ``credentials`` if it is present and unexpired."""
    if not credentials:
        return None
    blob = oauth.extract_oauth_data(credentials)
    if not isinstance(blob, dict):
        return None
    token = blob.get("accessToken")
    if not isinstance(token, str) or not token:
        return None
    if oauth.is_oauth_token_expired(blob.get("expiresAt")):
        return None
    return token


def valid_record(record: object) -> bool:
    return (
        isinstance(record, dict)
        and isinstance(record.get("plan"), dict)
        and isinstance(record["plan"].get("tier"), str)
        and isinstance(record.get("fetchedAt"), (int, float))
        and not isinstance(record.get("fetchedAt"), bool)
    )


def is_due(record: object, now: float, ttl: float = PLAN_TTL_S) -> bool:
    """Whether a record needs refreshing. A future stamp is clock skew: refresh."""
    if not valid_record(record):
        return True
    fetched_at = record["fetchedAt"]
    return fetched_at > now or now - fetched_at >= ttl


class PlanStore:
    """``subscription_plans.json``: ``{identity_key: {"plan": {...}, "fetchedAt": epoch}}``.

    No lock. Several processes (``list``, ``watch``, the menu bar) may refresh
    at once, so ``save`` re-reads and merges just before its atomic write: two
    writers refreshing different accounts both land, and the worst a true race
    costs is one extra profile call on the next pass. A lock would also force
    the network call outside it anyway, which is the shape this already has.
    """

    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self.path = path
        self.clock = clock

    def load(self) -> dict[str, dict]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def save(self, updates: dict[str, dict]) -> None:
        merged = self.load()
        merged.update(updates)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.path, merged)
