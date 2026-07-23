"""Filesystem primitives with no claude_swap dependencies.

Deliberately a leaf module: the atomic-write helpers here are needed by
settings, credentials, mappings and session, which sit on both sides of
the paths/models/usage_store import cycle.
"""

from __future__ import annotations

import os
import sys
import time

# Windows error codes that mean "someone else has the file open right now",
# not "this operation is invalid": ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION
# and ERROR_LOCK_VIOLATION. All three clear on their own within milliseconds.
_TRANSIENT_WIN_ERRORS = frozenset({5, 32, 33})


def replace_with_retry(
    src: os.PathLike | str,
    dst: os.PathLike | str,
    *,
    attempts: int = 10,
    initial_delay: float = 0.002,
) -> None:
    """``os.replace``, retried past transient Windows sharing failures.

    POSIX ``rename`` is genuinely atomic and never fails this way, but on
    Windows antivirus (Defender) and the search indexer open freshly-created
    files opportunistically. A replace onto a just-written target then fails
    with ERROR_ACCESS_DENIED or ERROR_SHARING_VIOLATION for a few milliseconds
    — measured at ~44% of replaces into a scanned temp directory, which made
    credential and usage-store writes fail intermittently for real users.

    Only the transient codes are retried; a genuine error (missing source,
    cross-device link) surfaces on the first attempt.
    """
    delay = initial_delay
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except OSError as e:
            transient = (
                sys.platform == "win32"
                and getattr(e, "winerror", None) in _TRANSIENT_WIN_ERRORS
            )
            if not transient or attempt == attempts - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.25)
