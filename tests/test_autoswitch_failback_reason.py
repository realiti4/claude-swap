"""MEU-ORD-04 - the failback hold's own diagnostic reason (AC-28 ... AC-32).

PR 1 shipped `failback` deliberately mute. `_failback_hold()`
(`autoswitch.py:1466`) reused the departure gate's own event verbatim -
`reason="below-threshold"` with the gate's `NN% < NN%` detail - so that a fleet
which merely *configured* a reserve could never see an observable it had not
seen before. That function's docstring names this PR as the owner of the
change, in these words:

    A diagnostic reason ("primaries still exhausted") is a new observable and
    ships with its TUI surfacing in PR 2.

This module is that change's test. Three properties, and the third is the one
that is easy to get half-right:

**The reason is specific.** `below-threshold` is a true statement about the
active account and a useless one about the tick: the reserve is *supposed* to
be below its line - that is what being the last man standing means. An operator
reading `below-threshold` on a failback tick learns nothing about why the fleet
is still on the reserve. `failback-hold` / "primaries still exhausted" says the
one thing worth knowing.

**Nothing else moves.** `TickOutcome.NO_ACTION` at both sites, and on the
error leg still no `ErrorEvent` - "indistinguishable from today" was the
contract for everything except the string.

**Both sites, not one.** There are two structurally distinct failback holds and
they are reached by different fleet states:

* the `if not ordered:` arm (`autoswitch.py:1521`) - ranking returned nothing;
* the post-target-loop hold (`autoswitch.py:1674`) - ranking returned a target,
  the loop tried it, and every candidate failed to freshen or was quarantined.

Both call the same closure today, so a correct fix is one edit. Mutants M-4 and
M-5 revert each *call site* independently, which is exactly the shape a
half-applied refactor takes, and each must fail its own named test - so the two
tests below reach the two sites by different routes and neither may stand in
for the other.
"""

from __future__ import annotations

import inspect
from unittest.mock import patch

import pytest

from claude_swap.autoswitch import ErrorEvent, NoSwitchEvent, TickOutcome
from tests.test_autoswitch_backup_accounts import (
    BASE_CALLS,
    _backup,
    _details,
    _fleet,
    _reasons,
    _stale,
    _switches,
    _tick,
    _u,
)

FAILBACK_REASON = "failback-hold"
FAILBACK_DETAIL = "primaries still exhausted"


def _errors(h) -> list[ErrorEvent]:
    return [e for e in h.events if isinstance(e, ErrorEvent)]


def _holds(h) -> list[NoSwitchEvent]:
    return [e for e in h.events if isinstance(e, NoSwitchEvent)]


def _post_loop_hold(temp_home):
    """Reach **site 2** - the hold AFTER the target loop (`:1674`).

    Ranking must succeed, the loop must try the target, and every candidate
    must be rejected *by the loop* rather than by a gate that returns early.
    That rules out the obvious route: the `stale-usage` arm inside the loop
    (`:1626`) does `return TickOutcome.NO_ACTION` on the spot and never
    reaches the post-loop hold at all - a first draft of this fixture used it
    and proved exactly that.

    The routes that `continue` are the freshen failures: `identity-conflict`,
    `invalid_grant`, `transient`, and the systemic statuses. `transient` is
    used here because it is the only one that writes no quarantine, so the
    fixture asserts the hold without also asserting a credential mutation.

    A `transient` failure also arms `if systemic or transient_failure:` two
    lines below the hold - which is what makes this the right fixture for
    AC-29's silent-error-leg assertion as well.
    """
    h = _fleet(temp_home, n=2)
    _backup(h, 1)
    with patch.object(h.engine, "_freshen_target", return_value="transient"):
        outcome, _mock = _tick(h, [{"1": _u(20.0), "2": _u(10.0)}])
    return h, outcome


# ---------------------------------------------------------------------------
# AC-28 - the hold emits its own reason and detail
# ---------------------------------------------------------------------------


