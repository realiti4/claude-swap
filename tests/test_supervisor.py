"""Tests for the managed-session supervisor's recovery + resume wiring.

These exercise the pure decision-feeding seams of ``Supervisor`` (the parts the
ultrareview flagged) without spawning a real ``claude`` or touching credentials:
``subprocess.Popen`` and the recovery side-effects are mocked, and the registry
is driven directly so we can assert what the supervisor reads/writes.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import patch

from claude_swap import balancer, registry
from claude_swap.supervisor import Supervisor
from claude_swap.switcher import ClaudeAccountSwitcher


def _seed_accounts(switcher, accounts: dict) -> None:
    """accounts: {num: (email, priority)}."""
    switcher._setup_directories()
    switcher._init_sequence_file()
    data = switcher._get_sequence_data()
    for num, (email, pri) in accounts.items():
        data["accounts"][num] = {
            "email": email,
            "uuid": "",
            "organizationUuid": "",
            "organizationName": "",
            "priority": pri,
            "added": "2024-01-01T00:00:00Z",
        }
    data["sequence"] = sorted(int(n) for n in accounts)
    switcher._write_json(switcher.sequence_file, data)


class _FakeProc:
    def __init__(self, pid=4321):
        self.pid = pid

    def poll(self):
        return 0


class TestAutoResumeSessionId:
    """BUG 007: auto-resume must read claude's CURRENT session id pre-Popen."""

    def test_resume_reads_session_id_from_registry_row(self, temp_home, monkeypatch):
        sw = ClaudeAccountSwitcher()
        _seed_accounts(sw, {"1": ("a@x.com", 5)})
        sup = Supervisor(sw, "mid", temp_home / "p", "1", cwd=str(temp_home), share=False)

        # Register the row, then seed claude's own session id as the statusline
        # would once claude has rendered — the authoritative value for --resume.
        sup._register()
        reg = registry.read_registry(sw)
        reg["sessions"]["mid"]["claude_session_id"] = "sid-1"
        from claude_swap.locking import FileLock

        with FileLock(sw.lock_file):
            registry.write_registry(sw, reg)

        # Capture every Popen arg list; never actually spawn a process.
        popen_calls: list[list[str]] = []

        def fake_popen(cmd, **kwargs):
            popen_calls.append(list(cmd))
            return _FakeProc()

        monkeypatch.setattr("claude_swap.supervisor.subprocess.Popen", fake_popen)
        # Neutralise the heavy / side-effecting steps so we drive only the loop.
        monkeypatch.setattr(sup, "_bootstrap_profile", lambda: None)
        monkeypatch.setattr(sup, "_register", lambda: None)
        monkeypatch.setattr(sup, "_set_pids", lambda pid: None)
        monkeypatch.setattr(sup, "_pause_and_resume", lambda: None)

        # First iteration: limit exit -> recover (resume armed). Second
        # iteration: clean exit on a healthy account -> deregister + return.
        outcomes = iter([("exit", 1), ("exit", 0)])
        limit_flags = iter([True, False])
        monkeypatch.setattr(sup, "_supervise", lambda proc: next(outcomes))
        monkeypatch.setattr(
            sup, "_should_handle_limit_exit", lambda: next(limit_flags)
        )

        rc = sup.run([], "/usr/bin/claude")
        assert rc == 0

        # Two launches: first fresh, second resumes with the row's session id.
        assert len(popen_calls) == 2
        first_args = popen_calls[0][1:]  # strip the claude binary
        second_args = popen_calls[1][1:]
        assert "--resume" not in first_args  # fresh start, no stale resume
        assert second_args == ["--resume", "sid-1"]

    def test_current_session_id_empty_when_row_missing(self, temp_home):
        sw = ClaudeAccountSwitcher()
        _seed_accounts(sw, {"1": ("a@x.com", 5)})
        sup = Supervisor(sw, "absent", temp_home / "p", "1", cwd=str(temp_home), share=False)
        assert sup._current_claude_session_id() == ""


