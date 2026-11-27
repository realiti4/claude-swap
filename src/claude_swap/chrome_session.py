"""Chrome extension OAuth-token capture + injection — the browser half of a switch.

claude-swap switches the Claude Code CLI account (an OAuth token in the
Keychain). This module is the optional, opt-in second half: it switches the
account of the **"Claude in Chrome" extension** so its Claude Code pairing tracks
the CLI account.

How the extension's auth works (reverse-engineered + verified)
--------------------------------------------------------------
The extension does NOT authenticate by a claude.ai cookie. It runs an OAuth PKCE
flow and stores the resulting tokens in ``chrome.storage.session`` (in-memory):

- ``accessToken``  (``sk-ant-oat01-…``) — 8h lifetime on refresh.
- ``refreshToken`` (``sk-ant-ort01-…``) — ~28.5 day lifetime, **rotates on every
  refresh** (each refresh mints a fresh 28.5 day window).
- ``tokenExpiry``  (ms) — when the access token expires.

``chrome.storage.local`` holds ``accountUuid`` and, on newer builds (v1.0.100+),
``tokenOrg`` (``{uuid, hybrid}``, read by the extension's identity/policy code),
``tokenScope`` and sometimes a ``diskRefreshToken`` copy. When the extension
stores a new token it **revokes** a displaced ``diskRefreshToken`` and signing
out revokes every refresh token it holds, so an injection must not leave another
account's token behind and users must never sign the extension out to move
between accounts (``cswap chrome detach`` clears it locally instead). The
service worker (SW) reads
``storage.session`` **once at startup** into JS runtime memory and connects the
bridge from there; it re-reads only when it restarts.

Switching accounts (the mechanism, verified end-to-end)
-------------------------------------------------------
1. Inject the target account's tokens into ``storage.session`` + ``accountUuid``
   into ``storage.local`` (via CDP, evaluating in the SW).
2. Terminate the SW with CDP ``Target.closeTarget`` — **not**
   ``chrome.runtime.reload()``. A runtime reload *clears* ``storage.session``
   (losing the just-injected tokens); ``closeTarget`` *preserves* it.
3. Wake the SW with a throwaway background ``claude.ai`` tab (closed afterwards).
   On restart the SW reads the injected tokens and reconnects the bridge as the
   new account. If the injected access token is expired, the SW auto-refreshes
   via the refresh token on adoption (self-heals).

The claude.ai web login (cookies) must be the same account: when a refresh
token fails, the extension re-authorizes silently (``prompt=none`` with
``login_hint=<accountUuid>``) through that login, and a login to another
account answers ``login_required``, which the extension (v1.0.109) treats as a
definitive ``account_mismatch`` and stops on ``session_expired``. Its content
script also reports the ``lastActiveOrg`` cookie as the org hint. So a switch
moves the web login too: :class:`WebVault` keeps each account's identity
cookies, captured only after ``/api/account`` (fetched by the extension's SW
with the browser's cookies) confirms whose they are.

Because the tokens live in in-memory session storage, capture requires the
automation Chrome running + CDP (there is no on-disk / offline path, and no
Keychain/openssl needed — unlike the old cookie approach).

Why a dedicated Chrome profile
------------------------------
CDP against modern Chrome (macOS ~M136+/2025) forces a dedicated, separately
launched profile: the default user-data-dir ignores ``--remote-debugging-port``;
``--load-extension`` is blocked (the extension is installed once from the Web
Store); the wake tab must be headed (Cloudflare blocks headless on claude.ai).

Keep-alive
----------
A parked account's refresh token expires after ~28.5 days untouched. cswap can
refresh it offline (``refresh_session`` → ``platform.claude.com/v1/oauth/token``)
to roll the window indefinitely without opening a browser.

macOS-only for now; every entry point no-ops (returns None/False, logs) on other
platforms so callers stay unconditional.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path

_logger = logging.getLogger("claude-swap")

CHROME_APP = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CLAUDE_EXTENSION_ID = "fcoeoabgfenejglbffodgkkbkcdhcgfn"
CLAUDE_URL = "https://claude.ai/"

# OAuth constants read from the extension bundle's production config
# (J.production in assets/mcpPermissions-*.js). The 54511e87… client id is the
# "development" one and is rejected by the token endpoint.
OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_CLIENT_ID = "dae2cad8-15c5-43d2-9046-fcaecc135fa4"
OAUTH_REQUESTED_EXPIRES_IN = 31536000  # what the extension asks for; server caps it

# Refresh a parked account's token once it is this old, before its ~28.5 day
# refresh-token window closes and it would need a browser re-login.
KEEPALIVE_REFRESH_DAYS = 20

# Refresh before injecting when the stored access token expires within this
# window, so the extension never adopts an already-dead access token.
ACCESS_EXPIRY_MARGIN_MS = 5 * 60 * 1000


def is_supported() -> bool:
    """Chrome extension sync is implemented for macOS only (for now)."""
    return sys.platform == "darwin"


# --------------------------------------------------------------------------- #
# Session value object — the extension's OAuth tokens + identity for one account
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ChromeSession:
    """One account's Claude-in-Chrome OAuth tokens (+ identity for matching).

    ``refresh_token`` is what makes a stored session durable: with a valid one
    the extension self-heals even from an expired/blank access token. ``email``
    is display-only; ``org_uuid`` backs the add-time org-match guard.
    """

    access_token: str
    refresh_token: str = ""
    token_expiry: int = 0       # ms epoch; access-token expiry
    account_uuid: str = ""
    org_uuid: str = ""
    email: str = ""
    captured_at: int = 0        # ms epoch; last capture/refresh (refresh-token age)
    token_org_uuid: str = ""    # extension's ``tokenOrg.uuid`` (storage.local)
    token_org_hybrid: bool | None = None  # ``tokenOrg.hybrid``; None = not captured
    token_scope: str = ""       # extension's ``tokenScope`` (storage.local)
    refresh_dead: bool = False  # the token endpoint rejected the refresh token

    def is_usable(self) -> bool:
        # A refresh token is the durable credential; without it a stored session
        # cannot survive the 8h access-token lifetime.
        return bool(self.access_token and self.refresh_token)

    def to_dict(self) -> dict:
        return {
            "accessToken": self.access_token,
            "refreshToken": self.refresh_token,
            "tokenExpiry": self.token_expiry,
            "accountUuid": self.account_uuid,
            "orgUuid": self.org_uuid,
            "email": self.email,
            "capturedAt": self.captured_at,
            **({"tokenOrg": {"uuid": self.token_org_uuid, "hybrid": self.token_org_hybrid}}
               if self.token_org_uuid and self.token_org_hybrid is not None else {}),
            **({"tokenScope": self.token_scope} if self.token_scope else {}),
            **({"refreshDead": True} if self.refresh_dead else {}),
        }

    @classmethod
    def from_dict(cls, data: dict) -> ChromeSession:
        return cls(
            access_token=data.get("accessToken", ""),
            refresh_token=data.get("refreshToken", ""),
            token_expiry=int(data.get("tokenExpiry", 0) or 0),
            account_uuid=data.get("accountUuid", ""),
            org_uuid=data.get("orgUuid", ""),
            email=data.get("email", ""),
            captured_at=int(data.get("capturedAt", 0) or 0),
            **_token_org_fields(data.get("tokenOrg")),
            token_scope=data.get("tokenScope", "") or "",
            refresh_dead=bool(data.get("refreshDead", False)),
        )


def _token_org_fields(raw) -> dict:
    """``tokenOrg`` as stored by the extension → ChromeSession fields."""
    if isinstance(raw, dict) and isinstance(raw.get("uuid"), str) and raw["uuid"]:
        return {"token_org_uuid": raw["uuid"].lower(), "token_org_hybrid": raw.get("hybrid") is True}
    return {}


# --------------------------------------------------------------------------- #
# Minimal CDP-over-WebSocket client (pure stdlib, no websocket-client dep)
# --------------------------------------------------------------------------- #
class _WS:
    """RFC6455 client: just the text-frame send/recv the CDP calls need."""

    def __init__(self, url: str, timeout: float = 10.0):
        if not url.startswith("ws://"):
            raise ValueError(f"expected ws:// url, got {url!r}")
        host_port, _, path = url[len("ws://"):].partition("/")
        host, _, port = host_port.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            (
                f"GET /{path} HTTP/1.1\r\nHost: {host_port}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            resp += chunk
        if b" 101 " not in resp.split(b"\r\n", 1)[0]:
            raise RuntimeError(f"WebSocket handshake failed: {resp[:80]!r}")
        self._buf = b""

    def send(self, text: str) -> None:
        payload = text.encode()
        header = bytearray([0x81])
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        self.sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def recv(self) -> str:
        while True:
            frame, self._buf = self._read_frame(self._buf)
            if frame is not None:
                return frame

    def _read_frame(self, buf: bytes):
        while len(buf) < 2:
            buf += self.sock.recv(4096)
        b1, length = buf[0], buf[1] & 0x7F
        masked = buf[1] & 0x80
        idx = 2
        if length == 126:
            while len(buf) < 4:
                buf += self.sock.recv(4096)
            length = struct.unpack(">H", buf[2:4])[0]
            idx = 4
        elif length == 127:
            while len(buf) < 10:
                buf += self.sock.recv(4096)
            length = struct.unpack(">Q", buf[2:10])[0]
            idx = 10
        if masked:
            idx += 4
        while len(buf) < idx + length:
            buf += self.sock.recv(4096)
        payload, rest = buf[idx:idx + length], buf[idx + length:]
        if (b1 & 0x0F) != 0x1:  # ignore non-text control frames
            return None, rest
        return payload.decode(errors="replace"), rest

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class _CDP:
    def __init__(self, ws_url: str):
        self._ws = _WS(ws_url)
        self._id = 0

    def call(self, method: str, params: dict | None = None, timeout: float = 15.0) -> dict:
        self._id += 1
        mid = self._id
        self._ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msg = json.loads(self._ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})
        raise TimeoutError(f"CDP {method} timed out")

    def evaluate(self, expression: str, *, timeout: float = 15.0):
        """Evaluate JS (awaiting a returned promise) and return the raw value."""
        self.call("Runtime.enable")
        res = self.call(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": True, "returnByValue": True},
            timeout=timeout,
        )
        return res.get("result", {}).get("value")

    def close(self) -> None:
        self._ws.close()


def _http_json(port: int, path: str):
    import urllib.request
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", headers={"Host": f"127.0.0.1:{port}"}
    )
    return json.load(urllib.request.urlopen(req, timeout=5))


def _list_targets(port: int) -> list[dict]:
    for path in ("/json", "/json/list"):
        try:
            data = _http_json(port, path)
            return [t for t in data if isinstance(t, dict)]
        except Exception:
            continue
    return []


def _browser_ws(port: int) -> str | None:
    try:
        return _http_json(port, "/json/version").get("webSocketDebuggerUrl")
    except Exception:
        return None


def cdp_ready(port: int) -> bool:
    return bool(_list_targets(port))


def wait_cdp_ready(port: int, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cdp_ready(port):
            return True
        time.sleep(0.4)
    return False


# --------------------------------------------------------------------------- #
# Extension service-worker access (where the tokens live)
# --------------------------------------------------------------------------- #
def _sw_target(port: int) -> dict | None:
    """The Claude extension's service-worker target, or None if not running."""
    for t in _list_targets(port):
        if t.get("type") == "service_worker" and CLAUDE_EXTENSION_ID in t.get("url", ""):
            return t
    return None