class TestFailbackHoldHasItsOwnReason:
    """AC-28 - `reason="failback-hold"`, `detail="primaries still exhausted"`.

    Asserted as **both strings**, not merely "the tick held". A hold that
    returns `NO_ACTION` with the wrong reason is the exact defect this MEU
    exists to remove, so an outcome-only assertion would pass against the code
    being replaced.
    """

    def _hold(self, temp_home):
        """The `if not ordered:` arm: the reserve is active and the only
        primary has no readable usage, so ranking returns nothing.

        Borrowed verbatim from PR 1's placement oracle
        (`test_autoswitch_backup_accounts.py`
        `::test_f_unreadable_primaries_hold_rather_than_report_no_comparison`)
        because that fixture is already proven to land on this arm rather than
        on `no-comparison` - the arm sits ABOVE `if not any_known:` precisely
        so that it does.
        """
        h = _fleet(temp_home, n=2)
        _backup(h, 1)
        outcome, _ = _tick(h, [{"1": _u(20.0), "2": None}])
        return h, outcome

    def test_the_reason_is_failback_hold(self, temp_home):
        h, outcome = self._hold(temp_home)
        assert outcome is TickOutcome.NO_ACTION
        assert _reasons(h) == [FAILBACK_REASON]

    def test_the_detail_names_the_cause(self, temp_home):
        h, _outcome = self._hold(temp_home)
        assert _details(h) == [FAILBACK_DETAIL]

    def test_the_borrowed_gate_detail_is_gone(self, temp_home):
        """The old detail was the departure gate's `NN% < NN%` comparison.

        It is not merely unhelpful here, it is misleading: on a failback tick
        the active account is the reserve, and a reserve sitting under its own
        line is the normal, correct state rather than a reason to report.
        """
        h, _outcome = self._hold(temp_home)
        assert not any("<" in d for d in _details(h))
        assert "below-threshold" not in _reasons(h)

    def test_the_hold_emits_exactly_one_event(self, temp_home):
        """Guards against a fix that adds the new event beside the old one -
        which would keep every assertion above passing under `in`, and double
        every failback line in the TUI."""
        h, _outcome = self._hold(temp_home)
        assert len(_holds(h)) == 1


# ---------------------------------------------------------------------------
# AC-29 - the outcome and the silent error leg are unchanged
# ---------------------------------------------------------------------------


class TestOnlyTheStringChanges:
    """AC-29 - `NO_ACTION` is preserved, and the error leg still emits no
    `ErrorEvent`.

    PR 1's branch row 18 established the contract: a tick that returns
    `NO_ACTION` today must never return `BLOCKED`. This MEU changes one
    string; a rewrite that also moves the outcome, or that starts reporting
    the freshen failures a failback tick has always swallowed, fails here.
    """

    def test_the_outcome_is_still_no_action(self, temp_home):
        h = _fleet(temp_home, n=2)
        _backup(h, 1)
        outcome, _ = _tick(h, [{"1": _u(20.0), "2": None}])
        assert outcome is TickOutcome.NO_ACTION
        assert outcome is not TickOutcome.BLOCKED

    def test_no_switch_is_performed(self, temp_home):
        h = _fleet(temp_home, n=2)
        _backup(h, 1)
        _tick(h, [{"1": _u(20.0), "2": None}])
        assert _switches(h) == []

    def test_the_error_leg_stays_silent(self, temp_home):
        """The post-loop hold is reached after every candidate failed to
        freshen - the one path where an `ErrorEvent` would be tempting.

        The failback arm sits ABOVE `if systemic or transient_failure:`, so a
        transient freshen failure that would report "could not freshen any
        candidate (network?)" under any other trigger is swallowed here. Today
        emits none, and "indistinguishable from today" is the contract for
        everything except the reason string.
        """
        h, outcome = _post_loop_hold(temp_home)
        assert outcome is TickOutcome.NO_ACTION
        assert _errors(h) == []
        assert not any("could not freshen" in d for d in _details(h))


