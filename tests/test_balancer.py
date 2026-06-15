"""Tests for the pure load-balancer decision logic (``claude_swap.balancer``).

The balancer is I/O-free, so these are fast, deterministic, table-driven unit
tests over synthetic :class:`AccountView` / :class:`SessionView` snapshots — no
filesystem, no network, no mocks.
"""

from __future__ import annotations

from claude_swap import balancer
from claude_swap.balancer import (
    BASE_RESERVE,
    BalancerConfig,
    AccountView,
    SessionView,
    assign_new_session,
    config_from_dict,
    rebalance,
)

CFG = BalancerConfig()  # exhaust 95, target 90, band 3, cooldown 600
NOW = 10_000.0


def AV(num, priority=0, max_pct=None, reset=None, signal="live"):
    return AccountView(num=num, priority=priority, max_pct=max_pct, soonest_reset=reset, signal=signal)


def SV(sid, acct, ctx=0, last_seen=0.0, paused=None, migrated=0.0, pinned=None):
    return SessionView(
        sid, acct, ctx_tokens=ctx, last_seen=last_seen,
        paused_until=paused, last_migrated_at=migrated, pinned_account=pinned,
    )


def _acts(plan):
    return [(a.kind, a.session_id, a.to_account) for a in plan.actions]


def _kinds(plan):
    return {a.session_id: a.kind for a in plan.actions}


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


class TestConfig:
    def test_safety_invariant_holds(self):
        # The load-bearing margin: a freshly-placed session can't immediately
        # re-exhaust its target.
        assert CFG.exhaust_threshold - CFG.target_safety >= BASE_RESERVE

    def test_from_dict_clamps_safety_below_threshold(self):
        cfg = config_from_dict({"threshold": 80, "targetSafety": 95})
        assert cfg.target_safety <= cfg.exhaust_threshold - int(BASE_RESERVE)

    def test_from_dict_defaults_on_garbage(self):
        cfg = config_from_dict({"threshold": "oops"})
        assert cfg.exhaust_threshold == BalancerConfig().exhaust_threshold


# --------------------------------------------------------------------------- #
# rebalance — the core pass
# --------------------------------------------------------------------------- #


