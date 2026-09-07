"""Path resolution for Codex config and for cswap's own Codex store.

Two distinct roots, deliberately kept apart:

- ``~/.codex`` (or ``$CODEX_HOME``) is the *codex CLI's* directory. cswap reads
  and writes exactly one file in it, ``auth.json``, and reads codex-auth's
  ``accounts/registry.json`` once at import time. Nothing else in there is ours.
- ``<cswap backup root>/codex/`` is cswap's own store, a sibling of the existing
  Claude ``configs/``/``credentials/``. Keeping it under the same root means the
  existing purge, backup-root migration and the tests' real-store write guard all
  cover Codex data for free.

``CODEX_HOME`` mirrors the codex CLI's own env var; resolving it the same way is
what makes cswap and codex agree on which file is live.
"""

from __future__ import annotations

import os
from pathlib import Path

from claude_swap.paths import get_backup_root


def get_codex_home() -> Path:
    """Return the Codex config home (``CODEX_HOME`` or ``~/.codex``)."""
    env = os.environ.get("CODEX_HOME")
    if env:
        return Path(env)
    return Path.home() / ".codex"


def get_live_auth_path() -> Path:
    """Return the live ``auth.json`` the codex CLI reads and writes."""
    return get_codex_home() / "auth.json"


def get_codex_auth_registry_path() -> Path:
    """Return codex-auth's registry, read once during the one-time import."""
    return get_codex_home() / "accounts" / "registry.json"


def get_codex_auth_accounts_dir() -> Path:
    """Return codex-auth's snapshot directory (read-only for cswap)."""
    return get_codex_home() / "accounts"


def get_codex_store_root() -> Path:
    """Return cswap's own Codex store root."""
    return get_backup_root() / "codex"


def get_codex_sequence_path() -> Path:
    """Return the slot registry (slot -> account_key and metadata)."""
    return get_codex_store_root() / "sequence.json"


def get_codex_credentials_dir() -> Path:
    """Return the on-disk snapshot directory (non-macOS, and macOS fallback)."""
    return get_codex_store_root() / "credentials"


def get_codex_cache_dir() -> Path:
    """Return the usage-cache directory backing ``UsageStore``."""
    return get_codex_store_root() / "cache"


def get_codex_lock_path() -> Path:
    """Return cswap's Codex lock file.

    Separate from the Claude lock on purpose: a Codex switch and a Claude
    switch touch disjoint files and must never block each other.
    """
    return get_codex_store_root() / ".lock"
