"""Provider seams on ``ClaudeAccountSwitcher`` (no behavior change for Claude).

A provider subclass (Codex) swaps the Claude-specific steps by overriding
small hook methods and class attributes. These tests pin that every hook is
actually consulted on its path — a hook nothing calls is a seam that silently
does nothing when overridden — and that the base bodies still resolve the
module globals the rest of the suite patches.
"""

from __future__ import annotations

import json
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_swap.credentials import SECURITY_SERVICE, CredentialStore
from claude_swap.models import Platform, SwitchTransaction
from claude_swap.paths import (
    get_backup_root,
    get_credentials_path,
    get_global_config_path,
)
from claude_swap.switcher import (
    ERROR_NOTES,
    SENTINEL_NOTES,
    ClaudeAccountSwitcher,
)

A = "a@example.com"
B = "b@example.com"
FAR = 9999999999000


def _oauth(tag: str, expires: int = FAR) -> str:
    return json.dumps({
        "claudeAiOauth": {
            "accessToken": f"at-{tag}",
            "refreshToken": f"rt-{tag}",
            "expiresAt": expires,
        }
    })


class _SubSwitcher(ClaudeAccountSwitcher):
    backup_subdir = "x"
    backup_keychain_service = "claude-swap-x"
    run_legacy_migrations = False


def _switcher(cls: type = ClaudeAccountSwitcher, platform=Platform.LINUX):
    s = cls()
    s.platform = platform
    s._setup_directories()
    s._init_sequence_file()
    return s


def _two_accounts(s: ClaudeAccountSwitcher) -> None:
    data = s._get_sequence_data()
    for num, email in ((1, A), (2, B)):
        s._write_account_credentials(str(num), email, _oauth(str(num)))
        s._write_account_config(str(num), email, json.dumps({
            "oauthAccount": {"emailAddress": email, "accountUuid": f"uuid-{num}"}
        }))
        data["accounts"][str(num)] = {
            "email": email, "uuid": f"uuid-{num}",
            "organizationUuid": "", "organizationName": "",
            "added": "2024-01-01T00:00:00Z",
        }
        data["sequence"].append(num)
    data["activeAccountNumber"] = 1
    s._write_json(s.sequence_file, data)


def _live(email: str, creds: str, uuid: str) -> Path:
    cfg = get_global_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({
        "oauthAccount": {"emailAddress": email, "accountUuid": uuid}
    }), encoding="utf-8")
    creds_path = get_credentials_path()
    creds_path.parent.mkdir(parents=True, exist_ok=True)
    creds_path.write_text(creds, encoding="utf-8")
    return cfg


def _spy(stack: ExitStack, s: ClaudeAccountSwitcher, *names: str) -> dict:
    return {
        name: stack.enter_context(
            patch.object(s, name, wraps=getattr(s, name))
        )
        for name in names
    }


class TestProviderAttributes:
    def test_claude_defaults(self, temp_home: Path):
        s = ClaudeAccountSwitcher()
        assert s.provider_name == "claude"
        assert s.display_name == "Claude Code"
        assert s.cli_prefix == "cswap"
        assert s.backup_subdir == ""
        assert s.backup_keychain_service == SECURITY_SERVICE == "claude-swap"
        assert s.run_legacy_migrations is True
        assert s.sentinel_notes is SENTINEL_NOTES
        assert s.error_notes is ERROR_NOTES
        assert s.root_dir == get_backup_root()
        assert s.backup_dir == s.root_dir


