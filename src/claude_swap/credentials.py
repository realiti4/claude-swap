"""Credential storage layer for claude-swap.

Owns *where* credentials live and *how* they are read/written — the macOS
Keychain-vs-file routing, per-process capability detection and sticky fallback,
and the ``.enc``-wins backup reconciliation that landed in #66. Split out of
``switcher.py`` so the switcher reads as account orchestration again.

``CredentialStore`` is a leaf collaborator: it imports only the OS-primitive and
path helpers (``macos_keychain``, ``paths``) and never imports ``switcher``. It
reads its live configuration (``platform``, ``_logger``, ``credentials_dir``)
from a host *view* — a small data-only window onto the switcher that constructs
it — and must never call a switcher *method* through that host, or storage and
orchestration would re-couple. The store owns only its two pieces of state:
``_keychain_usable_cache`` (sticky, process-local) and
``_last_active_credentials_backend`` (for the post-switch follow-up message).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Protocol

from claude_swap import macos_keychain
from claude_swap.exceptions import CredentialWriteError
from claude_swap.models import Platform
from claude_swap.paths import (
    get_claude_config_home,
    get_credentials_path,
    get_global_config_path,
)

# Service name for per-account backup credentials now managed via the ``security``
# CLI on macOS. Deliberately distinct from KEYRING_SERVICE so old keyring items and
# new security items coexist during migration (safe write → verify → delete).
SECURITY_SERVICE = "claude-swap"

# Service name of Claude Code's *active* OAuth credential in the macOS Keychain
# (read by Claude Code itself; we read/write it when switching accounts).
CLAUDE_CODE_KEYCHAIN_SERVICE = "Claude Code-credentials"

# Service name of Claude Code's *active* managed API key (``/login`` with an
# ``sk-ant-api…`` key) in the macOS Keychain. Distinct from the OAuth service above
# (no ``-credentials`` suffix); Claude Code resolves it on a separate auth axis
# (``getApiKeyFromConfigOrMacOSKeychain``). On non-macOS the managed key instead
# lives in ``~/.claude.json`` as ``primaryApiKey`` (see below).
CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE = "Claude Code"


def looks_like_api_key(credentials: str | None) -> bool:
    """Whether a stored active credential is a raw managed API key vs OAuth JSON.

    Strict on purpose: a managed key is a bare ``sk-ant-api…`` string, while every
    OAuth/setup-token credential is a JSON object (``{"claudeAiOauth": …}``). Requiring
    the ``sk-ant-api`` prefix (and that it isn't JSON) keeps a raw/garbled
    ``sk-ant-oat…`` setup token from ever being misclassified as an API key.
    """
    if not credentials:
        return False
    text = credentials.strip()
    return text.startswith("sk-ant-api") and not text.startswith("{")


def approved_form(api_key: str) -> str:
    """The value Claude Code stores in ``customApiKeyResponses.approved``.

    Mirrors Claude Code's ``normalizeApiKeyForConfig`` (``apiKey.slice(-20)``): the
    last 20 chars. Storing anything else makes Claude Code's "is this key approved?"
    check miss and re-prompt the user to approve the key.
    """
    return api_key.strip()[-20:]


class _StoreHost(Protocol):
    """The live configuration view ``CredentialStore`` reads from its owner.

    Data only — the store reads these attributes at call time so post-construction
    overrides (e.g. tests setting ``switcher.platform``) are honored. The store
    must not reach for any *method* here.
    """

    platform: Platform
    credentials_dir: Path
    _logger: logging.Logger


class CredentialStore:
    """Owns the active and per-account backup credential stores.

    One store per switcher: the capability cache is per-process, learned from real
    ``security`` calls, and a fresh process re-evaluates from scratch.
    """

    def __init__(self, host: _StoreHost):
        self._host = host
        # macOS Keychain usability, learned per-process from real `security`
        # calls (see _kc_call / _use_keychain). None = not yet probed; True/False
        # once an op has run. _last_active_credentials_backend records where the
        # most recent active-credential write landed ("keychain" | "file"), for the
        # post-switch follow-up message.
        self._keychain_usable_cache: bool | None = None
        self._last_active_credentials_backend: str | None = None

    def _kc_call(self, fn, *args):
        """Run a ``macos_keychain`` wrapper call, learning Keychain usability.

        A success (including ``get_password`` returning ``None`` for a missing
        item) marks the Keychain usable — but only flips the cache ``None -> True``,
        never ``False -> True``: once a call has failed this run we stay in file
        mode so one invocation can't split-brain between backends. A
        ``KeychainError`` / ``TimeoutExpired`` / ``OSError`` (binary missing) marks
        it unusable and re-raises so the caller can fall back. Only those three are
        caught — a programming error propagates.

        Do NOT route ``item_exists`` through here: it returns ``False`` for both
        "absent" and "failed", so a timeout would be misread as a usable Keychain.
        """
        try:
            result = fn(*args)
        except macos_keychain.KEYCHAIN_ERRORS:
            self._keychain_usable_cache = False
            raise
        if self._keychain_usable_cache is None:
            self._keychain_usable_cache = True
        return result

    def _use_keychain(self) -> bool:
        """Whether credential *writes* should target the macOS Keychain this run.

        ``False`` off macOS. On macOS, ``True`` until a Keychain op has failed this
        process (the cache flips to ``False`` and sticks). Unknown (``None``) is
        optimistic — the first real op tries the Keychain and records the outcome.
        """
        if self._host.platform != Platform.MACOS:
            return False
        return self._keychain_usable_cache is not False

    def _read_credentials(self) -> str | None:
        """Read Claude Code's active credential — OAuth *or* managed API key.

        Tries the OAuth credential first (Keychain "Claude Code-credentials" on
        macOS when usable, then the plaintext ``~/.claude/.credentials.json`` Claude
        Code also falls back to), and only then the managed-key locations (macOS
        Keychain "Claude Code", then ``~/.claude.json`` ``primaryApiKey``). Trying
        OAuth fully first means a macOS OAuth login that only has a file fallback
        (Keychain empty) is never misread as an API key. A returned managed key is a
        raw ``sk-ant-api…`` string — callers distinguish it via ``looks_like_api_key``.
        Non-mutating.

        Returns:
            Credential string if found, "" if not found, None on a file read error.
        """
        # 1. OAuth Keychain (macOS, when usable).
        if self._use_keychain():
            try:
                val = self._kc_call(
                    macos_keychain.get_password,
                    CLAUDE_CODE_KEYCHAIN_SERVICE,
                    macos_keychain.keychain_account_name(),
                )
            except macos_keychain.KEYCHAIN_ERRORS as e:
                # Locked / denied / unavailable Keychain (rc-44 "not found" comes
                # back as None, not raised). _kc_call has flipped routing to file
                # mode; fall through to the plaintext file Claude Code uses too.
                # (A programming error is NOT caught here — it propagates.)
                self._host._logger.warning(f"Keychain read failed, trying file: {e}")
                val = None
            if val:
                return val

        # 2. OAuth plaintext file (Claude Code's own fallback; every platform).
        cred_file = get_credentials_path()
        if cred_file.exists():
            try:
                text = cred_file.read_text(encoding="utf-8")
            except Exception as e:
                self._host._logger.error(f"Failed to read credentials file: {e}")
                return None
            if text.strip():
                return text

        # 3. Managed API key (Keychain "Claude Code" on macOS, then primaryApiKey).
        key = self._read_managed_key()
        if key:
            return key
        return ""

    def _read_managed_key(self) -> str:
        """Read the active managed API key, or "" when absent. Non-mutating.

        macOS Keychain "Claude Code" (when usable) first, then ``~/.claude.json``
        ``primaryApiKey`` — mirroring Claude Code's
        ``getApiKeyFromConfigOrMacOSKeychain``.
        """
        if self._use_keychain():
            try:
                val = self._kc_call(
                    macos_keychain.get_password,
                    CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE,
                    macos_keychain.keychain_account_name(),
                )
            except macos_keychain.KEYCHAIN_ERRORS as e:
                self._host._logger.warning(f"Managed-key Keychain read failed: {e}")
                val = None
            if val:
                return val
        cfg = self._read_global_config()
        if cfg:
            key = cfg.get("primaryApiKey")
            if isinstance(key, str) and key:
                return key
        return ""

    def _read_global_config(self) -> dict | None:
        """Read and parse ``~/.claude.json``, or None when absent/unreadable."""
        path = get_global_config_path()
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            self._host._logger.warning(f"Failed to read global config: {e}")
            return None
        return data if isinstance(data, dict) else None

    def _update_global_config(self, mutator) -> None:
        """Atomically apply ``mutator(dict)`` to ``~/.claude.json``, key-scoped.

        Reads the current config, lets ``mutator`` change only the keys it owns
        (``primaryApiKey`` / ``customApiKeyResponses``), and writes it back
        atomically — preserving every other key (``oauthAccount``, projects,
        settings). 0o600 mirrors the switcher's ``_write_json``.
        """
        path = get_global_config_path()
        try:
            data = self._read_global_config() or {}
        except Exception as e:  # pragma: no cover - defensive
            raise CredentialWriteError(f"Failed to read global config for update: {e}")
        mutator(data)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            os.write(fd, json.dumps(data, indent=2).encode("utf-8"))
            os.close(fd)
            fd = -1
            os.replace(tmp_path, str(path))
            if sys.platform != "win32":
                os.chmod(str(path), 0o600)
        except BaseException:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _write_active_credentials_file(self, credentials: str) -> None:
        """Atomically write Claude Code's plaintext active-credentials file."""
        cred_dir = get_claude_config_home()
        cred_dir.mkdir(parents=True, exist_ok=True)
        cred_file = cred_dir / ".credentials.json"
        import tempfile
        fd, tmp_path = tempfile.mkstemp(dir=str(cred_dir), suffix=".tmp")
        try:
            os.write(fd, credentials.encode("utf-8"))
            os.close(fd)
            fd = -1
            os.replace(tmp_path, str(cred_file))
            if sys.platform != "win32":
                os.chmod(str(cred_file), 0o600)
        except BaseException:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _delete_active_keychain_entry(self) -> None:
        """Best-effort removal of the active-credential Keychain item (macOS only).

        Claude Code reads the Keychain before the plaintext file, so once we fall
        back to the file we must clear any stale Keychain entry or Claude Code would
        resurrect it (#30337). Best-effort: when the Keychain is down the delete
        can't run, which is the documented recovery residual.
        """
        if self._host.platform != Platform.MACOS:
            return
        try:
            macos_keychain.delete_password(
                CLAUDE_CODE_KEYCHAIN_SERVICE, macos_keychain.keychain_account_name()
            )
        except Exception:
            pass  # best-effort; a down Keychain can't be cleaned now

    def _write_credentials(self, credentials: str) -> None:
        """Write Claude Code's active credential, enforcing a single auth axis.

        Detects the kind from the payload (raw ``sk-ant-api…`` key vs OAuth JSON) and
        mirrors Claude Code's own ``saveApiKey``/``removeApiKey``: activating one axis
        clears the other so a stale credential can't shadow the switch.

        - **OAuth** → write the OAuth credential (see ``_write_oauth_credentials``),
          then clear any managed key (Keychain "Claude Code" + ``primaryApiKey``;
          ``approved`` left intact, as ``removeApiKey`` does).
        - **API key** → record ``key[-20:]`` in ``approved`` and store the key (macOS
          Keychain "Claude Code" when usable, else ``~/.claude.json`` ``primaryApiKey``),
          then clear the OAuth credential (Keychain item + ``.credentials.json``).

        Raises:
            CredentialWriteError: If writing credentials fails.
        """
        if looks_like_api_key(credentials):
            self._write_managed_credentials(credentials.strip())
        else:
            self._write_oauth_credentials(credentials)
            self._clear_managed_key()

    def _write_managed_credentials(self, api_key: str) -> None:
        """Activate a managed API key, then clear OAuth (mutual exclusion).

        Always records ``key[-20:]`` in ``customApiKeyResponses.approved`` (Claude
        Code does this on every platform, even on Keychain success — otherwise it
        re-prompts to approve the key). Stores the key in the macOS Keychain when
        usable, else ``~/.claude.json`` ``primaryApiKey`` (matching ``saveApiKey``'s
        keychain-then-config fallback). Finally clears the OAuth credential.

        Raises:
            CredentialWriteError: If persisting the key fails.
        """
        wrote_to_keychain = False
        if self._use_keychain():
            try:
                self._kc_call(
                    macos_keychain.set_password,
                    CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE,
                    macos_keychain.keychain_account_name(),
                    api_key,
                )
            except macos_keychain.KEYCHAIN_ERRORS as e:
                # _kc_call flipped routing to file mode; fall back to config below.
                self._host._logger.warning(
                    f"Managed-key Keychain write failed, falling back to config: {e}"
                )
            else:
                wrote_to_keychain = True

        approved = approved_form(api_key)

        def _mutate(cfg: dict) -> None:
            responses = cfg.get("customApiKeyResponses")
            if not isinstance(responses, dict):
                responses = {}
            approved_list = responses.get("approved")
            if not isinstance(approved_list, list):
                approved_list = []
            if approved not in approved_list:
                approved_list.append(approved)
            responses["approved"] = approved_list
            responses.setdefault("rejected", [])
            cfg["customApiKeyResponses"] = responses
            if wrote_to_keychain:
                # Keychain holds the key; keep it out of plaintext config.
                cfg.pop("primaryApiKey", None)
            else:
                cfg["primaryApiKey"] = api_key

        try:
            self._update_global_config(_mutate)
        except CredentialWriteError:
            raise
        except Exception as e:
            raise CredentialWriteError(f"Failed to write managed API key: {e}")

        # Mutual exclusion: drop the OAuth credential so it can't shadow the key.
        self._clear_oauth_credential()
        self._last_active_credentials_backend = (
            "keychain" if wrote_to_keychain else "file"
        )

    def _clear_managed_key(self) -> None:
        """Clear any active managed API key (Claude Code ``removeApiKey`` semantics).

        Deletes the macOS Keychain "Claude Code" item (best-effort) and drops
        ``primaryApiKey`` from ``~/.claude.json``. Leaves
        ``customApiKeyResponses.approved`` untouched — ``removeApiKey`` doesn't clear
        it either, and removing it would force recovering ``key[-20:]`` from the
        Keychain for no benefit. A no-op (no config rewrite) when no key is present.
        """
        if self._host.platform == Platform.MACOS:
            try:
                macos_keychain.delete_password(
                    CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE,
                    macos_keychain.keychain_account_name(),
                )
            except Exception:
                pass  # best-effort; a down Keychain can't be cleaned now
        cfg = self._read_global_config()
        if cfg is not None and cfg.get("primaryApiKey") is not None:
            def _drop(c: dict) -> None:
                c.pop("primaryApiKey", None)

            try:
                self._update_global_config(_drop)
            except Exception as e:
                self._host._logger.warning(f"Failed to clear primaryApiKey: {e}")

    def _clear_oauth_credential(self) -> None:
        """Clear the active OAuth credential — Keychain item and plaintext file.

        Best-effort: a down Keychain or missing file is fine. Removing
        ``.credentials.json`` stops Claude Code from falling back to a stale OAuth
        login over the just-activated API key.
        """
        self._delete_active_keychain_entry()
        cred_file = get_credentials_path()
        try:
            if cred_file.exists():
                cred_file.unlink()
        except OSError as e:
            self._host._logger.warning(f"Failed to remove credentials file: {e}")

    def _write_oauth_credentials(self, credentials: str) -> None:
        """Write Claude Code's active OAuth credentials.

        macOS writes the Keychain when usable (recording backend ``"keychain"``)
        and **leaves the plaintext file untouched**, mirroring Claude Code, which
        preserves the file alongside a populated Keychain for container ``~/.claude``
        sharing (#1414): cswap can't prove an existing file is its own stale fallback
        vs. a credential another consumer relies on. If the Keychain write fails — or
        the Keychain is already known unusable — it writes the plaintext file and
        best-effort clears any stale Keychain entry (#30337), recording backend
        ``"file"``. Linux/WSL/Windows always write the file.

        Raises:
            CredentialWriteError: If writing credentials fails.
        """
        if self._use_keychain():
            try:
                self._kc_call(
                    macos_keychain.set_password,
                    CLAUDE_CODE_KEYCHAIN_SERVICE,
                    macos_keychain.keychain_account_name(),
                    credentials,
                )
            except macos_keychain.KEYCHAIN_ERRORS as e:
                # _kc_call flipped routing to file mode; fall through to the file.
                # (A programming error is NOT caught here — it propagates.)
                self._host._logger.warning(f"Keychain write failed, falling back to file: {e}")
            else:
                self._last_active_credentials_backend = "keychain"
                return

        # File mode: non-macOS, macOS Keychain known unusable, or a Keychain write
        # that just failed. Write the plaintext file and (macOS) best-effort clear
        # any stale Keychain entry so Claude Code's keychain-first read can't shadow
        # it (#30337).
        try:
            self._write_active_credentials_file(credentials)
        except Exception as e:
            raise CredentialWriteError(f"Failed to write credentials: {e}")
        self._delete_active_keychain_entry()
        self._last_active_credentials_backend = "file"

    def _uses_file_backup_backend(self) -> bool:
        """Whether per-account backup *writes* go to files vs. the Keychain.

        Linux/WSL/Windows always use base64 ``.enc`` files under ``credentials_dir``
        (Windows moved off the Credential Manager because it rejects entries over
        ~2,500 bytes, #45). macOS uses the Keychain while it's usable and falls back
        to ``.enc`` files when it isn't (headless/SSH/locked); UNKNOWN platforms have
        no Keychain, so they use files too. Backup *reads* are ``.enc``-wins
        regardless (see ``_read_account_credentials``).
        """
        return not self._use_keychain()

    # -- backup credential backends ---------------------------------------
    #
    # Two backends for per-account backups: base64 ``.enc`` files under
    # ``credentials_dir`` and the macOS Keychain (``SECURITY_SERVICE``). On macOS
    # reads are ``.enc``-wins: a fallback ``.enc`` (written while the Keychain was
    # unusable) is authoritative over a possibly-stale Keychain copy, so a Keychain
    # that recovers can't shadow a newer file. A successful Keychain write
    # therefore reconciles the ``.enc`` away (correctness-critical, not best-effort).

    def _backup_enc_path(self, account_num: str, email: str) -> Path:
        return self._host.credentials_dir / f".creds-{account_num}-{email}.enc"

    def _backup_username(self, account_num: str, email: str) -> str:
        return f"account-{account_num}-{email}"

    def _kc_read_backup(self, account_num: str, email: str) -> str:
        """Read a per-account backup from the Keychain only (no file fallback).

        Routes through ``_kc_call`` (so a failure flips the capability cache).
        Returns ``""`` when the item is absent; raises on a Keychain failure so
        the caller decides (normal reads swallow it; the migration defers).
        """
        creds = self._kc_call(
            macos_keychain.get_password,
            SECURITY_SERVICE,
            self._backup_username(account_num, email),
        )
        return creds or ""

    def _kc_write_backup(self, account_num: str, email: str, credentials: str) -> None:
        """Write a per-account backup to the Keychain only. Raises on failure."""
        self._kc_call(
            macos_keychain.set_password,
            SECURITY_SERVICE,
            self._backup_username(account_num, email),
            credentials,
        )

    def _kc_delete_backup(self, account_num: str, email: str) -> None:
        """Delete a per-account backup Keychain item only. Raises on failure."""
        self._kc_call(
            macos_keychain.delete_password,
            SECURITY_SERVICE,
            self._backup_username(account_num, email),
        )

    def _delete_backup_keychain_quiet(self, account_num: str, email: str) -> None:
        """Best-effort backup Keychain delete (never raises)."""
        try:
            self._kc_delete_backup(account_num, email)
        except Exception as e:
            self._host._logger.warning(f"Failed to delete credentials from Keychain: {e}")

    def _write_backup_enc(self, account_num: str, email: str, credentials: str) -> None:
        """Atomically write a per-account backup ``.enc`` (base64) file."""
        self._host.credentials_dir.mkdir(parents=True, exist_ok=True)
        enc_file = self._backup_enc_path(account_num, email)
        encoded = base64.b64encode(credentials.encode("utf-8")).decode("utf-8")
        import tempfile
        fd, tmp_path = tempfile.mkstemp(dir=str(self._host.credentials_dir), suffix=".tmp")
        try:
            os.write(fd, encoded.encode("utf-8"))
            os.close(fd)
            fd = -1
            os.replace(tmp_path, str(enc_file))
            if sys.platform != "win32":
                os.chmod(str(enc_file), 0o600)
        except BaseException:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _reconcile_enc_after_keychain_write(
        self, account_num: str, email: str, credentials: str
    ) -> None:
        """Stop a leftover ``.enc`` from shadowing a just-written Keychain backup.

        ``.enc``-wins reads make this correctness-critical: delete the ``.enc``; if
        the delete fails, atomically rewrite it with the same fresh creds; if that
        also fails, raise so the inconsistency surfaces rather than serving stale.
        """
        enc_file = self._backup_enc_path(account_num, email)
        if not enc_file.exists():
            return
        try:
            enc_file.unlink()
            return
        except Exception as e:
            self._host._logger.warning(
                f"Could not delete .enc after Keychain backup write ({e}); "
                "rewriting it with the fresh credentials to keep both consistent"
            )
        self._write_backup_enc(account_num, email, credentials)

    def _read_account_credentials(self, account_num: str, email: str) -> str:
        """Read account credentials from backup. ``""`` when missing.

        macOS is ``.enc``-wins (a fallback file beats a possibly-stale Keychain
        copy); only an absent or corrupt ``.enc`` falls through to the Keychain.
        Linux/WSL/Windows read the ``.enc`` only.
        """
        enc_file = self._backup_enc_path(account_num, email)
        if enc_file.exists():
            try:
                encoded = enc_file.read_text(encoding="utf-8").strip()
                # validate=True: reject non-alphabet junk (e.g. "!!!!") instead of
                # silently discarding it to empty bytes, which would let a corrupt
                # .enc shadow a valid Keychain copy.
                decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
            except Exception as e:
                # Corrupt/garbled .enc → on macOS fall through to the Keychain copy.
                self._host._logger.warning(f"Failed to read credentials file: {e}")
            else:
                if decoded:
                    return decoded
                # Empty/whitespace .enc is not a real backup → try the Keychain.
        if self._host.platform == Platform.MACOS:
            try:
                return self._kc_read_backup(account_num, email)
            except macos_keychain.KEYCHAIN_ERRORS as e:
                self._host._logger.warning(f"Failed to read credentials from Keychain: {e}")
        return ""

    def _write_account_credentials(
        self, account_num: str, email: str, credentials: str
    ) -> None:
        """Write account credentials to backup (pure I/O — no session invalidation).

        macOS writes the Keychain when usable, then reconciles the ``.enc`` away
        (see ``_reconcile_enc_after_keychain_write``). When the Keychain is unusable
        it writes the ``.enc`` atomically, then best-effort deletes any stale
        Keychain copy so a recovered Keychain can't shadow the fresh file.
        Linux/WSL/Windows write the ``.enc`` only.

        Raises on a file-write failure **before** returning, so the switcher wrapper
        runs ``_post_backup_write`` exactly once and only after a successful write.
        """
        if self._use_keychain():
            try:
                self._kc_write_backup(account_num, email, credentials)
            except macos_keychain.KEYCHAIN_ERRORS as e:
                # Keychain unusable; _kc_call flipped routing to file mode.
                # (A programming error is NOT caught here — it propagates.)
                self._host._logger.warning(
                    f"Keychain backup write failed, falling back to file: {e}"
                )
            else:
                self._reconcile_enc_after_keychain_write(account_num, email, credentials)
                return

        # File mode: write the .enc atomically, then (macOS) best-effort drop the
        # stale Keychain copy so a recovered Keychain can't shadow the fresh file.
        try:
            self._write_backup_enc(account_num, email, credentials)
        except Exception as e:
            self._host._logger.warning(f"Failed to write credentials file: {e}")
            raise
        if self._host.platform == Platform.MACOS:
            self._delete_backup_keychain_quiet(account_num, email)

    def _delete_account_credentials(self, account_num: str, email: str) -> None:
        """Delete account credentials from backup (both backends on macOS).

        Removes the ``.enc`` file(s) and, on macOS, the Keychain item(s). The
        Keychain delete is best-effort: if it's locked the item may linger as
        harmless unreferenced cruft (the slot is gone from sequence.json; a re-add
        overwrites it via ``-U``; purge sweeps it). Includes the legacy
        ``account-None-{email}`` alias.
        """
        nums = [account_num]
        if str(account_num) != "None":
            nums.append("None")
        for num in nums:
            enc_file = self._backup_enc_path(num, email)
            try:
                if enc_file.exists():
                    enc_file.unlink()
            except Exception as e:
                self._host._logger.warning(f"Failed to delete credentials file: {e}")
            if self._host.platform == Platform.MACOS:
                self._delete_backup_keychain_quiet(num, email)
