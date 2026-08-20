"""Tests for file locking mechanism."""

from __future__ import annotations

import multiprocessing
import time
from pathlib import Path

import pytest

from claude_swap import locking
from claude_swap.exceptions import LockError
from claude_swap.locking import FileLock


class TestFileLock:
    """Test FileLock class."""

    def test_acquire_and_release(self, tmp_path: Path):
        """Test basic lock acquire and release."""
        lock_path = tmp_path / ".lock"
        lock = FileLock(lock_path)

        assert lock.acquire(timeout=1.0) is True
        assert lock._locked is True
        lock.release()
        assert lock._locked is False

    def test_context_manager(self, tmp_path: Path):
        """Test using lock as context manager."""
        lock_path = tmp_path / ".lock"

        with FileLock(lock_path) as lock:
            assert lock._locked is True

        assert lock._locked is False

    def test_context_manager_creates_parent_dirs(self, tmp_path: Path):
        """Test that lock creates parent directories."""
        lock_path = tmp_path / "nested" / "dir" / ".lock"

        with FileLock(lock_path):
            assert lock_path.parent.exists()

    def test_lock_timeout(self, tmp_path: Path):
        """Test that lock times out when already held."""
        lock_path = tmp_path / ".lock"

        # Acquire first lock
        lock1 = FileLock(lock_path)
        assert lock1.acquire(timeout=1.0) is True

        # Try to acquire second lock - should timeout
        lock2 = FileLock(lock_path)
        assert lock2.acquire(timeout=0.5) is False

        lock1.release()

    def test_a_small_timeout_is_not_overshot_by_the_retry_sleep(
            self, tmp_path: Path, monkeypatch):
        """`timeout` must bound acquire(), the retry sleep included.

        The deadline is checked before the sleep, so a flat sleep runs to
        completion past a timeout shorter than itself and the check cannot
        fire until it returns.

        Asserted on the sleep ARGUMENT rather than on elapsed time alone: a
        wall-clock ceiling only says "overshoot smaller than N", so shrinking
        the flat sleep below N keeps it green while `timeout` is still
        unbounded for every value under the new constant. It also makes the
        test independent of clock granularity, which differs by platform.
        """
        lock_path = tmp_path / "overshoot.lock"
        holder = FileLock(lock_path)
        assert holder.acquire(timeout=1.0), "the fixture failed to take the lock"
        waiter = FileLock(lock_path)
        try:
            slept = []
            real_sleep = time.sleep

            def recording(seconds):
                slept.append((time.monotonic(), seconds))
                return real_sleep(seconds)

            monkeypatch.setattr(locking.time, "sleep", recording)
            # SMALL ON PURPOSE. The invariant only discriminates against a
            # flat sleep LARGER than the budget: with a generous budget a
            # 0.1s sleep fits inside what is left and the assert passes on
            # the very bug this exists for (measured: budget=0.5 lets flat
            # 0.1 through).
            budget = 0.01
            begin = time.monotonic()
            got = waiter.acquire(timeout=budget)
            elapsed = time.monotonic() - begin

            assert not got, "the lock was held; acquire must not succeed"
            assert slept, "no retry sleep happened — the instrument, not the code"
            # `begin` is anchored here, but `acquire` starts its own deadline
            # AFTER `mkdir(parents=...)` + `open()`, so a correct clamp can
            # sleep slightly past THIS deadline. Measured worst case for that
            # skew: 0.9ms, which left only 12% headroom under a 1ms tolerance
            # -- real flakiness on a slow first `open()`. Widened to `budget`,
            # which is the most a correct clamp can produce: its own cap is
            # the remaining internal budget, never more than `timeout`.
            #
            # NOT wider. At 0.05 a flat 0.02 and a flat 0.04 both PASS
            # (measured), so a regression reintroducing a small flat sleep
            # would ship green. A ceiling you can tune a flat sleep under is
            # not an invariant, which is the whole point of the docstring
            # above.
            deadline = begin + budget
            for at, seconds in slept:
                left = deadline - at
                assert seconds <= max(0.0, left) + budget, (
                    f"slept {seconds:.3f}s with {left:.3f}s of budget left — "
                    "the retry sleep ignored the deadline"
                )
                # Anchor-free, so no skew to absorb: the clamp is capped by
                # the remaining budget, which never exceeds the whole timeout.
                assert seconds <= budget + 1e-6, (
                    f"a single retry sleep of {seconds:.3f}s exceeds the "
                    f"whole {budget}s timeout"
                )
            # Kept alongside the invariant: this one catches an overshoot
            # end to end, the invariant catches a flat sleep tuned under the
            # wall-clock ceiling. Neither alone covers both.
            assert elapsed < 0.05, (
                f"a {budget}s timeout took {elapsed:.3f}s — the retry sleep "
                "ignored the remaining budget"
            )
        finally:
            waiter.release()
            holder.release()

    def test_lock_acquired_after_release(self, tmp_path: Path):
        """Test that lock can be acquired after previous holder releases."""
        lock_path = tmp_path / ".lock"

        lock1 = FileLock(lock_path)
        lock1.acquire(timeout=1.0)
        lock1.release()

        lock2 = FileLock(lock_path)
        assert lock2.acquire(timeout=1.0) is True
        lock2.release()

    def test_context_manager_raises_on_timeout(self, tmp_path: Path):
        """Test that context manager raises LockError on timeout."""
        lock_path = tmp_path / ".lock"

        # Hold the lock
        holder = FileLock(lock_path)
        holder.acquire(timeout=1.0)

        # Try to acquire with context manager
        with pytest.raises(LockError):
            # Create a lock with very short timeout
            lock = FileLock(lock_path)
            lock.acquire = lambda timeout=10.0: False  # Force failure
            with lock:
                pass

        holder.release()

    def test_double_release_safe(self, tmp_path: Path):
        """Test that releasing twice doesn't raise."""
        lock_path = tmp_path / ".lock"
        lock = FileLock(lock_path)

        lock.acquire(timeout=1.0)
        lock.release()
        lock.release()  # Should not raise