class TestShareHistory:
    """A shared balanced profile symlinks ~/.claude session history so
    --resume/--continue can reach sessions started with plain ``claude``."""

    def _make_sup(self, temp_home):
        sw = ClaudeAccountSwitcher()
        _seed_accounts(sw, {"1": ("a@x.com", 5)})
        profile = temp_home / "managed_profile"
        profile.mkdir()
        return Supervisor(
            sw, "mid", profile, "1", cwd=str(temp_home), share=True
        )

    def test_symlinks_existing_history_items(self, temp_home):
        claude = temp_home / ".claude"
        # Seed each shared item in the default ~/.claude with a sentinel inside.
        for name in Supervisor._SHARED_HISTORY:
            src = claude / name
            src.mkdir()
            (src / "marker.txt").write_text(name)

        sup = self._make_sup(temp_home)
        sup._share_history()

        for name in Supervisor._SHARED_HISTORY:
            dest = sup.profile_dir / name
            assert dest.is_symlink(), f"{name} should be a symlink"
            assert dest.resolve() == (claude / name).resolve()
            # The shared content is reachable through the symlink.
            assert (dest / "marker.txt").read_text() == name

    def test_missing_source_items_are_skipped(self, temp_home):
        claude = temp_home / ".claude"
        # Only "projects" exists in the default home; the others are absent.
        (claude / "projects").mkdir()

        sup = self._make_sup(temp_home)
        sup._share_history()

        assert (sup.profile_dir / "projects").is_symlink()
        for name in ("todos", "shell-snapshots"):
            dest = sup.profile_dir / name
            assert not dest.exists()
            assert not dest.is_symlink()

    def test_existing_real_dest_is_not_clobbered(self, temp_home):
        claude = temp_home / ".claude"
        (claude / "projects").mkdir()
        (claude / "projects" / "from_home.txt").write_text("home")

        sup = self._make_sup(temp_home)
        # A real (non-symlink) dir already lives in the managed profile.
        real_dest = sup.profile_dir / "projects"
        real_dest.mkdir()
        (real_dest / "local.txt").write_text("local")

        sup._share_history()

        # Left untouched: still a real directory, still holds its own content,
        # and was not replaced by a symlink to ~/.claude.
        assert real_dest.is_dir() and not real_dest.is_symlink()
        assert (real_dest / "local.txt").read_text() == "local"
        assert not (real_dest / "from_home.txt").exists()


class TestQolArgs:
    """QoL launch flags: model, skip-permissions, and the 529 fallback model."""

    def _sup(self, temp_home):
        sw = ClaudeAccountSwitcher()
        _seed_accounts(sw, {"1": ("a@x.com", 5)})
        return Supervisor(sw, "mid", temp_home / "p", "1", cwd=str(temp_home), share=False)

    def test_default_launch_adds_fallback_model_sonnet(self, temp_home):
        sup = self._sup(temp_home)
        args = sup._qol_args([])
        assert "--fallback-model" in args
        assert args[args.index("--fallback-model") + 1] == "sonnet"
        # Sanity: the other QoL defaults are still there.
        assert "--model" in args
        assert "--dangerously-skip-permissions" in args

    def test_user_fallback_model_wins_no_double_add(self, temp_home):
        sup = self._sup(temp_home)
        args = sup._qol_args(["--fallback-model", "haiku"])
        # Exactly one occurrence, and it's the user's value.
        assert args.count("--fallback-model") == 1
        assert args[args.index("--fallback-model") + 1] == "haiku"


class TestSessionEnv:
    """The managed-session env: transient-resilience retry knob."""

    def _sup(self, temp_home):
        sw = ClaudeAccountSwitcher()
        _seed_accounts(sw, {"1": ("a@x.com", 5)})
        return Supervisor(sw, "mid", temp_home / "p", "1", cwd=str(temp_home), share=False)

    def test_sets_default_max_retries(self, temp_home, monkeypatch):
        # Extra in-turn retries so transient errors self-heal before failing a turn.
        monkeypatch.delenv("CLAUDE_CODE_MAX_RETRIES", raising=False)
        sup = self._sup(temp_home)
        env = sup._session_env()
        assert env["CLAUDE_CODE_MAX_RETRIES"] == "20"

    def test_respects_user_set_max_retries(self, temp_home, monkeypatch):
        # An explicit user override is never clobbered.
        monkeypatch.setenv("CLAUDE_CODE_MAX_RETRIES", "3")
        sup = self._sup(temp_home)
        env = sup._session_env()
        assert env["CLAUDE_CODE_MAX_RETRIES"] == "3"


