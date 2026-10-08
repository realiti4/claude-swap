"""The live (active) credential store for OpenAI Codex CLI accounts.

Codex keeps its login in one of two places, chosen by
``cli_auth_credentials_store`` in ``$CODEX_HOME/config.toml`` (default
``"file"``; codex-rs/config/src/types.rs:112-125):

- ``file``: ``$CODEX_HOME/auth.json``.
- ``keyring``: the OS keyring only — on macOS the generic-password item
  service ``"Codex Auth"``, account ``"cli|" + sha256(canonical home)[:16]``,
  value the compact JSON of the same document. A successful save deletes
  ``auth.json`` (codex-rs/login/src/auth/storage.rs:235-322).
- ``auto``: keyring first, file on a miss or a keyring error; saves go to the
  keyring and fall back to the file (storage.rs:431-457).

``ephemeral`` (memory only) cannot be switched from outside the process, and
the keyring modes are only implemented for the macOS Keychain; both raise
:class:`CodexStoreUnsupported`.

:class:`CodexCredentialStore` overrides only the ACTIVE half of
``CredentialStore`` — ``_read_active_credentials`` and ``_write_credentials``
(``_read_credentials`` and the whole per-slot backup half are inherited, the
backup Keychain service coming from the host's ``backup_keychain_service``).
The Claude-specific active methods (managed key, ``.credentials.json``,
residual-item pinning) become unreachable.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import tempfile
import time
import tomllib
from pathlib import Path

from claude_swap import codex_auth, macos_keychain
from claude_swap.credentials import ActiveCredentials, CredentialStore, _StoreHost
from claude_swap.exceptions import ClaudeSwitchError, CredentialWriteError
from claude_swap.fsutil import replace_with_retry
from claude_swap.models import Platform

_logger = logging.getLogger("claude-swap")

CODEX_KEYRING_SERVICE = "Codex Auth"

# `codex -c` value forcing the file store whatever the user's config picks
# (the value is TOML; codex-rs/utils/cli/src/config_override.rs:19-77). For
# homes cswap creates (temp logins, session profiles), whose login must land
# in, and be read from, that home's auth.json.
FILE_STORE_OVERRIDE = 'cli_auth_credentials_store="file"'

# Codex rewrites auth.json IN PLACE (truncate + write, no temp file, no lock;
# storage.rs:206-223), so a read can land on an empty or half-written file.
# A short backoff lets that write finish. This rides out an external writer —
# it does not paper over an internal race.
_PARSE_RETRY_ATTEMPTS = 3
_PARSE_RETRY_DELAY = 0.05  # seconds between attempts


class CodexStoreUnsupported(ClaudeSwitchError):
    """Codex's configured credential store is one cswap cannot switch."""


def codex_home() -> Path:
    """``$CODEX_HOME`` when set and non-empty, else ``~/.codex``.

    Mirrors codex-rs/utils/home-dir/src/lib.rs:13-63. Codex refuses a
    ``CODEX_HOME`` that does not exist; here reads treat it as "no login" and
    writes raise (see :meth:`CodexCredentialStore._write_credentials`).
    """
    raw = os.environ.get("CODEX_HOME")
    return Path(raw) if raw else Path.home() / ".codex"


def live_auth_path() -> Path:
    return codex_home() / "auth.json"


def read_codex_config(home: Path | None = None) -> dict:
    """Parsed ``config.toml`` of ``home`` (default :func:`codex_home`), or
    ``{}`` when absent.

    An unparseable file (bad TOML, not UTF-8, unreadable) is logged and
    treated as ``{}`` — so the store mode falls back to ``"file"`` — rather
    than failing every read: Codex itself refuses to start on it, so there is
    no live keyring login to miss.
    """
    path = (home or codex_home()) / "config.toml"
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except (FileNotFoundError, NotADirectoryError):
        return {}
    except (OSError, ValueError) as e:  # TOMLDecodeError, UnicodeDecodeError
        _logger.warning(f"Ignoring unreadable Codex config {path}: {e}")
        return {}


def credential_store_mode(config: dict | None = None) -> str:
    """``cli_auth_credentials_store`` from Codex's config (default ``"file"``).

    ``config`` is an already-parsed :func:`read_codex_config` result, so one
    operation parses (and warns about) the file once.
    """
    if config is None:
        config = read_codex_config()
    return str(config.get("cli_auth_credentials_store", "file"))


