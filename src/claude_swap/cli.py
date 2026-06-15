"""Command-line interface for Claude Swap."""

from __future__ import annotations

import argparse
import os
import sys

from claude_swap import __version__
from claude_swap.exceptions import ClaudeSwitchError, ValidationError
from claude_swap.printer import accent, dimmed, error, muted, warning
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


def _statusline_command() -> None:
    """Handle `cswap statusline` — internal: Claude Code pipes session JSON on stdin.

    Pre-dispatched (like `run`) and kept bulletproof: it must always print at
    most one line and exit 0 so it can never break Claude Code's status render.
    Claude Code may close the stdout pipe before we (or the interpreter's
    shutdown flush) finish writing; the ``finally`` flushes once and then
    redirects stdout to /dev/null so the shutdown flush can never raise
    BrokenPipeError into a non-zero exit + traceback.
    """
    try:
        stdin_text = sys.stdin.read()
    except BaseException:
        stdin_text = ""
    try:
        from claude_swap.statusline import run_statusline

        switcher = ClaudeAccountSwitcher()
        run_statusline(switcher, stdin_text)
    except BaseException:
        try:
            print("")
        except BaseException:
            pass
    finally:
        try:
            sys.stdout.flush()
        except BaseException:
            pass
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except BaseException:
            pass
    sys.exit(0)


