"""Codex CLI rate-limit usage, read from Codex's own session logs.

Codex records its 5-hour and weekly rate-limit windows in the log it keeps for
every conversation. This module reads the latest such record so the menu bar
and ``cswap list`` can show Codex usage beside the Claude accounts. It opens
nothing but those logs, and keeps only the plan name and the window figures,
never any conversation text.

Why the log and not Codex itself: reading a file touches no credential, makes
no network request and runs no model. The cost is that the figures are as of
the last Codex turn on this machine, which is when they change, except for use
from another device. They belong to whichever login Codex had for that turn.

Format, measured on Codex CLI 0.171.0-alpha.13 (2026-11-21, macOS):

* Logs are ``$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl``, one JSON
  object per line; ``CODEX_HOME`` defaults to ``~/.codex``. A resumed session
  keeps appending to its original file, so a file can be newer than its day
  directory (32 of 299 files, by up to four days).
* After each turn Codex appends a line of this shape (trimmed; the values are
  from one real record)::

      {"timestamp": "2026-11-21T14:29:57.608Z", "type": "event_msg",
       "payload": {"type": "token_count", "info": {...},
                   "rate_limits": {"limit_id": "codex",
                       "primary": {"used_percent": 1.0, "window_minutes": 300,
                                   "resets_at": 1795289207},
                       "secondary": {"used_percent": 0.0, "window_minutes": 10080,
                                     "resets_at": 1795876007},
                       "plan_type": "plus", ...}}}

  ``resets_at`` is Unix seconds. Of 395 such lines in the 25 newest logs, 393
  were ``limit_id`` "codex" with those two windows; two were ``limit_id``
  "premium" with no windows at all, and are ignored.
* Windows are told apart by ``window_minutes`` (300 is the 5-hour window, 10080
  the weekly one), not by their position. A window whose reset time has passed
  is shown as 0 % used with no reset time, because no newer reading exists on
  this machine; use from another device is not visible.
* Logs are large (the biggest here is 118 MB; 299 files add up to 568 MB). The
  reader looks at the ``_MAX_DAY_DIRS`` newest day directories and at most
  ``_MAX_FILES`` files in them, newest first, stops after ``_MAX_READINGS`` that
  hold a reading, and reads each backwards from the end for at most
  ``_SCAN_CAP`` bytes. A result is cached per file until that file changes, so
  a once-a-minute refresh costs a few directory listings.

Every failure reads as "no reading": the callers show nothing, and nothing here
raises.
"""

from __future__ import annotations

import itertools
import json
import logging
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from claude_swap import oauth

_log = logging.getLogger("claude-swap")

# Only the newest week of day directories is looked at: a session resumed after
# longer than that holds figures already past their weekly reset.
_MAX_DAY_DIRS = 7
_MAX_FILES = 200  # runs refused at the usage limit write small logs with no reading; look past a long run of them
_MAX_READINGS = 3
_CHUNK = 256 * 1024
_SCAN_CAP = 4 * 1024 * 1024

_WINDOW_KEYS = {300: "five_hour", 10080: "seven_day"}  # window_minutes -> usage key
_MAX_EPOCH = 4_102_444_800  # 2100-01-01: a time beyond this is not a real one
_MAX_PLAN_LEN = 40  # longer, or not printable, is not a plan name


@dataclass(frozen=True)
class CodexUsage:
    """One reading of Codex's rate-limit windows.

    ``windows`` maps ``five_hour`` / ``seven_day`` to ``(pct, resets_at)``, the
    reset a Unix timestamp or None when the log did not carry one. ``read_at``
    is when Codex wrote the reading.
    """

    plan: str | None
    windows: dict[str, tuple[float, float | None]]
    read_at: float

    @property
    def plan_label(self) -> str | None:
        """``"Plus"`` for ``plus``: the plan name as Codex writes it, capitalised."""
        return self.plan.replace("_", " ").title() if self.plan else None

    def usage(self, now: float | None = None) -> dict:
        """The windows in the shape the Claude usage readings have.

        ``{"five_hour": {"pct", "resets_at"}, ...}``, so the existing row
        formatters apply. A window whose reset has passed since the reading is
        shown as 0 % used with no reset time.
        """
        now = time.time() if now is None else now
        out: dict[str, dict] = {}
        for key, (pct, resets_at) in self.windows.items():
            if resets_at is None:
                out[key] = {"pct": pct}
            elif resets_at <= now:
                out[key] = {"pct": 0.0}
            else:
                reset = datetime.fromtimestamp(resets_at, tz=timezone.utc)
                out[key] = {"pct": pct, "resets_at": reset.isoformat()}
        return out

    def read_clock(self) -> str:
        """When the reading was written, in local time ("16:29", or "Nov 20 16:29")."""
        return oauth.reset_clock_string(
            datetime.fromtimestamp(self.read_at, tz=timezone.utc),
            datetime.now(timezone.utc),
        )


def codex_home() -> Path:
    """Codex's data directory: ``$CODEX_HOME``, else ``~/.codex``."""
    env = os.environ.get("CODEX_HOME")
    return Path(env).expanduser() if env else Path.home() / ".codex"


