# claude-swap

Multi-account switcher for Claude Code. Easily switch between multiple Claude accounts without logging out.

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

Log out of Claude Code, log in with another account, then:

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

**Note:** Restart Claude Code after switching for the new account to take effect.

### Other commands

```bash
cswap --list              # List all managed accounts
cswap --status            # Show current account
cswap --remove-account 2  # Remove an account
cswap --purge             # Remove all claude-swap data
```

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