def _launch_command(argv: list[str]) -> None:
    """Handle `cswap launch [--no-share] [--debug] [-- <claude args>]`.

    Pre-dispatched before the main parser (a positional subcommand can't coexist
    with main()'s required mutually-exclusive group). Supervises claude until it
    exits; mirrors claude's exit code.
    """
    if "--" in argv:
        split = argv.index("--")
        head, tail = argv[:split], argv[split + 1 :]
    else:
        head, tail = argv, []

    parser = argparse.ArgumentParser(
        prog="cswap launch",
        description=(
            "[BETA] Launch a load-balancer-managed Claude Code session. cswap "
            "picks the best account, embeds a statusline, and migrates / pauses "
            "the session automatically as usage limits are reached."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--no-share",
        action="store_true",
        help="Don't share ~/.claude settings/skills/etc. into the session profile",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args(head)

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        if sys.platform != "win32":
            if os.geteuid() == 0 and not switcher._is_running_in_container():
                error("Error: Do not run this script as root (unless running in a container)")
                sys.exit(1)
        from claude_swap.supervisor import launch

        sys.exit(launch(switcher, tail, share=not args.no_share))
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


def _cmux_command(argv: list[str]) -> None:
    """Handle ``cswap cmux <setup | [fanout] N> [-- <claude args>]``.

    Pre-dispatched (like ``run``/``launch``): a positional subcommand can't coexist
    with main()'s required mutually-exclusive group. macOS-only; emits a clean
    error (no traceback) when cmux isn't installed or the host isn't macOS.

      cswap cmux setup           install the "Balanced Claude (cswap)" surface
      cswap cmux 2               fanout: open 2 balancer-managed workspaces
      cswap cmux fanout 2        explicit fanout form
      cswap cmux 2 -- --resume   forward args after '--' to each session's claude
    """
    if "--" in argv:
        split = argv.index("--")
        head, tail = argv[:split], argv[split + 1 :]
    else:
        head, tail = argv, []

    parser = argparse.ArgumentParser(
        prog="cswap cmux",
        description=(
            "[BETA] cmux integration (macOS). Install a balancer-managed Claude "
            "surface into cmux, or fan out N workspaces that each land on a "
            "different account."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "target",
        metavar="setup | [fanout] N",
        help="'setup' to install the surface, or a count N to fan out N sessions",
    )
    parser.add_argument(
        "count",
        nargs="?",
        metavar="N",
        help="Number of workspaces (with the explicit 'fanout' form)",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args(head)

    # Resolve the (subcommand, n) intent from the two positionals.
    sub = args.target.lower()
    if sub == "setup":
        if args.count is not None:
            parser.error("`cswap cmux setup` takes no count")
        n = None
    elif sub == "fanout":
        if args.count is None:
            parser.error("`cswap cmux fanout` requires a count, e.g. `cswap cmux fanout 2`")
        n = _parse_positive_int(parser, args.count)
    else:
        # Bare `cswap cmux N` shorthand for fanout.
        if args.count is not None:
            parser.error(f"unexpected argument '{args.count}'")
        n = _parse_positive_int(parser, args.target)

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        if sys.platform != "win32":
            if os.geteuid() == 0 and not switcher._is_running_in_container():
                error("Error: Do not run this script as root (unless running in a container)")
                sys.exit(1)
        from claude_swap import cmux

        if n is None:
            status = cmux.setup(switcher)
            _print_cmux_setup(status)
        else:
            status = cmux.fanout(switcher, n, tail)
            _print_cmux_fanout(status)
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


def _parse_positive_int(parser: argparse.ArgumentParser, value: str) -> int:
    if not value.isdigit() or int(value) < 1:
        parser.error(f"expected a positive integer count, got '{value}'")
    return int(value)


def _print_cmux_setup(status: dict) -> None:
    """Render the result of ``cswap cmux setup``."""
    if status["ok"]:
        verb = "Updated" if status["changed"] else "Verified"
        print(accent(f"{verb} the cswap surface in cmux."))
        print(dimmed(
            "Open it from cmux's command palette / plus-button as "
            '"Balanced Claude (cswap)".'
        ))
        if status.get("backup_path"):
            print(muted(f"Backed up cmux.json to {status['backup_path']}"))
        if not status["reloaded"]:
            warning("cmux did not reload automatically; run `cmux reload-config`.")
    else:
        error("cmux did not accept the config.")
        for msg in status.get("messages", []):
            warning(msg)
        sys.exit(1)


def _print_cmux_fanout(status: dict) -> None:
    """Render the result of a ``cswap cmux N`` fanout."""
    opened, requested = status["opened"], status["requested"]
    print(accent(f"Opened {opened}/{requested} balancer-managed cmux workspace(s)."))
    print(muted(f"Each runs: {status['command']}"))
    accts = [a for a in status.get("accounts", []) if a]
    if accts:
        spread = (
            "distinct accounts" if status["distinct_accounts"] > 1 else "account"
        )
        print(dimmed(f"Landed on {spread}: " + ", ".join(f"Account-{a}" for a in accts)))
    for msg in status.get("messages", []):
        warning(msg)


def _parse_set_priority(value: str) -> tuple[str, int]:
    """Parse a ``NUM:PRIORITY`` argument for ``--set-priority``."""
    num, sep, pri = value.partition(":")
    num = num.strip()
    if not sep or not num.isdigit():
        raise ValidationError("--set-priority expects NUM:PRIORITY (e.g. 2:5)")
    try:
        priority = int(pri.strip())
    except ValueError:
        raise ValidationError(f"Invalid priority '{pri.strip()}' (must be a whole number)")
    return num, priority


def main() -> None:
    """Main entry point for the CLI."""
    if len(sys.argv) > 1 and sys.argv[1] == "run":
        _run_command(sys.argv[2:])
        return  # only reachable in tests where exec/exit is mocked
    if len(sys.argv) > 1 and sys.argv[1] == "statusline":
        _statusline_command()
        return
    if len(sys.argv) > 1 and sys.argv[1] == "launch":
        _launch_command(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "cmux":
        _cmux_command(sys.argv[2:])
        return

    parser = argparse.ArgumentParser(
        description="Multi-Account Switcher for Claude Code",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --add-account
  %(prog)s --add-token sk-ant-oat01-...
  %(prog)s --add-token sk-ant-oat01-... --slot 3
  %(prog)s --add-token sk-ant-oat01-... --email me@example.com
  %(prog)s --add-token - --slot 3
  %(prog)s --list
  %(prog)s --switch
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
            "defaults to setup-token-{slot}@token.local since setup-tokens "
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
            "Register a raw OAuth setup-token as a new account. "
            "Pass '-' to read from stdin or omit the value to be prompted securely."
        ),
    )
    group.add_argument(
        "--install",
        action="store_true",
        help="Embed cswap into Claude Code so `cswap launch` sessions auto-balance",
    )
    group.add_argument(
        "--balance",
        action="store_true",
        help="Open the load-balancer dashboard / settings (Beta)",
    )
    group.add_argument(
        "--set-priority",
        metavar="NUM:PRIORITY",
        help="Set an account's balancing priority (higher = burned through first)",
    )

    args = parser.parse_args()

    if args.token_status and not args.list:
        parser.error("--token-status can only be used with --list")

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
            switcher.list_accounts(
                show_token_status=args.token_status,
            )
        elif args.switch:
            switcher.switch()
        elif args.switch_to:
            switcher.switch_to(args.switch_to)
        elif args.status:
            switcher.status()
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
        elif args.install:
            from claude_swap import embed

            health = embed.install(switcher)
            if health["ok"]:
                print(accent("cswap is embedded in Claude Code."))
                print(dimmed(
                    "Start a managed session with `cswap launch` — it auto-balances "
                    "across your accounts. Plain `claude` stays vanilla."
                ))
            else:
                for issue in health["issues"]:
                    warning(issue)
        elif args.balance:
            try:
                from claude_swap.tui import run_balance
            except ImportError:
                error(
                    "TUI mode requires the 'curses' module. "
                    "On Windows, install with: pip install windows-curses"
                )
                sys.exit(1)
            sys.exit(run_balance(switcher))
        elif args.set_priority:
            num, priority = _parse_set_priority(args.set_priority)
            switcher.set_account_priority(num, priority)
            print(f"{accent('Set')} Account-{num} priority to {priority}")
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)

    # Passive update notification (never fails). Skipped after --purge so we
    # don't immediately recreate <backup_root>/cache/update_check.json inside
    # the directory we just deleted. Skipped after --upgrade as a safety guard
    # in case the dispatch is later refactored to fall through.
    if not args.purge and not args.upgrade:
        from claude_swap.update_check import check_for_update

        msg = check_for_update(__version__)
        if msg:
            print(f"\n{muted(msg)}", file=sys.stderr)


if __name__ == "__main__":
    main()
