"""Tests for session mode (claude_swap.session + the switcher guards)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import unicodedata
from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_swap import macos_keychain
from claude_swap import oauth
from claude_swap import session as session_mod
from claude_swap.credentials import CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE
from claude_swap.exceptions import (
    AccountNotFoundError,
    CredentialReadError,
    SessionError,
    ValidationError,
)
from claude_swap.models import Platform
from claude_swap.paths import get_global_config_path
from claude_swap.session import (
    MCP_DISPLACED_STASH,
    MCP_MIRROR_MARKER,
    SHARE_MANIFEST,
    SessionManager,
    _probe_env,
    keychain_service_name,
    profile_is_quiescent,
    read_session_identity,
    scan_live_sessions,
    session_dir_for,
    session_identity_drifted,
    slugify_email,
    stale_marker_for,
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
    """Track consume-gate calls; the gate persists ROTATED_CREDS like the
    real one does (bootstrap re-reads the backup afterwards)."""
    calls: list[str] = []

    def fake_gate(self, account_num: str, email: str, snapshot: str):
        from claude_swap import oauth as oauth_mod
        calls.append(snapshot)
        self._write_account_credentials(account_num, email, ROTATED_CREDS)
        return oauth_mod.RefreshOutcome(ROTATED_CREDS, None)

    from claude_swap.switcher import ClaudeAccountSwitcher
    monkeypatch.setattr(
        ClaudeAccountSwitcher, "consume_backup_grant", fake_gate
    )
    return calls


def make_live(session_dir: Path, pid: int | None = None) -> None:
    """Simulate a live claude instance in a profile (own PID is always alive)."""
    pid = pid or os.getpid()
    pid_dir = session_dir / "sessions"
    pid_dir.mkdir(parents=True, exist_ok=True)
    (pid_dir / f"{pid}.json").write_text(json.dumps({"pid": pid}))


def _mark_stale(session_dir: Path, legacy_location: bool = False) -> None:
    """Plant a stale marker, in the current (sibling) or pre-move (child) spot."""
    if legacy_location:
        (session_dir / session_mod.STALE_MARKER).touch()
    else:
        session_mod.mark_session_stale(session_dir)


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

    def test_scan_live_sessions_missing_dir(self, tmp_path):
        assert scan_live_sessions(tmp_path / "nope") == ([], 0)

    def test_scan_live_sessions_dead_pid_ignored(self, tmp_path):
        make_live(tmp_path, pid=2**22 + 12345)  # vanishingly unlikely to exist
        assert scan_live_sessions(tmp_path) == ([], 0)

    def test_scan_live_sessions_own_pid(self, tmp_path):
        make_live(tmp_path)
        sessions, unreadable = scan_live_sessions(tmp_path)
        assert [s.pid for s in sessions] == [os.getpid()]
        assert unreadable == 0

    def test_unreadable_record_is_not_quiescent(self, tmp_path):
        """A dead PID and an unreadable record both yield zero live sessions.
        Only the first is evidence that nothing is running."""
        (tmp_path / "sessions").mkdir(parents=True, exist_ok=True)
        (tmp_path / "sessions" / "9999.json").write_text(
            "not json{{{", encoding="utf-8"
        )

        assert scan_live_sessions(tmp_path) == ([], 1)
        assert not profile_is_quiescent(tmp_path)

    def test_dead_pid_is_quiescent(self, tmp_path):
        """The control: zero live from a READABLE record IS safe to act on,
        so the predicate is not just refusing everything."""
        make_live(tmp_path, pid=2**22 + 12345)
        assert profile_is_quiescent(tmp_path)


class TestSessionIdentity:
    """read_session_identity / session_identity_drifted: an in-session /login
    can re-point a profile at a different account than its slot."""

    def _write_identity(self, session_dir, email, org_uuid=None):
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": email, "organizationUuid": org_uuid}
        }))

    def test_reads_email_and_org(self, tmp_path):
        self._write_identity(tmp_path, "a@x.com", "org-A")
        assert read_session_identity(tmp_path) == ("a@x.com", "org-A")

    def test_missing_org_reads_as_empty(self, tmp_path):
        self._write_identity(tmp_path, "a@x.com", None)
        assert read_session_identity(tmp_path) == ("a@x.com", "")

    def test_unreadable_variants_return_none(self, tmp_path):
        assert read_session_identity(tmp_path / "nope") is None  # no dir
        (tmp_path / ".claude.json").write_text("{not json")
        assert read_session_identity(tmp_path) is None  # invalid json
        (tmp_path / ".claude.json").write_bytes(b"\xff\xfe{}")
        assert read_session_identity(tmp_path) is None  # undecodable bytes
        (tmp_path / ".claude.json").write_text(json.dumps({"oauthAccount": {}}))
        assert read_session_identity(tmp_path) is None  # no email

    def test_different_email_is_drift(self, tmp_path):
        self._write_identity(tmp_path, "other@x.com", "org-A")
        assert session_identity_drifted(tmp_path, "a@x.com", "org-A")

    def test_same_email_different_org_is_drift(self, tmp_path):
        # The j@ck.gg case: one email, two orgs — two distinct subscriptions.
        self._write_identity(tmp_path, "a@x.com", "org-B")
        assert session_identity_drifted(tmp_path, "a@x.com", "org-A")

    def test_matching_identity_is_not_drift(self, tmp_path):
        self._write_identity(tmp_path, "a@x.com", "org-A")
        assert not session_identity_drifted(tmp_path, "a@x.com", "org-A")

    def test_org_check_is_lenient_when_either_side_empty(self, tmp_path):
        self._write_identity(tmp_path, "a@x.com", None)
        assert not session_identity_drifted(tmp_path, "a@x.com", "org-A")
        self._write_identity(tmp_path, "a@x.com", "org-B")
        assert not session_identity_drifted(tmp_path, "a@x.com", "")

    def test_unreadable_identity_is_not_drift(self, tmp_path):
        assert not session_identity_drifted(tmp_path / "nope", "a@x.com", "org-A")
        (tmp_path / ".claude.json").write_bytes(b"\xff\xfe{}")
        assert not session_identity_drifted(tmp_path, "a@x.com", "org-A")


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
        monkeypatch.setattr(
            ClaudeAccountSwitcher, "consume_backup_grant",
            lambda self, num, email, snap: oauth.RefreshOutcome(
                None, "transient"
            ),
        )
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
            ClaudeAccountSwitcher, "consume_backup_grant",
            lambda self, num, email, snap: refresh_calls.append(snap)
            or oauth.RefreshOutcome(None, "transient"),
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

    @pytest.mark.parametrize("legacy_location", [False, True])
    def test_stale_marker_forces_rebootstrap_after_session_exits(
        self, manager, seeded_switcher, auth_status_tracks_seed, refresh_rotates,
        legacy_location: bool,
    ):
        """Backup creds updated while the session was live → after it exits,
        the next run must re-bootstrap from the fresh backup even though the
        stale profile would still pass the local reuse check.

        `legacy_location=True` is the upgrade path: the marker moved to a
        SIBLING of the profile dir (a child could not be written by the fault
        that motivates it), and a profile marked by an older cswap on this
        machine has a pending re-bootstrap that the move must not drop.
        """
        session_dir, _, _ = manager.setup_session("2", share=False)
        (session_dir / ".credentials.json").write_text("stale lineage")
        _mark_stale(session_dir, legacy_location)
        # No live PID files → the session has exited.

        manager.setup_session("2", share=False)

        # Re-bootstrapped: fresh (refreshed) creds, marker cleared.
        assert (session_dir / ".credentials.json").read_text() == ROTATED_CREDS
        assert not session_mod.is_session_stale(session_dir)

    def test_stale_marker_plus_probe_timeout_still_rebootstraps(
        self, manager, seeded_switcher, auth_status_tracks_seed, refresh_rotates,
        monkeypatch,
    ):
        """A probe timeout on the stale path must not skip the re-seed.

        The stale path deletes .credentials.json before re-validating; a
        timeout there leaning valid would launch a cred-less profile. The
        local-artifact fallback reports invalid instead, bootstrap re-seeds,
        and the post-bootstrap timeout leans valid without deleting the
        profile (#224 follow-up).
        """
        session_dir, _, _ = manager.setup_session("2", share=False)
        (session_dir / session_mod.STALE_MARKER).touch()

        def raise_timeout(*a, **k):
            raise session_mod.subprocess.TimeoutExpired(cmd="claude", timeout=10)

        monkeypatch.setattr(session_mod.subprocess, "run", raise_timeout)

        manager.setup_session("2", share=False)

        assert (session_dir / ".credentials.json").read_text() == ROTATED_CREDS
        assert not (session_dir / session_mod.STALE_MARKER).exists()

    @pytest.mark.parametrize("legacy_location", [False, True])
    def test_stale_marker_preserved_while_session_still_live(
        self, manager, seeded_switcher, auth_status_tracks_seed, refresh_rotates,
        legacy_location: bool,
    ):
        """A second `cswap run` joining a live session must not invalidate
        under the running claude; the marker survives for later."""
        session_dir, _, _ = manager.setup_session("2", share=False)
        (session_dir / ".credentials.json").write_text("live lineage")
        _mark_stale(session_dir, legacy_location)
        make_live(session_dir)

        manager.setup_session("2", share=False)

        assert (session_dir / ".credentials.json").read_text() == "live lineage"
        assert session_mod.is_session_stale(session_dir)

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

    def _seed_profile(self, session_dir, email=ACCOUNT_EMAIL, org=ORG_UUID):
        """Local artifacts of a bootstrapped profile: creds + identity."""
        (session_dir / ".credentials.json").write_text("{}")
        (session_dir / ".claude.json").write_text(
            json.dumps(
                {"oauthAccount": {"emailAddress": email, "organizationUuid": org}}
            )
        )

    def _probe_times_out(self, monkeypatch):
        def raise_timeout(*a, **k):
            raise session_mod.subprocess.TimeoutExpired(cmd="claude", timeout=10)

        monkeypatch.setattr(session_mod.subprocess, "run", raise_timeout)

    def test_probe_timeout_leans_valid(self, manager, tmp_path, monkeypatch):
        """A probe timeout is a busy machine, not a bad login.

        setup_session escalates a False from here all the way to
        _cleanup_failed_session deleting the profile, so an indeterminate
        probe must not report invalid (#224).
        """
        tmp_path.mkdir(exist_ok=True)
        self._seed_profile(tmp_path)
        self._probe_times_out(monkeypatch)
        assert manager._is_session_valid(tmp_path, ACCOUNT_EMAIL, ORG_UUID)

    def test_probe_timeout_needs_credential_material(
        self, manager, tmp_path, monkeypatch
    ):
        """Timeout must not vouch for a profile with no credentials.

        The stale-marker path deletes .credentials.json right before
        re-validating; leaning valid there would skip bootstrap and launch
        claude logged out (#224 follow-up).
        """
        tmp_path.mkdir(exist_ok=True)
        self._seed_profile(tmp_path)
        (tmp_path / ".credentials.json").unlink()
        self._probe_times_out(monkeypatch)
        assert not manager._is_session_valid(tmp_path, ACCOUNT_EMAIL, ORG_UUID)

    def test_probe_timeout_accepts_keychain_only_credentials(
        self, manager, tmp_path, monkeypatch, block_real_keychain
    ):
        """A keychain-migrated macOS profile is not cred-less.

        Claude's first credential write moves the material into the hashed
        keychain entry and deletes the plaintext seed — the steady state for
        any used macOS profile. Only stale invalidation removes both, so
        the timeout fallback must consult the keychain before declaring the
        profile cred-less and forcing a re-bootstrap that would discard the
        profile's freshest token family (#224 follow-up).
        """
        tmp_path.mkdir(exist_ok=True)
        self._seed_profile(tmp_path)
        (tmp_path / ".credentials.json").unlink()
        block_real_keychain.set_password(
            session_mod.keychain_service_name(tmp_path),
            session_mod._keychain_account_name(),
            "migrated material",
        )
        self._probe_times_out(monkeypatch)
        assert manager._is_session_valid(tmp_path, ACCOUNT_EMAIL, ORG_UUID)

    def test_probe_timeout_with_unreadable_keychain_leans_valid(
        self, manager, tmp_path, monkeypatch
    ):
        """A locked/busy keychain is indeterminate, not a credential miss.

        Only rc 44 means "definitely absent"; under the same load that times
        out the probe, `security` can time out too, and treating that as
        cred-less would re-bootstrap over the keychain entry holding the
        profile's freshest token family (#224 follow-up).
        """
        tmp_path.mkdir(exist_ok=True)
        self._seed_profile(tmp_path)
        (tmp_path / ".credentials.json").unlink()

        def raise_keychain_error(*a, **k):
            raise session_mod.macos_keychain.KeychainError("keychain locked")

        monkeypatch.setattr(
            session_mod.macos_keychain, "get_password", raise_keychain_error
        )
        self._probe_times_out(monkeypatch)
        assert manager._is_session_valid(tmp_path, ACCOUNT_EMAIL, ORG_UUID)

    def test_probe_timeout_rejects_empty_credential_file(
        self, manager, tmp_path, monkeypatch
    ):
        tmp_path.mkdir(exist_ok=True)
        self._seed_profile(tmp_path)
        (tmp_path / ".credentials.json").write_text("")
        self._probe_times_out(monkeypatch)
        assert not manager._is_session_valid(tmp_path, ACCOUNT_EMAIL, ORG_UUID)

    def test_probe_timeout_still_rejects_drifted_identity(
        self, manager, tmp_path, monkeypatch
    ):
        """Timeout must not vouch for a profile re-pointed by /login.

        The profile's own .claude.json records the account it is logged in
        as; on timeout that local record still gates validity (#224
        follow-up).
        """
        tmp_path.mkdir(exist_ok=True)
        self._seed_profile(tmp_path, email="other@example.com")
        self._probe_times_out(monkeypatch)
        assert not manager._is_session_valid(tmp_path, ACCOUNT_EMAIL, ORG_UUID)

    def test_probe_timeout_lenient_on_unreadable_identity(
        self, manager, tmp_path, monkeypatch
    ):
        """A broken .claude.json degrades to trusting the profile.

        Mirrors session_identity_drifted's stance: unreadable metadata is
        not drift. Failing closed here would re-open the destructive
        cleanup path #224 removed: a backup whose oauthAccount lacks an
        emailAddress keeps the identity unreadable even after re-bootstrap,
        so the post-bootstrap check would delete the profile on every
        loaded launch.
        """
        tmp_path.mkdir(exist_ok=True)
        self._seed_profile(tmp_path)
        (tmp_path / ".claude.json").write_text("not json")
        self._probe_times_out(monkeypatch)
        assert manager._is_session_valid(tmp_path, ACCOUNT_EMAIL, ORG_UUID)

    def test_probe_oserror_stays_invalid(self, manager, tmp_path, monkeypatch):
        tmp_path.mkdir(exist_ok=True)

        def raise_oserror(*a, **k):
            raise OSError("spawn failed")

        monkeypatch.setattr(session_mod.subprocess, "run", raise_oserror)
        assert not manager._is_session_valid(tmp_path, ACCOUNT_EMAIL, ORG_UUID)

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

    def test_links_to_resolved_target_when_source_is_symlink(
        self, share_setup, temp_home
    ):
        """A dotfiles-managed ~/.claude item is itself a symlink: link straight to
        its final target so the chain is only ever one hop deep. Claude Code's
        atomic settings write resolves one hop only, so a link-to-a-link gets its
        intermediate link replaced by a regular file, silently detaching the user's
        real source of truth (anthropics/claude-code#78162).
        """
        source, session_dir, mgr = share_setup
        dotfiles = temp_home / "dotfiles"
        dotfiles.mkdir()
        real = dotfiles / "settings.json"
        real.write_text('{"real": true}')
        link = source / "settings.json"
        link.unlink()
        link.symlink_to(real)

        mgr._sync_sharing(session_dir, share=True)

        assert (session_dir / "settings.json").readlink() == real.resolve()

    def test_repoints_existing_link_to_resolved_target(self, share_setup, temp_home):
        """An already-adopted link pointing at the intermediate symlink is repointed
        at the final target, not left one hop short."""
        source, session_dir, mgr = share_setup
        dotfiles = temp_home / "dotfiles"
        dotfiles.mkdir()
        real = dotfiles / "settings.json"
        real.write_text("{}")
        link = source / "settings.json"
        link.unlink()
        link.symlink_to(real)
        (session_dir / "settings.json").symlink_to(link)

        mgr._sync_sharing(session_dir, share=True)

        assert (session_dir / "settings.json").readlink() == real.resolve()


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
# mcpServers mirror (issue #139)
# ---------------------------------------------------------------------------

GITHUB_MCP = {"type": "stdio", "command": "gh-mcp", "env": {"TOKEN": "abc"}}
LOCAL_MCP = {"type": "stdio", "command": "mine"}


@pytest.fixture
def mcp_setup(temp_home: Path, seeded_switcher):
    """A fake live default config and a session profile with its own config."""
    default_config = temp_home / ".claude.json"
    default_config.write_text(
        json.dumps(
            {
                "oauthAccount": {"emailAddress": "default@example.com"},
                "mcpServers": {"github": GITHUB_MCP},
                "projects": {"/repo": {"mcpServers": {"proj-local": {}}}},
            }
        )
    )
    session_dir = session_dir_for(
        seeded_switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
    )
    session_dir.mkdir(parents=True)
    (session_dir / ".claude.json").write_text(
        json.dumps(
            {
                "oauthAccount": {"emailAddress": ACCOUNT_EMAIL},
                "theme": "light",
                "projects": {"/w": {"allowedTools": []}},
            }
        )
    )
    return default_config, session_dir, SessionManager(seeded_switcher)


def _session_config(session_dir: Path) -> dict:
    return json.loads((session_dir / ".claude.json").read_text())


def _set_default_mcp(default_config: Path, servers: dict | None) -> None:
    data = json.loads(default_config.read_text())
    if servers is None:
        data.pop("mcpServers", None)
    else:
        data["mcpServers"] = servers
    default_config.write_text(json.dumps(data))


class TestMcpMirror:
    def test_bootstrap_launch_mirrors(
        self, temp_home, manager, auth_status_tracks_seed, refresh_rotates
    ):
        (temp_home / ".claude.json").write_text(
            json.dumps({"mcpServers": {"github": GITHUB_MCP}})
        )
        session_dir, _, _ = manager.setup_session("2", share=True)

        config = _session_config(session_dir)
        assert config["mcpServers"] == {"github": GITHUB_MCP}
        assert config["oauthAccount"]["emailAddress"] == ACCOUNT_EMAIL
        assert (session_dir / MCP_MIRROR_MARKER).exists()
        assert not (session_dir / MCP_DISPLACED_STASH).exists()  # nothing displaced

    def test_mirror_preserves_other_keys(self, mcp_setup):
        default_config, session_dir, mgr = mcp_setup
        mgr._sync_sharing(session_dir, share=True)

        config = _session_config(session_dir)
        assert config["mcpServers"] == {"github": GITHUB_MCP}
        assert config["oauthAccount"]["emailAddress"] == ACCOUNT_EMAIL
        assert config["theme"] == "light"
        assert config["projects"] == {"/w": {"allowedTools": []}}

    def test_edit_and_delete_propagate(self, mcp_setup):
        default_config, session_dir, mgr = mcp_setup
        mgr._sync_sharing(session_dir, share=True)

        edited = {"github": {**GITHUB_MCP, "env": {"TOKEN": "rotated"}}, "new": {}}
        _set_default_mcp(default_config, edited)
        mgr._sync_sharing(session_dir, share=True)
        assert _session_config(session_dir)["mcpServers"] == edited

        _set_default_mcp(default_config, {"new": {}})
        mgr._sync_sharing(session_dir, share=True)
        assert _session_config(session_dir)["mcpServers"] == {"new": {}}

    def test_default_without_key_removes_key(self, mcp_setup):
        default_config, session_dir, mgr = mcp_setup
        mgr._sync_sharing(session_dir, share=True)
        _set_default_mcp(default_config, None)

        mgr._sync_sharing(session_dir, share=True)

        assert "mcpServers" not in _session_config(session_dir)

    def test_legacy_config_json_source(self, mcp_setup, temp_home):
        default_config, session_dir, mgr = mcp_setup
        legacy = temp_home / ".claude" / ".config.json"
        legacy.write_text(json.dumps({"mcpServers": {"legacy-src": {}}}))

        mgr._sync_sharing(session_dir, share=True)

        assert _session_config(session_dir)["mcpServers"] == {"legacy-src": {}}

    def test_session_local_change_reset_without_stash(self, mcp_setup):
        default_config, session_dir, mgr = mcp_setup
        mgr._sync_sharing(session_dir, share=True)  # adopt

        config = _session_config(session_dir)
        config["mcpServers"]["mine"] = LOCAL_MCP
        (session_dir / ".claude.json").write_text(json.dumps(config))
        mgr._sync_sharing(session_dir, share=True)

        assert _session_config(session_dir)["mcpServers"] == {"github": GITHUB_MCP}
        # Post-adoption resets are documented behavior, never stashed.
        assert not (session_dir / MCP_DISPLACED_STASH).exists()

    def test_migration_stashes_displaced_only(self, mcp_setup, capsys):
        default_config, session_dir, mgr = mcp_setup
        config = _session_config(session_dir)
        config["mcpServers"] = {"pre-feature": LOCAL_MCP, "github": GITHUB_MCP}
        (session_dir / ".claude.json").write_text(json.dumps(config))

        mgr._sync_sharing(session_dir, share=True)

        assert _session_config(session_dir)["mcpServers"] == {"github": GITHUB_MCP}
        stash = json.loads((session_dir / MCP_DISPLACED_STASH).read_text())
        # Only the displaced entry — github matched the default and is not
        # duplicated into the stash.
        assert stash == {"schemaVersion": 1, "mcpServers": {"pre-feature": LOCAL_MCP}}
        assert "saved to" in capsys.readouterr().out
        assert (session_dir / MCP_MIRROR_MARKER).exists()

    def test_stash_is_write_once(self, mcp_setup):
        """A stash from an interrupted adoption is never overwritten."""
        default_config, session_dir, mgr = mcp_setup
        stash_path = session_dir / MCP_DISPLACED_STASH
        original = {"schemaVersion": 1, "mcpServers": {"real-pre-feature": {}}}
        stash_path.write_text(json.dumps(original))
        config = _session_config(session_dir)
        config["mcpServers"] = {"drift": {}}  # would look displaced
        (session_dir / ".claude.json").write_text(json.dumps(config))

        mgr._sync_sharing(session_dir, share=True)

        assert _session_config(session_dir)["mcpServers"] == {"github": GITHUB_MCP}
        assert json.loads(stash_path.read_text()) == original

    def test_invalid_stash_blocks_reset(self, mcp_setup):
        """A squatter on the stash name must not count as a saved copy."""
        default_config, session_dir, mgr = mcp_setup
        (session_dir / MCP_DISPLACED_STASH).mkdir()  # directory, not a stash
        config = _session_config(session_dir)
        config["mcpServers"] = {"pre-feature": LOCAL_MCP}
        (session_dir / ".claude.json").write_text(json.dumps(config))

        mgr._sync_sharing(session_dir, share=True)

        assert _session_config(session_dir)["mcpServers"] == {
            "pre-feature": LOCAL_MCP
        }
        assert not (session_dir / MCP_MIRROR_MARKER).exists()

    def test_null_valued_entry_is_stashed(self, mcp_setup):
        """Membership check: a JSON-null entry absent upstream still stashes."""
        default_config, session_dir, mgr = mcp_setup
        config = _session_config(session_dir)
        config["mcpServers"] = {"weird": None, "github": GITHUB_MCP}
        (session_dir / ".claude.json").write_text(json.dumps(config))

        mgr._sync_sharing(session_dir, share=True)

        stash = json.loads((session_dir / MCP_DISPLACED_STASH).read_text())
        assert stash["mcpServers"] == {"weird": None}
        assert _session_config(session_dir)["mcpServers"] == {"github": GITHUB_MCP}

    def test_stash_failure_aborts_reset(self, mcp_setup, monkeypatch):
        default_config, session_dir, mgr = mcp_setup
        config = _session_config(session_dir)
        config["mcpServers"] = {"pre-feature": LOCAL_MCP}
        (session_dir / ".claude.json").write_text(json.dumps(config))
        real_write = session_mod.atomic_write_json

        def flaky(path, data):
            if path.name == MCP_DISPLACED_STASH:
                raise OSError("disk full")
            real_write(path, data)

        monkeypatch.setattr(session_mod, "atomic_write_json", flaky)
        mgr._sync_sharing(session_dir, share=True)

        assert _session_config(session_dir)["mcpServers"] == {
            "pre-feature": LOCAL_MCP
        }
        assert not (session_dir / MCP_MIRROR_MARKER).exists()

    def test_in_sync_first_run_adopts_without_write(self, mcp_setup):
        default_config, session_dir, mgr = mcp_setup
        config_path = session_dir / ".claude.json"
        config = _session_config(session_dir)
        config["mcpServers"] = {"github": GITHUB_MCP}
        config_path.write_text(json.dumps(config))
        before = config_path.read_bytes()

        mgr._sync_sharing(session_dir, share=True)

        assert config_path.read_bytes() == before  # no rewrite
        assert not (config_path.parent / ".claude.json.lock").exists()  # released
        assert (session_dir / MCP_MIRROR_MARKER).exists()

    def test_adopted_in_sync_run_takes_no_lock(self, mcp_setup, monkeypatch):
        """The steady state must stay lock-free (only first adoption locks)."""
        default_config, session_dir, mgr = mcp_setup
        mgr._sync_sharing(session_dir, share=True)  # adopt + mirror

        def boom(*args, **kwargs):
            raise AssertionError("lock taken on the adopted in-sync path")

        monkeypatch.setattr(session_mod, "proper_lockfile", boom)
        mgr._sync_sharing(session_dir, share=True)  # must not raise

    @pytest.mark.parametrize(
        "source_state",
        ["missing", "corrupt", "non_dict_root", "non_dict_key", "binary"],
    )
    def test_fail_open_on_bad_source(self, mcp_setup, source_state):
        default_config, session_dir, mgr = mcp_setup
        if source_state == "missing":
            default_config.unlink()
        elif source_state == "corrupt":
            default_config.write_text("{not json")
        elif source_state == "non_dict_root":
            default_config.write_text("[]")
        elif source_state == "non_dict_key":
            default_config.write_text(json.dumps({"mcpServers": ["bad"]}))
        else:
            default_config.write_bytes(b"\xff\xfe not utf-8 \x00")
        before = (session_dir / ".claude.json").read_bytes()

        mgr._sync_sharing(session_dir, share=True)

        assert (session_dir / ".claude.json").read_bytes() == before
        assert not (session_dir / MCP_MIRROR_MARKER).exists()

    @pytest.mark.parametrize("bad_value", ["null", "[]", '"a-string"'])
    def test_fail_open_on_bad_target_mcp(self, mcp_setup, bad_value):
        """A malformed profile mcpServers must skip, never crash the launch."""
        default_config, session_dir, mgr = mcp_setup
        config = _session_config(session_dir)
        config["mcpServers"] = json.loads(bad_value)
        (session_dir / ".claude.json").write_text(json.dumps(config))
        before = (session_dir / ".claude.json").read_bytes()

        mgr._sync_sharing(session_dir, share=True)

        assert (session_dir / ".claude.json").read_bytes() == before
        assert not (session_dir / MCP_MIRROR_MARKER).exists()

    def test_corrupt_session_config_skipped(self, mcp_setup):
        default_config, session_dir, mgr = mcp_setup
        (session_dir / ".claude.json").write_text("{broken")

        mgr._sync_sharing(session_dir, share=True)

        assert (session_dir / ".claude.json").read_text() == "{broken"
        assert not (session_dir / MCP_MIRROR_MARKER).exists()

    @pytest.mark.skipif(sys.platform == "win32", reason="symlink target check")
    def test_symlinked_session_config_skipped(self, mcp_setup, temp_home):
        default_config, session_dir, mgr = mcp_setup
        elsewhere = temp_home / "elsewhere.json"
        (session_dir / ".claude.json").rename(elsewhere)
        (session_dir / ".claude.json").symlink_to(elsewhere)
        before = elsewhere.read_bytes()

        mgr._sync_sharing(session_dir, share=True)

        assert (session_dir / ".claude.json").is_symlink()
        assert elsewhere.read_bytes() == before

    def test_held_lock_fails_open(self, mcp_setup, monkeypatch):
        from claude_swap import claude_locks

        monkeypatch.setattr(claude_locks, "DEFAULT_TIMEOUT_S", 0.3)
        default_config, session_dir, mgr = mcp_setup
        (session_dir / ".claude.json.lock").mkdir()  # fresh mtime: live holder
        before = (session_dir / ".claude.json").read_bytes()

        mgr._sync_sharing(session_dir, share=True)

        assert (session_dir / ".claude.json").read_bytes() == before

    def test_no_share_before_adoption_untouched(self, mcp_setup):
        default_config, session_dir, mgr = mcp_setup
        config = _session_config(session_dir)
        config["mcpServers"] = {"pre-feature": LOCAL_MCP}
        (session_dir / ".claude.json").write_text(json.dumps(config))

        mgr._sync_sharing(session_dir, share=False)

        assert _session_config(session_dir)["mcpServers"] == {
            "pre-feature": LOCAL_MCP
        }

    def test_no_share_after_adoption_removes_then_restores(self, mcp_setup):
        default_config, session_dir, mgr = mcp_setup
        mgr._sync_sharing(session_dir, share=True)  # adopt

        mgr._sync_sharing(session_dir, share=False)
        config = _session_config(session_dir)
        assert "mcpServers" not in config
        assert config["oauthAccount"]["emailAddress"] == ACCOUNT_EMAIL
        assert (session_dir / MCP_MIRROR_MARKER).exists()  # adoption is history

        mgr._sync_sharing(session_dir, share=True)
        assert _session_config(session_dir)["mcpServers"] == {"github": GITHUB_MCP}


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

    def test_exec_default_uses_plain_env(self, manager, capture_exec, monkeypatch):
        """exec_default launches plain claude with the unmodified environment."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-key")
        with pytest.raises(_ExecCalled) as exc:
            manager.exec_default(["--resume"])

        assert exc.value.binary == "/fake/bin/claude"
        assert exc.value.argv == ["/fake/bin/claude", "--resume"]
        # Plain claude behavior: API key is NOT scrubbed (unlike session mode).
        assert exc.value.env["ANTHROPIC_API_KEY"] == "sk-ant-key"

    def test_exec_default_claude_not_on_path(self, manager, monkeypatch):
        monkeypatch.setattr(session_mod.shutil, "which", lambda name: None)
        with pytest.raises(SessionError, match="not found on PATH"):
            manager.exec_default([])


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

    @pytest.mark.parametrize(
        "dir_still_there", [True, False],
        ids=["profile_dir_present", "profile_dir_already_gone"],
    )
    def test_deleting_a_profile_takes_its_stale_marker_with_it(
        self, seeded_switcher, dir_still_there
    ):
        """The marker is a SIBLING of the profile dir, so `rmtree` no longer
        removes it. A leftover marker outlives the profile it described, and
        the next profile created for that same slot+email inherits a
        re-bootstrap flag that nothing set for it.

        The already-gone case is what ``purge`` leaves behind: it removes
        profile DIRS (``iterdir()`` filtered by ``is_dir()``) and the marker is
        a dot-FILE beside them, so it survives by design. This function then
        early-outs on the missing directory and never reaches the marker —
        two artifacts, one of them consulted.
        """
        session_dir = session_dir_for(
            seeded_switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )
        session_dir.mkdir(parents=True, exist_ok=True)
        session_mod.mark_session_stale(session_dir)
        assert session_mod.is_session_stale(session_dir), "premise: marked"
        if not dir_still_there:
            shutil.rmtree(session_dir)  # what purge does
            assert session_mod.is_session_stale(session_dir), (
                "premise: the marker outlives the dir purge removed"
            )

        seeded_switcher._delete_session_profile(ACCOUNT_NUM, ACCOUNT_EMAIL)

        assert not session_dir.exists(), "premise: the profile is gone"
        assert not session_mod.is_session_stale(session_dir), (
            "the marker outlived the profile: a freshly created profile for "
            "this slot inherits a stale flag nothing set for it"
        )

    @pytest.mark.skipif(
        sys.platform == "win32" or os.geteuid() == 0,
        reason="needs POSIX permission semantics (non-root)",
    )
    @pytest.mark.parametrize(
        "deny, marker",
        [
            ("child", "legacy"),
            ("child", None),
            (None, "legacy"),
            ("parent", "sibling"),
            ("parent", None),
        ],
        ids=[
            "denied_with_legacy_marker",
            "denied_no_marker",
            "writable_with_marker",
            "denied_parent_with_sibling_marker",
            "denied_parent_no_marker",
        ],
    )
    def test_delete_session_profile_survives_a_denied_dir_with_legacy_marker(
        self, seeded_switcher, caplog, deny, marker
    ):
        """`clear_session_stale` unlinks two marker locations: a legacy CHILD
        of the profile dir, and the SIBLING (in the profile dir's PARENT)
        that is where every marker is written today. The
        `rmtree(ignore_errors=True)` right above it already tolerates EACCES
        on the profile dir -- neither `unlink` does on its own, so only the
        COMBINATION (a denied dir + a marker actually inside it) raises,
        right after `remove_account` has already deleted the credentials but
        before it writes the roster. Covers both marker locations and both
        denied dirs (the profile dir itself, and its parent)."""
        session_dir = session_dir_for(
            seeded_switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "x.txt").write_text("keep", encoding="utf-8")
        if marker == "legacy":
            (session_dir / session_mod.STALE_MARKER).touch()
        elif marker == "sibling":
            stale_marker_for(session_dir).touch()
        denied_dir = session_dir if deny == "child" else session_dir.parent
        if deny:
            denied_dir.chmod(0o500)
        import logging

        with caplog.at_level(logging.DEBUG, logger="claude-swap"):
            try:
                seeded_switcher._delete_session_profile(ACCOUNT_NUM, ACCOUNT_EMAIL)
            finally:
                if deny:
                    try:
                        denied_dir.chmod(0o700)
                    except OSError:
                        pass

        # Tolerating the fault is the point; reporting the removal anyway is
        # not. Whatever survived on disk must be named at WARNING+, because
        # the caller (`remove_account`) has already deleted the credentials
        # and goes on to write the roster -- a slot recorded as gone with its
        # profile still there is the state nothing else looks for.
        leftovers = [
            pth
            for pth in (session_dir, stale_marker_for(session_dir),
                        session_dir / session_mod.STALE_MARKER)
            if pth.exists()
        ]
        warned = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert not leftovers or warned, (
            f"reported removal while {[str(x) for x in leftovers]} survived, "
            "and said nothing at WARNING+"
        )

    @pytest.mark.skipif(
        sys.platform == "win32" or os.geteuid() == 0,
        reason="needs POSIX permission semantics (non-root)",
    )
    @pytest.mark.parametrize("marker_lands", [True, False])
    def test_backup_credential_write_leaves_live_profile_alone_but_marks_stale(
        self, seeded_switcher, caplog, marker_lands
    ):
        """The LIVE arm used to discard `mark_session_stale`'s return value, so
        a marker that failed to land was reported to nobody -- the same
        silent-fallback shape the non-live arm's own ERROR log exists to
        avoid."""
        import logging

        session_dir = session_dir_for(
            seeded_switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )
        make_live(session_dir)
        (session_dir / ".credentials.json").write_text("live session creds")

        if not marker_lands:
            session_dir.parent.chmod(0o500)
        try:
            with caplog.at_level(logging.WARNING, logger="claude-swap"):
                seeded_switcher._write_account_credentials(
                    ACCOUNT_NUM, ACCOUNT_EMAIL, ROTATED_CREDS
                )
        finally:
            if not marker_lands:
                session_dir.parent.chmod(0o700)

        # Live copy untouched either way.
        assert (session_dir / ".credentials.json").read_text() == "live session creds"

        if marker_lands:
            assert session_mod.is_session_stale(session_dir)
            assert not any(r.levelno >= logging.ERROR for r in caplog.records)
        else:
            assert not session_mod.is_session_stale(session_dir), (
                "premise: the marker's own write target was denied"
            )
            assert any(
                r.levelno >= logging.ERROR and ACCOUNT_NUM in r.getMessage()
                for r in caplog.records
            ), (
                "the LIVE arm's failed marker was reported to nobody -- a "
                "function that reports failure to nobody has not stopped "
                "reporting success"
            )

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

        def fake_fetch(num, email, creds, is_active=False, persist_credentials=None, **kwargs):
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
            session_mod, "scan_live_sessions", lambda _dir: ([object()], 0)
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


class TestReadSessionCredentials:
    """The profile's current credential JSON: keychain first, then plaintext."""

    def test_missing_dir_returns_none(self, tmp_path):
        assert session_mod.read_session_credentials(tmp_path / "absent") is None

    def test_reads_plaintext_file_off_macos(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            Platform, "detect", classmethod(lambda cls: Platform.LINUX)
        )
        session_dir = tmp_path / "sess"
        session_dir.mkdir()
        (session_dir / ".credentials.json").write_text(
            '{"claudeAiOauth": {"accessToken": "sk-file"}}'
        )
        assert "sk-file" in session_mod.read_session_credentials(session_dir)

    def test_byte_corrupt_file_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            Platform, "detect", classmethod(lambda cls: Platform.LINUX)
        )
        session_dir = tmp_path / "sess"
        session_dir.mkdir()
        (session_dir / ".credentials.json").write_bytes(b"\xff\xfe\x00corrupt")
        assert session_mod.read_session_credentials(session_dir) is None

    def test_keychain_shadows_plaintext_on_macos(
        self, tmp_path, macos_platform, block_real_keychain
    ):
        """Claude migrates the seed into its hashed keychain entry on first
        write and rotates it there — the entry is the newest generation."""
        session_dir = tmp_path / "sess"
        session_dir.mkdir()
        (session_dir / ".credentials.json").write_text(
            '{"claudeAiOauth": {"accessToken": "sk-stale-seed"}}'
        )
        block_real_keychain.set_password(
            keychain_service_name(session_dir),
            session_mod._keychain_account_name(),
            '{"claudeAiOauth": {"accessToken": "sk-rotated"}}',
        )
        creds = session_mod.read_session_credentials(session_dir)
        assert creds is not None and "sk-rotated" in creds

    def test_macos_falls_back_to_file_without_keychain_entry(
        self, tmp_path, macos_platform, block_real_keychain
    ):
        session_dir = tmp_path / "sess"
        session_dir.mkdir()
        (session_dir / ".credentials.json").write_text(
            '{"claudeAiOauth": {"accessToken": "sk-seed"}}'
        )
        creds = session_mod.read_session_credentials(session_dir)
        assert creds is not None and "sk-seed" in creds


ACTIVE_TOKEN = "active-store-token"
CONFIG_DIR_TOKEN = "config-dir-token"
ACTIVE_CREDS = json.dumps({"claudeAiOauth": {"accessToken": ACTIVE_TOKEN}})
CONFIG_DIR_CREDS = json.dumps({"claudeAiOauth": {"accessToken": CONFIG_DIR_TOKEN}})
CONFIG_DIR_CONFIG = json.dumps(
    {
        "oauthAccount": {
            "emailAddress": "elsewhere@example.com",
            "accountUuid": "uuid-elsewhere",
            "organizationUuid": "org-elsewhere",
        }
    }
)
API_KEY = "sk-ant-api03-" + "x" * 20


class TestCaptureCredentials:
    """``add_account`` under ``CLAUDE_CONFIG_DIR``.

    The identity comes from the env-resolved ``.claude.json`` while the
    credential came from the active store, whose macOS Keychain backend ignores
    the env var — so a slot could hold one account's email and another's token.
    """

    @staticmethod
    def _switcher(platform, monkeypatch) -> ClaudeAccountSwitcher:
        monkeypatch.setattr(Platform, "detect", classmethod(lambda cls: platform))
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._init_sequence_file()
        return switcher

    @staticmethod
    def _config_dir(base: Path, *, credentials: str | None = CONFIG_DIR_CREDS) -> Path:
        directory = base / "elsewhere"
        directory.mkdir()
        (directory / ".claude.json").write_text(CONFIG_DIR_CONFIG, encoding="utf-8")
        if credentials is not None:
            (directory / ".credentials.json").write_text(credentials, encoding="utf-8")
        return directory

    @staticmethod
    def _stored(switcher: ClaudeAccountSwitcher) -> str:
        return switcher._read_account_credentials("1", "elsewhere@example.com")

    @pytest.mark.parametrize("platform", [Platform.LINUX, Platform.MACOS])
    def test_captures_config_dir_token(
        self, platform, temp_home: Path, tmp_path: Path, monkeypatch
    ):
        switcher = self._switcher(platform, monkeypatch)
        switcher._write_credentials(ACTIVE_CREDS)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(self._config_dir(tmp_path)))

        switcher.add_account()

        assert CONFIG_DIR_TOKEN in self._stored(switcher)
        assert ACTIVE_TOKEN not in self._stored(switcher)

    def test_macos_prefers_hashed_keychain_entry(
        self, temp_home: Path, tmp_path: Path, block_real_keychain, monkeypatch
    ):
        switcher = self._switcher(Platform.MACOS, monkeypatch)
        switcher._write_credentials(ACTIVE_CREDS)
        config_dir = self._config_dir(tmp_path)
        block_real_keychain.set_password(
            keychain_service_name(config_dir),
            session_mod._keychain_account_name(),
            json.dumps({"claudeAiOauth": {"accessToken": "rotated"}}),
        )
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))

        switcher.add_account()

        assert "rotated" in self._stored(switcher)

    def test_trailing_slash_still_finds_keychain_entry(
        self, temp_home: Path, tmp_path: Path, block_real_keychain, monkeypatch
    ):
        """Claude hashes the exported string verbatim, so the service name has
        to be derived from it and not from a normalized ``Path``."""
        switcher = self._switcher(Platform.MACOS, monkeypatch)
        config_dir = self._config_dir(tmp_path)
        exported = f"{config_dir}/"
        block_real_keychain.set_password(
            keychain_service_name(exported),
            session_mod._keychain_account_name(),
            json.dumps({"claudeAiOauth": {"accessToken": "rotated"}}),
        )
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", exported)

        switcher.add_account()

        assert "rotated" in self._stored(switcher)
        assert CONFIG_DIR_TOKEN not in self._stored(switcher)

    @pytest.mark.parametrize("platform", [Platform.LINUX, Platform.MACOS])
    def test_default_config_dir_uses_active_store(
        self, platform, temp_home: Path, monkeypatch
    ):
        """``CLAUDE_CONFIG_DIR=~/.claude`` names the default profile, whose
        credential is the active store's."""
        switcher = self._switcher(platform, monkeypatch)
        switcher._write_credentials(ACTIVE_CREDS)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))
        get_global_config_path().write_text(CONFIG_DIR_CONFIG, encoding="utf-8")

        switcher.add_account()

        assert ACTIVE_TOKEN in self._stored(switcher)

    @pytest.mark.skipif(sys.platform == "win32", reason="symlink creation is POSIX-only")
    def test_symlinked_default_config_dir_uses_active_store(
        self, temp_home: Path, tmp_path: Path, block_real_keychain, monkeypatch
    ):
        """A ``$HOME`` reached through a symlink spells the default profile a
        second way; it is still the profile the active store belongs to."""
        switcher = self._switcher(Platform.MACOS, monkeypatch)
        switcher._write_credentials(ACTIVE_CREDS)
        link = tmp_path / "home-link"
        link.symlink_to(Path.home())
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(link / ".claude"))
        get_global_config_path().write_text(CONFIG_DIR_CONFIG, encoding="utf-8")

        switcher.add_account()

        assert ACTIVE_TOKEN in self._stored(switcher)

    @pytest.mark.parametrize("platform", [Platform.LINUX, Platform.MACOS])
    def test_api_key_login_still_reaches_guard(
        self, platform, temp_home: Path, tmp_path: Path, monkeypatch
    ):
        """A managed key is not in any profile's OAuth store, but
        ``_reject_live_api_key_capture`` still has to answer for it."""
        switcher = self._switcher(platform, monkeypatch)
        config_dir = self._config_dir(tmp_path, credentials=None)
        (config_dir / ".claude.json").write_text(
            json.dumps(
                {
                    "oauthAccount": {"emailAddress": "elsewhere@example.com"},
                    "primaryApiKey": API_KEY,
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))

        with pytest.raises(ValidationError, match="API-key account"):
            switcher.add_account()

    def test_machine_managed_key_does_not_answer_for_config_dir(
        self, temp_home: Path, tmp_path: Path, block_real_keychain, monkeypatch
    ):
        """The unsuffixed "Claude Code" Keychain item is the default profile's.
        Reading it here would report an API-key login for an OAuth profile."""
        switcher = self._switcher(Platform.MACOS, monkeypatch)
        block_real_keychain.set_password(
            CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE,
            macos_keychain.keychain_account_name(),
            API_KEY,
        )
        monkeypatch.setenv(
            "CLAUDE_CONFIG_DIR", str(self._config_dir(tmp_path, credentials=None))
        )

        with pytest.raises(CredentialReadError):
            switcher.add_account()

    @pytest.mark.parametrize("platform", [Platform.LINUX, Platform.MACOS])
    def test_credentialless_config_dir_does_not_fall_back(
        self, platform, temp_home: Path, tmp_path: Path, monkeypatch
    ):
        switcher = self._switcher(platform, monkeypatch)
        switcher._write_credentials(ACTIVE_CREDS)
        monkeypatch.setenv(
            "CLAUDE_CONFIG_DIR", str(self._config_dir(tmp_path, credentials=None))
        )

        with pytest.raises(CredentialReadError):
            switcher.add_account()

    @pytest.mark.parametrize("platform", [Platform.LINUX, Platform.MACOS])
    def test_in_place_refresh_uses_same_source(
        self, platform, temp_home: Path, tmp_path: Path, monkeypatch
    ):
        switcher = self._switcher(platform, monkeypatch)
        switcher._write_credentials(ACTIVE_CREDS)
        config_dir = self._config_dir(tmp_path)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
        switcher.add_account()

        (config_dir / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "rotated"}}), encoding="utf-8"
        )
        switcher.add_account()

        assert "rotated" in self._stored(switcher)

    @pytest.mark.parametrize("platform", [Platform.LINUX, Platform.MACOS])
    @pytest.mark.parametrize("value", [None, ""])
    def test_no_config_dir_uses_active_store(
        self, value, platform, temp_home: Path, monkeypatch
    ):
        if value is None:
            monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        else:
            monkeypatch.setenv("CLAUDE_CONFIG_DIR", value)
        switcher = self._switcher(platform, monkeypatch)
        switcher._write_credentials(ACTIVE_CREDS)
        get_global_config_path().write_text(CONFIG_DIR_CONFIG, encoding="utf-8")

        switcher.add_account()

        assert ACTIVE_TOKEN in self._stored(switcher)

    @pytest.mark.parametrize(
        "error",
        [macos_keychain.KeychainError("keychain is locked"), OSError("no security binary")],
        ids=["keychain-error", "os-error"],
    )
    def test_unreadable_keychain_fails_closed(
        self, error, temp_home: Path, tmp_path: Path, monkeypatch
    ):
        """A locked/denied keychain must not silently capture the plaintext
        seed — it may predate an in-profile ``/login`` and belong to another
        account. One bounded retry, then the add fails. Covers the wrapper's
        whole ``KEYCHAIN_ERRORS`` contract, not just ``KeychainError``."""
        switcher = self._switcher(Platform.MACOS, monkeypatch)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(self._config_dir(tmp_path)))
        monkeypatch.setattr(session_mod, "_STRICT_KEYCHAIN_RETRY_DELAY", 0)
        calls: list[str] = []
        fake_store_read = macos_keychain.get_password

        def locked(service: str, account: str) -> str | None:
            if not service.startswith("Claude Code-credentials-"):
                return fake_store_read(service, account)  # cswap's own backup store
            calls.append(service)
            raise error

        monkeypatch.setattr(macos_keychain, "get_password", locked)

        with pytest.raises(CredentialReadError, match="unreadable"):
            switcher.add_account()

        assert len(calls) == session_mod._STRICT_KEYCHAIN_ATTEMPTS
        assert "1" not in (switcher._get_sequence_data() or {}).get("accounts", {})

    def test_transient_keychain_error_retries(
        self, temp_home: Path, tmp_path: Path, monkeypatch
    ):
        switcher = self._switcher(Platform.MACOS, monkeypatch)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(self._config_dir(tmp_path)))
        monkeypatch.setattr(session_mod, "_STRICT_KEYCHAIN_RETRY_DELAY", 0)
        outcomes = iter(["busy", json.dumps({"claudeAiOauth": {"accessToken": "rotated"}})])
        fake_store_read = macos_keychain.get_password

        def flaky(service: str, account: str) -> str | None:
            if not service.startswith("Claude Code-credentials-"):
                return fake_store_read(service, account)  # cswap's own backup store
            outcome = next(outcomes)
            if outcome == "busy":
                raise macos_keychain.KeychainError("busy")
            return outcome

        monkeypatch.setattr(macos_keychain, "get_password", flaky)

        switcher.add_account()

        assert "rotated" in self._stored(switcher)

    def test_session_read_still_falls_back_on_keychain_error(
        self, temp_home: Path, tmp_path: Path, monkeypatch
    ):
        """``read_session_credentials`` stays best-effort: the sync paths
        prefer a possibly-stale seed over aborting a listing on a locked
        keychain. Only capture is strict."""
        monkeypatch.setattr(Platform, "detect", classmethod(lambda cls: Platform.MACOS))
        session_dir = self._config_dir(tmp_path)

        def locked(service: str, account: str) -> str | None:
            raise macos_keychain.KeychainError("keychain is locked")

        monkeypatch.setattr(macos_keychain, "get_password", locked)

        creds = session_mod.read_session_credentials(session_dir)

        assert creds is not None and CONFIG_DIR_TOKEN in creds

    @staticmethod
    def _secure_dir(base: Path, token: str = "secure-store-token") -> Path:
        """A bare secure-storage dir: credentials only, no identity — claude's
        ``CLAUDE_SECURESTORAGE_CONFIG_DIR`` moves secure storage, not config."""
        directory = base / "securestore"
        directory.mkdir()
        (directory / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": token}}), encoding="utf-8"
        )
        return directory

    @pytest.mark.parametrize("platform", [Platform.LINUX, Platform.MACOS])
    def test_securestorage_dir_overrides_config_dir(
        self, platform, temp_home: Path, tmp_path: Path, monkeypatch
    ):
        """Claude sources secure storage from ``CLAUDE_SECURESTORAGE_CONFIG_DIR``
        when it is defined, ``CLAUDE_CONFIG_DIR`` otherwise; identity stays on
        ``CLAUDE_CONFIG_DIR``."""
        switcher = self._switcher(platform, monkeypatch)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(self._config_dir(tmp_path)))
        monkeypatch.setenv(
            "CLAUDE_SECURESTORAGE_CONFIG_DIR", str(self._secure_dir(tmp_path))
        )

        switcher.add_account()

        assert "secure-store-token" in self._stored(switcher)
        assert CONFIG_DIR_TOKEN not in self._stored(switcher)

    def test_securestorage_hashed_keychain_entry(
        self, temp_home: Path, tmp_path: Path, block_real_keychain, monkeypatch
    ):
        """The hashed keychain service name derives from the securestorage
        value when defined, not from ``CLAUDE_CONFIG_DIR``."""
        switcher = self._switcher(Platform.MACOS, monkeypatch)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(self._config_dir(tmp_path)))
        secure = self._secure_dir(tmp_path)
        block_real_keychain.set_password(
            keychain_service_name(str(secure)),
            session_mod._keychain_account_name(),
            json.dumps({"claudeAiOauth": {"accessToken": "rotated"}}),
        )
        monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", str(secure))

        switcher.add_account()

        assert "rotated" in self._stored(switcher)

    @pytest.mark.parametrize("platform", [Platform.LINUX, Platform.MACOS])
    def test_empty_securestorage_dir_forces_default_store(
        self, platform, temp_home: Path, tmp_path: Path, monkeypatch
    ):
        """Defined-but-empty is claude's "force the default secure store":
        unsuffixed keychain item and ``~/.claude/.credentials.json`` — even
        though ``CLAUDE_CONFIG_DIR`` names a profile with its own seed."""
        switcher = self._switcher(platform, monkeypatch)
        switcher._write_credentials(ACTIVE_CREDS)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(self._config_dir(tmp_path)))
        monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", "")

        switcher.add_account()

        assert ACTIVE_TOKEN in self._stored(switcher)
        assert CONFIG_DIR_TOKEN not in self._stored(switcher)

    @pytest.mark.parametrize("platform", [Platform.LINUX, Platform.MACOS])
    def test_securestorage_without_config_dir(
        self, platform, temp_home: Path, tmp_path: Path, monkeypatch
    ):
        """Securestorage alone moves only the credential read; identity still
        resolves through the (default) config profile."""
        switcher = self._switcher(platform, monkeypatch)
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        get_global_config_path().write_text(CONFIG_DIR_CONFIG, encoding="utf-8")
        monkeypatch.setenv(
            "CLAUDE_SECURESTORAGE_CONFIG_DIR", str(self._secure_dir(tmp_path))
        )

        switcher.add_account()

        assert "secure-store-token" in self._stored(switcher)

    @pytest.mark.parametrize("platform", [Platform.LINUX, Platform.MACOS])
    def test_empty_selected_store_does_not_leak_config_profile(
        self, platform, temp_home: Path, tmp_path: Path, monkeypatch
    ):
        """Defined-but-empty selects the default store; when that store is
        credentialless, claude sees a logged-out environment — the config
        profile's seed must not answer in its place."""
        switcher = self._switcher(platform, monkeypatch)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(self._config_dir(tmp_path)))
        monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", "")

        with pytest.raises(CredentialReadError):
            switcher.add_account()

        assert "1" not in (switcher._get_sequence_data() or {}).get("accounts", {})

    @pytest.mark.parametrize("platform", [Platform.LINUX, Platform.MACOS])
    def test_credentialless_securestorage_default_dir_does_not_fall_back(
        self, platform, temp_home: Path, tmp_path: Path, monkeypatch
    ):
        """A non-empty override naming ``~/.claude`` uses the *hashed* service
        name (claude keys the suffix off env presence, not the path), so
        neither the unsuffixed item nor the config profile's seed may answer."""
        switcher = self._switcher(platform, monkeypatch)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(self._config_dir(tmp_path)))
        monkeypatch.setenv(
            "CLAUDE_SECURESTORAGE_CONFIG_DIR", str(Path.home() / ".claude")
        )

        with pytest.raises(CredentialReadError):
            switcher.add_account()

        assert "1" not in (switcher._get_sequence_data() or {}).get("accounts", {})

