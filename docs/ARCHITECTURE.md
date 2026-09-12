# claude-swap: A Codebase Guide

**Audience:** engineers new to this codebase (and to some of the engineering
techniques it uses). This document explains what the tool does, how it is
built, and how to safely extend it. It also calls out *why* certain
patterns are used — atomic writes, cross-process locking, additive JSON
schemas, adaptive polling — as a tour of real-world software engineering
techniques, since those ideas show up constantly outside this project too.

Line numbers below are accurate as of the commit this doc was written
against; if a refactor moves things, treat them as "look near here," not
gospel — the module names and responsibilities are the durable part.

---

## 1. What this tool does

`claude-swap` (CLI: `cswap` / `claude-swap`, package: `claude_swap`) lets a
developer manage **multiple Claude Code accounts** on one machine:

- Swap the account Claude Code is logged in as, without running `/logout`.
- Watch usage and switch automatically before an account hits its rate
  limit (`cswap auto`).
- Run a second account in parallel, in one terminal, without disturbing the
  default login (`cswap run`, "session mode").
- See every account's usage in a live dashboard: a terminal TUI, or a macOS
  menu bar app.

The one-sentence architectural summary: **claude-swap does not reimplement
Claude Code's login. It captures a snapshot of Claude Code's own credential
storage per account, and later replays the right snapshot back into that
same storage** — while being extremely careful never to corrupt it, never to
race a live Claude Code process, and never to destroy a credential it can't
prove it owns.

Read `README.md` first if you haven't — it's the user-facing manual. This
document is the *implementer's* map.

---

## 2. The mental model

Think of claude-swap as three concentric layers:

```
┌─────────────────────────────────────────────────────────────────┐
│  Frontends (thin, interchangeable)                               │
│  cli.py  ·  tui/*.py  ·  menubar.py                               │
│  — parse input, call the core, render output                     │
├─────────────────────────────────────────────────────────────────┤
│  Core orchestration                                               │
│  switcher.py (ClaudeAccountSwitcher) — the ~150-method god object │
│  that every frontend drives. Owns: add/remove/switch/list/alias/  │
│  disable/move/purge, and composes everything below it.           │
│  autoswitch.py (AutoSwitchEngine) — the policy loop for           │
│  proactive switching. Frontend-agnostic; emits typed events.      │
│  session.py (SessionManager) — `cswap run` isolated profiles.     │
├─────────────────────────────────────────────────────────────────┤
│  Storage & correctness primitives (leaf modules)                  │
│  credentials.py, models.py, paths.py, macos_keychain.py,          │
│  migrations.py, transfer.py, mappings.py, fsutil.py               │
│  locking.py (cswap's own lock) + claude_locks.py (piggybacks on   │
│  Claude Code's own locks)                                         │
│  usage_store.py, oauth.py, poll_policy.py, pace.py, cache.py      │
└─────────────────────────────────────────────────────────────────┘
```

Every frontend (`cli.py`, the TUI, the menu bar) is a thin shell around the
**same** `ClaudeAccountSwitcher` instance and, where relevant, the **same**
`AutoSwitchEngine` class. This is why the README can truthfully say the menu
bar and `cswap auto` "stay in sync" — they are not two implementations of
the same idea, they are the *same code*, driven from two frontends, reading
and writing the same `settings.json` / `autoswitch_state.json` files.

**The one invariant that explains almost every hard design decision in this
repo:** Claude Code itself is a live, independent process that may be
reading, refreshing, or writing its own credential files *at the same
moment* claude-swap is trying to switch them. Nearly every subtle piece of
code in `switcher.py`, `claude_locks.py`, and `credentials.py` exists to
make that race either impossible (via locking) or provably safe (via
snapshotting, fingerprinting, and never overwriting bytes it can't
classify as its own).

---

## 3. Directory map

