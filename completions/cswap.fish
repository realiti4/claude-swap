# Fish completions for cswap (claude-swap) — https://github.com/realiti4/claude-swap
#
# Install: save as ~/.config/fish/completions/cswap.fish (auto-loaded by fish).

# --- helpers -----------------------------------------------------------

# True when no subcommand has been typed yet.
function __cswap_no_subcommand
    set -l cmd (commandline -opc)
    test (count $cmd) -eq 1
end

# True when $argv[1] is the subcommand currently being completed.
function __cswap_using_subcommand
    set -l cmd (commandline -opc)
    test (count $cmd) -gt 1
    and test "$cmd[2]" = "$argv[1]"
end

# Candidates for NUM|EMAIL positions: "1", "2", ... and each account's email,
# both annotated with the org name / active marker. Falls back to nothing
# (plain no-op) if cswap isn't configured yet or python3 is unavailable.
function __cswap_accounts
    cswap list --json 2>/dev/null | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for a in data.get("accounts", []):
    num = a.get("number")
    email = a.get("email", "")
    org = a.get("organizationName", "")
    tag = " (active)" if a.get("active") else ""
    desc = f"{org}{tag}".strip()
    if num is not None:
        print(f"{num}\t{email or desc}")
    if email:
        print(f"{email}\t{desc}" if desc else email)
' 2>/dev/null
end

# --- top-level subcommands ---------------------------------------------

complete -c cswap -n __cswap_no_subcommand -f -a help -d 'show this help'
complete -c cswap -n __cswap_no_subcommand -f -a list -d 'list managed accounts'
complete -c cswap -n __cswap_no_subcommand -f -a ls -d 'alias for list'
complete -c cswap -n __cswap_no_subcommand -f -a status -d 'show current account'
complete -c cswap -n __cswap_no_subcommand -f -a switch -d 'switch account (rotate, or to <num|email>)'
complete -c cswap -n __cswap_no_subcommand -f -a add -d 'add the current account'
complete -c cswap -n __cswap_no_subcommand -f -a add-token -d 'register a setup-token or API key'
complete -c cswap -n __cswap_no_subcommand -f -a remove -d 'remove an account'
complete -c cswap -n __cswap_no_subcommand -f -a rm -d 'alias for remove'
complete -c cswap -n __cswap_no_subcommand -f -a disable -d 'hold an account out of auto-rotation'
complete -c cswap -n __cswap_no_subcommand -f -a enable -d 'return a disabled account to rotation'
complete -c cswap -n __cswap_no_subcommand -f -a run -d 'run as an account, this terminal only'
complete -c cswap -n __cswap_no_subcommand -f -a map -d 'map a directory to an account'
complete -c cswap -n __cswap_no_subcommand -f -a unmap -d 'remove a directory mapping'
complete -c cswap -n __cswap_no_subcommand -f -a alias -d 'set/remove/list an account alias'
complete -c cswap -n __cswap_no_subcommand -f -a swap -d "exchange two accounts' slot numbers"
complete -c cswap -n __cswap_no_subcommand -f -a move -d 'assign an account to a slot'
complete -c cswap -n __cswap_no_subcommand -f -a auto -d 'auto-switch when nearing rate limits'
complete -c cswap -n __cswap_no_subcommand -f -a config -d 'show or change settings'
complete -c cswap -n __cswap_no_subcommand -f -a unclaimed -d 'list or drop stashed credential entries'
complete -c cswap -n __cswap_no_subcommand -f -a export -d 'export accounts'
complete -c cswap -n __cswap_no_subcommand -f -a import -d 'import accounts'
complete -c cswap -n __cswap_no_subcommand -f -a tui -d 'interactive dashboard'
complete -c cswap -n __cswap_no_subcommand -f -a watch -d 'dashboard on the live watch page'
complete -c cswap -n __cswap_no_subcommand -f -a menubar -d 'macOS menu bar app'
complete -c cswap -n __cswap_no_subcommand -f -a upgrade -d 'self-upgrade to latest'
complete -c cswap -n __cswap_no_subcommand -f -a update -d 'alias for upgrade'
complete -c cswap -n __cswap_no_subcommand -f -a purge -d 'remove all claude-swap data'

complete -c cswap -n __cswap_no_subcommand -l version -d "show program's version number"
complete -c cswap -n __cswap_no_subcommand -l help -s h -d 'show this help'

# --debug is accepted (and useful) after every subcommand.
complete -c cswap -l debug -f -d 'enable debug logging'

# --- switch --------------------------------------------------------------

complete -c cswap -n '__cswap_using_subcommand switch' -f -a '(__cswap_accounts)'
complete -c cswap -n '__cswap_using_subcommand switch' -l strategy -f -a 'best next-available' -d 'target-selection strategy'
complete -c cswap -n '__cswap_using_subcommand switch' -l model -f -d 'also compare these models\' weekly limits'
complete -c cswap -n '__cswap_using_subcommand switch' -l json -f -d 'emit machine-readable JSON'
complete -c cswap -n '__cswap_using_subcommand switch' -l force -f -d "don't back up the current login first"

# --- list / ls -------------------------------------------------------------

for c in list ls
    complete -c cswap -n "__cswap_using_subcommand $c" -l json -f -d 'emit machine-readable JSON'
    complete -c cswap -n "__cswap_using_subcommand $c" -l token-status -f -d 'show OAuth token diagnostics'
end

# --- status ----------------------------------------------------------------

