# JSON output reference

`cswap list --json`, `cswap status --json` and `cswap switch --json` print a single JSON object on stdout; human-readable notices go to stderr. The commands and an example are in the [README](../README.md#json-output-for-scripting).

## Contract

- Every payload carries a `schemaVersion` (currently `1`).
- On a handled error stdout is `{"schemaVersion":1,"error":{...}}` with a non-zero exit code.
- `--switch`/`--switch-to` report `{"switched": true|false, "from": …, "to": …, "reason": …}`.

## Usage windows

Each account row has a `usage` object with `fiveHour` and `sevenDay` windows (`pct`, `resetsAt`).

- A window's `pct` is `null` when the usage API reported `utilization: null` for it.
- A window the account does not have (an Ultra account's `fiveHour`) is omitted.
- Usage is served from a per-account cache: when the usage API is briefly unreachable, the last-known numbers are shown instead of nothing (the human view marks them with their age, e.g. `· 2m ago`).

## Optional row fields

These apply to `list` rows and the managed active row from `status --json`. All are additive.

| Field | Present when | Meaning |
|---|---|---|
| `usageFetchedAt`, `usageAgeSeconds` | the row has decision-trusted usage | How old the measurement is. |
| `lastGoodUsage`, `lastGoodFetchedAt`, `lastGoodAgeSeconds` | `usage` is null but a last-known measurement exists: data too old to drive a decision (`usageStatus` stays `unavailable`), or a row in a non-`ok` state such as `token_expired` | Preserves the human display without making the account actionable. |
| `usageError` | `usage` is null and nothing else explains it (`usageStatus` is `unavailable`) | The last fetch failure by kind (e.g. `http-429`, `timeout`). |
| `usageRetryAt` | the cache is backing off from that failure | The time of the next attempt. |
| `disabled` | the account is held out of rotation with `cswap disable` | `true`; absent otherwise. |
| `threshold` | a per-account threshold is set with `cswap threshold` | e.g. `85.0`; absent otherwise, so a fleet that never sets a policy emits rows identical to before. |
| `alias` | an alias is set with `cswap alias` | e.g. `"dev"`; accounts without one omit the key. |
| `loginExpiresAt` | the stored login records when its refresh token expires | ISO-8601 UTC: the moment the slot will need a fresh `/login` and `cswap add --slot N`, so a script can warn a few days ahead instead of discovering `relogin_required`. Absent when Claude Code recorded no such date for that login. |
| `plan` | the slot's subscription plan has been read from the profile endpoint at least once | `label` as the human views print it (`Ultra`, `Max 5x`, `Pro`, `Team`, `Free`), the raw `tier`, then `organizationType`, `billingType`, `subscriptionStatus` and `planDisplayName` when the profile carried them, `expired` (`true` once the subscription is `canceled` and the account is back on the free tier; the human views show that as a highlighted `Free`) and `fetchedAt`, the last successful profile read. Plans refresh on their own six-hour cadence, not with usage. |

## Pace fields on weekly windows

Weekly windows (`sevenDay` and per-model `scoped` entries, never `fiveHour`) additively carry pace fields once the week is about a day old.

| Field | Meaning |
|---|---|
| `expectedPct` | Where usage would sit if spread evenly across the week. |
| `aheadOfPace` | `true` when meaningfully above that: the same signal the human views show as an `(ahead)`/`(ahead of pace)` marker. |
| `projectedExhaustionAt` | The current rate extrapolated into an ETA to 100%. |
| `willLastToReset` | Whether the quota will last to the reset at the current rate. |

The two projections stay `--json`-only, since a linear projection is too rough to present as fact in the UI.

## `cswap auto --json`

`cswap auto --json` emits an event stream instead: one JSON object per line (`{"schemaVersion":1,"event":"switch","ts":…, …}`) with kinds like `poll`, `switch`, `no-switch`, `account-quarantined`, `all-exhausted`, `error`. The contract is additive: new kinds and fields may appear, so scripts should ignore unknown ones.
