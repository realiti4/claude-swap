"""Claude Code hook: enforce work / personal contexts (``cswap hook``).

Installed by ``cswap context install-hooks`` for two events:

``SessionStart``
    Tells Claude (and, when the account is not allowed here, the user) which
    context the directory is in and which account is active.
``UserPromptSubmit``
    Blocks the prompt when the active account is not allowed in this
    directory (see :mod:`claude_swap.contexts` for the rules).

The active account is read from Claude Code's own config
(``(CLAUDE_CONFIG_DIR || $HOME)/.claude.json``), so the check follows the
login of THIS session even when it runs under ``cswap run`` or after an
auto-switch.

Fails open: a hook that crashes or cannot read its inputs prints nothing and
exits 0, so a broken install never locks the user out of Claude Code. The
auto-switch engine applies the same rules on its own side.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import TextIO

from claude_swap import paths
from claude_swap.contexts import ContextStore
from claude_swap.fsutil import atomic_write_text

SESSION_START = "SessionStart"
USER_PROMPT_SUBMIT = "UserPromptSubmit"
EVENTS = (SESSION_START, USER_PROMPT_SUBMIT)


def current_identity() -> tuple[str, str] | None:
    """``(email, organization_uuid)`` of the live login, or None."""
    try:
        with open(paths.get_global_config_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    oauth = data.get("oauthAccount") if isinstance(data, dict) else None
    if not isinstance(oauth, dict) or not oauth.get("emailAddress"):
        return None
    return oauth["emailAddress"], oauth.get("organizationUuid") or ""


def build_response(event: str, cwd: str, store: ContextStore) -> dict | None:
    """The hook's JSON reply for ``event`` in ``cwd``, or None for no output."""
    identity = current_identity()
    if identity is None:
        return None  # API key / logged out: nothing to judge
    email, org = identity
    resolution, acct_ctx, verdict = store.verdict(cwd, email, org)
    mode = store.mode()
    acct_label = acct_ctx or "untagged, counts as personal"

    blocked_msg = (
        f"claude-swap: {verdict.reason} ({email}). "
        f"Switch account (`cswap switch`), relaunch with `cswap run <account>`, "
        f"or change the rules (`cswap context`)."
    )

    if event == SESSION_START:
        context = (
            f"[claude-swap] This directory is in the {resolution.context.upper()} "
            f"context ({resolution.describe()}). Active Claude account: {email} "
            f"({acct_label}). Barrier mode: {mode}. Keep work and personal "
            f"matters apart: when saving memories or notes, record which "
            f"context they belong to."
        )
        reply: dict = {
            "hookSpecificOutput": {
                "hookEventName": SESSION_START,
                "additionalContext": context,
            }
        }
        if not verdict.allowed:
            reply["systemMessage"] = "⚠ " + blocked_msg + " Prompts will be blocked."
        return reply

    if event == USER_PROMPT_SUBMIT and not verdict.allowed:
        return {"decision": "block", "reason": blocked_msg}
    return None


def _is_ours(entry: object, command: str) -> bool:
    """Whether a settings.json hook group runs ``command``."""
    if not isinstance(entry, dict):
        return False
    hooks = entry.get("hooks")
    return isinstance(hooks, list) and any(
        isinstance(h, dict) and h.get("command") == command for h in hooks
    )


def install(settings_file: Path, command: str, *, uninstall: bool = False) -> bool:
    """Add (or remove) the hook for every event in ``settings_file``.

    Merges into the existing file, keeps every other key and hook, and is
    idempotent. Writes through a symlinked settings.json (dotfile repos, cswap
    session profiles) instead of replacing the link. Returns whether the file
    changed.

    Raises:
        ValueError: the file exists but is not a JSON object.
    """
    target = settings_file.resolve()
    if target.exists():
        data = json.loads(target.read_text(encoding="utf-8") or "{}")
        if not isinstance(data, dict):
            raise ValueError(f"{target} is not a JSON object")
    else:
        data = {}
    before = json.dumps(data, sort_keys=True)

    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    for event in EVENTS:
        groups = [g for g in hooks.get(event, []) if not _is_ours(g, command)]
        if not uninstall:
            groups.append({"hooks": [{"type": "command", "command": command}]})
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    if hooks:
        data["hooks"] = hooks
    else:
        data.pop("hooks", None)

    if json.dumps(data, sort_keys=True) == before:
        return False
    mode = (target.stat().st_mode & 0o777) if target.exists() else 0o644
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target, json.dumps(data, indent=2) + "\n", mode=mode)
    return True


def run(stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> int:
    """Entry point for ``cswap hook``. Always returns 0 (fail open)."""
    try:
        payload = json.load(stdin)
        if not isinstance(payload, dict):
            return 0
        event = payload.get("hook_event_name", "")
        if event not in EVENTS:
            return 0
        cwd = payload.get("cwd") or os.getcwd()
        reply = build_response(event, cwd, ContextStore(paths.get_backup_root()))
        if reply is not None:
            stdout.write(json.dumps(reply))
            stdout.flush()
    except Exception as e:  # noqa: BLE001 - a hook must never take Claude down
        print(f"claude-swap hook: {e}", file=sys.stderr)
    return 0
