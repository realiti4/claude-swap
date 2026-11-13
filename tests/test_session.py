"""Tests for session mode (claude_swap.session + the switcher guards)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sys
import threading
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_swap import fsutil
from claude_swap import macos_keychain
from claude_swap import oauth
from claude_swap import session as session_mod
from claude_swap.credentials import CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE
from claude_swap.exceptions import (
    AccountNotFoundError,
    CredentialReadError,
    LockError,
    SessionError,
    SwitchError,
    ValidationError,
)
from claude_swap.fsutil import (
    is_link,
    link_points_at,
    make_dir_link,
    replace_with_retry,
)
from claude_swap.locking import FileLock
from claude_swap.models import Platform
from claude_swap.paths import get_backup_root, get_global_config_path
from claude_swap.process_detection import ClaudeSession
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
from claude_swap.settings import set_setting
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


def _host_can_symlink(where: Path) -> bool:
    """Whether the machine running the tests can create a symlink: always off
    Windows; on Windows with Developer Mode on or an elevated token."""
    probe = where / ".cswap-test-symlink-probe"
    try:
        probe.symlink_to(where / "nonexistent")
    except OSError:
        return False
    probe.unlink()
    return True


@pytest.fixture(autouse=True)
def _windows_without_symlinks(monkeypatch):
    """A test that puts the platform on Windows gets the common Windows
    machine: one that refuses symlinks. The real probe would answer for the
    host running the tests (an admin CI runner, a developer with Developer
    Mode on), and that must not decide what the suite exercises. Directory
    links are left as they are: any Windows user may make a junction, and off
    Windows a symlink stands in for one. ``windows_symlinks`` is the other
    machine."""
    monkeypatch.setattr(session_mod, "can_symlink", lambda _dir: False)
    monkeypatch.setattr(fsutil, "_LINK_PROBES", {})


@pytest.fixture(autouse=True)
def _default_login_signed_in(request, monkeypatch):
    """The default login holds a working token unless a test says otherwise.

    The fast-path tests name the default login's account through its identity
    and never seed a credential for it, which the real check reads as signed
    out. Tests of that check ask for ``real_default_login_check``."""
    if "real_default_login_check" in request.fixturenames:
        return
    monkeypatch.setattr(
        session_mod.SessionManager, "_default_login_signed_out", lambda self: False
    )


@pytest.fixture
def real_default_login_check():
    """Run ``SessionManager._default_login_signed_out`` as it is."""


@pytest.fixture
def windows_symlinks(monkeypatch, tmp_path):
    """Windows with Developer Mode (or an elevated terminal): file symlinks
    allowed. Skipped where the host cannot make one, since the links a test
    then checks have to be real."""
    if not _host_can_symlink(tmp_path):
        pytest.skip("this host cannot create symlinks")
    monkeypatch.setattr(session_mod, "can_symlink", lambda _dir: True)


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
    probe points at the right profile.

    It does NOT keep the envs it was handed. It did, for assertions nothing
    ever wrote -- and a kept env dict is what a failing case prints.
    """

    def fake_run(cmd, env=None, **kwargs):
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

    @pytest.mark.parametrize(
        "kind, expected",
        [
            # No note: the bare kind is the whole classification.
            ("transient", "transient"),
            # ERROR_NOTES has prose for this one, and the raw kind would
            # report a failure where nothing failed.
            ("consume-busy", "another cswap surface holds the slot"),
            # The opposite direction: this one IS fatal, and the line ends
            # "continuing with the stored credentials", so a bare kind reads
            # as reassurance.
            ("invalid_grant", "refresh lineage is dead"),
            # The producer is `_classify_usage_error` (upstream #407). Witnessed
            # here as a reader: a rename of the kind is otherwise a silent
            # un-wiring, and this file has already had one.
            ("tls-cert", "the TLS certificate was refused"),
        ],
    )
    def test_the_refresh_failure_warning_outlives_the_terminal(
        self, manager, auth_status_tracks_seed, monkeypatch, capsys, caplog,
        kind, expected,
    ):
        """The sibling above proves the warning is printed; this proves it is
        kept, and that the cause is rendered the way every other surface
        renders it. Nothing else carries either: the status line names the
        account, not the refresh, and oauth logs the kind at DEBUG."""
        monkeypatch.setattr(
            ClaudeAccountSwitcher, "consume_backup_grant",
            lambda self, num, email, snap: oauth.RefreshOutcome(None, kind),
        )
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            manager.setup_session("2", share=False)

        assert "Could not refresh" in capsys.readouterr().out
        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert "Could not refresh the token for Account-2" in logged
        assert expected in logged

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
        # The account's own conversation history, which a validation failure
        # must not take with it.
        (session_dir / "projects" / "a-repo").mkdir(parents=True, exist_ok=True)
        transcript = session_dir / "projects" / "a-repo" / "chat.jsonl"
        transcript.write_text('{"type":"user"}\n', encoding="utf-8")

        with pytest.raises(SessionError, match="failed\\s+validation"):
            # Sharing off: with it on (the default) the history is merged into
            # ~/.claude before validation runs, so none is left to survive.
            manager.setup_session("2", share=False, share_history=False)

        assert transcript.exists(), (
            "DEFECT: a failed validation deleted the account's conversation "
            "history -- reachable from an upstream change alone, since a "
            "`claude auth status --json` that exits non-zero reads as invalid"
        )
        assert transcript.read_text(encoding="utf-8") == '{"type":"user"}\n', (
            "the history survived the cleanup but its content did not"
        )

        # THE SEED, not the directory. A failed validation must stop the
        # profile being reused; the account's own conversation history is
        # user data and survives.
        assert not (session_dir / ".credentials.json").exists()
        assert block_real_keychain.get_password(service, account) is None

    def test_validation_failure_takes_the_share_links_with_the_manifest(
        self, manager, seeded_switcher, monkeypatch, refresh_rotates, block_real_keychain
    ):
        def always_invalid(cmd, env=None, **kwargs):
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"loggedIn": False, "authMethod": "none"}),
                stderr="",
            )

        monkeypatch.setattr(session_mod.subprocess, "run", always_invalid)
        # `_sync_sharing` mirrors from the DEFAULT ~/.claude, so the source
        # has to exist for any history link to be created at all.
        source = Path.home() / ".claude"
        (source / "projects").mkdir(parents=True, exist_ok=True)
        (source / "history.jsonl").touch()
        session_dir = session_dir_for(
            seeded_switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )

        with pytest.raises(SessionError, match="failed\\s+validation"):
            manager.setup_session("2", share=False, share_history=True)

        # PREMISE: the run took the sharing path and the sweep ran. Without
        # these the assertions below are the absence of something that was
        # never created -- measured: the first cut of this case passed
        # against the unfixed code for exactly that reason.
        assert manager.switcher.platform != session_mod.Platform.WINDOWS
        assert not (session_dir / ".credentials.json").exists(), (
            "premise: the sweep ran"
        )
        manifest = session_dir / session_mod.SHARE_MANIFEST
        assert not manifest.exists(), "premise: the sweep took the manifest"

        for name in session_mod.HISTORY_ITEMS:
            assert not (session_dir / name).is_symlink(), (
                f"DEFECT: the share link {name!r} outlived the manifest that "
                "is the only record cswap created it. `_sync_sharing` removes "
                "a link only if the manifest names it, so this one can never "
                "be removed again -- every later plain `cswap run` writes the "
                "account's history into the DEFAULT profile, with no flag."
            )

    def test_an_unreadable_record_does_not_delete_the_profiles_credential(
        self, manager, seeded_switcher, monkeypatch, refresh_rotates,
        block_real_keychain
    ):
        """`_live_session_pids` is scan-shaped; a destructive guard is not.

        An unreadable record contributes no PID, so the chokepoint of every
        backup write reads it as "nothing is running" and deletes the
        profile's seed and its Keychain entry. Nothing can say whether a
        claude is in there, and the launch gate one frame later refuses to
        re-seed it -- so the slot is unlaunchable by any cswap command, and
        `--add-account` fires the same chokepoint again.
        """
        session_dir = session_dir_for(
            seeded_switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / ".credentials.json").write_text(
            "live-generation", encoding="utf-8"
        )
        (session_dir / "sessions").mkdir(parents=True, exist_ok=True)
        (session_dir / "sessions" / "4242.json").write_text(
            '{"pid": 4242, "cw', encoding="utf-8"
        )
        # PREMISES: nothing can certify the profile as idle, and the
        # scan-shaped question answers "nothing is running".
        assert not session_mod.profile_is_quiescent(session_dir)
        assert seeded_switcher._live_session_pids(
            ACCOUNT_NUM, ACCOUNT_EMAIL) == []

        seeded_switcher._write_account_credentials(
            ACCOUNT_NUM, ACCOUNT_EMAIL,
            json.dumps({"claudeAiOauth": {"accessToken": "sk-new",
                                          "refreshToken": "rt-new"}}),
        )

        assert (session_dir / ".credentials.json").exists(), (
            "DEFECT: the backup write deleted the credential of a profile "
            "nothing could certify as idle, and the launch gate then "
            "refuses to re-seed it"
        )

    def test_a_skipped_re_seed_does_not_promote_an_unknown_verdict(
        self, manager, seeded_switcher, monkeypatch, refresh_rotates,
        block_real_keychain
    ):
        """The promotion's premise is that a bootstrap just ran.

        `_artifacts_say_usable` reads an unreadable identity as "no drift",
        which is sound only when the artifacts are the BACKUP's. On a live
        profile the re-seed is skipped, so they are the profile's own -- and
        an in-session /login plus a torn `.claude.json` then launches under
        the account it drifted to, announced as the one that was asked for.
        """
        session_dir = session_dir_for(
            seeded_switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / ".credentials.json").write_text(
            "ANOTHER-ACCOUNTS-CREDENTIAL", encoding="utf-8"
        )
        # A torn identity: present, so `_artifacts_say_usable` says "no
        # drift", and unreadable, so nothing can contradict it.
        (session_dir / ".claude.json").write_text("{{{torn", encoding="utf-8")
        make_live(session_dir)
        # PREMISES: the re-seed is skipped, and the artifacts would read
        # usable if one had run -- `reseeded=True` is what that says. Without
        # it the unreadable identity now refuses on its own, which is a
        # different reason and would leave this test asserting nothing.
        assert not session_mod.profile_is_quiescent(session_dir)
        assert session_mod._artifacts_say_usable(
            session_dir, ACCOUNT_EMAIL, "", reseeded=True
        )
        assert not session_mod._artifacts_say_usable(
            session_dir, ACCOUNT_EMAIL, "", reseeded=False
        )

        import subprocess

        probes = []

        def timing_out(cmd, env=None, **kwargs):
            # invalid, invalid, then a timeout: the reuse path refuses, the
            # re-seed is skipped, and the third probe reaches the promotion.
            probes.append(1)
            if len(probes) <= 2:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "loggedIn": True, "authMethod": "claudeai",
                        "email": "someone-else@example.com",
                    }),
                    stderr="",
                )
            raise subprocess.TimeoutExpired(cmd, 10)

        monkeypatch.setattr(session_mod.subprocess, "run", timing_out)
        outcome = "LAUNCH"
        try:
            manager.setup_session(ACCOUNT_NUM, share=False)
        except SessionError:
            outcome = "RAISE"
        # PREMISE: the reuse path did NOT answer; this reached the arm
        # after the skipped re-seed.
        assert len(probes) >= 2, "premise: the bootstrap arm must be reached"
        assert outcome == "RAISE", (
            "DEFECT: the unknown verdict was promoted to valid on a profile "
            "the gate had just refused to re-seed, so the launch runs under "
            "whatever account the profile drifted to"
        )

        assert (session_dir / ".credentials.json").read_text(
            encoding="utf-8"
        ) == "ANOTHER-ACCOUNTS-CREDENTIAL", (
            "premise: nothing may have re-seeded the profile"
        )

    def test_a_readable_identity_does_not_promote_without_a_re_seed_either(
        self, manager, seeded_switcher, monkeypatch, refresh_rotates,
        block_real_keychain
    ):
        """The premise is the RE-SEED, and no artifact substitutes for it.

        `_artifacts_say_usable` answers from the profile's own files, and on
        this path the probe has already contradicted them twice: it reported
        another account, then stopped answering. Promoting on the third
        result -- the one that said nothing -- launches `claude` under the
        credential those two verdicts were about, announced as the slot's.

        The re-seed is what makes the artifacts the BACKUP's and worth
        believing. Skipped, they are the record of whatever the profile
        drifted to, readable or not.
        """
        session_dir = session_dir_for(
            seeded_switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / ".credentials.json").write_text(
            "ANOTHER-ACCOUNTS-CREDENTIAL", encoding="utf-8"
        )
        # READABLE and matching -- the only difference from the torn-identity
        # case beside it, so what this pins is the re-seed and nothing else.
        (session_dir / ".claude.json").write_text(json.dumps(
            {"oauthAccount": {"emailAddress": ACCOUNT_EMAIL,
                              "organizationUuid": ORG_UUID}}
        ), encoding="utf-8")
        make_live(session_dir)
        # PREMISES: the re-seed is skipped, and the artifacts DO read usable
        # -- so only the missing re-seed can be what refuses.
        assert not session_mod.profile_is_quiescent(session_dir)
        assert session_mod._artifacts_say_usable(
            session_dir, ACCOUNT_EMAIL, ORG_UUID, reseeded=False
        )

        import subprocess

        probes = []

        def timing_out(cmd, env=None, **kwargs):
            # invalid, invalid, then a timeout: the reuse path refuses, the
            # re-seed is skipped, and the third probe reaches the promotion.
            probes.append(1)
            if len(probes) <= 2:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "loggedIn": True, "authMethod": "claude.ai",
                        "email": "someone-else@example.com",
                    }),
                    stderr="",
                )
            raise subprocess.TimeoutExpired(cmd, 10)

        monkeypatch.setattr(session_mod.subprocess, "run", timing_out)
        outcome = "LAUNCH"
        try:
            manager.setup_session(ACCOUNT_NUM, share=False)
        except SessionError:
            outcome = "RAISE"
        assert len(probes) >= 3, "premise: the promotion arm must be reached"
        assert outcome == "RAISE", (
            "DEFECT: the launch carries the credential two probes just "
            "reported as another account's, promoted by a third that "
            "answered nothing"
        )

    def test_a_stuck_profile_names_the_repair_that_actually_works(
        self, manager, seeded_switcher, monkeypatch, refresh_rotates,
        block_real_keychain
    ):
        """An unreadable record makes every gate on this path defer, and
        nothing in cswap prunes a record -- so `--add-account` routes
        through the same chokepoint and repairs nothing, and
        `--remove-account` refuses for the same reason. Naming it is the
        difference between a slot the user can fix and one they cannot.
        """
        session_dir = session_dir_for(
            seeded_switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / ".credentials.json").write_text("OLD", encoding="utf-8")
        (session_dir / "sessions").mkdir(parents=True, exist_ok=True)
        (session_dir / "sessions" / "9999.json").write_text('{"pid": 9999, "cw')
        # PREMISES: not quiescent, and nothing live -- the permanent case.
        assert not session_mod.profile_is_quiescent(session_dir)
        live, unreadable = session_mod.scan_live_sessions(session_dir)
        assert live == [] and unreadable == 1

        def always_invalid(cmd, env=None, **kwargs):
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"loggedIn": False, "authMethod": "none"}),
                stderr="",
            )

        monkeypatch.setattr(session_mod.subprocess, "run", always_invalid)
        with pytest.raises(SessionError) as caught:
            manager.setup_session(ACCOUNT_NUM, share=False)

        message = str(caught.value)
        assert "could not be read" in message and "sessions" in message, (
            "DEFECT: the launch refuses and names none of the obstruction "
            f"that blocked it: {message}"
        )
        assert "--add-account" not in message, (
            "DEFECT: the launch names a remedy that routes through the same "
            "chokepoint and repairs nothing on this state"
        )

    def test_an_unspawnable_claude_names_PATH_not_the_live_instance(
        self, manager, seeded_switcher, monkeypatch, refresh_rotates,
        block_real_keychain
    ):
        """`unreachable` is the BINARY's reason, and the gate has no better one.

        The gate's own reason is owed to the user only when it is the gate
        that blocked the launch. A `claude` that cannot be spawned blocked it
        before any gate looked, so "exit the live instance" spends a running
        session -- the user's in-progress work -- to reach a retry that
        cannot spawn it either.
        """
        session_dir = session_dir_for(
            seeded_switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / ".credentials.json").write_text("OLD", encoding="utf-8")
        make_live(session_dir)
        # PREMISE: the re-seed is skipped, which is what routes a non-valid
        # verdict into the gate's-own-reason block.
        assert not session_mod.profile_is_quiescent(session_dir)

        def unspawnable(cmd, env=None, **kwargs):
            raise OSError("claude: no such file")

        monkeypatch.setattr(session_mod.subprocess, "run", unspawnable)
        with pytest.raises(SessionError) as caught:
            manager.setup_session(ACCOUNT_NUM, share=False)

        message = str(caught.value)
        assert "on PATH" in message, (
            f"DEFECT: the binary could not be spawned and the launch says "
            f"nothing about it: {message}"
        )
        assert "Exit it" not in message, (
            "DEFECT: the launch tells the user to exit a live claude to fix "
            "a binary that cannot be spawned -- it costs them the session "
            "and the retry fails identically"
        )

    @pytest.mark.parametrize("probe_fails", [False, True])
    def test_a_live_profile_that_could_not_launch_names_its_pid(
        self, manager, seeded_switcher, monkeypatch, refresh_rotates,
        block_real_keychain, probe_fails
    ):
        """CONTROL for the test above, and the remedy's own guard.

        The gate is what blocked the launch: the re-seed was skipped because
        a claude is live in there. `--add-account` routes through the same
        chokepoint and refuses, so the only remedy is the instance, and the
        user cannot act on it without the PID.

        BOTH verdicts, because only one of them is tested by accident. An
        answered probe (`invalid`) and one that timed out (`unknown`) reach
        this raise by different routes, and narrowing the gate to `invalid`
        would drop the `unknown` user onto the PATH message with every
        committed test still green -- and `unknown` + live is the cell this
        whole branch is about.
        """
        session_dir = session_dir_for(
            seeded_switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / ".credentials.json").write_text("OLD", encoding="utf-8")
        make_live(session_dir)
        live, unreadable = session_mod.scan_live_sessions(session_dir)
        # PREMISES: live, and readable -- so the unreadable-record raise
        # above it cannot be the one that fires.
        assert len(live) == 1 and unreadable == 0

        import subprocess

        def probe(cmd, env=None, **kwargs):
            if probe_fails:
                raise subprocess.TimeoutExpired(cmd, 10)
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"loggedIn": False, "authMethod": "none"}),
                stderr="",
            )

        monkeypatch.setattr(session_mod.subprocess, "run", probe)
        with pytest.raises(SessionError) as caught:
            manager.setup_session(ACCOUNT_NUM, share=False)

        message = str(caught.value)
        assert str(live[0].pid) in message, (
            f"DEFECT: the only remedy is a process the message does not "
            f"name: {message}"
        )
        assert "--add-account" not in message, (
            "DEFECT: the launch names a remedy that routes through the same "
            "liveness chokepoint and refuses"
        )

    def test_a_marker_that_cannot_be_honoured_is_announced(
        self, manager, seeded_switcher, monkeypatch, refresh_rotates,
        block_real_keychain, capsys
    ):
        """Both readers of the flag require quiescence, so on an unreadable
        record the reuse path serves the very generation the flag exists to
        retire -- and says nothing."""
        session_dir = session_dir_for(
            seeded_switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / ".credentials.json").write_text(
            "SUPERSEDED", encoding="utf-8"
        )
        (session_dir / ".claude.json").write_text(json.dumps(
            {"oauthAccount": {"emailAddress": ACCOUNT_EMAIL,
                              "accountUuid": "u"}}))
        assert session_mod.mark_session_stale(session_dir)
        (session_dir / "sessions").mkdir(parents=True, exist_ok=True)
        (session_dir / "sessions" / "9999.json").write_text('{"pid": 9999, "cw')
        # PREMISES: flagged, not quiescent, nothing live.
        assert session_mod.is_session_stale(session_dir)
        assert not session_mod.profile_is_quiescent(session_dir)
        assert session_mod.scan_live_sessions(session_dir)[0] == []

        def valid(cmd, env=None, **kwargs):
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"loggedIn": True, "authMethod": "claudeai",
                                   "email": ACCOUNT_EMAIL}),
                stderr="",
            )

        monkeypatch.setattr(session_mod.subprocess, "run", valid)
        try:
            manager.setup_session(ACCOUNT_NUM, share=False)
        except SessionError:
            pass
        said = capsys.readouterr()
        assert "cannot be honoured" in (said.out + said.err), (
            "DEFECT: the profile is flagged for re-bootstrap, the flag can "
            "never be honoured, and the launch says nothing"
        )

    def test_a_live_profile_is_not_swept_at_all(
        self, manager, seeded_switcher, monkeypatch, refresh_rotates,
        block_real_keychain
    ):
        """The sweep deletes the seed and the identity. Under a running
        claude that is the rewrite every other invalidation site in this
        file refuses to do."""
        session_dir = session_dir_for(
            seeded_switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / ".credentials.json").write_text("seed", encoding="utf-8")
        make_live(session_dir)
        assert not session_mod.profile_is_quiescent(session_dir), (
            "premise: the live record must make the profile non-quiescent"
        )

        def always_invalid(cmd, env=None, **kwargs):
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"loggedIn": False, "authMethod": "none"}),
                stderr="",
            )

        monkeypatch.setattr(session_mod.subprocess, "run", always_invalid)
        with pytest.raises(SessionError):
            manager.setup_session(ACCOUNT_NUM, share=False)

        assert (session_dir / ".credentials.json").exists(), (
            "DEFECT: the sweep ran under a live claude, deleting the seed "
            "and the identity out from under a running instance"
        )

    def test_a_live_profile_is_not_reseeded_when_the_probe_fails(
        self, manager, seeded_switcher, monkeypatch, refresh_rotates,
        block_real_keychain
    ):
        """A failed probe is not evidence that nothing is running.

        `_bootstrap` deletes the Keychain entry and overwrites
        `.credentials.json` with the backup lineage. Under a live claude
        that is the generation the running instance rotated into, held
        nowhere else — its next refresh gets invalid_grant and the session
        dies. The raise that follows says the profile was left in place.
        """
        session_dir = session_dir_for(
            seeded_switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / ".credentials.json").write_text(
            "live-generation", encoding="utf-8"
        )
        make_live(session_dir)
        assert not session_mod.profile_is_quiescent(session_dir)

        def unreachable(cmd, env=None, **kwargs):
            raise OSError("claude is mid-update")

        monkeypatch.setattr(session_mod.subprocess, "run", unreachable)
        with pytest.raises(SessionError, match="left in place"):
            manager.setup_session(ACCOUNT_NUM, share=False)

        seed = (session_dir / ".credentials.json").read_text(encoding="utf-8")
        print(f"\nSEED AFTER: {seed[:60]!r}")
        assert seed == "live-generation", (
            "DEFECT: _bootstrap rewrote a LIVE profile's credential, and the "
            "error the user sees says the profile is left in place"
        )

    @staticmethod
    def _spy_on_sweep(monkeypatch) -> list[str]:
        """Record every `_cleanup_failed_session`, still running the real one."""
        swept: list[str] = []
        real = session_mod.SessionManager._cleanup_failed_session

        def spy(self, path):
            swept.append(str(path))
            return real(self, path)

        monkeypatch.setattr(
            session_mod.SessionManager, "_cleanup_failed_session", spy
        )
        return swept

    def test_a_swept_profile_keeps_the_only_copy_of_its_mcp_servers(
        self, manager, seeded_switcher, monkeypatch, refresh_rotates,
        block_real_keychain
    ):
        """`_sync_sharing` can write the stash minutes before the sweep runs.

        `_stash_displaced_mcp` is write-once and refuses to reset a profile's
        MCP servers unless this file already holds them, so it is the only
        copy of the profile's pre-mirror definitions. Sweeping it destroys
        exactly what that refusal exists to preserve, and nothing re-derives
        it: the definitions it held are gone from the profile too.
        """
        session_dir = session_dir_for(
            seeded_switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / ".credentials.json").write_text("OLD", encoding="utf-8")
        stash = session_dir / session_mod.MCP_DISPLACED_STASH
        stash.write_text(json.dumps(
            {"schemaVersion": 1, "mcpServers": {"pre-feature": LOCAL_MCP}}
        ), encoding="utf-8")

        def always_invalid(cmd, env=None, **kwargs):
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"loggedIn": False, "authMethod": "none"}),
                stderr="",
            )

        monkeypatch.setattr(session_mod.subprocess, "run", always_invalid)
        with pytest.raises(SessionError):
            manager.setup_session(ACCOUNT_NUM, share=False)

        # PREMISE: the sweep ran. Without this the assert below passes on a
        # version that simply never cleans anything up.
        assert not (session_dir / ".credentials.json").exists(), (
            "premise: the sweep did not run, so this proves nothing"
        )
        assert stash.exists() and json.loads(stash.read_text())[
            "mcpServers"
        ] == {"pre-feature": LOCAL_MCP}, (
            "DEFECT: the sweep took the write-once stash, which is the only "
            "copy of the profile's pre-mirror MCP definitions"
        )

    def test_a_probe_that_failed_never_reaches_the_sweep(
        self, manager, seeded_switcher, monkeypatch, refresh_rotates,
        block_real_keychain
    ):
        """The premise the reuse gate's strictness rests on.

        Refusing to REUSE an unreadable identity sends the launch down the
        bootstrap path, where a probe that then times out must not be read as
        a verdict about the profile: the sweep deletes the seed and the
        Keychain entry over nothing worse than a loaded machine (#224).

        The re-seed must NOT promote the timeout, or this passes without ever
        reaching the arm it is about -- so the backup names another account,
        which is drift the promotion still refuses post-bootstrap.
        """
        seeded_switcher._write_account_config(
            ACCOUNT_NUM, ACCOUNT_EMAIL,
            json.dumps({"oauthAccount": {
                "emailAddress": "someone-else@example.com",
                "organizationUuid": ORG_UUID}, "theme": "dark"}),
        )
        session_dir = session_dir_for(
            seeded_switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / ".credentials.json").write_text("OLD", encoding="utf-8")
        (session_dir / ".claude.json").write_text("{{{torn", encoding="utf-8")

        swept = self._spy_on_sweep(monkeypatch)

        import subprocess

        def times_out(cmd, env=None, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 10)

        monkeypatch.setattr(session_mod.subprocess, "run", times_out)
        with pytest.raises(SessionError) as caught:
            manager.setup_session(ACCOUNT_NUM, share=False)

        # PREMISE: the verdict stayed "unknown" and reached the probe-failure
        # raise. Anything else and this asserts about an arm it never entered.
        assert "could not be verified" in str(caught.value), str(caught.value)
        assert swept == [], (
            "DEFECT: a probe that merely timed out destroyed a profile it "
            "never managed to look at"
        )

    def test_an_invalid_verdict_still_reaches_the_sweep(
        self, manager, seeded_switcher, monkeypatch, refresh_rotates,
        block_real_keychain
    ):
        """CONTROL for the test above: a spy that can never see a sweep would
        pass it on a version that swept every launch."""
        session_dir = session_dir_for(
            seeded_switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / ".credentials.json").write_text("OLD", encoding="utf-8")
        (session_dir / ".claude.json").write_text("{{{torn", encoding="utf-8")

        swept = self._spy_on_sweep(monkeypatch)

        def always_invalid(cmd, env=None, **kwargs):
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"loggedIn": False, "authMethod": "none"}),
                stderr="",
            )

        monkeypatch.setattr(session_mod.subprocess, "run", always_invalid)
        with pytest.raises(SessionError):
            manager.setup_session(ACCOUNT_NUM, share=False)

        assert swept, "premise: the spy cannot see a sweep, so it proves nothing"

    def test_a_swept_profile_keeps_its_liveness_ledger(
        self, manager, seeded_switcher, monkeypatch, refresh_rotates,
        block_real_keychain
    ):
        """`sessions/` is what `scan_sessions` reads, and it reads nothing
        else. Sweeping it makes `profile_is_quiescent` answer True forever,
        so every guard that asks whether a claude is running goes blind."""
        session_dir = session_dir_for(
            seeded_switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / ".credentials.json").write_text("seed", encoding="utf-8")
        # A record for a pid that is NOT alive: the ledger exists, and the
        # profile is quiescent, so the sweep really runs.
        make_live(session_dir, pid=2**22 + 12345)
        assert session_mod.profile_is_quiescent(session_dir), (
            "premise: a dead record leaves the profile quiescent, or the "
            "sweep is skipped and this case measures nothing"
        )

        def always_invalid(cmd, env=None, **kwargs):
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"loggedIn": False, "authMethod": "none"}),
                stderr="",
            )

        monkeypatch.setattr(session_mod.subprocess, "run", always_invalid)
        with pytest.raises(SessionError):
            manager.setup_session(ACCOUNT_NUM, share=False)

        assert not (session_dir / ".credentials.json").exists(), (
            "premise: the sweep must have run"
        )
        assert (session_dir / "sessions").is_dir(), (
            "DEFECT: the sweep took the liveness ledger. `scan_sessions` "
            "reads nothing else, so `profile_is_quiescent` now answers True "
            "for this profile forever and the removal, the swap, the move "
            "and the purge all stop seeing a live claude"
        )

    def test_a_swept_profile_is_RE_BOOTSTRAPPED_on_the_next_launch(
        self, manager, seeded_switcher, monkeypatch, refresh_rotates,
        block_real_keychain
    ):
        """The behaviour, not the flag: the next launch must re-seed.

        Asserting the marker is a proxy -- the stale arm consumes it and
        then re-asks validity, which answers from the same artifacts that
        made the sweep's verdict unprovable, so the profile passed as
        usable and `claude` was exec'd into an empty directory.
        """
        session_dir = session_dir_for(
            seeded_switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )
        svc = session_mod.keychain_service_name(session_dir)
        acct = session_mod._keychain_account_name()
        real_get = session_mod.macos_keychain.get_password
        real_del = session_mod.macos_keychain.delete_password

        def locked(real):
            def _f(service, account, *a, **k):
                if (service, account) == (svc, acct):
                    raise session_mod.macos_keychain.KeychainError("locked")
                return real(service, account, *a, **k)
            return _f

        monkeypatch.setattr(
            session_mod.macos_keychain, "get_password", locked(real_get))
        monkeypatch.setattr(
            session_mod.macos_keychain, "delete_password", locked(real_del))

        def always_invalid(cmd, env=None, **kwargs):
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"loggedIn": False, "authMethod": "none"}),
                stderr="",
            )

        monkeypatch.setattr(session_mod.subprocess, "run", always_invalid)
        with pytest.raises(SessionError, match="failed\\s+validation"):
            manager.setup_session(ACCOUNT_NUM, share=False)

        # PREMISES: the sweep really emptied it, and the flag really landed.
        assert manager.switcher.platform == session_mod.Platform.MACOS
        assert not (session_dir / ".credentials.json").exists(), (
            "premise: the sweep took the seed"
        )
        assert session_mod.is_session_stale(session_dir), (
            "premise: the sweep flagged it for re-bootstrap"
        )

        # The next launch: the probe merely TIMES OUT, which is what makes
        # the artifacts alone decide.
        def times_out(cmd, env=None, **kwargs):
            raise session_mod.subprocess.TimeoutExpired(cmd, 10)

        monkeypatch.setattr(session_mod.subprocess, "run", times_out)
        # PREMISES that hold in BOTH worlds and pin the exact combination
        # the reuse check decides on: the keychain is unreadable so material
        # leans PRESENT, and the identity the sweep deleted is absent.
        assert session_mod._may_have_credential_material(session_dir) is True, (
            "premise: an unreadable keychain reads as material-may-be-present"
        )
        assert not (session_dir / ".claude.json").exists(), (
            "premise: the sweep took the recorded identity"
        )
        assert manager._session_validity(
            session_dir, ACCOUNT_EMAIL, ORG_UUID) == "unknown", (
            "premise: the probe merely did not answer, so artifacts decide"
        )

        manager.setup_session(ACCOUNT_NUM, share=False)

        assert (session_dir / ".credentials.json").exists(), (
            "DEFECT: the launch after a swept profile REUSED it. The stale "
            "arm consumed the marker and then re-asked validity, which "
            "answers from the same unprovable artifacts, so claude is "
            "exec'd into a directory with no credential and no identity -- "
            "any login there writes to the profile, never to the backup"
        )

    def test_a_swept_profile_is_not_reused_on_the_next_launch(
        self, manager, seeded_switcher, monkeypatch, refresh_rotates,
        block_real_keychain
    ):
        """A profile the cleanup swept must re-bootstrap, never be reused.

        The sweep spares user data, so the directory now SURVIVES and
        `_session_validity` no longer short-circuits on `not is_dir()`. On
        macOS the seed is not the only credential material: the keychain
        delete is best-effort, so a later probe TIMEOUT falls to
        `_artifacts_say_usable`, where an unreadable entry reads as present
        and the deleted `.claude.json` reads as "no drift".
        """
        session_dir = session_dir_for(
            seeded_switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )
        svc = session_mod.keychain_service_name(session_dir)
        acct = session_mod._keychain_account_name()
        real_get = session_mod.macos_keychain.get_password
        real_del = session_mod.macos_keychain.delete_password

        def locked(real):
            def _f(service, account, *a, **k):
                if (service, account) == (svc, acct):
                    raise session_mod.macos_keychain.KeychainError("locked")
                return real(service, account, *a, **k)
            return _f

        monkeypatch.setattr(
            session_mod.macos_keychain, "get_password", locked(real_get))
        monkeypatch.setattr(
            session_mod.macos_keychain, "delete_password", locked(real_del))

        def always_invalid(cmd, env=None, **kwargs):
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"loggedIn": False, "authMethod": "none"}),
                stderr="",
            )

        monkeypatch.setattr(session_mod.subprocess, "run", always_invalid)
        with pytest.raises(SessionError, match="failed\\s+validation"):
            manager.setup_session(ACCOUNT_NUM, share=False)

        # PREMISES: the state that makes the reuse check answer "usable".
        assert manager.switcher.platform == session_mod.Platform.MACOS
        assert session_dir.is_dir(), "premise: the sweep spares the directory"
        assert not (session_dir / ".credentials.json").exists()
        assert not (session_dir / ".claude.json").exists()
        assert session_mod._may_have_credential_material(session_dir) is True, (
            "premise: the unreadable keychain reads as material-may-be-present"
        )

        def times_out(cmd, env=None, **kwargs):
            raise session_mod.subprocess.TimeoutExpired(cmd, 10)

        monkeypatch.setattr(session_mod.subprocess, "run", times_out)
        assert manager._session_validity(
            session_dir, ACCOUNT_EMAIL, ORG_UUID) == "unknown", (
            "premise: the next launch's probe merely did not answer"
        )

        assert session_mod.is_session_stale(session_dir), (
            "DEFECT: the profile that FAILED validation carries no "
            "re-bootstrap flag, so the next launch takes the reuse fast "
            "path and execs claude into a profile with no identity and a "
            "credential that just failed -- any login there writes to the "
            "profile and never to the slot's backup"
        )
        assert session_mod.profile_is_quiescent(session_dir), (
            "control: the flag is only honoured on a quiescent profile"
        )

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

    def test_probe_timeout_refuses_an_unreadable_identity(
        self, manager, tmp_path, monkeypatch
    ):
        """A broken .claude.json is not a profile we may REUSE.

        Reuse runs before any bootstrap, so the artifacts are the profile's
        own: an in-session /login plus a torn `.claude.json` would otherwise
        exec claude into the account it drifted to.
        """
        tmp_path.mkdir(exist_ok=True)
        self._seed_profile(tmp_path)
        (tmp_path / ".claude.json").write_text("not json")
        self._probe_times_out(monkeypatch)
        assert not manager._is_session_valid(tmp_path, ACCOUNT_EMAIL, ORG_UUID)

    def test_a_readable_identity_still_survives_a_probe_timeout(
        self, manager, tmp_path, monkeypatch
    ):
        """CONTROL: the leniency is gone only for the unreadable case."""
        tmp_path.mkdir(exist_ok=True)
        self._seed_profile(tmp_path)
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

    def test_repoints_a_managed_link_gone_stale(self, share_setup, temp_home):
        """Recorded in the manifest, so cswap's own: a ~/.claude that moved,
        say. Pointed back at the source."""
        source, session_dir, mgr = share_setup
        elsewhere = temp_home / "elsewhere.json"
        elsewhere.write_text("{}")
        (session_dir / "settings.json").symlink_to(elsewhere)
        (session_dir / SHARE_MANIFEST).write_text(
            json.dumps({"items": ["settings.json"], "mode": "symlink"})
        )

        mgr._sync_sharing(session_dir, share=True)

        assert (session_dir / "settings.json").readlink() == source / "settings.json"

    def test_a_link_of_the_users_own_is_left_alone(
        self, share_setup, temp_home, capsys
    ):
        """Not in the manifest and leading somewhere else: a redirect the
        user set up, kept like a directory of their own (and its target is
        never touched)."""
        source, session_dir, mgr = share_setup
        elsewhere = temp_home / "elsewhere.json"
        elsewhere.write_text('{"mine": true}')
        (session_dir / "settings.json").symlink_to(elsewhere)

        mgr._sync_sharing(session_dir, share=True)

        assert (session_dir / "settings.json").readlink() == elsewhere
        assert elsewhere.read_text() == '{"mine": true}'
        assert "Not sharing settings.json" in capsys.readouterr().out
        manifest = json.loads((session_dir / SHARE_MANIFEST).read_text())
        assert "settings.json" not in manifest["items"]

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
    """Windows without a symlink privilege, the common machine: files are
    copies re-synced on every launch, directories are links (issue #292) —
    a junction on a Windows host, which any user may make; off Windows a
    symlink stands in for it, so this runs on any host."""

    @pytest.fixture
    def windows_mgr(self, share_setup):
        source, session_dir, mgr = share_setup
        mgr.switcher.platform = Platform.WINDOWS
        return source, session_dir, mgr

    def test_files_are_copies_and_directories_links(self, windows_mgr):
        source, session_dir, mgr = windows_mgr
        mgr._sync_sharing(session_dir, share=True)

        assert (session_dir / "settings.json").is_file()
        assert not is_link(session_dir / "settings.json")
        assert is_link(session_dir / "skills")
        assert link_points_at(session_dir / "skills", source / "skills")
        assert (session_dir / "skills" / "a.md").read_text() == "skill"
        manifest = json.loads((session_dir / SHARE_MANIFEST).read_text())
        assert set(manifest["items"]) == {"settings.json", "CLAUDE.md", "skills"}
        assert manifest["mode"] == "copy"

    def test_a_directory_link_is_live_both_ways(self, windows_mgr):
        source, session_dir, mgr = windows_mgr
        mgr._sync_sharing(session_dir, share=True)
        (source / "skills" / "new.md").write_text("added later")
        (session_dir / "skills" / "from-session.md").write_text("written inside")

        assert (session_dir / "skills" / "new.md").read_text() == "added later"
        assert (source / "skills" / "from-session.md").read_text() == "written inside"

    def test_resync_overwrites_managed_copies(self, windows_mgr):
        source, session_dir, mgr = windows_mgr
        mgr._sync_sharing(session_dir, share=True)
        (source / "settings.json").write_text('{"changed": true}')

        mgr._sync_sharing(session_dir, share=True)

        assert (session_dir / "settings.json").read_text() == '{"changed": true}'

    def test_resync_keeps_a_correct_directory_link(self, windows_mgr, monkeypatch):
        source, session_dir, mgr = windows_mgr
        mgr._sync_sharing(session_dir, share=True)
        removed = []
        real = session_mod.remove_link

        def tracking(link):
            removed.append(Path(link).name)
            real(link)

        monkeypatch.setattr(session_mod, "remove_link", tracking)
        mgr._sync_sharing(session_dir, share=True)

        assert removed == []
        assert is_link(session_dir / "skills")

    def test_a_managed_copy_becomes_a_link(self, windows_mgr, monkeypatch):
        """A profile from before directory links, or one on a volume that
        refused one (FAT, a network share), holds a copy; the next launch
        that can link does, and a launch that cannot copies as before."""
        source, session_dir, mgr = windows_mgr
        monkeypatch.setattr(session_mod, "can_junction", lambda *_a: False)
        mgr._sync_sharing(session_dir, share=True)
        assert (session_dir / "skills").is_dir()
        assert not is_link(session_dir / "skills")
        assert (session_dir / "skills" / "a.md").read_text() == "skill"

        monkeypatch.setattr(session_mod, "can_junction", lambda *_a: True)
        mgr._sync_sharing(session_dir, share=True)

        assert is_link(session_dir / "skills")
        assert (source / "skills" / "a.md").read_text() == "skill"

    def test_a_hand_made_link_to_the_same_place_is_adopted(self, windows_mgr):
        source, session_dir, mgr = windows_mgr
        make_dir_link(source / "skills", session_dir / "skills")

        mgr._sync_sharing(session_dir, share=True)

        manifest = json.loads((session_dir / SHARE_MANIFEST).read_text())
        assert "skills" in manifest["items"]
        assert is_link(session_dir / "skills")

    def test_a_hand_made_link_elsewhere_is_left_alone(
        self, windows_mgr, tmp_path, capsys
    ):
        """Not in the manifest and leading somewhere else: the user's own
        redirect (#292 names people who junctioned by hand before cswap
        could). Kept, like a directory of their own."""
        source, session_dir, mgr = windows_mgr
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "mine.md").write_text("mine")
        make_dir_link(elsewhere, session_dir / "skills")

        mgr._sync_sharing(session_dir, share=True)

        assert link_points_at(session_dir / "skills", elsewhere)
        assert (elsewhere / "mine.md").read_text() == "mine"
        assert "Not sharing skills: the session profile already has its own link" in (
            capsys.readouterr().out
        )
        manifest = json.loads((session_dir / SHARE_MANIFEST).read_text())
        assert "skills" not in manifest["items"]

    def test_a_managed_link_gone_stale_is_repointed(self, windows_mgr, tmp_path):
        source, session_dir, mgr = windows_mgr
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "mine.md").write_text("mine")
        make_dir_link(elsewhere, session_dir / "skills")
        (session_dir / SHARE_MANIFEST).write_text(
            json.dumps({"items": ["skills"], "mode": "copy"})
        )

        mgr._sync_sharing(session_dir, share=True)

        assert link_points_at(session_dir / "skills", source / "skills")
        assert (elsewhere / "mine.md").read_text() == "mine"

    def test_a_directory_link_that_fails_falls_back_to_a_copy(
        self, windows_mgr, monkeypatch
    ):
        """The probe said yes and the link itself still failed (a target the
        volume will not link to, say): the copy of old rather than nothing,
        and the managed copy that was there is not lost either."""
        source, session_dir, mgr = windows_mgr
        monkeypatch.setattr(session_mod, "can_junction", lambda *_a: False)
        mgr._sync_sharing(session_dir, share=True)  # a managed copy, as before

        monkeypatch.setattr(session_mod, "can_junction", lambda *_a: True)

        def refuse(target, link):
            raise OSError(1, "The request is not supported")

        monkeypatch.setattr(session_mod, "make_dir_link", refuse)
        mgr._sync_sharing(session_dir, share=True)

        assert (session_dir / "skills").is_dir()
        assert not is_link(session_dir / "skills")
        assert (session_dir / "skills" / "a.md").read_text() == "skill"
        manifest = json.loads((session_dir / SHARE_MANIFEST).read_text())
        assert "skills" in manifest["items"]

    def test_a_real_directory_in_the_profile_is_left_alone(self, windows_mgr, capsys):
        source, session_dir, mgr = windows_mgr
        (session_dir / "skills").mkdir()
        (session_dir / "skills" / "own.md").write_text("own")

        mgr._sync_sharing(session_dir, share=True)

        assert "Not sharing skills" in capsys.readouterr().out
        assert not is_link(session_dir / "skills")
        assert (session_dir / "skills" / "own.md").read_text() == "own"

    def test_no_share_removes_copies_and_links_but_not_their_targets(
        self, windows_mgr
    ):
        source, session_dir, mgr = windows_mgr
        mgr._sync_sharing(session_dir, share=True)
        mgr._sync_sharing(session_dir, share=False)

        assert not (session_dir / "settings.json").exists()
        assert not (session_dir / "skills").exists()
        assert not is_link(session_dir / "skills")
        assert (source / "skills" / "a.md").read_text() == "skill"


