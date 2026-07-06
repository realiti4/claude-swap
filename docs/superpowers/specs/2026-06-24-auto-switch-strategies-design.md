# Design: auto-switch hysteresis, false-alarm fix, and consume-first strategy

**Date:** 2026-06-24
**Status:** Approved (building)

## Summary

Enrich the menu-bar auto-switcher with three ideas distilled from upstream PR #69
(re-implemented for our rumps host — no code copied):

1. **5h hysteresis** — a sticky "blocked" set with a dead band so switching no
   longer thrashes when an account's usage dithers around the threshold.
2. **False-alarm fix** — distinguish "all peers truly exhausted" from "a peer's
   usage couldn't be read", and suppress the "no fresh account" notification in
   the latter (transient) case.
3. **`consume-first` strategy** — a proactive alternative to our threshold-only
   ("reactive") switching: keep the user on the account whose **weekly window
   resets soonest** (use-it-or-lose-it), switching as resets re-order the queue.

All changes live in `src/claude_swap/menubar.py`, extending the existing
pure-function + `MenuBarState` + settings + glue structure.

## Goals

- Eliminate auto-switch thrash near the threshold (hysteresis).
- Stop false "no fresh account" notifications on transient peer read failures.
- Offer `consume-first` as a selectable strategy, defaulting to the current
  `reactive` behavior (no change unless the user opts in).

## Non-goals

- No daemon/CLI/launchd plumbing from #69 (rumps is our host; we keep our menu-bar
  auto-switcher).
- No offline state machine (our per-IP 429 backoff + last-known-good already
  covers stale-data avoidance).
- No `resets_at`-aware timer shortening, no separate 5h/7d threshold settings
  (YAGNI — one threshold control).

## Design decisions (approved)

- Hysteresis is a **fixed constant** `AUTO_HYSTERESIS = 5.0` (not a setting).
- `consume-first` reuses the single `auto_switch_threshold` for both the 5h block
  threshold and the 7d cap.
- The existing **cooldown still applies** to `consume-first` (reuses
  `plan_auto_switch`); since consume-first only switches when the optimal account
  changes, the cooldown rarely binds but guards against dithering.

## Decision-outcome vocabulary (shared)

Both `decide_auto_switch` (reactive) and `decide_consume_first` return
`tuple[str, int | None]`, one of:

- `("switch", num)` — switch to `num` (subject to cooldown in `plan_auto_switch`).
- `("none", None)` — nothing to do (active has headroom / is already optimal).
- `("unknown_active", None)` — the active account's usage is unreadable; do nothing.
- `("no_candidate", None)` — every other account is **truly exhausted**; fire the
  rate-limited "no fresh account" notification.
- `("no_candidate_unverifiable", None)` — a peer's usage couldn't be read this
  tick; **silent** (retry next tick).
- `("all_session_limited", None)` — *(consume-first only)* peers have weekly room
  but are all 5h-blocked; **silent** (a 5h reset will reclaim one).

`plan_auto_switch` maps: `switch` → cooldown gate; `no_candidate` → notify
(rate-limited window); everything else → noop.

## Components

### Settings (`MenuBarSettings`)
- New field `auto_switch_strategy: str = "reactive"`.
- New constant `AUTO_STRATEGY_CHOICES = ("reactive", "consume-first")`.
- `MenuBarSettings.load` already type-checks generically, so the string field
  loads without changes.

### State (`MenuBarState`)
- New field `blocked: list[str] = field(default_factory=list)` — account numbers
  currently considered "at limit" (sticky, persisted; used as the hysteresis set).
- `MenuBarState.load` must be generalized: it currently only coerces numeric
  fields. Extend it to also accept a `list[str]` value for a list-typed field
  (validate every element is a string; otherwise keep the default). Float fields
  keep their int→float coercion.

### Pure helpers (unit-tested)
- `AUTO_HYSTERESIS = 5.0`
- `next_blocked(limiting_by_account, threshold, hysteresis, prev_blocked) -> frozenset[str]`
  — an account enters the set when its limiting % `>= threshold`; leaves only when
  it drops below `threshold - hysteresis`. A `None` limiting value (unknown usage)
  **carries** the prior membership (a network blip never unblocks).
- `_resets_at_ts(window) -> float` — parse a usage window's `resets_at`
  (ISO-8601) to a POSIX timestamp; missing/unparseable → `float("inf")` (ranked
  last). Uses `datetime.fromisoformat`.

