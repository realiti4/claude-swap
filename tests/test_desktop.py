"""Tests for macOS Claude Desktop account switching and session sharing."""

from __future__ import annotations

import json
import plistlib
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from claude_swap.desktop import (
    DesktopManager,
    DesktopProfile,
    DesktopProfileStore,
    DesktopSessionSync,
    DesktopSyncResult,
)
from claude_swap.exceptions import DesktopError, SwitchError
from claude_swap.process_detection import ClaudeSession


ACCOUNT_A = "11111111-1111-4111-8111-111111111111"
ACCOUNT_B = "22222222-2222-4222-8222-222222222222"
ORG_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ORG_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
CLI_SESSION = "33333333-3333-4333-8333-333333333333"
LOCAL_SESSION = "local_44444444-4444-4444-8444-444444444444"


def _write_transcript(config_dir: Path, cli_session_id: str = CLI_SESSION) -> None:
    project = config_dir / "projects" / "project"
    project.mkdir(parents=True, exist_ok=True)
    (project / f"{cli_session_id}.jsonl").write_text("{}\n", encoding="utf-8")


def _write_entry(
    sessions_root: Path,
    account: str,
    org: str,
    *,
    session_id: str = LOCAL_SESSION,
    cli_session_id: str | None = CLI_SESSION,
    unavailable: bool = False,
    **extra,
) -> Path:
    path = sessions_root / account / org / f"{session_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "sessionId": session_id,
        "cliSessionId": cli_session_id,
        "title": "Shared task",
        **extra,
    }
    if unavailable:
        data["transcriptUnavailable"] = True
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class TestDesktopSessionSync:
    def test_copies_resumable_entry_and_removes_account_bound_fields(self, tmp_path):
        user_data = tmp_path / "Claude"
        config_dir = tmp_path / ".claude"
        sessions_root = user_data / "claude-code-sessions"
        _write_transcript(config_dir)
        source = _write_entry(
            sessions_root,
            ACCOUNT_A,
            ORG_A,
            bridgeSessionIds=["old-server-id"],
            error="out of usage credits",
            errorAt="2026-01-01T00:00:00Z",
        )

        result = DesktopSessionSync(user_data, config_dir).sync(ACCOUNT_B, ORG_B)

        assert result == DesktopSyncResult(copied=1)
        destination = sessions_root / ACCOUNT_B / ORG_B / source.name
        copied = json.loads(destination.read_text(encoding="utf-8"))
        assert copied["sessionId"] == LOCAL_SESSION
        assert copied["cliSessionId"] == CLI_SESSION
        assert "bridgeSessionIds" not in copied
        assert "error" not in copied
        assert "errorAt" not in copied
        assert json.loads(source.read_text(encoding="utf-8"))["bridgeSessionIds"] == [
            "old-server-id"
        ]

    def test_skips_unavailable_and_missing_transcripts(self, tmp_path):
        user_data = tmp_path / "Claude"
        config_dir = tmp_path / ".claude"
        sessions_root = user_data / "claude-code-sessions"
        _write_transcript(config_dir)
        _write_entry(sessions_root, ACCOUNT_A, ORG_A, unavailable=True)
        _write_entry(
            sessions_root,
            ACCOUNT_A,
            ORG_A,
            session_id="local_missing-transcript",
            cli_session_id="55555555-5555-4555-8555-555555555555",
        )

        result = DesktopSessionSync(user_data, config_dir).sync(ACCOUNT_B, ORG_B)

        assert result == DesktopSyncResult(unavailable=2)
        assert not (sessions_root / ACCOUNT_B).exists()

    def test_existing_session_in_active_target_org_wins(self, tmp_path):
        user_data = tmp_path / "Claude"
        config_dir = tmp_path / ".claude"
        sessions_root = user_data / "claude-code-sessions"
        _write_transcript(config_dir)
        _write_entry(sessions_root, ACCOUNT_A, ORG_A, title="source")
        target = _write_entry(sessions_root, ACCOUNT_B, ORG_B, title="target")

        result = DesktopSessionSync(user_data, config_dir).sync(ACCOUNT_B, ORG_B)

        assert result == DesktopSyncResult(existing=1)
        assert json.loads(target.read_text(encoding="utf-8"))["title"] == "target"
        assert not (sessions_root / ACCOUNT_B / ORG_A / target.name).exists()

    def test_copies_entry_from_another_org_of_same_account(self, tmp_path):
        user_data = tmp_path / "Claude"
        config_dir = tmp_path / ".claude"
        sessions_root = user_data / "claude-code-sessions"
        _write_transcript(config_dir)
        source = _write_entry(sessions_root, ACCOUNT_B, ORG_A)

        result = DesktopSessionSync(user_data, config_dir).sync(ACCOUNT_B, ORG_B)

        assert result == DesktopSyncResult(copied=1)
        destination = sessions_root / ACCOUNT_B / ORG_B / source.name
        assert destination.is_file()
        assert source.is_file()

    def test_dry_run_reports_copy_without_writing(self, tmp_path):
        user_data = tmp_path / "Claude"
        config_dir = tmp_path / ".claude"
        sessions_root = user_data / "claude-code-sessions"
        _write_transcript(config_dir)
        _write_entry(sessions_root, ACCOUNT_A, ORG_A)

        result = DesktopSessionSync(user_data, config_dir).sync(
            ACCOUNT_B, ORG_B, dry_run=True
        )

        assert result == DesktopSyncResult(copied=1)
        assert not (sessions_root / ACCOUNT_B).exists()

    def test_new_entry_writer_never_replaces_existing_file(self, tmp_path):
        destination = tmp_path / "org" / f"{LOCAL_SESSION}.json"
        destination.parent.mkdir()
        destination.write_text('{"title": "keep"}', encoding="utf-8")

        created = DesktopSessionSync._write_new_entry(
            destination, {"title": "replace"}
        )

        assert created is False
        assert json.loads(destination.read_text(encoding="utf-8")) == {
            "title": "keep"
        }

    def test_rejects_invalid_account_uuid(self, tmp_path):
        syncer = DesktopSessionSync(tmp_path / "Claude", tmp_path / ".claude")
        with pytest.raises(DesktopError, match="valid account UUID"):
            syncer.sync("not-a-uuid", ORG_B)

    def test_rejects_invalid_organization_uuid(self, tmp_path):
        syncer = DesktopSessionSync(tmp_path / "Claude", tmp_path / ".claude")
        with pytest.raises(DesktopError, match="valid organization UUID"):
            syncer.sync(ACCOUNT_B, "not-a-uuid")

    def test_rejects_missing_desktop_index(self, tmp_path):
        syncer = DesktopSessionSync(tmp_path / "Claude", tmp_path / ".claude")
        with pytest.raises(DesktopError, match="session index not found"):
            syncer.sync(ACCOUNT_B, ORG_B)