@pytest.mark.skipif(sys.platform != "win32", reason="real junctions are Windows-only")
class TestSharingRealJunctions:
    def test_real_junction_share_and_removal(self, share_setup):
        source, session_dir, mgr = share_setup
        mgr.switcher.platform = Platform.WINDOWS  # seeded_switcher pins macOS
        mgr._sync_sharing(session_dir, share=True)

        skills = session_dir / "skills"
        assert skills.is_junction()
        (source / "skills" / "new.md").write_text("live")
        assert (skills / "new.md").read_text() == "live"

        mgr._sync_sharing(session_dir, share=True)
        assert skills.is_junction()

        mgr._sync_sharing(session_dir, share=False)
        assert not skills.exists()
        assert (source / "skills" / "a.md").read_text() == "skill"


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


def test_the_isolating_fixture_really_cleared_every_outbound_hop():
    """The subject is `_isolate_real_home`, not this process's environment.

    Every double here forwards `os.environ` into the thing it fakes, and pytest
    prints whatever a failing assertion touched -- so one unrelated red case
    would publish `https://user:secret@host` into the log. The autouse fixture
    deletes those names before any body runs; this asserts it did.

    So it fails when the fixture STOPS deleting, and only on a machine that
    has a hop set -- vacuous elsewhere, which is most CI. It is not a check on
    the operator's shell, and reading it as one credits it with reach it does
    not have.
    """
    from tests.conftest import outbound_hop_names

    present = outbound_hop_names(os.environ)
    assert present == [], (
        f"the suite is running with {present} set; a failing assertion that "
        f"touches an env dict prints their values"
    )