class TestStoreLayout:
    def test_subdir_partitions_everything_under_backup_dir(self, temp_home: Path):
        s = _SubSwitcher()
        assert s.root_dir == get_backup_root()
        assert s.backup_dir == s.root_dir / "x"
        assert s.sequence_file == s.backup_dir / "sequence.json"
        assert s.configs_dir == s.backup_dir / "configs"
        assert s.credentials_dir == s.backup_dir / "credentials"
        assert s.lock_file == s.backup_dir / ".lock"

    def test_legacy_dir_migration_receives_the_root(self, temp_home: Path):
        """Passing ``<root>/x`` would ``shutil.move`` the legacy store into
        its own child."""
        with patch(
            "claude_swap.switcher.migrate_legacy_backup_dir", return_value=False
        ) as migrate:
            s = _SubSwitcher()
        migrate.assert_called_once_with(s.root_dir)

    def test_legacy_data_migrations_are_gated(self, temp_home: Path):
        with patch("claude_swap.migrations.run_migrations") as run:
            ClaudeAccountSwitcher()
            assert run.call_count == 1
            _SubSwitcher()
            assert run.call_count == 1

    def test_make_store_hook_builds_the_store(self, temp_home: Path):
        class _Store(CredentialStore):
            pass

        class _S(ClaudeAccountSwitcher):
            def _make_store(self):
                return _Store(self)

        assert type(ClaudeAccountSwitcher()._store) is CredentialStore
        s = _S()
        assert isinstance(s._store, _Store)
        assert s._store._host is s


class TestBackupKeychainService:
    def test_backup_items_use_the_overridden_service(
        self, temp_home: Path, block_real_keychain
    ):
        s = _switcher(_SubSwitcher, Platform.MACOS)
        s._write_account_credentials("1", A, _oauth("1"))
        s._write_account_credentials("1", A, _oauth("1b"))  # retains a .prev
        keys = set(block_real_keychain.data)
        assert ("claude-swap-x", f"account-1-{A}") in keys
        assert ("claude-swap-x", f"account-1-{A}.prev") in keys
        assert not [k for k in keys if k[0] == SECURITY_SERVICE]
        assert s._read_account_credentials("1", A) == _oauth("1b")
        assert s._store._read_previous_backup("1", A) == _oauth("1")
        s._store._kc_delete_backup("1", A)
        s._store._kc_delete_backup_prev("1", A)
        assert not [k for k in block_real_keychain.data if k[0] == "claude-swap-x"]

    def test_purge_deletes_under_the_overridden_service(
        self, temp_home: Path, block_real_keychain, monkeypatch
    ):
        s = _switcher(platform=Platform.MACOS)
        s.backup_keychain_service = "svc-x"
        data = s._get_sequence_data()
        data["accounts"]["1"] = {"email": A, "organizationUuid": ""}
        data["sequence"].append(1)
        s._write_json(s.sequence_file, data)
        s._write_account_credentials("1", A, _oauth("1"))
        assert ("svc-x", f"account-1-{A}") in block_real_keychain.data
        (s.backup_dir / "sessions" / "1-a").mkdir(parents=True)
        monkeypatch.setattr("builtins.input", lambda *_: "y")

        with patch.object(s, "_scan_live_sessions", return_value=([], 0)) as scan:
            s.purge()

        scan.assert_called_once_with(s.backup_dir / "sessions" / "1-a")
        assert ("svc-x", f"account-1-{A}") not in block_real_keychain.data

    def test_subdir_purge_keeps_the_shared_root(
        self, temp_home: Path, block_real_keychain, monkeypatch
    ):
        """On macOS/Windows the legacy root IS the root: a provider subdir
        must not read it as a stale legacy dir and rmtree the whole root."""
        s = _switcher(_SubSwitcher, Platform.MACOS)
        root_file = s.root_dir / "sequence.json"
        root_file.write_text("{}", encoding="utf-8")
        monkeypatch.setattr("builtins.input", lambda *_: "y")

        with patch(
            "claude_swap.switcher.get_legacy_backup_root", return_value=s.root_dir
        ):
            s.purge()

        assert not s.backup_dir.exists()
        assert root_file.read_text(encoding="utf-8") == "{}"


