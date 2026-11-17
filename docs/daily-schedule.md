# Daily quota windows

`cswap schedule` starts all three accounts at **07:00** each day in `Asia/Shanghai`, then renews each expired window until midnight. Default-account selection balances remaining weekly quota, time until its reset, and five-hour headroom. Fixed selection times apply only when no available account has a trustworthy future seven-day reset:

| Account slot | Activation start | Selection fallback |
|---|---|---|
| 1 | 07:00 | 07:00–12:00 |
| 2 | 07:00 | 12:00–17:00 |
| 3 | 07:00 | 17:00–22:00 |

Run `cswap schedule --configure` once to bind these slots to their current account identities. A removed, replaced or reordered account does not silently inherit another account's schedule. The command refuses to overwrite an existing configuration. The schedule does not change `settings.json` or the ordinary `cswap auto` policy.

Run `cswap schedule` in the foreground, or `cswap schedule --once` for one check. On macOS, `cswap schedule --install` installs and starts `com.claude-swap.daily-schedule` as a login agent. `cswap schedule --uninstall` stops and removes only that agent. The installer uses the Python interpreter running the command; keep that environment installed. It refuses to replace an existing agent with different settings. The agent starts when the user logs in, requires an unlocked credential store, and retries after process failures. It cannot run while the computer is off or asleep. After wake or a late start, it immediately checks every account whose target start has passed that calendar day. Initial activation and subsequent renewal remain eligible after the original target window ends, until midnight; it does not wait for another scheduled start or replay previous dates. No account is activated before 07:00. After 07:00, every missed account is eligible immediately, including accounts whose selection slot has not started. Do not run another independent account-switching daemon alongside it: each could undo the other's selection.

## Activation

A read of the usage API does **not** start a five-hour window. At or after the shared daily 07:00 start, the scheduler sends one real Messages request with `max_tokens: 16` to the fixed Anthropic endpoint using the account's stored OAuth login. The prompt is `Reply with OK.`; it never reads project files or executes tools. This consumes a small amount of subscription quota. The activation model is `claude-sonnet-5-5`, with its own output limit and prompt. Shared 5h/7d limits and any reported Sonnet-specific limit gate this request; unrelated model limits do not. Ordinary usage probes are unchanged. No API-key fallback is used.

The provider owns the actual window and reset time. The scheduler cannot reset an existing window or guarantee that its provider-rounded boundary is exactly five hours after the request. The initial target end is 12:00 for all accounts. If a window is already aligned with that end (within five minutes), no new request is sent. If a different window is still active, the scheduler waits for its actual reset and can catch up later on the same date once the window is idle. Quota at 99%, unavailable credentials or untrusted usage prevent activation.

A successful HTTP response, valid model response body or future reset in response headers does **not** confirm that a subscription window started. The scheduler validates the Messages response and requires nonempty assistant text and a positive output-token count. `request_accepted` means only that this response passed validation. `activated` requires a separate, trustworthy usage sample collected after the attempt that reports an active five-hour window. The status records the provider's actual reset and how late the attempt occurred.

Confirmation remains subject to later usage readings. A new trustworthy sample that explicitly reports no active window can invalidate an earlier activation result, including an `activated` result written by an older scheduler version. Confirmation, contradiction checks and eligible catch-up requests continue after the target slot ends. Catch-up sends directly with each due account's credential; it does not switch the default account back to an earlier slot. After a confirmed window expires, a fresh endpoint sample taken after its reset must show either an idle account or an already-started next window. An idle account receives a new activation request immediately when eligible; an already-started window is adopted without inference. Each completed cycle is retained in history. A confirmed active window remains recorded when its quota reaches the threshold; exhaustion still prevents inference and triggers failover, but does not erase confirmation needed to track the next cycle. An exhausted retry budget cannot be reset by obsolete confirmation metadata alone. Renewals continue until midnight in Asia/Shanghai, regardless of which account is selected. At midnight sending stops; the next date starts with 07:00 eligibility for all accounts. A window started before midnight may naturally reset after midnight. A missing, stale, rejected or incomplete sample does not establish either confirmation or failure. An unconfirmed request therefore remains pending while usage polling is unavailable; missing reset metadata alone never triggers another Messages request.

The scheduler may retry only after a new, fresh, trustworthy usage sample explicitly shows no active five-hour window. It waits at least 300 seconds between attempts and honors a longer numeric `Retry-After`. It sends at most **three activation requests per account per window cycle** in `Asia/Shanghai`, and only from 07:00 until midnight on the same date. Catch-up and renewal obey the same freshness, backoff and per-cycle attempt budget; one unavailable account does not stop the other due accounts. Attempt claims, cycle numbers and counts are persisted before network I/O, so process restarts do not reset the current cycle budget or duplicate an ambiguous request. A new budget is available only after a confirmed window expires and a fresh sample establishes the next cycle. Successful cycles are not capped at three per day. The same usage sample cannot authorize another attempt. An HTTP error, timeout or missing confirmation is not by itself permission to retry.

These checks distinguish a request that was accepted from a window that the provider actually started. They cannot force a provider to charge a request to the five-hour subscription window or change the provider's reset time. If confirmation or the retry budget is exhausted, the status retains the unresolved result instead of reporting a successful activation.

Regular usage polling explicitly disables inference-based header probes in this scheduler, regardless of dashboard probe settings. This prevents a routine poll from starting a future account's window. Provider usage freshness, request claims and rate-limit backoff remain controlled by upstream's shared usage store.

## Automatic switching

The default account uses **weekly quota allocation**, at any time of day. A near deadline raises priority, but an almost-consumed weekly budget does not monopolize work while other accounts have large balances. Among enabled accounts below 99% in every applicable quota, each known future `seven_day.resets_at` receives these values:

