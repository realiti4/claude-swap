# Spec: Claude Code Swap final UI — numbered selector, Account card, alias editor, settings segments, Interlock

Supersedes the menubar-redesign SPEC.md (implemented and shipped in
0.28.0). Brainstorm design:
`docs/superpowers/specs/2026-09-12-claude-code-swap-final-ui-design.md`.
The consolidated handoff in `assets/ui-ux-handoff/` (HANDOFF.md, 15
screen exports, icon assets, manifest) is the authoritative design
reference; where anything here and HANDOFF.md disagree, HANDOFF.md wins.

## ASSUMPTIONS I'M MAKING

1. One release implements the full handoff; the five stages below are
   the build order, each landing green on `dev`.
2. The partial Account-card edits sitting uncommitted in the working
   tree are folded into stage 1 (fixed forward, no WIP commit).
3. Version bumps to 0.29.0 at ship time only, not during the build.
4. Selector status strings are exactly: Ready, Active, Disabled,
   API key, Needs login, Unavailable (cased as shown on the boards).
5. The title-percentage text feature (off/5h/7d/both) stays alongside
   the new Interlock status icon.
6. Native status icon uses `icon-template.pdf` (18pt template NSImage);
   the 18/36 PNG pair ships as the raster fallback if PDF rendering
   misbehaves on any macOS build.
7. `cswap` CLI, TUI, config paths, and the launchd label keep their
   names — "Claude Code Swap" is visible/accessible branding only.

## Objective

Finish the menubar UI to the final Claude Code Swap design: branded
header ("Claude Code Swap" + Interlock mark), numbered index-tab
account selector as an ARIA radiogroup, a structured Account card
(Alias / Email / Team / Account Index rows), in-panel alias editing
(the one new feature, via two narrow bridge actions reusing the core
alias APIs), segmented settings controls showing stored values, the
Interlock template icon in the native status item, and a terminology
sweep (Usage heading, no "workspace" wording). The per-account usage
mini-bars in the selector are dropped — usage lives in the selected
account's Usage section.

Success looks like: both themes match the boards at 360×560, every
HANDOFF acceptance check passes, the full pytest suite stays green,
and nothing outside the menubar web/shell layer changes behavior.

## User Stories

1. **Stable index recognition** — As a multi-account user, I select
   accounts by their stable slot number shown large on each tab, so
   ordering changes never confuse me about which account is which.
2. **Aliases without a terminal** — As a desktop user, I add, edit,
   and remove aliases in the panel, so I never drop to `cswap alias`
   for renaming.
3. **Branded, trustworthy identity** — As a user, I see "Claude Code
   Swap" with the Interlock mark in the panel and a crisp native
   menubar glyph, so the app feels like a maintained product.
4. **Honest settings** — As a user, the settings sheet shows my
   actual stored appearance, refresh interval, and title percentage,
   so nothing claims a value that isn't saved.
5. **Full identity visibility** — As a user with long work emails and
   team names, values wrap and stay selectable in the Account card,
   so I can read and copy them without truncation lies.
6. **Alias guardrails** — As a user, invalid or duplicate aliases are
   rejected with the backend's real message inline, my input is
   retained, and normalization (trim, lowercase) is visible in the
   saved result.
7. **Rename safety** — As a user, renaming an alias never switches
   accounts, touches credentials, or disturbs other accounts — even
   while a background refresh is in flight.
8. **Live rename feedback** — As a user, a saved alias updates the
   selector, Account card, and native menu immediately, without
   waiting for a usage refresh.
9. **Native appearance integration** — As a user, the menubar icon
   follows macOS light/dark and selection rendering via the template
   image, and my appearance choice persists across relaunch.
10. **Accessible operation** — As a keyboard user, I can operate the
    selector radiogroup, every sheet, and every control without a
    pointer, with focus contained and restored.

## Tech Stack

Unchanged: Python 3.14 (uv-managed, non-framework), PyObjC
(Cocoa/WebKit), vanilla JS/CSS/HTML classic scripts in WKWebView —
no frontend framework, no build step. pytest + pytest-xdist; Node
`vm` for JS behavior tests; fixture-browser + native runs for visual
verification. New runtime assets: vector icon files packaged inside
the package at `src/claude_swap/menubar/assets/`.

## Commands

```bash
# full suite (parallel, as CI runs it)
uv run pytest -n 4 -q

# focused menubar suites while iterating
uv run pytest tests/test_menubar.py tests/test_menubar_bridge.py \
  tests/test_menubar_viewmodel.py tests/test_menubar_appearance.py \
  tests/test_menubar_wire_contract.py -q

# JS syntax gate
node --check src/claude_swap/menubar/web/panel.js
node --check src/claude_swap/menubar/web/sheets.js
node --check src/claude_swap/menubar/web/appearance.js

# fixture browser (no-store server; fresh ?doc= URL per session)
python3 -m http.server 8765 --bind 127.0.0.1 \
  --directory src/claude_swap/menubar/web
#   open http://127.0.0.1:8765/index.html?fixture&theme=dark&doc=<n>

# native run from the repo (eyeball checks)
PYTHONPATH=src uv run python -m claude_swap menubar

# refresh the installed service after code changes
uv tool install --force --with pyobjc-framework-Cocoa \
  --with pyobjc-framework-WebKit \
  '/Users/honeybadgerxai/Library/Mobile Documents/com~apple~CloudDocs/Developer/Hung/Agents/Code/claude-swap'
cswap menubar --install-service
```

