"""Export and import Codex accounts, and purge cswap's Codex data.

The export file contains **live OAuth tokens**. That is the point — an export
you cannot log in with is not a backup — but it makes the file exactly as
sensitive as the Keychain items it came from, so it is written 0600 and the
format says so in a top-level ``warning`` field that any tool reading it will
surface.

Deliberately a separate format from the Claude side's: the two providers store
different things (``account_key`` and an ``auth.json`` payload here, an
org-scoped credential blob there), and one file that had to describe both would
be a union type nobody could validate. A ``provider`` field means an import can
refuse a file from the wrong side rather than half-applying it.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from claude_swap.codex import paths as cpaths
from claude_swap.codex.store import CodexStore
from claude_swap.exceptions import ClaudeSwitchError

#: Bumped when the on-disk export shape changes incompatibly.
EXPORT_VERSION = 1

_WARNING = (
    "This file contains live OAuth tokens for the accounts below. "
    "Anyone who can read it can use those accounts. Keep it private and "
    "delete it once imported."
)


def export_codex_accounts(
    switcher, destination: str, account: str | None = None
) -> int:
    """Write accounts (with credentials) to ``destination``; ``-`` is stdout.

    Returns how many accounts were written.
    """
    store = CodexStore()
    slots = store.slots()
    if account:
        number, _email, _label = switcher.resolve_account(account)
        slots = [s for s in slots if s.number == number]
    if not slots:
        raise ClaudeSwitchError("No Codex accounts to export")

    accounts = []
    for slot in slots:
        payload = store.read_snapshot(slot.account_key)
        if payload is None:
            # An account with no credentials cannot be imported anywhere; a
            # silent empty row would look like a successful backup.
            continue
        accounts.append(
            {
                "accountKey": slot.account_key,
                "email": slot.email,
                "plan": slot.plan,
                "workspaceName": slot.workspace_name,
                "alias": slot.alias,
                "disabled": slot.disabled,
                "authMode": slot.auth_mode,
                "auth": payload,
            }
        )
    if not accounts:
        raise ClaudeSwitchError("No Codex accounts with stored credentials to export")

    document = {
        "version": EXPORT_VERSION,
        "provider": "codex",
        "warning": _WARNING,
        "accounts": accounts,
    }
    blob = json.dumps(document, indent=2)

    if destination == "-":
        print(blob)
        return len(accounts)

    path = Path(destination).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Created 0600 *before* the tokens go in, not chmod'ed afterwards: between
    # write and chmod the file would be world-readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(blob)
    return len(accounts)


def import_codex_accounts(switcher, source: str, force: bool = False) -> int:
    """Read accounts from ``source`` (``-`` is stdin) into the store."""
    if source == "-":
        raw = sys.stdin.read()
    else:
        path = Path(source).expanduser()
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as e:
            raise ClaudeSwitchError(f"Cannot read {source}: {e}") from e

    try:
        document = json.loads(raw)
    except ValueError as e:
        raise ClaudeSwitchError(f"{source} is not valid JSON: {e}") from e
    if not isinstance(document, dict):
        raise ClaudeSwitchError(f"{source} is not a cswap export")

    provider = document.get("provider")
    if provider is not None and provider != "codex":
        # Refuse rather than half-apply: a Claude export's rows describe
        # different fields entirely.
        raise ClaudeSwitchError(
            f"{source} is a '{provider}' export, not a Codex one"
        )
    version = document.get("version")
    if isinstance(version, int) and version > EXPORT_VERSION:
        raise ClaudeSwitchError(
            f"{source} uses export version {version}, newer than this cswap understands"
        )

    rows = document.get("accounts")
    if not isinstance(rows, list) or not rows:
        raise ClaudeSwitchError(f"{source} contains no accounts")

    store = CodexStore()
    existing = {s.account_key for s in store.slots()}
    imported = 0

    for row in rows:
        if not isinstance(row, dict):
            continue
        key = row.get("accountKey")
        payload = row.get("auth")
        if not isinstance(key, str) or not key or not isinstance(payload, dict):
            continue
        if key in existing and not force:
            continue
        store.upsert_slot(
            key,
            email=str(row.get("email") or ""),
            plan=str(row.get("plan") or ""),
            workspace_name=str(row.get("workspaceName") or ""),
            auth_mode=str(row.get("authMode") or "chatgpt"),
        )
        alias = row.get("alias")
        if isinstance(alias, str) and alias:
            store.set_alias(key, alias)
        if row.get("disabled"):
            store.set_disabled(key, True)
        store.write_snapshot(key, payload)
        imported += 1

    return imported


def purge_codex_data(assume_yes: bool = False) -> bool:
    """Delete every Codex account cswap manages. Returns whether it ran.

    Removes the Keychain items too — a purge that left the secrets behind would
    be worse than none, since nothing would list them any more.
    """
    store = CodexStore()
    slots = store.slots()
    root = cpaths.get_codex_store_root()

    if not slots and not root.exists():
        print("No cswap Codex data to remove.")
        return False

    if not assume_yes:
        answer = input(
            f"Remove {len(slots)} managed Codex account(s) and all cswap Codex "
            "data? Your ~/.codex login is left alone. [y/N] "
        )
        if answer.lower() != "y":
            print("Cancelled")
            return False

    for slot in slots:
        store.delete_snapshot(slot.account_key)

    import shutil

    shutil.rmtree(root, ignore_errors=True)
    print(f"Removed {len(slots)} Codex account(s) and {root}")
    return True
