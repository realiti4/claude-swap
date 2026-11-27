"""Codex (OpenAI ChatGPT login) rate-limit usage.

Reads what Codex's ``/status`` shows: the same GET the Codex CLI makes
(openai/codex ``codex-rs/backend-client/src/client/rate_limit_resets.rs``,
``{base}/wham/usage`` with base ``https://chatgpt.com/backend-api``).
TLS trust comes from ``cli._use_native_tls`` (truststore), as in ``oauth``.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
# Codex's own token refresh (codex-rs login/src/auth/manager.rs).
REFRESH_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

_logger = logging.getLogger("claude-swap")


class Unauthorized(Exception):
    """The usage endpoint rejected the access token (HTTP 401)."""


class Window(NamedTuple):
    label: str  # "5h", "weekly"
    used_percent: float
    resets_at: str | None  # ISO 8601 UTC, the shape oauth.format_reset reads


def _label(seconds: int) -> str:
    named = {18000: "5h", 604800: "weekly"}.get(seconds)
    if named:
        return named
    return f"{seconds // 86400}d" if seconds % 86400 == 0 else f"{-(-seconds // 3600)}h"


def fetch_usage(access_token: str, account_id: str | None, timeout: float = 10) -> list[Window]:
    """Rate-limit windows for a ChatGPT login; [] when unknown.

    Raises only :class:`Unauthorized`, on a 401: the token is expired or
    revoked, which a refresh may fix and a retry never will.

    The window's length, not its slot, names it: a plan with only a weekly
    limit reports it as ``primary_window`` with ``secondary_window: null``.
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "User-Agent": "codex_cli_rs/0.167.0",
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    try:
        req = urllib.request.Request(USAGE_URL, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            rate_limit = json.loads(resp.read().decode())["rate_limit"]
        windows = []
        for key in ("primary_window", "secondary_window"):
            w = rate_limit.get(key)
            if not w:
                continue
            if w.get("reset_at"):  # epoch seconds
                reset = datetime.fromtimestamp(w["reset_at"], timezone.utc)
            elif w.get("reset_after_seconds") is not None:
                reset = datetime.now(timezone.utc) + timedelta(seconds=w["reset_after_seconds"])
            else:
                reset = None
            windows.append(Window(
                _label(int(w["limit_window_seconds"])),
                float(w["used_percent"]),
                reset.isoformat() if reset else None,
            ))
        return windows
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise Unauthorized from None
        _logger.debug("Codex usage fetch failed: HTTP %s", e.code)
        return []
    except Exception as e:
        _logger.debug("Codex usage fetch failed: %r", e)
        return []


def refresh_tokens(refresh_token: str, timeout: float = 10) -> dict | None:
    """New ``{id_token, access_token, refresh_token}`` from a refresh token; None on failure.

    The refresh token rotates: the caller must save what this returns, or the
    login is lost. Never raises.
    """
    body = json.dumps({
        "client_id": CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": "openid profile email",
    }).encode()
    req = urllib.request.Request(
        REFRESH_URL, data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "codex_cli_rs/0.167.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        fresh = {k: data[k] for k in ("id_token", "access_token", "refresh_token") if data.get(k)}
        return fresh if "access_token" in fresh else None
    except urllib.error.HTTPError as e:
        _logger.debug("Codex token refresh failed: HTTP %s", e.code)
        return None
    except Exception as e:
        _logger.debug("Codex token refresh failed: %r", e)
        return None
