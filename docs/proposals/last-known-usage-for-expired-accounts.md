# Design proposal: show last-known windows and reset times for expired accounts

Status: proposal (no code changes in this PR)

## Problem

When an account enters a sentinel state, `cswap list` replaces its whole usage
block with the sentinel note and a single summary line:

```
  1: alice@example.com [Work]
     re-login needed — refresh token dead; log in with Claude Code, then run: cswap add
     └ last seen 40% used · 1d ago

  2: bob@example.com [Work]
     re-login needed — refresh token dead; log in with Claude Code, then run: cswap add
     └ last seen 99% used · 3d ago

  3: carol@example.com [Personal] (active)
     ├ 5h:     29%   resets Sep 21 00:10  in 1h 22m
     ├ 7d:     87%   resets Sep 22 01:00  in 1d 2h
     └ Fable:  77%   resets Sep 22 01:00  in 1d 2h
```

Accounts 1 and 2 are the ones the operator most needs to plan around, and they
are the two that say the least. Two questions go unanswered:

1. **Which window is at 99%?** `last_seen_note` reports
   `100 - account_headroom(last_good)`, which is the *binding* window only. A
   5h window at 99% and a 7d window at 99% mean completely different things:
   one clears in hours, the other in days.
2. **When does it clear?** No reset time at all. The operator cannot tell
   whether re-logging in to account 2 buys them a usable account or a maxed
   one.

The information exists. `switcher._usage_entry_lines` discards it:

```python
if entry.sentinel is not None:
    out = [dimmed(SENTINEL_NOTES.get(entry.sentinel, entry.sentinel))]
    last_seen = last_seen_note(entry)
    if last_seen is not None and entry.sentinel != USAGE_API_KEY:
        out.append(f"{dimmed('└')} {muted(last_seen)}")
    return out
```

`entry.last_good` is the full normalized usage dict, per-window percentages and
`resets_at` included. The sentinel branch collapses it to one number before the
formatter that knows how to render windows ever sees it.

## The reset times are still correct

This is the part worth being precise about, because it is what makes the fix
worth doing rather than just cosmetic.

`resets_at` is an absolute UTC instant, and `oauth.fresh_reset_strings`
already recomputes the countdown and the clock string at render time for
exactly this reason — its docstring says the fetch-time strings drift as the
measurement ages, so it never uses them when `resets_at` is present.

A reset instant does not decay with the measurement that carried it. A 7-day
window observed three days ago with `resets_at` four days out still resets four
days out. So "when does this account clear" is answerable today for every
expired account that has a stored measurement. `cswap` simply declines to
answer it.

The percentages are the part that ages, and they age in a known direction. An
account cswap cannot authenticate cannot spend through cswap, so its measured
usage is a floor, not an estimate: it can only have grown if the operator used
that account somewhere else. Two consequences:

- A window whose `resets_at` has **already passed** is known to have rolled.
  Reporting its three-day-old 99% as the current state, which is what account 2
  does now, is not merely stale, it is backwards. The most likely truth is 0%.
- A window whose `resets_at` is **still ahead** keeps its measured percentage as
  a lower bound on usage, which is the conservative direction for a
  switch-planning decision.

## Proposal

Render the stored measurement through the normal window formatter, and classify
each window at render time by comparing its `resets_at` to now.

### Rendering

```
  1: alice@example.com [Work]
     re-login needed — refresh token dead; log in with Claude Code, then run: cswap add
     ├ last known · 1d ago
     ├ 5h:      —    window reset Sep 19 18:00
     ├ 7d:     40%   resets Sep 22 01:00  in 1d 2h
     └ Fable:  31%   resets Sep 22 01:00  in 1d 2h

  2: bob@example.com [Work]
     re-login needed — refresh token dead; log in with Claude Code, then run: cswap add
     ├ last known · 3d ago
     ├ 5h:      —    window reset Sep 17 21:00
     ├ 7d:     99%   resets Sep 22 01:00  in 1d 2h
     └ Fable:  99%   resets Sep 22 01:00  in 1d 2h
```

Three per-window states:

| State | Condition | Rendering |
| --- | --- | --- |
| Carried | `resets_at` in the future | measured percentage, plus countdown and clock recomputed by `fresh_reset_strings` |
| Rolled | `resets_at` in the past | `—` and `window reset <clock>`; no percentage is asserted |
| Unknown | no `resets_at` stored | measured percentage, no reset column |

The `last known · <age>` header carries the provenance once for the whole
block, so no individual line has to repeat it, and the reader is never invited
to mistake the block for a live reading.

The rolled rendering is taken verbatim from #376's suggestion, so both paths
produce one string from one classifier rather than two near-identical ones.

