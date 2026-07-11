"""Tests for the auto-switch engine (autoswitch.py)."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import oauth, poll_policy
from claude_swap.autoswitch import (
    IDLE_HOLD_MAX_S,
    NO_RESET_FALLBACK_S,
    AllExhaustedEvent,
    AutoSwitchEngine,
    ConfigWarningEvent,
    ErrorEvent,
    NoSwitchEvent,
    PollEvent,
    QuarantineEvent,
    SwitchEvent,
    TickOutcome,
    UnquarantineEvent,
    pct_label,
)
from claude_swap.json_output import USAGE_TOKEN_EXPIRED
from claude_swap.usage_store import FetchRecord, UsageEntry
from claude_swap.models import Platform
from claude_swap.settings import AutoSwitchSettings
from claude_swap.switcher import ClaudeAccountSwitcher


class FakeClock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _usage(pct: float, resets_at: str | None = None) -> dict:
    window: dict = {"pct": pct}
    if resets_at:
        window["resets_at"] = resets_at
    return {"five_hour": window, "seven_day": {"pct": 0.0}}


def _entry_for(value: dict | str | None, now: float) -> UsageEntry:
    """Synthesize the store entry a live fetch would have produced."""
    if isinstance(value, dict):
        return UsageEntry(last_good=value, fetched_at=now, age_s=0.0)
    if isinstance(value, str):
        return UsageEntry(sentinel=value)
    return UsageEntry()


class EngineHarness:
    """Seeded switcher + engine + captured events, on the Linux file backend."""

    def __init__(self, temp_home: Path, **settings_kwargs):
        self.temp_home = temp_home
        self.switcher = ClaudeAccountSwitcher()
        self.switcher.platform = Platform.LINUX
        self.switcher._setup_directories()
        self.switcher._init_sequence_file()
        self.settings = AutoSwitchSettings(**settings_kwargs)
        self.events: list = []
        self.clock = FakeClock()
        # Keep the usage store on the same fake clock as the engine so
        # freshness/claims/poll scheduling are deterministic in tests.
        self.switcher._usage_store.clock = self.clock
        self.engine = self._make_engine()

    def _make_engine(self, **kwargs) -> AutoSwitchEngine:
        return AutoSwitchEngine(
            self.switcher,
            self.settings,
            self.events.append,
            clock=self.clock,
            **kwargs,
        )

    def seed(self, num: int, email: str, *, expires_at: int | None = None) -> None:
        oauth_blob: dict = {
            "accessToken": f"sk-{num}",
            "refreshToken": f"rt-{num}",
        }
        if expires_at is not None:
            oauth_blob["expiresAt"] = expires_at
        self.switcher._write_account_credentials(
            str(num), email, json.dumps({"claudeAiOauth": oauth_blob})
        )
        self.switcher._write_account_config(
            str(num),
            email,
            json.dumps({
                "oauthAccount": {"emailAddress": email, "accountUuid": f"uuid-{num}"},
            }),
        )
        data = self.switcher._get_sequence_data()
        data["accounts"][str(num)] = {
            "email": email,
            "uuid": f"uuid-{num}",
            "organizationUuid": "",
            "organizationName": "",
            "added": "2024-01-01T00:00:00Z",
        }
        if num not in data["sequence"]:
            data["sequence"].append(num)
            data["sequence"].sort()
        if data["activeAccountNumber"] is None:
            data["activeAccountNumber"] = num
        self.switcher._write_json(self.switcher.sequence_file, data)

    def make_live(self, email: str, num: int) -> None:
        (self.temp_home / ".claude" / ".credentials.json").write_text(json.dumps({
            "claudeAiOauth": {"accessToken": "sk-live", "refreshToken": "rt-live"},
        }))
        (self.temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": email, "accountUuid": f"uuid-{num}"},
        }))

    def tick_with_usage(self, usage: dict) -> TickOutcome:
        entries = {
            num: _entry_for(value, self.clock.now) for num, value in usage.items()
        }
        return self.tick_with_entries(entries)

    def tick_with_entries(self, entries: dict[str, UsageEntry]) -> TickOutcome:
        with patch.object(
            self.switcher, "usage_entries_by_account", return_value=entries
        ):
            return self.engine.tick()

    def active_number(self) -> int | None:
        return self.switcher._get_sequence_data()["activeAccountNumber"]

    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]

    def state(self) -> dict:
        path = self.switcher.backup_dir / "autoswitch_state.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text())


@pytest.fixture
def harness(temp_home: Path) -> EngineHarness:
    h = EngineHarness(temp_home)
    h.seed(1, "a@example.com")
    h.seed(2, "b@example.com")
    h.seed(3, "c@example.com")
    h.make_live("a@example.com", 1)
    return h


class TestDecisionTable:
    def test_below_threshold_is_no_action(self, harness):
        outcome = harness.tick_with_usage({
            "1": _usage(50), "2": _usage(10), "3": _usage(10),
        })
        assert outcome is TickOutcome.NO_ACTION
        assert harness.active_number() == 1
        reasons = [e.reason for e in harness.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["below-threshold"]

    def test_over_threshold_switches_to_max_headroom(self, harness):
        outcome = harness.tick_with_usage({
            "1": _usage(95), "2": _usage(40), "3": _usage(20),
        })
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 3
        switch = next(e for e in harness.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "proactive"
        assert switch.to_ref == {"number": 3, "email": "c@example.com"}
        assert harness.state()["lastSwitchTo"] == "3"

    def test_no_active_account(self, temp_home):
        h = EngineHarness(temp_home)
        assert h.engine.tick() is TickOutcome.NO_ACTION
        assert [e.reason for e in h.events if isinstance(e, NoSwitchEvent)] == [
            "no-active-account"
        ]

    def test_hysteresis_margin_blocks_marginal_candidates(self, harness):
        # threshold 90, hysteresis 10 → a candidate must beat the active
        # account's utilization by >= 10 points; 95→86 is only 9 better.
        # Failing the margin is NOT exhaustion: no all-exhausted event, no
        # reset-sleep — the next tick must stay at normal cadence so the
        # at-limit escape isn't missed when the active account tops out.
        outcome = harness.tick_with_usage({
            "1": _usage(95), "2": _usage(86), "3": _usage(88),
        })
        assert outcome is TickOutcome.BLOCKED
        assert harness.active_number() == 1
        assert not any(isinstance(e, AllExhaustedEvent) for e in harness.events)
        reasons = [e.reason for e in harness.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["no-qualifying-candidate"]
        assert harness.engine._sleep_until_ts is None
        delay = harness.engine._next_delay(outcome)
        assert delay <= 1.1 * harness.settings.interval_seconds

    def test_issue_115_strictly_better_candidate_switches(self, harness):
        # Regression for #115: active bound by 5h (99%), candidate bound by
        # 7d (89%). The old absolute bar (<= 80% used) vetoed the candidate;
        # the relative gate takes it: 89 < 90 and 99 - 89 >= 10.
        outcome = harness.tick_with_usage({
            "1": {"five_hour": {"pct": 99.0}, "seven_day": {"pct": 24.0}},
            "2": {"five_hour": {"pct": 3.0}, "seven_day": {"pct": 89.0}},
            "3": {"five_hour": {"pct": 95.0}, "seven_day": {"pct": 10.0}},
        })
        assert outcome is TickOutcome.SWITCHED
        switch = next(e for e in harness.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "proactive"
        assert harness.active_number() == 2

    def test_proactive_never_lands_at_or_over_threshold(self, temp_home):
        # threshold 80, hysteresis 5: the candidate at 85% is five points
        # better than the active 90%, but it already sits over the threshold
        # and would re-trigger on the very next tick — blocked.
        h = EngineHarness(temp_home, threshold=80.0, hysteresis_pct=5.0)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)
        outcome = h.tick_with_usage({"1": _usage(90), "2": _usage(85)})
        assert outcome is TickOutcome.BLOCKED
        assert h.active_number() == 1
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["no-qualifying-candidate"]

    def test_stable_landing_does_not_switch_back(self, temp_home):
        # Cooldown disabled so only the gate itself prevents flapping: after
        # 99→89 the roles reverse, and the old account (99%) can never beat
        # the new active (89%) — the move is one-way.
        h = EngineHarness(temp_home, cooldown_seconds=0.0)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)
        usage = {
            "1": {"five_hour": {"pct": 99.0}, "seven_day": {"pct": 24.0}},
            "2": {"five_hour": {"pct": 3.0}, "seven_day": {"pct": 89.0}},
        }
        assert h.tick_with_usage(usage) is TickOutcome.SWITCHED
        assert h.active_number() == 2
        h.clock.advance(60)
        assert h.tick_with_usage(usage) is TickOutcome.NO_ACTION
        assert h.active_number() == 2
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["below-threshold"]

    def test_mixed_unknown_and_exhausted_is_not_all_exhausted(self, harness):
        # One candidate at its limit, the other unreadable this tick: usage
        # could recover any moment, so no long reset-sleep.
        outcome = harness.tick_with_usage({
            "1": _usage(95),
            "2": _usage(100, "2026-07-03T12:00:00Z"),
            "3": None,
        })
        assert outcome is TickOutcome.BLOCKED
        assert not any(isinstance(e, AllExhaustedEvent) for e in harness.events)
        reasons = [e.reason for e in harness.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["no-qualifying-candidate"]
        assert harness.engine._sleep_until_ts is None
        delay = harness.engine._next_delay(outcome)
        assert delay <= 1.1 * harness.settings.interval_seconds

    def test_stale_beyond_trust_blocks_all_exhausted(self, harness):
        # One candidate exhausted on trusted-stale data, the other's data aged
        # past every trust window (no failures, no plan — just overdue): the
        # unknown candidate could be viable, so no long reset-sleep.
        now = harness.clock.now
        reset = "2026-07-05T12:00:00Z"
        outcome = harness.tick_with_entries({
            "1": UsageEntry(last_good=_usage(95), fetched_at=now, age_s=0.0),
            "2": UsageEntry(
                last_good=_usage(100, reset), fetched_at=now - 400, age_s=400.0,
                consecutive_failures=1, trust_extended=True,
            ),
            "3": UsageEntry(last_good=_usage(10), fetched_at=now - 400, age_s=400.0),
        })
        assert outcome is TickOutcome.BLOCKED
        assert not any(isinstance(e, AllExhaustedEvent) for e in harness.events)
        reasons = [e.reason for e in harness.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["no-qualifying-candidate"]

    def test_trusted_stale_exhausted_set_still_fires_all_exhausted(self, harness):
        # Every candidate at its limit, known only through trusted-stale data
        # (in failure state) — that is still "known and exhausted".
        now = harness.clock.now
        reset = "2026-07-05T12:00:00Z"
        stale_exhausted = UsageEntry(
            last_good=_usage(100, reset), fetched_at=now - 400, age_s=400.0,
            consecutive_failures=1, trust_extended=True,
        )
        outcome = harness.tick_with_entries({
            "1": UsageEntry(last_good=_usage(95), fetched_at=now, age_s=0.0),
            "2": stale_exhausted,
            "3": stale_exhausted,
        })
        assert outcome is TickOutcome.BLOCKED
        exhausted = next(
            e for e in harness.events if isinstance(e, AllExhaustedEvent)
        )
        assert exhausted.earliest_reset_at == reset

    def test_cooldown_suppresses_proactive(self, harness):
        harness.engine._mutate_state(
            lambda s: s.update(lastSwitchAt=harness.clock() - 10)
        )
        outcome = harness.tick_with_usage({
            "1": _usage(95), "2": _usage(10), "3": _usage(10),
        })
        assert outcome is TickOutcome.NO_ACTION
        assert [e.reason for e in harness.events if isinstance(e, NoSwitchEvent)] == [
            "cooldown"
        ]

    def test_at_limit_bypasses_cooldown(self, harness):
        harness.engine._mutate_state(
            lambda s: s.update(lastSwitchAt=harness.clock() - 10)
        )
        outcome = harness.tick_with_usage({
            "1": _usage(100), "2": _usage(10), "3": _usage(50),
        })
        assert outcome is TickOutcome.SWITCHED
        switch = next(e for e in harness.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "at-limit"
        assert harness.active_number() == 2

    def test_cooldown_expires(self, harness):
        harness.engine._mutate_state(
            lambda s: s.update(lastSwitchAt=harness.clock())
        )
        harness.clock.advance(400)  # past the 300s default cooldown
        outcome = harness.tick_with_usage({
            "1": _usage(95), "2": _usage(10), "3": _usage(50),
        })
        assert outcome is TickOutcome.SWITCHED

    def test_unknown_active_usage_waits_then_fails_over(self, harness):
        usage = {"1": None, "2": _usage(10), "3": _usage(50)}
        assert harness.tick_with_usage(usage) is TickOutcome.NO_ACTION
        assert harness.tick_with_usage(usage) is TickOutcome.NO_ACTION
        assert harness.tick_with_usage(usage) is TickOutcome.SWITCHED
        switch = next(e for e in harness.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "failover"
        assert harness.active_number() == 2

    def test_known_active_usage_resets_unhealthy_counter(self, harness):
        unknown = {"1": None, "2": _usage(10), "3": _usage(10)}
        healthy = {"1": _usage(50), "2": _usage(10), "3": _usage(10)}
        harness.tick_with_usage(unknown)
        harness.tick_with_usage(unknown)
        harness.tick_with_usage(healthy)  # resets the counter
        assert harness.tick_with_usage(unknown) is TickOutcome.NO_ACTION
        assert harness.active_number() == 1

    def test_all_candidates_unknown_is_no_comparison(self, harness):
        outcome = harness.tick_with_usage({
            "1": _usage(95), "2": None, "3": None,
        })
        assert outcome is TickOutcome.BLOCKED
        assert [e.reason for e in harness.events if isinstance(e, NoSwitchEvent)] == [
            "no-comparison"
        ]

    def test_tie_resolves_to_earliest_slot(self, harness):
        outcome = harness.tick_with_usage({
            "1": _usage(95), "2": _usage(30), "3": _usage(30),
        })
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 2

    def test_candidate_not_better_than_active_is_skipped(self, harness):
        # Active 91% used (9 headroom); candidates worse or equal → exhausted.
        outcome = harness.tick_with_usage({
            "1": _usage(91), "2": _usage(95), "3": _usage(99),
        })
        assert outcome is TickOutcome.BLOCKED
        assert harness.active_number() == 1

    def test_at_limit_escapes_hysteresis_bar(self, harness):
        # Active hard at 100%; the only room anywhere is a candidate at 85%,
        # which the proactive hysteresis bar (<=80%) would reject. At-limit is
        # an escape: any account with real headroom beats a blocked one.
        outcome = harness.tick_with_usage({
            "1": _usage(100), "2": _usage(85), "3": _usage(97),
        })
        assert outcome is TickOutcome.SWITCHED
        switch = next(e for e in harness.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "at-limit"
        assert harness.active_number() == 2

    def test_at_limit_never_targets_another_at_limit_account(self, harness):
        outcome = harness.tick_with_usage({
            "1": _usage(100), "2": _usage(100), "3": _usage(100),
        })
        assert outcome is TickOutcome.BLOCKED
        assert harness.active_number() == 1

    def test_failover_ignores_hysteresis_bar(self, harness):
        # Active usage unreadable (auth likely dead); the only candidate with
        # room sits above the hysteresis bar — failover takes it anyway.
        usage = {"1": None, "2": _usage(85), "3": _usage(100)}
        harness.tick_with_usage(usage)
        harness.tick_with_usage(usage)
        outcome = harness.tick_with_usage(usage)
        assert outcome is TickOutcome.SWITCHED
        switch = next(e for e in harness.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "failover"
        assert harness.active_number() == 2

    def test_unmanaged_live_login_is_never_touched(self, temp_home):
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        # The user logged in with an account cswap doesn't manage.
        h.make_live("stranger@example.com", 9)
        live_before = (temp_home / ".claude" / ".credentials.json").read_text()
        outcome = h.tick_with_usage({"1": _usage(95), "2": _usage(10)})
        assert outcome is TickOutcome.NO_ACTION
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["unmanaged-active-account"]
        assert (temp_home / ".claude" / ".credentials.json").read_text() == live_before

    def test_all_exhausted_carries_earliest_reset(self, harness):
        outcome = harness.tick_with_usage({
            "1": _usage(100, "2026-07-03T12:00:00Z"),
            "2": _usage(100, "2026-07-03T10:30:00Z"),
            "3": _usage(100, "2026-07-03T11:00:00Z"),
        })
        assert outcome is TickOutcome.BLOCKED
        event = next(e for e in harness.events if isinstance(e, AllExhaustedEvent))
        assert event.earliest_reset_at == "2026-07-03T10:30:00Z"
        assert harness.engine._sleep_until_ts is not None


class TestIdleHold:
    """Active token expired while Claude Code owns it → hold, don't fail over."""

    _HELD = {"1": USAGE_TOKEN_EXPIRED, "2": _usage(10), "3": _usage(20)}

    def test_token_expired_holds_instead_of_failover(self, harness):
        for _ in range(6):  # far past unhealthy_ticks (3)
            assert harness.tick_with_usage(self._HELD) is TickOutcome.NO_ACTION
            harness.clock.advance(60)
        assert harness.active_number() == 1
        assert not any(isinstance(e, SwitchEvent) for e in harness.events)
        reasons = {e.reason for e in harness.events if isinstance(e, NoSwitchEvent)}
        assert reasons == {"active-idle"}
        assert harness.engine._unhealthy_ticks == 0

    def test_idle_hold_slows_cadence(self, harness):
        outcome = harness.tick_with_usage(self._HELD)
        assert outcome is TickOutcome.NO_ACTION
        assert harness.engine._next_delay(outcome) >= NO_RESET_FALLBACK_S

    def test_idle_hold_cap_escalates_to_failover(self, harness):
        assert harness.tick_with_usage(self._HELD) is TickOutcome.NO_ACTION
        harness.clock.advance(IDLE_HOLD_MAX_S + 1)
        # Past the cap the sentinel counts as unhealthy again → failover after
        # unhealthy_ticks (3) consecutive ticks.
        assert harness.tick_with_usage(self._HELD) is TickOutcome.NO_ACTION
        assert harness.tick_with_usage(self._HELD) is TickOutcome.NO_ACTION
        assert harness.tick_with_usage(self._HELD) is TickOutcome.SWITCHED
        switch = next(e for e in harness.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "failover"

    def test_recovery_resets_the_hold_clock(self, harness):
        healthy = {"1": _usage(50), "2": _usage(10), "3": _usage(20)}
        harness.tick_with_usage(self._HELD)
        harness.clock.advance(IDLE_HOLD_MAX_S - 60)
        harness.tick_with_usage(healthy)  # user came back; token refreshed
        harness.clock.advance(120)
        # New expiry long after: the hold clock restarted, so still held.
        assert harness.tick_with_usage(self._HELD) is TickOutcome.NO_ACTION
        assert harness.engine._unhealthy_ticks == 0
        assert harness.active_number() == 1

    def test_plain_fetch_failure_still_counts_unhealthy(self, harness):
        # A None (network failure / dead creds) is NOT the idle sentinel:
        # unhealthy counting and the hold clock reset both apply.
        harness.tick_with_usage(self._HELD)
        unknown = {"1": None, "2": _usage(10), "3": _usage(20)}
        assert harness.tick_with_usage(unknown) is TickOutcome.NO_ACTION
        assert harness.engine._unhealthy_ticks == 1
        assert harness.engine._idle_hold_since is None


class TestAdaptiveScheduler:
    """End-to-end through the real store: O(1) baseline, escalations,
    skip-to-reset, movement-based cadence."""

    def _harness(self, temp_home, monkeypatch, accounts=3, **settings_kwargs):
        monkeypatch.setattr("claude_swap.switcher._FETCH_STAGGER_S", 0)
        h = EngineHarness(temp_home, **settings_kwargs)
        emails = ["a@example.com", "b@example.com", "c@example.com"]
        for num in range(1, accounts + 1):
            h.seed(num, emails[num - 1])
        h.make_live("a@example.com", 1)
        # Deterministic owner detection: Claude Code "running" → the active
        # account is fetched hands-off (is_active=True), never refreshed.
        monkeypatch.setattr(h.switcher, "_active_cc_running", lambda: True)
        monkeypatch.setattr(h.switcher, "_live_session_pids", lambda *a: [])
        return h

    @staticmethod
    def _counting_fetch(counts, usage_by_num, errors_by_num=None):
        def fake(num, email, creds, is_active=False, persist_credentials=None):
            counts[num] = counts.get(num, 0) + 1
            error = (errors_by_num or {}).get(num)
            if error:
                return oauth.UsageOutcome(None, error=error)
            value = usage_by_num.get(num)
            return oauth.UsageOutcome(dict(value) if value else None)
        return fake

    def _tick(self, h, counts, usage_by_num, errors_by_num=None):
        with patch(
            "claude_swap.oauth.try_fetch_usage_for_account",
            side_effect=self._counting_fetch(counts, usage_by_num, errors_by_num),
        ):
            return h.engine.tick()

    def test_baseline_fetches_active_plus_one_candidate(self, temp_home, monkeypatch):
        h = self._harness(temp_home, monkeypatch)
        usage = {"1": _usage(50), "2": _usage(10), "3": _usage(20)}
        counts: dict[str, int] = {}
        # t0: active (never fetched) + the stalest candidate.
        self._tick(h, counts, usage)
        assert counts == {"1": 1, "2": 1}
        # t60: active planned MIN_INTERVAL_S out; the never-fetched candidate
        # is the due one.
        h.clock.advance(60)
        self._tick(h, counts, usage)
        assert counts == {"1": 1, "2": 1, "3": 1}
        # t120: nobody due — everyone served from the store.
        h.clock.advance(60)
        self._tick(h, counts, usage)
        assert counts == {"1": 1, "2": 1, "3": 1}
        # t180: the active account's plan comes due.
        h.clock.advance(60)
        self._tick(h, counts, usage)
        assert counts == {"1": 2, "2": 1, "3": 1}

    def test_near_threshold_escalates_to_full_refresh(self, temp_home, monkeypatch):
        # threshold 90, margin 15 → active at 80% is within the escalation band.
        h = self._harness(temp_home, monkeypatch)
        counts: dict[str, int] = {}
        outcome = self._tick(
            h, counts, {"1": _usage(80), "2": _usage(10), "3": _usage(20)}
        )
        assert outcome is TickOutcome.NO_ACTION  # still below the threshold
        assert counts == {"1": 1, "2": 1, "3": 1}  # but everyone got refreshed

    def test_active_unknown_escalates_before_failover(self, temp_home, monkeypatch):
        h = self._harness(temp_home, monkeypatch, unhealthy_ticks=1)
        counts: dict[str, int] = {}
        outcome = self._tick(
            h, counts,
            {"2": _usage(10), "3": _usage(50)},
            errors_by_num={"1": "timeout"},
        )
        # Candidate data was refreshed in the same tick the failover ran on.
        assert counts == {"1": 1, "2": 1, "3": 1}
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2

    def test_active_cadence_floor_and_decay(self, temp_home, monkeypatch):
        # The active account polls at MIN_INTERVAL_S first; unmoved usage
        # decays the interval ×1.5 toward ACTIVE_MAX_INTERVAL_S.
        h = self._harness(temp_home, monkeypatch, accounts=2)
        usage = {"1": _usage(10), "2": _usage(20)}
        counts: dict[str, int] = {}
        self._tick(h, counts, usage)  # never-fetched → fetched
        assert counts["1"] == 1
        for _ in range(2):  # ages 60s and 120s — inside the 180s floor
            h.clock.advance(60)
            self._tick(h, counts, usage)
        assert counts["1"] == 1
        h.clock.advance(60)  # age 180s → due again
        self._tick(h, counts, usage)
        assert counts["1"] == 2
        # Unmoved → interval decayed to 270s: not due at +240, due at +300.
        h.clock.advance(240)
        self._tick(h, counts, usage)
        assert counts["1"] == 2
        h.clock.advance(60)
        self._tick(h, counts, usage)
        assert counts["1"] == 3

    def test_urgent_cadence_when_burning_near_the_band(self, temp_home, monkeypatch):
        # Active moving inside the escalation band → 60s urgent cadence, so
        # a threshold crossing is seen within a minute of the previous poll.
        h = self._harness(temp_home, monkeypatch, accounts=2)
        usage = {"1": _usage(70), "2": _usage(10)}
        counts: dict[str, int] = {}
        self._tick(h, counts, usage)
        usage["1"] = _usage(80)  # burning: +10 pts, now inside the band
        h.clock.advance(180)
        self._tick(h, counts, usage)  # movement + in band → urgent plan
        assert counts["1"] == 2
        usage["1"] = _usage(84)
        h.clock.advance(60)
        self._tick(h, counts, usage)  # urgent plan due after only 60s
        assert counts["1"] == 3

    def test_in_band_without_movement_keeps_the_floor(self, temp_home, monkeypatch):
        # In the escalation band but not burning: no urgency — the normal
        # 180s floor applies (escalation keeps candidates fresh; it must not
        # re-fetch a fresh, unmoving active every tick).
        h = self._harness(temp_home, monkeypatch, accounts=2)
        usage = {"1": _usage(80), "2": _usage(10)}
        counts: dict[str, int] = {}
        self._tick(h, counts, usage)
        for _ in range(2):
            h.clock.advance(60)
            self._tick(h, counts, usage)
        assert counts["1"] == 1  # not due inside the floor
        h.clock.advance(60)
        self._tick(h, counts, usage)
        assert counts["1"] == 2

    def test_urgent_band_follows_the_threshold(self, temp_home, monkeypatch):
        # The urgent band is distance-to-threshold, not absolute pct: with
        # threshold 50 (band edge 35), movement at 40% engages the urgent
        # cadence that the default threshold would ignore.
        h = self._harness(temp_home, monkeypatch, accounts=2, threshold=50)
        usage = {"1": _usage(30), "2": _usage(10)}
        counts: dict[str, int] = {}
        self._tick(h, counts, usage)
        usage["1"] = _usage(40)
        h.clock.advance(180)
        self._tick(h, counts, usage)  # movement inside the 35..50 band
        assert counts["1"] == 2
        usage["1"] = _usage(44)
        h.clock.advance(60)
        self._tick(h, counts, usage)  # urgent plan due after only 60s
        assert counts["1"] == 3

    def test_stale_candidate_plan_never_gates_the_active(
        self, temp_home, monkeypatch
    ):
        # Role change outside a cswap switch (e.g. manual login): the active
        # slot can carry a plan written while it was an idle candidate, up to
        # 600s out. The ACTIVE_MAX_INTERVAL_S age cap overrides it.
        h = self._harness(temp_home, monkeypatch, accounts=2)
        usage = {"1": _usage(50), "2": _usage(20)}
        counts: dict[str, int] = {}
        self._tick(h, counts, usage)
        h.switcher._usage_store.set_poll_plan(
            {"1": (h.clock.now + 600.0, 600.0)}, {"1": ("a@example.com", "")}
        )
        h.clock.advance(240)  # inside the bogus plan, under the age cap
        self._tick(h, counts, usage)
        assert counts["1"] == 1
        h.clock.advance(120)  # age 360 ≥ ACTIVE_MAX_INTERVAL_S
        self._tick(h, counts, usage)
        assert counts["1"] == 2

    def test_exhausted_active_stays_parked_at_its_reset(
        self, temp_home, monkeypatch
    ):
        from datetime import datetime, timezone

        # The role-change age cap must not defeat reset parking: an exhausted
        # account's numbers cannot move before the reset, so even a
        # candidate-style slow interval leaves it parked (no candidates here,
        # so escalation cannot refetch it either).
        h = self._harness(temp_home, monkeypatch, accounts=1)
        reset_ts = h.clock.now + 7200.0
        reset_iso = (
            datetime.fromtimestamp(reset_ts, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        usage = {"1": _usage(100, reset_iso)}
        counts: dict[str, int] = {}
        self._tick(h, counts, usage)
        assert counts["1"] == 1  # measured once, parked at the reset
        h.switcher._usage_store.set_poll_plan(
            {"1": (reset_ts, 600.0)}, {"1": ("a@example.com", "")}
        )
        for _ in range(3):
            h.clock.advance(400)  # well past the age cap each tick
            self._tick(h, counts, usage)
        assert counts["1"] == 1  # never re-fetched before the reset

    def test_band_jump_is_seen_at_most_one_poll_late(
        self, temp_home, monkeypatch
    ):
        # Active at 40% jumps into the band between polls: the jump is picked
        # up on the next planned poll, escalates the same tick, and the
        # movement flips the active onto the urgent cadence.
        h = self._harness(temp_home, monkeypatch, accounts=2)
        usage = {"1": _usage(40), "2": _usage(20)}
        counts: dict[str, int] = {}
        self._tick(h, counts, usage)
        usage["1"] = _usage(80)
        h.clock.advance(60)
        self._tick(h, counts, usage)  # plan-skipped: still believed at 40%
        assert counts["1"] == 1
        h.clock.advance(120)
        self._tick(h, counts, usage)  # planned poll sees 80% → escalate-all
        assert counts["1"] == 2
        assert counts["2"] == 1  # at the TTL edge: still served, not refetched
        h.clock.advance(60)
        self._tick(h, counts, usage)  # movement in band → urgent cadence
        assert counts["1"] == 3
        assert counts["2"] == 2  # now stale → the escalation refreshes it

    def test_active_in_backoff_keeps_trusted_headroom(self, temp_home, monkeypatch):
        # The active account's fetches are being refused (429 with a long
        # Retry-After). Its last-good data ages past STALE_OK_S, but the
        # staleness is deliberate: headroom stays known, so no unhealthy
        # ticks and no escalate-all burst while the server is rate limiting.
        h = self._harness(temp_home, monkeypatch)
        usage = {"1": _usage(50), "2": _usage(10), "3": _usage(20)}
        counts: dict[str, int] = {}
        self._tick(h, counts, usage)
        h.clock.advance(60)
        self._tick(h, counts, usage)
        h.switcher._usage_store.record(
            {"1": FetchRecord(error="http-429", retry_after_s=600.0)},
            {"1": ("a@example.com", "")},
        )
        h.clock.advance(400)  # active data now well past STALE_OK_S, in backoff
        counts.clear()
        outcome = self._tick(h, counts, usage)
        assert outcome is TickOutcome.NO_ACTION
        assert h.engine._unhealthy_ticks == 0
        assert "1" not in counts  # backoff respected
        assert sum(counts.values()) == 1  # baseline slot only, no escalate-all

    def test_exhausted_candidate_skips_to_its_reset(self, temp_home, monkeypatch):
        from datetime import datetime, timezone

        h = self._harness(temp_home, monkeypatch)
        reset_iso = "2026-07-05T12:00:00Z"
        reset_ts = datetime(2026, 7, 5, 12, tzinfo=timezone.utc).timestamp()
        usage = {"1": _usage(50), "2": _usage(100, reset_iso), "3": _usage(20)}
        counts: dict[str, int] = {}
        for _ in range(3):
            self._tick(h, counts, usage)
            h.clock.advance(60)
        assert counts["2"] == 1  # fetched once, then parked until its reset
        entry = h.switcher._usage_store.entries(
            {"2": ("b@example.com", "")}
        )["2"]
        assert entry.next_poll_at == pytest.approx(reset_ts)

    def test_poll_never_scheduled_past_a_window_reset(self, temp_home, monkeypatch):
        from datetime import datetime, timezone

        from claude_swap.autoswitch import RESET_SLACK_S

        # The candidate's default interval is 300s, but its 5h window resets
        # in 90s — its stored 40% is obsolete at the rollover, so the next
        # poll must be clamped to reset + slack rather than waiting it out.
        h = self._harness(temp_home, monkeypatch, accounts=2)
        reset_ts = h.clock.now + 90.0
        reset_iso = (
            datetime.fromtimestamp(reset_ts, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        usage = {"1": _usage(50), "2": _usage(40, reset_iso)}
        counts: dict[str, int] = {}
        self._tick(h, counts, usage)
        entry = h.switcher._usage_store.entries(
            {"2": ("b@example.com", "")}
        )["2"]
        assert entry.next_poll_at == pytest.approx(reset_ts + RESET_SLACK_S)
        # Learned cadence untouched by the clamp.
        assert entry.poll_interval_s == poll_policy.CANDIDATE_DEFAULT_INTERVAL_S

    def test_movement_adapts_poll_interval(self, temp_home, monkeypatch):
        h = self._harness(temp_home, monkeypatch, accounts=2)
        usage = {"1": _usage(50), "2": _usage(10)}
        counts: dict[str, int] = {}

        def interval() -> float | None:
            return h.switcher._usage_store.entries(
                {"2": ("b@example.com", "")}
            )["2"].poll_interval_s

        self._tick(h, counts, usage)          # first data point → base interval
        assert interval() == poll_policy.CANDIDATE_DEFAULT_INTERVAL_S  # 300s
        h.clock.advance(180)
        self._tick(h, counts, usage)          # not due yet (300s interval)
        assert counts["2"] == 1
        h.clock.advance(120)
        self._tick(h, counts, usage)          # unmoved → backs off ×1.5
        assert counts["2"] == 2
        assert interval() == 450.0
        h.clock.advance(450)
        usage["2"] = _usage(20)               # moved 10 pts on another machine
        self._tick(h, counts, usage)
        assert counts["2"] == 3
        assert interval() == 225.0            # halved: polled closer while moving

    def test_idle_hold_skips_candidate_polling(self, temp_home, monkeypatch):
        h = self._harness(temp_home, monkeypatch)
        # Active token locally expired while "Claude Code is running" (owner
        # patched True) → sentinel without any request.
        (h.temp_home / ".claude" / ".credentials.json").write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-live", "refreshToken": "rt-live",
                "expiresAt": 1000,
            },
        }))
        usage = {"2": _usage(10), "3": _usage(20)}
        counts: dict[str, int] = {}
        assert self._tick(h, counts, usage) is TickOutcome.NO_ACTION
        h.clock.advance(60)
        counts.clear()
        # Hold established → the next tick polls nothing at all: the active
        # fetch short-circuits locally and no candidate slot is spent.
        assert self._tick(h, counts, usage) is TickOutcome.NO_ACTION
        assert counts == {}
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert set(reasons) == {"active-idle"}

    def test_poll_event_carries_fetch_errors(self, temp_home, monkeypatch):
        h = self._harness(temp_home, monkeypatch, accounts=2, unhealthy_ticks=3)
        counts: dict[str, int] = {}
        self._tick(
            h, counts, {"2": _usage(10)}, errors_by_num={"1": "http-429"}
        )
        poll = next(e for e in h.events if isinstance(e, PollEvent))
        assert poll.fetch_errors.get("1") == "http-429"
        assert "http-429" in poll.human()
        assert poll.to_json()["fetchErrors"] == {"1": "http-429"}

    def test_quarantined_candidate_never_consumes_the_poll_slot(
        self, temp_home, monkeypatch
    ):
        h = self._harness(temp_home, monkeypatch)
        h.engine._quarantine("2", "b@example.com", "invalid_grant")
        usage = {"1": _usage(50), "2": _usage(10), "3": _usage(20)}
        counts: dict[str, int] = {}
        for _ in range(3):
            self._tick(h, counts, usage)
            h.clock.advance(60)
        # The alternate slot always went to account 3; 2 is dead weight.
        assert "2" not in counts
        assert counts["3"] >= 1

    def test_expired_active_enters_idle_hold_even_during_backoff(
        self, temp_home, monkeypatch
    ):
        """Finding-2 regression: the owned+expired sentinel must not be hidden
        by the active row's failure backoff (e.g. a Retry-After window), or
        the engine would count unhealthy ticks toward a spurious failover."""
        from claude_swap.usage_store import FetchRecord

        h = self._harness(temp_home, monkeypatch)
        # Active token locally expired while an owner is present.
        (h.temp_home / ".claude" / ".credentials.json").write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-live", "refreshToken": "rt-live",
                "expiresAt": 1000,
            },
        }))
        # Active row sits in a long failure backoff → the fetch path (and its
        # own expired short-circuit) is unreachable this tick.
        h.switcher._usage_store.record(
            {"1": FetchRecord(error="http-429", retry_after_s=600.0)},
            {"1": ("a@example.com", "")},
        )
        counts: dict[str, int] = {}
        outcome = self._tick(h, counts, {"2": _usage(10), "3": _usage(20)})
        assert outcome is TickOutcome.NO_ACTION
        assert h.engine._unhealthy_ticks == 0
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["active-idle"]


