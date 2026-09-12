# Claude Code Swap — UI/UX implementation handoff

Status: final design reference, ready for implementation. This handoff does not
claim the application implements the complete design. Prepared 2026-09-12.

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
| Usage section | Heading **Usage** above the five-hour and weekly meters |
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
| `../untitled.pen` | Authoritative editable source, 15 artboards, named layers and hidden states |
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
Do not ship exploration candidates B/C. No font binaries are supplied: use the
existing local Inter/system and IBM Plex Mono/system-monospace fallback stacks.

## Layout and appearance

Keep the 360 × 560 popover, 44px header, 486px body and 30px footer. The body
scrolls vertically; the header and footer remain pinned. Main screenshots show
the initial scroll position, so action rows may be below the fold. Implement
all rows and scrolling rather than deleting actions to reproduce a screenshot.
Preserve scroll position and disclosure state during background refreshes.

Use graphite `#151A1C` and ivory `#F7F6F2` surfaces, restrained teal, fine borders,
existing spacing rhythm and rounded cards. Treat `tokens.css` as a starting
point, not a complete machine export. The exact final combinations, including
secondary text and focus treatments, must be checked against board 8 and the
rendered UI. Validate contrast rather than assuming the token seed guarantees it.

The alias remains a single line in a selector, with a real ellipsis when too
long. Keep its full value in the accessible name and in the Account card.
Indices are stable slot identifiers, not positions after sorting. Allow wider
indices without clipping; board 8 includes a two-digit example. Preserve the
existing Add account entry point and support more than one row of accounts.

The Account card has a fixed section heading plus Active/Preview status. Use
labeled Alias, Email, Team, and Account Index rows. Full identity values wrap,
including unbroken email strings, and remain selectable. Missing email/team
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

The Pen file includes hidden duplicate/invalid/saving and unset-alias variants.
They are behavior references; the default screenshots show normal states.

## Settings behavior

Match the System / Light / Dark selector in the Settings designs. The stored
value is `system`, `light`, or `dark`; legacy/invalid values fall back to System.
Apply immediately to the panel and sheets, persist across reopening/relaunch,
and restore the previous saved appearance on save failure. System follows live
OS changes; explicit Light/Dark override them. Keep URL theme overrides limited
to fixture mode. Existing `appearance.js` already implements most persistence
and rollback behavior; replace its dropdown presentation carefully.

Retain refresh interval and title percentage settings. Show actual current
preferences rather than hard-coding the selected example from the screenshot.
The selected-state semantics and persistence requirements apply to these controls
as well. Appearance controls the panel; the native template status image follows
macOS appearance and selection rendering.

## Other states and interaction requirements

- Usage bars and percentages mean **used**, not remaining. Missing data is
  unavailable, not zero. Keep last-known values visibly stale.
- A reset countdown reaching zero becomes “Awaiting updated usage” until a new
  measurement arrives. Pace applies only to weekly/per-model weekly windows;
  hide pace for stale or unavailable measurements.
- API-key accounts show “No subscription quota”; they are not failed logins.
  Disabled accounts stay distinct from authentication and availability errors.
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
| `src/claude_swap/menubar/web/panel.js` | Existing selector, wrapped identity card, meters/actions and render preservation. Header still uses `claude-swap`; card heading is the alias; fixtures contain “Research workspace”. Update final labels, index tabs, Alias row/actions and Usage heading. |
| `src/claude_swap/menubar/web/panel.css` | Existing dark/light tokens, fixed body scrolling, initial expanded card. Adapt layout for numbered tabs and final Account rows; preserve long-value wrapping. |
| `src/claude_swap/menubar/web/index.html` | Existing dialogs and Appearance dropdown. Add alias dialog and match Settings segmented presentation. |
| `src/claude_swap/menubar/web/sheets.js` | Existing modal lifecycle and management actions. Extend for alias editing and correct focus/error behavior. |
| `src/claude_swap/menubar/web/appearance.js` | Existing getPrefs/setPrefs theme persistence, preview application and rollback. Preserve while changing UI. |
| `src/claude_swap/menubar/web/icons.js` | Replace old swap geometry with supplied Interlock; keep utility icons. |
| `src/claude_swap/menubar/app.py` | Native shell, settings storage, bridge handlers/specs, image setup. Theme handling exists; add alias endpoints and custom NSImage template. Package assets under the installed package, not runtime paths into `assets/`. |
| `src/claude_swap/menubar/bridge.py` | Allowlist/type checks and error replies. Extend through handler/spec configuration rather than bypassing validation. |
| `src/claude_swap/menubar/viewmodel.py` | Provides slot, alias, email, org, active/status and usage. Keep additions compatible; refresh aliases without another usage API request if possible. |
| `src/claude_swap/switcher.py` | `set_alias(identifier, alias)` returns `(slot, normalized_alias)`; `unset_alias(identifier)` returns slot. Reuse these methods. |
| `src/claude_swap/models.py` | `normalize_alias` is validation authority. |

Suggested additive bridge contracts (not implemented by this handoff):
`setAlias {slot: string, alias: string}` → `{slot, alias}` and
`unsetAlias {slot: string}` → `{slot, alias: null}`. Route exceptions through the
existing bridge error channel. Rebuild native menu labels as needed and push
updated view data so renaming does not wait for a remote usage refresh.

Native logo: load the PDF or paired PNG representations, set nominal size to
18 × 18 points and mark the image as a template. A 36px file is the 2x raster
representation, not a 36pt status item. Keep transparent padding. For the panel,
inline `swap-mark.svg` with currentColor (or use a CSS mask); an external SVG
`img` does not inherit the parent element's text color automatically.

## Implementation sequence

1. Inspect git status and preserve unrelated work. Review final Pen artboards
   and this handoff. Audit what the current branch already implements.
2. Integrate final product/section labels, Account card and numbered selectors.
3. Add alias bridge handlers and dialog using existing core APIs; test outcomes.
4. Match Settings controls while preserving saved appearance behavior.
5. Integrate Interlock into panel and native status image; verify packaged assets.
6. Exercise the complete state matrix below in a real browser and native WKWebView.
7. Run appropriate tests, report results, and make a focused implementation commit.
   Do not publish, change versions, or alter release infrastructure as part of this scope.

## Acceptance checks

- Both themes match final screenshots at 360 × 560; Account and Usage headings
  are visible; no workspace wording or duplicate Account value remains.
- Long aliases, emails, team values, many accounts and multi-digit indices fit;
  selector ellipsis is deliberate, full Account values never ellipsize.
- All below-fold actions remain reachable; header/footer stay pinned; refresh
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
