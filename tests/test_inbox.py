"""Tests for the cross-session inbox client (inbox.py).

Delivery is exercised against a real local server of the platform's kind —
a named pipe on Windows, a Unix domain socket elsewhere — so the bytes on
the wire are the bytes Claude Code's inbox would read.
"""

from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import uuid
from pathlib import Path

import pytest

from claude_swap.inbox import (
    InboxError,
    encode_message,
    inbox_address,
    peer_token,
    post_message,
)

TOKEN = "0123456789abcdef0123456789abcdef"


def _lines(data: bytes) -> list[dict]:
    return [json.loads(line) for line in data.decode().splitlines() if line]


class TestEncoding:
    def test_auth_line_then_user_message(self):
        lines = _lines(encode_message("carry on", TOKEN))
        assert lines == [
            {"type": "auth", "token": TOKEN},
            {"type": "user", "message": {"role": "user", "content": "carry on"}},
        ]

    def test_no_token_sends_only_the_message(self):
        assert _lines(encode_message("hi", None)) == [
            {"type": "user", "message": {"role": "user", "content": "hi"}}
        ]

    def test_every_line_is_newline_terminated(self):
        assert encode_message("a\nb", TOKEN).endswith(b"\n")
        # Embedded newlines are escaped inside the JSON, never raw.
        assert len(encode_message("a\nb", TOKEN).splitlines()) == 2

    def test_uds_prefix_is_stripped(self):
        assert inbox_address("uds:/tmp/cc-socks/x.sock") == "/tmp/cc-socks/x.sock"
        assert inbox_address(r"\\.\pipe\LOCAL\cc-msg-1") == r"\\.\pipe\LOCAL\cc-msg-1"


@pytest.mark.skipif(sys.platform != "win32", reason="named pipes are Windows-only")
class TestNamedPipeDelivery:
    def test_posts_exact_bytes_to_a_named_pipe(self):
        from multiprocessing.connection import Listener

        address = rf"\\.\pipe\cswap-test-{uuid.uuid4().hex}"
        listener = Listener(address, family="AF_PIPE")
        received: list[bytes] = []

        def serve():
            with listener.accept() as conn:
                received.append(conn.recv_bytes())

        server = threading.Thread(target=serve, daemon=True)
        server.start()
        post_message(address, "carry on", TOKEN)
        server.join(timeout=5)
        listener.close()
        assert received and _lines(received[0]) == _lines(encode_message("carry on", TOKEN))

    def test_missing_pipe_raises(self):
        with pytest.raises(InboxError, match="no inbox"):
            post_message(rf"\\.\pipe\cswap-missing-{uuid.uuid4().hex}", "x", TOKEN, timeout=0.5)


@pytest.fixture
def short_tmp():
    """A temp dir whose socket paths fit AF_UNIX's ~104-byte cap. pytest's
    tmp_path doesn't on macOS (/private/var/folders/...), where connect()
    then fails with "AF_UNIX path too long" instead of the error under test.
    Claude Code keeps its own inboxes short for the same reason."""
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        yield Path(tmp)


@pytest.mark.skipif(sys.platform == "win32", reason="Unix domain sockets")
class TestUnixSocketDelivery:
    def test_posts_exact_bytes_to_a_unix_socket(self, short_tmp: Path):
        path = str(short_tmp / "in.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(path)
        server.listen(1)
        received: list[bytes] = []

        def serve():
            conn, _ = server.accept()
            chunks = []
            while chunk := conn.recv(4096):
                chunks.append(chunk)
            received.append(b"".join(chunks))
            conn.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        post_message(f"uds:{path}", "carry on", TOKEN)
        thread.join(timeout=5)
        server.close()
        assert received and received[0] == encode_message("carry on", TOKEN)

    def test_missing_socket_raises(self, short_tmp: Path):
        with pytest.raises(InboxError, match="no inbox"):
            post_message(str(short_tmp / "nope.sock"), "x", TOKEN)

    def test_overlong_path_is_an_inbox_error_not_a_crash(self, short_tmp: Path):
        # No real inbox can have such a path (Claude Code refuses to bind one),
        # but the caller must still get InboxError, never a raw OSError.
        with pytest.raises(InboxError, match="could not write"):
            post_message(str(short_tmp / ("x" * 200)), "x", TOKEN)


class TestPeerToken:
    def _key(self, sessions: Path, pid: int, **fields) -> Path:
        path = sessions / f"{pid}.{'a' * 64}.key"
        path.write_text(json.dumps(fields))
        return path

    def test_reads_the_peer_token_for_the_pid(self, tmp_path: Path):
        self._key(tmp_path, 4242, peerToken=TOKEN, procStartFt="1234")
        assert peer_token(tmp_path, 4242, "1234") == TOKEN

    def test_stale_key_from_a_reused_pid_is_ignored(self, tmp_path: Path):
        self._key(tmp_path, 4242, peerToken=TOKEN, procStartFt="1111")
        assert peer_token(tmp_path, 4242, "2222") is None

    def test_other_pids_and_bad_tokens_are_ignored(self, tmp_path: Path):
        self._key(tmp_path, 4243, peerToken=TOKEN)
        self._key(tmp_path, 4242, peerToken="not-hex")
        assert peer_token(tmp_path, 4242) is None

    def test_missing_dir_is_none(self, tmp_path: Path):
        assert peer_token(tmp_path / "absent", 1) is None
