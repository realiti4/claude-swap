"""Focused tests for explicit owner-profile token recovery."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from claude_swap import macos_keychain, oauth, recovery
from claude_swap.credentials import ActiveCredentials, CredentialStore
from claude_swap.macos_keychain import KeychainError
from claude_swap.models import Platform

EMAIL = "private@example.com"
ORG = "org-private"
ACCOUNT = "2"
USAGE = {"five_hour": {"pct": 12.0}}


def credentials(*, expired: bool, refresh: bool = True, scopes=None) -> str:
    data = {
        "accessToken": "access-private",
        "expiresAt": 1 if expired else 9_000_000_000_000,
    }
    if refresh:
        data["refreshToken"] = "refresh-private"
    if scopes is not None:
        data["scopes"] = scopes
    return json.dumps({"claudeAiOauth": data})


class FakeStore:
    def __init__(self, *, dead: bool = False):
        self.dead = dead

    def entries(self, identities, models=()):
        return {
            num: SimpleNamespace(token_dead=lambda: self.dead)
            for num in identities
        }


class FakeSwitcher:
    def __init__(self, values: list[str] | None = None, *, dead: bool = False):
        self.backup_dir = Path(tempfile.mkdtemp(prefix="cswap-recovery-"))
        self._usage_store = FakeStore(dead=dead)
        self.values = list(values or [])
        self.kind = "oauth"
        self.account_reads = 0

    def _account_kind(self, account_num):
        return self.kind

    def _read_default_profile_credentials(self):
        self.account_reads += 1
        return ActiveCredentials(self.values.pop(0), False)

    def _get_sequence_data(self):
        return {
            "accounts": {
                ACCOUNT: {"email": EMAIL, "organizationUuid": ORG}
            }
        }

    def _read_account_credentials(self, *_args):  # pragma: no cover - tripwire
        raise AssertionError("recovery must never read a slot backup")


def default_owner(monkeypatch):
    owner = recovery._Owner("default", None)
    monkeypatch.setattr(
        recovery, "_find_owner", lambda *_args: (owner, None)
    )
    monkeypatch.setattr(recovery, "_read_default_identity", lambda: (EMAIL, ORG))
    return owner


class TestDefaultCredentialRead:
    def test_ignores_inherited_session_config_dir(self, monkeypatch, tmp_path):
        default_dir = tmp_path / ".claude"
        session_dir = tmp_path / "session"
        default_dir.mkdir()
        session_dir.mkdir()
        (default_dir / ".credentials.json").write_text("default-credentials")
        (session_dir / ".credentials.json").write_text("session-credentials")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(session_dir))
        host = SimpleNamespace(
            platform=Platform.LINUX,
            credentials_dir=tmp_path / "backups",
            _logger=SimpleNamespace(error=lambda *_a: None, warning=lambda *_a: None),
        )

        result = CredentialStore(host)._read_default_profile_credentials()

        assert result == ActiveCredentials("default-credentials", False)

    def test_macos_keychain_failure_never_falls_back_to_default_seed(
        self, monkeypatch, tmp_path
    ):
        default_dir = tmp_path / ".claude"
        default_dir.mkdir()
        (default_dir / ".credentials.json").write_text("stale-default-seed")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(
            macos_keychain,
            "get_password",
            Mock(side_effect=KeychainError("locked")),
        )
        monkeypatch.setattr("claude_swap.credentials._ACTIVE_READ_RETRY_DELAY", 0)
        host = SimpleNamespace(
            platform=Platform.MACOS,
            credentials_dir=tmp_path / "backups",
            _logger=SimpleNamespace(error=lambda *_a: None, warning=lambda *_a: None),
        )

        result = CredentialStore(host)._read_default_profile_credentials()

        assert result == ActiveCredentials(None, True)


class TestOwnerSelection:
    def test_default_profile_owner_for_active_slot(self, monkeypatch):
        switcher = FakeSwitcher()
        monkeypatch.setattr(
            recovery, "get_running_instances", lambda _dir: ([object()], [])
        )
        monkeypatch.setattr(recovery, "_read_default_identity", lambda: (EMAIL, ORG))
        monkeypatch.setattr(recovery, "live_sessions_for", lambda _dir: [])

        owner, status = recovery._find_owner(switcher, ACCOUNT, EMAIL, ORG)

        assert owner == recovery._Owner("default", None)
        assert status is None

    def test_session_profile_owner_for_inactive_slot(self, monkeypatch):
        switcher = FakeSwitcher()
        monkeypatch.setattr(
            recovery, "get_running_instances", lambda _dir: ([object()], [])
        )
        monkeypatch.setattr(
            recovery, "_read_default_identity", lambda: ("other@example.com", "")
        )
        monkeypatch.setattr(recovery, "live_sessions_for", lambda _dir: [object()])
        monkeypatch.setattr(
            recovery, "read_session_identity", lambda _dir: (EMAIL, ORG)
        )

        owner, status = recovery._find_owner(switcher, ACCOUNT, EMAIL, ORG)

        assert owner is not None and owner.kind == "session"
        assert owner.config_dir == (
            switcher.backup_dir / "sessions" / "2-private_example.com"
        )
        assert status is None

    @pytest.mark.parametrize(
        ("default_identity", "default_live", "session_live", "session_identity"),
        [
            ((EMAIL, ORG), False, False, None),  # no owner
            ((EMAIL, ORG), True, True, (EMAIL, ORG)),  # two owner profiles
            (("other@example.com", ""), False, True, ("drift@example.com", ORG)),
            (None, True, False, None),  # live default identity unreadable
        ],
    )
    def test_zero_ambiguity_and_drift_fail_closed(
        self,
        monkeypatch,
        default_identity,
        default_live,
        session_live,
        session_identity,
    ):
        switcher = FakeSwitcher()
        monkeypatch.setattr(
            recovery,
            "get_running_instances",
            lambda _dir: ([object()] if default_live else [], []),
        )
        monkeypatch.setattr(
            recovery, "_read_default_identity", lambda: default_identity
        )
        monkeypatch.setattr(
            recovery,
            "live_sessions_for",
            lambda _dir: [object()] if session_live else [],
        )
        monkeypatch.setattr(
            recovery, "read_session_identity", lambda _dir: session_identity
        )

        owner, status = recovery._find_owner(switcher, ACCOUNT, EMAIL, ORG)

        assert owner is None
        assert status == "human_required"

    def test_session_owner_requires_provably_inactive_slot(self, monkeypatch):
        switcher = FakeSwitcher()
        monkeypatch.setattr(recovery, "get_running_instances", lambda _dir: ([], []))
        monkeypatch.setattr(recovery, "_read_default_identity", lambda: (EMAIL, ORG))
        monkeypatch.setattr(recovery, "live_sessions_for", lambda _dir: [object()])
        monkeypatch.setattr(
            recovery, "read_session_identity", lambda _dir: (EMAIL, ORG)
        )

        assert recovery._find_owner(switcher, ACCOUNT, EMAIL, ORG) == (
            None,
            "human_required",
        )


class TestCanary:
    class Process:
        pid = 4321

        def __init__(self):
            self.wait_calls = []

        def wait(self, timeout=None):
            self.wait_calls.append(timeout)
            return 0

        def poll(self):
            return 0

    def test_exact_argv_default_env_and_neutral_cwd(self, monkeypatch, tmp_path):
        seen = {}
        process = self.Process()
        monkeypatch.setattr(recovery.shutil, "which", lambda _name: "/bin/claude")
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("PATH", "/sealed/bin")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/wrong/profile")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "secret")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://private.invalid")

        def popen(argv, **kwargs):
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            assert Path(kwargs["cwd"]).is_dir()
            return process

        monkeypatch.setattr(recovery.subprocess, "Popen", popen)

        assert recovery.run_canary(None) == "exited"
        assert seen["argv"] == recovery._canary_argv("/bin/claude")
        kwargs = seen["kwargs"]
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["stdout"] is subprocess.DEVNULL
        assert kwargs["stderr"] is subprocess.DEVNULL
        assert kwargs["start_new_session"] is True
        assert "CLAUDE_CONFIG_DIR" not in kwargs["env"]
        assert "ANTHROPIC_API_KEY" not in kwargs["env"]
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in kwargs["env"]
        assert "ANTHROPIC_BASE_URL" not in kwargs["env"]
        assert kwargs["env"]["HOME"] == str(tmp_path / "home")
        assert kwargs["env"]["PATH"] == "/sealed/bin"
        assert set(kwargs["env"]) <= recovery._POSIX_ENV_ALLOWLIST
        assert not Path(kwargs["cwd"]).exists()

    def test_session_env_sets_only_exact_profile(self, monkeypatch, tmp_path):
        seen = {}
        monkeypatch.setattr(recovery.shutil, "which", lambda _name: "/bin/claude")
        monkeypatch.setattr(
            recovery.subprocess,
            "Popen",
            lambda argv, **kwargs: seen.update(argv=argv, kwargs=kwargs) or self.Process(),
        )
        profile = tmp_path / "session-profile"

        assert recovery.run_canary(profile) == "exited"
        assert seen["kwargs"]["env"]["CLAUDE_CONFIG_DIR"] == str(profile)

    def test_timeout_terms_then_kills_posix_group(self, monkeypatch):
        class Hung(self.Process):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def wait(self, timeout=None):
                self.calls += 1
                if self.calls < 3:
                    raise subprocess.TimeoutExpired("claude", timeout)
                return -signal.SIGKILL

            def poll(self):
                return None

        process = Hung()
        signals = []
        monkeypatch.setattr(recovery.shutil, "which", lambda _name: "/bin/claude")
        monkeypatch.setattr(recovery.subprocess, "Popen", lambda *_a, **_kw: process)
        monkeypatch.setattr(
            recovery.os, "killpg", lambda pid, sig: signals.append((pid, sig))
        )

        assert recovery.run_canary(None) == "timed_out"
        assert signals == [(4321, signal.SIGTERM), (4321, signal.SIGKILL)]

    def test_sigterm_cleans_tree_and_restores_handlers(self, monkeypatch):
        class Running(self.Process):
            def wait(self, timeout=None):
                assert current[signal.SIGTERM] is not previous[signal.SIGTERM]
                current[signal.SIGTERM](signal.SIGTERM, None)

            def poll(self):
                return None

        process = Running()
        cleaned = []
        previous = {
            signal.SIGTERM: signal.SIG_DFL,
            signal.SIGINT: signal.default_int_handler,
        }
        current = dict(previous)

        def install(signum, handler):
            old = current[signum]
            current[signum] = handler
            return old

        monkeypatch.setattr(recovery.shutil, "which", lambda _name: "/bin/claude")
        monkeypatch.setattr(recovery.subprocess, "Popen", lambda *_a, **_kw: process)
        monkeypatch.setattr(
            recovery, "_terminate_process_tree", lambda proc: cleaned.append(proc.pid)
        )
        monkeypatch.setattr(recovery.signal, "getsignal", lambda signum: current[signum])
        monkeypatch.setattr(recovery.signal, "signal", install)

        with pytest.raises(SystemExit) as excinfo:
            recovery.run_canary(None)

        assert excinfo.value.code == 128 + signal.SIGTERM
        assert cleaned == [4321]
        assert current == previous

    def test_sigint_cleans_tree_without_swallowing_keyboardinterrupt(self, monkeypatch):
        class Running(self.Process):
            def wait(self, timeout=None):
                current[signal.SIGINT](signal.SIGINT, None)

            def poll(self):
                return None

        process = Running()
        cleaned = []
        previous = {
            signal.SIGTERM: signal.SIG_DFL,
            signal.SIGINT: signal.default_int_handler,
        }
        current = dict(previous)

        def install(signum, handler):
            old = current[signum]
            current[signum] = handler
            return old

        monkeypatch.setattr(recovery.shutil, "which", lambda _name: "/bin/claude")
        monkeypatch.setattr(recovery.subprocess, "Popen", lambda *_a, **_kw: process)
        monkeypatch.setattr(
            recovery, "_terminate_process_tree", lambda proc: cleaned.append(proc.pid)
        )
        monkeypatch.setattr(recovery.signal, "getsignal", lambda signum: current[signum])
        monkeypatch.setattr(recovery.signal, "signal", install)

        with pytest.raises(KeyboardInterrupt):
            recovery.run_canary(None)

        assert cleaned == [4321]
        assert current == previous

    def test_non_main_thread_skips_signal_guard(self, monkeypatch):
        signal_calls = []
        monkeypatch.setattr(recovery.shutil, "which", lambda _name: "/bin/claude")
        monkeypatch.setattr(
            recovery.subprocess, "Popen", lambda *_a, **_kw: self.Process()
        )
        monkeypatch.setattr(recovery.threading, "main_thread", lambda: object())
        monkeypatch.setattr(recovery.threading, "current_thread", lambda: object())
        monkeypatch.setattr(
            recovery.signal,
            "signal",
            lambda *args: signal_calls.append(args),
        )

        assert recovery.run_canary(None) == "exited"
        assert signal_calls == []

    def test_windows_cleanup_uses_taskkill_fallback(self, monkeypatch):
        process = self.Process()
        process.poll = lambda: None
        process.send_signal = Mock(side_effect=OSError("no console"))
        process.kill = Mock()
        waits = iter([False, True])
        taskkills = []
        monkeypatch.setattr(recovery, "_wait_quietly", lambda *_a: next(waits))
        monkeypatch.setattr(
            recovery,
            "_taskkill_tree",
            lambda pid, force: taskkills.append((pid, force)),
        )

        recovery._terminate_windows_tree(process)

        assert taskkills == [(4321, False), (4321, True)]
        process.kill.assert_not_called()


class TestRecoverAccount:
    def test_rereads_owner_without_fetching_or_persisting_usage(self, monkeypatch):
        switcher = FakeSwitcher(
            [credentials(expired=True), credentials(expired=True), credentials(expired=False)]
        )
        default_owner(monkeypatch)
        monkeypatch.setattr(recovery, "run_canary", lambda _dir: "exited")
        usage_fetch = Mock(side_effect=AssertionError("recovery must not fetch usage"))
        direct_refresh = Mock(side_effect=AssertionError("direct refresh forbidden"))
        monkeypatch.setattr(oauth, "try_fetch_usage_for_account", usage_fetch)
        monkeypatch.setattr(oauth, "try_refresh_oauth_credentials", direct_refresh)

        payload = recovery.recover_account(switcher, ACCOUNT, EMAIL, ORG)

        assert payload == {
            "schemaVersion": 1,
            "operation": "recover",
            "accountNumber": 2,
            "recoveryStatus": "recovered",
        }
        assert switcher.account_reads == 3
        usage_fetch.assert_not_called()
        direct_refresh.assert_not_called()

    def test_session_owner_rereads_the_same_existing_profile(self, monkeypatch):
        switcher = FakeSwitcher()
        profile = Path("/sealed/cswap/sessions/2-private_example.com")
        owner = recovery._Owner("session", profile)
        reads = Mock(
            side_effect=[
                ActiveCredentials(credentials(expired=True), False),
                ActiveCredentials(credentials(expired=True), False),
                ActiveCredentials(credentials(expired=False), False),
            ]
        )
        monkeypatch.setattr(recovery, "_find_owner", lambda *_args: (owner, None))
        monkeypatch.setattr(recovery, "read_session_owner_credentials", reads)
        monkeypatch.setattr(
            recovery, "read_session_identity", lambda path: (EMAIL, ORG)
        )
        monkeypatch.setattr(recovery, "run_canary", lambda path: "exited")
        usage_fetch = Mock(side_effect=AssertionError("recovery must not fetch usage"))
        monkeypatch.setattr(oauth, "try_fetch_usage_for_account", usage_fetch)

        result = recovery.recover_account(switcher, ACCOUNT, EMAIL, ORG)

        assert result["recoveryStatus"] == "recovered"
        assert reads.call_args_list == [call(profile), call(profile), call(profile)]
        usage_fetch.assert_not_called()

    @pytest.mark.parametrize(
        ("value", "kind"),
        [
            ("sk-ant-api-private", "api_key"),
            (credentials(expired=True, refresh=False), "oauth"),
            (credentials(expired=True, scopes=["user:inference"]), "oauth"),
        ],
    )
    def test_api_key_no_refresh_and_setup_token_need_human(
        self, monkeypatch, value, kind
    ):
        switcher = FakeSwitcher([value])
        switcher.kind = kind
        default_owner(monkeypatch)
        canary = Mock()
        monkeypatch.setattr(recovery, "run_canary", canary)

        result = recovery.recover_account(switcher, ACCOUNT, EMAIL, ORG)

        assert result["recoveryStatus"] == "human_required"
        canary.assert_not_called()

    def test_fresh_token_is_not_needed(self, monkeypatch):
        switcher = FakeSwitcher([credentials(expired=False)])
        default_owner(monkeypatch)
        canary = Mock()
        monkeypatch.setattr(recovery, "run_canary", canary)

        result = recovery.recover_account(switcher, ACCOUNT, EMAIL, ORG)

        assert result["recoveryStatus"] == "not_needed"
        canary.assert_not_called()

    def test_dead_token_quarantine_is_never_canaried(self, monkeypatch):
        switcher = FakeSwitcher([credentials(expired=True)], dead=True)
        owner = Mock()
        canary = Mock()
        monkeypatch.setattr(recovery, "_find_owner", owner)
        monkeypatch.setattr(recovery, "run_canary", canary)

        result = recovery.recover_account(switcher, ACCOUNT, EMAIL, ORG)

        assert result["recoveryStatus"] == "human_required"
        owner.assert_not_called()
        canary.assert_not_called()

    def test_no_owner_is_human_required(self, monkeypatch):
        switcher = FakeSwitcher([credentials(expired=True)])
        monkeypatch.setattr(
            recovery,
            "_find_owner",
            lambda *_args: (None, "human_required"),
        )

        result = recovery.recover_account(switcher, ACCOUNT, EMAIL, ORG)

        assert result["recoveryStatus"] == "human_required"
        assert switcher.account_reads == 0

    def test_canary_start_failure_is_retry_later(self, monkeypatch):
        switcher = FakeSwitcher([credentials(expired=True), credentials(expired=True)])
        default_owner(monkeypatch)
        monkeypatch.setattr(recovery, "run_canary", lambda _dir: "start_failed")

        result = recovery.recover_account(switcher, ACCOUNT, EMAIL, ORG)

        assert result["recoveryStatus"] == "retry_later"
        assert switcher.account_reads == 2

    def test_timeout_can_still_recover_if_profile_refreshed(self, monkeypatch):
        switcher = FakeSwitcher(
            [credentials(expired=True), credentials(expired=True), credentials(expired=False)]
        )
        default_owner(monkeypatch)
        monkeypatch.setattr(recovery, "run_canary", lambda _dir: "timed_out")

        result = recovery.recover_account(switcher, ACCOUNT, EMAIL, ORG)

        assert result["recoveryStatus"] == "recovered"

    def test_post_canary_identity_mismatch_never_fetches_usage(self, monkeypatch):
        switcher = FakeSwitcher(
            [credentials(expired=True), credentials(expired=True)]
        )
        owner = recovery._Owner("default", None)
        identities = iter([(EMAIL, ORG), (EMAIL, ORG), ("other@example.com", ORG)])
        monkeypatch.setattr(recovery, "_find_owner", lambda *_args: (owner, None))
        monkeypatch.setattr(recovery, "run_canary", lambda _dir: "exited")
        monkeypatch.setattr(recovery, "_read_default_identity", lambda: next(identities))
        fetch = Mock()
        monkeypatch.setattr(oauth, "try_fetch_usage_for_account", fetch)

        result = recovery.recover_account(switcher, ACCOUNT, EMAIL, ORG)

        assert result["recoveryStatus"] == "human_required"
        fetch.assert_not_called()
        assert switcher.account_reads == 2

    def test_prelaunch_credential_change_skips_canary(self, monkeypatch):
        changed = credentials(expired=True).replace("access-private", "access-new")
        switcher = FakeSwitcher([credentials(expired=True), changed])
        default_owner(monkeypatch)
        canary = Mock()
        monkeypatch.setattr(recovery, "run_canary", canary)

        result = recovery.recover_account(switcher, ACCOUNT, EMAIL, ORG)

        assert result["recoveryStatus"] == "retry_later"
        canary.assert_not_called()

    def test_recovery_lock_contention_is_retry_later_and_released(self, monkeypatch):
        switcher = FakeSwitcher()
        lock = Mock()
        lock.acquire.return_value = False
        monkeypatch.setattr(recovery, "_recovery_lock", lambda *_args: lock)
        owner = Mock()
        monkeypatch.setattr(recovery, "_find_owner", owner)

        result = recovery.recover_account(switcher, ACCOUNT, EMAIL, ORG)

        assert result["recoveryStatus"] == "retry_later"
        owner.assert_not_called()
        lock.release.assert_called_once_with()

    def test_recovery_lock_is_per_slot_private_state(self):
        switcher = FakeSwitcher()

        lock = recovery._recovery_lock(switcher, ACCOUNT)

        assert lock.lock_path == switcher.backup_dir / "recovery-locks" / "2.lock"
        assert lock.lock_path.parent.is_dir()

    def test_usage_failures_do_not_affect_credential_recovery(self, monkeypatch):
        switcher = FakeSwitcher(
            [credentials(expired=True), credentials(expired=True), credentials(expired=False)]
        )
        default_owner(monkeypatch)
        monkeypatch.setattr(recovery, "run_canary", lambda _dir: "exited")
        fetch = Mock(side_effect=AssertionError("recovery must not fetch usage"))
        monkeypatch.setattr(oauth, "try_fetch_usage_for_account", fetch)

        result = recovery.recover_account(switcher, ACCOUNT, EMAIL, ORG)

        assert result["recoveryStatus"] == "recovered"
        fetch.assert_not_called()

    @pytest.mark.parametrize(
        "status", ["recovered", "not_needed", "retry_later", "human_required"]
    )
    def test_envelopes_are_finite_and_pii_free(self, status):
        payload = recovery._envelope(ACCOUNT, status)
        text = json.dumps(payload)

        assert set(payload) == {
            "schemaVersion",
            "operation",
            "accountNumber",
            "recoveryStatus",
        }
        assert EMAIL not in text
        assert "/sealed" not in text
        assert "4321" not in text
