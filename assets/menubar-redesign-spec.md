# Claude Code Swap — menu bar redesign

Design brief derived from `menubar/app.py`, `menubar/viewmodel.py`,
`menubar/bridge.py`, and `menubar/web/`. The editable design is saved in
[`assets/untitled.pen`](untitled.pen), with ten artboards covering
dark and light themes, account preview, stale usage, empty accounts, token
setup, removal confirmation, Settings in both themes, and design tokens with
interaction notes.
The [main dark preview](1%20%E2%80%94%20Main%20DARK.png) was
exported from Pen and visually inspected at 360 × 560. This deliverable is a
design specification; application implementation is separate.

## Direction

A compact, refined macOS utility with graphite and warm ivory themes,
restrained teal accents, precise typography, and clearly separated account
selection and account activation. Keep the existing 360 × 560 popover.
Additional accounts, model windows, and activity can scroll or disclose.

## Information hierarchy

1. Brand header: swap icon, claude-swap, refresh, settings.
2. Account selector: alias preferred, with a distinct active indicator.
   Selection previews the account; it never activates it.
3. Account card: full alias with Edit action, email, team, account
   index, and Active or Preview status.
4. Primary quotas: five-hour and weekly usage, percentage explicitly labeled
   **used**, a slim bar, and the next reset countdown.
5. Optional model limits and spend, visually subordinate to primary quotas.
6. Explicit action: Switch to Personal, or disabled Current account.
   Secondary Best and Rotate actions stay discoverable. Account management
   lives in an overflow menu.
7. Auto-switch: labeled toggle and persistent threshold/strategy summary.
8. Footer: measurement freshness, activity access, and add-account action.

The delivered panel uses a 44px header, a 486px scrollable body, and a
30px footer. If content exceeds the body height, scroll it instead of
compressing type or hiding actions.

## Visual tokens

| Token | Dark | Light |
|---|---|---|
| Canvas | #151A1C | #F7F6F2 |
| Raised surface | #20282B | #FFFFFF |
| Primary text | #F2F5F4 | #182623 |
| Secondary text | #AAB9B5 | #526760 |
| Accent | #3FC2B2 | #0C8577 |
| Warning | #F2BF70 | #865400 |
| Error | #FFA097 | #B3372E |

The Pen artwork uses Inter for interface labels and IBM Plex Mono for
measurements. Use local system fallbacks when those fonts are unavailable;
font files are not included. Keep secondary labels at least 12px and identity
text at least 14px, with tabular numbers for quotas, resets, and spend.
The Pen artwork is authoritative for final visual values; the supporting
CSS tokens are a hand-authored implementation starting point, not an export.
Use an approximately 4px spacing rhythm, 12–16px section padding, quiet
dividers, and 10–14px corner radii. Validate actual foreground/background
pairings for contrast before implementation. Status always includes text
or an icon as well as color.

## Required design frames

- Main popover in dark and light themes.
- Alternate account selected, with an explicit switching action.
- Stale/offline state preserving the last-known usage and its age.
- Empty account roster with current-login and setup-token entry points.
- Setup-token sheet: visible labels, optional email, concealed token,
  Cancel and Add account, field-specific errors.
- Removal confirmation with account identity and a clear consequence.
- Component and interaction notes, including loading and error treatments.

Use invented examples only: Work / alex@example.com, Personal /
alex.personal@example.com, Backup / backup@example.com. Main example usage:
68% five-hour, 41% weekly, optional 84% model usage, $12.40 of $100 spend.

## State and interaction rules

- Active and selected are different states. Only an explicit switch action
  activates the selected account. Show success only after confirmation from
  the backend, and preserve useful failure messages.
- All bars show used quota. Missing values show unavailable, never 0%.
- Last-known values remain labeled stale. A local countdown reaching zero
  becomes Awaiting updated usage; it must not fabricate a zero measurement.
- API-key accounts show No subscription quota. They are not authentication
  failures. Disabled accounts are excluded from automatic rotation but can
  remain manually switchable. Needs login is reserved for an actual
  authentication problem.
- Pace indicators apply only to weekly and per-model weekly windows, never
  the five-hour window. Hide pace for unavailable or stale measurements.
- The offline artboard illustrates Auto-switch off with Usage unavailable;
  this does not introduce an automatic pause policy.
- Refresh reflects the shared store's pacing; it must not promise a new
  measurement on every click.
- Pending actions prevent duplicate submission and retain keyboard focus.
- Sheets use dialog semantics, contain focus, close on Escape where safe,
  and restore focus to their trigger. Token values clear on close.
- Toggles have accessible names and checked states. Icon buttons have
  labels. Animations respect reduced-motion preferences.
- Quota-reset labels and freshness belong to the selected measurement;
  they must not accidentally describe another account's data.

## Code seams to recheck before implementation

