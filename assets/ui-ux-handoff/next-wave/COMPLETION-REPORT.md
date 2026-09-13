# Reset timelines — completion report

Wave complete on `dev`, 2026-09-12. Commits `799bfa5..b3735bd`
(16 commits). No release, no version change (0.29.0 unchanged), no
publishing — per scope.

## Delivered

A calendar button in the main header opens an adjacent companion panel
with two full-window Gantt charts (Session ±5h, Weekly ±7d), one row per
account, quota fills, shared Now marker, honest data states, and the
session-approved additions — rendered against the design boards under a
calibrated pixel-fidelity gate.

- **Native surface** (T1 decision recorded in `tasks/plan.md`): the
  expanded state is a borderless non-activating NSPanel — a resized
  popover always centers on the anchor and starves the 600px companion
  (measured, all anchors) — with the same WKWebView re-parented (JS
  state survives), right/left/in-panel placement from AppKit-only math,
  global+local outside-click monitors, screen-change re-placement, and
  clean collapse back to the untouched transient popover.
- **Data**: additive `timelineWindows` (explicit nullables; elapsed
  resets retained; per-account staleness; scoped/spend excluded);
  `windows[]` and quota cards byte-identical.
- **Charts**: pure geometry layer (node-tested against the frozen
  fixture fractions incl. DST and all edge cases) under DOM rendering
  with board-exact tokens extracted from the Pen source; six-row board
  roster incl. no-window/stale/elapsed demonstrations; selection is
  inspect-only and shared across charts; detail popover with exact
  spans and inferred-start marking; two-stage Escape; live rows on the
  30s tick; 140ms slide (suspension-safe), reduced-motion instant;
  skeleton first read; disabled-when-empty trigger.
- **Additions** (board deltas pending owner re-export): pointer
  crosshair with timestamp, per-chart countdown chips, `T` shortcut.
- **Fonts**: Inter + IBM Plex Mono OFL subsets bundled, `@font-face`
  shadows host copies, CSP `font-src 'self'` — every machine renders
  the boards' typography.

## Verification evidence (exact commands and results)

```sh
uv run pytest -n 4 -q                                    # 2410 passed, 10 skipped
uv run python -m pytest tests/test_menubar_timelines_browser.py -n0 -q   # 20 passed (macOS)
node --test tests/js/timelines.test.js                   # 8 pass, 0 fail
uv run --with pillow python -m pytest tests/test_menubar_board_diff.py -n0 -q  # 9 passed
uv build --wheel                                         # dist/claude_swap-0.29.0-py3-none-any.whl
# wheel contains web/timelines.js + all three fonts (verified by test + inspection)
uv run python scripts/timeline_native_proof.py           # evidence: tests/fixtures/captures/native-proof.json
uv run --with pyobjc-framework-Cocoa --with pyobjc-framework-WebKit \
  --with pyobjc-framework-Quartz python scripts/board_capture.py \
  --states 17-right-dark,18-right-light                  # captures + exact-point layout records
```

Pillow is intentionally not a locked dependency (the in-flight uv.lock
belongs to other work): the image gate runs via
`uv run --with pillow python -m pytest` and skips cleanly otherwise.

## Fidelity gate design (the no-exceptions mandate, as implemented)

- **Zero tolerance**: every geometry box, color and type style asserted
  from the harness's exact-point layout record (rgb-exact vs the Pen
  hexes, both themes; `TestScaffoldGeometry`).
- **Glyph rasterization masked**: WebKit and Pen rasterize text
  differently — the one physically renderer-dependent surface; text is
  pinned by its exact boxes instead.
- **Image budget at the measured floor**: WebKit-vs-Pen sub-pixel
  accumulation measured at 3.3%/5.2% (dark/light); budgets 4%/6% — any
  single real regression (shift, color change, missing element) moves
  ≥1% on its own. Attribution recorded in the test and Task 9's commit.

## Owner reconciliation queue (design-side re-exports)

1. Boards 01–15: add the timelines trigger button to the headers and
   re-export under bundled-font rendering (the main panel's text shifts
   toward the boards on machines without Inter installed).
2. Boards 17/18: fold in the countdown chips (and optionally note the
   crosshair/`T` additions) once approved as drawn; until then the
   board-exact fixture suppresses them (`CSWAP_TL_BOARD`).
3. Board 16: the closed state now includes the trigger — re-export.

## Unverified — manual pass checklist (single display here)

- [ ] Real-app feel: open/close via the item on a secondary display
      with nonzero origin; display unplug/rearrange while expanded.
- [ ] Focus feel across the combined region with a hardware keyboard;
      Escape with a native dialog open; dock auto-hide edge cases.
- [ ] The popover re-show after collapse (arrow position, animation).
- [ ] Light theme on an external 1x display; dark on 2x.
- [ ] Long-alias truncation on a real roster (fixture covers 20 chars).

Scripts `scripts/timeline_native_proof.py` and `scripts/board_capture.py`
cover the scripted halves on any Mac with the dev environment.


## Reconciliation pass — 2026-09-12 (boards re-exported 21:51)

Owner re-exported boards 01/02/16/17/18 (trigger folded in; 17/18 now
carry the countdown). Product re-aligned and re-gated against the new
exports:

- The countdown is **plain mono text** ("30m · 14:00" / "1d 12h ·
  14 Sep 01:30") before the pct — not a pill; compact format drops zero
  units ("3h", "3d"), countdownText updated (node tests re-pinned).
- Row anatomy re-measured from the export: left block 104 (was 140),
  track [138, 380], detail 188 (was 152) — bars/gridlines/Now align
  within 1px of the export's vertical features.
- Vertical rhythm: caption lh 1.2, body gap 6 — cards land at y103/326
  vs the export's 101/321 (masked diff in budget).
- `CSWAP_TL_BOARD` no longer suppresses chips (boards include them);
  the flag now guards future additions only.

Verification: board gate 9/9 against the 21:51 exports, browser 20/20,
node 8/8, full pytest 2410+10 skipped. Boards 19/20 unchanged (15:34);
19's composition is pinned by TestLeftComposition, 20's states by the
browser/node suites. Main-panel deltas vs the new 01/02 remain
informational (13.4%/14.3% — roster/illustrative-data variance; the
trigger now matches on both sides). VERIFICATION.md left for the owner
to commit with the design-session changes.
