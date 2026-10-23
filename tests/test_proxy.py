"""Tests for ``cswap proxy``: the pass-through and the live usage it records."""

from __future__ import annotations

import gzip
import hashlib
import http.client
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from claude_swap import proxy
from claude_swap.proxy import (
    LiveUsageRecorder,
    ProxyServer,
    SwitcherSlotResolver,
    Upstream,
    UpstreamPool,
    fingerprint_bearer,
)
from claude_swap.usage_store import UsageStore

TOKEN = "sk-ant-oat01-SECRET-access-token"
OTHER = "sk-ant-oat01-another-token"
RL = {
    "anthropic-ratelimit-unified-status": "allowed",
    "anthropic-ratelimit-unified-5h-utilization": "0.87",
    "anthropic-ratelimit-unified-5h-reset": "1792788000",
    "anthropic-ratelimit-unified-7d-utilization": "0.41",
    "anthropic-ratelimit-unified-7d-reset": "1793000000",
    "anthropic-ratelimit-unified-representative-claim": "five_hour",
}
IDENT = ("a@x.com", "")


def _creds(token: str) -> str:
    return json.dumps({"claudeAiOauth": {"accessToken": token, "refreshToken": "r-" + token}})


class FakeClock:
    def __init__(self, start: float = 100.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# -- fingerprint ---------------------------------------------------------------


class TestFingerprint:
    def test_matches_the_stored_access_token_fingerprint(self):
        from claude_swap.oauth import access_token_fingerprint

        assert fingerprint_bearer(f"Bearer {TOKEN}") == access_token_fingerprint(_creds(TOKEN))

    def test_scheme_matches_case_insensitively(self):
        assert fingerprint_bearer(f"bearer {TOKEN}") == fingerprint_bearer(f"Bearer {TOKEN}")

    @pytest.mark.parametrize("value", [None, "", "Bearer", "Bearer ", "Basic abc", TOKEN])
    def test_anything_but_a_bearer_token_is_none(self, value):
        assert fingerprint_bearer(value) is None

    def test_is_a_hash_not_the_token(self):
        fp = fingerprint_bearer(f"Bearer {TOKEN}")
        assert TOKEN not in fp
        assert fp.endswith(hashlib.sha256(TOKEN.encode()).hexdigest())


# -- slot resolver -------------------------------------------------------------


class FakeSwitcher:
    """The four switcher reads the resolver needs, counted."""

    def __init__(self):
        self.live = _creds(TOKEN)
        self.current = "1"
        self.backups = {"1": _creds(TOKEN), "2": _creds(OTHER)}
        self.accounts = {
            "1": {"email": "a@x.com", "organizationUuid": ""},
            "2": {"email": "b@x.com", "organizationUuid": "org-2"},
        }
        self.scans = 0

    def _get_sequence_data(self):
        self.scans += 1
        return {"accounts": self.accounts}

    def _read_credentials(self):
        return self.live

    def current_account_number(self):
        return self.current

    def read_account_credentials(self, num, email):
        assert self.accounts[num]["email"] == email
        return self.backups.get(num, "")


class TestSwitcherSlotResolver:
    def test_the_live_login_resolves_to_the_active_slot(self):
        sw = FakeSwitcher()
        sw.backups = {}  # proves the live read alone is enough
        resolve = SwitcherSlotResolver(sw, clock=FakeClock())
        assert resolve(fingerprint_bearer(f"Bearer {TOKEN}")) == ("1", ("a@x.com", ""))

    def test_a_stored_backup_resolves_to_its_slot(self):
        resolve = SwitcherSlotResolver(FakeSwitcher(), clock=FakeClock())
        assert resolve(fingerprint_bearer(f"Bearer {OTHER}")) == ("2", ("b@x.com", "org-2"))

    def test_a_hit_is_served_from_the_cache(self):
        sw = FakeSwitcher()
        resolve = SwitcherSlotResolver(sw, clock=FakeClock())
        fp = fingerprint_bearer(f"Bearer {TOKEN}")
        resolve(fp)
        scans = sw.scans
        assert resolve(fp) == ("1", ("a@x.com", ""))
        assert sw.scans == scans

    def test_a_miss_rescans_at_most_once_per_interval(self):
        sw = FakeSwitcher()
        clock = FakeClock()
        resolve = SwitcherSlotResolver(sw, clock=clock, rescan_s=10.0)
        unknown = fingerprint_bearer("Bearer nobody")
        assert resolve(unknown) is None
        scans = sw.scans
        assert resolve(unknown) is None
        assert sw.scans == scans
        clock.advance(11)
        assert resolve(unknown) is None
        assert sw.scans == scans + 1

    def test_a_rotated_token_is_found_on_the_next_scan(self):
        sw = FakeSwitcher()
        clock = FakeClock()
        resolve = SwitcherSlotResolver(sw, clock=clock, rescan_s=10.0)
        resolve(fingerprint_bearer(f"Bearer {TOKEN}"))
        sw.live = sw.backups["1"] = _creds("rotated")  # Claude Code refreshed it
        clock.advance(11)
        assert resolve(fingerprint_bearer("Bearer rotated")) == ("1", ("a@x.com", ""))

    def test_a_switch_moves_the_live_token_to_the_new_slot(self):
        sw = FakeSwitcher()
        clock = FakeClock()
        resolve = SwitcherSlotResolver(sw, clock=clock, rescan_s=10.0)
        sw.live, sw.current = _creds("fresh-2"), "2"
        assert resolve(fingerprint_bearer("Bearer fresh-2")) == ("2", ("b@x.com", "org-2"))

    def test_an_unmanaged_live_login_resolves_to_nothing(self):
        sw = FakeSwitcher()
        sw.current = None
        sw.backups = {}
        resolve = SwitcherSlotResolver(sw, clock=FakeClock())
        assert resolve(fingerprint_bearer(f"Bearer {TOKEN}")) is None

    def test_unreadable_credentials_are_skipped(self):
        sw = FakeSwitcher()
        sw.live = None
        sw.backups = {"1": "", "2": "not json"}
        resolve = SwitcherSlotResolver(sw, clock=FakeClock())
        assert resolve(fingerprint_bearer(f"Bearer {TOKEN}")) is None

    def test_no_sequence_data_resolves_to_nothing(self):
        sw = FakeSwitcher()
        sw._get_sequence_data = lambda: None
        resolve = SwitcherSlotResolver(sw, clock=FakeClock())
        assert resolve(fingerprint_bearer(f"Bearer {TOKEN}")) is None


# -- recorder ------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    return UsageStore(tmp_path / "cache")


def _recorder(store, clock=None, resolve=None, **kw):
    fp = fingerprint_bearer(f"Bearer {TOKEN}")
    if resolve is None:
        def resolve(f):
            return ("1", IDENT) if f == fp else None
    return LiveUsageRecorder(store, resolve, clock=clock or FakeClock(), **kw)


class TestLiveUsageRecorder:
    def test_records_the_reading_for_the_resolved_slot(self, store):
        assert _recorder(store).observe(f"Bearer {TOKEN}", RL) == "1"
        live = store.entries({"1": IDENT})["1"].live_usage
        assert live["five_hour"]["pct"] == pytest.approx(87.0)
        assert live["seven_day"]["pct"] == pytest.approx(41.0)

    def test_an_unknown_token_writes_nothing(self, store):
        assert _recorder(store).observe(f"Bearer {OTHER}", RL) is None
        assert store.entries({"1": IDENT})["1"].live_usage is None

    def test_no_bearer_writes_nothing(self, store):
        assert _recorder(store).observe(None, RL) is None
        assert _recorder(store).observe("x-api-key style", RL) is None

    def test_a_reply_without_ratelimit_headers_writes_nothing(self, store):
        assert _recorder(store).observe(f"Bearer {TOKEN}", {"content-type": "x"}) is None
        assert store.entries({"1": IDENT})["1"].live_usage is None

    def test_writes_are_throttled_per_slot(self, store):
        clock = FakeClock()
        rec = _recorder(store, clock=clock, min_write_s=5.0)
        assert rec.observe(f"Bearer {TOKEN}", RL) == "1"
        clock.advance(1)
        assert rec.observe(f"Bearer {TOKEN}", RL) is None
        clock.advance(5)
        assert rec.observe(f"Bearer {TOKEN}", RL) == "1"

    def test_a_reading_at_the_limit_skips_the_throttle(self, store):
        clock = FakeClock()
        rec = _recorder(store, clock=clock, min_write_s=5.0)
        rec.observe(f"Bearer {TOKEN}", RL)
        clock.advance(1)
        rejected = {**RL, "anthropic-ratelimit-unified-status": "rejected"}
        assert rec.observe(f"Bearer {TOKEN}", rejected) == "1"
        assert store.entries({"1": IDENT})["1"].live_usage["five_hour"]["pct"] == 100.0

    def test_a_failure_is_logged_never_raised(self, store, caplog):
        def broken(_fp):
            raise RuntimeError("keychain locked")

        caplog.set_level(logging.WARNING, logger="claude-swap")
        assert _recorder(store, resolve=broken).observe(f"Bearer {TOKEN}", RL) is None
        assert "keychain locked" in caplog.text
        assert TOKEN not in caplog.text


# -- the pass-through ----------------------------------------------------------


class FakeUpstream:
    """A local HTTP server standing in for api.anthropic.com."""

    def __init__(self):
        self.requests: list[dict] = []
        self.status = 200
        self.headers: dict[str, str] = {"content-type": "text/event-stream", **RL}
        self.chunks: list[bytes] = [b"event: one\n\n", b"event: two\n\n"]
        self.between_chunks = threading.Event()
        self.between_chunks.set()
        self.chunked = True
        self.broken_pipe = threading.Event()
        # Close the socket after a reply WITHOUT a "Connection: close" header:
        # the client believes the connection is reusable and finds it dead.
        self.drop_after_reply = False
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _serve(self):
                length = int(self.headers.get("content-length") or 0)
                if self.headers.get("transfer-encoding", "").lower() == "chunked":
                    body = b""
                    while True:
                        size = int(self.rfile.readline().strip(), 16)
                        if size == 0:
                            self.rfile.readline()
                            break
                        body += self.rfile.read(size)
                        self.rfile.readline()
                else:
                    body = self.rfile.read(length)
                upstream.requests.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": body,
                        "client": self.client_address,
                    }
                )
                if upstream.drop_after_reply:
                    self.close_connection = True
                self.send_response(upstream.status)
                for k, v in upstream.headers.items():
                    self.send_header(k, v)
                if not upstream.chunked:
                    payload = b"".join(upstream.chunks)
                    self.send_header("content-length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                self.send_header("transfer-encoding", "chunked")
                self.end_headers()
                try:
                    for i, chunk in enumerate(upstream.chunks):
                        if i:
                            upstream.between_chunks.wait(5)
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    upstream.broken_pipe.set()

            do_GET = do_POST = _serve

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.thread.start()

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class SpyRecorder:
    def __init__(self):
        self.calls: list[tuple[str | None, dict]] = []

    def observe(self, authorization, headers):
        self.calls.append((authorization, {k.lower(): v for k, v in headers.items()}))
        return None


@pytest.fixture
def upstream():
    up = FakeUpstream()
    yield up
    up.close()


@pytest.fixture
def running(upstream):
    spy = SpyRecorder()
    server = ProxyServer(
        ("127.0.0.1", 0),
        spy,
        Upstream(host="127.0.0.1", port=upstream.port, tls=False, timeout_s=10),
    )
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    )
    thread.start()
    yield server, spy
    server.shutdown()
    server.server_close()


