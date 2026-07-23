# Design: refresh the active account's expired token under Claude Code's own lock protocol

Status: draft — design agreed, implementation not started.
Grew out of the PR #152 field measurements; see the discussion there.

## Problem

On an idle machine the active account's access token can expire with nobody
refreshing it:

- cswap defers to the owner — any live Claude Code process (#62);
- the owner is idle, so it issues no API request and never triggers its own
  refresh;
- the background daemon may not complete OAuth headless ("headless daemon
  cannot complete OAuth", observed in daemon.log).

The autoswitch engine then walks `USAGE_TOKEN_EXPIRED` → 30-min idle-hold →
failover. Worse, with the token dead, the identity probe
(`fetch_oauth_profile`) 401s, so the outgoing-credential classifier falls to
`unresolved` and a *manual* switch to the account is bounced back off it —
measured at 56 seconds per bounce, repeatedly, on an otherwise healthy
account. Each bounce writes a "pre-fix backup"; in the observed incident this
left the slot's backup (both `.enc` and `.enc.prev`) with empty token fields.

#166 (`cswap recover`) fixes the manual side and deliberately leaves
auto/watch alone — the automatic path still oscillates.

## Why touching an owner-held token is safe (CC 2.1.218, verified in binary)

Claude Code's refresh path is built to *adopt* an external rotation, not
collide with it:

- it takes **two** locks before any credential write:
  `<config-home>/.oauth_refresh.lock` (primary) then legacy `~/.claude.lock`,
  both `stale: 60000 / update: 5000` (`uKi`/`CKi` in the 2.1.218 bundle);
- under the lock it re-reads the store and returns "refreshed" **without a
  network call** when the token already changed
  (`tengu_oauth_token_refresh_race_resolved`);
- its 401 path re-reads the store and adopts a changed token before forcing
  re-auth (`tengu_oauth_401_recovered_from_keychain`);
- it holds both locks **across its own network token exchange**.

So a rotation performed under CC's exact protocol is serialized against CC and
then adopted by it. The safety therefore *requires* following that protocol —
which current cswap does not:

- `claude_locks.py` contends only the legacy `~/.claude.lock`, never
  `.oauth_refresh.lock`, and treats the lock stale at **10s** where CC holds
  it legitimately up to 60s (steal window when CC's toucher stalls >10s);
- `_fetch_active_usage`'s no-owner refresh consumes the refresh grant **with
  no lock held**, then `persist_active` may *discard* the rotated credential
  when an owner appears mid-refresh — stranding a consumed generation as the
  live token. If that generation is ever re-presented, endpoint reuse handling
  can wipe the credential (the empty-token end state above). This hazard exists
  today, before any of this design.

## Change

1. **claude_locks.py — track CC's current protocol.**
   - acquire `<config-home>/.oauth_refresh.lock` first, then the legacy lock —
     CC's order;
   - credentials-lock staleness 10s → **60s**; touch cadence stays ≤5s;
     config lock (`~/.claude.json.lock`) stays 10s — its CC-side defaults
     differ;
   - correct the module docstring (it attributes 10s/5s to `~/.claude.lock`,
     which matches only the config lock).

2. **switcher.py — make the active-refresh sequence CC's own.**
   Replace consume-unlocked → persist-under-lock → maybe-discard with:
   acquire both locks → re-check owner + lineage **before** the POST →
   POST → **persist unconditionally** (active store + slot backup) → release.
   No discard branch after a consumed grant — the invariant is "never leave a
   consumed generation as the live credential" (same one `_freshen_target`
   already states for candidates).

3. **Extend to owner-held-but-expired.** With the sequence above, "an owner
   exists" stops being a reason to skip: a concurrently refreshing CC is
   serialized by the locks and adopts the rotation. The idle-expiry stall and
   the 401-driven `unresolved` bounce disappear because the token never stays
   dead.

## Non-goals / honest scope

- Does **not** fix #164's import/export mechanisms (stale `import --force`,
  cross-machine lineage merges). It removes the *steady-state motivation* for
  syncing; provisioning, dead-lineage repair, and the ~4-week refresh-token
  expiry remain sync/login territory.
- Does **not** replace #166 — `cswap recover` stays the explicit manual tool;
  this covers the automatic path it leaves out.
- Bridge-connected CC sessions already run a proactive refresh timer; this
  matters mainly for machines without one.

## Pre-implementation measurements still owed

1. **(decisive) refresh-token reuse semantics** — present the same refresh
   token twice on a disposable account: grace window vs family revocation.
   Revocation confirms "persist unconditionally" as a hard invariant.
2. Rotation preserves `refreshTokenExpiresAt` (single observation so far).
3. Lock interop under contention: cswap holding both locks 30-59s while CC
   attempts a refresh — expect ELOCKED-retry then clean `lock_timeout`,
   no steal, no `onCompromised`.
4. Re-run the idle-expiry scenario on the fixed build: expect no
   `USAGE_TOKEN_EXPIRED` sentinel, no idle-hold, no 56s flap.

## Test plan

- lock: both directories acquired in CC's order; no takeover before 60s;
  takeover after; release removes both; `ClaudeCodeLockTimeout` surfaced
  cleanly at every call site.
- refresh: owner present + expired → refresh happens under locks and persists;
  owner appears mid-sequence → still persists; CC holds the lock → bounded
  wait then timeout.
- regression: the existing no-owner path also holds locks across the exchange
  (the shared persist path is fixed, not a new parallel branch).

## Key code references

- `src/claude_swap/claude_locks.py` 42-43 (staleness), 52-61 (paths),
  9-24 (docstring to correct)
- `src/claude_swap/switcher.py` 2487-2639 (owner gating + `persist_active`),
  4591-4611 (`unresolved` pre-fix backup branch)
- `src/claude_swap/autoswitch.py` 549-643 (`_freshen_target` — the
  persist-first invariant to replicate)
- CC bundle `~/.local/share/claude/versions/2.1.218` — `uKi` (lock opts),
  `CKi` (dual acquisition), refresh race-resolved / 401-recovery paths
