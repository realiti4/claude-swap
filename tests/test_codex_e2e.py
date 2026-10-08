"""End to end: ``cswap codex …`` through the real CLI entry point.

Two Codex accounts on a machine that has never logged in to Codex, driven
only through ``cli.main()`` in a temp HOME:

- sign both in with ``add --login`` (a fake ``codex`` binary);
- switch 1 → 2 → 1 while Codex rotates the live login in between;
- read ``list`` / ``status`` JSON;
- launch a session (``run``);
- export, purge (Claude's store must survive), and import on a fresh machine.

``codex``, exec and the network are faked; conftest keeps the real
``~/.codex``, store and Keychain out of reach.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from claude_swap import cli, codex_auth, process_detection
from claude_swap.codex_switcher import CodexAccountSwitcher
from claude_swap.oauth import RefreshOutcome
from claude_swap.paths import get_backup_root
from tests import codex_fixtures as cf

CODEX = "/opt/bin/codex"
FILE_STORE = 'cli_auth_credentials_store="file"'
A = dict(email="a@example.com", account_id="acct-a", user_id="user-a", plan="plus")
B = dict(email="b@example.com", account_id="acct-b", user_id="user-b", plan="pro")
PCT = {"acct-a": 10.0, "acct-b": 60.0}  # 5h used % per account; 7d = +5


def rotated(text: str, tag: str) -> str:
    """``text`` after a token refresh: same account, new access and refresh
    tokens and ``last_refresh``, pretty-printed as Codex's file store is."""
    data = json.loads(text)
    tokens = data["tokens"]
    tokens["access_token"] = cf.access_token(account_id=tokens["account_id"], nonce=tag)
    tokens["refresh_token"] = f"rt-{tag}"
    data["last_refresh"] = codex_auth.now_rfc3339()
    return json.dumps(data, indent=2)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """``codex`` on PATH, exec, usage and refresh, all recorded.

    ``codex login`` writes the next of ``logins`` into the CODEX_HOME it is
    given. Any other ``codex`` invocation is a session launch: an exec on
    POSIX, a child process on Windows (``SessionManager._exec``). A real exec
    never returns, hence the SystemExit.
    """
    state = SimpleNamespace(logins=[], login_calls=[], execs=[], usage=[], refreshes=[])
    real_which = shutil.which

    def which(name, *args, **kwargs):
        return CODEX if name == "codex" else real_which(name, *args, **kwargs)

    def run(cmd, env=None, **kwargs):
        assert cmd[0] == CODEX, f"unexpected subprocess: {cmd}"
        if cmd[1] == "login":
            home = Path(env["CODEX_HOME"])
            state.login_calls.append(SimpleNamespace(cmd=list(cmd), home=home))
            (home / "auth.json").write_text(state.logins.pop(0), encoding="utf-8")
        else:
            state.execs.append(SimpleNamespace(argv=list(cmd), env=dict(env)))
        return subprocess.CompletedProcess(cmd, 0)

    def execvpe(file, argv, env):
        state.execs.append(SimpleNamespace(argv=list(argv), env=dict(env)))
        raise SystemExit(0)

    def request_usage(creds, base_url=None, timeout_s=10.0):
        account = json.loads(creds)["tokens"]["account_id"]
        state.usage.append(account)
        now = int(time.time())
        pct = PCT[account]
        return cf.wham_usage((pct, 18000, now + 3600), (pct + 5, 604800, now + 86400))

    def try_refresh(creds, timeout_s=10.0):
        state.refreshes.append(creds)
        return RefreshOutcome(rotated(creds, "refreshed"), None)

    monkeypatch.setattr(shutil, "which", which)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(os, "execvpe", execvpe)
    monkeypatch.setattr(codex_auth, "request_usage", request_usage)
    monkeypatch.setattr(codex_auth, "try_refresh", try_refresh)
    # The session's run record stamps its pid's start time with `ps`.
    monkeypatch.setattr(process_detection, "proc_start_stamp", lambda pid: "STAMP")
    return state


@pytest.fixture
def cswap(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    """``cswap <argv>`` → ``(exit code, stdout)``; exit 0 when it returns."""
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr("claude_swap.update_check.check_for_update", lambda v: None)

    def invoke(*argv: str) -> tuple[int, str]:
        monkeypatch.setattr(sys, "argv", ["cswap", *argv])
        capsys.readouterr()
        try:
            cli.main()
            code = 0
        except SystemExit as e:
            code = e.code or 0
        return code, capsys.readouterr().out

    return invoke


def _backup(num: str, email: str) -> str:
    """A slot's stored login, read the way a fresh ``cswap codex`` would."""
    return CodexAccountSwitcher()._read_account_credentials(num, email)


def _files(root: Path, *, skip: Path) -> dict[str, bytes]:
    """Every file under ``root`` outside ``skip``, logs aside (the shared
    logger writes to whichever store the last command built)."""
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and not p.name.startswith("claude-swap.log") and skip not in p.parents
    }


