"""OAuth token management and usage API for Claude Code accounts."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone

OAUTH_BETA_HEADER = "oauth-2025-04-20"
OAUTH_EXPIRY_BUFFER_MS = 5 * 60 * 1000
OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"

_logger = logging.getLogger("claude-swap")


def extract_access_token(credentials: str) -> str | None:
    """Extract the OAuth access token from a credentials JSON string."""
    try:
        data = json.loads(credentials)
        return data.get("claudeAiOauth", {}).get("accessToken")
    except (json.JSONDecodeError, AttributeError):
        return None


def extract_oauth_data(credentials: str) -> dict | None:
    """Extract the Claude AI OAuth payload from a credentials JSON string."""
    try:
        data = json.loads(credentials)
    except json.JSONDecodeError:
        return None
    oauth = data.get("claudeAiOauth")
    return oauth if isinstance(oauth, dict) else None


def is_oauth_token_expired(expires_at: object) -> bool:
    """Return whether an OAuth token is expired or about to expire."""
    if not isinstance(expires_at, (int, float)):
        return False

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    return now_ms + OAUTH_EXPIRY_BUFFER_MS >= int(expires_at)


def refresh_oauth_credentials(credentials: str) -> str | None:
    """Refresh an OAuth access token via direct token endpoint POST."""
    try:
        data = json.loads(credentials)
        oauth = data.get("claudeAiOauth")
        if not isinstance(oauth, dict):
            return None

        refresh_token = oauth.get("refreshToken")
        if not refresh_token:
            return None

        scopes = oauth.get("scopes")
        if not isinstance(scopes, list) or not scopes:
            _logger.debug("OAuth refresh skipped: scopes missing")
            return None

        body = json.dumps({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": OAUTH_CLIENT_ID,
            "scope": " ".join(scopes),
        }).encode()

        req = urllib.request.Request(
            OAUTH_TOKEN_URL,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp_data = json.loads(resp.read().decode())

        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        oauth["accessToken"] = resp_data["access_token"]
        oauth["expiresAt"] = now_ms + resp_data["expires_in"] * 1000
        if resp_data.get("refresh_token"):
            oauth["refreshToken"] = resp_data["refresh_token"]
        if resp_data.get("scope"):
            oauth["scopes"] = resp_data["scope"].split()

        data["claudeAiOauth"] = oauth
        return json.dumps(data)
    except Exception as e:
        _logger.debug("OAuth refresh failed: %r", e)
        return None


def format_reset(resets_at: str) -> tuple[str, str]:
    """Return (countdown, clock) for a reset time in local time."""
    reset_utc = datetime.fromisoformat(resets_at)
    now = datetime.now(timezone.utc)
    remaining = reset_utc - now
    total_seconds = max(0, int(remaining.total_seconds()))
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60

    if days > 0:
        countdown = f"{days}d {hours}h"
    elif hours > 0:
        countdown = f"{hours}h {minutes}m"
    else:
        countdown = f"{minutes}m"

    reset_local = reset_utc.astimezone()
    now_local = now.astimezone()
    if reset_local.date() == now_local.date():
        time_str = reset_local.strftime("%H:%M")
    else:
        day = str(reset_local.day)
        time_str = reset_local.strftime(f"%b {day} %H:%M")

    return countdown, time_str


def request_usage_data(access_token: str) -> dict:
    """Request raw utilization data from the Anthropic usage API."""
    url = "https://api.anthropic.com/api/oauth/usage"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "anthropic-beta": OAUTH_BETA_HEADER,
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode())


def build_usage_result(data: dict) -> dict | None:
    """Normalize raw usage API data into the structure used by the CLI."""
    _logger.debug("Usage API response: %s", json.dumps(data, indent=2))

    result = {}

    h5 = data.get("five_hour")
    if h5:
        h5_entry = {"pct": h5["utilization"]}
        if h5.get("resets_at"):
            h5_entry["countdown"], h5_entry["clock"] = format_reset(h5["resets_at"])
        result["five_hour"] = h5_entry

    d7 = data.get("seven_day")
    if d7:
        d7_entry = {"pct": d7["utilization"]}
        if d7.get("resets_at"):
            d7_entry["countdown"], d7_entry["clock"] = format_reset(d7["resets_at"])
        result["seven_day"] = d7_entry

    return result if result else None


def fetch_usage(access_token: str) -> dict | None:
    """Fetch 5-hour and 7-day utilization from the Anthropic usage API."""
    try:
        data = request_usage_data(access_token)
        return build_usage_result(data)
    except Exception as e:
        _logger.debug("Usage fetch failed: %r", e)
        return None


def fetch_usage_for_account(
    account_num: str,
    email: str,
    credentials: str,
    is_active: bool,
    persist_credentials: Callable[[str, str, str], None] | None = None,
) -> dict | None:
    """Fetch usage for an account, refreshing expired OAuth tokens if needed.

    Active accounts are read-only -- no refresh is attempted because Claude
    Code keeps its own live credentials fresh.  Inactive accounts may be
    refreshed via a direct OAuth2 token-endpoint POST and the result is
    persisted to backup storage only.
    """
    oauth = extract_oauth_data(credentials)
    access_token = oauth.get("accessToken") if oauth else None
    if not access_token:
        return None

    # Active account: use token as-is, never refresh.
    if is_active:
        return fetch_usage(access_token)

    # Inactive account: proactively refresh if expired.
    working_credentials = credentials

    if oauth.get("refreshToken") and is_oauth_token_expired(oauth.get("expiresAt")):
        refreshed = refresh_oauth_credentials(working_credentials)
        if refreshed:
            working_credentials = refreshed
            _persist(persist_credentials, account_num, email, working_credentials)
            oauth = extract_oauth_data(working_credentials) or oauth
            access_token = oauth.get("accessToken") or access_token

    try:
        data = request_usage_data(access_token)
        return build_usage_result(data)
    except urllib.error.HTTPError as e:
        _logger.debug("Usage fetch failed: %r", e)
        if e.code != 401 or not oauth or not oauth.get("refreshToken"):
            return None

        # Retry once after refreshing on 401.
        refreshed = refresh_oauth_credentials(working_credentials)
        if not refreshed:
            return None

        working_credentials = refreshed
        _persist(persist_credentials, account_num, email, working_credentials)
        refreshed_oauth = extract_oauth_data(working_credentials)
        new_token = refreshed_oauth.get("accessToken") if refreshed_oauth else None
        if not new_token:
            return None

        try:
            data = request_usage_data(new_token)
            return build_usage_result(data)
        except Exception as retry_error:
            _logger.debug("Usage fetch failed after refresh: %r", retry_error)
            return None
    except Exception as e:
        _logger.debug("Usage fetch failed: %r", e)
        return None


def _persist(
    callback: Callable[[str, str, str], None] | None,
    account_num: str,
    email: str,
    credentials: str,
) -> None:
    """Call the persist callback, logging any errors."""
    if not callback:
        return
    try:
        callback(account_num, email, credentials)
    except Exception as e:
        _logger.debug(
            "Failed to persist refreshed credentials for account %s: %r",
            account_num,
            e,
        )
