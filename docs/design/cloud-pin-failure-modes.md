# Cloud pin — when does the pin NOT hold?

Every way the pin can be silently absent, defeated, or lost, with the
current behaviour and whether a session can recover without restarting.
Written from a code audit plus live measurement on a mac; section C was
re-verified against the shipped implementation, which is when C4 was
added and C2's mechanism corrected.

**Sections A, B, D, E are as first written and still hold.** B4 in
particular is still an OPEN item, not a fixed one: `remove_account` does
not clear the pin today either. Where a row names a fix, it names the
layer the fix lives at — C2 is the case where that mattered, because the
mechanism moved after the row was written and the row kept describing the
superseded one.

The pin is **fail-open by design**: when anything below trips, the request
leaves with the session's own bearer instead of failing. That keeps a
proxy problem from taking the session down, and it is why every case here
needs to be *visible* — a silent fallback is what made two of tonight's
verification runs read wrong.

## A. The proxy is never wired in

| # | Case | Behaviour | Recover without restart? |
|---|---|---|---|
| A1 | `claude` launched outside cswap (no `pin-env` eval) | No proxy env → pin absent for that session | **No** — env is fixed at exec |
| A2 | Background tree whose daemon started before the pin | Daemon's children inherit the daemon's env | **No** for that tree |
| A3 | `CLAUDE_CODE_PROCESS_WRAPPER` users vs plain users | Same: the wrapper is not required. `cswap run`/`exec_default` wire the env directly (`session.py:_exec`), and `pin-env` covers hand-launched `claude`. A wrapper only changes *who* execs, not whether env is inherited | n/a |

`cswap tui` / `auto` / `watch` / `menubar` never exec `claude`, so they need
no wiring — they only change which account is active.

## B. The proxy is wired but the pin is not applied

