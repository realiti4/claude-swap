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

## Design reconciliation — 2026-09-12

Reviewed this implementation record, its dark/light product screenshots and
current panel.js/panel.css/index.html at repository HEAD `26c0580`. Updated
the editable Pen document and refreshed all20 exported boards. The main
layout, single-line identity rules, percentages, actions, statuses, branding,
Settings controls and alias dialogs have been reconciled. Boards16–20 retain
the planned timeline feature while using the implemented main-panel layout.

HANDOFF.md now reflects implemented alias/settings/native-logo integration
rather than asking a future agent to reimplement it. TIMELINES.md keeps the
full-window chart behavior separate. START-HERE.md includes the recurring
product/design review workflow; tokens.css no longer prescribes fixed body
height. Sample identities, quota values and optional disclosure content vary
between state boards and product fixtures; these are illustrative data, not
new backend behavior. The product screenshots remain the implementation
record, and Pen exports are design references rather than pixel-identical
runtime captures. Future visible changes must repeat this reconciliation.


## Reset timelines wave — 2026-09-12

Implemented on `dev` (commits `799bfa5..b3735bd`, v0.29.0 unchanged).
Entry point: `next-wave/COMPLETION-REPORT.md`. Deltas the design owner
should fold into the Pen source and re-export:

1. **Boards 01-15**: the main-panel headers now carry the timelines
   trigger (calendar-clock icon button before Refresh); re-export under
   the bundled Inter/IBM Plex Mono rendering (see 2).
2. **Fonts ship with the app now** (Inter variable + Plex Mono 400/600
   latin WOFF2, `@font-face` shadowing host copies): on machines
   without the fonts installed, main-panel text moves *toward* the
   boards — boards remain the authority.
3. **Boards 17/18**: session-approved additions — per-chart countdown
   chips on rows, pointer crosshair with timestamp, `T` shortcut — are
   deliberate deltas awaiting board re-export; the board-exact capture
   fixture suppresses them meanwhile (`CSWAP_TL_BOARD`).
4. **Board 16**: closed state includes the trigger button.

Native strategy (T1 proof): expanded state = borderless NSPanel with
the webview re-parented; evidence in `tests/fixtures/captures/
native-proof.json` and the decision record in `next-wave/tasks/plan.md`.
The fidelity gate: zero-tolerance geometry/color/type via exact-point
layout records; glyph rasterization masked (renderer-dependent); image
budgets at the measured WebKit-vs-Pen sub-pixel floor (4%/6%).

## Limit Timelines design re-export — 2026-09-12

Completed the requested Pen reconciliation and exported boards01–18 at PNG1x.
Main-header instances carry the calendar-clock trigger before Refresh, including
modal backgrounds; the empty-account trigger is disabled. Board08 retains its
reference-sheet role; board11 has no main header and remains the icon system.
Board16 shows the closed trigger. Boards17/18 now show countdown chips in both
charts, a neutral pointer crosshair with a15:30 timestamp distinct from amber
Now, and the T shortcut note. Long row aliases use an ellipsis.

Pen source was saved through the UI. Packaged images are direct native Pen
exports. Inter and IBM Plex Mono family/weight assignments were checked in Pen,
but exact bundled WOFF2 binding is unsupported: see [FONT-RENDERING.md](FONT-RENDERING.md).
Do not describe these as verified renders of the app's exact font binaries.

Coding-agent follow-up for fidelity capture (no product code changed here):

- Reconcile CSWAP_TL_BOARD, which currently suppresses countdown/crosshair
  additions, and refresh capture fixtures/layout records before comparing
  against these new references. Prior passing image metrics cover older images.
- The chips required more detail-column room: Pen narrowed the alias column
  and shifted the242px track, shared grid and Now together. Preserve full-window
  semantics and inspect final Pen geometry when updating exact-point records.
- Static chip labels use compact illustrative durations (3h,3d,40m); the
  runtime formatter floors minutes and includes spaces/zero remainder units.
  Keep the runtime calculation authoritative when rebuilding frozen fixtures.
- The inactive calendar container has a quiet rounded background to keep the
  glyph visible in native Pen export; align the runtime control style during
  fidelity review.

Boards19/20 and Settings21–28 were outside this re-export scope and are retained.
The organized canvas and Settings wave are preserved.

## Variable panel height — owner decision 2026-09-12 (post-wave)

The main panel's height now follows its content instead of fixed 560:
the panel measures its natural height on every render and disclosure
toggle (`sizePanel` bridge action), native clamps to the screen and
resizes the popover; the expanded pair stays board-fixed at 560 unless
content is taller. A **560px floor** keeps every board-drawn state
board-exact (the default three-account rhythm fills it exactly; the
empty state and short rosters hold it); taller rosters grow the panel
and the no-scroll rule holds absolutely. Boards need no re-export —
they depict the default state at 560, which is unchanged.
