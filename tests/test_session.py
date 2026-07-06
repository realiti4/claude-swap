"""Tests for session mode (claude_swap.session + the switcher guards)."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import unicodedata
from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_swap import oauth
from claude_swap import session as session_mod
from claude_swap.exceptions import AccountNotFoundError, SessionError
from claude_swap.models import Platform
from claude_swap.session import (
    SHARE_MANIFEST,
    SessionManager,
    _probe_env,
    keychain_service_name,
    live_sessions_for,
    session_dir_for,
    slugify_email,
)
from claude_swap.switcher import ClaudeAccountSwitcher

ACCOUNT_EMAIL = "account2@example.com"
ACCOUNT_NUM = "2"
ORG_UUID = "org-uuid-2"

CREDS = json.dumps(
    {
        "claudeAiOauth": {
            "accessToken": "stored-access",
            "refreshToken": "stored-refresh",
            "expiresAt": 1,
        }
    }
)
ROTATED_CREDS = json.dumps(
    {
        "claudeAiOauth": {
            "accessToken": "fresh-access",
            "refreshToken": "rotated-refresh",
            "expiresAt": 9999999999999,
        }
    }
)
CONFIG = json.dumps(
    {
        "oauthAccount": {
            "emailAddress": ACCOUNT_EMAIL,
            "accountUuid": "uuid-2",
            "organizationUuid": ORG_UUID,
        },
        "theme": "light",
    }
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def macos_platform(monkeypatch):
    """Force Platform.detect() to MACOS so keychain paths run on any host."""
    monkeypatch.setattr(Platform, "detect", classmethod(lambda cls: Platform.MACOS))


@pytest.fixture
def seeded_switcher(temp_home: Path, macos_platform) -> ClaudeAccountSwitcher:
    """A switcher with account 2 fully backed up (creds + config + sequence)."""
    switcher = ClaudeAccountSwitcher(debug=True)
    switcher._setup_directories()
    switcher._write_json(
        switcher.sequence_file,
        {
            "activeAccountNumber": 1,
            "lastUpdated": "2024-01-01T00:00:00Z",
            "sequence": [1, 2],
            "accounts": {
                "1": {
                    "email": "account1@example.com",
                    "uuid": "uuid-1",
                    "organizationUuid": "org-uuid-1",
                    "organizationName": "Org One",
                    "added": "2024-01-01T00:00:00Z",
                },
                ACCOUNT_NUM: {
                    "email": ACCOUNT_EMAIL,
                    "uuid": "uuid-2",
                    "organizationUuid": ORG_UUID,
                    "organizationName": "Org Two",
                    "added": "2024-01-02T00:00:00Z",
                },
            },
        },
    )
    switcher._write_account_credentials(ACCOUNT_NUM, ACCOUNT_EMAIL, CREDS)
    switcher._write_account_config(ACCOUNT_NUM, ACCOUNT_EMAIL, CONFIG)
    return switcher


@pytest.fixture
def manager(seeded_switcher) -> SessionManager:
    return SessionManager(seeded_switcher)


@pytest.fixture
def auth_status_tracks_seed(monkeypatch):
    """Fake `claude auth status --json`: logged in iff the profile is seeded.

    Reads CLAUDE_CONFIG_DIR from the probe env, so it also exercises that the
    probe points at the right profile. Records every probe env for assertions.
    """
    probe_envs: list[dict] = []

    def fake_run(cmd, env=None, **kwargs):
        probe_envs.append(env)
        config_dir = Path(env["CLAUDE_CONFIG_DIR"])
        if (config_dir / ".credentials.json").exists():
            payload = {
                "loggedIn": True,
                "authMethod": "claude.ai",
                "email": ACCOUNT_EMAIL,
                "orgId": ORG_UUID,
            }
        else:
            payload = {"loggedIn": False, "authMethod": "none"}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(session_mod.subprocess, "run", fake_run)
    return probe_envs


@pytest.fixture
def refresh_rotates(monkeypatch):
    calls: list[str] = []

    def fake_refresh(creds: str) -> str:
        calls.append(creds)
        return ROTATED_CREDS

    monkeypatch.setattr(session_mod, "refresh_oauth_credentials", fake_refresh)
    return calls


def make_live(session_dir: Path, pid: int | None = None) -> None:
    """Simulate a live claude instance in a profile (own PID is always alive)."""
    pid = pid or os.getpid()
    pid_dir = session_dir / "sessions"
    pid_dir.mkdir(parents=True, exist_ok=True)
    (pid_dir / f"{pid}.json").write_text(json.dumps({"pid": pid}))


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_slugify_plain(self):
        assert slugify_email("user@example.com") == "user_example.com"

    def test_slugify_plus_tag(self):
        assert slugify_email("user+tag@example.com") == "user_tag_example.com"

    def test_slugify_unicode(self):
        slug = slugify_email("bø@x.com")
        assert slug == "b__x.com"
        assert slug.isascii()

    def test_slugify_windows_illegal(self):
        slug = slugify_email('a<>:"/\\|?*b@x.com')
        assert not any(c in slug for c in '<>:"/\\|?*')

    def test_session_dir_naming(self, tmp_path):
        d = session_dir_for(tmp_path, "2", "user@example.com")
        assert d == tmp_path / "sessions" / "2-user_example.com"

    def test_keychain_service_name_known_vector(self, tmp_path):
        d = tmp_path / "profile"
        expected = hashlib.sha256(
            unicodedata.normalize("NFC", str(d)).encode()
        ).hexdigest()[:8]
        assert keychain_service_name(d) == f"Claude Code-credentials-{expected}"

    def test_keychain_service_name_nfc_nfd_equal(self):
        nfc = Path(unicodedata.normalize("NFC", "/tmp/sé"))
        nfd = Path(unicodedata.normalize("NFD", "/tmp/sé"))
        assert str(nfc) != str(nfd)  # sanity: inputs genuinely differ
        assert keychain_service_name(nfc) == keychain_service_name(nfd)

    def test_probe_env_drops_auth_overrides(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-key")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-tok")
        env = _probe_env(tmp_path)
        assert "ANTHROPIC_API_KEY" not in env
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
        assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path)

    def test_live_sessions_for_missing_dir(self, tmp_path):
        assert live_sessions_for(tmp_path / "nope") == []

    def test_live_sessions_for_dead_pid_ignored(self, tmp_path):
        make_live(tmp_path, pid=2**22 + 12345)  # vanishingly unlikely to exist
        assert live_sessions_for(tmp_path) == []

    def test_live_sessions_for_own_pid(self, tmp_path):
        make_live(tmp_path)
        assert [s.pid for s in live_sessions_for(tmp_path)] == [os.getpid()]


# ---------------------------------------------------------------------------
# resolve_account accessor
# ---------------------------------------------------------------------------


class TestResolveAccount:
    def test_by_number(self, seeded_switcher):
        assert seeded_switcher.resolve_account("2") == (
            ACCOUNT_NUM,
            ACCOUNT_EMAIL,
            ORG_UUID,
        )

    def test_by_email(self, seeded_switcher):
        num, email, org = seeded_switcher.resolve_account(ACCOUNT_EMAIL)
        assert (num, email) == (ACCOUNT_NUM, ACCOUNT_EMAIL)

    def test_unknown(self, seeded_switcher):
        with pytest.raises(AccountNotFoundError):
            seeded_switcher.resolve_account("9")

    def test_unknown_email(self, seeded_switcher):
        with pytest.raises(AccountNotFoundError):
            seeded_switcher.resolve_account("nobody@example.com")


# ---------------------------------------------------------------------------
# bootstrap
# ---------------------------------------------------------------------------


class TestBootstrap:
    def test_happy_path(
        self, manager, seeded_switcher, auth_status_tracks_seed, refresh_rotates
    ):
        session_dir, num, email = manager.setup_session("2", share=False)

        assert (num, email) == (ACCOUNT_NUM, ACCOUNT_EMAIL)
        creds_path = session_dir / ".credentials.json"
        assert creds_path.read_text() == ROTATED_CREDS

        config = json.loads((session_dir / ".claude.json").read_text())
        assert config["oauthAccount"]["emailAddress"] == ACCOUNT_EMAIL
        assert config["hasCompletedOnboarding"] is True
        assert config["theme"] == "light"  # carried over from backup config

        # Rotated refresh token persisted back to backup storage.
        assert (
            seeded_switcher.read_account_credentials(ACCOUNT_NUM, ACCOUNT_EMAIL)
            == ROTATED_CREDS
        )

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
    def test_profile_permissions(self, manager, auth_status_tracks_seed, refresh_rotates):
        session_dir, _, _ = manager.setup_session("2", share=False)
        assert (session_dir.stat().st_mode & 0o777) == 0o700
        assert ((session_dir / ".credentials.json").stat().st_mode & 0o777) == 0o600
        assert ((session_dir / ".claude.json").stat().st_mode & 0o777) == 0o600

    def test_reuse_skips_refresh_and_writes(
        self, manager, seeded_switcher, auth_status_tracks_seed, refresh_rotates
    ):
        session_dir, _, _ = manager.setup_session("2", share=False)
        first_creds = (session_dir / ".credentials.json").read_text()
        refresh_calls_after_bootstrap = len(refresh_rotates)

        session_dir2, _, _ = manager.setup_session("2", share=False)

        assert session_dir2 == session_dir
        assert len(refresh_rotates) == refresh_calls_after_bootstrap  # no new refresh
        assert (session_dir / ".credentials.json").read_text() == first_creds

    def test_refresh_failure_uses_stored_creds(
        self, manager, auth_status_tracks_seed, monkeypatch, capsys
    ):
        monkeypatch.setattr(session_mod, "refresh_oauth_credentials", lambda c: None)
        session_dir, _, _ = manager.setup_session("2", share=False)
        assert (session_dir / ".credentials.json").read_text() == CREDS
        assert "Could not refresh" in capsys.readouterr().out

    def test_setup_token_account_skips_refresh_silently(
        self, manager, seeded_switcher, auth_status_tracks_seed, monkeypatch, capsys
    ):
        """--add-token accounts have no refresh token; no attempt, no warning."""
        token_creds = json.dumps(
            {"claudeAiOauth": {"accessToken": "sk-ant-oat01-x", "expiresAt": 0}}
        )
        seeded_switcher._write_account_credentials(ACCOUNT_NUM, ACCOUNT_EMAIL, token_creds)
        refresh_calls = []
        monkeypatch.setattr(
            session_mod,
            "refresh_oauth_credentials",
            lambda c: refresh_calls.append(c) or None,
        )

        session_dir, _, _ = manager.setup_session("2", share=False)

        assert refresh_calls == []
        assert "Could not refresh" not in capsys.readouterr().out
        assert (session_dir / ".credentials.json").read_text() == token_creds

    def test_missing_credentials(self, manager, seeded_switcher, auth_status_tracks_seed):
        seeded_switcher._delete_account_credentials(ACCOUNT_NUM, ACCOUNT_EMAIL)
        with pytest.raises(SessionError, match="no stored credentials"):
            manager.setup_session("2", share=False)

    def test_missing_config(
        self, manager, seeded_switcher, auth_status_tracks_seed, refresh_rotates
    ):
        config_file = (
            seeded_switcher.configs_dir
            / f".claude-config-{ACCOUNT_NUM}-{ACCOUNT_EMAIL}.json"
        )
        config_file.unlink()
        with pytest.raises(SessionError, match="no stored config backup"):
            manager.setup_session("2", share=False)

    def test_validation_failure_cleans_up(
        self, manager, seeded_switcher, monkeypatch, refresh_rotates, block_real_keychain
    ):
        # Auth status never reports logged in → post-bootstrap validation fails.
        def always_invalid(cmd, env=None, **kwargs):
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"loggedIn": False, "authMethod": "none"}),
                stderr="",
            )

        monkeypatch.setattr(session_mod.subprocess, "run", always_invalid)
        session_dir = session_dir_for(
            seeded_switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )
        # A stale hashed-keychain entry from an earlier profile at this path.
        service = keychain_service_name(session_dir)
        account = session_mod._keychain_account_name()
        block_real_keychain.set_password(service, account, "stale")

        with pytest.raises(SessionError, match="failed\\s+validation"):
            manager.setup_session("2", share=False)

        assert not session_dir.exists()
        assert block_real_keychain.get_password(service, account) is None

    def test_stale_keychain_entry_deleted_before_seed(
        self,
        manager,
        seeded_switcher,
        auth_status_tracks_seed,
        refresh_rotates,
        block_real_keychain,
    ):
        session_dir = session_dir_for(
            seeded_switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )
        service = keychain_service_name(session_dir)
        account = session_mod._keychain_account_name()
        block_real_keychain.set_password(service, account, "stale")

        manager.setup_session("2", share=False)

        assert block_real_keychain.get_password(service, account) is None

    def test_stale_marker_forces_rebootstrap_after_session_exits(
        self, manager, seeded_switcher, auth_status_tracks_seed, refresh_rotates
    ):
        """Backup creds updated while the session was live → after it exits,
        the next run must re-bootstrap from the fresh backup even though the
        stale profile would still pass the local reuse check."""
        session_dir, _, _ = manager.setup_session("2", share=False)
        (session_dir / ".credentials.json").write_text("stale lineage")
        (session_dir / session_mod.STALE_MARKER).touch()
        # No live PID files → the session has exited.

        manager.setup_session("2", share=False)

        # Re-bootstrapped: fresh (refreshed) creds, marker cleared.
        assert (session_dir / ".credentials.json").read_text() == ROTATED_CREDS
        assert not (session_dir / session_mod.STALE_MARKER).exists()

    def test_stale_marker_preserved_while_session_still_live(
        self, manager, seeded_switcher, auth_status_tracks_seed, refresh_rotates
    ):
        """A second `cswap run` joining a live session must not invalidate
        under the running claude; the marker survives for later."""
        session_dir, _, _ = manager.setup_session("2", share=False)
        (session_dir / ".credentials.json").write_text("live lineage")
        (session_dir / session_mod.STALE_MARKER).touch()
        make_live(session_dir)

        manager.setup_session("2", share=False)

        assert (session_dir / ".credentials.json").read_text() == "live lineage"
        assert (session_dir / session_mod.STALE_MARKER).exists()

    def test_rebootstrap_preserves_profile_history(
        self, manager, seeded_switcher, auth_status_tracks_seed, refresh_rotates
    ):
        session_dir, _, _ = manager.setup_session("2", share=False)
        # Simulate claude having written its own state, then creds invalidated.
        config = json.loads((session_dir / ".claude.json").read_text())
        config["projects"] = {"/some/project": {"history": ["x"]}}
        (session_dir / ".claude.json").write_text(json.dumps(config))
        (session_dir / ".credentials.json").unlink()

        manager.setup_session("2", share=False)

        merged = json.loads((session_dir / ".claude.json").read_text())
        assert merged["projects"] == {"/some/project": {"history": ["x"]}}
        assert merged["oauthAccount"]["emailAddress"] == ACCOUNT_EMAIL


# ---------------------------------------------------------------------------
# validation strictness
# ---------------------------------------------------------------------------


class TestIsSessionValid:
    @pytest.fixture
    def valid_payload(self):
        return {
            "loggedIn": True,
            "authMethod": "claude.ai",
            "email": ACCOUNT_EMAIL,
            "orgId": ORG_UUID,
        }

    def check(self, manager, tmp_path, monkeypatch, payload, rc=0) -> bool:
        tmp_path.mkdir(exist_ok=True)
        monkeypatch.setattr(
            session_mod.subprocess,
            "run",
            lambda *a, **k: SimpleNamespace(
                returncode=rc, stdout=json.dumps(payload), stderr=""
            ),
        )
        return manager._is_session_valid(tmp_path, ACCOUNT_EMAIL, ORG_UUID)

    def test_valid(self, manager, tmp_path, monkeypatch, valid_payload):
        assert self.check(manager, tmp_path, monkeypatch, valid_payload)

    def test_rejects_api_key_auth(self, manager, tmp_path, monkeypatch, valid_payload):
        valid_payload["authMethod"] = "apiKey"
        assert not self.check(manager, tmp_path, monkeypatch, valid_payload)

    def test_rejects_wrong_email(self, manager, tmp_path, monkeypatch, valid_payload):
        valid_payload["email"] = "other@example.com"
        assert not self.check(manager, tmp_path, monkeypatch, valid_payload)

    def test_rejects_wrong_org(self, manager, tmp_path, monkeypatch, valid_payload):
        valid_payload["orgId"] = "different-org"
        assert not self.check(manager, tmp_path, monkeypatch, valid_payload)

    def test_lenient_when_org_absent(self, manager, tmp_path, monkeypatch, valid_payload):
        del valid_payload["orgId"]
        assert self.check(manager, tmp_path, monkeypatch, valid_payload)

    def test_rejects_nonzero_exit(self, manager, tmp_path, monkeypatch, valid_payload):
        assert not self.check(manager, tmp_path, monkeypatch, valid_payload, rc=1)

    def test_rejects_missing_dir(self, manager, tmp_path, monkeypatch):
        assert not manager._is_session_valid(
            tmp_path / "missing", ACCOUNT_EMAIL, ORG_UUID
        )

    def test_invokes_pathext_resolved_launcher(
        self, manager, tmp_path, monkeypatch, valid_payload
    ):
        """The probe must call the resolved launcher, not bare "claude".

        On Windows `claude` is a `.cmd` shim that a bare "claude" won't
        resolve, so validation would always fail. Use shutil.which instead.
        """
        tmp_path.mkdir(exist_ok=True)
        resolved = "/fake/bin/claude.CMD"
        monkeypatch.setattr(session_mod.shutil, "which", lambda name: resolved)
        seen_argv = {}

        def capture_run(argv, *a, **k):
            seen_argv["argv"] = argv
            return SimpleNamespace(
                returncode=0, stdout=json.dumps(valid_payload), stderr=""
            )

        monkeypatch.setattr(session_mod.subprocess, "run", capture_run)
        assert manager._is_session_valid(tmp_path, ACCOUNT_EMAIL, ORG_UUID)
        assert seen_argv["argv"][0] == resolved


# ---------------------------------------------------------------------------
# sharing
# ---------------------------------------------------------------------------


@pytest.fixture
def share_setup(temp_home: Path, seeded_switcher):
    """Source items in ~/.claude and an existing (seeded-enough) session dir."""
    source = temp_home / ".claude"
    (source / "settings.json").write_text("{}")
    (source / "CLAUDE.md").write_text("# memory")
    (source / "skills").mkdir()
    (source / "skills" / "a.md").write_text("skill")

    session_dir = session_dir_for(seeded_switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL)
    session_dir.mkdir(parents=True)
    return source, session_dir, SessionManager(seeded_switcher)


@pytest.mark.skipif(sys.platform == "win32", reason="symlink mode is POSIX-only")
class TestSharingPosix:
    def test_links_existing_sources_only(self, share_setup):
        source, session_dir, mgr = share_setup
        mgr._sync_sharing(session_dir, share=True)

        assert (session_dir / "settings.json").is_symlink()
        assert (session_dir / "CLAUDE.md").is_symlink()
        assert (session_dir / "skills").is_symlink()
        assert not (session_dir / "keybindings.json").exists()  # no source
        manifest = json.loads((session_dir / SHARE_MANIFEST).read_text())
        assert set(manifest["items"]) == {"settings.json", "CLAUDE.md", "skills"}
        assert manifest["mode"] == "symlink"

    def test_idempotent(self, share_setup):
        source, session_dir, mgr = share_setup
        mgr._sync_sharing(session_dir, share=True)
        mgr._sync_sharing(session_dir, share=True)
        assert (session_dir / "settings.json").readlink() == source / "settings.json"

    def test_prunes_when_source_vanishes(self, share_setup):
        source, session_dir, mgr = share_setup
        mgr._sync_sharing(session_dir, share=True)
        (source / "CLAUDE.md").unlink()
        mgr._sync_sharing(session_dir, share=True)

        assert not (session_dir / "CLAUDE.md").is_symlink()
        manifest = json.loads((session_dir / SHARE_MANIFEST).read_text())
        assert "CLAUDE.md" not in manifest["items"]

    def test_never_touches_user_data(self, share_setup, capsys):
        source, session_dir, mgr = share_setup
        (session_dir / "CLAUDE.md").write_text("session-private memory")

        mgr._sync_sharing(session_dir, share=True)

        assert not (session_dir / "CLAUDE.md").is_symlink()
        assert (session_dir / "CLAUDE.md").read_text() == "session-private memory"
        assert "Not sharing CLAUDE.md" in capsys.readouterr().out
        manifest = json.loads((session_dir / SHARE_MANIFEST).read_text())
        assert "CLAUDE.md" not in manifest["items"]

    def test_no_share_removes_only_managed(self, share_setup):
        source, session_dir, mgr = share_setup
        (session_dir / "private.txt").write_text("keep me")
        mgr._sync_sharing(session_dir, share=True)

        mgr._sync_sharing(session_dir, share=False)

        assert not (session_dir / "settings.json").exists()
        assert not (session_dir / "skills").exists()
        assert (session_dir / "private.txt").read_text() == "keep me"
        assert not (session_dir / SHARE_MANIFEST).exists()

    def test_repoints_stale_link(self, share_setup, temp_home):
        source, session_dir, mgr = share_setup
        elsewhere = temp_home / "elsewhere.json"
        elsewhere.write_text("{}")
        (session_dir / "settings.json").symlink_to(elsewhere)

        mgr._sync_sharing(session_dir, share=True)

        assert (session_dir / "settings.json").readlink() == source / "settings.json"


class TestSharingWindowsMode:
    """Copy mode, exercised by forcing the platform (runs on any host)."""

    @pytest.fixture
    def windows_mgr(self, share_setup):
        source, session_dir, mgr = share_setup
        mgr.switcher.platform = Platform.WINDOWS
        return source, session_dir, mgr

    def test_copies_instead_of_links(self, windows_mgr):
        source, session_dir, mgr = windows_mgr
        mgr._sync_sharing(session_dir, share=True)

        assert (session_dir / "settings.json").is_file()
        assert not (session_dir / "settings.json").is_symlink()
        assert (session_dir / "skills" / "a.md").read_text() == "skill"
        manifest = json.loads((session_dir / SHARE_MANIFEST).read_text())
        assert manifest["mode"] == "copy"

    def test_resync_overwrites_managed_copies(self, windows_mgr):
        source, session_dir, mgr = windows_mgr
        mgr._sync_sharing(session_dir, share=True)
        (source / "settings.json").write_text('{"changed": true}')

        mgr._sync_sharing(session_dir, share=True)

        assert (session_dir / "settings.json").read_text() == '{"changed": true}'

    def test_no_share_removes_copies(self, windows_mgr):
        source, session_dir, mgr = windows_mgr
        mgr._sync_sharing(session_dir, share=True)
        mgr._sync_sharing(session_dir, share=False)

        assert not (session_dir / "settings.json").exists()
        assert not (session_dir / "skills").exists()


# ---------------------------------------------------------------------------
# run() / exec handoff
# ---------------------------------------------------------------------------


class _ExecCalled(Exception):
    def __init__(self, binary, argv, env):
        self.binary, self.argv, self.env = binary, argv, env


@pytest.fixture
def capture_exec(monkeypatch):
    # Patch the handoff at the _exec() seam rather than the primitive beneath
    # it: _exec() dispatches to os.execvpe on POSIX but subprocess.run on
    # Windows, and patching subprocess.run here would also swallow the
    # `claude auth status` probe that some of these tests stub separately.
    def fake_exec(self, claude_bin, claude_args, env):
        raise _ExecCalled(claude_bin, [claude_bin, *claude_args], env)

    monkeypatch.setattr(session_mod.SessionManager, "_exec", fake_exec)
    monkeypatch.setattr(
        session_mod.shutil, "which", lambda name: f"/fake/bin/{name}"
    )


class TestRun:
    def test_claude_not_on_path(self, manager, monkeypatch):
        monkeypatch.setattr(session_mod.shutil, "which", lambda name: None)
        with pytest.raises(SessionError, match="not found on PATH"):
            manager.run("2", [])

    def test_exec_env_and_forwarded_args(
        self, manager, capture_exec, auth_status_tracks_seed, refresh_rotates
    ):
        with pytest.raises(_ExecCalled) as exc:
            manager.run("2", ["--resume", "--model", "x"])

        call = exc.value
        assert call.binary == "/fake/bin/claude"
        assert call.argv == ["/fake/bin/claude", "--resume", "--model", "x"]
        session_dir = session_dir_for(
            manager.switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )
        assert call.env["CLAUDE_CONFIG_DIR"] == str(session_dir)

    def test_fast_path_for_active_account(
        self, manager, capture_exec, monkeypatch, capsys
    ):
        monkeypatch.setattr(
            manager.switcher,
            "_get_current_account",
            lambda: (ACCOUNT_EMAIL, ORG_UUID),
        )
        with pytest.raises(_ExecCalled) as exc:
            manager.run("2", [])

        assert "CLAUDE_CONFIG_DIR" not in exc.value.env
        assert "already the active default login" in capsys.readouterr().out

    def test_preset_config_dir_disables_fast_path(
        self,
        manager,
        capture_exec,
        monkeypatch,
        auth_status_tracks_seed,
        refresh_rotates,
        capsys,
    ):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/somewhere/else")
        # Even a matching identity must NOT fast-path when the env var is set.
        monkeypatch.setattr(
            manager.switcher,
            "_get_current_account",
            lambda: (ACCOUNT_EMAIL, ORG_UUID),
        )
        with pytest.raises(_ExecCalled) as exc:
            manager.run("2", [])

        session_dir = session_dir_for(
            manager.switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )
        assert exc.value.env["CLAUDE_CONFIG_DIR"] == str(session_dir)
        assert "overriding it for this launch" in capsys.readouterr().out

    def test_auth_override_vars_scrubbed_from_session_env(
        self,
        manager,
        capture_exec,
        monkeypatch,
        auth_status_tracks_seed,
        refresh_rotates,
        capsys,
    ):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-key")
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok")
        monkeypatch.setenv("UNRELATED_VAR", "kept")
        with pytest.raises(_ExecCalled) as exc:
            manager.run("2", [])

        # Warned, and the overrides are scrubbed from the launched env —
        # `cswap run 2` means account 2, not whatever the API key resolves to.
        out = capsys.readouterr().out
        assert "Ignoring ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN" in out
        assert "ANTHROPIC_API_KEY" not in exc.value.env
        assert "ANTHROPIC_AUTH_TOKEN" not in exc.value.env
        assert exc.value.env["UNRELATED_VAR"] == "kept"

    def test_fast_path_keeps_env_untouched(
        self, manager, capture_exec, monkeypatch
    ):
        """Plain-claude fast path must NOT scrub: it's normal claude behavior."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-key")
        monkeypatch.setattr(
            manager.switcher,
            "_get_current_account",
            lambda: (ACCOUNT_EMAIL, ORG_UUID),
        )
        with pytest.raises(_ExecCalled) as exc:
            manager.run("2", [])

        assert exc.value.env["ANTHROPIC_API_KEY"] == "sk-ant-key"


