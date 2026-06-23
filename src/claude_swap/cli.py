"""Command-line interface for Claude Swap."""

from __future__ import annotations

import argparse
import os
import sys

from claude_swap import __version__
from claude_swap.auto_switch import AutoSwitcher, load_config as _as_load_config, save_config as _as_save_config
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.launchd import agent_status, install_agent, uninstall_agent
from claude_swap.models import Platform
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


def _fmt_clock_ago(ts: float) -> str:
    """Render a wall-clock POSIX ts as ``HH:MM (Nm ago)`` in local time."""
    from datetime import datetime, timezone

    try:
        dt_local = datetime.fromtimestamp(ts).astimezone()
        delta = datetime.now(timezone.utc).timestamp() - ts
        mins = max(0, int(delta // 60))
        if mins >= 60:
            ago = f"{mins // 60}h {mins % 60}m ago"
        else:
            ago = f"{mins}m ago"
        return f"{dt_local.strftime('%H:%M')} ({ago})"
    except Exception:
        return "unknown"


def _monitoring_status_line(backup_root) -> str:
    """One-line monitoring health for ``cswap auto status`` (offline-aware).

    Only renders "offline" once we've actually DECLARED offline
    (``offline_notified`` — i.e. >= 2 consecutive failures, matching the
    notification gate). A single transient failure that hasn't yet crossed the
    threshold renders as "checking — retrying", so the UI word "offline" lines
    up with when the engine truly considers itself offline.
    """
    from claude_swap.auto_switch_state import load_state

    state = load_state(backup_root)
    if state.offline_notified:
        if state.last_online_ts is not None:
            return f"offline since {_fmt_clock_ago(state.last_online_ts)}"
        return "offline (never reached the usage API)"
    if 0 < state.consecutive_failures < 2:
        n = state.consecutive_failures
        return f"checking — {n} failed check{'s' if n != 1 else ''}, retrying"
    if state.last_online_ts is not None:
        return f"online (last check {_fmt_clock_ago(state.last_online_ts)})"
    return "online (no check yet)"


def _last_switch_status_line(backup_root) -> str | None:
    """``account-X at HH:MM (reason)`` from state, or None when never switched."""
    from claude_swap.auto_switch_state import load_state

    state = load_state(backup_root)
    sw = state.last_switch
    if not isinstance(sw, dict) or "account" not in sw:
        return None
    acct = sw.get("account")
    reason = sw.get("reason", "")
    ts = sw.get("ts")
    when = _fmt_clock_ago(ts) if isinstance(ts, (int, float)) else "unknown"
    return f"account-{acct} at {when} ({reason})"


def _last_usage_status_lines(backup_root) -> list[str]:
    """Per-account last-known 5h/7d% (with age) from monitor state.

    Renders the ``last_usage`` the daemon records each tick — clearly labelled
    as last-known (the daemon probes only the active account most ticks, so a
    peer's reading can be older). Empty when nothing has been recorded yet.
    """
    from claude_swap.auto_switch_state import load_state

    state = load_state(backup_root)
    if not isinstance(state.last_usage, dict) or not state.last_usage:
        return []

    def _pct(usage: dict, key: str):
        w = usage.get(key) if isinstance(usage, dict) else None
        if isinstance(w, dict) and isinstance(w.get("pct"), (int, float)):
            return float(w["pct"])
        return None

    lines: list[str] = []
    for num in sorted(state.last_usage, key=lambda n: (len(n), n)):
        entry = state.last_usage[num]
        if not isinstance(entry, dict):
            continue
        usage = entry.get("usage")
        if not isinstance(usage, dict):
            continue
        h5 = _pct(usage, "five_hour")
        d7 = _pct(usage, "seven_day")
        h5_s = f"5h {h5:.0f}%" if h5 is not None else "5h —"
        d7_s = f"7d {d7:.0f}%" if d7 is not None else "7d —"
        fetched_at = entry.get("fetched_at")
        age = (
            _fmt_clock_ago(fetched_at)
            if isinstance(fetched_at, (int, float))
            else "unknown"
        )
        lines.append(f"  acct {num}: {h5_s}  {d7_s}  (last-known {age})")
    return lines


def _auto_command(argv: list[str]) -> None:
    """Handle ``cswap auto [on|off|status]``, ``cswap watch``, ``cswap _auto-daemon``.

    Pre-dispatched before the main argparse parser so these subcommands can
    coexist with the existing required mutually-exclusive group.
    """
    from dataclasses import replace as _replace

    from claude_swap.auto_switch import AutoSwitchConfig  # noqa: F401 — for type hints

    verb = argv[0] if argv else "auto"

    # Hidden daemon entry point (launchd calls this).
    if verb == "_auto-daemon":
        try:
            switcher = ClaudeAccountSwitcher()
            config = _as_load_config(switcher.backup_dir)
            AutoSwitcher(switcher, config).run_daemon()
        except Exception as exc:
            error(f"Error: auto-switch daemon: {exc}")
            sys.exit(1)
        return

    # Foreground watch loop.
    if verb == "watch":
        parser = argparse.ArgumentParser(prog="cswap watch")
        parser.add_argument("--session-threshold", type=float, metavar="N")
        parser.add_argument("--weekly-threshold", type=float, metavar="N")
        parser.add_argument("--no-notify", action="store_true")
        parser.add_argument(
            "--strategy", choices=["reactive", "consume-first"],
            metavar="{reactive,consume-first}",
        )
        args = parser.parse_args(argv[1:])

        if Platform.detect() is not Platform.MACOS:
            print(dimmed(
                "Auto-switch watch is a macOS-only feature for now.\n"
                "The decision engine and tests run on all platforms."
            ))
            sys.exit(0)

        try:
            switcher = ClaudeAccountSwitcher()
            config = _as_load_config(switcher.backup_dir)
            overrides: dict = {}
            if args.session_threshold is not None:
                overrides["session_threshold"] = args.session_threshold
            if args.weekly_threshold is not None:
                overrides["weekly_threshold"] = args.weekly_threshold
            if args.no_notify:
                overrides["notify"] = False
            if args.strategy is not None:
                overrides["strategy"] = args.strategy
            if overrides:
                config = _replace(config, **overrides)

            AutoSwitcher(switcher, config).watch()
        except Exception as exc:
            error(f"Error: {exc}")
            sys.exit(1)
        return

    # cswap auto [on|off|status]
    parser = argparse.ArgumentParser(prog="cswap auto")
    parser.add_argument(
        "subverb",
        nargs="?",
        choices=["on", "off", "status"],
        default="status",
    )
    parser.add_argument("--session-threshold", type=float, metavar="N")
    parser.add_argument("--weekly-threshold", type=float, metavar="N")
    parser.add_argument("--no-notify", action="store_true")
    parser.add_argument(
        "--strategy", choices=["reactive", "consume-first"],
        metavar="{reactive,consume-first}",
    )
    args = parser.parse_args(argv[1:])

    try:
        switcher = ClaudeAccountSwitcher()
        backup_root = switcher.backup_dir

        if args.subverb == "on":
            # New install (no config file yet) defaults to the proactive
            # consume-first policy; an existing config keeps its strategy unless
            # --strategy overrides it.
            config_existed = (backup_root / "auto-switch.json").exists()
            config = _as_load_config(backup_root)
            overrides_on: dict = {"enabled": True}
            if args.session_threshold is not None:
                overrides_on["session_threshold"] = args.session_threshold
            if args.weekly_threshold is not None:
                overrides_on["weekly_threshold"] = args.weekly_threshold
            if args.no_notify:
                overrides_on["notify"] = False
            if args.strategy is not None:
                overrides_on["strategy"] = args.strategy
            elif not config_existed:
                overrides_on["strategy"] = "consume-first"
            config = _replace(config, **overrides_on)
            _as_save_config(config, backup_root)
            # Normal-visibility disclosure: a new install silently defaults to
            # consume-first (and a purge-then-on can flip an existing user), so
            # the active strategy must be plainly shown, not dimmed.
            print(f"Auto-switch enabled (strategy: {config.strategy}).")

            if Platform.detect() is Platform.MACOS:
                msg = install_agent(backup_root)
                print(msg)
            else:
                print(dimmed(
                    "Auto-switch daemon is macOS-only; config saved but daemon "
                    "not installed. Use `cswap watch` to run it in the foreground."
                ))

        elif args.subverb == "off":
            config = _as_load_config(backup_root)
            config = _replace(config, enabled=False)
            _as_save_config(config, backup_root)

            if Platform.detect() is Platform.MACOS:
                msg = uninstall_agent()
                print(msg)
            else:
                print(dimmed("Config saved (no daemon to uninstall — macOS only)."))

        else:  # status (default)
            config = _as_load_config(backup_root)
            print(f"enabled:           {config.enabled}")
            print(f"strategy:          {config.strategy}")
            print(f"session_threshold: {config.session_threshold}%")
            print(f"weekly_threshold:  {config.weekly_threshold}%")
            print(f"notify:            {config.notify}")
            print(f"poll interval:     {config.min_interval}s – {config.max_interval}s")
            print(f"monitoring:        {_monitoring_status_line(backup_root)}")
            last_switch = _last_switch_status_line(backup_root)
            if last_switch is not None:
                print(f"last switch:       {last_switch}")
            usage_lines = _last_usage_status_lines(backup_root)
            if usage_lines:
                print("last-known usage:")
                for line in usage_lines:
                    print(line)

            if Platform.detect() is Platform.MACOS:
                print(f"agent:             {agent_status()}")
            else:
                print(dimmed("agent:             n/a (macOS only)"))

    except Exception as exc:
        error(f"Error: {exc}")
        sys.exit(1)


def main() -> None:
    """Main entry point for the CLI."""
    if len(sys.argv) > 1 and sys.argv[1] == "run":
        _run_command(sys.argv[2:])
        return  # only reachable in tests where exec/exit is mocked

    if len(sys.argv) > 1 and sys.argv[1] in ("auto", "watch", "_auto-daemon"):
        _auto_command(sys.argv[1:])
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
  %(prog)s --switch --strategy best             # switch to the account with most quota left
  %(prog)s --switch --strategy next-available   # rotate, skipping rate-limited accounts
  %(prog)s --switch-to 2
  %(prog)s --switch-to user@example.com
  %(prog)s run 2                            # run account 2 in this terminal only
  %(prog)s run 2 -- --resume                # forward args after '--' to claude
  %(prog)s auto on                          # macOS: auto-switch accounts before you hit a limit
  %(prog)s watch                            # run the auto-switcher in this terminal
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

    args = parser.parse_args()

    if args.token_status and not args.list:
        parser.error("--token-status can only be used with --list")

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
            switcher.switch(strategy=args.strategy)
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
