"""``cswap codex ...`` — the Codex provider's command surface.

A namespace rather than a ``--provider`` flag: bare ``cswap switch`` and
``cswap list`` keep meaning Claude, so every script and every habit built on the
existing CLI is untouched. Nothing in this module changes an existing command.

Dispatched from ``cli.main`` alongside the other pre-dispatch handlers
(``run``/``auto``/``config``/``map``), for the same reason they are: a
positional subcommand cannot coexist with the main parser's mutually-exclusive
flag group.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys

from claude_swap.codex.registry_import import import_codex_auth_registry
from claude_swap.codex.switcher import CodexSwitcher
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.printer import dimmed, error, warning


def _auto_import() -> None:
    """Import codex-auth's accounts the first time, once.

    Automatic because an explicit command the user has to discover means they
    run ``cswap codex list``, see nothing, and conclude cswap is broken. The
    source tree is left untouched, so this is reversible.
    """
    result = import_codex_auth_registry(only_if_empty=True)
    if result.unsupported_schema is not None:
        warning(
            f"codex-auth registry uses schema {result.unsupported_schema}, which this "
            "version of cswap does not understand — not importing."
        )
        return
    if result.imported:
        print(
            dimmed(
                f"Imported {result.imported} Codex account(s) from {result.source} "
                "(the original is left untouched)."
            )
        )
    if result.skipped:
        print(dimmed(f"Skipped {result.skipped} account(s) with no usable auth file."))


def _usage_summary(usage: dict | None) -> str:
    if not usage:
        return ""
    parts = []
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        window = usage.get(key)
        if isinstance(window, dict) and isinstance(window.get("pct"), (int, float)):
            parts.append(f"{label} {window['pct']:.0f}%")
    return "  ".join(parts)


def _relative(seconds: float | None) -> str:
    """A compact human duration, e.g. "6d 4h", "12m", "expired"."""
    if seconds is None:
        return "unknown"
    if seconds <= 0:
        return "expired"
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _print_list(
    switcher: CodexSwitcher,
    *,
    as_json: bool,
    skip_api: bool,
    token_status: bool = False,
) -> None:
    # None = every account eligible; an empty set = no network at all.
    numbers: set[str] | None = set() if skip_api else None
    snap = switcher.accounts_snapshot(fetch=numbers)

    if as_json:
        payload = {
            "provider": "codex",
            "activeNumber": snap.active_number,
            "accounts": [
                {
                    "number": a.number,
                    "email": a.email,
                    "workspace": a.org_name,
                    "alias": a.alias,
                    "active": a.is_active,
                    "disabled": a.disabled,
                    "kind": a.kind,
                    "usage": a.usage.last_good,
                    "sentinel": a.usage.sentinel,
                    "fetchedAt": a.usage.fetched_at,
                    "ageSeconds": a.usage.age_s,
                    "plan": (a.usage.last_good or {}).get("plan"),
                }
                for a in snap.accounts
            ],
        }
        if token_status:
            payload["tokenStatus"] = [
                switcher.token_status(a.number) for a in snap.accounts
            ]
        print(json.dumps(payload, indent=2))
        return

    if not snap.accounts:
        print("No Codex accounts. Run 'cswap codex add' or 'cswap codex login'.")
        return

    for a in snap.accounts:
        marker = "*" if a.is_active else " "
        alias = f" ({a.alias})" if a.alias else ""
        state = " [disabled]" if a.disabled else ""
        usage = a.usage.sentinel or _usage_summary(a.usage.last_good)
        suffix = f"  {usage}" if usage else ""
        print(f"{marker} {a.number}. {a.email} [{a.display_tag}]{alias}{state}{suffix}")

        if token_status:
            st = switcher.token_status(a.number)
            if st["state"] != "oauth":
                print(dimmed(f"      token: {st['state']}"))
            else:
                due = "refresh due" if st["refreshDue"] else "valid"
                rt = "" if st["hasRefreshToken"] else ", NO refresh token"
                last = st["lastRefresh"] or "never"
                print(
                    dimmed(
                        f"      token: {due}, expires in "
                        f"{_relative(st['expiresInSeconds'])}{rt}; "
                        f"last refresh {last}"
                    )
                )


def _do_login(args) -> None:
    """Run ``codex login``, then capture the account it produced."""
    binary = shutil.which("codex")
    if not binary:
        error(
            "The 'codex' CLI is not on PATH. Install it, or run 'cswap codex add' "
            "after logging in another way."
        )
        sys.exit(1)

    cmd = [binary, "login"]
    if args.device_auth:
        cmd.append("--device-auth")
    # shell=False, and the resolved absolute path: a shell function named
    # `codex` is a common setup (one that injects
    # --dangerously-bypass-approvals-and-sandbox is in the wild), and inheriting
    # it would run this login under flags cswap never chose.
    result = subprocess.run(cmd, shell=False)
    if result.returncode != 0:
        error("codex login did not complete.")
        sys.exit(result.returncode)

    slot = CodexSwitcher().add_account(alias=args.alias or "")
    print(f"Added Codex account {slot.number}: {slot.display_label}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cswap codex",
        description="Multi-account switcher for the Codex CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands:
  list [--json] [--skip-api] [--token-status]
                          list managed Codex accounts and their usage
  status                  show the account the codex CLI is currently using
  switch <num|email|alias>
                          activate a stored account
  add [--alias NAME]      store the account you are currently logged in as
  login [--device-auth] [--alias NAME]
                          run 'codex login', then store the result
  remove <num|email> [-y] forget an account and delete its credentials
  alias <num|email> NAME  set a short alias  (--unset to clear)
  disable|enable <target> hold an account out of / return it to auto-rotation
  import                  re-run the codex-auth import

Examples:
  cswap codex list                    5h / weekly usage per account
  cswap codex list --skip-api         cached only, no network
  cswap codex list --token-status     token expiry (never prints the token)
  cswap codex list --json             machine-readable
  cswap codex switch work             by alias
  cswap codex disable 2               keep it out of `cswap auto` rotation

Auto-switching:
  Codex rides along in `cswap auto`, which rotates both providers. Tune it with
  `cswap config set autoswitch.codexThreshold 85` (0 = inherit
  autoswitch.threshold) or turn it off with `autoswitch.codexEnabled false`.
  autoswitch.includeApiKeyAccounts is Claude-only: a Codex API-key login
  reports no usage, so a threshold has nothing to compare.

Notes:
  Bare `cswap list` / `cswap switch` still mean Claude — nothing you already
  type changes.

  Switching rewrites ~/.codex/auth.json. A codex session that is ALREADY
  RUNNING keeps its old account until you restart it; cswap warns you and names
  the running PIDs. This applies to automatic switching too — it only affects
  the next session you start.

  If you use codex-auth, your accounts are imported automatically on the first
  `cswap codex` command. ~/.codex/accounts/ is left untouched.
        """,
    )
    sub = parser.add_subparsers(dest="verb", required=True)

    p_list = sub.add_parser("list", help="List managed Codex accounts")
    p_list.add_argument("--json", action="store_true", help="Machine-readable output")
    p_list.add_argument("--skip-api", action="store_true", help="Do not fetch usage")
    p_list.add_argument(
        "--token-status",
        action="store_true",
        help="Show token expiry diagnostics (never the token itself)",
    )

    sub.add_parser("status", help="Show the currently active Codex account")

    p_switch = sub.add_parser("switch", help="Activate a stored account")
    p_switch.add_argument("account", metavar="NUM|EMAIL|ALIAS")

    p_add = sub.add_parser("add", help="Store the current Codex login")
    p_add.add_argument("--alias", metavar="NAME", default="")

    p_login = sub.add_parser("login", help="Run 'codex login', then store the account")
    p_login.add_argument("--device-auth", action="store_true")
    p_login.add_argument("--alias", metavar="NAME", default="")

    p_remove = sub.add_parser("remove", help="Forget an account")
    p_remove.add_argument("account", metavar="NUM|EMAIL|ALIAS")
    p_remove.add_argument("-y", "--yes", action="store_true")

    p_alias = sub.add_parser("alias", help="Set or clear an account alias")
    p_alias.add_argument("account", metavar="NUM|EMAIL")
    p_alias.add_argument("name", nargs="?", default="")
    p_alias.add_argument("--unset", action="store_true")

    for verb, holding in (("disable", True), ("enable", False)):
        p = sub.add_parser(
            verb,
            help=("Hold out of auto-rotation" if holding else "Return to auto-rotation"),
        )
        p.add_argument("account", metavar="NUM|EMAIL|ALIAS")

    sub.add_parser("import", help="Re-run the codex-auth registry import")
    return parser


