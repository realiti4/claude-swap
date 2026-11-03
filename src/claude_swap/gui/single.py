"""One window per user: a second launch brings the running one forward.

With the tray, the window is often hidden rather than closed, so launching
the app again is how people look for it. The first instance listens on a
loopback port recorded (with a random token) in the backup directory; a later
launch connects, asks it to show itself, and exits.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import threading
from pathlib import Path
from typing import Callable

_FILE = "gui-instance.json"


def _ask_running(path: Path) -> bool:
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
        with socket.create_connection(("127.0.0.1", int(info["port"])), timeout=2) as conn:
            conn.sendall(f"{info['token']} show\n".encode())
            return conn.recv(16).startswith(b"ok")
    except (OSError, ValueError, KeyError, TypeError):
        return False


def claim(backup_dir: Path, on_show: Callable[[], None]) -> bool:
    """True for the first instance; False after waking the one already running."""
    path = backup_dir / _FILE
    if _ask_running(path):
        return False
    token = secrets.token_hex(16)
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(4)
    backup_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(_FILE + ".tmp")
    tmp.write_text(json.dumps({"port": server.getsockname()[1], "token": token, "pid": os.getpid()}), encoding="utf-8")
    os.replace(tmp, path)

    def serve() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                try:
                    conn.settimeout(2)
                    line = conn.recv(256).decode(errors="replace").strip()
                    if line == f"{token} show":
                        conn.sendall(b"ok")
                        on_show()
                except OSError:
                    pass

    threading.Thread(target=serve, daemon=True).start()
    return True
