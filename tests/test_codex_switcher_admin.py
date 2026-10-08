"""Codex admin paths: remove / disable / alias / swap / move, purge, and the
running-process scan.

remove … move are the shared roster code; these tests pin that they work on
Codex slots and stay inside ``<root>/codex``: no Claude config, credential
file or Keychain item is ever created or deleted. Purge is per provider:
Claude's leaves ``<root>/codex`` alone, Codex's removes only that.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_swap import codex_auth, process_detection
from claude_swap.codex_switcher import CodexAccountSwitcher
from claude_swap.exceptions import SessionError
from claude_swap.models import Platform
from claude_swap.paths import (
    get_credentials_path,
    get_global_config_path,
    get_legacy_backup_root,
)

# The real scan: conftest stubs ``process_detection.list_codex_processes``
# for every test, and this binding is taken at import, before that stub.
from claude_swap.process_detection import list_codex_processes
from claude_swap.switcher import ClaudeAccountSwitcher
from tests import codex_fixtures as cf

PERSONAL = dict(email="user@example.com", account_id="acct-personal", user_id="user-1", plan="plus")
TEAM = dict(email="user@example.com", account_id="acct-team", user_id="user-1", plan="team")
OTHER = dict(email="other@example.com", account_id="acct-other", user_id="user-2", plan="pro")
USER, OTHER_EMAIL = "user@example.com", "other@example.com"
CLAUDE_EMAIL = "claude@example.com"
CLAUDE_CREDS = json.dumps({"claudeAiOauth": {
    "accessToken": "at-c", "refreshToken": "rt-c", "expiresAt": 4_102_444_800_000,
}})


def _switcher(platform: Platform = Platform.LINUX) -> CodexAccountSwitcher:
    s = CodexAccountSwitcher()
    s.platform = platform
    return s


def _add(s: CodexAccountSwitcher, codex_home: Path, **kw) -> str:
    """Make ``kw`` the live login and capture it; returns the auth.json text."""
    text = cf.auth_json(**kw)
    (codex_home / "auth.json").write_text(text, encoding="utf-8")
    s.add_account()
    return text


def _live(codex_home: Path) -> str:
    return (codex_home / "auth.json").read_text(encoding="utf-8")


def _roster(s) -> dict:
    return s._get_sequence_data()


def _assert_no_claude_files(s: CodexAccountSwitcher) -> None:
    assert not get_global_config_path().exists()
    assert not get_credentials_path().exists()
    assert not (s.root_dir / "sequence.json").exists()


def _seed_claude_account(platform: Platform = Platform.LINUX) -> ClaudeAccountSwitcher:
    """A Claude switcher holding one account (slot 1) in the shared root."""
    c = ClaudeAccountSwitcher()
    c.platform = platform
    c._setup_directories()
    c._init_sequence_file()
    c._write_account_credentials("1", CLAUDE_EMAIL, CLAUDE_CREDS)
    c._write_account_config("1", CLAUDE_EMAIL, json.dumps(
        {"oauthAccount": {"emailAddress": CLAUDE_EMAIL, "organizationUuid": ""}}
    ))
    data = c._get_sequence_data()
    data["accounts"]["1"] = {
        "email": CLAUDE_EMAIL, "uuid": "u-c", "organizationUuid": "",
        "organizationName": "", "added": "2024-01-01T00:00:00Z",
    }
    data["sequence"] = [1]
    c._write_json(c.sequence_file, data)
    return c


def _files(root: Path, *, skip: Path | None = None) -> dict[str, bytes]:
    """Every file under ``root`` (logs aside: the shared logger appends to
    whichever switcher was built last) with its bytes."""
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file()
        and not p.name.startswith("claude-swap.log")
        and (skip is None or skip not in p.parents)
    }


@pytest.fixture
def three(codex_home: Path) -> tuple[CodexAccountSwitcher, dict[str, str]]:
    """Slots 1 personal, 2 team (same email), 3 other — 3 is live/active."""
    s = _switcher()
    texts = {n: _add(s, codex_home, **kw) for n, kw in (("1", PERSONAL), ("2", TEAM), ("3", OTHER))}
    return s, texts


@pytest.fixture
def no_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    now = int(time.time())
    monkeypatch.setattr(codex_auth, "request_usage", lambda *a, **k: cf.wham_usage(
        (10.0, 18000, now + 3600), (20.0, 604800, now + 86400)
    ))


class TestAdmin:
    def test_alias_disable_enable(self, three, codex_home: Path):
        s, texts = three
        assert s.set_alias("2", "work") == ("2", "work")
        assert _roster(s)["accounts"]["2"]["alias"] == "work"
        s.set_account_disabled("work", True)
        assert s.disabled_account_numbers() == ["2"]
        assert "2" not in s.switchable_account_numbers()
        s.set_account_disabled("2", False)
        assert s.disabled_account_numbers() == []
        assert s.unset_alias("work") == "2"
        assert "alias" not in _roster(s)["accounts"]["2"]
        assert _live(codex_home) == texts["3"]
        _assert_no_claude_files(s)

    def test_swap_moves_backups_configs_and_the_active_slot(self, three, codex_home: Path):
        s, texts = three
        assert s.swap_accounts("1", "3") == ("1", "3")
        data = _roster(s)
        assert data["accounts"]["1"]["organizationUuid"] == "acct-other"
        assert data["accounts"]["3"]["organizationUuid"] == "acct-personal"
        assert data["activeAccountNumber"] == 1
        assert s._read_account_credentials("1", OTHER_EMAIL) == texts["3"]
        assert s._read_account_credentials("3", USER) == texts["1"]
        assert s._read_account_credentials("1", USER) == ""  # old key cleared
        config = json.loads(s._read_account_config("3", USER))["oauthAccount"]
        assert config["organizationUuid"] == "acct-personal"
        assert s.current_account_number() == "1"  # the live login followed

        # Same email on both slots: the overlapping-key staging path.
        s.swap_accounts("2", "3")
        assert s._read_account_credentials("2", USER) == texts["1"]
        assert s._read_account_credentials("3", USER) == texts["2"]

        # Still switchable after the renumbering.
        s.switch_to("2")
        assert codex_auth.identity(_live(codex_home))["organizationUuid"] == "acct-personal"
        _assert_no_claude_files(s)

    def test_move_relocates_or_trades_places(self, three, codex_home: Path):
        s, texts = three
        assert s.move_account("2", "7") == ("2", "7", False)
        data = _roster(s)
        assert "2" not in data["accounts"] and data["sequence"] == [1, 3, 7]
        assert s._read_account_credentials("7", USER) == texts["2"]
        assert s._read_account_credentials("2", USER) == ""
        assert s.move_account("7", "3") == ("7", "3", True)
        assert s._read_account_credentials("3", USER) == texts["2"]
        assert s._read_account_credentials("7", OTHER_EMAIL) == texts["3"]
        assert _roster(s)["activeAccountNumber"] == 7
        assert _live(codex_home) == texts["3"]
        _assert_no_claude_files(s)

    def test_remove_never_touches_the_live_login(self, three, codex_home: Path):
        s, texts = three
        s.remove_account("1", assume_yes=True)
        assert "1" not in _roster(s)["accounts"]
        assert s._read_account_credentials("1", USER) == ""
        assert not list(s.configs_dir.glob(".claude-config-1-*"))
        s.remove_account("3", assume_yes=True)  # the active one
        assert _live(codex_home) == texts["3"]
        assert set(_roster(s)["accounts"]) == {"2"}
        _assert_no_claude_files(s)

    def test_on_macos_only_the_codex_keychain_service_is_used(
        self, codex_home: Path, block_real_keychain
    ):
        claude_items = {
            ("claude-swap", f"account-1-{USER}"): "claude backup",
            ("Claude Code-credentials", "me"): "claude live",
        }
        block_real_keychain.data.update(claude_items)
        s = _switcher(Platform.MACOS)
        for kw in (PERSONAL, TEAM, OTHER):
            _add(s, codex_home, **kw)
        assert ("claude-swap-codex", f"account-1-{USER}") in block_real_keychain.data

        s.set_alias("1", "home")
        s.set_account_disabled("2", True)
        s.swap_accounts("home", "3")
        s.move_account("2", "5")
        s.remove_account("5", assume_yes=True)

        assert {k: v for k, v in block_real_keychain.data.items()
                if k[0] != "claude-swap-codex"} == claude_items
        assert ("claude-swap-codex", f"account-5-{USER}") not in block_real_keychain.data
        _assert_no_claude_files(s)

    def test_session_profiles_never_reach_claudes_keychain_namespace(
        self, three, monkeypatch: pytest.MonkeyPatch
    ):
        """A Claude session profile's Keychain entry is named from its dir
        (``Claude Code-credentials-<hash>``); a Codex profile has none."""
        s, _ = three
        calls: list[Path] = []
        monkeypatch.setattr("claude_swap.session.delete_macos_keychain_entry", calls.append)
        for num, email in (("1", USER), ("2", USER)):
            s._session_dir(num, email).mkdir(parents=True)

        s.swap_accounts("2", "3")  # backup writes invalidate profiles
        s.remove_account("1", assume_yes=True)  # deletes its profile
        assert not s._session_dir("1", USER).exists()
        assert calls == []


class TestPurge:
    def test_claude_purge_keeps_the_codex_store(
        self, codex_home: Path, block_real_keychain, monkeypatch, capsys
    ):
        codex = _switcher(Platform.MACOS)
        _add(codex, codex_home, **PERSONAL)
        claude = _seed_claude_account(Platform.MACOS)
        kept = _files(codex.backup_dir)
        assert kept and ("claude-swap", f"account-1-{CLAUDE_EMAIL}") in block_real_keychain.data
        monkeypatch.setattr("builtins.input", lambda *_: "y")

        claude.purge()

        assert _files(codex.backup_dir) == kept
        assert list(claude.root_dir.iterdir()) == [codex.backup_dir]
        assert set(block_real_keychain.data) == {("claude-swap-codex", f"account-1-{USER}")}
        out = capsys.readouterr().out
        assert "cswap codex purge" in out
        assert f"Directory: {claude.root_dir}" in out

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
    def test_claude_purge_of_a_symlinked_root_is_unchanged(
        self, temp_home: Path, monkeypatch
    ):
        """No Codex store: exactly the old ``rmtree(root)``, which refuses a
        symlinked root. Only the credential step before it (unlinking each
        slot's ``.enc``, unchanged) has touched the target."""
        from claude_swap.paths import get_backup_root

        target = temp_home / "elsewhere"
        target.mkdir()
        root = get_backup_root()
        root.parent.mkdir(parents=True, exist_ok=True)
        root.symlink_to(target, target_is_directory=True)
        claude = _seed_claude_account()
        before = _files(target)
        assert "sequence.json" in before
        monkeypatch.setattr("builtins.input", lambda *_: "y")

        with pytest.raises(OSError):
            claude.purge()

        assert _files(target) == {
            k: v for k, v in before.items() if not k.startswith("credentials/")
        }
        assert root.is_symlink()

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
    def test_claude_purge_never_follows_a_link_out_of_the_root(
        self, codex_home: Path, monkeypatch
    ):
        codex = _switcher()
        _add(codex, codex_home, **PERSONAL)
        claude = _seed_claude_account()
        outside = codex_home.parent / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("mine", encoding="utf-8")
        (claude.root_dir / "linked").symlink_to(outside, target_is_directory=True)
        monkeypatch.setattr("builtins.input", lambda *_: "y")

        claude.purge()

        assert not (claude.root_dir / "linked").is_symlink()
        assert (outside / "keep.txt").read_text(encoding="utf-8") == "mine"
        assert list(claude.root_dir.iterdir()) == [codex.backup_dir]

    def test_a_dangling_codex_link_is_kept_and_reported_alike(
        self, temp_home: Path, monkeypatch, capsys
    ):
        claude = _seed_claude_account()
        codex_dir = claude.root_dir / "codex"
        codex_dir.symlink_to(temp_home / "gone", target_is_directory=True)
        monkeypatch.setattr("builtins.input", lambda *_: "y")

        claude.purge()

        out = capsys.readouterr().out
        assert "cswap codex purge" in out and "(kept codex/)" in out
        assert codex_dir.is_symlink()

    def test_codex_purge_removes_only_the_codex_store(
        self, codex_home: Path, block_real_keychain, monkeypatch, capsys
    ):
        claude = _seed_claude_account(Platform.MACOS)
        block_real_keychain.data[("Claude Code-credentials", "me")] = "claude live"
        claude_items = dict(block_real_keychain.data)
        codex = _switcher(Platform.MACOS)
        _add(codex, codex_home, **PERSONAL)
        team = _add(codex, codex_home, **TEAM)
        codex._write_account_credentials("1", USER, cf.auth_json(**PERSONAL, refresh_token="rt-2"))
        assert ("claude-swap-codex", f"account-1-{USER}.prev") in block_real_keychain.data
        codex._session_dir("1", USER).mkdir(parents=True)
        claude_files = _files(claude.root_dir, skip=codex.backup_dir)
        monkeypatch.setattr("builtins.input", lambda *_: "y")
        capsys.readouterr()

        codex.purge()

        assert not codex.backup_dir.exists()
        assert _files(claude.root_dir) == claude_files
        assert block_real_keychain.data == claude_items
        assert _live(codex_home) == team
        out = capsys.readouterr().out
        assert "ALL claude-swap Codex data" in out
        assert "current Codex login" in out

    def test_codex_purge_never_touches_the_legacy_dir_or_keyring(
        self, codex_home: Path, monkeypatch, capsys
    ):
        """The pre-XDG ``~/.claude-swap-backup`` and the old ``claude-code``
        keyring entries are Claude's; Codex never had legacy data."""
        import keyring  # conftest's in-memory fake

        monkeypatch.setattr(Platform, "detect", staticmethod(lambda: Platform.LINUX))
        codex = _switcher(Platform.WINDOWS)  # the keyring-sweep branch
        _add(codex, codex_home, **PERSONAL)
        legacy = get_legacy_backup_root()
        assert legacy != codex.root_dir
        legacy.mkdir()
        (legacy / "keep.txt").write_text("claude", encoding="utf-8")
        keyring.set_password("claude-code", f"account-1-{USER}", "legacy claude")
        monkeypatch.setattr("builtins.input", lambda *_: "y")

        codex.purge()

        assert (legacy / "keep.txt").read_text(encoding="utf-8") == "claude"
        assert keyring.get_password("claude-code", f"account-1-{USER}") == "legacy claude"
        assert not codex.backup_dir.exists() and codex.root_dir.exists()
        assert "Legacy" not in capsys.readouterr().out

    def test_codex_purge_refuses_while_a_session_is_live(
        self, codex_home: Path, monkeypatch
    ):
        codex = _switcher()
        _add(codex, codex_home, **PERSONAL)
        codex._session_dir("1", USER).mkdir(parents=True)
        monkeypatch.setattr(
            codex, "_scan_live_sessions", lambda d: ([SimpleNamespace(pid=4242)], 0)
        )
        monkeypatch.setattr(
            "builtins.input", lambda *_: pytest.fail("prompt must not be reached")
        )
        with pytest.raises(SessionError, match="4242"):
            codex.purge()
        assert codex._read_account_credentials("1", USER)


PS_LINES = [
    (101, "codex", "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/"
                   "Contents/MacOS/codex app-server --listen stdio://"),
    (102, "codex", "/Users/me/.vscode/extensions/openai.chatgpt-26.9-darwin-arm64/bin/"
                   "macos-aarch64/codex -c features.code_mode_host=true app-server"),
    (103, "codex-code-mode-", "/Users/me/.codex/plugins/codex-cli/bin/codex-code-mode-host"),
    (104, "codex-code-mode", "/usr/lib/codex/codex-code-mode-host"),  # Linux: 15 chars
    (105, "node", "node /Users/me/.local/bin/codex resume"),  # the npm wrapper
    (106, "codex", "/Users/me/.local/lib/node_modules/@openai/codex/vendor/bin/codex resume"),
    (107, "Codex Computer U", "/Users/me/.codex/computer-use/Codex Computer Use.app/x"),
    (108, "codex", "codex"),
    (109, "zsh", "-zsh"),
]


def _ps_output(lines=PS_LINES) -> str:
    # ps pads each column; ucomm is 16 wide on macOS.
    return "".join(f"{pid:>5} {name:<16} {args}\n" for pid, name, args in lines)


@pytest.fixture
def fake_ps(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """``ps`` answering ``state.stdout``; records each argv."""
    state = SimpleNamespace(stdout=_ps_output(), rc=0, exc=None, calls=[])

    real_run = subprocess.run

    def run(cmd, **kwargs):
        if cmd[0] != "ps":
            return real_run(cmd, **kwargs)
        state.calls.append(list(cmd))
        if state.exc is not None:
            raise state.exc
        return subprocess.CompletedProcess(cmd, state.rc, stdout=state.stdout, stderr="")

    monkeypatch.setattr(process_detection.subprocess, "run", run)
    return state


class TestRunningInstances:
    def test_scan_matches_codex_executables_only(self, fake_ps):
        procs = list_codex_processes()
        assert [(p.pid, p.entrypoint) for p in procs] == [
            (101, "codex app-server"),
            (102, "codex app-server"),
            (106, "codex"),
            (108, "codex"),
        ]
        assert all(p.cwd == "" for p in procs)
        (cmd,) = fake_ps.calls
        assert cmd[0] == "ps" and "-A" in cmd and "pid=,ucomm=,args=" in cmd

    @pytest.mark.parametrize("failure", ["oserror", "timeout", "exit"])
    def test_an_unavailable_ps_finds_nothing(self, fake_ps, failure):
        if failure == "oserror":
            fake_ps.exc = OSError("no ps")
        elif failure == "timeout":
            fake_ps.exc = subprocess.TimeoutExpired("ps", 5)
        else:
            fake_ps.rc = 1
        assert list_codex_processes() == []

    def test_windows_finds_nothing(self, fake_ps, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        assert list_codex_processes() == []
        assert fake_ps.calls == []

    def test_codex_switcher_reports_the_scan_in_the_base_shape(
        self, codex_home: Path, fake_ps, monkeypatch
    ):
        monkeypatch.setattr(process_detection, "list_codex_processes", list_codex_processes)
        sessions, ide = _switcher()._running_instances()
        assert [p.pid for p in sessions] == [101, 102, 106, 108] and ide == []

    def test_list_shows_the_running_codex_processes(
        self, codex_home: Path, fake_ps, no_usage, monkeypatch, capsys
    ):
        monkeypatch.setattr(process_detection, "list_codex_processes", list_codex_processes)
        s = _switcher()
        _add(s, codex_home, **PERSONAL)
        capsys.readouterr()
        s.list_accounts()
        out = capsys.readouterr().out
        lines = out.split("Running instances:")[1].strip().splitlines()
        assert len(lines) == 2
        assert "codex app-server" in lines[0] and "(2 sessions)" in lines[0]
        assert "(2 sessions)" in lines[1]

    def test_switch_followup_counts_the_scan(
        self, codex_home: Path, fake_ps, no_usage, monkeypatch, capsys
    ):
        monkeypatch.setattr(process_detection, "list_codex_processes", list_codex_processes)
        s = _switcher()
        _add(s, codex_home, **OTHER)
        _add(s, codex_home, **PERSONAL)
        capsys.readouterr()
        s.switch_to("1")
        assert "4 Codex processes running now." in capsys.readouterr().out
