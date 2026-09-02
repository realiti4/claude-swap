"""Tests for the oauth module."""

from __future__ import annotations

import json
import ssl
import urllib.error
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from claude_swap import oauth


class TestExtractAccessToken:
    """Test extract_access_token."""

    def test_valid_credentials(self):
        creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-test-token"}})
        assert oauth.extract_access_token(creds) == "sk-test-token"

    def test_missing_key(self):
        creds = json.dumps({"claudeAiOauth": {}})
        assert oauth.extract_access_token(creds) is None

    def test_invalid_json(self):
        assert oauth.extract_access_token("not-json") is None

    def test_empty_string(self):
        assert oauth.extract_access_token("") is None


class TestAccountHeadroom:
    """Test account_headroom."""

    def test_binding_window_is_the_higher_utilization(self):
        usage = {"five_hour": {"pct": 80.0}, "seven_day": {"pct": 20.0}}
        assert oauth.account_headroom(usage) == 20.0  # 100 - max(80, 20)

    def test_seven_day_can_be_the_binding_window(self):
        usage = {"five_hour": {"pct": 10.0}, "seven_day": {"pct": 95.0}}
        assert oauth.account_headroom(usage) == 5.0

    def test_single_window(self):
        assert oauth.account_headroom({"five_hour": {"pct": 40.0}}) == 60.0

    def test_at_limit_is_zero_headroom(self):
        assert oauth.account_headroom({"five_hour": {"pct": 100.0}}) == 0.0

    def test_spend_is_ignored(self):
        # Pay-as-you-go credits must not drive rate-limit headroom.
        usage = {"spend": {"pct": 99.0}, "five_hour": {"pct": 10.0}}
        assert oauth.account_headroom(usage) == 90.0

    def test_no_window_data_is_unknown(self):
        assert oauth.account_headroom({"spend": {"pct": 50.0}}) is None
        assert oauth.account_headroom({}) is None

    def test_none_and_non_dict_are_unknown(self):
        assert oauth.account_headroom(None) is None
        assert oauth.account_headroom("no credentials") is None

    def test_malformed_pct_is_ignored(self):
        assert oauth.account_headroom({"five_hour": {"pct": None}}) is None

    def test_scoped_ignored_without_models_arg(self):
        # Default behavior is unchanged: per-model windows never bind.
        usage = {"five_hour": {"pct": 10.0}, "scoped": [{"name": "Fable", "pct": 100.0}]}
        assert oauth.account_headroom(usage) == 90.0

    def test_named_model_folds_into_binding_window(self):
        usage = {"five_hour": {"pct": 10.0}, "scoped": [{"name": "Fable", "pct": 95.0}]}
        assert oauth.account_headroom(usage, ["Fable"]) == 5.0

    def test_maxed_model_is_at_limit_despite_session_headroom(self):
        # The exact motivating case: 5h/7d fine, but the model is exhausted.
        usage = {
            "five_hour": {"pct": 1.0},
            "seven_day": {"pct": 40.0},
            "scoped": [{"name": "Fable", "pct": 100.0}],
        }
        assert oauth.account_headroom(usage, ["Fable"]) == 0.0

    def test_model_match_is_case_insensitive(self):
        usage = {"scoped": [{"name": "Fable", "pct": 70.0}]}
        assert oauth.account_headroom(usage, ["fable"]) == 30.0

    def test_unlisted_model_does_not_bind(self):
        usage = {"five_hour": {"pct": 10.0}, "scoped": [{"name": "Opus", "pct": 100.0}]}
        assert oauth.account_headroom(usage, ["Fable"]) == 90.0

    def test_multiple_models_take_the_worst(self):
        usage = {
            "five_hour": {"pct": 10.0},
            "scoped": [
                {"name": "Fable", "pct": 30.0},
                {"name": "Opus", "pct": 95.0},
                {"name": "Haiku", "pct": 50.0},
            ],
        }
        # Opus binds (95%); Sonnet is absent and simply contributes nothing.
        assert oauth.account_headroom(usage, ["Fable", "Opus", "Sonnet"]) == 5.0

    def test_works_for_any_model_name(self):
        for name in ("Opus", "Sonnet", "Haiku"):
            usage = {"scoped": [{"name": name, "pct": 100.0}]}
            assert oauth.account_headroom(usage, [name]) == 0.0

    def test_only_scoped_and_named_yields_headroom(self):
        # No 5h/7d at all (the live shape when the API returns only limits).
        assert oauth.account_headroom({"scoped": [{"name": "Fable", "pct": 100.0}]}, ["Fable"]) == 0.0

    def test_scoped_without_5h7d_and_unlisted_model_is_unknown(self):
        usage = {"scoped": [{"name": "Opus", "pct": 100.0}]}
        assert oauth.account_headroom(usage, ["Fable"]) is None

    def test_all_sentinel_matches_every_scoped_window(self):
        usage = {
            "five_hour": {"pct": 10.0},
            "scoped": [
                {"name": "Fable", "pct": 30.0},
                {"name": "Sonnet", "pct": 97.0},
            ],
        }
        assert oauth.account_headroom(usage, ["all"]) == 3.0
        assert oauth.account_headroom(usage, ["ALL"]) == 3.0


class TestRelevantWindows:
    """Test relevant_windows — the canonical window source."""

    def test_carries_labels_pcts_and_resets(self):
        usage = {
            "five_hour": {"pct": 80.0, "resets_at": "2026-07-10T12:00:00Z"},
            "seven_day": {"pct": 20.0},
            "scoped": [
                {"name": "Fable", "pct": 95.0, "resets_at": "2026-07-12T09:00:00Z"},
            ],
        }
        assert oauth.relevant_windows(usage, ["Fable"]) == [
            ("5h", 80.0, "2026-07-10T12:00:00Z"),
            ("7d", 20.0, None),
            ("Fable", 95.0, "2026-07-12T09:00:00Z"),
        ]

    def test_scoped_excluded_without_models(self):
        usage = {"five_hour": {"pct": 10.0}, "scoped": [{"name": "Fable", "pct": 99.0}]}
        assert oauth.relevant_windows(usage) == [("5h", 10.0, None)]

    def test_non_dict_usage_is_empty(self):
        assert oauth.relevant_windows(None) == []
        assert oauth.relevant_windows("no credentials") == []


class TestFormatReset:
    """Test format_reset."""

    def test_same_day_shows_time_only(self):
        from datetime import timedelta
        fixed_now = datetime(2026, 3, 23, 12, 0, 0, tzinfo=timezone.utc)
        future = fixed_now + timedelta(hours=2, minutes=15)
        with patch("claude_swap.oauth.datetime") as mock_dt:
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.now.return_value = fixed_now
            countdown, clock = oauth.format_reset(future.isoformat())
        assert countdown == "2h 15m"
        assert clock.count(":") == 1

    def test_different_day_shows_date(self):
        from datetime import timedelta
        fixed_now = datetime(2026, 3, 23, 12, 0, 0, tzinfo=timezone.utc)
        future = fixed_now + timedelta(days=2)
        with patch("claude_swap.oauth.datetime") as mock_dt:
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.now.return_value = fixed_now
            countdown, clock = oauth.format_reset(future.isoformat())
        import calendar
        months = list(calendar.month_abbr)[1:]
        assert any(m in clock for m in months)

    def test_minutes_only_when_under_one_hour(self):
        from datetime import timedelta
        fixed_now = datetime(2026, 3, 23, 12, 0, 0, tzinfo=timezone.utc)
        future = fixed_now + timedelta(minutes=45)
        with patch("claude_swap.oauth.datetime") as mock_dt:
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.now.return_value = fixed_now
            countdown, clock = oauth.format_reset(future.isoformat())
        assert countdown == "45m"
        assert "h" not in countdown


