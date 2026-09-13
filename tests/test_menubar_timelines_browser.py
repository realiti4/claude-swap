"""Browser-layer checks for the reset-timelines charts (macOS, WKWebView).

The panel's fixture mode (`?tlf=1`) gives the production renderer a frozen
six-account roster; these tests drive it like a user — open, select, push
mutated vms, Escape — and assert the honest-data and state-preservation
rules the QA matrix requires: selection never activates, alias/active/
removal pushes refresh rows in place without scroll loss, elapsed and
missing states render text instead of bars, and the two-stage Escape
collapses back to the trigger.

Skipped off macOS or without PyObjC: the pure halves of every rule are
already pinned by the node geometry suite and the view-model contract
tests.
"""

from __future__ import annotations

import json
import time

import pytest

pytest.importorskip("AppKit")
pytest.importorskip("WebKit")

from tests.test_menubar_timelines import NOW  # noqa: E402

WEB_DIR = None


def _web_dir():
    global WEB_DIR
    if WEB_DIR is None:
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        WEB_DIR = root / "src" / "claude_swap" / "menubar" / "web"
    return WEB_DIR


class Panel:
    """One WKWebView on the fixture page; eval via a runloop spin."""

    def __init__(self, query: str = "tlf=1"):
        import AppKit
        import WebKit
        from PyObjCTools import AppHelper

        self._AppKit = AppKit
        self._AppHelper = AppHelper
        self.app = AppKit.NSApplication.sharedApplication()
        self.loaded = {"ok": False}

        class Nav(AppKit.NSObject):
            pass

        nav = Nav.new()
        panel = self

        # PyObjC turns function attrs on ObjC subclasses into methods; a
        # closure over self via class-level dict is the safe carrier.
        Nav.handler = None
        import objc  # noqa: F401

        win = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            ((60.0, 60.0), (968.0, 560.0)), 0,
            AppKit.NSBackingStoreBuffered, False,
        )
        view = WebKit.WKWebView.alloc().initWithFrame_configuration_(
            ((0.0, 0.0), (968.0, 560.0)), WebKit.WKWebViewConfiguration.new()
        )
        win.setContentView_(view)
        win.orderFront_(None)
        self.view = view

        class Delegate(AppKit.NSObject):
            def webView_didFinishNavigation_(self, _wv, _nav):
                panel.loaded["ok"] = True

        self.delegate = Delegate.new()
        view.setNavigationDelegate_(self.delegate)
        url = "file://" + str(_web_dir() / "index.html")
        if query:
            url += "?" + query
        view.loadRequest_(AppKit.NSURLRequest.requestWithURL_(
            AppKit.NSURL.URLWithString_(url)
        ))
        self._run_until(lambda: self.loaded["ok"], timeout=15.0)
        # settle fonts/first paint
        for _ in range(20):
            if self.eval("document.fonts ? document.fonts.status : 'loaded'") == "loaded":
                break
            self.spin(0.05)
        self.spin(0.2)

    # ---- plumbing ---------------------------------------------------------

    def spin(self, seconds: float) -> None:
        AppKit = self._AppKit
        rl = AppKit.NSRunLoop.currentRunLoop()
        deadline = time.time() + seconds
        while time.time() < deadline:
            rl.runMode_beforeDate_(
                AppKit.NSDefaultRunLoopMode,
                AppKit.NSDate.dateWithTimeIntervalSinceNow_(0.05),
            )

    def _run_until(self, predicate, timeout: float) -> None:
        deadline = time.time() + timeout
        while not predicate() and time.time() < deadline:
            self.spin(0.05)
        assert predicate(), "timed out waiting for the panel"

    def eval(self, js: str, timeout: float = 5.0):
        box: dict = {}

        def done(value, error):
            box["v"], box["e"] = value, error

        self.view.evaluateJavaScript_completionHandler_(js, done)
        self._run_until(lambda: "v" in box or "e" in box, timeout)
        if box.get("e") is not None:
            raise RuntimeError(str(box["e"].localizedDescription()))
        return box.get("v")

    # ---- panel actions ----------------------------------------------------

    def open_timelines(self):
        # idempotent: re-opening must not toggle the panel closed
        if self.eval("CSWAP_TIMELINES.state.mode !== null"):
            return
        self.eval("document.querySelector('.tl-trigger').click(); 'ok'")
        self.spin(0.2)

    def rows(self, kind: str) -> list[dict]:
        data = self.eval(
            f"JSON.stringify(Array.from(document.querySelectorAll("
            f"'[data-tl-rows=\"{kind}\"] .tl-row')).map(r => ({{"
            f"slot: r.dataset.slot, selected: r.getAttribute('aria-selected'),"
            f"hasBar: !!r.querySelector('.bar-window'),"
            f"text: r.textContent.replace(/\\s+/g, ' ').trim().slice(0, 80)"
            f"}})))"
        )
        return json.loads(data)

    def send_actions(self) -> list[str]:
        """Every bridge action the page attempted (fixture mode records)."""
        return json.loads(self.eval(
            "JSON.stringify((window.__tlActions = window.__tlActions || [])."
            "map(a => a.action))"
        ))


