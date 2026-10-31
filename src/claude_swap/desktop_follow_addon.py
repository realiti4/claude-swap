"""mitmproxy addon: bill Claude Desktop's Code tab to cswap's active account.

The Code tab runs its CLI with the app's own login, so ``cswap switch`` never
reaches it. Run behind this addon, the Code tab's inference requests carry the
active account's access token instead. Only ``/v1/messages`` is rewritten;
everything else (profile, usage, OAuth, Remote Control) keeps the app's login.
This is the Windows stand-in for cswap-pin's ``pin.desktopFollows``, which needs
POSIX file locks and FIFOs.

    mitmdump -p 8899 -s desktop_follow_addon.py

Read-only: it never refreshes or writes a credential. A token that has expired
is not substituted, so the app's own login keeps working instead of failing.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

REWRITE_PREFIX = "/v1/messages"
API_HOST = "api.anthropic.com"
_CACHE: dict = {"mtime": None, "token": None}


def credentials_path() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    return Path(base) / ".credentials.json"


def active_token(path: Path | None = None, now_ms: float | None = None) -> str | None:
    """The active account's unexpired access token, or None."""
    path = path or credentials_path()
    try:
        mtime = path.stat().st_mtime_ns
        if _CACHE["mtime"] != mtime:
            oauth = json.loads(path.read_text(encoding="utf-8")).get("claudeAiOauth") or {}
            _CACHE["mtime"] = mtime
            _CACHE["token"] = (oauth.get("accessToken"), oauth.get("expiresAt"))
    except (OSError, ValueError):
        return None
    token, expires_at = _CACHE["token"] or (None, None)
    if not isinstance(token, str) or not token:
        return None
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    if isinstance(expires_at, (int, float)) and expires_at <= now_ms + 30_000:
        return None
    return token


def rewrite(host: str, path: str, headers) -> bool:
    """Swap the bearer on an inference request. True when it changed."""
    if host != API_HOST or not path.startswith(REWRITE_PREFIX):
        return False
    if not headers.get("authorization", "").lower().startswith("bearer "):
        return False
    token = active_token()
    if token is None or headers["authorization"] == f"Bearer {token}":
        return False
    headers["authorization"] = f"Bearer {token}"
    # A short hash, so a switch is visible in the log without exposing the token.
    print(f"cswap: bearer swapped to token {hashlib.sha256(token.encode()).hexdigest()[:8]}", flush=True)
    return True


ENTRYPOINT = "claude-desktop"


def marker_path() -> Path:
    """cswap-pin's ``inference-follows`` file, which the swap-meter band reads.

    Listing the entrypoint there is how the band knows the Code tab bills the
    active account instead of the app's own login."""
    override = os.environ.get("CSWAP_DESKTOP_FOLLOW_MARKER")
    if override:
        return Path(override)
    return Path.home() / ".claude-swap-backup" / "pin-proxy" / "inference-follows"


def publish_marker(path: Path | None = None) -> None:
    path = path or marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{ENTRYPOINT}  # written by desktop-follow while its proxy runs\n", encoding="utf-8")


def withdraw_marker(path: Path | None = None) -> None:
    (path or marker_path()).unlink(missing_ok=True)


class FollowActive:
    def running(self) -> None:
        publish_marker()

    def done(self) -> None:
        withdraw_marker()

    def request(self, flow) -> None:
        if rewrite(flow.request.pretty_host, flow.request.path, flow.request.headers):
            flow.comment = "cswap: billed to active account"


addons = [FollowActive()]
