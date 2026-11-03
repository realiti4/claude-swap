"""T3167: the percent alternation step on a mixed fleet, from the pairwise model
`tests/pict/autoswitch_mixed_fleet.pict`.

One due active (5h 40%, a step of 20 spent) and two candidates. Candidate 2 takes
the row's windows (5h, 7d, a pinned model window) and its Plan; candidate 3 is
always cold on the plan PartnerPlan, with 5h 20% and 7d 60% (40 points of room).
Each row below is a generated case with the account the rules take (1 = hold): a
candidate is taken iff it keeps `step + SPENT_HEADROOM_PCT` = 23 points of room on
its binding window, a cold one after its re-write is charged to that window (5h
points where it has a 5h window, 7d points where it has not, a model window the
user pins counted in the room), and of the candidates that do the one with the
most headroom first. `_rules` is that paragraph in code: the table must equal it,
and a branch that cannot read a plan skips the rows it decides differently at scale 1.
The 7d level `edge` leaves 24 points: a removed-5h cold candidate clears 23 only
if the /4.4 charge is not taken from it.

`coldSwitchCostPct` is the Ultra (Max 20x) figure and the engine scales it x4 on
a Max 5x account (`UsageEntry.plan`, on #199). One live session's prefix costs
`tokens x 1.9 units / (2.08M units per 5h point x plan scale)` points
(alternation-replay.md, calibration; Max 20x scale 1, Max 5x 0.25), and the
setting prices a whole live fleet landing together, about 19 sessions
(settings.py): 100k tokens on 20x = 1.7 points, 400k = 6.9, and 4x those on 5x.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_swap.usage_store import UsageEntry
from tests.test_alternation_step import _harness, _iso_at, _reasons, _stamp, _tick, _track, _u

HAS_PLAN = "plan" in UsageEntry.__dataclass_fields__
COST = {"small": 2.0, "large": 7.0}  # the Ultra figure
PLAN = {"max5x": "Max 5x", "max20x": "Ultra"}
SEVEN = {"low": 20.0, "mid": 50.0, "tight": 70.0, "edge": 76.0, "near_cap": 90.0, "reset_from_full": 0.0}

# plan, partner plan, 5h shape, 7d level, partner, context, pinned model window -> the account taken
CASES = [
    ("max5x", "max5x", "running", "mid", "cold", "large", "none", 1),         # 2: 50 - 28 = 22 < 23; 3: 40 - 28 = 12
    ("max5x", "max5x", "removed", "mid", "cold", "large", "open", 2),         # 28 / 4.4 = 6.4 on the 7d: 43.6
    ("max5x", "max5x", "idle", "mid", "warm", "large", "none", 2),            # warm: 50
    ("max5x", "max5x", "removed", "tight", "cold", "large", "none", 2),       # 30 - 6.4 = 23.6: only the /4.4 admits it
    ("max20x", "max20x", "idle", "reset_from_full", "cold", "small", "none", 2),  # both viable, 2 has 100 against 40
    ("max5x", "max20x", "running", "mid", "cold", "large", "none", 3),        # 2: 50 - 28 = 22; 3: 40 - 7 = 33
    ("max20x", "max5x", "running", "mid", "cold", "large", "none", 2),        # 2: 50 - 7 = 43; 3: 40 - 28 = 12
    ("max20x", "max5x", "removed", "edge", "cold", "large", "none", 1),       # 2: 24 clears 23 uncharged, 24 - 7 / 4.4 = 22.4 does not; 3: 12
    ("max5x", "max20x", "running", "edge", "warm", "small", "full", 3),       # 2's pinned window is full; 3: 40 - 2
    ("max20x", "max5x", "removed", "near_cap", "warm", "small", "none", 3),   # 2: 10 points of room; 3: 40 - 8 = 32
    ("max20x", "max20x", "removed", "low", "cold", "large", "none", 2),       # 2: 80 - 7 / 4.4 = 78; 3: 33; 80 against 40
    ("max20x", "max20x", "running", "tight", "warm", "small", "open", 3),     # 2: 30 warm; 3: 38; 40 against 30
    ("max5x", "max5x", "idle", "low", "warm", "small", "open", 2),            # 2: 80 warm; 3: 32; 80 against 40
    ("max20x", "max20x", "idle", "edge", "warm", "small", "open", 3),         # 2: 24 warm; 3: 38; 40 against 24
    ("max20x", "max5x", "removed", "low", "cold", "large", "full", 1),        # 2 full; 3: 40 - 28 = 12
    ("max5x", "max20x", "idle", "near_cap", "cold", "large", "open", 3),      # 2: 10 - 28; 3: 40 - 7 = 33
    ("max5x", "max20x", "idle", "tight", "cold", "large", "full", 3),         # 2 full; 3: 33
    ("max20x", "max20x", "idle", "mid", "cold", "small", "full", 3),          # 2 full; 3: 40 - 2 = 38
    ("max20x", "max5x", "running", "near_cap", "warm", "large", "full", 1),   # 2 full; 3: 40 - 28 = 12
    ("max5x", "max5x", "removed", "reset_from_full", "warm", "large", "full", 1),  # 2 full; 3: 40 - 28 = 12
    ("max20x", "max5x", "idle", "reset_from_full", "warm", "large", "open", 2),  # 2: 90 warm; 3: 12
    ("max20x", "max5x", "running", "low", "warm", "small", "none", 2),        # 2: 80 warm; 3: 32; 80 against 40
]


def _rules(case, max5x_scale):
    """The account the rules above take on `case` when a Max 5x landing costs
    `max5x_scale` times the Ultra figure; the table says 4, a branch without
    `UsageEntry.plan` prices every landing at 1."""
    plan, partner_plan, five, seven, partner, context, pinned, _ = case
    scale = {"max5x": max5x_scale, "max20x": 1.0}
    room2 = min(
        80.0 if five == "running" else 100.0, 100.0 - SEVEN[seven],
        {"none": 100.0, "open": 90.0, "full": 0.0}[pinned],
    )
    cost2 = 0.0 if partner == "warm" else COST[context] * scale[plan] / (4.4 if five == "removed" else 1.0)
    cost3 = COST[context] * scale[partner_plan]
    taken = [(h, n) for h, n, cost in ((room2, 2, cost2), (40.0, 3, cost3)) if h - cost >= 23.0]
    return max(taken)[1] if taken else 1


def test_the_cases_are_the_generated_ones():
    cases = Path(__file__).parent / "pict" / "autoswitch_mixed_fleet.cases"
    rows = [line.split("\t") for line in cases.read_text().splitlines()[1:]]
    assert rows == [list(case[:7]) for case in CASES]


def test_the_table_is_the_rules():
    assert [_rules(case, 4.0) for case in CASES] == [case[7] for case in CASES]


@pytest.mark.parametrize("case", CASES, ids=lambda c: "-".join(c[:7]))
def test_the_step_on_a_mixed_fleet(temp_home, case):
    plan, partner_plan, five, seven, partner, context, pinned, taken = case
    if not HAS_PLAN and _rules(case, 1.0) != taken:
        pytest.skip("UsageEntry.plan is on #199: this row turns on the Max 5x scale, which this branch cannot read")
    h = _harness(
        temp_home,
        cold_switch_cost_pct=COST[context],
        model=None if pinned == "none" else "Fable",
    )
    b = _u({"running": 20.0, "idle": 0.0, "removed": None}[five], SEVEN[seven])
    if five == "running":
        b["five_hour"]["resets_at"] = _iso_at(h.clock.now + 7200.0)
    if seven != "reset_from_full":
        b["seven_day"]["resets_at"] = _iso_at(h.clock.now + 3 * 86400.0)
    if pinned != "none":
        b["scoped"] = [{"name": "Fable", "pct": 10.0 if pinned == "open" else 100.0}]
    c = _u(20.0, 60.0, reset7=h.clock.now + 3 * 86400.0)
    c["five_hour"]["resets_at"] = _iso_at(h.clock.now + 7200.0)
    plans = {"1": "Ultra", "2": PLAN[plan], "3": PLAN[partner_plan]} if HAS_PLAN else None
    _stamp(h, **{"1": 60.0, **({"2": 120.0} if partner == "warm" else {})})
    _track(h, anchor=0.0)
    outcome = _tick(h, {"1": _u(40.0), "2": b, "3": c}, plans=plans)
    assert h.active_number() == taken, (outcome, _reasons(h))
