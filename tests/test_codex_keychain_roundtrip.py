"""Codex backups through the real macOS Keychain wrapper.

The autouse Keychain fake replaces the ``macos_keychain`` wrappers, so it
cannot see how ``security`` itself prints values back. A Codex ``auth.json``
is pretty-printed JSON, and ``security find-generic-password -w`` prints any
value holding an unprintable byte (its newlines) as bare hex — so a backup
that went in as text came back as hex, and every Codex slot read as "no
credentials" on macOS. These tests drive the real wrappers against a stand-in
``security`` that prints values the way the real one does.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_swap import macos_keychain
from claude_swap.codex_switcher import CodexAccountSwitcher
from claude_swap.models import Platform
from tests import codex_fixtures as cf

PERSONAL = dict(email="user@example.com", account_id="acct-personal", user_id="user-1", plan="plus")
OTHER = dict(email="other@example.com", account_id="acct-other", user_id="user-2", plan="pro")


def _done(returncode: int, stdout: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")


class FakeSecurity:
    """Just enough of ``/usr/bin/security``: items hold raw bytes, and ``-w``
    prints a value with any byte outside printable ASCII as bare hex."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], bytes] = {}

    def __call__(self, args, input=None, **kwargs):
        if args[0] != macos_keychain._SECURITY:
            return _done(1)
        if args[1:] == ["-i"]:
            args = [args[0], *shlex.split(input)]
        cmd, opts = args[1], args[2:]
        if cmd not in ("add-generic-password", "find-generic-password", "delete-generic-password"):
            return _done(0)
        key = (opts[opts.index("-s") + 1], opts[opts.index("-a") + 1])
        if cmd == "add-generic-password":
            self.items[key] = bytes.fromhex(opts[opts.index("-X") + 1])
            return _done(0)
        if cmd == "delete-generic-password":
            return _done(0 if self.items.pop(key, None) is not None else 44)
        if key not in self.items:
            return _done(44)
        if "-w" not in opts:
            return _done(0)
        data = self.items[key]
        printable = all(0x20 <= b <= 0x7E for b in data)
        return _done(0, (data.decode() if printable else data.hex()) + "\n")


@pytest.mark.no_keychain_fake
def test_a_pretty_auth_json_backup_reads_back_as_written(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    security = FakeSecurity()
    monkeypatch.setattr(macos_keychain.subprocess, "run", security)
    s = CodexAccountSwitcher()
    s.platform = Platform.MACOS

    text = cf.auth_json(**PERSONAL)
    assert "\n" in text  # Codex writes auth.json pretty-printed
    (codex_home / "auth.json").write_text(text, encoding="utf-8")
    s.add_account()

    assert ("claude-swap-codex", "account-1-user@example.com") in security.items
    assert s._read_account_credentials("1", PERSONAL["email"]) == text

    # The symptom: switching back to a stored slot was refused as "not a
    # ChatGPT login" because its backup came back as hex.
    other = cf.auth_json(**OTHER)
    (codex_home / "auth.json").write_text(other, encoding="utf-8")
    s.add_account()
    s.switch_to("1")
    assert (codex_home / "auth.json").read_text(encoding="utf-8") == text
    assert s._read_account_credentials("2", OTHER["email"]) == other
