"""T3167: `dynamic` alternates on a PERCENT step of the active's binding
window (`autoswitch.alternationChunkPct`), warms cold accounts early, ranks a
reset-from-full account first, and charges a cold landing to the window the
account really has.

Replay evidence and the knob choices: `check-cswap`'s alternation-replay.md.
The 5h-removed accounts are real captures (slots 7 and 8, 2026-10-31).
"""

from __future__ import annotations

import copy
import dataclasses
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from claude_swap import oauth
from claude_swap.autoswitch import (
    NoSwitchEvent,
    SwitchEvent,
    TickOutcome,
    _cold_landing_cost_pct,
)
from tests.test_autoswitch import EngineHarness, _entry_for, _iso_at

FIXTURES = Path(__file__).parent / "fixtures"
DAY = 86400.0
STEP = 20.0


def _u(five: float | None, seven: float = 0.0, *, reset7: float | None = None) -> dict:
    """A usage reading: a 5h window (None = none reported) and a 7d one."""
    usage: dict = {"seven_day": {"pct": seven}}
    if reset7 is not None:
        usage["seven_day"]["resets_at"] = _iso_at(reset7)
    if five is not None:
        usage["five_hour"] = {"pct": five}
    return usage


def _capture(name: str, seven: float | None = None) -> dict:
    """The parsed usage of a real removed-5h capture, its windows moved on to
    now, optionally with the 7d level changed."""
    raw = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    for window in (raw["seven_day"], *(l for l in raw["limits"])):
        if window.get("resets_at"):
            window["resets_at"] = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    usage = oauth.build_usage_result(raw)
    assert usage is not None and "five_hour" not in usage
    if seven is not None:
        usage["seven_day"]["pct"] = seven
    return usage


def _harness(temp_home, **kwargs) -> EngineHarness:
    h = EngineHarness(temp_home, strategy="dynamic", **kwargs)
    for n in (1, 2, 3):
        h.seed(n, f"acct{n}@example.invalid")
    h.make_live("acct1@example.invalid", 1)
    return h


def _state_path(h: EngineHarness) -> Path:
    return h.switcher.backup_dir / "autoswitch_state.json"


def _write_state(h: EngineHarness, **keys) -> None:
    path = _state_path(h)
    raw = json.loads(path.read_text()) if path.exists() else {"schemaVersion": 1}
    raw.update(keys)
    path.write_text(json.dumps(raw))


def _stamp(h: EngineHarness, **ages: float) -> None:
    """lastActiveAt: account number -> seconds ago (the active's stamp is its landing)."""
    _write_state(h, lastActiveAt={n: h.clock.now - age for n, age in ages.items()})


def _track(h: EngineHarness, *, anchor: float, rise: float = 0.0, ran: bool = False) -> None:
    """The tracker of a stay on account 1 that started when it was stamped."""
    since = json.loads(_state_path(h).read_text())["lastActiveAt"]["1"]
    _write_state(h, alternation={
        "account": "1", "since": since, "anchor": anchor, "rise": rise, "ran": ran,
    })


def _tick(
    h: EngineHarness, usage: dict, advance: float = 360.0, plans: dict | None = None
) -> TickOutcome:
    """One tick on a NEW engine, as every `cswap auto --once` run (and the first
    tick after a restart) is: nothing but the state file carries over. ``plans``
    (account number -> plan label) stamps each store entry's plan."""
    h.clock.advance(advance)
    h.engine = h._make_engine()
    if plans is None:
        return h.tick_with_usage(usage)
    return h.tick_with_entries({
        n: dataclasses.replace(_entry_for(v, h.clock.now), plan=plans.get(n))
        for n, v in usage.items()
    })


def _reasons(h: EngineHarness) -> list[str]:
    return [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]


def _trigger(h: EngineHarness) -> str:
    return next(e for e in h.events if isinstance(e, SwitchEvent)).trigger


# -- the step ---------------------------------------------------------------


def test_it_alternates_at_plus_step_and_not_before(temp_home):
    h = _harness(temp_home)
    _stamp(h, **{"1": 60.0, "2": 120.0})  # 2 is the warm partner
    spent = _u(100.0, 100.0)
    for level in (30.0, 49.0):  # the first tick anchors; +19 is short of 20
        assert _tick(h, {"1": _u(level), "2": _u(10.0), "3": spent}) is TickOutcome.NO_ACTION
        assert h.active_number() == 1
    assert _reasons(h) == ["below-threshold", "below-threshold"]
    assert _tick(h, {"1": _u(50.0), "2": _u(10.0), "3": spent}) is TickOutcome.SWITCHED
    assert (h.active_number(), _trigger(h)) == (2, "alternation")


