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
    accounts_needing_prime,
    assign_new_session,
    config_from_dict,
    five_hour_warm,
    rebalance,
)

CFG = BalancerConfig()  # exhaust 95, target 90, band 3 (no time cooldown)
NOW = 10_000.0


def AV(num, priority=0, max_pct=None, reset=None, signal="live", h5_pct=None, h5_reset=None,
       extra_usage=False, spend_pct=None):
    return AccountView(
        num=num, priority=priority, max_pct=max_pct, soonest_reset=reset, signal=signal,
        five_hour_pct=h5_pct, five_hour_reset=h5_reset,
        extra_usage=extra_usage, spend_pct=spend_pct,
    )


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

    def test_exhausted_session_migrates_to_open_account(self):
        accts = {"1": AV("1", priority=0, max_pct=96.0), "2": AV("2", priority=9, max_pct=5.0)}
        plan = rebalance(accts, [SV("s1", "1", migrated=NOW - 700)], NOW, CFG)
        assert _acts(plan) == [("MIGRATE", "s1", "2")]

    def test_recently_migrated_session_is_not_blocked_by_any_cooldown(self):
        # A session that JUST migrated (last_migrated_at == now) and whose account
        # is now exhausted must STILL be able to move — there is no time cooldown.
        # last_migrated_at is telemetry only and gates nothing. This is the
        # "stranded on a freshly-exhausted account" case the cooldown used to break.
        accts = {"1": AV("1", priority=0, max_pct=96.0), "2": AV("2", priority=9, max_pct=5.0)}
        for elapsed in (0.0, 1.0, 100.0, 599.0):  # all well within the old 600s lockout
            plan = rebalance(accts, [SV("s1", "1", migrated=NOW - elapsed)], NOW, CFG)
            assert _acts(plan) == [("MIGRATE", "s1", "2")], f"blocked at elapsed={elapsed}"

    def test_no_cooldown_field_on_config(self):
        # The cooldown was removed entirely; nothing in the config gates on time.
        assert not hasattr(CFG, "migration_cooldown")
        assert not hasattr(balancer, "DEFAULT_MIGRATION_COOLDOWN")
        assert not hasattr(balancer, "_in_cooldown")

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
            "2": AV("2", max_pct=80.0),  # room for the cheap session, not both (target 85)
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


# --------------------------------------------------------------------------- #
# Equal-priority EVEN USAGE — placement + migration spread (requirement #2)
# --------------------------------------------------------------------------- #


