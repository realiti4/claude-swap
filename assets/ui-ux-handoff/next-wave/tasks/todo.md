# Reset timelines — task checklist

Task list target for this wave (root `tasks/` belongs to the preceding wave).
Plan: [plan.md](plan.md). Build contract: [SPEC.md](../SPEC.md). All items
pending. Old T-ids map: T0→1–3, T1→5–6, T2→4+7, T3→9–10, T4→11–12, T5→13–14.

## Task 1: Bundle chart fonts + CSP

**Status: COMPLETE** (see git log; commits 799bfa5..c038e5c)

**Description:** Ship Inter and IBM Plex Mono as WOFF2 subsets in
`web/fonts/`, declare `@font-face` for the existing `--swap-font-*` tokens,
and extend the CSP with `font-src 'self'` so every machine renders the
boards' typography regardless of installed fonts.

**Acceptance criteria:**
- [ ] `@font-face` covers the weights/styles boards 16–20 use; subsets licensed OFL; versions pinned.
- [ ] CSP allows the bundled fonts and nothing else new.
- [ ] Built wheel contains the font files.

**Verification:**
- [ ] `uv run pytest -n 4 tests/test_menubar_package.py -q` (extended to assert fonts)
- [ ] `uv build --wheel` + wheel contents inspection
- [ ] Manual: panel typography unchanged on machines that already have the fonts

**Dependencies:** None
**Files likely touched:** `src/claude_swap/menubar/web/fonts/*`, `panel.css`, `index.html`, `tests/test_menubar_package.py`
**Estimated scope:** S

## Task 2: Extract Pen tokens into panel.css

**Status: COMPLETE** (see git log; commits 799bfa5..c038e5c)

**Description:** Extract every measurable value for boards 16–20 (type
sizes/weights incl. halves, colors, spacing, the 8px gap, radii, strokes,
bar heights, tick/label placement, icon paths) from `assets/untitled.pen`
and encode as `--swap-tl-*` tokens/classes in `panel.css`. No rendering yet;
extraction notes recorded for review.

**Acceptance criteria:**
- [ ] Token set covers all board 16–20 elements; values traceable to the Pen source.
- [ ] No visual change to the existing panel (tokens unused until Task 8+).

**Verification:**
- [ ] `uv run pytest -n 4 tests/test_menubar_appearance.py -q` (unchanged behavior)
- [ ] Manual: extraction table reviewed against board exports

**Dependencies:** None
**Files likely touched:** `panel.css`, `assets/ui-ux-handoff/next-wave/` (extraction notes)
**Estimated scope:** S

## Task 3: Board capture harness + diff gate

**Status: COMPLETE** (see git log; commits 799bfa5..c038e5c)

**Description:** `scripts/board_capture.py` renders board-equivalent fixture
states in a real WKWebView at 1x with bundled fonts forced;
`tests/test_menubar_board_diff.py` (Pillow, dev group) diffs captures against
board exports — zero differing pixels in geometry/color regions, text
anti-aliasing only (≤0.1%, no shape deltas).

**Acceptance criteria:**
- [ ] Harness self-test: identical images → 0 diff; synthetic perturbation → detected.
- [ ] First main-panel run recorded as a delta report (boards 01–15 were reconciled under fallback fonts).
- [ ] Diff runs offline from committed baselines.

