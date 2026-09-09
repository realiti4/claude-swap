"""Authoritative rate-limit reset overrides — opt-in bridge for worker-observed resets.

cswap derives every window reset time from the ``/api/oauth/usage`` endpoint
(:func:`oauth.build_usage_result`). A poller has no other source, but the
*authoritative* current-window reset is the one Claude Code itself prints in its
rate-limit message ("resets 6:50pm") — a value only the worker actually running
Claude Code ever observes. When the usage endpoint reports a reset later than the
true current-window reset (observed 2026-08: once a window is fully spent the
endpoint can return the *next* window's reset), an all-accounts-exhausted auto
loop sleeps to that too-late time and idles past the real reset.

This module lets an external worker that DOES see the authoritative reset feed it
in through a small JSON file. The clamp is deliberately **one-directional**: an
authoritative reset only ever pulls a window's reset *earlier*, never later — so a
stale or wrong override can shorten an over-wait but can never extend a wait or
hide a real limit. Absent file / missing entry / unparsable value = exact
pre-existing behavior (fail-open).

The caller reads this file and passes the parsed dict to
:func:`oauth.try_fetch_usage_for_account` as ``authoritative_resets``; this
module owns only the (tolerant) reader and the pure clamp. File format::

    {
      "<account-email>": {"five_hour": "<iso8601>", "seven_day": "<iso8601>"},
      ...
    }

Any window key may be omitted; unknown keys are ignored.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

_logger = logging.getLogger("claude-swap")

# usage-dict window key -> override key (same names; explicit so a rename of one
# side can't silently desync the other).
_WINDOW_KEYS = (("five_hour", "five_hour"), ("seven_day", "seven_day"))


def read_authoritative_resets(path: str | None) -> dict[str, dict]:
    """Load the override file as ``{email: {window: iso}}``; ``{}`` on any failure.

    Fail-open by contract: a missing, unreadable, or malformed file must never
    break a usage fetch — it just means "no overrides this pass". Only the outer
    shape is validated here; per-window parsing/comparison happens in
    :func:`clamp_account_resets` so one bad entry can't discard the rest.
    """
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        _logger.debug("authoritative reset file unreadable (%s): %r", path, e)
        return {}
    if not isinstance(data, dict):
        _logger.debug("authoritative reset file is not an object: %s", path)
        return {}
    return {
        email: windows
        for email, windows in data.items()
        if isinstance(email, str) and isinstance(windows, dict)
    }


def _parse(iso: object) -> datetime | None:
    """Parse an ISO-8601 string to an aware UTC datetime, or None."""
    if not isinstance(iso, str) or not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def clamp_account_resets(
    usage: dict | None, overrides: dict | None, now: datetime | None = None
) -> dict | None:
    """Return ``usage`` with window resets pulled earlier by authoritative overrides.

    For each 5h/7d window present in ``usage``, if ``overrides`` carries an
    authoritative reset for that window that is in the future AND strictly
    earlier than the endpoint's ``resets_at`` (or the endpoint sent none),
    replace it. Never moves a reset later. Pure: returns the same object when
    nothing changes, else a shallow copy with the affected window dicts replaced.
    """
    if not isinstance(usage, dict) or not isinstance(overrides, dict):
        return usage
    now = now or datetime.now(timezone.utc)
    result: dict | None = None
    for win_key, ovr_key in _WINDOW_KEYS:
        window = usage.get(win_key)
        if not isinstance(window, dict):
            continue
        auth = _parse(overrides.get(ovr_key))
        if auth is None or auth <= now:
            continue  # no override, or already elapsed — nothing to clamp to
        current = _parse(window.get("resets_at"))
        if current is not None and current <= auth:
            continue  # endpoint value is already at/earlier than authoritative
        if result is None:
            result = dict(usage)
        new_window = dict(window)
        new_window["resets_at"] = auth.isoformat()
        # Stale countdown/clock would misrender until the next fetch; drop them so
        # oauth.fresh_reset_strings recomputes from the new resets_at at render.
        new_window.pop("countdown", None)
        new_window.pop("clock", None)
        result[win_key] = new_window
    return result if result is not None else usage
