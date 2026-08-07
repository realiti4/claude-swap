"""Filesystem primitives with no claude_swap dependencies.

Deliberately a leaf module: the atomic-write helpers here are needed by
settings, credentials, mappings and session, which sit on both sides of
the paths/models/usage_store import cycle.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# Windows error codes that usually mean "someone else has the file open right
# now": ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION and ERROR_LOCK_VIOLATION.
# AV/indexer contention clears within milliseconds; ERROR_ACCESS_DENIED can
# also be a persistent condition (ACLs, read-only attribute), which still
# surfaces once the bounded retries are exhausted.
_TRANSIENT_WIN_ERRORS = frozenset({5, 32, 33})


def read_text_with_retry(
    path: os.PathLike | str,
    *,
    attempts: int = 10,
    initial_delay: float = 0.002,
) -> str:
    """``Path.read_text``, retried past the same transient Windows failures.

    The read side of :func:`replace_with_retry`, and it needs the same
    treatment for the same reason: ``_write_json`` publishes the roster by
    renaming onto ``sequence.json``, so the file is freshly-modified exactly
    when AV/the indexer opens it opportunistically, and a reader arriving in
    that window gets ERROR_SHARING_VIOLATION as a ``PermissionError``.

    That window matters more on the read side than the write side, because a
    strict roster read raises ``ConfigError`` at ~59 call sites — including
    read-only ones (``list_accounts``, ``_account_kind``) and
    ``SwitchTransaction.rollback``, where turning a recoverable switch failure
    into a failed ROLLBACK is the worst outcome available.

    POSIX is untouched: there is no such window, and an ``EACCES`` there is a
    genuine, persistent permission problem that must surface at once rather
    than after a ~0.75 s stall.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    delay = initial_delay
    for attempt in range(attempts):
        try:
            return Path(path).read_text(encoding="utf-8")
        except OSError as e:
            transient = (
                sys.platform == "win32"
                and getattr(e, "winerror", None) in _TRANSIENT_WIN_ERRORS
            )
            if not transient or attempt == attempts - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.25)
    raise AssertionError("unreachable")  # pragma: no cover - loop always exits


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

    Only the contention codes are retried; other errors (missing source,
    cross-device link) surface on the first attempt. A *persistent* access
    denial (ACLs, read-only target) matches the retried codes and so raises
    only after the attempt budget — a ~0.75 s delay in the worst case.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
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
