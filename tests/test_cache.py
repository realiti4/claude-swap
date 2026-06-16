"""Tests for the shared cache helper."""

from __future__ import annotations

import json
import time

from claude_swap.cache import (
    MISSING,
    PROBE_VERDICT_TTL_S,
    probe_ok,
    probe_recent,
    read_cache,
    write_cache,
)


class TestReadCache:
    def test_returns_data_within_ttl(self, tmp_path):
        cache_file = tmp_path / "test.json"
        cache_file.write_text(json.dumps({
            "timestamp": time.time(),
            "data": {"key": "value"},
        }))

        result = read_cache(cache_file, ttl=60)
        assert result == {"key": "value"}

    def test_returns_missing_when_expired(self, tmp_path):
        cache_file = tmp_path / "test.json"
        cache_file.write_text(json.dumps({
            "timestamp": time.time() - 100,
            "data": {"key": "value"},
        }))

        result = read_cache(cache_file, ttl=60)
        assert result is MISSING

    def test_returns_missing_for_missing_file(self, tmp_path):
        result = read_cache(tmp_path / "nonexistent.json", ttl=60)
        assert result is MISSING

    def test_returns_missing_for_corrupt_json(self, tmp_path):
        cache_file = tmp_path / "test.json"
        cache_file.write_text("not valid json{{{")

        result = read_cache(cache_file, ttl=60)
        assert result is MISSING

    def test_cached_none_is_distinguishable_from_miss(self, tmp_path):
        cache_file = tmp_path / "test.json"
        cache_file.write_text(json.dumps({
            "timestamp": time.time(),
            "data": None,
        }))

        result = read_cache(cache_file, ttl=60)
        assert result is None
        assert result is not MISSING


class TestProbeRecent:
    """The cross-process re-probe throttle gate."""

    def test_recent_within_ttl(self):
        now = 1000.0
        entry = {"_probed_at": now - (PROBE_VERDICT_TTL_S - 1)}
        assert probe_recent(entry, now=now) is True

    def test_stale_past_ttl(self):
        now = 1000.0
        entry = {"_probed_at": now - (PROBE_VERDICT_TTL_S + 1)}
        assert probe_recent(entry, now=now) is False

    def test_false_for_missing_stamp_and_non_dicts(self):
        assert probe_recent({"five_hour": {"pct": 5.0}}) is False  # plain usage entry
        assert probe_recent({}) is False                            # no _probed_at
        assert probe_recent(None) is False
        assert probe_recent("nope") is False


class TestProbeOk:
    """Distinguishes a confirmed-OK verdict from a no-signal marker."""

    def test_true_only_for_probe_ok_marker(self):
        assert probe_ok({"_probe_ok": True, "_probed_at": 1.0}) is True

    def test_false_otherwise(self):
        assert probe_ok({"_probe_ok": False}) is False
        assert probe_ok({"_unavailable": True, "_probed_at": 1.0}) is False
        assert probe_ok({"five_hour": {"pct": 5.0}}) is False
        assert probe_ok({}) is False
        assert probe_ok(None) is False


class TestWriteCache:
    def test_creates_file_and_parent_dirs(self, tmp_path):
        cache_file = tmp_path / "sub" / "dir" / "test.json"
        write_cache(cache_file, {"key": "value"})

        assert cache_file.exists()
        raw = json.loads(cache_file.read_text())
        assert raw["data"] == {"key": "value"}
        assert "timestamp" in raw

    def test_roundtrip(self, tmp_path):
        cache_file = tmp_path / "test.json"
        data = {"accounts": [1, 2, 3], "nested": {"a": True}}

        write_cache(cache_file, data)
        result = read_cache(cache_file, ttl=60)

        assert result == data
