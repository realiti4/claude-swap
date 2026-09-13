# Spec: Reset Timelines — next wave

Operationalizes the owner-approved design
(`docs/superpowers/specs/2026-09-12-reset-timelines-design.md`, session of
2026-09-12) using the spec-driven-development workflow. Lives with the design
package per project convention; the design doc remains the decision record,
this file is the build contract. Design authority for visuals: Pen source
(`assets/untitled.pen`) and board exports 16–20.

**Owner mandate: pixel-perfect fidelity to the design assets — no exceptions.**

## Capability Map

| Module id | Responsibility | Depends on |
| --- | --- | --- |
| `fonts-fidelity` | Bundle Inter + IBM Plex Mono WOFF2, CSP `font-src 'self'`, token extraction from Pen, board capture + diff harness | — |
| `timeline-data` | Additive `timelineWindows` view-model contract + contract tests | — |
| `native-surface` | `toggleTimelines` bridge action, wide-surface placement/readback/`timelineLayout` push, lifecycle, T1 native proof | — |
| `chart-ui` | `timelines.js` geometry + DOM, honest states, a11y/keyboard, live ticks, header trigger | `fonts-fidelity`, `timeline-data`, `native-surface` |
| `release-verify` | Full verification, packaging, reconciliation, completion evidence | `chart-ui` |

Build order: `fonts-fidelity`, `timeline-data`, `native-surface`
(parallelizable) → `chart-ui` → `release-verify`. Mapping onto the handoff's
T-sequence: `native-surface` = T1, `timeline-data` = T2 (data half),
`chart-ui` = T2 (render half) – T4, `release-verify` = T5. `fonts-fidelity`
is the session-added module; it precedes all chart CSS work and its diff gate
blocks Checkpoints A and B.

## Objective

Claude Code Swap users juggle several accounts whose session (5h) and weekly
(7d) quotas reset at different times. This wave adds a calendar button to the
main header that opens an adjacent companion panel with two full-window Gantt
charts — one row per account, quota-used fills, shared Now marker — so the
user can see at a glance when every account resets and how spent it is,
without switching accounts or extra API calls.

Success looks like: boards 16–20 reproduced exactly (pixel-fidelity gate),
all honest-data states correct per the QA matrix, main panel behavior
unchanged, and the full existing suite green.

## Tech Stack

- Python ≥ 3.12 (`requires-python`), PyObjC `Cocoa`/`WebKit` (menubar extra) for the native shell.
- Vanilla JS/CSS in WKWebView — no framework, no chart library; classic `<script>` includes per existing pattern.
- pytest + pytest-xdist (dev group); **Pillow in the dev group (proposed — ask-first)** for the pixel diff.
- Node (already required by QA commands) for `node --check` and `node --test` on pure geometry.
- uv + hatchling for build/package.
- Pen file is read-only design source: we extract values; Pen edits belong to the design owner. If implementation must deviate, the delta is recorded and the board re-exported by the owner — never silently.

## Commands

```sh
# focused menubar suites (existing)
uv run pytest -n 4 tests/test_menubar_viewmodel.py tests/test_menubar_bridge.py \
  tests/test_menubar_wire_contract.py tests/test_menubar_import_smoke.py \
  tests/test_menubar_package.py -q
uv run pytest -n 4 tests/test_menubar_appearance.py tests/test_menubar_alias_sheet.py -q

# this wave (new)
uv run pytest -n 4 tests/test_menubar_timelines.py -q      # VM contract + bridge specs
node --test tests/js/timelines.test.js                      # pure geometry vs fixtures
node --check src/claude_swap/menubar/web/timelines.js

# pixel-fidelity gate (macOS only; uses production WKWebView rasterizer)
uv run python scripts/board_capture.py --boards 16,17,18,20
uv run pytest tests/test_menubar_board_diff.py -q           # Pillow diff vs exports

# native proof (T1) and full gate
uv run python scripts/timeline_native_proof.py
uv build --wheel && uv run pytest -n 4 -q
```

## Project Structure

```
src/claude_swap/menubar/         # app.py (shell/bridge), viewmodel.py (pure VM), bridge.py
src/claude_swap/menubar/web/     # index.html, panel.js, panel.css, sheets.js,
                                 # timelines.js (new), fonts/*.woff2 (new, packaged)
src/claude_swap/menubar/assets/  # packaged native icons (unchanged)
tests/                           # pytest suites; tests/js/*.test.js for node --test
tests/fixtures/                  # board baselines/captures for the diff gate (new)
scripts/board_capture.py         # WKWebView capture harness (new, macOS)
scripts/timeline_native_proof.py # T1 native placement proof (new, macOS)
assets/ui-ux-handoff/next-wave/  # this spec + tasks/plan.md + tasks/todo.md (wave tracker)
```

## Code Style

Follow the existing modules. Pure transforms carry docstrings that state the
constraint, not the mechanics; JSON stays additive (optional fields are
absent, never `null`); JS uses data attributes + one live tick, no framework.

```python
# viewmodel.py — additive emission, explicit nullable chart fields
def _timeline_window_vm(kind, window, *, now, stale):
    """One timelineWindows entry; None only for no-window kinds.

    pct/resetsAt are nullable by design: null means unknown, never zero.
    Elapsed resetsAt is retained — the chart, not the VM, decides display.
    """
```

