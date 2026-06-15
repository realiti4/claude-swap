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

**Note:** You usually don't need to restart — on Linux/Windows the new account is picked up automatically, and on macOS after the Keychain cache expires. To apply it instantly, restart Claude Code or reopen the VS Code extension tab. See [Tips](#tips) for the per-platform details.

### Run multiple accounts at the same time (session mode) `[experimental]`

Launch Claude Code as a specific account in the current terminal only — every other terminal and the VS Code extension stay on your default account, so two accounts can work in parallel.

```bash
cswap run 2                     # launch Claude Code as account 2, here only
cswap run user@example.com      # by email
cswap run 2 -- --resume         # everything after '--' is forwarded to claude
cswap run 2 --no-share          # don't share your ~/.claude customizations
```

Your `~/.claude` customizations (settings, keybindings, CLAUDE.md, skills, commands, agents) are shared into the session by default — use `--no-share` for a bare profile. Conversation history stays per-account.

### Load balancer (Beta)

Run several Claude Code sessions across all your accounts and let cswap keep them
fed. When a session's account nears its usage limit, cswap migrates that session
to a higher-priority account that still has headroom — concentrating load on your
top accounts and only spilling to the next tier when one fills up. Migrations are
minimized (each one re-bills the context window), so a session only moves when its
account is actually exhausted, never for a marginal gain.

Only one subscription? There's nowhere to migrate, so cswap **pauses** the session
instead and **auto-resumes** it (via `claude --resume`) the moment the limit
window resets — your in-flight, dynamic workflow survives the limit instead of
being thrown away.

It's fully **event-driven**: the in-session statusline reports usage on each
message, which is what triggers a rebalance. There's no polling loop and no
background daemon. It's also credential-safe — each session's supervisor owns its
own profile and only ever re-points its own credentials.

#### Setup

```bash
cswap --install                 # embed cswap into Claude Code (one-time, idempotent)
cswap --status                  # shows load-balancer state + embed health
```

`--install` installs the statusline and QoL layer via a non-shared
`settings.json` inside each managed profile, so plain `claude` and
`cswap run` stay completely vanilla. It also runs automatically on upgrade.

#### Launch a managed session

```bash
cswap launch                    # start a load-balanced session in this terminal
cswap launch -- --resume        # everything after '--' is forwarded to claude
cswap launch --no-share         # bare profile (don't share ~/.claude items)
```

`cswap launch` picks the best account (highest priority with headroom), then
supervises claude until it exits — migrating or pausing/auto-resuming as limits
are reached. Managed sessions get QoL defaults: the latest model,
`--dangerously-skip-permissions`, and high effort (any flag you pass yourself
wins). Plain `claude` and `cswap run` are unaffected.

#### Priorities

Higher priority = burned through first.

```bash
cswap --set-priority 2:5        # set Account-2's priority to 5
```

Priorities are also editable in the `cswap --balance` TUI.

#### Dashboard

```bash
cswap --balance                 # settings + live dashboard (enable/disable, tune, priorities)
```

