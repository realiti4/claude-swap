"""Tests for the oauth module."""

from __future__ import annotations

import json
import urllib.error
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

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

        def mock_urlopen(req, timeout=0):
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

    def test_invalid_json_is_permanent(self):
        outcome = oauth.try_refresh_oauth_credentials("not json")
        assert outcome.error == "no_refresh_token"

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

        def mock_urlopen(req, timeout=0):
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

        def mock_urlopen(req, timeout=0):
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

        def mock_urlopen(req, timeout=0):
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

        def mock_urlopen(req, timeout=0):
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

        def mock_urlopen(req, timeout=0):
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

        def mock_urlopen(req, timeout=0):
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

        def mock_urlopen(req, timeout=0):
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
        # Any 429 = the per-token usage budget, which cumulative polling
        # across cswap surfaces can drain — the log says what is happening.
        assert "per-token usage budget" in line

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
        # "Retry-After: 0" is the drained-bucket edge — same budget hint.
        assert "retry-after 0s" in line
        assert "per-token usage budget" in line

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

        def mock_urlopen(req, timeout=0):
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

        def mock_urlopen(req, timeout=0):
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