class TestDesktopProfileStore:
    def test_dry_run_predicts_profile_without_writing(self, tmp_path):
        default = tmp_path / "Application Support" / "Claude"
        (default / "claude-code-sessions").mkdir(parents=True)
        backup = tmp_path / "backup"
        store = DesktopProfileStore(backup, default_profile_dir=default)

        profile = store.resolve(
            ACCOUNT_B,
            "user@example.com",
            current_account_uuid=None,
            current_email="",
            current_profile_dir=default,
            dry_run=True,
        )

        assert profile.path == backup / "desktop" / ACCOUNT_B
        assert profile.initialized is False
        assert not backup.exists()

    def test_adopts_default_and_creates_shared_session_link(self, tmp_path):
        default = tmp_path / "Application Support" / "Claude"
        shared = default / "claude-code-sessions"
        shared.mkdir(parents=True)
        store = DesktopProfileStore(tmp_path / "backup", default_profile_dir=default)

        profile = store.resolve(
            ACCOUNT_B,
            "user@example.com",
            current_account_uuid=ACCOUNT_A,
            current_email="old@example.com",
            current_profile_dir=default,
        )

        link = profile.path / "claude-code-sessions"
        assert link.is_symlink()
        assert link.resolve() == shared.resolve()
        registry = json.loads(store.registry_path.read_text(encoding="utf-8"))
        assert registry["profiles"][ACCOUNT_A]["isDefault"] is True
        assert registry["profiles"][ACCOUNT_B]["initialized"] is False

    def test_refuses_to_replace_private_session_data(self, tmp_path):
        default = tmp_path / "Claude"
        (default / "claude-code-sessions").mkdir(parents=True)
        backup = tmp_path / "backup"
        private = backup / "desktop" / ACCOUNT_B / "claude-code-sessions"
        private.mkdir(parents=True)
        store = DesktopProfileStore(backup, default_profile_dir=default)

        with pytest.raises(DesktopError, match="private session data"):
            store.resolve(
                ACCOUNT_B,
                "user@example.com",
                current_account_uuid=None,
                current_email="",
                current_profile_dir=default,
            )

    def test_marks_profile_initialized(self, tmp_path):
        default = tmp_path / "Claude"
        (default / "claude-code-sessions").mkdir(parents=True)
        store = DesktopProfileStore(tmp_path / "backup", default_profile_dir=default)
        profile = store.resolve(
            ACCOUNT_B,
            "user@example.com",
            current_account_uuid=None,
            current_email="",
            current_profile_dir=default,
        )

        initialized = store.mark_initialized(profile)

        assert initialized.initialized is True
        stored = json.loads(store.registry_path.read_text(encoding="utf-8"))
        assert stored["profiles"][ACCOUNT_B]["initialized"] is True

        uninitialized = store.mark_uninitialized(initialized)

        assert uninitialized.initialized is False
        stored = json.loads(store.registry_path.read_text(encoding="utf-8"))
        assert stored["profiles"][ACCOUNT_B]["initialized"] is False


