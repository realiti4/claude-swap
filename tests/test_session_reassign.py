"""Moving a running managed session to another account (Refs #382)."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

import pytest

from claude_swap import macos_keychain, oauth
from claude_swap import session_reassign as sr
from claude_swap.balance import AccountScore, BalanceParams
from claude_swap.exceptions import ClaudeSwitchError, LockError
from claude_swap.managed_launch import Placement
from claude_swap.managed_sessions import (
    SOURCE_LANE0,
    AccountRef,
    ManagedEntry,
    ManagedSessionRegistry,
    SessionState,
    create_managed_profile,
)
from claude_swap.session import keychain_service_name
from claude_swap.session_credentials import WriteResult
from claude_swap.settings import SessionsSettings

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="managed sessions are POSIX-only (v1)"
)

A = AccountRef("a@example.com", "org-a")
B = AccountRef("b@example.com", "org-b")
MINUTE_MS = 60_000
NOW_MS = 1_000 * MINUTE_MS

# The three accounts the managed_switcher fixture holds, by slot.
B2 = AccountRef("b@example.com", "org-2")
C3 = AccountRef("c@example.com", "org-3")


def _score(account, *, eligible=True, score=0.0, headroom=40.0, reason="ok"):
    return AccountScore(
        account=account, eligible=eligible, score=score, slack=score,
        projected_5h=0.0, headroom=headroom, recovery_ts=0.0, reason=reason,
    )


def _placement(number="2", account=B, **kw):
    return Placement(number, account, "backup", _score(number, **kw))


def _state(status, idle_minutes=None, *, now_ms=NOW_MS):
    return SessionState(
        has_record=status is not None,
        status=status,
        idle_since_ms=(
            now_ms - idle_minutes * MINUTE_MS if idle_minutes is not None else None
        ),
        cwd="/work",
    )


class TestIdleSeconds:
    def test_a_stamp_on_a_non_idle_record_is_not_idle_time(self):
        # Claude stamps a status change, not an idle-since: a record that is
        # busy again carries the stamp of the moment it went busy.
        state = SessionState(True, "busy", NOW_MS - 90 * MINUTE_MS, "/work")
        assert sr.idle_seconds(state, NOW_MS) is None

    def test_idle_time_is_reported_in_seconds(self):
        assert sr.idle_seconds(_state("idle", 90), NOW_MS) == 90 * 60.0


class TestMeetsIdleFloor:
    def test_no_reading_never_clears_it(self):
        assert sr.meets_idle_floor(None, 60.0) is False

    def test_short_of_the_floor_fails(self):
        assert sr.meets_idle_floor(59 * 60.0, 60.0) is False

    def test_exactly_the_floor_passes(self):
        assert sr.meets_idle_floor(60 * 60.0, 60.0) is True

    def test_past_the_floor_passes(self):
        assert sr.meets_idle_floor(61 * 60.0, 60.0) is True


class TestMeetsScoreMargin:
    def test_short_of_the_margin_fails(self):
        assert sr.meets_score_margin(0.0, 14.9, 15.0) is False

    def test_exactly_the_margin_passes(self):
        assert sr.meets_score_margin(0.0, 15.0, 15.0) is True

    def test_past_the_margin_passes(self):
        assert sr.meets_score_margin(0.0, 15.1, 15.0) is True


class TestDecide:
    def _decide(self, **kw):
        base = dict(
            current=A,
            current_score=_score("1", score=0.0),
            placement=_placement(score=100.0),
            state=_state("idle", 90),
            now_ms=NOW_MS,
            idle_minutes=60.0,
            margin=15.0,
        )
        base.update(kw)
        return sr.decide_reassignment(**base)

    def test_a_quarantined_account_is_escaped_whatever_the_session_is_doing(self):
        """The engine has stopped refreshing that account, so the session's
        token expires where it stands. Worse than at-limit, and nothing in
        its usage says so."""
        decision = self._decide(
            current_quarantined=True,
            current_score=_score("1", score=100.0, reason="ok"),
            state=_state("busy"),
            placement=_placement(score=0.0),
        )
        assert decision is not None
        assert decision.reason == sr.REASON_QUARANTINED

    def test_a_quarantined_account_is_escaped_with_no_usage_to_read(self):
        """A quarantined slot stops being fetched, so "unknown" is its normal
        state — and the refusal that unknown usage normally earns would strand
        the session there for exactly that reason."""
        decision = self._decide(current_quarantined=True, current_score=None)
        assert decision is not None
        assert decision.reason == sr.REASON_QUARANTINED

    def test_a_quarantined_account_still_needs_somewhere_to_go(self):
        """Swapping one account that cannot serve it for another only spends
        the prompt cache."""
        assert self._decide(
            current_quarantined=True, placement=_placement(headroom=0.0)
        ) is None
        assert self._decide(current_quarantined=True, placement=None) is None

    def test_a_quarantined_account_waits_for_a_session_claude_has_recorded(self):
        """The reservation is what holds a session Claude has not written a
        record for yet; a move before then races the launch's own seeding."""
        assert self._decide(
            current_quarantined=True, state=_state(None)
        ) is None

    def test_at_limit_moves_a_busy_session(self):
        decision = self._decide(
            current_score=_score("1", eligible=False, headroom=0.0, reason="at-limit"),
            state=_state("busy"),
        )
        assert decision is not None
        assert decision.reason == sr.REASON_AT_LIMIT

    def test_at_limit_accepts_an_ineligible_target_with_headroom(self):
        """Escaping a dead account beats waiting for a healthy one."""
        decision = self._decide(
            current_score=_score("1", eligible=False, headroom=0.0, reason="at-limit"),
            state=_state("waiting"),
            placement=_placement(eligible=False, reason="five-hour-ceiling", headroom=8.0),
        )
        assert decision is not None and decision.reason == sr.REASON_AT_LIMIT

    def test_at_limit_refuses_a_target_with_no_headroom(self):
        # Trading one blocked account for another costs the prompt cache and
        # buys no work.
        assert self._decide(
            current_score=_score("1", eligible=False, headroom=0.0, reason="at-limit"),
            state=_state("busy"),
            placement=_placement(eligible=False, reason="at-limit", headroom=0.0),
        ) is None

    def test_idle_long_enough_and_past_the_margin_moves(self):
        decision = self._decide()
        assert decision is not None and decision.reason == sr.REASON_IDLE

    def test_idle_but_inside_the_margin_stays(self):
        assert self._decide(placement=_placement(score=10.0)) is None

    def test_a_target_exactly_at_the_margin_moves(self):
        # The margin is what a move must be worth, not what it must beat.
        assert self._decide(placement=_placement(score=15.0)) is not None

    def test_idle_but_not_long_enough_stays(self):
        assert self._decide(state=_state("idle", 30)) is None

    def test_idle_exactly_as_long_as_required_moves(self):
        assert self._decide(state=_state("idle", 60)) is not None

    def test_a_busy_session_below_its_limit_never_moves(self):
        assert self._decide(state=_state("busy")) is None

    def test_a_busy_session_carrying_an_idle_stamp_never_moves(self):
        assert self._decide(
            state=SessionState(True, "busy", NOW_MS - 90 * MINUTE_MS, "/work")
        ) is None

    def test_a_waiting_session_below_its_limit_never_moves(self):
        assert self._decide(state=_state("waiting")) is None

    def test_a_starting_session_never_moves(self):
        # Not even off an account that cannot serve it: with no record, the
        # launch's own seeding write may still be in flight.
        assert self._decide(
            current_score=_score("1", eligible=False, headroom=0.0, reason="at-limit"),
            state=_state(None),
        ) is None

    def test_a_recorded_but_unreadable_status_never_moves(self):
        # Claude registered, but its record's status is not a string. What
        # the session is doing is unknown, which is not a licence to move it.
        assert self._decide(
            current_score=_score("1", eligible=False, headroom=0.0, reason="at-limit"),
            state=SessionState(True, None, None, "/work"),
        ) is None

    def test_an_idle_move_needs_an_eligible_target(self):
        assert self._decide(
            placement=_placement(score=100.0, eligible=False, reason="weekly-threshold")
        ) is None

    def test_unknown_usage_blocks_every_move(self):
        assert self._decide(current_score=None) is None
        assert self._decide(
            current_score=AccountScore("1", False, None, None, None, None, float("inf"),
                                       "unknown-usage")
        ) is None

    def test_no_target_stays(self):
        assert self._decide(placement=None) is None

    def test_the_current_account_is_not_a_target(self):
        assert self._decide(placement=_placement(number="1", account=A, score=100.0)) is None

    def test_an_idle_record_with_no_stamp_stays(self):
        assert self._decide(state=SessionState(True, "idle", None, "/work")) is None


