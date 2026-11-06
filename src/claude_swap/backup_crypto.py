"""Password-encrypted portable backups (fixed, bounded scrypt parameters)."""

from __future__ import annotations

import base64
import getpass
import json
import os
import warnings

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from claude_swap.exceptions import TransferError

ALGORITHM = "scrypt-N32768-r8-p1+A256GCM"
AAD = b"claude-swap-backup-v1"


def prompt_password(*, confirm: bool = False) -> str:
    try:
        with warnings.catch_warnings():
            # getpass must never fall back to echoing input on a pipe.
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("Backup password: ")
            if confirm and password != getpass.getpass("Repeat backup password: "):
                raise TransferError("Backup passwords do not match")
    except (getpass.GetPassWarning, EOFError) as exc:
        raise TransferError(
            "An interactive terminal is required for the backup password. "
            "For an external encryption pipeline, explicitly use export --plaintext."
        ) from exc
    if not password:
        raise TransferError("Backup password must not be empty")
    return password


def _key(password: str, salt: bytes) -> bytes:
    if not password:
        raise TransferError("Backup password must not be empty")
    return Scrypt(salt=salt, length=32, n=32768, r=8, p=1).derive(password.encode("utf-8"))


def encrypt_backup(payload: dict, password: str) -> dict:
    salt, nonce = os.urandom(16), os.urandom(12)
    ciphertext = AESGCM(_key(password, salt)).encrypt(
        nonce, json.dumps(payload).encode("utf-8"), AAD
    )
    return {
        "version": 1, "encrypted": True, "algorithm": ALGORITHM,
        "salt": base64.b64encode(salt).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }


def decrypt_backup(envelope: dict, password: str) -> dict:
    try:
        if envelope.get("algorithm") != ALGORITHM:
            raise TransferError("Unsupported backup encryption algorithm")
        salt, nonce, ciphertext = (
            base64.b64decode(envelope[name], validate=True)
            for name in ("salt", "nonce", "ciphertext")
        )
        if len(salt) != 16 or len(nonce) != 12:
            raise ValueError("invalid salt/nonce")
        decoded = AESGCM(_key(password, salt)).decrypt(nonce, ciphertext, AAD)
        result = json.loads(decoded)
        if not isinstance(result, dict) or result.get("version") != 1:
            raise ValueError("invalid payload")
        return result
    except TransferError:
        raise
    except (InvalidTag, ValueError, KeyError, TypeError) as exc:
        raise TransferError("Incorrect backup password or damaged backup") from exc
