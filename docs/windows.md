# Windows: a pane running cswap shows Temp as its folder

On Windows, a terminal pane running `cswap run` can be reported as being in `%TEMP%` rather than the folder you started it in. Terminals and multiplexers that read a pane's folder from the program running in it then label the pane "Temp", and new tabs or splits opened from it start in `%TEMP%`. Claude Code itself runs in the right folder; only the label and new tabs are affected.

## Why it happens

`cswap.exe` (and `claude-swap.exe`) are small launchers that uv and pip create for every Python tool. After a launcher starts cswap's Python, it moves its own working folder to the temp folder so it does not keep your folder open, then waits until the session ends. That is deliberate and comes from the launcher, not from cswap: see the `set_current_dir(std::env::temp_dir())` call in uv's [`crates/uv-trampoline/src/bounce.rs`](https://github.com/astral-sh/uv/blob/main/crates/uv-trampoline/src/bounce.rs), which mirrors distlib's `switch_working_directory`.

Because the launcher stays running for the whole `cswap run` session, anything that asks which folder the pane is in sees `%TEMP%`.

## Workaround: run cswap's Python directly

Skip the launcher by running `python -m claude_swap` from cswap's own environment. The commands below assume a `uv tool install` in the default location, `%APPDATA%\uv\tools\claude-swap`. If you set `UV_TOOL_DIR`, `uv tool dir` prints the folder to use instead. For pipx, the environment is in the folder that `pipx environment --value PIPX_LOCAL_VENVS` prints.

**PowerShell**, in your profile (`notepad $PROFILE`):

```powershell
function Invoke-ClaudeSwap {
    $py = Join-Path $env:APPDATA 'uv\tools\claude-swap\Scripts\python.exe'
    if (Test-Path $py) { & $py -m claude_swap @args }
    else { & (Join-Path $HOME '.local\bin\cswap.exe') @args }
}
Set-Alias -Name cswap -Value Invoke-ClaudeSwap
Set-Alias -Name claude-swap -Value Invoke-ClaudeSwap
```

**Git Bash**, in `~/.bashrc`:

```bash
_claude_swap() {
    local py
    py="$(cygpath -u "$APPDATA")/uv/tools/claude-swap/Scripts/python.exe"
    if [ -x "$py" ]; then "$py" -m claude_swap "$@"; else "$HOME/.local/bin/cswap.exe" "$@"; fi
}
cswap() { _claude_swap "$@"; }
claude-swap() { _claude_swap "$@"; }
```

**cmd**, as doskey macros. These only apply to commands typed at the prompt, not to `.bat` files:

```bat
doskey cswap="%APPDATA%\uv\tools\claude-swap\Scripts\python.exe" -m claude_swap $*
doskey claude-swap="%APPDATA%\uv\tools\claude-swap\Scripts\python.exe" -m claude_swap $*
```

The PowerShell and Git Bash versions fall back to the launcher if the environment is missing, for example after cswap is reinstalled somewhere else.

These wrappers only take effect when a shell runs `cswap`. If your terminal or multiplexer starts `cswap` directly as a pane's command, with no shell in between, point that command at the Python instead, for example `"%APPDATA%\uv\tools\claude-swap\Scripts\python.exe" -m claude_swap run 2`.

The same launcher behaviour applies to every Python tool installed by uv or pip. It only shows up with long-running commands such as `cswap run`.