def _hold_lock_process(lock_path: str, duration: float, ready_event, done_event):
    """Helper function to hold a lock in a subprocess."""
    lock = FileLock(Path(lock_path))
    if lock.acquire(timeout=5.0):
        ready_event.set()  # Signal that lock is held
        time.sleep(duration)
        lock.release()
    done_event.set()


class TestFileLockConcurrency:
    """Test concurrent access to file locks."""

    def test_concurrent_access_blocked(self, tmp_path: Path):
        """Test that concurrent processes are blocked."""
        lock_path = tmp_path / ".lock"

        ready_event = multiprocessing.Event()
        done_event = multiprocessing.Event()

        # Start process that holds the lock
        p = multiprocessing.Process(
            target=_hold_lock_process,
            args=(str(lock_path), 2.0, ready_event, done_event),
        )
        p.start()

        # Wait for the subprocess to acquire the lock
        ready_event.wait(timeout=5.0)

        # Now try to acquire - should fail fast
        lock = FileLock(lock_path)
        result = lock.acquire(timeout=0.5)

        assert result is False

        # Clean up
        p.join(timeout=5.0)
        if p.is_alive():
            p.terminate()

    def test_lock_acquired_after_process_exits(self, tmp_path: Path):
        """Test that lock can be acquired after holding process exits."""
        lock_path = tmp_path / ".lock"

        ready_event = multiprocessing.Event()
        done_event = multiprocessing.Event()

        # Start process that holds the lock briefly
        p = multiprocessing.Process(
            target=_hold_lock_process,
            args=(str(lock_path), 0.5, ready_event, done_event),
        )
        p.start()

        # Wait for subprocess to finish
        done_event.wait(timeout=5.0)
        p.join(timeout=5.0)

        # Now we should be able to acquire
        lock = FileLock(lock_path)
        result = lock.acquire(timeout=1.0)

        assert result is True
        lock.release()