```
src/claude_swap/
  cli.py              argparse entry point; all subcommands dispatch from here
  __main__.py          `python -m claude_swap` shim
  exceptions.py        the ClaudeSwitchError hierarchy
  printer.py            low-level terminal styling primitives (colors, theme)
  json_output.py        --json schema builders (schemaVersion 1)
  logging_config.py     file + console logging setup

  models.py             data model: AccountInfo, AccountSnapshot, Platform, ...
  paths.py               cross-platform on-disk layout resolution
  credentials.py         THE credential storage abstraction (biggest concern here)
  macos_keychain.py       shells out to `security` CLI
  migrations.py           one-time storage-format migrations
  transfer.py             `cswap export` / `cswap import`
  mappings.py             directory → account mappings (`cswap map`)
  fsutil.py               atomic file write/read helpers

  switcher.py            ClaudeAccountSwitcher — the core orchestrator (7000+ lines)
  locking.py              cswap's own advisory file lock
  claude_locks.py         piggybacks on Claude Code's OWN lock files
  session.py              `cswap run` session-mode profiles
  process_detection.py    detect live Claude Code processes/sessions

  oauth.py                OAuth token refresh + Anthropic usage API client
  usage_store.py          per-account usage cache + fetch/backoff state machine
  poll_policy.py           adaptive polling cadence math
  pace.py                 weekly "ahead of pace" / projection math
  cache.py                generic tiny TTL file cache
  snapshot_source.py       the one blocking "take a snapshot" call TUI/menubar use
  autoswitch.py           AutoSwitchEngine — the `cswap auto` policy loop
  update_check.py         PyPI update check + self-upgrade

  settings.py             settings.json schema + `cswap config`
  appearance.py           terminal light/dark background detection (shared)
  launch_agent.py          macOS launchd service management for the menu bar
  menubar.py               macOS menu bar app (rumps)
  tui/                     Textual terminal dashboard
    app.py, dashboard.py, autoview.py, widgets.py, modals.py, theme.py, data.py

tests/                  ~1900 tests, pytest, heavy use of fixtures/fakes
docs/ARCHITECTURE.md     this file
```

---

## 4. Core concepts you need before touching anything

### 4.1 What "an account" is

There's no single `Account` god-object. The data is split by what it's for:

- **`AccountInfo`** (`models.py:78`) — the roster record stored in
  `sequence.json`: `email`, `uuid`, `organization_uuid`, `organization_name`,
  `number` (its slot). This is identity + bookkeeping, not credentials.
- **The credential itself** — a full copy of whatever Claude Code stores
  (`claudeAiOauth: {accessToken, refreshToken, expiresAt, ...}` for OAuth, or
  a raw `sk-ant-api...` string for a managed API key) — lives in a separate
  per-slot **backup** (macOS Keychain, or a `.enc` file elsewhere).
- **`AccountSnapshot`** (`models.py:123`) — a read-only, UI-facing view that
  merges identity + live usage + `disabled`/`alias` for one paint of a
  dashboard.

Accounts are addressed by **slot number** (`1`, `2`, `3`, ...), but slots get
reused (an account can be removed and a new one added to the same slot), so
code that must survive a slot's identity changing underneath it (usage
cache, quarantine state) keys on `(email, organizationUuid)` instead, and
treats a slot whose stored identity doesn't match as "empty."

### 4.2 Where data lives

`paths.py` resolves everything cross-platform:

| Concern | macOS | Linux/WSL/Windows |
|---|---|---|
| Claude Code's own login | Keychain, or `~/.claude/.credentials.json` | file-based |
| claude-swap's backups | `~/.claude-swap-backup/` (legacy layout) | XDG: `${XDG_DATA_HOME:-~/.local/share}/claude-swap/` |
| Per-account credential backup | macOS Keychain (service `claude-swap`) | `.enc` files under `credentials/` |

Everything else — `sequence.json` (roster + rotation order), `settings.json`,
`autoswitch_state.json`, `cache/usage.json`, `mappings.json`,
`sessions/<slot>-<email>/` — lives directly under the resolved backup root.

### 4.3 Live vs. backup, and the "account-independent state" subtlety

This is the single most important correctness idea in the credential layer.
`~/.claude.json`'s credential blob contains **two kinds of data**:

- **Account-specific**: `claudeAiOauth`, `trustedDeviceToken` — this is what
  a switch actually swaps.
- **Machine-shared / account-independent**: MCP server OAuth logins
  (`mcpOAuth`, `mcpOAuthClientConfig`, ...), plugin secrets — these belong to
  *the machine*, not to any one Claude account, and rotate independently.

If a switch naively overwrote the whole credential blob with an old slot's
snapshot, it would silently revert your MCP server logins to whatever they
were the last time that slot was captured. Instead, every activation write
goes through `_prepare_credentials_for_activation()`
(`switcher.py:743`), which recomputes the shared fields from the **live**
machine state and merges them onto the target slot's snapshot
(`credentials.py:216` `shared_credential_fields`, `credentials.py:244`
`merge_shared_credential_fields`). This is exactly the README's "swap only
the account-specific login... account-independent OAuth state is
preserved" behavior.

