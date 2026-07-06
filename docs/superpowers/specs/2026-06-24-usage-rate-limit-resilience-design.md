# Design: usage-fetch rate-limit resilience

**Date:** 2026-06-24
**Status:** Approved (building)

## Problem

The menu bar (and CLI) frequently show "usage unavailable". Root cause, confirmed
by instrumentation: the Anthropic usage endpoint
(`https://api.anthropic.com/api/oauth/usage`) rate-limits **per IP** with a window
of roughly one minute, returning **HTTP 429 `rate_limit_error`**. The always-on
menu bar fetches **all N accounts in a parallel burst every 60s** (refresh 60s,
auto-switch on); combined with other Anthropic clients on the same IP, this trips
the limit. The code maps 429 to `None` ("usage unavailable") with **no backoff**,
so it keeps polling every 60s and never lets the window reset — self-perpetuating.

Evidence: with the menu bar stopped, a single request recovered in ~90s; all
accounts including idle backups returned 429 (→ per-IP, not per-account).

## Goals

- Stop the self-perpetuating poll-while-rate-limited loop (back off on 429).
- Keep showing the last real numbers during a backoff instead of "unavailable".
- Reduce steady-state request volume so the limit is tripped far less often.
- No behavior change for the CLI's all-accounts usage consumers (`--list`,
  `--switch --strategy`, `--status`).

## Non-goals

- No change to the usage API, headers, or auth flow.
- No new user-facing setting (backoff window and backup-refresh cadence are fixed
  constants — YAGNI).
- No retry/backoff for non-429 failures (they keep current degrade behavior).

## Part A — 429 backoff + last-known-good retention (shared)

Lives in `oauth.fetch_usage_for_account` and `switcher._collect_usage`, so the CLI
benefits too.

### A1. Detect 429 distinctly (`oauth.py`)
`fetch_usage_for_account` currently returns `None` on any error. Change it to
return the string sentinel `"rate limited"` when the usage request fails with
HTTP 429 (both on the first call and the post-refresh retry). Other failures
still return `None`. (`"rate limited"` joins the existing `"no credentials"`
string-sentinel convention; callers already render unknown strings dimly.)
`fetch_usage_for_account` does not sleep or store state — it only reports the 429
via the sentinel; the backoff window is derived in `_collect_usage`. The backoff
is a fixed `_DEFAULT_BACKOFF = 90` seconds (the server's `Retry-After` is not
honored — it adds return-value threading for marginal benefit, and if the server
wants a longer window we simply re-arm the 90s backoff on the next 429).

### A2. Global backoff + last-known-good (`switcher._collect_usage`)
Rate limiting is per-IP, so one 429 backs off **all** accounts.

- **Backoff state file:** `<backup_dir>/cache/usage_ratelimit.json`, written via the
  existing `write_cache`, holding `{"until": <epoch_seconds>}`. Read with an
  effectively-infinite TTL (it's an absolute deadline, not a freshness window).
  Helpers: `_rate_limited_until() -> float` (0.0 if absent/corrupt) and
  `_set_rate_limited_until(ts)`.
- **Skip while limited:** at the top of `_collect_usage`, if
  `time.time() < _rate_limited_until()`, **do not hit the network**. Return each
  account's last-known cached usage (read `usage.json` ignoring the 15s TTL, via
  `read_cache(path, ttl=_FOREVER)`); for accounts with no cached dict, return the
  `"rate limited"` sentinel.
- **Last-known-good retention:** after a live fetch round, build the to-write map
  so that any account whose fresh result is **not** a usage dict (i.e. `None`,
  `"no credentials"`, or `"rate limited"`) falls back to its prior cached **dict**
  if one exists; only when there is no prior dict is the sentinel/`None` stored.
  This stops a transient failure from erasing good numbers.
- **Arm the backoff:** if any fresh result is `"rate limited"`, set
  `until = now + _DEFAULT_BACKOFF` where `_DEFAULT_BACKOFF = 90`.
- The existing 15s same-keys cache-hit shortcut is preserved for the live path.

### A3. UI
No menu-bar change needed: `usage_summary("rate limited")` already passes the
string through (rendered dimly), and retained dicts render as normal numbers. The
title's `_window_pct` returns `None` for a string, so the title simply omits the
number for a never-fetched account during backoff — and shows the retained number
otherwise.

## Part B — active-first polling (menu-bar only)

Cuts the steady-state burst. The CLI path is untouched (it always fetches all).

- **`_collect_usage` gains `only: set[str] | None = None`.** When `only` is a set
  of account-number strings, the live fetch runs **only** for those accounts; every
  other account takes its value from the prior cache (last-known-good). `only=None`
  (default) fetches all — exactly today's behavior, so every CLI caller is unchanged.
- **Menu-bar worker** (`menubar._snapshot` / the app's `_worker`): pass
  `only={active_num}` on each refresh, and `only=None` (full) at most every
  **5 minutes** (`_FULL_REFRESH_EVERY = 300`) and on an explicit "Refresh now".
  The app tracks `_last_full_fetch`; backups render from cache between full
  refreshes. With no active account, it falls back to a full fetch.
- Net steady-state: ~1 request/60s plus an N-request burst every 5 min — well under
  the ~per-minute limit, and any stray 429 is absorbed by Part A.

## Error handling

- Backoff/cache files corrupt or missing → treated as "not limited" / "no cache".
- Non-429 failures unchanged (degrade to last-known-good, else `None`).
- `only` referencing an unknown account is simply ignored (that account isn't in
  `accounts_info`).

## Testing

Unit tests, no network — inject a fake `fetch` / monkeypatch
`oauth.fetch_usage_for_account` and use a `tmp_path` backup dir:

- 429 sentinel arms `usage_ratelimit.json` with `now + 90`.
- While `until` is in the future, `_collect_usage` performs **zero** fetch calls
  and returns last-known-good dicts (and `"rate limited"` for never-cached
  accounts).
- Last-known-good retention: an account that fetched a dict, then fails, keeps the
  dict in cache and in the returned list.
- `only={"1"}` fetches account 1 only; account 2 comes from cache (fetch called
  exactly once, for 1).
- Backoff expiry (`until` in the past) resumes normal fetching.

The menu-bar cadence wiring (`only` selection, `_last_full_fetch`,
`_FULL_REFRESH_EVERY`) is GUI glue verified manually, consistent with the rest of
the app.

## Files touched

- `src/claude_swap/oauth.py` — 429 → `"rate limited"` sentinel.
- `src/claude_swap/switcher.py` — `_collect_usage` backoff, last-known-good
  retention, `only` parameter; rate-limit state helpers; `_DEFAULT_BACKOFF`,
  `_FOREVER` constants.
- `src/claude_swap/menubar.py` — `_snapshot(only=...)`, the worker's active-first /
  5-min-full cadence, `_FULL_REFRESH_EVERY`.
- `tests/test_oauth.py`, `tests/test_cache.py` or `tests/test_switcher.py` — unit
  tests for the new behavior.
- `README.md` — one line noting usage is rate-limit-aware (optional).
