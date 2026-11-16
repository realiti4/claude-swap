# Daily quota windows

`cswap schedule` maintains three daily target windows in `Asia/Shanghai`:

| Account slot | Target window |
|---|---|
| 1 | 07:00–12:00 |
| 2 | 12:00–17:00 |
| 3 | 17:00–22:00 |

Run `cswap schedule --configure` once to bind these slots to their current account identities. A removed, replaced or reordered account does not silently inherit another account's schedule. The command refuses to overwrite an existing configuration. The schedule does not change `settings.json` or the ordinary `cswap auto` policy.

Run `cswap schedule` in the foreground, or `cswap schedule --once` for one check. On macOS, `cswap schedule --install` installs and starts `com.claude-swap.daily-schedule` as a login agent. `cswap schedule --uninstall` stops and removes only that agent. The installer uses the Python interpreter running the command; keep that environment installed. It refuses to replace an existing agent with different settings. The agent starts when the user logs in, requires an unlocked credential store, and retries after process failures. It cannot run while the computer is off or asleep. After wake or a late start, it handles only the current target window; it does not replay expired windows. Do not run another independent account-switching daemon alongside it: each could undo the other's selection.

## Activation

A read of the usage API does **not** start a five-hour window. Inside each slot's target window, the scheduler sends one real Messages request with `max_tokens: 16` to the fixed Anthropic endpoint using the account's stored OAuth login. The prompt is `Reply with OK.`; it never reads project files or executes tools. This consumes a small amount of subscription quota. The model is the same Haiku model used by upstream's minimal usage probe, but the activation request has its own output limit and prompt. Ordinary usage probes are unchanged. No API-key fallback is used.

The provider owns the actual window and reset time. The scheduler cannot reset an existing window or guarantee that its provider-rounded boundary is exactly five hours after the request. If a window is already aligned with the target end (within five minutes), no new request is sent. If a different window is still active, the scheduler waits for its actual reset and tries only while the account's target slot is still open. Quota at 99%, unavailable credentials or untrusted usage prevent activation.

A successful HTTP response, valid model response body or future reset in response headers does **not** confirm that a subscription window started. The scheduler validates the Messages response and requires nonempty assistant text and a positive output-token count. `request_accepted` means only that this response passed validation. `activated` requires a separate, trustworthy usage sample collected after the attempt that reports an active five-hour window. The status records the provider's actual reset and how late the attempt occurred.

Confirmation remains subject to later usage readings. A new trustworthy sample that explicitly reports no active window can invalidate an earlier activation result, including an `activated` result written by an older scheduler version. Confirmation and contradiction checks continue after the target slot ends, but no activation request is sent outside that slot. Once an independently confirmed window ends naturally, its historical success is preserved even if the target slot is still open; the scheduler does not start a second window that day. A missing, stale, rejected or incomplete sample does not establish either confirmation or failure. An unconfirmed request therefore remains pending while usage polling is unavailable; missing reset metadata alone never triggers another Messages request.

The scheduler may retry only after a new, fresh, trustworthy usage sample explicitly shows no active five-hour window. It waits at least 300 seconds between attempts and honors a longer numeric `Retry-After`. It sends at most **three activation requests per account per calendar date** in `Asia/Shanghai`, and only while that account's target slot remains open. Attempt claims and counts are persisted before network I/O, so process restarts do not reset the budget or duplicate an ambiguous request. The same usage sample cannot authorize another attempt. An HTTP error, timeout or missing confirmation is not by itself permission to retry.

These checks distinguish a request that was accepted from a window that the provider actually started. They cannot force a provider to charge a request to the five-hour subscription window or change the provider's reset time. If confirmation or the retry budget is exhausted, the status retains the unresolved result instead of reporting a successful activation.

Regular usage polling explicitly disables inference-based header probes in this scheduler, regardless of dashboard probe settings. This prevents a routine poll from starting a future account's window. Provider usage freshness, request claims and rate-limit backoff remain controlled by upstream's shared usage store.

## Automatic switching

At **99% or higher**, the scheduler switches the default Claude Code account to an available account below 99%. Both the shared five-hour and seven-day windows bind. Model-specific windows bind only when selected by the existing `autoswitch.model` setting; by default they are displayed but do not make the entire account unavailable. It prefers accounts whose daily target has already started, then chooses the most remaining quota. If none qualify, a future slot can be used early. **99% failover takes priority over target-window alignment**; the event explicitly reports this early use, and the UI shows the provider's actual reset.

Targets must have decision-grade usage, matching configured identities, valid finite utilization, an enabled OAuth account, and credentials that the upstream refresh gate can safely freshen. A live isolated session, failed refresh, disabled account, unknown usage or an expired reset from an old sample prevents selection. All unavailable means no switch; the status reports that condition. No production account is deliberately exhausted to test failover.

The service checks every 15 seconds. Actual usage fetches follow upstream's adaptive polling and server backoff, so this is a **sampled threshold**, not a guarantee that a busy request cannot cross 100% between samples. Upstream can trust deliberately older samples under bounded conditions; the display marks samples older than 300 seconds as stale even when the usage store still trusts them for a decision.

Each scheduled slot is selected once. Subsequent manual or threshold-driven switches are respected until a new slot begins. Credential writes use upstream's transaction and token refresh paths. Upstream documents that running Claude Code instances on macOS may take about 30 seconds to reread the Keychain; the scheduler does not terminate them. `cswap run` isolated profiles and external API-token overrides do not follow the default account switch.

## Files and display integration

All files live under the normal backup root (`~/.claude-swap-backup` on macOS):

| File | Purpose |
|---|---|
| `schedule.json` | Enabled flag and hashes binding slots to account identities |
| `schedule-state.json` | Persisted activation attempts, daily retry counts, confirmation state and last selected slot |
| `schedule-status.json` | Atomic, sanitized display snapshot |
| `schedule-events.jsonl` | Sanitized activation and switching events; no credentials or response bodies |
| `schedule-refresh.request` | A display may touch this file; the next 15-second check consumes it |
| `.schedule.lock` | Process-ownership lock; only one scheduler runs |
| `schedule.stdout.log`, `schedule.stderr.log` | Login-agent output |

The display snapshot has `schemaVersion: 1`, UTC `updated_at`, `active_account_id`, an `accounts` array, and `scheduler`. Account rows contain a slot ID, alias, masked email, subscription label, sampled quota percentages, raw provider reset times, sampling timestamp, target slot and activation status. Activation records also expose the persisted attempt count, retry deadline, confirmation timestamps and confirmed reset offset from the target end when present. They never contain tokens, raw credentials or account UUIDs. Model rows use `seven_day_sonnet`, `seven_day_opus`, `seven_day_fable` or `seven_day_haiku` when present. A display must age the global heartbeat independently of per-account samples; a stopped service cannot keep its own `enabled` flag current.

The refresh file never bypasses provider rate limits or starts inference. The process-ownership lock is independent of account/usage transaction locks; no account lock is held during the activation request. The active account store, backup stores and refresh-token successors remain owned by upstream modules.