def test_the_hop_selection_is_by_shape_not_a_hand_written_list():
    """`urllib` decides what an outbound hop is by SHAPE, so this must too.

    `urllib.request.getproxies()` treats any ``<scheme>_proxy`` as a hop, and
    the default opener installs a ``ProxyHandler`` built from it -- so the set
    is open-ended and a literal tuple goes stale the first time an operator's
    network needs SOCKS or FTP. What it misses is not a flag: it is a URL that
    can carry ``user:secret@host``, which is the whole exposure.
    """
    from tests.conftest import outbound_hop_names

    env = {
        "HTTPS_PROXY": "x", "no_proxy": "y",          # the ones a list catches
        "SOCKS_PROXY": "socks5://u:s@gw:1080",        # the ones it does not
        "FTP_PROXY": "http://u:s@h:3128",
        "NODE_USE_ENV_PROXY": "1",
        "PATH": "/usr/bin", "PROXY_MODE": "z",        # controls: neither is a hop
    }
    assert outbound_hop_names(env) == [
        "FTP_PROXY", "HTTPS_PROXY", "NODE_USE_ENV_PROXY", "SOCKS_PROXY", "no_proxy",
    ], (
        "the selection missed a hop urllib would read, or claimed one it would "
        "not -- PROXY_MODE is not a hop and PATH is not either"
    )


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

        # NAMES, not the mapping. A failing `not in` renders the whole dict,
        # and this one is the process environment: pytest keeps its head and
        # tail, so a token sitting at either end is printed with it.
        assert "CLAUDE_CONFIG_DIR" not in sorted(exc.value.env)
        assert "already the active default login" in capsys.readouterr().out

    def test_require_session_refuses_fast_path(
        self, manager, capture_exec, monkeypatch
    ):
        monkeypatch.setattr(
            manager.switcher,
            "_get_current_account",
            lambda: (ACCOUNT_EMAIL, ORG_UUID),
        )
        # SessionError, not _ExecCalled: nothing may launch.
        with pytest.raises(SessionError, match="active default login"):
            manager.run("2", [], require_session=True)

    def test_require_session_is_inert_off_the_active_account(
        self, manager, capture_exec, auth_status_tracks_seed, refresh_rotates
    ):
        with pytest.raises(_ExecCalled) as exc:
            manager.run("2", [], require_session=True)

        session_dir = session_dir_for(
            manager.switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )
        assert exc.value.env["CLAUDE_CONFIG_DIR"] == str(session_dir)

    def test_preset_config_dir_still_uses_session_for_other_account(
        self,
        manager,
        capture_exec,
        monkeypatch,
        auth_status_tracks_seed,
        refresh_rotates,
        capsys,
    ):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/somewhere/else")
        # The default login is a different account: no fast path.
        monkeypatch.setattr(
            session_mod.SessionManager,
            "_default_login_identity",
            staticmethod(lambda: ("other@example.com", "")),
        )
        with pytest.raises(_ExecCalled) as exc:
            manager.run("2", [])

        session_dir = session_dir_for(
            manager.switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )
        assert exc.value.env["CLAUDE_CONFIG_DIR"] == str(session_dir)
        assert "overriding it for this launch" in capsys.readouterr().out

    def test_preset_config_dir_fast_paths_for_active_default_account(
        self, manager, capture_exec, monkeypatch, capsys
    ):
        # A session for the active default account would consume the live
        # login's refresh token and log out every claude running on it.
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/somewhere/else")
        monkeypatch.setattr(
            session_mod.SessionManager,
            "_default_login_identity",
            staticmethod(lambda: (ACCOUNT_EMAIL, ORG_UUID)),
        )
        with pytest.raises(_ExecCalled) as exc:
            manager.run("2", [])

        assert "CLAUDE_CONFIG_DIR" not in exc.value.env
        assert "already the active default login" in capsys.readouterr().out

    def test_preset_config_dir_require_session_refuses_active_default(
        self, manager, capture_exec, monkeypatch
    ):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/somewhere/else")
        monkeypatch.setattr(
            session_mod.SessionManager,
            "_default_login_identity",
            staticmethod(lambda: (ACCOUNT_EMAIL, ORG_UUID)),
        )
        with pytest.raises(SessionError, match="active default"):
            manager.run("2", [], require_session=True)

    @pytest.mark.parametrize("preset", [False, True])
    def test_signed_out_default_login_launches_the_session_profile(
        self,
        manager,
        capture_exec,
        monkeypatch,
        auth_status_tracks_seed,
        refresh_rotates,
        capsys,
        real_default_login_check,
        preset,
    ):
        # ~/.claude.json still names account 2, but its login was signed out:
        # plain claude would stop at "Not logged in" on every launch.
        if preset:
            monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/somewhere/else")
            monkeypatch.setattr(
                session_mod.SessionManager,
                "_default_login_identity",
                staticmethod(lambda: (ACCOUNT_EMAIL, ORG_UUID)),
            )
        else:
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
        out = capsys.readouterr().out
        assert "holds no usable token" in out
        assert "already the active default login" not in out

    def test_signed_out_default_login_is_not_refused_by_require_session(
        self,
        manager,
        capture_exec,
        monkeypatch,
        auth_status_tracks_seed,
        refresh_rotates,
        real_default_login_check,
    ):
        monkeypatch.setattr(
            manager.switcher,
            "_get_current_account",
            lambda: (ACCOUNT_EMAIL, ORG_UUID),
        )
        with pytest.raises(_ExecCalled) as exc:
            manager.run("2", [], require_session=True)

        assert "CLAUDE_CONFIG_DIR" in exc.value.env

    def test_working_default_login_keeps_the_fast_path(
        self,
        manager,
        temp_home,
        capture_exec,
        monkeypatch,
        capsys,
        real_default_login_check,
    ):
        # The check itself runs here: a login that still holds a refresh
        # token must never be copied into a session profile.
        (temp_home / ".claude" / ".credentials.json").write_text(CREDS)
        monkeypatch.setattr(
            manager.switcher,
            "_get_current_account",
            lambda: (ACCOUNT_EMAIL, ORG_UUID),
        )
        with pytest.raises(_ExecCalled) as exc:
            manager.run("2", [])

        assert "CLAUDE_CONFIG_DIR" not in exc.value.env
        assert "already the active default login" in capsys.readouterr().out

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
        # NAMES, for the reason above.
        exec_env_names = sorted(exc.value.env)
        assert "ANTHROPIC_API_KEY" not in exec_env_names
        assert "ANTHROPIC_AUTH_TOKEN" not in exec_env_names
        assert exc.value.env["UNRELATED_VAR"] == "kept"

    @pytest.mark.parametrize(
        "env, logged_, not_logged",
        [
            (
                {"ANTHROPIC_API_KEY": "sk-ant-key", "ANTHROPIC_AUTH_TOKEN": "tok"},
                ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"],
                [],
            ),
            ({"CLAUDE_CONFIG_DIR": "/x"}, ["overriding it for this launch"], []),
            # The control: with neither var set, both rows above would pass
            # against a logger that warned unconditionally.
            ({}, [], ["overriding it for this launch", "ANTHROPIC_API_KEY"]),
        ],
        ids=["scrub", "config_dir", "control"],
    )
    def test_the_launch_warnings_outlive_the_terminal(
        self,
        manager,
        capture_exec,
        monkeypatch,
        auth_status_tracks_seed,
        refresh_rotates,
        caplog,
        env,
        logged_,
        not_logged,
    ):
        """The account is still carried by the status line after the exec;
        "the value you set was overridden" is carried by nothing."""
        for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CONFIG_DIR"):
            monkeypatch.delenv(var, raising=False)
        for var, value in env.items():
            monkeypatch.setenv(var, value)
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            with pytest.raises(_ExecCalled):
                manager.run("2", [])
        records = "\n".join(r.getMessage() for r in caplog.records)
        for expected in logged_:
            assert expected in records
        for absent in not_logged:
            assert absent not in records

    def test_our_own_config_dir_is_neither_printed_nor_logged(
        self,
        manager,
        capture_exec,
        monkeypatch,
        auth_status_tracks_seed,
        refresh_rotates,
        capsys,
        caplog,
    ):
        """run() sets CLAUDE_CONFIG_DIR to a session profile, so a nested
        `cswap run` takes this branch on every launch. Those records would
        bury the one the log exists for: a value the USER set.

        PRINTED-BUT-NOT-LOGGED WAS THE WRONG HALF TO KEEP. That is the
        print-only shape this whole file guards against -- the exec clears
        the screen, so the notice reached nobody either way. Saying nothing
        about a nested launch we caused ourselves is the honest version, and
        it leaves every notice that IS printed also recorded.
        """
        monkeypatch.setenv(
            "CLAUDE_CONFIG_DIR", str(manager.sessions_dir / "2-b_example.com")
        )
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            with pytest.raises(_ExecCalled):
                manager.run("2", [])

        assert "overriding it for this launch" not in capsys.readouterr().out
        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert "overriding it for this launch" not in logged

    def test_every_note_and_warn_on_the_launch_path_reaches_the_log(
        self, manager, capture_exec, monkeypatch, auth_status_tracks_seed,
        refresh_rotates, caplog,
    ):
        """FIVE OF NINE had no behavioural case at all.

        Swapping each `self._note` / `self._warn` for a print-only
        `warning(...)`, one at a time, left the whole file green at
        `session.py` 562, 582, 1113, 1347 and 1358 — including the
        non-quiescent notice the structural guard's own docstring cites as
        its motivating discovery, and the `Launching` line, whose text
        appears nowhere else in this file.

        Rather than nine injections, this asserts the invariant over the
        routed writers: on a launch everything printed THROUGH `_note`/`_warn`
        is also recorded, whichever line it is on. A bare `print` on the same
        path bypasses this collection entirely and is
        `test_no_launch_notice_is_print_only`'s subject, not this one's.
        """
        import re as _re

        printed: list[str] = []
        real_note, real_warn = manager._note, manager._warn
        monkeypatch.setattr(
            manager, "_note",
            # FORWARDED, not swallowed. A `**kw` that drops what it received
            # would accept a call shape the real method does not.
            lambda m, **kw: (printed.append(m), real_note(m, **kw))[1],
            raising=False)
        monkeypatch.setattr(
            manager, "_warn",
            lambda m: (printed.append(m), real_warn(m))[1], raising=False)

        with caplog.at_level(logging.INFO, logger="claude-swap"):
            with pytest.raises(_ExecCalled):
                manager.run("2", [])

        assert printed, "premise: the launch printed no notice at all"
        logged = "\n".join(r.getMessage() for r in caplog.records)
        strip = lambda t: _re.sub(r"\x1b\[[0-9;]*m", "", t)
        missing = [m for m in printed if strip(m) not in logged]
        assert missing == [], (
            "a launch notice reached the terminal and not the log, so the "
            f"exec's screen blank erases it and nothing records why: {missing}"
        )

    def test_the_launch_banner_is_not_dimmed_over_its_own_accent(
        self, manager, capture_exec, monkeypatch, auth_status_tracks_seed,
        refresh_rotates, capsys, caplog,
    ):
        """`_note` wraps in `dimmed` by design; this one line carries its own.

        `accent('Launching')` closes with its own reset, so a `dimmed` wrap
        around the whole string dims exactly that word and nothing after it --
        a visible change to a line the durable-warning fix only meant to
        RECORD. `dim=False` at the call site is what prevents it, and dropping
        it is invisible to every other case here: they read the LOG, which
        `_plain` strips either way.
        """
        import logging

        monkeypatch.setenv("FORCE_COLOR", "1")
        with caplog.at_level(logging.INFO, logger="claude-swap"):
            with pytest.raises(_ExecCalled):
                manager.run("2", [])

        # THE RECORD, TOO. `_plain` is the identity function when colour is
        # off, and every case that reads `caplog` runs colour-off -- so
        # dropping it from `_note`/`_warn` left the whole suite green while
        # the log the README points a user at filled with escape sequences.
        # This is the one case that turns colour ON.
        styled = [r.getMessage() for r in caplog.records if "\x1b[" in r.getMessage()]
        assert not styled, (
            f"the log record carries SGR escapes: {styled[:1]}"
        )

        line = next((l for l in capsys.readouterr().out.splitlines()
                     if "Launching" in l), None)
        assert line is not None, "premise: the launch banner was never printed"
        assert "\x1b[" in line, (
            "premise: colour is off on this line, so the wrap is invisible "
            "and this case proves nothing"
        )
        assert not line.startswith("\x1b[2m"), (
            f"the banner is wrapped in dimmed(), so its accent renders dim: "
            f"{line!r}"
        )

    def test_the_two_data_moves_outlive_the_terminal(
        self, manager, tmp_path, caplog
    ):
        """Both notices record where the user's own data WENT, and both are
        printed on the launch path the exec clears. Every failure beside them
        is already logged; only the successful moves were not."""
        from claude_swap.session import MCP_DISPLACED_STASH

        session_dir = tmp_path / "profile"
        session_dir.mkdir()
        src, dest = tmp_path / "shared", session_dir / "projects"
        dest.mkdir()
        (dest / "a.jsonl").write_text("{}")

        with caplog.at_level(logging.INFO, logger="claude-swap"):
            assert manager._stash_displaced_mcp(session_dir, {"x": {}})
            assert manager._prepare_history_share(src, dest, session_dir)

        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert MCP_DISPLACED_STASH in logged
        assert "projects" in logged

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

    def test_switch_refuses_live_target_whose_profile_is_ahead(
        self, seeded_switcher, monkeypatch
    ):
        """Once the live session has rotated past the backup, the backup is a
        consumed generation and activating it could only fail."""
        session_dir = session_dir_for(
            seeded_switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )
        session_dir.mkdir(parents=True)
        (session_dir / ".credentials.json").write_text(ROTATED_CREDS)
        (session_dir / ".claude.json").write_text(CONFIG)
        make_live(session_dir)
        monkeypatch.setattr(seeded_switcher, "_get_current_account", lambda: None)

        with pytest.raises(SwitchError, match="rotated past the stored backup"):
            seeded_switcher._perform_switch(ACCOUNT_NUM)

        data = seeded_switcher._get_sequence_data()
        assert data["activeAccountNumber"] == 1
        assert (
            seeded_switcher.read_account_credentials(ACCOUNT_NUM, ACCOUNT_EMAIL)
            == CREDS
        )

    def test_switch_adopts_exited_session_credential_first(
        self, seeded_switcher, monkeypatch
    ):
        """Nothing running against the profile: its newer generation becomes
        the backup, and that is what the switch activates."""
        session_dir = session_dir_for(
            seeded_switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )
        session_dir.mkdir(parents=True)
        (session_dir / ".credentials.json").write_text(ROTATED_CREDS)
        (session_dir / ".claude.json").write_text(CONFIG)
        monkeypatch.setattr(seeded_switcher, "_get_current_account", lambda: None)
        monkeypatch.setattr(seeded_switcher, "list_accounts", lambda **kw: None)

        seeded_switcher._perform_switch(ACCOUNT_NUM)

        assert (
            seeded_switcher.read_account_credentials(ACCOUNT_NUM, ACCOUNT_EMAIL)
            == ROTATED_CREDS
        )
        assert seeded_switcher._read_credentials() == ROTATED_CREDS
        # The profile is the source of that generation, not a stale seed.
        assert (session_dir / ".credentials.json").read_text() == ROTATED_CREDS

    def test_switch_refuses_exited_target_whose_ahead_profile_cannot_be_adopted(
        self, seeded_switcher, monkeypatch
    ):
        """Nothing running and the profile ahead, but its identity unreadable:
        adoption refuses to write (it cannot tell whose credential that is),
        and the switch must not then activate the consumed backup silently."""
        session_dir = session_dir_for(
            seeded_switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )
        session_dir.mkdir(parents=True)
        (session_dir / ".credentials.json").write_text(ROTATED_CREDS)
        (session_dir / ".claude.json").write_text("not json{{{")
        monkeypatch.setattr(seeded_switcher, "_get_current_account", lambda: None)

        with pytest.raises(SwitchError, match="could not be adopted"):
            seeded_switcher._perform_switch(ACCOUNT_NUM)

        data = seeded_switcher._get_sequence_data()
        assert data["activeAccountNumber"] == 1
        assert (
            seeded_switcher.read_account_credentials(ACCOUNT_NUM, ACCOUNT_EMAIL)
            == CREDS
        )
        assert (session_dir / ".credentials.json").read_text() == ROTATED_CREDS

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
        # Unexpired, so the read-only request is made; an expired copy under
        # a live session is not requested at all.
        seeded_switcher._write_account_credentials(
            ACCOUNT_NUM, ACCOUNT_EMAIL, ROTATED_CREDS
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
# history sharing (run.shareHistory / --share-history / --no-share-history)
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
    def test_not_shared_when_turned_off(self, history_setup):
        source, session_dir, mgr = history_setup
        mgr._sync_sharing(session_dir, share=True, share_history=False)

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

    def test_merge_keeps_an_unterminated_shared_line_whole(self, history_setup):
        source, session_dir, mgr = history_setup
        (source / "history.jsonl").write_text('{"p": "main"}')  # torn tail
        (session_dir / "history.jsonl").write_text('{"p": "profile"}\n')

        mgr._sync_sharing(session_dir, share=True, share_history=True)

        lines = (source / "history.jsonl").read_text().splitlines()
        assert lines == ['{"p": "main"}', '{"p": "profile"}']

    def test_merge_skipped_when_profile_history_is_byte_corrupt(self, history_setup):
        source, session_dir, mgr = history_setup
        (session_dir / "history.jsonl").write_bytes(b'{"p": "\xff\xfe"}\n')

        mgr._sync_sharing(session_dir, share=True, share_history=True)

        # Unreadable history is skipped, not fatal: the launch goes ahead and
        # the bytes stay put for a later attempt.
        assert (session_dir / "history.jsonl").read_bytes() == b'{"p": "\xff\xfe"}\n'
        assert (session_dir / "settings.json").is_symlink()
        manifest = json.loads((session_dir / SHARE_MANIFEST).read_text())
        assert "history.jsonl" not in manifest["items"]

    def test_merge_skipped_when_shared_history_is_byte_corrupt(self, history_setup):
        source, session_dir, mgr = history_setup
        (source / "history.jsonl").write_bytes(b'{"p": "\xff\xfe"}\n')
        (session_dir / "history.jsonl").write_text('{"p": "profile"}\n')

        mgr._sync_sharing(session_dir, share=True, share_history=True)

        assert (session_dir / "history.jsonl").read_text() == '{"p": "profile"}\n'
        assert (session_dir / "settings.json").is_symlink()
        manifest = json.loads((session_dir / SHARE_MANIFEST).read_text())
        assert "history.jsonl" not in manifest["items"]

    def test_merge_deferred_while_profile_live(self, history_setup, monkeypatch):
        source, session_dir, mgr = history_setup
        (session_dir / "projects").mkdir()
        (session_dir / "projects" / "x.jsonl").write_text("live\n")
        blocker = ClaudeSession(
            pid=1,
            session_id="live",
            cwd="/tmp",
            started_at=0,
            kind="interactive",
            entrypoint="cli",
        )
        monkeypatch.setattr(
            session_mod, "scan_live_sessions", lambda _dir: ([blocker], 0)
        )

        mgr._sync_sharing(session_dir, share=True, share_history=True)

        # Untouched: no merge, no link, not claimed in the manifest.
        assert not (session_dir / "projects").is_symlink()
        assert (session_dir / "projects" / "x.jsonl").read_text() == "live\n"
        manifest = json.loads((session_dir / SHARE_MANIFEST).read_text())
        assert "projects" not in manifest["items"]

    def test_deferral_names_the_blocking_session_and_the_command(
        self, history_setup, monkeypatch, capsys
    ):
        source, session_dir, mgr = history_setup
        proj = session_dir / "projects" / "-home-user-app"
        proj.mkdir(parents=True)
        (proj / "bbb.jsonl").write_text("profile-b\n")

        blocker = ClaudeSession(
            pid=63802,
            session_id="abc",
            cwd="/Users/matt/Documents/GitHub/soribashi",
            started_at=1785041704563,
            kind="interactive",
            entrypoint="cli",
        )
        monkeypatch.setattr(
            session_mod, "scan_live_sessions", lambda p: ([blocker], 0)
        )

        mgr._sync_sharing(session_dir, share=True, share_history=True)

        out = capsys.readouterr().out
        assert "63802" in out
        assert "soribashi" in out
        # Sharing is the default, so the command to repeat is the plain one.
        assert "then relaunch:  cswap run 2\n" in out
        # Pin the epoch-ms -> local-time conversion itself, not just the
        # surrounding text — a timezone bug (UTC mislabeled as local) or an
        # off-by-1000 that still lands in a plausible-looking date would slip
        # past a loose "2026-" substring check.
        expected_started = datetime.fromtimestamp(
            blocker.started_at / 1000, tz=UTC
        ).astimezone()
        assert f"{expected_started:%Y-%m-%d %H:%M}" in out

    def test_migration_rechecks_liveness_under_the_lock(
        self, history_setup, monkeypatch
    ):
        source, session_dir, mgr = history_setup
        proj = session_dir / "projects" / "-home-user-app"
        proj.mkdir(parents=True)
        (proj / "bbb.jsonl").write_text("profile-b\n")

        calls = {"n": 0}
        blocker = ClaudeSession(
            pid=2,
            session_id="a-session",
            cwd="/tmp",
            started_at=0,
            kind="interactive",
            entrypoint="cli",
        )

        def fake_scan(path):
            # Quiescent on the pre-lock check, busy once the lock is held.
            calls["n"] += 1
            return ([], 0) if calls["n"] == 1 else ([blocker], 0)

        monkeypatch.setattr(session_mod, "scan_live_sessions", fake_scan)
        mgr._sync_sharing(session_dir, share=True, share_history=True)

        assert calls["n"] >= 2, "liveness must be re-checked under the lock"
        assert (proj / "bbb.jsonl").exists(), "migration must not have run"
        assert not (session_dir / "projects").is_symlink()

    def test_migration_does_not_deadlock_against_the_bootstrap_lock(
        self, history_setup, monkeypatch
    ):
        """setup_session calls _sync_sharing while still holding
        switcher.lock_file (its re-validate-under-lock and post-bootstrap
        paths). Migration must not try to re-acquire that same lock: flock
        is per-fd, not per-process, so a second open of the same lock file
        from inside this same process would self-block rather than proceed,
        timing out into an error instead of migrating.
        """
        source, session_dir, mgr = history_setup
        proj = session_dir / "projects" / "-home-user-app"
        proj.mkdir(parents=True)
        (proj / "bbb.jsonl").write_text("profile-b\n")

        # Fail fast instead of hanging the suite if the regression reappears.
        monkeypatch.setattr(session_mod, "_MIGRATION_LOCK_TIMEOUT", 0.2)

        # Simulate the bootstrap lock already being held around this call,
        # exactly as setup_session holds it across its _sync_sharing calls.
        with FileLock(mgr.switcher.lock_file):
            mgr._sync_sharing(session_dir, share=True, share_history=True)

        merged = source / "projects" / "-home-user-app"
        assert merged.joinpath("aaa.jsonl").read_text() == "main-a\n"
        assert merged.joinpath("bbb.jsonl").read_text() == "profile-b\n"
        assert (session_dir / "projects").readlink() == source / "projects"

    def test_last_merge_counts_accumulate_across_history_items(self, history_setup):
        """_prepare_history_share runs once per HISTORY_ITEMS entry (projects,
        then history.jsonl), each merge assigning into the same field. A
        profile with real history in both must not have the projects/ counts
        (and its quarantine dir) clobbered by the history.jsonl merge that
        runs right after — the file-merge branch never quarantines and always
        reports (0, 0, None), so an overwrite would silently lose both.
        """
        source, session_dir, mgr = history_setup
        proj = session_dir / "projects" / "-home-user-app"
        proj.mkdir(parents=True)
        # A genuine collision in projects/: profile copy is newer, so it
        # wins — moved in, incumbent quarantined (moved=1, quarantined=1).
        (source / "projects" / "-home-user-app" / "aaa.jsonl").write_text(
            '{"timestamp": "2026-07-21T20:00:00.000Z"}\n'
        )
        (proj / "aaa.jsonl").write_text(
            '{"timestamp": "2026-07-23T20:00:00.000Z"}\n'
        )
        # Real profile-side history.jsonl too, merged by the separate
        # (0, 0, None)-always file-merge branch, right after projects/.
        (session_dir / "history.jsonl").write_text('{"p": "profile"}\n')

        mgr._sync_sharing(session_dir, share=True, share_history=True)

        moved, quarantined, run_dir = mgr._last_merge_counts
        assert (moved, quarantined) == (1, 1)
        assert run_dir is not None, "the quarantine directory must not be lost"

    def test_migration_lock_timeout_degrades_instead_of_raising(
        self, history_setup, monkeypatch, capsys
    ):
        """A LockError from the migration lock must degrade like every other
        failure in this function (a message, return False) rather than
        escape and abort the launch — a lock timeout means a peer is still
        merging into the shared tree, which is expected, not broken.
        """
        source, session_dir, mgr = history_setup
        proj = session_dir / "projects" / "-home-user-app"
        proj.mkdir(parents=True)
        (proj / "bbb.jsonl").write_text("profile-b\n")

        # Fail fast instead of waiting out the real 30s timeout.
        monkeypatch.setattr(session_mod, "_MIGRATION_LOCK_TIMEOUT", 0.2)

        # Hold the migration lock open, simulating a peer already merging
        # some profile — any profile, from any cswap store — into the same
        # shared tree. The lock is a file in that tree.
        with FileLock(source / session_mod.MIGRATION_LOCK):
            mgr._sync_sharing(session_dir, share=True, share_history=True)

        # Degraded, not raised: no migration happened, nothing linked.
        assert (proj / "bbb.jsonl").exists()
        assert not (session_dir / "projects").is_symlink()
        # And the reason given is the peer, not a session in this profile.
        out = capsys.readouterr().out
        assert "merging history into" in out
        assert "relaunch:  cswap run 2\n" in out

    def _lock_that_lets_a_peer_finish_first(self, monkeypatch, on_acquire):
        """Stand in for the migration lock a peer is already holding.

        The precondition that decided to merge (``dest.exists() and not
        dest.is_symlink()``) is evaluated *before* the wait, so whatever the
        peer did while we queued has to be re-checked once we hold the lock.
        """

        class PeerFinishedFirst:
            def __init__(self, path, timeout=None):
                pass

            def __enter__(self):
                on_acquire()
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(session_mod, "FileLock", PeerFinishedFirst)

    def test_peer_removed_dest_while_we_waited_on_the_lock(
        self, history_setup, monkeypatch
    ):
        """The peer migrated and removed the profile's copy. Merging a dest
        that is no longer there raises FileNotFoundError and reports "merging
        failed" for what was in fact a successful migration."""
        source, session_dir, mgr = history_setup
        proj = session_dir / "projects" / "-home-user-app"
        proj.mkdir(parents=True)
        (proj / "bbb.jsonl").write_text("profile-b\n")
        self._lock_that_lets_a_peer_finish_first(
            monkeypatch,
            lambda: session_mod.shutil.rmtree(session_dir / "projects"),
        )

        mgr._sync_sharing(session_dir, share=True, share_history=True)

        assert (session_dir / "projects").is_symlink()
        assert (
            source / "projects" / "-home-user-app" / "aaa.jsonl"
        ).read_text() == "main-a\n"

    def test_peer_linked_dest_while_we_waited_on_the_lock(
        self, history_setup, monkeypatch
    ):
        """The peer got all the way to the symlink. dest.is_dir() follows it,
        so the merge would walk ~/.claude/projects against itself: every
        shared transcript collides with itself, gets quarantined, and the
        shared directory is emptied out underneath the link."""
        source, session_dir, mgr = history_setup
        proj = session_dir / "projects" / "-home-user-app"
        proj.mkdir(parents=True)
        (proj / "bbb.jsonl").write_text("profile-b\n")

        def peer_migrated_and_linked():
            session_mod.shutil.rmtree(session_dir / "projects")
            (session_dir / "projects").symlink_to(source / "projects")

        self._lock_that_lets_a_peer_finish_first(
            monkeypatch, peer_migrated_and_linked
        )

        mgr._sync_sharing(session_dir, share=True, share_history=True)

        assert not (mgr.switcher.backup_dir / "history-conflicts").exists()
        assert (
            source / "projects" / "-home-user-app" / "aaa.jsonl"
        ).read_text() == "main-a\n"
        assert (session_dir / "projects").readlink() == source / "projects"

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

    def test_collision_keeps_newer_profile_copy_and_quarantines_target(
        self, history_setup
    ):
        source, session_dir, mgr = history_setup
        shared = source / "projects" / "-home-user-app" / "aaa.jsonl"
        shared.write_text('{"timestamp": "2026-07-21T20:00:00.000Z"}\n')
        proj = session_dir / "projects" / "-home-user-app"
        proj.mkdir(parents=True)
        (proj / "aaa.jsonl").write_text(
            '{"timestamp": "2026-07-23T20:00:00.000Z"}\n'
        )

        mgr._sync_sharing(session_dir, share=True, share_history=True)

        assert "2026-07-23" in shared.read_text()
        quarantined = list(
            (mgr.switcher.backup_dir / "history-conflicts").rglob("aaa.jsonl")
        )
        assert len(quarantined) == 1
        assert "2026-07-21" in quarantined[0].read_text()

    def test_collision_keeps_newer_target_and_quarantines_profile_copy(
        self, history_setup
    ):
        source, session_dir, mgr = history_setup
        shared = source / "projects" / "-home-user-app" / "aaa.jsonl"
        shared.write_text('{"timestamp": "2026-07-23T20:00:00.000Z"}\n')
        proj = session_dir / "projects" / "-home-user-app"
        proj.mkdir(parents=True)
        (proj / "aaa.jsonl").write_text(
            '{"timestamp": "2026-07-21T20:00:00.000Z"}\n'
        )

        mgr._sync_sharing(session_dir, share=True, share_history=True)

        assert "2026-07-23" in shared.read_text()
        quarantined = list(
            (mgr.switcher.backup_dir / "history-conflicts").rglob("aaa.jsonl")
        )
        assert len(quarantined) == 1
        assert "2026-07-21" in quarantined[0].read_text()

    def test_quarantine_never_overwrites_an_earlier_quarantine(
        self, history_setup, tmp_path
    ):
        # Two runs inside one timestamp granule land in the same run dir, so
        # the uniquify path is exercised directly rather than through a merge.
        source, session_dir, mgr = history_setup
        run_dir = mgr.switcher.backup_dir / "history-conflicts" / "same-granule"
        run_dir.mkdir(parents=True)
        rel = Path("-home-user-app") / "aaa.jsonl"

        first = tmp_path / "first.jsonl"
        first.write_text("first-loser\n")
        second = tmp_path / "second.jsonl"
        second.write_text("second-loser\n")

        landed_first = mgr._quarantine(first, rel, run_dir, "profile")
        landed_second = mgr._quarantine(second, rel, run_dir, "profile")

        assert landed_first != landed_second
        assert landed_first.read_text() == "first-loser\n"
        assert landed_second.read_text() == "second-loser\n"

    def test_merge_return_counts_when_profile_copy_wins(
        self, history_setup, tmp_path
    ):
        _, _, mgr = history_setup
        src = tmp_path / "shared" / "projects"
        dest = tmp_path / "profile" / "projects"
        (src / "-home-user-app").mkdir(parents=True)
        (dest / "-home-user-app").mkdir(parents=True)
        (src / "-home-user-app" / "aaa.jsonl").write_text(
            '{"timestamp": "2026-07-21T20:00:00.000Z"}\n'
        )
        (dest / "-home-user-app" / "aaa.jsonl").write_text(
            '{"timestamp": "2026-07-23T20:00:00.000Z"}\n'
        )

        moved, quarantined, run_dir = mgr._merge_history_into_source(src, dest)

        assert (moved, quarantined) == (1, 1)
        assert run_dir is not None
        assert "2026-07-23" in (src / "-home-user-app" / "aaa.jsonl").read_text()
        assert "2026-07-21" in (
            run_dir / "shared" / "-home-user-app" / "aaa.jsonl"
        ).read_text()

    def test_merge_return_counts_when_target_wins(self, history_setup, tmp_path):
        _, _, mgr = history_setup
        src = tmp_path / "shared" / "projects"
        dest = tmp_path / "profile" / "projects"
        (src / "-home-user-app").mkdir(parents=True)
        (dest / "-home-user-app").mkdir(parents=True)
        (src / "-home-user-app" / "aaa.jsonl").write_text(
            '{"timestamp": "2026-07-23T20:00:00.000Z"}\n'
        )
        (dest / "-home-user-app" / "aaa.jsonl").write_text(
            '{"timestamp": "2026-07-21T20:00:00.000Z"}\n'
        )

        moved, quarantined, run_dir = mgr._merge_history_into_source(src, dest)

        assert (moved, quarantined) == (0, 1)
        assert run_dir is not None
        assert "2026-07-23" in (src / "-home-user-app" / "aaa.jsonl").read_text()
        assert "2026-07-21" in (
            run_dir / "profile" / "-home-user-app" / "aaa.jsonl"
        ).read_text()

    def test_no_transcript_is_ever_deleted(self, history_setup):
        source, session_dir, mgr = history_setup
        shared = source / "projects" / "-home-user-app" / "aaa.jsonl"
        shared.write_text('{"timestamp": "2026-07-23T20:00:00.000Z"}\n')
        proj = session_dir / "projects" / "-home-user-app"
        proj.mkdir(parents=True)
        (proj / "aaa.jsonl").write_text(
            '{"timestamp": "2026-07-21T20:00:00.000Z"}\n'
        )

        mgr._sync_sharing(session_dir, share=True, share_history=True)

        # The merge must actually have happened (not silently skipped): the
        # profile's projects/ is a symlink into the shared tree, so its own
        # copy of aaa.jsonl no longer exists to double-count as "kept".
        assert (session_dir / "projects").is_symlink()
        bodies = {
            p.read_text()
            for p in (mgr.switcher.backup_dir / "history-conflicts").rglob(
                "aaa.jsonl"
            )
        } | {shared.read_text()}
        assert any("2026-07-21" in b for b in bodies)
        assert any("2026-07-23" in b for b in bodies)

    def test_quarantine_records_provenance_in_path_profile_wins(
        self, history_setup
    ):
        source, session_dir, mgr = history_setup
        # Profile copy is newer — it wins, shared copy is quarantined.
        shared = source / "projects" / "-home-user-app" / "aaa.jsonl"
        shared.write_text('{"timestamp": "2026-07-21T20:00:00.000Z"}\n')
        proj = session_dir / "projects" / "-home-user-app"
        proj.mkdir(parents=True)
        (proj / "aaa.jsonl").write_text(
            '{"timestamp": "2026-07-23T20:00:00.000Z"}\n'
        )

        mgr._sync_sharing(session_dir, share=True, share_history=True)

        # The shared-side loser should be under .../shared/, not .../profile/.
        quarantined = list(
            (mgr.switcher.backup_dir / "history-conflicts").rglob("aaa.jsonl")
        )
        assert len(quarantined) == 1
        quarantine_path = quarantined[0]
        assert "/shared/" in str(quarantine_path)
        assert "/profile/" not in str(quarantine_path)
        assert "2026-07-21" in quarantine_path.read_text()

    def test_quarantine_records_provenance_in_path_target_wins(
        self, history_setup
    ):
        source, session_dir, mgr = history_setup
        # Target (shared) copy is newer — it wins, profile copy is quarantined.
        shared = source / "projects" / "-home-user-app" / "aaa.jsonl"
        shared.write_text('{"timestamp": "2026-07-23T20:00:00.000Z"}\n')
        proj = session_dir / "projects" / "-home-user-app"
        proj.mkdir(parents=True)
        (proj / "aaa.jsonl").write_text(
            '{"timestamp": "2026-07-21T20:00:00.000Z"}\n'
        )

        mgr._sync_sharing(session_dir, share=True, share_history=True)

        # The profile-side loser should be under .../profile/, not .../shared/.
        quarantined = list(
            (mgr.switcher.backup_dir / "history-conflicts").rglob("aaa.jsonl")
        )
        assert len(quarantined) == 1
        quarantine_path = quarantined[0]
        assert "/profile/" in str(quarantine_path)
        assert "/shared/" not in str(quarantine_path)
        assert "2026-07-21" in quarantine_path.read_text()


OLDER = '{"timestamp": "2026-07-21T20:00:00.000Z"}\n'
NEWER = '{"timestamp": "2026-07-23T20:00:00.000Z"}\n'


def _live_record(config_dir: Path, session_id: str) -> None:
    """The record a running claude leaves in its config dir's ``sessions/``.

    Stamped with this process's pid, so the real liveness scan reads it as
    live — no stub in between.
    """
    sessions = config_dir / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{os.getpid()}.json").write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "sessionId": session_id,
                "cwd": "/home/user/app",
                "startedAt": 0,
                "kind": "interactive",
                "entrypoint": "cli",
            }
        )
    )


