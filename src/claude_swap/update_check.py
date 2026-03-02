"""Check PyPI for newer versions of claude-swap."""

from __future__ import annotations

import json
import os
import time
import urllib.request

CACHE_PATH = os.path.join(os.path.expanduser("~"), ".claude-swap-backup", "update_check.json")
CACHE_TTL = 72 * 3600  # 72 hours
PYPI_URL = "https://pypi.org/pypi/claude-swap/json"


def _parse_version(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def check_for_update(current_version: str) -> str | None:
    """Return a notification string if a newer version exists, else None."""
    try:
        cached = False
        latest_version = None

        # Try reading cache
        if os.path.exists(CACHE_PATH):
            with open(CACHE_PATH) as f:
                cache = json.load(f)
            if time.time() - cache["last_checked"] < CACHE_TTL:
                latest_version = cache.get("latest_version")
                cached = True

        # Fetch from PyPI if cache miss or stale
        if not cached:
            try:
                req = urllib.request.Request(PYPI_URL)
                with urllib.request.urlopen(req, timeout=2) as resp:
                    data = json.loads(resp.read().decode())
                latest_version = data["info"]["version"]
            except Exception:
                latest_version = None

            # Write cache regardless of success/failure
            os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
            with open(CACHE_PATH, "w") as f:
                json.dump({"last_checked": time.time(), "latest_version": latest_version}, f)

        if latest_version and _parse_version(latest_version) > _parse_version(current_version):
            return (
                f"A newer version of claude-swap is available ({latest_version}). "
                f"You are using {current_version}. Consider upgrading!"
            )
        return None
    except Exception:
        return None