**Lesson for extending this code:** if you ever add a new field to Claude
Code's credential JSON that claude-swap needs to understand, ask "is this
per-account or machine-shared?" *first* — that answer decides whether it
goes in `ACCOUNT_CREDENTIAL_KEYS` or `SHARED_CREDENTIAL_KEYS`
(`credentials.py:199-213`). Get it backwards and either MCP logins
mysteriously revert on switch, or an account's login leaks across slots.

### 4.4 Two lock systems, always acquired together, always in the same order

claude-swap has to worry about **two** kinds of concurrency:

1. Two claude-swap operations racing each other (two terminals both running
   `cswap switch`).
2. claude-swap racing **Claude Code itself**, which refreshes its own OAuth
   token in the background and holds its own lock while doing so.

`locking.py` implements (1): a plain OS-level advisory lock
(`fcntl.flock` / `msvcrt.locking`), non-reentrant, no staleness logic (the
OS releases it if the holder dies).

`claude_locks.py` implements (2) by **reimplementing Claude Code's own
locking protocol** — the npm `proper-lockfile` scheme, where the lock is a
*directory* (`mkdir` as the atomicity primitive), with staleness detection
and a background touch-thread so a long-held lock doesn't get stolen out
from under it. It targets the exact same lock paths Claude Code uses
(`~/.claude/.oauth_refresh.lock`, the legacy `~/.claude.lock`,
`~/.claude.json.lock`), in Claude Code's own acquisition order (primary
refresh lock, then the legacy one).

Every credential-mutating switch acquires **all three**, in this fixed
order (`switcher.py:6782`):

```python
with FileLock(self.lock_file), claude_credentials_lock(), claude_config_lock():
    ...
```

