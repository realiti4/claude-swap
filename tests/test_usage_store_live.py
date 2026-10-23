"""Tests for live readings (``cswap proxy``) kept beside the polled one."""

from __future__ import annotations

import pytest

from claude_swap.usage_store import (
    SERVE_TTL_S,
    STALE_OK_S,
    TRUST_MAX_AGE_S,
    FetchRecord,
    UsageStore,
    due_candidate,
    with_sentinel,
)

IDENT = {"1": ("a@x.com", ""), "2": ("b@x.com", "org-2")}
POLLED = {
    "five_hour": {"pct": 25.0},
    "seven_day": {"pct": 10.0},
    "scoped": [{"name": "Fable", "pct": 30.0}],
    "spend": {"pct": 5.0, "used": 1.0, "limit": 20.0, "currency": "USD"},
}
LIVE = {"five_hour": {"pct": 87.0}, "seven_day": {"pct": 41.0}}


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


def _poll(store: UsageStore, usage: dict = POLLED) -> None:
    store.record({"1": FetchRecord(usage=usage)}, IDENT)


class TestRecordLive:
    def test_reading_is_exposed_with_its_age(self, store, clock):
        store.record_live({"1": LIVE}, IDENT)
        clock.advance(7)
        entry = store.entries(IDENT)["1"]
        assert entry.live_usage == LIVE
        assert entry.live_age_s == 7.0

    def test_survives_a_new_store_on_the_same_directory(self, tmp_path, clock):
        UsageStore(tmp_path / "cache", clock=clock).record_live({"1": LIVE}, IDENT)
        again = UsageStore(tmp_path / "cache", clock=clock)
        assert again.entries(IDENT)["1"].live_usage == LIVE

    def test_empty_input_writes_nothing(self, store, tmp_path):
        store.record_live({}, IDENT)
        assert not (tmp_path / "cache").exists() or store.entries(IDENT)["1"].live_usage is None

    def test_polled_state_is_left_alone(self, store, clock):
        # The poller must keep its own cadence: it is still the only source of
        # the per-model and spend windows. So nothing it schedules on moves.
        _poll(store)
        store.set_poll_plan({"1": (clock.now + 600, 600.0)}, IDENT)
        store.record({"1": FetchRecord(error="http-429", retry_after_s=0.0)}, IDENT)
        before = store.entries(IDENT)["1"]
        clock.advance(5)
        store.record_live({"1": LIVE}, IDENT)
        after = store.entries(IDENT)["1"]
        assert after.last_good == before.last_good == POLLED
        assert after.fetched_at == before.fetched_at
        assert after.next_poll_at == before.next_poll_at
        assert after.consecutive_failures == before.consecutive_failures
        assert after.last_error == before.last_error
        assert after.backoff_until == before.backoff_until

    def test_does_not_change_when_the_slot_is_due(self, store, clock):
        _poll(store)
        clock.advance(SERVE_TTL_S + 1)
        assert due_candidate(["1"], store.entries(IDENT), clock.now) == "1"
        store.record_live({"1": LIVE}, IDENT)
        assert due_candidate(["1"], store.entries(IDENT), clock.now) == "1"

    def test_a_row_for_another_account_is_replaced_not_merged(self, store):
        _poll(store)
        moved = {"1": ("someone-else@x.com", "")}
        store.record_live({"1": LIVE}, moved)
        assert store.entries(moved)["1"].last_good is None
        assert store.entries(moved)["1"].live_usage == LIVE
        assert store.entries(IDENT)["1"].live_usage is None


class TestDecisionValue:
    def test_live_windows_override_the_polled_ones(self, store, clock):
        _poll(store)
        clock.advance(60)
        store.record_live({"1": LIVE}, IDENT)
        value = store.entries(IDENT)["1"].decision_value()
        assert value["five_hour"] == {"pct": 87.0}
        assert value["seven_day"] == {"pct": 41.0}

    def test_windows_the_headers_do_not_carry_come_from_the_poll(self, store, clock):
        _poll(store)
        store.record_live({"1": LIVE}, IDENT)
        value = store.entries(IDENT)["1"].decision_value()
        assert value["scoped"] == POLLED["scoped"]
        assert value["spend"] == POLLED["spend"]

    def test_overlay_does_not_mutate_the_stored_reading(self, store):
        _poll(store)
        store.record_live({"1": LIVE}, IDENT)
        entry = store.entries(IDENT)["1"]
        entry.decision_value()
        assert entry.last_good == POLLED

    def test_live_alone_when_nothing_was_polled(self, store):
        store.record_live({"1": LIVE}, IDENT)
        assert store.entries(IDENT)["1"].decision_value() == LIVE

    def test_an_untrusted_poll_contributes_nothing(self, store, clock):
        # A poll too old to decide on must not ride in on a fresh live
        # reading: its per-model windows would look current when they are not.
        _poll(store)
        clock.advance(TRUST_MAX_AGE_S + 1)
        store.record_live({"1": LIVE}, IDENT)
        assert store.entries(IDENT)["1"].decision_value() == LIVE

    def test_a_poll_newer_than_the_live_reading_wins(self, store, clock):
        store.record_live({"1": LIVE}, IDENT)
        clock.advance(30)
        _poll(store)
        assert store.entries(IDENT)["1"].decision_value() == POLLED

    def test_a_live_reading_too_old_to_trust_is_ignored(self, store, clock):
        _poll(store)
        store.set_poll_plan({"1": (clock.now + 7200, 7200.0)}, IDENT)
        clock.advance(1)
        store.record_live({"1": LIVE}, IDENT)
        clock.advance(STALE_OK_S + 1)
        # The poll is still trusted (scheduler-chosen staleness); the live
        # reading is not: an idle session's last header is no current reading.
        assert store.entries(IDENT)["1"].decision_value() == POLLED

    def test_a_stale_live_reading_alone_is_unknown(self, store, clock):
        store.record_live({"1": LIVE}, IDENT)
        clock.advance(STALE_OK_S + 1)
        assert store.entries(IDENT)["1"].decision_value() is None

    def test_sentinel_still_wins(self, store):
        store.record_live({"1": LIVE}, IDENT)
        entry = with_sentinel(store.entries(IDENT)["1"], "token_expired")
        assert entry.decision_value() == "token_expired"


class TestFresh:
    def test_a_recent_live_reading_is_fresh(self, store, clock):
        _poll(store)
        clock.advance(SERVE_TTL_S + 60)
        store.record_live({"1": LIVE}, IDENT)
        clock.advance(10)
        assert store.entries(IDENT)["1"].fresh(clock.now)

    def test_an_old_live_reading_is_not(self, store, clock):
        store.record_live({"1": LIVE}, IDENT)
        clock.advance(SERVE_TTL_S + 1)
        assert not store.entries(IDENT)["1"].fresh(clock.now)

    def test_a_recent_poll_is_still_fresh_on_its_own(self, store, clock):
        _poll(store)
        clock.advance(10)
        assert store.entries(IDENT)["1"].fresh(clock.now)
