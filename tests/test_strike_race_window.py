"""A quarantine with no exit, enterable by a race.

`_row_eligible` refuses a struck row and `record` clears the strike only on a
success, so a quarantined slot can never reach the fetch that would prove it
alive. The strike can be a race artifact: not from the consume gate (both
POST sites hold `.consume-N.lock` now), but from a concurrent Claude Code or a
sibling machine rotating the same lineage, outside every lock cswap holds. The dead direction is pinned first and deliberately: this guard
trades a false alarm for a silent failure if it is one line too wide.
"""
from __future__ import annotations

import pytest

from claude_swap.usage_store import (
    AUTH_DEAD_STRIKES,
    BACKOFF_BASE_S,
    FetchRecord,
    UsageEntry,
    UsageStore,
    _row_eligible,
)

IDENT = {"1": ("a@x.com", "")}

#: The width the doubt used to be bounded by. It bounds nothing now, so the
#: cases below carry it themselves: a gap this wide is what USED to end the
#: doubt, and each one asserts that it no longer does.
WIDE_GAP_S = 600.0


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


def _row(fetched_at, struck_at, strikes=AUTH_DEAD_STRIKES):
    # `lastAttemptAt` rides along at the same value: it is what a real row
    # looks like right after the strike, and it must NOT be what decides.
    return {"authDeadStrikes": strikes, "fetchedAt": fetched_at,
            "lastAttemptAt": struck_at, "struckAt": struck_at}


def test_a_sibling_machines_rotation_is_doubted_however_long_ago_it_was():
    """MEASURED on a three-machine fleet: one host quarantined a HEALTHY
    account and stayed there.

        fetchedAt  19:36     the last time this host's copy answered
        struckAt   21:26     one invalid_grant, 6600s later
        strikes    1
        the other two hosts polled the same account successfully all night

    The credential was not dead — a sibling had rotated the lineage, so this
    host's copy was simply the spent predecessor. That is the racer this
    window's own comment names, and a sibling rotates on ITS schedule: hours,
    not minutes. A 600s width therefore fails in exactly the case it was
    written for.

    THE COUNT IS WHAT BOUNDS THE COST, as the constant's comment already says.
    Doubt permits one POST that returns a verdict: it succeeds and `record`
    zeroes the count, or it lands a second strike no window excuses. One extra
    request, once, and the fleet's own answer decides which.
    """
    hours = 6_600.0
    entry = UsageEntry(auth_dead_strikes=AUTH_DEAD_STRIKES,
                       fetched_at=1_000.0, struck_at=1_000.0 + hours)
    assert not entry.token_dead(), (
        "a first strike %.0fs after a success stayed quarantined; on a shared "
        "account that gap is a sibling's rotation, not a dead lineage" % hours)
    # PAST THE CLAIM WINDOW, or this asserts about `_live_claim` instead: the
    # helper stamps `lastAttemptAt` with the strike, and a `now` one second
    # later reads as another collector's lease still running.
    assert _row_eligible(_row(1_000.0, 1_000.0 + hours),
                         now=1_000.0 + hours + 3_600,
                         respect_plans=False), (
        "the doubted row never reaches the fetch that would settle it")


# --- the dead direction: a real quarantine must survive all of this ---

@pytest.mark.parametrize("fetched,struck,why", [
    (None, 2_000.0, "never succeeded, so nothing says it was ever alive"),
    (2_000.0, None, "no strike time recorded (legacy row), so no gap exists"),
    # THE TWO GAP CASES MOVED TO THE OTHER SIDE, and that is the change: a
    # first strike after ANY prior success is doubted now, because the racer
    # this guard names is a sibling machine and a sibling rotates on its own
    # schedule. What still bounds the cost is the COUNT — see the two-strike
    # cases below, which no gap excuses.
    (2_000.0, 1_000.0, "struck BEFORE the success: a negative gap is not a race"),
])
def test_a_genuine_quarantine_is_not_released(fetched, struck, why):
    """No success inside the window means no evidence the lineage answered."""
    entry = UsageEntry(auth_dead_strikes=AUTH_DEAD_STRIKES,
                       fetched_at=fetched, struck_at=struck)
    assert entry.token_dead(), f"a dead token was released: {why}"
    assert not _row_eligible(_row(fetched, struck), now=9_999.0,
                             respect_plans=False), (
        f"a dead token was let back onto the fetch path: {why}"
    )


