#!/usr/bin/env bash
# Move every Claude Code session running in tmux to another model.
#
# An `autoswitch.onModelChange` command for `cswap auto --fallback-model`:
# cswap swaps credentials, but only a `/model` typed into a session changes
# the model it runs on. This types it, into every tmux pane running `claude`.
#
#   cswap config set autoswitch.onModelChange /path/to/tmux-model-change.sh
#
# Reads CSWAP_MODEL_EVENT (fallback|restored), set by cswap. Tunables, all
# optional, from the environment:
#
#   CSWAP_TMUX_PRIMARY_ID    /model argument on "restored"   (default: fable)
#   CSWAP_TMUX_FALLBACK_ID   /model argument on "fallback"   (default: opus)
#   CSWAP_TMUX_NUDGE         message sent to a session that stalled on the
#                            limit before the fallback engaged; empty = never
#   CSWAP_TMUX_STALL_REGEX   what such a stalled session shows near its prompt
#   CSWAP_TMUX_TARGETS       only these pane ids (space-separated, e.g. "%3")
#   CSWAP_TMUX_RETRY_S       how long to keep retrying deferred panes (43200)
#   CSWAP_TMUX_TICK_S        pause between looks at a pane after typing (1)
#   CSWAP_TMUX_DRY_RUN=1     log what would be sent, send nothing
#
# A pane is DEFERRED, never typed over, while its prompt holds text someone is
# writing. Deferred panes are retried in the background every 30s; a later
# model change supersedes the retry loop. Sessions started with an explicit
# `--model` are left alone — someone pinned them on purpose.
#
# Bash 3.2 compatible (stock macOS). Log: $STATE_DIR/tmux-model-change.log

set -u

EVENT="${CSWAP_MODEL_EVENT:-}"
PRIMARY_ID="${CSWAP_TMUX_PRIMARY_ID:-fable}"
FALLBACK_ID="${CSWAP_TMUX_FALLBACK_ID:-opus}"
NUDGE="${CSWAP_TMUX_NUDGE-You hit a limit and you have recovered, continue}"
STALL_REGEX="${CSWAP_TMUX_STALL_REGEX:-limit reached|hit your [a-zA-Z ]*limit|usage limit}"
RETRY_S="${CSWAP_TMUX_RETRY_S:-43200}"
TICK_S="${CSWAP_TMUX_TICK_S:-1}"
DRY_RUN="${CSWAP_TMUX_DRY_RUN:-}"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/cswap-tmux"
LOG="$STATE_DIR/tmux-model-change.log"
PIDFILE="$STATE_DIR/retry.pid"

case "$EVENT" in
  fallback) MODEL_ID="$FALLBACK_ID" ;;
  restored) MODEL_ID="$PRIMARY_ID" ;;
  *) echo "CSWAP_MODEL_EVENT must be 'fallback' or 'restored' (got '$EVENT')" >&2; exit 2 ;;
esac

command -v tmux >/dev/null 2>&1 || { echo "tmux not found" >&2; exit 1; }
mkdir -p "$STATE_DIR" || exit 1

log() { printf '%s [%s] %s\n' "$(date '+%Y-%m-%dT%H:%M:%S')" "$EVENT" "$*" >>"$LOG"; }

# Pane ids (%N) whose foreground command is claude.
claude_panes() {
  tmux list-panes -a -F '#{pane_id} #{pane_pid} #{pane_current_command}' 2>/dev/null |
    while read -r id pid cmd; do
      [ "$cmd" = "claude" ] || continue
      if [ -n "${CSWAP_TMUX_TARGETS:-}" ]; then
        case " $CSWAP_TMUX_TARGETS " in *" $id "*) ;; *) continue ;; esac
      fi
      echo "$id $pid"
    done
}

# Started with an explicit --model? (claude is the pane shell's child, or the
# pane process itself when tmux launched it directly.)
is_pinned() {
  local pid="$1" pids
  pids="$pid $(pgrep -P "$pid" 2>/dev/null | tr '\n' ' ')"
  # shellcheck disable=SC2086
  ps -o args= -p $pids 2>/dev/null | grep -Eq '(^|/)claude( .*)? --model[ =]'
}

# The text someone typed into the pane's prompt: the last line carrying the
# prompt glyph, minus the glyph and surrounding blanks (incl. the NBSP Claude
# Code pads it with). DIM text is dropped first — that is Claude Code's own
# suggestion/placeholder, which typing replaces, not input anyone would lose.
# Prints nothing for an empty prompt; returns 1 when no prompt is on screen at
# all (a dialog or picker owns the pane).
prompt_text() {
  local line esc=$'\x1b'
  line="$(tmux capture-pane -p -e -t "$1" 2>/dev/null | grep -E '❯' | tail -n 1)"
  [ -n "$line" ] || return 1
  printf '%s' "$line" | sed -E \
    -e "s/${esc}\\[2m[^${esc}]*//g" \
    -e "s/${esc}\\[[0-9;]*[a-zA-Z]//g" \
    -e $'s/^.*❯//; s/\xc2\xa0/ /g; s/^[[:space:]]+//; s/[[:space:]]+$//'
}

