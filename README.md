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
