"""MEU-FBD-01 - the opt-in failback delay (``autoswitch.failbackDelaySeconds``).

Covers AC-6..AC-14 and AC-17..AC-19.

The feature answers ivan-andreyev on #318: keep ``cooldownSeconds`` governing
ordinary rotation, and give the standby -> primary handback its own timer, the
way keepalived separates ``preempt_delay`` from the rest of its timers. The
timer is anchored on the moment the failback becomes **actionable** (a viable
primary exists), not on the last switch, so every value in the range measures
the recovered primary's steadiness rather than the reserve's dwell.

Five properties are easy to get wrong and each has its own class here:

- **Unset is today.** No new reason string, no new state key, cooldown still
  gates failback. `TestUnsetIsUnchanged`.
- **Disarm is total.** Round 1 of plan review lost this: the tick-entry gate
  and the `stale-usage` return both bypass `_failback_hold()`, so clearing the
  anchor only there leaves it stale across an outage. `TestTheAnchorDisarms`.
- **`--dry-run` writes nothing, anywhere in the tick** - including the wrapper
  disarm. `TestDryRunReadsButNeverWrites`.
- **The locked recheck is authoritative.** Two engines must make one
  serialized decision even when the delay has elapsed for both.
  `TestTheLockedRecheckIsAuthoritative`.
- **A lost disarm fails closed.** The clear is a write, and writes fail; a
  stale anchor that could not be removed must never be measured from.
  `TestALostDisarmFailsClosed`.

Every oracle drives a **real tick** (`engine.tick()` -> `_tick_inner` ->
`_rank` -> `_perform`) and asserts on the emitted reason string and the
`TickOutcome`, never merely that no switch occurred - three outcomes all report
"no switch". Synthetic fixtures only; nothing here reads a real account store.
"""

from __future__ import annotations

import math
import pathlib

import pytest

from unittest.mock import patch

from claude_swap.autoswitch import NoSwitchEvent, SwitchEvent, TickOutcome
from claude_swap.settings import AutoSwitchSettings

from tests.test_autoswitch import EngineHarness, _entry_for
from tests.test_autoswitch_standby_accounts import (
    _details,
    _fleet,
    _reasons,
    _stale,
    _standby,
    _switches,
    _tick,
    _triggers,
    _u,
)

COOLDOWN = AutoSwitchSettings().cooldown_seconds  # 300.0
ANCHOR = "failbackReadyAt"
DELAY_REASON = "failback-delay"


def _reserve_active(temp_home, **settings_kwargs) -> EngineHarness:
    """Account 1 is a standby and is the active account; account 2 is a primary.

    This is the state the engine reaches after every primary was exhausted and
    the reserve was promoted - the only state in which `failback` is reachable.
    Account 2 carries its own 85% line so the landing gate is addressable
    independently of the global 90.
    """
    h = _fleet(temp_home, n=2, **settings_kwargs)
    _standby(h, 1)
    h.switcher.set_account_threshold("2", 85.0)
    h.engine = h._make_engine()
    return h


def _switched_at(h, seconds_ago: float) -> None:
    """Record a switch `seconds_ago` - the reserve was promoted that long ago."""
    h.engine._mutate_state(lambda s: s.update(lastSwitchAt=h.clock() - seconds_ago))


def _set_anchor(h, value) -> None:
    h.engine._mutate_state(lambda s: s.update(**{ANCHOR: value}))


def _anchor(h):
    return h.state().get(ANCHOR)


def _healthy(h) -> dict:
    """The reserve is quiet at 20%; the primary has recovered to 70%."""
    return {"1": _u(20.0), "2": _u(70.0)}


def _exhausted(h) -> dict:
    """The primary is back over its own 85% line - nowhere to hand back to."""
    return {"1": _u(20.0), "2": _u(90.0)}


