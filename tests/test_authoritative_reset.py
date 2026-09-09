"""Tests for the authoritative_reset override bridge."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from claude_swap import authoritative_reset as ar

NOW = datetime(2026, 8, 11, 18, 0, tzinfo=timezone.utc)


def _iso(minutes: int) -> str:
    return (NOW + timedelta(minutes=minutes)).isoformat()


class TestReadAuthoritativeResets:
    def test_none_path(self):
        assert ar.read_authoritative_resets(None) == {}

    def test_missing_file(self, tmp_path):
        assert ar.read_authoritative_resets(str(tmp_path / "nope.json")) == {}

    def test_malformed_json(self, tmp_path):
        p = tmp_path / "b.json"
        p.write_text("{not json", encoding="utf-8")
        assert ar.read_authoritative_resets(str(p)) == {}

    def test_non_object_top_level(self, tmp_path):
        p = tmp_path / "arr.json"
        p.write_text("[1, 2]", encoding="utf-8")
        assert ar.read_authoritative_resets(str(p)) == {}

    def test_valid_and_filters_bad_entries(self, tmp_path):
        p = tmp_path / "ok.json"
        p.write_text(json.dumps({
            "a@x.com": {"five_hour": _iso(69)},
            "bad": "not-a-dict",  # dropped
        }), encoding="utf-8")
        out = ar.read_authoritative_resets(str(p))
        assert out == {"a@x.com": {"five_hour": _iso(69)}}


class TestClampAccountResets:
    def test_pulls_earlier(self):
        usage = {"five_hour": {"pct": 100.0, "resets_at": _iso(210)}}  # 21:30
        out = ar.clamp_account_resets(usage, {"five_hour": _iso(69)}, NOW)  # 19:09
        assert out["five_hour"]["resets_at"] == _iso(69)
        # stale render fields dropped so they recompute from the new reset
        assert "countdown" not in out["five_hour"]
        assert usage["five_hour"]["resets_at"] == _iso(210)  # input untouched

    def test_never_moves_later(self):
        usage = {"five_hour": {"pct": 100.0, "resets_at": _iso(30)}}
        out = ar.clamp_account_resets(usage, {"five_hour": _iso(200)}, NOW)
        assert out is usage  # no change → same object

    def test_fills_missing_endpoint_reset(self):
        usage = {"seven_day": {"pct": 95.0}}
        out = ar.clamp_account_resets(usage, {"seven_day": _iso(120)}, NOW)
        assert out["seven_day"]["resets_at"] == _iso(120)

    def test_ignores_elapsed_override(self):
        usage = {"five_hour": {"pct": 100.0, "resets_at": _iso(210)}}
        out = ar.clamp_account_resets(usage, {"five_hour": _iso(-10)}, NOW)
        assert out is usage

    def test_no_overrides_is_noop(self):
        usage = {"five_hour": {"pct": 100.0, "resets_at": _iso(210)}}
        assert ar.clamp_account_resets(usage, None, NOW) is usage
        assert ar.clamp_account_resets(usage, {}, NOW) is usage

    def test_unparsable_override_ignored(self):
        usage = {"five_hour": {"pct": 100.0, "resets_at": _iso(210)}}
        out = ar.clamp_account_resets(usage, {"five_hour": "garbage"}, NOW)
        assert out is usage

    def test_none_usage(self):
        assert ar.clamp_account_resets(None, {"five_hour": _iso(10)}, NOW) is None