def test_an_unstruck_row_is_unaffected_by_the_window():
    """CONTROL: the guard must not be what makes a healthy row healthy."""
    entry = UsageEntry(auth_dead_strikes=0, fetched_at=1_000.0,
                       struck_at=1_310.0)
    assert not entry.token_dead()
    # `token_dead` returns before the guard when unstruck, so that assert
    # alone survives every mutation of it. This call does traverse it.
    assert _row_eligible(_row(1_000.0, 1_310.0, strikes=0), now=9_999.0,
                         respect_plans=False)


# --- the race direction: the shape measured on a live host ---

def test_a_strike_moments_after_a_success_is_not_read_as_dead():
    """310 s inside a 600 s window, the gap seen in the field."""
    entry = UsageEntry(auth_dead_strikes=AUTH_DEAD_STRIKES,
                       fetched_at=1_000.0, struck_at=1_310.0)
    assert not entry.token_dead(), (
        "a lineage that answered 310s before it was struck was condemned"
    )


def test_a_suspected_race_is_fetched_again():
    """The escape. Display alone would leave the row quarantined forever —
    only eligibility lets the success that clears the strike happen."""
    assert _row_eligible(_row(1_000.0, 1_310.0), now=9_999.0,
                         respect_plans=False), (
        "a suspected race stayed locked out of the fetch that would clear it"
    )


def test_the_retry_that_fails_again_makes_the_strike_stick(store, clock):
    """The guard must not loop. Nothing about the SECOND strike's gap excuses
    it: what ends the doubt is that a strike already stands, so the count
    reaches two — end to end through the store, not by hand."""
    store.record({"1": FetchRecord(usage={"five_hour": {"utilization": 1}})},
                 IDENT)
    clock.advance(310)
    store.record({"1": FetchRecord(error="invalid_grant", struck_fp="sha256:a")},
                 IDENT)
    assert not store.entries(IDENT)["1"].token_dead(), (
        "PREMISE: the first strike must be doubted"
    )

    clock.advance(WIDE_GAP_S + 1)
    store.record({"1": FetchRecord(error="invalid_grant", struck_fp="sha256:a")},
                 IDENT)
    entry = store.entries(IDENT)["1"]
    assert entry.token_dead(), (
        f"a lineage that failed TWICE was still excused as a race "
        f"(second strike {WIDE_GAP_S + 311:.0f}s after its last success): "
        f"fetched_at={entry.fetched_at} struck_at={entry.struck_at}"
    )