class TestUnsetIsUnchanged:
    """AC-6, AC-7, AC-14 - a fleet that does not opt in sees nothing new.

    This is the same promise the three stacked PRs make for `threshold`,
    `standby` and `order`: the feature is invisible until it is configured,
    down to the bytes of the state file.
    """

    def test_cooldown_still_suppresses_failback_when_unset(self, temp_home):
        h = _reserve_active(temp_home)
        _switched_at(h, 1)
        outcome, _ = _tick(h, [_healthy(h)])
        assert outcome is TickOutcome.NO_ACTION
        assert _reasons(h) == ["cooldown"]
        assert _switches(h) == []

    def test_outside_cooldown_it_still_hands_back_when_unset(self, temp_home):
        h = _reserve_active(temp_home)
        _switched_at(h, COOLDOWN + 1)
        outcome, _ = _tick(h, [_healthy(h)])
        assert outcome is TickOutcome.SWITCHED
        assert _triggers(h) == ["failback"]

    def test_no_anchor_key_is_ever_written_when_unset(self, temp_home):
        """AC-7 - the state file of a fleet that never opts in is identical to
        a pre-feature one. Asserted on the key set, not on behaviour."""
        h = _reserve_active(temp_home)
        _switched_at(h, 1)
        _tick(h, [_healthy(h)])
        assert ANCHOR not in h.state()
        _tick(h, [_exhausted(h)])
        assert ANCHOR not in h.state()

    def test_the_new_reason_string_is_never_emitted_when_unset(self, temp_home):
        """AC-14 - #318 ships the failback hold deliberately mute, and that
        contract survives because the new observable is gated on the opt-in."""
        h = _reserve_active(temp_home)
        _switched_at(h, 1)
        _tick(h, [_healthy(h)])
        _tick(h, [_exhausted(h)])
        assert DELAY_REASON not in _reasons(h)


class TestZeroHandsBackOnTheFirstEligiblePoll:
    """AC-8 - ivan's policy: `failbackDelaySeconds 0`, `cooldownSeconds 300`.

    The second test is the oracle that matters. A patch that simply exempts
    the whole cooldown - rather than giving failback its own timer - passes the
    first test and fails the second, because ordinary rotation must keep its
    floor.
    """

    def test_it_switches_in_one_tick_inside_the_cooldown(self, temp_home):
        h = _reserve_active(temp_home, failback_delay_seconds=0.0)
        _switched_at(h, 10)
        outcome, _ = _tick(h, [_healthy(h)])
        assert outcome is TickOutcome.SWITCHED
        assert _triggers(h) == ["failback"]
        assert h.active_number() == 2

    def test_ordinary_rotation_still_obeys_cooldown(self, temp_home):
        """Same settings, same clock - but no reserve, so the trigger is
        `proactive` and the 300-second floor still applies."""
        h = _fleet(temp_home, n=2, failback_delay_seconds=0.0)
        _switched_at(h, 10)
        outcome, _ = _tick(h, [{"1": _u(95.0), "2": _u(10.0)}])
        assert outcome is TickOutcome.NO_ACTION
        assert _reasons(h) == ["cooldown"]
        assert _switches(h) == []


class TestAPositiveDelayHoldsThenFires:
    """AC-9 - the timer measures the recovered primary's steadiness."""

    def test_the_first_actionable_tick_holds_with_its_own_reason(self, temp_home):
        h = _reserve_active(temp_home, failback_delay_seconds=60.0)
        _switched_at(h, 10)
        outcome, _ = _tick(h, [_healthy(h)])
        assert outcome is TickOutcome.NO_ACTION
        assert _reasons(h) == [DELAY_REASON]
        assert _switches(h) == []
        assert _anchor(h) == h.clock()

    def test_it_still_holds_one_second_short(self, temp_home):
        h = _reserve_active(temp_home, failback_delay_seconds=60.0)
        _switched_at(h, 10)
        _tick(h, [_healthy(h)])
        armed = _anchor(h)
        h.clock.advance(59)
        h.events.clear()
        outcome, _ = _tick(h, [_healthy(h)])
        assert outcome is TickOutcome.NO_ACTION
        assert _reasons(h) == [DELAY_REASON]
        assert _anchor(h) == armed, "a held tick must not re-arm the timer"

    def test_it_fires_once_the_delay_has_elapsed(self, temp_home):
        h = _reserve_active(temp_home, failback_delay_seconds=60.0)
        _switched_at(h, 10)
        _tick(h, [_healthy(h)])
        h.clock.advance(60)
        h.events.clear()
        outcome, _ = _tick(h, [_healthy(h)])
        assert outcome is TickOutcome.SWITCHED
        assert _triggers(h) == ["failback"]
        assert h.active_number() == 2