class TestFetchUsage:
    """Test fetch_usage."""

    def test_success(self):
        from datetime import timedelta
        fixed_now = datetime(2026, 3, 23, 12, 0, 0, tzinfo=timezone.utc)
        future = fixed_now + timedelta(hours=1)
        response_data = {
            "five_hour": {"utilization": 22.0, "resets_at": future.isoformat()},
            "seven_day": {"utilization": 61.0, "resets_at": future.isoformat()},
        }
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(response_data).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("claude_swap.oauth.urllib.request.urlopen", return_value=mock_response), \
             patch("claude_swap.oauth.datetime") as mock_dt:
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.now.return_value = fixed_now
            result = oauth.fetch_usage("sk-test-token")

        assert result["five_hour"]["pct"] == 22.0
        assert result["seven_day"]["pct"] == 61.0
        assert result["five_hour"]["countdown"] == "1h 0m"

    def test_network_error(self):
        with patch("claude_swap.oauth.urllib.request.urlopen", side_effect=Exception("timeout")):
            result = oauth.fetch_usage("sk-test-token")
        assert result is None

    def test_http_error_logs_in_debug_mode(self, capsys):
        import logging
        logger = logging.getLogger("claude-swap")
        logger.setLevel(logging.DEBUG)
        handler = logging.StreamHandler()
        logger.addHandler(handler)
        try:
            http_error = urllib.error.HTTPError(
                url="https://api.anthropic.com/api/oauth/usage",
                code=429,
                msg="Too Many Requests",
                hdrs=None,
                fp=None,
            )

            with patch("claude_swap.oauth.urllib.request.urlopen", side_effect=http_error):
                result = oauth.fetch_usage("sk-test-token")

            assert result is None
            debug_output = capsys.readouterr().err
            assert "Usage fetch failed" in debug_output
            assert "<HTTPError 429: 'Too Many Requests'>" in debug_output
        finally:
            logger.removeHandler(handler)
            logger.setLevel(logging.WARNING)

    def test_bad_response(self):
        mock_response = MagicMock()
        mock_response.read.return_value = b"{}"
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("claude_swap.oauth.urllib.request.urlopen", return_value=mock_response):
            result = oauth.fetch_usage("sk-test-token")
        assert result is None

    def test_null_resets_at(self):
        """When resets_at is null, still return pct without clock/countdown."""
        from datetime import timedelta
        fixed_now = datetime(2026, 3, 23, 12, 0, 0, tzinfo=timezone.utc)
        future = fixed_now + timedelta(hours=22)
        response_data = {
            "five_hour": {"utilization": 0.0, "resets_at": None},
            "seven_day": {"utilization": 100.0, "resets_at": future.isoformat()},
        }
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(response_data).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("claude_swap.oauth.urllib.request.urlopen", return_value=mock_response), \
             patch("claude_swap.oauth.datetime") as mock_dt:
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.now.return_value = fixed_now
            result = oauth.fetch_usage("sk-test-token")

        assert result is not None
        assert result["five_hour"]["pct"] == 0.0
        assert "clock" not in result["five_hour"]
        assert "countdown" not in result["five_hour"]
        assert result["seven_day"]["pct"] == 100.0
        assert "clock" in result["seven_day"]
        assert "countdown" in result["seven_day"]

    @staticmethod
    def _fetch_with_response(response_data):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(response_data).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        with patch("claude_swap.oauth.urllib.request.urlopen", return_value=mock_response):
            return oauth.fetch_usage("sk-test-token")

    def test_extra_usage_complete(self):
        """All extra_usage fields populated — spend, five_hour, and seven_day all present."""
        result = self._fetch_with_response({
            "five_hour": {"utilization": 22.0, "resets_at": None},
            "seven_day": {"utilization": 61.0, "resets_at": None},
            "extra_usage": {
                "is_enabled": True,
                "used_credits": 72900,
                "monthly_limit": 500000,
                "utilization": 14.58,
                "currency": "USD",
            },
        })
        assert result is not None
        assert result["five_hour"]["pct"] == 22.0
        assert result["seven_day"]["pct"] == 61.0
        assert result["spend"]["used"] == 729.0
        assert result["spend"]["limit"] == 5000.0
        assert result["spend"]["pct"] == 14.58
        assert result["spend"]["currency"] == "USD"

    def test_extra_usage_unlimited_keeps_other_rows(self):
        """Unlimited (monthly_limit=None) drops the spend entry without losing five_hour/seven_day."""
        result = self._fetch_with_response({
            "five_hour": {"utilization": 22.0, "resets_at": None},
            "seven_day": {"utilization": 61.0, "resets_at": None},
            "extra_usage": {
                "is_enabled": True,
                "used_credits": 72900,
                "monthly_limit": None,
                "utilization": None,
                "currency": "USD",
            },
        })
        assert result is not None
        assert result["five_hour"]["pct"] == 22.0
        assert result["seven_day"]["pct"] == 61.0
        assert "spend" not in result

    def test_extra_usage_partial_keeps_other_rows(self):
        """A null in used_credits leaves the rest of the response untouched."""
        result = self._fetch_with_response({
            "five_hour": {"utilization": 22.0, "resets_at": None},
            "seven_day": {"utilization": 61.0, "resets_at": None},
            "extra_usage": {
                "is_enabled": True,
                "used_credits": None,
                "monthly_limit": 500000,
                "utilization": 14.58,
            },
        })
        assert result is not None
        assert result["five_hour"]["pct"] == 22.0
        assert result["seven_day"]["pct"] == 61.0
        assert "spend" not in result

    def test_extra_usage_disabled_keeps_other_rows(self):
        """is_enabled=False suppresses spend even with valid numeric fields."""
        result = self._fetch_with_response({
            "five_hour": {"utilization": 22.0, "resets_at": None},
            "seven_day": {"utilization": 61.0, "resets_at": None},
            "extra_usage": {
                "is_enabled": False,
                "used_credits": 72900,
                "monthly_limit": 500000,
                "utilization": 14.58,
            },
        })
        assert result is not None
        assert result["five_hour"]["pct"] == 22.0
        assert result["seven_day"]["pct"] == 61.0
        assert "spend" not in result

    def test_scoped_per_model_limits(self):
        """weekly_scoped entries in limits[] surface as result['scoped'] by model name."""
        from datetime import timedelta
        fixed_now = datetime(2026, 3, 23, 12, 0, 0, tzinfo=timezone.utc)
        future = fixed_now + timedelta(hours=3)
        response_data = {
            "five_hour": {"utilization": 7.0, "resets_at": None},
            "seven_day": {"utilization": 72.0, "resets_at": None},
            "seven_day_opus": None,
            "limits": [
                {"kind": "session", "group": "session", "percent": 7,
                 "resets_at": None, "scope": None, "is_active": False},
                {"kind": "weekly_all", "group": "weekly", "percent": 72,
                 "resets_at": None, "scope": None, "is_active": False},
                {"kind": "weekly_scoped", "group": "weekly", "percent": 100,
                 "severity": "critical", "resets_at": future.isoformat(),
                 "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
                 "is_active": True},
            ],
        }
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(response_data).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        with patch("claude_swap.oauth.urllib.request.urlopen", return_value=mock_response), \
             patch("claude_swap.oauth.datetime") as mock_dt:
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.now.return_value = fixed_now
            result = oauth.fetch_usage("sk-test-token")

        assert result is not None
        # Only the model-scoped entry is surfaced; session/weekly_all (scope=None) are not.
        assert len(result["scoped"]) == 1
        fable = result["scoped"][0]
        assert fable["name"] == "Fable"
        assert fable["pct"] == 100.0
        assert fable["resets_at"] == future.isoformat()
        assert fable["countdown"] == "3h 0m"
        assert "clock" in fable

    def test_no_limits_no_scoped_key(self):
        """A response without a limits array yields no 'scoped' key (backward compat)."""
        result = self._fetch_with_response({
            "five_hour": {"utilization": 22.0, "resets_at": None},
            "seven_day": {"utilization": 61.0, "resets_at": None},
        })
        assert result is not None
        assert "scoped" not in result


class TestRefreshOAuthCredentials:
    """Test direct OAuth refresh requests."""

    @staticmethod
    def _make_credentials(scopes=None):
        if scopes is None:
            scopes = ["user:profile", "user:inference", "user:sessions:claude_code"]
        return json.dumps({
            "claudeAiOauth": {
                "accessToken": "old-access",
                "refreshToken": "old-refresh",
                "expiresAt": 0,
                "scopes": scopes,
            }
        })

    def test_refresh_sends_correct_body(self):
        seen_body = {}
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
        }).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        def mock_urlopen(req, timeout=0, **_kw):
            seen_body.update(json.loads(req.data.decode()))
            return mock_response

        with patch("claude_swap.oauth.urllib.request.urlopen", side_effect=mock_urlopen):
            refreshed = oauth.refresh_oauth_credentials(self._make_credentials())

        assert refreshed is not None
        assert seen_body["grant_type"] == "refresh_token"
        assert seen_body["refresh_token"] == "old-refresh"
        assert seen_body["client_id"] == oauth.OAUTH_CLIENT_ID
        assert "scope" not in seen_body


class TestTryRefreshOAuthCredentials:
    """Typed refresh outcomes: permanent vs transient failure classification."""

    _make_credentials = staticmethod(TestRefreshOAuthCredentials._make_credentials)

    @staticmethod
    def _http_error(code, body: bytes, msg="err"):
        import io

        return urllib.error.HTTPError(
            oauth.OAUTH_TOKEN_URL, code, msg, hdrs=None, fp=io.BytesIO(body)
        )

    def test_success_rotates_and_has_no_error(self):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
        }).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch(
            "claude_swap.oauth.urllib.request.urlopen", return_value=mock_response
        ):
            outcome = oauth.try_refresh_oauth_credentials(self._make_credentials())

        assert outcome.error is None
        rotated = json.loads(outcome.credentials)["claudeAiOauth"]
        assert rotated["accessToken"] == "new-access"
        assert rotated["refreshToken"] == "new-refresh"

    def test_invalid_grant_body_on_400_is_permanent(self):
        err = self._http_error(400, b'{"error": "invalid_grant"}')
        with patch("claude_swap.oauth.urllib.request.urlopen", side_effect=err):
            outcome = oauth.try_refresh_oauth_credentials(self._make_credentials())
        assert outcome.credentials is None
        assert outcome.error == "invalid_grant"

    def test_400_without_marker_is_transient(self):
        err = self._http_error(400, b'{"error": "temporarily_unavailable"}')
        with patch("claude_swap.oauth.urllib.request.urlopen", side_effect=err):
            outcome = oauth.try_refresh_oauth_credentials(self._make_credentials())
        assert outcome.error == "transient"

    def test_5xx_is_transient_even_with_marker(self):
        err = self._http_error(500, b'{"error": "invalid_grant"}')
        with patch("claude_swap.oauth.urllib.request.urlopen", side_effect=err):
            outcome = oauth.try_refresh_oauth_credentials(self._make_credentials())
        assert outcome.error == "transient"

    def test_network_error_is_transient(self):
        with patch(
            "claude_swap.oauth.urllib.request.urlopen",
            side_effect=urllib.error.URLError("dns"),
        ):
            outcome = oauth.try_refresh_oauth_credentials(self._make_credentials())
        assert outcome.error == "transient"

    def test_missing_refresh_token_is_permanent(self):
        creds = json.dumps({"claudeAiOauth": {"accessToken": "a", "expiresAt": 0}})
        outcome = oauth.try_refresh_oauth_credentials(creds)
        assert outcome.error == "no_refresh_token"

    def test_invalid_json_is_transient(self):
        # Changed contract (stale-credential robustness): an unparseable blob
        # is more likely a torn read than a credential shape — it must not
        # produce a permanent strike-advancing verdict.
        outcome = oauth.try_refresh_oauth_credentials("not json")
        assert outcome.error == "transient"

    def test_wrapper_returns_none_on_failure(self):
        err = self._http_error(400, b'{"error": "invalid_grant"}')
        with patch("claude_swap.oauth.urllib.request.urlopen", side_effect=err):
            assert oauth.refresh_oauth_credentials(self._make_credentials()) is None


class TestBuildTokenStatus:
    """Test token status formatting."""

    def test_builds_fresh_token_status(self):
        fixed_now = datetime(2026, 4, 2, 18, 0, 0, tzinfo=timezone.utc)
        expires_at = int(datetime(2026, 4, 2, 19, 30, 0, tzinfo=timezone.utc).timestamp() * 1000)
        credentials = json.dumps({
            "claudeAiOauth": {
                "accessToken": "old-access",
                "refreshToken": "old-refresh",
                "expiresAt": expires_at,
            }
        })

        with patch("claude_swap.oauth.datetime") as mock_dt:
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.fromtimestamp = datetime.fromtimestamp
            mock_dt.now.return_value = fixed_now
            status = oauth.build_token_status(credentials)

        assert status is not None
        assert "oauth: fresh, refresh token yes" in status
        assert "in 1h 30m" in status

    def test_builds_unknown_expiry_status(self):
        credentials = json.dumps({
            "claudeAiOauth": {
                "accessToken": "old-access",
                "refreshToken": "old-refresh",
            }
        })

        status = oauth.build_token_status(credentials)

        assert status == "oauth: unknown expiry, refresh token yes"


