"""OpenAI Codex CLI accounts: snapshots of ``$CODEX_HOME/auth.json``.

Stored under ``<backup root>/codex/``: ``accounts.json`` maps slot numbers to
``{email, account_id}``, and ``auth-<num>.json`` holds each slot's copy of
auth.json. Token values are never printed or logged.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

from claude_swap import codex_usage, oauth, paths
from claude_swap.exceptions import AccountNotFoundError, ConfigError, SwitchError
from claude_swap.fsutil import atomic_write_text, publish_target
from claude_swap.locking import FileLock
from claude_swap.printer import bold_accent, bolded, dimmed, muted


def auth_path() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "auth.json"


def _store() -> Path:
    return paths.get_backup_root() / "codex"


def _slot_file(num: str) -> Path:
    return _store() / f"auth-{num}.json"


def _load_index() -> dict[str, dict]:
    path = _store() / "accounts.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except ValueError as e:
        raise ConfigError(f"{path} is unreadable: {e}") from e


def _save_index(index: dict) -> None:
    atomic_write_text(_store() / "accounts.json", json.dumps(index, indent=2))


def _read_auth(path: Path) -> tuple[str, dict] | None:
    """(raw text, parsed) of an auth.json, or None when the file is absent."""
    try:
        text = path.read_text(encoding="utf-8")
        return text, json.loads(text)
    except FileNotFoundError:
        return None
    except ValueError as e:
        raise ConfigError(f"{path} is not valid JSON: {e}") from e


def identity(auth: dict) -> tuple[str, str]:
    """(email, account_id) of a ChatGPT login; "" for whatever is missing.

    The email comes from the id_token's payload, decoded without a signature
    check: it only names the slot, it authorizes nothing.
    """
    tokens = auth.get("tokens") or {}
    try:
        payload = tokens["id_token"].split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        email = claims.get("email") or claims.get("https://api.openai.com/profile", {}).get("email")
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        email = None
    return email or "", tokens.get("account_id") or ""


def _slot_of(index: dict, email: str, account_id: str) -> str | None:
    """The slot holding this login: same account_id, or same email when there is no id.

    Email alone never matches when an id is present: one email can own several
    ChatGPT workspaces, and merging them would overwrite a stored login.
    """
    for num, acc in index.items():
        if account_id and acc.get("account_id") == account_id:
            return num
        if not account_id and email and acc.get("email") == email:
            return num
    return None


def _resolve(index: dict, target: str) -> str:
    if target in index:
        return target
    nums = [num for num, acc in index.items() if acc.get("email") == target]
    if len(nums) == 1:
        return nums[0]
    if nums:
        raise AccountNotFoundError(
            f"{target} matches Codex accounts {', '.join(nums)}; use the number"
        )
    raise AccountNotFoundError(f"No Codex account {target} (see `cswap codex list`)")


def add() -> tuple[str, str, bool]:
    """Snapshot the live auth.json into its slot (new or refreshed): (num, email, is_new)."""
    live = _read_auth(auth_path())
    if live is None:
        raise ConfigError(f"No Codex login at {auth_path()}; run `codex login` first")
    text, auth = live
    email, account_id = identity(auth)
    if not (email or account_id):
        raise ConfigError(f"{auth_path()} holds no ChatGPT login (API-key logins are not stored)")
    index = _load_index()
    num = _slot_of(index, email, account_id)
    is_new = num is None
    if is_new:
        num = str(max(map(int, index), default=0) + 1)
    index[num] = {"email": email, "account_id": account_id}
    _store().mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        os.chmod(_store(), 0o700)
    atomic_write_text(_slot_file(num), text)
    _save_index(index)
    return num, email, is_new


def switch(target: str) -> tuple[str, str]:
    """Save the live login back into its slot, then install the target's copy."""
    index = _load_index()
    num = _resolve(index, target)
    live_path = auth_path()
    # Codex's own write protocol (codex-rs/login/src/auth): flock ``auth.lock``
    # in CODEX_HOME for the whole read-modify-write, then ``auth.json.lock``
    # beside the resolved auth.json around the write. Same order, so a token
    # refresh in a running Codex waits for us, then sees the file changed and
    # skips its save instead of writing the old account back.
    with FileLock(live_path.parent / "auth.lock"), \
            FileLock(publish_target(live_path).with_suffix(".json.lock")):
        live = _read_auth(live_path)
        if live is not None:
            live_email, live_id = identity(live[1])
            live_num = _slot_of(index, live_email, live_id)
            if live_num is None:
                raise SwitchError(
                    f"The current Codex login ({live_email or 'unknown account'}) is not "
                    "stored; run `cswap codex add` first so it is not lost"
                )
            # Codex rotates refresh tokens: keep the live copy, not the stale snapshot.
            atomic_write_text(_slot_file(live_num), live[0])
            if live_num == num:
                return num, index[num]["email"]
        stored = _read_auth(_slot_file(num))
        if stored is None:
            raise ConfigError(f"Codex account {num}'s stored login is missing; run `cswap codex add` again")
        atomic_write_text(live_path, stored[0])
    return num, index[num]["email"]


def remove(target: str) -> tuple[str, str]:
    index = _load_index()
    num = _resolve(index, target)
    acc = index.pop(num)
    _save_index(index)
    _slot_file(num).unlink(missing_ok=True)
    return num, acc.get("email", "")


def _usage_lines(windows: list[codex_usage.Window]) -> list[str]:
    """Rows in the shape of the Claude list's usage rows (switcher._usage_entry_lines)."""
    if not windows:
        return [dimmed("usage unavailable")]
    width = max(len(w[0]) for w in windows) + 1  # label + ':'
    out = []
    for j, (label, pct, resets_at) in enumerate(windows):
        body = f"{pct:>3.0f}%"
        cell = oauth.fresh_reset_strings({"resets_at": resets_at})
        if cell:
            body += f"   resets {cell[1]:<12}  in {cell[0]}"
        row = f"{label + ':':<{width}} {body}"
        out.append(f"{dimmed('└' if j == len(windows) - 1 else '├')} {muted(row)}")
    return out


def list_lines() -> list[str]:
    """The "OpenAI accounts:" section; [] when no Codex account is stored."""
    index = _load_index()
    if not index:
        return []
    live = _read_auth(auth_path())
    live_num = _slot_of(index, *identity(live[1])) if live else None
    lines = [bolded("OpenAI accounts:")]
    nums = sorted(index, key=int)
    for i, num in enumerate(nums):
        active = num == live_num
        marker = f" {bold_accent('(active)')}" if active else ""
        lines.append(f"  {num}: {index[num].get('email') or index[num].get('account_id')}{marker}")
        auth = live if active else _read_auth(_slot_file(num))
        tokens = (auth[1] if auth else {}).get("tokens") or {}
        # ponytail: one blocking fetch per account; thread them if long lists get slow.
        windows = (
            codex_usage.fetch_usage(tokens["access_token"], tokens.get("account_id"))
            if tokens.get("access_token")
            else []
        )
        lines += [f"     {line}" for line in _usage_lines(windows)]
        if i < len(nums) - 1:
            lines.append("")
    return lines
