# Spec: Menubar v2 — CodexBar-class web panel

Source design: `docs/superpowers/specs/2026-09-12-menubar-panel-design.md`
(approved in brainstorming, 2026-09-12). This spec is the implementation
contract; the design doc carries the reasoning.

## ASSUMPTIONS I'M MAKING

1. Target is the existing Python package — no Xcode, no second codebase
   (approach B, approved).
2. `rumps` is dropped; the `menubar` extra becomes
   `pyobjc-framework-Cocoa` + `pyobjc-framework-WebKit` (floor ~10.x).
3. Notifications go through `osascript display notification`; the
   `ensure_notification_identity` plist hack is deleted (banner-only, same
   as today).
4. macOS 11+ gets the SF Symbol status icon; older systems fall back to the
   `⇄` text title. WKWebView is available on every macOS we support.
5. The panel is vanilla HTML/CSS/JS — no framework, no build step, no npm;
   assets are plain files bundled with the package (hatchling already
   packages everything under `src/claude_swap`).
6. Light and dark mode both ship, driven by `prefers-color-scheme`.
7. UI text is English, matching the rest of the project.

## Objective

Replace the rumps text-menu menubar with a visual popover panel of
CodexBar-class quality. One left-click on the status item shows every
managed Claude account with 5h / 7d / per-model utilization bars, live reset
countdowns, spend, and freshness; switching accounts, hold-out (disable),
add/remove, and the auto-switch engine are all operable from the panel. A
right-click classic menu remains as a fallback. The core —
`ClaudeAccountSwitcher`, `accounts_snapshot()`, `AutoSwitchEngine`, usage
store, locking, credential storage — is untouched; the panel is a fourth
thin frontend over it, like the TUI.

Success looks like: the user never opens a terminal for day-to-day account
management; `cswap menubar` and its launchd service behave exactly as
before from the outside.

## Tech Stack

- Python 3.12+ (existing package), PyObjC (`pyobjc-framework-Cocoa`,
  `pyobjc-framework-WebKit`) for the AppKit/WebKit shell
