# Spec: Menubar redesign — Pen handoff (graphite/ivory, statuses, dialogs)

Supersedes the menubar-v2 SPEC.md (fully implemented and shipped as
0.28.0). Design rationale lives in
`docs/superpowers/specs/2026-09-12-menubar-redesign-design.md`; the Pen
handoff in `assets/` is the authoritative visual reference.

## ASSUMPTIONS I'M MAKING

1. The Pen artwork is authoritative where the handoff token CSS and the
   artwork disagree; the token seed is the starting point, not the law.
2. Light theme is derived from the token table + mirrored layout and is
   human-reviewed in the fixture browser before sign-off.
3. No new bridge actions or protocol changes — the redesign is the web
   layer, additive view-model fields, and one reply-path bug fix.
4. Fonts: Inter / IBM Plex Mono with system fallback stacks; no font
   binaries ship (per the handoff).
5. Full pass: all eight artboards in one implementation.

## Objective

Rebuild the menubar panel's UI/UX to the Pen redesign: graphite and warm
ivory themes with restrained teal accents; account selection as segmented
cards where **selected** (teal ring) is distinct from **active**
(subtitle); "68% USED" quota bars with countdown sub-lines and collapsed
per-model/spend disclosures; explicit Best/Rotate actions; an iOS-style
auto-switch toggle with a persistent summary; native-`<dialog>` sheets
(token add with concealed input and field errors, remove confirmation,
switch-history Activity); honest states everywhere — missing ≠ 0%, stale
keeps measured values, API-key accounts aren't dead logins, countdowns at
zero say *Awaiting updated usage*.

Success looks like: the panel matches the main dark artboard at 360×560
(and its light twin), passes keyboard/reduced-motion/accessibility rules
from the handoff, keeps every existing capability working through the
unchanged bridge, and the full suite stays green.

## User Stories

1. **At-a-glance quota awareness** — As a multi-account developer, I want
   each account's five-hour and weekly usage shown as clearly labeled
   "used" bars with reset countdowns, so I know where headroom remains
   without opening a terminal.
2. **Safe browsing** — As a user flipping between accounts, I want
   *selected* (preview) visually distinct from *active*, so I never
   switch accounts by accident; only an explicit Switch action activates.
3. **Deliberate switching** — As a user near a limit, I want a primary
   "Switch to …" action plus discoverable Best and Rotate alternatives,
   so the right switch is one deliberate click.
4. **Progressive detail** — As a user who hits per-model limits or cares
   about spend, I want those available behind a disclosure, subordinate
   to the primary quotas, so the panel stays calm by default.
5. **Trustworthy automation** — As a user relying on auto-switch, I want
   its on/off state and "at N% used · strategy" summary always visible,
   so I can trust what the automation will do.
6. **Honest stale data** — As a user whose measurements are stale or
   offline, I want last-known values kept and labeled stale with their
   age (and "Awaiting updated usage" once a countdown hits zero), so I'm
   never misled by fabricated zeros or blanks.
7. **No crying wolf** — As an API-key account owner, I want my account
   to read "No subscription quota", not as a broken login; and when a
   token genuinely dies, I want "Needs login" with the recovery path.
8. **Safe account management** — As a user adding an account from a
   setup token, I want a focused sheet with a concealed token field,
   field-level errors, Esc-to-close, and no residue on cancel; as a user
   removing one, I want explicit identity and consequence before
   deletion — so neither action is ever accidental.
9. **Audit trail** — As a user watching the automation work, I want an
   Activity sheet listing recent switches with timestamps, so I can see
   what switched and when.
10. **Accessible operation** — As a keyboard-only or motion-sensitive
    user, I want full Tab/Esc/Enter operation, visible focus, and
    reduced-motion behavior, so the panel works for me.

## Tech Stack

Unchanged: Python 3.12+ package, PyObjC shell, WKWebView, vanilla JS/CSS
(no framework, no build step — plain `<script>` tags), pytest 8.

## Commands

```
Test (all):                        uv run pytest
Focused:                           uv run pytest tests/test_menubar_viewmodel.py -x
Run the app:                       PYTHONPATH=src uv run python -m claude_swap menubar
Panel in browser (fixture mode):   open 'http://127.0.0.1:8765/index.html'   (serve web/ no-store)
Theme override for screenshots:    ?theme=light|dark
JS syntax floor:                   node --check src/claude_swap/menubar/web/panel.js (and sheets.js, icons.js)
```

## Project Structure

