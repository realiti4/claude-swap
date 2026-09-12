# Implementation Plan: Menubar v2 — CodexBar-class web panel

Source: `SPEC.md` (committed) ← design doc `docs/superpowers/specs/2026-09-12-menubar-panel-design.md`

## Overview

Replace the rumps text-menu menubar with a PyObjC status item whose
left-click opens an NSPopover + WKWebView panel (bundled vanilla HTML/CSS/JS),
right-click keeps a slim NSMenu fallback. Pure Python layers (view-model,
bridge routing) are built and unit-tested first on all platforms; the
macOS-only shell follows as a vertical slice; feature completion and polish
last. The core (switcher, usage store, locks, credentials) is untouched.

## Architecture Decisions

- **Contract-first**: `viewmodel.py`'s additive JSON schema is the pivot
  everything else consumes (panel.js renders it, bridge.py carries it).
  Built and pinned by a key-snapshot test before any UI work.
- **Old app preserved until replaced, not rewritten in place**: Task 1
  converts `menubar.py` → `menubar/` package by moving the existing module
  wholesale (re-exported), so the repo stays green at every commit; the
  rumps glue is deleted only when the PyObjC shell replaces it (Task 4).
- **Fail-fast on the one real unknown**: the riskiest piece is PyObjC
  popover/WKWebView wiring. The shell lands early (Tasks 4-5) so the
  approach is proven before polish investment; if it fails, a menu-only v2
  from Task 4 still ships value and we reassess.
- **Panel developed against fixtures**: `panel.js` runs standalone in a
  browser with fixture data (`window.webkit` absent), so UI work is
  parallel with shell work and independently verifiable.
- **Vertical slices over layers**: each task leaves `cswap menubar` runnable
  and the full pytest suite green.

## Task List

(Details in `tasks/todo.md`.)

### Phase 1: Foundation — pure layers, fully testable
- [x] Task 1: Convert `menubar.py` to `menubar/` package (pure re-export, zero behavior change)
- [x] Task 2: `viewmodel.py` — move pure helpers, add `build()`, pin schema contract
- [x] Task 3: Panel web v1 — fixture-mode rendering of the full view-model

### Checkpoint A: after Tasks 1-3
- [ ] `uv run pytest` green (existing + new viewmodel tests)
- [ ] `open src/claude_swap/menubar/web/index.html` renders fixture panel in browser
- [ ] Human review before shell work

### Phase 2: Shell — macOS vertical slice (fail-fast gate)
- [x] Task 4: PyObjC status item + right-click menu + snapshot loop + osascript notifications; swap pyproject extra; delete rumps glue
- [x] Task 5: Popover + WKWebView + bridge integration (live panel)

### Checkpoint B: after Tasks 4-5
- [ ] `uv run cswap menubar` shows panel with live data; right-click menu works
- [ ] `--install-service` / `--service-status` / `--uninstall-service` flow works
- [ ] Linux CI still green (no PyObjC imports leak)
- [ ] Human review — this is the go/no-go for the approach

### Phase 3: Completion & polish
- [x] Task 6: All panel actions end-to-end (switch/rotate/best/disable/enable/remove/add/auto-switch/prefs)
- [x] Task 7: Stale, error, quarantine, and webview-failure states
- [x] Task 8: Dark mode + CodexBar-class visual polish + README screenshot
- [ ] Task 9: Docs (README section, ARCHITECTURE.md §6.5/map) + macOS CI import smoke (ask-first)

### Checkpoint C: complete
- [ ] All SPEC.md success criteria met, Definition of Done satisfied
- [ ] Ready for `/review` and release

## Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| PyObjC popover/WKWebView wiring unworkable in this env | High | Tasks 4-5 early (fail-fast); Task 4 alone still ships a better menu app; fallback documented |
| WKWebView keyboard focus (token sheet input) | Med | Minimal input surface; test at Task 6; fallback = paste via right-click menu item |
| PyObjC version floor wrong (open question in SPEC) | Med | Pin `>=10.0` initially; verify against CI runners at Task 9 |
| Memory growth from WKWebView in always-on service | Med | launchd restart-on-crash already in place; monitor logs |
| Moving ~400 lines of menubar.py helpers breaks hidden importers | Low | Re-export surface pinned by existing tests; grep repo for imports first |

## Open Questions

- PyObjC minimum version (SPEC open question 1) — resolve at Task 9
- README screenshot vs placeholder (SPEC open question 2) — Task 8 captures one if a populated store is available; else placeholder
