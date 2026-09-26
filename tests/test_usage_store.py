"""Tests for the per-account usage store."""

from __future__ import annotations

import json
import logging

import pytest

from claude_swap import oauth, usage_store
from claude_swap.poll_policy import (
    CANDIDATE_MAX_INTERVAL_S,
    POST_429_MIN_INTERVAL_S,
    POST_SWITCH_REPLAN_DEFER_S,
)
from claude_swap.usage_store import (
    BACKOFF_BASE_S,
    BACKOFF_CAP_S,
    CLAIM_TTL_S,
    SERVE_TTL_S,
    STALE_OK_S,
    TRUST_MAX_AGE_S,
    WALL_FALLBACK_S,
    FetchRecord,
    UsageEntry,
    UsageStore,
    due_candidate,
    with_sentinel,
)

IDENT = {"1": ("a@x.com", ""), "2": ("b@x.com", "org-2")}
USAGE = {"five_hour": {"pct": 25.0}, "seven_day": {"pct": 10.0}}


class FakeClock:
    def __init__(self, start: float = 1_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def store(tmp_path, clock):
    return UsageStore(tmp_path / "cache", clock=clock)


class TestSchema:
    def test_empty_when_missing(self, store):
        entries = store.entries(IDENT)
        assert entries["1"] == UsageEntry()
        assert entries["1"].decision_value() is None

    def test_versionless_legacy_snapshot_ignored(self, store):
        store.path.parent.mkdir(parents=True)
        store.path.write_text(
            json.dumps({"timestamp": 123, "data": {"1": USAGE}}), encoding="utf-8"
        )
        assert store.entries(IDENT)["1"].last_good is None

    def test_corrupt_file_ignored(self, store):
        store.path.parent.mkdir(parents=True)
        store.path.write_text("{not json", encoding="utf-8")
        assert store.entries(IDENT)["1"] == UsageEntry()

    def test_round_trip(self, store, clock):
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        raw = json.loads(store.path.read_text(encoding="utf-8"))
        assert raw["schemaVersion"] == 2
        row = raw["accounts"]["1"]
        assert row["email"] == "a@x.com"
        assert row["lastGood"] == USAGE
        assert row["fetchedAt"] == clock.now
        entry = store.entries(IDENT)["1"]
        assert entry.last_good == USAGE
        assert entry.age_s == 0.0
        assert entry.decision_value() == USAGE


class TestStaleOnError:
    def test_failure_preserves_last_good(self, store, clock):
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        clock.advance(60)
        store.record({"1": FetchRecord(error="http-429")}, IDENT)
        entry = store.entries(IDENT)["1"]
        assert entry.last_good == USAGE
        assert entry.age_s == 60.0
        assert entry.last_error == "http-429"
        assert entry.consecutive_failures == 1
        # Still trusted for decisions while within STALE_OK_S.
        assert entry.decision_value() == USAGE

    def test_too_stale_is_unknown_for_decisions(self, store, clock):
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        clock.advance(STALE_OK_S + 1)
        entry = store.entries(IDENT)["1"]
        assert entry.decision_value() is None
        # ... but display still sees the measurement + its age.
        assert entry.last_good == USAGE
        assert entry.age_s == STALE_OK_S + 1

    def test_success_clears_failure_state(self, store, clock):
        store.record({"1": FetchRecord(error="timeout")}, IDENT)
        clock.advance(5)
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        entry = store.entries(IDENT)["1"]
        assert entry.consecutive_failures == 0
        assert entry.last_error is None
        assert entry.backoff_until is None
        assert entry.decision_value() == USAGE

    def test_success_with_no_windows(self, store):
        store.record({"1": FetchRecord(usage=None)}, IDENT)
        entry = store.entries(IDENT)["1"]
        assert entry.last_error is None
        assert entry.fetched_at is not None
        assert entry.decision_value() is None


class TestExtendedTrust:
    """Deliberate staleness stays trusted past STALE_OK_S, capped at
    TRUST_MAX_AGE_S -- but only for a row that has NOT failed (scheduler
    cadence, a live fetch lease).

    A row whose last poll attempt FAILED is capped at POST_429_MIN_INTERVAL_S
    instead (T1102), whatever the error kind, Retry-After, backoff state or a
    window's own reset says: a reading the poller could not refresh must go
    unknown quickly, not stay trusted on a stale percentage. This replaces
    the old rule (commits 64840952, dd5c9ac1) that extended a 429's trust to
    its window's reset or RATE_LIMIT_TRUST_MAX_AGE_S -- the owner's order
    overrides that past the poll period; an out-of-band wall the pin itself
    saw is UsageStore.mark_at_limit's job instead (see TestMarkAtLimit).
    """

    @pytest.mark.parametrize("error,kwargs", [
        ("http-429", {"retry_after_s": 480.0}),
        ("timeout", {}),
    ])
    def test_a_failed_reading_is_trusted_up_to_the_poll_period(
        self, store, clock, error, kwargs
    ):
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        store.record({"1": FetchRecord(error=error, **kwargs)}, IDENT)
        clock.advance(POST_429_MIN_INTERVAL_S)
        entry = store.entries(IDENT)["1"]
        assert entry.trust_extended
        assert entry.decision_value() == USAGE

    @pytest.mark.parametrize("error,kwargs", [
        ("http-429", {"retry_after_s": 480.0}),
        ("timeout", {}),
    ])
    def test_a_failed_reading_past_the_poll_period_is_unknown(
        self, store, clock, error, kwargs
    ):
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        store.record({"1": FetchRecord(error=error, **kwargs)}, IDENT)
        clock.advance(POST_429_MIN_INTERVAL_S + 1)
        entry = store.entries(IDENT)["1"]
        assert not entry.trust_extended
        assert entry.decision_value() is None

    def test_backoff_expiry_does_not_extend_trust_past_the_poll_period(
        self, store, clock
    ):
        # The old rule kept a failed row trusted once its OWN backoff expired,
        # up to TRUST_MAX_AGE_S. That extension is gone: backoff state no
        # longer matters to trust, only age since the last success does.
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        store.record({"1": FetchRecord(error="timeout")}, IDENT)
        clock.advance(BACKOFF_BASE_S + 1)
        assert not store.entries(IDENT)["1"].in_backoff(clock.now)
        clock.advance(POST_429_MIN_INTERVAL_S)  # ...but still past the cap
        assert store.entries(IDENT)["1"].decision_value() is None

    def test_a_far_future_429_reset_does_not_extend_trust_either(
        self, store, clock
    ):
        # The old rule trusted a 429-frozen reading up to its window's own
        # reset. Gone: a reset far in the future no longer rescues a stale
        # poll past the poll period.
        from datetime import datetime, timezone

        far = (
            datetime.fromtimestamp(clock.now + 100_000.0, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        usage = {
            "five_hour": {"pct": 25.0, "resets_at": far},
            "seven_day": {"pct": 10.0, "resets_at": far},
        }
        store.record({"1": FetchRecord(usage=usage)}, IDENT)
        store.record({"1": FetchRecord(error="http-429")}, IDENT)
        clock.advance(POST_429_MIN_INTERVAL_S + 1)
        store.record({"1": FetchRecord(error="http-429")}, IDENT)
        assert store.entries(IDENT)["1"].decision_value() is None

    def test_within_poll_plan_past_stale_ok_is_trusted(self, store, clock):
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        store.set_poll_plan({"1": (clock.now + 600.0, 600.0)}, IDENT)
        clock.advance(400)
        entry = store.entries(IDENT)["1"]
        assert entry.consecutive_failures == 0
        assert entry.decision_value() == USAGE
        # Once overdue, the staleness is no longer scheduler-chosen.
        clock.advance(250)
        assert store.entries(IDENT)["1"].decision_value() is None

    def test_trust_ceiling_wins_over_non_failed_stale_plan(self, store, clock):
        # A row past TRUST_MAX_AGE_S with no failure reads as unknown even
        # with a LIVE plan (nextPollAt still ahead) -- the ceiling must win
        # over the scheduler-cadence extension, not just over "no plan at
        # all" (which trust_extended would already refuse for its own
        # reason).
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        store.set_poll_plan(
            {"1": (clock.now + TRUST_MAX_AGE_S + 500.0, 500.0)}, IDENT
        )
        clock.advance(TRUST_MAX_AGE_S + 1)
        entry = store.entries(IDENT)["1"]
        assert entry.consecutive_failures == 0
        assert entry.decision_value() is None


class TestMarkAtLimit:
    """Rule 2 (T1102): an out-of-band at-limit signal the poller cannot see
    (the pin's own 429 on /v1/messages, via UsageStore.mark_at_limit) makes
    decision_value() report the slot full until a persisted deadline, whatever
    a poll taken meanwhile says."""

    def _usage_resetting_at(self, clock, seconds_ahead):
        from datetime import datetime, timezone

        iso = (
            datetime.fromtimestamp(clock.now + seconds_ahead, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        return {
            "five_hour": {"pct": 25.0, "resets_at": iso},
            "seven_day": {"pct": 10.0, "resets_at": iso},
        }

    def test_full_before_the_earliest_reset_and_the_poll_after_it(
        self, store, clock
    ):
        usage = self._usage_resetting_at(clock, 600.0)  # resets in 10 min
        iso = usage["five_hour"]["resets_at"]
        store.record({"1": FetchRecord(usage=usage)}, IDENT)
        store.mark_at_limit("1", IDENT)

        # five_hour forced full at the mark's own deadline; seven_day kept
        # from the stored reading (I1: a reader like autoswitch's
        # `_seven_day_reset_unmeasured` must see the real weekly reset, not
        # "never reported").
        assert store.entries(IDENT)["1"].decision_value() == {
            "five_hour": {"pct": 100.0, "resets_at": iso},
            "seven_day": {"pct": 10.0, "resets_at": iso},
        }

        clock.advance(300.0)  # a poll taken while still walled...
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        assert store.entries(IDENT)["1"].decision_value() == {
            "five_hour": {"pct": 100.0, "resets_at": iso},
            "seven_day": {"pct": 10.0},
        }  # ...does not clear the mark early, but the polled seven_day
        # (no resets_at this time) still passes through unchanged.

        clock.advance(301.0)  # past the mark's own deadline
        fresh = {"five_hour": {"pct": 5.0}, "seven_day": {"pct": 0.0}}
        store.record({"1": FetchRecord(usage=fresh)}, IDENT)
        assert store.entries(IDENT)["1"].decision_value() == fresh

    def test_a_far_future_reset_is_capped_at_the_wall_fallback_span(
        self, store, clock
    ):
        # I2: `walledUntil = min(earliest future stored reset, now +
        # WALL_FALLBACK_S)` -- a stored reset further out than the fallback
        # (a 7d window, or a malformed far-future one) must not park the
        # mark past WALL_FALLBACK_S.
        usage = self._usage_resetting_at(clock, WALL_FALLBACK_S + 1000.0)
        store.record({"1": FetchRecord(usage=usage)}, IDENT)
        store.mark_at_limit("1", IDENT)

        clock.advance(WALL_FALLBACK_S - 1)
        assert store.entries(IDENT)["1"].walled  # still inside the cap
        clock.advance(2)
        assert not store.entries(IDENT)["1"].walled  # capped, not the far reset

    def test_no_stored_reading_falls_back_to_the_wall_fallback_span(
        self, store, clock
    ):
        store.mark_at_limit("1", IDENT)  # nothing stored to key a reset on
        resets_at = usage_store._reset_ts_to_resets_at(clock.now + WALL_FALLBACK_S)
        # I1: with nothing stored to keep, seven_day is synthesized full at
        # the same deadline instead of being silently dropped.
        assert store.entries(IDENT)["1"].decision_value() == {
            "five_hour": {"pct": 100.0, "resets_at": resets_at},
            "seven_day": {"pct": 100.0, "resets_at": resets_at},
        }
        clock.advance(WALL_FALLBACK_S - 1)
        assert store.entries(IDENT)["1"].walled
        clock.advance(2)
        assert not store.entries(IDENT)["1"].walled


class TestBackoff:
    def test_exponential_backoff(self, store, clock):
        expected = [30.0, 60.0, 120.0, 240.0, 480.0, 600.0, 600.0]
        for i, want in enumerate(expected):
            store.record({"1": FetchRecord(error="http-500")}, IDENT)
            entry = store.entries(IDENT)["1"]
            assert entry.consecutive_failures == i + 1
            assert entry.backoff_until == pytest.approx(clock.now + want)
            clock.advance(want + 1)

    def test_backoff_cap(self):
        assert usage_store._failure_backoff_s(50, None) == BACKOFF_CAP_S

    def test_huge_failure_count_does_not_overflow(self):
        # A permanently failing account increments consecutiveFailures forever;
        # past 1024 failures 2**(n-1) no longer converts to float and the old
        # code raised OverflowError before min() could cap it — killing every
        # subsequent tick (the crash also stopped the state write, so the
        # counter never moved and the loop errored forever).
        assert usage_store._failure_backoff_s(1025, None) == BACKOFF_CAP_S
        assert usage_store._failure_backoff_s(10_000, 90.0) == BACKOFF_CAP_S

    def test_record_failure_on_saturated_counter_does_not_raise(self, store, clock):
        store.path.parent.mkdir(parents=True)
        store.path.write_text(
            json.dumps(
                {
                    "schemaVersion": 2,
                    "accounts": {
                        "1": {
                            "email": "a@x.com",
                            "consecutiveFailures": 1024,
                            "lastError": "refresh-failed",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        store.record({"1": FetchRecord(error="refresh-failed")}, IDENT)
        entry = store.entries(IDENT)["1"]
        assert entry.consecutive_failures == 1025
        assert entry.backoff_until == pytest.approx(clock.now + BACKOFF_CAP_S)

    def test_retry_after_is_the_floor(self, store, clock):
        store.record(
            {"1": FetchRecord(error="http-429", retry_after_s=90.0)}, IDENT
        )
        entry = store.entries(IDENT)["1"]
        # First failure computes 30s, but the server asked for 90s — honored as
        # the floor. No margin below BACKOFF_CAP_S: there our own curve already
        # governs, and adding to a short ask would overtake it.
        assert entry.backoff_until == pytest.approx(clock.now + 90.0)
        assert entry.in_backoff(clock.now + 89)
        assert not entry.in_backoff(clock.now + 91)

    def test_own_curve_may_exceed_retry_after(self):
        assert usage_store._failure_backoff_s(5, 10.0) == pytest.approx(480.0)
        assert BACKOFF_BASE_S * 2**4 == 480.0

    def test_edge_429_backoff_floors_at_edge_backoff(self, store, clock):
        # "Retry-After: 0" is the saturated-window edge: the token's rolling
        # hour is full and frees only as old requests age out, so even the
        # first backoff waits EDGE_BACKOFF_S; the exponential curve may push
        # past it, capped at BACKOFF_CAP_S.
        expected = [300.0, 300.0, 300.0, 300.0, 480.0, 600.0, 600.0]
        for i, want in enumerate(expected):
            store.record(
                {"1": FetchRecord(error="http-429", retry_after_s=0.0)}, IDENT
            )
            entry = store.entries(IDENT)["1"]
            assert entry.consecutive_failures == i + 1
            assert entry.backoff_until == pytest.approx(clock.now + want)
            clock.advance(want + 1)

    def test_a_non_429_retry_after_zero_does_not_take_the_saturated_edge(
        self, store, clock
    ):
        """I-4 (round-10 review): `retry_after_s == 0` used to return before
        the `rate_limited` arm split, so ANY error carrying `Retry-After: 0`
        (e.g. a Cloudflare 503 "retry now") took the 429-only saturated-edge
        floor (EDGE_BACKOFF_S=300s) meant for a rate-limited token's full
        rolling hour. `_classify_usage_error` (oauth.py) parses Retry-After
        for any HTTPError code, not just 429, so this is reachable. A 503
        asking to be retried immediately should fall through to the plain
        exponential curve instead, same as no Retry-After header at all.
        """
        store.record(
            {"1": FetchRecord(error="http-503", retry_after_s=0.0)}, IDENT
        )
        entry = store.entries(IDENT)["1"]
        # 30s: BACKOFF_BASE_S at failures=1, the plain curve — NOT 300s
        # (EDGE_BACKOFF_S), which is the 429-only saturated-edge floor.
        assert entry.backoff_until == pytest.approx(clock.now + 30.0)

    def test_retry_after_floor_is_capped(self):
        # A pathological Retry-After can never park an account for hours.
        assert usage_store._failure_backoff_s(1, 50000.0) == pytest.approx(
            usage_store.RETRY_AFTER_FLOOR_CAP_S
        )

    def test_hour_scale_retry_after_honored(self):
        # The usage endpoint's burst block spans its ~1h rolling window and the
        # server's Retry-After counts that down to a fixed deadline (measured;
        # probing does not re-arm it). Capping it to minutes just re-probes two
        # or three times inside a block that lasts the full window anyway —
        # wasted requests — so an hour-scale Retry-After is honored whole, plus
        # the margin, up to the safety cap.
        assert usage_store._failure_backoff_s(1, 3600.0) == pytest.approx(4500.0)
        assert usage_store.RETRY_AFTER_FLOOR_CAP_S >= 4500.0

    def test_hour_scale_margin_clears_the_measured_re_block_band(self):
        # Honoring Retry-After *exactly* puts the retry on the deadline itself,
        # where the server is not reliably ready: measured over this machine's
        # log (re-measured 2026-08-03, round 8, method in the
        # RETRY_AFTER_MARGIN_S comment), 20 of 35 block lapses re-blocked
        # within 900s of their own deadline (+2s … +887s) and each cost a
        # fresh full hour, while the next one after that is +1004s. ("20 of
        # 35", not "of 38": 3 of the 38 raw gaps are negative — not a uniform
        # mechanism (per-gap detail in the RETRY_AFTER_MARGIN_S comment) —
        # excluded from both numerator and denominator so the fraction stays
        # apples-to-apples; the prior "21 of 36"/"2 of 38" figures here
        # reproduce too, on the OTHER of two equally-valid readings — round 8
        # switched readings, it did not correct a non-reproducing figure; see
        # the RETRY_AFTER_MARGIN_S comment for which reading and why.) On the
        # hour-scale block that produced that evidence,
        # the margin must clear the whole 900s band (13s of clearance:
        # 900 - 887).
        assert usage_store._failure_backoff_s(1, 3600.0) - 3600.0 >= 900.0

    def test_a_short_accurate_block_is_not_inflated(self):
        # The re-block evidence is entirely hour-scale (40 of 41 blocks
        # opened at exactly 3600, re-measured 2026-08-03), while short blocks
        # were separately measured as accurate — Retry-After 300 meant a
        # 300s block. Inflating those on no
        # evidence is still wrong; the margin now stays off them by applying
        # only ABOVE BACKOFF_CAP_S (strictly — an ask OF exactly that is what
        # the saturated curve already waits) rather than by scaling with the ask.
        assert usage_store._failure_backoff_s(1, 300.0) == 300.0
        assert usage_store._failure_backoff_s(1, 3600.0) - 3600.0 == 900.0
        # The boundary itself, both sides. The > / >= mutation at
        # BACKOFF_CAP_S is caught by two tests: this boundary check (which is
        # why it earns its keep — a direct failure here says exactly what
        # broke) and, independently,
        # TestAdaptiveScheduler::test_consume_first_stale_target_holds_then_switches
        # — an unrelated scheduler test whose failure message says nothing
        # about backoff.
        cap = usage_store.BACKOFF_CAP_S
        assert usage_store._failure_backoff_s(1, cap) == cap
        assert usage_store._failure_backoff_s(1, cap + 1.0) == cap + 1.0 + 900.0

    def test_margin_survives_a_mid_block_observation(self):
        # The margin exists to land past a FIXED deadline, and Retry-After is a
        # countdown to it — so what the server reports depends on WHEN we ask.
        # The budget is account-scoped, so a second machine polling into a block
        # another one opened sees only the remainder: that is the normal case,
        # not an edge (34 of 75 observed 429s were mid-block, re-measured
        # 2026-08-03, round 8, method in the RETRY_AFTER_MARGIN_S comment).
        # A margin
        # computed as a FRACTION of the remainder shrinks toward zero as the
        # deadline nears — the 0.25 fraction this replaces made a 1800s
        # remainder land +450s and a 900s remainder land +225s, both inside
        # the measured +2s..+887s re-block band. An absolute margin does not
        # decay.
        for remaining in (3600.0, 1800.0, 900.0):
            overshoot = usage_store._failure_backoff_s(1, remaining) - remaining
            assert overshoot >= 900.0, (
                f"Retry-After {remaining}s lands {overshoot:.0f}s past the "
                "deadline, inside the measured re-block band"
            )

    def test_a_429_wait_is_the_deadline_plus_the_margin(self):
        """The wait comes from the server's deadline, and nothing trims it.

        An earlier revision passed a `trust_expires_in_s` and cut the ask back
        to the deadline when the 429 trust expired first, reasoning that the
        extra 900s bought blindness and no freshness. Measured, that trim can
        never salvage the trust it is named for — its precondition is
        `trust < ask` and the floor keeps `wait >= ask`, so the row is
        untrusted at release either way. Over 180 reachable reset offsets it
        fired 35 times and salvaged trust 0 times, and at every one of them
        BOTH waits released with the row unknown.

        What it did do is drop the wait onto the deadline, which is where the
        measured evidence says we re-block 10 of 19 times for a fresh hour (as
        measured when this was derived — the re-block fraction has since
        moved to 20 of 35, re-measured 2026-08-03, method corrected round 8;
        this episode model concerns the removed 429 trust-trim, out of the
        live path, and is not re-derived at the new fraction). Episode model
        on the original number, 3600 runs:

            with the trim    blind 1148s   requests 1.21
            without it       blind  550s   requests 1.00

        So the parameter is gone and the margin applies to every hour-scale
        429 wait.
        """
        for ask in (3601.0, 3600.0, 4000.0):
            wait = usage_store._failure_backoff_s(1, ask, rate_limited=True)
            expected = min(
                ask + usage_store.RETRY_AFTER_MARGIN_S,
                usage_store.RETRY_AFTER_FLOOR_CAP_S,
            )
            assert wait == expected, (
                f"ask {ask} -> wait {wait}, expected {expected}"
            )

    def test_the_floor_cap_is_the_measured_block_plus_the_margin(self):
        # RETRY_AFTER_FLOOR_CAP_S's own comment justifies it as "the measured
        # block (3600s) plus the margin" ("37 of 39 observed blocks opened at
        # exactly 3600, and 3600 + 900 = this"). Pinned outright: neither
        # test_hour_scale_retry_after_honored (hardcodes 4500.0, not tied to
        # the constant) nor test_retry_after_floor_is_capped (pins only that
        # a huge ask saturates AT the cap, whatever its value) would catch
        # the constant drifting off that arithmetic.
        assert usage_store.RETRY_AFTER_FLOOR_CAP_S == pytest.approx(
            3600.0 + usage_store.RETRY_AFTER_MARGIN_S
        )

    def test_each_arm_is_bounded_by_the_ceiling_its_own_trust_uses(self):
        """A non-429 park must never outlast TRUST_MAX_AGE_S, its own ceiling.

        Pins the PARK duration alone, against the constants `_failure_backoff_s`
        itself is built from — not `entries()`'s decision trust, which a failed
        row (T1102) now caps at `poll_policy.POST_429_MIN_INTERVAL_S` (360s)
        regardless of arm, well inside this park cap. The 429 arm's own park
        cap (`RETRY_AFTER_FLOOR_CAP_S`, 4500s) is pinned by
        `test_the_floor_cap_is_the_measured_block_plus_the_margin` — there is
        no longer a wider 429-only trust ceiling to check it against (the old
        `RATE_LIMIT_TRUST_MAX_AGE_S` this replaced is gone).
        """
        for ask in (3601.0, 4500.0, 7200.0, 86_400.0, float("inf")):
            wait = usage_store._failure_backoff_s(1, ask, rate_limited=False)
            assert wait <= usage_store.TRUST_MAX_AGE_S, (
                f"non-429 ask {ask} produced a {wait}s park, past its own "
                f"trust ceiling {usage_store.TRUST_MAX_AGE_S}s — blind for "
                f"{wait - usage_store.TRUST_MAX_AGE_S:.0f}s"
            )

    def test_the_margin_never_lifts_the_floor_cap(self):
        """`RETRY_AFTER_FLOOR_CAP_S` bounds how long a server ask can park us.

        The margin is added INSIDE that cap, so a pathological header cannot
        buy itself an extra 900s on top.
        """
        huge = 86_400.0
        wait = usage_store._failure_backoff_s(1, huge, rate_limited=True)
        assert wait <= usage_store.RETRY_AFTER_FLOOR_CAP_S, (
            f"a {huge:.0f}s ask produced a {wait:.0f}s wait — the trim wrote "
            "the ask straight through the cap that bounds it"
        )

    def test_short_asks_stay_on_our_own_curve(self):
        # Below BACKOFF_CAP_S the margin deliberately does not apply: our own
        # saturated curve already waits longer than the server asked, so adding
        # to the ask would only overtake it — test_own_curve_may_exceed_retry_after
        # and test_huge_failure_count_does_not_overflow both pin that. A block
        # whose REMAINDER has fallen under the cap therefore still retries near
        # its deadline; distinguishing that from a genuine short burst block
        # needs the row's own backoff state, not the ask, and is left to the
        # caller rather than guessed at here.
        assert usage_store._failure_backoff_s(1, 90.0) == 90.0
        assert usage_store._failure_backoff_s(10_000, 90.0) == BACKOFF_CAP_S

    def test_measured_burst_block_honored_exactly(self):
        # The real burst rule (measured 2026-07-06) sends Retry-After: 300 and
        # the block is exactly that long — honored as the floor, with no margin
        # added: 300 is under BACKOFF_CAP_S, where our own curve governs.
        assert usage_store._failure_backoff_s(1, 300.0) == pytest.approx(300.0)


class TestIdentityGuard:
    def test_slot_reuse_hides_old_usage(self, store):
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        rebound = {"1": ("new@x.com", "")}
        assert store.entries(rebound)["1"] == UsageEntry()

    def test_same_email_different_org_is_a_different_account(self, store):
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        rebound = {"1": ("a@x.com", "org-9")}
        assert store.entries(rebound)["1"] == UsageEntry()

    def test_write_replaces_mismatched_row(self, store):
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        rebound = {"1": ("new@x.com", "")}
        store.record({"1": FetchRecord(error="timeout")}, rebound)
        entry = store.entries(rebound)["1"]
        assert entry.last_good is None  # old account's data did not survive
        assert entry.consecutive_failures == 1

    def test_untouched_slots_survive_subset_writes(self, store):
        store.record(
            {"1": FetchRecord(usage=USAGE), "2": FetchRecord(usage=USAGE)}, IDENT
        )
        store.record({"1": FetchRecord(error="timeout")}, {"1": IDENT["1"]})
        assert store.entries(IDENT)["2"].last_good == USAGE


class TestClaims:
    def test_claim_marks_in_flight(self, store, clock):
        claims = store.claim(["1"], IDENT)
        entry = store.entries(IDENT)["1"]
        assert set(claims) == {"1"}
        assert entry.claimed(clock.now)
        clock.advance(CLAIM_TTL_S + 1)
        assert not store.entries(IDENT)["1"].claimed(clock.now)

    def test_legacy_last_attempt_claim_is_honored_during_schema_overlap(
        self, store, clock
    ):
        store.claim(["1"], IDENT)
        raw = json.loads(store.path.read_text())
        row = raw["accounts"]["1"]
        row.pop("claimId")
        row.pop("claimUntil")
        store.path.write_text(json.dumps(raw))

        assert store.entries(IDENT)["1"].claimed(clock.now)
        assert store.reserve(["1"], IDENT, respect_plans=True) == {}
        clock.advance(11)
        assert set(store.reserve(["1"], IDENT, respect_plans=True)) == {"1"}

    def test_claim_does_not_touch_measurement(self, store, clock):
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        clock.advance(100)
        store.claim(["1"], IDENT)
        entry = store.entries(IDENT)["1"]
        assert entry.last_good == USAGE
        assert entry.age_s == 100.0

    def test_live_claim_outlasts_urgent_poll_interval(self, store, clock):
        claims = store.reserve(["1"], IDENT, respect_plans=True)
        assert set(claims) == {"1"}
        clock.advance(61)
        assert store.reserve(["1"], IDENT, respect_plans=False) == {}

    def test_record_releases_long_claim_immediately(self, store, clock):
        claims = store.reserve(["1"], IDENT, respect_plans=True)
        assert store.entries(IDENT)["1"].claimed(clock.now)
        assert store.record({"1": FetchRecord(usage=USAGE)}, IDENT, claims) == {"1"}
        assert not store.entries(IDENT)["1"].claimed(clock.now)

    def test_failure_releases_claim(self, store, clock):
        claims = store.reserve(["1"], IDENT, respect_plans=True)
        assert store.record({"1": FetchRecord(error="timeout")}, IDENT, claims) == {
            "1"
        }
        entry = store.entries(IDENT)["1"]
        assert not entry.claimed(clock.now)
        assert entry.last_error == "timeout"

    def test_sentinel_releases_claim_without_persisting_state(self, store, clock):
        claims = store.reserve(["1"], IDENT, respect_plans=True)
        claimed_at = store.entries(IDENT)["1"].last_attempt_at
        clock.advance(1)
        assert store.record(
            {"1": FetchRecord(sentinel="token expired")}, IDENT, claims
        ) == {"1"}
        entry = store.entries(IDENT)["1"]
        assert not entry.claimed(clock.now)
        assert entry.last_attempt_at == claimed_at
        assert entry.sentinel is None
        assert entry.last_good is None

    def test_expired_writer_cannot_clear_or_overwrite_new_lease(self, store, clock):
        first = store.reserve(["1"], IDENT, respect_plans=True)
        clock.advance(CLAIM_TTL_S + 1)
        second = store.reserve(["1"], IDENT, respect_plans=True)
        assert first["1"] != second["1"]

        assert store.record(
            {"1": FetchRecord(error="timeout")}, IDENT, first
        ) == set()
        entry = store.entries(IDENT)["1"]
        assert entry.claimed(clock.now)
        assert entry.last_error is None

        assert store.record(
            {"1": FetchRecord(usage=USAGE)}, IDENT, second
        ) == {"1"}
        assert store.entries(IDENT)["1"].last_good == USAGE

    def test_stale_writer_cannot_replace_a_rebound_identity(self, store, clock):
        stale_claim = store.reserve(["1"], IDENT, respect_plans=True)
        rebound = {"1": ("new@x.com", "org-new")}
        assert set(store.reserve(["1"], rebound, respect_plans=True)) == {"1"}

        assert store.record(
            {"1": FetchRecord(usage=USAGE)}, IDENT, stale_claim
        ) == set()
        entry = store.entries(rebound)["1"]
        assert entry.claimed(clock.now)
        assert entry.last_good is None

    def test_partial_records_can_reuse_their_explicit_claims(self, store):
        claims = store.reserve(["1", "2"], IDENT, respect_plans=True)
        assert store.record({"1": FetchRecord(usage=USAGE)}, IDENT, claims) == {
            "1"
        }
        assert store.entries(IDENT)["2"].claimed(store.clock())

        assert store.record({"2": FetchRecord(usage=USAGE)}, IDENT, claims) == {
            "2"
        }
        entries = store.entries(IDENT)
        assert entries["1"].last_good == USAGE
        assert entries["2"].last_good == USAGE

    def test_mixed_record_accepts_only_the_current_claim(self, store, clock):
        first = store.reserve(["1", "2"], IDENT, respect_plans=True)
        clock.advance(CLAIM_TTL_S + 1)
        second = store.reserve(["1"], IDENT, respect_plans=True)
        assert first["1"] != second["1"]

        outcomes = {
            "1": FetchRecord(error="timeout"),
            "2": FetchRecord(usage=USAGE),
        }
        assert store.record(outcomes, IDENT, first) == {"2"}
        entries = store.entries(IDENT)
        assert entries["1"].last_error is None
        assert entries["1"].claimed(clock.now)
        assert entries["2"].last_good == USAGE

    def test_unfenced_record_cannot_overwrite_a_live_claim(self, store, clock):
        claims = store.reserve(["1"], IDENT, respect_plans=True)
        assert store.record({"1": FetchRecord(error="timeout")}, IDENT) == set()
        entry = store.entries(IDENT)["1"]
        assert entry.claimed(clock.now)
        assert entry.last_error is None
        assert store.record({"1": FetchRecord(usage=USAGE)}, IDENT, claims) == {
            "1"
        }

    def test_unfenced_record_accepts_after_a_claim_expires(self, store, clock):
        store.reserve(["1"], IDENT, respect_plans=True)
        clock.advance(CLAIM_TTL_S + 1)
        assert store.record({"1": FetchRecord(usage=USAGE)}, IDENT) == {"1"}
        entry = store.entries(IDENT)["1"]
        assert entry.last_good == USAGE
        assert not entry.claimed(clock.now)

    def test_credential_refresh_revokes_an_old_fetch_claim(self, store, clock):
        claims = store.reserve(["1"], IDENT, respect_plans=True)
        store.clear_dead_token(["1"], IDENT)
        assert store.record(
            {"1": FetchRecord(error="invalid_grant")}, IDENT, claims
        ) == set()
        entry = store.entries(IDENT)["1"]
        assert not entry.claimed(clock.now)
        assert not entry.token_dead()
        assert set(store.reserve(["1"], IDENT, respect_plans=True)) == {"1"}

    def test_success_commits_its_new_plan_without_a_duplicate_window(
        self, store, clock
    ):
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        store.set_poll_plan({"1": (clock.now + 60.0, 60.0)}, IDENT)
        clock.advance(61)
        claims = store.reserve(["1"], IDENT, respect_plans=False)
        assert set(claims) == {"1"}

        next_poll = clock.now + 300.0
        store.record(
            {"1": FetchRecord(usage=USAGE)},
            IDENT,
            claims,
            {"1": (next_poll, 300.0)},
        )
        entry = store.entries(IDENT)["1"]
        assert entry.next_poll_at == next_poll
        assert entry.poll_interval_s == 300.0
        assert store.reserve(["1"], IDENT, respect_plans=False) == {}


class TestSentinels:
    def test_sentinel_record_is_a_store_noop(self, store):
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        store.record({"1": FetchRecord(sentinel="token expired")}, IDENT)
        entry = store.entries(IDENT)["1"]
        assert entry.sentinel is None  # never persisted
        assert entry.last_good == USAGE

    def test_refused_credential_stamp_rides_a_sentinel(self, store):
        """The one thing a sentinel persists: which access token a live
        session's fetch was refused with. Kept across plain sentinels,
        cleared by a success."""
        store.record(
            {"1": FetchRecord(sentinel="token expired", rejected_fp="sha256-at:abc")},
            IDENT,
        )
        assert store.entries(IDENT)["1"].rejected_fingerprint == "sha256-at:abc"
        store.record({"1": FetchRecord(sentinel="token expired")}, IDENT)
        assert store.entries(IDENT)["1"].rejected_fingerprint == "sha256-at:abc"
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        assert store.entries(IDENT)["1"].rejected_fingerprint is None

    def test_overlay_wins_decisions_but_not_display(self, store):
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        entry = with_sentinel(store.entries(IDENT)["1"], "token expired")
        assert entry.decision_value() == "token expired"
        assert entry.last_good == USAGE  # display can still show last-seen

    def test_with_sentinel_none_is_identity(self):
        entry = UsageEntry(last_good=USAGE)
        assert with_sentinel(entry, None) is entry


class TestFreshness:
    def test_fresh_within_serve_ttl(self, store, clock):
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        entry = store.entries(IDENT)["1"]
        assert entry.fresh(clock.now)
        assert entry.fresh(clock.now + SERVE_TTL_S)
        assert not entry.fresh(clock.now + SERVE_TTL_S + 1)


class TestPollPlan:
    def test_set_and_read_poll_plan(self, store, clock):
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        store.set_poll_plan({"1": (clock.now + 120.0, 120.0)}, IDENT)
        entry = store.entries(IDENT)["1"]
        assert entry.next_poll_at == clock.now + 120.0
        assert entry.poll_interval_s == 120.0
        assert entry.last_good == USAGE  # untouched

    def test_poll_plan_clear(self, store, clock):
        store.set_poll_plan({"1": (clock.now + 120.0, 120.0)}, IDENT)
        store.set_poll_plan({"1": (None, None)}, IDENT)
        entry = store.entries(IDENT)["1"]
        assert entry.next_poll_at is None
        assert entry.poll_interval_s is None


class TestDueCandidate:
    """Candidate selection shared by the auto engine and the TUI watch view."""

    NOW = 1_000_000.0

    def test_missing_entry_is_most_due(self):
        entries = {"3": UsageEntry(fetched_at=self.NOW - 60, age_s=60.0)}
        assert due_candidate(["2", "3"], entries, self.NOW) == "2"

    def test_never_fetched_beats_fetched(self):
        entries = {
            "2": UsageEntry(fetched_at=self.NOW - 999, age_s=999.0),
            "3": UsageEntry(),  # row exists but never fetched
        }
        assert due_candidate(["2", "3"], entries, self.NOW) == "3"

    def test_stalest_fetched_wins(self):
        entries = {
            "2": UsageEntry(fetched_at=self.NOW - 60, age_s=60.0),
            "3": UsageEntry(fetched_at=self.NOW - 300, age_s=300.0),
        }
        assert due_candidate(["2", "3"], entries, self.NOW) == "3"

    def test_sentinel_accounts_skipped(self):
        entries = {"2": UsageEntry(sentinel="api-key")}
        assert due_candidate(["2"], entries, self.NOW) is None

    def test_backoff_skipped_until_it_expires(self):
        entries = {"2": UsageEntry(backoff_until=self.NOW + 10)}
        assert due_candidate(["2"], entries, self.NOW) is None
        assert due_candidate(["2"], entries, self.NOW + 11) == "2"

    def test_future_next_poll_at_skipped(self):
        entries = {
            "2": UsageEntry(fetched_at=self.NOW - 300, next_poll_at=self.NOW + 60),
            "3": UsageEntry(fetched_at=self.NOW - 60),
        }
        # "2" is stalest but not yet due per auto's learned plan → "3" wins.
        assert due_candidate(["2", "3"], entries, self.NOW) == "3"

    def test_reset_parked_exhausted_plan_is_due_for_repair(self):
        exhausted = {"seven_day": {"pct": 100.0}}
        entries = {
            "2": UsageEntry(
                last_good=exhausted,
                fetched_at=self.NOW - 400,
                age_s=400.0,
                next_poll_at=self.NOW + 86_400,
                poll_interval_s=300.0,
            )
        }
        assert due_candidate(["2"], entries, self.NOW) == "2"

    def test_bounded_exhausted_plan_is_not_due_early(self):
        exhausted = {"seven_day": {"pct": 100.0}}
        entries = {
            "2": UsageEntry(
                last_good=exhausted,
                fetched_at=self.NOW - 400,
                age_s=400.0,
                next_poll_at=self.NOW + 600,
                poll_interval_s=600.0,
            )
        }
        assert due_candidate(["2"], entries, self.NOW) is None

    def test_parked_plan_is_repaired_after_scoped_model_is_deselected(self):
        entries = {
            "2": UsageEntry(
                last_good={
                    "five_hour": {"pct": 10.0},
                    "seven_day": {"pct": 10.0},
                    "scoped": [{"name": "Fable", "pct": 100.0}],
                },
                fetched_at=self.NOW - 400,
                age_s=400.0,
                next_poll_at=self.NOW + 86_400,
                poll_interval_s=300.0,
            )
        }
        # due_candidate has no current scoped-model selection: repair keys on
        # the impossible deadline shape rather than stale policy semantics.
        assert due_candidate(["2"], entries, self.NOW) == "2"

    def test_none_when_no_candidates(self):
        assert due_candidate([], {}, self.NOW) is None


class TestDeadTokenQuarantine:
    """invalid_grant strikes → token_dead → quarantined from fetching."""

    def test_invalid_grant_advances_strikes(self, store):
        store.record({"1": FetchRecord(error="invalid_grant")}, IDENT)
        assert store.entries(IDENT)["1"].auth_dead_strikes == 1
        store.record({"1": FetchRecord(error="invalid_grant")}, IDENT)
        assert store.entries(IDENT)["1"].auth_dead_strikes == 2

    def test_a_quarantine_says_who_it_quarantined_and_why(self, store, caplog):
        """A permanent verdict that leaves no trace cannot be diagnosed.

        One `invalid_grant` is enough to quarantine a slot (the threshold is
        1), the account then reads "re-login needed" until a human logs in
        again, and nothing anywhere records that it happened. Measured in a
        live incident: four accounts across two machines were quarantined and
        the log held not one line about any of them, so the cause took hours
        to find and could only be reconstructed from the store's own row.

        The strike is the ONE place that knows the slot, the identity and the
        verdict at the moment it binds.
        """
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            store.record({"1": FetchRecord(error="invalid_grant")}, IDENT)
        said = " ".join(r.getMessage() for r in caplog.records)
        assert "1" in said and "a@x.com" in said, (
            f"the quarantine names neither the slot nor the account: {said!r}"
        )
        assert "invalid_grant" in said, (
            f"the quarantine does not say what the server answered: {said!r}"
        )

    def test_a_transient_failure_stays_quiet(self, store, caplog):
        """The control: a 429 must not produce the quarantine line.

        Without this the assertion above is satisfied by logging on every
        failure, which buries the one verdict that needs a human.
        """
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            store.record({"1": FetchRecord(error="http-429")}, IDENT)
        said = " ".join(r.getMessage() for r in caplog.records)
        assert "re-login" not in said and "quarantin" not in said, (
            f"a transient failure produced the quarantine line: {said!r}"
        )

    def test_a_rotatable_quarantine_does_not_demand_a_re_login(self, store, caplog):
        """A strike on a credential something REPLACES without a human -- a
        `sha256:` refresh lineage -- condemns the generation, not the slot,
        and the live client's own rotation lifts it. Telling a person to
        re-login there is a wrong instruction, not a pessimistic one.
        """
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            # `sha256:` == the credential HAS a refresh token to rotate.
            store.record({"1": FetchRecord(error="invalid_grant",
                                           struck_fp="sha256:the-spent-one")},
                         IDENT)
        said = " ".join(r.getMessage() for r in caplog.records)
        assert "rotation clears it" in said, (
            f"a rotatable strike does not name the rotation that lifts it: {said!r}"
        )
        assert "only a re-login" not in said, (
            f"a rotatable strike still presents re-login as the remedy: {said!r}"
        )
        # THE HEDGE IS THE REQUIREMENT, so assert it directly. The line above
        # only rules out the SIBLING branch's wording; "re-login now to restore
        # it" clears it while violating the very thing this test is named for.
        assert "only if it persists" in said, (
            f"a rotatable strike hardened its re-login into a demand: {said!r}"
        )
        assert caplog.records[-1].levelno == logging.WARNING, (
            "a strike the message itself calls possibly-stale escalated to "
            f"{caplog.records[-1].levelname}"
        )
        # SCOPE. Two of the three struck_fp mint sites are idle-only, where no
        # live client rotates anything -- an unscoped promise is wrong there.
        assert "only on the active slot" in said, (
            f"the rotation promise lost its scope: {said!r}"
        )

    def test_an_unbound_quarantine_still_demands_a_re_login(self, store, caplog):
        """THE CONTROL. A row struck with no fingerprint binds
        unconditionally, so softening the sentence there would tell a person
        to wait for a heal that cannot arrive.
        """
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            store.record({"1": FetchRecord(error="invalid_grant")}, IDENT)
        said = " ".join(r.getMessage() for r in caplog.records)
        # Both halves, or neither discriminates: the ROTATION wording also
        # contains "re-login", so a bare `"re-login" in said` passes on both.
        assert "only a re-login" in said, (
            f"an unbound strike stopped naming the only thing that lifts it: {said!r}"
        )
        assert "rotation clears it" not in said, (
            f"an unbound strike promises a rotation that cannot lift it: {said!r}"
        )

    def test_a_credential_with_no_refresh_token_demands_a_re_login(
        self, store, caplog
    ):
        """A BOUND strike that no rotation can lift, so the binding is not the
        question -- rotatability is. ``no_refresh_token`` strikes too
        (PERMANENT_AUTH_ERRORS), and ``credential_fingerprint`` falls back to a
        full-CONTENT hash for a blob with no refresh token, which is truthy.
        Nothing rotates those bytes: only an explicit write replaces them.
        """
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            store.record({"1": FetchRecord(error="no_refresh_token",
                                           struck_fp="sha256-full:deadbeef")},
                         IDENT)
        said = " ".join(r.getMessage() for r in caplog.records)
        assert "only a re-login" in said, (
            f"a content-hash strike was told a rotation may lift it: {said!r}"
        )
        assert "rotation clears it" not in said, (
            f"a credential with no refresh token was promised a rotation: {said!r}"
        )
        # ARG SLOTS, not prose. Hardcoding `rec.error` renders a wrong cause
        # forever and the sentence still reads fine; only the slot catches it.
        slot, ident, err, remedy = caplog.records[-1].args[:4]
        assert (slot, ident, err) == ("1", "a@x.com", "no_refresh_token"), (
            f"the quarantine reported the wrong slot/identity/cause: "
            f"{(slot, ident, err)!r}"
        )

    def test_lifting_a_quarantine_says_so(self, store, caplog):
        """A transition log that speaks in ONE direction reports every
        recovery as a permanent fault: the quarantine is a WARNING and the
        heal was silent.
        """
        store.record({"1": FetchRecord(error="invalid_grant")}, IDENT)
        assert store.entries(IDENT)["1"].token_dead()
        # DROP THE SETUP'S OWN RECORDS. `caplog.records` accumulates over the
        # whole test, not over the `with` block, so without this the strike's
        # own QUARANTINE line satisfies the assertion below.
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="claude-swap"):
            store.clear_dead_token(["1"], IDENT)
        said = " ".join(r.getMessage() for r in caplog.records)
        assert "1" in said and "a@x.com" in said, (
            f"the heal names neither the slot nor the account: {said!r}"
        )
        assert "no longer matches" not in said, (
            "the heal claims a fingerprint comparison it never made -- this "
            "method never reads a credential, and only the collector path "
            f"passes a fingerprint at all: {said!r}"
        )
        assert caplog.records[-1].args[:2] == ("1", "a@x.com"), (
            f"the heal swapped its slot and identity args: "
            f"{caplog.records[-1].args[:2]!r}"
        )
        # DIRECTION. Position, level, args and guard are each pinned; without
        # this the line could announce the opposite and still pass them all.
        assert "out of quarantine" in said, (
            f"the heal announces the wrong direction: {said!r}"
        )

    def test_clearing_an_unstruck_row_stays_quiet(self, store, caplog):
        """THE CONTROL. `clear_dead_token` is called on rows with no strike as
        a matter of course -- every re-login and every add runs it -- so a line
        per call would bury the transitions it exists to show."""
        # A FAILURE HISTORY WITH NO STRIKE separates the two counters: on a
        # virgin row both are 0 and the guard could read either field.
        store.record({"1": FetchRecord(error="http-429")}, IDENT)
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="claude-swap"):
            store.clear_dead_token(["1"], IDENT)
        ours = [r for r in caplog.records if r.name == "claude-swap"]
        assert ours == [], (
            f"a no-op clear announced itself: {[r.getMessage() for r in ours]!r}"
        )

    def test_transient_error_does_not_advance_or_reset(self, store):
        store.record({"1": FetchRecord(error="invalid_grant")}, IDENT)
        store.record({"1": FetchRecord(error="http-429")}, IDENT)  # transient
        # 429 must neither bump nor clear the dead-token tally.
        assert store.entries(IDENT)["1"].auth_dead_strikes == 1

    def test_success_resets_strikes(self, store):
        store.record({"1": FetchRecord(error="invalid_grant")}, IDENT)
        store.record({"1": FetchRecord(error="invalid_grant")}, IDENT)
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        assert store.entries(IDENT)["1"].auth_dead_strikes == 0

    def test_token_dead_at_threshold(self, store):
        assert not store.entries(IDENT)["1"].token_dead()  # no strikes yet
        store.record({"1": FetchRecord(error="invalid_grant")}, IDENT)
        # A single server-confirmed invalid_grant is definitive.
        assert store.entries(IDENT)["1"].token_dead()

    def test_transient_error_alone_never_marks_dead(self, store):
        for _ in range(5):
            store.record({"1": FetchRecord(error="http-429")}, IDENT)
        assert not store.entries(IDENT)["1"].token_dead()

    def test_due_candidate_skips_dead_token(self, store, clock):
        store.record({"1": FetchRecord(error="invalid_grant")}, IDENT)
        store.record({"1": FetchRecord(error="invalid_grant")}, IDENT)
        clock.advance(10_000)  # past any backoff
        entries = store.entries(IDENT)
        assert entries["1"].token_dead()
        # A dead token is never nominated as the alternate to poll.
        assert due_candidate(["1"], entries, clock.now) is None

    def test_due_candidate_refuses_a_struck_row_whose_fingerprint_moved(
        self, store, clock
    ):
        """DELIBERATE. A future reader will see that `due_candidate` asks the
        UNBOUND question and take it for the bug this PR fixed elsewhere. It is
        not: the bound verdict ranges over the live credential AND the slot
        backup, neither of which the store can read, and the only caller has
        already healed every case it could determine. What reaches this line is
        the "could not determine" case, where refusing is what the switcher's
        heal scan relies on to keep the row out of a fetch.

        Passing a fingerprint in here would delete that guard, so this test
        fails if anyone does.
        """
        store.record({"1": FetchRecord(error="invalid_grant",
                                       struck_fp="sha256:the-condemned-one")},
                     IDENT)
        clock.advance(10_000)  # past any backoff
        entry = store.entries(IDENT)["1"]
        # The BOUND question says healed -- and is the wrong one to ask here.
        assert not entry.token_dead(stored_fp="sha256:a-rotated-one"), (
            "premise: a moved fingerprint would lift the bound verdict"
        )
        assert due_candidate(["1"], {"1": entry}, clock.now) is None, (
            "due_candidate stopped refusing a struck row, deleting the guard "
            "switcher._collect_usage_entries leans on for its "
            "could-not-determine case"
        )

    def test_clear_dead_token_lifts_quarantine(self, store):
        store.record({"1": FetchRecord(error="invalid_grant")}, IDENT)
        store.record({"1": FetchRecord(error="invalid_grant")}, IDENT)
        assert store.entries(IDENT)["1"].token_dead()
        store.clear_dead_token(["1"], IDENT)
        entry = store.entries(IDENT)["1"]
        assert entry.auth_dead_strikes == 0
        assert not entry.token_dead()
        assert entry.last_error is None
        assert entry.backoff_until is None

    def test_clear_dead_token_revokes_the_claim_by_default(self, store, clock):
        """The credential-refresh callers (login/add/import) need this: a
        fresh credential fences out any claim still bound to the OLD
        lineage, so a superseded fetch's `record()` can't land — see
        `test_credential_refresh_revokes_an_old_fetch_claim`. Default
        behavior stays unchanged; ``revoke_claim=False`` is the opt-out for
        callers with no credential change to fence (below)."""
        store.reserve(["1"], IDENT, respect_plans=True)
        assert store.entries(IDENT)["1"].claimed(clock.now)
        store.clear_dead_token(["1"], IDENT)
        assert not store.entries(IDENT)["1"].claimed(clock.now)

    def test_clear_dead_token_can_preserve_a_live_claim(self, store, clock):
        """`revoke_claim=False`: a lock-free heal (no credential change, no
        network) must not be able to void a lease it did not issue.

        Measured before this guard existed: a zero-strike row holding a
        live fetch claim (a collector's in-flight lease) had `claimId`
        nulled by `clear_dead_token` unconditionally — reachable from the
        TUI's lock-free 3s `fetch=set()` poll, every tick, with no strikes
        involved at all. `record()` fences its own writes on `claimId`, so
        that silently discarded a concurrent collector's in-flight fetch
        outcome — the engine's own measurement, thrown away by a stale
        read one poll cycle later.

        Control in the same test: strikes/backoff/error state are still
        cleared with the flag off — only the CLAIM is preserved, proving
        the mutator's real job (lifting the quarantine) survives the guard.
        """
        claims = store.reserve(["1"], IDENT, respect_plans=True)
        assert claims, "premise: the reserve won a live claim"
        before = store.entries(IDENT)["1"]
        assert before.auth_dead_strikes == 0, "premise: no strikes"
        assert before.claimed(clock.now), "premise: the claim is live"

        store.clear_dead_token(["1"], IDENT, revoke_claim=False)

        after = store.entries(IDENT)["1"]
        assert after.claimed(clock.now), (
            f"claim_until {before.claim_until!r} -> {after.claim_until!r}: "
            "revoke_claim=False must leave a live claim untouched"
        )
        assert after.claim_until == before.claim_until
        assert store.record(
            {"1": FetchRecord(usage=USAGE)}, IDENT, claims
        ) == {"1"}, "the preserved claim must still fence a real record()"

        # Control: strike/backoff/error state is still cleared with the flag
        # off — the guard narrows the write, it does not disable it.
        store.record({"2": FetchRecord(error="invalid_grant")}, IDENT)
        assert store.entries(IDENT)["2"].token_dead()
        store.clear_dead_token(["2"], IDENT, revoke_claim=False)
        assert not store.entries(IDENT)["2"].token_dead()


class TestReserve:
    """Atomic fetch reservation: eligibility re-checked under the lock."""

    def _stale(self, store, clock, num="1"):
        store.record({num: FetchRecord(usage=USAGE)}, IDENT)
        clock.advance(SERVE_TTL_S + CLAIM_TTL_S + 1)

    def test_reserve_wins_and_stamps(self, store):
        assert set(store.reserve(["1"], IDENT, respect_plans=True)) == {"1"}
        # The stamp is the claim: an immediate second reservation loses —
        # this is the double-fetch race the old read-then-claim flow allowed.
        assert store.reserve(["1"], IDENT, respect_plans=True) == {}
        assert store.reserve(["1"], IDENT, respect_plans=False) == {}

    def test_fresh_entry_not_won(self, store, clock):
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        clock.advance(CLAIM_TTL_S + 1)  # claim expired, entry still fresh
        assert store.reserve(["1"], IDENT, respect_plans=True) == {}

    def test_respect_plans_waits_for_next_poll(self, store, clock):
        self._stale(store, clock)
        store.set_poll_plan({"1": (clock.now + 300.0, 300.0)}, IDENT)
        assert store.reserve(["1"], IDENT, respect_plans=True) == {}
        clock.advance(301)
        assert set(store.reserve(["1"], IDENT, respect_plans=True)) == {"1"}

    def test_overslept_repair_rechecks_current_plan_under_lock(self, store, clock):
        self._stale(store, clock)
        store.set_poll_plan({"1": (clock.now + 86_400.0, 300.0)}, IDENT)
        claims = store.reserve(
            ["1"], IDENT, respect_plans=True, repair_overslept=True
        )
        assert set(claims) == {"1"}

        # A concurrent winner can replace the obsolete plan before this
        # collector reserves again. The locked predicate sees that valid plan
        # and does not let repair mode bypass it.
        assert store.record(
            {"1": FetchRecord(usage=USAGE)},
            IDENT,
            claims,
            {"1": (clock.now + 300.0, 300.0)},
        ) == {"1"}
        clock.advance(SERVE_TTL_S + 1)
        store.set_poll_plan({"1": (clock.now + 300.0, 300.0)}, IDENT)
        assert store.reserve(
            ["1"], IDENT, respect_plans=False, repair_overslept=True
        ) == {}

    def test_scheduler_beats_the_ttl_when_due(self, store, clock):
        # Urgent cadence: a due plan wins even inside the serve TTL for the
        # scheduler; on-demand callers still respect freshness.
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        store.set_poll_plan({"1": (clock.now + 60.0, 60.0)}, IDENT)
        clock.advance(61)
        assert store.reserve(["1"], IDENT, respect_plans=True) == {}
        assert set(store.reserve(["1"], IDENT, respect_plans=False)) == {"1"}

    def test_scheduler_may_fetch_a_not_due_stale_entry(self, store, clock):
        # Escalation semantics: an explicit set bypasses a future nextPollAt
        # when the entry has gone stale.
        self._stale(store, clock)
        store.set_poll_plan({"1": (clock.now + 600.0, 600.0)}, IDENT)
        assert set(store.reserve(["1"], IDENT, respect_plans=False)) == {"1"}

    def test_backoff_blocks_both_modes(self, store, clock):
        store.record({"1": FetchRecord(error="timeout")}, IDENT)
        clock.advance(BACKOFF_BASE_S - 1)  # completed claim gone, backoff still on
        assert store.reserve(["1"], IDENT, respect_plans=True) == {}
        assert store.reserve(["1"], IDENT, respect_plans=False) == {}

    def test_dead_token_never_won(self, store, clock):
        store.record({"1": FetchRecord(error="invalid_grant")}, IDENT)
        clock.advance(TRUST_MAX_AGE_S)  # backoff long gone; quarantine stays
        assert store.reserve(["1"], IDENT, respect_plans=True) == {}
        assert store.reserve(["1"], IDENT, respect_plans=False) == {}

    def test_unknown_row_and_identity_mismatch_win(self, store, clock):
        assert set(store.reserve(["1"], IDENT, respect_plans=True)) == {"1"}
        # Slot reused by a different account: the old row is invisible and
        # replaced, so the new identity is fetch-eligible immediately.
        store.record({"2": FetchRecord(usage=USAGE)}, IDENT)
        other = {"2": ("new@x.com", "org-9")}
        assert set(store.reserve(["2"], other, respect_plans=True)) == {"2"}


class TestAttemptLedger:
    """The hourly attempt cap: refuses reserve() outright, in every caller
    mode, once a row already holds ATTEMPTS_PER_HOUR_MAX attempts inside
    the trailing ATTEMPT_WINDOW_S — independent of backoff/plan state."""

    def _seed(self, store, num, **fields):
        store.path.parent.mkdir(parents=True, exist_ok=True)
        rows = {}
        if store.path.exists():
            rows = json.loads(store.path.read_text(encoding="utf-8")).get(
                "accounts", {}
            )
        row = {"email": IDENT[num][0], "organizationUuid": IDENT[num][1]}
        row.update(fields)
        rows[num] = row
        store.path.write_text(
            json.dumps({"schemaVersion": 2, "accounts": rows}), encoding="utf-8"
        )

    def test_at_cap_blocks_reserve_in_both_modes(self, store, clock):
        now = clock.now
        self._seed(
            store,
            "1",
            fetchedAt=now - SERVE_TTL_S - 1,  # stale
            nextPollAt=now - 1,  # poll-due
            attempts=[now - i * 10 for i in range(usage_store.ATTEMPTS_PER_HOUR_MAX)],
        )
        assert store.reserve(["1"], IDENT, respect_plans=True) == {}
        assert store.reserve(["1"], IDENT, respect_plans=False) == {}

    def test_due_candidate_skips_a_row_at_the_attempt_cap(self, store, clock):
        # due_candidate must not spend the auto engine's one alternate poll on
        # a row reserve() would then refuse outright — the same waste the
        # strike check's own docstring calls out for a struck row.
        now = clock.now
        self._seed(
            store,
            "1",
            fetchedAt=now - SERVE_TTL_S - 1,  # stale, most due
            nextPollAt=now - 1,  # poll-due
            attempts=[now - i * 10 for i in range(usage_store.ATTEMPTS_PER_HOUR_MAX)],
        )
        entries = store.entries(IDENT)
        assert usage_store.due_candidate(["1"], entries, now) is None

        # Control: the refusal tracks the trailing window, not something
        # permanent — once the OLDEST attempt ages past ATTEMPT_WINDOW_S,
        # exactly one slot frees and the row is picked again.
        window = usage_store.ATTEMPT_WINDOW_S
        oldest = now - (usage_store.ATTEMPTS_PER_HOUR_MAX - 1) * 10
        clock.advance(oldest + window + 1 - now)
        entries = store.entries(IDENT)
        assert usage_store.due_candidate(["1"], entries, clock.now) == "1"

    def test_an_aged_out_attempt_frees_a_slot_and_is_recorded(self, store, clock):
        now = clock.now
        window = usage_store.ATTEMPT_WINDOW_S
        attempts = [now - window - 1] + [
            now - i * 10 for i in range(usage_store.ATTEMPTS_PER_HOUR_MAX - 1)
        ]
        self._seed(
            store,
            "1",
            fetchedAt=now - SERVE_TTL_S - 1,
            nextPollAt=now - 1,
            attempts=attempts,
        )
        assert set(store.reserve(["1"], IDENT, respect_plans=True)) == {"1"}
        recorded = json.loads(store.path.read_text(encoding="utf-8"))["accounts"][
            "1"
        ]["attempts"]
        assert now in recorded
        assert all(t > now - window for t in recorded)  # the stale one is pruned


class TestHeaderReading:
    """record_header_reading: a reply's own rate-limit headers, no fetch."""

    @pytest.mark.parametrize(
        "bad_7d_reset",
        [float("inf"), 1790206800000.0, float("nan")],
        ids=["overflow", "ms-epoch-out-of-range", "nan"],
    )
    def test_records_a_reading_without_disturbing_attempts_or_other_windows(
        self, store, clock, bad_7d_reset
    ):
        # A prior real fetch left a per-model (scoped) window, a far-future
        # AIMD-backed-off plan, and its own lastAttemptAt.
        scoped = [{"name": "seven_day_opus", "pct": 33.0}]
        store.record({"1": FetchRecord(usage={**USAGE, "scoped": scoped})}, IDENT)
        last_attempt = clock.now
        far_future = clock.now + 1200.0
        store.set_poll_plan({"1": (far_future, 1200.0)}, IDENT)
        clock.advance(60)

        five_reset_ts = clock.now + 1800.0
        headers = {
            usage_store.USAGE_HEADER_5H_PCT: "0.42",
            usage_store.USAGE_HEADER_5H_RESET: str(five_reset_ts),
            usage_store.USAGE_HEADER_7D_PCT: "0.1",
            # Out of datetime's range (OverflowError), a millisecond epoch
            # that overflows the year field (ValueError: year 58699), and
            # NaN (ValueError) must all fall back to "no reset known" rather
            # than raise into the pin's request path.
            usage_store.USAGE_HEADER_7D_RESET: str(bad_7d_reset),
        }
        assert store.record_header_reading("1", IDENT, headers) is True

        entry = store.entries(IDENT)["1"]
        assert entry.last_good["five_hour"]["pct"] == pytest.approx(42.0)
        assert entry.last_good["seven_day"]["pct"] == pytest.approx(10.0)
        assert "resets_at" not in entry.last_good["seven_day"]
        assert usage_store.parse_reset_ts(
            entry.last_good["five_hour"]["resets_at"]
        ) == pytest.approx(five_reset_ts)
        assert entry.last_good["scoped"] == scoped  # per-model window untouched
        assert entry.fetched_at == clock.now
        assert entry.age_s == 0.0
        assert entry.last_attempt_at == pytest.approx(last_attempt)  # not an attempt
        assert entry.next_poll_at == pytest.approx(far_future)  # not pulled earlier

    def test_no_5h_header_records_nothing(self, store, clock):
        assert store.record_header_reading("1", IDENT, {"x": "1"}) is False
        assert store.entries(IDENT)["1"] == UsageEntry()

    def test_floors_next_poll_at_candidate_max_interval_when_unset(
        self, store, clock
    ):
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)  # no plan set
        headers = {usage_store.USAGE_HEADER_5H_PCT: "0.5"}
        store.record_header_reading("1", IDENT, headers)
        entry = store.entries(IDENT)["1"]
        assert entry.next_poll_at == pytest.approx(clock.now + CANDIDATE_MAX_INTERVAL_S)

    def test_cannot_preempt_the_post_switch_defer_once_the_attempt_is_old(
        self, store, clock
    ):
        # `_replan_new_active`'s defer window relies on this floor
        # (`lastAttemptAt + CANDIDATE_MAX_INTERVAL_S`) landing AFTER its own
        # near-term deadline for a header reading to have any effect. Once
        # the last endpoint attempt is already >= 570s old at switch time,
        # the floor lands at or before that deadline and a header reading
        # cannot push it out: the deferred poll still fires on schedule.
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)  # lastAttemptAt = t0
        clock.advance(600.0)  # the last attempt is now >= 570s old
        deferred = clock.now + POST_SWITCH_REPLAN_DEFER_S
        store.set_poll_plan({"1": (deferred, 180.0)}, IDENT)  # the replan's shape

        headers = {usage_store.USAGE_HEADER_5H_PCT: "0.5"}
        assert store.record_header_reading("1", IDENT, headers) is True
        entry = store.entries(IDENT)["1"]
        assert entry.next_poll_at == pytest.approx(deferred)  # unmoved

    def test_header_reading_inside_the_window_moves_next_poll_past_the_defer(
        self, store, clock
    ):
        # Companion to the case above: when the last endpoint attempt is
        # still recent at switch time (well under 570s old), the floor
        # (`lastAttemptAt + CANDIDATE_MAX_INTERVAL_S`) lands AFTER the
        # replan's own `now + POST_SWITCH_REPLAN_DEFER_S` deadline, so a
        # header reading landing inside the window moves `nextPollAt` past
        # that deadline instead of leaving it unmoved. Characterization only
        # (T1231): this row's own state is correct either way — the defect
        # the review found was autoswitch.py's own predicate reading this
        # shape as a stuck defer, not this store method, so this need not
        # (and does not) read differently before that fix.
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)  # lastAttemptAt = t0
        clock.advance(300.0)  # stale enough to replan, recent enough to matter
        deferred = clock.now + POST_SWITCH_REPLAN_DEFER_S
        store.set_poll_plan({"1": (deferred, 180.0)}, IDENT)  # the replan's shape

        clock.advance(10.0)  # inside the 30s window
        headers = {usage_store.USAGE_HEADER_5H_PCT: "0.5"}
        assert store.record_header_reading("1", IDENT, headers) is True
        entry = store.entries(IDENT)["1"]
        assert entry.next_poll_at > deferred  # pushed past now+30

    def test_does_not_join_the_attempt_ledger(self, store, clock):
        headers = {usage_store.USAGE_HEADER_5H_PCT: "0.5"}
        store.record_header_reading("1", IDENT, headers)
        store.record_header_reading("1", IDENT, headers)
        row = json.loads(store.path.read_text(encoding="utf-8"))["accounts"]["1"]
        assert "attempts" not in row

    def test_skips_a_struck_row(self, store, clock):
        # A row carrying an auth strike must not be refreshed by a header
        # reading: bumping fetchedAt would erase the strike-race doubt
        # (_strike_is_suspected_race) and would let entries() trust the row
        # again at age 0 through the whole backoff. Seeded directly with
        # consecutiveFailures at 0 (a fingerprint-healed strike leaves
        # exactly this shape) so this exercises the ``authDeadStrikes > 0``
        # arm of the skip condition alone — record()-ing a permanent-auth
        # FetchRecord bumps both fields together and would leave that arm
        # untested independent of the consecutiveFailures one below.
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text(json.dumps({
            "schemaVersion": 2,
            "accounts": {
                "1": {
                    "email": IDENT["1"][0],
                    "organizationUuid": IDENT["1"][1],
                    "authDeadStrikes": 1,
                    "consecutiveFailures": 0,
                }
            },
        }), encoding="utf-8")
        before = store.entries(IDENT)["1"]
        headers = {usage_store.USAGE_HEADER_5H_PCT: "0.5"}
        assert store.record_header_reading("1", IDENT, headers) is False
        assert store.entries(IDENT)["1"] == before

    def test_skips_a_failed_row(self, store, clock):
        # The other arm of the OR: a transient endpoint failure alone
        # (authDeadStrikes stays 0 for "timeout") must also skip.
        store.record({"1": FetchRecord(error="timeout")}, IDENT)
        before = store.entries(IDENT)["1"]
        headers = {usage_store.USAGE_HEADER_5H_PCT: "0.5"}
        assert store.record_header_reading("1", IDENT, headers) is False
        assert store.entries(IDENT)["1"] == before


class TestLast429Marker:
    def test_last_429_survives_recovery(self, store, clock):
        # The planner needs "was there a 429 recently?" even after a
        # successful fetch cleared the failure fields.
        store.record(
            {"1": FetchRecord(error="http-429", retry_after_s=0.0)}, IDENT
        )
        t429 = clock.now
        clock.advance(400)
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        entry = store.entries(IDENT)["1"]
        assert entry.consecutive_failures == 0
        assert entry.last_429_at == pytest.approx(t429)

    def test_non_429_failures_leave_the_marker_alone(self, store, clock):
        store.record({"1": FetchRecord(error="timeout")}, IDENT)
        assert store.entries(IDENT)["1"].last_429_at is None


class TestRecent429AcrossHonoredBlock:
    """The AIMD floor/growth keys on "did this token 429 recently?". A 429 with
    an hour-scale Retry-After is honored as one backoff spanning the whole
    block, so there is exactly one stamp and no attempts until it lifts. The
    "recent" test must still be True at the first post-block success — otherwise
    the very cap raise that stops mid-window re-probing also silently disables
    the AIMD growth and the POST_429 floor, and N machines never converge.
    """

    def _recent_429(self, entry: UsageEntry, now: float) -> bool:
        # Mirror the scheduler's gate (switcher._persist_poll_plans). Extracted
        # onto the entry so it can be exercised through the store, which is the
        # only place the last429At/backoff timing interaction is real.
        return entry.recent_429(now)

    def test_recent_429_true_at_first_success_after_hour_block(
        self, store, clock
    ):
        # 429 with a full-hour Retry-After: honored as a single 3600s backoff.
        store.record(
            {"1": FetchRecord(error="http-429", retry_after_s=3600.0)}, IDENT
        )
        before = store.entries(IDENT)["1"]
        # The next attempt can only run once the backoff lifts — advance to the
        # earliest eligible moment, exactly what the engine does.
        clock.advance(before.backoff_until - clock.now)
        # First post-block success is being processed: the pre-fetch snapshot
        # must still count as "recently 429'd" so the plan keeps the floor.
        assert self._recent_429(before, clock.now) is True

    def test_recent_429_false_once_window_truly_elapsed(self, store, clock):
        store.record(
            {"1": FetchRecord(error="http-429", retry_after_s=3600.0)}, IDENT
        )
        before = store.entries(IDENT)["1"]
        # Well past both the backoff and the recency window that follows it.
        # Derived from the honored backoff rather than hardcoded: the window is
        # anchored on when the backoff LIFTS, so it moves with the margin.
        clock.advance(
            (before.backoff_until - clock.now) + usage_store.RECENT_429_WINDOW_S + 1
        )
        assert self._recent_429(before, clock.now) is False

    def test_short_retry_after_recency_still_expires_normally(self, store, clock):
        # A short (Retry-After: 0) block anchors on its (short) 429 backoff and
        # so recency still elapses within a bounded window of the block — the
        # hour-scale anchoring must not leave a short block "recent" forever.
        store.record(
            {"1": FetchRecord(error="http-429", retry_after_s=0.0)}, IDENT
        )
        before = store.entries(IDENT)["1"]
        clock.advance(before.backoff_until - clock.now)  # EDGE_BACKOFF_S later
        assert self._recent_429(before, clock.now) is True  # just lifted
        clock.advance(usage_store.RECENT_429_WINDOW_S)  # a full window on
        assert self._recent_429(before, clock.now) is False

    def test_unrelated_timeout_does_not_re_arm_recency(self, store, clock):
        # Regression: the backoff anchor must fire only while the LIVE backoff is
        # a 429 backoff. A token that 429'd long ago (window fully elapsed) then
        # hits an unrelated timeout gets a fresh backoffUntil but keeps its old
        # last429At; recency must stay False (the timeout is not a 429), or the
        # post-429 floor/urgent-suppression would spuriously re-engage.
        store.record(
            {"1": FetchRecord(error="http-429", retry_after_s=0.0)}, IDENT
        )
        clock.advance(400)
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)  # recover
        clock.advance(usage_store.RECENT_429_WINDOW_S + 5000)  # 429 long gone
        assert self._recent_429(store.entries(IDENT)["1"], clock.now) is False
        store.record({"1": FetchRecord(error="timeout")}, IDENT)  # unrelated
        before = store.entries(IDENT)["1"]
        assert before.last_error == "timeout"
        assert before.last_429_at is not None  # stamp survives, but…
        clock.advance(before.backoff_until - clock.now)  # at the timeout's expiry
        assert self._recent_429(before, clock.now) is False  # …not re-armed


class TestHourScale429FloorEngagesThroughStore:
    """End-to-end through the store: a 429 with an hour-scale Retry-After, then
    the first post-block success, must still yield a post-429-floored plan. This
    is the integration the unit tests (which pass recent_429 directly) can't
    catch — it exercises the last429At/backoff-timing/recent_429 chain the
    scheduler actually runs (switcher._persist_poll_plans).
    """

    def _plan_after_first_success(self, store, clock, legacy_recency: bool):
        from claude_swap import poll_policy

        store.record(
            {"1": FetchRecord(error="http-429", retry_after_s=3600.0)}, IDENT
        )
        before = store.entries(IDENT)["1"]
        clock.advance(before.backoff_until - clock.now)  # earliest eligible
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        after = store.entries(IDENT)["1"]
        if legacy_recency:
            recent = (
                before.last_429_at is not None
                and (clock.now - before.last_429_at)
                < poll_policy.RECENT_429_WINDOW_S
            )
        else:
            recent = before.recent_429(clock.now)
        nxt, interval = poll_policy.plan_after_fetch(
            prev_interval_s=before.poll_interval_s,
            prev_usage=before.last_good,
            new_usage=after.last_good,
            is_active=False,
            threshold=90.0,
            models=(),
            recent_429=recent,
            now=clock.now,
            rng=lambda: 0.5,
        )
        return recent, interval

    def test_floor_engages_at_first_post_block_success(self, store, clock):
        from claude_swap import poll_policy

        recent, interval = self._plan_after_first_success(
            store, clock, legacy_recency=False
        )
        assert recent is True
        assert interval >= poll_policy.POST_429_MIN_INTERVAL_S

    def test_legacy_recency_would_drop_the_floor(self, store, clock):
        # Documents the regression the fix closes: with the old inline recency
        # (measured from the 429 stamp), the first post-block success sees
        # recent_429=False and the POST_429 floor never engages.
        from claude_swap import poll_policy

        recent, interval = self._plan_after_first_success(
            store, clock, legacy_recency=True
        )
        assert recent is False
        assert interval < poll_policy.POST_429_MIN_INTERVAL_S

    def test_repeated_429_episodes_converge_to_the_wide_ceiling(
        self, store, clock
    ):
        # The real convergence dynamic, driven end-to-end through the store:
        # each 429 episode (429 → honored backoff → first post-block success)
        # contributes one AIMD growth step, and successive episodes push the
        # persisted interval up to POST_429_MAX_INTERVAL_S. This is what lets N
        # machines sharing a token back off far enough to fit the budget — and
        # it only works because recent_429 is True at each episode's first
        # success (the fix). Uses short (60s) blocks so the episodes are quick;
        # the growth is independent of the block length.
        from claude_swap import poll_policy

        intervals = []
        for _ in range(6):
            store.record(
                {"1": FetchRecord(error="http-429", retry_after_s=60.0)}, IDENT
            )
            before = store.entries(IDENT)["1"]
            clock.advance(before.backoff_until - clock.now)
            store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
            after = store.entries(IDENT)["1"]
            nxt, interval = poll_policy.plan_after_fetch(
                prev_interval_s=before.poll_interval_s,
                prev_usage=before.last_good,
                new_usage=after.last_good,
                is_active=False,
                threshold=90.0,
                models=(),
                recent_429=before.recent_429(clock.now),
                now=clock.now,
                rng=lambda: 0.5,
            )
            store.set_poll_plan({"1": (nxt, interval)}, IDENT)
            intervals.append(interval)
            clock.advance(10)  # brief gap before the next episode

        assert intervals == sorted(intervals)  # monotonic growth
        assert intervals[-1] == poll_policy.POST_429_MAX_INTERVAL_S  # converged
        # and it climbed strictly while below the ceiling (real AIMD, not a jump)
        assert intervals[0] < intervals[2] < poll_policy.POST_429_MAX_INTERVAL_S


class TestClaimTrustBridge:
    def test_in_flight_claim_keeps_decision_trust(self, store, clock):
        # Reservation loser scenario: the entry is poll-due and past
        # STALE_OK_S, another process just won reserve() and is fetching.
        # The loser must keep trusting last-good for the claim window instead
        # of reading unknown (and e.g. counting an unhealthy tick).
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        store.set_poll_plan({"1": (clock.now + 400.0, 400.0)}, IDENT)
        clock.advance(401)  # poll-due, age > STALE_OK_S
        assert set(store.reserve(["1"], IDENT, respect_plans=True)) == {"1"}
        entry = store.entries(IDENT)["1"]
        assert entry.trust_extended
        assert entry.decision_value() == USAGE
        clock.advance(CLAIM_TTL_S)  # claim expired, no result recorded
        assert store.entries(IDENT)["1"].decision_value() is None


class TestFingerprintBoundStrikes:
    """M3: a dead-token strike binds to the refresh-token fingerprint of the
    POSTed bytes. token_dead() holds only while the stored credential still
    fingerprints to the struck generation — any credential-writing path
    (add, import, switch persist, gate CAS) heals the strike automatically."""

    def _store(self, tmp_path):
        from claude_swap.usage_store import UsageStore
        return UsageStore(tmp_path / "usage.json")

    def _record_invalid_grant(self, store, num="1", fp="fp-dead"):
        from claude_swap.usage_store import FetchRecord
        identities = {num: ("a@example.com", "")}
        claims = store.reserve([num], identities, respect_plans=False)
        store.record(
            {num: FetchRecord(error="invalid_grant", struck_fp=fp)},
            identities, claims,
        )

    def test_strike_stamps_fingerprint(self, tmp_path):
        store = self._store(tmp_path)
        self._record_invalid_grant(store, fp="fp-A")
        entry = store.entries({"1": ("a@example.com", "")}, [])["1"]
        assert entry.auth_dead_strikes == 1
        assert entry.token_dead(stored_fp="fp-A") is True

    def test_strike_unbinds_on_fingerprint_mismatch(self, tmp_path):
        """The stored credential was replaced (new lineage) — the old strike
        no longer condemns the slot."""
        store = self._store(tmp_path)
        self._record_invalid_grant(store, fp="fp-A")
        entry = store.entries({"1": ("a@example.com", "")}, [])["1"]
        assert entry.token_dead(stored_fp="fp-B") is False

    def test_strike_without_fp_binds_unconditionally(self, tmp_path):
        """Legacy rows (no struck fingerprint recorded) keep today's
        behavior: dead until strikes reset."""
        store = self._store(tmp_path)
        self._record_invalid_grant(store, fp=None)
        entry = store.entries({"1": ("a@example.com", "")}, [])["1"]
        assert entry.token_dead(stored_fp="fp-anything") is True


class TestStruckFingerprintHygiene:
    """A new strike must never inherit a stale struckFingerprint from an
    earlier, already-healed strike: a legacy writer (struck_fp=None) binds
    unconditionally, and clearing a quarantine drops the fingerprint too."""

    def test_legacy_strike_overwrites_stale_fingerprint(self, store):
        """I3 rewrite: the ORIGINAL version called ``clear_dead_token``
        between the two strikes, which itself zeroes ``struckFingerprint`` --
        so the asserted state (``None``) already existed before the second
        ``record()`` ran, and the assertion could not tell the overwrite
        under test from that setup step (it passed identically with the
        overwrite guarded out: ``if rec.struck_fp is not None:``). Reaching
        the asserted state ONLY via the second ``record()`` -- no intervening
        clear -- makes the overwrite the sole mechanism that can produce it.
        """
        ident = {"1": ("a@b.c", "")}
        store.record(
            {"1": FetchRecord(error="invalid_grant", struck_fp="sha256:old")},
            ident,
        )
        assert store.entries(ident)["1"].struck_fingerprint == "sha256:old", (
            "premise: a fingerprint is on the row before the legacy strike"
        )
        # legacy writer strikes without a fingerprint -- no clear in between,
        # so only THIS write can change struckFingerprint.
        store.clear_dead_token(["1"], ident)
        # legacy writer strikes without a fingerprint
        store.record({"1": FetchRecord(error="invalid_grant")}, ident)
        entry = store.entries(ident)["1"]
        assert entry.struck_fingerprint is None
        # unconditional binding: differs-from-old-fp must NOT heal it
        assert entry.token_dead(stored_fp="sha256:new")

    def test_a_legacy_restrike_overwrites_a_live_stale_fingerprint(self, store):
        """The same promise, with nothing else nulling the field first.

        `test_legacy_strike_overwrites_stale_fingerprint` calls
        `clear_dead_token` between the two strikes, which already sets
        `struckFingerprint` to None — so a conditional write and an
        unconditional one agree, and the guard mutates green.

        Here the row keeps its old fingerprint right up to the legacy strike.
        A conditional write leaves "sha256:old" in place, and the strike then
        binds to a generation the legacy writer never POSTed: a credential
        matching "sha256:old" would be condemned on someone else's evidence,
        and the one actually struck would read as healed.
        """
        ident = {"1": ("a@b.c", "")}
        store.record(
            {"1": FetchRecord(error="invalid_grant", struck_fp="sha256:old")},
            ident,
        )
        assert store.entries(ident)["1"].struck_fingerprint == "sha256:old"
        # A legacy writer strikes with no fingerprint, on a LIVE row.
        store.record({"1": FetchRecord(error="invalid_grant")}, ident)
        entry = store.entries(ident)["1"]
        assert entry.struck_fingerprint is None, (
            "a legacy strike must bind unconditionally, not inherit the "
            "fingerprint of an earlier, differently-bound strike"
        )
        assert entry.token_dead(stored_fp="sha256:new")

    def test_clear_dead_token_drops_fingerprint(self, store):
        ident = {"1": ("a@b.c", "")}
        store.record(
            {"1": FetchRecord(error="invalid_grant", struck_fp="sha256:old")},
            ident,
        )
        store.clear_dead_token(["1"], ident)
        assert store.entries(ident)["1"].struck_fingerprint is None


class TestStrikeOnlyHeal:
    """C1/I2: ``clear_dead_token(strike_only=True)`` clears the STRIKE only
    -- ``authDeadStrikes``/``struckFingerprint`` -- and leaves the server's
    own throttle state (``consecutiveFailures``/``lastError``/
    ``backoffUntil``) untouched. The five credential-refresh callers keep
    the full clear (``strike_only`` defaults False)."""

    def test_strike_only_preserves_backoff(self, store):
        ident = {"1": ("a@b.c", "")}
        store.record(
            {"1": FetchRecord(error="invalid_grant", retry_after_s=1800.0,
                               struck_fp="sha256:old")},
            ident,
        )
        row = store._read_rows()["1"]
        backoff_before = row["backoffUntil"]
        assert backoff_before is not None
        store.clear_dead_token(["1"], ident, revoke_claim=False,
                                strike_only=True)
        entry = store.entries(ident)["1"]
        assert entry.auth_dead_strikes == 0
        assert entry.struck_fingerprint is None
        assert entry.backoff_until == backoff_before, (
            "strike_only must not touch the server's own throttle deadline"
        )
        assert entry.last_error == "invalid_grant"
        assert entry.consecutive_failures == 1

    def test_default_full_clear_still_wipes_backoff(self, store):
        """The five credential-refresh callers (login/add/import) must keep
        today's full-clear behaviour -- a freshly written credential has no
        history at all, so a stale backoff must not survive it either."""
        ident = {"1": ("a@b.c", "")}
        store.record(
            {"1": FetchRecord(error="invalid_grant", retry_after_s=1800.0,
                               struck_fp="sha256:old")},
            ident,
        )
        store.clear_dead_token(["1"], ident)  # default: strike_only=False
        entry = store.entries(ident)["1"]
        assert entry.backoff_until is None
        assert entry.last_error is None
        assert entry.consecutive_failures == 0

    def test_expected_fingerprint_mismatch_is_a_no_op(self, store, caplog):
        """The TOCTOU re-check: a row whose struckFingerprint moved since
        the caller's lock-free read (a fresh strike, or a different
        collector's own heal, landed in the gap) must be left untouched."""
        ident = {"1": ("a@b.c", "")}
        store.record(
            {"1": FetchRecord(error="invalid_grant", retry_after_s=1800.0,
                               struck_fp="sha256:old")},
            ident,
        )
        # A concurrent writer moved the fingerprint before this heal's lock.
        store.record(
            {"1": FetchRecord(error="invalid_grant", retry_after_s=60.0,
                               struck_fp="sha256:concurrent")},
            ident,
        )
        row_before = dict(store._read_rows()["1"])
        caplog.clear()
        # at_level(INFO) or this asserts NOTHING: bare caplog captures nothing
        # below WARNING, so the absence below would hold however loud the heal.
        with caplog.at_level(logging.INFO, logger="claude-swap"):
            store.clear_dead_token(
                ["1"], ident, revoke_claim=False, strike_only=True,
                expected_fingerprints={"1": "sha256:old"},  # the STALE read
            )
        row_after = store._read_rows()["1"]
        assert row_after == row_before, (
            "a stale-read heal must not overwrite a row that changed under it"
        )
        # THE THIRD OUTCOME. Struck-but-REFUSED is neither of the two the heal
        # line splits on, and a line here claims a transition that did not
        # happen -- the exact defect class this wording change exists to remove.
        ours = [r for r in caplog.records if r.name == "claude-swap"]
        assert ours == [], (
            "a refused heal announced a heal that did not happen: "
            f"{[r.getMessage() for r in ours]!r}"
        )

    def test_expected_fingerprint_match_still_heals(self, store):
        ident = {"1": ("a@b.c", "")}
        store.record(
            {"1": FetchRecord(error="invalid_grant", retry_after_s=1800.0,
                               struck_fp="sha256:old")},
            ident,
        )
        store.clear_dead_token(
            ["1"], ident, revoke_claim=False, strike_only=True,
            expected_fingerprints={"1": "sha256:old"},  # matches
        )
        entry = store.entries(ident)["1"]
        assert entry.auth_dead_strikes == 0
        assert entry.struck_fingerprint is None


class TestHealPreservesTrustExtended:
    """I2: the heal must not itself flip a decision-trusted entry to
    unknown. Before the C1 fix, a struck entry's ``trust_extended`` rode on
    ``consecutiveFailures``/``lastError``/``backoffUntil`` -- exactly the
    fields the unconditional clear wiped -- so a stale-but-trusted entry
    flipped to unknown purely from observing a healed fingerprint, which
    ``autoswitch.py`` counts toward ``_unhealthy_ticks`` and a real
    failover."""

    def test_strike_only_heal_keeps_a_stale_entry_decision_trusted(
        self, store, clock
    ):
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        # The strike is itself the failure state keeping this entry trusted
        # past STALE_OK_S (consecutive_failures > 0).
        store.record(
            {"1": FetchRecord(error="invalid_grant", retry_after_s=1800.0,
                               struck_fp="sha256:old")},
            IDENT,
        )
        clock.advance(STALE_OK_S + 1)
        pre = store.entries(IDENT)["1"]
        assert pre.age_s > STALE_OK_S
        assert pre.trust_extended, "premise: the strike itself trusts it"
        assert pre.decision_value() == USAGE

        store.clear_dead_token(["1"], IDENT, revoke_claim=False,
                                strike_only=True)
        post = store.entries(IDENT)["1"]
        assert post.trust_extended, (
            "the heal flipped a decision-trusted entry to unknown -- "
            "autoswitch.py counts this toward a failover"
        )
        assert post.decision_value() == USAGE

    def test_full_clear_heal_does_flip_it_to_unknown(self, store, clock):
        """Documents the CONTRASTING behaviour of the default (non-strike-
        only) clear on the same setup, so the two tests together show the
        fix is exactly the ``strike_only`` axis, not a side effect."""
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        store.record(
            {"1": FetchRecord(error="invalid_grant", retry_after_s=1800.0,
                               struck_fp="sha256:old")},
            IDENT,
        )
        clock.advance(STALE_OK_S + 1)
        store.clear_dead_token(["1"], IDENT)  # full clear
        post = store.entries(IDENT)["1"]
        assert not post.trust_extended
        assert post.decision_value() is None