def _authenticated_profile(tmp_path: Path) -> DesktopProfile:
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    indexed = profile_dir / "IndexedDB" / "https_claude.ai_0.indexeddb.leveldb"
    indexed.mkdir(parents=True)
    (indexed / "000003.log").write_bytes(b"prefix" + ACCOUNT_B.encode() + b"suffix")
    return DesktopProfile(
        account_uuid=ACCOUNT_B,
        email="user@example.com",
        path=profile_dir,
        is_default=False,
        initialized=False,
    )


class TestDesktopProfileIdentity:
    def test_profile_storage_contains_account(self, tmp_path):
        profile = _authenticated_profile(tmp_path)

        assert DesktopManager._profile_matches_account(profile, ACCOUNT_B) is True
        assert DesktopManager._profile_matches_account(profile, ACCOUNT_A) is False

    def test_missing_account_identity_is_not_authenticated(self, tmp_path):
        profile = DesktopProfile(
            ACCOUNT_B,
            "user@example.com",
            tmp_path,
            False,
            False,
        )
        indexed = tmp_path / "IndexedDB"
        indexed.mkdir()
        (indexed / "data").write_text("no account here", encoding="ascii")

        assert DesktopManager._profile_matches_account(profile, ACCOUNT_B) is False


def _switcher() -> MagicMock:
    switcher = MagicMock()
    switcher.resolve_account.return_value = ("2", "user@example.com", ORG_B)
    switcher.account_identity.return_value = {
        "email": "user@example.com",
        "organizationUuid": ORG_B,
        "uuid": ACCOUNT_B,
    }
    return switcher


