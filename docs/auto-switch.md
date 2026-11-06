# Automatic switching: the exact rules

Reference for `cswap auto`, and for the menu bar's auto-switch, which runs the same engine. Everyday use is in the [README](../README.md#automatic-switching).

## Strategies

Set with `--strategy`, or `cswap config set autoswitch.strategy`.

- `best` stays put until the active account nears its limit, then moves to the account with the most quota left.
- `weekly-first` switches at the same lines as `best`, with the same landing rules and hysteresis margin, but among the accounts that qualify it lands on the one whose **weekly window resets soonest** (most quota left breaks ties), so weekly quota that would otherwise expire unused gets spent first. Its at-limit and failover escapes, and the all-over-the-lines case, rank by quota left exactly as `best` does.
- `consume-first` (default) proactively keeps you on the account whose **weekly window resets soonest** — use-it-or-lose-it — switching to a sooner-resetting account (with room to spare) even below the threshold, so perishable weekly quota isn't wasted.
- `dynamic` ranks like `consume-first` but also re-checks whether a `--model` window is really what's binding each tick, and never lands on an account that has no room on the window actually in force. Its own proactive switch point is fixed at ~97% used, not the configured `--threshold`; that still decides, with the weekly line when one is set, whether a fleet-wide `--model` blackout drops the model set for ranking, and it steers poll cadence and the exhausted-fleet recovery hold.
- `priority-consume-first` follows your own ranking first (see *Account priority tiers* below), then the soonest weekly reset within a tier.

## Cooldown and hysteresis

