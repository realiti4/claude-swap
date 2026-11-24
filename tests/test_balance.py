"""The balance strategy: scoring (balance.py) and the engine's use of it.

Engine tests run in dry-run mode on the ``temp_home`` fixture, through the
same ``EngineHarness`` as test_autoswitch.py.
"""

import pytest

from claude_swap import balance
from claude_swap.autoswitch import NoSwitchEvent, SwitchEvent, TickOutcome
from tests.test_autoswitch import EngineHarness, _iso_at

HOUR = 3600.0


# -- settings parsing -------------------------------------------------------


class TestParseWeightOverrides:
    def test_pairs(self):
        assert balance.parse_weight_overrides("1=5, 2=1") == {"1": 5.0, "2": 1.0}

    @pytest.mark.parametrize("value", [None, "", "  "])
    def test_empty(self, value):
        assert balance.parse_weight_overrides(value) == {}

    def test_malformed_parts_are_skipped_not_fatal(self):
        # No "=", non-numeric slot, non-numeric weight, zero, over the cap:
        # each is dropped, and the one good pair survives.
        value = "junk, a=3, 2=x, 3=0, 4=5000, 5=2.5"
        assert balance.parse_weight_overrides(value) == {"5": 2.5}


class TestWeightForPlan:
    @pytest.mark.parametrize(
        "tier, weight",
        [
            ("default_claude_ai", 1.0),
            ("default_claude_max_5x", 5.0),
            ("default_claude_max_20x", 20.0),
            ("something_new", balance.DEFAULT_WEIGHT),
        ],
    )
    def test_tiers(self, tier, weight):
        assert balance.weight_for_plan({"plan": {"tier": tier}}) == weight

    @pytest.mark.parametrize("record", [None, {}, {"plan": None}, {"plan": {}}, "x"])
    def test_missing_or_malformed_record_is_default(self, record):
        assert balance.weight_for_plan(record) == balance.DEFAULT_WEIGHT


# -- scoring ----------------------------------------------------------------


def _usage(now, pct, hours_left=None, five_hour_pct=None):
    seven_day = {"pct": pct}
    if hours_left is not None:
        seven_day["resets_at"] = _iso_at(now + hours_left * HOUR)
    usage = {"seven_day": seven_day}
    if five_hour_pct is not None:
        usage["five_hour"] = {"pct": five_hour_pct}
    return usage


class TestSpareRate:
    NOW = 1_800_000_000.0

    def test_formula(self):
        # 5 x (60% free) / 72 h, as in the module docstring's example.
        rate = balance.spare_rate(_usage(self.NOW, 40, hours_left=72), (), 5.0, self.NOW)
        assert rate.score == pytest.approx(5 * 0.60 / 72)
        assert rate.detail == "7d 40%/72h"

    def test_bigger_plan_outranks_less_used_smaller_plan(self):
        max5 = balance.spare_rate(_usage(self.NOW, 40, 72), (), 5.0, self.NOW)
        pro = balance.spare_rate(_usage(self.NOW, 10, 72), (), 1.0, self.NOW)
        assert max5.score > pro.score

    def test_five_hour_window_is_ignored(self):
        with_5h = balance.spare_rate(
            _usage(self.NOW, 40, 72, five_hour_pct=99), (), 1.0, self.NOW
        )
        without = balance.spare_rate(_usage(self.NOW, 40, 72), (), 1.0, self.NOW)
        assert with_5h.score == without.score

    def test_unknown_reset_assumes_a_full_week(self):
        rate = balance.spare_rate(_usage(self.NOW, 50), (), 1.0, self.NOW)
        assert rate.score == pytest.approx(0.5 / balance.HOURS_IN_A_WEEK)

    def test_imminent_reset_is_floored(self):
        # One minute to reset must not divide by ~0.
        rate = balance.spare_rate(_usage(self.NOW, 50, hours_left=1 / 60), (), 1.0, self.NOW)
        assert rate.score == pytest.approx(0.5 / balance.MIN_HOURS_TO_RESET)

    def test_over_100_percent_scores_zero_not_negative(self):
        rate = balance.spare_rate(_usage(self.NOW, 120, 24), (), 1.0, self.NOW)
        assert rate.score == 0.0

    def test_tightest_weekly_window_wins(self):
        usage = _usage(self.NOW, 10, 72)
        usage["scoped"] = [
            {"name": "Fable", "pct": 90, "resets_at": _iso_at(self.NOW + 72 * HOUR)}
        ]
        rate = balance.spare_rate(usage, ("Fable",), 1.0, self.NOW)
        assert rate.score == pytest.approx(0.10 / 72)
        assert rate.detail.startswith("Fable")

    @pytest.mark.parametrize("usage", [None, "token-expired", {}])
    def test_unreadable_usage_has_no_score(self, usage):
        assert balance.spare_rate(usage, (), 1.0, self.NOW) is None


