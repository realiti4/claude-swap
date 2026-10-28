"""Recover Remote Control attachments that failed to download after a switch.

Why. A Remote Control bridge serves the account that started it. Claude Code
downloads a file the phone attached (``GET /api/oauth/files/{uuid}/content``)
with whatever OAuth token is on disk *at that moment*. Once cswap has switched
the default login to another account, the download runs as that other account
and the files endpoint answers 404, because files belong to the user who
uploaded them, not to the organization. Claude Code does not retry a 404. The
user's prompt reaches the model with an ``[attachment did not arrive]``
placeholder instead of the file.

What. This is the handler for a ``UserPromptSubmit`` hook
(``cswap hook attachments``). When the prompt carries the placeholder, it:

1. maps the hook's ``session_id`` to its Remote Control session, using
   ``<config>/sessions/<pid>.json`` (``bridgeSessionId``);
2. lists that session's recent events with each stored account's token until
   one answers. Only the bridge owner can see its session; the others get
   404. ``CLAUDE_CODE_BRIDGE_OWNER_ACCOUNT_UUID`` is tried first when the
   hook inherits it;
3. takes the newest user event that has ``file_attachments`` and matches the
   prompt text, downloads each file with the owner's token into
   ``<config>/uploads/<session_id>/``, and
4. returns the saved paths as ``additionalContext``, which the model sees in
   the same turn.

It never touches the live login. An inactive account's stored token is
refreshed only when it has expired, and is persisted under cswap's lock, the
same way usage polling already does it. The live account's token is only
read; Claude Code owns its refresh.

It fails open: any problem produces no output and exit code 0, so the prompt
goes through unchanged.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable

from claude_swap import oauth
from claude_swap.fsutil import replace_with_retry
from claude_swap.locking import FileLock
from claude_swap.paths import get_claude_config_home

if TYPE_CHECKING:
    from claude_swap.switcher import ClaudeAccountSwitcher

API_BASE = "https://api.anthropic.com"
PLACEHOLDER_RE = re.compile(
    r"^\s*\[(?:attachment did not arrive"
    r"|\d+ attachments did not arrive"
    r"|\d+ of \d+ attachments did not arrive)\]\s*"
)
EVENTS_PAGE = 50
MAX_FILE_BYTES = 30 * 1024 * 1024
HTTP_TIMEOUT_S = 20.0
OWNER_ENV = "CLAUDE_CODE_BRIDGE_OWNER_ACCOUNT_UUID"


@dataclass(frozen=True)
class AccountToken:
    number: str
    email: str
    org_uuid: str
    uuid: str
    access_token: str


@dataclass(frozen=True)
class Attachment:
    file_uuid: str
    file_name: str


Fetch = Callable[[str, dict[str, str], int | None], tuple[int, bytes]]


def http_get(url: str, headers: dict[str, str], limit: int | None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            body = resp.read(limit + 1) if limit is not None else resp.read()
            return resp.status, body
    except urllib.error.HTTPError as e:
        return e.code, b""


def _headers(tok: AccountToken, *, org: bool) -> dict[str, str]:
    h = {
        "Authorization": f"Bearer {tok.access_token}",
        "anthropic-version": "2023-06-01",
        "User-Agent": "claude-swap/1.0",
    }
    if org and tok.org_uuid:
        h["x-organization-uuid"] = tok.org_uuid
    return h


def strip_placeholder(prompt: str) -> tuple[bool, str]:
    m = PLACEHOLDER_RE.match(prompt or "")
    if not m:
        return False, prompt or ""
    return True, (prompt or "")[m.end():]


def bridge_session_for(session_id: str, config_dir: Path) -> str | None:
    """Remote Control session id (``cse_…``) of a local Claude Code session."""
    sessions = config_dir / "sessions"
    if not sessions.is_dir():
        return None
    for path in sessions.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(data, dict) or data.get("sessionId") != session_id:
            continue
        bridge = data.get("bridgeSessionId")
        if isinstance(bridge, str) and bridge:
            # The pid file records the "session_" spelling; the code-sessions
            # API takes the "cse_" one for the same id.
            return "cse_" + bridge.removeprefix("session_").removeprefix("cse_")
    return None


def candidate_tokens(
    switcher: ClaudeAccountSwitcher, owner_uuid: str | None
) -> list[AccountToken]:
    """A usable access token per stored Claude account, owner first.

    Live account: read only (Claude Code owns its refresh). Others: the
    stored copy, refreshed and persisted only when expired.
    """
    data = switcher._get_sequence_data() or {}
    live = switcher._get_current_account()
    out: list[AccountToken] = []
    for num, acct in (data.get("accounts") or {}).items():
        provider = acct.get("provider", "claude")
        if provider != "claude":
            continue
        email = acct.get("email", "")
        org = acct.get("organizationUuid", "") or ""
        is_live = live is not None and live == (email, org)
        creds = switcher._read_credentials() if is_live else (
            switcher.read_account_credentials(num, email)
        )
        if not creds:
            continue
        data_oauth = oauth.extract_oauth_data(creds) or {}
        if oauth.is_oauth_token_expired(data_oauth.get("expiresAt")):
            if is_live:
                continue
            outcome = oauth.try_refresh_oauth_credentials(creds)
            if not outcome.credentials:
                continue
            with FileLock(switcher.lock_file):
                switcher.write_account_credentials(num, email, outcome.credentials)
            data_oauth = oauth.extract_oauth_data(outcome.credentials) or {}
        token = data_oauth.get("accessToken")
        if not token:
            continue
        out.append(
            AccountToken(num, email, org, (acct.get("uuid") or "").strip(), token)
        )
    # Owner first, then the non-live accounts (the live one is the account the
    # failed download already used), then the live one as a last resort.
    live_nums = {
        t.number for t in out if live is not None and live == (t.email, t.org_uuid)
    }
    out.sort(
        key=lambda t: (
            not (owner_uuid and t.uuid == owner_uuid),
            t.number in live_nums,
            int(t.number) if t.number.isdigit() else 0,
        )
    )
    return out


def _walk_attachments(payload: object) -> list[Attachment]:
    found = payload.get("file_attachments") if isinstance(payload, dict) else None
    out = []
    for a in found if isinstance(found, list) else []:
        if isinstance(a, dict) and isinstance(a.get("file_uuid"), str):
            out.append(Attachment(a["file_uuid"], str(a.get("file_name") or "file")))
    return out


def _message_text(payload: dict) -> str:
    msg = payload.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "") for b in content if isinstance(b, dict)
        )
    return ""


def find_attachments(events: Iterable[dict], prompt_text: str) -> list[Attachment]:
    """The newest user event carrying attachments, preferring the one whose
    text matches the prompt (newest first in ``events``)."""
    wanted = prompt_text.strip()
    fallback: list[Attachment] = []
    for ev in events:
        payload = ev.get("payload") if isinstance(ev, dict) else None
        if not isinstance(payload, dict) or payload.get("type") != "user":
            continue
        atts = _walk_attachments(payload)
        if not atts:
            continue
        if wanted and _message_text(payload).strip() == wanted:
            return atts
        if not fallback:
            fallback = atts
    return fallback


def _safe_name(att: Attachment) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(att.file_name).name) or "file"
    prefix = re.sub(r"[^A-Za-z0-9_-]", "_", att.file_uuid[:8])
    return f"{prefix}-{name}"


def recover(
    hook_input: dict,
    switcher: ClaudeAccountSwitcher,
    *,
    fetch: Fetch = http_get,
    config_dir: Path | None = None,
    owner_uuid: str | None = None,
) -> dict | None:
    """Hook output dict, or None when there is nothing to do."""
    if hook_input.get("hook_event_name") not in (None, "UserPromptSubmit"):
        return None
    missing, text = strip_placeholder(str(hook_input.get("prompt") or ""))
    session_id = hook_input.get("session_id")
    if not missing or not isinstance(session_id, str):
        return None
    config_dir = config_dir or get_claude_config_home()
    cse = bridge_session_for(session_id, config_dir)
    if cse is None:
        return None

    events_url = (
        f"{API_BASE}/v1/code/sessions/{urllib.parse.quote(cse)}/events"
        f"?limit={EVENTS_PAGE}&sort_order=desc"
    )
    owner: AccountToken | None = None
    attachments: list[Attachment] = []
    for tok in candidate_tokens(switcher, owner_uuid):
        status, body = fetch(events_url, _headers(tok, org=True), None)
        if status != 200:
            continue  # 404: not this account's session
        try:
            events = json.loads(body).get("data") or []
        except (json.JSONDecodeError, AttributeError):
            return None
        owner = tok
        attachments = find_attachments(events, text)
        break
    if owner is None or not attachments:
        return None

    uploads = config_dir / "uploads" / session_id
    saved: list[str] = []
    failed: list[str] = []
    for att in attachments:
        dest = uploads / _safe_name(att)
        if dest.is_file() and dest.stat().st_size > 0:
            saved.append(str(dest))
            continue
        url = f"{API_BASE}/api/oauth/files/{urllib.parse.quote(att.file_uuid)}/content"
        status, body = fetch(url, _headers(owner, org=False), MAX_FILE_BYTES)
        if status != 200 or not body or len(body) > MAX_FILE_BYTES:
            failed.append(att.file_name)
            continue
        uploads.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        tmp.write_bytes(body)
        replace_with_retry(tmp, dest)
        if os.name != "nt":
            os.chmod(dest, 0o600)
        saved.append(str(dest))

    if not saved:
        return None
    lines = [
        "The attachment placeholder in this message is stale: cswap recovered "
        f"{len(saved)} of {len(attachments)} attached file(s) after the "
        "original download failed. Treat them as attached to this message and "
        "read them before answering:",
        *[f"- {p}" for p in saved],
    ]
    if failed:
        lines.append(f"Still missing: {', '.join(failed)}")
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n".join(lines),
        }
    }


def run_hook(stdin_text: str, switcher_factory: Callable[[], ClaudeAccountSwitcher]) -> str:
    """Entry point for ``cswap hook attachments``. Never raises."""
    try:
        hook_input = json.loads(stdin_text or "{}")
        missing, _ = strip_placeholder(str(hook_input.get("prompt") or ""))
        if not missing:
            return ""  # fast path: no switcher construction on normal prompts
        result = recover(
            hook_input,
            switcher_factory(),
            owner_uuid=os.environ.get(OWNER_ENV) or None,
        )
        return json.dumps(result) if result else ""
    except Exception:
        return ""


# -- settings.json install / uninstall -----------------------------------------

HOOK_EVENT = "UserPromptSubmit"
HOOK_TIMEOUT_S = 60
_OUR_COMMAND = re.compile(r"(cswap|claude[-_]swap)(\.exe)?\W.*\bhook\s+attachments\b", re.I)


class SettingsError(Exception):
    """settings.json is unreadable or not shaped like Claude Code settings."""


def settings_path() -> Path:
    return get_claude_config_home() / "settings.json"


def hook_command() -> str:
    """The command Claude Code runs. An absolute path, so the hook does not
    depend on PATH inside the hook shell. Forward slashes work in both Git
    Bash and cmd on Windows."""
    import shutil
    import sys

    exe = shutil.which("cswap")
    if exe:
        return f'"{Path(exe).as_posix()}" hook attachments'
    return f'"{Path(sys.executable).as_posix()}" -m claude_swap hook attachments'


def _is_ours(handler: object) -> bool:
    return (
        isinstance(handler, dict)
        and handler.get("type") == "command"
        and isinstance(handler.get("command"), str)
        and bool(_OUR_COMMAND.search(handler["command"]))
    )


def _load_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        raise SettingsError(f"cannot read {path}: {e}") from e
    if not isinstance(data, dict):
        raise SettingsError(f"{path} is not a JSON object")
    return data


def _write_settings(path: Path, data: dict) -> Path | None:
    """Back up the current file, then replace it atomically (through a
    symlink, keeping the file's mode). Returns the backup path.

    Not ``settings.atomic_write_json``: that writer hardens cswap's own
    backup dir (0700 parent, 0600 file), which is wrong for Claude Code's
    config dir and the user's settings file. Same symlink and retry rules.
    """
    import shutil
    import tempfile
    import time as _time

    target = Path(os.path.realpath(path)) if path.is_symlink() else path
    backup = None
    if target.exists():
        backup = target.with_name(
            f"{target.name}.cswap-bak-{_time.strftime('%Y%m%d-%H%M%S')}"
        )
        shutil.copy2(target, backup)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        if backup is not None:
            shutil.copymode(backup, tmp)
        replace_with_retry(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return backup


def install_hook(path: Path | None = None, command: str | None = None) -> tuple[str, Path | None]:
    """Add (or update) the hook. Returns (``"installed"|"updated"|"unchanged"``, backup)."""
    path = path or settings_path()
    command = command or hook_command()
    data = _load_settings(path)
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise SettingsError(f"'hooks' in {path} is not an object")
    groups = hooks.setdefault(HOOK_EVENT, [])
    if not isinstance(groups, list):
        raise SettingsError(f"'hooks.{HOOK_EVENT}' in {path} is not a list")

    found = False
    changed = False
    for group in groups:
        handlers = group.get("hooks") if isinstance(group, dict) else None
        if not isinstance(handlers, list):
            continue
        for handler in handlers:
            if not _is_ours(handler):
                continue
            if found:
                continue  # duplicates are removed below
            found = True
            if handler.get("command") != command or handler.get("timeout") != HOOK_TIMEOUT_S:
                handler["command"] = command
                handler["timeout"] = HOOK_TIMEOUT_S
                changed = True
    # Collapse duplicates left by older installs (keep the first).
    seen = False
    for group in groups:
        handlers = group.get("hooks") if isinstance(group, dict) else None
        if not isinstance(handlers, list):
            continue
        kept = []
        for handler in handlers:
            if _is_ours(handler):
                if seen:
                    changed = True
                    continue
                seen = True
            kept.append(handler)
        group["hooks"] = kept
    groups[:] = [g for g in groups if not (isinstance(g, dict) and g.get("hooks") == [])]

    if not found:
        groups.append(
            {"hooks": [{"type": "command", "command": command, "timeout": HOOK_TIMEOUT_S}]}
        )
        return "installed", _write_settings(path, data)
    if changed:
        return "updated", _write_settings(path, data)
    return "unchanged", None


def uninstall_hook(path: Path | None = None) -> tuple[bool, Path | None]:
    """Remove every cswap attachment hook. Returns (removed, backup)."""
    path = path or settings_path()
    data = _load_settings(path)
    hooks = data.get("hooks")
    groups = hooks.get(HOOK_EVENT) if isinstance(hooks, dict) else None
    if not isinstance(groups, list):
        return False, None
    removed = False
    for group in groups:
        handlers = group.get("hooks") if isinstance(group, dict) else None
        if not isinstance(handlers, list):
            continue
        kept = [h for h in handlers if not _is_ours(h)]
        if len(kept) != len(handlers):
            removed = True
            group["hooks"] = kept
    if not removed:
        return False, None
    groups[:] = [g for g in groups if not (isinstance(g, dict) and g.get("hooks") == [])]
    if not groups:
        del hooks[HOOK_EVENT]
    if not hooks:
        del data["hooks"]
    return True, _write_settings(path, data)


def hook_installed(path: Path | None = None) -> bool:
    try:
        data = _load_settings(path or settings_path())
    except SettingsError:
        return False
    groups = (data.get("hooks") or {}).get(HOOK_EVENT) if isinstance(data.get("hooks"), dict) else None
    return any(
        _is_ours(h)
        for g in (groups if isinstance(groups, list) else [])
        if isinstance(g, dict) and isinstance(g.get("hooks"), list)
        for h in g["hooks"]
    )
