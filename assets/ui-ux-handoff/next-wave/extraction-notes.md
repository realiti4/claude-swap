# Timeline token extraction — Pen source → panel.css

Task 2 evidence (2026-09-12). Source of truth: `assets/untitled.pen`
(boards 16–20). Method: JSON-parse the Pen document; every value below is
machine-read from node properties, not eyeballed. Encoded as `--swap-tl-*`
in `panel.css` (all four theme contexts). Geometry tokens live only in
`:root`; colors repeat per theme.

Verified geometry identities (board 17, S5 card, track 242px):

- Window bar: x=12, w=121 → left 0.05·W, width 0.5·W (fixture `leftFraction`
  0.05 / `widthFraction` 0.5) ✓
- Fill: w=82 ≈ 121·0.68 (work session 68%) ✓; cap at window end ✓
- Tick centers land at 0.1/0.3/0.7/0.9 of the track (S5) and 1/14, 4/14,
  10/14, 13/14 (W7) ✓; Now at 0.5 ✓; gridlines at domain edges + ticks ✓

## Theme variables (dark / light)

| Pen var | dark | light | panel.css token |
| --- | --- | --- | --- |
| surface | #1B2224 | #FFFDF9 | --swap-tl-surface |
| surface-2 | #212A2C | #F0EDE6 | --swap-tl-surface-2 |
| surface-3 | #283234 | #E7E3DA | --swap-tl-surface-3 |
| border | #2B3538 | #E3DFD5 | --swap-tl-border |
| border-strong | #3A4649 | #D3CEC2 | --swap-tl-border-strong |
| text-primary | #ECEFEE | #14191B | --swap-tl-text |
| text-secondary | #A3AEAF | #565D5B | --swap-tl-text-2 |
| text-tertiary | #899395 | #6B726E | --swap-tl-text-3 |
| accent | #3FC2B2 | #0C8577 | --swap-tl-accent |
| accent-quiet | #23423F | #DCEDE9 | --swap-tl-accent-quiet |
| accent-text | #7FE0D2 | #0A6E62 | --swap-tl-accent-text |
| warn | #D9A441 | #9A6B10 | --swap-tl-warn |
| ok | #5FB98A | #2F7D57 | --swap-tl-ok |
| bg | #151A1C | #F7F6F2 | reuse --swap-bg (equal) |
| font-ui / font-num | Inter / IBM Plex Mono | | bundled (Task 1) |

