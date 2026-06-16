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

    def test_live_account_reads_spend_from_cache_no_fetch(self, temp_home: Path):
        # A live account reads pay-as-you-go info from the cache when present and
        # NEVER triggers a usage fetch (avoids extra usage-API load + token churn).
        sw = ClaudeAccountSwitcher()
        _seed_accounts(sw, {"1": ("a@x.com", 5)})
        write_cache(sw.backup_dir / "cache" / "usage.json",
                    {"1": {"extra_usage_enabled": True, "spend": {"pct": 12.0}}})
        reg = self._seed_live_session(sw)
        with patch("claude_swap.registry._fetch_idle_usage") as m:
            acct_views, _ = registry.build_world(sw, reg, fetch_idle=True)
        av = acct_views["1"]
        assert av.signal == "live"
        assert av.max_pct == 98.0                       # from the live signal
        assert (av.extra_usage, av.spend_pct) == (True, 12.0)  # from cache
        m.assert_not_called()                           # never fetch for a live acct

    def test_live_account_cold_cache_extra_usage_unknown_no_fetch(self, temp_home: Path):
        # Cold cache: a live account's extra-usage is simply unknown (False); we
        # still never fetch for it.
        sw = ClaudeAccountSwitcher()
        _seed_accounts(sw, {"1": ("a@x.com", 5)})
        reg = self._seed_live_session(sw)
        with patch("claude_swap.registry._fetch_idle_usage") as m:
            acct_views, _ = registry.build_world(sw, reg, fetch_idle=True)
        assert acct_views["1"].extra_usage is False
        m.assert_not_called()

    def test_failed_idle_fetch_is_cached_to_throttle_retries(self, temp_home: Path):
        # A failed idle fetch (e.g. usage-API 429 -> None) is recorded as an
        # "_unavailable" marker so the cache TTL throttles retries instead of
        # refetching every pass — the fix for the all-accounts 429 feedback loop.
        sw = ClaudeAccountSwitcher()
        _seed_accounts(sw, {"2": ("b@x.com", 0)})
        reg = registry.read_registry(sw)
        with patch("claude_swap.registry._fetch_idle_usage", return_value=None) as m:
            acct_views, _ = registry.build_world(sw, reg, fetch_idle=True)
            assert acct_views["2"].signal == "none"
            assert m.call_count == 1
            # The marker is now cached -> a second pass does NOT refetch.
            acct_views2, _ = registry.build_world(sw, reg, fetch_idle=True)
            assert acct_views2["2"].signal == "none"
            assert m.call_count == 1
        from claude_swap.cache import read_cache
        cp = sw.backup_dir / "cache" / "usage.json"
        assert read_cache(cp, 10**9).get("2", {}).get("_unavailable") is True

    def test_active_default_account_is_never_refreshed(self, temp_home: Path):
        # Credential-invalidation fix: the idle usage path MUST mark the active
        # default login (the account Claude Code owns right now) is_active=True so
        # it is never refreshed/rotated out from under the live login — the bug
        # that forced spontaneous re-logins. A genuinely-idle account still may.
        sw = ClaudeAccountSwitcher()
        _seed_accounts(sw, {"1": ("a@x.com", 0), "2": ("b@x.com", 0)})
        reg = registry.read_registry(sw)
        seen: dict[str, bool] = {}

        def fake_fetch(switcher, num, email, *, is_active=False, failure_out=None):
            seen[num] = is_active
            return None

        with patch("claude_swap.registry._fetch_idle_usage", side_effect=fake_fetch), \
             patch.object(ClaudeAccountSwitcher, "active_account_num", return_value="1"):
            registry.build_world(sw, reg, fetch_idle=True)

        assert seen == {"1": True, "2": False}

    def test_live_session_account_is_never_refreshed(self, temp_home: Path):
        # An account with a live (cswap run / vanilla) session is also is_active=True
        # — refreshing it would rotate the token out from under that live session.
        sw = ClaudeAccountSwitcher()
        _seed_accounts(sw, {"1": ("a@x.com", 0), "2": ("b@x.com", 0)})
        reg = registry.read_registry(sw)
        seen: dict[str, bool] = {}

        def fake_fetch(switcher, num, email, *, is_active=False, failure_out=None):
            seen[num] = is_active
            return None

        with patch("claude_swap.registry._fetch_idle_usage", side_effect=fake_fetch), \
             patch.object(ClaudeAccountSwitcher, "active_account_num", return_value=None), \
             patch.object(ClaudeAccountSwitcher, "has_live_session",
                          side_effect=lambda num, email: num == "2"):
            registry.build_world(sw, reg, fetch_idle=True)

        assert seen == {"1": False, "2": True}

    def test_retry_after_backoff_marker_suppresses_refetch_past_ttl(self, temp_home: Path):
        # A 429 backoff marker (retry_until in the future) suppresses refetch even
        # after the freshness TTL elapses (here TTL=0), so a rate-limited usage
        # endpoint is not re-hit every TTL — the persistent "usage unavailable" loop.
        sw = ClaudeAccountSwitcher()
        _seed_accounts(sw, {"2": ("b@x.com", 0)})
        write_cache(
            sw.backup_dir / "cache" / "usage.json",
            {"2": {"_unavailable": True, "retry_until": time.time() + 3600}},
        )
        reg = registry.read_registry(sw)
        with patch("claude_swap.registry._usage_ttl", return_value=0), \
             patch("claude_swap.registry._fetch_idle_usage") as m:
            acct_views, _ = registry.build_world(sw, reg, fetch_idle=True)
        assert acct_views["2"].signal == "none"
        m.assert_not_called()

    def test_expired_backoff_marker_allows_refetch(self, temp_home: Path):
        # Once the server's Retry-After window elapses, the account is refetched.
        sw = ClaudeAccountSwitcher()
        _seed_accounts(sw, {"2": ("b@x.com", 0)})
        write_cache(
            sw.backup_dir / "cache" / "usage.json",
            {"2": {"_unavailable": True, "retry_until": time.time() - 10}},
        )
        reg = registry.read_registry(sw)
        with patch("claude_swap.registry._usage_ttl", return_value=0), \
             patch("claude_swap.registry._fetch_idle_usage", return_value=None) as m:
            registry.build_world(sw, reg, fetch_idle=True)
        m.assert_called_once()

    def test_429_retry_after_is_recorded_as_backoff_marker(self, temp_home: Path):
        # A usage-endpoint 429 records a backoff marker carrying the server's
        # Retry-After so subsequent passes honor it (see the suppress test above).
        sw = ClaudeAccountSwitcher()
        _seed_accounts(sw, {"2": ("b@x.com", 0)})
        reg = registry.read_registry(sw)

        def fake_fetch(switcher, num, email, *, is_active=False, failure_out=None):
            if failure_out is not None:
                failure_out["retry_after"] = 1800
            return None

        with patch("claude_swap.registry._fetch_idle_usage", side_effect=fake_fetch):
            registry.build_world(sw, reg, fetch_idle=True)
        from claude_swap.cache import read_cache, usage_backoff_active
        entry = read_cache(sw.backup_dir / "cache" / "usage.json", 10**9).get("2")
        assert usage_backoff_active(entry) is True