| # | Case | Behaviour | Recover without restart? |
|---|---|---|---|
| B1 | Pinned account **is** the active account | Provider returns None deliberately (live credential is the client's own) | Yes — switch away and it applies again |
| B2 | Pinned account has no stored credential | Provider returns None → original bearer | Yes — `cswap add` for that slot |
| B3 | Pinned account's refresh lineage is dead (`invalid_grant`, or the ~4-week refresh-token expiry) | Refresh fails → provider returns None → original bearer, silently | Yes — re-login + `cswap add`; next request picks it up |
| B4 | Pin points at a removed/renamed account (**dangling pin**) | `ensure_proxy` resolves the account and returns None when it is gone, so no proxy starts at all | Yes — re-pin |

`remove_account` clears the pin when the account it names is the one going
away (`_clear_pin_if_removed`), so B4 is no longer reachable from a removal.
It stays in the table because the record can still be orphaned another way —
an account renamed in the cloud, or a settings file restored from a machine
whose roster differs.

| # | Case | Behaviour | Recover without restart? |
|---|---|---|---|
| B5 | The pin RECORD itself is lost while the proxy keeps running | Provider reads no pin → returns None → every request leaves with the session's own bearer, and `.claude.json` still names a live proxy so nothing looks wrong | Yes — re-pin; but nothing detects it on its own |

B5 is the only entry here that has actually happened, and it happened on
every machine at once, so it is worth the detail. Two independent bugs
erased the `remoteControl` section from `settings.json` while the daemon
and the wiring stayed healthy:

- `settings._read_raw` degraded ANY read failure (OSError, torn concurrent
  read, malformed JSON) to `{}`, and a read-modify-write starting from `{}`
  is a whole-file REPLACEMENT — every section the writer does not know about
  is dropped. Fixed by routing writers through `_read_raw_for_write`, which
  raises instead of guessing.
- `atomic_write_json` ended in `os.replace(tmp, path)`. That overwrites a
  directory entry and never follows a symlink, so on a machine where
  `<backup>/settings.json` is a dotfiles symlink the rename DETACHED the
  link: writes kept succeeding against a now-local file while the tracked
  copy went stale, and the next dotfiles install restored that copy — which
  had never received a single pin write. Same shape as issue #192 (Claude
  Code's own one-hop settings write). Fixed by resolving the link first.

The lesson that generalises: the pin is fail-open, so anything that can
silently drop its record produces a session that looks perfectly pinned and
is not. Guard the WRITE path, and monitor the pin RECORD, not just the
daemon and the wiring — those two were green throughout this incident.

## C. The proxy dies underneath a live session

| # | Case | Behaviour | Recover without restart? |
|---|---|---|---|
| C1 | Daemon killed / crashed, session keeps its `HTTPS_PROXY` | Requests to a dead port fail at the transport | Yes — `ensure_proxy` respawns on the **recorded port** (fix e98316b), so the live session reconnects to the same address |
| C2 | Daemon recycled onto a *different* port (pre-fix behaviour) | Session pointed at a dead port and its requests left **without the pin, silently** | This was the bug behind two bad verification calls; fixed by port reclamation, then closed completely by the handdown below |
| C3 | Idle teardown (last refcount holder closed) | Daemon exits by design; the next launch spawns a fresh one | Yes |
| C4 | Successor refuses the handed-down socket and binds a FRESH port | Same end state as C2 — the wiring names one port, the daemon serves another, requests leave unpinned — but reached from the *fix* rather than from its absence | Yes, on re-pin; nothing detects it on its own |

**C2 is fixed at a different layer than this table first recorded, and the
distinction is the whole property.** Port reclamation makes the successor come
back on the same *number*; it does not make it come back without a *gap*. The
successor is a `subprocess.Popen` reaching `bind()` about 50ms later, and that
start-up IS the window — measured on a live box before the handdown, **6
refused requests over 0.27s per handover**.

What closes it is that the listening socket itself is **handed down** to the
successor (`CSWAP_PIN_LISTEN_FD` / `CSWAP_PIN_LISTEN_FROM`, alongside the
systemd `LISTEN_FDS` / `LISTEN_PID` convention). The socket never unbinds, so
there is no instant at which `connect()` is refused: arrivals queue in the
backlog and are served by whoever accepts next. Measured with a real spawned
successor, hammering the port ~2ms apart across the whole window, with the old
design as a control in the same harness — **control 91 / 89 / 115 refused,
handdown 0 / 0 / 0**, three runs each.

The control is not decoration. A gapless-handover test whose control cannot
fail proves only that the harness runs.

**C4 is new, and it is the macOS-only way the fix defeats itself.**
`SO_ACCEPTCONN` is readable on Linux and NOT on Darwin — same call, `1` on
Linux, `OSError 42 "Protocol not available"` on macOS. Treating that raise as
"this is not a listening socket" refused *every* handover on macOS, and the
successor then bound a fresh port while the wiring still named the old one:

```
# illustrative, not a verbatim transcript
ignoring the handed-down fd 3: [Errno 42]
serving on port <fresh>        # while the wiring named <old>
```

A read failure reported as an absence — the same shape as B5 above, and why
the probe now dials the address instead of trusting the option. It matters
more than most: a Linux-green suite and a macOS fleet stranding its sessions
produce identical output, and most machines this runs on are the platform it
broke on.

## D. Concurrency

| # | Case | Behaviour |
|---|---|---|
| D1 | Token expires while several pinned requests are in flight | **Was**: every connection thread refreshed the same one-time refresh token → one winner, the rest `invalid_grant`, last writer persisting a consumed grant (lineage death). Measured: 8 threads → 8 refreshes. **Now**: refresh is serialized and waiters re-read the store and reuse the winner's rotation |

## E. Not a pin problem, but adjacent (separate PR)

`cswap run <n>` copies the account token into a session profile, so the
same token then has two consumers — cswap's usage polling and the new
session's own `fetchUtilization`. The usage budget is per token, and that
doubling produced `http-429 per-token usage budget reached` twice on a
mac, each about 2 minutes after a `cswap run`. The pin path never
touches `/api/oauth/usage` (0 calls in 433 traced requests) — this is a
`cswap run` issue and belongs in its own PR.

## Visibility (the recurring theme)

Every case above is survivable; what makes them dangerous is that most are
*quiet*. The mitigations that matter:

1. Surface the pin in the UI — done (`○ cloud` marker, menu label).
2. Say when a pin is set but cannot apply — done (`○ cloud (not applying)`
   for B2/B3/B4, and `:pinned#N!` in the status line). The account's own row
   already said "re-login needed"; the cloud marker beside it did not, which
   made the one line claiming "your claude.ai side lives here" the one line
   not admitting it no longer did.
3. Report daemon health where the pin is shown, so C1/C3 are visible
   rather than inferred.
4. Clear the pin when its account is removed (B4).
5. Keep fail-open, but log once per condition when the pin does not apply —
   a session that silently drops the pin is worse than one that says so.

What B5 taught, and what the monitoring here now reflects: watching the
daemon and the wiring is not enough, because both were green for hours
while no request was pinned. The pin RECORD is the thing to watch, and it
is watchable cheaply — a missing `remoteControl` next to live wiring is a
contradiction no healthy state produces.