def _client(server) -> http.client.HTTPConnection:
    return http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)


def _post(server, body=b'{"model":"x"}', headers=None, path="/v1/messages?beta=true"):
    conn = _client(server)
    hdrs = {"authorization": f"Bearer {TOKEN}", "content-type": "application/json"}
    hdrs.update(headers or {})
    conn.request("POST", path, body=body, headers=hdrs)
    return conn, conn.getresponse()


class TestPassThrough:
    def test_request_arrives_unchanged(self, running, upstream):
        server, _ = running
        conn, resp = _post(server, headers={"anthropic-beta": "oauth-2025-04-20"})
        resp.read()
        conn.close()
        (req,) = upstream.requests
        assert req["method"] == "POST"
        assert req["path"] == "/v1/messages?beta=true"
        assert req["body"] == b'{"model":"x"}'
        assert req["headers"]["authorization"] == f"Bearer {TOKEN}"
        assert req["headers"]["anthropic-beta"] == "oauth-2025-04-20"

    def test_reply_arrives_unchanged(self, running):
        server, _ = running
        conn, resp = _post(server)
        body = resp.read()
        conn.close()
        assert resp.status == 200
        assert resp.getheader("content-type") == "text/event-stream"
        assert resp.getheader("anthropic-ratelimit-unified-5h-utilization") == "0.87"
        assert body == b"event: one\n\nevent: two\n\n"

    def test_reply_is_streamed_not_buffered(self, running, upstream):
        # The second event is only sent once the client has the first: a
        # buffering proxy would deadlock here and the read would time out.
        upstream.between_chunks.clear()
        server, _ = running
        conn, resp = _post(server)
        first = resp.read1(64)
        assert first == b"event: one\n\n"
        upstream.between_chunks.set()
        assert resp.read() == b"event: two\n\n"
        conn.close()

    def test_the_recorder_sees_the_token_and_the_reply_headers(self, running):
        server, spy = running
        conn, resp = _post(server)
        resp.read()
        conn.close()
        ((auth, headers),) = spy.calls
        assert auth == f"Bearer {TOKEN}"
        assert headers["anthropic-ratelimit-unified-7d-utilization"] == "0.41"

    def test_a_429_passes_through_and_is_recorded(self, running, upstream):
        upstream.status = 429
        upstream.headers = {
            "content-type": "application/json",
            **RL,
            "anthropic-ratelimit-unified-status": "rejected",
        }
        upstream.chunks = [b'{"type":"error"}']
        server, spy = running
        conn, resp = _post(server)
        assert resp.status == 429
        assert resp.read() == b'{"type":"error"}'
        conn.close()
        assert spy.calls[0][1]["anthropic-ratelimit-unified-status"] == "rejected"

    def test_compressed_bytes_pass_untouched(self, running, upstream):
        packed = gzip.compress(b"hello")
        upstream.headers = {"content-encoding": "gzip", **RL}
        upstream.chunks = [packed]
        server, _ = running
        conn, resp = _post(server, headers={"accept-encoding": "gzip"})
        assert resp.getheader("content-encoding") == "gzip"
        assert resp.read() == packed
        conn.close()
        assert upstream.requests[0]["headers"]["accept-encoding"] == "gzip"

    def test_a_chunked_request_body_is_forwarded_whole(self, running, upstream):
        server, _ = running
        conn = _client(server)
        conn.putrequest("POST", "/v1/messages")
        conn.putheader("authorization", f"Bearer {TOKEN}")
        conn.putheader("transfer-encoding", "chunked")
        conn.endheaders()
        for part in (b'{"a":', b"1}"):
            conn.send(b"%x\r\n%s\r\n" % (len(part), part))
        conn.send(b"0\r\n\r\n")
        conn.getresponse().read()
        conn.close()
        assert upstream.requests[0]["body"] == b'{"a":1}'
        assert "transfer-encoding" not in upstream.requests[0]["headers"]

    def test_get_is_forwarded_too(self, running, upstream):
        server, _ = running
        conn = _client(server)
        conn.request("GET", "/v1/models", headers={"authorization": f"Bearer {TOKEN}"})
        conn.getresponse().read()
        conn.close()
        assert upstream.requests[0]["method"] == "GET"
        assert upstream.requests[0]["body"] == b""

    def test_hop_by_hop_headers_are_not_forwarded(self, running, upstream):
        server, _ = running
        conn, resp = _post(server, headers={"keep-alive": "timeout=5", "proxy-authorization": "x"})
        resp.read()
        conn.close()
        sent = upstream.requests[0]["headers"]
        assert "keep-alive" not in sent
        assert "proxy-authorization" not in sent
        assert sent["host"] == f"127.0.0.1:{upstream.port}"


