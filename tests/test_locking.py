"""Tests for file locking mechanism."""

from __future__ import annotations

import contextlib
import multiprocessing
import threading
import time
from pathlib import Path

import pytest

from tests.conftest import _advancing_clock, _thread_scoped_sleep, _crossing_clock

from claude_swap import locking
from claude_swap.exceptions import LockError
from claude_swap.locking import FileLock


@contextlib.contextmanager
def _scripted_time(monotonic, sleep):
    """Swap `locking.time`'s clock and sleep for the duration of the block.

    `locking.time` IS the `time` module, so this patch is process-global;
    restoring it HERE rather than at teardown keeps the assertions that follow
    -- and pytest's own bookkeeping -- on the real clock.
    """
    real = locking.time.monotonic, locking.time.sleep
    locking.time.monotonic, locking.time.sleep = monotonic, sleep
    try:
        yield
    finally:
        locking.time.monotonic, locking.time.sleep = real


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


class TestTheDeadlineCanPassBetweenTheCheckAndTheClamp:
    """The floor under the clamp, which nothing exercised.

    The loop reads the clock to decide whether the budget is gone, then reads
    it AGAIN to size the sleep. Between those two reads it does a `flock`
    attempt; on a network `~/.claude` that is milliseconds, not nanoseconds.
    When the budget expires in the gap, `deadline - monotonic()` is negative
    and `time.sleep` raises `ValueError` out of `acquire()`, which documents
    itself as returning True or False -- and the surrounding
    `except (BlockingIOError, OSError)` does not catch it, because the sleep
    is inside that handler.

    The constant this replaced could never be negative, so the hazard and its
    guard arrived together and neither had a witness.
    """

    def test_a_deadline_crossed_mid_iteration_is_not_a_ValueError(
        self, tmp_path, monkeypatch
    ):
        target = tmp_path / "held.json"
        holder = FileLock(target, timeout=5)
        assert holder.acquire() is True
        try:
            # The 3rd read is the clamp's, and it lands PAST the deadline the
            # 2nd read was measured against.
            reads = []
            clock = _crossing_clock(reads)

            monkeypatch.setattr(locking.time, "monotonic", clock)
            waiter = FileLock(target, timeout=0.5)
            got = waiter.acquire()
        finally:
            holder.release()
        # THE PREMISE, AND THE FULL SCHEDULE. `got is False` alone is
        # satisfied by any clock that simply runs out the budget, including
        # one that never revisits the clamp at all. A prefix check is not
        # enough either: ticks[0] and ticks[1] are both 0.0, so one foreign
        # `time.monotonic()` read before this closure's own first call shifts
        # every later read by one slot while `reads[:3]` still reads
        # `[0.0, 0.0, 0.6]` -- the deadline check then trips on that shifted
        # 0.6 and returns False WITHOUT ever reaching the clamp, so a deleted
        # floor is invisible again. The clamp branch, reached, produces
        # exactly four reads (start, deadline check, clamp, deadline check);
        # a shifted schedule produces three and never gets a fourth, so only
        # the full sequence -- not a prefix -- proves the clamp arm ran.
        assert reads == [0.0, 0.0, 0.6, 0.6], (
            f"the clamp arm was never entered: {reads}"
        )
        assert got is False, (
            f"acquire() answered {got!r}; it documents True or False"
        )