class TestApiKeyAccounts:
    def _mark_api_key(self, harness, num: int) -> None:
        data = harness.switcher._get_sequence_data()
        data["accounts"][str(num)]["kind"] = "api_key"
        harness.switcher._write_json(harness.switcher.sequence_file, data)

    def test_api_key_candidate_excluded_by_default(self, temp_home):
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "key@token.local")
        h.make_live("a@example.com", 1)
        self._mark_api_key(h, 2)
        outcome = h.tick_with_usage({"1": _usage(95), "2": "api key"})
        assert outcome is TickOutcome.BLOCKED
        assert h.active_number() == 1

    def test_api_key_is_last_resort_when_included(self, temp_home):
        h = EngineHarness(temp_home, include_api_key_accounts=True)
        h.seed(1, "a@example.com")
        h.seed(2, "key@token.local")
        h.seed(3, "c@example.com")
        h.make_live("a@example.com", 1)
        self._mark_api_key(h, 2)
        # A qualifying OAuth candidate wins over the API key...
        outcome = h.tick_with_usage({
            "1": _usage(95), "2": "api key", "3": _usage(10),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3

    def test_api_key_used_when_oauth_exhausted(self, temp_home):
        h = EngineHarness(temp_home, include_api_key_accounts=True)
        h.seed(1, "a@example.com")
        h.seed(2, "key@token.local")
        h.seed(3, "c@example.com")
        h.make_live("a@example.com", 1)
        self._mark_api_key(h, 2)
        outcome = h.tick_with_usage({
            "1": _usage(100), "2": "api key", "3": _usage(100),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2

    def test_active_api_key_idles_engine(self, temp_home):
        h = EngineHarness(temp_home)
        h.seed(1, "key@token.local")
        h.seed(2, "b@example.com")
        h.make_live("key@token.local", 1)
        self._mark_api_key(h, 1)
        outcome = h.tick_with_usage({"1": "api key", "2": _usage(10)})
        assert outcome is TickOutcome.NO_ACTION
        assert [e.reason for e in h.events if isinstance(e, NoSwitchEvent)] == [
            "active-api-key"
        ]


class TestFreshening:
    def test_near_expiry_target_is_refreshed_and_persisted(self, temp_home):
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com", expires_at=int(h.clock() * 1000) + 60_000)
        h.make_live("a@example.com", 1)

        rotated = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-2-new",
                "refreshToken": "rt-2-new",
                "expiresAt": int(h.clock() * 1000) + 3_600_000,
            }
        })
        live_creds_path = temp_home / ".claude" / ".credentials.json"
        live_before = live_creds_path.read_text()
        with patch(
            "claude_swap.autoswitch.oauth.try_refresh_oauth_credentials",
            return_value=oauth.RefreshOutcome(rotated, None),
        ) as mock_refresh:
            outcome = h.tick_with_usage({"1": _usage(95), "2": _usage(10)})

        assert outcome is TickOutcome.SWITCHED
        mock_refresh.assert_called_once()
        # Freshening itself never touched the active store (the switch did,
        # afterwards, via _perform_switch): the rotated token must have gone
        # through the backup, and now be live.
        assert "sk-2-new" in live_creds_path.read_text()
        assert live_creds_path.read_text() != live_before

    def test_fresh_target_is_not_refreshed(self, temp_home):
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com", expires_at=int(h.clock() * 1000) + 3_600_000)
        h.make_live("a@example.com", 1)
        with patch(
            "claude_swap.autoswitch.oauth.try_refresh_oauth_credentials"
        ) as mock_refresh:
            outcome = h.tick_with_usage({"1": _usage(95), "2": _usage(10)})
        assert outcome is TickOutcome.SWITCHED
        mock_refresh.assert_not_called()

    def test_invalid_grant_quarantines_and_tries_next(self, temp_home):
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com", expires_at=1)  # long expired
        h.seed(3, "c@example.com")
        h.make_live("a@example.com", 1)
        with patch(
            "claude_swap.autoswitch.oauth.try_refresh_oauth_credentials",
            return_value=oauth.RefreshOutcome(None, "invalid_grant"),
        ):
            outcome = h.tick_with_usage({
                "1": _usage(95), "2": _usage(10), "3": _usage(20),
            })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3  # next candidate after 2 was quarantined
        q = next(e for e in h.events if isinstance(e, QuarantineEvent))
        assert (q.number, q.reason) == ("2", "invalid_grant")
        assert "2" in h.state()["quarantine"]

    def test_transient_failure_skips_without_quarantine(self, temp_home):
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com", expires_at=1)
        h.make_live("a@example.com", 1)
        with patch(
            "claude_swap.autoswitch.oauth.try_refresh_oauth_credentials",
            return_value=oauth.RefreshOutcome(None, "transient"),
        ):
            outcome = h.tick_with_usage({"1": _usage(95), "2": _usage(10)})
        assert outcome is TickOutcome.ERROR
        assert h.active_number() == 1
        assert not h.state().get("quarantine")
        assert any(isinstance(e, ErrorEvent) for e in h.events)

    def test_live_session_target_is_skipped_even_with_fresh_token(self, temp_home):
        # Auto never activates an account that has a live `cswap run` session:
        # dual refresh-token ownership with nobody reading the warning.
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com", expires_at=int(h.clock() * 1000) + 3_600_000)
        h.make_live("a@example.com", 1)
        with patch.object(
            h.switcher, "live_session_pids_for", return_value=[4242]
        ), patch(
            "claude_swap.autoswitch.oauth.try_refresh_oauth_credentials"
        ) as mock_refresh:
            outcome = h.tick_with_usage({"1": _usage(95), "2": _usage(10)})
        assert outcome is TickOutcome.BLOCKED
        mock_refresh.assert_not_called()
        assert h.active_number() == 1

    def test_live_session_near_expiry_is_skipped(self, temp_home):
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com", expires_at=1)  # long expired
        h.make_live("a@example.com", 1)
        with patch.object(
            h.switcher, "live_session_pids_for", return_value=[4242]
        ), patch(
            "claude_swap.autoswitch.oauth.try_refresh_oauth_credentials"
        ) as mock_refresh:
            outcome = h.tick_with_usage({"1": _usage(95), "2": _usage(10)})
        assert outcome is TickOutcome.BLOCKED
        mock_refresh.assert_not_called()
        assert h.active_number() == 1


class TestQuarantineLifecycle:
    def test_quarantine_persists_across_engine_instances(self, harness):
        harness.engine._quarantine("2", "b@example.com", "invalid_grant")
        harness.events.clear()
        fresh_engine = harness._make_engine()
        usage = {"1": _usage(95), "2": _usage(0), "3": _usage(50)}
        with patch.object(
            harness.switcher,
            "usage_entries_by_account",
            return_value={
                num: _entry_for(value, harness.clock.now)
                for num, value in usage.items()
            },
        ):
            outcome = fresh_engine.tick()
        # 2 has the most headroom but is quarantined → 3 wins.
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 3

    def test_replaced_credentials_lift_quarantine(self, harness):
        harness.engine._quarantine("2", "b@example.com", "invalid_grant")
        # User re-logged in and re-captured the slot: new refresh token.
        harness.switcher._write_account_credentials(
            "2",
            "b@example.com",
            json.dumps({
                "claudeAiOauth": {"accessToken": "sk-2b", "refreshToken": "rt-2b"},
            }),
        )
        harness.events.clear()
        outcome = harness.tick_with_usage({
            "1": _usage(95), "2": _usage(0), "3": _usage(50),
        })
        assert any(isinstance(e, UnquarantineEvent) for e in harness.events)
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 2
        assert "2" not in (harness.state().get("quarantine") or {})

    def test_state_lock_preserves_concurrent_writes(self, harness):
        # Simulate another engine writing between our read and our write: the
        # RMW under the state lock must preserve its quarantine entry.
        harness.engine._mutate_state(
            lambda s: s.setdefault("quarantine", {}).update(
                {"3": {"email": "c@example.com", "reason": "invalid_grant",
                       "at": "x", "refreshTokenFingerprint": None}}
            )
        )
        harness.engine._mutate_state(lambda s: s.update(lastSwitchAt=123.0))
        state = harness.state()
        assert state["lastSwitchAt"] == 123.0
        assert "3" in state["quarantine"]


class TestDryRunAndNoOp:
    def test_dry_run_mutates_nothing(self, temp_home):
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)
        h.engine = h._make_engine(dry_run=True)
        live_before = (temp_home / ".claude" / ".credentials.json").read_text()

        outcome = h.tick_with_usage({"1": _usage(95), "2": _usage(10)})

        assert outcome is TickOutcome.SWITCHED
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert switch.dry_run is True
        assert h.active_number() == 1  # unchanged
        assert (temp_home / ".claude" / ".credentials.json").read_text() == live_before
        assert h.state() == {}  # no lastSwitchAt recorded

    def test_dry_run_never_freshens_or_quarantines(self, temp_home):
        # A near-expiry target would normally be refreshed (a real token
        # rotation) and a dead one quarantined (a state write). Dry-run must
        # stop at the decision: no network, no writes of any kind.
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com", expires_at=1)  # long expired
        h.make_live("a@example.com", 1)
        h.engine = h._make_engine(dry_run=True)
        backup_before = h.switcher.read_account_credentials("2", "b@example.com")

        with patch(
            "claude_swap.autoswitch.oauth.try_refresh_oauth_credentials"
        ) as mock_refresh:
            outcome = h.tick_with_usage({"1": _usage(95), "2": _usage(10)})

        assert outcome is TickOutcome.SWITCHED  # reported the would-switch
        mock_refresh.assert_not_called()
        assert h.switcher.read_account_credentials("2", "b@example.com") == backup_before
        assert h.state() == {}  # no quarantine, no lastSwitchAt

    def test_dry_run_does_not_release_quarantines(self, temp_home):
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)
        h.engine._quarantine("2", "b@example.com", "invalid_grant")
        # Replace the credential — a real tick would lift the quarantine.
        h.switcher._write_account_credentials(
            "2", "b@example.com",
            json.dumps({"claudeAiOauth": {"accessToken": "n", "refreshToken": "n"}}),
        )
        h.events.clear()
        h.engine = h._make_engine(dry_run=True)
        state_before = h.state()

        outcome = h.tick_with_usage({"1": _usage(95), "2": _usage(10)})

        assert not any(isinstance(e, UnquarantineEvent) for e in h.events)
        assert h.state() == state_before  # state file untouched
        # And the still-recorded quarantine keeps 2 out of the dry-run plan.
        assert outcome is TickOutcome.BLOCKED

    def test_already_active_result_is_noop(self, harness):
        with patch.object(
            harness.switcher,
            "switch_to",
            return_value={"switched": False, "reason": "already-active"},
        ):
            outcome = harness.tick_with_usage({
                "1": _usage(95), "2": _usage(10), "3": _usage(50),
            })
        assert outcome is TickOutcome.NO_ACTION
        assert "lastSwitchAt" not in harness.state()


