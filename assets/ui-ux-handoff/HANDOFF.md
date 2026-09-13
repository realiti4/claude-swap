# Claude Code Swap — UI/UX implementation handoff

Status: baseline aligned to the coding agent’s implemented 0.28.0-dev UI.
The timeline extension remains planned design. Updated 2026-09-12.
Read [IMPLEMENTATION-NOTES.md](IMPLEMENTATION-NOTES.md) for the implementation
record and [TIMELINES.md](TIMELINES.md) for the separate extension.

## Start here

Read this document, inspect the current code and working-tree changes, then
implement the final design in the existing native menu bar / WKWebView shell.
Use [the editable Pen document](../untitled.pen) and the freshly exported
[screens](screens/) in this package. This document supersedes earlier briefs
and screenshots elsewhere in `assets/` where they disagree.

The project already has a partial implementation. Extend it; do not replace
the application, introduce a frontend framework, or rebuild the account engine.
The user's final naming and behavior requirements below take precedence over
incidental example content in a screenshot.

## Final user requirements

| Element | Required result |
| --- | --- |
| App header | **Claude Code Swap**, with the Interlock logo |
| Account section heading | **Account** |
| Account rows | Alias, Email, Team, **Account Index** |
| Account Index value | Just the slot number, e.g. **2**, never “Account 2” in this row |
| Usage section | Heading **Usage** above **Five-hour** and **Weekly** meters |
| Selector button | Large stable account index on the left, spanning two lines; alias and status on separate single lines to the right |
| Alias editing | Add/Edit alias, Save, Cancel, Remove alias, useful errors |
| Appearance | System, Light, Dark; default System; persistent selection |
| Terminology | No workspace concept. Use aliases such as `work`, `research`, `backup` |

Brand naming applies to the visible panel title and accessible app labels.
Do not rename the Python package, CLI commands, config paths, launch-agent IDs,
or file names merely to match the visible product title.

## Asset inventory and authority

| Asset | Usage |
| --- | --- |
| `../untitled.pen` | Editable source, 20 artboards (01–15 implemented baseline, 16–20 planned extension), named layers and hidden states |
| `screens/01-main-dark.png`, `02-main-light.png` | Main panel, latest labels and logo |
| `screens/03-account-preview.png` | Selected account differs from active account |
| `screens/04-stale-offline.png` | Last-known usage and availability |
| `screens/05-no-accounts.png` | Empty roster and account setup entry points |
| `screens/06-add-token.png`, `07-remove-account.png` | Credential setup and removal confirmation |
| `screens/08-tokens-notes.png` | Design system and interaction notes |
| `screens/09-settings-dark.png`, `10-settings-light.png` | Appearance and display settings |
| `screens/11-icon-system.png` | Logo candidates, recommended Interlock, sizing and usage |
| `screens/12-account-dark.png`, `13-account-light.png` | Expanded account card and long identity examples |
| `screens/14-edit-alias-dark.png`, `15-edit-alias-light.png` | Alias editor |
| `icons/swap-mark.svg` | Exact Pen paths, `currentColor`, 24-unit viewBox; panel branding |
| `icons/swap-template.svg` | Exact same paths in black, nominal 18px; portable template source |
| `icons/icon-template.pdf` | 18 × 18 vector PDF, no raster image objects; native template candidate |
| `icons/icon-template-18.png`, `icon-template-36.png` | Black on transparent RGBA, 1x and 2x representations for an 18pt status image |
| `icons/icon-master-256.png` | Larger transparent branding mask / fallback |
| `source/icon-master-pen-export.html` | Original Pen HTML export proving SVG path provenance; reference only |
| `tokens.css` | Hand-authored implementation seed; reconcile against Pen and existing panel CSS |
| `manifest.json` | File inventory, dimensions and SHA-256 checksums for exports |

All 13 UI screens are 360 × 560. Boards 8 and 11 are larger reference sheets.
PNG screens are visual references, not images to embed as the interface.
SVG geometry was extracted from the three co-located paths in Pen's master
HTML export; no tracing or shape approximation was used. The source HTML uses
Tailwind's CDN, but neither the SVGs nor the implementation should depend on it.

Utility icons already live in `web/icons.js`; reuse that system for refresh,
settings, edit, close, etc. Replace only its old swap-brand glyph with Interlock.
Do not ship exploration candidates B/C. The app now bundles Inter variable and
IBM Plex Mono400/600 in `web/fonts/`; use those @font-face declarations. See
[FONT-RENDERING.md](FONT-RENDERING.md) for the distinction between Pen family
selection and exact bundled-byte runtime rendering.

## Layout and appearance

Keep the 360 × 560 popover, 44px header and 30px footer. The body flexes to
fill the remaining viewport; do not hard-code its height. The default three
accounts plus Add occupy one four-cell row (78px minimum tabs, 5px gap).
Compact card/row padding keeps Account, Usage, Switch/Best/Rotate and Auto-switch
visible without default scrolling. Switch/Best/Rotate share one horizontal row,
with 84px Best and Rotate buttons and 6px gaps. Auto-switch uses heading and
summary on two lines beside the toggle. Footer retains Activity and Add account.
More accounts and opened disclosures may
scroll. Header/footer remain pinned, and refresh preserves scroll/disclosures.

