"""Credential, refresh and usage helpers for OpenAI Codex CLI accounts.

A Codex credential is the text of ``$CODEX_HOME/auth.json``::

    {"auth_mode": "chatgpt", "OPENAI_API_KEY": null,
     "tokens": {"id_token", "access_token", "refresh_token", "account_id"},
     "last_refresh": "<RFC 3339, Z>"}

or an API-key login ``{"auth_mode": "apikey", "OPENAI_API_KEY": "sk-…"}``.
``oauth.py``'s shape helpers recognize these blobs (``is_codex_blob``) and
delegate here, so every caller that reads ``accessToken``/``refreshToken``/
``expiresAt`` off ``oauth.extract_oauth_data`` works unchanged on Codex
credentials through ``oauth_view``.

Identity is read offline from the id_token JWT (no signature check — the
token came from our own store, and nothing here is an authorization
decision). Behavior is cited against the Codex source (tag rust-v0.160.1).

This module must not import ``oauth`` at module level: ``oauth`` imports this
module for its dispatch, so the two names it needs (``RefreshOutcome``,
``format_reset``) are imported inside the functions that use them.
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from claude_swap import __version__

if TYPE_CHECKING:
    from claude_swap.oauth import RefreshOutcome

CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_DEFAULT_BASE_URL = "https://chatgpt.com/backend-api"
WORKSPACE_PLANS = frozenset({"team", "business", "enterprise", "edu", "education"})

_AUTH_CLAIM = "https://api.openai.com/auth"
_PROFILE_CLAIM = "https://api.openai.com/profile"
_USER_AGENT = f"claude-swap/{__version__}"
# Codex treats these refresh-endpoint codes as permanent (the lineage is dead:
# expired, already used, or revoked) — login/src/auth/manager.rs:1650-1707.
_DEAD_REFRESH_CODES = frozenset({
    "refresh_token_expired", "refresh_token_reused",
    "refresh_token_invalidated", "invalid_grant",
})

_logger = logging.getLogger("claude-swap")


def is_codex_blob(data: object) -> bool:
    """Whether parsed credential JSON is Codex-shaped. A ``claudeAiOauth``
    key always means Claude, whatever else the dict carries."""
    return (
        isinstance(data, dict)
        and "claudeAiOauth" not in data
        and any(k in data for k in ("tokens", "OPENAI_API_KEY", "auth_mode"))
    )


def parse_blob(creds: str) -> dict | None:
    """The Codex credential dict, or None when ``creds`` is not one."""
    try:
        data = json.loads(creds)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if is_codex_blob(data) else None


def decode_jwt_payload(token: str) -> dict | None:
    """A JWT's payload claims, unverified; None on anything malformed.

    Mirrors Codex's decoder (login/src/token_data.rs:129-140): exactly three
    non-empty dot-separated segments, payload is base64url JSON. The
    padding Codex's tokens omit is restored before decoding.
    """
    if not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        return None
    payload = parts[1]
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except ValueError:  # binascii.Error, UnicodeDecodeError, JSONDecodeError
        return None
    return claims if isinstance(claims, dict) else None


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _is_api_key(data: dict) -> bool:
    # auth_mode decides when present (a browser login can also store an
    # exchanged OPENAI_API_KEY next to its tokens); without it Codex falls
    # back to "a non-null key means API-key mode" (auth/manager.rs:1763-1780).
    mode = data.get("auth_mode")
    if mode is not None:
        return mode == "apikey"
    return bool(data.get("OPENAI_API_KEY"))


def is_api_key_blob(creds: str) -> bool:
    data = parse_blob(creds)
    return data is not None and _is_api_key(data)


def make_api_key_blob(key: str) -> str:
    # "apikey": serde rename_all="lowercase" of AuthMode::ApiKey
    # (protocol/src/auth.rs:7-11).
    return json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": key})


def chatgpt_tokens(creds: str) -> dict | None:
    """``tokens`` of a ChatGPT-mode blob; None for API-key or broken blobs."""
    data = parse_blob(creds)
    if data is None or _is_api_key(data):
        return None
    tokens = data.get("tokens")
    return tokens if isinstance(tokens, dict) else None


def _account_id(tokens: dict, auth: dict) -> str | None:
    # The stored account_id is what Codex itself keys refresh/reload on
    # (auth/manager.rs:614-626); the claim covers blobs written without it.
    return _str(tokens.get("account_id")) or _str(auth.get("chatgpt_account_id"))


def auth_claims(claims: dict | None) -> dict:
    auth = (claims or {}).get(_AUTH_CLAIM)
    return auth if isinstance(auth, dict) else {}


def claimed_account_id(id_token: object) -> str | None:
    """The id_token's ``chatgpt_account_id`` claim: the account its tokens
    belong to (the stored ``tokens.account_id`` can disagree in a hybrid
    file; codex-spec gotcha 5)."""
    return _str(auth_claims(decode_jwt_payload(id_token)).get("chatgpt_account_id"))


def identity(creds: str) -> dict | None:
    """``{"email", "uuid", "organizationUuid", "planType"}`` from the
    id_token, fields str-or-None; None for API-key or malformed blobs.

    ``organizationUuid`` is the ChatGPT account (workspace) id, so a personal
    account and a workspace membership of the same email stay distinct.
    """
    tokens = chatgpt_tokens(creds)
    claims = decode_jwt_payload(tokens.get("id_token")) if tokens else None
    if claims is None:
        return None
    auth = auth_claims(claims)
    profile = claims.get(_PROFILE_CLAIM)
    profile_email = profile.get("email") if isinstance(profile, dict) else None
    return {
        "email": _str(claims.get("email")) or _str(profile_email),
        "uuid": _str(auth.get("chatgpt_user_id")) or _str(auth.get("user_id")),
        "organizationUuid": _account_id(tokens, auth),
        "planType": _str(auth.get("chatgpt_plan_type")),
    }


def token_identity(creds: str) -> dict | None:
    """``{"uuid", "email", "organizationUuid"}`` of the account the tokens
    belong to, for lineage attribution: unlike :func:`identity`, the account
    comes from the id_token's own claim, so a hybrid (B's account_id over A's
    tokens) is attributed to A. None for API-key or malformed blobs."""
    ident = identity(creds)
    if ident is None:
        return None
    claim = claimed_account_id((chatgpt_tokens(creds) or {}).get("id_token"))
    return {
        "uuid": ident["uuid"],
        "email": ident["email"],
        "organizationUuid": claim or ident["organizationUuid"],
    }


def forced_login_violation(creds: str, config: dict) -> str | None:
    """Why Codex would log ``creds`` out at startup under ``config``, or None.

    Mirrors ``enforce_login_restrictions`` (login/src/auth/manager.rs:
    1315-1440), which DELETES a non-matching login: ``forced_login_method``
    must match the login's kind; ``forced_chatgpt_workspace_id`` (a string or
    a list, blank entries ignored) binds ChatGPT logins only, compared with the
    id_token's ``chatgpt_account_id``.
    """
    is_api_key = is_api_key_blob(creds)
    method = config.get("forced_login_method")
    if method == "api" and not is_api_key:
        return 'forced_login_method = "api" but this is a ChatGPT login'
    if method == "chatgpt" and is_api_key:
        return 'forced_login_method = "chatgpt" but this is an API-key login'
    allowed = config.get("forced_chatgpt_workspace_id")
    if isinstance(allowed, str):
        allowed = [allowed]
    if not isinstance(allowed, list) or is_api_key:
        return None
    allowed = [w.strip() for w in allowed if isinstance(w, str) and w.strip()]
    if not allowed:
        return None
    tokens = chatgpt_tokens(creds) or {}
    workspace = claimed_account_id(tokens.get("id_token"))
    if workspace in allowed:
        return None
    return (
        f"forced_chatgpt_workspace_id allows {', '.join(allowed)} but this "
        f"login belongs to {workspace or 'no workspace'}"
    )


def oauth_view(creds: str) -> dict | None:
    """The Claude-shaped OAuth view ``oauth.extract_oauth_data`` returns for
    Codex blobs: ``{"accessToken", "refreshToken", "expiresAt", "idToken",
    "accountId"}``. ``expiresAt`` is epoch ms from the access token's
    ``exp`` (None when undecodable — "unknown", never "expired")."""
    tokens = chatgpt_tokens(creds)
    if tokens is None:
        return None
    access = _str(tokens.get("access_token"))
    exp = (decode_jwt_payload(access) or {}).get("exp") if access else None
    if isinstance(exp, bool) or not isinstance(exp, (int, float)):
        exp = None
    id_token = _str(tokens.get("id_token"))
    return {
        "accessToken": access,
        "refreshToken": _str(tokens.get("refresh_token")),
        "expiresAt": int(exp * 1000) if exp is not None else None,
        "idToken": id_token,
        "accountId": _account_id(tokens, auth_claims(decode_jwt_payload(id_token))),
    }


def now_rfc3339() -> str:
    """UTC now as Codex serializes ``last_refresh``: microseconds plus ``Z``."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _error_code(body: str) -> str | None:
    """The refresh endpoint's error code, read where Codex reads it
    (login/src/oauth/error.rs:135-164): ``error`` (string), ``error.code``,
    then top-level ``code``."""
    try:
        data = json.loads(body)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    err = data.get("error")
    code = _str(err) or (_str(err.get("code")) if isinstance(err, dict) else None)
    code = code or _str(data.get("code"))
    return code.lower() if code else None


