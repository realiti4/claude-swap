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

**Note:** Restart Claude Code (or close and reopen the VS Code extension tab) after switching for the new account to take effect.

### Refresh expired tokens

If an account's token expires, log back into Claude Code with that account and re-run:

```bash
cswap --add-account
```

This will update the stored credentials without creating a duplicate.

### Add an account from a raw OAuth token

If you only have a long-lived setup-token (e.g., produced by `claude setup-token`)
and you don't want to log in via the browser flow first — useful on headless
servers or when receiving a token from another machine — register it directly:

```bash
cswap --add-token sk-ant-oat01-... --email user@example.com
cswap --add-token sk-ant-oat01-... --email user@example.com --slot 3
cswap --add-token - --email user@example.com           # read token from stdin
cswap --add-token --email user@example.com             # prompt securely (no echo)
```

`--email` is required so cswap's metadata stays aligned with the rest of the
accounts. No Anthropic API calls are made.

### Other commands

```bash
cswap --list                    # Show all accounts with 5h/7d usage and reset times
cswap --status                  # Show current account
cswap --add-account --slot 3    # Add account to a specific slot (prompts before overwrite)
cswap --remove-account 2        # Remove an account
cswap --purge                   # Remove all claude-swap data
```

## Tips

- **Continuing sessions after switching:** You can resume the same Claude Code session after switching accounts. Close Claude Code or the VS Code extension tab, run `cswap --switch` in any terminal, then reopen and select your previous session. Note that the first message on the new account may use extra usage as the conversation cache rebuilds for that account.

## How it works

- Backs up OAuth tokens and config when you add an account
- Swaps credentials when you switch accounts
- Account credentials stored securely using platform-appropriate methods

## Data locations

| Platform | Credentials | Config backups |
|----------|-------------|----------------|
| Windows | Windows Credential Manager | `~/.claude-swap-backup/` |
| macOS | macOS Keychain | `~/.claude-swap-backup/` |
| Linux | File-based (`~/.claude-swap-backup/credentials/`) | `~/.claude-swap-backup/` |

## Backup and migration

Move account data between machines or back it up:

```bash
cswap --export backup.cswap                  # All accounts to a file
cswap --export backup.cswap --account 2      # One account
cswap --export backup.cswap --full           # Include full local ~/.claude.json (same-PC backup)
cswap --import backup.cswap                  # Skips accounts that already exist
cswap --import backup.cswap --force          # Overwrite existing
```

The export file is plaintext JSON. If you need encryption, pipe through your tool of choice (e.g. `cswap --export - | gpg -c > backup.gpg`).

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
