# Implementation Plan: Claude Code Swap final UI

Source: `SPEC.md` (committed 4d2e062) ← brainstorm design
`docs/superpowers/specs/2026-09-12-claude-code-swap-final-ui-design.md` ←
authoritative handoff `assets/ui-ux-handoff/HANDOFF.md` + 15 boards.

## Overview

Land the final Claude Code Swap design on the existing menubar shell in
five staged vertical slices: Account card + labels (fixing the WIP in
the working tree forward), numbered index-tab selector, alias editor
(bridge actions + sheet — the one new feature), segmented settings, and
Interlock branding (panel SVG + native template status icon + packaged
assets). Every stage lands green on `dev`; the bridge protocol gains
exactly two actions (`setAlias`, `unsetAlias`) and one additive reply
extension (`getPrefs`); nothing else in the core changes.

## Architecture Decisions

- **Fix-forward the WIP card edits** (approved in brainstorm): stage 1
  corrects the uncommitted `panel.js`/`panel.css` changes to final
  spec — no WIP commit, no discard.
- **Contract-first for the alias feature**: bridge specs + handler
  tests (task 4) land before the sheet UI (task 5), mirroring the
  0.28.0 pattern that kept the wire honest.
- **New handlers stay inside the `_panel_handlers` block and
  `payload_specs={...}` call** (app.py ~488–553) so the wire-contract
  extraction anchors keep working; the wire test's send-scan gains the
  alias sheet's literals in task 5.
- **Inline SVG, not `<img>`**, for the panel Interlock (currentColor
  must follow both themes); native icon = template NSImage from the
  packaged `icon-template.pdf`, PNG pair as raster fallback.
- **Fixture browser remains the UI gate** per task; native runs at
  checkpoints for template rendering and persistence-across-relaunch.

## Task List

Tasks tracked in `tasks/todo.md`.

### Phase 1: Account card & labels
- [ ] Task 1: Final Account card + section labels
- [ ] Task 2: View-model card-field contract pin

### Checkpoint A: card & labels
- [ ] Focused suites green; both themes render board 12 structure

### Phase 2: Numbered selector
- [ ] Task 3: Index-tab selector with radiogroup semantics

### Checkpoint B: selector matrix
- [ ] Fixture matrix passes: 3/10+ accounts, empty roster, states

### Phase 3: Alias editor (vertical slice)
- [ ] Task 4: Bridge `setAlias`/`unsetAlias` + snapshot push
- [ ] Task 5: Alias sheet UI + card Add/Edit/Remove

### Checkpoint C: alias end-to-end
- [ ] Save/normalize/reject/duplicate/cancel/remove/refresh-race pass

### Phase 4: Settings segments
- [ ] Task 6: Segmented settings + extended `getPrefs`

### Checkpoint D: settings honesty
- [ ] Stored values shown; persistence + rollback verified natively

### Phase 5: Interlock branding
- [ ] Task 7: Panel header rebrand + icons.js Interlock
- [ ] Task 8: Native template icon + packaged assets

### Checkpoint E: complete
- [ ] Full state matrix + HANDOFF acceptance sweep + suite green
- [ ] Fixture screenshots + native eyeball list to user → /review

## Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Template PDF renders poorly on some macOS build | Med | PNG 18/36 fallback path in same task; verify 1x/2x light/dark |
| Selector rewrite breaks scroll/disclosure preservation | Med | Port the existing preservation logic verbatim; fixture check in-task |
| Wire-contract anchors shift when handlers are added | Med | Keep new registrations inside the anchored blocks; suite gate |
| Segmented settings regresses appearance rollback | Low | Extend the existing node-vm appearance tests to segments |
| Hatch packaging misses new asset types | Low | Packaging test asserts files in built wheel |
| iCloud working tree mid-build | Low | Commit per task; no service/runtime deps on repo paths |

## Open Questions

None — defaults recorded in SPEC.md; build-time choices resolve from
the boards.
