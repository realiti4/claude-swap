"""Access-token-only credentials for managed session profiles.

A managed session never holds a refresh token: the slot's backup store is
the single holder of each account's lineage (refresh tokens are single-use,
and two stores holding one lineage is the #96/#164 stale-copy failure).
Claude Code supports a credential without ``refreshToken`` as an
inference-only mode — it never refreshes, never persists, never logs out
because of it — so cswap refreshes centrally and pushes the new access
token into every session of that account through
:func:`write_session_credential`.
"""

from __future__ import annotations

import json

from claude_swap import oauth

# Allowlist, not a denylist: anything Claude grows later (a new lineage
# field) must not leak a refresh-capable secret into a managed profile.
# subscriptionType/rateLimitTier are Claude's plan metadata, read from the
# credential itself.
ACCESS_CREDENTIAL_KEYS = (
    "accessToken",
    "expiresAt",
    "scopes",
    "subscriptionType",
    "rateLimitTier",
)


def build_access_credential(credentials: str) -> str:
    """The access-token-only projection of a full Claude OAuth credential.

    Raises:
        ValueError: ``credentials`` carries no ``claudeAiOauth.accessToken``
            (malformed JSON, a managed API key, a refresh-only blob).
    """
    data = oauth.extract_oauth_data(credentials)
    token = data.get("accessToken") if data else None
    if not isinstance(token, str) or not token:
        raise ValueError("credential has no claudeAiOauth.accessToken")
    access = {
        key: data[key] for key in ACCESS_CREDENTIAL_KEYS if data.get(key) is not None
    }
    return json.dumps({"claudeAiOauth": access})
