"""One-time import of codex-auth's accounts into cswap's own store.

Read-only against ``~/.codex/accounts/``: cswap never writes that tree. The
user keeps a working codex-auth install and can go back to it — the price is
that the two stores diverge after the import, which is the accepted cost of
owning our own format.

Schema support mirrors codex-auth's own history:

- ``version = 2``  email-keyed, no ``account_key`` — nothing to key a snapshot
  by, so rows are counted as skipped rather than guessed at.
- ``schema_version = 3`` and ``4`` record-key-based; identical account layout.
  v4 renamed the plan tiers, and v3 rows are normalized to v4 semantics on the
  way in so a Business account does not display as Enterprise.

An unknown, newer schema is refused outright. Misreading a format we do not
know would corrupt the user's account list; reporting "too new" costs them
nothing but an upgrade.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from claude_swap.codex import paths as cpaths
from claude_swap.codex.auth_file import file_key
from claude_swap.codex.plans import normalize_plan
from claude_swap.codex.store import CodexStore

_logger = logging.getLogger(__name__)

#: Highest ``schema_version`` this importer understands.
MAX_SCHEMA = 4


@dataclass(frozen=True)
class ImportResult:
    """What one import pass did."""

    imported: int = 0
    skipped: int = 0
    source: str | None = None
    unsupported_schema: int | None = None

    @property
    def did_anything(self) -> bool:
        return self.imported > 0


def _load_registry() -> dict | None:
    path = cpaths.get_codex_auth_registry_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _rows(data: dict) -> list[dict]:
    """Normalize the accounts container to a list of row dicts.

    v3/v4 store a list; the v2 loader stored an email-keyed dict.
    """
    accounts = data.get("accounts")
    if isinstance(accounts, list):
        return [r for r in accounts if isinstance(r, dict)]
    if isinstance(accounts, dict):
        return [r for r in accounts.values() if isinstance(r, dict)]
    return []


def import_codex_auth_registry(*, only_if_empty: bool = False) -> ImportResult:
    """Import codex-auth's accounts. Safe to call repeatedly.

    ``only_if_empty`` is what the automatic first-run path passes: it makes the
    call a no-op once cswap has any Codex slot of its own, so an import can
    never overwrite accounts the user has since added or renamed here.
    """
    store = CodexStore()
    if only_if_empty and store.slots():
        return ImportResult(source=None)

    data = _load_registry()
    if data is None:
        return ImportResult(source=None)

    schema = data.get("schema_version") or data.get("version") or 2
    if not isinstance(schema, int) or schema > MAX_SCHEMA:
        _logger.warning(
            "codex-auth registry uses schema %s, newer than this cswap understands "
            "(max %s); not importing",
            schema,
            MAX_SCHEMA,
        )
        return ImportResult(
            unsupported_schema=schema if isinstance(schema, int) else None
        )

    accounts_dir = cpaths.get_codex_auth_accounts_dir()
    imported = skipped = 0

    for row in _rows(data):
        key = row.get("account_key")
        if not isinstance(key, str) or not key:
            skipped += 1
            continue

        snapshot_path = accounts_dir / f"{file_key(key)}.auth.json"
        try:
            payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # The registry row outlived its auth file. Costs this one account.
            skipped += 1
            continue
        if not isinstance(payload, dict):
            skipped += 1
            continue

        store.upsert_slot(
            key,
            email=str(row.get("email") or ""),
            plan=normalize_plan(row.get("plan")),
            workspace_name=str(row.get("account_name") or ""),
            auth_mode=str(row.get("auth_mode") or "chatgpt"),
        )
        alias = row.get("alias")
        if isinstance(alias, str) and alias:
            store.set_alias(key, alias)
        store.write_snapshot(key, payload)
        imported += 1

    active = data.get("active_account_key")
    if isinstance(active, str) and active and store.slot_for_key(active):
        store.set_active(active)

    return ImportResult(
        imported=imported,
        skipped=skipped,
        source=str(cpaths.get_codex_auth_registry_path()),
    )
