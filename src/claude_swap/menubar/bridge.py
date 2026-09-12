"""WKWebView ↔ Python action routing for the menubar panel.

The bridge is the panel's entire server side: it accepts one JSON message
shape (``{id, action, payload}``), validates the action against the exact
handler table it was constructed with (an allowlist by construction), checks
payload types before any handler runs, executes handlers via an injectable
dispatcher (the shell passes a background-thread launcher; tests pass an
inline one), and correlates replies by id.

Inbound content is data, never code: nothing from the webview is evaluated,
and ``send_js`` only ever receives strings this module built itself from
``json.dumps``. A malformed message is dropped (no id to correlate), never
raised — the app loop must survive anything the panel sends.

Error surface: handler ``ClaudeSwitchError`` messages pass through verbatim
(they're user-facing by design); unexpected exceptions collapse to a generic
message so internals never leak into the panel.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable

from claude_swap.exceptions import ClaudeSwitchError

logger = logging.getLogger("claude-swap")

Handler = Callable[[dict], Any]
# payload spec: {"required": {"slot": str, ...}, "optional": {"email": str}}
PayloadSpec = dict


def _default_dispatch(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, daemon=True).start()


class Bridge:
    """Route panel messages to handlers and serialize replies/pushes as JS."""

    def __init__(
        self,
        handlers: dict[str, Handler],
        *,
        send_js: Callable[[str], None],
        payload_specs: dict[str, PayloadSpec] | None = None,
        dispatch: Callable[[Callable[[], None]], None] = _default_dispatch,
    ) -> None:
        self._handlers = handlers
        self._send_js = send_js
        self._payload_specs = payload_specs or {}
        self._dispatch = dispatch

    # ---- inbound -----------------------------------------------------------

    def handle_message(self, raw: str) -> None:
        """Parse and dispatch one webview message; never raises."""
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError, RecursionError):
            # RecursionError: a pathologically nested payload — the bridge
            # contract says any input is survivable, and this exception would
            # otherwise propagate into the AppKit script-message callback.
            logger.debug("menubar bridge: unparseable message dropped")
            return
        if not isinstance(msg, dict):
            return
        reply_id = msg.get("id")
        action = msg.get("action")
        payload = msg.get("payload", {})
        if not isinstance(reply_id, (str, int)):
            return  # nothing to correlate a reply with
        dispatch = self._dispatch

        def run() -> None:
            result = self._execute(action, payload)
            self._reply(reply_id, result)

        dispatch(run)

    def _execute(self, action: Any, payload: Any) -> dict:
        if not isinstance(action, str) or action not in self._handlers:
            return {"ok": False, "error": f"unknown action: {action!r}"}
        if not isinstance(payload, dict):
            return {"ok": False, "error": "payload must be an object"}
        spec = self._payload_specs.get(action)
        if spec is not None:
            error = self._validate_payload(payload, spec)
            if error:
                return {"ok": False, "error": error}
        try:
            data = self._handlers[action](payload)
        except ClaudeSwitchError as e:
            return {"ok": False, "error": str(e)}
        except Exception:
            logger.exception("menubar bridge: handler for %r failed", action)
            return {"ok": False, "error": f"{action} failed"}
        return {"ok": True, "data": data if data is not None else {}}

    @staticmethod
    def _validate_payload(payload: dict, spec: PayloadSpec) -> str | None:
        for key, type_ in spec.get("required", {}).items():
            if key not in payload:
                return f"missing required field: {key}"
            if not isinstance(payload[key], type_) or isinstance(payload[key], bool) and type_ is not bool:
                return f"field {key!r} has the wrong type"
        for key, type_ in spec.get("optional", {}).items():
            if key in payload and (
                not isinstance(payload[key], type_) or isinstance(payload[key], bool) and type_ is not bool
            ):
                return f"field {key!r} has the wrong type"
        return None

    # ---- outbound ----------------------------------------------------------

    def _reply(self, reply_id: str | int, result: dict) -> None:
        body = json.dumps(result, ensure_ascii=False)
        # The id came from the webview; it is interpolated into JS that
        # evaluateJavaScript will run, so it must be a JSON literal — raw
        # text would be an injection sink (and a ReferenceError for any
        # non-numeric id).
        id_literal = json.dumps(reply_id)
        self._send_js(f"cswap.reply({id_literal}, {body})")

    def push(self, type_: str, data: Any) -> None:
        """Push a vm/engine event into the panel (serialized as a JS call)."""
        body = json.dumps({"type": type_, "data": data}, ensure_ascii=False)
        self._send_js(f"cswap.push({body})")
