"""``CodexAccountSwitcher``: add / list / status / switch / usage for Codex.

The Codex switcher is the shared orchestration with Codex leaves: the live
login is ``$CODEX_HOME/auth.json`` (``CodexCredentialStore``), identity is
read offline from the id_token, and the store lives under ``<root>/codex``.
Network is stubbed by conftest (``block_real_codex_network``); tests that
read usage or refresh patch ``codex_auth.request_usage`` / ``try_refresh``.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from claude_swap import codex_auth, codex_switcher
from claude_swap.codex_store import CodexCredentialStore, CodexStoreUnsupported
from claude_swap.codex_switcher import CodexAccountSwitcher
from claude_swap.credentials import ActiveCredentials
from claude_swap.exceptions import ConfigError, SwitchError, ValidationError
from claude_swap.models import Platform
from claude_swap.oauth import RefreshOutcome
from claude_swap.switcher import ClaudeAccountSwitcher
from tests import codex_fixtures as cf

PERSONAL = dict(email="user@example.com", account_id="acct-personal", user_id="user-1", plan="plus")
TEAM = dict(email="user@example.com", account_id="acct-team", user_id="user-1", plan="team")
OTHER = dict(email="other@example.com", account_id="acct-other", user_id="user-2", plan="pro")

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")


def _switcher() -> CodexAccountSwitcher:
    s = CodexAccountSwitcher()
    s.platform = Platform.LINUX
    return s


def _set_live(codex_home: Path, **kw) -> str:
    text = cf.auth_json(**kw)
    (codex_home / "auth.json").write_text(text, encoding="utf-8")
    return text


def _live(codex_home: Path) -> str:
    return (codex_home / "auth.json").read_text(encoding="utf-8")


def _add(s: CodexAccountSwitcher, codex_home: Path, **kw) -> str:
    """Make ``kw`` the live login and capture it; returns the auth.json text."""
    text = _set_live(codex_home, **kw)
    s.add_account()
    return text


def _roster(s: CodexAccountSwitcher) -> dict:
    return s._get_sequence_data()


@pytest.fixture
def usage_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """``codex_auth.request_usage`` answering a 5h/7d body; records each call."""
    calls: list[dict] = []

    def fake(creds, base_url=None, timeout_s=10.0):
        calls.append({"creds": creds, "base_url": base_url})
        now = int(time.time())
        return cf.wham_usage((25.0, 18000, now + 3600), (40.0, 604800, now + 86400))

    monkeypatch.setattr(codex_auth, "request_usage", fake)
    return calls


@pytest.fixture
def fake_codex(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A ``codex`` binary on PATH whose ``login`` writes ``state.auth`` into
    the ``CODEX_HOME`` it was given."""
    state = SimpleNamespace(auth=cf.auth_json(**OTHER), rc=0, exc=None, calls=[])

    def run(cmd, env=None, **kwargs):
        home = Path(env["CODEX_HOME"])
        state.calls.append({
            "cmd": list(cmd), "home": home,
            "mode": stat.S_IMODE(home.stat().st_mode),
        })
        if state.exc is not None:
            raise state.exc
        if state.auth is not None:
            (home / "auth.json").write_text(state.auth, encoding="utf-8")
        return subprocess.CompletedProcess(cmd, state.rc)

    monkeypatch.setattr(
        codex_switcher.shutil, "which",
        lambda name: "/opt/bin/codex" if name == "codex" else None,
    )
    monkeypatch.setattr(codex_switcher.subprocess, "run", run)
    return state


def _login_dirs(s: CodexAccountSwitcher) -> list[Path]:
    return list(s.backup_dir.glob(".login-*"))


class TestProvider:
    def test_attributes_and_store(self, codex_home: Path):
        s = _switcher()
        assert (s.provider_name, s.display_name, s.cli_prefix) == ("codex", "Codex", "cswap codex")
        assert s.backup_dir == s.root_dir / "codex"
        assert s.backup_keychain_service == "claude-swap-codex"
        assert s.run_legacy_migrations is False
        assert isinstance(s._store, CodexCredentialStore)
        assert s.live_login_path() == codex_home / "auth.json"

    def test_looks_like_api_key_per_provider(self, codex_home: Path):
        claude, codex = ClaudeAccountSwitcher(), _switcher()
        assert claude._looks_like_api_key("sk-ant-api03-xyz")
        assert not claude._looks_like_api_key(cf.api_key_json())
        assert codex._looks_like_api_key(cf.api_key_json())
        assert not codex._looks_like_api_key(cf.auth_json())
        assert not codex._looks_like_api_key("sk-ant-api03-xyz")