def chatgpt_base_url() -> str | None:
    """``chatgpt_base_url`` from Codex's config; None means Codex's default
    backend (codex-rs/core/src/config/mod.rs:4420-4422)."""
    value = read_codex_config().get("chatgpt_base_url")
    return value if isinstance(value, str) and value.strip() else None


def keyring_account_name(home: Path) -> str:
    """Codex's keyring account for ``home`` (storage.rs:235-249): ``"cli|"`` +
    the first 16 hex of sha256 over the canonical path (the path as spelled
    when it cannot be canonicalized)."""
    digest = hashlib.sha256(os.path.realpath(home).encode()).hexdigest()
    return f"cli|{digest[:16]}"


def _json_object(text: str | None) -> dict | None:
    """``text`` parsed, when it is a JSON object — Codex's ``AuthDotJson`` is
    one; anything else (null, a string, a list, garbage) fails its serde."""
    if text is None:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def read_auth_json(path: Path) -> str | None:
    """An auth.json's text when it is a JSON object, else None (garbage, not
    UTF-8, or still torn after the retry). Read as bytes: ``read_text`` would
    translate newlines and break a byte-exact rollback.

    Raises:
        OSError: The file cannot be read (``FileNotFoundError`` when absent).
    """
    for attempt in range(_PARSE_RETRY_ATTEMPTS):
        try:
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            text = None
        if _json_object(text) is not None:
            return text
        if attempt + 1 < _PARSE_RETRY_ATTEMPTS:
            time.sleep(_PARSE_RETRY_DELAY)
    return None