class TestExec:
    """The _exec() terminal handoff dispatches per-platform (runs on any host)."""

    def test_posix_replaces_process_with_execvpe(self, manager, monkeypatch):
        def fake_execvpe(binary, argv, env):
            # os.execvpe never returns; raising models that (and lets _exec's
            # "unreachable" guard stay unhit, as it would be in real life).
            raise _ExecCalled(binary, argv, env)

        monkeypatch.setattr(session_mod.sys, "platform", "linux")
        monkeypatch.setattr(session_mod.os, "execvpe", fake_execvpe)
        with pytest.raises(_ExecCalled) as exc:
            manager._exec("/bin/claude", ["--resume"], {"A": "B"})
        assert (exc.value.binary, exc.value.argv, exc.value.env) == (
            "/bin/claude",
            ["/bin/claude", "--resume"],
            {"A": "B"},
        )

    def test_windows_runs_subprocess_and_mirrors_exit_code(self, manager, monkeypatch):
        seen = {}

        def fake_run(argv, env=None, **kwargs):
            seen["call"] = (argv, env)
            return SimpleNamespace(returncode=7)

        monkeypatch.setattr(session_mod.sys, "platform", "win32")
        monkeypatch.setattr(session_mod.subprocess, "run", fake_run)
        with pytest.raises(SystemExit) as exc:
            manager._exec("/bin/claude", ["--resume"], {"A": "B"})
        assert exc.value.code == 7
        assert seen["call"] == (["/bin/claude", "--resume"], {"A": "B"})