@pytest.fixture(scope="module")
def panel():
    p = Panel()
    # record every attempted action: selection must never send "switch"
    p.eval("""
      (() => {
        window.__tlActions = [];
        const orig = window.cswap.send;
        window.cswap.send = (action, payload) => {
          window.__tlActions.push({action});
          return orig(action, payload);
        };
        return 'ok';
      })()
    """)
    p.open_timelines()  # every class assumes the charts are up
    yield p


class TestOpenAndSelect:
    def test_open_renders_every_account_in_both_charts(self, panel):
        panel.open_timelines()
        for kind in ("5h", "7d"):
            rows = panel.rows(kind)
            assert [r["slot"] for r in rows] == ["1", "2", "3", "4", "5", "6"]
        assert panel.eval(
            "document.getElementById('tl-companion') !== null"
        )

    def test_selection_is_shared_and_never_activates(self, panel):
        panel.eval(
            "document.querySelector('[data-tl-rows=\"5h\"] .tl-row[data-slot=\"2\"]').click(); 'ok'"
        )
        panel.spin(0.1)
        for kind in ("5h", "7d"):
            sel = [r["slot"] for r in panel.rows(kind) if r["selected"] == "true"]
            assert sel == ["2"], f"{kind}: selection must be inspect-only and shared"
        # no credential-bearing action was attempted from the charts
        actions = panel.send_actions()
        assert "switch" not in actions and "setAutoSwitch" not in actions

    def test_reselect_moves_without_deselecting_everything(self, panel):
        panel.eval(
            "document.querySelector('[data-tl-rows=\"7d\"] .tl-row[data-slot=\"3\"]').click(); 'ok'"
        )
        panel.spin(0.1)
        sel = [r["slot"] for r in panel.rows("5h") if r["selected"] == "true"]
        assert sel == ["3"]


class TestHonestStates:
    def test_state_matrix_rows_render_text_not_bars(self, panel):
        rows = {r["slot"]: r for r in panel.rows("5h")}
        # ci-runner: no-window -> text, never a bar
        assert rows["5"]["hasBar"] is False
        assert "No subscription quota" in rows["5"]["text"]
        # archive 5h: stale last-known -> bar + last-known label
        assert rows["6"]["hasBar"] is True
        assert "last known" in rows["6"]["text"]
        w7 = {r["slot"]: r for r in panel.rows("7d")}
        # archive 7d: elapsed -> awaiting text, no bar
        assert w7["6"]["hasBar"] is False
        assert "Awaiting updated usage" in w7["6"]["text"]

    def test_edge_states_push(self, panel):
        edge = {
            "slot": "7", "label": "staging", "alias": "staging", "active": False,
            "timelineWindows": [
                {"kind": "5h", "pct": 44, "resetsAt": None, "startsAt": None,
                 "state": "reset-unavailable", "observedAt": NOW - 3600},
                {"kind": "7d", "pct": None, "resetsAt": NOW + 86400,
                 "startsAt": None, "state": "usage-unavailable",
                 "observedAt": NOW - 3600},
            ],
        }
        vm = json.loads(panel.eval("JSON.stringify(CSWAP_TIMELINES.state.vm)"))
        vm["accounts"] = vm["accounts"][:4] + [edge]
        panel.eval(
            f"window.cswap.push({json.dumps({'type': 'vm', 'data': vm})}); 'ok'"
        )
        panel.spin(0.15)
        rows = {r["slot"]: r for r in panel.rows("5h")}
        assert rows["7"]["hasBar"] is False, "missing reset never positions a bar"
        assert "Reset time unavailable" in rows["7"]["text"]
        w7 = {r["slot"]: r for r in panel.rows("7d")}
        assert w7["7"]["hasBar"] is True, "known window renders, neutral"
        assert "no usage data" in w7["7"]["text"], "null pct is never zero"