## Project Structure

```
src/claude_swap/menubar/web/    panel.js, sheets.js, appearance.js,
                                icons.js, index.html, panel.css
src/claude_swap/menubar/assets/ NEW: packaged native icon files
                                (icon-template.pdf, -18/-36.png);
                                panel SVG is inlined, not a file read
src/claude_swap/menubar/        app.py (shell/bridge handlers),
                                bridge.py, viewmodel.py
src/claude_swap/                switcher.py (set_alias/unset_alias
                                reused as-is), models.py
                                (normalize_alias, authority)
tests/test_menubar*.py          unit / bridge / wire-contract /
                                DOM-guard / node-vm behavior tests
assets/ui-ux-handoff/           design reference only, never runtime
tasks/                          plan.md + todo.md (from /plan)
```

## Code Style

Same conventions as 0.28.0, shown by example — escape every
user-supplied string, absent-not-null optional fields, spec-driven
bridge payloads:

```js
// panel.js — identity card rows: esc() everywhere, wrap anywhere
<dt>Account Index</dt><dd>${esc(acct.slot || "Not available")}</dd>
```

```python
# app.py — payload specs stay the contract; unknown keys stripped
"setAlias": {"required": {"slot": str, "alias": str}},
"unsetAlias": {"required": {"slot": str}},
```

JS: classic scripts (no modules), `ic()` for icons in panel.js,
state/render split with scroll+disclosure preservation, explicit
Enter/Space activation, double-submit guards, `showModal()` with
`show()` fallback. Python: closure-style shell, additive viewmodel
fields, `atomic_write_json` for settings.

## Testing Strategy

- **TDD per stage** — bridge contract tests for `setAlias`/`unsetAlias`
  (specs, normalization, duplicate/invalid errors, slot captured
  against refresh, no credential writes), viewmodel always-present
  card fields + status strings, wire-contract scan extended to the
  alias sheet's sends, DOM selector guards for the new structure.
- **Node vm behavior tests** — alias sheet lifecycle and segmented
  appearance/settings controls, appearance-style.
- **Fixture browser** — full state matrix: both themes, empty roster,
  API-key, needs-login, disabled, stale, expired countdown, missing
  email/team/org, long identities, 10+ accounts (multi-row,
  two-digit indices), alias save/normalize/reject/duplicate/cancel/
  remove, rename during background refresh.
- **Native WKWebView + eyeball** — template icon 1x/2x in light/dark
  menubars with selection rendering, alias round-trip, settings
  persistence across relaunch, no console errors.
- **Packaging test** — installed wheel/package contains every new JS
  and image asset.
- Full suite green (`uv run pytest -n 4 -q`) before each stage commit.

## Boundaries

- **Always:** run the suite before each stage commit; `esc()` every
  user-supplied string; additive JSON schema (optional fields absent,
  never null); `node --check` all touched JS; preserve scroll and
  disclosure state across re-renders; explicit keyboard activation;
  update this spec when decisions change.
- **Ask first:** new dependencies; the version bump; bridge protocol
  changes beyond the two alias actions; anything beyond calling the
  existing `set_alias`/`unset_alias`/`normalize_alias` APIs; packaging
  changes beyond adding `menubar/assets/`.
- **Never:** touch the credential write path, lock ordering, usage
  polling cadence, auto-switch policy, CLI/TUI behavior, or launchd
  contracts; push or open PRs (dev-only); "workspace" terminology; a
  frontend framework or build step; renaming the package/CLI/config
  paths/launchd label; embedding screenshot PNGs as the interface.

## Success Criteria

1. Both themes match boards 01/02 at 360×560; "Account" and "Usage"
   headings present; no workspace wording; Account Index shows the
   bare number.
2. Long alias/email/team values fit; only the selector alias
   ellipsizes (deliberately); card values wrap and stay selectable;
   multi-digit indices don't clip; 10+ accounts wrap to rows.
3. Below-fold actions reachable; header/footer pinned; background
   refresh preserves scroll and disclosure.
4. Selecting an account previews it; the Active badge stays correct;
   switching remains explicit.
5. Alias flows all pass: save, mixed-case normalization, invalid and
   duplicate rejection with inline backend messages, cancel, remove
   (falls back to label, never deletes), failure retains input,
   rename during refresh targets the captured slot; selector, card,
   and native menu update immediately; tests assert no credential
   writes and no account switch.
6. System/Light/Dark persist across relaunch; System follows live OS
   changes; failed saves roll back; keyboard operation and focus
   recovery work in every sheet.
7. State matrix passes in fixture and native runs: empty roster,
   API-key, needs-login, disabled, stale/offline, missing optional
   model/spend data, expired countdowns.
8. Native template glyph verified at 1x/2x in light/dark/selected
   states, no boxed background, no stray color; packaged-asset test
   green.
9. No console errors; identity strings escaped; no token leakage.
10. Full suite green; five stages committed on `dev` in order.

## Open Questions

None blocking. Defaults if not revisited: status strings as assumed
above; PDF icon first with PNG fallback; title-percentage text keeps
its current behavior next to the new icon.
