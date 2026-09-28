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

from claude_swap.dirlock import directory_lock
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
# Note this is a PER-LOCK budget: claude_credentials_lock acquires two locks
# sequentially, so its worst case is ~2x this value.
DEFAULT_TIMEOUT_S = 9.0

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
    staleness: float = CONFIG_STALENESS_S,
):
    """Acquire a proper-lockfile-compatible directory lock.

    The mechanism lives in :func:`claude_swap.dirlock.directory_lock` -- it is
    a generic network-filesystem-safe mutex, not a Claude Code detail, and the
    Codex side needs the same primitive. This wrapper supplies the two things
    that ARE Claude Code details: the ``ClaudeCodeLockTimeout`` type its
    callers catch, and a ``timeout`` resolved at CALL time (not as a default
    argument) so tests can shorten ``DEFAULT_TIMEOUT_S`` by patching it.

    Raises:
        ClaudeCodeLockTimeout: The lock stayed held past ``timeout``.
    """
    if timeout is None:
        timeout = DEFAULT_TIMEOUT_S
    with directory_lock(
        lock_dir,
        timeout=timeout,
        staleness=staleness,
        # Read HERE, at call time, so patching claude_locks.TOUCH_INTERVAL_S
        # still reaches the toucher after the mechanism moved to dirlock.
        touch_interval=TOUCH_INTERVAL_S,
        timeout_error=ClaudeCodeLockTimeout,
    ):
        yield


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
