"""``cswap codex run N``: Codex session mode (``CodexSessionManager``).

A session runs ``codex -c cli_auth_credentials_store="file"`` with
``CODEX_HOME`` pointing at ``<root>/codex/sessions/<num>-<slug>/``, seeded
with the slot's auth.json. Exec, refresh and process scans are all faked:
no real ``codex`` is launched and no ``ps`` runs.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_swap import codex_auth, codex_session, process_detection
from claude_swap import session as session_mod
from claude_swap.codex_session import CodexSessionManager
from claude_swap.codex_switcher import CodexAccountSwitcher
from claude_swap.exceptions import SessionError, SwitchError
from claude_swap.models import Platform
from claude_swap.oauth import RefreshOutcome
from claude_swap.process_detection import CodexProcess
from claude_swap.session import SHARE_MANIFEST, SessionManager, stale_marker_for
from claude_swap.switcher import ClaudeAccountSwitcher
from tests import codex_fixtures as cf

PERSONAL = dict(email="user@example.com", account_id="acct-personal", user_id="user-1", plan="plus")
TEAM = dict(email="user@example.com", account_id="acct-team", user_id="user-1", plan="team")
OTHER = dict(email="other@example.com", account_id="acct-other", user_id="user-2", plan="pro")
FILE_STORE = 'cli_auth_credentials_store="file"'

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks/modes")


class _Exec(Exception):
    """Raised by the fake exec: a real one never returns."""


def _switcher() -> CodexAccountSwitcher:
    s = CodexAccountSwitcher()
    s.platform = Platform.LINUX
    return s


def _add(s: CodexAccountSwitcher, codex_home: Path, **kw) -> str:
    text = cf.auth_json(**kw)
    (codex_home / "auth.json").write_text(text, encoding="utf-8")
    s.add_account()
    return text


def _profile(s: CodexAccountSwitcher, num: str = "1", email: str = "other@example.com") -> Path:
    return s.backup_dir / "sessions" / f"{num}-{email.replace('@', '_')}"


@pytest.fixture
def seeded(codex_home: Path, monkeypatch) -> CodexAccountSwitcher:
    """Slot 1 = other@ (the session target), slot 2 = user@ (the live login).

    The pre-launch refresh is a no-op success (one test drives the real
    gate), and the run record's start stamp is fixed so no ``ps`` runs.
    """
    s = _switcher()
    _add(s, codex_home, **OTHER)
    _add(s, codex_home, **PERSONAL)
    s.gate_calls = []
    monkeypatch.setattr(
        s, "consume_backup_grant",
        lambda *a: s.gate_calls.append(a) or RefreshOutcome(None, None),
    )
    monkeypatch.setattr(process_detection, "proc_start_stamp", lambda pid: "STAMP")
    return s


@pytest.fixture
def execs(monkeypatch) -> list[SimpleNamespace]:
    """``codex`` on PATH, and every exec recorded then aborted."""
    calls: list[SimpleNamespace] = []

    def fake_exec(self, binary, args, env):
        calls.append(SimpleNamespace(binary=binary, args=list(args), env=dict(env)))
        raise _Exec

    monkeypatch.setattr(
        session_mod.shutil, "which",
        lambda name: "/opt/bin/codex" if name == "codex" else None,
    )
    monkeypatch.setattr(SessionManager, "_exec", fake_exec)
    return calls


def _run(s, *args, **kw) -> None:
    with pytest.raises(_Exec):
        s.make_session_manager().run(*args, **kw)


class TestSelection:
    def test_codex_switcher_builds_the_codex_manager(self, seeded):
        mgr = seeded.make_session_manager()
        assert isinstance(mgr, CodexSessionManager)
        assert mgr.sessions_dir == seeded.root_dir / "codex" / "sessions"

    def test_claude_switcher_builds_the_patchable_claude_manager(self, temp_home, monkeypatch):
        s = ClaudeAccountSwitcher()
        assert type(s.make_session_manager()) is SessionManager

        class Fake:
            def __init__(self, switcher):
                self.switcher = switcher

        monkeypatch.setattr("claude_swap.session.SessionManager", Fake)
        assert isinstance(s.make_session_manager(), Fake)


class TestRun:
    def test_launches_codex_in_the_profile_with_the_file_store_forced(self, seeded, execs):
        _run(seeded, "1", ["resume", "--last"])

        (call,) = execs
        profile = _profile(seeded)
        assert call.binary == "/opt/bin/codex"
        assert call.args == ["-c", FILE_STORE, "resume", "--last"]
        assert call.env["CODEX_HOME"] == str(profile)
        assert call.env["CSWAP_CODEX_DEFAULT_HOME"] == ""  # the implicit ~/.codex
        assert profile.parent.parent == seeded.root_dir / "codex"
        assert len(seeded.gate_calls) == 1  # one pre-launch refresh, as for Claude

    def test_auth_overrides_are_scrubbed_with_a_warning(self, seeded, execs, monkeypatch, capsys):
        monkeypatch.setenv("CODEX_API_KEY", "sk-env")
        monkeypatch.setenv("CODEX_ACCESS_TOKEN", "pat-env")
        monkeypatch.setenv("CODEX_SQLITE_HOME", "/elsewhere")

        _run(seeded, "1", [])

        env = execs[0].env
        assert "CODEX_API_KEY" not in env
        assert "CODEX_ACCESS_TOKEN" not in env
        # Without --share-history the profile keeps its own threads DB.
        assert "CODEX_SQLITE_HOME" not in env
        out = capsys.readouterr().out
        assert "CODEX_API_KEY" in out and "CODEX_ACCESS_TOKEN" in out

    @posix_only
    def test_profile_is_seeded_from_the_backup_0600(self, seeded, execs):
        _run(seeded, "1", [])

        profile = _profile(seeded)
        auth = profile / "auth.json"
        assert json.loads(auth.read_text()) == json.loads(
            seeded.read_account_credentials("1", "other@example.com")
        )
        assert stat.S_IMODE(auth.stat().st_mode) == 0o600
        assert stat.S_IMODE(profile.stat().st_mode) == 0o700
        # Codex's MCP OAuth file is not ours to seed.
        assert not (profile / ".credentials.json").exists()

    def test_run_record_is_written_before_exec(self, seeded, execs):
        _run(seeded, "1", [])

        record = _profile(seeded) / ".cswap-run" / f"{os.getpid()}.json"
        assert json.loads(record.read_text()) == {"pid": os.getpid(), "procStart": "STAMP"}

    def test_same_account_runs_plain_codex_on_the_default_login(self, seeded, execs):
        _run(seeded, "2", ["exec", "hi"])

        (call,) = execs
        assert call.args == ["exec", "hi"]  # no -c: this is the default login
        assert call.env == dict(os.environ)
        assert not _profile(seeded, "2", "user@example.com").exists()

    def test_require_session_refuses_the_same_account(self, seeded, execs):
        with pytest.raises(SessionError, match="active default login"):
            seeded.make_session_manager().run("2", [], require_session=True)
        assert execs == []

    def test_custom_codex_home_is_the_default_login(self, seeded, execs, codex_home, monkeypatch):
        # An exported CODEX_HOME outside the session profiles is the user's
        # real home: a second profile of its live account would fork the
        # rotating refresh token, so the fast path still applies.
        custom = codex_home.parent / "custom-codex"
        custom.mkdir()
        (custom / "auth.json").write_text(cf.auth_json(**OTHER), encoding="utf-8")
        monkeypatch.setenv("CODEX_HOME", str(custom))

        _run(seeded, "1", [])

        assert execs[0].args == []

    def test_nested_run_uses_the_default_home(self, seeded, execs, monkeypatch, capsys):
        # Inside slot 1's session (codex's shell commands inherit CODEX_HOME),
        # slot 2 is still the default ~/.codex login: run plain codex there.
        inside = _profile(seeded)
        inside.mkdir(parents=True)
        monkeypatch.setenv("CODEX_HOME", str(inside))

        _run(seeded, "2", [])

        assert execs[0].args == []
        assert "CODEX_HOME" not in execs[0].env
        assert "points at a session profile" in capsys.readouterr().out
        assert seeded.gate_calls == []

    def test_nested_run_never_spends_or_copies_the_default_login(
        self, codex_home, execs, monkeypatch
    ):
        """The real gate: inside slot 1's profile, `run 2` (slot 2 = the
        default login) must neither POST its refresh token nor seed a
        profile with a second copy of it."""
        s = _switcher()
        _add(s, codex_home, **OTHER)
        live = _add(s, codex_home, **PERSONAL, access_exp=int(time.time()) - 60)
        inside = _profile(s)
        inside.mkdir(parents=True)
        (inside / "auth.json").write_text(cf.auth_json(**OTHER))
        monkeypatch.setenv("CODEX_HOME", str(inside))
        posted: list[str] = []
        monkeypatch.setattr(
            codex_auth, "try_refresh",
            lambda creds, timeout_s=10.0: posted.append(creds) or RefreshOutcome(None, "transient"),
        )

        _run(s, "2", [])

        assert posted == []
        assert not _profile(s, "2", "user@example.com").exists()
        assert (codex_home / "auth.json").read_text() == live

    def test_nested_run_restores_a_custom_default_home(self, codex_home, execs, monkeypatch):
        """The user's default home is an exported CODEX_HOME. A session
        launch replaces it with the profile, so a nested run must restore it
        — not fall back to ~/.codex, which here holds another login."""
        custom = codex_home.parent / "custom-codex"
        custom.mkdir()
        monkeypatch.setenv("CODEX_HOME", str(custom))
        s = _switcher()
        _add(s, custom, **OTHER)
        live = _add(s, custom, **PERSONAL, access_exp=int(time.time()) - 60)
        (codex_home / "auth.json").write_text(cf.auth_json(**OTHER))  # stale ~/.codex
        monkeypatch.setattr(process_detection, "proc_start_stamp", lambda pid: "STAMP")
        posted: list[str] = []
        monkeypatch.setattr(
            codex_auth, "try_refresh",
            lambda creds, timeout_s=10.0: posted.append(creds) or RefreshOutcome(None, "transient"),
        )
        _run(s, "1", [])  # the session whose shell we then run inside
        session_env = execs[0].env
        assert session_env["CSWAP_CODEX_DEFAULT_HOME"] == str(custom)
        posted.clear()
        monkeypatch.setenv("CODEX_HOME", session_env["CODEX_HOME"])
        monkeypatch.setenv("CSWAP_CODEX_DEFAULT_HOME", session_env["CSWAP_CODEX_DEFAULT_HOME"])

        _run(s, "2", [])  # slot 2 is the login in the custom home

        nested = execs[1]
        assert nested.args == []  # plain codex: the fast path, against `custom`
        assert nested.env["CODEX_HOME"] == str(custom)
        assert "CSWAP_CODEX_DEFAULT_HOME" not in nested.env
        assert posted == []
        assert not _profile(s, "2", "user@example.com").exists()
        assert (custom / "auth.json").read_text() == live

    def test_a_default_store_cswap_cannot_read_still_runs_a_session(
        self, codex_home, execs, monkeypatch, capsys
    ):
        s = _switcher()
        _add(s, codex_home, **OTHER)
        _add(s, codex_home, **PERSONAL)
        monkeypatch.setattr(process_detection, "proc_start_stamp", lambda pid: "STAMP")
        (codex_home / "config.toml").write_text('cli_auth_credentials_store = "ephemeral"\n')
        posted: list[str] = []
        monkeypatch.setattr(
            codex_auth, "try_refresh",
            lambda creds, timeout_s=10.0: posted.append(creds) or RefreshOutcome(None, "transient"),
        )

        _run(s, "2", [])  # may be the default login: unknowable here

        assert execs[0].args[:2] == ["-c", FILE_STORE]
        assert execs[0].env["CODEX_HOME"] == str(_profile(s, "2", "user@example.com"))
        assert posted == []  # the gate declines while the live login is unreadable
        assert "Could not read the default Codex login" in capsys.readouterr().out

    def test_api_key_accounts_are_refused(self, seeded, execs):
        seeded.add_account_from_token("sk-proj-test", email="key@example.com")
        with pytest.raises(SessionError, match="cswap codex run' .*API-key"):
            seeded.make_session_manager().run("3", [])
        assert execs == []

    def test_missing_codex_binary(self, seeded, monkeypatch):
        monkeypatch.setattr(session_mod.shutil, "which", lambda name: None)
        with pytest.raises(SessionError, match="'codex' was not found on PATH"):
            seeded.make_session_manager().run("1", [])

    def test_share_history_is_refused_on_windows(self, seeded, execs):
        seeded.platform = Platform.WINDOWS
        with pytest.raises(SessionError, match="--share-history is not supported on Windows"):
            seeded.make_session_manager().run("1", [], share_history=True)

    def test_exec_default_runs_plain_codex(self, seeded, execs):
        with pytest.raises(_Exec):
            seeded.make_session_manager().exec_default(["--help"])
        assert execs[0].args == ["--help"]
        assert execs[0].env == dict(os.environ)

    def test_pre_launch_refresh_seeds_the_rotated_login(self, codex_home, execs, monkeypatch):
        s = _switcher()
        _add(s, codex_home, **OTHER)
        _add(s, codex_home, **PERSONAL)
        monkeypatch.setattr(process_detection, "proc_start_stamp", lambda pid: "STAMP")
        rotated = cf.auth_json(**OTHER, refresh_token="rt-rotated")
        monkeypatch.setattr(
            codex_auth, "try_refresh", lambda creds, timeout_s=10.0: RefreshOutcome(rotated, None)
        )

        _run(s, "1", [])

        profile_auth = json.loads((_profile(s) / "auth.json").read_text())
        assert profile_auth["tokens"]["refresh_token"] == "rt-rotated"
        backup = json.loads(s.read_account_credentials("1", "other@example.com"))
        assert backup["tokens"]["refresh_token"] == "rt-rotated"


@posix_only
class TestSharing:
    def test_existing_items_are_symlinked_and_recorded(self, seeded, execs, codex_home):
        (codex_home / "config.toml").write_text('model = "o3"\n')
        (codex_home / "AGENTS.md").write_text("be nice\n")
        (codex_home / "skills").mkdir()

        _run(seeded, "1", [])

        profile = _profile(seeded)
        for name in ("config.toml", "AGENTS.md", "skills"):
            assert (profile / name).is_symlink()
            assert (profile / name).readlink() == codex_home / name
        assert not (profile / "AGENTS.override.md").exists()  # absent upstream
        assert not (profile / "auth.json").is_symlink()
        manifest = json.loads((profile / SHARE_MANIFEST).read_text())
        assert sorted(manifest["items"]) == ["AGENTS.md", "config.toml", "skills"]

    def test_no_share_removes_the_links(self, seeded, execs, codex_home):
        (codex_home / "config.toml").write_text("")
        _run(seeded, "1", [])
        _run(seeded, "1", [], share=False)

        profile = _profile(seeded)
        assert not (profile / "config.toml").exists()
        assert not (profile / SHARE_MANIFEST).exists()
        assert (codex_home / "config.toml").exists()

    def test_share_history_links_history_and_shares_the_threads_db(self, seeded, execs, codex_home):
        _run(seeded, "1", [], share_history=True)

        profile = _profile(seeded)
        for name in ("sessions", "archived_sessions", "session_index.jsonl", "history.jsonl"):
            assert (profile / name).is_symlink()
            assert (profile / name).readlink() == codex_home / name
            assert (codex_home / name).exists()  # seeded empty to link to
        assert execs[0].env["CODEX_SQLITE_HOME"] == str(codex_home)

    def test_profile_history_is_merged_before_sharing(self, seeded, execs, codex_home):
        _run(seeded, "1", [])
        profile = _profile(seeded)
        rollout = profile / "sessions" / "2026" / "10" / "08" / "rollout-a.jsonl"
        rollout.parent.mkdir(parents=True)
        rollout.write_text("{}\n")
        (profile / ".cswap-run").rename(profile.parent / "parked")  # that run exited

        _run(seeded, "1", [], share_history=True)

        assert (codex_home / "sessions" / "2026" / "10" / "08" / "rollout-a.jsonl").exists()
        assert (profile / "sessions").is_symlink()

    def test_the_claude_mcp_mirror_never_runs(self, seeded, execs, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("Claude MCP mirror ran for Codex")

        monkeypatch.setattr(SessionManager, "_read_mcp_source", staticmethod(boom))
        monkeypatch.setattr(session_mod, "proper_lockfile", boom)
        _run(seeded, "1", [])
        _run(seeded, "1", [], share=False)

        assert not (_profile(seeded) / session_mod.MCP_MIRROR_MARKER).exists()

    def test_nested_run_shares_from_the_default_home(self, seeded, execs, codex_home, monkeypatch):
        (codex_home / "config.toml").write_text("")
        inside = _profile(seeded, "2", "user@example.com")
        inside.mkdir(parents=True)
        monkeypatch.setenv("CODEX_HOME", str(inside))

        _run(seeded, "1", [], share_history=True)

        assert (_profile(seeded) / "config.toml").readlink() == codex_home / "config.toml"
        assert execs[0].env["CODEX_SQLITE_HOME"] == str(codex_home)


class TestValidity:
    def test_a_valid_profile_is_reused_offline(self, seeded, execs, monkeypatch):
        _run(seeded, "1", [])

        def no_subprocess(*a, **k):
            raise AssertionError("validity must not spawn a process")

        def no_bootstrap(*a, **k):
            raise AssertionError("a valid profile must be reused")

        monkeypatch.setattr(session_mod.subprocess, "run", no_subprocess)
        monkeypatch.setattr(CodexSessionManager, "_bootstrap", no_bootstrap)
        _run(seeded, "1", [])

        assert len(execs) == 2
        assert len(seeded.gate_calls) == 1  # reuse spends no refresh

    # Built lazily: the blobs embed time-based expiries, and xdist workers
    # must collect identical test ids.
    PROFILE_LOGINS = {
        "slot-login": lambda: cf.auth_json(**OTHER),
        "no-last-refresh": lambda: cf.auth_json(**OTHER, last_refresh=None),
        "no-account-id": lambda: cf.auth_json(**OTHER, store_account_id=False),
        "another-account": lambda: cf.auth_json(**PERSONAL),
        "another-workspace": lambda: cf.auth_json(**{**OTHER, "account_id": "acct-team"}),
        "api-key": cf.api_key_json,
        "garbage": lambda: "{not json",
        "missing": lambda: None,
    }

    @pytest.mark.parametrize("login", PROFILE_LOGINS)
    def test_validity_reads_only_the_profile_auth_json(self, seeded, monkeypatch, login):
        monkeypatch.setattr(codex_session.time, "sleep", lambda s: None)
        profile = _profile(seeded)
        profile.mkdir(parents=True)
        auth = self.PROFILE_LOGINS[login]()
        if auth is not None:
            (profile / "auth.json").write_text(auth)
        verdict = "valid" if login == "slot-login" else "invalid"

        mgr = seeded.make_session_manager()
        assert mgr._session_validity(profile, "other@example.com", "acct-other") == verdict

    def test_a_backup_codex_could_not_use_is_refused_before_seeding(self, seeded, execs):
        seeded._write_account_credentials(
            "1", "other@example.com", cf.auth_json(**OTHER, last_refresh=None)
        )
        with pytest.raises(SessionError, match=r"cswap codex add --login --slot 1"):
            seeded.make_session_manager().run("1", [])
        assert not (_profile(seeded) / "auth.json").exists()
        assert execs == []

    def test_a_stale_marker_waits_for_the_live_session_to_exit(
        self, seeded, execs, alive, monkeypatch
    ):
        monkeypatch.setattr(process_detection, "pid_matches_record", lambda pid, stamp: True)
        _run(seeded, "1", [])
        profile = _profile(seeded)
        _record(profile, 42)  # a codex still running in the profile
        alive.add(42)
        stale_marker_for(profile).touch()

        _run(seeded, "1", [])  # joins it

        assert stale_marker_for(profile).exists()  # honored once it exits
        assert len(seeded.gate_calls) == 1  # no re-bootstrap under it

    def test_profile_generation_is_read_from_auth_json(self, seeded):
        profile = _profile(seeded)
        profile.mkdir(parents=True)
        mgr = seeded.make_session_manager()
        (profile / "auth.json").write_text(cf.auth_json(**OTHER, refresh_token="rt-rotated"))
        assert not mgr._profile_matches_backup(profile, "1", "other@example.com")

        (profile / "auth.json").write_text(
            seeded.read_account_credentials("1", "other@example.com")
        )
        assert mgr._profile_matches_backup(profile, "1", "other@example.com")

    def test_failed_cleanup_never_touches_claudes_keychain(self, seeded, monkeypatch):
        calls = []
        monkeypatch.setattr(session_mod, "delete_macos_keychain_entry", calls.append)
        profile = _profile(seeded)
        profile.mkdir(parents=True)

        seeded.make_session_manager()._cleanup_failed_session(profile)

        assert calls == [] and not profile.exists()


def _record(profile: Path, pid: int, text: str | None = None) -> None:
    records = profile / ".cswap-run"
    records.mkdir(parents=True, exist_ok=True)
    (records / f"{pid}.json").write_text(
        text if text is not None else json.dumps({"pid": pid, "procStart": "X"})
    )


DAEMON_NAMES = ("daemon.pid", "app-server.pid")


def _daemon(profile: Path, record: dict | str, name: str = "daemon.pid") -> None:
    d = profile / "app-server-daemon"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(record if isinstance(record, str) else json.dumps(record))


@pytest.fixture
def alive(monkeypatch) -> set[int]:
    """PIDs that ``is_pid_alive`` reports running."""
    pids: set[int] = set()
    monkeypatch.setattr(process_detection, "is_pid_alive", lambda pid: pid in pids)
    return pids


class TestLiveness:
    def test_run_record_of_a_live_matching_pid(self, tmp_path, alive, monkeypatch):
        seen = []
        monkeypatch.setattr(
            process_detection, "pid_matches_record",
            lambda pid, stamp: seen.append((pid, stamp)) or True,
        )
        _record(tmp_path, 123)
        alive.add(123)

        sessions, unreadable = codex_session.scan_live_sessions(tmp_path)

        assert [p.pid for p in sessions] == [123] and unreadable == 0
        assert seen == [(123, "X")]

    def test_run_record_of_a_dead_or_recycled_pid(self, tmp_path, alive, monkeypatch):
        monkeypatch.setattr(process_detection, "pid_matches_record", lambda pid, stamp: pid != 2)
        _record(tmp_path, 1)  # dead
        _record(tmp_path, 2)  # alive, but someone else now
        alive.add(2)

        assert codex_session.scan_live_sessions(tmp_path) == ([], 0)
        assert codex_session.profile_is_quiescent(tmp_path)

    @pytest.mark.parametrize("text", ["{torn", "[]", '{"pid": "7"}', "{}"])
    def test_unreadable_records_fail_closed(self, tmp_path, text):
        _record(tmp_path, 7, text)

        assert codex_session.scan_live_sessions(tmp_path) == ([], 1)
        assert not codex_session.profile_is_quiescent(tmp_path)

    def test_two_runs_on_one_profile_are_both_seen(self, tmp_path, alive, monkeypatch):
        monkeypatch.setattr(process_detection, "pid_matches_record", lambda pid, stamp: True)
        _record(tmp_path, 10)
        _record(tmp_path, 11)
        alive.update({10, 11})

        sessions, _ = codex_session.scan_live_sessions(tmp_path)
        assert sorted(p.pid for p in sessions) == [10, 11]

    def test_writing_a_record_prunes_exited_runs_only(self, tmp_path, alive, monkeypatch):
        monkeypatch.setattr(process_detection, "proc_start_stamp", lambda pid: "S")
        _record(tmp_path, 1)  # exited
        _record(tmp_path, 2)  # still running
        _record(tmp_path, 3, "{torn")  # unreadable: kept, fail closed
        alive.add(2)

        codex_session.write_run_record(tmp_path)

        names = sorted(p.name for p in (tmp_path / ".cswap-run").iterdir())
        assert names == sorted(["2.json", "3.json", f"{os.getpid()}.json"])

    def test_a_record_that_cannot_be_pruned_does_not_abort_the_launch(
        self, tmp_path, alive, monkeypatch
    ):
        monkeypatch.setattr(process_detection, "proc_start_stamp", lambda pid: "S")
        _record(tmp_path, 1)  # an exited run, owned by someone else
        real_unlink = Path.unlink

        def unlink(self, missing_ok=False):
            if self.name == "1.json":
                raise PermissionError(13, "Permission denied", str(self))
            return real_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", unlink)

        codex_session.write_run_record(tmp_path)

        assert (tmp_path / ".cswap-run" / f"{os.getpid()}.json").exists()

    def test_linux_daemon_identity_is_compared_by_start_ticks(self, tmp_path, alive, monkeypatch):
        monkeypatch.setattr(process_detection, "process_start_ticks", lambda pid: "555")
        alive.add(77)
        rec = {"pid": 77, "processStartTime": "x", "processIdentity": {"bootId": "b", "startTicks": 555}}
        _daemon(tmp_path, rec)
        assert [p.pid for p in codex_session.scan_live_sessions(tmp_path)[0]] == [77]

        rec["processIdentity"]["startTicks"] = 556  # pid reused since
        _daemon(tmp_path, rec)
        assert codex_session.scan_live_sessions(tmp_path) == ([], 0)

    def test_macos_daemon_identity_is_compared_by_start_seconds(self, tmp_path, alive, monkeypatch):
        started = {"at": 1_000_000}
        monkeypatch.setattr(process_detection, "process_started_at", lambda pid: started["at"])
        alive.add(77)
        _daemon(tmp_path, {
            "pid": 77, "processStartTime": "x",
            "processIdentity": {"bootId": "b", "uniqueId": 9, "startSeconds": 1_000_000, "startMicroseconds": 5},
        })
        assert [p.pid for p in codex_session.scan_live_sessions(tmp_path)[0]] == [77]

        started["at"] = 1_000_000 + 3600  # a younger process holds the pid now
        assert codex_session.scan_live_sessions(tmp_path) == ([], 0)

    def test_legacy_daemon_record_of_a_live_pid_counts_as_live(self, tmp_path, alive):
        # Its start time is `ps -o lstart` text in the daemon's own locale and
        # time zone, which cannot be compared reliably from here.
        alive.add(78)
        _daemon(tmp_path, {"pid": 78, "processStartTime": "Thu Oct  8 02:02:13 2026"}, "app-server.pid")
        assert [p.pid for p in codex_session.scan_live_sessions(tmp_path)[0]] == [78]

    def test_dead_daemon(self, tmp_path, alive):
        _daemon(tmp_path, {"pid": 79, "processStartTime": "x"})
        assert codex_session.scan_live_sessions(tmp_path) == ([], 0)

    @posix_only
    @pytest.mark.parametrize("name", DAEMON_NAMES)
    def test_empty_daemon_pid_follows_codexs_reservation_lock(self, tmp_path, name):
        import fcntl

        _daemon(tmp_path, "", name)  # reserved by a start, maybe one that died
        assert codex_session.scan_live_sessions(tmp_path) == ([], 0)  # never locked

        lock = tmp_path / "app-server-daemon" / f"{name}.lock"
        lock.touch()
        assert codex_session.scan_live_sessions(tmp_path) == ([], 0)  # start died

        with lock.open("w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)  # a daemon is starting right now
            assert codex_session.scan_live_sessions(tmp_path) == ([], 1)
        assert codex_session.scan_live_sessions(tmp_path) == ([], 0)

    @posix_only
    def test_a_pid_written_before_the_lock_is_taken_counts_as_running(
        self, tmp_path, monkeypatch
    ):
        import fcntl

        _daemon(tmp_path, "")
        pid_file = tmp_path / "app-server-daemon" / "daemon.pid"
        (tmp_path / "app-server-daemon" / "daemon.pid.lock").touch()

        def flock(fd, op):
            # The daemon finishes starting (writes its pid, releases the
            # reservation) between the scan's read and its lock attempt.
            pid_file.write_text(json.dumps({"pid": 77, "processStartTime": "x"}))
            return fcntl.flock(fd, op)

        monkeypatch.setattr(
            codex_session, "fcntl",
            SimpleNamespace(flock=flock, LOCK_EX=fcntl.LOCK_EX, LOCK_NB=fcntl.LOCK_NB),
        )
        assert codex_session.scan_live_sessions(tmp_path) == ([], 1)

    def test_empty_daemon_pid_without_flock_fails_closed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(codex_session, "fcntl", None)  # Windows
        _daemon(tmp_path, " \n")
        assert codex_session.scan_live_sessions(tmp_path) == ([], 1)

    def test_codex_process_running_from_the_profile(self, tmp_path, alive, monkeypatch):
        daemon = CodexProcess(pid=9, args=f"{tmp_path}/packages/app-server-daemon/bin/codex app-server --managed-daemon")
        stranger = CodexProcess(pid=10, args="/usr/local/bin/codex resume")
        monkeypatch.setattr(process_detection, "list_codex_processes", lambda: [daemon, stranger])

        sessions, unreadable = codex_session.scan_live_sessions(tmp_path)

        assert [p.pid for p in sessions] == [9] and unreadable == 0

    def test_missing_profile_is_quiescent(self, tmp_path):
        assert codex_session.scan_live_sessions(tmp_path / "absent") == ([], 0)


class TestSwitcherHooks:
    def test_session_hooks_route_to_the_codex_profile(self, seeded, alive, monkeypatch):
        monkeypatch.setattr(process_detection, "pid_matches_record", lambda pid, stamp: True)
        profile = _profile(seeded)
        profile.mkdir(parents=True)
        login = cf.auth_json(**OTHER)
        (profile / "auth.json").write_text(login)
        (profile / ".credentials.json").write_text('{"mcp": "oauth"}')  # Codex's MCP store

        assert seeded._read_session_credentials(profile) == login
        assert seeded._read_session_identity(profile) == ("other@example.com", "acct-other")
        assert seeded._profile_is_quiescent(profile)
        assert seeded._live_session_pids("1", "other@example.com") == []

        _record(profile, 42)
        alive.add(42)
        assert seeded._live_session_pids("1", "other@example.com") == [42]
        assert not seeded._profile_is_quiescent(profile)

    def test_a_torn_profile_read_is_retried(self, seeded, monkeypatch):
        profile = _profile(seeded)
        profile.mkdir(parents=True)
        login = cf.auth_json(**OTHER)
        (profile / "auth.json").write_text(login[:20])  # Codex mid-write
        monkeypatch.setattr(
            codex_session.time, "sleep", lambda s: (profile / "auth.json").write_text(login)
        )

        assert seeded._read_session_credentials(profile) == login

    def test_in_session_login_to_another_workspace_is_drift(self, seeded):
        profile = _profile(seeded)
        profile.mkdir(parents=True)
        (profile / "auth.json").write_text(cf.auth_json(**{**OTHER, "account_id": "acct-team"}))

        assert seeded._session_identity_drifted(profile, "other@example.com", "acct-other")
        assert not seeded._session_identity_drifted(profile, "other@example.com", "acct-team")

    def test_invalidation_drops_auth_json_and_keeps_the_rest(self, seeded):
        profile = _profile(seeded)
        (profile / "sessions").mkdir(parents=True)
        (profile / "auth.json").write_text(cf.auth_json(**OTHER))
        (profile / ".credentials.json").write_text('{"mcp": "oauth"}')
        stale_marker_for(profile).touch()

        seeded._invalidate_session_credentials("1", "other@example.com")

        assert not (profile / "auth.json").exists()
        assert (profile / ".credentials.json").exists()
        assert (profile / "sessions").is_dir()
        assert not stale_marker_for(profile).exists()

    def test_a_backup_write_invalidates_a_quiescent_profile(self, seeded):
        profile = _profile(seeded)
        profile.mkdir(parents=True)
        (profile / "auth.json").write_text(cf.auth_json(**OTHER))

        seeded._write_account_credentials("1", "other@example.com", cf.auth_json(**OTHER))

        assert not (profile / "auth.json").exists()

    def test_a_backup_write_marks_a_live_profile_stale(self, seeded, alive, monkeypatch):
        monkeypatch.setattr(process_detection, "pid_matches_record", lambda pid, stamp: True)
        profile = _profile(seeded)
        (profile / "auth.json").parent.mkdir(parents=True)
        (profile / "auth.json").write_text(cf.auth_json(**OTHER))
        _record(profile, 42)
        alive.add(42)

        seeded._write_account_credentials("1", "other@example.com", cf.auth_json(**OTHER))

        assert (profile / "auth.json").exists()  # never pulled from a live session
        assert stale_marker_for(profile).exists()

    def test_a_backup_write_keeps_a_profile_that_may_be_in_use(self, seeded):
        # `_post_backup_write` asks `_live_session_pids`, which drops the
        # unreadable record; the invalidation itself must still fail closed.
        profile = _profile(seeded)
        profile.mkdir(parents=True)
        ahead = cf.auth_json(
            **OTHER, refresh_token="rt-ahead", access_exp=int(time.time()) + 20 * 86400
        )
        (profile / "auth.json").write_text(ahead)
        _record(profile, 4242, "{torn")

        seeded._write_account_credentials("1", "other@example.com", cf.auth_json(**OTHER))

        assert (profile / "auth.json").read_text() == ahead
        assert stale_marker_for(profile).exists()

    def test_the_gate_never_consumes_while_the_profile_may_be_in_use(
        self, codex_home, monkeypatch
    ):
        past = int(time.time()) - 3600
        s = _switcher()
        _add(s, codex_home, **OTHER, access_exp=past)
        _add(s, codex_home, **PERSONAL)
        profile = _profile(s)
        profile.mkdir(parents=True)
        ahead = cf.auth_json(**OTHER, refresh_token="rt-ahead", access_exp=past + 60)
        (profile / "auth.json").write_text(ahead)
        _record(profile, 4242, "{torn")
        posted: list[str] = []
        monkeypatch.setattr(
            codex_auth, "try_refresh",
            lambda creds, timeout_s=10.0: posted.append(creds) or RefreshOutcome(None, "transient"),
        )
        backup = s.read_account_credentials("1", "other@example.com")

        outcome = s.consume_backup_grant("1", "other@example.com", backup)

        assert outcome.error == "transient"
        assert posted == []
        assert (profile / "auth.json").read_text() == ahead
        assert s.read_account_credentials("1", "other@example.com") == backup

    def test_removal_is_refused_while_a_session_runs(self, seeded, alive, monkeypatch):
        monkeypatch.setattr(process_detection, "pid_matches_record", lambda pid, stamp: True)
        profile = _profile(seeded)
        _record(profile, 42)
        alive.add(42)
        with pytest.raises(SessionError, match=r"live session-mode Codex process \(PID 42\)"):
            seeded._ensure_no_live_session("1", "other@example.com", "--remove-account")

        _record(profile, 42, "{torn")
        with pytest.raises(SessionError, match=r"\.cswap-run"):
            seeded._ensure_no_live_session("1", "other@example.com", "--remove-account")

    def test_a_rotated_profile_login_is_adopted_into_the_backup(self, seeded):
        profile = _profile(seeded)
        profile.mkdir(parents=True)
        rotated = cf.auth_json(
            **OTHER, refresh_token="rt-rotated", access_exp=int(time.time()) + 20 * 86400
        )
        (profile / "auth.json").write_text(rotated)

        assert seeded._adopt_session_credential("1", "other@example.com", "acct-other")
        assert seeded.read_account_credentials("1", "other@example.com") == rotated

    def test_switching_to_a_rotated_session_account_activates_the_rotation(self, seeded, codex_home):
        profile = _profile(seeded)
        profile.mkdir(parents=True)
        rotated = cf.auth_json(
            **OTHER, refresh_token="rt-rotated", access_exp=int(time.time()) + 20 * 86400
        )
        (profile / "auth.json").write_text(rotated)

        seeded.switch_to("1")

        live = json.loads((codex_home / "auth.json").read_text())
        assert live["tokens"]["refresh_token"] == "rt-rotated"

    def test_session_shell_guard(self, seeded, monkeypatch):
        profile = _profile(seeded)
        profile.mkdir(parents=True)
        monkeypatch.setenv("CODEX_HOME", str(profile))
        with pytest.raises(SwitchError, match="cswap codex run session profile"):
            seeded.remove_account("2")


class TestSessionShell:
    """Read-only commands run from a session's shell (codex's own shell
    commands inherit ``CODEX_HOME=<profile>``) must see the user's real
    default home, not the profile's login."""

    def test_list_inside_a_session_shell_never_posts_the_default_login(
        self, codex_home, monkeypatch
    ):
        s = _switcher()
        _add(s, codex_home, **OTHER)
        live = _add(s, codex_home, **PERSONAL, access_exp=int(time.time()) - 60)
        inside = _profile(s)
        inside.mkdir(parents=True)
        (inside / "auth.json").write_text(cf.auth_json(**OTHER))
        monkeypatch.setenv("CODEX_HOME", str(inside))
        posted: list[str] = []
        monkeypatch.setattr(
            codex_auth, "try_refresh",
            lambda creds, timeout_s=10.0: posted.append(creds) or RefreshOutcome(None, "transient"),
        )
        monkeypatch.setattr(
            codex_auth, "request_usage",
            lambda *a, **k: cf.wham_usage((1, 18000, int(time.time()) + 999)),
        )

        payload = _switcher().list_accounts(json_output=True)

        assert posted == []
        rows = {r["number"]: r for r in payload["accounts"]}
        assert (rows[1]["active"], rows[2]["active"]) == (False, True)
        assert (codex_home / "auth.json").read_text() == live

    def test_a_custom_default_home_is_restored_for_every_command(
        self, codex_home, monkeypatch
    ):
        custom = codex_home.parent / "custom-codex"
        custom.mkdir()
        monkeypatch.setenv("CODEX_HOME", str(custom))
        s = _switcher()
        _add(s, custom, **OTHER)
        _add(s, custom, **PERSONAL)
        (codex_home / "auth.json").write_text(cf.auth_json(**OTHER))  # stale ~/.codex
        inside = _profile(s)
        inside.mkdir(parents=True)
        monkeypatch.setenv("CODEX_HOME", str(inside))
        monkeypatch.setenv(codex_session.DEFAULT_HOME_ENV, str(custom))

        s = _switcher()

        assert os.environ["CODEX_HOME"] == str(custom)
        assert codex_session.DEFAULT_HOME_ENV not in os.environ
        assert s.current_identity() == ("user@example.com", "acct-personal")

    def test_a_recorded_default_inside_a_profile_is_never_restored(
        self, codex_home, monkeypatch
    ):
        s = _switcher()
        inside = _profile(s)
        inside.mkdir(parents=True)
        monkeypatch.setenv("CODEX_HOME", str(inside))
        monkeypatch.setenv(codex_session.DEFAULT_HOME_ENV, str(_profile(s, "2", "x@y")))

        _switcher()

        assert "CODEX_HOME" not in os.environ  # back to the implicit ~/.codex

    def test_the_gate_refuses_while_the_live_home_is_a_profile(
        self, codex_home, monkeypatch
    ):
        """Defense in depth: CODEX_HOME re-pointed into a profile after the
        switcher was built still never reaches a refresh POST."""
        s = _switcher()
        _add(s, codex_home, **OTHER)
        _add(s, codex_home, **PERSONAL, access_exp=int(time.time()) - 60)
        inside = _profile(s, "1", "other@example.com")
        inside.mkdir(parents=True)
        (inside / "auth.json").write_text(cf.auth_json(**OTHER))
        monkeypatch.setenv("CODEX_HOME", str(inside))
        posted: list[str] = []
        monkeypatch.setattr(
            codex_auth, "try_refresh",
            lambda creds, timeout_s=10.0: posted.append(creds) or RefreshOutcome(None, "transient"),
        )
        backup = s._read_account_credentials("2", "user@example.com")

        outcome = s.consume_backup_grant("2", "user@example.com", backup)

        assert (outcome.credentials, outcome.error) == (None, "transient")
        assert posted == []

    def test_a_run_built_inside_a_session_shell_still_says_so(
        self, codex_home, execs, monkeypatch, capsys
    ):
        s = _switcher()
        _add(s, codex_home, **OTHER)
        _add(s, codex_home, **PERSONAL)
        inside = _profile(s)
        inside.mkdir(parents=True)
        monkeypatch.setenv("CODEX_HOME", str(inside))

        _run(_switcher(), "2", [])  # the switcher restored the default home

        assert execs[0].args == []  # slot 2 is the default login: plain codex
        assert "CODEX_HOME" not in execs[0].env
        assert "points at a session profile" in capsys.readouterr().out

    def test_an_unknown_default_home_refreshes_nothing(
        self, codex_home, monkeypatch, capsys
    ):
        """The session's shell lost CSWAP_CODEX_DEFAULT_HOME: the default
        home is unknown (here a custom one holding slot 2's live login, while
        ~/.codex holds another), so no stored login may be refreshed."""
        custom = codex_home.parent / "custom-codex"
        custom.mkdir()
        monkeypatch.setenv("CODEX_HOME", str(custom))
        s = _switcher()
        _add(s, custom, **OTHER)
        _add(s, custom, **PERSONAL, access_exp=int(time.time()) - 60)
        (codex_home / "auth.json").write_text(cf.auth_json(**OTHER))
        inside = _profile(s)
        inside.mkdir(parents=True)
        monkeypatch.setenv("CODEX_HOME", str(inside))
        monkeypatch.delenv(codex_session.DEFAULT_HOME_ENV, raising=False)
        posted: list[str] = []
        monkeypatch.setattr(
            codex_auth, "try_refresh",
            lambda creds, timeout_s=10.0: posted.append(creds) or RefreshOutcome(None, "transient"),
        )
        monkeypatch.setattr(
            codex_auth, "request_usage",
            lambda *a, **k: cf.wham_usage((1, 18000, int(time.time()) + 999)),
        )
        capsys.readouterr()

        payload = _switcher().list_accounts(json_output=True)

        assert posted == []
        assert payload["provider"] == "codex"  # stdout stays JSON
        assert "default Codex home could not be determined" in capsys.readouterr().err

    def test_an_empty_recorded_default_is_the_implicit_home(
        self, codex_home, monkeypatch, capsys
    ):
        s = _switcher()
        _add(s, codex_home, **OTHER)
        _add(s, codex_home, **PERSONAL)
        inside = _profile(s, "2", "user@example.com")
        inside.mkdir(parents=True)
        monkeypatch.setenv("CODEX_HOME", str(inside))
        monkeypatch.setenv(codex_session.DEFAULT_HOME_ENV, "")
        posted: list[str] = []
        monkeypatch.setattr(
            codex_auth, "try_refresh",
            lambda creds, timeout_s=10.0: posted.append(creds) or RefreshOutcome(None, "transient"),
        )
        capsys.readouterr()

        s = _switcher()
        backup = s._read_account_credentials("1", "other@example.com")
        s.consume_backup_grant("1", "other@example.com", backup)

        assert "CODEX_HOME" not in os.environ
        assert posted == [backup]  # an inactive slot is refreshed, as before
        assert "could not be determined" not in capsys.readouterr().err


