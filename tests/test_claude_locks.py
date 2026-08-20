"""Tests for the proper-lockfile-compatible Claude Code lock helpers."""

from __future__ import annotations

import errno
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

    def _count_touches(self, monkeypatch, lock_dir, fail_first=None):
        """Patch os.utime and count only the calls aimed at OUR lock.

        Keyed on the path, not on a global call counter: the module does
        `import os`, so patching `claude_locks.os.utime` patches the global
        one and any other caller in the process consumes an injected failure.
        """
        real = os.utime
        state = {"n": 0}

        def counting(path, *a, **k):
            if not isinstance(path, int) and os.fspath(path) == os.fspath(lock_dir):
                state["n"] += 1
                if fail_first is not None and state["n"] == 1:
                    raise fail_first
            return real(path, *a, **k)

        monkeypatch.setattr(claude_locks.os, "utime", counting)
        return state, real

    def test_a_transient_utime_failure_does_not_disarm_the_heartbeat(
            self, lock_dir, monkeypatch):
        """One failed touch must not stop the touching.

        The toucher returned on any OSError, so a single transient failure
        killed the heartbeat while the lock was still held — and after
        CONFIG_STALENESS_S any waiter legally steals it.
        """
        monkeypatch.setattr(claude_locks, "TOUCH_INTERVAL_S", 0.05)
        touches, real = self._count_touches(
            monkeypatch, lock_dir, fail_first=OSError("transient"))
        with proper_lockfile(lock_dir):
            past = time.time() - 30
            real(lock_dir, (past, past))
            time.sleep(0.4)
            assert touches["n"] > 1, "the toucher stopped after one failure"
            assert time.time() - lock_dir.stat().st_mtime < 10.0, (
                "the lock went stale while still held, so a waiter may steal it"
            )

    @pytest.mark.parametrize("errno_", [errno.ESTALE, errno.EACCES, errno.EIO])
    def test_a_failure_that_is_not_absence_keeps_the_heartbeat(
            self, lock_dir, monkeypatch, errno_):
        """Only absence may stop the toucher; every other errno is transient.

        `os.stat` fails too, because that is what these errnos mean on a real
        filesystem — a stale NFS handle or an unreadable directory does not
        answer `stat` either. Injecting only a `utime` failure leaves the
        directory genuinely present, so any implementation that asks the
        filesystem gets a truthful "still there" and the test passes without
        exercising anything.

        With `stat` failing too, asking `lock_dir.exists()` is wrong on every
        supported Python and for two different reasons: 3.12 re-raises these
        errnos out of the thread, 3.13+ swallows them and answers False, which
        reads as "gone" for a lock we still hold. requires-python is >=3.12
        and CI runs 3.12, so neither half can be dismissed as theoretical.
        """
        monkeypatch.setattr(claude_locks, "TOUCH_INTERVAL_S", 0.05)
        real_utime, real_stat = os.utime, os.stat
        touches = {"n": 0}
        broken = {"on": False}

        def failing_utime(path, *a, **k):
            if not isinstance(path, int) and os.fspath(path) == os.fspath(lock_dir):
                touches["n"] += 1
                if not broken["on"]:
                    broken["on"] = True
                    raise OSError(errno_, "not absence")
            return real_utime(path, *a, **k)

        def failing_stat(path, *a, **k):
            if (broken["on"] and not isinstance(path, int)
                    and os.fspath(path) == os.fspath(lock_dir)):
                raise OSError(errno_, "not absence")
            return real_stat(path, *a, **k)

        monkeypatch.setattr(claude_locks.os, "utime", failing_utime)
        monkeypatch.setattr(claude_locks.os, "stat", failing_stat)
        with proper_lockfile(lock_dir):
            time.sleep(0.4)
            settled = touches["n"]
        assert broken["on"], (
            "the injected failure never fired — the instrument, not the code"
        )
        assert settled > 1, (
            f"errno {errno_} stopped the toucher after {settled} touch(es); "
            "the lock is still held and only absence may stop it"
        )

    def test_a_vanished_lock_still_stops_the_toucher(self, lock_dir, monkeypatch):
        """The control. Retrying forever on a lock that is GONE would be the
        opposite defect, and the original `return` existed for this case.

        Asserted on the TOUCHER, not on the directory: `os.utime` cannot
        create one, so `assert not lock_dir.exists()` passes for a toucher
        that never stops at all.
        """
        monkeypatch.setattr(claude_locks, "TOUCH_INTERVAL_S", 0.05)
        touches, _ = self._count_touches(monkeypatch, lock_dir)
        with proper_lockfile(lock_dir):
            os.rmdir(lock_dir)
            time.sleep(0.15)
            settled = touches["n"]
            assert settled >= 1, (
                "the toucher never ran in the window — the instrument, not "
                "the code (raise the sleep or lower TOUCH_INTERVAL_S)"
            )
            time.sleep(0.3)
            assert touches["n"] == settled, (
                f"the toucher kept going on a dead lock "
                f"({touches['n'] - settled} more attempts)"
            )

    def test_creates_missing_parent(self, tmp_path):
        nested = tmp_path / "a" / "b" / "target.lock"
        with proper_lockfile(nested):
            assert nested.is_dir()

    def test_a_small_timeout_is_not_overshot_by_the_retry_sleep(self, lock_dir):
        """`timeout` must bound the call, sleeps included.

        The unclamped retry sleeps a full jittered 0.25-0.5s whatever the
        budget, so a sub-sleep timeout never times out anywhere near when it
        says.
        """
        lock_dir.mkdir()  # fresh mtime -> contended, not stale
        start = time.monotonic()
        with pytest.raises(ClaudeCodeLockTimeout):
            with proper_lockfile(lock_dir, timeout=0.01):
                pass
        elapsed = time.monotonic() - start
        assert elapsed < 0.15, (
            f"a 0.01s timeout overshot to {elapsed:.3f}s — the retry sleep "
            "ignored the remaining budget"
        )

    def test_the_rmdir_branch_also_respects_the_deadline(
            self, lock_dir, monkeypatch):
        """The stale-lock branch sleeps too, and it had no test.

        A stale lock we cannot remove sends every pass through `os.rmdir`'s
        failure sleep. Unclamped that is a flat 0.05s per pass regardless of
        how little budget is left.
        """
        lock_dir.mkdir()
        past = time.time() - claude_locks.CONFIG_STALENESS_S - 30
        os.utime(lock_dir, (past, past))

        real_rmdir = os.rmdir

        def refuse(path, *a, **k):
            if os.fspath(path) == os.fspath(lock_dir):
                raise OSError(errno.EACCES, "cannot remove")
            return real_rmdir(path, *a, **k)

        monkeypatch.setattr(claude_locks.os, "rmdir", refuse)
        start = time.monotonic()
        with pytest.raises(ClaudeCodeLockTimeout):
            with proper_lockfile(lock_dir, timeout=0.001):
                pass
        elapsed = time.monotonic() - start
        assert elapsed < 0.03, (
            f"a 0.001s timeout took {elapsed:.3f}s — the rmdir-failure sleep "
            "ignored the remaining budget"
        )


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