class TestAdd:
    def test_captures_live_login(self, codex_home: Path):
        s = _switcher()
        live = _add(s, codex_home, **PERSONAL)

        data = _roster(s)
        rec = data["accounts"]["1"]
        assert (rec["email"], rec["uuid"], rec["organizationUuid"], rec["organizationName"]) == (
            "user@example.com", "user-1", "acct-personal", "",
        )
        assert "kind" not in rec
        assert data["activeAccountNumber"] == 1
        assert s._read_account_credentials("1", "user@example.com") == live
        assert json.loads(s._read_account_config("1", "user@example.com")) == {
            "oauthAccount": {
                "emailAddress": "user@example.com",
                "accountUuid": "user-1",
                "organizationUuid": "acct-personal",
                "organizationName": None,
            }
        }
        assert _live(codex_home) == live
        assert not (codex_home.parent / ".claude.json").exists()

    def test_same_identity_twice_refreshes_the_same_slot(self, codex_home: Path):
        s = _switcher()
        _add(s, codex_home, **PERSONAL)
        rotated = _add(s, codex_home, **PERSONAL, refresh_token="rt-rotated")
        assert list(_roster(s)["accounts"]) == ["1"]
        assert s._read_account_credentials("1", "user@example.com") == rotated

    def test_personal_and_workspace_of_one_email_are_distinct(self, codex_home: Path):
        s = _switcher()
        _add(s, codex_home, **PERSONAL)
        _add(s, codex_home, **TEAM)
        accounts = _roster(s)["accounts"]
        assert [(a["email"], a["organizationUuid"]) for a in accounts.values()] == [
            ("user@example.com", "acct-personal"), ("user@example.com", "acct-team"),
        ]

    def test_workspace_name_comes_from_backend_with_config_base_url(
        self, codex_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        (codex_home / "config.toml").write_text(
            'chatgpt_base_url = "https://proxy.test/backend-api"\n', encoding="utf-8"
        )
        seen: list = []

        def fetch(creds, base_url=None):
            seen.append((codex_auth.identity(creds)["organizationUuid"], base_url))
            return "Acme Corp"

        monkeypatch.setattr(codex_auth, "fetch_workspace_name", fetch)
        s = _switcher()
        _add(s, codex_home, **TEAM)
        assert seen == [("acct-team", "https://proxy.test/backend-api")]
        assert _roster(s)["accounts"]["1"]["organizationName"] == "Acme Corp"
        cfg = json.loads(s._read_account_config("1", "user@example.com"))
        assert cfg["oauthAccount"]["organizationName"] == "Acme Corp"

    def test_workspace_name_falls_back_to_plan_label(self, codex_home: Path):
        s = _switcher()
        _add(s, codex_home, **TEAM)  # conftest's lookup stub answers None
        assert _roster(s)["accounts"]["1"]["organizationName"] == "ChatGPT Team"

    def test_a_later_successful_fetch_corrects_the_stored_name(
        self, codex_home: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ):
        s = _switcher()
        _add(s, codex_home, **TEAM)  # lookup fails: the plan label is stored
        assert _roster(s)["accounts"]["1"]["organizationName"] == "ChatGPT Team"

        monkeypatch.setattr(codex_auth, "fetch_workspace_name", lambda c, base_url=None: "Acme Corp")
        capsys.readouterr()
        _switcher().add_account()  # plain re-add, a later process

        assert _roster(s)["accounts"]["1"]["organizationName"] == "Acme Corp"
        cfg = json.loads(s._read_account_config("1", "user@example.com"))
        assert cfg["oauthAccount"]["organizationName"] == "Acme Corp"
        assert "[Acme Corp]" in capsys.readouterr().out

    def test_personal_plan_skips_the_workspace_lookup(
        self, codex_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        fetch = MagicMock(return_value="nope")
        monkeypatch.setattr(codex_auth, "fetch_workspace_name", fetch)
        _add(_switcher(), codex_home, **PERSONAL)
        fetch.assert_not_called()

    def test_live_api_key_login_points_at_add_token(self, codex_home: Path):
        (codex_home / "auth.json").write_text(cf.api_key_json(), encoding="utf-8")
        with pytest.raises(ValidationError, match="cswap codex add-token"):
            _switcher().add_account()

    def test_no_live_login(self, codex_home: Path):
        (codex_home / "auth.json").unlink()
        with pytest.raises(ConfigError, match="No active Codex login"):
            _switcher().add_account()

    def test_claude_store_env_vars_do_not_redirect_the_capture(
        self, codex_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        profile = tmp_path / "claude-profile"
        profile.mkdir()
        (profile / ".credentials.json").write_text(json.dumps(
            {"claudeAiOauth": {"accessToken": "at-claude", "refreshToken": "rt-claude"}}
        ), encoding="utf-8")
        (profile / ".claude.json").write_text(json.dumps(
            {"oauthAccount": {"emailAddress": "claude@example.com"}}
        ), encoding="utf-8")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(profile))
        monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", str(profile))

        s = _switcher()
        live = _add(s, codex_home, **PERSONAL)
        assert _roster(s)["accounts"]["1"]["email"] == "user@example.com"
        assert s._read_account_credentials("1", "user@example.com") == live

    def test_refused_inside_a_codex_session_profile(
        self, codex_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        s = _switcher()
        profile = s.backup_dir / "sessions" / "1-user"
        profile.mkdir(parents=True)
        monkeypatch.setenv("CODEX_HOME", str(profile))
        with pytest.raises(SwitchError, match="CODEX_HOME"):
            s.add_account()


class TestAddViaLogin:
    @posix_only
    def test_new_account_in_a_fresh_home_leaves_live_alone(
        self, codex_home: Path, fake_codex: SimpleNamespace
    ):
        s = _switcher()
        live = _add(s, codex_home, **PERSONAL)

        s.add_account_via_login()

        (call,) = fake_codex.calls
        assert call["cmd"] == [
            "/opt/bin/codex", "login", "-c", 'cli_auth_credentials_store="file"',
        ]
        assert call["home"].parent == s.backup_dir
        assert call["home"].name.startswith(".login-")
        assert call["mode"] == 0o700
        assert not call["home"].exists()
        assert _login_dirs(s) == []
        assert _live(codex_home) == live
        data = _roster(s)
        assert data["accounts"]["2"]["email"] == "other@example.com"
        assert data["accounts"]["2"]["organizationUuid"] == "acct-other"
        assert data["activeAccountNumber"] == 1
        assert s._read_account_credentials("2", "other@example.com") == fake_codex.auth

    def test_device_auth_is_passed_through(
        self, codex_home: Path, fake_codex: SimpleNamespace
    ):
        _switcher().add_account_via_login(device_auth=True, alias="work")
        (call,) = fake_codex.calls
        assert call["cmd"] == [
            "/opt/bin/codex", "login", "--device-auth",
            "-c", 'cli_auth_credentials_store="file"',
        ]
        assert not any("logout" in arg for arg in call["cmd"])

    def test_relogin_refreshes_the_existing_slot(
        self, codex_home: Path, fake_codex: SimpleNamespace
    ):
        s = _switcher()
        _add(s, codex_home, **OTHER)
        live = _add(s, codex_home, **PERSONAL)
        fake_codex.auth = cf.auth_json(**OTHER, refresh_token="rt-fresh")

        s.add_account_via_login()

        data = _roster(s)
        assert sorted(data["accounts"]) == ["1", "2"]
        assert s._read_account_credentials("1", "other@example.com") == fake_codex.auth
        assert data["activeAccountNumber"] == 2
        assert _live(codex_home) == live

    def test_relogin_of_the_live_account_updates_live_too(
        self, codex_home: Path, fake_codex: SimpleNamespace
    ):
        """Otherwise the next switch away classifies the stale live lineage
        as this slot's own rotation and writes it over the fresh login."""
        s = _switcher()
        _add(s, codex_home, **PERSONAL)
        fake_codex.auth = cf.auth_json(**PERSONAL, refresh_token="rt-fresh")

        s.add_account_via_login()

        assert s._read_account_credentials("1", "user@example.com") == fake_codex.auth
        assert _live(codex_home) == fake_codex.auth

    @pytest.mark.parametrize("rc, writes_login", [(1, True), (0, False)])
    def test_failed_login_adds_nothing_and_cleans_up(
        self, codex_home: Path, fake_codex: SimpleNamespace, rc, writes_login
    ):
        s = _switcher()
        _add(s, codex_home, **PERSONAL)
        fake_codex.rc = rc
        if not writes_login:
            fake_codex.auth = None
        with pytest.raises(ConfigError, match="nothing was added"):
            s.add_account_via_login()
        assert _login_dirs(s) == []
        assert list(_roster(s)["accounts"]) == ["1"]

    def test_interrupt_still_removes_the_temp_home(
        self, codex_home: Path, fake_codex: SimpleNamespace
    ):
        s = _switcher()
        fake_codex.exc = KeyboardInterrupt()
        with pytest.raises(KeyboardInterrupt):
            s.add_account_via_login()
        assert fake_codex.calls and _login_dirs(s) == []

    def test_codex_not_on_path(self, codex_home: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(codex_switcher.shutil, "which", lambda name: None)
        run = MagicMock()
        monkeypatch.setattr(codex_switcher.subprocess, "run", run)
        with pytest.raises(ConfigError, match="codex"):
            _switcher().add_account_via_login()
        run.assert_not_called()


    def test_live_login_changed_at_the_prompt_is_left_alone(
        self, codex_home: Path, fake_codex: SimpleNamespace, monkeypatch, capsys
    ):
        s = _switcher()
        _add(s, codex_home, **PERSONAL)
        fake_codex.auth = cf.auth_json(**PERSONAL, refresh_token="rt-fresh")
        third = cf.auth_json(email="third@example.com", account_id="acct-3", user_id="user-3")
        real_add = ClaudeAccountSwitcher.add_account

        def add_then_live_changes(self, *args, **kwargs):
            real_add(self, *args, **kwargs)
            (codex_home / "auth.json").write_text(third, encoding="utf-8")

        monkeypatch.setattr(ClaudeAccountSwitcher, "add_account", add_then_live_changes)
        s.add_account_via_login()

        assert s._read_account_credentials("1", "user@example.com") == fake_codex.auth
        assert _live(codex_home) == third
        assert "left alone" in capsys.readouterr().out

    def test_cancelled_at_the_overwrite_prompt(
        self, codex_home: Path, fake_codex: SimpleNamespace, monkeypatch
    ):
        s = _switcher()
        live = _add(s, codex_home, **PERSONAL)
        before = _roster(s)
        monkeypatch.setattr("builtins.input", lambda prompt="": "n")

        s.add_account_via_login(slot=1)  # occupied by another account

        assert _roster(s)["accounts"] == before["accounts"]
        assert _roster(s)["activeAccountNumber"] == 1
        assert s._read_account_credentials("1", "user@example.com") == live
        assert _live(codex_home) == live
        assert _login_dirs(s) == []

    def test_stale_login_homes_are_swept_and_fresh_ones_kept(
        self, codex_home: Path, fake_codex: SimpleNamespace
    ):
        s = _switcher()
        s._setup_directories()
        stale = s.backup_dir / ".login-crashed"
        stale.mkdir()
        (stale / "auth.json").write_text(cf.auth_json(**OTHER), encoding="utf-8")
        old = time.time() - 2 * 3600
        os.utime(stale, (old, old))
        in_progress = s.backup_dir / ".login-other-terminal"
        in_progress.mkdir()

        s.add_account_via_login()

        assert not stale.exists()
        assert in_progress.exists()
        assert not fake_codex.calls[0]["home"].exists()

    def test_undeletable_login_home_is_reported(
        self, codex_home: Path, fake_codex: SimpleNamespace, monkeypatch, caplog
    ):
        def refuse(path, *args, **kwargs):
            raise PermissionError("denied")

        monkeypatch.setattr(codex_switcher.shutil, "rmtree", refuse)
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            _switcher().add_account_via_login()
        home = fake_codex.calls[0]["home"]
        assert any(
            str(home) in r.getMessage() and "unrevoked" in r.getMessage()
            for r in caplog.records
        )


class TestAddToken:
    def test_openai_api_key_account(self, codex_home: Path, usage_calls: list):
        s = _switcher()
        live = _add(s, codex_home, **PERSONAL)
        s.add_account_from_token("sk-proj-abc123")

        rec = _roster(s)["accounts"]["2"]
        assert (rec["email"], rec["kind"], rec["organizationUuid"]) == (
            "api-key-2@token.local", "api_key", "",
        )
        assert json.loads(s._read_account_credentials("2", rec["email"])) == {
            "auth_mode": "apikey", "OPENAI_API_KEY": "sk-proj-abc123",
        }
        rows = {r["number"]: r for r in s.list_accounts(json_output=True)["accounts"]}
        assert rows[2]["usageStatus"] == "api_key"
        assert rows[1]["usageStatus"] == "ok"
        assert [c["creds"] for c in usage_calls] == [live]

    def test_switching_to_an_api_key_slot_keeps_it_recognized(
        self, codex_home: Path
    ):
        s = _switcher()
        live = _add(s, codex_home, **PERSONAL)
        s.add_account_from_token("sk-proj-abc123")

        s.switch_to("2", json_output=True)
        assert json.loads(_live(codex_home))["OPENAI_API_KEY"] == "sk-proj-abc123"
        assert s.current_account_number() == "2"

        result = s.switch_to("1", json_output=True)
        assert result["from"] == {"number": 2, "email": "api-key-2@token.local"}
        assert _live(codex_home) == live
        assert s.list_unclaimed_credentials() == {}


    def test_api_key_identity_scans_backups_once_per_roster_state(
        self, codex_home: Path, monkeypatch
    ):
        s = _switcher()
        _add(s, codex_home, **PERSONAL)
        s.add_account_from_token("sk-proj-abc123")
        reads = MagicMock(wraps=s._read_account_credentials_ex)
        monkeypatch.setattr(s, "_read_account_credentials_ex", reads)

        for _ in range(3):  # a ChatGPT login never scans
            s.current_account_number()
        assert reads.call_count == 0

        (codex_home / "auth.json").write_text(
            s._read_account_credentials("2", "api-key-2@token.local"), encoding="utf-8"
        )
        reads.reset_mock()
        assert [s.current_account_number() for _ in range(3)] == ["2"] * 3
        assert reads.call_count == 1

        s.set_alias("2", "keyed")  # any roster write re-validates
        reads.reset_mock()
        assert s.current_account_number() == "2"
        assert reads.call_count == 1


class TestListStatus:
    def test_json_carries_provider_and_codex_is_organization(
        self, codex_home: Path, usage_calls: list, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(codex_auth, "fetch_workspace_name", lambda c, base_url=None: "Acme Corp")
        s = _switcher()
        _add(s, codex_home, **TEAM)
        _add(s, codex_home, **PERSONAL)

        payload = s.list_accounts(json_output=True)
        assert payload["provider"] == "codex"
        rows = {r["number"]: r for r in payload["accounts"]}
        assert (rows[1]["organizationName"], rows[1]["organizationUuid"], rows[1]["isOrganization"]) == (
            "Acme Corp", "acct-team", True,
        )
        assert (rows[2]["organizationName"], rows[2]["organizationUuid"], rows[2]["isOrganization"]) == (
            "", "acct-personal", False,
        )
        assert rows[2]["active"] is True

        status = s.status(json_output=True)
        assert status["provider"] == "codex"
        assert status["active"]["organizationUuid"] == "acct-personal"
        assert status["active"]["isOrganization"] is False

    def test_human_tags(self, codex_home: Path, usage_calls: list, monkeypatch, capsys):
        monkeypatch.setattr(codex_auth, "fetch_workspace_name", lambda c, base_url=None: "Acme Corp")
        s = _switcher()
        _add(s, codex_home, **TEAM)
        _add(s, codex_home, **PERSONAL)
        capsys.readouterr()
        s.list_accounts()
        out = capsys.readouterr().out
        assert "user@example.com [Acme Corp]" in out
        assert "user@example.com [personal]" in out

    def test_empty_list_json_has_provider(self, codex_home: Path):
        payload = _switcher().list_accounts(json_output=True)
        assert payload["provider"] == "codex" and payload["accounts"] == []

    def test_status_without_a_login(self, codex_home: Path, capsys):
        (codex_home / "auth.json").unlink()
        s = _switcher()
        assert s.status(json_output=True)["active"] is None
        s.status()
        assert "No active Codex login" in capsys.readouterr().out

    def test_unsupported_store_is_a_clean_error(self, codex_home: Path, usage_calls: list):
        s = _switcher()
        _add(s, codex_home, **PERSONAL)
        (codex_home / "config.toml").write_text(
            'cli_auth_credentials_store = "ephemeral"\n', encoding="utf-8"
        )
        with pytest.raises(CodexStoreUnsupported):
            s.list_accounts(json_output=True)
        # Callers that assume an active read never raises: unseen, not dead.
        assert s._slot_token_dead("1", "user@example.com") is False


class TestSwitch:
    def test_activates_target_and_backs_up_the_rotated_outgoing_login(
        self, codex_home: Path, usage_calls: list, monkeypatch, caplog
    ):
        profile = MagicMock(return_value=None)
        monkeypatch.setattr("claude_swap.oauth.fetch_oauth_profile", profile)
        s = _switcher()
        _add(s, codex_home, **OTHER)
        _add(s, codex_home, **PERSONAL)
        rotated = _set_live(
            codex_home, **PERSONAL, refresh_token="rt-rotated",
            access=cf.access_token(account_id="acct-personal", user_id="user-1", nonce="b"),
        )

        with caplog.at_level(logging.INFO, logger="claude-swap"):
            s.switch_to("1")

        assert _live(codex_home) == s._read_account_credentials("1", "other@example.com")
        assert s._read_account_credentials("2", "user@example.com") == rotated
        # own-rotated (offline id_token oracle), not the fail-open "unresolved".
        assert "Backed up account 2" in [r.getMessage() for r in caplog.records]
        assert _roster(s)["activeAccountNumber"] == 1
        profile.assert_not_called()
        assert not (codex_home.parent / ".claude.json").exists()
        assert not (codex_home.parent / ".claude" / ".credentials.json").exists()

    def test_json_result_carries_provider(self, codex_home: Path):
        s = _switcher()
        _add(s, codex_home, **OTHER)
        _add(s, codex_home, **PERSONAL)
        result = s.switch_to("1", json_output=True)
        assert result["provider"] == "codex" and result["switched"] is True
        noop = s.switch_to("1", json_output=True)
        assert noop["provider"] == "codex" and noop["reason"] == "already-active"

    def test_followup_names_the_restart_and_running_processes(
        self, codex_home: Path, usage_calls: list, monkeypatch, capsys
    ):
        s = _switcher()
        _add(s, codex_home, **OTHER)
        _add(s, codex_home, **PERSONAL)
        monkeypatch.setattr(s, "_running_instances", lambda: (
            [SimpleNamespace(entrypoint="cli", cwd="/w")] * 2,
            [SimpleNamespace(ide_name="vscode", workspace_folders=["/w"])],
        ))
        capsys.readouterr()
        s.switch_to("1")
        out = capsys.readouterr().out
        assert "codex app-server daemon restart" in out
        assert "3 Codex processes" in out

    def test_store_warning_reaches_the_switch_warnings(self, codex_home: Path):
        s = _switcher()
        hybrid = cf.auth_dict(**OTHER)
        hybrid["tokens"]["account_id"] = "acct-hybrid"
        (codex_home / "auth.json").write_text(json.dumps(hybrid, indent=2), encoding="utf-8")
        s.add_account()
        _add(s, codex_home, **PERSONAL)

        result = s.switch_to("1", json_output=True)
        assert any("inconsistent" in w for w in result["warnings"])

    @pytest.mark.parametrize("config, allowed", [
        ('forced_login_method = "api"\n', False),
        ('forced_login_method = "chatgpt"\n', True),
        ('forced_chatgpt_workspace_id = "acct-other"\n', True),
        ('forced_chatgpt_workspace_id = "acct-elsewhere"\n', False),
        ('forced_chatgpt_workspace_id = ["acct-x", "acct-other"]\n', True),
        ('forced_chatgpt_workspace_id = ["acct-x", "acct-y"]\n', False),
        ('forced_chatgpt_workspace_id = [" ", ""]\n', True),
    ])
    def test_forced_login_settings_guard_the_target(
        self, codex_home: Path, config: str, allowed: bool
    ):
        s = _switcher()
        _add(s, codex_home, **OTHER)
        live = _add(s, codex_home, **PERSONAL)
        (codex_home / "config.toml").write_text(config, encoding="utf-8")
        if allowed:
            s.switch_to("1", json_output=True)
            assert _live(codex_home) != live
        else:
            with pytest.raises(SwitchError, match="forced_"):
                s.switch_to("1", json_output=True)
            assert _live(codex_home) == live
            assert _roster(s)["activeAccountNumber"] == 2

    @pytest.mark.parametrize("config, allowed", [
        ('forced_login_method = "chatgpt"\n', False),
        ('forced_login_method = "api"\n', True),
        ('forced_chatgpt_workspace_id = "acct-x"\n', True),
    ])
    def test_forced_login_settings_with_an_api_key_target(
        self, codex_home: Path, config: str, allowed: bool
    ):
        s = _switcher()
        live = _add(s, codex_home, **PERSONAL)
        s.add_account_from_token("sk-proj-abc123")
        (codex_home / "config.toml").write_text(config, encoding="utf-8")
        if allowed:
            s.switch_to("2", json_output=True)
            assert _live(codex_home) != live
        else:
            with pytest.raises(SwitchError, match="forced_login_method"):
                s.switch_to("2", json_output=True)
            assert _live(codex_home) == live


    def test_workspace_name_survives_a_switch_in_a_later_process(
        self, codex_home: Path, monkeypatch
    ):
        monkeypatch.setattr(codex_auth, "fetch_workspace_name", lambda c, base_url=None: "Acme Corp")
        first = _switcher()
        _add(first, codex_home, **OTHER)
        _add(first, codex_home, **TEAM)
        monkeypatch.setattr(codex_auth, "fetch_workspace_name", lambda c, base_url=None: None)

        _switcher().switch_to("1", json_output=True)

        cfg = json.loads(first._read_account_config("2", "user@example.com"))
        assert cfg["oauthAccount"]["organizationName"] == "Acme Corp"


class TestUsage:
    def test_inactive_expired_token_refreshes_through_the_gate_and_persists(
        self, codex_home: Path, usage_calls: list, monkeypatch, tmp_path
    ):
        # Claude's store-redirect guard must not gate a Codex consume.
        monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", str(tmp_path))
        s = _switcher()
        expired = _add(s, codex_home, **OTHER, access_exp=int(time.time()) - 60)
        _add(s, codex_home, **PERSONAL)
        fresh = cf.auth_json(**OTHER, refresh_token="rt-other-2")
        posted: list[str] = []

        def refresh(creds, timeout_s=10.0):
            posted.append(creds)
            return RefreshOutcome(fresh, None)

        monkeypatch.setattr(codex_auth, "try_refresh", refresh)
        rows = {r["number"]: r for r in s.list_accounts(json_output=True)["accounts"]}

        assert posted == [expired]
        assert s._read_account_credentials("1", "other@example.com") == fresh
        assert rows[1]["usageStatus"] == "ok"
        assert fresh in [c["creds"] for c in usage_calls]

    def test_active_expired_token_is_never_refreshed(
        self, codex_home: Path, monkeypatch, capsys
    ):
        s = _switcher()
        _add(s, codex_home, **PERSONAL, access_exp=int(time.time()) - 60)
        refresh, usage = MagicMock(), MagicMock()
        monkeypatch.setattr(codex_auth, "try_refresh", refresh)
        monkeypatch.setattr(codex_auth, "request_usage", usage)

        assert s.status(json_output=True)["active"]["usageStatus"] == "token_expired"
        capsys.readouterr()
        s.status()
        assert "open Codex to refresh" in capsys.readouterr().out
        refresh.assert_not_called()
        usage.assert_not_called()

    def test_active_fresh_token_is_read_with_the_config_base_url(
        self, codex_home: Path, usage_calls: list
    ):
        (codex_home / "config.toml").write_text(
            'chatgpt_base_url = "https://proxy.test/backend-api/"\n', encoding="utf-8"
        )
        s = _switcher()
        _add(s, codex_home, **PERSONAL)
        assert s.status(json_output=True)["active"]["usageStatus"] == "ok"
        assert usage_calls and {c["base_url"] for c in usage_calls} == {
            "https://proxy.test/backend-api/"
        }

    def test_active_rotation_by_codex_resyncs_the_backup(
        self, codex_home: Path, usage_calls: list
    ):
        s = _switcher()
        _add(s, codex_home, **PERSONAL)
        rotated = _set_live(codex_home, **PERSONAL, refresh_token="rt-rotated")
        s.status(json_output=True)
        assert s._read_account_credentials("1", "user@example.com") == rotated
        assert _live(codex_home) == rotated

    def test_stash_records_the_live_auth_json_mtime(self, codex_home: Path):
        s = _switcher()
        s._setup_directories()
        entry_id = s._stash_live_credential(_live(codex_home), "alien", "1", None)
        entry = s.list_unclaimed_credentials()[entry_id]
        assert entry["credentialsMtime"] is not None
        assert entry["liveOauthAccount"]["organizationUuid"] == "acct-personal"

    def test_active_401_is_not_asked_again_until_the_token_changes(
        self, codex_home: Path, monkeypatch
    ):
        s = _switcher()
        _add(s, codex_home, **PERSONAL)
        asked: list[str] = []
        errors: list = []

        def rejected(creds, base_url=None, timeout_s=10.0):
            asked.append(creds)
            errors.append(cf.http_error(401))
            raise errors[-1]

        monkeypatch.setattr(codex_auth, "request_usage", rejected)
        refresh, gate = MagicMock(), MagicMock()
        monkeypatch.setattr(codex_auth, "try_refresh", refresh)
        monkeypatch.setattr(s, "consume_backup_grant", gate)

        for _ in range(3):
            assert s.status(json_output=True)["active"]["usageStatus"] == "token_expired"
        assert len(asked) == 1
        refresh.assert_not_called()
        gate.assert_not_called()

        renewed = _set_live(
            codex_home, **PERSONAL,
            access=cf.access_token(account_id="acct-personal", user_id="user-1", nonce="b"),
        )
        s.status(json_output=True)
        assert asked[-1] == renewed and len(asked) == 2
        for err in errors:
            err.close()


class TestConsumeGate:
    """Codex refreshes the live login itself; the gate must never spend it."""

    @pytest.mark.parametrize("verdict", ["unreadable", "degraded", "unsupported"])
    def test_no_consume_while_the_live_read_is_not_clean(
        self, codex_home: Path, monkeypatch, verdict: str
    ):
        s = _switcher()
        _add(s, codex_home, **OTHER, access_exp=int(time.time()) - 60)
        live = _add(s, codex_home, **PERSONAL, access_exp=int(time.time()) - 60)
        posted: list[str] = []
        monkeypatch.setattr(
            codex_auth, "try_refresh",
            lambda creds, timeout_s=10.0: posted.append(creds) or RefreshOutcome(None, "transient"),
        )
        rejected = cf.http_error(401)
        monkeypatch.setattr(codex_auth, "request_usage", MagicMock(side_effect=rejected))

        def read(store):
            if verdict == "unsupported":
                raise CodexStoreUnsupported("keyring off macOS")
            if verdict == "unreadable":  # keyring mode, Keychain locked
                return ActiveCredentials(None, True, True)
            return ActiveCredentials(live, False, True)  # auto-mode file fallback

        monkeypatch.setattr(CodexCredentialStore, "_read_active_credentials", read)
        for num, email in (("1", "other@example.com"), ("2", "user@example.com")):
            outcome = s.consume_backup_grant(
                num, email, s._read_account_credentials(num, email)
            )
            assert outcome.error == "transient"
        if verdict != "unsupported":
            s.list_accounts(json_output=True)
        rejected.close()
        assert posted == []

    def test_the_live_lineage_is_never_consumed(self, codex_home: Path, monkeypatch):
        s = _switcher()
        live = _add(s, codex_home, **PERSONAL, access_exp=int(time.time()) - 60)
        refresh = MagicMock()
        monkeypatch.setattr(codex_auth, "try_refresh", refresh)

        outcome = s.consume_backup_grant("1", "user@example.com", live)

        assert outcome.error == "transient"
        refresh.assert_not_called()


class TestHybridLogin:
    """codex-spec gotcha 5: a swap racing a refresh leaves B's
    ``tokens.account_id`` over A's tokens. The lineage oracle follows the
    tokens (the id_token's own claim), not the stored account_id."""

    @staticmethod
    def _hybrid(**tokens_of) -> str:
        data = cf.auth_dict(**tokens_of, refresh_token="rt-personal-rotated")
        data["tokens"]["account_id"] = "acct-team"
        return json.dumps(data, indent=2)

    def test_the_oracle_names_the_tokens_owner(self, codex_home: Path):
        hybrid = self._hybrid(**PERSONAL)
        assert _switcher()._resolve_token_identity(hybrid) == {
            "uuid": "user-1", "email": "user@example.com", "organizationUuid": "acct-personal",
        }

    def test_a_hybrid_never_resyncs_into_the_stored_account_ids_slot(self, codex_home: Path):
        s = _switcher()
        _add(s, codex_home, **PERSONAL)
        team = _add(s, codex_home, **TEAM)
        hybrid = self._hybrid(**PERSONAL)
        (codex_home / "auth.json").write_text(hybrid, encoding="utf-8")

        s._resync_rotated_backup("2", "user@example.com", "acct-team", hybrid)

        assert s._read_account_credentials("2", "user@example.com") == team


class TestWorkspaceNameRoster:
    @posix_only
    def test_a_roster_lock_timeout_keeps_the_completed_login(
        self, codex_home: Path, fake_codex: SimpleNamespace, monkeypatch, caplog
    ):
        from claude_swap.exceptions import LockError

        fake_codex.auth = cf.auth_json(**TEAM)
        monkeypatch.setattr(codex_auth, "fetch_workspace_name", lambda c, base_url=None: "Acme Corp")

        class Busy:
            def __init__(self, path):
                pass

            def __enter__(self):
                raise LockError("Failed to acquire lock - another instance may be running")

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(codex_switcher, "FileLock", Busy)
        s = _switcher()
        _add(s, codex_home, **PERSONAL)

        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            s.add_account_via_login()

        rec = _roster(s)["accounts"]["2"]
        assert (rec["organizationUuid"], rec["organizationName"]) == ("acct-team", "Acme Corp")
        assert s._read_account_credentials("2", "user@example.com") == fake_codex.auth
        assert any("workspace name" in r.getMessage() for r in caplog.records)


class TestSwitchTargetValidity:
    @pytest.mark.parametrize("drop", ["last_refresh", "account_id"])
    def test_a_stored_login_codex_could_not_use_is_refused(
        self, codex_home: Path, drop: str
    ):
        s = _switcher()
        _add(s, codex_home, **OTHER)
        live = _add(s, codex_home, **PERSONAL)
        broken = cf.auth_dict(**OTHER)
        if drop == "last_refresh":
            del broken["last_refresh"]
        else:
            del broken["tokens"]["account_id"]
        s._write_account_credentials("1", "other@example.com", json.dumps(broken, indent=2))

        with pytest.raises(SwitchError, match=f"no {'tokens.' if drop == 'account_id' else ''}{drop}.*cswap codex add --login --slot 1"):
            s.switch_to("1")

        assert _live(codex_home) == live
        assert _roster(s)["activeAccountNumber"] == 2

    def test_api_key_targets_are_not_held_to_the_chatgpt_rules(self, codex_home: Path):
        s = _switcher()
        _add(s, codex_home, **PERSONAL)
        s.add_account_from_token("sk-proj-test", email="key@example.com")
        s.switch_to("2")
        assert codex_auth.is_api_key_blob(_live(codex_home))