class TestRebalance:
    def test_single_exhausted_migrates_to_highest_priority_target(self):
        accts = {
            "1": AV("1", priority=0, max_pct=96.0),
            "2": AV("2", priority=1, max_pct=30.0),
            "3": AV("3", priority=5, max_pct=30.0),
        }
        plan = rebalance(accts, [SV("s1", "1")], NOW, CFG)
        assert _acts(plan) == [("MIGRATE", "s1", "3")]  # pri 5 wins

    def test_all_exhausted_pause_until_soonest_reset(self):
        now = 1000.0
        accts = {
            "1": AV("1", max_pct=96.0, reset=2000),
            "2": AV("2", max_pct=97.0, reset=1500),
        }
        plan = rebalance(accts, [SV("a", "1"), SV("b", "2")], now, CFG)
        kinds = _kinds(plan)
        assert kinds == {"a": "PAUSE", "b": "PAUSE"}
        assert all(a.resume_at == 1500 for a in plan.actions)  # soonest

    def test_new_high_priority_account_attracts_stranded_when_not_cooled(self):
        accts = {"1": AV("1", priority=0, max_pct=96.0), "2": AV("2", priority=9, max_pct=5.0)}
        plan = rebalance(accts, [SV("s1", "1", migrated=NOW - 700)], NOW, CFG)
        assert _acts(plan) == [("MIGRATE", "s1", "2")]

    def test_session_in_cooldown_is_not_stranded(self):
        accts = {"1": AV("1", priority=0, max_pct=96.0), "2": AV("2", priority=9, max_pct=5.0)}
        plan = rebalance(accts, [SV("s1", "1", migrated=NOW - 100)], NOW, CFG)
        assert plan.actions == []  # cooldown: left alone (no bounce)

    def test_paused_session_resumes_when_account_recovers(self):
        now = 1000.0
        accts = {"1": AV("1", max_pct=20.0)}  # recovered (below band)
        plan = rebalance(accts, [SV("s1", "1", paused=5000)], now, CFG)
        assert _acts(plan) == [("RESUME", "s1", "1")]

    def test_paused_session_stays_paused_while_account_capped(self):
        # 7d window still over the limit (max_pct=96), so no early resume.
        now = 1000.0
        accts = {"1": AV("1", max_pct=96.0, reset=9000)}
        plan = rebalance(accts, [SV("s1", "1", paused=5000)], now, CFG)
        assert plan.actions == []

    def test_expired_pause_still_capped_repauses_to_real_reset(self):
        # Timer elapsed (now >= paused_until) but the account is still capped and
        # there's nowhere to go -> re-pause until the real reset, NOT resume.
        now = 6000.0
        accts = {"1": AV("1", max_pct=96.0, reset=9000)}
        plan = rebalance(accts, [SV("s1", "1", paused=5000)], now, CFG)
        assert _kinds(plan) == {"s1": "PAUSE"}
        assert plan.actions[0].resume_at == 9000

    def test_expired_pause_migrates_when_another_account_opened_up(self):
        # Timer elapsed; current account still capped but a sibling now has room.
        now = 6000.0
        accts = {
            "1": AV("1", max_pct=96.0, reset=9000),
            "2": AV("2", priority=3, max_pct=20.0),
        }
        plan = rebalance(accts, [SV("s1", "1", paused=5000)], now, CFG)
        assert _acts(plan) == [("MIGRATE", "s1", "2")]

    def test_move_then_exhaust_trap_pauses_instead(self):
        # Only target at 88%; a big-context session would push it to ~98% > safety.
        accts = {"1": AV("1", max_pct=96.0, reset=2000), "2": AV("2", max_pct=88.0)}
        plan = rebalance(accts, [SV("s1", "1", ctx=400_000)], NOW, CFG)
        assert _kinds(plan) == {"s1": "PAUSE"}

    def test_co_exhaust_cheap_migrates_expensive_pauses(self):
        # One target with room for one small session; the cheap one moves, the
        # expensive one can't fit after the online reservation -> pauses.
        accts = {
            "1": AV("1", max_pct=96.0, reset=2000),
            "2": AV("2", max_pct=86.0),
        }
        sessions = [SV("cheap", "1", ctx=0), SV("expensive", "1", ctx=300_000)]
        plan = rebalance(accts, sessions, NOW, CFG)
        kinds = _kinds(plan)
        assert kinds["cheap"] == "MIGRATE"
        assert kinds["expensive"] == "PAUSE"

    def test_priority_tie_breaks_on_headroom_then_number(self):
        accts = {
            "1": AV("1", priority=0, max_pct=96.0),
            "2": AV("2", priority=5, max_pct=50.0),
            "3": AV("3", priority=5, max_pct=30.0),
        }
        plan = rebalance(accts, [SV("s1", "1")], NOW, CFG)
        assert _acts(plan) == [("MIGRATE", "s1", "3")]  # more headroom wins

        accts2 = {
            "1": AV("1", priority=0, max_pct=96.0),
            "2": AV("2", priority=5, max_pct=40.0),
            "3": AV("3", priority=5, max_pct=40.0),
        }
        plan2 = rebalance(accts2, [SV("s1", "1")], NOW, CFG)
        assert _acts(plan2) == [("MIGRATE", "s1", "2")]  # tie -> lowest num

    def test_unknown_usage_account_is_never_a_target(self):
        accts = {"1": AV("1", max_pct=96.0, reset=2000), "2": AV("2", max_pct=None)}
        plan = rebalance(accts, [SV("s1", "1")], NOW, CFG)
        assert _kinds(plan) == {"s1": "PAUSE"}

    def test_pinned_session_never_moved_or_paused(self):
        accts = {"1": AV("1", max_pct=96.0, reset=2000), "2": AV("2", max_pct=10.0)}
        plan = rebalance(accts, [SV("s1", "1", pinned="1")], NOW, CFG)
        assert plan.actions == []

    def test_healthy_session_left_alone(self):
        accts = {"1": AV("1", max_pct=40.0), "2": AV("2", max_pct=10.0)}
        plan = rebalance(accts, [SV("s1", "1")], NOW, CFG)
        assert plan.actions == []  # not exhausted -> no churn

    def test_deterministic_regardless_of_input_order(self):
        accts = {
            "1": AV("1", priority=1, max_pct=96.0, reset=2000),
            "2": AV("2", priority=5, max_pct=20.0),
            "3": AV("3", priority=5, max_pct=21.0),
        }
        sessions = [SV("a", "1", ctx=10), SV("b", "1", ctx=20)]
        plan1 = rebalance(accts, sessions, NOW, CFG)
        # Reverse both the dict and the list order.
        accts_rev = dict(reversed(list(accts.items())))
        plan2 = rebalance(accts_rev, list(reversed(sessions)), NOW, CFG)
        assert _acts(plan1) == _acts(plan2)


# --------------------------------------------------------------------------- #
# assign_new_session — initial placement
# --------------------------------------------------------------------------- #


class TestAssignNewSession:
    def test_picks_highest_priority_with_headroom(self):
        accts = {
            "1": AV("1", priority=1, max_pct=10.0),
            "2": AV("2", priority=9, max_pct=10.0),
        }
        assert assign_new_session(accts, 0, NOW, CFG) == "2"

    def test_skips_exhausted_high_priority_for_next_tier(self):
        accts = {
            "1": AV("1", priority=9, max_pct=99.0),
            "2": AV("2", priority=1, max_pct=10.0),
        }
        assert assign_new_session(accts, 0, NOW, CFG) == "2"

    def test_none_when_nothing_has_room(self):
        accts = {"1": AV("1", priority=9, max_pct=99.0)}
        assert assign_new_session(accts, 0, NOW, CFG) is None
