# source from ~/.bashrc: `cswap switch ...` writes a handoff, switches, opens claude
cswap() {
  if [ "${1:-}" = switch ] && [[ "${2:-}" != -* ]]; then
    shift
    ~/.claude/switch-with-handoff.sh "$@"
  else
    command cswap "$@"
  fi
}
