"""Fetch ChatGPT usage and shape it the way the rest of cswap already reads.

The whole point of this module is the mapping. cswap's renderers, its pace
calculation, its JSON output and its autoswitch comparison all consume a dict
with ``five_hour``/``seven_day`` windows of ``{"pct", "resets_at", "countdown",
"clock"}``. Producing exactly that shape from the ChatGPT response is what lets
every Claude-side consumer handle a Codex account with no branching at all.

Two conversions matter and are easy to get wrong:

- ChatGPT reports ``used_percent``; cswap's key is ``pct``.
- ChatGPT reports ``resets_at`` as epoch seconds; ``pace.compute_pace`` parses
  an ISO string. A raw epoch would silently disable pace for every Codex row.

Failure is reported, never raised: a broken account shows its status in the
usage column (codex-auth's behavior, wording included) and drops out of
autoswitch candidacy.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

from claude_swap.codex.plans import normalize_plan
from claude_swap.oauth import format_reset

_logger = logging.getLogger(__name__)

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
ACCOUNTS_URL = "https://chatgpt.com/backend-api/accounts"

USER_AGENT = "claude-swap/1.0"


@dataclass(frozen=True)
class UsageFetch:
    """One usage fetch: either a usage dict, or a sentinel explaining why not."""

    usage: dict | None = None
    sentinel: str | None = None


def _iso(epoch: object) -> str | None:
    """Convert epoch seconds to an ISO-8601 UTC string, or None."""
    if not isinstance(epoch, (int, float)) or isinstance(epoch, bool) or epoch <= 0:
        return None
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _window(raw: object) -> dict | None:
    """Map one ChatGPT usage window onto cswap's window shape."""
    if not isinstance(raw, dict):
        return None
    pct = raw.get("used_percent")
    if not isinstance(pct, (int, float)) or isinstance(pct, bool):
        return None
    entry: dict = {"pct": pct}
    resets_at = _iso(raw.get("resets_at"))
    if resets_at:
        entry["resets_at"] = resets_at
        entry["countdown"], entry["clock"] = format_reset(resets_at)
    return entry


def build_usage_result(data: object) -> dict | None:
    """Normalize a ``wham/usage`` response into cswap's usage dict."""
    if not isinstance(data, dict):
        return None

    result: dict = {}

    primary = _window(data.get("primary"))
    if primary:
        result["five_hour"] = primary

    secondary = _window(data.get("secondary"))
    if secondary:
        result["seven_day"] = secondary

    credits = data.get("credits")
    if isinstance(credits, dict) and credits.get("has_credits"):
        result["spend"] = {
            "unlimited": bool(credits.get("unlimited")),
            "balance": credits.get("balance"),
        }

    plan = normalize_plan(data.get("plan_type"))
    if plan:
        result["plan"] = plan

    return result or None


def _get_json(url: str, access_token: str, account_id: str, timeout_s: float) -> object:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "ChatGPT-Account-Id": account_id,
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return json.loads(resp.read().decode())


def fetch_usage(
    access_token: str, account_id: str, timeout_s: float = 10.0
) -> UsageFetch:
    """Fetch one account's usage. Never raises."""
    if not access_token or not account_id:
        # codex-auth's wording, kept so a user who knows one tool reads the
        # other's output without translation.
        return UsageFetch(sentinel="MissingAuth")

    try:
        data = _get_json(USAGE_URL, access_token, account_id, timeout_s)
    except urllib.error.HTTPError as e:
        _logger.debug("Codex usage fetch failed: http-%s", e.code)
        return UsageFetch(sentinel=f"http {e.code}")
    except urllib.error.URLError as e:
        _logger.debug("Codex usage fetch failed: network (%s)", type(e).__name__)
        return UsageFetch(sentinel="network")
    except Exception as e:
        _logger.debug("Codex usage fetch failed: %s", type(e).__name__)
        return UsageFetch(sentinel="bad-response")

    usage = build_usage_result(data)
    if usage is None:
        return UsageFetch(sentinel="bad-response")
    return UsageFetch(usage=usage)


def fetch_workspace_names(
    access_token: str, account_id: str, timeout_s: float = 10.0
) -> dict[str, str]:
    """Map ``chatgpt_account_id`` to workspace name.

    Entries with a null or empty name are omitted rather than stored as an
    empty string, so a later successful fetch can still fill them in. Failures
    return an empty mapping — a missing workspace name is cosmetic and must
    never fail a listing.
    """
    if not access_token or not account_id:
        return {}
    try:
        data = _get_json(ACCOUNTS_URL, access_token, account_id, timeout_s)
    except Exception as e:
        _logger.debug("Codex account fetch failed: %s", type(e).__name__)
        return {}

    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return {}
    out: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        ident, name = item.get("id"), item.get("name")
        if ident and isinstance(name, str) and name:
            out[str(ident)] = name
    return out