class TestFetchUsageForAccount:
    """Test refresh-aware usage fetches for managed accounts."""

    @staticmethod
    def _make_credentials(access="old-access", refresh="old-refresh",
                          expires_at=None, org_uuid="org-1", scopes=None):
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        if scopes is None:
            scopes = ["user:profile", "user:inference", "user:sessions:claude_code"]
        return json.dumps({
            "claudeAiOauth": {
                "accessToken": access,
                "refreshToken": refresh,
                "expiresAt": expires_at if expires_at is not None else now_ms + 3_600_000,
                "scopes": scopes,
                "subscriptionType": "pro",
                "rateLimitTier": "default_claude_ai",
            },
            "organizationUuid": org_uuid,
        })

    @staticmethod
    def _make_token_response(access="new-access", refresh="new-refresh",
                             expires_in=3600):
        return json.dumps({
            "access_token": access,
            "refresh_token": refresh,
            "expires_in": expires_in,
            "scope": "user:profile user:inference user:sessions:claude_code",
        }).encode()

    @staticmethod
    def _make_usage_response(h5_pct=12.0, d7_pct=34.0):
        resp = MagicMock()
        resp.read.return_value = json.dumps({
            "five_hour": {"utilization": h5_pct, "resets_at": None},
            "seven_day": {"utilization": d7_pct, "resets_at": None},
        }).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def test_refreshes_expired_token_before_usage_fetch(self):
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        credentials = self._make_credentials(expires_at=now_ms - 1_000)

        token_resp = MagicMock()
        token_resp.read.return_value = self._make_token_response()
        token_resp.__enter__ = lambda s: s
        token_resp.__exit__ = MagicMock(return_value=False)

        usage_resp = self._make_usage_response()
        persist_mock = MagicMock()

        def mock_urlopen(req, timeout=0, **_kw):
            if "oauth/token" in req.full_url:
                return token_resp
            if "oauth/usage" in req.full_url:
                assert req.get_header("Authorization") == "Bearer new-access"
                return usage_resp
            raise AssertionError(f"Unexpected URL: {req.full_url}")

        with patch("claude_swap.oauth.urllib.request.urlopen", side_effect=mock_urlopen):
            result = oauth.fetch_usage_for_account(
                "1", "test@example.com", credentials,
                is_active=False,
                persist_credentials=persist_mock,
            )

        assert result is not None
        assert result["five_hour"]["pct"] == 12.0
        persist_mock.assert_called_once()
        persisted_creds = persist_mock.call_args[0][2]
        merged = json.loads(persisted_creds)
        assert merged["organizationUuid"] == "org-1"
        assert merged["claudeAiOauth"]["accessToken"] == "new-access"
        assert merged["claudeAiOauth"]["refreshToken"] == "new-refresh"

    def test_retries_401_with_token_refresh(self):
        """Account gets 401, refreshes, retries successfully."""
        credentials = self._make_credentials()

        token_resp = MagicMock()
        token_resp.read.return_value = self._make_token_response()
        token_resp.__enter__ = lambda s: s
        token_resp.__exit__ = MagicMock(return_value=False)

        usage_resp = self._make_usage_response(h5_pct=56.0, d7_pct=78.0)
        usage_calls = 0
        persist_mock = MagicMock()

        def mock_urlopen(req, timeout=0, **_kw):
            nonlocal usage_calls
            if "oauth/token" in req.full_url:
                return token_resp
            if "oauth/usage" in req.full_url:
                usage_calls += 1
                if usage_calls == 1:
                    assert req.get_header("Authorization") == "Bearer old-access"
                    raise urllib.error.HTTPError(
                        req.full_url, 401, "Unauthorized", hdrs=None, fp=None,
                    )
                assert req.get_header("Authorization") == "Bearer new-access"
                return usage_resp
            raise AssertionError(f"Unexpected URL: {req.full_url}")

        with patch("claude_swap.oauth.urllib.request.urlopen", side_effect=mock_urlopen):
            result = oauth.fetch_usage_for_account(
                "2", "test@example.com", credentials,
                is_active=False,
                persist_credentials=persist_mock,
            )

        assert result is not None
        assert result["seven_day"]["pct"] == 78.0
        assert usage_calls == 2
        persist_mock.assert_called_once()
        refreshed_oauth = json.loads(persist_mock.call_args[0][2])["claudeAiOauth"]
        assert refreshed_oauth["accessToken"] == "new-access"

    def test_valid_token_fetches_usage_without_refresh(self):
        """Account with valid token fetches usage without refresh."""
        credentials = self._make_credentials()

        usage_resp = self._make_usage_response(h5_pct=10.0, d7_pct=20.0)

        def mock_urlopen(req, timeout=0, **_kw):
            if "oauth/usage" in req.full_url:
                assert req.get_header("Authorization") == "Bearer old-access"
                return usage_resp
            raise AssertionError(f"Unexpected URL: {req.full_url}")

        with patch("claude_swap.oauth.urllib.request.urlopen", side_effect=mock_urlopen), \
             patch("claude_swap.oauth.refresh_oauth_credentials") as refresh_mock:
            result = oauth.fetch_usage_for_account(
                "1", "test@example.com", credentials,
                is_active=False,
            )

        refresh_mock.assert_not_called()
        assert result is not None
        assert result["five_hour"]["pct"] == 10.0

    def test_refresh_failure_returns_none_gracefully(self):
        """If token refresh fails (e.g. revoked), usage returns None."""
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        credentials = self._make_credentials(expires_at=now_ms - 1_000)

        def mock_urlopen(req, timeout=0, **_kw):
            if "oauth/token" in req.full_url:
                raise urllib.error.HTTPError(
                    req.full_url, 400, "Bad Request", hdrs=None, fp=None,
                )
            if "oauth/usage" in req.full_url:
                raise urllib.error.HTTPError(
                    req.full_url, 401, "Unauthorized", hdrs=None, fp=None,
                )
            raise AssertionError(f"Unexpected URL: {req.full_url}")

        with patch("claude_swap.oauth.urllib.request.urlopen", side_effect=mock_urlopen):
            result = oauth.fetch_usage_for_account(
                "1", "test@example.com", credentials,
                is_active=False,
            )

        assert result is None

    def test_refreshes_when_scopes_are_missing(self):
        """Refresh should work even when stored credentials have no scopes."""
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        credentials = self._make_credentials(
            expires_at=now_ms - 1_000,
            scopes=None,
        )
        parsed = json.loads(credentials)
        del parsed["claudeAiOauth"]["scopes"]
        credentials = json.dumps(parsed)

        token_resp = MagicMock()
        token_resp.read.return_value = self._make_token_response()
        token_resp.__enter__ = lambda s: s
        token_resp.__exit__ = MagicMock(return_value=False)

        usage_resp = self._make_usage_response()
        persist_mock = MagicMock()

        def mock_urlopen(req, timeout=0, **_kw):
            if "oauth/token" in req.full_url:
                body = json.loads(req.data.decode())
                assert "scope" not in body
                return token_resp
            if "oauth/usage" in req.full_url:
                return usage_resp
            raise AssertionError(f"Unexpected URL: {req.full_url}")

        with patch("claude_swap.oauth.urllib.request.urlopen", side_effect=mock_urlopen):
            result = oauth.fetch_usage_for_account(
                "1", "test@example.com", credentials,
                is_active=False,
                persist_credentials=persist_mock,
            )

        assert result is not None
        persist_mock.assert_called_once()

    def test_active_account_skips_refresh_even_when_expired(self):
        """Active account with expired token must NOT trigger a refresh POST.

        Claude Code owns the active account's credentials and coordinates its
        own refresh via a lockfile on ~/.claude/ that cswap doesn't honor, so
        cswap must never touch the active account's tokens.
        """
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        credentials = self._make_credentials(expires_at=now_ms - 1_000)

        persist_mock = MagicMock()
        refresh_calls = 0

        def mock_urlopen(req, timeout=0, **_kw):
            nonlocal refresh_calls
            if "oauth/token" in req.full_url:
                refresh_calls += 1
                raise AssertionError(
                    "Active account must not trigger a refresh POST"
                )
            if "oauth/usage" in req.full_url:
                raise urllib.error.HTTPError(
                    req.full_url, 401, "Unauthorized", hdrs=None, fp=None,
                )
            raise AssertionError(f"Unexpected URL: {req.full_url}")

        with patch("claude_swap.oauth.urllib.request.urlopen", side_effect=mock_urlopen):
            result = oauth.fetch_usage_for_account(
                "1", "test@example.com", credentials,
                is_active=True,
                persist_credentials=persist_mock,
            )

        assert refresh_calls == 0
        persist_mock.assert_not_called()
        # Usage call 401'd and there's no retry-with-refresh for active, so None.
        assert result is None

    def test_active_account_401_does_not_retry_with_refresh(self):
        """Active account that 401s returns None without attempting a refresh."""
        credentials = self._make_credentials()

        def mock_urlopen(req, timeout=0, **_kw):
            if "oauth/token" in req.full_url:
                raise AssertionError(
                    "Active account must not trigger a refresh POST on 401"
                )
            if "oauth/usage" in req.full_url:
                raise urllib.error.HTTPError(
                    req.full_url, 401, "Unauthorized", hdrs=None, fp=None,
                )
            raise AssertionError(f"Unexpected URL: {req.full_url}")

        persist_mock = MagicMock()
        with patch("claude_swap.oauth.urllib.request.urlopen", side_effect=mock_urlopen):
            result = oauth.fetch_usage_for_account(
                "1", "test@example.com", credentials,
                is_active=True,
                persist_credentials=persist_mock,
            )

        assert result is None
        persist_mock.assert_not_called()

    def test_persist_failure_logs_warning_with_recovery_hint(self, caplog, capsys):
        """If the persist callback raises, _persist logs at WARNING level with
        a recovery hint (re-run `cswap --add-account`), not debug, AND prints
        a user-visible warning to stdout.
        """
        import logging

        def boom(acct_num, acct_email, creds):
            raise RuntimeError("disk exploded")

        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            oauth._persist(boom, "1", "test@example.com", "{}")

        warning_records = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and r.name == "claude-swap"
        ]
        assert len(warning_records) == 1
        msg = warning_records[0].getMessage()
        assert "failed to persist" in msg
        assert "cswap --add-account" in msg
        assert "1" in msg
        assert "test@example.com" in msg

        # Also verify the user-visible printed warning
        output = capsys.readouterr().out
        assert "failed to save refreshed token" in output
        assert "cswap --add-account" in output