class TestProbeUnavailable:
    """The messages-API headroom probe fallback (default OFF). A usage-429-backed-off
    idle account is probed only when ``probe_unavailable=True``; a 2xx synthesizes a
    ``signal="probe"`` view so a stranded session can resume onto it."""

    def _backed_off(self, sw):
        # Account "2" has a usage-429 backoff marker active (retry_until future).
        write_cache(
            sw.backup_dir / "cache" / "usage.json",
            {"2": {"_unavailable": True, "retry_until": time.time() + 3600}},
        )
        return registry.read_registry(sw)

    def test_probe_default_off_never_probes(self, temp_home: Path):
        sw = ClaudeAccountSwitcher()
        _seed_accounts(sw, {"2": ("b@x.com", 0)})
        reg = self._backed_off(sw)
        with patch("claude_swap.registry._probe_idle_headroom") as m:
            acct_views, _ = registry.build_world(sw, reg, fetch_idle=True)  # default off
        assert acct_views["2"].signal == "none"
        m.assert_not_called()

    def test_probe_confirms_headroom(self, temp_home: Path):
        from claude_swap import balancer
        from claude_swap.cache import probe_ok, read_cache

        sw = ClaudeAccountSwitcher()
        _seed_accounts(sw, {"2": ("b@x.com", 0)})
        reg = self._backed_off(sw)
        with patch("claude_swap.registry._probe_idle_headroom", return_value=True):
            acct_views, _ = registry.build_world(
                sw, reg, fetch_idle=True, probe_unavailable=True
            )
        av = acct_views["2"]
        assert av.signal == "probe"
        assert av.max_pct == balancer.PROBE_CONFIRMED_PCT
        # The OK verdict is cached so the next pass reuses it without re-probing.
        entry = read_cache(sw.backup_dir / "cache" / "usage.json", 10**9).get("2")
        assert probe_ok(entry) is True

    def test_probe_ok_verdict_reused_on_second_pass_without_reprobing(self, temp_home: Path):
        # Regression for the resume-path blocker: a cached probe-OK verdict carries no
        # _unavailable/retry_until, so without an explicit reuse branch the SECOND
        # build_world pass (the one _reassign_for_resume runs) would fall through to
        # signal="cache"/max_pct=None — a zero-headroom account that is never a
        # migration target — silently un-pausing the stranded session onto its still-
        # exhausted account. Drive build_world TWICE in-process and assert the second
        # pass STILL yields signal="probe" with NO second network probe.
        from claude_swap import balancer
        from claude_swap.cache import PROBE_VERDICT_TTL_S

        sw = ClaudeAccountSwitcher()
        _seed_accounts(sw, {"2": ("b@x.com", 0)})
        reg = self._backed_off(sw)
        with patch("claude_swap.registry._probe_idle_headroom", return_value=True) as m:
            first, _ = registry.build_world(
                sw, reg, fetch_idle=True, probe_unavailable=True
            )
            assert first["2"].signal == "probe"
            assert m.call_count == 1

            # Second pass: reuse the cached _probe_ok verdict WITHOUT re-probing.
            second, _ = registry.build_world(
                sw, reg, fetch_idle=True, probe_unavailable=True
            )
            assert second["2"].signal == "probe"
            assert second["2"].max_pct == balancer.PROBE_CONFIRMED_PCT
            assert m.call_count == 1  # NOT re-probed

        # The verdict is read off the persisted (inf-TTL) slot, so it survives the
        # 60s freshness TTL through the full PROBE_VERDICT_TTL_S cadence. Confirm the
        # cadence helper still considers it fresh well past the freshness window.
        assert PROBE_VERDICT_TTL_S > 60

    def test_probe_429_refreshes_backoff(self, temp_home: Path):
        from claude_swap.cache import read_cache, usage_backoff_active

        sw = ClaudeAccountSwitcher()
        _seed_accounts(sw, {"2": ("b@x.com", 0)})
        reg = self._backed_off(sw)

        def fake_probe(switcher, num, email, *, is_active=False, failure_out=None):
            if failure_out is not None:
                failure_out["retry_after"] = 1800
            return False

        with patch("claude_swap.registry._probe_idle_headroom", side_effect=fake_probe):
            acct_views, _ = registry.build_world(
                sw, reg, fetch_idle=True, probe_unavailable=True
            )
        assert acct_views["2"].signal == "none"
        entry = read_cache(sw.backup_dir / "cache" / "usage.json", 10**9).get("2")
        assert usage_backoff_active(entry) is True          # retry_until in the future
        assert isinstance(entry.get("_probed_at"), (int, float))  # probe stamped

    def test_probe_unknown_throttles_then_reprobes_when_stale(self, temp_home: Path):
        from claude_swap.cache import PROBE_VERDICT_TTL_S, read_cache

        sw = ClaudeAccountSwitcher()
        _seed_accounts(sw, {"2": ("b@x.com", 0)})
        reg = self._backed_off(sw)

        cp = sw.backup_dir / "cache" / "usage.json"
        with patch("claude_swap.registry._probe_idle_headroom", return_value=None) as m:
            acct_views, _ = registry.build_world(
                sw, reg, fetch_idle=True, probe_unavailable=True
            )
            assert acct_views["2"].signal == "none"
            assert m.call_count == 1
            # Marker stamped, NO retry_until (elapses on the usage TTL normally).
            entry = read_cache(cp, 10**9).get("2")
            assert entry.get("_unavailable") is True
            assert "retry_until" not in entry
            assert isinstance(entry.get("_probed_at"), (int, float))

            # A SECOND pass within the cadence does NOT re-probe (probe_recent gate).
            registry.build_world(sw, reg, fetch_idle=True, probe_unavailable=True)
            assert m.call_count == 1

            # Backdate the stamp past the cadence -> the next pass DOES re-probe.
            stale = dict(read_cache(cp, 10**9))
            stale["2"] = dict(stale["2"])
            stale["2"]["_probed_at"] = time.time() - (PROBE_VERDICT_TTL_S + 1)
            write_cache(cp, stale)
            registry.build_world(sw, reg, fetch_idle=True, probe_unavailable=True)
            assert m.call_count == 2

    def test_probe_stamps_claim_under_lock_before_network(self, temp_home: Path):
        # Cross-process herd guard (finding #4): before the (out-of-lock) network
        # probe fires, build_world must compare-and-set a provisional claim stamp
        # under the lock so a concurrently-waking supervisor that re-reads the slot
        # sees a fresh verdict and bows out instead of also probing. We assert the
        # slot is stamped fresh AT THE MOMENT the probe runs (inside the patched
        # _probe_idle_headroom), proving the claim landed before the network call.
        from claude_swap.cache import probe_recent, read_cache

        sw = ClaudeAccountSwitcher()
        _seed_accounts(sw, {"2": ("b@x.com", 0)})
        reg = self._backed_off(sw)
        cp = sw.backup_dir / "cache" / "usage.json"
        observed: dict = {}

        def fake_probe(switcher, num, email, *, is_active=False, failure_out=None):
            # At this point the claim must already be persisted: a second supervisor
            # re-reading now would see a fresh stamp and skip its own probe.
            entry = read_cache(cp, 10**9).get(num)
            observed["claim_recent"] = probe_recent(entry)
            return True

        with patch("claude_swap.registry._probe_idle_headroom", side_effect=fake_probe):
            registry.build_world(sw, reg, fetch_idle=True, probe_unavailable=True)
        assert observed.get("claim_recent") is True

    def test_probe_never_touches_active_account(self, temp_home: Path):
        sw = ClaudeAccountSwitcher()
        _seed_accounts(sw, {"2": ("b@x.com", 0)})
        reg = self._backed_off(sw)
        with patch("claude_swap.registry._probe_idle_headroom") as m, \
             patch.object(ClaudeAccountSwitcher, "active_account_num", return_value="2"):
            acct_views, _ = registry.build_world(
                sw, reg, fetch_idle=True, probe_unavailable=True
            )
        # The active default's creds are owned by Claude Code -> never probed.
        assert acct_views["2"].signal == "none"
        m.assert_not_called()

    def test_probe_never_touches_live_session_account(self, temp_home: Path):
        sw = ClaudeAccountSwitcher()
        _seed_accounts(sw, {"2": ("b@x.com", 0)})
        reg = self._backed_off(sw)
        with patch("claude_swap.registry._probe_idle_headroom") as m, \
             patch.object(ClaudeAccountSwitcher, "active_account_num", return_value=None), \
             patch.object(ClaudeAccountSwitcher, "has_live_session",
                          side_effect=lambda num, email: num == "2"):
            acct_views, _ = registry.build_world(
                sw, reg, fetch_idle=True, probe_unavailable=True
            )
        assert acct_views["2"].signal == "none"
        m.assert_not_called()

    def test_probe_skips_non_subscription_account(self, temp_home: Path):
        # Credential-safety: the probe sends the same billable /v1/messages turn as
        # priming, so it MUST carry priming's subscription-tier gate. A pay-as-you-go
        # / API / console account (no primeable subscriptionType) must NOT be probed —
        # a billed 2xx there would be misread as ~80% subscription headroom and charge
        # real dollars, defeating onlySubscriptionTokens. The network probe must never
        # be reached; the verdict is "unknown" (None) -> signal stays "none".
        import json

        sw = ClaudeAccountSwitcher()
        _seed_accounts(sw, {"2": ("b@x.com", 0)})
        reg = self._backed_off(sw)
        # API/console-style creds: no subscriptionType (is_primable_subscription False).
        api_creds = json.dumps({"claudeAiOauth": {"accessToken": "tok-api"}})
        with patch.object(
            ClaudeAccountSwitcher, "read_account_credentials", return_value=api_creds
        ), patch.object(
            ClaudeAccountSwitcher, "active_account_num", return_value=None
        ), patch.object(
            ClaudeAccountSwitcher, "has_live_session", return_value=False
        ), patch("claude_swap.oauth.probe_messages_headroom") as net:
            acct_views, _ = registry.build_world(
                sw, reg, fetch_idle=True, probe_unavailable=True
            )
        assert acct_views["2"].signal == "none"   # never a target -> no billing
        net.assert_not_called()                    # the billable turn never fired

    def test_probe_idle_headroom_gates_non_subscription(self, temp_home: Path):
        # Unit-level: the real _probe_idle_headroom returns None (unknown) for a
        # non-subscription account and never calls the billable messages endpoint.
        import json

        sw = ClaudeAccountSwitcher()
        _seed_accounts(sw, {"2": ("b@x.com", 0)})
        api_creds = json.dumps({"claudeAiOauth": {"accessToken": "tok-api"}})
        with patch.object(
            ClaudeAccountSwitcher, "read_account_credentials", return_value=api_creds
        ), patch("claude_swap.oauth.probe_messages_headroom") as net:
            verdict = registry._probe_idle_headroom(sw, "2", "b@x.com")
        assert verdict is None
        net.assert_not_called()

    def test_probe_on_within_ttl_failure_marker(self, temp_home: Path):
        # A plain (non-429) "_unavailable" marker within the freshness TTL is also a
        # probe candidate when probe_unavailable=True.
        from claude_swap import balancer

        sw = ClaudeAccountSwitcher()
        _seed_accounts(sw, {"2": ("b@x.com", 0)})
        write_cache(
            sw.backup_dir / "cache" / "usage.json",
            {"2": {"_unavailable": True}},  # no retry_until -> within-TTL failure
        )
        reg = registry.read_registry(sw)
        with patch("claude_swap.registry._probe_idle_headroom", return_value=True):
            acct_views, _ = registry.build_world(
                sw, reg, fetch_idle=True, probe_unavailable=True
            )
        assert acct_views["2"].signal == "probe"
        assert acct_views["2"].max_pct == balancer.PROBE_CONFIRMED_PCT


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