**This order must never change**, and no code path may re-enter any of
these locks from inside itself (they're not reentrant). Getting this wrong
reintroduces exactly the race the whole scheme exists to prevent: a live
Claude Code token refresh reading pre-swap credentials, refreshing over the
network, and writing its result *after* claude-swap's swap completes —
silently reverting the account or corrupting the backup with a stale
refresh token.

### 4.5 "Never destroy a credential you can't prove you own"

Before overwriting whatever credential currently sits in the live location,
`_classify_outgoing_credential()` (`switcher.py:7032`) figures out whose it
is — `own-bytes` / `own-rotated` / `foreign` / `alien` / `unresolved` / etc.
— and only ever backs it up into *that account's* slot, never blindly
discards it. This closes a historical bug class (#117) where a switch could
silently destroy another account's — or an unmanaged login's — only copy of
a refresh token, with no way to recover except a fresh `/login`. If you
touch the write path of `_perform_switch`, preserve this classify-then-back-up
step; it's not optional ceremony.

---

## 5. Walking through a command end to end

### `cswap switch 2`

1. `cli.py:main()` translates the memorable verb into the legacy flag form
   (`_translate_subcommand`, `cli.py:72`): `["switch", "2"]` →
   `["--switch-to", "2"]`.
2. Dispatch: `switcher.switch_to("2", ...)` (`switcher.py:6198`).
3. Resolves `"2"` to a slot, checks it isn't already active, checks no live
   `cswap run` session for that slot has a *newer* credential generation
   than the stored backup (if so: refuse — activating it would just fail
   with `invalid_grant` on Claude Code's next refresh).
4. Prefetches network identity **before** taking any locks (locks must stay
   pure local I/O — `switcher.py:6771`).
5. Acquires the three locks in order (§4.4), classifies and backs up the
   outgoing credential, merges shared fields onto the target's snapshot
   (§4.3), writes it, updates `sequence.json`, all wrapped in a
   `SwitchTransaction` (`models.py:164`) that rolls back on any exception.
6. Releases locks, prints the result (or returns a JSON payload if
   `--json`), re-arms auto-switch state for the new active account.

### `cswap list`

`switcher.list_accounts()` (`switcher.py:5515`) builds the roster
(`_build_accounts_info`), asks the usage layer for each account's usage
(`_collect_usage_entries` — this is where the *adaptive* polling in
`usage_store`/`poll_policy` decides which accounts are actually worth a
network round trip right now vs. served from cache), then either prints a
human dashboard (usage bars via `printer.py` primitives) or returns a
`schemaVersion: 1` JSON payload built with `json_output.py`'s helpers.

### `cswap auto` (one tick)

`AutoSwitchEngine._tick_inner()` (`autoswitch.py:892`) is the policy brain:

1. Release any quarantines whose credential has since been replaced.
2. Poll usage for the active account (if due) plus **one** stale candidate
   (`_collect_scheduled_usage`) — deliberately O(1) network calls per tick,
   not "poll everyone."
3. If the active account's utilization crosses `threshold`, or usage is
   unreadable for `unhealthy_ticks` in a row, decide a trigger:
   `proactive`, `at-limit`, `failover`, or (if `strategy=consume-first`)
   `consume-first`.
4. Rank candidates (`_rank_candidates`), respecting a hysteresis margin so
   two accounts near the threshold can't ping-pong, and a "no-return" bar
   that refuses to immediately undo the engine's own last move unless the
   account it left has genuinely gotten better or worse since.
5. **Freshen** the chosen target's stored token (refresh it if it's close to
   expiry) before activating it — this is what makes the ~30s macOS
   Keychain cache latency harmless: the new account is already valid by the
   time a running Claude Code picks it up.
6. Switch, persisting cooldown/quarantine state to `autoswitch_state.json`
   under its own lock (so a cron `--once` and a running `cswap auto` loop
   never double-switch).

`cswap auto --once` returns one of four exit codes matching
`TickOutcome` (`autoswitch.py:499`): `0` switched, `1` error, `2` no action,
`3` blocked (wanted to switch, no viable target).

---

## 6. Subsystem deep dives

### 6.1 CLI & dispatch (`cli.py`, `exceptions.py`, `printer.py`, `json_output.py`)

`cli.py` has **no single argparse subparser tree** for the memorable verbs.
Instead, `_translate_subcommand()` (`cli.py:72`) rewrites `cswap list` into
`--list` and hands it to one big `argparse.ArgumentParser` whose "action"
flags sit in a mutually-exclusive group (all `help=SUPPRESS`'d so `--help`
only shows the friendly verbs). A handful of commands that need their own
positional args or subparsers (`run`, `map`/`unmap`, `unclaimed`,
`swap`/`move`, `alias`, `auto`, `config`) are **pre-dispatched** in `main()`
before the big parser is even built, because argparse can't express "either
this subcommand's own grammar, or that mutually-exclusive flag group" in one
tree.

`main()`'s shape: parse → validate flag combinations by hand (dozens of
`parser.error(...)` calls, since argparse's grammar can't express them) →
one big `try/except ClaudeSwitchError` around dispatch → the CLI itself
(never the called module) does `json.dumps(payload, indent=2)` when
`--json` was passed.

`exceptions.py` is a flat hierarchy under `ClaudeSwitchError`. Only the base
class is ever caught at the CLI layer (exit code 1); the subclasses exist so
*internal* code can `isinstance`-narrow (e.g., "was this specifically a lock
timeout, which is safe to retry?").

`printer.py` is **only** the styling primitives layer — color detection,
theme palettes, `error()`/`warning()`, small display helpers. The actual
dashboard text (usage bars, `(disabled)`/`(active)` markers) is assembled in
`switcher.py` using those primitives — worth knowing so you don't go looking
for the dashboard layout logic in the wrong file.

`json_output.py` hand-builds dicts (no dataclasses/TypedDicts) around
`SCHEMA_VERSION = 1`, with a strict **additive-field** convention: a field
like `alias`, `disabled`, `lastGoodUsage`, or the weekly pace fields
(`expectedPct`, `aheadOfPace`, ...) is only added to the dict when it
actually applies, never emitted as `null` as a placeholder. This is what
lets the README promise "the contract is additive: new kinds and fields may
appear, so scripts should ignore unknown ones" — a script written against
today's schema keeps working when a field is added later, because it was
never relying on an exhaustive key list.

### 6.2 Credential storage (`credentials.py`, `models.py`, `macos_keychain.py`, `migrations.py`, `transfer.py`, `mappings.py`, `paths.py`, `fsutil.py`)

`CredentialStore` (`credentials.py:293`) never imports `switcher` — it's a
leaf that the switcher composes, keeping "how credentials are stored" fully
decoupled from "when to switch them."

Reading the *live* credential tries, in order: macOS Keychain OAuth item →
plaintext `.credentials.json` fallback → managed API key (Keychain, then
`primaryApiKey` in `~/.claude.json`). The result is an `ActiveCredentials`
tuple that distinguishes "genuinely absent" from "couldn't check" — this
distinction matters enormously: conflating them risks POSTing an
already-spent refresh token (`invalid_grant`) and wrongly quarantining a
perfectly healthy account.

`macos_keychain.py` shells out to `/usr/bin/security` (a pinned absolute
path, not resolved via `PATH`) rather than using the `keyring` PyPI package
or a native API — because Keychain trusts *the calling binary*, and
`keyring`/Security.framework would anchor trust to the Python interpreter,
which `uv tool upgrade` rebuilds, causing repeated "wants to use your
keychain" prompts. Its four functions
(`get_password`/`set_password`/`delete_password`/`item_exists`) are the
**entire** abstraction boundary — tests substitute an in-memory fake behind
exactly this surface, never touching `CredentialStore`'s logic.

`migrations.py` follows a simple, safe contract: a migration function
returns `True` (done, recorded), `False` (not applicable, not recorded, may
retry later), or raises `MigrationIncomplete` (partial failure, retried next
launch). `run_migrations()` never raises and swallows every exception per
migration — a broken migration must never brick the tool.

`transfer.py` implements `cswap export`/`import` as a plaintext JSON `.cswap`
file (no built-in encryption; pipe through `gpg -c` yourself). Notably, a
plain `cswap import` (no `--force`) will still silently **replace** an
existing slot if that slot is currently quarantined for a dead refresh
token — the import auto-heals it, since a fresh credential invalidates the
old dead verdict.

`fsutil.py`'s `replace_with_retry()` is the one atomic-write primitive most
of the storage layer is built on: `tempfile.mkstemp` in the *same
directory* as the target (so the rename is same-filesystem, hence atomic on
POSIX) → write → `os.close` → `os.replace` (retried past transient Windows
antivirus/indexer sharing violations) → `chmod(0o600)`. **Never write a
credential or config file with a raw `open(path, "w")`** — a crash mid-write
would leave a torn, unreadable, or world-readable file.

### 6.3 The switching engine, locking, and session mode (`switcher.py`, `locking.py`, `claude_locks.py`, `session.py`, `process_detection.py`)

Covered in depth in §4.4 and §5. A few additional things worth knowing:

- **Strategies.** `best` (`switcher.py:5284`) only moves when it can *prove*
  another account strictly beats the current one's headroom — ties favor
  staying put. `next-available` (inline in `switch()`, `switcher.py:6076`)
  just walks rotation order skipping exhausted/disabled accounts.
  `consume-first` (only used by `cswap auto`, in `autoswitch.py:1755`) ranks
  by soonest weekly reset — "burn the account whose quota is about to
  expire anyway" — with headroom as the tiebreaker.
- **Session mode (`cswap run`)** creates an isolated profile under
  `<backup_root>/sessions/<slot>-<email>/` and execs Claude Code with
  `CLAUDE_CONFIG_DIR` pointed at it. If the target is *already* your default
  login, it execs plain `claude` instead (a second live copy of the same
  rotating credential would go stale) — unless `--require-session` demands
  the isolation guarantee. When the session exits, its rotated credential
  is captured back into the slot's stored backup
  (`_adopt_session_credential`, `switcher.py:2830`) so a later `cswap
  switch` doesn't activate a stale copy.
- **`process_detection.py`** cross-checks a recorded PID against its
  recorded start time (not just `os.kill(pid, 0)`) so a crashed Claude Code
  whose PID got recycled by an unrelated process doesn't read as "still
  running." A **display** use (the "Running instances" list) drops
  unreadable records; a **guard** use (blocking `purge`/`move`/a switch)
  must treat "couldn't tell" as "assume live" — never destructive under
  uncertainty.

### 6.4 Usage tracking & auto-switch (`usage_store.py`, `oauth.py`, `poll_policy.py`, `pace.py`, `autoswitch.py`, `cache.py`, `snapshot_source.py`, `update_check.py`)

This is the most heavily-engineered corner of the codebase — the module
docstrings read like incident postmortems, because they are. The core
problem: Anthropic's usage endpoint enforces an undocumented request budget
(measured empirically at roughly 28-30 requests/hour per identity, refilling
only as old requests age out of a trailing hour — not a token bucket). Every
surface (`cswap list`, the TUI, the menu bar, `cswap auto`) must share one
adaptive cadence or they'll collectively trip it.

- **`usage_store.py`** is a per-account table of *last-known-good
  measurement* + *fetch/backoff state*, persisted to `cache/usage.json`. A
  failed fetch never blanks a previously-good measurement — it's
  "stale-on-error," shown with an age (`· 6m ago`), not blanked to nothing.
  Reads never take the write lock; every write is a locked
  read-modify-write, and the module is careful never to hold that lock
  across a network call. A `claim`/`reserve`/`record` three-phase protocol
  (`reserve` under lock → fetch with no lock → `record` under lock, fenced
  by a lease id) lets two collectors (e.g., the TUI and a concurrent `cswap
  auto`) skip an account the other is already fetching.
- **`oauth.py`** does the actual HTTP: token refresh
  (`try_refresh_oauth_credentials`) and the usage-API GET
  (`request_usage_data`/`fetch_usage`). It classifies failures precisely —
  `invalid_grant` (dead refresh-token lineage, permanent) vs. `transient`
  (network blip, retry later) — because misclassifying a transient failure
  as permanent would wrongly quarantine a perfectly good account.
- **`poll_policy.py`** computes each account's next poll interval:
  faster when an account is moving toward the switch threshold (down to a
  60s "urgent" floor), slower when idle (decaying toward 5-10 minutes), with
  AIMD-style congestion backoff after a 429 (each success shrinks the
  interval, each 429 grows it multiplicatively) — the same idea TCP uses to
  fair-share bandwidth with no shared state between competing senders,
  applied here to a shared per-account request budget with no way to see
  what other machines/processes are doing.
- **`pace.py`** is a small, pure module: "ahead of pace" means an account
  has used more of its weekly window than the fraction of the week that has
  elapsed. Deliberately excluded from the 5-hour window (resets too fast for
  the idea to mean anything) and gated to never fire in the first 24h after
  a reset (otherwise near-zero elapsed time makes almost any usage look
  "far ahead"). The linear-projection fields (`projectedExhaustionAt`,
  `willLastToReset`) are `--json`-only by design — a linear extrapolation
  against bursty real usage is too imprecise to present as fact in a human
  UI.
- **`autoswitch.py`**'s `AutoSwitchEngine` composes all of the above into
  the policy loop described in §5. Its anti-flap logic (hysteresis margins,
  a "no-return" bar that won't immediately undo its own last move, a
  reset-based ranking axis that only engages once every account is near its
  limit) is the most intricate code in the repo — if you need to touch it,
  read the extensive docstrings in place first; they document specific
  measured incidents the current logic exists to prevent.
- **`cache.py`** is a tiny generic TTL file cache (`read_cache`/`write_cache`)
  used for things far simpler than usage — currently just
  `update_check.py`'s "have I checked PyPI in the last 24h" cache.
- **`snapshot_source.py`** is the one blocking "take a coherent snapshot"
  entry point the TUI and menu bar both call from a background thread —
  never from a UI event loop, since it does file locks, Keychain
  subprocesses, and network I/O.

### 6.5 TUI & menu bar (`tui/*.py`, `menubar.py`, `appearance.py`, `settings.py`, `launch_agent.py`)

Both are thin frontends over the same core. The TUI (`tui/app.py`'s
`CswapApp`) polls on a `set_interval` timer, running fetches in a Textual
thread worker and marshaling results back via `call_from_thread` — the UI
thread itself never touches locks or the network. Mutating actions
(switch/add/remove/disable) go through the same `_start_action` →
background-thread → `call_from_thread` pattern, so the UI never blocks.

The menu bar (`menubar.py`, uses `rumps`) mirrors this with `rumps.Timer`s
instead of Textual's event loop, and — notably — when you enable "Auto-switch
accounts" in its Settings menu, it constructs a real `AutoSwitchEngine` and
runs `engine.run_loop()` in a background thread. It is not a separate
reimplementation of the policy; it's the identical class `cswap auto` uses,
reading and writing the identical `settings.json`/`autoswitch_state.json`.

`settings.py` centers on one table, `SETTING_SPECS`
(`settings.py:102`) — every `cswap config` key's section, JSON name, type,
valid range, and help text in one place. Both the lenient loader (clamps bad
values with a warning) and the strict CLI validator (`cswap config set`
raises loudly) read from this same table, so they can't drift apart. **This
is the pattern to copy when adding a new setting** (see §7).

`appearance.py` is the single shared light/dark terminal-background
detector both the plain CLI printer and the TUI rely on — deliberately not
duplicated.

---

## 7. Extending the codebase: concrete recipes

### Add a new CLI subcommand

Two shapes, both in `cli.py`:

- **Fits the flag model** (like `status`, `purge`): add it to
  `_SUBCOMMAND_FLAGS` (`cli.py:50`), add the `--foo` flag to the
  mutually-exclusive group, add an `elif args.foo:` dispatch branch that
  calls a new `ClaudeAccountSwitcher` method, and (if JSON-capable) build the
  payload via `json_output.py` helpers rather than printing JSON yourself —
  `main()` already does `json.dumps(payload, indent=2)` once, uniformly.
- **Needs its own positional args/subparsers** (like `map`, `alias`,
  `auto`, `config`): write a `_foo_command(argv) -> None` following the
  `_map_command`/`_alias_command` template (own `ArgumentParser`, own
  try/except around the same `ClaudeSwitchError`/`KeyboardInterrupt`
  handling), then register a pre-dispatch check in `main()` alongside the
  existing ones (`if argv and argv[0] == "foo": _foo_command(argv[1:]); return`).

Either way: raise the most specific existing `ClaudeSwitchError` subclass
(or add a new one) — never a bare `Exception` — so it's caught uniformly.

### Add a new `cswap config` key

1. Add a field with a sensible default to `AutoSwitchSettings` or
   `UiSettings` (`settings.py:32`/`:62`).
2. Add one `SettingSpec(...)` entry to `SETTING_SPECS` (`settings.py:102`):
   section, JSON key, field name, type, valid range/choices, help text.
3. That's it for an existing section — `list`/`get`/`set`/`unset`/`path`,
   validation, and clamped loading all iterate the spec table generically.
4. If a running engine/TUI needs to *see* a CLI-flag override merged in, add
   a case to `merged_with_cli()` (`settings.py:424`).

### Add a new switch strategy

Strategies live in two places depending on who uses them: `switcher.py`
(`best`, `next-available` — usable from `cswap switch --strategy`) or
`autoswitch.py` (`consume-first` — only meaningful for the proactive
engine). Model a new strategy on the existing ranking functions
(`_select_best_switchable` / `_rank_candidates`): compute
`oauth.account_headroom(usage, models)` per candidate, never treat `None`
headroom as "exhausted" (it means "unknown," not "zero"), and if it's a
proactive/automatic strategy, think hard about hysteresis — a strategy with
no anti-flap margin will oscillate between two near-tied accounts.

### Add a new storage migration

Write a `(switcher) -> bool` function matching the contract in
`migrations.py` (`True`=applied, `False`=not applicable/skip, raise
`MigrationIncomplete`=partial, retry next launch), make it idempotent and
self-guarding against a missing/corrupt `sequence.json`, and append
`(migration_id, fn)` to the `MIGRATIONS` list. Never let it raise anything
else — `run_migrations()` must not brick the tool on a broken migration.

### Add a new dashboard widget / column (TUI)

Per-account data goes into `account_card_text`/`mini_account_text`
(`tui/widgets.py:163`/`:243`); panel-level additions go into
`DashboardScreen.compose()` with a `self.watch(self.app, "snapshot", ...)`
handler to redraw on new data. If it needs new source data, extend
`AccountSnapshot` in `models.py` and populate it in `snapshot_source.py`.
Keep formatting helpers in `tui/data.py` next to `format_duration`/
`reset_text` so the CLI and TUI can share the exact same text, rather than
inlining string formatting in the widget.

---

## 8. Software engineering ideas worth learning from this codebase

Beyond "how claude-swap works," several patterns here are broadly useful
and worth internalizing:

**Atomic writes, always** (`fsutil.py`). Never `open(path, "w")` a file
another process might read concurrently. Write to a temp file in the same
directory, then `os.replace` (atomic rename on POSIX) into place. A crash or
concurrent read mid-write can never see a torn file.

**Piggyback on someone else's lock, don't invent a competing one**
(`claude_locks.py`). When two independent programs share a resource, the
safe move is not "add your own lock and hope the other program checks it
too" — it's reimplementing *their* exact locking protocol so you queue
behind them the same way any other instance of their own program would.

**Separate "I don't know" from "it's false"** (used constantly in
`usage_store.py`/`oauth.py`/`autoswitch.py`). `account_headroom()` returns
`None` for "can't tell," never `0` — because a caller that treats unknown as
zero will wrongly skip a perfectly fine account, or wrongly quarantine one
on a transient network blip.

**Additive schemas** (`json_output.py`). A public JSON contract should only
ever *add* optional fields, never repurpose or remove one — this is exactly
what lets old scripts keep working forever against a growing API.

**Classify before you destroy** (`_classify_outgoing_credential`). Before
overwriting shared state, prove you understand whose it is. "I don't
recognize this, so I'll assume it's safe to overwrite" is how #117-class
bugs happen.

**Idempotent, self-healing migrations** (`migrations.py`). A migration that
can't tell "already done" from "needs doing" will corrupt data on a second
run; one that can't catch its own failure will brick the tool for every
future launch. Both properties are required, not optional extras.

**Borrow control theory for shared, invisible budgets**
(`poll_policy.py`'s AIMD backoff). When multiple independent processes share
a rate limit they can't directly observe (no "requests remaining" header,
just an eventual 429), multiplicative backoff / additive increase is the
standard, proven way to converge to fair-share without any coordination
between them — the same idea TCP congestion control uses.

**Test isolation via a process-wide audit hook, not just fixtures**
(`tests/conftest.py`). A background thread that outlives its own test's
teardown can still see the *real* `$HOME` after a `monkeypatch` context
manager has unwound — a classic testing hazard for any code with threads
started inside a test. This repo's conftest installs a permanent
`sys.addaudithook` that refuses any write outside a frozen set of test-safe
roots, deliberately raising a *plain* `Exception` (not `OSError`) so it
can't be silently swallowed by code that does
`mkdir(parents=True, exist_ok=True)`. Worth reading in full
(`tests/conftest.py`) if you've ever wondered how to make "tests can never
touch real user data" actually airtight rather than aspirational.

---

## 9. Testing

- Run everything: `uv run pytest` (or `pytest` inside `uv run` / an active
  venv). Runs in parallel by default (`-n auto --dist loadgroup`, from
  `pyproject.toml`) — the suite is ~1900 tests of mostly-idle work (file
  locks, subprocess fakes, Textual pilots), so parallelism helps a lot.
- Run one file: `uv run pytest tests/test_switcher.py`.
- Run one test: `uv run pytest tests/test_switcher.py::test_name -x`.
- Two opt-out markers exist for tests that deliberately bypass the default
  fakes: `no_keychain_fake` (test mocks subprocess itself, or runs against a
  real temp keychain in CI) and `no_oauth_profile_fake`.
- CI (`.github/workflows/ci.yml`) runs the full suite on Linux, Windows, and
  macOS — the macOS job runs everything (not a narrow Keychain-only subset;
  a comment there explains why a narrower selection was previously a blind
  spot, not a saving).
- **Never write a test that can touch the real account store.** The
  `conftest.py` audit-hook guard (§8) will refuse it and fail loudly — that
  refusal is the point, not a bug to work around.

---

## 10. Glossary

- **Slot / account number** — the small integer (`1`, `2`, ...) a rotation
  position is addressed by. Reused after removal; not a stable identity.
- **Headroom** — `100 - utilization` of an account's *binding* (worst)
  usage window. `None` means "unknown," never "zero."
- **Binding window** — whichever of an account's 5h/7d/per-model windows has
  the highest utilization; the one actually gating requests.
- **Quarantine** — an account marked as having a provably dead refresh
  token (`invalid_grant`), excluded from fetching and switching until a
  fresh `/login` + `cswap add` replaces its credential.
- **Consume-first** — the `cswap auto` strategy that proactively burns the
  account whose *weekly* quota resets soonest, rather than staying on the
  current account as long as possible.
- **Hysteresis** — a required margin (percentage points, or a ratio) a
  candidate must clear before the engine will move to it, so two accounts
  hovering near a threshold can't switch back and forth every tick.
- **Session mode** — `cswap run`'s isolated Claude Code launch under a
  private `CLAUDE_CONFIG_DIR`, letting one account run in parallel with the
  default login.
- **Freshen** — refreshing a target account's stored token *before*
  activating it, so it's already valid by the time Claude Code's Keychain
  cache catches up.
