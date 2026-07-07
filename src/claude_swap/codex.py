"""Codex account tracking and auth switching."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

from claude_swap import oauth
from claude_swap.exceptions import AccountNotFoundError, ConfigError, ValidationError
from claude_swap.json_output import SCHEMA_VERSION, usage_freshness_fields
from claude_swap.locking import FileLock
from claude_swap.models import get_timestamp
from claude_swap.paths import (
    get_backup_root,
    get_codex_auth_path,
    get_legacy_backup_root,
    migrate_legacy_backup_dir,
)
from claude_swap.printer import accent, bolded, dimmed, format_age, muted
from claude_swap.usage_store import FetchRecord, UsageEntry, UsageStore


class _AuthMetadata(NamedTuple):
    account_id: str
    auth_mode: str
    fingerprint: str
    access_token: str


class _UsageFetchError(NamedTuple):
    """An HTTP usage-fetch failure carrying the server's ``Retry-After`` (if any)
    so ``UsageStore`` can honor the burst-block wait rather than only the default
    exponential backoff."""

    message: str
    retry_after_s: float | None


CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CODEX_USAGE_TIMEOUT_S = 10.0
_USAGE_AGE_NOTE_S = 90.0


def _atomic_write_text(path: Path, text: str) -> None:
    # mkstemp creates the temp at 0600 from the first byte, so the auth token
    # never has a world-readable window (write_text would create it 0644 under
    # the default umask). Mirrors credentials._write_active_credentials_file /
    # settings.atomic_write_json.
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        os.write(fd, text.encode("utf-8"))
        os.close(fd)
        fd = -1
        os.replace(tmp_name, str(path))
        if sys.platform != "win32":
            os.chmod(str(path), 0o600)
    except OSError as exc:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise ConfigError(f"Failed to write {path}: {exc}") from exc


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(data, indent=2) + "\n")


def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _safe_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _unix_seconds_to_iso(value: Any) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    return (
        datetime.fromtimestamp(value, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _clamp_percent(value: Any) -> float:
    pct = float(value) if isinstance(value, (int, float)) else 0.0
    return min(100.0, max(0.0, pct))


def _codex_window_label(window: dict[str, Any]) -> str:
    seconds = window.get("limit_window_seconds")
    if not isinstance(seconds, (int, float)):
        return "?"
    hours = round(seconds / 3600)
    if hours >= 168:
        return "Week"
    if hours >= 24:
        return "Day"
    return f"{hours}h"


def _parse_codex_usage(data: dict[str, Any]) -> dict[str, Any]:
    rate_limit = data.get("rate_limit") if isinstance(data.get("rate_limit"), dict) else {}
    windows = []
    for key in ("primary_window", "secondary_window"):
        window = rate_limit.get(key) if isinstance(rate_limit, dict) else None
        if not isinstance(window, dict):
            continue
        entry: dict[str, Any] = {
            "label": _codex_window_label(window),
            "pct": _clamp_percent(window.get("used_percent")),
        }
        resets_at = _unix_seconds_to_iso(window.get("reset_at"))
        if resets_at is not None:
            entry["resets_at"] = resets_at
        windows.append(entry)

    usage: dict[str, Any] = {"windows": windows}
    plan = _safe_str(data.get("plan_type"))
    if plan:
        usage["plan"] = plan
    credits = data.get("credits") if isinstance(data.get("credits"), dict) else {}
    balance = credits.get("balance") if isinstance(credits, dict) else None
    if isinstance(balance, (int, float)):
        usage["credits"] = float(balance)
    elif isinstance(balance, str):
        try:
            usage["credits"] = float(balance)
        except ValueError:
            pass
    return usage


def _format_codex_usage_lines(usage: dict[str, Any]) -> list[str]:
    rows: list[tuple[str, str]] = []
    for window in usage.get("windows") or []:
        if not isinstance(window, dict):
            continue
        label = _safe_str(window.get("label"))
        pct = window.get("pct")
        if not label or not isinstance(pct, (int, float)):
            continue
        body = f"{pct:>3.0f}%"
        cell = oauth.fresh_reset_strings(window)
        if cell:
            countdown, clock = cell
            body = f"{body}   resets {clock:<12}  in {countdown}"
        rows.append((label, body))
    plan = _safe_str(usage.get("plan"))
    if plan:
        rows.append(("Plan", plan))
    credits = usage.get("credits")
    if isinstance(credits, (int, float)):
        rows.append(("Credits", f"{credits:g}"))
    width = max((len(label) for label, _ in rows), default=0) + 1
    return [f"{label + ':':<{width}} {body}" for label, body in rows]


def _codex_window_to_json(window: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "label": _safe_str(window.get("label")),
        "pct": window["pct"],
    }
    if "resets_at" in window:
        out["resetsAt"] = window["resets_at"]
    cell = oauth.fresh_reset_strings(window)
    if cell:
        out["countdown"], out["clock"] = cell
    return out


def _codex_usage_to_json(usage: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    windows: list[dict[str, Any]] = []
    for window in usage.get("windows") or []:
        if not isinstance(window, dict):
            continue
        pct = window.get("pct")
        if _safe_str(window.get("label")) and isinstance(pct, (int, float)):
            windows.append(_codex_window_to_json(window))
    if windows:
        out["windows"] = windows
    plan = _safe_str(usage.get("plan"))
    if plan:
        out["plan"] = plan
    credits = usage.get("credits")
    if isinstance(credits, (int, float)):
        out["credits"] = credits
    return out


def _codex_usage_fields(entry: UsageEntry) -> tuple[str, dict[str, Any] | None]:
    usage = entry.decision_value()
    if isinstance(usage, dict):
        return "ok", _codex_usage_to_json(usage)
    return "unavailable", None


def _retry_after_seconds(exc: urllib.error.HTTPError) -> float | None:
    raw = exc.headers.get("Retry-After") if exc.headers else None
    if not raw:
        return None
    try:
        return max(0.0, float(raw.strip()))
    except ValueError:
        return None


def fetch_codex_usage(
    auth_text: str, timeout_s: float
) -> dict[str, Any] | str | _UsageFetchError:
    metadata = _metadata_from_text(auth_text)
    if not metadata.access_token:
        return "no access token"
    headers = {
        "Authorization": f"Bearer {metadata.access_token}",
        "Accept": "application/json",
        "originator": "claude-swap",
        "User-Agent": "claude-swap/codex-usage",
    }
    if metadata.account_id:
        headers["ChatGPT-Account-Id"] = metadata.account_id
    request = urllib.request.Request(CODEX_USAGE_URL, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        retry_after = _retry_after_seconds(exc)
        if exc.code in (401, 403):
            return _UsageFetchError("token expired", retry_after)
        return _UsageFetchError(f"HTTP {exc.code}", retry_after)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return str(exc) or "usage unavailable"

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return "malformed usage response"
    if not isinstance(data, dict):
        return "malformed usage response"
    return _parse_codex_usage(data)


def _metadata_from_text(text: str) -> _AuthMetadata:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Codex auth file is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("Codex auth file must contain a JSON object")
    tokens = data.get("tokens")
    token_data = tokens if isinstance(tokens, dict) else {}
    token_fields = ("account_id", "access_token", "refresh_token", "id_token")
    has_token = any(_safe_str(token_data.get(field)) for field in token_fields)
    has_key = any(
        _safe_str(data.get(field))
        for field in ("openai_api_key", "personal_access_token", "bedrock_api_key")
    )
    if not has_token and not has_key:
        raise ConfigError("Codex auth file does not contain a supported Codex credential")
    return _AuthMetadata(
        account_id=_safe_str(token_data.get("account_id")),
        auth_mode=_safe_str(data.get("auth_mode")),
        fingerprint=_fingerprint(text),
        access_token=_safe_str(token_data.get("access_token")),
    )


class CodexAccountSwitcher:
    """Independent account switcher for Codex CLI auth snapshots."""

    def __init__(self) -> None:
        self.backup_root = get_backup_root()
        if migrate_legacy_backup_dir(self.backup_root):
            legacy = get_legacy_backup_root()
            print(
                f"claude-swap: migrated data from {legacy} to {self.backup_root}",
                file=sys.stderr,
            )
        self.codex_dir = self.backup_root / "codex"
        self.auth_dir = self.codex_dir / "auth"
        self.sequence_file = self.codex_dir / "sequence.json"
        self.lock_file = self.codex_dir / ".lock"
        self.auth_path = get_codex_auth_path()
        self._usage_store = UsageStore(self.codex_dir / "cache")

    def _setup_directories(self) -> None:
        for directory in (self.codex_dir, self.auth_dir):
            directory.mkdir(parents=True, exist_ok=True)
            if sys.platform != "win32":
                os.chmod(directory, 0o700)

    def _read_json(self, path: Path) -> dict[str, Any] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except json.JSONDecodeError as exc:
            raise ConfigError(f"Codex state file is not valid JSON ({path}): {exc}") from exc
        except OSError as exc:
            raise ConfigError(f"Failed to read Codex state file {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"Codex state file must contain a JSON object: {path}")
        return data

    def _write_json(self, path: Path, data: dict[str, Any]) -> None:
        _atomic_write_json(path, data)

    def _init_sequence_file(self) -> None:
        if self.sequence_file.exists():
            return
        self._write_json(
            self.sequence_file,
            {
                "activeAccountNumber": None,
                "lastUpdated": get_timestamp(),
                "sequence": [],
                "accounts": {},
            },
        )

    def _sequence_data(self) -> dict[str, Any]:
        data = self._read_json(self.sequence_file)
        if data is None:
            return {
                "activeAccountNumber": None,
                "lastUpdated": get_timestamp(),
                "sequence": [],
                "accounts": {},
            }
        sequence = data.setdefault("sequence", [])
        accounts = data.setdefault("accounts", {})
        if not isinstance(sequence, list) or not all(
            isinstance(num, int) for num in sequence
        ):
            raise ConfigError(f"Codex state sequence must be a list of numbers: {self.sequence_file}")
        if not isinstance(accounts, dict):
            raise ConfigError(f"Codex state accounts must be an object: {self.sequence_file}")
        return data

    def _auth_backup_path(self, account_num: str) -> Path:
        return self.auth_dir / f"account-{account_num}.json"

    def _read_active_auth(self) -> str | None:
        try:
            text = self.auth_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ConfigError(f"Failed to read Codex auth file {self.auth_path}: {exc}") from exc
        return text if text.strip() else None

    def _read_required_active_auth(self) -> str:
        text = self._read_active_auth()
        if text is None:
            raise ConfigError(
                f"No active Codex auth found at {self.auth_path}. Run 'codex login' first."
            )
        return text

    def _write_active_auth(self, text: str) -> None:
        _atomic_write_text(self.auth_path, text)

    def _read_account_auth(self, account_num: str) -> str:
        try:
            return self._auth_backup_path(account_num).read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ConfigError(
                f"Codex Account-{account_num} has no stored auth. Re-add it with: "
                f"cswap codex add --slot {account_num}"
            ) from exc
        except OSError as exc:
            raise ConfigError(f"Failed to read Codex Account-{account_num} auth: {exc}") from exc

    def _write_account_auth(self, account_num: str, text: str) -> None:
        _atomic_write_text(self._auth_backup_path(account_num), text)

    def _metadata(self, text: str) -> _AuthMetadata:
        return _metadata_from_text(text)

    def _stored_metadata(self, account_num: str, text: str) -> _AuthMetadata:
        try:
            return self._metadata(text)
        except ConfigError as exc:
            raise ConfigError(
                f"stored auth for Codex Account-{account_num} is invalid: {exc}"
            ) from exc

    def _next_account_number(self, data: dict[str, Any]) -> int:
        accounts = data.get("accounts", {})
        numbers = [int(num) for num in accounts.keys()]
        return max(numbers, default=0) + 1

    def _derive_label(
        self, label: str | None, metadata: _AuthMetadata, account_num: str
    ) -> str:
        if label is not None:
            normalized = label.strip()
            if not normalized:
                raise ValidationError("Codex account label cannot be empty")
            if normalized.isdigit():
                raise ValidationError("Codex account label cannot be only digits")
            return normalized
        if metadata.account_id:
            return metadata.account_id
        return f"codex-account-{account_num}"

    def _account_by_label(self, data: dict[str, Any], label: str) -> str | None:
        for num, account in data.get("accounts", {}).items():
            if account.get("label") == label:
                return num
        return None

    def _current_account_number(
        self, data: dict[str, Any], active_auth: str | None
    ) -> str | None:
        if active_auth is None:
            active_auth = self._read_active_auth()
        if active_auth is None:
            return None
        metadata = self._metadata(active_auth)
        accounts = data.get("accounts", {})
        if metadata.account_id:
            for num, account in accounts.items():
                if account.get("accountId") == metadata.account_id:
                    return num
        for num, account in accounts.items():
            if account.get("fingerprint") == metadata.fingerprint:
                return num
        return None

    def _resolve_account_identifier(
        self, data: dict[str, Any], identifier: str
    ) -> str | None:
        accounts = data.get("accounts", {})
        if identifier.isdigit():
            return identifier if identifier in accounts else None
        return self._account_by_label(data, identifier)

    def _set_account_record(
        self,
        data: dict[str, Any],
        account_num: str,
        label: str,
        metadata: _AuthMetadata,
    ) -> None:
        existing = data.get("accounts", {}).get(account_num, {})
        data["accounts"][account_num] = {
            "label": label,
            "accountId": metadata.account_id,
            "authMode": metadata.auth_mode,
            "fingerprint": metadata.fingerprint,
            "added": existing.get("added") or get_timestamp(),
        }
        numeric = int(account_num)
        if numeric not in data["sequence"]:
            data["sequence"].append(numeric)
            data["sequence"].sort()

    def _account_ref(self, data: dict[str, Any], account_num: str | None) -> dict | None:
        if account_num is None:
            return None
        account = data.get("accounts", {}).get(account_num)
        if not account:
            return None
        return {"number": int(account_num), "label": account.get("label", "")}

    def has_accounts(self) -> bool:
        return bool(self._sequence_data().get("accounts"))

    def add_account(self, label: str | None, slot: int | None) -> None:
        active_auth = self._read_required_active_auth()
        metadata = self._metadata(active_auth)
        self._setup_directories()
        self._init_sequence_file()

        with FileLock(self.lock_file):
            data = self._sequence_data()
            existing_num = self._current_account_number(data, active_auth)
            if slot is None:
                account_num = existing_num or str(self._next_account_number(data))
            else:
                if slot < 1:
                    raise ConfigError("Codex slot number must be >= 1")
                account_num = str(slot)
                # existing_num already covers both auth modes (accountId match,
                # else fingerprint), so the guard must not gate on account_id —
                # api-key auth has none and would otherwise duplicate across slots.
                if existing_num is not None and existing_num != account_num:
                    raise ValidationError(
                        f"This Codex auth is already stored as Codex Account-{existing_num}"
                    )
                if (
                    account_num in data.get("accounts", {})
                    and existing_num != account_num
                ):
                    raise ConfigError(
                        f"Codex Account-{account_num} already exists. Remove it first "
                        f"or choose another slot."
                    )

            existing_account = data.get("accounts", {}).get(account_num, {})
            if label is None and existing_account:
                resolved_label = existing_account.get("label", "")
            else:
                resolved_label = self._derive_label(label, metadata, account_num)
            duplicate = self._account_by_label(data, resolved_label)
            if duplicate is not None and duplicate != account_num:
                raise ValidationError(
                    f"Codex account label '{resolved_label}' already exists as Account-{duplicate}"
                )

            self._write_account_auth(account_num, active_auth)
            self._set_account_record(data, account_num, resolved_label, metadata)
            data["activeAccountNumber"] = int(account_num)
            data["lastUpdated"] = get_timestamp()
            self._write_json(self.sequence_file, data)

        action = "Updated" if existing_num == account_num else "Added"
        print(f"{accent(action)} Codex Account-{account_num}: {resolved_label}")

    def _usage_identities(self, data: dict[str, Any]) -> dict[str, tuple[str, str]]:
        identities: dict[str, tuple[str, str]] = {}
        for num, account in data.get("accounts", {}).items():
            account_id = _safe_str(account.get("accountId"))
            fingerprint = _safe_str(account.get("fingerprint"))
            label = _safe_str(account.get("label"))
            if account_id and fingerprint:
                identities[num] = (f"{account_id}:{fingerprint}", "")
            else:
                identities[num] = (account_id or fingerprint or label or num, "")
        return identities

    def _fetch_usage_record(self, account_num: str) -> FetchRecord:
        try:
            auth_text = self._read_account_auth(account_num)
            usage = fetch_codex_usage(auth_text, CODEX_USAGE_TIMEOUT_S)
        except ConfigError as exc:
            return FetchRecord(error=str(exc))
        if isinstance(usage, dict):
            return FetchRecord(usage=usage)
        if isinstance(usage, _UsageFetchError):
            return FetchRecord(error=usage.message, retry_after_s=usage.retry_after_s)
        return FetchRecord(error=usage or "usage unavailable")

    def _collect_usage_entries(self, data: dict[str, Any]) -> dict[str, UsageEntry]:
        identities = self._usage_identities(data)
        if not identities:
            return {}
        store = self._usage_store
        now = store.clock()
        entries = store.entries(identities)
        to_fetch = [
            num
            for num in sorted(identities.keys(), key=int)
            if not entries[num].fresh(now)
            and not entries[num].in_backoff(now)
            and not entries[num].claimed(now)
        ]
        if to_fetch:
            store.claim(to_fetch, identities)
            store.record(
                {num: self._fetch_usage_record(num) for num in to_fetch},
                identities,
            )
            entries = store.entries(identities)
        return entries

    def _build_list_payload(
        self, data: dict[str, Any], entries: dict[str, UsageEntry]
    ) -> dict:
        active_num = self._current_account_number(data, None)
        accounts = []
        for num in sorted(data.get("accounts", {}).keys(), key=int):
            account = data["accounts"][num]
            entry = entries.get(num, UsageEntry())
            usage_status, usage = _codex_usage_fields(entry)
            row = {
                "number": int(num),
                "label": account.get("label", ""),
                "active": num == active_num,
                "usageStatus": usage_status,
                "usage": usage,
            }
            if usage is not None:
                row.update(usage_freshness_fields(entry.fetched_at, entry.age_s))
            elif entry.last_error:
                # "unavailable" alone can't tell a permanently-expired stored
                # token from a transient network blip; surface the detail.
                row["usageError"] = entry.last_error
            accounts.append(row)
        return {
            "schemaVersion": SCHEMA_VERSION,
            "provider": "codex",
            "activeAccountNumber": int(active_num) if active_num is not None else None,
            "accounts": accounts,
        }

    def _codex_usage_lines(self, entry: UsageEntry) -> list[str]:
        if entry.last_good is not None:
            lines = _format_codex_usage_lines(entry.last_good)
            if (
                lines
                and entry.age_s is not None
                and entry.age_s > _USAGE_AGE_NOTE_S
                and entry.fetched_at is not None
            ):
                lines[-1] += f" · {format_age(int(entry.fetched_at * 1000))}"
            if lines:
                return [
                    f"{dimmed('└' if j == len(lines) - 1 else '├')} {muted(line)}"
                    for j, line in enumerate(lines)
                ]
        detail = "usage unavailable"
        if entry.last_error:
            detail += f" ({entry.last_error})"
        return [dimmed(detail)]

    def list_accounts(self, json_output: bool) -> dict | None:
        data = self._sequence_data()
        entries = self._collect_usage_entries(data)
        if json_output:
            return self._build_list_payload(data, entries)

        accounts = data.get("accounts", {})
        if not accounts:
            print(dimmed("No Codex accounts are managed yet."))
            return None

        payload = self._build_list_payload(data, entries)
        print(bolded("Codex accounts:"))
        for account in payload["accounts"]:
            marker = f" {accent('(active)')}" if account["active"] else ""
            print(f"  {account['number']}: {account['label']}{marker}")
            entry = entries.get(str(account["number"]), UsageEntry())
            for line in self._codex_usage_lines(entry):
                print(f"     {line}")
        return None

    def status(self, json_output: bool) -> dict | None:
        data = self._sequence_data()
        active_auth = self._read_active_auth()
        if active_auth is None:
            payload = {"schemaVersion": SCHEMA_VERSION, "provider": "codex", "active": None}
        else:
            current_num = self._current_account_number(data, active_auth)
            if current_num is None:
                payload = {
                    "schemaVersion": SCHEMA_VERSION,
                    "provider": "codex",
                    "active": {"managed": False},
                }
            else:
                account = data["accounts"][current_num]
                payload = {
                    "schemaVersion": SCHEMA_VERSION,
                    "provider": "codex",
                    "active": {
                        "number": int(current_num),
                        "label": account.get("label", ""),
                        "managed": True,
                    },
                    "totalManagedAccounts": len(data.get("accounts", {})),
                }
        if json_output:
            return payload

        active = payload["active"]
        if active is None:
            print(f"{bolded('Codex status:')} {dimmed('No active Codex auth')}")
        elif active.get("managed"):
            active_label = accent(f"Account-{active['number']}")
            print(
                f"{bolded('Codex status:')} "
                f"{active_label} ({active['label']})"
            )
        else:
            print(f"{bolded('Codex status:')} {muted('(not managed)')}")
        return None

    def _rotation_target(self, data: dict[str, Any]) -> str | None:
        sequence = data.get("sequence", [])
        if not sequence:
            return None
        if len(sequence) == 1:
            return str(sequence[0])
        current_num = self._current_account_number(data, None)
        if current_num is None:
            active = data.get("activeAccountNumber")
            current_num = str(active) if active is not None else str(sequence[0])
        try:
            current_index = sequence.index(int(current_num))
        except ValueError:
            current_index = 0
        return str(sequence[(current_index + 1) % len(sequence)])

    def switch(self, identifier: str | None, json_output: bool) -> dict | None:
        self._setup_directories()
        with FileLock(self.lock_file):
            data = self._sequence_data()
            if not data.get("accounts"):
                raise ConfigError("No Codex accounts are managed yet")

            target_account = (
                self._rotation_target(data)
                if identifier is None
                else self._resolve_account_identifier(data, identifier)
            )
            if target_account is None:
                raise AccountNotFoundError(f"No Codex account found with identifier: {identifier}")
            if target_account not in data.get("accounts", {}):
                raise AccountNotFoundError(f"Codex Account-{target_account} does not exist")

            active_auth = self._read_active_auth()
            current_num = self._current_account_number(data, active_auth) if active_auth else None
            from_ref = self._account_ref(data, current_num)
            to_ref = self._account_ref(data, target_account)
            if current_num == target_account:
                result = {
                    "schemaVersion": SCHEMA_VERSION,
                    "provider": "codex",
                    "switched": False,
                    "from": from_ref,
                    "to": to_ref,
                    "reason": "already-active",
                    "message": f"Already on Codex Account-{target_account}",
                }
                if json_output:
                    return result
                print(f"{accent('Already on')} Codex Account-{target_account}")
                return None

            if active_auth is not None and current_num is not None:
                metadata = self._metadata(active_auth)
                current_label = data["accounts"][current_num].get("label", "")
                self._write_account_auth(current_num, active_auth)
                self._set_account_record(data, current_num, current_label, metadata)

            target_auth = self._read_account_auth(target_account)
            self._stored_metadata(target_account, target_auth)
            had_active_auth = active_auth is not None
            self._write_active_auth(target_auth)
            try:
                data["activeAccountNumber"] = int(target_account)
                data["lastUpdated"] = get_timestamp()
                self._write_json(self.sequence_file, data)
            except ConfigError:
                if had_active_auth and active_auth is not None:
                    self._write_active_auth(active_auth)
                else:
                    self.auth_path.unlink(missing_ok=True)
                raise

        label = data["accounts"][target_account].get("label", "")
        result = {
            "schemaVersion": SCHEMA_VERSION,
            "provider": "codex",
            "switched": True,
            "from": from_ref,
            "to": to_ref,
            "reason": "switched",
            "message": f"Switched Codex to Account-{target_account} ({label})",
        }
        if json_output:
            return result
        print(f"{accent('Switched Codex to')} Account-{target_account} ({label})")
        return None

    def remove_account(self, identifier: str) -> None:
        self._setup_directories()
        with FileLock(self.lock_file):
            data = self._sequence_data()
            account_num = self._resolve_account_identifier(data, identifier)
            if account_num is None:
                raise AccountNotFoundError(
                    f"No Codex account found with identifier: {identifier}"
                )
            account = data.get("accounts", {}).get(account_num)
            if account is None:
                raise AccountNotFoundError(f"Codex Account-{account_num} does not exist")

            self._auth_backup_path(account_num).unlink(missing_ok=True)
            data["accounts"].pop(account_num, None)
            numeric = int(account_num)
            if numeric in data.get("sequence", []):
                data["sequence"].remove(numeric)
            if data.get("activeAccountNumber") == numeric:
                data["activeAccountNumber"] = None
            data["lastUpdated"] = get_timestamp()
            self._write_json(self.sequence_file, data)

        print(f"{accent('Removed')} Codex Account-{account_num}: {account.get('label', '')}")
