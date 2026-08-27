"""Tests for the local bridge relay's platform-independent pieces.

The live relay path (a real LOCAL_BRIDGE Claude Code client + Anthropic's bridge)
is exercised manually; here we cover WebSocket framing, current-account
resolution from cswap state, and the LaunchAgent identity.
"""

from __future__ import annotations

import json
import socket
import threading

from claude_swap import chrome_bridge as cb
from claude_swap import chrome_session as cs


def test_frame_roundtrip_masked_and_unmasked():
    a, b = socket.socketpair()
    try:
        # client->server frames are masked; the reader must unmask them
        cb._write_frame(a, 1, json.dumps({"type": "connect"}), mask=True)
        op, data = cb._read_frame(b)
        assert op == 1
        assert json.loads(data) == {"type": "connect"}
        # server->client frames are unmasked
        cb._write_frame(a, 1, "hello", mask=False)
        op, data = cb._read_frame(b)
        assert op == 1 and data == b"hello"
    finally:
        a.close()
        b.close()


def test_frame_roundtrip_large_payload():
    a, b = socket.socketpair()
    try:
        payload = "x" * 70000  # forces the 8-byte extended length path
        # write from a thread — 70KB exceeds the socketpair buffer, so the
        # sender must be able to block while the reader drains concurrently.
        t = threading.Thread(target=cb._write_frame, args=(a, 1, payload), kwargs={"mask": True})
        t.start()
        op, data = cb._read_frame(b)
        t.join(5)
        assert op == 1 and data.decode() == payload
    finally:
        a.close()
        b.close()


def test_active_account_reads_sequence_and_vault(tmp_path):
    (tmp_path / "sequence.json").write_text(json.dumps({"activeAccountNumber": 2}))
    cs.ChromeVault(tmp_path).put("2", cs.ChromeSession(
        access_token="at", refresh_token="rt", account_uuid="uuid-2", email="two@x"))
    acct = cb._active_account(tmp_path)
    assert acct == {"num": "2", "token": "at", "uuid": "uuid-2", "email": "two@x"}


def test_active_account_none_when_missing(tmp_path):
    assert cb._active_account(tmp_path) is None  # no sequence.json
    (tmp_path / "sequence.json").write_text(json.dumps({"activeAccountNumber": 1}))
    assert cb._active_account(tmp_path) is None  # active account has no stored tokens


def test_is_running_false_on_closed_port():
    # pick a port nothing listens on
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert cb.is_running(port) is False


def test_launchd_label_stable():
    assert cb.LAUNCHD_LABEL == "com.claude-swap.bridge"
    assert cb._launchagent_path().name == "com.claude-swap.bridge.plist"