class TestConnections:
    def test_a_content_length_reply_keeps_its_length(self, running, upstream):
        upstream.chunked = False
        upstream.chunks = [b'{"ok":', b"true}"]
        server, _ = running
        conn, resp = _post(server)
        assert resp.getheader("content-length") == "11"
        assert resp.getheader("transfer-encoding") is None
        assert resp.read() == b'{"ok":true}'
        conn.close()

    def test_two_requests_share_one_client_connection(self, running, upstream):
        # Claude Code keeps its connection to the base URL alive between calls.
        server, _ = running
        conn = _client(server)
        for _ in range(2):
            conn.request("POST", "/v1/messages", body=b"{}", headers={"authorization": f"Bearer {TOKEN}"})
            assert conn.getresponse().read() == b"event: one\n\nevent: two\n\n"
        conn.close()
        assert len(upstream.requests) == 2

    def test_a_client_that_leaves_mid_stream_does_not_stop_the_proxy(self, running, upstream):
        upstream.chunks = [b"event: one\n\n"] + [b"x" * 65536] * 64
        upstream.between_chunks.clear()
        server, _ = running
        conn, resp = _post(server)
        assert resp.read1(64) == b"event: one\n\n"
        conn.sock.close()  # the session is killed mid-reply
        conn.close()
        upstream.between_chunks.set()
        upstream.chunks = [b"next"]
        conn2, resp2 = _post(server)
        assert resp2.read() == b"next"
        conn2.close()

    def test_request_trailers_are_consumed(self, running, upstream):
        server, _ = running
        conn = _client(server)
        conn.putrequest("POST", "/v1/messages")
        conn.putheader("transfer-encoding", "chunked")
        conn.endheaders()
        conn.send(b"2\r\n{}\r\n0\r\nx-trailer: 1\r\n\r\n")
        conn.getresponse().read()
        conn.close()
        assert upstream.requests[0]["body"] == b"{}"

    def test_a_malformed_request_line_is_answered_not_crashed(self, running, upstream):
        import socket

        server, _ = running
        with socket.create_connection(server.server_address[:2], timeout=5) as sock:
            sock.sendall(b"NONSENSE\r\n\r\n")
            reply = sock.recv(256)
        assert reply.startswith(b"HTTP/1.1 400") or reply.startswith(b"HTTP/1.0 400")
        assert upstream.requests == []