class TestNativeTlsFallbackIsAudible:
    """A silent fallback to stdlib ssl is a silent NARROWING OF TRUST.

    ``_use_native_tls`` swallows every exception so the CLI is never blocked
    over a trust nicety. That part is right. What is wrong is that it leaves no
    trace, and the two paths do not trust the same roots.

    Measured on macOS 2026-08-17, comparing the OS keychains against what
    stdlib actually loads:

        OS-store unique roots           173   (system 154, admin 4, login 15)
        stdlib-loaded roots             128
        trusted by OS, NOT by stdlib     67   <-- lost, with no message

    Four of those live in /Library/Keychains/System.keychain, which is where an
    administrator puts a corporate MITM CA. So the fallback can take away the
    exact root the machine was configured with, and the user is then told to
    "trust the CA in the OS store" by a remedy note pointing at a store that is
    no longer being read.
    """

    def test_a_failed_injection_says_so(self, caplog):
        import logging
        import builtins
        from claude_swap import cli

        real_import = builtins.__import__

        def refuse(name, *a, **k):
            if name == "truststore":
                raise ImportError("simulated: truststore unavailable")
            return real_import(name, *a, **k)

        builtins.__import__ = refuse
        try:
            with caplog.at_level(logging.WARNING):
                cli._use_native_tls()
        finally:
            builtins.__import__ = real_import

        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "truststore" in joined.lower() or "native" in joined.lower(), (
            f"the fallback left no trace; records={[r.getMessage() for r in caplog.records]}"
        )

    def test_a_successful_injection_stays_quiet(self, caplog):
        """The control: the normal path must not warn, or the warning is noise
        every run and stops being read.

        AND IT PUTS SSL BACK. `inject_into_ssl` is process-global and has no
        automatic undo, so without the `finally` below every
        `ssl.create_default_context()` for the rest of the worker returns a
        `truststore._api.SSLContext` — a different class with a different API.

        That is not hypothetical: it turned CI red. `truststore`'s context
        raises `NotImplementedError` from `get_ca_certs()`, so a later test in
        this file that asked a context what roots it carried died on a call
        this test had made. It passed locally and failed in CI purely on
        xdist's scheduling — same worker, poisoned; different workers, both
        green. A suite whose result depends on which worker drew which test is
        not reporting on the code.

        The undo belongs HERE rather than in the victim, because the blast
        radius is every test that touches ssl after this one, not the one that
        happened to be caught.
        """
        import logging
        from claude_swap import cli

        try:
            with caplog.at_level(logging.WARNING):
                cli._use_native_tls()
            assert not [r for r in caplog.records
                        if "truststore" in r.getMessage().lower()]
        finally:
            try:
                import truststore

                truststore.extract_from_ssl()
            except Exception:  # noqa: BLE001 — nothing to undo is a fine outcome
                pass

        # THE GUARD, not decoration. A cleanup nothing asserts is one the next
        # edit deletes, and its absence is invisible until some unrelated test
        # in some unrelated file starts failing on a scheduling coin-flip.
        import ssl

        assert type(ssl.create_default_context()).__module__ == "ssl", (
            "this test left truststore injected into ssl — every later test in "
            "this worker now gets a different SSLContext class")


class TestClassifyUsageError:
    """Test _classify_usage_error kinds and Retry-After parsing."""

    @staticmethod
    def _http_error(code: int, headers: dict | None = None):
        import email.message
        hdrs = None
        if headers is not None:
            hdrs = email.message.Message()
            for k, v in headers.items():
                hdrs[k] = v
        return urllib.error.HTTPError(
            url="https://api.anthropic.com/api/oauth/usage",
            code=code, msg="err", hdrs=hdrs, fp=None,
        )

    def test_http_codes(self):
        assert oauth._classify_usage_error(self._http_error(429))[0] == "http-429"
        assert oauth._classify_usage_error(self._http_error(500))[0] == "http-500"
        assert oauth._classify_usage_error(self._http_error(401))[0] == "http-401"

    def test_retry_after_seconds(self):
        kind, retry = oauth._classify_usage_error(
            self._http_error(429, {"Retry-After": "30"})
        )
        assert kind == "http-429"
        assert retry == 30.0

    def test_retry_after_date_form_ignored(self):
        _, retry = oauth._classify_usage_error(
            self._http_error(429, {"Retry-After": "Fri, 04 Jul 2026 12:00:00 GMT"})
        )
        assert retry is None

    def test_retry_after_negative_clamped(self):
        _, retry = oauth._classify_usage_error(
            self._http_error(429, {"Retry-After": "-5"})
        )
        assert retry == 0.0

    def test_no_headers(self):
        kind, retry = oauth._classify_usage_error(self._http_error(429))
        assert (kind, retry) == ("http-429", None)

    def test_timeout(self):
        import socket
        assert oauth._classify_usage_error(TimeoutError())[0] == "timeout"
        assert oauth._classify_usage_error(socket.timeout())[0] == "timeout"
        assert oauth._classify_usage_error(
            urllib.error.URLError(TimeoutError())
        )[0] == "timeout"

    def test_network(self):
        assert oauth._classify_usage_error(
            urllib.error.URLError(ConnectionRefusedError())
        )[0] == "network"

    def test_tls_cert_failure_is_not_flattened_to_network(self):
        """A MITM proxy with an untrusted CA must not read as a transport error.

        Measured on one host: every usage poll went through a
        TLS-terminating proxy whose CA urllib does not trust, so each one raised

            URLError(SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED]
            certificate verify failed: unable to get local issuer certificate"))

        and was recorded as ``network``. One account sat dead for ten days and
        "network" is the only word anyone could see; the real cause reaches
        DEBUG alone, which nothing enables. "Cannot reach the host" and "reached
        the host and refused its certificate" need opposite fixes, so they may
        not share a token.
        """
        e = urllib.error.URLError(
            ssl.SSLCertVerificationError(
                1,
                "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
                "unable to get local issuer certificate (_ssl.c:1000)",
            )
        )
        assert oauth._classify_usage_error(e)[0] == "tls-cert"

    def test_a_non_cert_ssl_failure_is_not_a_cert_failure(self):
        """Pins the PREDICATE, not just the outcome.

        ``ssl.SSLCertVerificationError`` is the narrow choice on purpose, and
        the obvious loosening -- ``ssl.SSLError`` -- passes every other test in
        this file. A non-cert TLS failure is a different condition with a
        different repair: speaking https to a plaintext port raises
        ``SSLError("record layer failure")``, and calling that ``tls-cert``
        sends the operator to fix a CA bundle for a wrong-port problem.
        """
        assert oauth._classify_usage_error(
            urllib.error.URLError(ssl.SSLEOFError("handshake failed"))
        )[0] == "network"
        assert oauth._classify_usage_error(
            urllib.error.URLError(ssl.SSLError("record layer failure"))
        )[0] == "network"

    def test_tls_cert_carries_a_remedy_note(self):
        """A kind with no ERROR_NOTES entry renders as the bare identifier.

        The whole point of splitting this out of ``network`` is that the two
        need different repairs, and that only reaches the operator through the
        note. Without one the display trades one uninformative word for
        another. ``test_every_deterministic_kind_has_a_note`` states the same
        principle but iterates ``_DETERMINISTIC_REFRESH_ERRORS``, which is a
        refresh-error list -- a usage-fetch kind is outside its loop, so it
        cannot cover this.
        """
        from claude_swap.switcher import ERROR_NOTES

        assert "tls-cert" in ERROR_NOTES
        note = ERROR_NOTES["tls-cert"]
        assert "SSL_CERT_FILE" in note

    def test_bad_response(self):
        try:
            json.loads("not json")
        except json.JSONDecodeError as e:
            assert oauth._classify_usage_error(e)[0] == "bad-response"

    def test_fallback_type_name(self):
        assert oauth._classify_usage_error(ValueError("x"))[0] == "ValueError"


class TestTryFetchUsageOutcome:
    """Test try_fetch_usage_for_account outcome classification."""

    @staticmethod
    def _make_credentials() -> str:
        from datetime import timedelta
        future_ms = int(
            (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp() * 1000
        )
        return json.dumps({
            "claudeAiOauth": {
                "accessToken": "old-access",
                "refreshToken": "old-refresh",
                "expiresAt": future_ms,
            }
        })

    def test_success_outcome(self):
        resp = MagicMock()
        resp.read.return_value = json.dumps(
            {"five_hour": {"utilization": 12.0, "resets_at": None}}
        ).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)

        with patch("claude_swap.oauth.urllib.request.urlopen", return_value=resp):
            outcome = oauth.try_fetch_usage_for_account(
                "1", "a@b.c", self._make_credentials(), is_active=False,
            )
        assert outcome.error is None
        assert outcome.usage["five_hour"]["pct"] == 12.0

    def test_429_outcome_carries_retry_after(self, caplog):
        import email.message
        import logging
        hdrs = email.message.Message()
        hdrs["Retry-After"] = "42"
        err = urllib.error.HTTPError(
            "https://api.anthropic.com/api/oauth/usage", 429, "Too Many",
            hdrs=hdrs, fp=None,
        )
        with (
            patch("claude_swap.oauth.urllib.request.urlopen", side_effect=err),
            caplog.at_level(logging.WARNING, logger="claude-swap"),
        ):
            outcome = oauth.try_fetch_usage_for_account(
                "1", "a@b.c", self._make_credentials(), is_active=False,
            )
        assert outcome.usage is None
        assert outcome.error == "http-429"
        assert outcome.retry_after_s == 42.0
        warnings = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        line = next(m for m in warnings if "http-429" in m)
        # The line users paste into public issues: account number and the
        # server's Retry-After, never the email.
        assert "account 1" in line
        assert "retry-after 42s" in line
        assert "a@b.c" not in line
        # Any 429 = the usage endpoint's own budget, which cumulative polling
        # across cswap surfaces can drain — the log says what is happening.
        # Deliberately not scoped to the token in the wording: the budget is
        # account/org-scoped (see poll_policy), so a re-login does not clear it.
        assert "usage-endpoint budget" in line

    def test_edge_429_warning_names_the_budget(self, caplog):
        import email.message
        import logging
        hdrs = email.message.Message()
        hdrs["Retry-After"] = "0"
        err = urllib.error.HTTPError(
            "https://api.anthropic.com/api/oauth/usage", 429, "Too Many",
            hdrs=hdrs, fp=None,
        )
        with (
            patch("claude_swap.oauth.urllib.request.urlopen", side_effect=err),
            caplog.at_level(logging.WARNING, logger="claude-swap"),
        ):
            outcome = oauth.try_fetch_usage_for_account(
                "1", "a@b.c", self._make_credentials(), is_active=False,
            )
        assert outcome.retry_after_s == 0.0
        line = next(
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING and "http-429" in r.getMessage()
        )
        # "Retry-After: 0" is the saturated-budget edge — same hint.
        assert "retry-after 0s" in line
        assert "usage-endpoint budget" in line

    def test_timeout_outcome(self):
        with patch(
            "claude_swap.oauth.urllib.request.urlopen",
            side_effect=urllib.error.URLError(TimeoutError()),
        ):
            outcome = oauth.try_fetch_usage_for_account(
                "1", "a@b.c", self._make_credentials(), is_active=False,
            )
        assert outcome.error == "timeout"

    def test_no_access_token_outcome(self):
        outcome = oauth.try_fetch_usage_for_account(
            "1", "a@b.c", json.dumps({"claudeAiOauth": {}}), is_active=False,
        )
        assert outcome.error == "no-access-token"


