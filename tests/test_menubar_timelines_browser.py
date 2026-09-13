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


_DELEGATE_SEQ = {"n": 0}


def _make_delegate_class():
    """One didFinishNavigation delegate class per Panel: PyObjC refuses
    re-registering a name, and a shared class with a static owner trips
    native teardown when two panels coexist."""
    import AppKit

    _DELEGATE_SEQ["n"] += 1

    def did_finish(self, _wv, _nav):
        owner = getattr(self, "owner", None)  # the loaded-flag dict
        if owner is not None:
            owner["ok"] = True

    # the selector must be in the type dict at creation — post-hoc
    # assignment never wires the ObjC method and crashes at dispatch
    return type(
        f"_PanelNavDelegate{_DELEGATE_SEQ['n']}", (AppKit.NSObject,),
        {"webView_didFinishNavigation_": did_finish},
    )


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
        delegate = _make_delegate_class().new()
        delegate.owner = self.loaded
        self.delegate = delegate

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
            raise RuntimeError(
                f"{box['e'].localizedDescription()}: {js[:100]}"
            )
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
    # the tlf fixture suppresses board-delta additions for the image
    # gate; these checks verify the additions, so enable them
    p.eval("window.CSWAP_TL_BOARD = false; 'ok'")
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
        before = panel.eval("JSON.stringify(CSWAP_TIMELINES.state.vm)")
        panel.eval(
            "document.querySelector('[data-tl-rows=\"5h\"] .tl-row[data-slot=\"2\"]').click(); 'ok'"
        )
        vm = json.loads(before)
        vm["accounts"] = [a for a in vm["accounts"] if a["slot"] != "2"]
        panel.eval(
            f"window.cswap.push({json.dumps({'type': 'vm', 'data': vm})}); 'ok'"
        )
        panel.spin(0.15)
        sel = [r["slot"] for r in panel.rows("5h") if r["selected"] == "true"]
        assert sel, "selection falls back rather than dangling"
        assert sel[0] != "2"
        # restore: later classes (detail content) target the full roster
        panel.eval(
            f"window.cswap.push({json.dumps({'type': 'vm', 'data': json.loads(before)})}); 'ok'"
        )
        panel.spin(0.15)


class TestEscapeAndFocus:
    def test_enter_opens_detail_with_exact_content(self, panel):
        panel.eval(
            "const r = document.querySelector('[data-tl-rows=\"5h\"] .tl-row[data-slot=\"2\"]');"
            "r.focus(); r.dispatchEvent(new KeyboardEvent('keydown', "
            "{key: 'Enter', bubbles: true})); 'ok'"
        )
        panel.spin(0.15)
        text = panel.eval(
            "(() => { const d = document.getElementById('tl-detail');"
            " return d ? d.textContent.replace(/\\s+/g, ' ') : 'MISSING'; })()"
        )
        assert "research-platform-eu" in text, "full alias, never ellipsized"
        assert "Session window · 5 hours" in text
        assert "32% used" in text and "64% used" in text
        assert "start inferred (reset − 5h)" in text
        assert "→" in text, "exact start → end span present"

    def test_escape_closes_detail_before_companion(self, panel):
        assert panel.eval("document.getElementById('tl-detail') !== null"), \
            "detail open from the previous test"
        panel.eval(
            "document.dispatchEvent(new KeyboardEvent('keydown', "
            "{key: 'Escape', bubbles: true})); 'ok'"
        )
        panel.spin(0.1)
        assert panel.eval("document.getElementById('tl-detail') === null"), \
            "stage 1 closes the detail only"
        assert panel.eval("document.getElementById('tl-companion') !== null"), \
            "companion survives stage 1"
        panel.eval(
            "document.dispatchEvent(new KeyboardEvent('keydown', "
            "{key: 'Escape', bubbles: true})); 'ok'"
        )
        panel.spin(0.1)
        assert panel.eval("document.getElementById('tl-companion') === null"), \
            "stage 2 collapses the companion"

    def test_detail_closes_when_its_account_vanishes(self):
        # fresh panel: the shared module panel's roster carries earlier
        # tests' mutations, and this flow needs a deterministic roster
        p = Panel()
        p.open_timelines()
        slots = [r["slot"] for r in p.rows("5h")]
        assert slots, "rows present before opening the detail"
        victim = slots[0]
        p.eval(
            f"const r = document.querySelector('[data-tl-rows=\"5h\"] "
            f".tl-row[data-slot=\"{victim}\"]');"
            "r.focus(); r.dispatchEvent(new KeyboardEvent('keydown', "
            "{key: 'Enter', bubbles: true})); 'ok'"
        )
        p.spin(0.15)
        assert p.eval("document.getElementById('tl-detail') !== null")
        vm = json.loads(p.eval("JSON.stringify(CSWAP_TIMELINES.state.vm)"))
        vm["accounts"] = [a for a in vm["accounts"] if a["slot"] != victim]
        outcome = p.eval(
            "(() => { try {"
            f"window.cswap.push({json.dumps({'type': 'vm', 'data': vm})});"
            " return 'pushed'; } catch (e) { return 'THREW: ' + e.message; } })()"
        )
        p.spin(0.15)
        assert outcome == "pushed", outcome
        assert p.eval("document.getElementById('tl-detail') === null"), \
            "detail closes safely when its account disappears"

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


class TestSessionAdditions:
    def test_countdown_chips_tick_per_chart(self, panel):
        panel.open_timelines()
        chips = json.loads(panel.eval(
            "JSON.stringify(Array.from(document.querySelectorAll('[data-tl-cd]'))"
            ".map(c => ({kind: c.dataset.kind, text: c.textContent})))"
        ))
        assert chips, "rows with future resets carry a chip"
        assert any(c["kind"] == "5h" for c in chips)
        assert any(c["kind"] == "7d" for c in chips)
        assert all(c["text"] for c in chips), "chips are populated immediately"

    def test_t_shortcut_toggles_and_ignores_fields(self, panel):
        was_open = panel.eval("CSWAP_TIMELINES.state.mode !== null")
        panel.eval(
            "document.dispatchEvent(new KeyboardEvent('keydown', "
            "{key: 't', bubbles: true})); 'ok'"
        )
        panel.spin(0.15)
        assert panel.eval("CSWAP_TIMELINES.state.mode !== null") != was_open, \
            "T toggles the timelines"
        panel.eval(
            "const inp = Object.assign(document.createElement('input'), {value: ''});"
            "document.body.appendChild(inp); inp.focus();"
            "inp.dispatchEvent(new KeyboardEvent('keydown', "
            "{key: 't', bubbles: true})); inp.remove(); 'ok'"
        )
        panel.spin(0.1)
        state_after_field = panel.eval("CSWAP_TIMELINES.state.mode !== null")
        panel.eval(
            "document.dispatchEvent(new KeyboardEvent('keydown', "
            "{key: 't', bubbles: true})); 'ok'"
        )
        panel.spin(0.1)
        assert panel.eval("CSWAP_TIMELINES.state.mode !== null") != state_after_field, \
            "T works again once focus leaves the field; the field typed nothing"

    def test_crosshair_present_for_fine_pointers(self, panel):
        panel.open_timelines()
        assert panel.eval(
            "document.querySelectorAll('.tl-crosshair').length"
        ) >= 2, "each chart mounts a crosshair guide"
