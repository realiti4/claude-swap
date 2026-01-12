"""File locking for concurrent access protection."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import IO

# Platform-specific imports for file locking
if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

from claude_swap.exceptions import LockError


class FileLock:
    """Cross-process file lock using platform-specific APIs."""

    def __init__(self, lock_path: Path):
        self.lock_path = lock_path
        self._lock_file: IO | None = None
        self._locked = False

    def acquire(self, timeout: float = 10.0) -> bool:
        """Acquire exclusive lock with timeout.

        Args:
            timeout: Maximum seconds to wait for lock.

        Returns:
            True if lock acquired, False if timeout.
        """
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_file = open(self.lock_path, "w")

        start = time.monotonic()
        while True:
            try:
                if sys.platform == "win32":
                    # Windows: use msvcrt for file locking
                    msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    # POSIX: use fcntl for file locking
                    fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._locked = True
                return True
            except (BlockingIOError, OSError):
                if time.monotonic() - start > timeout:
                    self._lock_file.close()
                    self._lock_file = None
                    return False
                time.sleep(0.1)

    def release(self) -> None:
        """Release the lock."""
        if self._lock_file and self._locked:
            if sys.platform == "win32":
                # Windows: unlock using msvcrt
                try:
                    msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass  # File may already be unlocked
            else:
                # POSIX: unlock using fcntl
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            self._lock_file.close()
            self._lock_file = None
            self._locked = False

    def __enter__(self) -> FileLock:
        if not self.acquire():
            raise LockError("Failed to acquire lock - another instance may be running")
        return self

    def __exit__(self, *args) -> None:
        self.release()