def test_codex_accounts_end_to_end(
    codex_home: Path, fake: SimpleNamespace, cswap, block_real_keychain, monkeypatch, tmp_path
):
    live = codex_home / "auth.json"
    live.unlink()  # Codex installed, never logged in
    store = get_backup_root() / "codex"

    # A Claude account in the same root, which nothing below may touch.
    assert cswap("add-token", "sk-ant-oat01-e2e", "--email", "claude@example.com")[0] == 0

    # -- add --login ×2: each login runs in a throwaway home ---------------------
    login_a, login_b = cf.auth_json(**A), cf.auth_json(**B)
    fake.logins = [login_a, login_b]
    assert cswap("codex", "add", "--login")[0] == 0
    assert cswap("codex", "add", "--login", "--device-auth")[0] == 0

    first, second = fake.login_calls
    assert first.cmd == [CODEX, "login", "-c", FILE_STORE]
    assert second.cmd == [CODEX, "login", "--device-auth", "-c", FILE_STORE]
    assert first.home.parent == second.home.parent == store
    assert not first.home.exists() and not second.home.exists()
    assert not live.exists()  # the live login is never involved
    assert _backup("1", "a@example.com") == login_a
    assert _backup("2", "b@example.com") == login_b

    # -- switch 1 → 2 → 1, Codex rotating the live login in between ---------------
    code, out = cswap("codex", "switch", "1")
    assert code == 0 and "codex app-server daemon restart" in out
    assert live.read_text(encoding="utf-8") == login_a

    a2 = rotated(login_a, "a2")
    live.write_text(a2, encoding="utf-8")  # Codex refreshed account 1
    assert cswap("codex", "switch", "2")[0] == 0
    assert _backup("1", "a@example.com") == a2  # the rotation went to slot 1
    assert live.read_text(encoding="utf-8") == login_b

    b2 = rotated(login_b, "b2")
    live.write_text(b2, encoding="utf-8")  # Codex refreshed account 2
    assert cswap("codex", "switch", "1")[0] == 0
    assert _backup("2", "b@example.com") == b2
    assert _backup("1", "a@example.com") == a2
    assert live.read_text(encoding="utf-8") == a2
    assert fake.refreshes == []  # tokens were fresh: nothing was refreshed

    # -- list / status JSON ----------------------------------------------------
    code, out = cswap("codex", "list", "--json")
    assert code == 0
    listing = json.loads(out)
    assert listing["provider"] == "codex"
    assert listing["activeAccountNumber"] == 1
    rows = {row["number"]: row for row in listing["accounts"]}
    assert [rows[1]["email"], rows[2]["email"]] == ["a@example.com", "b@example.com"]
    assert rows[1]["active"] is True and rows[2]["active"] is False
    for num, account in ((1, "acct-a"), (2, "acct-b")):
        assert rows[num]["usageStatus"] == "ok"
        assert rows[num]["usage"]["fiveHour"]["pct"] == PCT[account]
        assert rows[num]["usage"]["sevenDay"]["pct"] == PCT[account] + 5
        assert rows[num]["isOrganization"] is False  # personal plans
    assert sorted(set(fake.usage)) == ["acct-a", "acct-b"]
    assert fake.refreshes == []  # the live login is Codex's to refresh

    code, out = cswap("codex", "status", "--json")
    status = json.loads(out)
    assert code == 0 and status["provider"] == "codex"
    assert status["active"]["number"] == 1
    assert status["active"]["email"] == "a@example.com"

    # -- run: account 2 in a session profile, the default login untouched ----------
    monkeypatch.setenv("CODEX_API_KEY", "sk-would-override-the-session")
    assert cswap("codex", "run", "2", "--", "resume", "--last")[0] == 0

    (launch,) = fake.execs
    profile = store / "sessions" / "2-b_example.com"
    assert launch.argv == [CODEX, "-c", FILE_STORE, "resume", "--last"]
    assert launch.env["CODEX_HOME"] == str(profile)
    assert "CODEX_API_KEY" not in launch.env
    assert fake.refreshes == [b2]  # one pre-launch refresh, of slot 2 only
    b3 = _backup("2", "b@example.com")
    assert json.loads(b3)["tokens"]["refresh_token"] == "rt-refreshed"  # persisted
    assert json.loads((profile / "auth.json").read_text(encoding="utf-8")) == json.loads(b3)
    assert live.read_text(encoding="utf-8") == a2
    # The session exits: its recorded pid (this process) is gone.
    monkeypatch.setattr(process_detection, "is_pid_alive", lambda pid: pid != os.getpid())

    # -- export, then purge: Codex's store goes, Claude's stays -------------------
    exported = tmp_path / "accounts.cswap"
    assert cswap("codex", "export", str(exported))[0] == 0
    assert json.loads(exported.read_text(encoding="utf-8"))["provider"] == "codex"

    root = store.parent
    claude_files = _files(root, skip=store)
    assert "sequence.json" in claude_files
    claude_items = {
        k: v for k, v in block_real_keychain.data.items() if k[0] != "claude-swap-codex"
    }
    monkeypatch.setattr("builtins.input", lambda *_: "y")
    assert cswap("codex", "purge")[0] == 0

    assert not store.exists()
    assert _files(root, skip=store) == claude_files
    assert block_real_keychain.data == claude_items
    assert live.read_text(encoding="utf-8") == a2  # purge never logs Codex out

    # -- import on a fresh machine ----------------------------------------------
    machine = tmp_path / "new-machine"
    (machine / ".codex").mkdir(parents=True)
    with (
        patch.dict(os.environ, {"HOME": str(machine), "USERPROFILE": str(machine)}),
        patch("pathlib.Path.home", return_value=machine),
    ):
        assert get_backup_root() != root
        assert cswap("codex", "import", str(exported))[0] == 0
        # The export carries each login as JSON; the import re-serializes it.
        assert json.loads(_backup("1", "a@example.com")) == json.loads(a2)
        assert json.loads(_backup("2", "b@example.com")) == json.loads(b3)
        assert cswap("codex", "switch", "2")[0] == 0
        moved = (machine / ".codex" / "auth.json").read_text(encoding="utf-8")
        assert json.loads(moved) == json.loads(b3)
        assert not (get_backup_root() / "sequence.json").exists()  # no Claude roster