- Weekly budget: `99 - seven_day utilization`, in percentage points.
- Required weekly pace: weekly budget divided by hours until weekly reset, with a 15-minute minimum denominator.
- Five-hour headroom factor: `(99 - five_hour utilization) / 99`.
- Allocation score: required weekly pace multiplied by the headroom factor.

Higher scores get work first. Actual sampled consumption reduces the remaining budget and headroom, so the order changes as work is used. A shorter time until reset increases urgency again. These are per-account percentage-based priorities, not promised shares of requests or comparable token capacities across plans. The policy directs real work; full consumption still needs enough workload and available five-hour quota. It does not generate extra work to consume quota, and the 99% cutoff remains in effect.

An account at **80% five-hour usage** becomes a reserve candidate when another five-hour cycle can occur before weekly expiry. Known-deadline accounts below that level take precedence. If every candidate is in reserve, reserve quota remains usable up to 99%. In the last five-hour cycle before weekly expiry, the 80% reserve rule is waived so remaining weekly quota can still be used. The provider's future five-hour reset defines that last cycle; when no future five-hour reset is reported, the rule uses a five-hour horizon.

Normal changes have a persisted **10-minute minimum hold** after automatic or observed manual switches. Within the same reserve class, a new account's score must exceed the current score by **20%** before switching. This prevents repeated changes due to small timestamp or usage changes. Reset times within 30 minutes are reported as an equivalent deadline when the current score is within that margin; quota allocation still controls the choice. A reserve account can yield to a non-reserve account without the score margin, after the hold. Reaching 99%, or an unavailable current account, bypasses the hold. Failover uses the same allocation order. A refused credential is skipped, and failed selection does not move a usable current account to a lower allocation priority. The scheduler rechecks quota after credential refresh in case a reset occurred during refresh.

Known future weekly deadlines take precedence over missing, invalid or expired weekly resets. If every available weekly reset is unknown, the original 07:00/12:00/17:00 selection-slot rules are the fallback. Five-hour recovery and fresh weekly resets recalculate the allocation from the provider's new readings.

Both the shared five-hour and seven-day windows bind. Model-specific windows bind only when selected by the existing `autoswitch.model` setting; by default they are displayed but do not make the entire account unavailable. Shared 07:00 activation and midnight renewal limits are independent of quota allocation.

Targets must have decision-grade usage, matching configured identities, valid finite utilization, an enabled OAuth account, and credentials that the upstream refresh gate can safely freshen. A live isolated session, failed refresh, disabled account, unknown usage or an expired reset from an old sample prevents selection. All unavailable means no switch; the status reports that condition. No production account is deliberately exhausted to test failover.

The service checks every 15 seconds. Actual usage fetches follow the shared [usage polling settings](auto-switch.md#usage-polling) and server backoff, including fixed intervals configured for current and other accounts. A query runs on the next check after its deadline. This is a **sampled threshold**, not a guarantee that a busy request cannot cross 100% between samples. Upstream can trust deliberately older samples under bounded conditions; the display marks samples older than 300 seconds as stale even when the usage store still trusts them for a decision.

When weekly deadlines are unavailable, each fallback selection slot is selected once. With weekly deadlines available, a manual selection is held for 10 minutes before normal quota allocation resumes; quota exhaustion or unavailability can end the hold sooner. Credential writes use upstream's transaction and token refresh paths. Upstream documents that running Claude Code instances on macOS may take about 30 seconds to reread the Keychain; the scheduler does not terminate them. `cswap run` isolated profiles and external API-token overrides do not follow the default account switch.

## Files and display integration

All files live under the normal backup root (`~/.claude-swap-backup` on macOS):

| File | Purpose |
|---|---|
| `schedule.json` | Enabled flag and hashes binding slots to account identities |
| `schedule-state.json` | Persisted activation attempts, cycle history, retry counts, confirmation state, last selected slot and persistent selection hold |
| `schedule-status.json` | Atomic, sanitized display snapshot |
| `schedule-events.jsonl` | Sanitized activation and switching events; no credentials or response bodies |
| `schedule-refresh.request` | A display may touch this file; the next 15-second check consumes it |
| `.schedule.lock` | Process-ownership lock; only one scheduler runs |
| `schedule.stdout.log`, `schedule.stderr.log` | Login-agent output |

The display snapshot has `schemaVersion: 1`, UTC `updated_at`, `active_account_id`, an `accounts` array, and `scheduler`. Account rows contain a slot ID, alias, masked email, subscription label, sampled quota percentages, raw provider reset times, sampling timestamp, selection slot and activation status. The scheduler exposes `activation_start: "07:00"` separately from selection slots. Its `selection` object reports the weekly policy, allocation algorithm, decision reason, preferred account and weekly reset, ranked available account IDs, last change, hold deadline, 600-second hold, 1800-second deadline tolerance, 80% reserve threshold and 1.2 score multiplier. `allocation_accounts` reports each candidate’s remaining weekly percentage, budget to 99%, hours until reset, required pace, headroom, score, reserve status and last-cycle status. Slot fields remain the selection fallback, not a prediction of weekly priority. Activation records also expose the requested model, total daily attempt count, current cycle number and attempt count, cycle due time, retry deadline, confirmation timestamps and confirmed reset offset from the target end when present. They never contain tokens, raw credentials or account UUIDs. Model rows use `seven_day_sonnet`, `seven_day_opus`, `seven_day_fable` or `seven_day_haiku` when present. A display must age the global heartbeat independently of per-account samples; a stopped service cannot keep its own `enabled` flag current.

The refresh file never bypasses provider rate limits or starts inference. The process-ownership lock is independent of account/usage transaction locks; no account lock is held during the activation request. The active account store, backup stores and refresh-token successors remain owned by upstream modules.