class TestHookBasesResolveGlobalsAtCallTime:
    def test_locks_and_running_instances(self, temp_home: Path):
        s = ClaudeAccountSwitcher()
        with (
            patch("claude_swap.switcher.claude_credentials_lock", return_value="c"),
            patch("claude_swap.switcher.claude_config_lock", return_value="g"),
            patch("claude_swap.switcher.get_running_instances", return_value="r"),
        ):
            assert s._live_credentials_lock() == "c"
            assert s._live_config_lock() == "g"
            assert s._running_instances() == "r"

    def test_session_functions(self, temp_home: Path, tmp_path: Path):
        s = ClaudeAccountSwitcher()
        with (
            patch("claude_swap.session.scan_live_sessions", return_value=("s", 0)),
            patch("claude_swap.session.read_session_credentials", return_value="c"),
            patch("claude_swap.session.read_session_identity", return_value=("e", "o")),
            patch("claude_swap.session.profile_is_quiescent", return_value=False),
        ):
            assert s._scan_live_sessions(tmp_path) == ("s", 0)
            assert s._read_session_credentials(tmp_path) == "c"
            assert s._read_session_identity(tmp_path) == ("e", "o")
            assert s._profile_is_quiescent(tmp_path) is False

    def test_profile_oracle_takes_the_blob(self, temp_home: Path):
        s = ClaudeAccountSwitcher()
        with patch(
            "claude_swap.oauth.fetch_oauth_profile", return_value={"uuid": "u"}
        ) as fetch:
            assert s._resolve_token_identity(_oauth("1")) == {"uuid": "u"}
        fetch.assert_called_once_with("at-1")

    def test_store_env_guard(self, temp_home: Path, monkeypatch):
        s = ClaudeAccountSwitcher()
        monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)
        assert s._store_env_guard() is False
        monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", "/elsewhere")
        assert s._store_env_guard() is True

    def test_synth_config_is_the_token_account_shape(self, temp_home: Path):
        assert ClaudeAccountSwitcher()._synth_config_for({"email": A}) == {
            "oauthAccount": {
                "emailAddress": A,
                "accountUuid": "",
                "organizationUuid": None,
                "organizationName": None,
            }
        }

    def test_public_aliases(self, temp_home: Path):
        s = ClaudeAccountSwitcher()
        _live(A, _oauth("1"), "uuid-1")
        assert s.live_login_path() == s._get_claude_config_path()
        assert s.current_identity() == (A, "") == s._get_current_account()