complete -c cswap -n '__cswap_using_subcommand status' -l json -f -d 'emit machine-readable JSON'

# --- add / add-token ---------------------------------------------------

complete -c cswap -n '__cswap_using_subcommand add' -l slot -x -d 'slot number to add into'
complete -c cswap -n '__cswap_using_subcommand add' -l alias -x -d 'short display alias for the account'

complete -c cswap -n '__cswap_using_subcommand add-token' -f -a '-' -d 'read the token from stdin'
complete -c cswap -n '__cswap_using_subcommand add-token' -l slot -x -d 'slot number to add into'
complete -c cswap -n '__cswap_using_subcommand add-token' -l email -x -d 'email to label the account with'

# --- remove / rm / disable / enable -----------------------------------

for c in remove rm disable enable
    complete -c cswap -n "__cswap_using_subcommand $c" -f -a '(__cswap_accounts)'
end

# --- run ---------------------------------------------------------------

complete -c cswap -n '__cswap_using_subcommand run' -f -a '(__cswap_accounts)'
complete -c cswap -n '__cswap_using_subcommand run' -l no-share -f -d "don't share ~/.claude settings into the session"
complete -c cswap -n '__cswap_using_subcommand run' -l share-history -f -d 'share conversation history across accounts'
complete -c cswap -n '__cswap_using_subcommand run' -l no-share-history -f -d 'per-account history (default)'

# --- map / unmap ---------------------------------------------------------

complete -c cswap -n '__cswap_using_subcommand map' -f -a '(__cswap_accounts)'
complete -c cswap -n '__cswap_using_subcommand map' -f -a '(__fish_complete_directories)'
complete -c cswap -n '__cswap_using_subcommand unmap' -f -a '(__fish_complete_directories)'

# --- alias ---------------------------------------------------------------

complete -c cswap -n '__cswap_using_subcommand alias' -f -a '(__cswap_accounts)'
complete -c cswap -n '__cswap_using_subcommand alias' -l unset -f -d "remove the account's alias"

# --- swap / move ---------------------------------------------------------

complete -c cswap -n '__cswap_using_subcommand swap' -f -a '(__cswap_accounts)'
complete -c cswap -n '__cswap_using_subcommand move' -f -a '(__cswap_accounts)'

# --- auto ------------------------------------------------------------------

complete -c cswap -n '__cswap_using_subcommand auto' -l once -f -d 'evaluate once and exit'
complete -c cswap -n '__cswap_using_subcommand auto' -l json -f -d 'one JSON event per line'
complete -c cswap -n '__cswap_using_subcommand auto' -l interval -x -d 'poll interval in seconds (default 60)'
complete -c cswap -n '__cswap_using_subcommand auto' -l threshold -x -d 'switch above this % used (default 90)'
complete -c cswap -n '__cswap_using_subcommand auto' -l cooldown -x -d 'min seconds between switches (default 300)'
complete -c cswap -n '__cswap_using_subcommand auto' -l model -x -d 'also switch on these models\' weekly limits'
complete -c cswap -n '__cswap_using_subcommand auto' -l include-api-key-accounts -f -d 'allow switching onto API-key accounts'
complete -c cswap -n '__cswap_using_subcommand auto' -l no-include-api-key-accounts -f -d 'exclude API-key accounts (default)'
complete -c cswap -n '__cswap_using_subcommand auto' -l strategy -f -a 'best consume-first' -d 'target-selection strategy'
complete -c cswap -n '__cswap_using_subcommand auto' -l dry-run -f -d 'report but never switch'

# --- config ------------------------------------------------------------

set -l cswap_config_keys autoswitch.threshold autoswitch.intervalSeconds \
    autoswitch.cooldownSeconds autoswitch.hysteresisPct autoswitch.strategy \
    autoswitch.includeApiKeyAccounts autoswitch.unhealthyTicks autoswitch.model \
    ui.theme

function __cswap_using_config_action
    set -l cmd (commandline -opc)
    test (count $cmd) -gt 2
    and test "$cmd[2]" = config
    and test "$cmd[3]" = "$argv[1]"
end

function __cswap_config_no_action
    set -l cmd (commandline -opc)
    test (count $cmd) -eq 2
    and test "$cmd[2]" = config
end

complete -c cswap -n __cswap_config_no_action -f -a list -d 'show all effective settings (default)'
complete -c cswap -n __cswap_config_no_action -f -a get -d "print one setting's effective value"
complete -c cswap -n __cswap_config_no_action -f -a set -d 'validate and persist one setting'
complete -c cswap -n __cswap_config_no_action -f -a unset -d 'remove one setting (revert to default)'
complete -c cswap -n __cswap_config_no_action -f -a path -d 'print the settings.json location'
complete -c cswap -n '__cswap_using_subcommand config' -l json -f -d 'emit machine-readable JSON'

for action in get set unset
    complete -c cswap -n "__cswap_using_config_action $action" -f -a "$cswap_config_keys"
end

# --- unclaimed -----------------------------------------------------------

complete -c cswap -n '__cswap_using_subcommand unclaimed' -l purge -x -d 'delete this stashed entry by id'

# --- export / import -------------------------------------------------

complete -c cswap -n '__cswap_using_subcommand export' -l account -x -a '(__cswap_accounts)' -d 'limit export to one account'
complete -c cswap -n '__cswap_using_subcommand export' -l full -f -d 'include full ~/.claude.json'
complete -c cswap -n '__cswap_using_subcommand import' -l force -f -d 'overwrite existing accounts'