```
src/claude_swap/menubar/web/       rewritten assets (same directory)
  index.html        CSP; <dialog> skeletons (token / remove / activity); script tags
  panel.css         rebuilt from assets/menubar-redesign-tokens.css (--swap-* vars)
  icons.js          currentColor SVG factory (swap, refresh, gear, best, rotate, chevron, activity)
  sheets.js         <dialog> management: showModal, Esc, focus trap/restore, token clear,
                    field errors, activity/switch-history rendering
  panel.js          state + main-panel render (header, selector cards, identity,
                    quotas, disclosure, actions, auto-switch, footer)
src/claude_swap/menubar/viewmodel.py   additive: account.status; preserve-measured
                                       windows; pace gating on staleness
tests/
  test_menubar_viewmodel.py        extended: status mapping, preserved windows, pace gating
  test_menubar_wire_contract.py    extended: sheets.js/icons.js on the wire; reply-path
                                   behavior pinned (error messages survive)
  (all other menubar suites untouched or extended, never weakened)
```

## Code Style

Python: existing conventions (docstring'd modules, `from __future__`,
type hints, no GUI imports in platform-safe files). JS: one IIFE-free
module per file, `const`-first, no dependencies, `esc()` for every
string interpolation, tabular numerals for measurements. CSS:
`--swap-*` custom properties, `prefers-color-scheme` default +
`data-swap-theme` override, `prefers-reduced-motion` support.

## Testing Strategy

- **viewmodel**: status vocabulary (ok / api-key / needs-login /
  unavailable) pinned by contract tests; rolled/passed windows keep
  measured pct with `state: "stale"` + "Awaiting updated usage"; pace
  hidden when stale; additive-field behavior preserved.
- **wire contract**: every action `sheets.js`/`panel.js` can send is
  registered in `_panel_handlers` with payload specs; the reply adapter
  forwards `result.error` on failure (regression test for the bug).
- **Fixture-mode browser verification** (the primary UI gate): all eight
  artboard states pushed as synthetic view-models; keyboard Tab/Esc
  through sheets; long identities, many accounts, reduced motion; both
  themes; vision-gated screenshots of the final rendering.
- Repo rules unchanged: no test touches the real account store; full
  suite green before every commit.

## Boundaries

**Always:**
- Run `uv run pytest` before committing; suite green.
- Keep all view-model changes additive (new optional fields; never
  repurpose or remove; absent ≠ null).
- Escape every string interpolation in JS; status conveyed by text/icon,
  never color alone.
- Keep fixture mode working in a plain browser (it is the verification
  harness).

**Ask first:**
- Any change to `bridge.py` message protocol or `_panel_handlers`
  semantics.
- Any change to `app.py` beyond what the reply-path fix requires.
- Adding files beyond the four web assets listed above.

**Never:**
- Touch credential writes, lock ordering, usage polling, auto-switch
  policy, launchd contracts, CLI, or TUI behavior.
- Change legacy roll-to-zero semantics for CLI/TUI (panel-only
  preserve-measured).
- Introduce a frontend framework, build step, or external font/asset
  request.
- Remove or weaken existing tests; break the bridge protocol or the
  right-click fallback menu.

## Success Criteria

1. Panel matches the main dark artboard at 360×560 (44/486/30 layout),
   and a derived light twin — both human-reviewed in the fixture browser
   with vision-gated screenshots.
2. Selected ≠ active everywhere; only the explicit Switch action
   activates; success/error UI reflects the backend reply, with real
   error messages in toasts (reply-adapter fix).
3. Quotas: explicit **used** labeling, countdown sub-lines, collapsed
   per-model/spend disclosure; missing → *unavailable* (never 0%);
   rolled windows keep measured values marked stale; countdown at zero →
   *Awaiting updated usage*; pace hidden when stale.
4. Account status distinctions render correctly: API-key → *No
   subscription quota*; re-login → *Needs login*; transient sentinels →
   availability note (none presented as dead logins).
5. Sheets are native `<dialog>`s: Esc closes, focus trapped and restored
   to trigger, token input cleared on close, field-level errors; the
   Activity sheet lists switch history.
6. Keyboard operable (Tab/Esc/Enter), reduced-motion respected, tabular
   numerals for all measurements.
7. Wire-contract, viewmodel, bridge, package, and import-smoke suites
   extended and green; full suite green; live app verified.

## Open Questions

None blocking. Icon fidelity against the Pen artboards is judged during
fixture verification (handoff ships no icon exports).
