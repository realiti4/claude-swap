"""Export and import account data for claude-swap.

Moves the OAuth credentials and config across machines via a portable
JSON envelope. No encryption is built in — users compose their own
(e.g. `cswap --export - | gpg -c > out.gpg`).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from claude_swap import __version__
from claude_swap.exceptions import (
    ConfigError,
    CredentialReadError,
    TransferError,
)
from claude_swap.models import Platform, get_timestamp

if TYPE_CHECKING:
    from claude_swap.switcher import ClaudeAccountSwitcher


FORMAT_VERSION = 1

_PLATFORM_TAG = {
    Platform.MACOS: "macos",
    Platform.LINUX: "linux",
    Platform.WSL: "wsl",
    Platform.WINDOWS: "windows",
    Platform.UNKNOWN: "unknown",
}


def _eprint(msg: str) -> None:
    """Print to stderr so stdout stays pure JSON in pipe mode."""
    print(msg, file=sys.stderr)


def _find_account_slot(
    sequence_data: dict, email: str, organization_uuid: str
) -> str | None:
    """Return slot key for matching (email, organizationUuid), else None."""
    for num, account in sequence_data.get("accounts", {}).items():
        if (
            account.get("email") == email
            and account.get("organizationUuid", "") == organization_uuid
        ):
            return num
    return None


def _parse_payload(text: str, label: str) -> dict:
    """Parse a JSON string that should decode to an object."""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TransferError(f"{label} is not valid JSON: {exc}")
    if not isinstance(parsed, dict):
        raise TransferError(f"{label} must be a JSON object")
    return parsed


def _validate_imported_account(switcher: ClaudeAccountSwitcher, account: dict) -> tuple[str, str]:
    """Validate per-account fields BEFORE any filename construction.

    Defends against path traversal: email + slot number flow into f-string
    filenames in switcher._read_account_credentials etc., so they must be
    constrained before use.
    """
    if not isinstance(account, dict):
        raise TransferError("account entry must be a JSON object")

    email = account.get("email")
    if not isinstance(email, str) or not switcher._validate_email(email):
        raise TransferError(f"invalid or missing email in imported account: {email!r}")

    raw_number = account.get("number")
    if isinstance(raw_number, bool) or not isinstance(raw_number, int) or raw_number < 1:
        raise TransferError(
            f"invalid slot number in imported account ({email}): {raw_number!r}"
        )

    # Org/uuid/added must be strings (or absent). A list/dict here would
    # otherwise blow up downstream (unhashable in seen_keys, broken composite
    # key matching, garbage in sequence.json).
    for field in ("organizationUuid", "organizationName", "uuid", "added"):
        if field in account and account[field] is not None:
            if not isinstance(account[field], str):
                raise TransferError(
                    f"{field} for {email} must be a string, got {type(account[field]).__name__}"
                )

    return email, str(raw_number)


def _atomic_write_file(path: Path, content: str) -> None:
    """Write text atomically with 0600 perms — same pattern as switcher._write_json."""
    temp_path = path.with_suffix(f".{os.getpid()}.tmp")
    temp_path.write_text(content, encoding="utf-8")
    if sys.platform != "win32":
        os.chmod(temp_path, 0o600)
    shutil.move(str(temp_path), str(path))
    if sys.platform != "win32":
        os.chmod(path, 0o600)


def export_accounts(
    switcher: ClaudeAccountSwitcher,
    destination: str,
    account: str | None = None,
) -> None:
    """Export accounts to a JSON file or stdout.

    Args:
        switcher: Initialized ClaudeAccountSwitcher.
        destination: File path, or "-" for stdout.
        account: Optional NUM|EMAIL to limit export to a single account.

    Raises:
        TransferError: malformed/missing data, unknown account.
        CredentialReadError: failed to read credentials.
    """
    sequence_data = switcher._get_sequence_data_migrated()
    if not sequence_data or not sequence_data.get("accounts"):
        raise TransferError("no accounts to export — run cswap --add-account first")

    accounts_map = sequence_data["accounts"]

    # Resolve which account numbers to export
    if account is not None:
        resolved = switcher._resolve_account_identifier(account)
        if resolved is None or resolved not in accounts_map:
            raise TransferError(f"account not found: {account}")
        target_nums = [resolved]
    else:
        target_nums = sorted(accounts_map.keys(), key=int)

    # Identify the live active account (live vault has fresher tokens than backup)
    current_identity = switcher._get_current_account()

    accounts_payload: list[dict[str, Any]] = []
    for num in target_nums:
        record = accounts_map[num]
        email = record.get("email", "")
        org_uuid = record.get("organizationUuid", "") or ""

        is_active = (
            current_identity is not None
            and current_identity[0] == email
            and current_identity[1] == org_uuid
        )

        if is_active:
            creds_text = switcher._read_credentials()
            if not creds_text:
                raise CredentialReadError(
                    f"failed to read live credentials for active account {email}"
                )
            config_path = switcher._get_claude_config_path()
            if not config_path.exists():
                raise ConfigError("Claude config file not found")
            config_text = config_path.read_text(encoding="utf-8")
        else:
            creds_text = switcher._read_account_credentials(num, email)
            config_text = switcher._read_account_config(num, email)
            if not creds_text:
                raise CredentialReadError(
                    f"no backup credentials found for account {num} ({email})"
                )
            if not config_text:
                raise ConfigError(
                    f"no backup config found for account {num} ({email})"
                )

        accounts_payload.append(
            {
                "number": int(num),
                "email": email,
                "uuid": record.get("uuid", ""),
                "organizationUuid": org_uuid,
                "organizationName": record.get("organizationName", "") or "",
                "added": record.get("added", ""),
                "credentials": _parse_payload(creds_text, f"credentials for {email}"),
                "config": _parse_payload(config_text, f"config for {email}"),
            }
        )

    envelope = {
        "version": FORMAT_VERSION,
        "exportedAt": get_timestamp(),
        "exportedFrom": _PLATFORM_TAG.get(switcher.platform, "unknown"),
        "swapVersion": __version__,
        "encrypted": False,
        "activeAccountNumber": sequence_data.get("activeAccountNumber"),
        "accounts": accounts_payload,
    }

    serialized = json.dumps(envelope, indent=2)

    if destination == "-":
        sys.stdout.write(serialized)
        sys.stdout.write("\n")
        sys.stdout.flush()
        return

    out_path = Path(destination).expanduser()
    _atomic_write_file(out_path, serialized + "\n")
    _eprint(f"Exported {len(accounts_payload)} account(s) to {out_path}")


def import_accounts(
    switcher: ClaudeAccountSwitcher,
    source: str,
    force: bool = False,
) -> None:
    """Import accounts from a JSON file or stdin.

    Args:
        switcher: Initialized ClaudeAccountSwitcher.
        source: File path, or "-" for stdin.
        force: When True, overwrites the existing matching slot in place.

    Raises:
        TransferError: malformed file, version mismatch, encrypted payload.
    """
    if source == "-":
        text = sys.stdin.read()
    else:
        in_path = Path(source).expanduser()
        if not in_path.exists():
            raise TransferError(f"import file not found: {in_path}")
        text = in_path.read_text(encoding="utf-8")

    try:
        envelope = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TransferError(f"export file is not valid JSON: {exc}")

    if not isinstance(envelope, dict):
        raise TransferError("export file must be a JSON object")

    version = envelope.get("version")
    if version != FORMAT_VERSION:
        raise TransferError(
            f"unsupported export version: {version!r} (expected {FORMAT_VERSION})"
        )

    if envelope.get("encrypted") is True:
        raise TransferError(
            "encrypted exports are not supported in this version — "
            "decrypt before piping (e.g. gpg -d backup.gpg | cswap --import -)"
        )

    accounts = envelope.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        raise TransferError("export file has no accounts to import")

    # Pass 1: validate every account before any writes. A malformed account
    # later in the list must not leave earlier accounts half-imported.
    normalized: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()
    for raw in accounts:
        email, exported_num = _validate_imported_account(switcher, raw)
        org_uuid = raw.get("organizationUuid", "") or ""
        creds_obj = raw.get("credentials")
        config_obj = raw.get("config")
        if not isinstance(creds_obj, dict) or not isinstance(config_obj, dict):
            raise TransferError(
                f"credentials and config for {email} must be JSON objects"
            )
        key = (email, org_uuid)
        if key in seen_keys:
            raise TransferError(
                f"duplicate account in export: {email} (org={org_uuid or 'personal'})"
            )
        seen_keys.add(key)
        normalized.append(
            {
                "email": email,
                "exported_num": exported_num,
                "org_uuid": org_uuid,
                "org_name": raw.get("organizationName", "") or "",
                "uuid": raw.get("uuid", "") or "",
                "added": raw.get("added") or get_timestamp(),
                "creds_text": json.dumps(creds_obj),
                "config_text": json.dumps(config_obj, indent=2),
            }
        )

    # Pass 2: writes. Validation is complete; remaining failures (disk I/O,
    # keyring) are environmental and don't reflect on the file's integrity.
    switcher._setup_directories()
    switcher._init_sequence_file()

    imported = 0
    skipped = 0
    overwritten = 0

    # Track where the envelope's active account ended up locally. We can't
    # just look up envelope_active in the final account map afterwards: the
    # destination may already have an unrelated account at that slot number,
    # while the envelope's active account got allocated to a different slot.
    envelope_active = envelope.get("activeAccountNumber")
    envelope_active_str = (
        str(envelope_active) if isinstance(envelope_active, int) else None
    )
    resolved_active_slot: str | None = None

    for entry in normalized:
        is_envelope_active = (
            envelope_active_str is not None
            and entry["exported_num"] == envelope_active_str
        )

        # Re-read sequence each iteration so per-account writes see prior updates
        data = switcher._get_sequence_data_migrated() or {
            "activeAccountNumber": None,
            "lastUpdated": get_timestamp(),
            "sequence": [],
            "accounts": {},
        }
        existing_slot = _find_account_slot(data, entry["email"], entry["org_uuid"])

        if existing_slot is not None:
            if not force:
                _eprint(
                    f"Skipped {entry['email']} (already exists, use --force)"
                )
                skipped += 1
                # Even when skipped, the envelope's active account exists
                # locally — record where so we can seed activeAccountNumber.
                if is_envelope_active:
                    resolved_active_slot = existing_slot
                continue
            target_num = existing_slot
            outcome = "overwrote"
        else:
            if entry["exported_num"] not in data.get("accounts", {}):
                target_num = entry["exported_num"]
            else:
                target_num = str(switcher._get_next_account_number())
            outcome = "imported"

        switcher._write_account_credentials(
            target_num, entry["email"], entry["creds_text"]
        )
        switcher._write_account_config(
            target_num, entry["email"], entry["config_text"]
        )

        data.setdefault("accounts", {})
        data.setdefault("sequence", [])
        data["accounts"][target_num] = {
            "email": entry["email"],
            "uuid": entry["uuid"],
            "organizationUuid": entry["org_uuid"],
            "organizationName": entry["org_name"],
            "added": entry["added"],
        }
        if int(target_num) not in data["sequence"]:
            data["sequence"].append(int(target_num))
            data["sequence"].sort()
        data["lastUpdated"] = get_timestamp()
        switcher._write_json(switcher.sequence_file, data)

        if is_envelope_active:
            resolved_active_slot = target_num

        if outcome == "overwrote":
            _eprint(f"Overwrote {entry['email']} (slot {target_num})")
            overwritten += 1
        else:
            _eprint(f"Imported {entry['email']} → slot {target_num}")
            imported += 1

    # Migration UX: if the destination has no recorded active account
    # (clean home, no prior preference), seed activeAccountNumber from the
    # *resolved* slot of the envelope's active account — not the envelope's
    # raw slot number, which may already be occupied locally by an unrelated
    # account. If the user already has an active selection locally, leave it.
    final = switcher._get_sequence_data()
    if (
        final is not None
        and final.get("activeAccountNumber") in (None, 0)
        and resolved_active_slot is not None
    ):
        final["activeAccountNumber"] = int(resolved_active_slot)
        final["lastUpdated"] = get_timestamp()
        switcher._write_json(switcher.sequence_file, final)

    _eprint(
        f"Done: {imported} imported, {overwritten} overwritten, {skipped} skipped"
    )
