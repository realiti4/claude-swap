"""Allocation contracts; never access real credentials or invoke a model."""
import json
import os
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from claude_swap import allocation, cli
from claude_swap.exceptions import SessionError


def account(number, pct=20, **overrides):
    usage = {"five_hour": {"pct": pct}, "seven_day": {"pct": pct},
             "scoped": [{"name": "Fable", "pct": pct}]}
    value = NS(number=str(number), email=f"a{number}@test.invalid", org_uuid=f"org{number}",
               is_active=False, disabled=False, switchable=True, kind="oauth",
               usage=NS(decision_value=lambda: usage))
    value.__dict__.update(overrides)
    return value


@pytest.fixture
def manager(tmp_path, monkeypatch):
    if os.name != "posix":
        pytest.skip("automatic allocation is POSIX-only")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(allocation, "scan_live_sessions", lambda path: ([], 0))
    switcher = NS(backup_dir=tmp_path, set_poll_policy_inputs=Mock(),
                  accounts_snapshot=Mock(return_value=NS(accounts=[account(3), account(4, 10)])))
    return NS(switcher=switcher, run=Mock())


def test_prefers_headroom_for_equal_load_and_forwards(manager):
    allocation.run_allocated(manager, ["--resume", "test"], share=False)
    manager.run.assert_called_once_with("4", ["--model", "fable", "--resume", "test"],
                                        share=False, share_history=False, require_session=True)
    assert json.loads((manager.switcher.backup_dir / "allocation.json").read_text()) == {}


def test_counts_pending_launches_and_deduplicates_live_pids(manager, monkeypatch):
    records = manager.switcher.backup_dir / "allocation.json"
    records.write_text(json.dumps({str(os.getpid()): ["a4@test.invalid", "org4"]}))
    allocation.run_allocated(manager, [])
    assert manager.run.call_args.args[0] == "3"
    manager.run.reset_mock()
    monkeypatch.setattr(allocation, "scan_live_sessions", lambda path: ([NS(pid=os.getpid())], 0))
    records.write_text(json.dumps({str(os.getpid()): ["a4@test.invalid", "org4"]}))
    allocation.run_allocated(manager, [])
    assert manager.run.call_args.args[0] == "4"  # same registered PID counts once


@pytest.mark.parametrize("changes", [
    {"is_active": True}, {"disabled": True}, {"switchable": False}, {"kind": "api_key"},
    {"usage": NS(decision_value=lambda: None)},
    {"usage": NS(decision_value=lambda: "token expired")},
    {"usage": NS(decision_value=lambda: {"five_hour": {"pct": 0}, "seven_day": {"pct": 0}})},
])
def test_rejects_ineligible_accounts(manager, changes):
    manager.switcher.accounts_snapshot.return_value.accounts = [account(4, **changes)]
    with pytest.raises(SessionError, match="No isolated Fable account"):
        allocation.run_allocated(manager, [])
    manager.run.assert_not_called()


@pytest.mark.parametrize("window", ["five_hour", "seven_day", "scoped"])
@pytest.mark.parametrize("pct", [90, 100, float("nan"), float("inf"), -1])
def test_each_window_is_required_and_binds(manager, window, pct):
    a = account(3)
    usage = a.usage.decision_value()
    (usage[window][0] if window == "scoped" else usage[window])["pct"] = pct
    manager.switcher.accounts_snapshot.return_value.accounts = [a]
    with pytest.raises(SessionError):
        allocation.run_allocated(manager, [])


def test_dead_reservations_pruned_failure_releases_claim(manager, monkeypatch):
    records = manager.switcher.backup_dir / "allocation.json"
    records.write_text('{"999999": ["a4@test.invalid", "org4"]}')
    monkeypatch.setattr(allocation, "is_pid_alive", lambda pid: False)
    manager.run.side_effect = SessionError("bootstrap failed")
    with pytest.raises(SessionError, match="bootstrap failed"):
        allocation.run_allocated(manager, [])
    assert json.loads(records.read_text()) == {}


def test_unreadable_sessions_fail_closed(manager, monkeypatch):
    monkeypatch.setattr(allocation, "scan_live_sessions", lambda path: ([], 1))
    with pytest.raises(SessionError, match="unreadable session"):
        allocation.run_allocated(manager, [])


def test_cli_dispatch(manager, monkeypatch):
    monkeypatch.setattr(cli, "ClaudeAccountSwitcher", lambda **kw: manager.switcher)
    monkeypatch.setattr(cli, "_guard_root", lambda _: None)
    import claude_swap.session
    monkeypatch.setattr(claude_swap.session, "SessionManager", lambda _: manager)
    cli._run_command(["--auto", "--model", "fable", "--", "-p", "test"])
    assert manager.run.call_args.args == ("4", ["--model", "fable", "-p", "test"])


