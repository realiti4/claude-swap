#!/usr/bin/env python3
"""T1 native proof: can the current transient NSPopover host the 968x560
companion, or must the expanded state use a borderless NSPanel?

The reset-timelines design needs one wide surface placed so the main
column sits under the status item. NSPopover positions itself (centered on
the anchor, clamped to the screen) and cannot be frame-positioned — so
this proof measures, on real AppKit with simulated anchors at left /
center / right of every attached screen:

Variant A (popover resize): show the 360x560 popover, resize to 968x560
in place, read back the placed window frame, and derive the arrow offset
within the surface (= anchor midX − frame.minX). Then compute which side
holds the 600px companion given the main column pinned at that offset.

Variant B (borderless panel): manually place a 968x560 panel with the
main column exactly under the anchor, clamped to the visible frame.

Evidence prints as a table and lands in tests/fixtures/captures/
native-proof.json; the variant decision is recorded in
next-wave/tasks/plan.md per the workflow. Repeated open/close cycles on
variant A assert the transient popover survives resize (dismisses
cleanly, no exceptions) — the handoff's named risk.

Usage: uv run python scripts/timeline_native_proof.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

MAIN_W, GAP, COMPANION_W = 360, 8, 600
TOTAL_W = MAIN_W + GAP + COMPANION_W  # 968
HEIGHT = 560

EVIDENCE = Path("tests/fixtures/captures/native-proof.json")


def spin(seconds: float) -> None:
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
    except ImportError:
        print("requires macOS + PyObjC (menubar extra)", file=sys.stderr)
        return 2

    from PyObjCTools import AppHelper

    app = AppKit.NSApplication.sharedApplication()
    # Mirror production app.py: no explicit activation policy (Regular).
    # An Accessory policy prevented the popover from showing when probed
    # here — the shipped shell never sets one.

    # A real status item anchors variant A exactly as production will.
    status_item = AppKit.NSStatusBar.systemStatusBar(
    ).statusItemWithLength_(AppKit.NSVariableStatusItemLength)
    button = status_item.button()
    button.setTitle_("⇄")

    results: dict = {"screens": [], "cycles": None, "notes": []}

    class Launcher(AppKit.NSObject):
        def appDidFinish_(self, _note):
            spin(1.0)  # let the menu bar lay the status item out first —
            # probing before this measured a 0-height button window and a
            # no-op show (the first proof run's "popover did not show").
            run_probes()

    launcher = Launcher.new()

    def anchor_window_at(x_center: float, y_top: float):
        """Borderless anchor window whose view stands in for a status item
        at an arbitrary screen position (real status items can't move)."""
        w, h = 80.0, 24.0
        win = AppKit.NSWindow.alloc(
        ).initWithContentRect_styleMask_backing_defer_(
            ((x_center - w / 2, y_top - h), (w, h)), 0,
            AppKit.NSBackingStoreBuffered, False,
        )
        win.setLevel_(AppKit.NSStatusWindowLevel)
        win.orderFront_(None)
        return win, win.contentView()

    def probe_popover(anchor_view, anchor_label: str) -> dict:
        """Variant A: show 360x560 relative to the anchor view, resize in
        place to 968x560, read back the placement and companion room."""
        vc = AppKit.NSViewController.alloc().init()
        view = AppKit.NSView.alloc().initWithFrame_(((0, 0), (360, 560)))
        vc.setView_(view)
        pop = AppKit.NSPopover.alloc().init()
        pop.setContentViewController_(vc)
        pop.setContentSize_((360.0, 560.0))
        pop.setBehavior_(AppKit.NSPopoverBehaviorTransient)
        pop.showRelativeToRect_ofView_preferredEdge_(
            anchor_view.bounds(), anchor_view, 3  # NSMaxYEdge
        )
        spin(0.3)
        record = {"anchor": anchor_label, "shown_initially": bool(pop.isShown())}
        if not record["shown_initially"]:
            return record
        pop.setContentSize_((float(TOTAL_W), float(HEIGHT)))
        spin(0.5)  # let the resize animate/settle

        win = view.window()
        frame = win.frame() if win is not None else None
        a_win = anchor_view.window()
        a_frame = a_win.frame() if a_win is not None else None
        record["shown_after_resize"] = bool(pop.isShown())
        if frame is not None and a_frame is not None:
            anchor_mid_x = (
                a_frame.origin.x + a_frame.size.width / 2
            )
            arrow_off = anchor_mid_x - frame.origin.x
            # content coords: the popover window adds symmetric chrome
            # (measured 26pt total) around the 968-wide content
            chrome = (frame.size.width - TOTAL_W) / 2
            content_arrow = arrow_off - chrome
            main_lo = max(0.0, min(content_arrow - MAIN_W / 2, TOTAL_W - MAIN_W))
            main_hi = main_lo + MAIN_W
            record.update(
                popover_frame=[frame.origin.x, frame.origin.y,
                               frame.size.width, frame.size.height],
                arrow_offset_in_surface=round(arrow_off, 2),
                content_arrow_offset=round(content_arrow, 2),
                main_column=[round(main_lo, 1), round(main_hi, 1)],
                companion_space=[round(main_lo - GAP, 1),
                                 round(TOTAL_W - main_hi - GAP, 1)],
                companion_fits_600=bool(
                    main_lo - GAP >= COMPANION_W
                    or TOTAL_W - main_hi - GAP >= COMPANION_W
                ),
            )
        pop.close()
        spin(0.8)  # transient close animates; needs runloop turns
        record["closed_cleanly"] = not pop.isShown()
        if pop.isShown():
            # do not let a stuck popover block later probes
            pop.setContentSize_((360.0, 560.0))
            try:
                win.orderOut_(None)
            except AttributeError:
                pass
        return record

    def probe_panel_at(screen_frame, visible, anchor_x: float, label: str) -> dict:
        """Variant B: manual placement, main column centered under the
        anchor, clamped to the visible frame."""
        x = anchor_x - TOTAL_W / 2
        x = max(visible.origin.x, min(x, visible.origin.x + visible.size.width - TOTAL_W))
        y = visible.origin.y + visible.size.height - HEIGHT - 8
        fits = (visible.size.width >= TOTAL_W)
        panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            ((x, y), (TOTAL_W, HEIGHT)), 0,  # borderless
            AppKit.NSBackingStoreBuffered, False,
        )
        panel.setBecomesKeyOnlyIfNeeded_(True)
        panel.orderFront_(None)
        spin(0.1)
        f = panel.frame()
        panel.orderOut_(None)
        return {
            "anchor": label,
            "panel_frame": [f.origin.x, f.origin.y, f.size.width, f.size.height],
            "fits_visible": fits,
            "main_center_offset": round(anchor_x - f.origin.x, 2),
        }

    def run_probes() -> None:
        try:
            for i, screen in enumerate(AppKit.NSScreen.screens()):
                frame = screen.frame()
                visible = screen.visibleFrame()
                info = {
                    "index": i,
                    "frame": [frame.origin.x, frame.origin.y,
                              frame.size.width, frame.size.height],
                    "visible": [visible.origin.x, visible.origin.y,
                                visible.size.width, visible.size.height],
                }
                results["screens"].append(info)

            # Variant A: the real status item first (production-true
            # anchor), then simulated anchors at left/center/right of the
            # main screen via movable anchor windows (status items can't
            # move).
            main = AppKit.NSScreen.mainScreen()
            visible = main.visibleFrame()
            results.setdefault("popover_resize", []).append(
                probe_popover(button, "status-item (real)")
            )
            for frac, label in ((0.15, "left-15%"), (0.5, "center-50%"),
                                (0.9, "right-90%")):
                ax = visible.origin.x + visible.size.width * frac
                aw, av = anchor_window_at(ax, visible.origin.y + visible.size.height)
                spin(0.1)
                results["popover_resize"].append(
                    probe_popover(av, f"anchor@{label}")
                )
                aw.orderOut_(None)

            # Variant B on every screen at left/center/right anchors.
            for i, screen in enumerate(AppKit.NSScreen.screens()):
                vis = screen.visibleFrame()
                for frac, label in ((0.15, "left"), (0.5, "center"), (0.9, "right")):
                    ax = vis.origin.x + vis.size.width * frac
                    results.setdefault("borderless_panel", []).append(
                        probe_panel_at(screen.frame(), vis, ax,
                                       f"screen{i}-{label}")
                    )

            # Lifecycle: repeated open/resize/close on the popover path
            # against the real status item.
            rounds = []
            for _ in range(5):
                rounds.append(probe_popover(button, "cycle"))
            results["cycles"] = {
                "rounds": 5,
                "all_shown_after_resize": all(
                    r.get("shown_after_resize") for r in rounds
                ),
                "all_closed_cleanly": all(
                    r.get("closed_cleanly") for r in rounds
                ),
            }
        except Exception as exc:
            results["notes"].append(f"probe error: {exc}")
        finally:
            AppHelper.callAfter(app.stop_, None)

    AppKit.NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
        launcher, "appDidFinish:",
        AppKit.NSApplicationDidFinishLaunchingNotification, None,
    )
    app.run()

    AppKit.NSStatusBar.systemStatusBar().removeStatusItem_(status_item)

    out = Path(EVIDENCE)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=1), encoding="utf-8")
    print(json.dumps(results, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