def test_the_step_is_read_from_settings(temp_home):
    h = _harness(temp_home, alternation_chunk_pct=5.0)
    _stamp(h, **{"1": 60.0, "2": 120.0})
    spent = _u(100.0, 100.0)
    assert _tick(h, {"1": _u(30.0), "2": _u(10.0), "3": spent}) is TickOutcome.NO_ACTION
    assert _tick(h, {"1": _u(35.0), "2": _u(10.0), "3": spent}) is TickOutcome.SWITCHED


def test_a_window_roll_mid_stay_neither_stalls_nor_discards_the_spend(temp_home):
    h = _harness(temp_home)
    _stamp(h, **{"1": 60.0, "2": 120.0})
    spent = _u(100.0, 100.0)
    for level in (30.0, 40.0):  # 10 points spent before the roll
        assert _tick(h, {"1": _u(level), "2": _u(10.0), "3": spent}) is TickOutcome.NO_ACTION
    assert _tick(h, {"1": _u(5.0), "2": _u(10.0), "3": spent}) is TickOutcome.NO_ACTION  # the roll
    saved = json.loads(_state_path(h).read_text())["alternation"]
    assert (saved["anchor"], saved["rise"]) == (5.0, 10.0)
    # +10 on the new level: 10 kept from before the roll plus 10 after it is the step.
    assert _tick(h, {"1": _u(15.0), "2": _u(10.0), "3": spent}) is TickOutcome.SWITCHED


@pytest.mark.parametrize(
    "dwell, five_after, seven_after, expect_switch",
    [
        (700.0, 12.0, 60.0, True),    # traffic ran (5h moved), 7d reading unmoved, dwell served
        (700.0, 10.0, 60.0, False),   # nothing moved: the active served nothing
        (500.0, 12.0, 60.0, False),   # traffic, but the dwell is short
        (700.0, 12.0, 61.0, False),   # the binding reading moved: the step decides
    ],
)
def test_the_dwell_fallback_fires_only_on_an_unmoved_reading_with_traffic(
    temp_home, dwell, five_after, seven_after, expect_switch
):
    h = _harness(temp_home)
    _stamp(h, **{"1": dwell - 360.0, "2": 120.0})  # the dwell at the second tick
    spent = _u(100.0, 100.0)
    assert _tick(h, {"1": _u(10.0, 60.0), "2": _u(10.0), "3": spent}, 0.0) is TickOutcome.NO_ACTION
    outcome = _tick(h, {"1": _u(five_after, seven_after), "2": _u(10.0), "3": spent})
    assert (outcome is TickOutcome.SWITCHED) is expect_switch


def test_a_pre_existing_or_broken_state_file_just_anchors(temp_home):
    h = _harness(temp_home)
    _stamp(h, **{"1": 60.0, "2": 120.0})
    spent = _u(100.0, 100.0)
    for stored in (None, "garbage", {"account": "1"}, {"account": 1, "since": "x", "anchor": "y"}):
        if stored is not None:
            _write_state(h, alternation=stored)
        assert _tick(h, {"1": _u(30.0), "2": _u(10.0), "3": spent}) is TickOutcome.NO_ACTION
        saved = json.loads(_state_path(h).read_text())["alternation"]
        assert saved["account"] == "1" and saved["anchor"] == 30.0 and saved["rise"] == 0.0


def test_a_new_landing_starts_a_new_stay(temp_home):
    """The tracker belongs to a stay: a landing stamp it does not name re-anchors."""
    h = _harness(temp_home)
    _stamp(h, **{"1": 60.0, "2": 120.0})
    spent = _u(100.0, 100.0)
    _track(h, anchor=0.0, rise=0.0)
    _stamp(h, **{"1": 30.0, "2": 120.0})  # landed again on 1 since the tracker was written
    assert _tick(h, {"1": _u(60.0), "2": _u(10.0), "3": spent}) is TickOutcome.NO_ACTION
    assert json.loads(_state_path(h).read_text())["alternation"]["anchor"] == 60.0


def test_zero_turns_the_step_off_and_keeps_the_dwell_timer(temp_home):
    """alternationChunkPct 0: today's time-only alternation, an idle pair trades."""
    h = _harness(temp_home, alternation_chunk_pct=0.0)
    chunk = h.engine.settings.alternation_chunk_seconds
    _stamp(h, **{"1": chunk, "2": 10.0})
    assert _tick(h, {"1": _u(50.0), "2": _u(45.0)}, 0.0) is TickOutcome.SWITCHED
    assert "alternation" not in json.loads(_state_path(h).read_text())


