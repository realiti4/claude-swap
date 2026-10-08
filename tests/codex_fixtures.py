"""Builders for fake Codex credentials and HTTP responses.

Codex's ``auth.json`` carries two JWTs whose payloads cswap reads offline: the
id_token (identity: email, ``chatgpt_account_id``, ``chatgpt_user_id``, plan)
and the access token (``exp`` -> ``expiresAt``). These builders produce
unsigned ``header.payload.sig`` tokens with base64url JSON segments — enough
for ``codex_auth.decode_jwt_payload``, which never verifies signatures.

Defaults describe one personal ("plus") account; pass ``plan="team"`` plus a
different ``account_id`` for a workspace membership of the same person.
"""

from __future__ import annotations

import base64
import io
import json
import time
import urllib.error

AUTH_CLAIM = "https://api.openai.com/auth"
PROFILE_CLAIM = "https://api.openai.com/profile"
LAST_REFRESH = "2026-10-07T02:10:30.972491Z"
TEN_DAYS_S = 10 * 86400


def _b64url(obj: dict) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def fake_jwt(claims: dict) -> str:
    """Unsigned JWT with ``claims`` as its payload."""
    return f"{_b64url({'alg': 'none', 'typ': 'JWT'})}.{_b64url(claims)}.sig"


def id_token(
    *,
    email: str | None = "user@example.com",
    account_id: str | None = "acct-personal",
    user_id: str | None = "user-1",
    plan: str | None = "plus",
    exp: int | None = None,
) -> str:
    auth = {
        k: v
        for k, v in (
            ("chatgpt_account_id", account_id),
            ("chatgpt_user_id", user_id),
            ("chatgpt_plan_type", plan),
        )
        if v is not None
    }
    claims: dict = {AUTH_CLAIM: auth, "exp": exp or int(time.time()) + 3600}
    if email is not None:
        claims["email"] = email
    return fake_jwt(claims)


def access_token(
    *,
    exp: int | None = None,
    account_id: str | None = "acct-personal",
    user_id: str | None = "user-1",
    nonce: str = "a",
) -> str:
    """Access token expiring at ``exp`` (default: 10 days out, Codex's real
    lifetime). ``nonce`` makes two otherwise-equal tokens differ."""
    return fake_jwt({
        "exp": int(time.time()) + TEN_DAYS_S if exp is None else exp,
        AUTH_CLAIM: {"chatgpt_account_id": account_id, "chatgpt_user_id": user_id},
        "jti": nonce,
    })


def auth_dict(
    *,
    email: str | None = "user@example.com",
    account_id: str | None = "acct-personal",
    user_id: str | None = "user-1",
    plan: str | None = "plus",
    access_exp: int | None = None,
    refresh_token: str | None = None,
    access: str | None = None,
    last_refresh: str | None = LAST_REFRESH,
    store_account_id: bool = True,
) -> dict:
    """A ChatGPT-mode ``auth.json`` as a dict (tweak, then ``json.dumps``).

    ``refresh_token`` defaults to one derived from the identity so distinct
    accounts get distinct lineage fingerprints. ``store_account_id=False``
    omits ``tokens.account_id`` (the id_token claim still carries it).
    """
    tokens = {
        "id_token": id_token(email=email, account_id=account_id, user_id=user_id, plan=plan),
        "access_token": access
        or access_token(exp=access_exp, account_id=account_id, user_id=user_id),
        "refresh_token": refresh_token or f"rt-{user_id}-{account_id}",
    }
    if store_account_id:
        tokens["account_id"] = account_id
    data: dict = {"auth_mode": "chatgpt", "OPENAI_API_KEY": None, "tokens": tokens}
    if last_refresh is not None:
        data["last_refresh"] = last_refresh
    return data


def auth_json(*, pretty: bool = True, **kwargs) -> str:
    """``auth_dict`` serialized: pretty like Codex's file store, or compact
    like its keyring store."""
    data = auth_dict(**kwargs)
    return json.dumps(data, indent=2) if pretty else json.dumps(data, separators=(",", ":"))


def api_key_json(key: str = "sk-proj-test-key") -> str:
    """An API-key ``auth.json`` as ``codex login --with-api-key`` writes it."""
    return json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": key}, indent=2)


def wham_usage(
    primary: tuple[float, int, int] | None = None,
    secondary: tuple[float, int, int] | None = None,
    *,
    plan: str = "plus",
) -> dict:
    """A ``/wham/usage`` body; each window is ``(used_percent,
    limit_window_seconds, reset_at_epoch_s)`` or None."""

    def window(spec):
        if spec is None:
            return None
        pct, secs, reset_at = spec
        return {
            "used_percent": pct,
            "limit_window_seconds": secs,
            "reset_after_seconds": max(0, reset_at - int(time.time())),
            "reset_at": reset_at,
        }

    return {
        "plan_type": plan,
        "rate_limit": {
            "allowed": True,
            "limit_reached": False,
            "primary_window": window(primary),
            "secondary_window": window(secondary),
        },
    }


class FakeResponse:
    """Stand-in for ``urlopen``'s return value (a context manager)."""

    def __init__(self, body: dict | bytes) -> None:
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc) -> bool:
        return False


# Every HTTPError ``http_error`` made, closed by conftest after each test:
# each wraps an open response, which the GC would report as a ResourceWarning.
OPEN_HTTP_ERRORS: list[urllib.error.HTTPError] = []


def http_error(
    code: int, body: dict | bytes = b"", headers: dict | None = None, url: str = "https://x"
) -> urllib.error.HTTPError:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    err = urllib.error.HTTPError(url, code, "err", hdrs=headers, fp=io.BytesIO(raw))
    OPEN_HTTP_ERRORS.append(err)
    return err