# The last N non-blank lines on screen. Blank rows are dropped first: the UI
# is anchored to the top of a fresh pane, and a dialog leaves rows empty below
# it, so a plain `tail` can return nothing but padding.
screen_tail() { tmux capture-pane -p -t "$1" 2>/dev/null | grep -v '^[[:space:]]*$' | tail -n "$2"; }

# Mid-turn: the spinner line ("Thinking… (13s · ↓ 121 tokens)"), or the older
# "esc to interrupt" hint.
is_busy() { screen_tail "$1" 12 | grep -Eq '…[[:space:]]*\([0-9]+m?[[:space:]]?[0-9]*s|esc to interrupt'; }

is_stalled() { screen_tail "$1" 25 | grep -Eiq "$STALL_REGEX"; }

send_line() {
  if [ -n "$DRY_RUN" ]; then log "dry-run $1 <- $2"; return 0; fi
  tmux send-keys -t "$1" -l -- "$2" && sleep 0.3 && tmux send-keys -t "$1" Enter
}

# A session with history asks "Switch model?" (the prompt cache is per model)
# and waits — forever, unattended. Answer it, but only when "Yes" is the
# option the cursor is actually on; then check the switch was acknowledged.
confirm_switch() {
  local id="$1" screen i
  for i in 1 2 3 4 5; do
    sleep "$TICK_S"
    screen="$(screen_tail "$id" 20)"
    if echo "$screen" | grep -q 'Switch model?'; then
      if echo "$screen" | grep -Eq '❯[[:space:]]*1\. Yes'; then
        tmux send-keys -t "$id" Enter
        log "confirmed $id: Switch model? -> Yes"
      else
        log "WARNING $id: 'Switch model?' is open but Yes is not selected; left alone"
        return 0
      fi
    elif echo "$screen" | grep -q 'Set model to'; then
      log "ok $id: $(echo "$screen" | grep 'Set model to' | tail -n 1 | sed -E 's/^[^S]*//')"
      return 0
    fi
  done
  log "WARNING $id: no 'Set model to' acknowledgement seen"
}

# 0 = done (or deliberately skipped), 1 = deferred, try again later.
handle_pane() {
  local id="$1" pid="$2" text
  if is_pinned "$pid"; then log "skip $id: started with --model"; return 0; fi
  if ! text="$(prompt_text "$id")"; then log "defer $id: no prompt on screen"; return 1; fi
  if [ -n "$text" ]; then log "defer $id: prompt holds text"; return 1; fi
  # Read BEFORE /model repaints the pane.
  local stalled=""
  if [ "$EVENT" = "fallback" ] && [ -n "$NUDGE" ] && ! is_busy "$id" && is_stalled "$id"; then
    stalled=1
  fi
  send_line "$id" "/model $MODEL_ID" || { log "defer $id: send-keys failed"; return 1; }
  log "sent $id: /model $MODEL_ID"
  [ -n "$DRY_RUN" ] || confirm_switch "$id"
  if [ -n "$stalled" ]; then
    sleep "$TICK_S"; sleep "$TICK_S"
    send_line "$id" "$NUDGE" && log "nudged $id"
  fi
  return 0
}

# One pass over the given "id pid" lines; prints the ones still deferred.
pass() {
  while read -r id pid; do
    [ -n "$id" ] || continue
    handle_pane "$id" "$pid" || echo "$id $pid"
  done
}

# A newer model change supersedes whatever an older retry loop still owes.
if [ -f "$PIDFILE" ]; then
  old="$(cat "$PIDFILE" 2>/dev/null)"
  [ -n "$old" ] && kill "$old" 2>/dev/null
  rm -f "$PIDFILE"
fi

deferred="$(claude_panes | pass)"
[ -n "$deferred" ] || { log "all panes handled"; exit 0; }

log "retrying in background: $(echo "$deferred" | awk '{print $1}' | tr '\n' ' ')"
(
  deadline=$(( $(date +%s) + RETRY_S ))
  while [ -n "$deferred" ] && [ "$(date +%s)" -lt "$deadline" ]; do
    sleep 30
    # Panes that closed meanwhile drop out: only retry ones that still exist.
    live="$(claude_panes)"
    deferred="$(echo "$deferred" | while read -r id pid; do
      echo "$live" | grep -q "^$id " && echo "$id $pid"
    done | pass)"
  done
  [ -z "$deferred" ] && log "all deferred panes handled" || log "gave up on: $deferred"
  rm -f "$PIDFILE"
) </dev/null >/dev/null 2>&1 &
echo $! >"$PIDFILE"
disown 2>/dev/null || true
exit 0
