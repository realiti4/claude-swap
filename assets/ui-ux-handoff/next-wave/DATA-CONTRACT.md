# Data contract and geometry

## Existing source and gap

`menubar/viewmodel.py` emits account `slot`, `label`, optional `alias`, `active`,
`kind`, `disabled`, `status`, and `windows[]`. A window has `kind` (`5h`/`7d`),
`pct` in **0–100 percent units**, `state` and optional `resetsAt` in **Unix seconds**.
Do not divide seconds by 1000 or treat `pct` as a 0–1 ratio.

Current `_account_vm` only emits windows with numeric percentages. `_window_vm`
drops non-finite percentages and removes `resetsAt` when the reset has passed,
retaining a stale value and countdown message. Thus existing `windows` cannot
represent known-reset/missing-usage or reliably distinguish all missing states.
There is no measured start field in the current wire model. Account freshness
must be derived per account, not inferred from the selected account's global
freshness badge. Preserve the current quota-card contract.

## Proposed additive representation (not yet implemented)

Prefer optional `account.timelineWindows` separate from `windows` so richer
chart states do not change existing quota-card assumptions. Final naming may
follow project conventions; pin it in contract tests before UI integration.
One entry per primary kind, with explicit nullable values:

| Field | Meaning |
| --- | --- |
| `kind` | `5h` or `7d`; exclude spend and per-model windows |
| `pct` | finite measured percent or null; null is never zero |
| `resetsAt` | finite Unix seconds or null; retain elapsed measured timestamp |
| `startsAt` | measured start only, otherwise null |
| `state` | `ok`, `stale`, `elapsed`, `usage-unavailable`, `reset-unavailable`, `unavailable`, `no-window` |
| `observedAt` | actual measurement timestamp if available, otherwise null |

Do not add service requests to obtain missing fields. Use raw/last-good values
already available in the snapshot adapter. Preserve sanitization: JSON never
contains NaN/Infinity. If additive fields are absent (older producer), derive
known rows from existing windows; degrade unknown states to unavailable rather
than parsing localized countdown strings as a data protocol. Keep schemaVersion
compatible only if the repository's additive-version policy permits it.

## Geometry

Let N be a single shared current epoch in seconds, and D be 18000 for session
or 604800 for weekly. Domain is [N−D, N+D]. For plot width W and timestamp t:

`x(t) = W * (t - (N-D)) / (2*D)`

A nominal start S is R−D, where R is measured resetAt. Display full window
[x(S), x(R)] with a neutral track. Its nominal width is W/2. Overlay a colored
fill of `windowWidth * pct/100`. This fill endpoint is a quantity, **not a time
event**. The vertical dashed amber Now line is x(N)=W/2 across all rows.
Measured nonstandard starts, if ever supplied, must retain their true duration
and disclose it; the equal-width rule applies to nominal 5h/7d windows.

Exact detail includes full alias/index, percentage, remaining duration, start
and reset with local date/time and timezone. Label derived starts “inferred
(reset − 5h)” or “inferred (reset − 7d)”. Epoch arithmetic uses fixed duration,
not local calendar subtraction; a DST-crossing 7-day window remains 168 hours.
Tick labels may be sparse; zero-width/rounded tiny fills still have text labels.

## State precedence and live updates

1. No account window/API-key sentinel: text, no bar.
2. Missing or invalid reset: reset unavailable, no positioned bar.
3. Reset <= N: awaiting updated usage, no new cycle, even if marked stale.
4. Stale future reset: muted last-known full window and last-known percentage.
5. Known window + missing percentage: neutral track, “No usage data”.
6. Valid known measurement: full window + quota fill + explicit percentage.

When reset lies beyond the nominal domain, use a visibly clipped track with an
offscale indicator and exact detail; do not silently clamp it into a normal
window. Negative/non-finite percentage is unavailable. A finite >100 value may
retain its actual label while fill caps at100% and indicates exceeded quota.
Never change or normalize the stored measurement to satisfy drawing bounds.

Recompute against the existing 30-second local cadence while open (no remote
poll). Now stays centered while absolute window positions advance. At expiry,
transition immediately on the next tick. Preserve row focus, selection, detail
and scroll when snapshots refresh; close detail safely if its account vanishes.
