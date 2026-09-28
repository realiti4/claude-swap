"""Cross-process locking for concurrent access protection.

The lock artifact is a DIRECTORY, created with ``mkdir``. This module used to
use ``fcntl.flock`` (``msvcrt.locking`` on Windows), which is correct on a local
filesystem and silently useless on a network one.

WHY IT CHANGED. An NFS home mounted ``nolock,local_lock=all`` -- the common
cluster export, and the default on plenty of managed systems -- resolves
``flock`` on the CLIENT and never consults the server. Two processes on two
nodes both "acquire" the same lock, neither ever observes contention, and both
proceed into the critical section. Measured on such a home, two nodes contending
on one lock for 20 seconds:

    flock   2829 acquisitions   2653 critical-section violations   0 contentions
    mkdir   1088 acquisitions      0 critical-section violations   11387 contentions

The zero contentions under flock is the tell: the nodes never excluded each
other at all. ``mkdir`` is a single server-side operation that either creates
the directory or fails with ``EEXIST``, so it excludes properly across nodes --
verified on the same home with the same harness.

THE ONE BEHAVIOURAL COST. The kernel drops an ``flock`` when its process dies;
nothing drops a directory. Staleness replaces that: a lock whose mtime has not
been refreshed within ``_STALENESS_S`` is presumed abandoned and taken over, so
a crashed holder costs the next caller seconds rather than blocking forever. A
live holder refreshes every ``_TOUCH_INTERVAL_S``, giving four missed
refreshes of margin before anyone would steal a lock that is genuinely held.

The public API is unchanged: ``FileLock(path, timeout)``, ``acquire`` returning
a bool, ``release``, and a context manager that raises ``LockError``.
"""

from __future__ import annotations

from pathlib import Path

from claude_swap.dirlock import DirectoryLock
from claude_swap.exceptions import LockError

#: Seconds without a refresh before a held lock is presumed abandoned. Chosen
#: below FileLock's 10s default timeout so a single acquire() can recover from a
#: crashed holder rather than needing a second invocation, and at four times the
#: refresh interval so a merely busy holder is never robbed.
_STALENESS_S = 8.0
#: How often a holder refreshes its lock's mtime.
_TOUCH_INTERVAL_S = 2.0


class FileLock:
    """Cross-process lock held as a directory at ``lock_path``.

    Not reentrant: acquiring a lock this process already holds fails, which
    several callers rely on to catch nested-lock bugs.
    """

    def __init__(self, lock_path: Path, timeout: float = 10.0):
        self.lock_path = Path(lock_path)
        self.timeout = timeout
        self._locked = False
        self._lock = DirectoryLock(
            self.lock_path,
            staleness=_STALENESS_S,
            touch_interval=_TOUCH_INTERVAL_S,
        )

    def _clear_legacy_lock_file(self) -> None:
        """Remove a REGULAR FILE left at the lock path by the flock era.

        The old implementation created the path with ``open(path, "w")`` and
        never unlinked it, so every store written by an earlier version still
        has one. ``mkdir`` onto it would fail with ``FileExistsError`` forever
        and the staleness path cannot clear it either (``rmdir`` refuses a
        file), which would wedge the lock permanently after upgrading.

        Discarding it loses nothing: flock's state lived in the kernel, never in
        the file, so the file itself carries no information. The only cost is
        during an upgrade with an OLD version still running, where that process
        holds an flock this one cannot see -- an exposure that already exists,
        since flock and mkdir do not exclude each other regardless.
        """
        try:
            if self.lock_path.is_file():
                self.lock_path.unlink()
        except OSError:
            pass  # best effort; a real problem resurfaces as an acquire failure

    def acquire(self, timeout: float | None = None) -> bool:
        """Acquire the lock, waiting up to ``timeout`` seconds.

        Returns:
            True if acquired, False if it stayed held for the whole timeout.
        """
        if timeout is None:
            timeout = self.timeout
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._clear_legacy_lock_file()
        if self._lock.acquire(timeout=timeout):
            self._locked = True
            return True
        return False

    def release(self) -> None:
        """Release the lock. Safe to call when not held."""
        if self._locked:
            self._lock.release()
            self._locked = False

    def __enter__(self) -> FileLock:
        if not self.acquire():
            raise LockError("Failed to acquire lock - another instance may be running")
        return self

    def __exit__(self, *args) -> None:
        self.release()