@pytest.mark.parametrize("gap,strikes,relogin", [
    (310, AUTH_DEAD_STRIKES, False),
    # THE CASE THAT FLIPPED. A wide gap used to render the relogin banner; a
    # sibling machine rotates on its own schedule, so it no longer does.
    (WIDE_GAP_S + 1, AUTH_DEAD_STRIKES, False),
    # The positive control, and what still ends the doubt: a SECOND strike.
    (310, AUTH_DEAD_STRIKES + 1, True),
])
def test_the_sentinel_tracks_the_window(
    gap, strikes, relogin, temp_home, mock_claude_config, sample_sequence_data, monkeypatch,
):
    """What the owner actually sees. The guard sits before `token_dead`'s
    fingerprint compare, so `_entry_token_dead` answers False and the collector
    sets no sentinel — but that is a chain of four calls, and reasoning it
    through is not the same as running it."""
    import json
    from unittest.mock import patch

    from claude_swap import oauth
    from claude_swap.credentials import ActiveCredentials
    from claude_swap.json_output import USAGE_RELOGIN_REQUIRED
    from claude_swap.switcher import ClaudeAccountSwitcher

    creds = json.dumps({"claudeAiOauth": {
        "accessToken": "sk-a", "refreshToken": "rt-a", "expiresAt": 99999999999000}})
    sample_sequence_data["accounts"]["2"]["email"] = "b@example.com"
    s = ClaudeAccountSwitcher()
    s._setup_directories()
    s._write_json(s.sequence_file, sample_sequence_data)
    idents = {"2": ("b@example.com", "")}

    # A success, then a strike 310 s later on the SAME stored bytes.
    s._usage_store.record({"2": FetchRecord(usage={"five_hour": {"utilization": 5}})},
                          idents)
    row = s._usage_store._read_rows()["2"]
    row["lastAttemptAt"] = row["struckAt"] = row["fetchedAt"] + gap
    row["authDeadStrikes"] = strikes
    row["struckFingerprint"] = oauth.credential_fingerprint(creds)
    s._usage_store._write_rows({"2": row})

    s._write_account_credentials("2", "b@example.com", creds)
    monkeypatch.setattr(s, "_read_active_credentials",
                        lambda: ActiveCredentials(creds, False, False))
    monkeypatch.setattr(s, "_get_current_account", lambda: ("b@example.com", ""))
    with patch.object(s, "current_account_number", return_value="2"):
        entries = s._collect_usage_entries(s._build_accounts_info(), fetch=set())

    # `is None`, not `!=`: a negative over a six-way sentinel enum keeps
    # passing if the row starts hitting a DIFFERENT sentinel for an unrelated
    # reason. The positive row is the control that the branch is reached.
    expected = USAGE_RELOGIN_REQUIRED if relogin else None
    assert entries["2"].sentinel == expected, (
        f"gap={gap:.0f}s rendered {entries['2'].sentinel!r}"
    )


def test_only_the_FIRST_strike_is_ever_doubted(store, clock):
    """The COUNT is the whole bound now, so it carries the cost cap alone: a
    second rejection is dead however recent the last success was, or a dead
    grant would be re-POSTed forever."""
    store.record({"1": FetchRecord(usage={"five_hour": {"utilization": 1}})}, IDENT)
    clock.advance(10)
    for _ in range(2):
        store.record({"1": FetchRecord(error="invalid_grant", struck_fp="sha256:a")},
                     IDENT)
    entry = store.entries(IDENT)["1"]
    gap = entry.struck_at - entry.fetched_at
    assert gap <= WIDE_GAP_S, (
        f"PREMISE: the success is recent, so only the count can be killing it "
        f"(gap {gap:.0f}s)"
    )
    assert entry.token_dead(), (
        "a second rejection was excused, so a dead grant keeps being POSTed"
    )


# --- the doubt must survive an attempt that carries no new evidence ---

