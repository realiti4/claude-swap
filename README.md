# claude-swap

Multi-account switcher for Claude Code. Easily switch between multiple Claude accounts without logging out. Works with both the Claude Code CLI and the VS Code extension.

## Installation

### Using uv (recommended)

```bash
uv tool install claude-swap
```

### Using pipx

```bash
pipx install claude-swap
```

### From source

```bash
git clone https://github.com/realiti4/claude-swap.git
cd claude-swap
uv sync
uv run cswap --help
```

### Updating

```bash
cswap --upgrade        # uv/pipx installs on macOS/Linux: auto-detects and upgrades
# or run your installer directly:
uv tool upgrade claude-swap
pipx upgrade claude-swap
```

## Usage

### Add your first account

Log into Claude Code with your first account, then:

```bash
cswap --add-account
```

### Add more accounts

Log in with another account, then:

```bash
cswap --add-account
```

### Switch accounts

Rotate to the next account:

```bash
cswap --switch
```

Or switch to a specific account:

```bash
cswap --switch-to 2
cswap --switch-to user@example.com
```

Or let claude-swap auto-pick by remaining quota — `cswap --switch --strategy best` (most quota left) or `--strategy next-available` (skip rate-limited accounts).

**Note:** You usually don't need to restart — on Linux/Windows the new account is picked up automatically, and on macOS after the Keychain cache expires. To apply it instantly, restart Claude Code or reopen the VS Code extension tab. See [Tips](#tips) for the per-platform details.

### Run multiple accounts at the same time (session mode)

Launch Claude Code as a specific account in the current terminal only — every other terminal and the VS Code extension stay on your default account, so two accounts can work in parallel.

```bash
cswap run 2                     # launch Claude Code as account 2, here only
cswap run user@example.com      # by email
cswap run 2 -- --resume         # everything after '--' is forwarded to claude
cswap run 2 --no-share          # don't share your ~/.claude customizations
```

Your `~/.claude` customizations (settings, keybindings, CLAUDE.md, skills, commands, agents) are shared into the session by default — use `--no-share` for a bare profile. Conversation history stays per-account.

### Refresh expired tokens

If an account's token expires, log back into Claude Code with that account and re-run:

```bash
cswap --add-account
```

This will update the stored credentials without creating a duplicate.

### Other commands

```bash
cswap run 2                     # Run an account in this terminal only (session mode)
cswap --list                    # Show all accounts with 5h/7d usage and reset times
cswap --status                  # Show current account
cswap --add-account --slot 3    # Add account to a specific slot (prompts before overwrite)
cswap --remove-account 2        # Remove an account
cswap --tui                     # Launch the interactive arrow-key menu
cswap --upgrade                 # Upgrade claude-swap to the latest version
cswap --purge                   # Remove all claude-swap data
```

### Auto-switch (macOS only)

Automatically switch to the next available account when your current account's quota nears its limit — no manual intervention required.

#### Background daemon (recommended)

Install a launchd agent that monitors usage and switches in the background:

```bash
cswap auto on          # Enable auto-switch and install the launchd daemon
cswap auto off         # Disable and uninstall the daemon
cswap auto status      # Show daemon status and current config
```

The daemon starts automatically at login and restarts if it crashes.

#### Foreground watch mode

Run the monitor in the current terminal (useful for debugging or one-off sessions):

```bash
cswap watch
```

Press `Ctrl+C` to stop.

#### Strategies

Two policies decide *when* to switch (`cswap auto on --strategy <name>`, or `cswap watch --strategy <name>`):

