"""cswap codex: store, list, switch and remove Codex CLI logins (fake JWTs, tmp dirs only)."""

from __future__ import annotations

import base64
import functools
import json
import re
import stat
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from claude_swap import cli, codex_accounts, codex_usage, paths
from claude_swap.exceptions import LockError, SwitchError
from claude_swap.locking import FileLock

fcntl = pytest.importorskip("fcntl")  # POSIX: file modes and flock


def _jwt(claims: dict) -> str:
    seg = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"e30.{seg}.fake-signature"


def _plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


@pytest.fixture
def codex_home(temp_home, monkeypatch):
    home = temp_home / "codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


@pytest.fixture
def usage_calls(monkeypatch):
    calls = []
    resets = (datetime.now(timezone.utc) + timedelta(hours=3, minutes=37, seconds=30)).isoformat()

    def fake(access_token, account_id, timeout=10):
        calls.append((access_token, account_id))
        return [codex_usage.Window("5h", 30.0, resets), codex_usage.Window("weekly", 12.0, None)]

    monkeypatch.setattr(codex_usage, "fetch_usage", fake)
    return calls


def _login(codex_home, email, account_id, refresh="r1"):
    auth = {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": _jwt({"email": email}),
            "access_token": f"at-{account_id}",
            "refresh_token": refresh,
            "account_id": account_id,
        },
        "last_refresh": "2026-11-10T00:00:00Z",
    }
    text = json.dumps(auth)
    (codex_home / "auth.json").write_text(text)
    return text


def _slot(num):
    return paths.get_backup_root() / "codex" / f"auth-{num}.json"


def test_add_then_list_shows_active_with_usage(codex_home, usage_calls):
    _login(codex_home, "a@example.com", "acct-a")
    assert codex_accounts.add() == ("1", "a@example.com", True)
    assert codex_accounts.add() == ("1", "a@example.com", False)  # same account_id: refresh

    store = paths.get_backup_root() / "codex"
    assert stat.S_IMODE(store.stat().st_mode) == 0o700
    for f in (store / "accounts.json", _slot("1")):
        assert stat.S_IMODE(f.stat().st_mode) == 0o600

    lines = [_plain(line) for line in codex_accounts.list_lines()]
    assert lines[0] == "OpenAI accounts:"
    assert lines[1] == "  1: a@example.com (active)"
    assert re.fullmatch(r"     ├ 5h:      30%   resets .{12}  in 3h 37m", lines[2])
    assert lines[3] == "     └ weekly:  12%"
    assert usage_calls == [("at-acct-a", "acct-a")]
    assert not any("at-acct" in line or "r1" in line for line in lines)


def test_switch_back_saves_live_copy_first_then_installs_target(codex_home, usage_calls):
    a_text = _login(codex_home, "a@example.com", "acct-a")
    codex_accounts.add()
    _login(codex_home, "b@example.com", "acct-b")
    assert codex_accounts.add() == ("2", "b@example.com", True)
    rotated = _login(codex_home, "b@example.com", "acct-b", refresh="r2-rotated")  # Codex refreshed

    assert codex_accounts.switch("1") == ("1", "a@example.com")

    assert _slot("2").read_text() == rotated
    live = codex_home / "auth.json"
    assert live.read_text() == a_text
    assert stat.S_IMODE(live.stat().st_mode) == 0o600
    lines = [_plain(line) for line in codex_accounts.list_lines()]
    assert "  1: a@example.com (active)" in lines
    assert "  2: b@example.com" in lines
    assert ("at-acct-b", "acct-b") in usage_calls  # inactive row read from the stored copy

    assert codex_accounts.switch("b@example.com") == ("2", "b@example.com")
    assert live.read_text() == rotated


def test_switch_refuses_unsaved_live_login(codex_home):
    _login(codex_home, "a@example.com", "acct-a")
    codex_accounts.add()
    unsaved = _login(codex_home, "c@example.com", "acct-c")

    with pytest.raises(SwitchError, match="cswap codex add"):
        codex_accounts.switch("1")
    assert (codex_home / "auth.json").read_text() == unsaved


def test_switch_waits_on_codex_commit_lock(codex_home, monkeypatch):
    _login(codex_home, "a@example.com", "acct-a")
    codex_accounts.add()
    _login(codex_home, "b@example.com", "acct-b")
    codex_accounts.add()
    before = (codex_home / "auth.json").read_text()
    monkeypatch.setattr(codex_accounts, "FileLock", functools.partial(FileLock, timeout=0.2))

    with open(codex_home / "auth.json.lock", "a") as held:  # a Codex save in progress
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(LockError):
            codex_accounts.switch("1")
    assert (codex_home / "auth.json").read_text() == before


def test_remove(codex_home, usage_calls):
    _login(codex_home, "a@example.com", "acct-a")
    codex_accounts.add()
    _login(codex_home, "b@example.com", "acct-b")
    codex_accounts.add()

    assert codex_accounts.remove("b@example.com") == ("2", "b@example.com")
    assert not _slot("2").exists()
    assert not any("b@example.com" in line for line in codex_accounts.list_lines())
    codex_accounts.remove("1")
    assert codex_accounts.list_lines() == []


def test_email_from_jwt():
    def auth(claims):
        return {"tokens": {"id_token": _jwt(claims), "account_id": "acct"}}

    assert codex_accounts.identity(auth({"email": "e@example.com"})) == ("e@example.com", "acct")
    profile = {"https://api.openai.com/profile": {"email": "p@example.com"}}
    assert codex_accounts.identity(auth(profile)) == ("p@example.com", "acct")
    assert codex_accounts.identity({"tokens": {"id_token": "not-a-jwt"}}) == ("", "")


def test_cli_codex_and_list_print_openai_section(codex_home, usage_calls, capsys):
    _login(codex_home, "a@example.com", "acct-a")
    with patch("claude_swap.cli.ClaudeAccountSwitcher"), \
         patch("os.geteuid", return_value=1000, create=True), \
         patch("claude_swap.update_check.check_for_update", return_value=None):
        for argv in (["cswap", "codex", "add"], ["cswap", "codex"], ["cswap", "list"]):
            with patch.object(sys, "argv", argv):
                cli.main()
    out = _plain(capsys.readouterr().out)
    assert "Added Codex account 1: a@example.com" in out
    assert out.count("OpenAI accounts:") == 2
    assert out.count("  1: a@example.com (active)") == 2
