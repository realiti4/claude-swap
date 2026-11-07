"""Tests for the proper-lockfile-compatible Claude Code lock helpers."""

from __future__ import annotations

import os
import threading
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

    def test_two_waiters_cannot_both_take_over_one_stale_lock(
        self, lock_dir, monkeypatch
    ):
        """#420: two waiters that saw the same dead holder must not both end
        up holding the lock. The second used to remove the first's fresh lock
        as if it were the stale one, then take it as well."""
        lock_dir.mkdir()
        past = time.time() - 30
        os.utime(lock_dir, (past, past))
        monkeypatch.setattr(claude_locks, "os", _ForcedTakeoverRace(lock_dir))

        inside = most = 0
        count = threading.Lock()
        finished: list[bool] = []
        errors: list[BaseException] = []

        def waiter() -> None:
            nonlocal inside, most
            try:
                with proper_lockfile(lock_dir, timeout=5.0):
                    with count:
                        inside += 1
                        most = max(most, inside)
                    time.sleep(0.3)
                    with count:
                        inside -= 1
                finished.append(True)
            except BaseException as e:  # surfaced by the asserts below
                errors.append(e)

        threads = [threading.Thread(target=waiter) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        assert errors == []
        assert len(finished) == 2  # both still get the lock, one at a time
        assert most == 1

    def test_takeover_leaves_no_takeover_dir(self, lock_dir):
        lock_dir.mkdir()
        past = time.time() - 30
        os.utime(lock_dir, (past, past))
        with proper_lockfile(lock_dir, timeout=2.0):
            pass
        assert sorted(p.name for p in lock_dir.parent.iterdir()) == []

    def test_dead_takeover_dir_does_not_block_takeover(self, lock_dir):
        """A waiter killed mid-takeover leaves the takeover directory behind;
        once that is older than TAKEOVER_STALENESS_S it is cleared."""
        lock_dir.mkdir()
        takeover = lock_dir.with_name(lock_dir.name + claude_locks.TAKEOVER_SUFFIX)
        takeover.mkdir()
        past = time.time() - 30
        os.utime(lock_dir, (past, past))
        os.utime(takeover, (past, past))
        with proper_lockfile(lock_dir, timeout=2.0):
            assert time.time() - lock_dir.stat().st_mtime < 5.0
        assert not takeover.exists()


class _ForcedTakeoverRace:
    """Stand-in for ``claude_locks.os`` that forces the #420 interleaving:
    both waiters stat the same stale lock before either removes it, and the
    second waiter's takeover (its takeover-directory mkdir, or before the
    fix its rmdir) waits until the first waiter has retaken the lock."""

    def __init__(self, lock_dir: Path) -> None:
        self._lock_dir = str(lock_dir)
        self._takeover_dir = self._lock_dir + claude_locks.TAKEOVER_SUFFIX
        self._both_saw_stale = threading.Barrier(2, timeout=5)
        self._retaken = threading.Event()
        self._statted: set[int] = set()
        self._first_taker: int | None = None
        self._removals = 0
        self._guard = threading.Lock()

    def __getattr__(self, name):
        return getattr(os, name)

    def mkdir(self, path, *args, **kwargs):
        if str(path) == self._takeover_dir:
            me = threading.get_ident()
            with self._guard:
                if self._first_taker is None:
                    self._first_taker = me
                later = me != self._first_taker
            if later:
                self._retaken.wait(2)
        result = os.mkdir(path, *args, **kwargs)
        if str(path) == self._lock_dir:
            self._retaken.set()
        return result

    def stat(self, path, *args, **kwargs):
        result = os.stat(path, *args, **kwargs)
        if str(path) == self._lock_dir:
            me = threading.get_ident()
            with self._guard:
                first = me not in self._statted
                self._statted.add(me)
            if first:
                self._both_saw_stale.wait()
        return result

    def rmdir(self, path, *args, **kwargs):
        if str(path) == self._lock_dir:
            with self._guard:
                self._removals += 1
                second = self._removals == 2
            if second:
                self._retaken.wait(2)
        return os.rmdir(path, *args, **kwargs)


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


class TestCcRefreshLockProtocol:
    """Claude Code 2.1.218 guards its OAuth refresh with TWO locks —
    ``<config-home>/.oauth_refresh.lock`` (primary) then the legacy
    ``<config-home>.lock`` — both at a 60s staleness. cswap must follow the
    same protocol or mutual exclusion silently fails (extracted from the
    2.1.218 bundle: ``uKi``/``CKi``, ``stale: 60000, update: 5000``)."""

    def test_oauth_refresh_lock_dir_default(self, temp_home, monkeypatch):
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        assert (
            claude_locks.oauth_refresh_lock_dir()
            == temp_home / ".claude" / ".oauth_refresh.lock"
        )

    def test_oauth_refresh_lock_dir_honors_claude_config_dir(
        self, tmp_path, monkeypatch
    ):
        custom = tmp_path / "custom-claude"
        custom.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))
        assert claude_locks.oauth_refresh_lock_dir() == custom / ".oauth_refresh.lock"

    def test_credentials_lock_takes_both_locks(self, temp_home, monkeypatch):
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        new = temp_home / ".claude" / ".oauth_refresh.lock"
        legacy = temp_home / ".claude.lock"
        with claude_credentials_lock():
            assert new.is_dir(), "primary .oauth_refresh.lock not held"
            assert legacy.is_dir(), "legacy .claude.lock not held"
        assert not new.exists()
        assert not legacy.exists()

    def test_primary_contention_never_touches_legacy(self, temp_home, monkeypatch):
        """CC's order: primary first. If the primary is held we must time out
        without ever creating the legacy lock."""
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        new = temp_home / ".claude" / ".oauth_refresh.lock"
        new.mkdir(parents=True)  # fresh mtime = live CC holding its refresh lock
        with pytest.raises(ClaudeCodeLockTimeout):
            with claude_credentials_lock(timeout=0.5):
                pass
        assert not (temp_home / ".claude.lock").exists()
        assert new.is_dir()  # holder's lock untouched

    def test_legacy_contention_releases_primary(self, temp_home, monkeypatch):
        """If the legacy lock is contended after the primary was acquired,
        the primary must not be left behind (CC releases its new lock on
        legacy ELOCKED)."""
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        legacy = temp_home / ".claude.lock"
        legacy.mkdir()  # fresh = held
        with pytest.raises(ClaudeCodeLockTimeout):
            with claude_credentials_lock(timeout=0.5):
                pass
        assert not (temp_home / ".claude" / ".oauth_refresh.lock").exists()
        assert legacy.is_dir()

    def test_credentials_staleness_is_60s_not_10s(self, temp_home, monkeypatch):
        """A 30s-old credential lock belongs to a live CC (its budget is 60s)
        and must NOT be stolen — the old 10s staleness stole it."""
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        new = temp_home / ".claude" / ".oauth_refresh.lock"
        new.mkdir(parents=True)
        past = time.time() - 30
        os.utime(new, (past, past))
        with pytest.raises(ClaudeCodeLockTimeout):
            with claude_credentials_lock(timeout=0.5):
                pass
        assert new.is_dir()

    def test_credentials_lock_stale_past_60s_is_taken_over(
        self, temp_home, monkeypatch
    ):
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        new = temp_home / ".claude" / ".oauth_refresh.lock"
        legacy = temp_home / ".claude.lock"
        new.mkdir(parents=True)
        legacy.mkdir()
        past = time.time() - 70
        os.utime(new, (past, past))
        os.utime(legacy, (past, past))
        with claude_credentials_lock(timeout=2.0):
            assert new.is_dir()
            assert legacy.is_dir()
        assert not new.exists()
        assert not legacy.exists()

    def test_config_lock_staleness_stays_10s(self, temp_home, monkeypatch):
        """The config lock's CC-side defaults are unchanged — a 30s-old
        config lock is still stale and taken over."""
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        cfg = temp_home / ".claude.json.lock"
        cfg.mkdir()
        past = time.time() - 30
        os.utime(cfg, (past, past))
        with claude_config_lock(timeout=2.0):
            assert cfg.is_dir()
        assert not cfg.exists()
