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
from claude_swap.credentials import looks_like_api_key
from claude_swap.exceptions import (
    ConfigError,
    CredentialReadError,
    TransferError,
)
from claude_swap.models import Platform, get_timestamp, normalize_alias

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
    for field in ("organizationUuid", "organizationName", "uuid", "added", "alias"):
        if field in account and account[field] is not None:
            if not isinstance(account[field], str):
                raise TransferError(
                    f"{field} for {email} must be a string, got {type(account[field]).__name__}"
                )

    alias = account.get("alias")
    if isinstance(alias, str):
        try:
            normalize_alias(alias)
        except ValueError as e:
            raise TransferError(f"invalid alias for {email}: {e}") from e

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


def _slim_config(config_obj: dict, label: str) -> dict:
    """Reduce a parsed ~/.claude.json to just the keys a switch will consume.

    Today, only `oauthAccount` is read back during a switch. Stripping the
    rest at export time keeps cross-machine transfers small and avoids
    leaking source-machine identity (userID, anonymousId, absolute paths,
    cached feature flags) into the destination.
    """
    oauth = config_obj.get("oauthAccount")
    if not isinstance(oauth, dict):
        raise TransferError(
            f"{label} is missing oauthAccount — cannot export"
        )
    return {"oauthAccount": oauth}