class TestTheAnchorDisarms:
    """AC-10 - steadiness, not dwell: losing the primary restarts the clock.

    (b) is the discriminator. Plan review round 1 prescribed clearing the
    anchor inside `_failback_hold()`; that passes (a) and fails (b), because
    the `stale-usage` return never reaches the hold closure. Disarm therefore
    lives at `tick()`'s single exit, where every early return passes.
    """

    def test_a_exhausted_primaries_clear_it_through_the_hold_path(self, temp_home):
        h = _reserve_active(temp_home, failback_delay_seconds=60.0)
        _switched_at(h, 10)
        _tick(h, [_healthy(h)])
        assert _anchor(h) is not None
        h.events.clear()
        outcome, _ = _tick(h, [_exhausted(h)])
        assert outcome is TickOutcome.NO_ACTION
        assert _anchor(h) is None

    def test_b_a_stale_usage_return_clears_it_too(self, temp_home):
        """The path that bypasses `_failback_hold()` entirely."""
        h = _reserve_active(temp_home, failback_delay_seconds=60.0)
        _switched_at(h, 10)
        _tick(h, [_healthy(h)])
        assert _anchor(h) is not None
        h.events.clear()
        now = h.clock.now
        phase1 = _healthy(h)
        phase2 = {"1": _u(20.0), "2": _stale(70.0, now)}
        outcome, _ = _tick(h, [phase1, phase1, phase2])
        assert outcome is TickOutcome.NO_ACTION
        assert "stale-usage" in _reasons(h)
        assert _anchor(h) is None

    def test_c_the_active_no_longer_being_a_standby_clears_it(self, temp_home):
        h = _reserve_active(temp_home, failback_delay_seconds=60.0)
        _switched_at(h, 10)
        _tick(h, [_healthy(h)])
        assert _anchor(h) is not None
        h.switcher.set_account_standby("1", False)
        h.engine = h._make_engine()
        h.events.clear()
        _tick(h, [{"1": _u(20.0), "2": _u(70.0)}])
        assert _anchor(h) is None

    def test_a_later_recovery_serves_a_fresh_delay(self, temp_home):
        """The whole point: an outage in the middle does not bank time."""
        h = _reserve_active(temp_home, failback_delay_seconds=60.0)
        _switched_at(h, 10)
        _tick(h, [_healthy(h)])
        h.clock.advance(10)
        _tick(h, [_exhausted(h)])          # primary lost -> anchor cleared
        h.clock.advance(3_600)
        h.events.clear()
        outcome, _ = _tick(h, [_healthy(h)])   # recovered, but unproven
        assert outcome is TickOutcome.NO_ACTION
        assert _reasons(h) == [DELAY_REASON]
        h.clock.advance(60)
        h.events.clear()
        outcome, _ = _tick(h, [_healthy(h)])
        assert outcome is TickOutcome.SWITCHED
        assert _triggers(h) == ["failback"]


class TestASwitchRemovesTheAnchor:
    """AC-11 - removed, never stored as `null`.

    One canonical rule, because two spellings of "cleared" is how a reader of
    the state file - and the next implementer - end up disagreeing.
    """

    def test_the_key_is_absent_after_a_handback(self, temp_home):
        h = _reserve_active(temp_home, failback_delay_seconds=60.0)
        _switched_at(h, 10)
        _tick(h, [_healthy(h)])
        h.clock.advance(60)
        outcome, _ = _tick(h, [_healthy(h)])
        assert outcome is TickOutcome.SWITCHED
        assert ANCHOR not in h.state()
        assert h.state()["lastSwitchAt"] == h.clock()