A cooldown (default 5 min) and a hysteresis margin stop it flip-flopping near the threshold: a proactive switch only lands on an account that's below the threshold *and* better than the current one by the margin — a candidate that clears the margin is always taken, but two accounts hovering at the line never ping-pong. `consume-first` (the default) is the exception: no margin. Below the threshold it moves only to a sooner-resetting account, and at the threshold it takes any account still under it, soonest reset first. `dynamic` admits candidates below its own ~97% bar instead of the configured threshold. When every account is already over its switch lines, it moves to the one with the most quota left before a limit (the lowest usage on its fullest window, whatever line that window carries) under `best`, `weekly-first`, `consume-first` and `dynamic` alike, once that account has twice the current one's remaining quota or 3 points more than it, whichever is less. When every account is exhausted it keeps checking on a bounded slow cadence, waking sooner for an imminent reset. `priority-consume-first` does neither: it holds rather than land on an account over its lines (see [Account priority tiers](#account-priority-tiers)).

## A separate line for the weekly windows

Set with `--weekly-threshold 97`, or `cswap config set autoswitch.weeklyThreshold 97`.

One threshold compared against the busiest window forces the same number on windows that want different ones — the 5-hour window recycles in hours (an overshoot costs one interrupted turn), while the weekly window is the budget (quota unspent before its reset is gone). With a weekly line, the 7-day window and any per-model window (`--model`) are judged against it and the threshold keeps governing the 5-hour window; every window is compared against its own line, so 7d 93% under a 97 line holds while 5h 95% under an 85 line switches, and the log names the window it measured (`7d 93% < 97%`). Points on different windows are not the same amount of work — the lines are comparable, the clocks are not. `dynamic` keeps its own ~97% switch point and ranks proactive landings on raw headroom against it, so the weekly line moves neither when `dynamic` leaves the active account nor which account its proactive switch lands on. The one exception is `--model`: a model window is judged against the weekly line, so there it decides whether a fleet-wide model blackout drops the model set and widens the active account's headroom.

## Per-account threshold

`cswap threshold <num|email> <pct>` gives one account its own switch-away line (50–99.9) in place of the global one: auto-switch leaves that account when *its* line is reached and prefers candidates still under *theirs*, with ranking and the hysteresis margin counting room under each account's own line rather than raw headroom. Like the global threshold it is a switch-away line, not a hard cap — with nothing under its line, or the active account at its limit, it still lands somewhere rather than stall (`priority-consume-first` is the exception: it holds, see below). With a weekly line set, the override applies to the 5-hour window only (under `dynamic` it applies to every window). Accounts without one keep the global threshold. `cswap threshold` alone lists the default, every override and every cap, `--unset` clears one, and the [TUI](../README.md#interactive-dashboard-tui) shows a `th 85%` badge.

## Per-account weekly cap

`cswap threshold <num|email> <pct> --weekly-cap` (50–100, `--unset` clears) is a hard ceiling, not a line: auto-switch never stays on an account while any of its weekly windows (7d and every per-model one) reads at or past the cap, and never lands on it, whatever the strategy, even when every other account is walled (work may pause on the one that returns soonest rather than use the capped account past its cap). The 5-hour window never counts toward the cap and `--weekly-threshold` does not move it; 100 means use to exhaustion (no cap). With no other account to go to, the capped account stays and the engine reports it is blocked. `cswap switch <num>` to a capped account warns and still switches (as does a bare rotation that lands on one), and the engine moves off it on its next tick; `--strategy best` and `--strategy next-available` never pick one. The cap is a veto layered on the strategy's soft lines, not a replacement for them: a lower `--weekly-threshold` can still make the account leave earlier when another account qualifies, but nothing moves the hard stop at the cap. The cap is checked when the active account is polled. Within 3 points below the cap that account is polled every 2 minutes (up to 10% jitter) while its weekly reading keeps moving; a poll that reads the same as the one before lets the band go and the interval backs off toward 5 minutes, so an idle account parked below its cap does not spend the usage endpoint's request budget (28 to 30 an hour, shared by every machine polling the account). At 0.3 points a minute the overshoot stays under 1 point while the reading moves, and under 1.7 points when a burn resumes after the account sat idle in the band (one normal interval of burn, up to 5.5 minutes). An account the engine lands on just under its cap is first polled within 3 minutes (3.3 with jitter), because the cap is pinned at the start of the next tick, before that poll is planned. After a usage-endpoint 429 the interval is floored at 6 minutes and backs off toward 30, so the overshoot can reach about 10 points at that rate until the 429 window (an hour) clears. The [TUI](../README.md#interactive-dashboard-tui) shows a `cap 90%` badge (`CAPPED` once reached) and sets it from the menu (*Weekly cap…*), which also clears the account's own switch line so its 5-hour window follows the global one; on the CLI the two stay independent, so `cswap threshold <num> --unset` does that.

## Account priority tiers

`cswap priority <num|email> <tier>` ranks an account for the `priority-consume-first` strategy: higher tiers are used first, accounts without one are tier 0 (valid tiers are 0–1000), and within a tier the account whose weekly window resets soonest goes first. It moves back to a higher tier as soon as that account is usable again (after the cooldown), and it never lands on an account at or over its switch lines — when nothing qualifies, even at the limit, it holds and keeps polling instead of relaxing them. Tiers alone change nothing; `best`, `weekly-first`, `consume-first`, and manual switching ignore them. It uses neither the hysteresis margin nor the exhausted slow cadence described above: equal tiers and reset times keep the active account, and a blocked state keeps polling at the normal interval. `cswap priority` lists every account's tier and `--unset` clears one.

## Holding an account out of rotation

To hold an account out of rotation yourself — a work account you don't want touched, one you're resting — run `cswap disable <num|email>`; `cswap enable <num|email>` puts it back. Disabled accounts are skipped by auto-switch, bare `cswap switch`, and the `best` / `next-available` strategies, but stay fully managed and remain a valid explicit `cswap switch <num|email>` target, with the caveat that a RUNNING auto-switch leaves a disabled account on its next tick, so an explicit switch onto one sticks only while auto is stopped. They show a `(disabled)` marker in `cswap list`, in the [TUI](../README.md#interactive-dashboard-tui), and in the [menu bar](../README.md#menu-bar-macos) — both of which also let you toggle the state in place (TUI: menu → *Disable / enable account…*; menu bar: *Disable / enable account*).

## Per-model limits

By default only the account-wide 5h/7d windows drive switching. If you work on one model and hit its **weekly per-model limit** first (e.g. Fable), add `--model Fable` (or `cswap config set autoswitch.model Fable`) to fold that model's window into the decision, so it switches off an account whose model quota is spent even while its 5h/7d windows still have room.

**Model names** are Anthropic's own per-model `display_name`s, matched case-insensitively. The exact strings for your accounts are the per-model rows in `cswap list` (e.g. a line reading `Fable: 100%`).

## Accounts with no 5-hour window

When the usage API reports no `five_hour` window for an account (verified Ultra accounts have no 5-hour limit), cswap judges it on its 7-day window and any `--model` window alone and shows no 5h row.

## Usage polling

Usage polling is adaptive — a couple of accounts per check, busy alternates watched more closely, and exhausted ones checked about every ten minutes (or slower after 429s) — so API traffic stays flat no matter how many accounts you manage.

## When something fails

It fails safe: if a usage check errors it keeps trusting the last-known numbers, for a bounded time, while retries back off, and an expired token on an idle machine makes it hold rather than fail over (Claude Code refreshes the token on your next message).

An account whose refresh token has died is quarantined and reported until you either log in with it and re-run `cswap add --slot N`, or replace its stored credentials from a known-good export — a plain `cswap import backup.cswap` replaces dead-token slots on its own (`--force` is still required to replace other existing accounts; note a stale export can carry an already-superseded token). API-key accounts are never rotated onto unless you pass `--include-api-key-accounts`.

## Running alongside Claude Code

Runs safely alongside Claude Code: switches take the same credential locks Claude Code uses, so a swap never collides with a token refresh.

## Cron and systemd timers

For cron/systemd timers, `--once` reports the outcome in its exit code (`0` switched, `1` error, `2` nothing to do, `3` blocked — no viable target), and `--json` emits one JSON event per line:

```bash
*/5 * * * * cswap auto --once --json >> ~/.cswap-auto.log 2>&1
```

## Defaults

Defaults like the threshold and cooldown are configurable with `cswap config set autoswitch.threshold 80` — flags override them (see [Configuration](../README.md#configuration)).
