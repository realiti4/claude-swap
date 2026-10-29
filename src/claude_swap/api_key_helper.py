"""Carry an API-key account switch into Claude Code sessions that are already running.

Switching between OAuth accounts reaches a running session for free: Claude Code
watches ``.credentials.json`` and drops its memoized token when the file changes.

The managed-API-key axis has no such property. ``primaryApiKey`` is read when a
session starts and never again, so activating an API-key account used to be
worse than invisible: the OAuth credential is cleared for mutual exclusion, the
watcher fires, and every running session drops to "Not logged in · Please run
/login" while the fresh key sits unread in ``~/.claude.json``. A managed fallback
that ``cswap auto --include-api-key-accounts`` makes logs out every session on
the machine at the moment it was meant to keep them working.

``apiKeyHelper`` is the API-key channel Claude Code does re-read at runtime. It
is a settings key, Claude Code reloads its settings files when they change, and
it outranks the claude.ai OAuth login. Measured against Claude Code 2.1.302:

- adding the hook to ``settings.json`` moves a running session onto the key on
  its next request, and removing it moves the session back to the OAuth login;
- the helper's *output* is cached (``CLAUDE_CODE_API_KEY_HELPER_TTL_MS``, five
  minutes by default) for as long as the hook's command string is unchanged.
  Rewriting only the key file left a session on the previous key; changing the
  command string made the next request use the new one. So the registered
  command carries a fingerprint of the key, and a switch between two API-key
  accounts changes it.
- a running session loads the changed ``settings.json`` a moment after the
  write, not at once. Measured on 2.1.304, a request 0.5 s after the hook was
  registered still had no helper while one 1 s after had it; a loaded production
  host still missed it 1.7 s after. Clearing the OAuth login inside that window
  leaves a request with neither credential ("Not logged in · Please run
  /login"), and an agent stalls until something sends it a new prompt.

Constraints that shape the implementation:

- **Toggled, never left installed.** An installed helper outranks the OAuth
  login, so it is registered when an API-key account is activated and removed
  when an OAuth one is, including the activation ``add_account`` performs when
  it captures a fresh ``/login``, which Claude Code cannot undo itself.
- **Registered, and given ``SETTLE_SECONDS`` to load, before OAuth is cleared;
  removed only after OAuth is written**, so a running session never holds
  neither credential (the caller orders this and calls ``settle``).
- **A foreign ``apiKeyHelper`` is never touched.** If ``settings.json`` already
  names a different command, that is the user's own auth plumbing: cswap leaves
  it and logs why, and those users keep restart-to-pick-up behaviour.
- **Best-effort.** No failure here may fail a switch; the caller still stores
  the key where Claude Code's own ``/login`` would (``primaryApiKey``, or the
  macOS Keychain), so a newly started session always finds it.
- **No new plaintext copy on macOS.** When the key is in the Keychain the
  helper reads it from there with ``security``.

The approach follows realiti4/claude-swap#191 (@arthurdls), redone on current
``main`` with the per-key command string added.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shlex
import time
from pathlib import Path

from claude_swap import macos_keychain
from claude_swap.fsutil import atomic_write_text, publish_target
from claude_swap.models import Platform
from claude_swap.paths import get_backup_root, get_claude_config_home

# Generated script and the key file it prints, under the cswap backup root (the
# same 0700 tree that already holds the account credentials).
HELPER_SCRIPT_NAME = "api-key-helper.sh"
ACTIVE_KEY_NAME = "active-api-key"

SETTINGS_FILENAME = "settings.json"
SETTINGS_KEY = "apiKeyHelper"

# Length of the key fingerprint appended to the registered command. It only has
# to differ between the keys one machine switches among, and it must not leak
# the key: settings.json is often group- or world-readable.
FINGERPRINT_LEN = 12

# How long the caller waits after registering a new hook before it clears the
# OAuth login (see the module docstring for the measurements). About 3x the
# worst gap observed; it only delays a switch onto an API key.
SETTLE_SECONDS = 3.0

# ``set -eu`` plus a readability probe: with the key file gone the helper must
# fail loudly rather than print an empty key, which Claude Code would report as
# a confusing empty credential instead of a failing helper.
_HELPER_SCRIPT = """#!/bin/sh
# Managed by claude-swap. Do not edit: regenerated on every API-key switch and
# removed again when an OAuth account is activated.
#
# Prints the API key of the active cswap account, so running Claude Code
# sessions pick up an API-key switch without a restart. The argument is a key
# fingerprint that only exists to change the registered command per key.
set -eu
key_file={key_file}
[ -r "$key_file" ] || exit 1
exec cat "$key_file"
"""

# macOS with the key in the Keychain: read it from the same "Claude Code" item
# Claude Code's own /login key uses, so no plaintext copy is written to disk.
# Command substitution drops the newline `security -w` prints after the value.
_KEYCHAIN_HELPER_SCRIPT = """#!/bin/sh
# Managed by claude-swap. Do not edit: regenerated on every API-key switch and
# removed again when an OAuth account is activated.
#
# Prints the API key of the active cswap account from the macOS Keychain, so
# running Claude Code sessions pick up an API-key switch without a restart. The
# argument is a key fingerprint that only exists to change the registered
# command per key.
set -eu
key=$({security} find-generic-password -a {account} -s {service} -w)
[ -n "$key" ] || exit 1
printf '%s' "$key"
"""


def key_fingerprint(api_key: str) -> str:
    """Short, non-reversible tag for ``api_key`` (see ``FINGERPRINT_LEN``)."""
    return hashlib.sha256(api_key.strip().encode("utf-8")).hexdigest()[:FINGERPRINT_LEN]


class ApiKeyHelperChannel:
    """Installs and removes the ``apiKeyHelper`` hook that carries a live switch.

    Stateless apart from its logger and platform: every path is resolved at call
    time, so ``HOME`` / ``CLAUDE_CONFIG_DIR`` / ``XDG_DATA_HOME`` changes between
    calls are honored the same way the rest of ``paths`` honors them.
    """

    def __init__(self, logger: logging.Logger, platform: Platform) -> None:
        self._logger = logger
        self._platform = platform
        # Whether the last ``_set_hook`` rewrote settings.json. Only a rewrite
        # needs running sessions to reload; an unchanged hook is already live.
        self.hook_changed = False

    # -- locations ----------------------------------------------------------

    @property
    def script_path(self) -> Path:
        return get_backup_root() / HELPER_SCRIPT_NAME

    @property
    def key_path(self) -> Path:
        return get_backup_root() / ACTIVE_KEY_NAME

    @property
    def settings_path(self) -> Path:
        """The active profile's user settings file, where the hook is registered."""
        return get_claude_config_home() / SETTINGS_FILENAME

    def hook_command(self, api_key: str) -> str:
        """The command registered for ``api_key``: script path plus fingerprint."""
        return f"{shlex.quote(str(self.script_path))} {key_fingerprint(api_key)}"

    def is_ours(self, value: object) -> bool:
        """Whether a registered ``apiKeyHelper`` value is a hook cswap installed."""
        if not isinstance(value, str):
            return False
        try:
            argv = shlex.split(value)
        except ValueError:
            return False
        return bool(argv) and argv[0] == str(self.script_path)

    def supported(self) -> bool:
        """Whether the channel can run here.

        The helper is a POSIX ``/bin/sh`` script and Claude Code runs
        ``apiKeyHelper`` through ``cmd`` on Windows, so Windows keeps the old
        restart-to-pick-up behaviour rather than registering a hook it cannot
        execute.
        """
        return self._platform != Platform.WINDOWS

    # -- public API ---------------------------------------------------------

    def install(
        self, api_key: str, keychain_item: tuple[str, str] | None = None
    ) -> bool:
        """Point Claude Code's ``apiKeyHelper`` at ``api_key``. True when live.

        ``keychain_item`` is the ``(service, account)`` the caller stored the key
        under in the macOS Keychain; the helper then reads it from there and no
        key file is written. Otherwise the key goes to a 0600 file.

        Writes the key source and script before registering the hook, so the hook
        never names something that does not exist yet. Returns False, having
        changed nothing the caller depends on, when the channel is unavailable:
        an unsupported platform, an I/O failure, an unparsable settings file, or
        a foreign helper. The switch itself proceeds either way.
        """
        if not self.supported():
            return False
        try:
            if keychain_item is None:
                self._atomic_write(self.key_path, api_key.strip(), mode=0o600)
                body = _HELPER_SCRIPT.format(key_file=shlex.quote(str(self.key_path)))
            else:
                # A key file from an earlier file-mode spell must not linger.
                if self.key_path.exists():
                    self.key_path.unlink()
                service, account = keychain_item
                body = _KEYCHAIN_HELPER_SCRIPT.format(
                    security=shlex.quote(macos_keychain._SECURITY),
                    account=shlex.quote(account),
                    service=shlex.quote(service),
                )
            self._atomic_write(self.script_path, body, mode=0o700)
        except OSError as e:
            self._logger.warning(
                f"Could not write the apiKeyHelper files ({e}); running sessions "
                "will need a restart to pick up this API key"
            )
            return False
        return self._set_hook(self.hook_command(api_key))

    def settle(self) -> None:
        """Wait for running sessions to load a hook ``install`` just registered.

        Call it between ``install`` and clearing the OAuth login. A no-op when
        the hook was already registered.
        """
        if self.hook_changed and SETTLE_SECONDS > 0:
            time.sleep(SETTLE_SECONDS)

    def remove(self) -> bool:
        """Unregister the hook, then delete the key file. Never raises.

        Unregisters first so no window exists where a live hook points at a
        missing file. A foreign helper stays registered and its files untouched.
        A settings file without the hook is not rewritten. Returns True when a
        cswap hook was registered and is now gone.
        """
        try:
            had_hook = self.is_ours(
                self._read_settings(self.settings_path).get(SETTINGS_KEY)
            )
        except ValueError:
            had_hook = False
        if not self._set_hook(None):
            return False
        try:
            # Checked first: every OAuth activation lands here, and with no API
            # key ever activated there is nothing in the store to touch.
            if self.key_path.exists():
                self.key_path.unlink()
        except OSError as e:
            self._logger.warning(f"Could not remove the active API-key file: {e}")
        return had_hook

    # -- settings.json ------------------------------------------------------

    def _set_hook(self, value: str | None) -> bool:
        """Register ``value`` (or unregister, for ``None``). True on success.

        Skips the write when the file already says what we want: a needless
        rewrite wakes every running session's settings watcher. Returns False,
        untouched, when the hook belongs to someone else or the file cannot be
        parsed.
        """
        path = self.settings_path
        try:
            data = self._read_settings(path)
        except ValueError as e:
            self._logger.warning(
                f"Not touching {path} ({e}); running sessions will need a restart "
                "to pick up API-key switches"
            )
            return False

        self.hook_changed = False
        current = data.get(SETTINGS_KEY)
        if current == value:
            return True
        if current is not None and not self.is_ours(current):
            self._logger.warning(
                f"{path} already sets {SETTINGS_KEY} to {current!r}; leaving your "
                "own helper in place. Running sessions will not pick up cswap "
                "API-key switches while it is registered."
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
                "restart to pick up API-key switches"
            )
            return False
        self.hook_changed = True
        return True

    @staticmethod
    def _read_settings(path: Path) -> dict:
        """Parse ``settings.json``: ``{}`` when absent, ValueError when unusable.

        A file that exists but cannot be read, is not JSON, or is not a JSON
        object must not be rewritten: doing so would destroy settings we failed
        to parse.
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
        """Atomically rewrite ``settings.json``, keeping its mode and any symlink.

        A symlinked settings file (dotfile managers) is written through to its
        target, so the link survives. An existing file keeps its mode; a file
        created here starts owner-only.
        """
        try:
            mode = publish_target(path).stat().st_mode & 0o777
        except OSError:
            mode = 0o600
        self._atomic_write(path, json.dumps(data, indent=2) + "\n", mode=mode)

    @staticmethod
    def _atomic_write(path: Path, content: str, mode: int) -> None:
        """``fsutil.atomic_write_text``, creating the parent directory first."""
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, content, mode=mode)