class TestAMalformedOrStaleAnchorIsIgnored:
    """AC-12 - a hand-edited state file degrades, it never crashes the tick.

    `autoswitch_state.json` is documented in the README as a file users may
    delete to reset, so it is hand-editable by contract. `state.get(ANCHOR) or
    now` raises `TypeError` on the string case and turns the tick into `ERROR`.
    """

    @pytest.mark.parametrize(
        "bad", ["bad", None, True, [], {}, float("nan"), float("inf")]
    )
    def test_a_non_finite_value_reads_as_unarmed(self, temp_home, bad):
        h = _reserve_active(temp_home, failback_delay_seconds=60.0)
        _switched_at(h, 10)
        _set_anchor(h, bad)
        outcome, _ = _tick(h, [_healthy(h)])
        assert outcome is TickOutcome.NO_ACTION, "must not be ERROR"
        assert _reasons(h) == [DELAY_REASON]
        assert _anchor(h) == h.clock(), "it re-arms from now"

    def test_an_anchor_older_than_the_last_switch_is_ignored(self, temp_home):
        """A leftover from before the move onto the reserve would hand back
        instantly, skipping the window the user asked for."""
        h = _reserve_active(temp_home, failback_delay_seconds=60.0)
        _switched_at(h, 10)
        _set_anchor(h, h.clock() - 3_600)
        outcome, _ = _tick(h, [_healthy(h)])
        assert outcome is TickOutcome.NO_ACTION
        assert _reasons(h) == [DELAY_REASON]
        assert _anchor(h) == h.clock()

    def test_a_valid_anchor_after_the_last_switch_is_honoured(self, temp_home):
        """The identity half - the guard rejects stale values, not all of them."""
        h = _reserve_active(temp_home, failback_delay_seconds=60.0)
        _switched_at(h, 600)
        _set_anchor(h, h.clock() - 60)
        outcome, _ = _tick(h, [_healthy(h)])
        assert outcome is TickOutcome.SWITCHED
        assert _triggers(h) == ["failback"]


class TestTheDelayNeverWidensTheGates:
    """AC-13 - it defers a handback; it never authorises one.

    The landing gate, the no-return bar and the freshness checks all still
    apply once the delay has elapsed.
    """

    def test_an_elapsed_delay_still_cannot_land_on_an_over_line_primary(
        self, temp_home
    ):
        h = _reserve_active(temp_home, failback_delay_seconds=0.0)
        _switched_at(h, 10)
        outcome, _ = _tick(h, [_exhausted(h)])
        assert outcome is TickOutcome.NO_ACTION
        assert _reasons(h) == ["below-threshold"]
        assert _switches(h) == []

    def test_an_elapsed_delay_still_cannot_take_a_stale_target(self, temp_home):
        h = _reserve_active(temp_home, failback_delay_seconds=0.0)
        _switched_at(h, 10)
        now = h.clock.now
        phase1 = _healthy(h)
        phase2 = {"1": _u(20.0), "2": _stale(70.0, now)}
        outcome, _ = _tick(h, [phase1, phase1, phase2])
        assert outcome is TickOutcome.NO_ACTION
        assert "stale-usage" in _reasons(h)
        assert _switches(h) == []


class TestTheLockedRecheckIsAuthoritative:
    """AC-17 - two engines, one serialized decision, even at delay 0.

    `_perform`'s lock exists so a loop engine and a `cswap auto --once` cron
    tick cannot both act on the same snapshot. Cooldown used to supply that
    refusal for failback; once failback is exempt from cooldown, the locked
    recheck has to supply it directly, or the opt-in silently removes the
    serialization for exactly the fleets that opted in.
    """

    def test_a_second_engine_cannot_act_on_a_pre_switch_snapshot(self, temp_home):
        """A real interleaving, not a direct call into the private method.

        `other` is the cron `--once` tick; `h.engine` is the loop. `other`
        enters its tick and captures `lastSwitchAt` as it then stands, and the
        loop engine completes a whole real tick — and a real switch — while
        `other` is still collecting usage. `other` then walks the rest of its
        own tick on that superseded snapshot and must refuse at the lock.
        """
        h = _fleet(temp_home, n=3, failback_delay_seconds=0.0)
        _standby(h, 1)
        h.engine = h._make_engine()
        other = h._make_engine()
        snapshot = {"1": _u(20.0), "2": _u(10.0), "3": _u(10.0)}
        interleaved = {"done": False}

        def _fetch(**_kwargs):
            entries = {
                num: _entry_for(value, h.clock.now)
                for num, value in snapshot.items()
            }
            if not interleaved["done"]:
                # Re-entrancy is bounded by the flag: the loop engine's own
                # tick calls this same patched collector, and by then the
                # switch has already been triggered.
                interleaved["done"] = True
                assert h.engine.tick() is TickOutcome.SWITCHED
            return entries

        with patch.object(
            h.switcher, "usage_entries_by_account", side_effect=_fetch
        ):
            result = other.tick()

        assert interleaved["done"], "the interleaving never happened"
        assert result is TickOutcome.NO_ACTION
        assert DELAY_REASON in _reasons(h)
        assert "another engine switched first" in _details(h)
        assert len(_switches(h)) == 1, "exactly one engine may switch"
        assert h.active_number() == 2


