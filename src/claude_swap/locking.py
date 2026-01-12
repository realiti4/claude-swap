"""File locking for concurrent access protection."""

from __future__ import annotations

import fcntl
import time
from pathlib import Path
from typing import IO

from claude_swap.exceptions import LockError


class FileLock:
    """Cross-process file lock using fcntl."""

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
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._locked = True
                return True
            except BlockingIOError:
                if time.monotonic() - start > timeout:
                    self._lock_file.close()
                    self._lock_file = None
                    return False
                time.sleep(0.1)

    def release(self) -> None:
        """Release the lock."""
        if self._lock_file and self._locked:
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
