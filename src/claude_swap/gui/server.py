"""Browser shell: serve the GUI page on localhost and open the default browser.

Used where no native webview is bundled (the Linux build, to stay small) or
when the webview fails to start (e.g. a Windows machine without the WebView2
runtime). The same ``GuiApi`` object answers ``POST /api/<method>``.

Only loopback is bound, every API call must carry the per-launch token that
was injected into the page, and the Host header must name the loopback
address — so another site open in the same browser cannot drive the API
(no CORS, and DNS rebinding fails the Host check).
"""

from __future__ import annotations

import json
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from claude_swap.gui.api import GuiApi

# The page pings every 5s; with no ping for this long the tab is gone.
# Generous because browsers throttle timers in hidden tabs to ~1/min.
IDLE_EXIT_S = 150.0


def serve(api: GuiApi, page: str) -> int:
    token = secrets.token_urlsafe(24)
    html = page.replace("__CSWAP_TOKEN__", token).encode("utf-8")
    httpd: ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:  # silence per-request logging
            pass

        def _host_ok(self) -> bool:
            host = self.headers.get("Host", "")
            port = httpd.server_address[1]
            return host in (f"127.0.0.1:{port}", f"localhost:{port}")

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if not self._host_ok():
                self._send(403, b"forbidden", "text/plain")
                return
            if self.path != f"/?t={token}":
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, html, "text/html; charset=utf-8")

        def do_POST(self) -> None:
            if not self._host_ok() or self.headers.get("X-Token") != token:
                self._send(403, b"forbidden", "text/plain")
                return
            name = self.path.removeprefix("/api/")
            method = getattr(api, name, None) if not name.startswith("_") else None
            if method is None or not callable(method):
                self._send(404, b"not found", "text/plain")
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                args = json.loads(self.rfile.read(length) or b"[]")
                if not isinstance(args, list):
                    raise ValueError("arguments must be a list")
                result = method(*args)
                body = json.dumps({"result": result}).encode("utf-8")
            except Exception as e:
                body = json.dumps({"error": str(e)}).encode("utf-8")
            self._send(200, body, "application/json")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    url = f"http://127.0.0.1:{httpd.server_address[1]}/?t={token}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"claude-swap GUI: {url}")
    webbrowser.open(url)

    api.ping()
    try:
        while time.time() - api._last_ping < IDLE_EXIT_S:
            time.sleep(2)
    except KeyboardInterrupt:
        pass
    finally:
        api.shutdown()
        httpd.shutdown()
    return 0
