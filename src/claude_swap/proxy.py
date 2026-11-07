"""``cswap proxy``: a local pass-through that reads live usage off API replies.

Claude Code talks to it through ``ANTHROPIC_BASE_URL``; it forwards each
request to ``api.anthropic.com`` unchanged and streams the reply back
unchanged. On the way it reads the reply's rate-limit headers
(:mod:`claude_swap.live_usage`) and records them for the account whose access
token sent the request (``UsageStore.record_live``). ``auto`` then decides on
numbers that are seconds old instead of the last ``/api/oauth/usage`` poll,
which that endpoint's budget of about 30 requests an hour per account keeps
minutes behind a busy account.

What it will not do, by design:

- listen beyond loopback (``run_proxy`` refuses any other address), or act as
  a general proxy: an absolute-form request (``GET http://host/...``) is
  refused, and the upstream is fixed;
- store or log a token, a request body or a reply body. A request's token is
  hashed on arrival (the same ``sha256-at:`` fingerprint the store keeps) and
  only that hash is compared;
- let usage bookkeeping break a request: a failure to record is logged and
  the reply is still relayed.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import logging
import ssl
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Protocol

from claude_swap.live_usage import parse_ratelimit_headers
from claude_swap.oauth import access_token_fingerprint, fingerprint_access_token
from claude_swap.usage_store import Identity, UsageStore

_logger = logging.getLogger("claude-swap")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18999

#: Per-slot floor between two live writes. ``auto`` ticks every 60 s at the
#: most, so a few seconds of coalescing loses nothing and keeps a busy fleet
#: of sessions from rewriting the store on every reply. A reading at a limit
#: is written at once regardless.
MIN_WRITE_S = 5.0

#: How often an unknown token may trigger a rescan of the stored logins. A
#: token nobody manages (an API-key session, another tool) must not turn
#: every request into a credential read.
RESCAN_S = 10.0

#: Hop-by-hop headers (RFC 9110 7.6.1) plus the ones http.client sets itself.
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)

_CHUNK = 64 * 1024


def fingerprint_bearer(authorization: str | None) -> str | None:
    """The ``sha256-at:`` fingerprint of a ``Bearer`` header, else ``None``."""
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return fingerprint_access_token(token)


SlotResolver = Callable[[str], tuple[str, Identity] | None]


class _Switcher(Protocol):  # pragma: no cover - typing only
    def _get_sequence_data(self) -> dict | None: ...
    def _read_credentials(self) -> str | None: ...
    def current_account_number(self) -> str | None: ...
    def read_account_credentials(self, account_num: str, email: str) -> str: ...


class SwitcherSlotResolver:
    """Map a token fingerprint to the managed slot that owns it.

    The live login maps to the active slot; every slot's stored backup maps
    to that slot (a ``cswap run N`` session, or the old token in flight
    across a switch). Hits are cached; a miss rescans at most once per
    ``rescan_s``, which also picks up a token Claude Code rotated.
    """

    def __init__(
        self,
        switcher: _Switcher,
        clock: Callable[[], float] = time.monotonic,
        rescan_s: float = RESCAN_S,
    ):
        self._switcher = switcher
        self._clock = clock
        self._rescan_s = rescan_s
        self._cache: dict[str, tuple[str, Identity]] = {}
        self._scanned_at: float | None = None
        self._lock = threading.Lock()

    def __call__(self, fingerprint: str) -> tuple[str, Identity] | None:
        with self._lock:
            hit = self._cache.get(fingerprint)
            if hit is not None:
                return hit
            now = self._clock()
            if self._scanned_at is not None and now - self._scanned_at < self._rescan_s:
                return None
            self._scanned_at = now
            self._cache = self._scan()
            return self._cache.get(fingerprint)

    def _scan(self) -> dict[str, tuple[str, Identity]]:
        data = self._switcher._get_sequence_data() or {}
        accounts = data.get("accounts") or {}
        identities = {
            num: (info.get("email", ""), info.get("organizationUuid", "") or "")
            for num, info in accounts.items()
        }
        found: dict[str, tuple[str, Identity]] = {}
        current = self._switcher.current_account_number()
        live = self._switcher._read_credentials()
        if live and current in identities:
            fp = access_token_fingerprint(live)
            if fp is not None:
                found[fp] = (current, identities[current])
        for num, identity in identities.items():
            stored = self._switcher.read_account_credentials(num, identity[0])
            fp = access_token_fingerprint(stored) if stored else None
            if fp is not None:
                found.setdefault(fp, (num, identity))
        return found


class LiveUsageRecorder:
    """Turn one reply's headers into a ``record_live`` write for its slot."""

    def __init__(
        self,
        store: UsageStore,
        resolve: SlotResolver,
        clock: Callable[[], float] = time.monotonic,
        min_write_s: float = MIN_WRITE_S,
    ):
        self._store = store
        self._resolve = resolve
        self._clock = clock
        self._min_write_s = min_write_s
        self._written_at: dict[str, float] = {}
        self._lock = threading.Lock()

    def observe(self, authorization: str | None, headers: Mapping[str, str]) -> str | None:
        """Record the reading a reply carries. Returns the slot written, if any.

        Never raises: the reply is relayed whether or not its usage could be
        recorded, and a failure is logged (without the token) instead.
        """
        try:
            fingerprint = fingerprint_bearer(authorization)
            if fingerprint is None:
                return None
            usage = parse_ratelimit_headers(headers)
            if usage is None:
                return None
            hit = self._resolve(fingerprint)
            if hit is None:
                return None
            num, identity = hit
            at_limit = any(window.get("pct", 0.0) >= 100.0 for window in usage.values())
            with self._lock:
                now = self._clock()
                last = self._written_at.get(num)
                if not at_limit and last is not None and now - last < self._min_write_s:
                    return None
                self._store.record_live({num: usage}, {num: identity})
                self._written_at[num] = now
            _logger.debug(
                "proxy: live usage for slot %s: %s",
                num,
                ", ".join(f"{key} {window['pct']:.0f}%" for key, window in usage.items()),
            )
            return num
        except Exception as exc:
            _logger.warning("proxy: could not record live usage: %s", exc)
            return None


