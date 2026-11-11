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

    def test_condemned_true_refuses_before_any_network_call(self):
        """The guard's polarity, proven at the chokepoint itself: a CONFIRMED
        mismatch refuses without a single byte on the wire."""
        with patch("claude_swap.oauth.urllib.request.urlopen") as mock_urlopen:
            outcome = oauth.try_refresh_oauth_credentials(
                self._make_credentials(), condemned=lambda fp: True,
            )
        mock_urlopen.assert_not_called()
        assert outcome.error == "foreign-lineage"
        assert outcome.credentials is None

    def test_CONTROL_condemned_false_or_absent_still_posts(self):
        """Positive control: without this, the RED test above would pass
        just as well for a guard that refuses every refresh. Absence of
        evidence (``condemned`` returning False, or not passed at all) must
        never refuse — refusing a legitimate refresh is the harm the guard
        exists to prevent."""
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
            no_condemned = oauth.try_refresh_oauth_credentials(
                self._make_credentials()
            )
            not_condemned = oauth.try_refresh_oauth_credentials(
                self._make_credentials(), condemned=lambda fp: False,
            )
        assert no_condemned.error is None
        assert not_condemned.error is None

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
        a user-visible warning to stderr, so a ``--json`` payload on stdout
        stays one parseable object.
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

        # Also verify the user-visible printed warning, and that stdout stays clean
        captured = capsys.readouterr()
        assert "failed to save refreshed token" in captured.err
        assert "cswap --add-account" in captured.err
        assert captured.out == ""


