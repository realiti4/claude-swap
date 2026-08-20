"""Tests for the unified-header usage source."""

from __future__ import annotations

import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from claude_swap import oauth
from claude_swap.unified_headers import parse_unified_headers, probe_usage

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


def test_a_complete_header_set_ranks_normally():
    """The safe row of the table: both gating windows read, so headroom is a
    confident number and the account is rankable."""
    assert oauth.account_headroom(parse_unified_headers(HEALTHY)) == pytest.approx(89.0)
    assert oauth.account_headroom(parse_unified_headers(EXHAUSTED)) == 0.0


def test_partial_windows_are_shown_but_never_ranked():
    """A window this source never saw is still worth SHOWING (a number beats
    "unavailable"), but the surviving window's headroom is not the account's:
    a dropped weekly header on an exhausted account would otherwise read as
    100% headroom and make it the preferred rotation target."""
    only5h = {k: v for k, v in HEALTHY.items() if "-7d-" not in k}
    out = parse_unified_headers(only5h)
    assert out["five_hour"]["pct"] == 11.0
    assert "seven_day" not in out
    assert out["partial"] is True
    assert oauth.account_headroom(out) is None


def test_a_dropped_weekly_header_cannot_make_an_exhausted_account_look_healthy():
    """The live case: true state is 5h 0%, 7d 100%. Losing the weekly
    utilization header must not turn that into full headroom."""
    dropped = {
        k: v
        for k, v in EXHAUSTED.items()
        if k != "anthropic-ratelimit-unified-7d-utilization"
    }
    out = parse_unified_headers(dropped)
    assert out["five_hour"]["pct"] == 0.0
    assert oauth.account_headroom(out) is None


@pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "-inf", "Infinity", "-0.5"])
def test_non_finite_and_negative_utilization_is_rejected(raw):
    """``float()`` accepts all of these, and each one stored as a percentage
    reads downstream as a measurement: nan loses every comparison, an infinity
    poisons the reset math, and a negative fraction invents headroom."""
    bad = dict(HEALTHY, **{"anthropic-ratelimit-unified-7d-utilization": raw})
    out = parse_unified_headers(bad)
    assert "seven_day" not in out
    assert out["partial"] is True
    assert oauth.account_headroom(out) is None


def test_a_five_hour_infinity_is_never_stored_as_a_pct():
    bad = dict(HEALTHY, **{"anthropic-ratelimit-unified-5h-utilization": "inf"})
    out = parse_unified_headers(bad)
    assert "five_hour" not in out
    assert out["seven_day"]["pct"] == 10.0
    assert oauth.account_headroom(out) is None


def test_malformed_values_are_skipped_not_fatal():
    bad = dict(HEALTHY, **{"anthropic-ratelimit-unified-5h-utilization": "banana"})
    out = parse_unified_headers(bad)
    assert "five_hour" not in out
    assert out["seven_day"]["pct"] == 10.0
    assert oauth.account_headroom(out) is None


class TestProbeTransport:
    """``probe_usage``'s own transport, driven for real rather than mocked
    away. The oauth-side tests patch ``probe_usage`` out entirely, so nothing
    there can see which HTTP statuses it trusts. Only a 429 body is documented
    to carry usable utilization; parsing unified-looking headers off a 401,
    403, 404 or 5xx would report a dead token or a failing server as a healthy
    account.
    """

    @staticmethod
    def _http_error(code: int, headers: dict | None) -> urllib.error.HTTPError:
        return urllib.error.HTTPError(
            "https://api.anthropic.com/v1/messages", code, "error",
            hdrs=headers, fp=None,
        )

    @staticmethod
    def _ok_response(headers: dict) -> MagicMock:
        resp = MagicMock()
        resp.headers = headers
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def test_a_200_response_is_read_from_its_headers(self):
        with patch(
            "claude_swap.unified_headers.urllib.request.urlopen",
            return_value=self._ok_response(HEALTHY),
        ):
            assert probe_usage("sk-token")["five_hour"]["pct"] == 11.0

    def test_a_429_is_read_from_its_headers(self):
        with patch(
            "claude_swap.unified_headers.urllib.request.urlopen",
            side_effect=self._http_error(429, EXHAUSTED),
        ):
            assert probe_usage("sk-token")["seven_day"]["pct"] == 100.0

    @pytest.mark.parametrize("code", [400, 401, 403, 404, 500, 502, 503])
    def test_no_other_status_reports_usage(self, code):
        with patch(
            "claude_swap.unified_headers.urllib.request.urlopen",
            side_effect=self._http_error(code, HEALTHY),
        ):
            assert probe_usage("sk-token") is None

    def test_a_429_without_headers_reports_nothing(self):
        with patch(
            "claude_swap.unified_headers.urllib.request.urlopen",
            side_effect=self._http_error(429, None),
        ):
            assert probe_usage("sk-token") is None

    def test_a_transport_failure_reports_nothing(self):
        with patch(
            "claude_swap.unified_headers.urllib.request.urlopen",
            side_effect=urllib.error.URLError(TimeoutError()),
        ):
            assert probe_usage("sk-token") is None
