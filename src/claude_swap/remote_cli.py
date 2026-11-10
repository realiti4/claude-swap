"""`cswap remote ...`: share setup-token accounts through an encrypted git repository."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from typing import Callable, TypeVar

from claude_swap.exceptions import ClaudeSwitchError, PassphraseError, ValidationError
from claude_swap.printer import accent, bolded, dimmed, error, muted, warning, yellowed

T = TypeVar("T")

PASSPHRASE_ENV = "CSWAP_REMOTE_PASSPHRASE"

_EPILOG = """
Only `claude setup-token` accounts are shared: they never refresh, so every
machine can use the same token at once. Nothing runs in the background; each
command fetches the repository first.

Commands:
  add NAME URL            add a repository (an empty one is initialized)
  list                    the repositories this machine knows
  show NAME               fetch and compare: which accounts are where
  push NAME [ACCOUNT...]  copy accounts from here to the repository
  pull NAME [ENTRY...]    copy accounts from the repository to here
  add-token NAME [TOKEN|-]  put a setup-token in the repository only
  set-token NAME ENTRY [TOKEN|-]  replace an account's token (repository, and here if it's here)
  rm-entry NAME ENTRY     delete an account from the repository
  skip NAME ENTRY         don't pull this account on this machine (--undo)
  passwd NAME             change the repository's passphrase
  forget NAME             stop remembering the passphrase on this machine
  set NAME ...            change the URL, branch or git login
  remove NAME             forget a repository on this machine

Examples:
  cswap remote add team git@github.com:me/claude-tokens.git
  cswap remote add team https://github.com/me/claude-tokens.git --token -
  cswap remote add team git@host:x.git --ssh-key ~/.ssh/tokens_deploy
  cswap remote show team
  cswap remote push team            # every local account, asks about changes
  cswap remote pull team --yes      # every remote account, takes all changes
  cswap remote add-token team - --email spare@example.com < token.txt
  cswap remote set-token team spare@example.com - < new-token.txt

