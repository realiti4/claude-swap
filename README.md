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

#### How it works

- **Session threshold** (default 98%): when the active account's 5-hour usage reaches this percentage, auto-switch triggers.
- **Weekly threshold** (default 99%): same logic for the 7-day rolling window.
- The next account is chosen by: earliest reset time → most remaining headroom → rotation order.
- Accounts with active Claude Code sessions are excluded from the candidate pool.
- A macOS notification is sent when a switch happens or all accounts are exhausted (requires `notify: true`, which is the default).
- **Polling is light on the API.** The monitor polls between 60s (when the active account is near a limit) and 300s (when it's far away). A normal check reads only the active account's usage (one API call); the other accounts are read only when the active account actually crosses a threshold and a switch is possible.

#### Connection loss

The monitor depends on the Anthropic usage API. If the network drops and usage can't be read, the auto-switcher fails safely:

- **Safe:** it never switches on stale or unknown data — switching onto a possibly-exhausted account would be risky, and during a real outage Claude Code is blocked anyway, so no quota is burning. It simply waits.
- **Visible:** it backs off (exponentially, capped) instead of hammering the network, sends a one-shot "offline" notification, and `cswap auto status` shows a `monitoring: offline since …` line.
- **Recoverable:** the first successful poll after the outage resumes monitoring automatically and sends a one-shot "back online" notification.

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
