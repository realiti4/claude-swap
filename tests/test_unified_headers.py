"""Tests for the unified-header usage source."""

from __future__ import annotations

from claude_swap.unified_headers import parse_unified_headers

HEALTHY = {
    "anthropic-ratelimit-unified-status": "allowed",
    "anthropic-ratelimit-unified-5h-status": "allowed",
    "anthropic-ratelimit-unified-5h-reset": "1786140000",
    "anthropic-ratelimit-unified-5h-utilization": "0.11",
    "anthropic-ratelimit-unified-7d-status": "allowed",
    "anthropic-ratelimit-unified-7d-reset": "1786640400",
    "anthropic-ratelimit-unified-7d-utilization": "0.1",
    "anthropic-ratelimit-unified-representative-claim": "five_hour",
}

EXHAUSTED = {
    "anthropic-ratelimit-unified-status": "rejected",
    "anthropic-ratelimit-unified-5h-status": "allowed",
    "anthropic-ratelimit-unified-5h-reset": "1786137000",
    "anthropic-ratelimit-unified-5h-utilization": "0.0",
    "anthropic-ratelimit-unified-7d-status": "rejected",
    "anthropic-ratelimit-unified-7d-reset": "1786204800",
    "anthropic-ratelimit-unified-7d-utilization": "1.0",
    "anthropic-ratelimit-unified-overage-status": "rejected",
    "anthropic-ratelimit-unified-overage-utilization": "1.06",
    "anthropic-ratelimit-unified-overage-disabled-reason": "org_spend_cap_reached",
    "anthropic-ratelimit-unified-representative-claim": "seven_day",
}


def test_healthy_scales_fractions_to_percentages():
    out = parse_unified_headers(HEALTHY)
    assert out["five_hour"]["pct"] == 11.0
    assert out["seven_day"]["pct"] == 10.0


def test_reset_epoch_becomes_display_fields():
    out = parse_unified_headers(HEALTHY)
    assert out["five_hour"]["resets_at"] == "2026-08-07T22:00:00Z"
    assert out["five_hour"]["countdown"]
    assert out["five_hour"]["clock"]


def test_exhausted_account_reports_100_not_an_error():
    out = parse_unified_headers(EXHAUSTED)
    assert out["seven_day"]["pct"] == 100.0
    assert out["five_hour"]["pct"] == 0.0


def test_absent_headers_return_none():
    assert parse_unified_headers({}) is None
    assert parse_unified_headers({"content-type": "application/json"}) is None


def test_header_lookup_is_case_insensitive():
    upper = {k.upper(): v for k, v in HEALTHY.items()}
    assert parse_unified_headers(upper)["five_hour"]["pct"] == 11.0


def test_partial_windows_are_allowed():
    only5h = {k: v for k, v in HEALTHY.items() if "-7d-" not in k}
    out = parse_unified_headers(only5h)
    assert out["five_hour"]["pct"] == 11.0
    assert "seven_day" not in out


def test_malformed_values_are_skipped_not_fatal():
    bad = dict(HEALTHY, **{"anthropic-ratelimit-unified-5h-utilization": "banana"})
    out = parse_unified_headers(bad)
    assert "five_hour" not in out
    assert out["seven_day"]["pct"] == 10.0
