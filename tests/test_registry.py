"""Tests for the managed-session registry + world-building.

Covers the usage-signal normalization seam, PID-based liveness, intent
round-trips, atomic persistence, and that ``build_world`` does not require the
write lock (it must be safe to call before acquiring it).
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import patch

from claude_swap import registry
from claude_swap.cache import write_cache
from claude_swap.locking import FileLock
from claude_swap.switcher import ClaudeAccountSwitcher

DEAD_PID = 2_147_483_646  # almost certainly not a live process


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


# --------------------------------------------------------------------------- #
# Schema normalization
# --------------------------------------------------------------------------- #


class TestNormalization:
    def test_rl_to_usage_maps_used_percentage_to_pct(self):
        rl = {
            "five_hour": {"used_percentage": 88.0, "resets_at": 2000},
            "seven_day": {"used_percentage": 41.0, "resets_at": 9000},
        }
        usage = registry._rl_to_usage(rl)
        assert usage["five_hour"] == {"pct": 88.0, "resets_at": 2000}
        assert usage["seven_day"] == {"pct": 41.0, "resets_at": 9000}

    def test_rl_to_usage_none_and_empty(self):
        assert registry._rl_to_usage(None) is None
        assert registry._rl_to_usage({}) is None
        assert registry._rl_to_usage({"five_hour": {}}) is None

    def test_soonest_blocking_reset_picks_max_pct_window(self):
        # 7d is the capping window -> its reset is the blocking one.
        usage = {
            "five_hour": {"pct": 30.0, "resets_at": 1000},
            "seven_day": {"pct": 96.0, "resets_at": 5000},
        }
        assert registry.soonest_blocking_reset(usage) == 5000

    def test_soonest_blocking_reset_none(self):
        assert registry.soonest_blocking_reset(None) is None


# --------------------------------------------------------------------------- #
# State store
# --------------------------------------------------------------------------- #


class TestStateStore:
    def test_read_missing_returns_skeleton(self, temp_home: Path):
        sw = ClaudeAccountSwitcher()
        reg = registry.read_registry(sw)
        assert reg["sessions"] == {}
        assert reg["version"] == registry.REGISTRY_VERSION

    def test_upsert_round_trip_and_persist(self, temp_home: Path):
        sw = ClaudeAccountSwitcher()
        sw._setup_directories()
        reg = registry.read_registry(sw)
        registry.upsert_session(reg, "m1", account_num="2", supervisor_pid=os.getpid())
        registry.write_registry(sw, reg)
        reg2 = registry.read_registry(sw)
        assert reg2["sessions"]["m1"]["account_num"] == "2"

    def test_upsert_skips_none_and_stamps_started_at_once(self, temp_home: Path):
        sw = ClaudeAccountSwitcher()
        reg = registry.read_registry(sw)
        row = registry.upsert_session(reg, "m1", account_num="1", cwd="/a")
        started = row["started_at"]
        # A later heartbeat omits cwd (None) -> keeps the old value, started_at stable.
        row2 = registry.upsert_session(reg, "m1", account_num="1", cwd=None)
        assert row2["cwd"] == "/a"
        assert row2["started_at"] == started

    def test_reap_dead_drops_dead_keeps_alive(self, temp_home: Path):
        reg = {"version": 1, "sessions": {}}
        registry.upsert_session(reg, "alive", supervisor_pid=os.getpid())
        registry.upsert_session(reg, "dead", supervisor_pid=DEAD_PID)
        registry.upsert_session(reg, "nopid")  # no pid yet -> kept
        changed = registry.reap_dead(reg)
        assert changed is True
        assert set(reg["sessions"]) == {"alive", "nopid"}

    def test_intent_round_trip(self, temp_home: Path):
        reg = {"version": 1, "sessions": {}}
        registry.upsert_session(reg, "m1", account_num="1")
        registry.set_intent(reg, "m1", "3", now=100.0)
        assert reg["sessions"]["m1"]["migration"] == {"to": "3", "decided_at": 100.0}
        registry.clear_intent(reg, "m1")
        assert reg["sessions"]["m1"]["migration"] is None

    def test_expire_intents(self, temp_home: Path):
        reg = {"version": 1, "sessions": {}}
        registry.upsert_session(reg, "m1", account_num="1")
        registry.set_intent(reg, "m1", "3", now=0.0)
        assert registry.expire_intents(reg, now=registry._INTENT_TTL_S + 1) is True
        assert reg["sessions"]["m1"]["migration"] is None


# --------------------------------------------------------------------------- #
# World building
# --------------------------------------------------------------------------- #


class TestBuildWorld:
    def test_live_account_uses_session_rate_limits(self, temp_home: Path):
        sw = ClaudeAccountSwitcher()
        _seed_accounts(sw, {"1": ("a@x.com", 5)})
        reg = registry.read_registry(sw)
        registry.upsert_session(
            reg, "m1", account_num="1", supervisor_pid=os.getpid(),
            rate_limits={
                "five_hour": {"used_percentage": 88.0, "resets_at": 2000},
                "seven_day": {"used_percentage": 40.0, "resets_at": 9000},
            },
            last_seen=time.time(),
        )
        registry.write_registry(sw, reg)
        acct_views, sess_views = registry.build_world(sw, reg, fetch_idle=False)
        av = acct_views["1"]
        assert av.max_pct == 88.0
        assert av.signal == "live"
        assert av.priority == 5
        # Per-window breakdown carried for the dashboard (max_pct is only the max).
        assert (av.five_hour_pct, av.five_hour_reset) == (88.0, 2000)
        assert (av.seven_day_pct, av.seven_day_reset) == (40.0, 9000)
        assert [s.session_id for s in sess_views] == ["m1"]

    def test_idle_account_uses_cache(self, temp_home: Path):
        sw = ClaudeAccountSwitcher()
        _seed_accounts(sw, {"2": ("b@x.com", 0)})
        write_cache(
            sw.backup_dir / "cache" / "usage.json",
            {"2": {"five_hour": {"pct": 40.0}, "seven_day": {"pct": 10.0}}},
        )
        reg = registry.read_registry(sw)
        acct_views, _ = registry.build_world(sw, reg, fetch_idle=False)
        av = acct_views["2"]
        assert av.max_pct == 40.0
        assert av.signal == "cache"
        # Both windows surface; no resets_at in this cache entry -> resets None.
        assert (av.five_hour_pct, av.five_hour_reset) == (40.0, None)
        assert (av.seven_day_pct, av.seven_day_reset) == (10.0, None)

    def test_unknown_account_has_no_usage(self, temp_home: Path):
        sw = ClaudeAccountSwitcher()
        _seed_accounts(sw, {"3": ("c@x.com", 0)})
        reg = registry.read_registry(sw)
        acct_views, _ = registry.build_world(sw, reg, fetch_idle=False)
        assert acct_views["3"].max_pct is None
        assert acct_views["3"].signal == "none"

    def test_build_world_does_not_need_the_write_lock(self, temp_home: Path):
        sw = ClaudeAccountSwitcher()
        _seed_accounts(sw, {"1": ("a@x.com", 0)})
        reg = registry.read_registry(sw)
        # Hold the switcher lock; build_world must still return (it never locks).
        with FileLock(sw.lock_file):
            acct_views, _ = registry.build_world(sw, reg, fetch_idle=False)
        assert "1" in acct_views

    def _seed_live_session(self, sw):
        reg = registry.read_registry(sw)
        registry.upsert_session(
            reg, "m1", account_num="1", supervisor_pid=os.getpid(),
            rate_limits={"five_hour": {"used_percentage": 98.0, "resets_at": 2000}},
            last_seen=time.time(),
        )
        registry.write_registry(sw, reg)
        return reg

    def test_live_account_fetches_spend_on_cold_cache(self, temp_home: Path):
        # Regression: a LIVE account's pay-as-you-go capability must NOT be lost
        # when the usage cache is cold. build_world fetches it once; the live
        # signal still wins for the rate-limit numbers.
        sw = ClaudeAccountSwitcher()
        _seed_accounts(sw, {"1": ("a@x.com", 5)})
        reg = self._seed_live_session(sw)
        fetched = {"extra_usage_enabled": True, "spend": {"pct": 12.0},
                   "five_hour": {"pct": 5.0}}  # usage-API view; live signal wins for limits
        with patch("claude_swap.registry._fetch_idle_usage", return_value=fetched) as m:
            acct_views, _ = registry.build_world(sw, reg, fetch_idle=True)
        av = acct_views["1"]
        assert av.signal == "live"
        assert av.max_pct == 98.0           # from the LIVE signal, not the fetch
        assert av.extra_usage is True       # recovered from the cold-cache fetch
        assert av.spend_pct == 12.0
        m.assert_called_once()

    def test_live_account_no_fetch_when_idle_disabled(self, temp_home: Path):
        # With fetch_idle=False the cold-cache live account stays best-effort: no
        # network, extra_usage unknown (False).
        sw = ClaudeAccountSwitcher()
        _seed_accounts(sw, {"1": ("a@x.com", 5)})
        reg = self._seed_live_session(sw)
        with patch("claude_swap.registry._fetch_idle_usage") as m:
            acct_views, _ = registry.build_world(sw, reg, fetch_idle=False)
        assert acct_views["1"].extra_usage is False
        m.assert_not_called()


class TestPlacementReservation:
    """BUG 003: a just-placed session not yet reporting usage counts as load."""

    def test_reservation_raises_max_pct_for_fresh_session(self, temp_home: Path):
        from claude_swap import balancer

        sw = ClaudeAccountSwitcher()
        _seed_accounts(sw, {"1": ("a@x.com", 5)})
        now = time.time()
        reg = registry.read_registry(sw)
        # A reporting session establishes account "1" at 20%.
        registry.upsert_session(
            reg, "reporting", account_num="1", supervisor_pid=os.getpid(),
            rate_limits={
                "five_hour": {"used_percentage": 20.0, "resets_at": 2000},
                "seven_day": {"used_percentage": 5.0, "resets_at": 9000},
            },
            last_seen=now,
        )
        # A freshly-placed session: no rate_limits yet, reserved just now, with a
        # non-trivial context so _pct_cost is strictly positive.
        registry.upsert_session(
            reg, "fresh", account_num="1", supervisor_pid=os.getpid(),
            reserved_at=now, ctx_tokens=100_000, last_seen=now,
        )
        registry.write_registry(sw, reg)

        acct_views, _ = registry.build_world(sw, reg, fetch_idle=False)
        expected_reserve = balancer._pct_cost(100_000)
        assert expected_reserve > 0
        # max_pct of "1" is the live 20% raised by the reservation load.
        assert acct_views["1"].max_pct == 20.0 + expected_reserve
        assert acct_views["1"].signal == "live"

    def test_reservation_expires_after_ttl(self, temp_home: Path):
        sw = ClaudeAccountSwitcher()
        _seed_accounts(sw, {"1": ("a@x.com", 5)})
        now = time.time()
        reg = registry.read_registry(sw)
        registry.upsert_session(
            reg, "reporting", account_num="1", supervisor_pid=os.getpid(),
            rate_limits={"five_hour": {"used_percentage": 20.0, "resets_at": 2000}},
            last_seen=now,
        )
        # reserved_at is older than the TTL -> no synthetic load is added.
        registry.upsert_session(
            reg, "stale", account_num="1", supervisor_pid=os.getpid(),
            reserved_at=now - registry._RESERVE_TTL_S - 1, ctx_tokens=100_000,
            last_seen=now,
        )
        registry.write_registry(sw, reg)

        acct_views, _ = registry.build_world(sw, reg, fetch_idle=False)
        assert acct_views["1"].max_pct == 20.0  # unchanged

    def test_reservation_left_alone_when_usage_unknown(self, temp_home: Path):
        # An account with no usage signal stays max_pct=None even with a reserve.
        sw = ClaudeAccountSwitcher()
        _seed_accounts(sw, {"1": ("a@x.com", 5)})
        now = time.time()
        reg = registry.read_registry(sw)
        registry.upsert_session(
            reg, "fresh", account_num="1", supervisor_pid=os.getpid(),
            reserved_at=now, ctx_tokens=100_000, last_seen=now,
        )
        registry.write_registry(sw, reg)
        acct_views, _ = registry.build_world(sw, reg, fetch_idle=False)
        assert acct_views["1"].max_pct is None
        assert acct_views["1"].signal == "none"


class TestFiveHourSignal:
    """build_world populates the 5h-specific fields the prime detection needs."""

    def test_live_account_carries_five_hour_fields(self, temp_home: Path):
        sw = ClaudeAccountSwitcher()
        _seed_accounts(sw, {"1": ("a@x.com", 5)})
        reg = registry.read_registry(sw)
        registry.upsert_session(
            reg, "m1", account_num="1", supervisor_pid=os.getpid(),
            rate_limits={
                "five_hour": {"used_percentage": 12.0, "resets_at": 2000},
                "seven_day": {"used_percentage": 40.0, "resets_at": 9000},
            },
            last_seen=time.time(),
        )
        registry.write_registry(sw, reg)
        acct_views, _ = registry.build_world(sw, reg, fetch_idle=False)
        assert acct_views["1"].five_hour_pct == 12.0
        assert acct_views["1"].five_hour_reset == 2000

    def test_idle_cache_account_unstarted_window(self, temp_home: Path):
        # Cached usage with a 0% five_hour and no resets_at -> unstarted clock.
        sw = ClaudeAccountSwitcher()
        _seed_accounts(sw, {"2": ("b@x.com", 0)})
        write_cache(
            sw.backup_dir / "cache" / "usage.json",
            {"2": {"five_hour": {"pct": 0.0}, "seven_day": {"pct": 10.0}}},
        )
        reg = registry.read_registry(sw)
        acct_views, _ = registry.build_world(sw, reg, fetch_idle=False)
        assert acct_views["2"].five_hour_pct == 0.0
        assert acct_views["2"].five_hour_reset is None  # no reset => unstarted
        assert acct_views["2"].signal == "cache"

    def test_unknown_account_has_no_five_hour_fields(self, temp_home: Path):
        sw = ClaudeAccountSwitcher()
        _seed_accounts(sw, {"3": ("c@x.com", 0)})
        reg = registry.read_registry(sw)
        acct_views, _ = registry.build_world(sw, reg, fetch_idle=False)
        assert acct_views["3"].five_hour_pct is None
        assert acct_views["3"].five_hour_reset is None

    def test_five_hour_signal_helper(self):
        assert registry._five_hour_signal(None) == (None, None)
        assert registry._five_hour_signal({}) == (None, None)
        assert registry._five_hour_signal({"five_hour": {"pct": 5.0}}) == (5.0, None)
        assert registry._five_hour_signal(
            {"five_hour": {"pct": 5.0, "resets_at": 1234}}
        ) == (5.0, 1234)


class TestPrimeGuards:
    """The registry-side prime sweep + per-account guards (feature #3)."""

    def test_claim_prime_sweep_once_per_interval(self):
        reg = registry._skeleton()
        now = 1000.0
        assert registry.claim_prime_sweep(reg, now) is True   # first claim wins
        assert registry.claim_prime_sweep(reg, now + 1) is False  # too soon
        later = now + registry._PRIME_SWEEP_INTERVAL_S + 1
        assert registry.claim_prime_sweep(reg, later) is True  # interval elapsed

    def test_prime_guard_blocks_recent_then_clears(self):
        reg = registry._skeleton()
        now = 1000.0
        assert registry.prime_guarded(reg, "1", now) is False  # never primed
        registry.stamp_primed(reg, "1", now)
        assert registry.prime_guarded(reg, "1", now + 1) is True
        assert registry.prime_guarded(reg, "1", now + registry._PRIME_ACCOUNT_GUARD_S + 1) is False

    def test_prune_primed_drops_stale_stamps(self):
        reg = registry._skeleton()
        now = 1000.0
        registry.stamp_primed(reg, "1", now)
        registry.stamp_primed(reg, "2", now - registry._PRIME_ACCOUNT_GUARD_S - 1)
        changed = registry.prune_primed(reg, now)
        assert changed is True
        assert "1" in reg["primed"]
        assert "2" not in reg["primed"]