class TestEventsShape:
    def test_every_event_has_envelope(self, harness):
        harness.tick_with_usage({"1": _usage(95), "2": _usage(10), "3": _usage(50)})
        assert harness.events
        for event in harness.events:
            payload = event.to_json()
            assert payload["schemaVersion"] == 1
            assert payload["event"] == event.kind
            assert payload["ts"].endswith("Z")

    def test_switch_event_refs_match_account_ref_shape(self, harness):
        harness.tick_with_usage({"1": _usage(95), "2": _usage(10), "3": _usage(50)})
        switch = next(e for e in harness.events if isinstance(e, SwitchEvent))
        payload = switch.to_json()
        assert payload["from"] == {"number": 1, "email": "a@example.com"}
        assert payload["to"] == {"number": 2, "email": "b@example.com"}

    def test_poll_event_human_line(self, harness):
        harness.tick_with_usage({"1": _usage(42), "2": _usage(10), "3": None})
        poll = next(e for e in harness.events if isinstance(e, PollEvent))
        line = poll.human()
        assert "Account-1" in line and "42% used" in line
        # Others show per-window pcts, not just the ambiguous binding pct.
        assert "#2: 5h 10% · 7d 0%" in line
        assert "#3: ?" in line

    def test_poll_event_windows_match_the_decision_set(self, temp_home):
        # Scoped windows appear only when configured: rendering an ignored
        # Fable 100% next to a switch onto that account would read as a bug.
        usage = {
            "1": _usage(42),
            "2": {
                "five_hour": {"pct": 3.0},
                "seven_day": {"pct": 89.0},
                "scoped": [{"name": "Fable", "pct": 21.0}],
            },
        }

        def build(**kw):
            h = EngineHarness(temp_home, **kw)
            h.seed(1, "a@example.com")
            h.seed(2, "b@example.com")
            h.make_live("a@example.com", 1)
            return h

        plain = build()
        plain.tick_with_usage(usage)
        poll = next(e for e in plain.events if isinstance(e, PollEvent))
        assert "#2: 5h 3% · 7d 89%" in poll.human()
        assert "Fable" not in poll.human()
        assert poll.to_json()["windowsPct"]["2"] == {"5h": 3.0, "7d": 89.0}

        modeled = build(model="Fable")
        modeled.tick_with_usage(usage)
        poll = next(e for e in modeled.events if isinstance(e, PollEvent))
        assert "#2: 5h 3% · 7d 89% · Fable 21%" in poll.human()
        assert poll.to_json()["windowsPct"]["2"] == {
            "5h": 3.0, "7d": 89.0, "Fable": 21.0,
        }


