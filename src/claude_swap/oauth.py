"""OAuth token management and usage API for Claude Code accounts."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from claude_swap.printer import warning as print_warning

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


@dataclass(frozen=True)
class RefreshOutcome:
    """Result of a refresh-token grant attempt.

    ``credentials`` is the full rotated credentials JSON on success, else None.
    ``error`` classifies failures so callers can distinguish a dead refresh-token
    lineage (permanent: quarantine, stop retrying) from a network blip
    (transient: retry later):

    - ``None`` — success (``credentials`` is set)
    - ``"invalid_grant"`` — the token endpoint rejected the grant; this refresh
      token is dead and re-login is required
    - ``"no_refresh_token"`` — the stored credential carries no usable refresh
      token (also permanent for retry purposes)
    - ``"transient"`` — network/server error; the token may still be valid
    """

    credentials: str | None
    error: str | None


def try_refresh_oauth_credentials(credentials: str) -> RefreshOutcome:
    """Refresh an OAuth access token via direct token endpoint POST."""
    try:
        data = json.loads(credentials)
    except json.JSONDecodeError:
        return RefreshOutcome(None, "no_refresh_token")
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict) or not oauth.get("refreshToken"):
        return RefreshOutcome(None, "no_refresh_token")

    try:
        body = json.dumps({
            "grant_type": "refresh_token",
            "refresh_token": oauth["refreshToken"],
            "client_id": OAUTH_CLIENT_ID,
        }).encode()

        req = urllib.request.Request(
            OAUTH_TOKEN_URL,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "claude-swap/1.0",
            },
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
        return RefreshOutcome(json.dumps(data), None)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace") if hasattr(e, "read") else ""
        _logger.debug("OAuth refresh failed: %r, body: %s", e, body[:500])
        # Permanent only when the server itself rejected the grant: a 4xx AND
        # an explicit marker in the body. Anything ambiguous stays transient —
        # a misclassified transient costs one retry, a misclassified permanent
        # would wrongly quarantine a live token.
        if e.code in (400, 401, 403) and (
            "invalid_grant" in body or "invalid_client" in body
        ):
            return RefreshOutcome(None, "invalid_grant")
        return RefreshOutcome(None, "transient")
    except Exception as e:
        _logger.debug("OAuth refresh failed: %r", e)
        return RefreshOutcome(None, "transient")


def refresh_oauth_credentials(credentials: str) -> str | None:
    """Refresh an OAuth access token; None on any failure (see RefreshOutcome)."""
    return try_refresh_oauth_credentials(credentials).credentials



def build_token_status(credentials: str) -> str | None:
    """Return a short debug summary of stored OAuth token state."""
    oauth = extract_oauth_data(credentials)
    if not oauth:
        return None

    has_refresh_token = bool(oauth.get("refreshToken"))
    expires_at = oauth.get("expiresAt")
    refresh_str = "yes" if has_refresh_token else "no"

    if not isinstance(expires_at, (int, float)):
        return f"oauth: unknown expiry, refresh token {refresh_str}"

    expires_utc = datetime.fromtimestamp(expires_at / 1000, tz=timezone.utc)
    state = "expired" if is_oauth_token_expired(expires_at) else "fresh"
    countdown, clock = format_reset(expires_utc.isoformat())
    return f"oauth: {state}, refresh token {refresh_str}, expires {clock} in {countdown}"


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
        "User-Agent": "claude-swap/1.0",
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode())


def _classify_usage_error(e: Exception) -> tuple[str, float | None]:
    """Map a usage-fetch exception to ``(kind, retry_after_s)``.

    ``kind`` is a short stable token for logs and backoff decisions
    (``"http-429"``, ``"timeout"``, ``"network"``, ``"bad-response"``, or the
    exception type name as a fallback). ``retry_after_s`` is the parsed
    ``Retry-After`` header when the server sent one (seconds form only — the
    HTTP-date form is rare enough to ignore).
    """
    if isinstance(e, urllib.error.HTTPError):
        retry_after = None
        raw = e.headers.get("Retry-After") if e.headers else None
        if raw:
            try:
                retry_after = max(0.0, float(raw.strip()))
            except ValueError:
                pass
        return f"http-{e.code}", retry_after
    if isinstance(e, TimeoutError):  # socket.timeout is an alias since 3.10
        return "timeout", None
    if isinstance(e, urllib.error.URLError):
        if isinstance(e.reason, TimeoutError):
            return "timeout", None
        return "network", None
    if isinstance(e, json.JSONDecodeError):
        return "bad-response", None
    return type(e).__name__, None


def _log_usage_failure(context: str, e: Exception, kind: str) -> None:
    """One WARNING line with the cause so it lands in the default log file
    (issue #85 was undiagnosable with failures swallowed at DEBUG); the full
    exception repr stays at DEBUG."""
    where = f" {context}" if context else ""
    _logger.warning("Usage fetch failed%s: %s", where, kind)
    _logger.debug("Usage fetch failure detail%s: %r", where, e)



def build_usage_result(data: dict) -> dict | None:
    """Normalize raw usage API data into the structure used by the CLI."""
    _logger.debug("Usage API response: %s", json.dumps(data, indent=2))

    result = {}

    h5 = data.get("five_hour")
    if h5:
        h5_entry = {"pct": h5["utilization"]}
        if h5.get("resets_at"):
            h5_entry["resets_at"] = h5["resets_at"]
            h5_entry["countdown"], h5_entry["clock"] = format_reset(h5["resets_at"])
        result["five_hour"] = h5_entry

    d7 = data.get("seven_day")
    if d7:
        d7_entry = {"pct": d7["utilization"]}
        if d7.get("resets_at"):
            d7_entry["resets_at"] = d7["resets_at"]
            d7_entry["countdown"], d7_entry["clock"] = format_reset(d7["resets_at"])
        result["seven_day"] = d7_entry

    eu = data.get("extra_usage")
    if eu and eu.get("is_enabled"):
        # Claude Code returns nullable used_credits, monthly_limit, and utilization
        # (monthly_limit=None = unlimited). All three are needed to render the spend
        # line, so when any is null skip just the spend entry; five_hour/seven_day
        # go through unchanged.
        used_credits = eu.get("used_credits")
        monthly_limit = eu.get("monthly_limit")
        utilization = eu.get("utilization")
        if used_credits is not None and monthly_limit is not None and utilization is not None:
            try:
                spend_entry: dict = {
                    "used": float(used_credits) / 100,
                    "limit": float(monthly_limit) / 100,
                    "pct": float(utilization),
                    "currency": eu.get("currency", "USD"),
                }
                if eu.get("resets_at"):
                    spend_entry["resets_at"] = eu["resets_at"]
                    spend_entry["countdown"], spend_entry["clock"] = format_reset(eu["resets_at"])
                result["spend"] = spend_entry
            except (TypeError, ValueError) as e:
                _logger.debug("extra_usage parse failed: %r", e)

    # Per-model weekly limits live in the newer ``limits`` array as
    # ``weekly_scoped`` entries carrying a ``scope.model.display_name`` (e.g.
    # "Fable"). The legacy five_hour/seven_day keys above never expose these, so
    # surface each scoped window separately. Absent/older responses (no
    # ``limits``) simply yield no ``scoped`` key.
    limits = data.get("limits")
    if isinstance(limits, list):
        scoped: list[dict] = []
        for lim in limits:
            if not isinstance(lim, dict):
                continue
            scope = lim.get("scope")
            model = scope.get("model") if isinstance(scope, dict) else None
            name = model.get("display_name") if isinstance(model, dict) else None
            pct = lim.get("percent")
            if not name or not isinstance(pct, (int, float)):
                continue
            scoped_entry: dict = {"name": name, "pct": float(pct)}
            if lim.get("resets_at"):
                scoped_entry["resets_at"] = lim["resets_at"]
                scoped_entry["countdown"], scoped_entry["clock"] = format_reset(lim["resets_at"])
            scoped.append(scoped_entry)
        if scoped:
            result["scoped"] = scoped

    return result if result else None


def account_headroom(usage: dict | None) -> float | None:
    """Remaining percentage before this account hits a rate-limit window.

    Considers only the 5-hour and 7-day utilization windows — the two that
    actually gate requests. ``spend`` (pay-as-you-go extra-usage credits) is a
    separate axis and is deliberately ignored. Returns the headroom of the
    *binding* window (``100 - max(pct)``), so ``<= 0`` means the account is at
    or over a limit. Returns ``None`` when usage is unavailable or carries no
    window data, which callers treat as "unknown" (never auto-skipped).
    """
    if not isinstance(usage, dict):
        return None
    pcts = [
        window["pct"]
        for window in (usage.get("five_hour"), usage.get("seven_day"))
        if isinstance(window, dict) and isinstance(window.get("pct"), (int, float))
    ]
    if not pcts:
        return None
    return 100.0 - max(pcts)


@dataclass(frozen=True)
class UsageOutcome:
    """Result of a usage-API fetch attempt.

    ``usage`` is the normalized usage dict on success (it can also be ``None``
    on a successful round trip whose response carried no window data).
    ``error`` is ``None`` on success, else a ``_classify_usage_error`` kind
    (plus ``"no-access-token"`` / ``"refresh-failed"`` for pre-request
    failures). ``retry_after_s`` carries the server's Retry-After when sent.
    """

    usage: dict | None
    error: str | None = None
    retry_after_s: float | None = None


def fetch_usage(access_token: str) -> dict | None:
    """Fetch 5-hour and 7-day utilization from the Anthropic usage API."""
    try:
        data = request_usage_data(access_token)
        return build_usage_result(data)
    except Exception as e:
        kind, _ = _classify_usage_error(e)
        _log_usage_failure("", e, kind)
        return None


def try_fetch_usage_for_account(
    account_num: str,
    email: str,
    credentials: str,
    is_active: bool,
    persist_credentials: Callable[[str, str, str], None] | None = None,
) -> UsageOutcome:
    """Fetch usage for an account, refreshing expired tokens for inactive accounts only.

    Active accounts are never refreshed — Claude Code owns those credentials.
    """
    context = f"for account {account_num} ({email})"
    oauth = extract_oauth_data(credentials)
    access_token = oauth.get("accessToken") if oauth else None
    if not access_token:
        return UsageOutcome(None, error="no-access-token")

    working_credentials = credentials

    if (
        not is_active
        and oauth.get("refreshToken")
        and is_oauth_token_expired(oauth.get("expiresAt"))
    ):
        refreshed = refresh_oauth_credentials(working_credentials)
        if refreshed:
            working_credentials = refreshed
            _persist(persist_credentials, account_num, email, working_credentials)
            oauth = extract_oauth_data(working_credentials) or oauth
            access_token = oauth.get("accessToken") or access_token

    try:
        data = request_usage_data(access_token)
        return UsageOutcome(build_usage_result(data))
    except urllib.error.HTTPError as e:
        kind, retry_after = _classify_usage_error(e)
        if (
            e.code != 401
            or is_active
            or not oauth
            or not oauth.get("refreshToken")
        ):
            _log_usage_failure(context, e, kind)
            return UsageOutcome(None, error=kind, retry_after_s=retry_after)

        # Retry once after refreshing on 401 (inactive accounts only).
        refreshed = refresh_oauth_credentials(working_credentials)
        if not refreshed:
            _log_usage_failure(context, e, kind)
            return UsageOutcome(None, error="refresh-failed")

        working_credentials = refreshed
        _persist(persist_credentials, account_num, email, working_credentials)
        refreshed_oauth = extract_oauth_data(working_credentials)
        new_token = refreshed_oauth.get("accessToken") if refreshed_oauth else None
        if not new_token:
            return UsageOutcome(None, error="refresh-failed")

        try:
            data = request_usage_data(new_token)
            return UsageOutcome(build_usage_result(data))
        except Exception as retry_error:
            kind, retry_after = _classify_usage_error(retry_error)
            _log_usage_failure(context + " after refresh", retry_error, kind)
            return UsageOutcome(None, error=kind, retry_after_s=retry_after)
    except Exception as e:
        kind, retry_after = _classify_usage_error(e)
        _log_usage_failure(context, e, kind)
        return UsageOutcome(None, error=kind, retry_after_s=retry_after)


def fetch_usage_for_account(
    account_num: str,
    email: str,
    credentials: str,
    is_active: bool,
    persist_credentials: Callable[[str, str, str], None] | None = None,
) -> dict | None:
    """Usage dict or None (see try_fetch_usage_for_account for the cause)."""
    return try_fetch_usage_for_account(
        account_num, email, credentials, is_active, persist_credentials
    ).usage


def _persist(
    callback: Callable[[str, str, str], None] | None,
    account_num: str,
    email: str,
    credentials: str,
) -> None:
    """Call the persist callback, warning loudly on failure."""
    if not callback:
        return
    try:
        callback(account_num, email, credentials)
    except Exception as e:
        _logger.warning(
            "Refreshed OAuth token for account %s (%s) but failed to persist it: %r. "
            "The refresh token on disk may now be stale; if the next refresh fails "
            "with invalid_grant, re-run `cswap --add-account` after logging in.",
            account_num,
            email,
            e,
        )
        print_warning(
            f"Warning: failed to save refreshed token for account {account_num} ({email}). "
            f"If the next refresh fails, re-run `cswap --add-account` after logging in."
        )
