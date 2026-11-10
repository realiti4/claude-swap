"""Tests for codex_usage.fetch_usage (urlopen mocked; no network)."""

from __future__ import annotations

import io
import json
import urllib.error
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from claude_swap.codex_usage import Window, fetch_usage

URLOPEN = "claude_swap.codex_usage.urllib.request.urlopen"


def _window(used, seconds, reset_after, reset_at):
    return {"used_percent": used, "limit_window_seconds": seconds,
            "reset_after_seconds": reset_after, "reset_at": reset_at}


# The shape the live endpoint returns (values fake), extra keys included.
PAYLOAD = {
    "user_id": "user-x", "account_id": "acct-x", "email": "x@example.com",
    "plan_type": "plus",
    "rate_limit": {
        "allowed": True, "limit_reached": False,
        "primary_window": _window(12, 18000, 3600, 1794300000),
        "secondary_window": _window(40, 604800, 86400, 1794600000),
    },
    "code_review_rate_limit": None, "additional_rate_limits": None,
    "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
}


def _resp(body: bytes):
    m = MagicMock()
    m.__enter__.return_value.read.return_value = body
    return m


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def test_parses_both_windows_and_sends_auth_headers():
    with patch(URLOPEN, return_value=_resp(json.dumps(PAYLOAD).encode())) as urlopen:
        windows = fetch_usage("tok", "acct-1", timeout=3)
    assert windows == [
        Window("5h", 12.0, _iso(1794300000)),
        Window("weekly", 40.0, _iso(1794600000)),
    ]
    assert datetime.fromisoformat(windows[0].resets_at).tzinfo is not None
    req = urlopen.call_args.args[0]
    assert req.full_url == "https://chatgpt.com/backend-api/wham/usage"
    assert req.get_method() == "GET"
    assert req.get_header("Authorization") == "Bearer tok"
    assert req.get_header("Chatgpt-account-id") == "acct-1"  # urllib capitalizes
    assert urlopen.call_args.kwargs["timeout"] == 3


def test_weekly_only_plan_null_secondary_and_reset_after_fallback():
    # Observed live on a Pro login: the weekly limit sits in primary_window.
    body = {"plan_type": "pro", "rate_limit": {
        "primary_window": _window(100, 604800, 600, 0),
        "secondary_window": None,
    }}
    with patch(URLOPEN, return_value=_resp(json.dumps(body).encode())) as urlopen:
        before = datetime.now(timezone.utc)
        (w,) = fetch_usage("tok", None)
    assert (w.label, w.used_percent) == ("weekly", 100.0)
    assert 590 <= (datetime.fromisoformat(w.resets_at) - before).total_seconds() <= 610
    assert urlopen.call_args.args[0].get_header("Chatgpt-account-id") is None


@pytest.mark.parametrize("seconds,label", [(7200, "2h"), (86400, "1d"), (259200, "3d"), (5400, "2h")])
def test_other_window_lengths(seconds, label):
    body = {"rate_limit": {"primary_window": _window(5, seconds, 0, 1794300000)}}
    with patch(URLOPEN, return_value=_resp(json.dumps(body).encode())):
        assert fetch_usage("tok", "a")[0].label == label


@pytest.mark.parametrize("effect", [
    urllib.error.HTTPError("u", 401, "Unauthorized", {}, io.BytesIO(b"")),
    urllib.error.URLError("down"),
    TimeoutError(),
])
def test_network_failures_return_empty(effect):
    with patch(URLOPEN, side_effect=effect):
        assert fetch_usage("tok", "a") == []


@pytest.mark.parametrize("body", [
    b"<html>blocked</html>",
    b"[]",
    json.dumps({"plan_type": "plus"}).encode(),
    json.dumps({"rate_limit": None}).encode(),
])
def test_bad_or_missing_rate_limit_returns_empty(body):
    with patch(URLOPEN, return_value=_resp(body)):
        assert fetch_usage("tok", "a") == []
