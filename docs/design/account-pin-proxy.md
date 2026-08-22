# Account-Pin Proxy — design

Status: IMPLEMENTED. Written 2026-07-30 as a design draft ("implementation
pending"); the implementation shipped as PR #210, which moved the proxy into
its own `cswap-pin` package behind the `claude-swap[pin]` extra. This document
is kept as the RATIONALE — why a proxy at all, and what the forensics
established — not as a description of the current call graph. Where the two
disagree, the code wins. Runtime failure modes live in
`cloud-pin-failure-modes.md`, which is maintained against the shipped build.

## Problem

cswap swaps the on-disk credential in place so *inference* follows whichever
account is active. But two other operations authenticate with that SAME
credential and therefore also follow the swap, which the user does NOT want:

- **Remote Control (RC)** — a REPL session with `remoteControlAtStartup` creates
  a claude.ai "code session" (`cse_*`). Ownership is fixed at creation by the
  bearer token, and the session re-reads the disk credential on every ~8h
  worker-JWT refresh. So a swap moves RC to the new account: the phone/web loses
  the session (it now lives under a different account) and stale "ghost"
  sessions pile up.
- **Artifacts** ("frames") — published via `POST /api/frame/deploy/init`,
  owned by the creating bearer. A swap makes republish fail (403/404) and the
  artifact "disappears" from the account you're logged into on the web.

Goal: **inference follows the swap; RC and artifacts stay pinned to one chosen
account** — within any session, without changing how the user runs cswap.

## Why a proxy is the only mechanism (established by forensics)

All three operations read a single global credential accessor (`ys()`/`_s()` in
the 2.1.217 binary). There is no per-operation token selector wired to anything,
no multi-account store, no live token override the client honors for RC (the
`CLAUDE_CODE_OAUTH_TOKEN` env is rejected by RC's full-scope gate), and no
RC/artifact lifecycle hook. Verified dead ends: org-uuid header (server derives
ownership from the bearer, not `x-organization-uuid` — live-tested: bearer #2 +
org-header #1 → session owned by #2), trusted-device-token, `CLAUDE_CONFIG_DIR`/
`CLAUDE_SECURESTORAGE_CONFIG_DIR` (isolates the whole session, not per-op), dead
bridge overrides, and hooks.

The ONLY way to split auth per-operation inside one session is to intercept the
HTTP requests and swap the bearer on the specific routes. That means a MITM
forward proxy. Live-tested: creating a code session / listing frames with the
PIN account's bearer (while disk = the other account) yields PIN ownership
(200 for PIN, 401/404 for the other). Inference (`/v1/messages`) is left
untouched, so it keeps following the disk swap.

## Architecture

```
claude session
  HTTPS_PROXY = cswap-proxy         ← cswap wires this (in session.run / TUI launch)
     │
     ▼
cswap-proxy  (NEW, this feature)
  - MITM api.anthropic.com
  - route match:
      /v1/code/sessions*  → replace Authorization: Bearer <PIN token>
      /api/frame/*        → replace Authorization: Bearer <PIN token>
      everything else (esp. /v1/messages) → pass through unchanged
  - chain onward to the PREVIOUS HTTPS_PROXY value (CCF 9901, corp proxy, or
    direct if none)
     │
     ▼
(previous proxy, if any: CCF 9901 → corp 8118) → api.anthropic.com
```

The proxy is generic: it does NOT know about CCF. It reads whatever HTTPS_PROXY
was set before cswap inserted itself and CONNECT-chains through it. Works for
users with CCF, without CCF, and behind a corp proxy.

### Coexistence with other proxies (the three user classes)

| user | prior HTTPS_PROXY | cswap-proxy chains to |
|---|---|---|
| plain (no CCF, no corp) | (unset) | direct `net.connect` to api.anthropic.com |
| CCF user (this user) | `http://127.0.0.1:9901` | CCF at 9901 (CCF then chains to corp) |
| corp-proxy user | `http://corp:8118` | corp proxy |

cswap-proxy captures the inbound HTTPS_PROXY at launch and uses it as its own
upstream. CCF is never modified.

## Components

### 1. `proxy.py` (NEW) — the MITM token-swap proxy

Single-purpose port of CCF's forward-proxy transport, minus caching/extensions/
downloads. Pieces (CCF references filled in from the port-spec analysis):
- CONNECT server; MITM only `api.anthropic.com`, blind-tunnel the rest.
- On-the-fly leaf cert under a generated CA (Python `cryptography`); CA path
  handed back so the launcher can add it to `NODE_EXTRA_CA_CERTS`.
- Parse the decrypted HTTP request; on matched routes rewrite the `Authorization`
  header to the pinned token; relay upstream through the chained proxy.
- Onward chaining: CONNECT-through the captured prior HTTPS_PROXY, or direct.

Pin token source: read from cswap's own backup store for the pinned account
(never the live/default credential that cswap swaps), and refresh it there —
cswap is the single owner of the pin token, so no split-brain with the client.

### 2. Pin state — which account is pinned

Store the pin as an account identity (`(email, organizationUuid)` composite,
mirroring `mappings.py`; slot numbers are not stable). Options, TBD in planning:
a `SETTING_SPECS` entry (section `remoteControl`, key `account`) so
`cswap config get/set` works, and/or a dedicated small store. The pin must be
readable by the proxy and by every launch path.

### 3. Entry points (must all honor the pin)

- **`cswap run` / `cswap` exec path** (`session.py run` / `exec_default`,
  `cli.py _run_command`): if a pin is set, start/reuse the proxy and wire
  `HTTPS_PROXY` + `NODE_EXTRA_CA_CERTS` into the child env before exec.
- **`cswap` config**: `cswap remote-control pin <account>` / `unpin` / `status`
  (new subcommand) and/or a `SETTING_SPECS` key.
- **TUI** (`tui/dashboard.py`): a "Remote-control pin…" menu row following the
  `disable-menu` pattern (pick an account → set/clear pin), plus a badge on the
  pinned account in `AccountsPanel`.
- **Plain `cswap switch` + hand-run `claude`**: the proxy still needs to front
  the session. If the user doesn't launch via cswap, cswap cannot wire the env —
  document that pin requires launching through cswap (or provide a
  `cswap env`-style eval snippet), TBD.

### 4. Proxy lifecycle

One proxy per pinned account, started on demand, shared across sessions,
reaped when idle. Follow a simplified version of CCF's supervise/refcount model
(details from the port spec). No caching state to manage.

## What breaks / limits (accept)

- **Already-misowned RC**: with the proxy fronting, the next ~8h refresh (also an
  api.anthropic.com request) gets swapped to PIN → RC self-heals to PIN
  (new `cse_*`, URL changes once). No manual fix.
- **Already-misowned artifact**: cross-account republish is a hard 403/404 (no
  client fallback-to-create). The proxy makes FUTURE publishes PIN-owned; past
  artifacts under another account stay there (re-publish manually if needed).
- **Double MITM** (CCF users): client must trust cswap-proxy CA *and* CCF CA via
  NODE_EXTRA_CA_CERTS. Manage both.
- **Not launched via cswap** → no env wiring → no pin. Documented constraint.

## Testing

- Unit: route matcher, header swap, chain-target parsing, cert generation
  (no AKI pitfall).
- Integration (Mac + host-a via `~/workspace/cswap`): with disk=account B and
  pin=account A, assert a created code session and a listed frame are A-owned
  (200 for A, 401/404 for B) while a `/v1/messages` call bills B.
- Coexistence: run with CCF present (chain to 9901) and absent (direct).

## PR

Target upstream `realiti4/claude-swap` eventually; keep the proxy self-contained
and CCF-agnostic so it stands alone. Hold PR until validated on both machines.
