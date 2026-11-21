"""An account with no five-hour window is handled on its weekly windows alone.

Ultra accounts report only weekly windows. The engine runs in dry-run mode;
manual activation is mocked. All account files live under the ``temp_home``
fixture.
"""

from unittest.mock import patch

import pytest

from claude_swap import poll_policy
from claude_swap.autoswitch import NoSwitchEvent, SwitchEvent, TickOutcome
from tests.test_autoswitch import EngineHarness, _iso_at


def _harness(temp_home, *, accounts=3, **settings):
    h = EngineHarness(temp_home, **settings)
    for number in range(1, accounts + 1):
        h.seed(number, f"account{number}@example.com")
    h.make_live("account1@example.com", 1)
    h.engine = h._make_engine(dry_run=True)
    return h


def _weekly(h, pct, *, reset_in=3600):
    window = {"pct": pct}
    if reset_in is not None:
        window["resets_at"] = _iso_at(h.clock.now + reset_in)
    return {"seven_day": window}


def _targets(h):
    return [event.to_ref["number"] for event in h.events if isinstance(event, SwitchEvent)]


@pytest.mark.parametrize("strategy", ["best", "consume-first", "priority-consume-first"])
def test_weekly_only_active_can_switch_to_weekly_only_candidate(temp_home, strategy):
    h = _harness(temp_home, strategy=strategy)
    outcome = h.tick_with_usage({
        "1": _weekly(h, 95, reset_in=7200),
        "2": _weekly(h, 20),
        "3": {**_weekly(h, 30, reset_in=10800), "five_hour": {"pct": 10}},
    })

    assert outcome is TickOutcome.SWITCHED
    assert _targets(h) == [2]


@pytest.mark.parametrize("strategy", ["best", "consume-first", "priority-consume-first"])
def test_auto_skips_exhausted_weekly_and_unknown_candidates(temp_home, strategy):
    h = _harness(temp_home, strategy=strategy, accounts=4)
    outcome = h.tick_with_usage({
        "1": _weekly(h, 100, reset_in=7200),
        "2": _weekly(h, 100),
        "3": None,
        "4": _weekly(h, 30, reset_in=10800),
    })

    assert outcome is TickOutcome.SWITCHED
    assert _targets(h) == [4]


def test_weekly_only_holds_below_the_weekly_switch_line(temp_home):
    h = _harness(temp_home, threshold=90, weekly_threshold=97)
    outcome = h.tick_with_usage({
        "1": _weekly(h, 95),
        "2": _weekly(h, 20),
        "3": _weekly(h, 30),
    })

    assert outcome is TickOutcome.NO_ACTION
    assert not _targets(h)
    event = h.events[-1]
    assert isinstance(event, NoSwitchEvent)
    assert event.detail == "7d 95% < 97%"


def test_weekly_only_switches_at_the_weekly_switch_line(temp_home):
    h = _harness(temp_home, threshold=90, weekly_threshold=97)
    outcome = h.tick_with_usage({
        "1": _weekly(h, 98),
        "2": _weekly(h, 20),
        "3": _weekly(h, 30),
    })

    assert outcome is TickOutcome.SWITCHED
    assert _targets(h) == [2]


def test_scoped_limit_still_binds_without_a_five_hour_window(temp_home):
    h = _harness(temp_home, model="Fable", weekly_threshold=97)
    outcome = h.tick_with_usage({
        "1": {**_weekly(h, 20), "scoped": [{"name": "Fable", "pct": 100}]},
        "2": {**_weekly(h, 10), "scoped": [{"name": "Fable", "pct": 100}]},
        "3": {**_weekly(h, 30), "scoped": [{"name": "Fable", "pct": 20}]},
    })

    assert outcome is TickOutcome.SWITCHED
    assert _targets(h) == [3]


@pytest.mark.parametrize("strategy", ["best", "next-available"])
def test_manual_usage_strategies_skip_exhausted_weekly_candidate(temp_home, strategy):
    h = _harness(temp_home)
    usage = {"1": _weekly(h, 95), "2": _weekly(h, 100), "3": _weekly(h, 30)}
    with (
        patch.object(h.switcher, "_usage_by_account", return_value=usage),
        patch.object(h.switcher, "_perform_switch") as activate,
    ):
        h.switcher.switch(strategy=strategy)

    assert activate.call_count == 1
    assert activate.call_args.args[0] == "3"


def test_weekly_only_polling_responds_to_burn_and_imminent_reset(temp_home):
    h = _harness(temp_home)
    next_poll, interval = poll_policy.plan_after_fetch(
        prev_interval_s=300,
        prev_usage=_weekly(h, 80),
        new_usage=_weekly(h, 85),
        is_active=True,
        threshold=90,
        models=(),
        recent_429=False,
        now=h.clock.now,
        rng=lambda: 0.5,
    )

    assert interval == poll_policy.URGENT_INTERVAL_S
    assert next_poll == h.clock.now + poll_policy.URGENT_INTERVAL_S

    # Flat usage backs off, but its next reset must still cap the deadline.
    next_poll, interval = poll_policy.plan_after_fetch(
        prev_interval_s=300,
        prev_usage=_weekly(h, 80, reset_in=10),
        new_usage=_weekly(h, 80, reset_in=10),
        is_active=True,
        threshold=90,
        models=(),
        recent_429=False,
        now=h.clock.now,
        rng=lambda: 0.5,
    )

    assert next_poll == h.clock.now + 10 + poll_policy.RESET_SLACK_S
    assert next_poll < h.clock.now + interval
