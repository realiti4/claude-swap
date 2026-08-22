"""OAuth token management and usage API for Claude Code accounts."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
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


def credential_fingerprint(credentials: str) -> str | None:
    """Stable identity fingerprint for a stored credential.

    Refresh-token hash when one exists (survives access-token rotation, so two
    generations of the same OAuth lineage compare equal); full-content hash
    otherwise (API keys and setup-tokens rotate never, so content identity is
    lineage identity). None only for empty input — a caller comparing "did the
    credential change?" must never get None for real bytes, or every
    comparison against it would degenerate to "changed".
    """
    if not credentials:
        return None
    data = extract_oauth_data(credentials)
    token = data.get("refreshToken") if data else None
    if isinstance(token, str) and token:
        return "sha256:" + hashlib.sha256(token.encode()).hexdigest()
    return "sha256-full:" + hashlib.sha256(credentials.encode()).hexdigest()


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

    ``token_account`` is the account identity the token endpoint optionally
    includes alongside a successful grant (``{"uuid", "email",
    "organizationUuid"}``, fields possibly None) — a zero-request identity
    source for the credential that was just refreshed. None when the server
    omitted it or on failure; callers must treat it as opportunistic.
    """

    credentials: str | None
    error: str | None
    token_account: dict | None = None
    # Fingerprint of the generation actually consumed (POSTed). Set by the
    # consume gate, which may substitute a fresher re-read or a session
    # profile for the caller's snapshot — strike binding must follow the
    # POSTed bytes, not the snapshot.
    consumed_fp: str | None = None
    # Did the consumed successor actually reach the stash? Only meaningful on
    # a demoted (`transient` WITH credentials) outcome from the consume gate.
    # False there means the `consume-gate-unpersisted` corner: BOTH the
    # persist and the stash write failed, so the successor survives only in
    # `credentials` and retrying POSTs the spent predecessor. Callers that
    # tell the user what to do next must not promise a stash that never
    # happened.
    stashed: bool = False


def try_refresh_oauth_credentials(
    credentials: str, timeout_s: float = 10.0
) -> RefreshOutcome:
    """Refresh an OAuth access token via direct token endpoint POST.

    ``timeout_s`` bounds the network exchange. Callers that hold locks other
    processes contend for should pass a budget comfortably inside the
    contenders' acquire timeout (see ``_fetch_active_usage``).
    """
    # ``no_refresh_token`` is a PERMANENT verdict (it strikes at
    # AUTH_DEAD_STRIKES=1), so it demands a structurally complete OAuth dict
    # genuinely missing the field. An unparseable or non-dict blob is more
    # likely a torn/partial read than a real credential shape — transient:
    # the next pass re-reads and either succeeds or sees the true shape.
    try:
        data = json.loads(credentials)
    except json.JSONDecodeError:
        return RefreshOutcome(None, "transient")
    if not isinstance(data, dict):
        return RefreshOutcome(None, "transient")
    oauth = data.get("claudeAiOauth")
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
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            resp_data = json.loads(resp.read().decode())

        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        oauth["accessToken"] = resp_data["access_token"]
        oauth["expiresAt"] = now_ms + resp_data["expires_in"] * 1000
        if resp_data.get("refresh_token"):
            oauth["refreshToken"] = resp_data["refresh_token"]
        if resp_data.get("scope"):
            oauth["scopes"] = resp_data["scope"].split()

        data["claudeAiOauth"] = oauth
        return RefreshOutcome(
            json.dumps(data), None, _parse_token_account(resp_data)
        )
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace") if hasattr(e, "read") else ""
        _logger.debug("OAuth refresh failed: %r, body: %s", e, body[:500])
        # Permanent only when the server itself rejected the grant: a 4xx AND
        # an explicit marker in the body. Anything ambiguous stays transient —
        # a misclassified transient costs one retry, a misclassified permanent
        # would wrongly quarantine a live token.
        if e.code in (400, 401, 403):
            # RFC 6749 §5.2: the verdict is the top-level ``error`` member of
            # the JSON body. A substring scan misclassifies — the marker can
            # appear inside another envelope's detail text, and a dead-token
            # verdict at AUTH_DEAD_STRIKES=1 quarantines the slot on the
            # spot. Unparseable bodies stay transient (a misclassified
            # transient costs one retry; a misclassified permanent wrongly
            # quarantines a live token).
            try:
                err = json.loads(body).get("error")
            except (ValueError, AttributeError):
                err = None
            # invalid_grant: this slot's refresh lineage is dead.
            # invalid_client: OUR client credential was rejected — systemic
            # (client_id rotated/blocked), no evidence about any slot, so it
            # keeps its own kind and lands no strike.
            if err in ("invalid_grant", "invalid_client"):
                return RefreshOutcome(None, err)
        return RefreshOutcome(None, "transient")
    except Exception as e:
        _logger.debug("OAuth refresh failed: %r", e)
        return RefreshOutcome(None, "transient")


