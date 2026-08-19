"""Tests for the inference-only (setup-token) usage probe."""

from __future__ import annotations

from datetime import datetime, timezone

from claude_swap import oauth

# A real response's shape, trimmed to the headers the probe reads.
HEADERS = {
    "anthropic-ratelimit-unified-5h-utilization": "0.04",
    "anthropic-ratelimit-unified-5h-reset": "1787184600",
    "anthropic-ratelimit-unified-7d-utilization": "0.06",
    "anthropic-ratelimit-unified-7d-reset": "1787655600",
}


class TestIsInferenceOnly:
    def test_setup_token_scopes(self):
        assert oauth.is_inference_only({"scopes": ["user:inference"]})

    def test_full_login_scopes(self):
        assert not oauth.is_inference_only(
            {"scopes": ["user:inference", "user:profile"]}
        )

    def test_missing_or_empty_scopes(self):
        assert not oauth.is_inference_only({})
        assert not oauth.is_inference_only({"scopes": []})
        assert not oauth.is_inference_only(None)


class TestUsageFromRatelimitHeaders:
    def test_fraction_is_scaled_to_percent(self):
        # 0.04 in the header must read as 4%, not 0.04% — build_usage_result
        # passes the number straight through as ``pct``.
        out = oauth.usage_from_ratelimit_headers(HEADERS)
        assert out["five_hour"]["utilization"] == 4.0
        assert out["seven_day"]["utilization"] == 6.0

    def test_reset_epoch_becomes_parseable_iso(self):
        out = oauth.usage_from_ratelimit_headers(HEADERS)
        iso = out["five_hour"]["resets_at"]
        assert datetime.fromisoformat(iso) == datetime.fromtimestamp(
            1787184600, tz=timezone.utc
        )
        # The whole point of the ISO round trip: format_reset must accept it.
        oauth.format_reset(iso)

    def test_normalizes_through_build_usage_result(self):
        result = oauth.build_usage_result(oauth.usage_from_ratelimit_headers(HEADERS))
        assert result["five_hour"]["pct"] == 4.0
        # Headroom is what auto-switch ranks on; None here would keep the
        # account invisible, which is the bug this whole path fixes.
        assert oauth.account_headroom(result) == 94.0

    def test_absent_headers_yield_no_windows(self):
        assert oauth.usage_from_ratelimit_headers({}) == {}

    def test_garbage_utilization_is_skipped(self):
        out = oauth.usage_from_ratelimit_headers(
            {"anthropic-ratelimit-unified-5h-utilization": "not-a-number"}
        )
        assert out == {}

    def test_window_without_reset_still_reports_pct(self):
        out = oauth.usage_from_ratelimit_headers(
            {"anthropic-ratelimit-unified-5h-utilization": "0.5"}
        )
        assert out["five_hour"] == {"utilization": 50.0}


class TestProbeThrottle:
    def test_second_call_inside_interval_is_served_from_cache(self, monkeypatch):
        calls = []

        def fake_probe(token):
            calls.append(token)
            return {"five_hour": {"utilization": 1.0}}

        monkeypatch.setattr(oauth, "request_usage_via_probe", fake_probe)
        monkeypatch.setattr(oauth, "_probe_cache", {})

        oauth.probe_usage_throttled("tok", now=0.0)
        oauth.probe_usage_throttled("tok", now=oauth.PROBE_MIN_INTERVAL_S - 1)
        assert len(calls) == 1

    def test_call_after_interval_refetches(self, monkeypatch):
        calls = []

        def fake_probe(token):
            calls.append(token)
            return {"five_hour": {"utilization": 1.0}}

        monkeypatch.setattr(oauth, "request_usage_via_probe", fake_probe)
        monkeypatch.setattr(oauth, "_probe_cache", {})

        oauth.probe_usage_throttled("tok", now=0.0)
        oauth.probe_usage_throttled("tok", now=oauth.PROBE_MIN_INTERVAL_S + 1)
        assert len(calls) == 2

    def test_distinct_tokens_do_not_share_a_slot(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            oauth,
            "request_usage_via_probe",
            lambda t: (calls.append(t), {"five_hour": {"utilization": 1.0}})[1],
        )
        monkeypatch.setattr(oauth, "_probe_cache", {})

        oauth.probe_usage_throttled("tok-a", now=0.0)
        oauth.probe_usage_throttled("tok-b", now=0.0)
        assert len(calls) == 2