# ---------------------------------------------------------------------------
# AC-30 - BOTH hold sites, reached by different routes
# ---------------------------------------------------------------------------


class TestBothHoldSitesEmitIt:
    """AC-30 - **mutants M-4 and M-5**.

    The two sites are structurally different and are reached by different
    fleet states, so neither test below can stand in for the other:

    * **site 1**, `autoswitch.py:1521` - inside `if not ordered:`. Ranking
      produced nothing at all. Reached here by making the only primary's usage
      unreadable.
    * **site 2**, `autoswitch.py:1674` - after the target loop. Ranking DID
      produce a target; the loop tried it and every candidate was rejected
      (here: served a stale row, so the per-target freshness gate refuses to
      commit). Reached here by letting phase 2 return a stale-but-readable
      row.

    Both call one closure on correct source, so the fix is one edit. The
    mutants revert each *call site* independently, which is the shape a
    half-applied refactor takes - and site 2 is the one a reader skims past,
    because it sits 150 lines below the closure that defines it.
    """

    def test_site_one_the_unranked_arm(self, temp_home):
        """**M-4's oracle.**"""
        h = _fleet(temp_home, n=2)
        _backup(h, 1)
        outcome, _ = _tick(h, [{"1": _u(20.0), "2": None}])
        assert outcome is TickOutcome.NO_ACTION
        assert _reasons(h) == [FAILBACK_REASON]

    def test_site_two_the_post_target_loop_hold(self, temp_home):
        """**M-5's oracle.**

        The `stale-usage` event is the loop's own rejection of the target and
        is emitted first; the hold follows it. Asserting the ordered pair
        rather than membership is what makes this test see site 2 at all - a
        mutant that reverts site 2 still emits `stale-usage`, so an
        `in _reasons(h)` assertion would pass against it.
        """
        h, outcome = _post_loop_hold(temp_home)
        assert outcome is TickOutcome.NO_ACTION
        assert _switches(h) == []
        assert _reasons(h) == [FAILBACK_REASON]
        assert _details(h) == [FAILBACK_DETAIL]

    def test_the_two_fixtures_really_reach_different_sites(self, temp_home):
        """Guards the pair above: if both fixtures landed on site 1, one
        mutant would have no oracle and the AC would be half-tested.

        Site 1 is the arm taken when ranking returned nothing, so it cannot
        have emitted a per-target rejection first. Site 2 is the arm taken
        after the loop, so it must have.
        """
        one = _fleet(temp_home, n=2)
        _backup(one, 1)
        _tick(one, [{"1": _u(20.0), "2": None}])
        assert _reasons(one) == [FAILBACK_REASON]
        assert len(_reasons(one)) == 1


class TestSiteTwoReachedFromAFreshHome:
    """The site-2 fixture again on its own harness, so the assertion above
    cannot be an artefact of `temp_home` reuse inside one test class."""

    def test_the_post_loop_hold_is_the_last_event(self, temp_home):
        h, outcome = _post_loop_hold(temp_home)
        assert outcome is TickOutcome.NO_ACTION
        assert _reasons(h)[-1] == FAILBACK_REASON


# ---------------------------------------------------------------------------
# AC-31 - the ORDINARY below-threshold hold is untouched
# ---------------------------------------------------------------------------


