# Hand off a session when switching accounts

Requires `jq`.

1. Install the skill: `mkdir -p ~/.claude/skills/handoff && cp SKILL.md ~/.claude/skills/handoff/`
2. Install the hook: `cp session-start.sh ~/.claude/` and add to `~/.claude/settings.json`:

   ```json
   {
     "hooks": {
       "SessionStart": [
         {
           "matcher": "startup|clear",
           "hooks": [{ "type": "command", "command": "~/.claude/session-start.sh" }]
         }
       ]
     }
   }
   ```
3. Run `/handoff`, then `cswap switch`, then start `claude` in the same directory.
   Or run `switch-with-handoff.sh` to do all three.
4. To make a plain `cswap switch <n>` do all three, copy `cswap-wrapper.sh` and
   `switch-with-handoff.sh` to `~/.claude/` and add
   `. ~/.claude/cswap-wrapper.sh` to your `~/.bashrc` (or `.zshrc`). Other
   subcommands, `cswap switch --help` and `cswap auto` are unaffected.

Notes are stored per directory in `${XDG_STATE_HOME:-~/.local/state}/claude-handoff/`, used once (renamed to
`.used`), and ignored after 24 hours.