- **`consume-first`** (default for new installs) — *proactive*. Keeps you on the account to consume **first**: the available account whose **weekly window resets soonest** (use-it-or-lose-it, so quota isn't wasted at reset). It switches whenever that account changes — a reset re-orders the queue, the current account exhausts, or an account that hit its 5-hour limit clears and reclaims the soonest-reset slot. If the chosen account hits its 5-hour session limit while its weekly still has room, it moves to another account temporarily and returns once the 5-hour window resets.
- **`reactive`** — *threshold-only* (the original behavior). Stays put until the active account crosses a limit (5h ≥ 98% or 7d ≥ 99%), then jumps to the account with the soonest weekly reset among those that still have headroom.

```bash
cswap auto on --strategy consume-first   # proactive (default)
cswap auto on --strategy reactive        # switch only at 98%/99%
cswap auto status                        # shows the active strategy
```

> `cswap auto on --strategy X` **persists** the choice to the daemon config. `cswap watch --strategy X` applies the strategy to that foreground session **only** — it does not change the persisted daemon config. Use `cswap auto on --strategy X` to make the change stick.

#### How it works

- **Session threshold** (default 98%): the 5-hour limit. In `reactive` mode crossing it triggers a switch; in `consume-first` mode it marks an account temporarily unavailable (skipped, then reclaimed after its 5-hour reset).
- **Weekly threshold** (default 99%): the **weekly window** — a fixed weekly quota that resets on a schedule (not a gradually-rolling average; at reset an account's weekly utilization drops cleanly back to ~0%, which is what makes "consume the soonest-resetting account first" worthwhile). An account at/over it is excluded until it resets.
- The next account is chosen by: earliest reset time → most remaining headroom → rotation order.
- Accounts running a separate `cswap run` **session-mode** profile are excluded from the candidate pool (so the daemon never swaps an account out from under a dedicated `cswap run` session). Your **default-login** account — the one an ordinary `claude` chat reads — is **not** pinned: the daemon switches it when it hits the 5-hour limit. An in-flight conversation isn't migrated mid-stream; the switch takes effect on your **next message** (see [Tips](#tips), "Continuing sessions").
- **Inactive accounts.** The background daemon reads each peer's usage but never **refreshes** an expired inactive account's token — an OAuth refresh rotates a one-time refresh token, and a rotation the daemon can't persist (e.g. a locked Keychain while the Mac is asleep) would otherwise brick that account until re-login. So a peer that has been inactive for a while shows its **last-known** usage in `cswap auto status` until you next touch it (a foreground `cswap --list`, or a switch, refreshes and re-saves it). Reset times are fixed, so this never affects the consumption ranking.
- **Active-account usage cache (`usage_cache_file`).** The **active** account is the one Claude Code is actively using *and* polling for its statusline, so the daemon's own usage call on it contends with those and is the one that flaps under rate limits. If a CodexBar-compatible statusline is caching usage (default path `/tmp/claude/statusline-usage-cache.json`), the daemon **reuses that cache** for the active account instead of making a redundant call — but only when the cache is fresh (≤90s) *and* its 7‑day reset matches the active account's last-known reset (an account-identity check, since the cache carries no account id). Otherwise it falls back to a direct fetch. Set `usage_cache_file` to `""` in `auto-switch.json` to disable.
- A macOS notification is sent when a switch happens or all accounts are exhausted (requires `notify: true`, which is the default).
- **Polling.** In **`reactive`** mode polling is minimal: a normal check reads only the active account's usage (one API call), and the other accounts are read only when the active account crosses a threshold and a switch is possible. In **`consume-first`** mode every check polls **all** accounts (it has to, to rank them by reset time) — still gentle at the same 60s–300s cadence (slower when far away). As the active account's **5-hour** window nears the limit the cadence tightens to a brief critical interval (`critical_interval`, default 15s) so the switch fires **before** the hard 100% wall — which would otherwise abort the in-flight request and force a restart. This tight poll only fires close to the 5-hour limit and stops the moment the switch happens, so it doesn't raise the steady-state poll rate.

#### Connection loss

The monitor depends on the Anthropic usage API. If the network drops and usage can't be read, the auto-switcher fails safely:

- **Safe:** it never switches on stale or unknown data — switching onto a possibly-exhausted account would be risky, and during a real outage Claude Code is blocked anyway, so no quota is burning. It simply waits.
- **Visible:** `cswap auto status` shows a `monitoring: offline since …` line and a one-shot "offline" notification fires.
- **Recoverable:** the first successful poll resumes monitoring automatically and sends a one-shot "back online" notification. With a usage cache configured (the default — see `usage_cache_file` above) the daemon retries at the `min_interval` floor while offline: a fresh statusline cache recovers it without an API call, so a transient rate‑limit (the active account is the most polled, so it's the one that 429s under contention) doesn't leave it "offline" for long. With no cache it backs off exponentially (capped) instead of hammering a real outage.

#### Configuration

Adjust thresholds when enabling:

```bash
cswap auto on --session-threshold 95 --weekly-threshold 98
cswap auto on --no-notify          # disable desktop notifications
```

The config is stored in `<backup-root>/auto-switch.json` and survives daemon restarts.

> **Note:** Auto-switch is macOS-only. The `watch` command and daemon require macOS. The underlying decision engine (`cswap auto on/off/status`) works on all platforms but the daemon integration uses launchd.

## Tips

- **Do you need to restart after switching?** Usually not. On **Linux and Windows**, credentials are stored in a file and Claude Code re-reads them whenever that file changes, so the new account takes effect on your next message — no restart needed. On **macOS**, credentials live in the Keychain, which Claude Code caches for about 30 seconds; a running session picks up the switch once that cache expires. Restart Claude Code (or close and reopen the VS Code extension tab) only if you want the change to apply instantly.
- **Continuing sessions after switching:** You can keep using the same Claude Code session after switching — whether you ran `cswap --switch` manually or the **auto-switch daemon** swapped the account for you — and carry on; the new account takes effect on your next message (on macOS once the ~30s Keychain cache expires). An in-flight turn is not migrated mid-stream: it stays on the account it started on. This is why the daemon polls tightly as the 5-hour limit approaches and switches **before** 100% — so your next message lands on a fresh account rather than hitting the wall. If you'd prefer a clean start, close and reopen Claude Code (or the VS Code extension tab) and use `--resume` to pick your previous session. Either way, the first message on the new account may use extra usage as its conversation cache rebuilds.

## How it works

- Backs up OAuth tokens and config when you add an account
- Swaps credentials when you switch accounts
- Account credentials stored securely using platform-appropriate methods

## Data locations

| Platform | Credentials | Config backups |
|----------|-------------|----------------|
| Windows | File-based (inside the backup directory, under `credentials/`) | `~/.claude-swap-backup/` |
| macOS | macOS Keychain | `~/.claude-swap-backup/` |
| Linux / WSL | File-based (inside the backup directory, under `credentials/`) | `${XDG_DATA_HOME:-~/.local/share}/claude-swap/` |

Session-mode profiles (`cswap run`) live under the backup directory in `sessions/`.

On Linux/WSL, set `XDG_DATA_HOME` to override the default location. Data from older installs under `~/.claude-swap-backup/` is migrated automatically on first run.

## Advanced

### Backup and migration

Move account data between machines or back it up:

```bash
cswap --export backup.cswap                  # All accounts to a file
cswap --export backup.cswap --account 2      # One account
cswap --export backup.cswap --full           # Include full local ~/.claude.json (same-PC backup)
cswap --import backup.cswap                  # Skips accounts that already exist
cswap --import backup.cswap --force          # Overwrite existing
```

The export file is plaintext JSON. If you need encryption, pipe through your tool of choice (e.g. `cswap --export - | gpg -c > backup.gpg`).

### JSON output for scripting

Add `--json` to `--list`, `--status`, `--switch`, or `--switch-to` to emit a single machine-readable JSON object on stdout (human-readable notices go to stderr). Useful for scripting auto-swap and quota tracking.

```bash
cswap --list --json                 # all accounts with usage/quota
cswap --status --json               # current active account
cswap --switch --strategy best --json   # switch, then report the result
cswap --switch-to 2 --json
```

<details>
<summary>Example output & schema notes</summary>

```json
{
  "schemaVersion": 1,
  "activeAccountNumber": 2,
  "accounts": [
    { "number": 2, "email": "you@example.com", "active": true, "usageStatus": "ok",
      "usage": { "fiveHour": { "pct": 25.0, "resetsAt": "2026-06-22T23:29:59Z" },
                 "sevenDay": { "pct": 16.0, "resetsAt": "2026-06-26T17:59:59Z" } } }
  ]
}
```

Every payload carries a `schemaVersion` (currently `1`); on a handled error stdout is `{"schemaVersion":1,"error":{...}}` with a non-zero exit code. `--switch`/`--switch-to` report `{"switched": true|false, "from": …, "to": …, "reason": …}`.

</details>

### Add an account from a raw token or API key

If you only have a long-lived setup-token (e.g., produced by `claude setup-token`)
or a managed API key (`sk-ant-api...`) and you don't want to log in via the browser
flow first — useful on headless servers or when receiving a token from another
machine — register it directly. The token type is auto-detected:

```bash
cswap --add-token sk-ant-oat01-...           # OAuth setup-token
cswap --add-token sk-ant-api03-...           # managed API key
cswap --add-token sk-ant-oat01-... --slot 3
cswap --add-token - --slot 3                 # read token from stdin
cswap --add-token --email user@example.com   # optional label override
```

`--email` is optional; omitted values use `setup-token-{slot}@token.local`
(or `api-key-{slot}@token.local` for API keys). No Anthropic API calls are made.

**API-key accounts.** An `sk-ant-api...` value registers a managed API-key account
(the kind Claude Code uses after `/login` with a key) rather than an OAuth
setup-token. It switches like any other account; since API keys have no subscription
quota, they show no usage and the usage-aware `--switch` strategies never skip them as
rate-limited.

## Uninstall

Remove all data:

```bash
cswap --purge
```

Then uninstall the tool:

```bash
uv tool uninstall claude-swap
# or
pipx uninstall claude-swap
```

## Requirements

- Python 3.12+
- Claude Code installed and logged in

## License

MIT