The dashboard shows which sessions are on which accounts, live, and is where you
**enable** the balancer (it's opt-in/off by default), set the threshold/target/
cooldown, and edit priorities.

#### Statusline

Each managed session renders a compact one-liner, updated on every message:

```
⇄ a2 ▕███▁▁▏88% · s2/5          # on Account-2, 88% used, session 2 of 5
→a5 ▕███▁▁▏88% · s2/5           # migration to Account-5 pending
⏸ a2 1h12m · s2/5               # paused, auto-resuming in 1h12m
```

It falls back to ASCII (`>a2 [###-----] 88% s2/5`) on terminals that can't render
the box-drawing glyphs.

> **Beta:** this is new. Migrations re-bill the context window, and the in-session
> "ultracode" effort can't be persisted — the managed `xhigh` effort level is the
> closest you can set outside a session. Please report rough edges via
> [Issues](https://github.com/realiti4/claude-swap/issues).

#### Rate-limit safety net

The balancer migrates **proactively**, the moment the statusline reports an
account crossing the threshold. If a turn still hits a hard **rate limit**
(HTTP 429 — the "Retrying… attempt N/10" storm), a `StopFailure` safety net kicks
in: when the turn fails after Claude exhausts its retries and the session's
account is genuinely rate-limited, cswap auto-switches the session to a fresh
account so the **next** turn recovers — no manual switching. The failed turn
itself is lost (no hook can rescue an in-flight turn — a Claude Code limitation),
so this is a backstop, not a substitute for the proactive migration.

A **401 auth failure** (a "Please run /login · API Error: 401" turn) auto-recovers
the same way: cswap refreshes the account's token and re-seeds the session, and if
the account is logged out it migrates the session to a healthy account (re-login on
one account if they're all dead) — so the **next** turn re-authenticates.

Want more margin? Lower the threshold in `cswap --balance` so the balancer
migrates earlier and rarely reaches a hard limit.

An **overload** (HTTP 529 "Overloaded") is different: it's server/model-side, so
every account hits the same overloaded model — switching accounts wouldn't help.
Managed sessions instead auto-fall-back to a secondary model via Claude's native
`--fallback-model` (default `sonnet`). Override it per launch:

```bash
cswap launch -- --fallback-model haiku   # your flag wins; cswap won't double-add
```

#### cmux integration (Beta, macOS)

If you use [cmux](https://cmux.com), wire the balancer into it:

```bash
cswap cmux setup                # add a "Balanced Claude (cswap)" command to cmux
cswap cmux 2                    # fan out 2 managed sessions, one per workspace
cswap cmux 2 -- --resume        # forward args after '--' to each session's claude
```

`setup` backs up `~/.config/cmux/cmux.json` to a timestamped `.bak`, merges the
surface idempotently (your other config is preserved), then validates and
reloads. Afterwards you can spawn a load-balanced session from cmux's command
palette / plus-button.

`cswap cmux N` opens N cmux workspaces, each running `cswap launch`. The
balancer's online reservation spreads them, so each pane **lands on a different
account** — and each renders the same compact statusline as above. cswap pins
every pane to its own profile, surviving cmux's `claude` wrapper.

### Refresh expired tokens

If an account's token expires, log back into Claude Code with that account and re-run:

```bash
cswap --add-account
```

This will update the stored credentials without creating a duplicate.

### Other commands

```bash
cswap run 2                     # Run an account in this terminal only (session mode)
cswap launch                    # Start a load-balanced managed session (Beta)
cswap cmux setup                # Add a balanced-Claude command to cmux (Beta, macOS)
cswap cmux 2                    # Fan out 2 managed sessions across accounts in cmux
cswap --list                    # Show all accounts with 5h/7d usage and reset times
cswap --status                  # Show current account + load-balancer/embed health
cswap --add-account --slot 3    # Add account to a specific slot (prompts before overwrite)
cswap --remove-account 2        # Remove an account
cswap --install                 # Embed cswap into Claude Code for load balancing (Beta)
cswap --balance                 # Load-balancer dashboard + settings (Beta)
cswap --set-priority 2:5        # Set an account's balancing priority (Beta)
cswap --tui                     # Launch the interactive arrow-key menu
cswap --upgrade                 # Upgrade claude-swap to the latest version
cswap --purge                   # Remove all claude-swap data
```

## Tips

- **Do you need to restart after switching?** Usually not. On **Linux and Windows**, credentials are stored in a file and Claude Code re-reads them whenever that file changes, so the new account takes effect on your next message — no restart needed. On **macOS**, credentials live in the Keychain, which Claude Code caches for about 30 seconds; a running session picks up the switch once that cache expires. Restart Claude Code (or close and reopen the VS Code extension tab) only if you want the change to apply instantly.
- **Continuing sessions after switching:** You can keep using the same Claude Code session after switching — run `cswap --switch` in any terminal and carry on. If you'd prefer a clean start, close and reopen Claude Code (or the VS Code extension tab) and use `--resume` to pick your previous session. Either way, the first message on the new account may use extra usage as its conversation cache rebuilds.

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

### Add an account from a raw OAuth token

If you only have a long-lived setup-token (e.g., produced by `claude setup-token`)
and you don't want to log in via the browser flow first — useful on headless
servers or when receiving a token from another machine — register it directly:

```bash
cswap --add-token sk-ant-oat01-...
cswap --add-token sk-ant-oat01-... --slot 3
cswap --add-token - --slot 3                 # read token from stdin
cswap --add-token --email user@example.com   # optional label override
```

`--email` is optional; omitted values use `setup-token-{slot}@token.local`.
No Anthropic API calls are made.

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
