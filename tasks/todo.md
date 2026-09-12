# Tasks: Menubar v2 — CodexBar-class web panel

Plan: `tasks/plan.md` · Spec: `SPEC.md`. Definition of Done
(`references/definition-of-done.md` in agent-skills) applies to every task
on top of its own acceptance criteria.

## Task 1: Convert `menubar.py` to `menubar/` package (pure re-export)

**Description:** Restructure without behavior change. Move the current
module wholesale to `src/claude_swap/menubar/_legacy.py`; create
`menubar/__init__.py` re-exporting the public surface (`run`,
`framework_build_warning`, and every name `tests/test_menubar.py` /
`tests/test_cli.py` / `cli.py` import — grep first). Delete `menubar.py`.
Zero runtime behavior change.

**Acceptance criteria:**
- [x] `from claude_swap.menubar import run, framework_build_warning, ...` works for all existing importers (verified by grep + suite)
- [x] `uv run cswap menubar` still launches the old rumps app (manual, if rumps installed)
- [x] No public name removed (test_menubar.py untouched and green)

**Verification:**
- [x] `uv run pytest` green (full suite, untouched tests)

**Dependencies:** None

**Files likely touched:**
- `src/claude_swap/menubar/__init__.py` (new)
- `src/claude_swap/menubar/_legacy.py` (new, = old menubar.py)
- `src/claude_swap/menubar.py` (deleted)

**Estimated scope:** S

## Task 2: `viewmodel.py` — pure helpers move + `build()` + schema contract

**Description:** Move the pure helpers out of `_legacy.py`
(`format_title`, `usage_summary`, `format_account_label`,
`_window_pct`, `_resets_at_ts`, `_live_countdown`,
`_rolled_weekly_window`, `parse_switch_history`, `_adapt_snapshot`) into
`viewmodel.py`; re-export from `__init__`. Add `build(snapshot,
*, now=None) -> dict` producing the schemaVersion-1 additive view-model
(SPEC §7): accounts with 5h/7d/model windows, spend, pace, freshness,
quarantine from sentinel, autoSwitch block, history. Add
`test_menubar_viewmodel.py` and a key-snapshot contract test.

**Acceptance criteria:**
- [x] `build()` handles: healthy accounts, api-key/sentinel accounts (quarantined + note, no bars), missing spend (field absent), stale usage (ageText, state), disabled/alias/active flags
- [x] Additive contract: optional fields absent — never `null` — pinned by contract test
- [x] countdownText baked for first paint + `resetsAt` epochs for local ticking
- [x] Existing helper tests green via re-exports

**Verification:**
- [x] `uv run pytest tests/test_menubar_viewmodel.py tests/test_menubar.py -x` green
- [x] `uv run pytest` full suite green

**Dependencies:** Task 1

**Files likely touched:**
- `src/claude_swap/menubar/viewmodel.py` (new)
- `src/claude_swap/menubar/__init__.py`
- `src/claude_swap/menubar/_legacy.py` (helpers removed)
- `tests/test_menubar_viewmodel.py` (new)

**Estimated scope:** M

## Task 3: Panel web v1 — fixture-mode rendering

**Description:** Build `web/index.html`, `web/panel.css`, `web/panel.js`
(vanilla, no build step). Renders the full view-model per SPEC §8: header
(active account, status pill, freshness, refresh button), account pills,
selected-account card (5h/7d bars with live local countdown ticks, per-model
rows, spend line, pace chip), actions row, auto-switch section, footer.
Fixture mode: when `window.webkit` is absent, load an embedded fixture
view-model and log actions to console. Both light/dark palettes via
`prefers-color-scheme`.

**Acceptance criteria:**
- [x] Opening `index.html` in a browser renders the fixture panel with all sections
- [x] Countdowns tick locally every 30s from `resetsAt` without a push
- [x] Pills select cards without switching; action buttons emit calls (console in fixture mode)
- [x] Light and dark palettes both readable (manual toggle via devtools)
- [x] Bar thresholds: green <70, amber <90, red ≥90

