# Auto-continue after usage limits

When a usage limit stops a Claude Code session mid-task, claude-swap can carry
it on by itself as soon as there is capacity again. You no longer need to come
back and type "carry on" in every session.

```bash
cswap resume install    # once; snapshots ~/.claude/settings.json first
```

That's it. According to Claude Code's hooks docs, running sessions pick up
hook changes automatically; restart a session if in doubt.

## Test it (2 minutes, no real limit needed)

1. Check that it's on: `cswap resume status` should print `Auto-continue: on`.
2. In any Claude Code chat, type: **`run cswap resume test, then stop`**.
3. Claude runs it and finishes its reply. Within about 10 seconds, the chat
   receives `[claude-swap auto-continue] TEST ...` by itself, and Claude
   answers it without you typing anything. That's the feature working.
4. Confirm with `cswap resume status`: that chat shows `resumed`,
   `auto-continue test passed`.

The test runs through the real path: background waiter, wait for idle,
delivery into the session's inbox, confirmation. Only the usage-limit stop
is simulated. It must be run *by Claude inside a chat*, because it uses that
chat's own inbox token.

---

## When does a session continue?

As soon as **any** of these is true:

| Trigger | What happened | Typical wait |
|---|---|---|
| **Account switch** | `cswap auto` (or a manual `cswap switch`) moved you onto an account with quota left after the session stopped | seconds |
| **Limit reset** | the limit that stopped the session reset (plus `resume.slackSeconds`, default 90 s) | up to 5 h (5-hour limit) or days (weekly) |
| **Quota is back** | a fresh usage reading of the same account shows the limited window below 98 % (for example after buying extra usage) | minutes |

With `cswap auto` running and more than one account, the switch trigger is what
usually fires: the session continues seconds after the swap instead of hours
later. If every account is exhausted, `cswap auto` sleeps until the earliest
reset, switches, and the session continues right after.

The session receives this message (it starts a new turn):

> [claude-swap auto-continue] The usage limit that stopped this session has
> cleared (claude-swap switched to an account with quota left). Continue the
> task you were working on, but first re-check the current state of the files
> you were changing: time has passed, and they may have been edited since. If
> the task is already finished, or no longer makes sense given what you find,
> say so in one line and stop without making further changes. If you were
> waiting for an answer from the user, ask again briefly instead of guessing.

It is worded that way on purpose (see the next section): if a session didn't
need continuing, the cost is one short turn that checks and stops, not
unwanted changes.

**It never continues a session automatically when:**
- the stop happened more than `resume.maxWaitHours` ago (default 24 h). It is
  *held* and you get one notification; `cswap resume now` still continues it;
- you paused auto-continue with `cswap resume off`;
- you cancelled that session with `cswap resume cancel`;
- you already typed something in it, closed it, or Claude Code's own
  auto-continue got there first.

---

## Finished, abandoned, half-done: what it can and can't know

Claude Code records *that* a session stopped on a limit, not whether you still
want it. To pick the rules, the history on this machine was replayed: 166 stops
across 49 sessions (October to November 2026), checking what you did next.

| What you did after the stop | Share |
|---|---|
| Came back and said "carry on" (typos included) | ~82 % |
| Never touched that session again | ~8 % |
| Came back with something else (answers, `/model`, a new instruction) | ~5 % |
| Unclear, or carried on in another chat in the same folder | ~5 % |

The sessions you never came back to looked exactly like the rest at the moment
they stopped: same batch sizes, same time since your last prompt. So there is
no signal to *guess* "abandoned" from. A rule "skip if you started a new chat
in the same folder" was also measured: it would have wrongly blocked 1 session
you came back to and correctly held none you didn't, so it isn't used.

That's why the design is:
- **Act automatically on what is reliable.** It stands down if you type, if
  the session closes, or if the stop is old.
- **Make your intent a single command.** `off` when you're done for the day,
  `cancel` for specific sessions.