class TestRunLoop:
    def test_loop_ticks_until_stopped(self, harness):
        ticks = []

        def fake_tick():
            ticks.append(1)
            if len(ticks) >= 2:
                harness.engine.stop()
            return TickOutcome.NO_ACTION

        with patch.object(harness.engine, "tick", side_effect=fake_tick), \
             patch.object(harness.engine._wake, "wait", return_value=None):
            assert harness.engine.run_loop() == 0
        assert len(ticks) == 2

    def test_loop_survives_raising_tick(self, harness):
        calls = []

        def raising_inner():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            harness.engine.stop()
            return TickOutcome.NO_ACTION

        with patch.object(
            harness.engine, "_tick_inner", side_effect=raising_inner
        ), patch.object(harness.engine._wake, "wait", return_value=None):
            harness.engine.run_loop()
        assert len(calls) == 2
        assert any(isinstance(e, ErrorEvent) for e in harness.events)

    def test_stop_before_start_is_not_lost(self, harness):
        # A stop() issued before the worker thread enters run_loop must not
        # be cleared away: the loop exits without a single tick.
        harness.engine.stop()
        with patch.object(harness.engine, "tick") as tick:
            assert harness.engine.run_loop() == 0
        tick.assert_not_called()

    def test_wake_during_tick_cuts_the_following_sleep_short(self, harness):
        # No wait patching on purpose: if the clear-at-top ordering were
        # wrong (wake cleared after the wait), the wake fired during tick 1
        # would be lost and the loop would block on the real 60s sleep —
        # caught by the join timeout instead of hanging the suite.
        ticks: list[int] = []

        def fake_tick():
            ticks.append(1)
            if len(ticks) == 1:
                harness.engine.wake()  # e.g. apply_threshold landed mid-tick
            else:
                harness.engine.stop()
            return TickOutcome.NO_ACTION

        with patch.object(harness.engine, "tick", side_effect=fake_tick):
            worker = threading.Thread(target=harness.engine.run_loop)
            worker.start()
            worker.join(timeout=10)
            finished = not worker.is_alive()
            harness.engine.stop()  # unblock a failing loop before asserting
            worker.join(timeout=5)
        assert finished
        assert len(ticks) == 2

    def test_blocked_with_reset_sleeps_until_reset(self, harness):
        harness.engine._sleep_until_ts = harness.clock() + 1800
        delay = harness.engine._next_delay(TickOutcome.BLOCKED)
        assert 1700 < delay <= 1800

    def test_blocked_exhausted_without_reset_uses_fallback(self, harness):
        harness.engine._sleep_until_ts = None
        harness.engine._blocked_wait_long = True
        assert harness.engine._next_delay(TickOutcome.BLOCKED) == 300.0

    def test_blocked_on_resolvable_condition_keeps_normal_cadence(self, harness):
        harness.engine._sleep_until_ts = None
        harness.engine._blocked_wait_long = False
        delay = harness.engine._next_delay(TickOutcome.BLOCKED)
        assert 0.9 * 60 <= delay <= 1.1 * 60

    def test_normal_delay_is_jittered_interval(self, harness):
        delay = harness.engine._next_delay(TickOutcome.NO_ACTION)
        assert 0.9 * 60 <= delay <= 1.1 * 60

    def test_sleep_cap(self, harness):
        harness.engine._sleep_until_ts = harness.clock() + 50 * 3600
        assert harness.engine._next_delay(TickOutcome.BLOCKED) == 6 * 3600