# -- admission: cold and warm compete in one list ---------------------------


@pytest.mark.parametrize(
    "five_b, cost, expect_switch",
    [
        (50.0, 20.0, True),    # 100 - 50 - 20 = 30 >= step + 3
        (60.0, 20.0, False),   # 100 - 60 - 20 = 20 < 23: the landing would leave a sliver
        (60.0, 1.0, True),     # the same account when the landing is cheap
    ],
)
def test_early_warming_admits_a_cold_account_while_its_landing_is_cheap(
    temp_home, five_b, cost, expect_switch
):
    h = _harness(temp_home, cold_switch_cost_pct=cost)
    _stamp(h, **{"1": 60.0})  # no stamp for 2: cold
    _track(h, anchor=0.0)
    outcome = _tick(h, {"1": _u(40.0), "2": _u(five_b), "3": _u(100.0, 100.0)})
    assert (outcome is TickOutcome.SWITCHED) is expect_switch
    if expect_switch:
        assert (h.active_number(), _trigger(h)) == (2, "alternation")
    else:
        assert _reasons(h) == ["below-floor"]


def test_a_cold_account_with_more_room_beats_a_warm_one_on_the_alternation_arm(temp_home):
    """No warm-before-cold split: the top ranked admissible account wins."""
    h = _harness(temp_home)
    _stamp(h, **{"1": 60.0, "2": 120.0})  # 2 warm at h 40; 3 cold at h 90
    _track(h, anchor=0.0)
    assert _tick(h, {"1": _u(40.0), "2": _u(60.0), "3": _u(10.0)}) is TickOutcome.SWITCHED
    assert h.active_number() == 3


def test_an_account_without_room_for_the_step_is_not_admissible_even_warm(temp_home):
    h = _harness(temp_home)
    _stamp(h, **{"1": 60.0, "2": 120.0})
    _track(h, anchor=0.0)
    assert _tick(h, {"1": _u(40.0), "2": _u(80.0), "3": _u(100.0, 100.0)}) is TickOutcome.NO_ACTION
    assert h.active_number() == 1


# -- ranking: a reset-from-full account is first-class ------------------------


@pytest.mark.parametrize(
    "reset_b, expect",
    [
        (3 * DAY, 3),        # nobody resets within a day: the fresh account has the most room
        (3 * 3600.0, 2),     # B resets in 3 h: use it or lose it
    ],
)
def test_a_zero_percent_account_with_no_reset_is_not_ranked_last(temp_home, reset_b, expect):
    h = _harness(temp_home)
    now = h.clock.now + 360.0
    outcome = _tick(h, {
        "1": _u(98.0),                                  # walled
        "2": _u(30.0, 40.0, reset7=now + reset_b),      # cold, headroom 60
        "3": _u(0.0, 0.0),                              # cold, reset-from-full: no resets_at
    })
    assert outcome is TickOutcome.SWITCHED
    assert (h.active_number(), _trigger(h)) == (expect, "proactive")


# -- the binding window of an account without a 5h window ---------------------


def test_the_real_removed_5h_captures_bind_on_7d():
    for name, level in (("usage_removed_5h.json", 97.0), ("usage_removed_5h_slot7.json", 53.0)):
        usage = _capture(name)
        assert oauth.account_headroom(usage) == 100.0 - level
        assert oauth.binding_window_label(usage) == "7d"


def test_a_removed_5h_active_alternates_on_its_7d_step(temp_home):
    h = _harness(temp_home)
    _stamp(h, **{"1": 60.0, "2": 120.0})
    spent = _u(100.0, 100.0)
    assert _tick(h, {"1": _capture("usage_removed_5h_slot7.json"), "2": _u(10.0), "3": spent}) is TickOutcome.NO_ACTION
    assert json.loads(_state_path(h).read_text())["alternation"]["anchor"] == 53.0
    assert _tick(h, {"1": _capture("usage_removed_5h_slot7.json", 72.0), "2": _u(10.0), "3": spent}) is TickOutcome.NO_ACTION
    assert _tick(h, {"1": _capture("usage_removed_5h_slot7.json", 73.0), "2": _u(10.0), "3": spent}) is TickOutcome.SWITCHED


