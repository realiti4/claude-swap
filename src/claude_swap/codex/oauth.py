"""Refresh Codex access tokens.

codex-auth does not do this — it leaves refresh to the codex CLI and renders the
resulting HTTP status in its usage column. cswap refreshes, because autoswitch
has to compare accounts that nobody has opened in hours, and a stale token
answers 401 instead of a percentage.

Endpoint, client id and error shape were established empirically against the
live endpoint using a deliberately invalid refresh token (2026-08-16):

- ``POST https://auth.openai.com/oauth/token`` accepts a **JSON** body.
- ``client_id=app_EMoamEEZ73f0CkXaXp7hrann``. A public OAuth client identifier
  shipped inside the publicly distributed ``codex`` binary — it identifies the
  app, it does not authenticate it, and it is not a secret. (The binary also
  contains ``app_69a1d78e929881919bba0dbda1f6436d``, which this endpoint rejects
  with ``invalid_client``; it belongs to something else. Do not "fix" the id
  back to it.)
- Failures answer **401**, not 400, and the body is **nested**:
  ``{"error": {"code": "token_expired", "message": ..., "type": ...}}`` — not
  RFC 6749's flat ``{"error": "invalid_grant"}``. Both shapes are parsed, since
  the flat one is what the standard specifies and this endpoint is undocumented.

Nothing here is documented by OpenAI, so every failure mode degrades rather than
raises: the account renders its status and drops out of autoswitch candidacy,
and the rest of cswap keeps working.

**The active account is never refreshed from a stored snapshot.** The codex CLI
holds its own copy of that same refresh token and keeps ``auth.json`` current;
refreshing our copy in parallel risks invalidating whichever token the server
rotates away from, and that is a logout. Callers must pass the *live* payload
for the active account, and snapshots only for inactive ones — see
``switcher.CodexSwitcher._payload_for_usage``.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

from claude_swap.codex.auth_file import access_token_expiry

_logger = logging.getLogger(__name__)

OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"

#: Public OAuth client of the Codex CLI. Verified against the live endpoint —
#: see the module docstring before changing it.
OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

OAUTH_SCOPE = "openid profile email offline_access"

#: Refresh this far before nominal expiry. A token that expires mid-request is
#: indistinguishable from a revoked one at the call site.
EXPIRY_MARGIN_S = 120.0

#: Server verdicts that will not improve by retrying. ``token_expired`` is this
#: endpoint's wording for a dead *refresh* token: the account needs a fresh
#: login, and retrying only wastes requests. Anything not listed here stays
#: transient — a misclassified transient costs one retry, a misclassified
#: permanent quarantines a live account.
PERMANENT_ERRORS = frozenset({"invalid_grant", "invalid_client", "token_expired"})


@dataclass(frozen=True)
class RefreshOutcome:
    """Result of one refresh attempt.

    ``kind`` is None on success. ``"transient"`` means try again later;
    everything else is a verdict about this account that will not improve by
    retrying.
    """

    payload: dict | None = None
    kind: str | None = None


def needs_refresh(payload: object, *, now: float | None = None) -> bool:
    """Whether this payload's access token is expired or about to be."""
    exp = access_token_expiry(payload)
    if exp is None:
        # No readable expiry: treat as needing refresh. A pointless refresh
        # costs one request; a skipped one costs a blank usage row.
        return True
    return exp - (now if now is not None else time.time()) <= EXPIRY_MARGIN_S


def _error_code(body: str) -> str | None:
    """Extract the server's error code from either body shape.

    This endpoint nests it (``{"error": {"code": ...}}``); RFC 6749 puts a bare
    string there. Undocumented endpoints change, so both are read.
    """
    try:
        err = json.loads(body).get("error")
    except (ValueError, AttributeError):
        return None
    if isinstance(err, str):
        return err
    if isinstance(err, dict):
        code = err.get("code") or err.get("type")
        return code if isinstance(code, str) else None
    return None


def try_refresh(payload: object, timeout_s: float = 10.0) -> RefreshOutcome:
    """Exchange a refresh token for a fresh access token.

    Returns the updated payload; persisting it is the caller's job, and the
    caller must do so *immediately and under the store lock* — a rotated
    refresh token that is never written down is an account lost.
    """
    if not isinstance(payload, dict):
        return RefreshOutcome(kind="transient")

    tokens = payload.get("tokens")
    if not isinstance(tokens, dict):
        if payload.get("auth_mode") == "apikey" or payload.get("OPENAI_API_KEY"):
            return RefreshOutcome(kind="not_applicable")
        return RefreshOutcome(kind="transient")

    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        return RefreshOutcome(kind="no_refresh_token")

    body = json.dumps(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": OAUTH_CLIENT_ID,
            "scope": OAUTH_SCOPE,
        }
    ).encode()

    try:
        req = urllib.request.Request(
            OAUTH_TOKEN_URL,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "claude-swap/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace") if hasattr(e, "read") else ""
        # Log the status and the server's code only. The request body carries a
        # refresh token and the response can echo request context; neither
        # belongs in a log file users paste into public issues.
        code = _error_code(raw)
        _logger.debug("Codex token refresh failed: http-%s (%s)", e.code, code)
        if e.code in (400, 401, 403) and code in PERMANENT_ERRORS:
            return RefreshOutcome(kind=code)
        return RefreshOutcome(kind="transient")
    except Exception as e:
        _logger.debug("Codex token refresh failed: %s", type(e).__name__)
        return RefreshOutcome(kind="transient")

    if not isinstance(data, dict) or not data.get("access_token"):
        # A 200 that carries no token is not a success; treating it as one would
        # persist a payload with a stale access token and a possibly-spent
        # refresh token.
        return RefreshOutcome(kind="transient")

    updated = dict(payload)
    new_tokens = dict(tokens)
    new_tokens["access_token"] = data["access_token"]
    if data.get("id_token"):
        new_tokens["id_token"] = data["id_token"]
    # Absent means "keep using the one you have" (RFC 6749 §6). Overwriting it
    # with None would destroy the account's only way back.
    if data.get("refresh_token"):
        new_tokens["refresh_token"] = data["refresh_token"]
    updated["tokens"] = new_tokens
    updated["last_refresh"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return RefreshOutcome(payload=updated)