def export_accounts(
    switcher: ClaudeAccountSwitcher,
    destination: str,
    account: str | None = None,
    full: bool = False,
) -> None:
    """Export accounts to a JSON file or stdout.

    Args:
        switcher: Initialized ClaudeAccountSwitcher.
        destination: File path, or "-" for stdout.
        account: Optional NUM|EMAIL to limit export to a single account.
        full: When True, include the entire ~/.claude.json snapshot per
            account (same-PC backup). Default False writes only oauthAccount.

    Raises:
        TransferError: malformed/missing data, unknown account.
        CredentialReadError: failed to read credentials.
    """
    sequence_data = switcher._get_sequence_data_migrated()
    if not sequence_data or not sequence_data.get("accounts"):
        raise TransferError("no accounts to export — run cswap --add-account first")

    accounts_map = sequence_data["accounts"]

    # Resolve which account numbers to export. When the user named a specific
    # account, missing backup data is a hard failure (they asked for that one);
    # in the all-accounts case we skip broken slots with a warning so one
    # damaged slot doesn't poison the whole backup.
    explicit_account = account is not None
    if explicit_account:
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
            if not creds_text or not config_text:
                if explicit_account:
                    if not creds_text:
                        raise CredentialReadError(
                            f"no backup credentials found for account {num} ({email})"
                        )
                    raise ConfigError(
                        f"no backup config found for account {num} ({email})"
                    )
                _eprint(
                    f"Skipping Account-{num} ({email}): no stored "
                    f"credentials/config — re-add with: "
                    f"cswap --add-account --slot {num}"
                )
                continue

        config_obj = _parse_payload(config_text, f"config for {email}")
        if not full:
            config_obj = _slim_config(config_obj, f"config for {email}")

        # API-key accounts store the credential as a raw ``sk-ant-api…`` string,
        # not OAuth JSON — carry it verbatim (and tag the kind) so the JSON parse
        # below doesn't choke and import can restore it as-is.
        is_api_key = looks_like_api_key(creds_text)
        entry: dict[str, Any] = {
            "number": int(num),
            "email": email,
            "uuid": record.get("uuid", ""),
            "organizationUuid": org_uuid,
            "organizationName": record.get("organizationName", "") or "",
            "added": record.get("added", ""),
            "credentials": (
                creds_text.strip()
                if is_api_key
                else _parse_payload(creds_text, f"credentials for {email}")
            ),
            "config": config_obj,
        }
        if is_api_key:
            entry["kind"] = "api_key"
        if record.get("alias"):
            entry["alias"] = record["alias"]
        accounts_payload.append(entry)

    if not accounts_payload:
        raise TransferError(
            "no exportable accounts — all managed slots are missing stored "
            "credentials/config. Re-add with: cswap --add-account --slot <number>"
        )

    # Only carry activeAccountNumber if that slot is actually present in the
    # payload — otherwise import would reference an account that isn't there
    # (e.g., the recorded active slot was skipped due to missing backup).
    recorded_active = sequence_data.get("activeAccountNumber")
    exported_nums = {a["number"] for a in accounts_payload}
    active_in_payload = (
        recorded_active if recorded_active in exported_nums else None
    )

    envelope = {
        "version": FORMAT_VERSION,
        "exportedAt": get_timestamp(),
        "exportedFrom": _PLATFORM_TAG.get(switcher.platform, "unknown"),
        "swapVersion": __version__,
        "encrypted": False,
        "activeAccountNumber": active_in_payload,
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
            Without it, existing accounts are skipped — unless the slot is
            quarantined as refresh-token-dead, which a plain import replaces
            (auto-heal, issue #136).

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
    local_data = switcher._get_sequence_data_migrated() or {}
    local_aliases: dict[str, tuple[str, str]] = {
        (acc.get("alias") or "").lower(): (
            acc.get("email", ""), acc.get("organizationUuid", "") or "",
        )
        for acc in local_data.get("accounts", {}).values()
        if acc.get("alias")
    }
    normalized: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()
    seen_aliases: set[str] = set()
    for raw in accounts:
        email, exported_num = _validate_imported_account(switcher, raw)
        org_uuid = raw.get("organizationUuid", "") or ""
        creds_obj = raw.get("credentials")
        config_obj = raw.get("config")
        if not isinstance(config_obj, dict):
            raise TransferError(f"config for {email} must be a JSON object")
        # API-key accounts carry the credential as a raw string; OAuth accounts
        # carry a JSON object.
        is_api_key = raw.get("kind") == "api_key" or isinstance(creds_obj, str)
        if is_api_key:
            if not (isinstance(creds_obj, str) and looks_like_api_key(creds_obj)):
                raise TransferError(
                    f"API-key credentials for {email} must be a raw sk-ant-api… string"
                )
            creds_text = creds_obj.strip()
        else:
            if not isinstance(creds_obj, dict):
                raise TransferError(
                    f"credentials for {email} must be a JSON object"
                )
            creds_text = json.dumps(creds_obj)
        key = (email, org_uuid)
        if key in seen_keys:
            raise TransferError(
                f"duplicate account in export: {email} (org={org_uuid or 'personal'})"
            )
        seen_keys.add(key)

        alias = raw.get("alias") or None
        if alias:
            alias_key = normalize_alias(alias)  # already validated in pass-1 above
            if alias_key in seen_aliases:
                raise TransferError(f"duplicate alias in export: {alias_key}")
            seen_aliases.add(alias_key)
            owner = local_aliases.get(alias_key)
            if owner is not None and owner != (email, org_uuid):
                _eprint(
                    f"Warning: alias '{alias_key}' for {email} already used by an "
                    "existing account, dropping the imported alias"
                )
                alias = None
            else:
                alias = alias_key

        normalized.append(
            {
                "email": email,
                "exported_num": exported_num,
                "org_uuid": org_uuid,
                "org_name": raw.get("organizationName", "") or "",
                "uuid": raw.get("uuid", "") or "",
                "added": raw.get("added") or get_timestamp(),
                "kind": "api_key" if is_api_key else "oauth",
                "alias": alias,
                "creds_text": creds_text,
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
    replaced = 0
    written_slots: set[str] = set()

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
        existing_slot = switcher._find_account_slot(
            data, entry["email"], entry["org_uuid"]
        )

        if existing_slot is not None:
            if force:
                outcome = "overwrote"
            elif (
                switcher._usage_store.entries(
                    {existing_slot: (entry["email"], entry["org_uuid"])}
                )[existing_slot].token_dead()
            ):
                # Narrow auto-heal (issue #136): a plain import replaces a
                # slot iff its identity-matched usage row is quarantined as
                # refresh-token-dead. The verdict normally postdates the
                # slot's last credential write, so the heal targets creds that
                # failed after being stored (known exception and full
                # trade-off: INVESTIGATION-import-dead-token.md). Identity-
                # guarded — a stale row for a different account returns an
                # empty entry — so healthy slots still require --force. Never
                # triggered by the live store's "no credentials" state, which
                # isn't attributable to the backup.
                outcome = "replaced"
            else:
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
            # The credential write below invalidates the slot's non-live
            # session profile (chokepoint in _write_account_credentials), so
            # the next `cswap run` re-bootstraps from the imported creds. A
            # live session keeps running on its own copy — warn about it.
            live_pids = switcher._live_session_pids(target_num, entry["email"])
            if live_pids:
                _eprint(
                    f"Warning: {entry['email']} (slot {target_num}) has a live "
                    f"session-mode instance (PID {', '.join(map(str, live_pids))}); "
                    "its session profile keeps the pre-import credentials until "
                    "it is restarted via 'cswap run'."
                )
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
        # Every successful import write introduces credential material whose
        # previous auth verdict is no longer authoritative, so lift any
        # dead-token quarantine on this slot (mirrors add_account / the
        # add-token paths). This clears for both "imported" and "overwrote":
        # account removal doesn't prune usage.json, so re-importing a removed
        # identity into the same slot would otherwise stay quarantined and
        # never re-fetch to prove the imported token — issue #138.
        switcher._usage_store.clear_dead_token(
            [target_num], {target_num: (entry["email"], entry["org_uuid"])}
        )

        data.setdefault("accounts", {})
        data.setdefault("sequence", [])
        new_record = {
            "email": entry["email"],
            "uuid": entry["uuid"],
            "organizationUuid": entry["org_uuid"],
            "organizationName": entry["org_name"],
            "added": entry["added"],
        }
        if entry["kind"] == "api_key":
            new_record["kind"] = "api_key"
        if entry.get("alias"):
            new_record["alias"] = entry["alias"]
        data["accounts"][target_num] = new_record
        if int(target_num) not in data["sequence"]:
            data["sequence"].append(int(target_num))
            data["sequence"].sort()
        data["lastUpdated"] = get_timestamp()
        switcher._write_json(switcher.sequence_file, data)

        if is_envelope_active:
            resolved_active_slot = target_num
        written_slots.add(target_num)

        if outcome == "overwrote":
            _eprint(f"Overwrote {entry['email']} (slot {target_num})")
            overwritten += 1
        elif outcome == "replaced":
            # Describe the observed trigger (the quarantine verdict), not the
            # token itself — a stale verdict can sit over newer working creds.
            _eprint(
                f"Replaced {entry['email']} (slot {target_num} was "
                "quarantined: refresh token dead)"
            )
            replaced += 1
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

    # "replaced" gets its own count — the user must be able to distinguish
    # "I forced this" from "cswap healed this". Appended only when it
    # happened, keeping the common-case summary stable.
    summary = (
        f"Done: {imported} imported, {overwritten} overwritten, {skipped} skipped"
    )
    if replaced:
        summary += f", {replaced} replaced (dead token)"
    _eprint(summary)

    # If we just rewrote the stored backup for the account that is the current
    # live login, a plain switch would back the (possibly stale) live
    # credentials up over it (issue #79) — point at the explicit activation
    # path instead.
    identity = switcher._get_current_account()
    if identity is not None and final is not None:
        live_slot = switcher._find_account_slot(final, identity[0], identity[1])
        if live_slot is not None and live_slot in written_slots:
            _eprint(
                f"Note: {identity[0]} is your current live login — activate the "
                f"imported credentials with: cswap --switch-to {live_slot} --force"
            )