```js
// timelines.js — pure geometry, DOM-free, node-testable
export function layoutWindow(resetAt, pct, now, domainS, width) {
  // x(t) = W * (t - (N-D)) / (2D); fill endpoint is a quantity, not a time
  ...
}
```

## Testing Strategy

Four layers, all blocking at their checkpoints:

1. **pytest** — VM contract (field set, state precedence, elapsed retention,
   per-account staleness, degraded older-producer, sanitization); bridge
   `payload_specs`; packaging includes `timelines.js` + `web/fonts/`; wire
   scan covers the new module.
2. **node --test** — pure geometry against `next-wave/fixtures/timelines.json`
   (four nominal accounts × both kinds, all 8 `edgeCases`, DST example,
   clock-jump, clamp/cap) with float tolerance.
3. **Pixel-fidelity gate** — `board_capture.py` renders board-equivalent
   fixture states in a real WKWebView (production rasterizer, bundled fonts
   forced) at 1x board dimensions; Pillow diff vs board exports: zero
   differing pixels in geometry/color regions, text regions anti-aliasing
   only (≤0.1%, no shape deltas). Computed-style assertions pin extracted
   token values.
4. **Browser fixture matrix + native evidence** — states, keyboard sequence,
   two-stage Escape, focus return, reduced motion; T1 proof frames/screenshots;
   documented manual-pass checklist for tactile checks.

## Boundaries

- **Always:** run the focused suites before calling a task done; extract
  visual values from the Pen source before writing chart CSS; keep timeline
  interactions free of account activation, credential writes, polling-policy
  and preference writes; preserve row focus/selection/detail/scroll across
  snapshot pushes; keep NaN/Infinity off the wire.
- **Ask first:** new dependencies (Pillow proposed); CSP changes; any bridge
  action beyond `toggleTimelines`; edits to design-package files beyond
  `tasks/plan.md` / `tasks/todo.md` reconciliation; committing the design
  package to git.
- **Never:** publish a release or bump versions; accept a visual delta vs
  boards silently; fabricate zeros or roll forward resets; parse countdown
  strings as data; touch unrelated in-flight work (`uv.lock`, `docs/audits/`,
  root `tasks/` tracker of the preceding wave); load screenshot images as
  interface.

## Success Criteria

- Boards 16, 17, 18, 20 equivalents pass the pixel-fidelity gate in both
  themes; board 19's composition verified via the harness layout variant.
- `TIMELINES.md` acceptance list passes (placement both sides + fallback,
  correct resets, equal-width windows, Now marker, countdowns, exact dates,
  DST/midnight, fills agree with measurements/main card, no fabricated data,
  focus/Escape/outside-click across the combined region, multi-display,
  many-account scrolling, narrow fallback, keyboard/reduced-motion/contrast,
  no activation/credential/polling changes).
- Session additions work: crosshair, per-chart countdown chips, `T` shortcut.
- `toggleTimelines` round-trips with validated payloads; no observer/timer
  accumulation across repeated cycles; degraded older-producer path covered.
- Full suite green; wheel contains new JS + fonts; completion evidence per
  QA.md; reconciliation updates recorded; no release step.

## Open Questions

1. Diff tooling: Pillow (recommended, stays in uv/pytest) vs node/pixelmatch
   (adds an npm project) — gate below.
2. Board capture cadence: local-macOS-only with committed baselines vs CI
   attempt — recommendation: local-only, baselines committed.
3. Commit timing for the untracked `next-wave/` package + this spec.

## Module specs

### `fonts-fidelity`
Objective: make every machine render the boards' typography and enforce it
forever. Boundary: `web/fonts/`, `@font-face`, CSP `font-src`, extraction
notes, `board_capture.py`, diff test. Acceptance: fixture harness renders
with bundled fonts regardless of host fonts; diff gate enforces §0 of the
design doc; token values asserted. Out: chart CSS itself.

### `timeline-data`
Objective: close the two VM gaps additively. Boundary: `viewmodel.py` +
tests only; `windows[]`/quota-card output byte-identical. Acceptance:
contract tests incl. degraded producer; no new fetching.

### `native-surface`
Objective: T1-proof-then-implement the single wide surface. Boundary:
`app.py` (+ bridge specs), placement/readback/`timelineLayout`, lifecycle
cleanup; empty companion content. Acceptance: right/left/in-panel per §1 of
the design doc, focus/dismissal across the combined region, no accumulation,
failure returns to usable main panel truthfully; proof evidence recorded.

### `chart-ui`
Objective: render boards 17–20 exactly with honest states. Boundary:
`timelines.js` (+ include/wire scan), `index.html`, `panel.css`, fixture
mode extensions. Acceptance: QA matrix rows all pass; fidelity gate green;
selection never activates; live ticks without remote polls.

### `release-verify`
Objective: prove the wave shippable without shipping. Boundary: full suites,
wheel inspection, native evidence + manual checklist, IMPLEMENTATION-NOTES /
handoff reconciliation, completion report. Acceptance: QA.md "Completion
evidence" satisfied; unverified cases listed explicitly; no version change.
