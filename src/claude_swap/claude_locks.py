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
# We touch a little faster than CC's 5s for margin.
TOUCH_INTERVAL_S = 3.0
# Claude Code holds the credentials lock for one token-endpoint round trip
# (sub-second to a few seconds); its config lock for a local RMW. 9s of
# bounded waiting comfortably outlasts both without stalling the CLI forever.
DEFAULT_TIMEOUT_S = 9.0

# Backwards-compatible alias: the old single staleness constant. Kept because
# tests and external callers may monkeypatch it; the config path still uses it.
STALENESS_S = CONFIG_STALENESS_S

_logger = logging.getLogger("claude-swap")


def credentials_lock_dir() -> Path:
    """Legacy credential lock (``~/.claude.lock``) — CC still takes it for
    compatibility; external exclusion today rests on this one."""
    home = get_claude_config_home()
    return home.parent / (home.name + ".lock")


def oauth_refresh_lock_dir() -> Path:
    """Claude Code's primary OAuth refresh lock
    (``<config-home>/.oauth_refresh.lock``, 2.1.218+)."""
    return get_claude_config_home() / ".oauth_refresh.lock"


def config_lock_dir() -> Path:
    """Lock directory guarding the global config file (``~/.claude.json.lock``)."""
    path = get_global_config_path()
    return path.parent / (path.name + ".lock")


@contextmanager
def proper_lockfile(
    lock_dir: Path,
    *,
    timeout: float | None = None,
    staleness: float = STALENESS_S,
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
    with proper_lockfile(
        oauth_refresh_lock_dir(),
        timeout=timeout,
        staleness=CREDENTIALS_STALENESS_S,
    ):
        with proper_lockfile(
            credentials_lock_dir(),
            timeout=timeout,
            staleness=CREDENTIALS_STALENESS_S,
        ):
            yield


@contextmanager
def claude_config_lock(*, timeout: float | None = None):
    """Hold Claude Code's global-config write lock (``~/.claude.json.lock``)."""
    with proper_lockfile(config_lock_dir(), timeout=timeout):
        yield