class TestMarginAndDisplay:
    def test_required_score(self):
        assert balance.required_score(40.0, 25.0) == pytest.approx(50.0)

    def test_fmt(self):
        assert balance.fmt(None) == "-"
        assert balance.fmt(0.0417) == "41.7"


# -- the engine -------------------------------------------------------------


def _harness(temp_home, **settings):
    settings.setdefault("strategy", "balance")
    h = EngineHarness(temp_home, **settings)
    for number in (1, 2, 3):
        h.seed(number, f"account{number}@example.com")
    h.make_live("account1@example.com", 1)
    h.engine = h._make_engine(dry_run=True)
    return h


def _weekly(h, pct, hours_left):
    return {
        "five_hour": {"pct": 0},
        "seven_day": {"pct": pct, "resets_at": _iso_at(h.clock.now + hours_left * HOUR)},
    }


def _targets(h):
    return [e.to_ref["number"] for e in h.events if isinstance(e, SwitchEvent)]


def test_below_the_line_moves_to_a_clearly_better_paced_account(temp_home):
    h = _harness(temp_home)
    outcome = h.tick_with_usage({
        "1": _weekly(h, 60, 100),  # 0.40/100 = 0.0040
        "2": _weekly(h, 10, 100),  # 0.90/100 = 0.0090: more than 25% better
        "3": _weekly(h, 50, 100),  # 0.0050
    })
    assert outcome is TickOutcome.SWITCHED
    assert _targets(h) == [2]


def test_stays_when_no_account_beats_the_margin(temp_home):
    h = _harness(temp_home, balance_margin=25)
    outcome = h.tick_with_usage({
        "1": _weekly(h, 50, 100),  # 0.0050
        "2": _weekly(h, 40, 100),  # 0.0060: 20% better, under the 25% margin
        "3": _weekly(h, 45, 100),
    })
    assert outcome is TickOutcome.NO_ACTION
    assert not _targets(h)
    event = h.events[-1]
    assert isinstance(event, NoSwitchEvent)
    assert event.reason == "balanced"
    assert "need +25% to move" in event.detail


def test_never_lands_on_an_account_over_its_line(temp_home):
    h = _harness(temp_home, threshold=90)
    outcome = h.tick_with_usage({
        "1": _weekly(h, 60, 100),
        # Huge spare rate (resets in 1 h) but over the 90 line: not a target.
        "2": _weekly(h, 92, 1),
        "3": _weekly(h, 20, 100),
    })
    assert outcome is TickOutcome.SWITCHED
    assert _targets(h) == [3]


def test_plan_weight_override_changes_the_pick(temp_home):
    # Same usage on 2 and 3; weighting slot 3 as Max 5x makes it the pick.
    h = _harness(temp_home, balance_weights="3=5")
    outcome = h.tick_with_usage({
        "1": _weekly(h, 60, 100),
        "2": _weekly(h, 30, 100),
        "3": _weekly(h, 30, 100),
    })
    assert outcome is TickOutcome.SWITCHED
    assert _targets(h) == [3]
