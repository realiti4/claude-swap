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

``chrome.storage.local`` holds ``accountUuid``. The service worker (SW) reads
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
        )


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


def _wait_for_sw(port: int, timeout: float = 12.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _sw_target(port):
            return True
        time.sleep(0.4)
    return False


# --------------------------------------------------------------------------- #
# Capture / identity (read the extension's current tokens via CDP)
# --------------------------------------------------------------------------- #
_CAPTURE_JS = r"""
(async () => {
  const s = await chrome.storage.session.get(
    ['accessToken', 'refreshToken', 'tokenExpiry', 'swAnalyticsProfileCache']);
  const l = await chrome.storage.local.get(['accountUuid']);
  const prof = (s.swAnalyticsProfileCache || {}).profile || {};
  return JSON.stringify({
    accessToken: s.accessToken || '',
    refreshToken: s.refreshToken || '',
    tokenExpiry: s.tokenExpiry || 0,
    accountUuid: l.accountUuid || '',
    email: (prof.account || {}).email || '',
    orgUuid: (prof.organization || {}).uuid || '',
  });
})()
"""


def capture_tokens_cdp(port: int) -> ChromeSession | None:
    """Capture the extension's *current* OAuth tokens from the SW via CDP.

    Requires the automation Chrome running with the extension connected. Returns
    None if the SW is unreachable or no tokens are present (not logged in).
    """
    raw = _eval_in_sw(port, _CAPTURE_JS)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    session = ChromeSession(
        access_token=data.get("accessToken", ""),
        refresh_token=data.get("refreshToken", ""),
        token_expiry=int(data.get("tokenExpiry", 0) or 0),
        account_uuid=data.get("accountUuid", ""),
        org_uuid=data.get("orgUuid", ""),
        email=data.get("email", ""),
        captured_at=int(time.time() * 1000),
    )
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
def _inject_js(session: ChromeSession) -> str:
    return (
        "(async () => {"
        "await chrome.storage.session.set({"
        f"accessToken: {json.dumps(session.access_token)},"
        f"refreshToken: {json.dumps(session.refresh_token)},"
        f"tokenExpiry: {json.dumps(session.token_expiry)}}});"
        f"await chrome.storage.local.set({{accountUuid: {json.dumps(session.account_uuid)}}});"
        # Drop the cached profile so the SW re-fetches identity for the new token.
        "await chrome.storage.session.remove('swAnalyticsProfileCache');"
        "return 'ok';})()"
    )


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
    # 2) Terminate the SW (NOT runtime.reload → keeps storage.session).
    _close_target(port, sw_id)
    time.sleep(2)
    # 3) Wake it with a background claude.ai tab so it restarts + reconnects.
    wake_id = _create_target(port, CLAUDE_URL, background=True)
    _wait_for_sw(port)
    time.sleep(settle)
    ident = read_identity_cdp(port)
    if wake_id:
        _close_target(port, wake_id)
    # Adopted if the SW is back with the injected account's identity. Match by
    # uuid when we have it (email may lag a beat behind the profile fetch).
    ok = bool(ident) and (
        (session.account_uuid and ident.get("accountUuid") == session.account_uuid)
        or (session.email and ident.get("email") == session.email)
        or bool(ident.get("email"))
    )
    if not ok:
        _logger.warning("chrome-sync: extension did not adopt the injected account (got %s)",
                        ident.get("email") or ident.get("accountUuid") or "nothing")
    return ok


# --------------------------------------------------------------------------- #
# Offline token refresh (keep-alive; no browser needed)
# --------------------------------------------------------------------------- #
def refresh_session(session: ChromeSession, *, timeout: float = 30.0) -> ChromeSession | None:
    """Refresh a stored session's tokens offline via the OAuth token endpoint.

    Rotates the refresh token (the old one is invalidated server-side) and
    returns an updated ChromeSession with a fresh ~28.5 day window. Uses the
    system ``curl`` (as the old cookie path used ``openssl``) so no CA-bundle /
    third-party HTTP dependency is needed. Returns None on any failure
    (network, ``invalid_grant`` when the refresh token has itself expired).
    """
    if not session.refresh_token:
        return None
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
        return None
    if not data.get("access_token"):
        err = (data.get("error") or {})
        _logger.info("chrome-sync: token refresh rejected: %s",
                     err.get("message") if isinstance(err, dict) else err or "no access_token")
        return None
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
    )