def _parked(mgr: SessionManager, name: str) -> list[Path]:
    """Quarantined copies of ``name``, as paths relative to their run dir."""
    root = mgr.switcher.backup_dir / "history-conflicts"
    return [
        p.relative_to(run_dir)
        for run_dir in sorted(root.iterdir())
        for p in sorted(run_dir.rglob(name))
    ] if root.exists() else []


def _parked_bodies(mgr: SessionManager, name: str) -> list[str]:
    root = mgr.switcher.backup_dir / "history-conflicts"
    return sorted(p.read_text() for p in root.rglob(name)) if root.exists() else []


@pytest.mark.skipif(sys.platform == "win32", reason="history sharing is POSIX-only")
class TestHistoryMergeSharesTheDestination:
    """``~/.claude`` has other writers than the profile being merged: other
    profiles merging into it, and live sessions already writing it."""

    # -- one lock for the destination -------------------------------------

    def _race(self, monkeypatch, first, first_dir, second, second_dir) -> None:
        """Run two merges in the order that loses a file when they do not
        share a lock: the first is held once it has seen the shared path
        missing and before it moves onto it, and the second gets its chance
        in that gap."""
        first_is_moving = threading.Event()
        second_had_its_chance = threading.Event()
        real_move = shutil.move

        def move(src_path, dst_path):
            if str(first_dir) in str(src_path) and not first_is_moving.is_set():
                first_is_moving.set()
                assert second_had_its_chance.wait(timeout=10)
            return real_move(src_path, dst_path)

        class WatchedLock(FileLock):
            def acquire(self, timeout=None):
                if threading.current_thread().name == "second":
                    if super().acquire(timeout=0.3):
                        return True
                    # Still shut out after a wait: its chance was spent
                    # queueing behind the first, which is the point.
                    second_had_its_chance.set()
                return super().acquire(timeout)

        monkeypatch.setattr(session_mod.shutil, "move", move)
        monkeypatch.setattr(session_mod, "FileLock", WatchedLock)

        def launch(mgr, profile):
            try:
                mgr._sync_sharing(profile, share=False, share_history=True)
            finally:
                if threading.current_thread().name == "second":
                    second_had_its_chance.set()

        a = threading.Thread(target=launch, args=(first, first_dir), name="first")
        b = threading.Thread(target=launch, args=(second, second_dir), name="second")
        a.start()
        assert first_is_moving.wait(timeout=10)
        b.start()
        a.join(timeout=30)
        b.join(timeout=30)
        assert not a.is_alive() and not b.is_alive()

    def test_two_profiles_merging_one_path_do_not_overwrite_each_other(
        self, history_setup, seeded_switcher, monkeypatch
    ):
        """With a lock per profile, two DIFFERENT profiles each find the same
        shared path missing, and the second move lands on top of the first:
        a file is gone without a collision ever having been seen. One lock
        on the destination makes the second merge wait, and meet one."""
        source, first_dir, first = history_setup
        second_dir = first.sessions_dir / "3-account3_example.com"
        second = SessionManager(seeded_switcher)
        rel = Path("-home-user-app") / "memory" / "MEMORY.md"
        for profile, body in ((first_dir, "first\n"), (second_dir, "second\n")):
            (profile / "projects" / rel).parent.mkdir(parents=True)
            (profile / "projects" / rel).write_text(body)

        self._race(monkeypatch, first, first_dir, second, second_dir)

        shared = source / "projects" / rel
        assert sorted([shared.read_text(), *_parked_bodies(first, "MEMORY.md")]) == [
            "first\n",
            "second\n",
        ], "one profile's file was overwritten by the other's"
        assert (first_dir / "projects").readlink() == source / "projects"
        assert (second_dir / "projects").readlink() == source / "projects"

    def test_two_stores_merging_into_one_home_share_the_lock(
        self, history_setup, monkeypatch
    ):
        """On Linux the backup root follows ``XDG_DATA_HOME``, so two cswap
        stores can serve one ``~/.claude``. A lock inside either store is
        invisible to the other; only one in the destination holds both."""
        source, _, _ = history_setup
        monkeypatch.setattr(
            Platform, "detect", classmethod(lambda cls: Platform.LINUX)
        )
        rel = Path("-home-user-app") / "memory" / "MEMORY.md"
        stores = []
        for n in (1, 2):
            monkeypatch.setenv("XDG_DATA_HOME", str(source.parent / f"data-{n}"))
            backup = get_backup_root()
            profile = backup / "sessions" / f"{n + 1}-account{n + 1}_example.com"
            (profile / "projects" / rel).parent.mkdir(parents=True)
            (profile / "projects" / rel).write_text(f"store {n}\n")
            mgr = SessionManager(
                SimpleNamespace(
                    backup_dir=backup,
                    platform=Platform.LINUX,
                    _logger=logging.getLogger("claude-swap-test"),
                    lock_file=backup / ".lock",
                )
            )
            stores.append((mgr, profile))
        (first, first_dir), (second, second_dir) = stores
        assert first.switcher.backup_dir != second.switcher.backup_dir

        self._race(monkeypatch, first, first_dir, second, second_dir)

        shared = source / "projects" / rel
        survivors = [
            shared.read_text(),
            *_parked_bodies(first, "MEMORY.md"),
            *_parked_bodies(second, "MEMORY.md"),
        ]
        assert sorted(survivors) == ["store 1\n", "store 2\n"], (
            "one store's file was overwritten by the other's"
        )

    def test_a_lock_that_cannot_be_opened_skips_the_merge_not_the_launch(
        self, history_setup, monkeypatch, capsys
    ):
        """The lock is a file in ``~/.claude``, which cswap does not own. A
        read-only one must cost this launch its history sharing, not the
        launch."""
        _, session_dir, mgr = history_setup
        proj = session_dir / "projects" / "-home-user-app"
        proj.mkdir(parents=True)
        (proj / "bbb.jsonl").write_text("profile-b\n")

        class Unopenable(FileLock):
            def acquire(self, timeout=None):
                raise PermissionError("read-only file system")

        monkeypatch.setattr(session_mod, "FileLock", Unopenable)
        mgr._sync_sharing(session_dir, share=True, share_history=True)

        assert (proj / "bbb.jsonl").read_text() == "profile-b\n"
        assert not (session_dir / "projects").is_symlink()
        assert "could not be locked" in capsys.readouterr().out

    def test_the_link_is_installed_before_the_lock_is_released(
        self, history_setup, monkeypatch
    ):
        """Released in between, a peer finds the profile with its history
        moved out and nothing linked, and takes it for one with nothing to
        share."""
        _, session_dir, mgr = history_setup
        proj = session_dir / "projects" / "-home-user-app"
        proj.mkdir(parents=True)
        (proj / "bbb.jsonl").write_text("profile-b\n")
        linked_at_release: list[bool] = []

        class WatchedLock(FileLock):
            def release(self):
                linked_at_release.append((session_dir / "projects").is_symlink())
                super().release()

        monkeypatch.setattr(session_mod, "FileLock", WatchedLock)
        mgr._sync_sharing(session_dir, share=True, share_history=True)

        assert linked_at_release == [True]

    def test_a_profile_with_nothing_to_merge_does_not_wait_on_the_lock(
        self, history_setup, monkeypatch
    ):
        """Linking moves nothing, so a new profile must not queue behind
        another profile's merge (and be told it "keeps its own" history)."""
        source, session_dir, mgr = history_setup
        monkeypatch.setattr(session_mod, "_MIGRATION_LOCK_TIMEOUT", 0.2)

        with FileLock(source / session_mod.MIGRATION_LOCK):
            mgr._sync_sharing(session_dir, share=True, share_history=True)

        assert (session_dir / "projects").readlink() == source / "projects"
        assert (session_dir / "history.jsonl").readlink() == source / "history.jsonl"

    # -- a shared copy that is in use stays where it is ---------------------

    def _collide(self, history_setup, rel: str, shared_body: str, profile_body: str):
        source, session_dir, mgr = history_setup
        shared = source / "projects" / rel
        shared.parent.mkdir(parents=True, exist_ok=True)
        shared.write_text(shared_body)
        mine = session_dir / "projects" / rel
        mine.parent.mkdir(parents=True, exist_ok=True)
        mine.write_text(profile_body)
        return source, session_dir, mgr, shared

    def test_a_transcript_a_live_default_login_session_is_writing_stays_in_place(
        self, history_setup
    ):
        """The profile's copy is the later one and would normally replace
        the shared one — but a running claude appends to that path, so
        swapping the file under it splits the conversation in two."""
        source, session_dir, mgr, shared = self._collide(
            history_setup, "-home-user-app/aaa.jsonl", OLDER, NEWER
        )
        _live_record(source, "aaa")

        mgr._sync_sharing(session_dir, share=True, share_history=True)

        assert shared.read_text() == OLDER
        assert _parked(mgr, "aaa.jsonl") == [
            Path("profile") / "-home-user-app" / "aaa.jsonl"
        ]
        assert _parked_bodies(mgr, "aaa.jsonl") == [NEWER]
        assert (session_dir / "projects").readlink() == source / "projects"

    def test_a_session_in_a_linked_peer_profile_protects_the_shared_copy_too(
        self, history_setup
    ):
        """A claude in another profile writes the shared tree through its
        link, and its record is in that profile, not in ``~/.claude``."""
        _, session_dir, mgr, shared = self._collide(
            history_setup, "-home-user-app/aaa.jsonl", OLDER, NEWER
        )
        _live_record(mgr.sessions_dir / "3-account3_example.com", "aaa")

        mgr._sync_sharing(session_dir, share=True, share_history=True)

        assert shared.read_text() == OLDER
        assert _parked_bodies(mgr, "aaa.jsonl") == [NEWER]

    def test_a_live_sessions_subagent_transcript_stays_in_place(self, history_setup):
        rel = "-home-user-app/aaa/subagents/agent-1.jsonl"
        source, session_dir, mgr, shared = self._collide(
            history_setup, rel, OLDER, NEWER
        )
        _live_record(source, "aaa")

        mgr._sync_sharing(session_dir, share=True, share_history=True)

        assert shared.read_text() == OLDER
        assert _parked(mgr, "agent-1.jsonl") == [Path("profile") / rel]

    def test_an_unreadable_session_record_keeps_every_shared_copy(
        self, history_setup
    ):
        """Not knowing who is running is not knowing nobody is."""
        source, session_dir, mgr, shared = self._collide(
            history_setup, "-home-user-app/aaa.jsonl", OLDER, NEWER
        )
        (source / "sessions").mkdir()
        (source / "sessions" / "4242.json").write_text("{not json")

        mgr._sync_sharing(session_dir, share=True, share_history=True)

        assert shared.read_text() == OLDER
        assert _parked_bodies(mgr, "aaa.jsonl") == [NEWER]

    def test_a_session_that_resumes_mid_merge_keeps_its_transcript(
        self, history_setup, monkeypatch
    ):
        """The live sessions are read before every swap, not once per merge:
        a claude that ``/resume``s another conversation while the merge is
        busy with other files is writing that one by the time its turn
        comes."""
        _, session_dir, mgr, first = self._collide(
            history_setup, "-home-user-app/zzz.jsonl", OLDER, NEWER
        )
        source, _, _, second = self._collide(
            history_setup, "-home-user-app/aaa.jsonl", OLDER, NEWER
        )
        _live_record(source, "some-other-session")
        real_quarantine = mgr._quarantine

        def resume_after_the_first_swap(path, rel, run_dir, side):
            landed = real_quarantine(path, rel, run_dir, side)
            if rel.name == "zzz.jsonl":
                # The same claude, now on ``aaa``: its record follows.
                _live_record(source, "aaa")
            return landed

        monkeypatch.setattr(mgr, "_quarantine", resume_after_the_first_swap)
        mgr._sync_sharing(session_dir, share=True, share_history=True)

        # The walk is reverse-sorted: zzz was idle and is swapped, aaa went
        # live in between and stays where its session will append to it.
        assert first.read_text() == NEWER
        assert second.read_text() == OLDER
        assert _parked(mgr, "aaa.jsonl") == [
            Path("profile") / "-home-user-app" / "aaa.jsonl"
        ]

    def test_a_live_session_on_another_transcript_does_not_block_the_swap(
        self, history_setup
    ):
        source, session_dir, mgr, shared = self._collide(
            history_setup, "-home-user-app/aaa.jsonl", OLDER, NEWER
        )
        _live_record(source, "some-other-session")

        mgr._sync_sharing(session_dir, share=True, share_history=True)

        assert shared.read_text() == NEWER
        assert _parked(mgr, "aaa.jsonl") == [
            Path("shared") / "-home-user-app" / "aaa.jsonl"
        ]

    def test_a_diverged_memory_file_keeps_the_shared_copy(self, history_setup):
        """Nothing ties ``MEMORY.md`` to one session, so it can never be
        shown to be idle — and a claude rewriting it in place would write
        over a copy swapped in beneath it. Longer does not win here."""
        rel = "-home-user-app/memory/MEMORY.md"
        _, session_dir, mgr, shared = self._collide(
            history_setup, rel, "shared index\n", "profile index\nwith\nmore\nlines\n"
        )
        (session_dir / "projects" / "-home-user-app" / "memory" / "note.md").write_text(
            "only in the profile\n"
        )

        mgr._sync_sharing(session_dir, share=True, share_history=True)

        assert shared.read_text() == "shared index\n"
        assert _parked(mgr, "MEMORY.md") == [Path("profile") / rel]
        assert _parked_bodies(mgr, "MEMORY.md") == ["profile index\nwith\nmore\nlines\n"]
        # A name only the profile had still moves in.
        assert (shared.parent / "note.md").read_text() == "only in the profile\n"

    # -- a copy that adds nothing is the only one dropped -------------------

    def test_an_identical_copy_is_dropped_not_parked(self, history_setup):
        source, session_dir, mgr, shared = self._collide(
            history_setup, "-home-user-app/aaa.jsonl", "main-a\n", "main-a\n"
        )

        mgr._sync_sharing(session_dir, share=True, share_history=True)

        assert shared.read_text() == "main-a\n"
        assert mgr._last_merge_counts == (0, 0, None)
        assert not (mgr.switcher.backup_dir / "history-conflicts").exists()
        assert (session_dir / "projects").readlink() == source / "projects"

    def test_a_copy_the_shared_one_continues_is_dropped_not_parked(
        self, history_setup
    ):
        _, session_dir, mgr, shared = self._collide(
            history_setup, "-home-user-app/aaa.jsonl", "a\nb\nc\n", "a\nb\n"
        )

        mgr._sync_sharing(session_dir, share=True, share_history=True)

        assert shared.read_text() == "a\nb\nc\n"
        assert not (mgr.switcher.backup_dir / "history-conflicts").exists()

    def test_a_copy_that_continues_the_shared_one_is_never_dropped(
        self, history_setup
    ):
        """Issue #241 as reported: the shared copy was an old snapshot, the
        profile's twelve days longer, and the longer one was deleted."""
        _, session_dir, mgr, shared = self._collide(
            history_setup, "-home-user-app/aaa.jsonl", "a\nb\n", "a\nb\nc\nd\n"
        )

        mgr._sync_sharing(session_dir, share=True, share_history=True)

        assert shared.read_text() == "a\nb\nc\nd\n"
        assert _parked_bodies(mgr, "aaa.jsonl") == ["a\nb\n"]

    # -- the merge never runs against the shared tree itself ----------------

    def test_merge_refuses_a_dest_that_links_to_the_shared_tree(self, history_setup):
        """``is_dir()`` and ``rglob`` follow the link, so every shared file
        meets itself — and an identical copy is the one that gets dropped."""
        source, session_dir, mgr = history_setup
        (session_dir / "projects").symlink_to(source / "projects")

        with pytest.raises(OSError):
            mgr._merge_history_into_source(
                source / "projects", session_dir / "projects"
            )

        assert (
            source / "projects" / "-home-user-app" / "aaa.jsonl"
        ).read_text() == "main-a\n"
        assert not (mgr.switcher.backup_dir / "history-conflicts").exists()

    def test_merge_refuses_a_dest_that_reaches_the_shared_tree_another_way(
        self, history_setup
    ):
        """Not a link itself, but the same directory through a linked
        parent — ``is_symlink()`` alone does not see it."""
        source, session_dir, mgr = history_setup
        (session_dir / "alias").symlink_to(source)
        dest = session_dir / "alias" / "projects"
        assert not dest.is_symlink()

        with pytest.raises(OSError):
            mgr._merge_history_into_source(source / "projects", dest)

        assert (
            source / "projects" / "-home-user-app" / "aaa.jsonl"
        ).read_text() == "main-a\n"

    def test_a_merge_cut_short_between_park_and_move_finishes_next_launch(
        self, history_setup, monkeypatch, capsys
    ):
        source, session_dir, mgr, shared = self._collide(
            history_setup, "-home-user-app/aaa.jsonl", OLDER, NEWER
        )
        mine = session_dir / "projects" / "-home-user-app" / "aaa.jsonl"
        real_move = shutil.move

        def fail_the_move_in(src_path, dst_path):
            if Path(dst_path) == shared:
                raise OSError("no space left on device")
            return real_move(src_path, dst_path)

        monkeypatch.setattr(session_mod.shutil, "move", fail_the_move_in)
        mgr._sync_sharing(session_dir, share=True, share_history=True)

        # Incumbent parked, winner still in the profile, nothing linked.
        assert not shared.exists()
        assert mine.read_text() == NEWER
        assert not (session_dir / "projects").is_symlink()
        # The shared path is empty until the next launch, so this is the one
        # failure where the user most needs to be told where the copy went:
        # it counts as parked the moment it is parked, not once the pair is
        # complete.
        out = capsys.readouterr().out
        _, quarantined, run_dir = mgr._last_merge_counts
        assert quarantined == 1
        assert f"Kept 1 conflicting history file aside in {run_dir}" in out
        assert (
            run_dir / "shared" / "-home-user-app" / "aaa.jsonl"
        ).read_text() == OLDER

        monkeypatch.setattr(session_mod.shutil, "move", real_move)
        mgr._sync_sharing(session_dir, share=True, share_history=True)

        assert shared.read_text() == NEWER
        assert _parked_bodies(mgr, "aaa.jsonl") == [OLDER]
        assert (session_dir / "projects").readlink() == source / "projects"

    # -- what the user is told ----------------------------------------------

    def test_conflicts_are_reported_once_with_their_count_and_directory(
        self, history_setup, capsys
    ):
        _, session_dir, mgr, _ = self._collide(
            history_setup, "-home-user-app/memory/MEMORY.md", "shared\n", "profile\n"
        )
        (session_dir / "projects" / "-home-user-app" / "aaa.jsonl").write_text(
            "diverged\n"
        )
        (session_dir / "history.jsonl").write_text('{"p": "profile"}\n')

        mgr._sync_sharing(session_dir, share=True, share_history=True)

        out = capsys.readouterr().out
        _, quarantined, run_dir = mgr._last_merge_counts
        assert quarantined == 2
        assert out.count("conflicting history file") == 1
        assert f"Kept 2 conflicting history files aside in {run_dir}" in out
        # Named for the profile: several migrate on the same day.
        assert run_dir.name.endswith(session_dir.name)
        assert sorted(p.name for p in run_dir.rglob("*") if p.is_file()) == [
            "MEMORY.md",
            "aaa.jsonl",
        ]

    def test_a_merge_that_fails_later_still_reports_what_it_parked(
        self, history_setup, monkeypatch, capsys
    ):
        """The counts used to live only in the merge's return value, so a
        merge that raised halfway told the user "Not sharing projects" and
        nothing about the copies it had already moved out of the profile."""
        _, session_dir, mgr, _ = self._collide(
            history_setup, "-home-user-app/zzz.md", "shared\n", "profile z\n"
        )
        self._collide(history_setup, "-home-user-app/aaa.md", "shared\n", "profile a\n")
        real_quarantine = mgr._quarantine

        def fail_on_the_second_file(path, rel, run_dir, side):
            if rel.name == "aaa.md":
                raise PermissionError("cannot move the second file")
            return real_quarantine(path, rel, run_dir, side)

        monkeypatch.setattr(mgr, "_quarantine", fail_on_the_second_file)
        mgr._sync_sharing(session_dir, share=True, share_history=True)

        out = capsys.readouterr().out
        assert "merging the profile's existing history failed" in out
        _, quarantined, run_dir = mgr._last_merge_counts
        assert quarantined == 1
        assert f"Kept 1 conflicting history file aside in {run_dir}" in out
        assert _parked_bodies(mgr, "zzz.md") == ["profile z\n"]
        # The file that could not be parked is still the profile's.
        mine = session_dir / "projects" / "-home-user-app"
        assert not (mine / "zzz.md").exists()
        assert (mine / "aaa.md").read_text() == "profile a\n"

    def test_a_clean_merge_reports_no_conflicts(self, history_setup, capsys):
        _, session_dir, mgr = history_setup
        proj = session_dir / "projects" / "-home-user-app"
        proj.mkdir(parents=True)
        (proj / "bbb.jsonl").write_text("profile-b\n")

        mgr._sync_sharing(session_dir, share=True, share_history=True)

        assert "conflicting" not in capsys.readouterr().out

    def test_a_deferral_is_explained_once_per_launch(
        self, history_setup, monkeypatch, capsys
    ):
        """Both history items defer for the same reason; a second copy of
        the block only buries the first."""
        _, session_dir, mgr = history_setup
        (session_dir / "projects").mkdir()
        (session_dir / "history.jsonl").write_text('{"p": "profile"}\n')
        blocker = ClaudeSession(
            pid=63802,
            session_id="live",
            cwd="/tmp",
            started_at=0,
            kind="interactive",
            entrypoint="cli",
        )
        monkeypatch.setattr(
            session_mod, "scan_live_sessions", lambda _dir: ([blocker], 0)
        )

        mgr._sync_sharing(session_dir, share=True, share_history=True)

        out = capsys.readouterr().out
        assert out.count("keeps its own") == 1
        assert out.count("63802") == 1
        assert not (session_dir / "projects").is_symlink()
        assert not (session_dir / "history.jsonl").is_symlink()

    def test_an_unreadable_record_is_not_reported_as_a_peer_migration(
        self, history_setup, monkeypatch, capsys
    ):
        _, session_dir, mgr = history_setup
        (session_dir / "projects").mkdir()
        (session_dir / "projects" / "x.jsonl").write_text("mine\n")
        monkeypatch.setattr(session_mod, "scan_live_sessions", lambda _dir: ([], 1))

        mgr._sync_sharing(session_dir, share=True, share_history=True)

        out = capsys.readouterr().out
        assert "could not be read" in out
        assert "merging history" not in out
        assert (session_dir / "projects" / "x.jsonl").read_text() == "mine\n"


