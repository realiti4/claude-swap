# Implementation notes — deltas from the 15-board baseline

Status: implemented on `dev` (0.28.0-dev, commits b33d6a1…4f12903,
2026-09-12). This documents where the shipped UI deliberately differs
from boards 01–15 so the design assets can be updated to match. Where a
change contradicts an earlier rule in HANDOFF.md, the change was
requested directly by the product owner during pre-review and wins.

Shipped-state references (360 × 560, account 2 selected):
[implemented-main-dark.png](implemented-main-dark.png) ·
[implemented-main-light.png](implemented-main-light.png).

## Visual deltas to fold into the boards

### Account card (boards 12/13)
- **Heading style unified**: "Account" uses the same heading treatment
  as "Usage" (13 px semibold, `.section-h`). There is **no horizontal
  divider** under the Account title — the rows start directly below.
- **Single-line rows with ellipsis + hover**: every card value (Alias,
  Email, Team, Account Index) renders on one line; long values
  ellipsize instead of wrapping. The **full value is shown in a native
  tooltip on hover** (`title` attribute). This supersedes the earlier
  "values wrap and remain selectable" rule.
- Alias row keeps inline affordances beside the value: pencil **Edit**
  icon button when set, **Add alias** text button when unset. Buttons
  never clip.
- Missing values read "Not available" (email/team) / "Not set" (alias).

### Usage card (boards 01/02)
- Percentages render as **"68% USED"** — the percent sign is explicit
  in every meter, including per-model rows.

### Frame and vertical rhythm (all boards)
- **Full-height layout, no scrolling** in the default state: the panel
  fills its host viewport end to end; header and footer stay pinned and
  the body flexes to the remaining height (no fixed 486 px body).
- The vertical rhythm was compressed so the default three-account view
  fits 360 × 560 with **zero overflow** in both themes: account tabs on
  a **single row** (index tabs compress to ~78 px minimum; Add tab is
  the 4th cell), tighter card/row padding throughout.
- Overflow states (many accounts, opened disclosures) still scroll
  rather than compressing type — boards depicting scroll positions
  should assume the compressed baseline above the fold.

### Action row (boards 01/03)
- Switch button: **icon-led, index-only label** — "⇄ Switch to 2" with
  the Interlock mark, consistent with Best/Rotate icon-led styling.
  No parenthesized name in the label; the full target ("Switch to 2 ·
  research") is in the tooltip. Disabled active state unchanged
  ("✓ Current account").

### Selector (boards 01/03)
- Status vocabulary on tabs: **Ready / Active / Disabled / API key /
  Needs login / Unavailable** ("Disabled" replaces the earlier "Held
  out"). Active = teal "Active" status word; selected = teal ring +
  teal index; disabled = dimmed with strikethrough alias.
- Accessible name per tab: "2 research · Ready" (full alias, never the
  ellipsized text).

### Empty roster (board 05) — implemented as drawn
- Centered card, circled plus, "No accounts yet", body copy, and both
  entry points ("Use current Claude Code login", "Setup with token").
  The tab strip is absent entirely.

### Settings (boards 09/10)
- Appearance / Refresh interval / Title percentage are **segmented
  radiogroups** (the dropdown is gone), showing the stored selection;
  per-control help text; "Done" closes. Selections re-read stored
  values every time the sheet opens.

### Branding (all boards)
- Header: "Claude Code Swap" with the **Interlock mark at 16 px,
  currentColor** (teal accent in both themes).
- Native status item: Interlock **template image** from the 18 × 18 pt
  vector PDF (PNG pair as raster fallback); tooltip and accessibility
  description "Claude Code Swap"; title-percentage text unchanged.

## New surfaces with no board yet (draw freely)

- **Alias editor dialog** (board 14/15 exist; shipped details): title
  "Edit alias"/"Add alias"; context line "Account 2 ·
  alexandra.research@example.com"; labeled input with inline field
  error; buttons — **Remove alias** (quiet/danger, left), Cancel,
  **Save** (primary, right); input retains text on failure; Remove
  appears only when an alias is set.
- Rename happens in place: selector tab, card rows, and the native menu
  update immediately (no usage refresh); renaming never switches
  accounts or touches credentials.

## Unchanged

Bridge protocol apart from `setAlias`/`unsetAlias` and the extended
`getPrefs`; credential/lock/polling/auto-switch/launchd contracts;
CLI/TUI; the graphite/ivory palettes and tokens; all quota-state rules
(stale, awaiting-updated-usage, no fabricated zeros).
