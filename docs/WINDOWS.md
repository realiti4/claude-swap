# Windows Setup Guide

Everything you need to run claude-swap on Windows 10/11.

---

## Installation

**Requirements:** Python 3.12+, Claude Code CLI, pipx

```powershell
# Install pipx
pip install pipx

# Add pipx to PATH (then restart terminal)
python -m pipx ensurepath

# Install claude-swap
pipx install claude-swap

# Verify
cswap --version
```

> **PATH tip:** After `pipx ensurepath`, close and reopen PowerShell before continuing.

---

## Adding accounts

```powershell
cswap add   # opens browser — log in with account 1
cswap add   # repeat for account 2, 3, etc.
cswap list  # confirm they're all registered
```

---

## Recommended configuration

```powershell
cswap config apply-preset max-drain
```

Equivalent to setting each key by hand:

```powershell
cswap config set autoswitch.threshold 99.9
cswap config set autoswitch.hysteresisPct 0
cswap config set autoswitch.cooldownSeconds 60
cswap config set autoswitch.intervalSeconds 30
cswap config set autoswitch.strategy best
```

This drains each account to 99.9% before rotating to the one with the most
headroom.

---

## Starting auto-rotation

```powershell
cswap auto
```

Keep this terminal open.  claude-swap watches your usage every 30 seconds
and switches automatically.  You'll get a Windows toast notification on
every account swap and when all accounts run out.

### Run in background (without a terminal window)

**Option A — Setup script (recommended)**

This is the "set it up once and forget about it" option: it registers a
Scheduled Task that starts `cswap auto` hidden at every login and restarts it
automatically if it ever crashes, so nothing depends on a terminal staying
open.

```powershell
cd C:\WORK\claude-swap\scripts\windows
.\install-autostart.ps1              # notifications on (default)
.\install-autostart.ps1 -NoNotify    # or suppress them
```

It also starts the task immediately, so auto-switch is live right after you
run it — no reboot or re-login needed. To check on it later or turn it off:

```powershell
Get-ScheduledTask -TaskName "Claude Swap Auto" | Get-ScheduledTaskInfo
.\uninstall-autostart.ps1
```

**Option B — Task Scheduler by hand**

Same result as the script above, done through the GUI — useful if you'd
rather not run a script, or want to see exactly what gets configured:

1. Open **Task Scheduler** (`taskschd.msc`)
2. **Create Basic Task** → name it `Claude Swap Auto`
3. Trigger: **When I log on**
4. Action: **Start a program**
   - Program: `powershell`
   - Arguments: `-WindowStyle Hidden -Command "cswap auto --no-notify"`
5. Finish → right-click the task → **Properties** → **Settings** tab → set
   **"Stop the task if it runs longer than"** to unchecked (the default 3-day
   limit would silently kill a loop meant to run forever)
6. Right-click the task → **Run** to start it immediately

> Use `--no-notify` only if you don't want notifications from the background
> task — but leaving it on (the default) means you'll be notified on every
> switch even while the window is hidden.

**Option C — Windows Terminal in a separate tab**
Just open a new tab and run `cswap auto` there. Simplest option, but it stops
the moment that tab or terminal window closes.

**Option D — PowerShell background job**

```powershell
# Start as a background job in the current session
$job = Start-Job { cswap auto }

# Check it's running
Get-Job

# See its output
Receive-Job $job

# Stop it
Stop-Job $job; Remove-Job $job
```

---

## Notifications

Desktop toast notifications fire for:
- Every account switch (normal priority)
- When all accounts are fully exhausted (alarm sound)
- When an account is quarantined (dead token)

They use the Windows Runtime toast API via PowerShell — no third-party
packages required.

### If notifications don't appear

1. Check Windows Focus Assist isn't blocking them:
   **Settings → System → Focus assist → Off** (or allow the app)

2. Check notification settings:
   **Settings → System → Notifications → Scroll down → Claude Swap**
   and make sure it's enabled.

3. Test manually:
   ```powershell
   powershell -Command "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null; Write-Host 'WinRT loaded OK'"
   ```
   If this errors, your Windows build may not support WinRT notifications
   (rare on Windows 10 1903+).

---

## Credentials storage

On Windows, claude-swap stores credentials as files under:
```
%USERPROFILE%\.claude-swap\credentials\
```

Each slot (`account-1.json`, `account-2.json`, …) holds the OAuth token for
that account.  The active Claude Code session reads from:
```
%USERPROFILE%\.claude\.credentials.json
```

claude-swap atomically replaces this file on every switch.

---

## Known Windows limitations

| Feature | Status |
|---|---|
| Auto-rotation (`cswap auto`) | Fully supported |
| Desktop notifications | Fully supported (Windows 10 1903+) |
| Session mode (`cswap run`) | Not yet supported on Windows |
| macOS Keychain | N/A |

---

## Checking what's happening

```powershell
cswap list        # all accounts with live usage %
cswap status      # which account is active right now
cswap auto --dry-run --threshold 1   # simulate a switch without doing it
```

---

## Updating

```powershell
pipx upgrade claude-swap
```

Or, if running from this fork:
```powershell
cd C:\WORK\claude-swap
git pull
pip install -e .
```