class TestUpstreamReuse:
    """A new TLS handshake to the API costs ~200 ms; Claude Code direct reuses
    its connection, so the proxy must too or every request pays it."""

    def test_sequential_requests_reuse_one_upstream_connection(self, running, upstream):
        server, _ = running
        for _ in range(3):
            conn, resp = _post(server)
            resp.read()
            conn.close()
        assert len({r["client"] for r in upstream.requests}) == 1

    def test_a_connection_the_server_dropped_is_retried_once_fresh(self, running, upstream):
        upstream.drop_after_reply = True
        server, _ = running
        for _ in range(3):
            conn, resp = _post(server)
            assert resp.status == 200
            assert resp.read() == b"event: one\n\nevent: two\n\n"
            conn.close()
        assert len(upstream.requests) == 3
        assert len({r["client"] for r in upstream.requests}) == 3

    def test_a_reply_marked_close_is_not_reused(self, running, upstream):
        upstream.headers = {**upstream.headers, "connection": "close"}
        server, _ = running
        for _ in range(2):
            conn, resp = _post(server)
            resp.read()
            conn.close()
        assert len({r["client"] for r in upstream.requests}) == 2

    def test_a_stream_the_client_abandoned_is_not_reused(self, running, upstream):
        upstream.chunks = [b"event: one\n\n"] + [b"x" * 65536] * 64
        upstream.between_chunks.clear()
        server, _ = running
        conn, resp = _post(server)
        resp.read1(64)
        conn.sock.close()
        conn.close()
        upstream.between_chunks.set()
        assert upstream.broken_pipe.wait(5)
        upstream.chunks = [b"next"]
        conn2, resp2 = _post(server)
        assert resp2.read() == b"next"
        conn2.close()
        assert upstream.requests[0]["client"] != upstream.requests[1]["client"]

    def test_a_listen_backlog_sized_for_a_fleet_of_sessions(self):
        assert ProxyServer.request_queue_size >= 64

    def test_no_address_reuse_on_windows(self):
        # On Windows SO_REUSEADDR lets a second process bind a port that is
        # already listening: the proxy would start "fine" on a port someone
        # else holds, and another process could take over the relay.
        import sys

        assert ProxyServer.allow_reuse_address is (sys.platform != "win32")

    def test_an_aborted_client_connection_is_not_an_error(self, running, monkeypatch):
        # Windows reports a client that hung up as ConnectionAbortedError.
        server, _ = running
        handler = proxy._Handler.__new__(proxy._Handler)

        class Gone:
            def write(self, data):
                raise ConnectionAbortedError

            def flush(self):
                pass

        class OneChunk:
            def __init__(self):
                self.sent = False

            def read1(self, n):
                if self.sent:
                    return b""
                self.sent = True
                return b"x"

            def getheader(self, name):
                return None

        handler.wfile = Gone()
        handler.close_connection = False
        monkeypatch.setattr(handler, "send_response_only", lambda *a: None, raising=False)
        monkeypatch.setattr(handler, "send_header", lambda *a: None, raising=False)
        monkeypatch.setattr(handler, "end_headers", lambda: None, raising=False)
        resp = OneChunk()
        resp.status, resp.reason = 200, "OK"
        assert handler._send_reply(resp, []) is False
        assert handler.close_connection is True