class TestInvalidGrantPropagation:
    """A dead refresh-token lineage surfaces as error='invalid_grant', distinct
    from a transient 'refresh-failed', so the store can quarantine the account."""

    @staticmethod
    def _expired_credentials() -> str:
        from datetime import timedelta
        past_ms = int(
            (datetime.now(timezone.utc) - timedelta(hours=1)).timestamp() * 1000
        )
        return json.dumps({"claudeAiOauth": {
            "accessToken": "old-access", "refreshToken": "dead-refresh",
            "expiresAt": past_ms,
        }})

    @staticmethod
    def _valid_credentials() -> str:
        from datetime import timedelta
        future_ms = int(
            (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp() * 1000
        )
        return json.dumps({"claudeAiOauth": {
            "accessToken": "good-access", "refreshToken": "dead-refresh",
            "expiresAt": future_ms,
        }})

    def test_proactive_refresh_invalid_grant_short_circuits(self):
        """Expired token + dead refresh: report invalid_grant without hitting usage."""
        with patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   return_value=oauth.RefreshOutcome(None, "invalid_grant")), \
             patch("claude_swap.oauth.request_usage_data") as usage:
            outcome = oauth.try_fetch_usage_for_account(
                "1", "a@b.c", self._expired_credentials(), is_active=False,
            )
        assert outcome.error == "invalid_grant"
        usage.assert_not_called()  # no pointless 401/429 on a lost cause

    def test_401_retry_invalid_grant_is_permanent(self):
        """Valid-looking token, server 401, dead refresh → invalid_grant."""
        err = urllib.error.HTTPError(
            "https://api.anthropic.com/api/oauth/usage", 401, "Unauthorized",
            hdrs=None, fp=None,
        )
        with patch("claude_swap.oauth.urllib.request.urlopen", side_effect=err), \
             patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   return_value=oauth.RefreshOutcome(None, "invalid_grant")):
            outcome = oauth.try_fetch_usage_for_account(
                "1", "a@b.c", self._valid_credentials(), is_active=False,
            )
        assert outcome.error == "invalid_grant"

    def test_transient_refresh_failure_is_not_permanent(self):
        """A transient refresh failure stays 'refresh-failed', not invalid_grant."""
        err = urllib.error.HTTPError(
            "https://api.anthropic.com/api/oauth/usage", 401, "Unauthorized",
            hdrs=None, fp=None,
        )
        with patch("claude_swap.oauth.urllib.request.urlopen", side_effect=err), \
             patch("claude_swap.oauth.try_refresh_oauth_credentials",
                   return_value=oauth.RefreshOutcome(None, "transient")):
            outcome = oauth.try_fetch_usage_for_account(
                "1", "a@b.c", self._valid_credentials(), is_active=False,
            )
        assert outcome.error == "refresh-failed"


class TestCredentialFingerprint:
    """Identity fingerprints for stored credentials (issue #117 guard)."""

    def test_stable_across_access_token_rotation(self):
        a = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-old", "refreshToken": "rt-1"}})
        b = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-new", "refreshToken": "rt-1", "expiresAt": 5}})
        assert oauth.credential_fingerprint(a) == oauth.credential_fingerprint(b)

    def test_differs_across_refresh_token_rotation(self):
        a = json.dumps({"claudeAiOauth": {"refreshToken": "rt-1"}})
        b = json.dumps({"claudeAiOauth": {"refreshToken": "rt-2"}})
        assert oauth.credential_fingerprint(a) != oauth.credential_fingerprint(b)

    def test_full_content_fallback_for_api_keys_and_setup_tokens(self):
        api_key = "sk-ant-api03-xyz"
        setup = json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat01-abc"}})
        assert oauth.credential_fingerprint(api_key) is not None
        assert oauth.credential_fingerprint(setup) is not None
        # Never None for real bytes: a None would make every "did it change?"
        # comparison degenerate to "changed".
        assert oauth.credential_fingerprint(api_key) != oauth.credential_fingerprint(setup)

    def test_full_hash_never_collides_with_refresh_hash(self):
        with_rt = json.dumps({"claudeAiOauth": {"refreshToken": "rt-1"}})
        assert oauth.credential_fingerprint(with_rt).startswith("sha256:")
        assert oauth.credential_fingerprint("raw-token").startswith("sha256-full:")

    def test_empty_input_is_none(self):
        assert oauth.credential_fingerprint("") is None


class TestTokenAccountParsing:
    """The token endpoint's optional account identity must not be discarded."""

    _make_credentials = staticmethod(TestRefreshOAuthCredentials._make_credentials)

    def _refresh_with_response(self, payload: dict) -> oauth.RefreshOutcome:
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(payload).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        with patch(
            "claude_swap.oauth.urllib.request.urlopen", return_value=mock_response
        ):
            return oauth.try_refresh_oauth_credentials(self._make_credentials())

    def test_token_account_surfaced_when_present(self):
        outcome = self._refresh_with_response({
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
            "account": {"uuid": "acc-uuid", "email_address": "a@b.c"},
            "organization": {"uuid": "org-uuid"},
        })
        assert outcome.error is None
        assert outcome.token_account == {
            "uuid": "acc-uuid", "email": "a@b.c", "organizationUuid": "org-uuid",
        }

    def test_token_account_absent_is_none(self):
        outcome = self._refresh_with_response({
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
        })
        assert outcome.error is None
        assert outcome.token_account is None

    # Same strict boundary as fetch_oauth_profile: identity is opportunistic
    # and must never break the refresh that carried it — malformed or
    # uuid-less data is None, optional fields normalize to str-or-None.

    def test_token_account_without_uuid_is_none(self):
        outcome = self._refresh_with_response({
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
            "account": {"email_address": "a@b.c"},
        })
        assert outcome.error is None
        assert outcome.token_account is None

    def test_token_account_non_string_uuid_is_none(self):
        outcome = self._refresh_with_response({
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
            "account": {"uuid": 12345, "email_address": "a@b.c"},
        })
        assert outcome.error is None
        assert outcome.token_account is None

    def test_token_account_uuid_whitespace_normalized(self):
        """Normalization happens at the boundary so padded uuids never reach
        comparisons or sequence.json backfills."""
        outcome = self._refresh_with_response({
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
            "account": {"uuid": "  acc-uuid  ", "email_address": "a@b.c"},
        })
        assert outcome.token_account["uuid"] == "acc-uuid"

    def test_token_account_non_string_optionals_normalized(self):
        outcome = self._refresh_with_response({
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
            "account": {"uuid": "acc-uuid", "email_address": {"weird": 1}},
            "organization": {"uuid": 99},
        })
        assert outcome.error is None
        assert outcome.token_account == {
            "uuid": "acc-uuid", "email": None, "organizationUuid": None,
        }


@pytest.mark.no_oauth_profile_fake
class TestFetchOauthProfile:
    """Access-token → account-identity resolution (/api/oauth/profile)."""

    def _profile_response(self, payload: dict):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(payload).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        return mock_response

    def test_resolves_identity(self):
        seen = {}

        def mock_urlopen(req, timeout=0, **_kw):
            seen["url"] = req.full_url
            seen["auth"] = req.headers.get("Authorization")
            return self._profile_response({
                "account": {"uuid": "acc-uuid", "email": "a@b.c"},
                "organization": {"uuid": "org-uuid"},
            })

        with patch("claude_swap.oauth.urllib.request.urlopen", side_effect=mock_urlopen):
            result = oauth.fetch_oauth_profile("sk-live")
        assert result == {
            "uuid": "acc-uuid", "email": "a@b.c", "organizationUuid": "org-uuid",
        }
        assert seen["url"].endswith("/api/oauth/profile")
        assert seen["auth"] == "Bearer sk-live"

    def test_uses_bounded_timeout(self):
        """One bounded call: the profile lookup may only ever add latency,
        never hang a switch."""
        seen = {}

        def mock_urlopen(req, timeout=0, **_kw):
            seen["timeout"] = timeout
            return self._profile_response({
                "account": {"uuid": "acc-uuid", "email": "a@b.c"},
            })

        with patch("claude_swap.oauth.urllib.request.urlopen", side_effect=mock_urlopen):
            oauth.fetch_oauth_profile("sk-live")
        assert seen["timeout"] == 5

    def test_network_failure_is_unresolvable_not_error(self):
        with patch(
            "claude_swap.oauth.urllib.request.urlopen",
            side_effect=urllib.error.URLError("down"),
        ):
            assert oauth.fetch_oauth_profile("sk-live") is None

    def test_missing_account_object_is_unresolvable(self):
        with patch(
            "claude_swap.oauth.urllib.request.urlopen",
            return_value=self._profile_response({"unexpected": True}),
        ):
            assert oauth.fetch_oauth_profile("sk-live") is None

    # Strict resolution boundary: the oracle is advisory (None keeps the
    # switch on the fail-open path), so a response only counts as resolved
    # with a non-empty string account.uuid — a schema change must degrade to
    # pre-fix behavior, not to preserve-and-skip.

    def test_missing_uuid_is_unresolvable(self):
        with patch(
            "claude_swap.oauth.urllib.request.urlopen",
            return_value=self._profile_response({
                "account": {"email": "a@b.c"},
                "organization": {"uuid": "org-uuid"},
            }),
        ):
            assert oauth.fetch_oauth_profile("sk-live") is None

    def test_non_string_uuid_is_unresolvable(self):
        with patch(
            "claude_swap.oauth.urllib.request.urlopen",
            return_value=self._profile_response({
                "account": {"uuid": 12345, "email": "a@b.c"},
            }),
        ):
            assert oauth.fetch_oauth_profile("sk-live") is None

    def test_blank_uuid_is_unresolvable(self):
        with patch(
            "claude_swap.oauth.urllib.request.urlopen",
            return_value=self._profile_response({
                "account": {"uuid": "   ", "email": "a@b.c"},
            }),
        ):
            assert oauth.fetch_oauth_profile("sk-live") is None

    def test_malformed_json_is_unresolvable(self):
        mock_response = MagicMock()
        mock_response.read.return_value = b"<!doctype html><html>gateway error"
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        with patch(
            "claude_swap.oauth.urllib.request.urlopen", return_value=mock_response,
        ):
            assert oauth.fetch_oauth_profile("sk-live") is None

    def test_uuid_whitespace_normalized_at_boundary(self):
        with patch(
            "claude_swap.oauth.urllib.request.urlopen",
            return_value=self._profile_response({
                "account": {"uuid": "  acc-uuid  ", "email": "a@b.c"},
            }),
        ):
            result = oauth.fetch_oauth_profile("sk-live")
        assert result["uuid"] == "acc-uuid"

    def test_valid_uuid_with_missing_email_still_resolves(self):
        """email/organization are optional; uuid is the identity."""
        with patch(
            "claude_swap.oauth.urllib.request.urlopen",
            return_value=self._profile_response({
                "account": {"uuid": "acc-uuid"},
            }),
        ):
            result = oauth.fetch_oauth_profile("sk-live")
        assert result == {"uuid": "acc-uuid", "email": None, "organizationUuid": None}

    def test_non_string_optional_fields_are_dropped_not_fatal(self):
        with patch(
            "claude_swap.oauth.urllib.request.urlopen",
            return_value=self._profile_response({
                "account": {"uuid": "acc-uuid", "email": {"weird": True}},
                "organization": {"uuid": 99},
            }),
        ):
            result = oauth.fetch_oauth_profile("sk-live")
        assert result == {"uuid": "acc-uuid", "email": None, "organizationUuid": None}

    def test_401_is_unresolvable_with_log_file_warning(self, caplog):
        """401 is evidence (the live token can't authenticate) but not proof —
        fail open, and record it at warning level in the log only (the
        console handler exists only under --debug)."""
        import logging

        err = urllib.error.HTTPError(
            "https://api.anthropic.com/api/oauth/profile", 401,
            "Unauthorized", {}, None,
        )
        with patch(
            "claude_swap.oauth.urllib.request.urlopen", side_effect=err,
        ), caplog.at_level(logging.WARNING, logger="claude-swap"):
            assert oauth.fetch_oauth_profile("sk-live") is None
        assert any(
            "401" in r.message and "pre-fix" in r.message
            for r in caplog.records
        )