- **Keep a wrong continue cheap.** The message re-checks state first and stops
  if there's nothing sensible to do.

---

## How it works

1. `cswap resume install` adds a `StopFailure` hook (matcher `rate_limit`) to
   Claude Code's `settings.json`. Claude Code runs it when a turn ends on a
   rate or usage limit.
2. The hook runs `cswap resume arm`. It reads the stop from the session's
   transcript (limit type and reset time), records it under
   `<backup dir>/resume/<session id>.json`, and starts one small background
   waiter per session. The hook fires several times for one limit; repeats
   are ignored.
3. The waiter only reads local files: the transcript, the session's status,
   the live login, cswap's cached usage and `cswap auto`'s last switch. It
   never calls the usage API itself, so ten stopped sessions cost no more API
   traffic than one.
4. When there's capacity and the session is idle, the waiter sends the
   continue message into the session's own **cross-session inbox** (the
   socket or named pipe Claude Code uses for messages between your sessions).
   It authenticates with the session's own-child token, inherited from the
   hook, so Claude Code treats the message as the session's own hook posting
   back. That works even in sessions that bypass permission prompts. The
   token stays in the waiter's memory and is never written to disk.
5. It then checks that the session recorded the message. A message Claude
   Code holds or refuses leaves no trace. If nothing shows up within a
   minute, the session is marked `undelivered` and you get a notification
   instead of a false "continued". If the session is busy at that point (you
   typed something at the same moment, so the message is queued behind your
   prompt), it keeps checking until the session goes idle, for up to 30
   minutes.

The waiter stops by itself when:

- you continue the session yourself;
- the session closes;
- the session is handled by Claude Code's own auto-continue;
- you cancel it (`cswap resume cancel`);
- it has waited 8 days.

While held (older than `resume.maxWaitHours`) or paused (`cswap resume off`),
the waiter keeps watching quietly, so `cswap resume now` or `cswap resume on`
still work, with the session's own token.

**Loop guard:** if a continue runs straight back into the limit without doing
any work, that counts as consecutive. After `resume.maxConsecutive` (default 3)
in a row, claude-swap stops and sends a notification instead of retrying
forever. A prompt you type yourself resets the count.

### A stop makes `cswap auto` switch at once

A session stopping on a limit is first-hand proof that the account is spent.
So `cswap auto` doesn't wait for the usage endpoint to agree. On its next
tick (within `autoswitch.intervalSeconds`, 15 s on this machine) it leaves
the account:

- It decides on the usage it already has stored and makes no usage calls
  first. In a network glitch, the endpoint is exactly what fails. Before this
  change, unreadable usage meant three "usage unknown" ticks and then waiting
  for a candidate to become readable again (found live on 2026-11-03: the
  switch landed 80 s after three sessions stopped).
- It picks the best account it can measure. If none is measurable, it lands
  "blind" on the account whose last reading was lowest. Usage only rises
  within a window, so an old reading is a floor. It skips accounts:
  - that never had a reading;
  - that the usage endpoint refuses (401/403, like a broken slot);
  - whose last reading is already at a limit that hasn't reset;
  - that a stopped session, or the account it just left, shows is spent.
- If it guesses wrong, the session stops again when the waiter continues it
  on that account. That stop names the account, and `cswap auto` moves on
  again without going back. It can tell that stop from a late one on the old
  account because Claude Code reports a different reset time for it.
- Then the waiters continue the stopped sessions as usual (`account-switched`).

It ignores stops from sessions that already carried on, from `cswap run`
profiles and from rehearsals. In the first 60 s after its own switch it also
ignores stops that report the old account's reset time: a running session can
still be on the old account's token that long. This needs the `cswap auto`
loop and the auto-continue hook. If the loop itself is stuck, nothing
switches. Turn it off with `cswap config set resume.switchOnStop false`.

---

## Commands