def test_a_walled_removed_5h_capture_leaves_by_its_7d(temp_home):
    h = _harness(temp_home)
    outcome = _tick(h, {"1": _capture("usage_removed_5h.json"), "2": _u(10.0), "3": _u(20.0)})
    assert outcome is TickOutcome.SWITCHED
    assert _trigger(h) == "proactive"


# -- the cold landing is charged to a window the account has -------------------


def test_the_cost_is_charged_to_the_7d_when_there_is_no_5h_window():
    five = _u(10.0, 85.0)
    idle = {"five_hour": {"pct": 0.0}, "seven_day": {"pct": 85.0}}
    removed = _capture("usage_removed_5h_slot7.json", 85.0)
    assert _cold_landing_cost_pct(five, 20.0) == 20.0
    assert _cold_landing_cost_pct(idle, 20.0) == 20.0  # an idle 5h window is still a window
    assert _cold_landing_cost_pct(removed, 20.0) == pytest.approx(20.0 / 4.4)
    assert _cold_landing_cost_pct(None, 20.0) == 20.0  # unreadable: the conservative figure


def test_the_cost_is_the_ultra_figure_and_four_times_that_on_max_5x():
    five = _u(10.0, 85.0)
    removed = _capture("usage_removed_5h_slot7.json", 85.0)
    assert _cold_landing_cost_pct(five, 7.0, "Ultra") == 7.0
    assert _cold_landing_cost_pct(five, 7.0, "Max 5x") == 28.0
    assert _cold_landing_cost_pct(five, 7.0, None) == 7.0
    # a tier-less "Max" blob scales 1.0 (undercharged if it is really 5x), as does Pro
    assert _cold_landing_cost_pct(five, 7.0, "Max") == _cold_landing_cost_pct(five, 7.0, "Pro") == 7.0
    # the plan scale comes first, the no-5h ratio after it
    assert _cold_landing_cost_pct(removed, 7.0, "Max 5x") == pytest.approx(28.0 / 4.4)
    assert _cold_landing_cost_pct(removed, 7.0, "Ultra") == pytest.approx(7.0 / 4.4)


@pytest.mark.parametrize(
    "candidate, warm, cost, expect_switch",
    [
        # a large-context landing (cost 80 5h-points = 18.2 7d-points) on 15 points of 7d room
        (_capture("usage_removed_5h_slot7.json", 85.0), False, 80.0, False),
        # warm pays the same figure as its floor (never 0), so 15 < 18.2 is refused too
        (_capture("usage_removed_5h_slot7.json", 85.0), True, 80.0, False),
        # 20 points of room: warm clears 18.2, cold does not clear 18.2 + 3
        (_capture("usage_removed_5h_slot7.json", 80.0), True, 80.0, True),
        (_capture("usage_removed_5h_slot7.json", 80.0), False, 80.0, False),
        # the default cost is 4.5 7d-points: T3113's case, 15 - 4.5 clears the wall
        (_capture("usage_removed_5h_slot7.json", 85.0), False, 20.0, True),
        # the same 7d room behind a live 5h window keeps today's refusal
        (_u(10.0, 85.0), False, 20.0, False),
    ],
    ids=[
        "large-cold-removed", "large-warm-removed", "large-warm-removed-roomy",
        "large-cold-removed-roomy", "default-cold-removed", "default-cold-live-5h",
    ],
)
def test_a_cold_landing_on_a_removed_5h_account_is_priced_in_7d(
    temp_home, candidate, warm, cost, expect_switch
):
    """The time-only arm (step 0) carries the floors."""
    h = _harness(temp_home, alternation_chunk_pct=0.0, cold_switch_cost_pct=cost)
    chunk = h.engine.settings.alternation_chunk_seconds
    _stamp(h, **{"1": chunk + 360.0, **({"2": 120.0} if warm else {})})
    outcome = _tick(h, {"1": _u(30.0), "2": copy.deepcopy(candidate), "3": _u(100.0, 100.0)})
    assert (outcome is TickOutcome.SWITCHED) is expect_switch


@pytest.mark.parametrize(
    "five, expect_switch",
    [
        (20.0, False),   # 5h points: 28 - 7 = 21 < step + 3
        (None, True),    # 7d points: 28 - 7 / 4.4 = 26.4 >= 23
    ],
    ids=["running-5h", "removed-5h"],
)
def test_the_cold_charge_lands_on_a_pinned_model_window_in_the_unit_the_5h_picks(
    temp_home, five, expect_switch
):
    """The Fable window binds the partner (72%, room 28); the charge lands on it,
    in 5h points while a 5h window runs and at the 7d ratio once it is gone."""
    h = _harness(temp_home, cold_switch_cost_pct=7.0, model="Fable")
    _stamp(h, **{"1": 60.0})
    _track(h, anchor=0.0)
    fable = {**_u(five, 20.0), "scoped": [{"name": "Fable", "pct": 72.0}]}
    outcome = _tick(h, {"1": _u(40.0), "2": fable, "3": _u(100.0, 100.0)})
    assert (outcome is TickOutcome.SWITCHED) is expect_switch
    if not expect_switch:
        assert _reasons(h) == ["below-floor"]