class TestALostDisarmFailsClosed:
    """AC-19 - a disarm that cannot be written must not license a handback.

    Execution review F1. The `finally` disarm swallows its exception so that
    bookkeeping never rewrites an outcome the tick already emitted — but the
    anchor is load-bearing timer state, not bookkeeping. Left trusted, a
    window that was lost hours ago authorises an instant handback, which is
    the one thing the timer exists to prevent.

    The guard is **in-process only**, and its lifecycle is half the contract:
    round 2 of execution review found that a distrust flag which never clears
    blocks the handback forever, and that the durable age bound which was
    supposed to extend the guard across processes instead broke `cswap auto
    --once` on every cron invocation. So: distrust until a write succeeds, then
    stop; and never infer staleness from elapsed time, because only the polling
    loop controls its own cadence.
    """

    def test_a_failed_clear_is_not_trusted_by_the_same_engine(self, temp_home):
        """In-process: the engine that saw the write fail refuses the value."""
        h = _reserve_active(temp_home, failback_delay_seconds=60.0)
        _switched_at(h, 10)
        _tick(h, [_healthy(h)])
        armed = _anchor(h)
        assert armed is not None

        with patch(
            "claude_swap.autoswitch.atomic_write_json",
            side_effect=OSError("disk full"),
        ):
            outcome, _ = _tick(h, [_exhausted(h)])
        assert outcome is TickOutcome.NO_ACTION, "the tick's own outcome stands"
        assert _anchor(h) == armed, "the stale value really is still on disk"

        # +60s, so the stale anchor is 60s old: the delay has elapsed, but the
        # value is still inside the 120s (delay + interval) durable bound. Only
        # the in-memory flag can refuse it here, which is what makes this
        # oracle discriminate that guard rather than the bound.
        h.clock.advance(60)
        assert h.clock() - armed < 60.0 + h.settings.interval_seconds
        h.events.clear()
        outcome, _ = _tick(h, [_healthy(h)])
        assert outcome is TickOutcome.NO_ACTION
        assert _reasons(h) == [DELAY_REASON]
        rearmed = _anchor(h)
        assert rearmed == h.clock(), "re-armed from now, not measured from T"

        # And the distrust must not outlive the problem. The re-arm above was a
        # successful write, so the value on disk is this engine's own: the next
        # full window has to actually elapse into a handback. Without clearing
        # the flag the engine re-arms on every tick forever and the timer never
        # fires at all - a worse failure than the staleness it guarded against.
        assert h.engine._failback_anchor_untrusted is False
        h.clock.advance(60)
        h.events.clear()
        outcome, _ = _tick(h, [_healthy(h)])
        assert outcome is TickOutcome.SWITCHED
        assert _triggers(h) == ["failback"]
        assert ANCHOR not in h.state()

    def test_an_unreadable_state_file_is_not_read_as_an_absent_anchor(
        self, temp_home
    ):
        """AC-19(d) - "no anchor" and "could not read the anchor" differ.

        Execution review R3-F1. `_read_state` answers `{}` for a missing file,
        a corrupt one and an I/O error alike. The disarm treating that as
        confirmed absence is a silent no-op that leaves the anchor armed *and*
        restores trust in it - so the next recovery hands back with no window
        at all, which is the failure this whole guard exists to stop.
        """
        h = _reserve_active(temp_home, failback_delay_seconds=60.0)
        _switched_at(h, 10)
        _tick(h, [_healthy(h)])
        armed = _anchor(h)
        assert armed is not None

        real_read_text = pathlib.Path.read_text
        state_path = h.engine.state_path

        def _unreadable(self, *args, **kwargs):
            if self == state_path:
                raise OSError("transient I/O error")
            return real_read_text(self, *args, **kwargs)

        with patch.object(pathlib.Path, "read_text", _unreadable):
            outcome, _ = _tick(h, [_exhausted(h)])
        assert outcome is TickOutcome.NO_ACTION
        assert _anchor(h) == armed, "the anchor really is still armed on disk"
        assert h.engine._failback_anchor_untrusted is True

        h.clock.advance(60)
        h.events.clear()
        outcome, _ = _tick(h, [_healthy(h)])
        assert outcome is TickOutcome.NO_ACTION, "a stale window must not fire"
        assert _reasons(h) == [DELAY_REASON]
        assert _anchor(h) == h.clock()

    def test_a_cron_scheduler_gap_does_not_invalidate_the_anchor(self, temp_home):
        """Out of process, and the reason the anchor is persisted at all.

        `cswap auto --once` is a cron mode whose cadence is external - the
        README documents a five-minute schedule. A fresh process must measure
        from the anchor its predecessor wrote, however long ago that was. An
        earlier revision rejected anything older than `delay + intervalSeconds`
        on the theory that a still-eligible handback would already have fired;
        under a five-minute cron that is false on every single invocation, and
        the timer never elapsed at all.
        """
        h = _reserve_active(temp_home, failback_delay_seconds=60.0)
        _switched_at(h, 4_000)
        _set_anchor(h, h.clock() - 300)  # one five-minute cron gap
        fresh = h._make_engine()
        assert fresh._failback_anchor_untrusted is False, "a new process is clean"
        h.engine = fresh
        outcome, _ = _tick(h, [_healthy(h)])
        assert outcome is TickOutcome.SWITCHED
        assert _triggers(h) == ["failback"]

    def test_a_fresh_process_measures_from_the_stored_anchor_not_from_now(
        self, temp_home
    ):
        """The identity half: it is the stored value being honoured, not the
        gap being ignored. 30s of a 60s window has run, so this one holds."""
        h = _reserve_active(temp_home, failback_delay_seconds=60.0)
        _switched_at(h, 4_000)
        _set_anchor(h, h.clock() - 30)
        h.engine = h._make_engine()
        outcome, _ = _tick(h, [_healthy(h)])
        assert outcome is TickOutcome.NO_ACTION
        assert _reasons(h) == [DELAY_REASON]
        assert _anchor(h) == h.clock() - 30, "the window kept running"