class FakeConn:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeUpstreamFactory:
    def __init__(self):
        self.made: list[FakeConn] = []

    def connect(self):
        conn = FakeConn()
        self.made.append(conn)
        return conn


class TestUpstreamPool:
    def test_a_released_connection_is_handed_out_again(self):
        factory = FakeUpstreamFactory()
        pool = UpstreamPool(factory, clock=FakeClock())
        conn, reused = pool.acquire()
        assert reused is False
        pool.release(conn)
        assert pool.acquire() == (conn, True)

    def test_fresh_skips_the_idle_connections(self):
        factory = FakeUpstreamFactory()
        pool = UpstreamPool(factory, clock=FakeClock())
        first, _ = pool.acquire()
        pool.release(first)
        conn, reused = pool.acquire(fresh=True)
        assert conn is not first
        assert reused is False

    def test_idle_connections_expire(self):
        factory = FakeUpstreamFactory()
        clock = FakeClock()
        pool = UpstreamPool(factory, clock=clock, idle_s=20.0)
        old, _ = pool.acquire()
        pool.release(old)
        clock.advance(21)
        conn, reused = pool.acquire()
        assert conn is not old
        assert reused is False
        assert old.closed

    def test_the_pool_is_capped(self):
        factory = FakeUpstreamFactory()
        pool = UpstreamPool(factory, clock=FakeClock(), max_idle=2)
        conns = [pool.acquire()[0] for _ in range(4)]
        for conn in conns:
            pool.release(conn)
        assert [c.closed for c in conns] == [False, False, True, True]

    def test_close_all_closes_the_idle_connections(self):
        factory = FakeUpstreamFactory()
        pool = UpstreamPool(factory, clock=FakeClock())
        conn, _ = pool.acquire()
        pool.release(conn)
        pool.close_all()
        assert conn.closed
        assert pool.acquire()[1] is False


