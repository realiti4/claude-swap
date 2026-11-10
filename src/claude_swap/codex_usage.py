"""Codex (OpenAI ChatGPT login) rate-limit usage.

Contract shared by the codex account commands; the fetch is filled in
separately.
"""

from __future__ import annotations

from typing import NamedTuple


class Window(NamedTuple):
    label: str  # "5h", "weekly"
    used_percent: float
    resets_at: str | None  # ISO 8601 UTC, the shape oauth.format_reset reads


def fetch_usage(access_token: str, account_id: str | None, timeout: float = 10) -> list[Window]:
    """Rate-limit windows for a ChatGPT login; [] when unknown. Never raises."""
    return []