@dataclass(frozen=True)
class Upstream:
    """Where requests go. Only tests point it anywhere but the Claude API."""

    host: str = "api.anthropic.com"
    port: int = 443
    tls: bool = True
    timeout_s: float = 600.0

    def connect(self) -> http.client.HTTPConnection:
        if self.tls:
            return http.client.HTTPSConnection(
                self.host, self.port, timeout=self.timeout_s, context=ssl.create_default_context()
            )
        return http.client.HTTPConnection(self.host, self.port, timeout=self.timeout_s)


#: Idle upstream connections kept for reuse, and for how long. A new TLS
#: connection to the API costs about 200 ms; Claude Code talking to the API
#: directly reuses its own, so without this every proxied request would pay
#: it. The idle bound stays well under a server's keep-alive timeout, so a
#: reused connection is rarely one the server already closed (and when it is,
#: the request is retried once on a fresh one).
POOL_MAX_IDLE = 8
POOL_IDLE_S = 20.0

#: What a reused connection the server closed while idle raises on reuse.
#: ``RemoteDisconnected`` is a ``ConnectionResetError``.
_STALE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


class _Connector(Protocol):  # pragma: no cover - typing only
    def connect(self) -> http.client.HTTPConnection: ...


class UpstreamPool:
    """Idle keep-alive connections to the upstream, most recent first."""

    def __init__(
        self,
        upstream: _Connector,
        clock: Callable[[], float] = time.monotonic,
        max_idle: int = POOL_MAX_IDLE,
        idle_s: float = POOL_IDLE_S,
    ):
        self._upstream = upstream
        self._clock = clock
        self._max_idle = max_idle
        self._idle_s = idle_s
        self._idle: list[tuple[http.client.HTTPConnection, float]] = []
        self._lock = threading.Lock()

    def acquire(self, fresh: bool = False) -> tuple[http.client.HTTPConnection, bool]:
        """A connection, and whether it is a reused one."""
        expired = []
        try:
            with self._lock:
                now = self._clock()
                while self._idle and not fresh:
                    conn, released_at = self._idle.pop()
                    if now - released_at <= self._idle_s:
                        return conn, True
                    expired.append(conn)
        finally:
            for conn in expired:
                conn.close()
        return self._upstream.connect(), False

    def release(self, conn: http.client.HTTPConnection) -> None:
        """Keep a connection whose reply was read to the end, if there is room."""
        with self._lock:
            if len(self._idle) < self._max_idle:
                self._idle.append((conn, self._clock()))
                return
        conn.close()

    def close_all(self) -> None:
        with self._lock:
            idle, self._idle = self._idle, []
        for conn, _ in idle:
            conn.close()


class _Recorder(Protocol):  # pragma: no cover - typing only
    def observe(self, authorization: str | None, headers: Mapping[str, str]) -> str | None: ...