class TestHooksAreConsultedOnTheirPaths:
    def test_normal_switch(self, temp_home: Path):
        s = _switcher()
        _two_accounts(s)
        cfg = _live(A, _oauth("1-rotated"), "uuid-1")
        with ExitStack() as stack:
            spies = _spy(
                stack, s,
                "_live_credentials_lock", "_live_config_lock",
                "_snapshot_live_config", "_apply_live_config",
                "_scan_live_sessions", "_resolve_token_identity",
            )
            out = s.switch_to("2", json_output=True)
        assert out["switched"] is True
        for name, spy in spies.items():
            assert spy.called, name
        spies["_resolve_token_identity"].assert_called_once_with(_oauth("1-rotated"))
        assert json.loads(cfg.read_text())["oauthAccount"]["emailAddress"] == B
        # Outgoing config backed up from the snapshot hook's text.
        assert json.loads(s._read_account_config("1", A))["oauthAccount"][
            "emailAddress"
        ] == A

    def test_direct_activation_rollback_restores_through_hook(self, temp_home: Path):
        s = _switcher()
        _two_accounts(s)
        cfg = _live("stranger@example.com", _oauth("x"), "uuid-x")
        original = cfg.read_text(encoding="utf-8")
        real_write_json = s._write_json

        def failing_write_json(path, data):
            if path == s.sequence_file and data.get("activeAccountNumber") == 2:
                raise OSError("disk full")
            return real_write_json(path, data)

        with ExitStack() as stack:
            spies = _spy(stack, s, "_snapshot_live_config", "_apply_live_config",
                         "_restore_live_config")
            stack.enter_context(
                patch.object(s, "_write_json", side_effect=failing_write_json)
            )
            with pytest.raises(OSError):
                s.switch_to("2", json_output=True)
        spies["_snapshot_live_config"].assert_called_once_with()
        assert spies["_apply_live_config"].called
        spies["_restore_live_config"].assert_called_once_with(original)
        assert cfg.read_text(encoding="utf-8") == original

    def test_transaction_rollback_restores_config_through_hook(self):
        switcher = MagicMock()
        tx = SwitchTransaction(
            original_credentials="creds",
            original_config="cfg-text",
            original_account_num="1",
            original_email=A,
        )
        tx.record_step("credentials_written")
        tx.record_step("config_written")
        assert tx.rollback(switcher) is True
        switcher._restore_live_config.assert_called_once_with("cfg-text")
        switcher._write_credentials.assert_called_once_with("creds")

    def test_add_account(self, temp_home: Path):
        s = _switcher()
        cfg = _live(A, _oauth("1"), "uuid-1")
        with ExitStack() as stack:
            spies = _spy(stack, s, "_snapshot_live_config", "_live_config_data",
                         "_resolve_token_identity")
            s.add_account()
        for name, spy in spies.items():
            assert spy.called, name
        spies["_resolve_token_identity"].assert_called_once_with(_oauth("1"))
        assert s._read_account_config("1", A) == cfg.read_text(encoding="utf-8")
        assert s._get_sequence_data()["accounts"]["1"]["uuid"] == "uuid-1"

        with patch.object(
            s, "_snapshot_live_config", wraps=s._snapshot_live_config
        ) as snap:
            s.add_account()  # refresh-in-place path
        snap.assert_called_once_with()

    def test_add_token_synthesizes_config_through_hook(self, temp_home: Path):
        s = _switcher()
        with patch.object(s, "_synth_config_for", wraps=s._synth_config_for) as synth:
            s.add_account_from_token("sk-ant-oat01-token")
        email = "setup-token-1@token.local"
        synth.assert_called_once_with({"email": email})
        assert s._read_account_config("1", email) == json.dumps({
            "oauthAccount": {
                "emailAddress": email,
                "accountUuid": "",
                "organizationUuid": None,
                "organizationName": None,
            }
        })

    def test_resync_rotated_backup(self, temp_home: Path):
        s = _switcher()
        _two_accounts(s)
        _live(A, _oauth("1-rot"), "uuid-1")
        resolved = {"uuid": "uuid-1", "email": A, "organizationUuid": None}
        with (
            patch.object(s, "_resolve_token_identity", return_value=resolved) as res,
            patch.object(
                s, "_live_credentials_lock", wraps=s._live_credentials_lock
            ) as lock,
        ):
            s._resync_rotated_backup("1", A, "", _oauth("1-rot"))
        res.assert_called_once_with(_oauth("1-rot"))
        lock.assert_called_once_with()
        assert s._read_account_credentials("1", A) == _oauth("1-rot")

    def test_store_env_guard_gates_both_consumers(self, temp_home: Path):
        s = _switcher()
        _two_accounts(s)
        with patch.object(s, "_store_env_guard", return_value=True) as guard:
            assert s.consume_backup_grant("2", B, _oauth("2")).error == (
                "store-unmirrored"
            )
            record = s._fetch_active_usage("1", A, _oauth("1", expires=1))
            assert record.error == "store-unmirrored"
        assert guard.call_count == 2

    def test_list_reads_running_instances_through_hook(self, temp_home: Path):
        s = _switcher()
        with patch.object(s, "_running_instances", return_value=([], [])) as hook:
            s.list_accounts(fetch=set())
        hook.assert_called_once_with()

    def test_session_hooks(self, temp_home: Path):
        s = _switcher()
        _two_accounts(s)
        sdir = s._session_dir("2", B)

        with patch.object(s, "_scan_live_sessions", return_value=([], 0)) as scan:
            assert s._live_session_pids("2", B) == []
            s._ensure_no_live_session("2", B, "the test")
        assert scan.call_count == 3
        scan.assert_called_with(sdir)

        with (
            patch.object(
                s, "_read_session_credentials", return_value=_oauth("2-session")
            ) as creds,
            patch.object(
                s, "_read_session_identity", return_value=("other@example.com", "")
            ) as identity,
        ):
            lines = s._token_status_lines((2, B, "", "", False, _oauth("2"), None))
        creds.assert_called_once_with(sdir)
        identity.assert_called_once_with(sdir)
        assert "session profile: ignored (different account)" in lines

        newer = _oauth("2-newer", expires=FAR + 1)
        with (
            patch.object(s, "_read_session_credentials", return_value=newer),
            patch.object(s, "_read_session_identity", return_value=(B, "")),
        ):
            assert s._session_profile_ahead("2", B, "") == newer

        with patch.object(s, "_profile_is_quiescent", return_value=False) as quiet:
            assert s._adopt_session_credential("2", B, "") is False
        quiet.assert_called_once_with(sdir)
