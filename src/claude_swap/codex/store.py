"""cswap's own Codex account store: slot registry plus credential snapshots.

Two pieces with different lifetimes and different security postures:

- ``sequence.json`` — non-secret metadata (slot number, email, plan, alias,
  workspace name, disabled flag) keyed by slot number, each row naming its
  ``account_key``.
- the snapshot store — one ``auth.json`` payload per account, keyed by
  ``account_key``, in the macOS Keychain or in 0600 files elsewhere.

Snapshots are keyed by ``account_key`` rather than slot number on purpose. Slot
numbers are a presentation concern that a future ``cswap codex swap``/``move``
will renumber; the account key never changes. Keying secrets by a mutable
number turns that feature into a data migration.

Slot numbers are not compacted when an account is removed: renumbering would
silently repoint every alias and every number a user has memorised. The gap is
left, and the next add reuses the lowest free number — the same rule the Claude
side follows.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from claude_swap import macos_keychain
from claude_swap.codex import paths as cpaths
from claude_swap.codex.auth_file import file_key
from claude_swap.models import get_timestamp

#: Keychain service for Codex snapshots. Distinct from the Claude side's so a
#: purge or an audit can tell the two apart at a glance.
KEYCHAIN_SERVICE = "claude-swap-codex"


@dataclass
class CodexSlot:
    """One managed Codex account as stored (no secrets in here)."""

    number: str
    account_key: str
    email: str = ""
    plan: str = ""
    workspace_name: str = ""
    alias: str = ""
    added: str = ""
    disabled: bool = False
    auth_mode: str = "chatgpt"

    @property
    def display_label(self) -> str:
        """``email [workspace]`` — mirrors ``AccountInfo.display_label``."""
        tag = self.workspace_name or "personal"
        return f"{self.email} [{tag}]"

    def to_dict(self) -> dict:
        return {
            "account_key": self.account_key,
            "email": self.email,
            "plan": self.plan,
            "workspaceName": self.workspace_name,
            "alias": self.alias,
            "added": self.added,
            "disabled": self.disabled,
            "authMode": self.auth_mode,
        }

    @classmethod
    def from_dict(cls, number: str, data: dict) -> CodexSlot:
        return cls(
            number=number,
            account_key=data.get("account_key", ""),
            email=data.get("email", "") or "",
            plan=data.get("plan", "") or "",
            workspace_name=data.get("workspaceName", "") or "",
            alias=data.get("alias", "") or "",
            added=data.get("added", "") or "",
            disabled=bool(data.get("disabled", False)),
            auth_mode=data.get("authMode", "chatgpt") or "chatgpt",
        )


class CodexStore:
    """Reads and writes the Codex slot registry and snapshot store."""

    def __init__(self) -> None:
        self._sequence_path = cpaths.get_codex_sequence_path()
        self._credentials_dir = cpaths.get_codex_credentials_dir()

    # ---- slot registry -------------------------------------------------

    def _read(self) -> dict:
        try:
            data = json.loads(self._sequence_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A missing file is the normal fresh-install case; a corrupt one is
            # a torn write. Both degrade to "no accounts" rather than making
            # every cswap command raise.
            return {"accounts": {}, "activeAccountKey": None}
        if not isinstance(data, dict):
            return {"accounts": {}, "activeAccountKey": None}
        if not isinstance(data.get("accounts"), dict):
            data["accounts"] = {}
        data.setdefault("activeAccountKey", None)
        return data

    def _write(self, data: dict) -> None:
        self._sequence_path.parent.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            os.chmod(self._sequence_path.parent, 0o700)
        data["lastUpdated"] = get_timestamp()
        tmp = self._sequence_path.with_name(self._sequence_path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        if sys.platform != "win32":
            os.chmod(tmp, 0o600)
        os.replace(tmp, self._sequence_path)

    def slots(self) -> list[CodexSlot]:
        """Every managed slot, ordered by slot number."""
        accounts = self._read()["accounts"]
        out = [
            CodexSlot.from_dict(num, row)
            for num, row in accounts.items()
            if isinstance(row, dict) and str(num).isdigit()
        ]
        out.sort(key=lambda s: int(s.number))
        return out

    def slot_for_key(self, account_key: str) -> CodexSlot | None:
        for slot in self.slots():
            if slot.account_key == account_key:
                return slot
        return None

    def _next_free_number(self, accounts: dict) -> str:
        taken = {int(n) for n in accounts if str(n).isdigit()}
        n = 1
        while n in taken:
            n += 1
        return str(n)

    def upsert_slot(
        self,
        account_key: str,
        *,
        email: str = "",
        plan: str = "",
        workspace_name: str = "",
        auth_mode: str = "chatgpt",
    ) -> CodexSlot:
        """Create or update the slot for ``account_key``; returns it."""
        data = self._read()
        accounts = data["accounts"]

        for num, row in accounts.items():
            if isinstance(row, dict) and row.get("account_key") == account_key:
                row["email"] = email or row.get("email", "")
                row["plan"] = plan or row.get("plan", "")
                if workspace_name:
                    row["workspaceName"] = workspace_name
                row["authMode"] = auth_mode
                self._write(data)
                return CodexSlot.from_dict(num, row)

        number = self._next_free_number(accounts)
        slot = CodexSlot(
            number=number,
            account_key=account_key,
            email=email,
            plan=plan,
            workspace_name=workspace_name,
            added=get_timestamp(),
            auth_mode=auth_mode,
        )
        accounts[number] = slot.to_dict()
        self._write(data)
        return slot

    def remove_slot(self, account_key: str) -> bool:
        """Forget a slot and its snapshot. Returns whether anything was removed."""
        data = self._read()
        accounts = data["accounts"]
        target = next(
            (
                num
                for num, row in accounts.items()
                if isinstance(row, dict) and row.get("account_key") == account_key
            ),
            None,
        )
        if target is None:
            return False
        del accounts[target]
        if data.get("activeAccountKey") == account_key:
            data["activeAccountKey"] = None
        self._write(data)
        self.delete_snapshot(account_key)
        return True

    def renumber(self, mapping: dict[str, str]) -> None:
        """Reassign slot numbers: ``{account_key: new_number}``, atomically.

        Only ``sequence.json`` moves. Snapshots are keyed by ``account_key``
        precisely so renumbering never has to touch a secret — the whole reason
        this store does not key them by slot.
        """
        if not mapping:
            return
        data = self._read()
        accounts = data["accounts"]
        rows = {
            row.get("account_key"): (num, row)
            for num, row in accounts.items()
            if isinstance(row, dict)
        }
        for key in mapping:
            if key not in rows:
                raise KeyError(f"unknown account_key: {key}")
        for key, new_number in mapping.items():
            old_number, _row = rows[key]
            accounts.pop(old_number, None)
        for key, new_number in mapping.items():
            accounts[str(new_number)] = rows[key][1]
        self._write(data)

    def set_active(self, account_key: str | None) -> None:
        """Record cswap's *intent*. The live auth.json remains the authority on
        what is actually active — see ``auth_file.read_live_identity``."""
        data = self._read()
        data["activeAccountKey"] = account_key
        self._write(data)

    def active_key(self) -> str | None:
        return self._read().get("activeAccountKey")

    def active_number(self) -> str | None:
        key = self.active_key()
        if not key:
            return None
        slot = self.slot_for_key(key)
        return slot.number if slot else None

    def set_alias(self, account_key: str, alias: str) -> None:
        self._mutate(account_key, "alias", alias)

    def set_disabled(self, account_key: str, disabled: bool) -> None:
        self._mutate(account_key, "disabled", bool(disabled))

    def set_workspace_name(self, account_key: str, name: str) -> None:
        self._mutate(account_key, "workspaceName", name)

    def _mutate(self, account_key: str, field_name: str, value: object) -> None:
        data = self._read()
        for row in data["accounts"].values():
            if isinstance(row, dict) and row.get("account_key") == account_key:
                row[field_name] = value
                self._write(data)
                return

    # ---- snapshot store ------------------------------------------------

    def _use_keychain(self) -> bool:
        return sys.platform == "darwin"

    def _snapshot_path(self, account_key: str) -> Path:
        return self._credentials_dir / f"{file_key(account_key)}.json"

    def write_snapshot(self, account_key: str, payload: dict) -> None:
        """Persist one account's ``auth.json`` payload."""
        blob = json.dumps(payload)
        if self._use_keychain():
            macos_keychain.set_password(KEYCHAIN_SERVICE, file_key(account_key), blob)
            return
        self._credentials_dir.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            os.chmod(self._credentials_dir, 0o700)
        path = self._snapshot_path(account_key)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(blob, encoding="utf-8")
        if sys.platform != "win32":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    def read_snapshot(self, account_key: str) -> dict | None:
        """Read one account's stored payload, or None when absent/corrupt."""
        if self._use_keychain():
            blob = macos_keychain.get_password(KEYCHAIN_SERVICE, file_key(account_key))
        else:
            try:
                blob = self._snapshot_path(account_key).read_text(encoding="utf-8")
            except OSError:
                blob = None
        if not blob:
            return None
        try:
            data = json.loads(blob)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def delete_snapshot(self, account_key: str) -> None:
        if self._use_keychain():
            macos_keychain.delete_password(KEYCHAIN_SERVICE, file_key(account_key))
            return
        try:
            self._snapshot_path(account_key).unlink()
        except OSError:
            pass