**Verification:**
- [x] Manual: `open src/claude_swap/menubar/web/index.html`
- [x] Visual check against CodexBar reference screenshot

**Dependencies:** Task 2 (schema)

**Estimated scope:** M

**Files likely touched:**
- `src/claude_swap/menubar/web/index.html`, `panel.css`, `panel.js` (new)

## Checkpoint A: after Tasks 1-3

- [x] `uv run pytest` green
- [x] Fixture panel renders in browser
- [x] Review with human before shell work

## Task 4: PyObjC shell — status item, menu, loop, notifications; drop rumps

**Description:** New `app.py`: NSStatusItem (SF Symbol
`arrow.left.arrow.right`, `⇄` fallback; title pct via `format_title`),
right-click NSMenu (best / rotate / refresh / auto-switch toggle /
start-at-login / quit), NSTimer snapshot loop on `MenuBarSettings`
interval → background-thread `accounts_snapshot()` → title update,
osascript notifications for engine events, `AutoSwitchEngine` thread
management, main-thread marshaling. `run()` dispatches to the new shell.
Swap pyproject extra to `pyobjc-framework-Cocoa` + `pyobjc-framework-WebKit`,
delete `_legacy.py` and `ensure_notification_identity`. Service flags
unchanged.

**Acceptance criteria:**
- [ ] `uv run cswap menubar` shows status item with live pct title; right-click menu fully functional (switch/rotate/refresh/auto-switch/quit)
- [ ] Auto-switch events produce osascript notifications
- [ ] `rumps` gone from pyproject and source; `grep -r rumps src/` empty
- [ ] Launchd flow works: `--install-service`, `--service-status`, `--uninstall-service`
- [ ] Linux import safety: `python -c "import claude_swap.menubar.viewmodel, claude_swap.menubar.bridge"` on Linux CI green (no AppKit import at module level)

**Verification:**
- [ ] `uv run pytest` green
- [ ] Manual on macOS: run app, exercise every menu item, install+uninstall service

**Dependencies:** Task 1 (package). Parallelizable with Tasks 2-3.

**Files likely touched:**
- `src/claude_swap/menubar/app.py` (new)
- `src/claude_swap/menubar/__init__.py`
- `src/claude_swap/menubar/_legacy.py` (deleted)
- `pyproject.toml`
- `tests/test_menubar_import_smoke.py` (new, macOS-gated import test)

**Estimated scope:** M

## Task 5: Popover + WKWebView + bridge integration

**Description:** `bridge.py` (pure routing: action allowlist, payload
validation, id/reply correlation, push serialization; unit-tested with a
fake transport) + the popover wiring in `app.py`: left-click toggles
NSPopover hosting WKWebView loading bundled `web/index.html`
(`loadFileURL:allowingReadAccessTo:` package web dir, navigation
elsewhere cancelled), `getSnapshot` on open, `cswap.push` view-model
updates, `cswap.reply` for actions. Webview load failure → fallback
notification, right-click menu still works.

**Acceptance criteria:**
- [ ] Left-click opens panel showing live account data; click-outside closes; re-click reopens with fresh data
- [ ] Bridge: `uv run pytest tests/test_menubar_bridge.py` covers routing, correlation, allowlist rejection, error replies
- [ ] Webview loads bundled content only; external navigation cancelled
- [ ] Missing WebKit framework → clean error + menu-only operation

**Verification:**
- [ ] `uv run pytest` green
- [ ] Manual: open/close panel repeatedly; verify bars/countdowns match `cswap list`

**Dependencies:** Tasks 2, 3, 4

**Files likely touched:**
- `src/claude_swap/menubar/bridge.py` (new)
- `src/claude_swap/menubar/app.py`
- `tests/test_menubar_bridge.py` (new)

**Estimated scope:** M

## Checkpoint B: after Tasks 4-5 — go/no-go for the approach

- [ ] `uv run cswap menubar` runs with live panel + fallback menu
- [ ] Service flow intact; Linux CI green
- [ ] Human review before feature completion

## Task 6: Panel actions end-to-end

