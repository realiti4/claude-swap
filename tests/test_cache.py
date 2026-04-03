"""Tests for the shared cache helper."""

from __future__ import annotations

import json
import time

from claude_swap.cache import MISSING, read_cache, write_cache


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