class TestShareHistoryWindows:
    """Windows without a symlink privilege: transcripts (``projects/``) are
    shared through a directory link, the prompt-history file stays the
    account's own, and the launch says so once. Runs on any host: a symlink
    stands in for the junction off Windows."""

    @pytest.fixture
    def win_history(self, history_setup):
        source, session_dir, mgr = history_setup
        mgr.switcher.platform = Platform.WINDOWS
        return source, session_dir, mgr

    def test_projects_are_shared_and_the_history_file_is_not(
        self, win_history, capsys
    ):
        source, session_dir, mgr = win_history
        mgr._sync_sharing(session_dir, share=True, share_history=True)

        assert is_link(session_dir / "projects")
        assert link_points_at(session_dir / "projects", source / "projects")
        assert (
            session_dir / "projects" / "-home-user-app" / "aaa.jsonl"
        ).read_text() == "main-a\n"
        assert not (session_dir / "history.jsonl").exists()
        manifest = json.loads((session_dir / SHARE_MANIFEST).read_text())
        assert "projects" in manifest["items"]
        assert "history.jsonl" not in manifest["items"]
        out = capsys.readouterr().out
        assert "history.jsonl stays per-account" in out
        assert "Developer Mode" in out

    def test_said_once_per_launch(self, win_history, capsys):
        source, session_dir, mgr = win_history
        mgr._sync_sharing(session_dir, share=True, share_history=True)
        assert capsys.readouterr().out.count("stays per-account") == 1

        mgr._sync_sharing(session_dir, share=True, share_history=True)
        assert capsys.readouterr().out.count("stays per-account") == 1

    def test_dimmed_for_the_default_a_warning_when_asked_for(
        self, win_history, monkeypatch
    ):
        source, session_dir, mgr = win_history
        warned: list[str] = []
        monkeypatch.setattr(
            session_mod, "warning", lambda msg, **_k: warned.append(msg)
        )

        mgr._resolve_share_history(None)  # the built-in default
        mgr._sync_sharing(session_dir, share=True, share_history=True)
        assert warned == []

        mgr._resolve_share_history(True)  # the flag
        mgr._sync_sharing(session_dir, share=True, share_history=True)
        assert any("history.jsonl stays per-account" in m for m in warned)

    def test_a_warning_when_the_setting_asks_for_it(self, win_history, monkeypatch):
        source, session_dir, mgr = win_history
        warned: list[str] = []
        monkeypatch.setattr(
            session_mod, "warning", lambda msg, **_k: warned.append(msg)
        )
        _turn_sharing(mgr, "true")

        mgr._resolve_share_history(None)
        mgr._sync_sharing(session_dir, share=True, share_history=True)

        assert any("history.jsonl stays per-account" in m for m in warned)

    @pytest.fixture
    def launch(self, win_history, capture_exec, auth_status_tracks_seed, refresh_rotates):
        """``cswap run 2`` up to the exec, through bootstrap and the sync."""
        source, session_dir, mgr = win_history
        # The seed went in under the macOS backend the fixtures pin; the
        # bootstrap reads through the Windows one, so persist it there too.
        mgr.switcher._write_account_credentials(ACCOUNT_NUM, ACCOUNT_EMAIL, CREDS)
        mgr.switcher._write_account_config(ACCOUNT_NUM, ACCOUNT_EMAIL, CONFIG)

        def run(**kwargs) -> None:
            with pytest.raises(_ExecCalled):
                mgr.run("2", [], **kwargs)

        return run

    def test_a_launch_that_asked_for_nothing_gets_the_dimmed_note(
        self, win_history, launch, monkeypatch, capsys
    ):
        """Through ``run`` and ``setup_session`` both, where the request is
        resolved twice: the default must still read as a default."""
        warned: list[str] = []
        monkeypatch.setattr(
            session_mod, "warning", lambda msg, **_k: warned.append(msg)
        )

        launch()

        assert "history.jsonl stays per-account" in capsys.readouterr().out
        assert not any("stays per-account" in m for m in warned)

    def test_a_launch_that_asked_with_the_flag_gets_a_warning(
        self, win_history, launch, monkeypatch
    ):
        warned: list[str] = []
        monkeypatch.setattr(
            session_mod, "warning", lambda msg, **_k: warned.append(msg)
        )

        launch(share_history=True)

        assert any("history.jsonl stays per-account" in m for m in warned)

    def test_the_accounts_own_history_file_is_kept_not_merged(self, win_history):
        """The merge moves a profile's history into ~/.claude for the link
        that follows. With no link possible for history.jsonl, merging it
        would make the account's prompt history vanish from its own view
        (#159), so it is left exactly where it is."""
        source, session_dir, mgr = win_history
        (session_dir / "history.jsonl").write_text('{"p": "profile"}\n')

        mgr._sync_sharing(session_dir, share=True, share_history=True)

        assert (session_dir / "history.jsonl").read_text() == '{"p": "profile"}\n'
        assert (source / "history.jsonl").read_text() == '{"p": "main"}\n'
        assert is_link(session_dir / "projects")

    def test_own_transcripts_are_merged_then_linked(self, win_history):
        source, session_dir, mgr = win_history
        own = session_dir / "projects" / "-home-user-app"
        own.mkdir(parents=True)
        (own / "bbb.jsonl").write_text("profile-b\n")

        mgr._sync_sharing(session_dir, share=True, share_history=True)

        assert is_link(session_dir / "projects")
        merged = source / "projects" / "-home-user-app"
        assert (merged / "bbb.jsonl").read_text() == "profile-b\n"
        assert (merged / "aaa.jsonl").read_text() == "main-a\n"

    def test_a_history_link_made_with_the_privilege_keeps_working(
        self, win_history, tmp_path, capsys
    ):
        """Only making a link needs the privilege. A profile linked in an
        elevated terminal, or before Developer Mode was turned off, stays
        shared: not replaced with a copy, and nothing to report."""
        source, session_dir, mgr = win_history
        if not _host_can_symlink(tmp_path):
            pytest.skip("this host cannot create symlinks")
        (session_dir / "history.jsonl").symlink_to(source / "history.jsonl")

        mgr._sync_sharing(session_dir, share=True, share_history=True)

        assert (session_dir / "history.jsonl").is_symlink()
        manifest = json.loads((session_dir / SHARE_MANIFEST).read_text())
        assert "history.jsonl" in manifest["items"]
        assert "stays per-account" not in capsys.readouterr().out

    def test_toggle_off_removes_the_link_and_keeps_the_data(self, win_history):
        source, session_dir, mgr = win_history
        mgr._sync_sharing(session_dir, share=True, share_history=True)
        mgr._sync_sharing(session_dir, share=True, share_history=False)

        assert not (session_dir / "projects").exists()
        assert not is_link(session_dir / "projects")
        assert (
            source / "projects" / "-home-user-app" / "aaa.jsonl"
        ).read_text() == "main-a\n"

    def test_a_stale_manifest_never_deletes_real_history(self, win_history):
        """Lock-free launches race: a manifest may claim ``projects`` while
        the profile holds a real directory there. Only a link is removed."""
        source, session_dir, mgr = win_history
        own = session_dir / "projects" / "-home-user-app"
        own.mkdir(parents=True)
        (own / "bbb.jsonl").write_text("profile-b\n")
        (session_dir / SHARE_MANIFEST).write_text(
            json.dumps({"items": ["projects"], "mode": "copy"})
        )

        mgr._sync_sharing(session_dir, share=True, share_history=False)

        assert (own / "bbb.jsonl").read_text() == "profile-b\n"

    def test_no_directory_link_possible_keeps_transcripts_apart_and_says_so(
        self, win_history, monkeypatch, capsys
    ):
        """A profile on a volume without reparse points: nothing is linked,
        so nothing is merged, and the launch says why."""
        source, session_dir, mgr = win_history
        monkeypatch.setattr(session_mod, "can_junction", lambda *_a: False)
        own = session_dir / "projects" / "-home-user-app"
        own.mkdir(parents=True)
        (own / "bbb.jsonl").write_text("profile-b\n")

        mgr._sync_sharing(session_dir, share=True, share_history=True)

        assert (own / "bbb.jsonl").read_text() == "profile-b\n"
        assert not (source / "projects" / "-home-user-app" / "bbb.jsonl").exists()
        out = capsys.readouterr().out
        assert "projects stays per-account" in out
        assert "junction" in out

    def test_run_accepts_the_flag(self, win_history, monkeypatch):
        source, session_dir, mgr = win_history
        monkeypatch.setattr(
            session_mod.shutil, "which", lambda _name: "/usr/bin/claude"
        )

        def past_the_resolver(_identifier):
            raise RuntimeError("resolved: the launch goes on")

        monkeypatch.setattr(mgr.switcher, "resolve_account", past_the_resolver)
        with pytest.raises(RuntimeError, match="the launch goes on"):
            mgr.run(ACCOUNT_NUM, [], share=True, share_history=True)

    def test_a_directory_link_that_cannot_be_made_leaves_transcripts_where_they_were(
        self, win_history, monkeypatch, capsys
    ):
        """The probe said yes, the junction itself failed: nothing has moved,
        and the shared tree is untouched."""
        source, session_dir, mgr = win_history
        own = session_dir / "projects" / "-home-user-app"
        own.mkdir(parents=True)
        (own / "bbb.jsonl").write_text("profile-b\n")

        def refuse(target, link):
            raise OSError(1, "The request is not supported")

        monkeypatch.setattr(session_mod, "make_dir_link", refuse)
        mgr._sync_sharing(session_dir, share=True, share_history=True)

        assert (own / "bbb.jsonl").read_text() == "profile-b\n"
        assert not is_link(session_dir / "projects")
        assert not (source / "projects" / "-home-user-app" / "bbb.jsonl").exists()
        assert not list(session_dir.glob("projects.*"))
        assert "projects: it could not be linked here" in capsys.readouterr().out

    def test_a_staging_link_left_by_an_interrupted_launch_is_cleared(
        self, win_history
    ):
        source, session_dir, mgr = win_history
        own = session_dir / "projects" / "-home-user-app"
        own.mkdir(parents=True)
        (own / "bbb.jsonl").write_text("profile-b\n")
        make_dir_link(source, session_dir / "projects.cswap-link-staging")

        mgr._sync_sharing(session_dir, share=True, share_history=True)

        assert is_link(session_dir / "projects")
        assert (source / "projects" / "-home-user-app" / "bbb.jsonl").read_text() == "profile-b\n"
        assert not list(session_dir.glob("projects.*"))

    def test_a_staging_link_is_cleared_when_the_history_is_already_gone(
        self, win_history
    ):
        """Killed after the merge removed the profile's history and before the
        rename: the next launch links ``projects`` afresh — and must not walk
        past the leftover forever."""
        source, session_dir, mgr = win_history
        make_dir_link(source / "projects", session_dir / "projects.cswap-link-staging")

        mgr._sync_sharing(session_dir, share=True, share_history=True)

        assert is_link(session_dir / "projects")
        assert not list(session_dir.glob("projects.*"))

    def test_a_staging_link_is_cleared_by_a_launch_that_does_not_share(
        self, win_history
    ):
        source, session_dir, mgr = win_history
        make_dir_link(source / "projects", session_dir / "projects.cswap-link-staging")

        mgr._sync_sharing(session_dir, share=True, share_history=False)

        assert not list(session_dir.glob("projects.*"))
        assert (source / "projects" / "-home-user-app" / "aaa.jsonl").exists()

    def test_only_a_link_is_cleared_from_the_staging_name(self, win_history):
        source, session_dir, mgr = win_history
        odd = session_dir / "projects.cswap-link-staging"
        odd.mkdir()
        (odd / "keep.jsonl").write_text("not a link\n")

        mgr._sync_sharing(session_dir, share=True, share_history=True)

        assert (odd / "keep.jsonl").read_text() == "not a link\n"
        assert is_link(session_dir / "projects")

    def test_a_staging_link_is_left_while_a_peer_holds_the_lock(
        self, win_history, monkeypatch
    ):
        """A busy migration lock means the link may be a peer's live staging
        link, not a leftover: left for the next launch, and the launch goes on."""
        source, session_dir, mgr = win_history
        make_dir_link(source / "projects", session_dir / "projects.cswap-link-staging")

        class Busy:
            def __init__(self, path, timeout=None):
                pass

            def __enter__(self):
                raise LockError("held by a peer")

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(session_mod, "FileLock", Busy)
        mgr._sync_sharing(session_dir, share=True, share_history=True)

        assert is_link(session_dir / "projects.cswap-link-staging")
        assert is_link(session_dir / "projects")

    def test_a_failed_rename_is_retried_as_a_direct_link(
        self, win_history, monkeypatch
    ):
        source, session_dir, mgr = win_history
        own = session_dir / "projects" / "-home-user-app"
        own.mkdir(parents=True)
        (own / "bbb.jsonl").write_text("profile-b\n")

        def refuse(src, dst, **_kwargs):
            raise OSError(5, "Access is denied")

        monkeypatch.setattr(session_mod, "replace_with_retry", refuse)
        mgr._sync_sharing(session_dir, share=True, share_history=True)

        assert is_link(session_dir / "projects")
        assert link_points_at(session_dir / "projects", source / "projects")
        assert (source / "projects" / "-home-user-app" / "bbb.jsonl").read_text() == "profile-b\n"
        assert not list(session_dir.glob("projects.*"))

    def test_when_the_link_cannot_be_installed_the_next_launch_links_it(
        self, win_history, monkeypatch, capsys
    ):
        """Both the rename and the direct attempt failed, after the merge: the
        history is in ~/.claude, the launch says so, nothing is left behind,
        and the next launch links the profile."""
        source, session_dir, mgr = win_history
        own = session_dir / "projects" / "-home-user-app"
        own.mkdir(parents=True)
        (own / "bbb.jsonl").write_text("profile-b\n")
        real_make_link = session_mod.SessionManager._make_link

        def staging_only(self, target, dest):
            if dest.name == "projects":
                raise OSError(5, "Access is denied")
            return real_make_link(self, target, dest)

        def refuse(src, dst, **_kwargs):
            raise OSError(5, "Access is denied")

        monkeypatch.setattr(session_mod.SessionManager, "_make_link", staging_only)
        monkeypatch.setattr(session_mod, "replace_with_retry", refuse)
        mgr._sync_sharing(session_dir, share=True, share_history=True)

        assert (source / "projects" / "-home-user-app" / "bbb.jsonl").read_text() == "profile-b\n"
        assert not (session_dir / "projects").exists()
        assert not list(session_dir.glob("projects.*"))
        assert "linking it back failed" in capsys.readouterr().out

        monkeypatch.setattr(session_mod.SessionManager, "_make_link", real_make_link)
        monkeypatch.setattr(session_mod, "replace_with_retry", replace_with_retry)
        mgr._sync_sharing(session_dir, share=True, share_history=True)
        assert is_link(session_dir / "projects")