class TestSelfSessionView:
    """BUG 009: recovery paths must size the view with this session's ctx_tokens."""

    def test_view_reflects_ctx_tokens_so_cost_is_not_undersized(self, temp_home):
        sw = ClaudeAccountSwitcher()
        _seed_accounts(sw, {"1": ("a@x.com", 5)})
        sup = Supervisor(sw, "mid", temp_home / "p", "1", cwd=str(temp_home), share=False)
        sup._register()

        # No ctx yet -> bare-view cost is the base reserve only.
        bare = sup._self_session_view()
        assert bare.ctx_tokens == 0
        assert balancer._pct_cost(bare.ctx_tokens) == balancer.BASE_RESERVE

        # Record a large context (as the statusline heartbeat would).
        reg = registry.read_registry(sw)
        reg["sessions"]["mid"]["ctx_tokens"] = 300_000
        reg["sessions"]["mid"]["paused_until"] = 12345
        reg["sessions"]["mid"]["last_migrated_at"] = 99.0
        reg["sessions"]["mid"]["pinned_account"] = "2"
        from claude_swap.locking import FileLock

        with FileLock(sw.lock_file):
            registry.write_registry(sw, reg)

        sv = sup._self_session_view()
        assert sv.ctx_tokens == 300_000
        # The cost is strictly larger than the bare/base reserve now.
        assert balancer._pct_cost(sv.ctx_tokens) > balancer.BASE_RESERVE
        # The other fields are carried through from the row, not defaulted.
        assert sv.paused_until == 12345
        assert sv.last_migrated_at == 99.0
        assert sv.pinned_account == "2"


class TestMigrateReservation:
    """BUG 003 (migrate path): _migrate must re-arm the cross-process headroom
    reservation on the NEW account so a second, independently-stranded session's
    separate build_world pass sees the target as non-empty and does not stack."""

    def _sup(self, temp_home, monkeypatch):
        sw = ClaudeAccountSwitcher()
        _seed_accounts(sw, {"1": ("a@x.com", 5), "2": ("b@x.com", 5), "3": ("c@x.com", 5)})
        sup = Supervisor(sw, "mid", temp_home / "p", "1", cwd=str(temp_home), share=False)
        sup._register()
        # Give this session a real context so its reservation cost is positive.
        reg = registry.read_registry(sw)
        reg["sessions"]["mid"]["ctx_tokens"] = 100_000
        from claude_swap.locking import FileLock
        with FileLock(sw.lock_file):
            registry.write_registry(sw, reg)
        # Migration only re-points credentials; stub the disk seed so the test is
        # filesystem-light and never touches a real keychain/profile.
        monkeypatch.setattr(sup.switcher, "seed_profile_credentials",
                            lambda *a, **k: None)
        return sup

    def test_migrate_restamps_reserved_at_and_clears_rate_limits(self, temp_home, monkeypatch):
        sup = self._sup(temp_home, monkeypatch)
        before = time.time()
        sup._migrate("2")

        reg = registry.read_registry(sup.switcher)
        row = reg["sessions"]["mid"]
        assert row["account_num"] == "2"            # re-pointed to the target
        assert row["rate_limits"] is None           # don't judge by old numbers
        assert row["paused_until"] is None
        assert row["_prev_max_pct"] is None          # fresh rising-edge basis
        # The reservation stamp is fresh (the load-bearing BUG-003 fix).
        assert isinstance(row.get("reserved_at"), (int, float))
        assert row["reserved_at"] >= before

    def test_migrated_session_counts_as_load_on_new_account(self, temp_home, monkeypatch):
        # After the migrate, build_world must attribute _pct_cost(ctx) of synthetic
        # load to the NEW account so a separate stranded session does not co-stack.
        sup = self._sup(temp_home, monkeypatch)
        # Seed a cached usage so account "2" is otherwise idle at a known pct
        # (use write_cache so it lands inside the TTL envelope build_world reads).
        from claude_swap.cache import write_cache
        write_cache(
            sup.switcher.backup_dir / "cache" / "usage.json",
            {"2": {"five_hour": {"pct": 80.0, "resets_at": 2000}}},
        )
        sup._migrate("2")

        reg = registry.read_registry(sup.switcher)
        acct_views, _ = registry.build_world(sup.switcher, reg, fetch_idle=False)
        expected = balancer._pct_cost(100_000)
        assert expected > 0
        # Account 2's cached 80% is raised by the migrated session's reservation.
        assert acct_views["2"].max_pct == 80.0 + expected