def codex_command(argv: list[str]) -> None:
    """Entry point for ``cswap codex ...``."""
    args = _build_parser().parse_args(argv)

    try:
        if args.verb == "import":
            result = import_codex_auth_registry()
            print(f"Imported {result.imported}, skipped {result.skipped}.")
            return

        _auto_import()
        switcher = CodexSwitcher()

        if args.verb == "list":
            _print_list(
                switcher,
                as_json=args.json,
                skip_api=args.skip_api,
                token_status=args.token_status,
            )
        elif args.verb == "status":
            number = switcher.current_account_number()
            if number is None:
                print("No managed Codex account is active.")
            else:
                _num, _email, label = switcher.resolve_account(number)
                print(f"Active Codex account {number}: {label}")
        elif args.verb == "switch":
            result = switcher.switch_to(args.account)
            print(f"Switched to Codex account {result.number}: {result.email}")
            if result.running_pids:
                pids = ", ".join(str(p) for p in result.running_pids)
                warning(
                    f"codex is running (pid {pids}) — restart it for the new "
                    "account to take effect."
                )
        elif args.verb == "add":
            slot = switcher.add_account(alias=args.alias)
            print(f"Added Codex account {slot.number}: {slot.display_label}")
        elif args.verb == "login":
            _do_login(args)
        elif args.verb == "remove":
            switcher.remove_account(args.account, assume_yes=args.yes)
            print(f"Removed Codex account {args.account}")
        elif args.verb == "alias":
            if args.unset or not args.name:
                number = switcher.unset_alias(args.account)
                print(f"Cleared alias for Codex account {number}")
            else:
                number, alias = switcher.set_alias(args.account, args.name)
                print(f"Codex account {number} is now '{alias}'")
        elif args.verb in ("disable", "enable"):
            switcher.set_account_disabled(args.account, args.verb == "disable")
            print(f"Codex account {args.account} {args.verb}d")
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except ValueError as e:
        # normalize_alias rejects bad aliases with ValueError; that is user
        # input, not a bug.
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)
