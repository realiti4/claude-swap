"""Read and write ``~/.codex/auth.json``, and derive an account identity from it.

The live file is the authority on which Codex account is active. The codex CLI
refreshes its own tokens and writes them back here, so a session that is still
open on account A can overwrite this file *after* cswap has switched to B. Any
"which account is active" answer derived from cswap's own registry alone would
therefore be wrong; it is derived from this file instead, and the mismatch is
what capture-on-switch repairs.

Identity resolution follows codex-auth's rules so imported records and
cswap-captured records key identically:

1. ``tokens.account_id`` when present — the value the codex CLI itself uses.
2. else the JWT's ``chatgpt_account_id``.
3. else an organization id from the JWT's ``organizations[]``, preferring
   ``is_default``, falling back to the first non-empty id. Phone-login auth
   files carry only this.

Nothing here verifies a token's signature: that is the server's job, and cswap
only needs the claims to know whose token it is holding.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from claude_swap.codex.paths import get_live_auth_path

#: Namespaced claim block the ChatGPT tokens carry their account context in.
AUTH_CLAIM = "https://api.openai.com/auth"


@dataclass(frozen=True)
class CodexIdentity:
    """Who an ``auth.json`` payload belongs to."""

    account_id: str
    user_id: str
    email: str
    plan: str
    is_api_key: bool = False

    @property
    def account_key(self) -> str:
        """Stable identity key, byte-identical to codex-auth's."""
        return account_key(self.user_id, self.account_id)

    @property
    def is_identifiable(self) -> bool:
        """Whether this identity can be matched to a stored slot.

        An API-key login, or a payload whose account context could not be
        resolved at all, has no key to match on — it is a real login but not a
        *managed* one until it is explicitly added.
        """
        return bool(self.account_id)


def account_key(user_id: str, account_id: str) -> str:
    """Join a user and account id into codex-auth's record key."""
    return f"{user_id}::{account_id}"


def file_key(key: str) -> str:
    """Encode an account key for use as a filename or Keychain account.

    Unpadded base64url, matching codex-auth's snapshot filenames — verified
    against a real ``<key>.auth.json`` on disk.
    """
    return base64.urlsafe_b64encode(key.encode()).decode().rstrip("=")


def _decode_jwt_claims(token: object) -> dict | None:
    """Decode a JWT's payload segment without verifying it."""
    if not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) < 2:
        return None
    seg = parts[1]
    try:
        raw = base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))
        claims = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return claims if isinstance(claims, dict) else None


def _org_fallback(auth_claims: dict) -> str:
    """Pick an organization id when no account id is present."""
    orgs = auth_claims.get("organizations")
    if not isinstance(orgs, list):
        return ""
    for org in orgs:
        if isinstance(org, dict) and org.get("is_default") and org.get("id"):
            return str(org["id"])
    for org in orgs:
        if isinstance(org, dict) and org.get("id"):
            return str(org["id"])
    return ""


def parse_identity(payload: object) -> CodexIdentity | None:
    """Derive a :class:`CodexIdentity` from an ``auth.json`` payload."""
    if not isinstance(payload, dict):
        return None

    tokens = payload.get("tokens")
    if not isinstance(tokens, dict):
        # An API-key login has no OAuth tokens at all. It is a real, listable
        # account — it simply has no usage and nothing to refresh — so it gets
        # an identity rather than a parse failure.
        if payload.get("auth_mode") == "apikey" or payload.get("OPENAI_API_KEY"):
            return CodexIdentity("", "", "", "", is_api_key=True)
        return None

    claims = _decode_jwt_claims(tokens.get("id_token")) or _decode_jwt_claims(
        tokens.get("access_token")
    )
    if claims is None:
        return None

    auth_claims = claims.get(AUTH_CLAIM)
    if not isinstance(auth_claims, dict):
        auth_claims = {}

    account_id = tokens.get("account_id") or auth_claims.get("chatgpt_account_id") or ""
    if not account_id:
        account_id = _org_fallback(auth_claims)

    return CodexIdentity(
        account_id=str(account_id),
        user_id=str(auth_claims.get("chatgpt_user_id") or ""),
        email=str(claims.get("email") or ""),
        plan=str(auth_claims.get("chatgpt_plan_type") or ""),
        is_api_key=payload.get("auth_mode") == "apikey",
    )


def read_live_payload() -> dict | None:
    """Read the live ``auth.json``, or None when absent/unreadable/torn."""
    path = get_live_auth_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A torn write is transient — the next pass re-reads. Never fatal.
        return None
    return data if isinstance(data, dict) else None


def read_live_identity() -> CodexIdentity | None:
    """Identity of whoever is currently logged in to the codex CLI."""
    payload = read_live_payload()
    return parse_identity(payload) if payload is not None else None


def access_token_expiry(payload: object) -> float | None:
    """The access token's ``exp`` claim as a POSIX timestamp, or None."""
    if not isinstance(payload, dict):
        return None
    tokens = payload.get("tokens")
    if not isinstance(tokens, dict):
        return None
    claims = _decode_jwt_claims(tokens.get("access_token"))
    if not claims:
        return None
    exp = claims.get("exp")
    return float(exp) if isinstance(exp, (int, float)) else None


def write_live_auth(payload: dict) -> Path:
    """Write ``payload`` to the live ``auth.json``, atomically.

    An existing file keeps its current mode: it belongs to the codex CLI, which
    created it, and a switch has no business re-permissioning it. A file we
    create from scratch is private (0600), because the snapshot it came from is.
    """
    path = get_live_auth_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    existing_mode: int | None = None
    if path.exists() and sys.platform != "win32":
        existing_mode = os.stat(path).st_mode & 0o777

    tmp = path.with_name(path.name + ".cswap.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    if sys.platform != "win32":
        os.chmod(tmp, existing_mode if existing_mode is not None else 0o600)
    os.replace(tmp, path)
    return path
