"""Cooperate with Claude Code's own advisory locks while mutating its files.

Claude Code guards its OAuth token refresh with the npm ``proper-lockfile``
package, and its ``~/.claude.json`` writes with the same mechanism on the
config file. The protocol (verified against the 2.1.218 bundle):

- The lock artifact is a **directory**; ``mkdir`` atomicity is the mutex.
- The refresh path takes **two** locks, in order: the primary
  ``<config-home>/.oauth_refresh.lock``, then the legacy
  ``<config-home>.lock`` (``~/.claude.lock``) kept for compatibility with
  external tools. Both run ``stale: 60000, update: 5000`` — a credential
  lock is stale only past **60s**, and live holders touch every 5s. On a
  contended legacy lock Claude Code releases the primary and retries.
- The config lock (``~/.claude.json.lock``) keeps the older defaults:
  stale after 10s, touched every 5s.
- Claude Code retries a held credentials lock 5 times with 1-2s jittered
  sleeps before giving up, so briefly holding it is fully cooperative.

Holding these locks while swapping credentials closes the one real race with a
running Claude Code: its refresh reads credentials, refreshes over the network,
and saves — all under both credential locks — so a swap landing inside that
window would be overwritten by the refreshed old-account token (and the
just-taken backup would keep a pre-rotation refresh token). Under the lock,
Claude Code's own double-checked re-read sees the swapped (non-expired)
credential and aborts the refresh instead.

References (claude-code 2.1.218 bundle): the ``uKi`` lock-options helper
(``lockfilePath: join(dir, ".oauth_refresh.lock"), stale: 60000, update:
5000``) and ``CKi`` (dual acquisition, legacy released-on-contention with
``tengu_oauth_refresh_legacy_lock_contended`` telemetry).
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from claude_swap.exceptions import ClaudeCodeLockTimeout
from claude_swap.paths import get_claude_config_home, get_global_config_path

# Claude Code's credential-refresh locks run ``stale: 60000, update: 5000``
# (2.1.218 ``uKi``): a lock younger than 60s belongs to a live holder and
# must never be stolen — the holder's toucher may stall well past 10s
# (suspend, blocked event loop) while it still legitimately owns the lock.
CREDENTIALS_STALENESS_S = 60.0
# The config lock (~/.claude.json.lock) keeps the older proper-lockfile
# defaults: stale after 10s, touched every 5s.
CONFIG_STALENESS_S = 10.0
# Claude Code's secure-storage write lock (2.1.278): proper-lockfile on
# ``<config-home>/.storage-write`` with ``realpath: false, stale: 15000`` —
# so the artifact is the directory ``<config-home>/.storage-write.lock``.
# Every storage ``update()``/``mutate()`` (credential saves, MCP OAuth)
# runs under it, which is what a writer of a managed profile's keychain
# item + plaintext pair must exclude.
STORAGE_WRITE_STALENESS_S = 15.0
# We touch a little faster than CC's 5s for margin.
TOUCH_INTERVAL_S = 3.0
# Claude Code holds the credentials lock for one token-endpoint round trip
# (sub-second to a few seconds); its config lock for a local RMW. 9s of
# bounded waiting comfortably outlasts both without stalling the CLI forever.
# Note this is a PER-LOCK budget: claude_credentials_lock acquires two locks
# sequentially, so its worst case is ~2x this value.
DEFAULT_TIMEOUT_S = 9.0

_logger = logging.getLogger("claude-swap")


def credentials_lock_dir() -> Path:
    """Legacy credential lock (``~/.claude.lock``) — CC still takes it for
    compatibility; external exclusion today rests on this one."""
    home = get_claude_config_home()
    return home.parent / (home.name + ".lock")


def oauth_refresh_lock_dir(config_dir: Path | None = None) -> Path:
    """Claude Code's primary OAuth refresh lock
    (``<config-home>/.oauth_refresh.lock``, 2.1.218+).

    ``config_dir`` names a specific profile (a managed session's
    ``CLAUDE_CONFIG_DIR``); ``None`` is the invoking environment's.
    """
    base = config_dir if config_dir is not None else get_claude_config_home()
    return base / ".oauth_refresh.lock"


def storage_write_lock_dir(config_dir: Path | None = None) -> Path:
    """Claude Code's secure-storage write lock (``<cfg>/.storage-write.lock``)."""
    base = config_dir if config_dir is not None else get_claude_config_home()
    return base / ".storage-write.lock"


def claude_json_lock_dir(config_dir: Path) -> Path:
    """The ``.claude.json`` lock of an explicit profile. With
    ``CLAUDE_CONFIG_DIR`` set, Claude keeps ``.claude.json`` inside the
    config dir, so its lock is ``<cfg>/.claude.json.lock``.

    Where this lock sits relative to ``.storage-write.lock`` is inferred,
    not read out of a bundle the way the refresh/storage pair above is:
    nothing observed says which of the two Claude Code takes first.
    Callers that need both nest this one innermost, because a config
    splice accompanies a credential write. If Claude nests them the other
    way the two orders invert, and the cost is bounded by the acquire
    timeout: both sides give up and retry rather than wait on each other.
    """
    return config_dir / ".claude.json.lock"


