"""Install / remove claude-swap's Claude Code hook in ``settings.json``.

Auto-continue hangs off Claude Code's ``StopFailure`` hook event (v2.1.78+),
which fires when a turn ends on an API error; its matcher is the error code,
so ``rate_limit`` scopes it to rate/usage limits. The hook runs
``cswap resume arm`` with the event JSON on stdin.

Edits are surgical: only our own entry (recognized by its command) is added or
removed and every other hook — and every other key — survives. The original
file is snapshotted into the backup dir before each write, so a bad edit is
always one copy away from undone.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from claude_swap.exceptions import ConfigError
from claude_swap.paths import get_claude_config_home

HOOK_EVENT = "StopFailure"
HOOK_MATCHER = "rate_limit"
ARM_ARGS = "resume arm"
# arm only records the stop and spawns the waiter; it returns in about a
# second. Run async so Claude Code never waits on it (StopFailure output is
# ignored anyway); the timeout is a ceiling for a slow disk.
HOOK_TIMEOUT_S = 30
_OUR_MARKERS = ("cswap", "claude_swap", "claude-swap")


@dataclass(frozen=True)
class HookChange:
    changed: bool
    settings_path: Path
    command: str | None = None
    snapshot: Path | None = None


def claude_settings_path() -> Path:
    """User-level Claude Code settings (``<config_home>/settings.json``)."""
    return get_claude_config_home() / "settings.json"


def _shell_word(path: str) -> str:
    # Hooks run under sh/Git Bash, or PowerShell on Windows without Git Bash.
    # A bare path parses the same in all of them; only a path with spaces
    # needs quotes (fine for sh/bash — the shell Claude Code prefers).
    return f'"{path}"' if any(c.isspace() for c in path) else path


def default_hook_command() -> str:
    """Absolute command for the hook: the very cswap installation running
    this install. Not "first cswap on PATH" — a machine can carry several
    (pipx, pip --user, a venv), and the hook must run the one that has this
    feature, independent of the hook shell's PATH."""
    launcher = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if launcher is not None and launcher.stem.lower() in ("cswap", "claude-swap"):
        if launcher.exists():
            return f"{_shell_word(launcher.resolve().as_posix())} {ARM_ARGS}"
    python = _shell_word(Path(sys.executable).as_posix())
    return f"{python} -m claude_swap {ARM_ARGS}"


def is_our_command(command: object) -> bool:
    return (
        isinstance(command, str)
        and command.rstrip().endswith(ARM_ARGS)
        and any(marker in command for marker in _OUR_MARKERS)
    )


def _read(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"could not read {path}: {e}") from e
    try:
        data = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path} is not valid JSON ({e}); fix it before installing") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{path} is not a JSON object; fix it before installing")
    return data


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2) + "\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _snapshot(path: Path, snapshot_dir: Path) -> Path | None:
    if not path.exists():
        return None
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    target = snapshot_dir / f"claude-settings.{time.strftime('%Y%m%d-%H%M%S')}.json"
    shutil.copy2(path, target)
    return target


def installed_command(path: Path | None = None) -> str | None:
    """The command of our installed hook, or ``None`` when not installed."""
    try:
        data = _read(path or claude_settings_path())
    except ConfigError:
        return None
    for group in _event_groups(data):
        for hook in group.get("hooks") or []:
            if isinstance(hook, dict) and is_our_command(hook.get("command")):
                return hook["command"]
    return None


def _event_groups(data: dict) -> list[dict]:
    hooks = data.get("hooks")
    groups = hooks.get(HOOK_EVENT) if isinstance(hooks, dict) else None
    return [g for g in groups if isinstance(g, dict)] if isinstance(groups, list) else []


def install_hook(
    snapshot_dir: Path, command: str | None = None, path: Path | None = None
) -> HookChange:
    """Add (or update) our StopFailure hook; idempotent."""
    path = path or claude_settings_path()
    command = command or default_hook_command()
    data = _read(path)
    hooks = data.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        raise ConfigError(f"{path}: 'hooks' is not an object; fix it before installing")
    hooks = hooks if isinstance(hooks, dict) else {}
    groups = hooks.get(HOOK_EVENT)
    if groups is not None and not isinstance(groups, list):
        raise ConfigError(f"{path}: 'hooks.{HOOK_EVENT}' is not a list; fix it first")
    groups = groups if isinstance(groups, list) else []

    entry = {"type": "command", "command": command, "timeout": HOOK_TIMEOUT_S, "async": True}
    for group in groups:
        if not isinstance(group, dict):
            continue
        for i, hook in enumerate(group.get("hooks") or []):
            if isinstance(hook, dict) and is_our_command(hook.get("command")):
                if hook == entry and group.get("matcher") == HOOK_MATCHER:
                    return HookChange(False, path, command)
                snapshot = _snapshot(path, snapshot_dir)
                group["hooks"][i] = entry
                group["matcher"] = HOOK_MATCHER
                _write(path, data)
                return HookChange(True, path, command, snapshot)

    snapshot = _snapshot(path, snapshot_dir)
    groups.append({"matcher": HOOK_MATCHER, "hooks": [entry]})
    hooks[HOOK_EVENT] = groups
    data["hooks"] = hooks
    _write(path, data)
    return HookChange(True, path, command, snapshot)


def uninstall_hook(snapshot_dir: Path, path: Path | None = None) -> HookChange:
    """Remove our hook entries, pruning only the containers we emptied."""
    path = path or claude_settings_path()
    data = _read(path)
    hooks = data.get("hooks")
    if not isinstance(hooks, dict) or not isinstance(hooks.get(HOOK_EVENT), list):
        return HookChange(False, path)
    removed = False
    kept_groups = []
    for group in hooks[HOOK_EVENT]:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            kept_groups.append(group)
            continue
        kept = [
            h
            for h in group["hooks"]
            if not (isinstance(h, dict) and is_our_command(h.get("command")))
        ]
        if len(kept) != len(group["hooks"]):
            removed = True
            if not kept:
                continue
            group["hooks"] = kept
        kept_groups.append(group)
    if not removed:
        return HookChange(False, path)
    snapshot = _snapshot(path, snapshot_dir)
    if kept_groups:
        hooks[HOOK_EVENT] = kept_groups
    else:
        del hooks[HOOK_EVENT]
        if not hooks:
            del data["hooks"]
    _write(path, data)
    return HookChange(True, path, snapshot=snapshot)
