# Verification and acceptance

## Deterministic matrix

Use `fixtures/timelines.json` with its frozen `now`. Assert endpoints, width,
fill fraction and Now50% with floating-point tolerance. Fixtures are normalized
chart inputs, not an already-supported wire schema. Test the live adapter too.

| Case | Required assertion |
| --- | --- |
| Four nominal account windows | Session and weekly width50%; correct offsets/fills/date labels |
| Work selected/active | Main68%/41% agrees with charts; selected is not activation |
| Research selected, work active | Independent markers; Enter opens detail without switch |
| Missing usage, known reset | Neutral full window; no fabricated zero |
| Missing reset, known usage | No positioned bar; truthful status/detail |
| Stale and elapsed | Last-known style; no automatic future cycle at expiry |
| API-key/no-window | Both charts retain row with text status |
| Empty/first load | Empty state or skeleton; no false data |
| Long alias, 10+ accounts | Full detail accessible; matching order and independent row scroll |
| Clock crosses reset | Awaiting updated usage on tick; no fill reset to0 |
| DST/midnight/timezone | Epoch duration stable; local exact start/end correct |
| Snapshot mutation | Alias/active refresh in place; removed detail closes safely |
| Out-of-range/invalid | Explicit state/indicator; finite geometry; no NaN/Infinity |

## Browser and native checks

Capture boards16–20 equivalents in both themes using fixture data. Check all
controls at360×560, expanded968×560 and narrow fallback. Verify tab sequence,
Enter/Space, two-stage Escape, focus restoration and reduced motion. Existing
Settings/alias dialogs remain usable without a competing focus trap.

On macOS verify left/right display edges, a secondary display with nonzero
origin, display changes while open, menu-bar/dock visible bounds, repeated
open/close, outside click and clicking every main action while the companion
is open. Confirm real account switching remains explicit, renames propagate,
no extra usage requests occur and observers/windows are cleaned up.

## Suggested commands for the implementing agent

Run from repository root; use the project's existing environment. These are
future implementation checks, not tests run by the design handoff author.

```sh
uv run pytest -n 4 tests/test_menubar_viewmodel.py tests/test_menubar_bridge.py tests/test_menubar_wire_contract.py tests/test_menubar_import_smoke.py tests/test_menubar_package.py -q
uv run pytest -n 4 tests/test_menubar_appearance.py tests/test_menubar_alias_sheet.py -q
node --check src/claude_swap/menubar/web/panel.js
node --check src/claude_swap/menubar/web/timelines.js
uv build --wheel
uv run pytest -n 4 -q
```

Add and run meaningful geometry/state tests for the chosen module structure.
Existing wire tests scan known JS files; extend the scan if bridge sends move
into a new module. Inspect the built wheel contents, not only source paths.

## Completion evidence

Record source commit, changed modules/contracts, exact checks/results, browser
screenshots, native strategy and tested display/focus scenarios. List any
unverified native cases explicitly. Reconcile deliberate design changes in
IMPLEMENTATION-NOTES.md and the affected Pen boards/exports before calling
product/design synchronized. No release/publishing step is included.
