# Account-Pin Proxy — design

Status: IMPLEMENTED, in the `cswap-pin` package behind the `claude-swap[pin]`
extra. Kept as the RATIONALE -- why a proxy at all, and what the forensics
established -- never as a description of the call graph; where the two
disagree, the code wins. The draft's component breakdown and entry-point plan
were cut when they shipped, because a superseded plan reads as documentation.
Runtime failure modes live in `cloud-pin-failure-modes.md`, maintained against
the shipped build.

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
- **Artifacts** ("frames") — published under `/api/frame/*`, owned by the
  creating bearer. A swap makes republish fail (403/404) and the artifact
  "disappears" from the account you're logged into on the web.

Goal: **inference follows the swap; RC and artifacts stay pinned to one chosen
account** — within any session, without changing how the user runs cswap.

## Why a proxy is the only mechanism (established by forensics)

All three operations read a single global credential accessor. There is no
per-operation token selector wired to anything,
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
  - route match — the OWNERSHIP routes, NOT a prefix rule (see below):
      /v1/code/sessions[/…] , /v1/sessions[/…]  → replace Authorization: Bearer <PIN token>
      /v1/environments , …/bridge[/…] ,
        …/<env>/bridge/reconnect               → replace Authorization: Bearer <PIN token>
      /api/frame/*                             → replace Authorization: Bearer <PIN token>
      NOT swapped, reached by a pattern and refused by a guard:
        /v1/(code/)?sessions/<id>/(worker[/…]|client/presence) , ?beta=true UNDER /v1/environments
      NOT swapped, reached by nothing (no guard, see below):
        /v1/environments/<env>/work/*
      everything else (esp. /v1/messages)      → pass through unchanged
    (a summary, not the list — `is_pinned_route` owns a few more, e.g.
     /api/oauth/validate and /v1/ultrareview/…)
  - chain onward to the PREVIOUS HTTPS_PROXY value (a local caching proxy, a
    direct if none)
     │
     ▼
(previous proxy, if any: local cache → outbound proxy) → api.anthropic.com
```

The proxy is generic: it knows about no particular next hop. It reads whatever HTTPS_PROXY
was set before cswap inserted itself and CONNECT-chains through it. Works for
users with a local caching proxy, without one, and behind an outbound proxy.

### Two families, because Remote Control has two front doors

`/remote-control` inside the REPL and `claude remote-control` on the command
line are different code paths, and they own their sessions through different
routes:

| entry point | it creates | ownership route |
|---|---|---|
| `/remote-control` in the REPL | a bridge on the current session | `POST /v1/code/sessions/<id>/bridge` |
| `claude remote-control` | an ENVIRONMENT this machine offers | `POST /v1/environments/bridge` |

The second family is the environment's OWNERSHIP routes: register
(`POST /v1/environments/bridge`), deregister (`DELETE .../bridge/<env>`) and
`bridge/reconnect`. Each goes through the one auth wrapper that reads
`getAccessToken()`, so each has to follow the pin -- unpinned, the machine
registers under the active account, and ownership is fixed at creation.
No longer inherited: the collection read below shows the registered machine
under the pinned account and not under the active one, so the register did
follow the pin. The header builder is NOT the discriminator -- the `work/`
calls below share it.

The bare collection read is pinned too but for a different reason: it creates
nothing and mints nothing, yet asked as the active account it answers 200 with
the WRONG account's environments, so the pinned machines are simply absent and
nothing looks broken. Traced here, not by analogy: the same request with the
active bearer and with the pinned one returns 200 both times, with disjoint
contents.

Two neighbours stay OUT, and they stay out by DIFFERENT means:

- the `work/` queue the machine polls (`poll`, `ack`, `stop`, `heartbeat`),
  whose calls take a token as an ARGUMENT instead of reading the bearer.
  Measured against an environment this pin had just registered: the register
  answered fine swapped, and the next `work/poll` on that same environment
  answered 401 swapped and 200 with the bearer it arrived with. Ownership is
  still the pin's, because the register is; the queue is not an ownership
  route. Excluded by the ownership pattern not reaching it, NOT by a guard,
  so widening that pattern silently removes the protection.
- anything spelled `?beta=true` under `/v1/environments`, which is the
  managed-agents SDK sharing the path space with a different credential.
  This one IS a guard: an explicit clause beside the pattern.

The rule both express: swap a bearer only where it has been read, never
because a prefix happened to reach.

### The route table is not the whole answer: there are two ways in

A pinned route only gets swapped on a path that reads the bearer, and the
proxy has two:

| how the client speaks | what the proxy does |
|---|---|
| `CONNECT api.anthropic.com:443` then TLS | MITM, read each request, swap a pinned route |
| `POST https://api.anthropic.com/... HTTP/1.1` (absolute form) | plain relay, read the request line, swap a pinned route (https to the upstream host only) |

The absolute form is what the Remote Control bridge client uses -- measured on
the wire, the registration leaves as
`POST https://api.anthropic.com/v1/environments/bridge` with the OAuth bearer
in the clear. That branch existed for the auto-updater and telemetry and
relayed verbatim, so adding the routes to the table changed nothing: the
requests never reached the code that consults it.

Both paths now take the same decision from the same predicate, and both trace
it -- the MITM for every request, the relay only for the ones it pins.

### Coexistence with other proxies (the three user classes)

| user | prior HTTPS_PROXY | cswap-proxy chains to |
|---|---|---|
| plain (no next hop) | (unset) | direct `net.connect` to api.anthropic.com |
| a user behind a local caching proxy | `http://127.0.0.1:<its port>` | that proxy (which may itself chain onward) |
| behind an outbound proxy | `http://proxy.example:8080` | that proxy |

cswap-proxy captures the inbound HTTPS_PROXY at launch and uses it as its own
upstream. The next hop is never modified.
