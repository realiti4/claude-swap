# Tasks: Menubar redesign — Pen handoff

Plan: `tasks/plan.md` · Spec: `SPEC.md` · Design:
`docs/superpowers/specs/2026-09-12-menubar-redesign-design.md`.
Definition of Done applies to every task on top of its own acceptance
criteria.

## Task 1: `account.status` mapping + contract tests

**Description:** Additive `status` field on view-model accounts, derived
from the sentinel: `api key` → `"api-key"`, `re-login needed` →
`"needs-login"`, `token expired`/`keychain unavailable`/foreign-credential
→ `"unavailable"`, none → `"ok"`. `quarantined` remains emitted (wire
compat) but the panel will key off `status`. Contract tests pin the
vocabulary and every sentinel mapping.

**Acceptance criteria:**
- [x] Each sentinel maps to its status; non-sentinel accounts emit `"ok"`
- [x] `status` added to ALLOWED/REQUIRED contract sets; vocabulary pinned
- [x] No existing field changed or removed (additive only)

**Verification:**
- [x] `uv run pytest tests/test_menubar_viewmodel.py -x` green; full suite green

**Dependencies:** None
**Files:** `src/claude_swap/menubar/viewmodel.py`, `tests/test_menubar_viewmodel.py`
**Estimated scope:** S

## Task 2: preserve-measured windows + pace gating

**Description:** Panel view-model stops zeroing rolled/passed windows:
five-hour and weekly windows whose reset passed keep measured `pct`,
`state: "stale"`, `countdownText: "Awaiting updated usage"`. Pace chip
data is suppressed when the measurement is stale. Legacy helpers
(`usage_summary`, `format_title`) untouched — CLI/TUI keep roll-to-zero.

**Acceptance criteria:**
- [x] Passed weekly AND five-hour windows keep measured pct + stale + awaiting text
- [x] Pace absent for stale accounts; present otherwise (existing tests stay green)
- [x] Legacy helper behavior unchanged (existing legacy tests prove it)

**Verification:**
- [x] `uv run pytest tests/test_menubar_viewmodel.py tests/test_menubar.py -x` green; full suite green

**Dependencies:** Task 1 (same file)
**Files:** `src/claude_swap/menubar/viewmodel.py`, `tests/test_menubar_viewmodel.py`
**Estimated scope:** S-M

## Task 3: reply-adapter error fix + wire regression test

**Description:** `panel.js`'s `cswap.reply` currently forwards
`result.data` on failures, so toasts show a generic message; pass
`result.error` through. Wire-contract test pins that the reply path
preserves error text (source-level or behavioral).

**Acceptance criteria:**
- [x] Reply path forwards `.error` on `ok:false` (source check in wire-contract tests)
- [x] Full suite green

**Verification:**
- [x] `uv run pytest tests/test_menubar_wire_contract.py -x`; full suite green

**Dependencies:** None
**Files:** `src/claude_swap/menubar/web/panel.js`, `tests/test_menubar_wire_contract.py`
**Estimated scope:** S

## Checkpoint A: pure layers
- [x] Full suite green; status vocabulary and no-fabricated-zero pinned by tests

## Task 4: token CSS foundation + icons + HTML skeletons

**Description:** Rebuild `panel.css` from
`assets/menubar-redesign-tokens.css` (`--swap-*` vars, graphite/ivory,
`data-swap-theme` + `?theme=` override, reduced-motion, Inter/Plex-Mono
fallbacks, 44/486/30 frame, tabular numerals). New `icons.js`
(currentColor SVG factory: swap/refresh/gear/best/rotate/chevron/activity).
`index.html`: load order (icons → sheets → panel), static `<dialog>`
skeletons (token/remove/activity), CSP unchanged. Old panel.js keeps
functioning against the new CSS (ugly but working).

**Acceptance criteria:**
- [x] Fixture page loads with new tokens; both themes flip correctly via `?theme=`
- [x] `node --check` passes on icons.js; every icon renders via `icon("name")`
- [x] Old panel remains bridge-functional (fixture actions still log)

**Verification:**
- [x] Browser: fixture renders (unstyled-ish), theme override works
- [x] `uv run pytest` green

**Dependencies:** Task 3 (panel.js state)
**Files:** `web/panel.css`, `web/icons.js`, `web/index.html`
**Estimated scope:** M

## Task 5: panel.js core render