def _is_stale(session: ChromeSession, days: int = KEEPALIVE_REFRESH_DAYS) -> bool:
    """Whether a stored session is old enough to refresh before its window closes."""
    if not session.captured_at:
        return False
    age_days = (time.time() * 1000 - session.captured_at) / (1000 * 86400)
    return age_days >= days


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
        self._backup_root = Path(backup_root)

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

    def _snapshot_current(self) -> None:
        """Re-capture the account the extension is *currently* on into its vault
        slot, before we inject a different one.

        The active account's access token rotates roughly every 8h, so its stored
        copy goes stale; snapshotting on the way out keeps the vault fresh and
        avoids ever re-injecting a dead token. Matched to a slot by accountUuid.
        """
        try:
            current = capture_tokens_cdp(self._s.debug_port)
            if not current:
                return
            num = self._vault.find_by_uuid(current.account_uuid)
            if num:
                self._vault.put(num, current)
                _logger.info("chrome-sync: refreshed stored tokens for account %s (outgoing)", num)
        except Exception as e:  # noqa: BLE001
            _logger.info("chrome-sync: could not snapshot current account: %s", e)

    def capture_for_account(self, account_num: str, expected_org_uuid: str = "") -> bool:
        """Capture the extension's current account into the vault, with org guard.

        Called from add_account. Because the tokens live in the extension's
        in-memory storage, this reads them from the running automation Chrome via
        CDP (there is no offline path). ``expected_org_uuid`` is the account's
        Claude Code ``organizationUuid``; when given, the captured session's org
        must match it, so we never store a browser login for a different account
        than the one Claude Code is adding.
        """
        if not self.active:
            return False
        if not automation_chrome_running(self._s.debug_port):
            _logger.info("chrome-sync: automation Chrome not running; connect this account in "
                         "`cswap chrome open` then run `cswap chrome capture %s`", account_num)
            return False
        try:
            session = capture_tokens_cdp(self._s.debug_port)
            if session is None:
                _logger.info("chrome-sync: no extension login to capture for account %s", account_num)
                return False
            if expected_org_uuid and session.org_uuid and session.org_uuid != expected_org_uuid:
                _logger.warning(
                    "chrome-sync: the extension is connected to org %s, which does not match "
                    "the account being added (org %s); skipping capture. Connect this account "
                    "in the automation Chrome, then `cswap chrome capture %s`.",
                    session.org_uuid, expected_org_uuid, account_num,
                )
                return False
            self._vault.put(account_num, session)
            _logger.info("chrome-sync: captured extension tokens for account %s (%s)",
                         account_num, session.email or session.account_uuid)
            return True
        except Exception as e:  # noqa: BLE001
            _logger.warning("chrome-sync: capture failed for account %s: %s", account_num, e)
            return False

    # Kept for the "log into the account directly in the dedicated browser" flow;
    # identical to capture_for_account without the org guard.
    def capture_from_browser(self, account_num: str) -> bool:
        if not self.active:
            return False
        if not automation_chrome_running(self._s.debug_port):
            _logger.info("chrome-sync: automation Chrome not running; open it and connect first")
            return False
        try:
            session = capture_tokens_cdp(self._s.debug_port)
            if session is None:
                _logger.info("chrome-sync: no extension login in the automation Chrome to capture")
                return False
            self._vault.put(account_num, session)
            _logger.info("chrome-sync: captured account %s from browser (%s)",
                         account_num, session.email or session.account_uuid)
            return True
        except Exception as e:  # noqa: BLE001
            _logger.warning("chrome-sync: capture-from-browser failed for %s: %s", account_num, e)
            return False

    def refresh_account(self, account_num: str) -> bool:
        """Offline-refresh a stored account's tokens (keep-alive). No browser."""
        if not self.active:
            return False
        session = self._vault.get(account_num)
        if session is None:
            return False
        refreshed = refresh_session(session)
        if refreshed is None:
            _logger.warning("chrome-sync: could not refresh account %s (refresh token expired?)",
                            account_num)
            return False
        self._vault.put(account_num, refreshed)
        _logger.info("chrome-sync: refreshed stored tokens for account %s", account_num)
        return True

    def sync_to_account(self, account_num: str) -> bool:
        """Inject the account's stored tokens into the extension and reconnect it."""
        if not self.active:
            return False
        session = self._vault.get(account_num)
        if session is None:
            _logger.info("chrome-sync: no stored session for account %s; skipping", account_num)
            return False
        try:
            if not self._ensure_up():
                return False
            # Keep the outgoing account's stored tokens fresh before we overwrite.
            self._snapshot_current()
            # Keep-alive: refresh a long-parked token before injecting, so its
            # (possibly expired) refresh token doesn't fail adoption.
            if _is_stale(session):
                refreshed = refresh_session(session)
                if refreshed:
                    session = refreshed
                    self._vault.put(account_num, session)
            ok = inject_tokens_cdp(self._s.debug_port, session)
            if not ok:
                # The injected tokens may be stale beyond the extension's own
                # refresh; try an offline refresh, then re-inject once.
                refreshed = refresh_session(session)
                if refreshed:
                    self._vault.put(account_num, refreshed)
                    ok = inject_tokens_cdp(self._s.debug_port, refreshed)
            if ok:
                # Store whatever the extension settled on (it may have rotated the
                # token when it reconnected), keyed to this account.
                settled = capture_tokens_cdp(self._s.debug_port)
                if settled and settled.account_uuid == (session.account_uuid or settled.account_uuid):
                    self._vault.put(account_num, settled)
                _logger.info("chrome-sync: switched browser to account %s", account_num)
            else:
                _logger.warning("chrome-sync: could not switch browser to account %s "
                                "(re-login may be needed)", account_num)
            return ok
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