class TestRefusals:
    def test_an_absolute_url_is_refused_so_it_is_no_open_proxy(self, running, upstream):
        server, spy = running
        conn = _client(server)
        conn.request("GET", "http://example.com/steal")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        assert resp.status == 400
        assert upstream.requests == []
        assert spy.calls == []

    def test_an_unreachable_upstream_is_a_502(self, upstream, caplog):
        caplog.set_level(logging.DEBUG, logger="claude-swap")
        upstream.close()
        server = ProxyServer(
            ("127.0.0.1", 0),
            SpyRecorder(),
            Upstream(host="127.0.0.1", port=upstream.port, tls=False, timeout_s=2),
        )
        thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    )
        thread.start()
        try:
            conn, resp = _post(server)
            assert resp.status == 502
            resp.read()
            conn.close()
        finally:
            server.shutdown()
            server.server_close()
        assert TOKEN not in caplog.text

    def test_no_token_reaches_the_log(self, running, caplog):
        caplog.set_level(logging.DEBUG, logger="claude-swap")
        server, _ = running
        conn, resp = _post(server)
        resp.read()
        conn.close()
        assert "POST /v1/messages" in caplog.text
        assert TOKEN not in caplog.text


class TestUpstream:
    def test_default_is_the_anthropic_api_over_tls(self):
        up = Upstream()
        assert (up.host, up.port, up.tls) == ("api.anthropic.com", 443, True)
        conn = up.connect()
        assert isinstance(conn, http.client.HTTPSConnection)
        assert conn.host == "api.anthropic.com"

    def test_plain_upstream_for_tests(self):
        conn = Upstream(host="127.0.0.1", port=1, tls=False).connect()
        assert type(conn) is http.client.HTTPConnection