### `decide_auto_switch` (reactive) — refactor
New signature `decide_auto_switch(accounts, threshold, blocked) -> (str, int|None)`.
- Limiting metric = worst-of(5h, 7d) (`_worst_pct`).
- Active unreadable → `unknown_active`; active worst `< threshold` → `none`.
- Else candidates = other accounts whose worst is **eligible** under hysteresis:
  `worst < (threshold - AUTO_HYSTERESIS)` if the account is in `blocked`, else
  `worst < threshold`. Track `any_unverifiable` for peers with unreadable usage.
- No candidates → `no_candidate_unverifiable` if any peer was unverifiable, else
  `no_candidate`. Else pick min worst (tie-break lower 7d then 5h) → `switch`.
- `blocked` defaults to an empty set so existing call sites/tests still pass.

### `decide_consume_first` — new
`decide_consume_first(accounts, threshold, blocked) -> (str, int|None)`.
- Active unreadable (missing 5h or 7d) → `unknown_active`.
- For every account (active included), eligible when: 5h is **not blocked**
  (`five < threshold - AUTO_HYSTERESIS` if in `blocked` else `five < threshold`)
  AND `seven < threshold`. Track `any_unverifiable` (unreadable peers) and
  `any_weekly_room` (any account with `seven < threshold`, even if 5h-blocked).
- Eligible set empty:
  - `any_unverifiable` → `no_candidate_unverifiable` (silent),
  - else `any_weekly_room` → `all_session_limited` (silent, temporary),
  - else → `no_candidate` (notify; truly exhausted).
- Else rank eligible by `(_resets_at_ts(seven_day), worst_pct, rotation_index)`
  ascending; the minimum is `optimal`. If `optimal` is the active account →
  `none` (already optimal); else → `("switch", optimal)`.

### `plan_auto_switch` — tweak
Only change: it already notifies on `no_candidate`; ensure
`no_candidate_unverifiable` and `all_session_limited` map to `noop` (no
notification). `switch`/cooldown unchanged.

### Glue (`MenuBarApp`, manual-verify)
- A **"Auto-switch strategy ▸"** radio submenu (Reactive / Consume-first) with a
  `_make_strategy(name)` callback that persists and rebuilds.
- `_auto_tick` / `_maybe_auto_switch`:
  - Compute the per-account limiting % for the active strategy (worst-of for
    reactive, 5h for consume-first), call `next_blocked`, store the result in
    `self.state.blocked` (persisted).
  - Dispatch: `decide_auto_switch(..., blocked)` for reactive,
    `decide_consume_first(..., blocked)` for consume-first; then `plan_auto_switch`.
  - For `consume-first`, the tick must fetch **all** accounts (ranking needs every
    account): use `full=True` for the refresh that feeds a consume-first eval.

## Error handling

- All `decide_*` / `next_blocked` / `_resets_at_ts` functions are total — never
  raise (guard non-dict usage, missing windows, non-numeric pct, bad `resets_at`).
- Unknown `auto_switch_strategy` value falls back to `reactive`.
- `MenuBarState.load` keeps defaults on any malformed `blocked` value.

## Testing

Unit tests (no rumps/network), in `tests/test_menubar.py`:
- `next_blocked`: enter at `>= threshold`; stay until `< threshold - hyst`; exit
  below the dead band; `None` carries prior membership.
- `_resets_at_ts`: valid ISO → timestamp ordering; missing/garbage → `inf`.
- `decide_auto_switch` with `blocked`: hysteresis keeps a borderline candidate
  eligible/ineligible; `no_candidate` vs `no_candidate_unverifiable`; existing
  reactive cases still hold with `blocked=frozenset()`.
- `decide_consume_first`: ranks by soonest 7d reset; tie-break headroom then
  rotation; stay when active is optimal; `all_session_limited` vs `no_candidate`
  vs `no_candidate_unverifiable`; `unknown_active`.
- `plan_auto_switch`: `no_candidate` notifies; `no_candidate_unverifiable` and
  `all_session_limited` are noop; `switch` cooldown unchanged.
- `MenuBarState` round-trips `blocked`; malformed `blocked` → default.

The glue (strategy dispatch, full-fetch for consume-first, menu submenu) is
manual-verified on macOS, consistent with the rest of the app.

## Files touched

- `src/claude_swap/menubar.py` — settings field + strategy choices + hysteresis
  constant; `MenuBarState.blocked` + generalized `load`; `next_blocked`,
  `_resets_at_ts`, `decide_consume_first`; `decide_auto_switch` /
  `plan_auto_switch` refactor; glue (strategy submenu, dispatch, blocked update,
  consume-first full-fetch).
- `tests/test_menubar.py` — tests for all the above.
- `README.md` — document the strategy choice under the menu-bar section.