class TestTheClampSurvivesWeakeningNotOnlyDeletion:
    """`min(sleep, timeout)` is a no-op once most of the budget is spent.

    The case above uses a budget SMALLER than the flat sleep (0.01 vs 0.1),
    where `min(0.1, timeout)` and `min(0.1, remaining)` are the same number.
    Measured: weakening this clamp to the whole timeout leaves the two lock
    suites at 38 passed, while DELETING it is caught — so the file
    discriminates against deletion and is blind to weakening. That is the
    identical hole `test_claude_locks.py` grew a class to close, left open on
    the other file the same change touched.

    Per sleep against the budget left at that moment, never a wall-clock total
    or the shape of the last draw: both depend on when the loop happens to
    arrive, and on a saturated CI core they report a correct clamp as broken.
    """

    def test_a_budget_smaller_than_the_flat_sleep_is_not_overshot(
        self, tmp_path
    ):
        """THE CLAMP'S BOUND, which the case below cannot see.

        That one scripts a budget of 0.407, so flooring the deadline to
        anything up to 0.407 is a no-op for it -- and `deadline` has exactly
        one consumer here, the clamp, so mutating the bound IS mutating the
        clamp. Measured on the shipped tree: `deadline = start + max(timeout,
        0.1)` leaves both lock files at 42 passed while a caller asking for
        10 ms waits 100 ms, a 10x overshoot of the budget this module exists
        to honour.

        A budget BELOW the flat sleep is what makes any floor above it
        visible, and the total is the reading that shows it -- a single
        clamped sleep is the whole hold.
        """
        target = tmp_path / "held.json"
        holder = FileLock(target, timeout=5)
        assert holder.acquire(), "premise: the lock must be held to contend"
        try:
            budget, clock, slept = 0.01, [0.0], []
            # THE SHARED CLOCK, not a copy of its arithmetic. Advancing only
            # inside `fake_sleep` freezes it the moment a sleep is deleted, so
            # the run hangs instead of going red.
            with _scripted_time(_advancing_clock(clock, budget),
                                _thread_scoped_sleep(locking, clock, slept, step=0.001)):
                waiter = FileLock(target, timeout=budget)
                assert not waiter.acquire(), "the held lock was handed over"

            assert slept, f"the instrument, not the code: {slept}"
            assert sum(slept) <= budget, (
                f"slept {sum(slept)}s against a {budget}s budget -- the "
                f"deadline was not the caller's (sleeps: {slept})"
            )
            # AND THE OTHER SIDE. An upper bound alone is satisfied by a list
            # of zeros, which is precisely a hot spin: the clamp collapsing to
            # 0.0 gives ten sleeps of nothing, sums to 0.0, and passes. The
            # waiter must spend its whole budget waiting, not burning CPU.
            assert sum(slept) >= budget * 0.9, (
                f"slept only {sum(slept)}s of a {budget}s budget across "
                f"{len(slept)} call(s) -- the retry loop is spinning rather "
                f"than waiting (sleeps: {slept})"
            )
        finally:
            holder.release()

    def test_no_retry_sleep_outlives_the_budget_it_was_given(self, tmp_path):
        """A SCRIPTED CLOCK: the remaining budget at each sleep is CHOSEN.

        The earlier form raced for it -- anchor on the code's own `monotonic`,
        then assert the run had ENTERED the region where a flat sleep shows.
        `min(lefts)` is `budget mod flat`, so losing one loop iteration to
        latency raises it by a whole `flat` and the assertion can never be
        satisfied: measured on pristine source, FAIL at 10ms and 12ms and 40ms
        of injected latency, PASS at 0 and 25 and 30. Under ordinary
        fair-share contention, 2 of 60 runs red.

        Nothing here touches the wall clock now, so there is no race to lose.
        """
        target = tmp_path / "held.json"
        holder = FileLock(target, timeout=5)
        assert holder.acquire(), "premise: the lock must be held to contend"
        try:
            # CHOSEN SO THE TAIL REMAINDER IS TINY. The loop spends
            # 0.1 + 0.001 per iteration, so `budget mod 0.101` is what is
            # left when the last sleep is drawn, and a flat sleep is only
            # visible when it exceeds that. 0.45 left 0.046 and a flat 0.005
            # was invisible; 0.407 leaves 0.003.
            budget, clock, slept = 0.407, [0.0], []
            with _scripted_time(
                _advancing_clock(clock, budget),
                _thread_scoped_sleep(locking, clock, slept, budget, step=0.001),
            ):
                waiter = FileLock(target, timeout=budget)
                assert not waiter.acquire(), "the held lock was handed over"

            assert len(slept) >= 2, f"the instrument, not the code: {slept}"
            for left, seconds in slept:
                assert seconds <= max(left, 0.0), (
                    f"slept {seconds}s with {left}s left — the clamp used "
                    f"`timeout`, not what remains of it (all sleeps: {slept})"
                )
            # AN ATTEMPT COUNT, which is what the remainder assert cannot
            # see, and FIRST, because it is the one that names the defect.
            # Detection below is a RESONANCE -- it fires only when the budget
            # happens to leave a remainder smaller than the sleep -- so for
            # every shrink the resonance DOES catch, the reader was told the
            # instrument could not reach a small enough remainder and invited
            # to retune the budget rather than revert the shrink.
            #
            # THE MEASURED COUNT EXACTLY. A bound with a spare sleep in it
            # leaves exactly one shrink alive -- 0.1 -> 0.08, 25% more flock
            # attempts on every contended acquire, whole suite green. The
            # bound cannot flake upward: extra `time.monotonic()` reads from
            # any thread ADVANCE the scripted clock, which exhausts the
            # budget sooner and yields FEWER sleeps.
            # THE MARGIN, so a reader knows how exact this is. Iteration 4
            # records 0.104s left against a 0.1 clamp, so ~0.004s of extra
            # clock advance -- about 980 foreign reads at the scripted step --
            # would drop the count to 4. That direction passes this bound and
            # fails the remainder assert below instead.
            assert len(slept) <= 5, (
                f"{len(slept)} sleeps in a {budget}s budget — the retry "
                "backed off less than it claims to"
            )
            assert min(l for l, _ in slept) < 0.005, (
                f"the run must reach a remainder small enough for any flat "
                f"sleep to overshoot it: {slept}"
            )
        finally:
            holder.release()