class TestRecoverAuth:
    """401 auto-recovery: refresh+re-seed the same account, else migrate."""

    def _sup(self, temp_home):
        sw = ClaudeAccountSwitcher()
        _seed_accounts(sw, {"1": ("a@x.com", 5), "2": ("b@x.com", 5)})
        sup = Supervisor(sw, "mid", temp_home / "p", "1", cwd=str(temp_home), share=False)
        sup._register()
        return sup

    def test_refresh_success_does_not_migrate(self, temp_home, monkeypatch):
        sup = self._sup(temp_home)
        monkeypatch.setattr(
            sup.switcher, "refresh_account_and_reseed", lambda acct, pdir: True
        )
        migrated: list[str] = []
        monkeypatch.setattr(sup, "_migrate", lambda to: migrated.append(to))

        sup._recover_auth()

        assert migrated == []  # same-account refresh worked -> no migration

    def test_refresh_fail_with_target_migrates(self, temp_home, monkeypatch):
        sup = self._sup(temp_home)
        monkeypatch.setattr(
            sup.switcher, "refresh_account_and_reseed", lambda acct, pdir: False
        )
        migrated: list[str] = []
        monkeypatch.setattr(sup, "_migrate", lambda to: migrated.append(to))
        with patch("claude_swap.supervisor.registry.build_world", return_value=({}, {})), \
             patch("claude_swap.supervisor.balancer.choose_migration_target", return_value="2"):
            sup._recover_auth()

        assert migrated == ["2"]  # account logged out -> migrate to healthy target

    def test_refresh_fail_no_target_does_not_crash(self, temp_home, monkeypatch):
        sup = self._sup(temp_home)
        monkeypatch.setattr(
            sup.switcher, "refresh_account_and_reseed", lambda acct, pdir: False
        )
        migrated: list[str] = []
        monkeypatch.setattr(sup, "_migrate", lambda to: migrated.append(to))
        with patch("claude_swap.supervisor.registry.build_world", return_value=({}, {})), \
             patch("claude_swap.supervisor.balancer.choose_migration_target", return_value=None):
            # No target anywhere -> warn the user, no migration, no crash.
            sup._recover_auth()

        assert migrated == []

    def test_consume_state_routes_recent_auth_flag_and_clears_it(
        self, temp_home, monkeypatch
    ):
        import time
        from claude_swap.locking import FileLock

        sup = self._sup(temp_home)
        reg = registry.read_registry(sup.switcher)
        reg["sessions"]["mid"]["auth_recover"] = time.time()
        with FileLock(sup.switcher.lock_file):
            registry.write_registry(sup.switcher, reg)

        calls: list[int] = []
        monkeypatch.setattr(sup, "_recover_auth", lambda: calls.append(1))

        decision = sup._consume_own_state()

        assert decision == "auth"
        assert calls == [1]
        # The flag is cleared so the next tick doesn't re-recover.
        reg2 = registry.read_registry(sup.switcher)
        assert reg2["sessions"]["mid"].get("auth_recover") is None

    def test_consume_state_ignores_stale_auth_flag(self, temp_home, monkeypatch):
        import time
        from claude_swap.locking import FileLock

        sup = self._sup(temp_home)
        reg = registry.read_registry(sup.switcher)
        # A flag older than the TTL was left by a crash -> cleared, not acted on.
        reg["sessions"]["mid"]["auth_recover"] = time.time() - 10_000
        with FileLock(sup.switcher.lock_file):
            registry.write_registry(sup.switcher, reg)

        calls: list[int] = []
        monkeypatch.setattr(sup, "_recover_auth", lambda: calls.append(1))

        decision = sup._consume_own_state()

        assert decision is None
        assert calls == []
        reg2 = registry.read_registry(sup.switcher)
        assert reg2["sessions"]["mid"].get("auth_recover") is None


