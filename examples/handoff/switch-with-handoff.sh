#!/usr/bin/env bash
# Write a handoff note with the current account, switch, then start a fresh
# session that picks the note up through session-start.sh.
# Usage: switch-with-handoff.sh [cswap switch args...]
set -euo pipefail

dir="${XDG_STATE_HOME:-$HOME/.local/state}/claude-handoff"
mkdir -p "$dir"
note="$dir/$(pwd | tr / -).md"

claude -p "/handoff $note" --continue --permission-mode acceptEdits --add-dir "$dir" </dev/null \
  || echo "handoff skipped (no session to hand off)" >&2
cswap switch "$@"
exec claude
