"""Install/migrate account slash commands and two user-bin launch shims."""
from __future__ import annotations
import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from user_path import path_plan, write_user_path, notify_environment

SOURCE = Path(__file__).resolve().parent
BASE = SOURCE.parent
NAMES = ("claude-account-a", "claude-account-b")
MATCHER = "^(claude-account-a|claude-account-b)$"


def hook_entry(matcher: str = MATCHER) -> dict:
    python = (BASE / "venv" / "Scripts" / "python.exe").as_posix()
    hook = (SOURCE / "switch_hook.py").as_posix()
    return {"matcher": matcher, "hooks": [{"type": "command", "command": f'"{python}" -B "{hook}"', "timeout": 90}]}


def build_settings(settings: dict, *, uninstall: bool = False) -> dict:
    if not isinstance(settings, dict):
        raise ValueError("Claude settings must be a JSON object.")
    updated = json.loads(json.dumps(settings))
    if updated.get("disableAllHooks") and not uninstall:
        raise ValueError("Claude hooks are disabled; preserved.")
    hooks = updated.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("Invalid hooks setting; preserved.")
    entries = hooks.get("UserPromptExpansion", [])
    if not isinstance(entries, list):
        raise ValueError("Invalid UserPromptExpansion setting; preserved.")
    wanted = hook_entry()
    owned = (wanted,)
    merged = []
    inserted = False
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Invalid hook entry; preserved.")
        if entry in owned:
            if not uninstall and not inserted:
                merged.append(wanted)
                inserted = True
        elif entry.get("matcher") == MATCHER:
            raise ValueError("A different hook owns these command names; preserved.")
        else:
            merged.append(entry)
    if not uninstall and not inserted:
        merged.append(wanted)
    if merged:
        hooks["UserPromptExpansion"] = merged
    else:
        hooks.pop("UserPromptExpansion", None)
    return updated


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".cswap-account-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def current_bytes(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def managed_files(home: Path) -> dict[Path, bytes]:
    files = {}
    for name in NAMES:
        files[home / ".claude" / "commands" / f"{name}.md"] = (SOURCE / f"{name}.md").read_bytes()
        wrapper = BASE / f"{name}.cmd"
        if not wrapper.is_file():
            raise ValueError(f"Missing launcher: {wrapper.name}")
        shim = f'@echo off\r\nrem Managed by local claude-swap account launcher.\r\ncall "{wrapper}" %*\r\nexit /b %ERRORLEVEL%\r\n'
        files[home / "bin" / f"{name}.cmd"] = shim.encode("utf-8")
    return files


def apply_changes(home: Path, changes: dict[Path, bytes | None], before: dict[Path, bytes | None], *, finish=None, extra_backup=None) -> Path | None:
    if any(current_bytes(path) != before[path] for path in changes):
        raise ValueError("Files changed after validation; nothing overwritten.")
    changes = {path: content for path, content in changes.items() if content != before[path]}
    if not changes and finish is None:
        return None
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = home / ".claude-swap-backup" / f"slash-commands-{timestamp}"
    backup.mkdir(parents=True, exist_ok=False)
    if extra_backup is not None:
        (backup / "user-path-before.json").write_text(json.dumps(extra_backup, indent=2) + "\n", encoding="utf-8")
    manifest = []
    for path in changes:
        relative = path.relative_to(home)
        manifest.append({"path": str(relative), "existed": before[path] is not None})
        if before[path] is not None:
            destination = backup / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(before[path])
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    committed = []
    try:
        for path, content in changes.items():
            if current_bytes(path) != before[path]:
                raise ValueError(f"File changed during installation; preserved: {path.name}")
            if content is None:
                path.unlink()
            else:
                atomic_write(path, content)
            committed.append(path)
        if finish is not None:
            finish()
    except BaseException:
        for path in reversed(committed):
            if current_bytes(path) != changes[path]:
                print(f"Concurrent edit preserved; consult backup for {path.name}.", file=sys.stderr)
                continue
            if before[path] is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, before[path])
        raise
    return backup


def install(home: Path, *, check: bool = False, uninstall: bool = False) -> dict:
    root = home / ".claude"
    settings_path = root / "settings.json"
    settings_before = settings_path.read_bytes()
    settings = json.loads(settings_before.decode("utf-8-sig"))
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir and Path(config_dir).expanduser().resolve() != root.resolve():
        raise ValueError("Run outside a custom Claude config profile.")
    files = managed_files(home)
    before = {target: current_bytes(target) for target in files}
    for target, content in files.items():
        if before[target] is not None and before[target] != content:
            raise ValueError(f"Existing file has different content; preserved: {target}")
    plan = path_plan(home, uninstall=uninstall)
    before[plan["receipt"]] = plan["receipt_before"]
    if not uninstall:
        for name in NAMES:
            if (root / "skills" / name / "SKILL.md").exists():
                raise ValueError(f"A skill already owns the name: {name}")
    updated = build_settings(settings, uninstall=uninstall)
    status = {"ready": True, "installed": updated == settings and all(current_bytes(path) == content for path, content in files.items()) and plan["present"],
              "commands": [f"/{name}" for name in NAMES], "powershell": list(NAMES),
              "shared_default": True, "user_path_change_needed": plan["before"] != plan["after"]}
    if check:
        return status
    changes = {path: None if uninstall else content for path, content in files.items()}
    changes[plan["receipt"]] = plan["receipt_after"]
    # Commit settings last, after command definitions and launch shims.
    if updated != settings:
        changes[settings_path] = (json.dumps(updated, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        before[settings_path] = settings_before
    if settings_path.read_bytes() != settings_before:
        raise ValueError("Settings changed during preparation; nothing overwritten.")
    path_change = plan["before"] != plan["after"]
    finish = (lambda: write_user_path(plan["before"], plan["after"])) if path_change else None
    backup = apply_changes(home, changes, before, finish=finish, extra_backup=plan["before"] if path_change else None)
    if path_change:
        notify_environment()
    return {**status, "installed": not uninstall, "backup": str(backup) if backup else None}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true")
    action.add_argument("--install", action="store_true")
    action.add_argument("--uninstall", action="store_true")
    args = parser.parse_args()
    result = install(Path.home(), check=args.check, uninstall=args.uninstall)
    if args.check:
        print(json.dumps(result, ensure_ascii=True))
        return 0
    print("Uninstalled." if args.uninstall else "Installed.")
    if result["backup"]:
        print("Backup: " + result["backup"])
    if not args.uninstall:
        print("Open a NEW PowerShell window: claude-account-a OR claude-account-b.")
        print("User PATH now includes ~/bin. Existing terminals may need a one-time PATH refresh.")
        print("Keep the original Claude session open. Run /reload-skills, then /claude-account-a or /claude-account-b.")
        print("Existing default-profile Claude windows follow the selected account. No execution-policy changes.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, TypeError) as error:
        print(f"Installation stopped: {error}", file=sys.stderr)
        raise SystemExit(1)
