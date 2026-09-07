"""Codex fixtures: a fake ~/.codex tree and JWT/auth.json builders.

Imported by ``tests/conftest.py`` so these are available everywhere without a
second conftest layer. No test may touch the developer's real ~/.codex — the
autouse ``_isolate_real_home`` fixture already redirects ``$HOME``, and
``codex_home`` builds its tree under that redirect.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import pytest


def make_jwt(claims: dict) -> str:
    """Build an unsigned JWT carrying ``claims``.

    Signature is a fixed placeholder: nothing in cswap verifies these tokens —
    the server does. cswap only reads the claims to learn which account a token
    belongs to, so an unsigned token is a faithful stand-in for a test.
    """

    def seg(obj: dict) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{seg({'alg': 'none', 'typ': 'JWT'})}.{seg(claims)}.sig"


def make_auth_json(
    *,
    account_id: str = "2f4dac8f-f15f-4c58-a567-e96985d51cfd",
    user_id: str = "user-K6XCCWw4gcRpfaGR6VQKAFgA",
    email: str = "a@example.com",
    plan: str = "pro",
    exp: float | None = None,
    refresh_token: str = "rt-a",
    access_token: str | None = None,
    auth_mode: str = "chatgpt",
    api_key: str | None = None,
    organizations: list[dict] | None = None,
) -> dict:
    """Build an ``auth.json`` payload in the real shape codex writes."""
    claims: dict = {
        "exp": int(exp if exp is not None else time.time() + 3600),
        "email": email,
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account_id,
            "chatgpt_user_id": user_id,
            "chatgpt_plan_type": plan,
        },
    }
    if organizations is not None:
        claims["https://api.openai.com/auth"]["organizations"] = organizations
    token = make_jwt(claims)
    return {
        "auth_mode": auth_mode,
        "OPENAI_API_KEY": api_key,
        "tokens": {
            "id_token": token,
            "access_token": access_token or token,
            "refresh_token": refresh_token,
            "account_id": account_id,
        },
        "last_refresh": "2026-08-16T00:00:00Z",
    }


def strip_account_claim(payload: dict) -> dict:
    """Remove ``chatgpt_account_id`` and ``tokens.account_id`` from a payload.

    Reproduces a phone-login auth file, which carries neither and can only be
    identified through the organization fallback. Rewrites the JWT so the
    removal is visible to the parser, not just to the test.
    """
    seg = payload["tokens"]["id_token"].split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))
    claims["https://api.openai.com/auth"].pop("chatgpt_account_id", None)
    token = make_jwt(claims)
    payload["tokens"]["id_token"] = token
    payload["tokens"]["access_token"] = token
    payload["tokens"]["account_id"] = None
    return payload


@pytest.fixture
def codex_home(temp_home: Path) -> Path:
    """An isolated ``~/.codex`` with no accounts yet."""
    home = temp_home / ".codex"
    home.mkdir(parents=True, exist_ok=True)
    return home


@pytest.fixture
def live_auth(codex_home: Path):
    """Write an ``auth.json`` into the fake codex home; returns the writer."""

    def _write(payload: dict | None = None) -> Path:
        path = codex_home / "auth.json"
        path.write_text(json.dumps(payload if payload is not None else make_auth_json()))
        return path

    return _write