class TestPrimeIdleWindows:
    """The resident supervisor's idle-5h-window prime sweep (feature #3).

    Every test MOCKS oauth.prime_account — NO real prime call is ever made.
    """

    def _sup(self, temp_home):
        sw = ClaudeAccountSwitcher()
        _seed_accounts(sw, {"1": ("a@x.com", 5), "2": ("b@x.com", 5), "3": ("c@x.com", 5)})
        # Priming requires BOTH the balancer being enabled AND the dedicated,
        # default-OFF primeIdleWindows opt-in (feature #3).
        sw.set_auto_balance_config(enabled=True, prime_idle_windows=True)
        sup = Supervisor(sw, "mid", temp_home / "p", "1", cwd=str(temp_home), share=False)
        sup._register()
        return sup

    def _idle_world(self):
        # Account 1 hosts this session (live); 2 and 3 are idle with unstarted
        # 5h windows -> prime candidates; choose only those != self.account.
        return (
            {
                "1": balancer.AccountView("1", priority=5, max_pct=10.0, signal="live",
                                          five_hour_pct=10.0),
                "2": balancer.AccountView("2", priority=5, max_pct=0.0, signal="cache",
                                          five_hour_pct=0.0, five_hour_reset=None),
                "3": balancer.AccountView("3", priority=5, max_pct=0.0, signal="cache",
                                          five_hour_pct=0.0, five_hour_reset=None),
            },
            [],
        )

    def test_primes_idle_candidates_and_stamps_them(self, temp_home, monkeypatch):
        sup = self._sup(temp_home)
        primed: list[str] = []
        monkeypatch.setattr(sup, "_prime_one_account",
                            lambda num: primed.append(num) or True)
        with patch("claude_swap.supervisor.registry.build_world", return_value=self._idle_world()):
            sup._maybe_prime_idle_windows()

        # Account 1 is self (skipped); 2 and 3 are idle candidates and get primed.
        assert sorted(primed) == ["2", "3"]
        reg = registry.read_registry(sup.switcher)
        assert set(reg.get("primed", {})) == {"2", "3"}

    def test_local_interval_gate_skips_repeated_calls(self, temp_home, monkeypatch):
        sup = self._sup(temp_home)
        calls = {"n": 0}
        monkeypatch.setattr(sup, "_prime_one_account",
                            lambda num: calls.__setitem__("n", calls["n"] + 1) or True)
        with patch("claude_swap.supervisor.registry.build_world", return_value=self._idle_world()):
            sup._maybe_prime_idle_windows()
            first = calls["n"]
            sup._maybe_prime_idle_windows()  # immediate second call -> local gate
        assert first == 2
        assert calls["n"] == 2  # no additional primes on the gated second tick

    def test_disabled_balancer_never_primes(self, temp_home, monkeypatch):
        sup = self._sup(temp_home)
        sup.switcher.set_auto_balance_config(enabled=False)
        primed: list[str] = []
        monkeypatch.setattr(sup, "_prime_one_account",
                            lambda num: primed.append(num) or True)
        with patch("claude_swap.supervisor.registry.build_world", return_value=self._idle_world()):
            sup._maybe_prime_idle_windows()
        assert primed == []

    def test_enabled_but_prime_flag_off_never_primes(self, temp_home, monkeypatch):
        # primeIdleWindows is a dedicated, default-OFF opt-in (review fix #3): even
        # with the balancer enabled, priming must NOT fire unless it is set, because
        # it spends real credits on the unverified fixed-from-first-use premise.
        sup = self._sup(temp_home)
        sup.switcher.set_auto_balance_config(enabled=True, prime_idle_windows=False)
        primed: list[str] = []
        monkeypatch.setattr(sup, "_prime_one_account",
                            lambda num: primed.append(num) or True)
        with patch("claude_swap.supervisor.registry.build_world", return_value=self._idle_world()):
            sup._maybe_prime_idle_windows()
        assert primed == []

    def test_prime_fires_only_when_both_flags_on(self, temp_home, monkeypatch):
        # Mirror of the above: with BOTH enabled and primeIdleWindows on, the sweep
        # fires. (The default new-config state has primeIdleWindows False.)
        sup = self._sup(temp_home)
        sup.switcher.set_auto_balance_config(enabled=True, prime_idle_windows=True)
        primed: list[str] = []
        monkeypatch.setattr(sup, "_prime_one_account",
                            lambda num: primed.append(num) or True)
        with patch("claude_swap.supervisor.registry.build_world", return_value=self._idle_world()):
            sup._maybe_prime_idle_windows()
        assert sorted(primed) == ["2", "3"]

    def test_cross_process_claim_blocks_second_supervisor(self, temp_home, monkeypatch):
        # A second supervisor in the same interval must NOT re-sweep (the registry
        # claim is shared); only one fires the (mocked) prime calls.
        sup1 = self._sup(temp_home)
        sup2 = Supervisor(sup1.switcher, "mid2", temp_home / "p2", "1",
                          cwd=str(temp_home), share=False)
        sup2._register()
        primed: list[str] = []
        for s in (sup1, sup2):
            monkeypatch.setattr(s, "_prime_one_account",
                                lambda num: primed.append(num) or True)
        with patch("claude_swap.supervisor.registry.build_world", return_value=self._idle_world()):
            sup1._maybe_prime_idle_windows()
            sup2._maybe_prime_idle_windows()
        assert sorted(primed) == ["2", "3"]  # exactly one sweep happened

    def test_guard_skips_recently_primed_account(self, temp_home, monkeypatch):
        sup = self._sup(temp_home)
        # Pre-stamp account 2 as just-primed so it is guarded out this sweep.
        from claude_swap.locking import FileLock
        with FileLock(sup.switcher.lock_file):
            reg = registry.read_registry(sup.switcher)
            registry.stamp_primed(reg, "2", __import__("time").time())
            registry.write_registry(sup.switcher, reg)
        primed: list[str] = []
        monkeypatch.setattr(sup, "_prime_one_account",
                            lambda num: primed.append(num) or True)
        with patch("claude_swap.supervisor.registry.build_world", return_value=self._idle_world()):
            sup._maybe_prime_idle_windows()
        assert primed == ["3"]  # 2 guarded, 1 is self

    def test_prime_failure_is_not_stamped(self, temp_home, monkeypatch):
        sup = self._sup(temp_home)
        monkeypatch.setattr(sup, "_prime_one_account", lambda num: False)  # all fail
        with patch("claude_swap.supervisor.registry.build_world", return_value=self._idle_world()):
            sup._maybe_prime_idle_windows()
        reg = registry.read_registry(sup.switcher)
        assert reg.get("primed", {}) == {}  # nothing stamped on failure