**Description:** Rewrite `panel.js` render to the main artboard: brand
header (icon + wordmark + refresh + gear), account selector cards
(selected = teal ring + dot; active/ready/disabled/needs-login subtitle),
selected-identity row (alias ≥14px, full email, org, Active/Preview
badge), primary quotas ("N% USED" + slim bar + countdown sub-line), and
the state/pending plumbing for actions. Local countdown → "Awaiting
updated usage" at zero.

**Acceptance criteria:**
- [ ] Fixture with the spec's example data (Work 68%/41%, 84% model, $12.40/$100) matches the dark artboard's structure
- [ ] Selected ≠ active visually and behaviorally (selection never switches)
- [ ] Missing values render unavailable, never 0%; stale states render per Task 2

**Verification:**
- [ ] Browser fixture: DOM + visual check vs artboard; theme flip
- [ ] `uv run pytest` green (wire contract: only registered actions)

**Dependencies:** Tasks 1, 2, 4
**Files:** `web/panel.js`
**Estimated scope:** M

## Task 6: panel.js completion — disclosure, actions, auto-switch, footer

**Description:** Collapsed per-model + spend disclosure (`›`), full-width
primary switch (pending/disabled states), half-width Best/Rotate with
icons, iOS-style auto-switch toggle with persistent "at N% used ·
strategy" summary, footer (freshness dot + age/stale note, Activity ›,
Add account), overflow items under gear (disable/enable, remove…,
settings).

**Acceptance criteria:**
- [ ] Every bridge action reachable from the panel; pending blocks duplicates and keeps focus
- [ ] Disclosure collapsed by default; spend/model subordinate
- [ ] Auto-switch summary always visible; toggle reflects vm state immediately

**Verification:**
- [ ] Browser fixture: click-through all controls; wire-contract green
- [ ] `uv run pytest` green

**Dependencies:** Task 5
**Files:** `web/panel.js`
**Estimated scope:** M

## Task 7: sheets.js — dialogs and wiring

**Description:** Token sheet (concealed input, optional email, field
errors, clears on close), remove confirmation (identity + consequence),
activity sheet (switch history from `vm.history`), all native
`<dialog>.showModal()` with Esc, focus trap/restore, plus a capability
fallback if `showModal` is unavailable. Gear/footer buttons open sheets;
submissions go through the existing bridge actions only.

**Acceptance criteria:**
- [ ] Three sheets open/close via keyboard and pointer; focus restored to trigger
- [ ] Token field clears on close; empty/invalid token shows field error, never sends
- [ ] Activity lists `vm.history` entries with timestamps

**Verification:**
- [ ] Browser fixture: Tab/Esc walk through each sheet; wire-contract green
- [ ] `uv run pytest` green

**Dependencies:** Tasks 4, 6
**Files:** `web/sheets.js`, `web/panel.js` (wiring)
**Estimated scope:** M

## Checkpoint B: full panel in fixture + hosted
- [ ] Both themes faithful to the artboards; sheets work; **human review of derived light theme**

## Task 8: states, keyboard, reduced motion, icon fidelity

**Description:** All eight artboard states in fixture (healthy,
preview-selected, stale/offline, api-key, needs-login, empty roster, long
identities, many accounts), full keyboard operation, reduced-motion
verification, icon fidelity pass against the artboard.

**Acceptance criteria:**
- [ ] Every state renders per spec rules (text+icon status, honest stale, empty CTA)
- [ ] Tab order sensible; Enter/Esc operate sheets; focus visible
- [ ] Reduced-motion honored (no essential animation)

**Verification:**
- [ ] Browser fixture with synthetic vms for each state; visual review

**Dependencies:** Task 7
**Files:** `web/*` as needed
**Estimated scope:** M

## Task 9: verification sweep + docs

**Description:** Vision-gated screenshots (dark + derived light) at final
rendering, live hosted app run (real snapshot, popover opens, right-click
menu intact), README screenshot refresh + copy tweak, full suite.

**Acceptance criteria:**
- [ ] Screenshots pass vision gate both themes; README shows the redesign
- [ ] Live app clean run; right-click fallback untouched and working
- [ ] `uv run pytest` fully green

**Verification:**
- [ ] Browser + live app + suite; SPEC success criteria 1–7 walked

**Dependencies:** Task 8
**Files:** `assets/menubar-panel-*.png`, `README.md`
**Estimated scope:** S-M

## Checkpoint C: complete
- [ ] SPEC success criteria 1–7 verified; Definition of Done satisfied
