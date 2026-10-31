"""T3167: the percent alternation step on a mixed fleet, from the pairwise model
`tests/pict/autoswitch_mixed_fleet.pict`.

One due active (5h 40%, a step of 20 spent) and one partner. Each row below is a
generated case with the decision the rules give it: the partner is taken iff it
keeps `step + SPENT_HEADROOM_PCT` points of room on its binding window, a cold
one after its re-write is charged to that window (5h points where it has a 5h
window, 7d points where it has not), a model window the user pins counted in
the room.

Plan and context set `coldSwitchCostPct`. One live session's prefix costs
`tokens x 1.9 units / (2.08M units per 5h point x plan scale)` points
(alternation-replay.md, calibration; Max 20x scale 1, Max 5x 0.25), and the
setting prices a whole live fleet landing together, about 19 sessions
(settings.py): 100k tokens on 20x = 1.7 points, 400k = 6.9, and 4x those on 5x.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_alternation_step import _harness, _iso_at, _reasons, _stamp, _tick, _track, _u

COST = {("max20x", "small"): 2.0, ("max20x", "large"): 7.0,
        ("max5x", "small"): 7.0, ("max5x", "large"): 28.0}
SEVEN = {"low": 20.0, "mid": 50.0, "near_cap": 90.0, "reset_from_full": 0.0}
SWITCH, HOLD = True, False

# plan, 5h shape, 7d level, partner, context, pinned model window -> the decision
CASES = [
    ("max5x", "running", "mid", "cold", "large", "none", HOLD),     # 50 - 28 = 22 < 23
    ("max5x", "removed", "mid", "cold", "large", "open", SWITCH),   # 28 / 4.4 = 6.4 on the 7d: 43.6
    ("max5x", "idle", "mid", "warm", "large", "none", SWITCH),      # warm: 50
    ("max20x", "running", "near_cap", "warm", "small", "full", HOLD),
    ("max20x", "idle", "mid", "cold", "small", "full", HOLD),       # the pinned window is full
    ("max20x", "removed", "reset_from_full", "warm", "small", "open", SWITCH),
    ("max5x", "running", "reset_from_full", "cold", "large", "full", HOLD),
    ("max5x", "idle", "reset_from_full", "warm", "small", "none", SWITCH),
    ("max20x", "removed", "low", "cold", "large", "none", SWITCH),
    ("max5x", "idle", "low", "warm", "small", "open", SWITCH),
    ("max5x", "removed", "near_cap", "cold", "large", "open", HOLD),  # 10 points of room
    ("max5x", "idle", "near_cap", "warm", "large", "none", HOLD),
    ("max5x", "removed", "low", "cold", "large", "full", HOLD),
    ("max20x", "running", "low", "cold", "small", "open", SWITCH),
]


def test_the_cases_are_the_generated_ones():
    cases = Path(__file__).parent / "pict" / "autoswitch_mixed_fleet.cases"
    rows = [line.split("\t") for line in cases.read_text().splitlines()[1:]]
    assert rows == [list(case[:6]) for case in CASES]


@pytest.mark.parametrize("case", CASES, ids=lambda c: "-".join(c[:6]))
def test_the_step_on_a_mixed_fleet(temp_home, case):
    plan, five, seven, partner, context, pinned, switch = case
    h = _harness(
        temp_home,
        cold_switch_cost_pct=COST[plan, context],
        model=None if pinned == "none" else "Fable",
    )
    b = _u({"running": 20.0, "idle": 0.0, "removed": None}[five], SEVEN[seven])
    if five == "running":
        b["five_hour"]["resets_at"] = _iso_at(h.clock.now + 7200.0)
    if seven != "reset_from_full":
        b["seven_day"]["resets_at"] = _iso_at(h.clock.now + 3 * 86400.0)
    if pinned != "none":
        b["scoped"] = [{"name": "Fable", "pct": 10.0 if pinned == "open" else 100.0}]
    _stamp(h, **{"1": 60.0, **({"2": 120.0} if partner == "warm" else {})})
    _track(h, anchor=0.0)
    outcome = _tick(h, {"1": _u(40.0), "2": b, "3": _u(100.0, 100.0)})
    assert h.active_number() == (2 if switch else 1), (outcome, _reasons(h))
