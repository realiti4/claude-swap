# Implementation Plan: Reset Timelines

Task list target: [todo.md](todo.md) in this directory — the project
designates the wave tracker here; the repository-root `tasks/` belongs to the
preceding wave and stays untouched. Task list target per
planning-and-task-breakdown defaults would be root `tasks/todo.md`; the
design package's designation overrides it.

Inputs (reference only, not to be redone): decision record
`docs/superpowers/specs/2026-09-12-reset-timelines-design.md` · build
contract [SPEC.md](../SPEC.md) · design authority
[TIMELINES.md](../../TIMELINES.md) + Pen source `assets/untitled.pen` ·
[DATA-CONTRACT.md](../DATA-CONTRACT.md) · [QA.md](../QA.md) ·
[fixtures/timelines.json](../fixtures/timelines.json).

## Overview

Add a calendar button to the main header that opens an adjacent companion
panel with two full-window Gantt charts (Session ±5h, Weekly ±7d) — one row
per account, quota-used fills, shared Now marker — rendered pixel-perfect
against boards 16–20, with honest data states, one snapshot, no new polling,
and the compact main panel preserved.

## Architecture Decisions

- **Single wide native surface** (owner-approved): one popover/surface at
  968×560 expanded; AppKit placement read back after show → `timelineLayout`
  push (`right|left|in-panel` + anchor offset); dead-center anchors degrade
  to in-panel. T1-proof (Task 5) picks popover-resize vs borderless-panel
  variant and records evidence before chart UI work.
- **Additive `timelineWindows`** VM contract (per DATA-CONTRACT.md);
  `windows[]`/quota-card output stays byte-identical.
- **Pixel-perfect fidelity is a blocking gate** (owner mandate): fonts
  bundled (Inter + IBM Plex Mono WOFF2, CSP `font-src 'self'`), tokens
  extracted from the Pen source before chart CSS, WKWebView capture harness
  + Pillow diff (zero-tolerance geometry; text anti-aliasing ≤0.1% only).
- **Geometry/state logic separated from DOM** in `web/timelines.js` so node
  tests verify time arithmetic and state honesty deterministically. This
  module split is mandated by the design package; the plan therefore slices
  chart-ui vertically *within* that boundary (scaffold → render → states →
  interactions) rather than by layer.
- **Tooling**: Pillow joins the uv dev group for the diff (recommended,
  pending user veto). Board capture runs locally on macOS with the
  production rasterizer; baselines committed.

## Task List

### Phase 1 — Foundations (Tasks 1–6, parallelizable per dependency notes)
- [ ] Task 1: Bundle chart fonts + CSP (fonts-fidelity)
- [ ] Task 2: Extract Pen tokens into panel.css (fonts-fidelity)
- [ ] Task 3: Board capture harness + diff gate (fonts-fidelity)
- [ ] Task 4: Additive timelineWindows contract (timeline-data)
- [ ] Task 5: T1 native proof — pick surface variant (native-surface)
- [ ] Task 6: toggleTimelines bridge + placement + lifecycle (native-surface)

### Checkpoint A — Foundations (after Tasks 1–6)
- [ ] Native placement proven with evidence; VM contract tests green; fonts
      packaged; fidelity harness live and gate active.

### Phase 2 — Chart UI (Tasks 7–12, sequential)
- [ ] Task 7: Pure geometry module + node tests (chart-ui)
- [ ] Task 8: Header trigger + companion scaffold + layout modes (chart-ui)
- [ ] Task 9: Full chart rendering — rows/bars/fills/Now/ticks/legend (chart-ui)
- [ ] Task 10: Honest states + live updates + snapshot sync (chart-ui)
- [ ] Task 11: Details, keyboard, a11y, Escape hierarchy (chart-ui)
- [ ] Task 12: Session additions — crosshair, chips, `T` shortcut (chart-ui)

### Checkpoint B — Charts (after Tasks 7–12)
- [ ] QA matrix rows pass; boards 16/17/18/20 pass the fidelity gate in both
      themes (19 composition via harness variant); token assertions green.

### Phase 3 — Hardening and release evidence (Tasks 13–14)
- [ ] Task 13: Fallback/scroll/reduced-motion/modal hardening pass
- [ ] Task 14: Full verification, packaging, reconciliation, completion report

### Checkpoint C — Complete
- [ ] SPEC success criteria met; review with human before calling the wave done.

## T1 decision record (2026-09-12, evidence: tests/fixtures/captures/native-proof.json)

Proof run on the development machine (single 5120×1440 display, macOS 26,
uv Python). Both variants measured against the real status item plus
simulated anchors at 15%/50%/90% of the visible frame:

- **Variant A — popover resize: REJECTED.** The resize itself is sound
  (968×560 in place, stays shown, 5/5 clean open/resize/close cycles), but
  AppKit centers the wide surface on the anchor at every position
  (content arrow offset 484/968), leaving ~296px per side of the anchored
  main column — `companion_fits_600` is false for **all** anchors. A
  centered popover can never host the 600px companion beside the anchored
  main column.
- **Variant B — single borderless NSPanel for the expanded state:
  CHOSEN.** Manual placement fits at every probed anchor, and edge-aligning
  the main column under the anchor (instead of centering) makes the
  companion fit whenever the display can hold 968px at all:
  companion-right iff `anchorX ≤ visibleRight − 788`, companion-left iff
  `anchorX ≥ visibleLeft + 788`, else the designed in-panel fallback.

Implementation shape for Task 6: the collapsed state keeps the existing
transient NSPopover untouched (arrow + free dismissal); expanding closes
the popover and re-parents the *same* WKWebView into a borderless NSPanel
(DOM/JS state survives), placed with the main column edge-aligned under
the anchor and clamped to the visible frame; `timelineLayout`
(`right|left|in-panel` + anchor offset) is computed from the placed frame
exactly as the spec's readback describes. The panel's outside-click
dismissal and Escape route through JS-first handlers plus one global
event monitor, torn down on collapse. Note: screenshots were not taken
for the empty-container probes — frame geometry is the decision evidence.

## Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Popover can't keep main column under a mid-screen anchor | High | Anchor-offset readback; dead-center → in-panel; borderless variant as proof fallback (Task 5) |
| Transient dismissal/arrow differs across macOS versions | High | T1 proof on this machine first; variant decision recorded with evidence |
| Host font availability breaks pixel fidelity | High | Bundled WOFF2 + CSP; harness forces bundled fonts (Tasks 1, 3) |
| Renderer drift vs Pen exports | Medium | Zero-tolerance on computed values; image diff AA-only ≤0.1%; Pen source wins disagreements |
| VM change breaks quota cards | High | Additive-only; existing suites pin `windows[]` (Task 4) |
| Snapshot refresh resets chart UI state | Medium | Push-handling tests preserving selection/scroll (Task 10) |
| Packaging omits new JS/fonts | Medium | Wheel-content inspection (Tasks 1, 14) |

## Open Questions

- Pillow in the dev group (recommended; awaiting user veto-or-confirm).
- Commit timing for the untracked `next-wave/` package (recommended: one
  clean commit before Task 1 starts).
- Boards 01–15 may need an owner re-export after fonts land (main panel
  shifts toward spec on machines without Inter); handled in Task 14
  reconciliation.
