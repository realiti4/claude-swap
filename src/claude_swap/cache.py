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
