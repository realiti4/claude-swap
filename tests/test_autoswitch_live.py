"""``auto`` decides on a ``cswap proxy`` live reading, through the real store.

The case this exists for: the poll says 79% (minutes old: the usage endpoint
allows about 30 requests an hour per account), while the account's own API
replies already say 91%. Without the live reading the engine holds; with it,
it switches before the session reaches the limit.
"""

from __future__ import annotations

from claude_swap.autoswitch import NoSwitchEvent, SwitchEvent, TickOutcome
from claude_swap.usage_store import STALE_OK_S, FetchRecord
from tests.test_autoswitch import EngineHarness, _usage


def _harness(temp_home) -> EngineHarness:
    h = EngineHarness(temp_home)
    h.seed(1, "a@example.com")
    h.seed(2, "b@example.com")
    h.seed(3, "c@example.com")
    h.make_live("a@example.com", 1)
    return h


def _identities(h: EngineHarness) -> dict:
    accounts = h.switcher._get_sequence_data()["accounts"]
    return {num: (info["email"], info.get("organizationUuid", "")) for num, info in accounts.items()}


def _poll_all(h: EngineHarness, active_pct: float) -> None:
    ids = _identities(h)
    h.switcher._usage_store.record(
        {
            "1": FetchRecord(usage=_usage(active_pct)),
            "2": FetchRecord(usage=_usage(0.0)),
            "3": FetchRecord(usage=_usage(0.0)),
        },
        ids,
    )


def _tick_on_store(h: EngineHarness) -> TickOutcome:
    # Entries straight from the store the proxy writes to, overlay included;
    # only the network fetch is left out.
    return h.tick_with_entries(h.switcher._usage_store.entries(_identities(h)))


def test_a_stale_poll_alone_does_not_switch(temp_home):
    h = _harness(temp_home)
    _poll_all(h, 79.0)
    h.clock.advance(30)
    assert _tick_on_store(h) == TickOutcome.NO_ACTION
    assert h.active_number() == 1


def test_a_live_reading_over_the_threshold_switches(temp_home):
    h = _harness(temp_home)
    _poll_all(h, 79.0)
    h.clock.advance(30)
    h.switcher._usage_store.record_live(
        {"1": {"five_hour": {"pct": 91.0}, "seven_day": {"pct": 40.0}}},
        {"1": _identities(h)["1"]},
    )
    assert _tick_on_store(h) == TickOutcome.SWITCHED
    assert h.active_number() != 1
    assert any(isinstance(e, SwitchEvent) for e in h.events)


def test_a_live_reading_too_old_to_trust_does_not_switch(temp_home):
    h = _harness(temp_home)
    h.clock.advance(1)
    h.switcher._usage_store.record_live(
        {"1": {"five_hour": {"pct": 91.0}, "seven_day": {"pct": 40.0}}},
        {"1": _identities(h)["1"]},
    )
    h.clock.advance(STALE_OK_S + 1)
    _poll_all(h, 79.0)  # the poll is now the newer, trusted reading
    assert _tick_on_store(h) == TickOutcome.NO_ACTION
    assert h.active_number() == 1
    assert any(isinstance(e, NoSwitchEvent) for e in h.events)
