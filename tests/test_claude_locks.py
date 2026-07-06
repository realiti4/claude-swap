"""Tests for the proper-lockfile-compatible Claude Code lock helpers."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from claude_swap import claude_locks
from claude_swap.claude_locks import (
    claude_config_lock,
    claude_credentials_lock,
    config_lock_dir,
    credentials_lock_dir,
    proper_lockfile,
)
from claude_swap.exceptions import ClaudeCodeLockTimeout


@pytest.fixture
def lock_dir(tmp_path: Path) -> Path:
    return tmp_path / "target.lock"


class TestProperLockfile:
    def test_acquire_creates_and_release_removes(self, lock_dir):
        with proper_lockfile(lock_dir):
            assert lock_dir.is_dir()
        assert not lock_dir.exists()

    def test_reacquire_after_release(self, lock_dir):
        with proper_lockfile(lock_dir):
            pass
        with proper_lockfile(lock_dir):
            assert lock_dir.is_dir()

    def test_contention_times_out(self, lock_dir):
        lock_dir.mkdir()  # fresh mtime = live holder
        start = time.monotonic()
        with pytest.raises(ClaudeCodeLockTimeout):
            with proper_lockfile(lock_dir, timeout=0.5):
                pass
        assert time.monotonic() - start < 5.0
        assert lock_dir.is_dir()  # the holder's lock is left alone

    def test_stale_lock_is_taken_over(self, lock_dir):
        lock_dir.mkdir()
        past = time.time() - 30
        os.utime(lock_dir, (past, past))
        with proper_lockfile(lock_dir, timeout=2.0):
            assert lock_dir.is_dir()
            # We own it now: mtime is fresh, not the 30s-old corpse.
            assert time.time() - lock_dir.stat().st_mtime < 5.0
        assert not lock_dir.exists()

    def test_release_tolerates_stolen_lock(self, lock_dir):
        with proper_lockfile(lock_dir):
            os.rmdir(lock_dir)  # simulate a stale-takeover by another process
        # No exception; nothing left behind.
        assert not lock_dir.exists()

    def test_toucher_keeps_mtime_fresh(self, lock_dir, monkeypatch):
        monkeypatch.setattr(claude_locks, "TOUCH_INTERVAL_S", 0.1)
        with proper_lockfile(lock_dir):
            past = time.time() - 30
            os.utime(lock_dir, (past, past))
            time.sleep(0.4)
            assert time.time() - lock_dir.stat().st_mtime < 10.0

    def test_creates_missing_parent(self, tmp_path):
        nested = tmp_path / "a" / "b" / "target.lock"
        with proper_lockfile(nested):
            assert nested.is_dir()


class TestLockPaths:
    def test_default_paths(self, temp_home, monkeypatch):
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        assert credentials_lock_dir() == temp_home / ".claude.lock"
        assert config_lock_dir() == temp_home / ".claude.json.lock"

    def test_claude_config_dir_is_honored(self, tmp_path, monkeypatch):
        custom = tmp_path / "custom-claude"
        custom.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))
        assert credentials_lock_dir() == tmp_path / "custom-claude.lock"
        # ~/.claude.json resolves relative to CLAUDE_CONFIG_DIR too.
        assert config_lock_dir() == custom / ".claude.json.lock"

    def test_named_helpers_lock_their_dirs(self, temp_home, monkeypatch):
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        with claude_credentials_lock():
            assert (temp_home / ".claude.lock").is_dir()
            with claude_config_lock():
                assert (temp_home / ".claude.json.lock").is_dir()
        assert not (temp_home / ".claude.lock").exists()
        assert not (temp_home / ".claude.json.lock").exists()
