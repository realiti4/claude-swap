"""Tests for the local standalone browser-OAuth login (cswap login)."""

from __future__ import annotations

import base64
import hashlib
import json
from unittest.mock import patch

import pytest

from claude_swap import standalone_login as sl


class TestPkceAndUrl:
    def test_pkce_challenge_is_s256_of_verifier(self):
        verifier, challenge = sl._generate_pkce()
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).rstrip(b"=").decode()
        assert challenge == expected

    def test_authorize_url_carries_required_params(self):
        url = sl._build_authorize_url("CHAL", "STATE")
        for frag in (
            "claude.ai/oauth/authorize",
            "response_type=code",
            "code_challenge=CHAL",
            "code_challenge_method=S256",
            "state=STATE",
            "user%3Aprofile",  # broad scope → usage access
        ):
            assert frag in url


class TestParsePastedCode:
    def test_code_hash_state(self):
        assert sl._parse_pasted_code("abc#xyz", "fb") == ("abc", "xyz")

    def test_bare_code_uses_fallback_state(self):
        assert sl._parse_pasted_code("  bare  ", "fb") == ("bare", "fb")

    def test_full_redirect_url(self):
        got = sl._parse_pasted_code("https://x/cb?code=C&state=S", "fb")
        assert got == ("C", "S")


class TestCredentialsFromResponse:
    def test_full_response_builds_credential_json(self):
        creds = sl._credentials_from_token_response({
            "access_token": "AT", "refresh_token": "RT",
            "expires_in": 28800, "scope": "user:profile user:inference",
        })
        oauth = json.loads(creds)["claudeAiOauth"]
        assert oauth["accessToken"] == "AT"
        assert oauth["refreshToken"] == "RT"
        assert oauth["expiresAt"] > 0
        assert "user:profile" in oauth["scopes"]

    def test_missing_access_token_raises(self):
        with pytest.raises(sl.LoginError):
            sl._credentials_from_token_response({"refresh_token": "RT"})


class TestPrepareCompleteLogin:
    """The non-interactive halves the TUI drives directly."""

    def test_prepare_login_returns_pending_without_browser(self):
        with patch.object(sl.webbrowser, "open") as opened:
            pending, label = sl.prepare_login(open_browser=False)
        opened.assert_not_called()
        assert label is None
        assert pending.verifier and pending.state
        assert pending.url.startswith(sl.AUTHORIZE_URL)
        assert pending.state in pending.url

    def test_prepare_login_private_reports_browser_label(self):
        with patch.object(sl, "_open_private_browser", return_value="Chrome") as opener:
            pending, label = sl.prepare_login(private=True)
        assert label == "Chrome"
        opener.assert_called_once_with(pending.url)

    def test_complete_login_empty_paste_raises(self):
        with pytest.raises(sl.LoginError):
            sl.complete_login(sl.PendingLogin("v", "s", "u"), "   ")

    def test_complete_login_exchanges_parsed_code(self):
        seen: dict = {}

        def fake_exchange(code, state, verifier):
            seen.update(code=code, state=state, verifier=verifier)
            return {
                "access_token": "AT",
                "account": {"email_address": "a@x.com", "uuid": "au"},
                "organization": {"uuid": "ou", "name": "Org"},
            }

        with patch.object(sl, "_exchange_code", side_effect=fake_exchange):
            result = sl.complete_login(
                sl.PendingLogin("ver", "st", "url"), "code123#other"
            )
        assert seen == {"code": "code123", "state": "other", "verifier": "ver"}
        assert result.email == "a@x.com"
        assert result.organization_uuid == "ou"


class TestPrivateBrowserResolution:
    def test_prefers_chrome_over_firefox(self):
        with patch("sys.platform", "darwin"), \
             patch("os.path.exists", lambda p: "Chrome" in p or "Firefox" in p):
            label, _bin, flag = sl._resolve_private_browser()
        assert label == "Chrome" and flag == "--incognito"

    def test_falls_back_to_firefox(self):
        with patch("sys.platform", "darwin"), \
             patch("os.path.exists", lambda p: "Firefox" in p):
            label, _bin, flag = sl._resolve_private_browser()
        assert label == "Firefox" and flag == "--private-window"

    def test_none_when_no_private_browser(self):
        with patch("sys.platform", "darwin"), patch("os.path.exists", lambda p: False):
            assert sl._resolve_private_browser() is None

    def test_launch_builds_flagged_cmdline_without_opening(self):
        with patch.object(sl.subprocess, "Popen") as popen, \
             patch.object(sl, "_resolve_private_browser",
                          return_value=("Chrome", "/bin/chrome", "--incognito")):
            label = sl._open_private_browser("https://x")
        assert label == "Chrome"
        assert popen.call_args[0][0] == ["/bin/chrome", "--incognito", "https://x"]

    def test_launch_returns_none_when_nothing_resolves(self):
        with patch.object(sl, "_resolve_private_browser", return_value=None):
            assert sl._open_private_browser("https://x") is None


class TestRunLoginFlowPrivate:
    def _resp(self):
        return {
            "access_token": "AT", "refresh_token": "RT", "expires_in": 28800,
            "scope": "user:profile",
            "account": {"email_address": "x@y.z", "uuid": "acc"},
            "organization": {"uuid": "org", "name": "Org"},
        }

    def test_private_uses_incognito_path(self, capsys):
        with patch.object(sl, "_open_private_browser", return_value="Chrome") as ob, \
             patch.object(sl, "input", create=True, return_value="CODE#STATE"), \
             patch.object(sl, "_exchange_code", return_value=self._resp()):
            result = sl.run_login_flow(private=True)
        ob.assert_called_once()
        assert result.email == "x@y.z" and result.organization_name == "Org"
        assert "private Chrome window" in capsys.readouterr().out

    def test_private_warns_when_no_incognito_browser(self, capsys):
        with patch.object(sl, "_open_private_browser", return_value=None), \
             patch.object(sl, "input", create=True, return_value="CODE#STATE"), \
             patch.object(sl, "_exchange_code", return_value=self._resp()):
            sl.run_login_flow(private=True)
        assert "private window" in capsys.readouterr().out.lower()

    def test_non_private_does_not_launch_incognito(self):
        with patch.object(sl, "_open_private_browser") as ob, \
             patch.object(sl, "webbrowser") as wb, \
             patch.object(sl, "input", create=True, return_value="CODE#STATE"), \
             patch.object(sl, "_exchange_code", return_value=self._resp()):
            sl.run_login_flow(private=False)
        ob.assert_not_called()
        wb.open.assert_called_once()
