"""Deliver a message into a running Claude Code session's inbox.

Claude Code (v2.1.224+ on macOS/Linux, v2.1.234+ on native Windows) binds a
per-session inbox for cross-session messaging — a Unix domain socket, or a
named pipe on Windows — and records its address as ``messagingSocketPath`` in
``<config_home>/sessions/<pid>.json``. The documented wire format is
newline-delimited JSON: an auth line ``{"type":"auth","token":...}`` (optional
on macOS/Linux, required on Windows) followed by ``{"type":"user","message":
{"role":"user","content":...}}``. An idle session starts a new turn with the
message; a busy one reads it between tool calls.

Which token authenticates the connection decides how the session treats it:

- the session's own-child token (``CLAUDE_CODE_MESSAGING_TOKEN``, exported
  only to its own hooks and tool commands): the message counts as the
  session's own hook posting back, delivered even when the session bypasses
  permission prompts (unless ``crossSessionInbound`` says otherwise);
- the peer token in ``<config_home>/sessions/<pid>.<hex>.key``: an ordinary
  message from another session, which a bypass-mode receiver holds for
  approval (and the VS Code extension can't show that dialog).

Reference: https://code.claude.com/docs/en/cross-session-messaging
"""

from __future__ import annotations

import json
import re
import socket
import sys
import time
from pathlib import Path

from claude_swap.exceptions import ClaudeSwitchError

CONNECT_TIMEOUT_S = 5.0

# Windows: every pipe instance is busy (another sender mid-write) — retry.
_ERROR_PIPE_BUSY = 231
_KEY_FILE = re.compile(r"^(\d+)\.[0-9a-f]{64}\.key$")
_PEER_TOKEN = re.compile(r"^[0-9a-f]{32}$")
_KEY_MAX_BYTES = 4096


class InboxError(ClaudeSwitchError):
    """The message could not be handed to the session's inbox."""


def inbox_address(raw: str) -> str:
    """Strip the ``uds:`` prefix Claude Code shows on inbox addresses."""
    return raw[4:] if raw.startswith("uds:") else raw


def encode_message(text: str, token: str | None) -> bytes:
    """The bytes one delivery writes: optional auth line, then the message."""
    lines = []
    if token:
        lines.append(json.dumps({"type": "auth", "token": token}))
    lines.append(json.dumps({"type": "user", "message": {"role": "user", "content": text}}))
    return ("\n".join(lines) + "\n").encode("utf-8")


def post_message(
    address: str, text: str, token: str | None, timeout: float = CONNECT_TIMEOUT_S
) -> None:
    """Write one message to a session inbox and close the connection.

    "Posted" means the session's inbox accepted the bytes — not that Claude
    has read them: the receiving session still applies its inbound controls.

    Raises:
        InboxError: the inbox isn't listening, or the write failed.
    """
    path = inbox_address(address)
    payload = encode_message(text, token)
    if sys.platform == "win32":
        _post_pipe(path, payload, timeout)
    else:
        _post_unix(path, payload, timeout)


def _post_pipe(path: str, payload: bytes, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            with open(path, "r+b", buffering=0) as pipe:
                pipe.write(payload)
            return
        except FileNotFoundError:
            raise InboxError(f"no inbox listening at {path}") from None
        except OSError as e:
            busy = getattr(e, "winerror", None) == _ERROR_PIPE_BUSY
            if not busy or time.monotonic() > deadline:
                raise InboxError(f"could not write to inbox {path}: {e}") from e
            time.sleep(0.1)


def _post_unix(path: str, payload: bytes, timeout: float) -> None:
    if sys.platform == "win32":
        raise InboxError("Unix-socket inboxes don't exist on Windows (named pipes do)")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(path)
        sock.sendall(payload)
        sock.shutdown(socket.SHUT_WR)
    except FileNotFoundError:
        raise InboxError(f"no inbox listening at {path}") from None
    except OSError as e:
        raise InboxError(f"could not write to inbox {path}: {e}") from e
    finally:
        sock.close()


def peer_token(sessions_dir: Path, pid: int, proc_start: str | None = None) -> str | None:
    """The peer auth token a session published for other local sessions.

    Key files are ``<pid>.<64 hex>.key`` holding ``{"peerToken": <32 hex>,
    "procStartFt"?: ...}``. When the session record carries a process start
    stamp, a key whose stamp disagrees belongs to a dead process that reused
    the PID and is ignored.
    """
    try:
        candidates = [p for p in sessions_dir.iterdir() if p.name.startswith(f"{pid}.")]
    except OSError:
        return None
    for path in candidates:
        match = _KEY_FILE.match(path.name)
        if not match or int(match.group(1)) != pid:
            continue
        try:
            if path.stat().st_size > _KEY_MAX_BYTES:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        token = data.get("peerToken")
        if not isinstance(token, str) or not _PEER_TOKEN.match(token):
            continue
        stamp = data.get("procStartFt") or data.get("procStart")
        if proc_start and stamp and str(stamp) != str(proc_start):
            continue
        return token
    return None