def _parse_token_account(resp_data: dict) -> dict | None:
    """Extract the optional account identity from a token-endpoint response.

    The refresh grant's response body may carry ``account`` / ``organization``
    objects naming who the rotated token belongs to (Claude Code surfaces the
    same fields as ``tokenAccount``). Absent in some responses — never rely on
    it, but never discard it either. Same strict boundary as
    ``fetch_oauth_profile``: usable identity requires a non-empty string
    ``account.uuid`` (consumers backfill and compare by uuid), optional
    fields are normalized to str-or-None, and anything malformed is None —
    this identity is opportunistic and must never break the refresh that
    carried it.
    """
    account = resp_data.get("account")
    if not isinstance(account, dict):
        return None
    uuid = account.get("uuid")
    if not isinstance(uuid, str) or not uuid.strip():
        return None
    email = account.get("email_address")
    organization = resp_data.get("organization")
    org_uuid = organization.get("uuid") if isinstance(organization, dict) else None
    return {
        "uuid": uuid.strip(),
        "email": email if isinstance(email, str) else None,
        "organizationUuid": org_uuid if isinstance(org_uuid, str) else None,
    }


def refresh_oauth_credentials(credentials: str) -> str | None:
    """Refresh an OAuth access token; None on any failure (see RefreshOutcome)."""
    return try_refresh_oauth_credentials(credentials).credentials


def fetch_oauth_profile(access_token: str) -> dict | None:
    """Resolve an OAuth access token to its account identity, or None.

    ``GET /api/oauth/profile`` answers the one question the credential bytes
    can't: *whose* token is this. Returns ``{"uuid", "email",
    "organizationUuid"}`` or None on any failure — callers treat None as
    "unresolvable", never as an error. The identity oracle is strictly
    advisory (a switch proceeds pre-fix on None), so the boundary is strict
    the other way: a response counts as resolved only when it carries a
    non-empty string ``account.uuid`` — a response without a usable uuid,
    including a schema change that renames it, is None, keeping that drift
    on the fail-open path rather than silently degrading switches.
    ``email``/``organizationUuid`` are optional (str-or-None); a uuid-only
    response *does* resolve, and classification decides whether such partial
    evidence is sufficient for each decision. Must not be called while any
    credential/config lock is held (network under locks is forbidden).
    """
    url = "https://api.anthropic.com/api/oauth/profile"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": "claude-swap/1.0",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(
            req, timeout=5, context=_pin_aware_ssl_context()
        ) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            # Evidence, not proof: the live access token can't authenticate.
            # A freshly rotated own-credential would carry a fresh token, but
            # this also fires benignly (family rotated, then the access token
            # expired on an idle machine). Log-file only — the caller falls
            # back to pre-fix behavior and the user sees nothing.
            _logger.warning(
                "OAuth profile returned 401 while resolving credential "
                "ownership; proceeding without identity (pre-fix behavior)."
            )
        else:
            _logger.debug("OAuth profile fetch failed: %r", e)
        return None
    except Exception as e:
        _logger.debug("OAuth profile fetch failed: %r", e)
        return None
    account = data.get("account") if isinstance(data, dict) else None
    if not isinstance(account, dict):
        _logger.debug("OAuth profile response missing account object")
        return None
    uuid = account.get("uuid")
    if not isinstance(uuid, str) or not uuid.strip():
        _logger.debug("OAuth profile response missing account.uuid")
        return None
    email = account.get("email")
    organization = data.get("organization")
    org_uuid = organization.get("uuid") if isinstance(organization, dict) else None
    return {
        "uuid": uuid.strip(),
        "email": email if isinstance(email, str) else None,
        "organizationUuid": org_uuid if isinstance(org_uuid, str) else None,
    }



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

    return countdown, reset_clock_string(reset_utc, now)