Use graphite `#151A1C` and ivory `#F7F6F2` surfaces, restrained teal, fine borders,
existing spacing rhythm and rounded cards. Treat `tokens.css` as a starting
point, not a complete machine export. The exact final combinations, including
secondary text and focus treatments, must be checked against board 8 and the
rendered UI. Validate contrast rather than assuming the token seed guarantees it.

The alias remains a single line in a selector, with a real ellipsis when too
long. Keep its full value in the accessible name and the Account card’s hover tooltip.
Indices are stable slot identifiers, not positions after sorting. Allow wider
indices without clipping; board 8 includes a two-digit example. Preserve the
existing Add account entry point and support more than one row of accounts.

The Account card has a fixed section heading plus Active/Preview status. Use
labeled Alias, Email, Team, and Account Index rows. Values stay on one line,
ellipsize, and expose the full value in a native hover tooltip (`title`).
Account uses the same 13px semibold heading as Usage, without a divider below
the heading. Keep the transparent 11px Alias pencil icon or Add alias text affordance
unclipped; the pencil tooltip/accessibility label is Edit alias.
Missing email/team
values read “Not available”; a missing alias reads “Not set” with Add alias.
The selector's fallback is the existing account label. Do not replace every
missing alias with a fabricated name. Team uses the available `org` display
value; do not invent another organization lookup for this UI change.

Selection only previews an account. Only the explicit Switch action activates
it. Selected appearance and Active status must be visually and semantically
distinct. Do not infer account health from whether it is selected.

## Alias editing behavior

Open a dialog from Add alias/Edit on the selected account's Alias row. Show the
stable index and email as context; prefill the existing alias. Keep the target
slot captured while the dialog is open so refreshes cannot retarget the save.

Save calls the existing core alias API via a narrow bridge action. It must
trim/lowercase using `normalize_alias`, reject invalid/duplicate values, retain
input on failure, and surface the backend's useful error message inline. The
rules are letters/digits/dot/hyphen/underscore; nonempty, not purely numeric,
and no leading hyphen. Do not invent a new length limit or validation policy.

Disable duplicate submission while saving. A successful reply returns the
normalized alias and updates both the selector and Account card promptly.
Cancel closes without mutation. Remove alias calls the core unset method,
then displays the fallback account label; it must not delete an account.
Alias changes never switch accounts, write active credentials, or restart a
session. Keep active-slot identity and other accounts unchanged.

The alias dialog title is Edit alias or Add alias; the context includes account
index and email. Buttons are Remove alias (only if set), Cancel and Save in one footer row.
Settings and alias dialogs follow the implemented centered 300px container
with 16px padding and 12px radius; Settings Done uses quiet/ghost styling.
The Pen file includes hidden duplicate/invalid/saving and unset-alias variants.
They are behavior references; the default screenshots show normal states.

## Settings behavior

Match the System / Light / Dark selector in the Settings designs. The stored
value is `system`, `light`, or `dark`; legacy/invalid values fall back to System.
Apply immediately to the panel and sheets, persist across reopening/relaunch,
and restore the previous saved appearance on save failure. System follows live
OS changes; explicit Light/Dark override them. Keep URL theme overrides limited
to fixture mode. Existing `appearance.js` already implements most persistence
and rollback behavior. All Settings controls are now segmented radiogroups.

Refresh interval offers 30s / 60s / 5 min; Title percentage offers Off /
5-hour / Weekly / Both. Include per-control help and Done to close. Re-read
stored preferences on every open. Show actual current
preferences rather than hard-coding the selected example from the screenshot.
The selected-state semantics and persistence requirements apply to these controls
as well. Appearance controls the panel; the native template status image follows
macOS appearance and selection rendering.

## Other states and interaction requirements

- Usage meters always include an explicit percent sign, e.g. **68% USED**,
  including per-model values. Usage bars and percentages mean **used**, not remaining. Missing data is
  unavailable, not zero. Keep last-known values visibly stale.
- A reset countdown reaching zero becomes “Awaiting updated usage” until a new
  measurement arrives. Pace applies only to weekly/per-model weekly windows;
  hide pace for stale or unavailable measurements.
- API-key accounts show “No subscription quota”; they are not failed logins.
  Disabled accounts stay distinct from authentication and availability errors.
- Switch uses the Interlock icon and index-only label **Switch to 2**; its
  tooltip includes the full alias. The active state is disabled **Current account**.
- Tab statuses are Ready / Active / Disabled / API key / Needs login / Unavailable.
  Disabled aliases are dimmed and struck through; selected ring/index and Active
  status are distinct. Tab accessible names retain the full alias.
- Best, Rotate, explicit Switch, auto-switch and management actions remain
  available. The offline example does not create a new auto-pause policy.