```bash
cswap resume status            # sessions waiting / continued / stopped on a limit
cswap resume status --json     # same, machine-readable
cswap resume now --all         # continue every waiting session right now
cswap resume now my-session    # one session, by name or session-id prefix
cswap resume cancel my-session # don't continue this one (or --all: none of them)
cswap resume off               # pause auto-continue, e.g. when you're done for the day
cswap resume on                # turn it back on
cswap resume uninstall         # remove the hook (also snapshotted first)
```

`cswap resume now` on a session that already has a waiter just tells that
waiter to go ahead. A session that stopped **before** you installed the hook
has no waiter. `now` then sends the message as an ordinary cross-session
message. Claude Code holds those for approval in sessions that bypass
permissions, and the VS Code extension can't show that approval dialog. So
`now` skips such sessions and tells you to type "continue" there (or set
`crossSessionInbound` to `accept`). Every limit stop after install is handled
by a waiter.

---

## Settings

| Key | Default | Meaning |
|---|---|---|
| `resume.slackSeconds` | `90` | wait this long past a limit's reset before continuing |
| `resume.onSwitch` | `true` | continue as soon as an account switch lands |
| `resume.maxConsecutive` | `3` | stop after this many continues in a row that hit the limit again |
| `resume.notify` | `true` | desktop notification when a session is continued, held, or given up on |
| `resume.enabled` | `true` | the master switch; `cswap resume off`/`on` set it |
| `resume.maxWaitHours` | `24` | hold sessions stopped longer ago than this (0 = no cap) |
| `resume.switchOnStop` | `true` | `cswap auto` leaves an account the moment a session stops on its limit |

```bash
cswap config set resume.slackSeconds 300
```

---

## Requirements and caveats

- **Claude Code 2.1.234+ on Windows** (2.1.224+ on macOS/Linux) for
  cross-session messaging. `StopFailure` hooks need 2.1.78+. Check with
  `/list-agents` in a session: if the command is recognized, messaging is on.
- **`crossSessionInbound`.** If your Claude Code settings set it to `hold` or
  `refuse`, sessions won't accept the message. `cswap resume install` warns
  you about this.
- **Built-in auto-continue.** Claude Code's terminal UI has
  `autoContinueAtUsageLimit`, which waits for the *same* account's reset.
  It didn't fire in VS Code sessions on 2.1.301–2.1.314 here. The two
  coexist: the 90 s slack lets the built-in act first at a reset, and the
  waiter stands down once the session moves on. claude-swap's advantage is
  continuing right after an account switch, without waiting for the reset.
- **Permission prompts still apply.** A continued task that needs a
  permission you haven't granted will stop at the prompt, as usual.
- **`cswap run` sessions** are pinned to one account, so for them only the
  limit's own reset counts, not account switches.
- **Logs** go to `claude-swap.log` in the backup dir. Search for
  `auto-continue`.

## What has been tested

Tested on Windows 11 with Claude Code 2.1.314:

- A real `StopFailure` hook (async and sync) receives the session's inbox
  address and own-child token. Its detached waiter keeps running after the
  session exits.
- A message sent with the own-child token reaches a session running in
  `bypassPermissions`; Claude Code records it as `selfSent`. A message sent
  with the peer token to the same session is held and never recorded.
- The wire format, matching Claude Code's own documented recipe, over a real
  named pipe.
- The whole production path on an idle VS Code extension session: a
  hook-shaped event goes into `cswap resume arm`, which spawns the waiter.
  The waiter waits while the session is busy, then delivers once it's idle.
  The session starts a new turn and records the message as `selfSent`. The
  stop in that test was synthetic, since a real limit can't be triggered on
  demand.

Not tested yet: the interactive terminal UI's limit dialog, macOS and Linux.
The unit tests cover the policy, the loop guard and the waiter.

**Internals:** [HOW-IT-WORKS.md](./HOW-IT-WORKS.md) is the maintainer guide.
It covers the design, the files on disk, the states, the Claude Code
behaviours this depends on, how to re-run each live check, and
troubleshooting.
