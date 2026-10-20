"""Directory-based advisory locking that works on network filesystems.

``mkdir`` is the mutex: it is a single server-side operation that either creates
the directory or fails with ``EEXIST``, with no separate test-and-set for two
callers to interleave. That property is what makes this the right primitive for
a shared home, and it is not interchangeable with ``fcntl.flock``.

WHY NOT flock. A cluster home exported over NFS is commonly mounted
``nolock,local_lock=all``, which resolves ``flock``/``fcntl`` locks on the
CLIENT and never consults the server. Two processes on two nodes then both
"acquire" the same lock and neither ever sees contention. Measured on one such
home, two nodes contending for 20s on the same file:

    flock   2829 acquisitions   2653 critical-section violations   0 contentions
    mkdir   1088 acquisitions      0 critical-section violations   11387 contentions

The zero contentions under flock is the tell: the nodes never excluded each
other at all. Use this module for anything guarding state on a path that might
be a network mount.

Staleness is mtime-based, so it also assumes roughly-synchronised clocks --
true under NTP, which the same environments run.
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from claude_swap.exceptions import LockError

#: Default seconds before a held lock is presumed abandoned and taken over.
DEFAULT_STALENESS_S = 10.0
#: How often a holder refreshes the lock's mtime so others do not deem it stale.
TOUCH_INTERVAL_S = 3.0
#: Default bounded wait before giving up on a contended lock.
DEFAULT_TIMEOUT_S = 9.0

_logger = logging.getLogger("claude-swap")


class DirectoryLock:
    """An exclusive lock held as a directory, with explicit acquire/release.

    Separate from the :func:`directory_lock` context manager because callers
    that expose their own acquire/release API (``claude_swap.locking.FileLock``)
    need the mechanism without the ``with`` shape, and two implementations of a
    lock protocol is one too many.
    """

    def __init__(
        self,
        lock_dir: Path,
        *,
        staleness: float = DEFAULT_STALENESS_S,
        touch_interval: float = TOUCH_INTERVAL_S,
    ) -> None:
        self.lock_dir = Path(lock_dir)
        self.staleness = staleness
        self.touch_interval = touch_interval
        self._stop_touching: threading.Event | None = None
        self._toucher: threading.Thread | None = None
        self._held = False

    @property
    def held(self) -> bool:
        return self._held

    def acquire(self, timeout: float = DEFAULT_TIMEOUT_S) -> bool:
        """Take the lock, waiting up to ``timeout``. False if it stayed held.

        Returns a bool rather than raising so the caller chooses the failure
        shape; :func:`directory_lock` turns False into its own exception.
        """
        if self._held:
            raise RuntimeError(f"{self.lock_dir} is already held by this instance")
        self.lock_dir.parent.mkdir(parents=True, exist_ok=True)
        start = time.monotonic()
        while True:
            try:
                os.mkdir(self.lock_dir)
                break
            except FileExistsError:
                pass
            if time.monotonic() - start > timeout:
                return False
            try:
                held_mtime = os.stat(self.lock_dir).st_mtime
            except FileNotFoundError:
                continue  # holder released between mkdir and stat; retry now
            if time.time() - held_mtime > self.staleness:
                # A holder that died leaves the directory behind -- unlike
                # flock, which the kernel drops when the process exits. Staleness
                # is what replaces that, so a crash costs the next caller up to
                # `staleness` seconds rather than blocking forever. Losing the
                # rmdir/mkdir race to another waiter just means looping again.
                try:
                    os.rmdir(self.lock_dir)
                except OSError:
                    time.sleep(0.05)  # can't remove it either; don't spin hot
                continue
            time.sleep(0.25 + random.random() * 0.25)

        self._held = True
        self._stop_touching = threading.Event()
        stop = self._stop_touching
        lock_dir = self.lock_dir
        interval = self.touch_interval

        def _touch() -> None:
            while not stop.wait(interval):
                try:
                    os.utime(lock_dir)
                except OSError:
                    return  # lock stolen/removed; nothing left to keep alive

        self._toucher = threading.Thread(target=_touch, daemon=True)
        self._toucher.start()
        return True

    def release(self) -> None:
        """Release the lock. Safe to call when not held."""
        if not self._held:
            return
        if self._stop_touching is not None:
            self._stop_touching.set()
        if self._toucher is not None:
            self._toucher.join(timeout=1.0)
        self._stop_touching = None
        self._toucher = None
        self._held = False
        try:
            os.rmdir(self.lock_dir)
        except FileNotFoundError:
            _logger.warning(
                "Lock %s vanished while held (taken over as stale?)", self.lock_dir
            )
        except OSError as e:
            _logger.warning("Failed to release lock %s: %s", self.lock_dir, e)


@contextmanager
def directory_lock(
    lock_dir: Path,
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
    staleness: float = DEFAULT_STALENESS_S,
    touch_interval: float = TOUCH_INTERVAL_S,
    timeout_error: type[Exception] = LockError,
):
    """Hold ``lock_dir`` as an exclusive lock for the duration of the block.

    ``touch_interval`` is a parameter rather than a module constant read at run
    time so a WRAPPER can expose its own knob: patching the constant in the
    module a caller imports must still take effect here.

    ``timeout_error`` lets a caller raise its own exception type without
    reimplementing the loop -- the Claude Code lock helpers raise
    ``ClaudeCodeLockTimeout`` so their callers can distinguish that contention
    from any other lock failure.

    Raises:
        timeout_error: The lock stayed held past ``timeout``.
    """
    lock = DirectoryLock(
        lock_dir, staleness=staleness, touch_interval=touch_interval
    )
    if not lock.acquire(timeout=timeout):
        raise timeout_error(
            f"Could not acquire {Path(lock_dir).name} within {timeout:g}s — "
            "another process is holding it. Retry in a few seconds."
        )
    try:
        yield
    finally:
        lock.release()