class TestPushPreservation:
    def test_alias_active_and_removal_update_in_place(self, panel):
        vm = json.loads(panel.eval("JSON.stringify(CSWAP_TIMELINES.state.vm)"))
        # grow the roster past the 142px viewport so scrolling is real
        base = [a for a in vm["accounts"] if a["slot"] in ("1", "2", "3", "5", "6")]
        extra = []
        for i in range(5):
            clone = json.loads(json.dumps(base[0]))
            clone.update({"slot": f"1{i}", "alias": f"clone{i}", "active": False,
                          "label": f"clone{i}"})
            extra.append(clone)
        vm["accounts"] = base + extra
        panel.eval(
            f"window.cswap.push({json.dumps({'type': 'vm', 'data': vm})}); 'ok'"
        )
        panel.spin(0.15)
        # scroll the session chart, then push a mutated vm
        panel.eval(
            "const el = document.querySelector('[data-tl-rows=\"5h\"]');"
            "el.scrollTop = 8; 'ok'"
        )
        vm2 = json.loads(panel.eval("JSON.stringify(CSWAP_TIMELINES.state.vm)"))
        for acc in vm2["accounts"]:
            if acc["slot"] == "1":
                acc["alias"] = "work-renamed"
        vm2["accounts"] = [a for a in vm2["accounts"] if a["slot"] != "10"]
        vm2["activeSlot"] = "2"
        for acc in vm2["accounts"]:
            acc["active"] = acc["slot"] == "2"
        panel.eval(
            f"window.cswap.push({json.dumps({'type': 'vm', 'data': vm2})}); 'ok'"
        )
        panel.spin(0.15)
        rows = panel.rows("5h")
        slots = [r["slot"] for r in rows]
        assert "10" not in slots and "1" in slots
        assert any("work-renamed" in r["text"] for r in rows)
        # scroll preserved across the push (roster still overflows)
        assert panel.eval(
            "document.querySelector('[data-tl-rows=\"5h\"]').scrollTop"
        ) == 8
        # active moved to slot 2 without any switch action
        assert "switch" not in panel.send_actions()
        sel = [r["slot"] for r in rows if r["selected"] == "true"]
        assert sel and sel[0] != "10", "selection survives; removed row falls back"

    def test_selected_row_removal_falls_back_cleanly(self, panel):
        panel.eval(
            "document.querySelector('[data-tl-rows=\"5h\"] .tl-row[data-slot=\"2\"]').click(); 'ok'"
        )
        vm = json.loads(panel.eval("JSON.stringify(CSWAP_TIMELINES.state.vm)"))
        vm["accounts"] = [a for a in vm["accounts"] if a["slot"] != "2"]
        panel.eval(
            f"window.cswap.push({json.dumps({'type': 'vm', 'data': vm})}); 'ok'"
        )
        panel.spin(0.15)
        sel = [r["slot"] for r in panel.rows("5h") if r["selected"] == "true"]
        assert sel, "selection falls back rather than dangling"
        assert sel[0] != "2"


class TestEscapeAndFocus:
    def test_escape_collapses_and_returns_focus_to_trigger(self, panel):
        panel.open_timelines()
        panel.eval(
            "document.querySelector('.tl-trigger').focus();"
            "document.dispatchEvent(new KeyboardEvent('keydown', "
            "{key: 'Escape', bubbles: true})); 'ok'"
        )
        panel.spin(0.15)
        assert panel.eval("document.getElementById('tl-companion') === null")
        assert panel.eval(
            "document.activeElement === document.querySelector('.tl-trigger')"
        )

    def test_close_button_collapses(self, panel):
        panel.open_timelines()
        panel.eval("document.querySelector('.tl-close-btn').click(); 'ok'")
        panel.spin(0.15)
        assert panel.eval("document.getElementById('tl-companion') === null")