class TestDesktopManager:
    def test_launches_managed_profile_through_gui_session(self, tmp_path):
        switcher = _switcher()
        store = MagicMock()
        store.default_profile_dir = tmp_path / "default"
        manager = DesktopManager(
            switcher, syncer=MagicMock(), profile_store=store
        )
        profile = DesktopProfile(
            ACCOUNT_B, "user@example.com", tmp_path / "profile", False, True
        )
        completed = MagicMock(returncode=0, stderr="")

        with patch.object(Path, "is_file", return_value=True), \
             patch("claude_swap.desktop.subprocess.run", return_value=completed) as run, \
             patch.object(manager, "_desktop_is_running", return_value=True):
            manager._launch_desktop(profile)

        run.assert_called_once_with(
            [
                "/usr/bin/open",
                "-n",
                "-b",
                "com.anthropic.claudefordesktop",
                "--env",
                f"CLAUDE_USER_DATA_DIR={profile.path}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_running_profile_path_drops_ps_trailing_newline(self):
        switcher = _switcher()
        store = MagicMock()
        store.default_profile_dir = Path("/default")
        manager = DesktopManager(
            switcher, syncer=MagicMock(), profile_store=store
        )
        ps_result = MagicMock(
            returncode=0,
            stdout=(
                "/Applications/Claude.app/Contents/MacOS/Claude "
                "CLAUDE_USER_DATA_DIR=/profiles/b\n"
            ),
        )

        with patch.object(manager, "_desktop_pids", return_value=[42]), \
             patch("claude_swap.desktop.subprocess.run", return_value=ps_result):
            profile = manager._running_profile_dir()

        assert profile == Path("/profiles/b")

    def test_dry_run_never_switches_or_touches_app(self):
        switcher = _switcher()
        syncer = MagicMock()
        syncer.sync.return_value = DesktopSyncResult(copied=3, existing=2)
        store = MagicMock()
        store.default_profile_dir = Path("/default")
        target = DesktopProfile(
            ACCOUNT_B, "user@example.com", Path("/profiles/b"), False, False
        )
        store.resolve.return_value = target
        manager = DesktopManager(switcher, syncer=syncer, profile_store=store)

        with patch("claude_swap.desktop.sys.platform", "darwin"), \
             patch.object(manager, "_desktop_is_running", return_value=False), \
             patch.object(manager, "_live_identity", return_value=(ACCOUNT_A, "old@example.com")), \
             patch.object(manager, "_running_profile_dir", return_value=Path("/default")), \
             patch.object(manager, "_quit_desktop") as quit_desktop, \
             patch.object(manager, "_launch_desktop") as launch:
            payload = manager.run("2", dry_run=True)

        syncer.sync.assert_called_once_with(ACCOUNT_B, ORG_B, dry_run=True)
        switcher.switch_to.assert_not_called()
        quit_desktop.assert_not_called()
        launch.assert_not_called()
        assert payload["dryRun"] is True
        assert payload["loginRequired"] is True
        assert payload["sessions"]["copied"] == 3

    def test_switches_syncs_and_relaunches(self):
        switcher = _switcher()
        switcher._get_sequence_data.return_value = {
            "accounts": {"1": {"uuid": ACCOUNT_A}}
        }
        syncer = MagicMock()
        syncer.claude_config_dir = Path("/tmp/claude-test")
        syncer.sync.return_value = DesktopSyncResult(copied=2)
        store = MagicMock()
        store.default_profile_dir = Path("/default")
        original = DesktopProfile(
            ACCOUNT_A, "old@example.com", Path("/default"), True, True
        )
        target = DesktopProfile(
            ACCOUNT_B, "user@example.com", Path("/profiles/b"), False, True
        )
        store.resolve.side_effect = [original, target]
        store.mark_initialized.return_value = target
        manager = DesktopManager(switcher, syncer=syncer, profile_store=store)

        with patch("claude_swap.desktop.sys.platform", "darwin"), \
             patch.object(manager, "_blocking_sessions", return_value=[]), \
             patch.object(manager, "_desktop_is_running", return_value=True), \
             patch.object(manager, "_live_identity", return_value=(ACCOUNT_A, "old@example.com")), \
             patch.object(manager, "_running_profile_dir", return_value=Path("/default")), \
             patch.object(manager, "_quit_desktop") as quit_desktop, \
             patch.object(manager, "_launch_desktop") as launch, \
             patch.object(manager, "_wait_for_identity", return_value=True):
            payload = manager.run("2", json_output=True)

        quit_desktop.assert_called_once_with()
        switcher.switch_to.assert_called_once_with("2", json_output=True)
        assert syncer.sync.call_args_list == [
            call(ACCOUNT_B, ORG_B, dry_run=True),
            call(ACCOUNT_B, ORG_B),
        ]
        launch.assert_called_once_with(target)
        store.mark_initialized.assert_called_once_with(target)
        assert payload["loginRequired"] is False
        assert payload["sessions"]["copied"] == 2

    def test_new_profile_opens_for_one_time_login(self):
        switcher = _switcher()
        switcher._get_sequence_data.return_value = {
            "accounts": {"1": {"uuid": ACCOUNT_A}}
        }
        syncer = MagicMock()
        syncer.claude_config_dir = Path("/tmp/claude-test")
        syncer.sync.return_value = DesktopSyncResult()
        store = MagicMock()
        store.default_profile_dir = Path("/default")
        original = DesktopProfile(
            ACCOUNT_A, "old@example.com", Path("/default"), True, True
        )
        target = DesktopProfile(
            ACCOUNT_B, "user@example.com", Path("/profiles/b"), False, False
        )
        store.resolve.side_effect = [original, target]
        manager = DesktopManager(switcher, syncer=syncer, profile_store=store)

        with patch("claude_swap.desktop.sys.platform", "darwin"), \
             patch.object(manager, "_blocking_sessions", return_value=[]), \
             patch.object(manager, "_desktop_is_running", return_value=True), \
             patch.object(manager, "_live_identity", return_value=(ACCOUNT_A, "old@example.com")), \
             patch.object(manager, "_running_profile_dir", return_value=Path("/default")), \
             patch.object(manager, "_quit_desktop"), \
             patch.object(manager, "_launch_desktop") as launch, \
             patch.object(manager, "_profile_matches_account", return_value=False), \
             patch.object(manager, "_wait_for_identity") as wait:
            payload = manager.run("2")

        launch.assert_called_once_with(target)
        wait.assert_not_called()
        store.mark_initialized.assert_not_called()
        assert payload["loginRequired"] is True

    def test_already_active_profile_is_verified_without_switching(self):
        switcher = _switcher()
        syncer = MagicMock()
        syncer.sync.return_value = DesktopSyncResult(existing=2)
        store = MagicMock()
        store.default_profile_dir = Path("/default")
        target = DesktopProfile(
            ACCOUNT_B, "user@example.com", Path("/profiles/b"), False, False
        )
        initialized = DesktopProfile(
            ACCOUNT_B, "user@example.com", Path("/profiles/b"), False, True
        )
        store.resolve.side_effect = [target, target]
        store.mark_initialized.return_value = initialized
        manager = DesktopManager(switcher, syncer=syncer, profile_store=store)

        with patch("claude_swap.desktop.sys.platform", "darwin"), \
             patch.object(manager, "_desktop_is_running", return_value=True), \
             patch.object(manager, "_live_identity", return_value=(ACCOUNT_B, "user@example.com")), \
             patch.object(manager, "_running_profile_dir", return_value=Path("/profiles/b")), \
             patch.object(manager, "_wait_for_identity", return_value=True), \
             patch.object(manager, "_quit_desktop") as quit_desktop, \
             patch.object(manager, "_launch_desktop") as launch:
            payload = manager.run("2")

        switcher.switch_to.assert_not_called()
        quit_desktop.assert_not_called()
        launch.assert_not_called()
        assert payload["alreadyActive"] is True
        assert payload["loginRequired"] is False

    def test_confirms_visually_verified_active_profile(self):
        switcher = _switcher()
        syncer = MagicMock()
        syncer.sync.return_value = DesktopSyncResult(existing=2)
        store = MagicMock()
        store.default_profile_dir = Path("/default")
        target = DesktopProfile(
            ACCOUNT_B, "user@example.com", Path("/profiles/b"), False, False
        )
        initialized = DesktopProfile(
            ACCOUNT_B, "user@example.com", Path("/profiles/b"), False, True
        )
        store.resolve.side_effect = [target, target]
        store.mark_initialized.return_value = initialized
        manager = DesktopManager(switcher, syncer=syncer, profile_store=store)

        with patch("claude_swap.desktop.sys.platform", "darwin"), \
             patch.object(manager, "_desktop_is_running", return_value=True), \
             patch.object(manager, "_live_identity", return_value=(ACCOUNT_B, "user@example.com")), \
             patch.object(manager, "_running_profile_dir", return_value=Path("/profiles/b")), \
             patch.object(manager, "_wait_for_identity") as wait:
            payload = manager.run("2", confirm_login=True)

        store.mark_initialized.assert_called_once_with(target)
        wait.assert_not_called()
        assert payload["loginRequired"] is False

    def test_refuses_non_idle_desktop_task(self):
        switcher = _switcher()
        syncer = MagicMock()
        syncer.claude_config_dir = Path("/tmp/claude-test")
        store = MagicMock()
        store.default_profile_dir = Path("/default")
        original = DesktopProfile(
            ACCOUNT_A, "old@example.com", Path("/default"), True, True
        )
        target = DesktopProfile(
            ACCOUNT_B, "user@example.com", Path("/profiles/b"), False, True
        )
        store.resolve.side_effect = [original, target]
        manager = DesktopManager(switcher, syncer=syncer, profile_store=store)
        busy = ClaudeSession(
            pid=42,
            session_id="s",
            cwd="/tmp/project",
            started_at=0,
            kind="interactive",
            entrypoint="claude-desktop",
            status="busy",
        )

        with patch("claude_swap.desktop.sys.platform", "darwin"), \
             patch.object(manager, "_blocking_sessions", return_value=[busy]), \
             patch.object(manager, "_desktop_is_running", return_value=True), \
             patch.object(manager, "_live_identity", return_value=(ACCOUNT_A, "old@example.com")), \
             patch.object(manager, "_running_profile_dir", return_value=Path("/default")), \
             patch.object(manager, "_quit_desktop") as quit_desktop, \
             patch.object(manager, "_launch_desktop") as launch, \
             pytest.raises(DesktopError, match="active local task"):
            manager.run("2")

        switcher.switch_to.assert_not_called()
        quit_desktop.assert_not_called()
        launch.assert_not_called()

    def test_refuses_locked_console_before_closing(self):
        switcher = _switcher()
        switcher._get_sequence_data.return_value = {
            "accounts": {"1": {"uuid": ACCOUNT_A}}
        }
        syncer = MagicMock()
        syncer.claude_config_dir = Path("/tmp/claude-test")
        syncer.sync.return_value = DesktopSyncResult()
        store = MagicMock()
        store.default_profile_dir = Path("/default")
        original = DesktopProfile(
            ACCOUNT_A, "old@example.com", Path("/default"), True, True
        )
        target = DesktopProfile(
            ACCOUNT_B, "user@example.com", Path("/profiles/b"), False, True
        )
        store.resolve.side_effect = [original, target]
        manager = DesktopManager(switcher, syncer=syncer, profile_store=store)

        with patch("claude_swap.desktop.sys.platform", "darwin"), \
             patch.object(manager, "_blocking_sessions", return_value=[]), \
             patch.object(manager, "_desktop_is_running", return_value=True), \
             patch.object(manager, "_live_identity", return_value=(ACCOUNT_A, "old@example.com")), \
             patch.object(manager, "_running_profile_dir", return_value=Path("/default")), \
             patch.object(manager, "_console_is_unlocked", return_value=False), \
             patch.object(manager, "_quit_desktop") as quit_desktop, \
             pytest.raises(DesktopError, match="Mac is locked"):
            manager.run("2")

        quit_desktop.assert_not_called()
        switcher.switch_to.assert_not_called()

    def test_console_lock_probe_reads_ioreg_plist(self):
        unlocked = plistlib.dumps({"IOConsoleLocked": False})
        locked = plistlib.dumps({"IOConsoleLocked": True})

        with patch("claude_swap.desktop.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout=unlocked)
            assert DesktopManager._console_is_unlocked() is True
            run.return_value = MagicMock(returncode=0, stdout=locked)
            assert DesktopManager._console_is_unlocked() is False

    def test_relaunches_original_app_when_switch_fails(self):
        switcher = _switcher()
        switcher._get_sequence_data.return_value = {
            "accounts": {"1": {"uuid": ACCOUNT_A}}
        }
        switcher.switch_to.side_effect = SwitchError("boom")
        syncer = MagicMock()
        syncer.claude_config_dir = Path("/tmp/claude-test")
        store = MagicMock()
        store.default_profile_dir = Path("/default")
        original = DesktopProfile(
            ACCOUNT_A, "old@example.com", Path("/default"), True, True
        )
        target = DesktopProfile(
            ACCOUNT_B, "user@example.com", Path("/profiles/b"), False, True
        )
        store.resolve.side_effect = [original, target]
        manager = DesktopManager(switcher, syncer=syncer, profile_store=store)

        with patch("claude_swap.desktop.sys.platform", "darwin"), \
             patch.object(manager, "_blocking_sessions", return_value=[]), \
             patch.object(manager, "_desktop_is_running", return_value=True), \
             patch.object(manager, "_live_identity", return_value=(ACCOUNT_A, "old@example.com")), \
             patch.object(manager, "_running_profile_dir", return_value=Path("/default")), \
             patch.object(manager, "_quit_desktop"), \
             patch.object(manager, "_restore_original") as restore, \
             pytest.raises(SwitchError, match="boom"):
            manager.run("2")

        restore.assert_called_once_with(
            "1", original, was_running=True, switch_completed=False
        )
        syncer.sync.assert_called_once_with(ACCOUNT_B, ORG_B, dry_run=True)

    def test_confirmed_profile_reuses_isolated_login_without_private_storage_probe(self):
        switcher = _switcher()
        switcher._get_sequence_data.return_value = {
            "accounts": {"1": {"uuid": ACCOUNT_A}}
        }
        syncer = MagicMock()
        syncer.claude_config_dir = Path("/tmp/claude-test")
        syncer.sync.return_value = DesktopSyncResult()
        store = MagicMock()
        store.default_profile_dir = Path("/default")
        original = DesktopProfile(
            ACCOUNT_A, "old@example.com", Path("/default"), True, True
        )
        target = DesktopProfile(
            ACCOUNT_B, "user@example.com", Path("/profiles/b"), False, True
        )
        store.resolve.side_effect = [original, target]
        manager = DesktopManager(switcher, syncer=syncer, profile_store=store)

        with patch("claude_swap.desktop.sys.platform", "darwin"), \
             patch.object(manager, "_blocking_sessions", return_value=[]), \
             patch.object(manager, "_desktop_is_running", return_value=True), \
             patch.object(manager, "_live_identity", return_value=(ACCOUNT_A, "old@example.com")), \
             patch.object(manager, "_running_profile_dir", return_value=Path("/default")), \
             patch.object(manager, "_quit_desktop"), \
             patch.object(manager, "_launch_desktop"), \
             patch.object(manager, "_wait_for_identity") as wait, \
             patch.object(manager, "_restore_original") as restore:
            payload = manager.run("2")

        wait.assert_not_called()
        restore.assert_not_called()
        assert payload["loginRequired"] is False

    def test_refuses_untracked_current_account_before_closing(self):
        switcher = _switcher()
        switcher._get_sequence_data.return_value = {"accounts": {}}
        syncer = MagicMock()
        syncer.claude_config_dir = Path("/tmp/claude-test")
        syncer.sync.return_value = DesktopSyncResult()
        store = MagicMock()
        store.default_profile_dir = Path("/default")
        original = DesktopProfile(
            ACCOUNT_A, "old@example.com", Path("/default"), True, True
        )
        target = DesktopProfile(
            ACCOUNT_B, "user@example.com", Path("/profiles/b"), False, False
        )
        store.resolve.side_effect = [original, target]
        manager = DesktopManager(switcher, syncer=syncer, profile_store=store)

        with patch("claude_swap.desktop.sys.platform", "darwin"), \
             patch.object(manager, "_blocking_sessions", return_value=[]), \
             patch.object(manager, "_desktop_is_running", return_value=True), \
             patch.object(manager, "_live_identity", return_value=(ACCOUNT_A, "old@example.com")), \
             patch.object(manager, "_running_profile_dir", return_value=Path("/default")), \
             patch.object(manager, "_quit_desktop") as quit_desktop, \
             pytest.raises(DesktopError, match="not stored in cswap"):
            manager.run("2")

        quit_desktop.assert_not_called()
        switcher.switch_to.assert_not_called()

    def test_rejects_non_macos(self):
        manager = DesktopManager(
            _switcher(), syncer=MagicMock(), profile_store=MagicMock()
        )
        with patch("claude_swap.desktop.sys.platform", "linux"), \
             pytest.raises(DesktopError, match="macOS-only"):
            manager.run("2")
