"""Tests for the per-account usage store."""

from __future__ import annotations

import json

import pytest

from claude_swap import oauth, usage_store
from claude_swap.usage_store import (
    BACKOFF_BASE_S,
    BACKOFF_CAP_S,
    CLAIM_TTL_S,
    RATE_LIMIT_TRUST_MAX_AGE_S,
    SERVE_TTL_S,
    STALE_OK_S,
    TRUST_MAX_AGE_S,
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
    """Deliberate staleness (failure state, scheduler cadence) stays trusted."""

    def test_in_backoff_past_stale_ok_is_still_trusted(self, store, clock):
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        clock.advance(STALE_OK_S)
        store.record(
            {"1": FetchRecord(error="http-429", retry_after_s=480.0)}, IDENT
        )
        clock.advance(60)
        entry = store.entries(IDENT)["1"]
        assert entry.age_s > STALE_OK_S
        assert entry.in_backoff(clock.now)
        assert entry.trust_extended
        assert entry.decision_value() == USAGE

    def test_failure_state_after_backoff_expiry_is_still_trusted(self, store, clock):
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        clock.advance(60)
        store.record({"1": FetchRecord(error="timeout")}, IDENT)
        clock.advance(BACKOFF_BASE_S + STALE_OK_S)  # backoff long expired
        entry = store.entries(IDENT)["1"]
        assert not entry.in_backoff(clock.now)
        assert entry.decision_value() == USAGE

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

    def test_trust_ceiling_wins_over_non_429_failure_state(self, store, clock):
        # A non-429 failure (timeout/network) past the general ceiling reads as
        # unknown: such an error is no evidence the last_good still holds, so
        # the unknown-path machinery must take back over.
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)
        store.record({"1": FetchRecord(error="timeout")}, IDENT)
        clock.advance(TRUST_MAX_AGE_S + 1)
        store.record({"1": FetchRecord(error="timeout")}, IDENT)
        entry = store.entries(IDENT)["1"]
        assert entry.consecutive_failures == 2
        assert entry.decision_value() is None

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

    def test_429_staleness_trusted_until_window_reset(self, store, clock):
        # A usage-endpoint 429 throttles polling; it does NOT move the account's
        # real windows. Usage only rises within a window (monotone until reset),
        # so last_good is a valid lower bound — trust it up to its reset, as long
        # as that reset is within the client-side ceiling. Reset inside the
        # ceiling → trusted right up to it, past the general TRUST_MAX_AGE_S.
        reset_ahead = (TRUST_MAX_AGE_S + RATE_LIMIT_TRUST_MAX_AGE_S) / 2  # < ceiling
        usage = self._usage_resetting_at(clock, reset_ahead)
        store.record({"1": FetchRecord(usage=usage)}, IDENT)
        store.record({"1": FetchRecord(error="http-429")}, IDENT)
        clock.advance(TRUST_MAX_AGE_S + 1)  # past the general ceiling...
        store.record({"1": FetchRecord(error="http-429")}, IDENT)
        entry = store.entries(IDENT)["1"]
        assert entry.trust_extended
        assert entry.decision_value() == usage  # ...but before the reset

    def test_429_staleness_expires_at_window_reset(self, store, clock):
        # Once the window has reset, usage is zeroed and last_good is obsolete —
        # it reads as unknown so the unknown-path machinery takes over. This is
        # the natural, data-driven bound (no fixed clock): trust ends exactly
        # when the measured value can no longer hold.
        usage = self._usage_resetting_at(clock, 600.0)  # resets in 10 min
        store.record({"1": FetchRecord(usage=usage)}, IDENT)
        store.record({"1": FetchRecord(error="http-429")}, IDENT)
        clock.advance(601.0)  # past the window reset
        store.record({"1": FetchRecord(error="http-429")}, IDENT)
        entry = store.entries(IDENT)["1"]
        assert entry.decision_value() is None
        assert entry.last_good == usage  # display still sees it

    def test_429_staleness_without_reset_info_falls_back_to_ceiling(
        self, store, clock
    ):
        # Older stored data may carry no resets_at. Fall back to the fixed
        # rate-limit ceiling so such an entry still can't be trusted forever.
        store.record({"1": FetchRecord(usage=USAGE)}, IDENT)  # no resets_at
        store.record({"1": FetchRecord(error="http-429")}, IDENT)
        clock.advance(TRUST_MAX_AGE_S + 1)
        store.record({"1": FetchRecord(error="http-429")}, IDENT)
        assert store.entries(IDENT)["1"].decision_value() == USAGE  # within
        clock.advance(RATE_LIMIT_TRUST_MAX_AGE_S)
        store.record({"1": FetchRecord(error="http-429")}, IDENT)
        assert store.entries(IDENT)["1"].decision_value() is None  # past cap