class TestSessionThreshold:
    """apply_threshold(): the TUI's session-only, mid-run override."""

    def test_apply_threshold_retargets_trigger_and_poll_pin(self, harness):
        harness.engine.apply_threshold(72.0)
        assert harness.engine.settings.threshold == 72.0
        # Poll-cadence planning follows the new value immediately.
        assert harness.switcher._poll_inputs_override == (72.0, ())
        # And the very next tick decides with it: 80% ≥ 72 switches, where
        # the constructed 90 would not have.
        outcome = harness.tick_with_usage({
            "1": _usage(80), "2": _usage(10), "3": _usage(10),
        })
        assert outcome is TickOutcome.SWITCHED

    def test_clear_poll_policy_inputs_unpins(self, harness):
        harness.engine.apply_threshold(72.0)
        harness.switcher.clear_poll_policy_inputs()
        assert harness.switcher._poll_inputs_override is None

    def _collect_fetch_sets(self, harness, threshold: float) -> list:
        entries = {
            n: _entry_for(_usage(80.0 if n == "1" else 10.0), harness.clock.now)
            for n in ("1", "2", "3")
        }
        with patch.object(
            harness.switcher, "usage_entries_by_account", return_value=entries
        ) as collect:
            harness.engine._collect_scheduled_usage("1", threshold=threshold)
        return [c.kwargs.get("fetch") for c in collect.call_args_list]

    def test_collect_escalates_on_the_tick_snapshot_threshold(self, harness):
        # Escalation must key on the threshold captured by the tick, not a
        # re-read of self.settings (engine settings stay at 90 throughout).
        # Active at 80%: within ESCALATION_MARGIN_PCT of 90 → full refresh...
        assert {"1", "2", "3"} in self._collect_fetch_sets(harness, 90.0)
        # ...but not of 99.9 → baseline fetching only.
        assert {"1", "2", "3"} not in self._collect_fetch_sets(harness, 99.9)


