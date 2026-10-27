"""Optional priority tiers, strict landing, and a complete multi-tick rotation."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from claude_swap.autoswitch import (
    NoSwitchEvent,
    SwitchEvent,
    TickOutcome,
    _headroom_by_account,
)
from claude_swap.models import AccountPolicy, normalize_account_priority
from claude_swap.settings import load_settings, set_setting
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.usage_store import UsageEntry
from tests.test_account_policy_cli import PolicyCliBase
from tests.test_autoswitch import EngineHarness, _entry_for, _iso_at, _usage7


def fleet(temp_home, **settings):
    h = EngineHarness(temp_home, **{
        "strategy": "priority-consume-first", "threshold": 90,
        "weekly_threshold": 99.9, "cooldown_seconds": 0, **settings,
    })
    for num in range(1, 5):
        h.seed(num, f"account-{num}@example.test")
    h.make_live("account-1@example.test", 1)
    h.switcher.set_account_priority("1", 100)
    h.switcher.set_account_priority("2", 100)
    return h


def usage(h, pcts=(10, 10, 10, 10), weekly=(60, 70, 50, 40)):
    # Fallback 4 resets soonest, then 3, then the preferred tier 1 and 2.
    return {
        str(num): _usage7(pcts[num - 1], weekly[num - 1],
                         _iso_at(h.clock.now + days * 86400))
        for num, days in enumerate((4, 5, 3, 2), 1)
    }


def test_complete_rotation_and_immediate_return_after_reset(temp_home):
    h = fleet(temp_home)
    values = usage(h)
    assert h.tick_with_usage(values) is TickOutcome.NO_ACTION
    values["1"]["five_hour"]["pct"] = 90
    assert h.tick_with_usage(values) is TickOutcome.SWITCHED
    assert h.active_number() == 2
    values["2"]["five_hour"]["pct"] = 90
    assert h.tick_with_usage(values) is TickOutcome.SWITCHED
    assert h.active_number() == 4  # Earliest weekly reset in the fallback tier.
    values["4"]["five_hour"]["pct"] = 90
    assert h.tick_with_usage(values) is TickOutcome.SWITCHED
    assert h.active_number() == 3
    # A primary recovers while fallback 3 still has plenty of room.
    values["2"]["five_hour"]["pct"] = 0
    assert h.tick_with_usage(values) is TickOutcome.SWITCHED
    assert h.active_number() == 2
    for _ in range(4):
        h.clock.advance(60)
        assert h.tick_with_usage(values) is TickOutcome.NO_ACTION
    assert h.active_number() == 2


def test_return_to_the_account_just_left_is_not_barred(temp_home):
    h = fleet(temp_home)
    values = usage(h, pcts=(90, 90, 30, 90))
    assert h.tick_with_usage(values) is TickOutcome.SWITCHED
    assert h.active_number() == 3
    values["1"]["five_hour"]["pct"] = 0
    assert h.tick_with_usage(values) is TickOutcome.SWITCHED
    assert h.active_number() == 1


@pytest.mark.parametrize("active_pct", [90, 100])
def test_no_above_threshold_escape_even_at_the_hard_limit(temp_home, active_pct):
    h = fleet(temp_home)
    values = usage(h, pcts=(active_pct, 90, 95, 99))
    assert h.tick_with_usage(values) is TickOutcome.BLOCKED
    assert h.active_number() == 1
    assert not any(isinstance(e, SwitchEvent) for e in h.events)


def test_failover_also_obeys_the_cutoffs(temp_home):
    h = fleet(temp_home, unhealthy_ticks=1)
    values = usage(h, pcts=(0, 90, 95, 99))
    values["1"] = None
    assert h.tick_with_usage(values) is TickOutcome.BLOCKED
    values["3"]["five_hour"]["pct"] = 89.9
    assert h.tick_with_usage(values) is TickOutcome.SWITCHED
    assert h.active_number() == 3


def test_multiple_priority_tiers_and_near_line_target(temp_home):
    h = fleet(temp_home)
    h.switcher.set_account_priority("2", 50)
    values = usage(h, pcts=(90, 89.9, 0, 0))
    assert h.tick_with_usage(values) is TickOutcome.SWITCHED
    # No percentage hysteresis can overrule the highest usable tier.
    assert h.active_number() == 2


def test_weekly_exhausted_primary_and_fallback_are_skipped(temp_home):
    h = fleet(temp_home)
    values = usage(h, pcts=(90, 0, 0, 0), weekly=(60, 100, 99.8, 100))
    assert h.tick_with_usage(values) is TickOutcome.SWITCHED
    assert h.active_number() == 3


@pytest.mark.parametrize("weekly_line,pct", [(95, 95), (99.9, 99.9), (99.9, 100)])
def test_weekly_line_alone_switches_with_zero_five_hour_usage(temp_home, weekly_line, pct):
    h = fleet(temp_home, weekly_threshold=weekly_line)
    values = usage(h, pcts=(0, 0, 0, 0), weekly=(pct, 70, 50, 40))
    assert h.tick_with_usage(values) is TickOutcome.SWITCHED
    assert h.active_number() == 2


def test_custom_weekly_and_per_account_thresholds_are_respected(temp_home):
    h = fleet(temp_home, weekly_threshold=95)
    h.switcher.set_account_threshold("2", 80)
    values = usage(h, pcts=(90, 80, 0, 0), weekly=(50, 50, 94, 95))
    assert h.tick_with_usage(values) is TickOutcome.SWITCHED
    assert h.active_number() == 3


def test_model_weekly_exhaustion_blocks_high_priority(temp_home):
    h = fleet(temp_home, model="Fable")
    values = usage(h, pcts=(90, 0, 0, 0))
    values["2"]["scoped"] = [{"name": "Fable", "pct": 100}]
    assert h.tick_with_usage(values) is TickOutcome.SWITCHED
    assert h.active_number() == 4


@pytest.mark.parametrize("excluded", ["disabled", "quarantined", "unknown"])
def test_priority_does_not_bypass_account_eligibility(temp_home, excluded):
    h = fleet(temp_home)
    values = usage(h, pcts=(90, 0, 0, 0))
    if excluded == "disabled":
        h.switcher.set_account_disabled("2", True)
    elif excluded == "quarantined":
        h.engine._quarantine("2", "account-2@example.test", "invalid_grant")
    else:
        values["2"] = None
    assert h.tick_with_usage(values) is TickOutcome.SWITCHED
    assert h.active_number() == 4


@pytest.mark.parametrize("known_reset", [True, False])
def test_equal_priority_and_reset_keep_the_active_account(temp_home, known_reset):
    h = fleet(temp_home)
    values = usage(h)
    for num in ("1", "2"):
        values[num]["seven_day"] = {"pct": 30}
        if known_reset:
            values[num]["seven_day"]["resets_at"] = _iso_at(h.clock.now + 86400)
    values["1"]["five_hour"]["pct"] = 89
    values["2"]["five_hour"]["pct"] = 0
    for _ in range(3):
        assert h.tick_with_usage(values) is TickOutcome.NO_ACTION
        h.clock.advance(60)
    assert h.active_number() == 1


def test_known_reset_wins_unknown_only_within_the_same_tier(temp_home):
    h = fleet(temp_home)
    values = usage(h)
    values["1"]["seven_day"].pop("resets_at")
    assert h.tick_with_usage(values) is TickOutcome.SWITCHED
    assert h.active_number() == 2
    # Unknown reset on a higher priority account still beats known fallback resets.
    h.switcher.set_account_priority("1", 200)
    assert h.tick_with_usage(values) is TickOutcome.SWITCHED
    assert h.active_number() == 1


def test_cooldown_then_recovery_and_hard_limit_bypass(temp_home):
    h = fleet(temp_home, cooldown_seconds=300)
    values = usage(h, pcts=(90, 90, 20, 20))
    assert h.tick_with_usage(values) is TickOutcome.SWITCHED
    assert h.active_number() == 4
    values["1"]["five_hour"]["pct"] = 0
    assert h.tick_with_usage(values) is TickOutcome.NO_ACTION
    assert h.active_number() == 4
    h.clock.advance(301)
    assert h.tick_with_usage(values) is TickOutcome.SWITCHED
    assert h.active_number() == 1
    values["1"]["five_hour"]["pct"] = 100
    assert h.tick_with_usage(values) is TickOutcome.SWITCHED
    assert h.active_number() == 4


def test_pre_switch_refresh_can_veto_the_provisional_priority_target(temp_home):
    h = fleet(temp_home)
    h.switcher.set_account_priority("2", 200)
    initial = usage(h)
    fresh = usage(h, pcts=(10, 90, 10, 10))
    initial_entries = {n: _entry_for(v, h.clock.now) for n, v in initial.items()}
    fresh_entries = {n: _entry_for(v, h.clock.now) for n, v in fresh.items()}
    with patch.object(h.engine, "_collect_scheduled_usage", return_value=(
        initial_entries, initial, {n: 100 - max(v["five_hour"]["pct"], v["seven_day"]["pct"])
                                   for n, v in initial.items()},
    )), patch.object(h.switcher, "usage_entries_by_account", return_value=fresh_entries):
        assert h.engine.tick() is TickOutcome.NO_ACTION
    assert h.active_number() == 1


@pytest.mark.parametrize("active_pct,expected", [(10, TickOutcome.NO_ACTION), (100, TickOutcome.BLOCKED)])
def test_stale_priority_target_never_activates(temp_home, active_pct, expected):
    h = fleet(temp_home)
    h.switcher.set_account_priority("2", 200)
    values = usage(h, pcts=(active_pct, 10, 90, 90))
    entries = {n: _entry_for(v, h.clock.now) for n, v in values.items()}
    entries["2"] = UsageEntry(last_good=values["2"], fetched_at=h.clock.now - 600,
                              age_s=600, trust_extended=True)
    assert h.tick_with_entries(entries) is expected
    assert h.active_number() == 1
    assert any(isinstance(e, NoSwitchEvent) and e.reason == "stale-usage" for e in h.events)


@pytest.mark.parametrize("departure", ["five-hour", "hard-limit", "weekly", "override", "model", "failover"])
@pytest.mark.parametrize("stale_targets,expected_target", [(("2",), 4), (("2", "4"), 3)])
def test_required_priority_move_skips_stale_targets(
    temp_home, departure, stale_targets, expected_target,
):
    h = fleet(temp_home, unhealthy_ticks=1, model="Fable")
    h.switcher.set_account_priority("2", 200)
    values = usage(h, pcts=(0, 10, 30, 40))
    if departure == "five-hour":
        values["1"]["five_hour"]["pct"] = 90
    elif departure == "hard-limit":
        values["1"]["five_hour"]["pct"] = 100
    elif departure == "weekly":
        values["1"]["seven_day"]["pct"] = 99.9
    elif departure == "override":
        h.switcher.set_account_threshold("1", 80)
        values["1"]["five_hour"]["pct"] = 80
    elif departure == "model":
        values["1"]["scoped"] = [{"name": "Fable", "pct": 99.9}]
    else:
        values["1"] = None
    entries = {n: _entry_for(v, h.clock.now) for n, v in values.items()}
    for num in stale_targets:
        entries[num] = UsageEntry(
            last_good=values[num], fetched_at=h.clock.now - 600,
            age_s=600, trust_extended=True,
        )
    with patch.object(h.engine, "_freshen_target", return_value="ok") as freshen:
        assert h.tick_with_entries(entries) is TickOutcome.SWITCHED
    assert h.active_number() == expected_target
    freshen.assert_called_once_with(str(expected_target), f"account-{expected_target}@example.test")
    switch = next(e for e in h.events if isinstance(e, SwitchEvent))
    assert switch.trigger == (
        "failover" if departure == "failover"
        else "at-limit" if departure == "hard-limit" else "proactive"
    )


def test_opportunistic_priority_move_holds_for_stale_top_target(temp_home):
    h = fleet(temp_home)
    h.switcher.set_account_priority("2", 300)
    h.switcher.set_account_priority("3", 200)
    values = usage(h)
    entries = {n: _entry_for(v, h.clock.now) for n, v in values.items()}
    entries["2"] = UsageEntry(
        last_good=values["2"], fetched_at=h.clock.now - 600,
        age_s=600, trust_extended=True,
    )
    with patch.object(h.engine, "_freshen_target") as freshen:
        assert h.tick_with_entries(entries) is TickOutcome.NO_ACTION
    assert h.active_number() == 1
    freshen.assert_not_called()
    assert not any(isinstance(e, SwitchEvent) for e in h.events)
    assert any(isinstance(e, NoSwitchEvent) and e.reason == "stale-usage" for e in h.events)


@pytest.mark.parametrize("initial_pct,fresh_pct,expected", [
    (10, 90, TickOutcome.SWITCHED),
    (90, 10, TickOutcome.NO_ACTION),
    (None, 10, TickOutcome.NO_ACTION),
])
def test_stale_target_skip_uses_refreshed_active_eligibility(
    temp_home, initial_pct, fresh_pct, expected,
):
    h = fleet(temp_home, unhealthy_ticks=1)
    h.switcher.set_account_priority("2", 200)
    initial = usage(h)
    if initial_pct is None:
        initial["1"] = None
    else:
        initial["1"]["five_hour"]["pct"] = initial_pct
    initial_entries = {n: _entry_for(v, h.clock.now) for n, v in initial.items()}
    fresh = usage(h, pcts=(fresh_pct, 10, 10, 10))
    fresh_entries = {n: _entry_for(v, h.clock.now) for n, v in fresh.items()}
    fresh_entries["2"] = UsageEntry(
        last_good=fresh["2"], fetched_at=h.clock.now - 600,
        age_s=600, trust_extended=True,
    )
    with patch.object(h.engine, "_collect_scheduled_usage", return_value=(
        initial_entries, initial, _headroom_by_account(initial, h.engine._models),
    )), patch.object(h.switcher, "usage_entries_by_account", return_value=fresh_entries):
        assert h.engine.tick() is expected
    assert h.active_number() == (4 if expected is TickOutcome.SWITCHED else 1)


def test_required_priority_move_skips_stale_targets_in_dry_run(temp_home):
    h = fleet(temp_home)
    h.engine.dry_run = True
    h.switcher.set_account_priority("2", 200)
    values = usage(h, pcts=(100, 10, 30, 40))
    entries = {n: _entry_for(v, h.clock.now) for n, v in values.items()}
    for num in ("2", "4"):
        entries[num] = UsageEntry(
            last_good=values[num], fetched_at=h.clock.now - 600,
            age_s=600, trust_extended=True,
        )
    before = h.switcher.sequence_file.read_bytes()
    with patch.object(h.engine, "_freshen_target") as freshen:
        assert h.tick_with_entries(entries) is TickOutcome.SWITCHED
    freshen.assert_not_called()
    assert h.switcher.sequence_file.read_bytes() == before
    assert not h.engine.state_path.exists()
    switch = next(e for e in h.events if isinstance(e, SwitchEvent))
    assert switch.dry_run
    assert switch.to_ref["number"] == 3


def test_dry_run_reports_without_changing_login_or_state(temp_home):
    h = fleet(temp_home)
    h.engine.dry_run = True
    before = h.switcher.sequence_file.read_bytes()
    with patch.object(h.engine, "_freshen_target", side_effect=AssertionError("must not refresh")):
        assert h.tick_with_usage(usage(h, pcts=(90, 90, 10, 10))) is TickOutcome.SWITCHED
    assert h.switcher.sequence_file.read_bytes() == before
    assert not h.engine.state_path.exists()
    assert next(e for e in h.events if isinstance(e, SwitchEvent)).dry_run


@pytest.mark.parametrize("strategy,target", [("best", 3), ("consume-first", 4)])
def test_existing_strategies_ignore_configured_priorities(temp_home, strategy, target):
    h = fleet(temp_home, strategy=strategy)
    values = usage(h, pcts=(95, 70, 0, 20), weekly=(50, 50, 0, 30))
    assert h.tick_with_usage(values) is TickOutcome.SWITCHED
    assert h.active_number() == target


class TestPriorityCli(PolicyCliBase):
    def test_set_by_email_list_unset_and_default_strategy(self, temp_home, capsys):
        s = self._fleet(temp_home)
        self._main(["priority", "account-2@example.test", "100"])
        assert s.account_policies()["2"].priority == 100
        self._main(["priority"])
        out = capsys.readouterr().out
        assert "account-2@example.test" in out and "100" in out
        assert load_settings(s.backup_dir).strategy == "best"
        self._main(["priority", "2", "--unset"])
        assert "priority" not in self._record(s, "2")
        before = s.sequence_file.read_bytes()
        self._main(["priority", "2", "0"])
        assert s.sequence_file.read_bytes() == before

    @pytest.mark.parametrize("value", ["-1", "1001", "1.5", "bad", "NaN"])
    def test_invalid_cli_value_writes_nothing(self, temp_home, value):
        s = self._fleet(temp_home)
        before = s.sequence_file.read_bytes()
        with pytest.raises(SystemExit) as exc:
            self._main(["priority", "2", value])
        assert exc.value.code != 0
        assert s.sequence_file.read_bytes() == before

    def test_alias_and_unknown_identifier(self, temp_home):
        s = self._fleet(temp_home)
        self._alias(s, "2", "work")
        self._main(["priority", "work", "20"])
        assert s.account_policies()["2"].priority == 20
        before = s.sequence_file.read_bytes()
        with pytest.raises(SystemExit):
            self._main(["priority", "missing@example.test", "20"])
        assert s.sequence_file.read_bytes() == before


@pytest.mark.parametrize("invalid", [True, 1.0, -1, 1001, None, [], {}, "bad", "9" * 5000])
def test_priority_validation_and_corrupt_read_fallback(invalid):
    with pytest.raises(ValueError):
        normalize_account_priority(invalid)
    policy = ClaudeAccountSwitcher._policy_from_data({"accounts": {"1": {
        "priority": invalid, "threshold": 85,
    }}}, "1")
    assert policy == AccountPolicy(threshold=85, priority=0)


def test_priority_strategy_uses_existing_configurable_cutoffs(tmp_path):
    set_setting(tmp_path, "autoswitch.strategy", "priority-consume-first")
    set_setting(tmp_path, "autoswitch.threshold", "85")
    set_setting(tmp_path, "autoswitch.weeklyThreshold", "99.9")
    settings = load_settings(tmp_path)
    assert settings.strategy == "priority-consume-first"
    assert settings.threshold == 85
    assert settings.weekly_threshold == 99.9