def try_refresh(creds: str, timeout_s: float = 10.0) -> RefreshOutcome:
    """Refresh-token grant against auth.openai.com -> ``oauth.RefreshOutcome``.

    Error kinds match Claude's so the consume gate and strike accounting
    apply unchanged: ``invalid_grant`` (dead lineage: any 401, or a 400/403
    with a dead-token code), ``invalid_client`` (our client id rejected —
    systemic, no strike), ``no_refresh_token``, else ``transient``.
    """
    from claude_swap.oauth import RefreshOutcome

    # Unparseable / non-dict = probably a torn read: transient, never the
    # permanent no_refresh_token (same rule as Claude's refresh).
    try:
        data = json.loads(creds)
    except (json.JSONDecodeError, TypeError):
        return RefreshOutcome(None, "transient")
    if not isinstance(data, dict):
        return RefreshOutcome(None, "transient")
    tokens = data.get("tokens")
    refresh_token = _str(tokens.get("refresh_token")) if isinstance(tokens, dict) else None
    if refresh_token is None:
        return RefreshOutcome(None, "no_refresh_token")

    try:
        req = urllib.request.Request(
            CODEX_TOKEN_URL,
            data=json.dumps({
                "client_id": CODEX_CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }).encode(),
            headers={"Content-Type": "application/json", "User-Agent": _USER_AGENT},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            resp_data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace") if e.fp else ""
        _logger.debug("Codex refresh failed: %r, body: %s", e, body[:500])
        code = _error_code(body)
        # Code-based verdicts on the same statuses as Claude's refresh.
        if e.code in (400, 401, 403):
            if code == "invalid_client":
                return RefreshOutcome(None, "invalid_client")
            if e.code == 401 or code in _DEAD_REFRESH_CODES:
                return RefreshOutcome(None, "invalid_grant")
        return RefreshOutcome(None, "transient")
    except Exception as e:
        _logger.debug("Codex refresh failed: %r", e)
        return RefreshOutcome(None, "transient")

    # Each token is optional in the response; overwrite only what came back
    # (auth/manager.rs:1599-1622). Even a lone rotated refresh_token must be
    # kept: the one we POSTed is already spent server-side, so dropping its
    # successor would get the next refresh refresh_token_reused. Only a reply
    # with no token at all refreshed nothing.
    returned = (
        {k: resp_data[k] for k in ("id_token", "access_token", "refresh_token")
         if _str(resp_data.get(k))}
        if isinstance(resp_data, dict) else {}
    )
    if not returned:
        return RefreshOutcome(None, "transient")
    tokens.update(returned)
    # A stored account_id is never rewritten (it is the key Codex's guarded
    # reload compares). A blob lacking it gets it from the id_token claim,
    # as Codex's login does: Codex's guarded refresh fails without one.
    if not _str(tokens.get("account_id")):
        claim = claimed_account_id(tokens.get("id_token"))
        if claim:
            tokens["account_id"] = claim
    data["last_refresh"] = now_rfc3339()
    # Keep the input's style: pretty as Codex's file store writes it,
    # compact as its keyring store does.
    if "\n" in creds:
        new_creds = json.dumps(data, indent=2)
    else:
        new_creds = json.dumps(data, separators=(",", ":"))

    ident = token_identity(new_creds)
    token_account = ident if ident and ident["uuid"] else None
    return RefreshOutcome(new_creds, None, token_account)


def _backend_url(base_url: str | None, path: str) -> str:
    """``{base}/wham/{path}`` or ``{base}/api/codex/{path}``, normalizing the
    base exactly as Codex's backend client does (backend-client/src/
    client.rs:149-157, 208-230)."""
    base = (base_url or CODEX_DEFAULT_BASE_URL).rstrip("/")
    if (
        base.startswith(("https://chatgpt.com", "https://chat.openai.com"))
        and "/backend-api" not in base
    ):
        base += "/backend-api"
    prefix = "/wham/" if "/backend-api" in base else "/api/codex/"
    return base + prefix + path


def _backend_get(creds: str, path: str, base_url: str | None, timeout_s: float) -> object:
    view = oauth_view(creds) or {}
    if not view.get("accessToken"):
        raise ValueError("Codex credential has no access token")
    headers = {
        "Authorization": f"Bearer {view['accessToken']}",
        "User-Agent": _USER_AGENT,
        "Accept": "application/json",
    }
    if view.get("accountId"):
        headers["ChatGPT-Account-ID"] = view["accountId"]
    req = urllib.request.Request(_backend_url(base_url, path), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return json.loads(resp.read().decode())


def request_usage(creds: str, base_url: str | None = None, timeout_s: float = 10.0) -> dict:
    """Raw ``/wham/usage`` body. Raises like ``oauth.request_usage_data``
    (``HTTPError``/``URLError``/``JSONDecodeError``) so the shared usage
    error classification applies unchanged."""
    return _backend_get(creds, "usage", base_url, timeout_s)


def build_usage_result(data: dict) -> dict | None:
    """Normalize a ``/wham/usage`` body into the shape Claude usage uses.

    Windows are bucketed by duration, not position — which window is primary
    is server-defined: ``<= 1 day`` is ``five_hour``, longer is
    ``seven_day``; two windows in one bucket keep the higher utilization.
    ``credits``/``spend_control``/``additional_rate_limits`` are not mapped.
    No window data at all is None ("unknown").
    """
    from claude_swap.oauth import format_reset

    rate_limit = data.get("rate_limit") if isinstance(data, dict) else None
    if not isinstance(rate_limit, dict):
        return None
    result: dict = {}
    for key, fallback in (("primary_window", "five_hour"), ("secondary_window", "seven_day")):
        window = rate_limit.get(key)
        if not isinstance(window, dict):
            continue
        pct = window.get("used_percent")
        if isinstance(pct, bool) or not isinstance(pct, (int, float)):
            continue
        secs = window.get("limit_window_seconds")
        if isinstance(secs, (int, float)) and not isinstance(secs, bool):
            bucket = "five_hour" if secs <= 86400 else "seven_day"
        else:
            bucket = fallback
        entry: dict = {"pct": float(pct)}
        reset_at = window.get("reset_at")
        if isinstance(reset_at, (int, float)) and not isinstance(reset_at, bool):
            entry["resets_at"] = datetime.fromtimestamp(reset_at, tz=timezone.utc).isoformat()
            entry["countdown"], entry["clock"] = format_reset(entry["resets_at"])
        if bucket not in result or entry["pct"] > result[bucket]["pct"]:
            result[bucket] = entry
    return result or None


def fetch_workspace_name(creds: str, base_url: str | None = None) -> str | None:
    """The workspace's display name from ``/wham/accounts/check`` (advisory:
    any failure is None, never an exception)."""
    try:
        account_id = (oauth_view(creds) or {}).get("accountId")
        if not account_id:
            return None
        data = _backend_get(creds, "accounts/check", base_url, 5.0)
        for account in data.get("accounts") or []:
            if isinstance(account, dict) and account.get("id") == account_id:
                return _str((account.get("name") or "").strip())
    except Exception as e:
        _logger.debug("Codex workspace name lookup failed: %r", e)
    return None
