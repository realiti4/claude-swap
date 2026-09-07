"""Fetch ChatGPT usage and shape it the way the rest of cswap already reads.

The whole point of this module is the mapping. cswap's renderers, its pace
calculation, its JSON output and its autoswitch comparison all consume a dict
with ``five_hour``/``seven_day`` windows of ``{"pct", "resets_at", "countdown",
"clock"}``. Producing exactly that shape from the ChatGPT response is what lets
every Claude-side consumer handle a Codex account with no branching at all.

The wire shape was read off the live endpoint (2026-08-16), not inferred from
codex-auth. That distinction cost a bug: codex-auth's ``registry.json`` stores
its own *normalized* view under ``last_usage`` as ``primary``/``secondary`` with
``resets_at``, and it is tempting to assume the API returns the same. It does
not. The response is::

    plan_type: str
    rate_limit:
      allowed: bool
      limit_reached: bool
      primary_window:   {used_percent, limit_window_seconds,
                         reset_after_seconds, reset_at}
      secondary_window: {same} | null
    credits: {has_credits, unlimited, overage_limit_reached, balance, ...}

Four conversions matter and are each easy to get wrong:

- the windows are nested under ``rate_limit``, not top level.
- ChatGPT reports ``used_percent``; cswap's key is ``pct``.
- ChatGPT reports ``reset_at`` as epoch seconds; ``pace.compute_pace`` parses an
  ISO string. A raw epoch would silently disable pace for every Codex row.
- **``primary_window`` is not necessarily the 5-hour window.** Its length is
  data, carried in ``limit_window_seconds``, and it varies by plan. Both live
  Plus accounts measured here reported a ``primary_window`` of 604800 s — a
  *week* — with ``secondary_window: null``. Mapping by position would have
  labelled weekly usage as 5-hourly: the displayed row would be wrong, and
  ``pace`` (which only applies to weekly windows) would never fire for any Codex
  account. So windows are classified by their declared length, not their key.

An absent window is normal — a plan may genuinely have only one — and must
render as absent, never as 0% used.

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

#: A rate-limit window at or above this length is cswap's "weekly" window;
#: anything shorter is the "5h" one. One day is a wide moat between the two real
#: values (5 h and 7 d), so a plan with, say, a 24-hour window would have to be
#: invented before this needs revisiting.
WEEKLY_WINDOW_MIN_S = 86400


@dataclass(frozen=True)
class UsageFetch:
    """One usage fetch: either a usage dict, or a sentinel explaining why not."""

    usage: dict | None = None
    sentinel: str | None = None
    #: The server's ``Retry-After``, in seconds, when it sent one. Honouring it
    #: is the difference between backing off and being rate-limited harder.
    retry_after_s: float | None = None


def _retry_after_seconds(err: urllib.error.HTTPError) -> float | None:
    """Parse a ``Retry-After`` header. Only the delta-seconds form is read.

    RFC 9110 also permits an HTTP-date, but this endpoint has only ever been
    observed sending seconds, and a misparsed date that yields a huge backoff
    would silently park an account for hours.
    """
    try:
        raw = err.headers.get("Retry-After")
    except AttributeError:
        return None
    if raw is None:
        return None
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _iso(epoch: object) -> str | None:
    """Convert epoch seconds to an ISO-8601 UTC string, or None."""
    if not isinstance(epoch, (int, float)) or isinstance(epoch, bool) or epoch <= 0:
        return None
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _window(raw: object) -> dict | None:
    """Map one ChatGPT rate-limit window onto cswap's window shape."""
    if not isinstance(raw, dict):
        return None
    pct = raw.get("used_percent")
    if not isinstance(pct, (int, float)) or isinstance(pct, bool):
        return None
    entry: dict = {"pct": pct}
    resets_at = _iso(raw.get("reset_at"))
    if resets_at:
        entry["resets_at"] = resets_at
        entry["countdown"], entry["clock"] = format_reset(resets_at)
    return entry


def build_usage_result(data: object) -> dict | None:
    """Normalize a ``wham/usage`` response into cswap's usage dict."""
    if not isinstance(data, dict):
        return None

    result: dict = {}

    rate_limit = data.get("rate_limit")
    if isinstance(rate_limit, dict):
        # Classify by declared length, never by key name — see the module
        # docstring. Both keys are read, and whichever is weekly-length becomes
        # seven_day. If both windows classify the same way (not observed, but
        # nothing in the response forbids it), the FIRST wins: the API lists
        # primary first, and averaging two windows of one class would invent a
        # number the server never reported.
        for key in ("primary_window", "secondary_window"):
            raw = rate_limit.get(key)
            window = _window(raw)
            if not window:
                continue
            length = raw.get("limit_window_seconds")
            if isinstance(length, (int, float)) and not isinstance(length, bool):
                slot = "seven_day" if length >= WEEKLY_WINDOW_MIN_S else "five_hour"
            else:
                # No declared length: fall back to the conventional positions.
                slot = "five_hour" if key == "primary_window" else "seven_day"
            result.setdefault(slot, window)

    # A response with no rate-limit window at all is not usable usage: every
    # consumer (the renderers, pace, the autoswitch comparison) needs a window,
    # and returning a plan-only dict renders as a blank row with no explanation
    # — which is exactly how the first live run failed.
    if "five_hour" not in result and "seven_day" not in result:
        return None

    credits = data.get("credits")
    if isinstance(credits, dict) and credits.get("has_credits"):
        result["spend"] = {
            "unlimited": bool(credits.get("unlimited")),
            "balance": credits.get("balance"),
        }

    plan = normalize_plan(data.get("plan_type"))
    if plan:
        result["plan"] = plan

    return result


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
        retry_after = _retry_after_seconds(e)
        _logger.debug(
            "Codex usage fetch failed: http-%s%s",
            e.code,
            f", retry-after {retry_after:.0f}s" if retry_after is not None else "",
        )
        return UsageFetch(sentinel=f"http {e.code}", retry_after_s=retry_after)
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