def test_the_walled_escape_does_not_call_a_spent_warm_removed_5h_partner_qualified(temp_home):
    """The walled escape drops a cold peer once a QUALIFIED warm partner exists.
    A warm no-5h account with 4 points of room does not qualify (its floor is the
    cold figure in its own window, 20 / 4.4 = 4.5, not 0), so the cold peer with
    15 points that resets sooner is still taken, as it was before the step."""
    h = _harness(temp_home, model="Fable", threshold=70.0, alternation_chunk_pct=0.0)
    _stamp(h, **{"2": 120.0})
    fable = lambda pct, reset=None: [{"name": "Fable", "pct": pct, **({"resets_at": reset} if reset else {})}]  # noqa: E731
    outcome = _tick(h, {
        "1": {**_u(30.0, 40.0), "scoped": fable(100.0)},            # walled on Fable
        "2": {"seven_day": {"pct": 96.0}, "scoped": fable(10.0)},   # warm, no 5h, room 4
        "3": {**_u(5.0, 10.0), "scoped": fable(85.0, _iso_at(h.clock.now + 3960.0))},  # cold, room 15
    })
    assert outcome is TickOutcome.SWITCHED
    assert h.active_number() == 3


# -- planning the forced cold landing ------------------------------------------


@pytest.mark.parametrize(
    "dwell, five_b, five_c, expect",
    [
        (700.0, 94.0, 10.0, 3),     # the warm set is nearly spent: land on cold 3 now, prefix small
        (300.0, 94.0, 10.0, None),  # once per alternationChunkSeconds: the dwell is short
        (700.0, 40.0, 10.0, None),  # the warm partner has room: nothing to plan
        (700.0, 94.0, 80.0, None),  # the cold account would arrive with a sliver after its landing
        (700.0, 94.0, 58.0, None),  # 42 - 20 = 22: more than the active's 20, short of step + SPENT
    ],
    ids=["planned", "dwell-short", "warm-room", "cold-sliver", "short-of-step"],
)
def test_the_forced_cold_landing_is_planned_before_the_warm_room_runs_out(
    temp_home, dwell, five_b, five_c, expect
):
    h = _harness(temp_home)
    _stamp(h, **{"1": dwell, "2": 120.0})  # 2 warm, 3 cold
    _track(h, anchor=80.0)  # the active has spent nothing since landing: not due
    outcome = _tick(h, {"1": _u(80.0), "2": _u(five_b), "3": _u(five_c)}, 0.0)
    if expect is None:
        assert outcome is TickOutcome.NO_ACTION and h.active_number() == 1
    else:
        assert outcome is TickOutcome.SWITCHED and h.active_number() == expect
        assert _trigger(h) == "alternation"


def test_the_planned_landing_ranks_by_perish_then_room_not_by_cost(temp_home):
    """3 resets its 7d in 3 h (use it or lose it), 2 has no 5h window and so the
    cheaper landing: the planned landing takes 3, the step arm's order."""
    h = _harness(temp_home)
    _stamp(h, **{"1": 700.0})  # nobody warm
    _track(h, anchor=80.0)
    outcome = _tick(h, {
        "1": _u(80.0),
        "2": _capture("usage_removed_5h_slot7.json", 10.0),
        "3": _u(40.0, 30.0, reset7=h.clock.now + 360.0 + 3 * 3600.0),
    }, 0.0)
    assert outcome is TickOutcome.SWITCHED and h.active_number() == 3


# -- the label of a tick the step holds ------------------------------------------


def test_a_cold_only_fleet_not_yet_due_says_the_step_holds_it_not_the_floor(temp_home):
    h = _harness(temp_home)
    _stamp(h, **{"1": 60.0})  # nobody warm
    _track(h, anchor=40.0)
    assert _tick(h, {"1": _u(45.0), "2": _u(10.0), "3": _u(100.0, 100.0)}) is TickOutcome.NO_ACTION
    event = next(e for e in h.events if isinstance(e, NoSwitchEvent))
    assert event.reason == "below-threshold" and "step" in event.detail