- Setup-token entry takes token plus optional email, conceals the token and
  clears it on close. Credential storage copy stays neutral and accurate.
- Sheets have dialog labels, contained keyboard focus, safe Escape handling,
  and focus restoration. Inputs and icon buttons have accessible labels.
- Use semantic buttons and selection states; do not nest Edit inside a selector
  button. Support keyboard navigation and reduced motion. Do not rely on color
  alone. Escape all user-supplied identity strings in HTML.
- Busy/error states must reflect real backend outcomes. Do not show success
  before a save or account operation completes. Preserve user input on failure.

## Current code and integration map

Paths below are relative to the repository root. Recheck the working tree first;
other work may land while this handoff is being implemented.

| File | Current state / implementation work |
| --- | --- |
| `src/claude_swap/menubar/web/panel.js` | Implemented indexed selector, single-line Account values, explicit percent meters and index-only switch labels. |
| `src/claude_swap/menubar/web/panel.css` | Implemented flexible viewport layout, compressed cards, ellipsis and themes. |
| `src/claude_swap/menubar/web/index.html` | Alias dialog and three segmented Settings groups. |
| `src/claude_swap/menubar/web/sheets.js` | Dialog lifecycle, alias operations, errors and Settings persistence. |
| `src/claude_swap/menubar/web/appearance.js` | Theme application and system-mode handling. |
| `src/claude_swap/menubar/web/icons.js` | Interlock branding and utility icon system. |
| `src/claude_swap/menubar/app.py` | Native shell, preference/alias bridge handlers and template image. Timeline native placement is separate planned work. |
| `src/claude_swap/menubar/bridge.py` | Bridge validation and error replies; preserve protocol. |
| `src/claude_swap/menubar/viewmodel.py` | Account identity, status and quota measurements. |
| `src/claude_swap/switcher.py`, `models.py` | Core alias mutation and normalization authority. |

Alias bridge actions `setAlias` / `unsetAlias` are implemented. Rename updates
selector, card and native menu in place, without a remote usage refresh,
credential changes or account activation. Preserve these semantics.

Native logo: load the PDF or paired PNG representations, set nominal size to
18 × 18 points and mark the image as a template. A 36px file is the 2x raster
representation, not a 36pt status item. Keep transparent padding. For the panel,
inline `swap-mark.svg` at 16px with currentColor (or use a CSS mask); an external SVG
`img` does not inherit the parent element's text color automatically.

## Maintenance sequence

1. Inspect git status, implementation notes and current source before changing a
   surface. Preserve unrelated work and verify whether it is already implemented.
2. For an intentional visible product change, update its implementation record,
   corresponding Pen boards, exported previews and written behavior together.
3. Compare 360 × 560 dark/light product fixtures, including long data and errors;
   update manifest hashes and the verification record after exporting.
4. Keep future features such as timelines explicitly marked planned until their
   native and browser acceptance checks pass. Do not restore old design behavior
   over a documented product decision just to match an obsolete screenshot.
5. Run appropriate implementation tests only when changing application behavior.

## Acceptance checks

- Both themes match final screenshots at 360 × 560; Account and Usage headings
  are visible; no workspace wording or duplicate Account value remains.
- Long aliases, emails, team values, many accounts and multi-digit indices fit;
  selector and Account value ellipsis is deliberate; native tooltips expose full
  Account values and alias controls remain visible.
- Default three-account actions and Auto-switch fit without scrolling; overflow
  actions remain reachable; header/footer stay pinned; refresh
  preserves scroll and disclosure state.
- Select another account without switching it. Active badge remains correct.
- Save an alias, normalize mixed case, reject invalid/duplicate aliases, cancel,
  remove, simulate storage failure and rename while a background refresh occurs.
  Selector, details and native labels reflect success; credentials do not change.
- Persist all three theme choices across relaunch; test system changes and failed
  saves. Keyboard selection and focus recovery work in all sheets.
- Test empty roster, API-key account, needs-login, disabled, stale/offline,
  missing optional model/spend data, and expired reset countdowns.
- Verify transparent native glyph at 1x/2x and system light/dark/selected states.
  Confirm no boxed background and no unintended color in the status template.
- No console errors, escaped identity strings, no token leakage; inspect the
  installed wheel/package for every new JS and image asset.

Suggested validation commands (run from repository root):

```sh
node --check src/claude_swap/menubar/web/panel.js
node --check src/claude_swap/menubar/web/sheets.js
.venv/bin/python -m pytest tests/test_menubar.py tests/test_menubar_bridge.py tests/test_menubar_viewmodel.py tests/test_menubar_appearance.py tests/test_menubar_import_smoke.py -q
.venv/bin/python -m pytest -n 4 -q
```

Add meaningful alias bridge/UI tests. The design export step does not substitute
for implementation tests or native verification. Do not modify credential lock
ordering, usage polling cadence, auto-switch policy, CLI/TUI behavior, or launchd
contracts to accommodate visual changes.