class CodexCredentialStore(CredentialStore):
    """Active Codex login (auth.json or the ``Codex Auth`` Keychain item) plus
    the inherited per-slot backup store.

    KEYCHAIN ROUTING. The base class learns per-process Keychain usability
    from real ops (``_kc_call``) and routes on it (``_use_keychain``); the
    backup half depends on that state. Codex active I/O deliberately goes
    straight to the ``macos_keychain`` wrappers instead, in every mode:

    - The store is dictated by Codex's config, not by what we learned. Gating
      on ``_use_keychain()`` would make a failed BACKUP op skip the one store
      a keyring-mode Codex reads.
    - A failure on the ``Codex Auth`` item is reported precisely where the
      switcher consumes it — this read's ``ActiveCredentials`` verdict — and
      the backup half learns usability from its own first op, so flipping the
      shared cache would add nothing but cross-talk: a later success on the
      Codex item would erase a failure ``_kc_call`` recorded for a backup
      read (the erasure ``_active_read_failed`` exists to prevent).

    The last write's user-facing warnings (a Keychain fallback, a leftover
    auth.json, the account_id mismatch) are kept in ``write_warnings`` as well
    as logged, so the switch can show them instead of burying them in the log.
    """

    def __init__(self, host: _StoreHost) -> None:
        super().__init__(host)
        self.write_warnings: list[str] = []

    def _warn(self, message: str) -> None:
        self._host._logger.warning(message)
        self.write_warnings.append(message)

    # -- mode -------------------------------------------------------------------

    def _resolve_mode(self) -> str:
        """The effective store mode, or raise :class:`CodexStoreUnsupported`.

        Parses config.toml once; callers pass the mode down rather than
        resolving it again.
        """
        config = read_codex_config()
        mode = credential_store_mode(config)
        if mode == "file":
            return mode
        if mode in ("keyring", "auto"):
            if self._host.platform != Platform.MACOS:
                reason = "cswap reaches Codex's keyring only through the macOS Keychain"
            else:
                features = config.get("features")
                if not (
                    isinstance(features, dict)
                    and features.get("secret_auth_storage") is True
                ):
                    return mode
                # The "secrets" keyring backend keeps the login in an encrypted
                # $CODEX_HOME/secrets file (core/src/config/auth_keyring.rs:110-118);
                # writing the Keychain item would switch nothing.
                reason = (
                    "[features] secret_auth_storage keeps the login in an encrypted file"
                )
        else:
            reason = "cswap cannot switch a login stored that way"
        raise CodexStoreUnsupported(
            f"Codex is configured with cli_auth_credentials_store = {mode!r} in "
            f"{codex_home() / 'config.toml'}: {reason}. Set "
            f'cli_auth_credentials_store = "file" there, then sign in to Codex again.'
        )

    # -- reads --------------------------------------------------------------------

    def _read_active_credentials(self) -> ActiveCredentials:
        """Read the live Codex login, classified like the Claude read.

        ``value``: the credential text (verbatim, so a rollback writes back
        exactly what was read), ``""`` when no store holds one, ``None`` when
        the store that would hold it could not be read or holds something
        that is not a JSON object. ``keychain_unavailable`` / ``degraded``:
        the ``Codex Auth`` item could not be read — a fallback file (auto
        mode) may then be served, degraded.

        Raises:
            CodexStoreUnsupported: Codex is configured for a store cswap
                cannot switch.
        """
        return self._read_live(self._resolve_mode())

    def _read_live(self, mode: str) -> ActiveCredentials:
        keychain_failed = False
        if mode != "file":
            try:
                value = macos_keychain.get_password(
                    CODEX_KEYRING_SERVICE, keyring_account_name(codex_home())
                )
            except macos_keychain.KEYCHAIN_ERRORS as e:
                self._host._logger.warning(f"Codex Keychain read failed: {e}")
                keychain_failed = True
            else:
                if value and _json_object(value) is not None:
                    return ActiveCredentials(value, False, False)
                if value:
                    # Codex maps an undeserializable item to a load ERROR
                    # (storage.rs:265-279) — auto then falls back to the file.
                    self._host._logger.warning(
                        "Codex Keychain item is not a JSON object; "
                        "treating it as unreadable"
                    )
                    keychain_failed = True
            if mode == "keyring":
                # No fallback: a failed read is the read error of the only
                # store. "" would read as "nothing live to back up".
                return (
                    ActiveCredentials(None, True, True)
                    if keychain_failed
                    else ActiveCredentials("", False, False)
                )

        text = self._read_auth_file()
        if text is None:
            return ActiveCredentials(None, keychain_failed, keychain_failed)
        if not text and keychain_failed:
            # Auto mode, Keychain down, no file: the login may well be in the
            # Keychain — unknown, not absent.
            return ActiveCredentials(None, True, True)
        return ActiveCredentials(text, False, keychain_failed)

    def _read_auth_file(self) -> str | None:
        """``auth.json`` text, ``""`` when absent, ``None`` when unreadable or
        still not a JSON object after the torn-write retry.

        Bytes, decoded here: ``read_text`` would translate newlines and break
        the byte-exact rollback.
        """
        path = live_auth_path()
        try:
            text = read_auth_json(path)
        except (FileNotFoundError, NotADirectoryError):
            return ""
        except OSError as e:
            self._host._logger.error(f"Failed to read Codex auth file {path}: {e}")
            return None
        if text is None:
            # Garbage must not travel on as the live credential: a switch would
            # back it up over the departing slot's good backup.
            self._host._logger.warning(f"Codex auth file {path} is not a JSON object")
        return text

    # -- writes -------------------------------------------------------------------

    def _write_credentials(self, credentials: str) -> None:
        """Activate ``credentials`` in Codex's configured store.

        Atomic in every mode; accepts the exact text a read returned (the
        switch rollback replays it). Records ``_last_active_credentials_backend``.

        Raises:
            CodexStoreUnsupported: Codex is configured for a store cswap
                cannot switch.
            CredentialWriteError: The write failed; the live store is unchanged.
        """
        self.write_warnings = []
        mode = self._resolve_mode()
        home = codex_home()
        if os.environ.get("CODEX_HOME") and not home.is_dir():
            # Codex refuses a missing CODEX_HOME; creating it would park the
            # login in a directory no Codex process reads.
            raise CredentialWriteError(
                f"CODEX_HOME points to {home}, which is not an existing directory"
            )
        parsed = _json_object(credentials)
        if parsed is None:
            raise CredentialWriteError(
                "Refusing to activate a Codex credential that is not a JSON object"
            )

        if mode == "file":
            self._write_auth_file(credentials)
            backend = "file"
        else:
            backend = self._write_keychain(mode, home, credentials, parsed)
        self._last_active_credentials_backend = backend
        self._warn_on_account_id_mismatch(mode)

    def _write_keychain(
        self, mode: str, home: Path, credentials: str, parsed: dict
    ) -> str:
        """keyring/auto save, mirroring Codex; returns the backend that took it."""
        account = keyring_account_name(home)
        try:
            # Compact like Codex's serde_json::to_string (storage.rs:301-305).
            macos_keychain.set_password(
                CODEX_KEYRING_SERVICE,
                account,
                json.dumps(parsed, separators=(",", ":"), ensure_ascii=False),
            )
        except macos_keychain.KEYCHAIN_ERRORS as e:
            if mode == "keyring":
                raise CredentialWriteError(
                    f"Failed to write the Codex login to the Keychain: {e}"
                )
            # Auto reads the Keychain FIRST, so a surviving item would shadow
            # the file and Codex would come back on the previous login once the
            # Keychain answers again. Clear it BEFORE touching the file, and
            # refuse when that cannot be done (a locked Keychain usually fails
            # both): nothing has changed yet, so the switch fails cleanly.
            # Codex's own fallback skips this and leaves the split brain.
            try:
                macos_keychain.delete_password(CODEX_KEYRING_SERVICE, account)
            except macos_keychain.KEYCHAIN_ERRORS as e2:
                raise CredentialWriteError(
                    f"Codex Keychain write failed ({e}) and its existing item could "
                    f"not be cleared ({e2}); refusing to switch via auth.json while "
                    "that item would shadow it. Unlock the Keychain and retry."
                )
            self._warn(f"Codex Keychain write failed, falling back to auth.json: {e}")
            # ponytail: if this file write then fails, the item is already
            # gone and the previous login survives only in its slot backup;
            # restoring it would need the old item value read first.
            self._write_auth_file(credentials)
            return "file"
        # A keyring save deletes the file fallback (storage.rs:306-308) —
        # unlinking a symlinked auth.json itself, as Codex's remove_file does.
        try:
            live_auth_path().unlink(missing_ok=True)
        except OSError as e:
            self._warn(f"Could not remove {live_auth_path()} after Keychain write: {e}")
        return "keychain"

    def _write_auth_file(self, credentials: str) -> None:
        """Atomic 0600 write of ``auth.json`` (temp file beside the target +
        ``os.replace``). A symlinked ``auth.json`` is written THROUGH: renaming
        onto the link would detach it, while Codex follows it."""
        path = live_auth_path()
        target = Path(os.path.realpath(path)) if path.is_symlink() else path
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # mkstemp creates the file 0600, so the secret is never exposed and
            # the replace leaves the live file 0600 whatever it was before.
            fd, tmp_path = tempfile.mkstemp(
                dir=str(target.parent), prefix=".auth.json.", suffix=".tmp"
            )
            try:
                # Binary: text mode would turn "\n" into "\r\n" on Windows and
                # break the byte-exact rollback.
                with os.fdopen(fd, "wb") as f:
                    f.write(credentials.encode("utf-8"))
                replace_with_retry(tmp_path, str(target))
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)
                raise
        except OSError as e:
            raise CredentialWriteError(f"Failed to write {path}: {e}")

    def _warn_on_account_id_mismatch(self, mode: str) -> None:
        """Re-read the live login and warn when ``tokens.account_id`` disagrees
        with the id_token's ``chatgpt_account_id`` — the hybrid a Codex refresh
        racing our write leaves behind (one account's tokens under another's
        account_id; codex-spec gotcha 5).

        Never raises: the live store has already changed, and the switcher
        records the write for rollback only once ``_write_credentials``
        returns — an exception here would strand it un-rollbackable.
        """
        # ponytail: detect-and-warn only. Codex takes no lock on auth.json
        # (cooperative locking does not exist on its side), so a refresh that
        # lands between our replace and its next read can still win; a real
        # fix needs Codex to lock or the user to quit Codex before switching.
        try:
            live = self._read_live(mode).value
            tokens = codex_auth.chatgpt_tokens(live) if live else None
            if not tokens:
                return
            stored = tokens.get("account_id")
            claimed = codex_auth.claimed_account_id(tokens.get("id_token"))
        except (ClaudeSwitchError, ValueError, OSError) as e:
            self._host._logger.debug(f"Skipped post-write Codex verification: {e}")
            return
        if stored and claimed and stored != claimed:
            self._warn(
                f"Live Codex login is inconsistent after writing it: tokens.account_id "
                f"{stored!r} but the id_token belongs to {claimed!r} — a running Codex "
                "probably refreshed concurrently. Quit Codex and switch again."
            )
