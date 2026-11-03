"""`cswap auto` leaves an account the moment a session reports it stopped on
that account's limit (auto-continue's stop records), without waiting for the
usage endpoint: in a network glitch that endpoint is exactly what fails."""

from __future__ import annotations

import json
import logging
from unittest.mock import patch

import pytest

from claude_swap import autoswitch
from claude_swap import resume as rs
from claude_swap.autoswitch import NoSwitchEvent, SwitchEvent, TickOutcome
from claude_swap.settings import settings_path
from claude_swap.usage_store import UsageEntry
from tests.test_autoswitch import EngineHarness, _entry_for, _usage

SID = "0123abcd-0000-4000-8000-000000000000"


def _stop(
    h: EngineHarness,
    email: str,
    *,
    sid: str = SID,
    ago: float = 30.0,
    resets_in: float | None = 3600.0,
    **fields,
) -> rs.ResumeRecord:
    now = h.clock.now
    record = rs.ResumeRecord(
        session_id=sid,
        transcript_path="t.jsonl",
        config_home=str(h.temp_home / ".claude"),
        global_config_path=str(h.temp_home / ".claude.json"),
        default_profile=True,
        armed_at=now - ago,
        identity_email=email,
        identity_org="",
        stall_uuid="u-" + sid[:8],
        stalled_at=now - ago,
        resets_at=None if resets_in is None else now + resets_in,
        limit_type="five_hour",
        updated_at=now - ago,
    )
    record = rs.ResumeRecord(**{**record.__dict__, **fields})
    rs.mutate_record(h.switcher.backup_dir, sid, lambda _: record)
    return record


def _tick_stored_only(h: EngineHarness, usage: dict) -> TickOutcome:
    """Tick with the store holding ``usage``; fail if anything fetches."""
    entries = {num: _entry_for(value, h.clock.now) for num, value in usage.items()}

    def stored(fetch=None, *, scheduled=False) -> dict[str, UsageEntry]:
        assert fetch == set(), f"the stop path fetched usage (fetch={fetch!r})"
        return entries

    def no_network(*args, **kwargs):
        raise AssertionError("the stop path hit the usage endpoint")

    with (
        patch.object(h.switcher, "usage_entries_by_account", side_effect=stored),
        patch.object(h.switcher, "_fetch_account_usage", side_effect=no_network),
    ):
        return h.engine.tick()


def _switch(h: EngineHarness) -> SwitchEvent:
    return next(e for e in h.events if isinstance(e, SwitchEvent))


@pytest.fixture
def h(temp_home) -> EngineHarness:
    harness = EngineHarness(temp_home)
    harness.seed(1, "a@example.com")
    harness.seed(2, "b@example.com")
    harness.seed(3, "c@example.com")
    harness.make_live("a@example.com", 1)
    return harness


class TestAStopOnTheActiveAccountSwitchesAtOnce:
    def test_unreadable_active_switches_on_the_first_tick_without_fetching(self, h):
        _stop(h, "a@example.com")
        outcome = _tick_stored_only(h, {"1": None, "2": _usage(30), "3": _usage(60)})
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2
        assert _switch(h).trigger == "at-limit"
        # No "active-usage-unknown 1/3 before failover" counting first.
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert "active-usage-unknown" not in reasons

    def test_a_lagging_reading_below_the_line_does_not_hold_it(self, h):
        _stop(h, "a@example.com")
        outcome = _tick_stored_only(h, {"1": _usage(60), "2": _usage(30), "3": _usage(50)})
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2

    def test_a_readable_candidate_beats_an_unmeasured_one(self, h):
        _stop(h, "a@example.com")
        assert _tick_stored_only(h, {"1": None, "2": None, "3": _usage(40)}) is (
            TickOutcome.SWITCHED
        )
        assert h.active_number() == 3

    def test_an_exhausted_candidate_is_never_the_target(self, h):
        _stop(h, "a@example.com")
        assert _tick_stored_only(h, {"1": None, "2": _usage(100), "3": _usage(70)}) is (
            TickOutcome.SWITCHED
        )
        assert h.active_number() == 3

    def test_the_departure_is_recorded_as_spent_until_the_reported_reset(self, h):
        stop = _stop(h, "a@example.com")
        _tick_stored_only(h, {"1": None, "2": _usage(30), "3": _usage(60)})
        state = h.state()
        assert state["leftTrigger"] == "at-limit"
        assert state["leftHeadroom"] == 0.0
        assert state["leftRecoveryAt"] == stop.resets_at

    def test_a_stop_with_no_recorded_reset_counts_while_recent(self, h):
        _stop(h, "a@example.com", resets_in=None, ago=60.0)
        assert _tick_stored_only(h, {"1": None, "2": _usage(30)}) is TickOutcome.SWITCHED


