"""Standalone browser OAuth login — add an account without the Claude Code login dance.

``cswap login`` runs Claude Code's own PKCE authorization-code flow directly
against ``claude.ai``, so a fresh account (or a second org of an email you are
already logged in as) lands in a slot without the usual log-out / log-in / add
ping-pong through Claude Code. Unlike ``add-token`` (setup-tokens, which are
inference-only server-side and so show no usage in ``list``), the full flow
returns the same broad-scope credential Claude Code itself gets — refresh token,
expiry, and the account/organization metadata needed for usage tracking and the
(email, org) identity key.

This reuses Claude Code's public OAuth client id and endpoints; it is a
reverse-engineered flow, not a supported API, so treat breakage as expected if
Anthropic changes the flow. Local-only helper — not part of the upstream package.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass

from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.oauth import OAUTH_CLIENT_ID, OAUTH_TOKEN_URL
from claude_swap.printer import accent, dimmed, muted, warning

_logger = logging.getLogger("claude-swap")

# Claude Code's authorization endpoint and the exact-match redirect its client id
# is registered for. The redirect renders the authorization code on-screen for
# the manual copy-paste (out-of-band) flow, so no local callback server is needed.
AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"

# The broad scopes Claude Code requests — user:profile is what unlocks the usage
# API (setup-tokens carry only user:inference and 403 on the profile endpoints).
LOGIN_SCOPES = ("org:create_api_key", "user:profile", "user:inference")


class LoginError(ClaudeSwitchError):
    """The standalone OAuth login could not complete."""


@dataclass(frozen=True)
class LoginResult:
    """Everything a slot write needs from a completed login."""

    credentials: str  # Claude Code credential JSON: {"claudeAiOauth": {...}}
    email: str
    account_uuid: str
    organization_uuid: str
    organization_name: str


@dataclass(frozen=True)
class PendingLogin:
    """An authorization round-trip in flight: the URL is open in a browser,
    the pasted code hasn't come back yet. Holds the PKCE verifier and state
    that ``complete_login`` needs to finish the exchange."""

    verifier: str
    state: str
    url: str


# Browsers that open a fresh private/incognito window from the command line, in
# preference order. Each entry: (label, incognito flag, macOS app binary,
# POSIX binary names for shutil.which). A private window carries no claude.ai
# cookies, so you can log in as a second account without disturbing the one your
# normal browser is signed into. Safari and Arc are intentionally absent: neither
# exposes a reliable private-window CLI flag.
_PRIVATE_BROWSERS = (
    ("Chrome", "--incognito", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
     ("google-chrome", "google-chrome-stable", "chrome")),
    ("Brave", "--incognito", "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
     ("brave-browser", "brave")),
    ("Edge", "--inprivate", "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
     ("microsoft-edge", "microsoft-edge-stable")),
    ("Chromium", "--incognito", "/Applications/Chromium.app/Contents/MacOS/Chromium",
     ("chromium", "chromium-browser")),
    ("Vivaldi", "--incognito", "/Applications/Vivaldi.app/Contents/MacOS/Vivaldi",
     ("vivaldi", "vivaldi-stable")),
    ("Firefox", "--private-window", "/Applications/Firefox.app/Contents/MacOS/firefox",
     ("firefox",)),
)


def _resolve_private_browser() -> tuple[str, str, str] | None:
    """First installed private-capable browser as ``(label, binary, flag)``."""
    for label, flag, mac_bin, posix_names in _PRIVATE_BROWSERS:
        if sys.platform == "darwin":
            if os.path.exists(mac_bin):
                return label, mac_bin, flag
        else:
            for name in posix_names:
                found = shutil.which(name)
                if found:
                    return label, found, flag
    return None


def _open_private_browser(url: str) -> str | None:
    """Launch ``url`` in a private/incognito window; return the browser label or None.

    Direct-binary launch (not ``webbrowser``/``open -a``) so the flag is honored
    even when the browser is already running — it opens a new private window in
    the existing instance rather than a normal tab.
    """
    resolved = _resolve_private_browser()
    if resolved is None:
        return None
    label, binary, flag = resolved
    try:
        subprocess.Popen(
            [binary, flag, url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return label
    except OSError as e:
        _logger.debug("Private browser launch failed (%s): %r", binary, e)
        return None


def _b64url(raw: bytes) -> str:
    """Base64url without padding (PKCE / RFC 7636)."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _generate_pkce() -> tuple[str, str]:
    """Return ``(verifier, challenge)`` for the S256 PKCE method."""
    verifier = _b64url(os.urandom(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def _build_authorize_url(challenge: str, state: str) -> str:
    from urllib.parse import urlencode

    params = {
        "code": "true",
        "client_id": OAUTH_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": " ".join(LOGIN_SCOPES),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def _parse_pasted_code(pasted: str, fallback_state: str) -> tuple[str, str]:
    """Split the pasted callback value into ``(code, state)``.

    The callback page renders the code as ``<code>#<state>``; some surfaces show
    a bare code. Accept a full redirect URL too and pull ``code``/``state`` out.
    """
    pasted = pasted.strip()
    if pasted.startswith("http"):
        from urllib.parse import parse_qs, urlparse

        q = parse_qs(urlparse(pasted).query)
        code = (q.get("code") or [""])[0]
        state = (q.get("state") or [fallback_state])[0]
        return code, state
    if "#" in pasted:
        code, _, state = pasted.partition("#")
        return code.strip(), (state.strip() or fallback_state)
    return pasted, fallback_state


def _exchange_code(code: str, state: str, verifier: str) -> dict:
    """Trade the authorization code for tokens; returns the raw token response."""
    body = json.dumps({
        "grant_type": "authorization_code",
        "code": code,
        "state": state,
        "client_id": OAUTH_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": verifier,
    }).encode()
    req = urllib.request.Request(
        OAUTH_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "claude-swap/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace") if hasattr(e, "read") else ""
        _logger.debug("Token exchange failed: %r, body: %s", e, detail[:500])
        raise LoginError(
            f"Token exchange rejected (HTTP {e.code}). The code may have expired "
            f"or been mistyped — rerun 'cswap login' for a fresh one."
        ) from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise LoginError(f"Could not reach the token endpoint: {e}") from e


def _credentials_from_token_response(resp: dict) -> str:
    """Build Claude Code's credential JSON from the token endpoint response."""
    from datetime import datetime, timezone

    access_token = resp.get("access_token")
    if not access_token:
        raise LoginError("Token response contained no access_token.")
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    oauth: dict = {"accessToken": access_token}
    if resp.get("refresh_token"):
        oauth["refreshToken"] = resp["refresh_token"]
    if isinstance(resp.get("expires_in"), (int, float)):
        oauth["expiresAt"] = now_ms + int(resp["expires_in"]) * 1000
    scope = resp.get("scope")
    oauth["scopes"] = scope.split() if isinstance(scope, str) else list(LOGIN_SCOPES)
    return json.dumps({"claudeAiOauth": oauth})


def prepare_login(
    *, open_browser: bool = True, private: bool = False
) -> tuple[PendingLogin, str | None]:
    """Start an authorization round-trip: PKCE, URL, browser (best effort).

    Non-interactive half of the login, shared by the CLI and the TUI. Returns
    ``(pending, private_label)`` — ``private_label`` is the browser a private
    window was opened in, or ``None`` (not requested, or none found: the
    caller tells the user to open ``pending.url`` in a private window
    themselves).
    """
    verifier, challenge = _generate_pkce()
    state = _b64url(os.urandom(24))
    url = _build_authorize_url(challenge, state)
    pending = PendingLogin(verifier=verifier, state=state, url=url)

    private_label = None
    if open_browser and private:
        private_label = _open_private_browser(url)
    elif open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 — headless/no-browser is fine, URL is shown
            pass
    return pending, private_label


def complete_login(pending: PendingLogin, pasted: str) -> LoginResult:
    """Finish an authorization round-trip from the pasted callback value.

    Raises LoginError when no code can be read or the exchange fails.
    """
    pasted = pasted.strip()
    if not pasted:
        raise LoginError("No authorization code entered.")
    code, returned_state = _parse_pasted_code(pasted, pending.state)
    if not code:
        raise LoginError("Could not read an authorization code from that input.")

    resp = _exchange_code(code, returned_state, pending.verifier)
    credentials = _credentials_from_token_response(resp)

    account = resp.get("account") if isinstance(resp.get("account"), dict) else {}
    org = resp.get("organization") if isinstance(resp.get("organization"), dict) else {}
    email = account.get("email_address") or account.get("email") or ""
    return LoginResult(
        credentials=credentials,
        email=email,
        account_uuid=account.get("uuid", "") or "",
        organization_uuid=org.get("uuid", "") or "",
        organization_name=org.get("name", "") or "",
    )


def run_login_flow(*, open_browser: bool = True, private: bool = False) -> LoginResult:
    """Drive the interactive PKCE login and return the resulting credential.

    Prints the authorization URL, opens it in a browser (best effort), then reads
    the pasted authorization code from stdin and exchanges it. With ``private``,
    the URL opens in an incognito/private window so a second account can be added
    without touching the claude.ai session your normal browser is signed into.
    Raises LoginError on any failure; the caller persists the result into a slot.
    """
    print(accent("Standalone login") + " — authorize claude-swap in your browser.")
    print()
    print(muted("If your browser doesn't open, paste this URL manually:"))

    pending, private_label = prepare_login(open_browser=open_browser, private=private)
    print(pending.url)
    print()
    print(dimmed(
        "Pick the account (and, for a merged email, the organization) you want, "
        "then copy the code the page shows you."
    ))
    print()

    if open_browser and private:
        if private_label:
            print(muted(
                f"Opened a private {private_label} window "
                "(your normal session stays signed in)."
            ))
        else:
            warning(
                "No incognito-capable browser found (Chrome/Firefox/Brave/Edge). "
                "Open the URL above in a private window yourself, then paste the code."
            )

    try:
        pasted = input("Paste the authorization code here: ").strip()
    except (EOFError, KeyboardInterrupt):
        raise LoginError("Login cancelled.")

    return complete_login(pending, pasted)
