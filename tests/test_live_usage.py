"""Tests for reading live usage from Anthropic's rate-limit response headers."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from claude_swap.live_usage import parse_ratelimit_headers

RESET_5H = 1_792_788_000
RESET_7D = 1_793_000_000


def _headers(**overrides: str) -> dict[str, str]:
    base = {
        "anthropic-ratelimit-unified-status": "allowed",
        "anthropic-ratelimit-unified-5h-utilization": "0.05",
        "anthropic-ratelimit-unified-5h-reset": str(RESET_5H),
        "anthropic-ratelimit-unified-7d-utilization": "0.42",
        "anthropic-ratelimit-unified-7d-reset": str(RESET_7D),
        "anthropic-ratelimit-unified-representative-claim": "five_hour",
    }
    base.update(overrides)
    return base


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


class TestParse:
    def test_fractions_become_the_percentages_the_store_holds(self):
        usage = parse_ratelimit_headers(_headers())
        assert usage is not None
        assert usage["five_hour"]["pct"] == pytest.approx(5.0)
        assert usage["seven_day"]["pct"] == pytest.approx(42.0)

    def test_reset_epochs_become_the_iso_strings_the_usage_api_uses(self):
        usage = parse_ratelimit_headers(_headers())
        assert usage["five_hour"]["resets_at"] == _iso(RESET_5H)
        assert usage["seven_day"]["resets_at"] == _iso(RESET_7D)
        # Same derived display fields build_usage_result adds.
        assert {"countdown", "clock"} <= usage["five_hour"].keys()

    def test_header_names_match_case_insensitively(self):
        upper = {k.upper(): v for k, v in _headers().items()}
        assert parse_ratelimit_headers(upper)["seven_day"]["pct"] == pytest.approx(42.0)

    def test_no_ratelimit_headers_is_no_reading(self):
        # A reply that is not a metered /v1/messages call (count_tokens, an
        # error page from a middlebox) must not overwrite a real reading.
        assert parse_ratelimit_headers({"content-type": "application/json"}) is None

    def test_a_window_with_garbage_utilization_is_left_out(self):
        usage = parse_ratelimit_headers(
            _headers(**{"anthropic-ratelimit-unified-5h-utilization": "n/a"})
        )
        assert "five_hour" not in usage
        assert usage["seven_day"]["pct"] == pytest.approx(42.0)

    @pytest.mark.parametrize("bad", ["nan", "inf", "-0.1"])
    def test_non_finite_or_negative_utilization_is_left_out(self, bad):
        usage = parse_ratelimit_headers(
            _headers(**{"anthropic-ratelimit-unified-7d-utilization": bad})
        )
        assert "seven_day" not in usage

    def test_a_window_without_a_usable_reset_keeps_its_pct(self):
        usage = parse_ratelimit_headers(
            _headers(**{"anthropic-ratelimit-unified-5h-reset": "soon"})
        )
        assert usage["five_hour"] == {"pct": pytest.approx(5.0)}

    def test_garbage_in_every_window_is_no_reading(self):
        usage = parse_ratelimit_headers(
            {
                "anthropic-ratelimit-unified-status": "allowed",
                "anthropic-ratelimit-unified-5h-utilization": "x",
                "anthropic-ratelimit-unified-7d-utilization": "y",
            }
        )
        assert usage is None


class TestRejected:
    def test_rejected_marks_the_claimed_window_at_its_limit(self):
        # A 429 can carry a utilization a hair under 1.0 (rounding, or the
        # request that tipped it over is not counted yet). The status is the
        # authority: the account is at its limit on that window now.
        usage = parse_ratelimit_headers(
            _headers(
                **{
                    "anthropic-ratelimit-unified-status": "rejected",
                    "anthropic-ratelimit-unified-5h-utilization": "0.97",
                }
            )
        )
        assert usage["five_hour"]["pct"] == 100.0
        assert usage["seven_day"]["pct"] == pytest.approx(42.0)

    def test_rejected_on_the_weekly_claim_marks_the_weekly_window(self):
        usage = parse_ratelimit_headers(
            _headers(
                **{
                    "anthropic-ratelimit-unified-status": "rejected",
                    "anthropic-ratelimit-unified-representative-claim": "seven_day",
                }
            )
        )
        assert usage["seven_day"]["pct"] == 100.0
        assert usage["five_hour"]["pct"] == pytest.approx(5.0)

    def test_rejected_keeps_a_utilization_already_over_the_limit(self):
        usage = parse_ratelimit_headers(
            _headers(
                **{
                    "anthropic-ratelimit-unified-status": "rejected",
                    "anthropic-ratelimit-unified-5h-utilization": "1.2",
                }
            )
        )
        assert usage["five_hour"]["pct"] == pytest.approx(120.0)

    def test_rejected_with_an_unknown_claim_marks_nothing(self):
        # A claim this parser does not know (a per-model or overage window)
        # says nothing about 5h/7d; leave them as measured.
        usage = parse_ratelimit_headers(
            _headers(
                **{
                    "anthropic-ratelimit-unified-status": "rejected",
                    "anthropic-ratelimit-unified-representative-claim": "overage",
                }
            )
        )
        assert usage["five_hour"]["pct"] == pytest.approx(5.0)
        assert usage["seven_day"]["pct"] == pytest.approx(42.0)

    def test_rejected_on_a_window_the_reply_did_not_measure_adds_it(self):
        headers = _headers(
            **{
                "anthropic-ratelimit-unified-status": "rejected",
                "anthropic-ratelimit-unified-representative-claim": "seven_day",
            }
        )
        del headers["anthropic-ratelimit-unified-7d-utilization"]
        del headers["anthropic-ratelimit-unified-7d-reset"]
        assert parse_ratelimit_headers(headers)["seven_day"] == {"pct": 100.0}