def reset_clock_string(reset_utc: datetime, now_utc: datetime) -> str:
    """Absolute reset time in local time: "20:39" same-day, else "Jul 5 08:59"."""
    reset_local = reset_utc.astimezone()
    now_local = now_utc.astimezone()
    if reset_local.date() == now_local.date():
        return reset_local.strftime("%H:%M")
    day = str(reset_local.day)
    return reset_local.strftime(f"%b {day} %H:%M")


def fresh_reset_strings(window: dict) -> tuple[str, str] | None:
    """``(countdown, clock)`` for one usage window, or None when unknown.

    Recomputed from ``resets_at`` at render time: the strings cached at fetch
    time drift as the measurement ages (a countdown frozen 2h ago overstates
    the remaining wait by those 2h, and a same-day "15:30" clock silently
    starts meaning yesterday). Entries persisted without ``resets_at`` fall
    back to the fetch-time strings — stale beats blank.
    """
    resets_at = window.get("resets_at")
    if resets_at:
        try:
            return format_reset(resets_at)
        except (ValueError, TypeError):
            pass  # unparseable cached value — fall back below
    if "clock" in window:
        return window.get("countdown", "?"), window["clock"]
    return None


def request_usage_data(access_token: str) -> dict:
    """Request raw utilization data from the Anthropic usage API."""
    url = "https://api.anthropic.com/api/oauth/usage"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "anthropic-beta": OAUTH_BETA_HEADER,
        "User-Agent": "claude-swap/1.0",
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(
        req, timeout=5, context=_pin_aware_ssl_context()
    ) as resp:
        return json.loads(resp.read().decode())


