"""Path resolution for Claude Code config and credential files.

Mirrors claude-code's own resolution so cswap reads and writes the same files
claude-code does. Key rules (from claude-code source):

- Config home: ``CLAUDE_CONFIG_DIR`` if set, else ``~/.claude``.
- Global config: ``<config_home>/.config.json`` if it exists (legacy),
  otherwise ``(CLAUDE_CONFIG_DIR || $HOME)/.claude.json``. Note the asymmetry:
  ``.claude.json`` sits at homedir by default, not inside ``.claude/``.
- Credentials: ``<config_home>/.credentials.json``.

Also resolves the cswap backup root, which on Linux/WSL follows the XDG Base
Directory Specification (``$XDG_DATA_HOME/claude-swap``) and falls back to the
legacy ``~/.claude-swap-backup`` on macOS/Windows.

References:
- claude-code utils/env.ts getGlobalClaudeFile
- claude-code utils/secureStorage/plainTextStorage.ts getStoragePath
- XDG Base Directory Specification: https://specifications.freedesktop.org/basedir-spec/basedir-spec-latest.html
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from claude_swap.exceptions import MigrationError
from claude_swap.models import Platform

LEGACY_BACKUP_DIRNAME = ".claude-swap-backup"


def get_claude_config_home() -> Path:
    """Return the Claude config home directory (CLAUDE_CONFIG_DIR or ~/.claude)."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        return Path(env)
    return Path.home() / ".claude"


def get_global_config_path() -> Path:
    """Return the path to the global Claude config file.

    Returns the legacy ``<config_home>/.config.json`` if it exists, else
    ``(CLAUDE_CONFIG_DIR || $HOME)/.claude.json``.
    """
    legacy = get_claude_config_home() / ".config.json"
    if legacy.exists():
        return legacy
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(env) if env else Path.home()
    return base / ".claude.json"


def get_credentials_path() -> Path:
    """Return the path to the Claude credentials file."""
    return get_claude_config_home() / ".credentials.json"


def get_legacy_backup_root() -> Path:
    """Return the legacy (pre-XDG) backup root: ``~/.claude-swap-backup``."""
    return Path.home() / LEGACY_BACKUP_DIRNAME


def get_backup_root() -> Path:
    """Return the cswap backup root for the current platform.

    Linux/WSL: ``$XDG_DATA_HOME/claude-swap`` (default ``~/.local/share/claude-swap``).
    macOS/Windows/unknown: ``~/.claude-swap-backup`` (legacy layout).

    Per the XDG spec, ``$XDG_DATA_HOME`` is ignored when unset, empty, or
    non-absolute. A leading ``~`` is expanded so values like ``~/data`` set
    via systemd unit files or Dockerfiles (which don't get shell expansion)
    still work.
    """
    if Platform.detect() in (Platform.LINUX, Platform.WSL):
        xdg = os.environ.get("XDG_DATA_HOME", "")
        if xdg:
            xdg_path = Path(os.path.expanduser(xdg))
            if xdg_path.is_absolute():
                return xdg_path / "claude-swap"
        return Path.home() / ".local" / "share" / "claude-swap"
    return get_legacy_backup_root()


def migrate_legacy_backup_dir(target: Path) -> bool:
    """Move the legacy backup directory to ``target`` if needed.

    Uses ``shutil.move`` (atomic ``rename`` on same FS; copy + unlink across
    FS) guarded by a ``<target>.migrating`` flag file. Touching the flag
    *before* the move and removing it *after* lets us tell an interrupted
    migration apart from a foreign collision on the next run:

    * Flag present, legacy still there → resume (discard any partial target
      and retry).
    * Flag present, legacy gone → previous run completed but didn't get to
      clean the flag; just unlink it.
    * No flag, both paths exist → genuine collision, refuse.

    Returns:
        True if the move ran in this call, False if it was a no-op.

    Raises:
        MigrationError: on a genuine collision, or when ``shutil.move`` fails.
    """
    legacy = get_legacy_backup_root()
    try:
        same_path = legacy.resolve() == target.resolve()
    except OSError:
        same_path = legacy == target
    if same_path:
        return False

    flag = target.parent / f".{target.name}.migrating"

    if not legacy.exists():
        # Successful prior run that died before unlinking the flag.
        flag.unlink(missing_ok=True)
        return False

    if flag.exists():
        # Prior run was interrupted before completion. Discard any
        # (potentially partial) target and retry the move from legacy.
        if target.exists():
            shutil.rmtree(target)
    elif target.exists():
        raise MigrationError(
            f"Both legacy ({legacy}) and new ({target}) backup paths exist. "
            f"Refusing to merge or overwrite — inspect both and remove the "
            f"stale one manually before re-running."
        )

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        flag.touch()
        shutil.move(legacy, target)
        flag.unlink()
    except OSError as exc:
        raise MigrationError(
            f"Migration of {legacy} → {target} failed: {exc}"
        ) from exc

    return True