class TestBridgeTitleRestoreRunsWithoutTheProxy:
    """The restore must not depend on the proxy it exists to survive.

    cswap-pin already implements it, with exactly one caller —
    `_sweep_bridges_after_connect` — so it fires only when a
    `POST /v1/code/sessions` REACHES the proxy. Measured: all 24
    claude processes carried HTTPS_PROXY=127.0.0.1:9901 (CCF), because Claude
    Code reads ~/.claude.json's env block once at boot and the pin wiring
    landed after they exec'd. Nothing reached the proxy, nothing was restored,
    and a session sat under `Fix Claude AI session naming issue` until it was
    renamed by hand.

    WHAT IS TESTED HERE IS THE TRANSPORT AND THE GUARDS. The POLICY — which
    titles may be touched — stays in cswap-pin and is tested there; asserting
    it again here would be a second copy to drift. The one thing this file must
    prove about the policy is that the REAL one is wired, and that is the last
    case.

    A FIRST VERSION OF THIS CLASS WAS ALL TAUTOLOGY, worth recording because it
    passed. `cswap_pin` is an optional extra and is not importable in this
    environment, so `restore_bridge_titles` returned 0 from its ImportError
    guard before reaching anything — three of four cases went green against an
    implementation that never ran.
    """

    def _policy(self, monkeypatch, fn):
        """Install a fake policy AT THE SEAM.

        The transport is what this class owns, so the decision is injected
        rather than imported — and injecting it is also what makes these cases
        run at all on a machine without the extra.

        INJECTED AT `claude_swap.pin`, NOT INTO sys.modules, since oauth.py
        stopped importing the package directly. That is not a workaround: the
        seam IS the policy boundary now, so patching it is patching exactly
        what the class docstring says is not ours. The old sys.modules
        injection also no longer reaches the code, deliberately — `_impl`
        resolves through `find_spec`, and a bare `types.ModuleType` has no
        `__spec__`, which it treats as "not installed" (`except ValueError`).

        `is_available` too, because the guard and the call are separate
        questions and a fake policy with no available seam would return
        "no-extra" before reaching the transport under test.
        """
        monkeypatch.setattr("claude_swap.pin.is_available", lambda: True)
        monkeypatch.setattr("claude_swap.pin.titles_to_restore",
                            lambda sessions, names: list(fn(sessions, names)))

    def test_the_bridge_calls_trust_our_own_proxy_without_an_env_var(self):
        """A python client of the pin must ADD our CA, never REPLACE the store.

        These two calls are plain urllib through whatever proxy the session
        was wired to. When that proxy is the pin, it MITMs api.anthropic.com,
        so the default context cannot verify it and every call dies
        CERTIFICATE_VERIFY_FAILED — swallowed to debug, returning None, which
        is how the cloud session names stayed wrong for hours with nothing in
        any log.

        SSL_CERT_FILE WAS THE WRONG TOOL AND THE MEASUREMENTS SAY SO. It
        REPLACES OpenSSL's file, so it is only safe where the bundle subsumes
        the store it displaces — measured per machine:

            host-a     ambient 124  bundle 126  safe
            host-b      ambient 128  bundle 167  NOT (27 missing)
            host-c  ambient 128  bundle   2  NOT (128 missing)

        so the gate that writes it correctly refuses on two of the three, and the
        restore stays broken there. Adding the CA to a default context keeps
        the system roots and needs no variable at all. Measured on
        host-c, same process, same proxy:

            default ctx                 CERTIFICATE_VERIFY_FAILED
            default ctx + our CA added  HTTP 200

        This pins that the context builder ADDS: it hands back the DEFAULT
        context, with our CA loaded into it.

        A CERT COUNT CANNOT ASK THIS QUESTION, and the first version of this
        test tried. `assert len(ctx.get_ca_certs()) > 20` was wrong twice over:

          - `get_ca_certs()` reports only certs loaded from a *cafile*. Roots
            that arrive from a *capath* directory load lazily and never appear,
            so a perfectly healthy store reads 0 on such a machine. Measured on
            host-a: `get_default_verify_paths().cafile` is None and the
            count is 0 while verification works fine.
          - it is a COUNT, and containment is not size. Measured on
            host-b: a 167-cert bundle was still missing 27 certs of the
            128-cert store it would have displaced. A bigger number looked like
            an answer and was not.

        So assert the structure instead: `create_default_context()` is the
        base — whatever roots this machine and this interpreter give it — and
        the pin CA goes in through `load_verify_locations`, which ADDS. Both
        claims are checked without asking any context to enumerate anything,
        which is also why this survives `truststore.inject_into_ssl()` — its
        context class raises NotImplementedError from `get_ca_certs()`.
        """
        import ssl

        from claude_swap import oauth

        class Recorder:
            """Stands in for the default context and records what was ADDED."""

            def __init__(self):
                self.loaded = []

            def load_verify_locations(self, cafile=None, capath=None, cadata=None):
                self.loaded.append(cafile)

        def build(ca):
            """Run the builder with `pin.ca_path_for_trust -> ca`.

            AT THE SEAM, not sys.modules: oauth.py asks `claude_swap.pin` now,
            and the seam already collapses "no extra" and "extra raised" to
            None, which is the only distinction this builder ever made.
            """
            from claude_swap import pin as _pin

            spy = Recorder()
            # THE CACHE IS PROCESS-WIDE, so a build under a different CA state
            # would otherwise hand this one a context built for that state.
            # ONE SLOT now rather than a dict — see `_PIN_CTX_SLOT`, which
            # replaced an entry-per-CA map that never evicted.
            oauth._PIN_CTX_SLOT = None
            real_ctx = ssl.create_default_context
            real_ca = _pin.ca_path_for_trust
            _pin.ca_path_for_trust = lambda: ca
            ssl.create_default_context = lambda *a, **k: spy
            try:
                return spy, oauth._pin_aware_ssl_context()
            finally:
                ssl.create_default_context = real_ctx
                _pin.ca_path_for_trust = real_ca

        # A PIN IS INSTALLED AND HAS A CA.
        spy, ctx = build("/somewhere/pin-proxy/ca.pem")
        # THE SAME OBJECT BACK. A builder that returned a fresh, narrower
        # context — which is what SSL_CERT_FILE effectively does — fails here,
        # because the ambient roots live in the object it was handed.
        assert ctx is spy, (
            "the builder did not return the default context — the ambient "
            "roots were replaced, which is SSL_CERT_FILE's bug rebuilt in code")
        assert spy.loaded == ["/somewhere/pin-proxy/ca.pem"], spy.loaded

        # THE CONTROL, and it is the case that runs on every machine WITHOUT a
        # pin — including CI, where `cswap_pin` is not importable at all. The
        # first version of this test had no control and asserted the CA was
        # always added, so it failed in CI for the correct behaviour: with no
        # pin there is nothing to add, and adding nothing is right.
        spy, ctx = build(None)
        assert ctx is spy, "no pin must still yield the untouched default context"
        assert spy.loaded == [], (
            f"nothing to trust, so nothing may be loaded; got {spy.loaded}")

        # AND IT IS BUILT ONCE PER CA STATE, not once per API call. This runs
        # on the polling path — once per account per usage poll, plus every
        # profile fetch, listing and rename — and each build loads the whole
        # system trust store before parsing ours on top.
        again = oauth._pin_aware_ssl_context()
        assert again is ctx, (
            "the second call rebuilt the context, so every polled request "
            "reloads ~130 system certificates for an answer that cannot have "
            "changed")

        # AND A REGENERATED CA INVALIDATES IT, which is the only way a cache
        # here can be wrong: `cswap pin` can mint a new CA at the SAME path,
        # and a context still trusting the old one fails every call with
        # exactly the error this helper exists to prevent.
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            ca = os.path.join(d, "ca.pem")
            with open(ca, "w") as fh:
                fh.write("first")
            first_spy, first = build(ca)
            assert first is first_spy, "precondition: the first build is fresh"
            with open(ca, "w") as fh:
                fh.write("regenerated")
            os.utime(ca, (0, 0))
            second = oauth._pin_aware_ssl_context()
            assert second is not first, (
                "a regenerated CA at the same path kept the context built for "
                "the old one — every call afterwards dies "
                "CERTIFICATE_VERIFY_FAILED and the cache is the cause")

    def test_every_title_the_policy_names_is_put_back(self, monkeypatch):
        from claude_swap import oauth

        calls = []
        self._policy(monkeypatch, lambda sessions, names: [("cse_a", "slack")])
        monkeypatch.setattr(oauth, "_list_bridge_sessions",
                            lambda tok: [{"id": "cse_a", "title": "invented"}])
        monkeypatch.setattr(oauth, "_put_bridge_title",
                            lambda tok, sid, title: calls.append((sid, title)) or True)
        assert oauth.restore_bridge_titles("tok", {"cse_a": "slack"}) == (1, "renamed")
        assert calls == [("cse_a", "slack")], calls

    def test_the_policy_naming_nothing_puts_nothing(self, monkeypatch):
        """THE CONTROL. Without it, "renames what the policy names" also passes
        on a version that renames unconditionally — and that version reverts
        every name set in the claude.ai web app, permanently."""
        from claude_swap import oauth

        calls = []
        self._policy(monkeypatch, lambda sessions, names: [])
        monkeypatch.setattr(oauth, "_list_bridge_sessions",
                            lambda tok: [{"id": "cse_b", "title": "a name I typed"}])
        monkeypatch.setattr(oauth, "_put_bridge_title",
                            lambda tok, sid, title: calls.append((sid, title)) or True)
        assert oauth.restore_bridge_titles("tok", {"cse_b": "local"}) == (
            0, "nothing-to-rename")
        assert calls == [], calls

    def test_a_listing_that_failed_is_not_read_as_an_empty_one(self, monkeypatch):
        """`None` means "could not ask", never "nothing there". The policy must
        not even be consulted — handing it an empty listing is how a rename
        happens on no evidence."""
        from claude_swap import oauth

        seen = []
        self._policy(monkeypatch,
                     lambda sessions, names: seen.append(sessions) or [])
        monkeypatch.setattr(oauth, "_list_bridge_sessions", lambda tok: None)
        assert oauth.restore_bridge_titles("tok", {"cse_a": "slack"}) == (
            0, "list-failed"), "a failed listing must be distinguishable"
        assert seen == [], "the policy was asked to judge a listing we never got"

    def test_a_rename_that_fails_is_not_counted(self, monkeypatch):
        """The count is what a caller logs. Counting an attempt would report a
        repair that did not happen — the shape this whole area keeps producing.
        """
        from claude_swap import oauth

        self._policy(monkeypatch, lambda sessions, names: [("cse_a", "slack")])
        monkeypatch.setattr(oauth, "_list_bridge_sessions",
                            lambda tok: [{"id": "cse_a", "title": "invented"}])
        monkeypatch.setattr(oauth, "_put_bridge_title",
                            lambda tok, sid, title: False)
        assert oauth.restore_bridge_titles("tok", {"cse_a": "slack"}) == (
            0, "all-puts-refused")

    def test_it_never_raises_into_its_caller(self, monkeypatch):
        """`AutoSwitchEngine.tick()` is documented "Never raises". A cosmetic
        repair must not end a tick that was about to prevent a lockout."""
        from claude_swap import oauth

        def boom(tok):
            raise RuntimeError("upstream on fire")

        self._policy(monkeypatch, lambda sessions, names: [])
        monkeypatch.setattr(oauth, "_list_bridge_sessions", boom)
        assert oauth.restore_bridge_titles("tok", {"cse_a": "slack"}) == (
            0, "raised:RuntimeError")

    def test_a_host_without_the_pin_extra_loses_the_repair_not_the_tick(self):
        """cswap-pin is optional and ships on its own schedule."""
        import sys

        from claude_swap import oauth

        saved = {k: sys.modules.pop(k, None)
                 for k in ("cswap_pin", "cswap_pin.proxy")}
        sys.modules["cswap_pin"] = None  # force ImportError on import
        try:
            assert oauth.restore_bridge_titles("tok", {"cse_a": "slack"}) == (
                0, "no-extra")
        finally:
            sys.modules.pop("cswap_pin", None)
            for k, v in saved.items():
                if v is not None:
                    sys.modules[k] = v

    def test_the_real_policy_is_the_one_wired(self):
        """The only claim here that the injected cases cannot make.

        They prove the transport calls SOMETHING; this proves it is cswap-pin's
        decision and not a second copy. It needs the optional extra, so it does
        not run on a machine without it — and a skip reads exactly like a pass,
        so the reason says so out loud rather than being silently absent.
        """
        import pytest

        pytest.importorskip(
            "cswap_pin.proxy",
            reason="THIS CASE DID NOT RUN: cswap-pin is not installed here, so "
                   "nothing in this file has checked that the real policy is "
                   "wired. CI installs the [pin] extra and does check it.",
        )
        import inspect

        from claude_swap import oauth, pin

        # BOTH HOPS, because there are two now. oauth.py stopped importing the
        # package directly — it asks the seam, and the seam asks the package.
        # Checking only the first hop would pass on a seam that returned a
        # hardcoded list, which is the exact second copy this case exists to
        # forbid.
        caller = inspect.getsource(oauth.restore_bridge_titles)
        assert "titles_to_restore" in caller, "the transport calls nothing"
        assert "_pin.titles_to_restore" in caller, (
            "the transport must go through the seam, not around it")

        # THREE HOPS NOW: oauth -> pin.titles_to_restore -> pin._ask ->
        # the package. The passthroughs were collapsed onto one caller, so
        # checking only the named wrapper would pass on a wrapper that
        # decided something itself.
        seam = inspect.getsource(pin.titles_to_restore)
        assert '_ask("titles_to_restore"' in seam, (
            "the seam must forward, not decide anything itself")
        asker = inspect.getsource(pin._ask)
        assert "getattr(impl, name)" in asker, (
            "the shared caller must reach the package by name")

        # AND THE SEAM RESOLVES THE REAL PACKAGE. `_impl` is what makes
        # `impl.titles_to_restore` the package's function rather than any
        # object; importorskip above already proved the package is here.
        #
        # NOT ON WINDOWS, and the reason is the contract rather than the
        # runner. `_impl` RAISES there by design — the proxy holds its daemon
        # lock with fcntl.flock and refcounts through a FIFO, so there is no
        # real package to resolve and "resolves the real one" is not a
        # question that has an answer. `is_available` is the seam's own way of
        # saying so, so ask it rather than naming a platform here.
        #
        # This is the seam being STRICTER than the four bypasses it replaced:
        # each of those degraded silently on a platform where the pin cannot
        # exist, and no single site had to admit it. The two asserts above
        # still run everywhere, so the wiring claim is not lost — only the
        # resolution half, which Windows cannot make.
        if pin.is_available():
            assert pin._impl().titles_to_restore.__module__ == "cswap_pin.proxy"


