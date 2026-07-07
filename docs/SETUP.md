# Setup Guide

Complete walkthrough for getting claude-swap running from scratch.

---

## Prerequisites

| Requirement | Check | Install |
|---|---|---|
| Python 3.12+ | `python --version` | [python.org](https://www.python.org/downloads/) |
| Claude Code CLI | `claude --version` | [docs.anthropic.com](https://docs.anthropic.com/en/docs/claude-code) |
| pipx or uv | `pipx --version` | `pip install pipx` |

---

## Step 1 — Install claude-swap

### Option A: pipx (recommended)
```bash
pip install pipx
pipx install claude-swap
```

### Option B: uv
```bash
pip install uv
uv tool install claude-swap
```

### Option C: from this fork (latest changes)
```bash
git clone https://github.com/dabirideji/claude-swap
cd claude-swap
pip install -e .
```

Verify:
```bash
cswap --version
```

---

## Step 2 — Add your accounts

Register each Claude account you want in the rotation pool.

```bash
cswap add
```

This opens a browser and logs you in.  Repeat for each account:

```bash
cswap add   # account 1
cswap add   # account 2
cswap add   # account 3 (add as many as you have)
```

Check your accounts are registered:

```bash
cswap list
```

You should see a table like:

```
  #  Email                    5h %   7d %   Status
  1  work@example.com          12%    34%   active
  2  personal@example.com       3%     8%   ready
```

---

## Step 3 — Configure for maximum token use

Set the threshold as high as the tool allows so rotation happens only when
you've genuinely drained the current account:

```bash
cswap config set autoswitch.threshold 99.9
cswap config set autoswitch.hysteresisPct 0
cswap config set autoswitch.cooldownSeconds 60
cswap config set autoswitch.intervalSeconds 30
cswap config set autoswitch.strategy best
```

| Setting | Value | Why |
|---|---|---|
| `threshold` | `99.9` | Only rotate at 99.9% — drain as much as possible |
| `hysteresisPct` | `0` | No dead-band: always pick the best account immediately |
| `cooldownSeconds` | `60` | 1-minute minimum between switches (prevents flip-flop) |
| `intervalSeconds` | `30` | Check usage every 30 seconds |
| `strategy` | `best` | Always switch to the account with the most quota left |

Verify your config:
```bash
cswap config
```

---

## Step 4 — Start auto-rotation

```bash
cswap auto
```

This runs the rotation loop in your terminal.  To run in the background:

**Windows** — open a separate PowerShell window and run `cswap auto` there, or
use the Windows Task Scheduler method in [WINDOWS.md](./WINDOWS.md).

**macOS / Linux** — background it with:
```bash
cswap auto &
# or as a persistent service:
# see LINUX.md / MACOS.md (coming soon)
```

### With desktop notifications (default: on)

Notifications fire automatically on every account switch and when all
accounts are exhausted:

```bash
cswap auto            # notifications on by default
cswap auto --no-notify  # suppress notifications
```

---

## Step 5 — Verify it's working

In a separate terminal, check the live status:

```bash
cswap list      # account table with usage bars
cswap status    # which account is currently active
```

Simulate a near-limit account to confirm rotation fires:

```bash
cswap auto --dry-run --threshold 1
# (triggers immediately on any non-zero usage, without actually switching)
```

---

## Day-to-day commands

| Command | What it does |
|---|---|
| `cswap list` | Show all accounts with 5h/7d usage percentages |
| `cswap status` | Show the currently active account |
| `cswap switch` | Manually rotate to the next best account |
| `cswap switch 2` | Switch directly to account #2 |
| `cswap auto` | Start the automatic rotation loop |
| `cswap auto --once` | Evaluate once and exit (useful for cron) |
| `cswap add` | Register a new Claude account |
| `cswap remove` | Remove an account from the pool |
| `cswap config` | Show all settings |
| `cswap config set <key> <value>` | Change a setting |

---

## Troubleshooting

### `cswap: command not found`
pipx's bin directory isn't in your PATH.  Run:
```bash
pipx ensurepath   # then restart your terminal
```

### Account shows as "quarantined"
The account's OAuth token has expired and can't be refreshed automatically.
Re-authenticate:
```bash
cswap add --slot 2   # replace 2 with the slot number shown in cswap list
```

### Rotation isn't happening near the limit
Check the threshold is set correctly:
```bash
cswap config get autoswitch.threshold
# should show: 99.9
```

### Notifications not appearing on Windows
See [WINDOWS.md — Notifications](./WINDOWS.md#notifications).

---

## Next steps

- [WINDOWS.md](./WINDOWS.md) — Windows-specific setup, background service, Task Scheduler
- Advanced: use `cswap run 2` to open a parallel terminal on account #2 while
  account #1 is active (experimental, not available on Windows yet)