class TestShareHistoryWindowsSymlinks:
    """Windows with Developer Mode, or an elevated terminal: both history
    items are linked exactly as on macOS and Linux, and files are symlinks
    rather than copies."""

    @pytest.fixture
    def win_history(self, history_setup, windows_symlinks):
        source, session_dir, mgr = history_setup
        mgr.switcher.platform = Platform.WINDOWS
        return source, session_dir, mgr

    def test_links_both_history_items(self, win_history, capsys):
        source, session_dir, mgr = win_history
        mgr._sync_sharing(session_dir, share=True, share_history=True)

        assert is_link(session_dir / "projects")
        assert (session_dir / "history.jsonl").is_symlink()
        assert link_points_at(session_dir / "history.jsonl", source / "history.jsonl")
        manifest = json.loads((session_dir / SHARE_MANIFEST).read_text())
        assert {"projects", "history.jsonl"} <= set(manifest["items"])
        assert manifest["mode"] == "symlink"
        assert "stays per-account" not in capsys.readouterr().out

    def test_merges_the_accounts_own_history_file_then_links_it(self, win_history):
        source, session_dir, mgr = win_history
        (session_dir / "history.jsonl").write_text('{"p": "main"}\n{"p": "profile"}\n')

        mgr._sync_sharing(session_dir, share=True, share_history=True)

        assert (session_dir / "history.jsonl").is_symlink()
        lines = (source / "history.jsonl").read_text().splitlines()
        assert lines.count('{"p": "main"}') == 1
        assert '{"p": "profile"}' in lines

    def test_files_are_symlinks_too(self, win_history):
        source, session_dir, mgr = win_history
        mgr._sync_sharing(session_dir, share=True)

        assert (session_dir / "settings.json").is_symlink()
        assert (session_dir / "CLAUDE.md").is_symlink()
        assert is_link(session_dir / "skills")
        (source / "settings.json").write_text('{"changed": true}')
        assert (session_dir / "settings.json").read_text() == '{"changed": true}'

    def test_copies_from_a_launch_without_the_privilege_become_links(
        self, win_history, monkeypatch
    ):
        source, session_dir, mgr = win_history
        monkeypatch.setattr(session_mod, "can_symlink", lambda _dir: False)
        mgr._sync_sharing(session_dir, share=True)
        assert (session_dir / "settings.json").is_file()
        assert not is_link(session_dir / "settings.json")

        monkeypatch.setattr(session_mod, "can_symlink", lambda _dir: True)
        mgr._sync_sharing(session_dir, share=True)

        assert (session_dir / "settings.json").is_symlink()

    def test_resync_leaves_correct_links_alone(self, win_history, monkeypatch):
        source, session_dir, mgr = win_history
        mgr._sync_sharing(session_dir, share=True, share_history=True)
        removed: list[str] = []
        real = session_mod.remove_link

        def tracking(link):
            removed.append(Path(link).name)
            real(link)

        monkeypatch.setattr(session_mod, "remove_link", tracking)
        mgr._sync_sharing(session_dir, share=True, share_history=True)

        assert removed == []

    def test_a_link_that_cannot_be_made_leaves_the_history_where_it_was(
        self, win_history, monkeypatch, capsys
    ):
        """The probe said yes and the link itself still failed. Because the
        link is made before the merge, the account's prompt history has not
        moved: it is still the profile's own, ~/.claude is unchanged, and
        the launch that can link merges it."""
        source, session_dir, mgr = win_history
        (session_dir / "history.jsonl").write_text('{"p": "profile"}\n')
        real_symlink_to = Path.symlink_to

        def refuse_history(self, target, *args, **kwargs):
            if self.name.startswith("history.jsonl"):
                e = OSError(22, "A required privilege is not held by the client")
                e.winerror = 1314
                raise e
            return real_symlink_to(self, target, *args, **kwargs)

        monkeypatch.setattr(Path, "symlink_to", refuse_history)
        mgr._sync_sharing(session_dir, share=True, share_history=True)

        assert (session_dir / "history.jsonl").read_text() == '{"p": "profile"}\n'
        assert (source / "history.jsonl").read_text() == '{"p": "main"}\n'
        assert not list(session_dir.glob("history.jsonl.*"))  # no staging left
        assert "could not be linked here" in capsys.readouterr().out
        manifest = json.loads((session_dir / SHARE_MANIFEST).read_text())
        assert "history.jsonl" not in manifest["items"]

        monkeypatch.setattr(Path, "symlink_to", real_symlink_to)
        mgr._sync_sharing(session_dir, share=True, share_history=True)
        assert (session_dir / "history.jsonl").is_symlink()
        assert '{"p": "profile"}' in (source / "history.jsonl").read_text()


class TestHistoryMergeNeverLooksThroughLinks:
    """``rglob`` descends into a junction; the merge must not. A link inside
    a profile's history — a directory junction or symlink, or a file symlink
    — stops the merge before anything moves, and what it leads to is never
    touched."""

    @staticmethod
    def _own_history(session_dir: Path) -> Path:
        own = session_dir / "projects" / "-home-user-app"
        own.mkdir(parents=True)
        (own / "bbb.jsonl").write_text("profile-b\n")
        return own

    def test_a_directory_link_inside_projects_stops_the_merge(
        self, history_setup, tmp_path, capsys
    ):
        source, session_dir, mgr = history_setup
        own = self._own_history(session_dir)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "mine.jsonl").write_text("mine\n")
        make_dir_link(elsewhere, session_dir / "projects" / "linked")

        mgr._sync_sharing(session_dir, share=True, share_history=True)

        assert (elsewhere / "mine.jsonl").read_text() == "mine\n"
        assert (own / "bbb.jsonl").read_text() == "profile-b\n"  # nothing moved
        assert not (source / "projects" / "linked").exists()
        assert not is_link(session_dir / "projects")
        assert "merging the profile's existing history failed" in (
            capsys.readouterr().out
        )

    def test_a_file_symlink_inside_projects_stops_the_merge(
        self, history_setup, tmp_path, capsys
    ):
        if not _host_can_symlink(tmp_path):
            pytest.skip("this host cannot create symlinks")
        source, session_dir, mgr = history_setup
        own = self._own_history(session_dir)
        elsewhere = tmp_path / "mine.jsonl"
        elsewhere.write_text("mine\n")
        (own / "link.jsonl").symlink_to(elsewhere)

        mgr._sync_sharing(session_dir, share=True, share_history=True)

        assert elsewhere.read_text() == "mine\n"
        assert (own / "bbb.jsonl").read_text() == "profile-b\n"
        assert (own / "link.jsonl").is_symlink()
        assert not (source / "projects" / "-home-user-app" / "link.jsonl").exists()
        assert "merging the profile's existing history failed" in (
            capsys.readouterr().out
        )

    def test_history_tree_is_deepest_first_and_names_a_link(self, tmp_path):
        root = tmp_path / "root"
        (root / "a" / "b").mkdir(parents=True)
        (root / "a" / "b" / "f.jsonl").write_text("x")
        (root / "c.jsonl").write_text("y")

        tree = session_mod._history_tree(root)

        assert set(tree) == {root / "a", root / "a" / "b", root / "a" / "b" / "f.jsonl", root / "c.jsonl"}
        index = {p: i for i, p in enumerate(tree)}
        assert index[root / "a" / "b" / "f.jsonl"] < index[root / "a" / "b"] < index[root / "a"]

        make_dir_link(tmp_path, root / "a" / "up")  # a link to an ancestor
        with pytest.raises(OSError, match="is a link"):
            session_mod._history_tree(root)


def _turn_sharing(mgr: SessionManager, value: str) -> None:
    """Write ``run.shareHistory`` the way ``cswap config set`` does."""
    set_setting(mgr.switcher.backup_dir, "run.shareHistory", value)


def _active_default_login(monkeypatch, mgr: SessionManager) -> None:
    """Make account 2 the default login, so ``run`` takes its fast path."""
    monkeypatch.setattr(
        mgr.switcher, "_get_current_account", lambda: (ACCOUNT_EMAIL, ORG_UUID)
    )


OPT_OUT = "cswap config set run.shareHistory false"


class TestShareHistoryDecision:
    """Who decides whether a launch shares history: the launch's own flag,
    then ``run.shareHistory``, then the default — which is to share."""

    def test_shared_unless_turned_off(self, manager):
        assert manager._resolve_share_history(None) is True
        _turn_sharing(manager, "false")
        assert manager._resolve_share_history(None) is False

    @pytest.mark.parametrize("stored", [None, "true", "false"])
    def test_the_flag_wins_over_the_setting(self, manager, stored):
        if stored:
            _turn_sharing(manager, stored)
        assert manager._resolve_share_history(True) is True
        assert manager._resolve_share_history(False) is False

    # -- Windows ------------------------------------------------------------
    #
    # The platform no longer decides. Whether each history item can be
    # linked on this machine is settled per item when the profile is synced
    # (TestShareHistoryWindows); the answer here is the same as anywhere.

    def test_windows_follows_the_default_the_setting_and_the_flag(
        self, manager, capsys
    ):
        manager.switcher.platform = Platform.WINDOWS
        assert manager._resolve_share_history(None) is True
        assert manager._resolve_share_history(True) is True
        assert manager._resolve_share_history(False) is False
        _turn_sharing(manager, "false")
        assert manager._resolve_share_history(None) is False
        assert manager._resolve_share_history(True) is True
        assert capsys.readouterr().out == ""

    @pytest.mark.parametrize("requested", [None, True, False])
    def test_run_hands_setup_session_the_request_not_the_answer(
        self, manager, capture_exec, monkeypatch, capsys, requested
    ):
        """``setup_session`` resolves the same request the same way; handed
        the answer instead, a defaulted True would read as asked for, and
        the note for an unlinkable history item would come out as a
        warning on a launch that asked for nothing."""
        manager.switcher.platform = Platform.WINDOWS
        handed_over = []

        def setup_session(identifier, share, share_history=None):
            handed_over.append(share_history)
            raise SessionError("stop here")

        monkeypatch.setattr(manager, "setup_session", setup_session)
        with pytest.raises(SessionError, match="stop here"):
            manager.run("2", [], share_history=requested)

        assert handed_over == [requested]
        assert capsys.readouterr().out == ""

    def test_the_flag_is_remembered_as_asked_for(self, manager):
        """What ``_report_history_unlinkable`` reads to pick its volume."""
        manager._resolve_share_history(None)
        assert manager._share_history_asked is False
        manager._resolve_share_history(True)
        assert manager._share_history_asked is True
        manager._resolve_share_history(False)
        assert manager._share_history_asked is False

    def test_plain_claude_launch_never_asks(self, manager, capture_exec, capsys):
        """A bare unmapped ``cswap run`` is plain claude: no profile, so no
        history question, and nothing to warn a Windows user about."""
        manager.switcher.platform = Platform.WINDOWS
        _turn_sharing(manager, "true")
        with pytest.raises(_ExecCalled):
            manager.exec_default([])
        assert capsys.readouterr().out == ""

    # -- the same-account fast path (#239) --------------------------------
    #
    # Plain claude on the default login reads and writes ``~/.claude``'s
    # history, while every other launch of the account uses its profile's.
    # These are the launches that do not share; the ones that do merge the
    # profile's history instead (TestShareHistoryIsTheDefault).

    @staticmethod
    def _profile_with_its_own_history(mgr: SessionManager) -> Path:
        session_dir = session_dir_for(
            mgr.switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
        )
        transcript = session_dir / "projects" / "-home-user-app" / "bbb.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text("profile-b\n")
        return session_dir

    @staticmethod
    def _not_sharing(mgr: SessionManager, how: str) -> bool | None:
        """Put ``mgr`` in one of the two states that do not share history;
        returns what the launch itself has to pass."""
        if how == "setting":
            _turn_sharing(mgr, "false")
        return False if how == "flag" else None

    def test_fast_path_has_nothing_to_add_when_history_is_shared(
        self, manager, capture_exec, monkeypatch, capsys
    ):
        _active_default_login(monkeypatch, manager)
        with pytest.raises(_ExecCalled):
            manager.run("2", [])
        out = capsys.readouterr().out
        assert "already the active default login" in out
        assert "chat history" not in out

    @pytest.mark.parametrize("how", ["flag", "setting"])
    def test_fast_path_says_whose_history_it_uses_when_the_account_has_its_own(
        self, manager, capture_exec, monkeypatch, capsys, how
    ):
        """The one launch of the account whose conversations ``--resume``
        will not find among the others, and the reverse."""
        _active_default_login(monkeypatch, manager)
        session_dir = self._profile_with_its_own_history(manager)
        requested = self._not_sharing(manager, how)

        with pytest.raises(_ExecCalled) as exc:
            manager.run("2", [], share_history=requested)

        assert "CLAUDE_CONFIG_DIR" not in exc.value.env
        assert "default login's chat history" in capsys.readouterr().out
        # Said, not acted on: the history stays where the user keeps it.
        assert (
            session_dir / "projects" / "-home-user-app" / "bbb.jsonl"
        ).read_text() == "profile-b\n"

    @pytest.mark.parametrize("how", ["flag", "setting"])
    def test_fast_path_has_nothing_to_add_without_a_history_of_its_own(
        self, manager, capture_exec, monkeypatch, capsys, how
    ):
        """No profile, or one that never held a conversation: there is no
        second history for this launch to be mistaken for."""
        _active_default_login(monkeypatch, manager)
        requested = self._not_sharing(manager, how)

        with pytest.raises(_ExecCalled):
            manager.run("2", [], share_history=requested)

        out = capsys.readouterr().out
        assert "already the active default login" in out
        assert "chat history" not in out

    def test_fast_path_merges_on_windows_too(
        self, manager, capture_exec, monkeypatch, temp_home
    ):
        """Transcripts are linked through a junction, which any Windows user
        may make, so the default login's profile is merged there as anywhere
        — on a machine without the symlink privilege included."""
        _active_default_login(monkeypatch, manager)
        manager.switcher.platform = Platform.WINDOWS
        session_dir = self._profile_with_its_own_history(manager)

        with pytest.raises(_ExecCalled):
            manager.run("2", [], share_history=True)

        assert (
            temp_home / ".claude" / "projects" / "-home-user-app" / "bbb.jsonl"
        ).read_text() == "profile-b\n"
        assert is_link(session_dir / "projects")