def _eval_in_sw(port: int, expression: str, *, timeout: float = 15.0):
    """Evaluate JS in the extension SW. Returns None if the SW isn't reachable."""
    t = _sw_target(port)
    if not t or not t.get("webSocketDebuggerUrl"):
        return None
    cdp = _CDP(t["webSocketDebuggerUrl"])
    try:
        return cdp.evaluate(expression, timeout=timeout)
    finally:
        cdp.close()


def _create_target(port: int, url: str, *, background: bool = True) -> str | None:
    bws = _browser_ws(port)
    if not bws:
        return None
    cdp = _CDP(bws)
    try:
        return cdp.call("Target.createTarget", {"url": url, "background": background}).get("targetId")
    except Exception:  # noqa: BLE001
        return None
    finally:
        cdp.close()


def _close_target(port: int, target_id: str) -> None:
    if not target_id:
        return
    bws = _browser_ws(port)
    if not bws:
        return
    cdp = _CDP(bws)
    try:
        cdp.call("Target.closeTarget", {"targetId": target_id})
    except Exception:  # noqa: BLE001
        pass
    finally:
        cdp.close()


# Poll step for the waits around an SW restart. A CDP target listing is a
# local HTTP call of a few milliseconds, so polling this often costs nothing.
_POLL_S = 0.1


