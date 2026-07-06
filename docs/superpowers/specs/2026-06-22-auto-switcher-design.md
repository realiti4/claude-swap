# Design: in-app auto-switcher for the claude-swap menu bar

**Date:** 2026-06-22
**Status:** Approved (pending spec review)

## Summary

Add automatic account switching to the claude-swap macOS menu bar app. When the
active account crosses a usage threshold, the app switches the on-disk
credential to the account with the most headroom — porting the behavior of an
existing external launchd monitor (`~/.claude-swap-monitor/swap-monitor.py`)
into the app, computed from the app's in-process usage data instead of parsing
`cswap --list`.

The app contains **no launchd logic**. The existing launchd agent is
machine-specific cruft; it is removed once, manually, after this feature ships
(see "One-time machine cleanup").

## Goals

- Auto-switch away from a saturated active account to the best alternative,
  with the same trigger/target/cooldown semantics as the current monitor.
- Configure it entirely from the menu (on/off, threshold, cooldown, check
  cadence), with defaults that reproduce the current monitor (95% / 10m).
- Surface swaps and "no fresh account" situations via macOS notifications.

## Non-goals

- No launchd awareness in the app (detect/load/unload/remove). The launchd
  agent is removed manually, once, outside the app.
- No migration of in-flight Claude sessions — switching rewrites the credential
  for the next launch/resume (same limitation as the monitor; the post-switch
  notification already tells the user to restart/resume).
- No per-account auto-switch rules or schedules (YAGNI).

## Ported behavior (the algorithm)

On each evaluation, using the app's structured usage snapshot
(`{num, email, is_active, usage}` per account, where `usage` is a dict with
`five_hour.pct` / `seven_day.pct`, or the string/`None` sentinels):

