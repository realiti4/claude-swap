"""Tests for the ``cswap statusfailure`` StopFailure safety net.

The hook fires when a managed turn fails after claude exhausts its API retries
(a hard rate limit). When the session's account is genuinely at/over the exhaust
threshold AND the failure isn't a plainly-not-account-specific kind (overload /
server error), the handler records a migration *intent* so the owning supervisor
re-points the session and the NEXT turn lands on a fresh account. It never writes
credentials and always exits 0.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from claude_swap import registry, statusline
from claude_swap.cache import write_cache
from claude_swap.locking import FileLock
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


def _stdin(error_type: str | None, sid: str = "claude-sid") -> str:
    """A StopFailure stdin payload with the given ``error_type``."""
    payload = {
        "session_id": sid,
        "cwd": "/work",
        "hook_event_name": "StopFailure",
    }
    if error_type is not None:
        payload["error_type"] = error_type
    return json.dumps(payload)


def _setup(sw, profile_dir: Path, *, own_pct: float, target_headroom: bool):
    """Seed a managed session on Account-1 with ``own_pct`` utilization.

    Account-1's live usage is set via the registry heartbeat; Account-2 gets a
    cache signal (with or without headroom) to control whether a target exists.
    Returns the managed id.
    """
    _seed_accounts(sw, {"1": ("a@x.com", 0), "2": ("b@x.com", 5)})
    sw.set_auto_balance_config(enabled=True, threshold=95)
    write_cache(
        sw.backup_dir / "cache" / "usage.json",
        {"2": {"five_hour": {"pct": (10.0 if target_headroom else 99.0)}, "seven_day": {"pct": 5.0}}},
    )
    managed_id = profile_dir.name
    reg = registry.read_registry(sw)
    registry.upsert_session(
        reg,
        managed_id,
        account_num="1",
        supervisor_pid=os.getpid(),
        profile_dir=str(profile_dir),
        ctx_tokens=1000,
        last_seen=time.time(),
        rate_limits={
            "five_hour": {"used_percentage": own_pct, "resets_at": int(time.time()) + 3600},
            "seven_day": {"used_percentage": 10.0, "resets_at": int(time.time()) + 9000},
        },
    )
    with FileLock(sw.lock_file):
        registry.write_registry(sw, reg)
    return managed_id


def _intent(sw, managed_id: str):
    reg = registry.read_registry(sw)
    return (reg["sessions"][managed_id].get("migration") or {}).get("to")


class TestStopFailureSafetyNet:
    def test_rate_limit_over_threshold_with_target_writes_intent(
        self, temp_home, monkeypatch
    ):
        sw = ClaudeAccountSwitcher()
        profile_dir = sw.managed_dir / "rl-over"
        profile_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(profile_dir))
        managed_id = _setup(sw, profile_dir, own_pct=97.0, target_headroom=True)

        statusline.run_statusfailure(sw, _stdin("rate_limit"))

        assert _intent(sw, managed_id) == "2"

    def test_overloaded_writes_no_intent_even_over_threshold(
        self, temp_home, monkeypatch
    ):
        sw = ClaudeAccountSwitcher()
        profile_dir = sw.managed_dir / "ovl"
        profile_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(profile_dir))
        managed_id = _setup(sw, profile_dir, own_pct=97.0, target_headroom=True)

        statusline.run_statusfailure(sw, _stdin("overloaded"))

        assert _intent(sw, managed_id) is None

    def test_server_error_writes_no_intent_even_over_threshold(
        self, temp_home, monkeypatch
    ):
        sw = ClaudeAccountSwitcher()
        profile_dir = sw.managed_dir / "srv"
        profile_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(profile_dir))
        managed_id = _setup(sw, profile_dir, own_pct=97.0, target_headroom=True)

        statusline.run_statusfailure(sw, _stdin("server_error"))

        assert _intent(sw, managed_id) is None

    def test_under_threshold_writes_no_intent(self, temp_home, monkeypatch):
        sw = ClaudeAccountSwitcher()
        profile_dir = sw.managed_dir / "under"
        profile_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(profile_dir))
        managed_id = _setup(sw, profile_dir, own_pct=50.0, target_headroom=True)

        statusline.run_statusfailure(sw, _stdin("rate_limit"))

        assert _intent(sw, managed_id) is None

    def test_unknown_error_type_relies_on_usage_check(self, temp_home, monkeypatch):
        # An absent/unknown error_type still migrates when the account is over
        # threshold (usage check alone), since (a) is sufficient.
        sw = ClaudeAccountSwitcher()
        profile_dir = sw.managed_dir / "unk"
        profile_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(profile_dir))
        managed_id = _setup(sw, profile_dir, own_pct=97.0, target_headroom=True)

        statusline.run_statusfailure(sw, _stdin(None))

        assert _intent(sw, managed_id) == "2"

    def test_over_threshold_but_no_target_headroom_writes_no_intent(
        self, temp_home, monkeypatch
    ):
        sw = ClaudeAccountSwitcher()
        profile_dir = sw.managed_dir / "no-target"
        profile_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(profile_dir))
        managed_id = _setup(sw, profile_dir, own_pct=97.0, target_headroom=False)

        statusline.run_statusfailure(sw, _stdin("rate_limit"))

        assert _intent(sw, managed_id) is None

    def test_malformed_stdin_exits_zero_no_crash(self, temp_home, monkeypatch):
        sw = ClaudeAccountSwitcher()
        profile_dir = sw.managed_dir / "bad"
        profile_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(profile_dir))

        assert statusline.run_statusfailure(sw, "not json{{{") == 0
        assert statusline.run_statusfailure(sw, "") == 0
        assert statusline.run_statusfailure(sw, "[1, 2, 3]") == 0

    def test_no_managed_id_exits_zero(self, temp_home, monkeypatch):
        sw = ClaudeAccountSwitcher()
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        assert statusline.run_statusfailure(sw, _stdin("rate_limit")) == 0

    def test_no_registry_row_exits_zero(self, temp_home, monkeypatch):
        sw = ClaudeAccountSwitcher()
        profile_dir = sw.managed_dir / "orphan"
        profile_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(profile_dir))
        _seed_accounts(sw, {"1": ("a@x.com", 0)})
        sw.set_auto_balance_config(enabled=True, threshold=95)
        # No registry row for this session -> bail.
        assert statusline.run_statusfailure(sw, _stdin("rate_limit")) == 0
