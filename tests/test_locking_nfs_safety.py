"""The properties FileLock gained when it stopped using flock.

Kept out of test_locking.py so that file stays byte-identical to upstream and
merges cleanly; these assert the NEW guarantees, not the old contract.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from claude_swap.dirlock import DirectoryLock
from claude_swap.locking import FileLock


class TestLockIsADirectory:
    def test_the_artifact_is_a_directory(self, tmp_path: Path):
        """mkdir is the mutex. A regular file would mean flock, which gives no
        cross-node exclusion on an NFS home mounted local_lock=all."""
        lock_path = tmp_path / ".lock"
        lock = FileLock(lock_path)
        assert lock.acquire(timeout=1.0)
        try:
            assert lock_path.is_dir()
            assert not lock_path.is_file()
        finally:
            lock.release()
        assert not lock_path.exists()


class TestLegacyLockFileUpgrade:
    def test_a_flock_era_lock_file_does_not_wedge_the_lock(self, tmp_path: Path):
        """Every store written by an older version still has a regular file at
        the lock path. mkdir onto it fails forever and rmdir cannot clear it,
        so without explicit handling the first upgraded run wedges."""
        lock_path = tmp_path / ".lock"
        lock_path.write_text("")  # exactly what the old implementation left
        assert lock_path.is_file()

        lock = FileLock(lock_path)
        assert lock.acquire(timeout=2.0) is True
        assert lock_path.is_dir()
        lock.release()

    def test_a_legacy_file_with_content_is_still_discarded(self, tmp_path: Path):
        # flock's state lived in the kernel, never in the file, so nothing in
        # the file's bytes can be worth preserving.
        lock_path = tmp_path / ".lock"
        lock_path.write_text("stale pid data from some other tool")
        lock = FileLock(lock_path)
        assert lock.acquire(timeout=2.0) is True
        lock.release()


class TestCrashedHolderRecovery:
    def test_a_stale_lock_is_taken_over(self, tmp_path: Path):
        """The kernel drops an flock when its process dies; nothing drops a
        directory. Staleness is what replaces that, or a crash would block
        every later run forever."""
        lock_path = tmp_path / ".lock"
        lock_path.mkdir()
        ancient = time.time() - 3600
        os.utime(lock_path, (ancient, ancient))

        lock = FileLock(lock_path)
        assert lock.acquire(timeout=2.0) is True
        assert time.time() - lock_path.stat().st_mtime < 5.0  # ours now
        lock.release()

    def test_a_freshly_held_lock_is_not_stolen(self, tmp_path: Path):
        lock_path = tmp_path / ".lock"
        lock_path.mkdir()  # fresh mtime = a live holder
        lock = FileLock(lock_path)
        assert lock.acquire(timeout=0.5) is False
        assert lock_path.is_dir()  # the holder's lock is left alone

    def test_recovery_fits_inside_one_default_timeout(self):
        """Staleness must sit below FileLock's default timeout, or recovering
        from a crashed holder needs a second invocation."""
        from claude_swap import locking

        assert locking._STALENESS_S < FileLock(Path("/x")).timeout

    def test_a_live_holder_has_refresh_margin(self):
        """Staleness must be several refresh intervals, or a merely busy holder
        gets robbed of a lock it still owns."""
        from claude_swap import locking

        assert locking._STALENESS_S >= 3 * locking._TOUCH_INTERVAL_S


class TestNonReentrancy:
    def test_the_same_instance_cannot_double_acquire(self, tmp_path: Path):
        # Several callers rely on non-reentrancy to catch nested-lock bugs.
        lock = FileLock(tmp_path / ".lock")
        assert lock.acquire(timeout=1.0)
        try:
            with pytest.raises(RuntimeError):
                lock._lock.acquire(timeout=0.2)
        finally:
            lock.release()

    def test_a_second_instance_is_blocked(self, tmp_path: Path):
        lock_path = tmp_path / ".lock"
        first, second = FileLock(lock_path), FileLock(lock_path)
        assert first.acquire(timeout=1.0)
        try:
            assert second.acquire(timeout=0.3) is False
        finally:
            first.release()
        assert second.acquire(timeout=1.0) is True
        second.release()


class TestDirectoryLockHolding:
    def test_release_without_acquire_is_safe(self, tmp_path: Path):
        DirectoryLock(tmp_path / ".lock").release()  # must not raise

    def test_held_reflects_state(self, tmp_path: Path):
        lock = DirectoryLock(tmp_path / ".lock")
        assert lock.held is False
        assert lock.acquire(timeout=1.0)
        assert lock.held is True
        lock.release()
        assert lock.held is False
