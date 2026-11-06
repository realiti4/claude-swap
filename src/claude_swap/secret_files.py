"""Authenticated encryption for backup files; keys never live beside the data.

Claude Code's own active credential files are outside this format. File-backed
backups require an unlocked OS credential store; failure never downgrades to
Base64. Moving a backup directory alone does not move its decryption key.
"""

from __future__ import annotations

import base64
import json
import hashlib
import sys
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from claude_swap import macos_keychain
from claude_swap.exceptions import CredentialReadError, CredentialWriteError
from claude_swap.fsutil import atomic_write_text
from claude_swap.locking import FileLock

PREFIX = "cswap-vault-v1:"
SERVICE = "claude-swap-file-encryption"
ACCOUNT = "master-key-v1"


def _backend():
    if sys.platform == "darwin":
        return macos_keychain
    import keyring

    backend = keyring.get_keyring()
    # Never accept keyrings.alt plaintext/file backends or an unexamined chain.
    candidates = getattr(backend, "backends", (backend,))
    for candidate in candidates:
        if type(candidate).__module__ in {
            "keyring.backends.Windows", "keyring.backends.SecretService",
            "keyring.backends.libsecret", "keyring.backends.macOS",
        }:
            return candidate
    raise CredentialWriteError(
        "An OS credential store is required for encrypted backups. Unlock "
        "Keychain, Windows Credential Manager, or Secret Service; plaintext "
        "keyring backends are not supported."
    )


def _cipher(directory: Path, *, create: bool) -> Fernet:
    # Serialize first use across processes. Do not cache keys across lock/sleep.
    with FileLock(directory / ".encryption-key.lock"):
        try:
            backend = _backend()
            account = ACCOUNT + ":" + hashlib.sha256(str(directory.resolve()).encode()).hexdigest()
            key = backend.get_password(SERVICE, account)
            if key is None:
                if not create:
                    raise CredentialReadError("Backup decryption key is unavailable")
                for path in directory.iterdir():
                    if path.is_file():
                        with path.open("rb") as existing:
                            if existing.read(len(PREFIX)) == PREFIX.encode("ascii"):
                                raise CredentialReadError(
                                    "Encryption key missing for existing backups; refusing to replace it"
                                )
                key = Fernet.generate_key().decode("ascii")
                backend.set_password(SERVICE, account, key)
                if backend.get_password(SERVICE, account) != key:
                    raise CredentialWriteError("Could not verify backup encryption key")
            return Fernet(key.encode("ascii"))
        except (CredentialReadError, CredentialWriteError):
            raise
        except Exception as exc:
            # Backend errors may contain secret arguments; never repeat them.
            raise CredentialReadError(
                "OS credential store unavailable; refusing unencrypted storage"
            ) from exc


def seal(path: Path, value: str) -> str:
    # Authenticate the filename too: swapping two account files must fail.
    payload = json.dumps({"file": path.name, "value": value}).encode("utf-8")
    return PREFIX + _cipher(path.parent, create=True).encrypt(payload).decode("ascii")


def unseal(path: Path, encoded: str) -> str:
    if not encoded.startswith(PREFIX):
        # Legacy compatibility. It is encrypted on the next successful write;
        # a read must not race a token refresh by rewriting the stored generation.
        return base64.b64decode(encoded, validate=True).decode("utf-8")
    try:
        decoded = _cipher(path.parent, create=False).decrypt(
            encoded[len(PREFIX):].encode("ascii")
        )
        payload = json.loads(decoded)
        if payload["file"] != path.name or not isinstance(payload["value"], str):
            raise ValueError("wrong file binding")
        return payload["value"]
    except CredentialReadError:
        raise
    except CredentialWriteError as exc:
        # Backend configuration failures during a read are unreadable, never
        # an absent/corrupt backup that callers may safely overwrite.
        raise CredentialReadError("Encrypted backup key is unavailable") from exc
    except (InvalidToken, ValueError, KeyError, TypeError) as exc:
        raise CredentialReadError("Encrypted backup failed authentication") from exc


def write_secret(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write_text(path, seal(path, value), follow_symlinks=False)


def migrate_backups(switcher) -> int:
    """Encrypt legacy backups under the same lock as account mutations.

    Never follows symlinks or rewrites native Claude Code session files.
    Partial progress is safe: encrypted files are skipped on the next run.
    """
    count = 0
    with FileLock(switcher.lock_file):
        for directory, pattern in (
            (switcher.credentials_dir, "*.enc"),
            (switcher.credentials_dir, "*.enc.prev"),
            (switcher.credentials_dir, ".swap-staging-*.json"),
            (switcher.configs_dir, ".claude-config-*.json"),
        ):
            for path in directory.glob(pattern):
                if path.is_symlink() or not path.is_file():
                    raise CredentialReadError("Refusing non-regular backup during migration")
                text = path.read_text(encoding="utf-8").strip()
                if text.startswith(PREFIX):
                    continue
                if path.suffix == ".json":
                    value = text
                    json.loads(value)
                else:
                    value = unseal(path, text)
                write_secret(path, value)
                count += 1
    return count