class TestPctLabel:
    def test_whole_numbers_drop_the_decimal(self):
        assert pct_label(90.0) == "90"

    def test_fractional_threshold_keeps_one_decimal(self):
        # .0f would render the valid maximum 99.9 as a lying "100".
        assert pct_label(99.9) == "99.9"

    def test_configured_precision_is_preserved(self):
        # settings.json accepts arbitrary floats; display must not round.
        assert pct_label(85.55) == "85.55"
        assert pct_label(85.555555) == "85.555555"

    def test_float_noise_is_absorbed(self):
        assert pct_label(100.0 - 37.4) == "62.6"
        assert pct_label(99.85000000000001) == "99.85"

    def test_poll_event_shows_fractional_threshold(self):
        poll = PollEvent(
            active={"number": 1, "email": "a@example.com"},
            headroom={"1": 40.0},
            threshold=99.9,
        )
        assert "switch at 99.9%" in poll.human()

    def test_below_threshold_detail_shows_fractional_threshold(self, temp_home):
        h = EngineHarness(temp_home, threshold=99.9)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)
        h.tick_with_usage({"1": _usage(50), "2": _usage(10)})
        details = [
            e.detail for e in h.events if isinstance(e, NoSwitchEvent)
        ]
        assert details == ["50% < 99.9%"]

    def test_below_threshold_detail_never_shows_impossible_comparison(
        self, temp_home
    ):
        # utilization 99.85 with threshold 99.9: .0f on the left side used
        # to render the logically impossible "100% < 99.9%".
        h = EngineHarness(temp_home, threshold=99.9)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)
        h.tick_with_usage({"1": _usage(99.85), "2": _usage(10)})
        details = [
            e.detail for e in h.events if isinstance(e, NoSwitchEvent)
        ]
        assert details == ["99.85% < 99.9%"]