class TestDryRunReadsButNeverWrites:
    """AC-18 - `--dry-run` reads the timer and writes nothing, on every path.

    (c) is the discriminator, and it is the defect plan review round 2 caught:
    (a) and (b) both reach `_perform`, so an implementation that marks the
    timer observed inside `_perform` passes them while a dry-run tick that
    returns early still deletes a real tick's anchor at the wrapper.
    """

    def _dry(self, temp_home, **kwargs) -> EngineHarness:
        h = _reserve_active(temp_home, **kwargs)
        h.engine = h._make_engine(dry_run=True)
        return h

    def test_a_no_anchor_holds_and_writes_nothing(self, temp_home):
        h = self._dry(temp_home, failback_delay_seconds=60.0)
        _switched_at(h, 10)
        before = h.engine.state_path.read_bytes()
        outcome, _ = _tick(h, [_healthy(h)])
        assert outcome is TickOutcome.NO_ACTION
        assert _reasons(h) == [DELAY_REASON]
        assert h.engine.state_path.read_bytes() == before
        assert ANCHOR not in h.state()

    def test_b_an_elapsed_anchor_previews_the_switch_and_writes_nothing(
        self, temp_home
    ):
        h = self._dry(temp_home, failback_delay_seconds=60.0)
        _switched_at(h, 600)
        _set_anchor(h, h.clock() - 60)
        before = h.engine.state_path.read_bytes()
        outcome, _ = _tick(h, [_healthy(h)])
        assert outcome is TickOutcome.SWITCHED
        switches = _switches(h)
        assert [e.trigger for e in switches] == ["failback"]
        assert switches[0].dry_run is True
        assert h.engine.state_path.read_bytes() == before

    def test_c_an_early_return_leaves_a_real_ticks_anchor_intact(self, temp_home):
        """An armed anchor, a dry-run tick that never reaches `_perform`."""
        h = self._dry(temp_home, failback_delay_seconds=60.0)
        _switched_at(h, 600)
        _set_anchor(h, h.clock() - 10)
        before = h.engine.state_path.read_bytes()
        now = h.clock.now
        phase1 = _healthy(h)
        phase2 = {"1": _u(20.0), "2": _stale(70.0, now)}
        outcome, _ = _tick(h, [phase1, phase1, phase2])
        assert outcome is TickOutcome.NO_ACTION
        assert "stale-usage" in _reasons(h)
        assert h.engine.state_path.read_bytes() == before
        assert _anchor(h) == now - 10, "the real engine still measures from it"