The passphrase is remembered on this machine unless --no-remember is given
(Keychain on macOS, otherwise a 0600 file in the cswap data folder). It can
also come from the CSWAP_REMOTE_PASSPHRASE environment variable.
"""


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _read_secret(value: str | None, prompt: str) -> str:
    """'-' reads a line from stdin, '' prompts; anything else is refused (argv leaks)."""
    if value == "-":
        return sys.stdin.readline().rstrip("\n")
    if value:
        raise ValidationError(
            "Don't put the access token on the command line (shell history, "
            "process list): pass '-' to read it from stdin, or nothing to be asked."
        )
    if not _interactive():
        raise ValidationError("No terminal to ask for the access token on; pass '-' and pipe it in.")
    return getpass.getpass(prompt)


def _new_passphrase(prompt: str = "Passphrase: ") -> str:
    env = os.environ.get(PASSPHRASE_ENV)
    if env:
        return env
    if not _interactive():
        raise ValidationError(f"No terminal to ask for the passphrase on; set {PASSPHRASE_ENV}.")
    first = getpass.getpass(prompt)
    if getpass.getpass("Again: ") != first:
        raise ValidationError("The passphrases did not match.")
    return first


def _with_passphrase(sync, name: str, fn: Callable[[str], T]) -> T:
    """Run ``fn(passphrase)``: from the environment, then storage, then a prompt.

    A stored passphrase that no longer opens the repository (changed on
    another machine) leads to one prompt; success stores the new one.
    """
    cfg = sync.get(name)
    passphrase = os.environ.get(PASSPHRASE_ENV) or sync.stored_passphrase(cfg)
    if passphrase:
        try:
            return fn(passphrase)
        except PassphraseError:
            if not _interactive():
                raise
            warning(f"The saved passphrase no longer opens '{cfg.name}' (changed elsewhere?).")
    elif not _interactive():
        raise ValidationError(
            f"No passphrase saved for '{cfg.name}' and no terminal to ask on; "
            f"set {PASSPHRASE_ENV}."
        )
    return fn(getpass.getpass(f"Passphrase for '{cfg.name}': "))


def _ask(question: str, assume: bool | None) -> bool:
    if assume is not None:
        return assume
    if not _interactive():
        return False
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def _read_token(arg: str, prompt: str) -> str:
    """A token from the argument, stdin ('-'), or a hidden prompt."""
    if arg == "-":
        return sys.stdin.readline().rstrip("\n")
    if arg:
        return arg
    if not _interactive():
        raise ValidationError("No terminal to ask for the token on; pass '-' and pipe it in.")
    return getpass.getpass(prompt)


def _print_report(report) -> None:
    for line in report.lines:
        print(f"  {line}")


_STATE_STYLE = {
    "in-sync": ("synced", dimmed),
    "differs": ("differs", accent),
    "here-only": ("local", bolded),
    "repo-only": ("remote", bolded),
    "skipped": ("skipped", muted),
    "unreadable": ("unreadable", yellowed),
    "not-shareable": ("not shareable", muted),
}


def _show(sync, name: str) -> None:
    from claude_swap import remote as rm

    status = _with_passphrase(sync, name, lambda p: sync.status(name, p))
    cfg = status.remote
    print(f"{bolded(cfg.name)}  {cfg.url}  {muted(f'({cfg.branch})')}")
    if not status.rows:
        print(dimmed("  No setup-token accounts here or in the repository yet."))
        return
    for row in status.rows:
        label, style = _STATE_STYLE.get(row.state, (row.state, str))
        local = f"Account-{row.local_num}" if row.local_num else "-"
        entry = row.entry_id[:8] if row.entry_id else "-"
        line = f"  {style(f'{label:<13}')} {local:<11} {entry:<9} {row.label}"
        if row.note:
            line += f"  {muted(row.note)}"
        print(line)
    counts = {s: sum(r.state == s for r in status.rows) for s in (rm.HERE_ONLY, rm.REPO_ONLY, rm.DIFFERS)}
    hints = []
    if counts[rm.HERE_ONLY]:
        hints.append(f"`cswap remote push {cfg.name}` shares the local accounts")
    if counts[rm.REPO_ONLY]:
        hints.append(f"`cswap remote pull {cfg.name}` adds the remote ones here")
    if counts[rm.DIFFERS]:
        hints.append("push or pull asks which copy to keep for each that differs")
    for hint in hints:
        print(dimmed(f"  {hint}"))


def _differs_question(direction: str) -> Callable:
    def ask(row) -> bool:
        where = row.note or "the repository copy is different"
        if direction == "push":
            q = f"Account-{row.local_num} {row.label} differs ({where}). Replace the repository's copy with this machine's?"
        else:
            q = f"Account-{row.local_num} {row.label} differs ({where}). Replace this machine's token with the repository's?"
        return _ask(q, None)
    return ask


def remote_command(argv: list[str]) -> None:
    from claude_swap import remote as rm
    from claude_swap.cli import _guard_root
    from claude_swap.switcher import ClaudeAccountSwitcher

    parser = argparse.ArgumentParser(
        prog="cswap remote",
        description="Share setup-token accounts between machines through an encrypted git repository.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_EPILOG,
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    sub = parser.add_subparsers(dest="cmd", metavar="COMMAND")

    p = sub.add_parser("add", help="add a repository (an empty one is initialized)")
    p.add_argument("name")
    p.add_argument("url")
    p.add_argument("--branch", default="main")
    login = p.add_mutually_exclusive_group()
    login.add_argument("--ssh-key", metavar="PATH", help="use this SSH key (default: your SSH agent / keys)")
    login.add_argument("--token", nargs="?", const="", metavar="-",
                       help="HTTPS access token: '-' reads it from stdin, nothing asks for it")
    p.add_argument("--username", default="", help="HTTPS username (default: git)")
    p.add_argument("--no-remember", action="store_true", help="ask for the passphrase every time")

    sub.add_parser("list", help="the repositories this machine knows")

    p = sub.add_parser("show", help="fetch and compare")
    p.add_argument("name")

    p = sub.add_parser("push", help="copy accounts from here to the repository")
    p.add_argument("name")
    p.add_argument("accounts", nargs="*", metavar="ACCOUNT")
    p.add_argument("--yes", "-y", action="store_true", help="replace the repository's copy of every account that differs")

    p = sub.add_parser("pull", help="copy accounts from the repository to here")
    p.add_argument("name")
    p.add_argument("entries", nargs="*", metavar="ENTRY")
    p.add_argument("--yes", "-y", action="store_true", help="take the repository's copy of every account that differs")
    p.add_argument("--replace", action="store_true",
                   help="let a pulled account replace the token of a local account with the same email label")

    p = sub.add_parser("add-token", help="put a setup-token in the repository only")
    p.add_argument("name")
    p.add_argument("token", nargs="?", default="", metavar="TOKEN|-")
    p.add_argument("--email", help="label it (default: a placeholder)")
    p.add_argument("--alias")

    p = sub.add_parser(
        "set-token",
        help="replace an account's token in the repository (it need not be on this machine)",
    )
    p.add_argument("name")
    p.add_argument("entry", metavar="ENTRY")
    p.add_argument("token", nargs="?", default="", metavar="TOKEN|-")

    p = sub.add_parser("rm-entry", help="delete an account from the repository")
    p.add_argument("name")
    p.add_argument("entry", metavar="ENTRY")
    p.add_argument("--yes", "-y", action="store_true")

    p = sub.add_parser("skip", help="don't pull this account on this machine")
    p.add_argument("name")
    p.add_argument("entry", metavar="ENTRY")
    p.add_argument("--undo", action="store_true")

    p = sub.add_parser("passwd", help="change the repository's passphrase")
    p.add_argument("name")

    p = sub.add_parser("forget", help="stop remembering the passphrase on this machine")
    p.add_argument("name")

    p = sub.add_parser("set", help="change the URL, branch or git login")
    p.add_argument("name")
    p.add_argument("--url")
    p.add_argument("--branch")
    login = p.add_mutually_exclusive_group()
    login.add_argument("--ssh-key", metavar="PATH")
    login.add_argument("--ssh-default", action="store_true", help="use your SSH agent / default keys")
    login.add_argument("--token", nargs="?", const="", metavar="-")
    p.add_argument("--username")

    p = sub.add_parser("remove", help="forget a repository on this machine (the repository is untouched)")
    p.add_argument("name")
    p.add_argument("--yes", "-y", action="store_true")

    args = parser.parse_args(argv)
    if args.cmd is None:
        parser.print_help()
        return

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        _guard_root(switcher)
        sync = rm.RemoteSync(switcher)
        cmd = args.cmd

        if cmd == "list":
            remotes = sync.remotes()
            if not remotes:
                print(dimmed("No repositories yet. Add one with `cswap remote add NAME URL`."))
                return
            for cfg in remotes:
                login_label = {"default": "ssh (agent/default keys)", "ssh-key": f"ssh key {cfg.ssh_key}",
                               "https-token": "https token"}[cfg.auth]
                remembered = "passphrase saved" if sync.stored_passphrase(cfg) else "passphrase asked"
                print(f"{bolded(cfg.name)}  {cfg.url}  {muted(f'{cfg.branch} · {login_label} · {remembered}')}")
            return

        if cmd == "add":
            token = ""
            auth = "default"
            if args.ssh_key:
                auth = "ssh-key"
            elif args.token is not None:
                auth = "https-token"
                token = _read_secret(args.token, "Access token: ")
            passphrase = _new_passphrase("Passphrase (new, or the repository's): ")
            cfg, created, count = sync.add(
                args.name, args.url, passphrase=passphrase, branch=args.branch, auth=auth,
                ssh_key=args.ssh_key or "", username=args.username, token=token,
                remember=not args.no_remember,
            )
            if created:
                print(f"{accent('Initialized')} '{cfg.name}' as a new claude-swap remote.")
            else:
                print(f"{accent('Added')} '{cfg.name}': {count} shared account{'s' if count != 1 else ''}.")
            print(dimmed(f"See it with `cswap remote show {cfg.name}`."))
            return

        if cmd == "show":
            _show(sync, args.name)
            return

        if cmd == "push":
            confirm = (lambda row: True) if args.yes else _differs_question("push")
            report = _with_passphrase(
                sync, args.name, lambda p: sync.push(args.name, p, args.accounts or None, confirm=confirm))
            _print_report(report)
            return

        if cmd == "pull":
            confirm = (lambda row: True) if args.yes else _differs_question("pull")
            report = _with_passphrase(
                sync, args.name, lambda p: sync.pull(args.name, p, args.entries or None,
                                                     confirm=confirm, replace_conflicts=args.replace))
            _print_report(report)
            return

        if cmd == "set-token":
            token = _read_token(args.token, "New setup-token: ")
            done = _with_passphrase(
                sync, args.name, lambda p: sync.set_token(args.name, p, args.entry, token))
            entry = done.entry
            if not done.changed and not done.local_changed:
                print(dimmed(f"{entry.label} already has that token."))
                return
            if done.changed:
                print(f"{accent('Replaced')} the token of {entry.label} in '{args.name}' "
                      f"{muted(f'(entry {entry.id[:8]})')}.")
            if done.local_changed:
                print(f"{accent('Replaced')} the token of Account-{done.local_num} on this machine.")
            if done.changed:
                print(dimmed("Other machines that have it see it as differs; a pull there takes the new token."))
            return

        if cmd == "add-token":
            token = _read_token(args.token, "Setup-token: ")
            entry = _with_passphrase(
                sync, args.name,
                lambda p: sync.add_token(args.name, p, token, email=args.email, alias=args.alias))
            print(f"{accent('Added')} {entry.label} to '{args.name}' {muted(f'(entry {entry.id[:8]})')}.")
            print(dimmed("Not added on this machine; `cswap remote pull` adds it here if you want it."))
            return

        if cmd == "rm-entry":
            if not _ask(f"Delete '{args.entry}' from the repository for every machine?", True if args.yes else None):
                print(dimmed("Nothing removed."))
                return
            label = _with_passphrase(sync, args.name, lambda p: sync.remove_entry(args.name, p, args.entry))
            print(f"{accent('Removed')} {label} from '{args.name}'. Local accounts are unchanged.")
            return

        if cmd == "skip":
            label = _with_passphrase(
                sync, args.name, lambda p: sync.set_skipped(args.name, p, args.entry, not args.undo))
            if args.undo:
                print(f"{label}: pulled again by `cswap remote pull {args.name}`.")
            else:
                print(f"{label}: marked not on this machine; a pull leaves it alone.")
            return

        if cmd == "passwd":
            cfg = sync.get(args.name)
            if os.environ.get(PASSPHRASE_ENV):
                raise ValidationError(f"Unset {PASSPHRASE_ENV} to change the passphrase.")
            if not _interactive():
                raise ValidationError("Changing the passphrase needs a terminal.")
            old = sync.stored_passphrase(cfg) or getpass.getpass(f"Current passphrase for '{cfg.name}': ")
            new = _new_passphrase("New passphrase: ")
            try:
                n = sync.change_passphrase(args.name, old, new)
            except PassphraseError:
                old = getpass.getpass(f"Current passphrase for '{cfg.name}': ")
                n = sync.change_passphrase(args.name, old, new)
            print(f"{accent('Changed')} the passphrase; re-encrypted {n} account{'s' if n != 1 else ''}.")
            print(dimmed("Other machines are asked for the new one next time."))
            return

        if cmd == "forget":
            cfg = sync.forget_passphrase(args.name)
            print(f"'{cfg.name}': the passphrase will be asked for each time.")
            return

        if cmd == "set":
            token = None
            auth = None
            if args.ssh_key:
                auth = "ssh-key"
            elif args.ssh_default:
                auth = "default"
            elif args.token is not None:
                auth = "https-token"
                token = _read_secret(args.token, "Access token: ")
            cfg = sync.update(args.name, url=args.url, branch=args.branch, auth=auth,
                              ssh_key=args.ssh_key, username=args.username, token=token)
            print(f"{accent('Updated')} '{cfg.name}'.")
            return

        if cmd == "remove":
            if not _ask(f"Forget '{args.name}' on this machine? The repository is not changed.",
                        True if args.yes else None):
                print(dimmed("Nothing removed."))
                return
            cfg = sync.remove(args.name)
            print(f"{accent('Removed')} '{cfg.name}' from this machine.")
            return
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print()
        sys.exit(130)