class TestRateLimitTrustBounds:
    """The 429-stale trust must be bounded even with reset metadata present:
    (a) clamped to a client-side ceiling so a far-future/malformed resets_at
    can't grant indefinite trust, (b) keyed on the EARLIEST future reset (any
    relevant window reset invalidates the snapshot), and (c) robust to partial
    metadata (a window missing resets_at must not let a longer window's reset
    override the ceiling).
    """

    def _usage(self, clock, five_h_ahead, seven_d_ahead):
        from datetime import datetime, timezone

        def iso(ahead):
            if ahead is None:
                return None
            return (
                datetime.fromtimestamp(clock.now + ahead, tz=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )

        five = {"pct": 25.0}
        if five_h_ahead is not None:
            five["resets_at"] = iso(five_h_ahead)
        seven = {"pct": 10.0}
        if seven_d_ahead is not None:
            seven["resets_at"] = iso(seven_d_ahead)
        return {"five_hour": five, "seven_day": seven}

    def test_far_future_reset_is_clamped_to_the_ceiling(self, store, clock):
        # A malformed/far-future resets_at (year 2099) must NOT grant unbounded
        # trust: the client-side ceiling caps it.
        far = 10 * 365 * 24 * 3600.0  # ~10 years
        usage = self._usage(clock, far, far)
        store.record({"1": FetchRecord(usage=usage)}, IDENT)
        store.record({"1": FetchRecord(error="http-429")}, IDENT)
        clock.advance(RATE_LIMIT_TRUST_MAX_AGE_S + 1)
        store.record({"1": FetchRecord(error="http-429")}, IDENT)
        # past the ceiling → no longer trusted, despite the far-future reset
        assert store.entries(IDENT)["1"].decision_value() is None

    def test_trust_keys_on_earliest_future_reset(self, store, clock):
        # 5h resets soon, 7d resets far ahead. The snapshot is invalid once the
        # SOONER window rolls over (usage zeroes there), so trust must end at the
        # earliest reset, not the latest.
        usage = self._usage(clock, 600.0, 100 * 3600.0)  # 5h: 10min, 7d: far
        store.record({"1": FetchRecord(usage=usage)}, IDENT)
        store.record({"1": FetchRecord(error="http-429")}, IDENT)
        clock.advance(601.0)  # past the 5h reset, long before the 7d one
        store.record({"1": FetchRecord(error="http-429")}, IDENT)
        assert store.entries(IDENT)["1"].decision_value() is None

    def test_partial_metadata_still_bounded_by_ceiling(self, store, clock):
        # 5h has NO resets_at (a shape the server actually sends); 7d resets far
        # ahead. The missing-reset window must not let the far 7d reset grant
        # near-unbounded trust — the ceiling still applies.
        usage = self._usage(clock, None, 100 * 3600.0)
        store.record({"1": FetchRecord(usage=usage)}, IDENT)
        store.record({"1": FetchRecord(error="http-429")}, IDENT)
        clock.advance(RATE_LIMIT_TRUST_MAX_AGE_S + 1)
        store.record({"1": FetchRecord(error="http-429")}, IDENT)
        assert store.entries(IDENT)["1"].decision_value() is None

    def test_ceiling_wins_when_reset_is_beyond_it(self, store, clock):
        # Reset farther out than the ceiling: trusted up to the ceiling, then
        # unknown — the ceiling, not the reset, is the bound.
        usage = self._usage(
            clock, RATE_LIMIT_TRUST_MAX_AGE_S * 3, RATE_LIMIT_TRUST_MAX_AGE_S * 3
        )
        store.record({"1": FetchRecord(usage=usage)}, IDENT)
        store.record({"1": FetchRecord(error="http-429")}, IDENT)
        clock.advance(RATE_LIMIT_TRUST_MAX_AGE_S - 60)  # just inside the ceiling
        store.record({"1": FetchRecord(error="http-429")}, IDENT)
        assert store.entries(IDENT)["1"].decision_value() == usage
        clock.advance(120)  # now just past the ceiling
        store.record({"1": FetchRecord(error="http-429")}, IDENT)
        assert store.entries(IDENT)["1"].decision_value() is None


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

    def test_the_cap_sits_inside_the_trust_it_relies_on(self):
        """The cap has a floor test and no ceiling; the ceiling is the invariant.

        `RETRY_AFTER_FLOOR_CAP_S`'s own comment justifies the 429-only margin
        with "a 4500s wait sits comfortably inside its own trust"
        (RATE_LIMIT_TRUST_MAX_AGE_S = 7200). Nothing asserted it. Measured on
        this tree by mutating the constant and running the full suite, the
        inequality below admits `[4500, 7200]` — 4499 fails 12, 7201 fails 5
        (re-measured 2026-08-03: this PR added four more tests that also bind
        the constant since the "1" was first measured).

        The inequality is NOT what bounds blind time. It compares two
        constants; raising the cap to 7200 satisfies it while, at an ask of
        6300s or more (where the wait itself reaches the raised cap), more
        than doubling the blind window over consecutive blocks (6300s ->
        14400s, measured). The IDENTITY assertion below is what actually
        stops that drift, and it is the reason this test still fails at
        7200. See
        `test_consecutive_blocks_go_blind_because_fetchedAt_only_moves_on_success`.

        That leaves 2700s of slack in which the constant can drift silently, so
        the arithmetic identity its comment states ("40 of 41 observed blocks
        opened at exactly 3600, and 3600 + 900 = this" — re-measured
        2026-08-03) is pinned outright below. The inequality stays as the
        invariant that explains WHY the margin is 429-only.

        The same bound neutralises `Retry-After: inf`, which reaches
        `min(inf + 900, cap)` and is finite only because of it.
        """
        assert usage_store.RETRY_AFTER_FLOOR_CAP_S == (
            3600.0 + usage_store.RETRY_AFTER_MARGIN_S
        ), (
            f"cap {usage_store.RETRY_AFTER_FLOOR_CAP_S} is no longer the "
            "measured block (3600s) plus the margin — the inequality below "
            "admits up to 7200, so nothing else would catch the drift"
        )
        assert (
            usage_store.RETRY_AFTER_FLOOR_CAP_S
            <= usage_store.RATE_LIMIT_TRUST_MAX_AGE_S
        ), (
            f"cap {usage_store.RETRY_AFTER_FLOOR_CAP_S} exceeds the 429 trust "
            f"ceiling {usage_store.RATE_LIMIT_TRUST_MAX_AGE_S}, so a single "
            "header can park a row past the moment its own data goes unknown"
        )
        # And the wait it produces stays inside that ceiling for any ask.
        for ask in (3600.0, 4500.0, 50_000.0, 86_400.0, float("inf")):
            wait = usage_store._failure_backoff_s(1, ask, rate_limited=True)
            assert wait <= usage_store.RATE_LIMIT_TRUST_MAX_AGE_S, (
                f"ask {ask} produced a {wait}s wait, past the trust ceiling"
            )

    def test_each_arm_is_bounded_by_the_ceiling_its_own_trust_uses(self):
        """A non-429 park must never outlast TRUST_MAX_AGE_S, its own ceiling.

        `entries()` reads a non-429 row unknown once `TRUST_MAX_AGE_S` (3600s)
        elapses past the last success — that is the ceiling this arm's trust
        actually uses. Before this fix, the PARK BOUND capped every ask at
        `RETRY_AFTER_FLOOR_CAP_S` (4500s) regardless of which arm produced it,
        so a non-429 ask above 3600 parked the row past its own trust: blind
        (un-pollable AND unknown) for up to 900s — a regression this PR
        introduced against upstream/main, where `RETRY_AFTER_FLOOR_CAP_S` was
        3600, identical to `TRUST_MAX_AGE_S`, so the blind window was always
        0. The 429 arm keeps `RETRY_AFTER_FLOOR_CAP_S`, correctly inside its
        own ceiling `RATE_LIMIT_TRUST_MAX_AGE_S` (7200s).
        """
        for ask in (3601.0, 4500.0, 7200.0, 86_400.0, float("inf")):
            wait = usage_store._failure_backoff_s(1, ask, rate_limited=False)
            assert wait <= usage_store.TRUST_MAX_AGE_S, (
                f"non-429 ask {ask} produced a {wait}s park, past its own "
                f"trust ceiling {usage_store.TRUST_MAX_AGE_S}s — blind for "
                f"{wait - usage_store.TRUST_MAX_AGE_S:.0f}s"
            )
        for ask in (4500.0, 7200.0, 50_000.0, 86_400.0, float("inf")):
            wait = usage_store._failure_backoff_s(1, ask, rate_limited=True)
            assert wait <= usage_store.RATE_LIMIT_TRUST_MAX_AGE_S, (
                f"429 ask {ask} produced a {wait}s park, past the 429 trust "
                f"ceiling {usage_store.RATE_LIMIT_TRUST_MAX_AGE_S}s"
            )

    def test_a_soon_resetting_window_can_end_trust_before_the_429_wait_releases(
        self, store, clock
    ):
        """The other half of the bound: `min(earliest reset, age-ceiling)`.

        `test_the_cap_sits_inside_the_trust_it_relies_on` only pins the
        age-ceiling half (RATE_LIMIT_TRUST_MAX_AGE_S = 7200) — it never gives
        `last_good` a `resets_at`, so `_earliest_reset` is always None there
        and only the ceiling can bind. Trust actually ends at
        `min(earliest reset, fetched_at + ceiling)`, and a 5h window that
        resets sooner than that ends it first.

        Retry-After 3600 -> a 429 wait released at +4500s (measured in
        `test_hour_scale_retry_after_honored`). Here the 5h window resets at
        +1800s, well before that release: the row goes untrusted while still
        in backoff (un-pollable AND unknown at once) — a blind gap that
        exists and is bounded, not the "sits comfortably inside its own
        trust" the old comment claimed.

        The reset is fixed at +1800s, not +3600s: at +3600s the reset lands
        exactly on `TRUST_MAX_AGE_S`, where this test's own branch (the 429
        trust bound, `_rate_limited_trust_ok`) and the general age-ceiling
        branch (`age_s <= TRUST_MAX_AGE_S`) give identical answers at all
        three instants this test checks — a mutation that routes 429 rows
        through the general branch (`if row.get("lastError") == "http-429"`
        -> `if False`) survives here even though it kills 8 tests elsewhere.
        +1800 makes the two branches disagree (confirmed: MUT-D reads
        `decision_value() == usage` at t0+1801 instead of `None`), so this
        test now actually depends on the 429-specific trust path it exists
        to pin.
        """
        from datetime import datetime, timezone

        def iso(ahead):
            return (
                datetime.fromtimestamp(clock.now + ahead, tz=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )

        usage = {
            "five_hour": {"pct": 25.0, "resets_at": iso(1800.0)},
            "seven_day": {"pct": 10.0, "resets_at": iso(100 * 3600.0)},
        }
        # Schema gotcha: the window key is "pct", not "utilization" — confirm
        # the fixture actually produces relevant windows before trusting
        # anything measured against it.
        assert oauth.relevant_windows(usage, ()) != []

        store.record({"1": FetchRecord(usage=usage)}, IDENT)
        store.record(
            {"1": FetchRecord(error="http-429", retry_after_s=3600.0)}, IDENT
        )
        wait = usage_store._failure_backoff_s(1, 3600.0, rate_limited=True)
        assert wait == pytest.approx(4500.0)

        clock.advance(1799.0)  # just before the 5h reset
        entry = store.entries(IDENT)["1"]
        assert entry.in_backoff(clock.now)
        assert entry.decision_value() == usage

        clock.advance(2.0)  # just past the 5h reset, still well inside backoff
        entry = store.entries(IDENT)["1"]
        assert entry.in_backoff(clock.now)  # still can't be re-polled...
        assert entry.decision_value() is None  # ...and already unknown

        clock.advance(wait - 1801.0)  # past the wait's release
        entry = store.entries(IDENT)["1"]
        assert not entry.in_backoff(clock.now)
        assert entry.decision_value() is None

    def test_consecutive_blocks_go_blind_because_fetchedAt_only_moves_on_success(
        self, store, clock
    ):
        """The blind gap is bounded PER BLOCK, never across a chain of them.

        The 429 comment presents the un-pollable-and-unknown window as caused
        by "a window that resets before the ceiling". That is one way in. The
        age-ceiling half opens the same gap with NO early reset at all, because
        `record()` writes `fetchedAt` only when `rec.error is None`
        (usage_store.py, the success branch) — a chain of failed blocks never
        refreshes it, so trust keeps expiring against the FIRST success while
        each new block adds another full wait. Driven through the real
        `store.record()`/`store.entries()` round trip, not a hand-rolled
        stand-in, so a regression in the success-only write actually fails
        this test.

        Measured here with far-future resets only, so `_earliest_reset` can
        never bind and only the ceiling can:

            block 1  wait [    0,  4500]  trust ends 7200  blind      0s
            block 2  wait [ 4500,  9000]  trust ends 7200  blind   1800s
            block 3  wait [ 9000, 13500]  trust ends 7200  blind   4500s

        By block 3 the row is blind for the ENTIRE wait. `cap <= TRUST` says
        nothing about this: it bounds ONE wait against the ceiling, and the
        ceiling does not move.

        WHY THIS IS A TEST AND NOT A COMMENT FIX. The inequality in
        `test_the_cap_sits_inside_the_trust_it_relies_on` admits [4500, 7200],
        and at 7200 an ask of 6300s or more drives the wait itself to 7200,
        taking the same three blocks from 6300s to 14400s blind — 2.3x. What
        actually stops that drift is the IDENTITY assertion
        (`cap == 3600 + MARGIN`) in that same test, not the inequality the
        comment reasons from. Pin the consequence directly so the bound is
        argued from blind time rather than from a constant comparison that
        does not imply it.
        """
        import datetime

        def iso(t):
            return (
                datetime.datetime.fromtimestamp(t, tz=datetime.timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )

        far = 10 ** 9
        last_good = {
            "five_hour": {"pct": 25.0, "resets_at": iso(far)},
            "seven_day": {"pct": 10.0, "resets_at": iso(far)},
        }
        # NON-VACUITY: with an empty window list every trust question answers
        # the same way and the test proves nothing. The schema key is `pct`,
        # not `utilization` — a probe using the wrong one returned [] here and
        # read as green.
        assert oauth.relevant_windows(last_good, ()) != []
        assert usage_store._earliest_reset(last_good) is not None

        # ONE success establishes fetchedAt; every record() after this is a
        # 429 failure, so a chain of them must never move it again.
        store.record({"1": FetchRecord(usage=last_good)}, IDENT)
        fetched_at = store.entries(IDENT)["1"].fetched_at

        blind_per_block = []
        for _ in range(3):
            store.record(
                {"1": FetchRecord(error="http-429", retry_after_s=3600.0)}, IDENT
            )
            entry = store.entries(IDENT)["1"]
            assert entry.fetched_at == fetched_at, (
                "a failed record() moved fetchedAt — the chain premise this "
                "test pins no longer holds"
            )
            assert entry.backoff_until is not None
            block_end = entry.backoff_until
            wait = block_end - clock.now

            # Ask the REAL read model at each second-boundary of this block.
            blind = 0.0
            while clock.now < block_end:
                if store.entries(IDENT)["1"].decision_value() is None:
                    blind = block_end - clock.now
                    break
                clock.advance(60.0)
            blind_per_block.append(blind)
            clock.advance(max(block_end - clock.now, 0.0))

        assert wait == pytest.approx(4500.0)
        assert blind_per_block[0] == 0.0, (
            "the first block is supposed to sit inside its trust — if this "
            "fires, the single-block claim itself is wrong"
        )
        assert blind_per_block[1] > 0.0, (
            "a second consecutive block must show the gap the comment "
            "attributes only to an early reset"
        )
        assert blind_per_block[2] == pytest.approx(wait), (
            f"by the third block the row should be blind for the whole wait; "
            f"got {blind_per_block[2]}"
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

    def test_park_bound_blind_window_equals_age_at_failure(self, tmp_path):
        """PIN, not fix: the PARK BOUND caps the park, never the blind window.

        The PARK BOUND (`asked = min(asked, ceiling ...)`, right below this
        test's target) compares a `now`-relative duration (`asked`) against a
        `fetchedAt`-relative ceiling (`TRUST_MAX_AGE_S` /
        `RATE_LIMIT_TRUST_MAX_AGE_S`). Those only agree when the row was
        already fresh (age 0) at the moment it failed — round-7 review found
        an earlier comment here wrongly claimed this bound closed the gap in
        general ("the blind window was always 0"); it does not, on either
        arm, and this is pre-existing upstream behaviour left open
        (documented, not fixed, in this PR — see the PARK BOUND comment).

        Driven end-to-end through the real `store.record()` /
        `entries().decision_value()`, not a hand-rolled stand-in, on the
        non-429 arm where the park cap equals TRUST_MAX_AGE_S (3600)
        exactly, so the gap is the whole story rather than diluted by 429
        trust's extra slack. `age_at_fail=0` is the CONTROL: no gap.

        The non-429 rows below are also the CONTROL for the 429-arm rows
        that follow: identical harness, only `error` differs, so any drift
        here would show the probe itself moved rather than the arm under
        test. On the 429 arm (`RETRY_AFTER_FLOOR_CAP_S` 4500 vs the non-429
        arm's `TRUST_MAX_AGE_S` 3600, both bounded by
        `RATE_LIMIT_TRUST_MAX_AGE_S` 7200) the identity `blind ==
        age_at_fail` does NOT hold -- the cap and the trust ceiling are
        different constants there, so
        `blind = age_at_fail - (ceiling - park) = age_at_fail - 2700`. This
        PR raises the 429 cap 3600 -> 4500, which moves the 429 blind onset
        900s earlier (age 3600 -> 2700) and adds a flat +900s at every age
        past that, compared to upstream. See the PARK BOUND comment.
        """
        IDENT_1 = {"1": ("a@example.com", "")}

        def blind_window(age_at_fail: float, ask: float, error: str = "http-500") -> float:
            clock = FakeClock()
            store = UsageStore(
                tmp_path / f"cache-{error}-{age_at_fail}-{ask}", clock=clock
            )
            store.record({"1": FetchRecord(usage={"five_hour": {"pct": 1.0}})}, IDENT_1)
            clock.advance(age_at_fail)
            store.record(
                {"1": FetchRecord(error=error, retry_after_s=ask)}, IDENT_1
            )
            park_end = store.entries(IDENT_1)["1"].backoff_until
            assert park_end is not None
            blind_start = None
            t = clock.now
            while t < park_end:
                clock.now = t
                entry = store.entries(IDENT_1)["1"]
                if entry.in_backoff(clock.now) and entry.decision_value() is None:
                    blind_start = t
                    break
                t += 1.0
            clock.now = park_end
            return 0.0 if blind_start is None else park_end - blind_start

        # (age_at_fail, ask, expected blind window) -- non-429 arm, also the
        # CONTROL for the 429-arm cases below.
        cases = [
            (0.0, 5000.0, 0.0),  # CONTROL: fresh at failure, no gap
            (1.0, 5000.0, 1.0),
            (120.0, 5000.0, 120.0),
            (300.0, 5000.0, 300.0),
            (1800.0, 5000.0, 1800.0),
            (3599.0, 5000.0, 3599.0),
            (300.0, 4000.0, 300.0),
            (300.0, 3600.0, 300.0),
            (300.0, 600.0, 0.0),  # park itself (600) is short: never blind
        ]
        for age_at_fail, ask, expected in cases:
            blind = blind_window(age_at_fail, ask)
            assert blind == pytest.approx(expected, abs=2.0), (
                f"age@fail={age_at_fail:.0f} ask={ask:.0f}: blind window "
                f"{blind:.0f}s, expected {expected:.0f}s"
            )

        # 429 ARM (rate_limited) -- `blind = age_at_fail - 2700`, clamped to
        # [0, park]. `ask=5000` (not 3600) is deliberate: at ask=3600 the
        # margin-adjusted ask (4500) already sits exactly on
        # RETRY_AFTER_FLOOR_CAP_S, so the row would read the same whether the
        # cap were 4500 or 7200 -- an ask above 3600 is needed for the cap to
        # actually bind and for mutation M5 (cap -> 7200) to move this test.
        rate_limited_cases = [
            (0.0, 5000.0, 0.0),  # CONTROL: fresh at failure, no gap
            (2701.0, 5000.0, 1.0),
            (3600.0, 5000.0, 900.0),
            (5000.0, 5000.0, 2300.0),
        ]
        for age_at_fail, ask, expected in rate_limited_cases:
            blind = blind_window(age_at_fail, ask, error="http-429")
            assert blind == pytest.approx(expected, abs=2.0), (
                f"[429 arm] age@fail={age_at_fail:.0f} ask={ask:.0f}: blind "
                f"window {blind:.0f}s, expected {expected:.0f}s"
            )


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
        ident = {"1": ("a@b.c", "")}
        store.record(
            {"1": FetchRecord(error="invalid_grant", struck_fp="sha256:old")},
            ident,
        )
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
