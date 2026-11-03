# Fish completions for cswap (claude-swap).
#
# Install: copy to ~/.config/fish/completions/cswap.fish (fish auto-loads it).

# --- account candidates -------------------------------------------------
#
# Read straight from the local sequence.json rather than shelling out to
# `cswap list --json`. `list` is an on-demand usage caller: entries older than
# SERVE_TTL_S (180s) are refetched over HTTPS with a 5s per-account timeout,
# so driving completions from it would freeze the prompt for seconds on every
# Tab once the cache went stale. sequence.json holds the number, email, and
# alias — everything a completion needs — and costs one local read.

function __cswap_sequence_file --description 'Path to cswap sequence.json, if present'
    # Linux/WSL use the XDG data dir; macOS and the legacy layout use ~/.claude-swap-backup.
    if set -q XDG_DATA_HOME; and string match -q -- '/*' "$XDG_DATA_HOME"
        set -l xdg "$XDG_DATA_HOME/claude-swap/sequence.json"
        if test -r $xdg
            echo $xdg
            return 0
        end
    end
    for candidate in "$HOME/.local/share/claude-swap/sequence.json" "$HOME/.claude-swap-backup/sequence.json"
        if test -r $candidate
            echo $candidate
            return 0
        end
    end
    return 1
end

function __cswap_accounts --description 'Account numbers, emails and aliases with descriptions'
    set -l file (__cswap_sequence_file); or return 0
    awk '
        function value(   rest) {
            rest = substr($0, index($0, ":") + 1)
            if (match(rest, /"[^"]*"/)) return substr(rest, RSTART + 1, RLENGTH - 2)
            return ""
        }
        function flush(   tag, label) {
            if (num == "") return
            tag = (num == active) ? " (active)" : ""
            label = (email == "") ? "account " num : email
            if (alias != "") label = label " [" alias "]"
            print num "\t" label tag
            if (email != "") print email "\t#" num tag
            if (alias != "") print alias "\t#" num " " email tag
            num = ""; email = ""; alias = ""
        }
        /"activeAccountNumber"[ \t]*:/ {
            if (match($0, /[0-9]+/)) active = substr($0, RSTART, RLENGTH)
            next
        }
        /^[ \t]*"[0-9]+"[ \t]*:[ \t]*\{/ {
            flush()
            match($0, /"[0-9]+"/)
            num = substr($0, RSTART + 1, RLENGTH - 2)
            next
        }
        num != "" && /"email"[ \t]*:/ { email = value(); next }
        num != "" && /"alias"[ \t]*:/ { alias = value(); next }
        END { flush() }
    ' $file 2>/dev/null
    # A corrupt or unreadable file yields no candidates, never an error.
    return 0
end

# --- context helpers ----------------------------------------------------

# The CLI dispatches on argv[0], so the subcommand is always token 2 and
# global flags have to follow it (`cswap list --json`, not `cswap --json list`).
function __cswap_no_subcommand --description 'No subcommand typed yet'
    test (count (commandline -opc)) -eq 1
end

function __cswap_subcommand --description 'Current subcommand is $argv[1]'
    set -l cmd (commandline -opc)
    test (count $cmd) -gt 1; and contains -- "$cmd[2]" $argv
end

# Positional args already given after the subcommand. Only used for
# subcommands whose flags are all boolean (--debug, --unset), so a value-taking
# flag can never be miscounted as a positional here.
function __cswap_positionals --description 'Count of positional args after the subcommand'
    set -l cmd (commandline -opc)
    set -l n 0
    if test (count $cmd) -ge 3
        for token in $cmd[3..-1]
            string match -q -- '-*' $token; or set n (math $n + 1)
        end
    end
    echo $n
end

function __cswap_positional_is --description 'Next positional slot equals $argv[1]'
    test (__cswap_positionals) -eq (math $argv[1] - 1)
end

# --- subcommands --------------------------------------------------------

complete -c cswap -n __cswap_no_subcommand -f -a help -d 'Show help'
complete -c cswap -n __cswap_no_subcommand -f -a list -d 'List managed accounts'
complete -c cswap -n __cswap_no_subcommand -f -a ls -d 'List managed accounts'
complete -c cswap -n __cswap_no_subcommand -f -a status -d 'Show the current account'
complete -c cswap -n __cswap_no_subcommand -f -a switch -d 'Rotate, or switch to an account'
complete -c cswap -n __cswap_no_subcommand -f -a add -d 'Add the currently logged-in account'
complete -c cswap -n __cswap_no_subcommand -f -a add-token -d 'Register a setup-token or API key'
complete -c cswap -n __cswap_no_subcommand -f -a remove -d 'Remove an account'
complete -c cswap -n __cswap_no_subcommand -f -a rm -d 'Remove an account'
complete -c cswap -n __cswap_no_subcommand -f -a disable -d 'Hold an account out of auto-rotation'
complete -c cswap -n __cswap_no_subcommand -f -a enable -d 'Return a disabled account to rotation'
complete -c cswap -n __cswap_no_subcommand -f -a run -d 'Run Claude as an account, this terminal only'
complete -c cswap -n __cswap_no_subcommand -f -a map -d 'Map a directory to an account'
complete -c cswap -n __cswap_no_subcommand -f -a unmap -d 'Remove a directory mapping'
complete -c cswap -n __cswap_no_subcommand -f -a alias -d "Set, remove or list an account's alias"
complete -c cswap -n __cswap_no_subcommand -f -a swap -d "Exchange two accounts' slot numbers"
complete -c cswap -n __cswap_no_subcommand -f -a move -d 'Assign an account to a slot'
complete -c cswap -n __cswap_no_subcommand -f -a auto -d 'Auto-switch when nearing rate limits'
complete -c cswap -n __cswap_no_subcommand -f -a config -d 'Show or change settings'
complete -c cswap -n __cswap_no_subcommand -f -a unclaimed -d 'List or drop stashed credential entries'
complete -c cswap -n __cswap_no_subcommand -f -a export -d 'Export accounts to a file'
complete -c cswap -n __cswap_no_subcommand -f -a import -d 'Import accounts from a file'
complete -c cswap -n __cswap_no_subcommand -f -a tui -d 'Interactive dashboard'
complete -c cswap -n __cswap_no_subcommand -f -a watch -d 'Dashboard, on the live watch page'
complete -c cswap -n __cswap_no_subcommand -f -a menubar -d 'macOS menu bar app'
complete -c cswap -n __cswap_no_subcommand -f -a upgrade -d 'Self-upgrade to the latest version'
complete -c cswap -n __cswap_no_subcommand -f -a update -d 'Self-upgrade to the latest version'
complete -c cswap -n __cswap_no_subcommand -f -a purge -d 'Remove all claude-swap data'

complete -c cswap -n __cswap_no_subcommand -f -l version -d "Show the program's version"

# Accepted alongside every subcommand. Deliberately not -f: an unconditional
# -f would switch the whole command to no-file mode and break the path
# completion that `export`/`import` need.
complete -c cswap -s h -l help -d 'Show help'
complete -c cswap -l debug -d 'Enable debug logging'

# A path is only ever an argument to export/import (a file) or map/unmap (a
# directory, completed below). Everywhere else fish's default file list is
# noise — `cswap alias 2 <TAB>` wants a new name, not a filename.
complete -c cswap -n 'not __cswap_subcommand export import map unmap' -f

# --- switch -------------------------------------------------------------

complete -c cswap -n '__cswap_subcommand switch' -f -a '(__cswap_accounts)'
# -x, not -f: --strategy takes a value, so its candidates belong to the next token.
complete -c cswap -n '__cswap_subcommand switch' -x -l strategy -a best -d 'Jump to the account with the most quota left'
complete -c cswap -n '__cswap_subcommand switch' -x -l strategy -a next-available -d 'Rotate, skipping rate-limited accounts'
complete -c cswap -n '__cswap_subcommand switch' -x -l model -a 'Fable Opus Sonnet Haiku all' -d "Also compare these models' weekly limits"
complete -c cswap -n '__cswap_subcommand switch' -f -l json -d 'Emit machine-readable JSON'
complete -c cswap -n '__cswap_subcommand switch' -f -l force -d "Activate without backing up the current login"

# --- list / status ------------------------------------------------------

complete -c cswap -n '__cswap_subcommand list ls' -f -l json -d 'Emit machine-readable JSON'
complete -c cswap -n '__cswap_subcommand list ls' -f -l token-status -d 'Show OAuth token diagnostics'
complete -c cswap -n '__cswap_subcommand status' -f -l json -d 'Emit machine-readable JSON'

# --- add / add-token ----------------------------------------------------

complete -c cswap -n '__cswap_subcommand add' -x -l slot -d 'Slot number to add into'
complete -c cswap -n '__cswap_subcommand add' -x -l alias -d 'Short display alias for the account'

complete -c cswap -n '__cswap_subcommand add-token' -f -a '-' -d 'Read the token from stdin'
complete -c cswap -n '__cswap_subcommand add-token' -x -l slot -d 'Slot number to add into'
complete -c cswap -n '__cswap_subcommand add-token' -x -l email -d 'Email to label the account with'

# --- account-target subcommands ----------------------------------------

complete -c cswap -n '__cswap_subcommand remove rm disable enable' -f -a '(__cswap_accounts)'

# --- run ----------------------------------------------------------------

complete -c cswap -n '__cswap_subcommand run; and __cswap_positional_is 1' -f -a '(__cswap_accounts)'
complete -c cswap -n '__cswap_subcommand run' -f -l no-share -d "Don't share ~/.claude settings into the session"
complete -c cswap -n '__cswap_subcommand run' -f -l share-history -d 'Share conversation history across accounts'
complete -c cswap -n '__cswap_subcommand run' -f -l no-share-history -d 'Keep per-account history (default)'

# --- map / unmap --------------------------------------------------------
#
# `map <NUM|EMAIL> [PATH]`: account first, directory second.

complete -c cswap -n '__cswap_subcommand map; and __cswap_positional_is 1' -f -a '(__cswap_accounts)'
complete -c cswap -n '__cswap_subcommand map; and __cswap_positional_is 2' -f -a '(__fish_complete_directories)'
complete -c cswap -n '__cswap_subcommand unmap; and __cswap_positional_is 1' -f -a '(__fish_complete_directories)'

# --- alias --------------------------------------------------------------
#
# `alias <NUM|EMAIL> <NAME>`: the second positional is a new name, so it has
# no candidates — offering accounts there would be actively misleading.

complete -c cswap -n '__cswap_subcommand alias; and __cswap_positional_is 1' -f -a '(__cswap_accounts)'
complete -c cswap -n '__cswap_subcommand alias' -f -l unset -d "Remove the account's alias"

# --- swap / move --------------------------------------------------------

complete -c cswap -n '__cswap_subcommand swap; and __cswap_positional_is 1' -f -a '(__cswap_accounts)'
complete -c cswap -n '__cswap_subcommand swap; and __cswap_positional_is 2' -f -a '(__cswap_accounts)'
# `move <NUM|EMAIL|ALIAS> <SLOT>`: only slot numbers make sense second.
complete -c cswap -n '__cswap_subcommand move; and __cswap_positional_is 1' -f -a '(__cswap_accounts)'
complete -c cswap -n '__cswap_subcommand move; and __cswap_positional_is 2' -f -a '(__cswap_accounts | string match -r "^[0-9]+\t.*")' -d 'Destination slot'

# --- auto ---------------------------------------------------------------

complete -c cswap -n '__cswap_subcommand auto' -f -l once -d 'Evaluate once and exit'
complete -c cswap -n '__cswap_subcommand auto' -f -l json -d 'One JSON event per line'
complete -c cswap -n '__cswap_subcommand auto' -x -l interval -d 'Poll interval, seconds (15-3600, default 60)'
complete -c cswap -n '__cswap_subcommand auto' -x -l threshold -d 'Switch above this pct used (50-99.9, default 90)'
complete -c cswap -n '__cswap_subcommand auto' -x -l cooldown -d 'Min seconds between switches (default 300)'
complete -c cswap -n '__cswap_subcommand auto' -x -l model -a 'Fable Opus Sonnet Haiku all' -d "Also switch on these models' weekly limits"
complete -c cswap -n '__cswap_subcommand auto' -f -l include-api-key-accounts -d 'Allow rotating onto API-key accounts'
complete -c cswap -n '__cswap_subcommand auto' -f -l no-include-api-key-accounts -d 'Exclude API-key accounts (default)'
complete -c cswap -n '__cswap_subcommand auto' -x -l strategy -a best -d 'Account with the most quota left (default)'
complete -c cswap -n '__cswap_subcommand auto' -x -l strategy -a consume-first -d 'Account whose weekly window resets soonest'
complete -c cswap -n '__cswap_subcommand auto' -f -l dry-run -d 'Report decisions but never switch'

# --- config -------------------------------------------------------------

# Only at the KEY slot (`config get <here>`); once a key is typed the value
# candidates take over, so the key list must stop firing.
function __cswap_config_key --description 'Completing the KEY of config get/set/unset'
    set -l cmd (commandline -opc)
    test (count $cmd) -eq 3; and test "$cmd[2]" = config
    and contains -- "$cmd[3]" get set unset
end

function __cswap_config_bare --description 'config with no action yet'
    set -l cmd (commandline -opc)
    test (count $cmd) -eq 2; and test "$cmd[2]" = config
end

complete -c cswap -n __cswap_config_bare -f -a list -d 'Show all effective settings (default)'
complete -c cswap -n __cswap_config_bare -f -a get -d "Print one setting's effective value"
complete -c cswap -n __cswap_config_bare -f -a set -d 'Validate and persist one setting'
complete -c cswap -n __cswap_config_bare -f -a unset -d 'Remove one setting (revert to default)'
complete -c cswap -n __cswap_config_bare -f -a path -d 'Print the settings.json location'
complete -c cswap -n '__cswap_subcommand config' -f -l json -d 'Emit machine-readable JSON'

# Keys, mirroring SETTING_SPECS in src/claude_swap/settings.py.
complete -c cswap -n '__cswap_config_key' -f -a autoswitch.threshold -d 'Switch at this pct used (50-99.9, default 90)'
complete -c cswap -n '__cswap_config_key' -f -a autoswitch.intervalSeconds -d 'Auto-loop poll interval (15-3600, default 60)'
complete -c cswap -n '__cswap_config_key' -f -a autoswitch.cooldownSeconds -d 'Min seconds between switches (default 300)'
complete -c cswap -n '__cswap_config_key' -f -a autoswitch.hysteresisPct -d 'Target must beat active by this pct (default 10)'
complete -c cswap -n '__cswap_config_key' -f -a autoswitch.strategy -d 'How auto-switch picks a target (default best)'
complete -c cswap -n '__cswap_config_key' -f -a autoswitch.includeApiKeyAccounts -d 'Rotate onto API-key accounts (default false)'
complete -c cswap -n '__cswap_config_key' -f -a autoswitch.unhealthyTicks -d 'Failed polls before unhealthy (default 3)'
complete -c cswap -n '__cswap_config_key' -f -a autoswitch.model -d "Also switch on these models' weekly limits"
complete -c cswap -n '__cswap_config_key' -f -a ui.theme -d 'Color theme (default auto)'

# Values, for the keys with a closed set.
function __cswap_config_setting_is --description 'config set KEY where KEY is $argv[1]'
    set -l cmd (commandline -opc)
    test (count $cmd) -eq 4; and test "$cmd[2]" = config; and test "$cmd[3]" = set
    and test "$cmd[4]" = "$argv[1]"
end

complete -c cswap -n '__cswap_config_setting_is autoswitch.strategy' -f -a 'best consume-first'
complete -c cswap -n '__cswap_config_setting_is autoswitch.includeApiKeyAccounts' -f -a 'true false'
complete -c cswap -n '__cswap_config_setting_is ui.theme' -f -a 'dark light auto'

# --- unclaimed ----------------------------------------------------------

complete -c cswap -n '__cswap_subcommand unclaimed' -x -l purge -d 'Delete this stashed entry by id'

# --- export / import ----------------------------------------------------
#
# Both take a PATH positional, so file completion stays on (see the --debug
# note above).

complete -c cswap -n '__cswap_subcommand export' -x -l account -a '(__cswap_accounts)' -d 'Limit the export to one account'
complete -c cswap -n '__cswap_subcommand export' -f -l full -d 'Include the full ~/.claude.json'
complete -c cswap -n '__cswap_subcommand import' -f -l force -d 'Overwrite existing accounts'