# ---------------------------------------------------------------------------
# switcher guards
# ---------------------------------------------------------------------------


class TestGuards:
    def test_remove_account_refused_while_live(self, seeded_switcher, monkeypatch):
        session_dir = session_dir_for(
            seeded_switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )
        make_live(session_dir)
        monkeypatch.setattr(
            "builtins.input", lambda *a: pytest.fail("prompt must not be reached")
        )
        with pytest.raises(SessionError, match="live session-mode"):
            seeded_switcher.remove_account(ACCOUNT_NUM)
        # Account untouched.
        assert seeded_switcher.read_account_credentials(ACCOUNT_NUM, ACCOUNT_EMAIL)

    def test_remove_account_cleans_session_profile(
        self, seeded_switcher, monkeypatch, block_real_keychain
    ):
        session_dir = session_dir_for(
            seeded_switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )
        session_dir.mkdir(parents=True)
        service = keychain_service_name(session_dir)
        account = session_mod._keychain_account_name()
        block_real_keychain.set_password(service, account, "creds")

        monkeypatch.setattr("builtins.input", lambda *a: "y")
        seeded_switcher.remove_account(ACCOUNT_NUM)

        assert not session_dir.exists()
        assert block_real_keychain.get_password(service, account) is None

    def test_remove_account_assume_yes_skips_prompt(self, seeded_switcher, monkeypatch):
        monkeypatch.setattr(
            "builtins.input", lambda *a: pytest.fail("prompt must not be reached")
        )
        seeded_switcher.remove_account(ACCOUNT_NUM, assume_yes=True)
        data = seeded_switcher._get_sequence_data()
        assert ACCOUNT_NUM not in data["accounts"]

    def test_delete_account_files_chokepoint_refuses_live(self, seeded_switcher):
        session_dir = session_dir_for(
            seeded_switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )
        make_live(session_dir)
        with pytest.raises(SessionError, match="live session-mode"):
            seeded_switcher._delete_account_files(ACCOUNT_NUM, ACCOUNT_EMAIL)

    def test_purge_refused_while_live(self, seeded_switcher, monkeypatch):
        session_dir = session_dir_for(
            seeded_switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )
        make_live(session_dir)
        monkeypatch.setattr(
            "builtins.input", lambda *a: pytest.fail("prompt must not be reached")
        )
        with pytest.raises(SessionError, match="Exit them first"):
            seeded_switcher.purge()
        assert seeded_switcher.backup_dir.exists()

    def test_purge_sweeps_session_keychain_entries(
        self, seeded_switcher, monkeypatch, block_real_keychain
    ):
        session_dir = session_dir_for(
            seeded_switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )
        session_dir.mkdir(parents=True)
        service = keychain_service_name(session_dir)
        account = session_mod._keychain_account_name()
        block_real_keychain.set_password(service, account, "creds")

        monkeypatch.setattr("builtins.input", lambda *a: "y")
        seeded_switcher.purge()

        assert block_real_keychain.get_password(service, account) is None
        assert not seeded_switcher.backup_dir.exists()

    def test_switch_warns_on_live_target_but_completes(
        self, seeded_switcher, monkeypatch, capsys
    ):
        session_dir = session_dir_for(
            seeded_switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )
        make_live(session_dir)
        # Direct-activation path (no live default identity) keeps this focused.
        monkeypatch.setattr(seeded_switcher, "_get_current_account", lambda: None)
        monkeypatch.setattr(seeded_switcher, "list_accounts", lambda **kw: None)

        seeded_switcher._perform_switch(ACCOUNT_NUM)

        out = capsys.readouterr().out
        assert "live session-mode" in out
        data = seeded_switcher._get_sequence_data()
        assert data["activeAccountNumber"] == int(ACCOUNT_NUM)

    def test_backup_credential_write_invalidates_stale_profile(
        self, seeded_switcher, block_real_keychain
    ):
        """Re-login + --add-account (or any backup cred write) must force the
        non-live session profile to re-bootstrap — otherwise the documented
        recovery path leaves `cswap run` on stale credentials that still pass
        the local reuse check."""
        session_dir = session_dir_for(
            seeded_switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )
        session_dir.mkdir(parents=True)
        (session_dir / ".credentials.json").write_text("stale")
        (session_dir / ".claude.json").write_text('{"projects": {}}')
        service = keychain_service_name(session_dir)
        account = session_mod._keychain_account_name()
        block_real_keychain.set_password(service, account, "stale")

        seeded_switcher._write_account_credentials(
            ACCOUNT_NUM, ACCOUNT_EMAIL, ROTATED_CREDS
        )

        assert not (session_dir / ".credentials.json").exists()
        assert (session_dir / ".claude.json").exists()  # history preserved
        assert block_real_keychain.get_password(service, account) is None

    def test_backup_credential_write_leaves_live_profile_alone_but_marks_stale(
        self, seeded_switcher
    ):
        session_dir = session_dir_for(
            seeded_switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )
        make_live(session_dir)
        (session_dir / ".credentials.json").write_text("live session creds")

        seeded_switcher._write_account_credentials(
            ACCOUNT_NUM, ACCOUNT_EMAIL, ROTATED_CREDS
        )

        # Live copy untouched, but flagged for re-bootstrap after exit.
        assert (session_dir / ".credentials.json").read_text() == "live session creds"
        assert (session_dir / session_mod.STALE_MARKER).exists()

    def test_list_skips_refresh_for_live_session_accounts(
        self, seeded_switcher, monkeypatch
    ):
        """cswap --list must not proactively refresh an account that is live in
        a session — rotating the backup copy's token could invalidate the
        session's copy."""
        session_dir = session_dir_for(
            seeded_switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )
        make_live(session_dir)
        seen: dict[str, bool] = {}

        def fake_fetch(num, email, creds, is_active=False, persist_credentials=None):
            seen[num] = is_active
            return oauth.UsageOutcome(None)

        monkeypatch.setattr(
            "claude_swap.oauth.try_fetch_usage_for_account", fake_fetch
        )
        seeded_switcher.list_accounts()

        assert seen[ACCOUNT_NUM] is True  # treated like active: no refresh
        assert seen.get("1") in (None, False)  # account 1 has no live session

    def test_invalidate_session_credentials_keeps_history(
        self, seeded_switcher, block_real_keychain
    ):
        session_dir = session_dir_for(
            seeded_switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )
        session_dir.mkdir(parents=True)
        (session_dir / ".credentials.json").write_text("old creds")
        (session_dir / ".claude.json").write_text('{"projects": {}}')
        service = keychain_service_name(session_dir)
        account = session_mod._keychain_account_name()
        block_real_keychain.set_password(service, account, "creds")

        seeded_switcher._invalidate_session_credentials(ACCOUNT_NUM, ACCOUNT_EMAIL)

        assert not (session_dir / ".credentials.json").exists()
        assert (session_dir / ".claude.json").exists()
        assert block_real_keychain.get_password(service, account) is None