Note: these differ subtly from the legacy main-panel tokens (e.g. Pen
`$surface` #1B2224 vs shipped `--swap-surface` #20282B) — deliberate: the
main panel keeps its reconciled baseline; timelines render board-exact.

## Companion frame (board 17/18)

- 600 × 560, radius 14, fill `$bg`, vertical layout; 8px gap to main panel.
- Header 44px (bottom border `$border`), gap 9: mark 24×24 `$accent-quiet`
  r7 with lucide `calendar-clock` 14×14 `$accent-text`; title "Reset
  timelines" 13.5/600 ls −0.2; spacer; timezone pill `$surface-2` r20 gap 6
  — "Now 13:30" mono 12/600 `$text-primary` + "America/Los_Angeles" ui 12
  `$text-secondary`; close 24×24 r7, lucide `x` 13×13 `$text-secondary`.
- Body (fills remainder, gap 9): caption 11.5 lh1.35 `$text-secondary`
  "Each bar is one full window on a shared clock; the fill is quota used,
  not time elapsed."; legend gap 14; then S5 and W7 cards.
- Legend: window swatch 20×9 r4.5 `$surface-3`+`$border` 1; label 12
  `$text-secondary` "full window (5h / 7d)"; quota swatch 20×9 r4.5
  `$accent`, label "quota used — not elapsed"; now dashes 2×18 (3× 4px
  dashes, gap 3, r1) `$warn`, label "now" 12/600 `$warn`.

## Chart cards (S5 = "Session resets · 5-hour", W7 = "Weekly resets · 7-day")

- Card: fill `$surface`, radius 10, `$border` 1, pad 10, vertical gap 6.
- Title 14/600 ls −0.2 lh1.3; hint 11 `$text-tertiary` ("full 5-hour window
  per account" / "full 7-day window per account"); title row gap 8 with
  spacer; divider 1px `$border`; axis height 16; rows area height 142, gap 2.
- Axis: ticks `$text-tertiary` 12, fixed width 36, centered; Now label
  `$warn` 12/600 width 36. S5 ticks −4h/−2h/+2h/+4h; W7 −6d/−3d/+3d/+6d.
- Rows area backdrop: 1px `$border` gridlines at track edges + tick
  positions; Now line 2px `$warn` dashed (5 on / 4 off) full height.

## Row (22px, radius 6, gap 8)

- Unselected: transparent fill; index badge 22×22 `$surface-2` r6, label
  mono 12/600 `$accent-text`… board 20 RS rows use `$text-secondary` badge
  text on `$surface-2`; selected row: fill `$surface-2`, badge
  `$accent-quiet` + rail 2.5×16 @(0,3) `$accent` r1.25. (Board 17 Row 1 is
  selected+active — the fixture's selectedSlot 1.)
- Left block 140px gap 6: badge, alias ui 12 ls −0.1 `$text-primary`,
  active dot ellipse 5×5 `$ok`.
- Track 242×22: window bar y6 10px r5 `$surface-3` + `$border` 1; quota
  fill same origin r5; reset cap 2.5×14 @y4 `$accent-text` r1.25.
- Detail block 152px gap 4 right-aligned: "68% used" 12/600
  `$text-primary`; "· 14:00" 12 `$text-secondary`.

## Row states (board 20, RS rows)

| State | Window | Fill | Pct text | Reset text |
| --- | --- | --- | --- | --- |
| ok | `$surface-3`+`$border` | `$accent` | "68% used" primary 600 | "· 14:00" secondary |
| near limit (≥~90) | same | `$warn` | same | same |
| usage-unavailable | same, no fill | — | "no usage data" tertiary 600 | "· 16:05" secondary |
| stale | transparent + `$border-strong` 1 | `$border-strong` @ 0.7 opacity | "54% used" tertiary 600 | "· last known" secondary |
| elapsed | none | none | status text across track+detail: "Awaiting updated usage — no fresh cycle" tertiary 12 | — |
| no-window | none | none | "No subscription quota — no reset window" tertiary 12 | — |

Stale reset cap: `$text-tertiary`. Stale rows keep last-known geometry.

## Row detail popover (board 20)

`$surface-3` r8 `$border-strong` 1, pad 10, gap 8. Head: badge 20×20
`$surface` r6, index mono 12/600 `$text-secondary`; full alias 12/600
`$text-primary`. Per window: label 11 `$text-tertiary` ("Session window · 5
hours"); row: pct 12/600 primary + "· 1h 45m left" 12 secondary; span
"Sat 12 Sep 10:15 → 15:15" 12 secondary; "start inferred (reset − 5h)" 11
`$text-tertiary`.

## Header trigger (board 16)

26×26 r7 icon button between spacer and Refresh: lucide `calendar-clock`
14×14 `$text-secondary`. States (board 20): default (tooltip/accessible
name "Reset timelines"), open (highlighted while companion open), loading
(pressable; skeleton rows), disabled (no accounts; tooltip says why).

## Fallback (board 20 Fallback Section)

360-wide takeover of the main panel: header 44 + back row 34 (bottom
border) + body gap 8. "Accounts" control returns to the list. Narrow rows
carry pct only; detail holds exact start/end/countdown.

## Auto-sized values not serialized in Pen — measure from 1x exports

Timezone-pill vertical/horizontal padding, mark icon optical centering,
skeleton row shape (board 20 Loading Body), empty-state body (board 20),
fallback "Accounts" control internals. The fidelity gate (Task 3) diffs
against exports, closing these gaps empirically.

## Lucide icons required

`calendar-clock` (trigger + companion mark), `x` (companion close).
Existing panel icons (`refresh-cw`, `settings`) already ship — check
`icons.js` coverage; reproduce `calendar-clock` in the same system if
absent (utility-icon style, no library dependency).
