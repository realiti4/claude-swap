"""Codex usage behind the shared UsageStore: what does and does not hit the network."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_swap.codex.auth_file import account_key
from claude_swap.codex.store import CodexSlot, CodexStore
from claude_swap.codex.usage import UsageFetch
from claude_swap.codex.usage_cache import CodexUsageCache
from tests.conftest_codex import make_auth_json

KEY_A = account_key("user-a", "acct-a")
KEY_B = account_key("user-b", "acct-b")

USAGE = {"five_hour": {"pct": 10}, "seven_day": {"pct": 20}}


@pytest.fixture
def cache(codex_home: Path) -> CodexUsageCache:
    return CodexUsageCache(CodexStore())


@pytest.fixture
def slots(codex_home: Path) -> list[CodexSlot]:
    store = CodexStore()
    a = store.upsert_slot(KEY_A, email="a@x", plan="pro")
    b = store.upsert_slot(KEY_B, email="b@x", plan="pro")
    for key in (KEY_A, KEY_B):
        store.write_snapshot(key, make_auth_json())
    return [a, b]


@pytest.fixture
def counter(monkeypatch):
    """Count fetch_usage calls and control what they return."""
    calls: list[tuple[str, str]] = []
    result = {"value": UsageFetch(usage=USAGE)}

    def fake(access_token, account_id, timeout_s=10.0):
        calls.append((access_token, account_id))
        return result["value"]

    monkeypatch.setattr("claude_swap.codex.usage_cache.fetch_usage", fake)
    return calls, result


def _payload_for(slot: CodexSlot) -> dict | None:
    return CodexStore().read_snapshot(slot.account_key)


def _refresh(cache: CodexUsageCache, slots, **kw):
    return cache.refresh(slots, payload_for=_payload_for, **kw)


def test_a_cold_cache_fetches_every_slot(cache, slots, counter):
    calls, _ = counter
    entries = _refresh(cache, slots)
    assert len(calls) == 2
    assert entries["1"].last_good == USAGE


def test_a_second_pass_serves_from_cache_without_a_request(cache, slots, counter):
    """The entire point of this stage: two `cswap codex list` calls in a row
    cost one round of requests, not two."""
    calls, _ = counter
    _refresh(cache, slots)
    _refresh(cache, slots)
    assert len(calls) == 2  # unchanged by the second pass


def test_the_cached_value_is_still_served_on_the_second_pass(cache, slots, counter):
    _refresh(cache, slots)
    entries = _refresh(cache, slots)
    assert entries["1"].last_good == USAGE
    assert entries["1"].age_s is not None


def test_entries_never_touches_the_network(cache, slots, counter):
    calls, _ = counter
    cache.entries(slots)
    assert calls == []


def test_a_slot_leased_by_another_process_is_not_fetched(cache, slots, counter):
    """Two collectors both passing the staleness check and both fetching is
    exactly what the lease exists to prevent."""
    calls, _ = counter
    other = CodexUsageCache(CodexStore())
    other._usage.reserve(["1", "2"], other.identities(slots), respect_plans=True)

    _refresh(cache, slots)

    assert calls == []


def test_a_failure_records_an_error_and_shows_it(cache, slots, counter):
    calls, result = counter
    result["value"] = UsageFetch(sentinel="http 401")
    entries = _refresh(cache, slots)
    assert entries["1"].sentinel == "http 401"
    assert entries["1"].last_good is None


def test_a_failure_does_not_destroy_the_previous_good_value(cache, slots, counter):
    """Serving stale-with-age beats serving blank."""
    calls, result = counter
    _refresh(cache, slots)  # succeed once
    result["value"] = UsageFetch(sentinel="http 500")

    # force the entries out of their serve window by aging the clock
    cache._usage.clock = lambda: __import__("time").time() + 86400
    entries = _refresh(cache, slots)

    assert entries["1"].last_good == USAGE  # preserved
    assert entries["1"].consecutive_failures >= 1


def test_a_retry_after_is_carried_into_the_record(cache, slots, counter):
    calls, result = counter
    result["value"] = UsageFetch(sentinel="http 429", retry_after_s=120.0)
    entries = _refresh(cache, slots)
    assert entries["1"].backoff_until is not None


def test_a_success_writes_a_poll_plan(cache, slots, counter):
    entries = _refresh(cache, slots)
    assert entries["1"].next_poll_at is not None
    assert entries["1"].poll_interval_s is not None


def test_an_api_key_slot_is_a_sentinel_and_makes_no_request(cache, codex_home: Path, counter):
    calls, _ = counter
    store = CodexStore()
    slot = store.upsert_slot(KEY_A, email="a@x", auth_mode="apikey")
    entries = _refresh(cache, [slot])
    assert calls == []
    assert entries["1"].sentinel == "api key"


def test_a_missing_snapshot_is_a_sentinel_not_a_failure(cache, codex_home: Path, counter):
    """A structurally unusable account must not accrue backoff it can never
    clear by itself."""
    calls, _ = counter
    store = CodexStore()
    slot = store.upsert_slot(KEY_A, email="a@x", plan="pro")
    entries = _refresh(cache, [slot])
    assert entries["1"].sentinel == "no credentials"
    assert entries["1"].consecutive_failures == 0


def test_the_identity_guard_hides_a_reused_slots_old_usage(cache, slots, counter):
    """A slot re-created for a different account must not inherit the previous
    account's percentages."""
    _refresh(cache, slots)
    store = CodexStore()
    store.remove_slot(KEY_A)
    fresh = store.upsert_slot(account_key("user-c", "acct-c"), email="c@x", plan="pro")
    assert fresh.number == "1"  # same slot number, different account

    assert cache.entries([fresh])["1"].last_good is None


def test_identity_uses_the_account_id_not_just_the_email(codex_home: Path):
    """One user with two workspaces has one email and two accounts; the email
    alone would let one workspace's usage serve the other."""
    slot = CodexSlot(number="1", account_key=account_key("user-a", "acct-x"), email="a@x")
    assert CodexUsageCache.identity_for(slot) == ("a@x", "acct-x")


def test_refresh_of_no_slots_is_a_noop(cache, counter):
    calls, _ = counter
    assert _refresh(cache, []) == {}
    assert calls == []