def _classify_usage_error(e: Exception) -> tuple[str, float | None]:
    """Map a usage-fetch exception to ``(kind, retry_after_s)``.

    ``kind`` is a short stable token for logs and backoff decisions
    (``"http-429"``, ``"timeout"``, ``"tls-cert"``, ``"network"``,
    ``"bad-response"``, or the exception type name as a fallback).
    ``retry_after_s`` is the parsed ``Retry-After`` header when the server sent
    one (seconds form only — the HTTP-date form is rare enough to ignore).
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
        # A TLS handshake the SERVER answered and we refused is not a
        # transport failure, and calling it one sends you to DNS while the
        # repair is a CA bundle. Measured 2026-08-17: a TLS-terminating proxy
        # presented a CA urllib does not trust, every poll raised
        # URLError(SSLCertVerificationError), all of it stored as "network",
        # and one account sat unpolled for ten days with that one word as the
        # whole record. The remedy is platform-dependent and lives in
        # ERROR_NOTES["tls-cert"], where the operator actually reads it.
        if isinstance(e.reason, ssl.SSLCertVerificationError):
            return "tls-cert", None
        return "network", None
    if isinstance(e, json.JSONDecodeError):
        return "bad-response", None
    return type(e).__name__, None


def _log_usage_failure(
    context: str, e: Exception, kind: str, retry_after_s: float | None = None
) -> None:
    """One WARNING line with the cause so it lands in the default log file
    (issue #85 was undiagnosable with failures swallowed at DEBUG); the full
    exception repr stays at DEBUG. The line is what users paste into public
    issues, so ``context`` must not carry the email, and the server's
    Retry-After rides along when present (it answers the backoff-tuning
    question without a second ask)."""
    where = f" {context}" if context else ""
    cause = kind if retry_after_s is None else f"{kind}, retry-after {retry_after_s:.0f}s"
    if kind == "http-429":
        # Whether the budget counts per access token or per account depends
        # on the org's 429 regime (both measured; see poll_policy), so the
        # message stays scope-neutral. Under the account-scoped regime
        # re-authenticating does not clear a block, and two machines holding
        # different tokens for one account still compete for one budget —
        # cumulative polling across surfaces and machines can saturate it,
        # and backoff plus the adaptive cadence are the recovery.
        cause += " (usage-endpoint budget reached; backing off)"
    _logger.warning("Usage fetch failed%s: %s", where, cause)
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


def relevant_windows(
    usage: dict | None, models: Sequence[str] = ()
) -> list[tuple[str, float, str | None]]:
    """Every ``(label, pct, resets_at)`` window that gates this account.

    Always the 5-hour ("5h") and 7-day ("7d") windows. When ``models`` is
    non-empty, each named per-model weekly ``scoped`` window is included too
    (matched case-insensitively on display name, e.g. "Fable"; the sentinel
    ``all`` matches every scoped window the account reports). The single
    canonical window source for decisions, scheduling, and reset math — so a
    window that binds a decision can never be invisible to the scheduler.
    ``spend`` (pay-as-you-go extra-usage credits) is a separate axis and is
    deliberately excluded. ``resets_at`` is the ISO string as fetched, or
    ``None`` when the API sent none.
    """
    if not isinstance(usage, dict):
        return []
    windows: list[tuple[str, float, str | None]] = []
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        window = usage.get(key)
        if isinstance(window, dict) and isinstance(window.get("pct"), (int, float)):
            windows.append((label, float(window["pct"]), window.get("resets_at")))
    if models:
        wanted = {m.lower() for m in models}
        match_all = "all" in wanted
        scoped = usage.get("scoped")
        if isinstance(scoped, list):
            for s in scoped:
                if (
                    isinstance(s, dict)
                    and isinstance(s.get("pct"), (int, float))
                    and isinstance(s.get("name"), str)
                    and (match_all or s["name"].lower() in wanted)
                ):
                    windows.append((s["name"], float(s["pct"]), s.get("resets_at")))
    return windows


def account_headroom(
    usage: dict | None, models: Sequence[str] = ()
) -> float | None:
    """Remaining percentage before this account hits a rate-limit window.

    Considers the 5-hour and 7-day utilization windows — the two that always
    gate requests. When ``models`` is non-empty, each named per-model weekly
    ``scoped`` window (see :func:`relevant_windows`) is folded in too: a model
    maxed at 100% blocks that model's work even with 5h/7d headroom, so for
    someone pinned to that model it binds just as hard. Returns the headroom
    of the *binding* window (``100 - max(pct)``), so ``<= 0`` means the
    account is at or over a limit. Returns ``None`` when usage is unavailable
    or carries no window data, which callers treat as "unknown" (never
    auto-skipped).
    """
    pcts = [pct for _, pct, _ in relevant_windows(usage, models)]
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
    # Fingerprint of the credential whose rt was POSTed when error is a
    # permanent auth kind — lets the store bind the strike to that
    # generation (see usage_store.FetchRecord.struck_fp).
    struck_fp: str | None = None


def fetch_usage(access_token: str) -> dict | None:
    """Fetch 5-hour and 7-day utilization from the Anthropic usage API."""
    try:
        data = request_usage_data(access_token)
        return build_usage_result(data)
    except Exception as e:
        kind, _ = _classify_usage_error(e)
        _log_usage_failure("", e, kind)
        return None


# Refresh failures that will not resolve by retrying THIS pass, so the caller
# must not fall through to the usage endpoint with the known-expired token.
# `consume-busy` belongs here for a reason the other two make obvious only in
# hindsight: the retry re-enters the same gate, finds it still held, and the
# distinct kind arrives as generic "refresh-failed" — hiding it, and spending a
# guaranteed 401 per pass to learn nothing.
_DETERMINISTIC_REFRESH_ERRORS = (
    "store-unmirrored", "invalid_client", "consume-busy", "stash-unreadable",
)


def try_fetch_usage_for_account(
    account_num: str,
    email: str,
    credentials: str,
    is_active: bool,
    persist_credentials: Callable[[str, str, str], None] | None = None,
    refresh_via: Callable[[str, str, str], RefreshOutcome] | None = None,
) -> UsageOutcome:
    """Fetch usage for an account, refreshing expired tokens for inactive accounts only.

    Active accounts are never refreshed — Claude Code owns those credentials.
    ``refresh_via(account_num, email, snapshot)`` supersedes the direct POST
    when given: the switcher passes its consume gate, which re-reads the
    freshest copy under the slot lock, persists via fingerprint CAS, and
    never consumes a superseded snapshot. ``persist_credentials`` is then
    unused for the refresh (the gate persists internally).
    """
    context = f"for account {account_num}"  # no email: paste-safe for public issues
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
        if refresh_via is not None:
            refresh = refresh_via(account_num, email, working_credentials)
        else:
            refresh = try_refresh_oauth_credentials(working_credentials)
        if refresh.credentials:
            working_credentials = refresh.credentials
            if refresh_via is None:
                _persist(persist_credentials, account_num, email, working_credentials)
            oauth = extract_oauth_data(working_credentials) or oauth
            access_token = oauth.get("accessToken") or access_token
        elif refresh.error in ("invalid_grant", "no_refresh_token"):
            # The refresh-token lineage is server-rejected (or structurally
            # absent) — permanently dead. Don't hit the usage endpoint with
            # a token we know is expired (that just adds a 401/429 to a lost
            # cause): report the permanent failure distinctly so the store
            # can quarantine the account. The strike binds to the bytes the
            # gate actually POSTed (it may have substituted a fresher
            # re-read for our snapshot) — fall back to the snapshot's
            # fingerprint only for the direct-POST path.
            return UsageOutcome(
                None, error=refresh.error,
                struck_fp=(
                    refresh.consumed_fp
                    or credential_fingerprint(working_credentials)
                ),
            )
        elif refresh.error in _DETERMINISTIC_REFRESH_ERRORS:
            # Deterministic refusals (M4 parity guard; a systemic client_id
            # rejection; another process holding the consume gate): hitting the
            # usage endpoint with the known-expired token would 401 every pass.
            # Surface the distinct kind instead — ERROR_NOTES renders the
            # remedy for each.
            return UsageOutcome(None, error=refresh.error)
        # A transient refresh failure falls through to try the (expired) token;
        # the 401 path below retries the refresh.

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
            _log_usage_failure(context, e, kind, retry_after)
            return UsageOutcome(None, error=kind, retry_after_s=retry_after)

        # Retry once after refreshing on 401 (inactive accounts only). A
        # server-rejected grant (invalid_grant) means this refresh-token lineage
        # is permanently dead — surface it distinctly (not the generic
        # "refresh-failed") so the store can quarantine instead of retrying a
        # dead token forever.
        if refresh_via is not None:
            refresh = refresh_via(account_num, email, working_credentials)
        else:
            refresh = try_refresh_oauth_credentials(working_credentials)
        if not refresh.credentials:
            _log_usage_failure(context, e, kind)
            dead = refresh.error in ("invalid_grant", "no_refresh_token")
            # Deterministic kinds keep their identity here too — collapsing
            # them to "refresh-failed" would hide the ERROR_NOTES remedy
            # exactly on the 401 path (a not-yet-locally-expired token the
            # server already rotated past).
            distinct = dead or refresh.error in _DETERMINISTIC_REFRESH_ERRORS
            return UsageOutcome(
                None,
                error=refresh.error if distinct else "refresh-failed",
                struck_fp=(
                    (refresh.consumed_fp
                     or credential_fingerprint(working_credentials))
                    if dead else None
                ),
            )

        working_credentials = refresh.credentials
        if refresh_via is None:
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
            _log_usage_failure(context + " after refresh", retry_error, kind, retry_after)
            return UsageOutcome(None, error=kind, retry_after_s=retry_after)
    except Exception as e:
        kind, retry_after = _classify_usage_error(e)
        _log_usage_failure(context, e, kind, retry_after)
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


_BRIDGE_SESSIONS_URL = "https://api.anthropic.com/v1/code/sessions"


_POLICY_LIMITS_URL = "https://api.anthropic.com/api/claude_code/policy_limits"


def fetch_policy_limits(access_token: str,
                        timeout_s: float = 10.0) -> dict | None:
    """The org-policy document the SERVER returns for this credential.

    Claude Code caches this at `<config home>/policy-limits.json` and reads it
    once per process into a session cache; every gate check — `/remote-control`
    among them — resolves against that copy. The fetch carries whichever
    account is ACTIVE and the file is machine-wide, so the answer has to be
    re-asked when the active account changes or one account's restrictions go
    on gating every session on the machine.

    ``None`` when it could not be asked, which the caller must keep distinct
    from an empty document: ABSENT IS DENIED on the reader's side
    (`Ms()` returns false for a gated capability when no document is present),
    so writing nothing is never the safe default.
    """
    req = urllib.request.Request(_POLICY_LIMITS_URL, headers={
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
        "anthropic-client-platform": "cli",
    })
    try:
        with urllib.request.urlopen(
            req, timeout=timeout_s, context=_pin_aware_ssl_context()
        ) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:  # noqa: BLE001 — a switch must not fail on this
        _logger.debug("policy_limits fetch failed: %r", e)
        return None
    return data if isinstance(data, dict) else None


# One entry per distinct CA state. Bounded by construction: a machine has one
# pin CA, so this holds one context, two across a regeneration.
# ONE SLOT, NOT A DICT. Keyed on the CA's path and mtime, so a regenerated CA
# inserts a new entry — and the old `SSLContext`, holding a full parsed copy of
# the system trust store (~130 roots), stayed referenced forever. In `cswap
# tui`, the menu-bar app or a long-running `cswap auto`, repeated re-pins grew
# it monotonically. The docstring's "bounded by construction: a machine has one
# pin CA" is true only until the CA is regenerated, which is exactly what a
# re-pin does.
#
# A single slot gives the same hit rate for the same reason the dict did — the
# key changes only when the CA does, and when it changes the old context is
# dead — with a ceiling of one.
_PIN_CTX_SLOT: "tuple | None" = None


def _pin_ca_fingerprint():
    """What the cached context was built FOR — path and mtime, or None.

    A REGENERATED CA MUST NOT KEEP THE OLD CONTEXT, which is the only way a
    cache here can be wrong: `cswap pin` can mint a new CA at the same path,
    and a context still trusting the old one fails every call afterwards with
    exactly the error this helper exists to prevent. mtime answers that
    without reading the file.

    None when there is no pin at all — a real key, not an error: it caches the
    plain default context for machines without the extra.
    """
    # THROUGH THE SEAM. `pin.ca_path_for_trust` already returns None for
    # "cannot ask", so the guard that used to live here is the seam's now.
    from claude_swap import pin as _pin

    ca = _pin.ca_path_for_trust()
    if not ca:
        return None
    # A CA THAT NAMES A PATH WE CANNOT STAT IS NOT THE SAME STATE AS NO PIN AT
    # ALL, and collapsing the two hands the no-pin caller a context built for a
    # pin. Distinct key, so each caches its own.
    try:
        stamp = os.stat(ca).st_mtime_ns
    except OSError:
        stamp = None
    return (str(ca), stamp)


def _pin_aware_ssl_context():
    """A verifying context that ALSO trusts the pin's CA, if there is one.

    NAMED FOR THE PROPERTY, NOT THE CALLER. It started as
    `_bridge_ssl_context` because the bridge calls were the first to need it,
    and this module has two more sites with the identical problem — profile
    and usage. A helper named after its first caller is one the next caller
    writes a second copy of.

    TOKEN REFRESH IS NOT ONE OF THEM, and an earlier draft of this docstring
    said it was, which is enough to make a reviewer file the missing `context=`
    as a bug. The helper is only needed where the pin RE-SIGNS the host: it
    MITMs `UPSTREAM_HOST` and blind-tunnels everything else, and
    `OAUTH_TOKEN_URL` is on a different host, so that call is verified against
    the real certificate by an ordinary default context. Adding ours there
    would buy nothing and reload the system CA store on every refresh. The
    relationship is pinned by a test rather than left here as prose, because
    it holds between two packages and the day the pin re-signs a second host
    it stops being true.

    These calls are plain urllib through whatever proxy the session was wired
    to. When that is the pin, it MITMs api.anthropic.com — so the default
    context cannot verify it, every call dies CERTIFICATE_VERIFY_FAILED, and
    `_list_bridge_sessions` swallows that to debug and returns None. That is
    how the cloud session names stayed wrong for hours with nothing in any log.

    ADD, NEVER REPLACE, and the difference is not cosmetic. `SSL_CERT_FILE`
    REPLACES OpenSSL's file, so it is safe only where the bundle subsumes the
    store it displaces. Measured per machine, by certificate SET:

        host-a     ambient 124  bundle 126  safe
        host-b      ambient 128  bundle 167  NOT — 27 missing
        host-c  ambient 128  bundle   2  NOT — 128 missing

    So the writer that sets it correctly refuses on both Macs, and the repair
    stays dead there. Loading our CA into a default context keeps every
    ambient root and needs no environment variable at all. Measured on
    host-c, same process and proxy:

        default ctx                 CERTIFICATE_VERIFY_FAILED
        default ctx + our CA added  HTTP 200

    NEVER RAISES and never returns None: with no pin installed, or an
    unreadable CA, the caller still gets an ordinary verifying context. An
    optional extra must not be able to break a call that worked without it.
    """
    # BUILT ONCE PER CA, NOT PER CALL. `create_default_context()` loads the
    # whole system trust store (~130 certificates) and `load_verify_locations`
    # parses ours on top — and this is on the polling path: once per account
    # per usage poll, plus every profile fetch, every bridge listing and every
    # rename. Nothing in the result changes unless the pin's CA file does, so
    # the cache is keyed on that file's path and mtime and a regenerated CA
    # invalidates it by itself.
    global _PIN_CTX_SLOT

    key = _pin_ca_fingerprint()
    if _PIN_CTX_SLOT is not None and _PIN_CTX_SLOT[0] == key:
        return _PIN_CTX_SLOT[1]

    ctx = ssl.create_default_context()
    from claude_swap import pin as _pin

    try:
        ca = _pin.ca_path_for_trust()
        if ca:
            ctx.load_verify_locations(cafile=str(ca))
    except Exception as e:  # noqa: BLE001 — a missing CA is not a failed call
        _logger.debug("bridge ssl context: pin CA not added: %r", e)
    _PIN_CTX_SLOT = (key, ctx)
    return ctx


def _bridge_headers(access_token: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
        "anthropic-client-platform": "cli",
    }


def _list_bridge_sessions(access_token: str) -> list[dict] | None:
    """The account's cloud sessions, or ``None`` when the call failed.

    NONE IS NOT AN EMPTY LISTING. A caller that reads a failed request as "no
    bridges" would go on to act on no evidence; every caller here has to be
    able to tell the two apart.
    """
    req = urllib.request.Request(
        f"{_BRIDGE_SESSIONS_URL}?limit=100", headers=_bridge_headers(access_token)
    )
    try:
        with urllib.request.urlopen(
                req, timeout=10, context=_pin_aware_ssl_context()) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:  # noqa: BLE001 — cosmetic repair, never fatal
        _logger.debug("bridge listing failed: %r", e)
        return None
    # SHAPED, NOT JUST PARSED, and this used to sit outside the try. A JSON
    # array or string body — an error envelope, a captive-portal page that
    # happens to parse — made `.get` raise AttributeError out of a function
    # documented to return None on failure. The caller then recorded
    # `raised:AttributeError` instead of `list-failed`, which collapses the
    # exact distinction the (renamed, outcome) pair exists to preserve and
    # files an ordinary transport fault under programming error.
    if not isinstance(data, dict):
        _logger.debug(
            "bridge listing returned %s, not an object", type(data).__name__)
        return None
    items = data.get("data") or data.get("sessions") or []
    return items if isinstance(items, list) else None


def _put_bridge_title(access_token: str, session_id: str, title: str) -> bool:
    """Rename one cloud session. PUT, not PATCH — measured, PATCH is 405."""
    req = urllib.request.Request(
        f"{_BRIDGE_SESSIONS_URL}/{session_id}",
        data=json.dumps({"title": title}).encode("utf-8"),
        headers=_bridge_headers(access_token),
        method="PUT",
    )
    try:
        with urllib.request.urlopen(
                req, timeout=10, context=_pin_aware_ssl_context()):
            return True
    except Exception as e:  # noqa: BLE001
        _logger.debug("bridge rename failed for %s: %r", session_id, e)
        return False


#: How long the bridge-title repair may spend on PUTs in ONE pass. It runs
#: inside `AutoSwitchEngine.tick()`, the loop body that decides when to switch
#: before a rate-limit lockout, and each PUT carries its own 10s timeout with
#: no cap on how many there are. Smaller than one tick's usual cost on purpose:
#: a repair that delays the switch is worse than a repair that finishes next
#: pass, and the cadence is 300s so there always is a next pass.
#:
#: THE CEILING IS THIS PLUS ONE TIMEOUT, not this. The deadline is tested
#: BEFORE each PUT, so a PUT begun just under it still gets its full 10s and
#: the worst case is 30s. Left that way deliberately: refusing to start a PUT
#: that might overrun would idle the last third of every pass, and shortening
#: the timeout to the remaining budget would report a starved PUT as a refusal
#: and turn `all-puts-refused` into a lie about the server.
_BRIDGE_TITLE_BUDGET_S = 20.0


def restore_bridge_titles(access_token: str, names: dict) -> "tuple[int, str]":
    """Put each live session's own name back on its cloud bridge.

    WHY THIS EXISTS HERE AND NOT IN THE PROXY. cswap-pin already implements
    this, and it has exactly one caller: `_sweep_bridges_after_connect`, which
    runs when a `POST /v1/code/sessions` REACHES the proxy. So the repair is
    triggered by the very thing it exists to survive. Measured 2026-08-17: all
    24 claude processes carried `HTTPS_PROXY=127.0.0.1:9901` because Claude
    Code reads the `~/.claude.json` env block once at boot and the pin wiring
    landed after they exec'd. Nothing reached the proxy, nothing was restored,
    and a session sat under `Fix Claude AI session naming issue` until it was
    renamed by hand.

    THE POLICY STAYS IN cswap-pin. `titles_to_restore` decides what to touch —
    only a listed bridge whose title the server invented, never one a human
    typed — and duplicating that judgement here is how the two copies drift.
    This module contributes the transport it already has, nothing more.

    Returns ``(renamed, outcome)``. THE SECOND VALUE EXISTS BECAUSE THE FIRST
    CANNOT TELL THE CASES APART: five distinct states all produced 0 — the
    extra missing, the listing failing, the listing empty, nothing needing a
    rename, every PUT refused — and the caller only spoke when it was
    non-zero. Measured 2026-08-17: this ran ~20 times over 107 minutes with
    every listing dying on CERTIFICATE_VERIFY_FAILED, and nothing anywhere
    said so; the user found their cloud session names wrong before any log
    did.

    NEVER RAISES: the caller is `AutoSwitchEngine.tick()`, which is documented
    "Never raises", and a cosmetic repair must not be able to end a tick that
    was about to prevent a rate-limit lockout.
    """
    from claude_swap import pin as _pin

    if not _pin.is_available():
        return 0, "no-extra"
    try:
        sessions = _list_bridge_sessions(access_token)
        if sessions is None:
            # COULD NOT ASK. Distinct from an empty listing: this is the state
            # that persisted for hours unnoticed, and it is the one worth
            # waking someone for.
            return 0, "list-failed"
        if not sessions:
            return 0, "no-bridges"
        wanted = _pin.titles_to_restore(sessions, names)
        if wanted is None:
            # The extra went away between the check above and here, or the
            # package raised. Same outcome as never having it: nothing to do.
            return 0, "no-extra"
        # THE ZERO-OUTCOME IS DECIDED BEFORE THE LOOP IT PRECEDES. This sat
        # BELOW the loop, so the empty case walked a body that cannot execute
        # before being detected and a reader had to prove the loop was a no-op
        # to see the branch was reachable.
        if not wanted:
            return 0, "nothing-to-rename"
        done = 0
        # A DEADLINE, BECAUSE THIS RUNS INSIDE `tick()`. Each PUT carries a 10s
        # timeout and `wanted` is unbounded, so 100 stale titles against a
        # black-holing endpoint block the switch engine for ~1000s — on the
        # very tick that was about to prevent a rate-limit lockout, which is
        # the outcome moving this call into `finally` was meant to avoid. The
        # cadence gate does not help: `_bridge_titles_next_at` is advanced
        # before the work.
        #
        # LEFTOVERS ARE NOT LOST. This is a repair on a 300s cadence, so a pass
        # that runs out of budget renames what it reached and the next one
        # picks up the rest; the outcome says so rather than reporting a clean
        # `renamed`.
        deadline = time.monotonic() + _BRIDGE_TITLE_BUDGET_S
        for sid, want in wanted:
            if time.monotonic() >= deadline:
                return done, f"partial-{done}-of-{len(wanted)}"
            if _put_bridge_title(access_token, sid, want):
                done += 1
                _logger.info("restored the cloud title for %s to %r", sid, want)
        if not done:
            # Listed fine, had work, renamed none: every PUT was refused. A
            # different fault from every other zero here.
            return 0, "all-puts-refused"
        return done, "renamed"
    except Exception as e:  # noqa: BLE001 — see the docstring
        _logger.debug("bridge title restore failed: %r", e)
        return 0, f"raised:{type(e).__name__}"
