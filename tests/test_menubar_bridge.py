"""Tests for the menubar panel bridge (pure routing, fake transport).

The bridge is the only thing between the WKWebView panel and the switcher:
it validates every inbound message against an explicit action/payload
registry, runs the handler off the AppKit thread, and correlates replies by
id. These tests use an inline dispatcher and a recording transport, so no
PyObjC, webview, or switcher is involved.
"""

from __future__ import annotations

import json

import pytest

from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.menubar.bridge import Bridge


class FakeTransport:
    """Records evaluateJavaScript calls; a queue the tests can assert on."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, js: str) -> None:
        self.calls.append(js)


def make_bridge(transport: FakeTransport, **extra) -> Bridge:
    handlers = {
        "getSnapshot": lambda payload: {"schemaVersion": 1, "accounts": []},
        "switch": lambda payload: {"switchedTo": payload["slot"]},
        "boom": lambda payload: (_ for _ in ()).throw(
            ClaudeSwitchError("switch refused: live session")
        ),
        **extra,
    }
    return Bridge(handlers, send_js=transport, dispatch=lambda fn: fn())


def sent_jsons(transport: FakeTransport):
    out = []
    for call in transport.calls:
        if call.startswith("cswap.reply("):
            rest = call[len("cswap.reply(") : -1]
            _reply_id, body = rest.split(", ", 1)
            out.append(("cswap.reply(", json.loads(body)))
        else:
            body = call[len("cswap.push(") : -1]
            out.append(("cswap.push(", json.loads(body)))
    return out


class TestValidDispatch:
    def test_invokes_handler_and_replies_with_data(self) -> None:
        t = FakeTransport()
        bridge = make_bridge(t)
        bridge.handle_message(json.dumps({"id": "7", "action": "getSnapshot", "payload": {}}))
        [(head, body)] = sent_jsons(t)
        assert head == "cswap.reply("
        assert body["ok"] is True
        assert body["data"]["schemaVersion"] == 1

    def test_reply_id_matches_request(self) -> None:
        t = FakeTransport()
        make_bridge(t).handle_message(
            json.dumps({"id": "42", "action": "switch", "payload": {"slot": "2"}})
        )
        assert t.calls == [
            'cswap.reply(42, {"ok": true, "data": {"switchedTo": "2"}})'
        ]

    def test_payload_forwarded(self) -> None:
        t = FakeTransport()
        seen = {}
        handlers = {"setAutoSwitch": lambda p: seen.update(p) or {"done": True}}
        Bridge(handlers, send_js=t).handle_message(
            json.dumps({"id": "1", "action": "setAutoSwitch", "payload": {"enabled": True}})
        )
        assert seen == {"enabled": True}


class TestValidation:
    def test_unknown_action_is_rejected_not_raised(self) -> None:
        t = FakeTransport()
        make_bridge(t).handle_message(
            json.dumps({"id": "9", "action": "rm -rf", "payload": {}})
        )
        [(head, body)] = sent_jsons(t)
        assert head == "cswap.reply("
        assert body["ok"] is False
        assert "unknown action" in body["error"]

    def test_missing_required_slot_rejected(self) -> None:
        t = FakeTransport()
        bridge = Bridge(
            {"switch": lambda p: {}}, send_js=t, dispatch=lambda fn: fn(),
            payload_specs={"switch": {"required": {"slot": str}}},
        )
        bridge.handle_message(json.dumps({"id": "2", "action": "switch", "payload": {}}))
        [(head, body)] = sent_jsons(t)
        assert body["ok"] is False
        assert "slot" in body["error"]

    def test_wrong_payload_type_rejected(self) -> None:
        t = FakeTransport()
        bridge = Bridge(
            {"switch": lambda p: {}}, send_js=t, dispatch=lambda fn: fn(),
            payload_specs={"switch": {"required": {"slot": str}}},
        )
        bridge.handle_message(
            json.dumps({"id": "3", "action": "switch", "payload": {"slot": 2}})
        )
        [(head, body)] = sent_jsons(t)
        assert body["ok"] is False

    def test_non_dict_payload_rejected(self) -> None:
        t = FakeTransport()
        make_bridge(t).handle_message(
            json.dumps({"id": "4", "action": "getSnapshot", "payload": ["nope"]})
        )
        [(head, body)] = sent_jsons(t)
        assert body["ok"] is False

    def test_malformed_json_never_raises(self) -> None:
        t = FakeTransport()
        bridge = make_bridge(t)
        bridge.handle_message("this is not json{")
        bridge.handle_message("123")
        assert t.calls == []  # no id to correlate — dropped, app keeps running


class TestErrors:
    def test_handler_claude_switch_error_surfaces_message(self) -> None:
        t = FakeTransport()
        make_bridge(t).handle_message(json.dumps({"id": "5", "action": "boom", "payload": {}}))
        [(head, body)] = sent_jsons(t)
        assert body["ok"] is False
        assert "switch refused" in body["error"]

    def test_handler_unexpected_error_is_generic(self) -> None:
        t = FakeTransport()

        def explode(payload):
            raise RuntimeError("secret internals")

        Bridge({"x": explode}, send_js=t, dispatch=lambda fn: fn()).handle_message(
            json.dumps({"id": "6", "action": "x", "payload": {}})
        )
        [(head, body)] = sent_jsons(t)
        assert body["ok"] is False
        assert "secret internals" not in body["error"]
        assert body["error"]  # something human-readable


class TestPush:
    def test_push_serializes_type_and_data(self) -> None:
        t = FakeTransport()
        make_bridge(t).push("engine", {"event": "switched", "text": "2 → 1"})
        [(head, body)] = sent_jsons(t)
        assert head == "cswap.push("
        assert body == {"type": "engine", "data": {"event": "switched", "text": "2 → 1"}}


class TestDispatch:
    def test_dispatch_controls_threading(self) -> None:
        t = FakeTransport()
        ran_on = []
        bridge = Bridge(
            {"getSnapshot": lambda p: {"thread": True}},
            send_js=t,
            dispatch=lambda fn: (ran_on.append(1), fn())[1],
        )
        bridge.handle_message(json.dumps({"id": "1", "action": "getSnapshot", "payload": {}}))
        assert ran_on == [1]  # the injected dispatcher ran the handler

    def test_json_roundtrip_of_floats_and_unicode(self) -> None:
        t = FakeTransport()
        make_bridge(t).push("vm", {"pct": 68.4, "label": "hüngetrève→✓"})
        [(_, body)] = sent_jsons(t)
        assert body["data"]["pct"] == 68.4
        assert body["data"]["label"] == "hüngetrève→✓"