NOW_S = 1_700_000_000.0
HOUR_S = 3600.0
DAY_S = 86400.0


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _usage(pct7: float) -> dict:
    """Store-shape usage: an unused 5h window, and a 7d one resetting in 3
    days — four days into a schedule that ends a day early, so on-schedule
    utilization is 66.7%."""
    return {
        "five_hour": {"pct": 0.0, "resets_at": _iso(NOW_S + 2 * HOUR_S)},
        "seven_day": {"pct": pct7, "resets_at": _iso(NOW_S + 3 * DAY_S)},
    }


def _entry(account: AccountRef) -> ManagedEntry:
    return ManagedEntry(
        session_id="auto-0000000b",
        account=account,
        pid=os.getpid(),
        proc_start=None,
        source="backup",
        created_at="2026-01-01T00:00:00Z",
        last_assigned_at="2026-01-01T00:00:00Z",
        last_reason="launch",
    )


class TestPlan:
    """Scoring the session's own account and ranking the pool it is handed."""

    def _plan(self, switcher, *, account=B2, **kw):
        base = dict(
            state=_state("idle", 90, now_ms=NOW_S * 1000.0),
            usage={"1": _usage(0.0), "2": _usage(60.0), "3": _usage(0.0)},
            busy={},
            identities={"2": B2, "3": C3},
            lane0="1",
            now=NOW_S,
            models=(),
            params=BalanceParams(),
            sessions_settings=SessionsSettings(),
        )
        base.update(kw)
        return sr.plan_for_entry(switcher, _entry(account), **base)

    def test_an_idle_session_is_planned_onto_the_best_ranked_account(self, managed_switcher):
        decision = self._plan(managed_switcher)
        assert decision is not None
        assert (decision.reason, decision.placement.number) == (sr.REASON_IDLE, "3")

    def test_a_blocked_account_is_planned_off_whatever_the_session_is_doing(
        self, managed_switcher
    ):
        decision = self._plan(
            managed_switcher,
            usage={"1": _usage(0.0), "2": _usage(100.0), "3": _usage(0.0)},
            state=_state("busy"),
        )
        assert decision is not None
        assert (decision.reason, decision.placement.number) == (sr.REASON_AT_LIMIT, "3")

    def test_a_session_on_a_quarantined_slot_is_planned_off_it(
        self, managed_switcher
    ):
        """The pool this ranks is already narrowed to slots that are not
        quarantined, which says nothing about the slot the session is ON."""
        decision = self._plan(
            managed_switcher,
            usage={"1": _usage(0.0), "2": _usage(10.0), "3": _usage(0.0)},
            state=_state("busy"),
            quarantined={"2"},
        )
        assert decision is not None
        assert (decision.reason, decision.placement.number) == (
            sr.REASON_QUARANTINED, "3",
        )

    def test_a_quarantined_slot_that_is_not_this_session_changes_nothing(
        self, managed_switcher
    ):
        decision = self._plan(
            managed_switcher,
            usage={"1": _usage(0.0), "2": _usage(10.0), "3": _usage(0.0)},
            state=_state("busy"),
            quarantined={"3"},
        )
        assert decision is None

    def test_a_target_inside_the_margin_plans_nothing(self, managed_switcher):
        # Five points of balance is not worth an uncached conversation.
        assert self._plan(
            managed_switcher,
            usage={"1": _usage(0.0), "2": _usage(60.0), "3": _usage(55.0)},
        ) is None

    def test_an_account_no_slot_holds_any_more_plans_nothing(
        self, managed_switcher, monkeypatch
    ):
        def _never(**_kw):
            raise AssertionError("an account with no slot must not be ranked")

        monkeypatch.setattr(sr, "choose_placement", _never)
        assert self._plan(managed_switcher, account=AccountRef("gone@example.com", "")) is None

    def test_an_unreadable_roster_plans_nothing(self, managed_switcher, monkeypatch):
        def _raise(*_a, **_kw):
            raise ClaudeSwitchError("sequence data unreadable")

        monkeypatch.setattr(sr, "slot_for_account", _raise)
        assert self._plan(managed_switcher) is None


