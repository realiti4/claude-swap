# Menu bar app (macOS): how it runs

Reference for `cswap menubar`. Installing it and everyday use are in the [README](../README.md#menu-bar-macos).

## Lifecycle

`cswap menubar` hands the menu bar to launchd and returns: it keeps running after you close the terminal, comes back at login and is restarted if it crashes. Running the command again only tells you it is already up. *Quit* in the menu stops it until the next login. *Settings → Start at login* turns the login start off, after which `cswap menubar` runs it for the current session only. `cswap menubar --uninstall-service` removes it altogether, and `cswap menubar --foreground` runs it attached to the terminal instead, for debugging.

```bash
cswap menubar --service-status      # running? pid? starts at login? auto-switch on?
cswap menubar --install-service     # register the login start again and restart it now
cswap menubar --uninstall-service   # stop it and remove the LaunchAgent
cswap menubar --foreground          # run attached to this terminal, for debugging
```

## Auto-switch

Enable *Settings → Auto-switch accounts* to run the same engine as [`cswap auto`](../README.md#automatic-switching) in the background; it shares the `autoswitch.*` settings, so the menu bar and CLI stay in sync. Off until you turn it on: `cswap menubar --auto-switch` turns it on from the terminal (`--no-auto-switch` off), and both that command and `--service-status` say which it is. The menu's first row says whether auto-switch is running, off, or stopped (click it to restart) and when usage was last refreshed; the title carries a ⚠ while a stopped engine waits for that click.

## Codex usage

If the [Codex CLI](https://github.com/openai/codex) has run on this Mac, the menu shows its 5-hour and weekly limits in a greyed row under the accounts, for example `Codex · Plus  5h 1% (4h 59m) · 7d 0% (6d 18h)`; hover the row for when the reading was taken. `cswap list` prints the same figures as a block after the accounts. Without a reading nothing is shown, and the *Settings → Show Codex usage in menu bar* switch is not offered (unless it is already on). Turned on, the switch appends `Codex 1% · 0%` to the title (only while a Claude account is active), choosing the 5h and 7d figures by *Title percentage* like the Claude ones; `--json` output is unchanged.

The figures come from the session logs Codex writes under `$CODEX_HOME/sessions` (default `~/.codex/sessions`), not from Codex or OpenAI: nothing is sent, no login is read, and no model runs. They are therefore as of the last Codex turn on this Mac, and use from another device is not visible. A window whose reset time has passed is shown as 0 % with no reset time, because no newer reading exists on this Mac. What is read, and how, is in the docstring of `claude_swap/codex_usage.py`.

## The wrapper app

`cswap menubar` runs from a small wrapper app, `/Applications/claude-swap Menubar.app`, that it builds (and refreshes after upgrades) on every start. The wrapper gives it the bundle identifier `com.claude-swap.menubar`, which menu bar managers (Dimble, Ice, Bartender, SaneBar) need to list, hide and order the icon. If `/Applications` is not writable the bundle lands in the data directory with a one-line warning; there, the first time a manager hides anything the icon goes down with it. Set `CLAUDE_SWAP_NO_BUNDLE=1` to run in the plain interpreter instead.

## Files

The plist lives at `~/Library/LaunchAgents/com.cswap.menubar.plist` while Start at login is on and in the data directory while it is off; logs go to `~/Library/Logs/com.cswap.menubar.{log,err}`.

## Upgrades

`cswap upgrade` restarts a running menu bar when the build changed; after an upgrade made some other way, the next `cswap menubar` notices the old build and restarts it.

## Environment

The service follows the `CLAUDE_CONFIG_DIR`, `CLAUDE_SECURESTORAGE_CONFIG_DIR` and `XDG_DATA_HOME` of the shell that set it up, and `--install-service` from a shell in another profile says what moved. It does not receive a `CODEX_HOME` that only the shell has, so the menu bar reads the default `~/.codex` for [Codex usage](#codex-usage) unless `CODEX_HOME` is set for launchd.

## Login Items & Extensions

If you switched it off under System Settings → General → Login Items & Extensions → Allow in the Background, `cswap menubar` still starts it when you ask, but says that macOS will not start it at login until you turn that back on.