class TestRevokingCommands:
    """``codex login``/``logout`` revoke the login of the home they run in
    (codex-spec gotcha 1): never inside a home holding a stored account."""

    @pytest.mark.parametrize("args", [
        ["logout"], ["login"], ["login", "--device-auth"], ["--search", "logout"],
        # An option's separate value hides the subcommand from a naive parse:
        # any bare login/logout token fails closed.
        ["-m", "o3", "logout"], ["-c", "k=v", "login"], ["--profile", "x", "logout"],
        ["login", "status"], ["mcp", "logout", "srv"], ["exec", "login"],
    ])
    def test_refused_in_a_session(self, seeded, execs, args):
        with pytest.raises(
            SessionError, match="(?s)cswap codex add --login.*cswap codex remove.*longer"
        ):
            seeded.make_session_manager().run("1", args)
        assert execs == []
        assert seeded.gate_calls == []
        assert not _profile(seeded).exists()

    def test_refused_on_the_same_account_fast_path(self, seeded, execs):
        with pytest.raises(SessionError, match="revoke"):
            seeded.make_session_manager().run("2", ["logout"])
        assert execs == []

    def test_refused_on_the_default_launch(self, seeded, execs):
        with pytest.raises(SessionError, match="revoke"):
            seeded.make_session_manager().exec_default(["login"])
        assert execs == []

    @pytest.mark.parametrize("args", [
        ["fix the logout bug"], ["exec", "fix the login bug"], ["--login-hint=x"],
    ])
    def test_non_revoking_commands_run(self, seeded, execs, args):
        _run(seeded, "1", args)
        assert execs[0].args == ["-c", FILE_STORE, *args]


class TestForcedLogin:
    """A shared config.toml whose forced_* settings the session's login
    fails would make Codex delete the profile's login at startup."""

    def test_a_session_codex_would_log_out_is_refused(self, seeded, execs, codex_home):
        (codex_home / "config.toml").write_text('forced_chatgpt_workspace_id = "acct-team"\n')
        with pytest.raises(SessionError, match="forced_chatgpt_workspace_id"):
            seeded.make_session_manager().run("1", [])
        assert execs == []

    def test_a_matching_session_runs(self, seeded, execs, codex_home):
        (codex_home / "config.toml").write_text('forced_chatgpt_workspace_id = "acct-other"\n')
        _run(seeded, "1", [])
        assert execs[0].args == ["-c", FILE_STORE]

    def test_an_unshared_profile_is_held_to_its_own_config(self, seeded, execs, codex_home):
        (codex_home / "config.toml").write_text('forced_login_method = "api"\n')
        _run(seeded, "1", [], share=False)  # the profile has no config.toml
        assert len(execs) == 1