These observations were recorded during the initial review. Concurrent code
changes may already address them; inspect the current code before applying fixes.

- `Bridge` already offers switch, rotate, best, disable, enable, remove,
  addFromLogin, addFromToken, refresh, setAutoSwitch, setPrefs, and quit.
- The current visible panel does not expose all existing strategy handlers.
- The JavaScript reply adapter currently forwards `result.data` even for
  errors; implementation should preserve `result.error` on failure.
- The existing view-model folds all sentinel strings into `quarantined`.
  A redesign needs an additive status distinction so API keys and temporary
  availability problems are not presented as dead logins.
- Current weekly display helpers roll old windows forward and set usage to
  zero. The proposed stale state should preserve measured values instead.
- Preserve existing in-progress changes throughout the repository.

## Implementation boundaries

Keep credential writes, lock ordering, usage polling, auto-switch policy,
launchd contracts, CLI, and TUI behavior unchanged. Keep any view-model
extension additive. Do not introduce a frontend framework or build step.

Validate the design in Pen at actual size, then verify a future implementation
with browser interaction checks for both themes, long identities, many
accounts, missing optional data, stale/error states, keyboard operation,
and each action's success/failure path. Run existing menu bar tests and the
full Python suite before reporting an implementation complete.

## Settings appearance addition

Add matching Settings DARK and Settings LIGHT artboards using the existing
360 × 560 panel and sheet styling. Appearance has three mutually exclusive
choices: System (default), Light, and Dark. Helper copy: “System follows your
Mac’s appearance.” Keep refresh interval (30s, 60s, 5 min), title percentage
(Off, 5h, 7d, Both), and Done visible beneath it.

- Apply the selection immediately to the panel and all sheets.
- Persist the preference across reopening and app relaunch.
- System follows live macOS appearance changes; explicit Light/Dark override it.
- Communicate selection with an indicator and text, not color alone.
- Support keyboard selection; close returns focus to the Settings trigger.
- While saving, prevent duplicate changes. On save failure, restore the last
  saved appearance and show “Couldn’t save appearance. Please try again.”
- Keep loading/error states honest: never report a saved preference before
  persistence succeeds.

Implementation must follow these Pen Settings artboards. The initial uncommitted
code draft is paused pending this design update and is not the design authority.

## Expanded selected-account card

Artboards 12 (dark) and 13 (light) replace the compact inline identity row with
a separate full-width surface below the selector. Use an Account
heading, Active/Preview badge, and labeled Alias, Email, Team, and Account rows.
Values are at least 13px, wrap naturally (including unbroken email addresses),
and remain selectable. Labels are at least 12px. Missing email/team values read
“Not available”. Escape all account-provided content when rendering HTML.

The card grows with content; the existing panel body scrolls while the header
and footer stay fixed. Do not truncate identity values, shrink quota labels,
or remove switch actions to keep all content within the initial viewport.

## Alias editing, numbered selectors, and unified branding

The selected-account section is titled **Account**. Alias is its
own labeled row, followed by Email, Team, and Account. This product has no
workspace concept; example aliases use `work`, `research`, and `backup`.
Show the full alias in the information card, wrapping as necessary.

Each selector button has a large stable account index on the left, spanning
two text lines. To its right, show the alias on one line and status on the
next. Use the slot identifier, not the current sort position. Accommodate
two-digit indices. Only the compact selector alias may truncate. When an alias
is absent, use the existing account label in the selector and “Not set” in
the card. Active status and selected-preview styling remain distinct.

The Alias row exposes Edit alias (Add alias when absent). The editor includes
account index and email context, a labeled input, Cancel, Save alias, and a
secondary Remove alias action when an alias exists. Keep user input on failure,
show a useful inline error, prevent duplicate saves while pending, and return
focus to the trigger on close. Success updates both the selector and details;
cancel leaves values unchanged. Editing an alias never activates the account.

Reuse `ClaudeAccountSwitcher.set_alias` / `unset_alias` and `normalize_alias`:
trim whitespace and normalize to lowercase; allow letters, digits, period,
hyphen, underscore; reject empty values, purely numeric names, leading hyphens,
and duplicates. The menu-bar bridge will need explicit alias actions during
implementation; the existence of the core API does not mean the panel already
exposes it.

Use board 11's recommended **A — Interlock** vector geometry throughout every
UI artboard and sheet background. Teal belongs in panel headers and branding;
menu-bar template examples stay monochrome. Historical icon candidates remain
on board 11 as exploration, not alternative production logos.

## Final display labels

- Visible product title: **Claude Code Swap**, retaining the Interlock logo.
- Selected-account section heading: **Account**.
- Index row: label **Account Index**, value is just the slot number (for example **2**).
- Quota section heading: **Usage**, above the five-hour and weekly meters.

Apply these labels across the main panels, previews, stale states and panel
backgrounds beneath sheets. Preserve literal CLI commands and repository paths.