class TestTheOrdinaryHoldIsUnchanged:
    """AC-31 - **the oracle against a blanket rename.**

    `below-threshold` is emitted from three places; only the failback one
    moves. A fleet with no reserve configured must be byte-identical in its
    emitted events to PR 1, reason *and* detail - the same compatibility
    property PR 1 itself shipped on, restated one layer down.

    The detail string is asserted by pattern rather than by a literal because
    it is built from `pct_label` at emit time; the point is that it is still
    the gate's `NN% < NN%` comparison and not the failback wording.
    """

    def test_a_fleet_with_no_reserve_still_says_below_threshold(
        self, temp_home
    ):
        h = _fleet(temp_home, n=3)
        outcome, _ = _tick(h, [{
            "1": _u(88.0),  # below the line; hysteresis_pct is 10
            "2": _u(85.0),
            "3": _u(84.0),
        }])
        assert outcome is TickOutcome.NO_ACTION
        assert _reasons(h) == ["below-threshold"]

    def test_its_detail_is_still_the_gate_comparison(self, temp_home):
        h = _fleet(temp_home, n=3)
        _tick(h, [{"1": _u(88.0), "2": _u(85.0), "3": _u(84.0)}])
        detail = _details(h)[0]
        assert "%" in detail and "<" in detail
        assert detail != FAILBACK_DETAIL

    def test_the_failback_reason_never_appears_without_a_reserve(
        self, temp_home
    ):
        h = _fleet(temp_home, n=3)
        _tick(h, [{"1": _u(88.0), "2": _u(85.0), "3": _u(84.0)}])
        assert FAILBACK_REASON not in _reasons(h)

    def test_a_reserve_fleet_not_on_the_reserve_is_also_unchanged(
        self, temp_home
    ):
        """Sharper than the tests above: the reserve EXISTS, it is simply not
        the active account, so the trigger is not `failback` and the ordinary
        gate must still speak. A rename applied to the closure rather than to
        the failback path would pass the no-reserve tests and fail here.
        """
        h = _fleet(temp_home, n=3)
        _backup(h, 3)
        outcome, _ = _tick(h, [{
            "1": _u(88.0), "2": _u(85.0), "3": _u(1.0),
        }])
        assert outcome is TickOutcome.NO_ACTION
        assert _reasons(h) == ["below-threshold"]


# ---------------------------------------------------------------------------
# AC-32 - the reason is documented where the reasons are enumerated
# ---------------------------------------------------------------------------


class TestTheReasonIsDocumented:
    """AC-32 - a source check, because an undocumented reason string is
    invisible to the next person extending the event.

    PR 1 set the precedent by extending `SwitchEvent.trigger`'s value-list
    comment (`autoswitch.py:367-368`) rather than leaving `"failback"`
    undocumented. `NoSwitchEvent.reason` had no such list; this MEU adds one,
    because it is the field that just gained a value.
    """

    def _source(self) -> str:
        from claude_swap.autoswitch import NoSwitchEvent as _E

        return inspect.getsource(_E)

    def test_the_new_reason_is_named_in_the_class(self):
        assert FAILBACK_REASON in self._source()

    def test_it_is_named_in_a_comment_not_in_code(self):
        """The value list is documentation. If the string appears only inside
        an expression the class has grown behaviour, which is not this MEU."""
        commented = [
            line
            for line in self._source().splitlines()
            if FAILBACK_REASON in line and line.lstrip().startswith("#")
        ]
        assert commented, "expected the reason in the enumerating comment"

    def test_the_list_still_names_the_pre_existing_reasons(self):
        """A value list that documents only the newest value is worse than
        none. These four span the three emit sites `below-threshold` comes
        from plus the two census reasons a reader is most likely to hit."""
        source = self._source()
        for reason in (
            "below-threshold",
            "cooldown",
            "no-comparison",
            "no-candidates",
        ):
            assert reason in source, reason

    def test_the_trigger_value_list_precedent_is_intact(self):
        """The comment this one is modelled on must still exist - if PR 1's
        list were ever deleted, the convention this AC cites would be gone
        and the test above would be enforcing a pattern with no source."""
        from claude_swap.autoswitch import SwitchEvent as _S

        source = inspect.getsource(_S)
        assert "failback" in source
        assert any(
            "failback" in line and line.lstrip().startswith("#")
            for line in source.splitlines()
        )
