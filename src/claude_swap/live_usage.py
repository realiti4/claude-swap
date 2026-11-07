"""Live usage from the rate-limit headers on Claude API replies.

Every ``/v1/messages`` reply to a Claude Code OAuth session carries the
account's unified rate-limit state::

    anthropic-ratelimit-unified-status: allowed
    anthropic-ratelimit-unified-5h-utilization: 0.05
    anthropic-ratelimit-unified-5h-reset: 1792788000
    anthropic-ratelimit-unified-7d-utilization: 0.42
    anthropic-ratelimit-unified-7d-reset: 1793000000
    anthropic-ratelimit-unified-representative-claim: five_hour

That is the same 5h/7d picture ``/api/oauth/usage`` returns, measured on the
request itself: free, and as fresh as the session's last call. The usage
endpoint allows about 30 requests an hour per account (see ``poll_policy``),
so its reading of a busy account is minutes old; these headers are not.

They carry no per-model (scoped) windows and no spend, so a reading parsed
here is partial by design: ``UsageStore.record_live`` keeps it beside the
full reading instead of replacing it.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime, timezone

from claude_swap.oauth import format_reset

_PREFIX = "anthropic-ratelimit-unified-"

#: Header stem -> the window key ``oauth.build_usage_result`` uses.
_WINDOWS = {"5h": "five_hour", "7d": "seven_day"}

#: ``representative-claim`` values that name one of those windows.
_CLAIMS = {"five_hour": "five_hour", "seven_day": "seven_day"}


def _finite_non_negative(text: str | None) -> float | None:
    if text is None:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value


def _window(headers: Mapping[str, str], stem: str) -> dict | None:
    utilization = _finite_non_negative(headers.get(f"{_PREFIX}{stem}-utilization"))
    if utilization is None:
        return None
    entry: dict = {"pct": utilization * 100.0}
    reset = _finite_non_negative(headers.get(f"{_PREFIX}{stem}-reset"))
    if reset is not None:
        resets_at = datetime.fromtimestamp(reset, tz=timezone.utc).isoformat()
        entry["resets_at"] = resets_at
        entry["countdown"], entry["clock"] = format_reset(resets_at)
    return entry


def parse_ratelimit_headers(headers: Mapping[str, str]) -> dict | None:
    """Read the 5h/7d windows from one reply's rate-limit headers.

    Args:
        headers: The reply's headers. Names match case-insensitively.

    Returns:
        A usage dict in the shape ``oauth.build_usage_result`` produces, with
        only the windows the reply measured, or ``None`` when it measured
        none (no rate-limit headers, or none readable). A ``rejected`` status
        puts the window its ``representative-claim`` names at 100% or more:
        the reply is the server refusing the request on that window, whatever
        rounding the utilization shows.
    """
    lowered = {name.lower(): value for name, value in headers.items()}
    usage: dict = {}
    for stem, key in _WINDOWS.items():
        entry = _window(lowered, stem)
        if entry is not None:
            usage[key] = entry
    if lowered.get(f"{_PREFIX}status") == "rejected":
        claimed = _CLAIMS.get(lowered.get(f"{_PREFIX}representative-claim", ""))
        if claimed is not None:
            entry = usage.setdefault(claimed, {"pct": 100.0})
            entry["pct"] = max(entry["pct"], 100.0)
    return usage or None
