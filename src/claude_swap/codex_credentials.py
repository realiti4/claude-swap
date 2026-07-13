"""Codex CLI credential read/write — the ``~/.codex/auth.json`` counterpart
of :mod:`claude_swap.credentials`.

Per the research in task 0.1.1 (``docs/planning/phase-0/sprint-1/
codex-format-notes.md``): Codex CLI stores its credentials in
``~/.codex/auth.json`` (overridable via ``CODEX_HOME``), either as
OAuth-style tokens (``auth_mode: "chatgpt"``, an ``access_token``/
``refresh_token``/``id_token`` triple plus ``account_id``) or a static
``OPENAI_API_KEY``. The live identity (email) is not a top-level field —
it's a claim inside the ``id_token`` JWT payload, decoded here without any
signature verification (a read-only extraction of a claim the Codex CLI
itself already trusts, having received and stored the token from its own
OAuth flow; this never authenticates on the token's behalf).

Backup storage mirrors :mod:`claude_swap.credentials`' two backends (base64
``.enc`` files on Linux/Windows, the macOS Keychain via
:mod:`claude_swap.macos_keychain`) at a smaller scale appropriate to this
module's scope (add/list/remove only — no Keychain-vs-file reconciliation
dance, no Windows Credential Manager migration path, since Codex support is
starting fresh rather than carrying years of accumulated edge cases). A
dedicated Keychain service name and file prefix keep Codex backups from ever
colliding with Claude's.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import sys
import tempfile
from pathlib import Path

from claude_swap import macos_keychain
from claude_swap.models import Platform

# Distinct from credentials.py's CLAUDE_CODE_KEYCHAIN_SERVICE/SECURITY_SERVICE
# so a Codex and a Claude backup for the same slot number never collide.
CODEX_KEYCHAIN_SERVICE = "claude-swap-codex-backup"


def codex_home() -> Path:
    """``~/.codex``, overridable via ``CODEX_HOME`` (matches the Codex CLI)."""
    override = os.environ.get("CODEX_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".codex"


def codex_auth_path() -> Path:
    return codex_home() / "auth.json"


def read_live_auth() -> dict | None:
    """Read+parse the live ``~/.codex/auth.json``; ``None`` if missing/corrupt."""
    try:
        raw = codex_auth_path().read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def read_live_auth_text() -> str | None:
    """Raw text of the live auth.json, for storing as a backup blob."""
    try:
        return codex_auth_path().read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return None


def _b64url_decode(segment: str) -> bytes:
    padded = segment + "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(padded)


def decode_id_token_claims(id_token: str) -> dict | None:
    """Decode a JWT's payload claims without verifying its signature.

    ``None`` on any malformed input (wrong segment count, bad base64, bad
    JSON) — never raises, since a corrupt/foreign token should degrade to
    "no identity available", not crash account discovery.
    """
    parts = id_token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = json.loads(_b64url_decode(parts[1]))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError, binascii.Error):
        return None
    return payload if isinstance(payload, dict) else None


def get_current_codex_identity(auth_data: dict | None = None) -> tuple[str, str] | None:
    """``(email, account_id)`` for the live Codex login, or ``None``.

    ``None`` covers every case that isn't a usable OAuth identity: no
    auth.json, API-key-only mode (no ``tokens``), or an ``id_token`` that
    doesn't decode to an ``email`` claim.
    """
    data = auth_data if auth_data is not None else read_live_auth()
    if not data:
        return None
    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        return None
    id_token = tokens.get("id_token")
    if not id_token or not isinstance(id_token, str):
        return None
    claims = decode_id_token_claims(id_token)
    if not claims:
        return None
    email = claims.get("email")
    if not email or not isinstance(email, str):
        return None
    account_id = tokens.get("account_id", "") or ""
    return (email, account_id)


def get_live_api_key(auth_data: dict | None = None) -> str | None:
    """The static ``OPENAI_API_KEY`` from auth.json, if that's the active mode."""
    data = auth_data if auth_data is not None else read_live_auth()
    if not data:
        return None
    key = data.get("OPENAI_API_KEY")
    return key if isinstance(key, str) and key else None


class CodexCredentialStore:
    """Per-account backup storage for a Codex credential blob.

    ``write``/``read``/``delete`` operate on the *stored* blob (an opaque
    string — the full ``auth.json`` JSON text, or a raw API key) keyed by
    ``(account_num, email)``, mirroring :class:`credentials.CredentialStore`'s
    file-naming and macOS Keychain-vs-file split at a scope appropriate to
    add/list/remove (no live-credential activation — that's a later task).
    """

    def __init__(self, credentials_dir: Path, platform: Platform) -> None:
        self.credentials_dir = credentials_dir
        self.platform = platform

    def _uses_file_backend(self) -> bool:
        return self.platform != Platform.MACOS

    def _enc_path(self, account_num: str, email: str) -> Path:
        return self.credentials_dir / f".codex-creds-{account_num}-{email}.enc"

    def _keychain_account(self, account_num: str, email: str) -> str:
        return f"codex-account-{account_num}-{email}"

    def write(self, account_num: str, email: str, blob: str) -> None:
        if self._uses_file_backend():
            self._write_enc(account_num, email, blob)
            return
        try:
            macos_keychain.set_password(
                CODEX_KEYCHAIN_SERVICE, self._keychain_account(account_num, email), blob
            )
        except macos_keychain.KEYCHAIN_ERRORS:
            self._write_enc(account_num, email, blob)
            return
        # Keychain write succeeded — drop any stale .enc so reads stay
        # Keychain-authoritative (mirrors credentials.py's convention).
        enc = self._enc_path(account_num, email)
        if enc.exists():
            try:
                enc.unlink()
            except OSError:
                pass

    def _write_enc(self, account_num: str, email: str, blob: str) -> None:
        encoded = base64.b64encode(blob.encode("utf-8")).decode("ascii")
        path = self._enc_path(account_num, email)
        path.parent.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            os.chmod(path.parent, 0o700)
        fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            os.write(fd, encoded.encode("ascii"))
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

    def read(self, account_num: str, email: str) -> str:
        """Empty string when nothing is stored (never raises)."""
        enc = self._enc_path(account_num, email)
        if enc.exists():
            try:
                raw = enc.read_text(encoding="utf-8").strip()
                if raw:
                    return base64.b64decode(raw).decode("utf-8")
            except (OSError, UnicodeDecodeError, binascii.Error):
                pass
        if not self._uses_file_backend():
            try:
                value = macos_keychain.get_password(
                    CODEX_KEYCHAIN_SERVICE, self._keychain_account(account_num, email)
                )
                return value or ""
            except macos_keychain.KEYCHAIN_ERRORS:
                return ""
        return ""

    def delete(self, account_num: str, email: str) -> None:
        enc = self._enc_path(account_num, email)
        if enc.exists():
            try:
                enc.unlink()
            except OSError:
                pass
        if not self._uses_file_backend():
            try:
                macos_keychain.delete_password(
                    CODEX_KEYCHAIN_SERVICE, self._keychain_account(account_num, email)
                )
            except macos_keychain.KEYCHAIN_ERRORS:
                pass
