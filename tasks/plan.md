# Implementation Plan: Menubar redesign — Pen handoff

Source: `SPEC.md` (committed) ← design doc
`docs/superpowers/specs/2026-09-12-menubar-redesign-design.md` ← Pen
handoff in `assets/` (authoritative visuals).

## Overview

Rebuild the panel's web layer to the graphite/ivory Pen redesign as three
plain script files (icons / sheets / panel) over a token-seeded CSS, with
two additive view-model changes (account.status, preserve-measured
windows) and one reply-path bug fix. Bridge protocol, shells, and all
core contracts unchanged. Pure layers land first (fully pytest-covered),
then the web layer rises against the fixture browser, then states,
accessibility, and a full verification sweep.

## Architecture Decisions

- **In-place web rewrite (no parallel v2 dir)** — the wire-contract tests
  and fixture mode keep the single source honest; mid-sequence commits
  may be briefly ugly (old JS against new CSS) but never broken: the
  panel always loads and every bridge action keeps working.
- **Contract-first again**: status vocabulary + preserve-measured windows
  are pinned by view-model tests before any UI consumes them.
- **Native `<dialog>`** for sheets (focus trap/Esc/restore from the
  platform). Fallback guard if `showModal` is unavailable in an older
  WKWebView.
- **Fixture browser is the UI gate**: every web task verifies by pushing
  synthetic view-models to the fixture page; light theme is derived and
  human-reviewed at Checkpoint B.

## Task List

### Phase 1: Pure layers (pytest-covered, no UI)
- [x] Task 1: `account.status` mapping + contract tests
- [x] Task 2: preserve-measured windows + pace gating + tests
- [x] Task 3: JS reply-adapter error fix + wire-contract regression test

### Checkpoint A: pure layers
- [x] `uv run pytest` green; status vocabulary + no-fabricated-zero pinned

### Phase 2: Web layer (fixture-verified per task)
- [x] Task 4: `panel.css` token foundation + `icons.js` + `index.html` skeletons
- [x] Task 5: `panel.js` core render — header, selector cards, identity, quotas
- [x] Task 6: `panel.js` completion — disclosure, actions, auto-switch, footer
- [x] Task 7: `sheets.js` — token/remove/activity `<dialog>`s + wiring

### Checkpoint B: full panel in fixture + hosted
- [ ] Both themes render the main artboard faithfully; sheets work; human review of derived light theme

### Phase 3: States, accessibility, verification
- [ ] Task 8: all artboard states, keyboard, reduced motion, icon fidelity
- [ ] Task 9: verification sweep — vision gates, live run, README/screenshots refresh

### Checkpoint C: complete
- [ ] SPEC success criteria 1–7 verified; Definition of Done satisfied

## Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Mid-sequence commits look unstyled (old JS, new CSS) | Low | Fixture verification per task; functionality never breaks |
| `<dialog>`/`showModal` missing in older WKWebView | Med | Capability check + class-based fallback at Task 7 |
| Derived light theme drifts from Pen intent | Med | Human review gate at Checkpoint B; token table + mirrored layout |
| Icon fidelity vs Pen artboards (no exports shipped) | Low | Existing icon language baseline; judged at Task 8 fixture pass |
| Preserve-measured changes pace tests subtly | Low | Pace-gating tests extended in Task 2, before UI consumes them |

## Open Questions

None blocking (see SPEC).