def _number(value: object) -> float | None:
    """``value`` as a float when it is a finite number (a bool is not one)."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except OverflowError:  # an int beyond float range
        return None
    return number if math.isfinite(number) else None


def _parse(line: bytes) -> CodexUsage | None:
    """The reading in one log line, or None if it carries none.

    A record without a usable timestamp is not a reading: nothing could say
    when it was taken.
    """
    try:
        event = json.loads(line)
    except ValueError:
        return None  # torn or foreign line
    payload = event.get("payload") if isinstance(event, dict) else None
    if not isinstance(payload, dict) or payload.get("type") != "token_count":
        return None
    limits = payload.get("rate_limits")
    if not isinstance(limits, dict) or limits.get("limit_id") not in (None, "codex"):
        return None
    windows: dict[str, tuple[float, float | None]] = {}
    for name in ("primary", "secondary"):
        window = limits.get(name)
        if not isinstance(window, dict):
            continue
        minutes = _number(window.get("window_minutes"))
        key = _WINDOW_KEYS.get(minutes) if minutes is not None else None
        pct = _number(window.get("used_percent"))
        if key is None or pct is None:
            continue
        resets_at = _number(window.get("resets_at"))
        if resets_at is not None and not 0 < resets_at < _MAX_EPOCH:
            resets_at = None
        windows[key] = (pct, resets_at)
    if not windows:
        return None
    try:
        read_at = datetime.fromisoformat(event["timestamp"]).timestamp()
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return None
    if not 0 < read_at < _MAX_EPOCH:
        return None
    plan = limits.get("plan_type")
    if not isinstance(plan, str) or not plan.isprintable() or len(plan) > _MAX_PLAN_LEN:
        plan = None
    return CodexUsage(plan, windows, read_at)


def _scan(path: Path, size: int) -> CodexUsage | None:
    """The last reading in the first ``size`` bytes of a log, read from the end.

    Works backwards in ``_CHUNK`` blocks and gives up after ``_SCAN_CAP``
    bytes. A block's first line is held back until the block before it has
    been read, since it may be cut in half.
    """
    scanned = 0
    pos = size
    carry = b""
    with open(path, "rb") as f:
        while pos > 0 and scanned < _SCAN_CAP:
            step = min(_CHUNK, pos)
            pos -= step
            scanned += step
            f.seek(pos)
            lines = (f.read(step) + carry).split(b"\n")
            carry = lines[0] if pos > 0 else b""
            for line in reversed(lines[1:] if pos > 0 else lines):
                if b'"rate_limits"' in line:
                    reading = _parse(line)
                    if reading is not None:
                        return reading
    return None


def _digit_dirs(parent: Path) -> list[Path]:
    """Subdirectories named by digits, newest (highest) first."""
    try:
        names = [p for p in parent.iterdir() if p.name.isdigit()]
    except OSError:
        return []
    return sorted(names, key=lambda p: p.name, reverse=True)


def _newest_logs(root: Path) -> list[tuple[Path, int, int]]:
    """``(path, mtime_ns, size)`` of the newest logs, most recently written first."""
    days = (
        day
        for year in _digit_dirs(root)
        for month in _digit_dirs(year)
        for day in _digit_dirs(month)
    )
    logs = []
    for day in itertools.islice(days, _MAX_DAY_DIRS):
        try:
            for path in day.glob("rollout-*.jsonl"):
                st = path.stat()
                if st.st_size:
                    logs.append((path, st.st_mtime_ns, st.st_size))
        except OSError:
            continue
    logs.sort(key=lambda log: log[1], reverse=True)
    return logs[:_MAX_FILES]


# (path, mtime_ns, size) -> that log's last reading (None: it has none). Rebuilt
# on every read from the logs looked at, so it cannot grow.
_cache: dict[tuple[str, int, int], CodexUsage | None] = {}
_MISSING = object()


def read() -> CodexUsage | None:
    """The newest Codex rate-limit reading on this machine, or None.

    None covers every way of having nothing to show: no Codex, no logs, no
    record in them, an unreadable file.
    """
    global _cache
    cache = _cache  # one snapshot: another thread may swap the global meanwhile
    try:
        seen: dict[tuple[str, int, int], CodexUsage | None] = {}
        readings = []
        for path, mtime_ns, size in _newest_logs(codex_home() / "sessions"):
            key = (str(path), mtime_ns, size)
            reading = cache.get(key, _MISSING)
            if reading is _MISSING:
                try:
                    reading = _scan(path, size)
                except OSError:
                    continue  # not remembered: the next read tries again
            seen[key] = reading
            if reading is not None:
                readings.append(reading)
                if len(readings) >= _MAX_READINGS:
                    break
        _cache = seen
        # The newest record wins, whichever log it is in: two sessions can be
        # writing at once, and the limits belong to the account, not a session.
        return max(readings, key=lambda r: r.read_at, default=None)
    except Exception:
        _log.debug("could not read Codex usage", exc_info=True)
        return None