class TestWhenNothingIsMeasurable:
    def test_lands_on_an_unmeasured_account_in_sequence_order(self, h):
        _stop(h, "a@example.com")
        assert _tick_stored_only(h, {"1": None, "2": None, "3": None}) is TickOutcome.SWITCHED
        assert h.active_number() == 2
        assert _switch(h).trigger == "at-limit"

    def test_skips_an_account_another_session_reported_limited(self, h):
        _stop(h, "a@example.com")
        _stop(h, "b@example.com", sid="4567efab-0000-4000-8000-000000000000", ago=600.0)
        assert _tick_stored_only(h, {"1": None, "2": None, "3": None}) is TickOutcome.SWITCHED
        assert h.active_number() == 3

    def test_without_a_stop_the_unmeasured_fleet_still_waits(self, h):
        # The blind landing is only for first-hand proof; unknown usage alone
        # keeps the ordinary failover counting.
        assert h.tick_with_usage({"1": None, "2": None, "3": None}) is TickOutcome.NO_ACTION
        assert h.active_number() == 1


class TestStopsThatDoNotCount:
    def _assert_ignored(self, h):
        outcome = h.tick_with_usage({"1": _usage(50), "2": _usage(10), "3": _usage(10)})
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["below-threshold"]

    def test_a_stop_on_another_account(self, h):
        _stop(h, "b@example.com")
        self._assert_ignored(h)

    def test_a_stop_from_before_the_last_switch(self, h):
        _stop(h, "a@example.com", ago=30.0)
        h.engine._mutate_state(lambda s: s.update(lastSwitchAt=h.clock.now - 10.0))
        self._assert_ignored(h)

    def test_a_stop_inside_the_settle_window_after_a_switch(self, h):
        # A session may still have been on the old account's token.
        _stop(h, "a@example.com", ago=30.0)
        h.engine._mutate_state(lambda s: s.update(lastSwitchAt=h.clock.now - 60.0))
        self._assert_ignored(h)

    @pytest.mark.parametrize(
        "fields",
        [
            {"state": rs.RESUMED},
            {"state": rs.MOVED_ON},
            {"state": rs.CANCELLED},
            {"rehearsal": True},
            {"default_profile": False},
        ],
        ids=["resumed", "moved-on", "cancelled", "rehearsal", "session-profile"],
    )
    def test_a_record_that_is_not_a_live_stop(self, h, fields):
        _stop(h, "a@example.com", **fields)
        self._assert_ignored(h)

    def test_a_limit_whose_reset_has_passed(self, h):
        _stop(h, "a@example.com", ago=7200.0, resets_in=-60.0)
        self._assert_ignored(h)

    def test_an_untimed_stop_older_than_a_five_hour_window(self, h):
        _stop(h, "a@example.com", resets_in=None, ago=rs.UNTIMED_STOP_TRUST_S + 60)
        self._assert_ignored(h)

    def test_switch_on_stop_turned_off(self, h):
        settings_path(h.switcher.backup_dir).write_text(
            json.dumps({"schemaVersion": 1, "resume": {"switchOnStop": False}}),
            encoding="utf-8",
        )
        _stop(h, "a@example.com")
        self._assert_ignored(h)

    def test_unreadable_records_never_break_a_tick(self, h):
        (rs.resume_dir(h.switcher.backup_dir) / f"{SID}.json").parent.mkdir(
            parents=True, exist_ok=True
        )
        (rs.resume_dir(h.switcher.backup_dir) / f"{SID}.json").write_text("{not json")
        self._assert_ignored(h)


class TestLimitStops:
    def test_newest_first(self, h):
        _stop(h, "a@example.com", ago=300.0)
        _stop(h, "b@example.com", sid="4567efab-0000-4000-8000-000000000000", ago=30.0)
        stops = rs.limit_stops(h.switcher.backup_dir, h.clock)
        assert [s.identity[0] for s in stops] == ["b@example.com", "a@example.com"]

    def test_the_clock_is_not_read_without_a_stop_to_judge(self, h):
        # Engine tests script clock() call by call; an idle check must not
        # shift them.
        def no_clock() -> float:
            raise AssertionError("clock read with no stop to judge")

        _stop(h, "a@example.com", state=rs.RESUMED)
        assert rs.limit_stops(h.switcher.backup_dir, no_clock) == []


class TestTheDecisionIsLoggedOnce:
    def _lines(self, caplog) -> list[str]:
        return [r.getMessage() for r in caplog.records if "leaving it now" in r.getMessage()]

    def test_a_blocked_switch_logs_its_stop_once(self, h, caplog):
        _stop(h, "a@example.com")
        with caplog.at_level(logging.INFO):
            for _ in range(3):  # every candidate exhausted: blocked each tick
                _tick_stored_only(h, {"1": None, "2": _usage(100), "3": _usage(100)})
        assert h.active_number() == 1
        assert self._lines(caplog) == [
            f"Session {SID[:8]} stopped on account 1's five_hour limit; leaving it now"
        ]

    def test_a_dry_run_engine_logs_nothing(self, h, caplog):
        _stop(h, "a@example.com")
        h.engine = h._make_engine(dry_run=True)
        with caplog.at_level(logging.INFO):
            _tick_stored_only(h, {"1": None, "2": _usage(30)})
        assert self._lines(caplog) == []
        assert _switch(h).dry_run


def test_a_slow_tick_is_logged(h, caplog):
    with patch.object(autoswitch, "SLOW_TICK_S", -1.0), caplog.at_level(logging.WARNING):
        h.tick_with_usage({"1": _usage(50), "2": _usage(10), "3": _usage(10)})
    assert any("stalled?" in r.getMessage() for r in caplog.records)