class TestTokenIdentity:
    """The token endpoint's free identity data: uuid backfill and the
    identity-conflict detector (the zero-request check that catches a
    poisoned slot the moment auto freshens it)."""

    def test_uuid_backfill_from_token_account_on_freshen(self, harness):
        data = harness.switcher._get_sequence_data()
        data["accounts"]["2"]["uuid"] = ""
        harness.switcher._write_json(harness.switcher.sequence_file, data)
        # Slot 2 near expiry → freshen path runs.
        harness.switcher._write_account_credentials(
            "2", "b@example.com",
            json.dumps({"claudeAiOauth": {
                "accessToken": "sk-2", "refreshToken": "rt-2", "expiresAt": 0,
            }}),
        )
        fresh = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-2f", "refreshToken": "rt-2f",
            "expiresAt": 99_999_999_999_000,
        }})
        with patch(
            "claude_swap.autoswitch.oauth.try_refresh_oauth_credentials",
            return_value=oauth.RefreshOutcome(
                fresh, None,
                {"uuid": "uuid-2-real", "email": "b@example.com",
                 "organizationUuid": ""},
            ),
        ):
            status = harness.engine._freshen_target("2", "b@example.com")
        assert status == "ok"
        assert harness.switcher._get_sequence_data()["accounts"]["2"]["uuid"] == (
            "uuid-2-real"
        )

    def test_conflicting_token_identity_returns_identity_conflict(self, harness):
        """A slot whose credential authenticates as a different account is not
        a viable target — but the rotated generation is still persisted (the
        grant consumed its predecessor)."""
        harness.switcher._write_account_credentials(
            "2", "b@example.com",
            json.dumps({"claudeAiOauth": {
                "accessToken": "sk-2", "refreshToken": "rt-2", "expiresAt": 0,
            }}),
        )
        fresh = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-2f", "refreshToken": "rt-2f",
            "expiresAt": 99_999_999_999_000,
        }})
        with patch(
            "claude_swap.autoswitch.oauth.try_refresh_oauth_credentials",
            return_value=oauth.RefreshOutcome(
                fresh, None,
                {"uuid": "uuid-somebody-else", "email": "z@example.com",
                 "organizationUuid": ""},
            ),
        ):
            status = harness.engine._freshen_target("2", "b@example.com")
        assert status == "identity-conflict"
        # The consumed generation's successor was persisted regardless.
        assert harness.switcher.read_account_credentials(
            "2", "b@example.com"
        ) == fresh

    def test_identity_conflict_quarantines_instead_of_activating(self, harness):
        """Tick path: the conflicted slot is quarantined (wrong-account switch
        prevented); rotation falls through to the next candidate."""
        harness.switcher._write_account_credentials(
            "2", "b@example.com",
            json.dumps({"claudeAiOauth": {
                "accessToken": "sk-2", "refreshToken": "rt-2", "expiresAt": 0,
            }}),
        )
        fresh = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-2f", "refreshToken": "rt-2f",
            "expiresAt": 99_999_999_999_000,
        }})

        def refresh(creds):
            data = json.loads(creds)["claudeAiOauth"]
            if data["refreshToken"] == "rt-2":
                return oauth.RefreshOutcome(
                    fresh, None,
                    {"uuid": "uuid-somebody-else", "email": "z@example.com",
                     "organizationUuid": ""},
                )
            return oauth.RefreshOutcome(creds, None)

        with patch(
            "claude_swap.autoswitch.oauth.try_refresh_oauth_credentials",
            side_effect=refresh,
        ):
            outcome = harness.tick_with_usage({
                "1": _usage(95), "2": _usage(10), "3": _usage(80),
            })
        # Account 2 had the most headroom but is conflicted → quarantined,
        # and the switch landed elsewhere.
        assert "account-quarantined" in harness.kinds()
        q = harness.state().get("quarantine", {})
        assert q.get("2", {}).get("reason") == "identity-conflict"
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 3

    def test_dead_slot_quarantined_even_with_safety_copy_present(self, harness):
        """No automatic promotion (fail-open rework of the issue #117 guard):
        a dead slot is quarantined outright; safety copies are forensic
        material, and recovery is the documented /login + cswap add."""
        harness.switcher._store._write_unclaimed_credential(
            json.dumps({"claudeAiOauth": {
                "accessToken": "sk-2-successor",
                "refreshToken": "rt-2-successor",
                "expiresAt": 99_999_999_999_000,
            }}),
            {"resolvedIdentity": {
                "uuid": "uuid-2", "email": "b@example.com",
                "organizationUuid": "",
            }},
        )
        harness.switcher._write_account_credentials(
            "2", "b@example.com",
            json.dumps({"claudeAiOauth": {
                "accessToken": "sk-2-dead", "refreshToken": "rt-2-dead",
                "expiresAt": 0,
            }}),
        )

        def refresh(creds):
            data = json.loads(creds)["claudeAiOauth"]
            if data["refreshToken"] == "rt-2-dead":
                return oauth.RefreshOutcome(None, "invalid_grant")
            return oauth.RefreshOutcome(creds, None)

        with patch(
            "claude_swap.autoswitch.oauth.try_refresh_oauth_credentials",
            side_effect=refresh,
        ):
            outcome = harness.tick_with_usage({
                "1": _usage(95), "2": _usage(10), "3": _usage(80),
            })
        q = harness.state().get("quarantine", {})
        assert q.get("2", {}).get("reason") == "invalid_grant"
        # The safety copy was not consumed, and the switch landed elsewhere.
        assert len(harness.switcher.list_unclaimed_credentials()) == 1
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 3

    def test_same_uuid_different_org_is_identity_conflict(self, harness):
        """Organization is part of account identity everywhere else in the
        codebase: the same account uuid under a different org is a conflict
        (org compared only when both sides record one)."""
        data = harness.switcher._get_sequence_data()
        data["accounts"]["2"]["organizationUuid"] = "org-2"
        harness.switcher._write_json(harness.switcher.sequence_file, data)
        harness.switcher._write_account_credentials(
            "2", "b@example.com",
            json.dumps({"claudeAiOauth": {
                "accessToken": "sk-2", "refreshToken": "rt-2", "expiresAt": 0,
            }}),
        )
        fresh = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-2f", "refreshToken": "rt-2f",
            "expiresAt": 99_999_999_999_000,
        }})
        with patch(
            "claude_swap.autoswitch.oauth.try_refresh_oauth_credentials",
            return_value=oauth.RefreshOutcome(
                fresh, None,
                {"uuid": "uuid-2", "email": "b@example.com",
                 "organizationUuid": "org-other"},
            ),
        ):
            status = harness.engine._freshen_target("2", "b@example.com")
        assert status == "identity-conflict"

    def test_malformed_token_identity_never_breaks_freshen(self, harness):
        """A schema change feeding a non-string uuid must be ignored, not
        raise — by this point the refreshed credential is already persisted,
        and a crash here would skip the persist bookkeeping and error the
        tick."""
        harness.switcher._write_account_credentials(
            "2", "b@example.com",
            json.dumps({"claudeAiOauth": {
                "accessToken": "sk-2", "refreshToken": "rt-2", "expiresAt": 0,
            }}),
        )
        fresh = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-2f", "refreshToken": "rt-2f",
            "expiresAt": 99_999_999_999_000,
        }})
        with patch(
            "claude_swap.autoswitch.oauth.try_refresh_oauth_credentials",
            return_value=oauth.RefreshOutcome(
                fresh, None, {"uuid": 12345, "email": ["weird"]},
            ),
        ):
            status = harness.engine._freshen_target("2", "b@example.com")
        assert status == "ok"
        assert harness.switcher.read_account_credentials(
            "2", "b@example.com"
        ) == fresh

    def test_blank_uuid_slot_with_org_conflict_quarantines_not_backfills(
        self, harness,
    ):
        """Org conflict must be checked before the blank-uuid backfill: a
        wrong-org credential is evidence the slot holds the wrong account,
        and backfilling its uuid would stick a foreign identity onto the
        slot (backfill never rewrites a non-empty uuid). Blank-uuid slots
        with a recorded org are what accounts added by older versions look
        like."""
        data = harness.switcher._get_sequence_data()
        data["accounts"]["2"]["uuid"] = ""
        data["accounts"]["2"]["organizationUuid"] = "org-A"
        harness.switcher._write_json(harness.switcher.sequence_file, data)
        harness.switcher._write_account_credentials(
            "2", "b@example.com",
            json.dumps({"claudeAiOauth": {
                "accessToken": "sk-2", "refreshToken": "rt-2", "expiresAt": 0,
            }}),
        )
        fresh = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-2f", "refreshToken": "rt-2f",
            "expiresAt": 99_999_999_999_000,
        }})
        with patch(
            "claude_swap.autoswitch.oauth.try_refresh_oauth_credentials",
            return_value=oauth.RefreshOutcome(
                fresh, None,
                {"uuid": "uuid-real", "email": "z@example.com",
                 "organizationUuid": "org-B"},
            ),
        ):
            status = harness.engine._freshen_target("2", "b@example.com")
        assert status == "identity-conflict"
        # The foreign uuid was NOT backfilled onto the slot.
        assert harness.switcher._get_sequence_data()["accounts"]["2"]["uuid"] == ""
        # The successor generation was still persisted (grant consumed it).
        assert harness.switcher.read_account_credentials(
            "2", "b@example.com"
        ) == fresh