def config_lock_dir() -> Path:
    """Lock directory guarding the global config file (``~/.claude.json.lock``)."""
    path = get_global_config_path()
    return path.parent / (path.name + ".lock")


@contextmanager
def proper_lockfile(
    lock_dir: Path,
    *,
    timeout: float | None = None,
    staleness: float = CONFIG_STALENESS_S,
):
    """Acquire a proper-lockfile-compatible directory lock.

    Blocks up to ``timeout`` seconds (default ``DEFAULT_TIMEOUT_S``, resolved
    at call time so tests can shorten it), taking over locks whose mtime is
    older than ``staleness``, touches the directory mtime while held so other
    holders don't deem us stale, and removes it on exit.

    Raises:
        ClaudeCodeLockTimeout: The lock stayed held past ``timeout``.
    """
    if timeout is None:
        timeout = DEFAULT_TIMEOUT_S
    lock_dir.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    while True:
        try:
            os.mkdir(lock_dir)
            break
        except FileExistsError:
            pass
        if time.monotonic() - start > timeout:
            raise ClaudeCodeLockTimeout(
                f"Could not acquire {lock_dir.name} — Claude Code appears "
                "to be refreshing credentials. Retry in a few seconds."
            )
        try:
            held_mtime = os.stat(lock_dir).st_mtime
        except FileNotFoundError:
            continue  # holder released between mkdir and stat; retry now
        if time.time() - held_mtime > staleness:
            # Dead holder per the protocol: remove and retake. Losing the
            # rmdir/mkdir race to another waiter just means looping again.
            try:
                os.rmdir(lock_dir)
            except OSError:
                time.sleep(0.05)  # can't remove it either; don't spin hot
            continue
        time.sleep(0.25 + random.random() * 0.25)

    stop_touching = threading.Event()

    def _touch() -> None:
        while not stop_touching.wait(TOUCH_INTERVAL_S):
            try:
                os.utime(lock_dir)
            except OSError:
                return  # lock stolen/removed; nothing left to keep alive

    toucher = threading.Thread(target=_touch, daemon=True)
    toucher.start()
    try:
        yield
    finally:
        stop_touching.set()
        toucher.join(timeout=1.0)
        try:
            os.rmdir(lock_dir)
        except FileNotFoundError:
            _logger.warning(
                "Lock %s vanished while held (taken over as stale?)", lock_dir
            )
        except OSError as e:
            _logger.warning("Failed to release lock %s: %s", lock_dir, e)


@contextmanager
def claude_credentials_lock(*, timeout: float | None = None):
    """Hold Claude Code's credential-refresh locks, in CC's own order.

    2.1.218 takes ``<config-home>/.oauth_refresh.lock`` first, then the
    legacy ``~/.claude.lock``; on legacy contention it releases the primary
    before retrying. Mirroring both the pair and the order means a waiting
    cswap and a waiting Claude Code can never deadlock against each other,
    and exclusion holds even after CC drops the legacy lock. Both use CC's
    60s staleness — never steal a lock a live CC may still hold.
    """
    with (
        proper_lockfile(
            oauth_refresh_lock_dir(),
            timeout=timeout,
            staleness=CREDENTIALS_STALENESS_S,
        ),
        proper_lockfile(
            credentials_lock_dir(),
            timeout=timeout,
            staleness=CREDENTIALS_STALENESS_S,
        ),
    ):
        yield


@contextmanager
def claude_config_lock(*, timeout: float | None = None):
    """Hold Claude Code's global-config write lock (``~/.claude.json.lock``)."""
    with proper_lockfile(config_lock_dir(), timeout=timeout):
        yield


@contextmanager
def session_credential_locks(config_dir: Path, *, timeout: float | None = None):
    """Both locks Claude Code takes around a credential write in one profile.

    Order mirrors Claude's own nesting (a refresh holds the refresh lock and
    saves through storage, which takes the storage-write lock inside it), so
    a waiting cswap and a waiting Claude can never deadlock. Managed
    profiles hold no refresh token, so Claude never takes the refresh lock
    there today; holding it anyway keeps a future refresh from interleaving.
    """
    with proper_lockfile(
        oauth_refresh_lock_dir(config_dir),
        timeout=timeout,
        staleness=CREDENTIALS_STALENESS_S,
    ):
        with proper_lockfile(
            storage_write_lock_dir(config_dir),
            timeout=timeout,
            staleness=STORAGE_WRITE_STALENESS_S,
        ):
            yield
