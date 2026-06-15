"""Tests for the ``cswap statusline`` balancer trigger (planning re-arm).

Focus: BUG 012 — once an account is at/over threshold, ``_prev_max_pct`` stays
over threshold every tick, so a rising-EDGE-only trigger would never re-fire if a
migration intent failed to consume. The level-based re-arm must re-plan whenever
the account is over threshold with NO pending intent, while still NOT re-planning
when an intent is already pending (no churn).
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


def _stdin(pct: float, sid: str = "claude-sid") -> str:
    """A statusline stdin payload putting the session's account at ``pct``."""
    return json.dumps(
        {
            "rate_limits": {
                "five_hour": {"used_percentage": pct, "resets_at": int(time.time()) + 3600},
                "seven_day": {"used_percentage": 10.0, "resets_at": int(time.time()) + 9000},
            },
            "context_window": {"total_input_tokens": 1000},
            "model": {"id": "claude-opus"},
            "cwd": "/work",
            "session_id": sid,
        }
    )


def _setup_over_threshold(sw, profile_dir: Path, *, with_intent: bool):
    """Seed a managed session already at/over threshold with prev>=threshold.

    A second account ("2") is given cache usage with plenty of headroom so the
    balancer has a real migration target. Returns the managed id.
    """
    _seed_accounts(sw, {"1": ("a@x.com", 0), "2": ("b@x.com", 5)})
    sw.set_auto_balance_config(enabled=True, threshold=95)
    # Account 2 has headroom (cache signal) so a MIGRATE target exists.
    write_cache(
        sw.backup_dir / "cache" / "usage.json",
        {"2": {"five_hour": {"pct": 10.0}, "seven_day": {"pct": 5.0}}},
    )
    managed_id = profile_dir.name
    reg = registry.read_registry(sw)
    registry.upsert_session(
        reg, managed_id, account_num="1", supervisor_pid=os.getpid(),
        profile_dir=str(profile_dir), last_seen=time.time(),
    )
    # Already crossed: prev_max is over the threshold (so it's NOT a rising edge).
    reg["sessions"][managed_id]["_prev_max_pct"] = 96.0
    if with_intent:
        # A distinctly-old decided_at (still within the 1800s TTL) so a re-plan
        # would be detectable as an overwrite, but it won't be expired.
        registry.set_intent(reg, managed_id, "2", time.time() - 100.0)
    with FileLock(sw.lock_file):
        registry.write_registry(sw, reg)
    return managed_id


class TestPlanningReArm:
    def test_re_fires_when_over_threshold_and_no_intent(self, temp_home, monkeypatch):
        sw = ClaudeAccountSwitcher()
        profile_dir = sw.managed_dir / "abc123managed"
        profile_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(profile_dir))
        managed_id = _setup_over_threshold(sw, profile_dir, with_intent=False)

        # No rising edge (prev=96 >= 95), no pending intent -> level re-arm fires.
        statusline.run_statusline(sw, _stdin(96.0))

        reg = registry.read_registry(sw)
        intent = reg["sessions"][managed_id].get("migration")
        assert isinstance(intent, dict) and intent.get("to") == "2"

    def test_does_not_re_fire_when_intent_already_pending(self, temp_home, monkeypatch):
        sw = ClaudeAccountSwitcher()
        profile_dir = sw.managed_dir / "def456managed"
        profile_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(profile_dir))
        managed_id = _setup_over_threshold(sw, profile_dir, with_intent=True)

        # The intent is recorded with a sentinel decided_at; re-planning would
        # overwrite it with ``now``. Capture the original to detect a re-fire.
        reg_before = registry.read_registry(sw)
        before = reg_before["sessions"][managed_id]["migration"]["decided_at"]

        statusline.run_statusline(sw, _stdin(96.0))

        reg_after = registry.read_registry(sw)
        intent = reg_after["sessions"][managed_id].get("migration")
        # Intent is still present and unchanged (no re-plan happened this tick).
        assert isinstance(intent, dict) and intent.get("to") == "2"
        assert intent["decided_at"] == before
