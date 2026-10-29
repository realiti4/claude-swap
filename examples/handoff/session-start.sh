#!/usr/bin/env bash
# SessionStart hook: inject the note written by /handoff into a fresh session,
# once. Needs jq. Resumed sessions are skipped (matcher: startup|clear).
set -euo pipefail

cwd=$(jq -r '.cwd // empty')
[ -n "$cwd" ] || exit 0

note="${XDG_STATE_HOME:-$HOME/.local/state}/claude-handoff/$(printf '%s' "$cwd" | tr / -).md"
[ -f "$note" ] || exit 0

# Ignore stale notes (older than 24h) so an old handoff never leaks in.
[ -n "$(find "$note" -mmin -1440 2>/dev/null)" ] || exit 0

jq -n --rawfile note "$note" '{
  hookSpecificOutput: {
    hookEventName: "SessionStart",
    additionalContext: ("Handoff from the previous session (continue from here):\n\n" + $note)
  }
}'
mv "$note" "$note.used"
