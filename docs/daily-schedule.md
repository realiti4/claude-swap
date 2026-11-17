# Daily quota windows

`cswap schedule` starts all three accounts at **07:00** each day in `Asia/Shanghai`, then renews each expired window until midnight. Default-account selection follows a separate rotation:

| Account slot | Activation start | Default-account selection |
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

At **99% or higher**, the scheduler switches the default Claude Code account to an available account below 99%. Both the shared five-hour and seven-day windows bind. Model-specific windows bind only when selected by the existing `autoswitch.model` setting; by default they are displayed but do not make the entire account unavailable. It prefers accounts whose selection slot has already started, then chooses the most remaining quota. If none qualify, an account with a later selection slot can be selected early. **99% failover takes priority over scheduled account selection**. Selection slots do not delay the shared 07:00 activation start, and the UI shows the provider's actual reset.

Targets must have decision-grade usage, matching configured identities, valid finite utilization, an enabled OAuth account, and credentials that the upstream refresh gate can safely freshen. A live isolated session, failed refresh, disabled account, unknown usage or an expired reset from an old sample prevents selection. All unavailable means no switch; the status reports that condition. No production account is deliberately exhausted to test failover.

The service checks every 15 seconds. Actual usage fetches follow upstream's adaptive polling and server backoff, so this is a **sampled threshold**, not a guarantee that a busy request cannot cross 100% between samples. Upstream can trust deliberately older samples under bounded conditions; the display marks samples older than 300 seconds as stale even when the usage store still trusts them for a decision.

Each scheduled slot is selected once. Subsequent manual or threshold-driven switches are respected until a new slot begins. Credential writes use upstream's transaction and token refresh paths. Upstream documents that running Claude Code instances on macOS may take about 30 seconds to reread the Keychain; the scheduler does not terminate them. `cswap run` isolated profiles and external API-token overrides do not follow the default account switch.

## Files and display integration

All files live under the normal backup root (`~/.claude-swap-backup` on macOS):

| File | Purpose |
|---|---|
| `schedule.json` | Enabled flag and hashes binding slots to account identities |
| `schedule-state.json` | Persisted activation attempts, cycle history, retry counts, confirmation state and last selected slot |
| `schedule-status.json` | Atomic, sanitized display snapshot |
| `schedule-events.jsonl` | Sanitized activation and switching events; no credentials or response bodies |
| `schedule-refresh.request` | A display may touch this file; the next 15-second check consumes it |
| `.schedule.lock` | Process-ownership lock; only one scheduler runs |
| `schedule.stdout.log`, `schedule.stderr.log` | Login-agent output |

The display snapshot has `schemaVersion: 1`, UTC `updated_at`, `active_account_id`, an `accounts` array, and `scheduler`. Account rows contain a slot ID, alias, masked email, subscription label, sampled quota percentages, raw provider reset times, sampling timestamp, selection slot and activation status. The scheduler exposes `activation_start: "07:00"` separately from selection slots. Activation records also expose the requested model, total daily attempt count, current cycle number and attempt count, cycle due time, retry deadline, confirmation timestamps and confirmed reset offset from the target end when present. They never contain tokens, raw credentials or account UUIDs. Model rows use `seven_day_sonnet`, `seven_day_opus`, `seven_day_fable` or `seven_day_haiku` when present. A display must age the global heartbeat independently of per-account samples; a stopped service cannot keep its own `enabled` flag current.

The refresh file never bypasses provider rate limits or starts inference. The process-ownership lock is independent of account/usage transaction locks; no account lock is held during the activation request. The active account store, backup stores and refresh-token successors remain owned by upstream modules.