def _model_usage(five_h: float, fable: float) -> dict:
    """Usage with a low 5h/7d but a per-model (Fable) weekly window."""
    return {
        "five_hour": {"pct": five_h},
        "seven_day": {"pct": 0.0},
        "scoped": [{"name": "Fable", "pct": fable}],
    }


class TestModelAwareSwitch:
    """`autoswitch.model` folds a per-model weekly limit into the decision."""

    def _seed(self, temp_home: Path, **kw) -> EngineHarness:
        h = EngineHarness(temp_home, **kw)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.seed(3, "c@example.com")
        h.make_live("a@example.com", 1)
        return h

    def test_model_maxed_switches_despite_session_headroom(self, temp_home):
        # Active #1: 5h only 5% used, but Fable is maxed → must leave.
        h = self._seed(temp_home, model="Fable")
        outcome = h.tick_with_usage({
            "1": _model_usage(5, 100),
            "2": _model_usage(5, 30),
            "3": _model_usage(5, 60),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2  # most Fable headroom
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert switch.to_ref == {"number": 2, "email": "b@example.com"}

    def test_without_model_setting_the_same_usage_holds(self, temp_home):
        # Default engine ignores scoped windows → #1 reads 5% used, no switch.
        h = self._seed(temp_home)
        outcome = h.tick_with_usage({
            "1": _model_usage(5, 100),
            "2": _model_usage(5, 30),
            "3": _model_usage(5, 60),
        })
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["below-threshold"]

    def test_model_headroom_still_gated_by_session_window(self, temp_home):
        # Fable has room on every account, but #1's 5h is maxed → still leaves.
        h = self._seed(temp_home, model="Fable")
        outcome = h.tick_with_usage({
            "1": _model_usage(100, 40),
            "2": _model_usage(10, 40),
            "3": _model_usage(20, 40),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2  # lowest binding (max of 5h, Fable)

    def test_comma_separated_models_switch_on_any(self, temp_home):
        # Configured for "Fable,Opus"; active #1 is fine on Fable but maxed on
        # Opus → must leave. Candidate scoped windows carry both models.
        h = self._seed(temp_home, model="Fable,Opus")

        def usage(five_h, fable, opus):
            return {
                "five_hour": {"pct": five_h},
                "seven_day": {"pct": 0.0},
                "scoped": [
                    {"name": "Fable", "pct": fable},
                    {"name": "Opus", "pct": opus},
                ],
            }

        outcome = h.tick_with_usage({
            "1": usage(5, 20, 100),   # Opus maxed
            "2": usage(5, 20, 30),    # most headroom
            "3": usage(5, 20, 70),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2

    def test_all_sentinel_binds_every_scoped_window(self, temp_home):
        # "all" needs no names: each account's own scoped windows bind,
        # whatever they're called.
        h = self._seed(temp_home, model="all")
        outcome = h.tick_with_usage({
            "1": {"five_hour": {"pct": 5.0}, "seven_day": {"pct": 0.0},
                  "scoped": [{"name": "Sonnet", "pct": 100.0}]},
            "2": {"five_hour": {"pct": 5.0}, "seven_day": {"pct": 0.0},
                  "scoped": [{"name": "Sonnet", "pct": 20.0}]},
            "3": {"five_hour": {"pct": 5.0}, "seven_day": {"pct": 0.0},
                  "scoped": [{"name": "Opus", "pct": 60.0}]},
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2

    def test_dual_exhausted_candidate_recovers_at_its_later_reset(self, temp_home):
        # #2 is blocked on both its 5h (resets 12:00) and Fable (15:00): it's
        # only usable again at the LATER one. #3 recovers later still (20:00),
        # so the all-exhausted wake is #2's Fable reset — which the old
        # earliest-of-any-window scan (12:00) would have jumped early for.
        h = self._seed(temp_home, model="Fable")
        fable_reset = "2026-07-05T15:00:00Z"
        outcome = h.tick_with_usage({
            "1": _model_usage(95, 10),
            "2": {
                "five_hour": {"pct": 100.0, "resets_at": "2026-07-05T12:00:00Z"},
                "seven_day": {"pct": 0.0},
                "scoped": [
                    {"name": "Fable", "pct": 100.0, "resets_at": fable_reset},
                ],
            },
            "3": {
                "five_hour": {"pct": 100.0, "resets_at": "2026-07-05T20:00:00Z"},
                "seven_day": {"pct": 0.0},
            },
        })
        assert outcome is TickOutcome.BLOCKED
        exhausted = next(e for e in h.events if isinstance(e, AllExhaustedEvent))
        assert exhausted.earliest_reset_at == fable_reset

    def test_unknown_recovery_falls_back_instead_of_oversleeping(self, temp_home):
        # #2 is exhausted with NO reset timestamp — it could recover any
        # moment. Sleeping toward #3's known 20:00 reset would suppress
        # checks for hours, so the wake time must be unprovable (bounded
        # blocked-cadence fallback instead of a reset sleep).
        h = self._seed(temp_home, model="Fable")
        outcome = h.tick_with_usage({
            "1": _model_usage(95, 10),
            "2": {
                "five_hour": {"pct": 0.0},
                "seven_day": {"pct": 0.0},
                "scoped": [{"name": "Fable", "pct": 100.0}],  # no resets_at
            },
            "3": {
                "five_hour": {"pct": 100.0, "resets_at": "2026-07-05T20:00:00Z"},
                "seven_day": {"pct": 0.0},
            },
        })
        assert outcome is TickOutcome.BLOCKED
        exhausted = next(e for e in h.events if isinstance(e, AllExhaustedEvent))
        assert exhausted.earliest_reset_at is None
        assert h.engine._sleep_until_ts is None
        assert h.engine._next_delay(outcome) == NO_RESET_FALLBACK_S

    def test_scoped_only_exhaustion_drives_the_wake_time(self, temp_home):
        # Candidates blocked ONLY by Fable: the wake must come from the scoped
        # reset — the 5h/7d-only scan would find no ≥100% window at all.
        h = self._seed(temp_home, model="Fable")
        fable_reset = "2026-07-06T09:00:00Z"
        blocked = {
            "five_hour": {"pct": 3.0, "resets_at": "2026-07-05T12:00:00Z"},
            "seven_day": {"pct": 0.0},
            "scoped": [{"name": "Fable", "pct": 100.0, "resets_at": fable_reset}],
        }
        outcome = h.tick_with_usage({
            "1": _model_usage(95, 10), "2": blocked, "3": blocked,
        })
        assert outcome is TickOutcome.BLOCKED
        exhausted = next(e for e in h.events if isinstance(e, AllExhaustedEvent))
        assert exhausted.earliest_reset_at == fable_reset

    def test_scoped_binding_window_keeps_active_cadence_tight(self, temp_home):
        # Fable moving at 88% is inside the escalation band: with the model
        # configured the urgent cadence engages, while the 5%-used 5h window
        # alone would just decay the interval.
        kwargs = dict(
            prev_interval_s=poll_policy.MIN_INTERVAL_S,
            prev_usage=_model_usage(5, 84),
            new_usage=_model_usage(5, 88),
            is_active=True,
            threshold=90.0,
            recent_429=False,
            now=1000.0,
            rng=lambda: 0.5,
        )
        _, scoped = poll_policy.plan_after_fetch(models=("Fable",), **kwargs)
        assert scoped == poll_policy.URGENT_INTERVAL_S
        _, unscoped = poll_policy.plan_after_fetch(models=(), **kwargs)
        assert unscoped > poll_policy.MIN_INTERVAL_S  # plain decay

    def test_unmatched_model_name_warns_once(self, temp_home):
        h = self._seed(temp_home, model="Fabel")  # deliberate typo
        usage = {
            "1": _model_usage(5, 10),
            "2": _model_usage(5, 10),
            "3": _model_usage(5, 10),
        }
        h.tick_with_usage(usage)
        warnings = [e for e in h.events if isinstance(e, ConfigWarningEvent)]
        assert len(warnings) == 1
        assert "Fabel" in warnings[0].message
        assert warnings[0].to_json()["event"] == "config-warning"
        h.tick_with_usage(usage)
        warnings = [e for e in h.events if isinstance(e, ConfigWarningEvent)]
        assert len(warnings) == 1  # once per run, not per tick

    def test_no_false_warning_while_an_account_is_unreadable(self, temp_home):
        h = self._seed(temp_home, model="Fabel")
        h.tick_with_usage({
            "1": _model_usage(5, 10), "2": _model_usage(5, 10), "3": None,
        })
        assert not any(isinstance(e, ConfigWarningEvent) for e in h.events)
        # Once every account reports, the check completes and warns.
        h.tick_with_usage({
            "1": _model_usage(5, 10),
            "2": _model_usage(5, 10),
            "3": _model_usage(5, 10),
        })
        assert any(isinstance(e, ConfigWarningEvent) for e in h.events)

    def test_matching_name_never_warns(self, temp_home):
        h = self._seed(temp_home, model="Fable")
        h.tick_with_usage({
            "1": _model_usage(5, 10),
            "2": _model_usage(5, 10),
            "3": _model_usage(5, 10),
        })
        assert not any(isinstance(e, ConfigWarningEvent) for e in h.events)
