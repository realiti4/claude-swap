"""Usage source that reads utilization from unified rate-limit response headers.

Upstream issue realiti4/claude-swap#220: ``GET /api/oauth/usage`` rate-limits
per account, keyed on that account's own inference activity. So the busiest
accounts, the ones most worth watching, answer 429 and never report. The
same account's token still gets a 200 from ``POST /v1/messages``, and that
response, on a 429 as well as a 200, carries the same utilization data as
``anthropic-ratelimit-unified-*`` response headers. This module parses those
headers and probes for them. A later patch wires the probe into the regular
fetch path; nothing here calls it on its own.

Two things a caller must not get wrong:

- The headers are fractions (``0.11`` means 11%), while cswap's own
  ``pct``/``utilization`` fields, and the shape ``oauth.build_usage_result``
  consumes, are percentages. ``parse_unified_headers`` multiplies by 100
  before handing off, so its output renders through every existing display
  path exactly like a real ``/api/oauth/usage`` fetch.
- The probe spends a small amount of the account's own quota: a real
  completion with ``max_tokens=1``, roughly 10 tokens including the fixed
  prompt. Every caller must bound how often it fires.

``PROBE_MODEL`` is a dated snapshot id, and dated snapshots are retired.
Nothing here notices: ``probe_usage`` swallows every transport failure, so
the day Anthropic drops that snapshot the 404 reads as "probe failed", every
429 falls back to the original error, and the whole issue-#220 rescue goes
quiet with no diagnostic beyond a DEBUG line. It is pinned rather than
aliased deliberately (an alias would silently change what the probe costs
and which limits it counts against), so keeping it current is a maintenance
obligation, not something the code can carry on its own. Any cheap model the
account can call works; the reply is discarded and only the response headers
are read.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Mapping
from datetime import datetime, timezone

from claude_swap import oauth

_logger = logging.getLogger("claude-swap")

MESSAGES_URL = "https://api.anthropic.com/v1/messages"
PROBE_MODEL = "claude-haiku-4-5-20251001"
MESSAGES_API_VERSION = "2023-06-01"

_WINDOWS = (("5h", "five_hour"), ("7d", "seven_day"))


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive lookup by header name.

    HTTP header names are case-insensitive by spec, but the mapping a caller
    hands in is not guaranteed to normalize case (a test fixture, or a real
    ``HTTPMessage`` that already normalizes it another way). A linear scan is
    fine: callers only ever look up a handful of fixed header names.
    """
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def _reset_iso(raw: str) -> str | None:
    """Epoch-seconds string to an ISO 8601 string ending in ``Z``, or None."""
    try:
        epoch = int(raw)
    except (TypeError, ValueError):
        return None
    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except (OverflowError, OSError, ValueError):
        return None


def parse_unified_headers(headers: Mapping[str, str]) -> dict | None:
    """Normalize ``anthropic-ratelimit-unified-*`` headers into cswap's usage shape.

    Reads the ``5h``/``7d`` ``utilization`` and ``reset`` headers
    case-insensitively, scales utilization from a fraction to a percentage,
    then delegates the final shape to ``oauth.build_usage_result`` so the
    output is indistinguishable from a real usage-endpoint fetch. A window
    whose ``utilization`` header is missing or unparseable is skipped
    entirely, not defaulted to 0 (which would misreport a window this source
    never actually saw as an idle one). Returns None when no window
    survives, matching ``build_usage_result``'s own empty-result contract.
    """
    endpoint_shaped: dict = {}
    for prefix, key in _WINDOWS:
        raw_util = _header(headers, f"anthropic-ratelimit-unified-{prefix}-utilization")
        if raw_util is None:
            continue
        try:
            pct = float(raw_util) * 100
        except ValueError:
            continue
        window: dict = {"utilization": pct}
        raw_reset = _header(headers, f"anthropic-ratelimit-unified-{prefix}-reset")
        if raw_reset is not None:
            resets_at = _reset_iso(raw_reset)
            if resets_at is not None:
                window["resets_at"] = resets_at
        endpoint_shaped[key] = window
    if not endpoint_shaped:
        return None
    return oauth.build_usage_result(endpoint_shaped)


def probe_usage(access_token: str, timeout_s: float = 30.0) -> dict | None:
    """Spend a 1-token completion to read live utilization off response headers.

    ``POST /v1/messages`` rate-limits far more loosely than ``GET
    /api/oauth/usage`` (issue #220), so this succeeds, or comes back 429 with
    the unified headers still attached, exactly on the accounts the usage
    endpoint goes silent on. Uses the same bearer and beta headers cswap
    already sends to the usage endpoint (see ``oauth.request_usage_data``),
    plus ``anthropic-version``, which ``/v1/messages`` rejects with a 400
    when it's absent but the usage endpoint never required. Returns None on
    any transport failure, or when the response carried no unified headers
    at all.
    """
    body = json.dumps(
        {
            "model": PROBE_MODEL,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}],
        }
    ).encode()
    headers = {
        "Authorization": f"Bearer {access_token}",
        "anthropic-beta": oauth.OAUTH_BETA_HEADER,
        "anthropic-version": MESSAGES_API_VERSION,
        "Content-Type": "application/json",
        "User-Agent": "claude-swap/1.0",
    }
    req = urllib.request.Request(
        MESSAGES_URL, data=body, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return parse_unified_headers(resp.headers)
    except urllib.error.HTTPError as e:
        if not e.headers:
            return None
        return parse_unified_headers(e.headers)
    except Exception as e:
        _logger.debug("Unified-header probe failed: %r", e)
        return None