class TestInvalidGrantTaxonomy:
    """M3: the permanent invalid_grant verdict requires an RFC 6749 §5.2
    parse — top-level error == "invalid_grant" in the JSON body. Substring
    hits inside other envelopes stay transient; invalid_client is a distinct
    systemic kind, never a dead-token verdict."""

    def _refresh_with_body(self, monkeypatch, code, body):
        import urllib.error, io
        creds = json.dumps({
            "claudeAiOauth": {"refreshToken": "rt-x", "accessToken": "a"}
        })

        def raise_http(*a, **k):
            raise urllib.error.HTTPError(
                "url", code, "err", {}, io.BytesIO(body.encode())
            )

        monkeypatch.setattr(
            "claude_swap.oauth.urllib.request.urlopen", raise_http
        )
        return oauth.try_refresh_oauth_credentials(creds)

    def test_rfc_invalid_grant_is_permanent(self, monkeypatch):
        out = self._refresh_with_body(
            monkeypatch, 400, '{"error": "invalid_grant"}'
        )
        assert out.error == "invalid_grant"

    def test_substring_in_other_envelope_is_transient(self, monkeypatch):
        # the marker appears only inside a nested message — not a §5.2 error
        out = self._refresh_with_body(
            monkeypatch, 400,
            '{"error": "server_error", "detail": "log mentions invalid_grant"}'
        )
        assert out.error == "transient"

    def test_invalid_client_is_systemic_not_dead_token(self, monkeypatch):
        out = self._refresh_with_body(
            monkeypatch, 401, '{"error": "invalid_client"}'
        )
        assert out.error == "invalid_client"

    def test_unparseable_body_is_transient(self, monkeypatch):
        out = self._refresh_with_body(monkeypatch, 400, "<html>oops</html>")
        assert out.error == "transient"

    def test_error_description_variant_still_permanent(self, monkeypatch):
        out = self._refresh_with_body(
            monkeypatch, 400,
            '{"error": "invalid_grant", "error_description": "revoked"}'
        )
        assert out.error == "invalid_grant"


class TestNoRefreshTokenStructuralGuard:
    """M3: ``no_refresh_token`` is permanent only for a structurally complete
    OAuth dict genuinely missing the field — an unparseable/partial blob is
    transient (a torn read must not condemn the slot)."""

    def test_complete_dict_without_rt_is_permanent(self):
        creds = json.dumps({"claudeAiOauth": {"accessToken": "a"}})
        out = oauth.try_refresh_oauth_credentials(creds)
        assert out.error == "no_refresh_token"

    def test_unparseable_blob_is_transient(self):
        out = oauth.try_refresh_oauth_credentials('{"claudeAiOa')  # torn read
        assert out.error == "transient"

    def test_non_dict_payload_is_transient(self):
        out = oauth.try_refresh_oauth_credentials('"just-a-string"')
        assert out.error == "transient"


class TestConsumeBusyIsDeterministic:
    """A busy consume gate must not fall through to a guaranteed 401.

    `consume-busy` means another process holds the gate — the token in hand is
    known-expired, so calling the usage endpoint with it 401s every time, and
    the retry re-enters the gate and gets busy again. The kind then arrives as
    generic "refresh-failed", hiding the distinct kind this PR added and
    spending a request per pass to learn nothing.
    """

    def test_a_busy_gate_does_not_spend_a_doomed_request(self):
        creds = json.dumps({
            "claudeAiOauth": {
                "accessToken": "expired",
                "refreshToken": "r",
                "expiresAt": 1,  # long past
            }
        })
        with patch("claude_swap.oauth.request_usage_data") as usage:
            out = oauth.try_fetch_usage_for_account(
                "1", "a@example.com", creds, is_active=False,
                refresh_via=lambda *_: oauth.RefreshOutcome(None, "consume-busy"),
            )
        assert out.error == "consume-busy", out.error
        usage.assert_not_called()

    def test_every_deterministic_kind_has_a_note(self):
        """The reason these kinds stay distinct is the note they carry.

        ``try_fetch_usage_for_account`` keeps a deterministic kind rather than
        collapsing it to "refresh-failed" because "ERROR_NOTES renders the
        remedy for each" — a kind with no note renders the bare identifier,
        which is strictly worse than the generic string it displaced.
        """
        from claude_swap.switcher import ERROR_NOTES

        missing = [
            k for k in oauth._DETERMINISTIC_REFRESH_ERRORS if k not in ERROR_NOTES
        ]
        assert not missing, missing