**Verification:**
- [ ] `uv run python scripts/board_capture.py --boards 16,17,18,20` then `uv run pytest tests/test_menubar_board_diff.py -q`
- [ ] `uv run pytest -n 4 -q` (dev group addition doesn't disturb the suite)

**Dependencies:** Task 1 (bundled fonts)
**Files likely touched:** `scripts/board_capture.py`, `tests/test_menubar_board_diff.py`, `pyproject.toml`, `tests/fixtures/`
**Estimated scope:** M

## Task 4: Additive timelineWindows contract

**Status: COMPLETE** (see git log; commits 799bfa5..c038e5c)

**Description:** `viewmodel.py` emits optional per-account
`timelineWindows` (kinds 5h/7d only; nullable `pct`/`resetsAt`/`startsAt`;
`state` in the seven-value set; `observedAt` from `fetched_at`), retaining
elapsed resets and no-pct windows. Per-account staleness. `windows[]` and
quota-card output byte-identical.

**Acceptance criteria:**
- [ ] Contract tests cover field set, state precedence, elapsed retention, sanitization, degraded older-producer path.
- [ ] Existing view-model suites pass unchanged.

**Verification:**
- [ ] `uv run pytest -n 4 tests/test_menubar_timelines.py tests/test_menubar_viewmodel.py -q`

**Dependencies:** None
**Files likely touched:** `viewmodel.py`, `tests/test_menubar_timelines.py`
**Estimated scope:** S

## Task 5: T1 native proof — pick surface variant

**Status: COMPLETE** (see git log; commits 799bfa5..c038e5c)

**Description:** `scripts/timeline_native_proof.py` exercises both variants
— popover resize vs borderless single panel — around a synthetic anchor:
placement readback, arrow offset math, right/left feasibility, secondary
display origin, repeated open/close cleanup. Records the chosen variant and
evidence in the plan before any chart UI exists.

**Acceptance criteria:**
- [ ] Frames logged and screenshots captured for both variants on the main display (plus secondary if present).
- [ ] Variant decision + rationale recorded in `plan.md`.

**Verification:**
- [ ] `uv run python scripts/timeline_native_proof.py` (macOS)
- [ ] Manual review of logged frames/screenshots

**Dependencies:** None
**Files likely touched:** `scripts/timeline_native_proof.py`, `plan.md` (decision record)
**Estimated scope:** M

## Task 6: toggleTimelines bridge + placement + lifecycle

**Status: COMPLETE** (see git log; commits 799bfa5..c038e5c)

**Description:** `app.py` registers the validated `toggleTimelines` action,
computes intended mode from button frame + screen visible frames, resizes or
re-shows per the Task 5 variant, reads back actual placement, pushes
`timelineLayout` (demoting to in-panel on contradiction); JS renders an
*empty* companion with correct layout modes. Full lifecycle: toggle, close
button, Escape, outside-click, main-close teardown, no observer/timer
accumulation.

**Acceptance criteria:**
- [ ] Empty companion opens right/left/in-panel per available space; failure returns to a usable main panel truthfully.
- [ ] Focus/dismissal work across the combined region; repeated cycles leak nothing.
- [ ] `payload_specs` covers the action; wire-contract scan passes.

**Verification:**
- [ ] `uv run pytest -n 4 tests/test_menubar_bridge.py tests/test_menubar_wire_contract.py -q`
- [ ] `node --check src/claude_swap/menubar/web/panel.js src/claude_swap/menubar/web/timelines.js`
- [ ] Manual/native: open/close across displays per QA.md

**Dependencies:** Task 5
**Files likely touched:** `app.py`, `web/panel.js`, `web/timelines.js` (scaffold), `web/index.html`, `tests/test_menubar_bridge.py`
**Estimated scope:** M

## Checkpoint A — Foundations
- [ ] Native placement proven with evidence; snapshot data and geometry tests pass without new polling.
- [ ] Fidelity harness live: fonts bundled, tokens extracted, board diffs wired — gate active for all chart-ui work.
- [ ] Review with human before Phase 2.

## Task 7: Pure geometry module + node tests

**Description:** `timelines.js` geometry layer (DOM-free): `x(t)` mapping,
nominal start, fill fraction with cap-at-track + true label, Now at 50%,
six-step state precedence, offscale detection, tick sparsity, DST-stable
epoch arithmetic. Tested against the frozen fixture fractions.

**Acceptance criteria:**
- [ ] Four nominal accounts × both kinds match expected fractions within tolerance.
- [ ] All 8 `edgeCases`, `dstExample`, clock-jump, and clamp/cap cases pass.

**Verification:**
- [ ] `node --test tests/js/timelines.test.js`
- [ ] `node --check src/claude_swap/menubar/web/timelines.js`

**Dependencies:** Task 4 (fixture/contract shape)
**Files likely touched:** `web/timelines.js`, `tests/js/timelines.test.js`
**Estimated scope:** M

## Task 8: Header trigger + companion scaffold + layout modes

**Description:** Calendar icon (utility-icon system) in the main header with
open-state indication; `timelines.js` DOM scaffold renders the companion
shell — two chart sections, row list, legend, close button — positioned per
`timelineLayout` (right/left/in-panel with Back), styled exclusively with
Task 2 tokens.

**Acceptance criteria:**
- [ ] Toggle round-trips; scaffold matches board 16/17 frame geometry.
- [ ] In-panel mode shows Back and restores on return.

**Verification:**
- [ ] `node --check` on changed JS; fixture-mode browser check of scaffold in both themes
- [ ] Computed-style assertions on scaffold token values

**Dependencies:** Tasks 1, 2, 6
**Files likely touched:** `web/timelines.js`, `web/index.html`, `web/panel.css`, `web/panel.js`
**Estimated scope:** M

## Task 9: Full chart rendering

**Description:** Rows (stable slot order, index/alias, same order both
charts, independent scrolling, fixed heading/axis), neutral full-window
tracks, quota fills with explicit `% used` labels, amber dashed Now line
across rows, sparse relative ticks, persistent legend, active-vs-selected
markers. Board-exact styling.

**Acceptance criteria:**
- [ ] Board 17/18 equivalents render with correct geometry from fixture data.
- [ ] Fidelity gate passes for the rendered charts (both themes) or deltas are fixed, not excused.

**Verification:**
- [ ] `uv run pytest tests/test_menubar_board_diff.py -q`
- [ ] `node --test tests/js/timelines.test.js`

**Dependencies:** Tasks 7, 8
**Files likely touched:** `web/timelines.js`, `web/panel.css`
**Estimated scope:** M

## Task 10: Honest states + live updates + snapshot sync

**Description:** State matrix from `timelineWindows` (stale/elapsed/
usage-unavailable/reset-unavailable/unavailable/no-window, offscale,
>100% cap); Now/countdown tick on the existing 1s interval; `vm`/alias/
appearance pushes update open charts in place, preserving row focus,
selection, detail, and scroll; detail closes safely when its account goes.

**Acceptance criteria:**
- [ ] QA matrix state rows pass; no fabricated zeros or rolled-forward cycles.
- [ ] Alias/active/removal pushes preserve chart state (browser tests).

**Verification:**
- [ ] `uv run pytest -n 4 tests/test_menubar_timelines.py -q` + fixture-mode state matrix
- [ ] `uv run pytest -n 4 tests/test_menubar_alias_sheet.py tests/test_menubar_appearance.py -q`

**Dependencies:** Task 9
**Files likely touched:** `web/timelines.js`, `web/panel.js`, `tests/test_menubar_timelines.py`
**Estimated scope:** M

## Task 11: Details, keyboard, a11y, Escape hierarchy

**Description:** Keyboard/focus details (full alias, index, percent,
remaining, exact local start/reset + timezone, inferred-start marking);
Tab/Enter/Space; two-stage Escape restoring opener focus; ARIA roles/labels;
keyboard access equal to hover.

**Acceptance criteria:**
- [ ] QA keyboard/focus rows pass, including existing modal coordination without competing focus traps.

**Verification:**
- [ ] Fixture-mode keyboard sequence tests; `node --check`

**Dependencies:** Task 10
**Files likely touched:** `web/timelines.js`, `web/sheets.js`, `web/panel.css`
**Estimated scope:** M

## Task 12: Session additions — crosshair, chips, `T` shortcut

**Description:** Pointer crosshair with timestamp guide (hover-capable
pointers, reduced-motion respected); per-chart countdown chips on rows
ticking at 1s; `T` toggles timelines when the main panel holds focus,
tooltip documents it.

**Acceptance criteria:**
- [ ] Additions work without altering Escape hierarchy or selection semantics.

**Verification:**
- [ ] Fixture-mode interaction tests; fidelity re-run (chips are board deltas → recorded for owner re-export approval)

**Dependencies:** Task 11
**Files likely touched:** `web/timelines.js`, `web/panel.css`
**Estimated scope:** S

## Checkpoint B — Charts
- [ ] Both chart types and the complete state matrix agree with the design and live data.
- [ ] Boards 16/17/18/20 equivalents pass the pixel-fidelity gate in both themes (19 composition via harness variant); computed-style token assertions green.
- [ ] Review with human before Phase 3.

## Task 13: Fallback/scroll/reduced-motion/modal hardening pass

**Description:** Sweep the QA browser/native matrix for narrow fallback,
many-account (10+) scrolling, reduced-motion instant transitions, and modal
coordination edge cases; fix what the sweep finds.

**Acceptance criteria:**
- [ ] Every QA.md browser/native row passes or is explicitly documented as manual-pending.

**Verification:**
- [ ] Full fixture matrix at 360/968/narrow; `uv run pytest -n 4 -q`

**Dependencies:** Checkpoint B
**Files likely touched:** `web/timelines.js`, `web/panel.css`, focused tests
**Estimated scope:** S

## Task 14: Full verification, packaging, reconciliation, completion report

**Description:** Full suites + wheel inspection; native multi-display
evidence and the manual-pass checklist; IMPLEMENTATION-NOTES and handoff
reconciliation (incl. font-driven main-panel shift, chip/crosshair board
deltas for owner re-export); completion report with exact commands/results
and explicit unverified cases.

**Acceptance criteria:**
- [ ] QA.md "Completion evidence" satisfied; no release/version change.

**Verification:**
- [ ] `uv build --wheel && uv run pytest -n 4 -q` + QA.md commands
- [ ] Human review of the completion report

**Dependencies:** Task 13
**Files likely touched:** `IMPLEMENTATION-NOTES.md`, handoff docs, evidence files
**Estimated scope:** M

## Checkpoint C — Complete
- [ ] SPEC success criteria met; final review with human before the wave is called done.
