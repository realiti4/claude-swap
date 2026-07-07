"""Tests for the per-account usage store."""

from __future__ import annotations

import json

import pytest

from claude_swap import usage_store
from claude_swap.usage_store import (
    BACKOFF_BASE_S,
    BACKOFF_CAP_S,
    CLAIM_TTL_S,
    SERVE_TTL_S,
    STALE_OK_S,
    TRUST_MAX_AGE_S,
    FetchRecord,
    UsageEntry,
    UsageStore,
    due_candidate,
    with_sentinel,
)

IDENT = {"1": ("a@x.com", ""), "2": ("b@x.com", "org-2")}
USAGE = {"five_hour": {"pct": 25.0}, "seven_day": {"pct": 10.0}}


class FakeClock:
    def __init__(self, start: float = 1_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def store(tmp_path, clock):
    return UsageStore(tmp_path / "cache", clock=clock)


class TestSchema:
    def test_empty_when_missing(self, store):
        entries = store.entries(IDENT)
        assert entries["1"] == UsageEntry()
        assert entries["1"].decision_value() is None

    def test_versionless_legacy_snapshot_ignored(self, store):
        store.path.parent.mkdir(parents=True)
        store.path.write_text(
            json.dumps({"timestamp": 123, "data": {"1": USAGE}}), encoding="utf-8"
        )
        assert store.entries(IDENT)["1"].last_good is None

    def test_corrupt_file_ignored(self, store):
        store.path.parent.mkdir(parents=True)
        store.path.write_text("{not json", encoding="utf-8")
        assert store.entries(IDENT)["1"] == UsageEntry()

    def test_round_trip(self, store, clock):
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        raw = json.loads(store.path.read_text(encoding="utf-8"))
        assert raw["schemaVersion"] == 2
        row = raw["accounts"]["1"]
        assert row["email"] == "a@x.com"
        assert row["lastGood"] == USAGE
        assert row["fetchedAt"] == clock.now
        entry = store.entries(IDENT)["1"]
        assert entry.last_good == USAGE
        assert entry.age_s == 0.0
        assert entry.decision_value() == USAGE


class TestStaleOnError:
    def test_failure_preserves_last_good(self, store, clock):
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        clock.advance(60)
        store.record({"1": FetchRecord(error="http-429")}, IDENT)
        entry = store.entries(IDENT)["1"]
        assert entry.last_good == USAGE
        assert entry.age_s == 60.0
        assert entry.last_error == "http-429"
        assert entry.consecutive_failures == 1
        # Still trusted for decisions while within STALE_OK_S.
        assert entry.decision_value() == USAGE

    def test_too_stale_is_unknown_for_decisions(self, store, clock):
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        clock.advance(STALE_OK_S + 1)
        entry = store.entries(IDENT)["1"]
        assert entry.decision_value() is None
        # ... but display still sees the measurement + its age.
        assert entry.last_good == USAGE
        assert entry.age_s == STALE_OK_S + 1

    def test_success_clears_failure_state(self, store, clock):
        store.record({"1": FetchRecord(error="timeout")}, IDENT)
        clock.advance(5)
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        entry = store.entries(IDENT)["1"]
        assert entry.consecutive_failures == 0
        assert entry.last_error is None
        assert entry.backoff_until is None
        assert entry.decision_value() == USAGE

    def test_success_with_no_windows(self, store):
        store.record({"1": FetchRecord(usage=None)}, IDENT)
        entry = store.entries(IDENT)["1"]
        assert entry.last_error is None
        assert entry.fetched_at is not None
        assert entry.decision_value() is None


class TestExtendedTrust:
    """Deliberate staleness (failure state, scheduler cadence) stays trusted."""

    def test_in_backoff_past_stale_ok_is_still_trusted(self, store, clock):
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        clock.advance(STALE_OK_S)
        store.record(
            {"1": FetchRecord(error="http-429", retry_after_s=480.0)}, IDENT
        )
        clock.advance(60)
        entry = store.entries(IDENT)["1"]
        assert entry.age_s > STALE_OK_S
        assert entry.in_backoff(clock.now)
        assert entry.trust_extended
        assert entry.decision_value() == USAGE

    def test_failure_state_after_backoff_expiry_is_still_trusted(self, store, clock):
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        clock.advance(60)
        store.record({"1": FetchRecord(error="timeout")}, IDENT)
        clock.advance(BACKOFF_BASE_S + STALE_OK_S)  # backoff long expired
        entry = store.entries(IDENT)["1"]
        assert not entry.in_backoff(clock.now)
        assert entry.decision_value() == USAGE

    def test_within_poll_plan_past_stale_ok_is_trusted(self, store, clock):
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        store.set_poll_plan({"1": (clock.now + 600.0, 600.0)}, IDENT)
        clock.advance(400)
        entry = store.entries(IDENT)["1"]
        assert entry.consecutive_failures == 0
        assert entry.decision_value() == USAGE
        # Once overdue, the staleness is no longer scheduler-chosen.
        clock.advance(250)
        assert store.entries(IDENT)["1"].decision_value() is None

    def test_trust_ceiling_wins_over_failure_state(self, store, clock):
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        store.record({"1": FetchRecord(error="http-429")}, IDENT)
        clock.advance(TRUST_MAX_AGE_S + 1)
        store.record({"1": FetchRecord(error="http-429")}, IDENT)
        entry = store.entries(IDENT)["1"]
        assert entry.consecutive_failures == 2
        assert entry.decision_value() is None
        # Display still sees the measurement + its age.
        assert entry.last_good == USAGE


class TestBackoff:
    def test_exponential_backoff(self, store, clock):
        expected = [30.0, 60.0, 120.0, 240.0, 480.0, 600.0, 600.0]
        for i, want in enumerate(expected):
            store.record({"1": FetchRecord(error="http-500")}, IDENT)
            entry = store.entries(IDENT)["1"]
            assert entry.consecutive_failures == i + 1
            assert entry.backoff_until == pytest.approx(clock.now + want)
            clock.advance(want + 1)

    def test_backoff_cap(self):
        assert usage_store._failure_backoff_s(50, None) == BACKOFF_CAP_S

    def test_retry_after_is_the_floor(self, store, clock):
        store.record(
            {"1": FetchRecord(error="http-429", retry_after_s=90.0)}, IDENT
        )
        entry = store.entries(IDENT)["1"]
        # First failure computes 30s, but the server asked for 90s.
        assert entry.backoff_until == pytest.approx(clock.now + 90.0)
        assert entry.in_backoff(clock.now + 89)
        assert not entry.in_backoff(clock.now + 91)

    def test_own_curve_may_exceed_retry_after(self):
        assert usage_store._failure_backoff_s(5, 10.0) == pytest.approx(480.0)
        assert BACKOFF_BASE_S * 2**4 == 480.0

    def test_edge_429_backoff_capped(self, store, clock):
        # "Retry-After: 0" is the sustained/edge rule: retries are penalty-free
        # and ~every other one succeeds, so backoff must stay tight instead of
        # doubling to BACKOFF_CAP_S — freshness matters most during heavy burn.
        expected = [30.0, 60.0, 120.0, 120.0, 120.0]
        for i, want in enumerate(expected):
            store.record(
                {"1": FetchRecord(error="http-429", retry_after_s=0.0)}, IDENT
            )
            entry = store.entries(IDENT)["1"]
            assert entry.consecutive_failures == i + 1
            assert entry.backoff_until == pytest.approx(clock.now + want)
            clock.advance(want + 1)

    def test_retry_after_floor_is_capped(self):
        # A pathological Retry-After can never park an account for hours.
        assert usage_store._failure_backoff_s(1, 5000.0) == pytest.approx(
            usage_store.RETRY_AFTER_FLOOR_CAP_S
        )

    def test_measured_burst_block_honored_exactly(self):
        # The real burst rule (measured 2026-07-06) sends Retry-After: 300 and
        # the block is exactly that long — honor it as the floor, uncapped.
        assert usage_store._failure_backoff_s(1, 300.0) == pytest.approx(300.0)


class TestIdentityGuard:
    def test_slot_reuse_hides_old_usage(self, store):
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        rebound = {"1": ("new@x.com", "")}
        assert store.entries(rebound)["1"] == UsageEntry()

    def test_same_email_different_org_is_a_different_account(self, store):
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        rebound = {"1": ("a@x.com", "org-9")}
        assert store.entries(rebound)["1"] == UsageEntry()

    def test_write_replaces_mismatched_row(self, store):
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        rebound = {"1": ("new@x.com", "")}
        store.record({"1": FetchRecord(error="timeout")}, rebound)
        entry = store.entries(rebound)["1"]
        assert entry.last_good is None  # old account's data did not survive
        assert entry.consecutive_failures == 1

    def test_untouched_slots_survive_subset_writes(self, store):
        store.record(
            {"1": FetchRecord(usage=USAGE), "2": FetchRecord(usage=USAGE)}, IDENT
        )
        store.record({"1": FetchRecord(error="timeout")}, {"1": IDENT["1"]})
        assert store.entries(IDENT)["2"].last_good == USAGE


class TestClaims:
    def test_claim_marks_in_flight(self, store, clock):
        store.claim(["1"], IDENT)
        entry = store.entries(IDENT)["1"]
        assert entry.claimed(clock.now)
        clock.advance(CLAIM_TTL_S + 1)
        assert not store.entries(IDENT)["1"].claimed(clock.now)

    def test_claim_does_not_touch_measurement(self, store, clock):
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        clock.advance(100)
        store.claim(["1"], IDENT)
        entry = store.entries(IDENT)["1"]
        assert entry.last_good == USAGE
        assert entry.age_s == 100.0


class TestSentinels:
    def test_sentinel_record_is_a_store_noop(self, store):
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        store.record({"1": FetchRecord(sentinel="token expired")}, IDENT)
        entry = store.entries(IDENT)["1"]
        assert entry.sentinel is None  # never persisted
        assert entry.last_good == USAGE

    def test_overlay_wins_decisions_but_not_display(self, store):
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        entry = with_sentinel(store.entries(IDENT)["1"], "token expired")
        assert entry.decision_value() == "token expired"
        assert entry.last_good == USAGE  # display can still show last-seen

    def test_with_sentinel_none_is_identity(self):
        entry = UsageEntry(last_good=USAGE)
        assert with_sentinel(entry, None) is entry


class TestFreshness:
    def test_fresh_within_serve_ttl(self, store, clock):
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        entry = store.entries(IDENT)["1"]
        assert entry.fresh(clock.now)
        assert entry.fresh(clock.now + SERVE_TTL_S)
        assert not entry.fresh(clock.now + SERVE_TTL_S + 1)


class TestPollPlan:
    def test_set_and_read_poll_plan(self, store, clock):
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        store.set_poll_plan({"1": (clock.now + 120.0, 120.0)}, IDENT)
        entry = store.entries(IDENT)["1"]
        assert entry.next_poll_at == clock.now + 120.0
        assert entry.poll_interval_s == 120.0
        assert entry.last_good == USAGE  # untouched

    def test_poll_plan_clear(self, store, clock):
        store.set_poll_plan({"1": (clock.now + 120.0, 120.0)}, IDENT)
        store.set_poll_plan({"1": (None, None)}, IDENT)
        entry = store.entries(IDENT)["1"]
        assert entry.next_poll_at is None
        assert entry.poll_interval_s is None


class TestDueCandidate:
    """Candidate selection shared by the auto engine and the TUI watch view."""

    NOW = 1_000_000.0

    def test_missing_entry_is_most_due(self):
        entries = {"3": UsageEntry(fetched_at=self.NOW - 60, age_s=60.0)}
        assert due_candidate(["2", "3"], entries, self.NOW) == "2"

    def test_never_fetched_beats_fetched(self):
        entries = {
            "2": UsageEntry(fetched_at=self.NOW - 999, age_s=999.0),
            "3": UsageEntry(),  # row exists but never fetched
        }
        assert due_candidate(["2", "3"], entries, self.NOW) == "3"

    def test_stalest_fetched_wins(self):
        entries = {
            "2": UsageEntry(fetched_at=self.NOW - 60, age_s=60.0),
            "3": UsageEntry(fetched_at=self.NOW - 300, age_s=300.0),
        }
        assert due_candidate(["2", "3"], entries, self.NOW) == "3"

    def test_sentinel_accounts_skipped(self):
        entries = {"2": UsageEntry(sentinel="api-key")}
        assert due_candidate(["2"], entries, self.NOW) is None

    def test_backoff_skipped_until_it_expires(self):
        entries = {"2": UsageEntry(backoff_until=self.NOW + 10)}
        assert due_candidate(["2"], entries, self.NOW) is None
        assert due_candidate(["2"], entries, self.NOW + 11) == "2"

    def test_future_next_poll_at_skipped(self):
        entries = {
            "2": UsageEntry(fetched_at=self.NOW - 300, next_poll_at=self.NOW + 60),
            "3": UsageEntry(fetched_at=self.NOW - 60),
        }
        # "2" is stalest but not yet due per auto's learned plan → "3" wins.
        assert due_candidate(["2", "3"], entries, self.NOW) == "3"

    def test_none_when_no_candidates(self):
        assert due_candidate([], {}, self.NOW) is None