@pytest.mark.skipif(sys.platform == "win32", reason="history sharing is POSIX-only")
class TestShareHistoryIsTheDefault:
    """A launch that names no preference shares history, and a profile that
    kept its own until now is told what happened to it."""

    @pytest.fixture
    def launch(self, manager, capture_exec, auth_status_tracks_seed, refresh_rotates):
        """``cswap run 2`` up to the exec; returns the profile it launched into."""

        def run(**kwargs) -> Path:
            with pytest.raises(_ExecCalled) as exc:
                manager.run("2", [], **kwargs)
            return Path(exc.value.env["CLAUDE_CONFIG_DIR"])

        return run

    @staticmethod
    def _is_linked(session_dir: Path, shared: Path) -> bool:
        return all(
            (session_dir / name).is_symlink()
            and (session_dir / name).readlink() == shared / name
            for name in session_mod.HISTORY_ITEMS
        )

    @staticmethod
    def _give_own_history(session_dir: Path) -> None:
        proj = session_dir / "projects" / "-home-user-app"
        proj.mkdir(parents=True)
        (proj / "bbb.jsonl").write_text("profile-b\n")
        (session_dir / "history.jsonl").write_text('{"p": "profile"}\n')

    def test_a_plain_run_shares_history(self, launch, temp_home):
        session_dir = launch()
        assert self._is_linked(session_dir, temp_home / ".claude")

    def test_setting_it_off_keeps_history_in_the_profile(
        self, launch, manager, temp_home
    ):
        _turn_sharing(manager, "false")
        session_dir = launch()
        assert not (session_dir / "projects").exists()
        assert not (session_dir / "history.jsonl").exists()
        # Customizations are a separate switch and stay shared.
        assert not (temp_home / ".claude" / "projects").exists()

    def test_no_share_history_flag_opts_one_launch_out(self, launch, temp_home):
        session_dir = launch(share_history=False)
        assert not (session_dir / "projects").exists()
        # The next launch without it is back on the default.
        assert launch() == session_dir
        assert self._is_linked(session_dir, temp_home / ".claude")

    def test_share_history_flag_opts_one_launch_in(self, launch, manager, temp_home):
        _turn_sharing(manager, "false")
        session_dir = launch(share_history=True)
        assert self._is_linked(session_dir, temp_home / ".claude")

    def test_setup_session_reads_the_default_too(
        self, manager, auth_status_tracks_seed, refresh_rotates, temp_home
    ):
        """``run`` is not the only way into a profile. A launcher that calls
        ``setup_session`` and forwards "no flag" as None must land on the
        same default, not on a falsy None."""
        session_dir, _, _ = manager.setup_session("2", share=True)
        assert self._is_linked(session_dir, temp_home / ".claude")

        _turn_sharing(manager, "false")
        manager.setup_session("2", share=True, share_history=None)
        assert not (session_dir / "projects").exists()

    def test_first_plain_run_after_the_upgrade_merges_the_accounts_own_history(
        self, launch, temp_home, capsys
    ):
        """The upgrade itself: a profile bootstrapped while history was per
        account, launched by a user who never set anything."""
        session_dir = launch(share_history=False)  # as the old default left it
        self._give_own_history(session_dir)
        capsys.readouterr()

        assert launch() == session_dir

        shared = temp_home / ".claude"
        assert (
            shared / "projects" / "-home-user-app" / "bbb.jsonl"
        ).read_text() == "profile-b\n"
        assert '{"p": "profile"}' in (shared / "history.jsonl").read_text()
        assert self._is_linked(session_dir, shared)
        # Told once, although two items were merged.
        assert capsys.readouterr().out.count(OPT_OUT) == 1

        # And never again: nothing of its own is left to meet.
        launch()
        assert OPT_OUT not in capsys.readouterr().out

    # -- the account that is the default login (the fast path) ------------

    def test_default_login_account_gets_its_own_history_merged_too(
        self, launch, manager, monkeypatch, temp_home, capsys
    ):
        """The account's sessions wrote into its profile; then it became the
        default login, and ``cswap run`` for it is plain claude on
        ``~/.claude``. Without a merge on that path the old conversations
        stay out of ``--resume`` for as long as the account stays the
        default, however often it is run."""
        session_dir = launch(share_history=False)  # as the old default left it
        self._give_own_history(session_dir)
        credentials = (session_dir / ".credentials.json").read_bytes()
        _active_default_login(monkeypatch, manager)
        capsys.readouterr()

        with pytest.raises(_ExecCalled) as exc:
            manager.run("2", [])

        assert "CLAUDE_CONFIG_DIR" not in exc.value.env  # still plain claude
        shared = temp_home / ".claude"
        assert (
            shared / "projects" / "-home-user-app" / "bbb.jsonl"
        ).read_text() == "profile-b\n"
        assert '{"p": "profile"}' in (shared / "history.jsonl").read_text()
        assert self._is_linked(session_dir, shared)
        out = capsys.readouterr().out
        assert "already the active default login" in out
        assert out.count(OPT_OUT) == 1
        # History only: the profile's credential copy is what the fast path
        # exists to leave alone.
        assert (session_dir / ".credentials.json").read_bytes() == credentials

    def test_fast_path_merge_touches_history_and_nothing_else(
        self, launch, manager, monkeypatch, temp_home
    ):
        """It is the history half of a session launch, run for a profile the
        launch does not use. ``--no-share`` on such a launch must not strip
        the profile's customizations: that is the next session launch's
        business, and its flags may differ."""
        (temp_home / ".claude" / "settings.json").write_text("{}")
        session_dir = launch(share_history=False)
        assert (session_dir / "settings.json").is_symlink()
        self._give_own_history(session_dir)
        _active_default_login(monkeypatch, manager)

        with pytest.raises(_ExecCalled):
            manager.run("2", [], share=False)

        shared = temp_home / ".claude"
        assert self._is_linked(session_dir, shared)
        assert (session_dir / "settings.json").readlink() == shared / "settings.json"
        manifest = json.loads((session_dir / SHARE_MANIFEST).read_text())
        assert set(manifest["items"]) == {"settings.json", "projects", "history.jsonl"}

    def test_links_made_on_the_fast_path_are_undone_by_a_later_opt_out(
        self, launch, manager, monkeypatch, temp_home
    ):
        """They are recorded like any other link cswap makes. Unrecorded,
        ``--no-share-history`` would leave the profile on the shared history."""
        session_dir = launch(share_history=False)
        self._give_own_history(session_dir)
        _active_default_login(monkeypatch, manager)
        with pytest.raises(_ExecCalled):
            manager.run("2", [])
        assert self._is_linked(session_dir, temp_home / ".claude")

        monkeypatch.setattr(manager.switcher, "_get_current_account", lambda: None)
        assert launch(share_history=False) == session_dir
        assert not (session_dir / "projects").exists()
        assert not (session_dir / "history.jsonl").exists()
        # What was merged stays shared; only the links go.
        assert (
            temp_home / ".claude" / "projects" / "-home-user-app" / "bbb.jsonl"
        ).exists()

    def test_fast_path_merge_waits_for_a_session_still_in_the_profile(
        self, launch, manager, monkeypatch, temp_home, capsys
    ):
        """A session started before the account became the default login is
        still writing the profile's history. The launch goes ahead on the
        default login and says why the old conversations are not there."""
        session_dir = launch(share_history=False)
        self._give_own_history(session_dir)
        _active_default_login(monkeypatch, manager)
        make_live(session_dir)
        capsys.readouterr()

        with pytest.raises(_ExecCalled) as exc:
            manager.run("2", [])

        assert "CLAUDE_CONFIG_DIR" not in exc.value.env
        out = capsys.readouterr().out
        assert out.count("keeps its own") == 1
        assert "then relaunch:  cswap run 2\n" in out
        assert (
            session_dir / "projects" / "-home-user-app" / "bbb.jsonl"
        ).read_text() == "profile-b\n"
        assert not (temp_home / ".claude" / "projects" / "-home-user-app").exists()

    def test_fast_path_launches_even_when_the_merge_cannot_run(
        self, launch, manager, monkeypatch, capsys
    ):
        session_dir = launch(share_history=False)
        self._give_own_history(session_dir)
        _active_default_login(monkeypatch, manager)

        class Unopenable(FileLock):
            def acquire(self, timeout=None):
                raise PermissionError("read-only file system")

        monkeypatch.setattr(session_mod, "FileLock", Unopenable)
        with pytest.raises(_ExecCalled):
            manager.run("2", [])

        assert "could not be locked" in capsys.readouterr().out
        assert not (session_dir / "projects").is_symlink()

    def test_a_partial_sync_leaves_the_rest_of_the_manifest_alone(
        self, history_setup
    ):
        """``scope`` is the whole contract of a partial sync: entries outside
        it survive on disk and in the manifest, whatever happens inside it —
        including when nothing inside it is left active."""
        _, session_dir, mgr = history_setup
        mgr._sync_sharing(session_dir, share=True, share_history=True)
        before = json.loads((session_dir / SHARE_MANIFEST).read_text())["items"]
        assert {"settings.json", "projects"} <= set(before)

        mgr._sync_items(session_dir, (), scope=session_mod.HISTORY_ITEMS)

        assert not (session_dir / "projects").exists()
        assert not (session_dir / "history.jsonl").exists()
        assert (session_dir / "settings.json").is_symlink()
        after = json.loads((session_dir / SHARE_MANIFEST).read_text())["items"]
        assert set(after) == set(before) - set(session_mod.HISTORY_ITEMS)

    def test_fast_path_never_creates_a_profile(
        self, manager, capture_exec, monkeypatch, temp_home
    ):
        """No profile means no history to bring in, and no reason to make
        one: a second copy of the default login's credential is what this
        path exists to avoid."""
        _active_default_login(monkeypatch, manager)
        with pytest.raises(_ExecCalled):
            manager.run("2", [])

        assert not manager.sessions_dir.exists()
        assert not (temp_home / ".claude" / "projects").exists()

    def test_a_profile_with_nothing_of_its_own_is_told_nothing(
        self, history_setup, capsys
    ):
        _, session_dir, mgr = history_setup
        mgr._sync_sharing(session_dir, share=True, share_history=True)
        assert (session_dir / "projects").is_symlink()
        assert OPT_OUT not in capsys.readouterr().out

    @pytest.mark.parametrize("stored", ["true", "false"])
    def test_a_user_who_chose_is_told_nothing(self, history_setup, capsys, stored):
        """``false`` reaches a merge only through an explicit --share-history;
        either way the setting is theirs and they know where it is."""
        _, session_dir, mgr = history_setup
        _turn_sharing(mgr, stored)
        self._give_own_history(session_dir)

        mgr._sync_sharing(session_dir, share=True, share_history=True)

        out = capsys.readouterr().out
        assert "conversation history is now shared" in out  # the merge ran
        assert OPT_OUT not in out

    def test_a_merge_that_has_to_wait_names_the_opt_out_too(
        self, history_setup, monkeypatch, capsys
    ):
        """Deferred is where the opt-out matters most: until the sessions
        are closed the warning repeats on every launch, and this is the one
        line that says how to make it stop."""
        _, session_dir, mgr = history_setup
        self._give_own_history(session_dir)
        blocker = ClaudeSession(
            pid=63802,
            session_id="abc",
            cwd="/tmp",
            started_at=0,
            kind="interactive",
            entrypoint="cli",
        )
        monkeypatch.setattr(
            session_mod, "scan_live_sessions", lambda p: ([blocker], 0)
        )

        mgr._sync_sharing(session_dir, share=True, share_history=True)

        out = capsys.readouterr().out
        assert out.count("keeps its own") == 1
        assert out.count(OPT_OUT) == 1
        assert not (session_dir / "projects").is_symlink()

    def test_deferral_repeats_the_flag_where_settings_turn_sharing_off(
        self, history_setup, monkeypatch, capsys
    ):
        """The command to repeat has to share history when it is repeated.
        With ``run.shareHistory false`` a plain ``cswap run 2`` would not."""
        _, session_dir, mgr = history_setup
        _turn_sharing(mgr, "false")
        self._give_own_history(session_dir)
        blocker = ClaudeSession(
            pid=63802,
            session_id="abc",
            cwd="/tmp",
            started_at=0,
            kind="interactive",
            entrypoint="cli",
        )
        monkeypatch.setattr(
            session_mod, "scan_live_sessions", lambda p: ([blocker], 0)
        )

        mgr._sync_sharing(session_dir, share=True, share_history=True)

        assert (
            "then relaunch:  cswap run 2 --share-history\n"
            in capsys.readouterr().out
        )


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

    @pytest.mark.skipif(sys.platform == "win32", reason="symlinks are POSIX-only")
    def test_symlinked_profile_reads_the_target_keychain_entry(
        self, tmp_path, macos_platform, block_real_keychain
    ):
        """A tool that launches claude at a shared directory and leases it to
        an account through a profile symlink leaves every rotation under the
        target's hashed name, with the seed at the link's path behind it."""
        target = tmp_path / "space"
        target.mkdir()
        (target / ".credentials.json").write_text(
            '{"claudeAiOauth": {"accessToken": "sk-stale-seed"}}'
        )
        session_dir = tmp_path / "sess"
        session_dir.symlink_to(target)
        block_real_keychain.set_password(
            keychain_service_name(target),
            session_mod._keychain_account_name(),
            '{"claudeAiOauth": {"accessToken": "sk-rotated"}}',
        )
        creds = session_mod.read_session_credentials(session_dir)
        assert creds is not None and "sk-rotated" in creds

    @pytest.mark.skipif(sys.platform == "win32", reason="symlinks are POSIX-only")
    def test_symlinked_profile_prefers_its_own_keychain_entry(
        self, tmp_path, macos_platform, block_real_keychain
    ):
        target = tmp_path / "space"
        target.mkdir()
        session_dir = tmp_path / "sess"
        session_dir.symlink_to(target)
        account = session_mod._keychain_account_name()
        block_real_keychain.set_password(
            keychain_service_name(target),
            account,
            '{"claudeAiOauth": {"accessToken": "sk-target"}}',
        )
        block_real_keychain.set_password(
            keychain_service_name(session_dir),
            account,
            '{"claudeAiOauth": {"accessToken": "sk-own"}}',
        )
        creds = session_mod.read_session_credentials(session_dir)
        assert creds is not None and "sk-own" in creds

    @pytest.mark.skipif(sys.platform == "win32", reason="symlinks are POSIX-only")
    def test_relative_symlink_target_is_normalised_before_hashing(
        self, tmp_path, macos_platform, block_real_keychain
    ):
        """``sessions/profile -> ../space`` hashes ``/x/space``, the path a
        tool launching at the target exports, not ``/x/sessions/../space``."""
        target = tmp_path / "space"
        target.mkdir()
        (target / ".credentials.json").write_text(
            '{"claudeAiOauth": {"accessToken": "sk-stale-seed"}}'
        )
        (tmp_path / "sessions").mkdir()
        session_dir = tmp_path / "sessions" / "profile"
        session_dir.symlink_to("../space")
        block_real_keychain.set_password(
            keychain_service_name(target),
            session_mod._keychain_account_name(),
            '{"claudeAiOauth": {"accessToken": "sk-rotated"}}',
        )
        creds = session_mod.read_session_credentials(session_dir)
        assert creds is not None and "sk-rotated" in creds

    @pytest.mark.skipif(sys.platform == "win32", reason="symlinks are POSIX-only")
    def test_lexical_target_naming_another_directory_is_not_consulted(
        self, tmp_path, macos_platform, block_real_keychain
    ):
        """When the link's parent is itself a symlink elsewhere, ``..`` resolves
        under the parent's target, so the lexical spelling names an unrelated
        directory whose entry must never be served as this profile's."""
        (tmp_path / "leases" / "sessions").mkdir(parents=True)
        (tmp_path / "leases" / "space").mkdir()
        (tmp_path / "leases" / "space" / ".credentials.json").write_text(
            '{"claudeAiOauth": {"accessToken": "sk-seed"}}'
        )
        (tmp_path / "sessions").symlink_to("leases/sessions")
        session_dir = tmp_path / "sessions" / "profile"
        session_dir.symlink_to("../space")
        unrelated = tmp_path / "space"
        unrelated.mkdir()
        block_real_keychain.set_password(
            keychain_service_name(unrelated),
            session_mod._keychain_account_name(),
            '{"claudeAiOauth": {"accessToken": "sk-unrelated"}}',
        )
        creds = session_mod.read_session_credentials(session_dir)
        assert creds is not None and "sk-seed" in creds

    @pytest.mark.skipif(sys.platform == "win32", reason="symlinks are POSIX-only")
    def test_raw_joined_target_spelling_still_matches(
        self, tmp_path, macos_platform, block_real_keychain
    ):
        """A tool that exports ``$(dirname link)/$(readlink link)`` verbatim
        hashes the un-normalised join; that spelling stays a candidate."""
        target = tmp_path / "space"
        target.mkdir()
        (target / ".credentials.json").write_text(
            '{"claudeAiOauth": {"accessToken": "sk-stale-seed"}}'
        )
        (tmp_path / "sessions").mkdir()
        session_dir = tmp_path / "sessions" / "profile"
        session_dir.symlink_to("../space")
        block_real_keychain.set_password(
            keychain_service_name(f"{session_dir.parent}/../space"),
            session_mod._keychain_account_name(),
            '{"claudeAiOauth": {"accessToken": "sk-rotated"}}',
        )
        creds = session_mod.read_session_credentials(session_dir)
        assert creds is not None and "sk-rotated" in creds


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

        # THE SEED, not the directory. A failed validation must stop the
        # profile being reused; the account's own conversation history is
        # user data and survives.
        assert not (session_dir / ".credentials.json").exists()

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


#: Methods whose `print` IS the sanctioned one. A set, not a single name, so
#: adding a second printer with the same print-and-log contract is a one-line
#: change rather than a guard failure.
_SANCTIONED_PRINTERS = {"_note", "_warn"}

def _own_scope(func):
    """Statements `func` binds names in -- its body, minus every nested scope.

    A `def` inside an `if` inside `func` binds in `func`; one inside a nested
    `def`, or in a `class` body, does not. `ast.walk` cannot tell those apart.
    """
    import ast

    out = []
    stack = list(func.body)
    while stack:
        node = stack.pop()
        out.append(node)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue          # its body binds in ITS scope, not in `func`'s
        stack.extend(ast.iter_child_nodes(node))
    return out


def _bare_print_printers(src: str | None = None) -> set[str]:
    """`printer`'s own functions that reach the builtin `print`.

    PROSPECTIVE ON THIS TREE, and worth saying plainly: `printer.py` has
    eight module-level assignments and every one is a `Dict` or a `Constant`,
    so `bindings`, `_names` and the alias promotion contribute NOTHING to
    today's answer. They fire the day it grows an alias, a `partial` or a
    delegating printer. A re-export (`from ._impl import warning`) is the one
    shape still missed on purpose -- proving that name is a printer needs the
    other module, which this function does not read.

    DERIVED, not listed: a list is right until someone adds a function, and
    then it is silently short. Measured today this returns `{"error",
    "warning"}` -- the same answer a top-level scan gives, so nothing on this
    tree changes. What the walk and the fixpoint add is the two shapes that
    scan misses and that a future printer is most likely to take: one defined
    inside an `if`/`try`, and one that DELEGATES to a printer instead of
    calling `print` itself. Measured, appending either to `printer.py`: the
    top-level scan still answers `{"error", "warning"}`, this answers with the
    new name.
    """
    import ast

    # `src` SO THE FILTERS BELOW HAVE A POPULATION. Against `printer.py` alone
    # both of them remove nothing -- measured, 0 nodes and 0 names -- so each
    # could be deleted with the suite green, and one of them shipped inverted
    # for exactly that reason. The default is still the real module.
    if src is None:
        src = (Path(__file__).resolve().parent.parent
               / "src" / "claude_swap" / "printer.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    # REACHABLE AS `printer.<name>(...)` IS A MODULE-LEVEL BINDING, not a
    # nesting depth. Keyed on nesting this exported `_make_banner`, which
    # prints nothing, and missed the `banner` that factory returns, which
    # does -- the filter inverted on the one shape it was reasoning about.
    # A def inside a module-level `if`/`try` binds here; one inside a class
    # or another function does not, unless a module-level name of its own
    # spelling is bound to it. Over-reporting costs a loud false alarm in the
    # matcher; under-reporting is silent.
    def _names(value):
        """Function names this module-level value can be reaching."""
        if isinstance(value, ast.Name):
            return {value.id}
        if isinstance(value, ast.Call):
            out = _names(value.func)
            # ONLY THE CALLABLE `partial` IS GIVEN, not every argument. In
            # `partial(f, *bound)` the rest are data, so following them
            # promotes a target on a name that is merely bound to it.
            if _is_partial(value.func):
                for a in (value.args[:1]
                          + [k.value for k in value.keywords
                             if k.arg == "func"]):
                    out |= _names(a)
            return out
        # A LAMBDA REACHES WHATEVER ITS BODY CALLS. `banner = lambda m:
        # warning(m)` is a printer under a name a `Name`/`Call` match cannot
        # see, and missing it is the silent direction.
        if isinstance(value, ast.Lambda):
            return {n.func.id for n in ast.walk(value.body)
                    if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Name)}
        return set()

    # THE SOURCE'S OWN NAMES, not the literal `partial` -- the same rule
    # `_print_only_offenders` states for the printer module a hundred lines
    # below. A literal misses `from functools import partial as pt` and
    # `p2 = partial` (silent), and fires on an unrelated `c.partial(...)`
    # and on a local `def partial` that shadows the import (loud).
    _partial_names = {"functools.partial"}
    for _n in ast.walk(tree):
        if isinstance(_n, ast.ImportFrom) and _n.module == "functools":
            _partial_names |= {a.asname or a.name for a in _n.names
                               if a.name == "partial"}
        elif isinstance(_n, ast.Import):
            _partial_names |= {f"{a.asname or a.name}.partial"
                               for a in _n.names if a.name == "functools"}
    _shadowed_partial = {
        f.name for f in _own_scope(tree)
        if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
    } | {
        t.id for n in _own_scope(tree) if isinstance(n, ast.Assign)
        for t in n.targets if isinstance(t, ast.Name)
    }

    def _is_partial(f) -> bool:
        if isinstance(f, ast.Name):
            return f.id in _partial_names and f.id not in _shadowed_partial
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
            return f"{f.value.id}.{f.attr}" in _partial_names
        return False

    exported: set[str] = set()
    bindings: dict[str, set[str]] = {}
    for n in _own_scope(tree):
        # A RE-EXPORT IS A BINDING. `from ._impl import warning` makes it
        # reachable as `printer.warning`, and `ImportFrom` was not a binder
        # here at all -- the whole name derived nothing.
        if isinstance(n, ast.ImportFrom):
            exported |= {a.asname or a.name for a in n.names}
            for a in n.names:
                if a.asname:
                    bindings.setdefault(a.asname, set()).add(a.name)
            continue
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            exported.add(n.name)
            continue
        targets, value = [], None
        if isinstance(n, ast.Assign):
            targets = [t.id for t in n.targets if isinstance(t, ast.Name)]
            value = n.value
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            targets, value = [n.target.id], n.value
        exported |= set(targets)
        for t in targets:
            # A SET, NOT THE LAST ONE. `_names` returns a set, so assigning
            # per element kept whichever came last in ITERATION order -- i.e.
            # PYTHONHASHSEED. Measured: `banner = partial(_emit, 'x')` derived
            # the printer on 2 of 6 seeds and lost it on 4.
            bindings.setdefault(t, set()).update(_names(value))
    funcs = [n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
             and n.name in exported]
    # `ast.walk`, not `tree.body`: a printer defined inside an `if` or a
    # `try` is still a printer -- one inside a class is NOT, and the filter
    # above drops it, because it is not reachable as `printer.<name>(...)`. And a FIXPOINT, because a printer that
    # DELEGATES to one reaches `print` just as surely -- a new `notice()`
    # that calls `warning()` dies in the same screen blank, and the top-level
    # scan alone reports it as safe.
    found: set[str] = set()
    while True:
        grown = set()
        for f in funcs:
            # A NAME THE FUNCTION DEFINES ITSELF IS NOT THE PRINTER. A local
            # `def warning(x): return x` shadows the module one, prints
            # nothing, and would otherwise promote its enclosing function --
            # which then marks every unrelated call of that name elsewhere.
            # `ast.walk` HERE WAS THE BUG. It descends into nested CLASS
            # bodies and doubly-nested defs, and neither binds anything in
            # this function -- so an unrelated `_Fmt.print` suppressed a real
            # `print(...)` and the printer went unexported. Only a def in
            # this function's OWN scope shadows the module one.
            # EVERY BINDER, not only `def`. A walrus, a `for` target, a
            # `with ... as`, an `except ... as`, an `import ... as` and a
            # plain assignment all rebind the name just as a nested `def`
            # does, and the comment here used to say only a `def` could --
            # a false statement about Python, and seven shapes reported as
            # printers for a call to a name they had rebound.
            shadowed = set()
            for n in _own_scope(f):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    shadowed.add(n.name)
                elif isinstance(n, (ast.Import, ast.ImportFrom)):
                    shadowed |= {(a.asname or a.name).split(".")[0]
                                 for a in n.names}
                elif isinstance(n, ast.ExceptHandler) and n.name:
                    shadowed.add(n.name)
                else:
                    tgts = []
                    if isinstance(n, ast.Assign):
                        tgts = n.targets
                    elif isinstance(n, (ast.AnnAssign, ast.AugAssign)):
                        tgts = [n.target]
                    elif isinstance(n, ast.NamedExpr):
                        tgts = [n.target]
                    elif isinstance(n, ast.For):
                        tgts = [n.target]
                    elif isinstance(n, ast.withitem) and n.optional_vars:
                        tgts = [n.optional_vars]
                    for t in tgts:
                        if isinstance(t, ast.Name):
                            shadowed.add(t.id)
                        elif isinstance(t, (ast.Tuple, ast.List)):
                            shadowed |= {e.id for e in t.elts
                                         if isinstance(e, ast.Name)}
            # THE CALL SCAN STAYS `ast.walk`, DELIBERATELY. Scoping it the
            # way the shadow set is scoped would stop seeing a print inside
            # a nested helper this function calls, and that miss is SILENT.
            # Left wide it over-reports a function whose nested helper prints
            # but is never called -- a loud false alarm in the matcher, which
            # is the direction this guard is allowed to be wrong in.
            if any(isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                   and sub.func.id not in shadowed
                   and (sub.func.id == "print" or sub.func.id in found)
                   for sub in ast.walk(f)):
                grown.add(f.name)
        # THE NAME IT IS EXPORTED UNDER, which need not be the name of the
        # def: `banner = _emit`, `banner = _make_banner()`. Resolved in a
        # SECOND loop after this one, the names it promotes could never be
        # consumed by the delegation scan above, so a printer delegating to
        # an ALIASED printer was silently missed.
        # `print` TOO. `found` holds module functions that REACH the
        # builtin, so it never contains the builtin itself -- and a
        # binding whose only candidate is `print` (`banner = print`,
        # `banner = lambda m: print(m)`) then promoted nothing. That is
        # the most direct printer there is.
        grown |= {t for t, v in bindings.items()
                  if v & (found | grown | {"print"})}
        if grown <= found:
            break
        found |= grown
    # NO FLOOR ON THE POPULATION. An empty answer is what a printer module
    # with no bare `print` gives, and that tree is strictly healthier. Asserted
    # here it fires at IMPORT: measured, rewriting the two printers to
    # `sys.stdout.write` -- a real weakening, still erased by the screen blank
    # -- collected 0 of 199 tests and blamed "the parse or the layout". The
    # matcher keeps its own denominator, and `_PRINTERS` always holds `print`.
    return found


#: Everything in `printer` that ends in a bare `print`, plus the builtin
#: itself. A notice routed through any of these dies in the screen blank
#: exactly as `print` does.
_PRINTERS = {"print"} | _bare_print_printers()

#: Fallback when the source binds the printer module under no name we can
#: see. A LOGGER answers to `.warning`/`.error` too, and five modules here
#: hold a module-level `_logger = logging.getLogger(...)`, so matching any
#: Name base turns the CURE into an offender the moment this module adopts
#: that idiom.
_PRINTER_MODULES = {"printer"}


def _print_only_offenders(src: str) -> tuple[list[int], int]:
    """Lines carrying a print-only notice, and how many printer calls were seen.

    The second number is the DENOMINATOR: a matcher that matches nothing --
    a typo, a renamed helper -- is green for ever without it.

    EVERY PRINTER, not the builtin alone. `printer.warning` IS
    `print(_style(...))` and `printer.error` the same, so a bare `warning(msg)`
    is a print-only notice the screen blank erases. `printer.warning(...)` as
    well as `warning(...)`: an attribute call has no `.id`.

    A NAMED PRINTER MODULE, though. `self._logger.warning(...)` and a
    module-level `_logger.warning(...)` are attribute calls whose `attr` is
    also "warning", and they are the CURE this guards for, not the defect.
    """
    import ast

    tree = ast.parse(src)
    # THE SOURCE'S OWN NAMES, not a literal. `from claude_swap import printer
    # as p` binds the module under `p`, and a literal set misses it -- a miss
    # this guard did not have before the set was introduced.
    bases = set(_PRINTER_MODULES)
    # THE FUNCTIONS TOO, by the same rule. `from claude_swap.printer import
    # warning as print_warning` binds a printer under a name no literal set
    # can hold -- and that is not a hypothetical spelling: `oauth.py` uses it.
    # The module half of this took the source's own names and the function
    # half kept a literal, so the guard read one alias and not the other.
    printers = set(_PRINTERS)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "claude_swap":
            bases |= {a.asname or a.name for a in node.names if a.name == "printer"}
        elif isinstance(node, ast.ImportFrom) and node.module == "claude_swap.printer":
            printers |= {a.asname or a.name for a in node.names
                         if a.name in _PRINTERS}
        elif isinstance(node, ast.Import):
            bases |= {a.asname for a in node.names
                      if a.name == "claude_swap.printer" and a.asname}
    found: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Name):
            called = f.id
        elif (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                and f.value.id in bases):
            called = f.attr
        else:
            continue
        # ARGS OR KEYWORDS. `printer.warning(msg="...")` is a print-only
        # notice with an empty `node.args`, and the filter was inert in both
        # directions: deleting it changed nothing either.
        if called not in printers or not (node.args or node.keywords):
            continue
        found.append((node.lineno, id(node)))
    # `_note` IS the sanctioned print. Excluded by NODE IDENTITY, not by line
    # range, and scoped to CLASS BODIES: `ast.walk` reaches a nested
    # `def _note(m): print(...)` written inside the function under test, so
    # the bypass would otherwise be one line long.
    sanctioned = {
        id(c)
        for cls in ast.walk(tree)
        if isinstance(cls, ast.ClassDef)
        for fn in cls.body
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
        and fn.name in _SANCTIONED_PRINTERS
        for c in ast.walk(fn)
        if isinstance(c, ast.Call)
    }
    return [n for n, node_id in found if node_id not in sanctioned], len(found)


def _notices_before_exec(src: str) -> tuple[list[int], int]:
    """Print-only calls in the statements that lead straight into an exec.

    For every `<x>.exec_default(...)` statement, EVERY statement before it in
    the same block is the notice's whole life on screen -- a branch that
    returns first included. Narrowing to the paths that reach the exec has no
    shape to key on: a `return` marking one is either nested in an `if`, and
    so not a statement of this block, or a statement of it, and so makes the
    exec below unreachable.
    """
    import ast

    tree = ast.parse(src)
    offender_lines = set(_print_only_offenders(src)[0])
    found: list[int] = []
    execs = 0
    # EVERY statement list (body, orelse, finalbody, a handler's or a case's
    # body), not `.body` alone: an exec in an `else:` is a statement of that
    # list, and the first walk never looked there.
    blocks = [b for n in ast.walk(tree) for _, b in ast.iter_fields(n)
              if isinstance(b, list) and b and all(isinstance(s, ast.stmt) for s in b)]
    for body in blocks:
        for i, stmt in enumerate(body):
            call = stmt.value if isinstance(stmt, ast.Expr) else None
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "exec_default"):
                continue
            execs += 1
            found += [c.lineno for prev in body[:i] for c in ast.walk(prev)
                      if isinstance(c, ast.Call) and c.lineno in offender_lines]
    return sorted(set(found)), execs


