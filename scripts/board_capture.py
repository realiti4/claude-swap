#!/usr/bin/env python3
"""Board-capture harness for the menubar panel's pixel-fidelity gate.

Renders fixture states of the panel in a real WKWebView (the production
rasterizer) at exact board dimensions, forces the bundled fonts and the
requested theme, then writes for each state:

  <out>/<state>.png          1x board-space capture (retina captures are
                             resampled to 1x via CoreGraphics, keeping the
                             production raster)
  <out>/<state>.layout.json  exact-point boxes (getBoundingClientRect) for
                             the fidelity selectors — the zero-tolerance
                             geometry record

macOS only. The pure-image half of the gate lives in
tests/test_menubar_board_diff.py; this script never fails a build, it
produces evidence.

Usage:
  uv run python scripts/board_capture.py --states main-dark,main-light
  uv run python scripts/board_capture.py --states 17-right-dark
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parents[1] / "src" / "claude_swap" / "menubar" / "web"

# state -> config. Timeline states (16/17/18/20 equivalents) register here
# as their UI lands; their selectors pin the extracted geometry: track,
# bars, now line, ticks, rows.
STATES: dict[str, dict] = {
    # Default fixture roster (alex/work…) — the one boards 01/02 were drawn
    # from — keeps the informational delta dominated by font shift, not
    # different sample identities.
    "main-dark": {
        "width": 360, "height": 560, "query": "", "theme": "dark",
        "selectors": ["#panel", "header", "footer"],
    },
    "main-light": {
        "width": 360, "height": 560, "query": "", "theme": "light",
        "selectors": ["#panel", "header", "footer"],
    },
    # Expanded surface (fixture mode expands locally): main column at
    # anchor 0 + companion right — the board-17/18 composition at 968x560.
    "tl-open-dark": {
        "width": 968, "height": 560, "query": "", "theme": "dark",
        "click": ".tl-trigger",
        "selectors": ["#panel", "#tl-companion", ".tl-head", ".tl-body",
                       ".tl-card"],
    },
    "tl-open-light": {
        "width": 968, "height": 560, "query": "", "theme": "light",
        "click": ".tl-trigger",
        "selectors": ["#panel", "#tl-companion", ".tl-head", ".tl-body",
                       ".tl-card"],
    },
    # Gated fidelity states: the handoff's frozen timeline fixture drives
    # both panels; boards 17/18 equivalents at 968x560.
    "17-right-dark": {
        "width": 968, "height": 560, "query": "tlf=1", "theme": "dark",
        "click": ".tl-trigger",
        "selectors": ["#panel", "#tl-companion", ".tl-card"],
    },
    "18-right-light": {
        "width": 968, "height": 560, "query": "tlf=1", "theme": "light",
        "click": ".tl-trigger",
        "selectors": ["#panel", "#tl-companion", ".tl-card"],
    },
}

LAYOUT_JS = """
(() => {
  const sels = %s;
  const boxes = [];
  for (const sel of sels) {
    for (const el of document.querySelectorAll(sel)) {
      const r = el.getBoundingClientRect();
      boxes.push({sel, id: el.id || null, cls: String(el.className || ""),
                  x: r.x, y: r.y, w: r.width, h: r.height});
    }
  }
  const texts = [];
  for (const el of document.querySelectorAll(".tl-text")) {
    const r = el.getBoundingClientRect();
    if (r.width > 0) texts.push([r.x, r.y, r.width, r.height]);
  }
  const fids = [];
  for (const el of document.querySelectorAll("[data-fid]")) {
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    fids.push({fid: el.dataset.fid, x: r.x, y: r.y, w: r.width, h: r.height,
               bg: cs.backgroundColor, fg: cs.color, radius: cs.borderRadius,
               fs: cs.fontSize, fw: cs.fontWeight});
  }
  return JSON.stringify({viewport: {w: innerWidth, h: innerHeight},
                         boxes, fids, texts});
})()
"""


def spin_runloop(seconds: float) -> None:
    """Pump the main run loop so async AppKit/WebKit callbacks can fire."""
    import AppKit

    rl = AppKit.NSRunLoop.currentRunLoop()
    deadline = time.time() + seconds
    while time.time() < deadline:
        rl.runMode_beforeDate_(
            AppKit.NSDefaultRunLoopMode,
            AppKit.NSDate.dateWithTimeIntervalSinceNow_(0.05),
        )


def main() -> int:
    try:
        import AppKit
        import WebKit
    except ImportError:
        print("board_capture requires macOS with PyObjC (menubar extra)", file=sys.stderr)
        return 2

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--states", default="main-dark,main-light",
                        help="comma-separated state names (see STATES)")
    parser.add_argument("--out", default="tests/fixtures/captures")
    parser.add_argument("--settle", type=float, default=0.6,
                        help="seconds to wait after fonts ready before capture")
    args = parser.parse_args()

    names = [s.strip() for s in args.states.split(",") if s.strip()]
    unknown = [n for n in names if n not in STATES]
    if unknown:
        parser.error(f"unknown states {unknown}; known: {sorted(STATES)}")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    from PyObjCTools import AppHelper

    # PyObjC turns function-valued attributes on NSObject subclasses into
    # method registrations; the callback lives in a plain dict instead.
    _nav_callback: dict = {"handler": None}

    class NavDelegate(AppKit.NSObject):
        def webView_didFinishNavigation_(self, _wv, _nav):
            handler = _nav_callback["handler"]
            if handler is not None:
                _nav_callback["handler"] = None
                AppHelper.callAfter(handler)

    def eval_sync(view, js: str, timeout: float = 5.0):
        """evaluateJavaScript with a bounded runloop spin (main thread only)."""
        box: dict = {}

        def done(value, error):
            box["v"], box["e"] = value, error

        view.evaluateJavaScript_completionHandler_(js, done)
        deadline = time.time() + timeout
        while "v" not in box and "e" not in box and time.time() < deadline:
            spin_runloop(0.05)
        if "v" not in box and "e" not in box:
            return None
        if box.get("e") is not None:
            raise RuntimeError(f"eval failed: {box['e']}")
        return box.get("v")

    def capture_png(view, path: Path, w: int, h: int) -> None:
        # macOS 26 removed the NSView snapshot methods (cacheDisplay,
        # drawViewHierarchy) — capture our own onscreen window through the
        # window server instead (no TCC prompt for owned windows), then
        # resample to exact 1x board-space via CoreGraphics so retina and
        # 1x displays produce comparable PNGs from the production raster.
        import Quartz

        win_id = view.window().windowNumber()
        cg = Quartz.CGWindowListCreateImage(
            Quartz.CGRectNull,
            Quartz.kCGWindowListOptionIncludingWindow,
            win_id,
            Quartz.kCGWindowImageBoundsIgnoreFraming
            | Quartz.kCGWindowImageNominalResolution,  # 1x board-space
        )
        if cg is None:
            raise RuntimeError("window-server capture returned nothing")
        image = AppKit.NSImage.alloc().initWithCGImage_size_(cg, (float(w), float(h)))
        final = AppKit.NSBitmapImageRep.alloc(
        ).initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
            None, w, h, 8, 4, True, False,
            AppKit.NSDeviceRGBColorSpace, 0, 0,
        )
        final.setSize_((float(w), float(h)))
        AppKit.NSGraphicsContext.saveGraphicsState()
        AppKit.NSGraphicsContext.setCurrentContext_(
            AppKit.NSGraphicsContext.graphicsContextWithBitmapImageRep_(final)
        )
        image.drawInRect_fromRect_operation_fraction_(
            ((0.0, 0.0), (float(w), float(h))),
            ((0.0, 0.0), (image.size().width, image.size().height)),
            AppKit.NSCompositeCopy,
            1.0,
        )
        AppKit.NSGraphicsContext.restoreGraphicsState()
        png = final.representationUsingType_properties_(
            AppKit.NSBitmapImageFileTypePNG, None
        )
        png.writeToFile_atomically_(str(path), True)

    results: list[str] = []
    delegate = NavDelegate.new()

    def run_one(state: str) -> None:
        cfg = STATES[state]
        w, h = float(cfg["width"]), float(cfg["height"])
        window = AppKit.NSWindow.alloc(
        ).initWithContentRect_styleMask_backing_defer_(
            ((100.0, 100.0), (w, h)), 0, AppKit.NSBackingStoreBuffered, False
        )
        config = WebKit.WKWebViewConfiguration.new()
        view = WebKit.WKWebView.alloc().initWithFrame_configuration_(
            ((0.0, 0.0), (w, h)), config
        )
        window.setContentView_(view)
        window.makeKeyAndOrderFront_(None)

        url = "file://" + str(WEB_DIR / "index.html")
        if cfg["query"]:
            url += "?" + cfg["query"]
        loaded = {"ok": False}

        def page_loaded() -> None:
            loaded["ok"] = True

        _nav_callback["handler"] = page_loaded
        view.setNavigationDelegate_(delegate)
        request = AppKit.NSURLRequest.requestWithURL_(
            AppKit.NSURL.URLWithString_(url)
        )
        view.loadRequest_(request)
        deadline = time.time() + 15.0
        while not loaded["ok"] and time.time() < deadline:
            spin_runloop(0.05)
        if not loaded["ok"]:
            raise RuntimeError(f"{state}: page did not finish loading")

        # Fixture mode self-renders without a bridge. Force the theme, then
        # wait for the bundled @font-face fonts before capturing.
        eval_sync(view, f"document.documentElement.dataset.theme = {cfg['theme']!r}; 'ok'")
        for _ in range(100):  # <= 5s for document.fonts to settle
            status = eval_sync(view, "document.fonts ? document.fonts.status : 'loaded'")
            if status == "loaded":
                break
            spin_runloop(0.05)
        time.sleep(args.settle)
        # Two rAF ticks prove the page composited at least one frame: the
        # window-server capture below races WKWebView's first paint
        # otherwise (blank 9KB PNGs with a fully-built DOM). evaluate-
        # JavaScript cannot await Promises, so drive the ticks through a
        # window flag polled from here.
        eval_sync(
            view,
            "window.__painted = false; "
            "requestAnimationFrame(() => requestAnimationFrame("
            "  () => { window.__painted = true; })); 'ok'",
        )
        self_spin = spin_runloop
        for _ in range(60):
            if eval_sync(view, "window.__painted") is True:
                break
            self_spin(0.05)
        time.sleep(0.05)
        if cfg.get("click"):
            eval_sync(
                view,
                f"document.querySelector({cfg['click']!r}).click(); 'clicked'",
            )
            spin_runloop(0.3)

        capture_png(view, out_dir / f"{state}.png", cfg["width"], cfg["height"])
        layout = eval_sync(view, LAYOUT_JS % json.dumps(cfg["selectors"]))
        (out_dir / f"{state}.layout.json").write_text(layout or "{}", encoding="utf-8")
        results.append(
            f"{state}: {cfg['width']}x{cfg['height']} captured "
            f"({(out_dir / f'{state}.png').stat().st_size} bytes png, "
            f"{len(layout or '')}B layout)"
        )
        window.orderOut_(None)

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyProhibited)

    def run_all() -> None:
        try:
            for state in names:
                run_one(state)
        except Exception as exc:  # report and exit non-zero, keep evidence
            print(f"capture failed: {exc}", file=sys.stderr)
        finally:
            AppHelper.callAfter(app.stop_, None)

    AppHelper.callAfter(run_all)
    app.run()

    for line in results:
        print(line)
    return 0 if len(results) == len(names) else 1


if __name__ == "__main__":
    raise SystemExit(main())
