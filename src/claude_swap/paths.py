"""Path resolution for Claude Code config and credential files.

Mirrors claude-code's own resolution so cswap reads and writes the same files
claude-code does. Key rules (from claude-code source):

- Config home: ``CLAUDE_CONFIG_DIR`` if set, else ``~/.claude``.
- Global config: ``<config_home>/.config.json`` if it exists (legacy),
  otherwise ``(CLAUDE_CONFIG_DIR || $HOME)/.claude.json``. Note the asymmetry:
  ``.claude.json`` sits at homedir by default, not inside ``.claude/``.
- Credentials: ``<config_home>/.credentials.json``.

References:
- claude-code utils/env.ts getGlobalClaudeFile
- claude-code utils/secureStorage/plainTextStorage.ts getStoragePath
"""

from __future__ import annotations

import os
from pathlib import Path


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