# OPTS OUT OF #216's AUTOUSE `block_real_oauth_profile_fetch`, which replaces
# `fetch_oauth_profile` with a stub so dozens of unrelated tests do not open
# real 5s HTTPS connections through add_account's identity guard. This class
# mocks `urlopen` BENEATH that function and asserts the call reaches it, so a
# stub above makes the assertion unreachable — "fetch_oauth_profile made no
# request", green on either branch alone and red only on the merged tree.
#
# The marker is the escape hatch that fixture's own docstring already names for
# exactly this shape. Narrowing the fixture instead would trade a hermetic
# suite for a merge convenience.
@pytest.mark.no_oauth_profile_fake
class TestEveryPinRoutedCallCarriesThePinAwareContext:
    """The helper existing is not the helper being USED.

    `_pin_aware_ssl_context` is covered by a test that proves it ADDS rather
    than replaces. Nothing proved any call site passes it. That is the shape
    where a fix ships and changes nothing: the builder is correct, the callers
    still hand urllib the default context, and every request through the pin
    keeps dying CERTIFICATE_VERIFY_FAILED exactly as before.

    Measured through the pin on all three machines, each with its own
    interpreter and its port read from `cswap pin --get_port`:

        host-b      default FAIL -> +pin CA HTTP 429
        host-c  default FAIL -> +pin CA HTTP 429
        host-a-docker      default FAIL -> +pin CA HTTP 429

    So every one of these calls needs the context, not just the bridge pair
    that happened to be noticed first.

    THE SENTINEL IS THE POINT. Asserting `context is not None` would pass on a
    site that built its own bare `ssl.create_default_context()` — which is the
    unfixed behaviour wearing the right shape. Identity against the sentinel
    is the only assertion that separates them.
    """

    SENTINEL = object()

    def _capture(self, monkeypatch):
        """Record the kwargs of every urlopen this module makes."""
        seen = []

        class _Resp:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

            def read(self_inner):
                return b"{}"

        def _fake_urlopen(req, **kwargs):
            seen.append(kwargs)
            return _Resp()

        monkeypatch.setattr(oauth, "_pin_aware_ssl_context",
                            lambda: self.SENTINEL)
        monkeypatch.setattr(oauth.urllib.request, "urlopen", _fake_urlopen)
        return seen

    def test_the_usage_request_carries_it(self, monkeypatch):
        """acct7's call. This is the one the failure was measured on."""
        seen = self._capture(monkeypatch)
        oauth.request_usage_data("tok")
        assert seen, "request_usage_data made no request"
        assert seen[0].get("context") is self.SENTINEL, (
            "request_usage_data did not pass the pin-aware context — through "
            "the pin this call cannot verify api.anthropic.com")

    def test_the_profile_request_carries_it(self, monkeypatch):
        seen = self._capture(monkeypatch)
        oauth.fetch_oauth_profile("tok")
        assert seen, "fetch_oauth_profile made no request"
        assert seen[0].get("context") is self.SENTINEL, (
            "fetch_oauth_profile did not pass the pin-aware context")

    def test_the_bridge_listing_carries_it(self, monkeypatch):
        seen = self._capture(monkeypatch)
        oauth._list_bridge_sessions("tok")
        assert seen, "_list_bridge_sessions made no request"
        assert seen[0].get("context") is self.SENTINEL

    def test_the_bridge_rename_carries_it(self, monkeypatch):
        seen = self._capture(monkeypatch)
        oauth._put_bridge_title("tok", "cse_a", "name")
        assert seen, "_put_bridge_title made no request"
        assert seen[0].get("context") is self.SENTINEL

    def test_a_body_that_is_not_an_object_is_a_listing_failure(
        self, monkeypatch
    ):
        """`data.get(...)` sat OUTSIDE the try, in a function documented to
        return None on failure.

        A JSON array or string body — an error envelope, a captive portal page
        that happens to parse — makes it raise AttributeError out of
        `_list_bridge_sessions` entirely. The caller's outer handler then
        records `raised:AttributeError` instead of `list-failed`, collapsing
        the distinction the (renamed, outcome) return value exists to preserve
        and routing an ordinary transport fault into the branch reserved for
        programming errors.
        """
        import contextlib
        import io

        class _Resp:
            def __init__(self, body):
                self._body = body

            def read(self):
                return self._body

        for body in (b'["not", "an", "object"]', b'"a string"', b'42'):
            @contextlib.contextmanager
            def _open(req, timeout=None, context=None, _b=body):
                yield _Resp(_b)

            monkeypatch.setattr(oauth.urllib.request, "urlopen", _open)
            with contextlib.redirect_stderr(io.StringIO()):
                got = oauth._list_bridge_sessions("tok")
            assert got is None, (
                f"a {body!r} body returned {got!r} instead of the None that "
                "means 'could not list' — or raised out of a function that "
                "documents itself as never raising")

    def test_the_token_refresh_needs_no_pin_ca(self):
        """THE ONE SITE THAT DOES NOT NEED IT, and the docstring used to name
        it as though it did.

        The helper is only necessary where the pin MITMs the host. It MITMs
        `UPSTREAM_HOST` and blind-tunnels everything else, so a call to a
        different host is verified against the REAL certificate by an ordinary
        default context. Adding ours there would buy nothing and reload the
        system CA store on every refresh.

        Pinned as a test rather than a comment because it is a relationship
        between two packages: the day the pin starts MITMing more than one
        host, this fails and the refresh call needs the context.
        """
        from urllib.parse import urlparse

        mitm = None
        try:
            from cswap_pin.proxy import UPSTREAM_HOST as mitm
        except Exception:  # noqa: BLE001 — optional extra, nothing to check
            pytest.skip("cswap-pin is not installed")

        assert urlparse(oauth.OAUTH_TOKEN_URL).hostname != mitm, (
            f"the token endpoint is on {mitm}, which the pin re-signs — that "
            "call now needs `_pin_aware_ssl_context()` like the other three, "
            "and without it every refresh dies CERTIFICATE_VERIFY_FAILED and "
            "is reported as a transient network blip")
        assert urlparse(oauth._BRIDGE_SESSIONS_URL).hostname == mitm, (
            "the scan is broken: if the bridge endpoint is not on the MITM'd "
            "host either, this case proves nothing about the split")


class TestTheSslContextCacheHasACeiling:
    """It kept one full trust store per CA REGENERATION, forever.

    Keyed on the CA file's path and mtime, so a re-pin — which regenerates the
    CA — inserted a new entry while the old `SSLContext`, holding a parsed copy
    of the system roots (~130 of them), stayed referenced. In `cswap tui`, the
    menu-bar app or a long-running `cswap auto`, repeated re-pins grew it
    monotonically. "Bounded by construction: a machine has one pin CA" held
    only until the CA changed, which is precisely what a re-pin does.
    """

    def test_a_hundred_regenerations_leave_one_context(self, monkeypatch):
        from claude_swap import oauth

        oauth._PIN_CTX_SLOT = None
        made = []

        # A CONTEXT THAT ACCEPTS THE CA, because the fingerprint below says
        # there is one. A bare `object()` made `load_verify_locations` an
        # AttributeError, so this walked the no-CA path while claiming to
        # regenerate a CA a hundred times.
        class _Ctx:
            def load_verify_locations(self, cafile=None):
                pass

        monkeypatch.setattr(
            oauth.ssl, "create_default_context",
            lambda *a, **k: made.append(1) or _Ctx())
        # Each "regeneration" is a new mtime on the same path, which is what
        # the key is built from.
        for i in range(100):
            monkeypatch.setattr(
                oauth, "_pin_ca_fingerprint", lambda i=i: ("/p/ca.pem", i))
            oauth._pin_aware_ssl_context()

        held = 0 if oauth._PIN_CTX_SLOT is None else 1
        assert held == 1, (
            f"the cache holds {held} contexts after 100 CA regenerations; each "
            "carries a parsed copy of the system trust store")
        # CONTROL: it must still be CACHING. A slot that holds one because it
        # rebuilds every call passes the assertion above and defeats the point.
        before = len(made)
        oauth._pin_aware_ssl_context()
        assert len(made) == before, (
            "a repeat call with an unchanged CA built a new context, so the "
            "single slot is not caching at all")
        oauth._PIN_CTX_SLOT = None


class TestThePolicyFetchActuallyCarriesItsBudget:
    """THE ONLY LINE THAT DELIVERS THE BUDGET HAD NO WITNESS.

    `_perform_switch` blocks on this fetch between writing the credential and
    writing `activeAccountNumber`, so the timeout bounds a window where the
    two disagree. The seam and the call site are both tested -- but reverting
    `timeout=timeout_s` to a literal left the whole suite byte-identical,
    because those tests spy on `fetch_policy_limits` itself and never on what
    it hands `urlopen`. A seam that passes 2.0 to a function that ignores it
    is a budget in name only.
    """

    def _run(self, monkeypatch, timeout_s):
        import claude_swap.oauth as oauth

        seen = {}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"{}"

        def _fake_urlopen(req, timeout=None, **_kw):
            seen["timeout"] = timeout
            return _Resp()

        monkeypatch.setattr(oauth.urllib.request, "urlopen", _fake_urlopen)
        monkeypatch.setattr(oauth, "_pin_aware_ssl_context", lambda: None)
        oauth.fetch_policy_limits("tok", timeout_s=timeout_s)
        return seen

    def test_the_caller_s_budget_reaches_urlopen(self, monkeypatch):
        assert self._run(monkeypatch, 0.25)["timeout"] == 0.25, (
            "the request went out on some other deadline, so the switch can "
            "block for longer than the budget it was given")

    def test_a_second_value_proves_it_is_not_hardcoded(self, monkeypatch):
        """THE CONTROL. One value can be matched by a literal that happens to
        agree; two cannot."""
        assert self._run(monkeypatch, 3.5)["timeout"] == 3.5


class TestAFailedCaLoadIsNotCached:
    """A transient failure must not outlive itself.

    The cache is keyed on the CA file's path and mtime, and neither moves
    because a load failed. So caching the fallback context under that key
    freezes it for the life of the process: every later bridge listing,
    profile fetch and usage poll goes out on a context that does not trust the
    pin's CA, dies CERTIFICATE_VERIFY_FAILED, and is swallowed to debug --
    which is the silent hours this helper exists to end.
    """

    def _spy_ctx(self, monkeypatch, oauth, ca, fail_load=False, second_ca=...):
        loaded = []

        class _Ctx:
            def load_verify_locations(self, cafile=None):
                if fail_load and not loaded:
                    loaded.append(None)
                    raise OSError("CA momentarily unreadable")
                loaded.append(cafile)

        monkeypatch.setattr(oauth.ssl, "create_default_context", _Ctx)
        return loaded

    def test_a_transient_load_failure_is_retried(self, tmp_path, monkeypatch):
        from claude_swap import oauth, pin

        ca = tmp_path / "ca.pem"
        ca.write_text("cert", encoding="utf-8")
        monkeypatch.setattr(oauth, "_PIN_CTX_SLOT", None)
        monkeypatch.setattr(pin, "ca_path_for_trust", lambda: ca)
        loaded = self._spy_ctx(monkeypatch, oauth, ca, fail_load=True)

        oauth._pin_aware_ssl_context()
        assert loaded == [None], f"the failure did not happen: {loaded}"
        oauth._pin_aware_ssl_context()
        assert loaded == [None, str(ca)], (
            "the context built WITHOUT the pin CA was cached under the CA's "
            "own path+mtime, so nothing can ever rebuild it: every later "
            f"request dies CERTIFICATE_VERIFY_FAILED. {loaded}")

    def test_the_ca_is_read_once_per_rebuild(self, tmp_path, monkeypatch):
        """The key and the file loaded must come from ONE read.

        `ca_path_for_trust` collapses every failure inside the optional
        package to None, so asking it twice lets the two answers disagree: a
        None on the second read caches a context with no CA under a key that
        says one was there, and the key cannot move on its own.
        """
        from claude_swap import oauth, pin

        ca = tmp_path / "ca.pem"
        ca.write_text("cert", encoding="utf-8")
        asked = []
        monkeypatch.setattr(oauth, "_PIN_CTX_SLOT", None)
        monkeypatch.setattr(pin, "ca_path_for_trust",
                            lambda: asked.append(1) or ca)
        self._spy_ctx(monkeypatch, oauth, ca)

        oauth._pin_aware_ssl_context()
        assert asked == [1], (
            f"the CA was read {len(asked)} times to build one context, so the "
            "key and the file loaded are two separate answers that can "
            "disagree")