class TestBootstrapRefreshRoutesThroughGate:
    """M2: the session-profile bootstrap refresh consumes the backup rt via
    the switcher's consume gate, not a direct POST of its own read."""

    def test_bootstrap_uses_gate(self, temp_home, monkeypatch):
        from claude_swap import oauth as oauth_mod
        from claude_swap.switcher import ClaudeAccountSwitcher
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        s._init_sequence_file()
        expired = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-o", "refreshToken": "rt-o",
                "expiresAt": 1000,
            }
        })
        s._write_account_credentials("1", "a@example.com", expired)
        s._write_account_config("1", "a@example.com", json.dumps({
            "oauthAccount": {"emailAddress": "a@example.com"},
        }))
        data = s._get_sequence_data()
        data["accounts"]["1"] = {"email": "a@example.com", "uuid": "u1",
                                 "organizationUuid": "", "organizationName": ""}
        data["sequence"] = [1]
        s._write_json(s.sequence_file, data)
        fresh = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-f", "refreshToken": "rt-f",
                "expiresAt": 9999999999000,
            }
        })
        gate = {}

        def mock_gate(num, email, snapshot):
            gate["args"] = (num, email)
            return oauth_mod.RefreshOutcome(fresh, None)

        monkeypatch.setattr(s, "consume_backup_grant", mock_gate)
        direct = {}

        def direct_post(credentials, **kw):
            direct["called"] = True
            return oauth_mod.RefreshOutcome(None, "transient")

        # The bypass seam: session.py no longer imports any direct refresh
        # helper, so a regression would have to call oauth's POST directly.
        monkeypatch.setattr(
            "claude_swap.oauth.try_refresh_oauth_credentials", direct_post
        )
        monkeypatch.setattr(
            "claude_swap.oauth.refresh_oauth_credentials", direct_post
        )
        from claude_swap.session import SessionManager
        mgr = SessionManager(s)
        # setup_session is the seam: it must call the gate BEFORE the
        # bootstrap lock (the gate takes the same non-reentrant FileLock).
        # (run() itself needs a claude binary on PATH — absent on CI.)
        try:
            mgr.setup_session("1", share=False)
        except Exception:
            pass  # profile validation may fail in this stub env — the
                  # assertion below is about the gate routing only
        assert gate.get("args") == ("1", "a@example.com")
        assert "called" not in direct


