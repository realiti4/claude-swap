"""Directory → account mappings for `cswap run` auto-resolution.

Maps a normalized absolute directory path to a stored account identity
(email + organizationUuid). `cswap run` with no account argument resolves the
current working directory to the nearest mapped ancestor and launches that
account in session mode.

Persisted to ``<backup_dir>/mappings.json``. Identity is stored as the stable
(email, organizationUuid) composite rather than the slot number, since slot
numbers are reused when accounts are removed and re-added. This module is
deliberately decoupled from ``switcher`` (it never imports it): callers resolve
an entry's (email, org) to a live slot via the switcher themselves.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from claude_swap.models import PROVIDER_CLAUDE, get_timestamp, normalize_provider

SCHEMA_VERSION = 2


def normalize_path(p: str | Path) -> str:
    """Normalize a path to a stable, comparable mapping key.

    Expands ``~``, makes the path absolute, resolves symlinks, and applies
    ``os.path.normcase`` (case-folding on Windows, a no-op on POSIX) so the
    same directory always produces the same key regardless of how it was typed.
    """
    resolved = Path(p).expanduser().resolve()
    return os.path.normcase(str(resolved))


class MappingStore:
    """Reads and writes ``<backup_dir>/mappings.json``."""

    def __init__(self, backup_dir: Path):
        self.path = Path(backup_dir) / "mappings.json"

    def load(self) -> dict[str, dict]:
        """Return the normalized-path → entry map (empty on missing/corrupt)."""
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            return {}
        if not isinstance(data, dict):
            return {}
        mappings = data.get("mappings", {})
        return mappings if isinstance(mappings, dict) else {}

    def all(self) -> dict[str, dict]:
        """Public alias for the full mapping table."""
        return self.load()

    def get(self, path: str | Path) -> dict | None:
        """Exact-match lookup for a normalized path (no ancestor walk)."""
        return self.load().get(normalize_path(path))

    def set(
        self,
        path: str | Path,
        email: str,
        org_uuid: str,
        provider: str = PROVIDER_CLAUDE,
    ) -> None:
        """Upsert a mapping for ``path`` and persist atomically.

        ``provider`` defaults to ``claude`` so existing call sites (and
        mappings written before this field existed) are unaffected.
        """
        mappings = self.load()
        mappings[normalize_path(path)] = {
            "email": email,
            "organizationUuid": org_uuid or "",
            "provider": provider,
            "added": get_timestamp(),
        }
        self._write(mappings)

    def remove(self, path: str | Path) -> bool:
        """Delete the mapping for ``path``; return whether one was removed."""
        mappings = self.load()
        if mappings.pop(normalize_path(path), None) is None:
            return False
        self._write(mappings)
        return True

    def prune_account(
        self, email: str, org_uuid: str, provider: str = PROVIDER_CLAUDE
    ) -> int:
        """Drop every mapping pointing at (email, org_uuid, provider).

        ``provider`` defaults to ``claude`` and matches entries written
        before the field existed (back-compat) — an (email, org_uuid) pair
        could theoretically be reused across providers, so provider is
        checked alongside identity rather than dropped from the match.
        Returns the count removed.
        """
        mappings = self.load()
        org = org_uuid or ""
        doomed = [
            key
            for key, entry in mappings.items()
            if entry.get("email") == email
            and (entry.get("organizationUuid", "") or "") == org
            and normalize_provider(entry.get("provider")) == provider
        ]
        for key in doomed:
            del mappings[key]
        if doomed:
            self._write(mappings)
        return len(doomed)

    def resolve(self, cwd: str | Path) -> tuple[str, dict] | None:
        """Return (key, entry) of the longest mapped ancestor of ``cwd``.

        A mapping matches when its directory equals ``cwd`` or is an ancestor
        of it. The most specific (longest path) match wins, so nested folders
        inherit the closest mapping. All candidates lie on the single
        root→cwd chain, so the longest key string is the deepest match.
        """
        target = Path(normalize_path(cwd))
        best: tuple[str, dict] | None = None
        best_len = -1
        for key, entry in self.load().items():
            candidate = Path(key)
            if candidate == target or candidate in target.parents:
                if len(key) > best_len:
                    best = (key, entry)
                    best_len = len(key)
        return best

    def _write(self, mappings: dict[str, dict]) -> None:
        """Atomically write the mappings file (tempfile + os.replace)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            os.chmod(self.path.parent, 0o700)
        payload = json.dumps(
            {"schemaVersion": SCHEMA_VERSION, "mappings": mappings}, indent=2
        )
        fd, tmp = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".mappings-", suffix=".tmp"
        )
        try:
            if sys.platform != "win32":
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, self.path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
