# Claude Code Swap final UI — task list

Plan: `tasks/plan.md` · Spec: `SPEC.md` · Design:
`docs/superpowers/specs/2026-09-12-claude-code-swap-final-ui-design.md`.
Definition of Done applies to every task on top of its own acceptance
criteria.

## Phase 1: Account card & labels

- [x] Task 1: Final Account card + section labels
  - Acceptance: card shows "Account" heading + Active/Preview badge; labeled Alias ("Not set" + Add alias, or value + Edit), Email, Team, Account Index (bare number) rows; "Usage" heading above meters; missing email/team read "Not available"; values wrap anywhere and stay selectable; fixtures use work/research/backup with long org/email examples, no "workspace" wording; every string esc()'d
  - Verify: `uv run pytest tests/test_menubar.py tests/test_menubar_wire_contract.py -q`; fixture render of both themes matches board 12; `node --check` panel.js
  - Files: src/claude_swap/menubar/web/panel.js, panel.css, tests/test_menubar.py
  - Dependencies: none (folds in the uncommitted WIP)

- [x] Task 2: View-model card-field contract pin
  - Acceptance: viewmodel tests pin slot/email/org always present and alias absent-when-unset (never null) for the card rows
  - Verify: `uv run pytest tests/test_menubar_viewmodel.py -q`
  - Files: tests/test_menubar_viewmodel.py (viewmodel.py only if a gap surfaces)
  - Dependencies: none

## Checkpoint A
- [ ] Focused suites green; both themes render the board-12 card; no workspace wording anywhere

## Phase 2: Numbered selector

- [x] Task 3: Index-tab selector with radiogroup semantics
  - Acceptance: each tab shows the large stable index spanning two lines with alias + status text right; status strings Ready/Active/Disabled/API key/Needs login/Unavailable; one ARIA radiogroup of radios with explicit Enter/Space activation; grid wraps to multiple rows; two-digit indices don't clip; long alias ellipsizes with full value in accessible name; selected (teal ring) ≠ active (mark) ≠ disabled visuals; per-account usage mini-bars removed; click-to-preview preserved; scroll/disclosure preservation intact
  - Verify: DOM-guard tests + fixture matrix (3 accounts, 10+ accounts multi-row, empty roster, each status state, long alias); `node --check`
  - Files: panel.js, panel.css, tests (DOM guards)
  - Dependencies: Task 1 (status vocabulary renders against the final card)

## Checkpoint B
- [x] Selector fixture matrix passes; selection preview and Active badge verified against board 03

## Phase 3: Alias editor

- [x] Task 4: Bridge setAlias/unsetAlias + snapshot push
  - Acceptance: `setAlias {slot, alias}` → `{slot, alias}` and `unsetAlias {slot}` → `{slot, alias: null}` registered inside the `_panel_handlers` block with payload specs; handlers call switcher.set_alias/unset_alias with normalize_alias as authority; errors flow through the bridge error channel; success pushes an updated snapshot (no usage round-trip) and rebuilds native menu labels; tests cover normalization (trim/lowercase), duplicate, invalid, purely-numeric, leading-hyphen, unknown slot, and assert no credential writes and no account switch
  - Verify: `uv run pytest tests/test_menubar.py tests/test_menubar_bridge.py tests/test_menubar_wire_contract.py -q`
  - Files: src/claude_swap/menubar/app.py, tests
  - Dependencies: none (parallel-safe with tasks 1–3)

- [x] Task 5: Alias sheet UI + card Add/Edit/Remove
  - Acceptance: `dlg-alias` opens from the card's Add alias / Edit with the target slot captured at open (refreshes can't retarget); context row shows index + email; input prefilled; inline error shows the backend's real message and retains input; Save double-submit-guarded; Cancel closes without mutation; Remove alias appears when set, unsets and falls back to the account label (never deletes); focus contained, Escape safe, focus restored, state cleared on close; wire-contract send-scan covers the new literals; node-vm lifecycle test
  - Verify: wire-contract + node-vm tests green; fixture: save/normalize/reject/duplicate/cancel/remove + rename during a background refresh
  - Files: index.html, sheets.js, panel.js, panel.css, tests
  - Dependencies: Tasks 1, 4

## Checkpoint C
- [x] Alias end-to-end green in fixture; selector, card, and (native) menu labels update immediately

## Phase 4: Settings segments

- [ ] Task 6: Segmented settings + extended getPrefs
  - Acceptance: getPrefs reply adds refreshInterval and titlePct (additive); Appearance/Refresh/Title% render as segmented controls showing the stored value on open; appearance.js keeps persistence + rollback (extended to all three controls); System follows live OS changes; failed save rolls back; help texts per boards; dropdown removed
  - Verify: bridge getPrefs test; node-vm segmented behavior tests (current selection, save, rollback); native relaunch persistence check at checkpoint
  - Files: app.py, appearance.js, index.html, panel.css, tests/test_menubar_appearance.py
  - Dependencies: none (parallel-safe after Checkpoint A)

## Checkpoint D
- [ ] Settings show stored values; all three choices persist across native relaunch; rollback verified

## Phase 5: Interlock branding

- [ ] Task 7: Panel header rebrand + icons.js Interlock
  - Acceptance: header reads "Claude Code Swap" with the Interlock mark inlined as currentColor SVG (both themes); icons.js swap glyph geometry replaced by the Interlock paths with all `ic()` consumers intact; no other utility icons changed
  - Verify: fixture render both themes; icons wire/DOM guards; `node --check`
  - Files: index.html (or panel.js), icons.js, tests
  - Dependencies: none

- [ ] Task 8: Native template icon + packaged assets
  - Acceptance: icon-template.pdf/-18/-36.png copied into src/claude_swap/menubar/assets/; status item uses template NSImage (18×18pt nominal, setTemplate_) replacing the SF Symbol, with PNG fallback if the PDF fails to load; accessibility description "Claude Code Swap"; title-percentage text feature unchanged; packaging test asserts the assets ship in the built wheel
  - Verify: packaging test; native run — icon at 1x/2x in light/dark menubar with selection rendering, no boxed background, no stray color
  - Files: src/claude_swap/menubar/assets/* (new), app.py, tests
  - Dependencies: Task 7 (shared icon source)

## Checkpoint E
- [ ] Full suite `uv run pytest -n 4 -q` green
- [ ] Full state matrix in fixture (both themes, empty roster, API-key, needs-login, disabled, stale, expired countdown, missing optional data, long identities, 10+ accounts)
- [ ] HANDOFF.md acceptance checklist walked end-to-end; fixture screenshots + native eyeball list to user
- [ ] Ready for /review