class TestAConsumedGrantIsNotSpentOnAProfileThatWonBootstrap:
    """A one-time grant consumed for THIS pass must reach the profile it was for.

    The consume runs before the bootstrap lock (it POSTs, and must never hold
    one). The under-lock re-check then returns early when another `cswap run`
    bootstrapped while we waited — at which point this pass has already burned
    a one-time refresh token whose successor nobody uses for the session it was
    fetched for. The successor is persisted to the BACKUP, so nothing is lost;
    what must hold is that the winning profile is seeded from that rotated
    backup rather than from the generation we just spent.
    """

    def test_the_early_return_leaves_the_profile_on_the_rotated_generation(
        self, manager, seeded_switcher, auth_status_tracks_seed, monkeypatch
    ):
        session_dir = session_dir_for(
            seeded_switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )

        # The gate rotates the backup, exactly as the real one does.
        def fake_gate(self, num, email, snapshot):
            self._write_account_credentials(num, email, ROTATED_CREDS)
            return oauth.RefreshOutcome(ROTATED_CREDS, None)

        monkeypatch.setattr(
            ClaudeAccountSwitcher, "consume_backup_grant", fake_gate
        )

        # The pre-lock check must MISS (or we never reach the consume at all);
        # the peer then bootstraps while we wait, so the under-lock re-check
        # hits — on a profile seeded BEFORE our rotation.
        calls = {"n": 0}

        def peer_bootstraps_while_we_wait(self, sdir, email, org_uuid):
            calls["n"] += 1
            if calls["n"] == 1:
                return False  # pre-lock: nothing there yet
            sdir.mkdir(parents=True, exist_ok=True)
            (sdir / ".credentials.json").write_text(CREDS)  # PRE-rotation
            return True

        monkeypatch.setattr(
            SessionManager, "_is_session_valid", peer_bootstraps_while_we_wait
        )

        got, _, _ = manager.setup_session("2", share=False)

        assert (got / ".credentials.json").read_text() == ROTATED_CREDS, (
            "the profile kept a generation the consume already spent"
        )

    def test_a_live_peer_is_not_re_seeded_beneath_itself(
        self, manager, seeded_switcher, auth_status_tracks_seed, monkeypatch
    ):
        """The re-seed above must never fire under a RUNNING claude.

        Same shape as the test above — a peer bootstraps a pre-rotation
        profile while we wait for the lock — except the peer has already
        exec'd into it. `_bootstrap` deletes the profile's Keychain entry and
        overwrites `.credentials.json`, so re-seeding there costs the peer its
        session, while deferring costs it only a generation it can still
        refresh from.

        Mutation-checked: dropping `and profile_is_quiescent(session_dir)`
        left all 1783 green. The branch fires precisely when the profile is
        VALID, which IS the live case, so it needed the guard most and had
        nothing pinning it.
        """
        session_dir = session_dir_for(
            seeded_switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )

        def fake_gate(self, num, email, snapshot):
            self._write_account_credentials(num, email, ROTATED_CREDS)
            return oauth.RefreshOutcome(ROTATED_CREDS, None)

        monkeypatch.setattr(
            ClaudeAccountSwitcher, "consume_backup_grant", fake_gate
        )

        calls = {"n": 0}

        def peer_bootstraps_while_we_wait(self, sdir, email, org_uuid):
            calls["n"] += 1
            if calls["n"] == 1:
                return False
            sdir.mkdir(parents=True, exist_ok=True)
            (sdir / ".credentials.json").write_text(CREDS)  # PRE-rotation
            return True

        monkeypatch.setattr(
            SessionManager, "_is_session_valid", peer_bootstraps_while_we_wait
        )
        # ...and that peer is RUNNING against the profile.
        live = SimpleNamespace(pid=4242)
        monkeypatch.setattr(
            session_mod, "scan_live_sessions", lambda _sdir: ([live], 0)
        )

        got, _, _ = manager.setup_session("2", share=False)

        assert (got / ".credentials.json").read_text() == CREDS, (
            "re-seeded a profile a live claude is running against; "
            "_bootstrap would delete its Keychain entry mid-session"
        )

    def test_an_unverifiable_probe_does_not_destroy_the_profile(
        self, manager, seeded_switcher, monkeypatch
    ):
        """`claude` unresolvable on PATH is not a verdict about the profile.

        `_is_session_valid` catches OSError/TimeoutExpired and returns False,
        and the post-bootstrap caller reads False as "invalid" and runs
        `_cleanup_failed_session` — which deletes the Keychain entry AND
        rmtree's the profile, then tells the user to re-add the account. So a
        missing binary, or `claude auth status` exceeding its 10s timeout on a
        loaded machine, destroys a profile that was just built.

        The file's own comment above that probe records this already happening
        on Windows via FileNotFoundError; that fixed the PATHEXT cause and left
        the collapse.
        """
        session_dir = session_dir_for(
            seeded_switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )

        def unresolvable(*args, **kwargs):
            raise FileNotFoundError(2, "No such file or directory", "claude")

        monkeypatch.setattr(session_mod.subprocess, "run", unresolvable)

        with pytest.raises(SessionError, match="could not be verified"):
            manager.setup_session(ACCOUNT_NUM, share=False)

        assert session_dir.exists(), (
            "deleted a profile it was never able to verify — the probe failing "
            "is not evidence the profile is invalid"
        )

    def test_a_genuinely_invalid_profile_is_still_cleaned_up(
        self, manager, seeded_switcher, monkeypatch
    ):
        """The control. A probe that RUNS and reports not-logged-in is a real
        verdict, and must still clean up — otherwise the test above passes on
        a version that simply never cleans up anything."""
        session_dir = session_dir_for(
            seeded_switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )

        def not_logged_in(*args, **kwargs):
            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"loggedIn": False}), stderr=""
            )

        monkeypatch.setattr(session_mod.subprocess, "run", not_logged_in)

        with pytest.raises(SessionError, match="failed validation"):
            manager.setup_session(ACCOUNT_NUM, share=False)

        assert not session_dir.exists()

    def test_a_failed_persist_does_not_seed_the_profile_from_a_spent_grant(
        self, manager, seeded_switcher, auth_status_tracks_seed, monkeypatch
    ):
        """A failed persist returns credentials AND an error — both matter.

        The gate consumes the grant, fails to write the successor, and reports
        ``transient`` while the BACKUP still holds the spent generation. Its
        own comment says callers read ``error is None`` as "safe to activate",
        and after a failed persist it is the opposite.

        Warning about it is not enough: the code continued into ``_bootstrap``,
        which re-reads the backup, and in exactly this state the backup is the
        generation whose grant was just spent. The profile is seeded with a
        dead refresh token and claude's first refresh gets invalid_grant — the
        warning scrolls past and the session is broken anyway.

        Refuse instead. The next run's gate pass adopts the stashed successor
        without consuming anything and bootstraps normally, so the recovery
        machinery this PR already builds does the rest.
        """
        session_dir = session_dir_for(
            seeded_switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )
        session_dir.mkdir(parents=True, exist_ok=True)

        def gate_consumes_then_fails_to_persist(self, num, email, snapshot):
            # Grant spent; successor NOT written to the backup, which still
            # holds CREDS, but it DID reach the stash. Exactly the shape
            # switcher.py returns when the persist fails and the successor is
            # parked for the next pass.
            return oauth.RefreshOutcome(
                ROTATED_CREDS, "transient", stashed=True
            )

        monkeypatch.setattr(
            ClaudeAccountSwitcher,
            "consume_backup_grant",
            gate_consumes_then_fails_to_persist,
        )

        with pytest.raises(SessionError, match="stashed — please retry"):
            manager.setup_session("2", share=False)

        assert seeded_switcher.read_account_credentials(
            ACCOUNT_NUM, ACCOUNT_EMAIL
        ) == CREDS, "test premise: the backup still holds the spent generation"
        # The half the old assertions never covered: what landed in the
        # PROFILE. A warning that fires while the spent generation is seeded
        # anyway pins the symptom and misses the defect.
        seeded = session_dir / ".credentials.json"
        assert not seeded.exists() or seeded.read_text() != CREDS, (
            "seeded the profile with the generation whose grant the gate had "
            "just spent — claude's first refresh gets invalid_grant"
        )

    def test_an_unpersisted_successor_is_not_reported_as_stashed(
        self, manager, seeded_switcher, auth_status_tracks_seed, monkeypatch
    ):
        """The `consume-gate-unpersisted` corner needs the OPPOSITE advice.

        There the persist AND the stash both failed, so the successor survived
        only in the return value the raise discards, and retrying POSTs the
        spent predecessor — earning a strike. `error` and `credentials` are
        identical to the stashed shape, so without the gate carrying
        `stashed` this message promises a stash that never happened and sends
        the user to retry into a guaranteed invalid_grant.
        """
        session_dir = session_dir_for(
            seeded_switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )
        session_dir.mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(
            ClaudeAccountSwitcher,
            "consume_backup_grant",
            lambda self, num, email, snap: oauth.RefreshOutcome(
                ROTATED_CREDS, "transient", stashed=False
            ),
        )

        with pytest.raises(SessionError) as exc:
            manager.setup_session("2", share=False)

        msg = str(exc.value)
        assert "neither be stored nor stashed" in msg
        assert "Fix the storage failure" in msg
        assert "the successor is stashed" not in msg, (
            "promised a stash that never happened"
        )
