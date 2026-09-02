"""Local bridge relay — the browser MCP follows a switch *without a restart*.

Background
----------
The "Claude in Chrome" extension and Claude Code pair through Anthropic's bridge
at ``wss://bridge.claudeusercontent.com/chrome/<accountUuid>``. The account UUID
is the *room*: both peers must resolve to the same account to see each other. A
running Claude Code session binds that account at process start, so a mid-session
``cswap switch`` (which swaps the Keychain credential) is *not* adopted by the
already-open bridge connection — the browser MCP stays on the old account until
the process restarts.

Claude Code ships a native escape hatch: with the env var ``LOCAL_BRIDGE=1`` it
connects to a plaintext ``ws://localhost:8765/chrome/dev_user_local`` with a
tokenless connect (``{"type":"connect","client_type":"claude-code",
"dev_user_id":"dev_user_local"}``) — account-agnostic.

This module is the other half: a local relay listening on that port. It swallows
Claude Code's tokenless local connect and originates an *authenticated* upstream
connection to the real bridge in the **current cswap account's** room, then
relays every frame both ways. The extension is left untouched on the real bridge
(cswap keeps switching it via CDP, so ordinary extension updates keep working).
When ``cswap switch`` changes the active account, the relay re-points its
upstream to the new room while the downstream Claude Code connection stays up —
so the browser follows the switch live, no restart.

Enable by running the daemon (``cswap chrome bridge``) and launching Claude Code
with ``LOCAL_BRIDGE=1``.

Caveat: ``LOCAL_BRIDGE`` / ``dev_user_local`` are internal Claude Code dev
features and may change between versions. macOS-only for now, like the rest of
the Chrome sync.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import plistlib
import select
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

from claude_swap.chrome_session import ChromeVault, is_supported

_logger = logging.getLogger("claude-swap")

BRIDGE_HOST = "bridge.claudeusercontent.com"
LOCAL_ROOM = "dev_user_local"
_WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_ACCOUNT_POLL_SECONDS = 3.0

LAUNCHD_LABEL = "com.claude-swap.bridge"


# --------------------------------------------------------------------------- #
# WebSocket framing (server side unmasked out; client side masked out)
# --------------------------------------------------------------------------- #
def _recvn(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


def _read_frame(sock: socket.socket):
    """Return (opcode, payload) or None on EOF. Handles masked client frames."""
    hdr = _recvn(sock, 2)
    if len(hdr) < 2:
        return None
    op = hdr[0] & 0x0F
    ln = hdr[1] & 0x7F
    masked = hdr[1] & 0x80
    if ln == 126:
        ln = struct.unpack(">H", _recvn(sock, 2))[0]
    elif ln == 127:
        ln = struct.unpack(">Q", _recvn(sock, 8))[0]
    mask = _recvn(sock, 4) if masked else b""
    data = _recvn(sock, ln)
    if masked and data:
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    return op, data


def _write_frame(sock: socket.socket, op: int, payload=b"", *, mask: bool = False) -> None:
    if isinstance(payload, str):
        payload = payload.encode()
    header = bytearray([0x80 | op])
    n = len(payload)
    mbit = 0x80 if mask else 0
    if n < 126:
        header.append(mbit | n)
    elif n < 65536:
        header.append(mbit | 126)
        header += struct.pack(">H", n)
    else:
        header.append(mbit | 127)
        header += struct.pack(">Q", n)
    if mask:
        mk = os.urandom(4)
        header += mk
        payload = bytes(b ^ mk[i % 4] for i, b in enumerate(payload))
    sock.sendall(bytes(header) + payload)


# --------------------------------------------------------------------------- #
# Current account + upstream connection
# --------------------------------------------------------------------------- #
def _tls_context() -> ssl.SSLContext:
    """A verifying TLS context using the OS trust store (via truststore, a
    project dependency) so no CA bundle wrangling — and never unverified."""
    try:
        import truststore
        truststore.inject_into_ssl()
    except Exception as e:  # noqa: BLE001
        _logger.debug("chrome-bridge: truststore unavailable (%s); using default CAs", e)
    return ssl.create_default_context()


def _active_account(backup_root: Path) -> dict | None:
    """The current cswap account's tokens+uuid, or None if unavailable."""
    try:
        data = json.loads((Path(backup_root) / "sequence.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    num = data.get("activeAccountNumber")
    if num is None:
        return None
    session = ChromeVault(backup_root).get(str(num))
    if session is None:
        return None
    return {"num": str(num), "token": session.access_token,
            "uuid": session.account_uuid, "email": session.email}


def _open_upstream(uuid: str, token: str, ctx: ssl.SSLContext) -> socket.socket | None:
    """Open a verified WSS connection to the real bridge room for ``uuid`` and
    send the authenticated claude-code connect. Returns the socket or None."""
    try:
        raw = socket.create_connection((BRIDGE_HOST, 443), timeout=15)
        sock = ctx.wrap_socket(raw, server_hostname=BRIDGE_HOST)
    except Exception as e:  # noqa: BLE001
        _logger.warning("chrome-bridge: upstream TLS connect failed: %s", e)
        return None
    key = base64.b64encode(os.urandom(16)).decode()
    path = f"/chrome/{uuid}"
    sock.sendall((
        f"GET {path} HTTP/1.1\r\nHost: {BRIDGE_HOST}\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    ).encode())
    resp = b""
    try:
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                break
            resp += chunk
    except OSError:
        resp = b""
    if b" 101 " not in resp.split(b"\r\n", 1)[0]:
        _logger.warning("chrome-bridge: upstream handshake failed: %r", resp[:120])
        sock.close()
        return None
    connect = {"type": "connect", "client_type": "claude-code",
               "oauth_token": token, "hb_capable": True, "cancel_capable": True}
    _write_frame(sock, 1, json.dumps(connect), mask=True)
    return sock


# --------------------------------------------------------------------------- #
# Per-connection relay with live account re-pointing
# --------------------------------------------------------------------------- #
def _relay_connection(down: socket.socket, backup_root: Path, ctx: ssl.SSLContext) -> None:
    # WebSocket handshake with the downstream Claude Code client.
    req = b""
    try:
        while b"\r\n\r\n" not in req:
            chunk = down.recv(4096)
            if not chunk:
                return
            req += chunk
    except OSError:
        return
    head = req.decode("latin1", "replace")
    key = ""
    for line in head.split("\r\n"):
        if line.lower().startswith("sec-websocket-key:"):
            key = line.split(":", 1)[1].strip()
    accept = base64.b64encode(hashlib.sha1((key + _WS_MAGIC).encode()).digest()).decode()
    down.sendall((
        "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
        f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n"
    ).encode())

    # Swallow Claude Code's tokenless local connect; we originate our own upstream.
    first = _read_frame(down)
    if first is None:
        down.close()
        return

    acct = _active_account(backup_root)
    if acct is None:
        _logger.warning("chrome-bridge: no current cswap account with tokens; closing")
        down.close()
        return
    up = _open_upstream(acct["uuid"], acct["token"], ctx)
    if up is None:
        down.close()
        return
    _logger.info("chrome-bridge: relaying Claude Code ↔ account %s (%s)",
                 acct["num"], acct["email"] or acct["uuid"][:8])
    cur_uuid = acct["uuid"]
    last_poll = time.monotonic()

    try:
        while True:
            rlist, _, _ = select.select([down, up], [], [], 1.0)
            if down in rlist:
                fr = _read_frame(down)
                if fr is None:
                    break
                op, data = fr
                if op == 8:  # close from Claude Code → tear the whole thing down
                    break
                _write_frame(up, op, data, mask=True)
            if up in rlist:
                fr = _read_frame(up)
                if fr is None:
                    # Upstream dropped — try to re-establish on the current account.
                    up = _reopen(up, backup_root, ctx)
                    if up is None:
                        break
                    cur_uuid = _active_account(backup_root)["uuid"]
                    continue
                op, data = fr
                if op == 8:
                    up = _reopen(up, backup_root, ctx)
                    if up is None:
                        break
                    cur_uuid = _active_account(backup_root)["uuid"]
                    continue
                _write_frame(down, op, data, mask=False)

            # Live re-point: if cswap switched the active account, reconnect
            # upstream to the new room while the Claude Code side stays put.
            now = time.monotonic()
            if now - last_poll >= _ACCOUNT_POLL_SECONDS:
                last_poll = now
                acct = _active_account(backup_root)
                if acct and acct["uuid"] != cur_uuid:
                    _logger.info("chrome-bridge: account changed → re-pointing upstream to %s (%s)",
                                 acct["num"], acct["email"] or acct["uuid"][:8])
                    try:
                        up.close()
                    except OSError:
                        pass
                    up = _open_upstream(acct["uuid"], acct["token"], ctx)
                    if up is None:
                        break
                    cur_uuid = acct["uuid"]
    except Exception as e:  # noqa: BLE001
        _logger.info("chrome-bridge: relay ended: %s", e)
    finally:
        for s in (down, up):
            try:
                s.close()
            except (OSError, AttributeError):
                pass


def _reopen(old: socket.socket, backup_root: Path, ctx: ssl.SSLContext) -> socket.socket | None:
    try:
        old.close()
    except OSError:
        pass
    acct = _active_account(backup_root)
    if acct is None:
        return None
    return _open_upstream(acct["uuid"], acct["token"], ctx)


# --------------------------------------------------------------------------- #
# Server
# --------------------------------------------------------------------------- #
def serve(backup_root: Path, port: int = 8765, *, host: str = "127.0.0.1") -> None:
    """Run the relay server (blocking). One relay thread per Claude Code client."""
    if not is_supported():
        _logger.info("chrome-bridge: unsupported platform; not starting")
        return
    ctx = _tls_context()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(16)
    _logger.info("chrome-bridge: relay listening on ws://%s:%s (LOCAL_BRIDGE=1 to use it)", host, port)
    try:
        while True:
            conn, _addr = srv.accept()
            threading.Thread(
                target=_relay_connection, args=(conn, Path(backup_root), ctx), daemon=True
            ).start()
    except KeyboardInterrupt:
        pass
    finally:
        srv.close()


# --------------------------------------------------------------------------- #
# Persistence via a LaunchAgent (mirrors the menu bar app's setup)
# --------------------------------------------------------------------------- #
def is_running(port: int) -> bool:
    """Whether something is already listening on the relay port."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def _cswap_executable() -> str:
    """Absolute path to the ``cswap`` CLI (for the LaunchAgent ProgramArguments)."""
    found = shutil.which("cswap")
    if found:
        return str(Path(found).resolve())
    # Fall back to a console-script beside the running interpreter.
    guess = Path(sys.executable).parent / "cswap"
    return str(guess)


def _launchagent_path() -> Path:
    return Path(os.path.expanduser("~/Library/LaunchAgents")) / f"{LAUNCHD_LABEL}.plist"


def launchd_loaded() -> bool:
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=8)
        return LAUNCHD_LABEL in out.stdout
    except Exception:  # noqa: BLE001
        return False


def install_launchagent(port: int) -> Path:
    """Write + load the relay LaunchAgent so it runs on login and restarts on exit."""
    plist = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [_cswap_executable(), "chrome", "bridge", "--port", str(port)],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "StandardOutPath": "/tmp/cswap-bridge.log",
        "StandardErrorPath": "/tmp/cswap-bridge.err",
    }
    path = _launchagent_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(plist))
    uid = os.getuid()
    # bootout an old copy first (ignore failure), then bootstrap the new one.
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LAUNCHD_LABEL}"],
                   capture_output=True, text=True)
    subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(path)],
                   capture_output=True, text=True)
    return path


def uninstall_launchagent() -> bool:
    """Unload + remove the relay LaunchAgent. Returns True if a plist was removed."""
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LAUNCHD_LABEL}"],
                   capture_output=True, text=True)
    path = _launchagent_path()
    if path.exists():
        path.unlink()
        return True
    return False
