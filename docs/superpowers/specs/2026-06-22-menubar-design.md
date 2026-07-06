# Design: macOS menu bar for claude-swap

**Date:** 2026-06-22
**Status:** Approved (pending spec review)

## Summary

Add a macOS menu bar app to `claude-swap` that shows each managed account's
usage stats (mirroring `cswap --list`) and offers one-click account switching,
plus the broader action set from `cswap --tui` (add / remove / refresh) and a
settings submenu. The app is a thin GUI shell over the existing
`ClaudeAccountSwitcher`; it never re-implements account logic.

Launched via `cswap --menubar`. Built on `rumps`, shipped as an optional extra.

## Goals

- See every managed account's quota (5h / 7d / spend) at a glance.
- Switch accounts with one click (specific account, rotate, best, next-available).
- Mirror the TUI action set: add account (login / setup-token), remove account,
  refresh current credentials.
- A settings submenu controlling menu-bar title content, refresh cadence, and
  (optionally) launch-at-login.

## Non-goals

- No re-implementation of any account/usage/switch logic — strictly a shell over
  `ClaudeAccountSwitcher`.
- No native AppKit settings window (a `rumps` submenu covers the settings).
- No cross-platform GUI — macOS only (rumps is macOS-only; clean error elsewhere).
- No per-account detail panes beyond a compact one-line usage summary (YAGNI).

## Architecture

A single new module `src/claude_swap/menubar.py` containing a `rumps.App`
subclass. Three layers, kept separable:

1. **Pure helpers (unit-tested, no rumps import required):**
   - `MenuBarSettings` — dataclass with `load(path)` / `save(path)` over a small
     JSON file. Holds defaults.
   - `format_title(snapshot, settings) -> str` — builds the menu-bar title text
     from the active account + settings toggles.
   - `format_account_label(info, usage) -> str` — builds one account row's label.
2. **Data layer (existing, untouched):** `ClaudeAccountSwitcher` —
   `_build_accounts_info()`, `_collect_usage()`, `_format_usage_lines()`,
   `switch()`, `switch_to()`, `add_account()`, `add_account_from_token()`,
   `remove_account()`, `_get_current_account()`.
3. **App glue (thin, not unit-tested):** rumps callbacks, the refresh timer, and
   the worker thread. Kept deliberately thin, mirroring how `tui.py` keeps its
   curses primitives thin and untested.

### Why this shape

`rumps` runs its event loop on the **main thread**, and `_collect_usage()` does
**blocking network I/O**. Calling it from a menu callback or timer on the main
thread would freeze the menu bar during every fetch. Therefore: usage fetching
runs on a **worker thread** that writes an in-memory `snapshot`; all UI
rendering only *reads* that snapshot and never blocks.

## Distribution & launch

- `pyproject.toml` optional extra:
  ```toml
  [project.optional-dependencies]
  menubar = ["rumps>=0.4.0"]
  ```
  Install: `uv tool install 'claude-swap[menubar]'` or
  `pipx install 'claude-swap[menubar]'`.
- New `--menubar` flag added to the CLI's mutually-exclusive group (consistent
  with the existing `--tui` flag).
- Dispatch lazily imports `rumps`. If the import fails, print an install hint and
  exit (mirrors the existing curses `ImportError` handling in `cli.py`). If not
  running on macOS, print a clean error and exit.

## Menu layout

```
⇄ loc · 42%                                  ← title (configurable, see Settings)
─────────────────
✓ 2  loc@papaya  5h 42% · 7d 18% · $ 30%     ← click = switch_to(2)
  3  alt@gmail   5h 88% · 7d 60%
─────────────────
Rotate to next                               ← switch()
Switch to best                               ← switch(strategy="best")
Next available                               ← switch(strategy="next-available")
─────────────────
Add account ▸     From current login         ← add_account()
                  From setup-token…           ← prompt email+token, add_account_from_token()
Remove account ▸  (account list, with confirm)← remove_account(num)
Refresh current credentials                  ← add_account() on current login
─────────────────
Settings ▸
Refresh now                                  ← force a worker refresh
Quit
```

- Each account row is one clickable item; a checkmark marks the active account;
  clicking calls `switch_to(num)`.
- Row label shows a compact one-line usage summary derived from the same usage
  dict `_format_usage_lines()` consumes (tightest 5h / 7d / spend figures).
- Add / Remove submenus are rebuilt from the current snapshot on each render.

## Data flow & threading

- On launch, on every refresh-interval timer tick, and on menu-open: spawn (or
  reuse) a worker thread that calls `_build_accounts_info()` + `_collect_usage()`,
  stores the result in the in-memory `snapshot`, and requests a menu rebuild.
- The timer callback and menu rendering only **read** the snapshot. First render
  (before the first fetch completes) shows accounts from local sequence data with
  a "loading…" usage placeholder.
- `_collect_usage()` already writes the shared 15s usage cache
  (`<backup_dir>/cache/usage.json`), so the menu and the CLI stay consistent and
  repeat fetches are cheap.
- After any switch, show a `rumps.notification` carrying the existing
  platform-aware "restart Claude Code to activate" post-switch guidance, and
  trigger an immediate refresh.

## Settings (submenu, persisted to `<backup_dir>/menubar_settings.json`)

- ☑ **Show quota % in menu bar** — append the active account's tightest quota %
  to the title.
- ☑ **Show account name in menu bar** — append the active account's email
  local-part (truncated) to the title.
- **Refresh interval ▸** — 30s / 60s / 5m, radio-style (checkmark on the active
  choice). Changing it reschedules the timer.
- ☐ **Launch at login** *(optional)* — when enabled, write a LaunchAgent plist to
  `~/Library/LaunchAgents`; when disabled, remove it. The one item beyond the
  strict ask; included because menu-bar apps usually need it.

Title combinations (icon `⇄` is always present):
`⇄` · `⇄ loc` · `⇄ 42%` · `⇄ loc · 42%`. With no active managed account, the
title is the icon only.

`menubar_settings.json` is created with defaults on first launch. Unknown/missing
keys fall back to defaults so forward/backward changes don't break loading.

## Error handling

- Usage fetch failures already degrade to `"no credentials"` / `"usage
  unavailable"` strings — rendered inline in the row label, never crash the app.
- `ClaudeSwitchError` from switch/add/remove → `rumps.alert` with the message;
  the app keeps running.
- Missing `rumps` or non-macOS host → clean CLI error before the app starts.
- Settings file corrupt/unparseable → log, fall back to defaults, overwrite on
  next save.

## Testing

- Unit-test the pure helpers with `rumps` mocked or absent (same strategy as
  `test_tui.py`):
  - `MenuBarSettings` load/save round-trip, defaults, and corrupt-file fallback.
  - `format_title` across all toggle combinations and the no-active-account case.
  - `format_account_label` for dict usage, `"no credentials"`, `None`, and the
    active vs inactive marker.
- App glue (rumps callbacks, run loop, worker thread wiring) is verified
  manually; AppKit cannot run headless in CI.

## Files touched

- `src/claude_swap/menubar.py` — new module (app + pure helpers).
- `src/claude_swap/cli.py` — add `--menubar` flag and dispatch.
- `pyproject.toml` — add `menubar` optional extra.
- `tests/test_menubar.py` — new tests for the pure helpers.
- `README.md` — document `cswap --menubar` and the optional install.