BUFFER_MS = 10 * 60 * 1000


def _spy_writer(result=WriteResult(True, "ok")):
    """A writer that records every call's keyword arguments."""
    calls: list[dict] = []

    def writer(*_args, **kwargs):
        calls.append(kwargs)
        return result

    return calls, writer


class TestApply:
    @pytest.fixture
    def seeded(self, managed_switcher):
        registry = ManagedSessionRegistry(managed_switcher.backup_dir)
        session_id = "auto-aaaaaaaa"
        entry = registry.allocate(
            session_id, lambda _busy: (B2, "backup"), pid=os.getpid(), proc_start=None
        )
        create_managed_profile(registry.session_dir(session_id))
        return managed_switcher, registry, entry

    def _decision(self, reason=sr.REASON_IDLE):
        return sr.ReassignDecision(reason, Placement("3", C3, "backup", _score("3")))

    def _apply(self, seeded, decision=None, **kw):
        switcher, registry, entry = seeded
        return sr.apply_reassignment(
            switcher, registry, entry, decision or self._decision(),
            now_ms=NOW_MS, buffer_ms=BUFFER_MS, **kw,
        )

    def test_a_successful_move_rewrites_the_row(self, seeded):
        _, registry, entry = seeded
        result = self._apply(seeded)
        assert result.ok and result.detail == "moved"
        assert (result.session_id, result.reason, result.number) == (
            entry.session_id, sr.REASON_IDLE, "3",
        )
        assert (result.from_account, result.to_account) == (B2, C3)
        row = registry.get(entry.session_id)
        assert row.account == C3
        assert row.last_reason == sr.REASON_IDLE
        # The fingerprint recorded is the token now live in the profile, so
        # the next push pass has nothing to write for this session.
        assert row.access_fingerprint == oauth.access_token_fingerprint(
            (registry.session_dir(entry.session_id) / ".credentials.json").read_text()
        )

    def test_the_profile_serves_the_new_account(self, seeded):
        _, registry, entry = seeded
        self._apply(seeded)
        session_dir = registry.session_dir(entry.session_id)
        creds = json.loads((session_dir / ".credentials.json").read_text())
        assert creds["claudeAiOauth"]["accessToken"] == "at-3"
        assert "refreshToken" not in creds["claudeAiOauth"]
        config = json.loads((session_dir / ".claude.json").read_text())
        assert config["oauthAccount"]["emailAddress"] == "c@example.com"

    def test_a_failed_write_puts_the_row_back_and_clears_the_fingerprint(self, seeded):
        _, registry, entry = seeded
        registry.update(
            entry.session_id, access_fingerprint="old-fp", source=SOURCE_LANE0
        )

        def refusing_writer(*_a, **_kw):
            return WriteResult(False, "lock-timeout")

        result = self._apply(seeded, writer=refusing_writer)
        assert not result.ok and result.detail == "lock-timeout"
        row = registry.get(entry.session_id)
        assert row.account == B2
        # The source the ROW has now, not the one the caller's copy was read
        # with: the daemon captures its entries before the push pass rewrites
        # them in place, and putting back a stale `backup` would turn off the
        # only thing that tells the hook this token is lane 0's and has to be
        # watched for rotation.
        assert entry.source != SOURCE_LANE0
        assert row.source == SOURCE_LANE0
        # A session that never left its account must not report the moment
        # the move failed as the moment it was assigned.
        assert row.last_assigned_at == entry.last_assigned_at
        # Cleared, so the next push rewrites the old account's token into the
        # profile whatever the failed move half-landed there.
        assert row.access_fingerprint is None
        assert row.last_reason == f"{sr.REASON_IDLE}-failed"

    def test_a_move_another_pass_committed_meanwhile_is_not_reverted(self, seeded):
        _, registry, entry = seeded
        elsewhere = AccountRef("d@example.com", "org-4")

        def losing_writer(*_a, **_kw):
            # Another mover repointed the row and landed its own token; this
            # call's write is the one the writer's re-check takes back.
            registry.update(entry.session_id, account=elsewhere)
            return WriteResult(False, "registry-changed")

        result = self._apply(seeded, writer=losing_writer)
        assert not result.ok and result.detail == "registry-changed"
        # Restoring this call's stale account would undo a completed move and
        # leave the row naming one account while the profile holds another's
        # token.
        assert registry.get(entry.session_id).account == elsewhere

    def test_a_move_to_the_same_target_is_not_reverted_either(self, seeded):
        """The dangerous half of the same case, and the likely one: both
        movers rank the same roster with the same functions, so when a
        session has to leave an account they usually agree about where it
        goes. The account alone cannot tell this call's own step 1 apart
        from the other mover's, so an account-only compare reverts a move
        that has already landed its token — leaving the row on the old
        account while the profile holds the target's."""
        _, registry, entry = seeded
        landed = {}

        def losing_writer(*_a, **_kw):
            # The other mover: the SAME target this call is moving to, with
            # its own stamp, its own token written and recorded.
            registry.update(
                entry.session_id,
                account=C3,
                last_assigned_at="2026-01-01T00:00:00Z",
                access_fingerprint="theirs",
            )
            landed["row"] = registry.get(entry.session_id)
            return WriteResult(False, "lock-timeout")

        result = self._apply(seeded, writer=losing_writer)
        assert not result.ok and result.detail == "lock-timeout"
        row = registry.get(entry.session_id)
        assert row.account == C3 and row.account != entry.account
        assert row.last_assigned_at == landed["row"].last_assigned_at
        assert row.access_fingerprint == "theirs"

    def test_a_peer_that_stamped_the_same_second_is_not_reverted(self, seeded):
        """These stamps have a second's resolution and both movers run on
        the same cadence, so agreeing about the moment as well as about the
        target is not an exotic case. What still tells them apart is the
        fingerprint: step 1 clears it, and a move that finished has put the
        new one back."""
        _, registry, entry = seeded

        def losing_writer(*_a, **_kw):
            mine = registry.get(entry.session_id)
            registry.update(
                entry.session_id,
                account=C3,
                last_assigned_at=mine.last_assigned_at,
                access_fingerprint="theirs",
            )
            return WriteResult(False, "lock-timeout")

        result = self._apply(seeded, writer=losing_writer)
        assert not result.ok and result.detail == "lock-timeout"
        row = registry.get(entry.session_id)
        assert row.account == C3 and row.account != entry.account
        assert row.access_fingerprint == "theirs"

    def test_its_own_half_written_move_is_still_taken_back(self, seeded):
        """The other half of the same compare: when nobody else touched the
        row, what is on it is this call's own step 1, and a failed write has
        to put the session back where it was."""
        _, registry, entry = seeded

        def refusing_writer(*_a, **_kw):
            return WriteResult(False, "lock-timeout")

        result = self._apply(seeded, writer=refusing_writer)
        assert not result.ok
        row = registry.get(entry.session_id)
        assert row.account == entry.account
        assert row.last_assigned_at == entry.last_assigned_at

    def test_a_row_that_moved_since_the_plan_is_left_where_it_is(self, seeded):
        """A plan is always older than the move it asks for, and two movers
        write these rows: this pass and the session's own prompt hook. Acting
        on the stale one would overwrite a committed move with a decision
        about the account the session has already left."""
        _, registry, entry = seeded
        elsewhere = AccountRef("d@example.com", "org-4")
        registry.update(entry.session_id, account=elsewhere)
        calls, writer = _spy_writer()

        result = self._apply(seeded, writer=writer)
        assert not result.ok and result.detail == "registry-changed"
        assert calls == []
        assert registry.get(entry.session_id).account == elsewhere

    def test_a_row_it_cannot_read_leaves_the_session_alone(self, seeded, monkeypatch):
        _, registry, entry = seeded
        calls, writer = _spy_writer()

        def _raise(*_a, **_kw):
            raise LockError("registry lock timeout")

        monkeypatch.setattr(registry, "get_locked", _raise)
        result = self._apply(seeded, writer=writer)
        assert not result.ok and result.detail == "registry-unreadable"
        assert calls == []
        assert registry.get(entry.session_id) == entry

    def test_an_unresolvable_target_never_touches_the_row(self, seeded):
        _, registry, entry = seeded
        decision = sr.ReassignDecision(
            sr.REASON_IDLE,
            Placement("9", AccountRef("gone@example.com", ""), "backup", _score("9")),
        )
        result = self._apply(seeded, decision)
        assert not result.ok and result.detail == "account-gone"
        assert registry.get(entry.session_id) == entry

    def test_a_registry_that_cannot_be_rewritten_leaves_the_session_alone(
        self, seeded, monkeypatch
    ):
        _, registry, entry = seeded
        calls, writer = _spy_writer()

        def _raise(*_a, **_kw):
            raise LockError("registry lock timeout")

        monkeypatch.setattr(registry, "update", _raise)
        result = self._apply(seeded, writer=writer)
        assert not result.ok and result.detail == "registry-unwritable"
        # A token written against a row that still names the old account is
        # exactly what the writer's own re-check takes back, so nothing is
        # written at all.
        assert calls == []
        assert not (registry.session_dir(entry.session_id) / ".credentials.json").exists()

    def test_a_session_that_exited_mid_move_is_not_written(self, seeded):
        _, registry, entry = seeded
        calls, writer = _spy_writer()
        registry.remove(entry.session_id)
        result = self._apply(seeded, writer=writer)
        assert not result.ok and result.detail == "no-registry-entry"
        assert calls == []
        assert registry.get(entry.session_id) is None

    def test_a_session_swept_away_while_the_write_failed_has_nothing_put_back(
        self, seeded
    ):
        _, registry, entry = seeded

        def losing_writer(*_a, **_kw):
            # The session exited and a sweep dropped its row while this write
            # was failing.
            registry.remove(entry.session_id)
            return WriteResult(False, "session-ended")

        result = self._apply(seeded, writer=losing_writer)
        assert not result.ok and result.detail == "session-ended"
        # Nothing to put a row back for; reserving one again would strand a
        # profile the sweep is entitled to delete.
        assert registry.get(entry.session_id) is None

    def test_a_crash_between_the_row_and_the_write_leaves_the_row_on_the_target(self, seeded):
        _, registry, entry = seeded
        registry.update(entry.session_id, access_fingerprint="old-fp")
        seen: list = []

        def crashing_writer(*_a, **_kw):
            seen.append(registry.get(entry.session_id))
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            self._apply(seeded, writer=crashing_writer)
        # The row led the write and its fingerprint was cleared first, so a
        # later push writes the target's token into the profile and finishes
        # the move on its own.
        [row] = seen
        assert row.account == C3 and row.access_fingerprint is None
        assert registry.get(entry.session_id) == row

    def test_the_move_is_refused_unless_the_keychain_item_moves_too(
        self, seeded, monkeypatch
    ):
        # Claude reads the item before the plaintext, so an item this move
        # could not replace would go on serving the OLD account's token. The
        # real writer decides that on require_keychain, and a move over a
        # profile that already holds a token cannot settle for plaintext.
        _, registry, entry = seeded
        profile_service = keychain_service_name(registry.session_dir(entry.session_id))
        readable = macos_keychain.get_password

        def unreadable(service, account):
            if service == profile_service:
                raise macos_keychain.KeychainError("timed out")
            return readable(service, account)

        monkeypatch.setattr(macos_keychain, "get_password", unreadable)
        result = self._apply(seeded)
        assert not result.ok and result.detail == "keychain-unreadable"
        row = registry.get(entry.session_id)
        assert row.account == B2
        assert row.access_fingerprint is None

    def test_the_callers_lock_budget_reaches_the_writer(self, seeded):
        calls, writer = _spy_writer()
        self._apply(seeded, writer=writer, lock_timeout=3.0)
        assert [call["lock_timeout"] for call in calls] == [3.0]
