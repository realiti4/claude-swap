"""Command-line interface for Claude Swap."""

from __future__ import annotations

import argparse
import os
import sys

from claude_swap import __version__
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.switcher import ClaudeAccountSwitcher


def main() -> None:
    """Main entry point for the CLI."""
    parser = argparse.ArgumentParser(
        description="Multi-Account Switcher for Claude Code",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --add-account
  %(prog)s --list
  %(prog)s --switch
  %(prog)s --switch-to 2
  %(prog)s --switch-to user@example.com
  %(prog)s --remove-account user@example.com
  %(prog)s --status
  %(prog)s --purge
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

    args = parser.parse_args()

    # Initialize switcher with debug mode
    switcher = ClaudeAccountSwitcher(debug=args.debug)

    # Check for root (unless in container) - POSIX only
    if sys.platform != "win32":
        if os.geteuid() == 0 and not switcher._is_running_in_container():
            print("Error: Do not run this script as root (unless running in a container)")
            sys.exit(1)

    try:
        if args.add_account:
            switcher.add_account()
        elif args.remove_account:
            switcher.remove_account(args.remove_account)
        elif args.list:
            switcher.list_accounts()
        elif args.switch:
            switcher.switch()
        elif args.switch_to:
            switcher.switch_to(args.switch_to)
        elif args.status:
            switcher.status()
        elif args.purge:
            switcher.purge()
    except ClaudeSwitchError as e:
        print(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nOperation cancelled")
        sys.exit(130)


if __name__ == "__main__":
    main()