class TestEqualPriorityEvenUsage:
    """Among accounts of the SAME priority, the least-used (most projected
    headroom) is preferred so equal-priority accounts stay evenly used over time.

    This is a tiebreak for WHERE a session goes, not WHETHER it moves: switch
    minimization (only-switch-when-exhausted) is exercised separately below.
    """

    def _place_k_across_j(self, j: int, k: int, ctx: int = 0) -> dict[str, int]:
        """Place K fresh sessions one-by-one across J equal-priority, equally-empty
        accounts, threading the online reservation between placements (as a real
        concurrent-launch pass does). Returns the per-account placement count.
        """
        accts = {str(n): AV(str(n), priority=5, max_pct=0.0) for n in range(1, j + 1)}
        projected = {num: 0.0 for num in accts}
        counts = {num: 0 for num in accts}
        for _ in range(k):
            chosen = assign_new_session(accts, ctx, NOW, CFG, projected=projected)
            assert chosen is not None
            counts[chosen] += 1
            projected[chosen] += balancer._pct_cost(ctx)
        return counts

    def test_spreads_evenly_across_equal_priority_accounts(self):
        # K = a multiple of J -> a perfectly even spread.
        for j, k in ((2, 6), (3, 9), (4, 8), (5, 10)):
            counts = self._place_k_across_j(j, k)
            assert set(counts.values()) == {k // j}, (j, k, counts)

    def test_spread_is_balanced_when_not_a_multiple(self):
        # K not a multiple of J -> counts differ by at most one (round-robin).
        for j, k in ((3, 7), (4, 10), (5, 13)):
            counts = self._place_k_across_j(j, k)
            assert max(counts.values()) - min(counts.values()) <= 1, (j, k, counts)
            assert sum(counts.values()) == k

    def test_first_placements_break_ties_by_account_number(self):
        # Three equal-priority, equally-empty accounts: the first session goes to
        # the lowest number, the second to the next, the third to the last — a
        # deterministic, number-ordered round-robin (no dependence on dict order).
        accts = {
            "3": AV("3", priority=5, max_pct=0.0),
            "1": AV("1", priority=5, max_pct=0.0),
            "2": AV("2", priority=5, max_pct=0.0),
        }
        projected = {num: 0.0 for num in accts}
        picks = []
        for _ in range(3):
            chosen = assign_new_session(accts, 0, NOW, CFG, projected=projected)
            picks.append(chosen)
            projected[chosen] += balancer._pct_cost(0)
        assert picks == ["1", "2", "3"]

    def test_prefers_least_used_within_priority_tier(self):
        # Equal priority, different current usage -> the emptiest one wins.
        accts = {
            "1": AV("1", priority=5, max_pct=60.0),
            "2": AV("2", priority=5, max_pct=10.0),
            "3": AV("3", priority=5, max_pct=40.0),
        }
        assert assign_new_session(accts, 0, NOW, CFG) == "2"

    def test_higher_priority_beats_emptier_lower_priority(self):
        # Priority dominates the even-usage tiebreak: a near-empty low-priority
        # account does NOT win over a higher-priority one with headroom.
        accts = {
            "1": AV("1", priority=9, max_pct=70.0),
            "2": AV("2", priority=1, max_pct=2.0),
        }
        assert assign_new_session(accts, 0, NOW, CFG) == "1"

    def test_migration_target_spreads_across_equal_priority(self):
        # Two sessions stranded on an exhausted account, two equal-priority empty
        # targets: each session lands on a DIFFERENT target (the online
        # reservation makes the second pick the now-emptier sibling), so the
        # exhausted account's load spreads evenly rather than stacking on one.
        accts = {
            "1": AV("1", priority=0, max_pct=97.0, reset=2000),
            "2": AV("2", priority=5, max_pct=0.0),
            "3": AV("3", priority=5, max_pct=0.0),
        }
        sessions = [SV("a", "1", ctx=0), SV("b", "1", ctx=0)]
        plan = rebalance(accts, sessions, NOW, CFG)
        targets = {a.session_id: a.to_account for a in plan.actions if a.kind == "MIGRATE"}
        assert set(targets.values()) == {"2", "3"}, targets

    def test_per_supervisor_migration_storm_spreads_via_reservation(self):
        # The per-supervisor (independent build_world) path: each stranded session
        # is migrated by its OWN supervisor in a SEPARATE pass with an empty
        # in-pass `projected`. Cross-process spread therefore relies on the migrate
        # path re-stamping reserved_at (BUG-003), which build_world folds into the
        # target's max_pct. Simulate that here: fold each chosen target's
        # reservation into av[tgt].max_pct between calls. The targets must spread
        # across {'2','3'} rather than all stacking on the lowest-numbered '2'.
        cost = balancer._pct_cost(0)
        av = {
            "1": AV("1", priority=0, max_pct=97.0, reset=2000),
            "2": AV("2", priority=5, max_pct=0.0),
            "3": AV("3", priority=5, max_pct=0.0),
        }
        picks = []
        for sid in ("a", "b", "c", "d"):
            tgt = balancer.choose_migration_target(SV(sid, "1", ctx=0), av, {}, CFG)
            assert tgt is not None
            picks.append(tgt)
            # Mimic build_world folding this just-migrated session's reservation
            # into the new account's apparent usage on the next supervisor's pass.
            av[tgt] = AV(tgt, priority=5, max_pct=av[tgt].max_pct + cost)
        # Round-robin spread across the two equal-priority empties, ties by number.
        assert picks == ["2", "3", "2", "3"], picks
        counts = {t: picks.count(t) for t in ("2", "3")}
        assert counts == {"2": 2, "3": 2}, counts

    def test_only_switches_when_exhausted_not_for_even_usage(self):
        # WHETHER to switch is still governed by exhaustion: a session on a
        # heavily-but-not-exhausted account is LEFT ALONE even though an
        # equal-priority sibling is far emptier. Even-usage is a WHERE tiebreak,
        # never a reason to migrate on its own.
        accts = {
            "1": AV("1", priority=5, max_pct=80.0),   # busy but below threshold
            "2": AV("2", priority=5, max_pct=1.0),    # much emptier sibling
        }
        plan = rebalance(accts, [SV("s1", "1")], NOW, CFG)
        assert plan.actions == []  # no churn purely to even out usage


# --------------------------------------------------------------------------- #
# Idle-5h-window prime detection (feature #3) — pure, table-driven
# --------------------------------------------------------------------------- #


class TestAccountsNeedingPrime:
    """``accounts_needing_prime`` flags managed accounts whose 5h window is
    UNSTARTED so the supervisor can prime them. Pure / I/O-free."""

    def test_idle_unstarted_window_is_a_candidate(self):
        # signal=cache, 5h pct ~0, no 5h reset, 7d not exhausted -> prime it.
        accts = {"1": AV("1", max_pct=0.0, signal="cache", h5_pct=0.0, h5_reset=None)}
        assert accounts_needing_prime(accts, CFG) == ["1"]

    def test_started_window_is_not_a_candidate_by_pct(self):
        # 5h pct above the epsilon => clock already running => leave alone.
        accts = {"1": AV("1", max_pct=12.0, signal="cache", h5_pct=12.0, h5_reset=None)}
        assert accounts_needing_prime(accts, CFG) == []

    def test_started_window_is_not_a_candidate_by_reset(self):
        # A concrete future 5h reset means the clock is running even if pct rounds
        # to ~0 — don't burn a credit re-priming it.
        accts = {"1": AV("1", max_pct=0.0, signal="cache", h5_pct=0.0, h5_reset=99999)}
        assert accounts_needing_prime(accts, CFG) == []

    def test_live_account_is_never_primed(self):
        # A live (in-use) account already has a started clock; never prime it.
        accts = {"1": AV("1", max_pct=0.0, signal="live", h5_pct=0.0, h5_reset=None)}
        assert accounts_needing_prime(accts, CFG) == []

    def test_unknown_usage_is_never_primed(self):
        # signal=none => couldn't read usage => skip (don't blind-prime).
        accts = {"1": AV("1", max_pct=None, signal="none", h5_pct=None, h5_reset=None)}
        assert accounts_needing_prime(accts, CFG) == []

    def test_seven_day_exhausted_account_is_not_primed(self):
        # 5h unstarted but the WEEKLY cap is binding (max_pct over threshold from
        # the 7d window) -> a fresh 5h window can't help, so don't waste a credit.
        accts = {"1": AV("1", max_pct=99.0, signal="cache", h5_pct=0.0, h5_reset=None)}
        assert accounts_needing_prime(accts, CFG) == []

    def test_epsilon_tolerates_tiny_nonzero_pct(self):
        accts = {"1": AV("1", max_pct=0.3, signal="cache", h5_pct=0.3, h5_reset=None)}
        assert accounts_needing_prime(accts, CFG) == ["1"]

    def test_candidates_returned_in_account_number_order(self):
        accts = {
            "10": AV("10", max_pct=0.0, signal="cache", h5_pct=0.0),
            "2": AV("2", max_pct=0.0, signal="cache", h5_pct=0.0),
            "1": AV("1", max_pct=0.0, signal="cache", h5_pct=0.0),
        }
        assert accounts_needing_prime(accts, CFG) == ["1", "2", "10"]

    def test_mixed_pool_only_idle_cache_accounts(self):
        accts = {
            "1": AV("1", max_pct=0.0, signal="cache", h5_pct=0.0),     # candidate
            "2": AV("2", max_pct=50.0, signal="live", h5_pct=50.0),    # live
            "3": AV("3", max_pct=0.0, signal="cache", h5_pct=8.0),     # started (pct)
            "4": AV("4", max_pct=None, signal="none"),                 # unknown
            "5": AV("5", max_pct=0.0, signal="cache", h5_pct=0.0),     # candidate
        }
        assert accounts_needing_prime(accts, CFG) == ["1", "5"]


class TestFiveHourWarm:
    """``five_hour_warm`` reports the dashboard's warm/cold/unknown 5h state —
    the exact inverse of priming candidacy (a primeable window reads cold)."""

    def test_running_reset_is_warm(self):
        assert five_hour_warm(AV("1", h5_pct=0.0, h5_reset=99999)) is True

    def test_pct_above_epsilon_is_warm(self):
        assert five_hour_warm(AV("1", h5_pct=8.0, h5_reset=None)) is True

    def test_zero_pct_no_reset_is_cold(self):
        # The unstarted clock — exactly what idle-window priming targets.
        assert five_hour_warm(AV("1", h5_pct=0.0, h5_reset=None)) is False

    def test_unknown_usage_is_none(self):
        assert five_hour_warm(AV("1", h5_pct=None, h5_reset=None)) is None

    def test_inverse_of_prime_candidacy(self):
        # An account flagged for priming must read cold; one left alone reads warm.
        cold = AV("1", max_pct=0.0, signal="cache", h5_pct=0.0, h5_reset=None)
        warm = AV("2", max_pct=12.0, signal="cache", h5_pct=12.0, h5_reset=None)
        assert accounts_needing_prime({"1": cold}, CFG) == ["1"]
        assert five_hour_warm(cold) is False
        assert accounts_needing_prime({"2": warm}, CFG) == []
        assert five_hour_warm(warm) is True


# A config with the API-rate last-resort tier enabled (only_subscription off).
CFG_API = BalancerConfig(only_subscription=False)


class TestApiRateLastResort:
    """Subscription accounts are always preferred; pay-as-you-go / API-rate
    accounts (and exhausted-subscription-with-extra-usage) are a last resort,
    gated behind ``only_subscription``."""

    # -- default (only_subscription=True): API tier is never touched ---------

    def test_default_never_places_new_session_on_api(self):
        accts = {
            "1": AV("1", max_pct=98.0),                                   # exhausted subscription
            "2": AV("2", max_pct=None, extra_usage=True, spend_pct=0.0),  # API-capable
        }
        # Subscription-only: no headroom => None (caller starts paused), never a2.
        assert assign_new_session(accts, 0, NOW, CFG) is None

    def test_default_pauses_rather_than_spill_into_extra_usage(self):
        accts = {"1": AV("1", max_pct=98.0, extra_usage=True, spend_pct=0.0)}
        assert _kinds(rebalance(accts, [SV("s1", "1")], NOW, CFG)) == {"s1": "PAUSE"}

    # -- API tier enabled: subscription STILL strictly preferred -------------

    def test_subscription_preferred_over_api_even_at_lower_priority(self):
        accts = {
            "1": AV("1", priority=0, max_pct=50.0),                       # subscription headroom
            "2": AV("2", priority=9, max_pct=None, extra_usage=True),     # API, higher priority
        }
        assert assign_new_session(accts, 0, NOW, CFG_API) == "1"

    def test_api_used_only_when_no_subscription_headroom(self):
        accts = {
            "1": AV("1", max_pct=98.0),                                    # exhausted, no extra-usage
            "2": AV("2", max_pct=None, extra_usage=True, spend_pct=10.0),  # API-capable
        }
        assert assign_new_session(accts, 0, NOW, CFG_API) == "2"

    def test_migrate_exhausted_to_subscription_not_api(self):
        accts = {
            "1": AV("1", max_pct=98.0, extra_usage=True, spend_pct=0.0),   # current, exhausted, extra-usage
            "2": AV("2", max_pct=40.0),                                    # subscription headroom
            "3": AV("3", max_pct=None, extra_usage=True, spend_pct=0.0),   # API
        }
        # Migrating to the subscription account saves money — preferred over API/stay.
        assert _acts(rebalance(accts, [SV("s1", "1")], NOW, CFG_API)) == [("MIGRATE", "s1", "2")]

    def test_keep_on_current_extra_usage_when_no_subscription_left(self):
        accts = {
            "1": AV("1", max_pct=98.0, extra_usage=True, spend_pct=0.0),   # current, exhausted, extra-usage
            "2": AV("2", max_pct=99.0),                                    # other subscription, exhausted, no extra-usage
        }
        # Keep working on the current account's pay-as-you-go capacity — don't pause,
        # don't thrash to a different API account.
        assert _kinds(rebalance(accts, [SV("s1", "1")], NOW, CFG_API)) == {"s1": "KEEP"}

    def test_dead_current_migrates_to_api_elsewhere(self):
        accts = {
            "1": AV("1", max_pct=99.0),                                    # current exhausted, NO extra-usage
            "2": AV("2", max_pct=None, extra_usage=True, spend_pct=0.0),   # API-capable elsewhere
        }
        assert _acts(rebalance(accts, [SV("s1", "1")], NOW, CFG_API)) == [("MIGRATE", "s1", "2")]

    def test_pause_when_neither_subscription_nor_api_can_serve(self):
        accts = {
            "1": AV("1", max_pct=99.0),  # exhausted, no extra-usage
            "2": AV("2", max_pct=99.0),  # exhausted, no extra-usage
        }
        assert _kinds(rebalance(accts, [SV("s1", "1")], NOW, CFG_API)) == {"s1": "PAUSE"}

    def test_api_over_monthly_cap_is_not_usable(self):
        accts = {
            "1": AV("1", max_pct=98.0),                                      # exhausted subscription
            "2": AV("2", max_pct=None, extra_usage=True, spend_pct=100.0),   # API maxed monthly budget
        }
        assert assign_new_session(accts, 0, NOW, CFG_API) is None

    def test_api_least_monthly_spend_chosen_first(self):
        accts = {
            "1": AV("1", max_pct=98.0),                                     # exhausted subscription
            "2": AV("2", max_pct=None, extra_usage=True, spend_pct=60.0),
            "3": AV("3", max_pct=None, extra_usage=True, spend_pct=20.0),   # most budget left
        }
        assert assign_new_session(accts, 0, NOW, CFG_API) == "3"

    def test_pause_when_all_api_accounts_monthly_budget_exhausted(self):
        # Every account is extra-usage-capable but all monthly budgets are maxed
        # (spend_pct == cap) — nothing can serve, so the session pauses.
        accts = {
            "1": AV("1", max_pct=98.0, extra_usage=True, spend_pct=100.0),  # current, exhausted + maxed
            "2": AV("2", max_pct=99.0, extra_usage=True, spend_pct=100.0),  # other, maxed
        }
        assert _kinds(rebalance(accts, [SV("s1", "1")], NOW, CFG_API)) == {"s1": "PAUSE"}

    def test_expired_pause_keeps_on_current_extra_usage_when_api_enabled(self):
        # A paused session whose timer has elapsed, stranded on an exhausted
        # account that still has pay-as-you-go capacity, KEEPs (spills to API)
        # rather than re-pausing — the resume path's API-tier interaction.
        accts = {
            "1": AV("1", max_pct=98.0, extra_usage=True, spend_pct=0.0),  # current, exhausted, extra-usage
            "2": AV("2", max_pct=99.0),                                   # other subscription, exhausted
        }
        plan = rebalance(accts, [SV("s1", "1", paused=5000)], NOW, CFG_API)
        assert _kinds(plan) == {"s1": "KEEP"}

    def test_multiple_stranded_sessions_rank_api_by_budget(self):
        # Two sessions stranded on an exhausted, non-extra-usage account both pick
        # the lowest-spend API account. _rank_api_accounts ignores `projected` by
        # design (spend_pct is server-side monthly tracking), so they may stack.
        accts = {
            "1": AV("1", max_pct=99.0),                                    # exhausted, no extra-usage
            "2": AV("2", max_pct=None, extra_usage=True, spend_pct=60.0),
            "3": AV("3", max_pct=None, extra_usage=True, spend_pct=20.0),  # most budget left
        }
        plan = rebalance(accts, [SV("s1", "1"), SV("s2", "1")], NOW, CFG_API)
        targets = {a.session_id: a.to_account for a in plan.actions if a.kind == "MIGRATE"}
        assert targets == {"s1": "3", "s2": "3"}

    def test_fresh_config_defaults_to_subscription_only(self):
        # The critical default-safe guarantee, end-to-end through config parsing:
        # a fresh install (no autoBalance / empty dict) is subscription-only, so
        # an exhausted account with extra-usage never spills to API rates.
        for raw in (None, {}):
            cfg = config_from_dict(raw)
            assert cfg.only_subscription is True
            accts = {"1": AV("1", max_pct=98.0, extra_usage=True, spend_pct=0.0)}
            assert assign_new_session(accts, 0, NOW, cfg) is None
            assert _kinds(rebalance(accts, [SV("s1", "1")], NOW, cfg)) == {"s1": "PAUSE"}
