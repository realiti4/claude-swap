"""Tests for the managed-session supervisor's recovery + resume wiring.

These exercise the pure decision-feeding seams of ``Supervisor`` (the parts the
ultrareview flagged) without spawning a real ``claude`` or touching credentials:
``subprocess.Popen`` and the recovery side-effects are mocked, and the registry
is driven directly so we can assert what the supervisor reads/writes.
"""

from __future__ import annotations

import os
from pathlib import Path

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