def _wait_for_sw(port: int, timeout: float = 12.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _sw_target(port):
            return True
        time.sleep(_POLL_S)
    return False


def _wait_for_sw_gone(port: int, sw_id: str | None, timeout: float = 2.0) -> bool:
    """Wait until the SW target ``sw_id`` has left the target list.

    The wake tab must only be opened once the old SW is gone: a still-running
    SW would serve it from memory instead of re-reading the injected storage.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        t = _sw_target(port)
        if t is None or t.get("id") != sw_id:
            return True
        time.sleep(_POLL_S)
    return False


# --------------------------------------------------------------------------- #
# Capture / identity (read the extension's current tokens via CDP)
# --------------------------------------------------------------------------- #
_CAPTURE_JS = r"""
(async () => {
  const s = await chrome.storage.session.get(
    ['accessToken', 'refreshToken', 'tokenExpiry', 'swAnalyticsProfileCache']);
  const l = await chrome.storage.local.get(
    ['accountUuid', 'tokenOrg', 'tokenScope', 'lastAuthFailureReason']);
  const f = await chrome.storage.session.get(['lastAuthFailureReason']);
  const prof = (s.swAnalyticsProfileCache || {}).profile || {};
  return JSON.stringify({
    accessToken: s.accessToken || '',
    refreshToken: s.refreshToken || '',
    tokenExpiry: s.tokenExpiry || 0,
    accountUuid: l.accountUuid || '',
    email: (prof.account || {}).email || '',
    orgUuid: (prof.organization || {}).uuid || '',
    tokenOrg: l.tokenOrg || null,
    tokenScope: l.tokenScope || '',
    authFailure: l.lastAuthFailureReason || f.lastAuthFailureReason || '',
  });
})()
"""


def _read_state_cdp(port: int) -> dict | None:
    raw = _eval_in_sw(port, _CAPTURE_JS)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _session_from_state(data: dict) -> ChromeSession:
    org = _token_org_fields(data.get("tokenOrg"))
    profile_org = (data.get("orgUuid") or "").lower()
    if org and profile_org and org["token_org_uuid"] != profile_org:
        org = {}  # left over from another account; the profile is authoritative
    return ChromeSession(
        access_token=data.get("accessToken", ""),
        refresh_token=data.get("refreshToken", ""),
        token_expiry=int(data.get("tokenExpiry", 0) or 0),
        account_uuid=data.get("accountUuid", ""),
        # The profile cache is the authoritative org; ``tokenOrg`` covers the
        # window before the extension has re-fetched the profile.
        org_uuid=data.get("orgUuid", "") or org.get("token_org_uuid", ""),
        email=data.get("email", ""),
        captured_at=int(time.time() * 1000),
        token_scope=data.get("tokenScope", "") or "",
        **org,
    )


def capture_tokens_cdp(port: int) -> ChromeSession | None:
    """Capture the extension's *current* OAuth tokens from the SW via CDP.

    Requires the automation Chrome running with the extension connected. Returns
    None if the SW is unreachable, no tokens are present (not logged in), or the
    extension has recorded an auth failure for the tokens it holds.
    """
    data = _read_state_cdp(port)
    if not data or data.get("authFailure"):
        return None
    session = _session_from_state(data)
    return session if session.is_usable() else None


def read_identity_cdp(port: int) -> dict:
    """The extension's current account identity (email/accountUuid/orgUuid).

    Empty dict if the SW is unreachable; empty ``email`` means the profile has
    not been fetched yet (unauthenticated or mid-connect).
    """
    raw = _eval_in_sw(port, _CAPTURE_JS)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return {
        "email": data.get("email", ""),
        "accountUuid": data.get("accountUuid", ""),
        "orgUuid": data.get("orgUuid", ""),
    }


# --------------------------------------------------------------------------- #
# Injection + SW restart (adopt a stored account into the running extension)
# --------------------------------------------------------------------------- #
# State tied to whichever account the extension held before an injection:
# dropped so it cannot be attributed to the injected token. ``diskRefreshToken``
# matters most — the extension revokes a displaced disk token the next time it
# stores one, which would kill the outgoing account's stored session.
# Key names are the extension's storage enum (v1.0.107); removing a key from an
# area that does not hold it is a no-op, so both areas get the same list.
_ACCOUNT_STATE_KEYS = [
    "swAnalyticsProfileCache", "tokenOrgUuid", "diskRefreshToken", "lastAuthFailureReason",
    "startupReauthState", "oauthState", "codeVerifier", "oauthOrgSwitch", "orgTelemetry",
    "orgGateOff", "lastHybridPairing", "pairingOrgMismatch", "lastActiveOrgHint",
]
_INJECT_CLEAR_SESSION = _INJECT_CLEAR_LOCAL = _ACCOUNT_STATE_KEYS
# Signing out locally (``detach``): what the extension's own sign-out clears
# about the account (tokens, identity, org state), minus the revoke. User
# preferences, permissions and tab groups are left alone.
_DETACH_KEYS = ["accessToken", "refreshToken", "tokenExpiry", "tokenHandOffAt", "signInState",
                "accountUuid", "tokenOrg", "tokenScope", *_ACCOUNT_STATE_KEYS]
_DETACH_CLEAR_SESSION = _DETACH_CLEAR_LOCAL = _DETACH_KEYS


def _inject_js(session: ChromeSession) -> str:
    local = {"accountUuid": session.account_uuid}
    # A tokenOrg we never captured is removed rather than left on the previous
    # account's org. Absent reads as a non-hybrid org in v1.0.107
    # (``refusesClearText`` treats a missing tokenOrg like ``hybrid: false``).
    clear_local = list(_INJECT_CLEAR_LOCAL)
    if session.token_org_uuid and session.token_org_hybrid is not None:
        local["tokenOrg"] = {"uuid": session.token_org_uuid, "hybrid": session.token_org_hybrid}
    else:
        clear_local.append("tokenOrg")
    if session.token_scope:
        local["tokenScope"] = session.token_scope
    return (
        "(async () => {"
        "await chrome.storage.session.set({"
        f"accessToken: {json.dumps(session.access_token)},"
        f"refreshToken: {json.dumps(session.refresh_token)},"
        f"tokenExpiry: {json.dumps(session.token_expiry)}}});"
        f"await chrome.storage.session.remove({json.dumps(_INJECT_CLEAR_SESSION)});"
        f"await chrome.storage.local.remove({json.dumps(clear_local)});"
        f"await chrome.storage.local.set({json.dumps(local)});"
        "return 'ok';})()"
    )


_DETACH_JS = (
    "(async () => {"
    f"await chrome.storage.session.remove({json.dumps(_DETACH_CLEAR_SESSION)});"
    f"await chrome.storage.local.remove({json.dumps(_DETACH_CLEAR_LOCAL)});"
    "return 'ok';})()"
)


def _restart_sw(port: int, sw_id: str | None) -> str | None:
    """Terminate the SW (keeps storage.session) and wake it with a background
    claude.ai tab. Returns the wake tab's id (caller closes it)."""
    _close_target(port, sw_id)
    _wait_for_sw_gone(port, sw_id)  # on timeout, wake anyway; adoption is checked after
    wake_id = _create_target(port, CLAUDE_URL, background=True)
    _wait_for_sw(port)
    return wake_id


def _adopted(data: dict | None, session: ChromeSession) -> bool:
    """Whether the extension holds ``session``'s account and has not failed on it.

    A failed adoption leaves a ``lastAuthFailureReason`` and drops the refresh
    token, so both are required to be clean. The profile email is checked only
    when present: the extension re-fetches the profile on its own schedule
    (browser start-up, auth changes), not necessarily on a SW restart.
    """
    if not data or data.get("authFailure"):
        return False
    if not (data.get("accessToken") and data.get("refreshToken")):
        return False
    if session.account_uuid:
        if data.get("accountUuid") != session.account_uuid:
            return False
    elif not (session.email and data.get("email")):
        # Nothing the injection wrote identifies the account: only a profile
        # fetched with the token can.
        return False
    if session.email and data.get("email") and data["email"].lower() != session.email.lower():
        return False
    return True


def inject_tokens_cdp(port: int, session: ChromeSession, *, settle: float = 10.0) -> bool:
    """Make the running extension adopt ``session``'s account.

    Writes the tokens into the SW's storage, terminates the SW with
    ``Target.closeTarget`` (which preserves ``storage.session``, unlike a runtime
    reload), then wakes it with a throwaway background claude.ai tab so it
    restarts, reads the injected tokens, and reconnects the bridge. The wake tab
    is closed afterwards, so no claude.ai tab lingers. Returns True if the
    extension came back up on the injected account.
    """
    if not session.is_usable():
        return False
    t = _sw_target(port)
    if not t:
        _logger.warning("chrome-sync: extension service worker not found on port %s", port)
        return False
    sw_id = t.get("id")
    # 1) Write the tokens into the SW's storage.
    if _eval_in_sw(port, _inject_js(session)) != "ok":
        _logger.warning("chrome-sync: failed to write tokens into the extension")
        return False
    # 2) Terminate the SW (NOT runtime.reload → keeps storage.session), then
    # 3) wake it with a background claude.ai tab so it restarts + reconnects.
    wake_id = _restart_sw(port, sw_id)
    # Poll for the whole settle window unless the outcome is already certain:
    # an auth failure (the token was rejected) or a re-fetched profile email
    # (only the injected token can fetch it, since the cache was dropped).
    data: dict | None = None
    deadline = time.monotonic() + settle
    while True:
        data = _read_state_cdp(port)
        if data and (data.get("authFailure") or (_adopted(data, session) and data.get("email"))):
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(0.25)
    if wake_id:
        _close_target(port, wake_id)
    ok = _adopted(data, session)
    if not ok:
        got = (data or {}).get("authFailure") or (data or {}).get("email") or "nothing"
        _logger.warning("chrome-sync: extension did not adopt the injected account (got %s)", got)
    return ok


def detach_extension_cdp(port: int) -> bool:
    """Sign the extension out locally, without revoking its tokens.

    The extension's own sign-out revokes every refresh token it holds, which
    kills the stored copy of that account. Clearing its storage and restarting
    the SW leaves it signed out while the tokens stay valid server-side.
    """
    t = _sw_target(port)
    if not t:
        return False
    if _eval_in_sw(port, _DETACH_JS) != "ok":
        return False
    wake_id = _restart_sw(port, t.get("id"))
    if wake_id:
        _close_target(port, wake_id)
    # The restarted SW must come back up signed out (no tokens re-appearing).
    for _ in range(5):
        data = _read_state_cdp(port)
        if data is not None:
            return not (data.get("accessToken") or data.get("refreshToken"))
        time.sleep(1)
    return False


# --------------------------------------------------------------------------- #
# claude.ai web login (cookies) — kept on the same account as the extension
# --------------------------------------------------------------------------- #
CLAUDE_COOKIE_URL = "https://claude.ai"
# The cookies that make up an account's web login. Everything else on
# claude.ai (Cloudflare clearance, device id, analytics, UI prefs) belongs to
# the browser and stays put.
WEB_IDENTITY_COOKIES = frozenset({"sessionKey", "sessionKeyLC", "routingHint", "lastActiveOrg"})
# Short-lived cookies the server binds to the current login: dropped on a
# switch so it issues fresh ones for the new login instead of rejecting them.
WEB_BOUND_COOKIES = frozenset({"__Host-sessionBinding", "__Host-sessionBindingGrace",
                               "activitySessionId"})

_WEB_IDENTITY_JS = r"""
(async () => {
  try {
    const r = await fetch('https://claude.ai/api/account',
                          {credentials: 'include', signal: AbortSignal.timeout(4000)});
    if (!r.ok) return JSON.stringify({status: r.status});
    const j = await r.json();
    return JSON.stringify({status: r.status, uuid: j.uuid || '', email: j.email_address || ''});
  } catch (e) { return JSON.stringify({status: 0}); }
})()
"""


def read_web_identity_cdp(port: int) -> dict | None:
    """Whose claude.ai web login the browser holds: ``{"uuid", "email"}``.

    Fetched by the extension's SW, which sends the browser's claude.ai
    cookies (``<all_urls>`` host permission). None when signed out or
    unreachable.
    """
    for _ in range(2):  # one retry when claude.ai does not answer
        raw = _eval_in_sw(port, _WEB_IDENTITY_JS, timeout=6.0)
        try:
            data = json.loads(raw) if raw else None
        except (TypeError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict) and data.get("status") == 200 and data.get("uuid"):
            return {"uuid": data["uuid"], "email": data.get("email", "")}
        if isinstance(data, dict) and data.get("status"):
            return None  # answered: not signed in
    return None


def _browser_call(port: int, method: str, params: dict | None = None) -> dict:
    """One CDP call on the browser target, retried once: Chrome answers
    ``Browser context management is not supported`` now and then while
    another client or the extension's SW is attaching."""
    for attempt in range(2):
        bws = _browser_ws(port)
        if not bws:
            raise RuntimeError("automation Chrome is not reachable")
        cdp = _CDP(bws)
        try:
            return cdp.call(method, params or {})
        except RuntimeError:
            if attempt:
                raise
            time.sleep(0.5)
        finally:
            cdp.close()
    raise AssertionError("unreachable")


def _claude_cookies(port: int) -> list[dict]:
    cookies = _browser_call(port, "Storage.getCookies").get("cookies", [])
    return [c for c in cookies if c.get("domain", "").lstrip(".") == "claude.ai"]


def _cookie_param(c: dict) -> dict:
    """A CDP cookie → a ``Storage.setCookies`` param that recreates it exactly.

    A host-only cookie (domain without a leading dot, e.g. ``__Host-`` ones)
    is set by URL, since passing a domain would make it a domain cookie.
    """
    param = {"name": c["name"], "value": c["value"], "path": c.get("path", "/"),
             "secure": bool(c.get("secure")), "httpOnly": bool(c.get("httpOnly"))}
    if c.get("domain", "").startswith("."):
        param["domain"] = c["domain"]
    else:
        param["url"] = CLAUDE_COOKIE_URL + param["path"]
    if c.get("sameSite"):
        param["sameSite"] = c["sameSite"]
    if not c.get("session") and c.get("expires", -1) > 0:
        param["expires"] = c["expires"]
    return param


def capture_web_cookies_cdp(port: int) -> tuple[str, list[dict]] | None:
    """``(account uuid, identity cookies)`` of the browser's claude.ai login."""
    got = capture_web_login_cdp(port)
    return (got[0], got[2]) if got else None


def capture_web_login_cdp(port: int) -> tuple[str, str, list[dict]] | None:
    """``(account uuid, email, identity cookies)`` of the browser's claude.ai login."""
    ident = read_web_identity_cdp(port)
    if not ident:
        return None
    cookies = [_cookie_param(c) for c in _claude_cookies(port) if c["name"] in WEB_IDENTITY_COOKIES]
    if not any(c["name"] == "sessionKey" for c in cookies):
        return None
    return ident["uuid"], ident.get("email", ""), cookies


def match_slot(slots, email: str, org_uuid: str = "") -> str | None:
    """The one managed slot for ``email`` (and ``org_uuid`` when known).

    ``slots`` is ``[(num, email, organizationUuid), ...]``. One email can hold
    several slots (one per org), so the org narrows it; None unless exactly
    one slot is left.
    """
    if not email:
        return None
    hits = [num for num, e, o in slots if (e or "").lower() == email.lower()]
    if org_uuid and len(hits) > 1:
        hits = [num for num, e, o in slots if (e or "").lower() == email.lower()
                and (o or "").lower() == org_uuid.lower()]
    return hits[0] if len(hits) == 1 else None


def apply_web_cookies_cdp(port: int, cookies: list[dict]) -> None:
    """Replace the browser's claude.ai login with ``cookies`` (an empty list
    signs the web out locally). Only identity and login-bound cookies are
    touched; nothing is sent to claude.ai, so the replaced login stays valid.
    """
    expired = [{**_cookie_param(c), "value": "", "expires": 1}
               for c in _claude_cookies(port)
               if c["name"] in WEB_IDENTITY_COOKIES or c["name"] in WEB_BOUND_COOKIES]
    if expired:
        _browser_call(port, "Storage.setCookies", {"cookies": expired})
    if cookies:
        _browser_call(port, "Storage.setCookies", {"cookies": cookies})


# --------------------------------------------------------------------------- #
# Offline token refresh (keep-alive; no browser needed)
# --------------------------------------------------------------------------- #
REFRESH_OK = "ok"
REFRESH_REJECTED = "rejected"   # the token endpoint refused the refresh token
REFRESH_FAILED = "failed"       # network / unexpected response; may succeed later


def refresh_session(session: ChromeSession, *, timeout: float = 30.0) -> ChromeSession | None:
    """:func:`refresh_session_detailed` without the outcome."""
    return refresh_session_detailed(session, timeout=timeout)[0]


def refresh_session_detailed(
    session: ChromeSession, *, timeout: float = 30.0
) -> tuple[ChromeSession | None, str]:
    """Refresh a stored session's tokens offline via the OAuth token endpoint.

    Rotates the refresh token (the old one is invalidated server-side) and
    returns an updated ChromeSession with a fresh ~28.5 day window. Uses the
    system ``curl`` (as the old cookie path used ``openssl``) so no CA-bundle /
    third-party HTTP dependency is needed. Returns ``(session, REFRESH_OK)``,
    or ``(None, REFRESH_REJECTED)`` when the endpoint answers ``invalid_grant``
    (revoked or expired refresh token: only a re-capture helps), or
    ``(None, REFRESH_FAILED)`` for anything that may succeed on a retry.
    """
    if not session.refresh_token:
        return None, REFRESH_REJECTED
    try:
        proc = subprocess.run(
            [
                "curl", "-sS", "-X", "POST", OAUTH_TOKEN_URL,
                "-H", "Content-Type: application/x-www-form-urlencoded",
                "--data-urlencode", "grant_type=refresh_token",
                "--data-urlencode", f"client_id={OAUTH_CLIENT_ID}",
                "--data-urlencode", f"refresh_token={session.refresh_token}",
                "--data-urlencode", f"expires_in={OAUTH_REQUESTED_EXPIRES_IN}",
            ],
            capture_output=True, text=True, timeout=timeout,
        )
        data = json.loads(proc.stdout or "{}")
    except Exception as e:  # noqa: BLE001
        _logger.warning("chrome-sync: token refresh call failed: %s", e)
        return None, REFRESH_FAILED
    if not data.get("access_token"):
        err = (data.get("error") or {})
        _logger.info("chrome-sync: token refresh rejected: %s",
                     err.get("message") if isinstance(err, dict) else err or "no access_token")
        # invalid_grant = the refresh token was revoked (e.g. the extension
        # signed out) or expired; nothing but a re-capture brings it back.
        return None, (REFRESH_REJECTED if "invalid_grant" in (proc.stdout or "") else REFRESH_FAILED)
    now = int(time.time() * 1000)
    account = data.get("account") or {}
    org = data.get("organization") or {}
    return replace(
        session,
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token") or session.refresh_token,
        token_expiry=now + 1000 * int(data.get("expires_in", 0) or 0),
        account_uuid=account.get("uuid") or session.account_uuid,
        org_uuid=org.get("uuid") or session.org_uuid,
        email=account.get("email_address") or session.email,
        captured_at=now,
        refresh_dead=False,
    ), REFRESH_OK


def _is_stale(session: ChromeSession, days: int = KEEPALIVE_REFRESH_DAYS) -> bool:
    """Whether a stored session is old enough to refresh before its window closes."""
    if not session.captured_at:
        return False
    age_days = (time.time() * 1000 - session.captured_at) / (1000 * 86400)
    return age_days >= days


def _access_expired(session: ChromeSession, margin_ms: int = ACCESS_EXPIRY_MARGIN_MS) -> bool:
    """Whether the stored access token is expired (or about to be)."""
    return bool(session.token_expiry) and session.token_expiry <= time.time() * 1000 + margin_ms


# --------------------------------------------------------------------------- #
# Automation profile lifecycle
# --------------------------------------------------------------------------- #
#: Distinct name + toolbar color for the automation profile so its Chrome
#: window is recognizable and never confused with the user's real profile.
AUTOMATION_PROFILE_NAME = "Claude Code (cswap)"
AUTOMATION_THEME_COLOR = 0xFFD97757  # Claude terracotta (SkColor ARGB)

#: Web Store page for the Claude extension — opened on the fresh profile so the
#: user can install it in one click (the only manual, one-time setup step).
CLAUDE_EXTENSION_STORE_URL = (
    f"https://chromewebstore.google.com/detail/{CLAUDE_EXTENSION_ID}"
)


def ensure_automation_profile(dest_user_data_dir: Path, profile: str = "Default") -> None:
    """Ensure the dedicated automation user-data-dir exists — **fresh, not a clone**.

    A full clone of the user's profile dragged in every installed extension plus
    GBs of state, so the second Chrome was slow. Instead this creates an empty
    profile; the user installs only the Claude extension into it once from the
    Web Store. Idempotent and non-destructive.
    """
    (Path(dest_user_data_dir) / profile).mkdir(parents=True, exist_ok=True)


def has_claude_extension(user_data_dir: Path, profile: str = "Default") -> bool:
    """Whether the Claude extension is installed in the automation profile yet."""
    return (Path(user_data_dir) / profile / "Extensions" / CLAUDE_EXTENSION_ID).exists()


def set_profile_display_name(user_data_dir: Path, profile: str = "Default",
                             name: str = AUTOMATION_PROFILE_NAME,
                             theme_color: int = AUTOMATION_THEME_COLOR) -> None:
    """Set the profile's toolbar name + color so its window is recognizable.

    Chrome must be closed for the change to stick (it rewrites both files on
    exit). Best-effort; missing files (fresh profile, pre-first-launch) are
    silently ignored — a later launch applies it once the files exist.
    """
    local_state = Path(user_data_dir) / "Local State"
    try:
        data = json.loads(local_state.read_text(encoding="utf-8"))
        entry = data.setdefault("profile", {}).setdefault("info_cache", {}).setdefault(profile, {})
        entry["name"] = name
        entry["is_using_default_name"] = False
        local_state.write_text(json.dumps(data), encoding="utf-8")
    except (OSError, json.JSONDecodeError):
        pass
    prefs = Path(user_data_dir) / profile / "Preferences"
    try:
        data = json.loads(prefs.read_text(encoding="utf-8"))
        data.setdefault("profile", {})["name"] = name
        data["profile"]["using_default_name"] = False
        theme = data.setdefault("browser", {}).setdefault("theme", {})
        theme["user_color"] = theme_color
        theme["is_grayscale"] = False
        prefs.write_text(json.dumps(data), encoding="utf-8")
    except (OSError, json.JSONDecodeError):
        pass


def automation_chrome_running(port: int) -> bool:
    return cdp_ready(port)


def launch_automation_chrome(user_data_dir: Path, port: int,
                             profile: str = "Default",
                             start_url: str | None = None) -> "subprocess.Popen | None":
    """Launch the headed automation Chrome with remote debugging.

    Headed (not headless) is mandatory: the wake tab hits claude.ai, which
    Cloudflare blocks headless. ``--disable-sync`` keeps this lean, dedicated
    profile from pulling the user's synced extensions back in. With no
    ``start_url`` Chrome opens its normal New Tab Page. Runs alongside the user's
    normal Chrome (separate user-data-dir). Returns None if already up on `port`.
    """
    if not is_supported():
        return None
    if automation_chrome_running(port):
        return None
    argv = [
        CHROME_APP,
        f"--user-data-dir={user_data_dir}",
        f"--profile-directory={profile}",
        f"--remote-debugging-port={port}",
        "--disable-sync",
        "--no-first-run", "--no-default-browser-check",
        "--window-size=560,820",
    ]
    if start_url:
        argv.append(start_url)
    proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    wait_cdp_ready(port, timeout=25.0)
    return proc


def bring_automation_to_front(port: int) -> bool:
    """Raise + focus the automation Chrome's window (best-effort).

    The automation Chrome is a separate instance sharing the "Google Chrome"
    app, so AppleScript ``activate`` can't target it; CDP can via a page target.
    """
    pages = [t for t in _list_targets(port) if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    if not pages:
        return False
    cdp = _CDP(pages[0]["webSocketDebuggerUrl"])
    try:
        try:
            win = cdp.call("Browser.getWindowForTarget")
            wid = win.get("windowId")
            if wid is not None:
                cdp.call("Browser.setWindowBounds",
                         {"windowId": wid, "bounds": {"windowState": "normal"}})
        except Exception:  # noqa: BLE001
            pass
        cdp.call("Page.bringToFront")
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        cdp.close()


# --------------------------------------------------------------------------- #
# Per-account vault (chrome_sessions.json beside claude-swap's other state)
# --------------------------------------------------------------------------- #
class ChromeVault:
    """Stores one ChromeSession (OAuth tokens) per account number.

    Bearer tokens (``sk-ant-oat01-``/``sk-ant-ort01-``) live here in plaintext,
    chmod 600 — the same trust model as claude-swap's own credential backups,
    kept in a separate file (different credential type + lifecycle).
    """

    def __init__(self, backup_root: Path):
        self._path = Path(backup_root) / "chrome_sessions.json"

    def _read(self) -> dict:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _write(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        if sys.platform != "win32":
            os.chmod(self._path, 0o600)

    def get(self, account_num: str) -> ChromeSession | None:
        data = self._read().get(str(account_num))
        if not data:
            return None
        session = ChromeSession.from_dict(data)
        return session if session.is_usable() else None

    def account_numbers(self) -> set[str]:
        """Account numbers that have a usable stored session (for list markers)."""
        return {k for k, v in self._read().items() if ChromeSession.from_dict(v).is_usable()}

    def find_by_uuid(self, account_uuid: str) -> str | None:
        """The account number whose stored session has this accountUuid, if any."""
        if not account_uuid:
            return None
        for num, v in self._read().items():
            if v.get("accountUuid") == account_uuid:
                return num
        return None

    def put(self, account_num: str, session: ChromeSession) -> None:
        data = self._read()
        data[str(account_num)] = session.to_dict()
        self._write(data)

    def delete(self, account_num: str) -> None:
        data = self._read()
        if data.pop(str(account_num), None) is not None:
            self._write(data)


class WebVault(ChromeVault):
    """Each account's claude.ai web-login cookies, chmod 600.

    ``{"<n>": {"accountUuid": ..., "email": ..., "cookies": [...], "capturedAt": ms}}``.
    A ``sessionKey`` signs in to claude.ai as that account, so it gets the
    same protection as the OAuth tokens, in its own file.
    """

    def __init__(self, backup_root: Path):
        self._path = Path(backup_root) / "chrome_web_sessions.json"

    def get_web(self, account_num: str) -> dict | None:
        entry = self._read().get(str(account_num))
        return entry if isinstance(entry, dict) and entry.get("cookies") else None

    def put_web(self, account_num: str, account_uuid: str, email: str, cookies: list[dict]) -> None:
        data = self._read()
        data[str(account_num)] = {"accountUuid": account_uuid, "email": email,
                                  "cookies": cookies, "capturedAt": int(time.time() * 1000)}
        self._write(data)

    def account_numbers(self) -> set[str]:
        return {k for k, v in self._read().items() if isinstance(v, dict) and v.get("cookies")}


# --------------------------------------------------------------------------- #
# Facade used by claude-swap's switcher (the only surface switcher.py imports)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ChromeSyncSettings:
    """The ``chrome`` section of settings.json (see settings.load_chrome_settings)."""

    enabled: bool = False
    debug_port: int = 9333
    automation_dir: str = ""          # dedicated user-data-dir; default derived under backup_root
    source_profile: str = "Default"   # retained for settings compatibility (unused by token path)
    auto_launch: bool = True          # launch the automation Chrome on demand during a switch
    bridge_port: int = 8765           # local bridge relay port (LOCAL_BRIDGE=1 targets this)


def resolve_automation_dir(settings: "ChromeSyncSettings", backup_root: Path) -> Path:
    """The dedicated automation user-data-dir: the configured path, else a
    default beside claude-swap's other state."""
    return Path(settings.automation_dir) if settings.automation_dir \
        else Path(backup_root) / "chrome-automation"


class ChromeSync:
    """Best-effort browser-side companion to a claude-swap account switch.

    Every method swallows its own failures (logged) and returns a bool/None: a
    dead browser or a Cloudflare hiccup must never fail the OAuth switch that
    already committed. Construct once per switcher with the loaded settings +
    backup_root; a no-op when disabled/unsupported.
    """

    def __init__(self, settings: ChromeSyncSettings, backup_root: Path):
        self._s = settings
        self._vault = ChromeVault(backup_root)
        self._web = WebVault(backup_root)
        self._web_clearable = False  # set by _snapshot_web, read by _apply_web
        self._backup_root = Path(backup_root)
        # Background mode (the menu bar): a switch hands the browser sync to a
        # worker thread instead of waiting the seconds an injection takes.
        self.background = False
        self._bg_lock = threading.Lock()
        self._bg_target: str | None = None
        self._bg_thread: threading.Thread | None = None

    def follow_switch(self, account_num: str) -> None:
        """Move the browser to ``account_num`` after a committed CLI switch.

        Inline by default. In background mode it returns at once and a single
        worker performs the sync; switches made while it runs collapse into
        one sync to the latest target, so the browser ends on the account the
        CLI ended on.
        """
        if not self.active:
            return
        if not self.background:
            self.sync_to_account(account_num)
            return
        with self._bg_lock:
            self._bg_target = account_num
            if self._bg_thread is None or not self._bg_thread.is_alive():
                self._bg_thread = threading.Thread(
                    target=self._bg_worker, name="chrome-sync", daemon=True)
                self._bg_thread.start()

    def _bg_worker(self) -> None:
        while True:
            with self._bg_lock:
                target, self._bg_target = self._bg_target, None
                if target is None:
                    self._bg_thread = None
                    return
            self.sync_to_account(target)  # swallows and logs its own failures

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Block until the background worker has nothing left to do."""
        with self._bg_lock:
            t = self._bg_thread
        if t is not None:
            t.join(timeout)
        with self._bg_lock:
            return self._bg_thread is None or not self._bg_thread.is_alive()

    @property
    def active(self) -> bool:
        return bool(self._s.enabled) and is_supported()

    def _automation_dir(self) -> Path:
        return resolve_automation_dir(self._s, self._backup_root)

    def _ensure_up(self) -> bool:
        """Provision + launch the automation Chrome if needed; True if reachable."""
        if automation_chrome_running(self._s.debug_port):
            return True
        if not self._s.auto_launch:
            _logger.info("chrome-sync: automation Chrome not running on port %s", self._s.debug_port)
            return False
        try:
            udd = self._automation_dir()
            ensure_automation_profile(udd)
            # Chrome is down here, so the name/color edit sticks. No-op before the
            # first launch creates the files.
            set_profile_display_name(udd)
            # Until the extension is installed, land on its Web Store page so it's
            # one click away; afterwards open the normal New Tab Page.
            start = None if has_claude_extension(udd) else CLAUDE_EXTENSION_STORE_URL
            launch_automation_chrome(udd, self._s.debug_port, start_url=start)
        except Exception as e:  # noqa: BLE001
            _logger.warning("chrome-sync: could not start automation Chrome: %s", e)
            return False
        return automation_chrome_running(self._s.debug_port)

    def _snapshot_current(self) -> str | None:
        """Re-capture the account the extension is *currently* on into its vault
        slot, before we inject a different one.

        The active account's access token rotates roughly every 8h, so its stored
        copy goes stale; snapshotting on the way out keeps the vault fresh and
        avoids ever re-injecting a dead token. Matched to a slot by accountUuid.
        The claude.ai login is stored the same way; returns the slot it
        belongs to (None if signed out or unmanaged).
        """
        try:
            current = capture_tokens_cdp(self._s.debug_port)
            if current:
                num = self._vault.find_by_uuid(current.account_uuid)
                if num:
                    self._vault.put(num, current)
                    _logger.info("chrome-sync: refreshed stored tokens for account %s (outgoing)", num)
        except Exception as e:  # noqa: BLE001
            _logger.info("chrome-sync: could not snapshot current account: %s", e)
        return self._snapshot_web()

    def _snapshot_web(self, slots=()) -> str | None:
        """Store the browser's claude.ai login under the slot it belongs to.

        The slot is found by the login's own account uuid (``/api/account``)
        in the token vault, never assumed to be the extension's account; for
        an account with no stored tokens yet, by its email among ``slots``
        (only when exactly one slot has it). Returns that slot.
        """
        self._web_clearable = False
        try:
            got = capture_web_login_cdp(self._s.debug_port)
            if not got:
                return None
            uuid, email, cookies = got
            num = self._vault.find_by_uuid(uuid)
            if num is None:
                num = match_slot(slots, email)
                stored = self._vault.get(num) if num else None
                if stored is not None and stored.account_uuid and stored.account_uuid != uuid:
                    num = None  # the slot's tokens belong to another account uuid
            if num:
                self._web.put_web(num, uuid, email, cookies)
                self._web_clearable = True
            return num
        except Exception as e:  # noqa: BLE001
            _logger.info("chrome-sync: could not snapshot the claude.ai login: %s", e)
            return None

    def _apply_web(self, account_num: str) -> None:
        """Put ``account_num``'s claude.ai login in the browser.

        With none stored, the web is signed out locally rather than left on
        another account, which the extension rejects; only when the login
        being replaced was just stored, so none is lost.
        """
        entry = self._web.get_web(account_num)
        clearable, self._web_clearable = self._web_clearable, False
        if entry is None:
            _logger.info("chrome-sync: no claude.ai login stored for account %s; sign in to "
                         "claude.ai as it in the Claude Browser, then `cswap chrome capture`",
                         account_num)
            if not clearable:
                return
        try:
            apply_web_cookies_cdp(self._s.debug_port, entry["cookies"] if entry else [])
        except Exception as e:  # noqa: BLE001
            _logger.warning("chrome-sync: could not switch the claude.ai login: %s", e)

    def capture_checked(
        self, account_num: str, expected_org_uuid: str = "", expected_email: str = ""
    ) -> str:
        """Capture the extension's current account into ``account_num``'s slot.

        Returns "" on success, else a one-line reason. ``expected_org_uuid`` /
        ``expected_email`` are the slot's Claude Code identity; when given, the
        extension must be connected to that same account, so a capture can never
        file one account's browser session under another account's slot. (A Team
        org holds several members, so the email is checked as well as the org.)
        """
        if not self.active:
            return "Chrome sync is disabled"
        if not automation_chrome_running(self._s.debug_port):
            return "the Claude Browser is not running (open it with `cswap chrome open`)"
        try:
            return self._capture_tokens_into(account_num, expected_org_uuid, expected_email)
        finally:
            # After the tokens, so a first capture's uuid can file the web
            # login; it is filed by its own uuid, whichever account the
            # extension is on.
            self._snapshot_web(((account_num, expected_email, expected_org_uuid),)
                               if expected_email else ())

    def _capture_tokens_into(
        self, account_num: str, expected_org_uuid: str, expected_email: str
    ) -> str:
        try:
            session = capture_tokens_cdp(self._s.debug_port)
        except Exception as e:  # noqa: BLE001
            _logger.warning("chrome-sync: capture failed for account %s: %s", account_num, e)
            return f"capture failed: {e}"
        if session is None:
            return "the extension is not signed in (or its last sign-in failed)"
        if expected_email and session.email.lower() != expected_email.lower():
            return (f"the extension is connected to {session.email or 'an account not loaded yet'}, "
                    f"not {expected_email}")
        if expected_org_uuid and session.org_uuid != expected_org_uuid:
            return (f"the extension is connected to org {session.org_uuid or '(not loaded yet)'}, "
                    f"not this account's org {expected_org_uuid}")
        self._vault.put(account_num, session)
        _logger.info("chrome-sync: captured extension tokens for account %s (%s)",
                     account_num, session.email or session.account_uuid)
        return ""

    def capture_auto(self, slots) -> dict:
        """Capture whatever the browser holds into the slots it belongs to.

        The extension's account goes to the slot with its email (and org);
        the claude.ai login to the slot with its account uuid or email. They
        need not be the same account. Returns ``{"extension": num | None,
        "web": num | None, "reason": str}``; ``reason`` explains a missing
        extension capture.
        """
        out: dict = {"extension": None, "web": None, "reason": ""}
        if not self.active:
            out["reason"] = "Chrome sync is disabled"
            return out
        if not automation_chrome_running(self._s.debug_port):
            out["reason"] = "the Claude Browser is not running (open it with `cswap chrome open`)"
            return out
        try:
            session = capture_tokens_cdp(self._s.debug_port)
            if session is None:
                out["reason"] = "the extension is not signed in (or its last sign-in failed)"
            else:
                num = match_slot(slots, session.email, session.org_uuid)
                if num is None:
                    out["reason"] = (f"no single managed account matches the extension's "
                                     f"{session.email or 'account'} (add it with `cswap add`)")
                else:
                    self._vault.put(num, session)
                    out["extension"] = num
                    _logger.info("chrome-sync: captured extension tokens for account %s (%s)",
                                 num, session.email or session.account_uuid)
        except Exception as e:  # noqa: BLE001
            out["reason"] = f"capture failed: {e}"
        out["web"] = self._snapshot_web(slots)
        return out

    def web_stored(self, account_num: str) -> bool:
        return self._web.get_web(account_num) is not None

    def web_stored_at(self, account_num: str) -> int:
        """When ``account_num``'s web login was last stored (ms), 0 if never."""
        entry = self._web.get_web(account_num)
        return int(entry.get("capturedAt", 0)) if entry else 0

    def has_session(self, account_num: str) -> bool:
        return self._vault.get(account_num) is not None

    def capture_for_account(
        self, account_num: str, expected_org_uuid: str = "", expected_email: str = ""
    ) -> bool:
        """Best-effort capture with the identity guard (called from add_account).

        Because the tokens live in the extension's in-memory storage, this reads
        them from the running automation Chrome via CDP (no offline path).
        """
        reason = self.capture_checked(account_num, expected_org_uuid, expected_email)
        if reason:
            _logger.info("chrome-sync: not capturing account %s: %s; connect it in the Claude "
                         "Browser, then `cswap chrome capture %s`", account_num, reason, account_num)
        return not reason

    # Unguarded capture, for `cswap chrome capture N --force` only.
    def capture_from_browser(self, account_num: str) -> bool:
        return not self.capture_checked(account_num)

    def detach_web(self) -> bool:
        """Sign the claude.ai web login out locally, after storing it.

        For signing in to claude.ai as another account: its own sign-out
        would end the stored login server-side.
        """
        if not self.active or not automation_chrome_running(self._s.debug_port):
            return False
        try:
            self._snapshot_web()
            apply_web_cookies_cdp(self._s.debug_port, [])
            return read_web_identity_cdp(self._s.debug_port) is None
        except Exception as e:  # noqa: BLE001
            _logger.warning("chrome-sync: web detach failed: %s", e)
            return False

    def detach(self) -> bool:
        """Sign the extension out locally (no revoke), after storing its account.

        The outgoing account is snapshotted first, so its stored tokens are the
        ones it held at detach time.
        """
        if not self.active or not automation_chrome_running(self._s.debug_port):
            return False
        try:
            self._snapshot_current()
            # Sign the web out locally as well: connecting the next account in
            # the extension goes through the claude.ai login.
            apply_web_cookies_cdp(self._s.debug_port, [])
            return detach_extension_cdp(self._s.debug_port)
        except Exception as e:  # noqa: BLE001
            _logger.warning("chrome-sync: detach failed: %s", e)
            return False

    def _live_account_uuid(self) -> str:
        """The account the running extension holds tokens for, or ""."""
        if not automation_chrome_running(self._s.debug_port):
            return ""
        data = _read_state_cdp(self._s.debug_port) or {}
        return data.get("accountUuid", "") if data.get("refreshToken") else ""

    def _refresh_stored(self, account_num: str, session: ChromeSession) -> ChromeSession | None:
        refreshed, outcome = refresh_session_detailed(session)
        if refreshed is not None:
            self._vault.put(account_num, refreshed)
            return refreshed
        if outcome == REFRESH_REJECTED and not session.refresh_dead:
            self._vault.put(account_num, replace(session, refresh_dead=True))
        return None

    def refresh_account(self, account_num: str) -> str:
        """Offline-refresh a stored account's tokens (keep-alive). No browser.

        Returns "" on success, else a one-line reason. Refuses the account the
        extension is on right now: rotating its refresh token would invalidate
        the one the extension holds in memory.
        """
        if not self.active:
            return "Chrome sync is disabled"
        session = self._vault.get(account_num)
        if session is None:
            return "no stored browser session for this account"
        if session.account_uuid and self._live_account_uuid() == session.account_uuid:
            return ("the Claude Browser is on this account right now; it keeps its own "
                    "tokens fresh")
        if self._refresh_stored(account_num, session) is None:
            stored = self._vault.get(account_num)
            if stored is not None and stored.refresh_dead:
                return (f"the refresh token was revoked or expired; connect the account in the "
                        f"Claude Browser and run `cswap chrome capture {account_num}`")
            return "the token endpoint could not be reached; try again later"
        _logger.info("chrome-sync: refreshed stored tokens for account %s", account_num)
        return ""

    def sync_to_account(self, account_num: str) -> bool:
        """Inject the account's stored tokens into the extension and reconnect it.

        True only once the extension is authenticated as that account; the
        vault then holds the tokens the extension settled on.
        """
        if not self.active:
            return False
        session = self._vault.get(account_num)
        if session is None:
            _logger.info("chrome-sync: no stored session for account %s; skipping", account_num)
            return False
        if session.refresh_dead:
            _logger.warning("chrome-sync: account %s's browser session was revoked; re-capture "
                            "it with `cswap chrome capture %s`", account_num, account_num)
            return False
        try:
            if not self._ensure_up():
                return False
            port = self._s.debug_port
            if _adopted(_read_state_cdp(port), session):
                # Already there: keep the stored copy current, inject nothing.
                if self._snapshot_current() != account_num:
                    self._apply_web(account_num)
                return True
            # Keep the outgoing account's stored tokens and web login fresh
            # before we overwrite them.
            web_slot = self._snapshot_current()
            # The web login first: the wake tab then loads claude.ai as the
            # target, and a silent re-auth runs through the right login.
            if web_slot != account_num:
                self._apply_web(account_num)
            # The snapshot may have just stored newer tokens for this very
            # account (e.g. the extension was on it but not yet confirmed);
            # never act on the copy read before it.
            session = self._vault.get(account_num) or session
            # Refresh before injecting when the window is closing or the access
            # token is already dead, so adoption never starts from a dead token.
            if _is_stale(session) or _access_expired(session):
                session = self._refresh_stored(account_num, session) or session
            ok = inject_tokens_cdp(port, session)
            if not ok:
                # The extension may have failed on tokens past its own refresh;
                # refresh offline and re-inject once.
                refreshed = self._refresh_stored(account_num, session)
                if refreshed:
                    ok = inject_tokens_cdp(port, refreshed)
            settled = capture_tokens_cdp(port) if ok else None
            if settled is None or (session.account_uuid
                                   and settled.account_uuid != session.account_uuid):
                _logger.warning("chrome-sync: could not switch browser to account %s "
                                "(re-login may be needed)", account_num)
                return False
            # Store what the extension settled on: it may have rotated the
            # refresh token while adopting, which invalidates the stored one.
            self._vault.put(account_num, replace(
                settled,
                token_org_uuid=settled.token_org_uuid or session.token_org_uuid,
                token_org_hybrid=(settled.token_org_hybrid if settled.token_org_hybrid is not None
                                  else session.token_org_hybrid),
                token_scope=settled.token_scope or session.token_scope,
            ))
            _logger.info("chrome-sync: switched browser to account %s", account_num)
            return True
        except Exception as e:  # noqa: BLE001
            _logger.warning("chrome-sync: sync failed for account %s: %s", account_num, e)
            return False

    def open_and_activate(self, account_num: str, focus: bool = False) -> bool:
        """Ensure the automation Chrome is up and on ``account_num``.

        ``focus`` (opt-in) raises the window; it defaults off so neither the
        launcher nor a switch steals focus. When the browser is already running
        this only ensures it's up (no re-inject), so opening it when nothing
        changed doesn't disturb the current tab.
        """
        if not self.active:
            _logger.info("chrome-sync: disabled; not opening automation Chrome")
            return False
        already_running = automation_chrome_running(self._s.debug_port)
        self._ensure_up()
        # First-run: extension not installed yet — we're parked on its Web Store
        # page. Don't inject; let the user click "Add".
        if not has_claude_extension(self._automation_dir()):
            if focus:
                bring_automation_to_front(self._s.debug_port)
            _logger.info("chrome-sync: install the Claude extension in the opened window")
            return False
        # Fresh launch establishes the active account once; an already-running
        # browser is left as-is (no re-inject).
        ok = True if already_running else self.sync_to_account(account_num)
        if focus:
            bring_automation_to_front(self._s.debug_port)
        return ok

    def forget(self, account_num: str) -> None:
        try:
            self._vault.delete(account_num)
        except Exception as e:  # noqa: BLE001
            _logger.warning("chrome-sync: could not forget account %s: %s", account_num, e)