@pytest.mark.parametrize("args", [
    ["--auto"], ["3", "--auto", "--model", "fable"], ["--model", "fable"],
    ["--auto", "--model", "fable", "--threshold", "101"],
    ["--auto", "--model", "fable", "--", "--model=opus"],
    ["--auto", "--model", "fable", "--", "--fallback-model", "opus"],
])
def test_cli_invalid_args_never_initialize_switcher(args, monkeypatch):
    factory = Mock()
    monkeypatch.setattr(cli, "ClaudeAccountSwitcher", factory)
    with pytest.raises(SystemExit):
        cli._run_command(args)
    factory.assert_not_called()


def test_nested_profile_refused(manager, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/another/profile")
    with pytest.raises(SessionError, match="regular macOS/Linux terminal"):
        allocation.run_allocated(manager, [])
    manager.switcher.accounts_snapshot.assert_not_called()


def test_windows_refused_before_snapshot(monkeypatch):
    monkeypatch.setattr(allocation.sys, "platform", "win32")
    with pytest.raises(SessionError, match="regular macOS/Linux terminal"):
        allocation.run_allocated(None, [])


@pytest.mark.skipif(os.name != "posix", reason="allocation uses POSIX exec")
def test_two_real_processes_keep_separate_reservations(tmp_path):
    import select
    import subprocess
    import sys

    script = '''
import os, sys
from pathlib import Path
from types import SimpleNamespace as NS
from claude_swap.allocation import run_allocated
from claude_swap.usage_store import UsageEntry
from claude_swap.models import AccountSnapshot
accounts = [AccountSnapshot(str(n), f"a{n}@test.invalid", "org", str(n), False,
    "oauth", True, UsageEntry(last_good={"five_hour":{"pct":10},"seven_day":{"pct":10},
    "scoped":[{"name":"Fable","pct":10}]}, age_s=0)) for n in [3,4]]
def run(number, args, **kwargs):
    os.execv(sys.executable, [sys.executable, "-c",
        "import sys; print(sys.argv[1], flush=True); sys.stdin.readline()", number])
switcher = NS(backup_dir=Path(sys.argv[1]), set_poll_policy_inputs=lambda *a: None,
    accounts_snapshot=lambda: NS(accounts=accounts))
run_allocated(NS(switcher=switcher, run=run), [])
'''
    processes = []
    try:
        for _ in range(2):
            p = subprocess.Popen([sys.executable, "-c", script, str(tmp_path)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
            processes.append(p)
            assert select.select([p.stdout], [], [], 10)[0], "allocation did not start"
            assert p.stdout.readline().strip() == str(2 + len(processes))
        records = json.loads((tmp_path / "allocation.json").read_text())
        assert set(records) == {str(p.pid) for p in processes}
        assert all(p.poll() is None for p in processes)
    finally:
        for p in processes:
            try:
                p.communicate("\n", timeout=10)
            except Exception:
                p.kill()
                p.wait(timeout=5)


@pytest.mark.parametrize("content", [
    '{"abc": [], "99999999999999999999": [], "12": "junk"}',
    '{broken', '[]', '{"1": ["email", "org", 123]}',
])
def test_malformed_reservations_are_discarded(manager, content):
    (manager.switcher.backup_dir / "allocation.json").write_text(content)
    allocation.run_allocated(manager, [])
    assert manager.run.call_args.args[0] == "4"


def test_cleanup_preserves_original_error(manager, monkeypatch, capsys):
    def fail(*args, **kwargs):
        monkeypatch.setattr(allocation, "_load_reservations", Mock(side_effect=OSError("unreadable")))
        raise SessionError("original bootstrap failure")
    manager.run.side_effect = fail
    with pytest.raises(SessionError, match="original bootstrap failure"):
        allocation.run_allocated(manager, [])
    assert "Could not release allocation reservation" in capsys.readouterr().err


def test_reused_pid_reservation_is_discarded(manager, monkeypatch):
    records = manager.switcher.backup_dir / "allocation.json"
    records.write_text(json.dumps({str(os.getpid()): ["a4@test.invalid", "org4", "old-start"]}))
    monkeypatch.setattr(allocation, "pid_matches_record", lambda pid, stamp: stamp != "old-start")
    allocation.run_allocated(manager, [])
    assert manager.run.call_args.args[0] == "4"