**Description:** Wire every SPEC §6 action through bridge → core on
background threads with per-action UI feedback (button spinner →
success toast / error toast): switch/rotate/best, disable/enable, remove
(with in-panel confirm), addFromLogin, addFromToken (in-panel input
sheet), setAutoSwitch, setPrefs (refresh interval, title pct), quit.
Engine events push into the panel live.

**Acceptance criteria:**
- [ ] Every action works against the real switcher with correct success/error feedback
- [ ] UI never blocks during actions (main thread stays responsive)
- [ ] Panel state stays consistent after every action (snapshot re-push)
- [ ] Auto-switch toggle starts/stops the engine; events appear in panel + notifications

**Verification:**
- [ ] `uv run pytest` green (bridge tests extended for new handlers)
- [ ] Manual: exercise every action on macOS with a test account store

**Dependencies:** Task 5

**Files likely touched:**
- `src/claude_swap/menubar/app.py`, `bridge.py`
- `src/claude_swap/menubar/web/panel.js`, `index.html`, `panel.css`

**Estimated scope:** M

## Task 7: Stale, error, quarantine & failure states

**Description:** Fetch failure → stale banner + age (stale-on-error);
sentinel/quarantined cards with explainer + re-add actions; zero-account
empty state with add-account CTA; allowlist rejection UX (toast, not
crash); title keeps last-known pct on failure (parity with today).

**Acceptance criteria:**
- [ ] Simulated fetch failure (network off) shows stale data + banner, no crash, title retains pct
- [ ] Quarantined account renders explainer card, not bars
- [ ] Zero-account state guides to add; removed account disappears cleanly while panel open

**Verification:**
- [ ] `uv run pytest` green; viewmodel tests cover stale/error fixtures
- [ ] Manual: airplane-mode refresh; quarantine fixture in browser fixture mode

**Dependencies:** Task 6

**Files likely touched:**
- `src/claude_swap/menubar/viewmodel.py`, `web/panel.js`, `web/panel.css`

**Estimated scope:** S-M

## Task 8: Dark mode + CodexBar-class polish + screenshot

**Description:** Visual pass against the CodexBar reference: typography
scale, spacing rhythm, pill states, bar animations, status pill, toast
design, focus states for the token sheet, both palettes. Capture a README
screenshot (populated store) or confirm placeholder decision.

**Acceptance criteria:**
- [ ] Side-by-side with CodexBar reference: flat cards, clean hierarchy, no default-browser look
- [ ] Dark/light both pass contrast on every state (bars, pills, toasts)
- [ ] Screenshot captured (or placeholder decision recorded)

**Verification:**
- [ ] Manual visual review; human sign-off on aesthetics

**Dependencies:** Task 6

**Files likely touched:**
- `src/claude_swap/menubar/web/panel.css`, `panel.js`
- `README.md` (screenshot embed point)

**Estimated scope:** S-M

## Task 9: Docs + macOS CI import smoke

**Description:** Rewrite README "Menu bar (macOS)" section (panel
description, screenshot, unchanged install/service commands). Update
`docs/ARCHITECTURE.md` directory map + §6.5. Add macOS-only import smoke
(install menubar extra in macOS CI job, import `app.py` headless — no
NSApplication). CI changes are ask-first: present the workflow diff before
pushing.

**Acceptance criteria:**
- [ ] README section matches shipped behavior; ARCHITECTURE.md accurate to the new layout
- [ ] macOS CI installs the extra and the import smoke passes; Linux jobs untouched
- [ ] Human approved the CI diff (ask-first boundary)

**Verification:**
- [ ] `uv run pytest` green; CI green on all three platforms

**Dependencies:** Task 5 (docs can trail shell completion)

**Files likely touched:**
- `README.md`, `docs/ARCHITECTURE.md`
- `.github/workflows/ci.yml` (after approval)

**Estimated scope:** S

## Checkpoint C: complete

- [ ] All SPEC.md success criteria (1-8) verified
- [ ] Definition of Done checklist satisfied
- [ ] Ready for `/review` → `/ship`
