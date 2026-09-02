"""Tests for session mode (claude_swap.session + the switcher guards)."""

from __future__ import annotations

import hashlib
import json
import logging
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
    SwitchError,
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
            # NO PRODUCER ON THIS BRANCH -- `_classify_usage_error` answers
            # `http-<code>`/`timeout`/`network`/`bad-response`, and the kind
            # is added for the merged tree. Witnessed here anyway: without a
            # reader, a sibling branch renaming it is a silent un-wiring, and
            # this file has already had one.
            ("tls-cert", "certificate chain was not trusted"),
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
            manager.setup_session("2", share=False)

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
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            continue
        for i, stmt in enumerate(body):
            call = stmt.value if isinstance(stmt, ast.Expr) else None
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "exec_default"):
                continue
            execs += 1
            found += [c.lineno for prev in body[:i] for c in ast.walk(prev)
                      if isinstance(c, ast.Call) and c.lineno in offender_lines]
    return sorted(set(found)), execs


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
        import json
        import pathlib
        import subprocess
        import sys
        import tempfile

        src = (
            "from functools import partial\n"
            "def _emit(p, m): print(m)\n"
            "banner = partial(_emit, 'x')\n"
        )
        here = pathlib.Path(__file__).resolve()
        prog = (
            "import importlib.util, json, sys\n"
            f"spec = importlib.util.spec_from_file_location('t', {str(here)!r})\n"
            "m = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(m)\n"
            "print(json.dumps(sorted(m._bare_print_printers(sys.argv[1]))))\n"
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