class TestRunProxy:
    @pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "example.com", "::"])
    def test_refuses_to_listen_beyond_loopback(self, host):
        with pytest.raises(ValueError, match="loopback"):
            proxy.run_proxy(host=host, port=0, switcher=FakeSwitcher())

    def test_localhost_is_accepted(self, monkeypatch, tmp_path):
        def interrupted(self):
            raise KeyboardInterrupt

        monkeypatch.setattr(ProxyServer, "serve_forever", interrupted)
        sw = FakeSwitcher()
        sw._usage_store = UsageStore(tmp_path / "cache")
        assert proxy.run_proxy(host="localhost", port=0, switcher=sw) == 0

    def test_serves_until_interrupted(self, monkeypatch, tmp_path, caplog):
        caplog.set_level(logging.INFO, logger="claude-swap")
        served = {}

        def fake_serve_forever(self):
            served["address"] = self.server_address
            raise KeyboardInterrupt

        monkeypatch.setattr(ProxyServer, "serve_forever", fake_serve_forever)
        sw = FakeSwitcher()
        sw._usage_store = UsageStore(tmp_path / "cache")
        announced = []
        assert (
            proxy.run_proxy(
                host="127.0.0.1",
                port=0,
                switcher=sw,
                on_listening=lambda h, p: announced.append((h, p)),
            )
            == 0
        )
        assert served["address"][0] == "127.0.0.1"
        assert announced == [served["address"][:2]]
        assert "ANTHROPIC_BASE_URL=http://127.0.0.1:" in caplog.text