Rolled windows deliberately print `—` rather than `0%`. Zero would be an
assertion cswap cannot make: the operator may have used that account directly in
Claude Code. `—` says the stored number expired without replacing it with a
guess.

### Behavior notes

- **No new configuration.** This replaces one line with a block of the same
  shape every other account already prints. Operators who want the terse form
  have `cswap list --json`.
- **`USAGE_API_KEY` keeps its current behavior.** An API-key account has no
  quota windows, and the existing code already suppresses the last-seen line for
  it.
- **No stored measurement, as today.** Accounts with `last_good is None` still
  print the sentinel note alone.
- **Ordering stays as-is.** This proposal does not change `account_headroom`,
  autoswitch, or any ranking. It is a display change only. Whether a rolled
  window should also stop binding `account_headroom` is a real question, and a
  separate one.

### Surfaces to keep in parity

`SENTINEL_NOTES` and `last_seen_note` are shared deliberately so the surfaces
stay word-for-word identical. The same applies here:

- `switcher._usage_entry_lines` — the CLI block above.
- `tui/widgets.py:200` and `tui/data.py` — the dashboard, which calls
  `last_seen_note` today.
- `menubar.py:399` — has less vertical room; the minimum is the binding
  *carried* window rather than the binding window overall, so the menu stops
  showing a percentage that has since rolled.
- `json_output.last_good_usage_fields` — already emits the full stored dict and
  its age, so consumers can classify themselves. Worth confirming `resets_at`
  survives into the JSON payload unmodified rather than adding a derived field.

## Relationship to existing issues

**#376 (open) is the same rule on the healthy path.** It reports that a
past-due reset on a live account renders as `in 0m`, because
`oauth.format_reset` clamps the remaining seconds with `max(0, ...)`, and its
first suggestion is exactly the rolled rendering proposed here:

> When `resets_at` is in the past, the stored percentage is known to be
> obsolete. Render it as unknown (for example `5h: —  window reset Sep 16
> 17:40`) instead of the frozen percentage plus `in 0m`.

The two reports arrive at the same rule from opposite directions. #376 reaches
it from a 45-hour 429 freeze on an *active* account; this one reaches it from a
dead refresh token on an *expired* account. The underlying fact is shared: a
window whose reset instant has passed cannot still hold its measured
percentage, whatever stopped the measurement from refreshing.

So the classifier belongs in `oauth`, used by both paths. #376 also asks for
the age note to mark the whole block rather than only the last row, which is
what the `last known · <age>` header does here. Neither change blocks the
other, and whichever lands first should expose the helper for the second.

**#222 and #8 (both closed, completed) are the precedent.** Reset times were
added to `cswap list` because "the information provided is too limited" for an
exhausted account, and #8's requested rendering was simply "show it in the same
way as usual". That is this proposal's argument applied to the healthy path.
An expired account is the remaining case where the display still collapses, and
the same answer fits it.

**#180 (closed, completed) settled the polling half.** It established that an
exhausted account should keep a measured state rather than aging into
`unavailable`, on the grounds that routing should not drop a known-exhausted
account as unknown. The display half of that argument is unfinished: an expired
account still discards a measurement the store is holding.

**Not related, despite surface similarity.** #165 and #254 concern recovering
the credential itself, and #333 concerns where cswap looks for a token. This
proposal changes nothing about authentication; it only renders what is already
stored.

## Implementation sketch

1. Add a window classifier in `oauth`, next to `fresh_reset_strings`, returning
   carried / rolled / unknown for a window dict and a reference time. Injecting
   `now` keeps it testable without freezing the clock. This is the piece #376
   also needs, so it should land as a shared helper rather than inside either
   caller.
2. Give `_format_usage_lines` a mode that renders rolled windows as `—`, so the
   sentinel branch and the normal branch share one formatter.
3. Replace the sentinel branch's `last_seen_note` call with the header line plus
   the formatted block.
4. Point the TUI and the menu bar at the same helpers.
5. Tests: a stored measurement whose 5h window has rolled but whose 7d has not;
   all windows rolled; a measurement with no `resets_at`; an API-key account;
   and a sentinel account with no measurement at all.

`last_seen_note` stays exported until the TUI and the menu bar have moved over,
then goes.

## Alternatives considered

**Leave it and tell the operator to re-login.** Re-logging in is the action the
proposal helps them decide *between*, when several accounts are expired. Account
2 at 99% on a 7-day window is not worth re-logging in to today; account 1 at 40%
is. Today both look the same.

**Show the full block with no rolled/carried distinction.** Simpler, but it
reprints account 2's three-day-old 99% on a 5h window as though it were current.
That is the specific misreading this proposal exists to remove.

**Assert 0% for rolled windows.** Tempting, and usually right, but cswap cannot
see usage spent outside it. `—` is the honest symbol.