class TestNativeTlsFallbackIsAudible:
    """A silent fallback to stdlib ssl is a silent NARROWING OF TRUST.

    ``_use_native_tls`` swallows every exception so the CLI is never blocked
    over a trust nicety. That part is right. What is wrong is that it leaves no
    trace, and the two paths do not trust the same roots.

    Measured on macOS, comparing the OS keychains against what stdlib
    actually loads:

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
                        if r.name == "claude-swap"
                        and r.levelno == logging.WARNING]
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
    def _http_error(code: int, headers: dict | None = None, body: bytes | None = None):
        import email.message
        import io
        hdrs = None
        if headers is not None:
            hdrs = email.message.Message()
            for k, v in headers.items():
                hdrs[k] = v
        return urllib.error.HTTPError(
            url="https://api.anthropic.com/api/oauth/usage",
            code=code, msg="err", hdrs=hdrs,
            fp=None if body is None else io.BytesIO(body),
        )

    def test_http_codes(self):
        assert oauth._classify_usage_error(self._http_error(429))[0] == "http-429"
        assert oauth._classify_usage_error(self._http_error(500))[0] == "http-500"
        assert oauth._classify_usage_error(self._http_error(401))[0] == "http-401"

    ORG = "oauth_not_allowed_for_organization"
    ORG_BODY = json.dumps({"error": {"details": {"error_code": ORG}}}).encode()

    @pytest.mark.parametrize("code, body, kind", [
        (403, ORG_BODY, ORG),
        (401, ORG_BODY, "http-401"),
        (403, None, "http-403"),
        (403, b"<html>forbidden</html>", "http-403"),
        (403, b'{"error": "x"}', "http-403"),
    ])
    def test_403_org_oauth_policy_names_itself(self, code, body, kind):
        """A 403 whose body carries the server's org-policy code is told apart
        from every other 403; an unreadable or differently shaped body, and any
        other status, keep ``http-<code>``. Retry-After rides along either way."""
        err = self._http_error(code, {"Retry-After": "7"}, body)
        assert oauth._classify_usage_error(err) == (kind, 7.0)

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

    @pytest.mark.parametrize("reason, kind", [
        (ssl.SSLCertVerificationError(1, "certificate verify failed"), "tls-cert"),
        (ssl.SSLEOFError("EOF occurred in violation of protocol"), "network"),
        (ssl.SSLError("wrong version number"), "network"),
    ])
    def test_refused_certificate_is_told_apart(self, reason, kind):
        """Only a certificate the client refused is ``tls-cert``; a TLS failure
        of any other class stays ``network``, since no CA repairs it."""
        err = urllib.error.URLError(reason)
        assert oauth._classify_usage_error(err) == (kind, None)

    def test_refused_certificate_shows_its_remedy(self):
        """The unavailable line names the fix, not the bare kind."""
        from claude_swap.switcher import _usage_entry_lines
        from claude_swap.usage_store import UsageEntry

        (line,) = _usage_entry_lines(UsageEntry(last_error="tls-cert"))
        assert "SSL_CERT_FILE" in line and "tls-cert" not in line

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


def _headers(values: dict):
    import email.message
    hdrs = email.message.Message()
    for key, value in values.items():
        hdrs[key] = value
    return hdrs


# Shape measured on a live setup-token account (values illustrative).
_UNIFIED_HEADERS = {
    "anthropic-ratelimit-unified-status": "allowed",
    "anthropic-ratelimit-unified-5h-status": "allowed",
    "anthropic-ratelimit-unified-5h-utilization": "0.34",
    "anthropic-ratelimit-unified-5h-reset": "1790766000",
    "anthropic-ratelimit-unified-7d-status": "allowed",
    "anthropic-ratelimit-unified-7d-utilization": "0.6",
    "anthropic-ratelimit-unified-7d-reset": "1791154800",
    "anthropic-ratelimit-unified-representative-claim": "five_hour",
}


class TestIsSetupToken:
    def test_add_token_shape(self):
        assert oauth.is_setup_token(
            {"accessToken": "t", "scopes": ["user:inference"]}
        )

    def test_login_with_refresh_token_is_not(self):
        assert not oauth.is_setup_token({
            "accessToken": "t",
            "refreshToken": "r",
            "scopes": ["user:inference"],
        })

    def test_profile_scope_is_not(self):
        assert not oauth.is_setup_token({
            "accessToken": "t",
            "scopes": ["user:inference", "user:profile"],
        })

    def test_missing_scopes_is_not(self):
        # Unknown provenance: keep the endpoint's answer, don't spend quota.
        assert not oauth.is_setup_token({"accessToken": "t"})
        assert not oauth.is_setup_token(None)


class TestParseUnifiedRatelimitHeaders:
    def test_maps_fractions_and_epochs_to_usage_api_shape(self):
        data = oauth.parse_unified_ratelimit_headers(_headers(_UNIFIED_HEADERS))
        assert data["five_hour"]["utilization"] == 34.0
        assert data["seven_day"]["utilization"] == 60.0
        assert datetime.fromisoformat(
            data["five_hour"]["resets_at"]
        ).timestamp() == 1790766000
        # Feeds build_usage_result unchanged.
        usage = oauth.build_usage_result(data)
        assert usage["five_hour"]["pct"] == 34.0
        assert usage["seven_day"]["pct"] == 60.0
        assert "clock" in usage["seven_day"]
        assert "scoped" not in usage

    def test_missing_window_is_left_out(self):
        values = {
            k: v for k, v in _UNIFIED_HEADERS.items() if "-7d-" not in k
        }
        data = oauth.parse_unified_ratelimit_headers(_headers(values))
        assert set(data) == {"five_hour"}

    def test_unusable_reset_keeps_utilization(self):
        values = dict(_UNIFIED_HEADERS)
        values["anthropic-ratelimit-unified-5h-reset"] = "soon"
        data = oauth.parse_unified_ratelimit_headers(_headers(values))
        assert data["five_hour"] == {"utilization": 34.0}

    def test_no_headers(self):
        assert oauth.parse_unified_ratelimit_headers(_headers({})) == {}

    def test_fable_7d_oi_becomes_the_fable_scoped_window(self):
        values = dict(_UNIFIED_HEADERS)
        values["anthropic-ratelimit-unified-7d_oi-utilization"] = "0.82"
        values["anthropic-ratelimit-unified-7d_oi-reset"] = "1791154800"
        usage = oauth.build_usage_result(
            oauth.parse_unified_ratelimit_headers(_headers(values))
        )
        [fable] = usage["scoped"]
        assert fable["name"] == "Fable"
        assert fable["pct"] == 82.0
        assert datetime.fromisoformat(fable["resets_at"]).timestamp() == 1791154800

    def test_rejected_status_is_kept(self):
        values = dict(_UNIFIED_HEADERS)
        values["anthropic-ratelimit-unified-status"] = "rejected"
        values["anthropic-ratelimit-unified-reset"] = "1790766000"
        data = oauth.parse_unified_ratelimit_headers(_headers(values))
        assert datetime.fromisoformat(
            data["rejected"]["resets_at"]
        ).timestamp() == 1790766000


class TestSetupTokenHeaderProbe:
    """Setup-token accounts: the usage endpoint cannot read them, so usage is
    read from the rate-limit headers of a 1-token model request (opt-in)."""

    TOKEN = "sk-ant-oat01-secret-token"
    USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

    def _creds(self) -> str:
        return json.dumps({"claudeAiOauth": {
            "accessToken": self.TOKEN, "scopes": ["user:inference"],
        }})

    def _endpoint_error(self, code: int = 429):
        return urllib.error.HTTPError(
            self.USAGE_URL, code, "Too Many",
            hdrs=_headers({"Retry-After": "318"}), fp=None,
        )

    @staticmethod
    def _ok(headers: dict):
        resp = MagicMock()
        resp.headers = _headers(headers)
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def _route(self, endpoint, probe):
        """urlopen fake: ``endpoint``/``probe`` are a response or an
        exception for the usage GET and the messages POST respectively."""
        calls: list = []

        def fake_urlopen(req, timeout=None, **_kw):
            calls.append(req)
            result = probe if req.full_url == oauth.USAGE_PROBE_URL else endpoint
            if isinstance(result, Exception):
                raise result
            return result

        return fake_urlopen, calls

    def _fetch(self, fake_urlopen, creds=None, header_probe=True, **kw):
        with patch("claude_swap.oauth.urllib.request.urlopen", side_effect=fake_urlopen):
            return oauth.try_fetch_usage_for_account(
                "3", "a@b.c", creds or self._creds(),
                is_active=kw.pop("is_active", False),
                header_probe=header_probe, **kw,
            )

    def _probe_429(self, values: dict):
        return urllib.error.HTTPError(
            oauth.USAGE_PROBE_URL, 429, "Too Many",
            hdrs=_headers(values), fp=None,
        )

    def test_setup_token_skips_the_endpoint_and_probes(self, caplog):
        import logging
        fake, calls = self._route(
            self._endpoint_error(), self._ok(_UNIFIED_HEADERS)
        )
        with caplog.at_level(logging.DEBUG, logger="claude-swap"):
            outcome = self._fetch(fake)

        assert outcome.error is None
        assert outcome.retry_after_s is None
        assert outcome.usage["five_hour"]["pct"] == 34.0
        assert outcome.usage["seven_day"]["pct"] == 60.0
        assert outcome.usage["source"] == oauth.USAGE_SOURCE_HEADERS
        assert "rejected" not in outcome.usage
        # Never the usage endpoint: its inference-only scope cannot read it.
        # The main probe, then one per scoped model (Fable's weekly bucket).
        assert [c.full_url for c in calls] == [oauth.USAGE_PROBE_URL] * (
            1 + len(oauth.USAGE_PROBE_SCOPED_MODELS)
        )
        assert [json.loads(c.data)["model"] for c in calls[1:]] == list(
            oauth.USAGE_PROBE_SCOPED_MODELS
        )
        probe = calls[0]
        assert probe.get_method() == "POST"
        assert probe.get_header("Authorization") == f"Bearer {self.TOKEN}"
        assert probe.get_header("Anthropic-beta") == oauth.OAUTH_BETA_HEADER
        assert probe.get_header("Anthropic-version") == "2023-06-01"
        body = json.loads(probe.data)
        assert body["model"] == oauth.USAGE_PROBE_MODEL
        assert body["max_tokens"] == 1
        messages = [r.getMessage() for r in caplog.records]
        assert not any(
            r.levelno >= logging.WARNING for r in caplog.records
        )
        assert not any(self.TOKEN in m for m in messages)

    def _route_by_model(self, by_model: dict):
        """urlopen fake answering each probe by its request's model."""
        calls: list = []

        def fake_urlopen(req, timeout=None):
            calls.append(req)
            assert req.full_url == oauth.USAGE_PROBE_URL
            result = by_model[json.loads(req.data)["model"]]
            if isinstance(result, Exception):
                raise result
            return result

        return fake_urlopen, calls

    _FABLE_HEADERS = {
        **_UNIFIED_HEADERS,
        "anthropic-ratelimit-unified-7d_oi-status": "allowed_warning",
        "anthropic-ratelimit-unified-7d_oi-utilization": "0.97",
        "anthropic-ratelimit-unified-7d_oi-reset": "1791154800",
    }

    def test_fable_bucket_is_read_from_a_fable_probe(self):
        """The Haiku probe never carries ``7d_oi``; the Fable probe does."""
        fake, _ = self._route_by_model({
            oauth.USAGE_PROBE_MODEL: self._ok(_UNIFIED_HEADERS),
            "claude-fable-5-1": self._ok(self._FABLE_HEADERS),
        })
        usage = self._fetch(fake).usage
        assert usage["seven_day"]["pct"] == 60.0
        assert [(w["name"], w["pct"]) for w in usage["scoped"]] == [("Fable", 97.0)]
        assert usage["scoped"][0]["resets_at"]

    def _fable_refused(self, values: dict):
        return self._probe_429({
            **values, "anthropic-ratelimit-unified-status": "rejected",
        })

    def test_refused_fable_reads_at_its_limit_whatever_its_pct(self):
        """A rejected Fable probe at 97% is Fable unavailable until the
        refusal ends; 5h/7d stay the main probe's and the account is not
        rejected."""
        values = {
            **self._FABLE_HEADERS,
            "anthropic-ratelimit-unified-7d_oi-status": "rejected",
            "Retry-After": "600",
        }
        fake, _ = self._route_by_model({
            oauth.USAGE_PROBE_MODEL: self._ok(_UNIFIED_HEADERS),
            "claude-fable-5-1": self._fable_refused(values),
        })
        before = datetime.now(timezone.utc).timestamp()
        usage = self._fetch(fake).usage
        assert usage["five_hour"]["pct"] == 34.0
        assert usage["seven_day"]["pct"] == 60.0
        assert "rejected" not in usage
        [fable] = usage["scoped"]
        assert (fable["name"], fable["pct"]) == ("Fable", 100.0)
        until = datetime.fromisoformat(fable["resets_at"]).timestamp()
        assert before + 600 <= until <= before + 660
        assert oauth.account_headroom(usage) == 40.0
        assert oauth.account_headroom(usage, ["Fable"]) == 0.0

    def test_refused_fable_without_a_utilization_still_shows_its_row(self):
        """No utilization header: the row stays, at 100%, resetting at the
        bucket's own reset when there is no Retry-After."""
        fake, _ = self._route_by_model({
            oauth.USAGE_PROBE_MODEL: self._ok(_UNIFIED_HEADERS),
            "claude-fable-5-1": self._fable_refused({
                "anthropic-ratelimit-unified-7d_oi-reset": "1791154800",
                "anthropic-ratelimit-unified-reset": "1790766000",
            }),
        })
        [fable] = self._fetch(fake).usage["scoped"]
        assert fable["pct"] == 100.0
        assert datetime.fromisoformat(fable["resets_at"]).timestamp() == 1791154800

    def test_bare_429_on_the_fable_probe_adds_no_row(self):
        """No rejected status proves nothing about Fable, even with a
        utilization header: the main reading stands alone."""
        fake, _ = self._route_by_model({
            oauth.USAGE_PROBE_MODEL: self._ok(_UNIFIED_HEADERS),
            "claude-fable-5-1": self._probe_429(
                {**self._FABLE_HEADERS, "Retry-After": "30"}
            ),
        })
        outcome = self._fetch(fake)
        assert outcome.error is None
        assert outcome.usage["seven_day"]["pct"] == 60.0
        assert "scoped" not in outcome.usage

    def test_no_fable_probe_after_a_refused_main_probe(self):
        values = {
            **_UNIFIED_HEADERS,
            "anthropic-ratelimit-unified-status": "rejected",
            "Retry-After": "600",
        }
        fake, calls = self._route_by_model({
            oauth.USAGE_PROBE_MODEL: self._probe_429(values),
            "claude-fable-5-1": self._ok(self._FABLE_HEADERS),
        })
        usage = self._fetch(fake).usage
        assert usage["rejected"]["retry_after_s"] == 600.0
        assert "scoped" not in usage
        assert [json.loads(c.data)["model"] for c in calls] == [
            oauth.USAGE_PROBE_MODEL
        ]

    @pytest.mark.parametrize("failure", [
        pytest.param(
            urllib.error.HTTPError(oauth.USAGE_PROBE_URL, 404, "nf", hdrs=None, fp=None),
            id="no-access-to-model",
        ),
        pytest.param(urllib.error.URLError("down"), id="network"),
    ])
    def test_a_failed_fable_probe_keeps_the_main_reading(self, failure):
        fake, _ = self._route_by_model({
            oauth.USAGE_PROBE_MODEL: self._ok(_UNIFIED_HEADERS),
            "claude-fable-5-1": failure,
        })
        outcome = self._fetch(fake)
        assert outcome.error is None
        assert outcome.usage["seven_day"]["pct"] == 60.0
        assert "scoped" not in outcome.usage

    def test_active_account_probes_too(self):
        fake, _ = self._route(self._endpoint_error(), self._ok(_UNIFIED_HEADERS))
        outcome = self._fetch(fake, is_active=True)
        assert outcome.usage["seven_day"]["pct"] == 60.0

    @pytest.mark.parametrize("is_active", [False, True])
    def test_disabled_sends_no_request(self, is_active):
        fake, calls = self._route(self._endpoint_error(), self._ok(_UNIFIED_HEADERS))
        outcome = self._fetch(fake, header_probe=False, is_active=is_active)
        assert outcome.usage is None
        assert outcome.error == "setup-token"
        assert calls == []

    def test_exhausted_account_reads_headers_off_the_429(self):
        values = dict(_UNIFIED_HEADERS)
        values["anthropic-ratelimit-unified-status"] = "rejected"
        values["anthropic-ratelimit-unified-5h-utilization"] = "1.0"
        fake, _ = self._route(self._endpoint_error(), self._probe_429(values))
        outcome = self._fetch(fake)
        assert outcome.error is None
        assert outcome.usage["five_hour"]["pct"] == 100.0
        assert oauth.account_headroom(outcome.usage) == 0.0

    def test_rejected_probe_below_100_is_still_at_its_limit(self, caplog):
        """A 429 whose 5h/7d numbers look healthy (another limit binds) keeps
        the rejection and its Retry-After: never a healthy switch target."""
        import logging
        values = dict(_UNIFIED_HEADERS)  # 5h 34%, 7d 60%
        values["anthropic-ratelimit-unified-status"] = "rejected"
        values["Retry-After"] = "600"
        fake, _ = self._route(self._endpoint_error(), self._probe_429(values))
        before = datetime.now(timezone.utc).timestamp()
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            outcome = self._fetch(fake)
        usage = outcome.usage
        # Shown as measured...
        assert usage["five_hour"]["pct"] == 34.0
        assert usage["seven_day"]["pct"] == 60.0
        # ...but decided on as at its limit until Retry-After passes.
        assert usage["rejected"]["retry_after_s"] == 600.0
        until = datetime.fromisoformat(usage["rejected"]["resets_at"]).timestamp()
        assert before + 600 <= until <= before + 660
        assert oauth.account_headroom(usage) == 0.0
        assert ("rejected", 100.0, usage["rejected"]["resets_at"]) in (
            oauth.relevant_windows(usage)
        )
        line = next(r.getMessage() for r in caplog.records)
        assert "rejected" in line and "account 3" in line
        assert "a@b.c" not in line and self.TOKEN not in line

    @pytest.mark.parametrize("values", [
        {"Retry-After": "30"},
        # Unified headers but no rejected status: still not a quota refusal.
        {**_UNIFIED_HEADERS, "Retry-After": "30"},
    ])
    def test_bare_429_stays_an_http_429_failure(self, values):
        """Only ``unified-status: rejected`` proves the quota refuses; a bare
        429 is a failure with its Retry-After, never zero headroom."""
        fake, _ = self._route(self._endpoint_error(), self._probe_429(values))
        outcome = self._fetch(fake)
        assert outcome.usage is None
        assert outcome.error == "http-429"
        assert outcome.retry_after_s == 30.0

    def test_revoked_token_surfaces_the_probe_401(self):
        revoked = urllib.error.HTTPError(
            oauth.USAGE_PROBE_URL, 401, "Unauthorized", hdrs=_headers({}), fp=None,
        )
        fake, _ = self._route(self._endpoint_error(), revoked)
        outcome = self._fetch(fake)
        assert outcome.error == "http-401"

    def test_response_without_headers_is_bad_response(self):
        fake, _ = self._route(self._endpoint_error(), self._ok({}))
        outcome = self._fetch(fake)
        assert outcome.usage is None
        assert outcome.error == "bad-response"

    def test_probe_network_error_is_classified(self):
        fake, _ = self._route(
            self._endpoint_error(), urllib.error.URLError(TimeoutError())
        )
        assert self._fetch(fake).error == "timeout"

    def test_login_credentials_never_probe(self):
        """A normal login's 429 is the endpoint budget: back off, don't
        spend the account's quota."""
        from datetime import timedelta
        future_ms = int(
            (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp() * 1000
        )
        creds = json.dumps({"claudeAiOauth": {
            "accessToken": "a", "refreshToken": "r", "expiresAt": future_ms,
            "scopes": ["user:inference", "user:profile"],
        }})
        fake, calls = self._route(self._endpoint_error(), self._ok(_UNIFIED_HEADERS))
        outcome = self._fetch(fake, creds=creds)
        assert outcome.error == "http-429"
        assert [c.full_url for c in calls] == [self.USAGE_URL]


class TestHeaderReadingDecisions:
    """A header-probe reading gates decisions only when it carries every
    window the decision needs; partial readings read as unknown."""

    H = oauth.USAGE_SOURCE_HEADERS

    def test_partial_reading_is_unknown_not_95_headroom(self):
        # 5h at 5% with no 7d header must not become 95% headroom.
        usage = {"five_hour": {"pct": 5.0}, "source": self.H}
        assert oauth.relevant_windows(usage) == []
        assert oauth.account_headroom(usage) is None

    def test_weekly_only_reading_decides(self):
        # An Ultra account has no 5h window, and its probe carries no 5h header.
        usage = {"seven_day": {"pct": 88.0}, "source": self.H}
        assert oauth.relevant_windows(usage) == [("7d", 88.0, None)]
        assert oauth.account_headroom(usage) == 12.0
        assert oauth.account_headroom(usage, ("Fable",)) is None
        assert oauth.account_headroom(usage, ("all",)) is None
        with_fable = {
            "seven_day": {"pct": 10.0},
            "scoped": [{"name": "Fable", "pct": 70.0}],
            "source": self.H,
        }
        assert oauth.account_headroom(with_fable, ("Fable",)) == 30.0

    def test_endpoint_reading_is_unchanged(self):
        # The endpoint's own partial answers keep today's meaning.
        assert oauth.account_headroom({"five_hour": {"pct": 5.0}}) == 95.0

    def test_complete_reading_decides(self):
        usage = {
            "five_hour": {"pct": 5.0}, "seven_day": {"pct": 60.0}, "source": self.H,
        }
        assert oauth.account_headroom(usage) == 40.0

    def test_fable_switching_needs_the_fable_window(self):
        base = {
            "five_hour": {"pct": 5.0}, "seven_day": {"pct": 10.0}, "source": self.H,
        }
        assert oauth.account_headroom(base, ("Fable",)) is None
        with_fable = {**base, "scoped": [{"name": "Fable", "pct": 70.0}]}
        assert oauth.account_headroom(with_fable, ("fable",)) == 30.0
        # Only Fable's bucket is in the headers: other models and "all" stay
        # unknown rather than passing on the account-wide windows alone.
        assert oauth.account_headroom(with_fable, ("Fable", "Opus")) is None
        assert oauth.account_headroom(with_fable, ("all",)) is None
        # Account-wide switching is unaffected by the per-model window.
        assert oauth.account_headroom(with_fable) == 90.0

    def test_rejection_binds_even_on_a_partial_reading(self):
        from claude_swap import poll_policy
        resets_at = "2030-01-01T00:00:00+00:00"
        usage = {
            "five_hour": {"pct": 5.0},
            "rejected": {"retry_after_s": 60.0, "resets_at": resets_at},
            "source": self.H,
        }
        assert oauth.relevant_windows(usage) == [("rejected", 100.0, resets_at)]
        assert oauth.account_headroom(usage, ("Fable",)) == 0.0
        # Reset scheduling waits for the refusal to end.
        assert poll_policy.limiting_reset_ts(usage) == datetime.fromisoformat(
            resets_at
        ).timestamp()


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


class TestThePinAwareContextAddsTheCaAndNeverReplacesTheStore:
    """`_pin_aware_ssl_context` hands back the DEFAULT context with our CA loaded."""

    def test_the_pin_aware_context_trusts_our_own_proxy_without_an_env_var(self):
        """A python client of the pin must ADD our CA, never REPLACE the store.

        The profile fetch, the usage poll and the policy-limits fetch are
        plain urllib through whatever proxy the session was wired to. When
        that proxy is the pin, it MITMs api.anthropic.com, so the default
        context cannot verify it and every call dies CERTIFICATE_VERIFY_FAILED,
        swallowed to debug and returning None, which is how a failed fetch
        stayed invisible for hours with nothing in any log.

        SSL_CERT_FILE WAS THE WRONG TOOL AND THE MEASUREMENTS SAY SO. It
        REPLACES OpenSSL's file, so it is only safe where the bundle subsumes
        the store it displaces — measured per machine:

            host-a     ambient 124  bundle 126  safe
            host-b      ambient 128  bundle 167  NOT (27 missing)
            host-c  ambient 128  bundle   2  NOT (128 missing)

        so the gate that writes it correctly refuses on two of the three, and
        the calls stay broken there. Adding the CA to a default context keeps
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
        # profile fetch and policy-limits fetch, and each build loads the
        # whole system trust store before parsing ours on top.
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

    def test_a_condemned_lineage_does_not_spend_a_doomed_request_either(self):
        """The consume-gate path's own refusal (`condemned=` returning True,
        via `consume_backup_grant` as `refresh_via`) must reach the store as
        `foreign-lineage`, not the generic `refresh-failed` a fallthrough-then-
        401-then-retry would produce, and must not re-enter the gate a second
        time to learn the same thing."""
        creds = json.dumps({
            "claudeAiOauth": {
                "accessToken": "expired",
                "refreshToken": "r",
                "expiresAt": 1,  # long past
            }
        })
        calls = []

        def refusing_gate(*a):
            calls.append(a)
            return oauth.RefreshOutcome(None, "foreign-lineage")

        with patch("claude_swap.oauth.request_usage_data") as usage:
            out = oauth.try_fetch_usage_for_account(
                "1", "a@example.com", creds, is_active=False,
                refresh_via=refusing_gate,
            )
        assert out.error == "foreign-lineage", out.error
        usage.assert_not_called()          # no doomed GET with the dead token
        assert len(calls) == 1             # no second gate round

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

    So every one of these calls needs the context, not just the usage poll
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

    def test_the_plan_profile_request_carries_it(self, monkeypatch):
        from claude_swap import subscription

        seen = self._capture(monkeypatch)
        subscription.fetch_plan("tok")
        assert seen, "fetch_plan made no request"
        assert seen[0].get("context") is self.SENTINEL, (
            "fetch_plan did not pass the pin-aware context")

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
        assert urlparse(oauth._POLICY_LIMITS_URL).hostname == mitm, (
            "the scan is broken: if the policy-limits endpoint is not on the MITM'd "
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


@pytest.mark.no_policy_limits_fake
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
    freezes it for the life of the process: every later profile fetch, usage
    poll and policy-limits fetch goes out on a context that does not trust the
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


class TestDaemonSslContext:
    """In the pin daemon the egress is a re-signing hop, so the pin's own
    `_verifying_context` decides what is trusted; everywhere else the
    roots-plus-ca.pem context is unchanged."""

    DAEMON = "cswap_pin.proxy"

    @pytest.fixture(autouse=True)
    def _fresh_slot(self, monkeypatch):
        from claude_swap import pin

        monkeypatch.setattr(oauth, "_PIN_CTX_SLOT", None)
        monkeypatch.setattr(pin, "ca_path_for_trust", lambda: None)

    def _as_main(self, monkeypatch, spec_name, impl):
        import sys
        import types

        from claude_swap import pin

        spec = None if spec_name is None else types.SimpleNamespace(name=spec_name)
        monkeypatch.setitem(sys.modules, "__main__",
                            types.SimpleNamespace(__spec__=spec))
        monkeypatch.setattr(pin, "_live_impl", lambda: impl)

    @staticmethod
    def _impl(verifying):
        import types

        return types.SimpleNamespace(_DAEMON_MODULE_NAMES=("cswap_pin.proxy",),
                                     _verifying_context=verifying)

    def test_a_non_daemon_main_keeps_the_cached_pin_context(self, monkeypatch):
        from claude_swap import pin

        sentinel = ssl.create_default_context()
        self._as_main(monkeypatch, "claude_swap.__main__",
                      self._impl(lambda: sentinel))

        assert pin.daemon_ssl_context() is None
        first = oauth._pin_aware_ssl_context()
        assert first is not sentinel
        assert oauth._pin_aware_ssl_context() is first

    def test_the_daemon_gets_the_pins_verifying_context(self, monkeypatch):
        from claude_swap import pin

        sentinel = ssl.create_default_context()
        self._as_main(monkeypatch, self.DAEMON, self._impl(lambda: sentinel))

        assert pin.daemon_ssl_context() is sentinel
        assert oauth._pin_aware_ssl_context() is sentinel
        assert oauth._PIN_CTX_SLOT is None, (
            "the daemon context went into the slot keyed on the pin CA alone")

    @pytest.mark.parametrize("case", [
        "no-spec", "no-impl", "no-names", "no-verifying", "raises"])
    def test_anything_missing_or_raising_falls_back(self, monkeypatch, case):
        import types

        from claude_swap import pin

        def boom():
            raise RuntimeError("pin refactored")

        impl = {
            "no-impl": None,
            "no-names": types.SimpleNamespace(_verifying_context=boom),
            "no-verifying": types.SimpleNamespace(
                _DAEMON_MODULE_NAMES=(self.DAEMON,)),
        }.get(case, self._impl(boom))
        self._as_main(monkeypatch, None if case == "no-spec" else self.DAEMON, impl)

        assert pin.daemon_ssl_context() is None
        ctx = oauth._pin_aware_ssl_context()
        assert isinstance(ctx, ssl.SSLContext)
        assert oauth._pin_aware_ssl_context() is ctx

    def test_a_pin_that_asks_the_host_back_does_not_recurse(self, monkeypatch):
        """cswap-pin 0.1.309 and 0.1.310 answer `_verifying_context` with the
        host's own `_pin_aware_ssl_context`, which would be this same call."""
        asked = []

        def asks_the_host():
            asked.append(1)
            return oauth._pin_aware_ssl_context()

        self._as_main(monkeypatch, self.DAEMON, self._impl(asks_the_host))

        ctx = oauth._pin_aware_ssl_context()
        assert asked == [1], f"the impl was entered {len(asked)} times"
        assert isinstance(ctx, ssl.SSLContext)

    def test_the_re_entry_guard_is_per_thread(self, monkeypatch):
        """`_verifying_context` builds a context in milliseconds, so a second
        `_run_usage_fetches` pool thread arriving meanwhile must not see the
        first one's guard and fall back to a context that fails `tls-cert`."""
        import threading

        sentinel = ssl.create_default_context()
        meet = threading.Barrier(2, timeout=5)

        def verifying():
            meet.wait()
            return sentinel

        self._as_main(monkeypatch, self.DAEMON, self._impl(verifying))
        got = []
        threads = [threading.Thread(
            target=lambda: got.append(oauth._pin_aware_ssl_context()))
            for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)

        assert [c is sentinel for c in got] == [True, True], (
            "a concurrent caller was refused the daemon context because "
            "another thread was inside `_verifying_context`")


class TestLoginExpiresAtIso:
    def test_refresh_token_expiry_is_reported_as_iso_utc(self):
        creds = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-x", "refreshTokenExpiresAt": 1791421596865,
        }})
        assert oauth.login_expires_at_iso(creds) == "2026-10-08T01:06:36Z"

    @pytest.mark.parametrize("creds", [
        "",
        "not json",
        json.dumps({"claudeAiOauth": {"accessToken": "sk-x"}}),
        json.dumps({"claudeAiOauth": {"refreshTokenExpiresAt": "soon"}}),
        json.dumps({"claudeAiOauth": {"refreshTokenExpiresAt": True}}),
        json.dumps({"claudeAiOauth": {"refreshTokenExpiresAt": 0}}),
        json.dumps({"claudeAiOauth": {"refreshTokenExpiresAt": float("nan")}}),
        json.dumps({"claudeAiOauth": {"refreshTokenExpiresAt": float("inf")}}),
        json.dumps({"other": {}}),
    ])
    def test_anything_but_a_positive_epoch_is_unknown(self, creds):
        assert oauth.login_expires_at_iso(creds) is None