1. Find the active account. If its usage is unknown this round (sentinel/`None`)
   → do nothing (can't decide safely).
2. If active's 5h **and** 7d are both `< threshold` → do nothing (headroom).
3. Else (active ≥ threshold on either window): candidates = the other accounts
   whose worst-of(5h, 7d) is `< threshold`, **excluding** any with unknown usage.
4. No candidate → "no fresh account" outcome (caller notifies, rate-limited).
5. Otherwise target = the candidate with the most headroom: `min` by
   `(worst(5h,7d), seven_day, five_hour)` — lowest worst-case usage, tie-break
   lower 7d then lower 5h (identical to the monitor's `min` key).

`worst(a) = max(a.five_hour.pct, a.seven_day.pct)`.

## Architecture

All new code lives in `src/claude_swap/menubar.py` (the feature's module),
split by responsibility:

### Pure decision function (unit-tested, no rumps/network)

```
AutoSwitchDecision = tuple[str, int | None]
def decide_auto_switch(accounts, threshold) -> AutoSwitchDecision
```

`accounts` is the snapshot's account list (`[(num, email, is_active, usage), …]`).
Returns exactly one of:
- `("switch", target_num)` — switch to `target_num`
- `("none", None)` — active has headroom, nothing to do
- `("no_candidate", None)` — active saturated, no other account has headroom
- `("unknown_active", None)` — active account's usage is unknown this round

This is the whole algorithm in one total, side-effect-free function. It never
raises.

### Cooldown + notification state (unit-tested)

A small JSON store persisted to `<backup_dir>/menubar_state.json`:

```
@dataclass class MenuBarState:
    last_switch_at: float = 0.0          # epoch seconds
    last_noswap_notify_at: float = 0.0   # epoch seconds
    load(path) -> MenuBarState           # corrupt/missing -> defaults
    save(path) -> None
```

Persisting across restarts means a relaunch (e.g. launch-at-login) respects the
cooldown rather than swapping immediately. `last_switch_at` is stamped on **any**
app-initiated switch (manual menu click or auto-switch), so a manual switch
does not trigger an immediate auto-swap.

Because `time.time()` is needed for cooldown math and the monitor used wall
time, the menu bar app may call `time.time()` (it is not a workflow script).

### Settings additions

Appended to `MenuBarSettings` (defaults reproduce the current monitor):

- `auto_switch_enabled: bool = False`
- `auto_switch_threshold: int = 95`     (choices 80 / 90 / 95)
- `auto_switch_cooldown: int = 600`     (choices 300 / 600 / 1800)
- `auto_switch_interval: int = 0`       (0 = "with display refresh"; else 60 / 180 / 300)

Settings submenu gains an "Auto-switch" section: a master toggle, and
`Threshold ▸`, `Cooldown ▸`, and `Check ▸` sub-submenus (radio-style
checkmarks). `Check ▸` first item "With display refresh" maps to
`auto_switch_interval = 0`; the time items map to mode B.

### Driver (rumps glue, manual-verify)

A new method `maybe_auto_switch(snapshot)` runs at the end of the refresh
worker's completion path (same place the snapshot is published), so switching
decisions use the freshest data and all switching happens off the main thread
like the existing worker:

- Return early if `auto_switch_enabled` is false.
- **Cadence gate:** in mode A (`interval == 0`) always proceed; in mode B
  proceed only if `now - last_eval >= auto_switch_interval` (tracked in memory).
- `decide_auto_switch(...)`:
  - `("unknown_active"|"none", _)` → nothing.
  - `("no_candidate", _)` → if `now - state.last_noswap_notify_at >= 3600`,
    post the "no fresh account" notification and persist `last_noswap_notify_at`.
  - `("switch", num)` → **cooldown gate**: if `now - state.last_switch_at <
    cooldown`, skip. Else `switch_to(str(num))`, stamp + persist
    `last_switch_at`, post the "swapped" notification, request a UI rebuild.

**Mode B timer:** when auto-switch is enabled and `interval > 0`, start a
`rumps.Timer(interval)` whose callback triggers `refresh_async()` (guaranteeing
a fetch at cadence N even if the display refresh is slower); the cadence gate in
`maybe_auto_switch` then bounds how often it acts. When disabled or in mode A,
the timer is stopped. An **in-flight guard** (a bool set/cleared around the
worker) prevents overlapping refresh workers when display + auto timers coincide.

Manual switches (`_make_switch_to`, `_switch`) also stamp + persist
`state.last_switch_at` so they feed the same cooldown.

## Error handling

- `decide_auto_switch` is pure and total — never raises.
- `switch_to` is wrapped in `try/except ClaudeSwitchError` (covers the
  `SessionError` raised when the *target* account has a live session-mode
  instance): log + post an "auto-switch failed" notification; no crash. The next
  poll re-evaluates.
- `MenuBarState.load` falls back to defaults on missing/corrupt/wrong-typed
  data, mirroring `MenuBarSettings.load`.

## Testing

Unit tests (rumps absent, no network), in `tests/test_menubar.py`:

- `decide_auto_switch`: active under threshold → `none`; active over on 5h only
  and on 7d only → `switch`; best-candidate selection; tie-break by 7d then 5h;
  all-others saturated → `no_candidate`; unknown active usage → `unknown_active`;
  candidate with unknown usage excluded; no other accounts → `no_candidate`.
- `MenuBarState`: round-trip, corrupt/missing → defaults, wrong-typed fallback.
- `MenuBarSettings`: the four new fields load/save and default correctly,
  including the `auto_switch_interval = 0` sentinel.

The cadence/cooldown driver, the mode-B timer, launchd-free notifications, and
the rumps wiring are manual-verify on macOS (AppKit can't run headless), same
policy as the rest of the menu bar.

## One-time machine cleanup (outside the app)

After the feature is built, tested, and the app's auto-switcher is verified
working, remove the obsolete launchd monitor from this machine (it is custom and
exists only here):

1. `launchctl bootout gui/$(id -u)/com.deathemperor.claude-swap-monitor`
   (or `launchctl unload ~/Library/LaunchAgents/com.deathemperor.claude-swap-monitor.plist`).
2. Delete `~/Library/LaunchAgents/com.deathemperor.claude-swap-monitor.plist`.
3. Archive `~/.claude-swap-monitor/` (keep the script + logs as a backup, e.g.
   rename to `~/.claude-swap-monitor.disabled/`), rather than deleting outright.

This is a manual operator step, not part of the shipped code.

## Files touched

- `src/claude_swap/menubar.py` — `decide_auto_switch`, `MenuBarState`, the four
  `MenuBarSettings` fields, the Settings submenu additions, and the driver
  (`maybe_auto_switch`, mode-B timer, in-flight guard, manual-switch stamping).
- `tests/test_menubar.py` — tests for `decide_auto_switch`, `MenuBarState`, and
  the new settings fields.
- `README.md` — document the auto-switch settings under the menu bar section.
