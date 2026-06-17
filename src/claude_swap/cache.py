"""Simple file-based cache utilities for claude-swap."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from claude_swap.paths import get_backup_root

CACHE_DIR = get_backup_root() / "cache"

MISSING = object()

# The cross-process re-probe cadence for the messages-API headroom fallback.
# When the usage endpoint is 429-backed-off (so an idle account reports NO usage
# signal), the supervisor's strand/resume paths fall back to a billable messages
# probe (see ``oauth.probe_messages_headroom`` / ``registry.build_world``). The
# verdict is stamped (``_probed_at``) into the SAME shared ``usage.json`` slot as
# the 429 marker, so an account is re-probed AT MOST once per this many seconds
# across ALL supervisors — not every tick, and not the whole ``_MAX_USAGE_BACKOFF_S``
# backoff hour. This is the "intelligent re-probe cadence", deliberately short so
# a confirmed account is rediscovered quickly while still capping the billable rate.
PROBE_VERDICT_TTL_S = 120

# How old a last-known-good usage reading may be and still back an optimistic
# ``signal="stale"`` view (see ``merge_last_known`` / ``last_known_usage``).
# Comfortably exceeds the 1h max usage-endpoint backoff so a reading taken when a
# 429 first hit stays usable for the whole window; an idle account's usage drifts
# over hours, not minutes, so a reading this fresh is a sound optimistic estimate.
STALE_USAGE_MAX_AGE_S = 2 * 3600


def usage_backoff_active(entry, now: float | None = None) -> bool:
    """Whether a cached usage entry is a still-active 429 backoff marker.

    A failed usage fetch is cached as ``{"_unavailable": True, "retry_until": <epoch>}``
    (the epoch is set from the server's ``Retry-After`` on a 429). While
    ``retry_until`` is in the future the account must not be refetched — the usage
    endpoint asked the whole client to back off. Returns ``False`` for real usage
    data, plain ``{"_unavailable": True}`` markers, and elapsed backoffs.
    """
    if not (isinstance(entry, dict) and entry.get("_unavailable")):
        return False
    until = entry.get("retry_until")
    if not isinstance(until, (int, float)):
        return False
    return until > (now if now is not None else time.time())


def probe_recent(entry, now: float | None = None) -> bool:
    """Whether a cached entry was probe-stamped within :data:`PROBE_VERDICT_TTL_S`.

    Throttles the messages-API headroom re-probe CROSS-PROCESS: while a verdict's
    ``_probed_at`` is within the cadence, no supervisor re-probes (they all reuse
    the cached verdict — :func:`probe_ok` distinguishes a confirmed-OK verdict from
    a no-signal one). Returns ``False`` for non-dicts and entries with no
    ``_probed_at`` stamp (a plain usage entry or an un-probed failure marker).
    """
    if not isinstance(entry, dict):
        return False
    at = entry.get("_probed_at")
    if not isinstance(at, (int, float)):
        return False
    return (now if now is not None else time.time()) - at < PROBE_VERDICT_TTL_S


def probe_ok(entry) -> bool:
    """Whether a cached entry is a confirmed-OK headroom-probe verdict.

    A ``True`` messages probe is cached as ``{"_probe_ok": True, "_probed_at": ...}``
    (it carries no usage windows). This lets :func:`registry.build_world`
    reconstruct a probe :class:`~claude_swap.balancer.AccountView` from a fresh
    cached verdict WITHOUT a redundant re-probe. ``False`` for everything else
    (real usage, no-signal/429 markers, non-dicts).
    """
    return isinstance(entry, dict) and entry.get("_probe_ok") is True


def is_real_usage(entry) -> bool:
    """Whether a cached entry holds REAL usage windows (not a failure/probe marker).

    Real usage is a dict carrying the usage-API result; a failure marker carries
    ``_unavailable`` and a probe verdict carries ``_probe_ok``. Used to decide when a
    failure marker should snapshot the prior reading as last-known-good.
    """
    return (
        isinstance(entry, dict)
        and not entry.get("_unavailable")
        and not entry.get("_probe_ok")
    )


def merge_last_known(marker: dict, prev, now: float | None = None) -> dict:
    """Carry a last-known-good usage snapshot onto a failure ``marker``.

    Optimistic-headroom support: a failure/backoff marker remembers the most recent
    REAL usage reading so :func:`registry.build_world` can keep an idle account that
    was healthy-when-last-seen as a (no-credit) migration target through the backoff
    window, instead of treating its unknown usage as zero headroom. If ``prev`` is
    itself real usage, snapshot it now; if ``prev`` is an earlier marker already
    carrying ``_last_known`` (a chain of markers across the window), inherit it.
    Returns ``marker`` (mutated in place).
    """
    if not isinstance(marker, dict):
        return marker
    if is_real_usage(prev):
        marker["_last_known"] = prev
        marker["_last_known_at"] = now if now is not None else time.time()
    elif isinstance(prev, dict) and isinstance(prev.get("_last_known"), dict):
        marker["_last_known"] = prev["_last_known"]
        at = prev.get("_last_known_at")
        if isinstance(at, (int, float)):
            marker["_last_known_at"] = at
    return marker


def last_known_usage(entry, max_age: float, now: float | None = None):
    """A still-recent last-known-good usage snapshot carried on a failure marker.

    Returns the snapshotted usage dict when ``entry`` carries ``_last_known`` and its
    ``_last_known_at`` is within ``max_age`` seconds; otherwise ``None``. Lets
    :func:`registry.build_world` synthesize an optimistic ``signal="stale"`` view for
    an account whose usage endpoint is 429-backed-off but which was below the safety
    line when last read — so a stranded session can migrate onto it without spending
    a probe credit, accepting that it may turn out capped (then it recovers again).
    """
    if not isinstance(entry, dict):
        return None
    lk = entry.get("_last_known")
    at = entry.get("_last_known_at")
    if not (isinstance(lk, dict) and isinstance(at, (int, float))):
        return None
    if (now if now is not None else time.time()) - at >= max_age:
        return None
    return lk


def read_cache(path: Path, ttl: float, default=MISSING):
    """Read cached JSON data if the file exists and is within TTL.

    Returns the stored 'data' value, or *default* if missing/expired/invalid.
    When *default* is not provided, returns the ``MISSING`` sentinel so
    callers can distinguish "no cache" from a cached ``None`` value.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - raw["timestamp"] < ttl:
            return raw["data"]
    except (
        OSError,
        json.JSONDecodeError,
        UnicodeDecodeError,
        KeyError,
        TypeError,
    ):
        pass
    return default


def write_cache(path: Path, data) -> None:
    """Write data to a cache file with a timestamp.

    Commits via a temp file + ``os.replace`` (atomic rename) so a concurrent
    reader — another cswap process, a statusline, or the dashboard's idle-usage
    refresher thread — never observes a half-written file (it sees the old
    contents or the new ones, whole).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"timestamp": time.time(), "data": data})
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    except OSError:
        # Best-effort cache: clean up the temp file and move on rather than
        # raising into the caller (usage rendering, balancing, etc.).
        try:
            os.unlink(tmp)
        except OSError:
            pass