class ProxyServer(ThreadingHTTPServer):
    """One thread per connection, so long streams do not block each other."""

    daemon_threads = True
    # The default backlog of 5 refuses connections when many sessions start
    # requests at once (a fleet of agents behind one proxy).
    request_queue_size = 128
    # HTTPServer sets SO_REUSEADDR. On POSIX that only skips TIME_WAIT; on
    # Windows it lets a second socket bind a port that is already listening,
    # so a busy port would not be reported and another process could take
    # over the relay.
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, address: tuple[str, int], recorder: _Recorder, upstream: Upstream):
        self.recorder = recorder
        self.upstream = upstream
        self.pool = UpstreamPool(upstream)
        super().__init__(address, _Handler)

    def server_close(self) -> None:
        super().server_close()
        self.pool.close_all()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: ProxyServer

    def _read_body(self) -> bytes:
        if self.headers.get("transfer-encoding", "").lower() == "chunked":
            parts = []
            while True:
                size = int(self.rfile.readline().split(b";", 1)[0].strip(), 16)
                if size == 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass  # trailers, if any
                    return b"".join(parts)
                parts.append(self.rfile.read(size))
                self.rfile.readline()
        return self.rfile.read(int(self.headers.get("content-length") or 0))

    def _forward_headers(self) -> dict[str, str]:
        listed = {
            token.strip().lower()
            for token in self.headers.get("connection", "").split(",")
            if token.strip()
        }
        return {
            name: value
            for name, value in self.headers.items()
            if name.lower() not in _HOP_BY_HOP and name.lower() not in listed
        }

    def _refuse(self, status: int, message: str) -> None:
        body = json.dumps(
            {"type": "error", "error": {"type": "proxy_error", "message": message}}
        ).encode()
        self.send_response_only(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _relay(self) -> None:
        if not self.path.startswith("/"):
            self.close_connection = True
            self._refuse(400, "cswap proxy forwards origin-form requests only")
            return
        body = self._read_body()
        headers_out = self._forward_headers()
        pool = self.server.pool
        fresh = False
        while True:
            conn, reused = pool.acquire(fresh=fresh)
            try:
                conn.request(self.command, self.path, body=body or None, headers=headers_out)
                resp = conn.getresponse()
                break
            except (OSError, http.client.HTTPException) as exc:
                conn.close()
                if reused and isinstance(exc, _STALE):
                    # The server closed this idle connection before reading
                    # the request; send it once more on a new connection.
                    fresh = True
                    continue
                _logger.warning(
                    "proxy: %s %s: upstream %s unreachable: %s",
                    self.command,
                    self.path.split("?", 1)[0],
                    self.server.upstream.host,
                    exc.__class__.__name__,
                )
                self._refuse(502, f"cswap proxy could not reach {self.server.upstream.host}")
                return
        reusable = False
        try:
            headers = resp.getheaders()
            self.server.recorder.observe(self.headers.get("authorization"), dict(headers))
            _logger.debug(
                "proxy: %s %s -> %d", self.command, self.path.split("?", 1)[0], resp.status
            )
            reusable = self._send_reply(resp, headers) and not resp.will_close
        finally:
            if reusable:
                pool.release(conn)
            else:
                conn.close()

    def _send_reply(self, resp: http.client.HTTPResponse, headers: list[tuple[str, str]]) -> bool:
        """Relay the reply. Returns whether it was read to the end."""
        self.send_response_only(resp.status, resp.reason)
        for name, value in headers:
            if name.lower() not in _HOP_BY_HOP:
                self.send_header(name, value)
        length = resp.getheader("content-length")
        chunked = length is None
        if chunked:
            self.send_header("transfer-encoding", "chunked")
        else:
            self.send_header("content-length", length)
        self.end_headers()
        try:
            while chunk := resp.read1(_CHUNK):
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk) if chunked else chunk)
                self.wfile.flush()
            if chunked:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True  # the client went away mid-stream
            return False
        return True

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _relay

    def log_message(self, format: str, *args: object) -> None:
        pass  # _relay logs each request itself, without the query string


def _require_loopback(host: str) -> None:
    if host == "localhost":
        return
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is None or address.version != 4 or not address.is_loopback:
        raise ValueError(
            f"cswap proxy listens on a loopback IPv4 address only (got {host!r}): "
            "it relays your Claude login"
        )


def run_proxy(
    host: str,
    port: int,
    switcher,
    on_listening: Callable[[str, int], None] | None = None,
) -> int:
    """Serve until interrupted. Returns the process exit code.

    Args:
        host: Loopback address to listen on.
        port: Port to listen on (0 picks a free one).
        switcher: Initialized ClaudeAccountSwitcher.
        on_listening: Called with the bound address once the socket is
            listening, so a caller never announces a port it failed to take.

    Raises:
        ValueError: ``host`` is not a loopback IPv4 address or ``localhost``.
        OSError: the address could not be bound (port in use, ...).
    """
    _require_loopback(host)
    recorder = LiveUsageRecorder(switcher._usage_store, SwitcherSlotResolver(switcher))
    server = ProxyServer((host, port), recorder, Upstream())
    bound_host, bound_port = server.server_address[:2]
    _logger.info(
        "cswap proxy listening on %s:%d; start Claude Code with "
        "ANTHROPIC_BASE_URL=http://%s:%d",
        bound_host,
        bound_port,
        bound_host,
        bound_port,
    )
    if on_listening is not None:
        on_listening(bound_host, bound_port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