class TestPrimeOneAccount:
    """`_prime_one_account` reads inactive-account creds and calls oauth.prime_account.

    oauth.prime_account is MOCKED — NO real network call is made.
    """

    def _sup_with_creds(self, temp_home):
        sw = ClaudeAccountSwitcher()
        _seed_accounts(sw, {"1": ("a@x.com", 5), "2": ("b@x.com", 5)})
        # Seed a fresh (unexpired) token for account 2 so no refresh path runs.
        import json as _json
        import time as _time
        from claude_swap.locking import FileLock
        creds = _json.dumps({
            "claudeAiOauth": {
                "accessToken": "tok-2",
                "refreshToken": "ref-2",
                "expiresAt": int((_time.time() + 3600) * 1000),
                "subscriptionType": "max",
            }
        })
        with FileLock(sw.lock_file):
            sw.write_account_credentials("2", "b@x.com", creds)
        sup = Supervisor(sw, "mid", temp_home / "p", "1", cwd=str(temp_home), share=False)
        return sup

    def test_calls_prime_with_extracted_token(self, temp_home):
        sup = self._sup_with_creds(temp_home)
        with patch("claude_swap.supervisor.oauth.prime_account", return_value=True) as m:
            ok = sup._prime_one_account("2")
        assert ok is True
        m.assert_called_once_with("tok-2")

    def test_skips_api_credit_account_without_billing_it(self, temp_home):
        # An account authenticated without a recognized subscription tier (e.g.
        # API / console billing) must never be primed — no prime POST at all.
        sw = ClaudeAccountSwitcher()
        _seed_accounts(sw, {"1": ("a@x.com", 5), "2": ("b@x.com", 5)})
        import json as _json
        import time as _time
        from claude_swap.locking import FileLock
        creds = _json.dumps({
            "claudeAiOauth": {
                "accessToken": "tok-2",
                "refreshToken": "ref-2",
                "expiresAt": int((_time.time() + 3600) * 1000),
                # No subscriptionType -> treated as a non-subscription account.
            }
        })
        with FileLock(sw.lock_file):
            sw.write_account_credentials("2", "b@x.com", creds)
        sup = Supervisor(sw, "mid", temp_home / "p", "1", cwd=str(temp_home), share=False)
        with patch("claude_swap.supervisor.oauth.prime_account", return_value=True) as m:
            ok = sup._prime_one_account("2")
        assert ok is False
        m.assert_not_called()

    def test_missing_credentials_returns_false_without_calling(self, temp_home):
        sup = self._sup_with_creds(temp_home)
        # Account "1" has no stored creds in this fixture.
        with patch("claude_swap.supervisor.oauth.prime_account", return_value=True) as m:
            ok = sup._prime_one_account("1")
        assert ok is False
        m.assert_not_called()
