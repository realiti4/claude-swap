"""Tests for the per-account usage store."""

from __future__ import annotations

import json

import pytest

from claude_swap import usage_store
from claude_swap.usage_store import (
    BACKOFF_BASE_S,
    BACKOFF_CAP_S,
    CLAIM_TTL_S,
    RATE_LIMIT_TRUST_MAX_AGE_S,
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

    def test_trust_ceiling_wins_over_non_429_failure_state(self, store, clock):
        # A non-429 failure (timeout/network) past the general ceiling reads as
        # unknown: such an error is no evidence the last_good still holds, so
        # the unknown-path machinery must take back over.
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        store.record({"1": FetchRecord(error="timeout")}, IDENT)
        clock.advance(TRUST_MAX_AGE_S + 1)
        store.record({"1": FetchRecord(error="timeout")}, IDENT)
        entry = store.entries(IDENT)["1"]
        assert entry.consecutive_failures == 2
        assert entry.decision_value() is None

    def _usage_resetting_at(self, clock, seconds_ahead):
        from datetime import datetime, timezone

        iso = (
            datetime.fromtimestamp(clock.now + seconds_ahead, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        return {
            "five_hour": {"pct": 25.0, "resets_at": iso},
            "seven_day": {"pct": 10.0, "resets_at": iso},
        }

    def test_429_staleness_trusted_until_window_reset(self, store, clock):
        # A usage-endpoint 429 throttles polling; it does NOT move the account's
        # real windows. Usage only rises within a window (monotone until reset),
        # so last_good is a valid lower bound — trust it right up to its window
        # reset, no matter how long the throttle lasts. Far past the general
        # TRUST_MAX_AGE_S but still before the reset → still trusted.
        usage = self._usage_resetting_at(clock, TRUST_MAX_AGE_S * 4)
        store.record({"1": FetchRecord(usage=usage)}, IDENT)
        store.record({"1": FetchRecord(error="http-429")}, IDENT)
        clock.advance(TRUST_MAX_AGE_S + 1)  # past the general ceiling...
        store.record({"1": FetchRecord(error="http-429")}, IDENT)
        entry = store.entries(IDENT)["1"]
        assert entry.trust_extended
        assert entry.decision_value() == usage  # ...but before the reset

    def test_429_staleness_expires_at_window_reset(self, store, clock):
        # Once the window has reset, usage is zeroed and last_good is obsolete —
        # it reads as unknown so the unknown-path machinery takes over. This is
        # the natural, data-driven bound (no fixed clock): trust ends exactly
        # when the measured value can no longer hold.
        usage = self._usage_resetting_at(clock, 600.0)  # resets in 10 min
        store.record({"1": FetchRecord(usage=usage)}, IDENT)
        store.record({"1": FetchRecord(error="http-429")}, IDENT)
        clock.advance(601.0)  # past the window reset
        store.record({"1": FetchRecord(error="http-429")}, IDENT)
        entry = store.entries(IDENT)["1"]
        assert entry.decision_value() is None
        assert entry.last_good == usage  # display still sees it

    def test_429_staleness_without_reset_info_falls_back_to_ceiling(
        self, store, clock
    ):
        # Older stored data may carry no resets_at. Fall back to the fixed
        # rate-limit ceiling so such an entry still can't be trusted forever.
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)  # no resets_at
        store.record({"1": FetchRecord(error="http-429")}, IDENT)
        clock.advance(TRUST_MAX_AGE_S + 1)
        store.record({"1": FetchRecord(error="http-429")}, IDENT)
        assert store.entries(IDENT)["1"].decision_value() == USAGE  # within
        clock.advance(RATE_LIMIT_TRUST_MAX_AGE_S)
        store.record({"1": FetchRecord(error="http-429")}, IDENT)
        assert store.entries(IDENT)["1"].decision_value() is None  # past cap


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

    def test_edge_429_backoff_floors_at_edge_backoff(self, store, clock):
        # "Retry-After: 0" is the saturated-window edge: the token's rolling
        # hour is full and frees only as old requests age out, so even the
        # first backoff waits EDGE_BACKOFF_S; the exponential curve may push
        # past it, capped at BACKOFF_CAP_S.
        expected = [300.0, 300.0, 300.0, 300.0, 480.0, 600.0, 600.0]
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
        assert usage_store._failure_backoff_s(1, 50000.0) == pytest.approx(
            usage_store.RETRY_AFTER_FLOOR_CAP_S
        )

    def test_hour_scale_retry_after_honored(self):
        # The usage endpoint's burst block spans its ~1h rolling window and the
        # server's Retry-After counts that down accurately (measured). Capping
        # it to minutes means re-probing mid-window, which re-arms the block and
        # never lets the token drain — so an hour-scale Retry-After must be
        # honored, up to the (raised) safety cap.
        assert usage_store._failure_backoff_s(1, 3600.0) == pytest.approx(3600.0)
        assert usage_store.RETRY_AFTER_FLOOR_CAP_S >= 3600.0

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


class TestDeadTokenQuarantine:
    """invalid_grant strikes → token_dead → quarantined from fetching."""

    def test_invalid_grant_advances_strikes(self, store):
        store.record({"1": FetchRecord(error="invalid_grant")}, IDENT)
        assert store.entries(IDENT)["1"].auth_dead_strikes == 1
        store.record({"1": FetchRecord(error="invalid_grant")}, IDENT)
        assert store.entries(IDENT)["1"].auth_dead_strikes == 2

    def test_transient_error_does_not_advance_or_reset(self, store):
        store.record({"1": FetchRecord(error="invalid_grant")}, IDENT)
        store.record({"1": FetchRecord(error="http-429")}, IDENT)  # transient
        # 429 must neither bump nor clear the dead-token tally.
        assert store.entries(IDENT)["1"].auth_dead_strikes == 1

    def test_success_resets_strikes(self, store):
        store.record({"1": FetchRecord(error="invalid_grant")}, IDENT)
        store.record({"1": FetchRecord(error="invalid_grant")}, IDENT)
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        assert store.entries(IDENT)["1"].auth_dead_strikes == 0

    def test_token_dead_at_threshold(self, store):
        assert not store.entries(IDENT)["1"].token_dead()  # no strikes yet
        store.record({"1": FetchRecord(error="invalid_grant")}, IDENT)
        # A single server-confirmed invalid_grant is definitive.
        assert store.entries(IDENT)["1"].token_dead()

    def test_transient_error_alone_never_marks_dead(self, store):
        for _ in range(5):
            store.record({"1": FetchRecord(error="http-429")}, IDENT)
        assert not store.entries(IDENT)["1"].token_dead()

    def test_due_candidate_skips_dead_token(self, store, clock):
        store.record({"1": FetchRecord(error="invalid_grant")}, IDENT)
        store.record({"1": FetchRecord(error="invalid_grant")}, IDENT)
        clock.advance(10_000)  # past any backoff
        entries = store.entries(IDENT)
        assert entries["1"].token_dead()
        # A dead token is never nominated as the alternate to poll.
        assert due_candidate(["1"], entries, clock.now) is None

    def test_clear_dead_token_lifts_quarantine(self, store):
        store.record({"1": FetchRecord(error="invalid_grant")}, IDENT)
        store.record({"1": FetchRecord(error="invalid_grant")}, IDENT)
        assert store.entries(IDENT)["1"].token_dead()
        store.clear_dead_token(["1"], IDENT)
        entry = store.entries(IDENT)["1"]
        assert entry.auth_dead_strikes == 0
        assert not entry.token_dead()
        assert entry.last_error is None
        assert entry.backoff_until is None


class TestReserve:
    """Atomic fetch reservation: eligibility re-checked under the lock."""

    def _stale(self, store, clock, num="1"):
        store.record({num: FetchRecord(usage=USAGE)}, IDENT)
        clock.advance(SERVE_TTL_S + CLAIM_TTL_S + 1)

    def test_reserve_wins_and_stamps(self, store):
        assert store.reserve(["1"], IDENT, respect_plans=True) == ["1"]
        # The stamp is the claim: an immediate second reservation loses —
        # this is the double-fetch race the old read-then-claim flow allowed.
        assert store.reserve(["1"], IDENT, respect_plans=True) == []
        assert store.reserve(["1"], IDENT, respect_plans=False) == []

    def test_fresh_entry_not_won(self, store, clock):
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        clock.advance(CLAIM_TTL_S + 1)  # claim expired, entry still fresh
        assert store.reserve(["1"], IDENT, respect_plans=True) == []

    def test_respect_plans_waits_for_next_poll(self, store, clock):
        self._stale(store, clock)
        store.set_poll_plan({"1": (clock.now + 300.0, 300.0)}, IDENT)
        assert store.reserve(["1"], IDENT, respect_plans=True) == []
        clock.advance(301)
        assert store.reserve(["1"], IDENT, respect_plans=True) == ["1"]

    def test_scheduler_beats_the_ttl_when_due(self, store, clock):
        # Urgent cadence: a due plan wins even inside the serve TTL for the
        # scheduler; on-demand callers still respect freshness.
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        store.set_poll_plan({"1": (clock.now + 60.0, 60.0)}, IDENT)
        clock.advance(61)
        assert store.reserve(["1"], IDENT, respect_plans=True) == []
        assert store.reserve(["1"], IDENT, respect_plans=False) == ["1"]

    def test_scheduler_may_fetch_a_not_due_stale_entry(self, store, clock):
        # Escalation semantics: an explicit set bypasses a future nextPollAt
        # when the entry has gone stale.
        self._stale(store, clock)
        store.set_poll_plan({"1": (clock.now + 600.0, 600.0)}, IDENT)
        assert store.reserve(["1"], IDENT, respect_plans=False) == ["1"]

    def test_backoff_blocks_both_modes(self, store, clock):
        store.record({"1": FetchRecord(error="timeout")}, IDENT)
        clock.advance(CLAIM_TTL_S + 1)  # claim gone, 30s backoff still on
        assert store.reserve(["1"], IDENT, respect_plans=True) == []
        assert store.reserve(["1"], IDENT, respect_plans=False) == []

    def test_dead_token_never_won(self, store, clock):
        store.record({"1": FetchRecord(error="invalid_grant")}, IDENT)
        clock.advance(TRUST_MAX_AGE_S)  # backoff long gone; quarantine stays
        assert store.reserve(["1"], IDENT, respect_plans=True) == []
        assert store.reserve(["1"], IDENT, respect_plans=False) == []

    def test_unknown_row_and_identity_mismatch_win(self, store, clock):
        assert store.reserve(["1"], IDENT, respect_plans=True) == ["1"]
        # Slot reused by a different account: the old row is invisible and
        # replaced, so the new identity is fetch-eligible immediately.
        store.record({"2": FetchRecord(usage=USAGE)}, IDENT)
        other = {"2": ("new@x.com", "org-9")}
        assert store.reserve(["2"], other, respect_plans=True) == ["2"]


class TestLast429Marker:
    def test_last_429_survives_recovery(self, store, clock):
        # The planner needs "was there a 429 recently?" even after a
        # successful fetch cleared the failure fields.
        store.record(
            {"1": FetchRecord(error="http-429", retry_after_s=0.0)}, IDENT
        )
        t429 = clock.now
        clock.advance(400)
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        entry = store.entries(IDENT)["1"]
        assert entry.consecutive_failures == 0
        assert entry.last_429_at == pytest.approx(t429)

    def test_non_429_failures_leave_the_marker_alone(self, store, clock):
        store.record({"1": FetchRecord(error="timeout")}, IDENT)
        assert store.entries(IDENT)["1"].last_429_at is None


class TestClaimTrustBridge:
    def test_in_flight_claim_keeps_decision_trust(self, store, clock):
        # Reservation loser scenario: the entry is poll-due and past
        # STALE_OK_S, another process just won reserve() and is fetching.
        # The loser must keep trusting last-good for the claim window instead
        # of reading unknown (and e.g. counting an unhealthy tick).
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        store.set_poll_plan({"1": (clock.now + 400.0, 400.0)}, IDENT)
        clock.advance(401)  # poll-due, age > STALE_OK_S
        assert store.reserve(["1"], IDENT, respect_plans=True) == ["1"]
        entry = store.entries(IDENT)["1"]
        assert entry.trust_extended
        assert entry.decision_value() == USAGE
        clock.advance(CLAIM_TTL_S)  # claim expired, no result recorded
        assert store.entries(IDENT)["1"].decision_value() is None
