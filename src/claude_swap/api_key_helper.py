"""Carry an API-key account switch into *already running* Claude Code sessions.

Switching to an OAuth account lands in a running session for free: Claude Code
reads ``.credentials.json`` uncached on every auth resolution and watches the
file's mtime, dropping its memoized token when it changes. Rewriting the file is
the whole mechanism (it is what #86 leans on).

The managed-API-key axis has no such property:

- ``primaryApiKey`` is read out of an in-memory snapshot of ``~/.claude.json``
  that Claude Code never re-stats on the read path, and
- the resolved key is additionally memoized for the lifetime of the process.
  That memo is cleared only by the *same* process saving or removing a key (its
  own ``/login`` / ``/logout``) — never by an outside writer.

So activating an API-key account used to be invisible to every running session,
and *worse* than invisible: the OAuth credential is cleared for mutual exclusion,
the credentials-file watcher fires, and the session drops to "Login expired —
please run /login" while the fresh key sits unread in ``~/.claude.json``.

``apiKeyHelper`` is the one API-key channel Claude Code re-reads at runtime. It
is a settings hook, and Claude Code both (a) watches the settings files and drops
its settings cache when one changes, and (b) awaits the helper while building the
client for a request, re-running it once the cached value ages past
``CLAUDE_CODE_API_KEY_HELPER_TTL_MS`` (default 5 minutes). Pointing it at a
script that prints the active managed key therefore makes an API-key switch land
in a running session the same way an OAuth switch does.

Two constraints shape the implementation:

- **The hook is toggled, never left installed.** A configured ``apiKeyHelper``
  outranks the claude.ai OAuth credential in Claude Code's source resolution
  (it forces the API-key axis for the whole session), so leaving it in place
  would pin every session to the key and break the OAuth accounts. It is
  installed when an API-key account is activated and removed when an OAuth one
  is — including the activation ``add_account`` performs when it captures a live
  ``/login`` as a new account, which Claude Code cannot clean up itself.
- **A foreign ``apiKeyHelper`` is never touched.** If ``settings.json`` already
  points somewhere else, that is the user's own auth plumbing: cswap leaves it
  alone and logs why, rather than hijacking the hook. Those users keep the old
  restart-to-pick-up behaviour instead of silently losing their helper.

When this channel is live it is also the *only* place the key is stored: the
``primaryApiKey`` write is dropped rather than kept as a belt-and-braces copy.
That is deliberate, and it is what makes the switch *away* from an API-key
account land too. ``primaryApiKey`` is memoized for the lifetime of a process
and cleared only by that process's own ``/login`` / ``/logout``, so any session
that starts while it is set is pinned to that key permanently — activating an
OAuth account later removes the value from the file but cannot touch the memo,
and a managed key outranks the claude.ai credential. Sessions started during a
spell on an API-key account would otherwise keep billing it while ``cswap
status`` reported the subscription account. A newly started session reads the
helper just as readily, so nothing is lost by making it the sole home.

Everything here is still best-effort: when the channel is unavailable (Windows,
a foreign helper, an I/O failure) the caller falls back to writing
``primaryApiKey`` and those sessions keep the old restart-to-pick-up behaviour.
No failure in it may fail a switch.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import sys
import tempfile
from pathlib import Path

from claude_swap.paths import get_backup_root, get_claude_config_home

# Basename of the generated helper script and of the file it prints, both under
# the cswap backup root (the same 0700 tree that already holds credentials).
HELPER_SCRIPT_NAME = "api-key-helper.sh"
ACTIVE_KEY_NAME = "active-api-key"

# Claude Code's user-level settings file, inside the config home, and the hook
# key within it.
SETTINGS_FILENAME = "settings.json"
SETTINGS_KEY = "apiKeyHelper"

# ``set -eu`` plus an explicit readability probe: when the key file is gone the
# helper must fail loudly rather than print an empty key, which Claude Code would
# surface as a confusing empty-credential error instead of "helper is failing".
_HELPER_SCRIPT = """#!/bin/sh
# Managed by claude-swap — do not edit; regenerated on every switch.
#
# Prints the API key of the currently active cswap account. Claude Code re-runs
# this on a TTL, so a *running* session picks up an API-key switch on its next
# request instead of needing a restart. Removed again when an OAuth account is
# activated (an installed apiKeyHelper would otherwise outrank the OAuth login).
set -eu
key_file={key_file}
[ -r "$key_file" ] || exit 1
exec cat "$key_file"
"""


class ApiKeyHelperChannel:
    """Installs and removes the ``apiKeyHelper`` hook that carries a live switch.

    Stateless apart from its logger: every path is resolved at call time, so a
    test (or a user) changing ``HOME`` / ``CLAUDE_CONFIG_DIR`` / ``XDG_DATA_HOME``
    between calls is honored, exactly as the rest of ``paths`` behaves.
    """

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    # -- locations ----------------------------------------------------------

    @property
    def script_path(self) -> Path:
        """The generated helper script."""
        return get_backup_root() / HELPER_SCRIPT_NAME

    @property
    def key_path(self) -> Path:
        """The file the helper prints: the active account's raw API key."""
        return get_backup_root() / ACTIVE_KEY_NAME

    @property
    def settings_path(self) -> Path:
        """Claude Code's user settings file, where the hook is registered."""
        return get_claude_config_home() / SETTINGS_FILENAME

    # -- public API ---------------------------------------------------------

    def install(self, api_key: str) -> bool:
        """Point Claude Code's ``apiKeyHelper`` at ``api_key``. True when live.

        Writes the key file first and the script second, so the hook is never
        registered before the thing it reads exists. Returns False (having
        changed nothing the caller depends on) when the channel is unavailable:
        an unsupported platform, an I/O failure, or a foreign helper we refuse to
        overwrite. The switch itself is unaffected either way — a False just
        means running sessions will need a restart to see this account.
        """
        if not self.supported():
            return False
        try:
            self._write_key_file(api_key)
            self._write_script()
        except OSError as e:
            self._logger.warning(
                f"Could not write the apiKeyHelper files ({e}); running sessions "
                "will need a restart to pick up this API key"
            )
            return False
        return self._set_hook(str(self.script_path))

    def remove(self) -> None:
        """Unregister the hook and shred the key file. Best-effort, never raises.

        Unregisters *before* deleting the key file so there is no window where a
        live hook points at a missing file. A foreign helper is left registered
        (and its key file, which is not ours, untouched).
        """
        if not self._set_hook(None):
            return
        try:
            if self.key_path.exists():
                self.key_path.unlink()
        except OSError as e:
            self._logger.warning(f"Could not remove the active-API-key file: {e}")

    def active_key(self) -> str:
        """The key this channel is currently serving, or "" when it isn't.

        A read source, not a probe: when the helper holds the key it is the only
        copy on disk, so ``_read_managed_key`` has to be able to find it here or
        cswap would report an active API-key account as having no credential.

        Ownership is checked against ``settings.json`` rather than the key file's
        mere existence: ``remove()`` unregisters the hook before unlinking the
        file, and a foreign helper's registration means the key file left behind
        by an older cswap is not what Claude Code is reading.
        """
        try:
            registered = self._read_settings(self.settings_path).get(SETTINGS_KEY)
        except ValueError:
            return ""
        if registered != str(self.script_path):
            return ""
        try:
            return self.key_path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def supported(self) -> bool:
        """Whether this channel can run here.

        The helper is a POSIX ``/bin/sh`` script; Windows has no equivalent yet,
        so cswap keeps the old behaviour there rather than registering a hook
        Claude Code cannot execute.
        """
        return sys.platform != "win32"

    # -- files --------------------------------------------------------------

    def _write_key_file(self, api_key: str) -> None:
        """Atomically write the raw key, owner-readable only."""
        self._atomic_write(self.key_path, api_key.strip(), mode=0o600)

    def _write_script(self) -> None:
        """(Re)write the helper script, owner-executable only.

        Rewritten on every install rather than written once: the key-file path it
        embeds moves with ``XDG_DATA_HOME``, and a script left over from an older
        cswap must not keep pointing at a stale location.
        """
        body = _HELPER_SCRIPT.format(key_file=shlex.quote(str(self.key_path)))
        self._atomic_write(self.script_path, body, mode=0o700)

    def _atomic_write(self, path: Path, content: str, mode: int) -> None:
        """Write ``content`` to ``path`` via a same-directory temp file + rename."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            os.write(fd, content.encode("utf-8"))
            os.close(fd)
            fd = -1
            os.chmod(tmp_path, mode)
            os.replace(tmp_path, str(path))
        except BaseException:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # -- settings.json ------------------------------------------------------

    def _set_hook(self, value: str | None) -> bool:
        """Set (``value``) or unset (``None``) ``apiKeyHelper``. True on success.

        Returns True without writing when the file already says what we want —
        an unnecessary rewrite would wake every running session's settings
        watcher for nothing. Returns False, untouched, when the hook belongs to
        someone else or the file cannot be parsed.
        """
        path = self.settings_path
        try:
            data = self._read_settings(path)
        except ValueError as e:
            self._logger.warning(
                f"Not touching {path} ({e}); running sessions will need a restart "
                "to pick up this account"
            )
            return False

        current = data.get(SETTINGS_KEY)
        if current == value:
            return True
        if current is not None and current != str(self.script_path):
            self._logger.warning(
                f"{path} already sets {SETTINGS_KEY} to {current!r}; leaving your "
                "own helper in place. Running sessions will not pick up cswap "
                "API-key switches until it is removed."
            )
            return False

        if value is None:
            data.pop(SETTINGS_KEY, None)
        else:
            data[SETTINGS_KEY] = value

        try:
            self._write_settings(path, data)
        except OSError as e:
            self._logger.warning(
                f"Could not update {path} ({e}); running sessions will need a "
                "restart to pick up this account"
            )
            return False
        return True

    @staticmethod
    def _read_settings(path: Path) -> dict:
        """Parse ``settings.json``. ``{}`` when absent; raises on unusable content.

        Raises:
            ValueError: The file exists but is not readable JSON, or is not a
                JSON object. Either way it is a file we must not rewrite — doing
                so would destroy settings we failed to parse.
        """
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except OSError as e:
            raise ValueError(f"cannot read it: {e}") from e
        except json.JSONDecodeError as e:
            raise ValueError(f"it is not valid JSON: {e}") from e
        if not isinstance(data, dict):
            raise ValueError("its top level is not a JSON object")
        return data

    def _write_settings(self, path: Path, data: dict) -> None:
        """Atomically rewrite ``settings.json``, preserving its permissions.

        Claude Code's watcher reacts to the rename, which is what makes the hook
        change land in a running session. An existing file keeps its mode (this
        is the user's own settings file, not ours to lock down); a file we create
        starts owner-only.
        """
        try:
            mode = path.stat().st_mode & 0o777
        except OSError:
            mode = 0o600
        self._atomic_write(path, json.dumps(data, indent=2) + "\n", mode=mode)