# ---------------------------------------------------------------------------
# history sharing (--share-history)
# ---------------------------------------------------------------------------


@pytest.fixture
def history_setup(share_setup, temp_home: Path):
    """share_setup plus conversation history on both sides."""
    source, session_dir, mgr = share_setup
    (source / "projects").mkdir()
    (source / "projects" / "-home-user-app").mkdir()
    (source / "projects" / "-home-user-app" / "aaa.jsonl").write_text("main-a\n")
    (source / "history.jsonl").write_text('{"p": "main"}\n')
    return source, session_dir, mgr


@pytest.mark.skipif(sys.platform == "win32", reason="history sharing is POSIX-only")
class TestShareHistoryPosix:
    def test_not_shared_by_default(self, history_setup):
        source, session_dir, mgr = history_setup
        mgr._sync_sharing(session_dir, share=True)

        assert not (session_dir / "projects").exists()
        assert not (session_dir / "history.jsonl").exists()
        manifest = json.loads((session_dir / SHARE_MANIFEST).read_text())
        assert "projects" not in manifest["items"]

    def test_links_history_items(self, history_setup):
        source, session_dir, mgr = history_setup
        mgr._sync_sharing(session_dir, share=True, share_history=True)

        assert (session_dir / "projects").readlink() == source / "projects"
        assert (session_dir / "history.jsonl").readlink() == source / "history.jsonl"
        manifest = json.loads((session_dir / SHARE_MANIFEST).read_text())
        assert {"projects", "history.jsonl"} <= set(manifest["items"])

    def test_creates_missing_source(self, share_setup):
        source, session_dir, mgr = share_setup  # no history in ~/.claude yet
        mgr._sync_sharing(session_dir, share=True, share_history=True)

        assert (source / "projects").is_dir()
        assert (source / "history.jsonl").is_file()
        assert (session_dir / "projects").readlink() == source / "projects"

    def test_merges_existing_profile_history(self, history_setup):
        source, session_dir, mgr = history_setup
        proj = session_dir / "projects" / "-home-user-app"
        proj.mkdir(parents=True)
        (proj / "bbb.jsonl").write_text("profile-b\n")
        (session_dir / "projects" / "-home-user-other").mkdir()
        (session_dir / "projects" / "-home-user-other" / "ccc.jsonl").write_text(
            "profile-c\n"
        )
        (session_dir / "history.jsonl").write_text(
            '{"p": "main"}\n{"p": "profile"}\n'
        )

        mgr._sync_sharing(session_dir, share=True, share_history=True)

        # Profile history landed in ~/.claude, alongside what was there.
        merged = source / "projects"
        assert (merged / "-home-user-app" / "aaa.jsonl").read_text() == "main-a\n"
        assert (merged / "-home-user-app" / "bbb.jsonl").read_text() == "profile-b\n"
        assert (merged / "-home-user-other" / "ccc.jsonl").read_text() == "profile-c\n"
        # Prompt history merged without duplicating shared lines.
        assert source / "history.jsonl" == (session_dir / "history.jsonl").readlink()
        lines = (source / "history.jsonl").read_text().splitlines()
        assert lines.count('{"p": "main"}') == 1
        assert '{"p": "profile"}' in lines
        # And the profile now links to the shared copy.
        assert (session_dir / "projects").readlink() == merged

    def test_merge_collision_keeps_target(self, history_setup):
        source, session_dir, mgr = history_setup
        proj = session_dir / "projects" / "-home-user-app"
        proj.mkdir(parents=True)
        (proj / "aaa.jsonl").write_text("profile-duplicate\n")

        mgr._sync_sharing(session_dir, share=True, share_history=True)

        assert (
            source / "projects" / "-home-user-app" / "aaa.jsonl"
        ).read_text() == "main-a\n"
        assert (session_dir / "projects").is_symlink()

    def test_merge_deferred_while_profile_live(self, history_setup, monkeypatch):
        source, session_dir, mgr = history_setup
        (session_dir / "projects").mkdir()
        (session_dir / "projects" / "x.jsonl").write_text("live\n")
        monkeypatch.setattr(
            session_mod, "live_sessions_for", lambda _dir: [object()]
        )

        mgr._sync_sharing(session_dir, share=True, share_history=True)

        # Untouched: no merge, no link, not claimed in the manifest.
        assert not (session_dir / "projects").is_symlink()
        assert (session_dir / "projects" / "x.jsonl").read_text() == "live\n"
        manifest = json.loads((session_dir / SHARE_MANIFEST).read_text())
        assert "projects" not in manifest["items"]

    def test_toggle_off_removes_links_keeps_data(self, history_setup):
        source, session_dir, mgr = history_setup
        mgr._sync_sharing(session_dir, share=True, share_history=True)
        mgr._sync_sharing(session_dir, share=True, share_history=False)

        assert not (session_dir / "projects").exists()
        assert not (session_dir / "history.jsonl").exists()
        # Shared source data is never touched; customizations stay linked.
        assert (source / "projects" / "-home-user-app" / "aaa.jsonl").exists()
        assert (session_dir / "settings.json").is_symlink()

    def test_share_history_independent_of_no_share(self, history_setup):
        source, session_dir, mgr = history_setup
        mgr._sync_sharing(session_dir, share=False, share_history=True)

        assert (session_dir / "projects").is_symlink()
        assert not (session_dir / "settings.json").exists()

    def test_seeded_source_has_claude_code_modes(self, share_setup):
        source, session_dir, mgr = share_setup  # no history in ~/.claude yet
        mgr._sync_sharing(session_dir, share=True, share_history=True)

        assert (source / "projects").stat().st_mode & 0o777 == 0o700
        assert (source / "history.jsonl").stat().st_mode & 0o777 == 0o600

    def test_merge_creates_dirs_and_files_with_claude_code_modes(self, share_setup):
        source, session_dir, mgr = share_setup  # no history in ~/.claude yet
        deep = session_dir / "projects" / "-home-user-app" / "sess1"
        deep.mkdir(parents=True)
        (deep / "agent.jsonl").write_text("profile\n")
        (session_dir / "history.jsonl").write_text('{"p": "profile"}\n')

        mgr._sync_sharing(session_dir, share=True, share_history=True)

        for created in (
            source / "projects",
            source / "projects" / "-home-user-app",
            source / "projects" / "-home-user-app" / "sess1",
        ):
            assert created.stat().st_mode & 0o777 == 0o700
        assert (source / "history.jsonl").stat().st_mode & 0o777 == 0o600

    def test_stale_manifest_never_deletes_real_history(self, history_setup):
        # Lock-free launches can race: the manifest claims history items are
        # managed while the profile holds a real dir. Must merge, not rmtree.
        source, session_dir, mgr = history_setup
        proj = session_dir / "projects" / "-home-user-app"
        proj.mkdir(parents=True)
        (proj / "bbb.jsonl").write_text("profile-b\n")
        (session_dir / "history.jsonl").write_text('{"p": "profile"}\n')
        (session_dir / SHARE_MANIFEST).write_text(
            json.dumps({"items": ["projects", "history.jsonl"], "mode": "symlink"})
        )

        mgr._sync_sharing(session_dir, share=True, share_history=True)

        assert (
            source / "projects" / "-home-user-app" / "bbb.jsonl"
        ).read_text() == "profile-b\n"
        assert '{"p": "profile"}' in (source / "history.jsonl").read_text()
        assert (session_dir / "projects").readlink() == source / "projects"

    def test_toggle_off_with_stale_manifest_keeps_real_history(self, history_setup):
        source, session_dir, mgr = history_setup
        proj = session_dir / "projects" / "-home-user-app"
        proj.mkdir(parents=True)
        (proj / "bbb.jsonl").write_text("profile-b\n")
        (session_dir / SHARE_MANIFEST).write_text(
            json.dumps({"items": ["projects"], "mode": "symlink"})
        )

        mgr._sync_sharing(session_dir, share=True, share_history=False)

        # Real history is user data even when the manifest claims it.
        assert (proj / "bbb.jsonl").read_text() == "profile-b\n"


class TestShareHistoryWindows:
    def test_sync_never_links_history_in_copy_mode(self, history_setup):
        source, session_dir, mgr = history_setup
        mgr.switcher.platform = Platform.WINDOWS
        mgr._sync_sharing(session_dir, share=True, share_history=True)

        assert not (session_dir / "projects").exists()
        manifest = json.loads((session_dir / SHARE_MANIFEST).read_text())
        assert "projects" not in manifest["items"]

    def test_run_rejects_flag(self, history_setup, monkeypatch):
        source, session_dir, mgr = history_setup
        mgr.switcher.platform = Platform.WINDOWS
        monkeypatch.setattr(
            session_mod.shutil, "which", lambda _name: "/usr/bin/claude"
        )

        with pytest.raises(SessionError, match="Windows"):
            mgr.run(ACCOUNT_NUM, [], share=True, share_history=True)
