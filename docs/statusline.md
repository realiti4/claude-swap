# Usage from Claude Code's status line

Reference for `cswap statusline`. The short version is in the [README](../README.md#add-an-account-from-a-raw-token-or-api-key) (under *Limits*).

## Why

The usage endpoint doesn't answer setup-token accounts, so cswap can only read their usage by sending a request of its own (the header probe, off by default, or `u` in `cswap watch`). Claude Code already has those numbers. Every response it gets carries the account's rate-limit headers, and it passes the 5-hour and 7-day utilization to its status line command as `rate_limits` (Claude Code 2.1 and later, subscribers only). The hook saves them, so cswap gets them at no cost.

## Commands

```bash
cswap statusline              # installed or not, and the last reading per profile
cswap statusline install      # put cswap-statusline in front of your status line
cswap statusline uninstall    # put your status line back as it was
```

`install` edits `statusLine` in Claude Code's user settings (`~/.claude/settings.json`, or `$CLAUDE_CONFIG_DIR/settings.json`):

| Before | After |
|--------|-------|
| `"command": "~/.claude/statusline.sh"` | `"command": "/path/to/cswap-statusline --then '~/.claude/statusline.sh'"` |
| no `statusLine` | `"command": "/path/to/cswap-statusline"`, which prints `team · 5h 25% (1h 59m) · 7d 61%` |

Other fields (`padding` and so on) are kept. The first change leaves a copy of the file as `settings.json.cswap-backup`. Running `install` again changes nothing. `cswap run` session profiles link their `settings.json` to this one, so they're covered too. `install` refuses a status line it can't run in front of, such as one set to run under PowerShell, and a settings file that isn't valid JSON.

A project's own `.claude/settings.json` that sets `statusLine` takes precedence over the user setting, so the hook doesn't run in that project. `cswap statusline` warns about one in the current folder or above it.

`cswap-statusline` is a second command installed with claude-swap. If you installed claude-swap before this command existed, reinstall it (for example `uv tool install --force claude-swap`) to get it.

## What the hook does

On each render, Claude Code runs the command and writes the payload to its stdin. `cswap-statusline`:

1. starts your command with bash (Git Bash on Windows, as Claude Code does) and passes it the same payload. Your command's output and exit status go back to Claude Code unchanged.
2. While your command runs, it saves the rate limits to `statusline/<profile>.json` in the cswap data folder (mode 0600). It skips a render that brings nothing new.

It loads none of cswap's modules and takes about as long as starting Python. If anything goes wrong while saving, it still lets your command's output through.

What it saves:

- the 5h and 7d `used_percentage` and `resets_at`;
- when the request behind them was sent and when its response arrived, from the end of the session transcript;
- a SHA-256 hash of the access token in use, and when the credential last changed. This applies when the credential is a file (Linux, WSL, Windows) or `CLAUDE_CODE_OAUTH_TOKEN`. The token itself is never written;
- otherwise (the macOS Keychain), the signed-in account's email and organization from `.claude.json`, and when cswap last switched accounts;
- which Claude Code profile it was (the default one, or the `CLAUDE_CONFIG_DIR` folder).

## How a reading is matched to an account

The payload doesn't say which account it belongs to, so cswap matches a reading by:

1. **the token hash**, against the stored token of each setup-token account. This is an exact match.
2. **the profile**, for a `cswap run` session: its folder belongs to one account.
3. **the signed-in account** in `.claude.json` (macOS default profile).

A reading is dropped when its request went out before the credential last changed, or too soon after the change: 5 seconds for a credential file, and 45 seconds for the Keychain, which Claude Code caches for about 30 seconds. After a switch, the numbers Claude Code still shows from the previous account are not credited to the new one. The first response after the switch is.

The collectors (`cswap list`, the dashboard, `cswap auto`, the menu bar) take the readings at the start of each pass. A reading replaces the stored one only when it is newer, and readings older than 6 hours are not used. Readings count as header readings, like the probe's. One without the 5h or 7d window is shown but not used for auto-switching.

## Only setup-token accounts

A normal login's usage comes from the usage endpoint, which also reports the per-model windows and extra usage. A status line reading has only the 5h and 7d windows, so it would replace a fuller reading. cswap therefore ignores readings that match a normal login or an API key.

## Removing it

`cswap statusline uninstall` puts the command back, or removes `statusLine` if you had none. `cswap purge` does the same. If you uninstall claude-swap without either, Claude Code's status line points at a command that no longer exists. Then restore `settings.json.cswap-backup`, or edit `statusLine` by hand.
