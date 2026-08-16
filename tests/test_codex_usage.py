"""Mapping the ChatGPT usage API onto cswap's usage dict."""

from __future__ import annotations

import json
import urllib.error
from datetime import datetime
from io import BytesIO

from claude_swap.codex import usage as cusage


class _Resp:
    def __init__(self, payload: object):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _raiser(exc):
    def boom(*a, **k):
        raise exc

    return boom


RAW = {
    "primary": {"used_percent": 42, "window_minutes": 300, "resets_at": 1_800_000_000},
    "secondary": {"used_percent": 7, "window_minutes": 10080, "resets_at": 1_800_600_000},
    "credits": {"has_credits": True, "unlimited": False, "balance": "12.50"},
    "plan_type": "pro",
}


def test_primary_window_maps_to_five_hour():
    """cswap's renderers, pace and autoswitch all read five_hour/seven_day.
    Mapping here is what makes every Claude-side consumer work unchanged."""
    out = cusage.build_usage_result(RAW)
    assert out["five_hour"]["pct"] == 42
    assert out["five_hour"]["resets_at"].startswith("2027-01-15")


def test_secondary_window_maps_to_seven_day():
    assert cusage.build_usage_result(RAW)["seven_day"]["pct"] == 7


def test_windows_carry_countdown_and_clock_like_the_claude_side():
    out = cusage.build_usage_result(RAW)
    assert "countdown" in out["five_hour"] and "clock" in out["five_hour"]


def test_epoch_resets_are_converted_to_iso():
    """The API reports epoch seconds; pace.compute_pace parses ISO strings. A
    raw epoch would silently disable pace for every Codex row."""
    out = cusage.build_usage_result(RAW)
    datetime.fromisoformat(out["five_hour"]["resets_at"])  # must not raise


def test_the_mapped_shape_is_readable_by_the_existing_pace_calculation():
    """The real contract this mapping exists to satisfy."""
    from claude_swap.pace import compute_pace

    window = cusage.build_usage_result(RAW)["seven_day"]
    # fetched_at three days into the weekly cycle
    fetched_at = 1_800_600_000 - 4 * 86400
    assert compute_pace(window, fetched_at=fetched_at) is not None


def test_a_missing_secondary_window_is_omitted_not_zeroed():
    """A plan with no weekly window must not render as 0% used."""
    assert "seven_day" not in cusage.build_usage_result(dict(RAW, secondary=None))


def test_a_window_without_a_percentage_is_dropped():
    raw = dict(RAW, primary={"window_minutes": 300, "resets_at": 1_800_000_000})
    assert "five_hour" not in cusage.build_usage_result(raw)


def test_a_window_without_a_reset_still_reports_its_percentage():
    raw = dict(RAW, primary={"used_percent": 12})
    out = cusage.build_usage_result(raw)
    assert out["five_hour"] == {"pct": 12}


def test_plan_type_is_normalized_to_v4_semantics():
    assert cusage.build_usage_result(dict(RAW, plan_type="team"))["plan"] == "business"


def test_credits_become_a_spend_entry():
    assert cusage.build_usage_result(RAW)["spend"]["balance"] == "12.50"


def test_unlimited_credits_are_marked_not_priced():
    raw = dict(RAW, credits={"has_credits": True, "unlimited": True, "balance": None})
    assert cusage.build_usage_result(raw)["spend"]["unlimited"] is True


def test_an_account_without_credits_has_no_spend_entry():
    raw = dict(RAW, credits={"has_credits": False})
    assert "spend" not in cusage.build_usage_result(raw)


def test_an_empty_response_yields_none():
    assert cusage.build_usage_result({}) is None
    assert cusage.build_usage_result(None) is None
    assert cusage.build_usage_result([1, 2]) is None


def test_fetch_sends_both_required_headers(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["headers"] = dict(req.headers)
        seen["url"] = req.full_url
        return _Resp(RAW)

    monkeypatch.setattr(cusage.urllib.request, "urlopen", fake_urlopen)
    cusage.fetch_usage("at-1", "acct-1")

    assert seen["url"] == cusage.USAGE_URL
    assert seen["headers"]["Authorization"] == "Bearer at-1"
    assert seen["headers"]["Chatgpt-account-id"] == "acct-1"


def test_fetch_returns_the_mapped_usage(monkeypatch):
    monkeypatch.setattr(cusage.urllib.request, "urlopen", lambda *a, **k: _Resp(RAW))
    result = cusage.fetch_usage("at-1", "acct-1")
    assert result.sentinel is None
    assert result.usage["five_hour"]["pct"] == 42


def test_fetch_reports_the_http_status_as_a_sentinel(monkeypatch):
    """codex-auth renders the status in the usage column; matching that keeps a
    broken account legible instead of blank."""
    monkeypatch.setattr(
        cusage.urllib.request,
        "urlopen",
        _raiser(urllib.error.HTTPError("u", 401, "no", {}, BytesIO(b"{}"))),
    )
    result = cusage.fetch_usage("at-1", "acct-1")
    assert result.sentinel == "http 401"
    assert result.usage is None


def test_fetch_without_an_account_id_is_missing_auth():
    assert cusage.fetch_usage("at-1", "").sentinel == "MissingAuth"


def test_fetch_without_an_access_token_is_missing_auth():
    assert cusage.fetch_usage("", "acct-1").sentinel == "MissingAuth"


def test_a_network_failure_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(
        cusage.urllib.request, "urlopen", _raiser(urllib.error.URLError("down"))
    )
    assert cusage.fetch_usage("at", "acct").sentinel == "network"


def test_an_unparseable_response_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(cusage.urllib.request, "urlopen", lambda *a, **k: _Resp("junk"))
    assert cusage.fetch_usage("at", "acct").sentinel == "bad-response"


def test_workspace_names_are_keyed_by_account_id(monkeypatch):
    payload = {"items": [{"id": "team-1", "name": "Workspace Alpha"}]}
    monkeypatch.setattr(cusage.urllib.request, "urlopen", lambda *a, **k: _Resp(payload))
    assert cusage.fetch_workspace_names("at", "acct") == {"team-1": "Workspace Alpha"}


def test_a_null_workspace_name_is_omitted_not_stored_as_empty(monkeypatch):
    """Storing "" would look like a real answer and stop a later successful
    fetch from filling the name in."""
    payload = {"items": [{"id": "team-1", "name": None}, {"id": "team-2", "name": ""}]}
    monkeypatch.setattr(cusage.urllib.request, "urlopen", lambda *a, **k: _Resp(payload))
    assert cusage.fetch_workspace_names("at", "acct") == {}


def test_a_workspace_fetch_failure_is_never_fatal(monkeypatch):
    monkeypatch.setattr(
        cusage.urllib.request, "urlopen", _raiser(urllib.error.URLError("down"))
    )
    assert cusage.fetch_workspace_names("at", "acct") == {}
