"""Check PyPI for newer versions of claude-swap."""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

from claude_swap.cache import CACHE_DIR, MISSING, read_cache, write_cache

CACHE_PATH = CACHE_DIR / "update_check.json"
CACHE_TTL = 24 * 3600  # 24 hours
PYPI_URL = "https://pypi.org/pypi/claude-swap/json"


def _parse_version(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def _detect_install_method() -> str | None:
    """Return 'uv', 'pipx', or None if we can't tell."""
    prefix = Path(sys.prefix)
    parts = tuple(p.lower() for p in prefix.parts)
    pairs = list(zip(parts, parts[1:]))

    if ("uv", "tools") in pairs:
        return "uv"
    if ("pipx", "venvs") in pairs:
        return "pipx"

    # Env-var override: only trust if sys.prefix is actually under it.
    for env_var, name in (("UV_TOOL_DIR", "uv"), ("PIPX_HOME", "pipx")):
        root = os.environ.get(env_var)
        if root:
            try:
                if prefix.is_relative_to(Path(root)):
                    return name
            except (ValueError, OSError):
                pass
    return None


def check_for_update(current_version: str) -> str | None:
    """Return a notification string if a newer version exists, else None."""
    try:
        latest_version = None

        # Try reading cache
        cached_data = read_cache(CACHE_PATH, CACHE_TTL)
        if cached_data is not MISSING:
            latest_version = cached_data
        else:
            # Fetch from PyPI
            try:
                req = urllib.request.Request(PYPI_URL)
                with urllib.request.urlopen(req, timeout=2) as resp:
                    data = json.loads(resp.read().decode())
                latest_version = data["info"]["version"]
            except Exception:
                latest_version = None

            # Write cache regardless of success/failure
            write_cache(CACHE_PATH, latest_version)

        if latest_version and _parse_version(latest_version) > _parse_version(current_version):
            hint = {
                "uv": "Run `uv tool upgrade claude-swap` to update.",
                "pipx": "Run `pipx upgrade claude-swap` to update.",
            }.get(_detect_install_method() or "", "Consider upgrading!")
            return (
                f"A newer version of claude-swap is available ({latest_version}). "
                f"You are using {current_version}. {hint}"
            )
        return None
    except Exception:
        return None