- Vanilla JS + CSS, WKWebView-rendered, no build tooling
- pytest 8 (+ xdist parallel, existing config) for everything testable
- Design reference: [steipete/codexbar](https://github.com/steipete/codexbar)

## Commands

```
Test (all, parallel by default):   uv run pytest
Test one file:                     uv run pytest tests/test_menubar_viewmodel.py -x
Test one test:                     uv run pytest tests/test_menubar_viewmodel.py::test_name -x
Run the app (dev):                 uv run cswap menubar
Panel in browser (fixture mode):   open src/claude_swap/menubar/web/index.html
Service install/status/remove:     uv run cswap menubar --install-service | --service-status | --uninstall-service
```

No build step: hatchling packages `src/claude_swap` wholesale, so
`menubar/web/*` assets ship as package data with no manifest change.

## Project Structure

```
src/claude_swap/menubar/         NEW package (menubar.py becomes a shim)
  __init__.py                    re-exports run, framework_build_warning,
                                 pure helpers — existing imports keep working
  app.py                         PyObjC glue: NSStatusItem, NSPopover,
                                 WKWebView, timers, right-click NSMenu,
                                 engine thread, osascript notifications
  bridge.py                      action routing table + id/reply correlation
                                 (routing logic pure and unit-tested)
  viewmodel.py                   PURE AccountsSnapshot → JSON view-model;
                                 absorbs menubar.py's pure helpers
  web/                           panel assets (package data)
    index.html  panel.css  panel.js
tests/
  test_menubar.py                existing — keeps passing via re-exports
  test_menubar_viewmodel.py      NEW — view-model construction
  test_menubar_bridge.py         NEW — dispatch/reply/allowlist
  test_menubar_import_smoke.py   NEW — macOS-only import smoke of app.py
docs/ARCHITECTURE.md             update §6.5 + directory map
README.md                        rewrite "Menu bar (macOS)" section
```

## Code Style

Python follows the existing codebase: module docstring explaining the
layer's contract, `from __future__ import annotations`, type hints,
dataclasses for value objects, no GUI imports at module level in
platform-independent files:

```python
"""Pure snapshot → view-model transform for the menubar panel.

Import-safe on every platform (no PyObjC); ``app.py`` owns all GUI glue.
Countdown text is baked here for first paint; ``resetsAt`` epochs let the
panel tick locally between pushes.
"""

from __future__ import annotations

from claude_swap.models import AccountsSnapshot

def build(snapshot: AccountsSnapshot, *, now: float | None = None) -> dict:
    """Return the additive schemaVersion-1 view-model for the panel."""
```

JS is one IIFE-free module per file, `const`-first, state + render
functions, no dependencies; CSS uses custom properties for the two palettes.

## Testing Strategy

- **viewmodel / bridge**: pure pytest on all platforms, synthetic
  `AccountsSnapshot`/`UsageEntry` fixtures (port/extend existing
  `test_menubar.py` helper tests). Windows in fixtures for: 5h/7d/model
  windows, sentinel quarantine, spend presence/absence, pace gating,
  additive-field behavior (field absent, not `null`).
- **Contract**: a key-snapshot test pins the view-model schema so fields
  can't be silently removed (additive convention, like `json_output.py`).
- **Bridge**: fake transport tests routing, id/reply correlation, allowlist
  rejection of unknown actions and malformed payloads, error replies.
- **Import smoke**: macOS CI installs the `menubar` extra and imports
  `app.py` (no `NSApplication` run — headless-safe). Linux CI unchanged.
- **Panel visuals**: manual + fixture-mode browser pass; GUI automation can
  drive `index.html` fixture mode later.
- Repo rules apply absolutely: no test may touch the real account store
  (`tests/conftest.py` audit hook enforces this); suite stays green before
  every commit.

## Boundaries

**Always:**
- Run `uv run pytest` before committing; full suite green.
- Keep GUI imports lazy/macOS-gated so Linux CI never sees PyObjC.
- Run every blocking operation (locks, Keychain, network) on background
  threads; never block the AppKit main thread.
- Keep the view-model JSON additive: new fields optional, never `null`
  placeholders, never repurpose or remove.
- Use `fsutil.replace_with_retry` for any new file write (e.g. prefs).
- Webview loads bundled local content only; navigation elsewhere cancelled;
  bridge validates action + payload types against an allowlist.

**Ask first:**
- Any `pyproject.toml` change beyond swapping the `menubar` extra.
- CI workflow changes.
- Any new `cswap config` / `SETTING_SPECS` additions.
- Changes to the launchd plist contract (label, paths, log locations).

**Never:**
- Touch the credential write path: `_classify_outgoing_credential`,
  `_prepare_credentials_for_activation`, `shared_credential_fields`.
- Reorder or re-enter the three-lock acquisition (`FileLock` →
  `claude_credentials_lock` → `claude_config_lock`).
- Add network calls that bypass the shared usage store / adaptive polling
  (the ~28 req/hour budget is sacred).
- Break `cswap menubar` / `--install-service` / launchd-label compatibility
  or the `claude_swap.menubar` import surface used by tests.
- Remove or weaken a failing test.

## Success Criteria

1. `uv run cswap menubar` shows the status item; left-click opens the
   panel; every managed account renders with 5h/7d bars + live countdowns,
   per-model rows, and spend when present.
2. Switch (explicit / rotate / best), disable/enable, remove, add (from
   login / from setup-token), and the auto-switch toggle all work from the
   panel, with per-action spinner/toast feedback.
3. Right-click menu works even if the webview fails to load.
4. Dark mode follows the system appearance.
5. `uv run pytest` fully green, including untouched `tests/test_menubar.py`
   and the new viewmodel/bridge/contract tests, on Linux and macOS CI.
6. `cswap menubar --install-service` flow works unchanged (same label
   `com.cswap.menubar`, same logs).
7. Auto-switch events push to the panel and notify via `osascript`.
8. README menubar section rewritten (screenshot placeholder acceptable);
   ARCHITECTURE.md §6.5 and directory map updated.

## Open Questions

1. Exact PyObjC minimum version to pin — assumed `>=10.0`; confirm during
   implementation against the oldest macOS we test (CI runners).
2. README screenshot: capture during implementation (needs a populated
   account store) — acceptable to merge with a placeholder first?