def _unscanned_exec_sites(root: Path, scanned: set[Path]) -> list[str]:
    """Files under `root`, outside `scanned`, that call `<x>.exec_default(...)`."""
    import ast

    return sorted(
        str(p.relative_to(root))
        for p in root.rglob("*.py")
        if p not in scanned
        and "exec_default" in (src := p.read_text(encoding="utf-8"))
        and any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "exec_default"
                for n in ast.walk(ast.parse(src)))
    )


class TestEveryLaunchNoticeOutlivesTheBlank:
    """The screen blank erases stdout, so a print-only notice reaches nobody.

    This PR converted the success line in `_sync_sharing` to `_note` and left
    two failure notices beside it on the same `run()` path. Measured: with a
    non-quiescent profile the reason is printed and the log holds nothing, so
    `--share-history` silently does not share and the explanation is gone.

    STRUCTURAL, because a behavioural case per site invites the next one to be
    added without one: no bare `print(dimmed(...))` may survive in the module
    whose whole subject is notices that outlive the blank.
    """

    def test_a_styled_line_reaches_the_log_plain(self, manager, caplog):
        """`_plain` on BOTH routes.

        The colour-on launch case reads records the launch path produced, and
        no `_warn` fires there -- measured, dropping `_plain` from `_warn`
        alone left the whole suite green. Every other case runs colour-off,
        where `_plain` is the identity function, so nothing held the warning
        route at all.

        The escape is written literally rather than through `accent`, so the
        premise does not depend on the printer's colour detection.
        """
        import logging

        styled = "\x1b[38;5;173mLaunching\x1b[0m into \x1b[2ma slot\x1b[0m"
        assert "\x1b[" in styled, (
            "premise: the input carries no escape, so a logger that recorded "
            "it verbatim would pass this too"
        )
        with caplog.at_level(logging.INFO, logger="claude-swap"):
            manager._warn(styled)
            manager._note(styled)

        got = [r.getMessage() for r in caplog.records]
        assert len(got) == 2, f"premise: both routes did not record: {got}"
        assert [m for m in got if "\x1b[" in m] == [], (
            "a record carries SGR escapes, so the log the README points a "
            f"user at fills with them: {got}"
        )

    def test_no_launch_notice_is_print_only(self):
        import pathlib

        import claude_swap.session as session_mod

        src = pathlib.Path(session_mod.__file__).read_text(encoding="utf-8")
        offenders, seen = _print_only_offenders(src)
        # THE DERIVED NAMES, NOT THE BUILTIN. `_PRINTERS` always holds
        # `print`, and session.py always has one sanctioned `print`, so
        # `seen` could not reach zero however badly the derivation failed:
        # replacing it wholesale with `{"print"}` left this assert green.
        derived = _bare_print_printers() - {"print"}
        assert derived, (
            "the derivation produced no printer name beyond the builtin, so "
            "every call this matcher could newly catch is invisible to it"
        )
        assert seen, (
            "the walk found no `print` at all in session.py — the matcher is "
            "broken, not the module clean"
        )
        assert offenders == [], (
            "a launch notice is print-only, so the screen blank erases it and "
            f"nothing records why: {[f'session.py:{n}' for n in offenders]}. "
            "Use `_note`."
        )

    def test_no_launch_notice_in_the_cli_is_print_only(self):
        """The CLI's own launch path: a notice printed and then `exec_default`.

        The sibling of the `session.py` defect this PR fixed. The block that
        precedes the exec in the same function is the notice's whole life on
        screen; anything printed there without a log record is gone with the
        blank."""
        import pathlib

        import claude_swap.cli as cli_mod

        src = pathlib.Path(cli_mod.__file__).read_text(encoding="utf-8")
        offenders, execs = _notices_before_exec(src)
        assert execs, "no `exec_default` call found in cli.py — the matcher is blind"
        # AND THE OTHER HALF. `execs` covers finding the exec; this covers
        # finding the notices. It is the exact set the narrowing intersects, so
        # populated, an empty result above is a clean block, not a blind walk.
        printed_anywhere, _ = _print_only_offenders(src)
        assert printed_anywhere, (
            "the print-only matcher reported nothing anywhere in cli.py, so an "
            "empty result above means the matcher is blind, not the block clean"
        )
        assert offenders == [], (
            "a launch notice is print-only in the block before an exec, so the "
            f"screen blank erases it: {[f'cli.py:{n}' for n in offenders]}. "
            "Route it through the manager's `_warn`/`_note`."
        )

    @pytest.mark.parametrize(
        "head",
        [
            "if x:\n        pass\n    else:",
            "try:\n        pass\n    finally:",
            "for _ in x:\n        pass\n    else:",
            "try:\n        pass\n    except OSError:\n        pass\n    else:",
        ],
        ids=["else", "finally", "for-else", "try-else"],
    )
    def test_an_exec_in_any_statement_list_is_seen(self, head):
        """The walk keyed on `.body` alone, so an `exec_default` written as a
        statement of an `else:` / `finally:` list was invisible, and a
        print-only notice ahead of it shipped green."""
        src = f"def f(m, x):\n    {head}\n        print('n')\n        m.exec_default([])\n"
        offenders, execs = _notices_before_exec(src)
        assert execs == 1, f"the exec in this list was not seen: {execs}"
        assert len(offenders) == 1, f"the notice before it was missed: {offenders}"

    def test_every_exec_default_site_in_src_is_scanned(self, tmp_path):
        """A handover added in a third file is otherwise simply unscanned."""
        import claude_swap.cli as cli_mod

        root = Path(session_mod.__file__).parent
        scanned = {Path(session_mod.__file__), Path(cli_mod.__file__)}
        assert _unscanned_exec_sites(root, set()) != [], (
            "premise: no `exec_default` call found under src/, so an empty "
            "result below means the walk is blind, not the tree covered"
        )
        assert _unscanned_exec_sites(root, scanned) == []

        (tmp_path / "scanned.py").write_text("m.exec_default([])\n")
        (tmp_path / "third.py").write_text("def f(m):\n    m.exec_default([])\n")
        assert _unscanned_exec_sites(tmp_path, {tmp_path / "scanned.py"}) == [
            "third.py"
        ]

    def test_the_derivation_does_not_depend_on_the_hash_seed(self):
        """The regression case for the seed defect was itself seed-dependent.

        A two-candidate binding is 50/50 under a last-wins `bindings`, so the
        case written to catch that defect passed on 4 runs in 10 -- and the
        direction that ships is the silent one. Nothing about a source shape
        can fix this: an n-candidate set is 1/n by construction. The seed has
        to be forced, and forcing it needs a subprocess.

        HOME and XDG_DATA_HOME are pointed at a scratch dir so that even an
        import side effect cannot reach the real account store.
        """
        import inspect
        import json
        import subprocess
        import sys
        import tempfile

        src = (
            "from functools import partial\n"
            "def _emit(p, m): print(m)\n"
            "banner = partial(_emit, 'x')\n"
        )
        # THE TWO FUNCTIONS' OWN SOURCE, not the whole module: both already
        # `import ast` themselves, so this needs no other module-level name.
        prog = (
            "import json, sys\n"
            + inspect.getsource(_own_scope) + "\n"
            + inspect.getsource(_bare_print_printers) + "\n"
            "print(json.dumps(sorted(_bare_print_printers(sys.argv[1]))))\n"
        )
        answers = {}
        with tempfile.TemporaryDirectory() as scratch:
            env = {
                **os.environ, "HOME": scratch, "XDG_DATA_HOME": scratch,
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            for seed in ("0", "1", "2", "3", "4", "5"):
                out = subprocess.run(
                    [sys.executable, "-c", prog, src],
                    env={**env, "PYTHONHASHSEED": seed},
                    capture_output=True, text=True, timeout=120,
                )
                assert out.returncode == 0, (
                    f"seed {seed}: the probe did not run, so this case "
                    f"measured nothing: {out.stderr[-400:]}"
                )
                answers[seed] = json.loads(out.stdout.strip().splitlines()[-1])
        distinct = {json.dumps(v) for v in answers.values()}
        assert len(distinct) == 1, (
            "the derived set changes with PYTHONHASHSEED, so the guard's "
            f"answer is a coin flip: {answers}"
        )
        # AND THE RIGHT ANSWER, not merely a stable one. A derivation that
        # returned nothing at all would be perfectly consistent.
        assert "banner" in answers["0"], (
            f"stable but wrong -- the exported printer is absent: {answers['0']}"
        )

    def test_a_printer_delegating_to_an_ALIASED_printer_is_derived(self):
        """Resolving aliases in a loop AFTER the delegation fixpoint misses them.

        Both features shipped together and did not compose: the names the
        alias pass promoted could never re-enter the loop that consumes
        `found`, so `notice` -- which reaches `print` through `warning = _emit`
        -- was absent from `_PRINTERS` and every `notice(...)` in session.py
        went unflagged. Silent, which is the direction that matters.
        """
        found = _bare_print_printers("""
def _emit(m): print(m)
warning = _emit
def notice(m): warning(m)
def deeper(m): notice(m)
""")
        assert {"notice", "deeper"} <= found, (
            "delegation through an alias was not resolved: "
            f"{sorted(found)}"
        )

    def test_a_constant_built_from_a_call_is_not_a_printer(self):
        """Following EVERY call's arguments promotes non-printers without bound.

        A module constant built by a call that merely mentions a printer would
        become a printer, and then anything built from that constant too. The
        cost is not a bounded false alarm: `_PRINTERS` holds a name that prints
        nothing, and the matcher then fails on session.py lines that are not
        notices -- a red suite with a wrong diagnosis.
        """
        found = _bare_print_printers("""
def warning(m): print(m)
def _len(x): return 1
WIDTH = _len(warning)
TABLE = dict(WIDTH)
LABEL = str(TABLE)
""")
        assert found == {"warning"}, (
            f"a non-printer was promoted through a call chain: {sorted(found)}"
        )

    def test_a_factory_exported_printer_is_not_filtered_out(self):
        """The nesting filter dropped the reachable name and kept the unreachable
        one.

        Its question is "is this reachable as `printer.<name>(...)`", and it
        answered it by NESTING -- so `banner = _make_banner()` exported
        `_make_banner`, which prints nothing, and missed `banner`, which does.
        Reachability is a module-level BINDING, which is what it keys on now.
        Over-reporting a name only costs a loud false alarm in the matcher;
        under-reporting is silent, and silent is the failure this guard exists
        to prevent.
        """
        missed = []
        for label, src in (
            # THE INNER DEF IS RENAMED, so a match on `.name` cannot find it.
            # Spelled `banner` this case passes by coincidence, which is how
            # the first cut shipped resolving nothing.
            ("factory", """
def _make_banner():
    def _inner(msg): print(msg)
    return _inner
banner = _make_banner()
"""),
            ("plain alias", """
def _emit(m): print(m)
banner = _emit
"""),
            ("partial", """
import functools
def _emit(p, m): print(m)
banner = functools.partial(_emit, 'x')
"""),
            # SPELLED BARE, so the candidate set is {'partial', '_emit'} and
            # not the single name an `Attribute` callee leaves. Keeping one
            # candidate per target answered this by PYTHONHASHSEED -- measured
            # on the shipped code, derived on 2 of 6 seeds and lost on 4.
            ("bare partial", """
from functools import partial
def _emit(p, m): print(m)
banner = partial(_emit, 'x')
"""),
            # AND THE KEYWORD FORM, which the argument scan did not read.
            ("partial by keyword", """
from functools import partial
def _emit(m): print(m)
banner = partial(func=_emit)
"""),
            # THE MOST DIRECT PRINTER THERE IS, and it derived nothing: the
            # promotion tests membership in `found`, which never holds the
            # builtin.
            ("lambda straight to print", """
banner = lambda m: print(m)
"""),
            ("plain alias of the builtin", """
banner = print
"""),
            # `partial` UNDER AN IMPORT ALIAS. Matched by the literal
            # spelling this was a silent miss.
            ("partial imported as another name", """
from functools import partial as pt
def _emit(p, m): print(m)
banner = pt(_emit, 'x')
"""),
            ("functools under an alias", """
import functools as ft
def _emit(p, m): print(m)
banner = ft.partial(_emit, 'x')
"""),
        ):
            # COLLECTED, NOT ASSERTED PER ROW. Asserting inside the loop
            # stops at the first failure, so a mutation that kills rows 2-5
            # is reported as a row-1 failure and the other four never run.
            if "banner" not in _bare_print_printers(src):
                missed.append(label)
        assert not missed, (
            f"the exported printer was not derived for: {missed}"
        )

    @pytest.mark.parametrize("shape,body,expected", [
        (
            "a nested class method of the same name",
            """
def print_helper():
    class _Fmt:
        def print(self): pass
    print("x")
""",
            {"print_helper"},
        ),
        (
            "a doubly-nested def of the same name",
            """
def banner(msg):
    def outer():
        def print(x): return x
    print(msg)
""",
            {"banner"},
        ),
        (
            "a genuine same-scope shadow",
            """
def banner(msg):
    def print(x): return x
    print(msg)
""",
            set(),
        ),
        (
            "a walrus rebinding the name",
            """
def banner(msg):
    (print := (lambda x: x))
    print(msg)
""",
            set(),
        ),
        (
            "an `except ... as` rebinding it",
            """
def banner(msg):
    try: pass
    except Exception as print: pass
    print(msg)
""",
            set(),
        ),
        (
            "a plain assignment rebinding it",
            """
def banner(msg):
    print = str
    print(msg)
""",
            set(),
        ),
        (
            "a comprehension, which has its OWN scope and shadows nothing",
            """
def banner(msg):
    [print for print in []]
    print(msg)
""",
            {"banner"},
        ),
    ])
    def test_a_shadow_only_counts_in_the_scope_that_binds_it(
        self, shape, body, expected
    ):
        """Source of its own, because `printer.py` gives the filters NOTHING.

        Measured on the real module: the nested-def filter removes 0 nodes and
        the shadow filter removes 0 names, so either could be deleted with the
        suite green -- and one of them shipped over-broad for exactly that
        reason. `shadowed` was collected with `ast.walk`, which descends into
        nested CLASS bodies and doubly-nested defs. Neither binds anything in
        the enclosing function, so an unrelated `_Fmt.print` suppressed a real
        `print(...)` and the printer went unexported -- the launch notice it
        carries then reads as safe. Only the third case is a shadow Python
        agrees with.
        """
        assert _bare_print_printers(body) == expected, shape

    def test_the_matcher_sees_both_call_shapes_and_spares_the_cure(self):
        """Source of its own, because session.py exercises ONE of the halves.

        The module has no `printer.warning(...)` and eleven
        `self._logger.warning(...)`, so the attribute branch never fires on
        it: deleting that branch, or widening it to any `attr`, is invisible
        from the real file. Handing the matcher each shape is what makes both
        halves answerable.
        """
        cases = [
            ("the builtin", "print('x')", True),
            ("a bare printer name", "warning('x')", True),
            ("a module alias", "printer.warning('x')", True),
            ("printer.error too", "printer.error('x')", True),
            ("a keyword-only notice", "printer.warning(msg='x')", True),
            ("a blank-line separator", "print()", False),
            ("an ALIASED printer module",
             "import x\nfrom claude_swap import printer as p\np.warning('x')",
             True),
            # NOT A HYPOTHETICAL SPELLING: `oauth.py` imports the printer this
            # way today, so a launch notice copied from it was invisible here.
            ("an ALIASED printer FUNCTION",
             "from claude_swap.printer import warning as print_warning\n"
             "print_warning('x')",
             True),
            ("...and an alias of something that is not a printer",
             "from claude_swap.printer import accent as a\na('x')",
             False),
            ("the logger, which is the CURE", "self._logger.warning('x')", False),
            ("a MODULE-LEVEL logger, the sibling modules' idiom",
             "_logger.warning('x')", False),
            ("a sanctioned printer", "self._warn('x')", False),
        ]
        for label, expr, flagged in cases:
            src = f"class C:\n    def f(self):\n        {expr}\n"
            offenders, seen = _print_only_offenders(src)
            assert bool(offenders) is flagged, (
                f"{label}: `{expr}` was "
                f"{'missed' if flagged else 'flagged'} by the matcher "
                f"(offenders={offenders}, printer calls seen={seen})"
            )

    def test_a_sanctioned_printers_own_print_is_not_an_offender(self):
        """The exclusion is by node identity, and it must survive a rename."""
        src = ("class C:\n"
               "    def _note(self, m):\n"
               "        print(m)\n"
               "    def other(self, m):\n"
               "        print(m)\n")
        offenders, seen = _print_only_offenders(src)
        assert seen == 2, f"premise: the matcher saw {seen} prints, not 2"
        assert offenders == [5], (
            f"only `other`'s print is an offender; got {offenders}"
        )

    def test_a_nested_sanctioned_name_does_not_sanction_its_own_print(self):
        """The exclusion is scoped to CLASS BODIES, and that is load-bearing.

        `ast.walk` reaches a `def _note(...)` written INSIDE the function
        under test, so an unscoped walk lets one line sanction itself. Nothing
        held that: replacing the class-body walk with a bare one over the tree
        left every case green.
        """
        src = ("class C:\n"
               "    def run(self, m):\n"
               "        def _note(x):\n"
               "            print(x)\n"
               "        _note(m)\n")
        offenders, seen = _print_only_offenders(src)
        assert seen == 1, f"premise: the matcher saw {seen} prints, not 1"
        assert offenders == [4], (
            "a `_note` nested inside a method sanctioned its own print, so "
            f"the bypass is one line long: {offenders}"
        )


def test_manifest_interrupted_mid_write_leaves_no_temp(tmp_path: Path, monkeypatch):
    """The manifest writer swallows OSError on purpose (best effort), but a
    Ctrl-C must still propagate and must not strand the temp."""
    from types import SimpleNamespace

    from claude_swap.session import SessionManager

    mgr = SessionManager.__new__(SessionManager)
    mgr.switcher = SimpleNamespace(platform=object())  # anything but WINDOWS

    def interrupt(fd):
        raise KeyboardInterrupt

    monkeypatch.setattr("claude_swap.fsutil.os.fsync", interrupt)
    with pytest.raises(KeyboardInterrupt):
        mgr._write_manifest(tmp_path / ".cswap-shared.json", ["a"])
    assert not list(tmp_path.glob("*.tmp"))


class TestSurvivorSelection:
    """Which of two same-named transcripts wins a merge collision."""

    def _write(self, path: Path, lines: list[str]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f"{line}\n" for line in lines))
        return path

    def test_last_timestamp_scans_past_untimestamped_trailer(self, tmp_path):
        # ~99% of real transcripts end on an untimestamped state line.
        f = self._write(tmp_path / "a.jsonl", [
            '{"type": "user", "timestamp": "2026-07-22T01:33:08.788Z"}',
            '{"type": "last-prompt", "prompt": "hi"}',
        ])
        assert session_mod._last_timestamp(f) == "2026-07-22T01:33:08.788Z"

    def test_last_timestamp_none_when_absent(self, tmp_path):
        f = self._write(tmp_path / "a.jsonl", ['{"type": "last-prompt"}'])
        assert session_mod._last_timestamp(f) is None

    def test_last_timestamp_skips_unparseable_lines(self, tmp_path):
        f = self._write(tmp_path / "a.jsonl", [
            '{"timestamp": "2026-07-01T00:00:00.000Z"}',
            "not json at all",
        ])
        assert session_mod._last_timestamp(f) == "2026-07-01T00:00:00.000Z"

    def test_later_timestamp_wins(self, tmp_path):
        target = self._write(tmp_path / "t.jsonl", [
            '{"timestamp": "2026-07-21T20:39:55.399Z"}'] * 5)
        cand = self._write(tmp_path / "c.jsonl", [
            '{"timestamp": "2026-07-23T18:03:53.827Z"}'])
        # Shorter but newer still wins: a compacted continuation must not lose.
        assert session_mod._profile_copy_wins(target, cand) is True

    def test_earlier_timestamp_loses(self, tmp_path):
        target = self._write(tmp_path / "t.jsonl", [
            '{"timestamp": "2026-07-23T18:03:53.827Z"}'])
        cand = self._write(tmp_path / "c.jsonl", [
            '{"timestamp": "2026-07-21T20:39:55.399Z"}'] * 99)
        assert session_mod._profile_copy_wins(target, cand) is False

    def test_untimestamped_loses_to_timestamped(self, tmp_path):
        target = self._write(tmp_path / "t.jsonl", [
            '{"timestamp": "2026-07-01T00:00:00.000Z"}'])
        cand = self._write(tmp_path / "c.jsonl", ['{"type": "mode"}'] * 50)
        assert session_mod._profile_copy_wins(target, cand) is False

    def test_neither_timestamped_falls_back_to_line_count(self, tmp_path):
        target = self._write(tmp_path / "t.jsonl", ["# a"])
        cand = self._write(tmp_path / "c.jsonl", ["# a", "# b"])
        assert session_mod._profile_copy_wins(target, cand) is True

    def test_exact_tie_keeps_the_target(self, tmp_path):
        target = self._write(tmp_path / "t.jsonl", ['{"timestamp": "2026-07-01T00:00:00.000Z"}'])
        cand = self._write(tmp_path / "c.jsonl", ['{"timestamp": "2026-07-01T00:00:00.000Z"}'])
        assert session_mod._profile_copy_wins(target, cand) is False


# ---------------------------------------------------------------------------
# _default_login_signed_out
# ---------------------------------------------------------------------------


class TestDefaultLoginSignedOut:
    """What the same-account fast path reads before it runs plain claude."""

    @pytest.mark.parametrize(
        ("credential", "signed_out"),
        [
            (None, True),  # no credential anywhere
            ("", True),
            # Claude Code's reaction to invalid_grant: both tokens emptied.
            ('{"claudeAiOauth": {"accessToken": "", "refreshToken": ""}}', True),
            ('{"claudeAiOauth": {"accessToken": "a", "expiresAt": 1}}', True),
            ('{"claudeAiOauth": {"accessToken": "a", "expiresAt": 9999999999999}}', False),
            ('{"claudeAiOauth": {"accessToken": "a"}}', False),
            (CREDS, False),
            ('{"somethingElse": {}}', False),  # unknown shape: keep the fast path
        ],
    )
    def test_reads_the_default_login(
        self, manager, temp_home, real_default_login_check, credential, signed_out
    ):
        if credential is not None:
            (temp_home / ".claude" / ".credentials.json").write_text(credential)

        assert manager._default_login_signed_out() is signed_out

    def test_reads_the_default_profile_not_the_preset_one(
        self, manager, temp_home, tmp_path, monkeypatch, real_default_login_check
    ):
        preset = tmp_path / "preset-profile"
        preset.mkdir()
        (preset / ".credentials.json").write_text(CREDS)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(preset))

        assert manager._default_login_signed_out() is True
        assert os.environ["CLAUDE_CONFIG_DIR"] == str(preset)

    def test_an_unreadable_keychain_keeps_the_fast_path(
        self, manager, monkeypatch, real_default_login_check
    ):
        from claude_swap.credentials import ActiveCredentials

        monkeypatch.setattr(
            manager.switcher,
            "_read_active_credentials",
            lambda: ActiveCredentials(None, True, True),
        )
        assert manager._default_login_signed_out() is False

    def test_a_redirected_credential_store_keeps_the_fast_path(
        self, manager, tmp_path, monkeypatch, real_default_login_check
    ):
        # claude reads its plaintext credential from the override directory.
        # The default profile holds none, and that must not read as signed
        # out while the login claude would use works.
        redirected = tmp_path / "redirected-store"
        redirected.mkdir()
        (redirected / ".credentials.json").write_text(CREDS)
        monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", str(redirected))

        assert manager._default_login_signed_out() is False

    def test_an_empty_override_still_reads_the_default_store(
        self, manager, monkeypatch, real_default_login_check
    ):
        # Defined but empty selects the default credential store, which is
        # the one this check reads, so its answer stands.
        monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", "")

        assert manager._default_login_signed_out() is True