def test_a_transient_retry_does_not_erase_the_doubt(store, clock):
    """The bound that makes the guard worth having. `lastAttemptAt` advances
    on EVERY attempt while `fetchedAt` moves only on a success, so measuring
    the gap between them lets a timeout — which is no evidence about the
    token — widen a doubted strike out of its own window. One invalid_grant
    plus one network blip would then quarantine a healthy account for good,
    which is the state this guard exists to prevent."""
    store.record({"1": FetchRecord(usage={"five_hour": {"utilization": 1}})}, IDENT)
    clock.advance(310)
    store.record({"1": FetchRecord(error="invalid_grant", struck_fp="sha256:a")},
                 IDENT)
    assert not store.entries(IDENT)["1"].token_dead(), "PREMISE: doubted"

    clock.advance(WIDE_GAP_S + 1)
    store.record({"1": FetchRecord(error="http-429")}, IDENT)
    entry = store.entries(IDENT)["1"]
    assert entry.auth_dead_strikes == AUTH_DEAD_STRIKES, (
        "PREMISE: a transient error must not advance the strike count"
    )
    assert not entry.token_dead(), (
        "a timeout carried no evidence about the token and still turned a "
        "doubted strike into a permanent quarantine"
    )
    # PACED, not free. Surviving a transient is only safe because the failure
    # backoff still holds the row off; without it a doubted row would be
    # re-POSTed every collect pass forever. Measured just UNDER a base
    # interval rather than at `now`, where any backoff at all passes and a
    # one-second one would slip through. Under, not on: `_row_eligible` uses
    # a strict `now < backoff_until`, so landing exactly on the boundary
    # would read as eligible.
    assert not _row_eligible(store._read_rows()["1"],
                             now=clock.now + BACKOFF_BASE_S - 1,
                             respect_plans=False), (
        "the doubt survived the transient AND the pacing that bounds it"
    )
    # DISPLAY IS THE LESSER HALF. Only a fetch can land the success that
    # clears the strike, so the row must stay ELIGIBLE — `now` is past the
    # failure backoff so the strike guard is the only thing that can veto.
    assert _row_eligible(store._read_rows()["1"], now=clock.now + 100_000,
                         respect_plans=False), (
        "a doubted row was locked out of the fetch that would clear it"
    )


def test_a_lease_taken_and_never_recorded_does_not_erase_the_doubt(store, clock):
    """`reserve` stamps `lastAttemptAt` BEFORE the fetch, so a collector that
    is killed mid-fetch widens the gap having learned nothing at all."""
    store.record({"1": FetchRecord(usage={"five_hour": {"utilization": 1}})}, IDENT)
    clock.advance(310)
    store.record({"1": FetchRecord(error="invalid_grant", struck_fp="sha256:a")},
                 IDENT)
    clock.advance(WIDE_GAP_S + 1)
    assert store.reserve(["1"], IDENT, respect_plans=False), (
        "PREMISE: a doubted row must still be eligible to retry"
    )
    assert not store.entries(IDENT)["1"].token_dead(), (
        "a lease nobody ever recorded a result for condemned the token"
    )


def test_a_row_struck_before_the_field_existed_keeps_its_doubt(store, clock):
    """THE UPGRADE. The deployed release recorded `lastAttemptAt` and no
    `struckAt`, so reading the absence as "no evidence" re-condemns every row
    it had doubted — and only a fetch can clear a strike, so that quarantine
    is permanent. A struck legacy row is frozen out of fetches, which is
    exactly why `lastAttemptAt` has not moved since the strike and can stand
    in for it."""
    store._write_rows({"1": {
        "email": "a@x.com", "organizationUuid": "",
        "authDeadStrikes": AUTH_DEAD_STRIKES,
        "struckFingerprint": "sha256:a",
        "fetchedAt": clock.now, "lastAttemptAt": clock.now + 310,
    }})
    assert not store.entries(IDENT)["1"].token_dead(), (
        "upgrading condemned a row the previous release was doubting"
    )
    assert _row_eligible(store._read_rows()["1"], now=clock.now + 100_000,
                         respect_plans=False), (
        "upgrading locked a doubted row out of the fetch that would clear it"
    )

    # AND IT MUST SURVIVE TAKING THAT RETRY. `reserve` stamps `lastAttemptAt`
    # before the fetch, so a fallback that keeps reading that field un-dooubts
    # the row exactly once and then re-condemns it on a timeout that carried
    # no evidence — the one-shot migration this pins shut.
    clock.advance(700)
    assert store.reserve(["1"], IDENT, respect_plans=False)
    store.record({"1": FetchRecord(error="timeout")}, IDENT)
    entry = store.entries(IDENT)["1"]
    assert entry.auth_dead_strikes == AUTH_DEAD_STRIKES, "PREMISE: still one strike"
    assert not entry.token_dead(), (
        "a legacy row lost its doubt to a timeout, which is evidence of "
        "nothing, so the quarantine became permanent after one retry"
    )
