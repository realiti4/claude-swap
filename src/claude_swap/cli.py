"""Command-line interface for Claude Swap."""

from __future__ import annotations

import argparse
import json
import os
import sys

from claude_swap import __version__
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.json_output import error_envelope
from claude_swap.printer import dimmed, error, muted
from claude_swap.switcher import ClaudeAccountSwitcher


def _run_command(argv: list[str]) -> None:
    """Handle `cswap run NUM|EMAIL [--no-share] [-- <claude args>]`.

    Pre-dispatched before the main parser is built: a positional subcommand
    can't coexist with main()'s required mutually-exclusive flag group, and
    this keeps the existing parser untouched. Limitation: `run` must be the
    first argument (`cswap --debug run 2` is not supported; use
    `cswap run 2 --debug`).

    On POSIX this execs claude and never returns; on Windows it exits with
    claude's return code. Either way the post-dispatch update check in
    main() is unreachable, which is intended.
    """
    # Everything after the first `--` is forwarded to claude verbatim.
    if "--" in argv:
        split = argv.index("--")
        head, tail = argv[:split], argv[split + 1 :]
    else:
        head, tail = argv, []

    parser = argparse.ArgumentParser(
        prog="cswap run",
        description=(
            "[EXPERIMENTAL] Launch Claude Code as a stored account in this "
            "terminal only (the default login and other terminals are "
            "unaffected)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  cswap run 2
  cswap run user@example.com
  cswap run 2 --no-share
  cswap run 2 -- --resume
        """,
    )
    parser.add_argument(
        "account",
        metavar="NUM|EMAIL",
        help="Account to run (number or email)",
    )
    parser.add_argument(
        "--no-share",
        action="store_true",
        help=(
            "Don't share settings/keybindings/CLAUDE.md/skills/commands/agents "
            "from ~/.claude into the session profile (and remove previously "
            "shared items)"
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args(head)

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)

        if sys.platform != "win32":
            if os.geteuid() == 0 and not switcher._is_running_in_container():
                error("Error: Do not run this script as root (unless running in a container)")
                sys.exit(1)

        from claude_swap.session import SessionManager

        SessionManager(switcher).run(args.account, tail, share=not args.no_share)
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


def main() -> None:
    """Main entry point for the CLI."""
    if len(sys.argv) > 1 and sys.argv[1] == "run":
        _run_command(sys.argv[2:])
        return  # only reachable in tests where exec/exit is mocked

    parser = argparse.ArgumentParser(
        description="Multi-Account Switcher for Claude Code",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --add-account
  %(prog)s --add-token sk-ant-oat01-...           # OAuth setup-token
  %(prog)s --add-token sk-ant-api03-...           # managed API key
  %(prog)s --add-token sk-ant-oat01-... --slot 3
  %(prog)s --add-token sk-ant-oat01-... --email me@example.com
  %(prog)s --add-token - --slot 3
  %(prog)s --list
  %(prog)s --switch
  %(prog)s --switch --strategy best             # switch to the account with most quota left
  %(prog)s --switch --strategy next-available   # rotate, skipping rate-limited accounts
  %(prog)s --switch-to 2
  %(prog)s --switch-to user@example.com
  %(prog)s run 2                            # run account 2 in this terminal only
  %(prog)s run 2 -- --resume                # forward args after '--' to claude
  %(prog)s --remove-account user@example.com
  %(prog)s --status
  %(prog)s --purge
  %(prog)s --export backup.cswap
  %(prog)s --import backup.cswap
  %(prog)s --tui                              # interactive arrow-key menu
  %(prog)s --upgrade                          # self-upgrade to latest version
        """,
    )

    # Version and debug flags (outside mutually exclusive group)
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--token-status",
        action="store_true",
        help="Show OAuth token expiry state (use with --list)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help=(
            "Emit machine-readable JSON to stdout (use with --list, --status, "
            "--switch, or --switch-to). See README 'JSON output for scripting'."
        ),
    )
    parser.add_argument(
        "--strategy",
        choices=["best", "next-available"],
        metavar="{best,next-available}",
        help=(
            "With --switch: pick the target by remaining 5h/7d quota. "
            "'best' jumps to the account with the most headroom; "
            "'next-available' rotates to the next account, skipping any at their limit"
        ),
    )
    parser.add_argument(
        "--slot",
        type=int,
        metavar="NUM",
        help="Specify slot number when adding account (use with --add-account or --add-token)",
    )
    parser.add_argument(
        "--email",
        metavar="EMAIL",
        help=(
            "Email address for the account. Optional with --add-token; "
            "defaults to setup-token-{slot}@token.local (or "
            "api-key-{slot}@token.local for API keys) since these tokens "
            "carry no real email metadata."
        ),
    )
    parser.add_argument(
        "--account",
        metavar="NUM|EMAIL",
        help="Limit export to one account (use with --export)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing accounts during import",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Include full ~/.claude.json in export (default: oauthAccount only)",
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--add-account",
        action="store_true",
        help="Add current account to managed accounts",
    )
    group.add_argument(
        "--remove-account",
        metavar="NUM|EMAIL",
        help="Remove account by number or email",
    )
    group.add_argument(
        "--list",
        action="store_true",
        help="List all managed accounts",
    )
    group.add_argument(
        "--switch",
        action="store_true",
        help="Rotate to next account in sequence",
    )
    group.add_argument(
        "--switch-to",
        metavar="NUM|EMAIL",
        help="Switch to specific account number or email",
    )
    group.add_argument(
        "--status",
        action="store_true",
        help="Show current account status",
    )
    group.add_argument(
        "--purge",
        action="store_true",
        help="Remove all claude-swap data from the system",
    )
    group.add_argument(
        "--export",
        metavar="PATH",
        help="Export accounts to file (use '-' for stdout)",
    )
    group.add_argument(
        "--import",
        dest="import_",
        metavar="PATH",
        help="Import accounts from file (use '-' for stdin)",
    )
    group.add_argument(
        "--tui",
        action="store_true",
        help="Launch interactive arrow-key menu (single-level)",
    )
    group.add_argument(
        "--upgrade",
        action="store_true",
        help="Upgrade claude-swap to the latest version on PyPI",
    )
    group.add_argument(
        "--add-token",
        metavar="TOKEN|-",
        nargs="?",
        const="",
        help=(
            "Register a raw OAuth setup-token or managed API key (sk-ant-api...) "
            "as a new account; the type is auto-detected. Pass '-' to read from "
            "stdin or omit the value to be prompted securely."
        ),
    )

    args = parser.parse_args()

    if args.token_status and not args.list:
        parser.error("--token-status can only be used with --list")

    if args.json and not (args.list or args.status or args.switch or args.switch_to):
        parser.error(
            "--json can only be used with --list, --status, --switch, or --switch-to"
        )

    if args.json and args.token_status:
        # Token status is not part of the JSON v1 schema; reject rather than
        # silently ignore it (a future additive field can add it).
        parser.error("--token-status cannot be combined with --json")

    if args.strategy is not None and not args.switch:
        parser.error("--strategy can only be used with --switch")

    if args.slot is not None and not (args.add_account or args.add_token is not None):
        parser.error("--slot can only be used with --add-account or --add-token")

    if args.email is not None and args.add_token is None:
        parser.error("--email can only be used with --add-token")

    if args.account is not None and not args.export:
        parser.error("--account can only be used with --export")

    if args.force and not args.import_:
        parser.error("--force can only be used with --import")

    if args.full and not args.export:
        parser.error("--full can only be used with --export")

    # Self-upgrade runs before switcher init so we don't touch config/keychain
    # just to upgrade the tool itself.
    if args.upgrade:
        from claude_swap.update_check import run_self_upgrade

        try:
            sys.exit(run_self_upgrade())
        except KeyboardInterrupt:
            print(f"\n{dimmed('Upgrade cancelled')}")
            sys.exit(130)

    # Initialize switcher and dispatch under a single error handler so
    # init-time failures (e.g. MigrationError on a backup-dir collision)
    # are presented like every other ClaudeSwitchError: clean stderr line,
    # exit 1, no traceback.
    # JSON-capable commands return a payload; the CLI is the single point that
    # serializes it (so no command writes JSON to stdout itself).
    payload: dict | None = None
    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)

        # Check for root (unless in container) - POSIX only
        if sys.platform != "win32":
            if os.geteuid() == 0 and not switcher._is_running_in_container():
                error("Error: Do not run this script as root (unless running in a container)")
                sys.exit(1)

        if args.add_account:
            switcher.add_account(slot=args.slot)
        elif args.add_token is not None:
            switcher.add_account_from_token(
                token=args.add_token,
                email=args.email,
                slot=args.slot,
            )
        elif args.remove_account:
            switcher.remove_account(args.remove_account)
        elif args.list:
            payload = switcher.list_accounts(
                show_token_status=args.token_status,
                json_output=args.json,
            )
        elif args.switch:
            payload = switcher.switch(strategy=args.strategy, json_output=args.json)
        elif args.switch_to:
            payload = switcher.switch_to(args.switch_to, json_output=args.json)
        elif args.status:
            payload = switcher.status(json_output=args.json)
        elif args.purge:
            switcher.purge()
        elif args.export:
            from claude_swap.transfer import export_accounts

            export_accounts(switcher, args.export, account=args.account, full=args.full)
        elif args.import_:
            from claude_swap.transfer import import_accounts

            import_accounts(switcher, args.import_, force=args.force)
        elif args.tui:
            try:
                from claude_swap.tui import run as tui_run
            except ImportError as e:
                error(
                    "TUI mode requires the 'curses' module. "
                    "On Windows, install with: pip install windows-curses"
                )
                sys.exit(1)
            sys.exit(tui_run(switcher))
    except ClaudeSwitchError as e:
        # In JSON mode keep stdout pure JSON: emit the structured error envelope
        # there (exit 1) instead of a red stderr line.
        if args.json:
            print(json.dumps(error_envelope(e), indent=2))
        else:
            error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        # Route the cancellation note to stderr in JSON mode so stdout stays
        # parseable (the guarantee covers completion / handled errors, not Ctrl-C).
        print(
            f"\n{dimmed('Operation cancelled')}",
            file=sys.stderr if args.json else sys.stdout,
        )
        sys.exit(130)

    if args.json and payload is not None:
        print(json.dumps(payload, indent=2))

    # Passive update notification (never fails). Skipped after --purge so we
    # don't immediately recreate <backup_root>/cache/update_check.json inside
    # the directory we just deleted. Skipped after --upgrade as a safety guard
    # in case the dispatch is later refactored to fall through.
    if not args.purge and not args.upgrade and not args.json:
        from claude_swap.update_check import check_for_update

        msg = check_for_update(__version__)
        if msg:
            print(f"\n{muted(msg)}", file=sys.stderr)


if __name__ == "__main__":
    main()
