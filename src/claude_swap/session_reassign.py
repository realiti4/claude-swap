"""Moving a running managed session to another account.

Three triggers, and they answer different questions.

**quarantined**: the account a session sits on has a grant the engine could
not use — a dead refresh token, an identity that no longer matches — so the
engine has stopped refreshing it. The session is not near a limit; it is on
an account that cannot serve it at all, and its token expires where it
stands. Strictly worse than at-limit, and escaped the same way: any target
with room, whatever the session is doing.

**at-limit**: the account a session sits on has no headroom left on any
window that binds it. The session cannot work at all, so it moves whatever
it is doing and whatever it costs — including the prompt cache, which is
worthless on an account that will refuse the request anyway.

**idle**: the session has been waiting for a prompt longer than the prompt
cache lives, and some other account is meaningfully further behind its
weekly schedule. Moving invalidates the conversation's cache, so this is
only worth doing once the cache has expired on its own; the idle delay is
the price of never charging a working session for a balancing decision.

A move writes three things and nothing else: the profile's keychain item,
its ``.credentials.json`` and the ``oauthAccount`` in its ``.claude.json``.
The shared history links are untouched, so ``--resume`` works across a move
exactly as it works across accounts.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from claude_swap import balance
from claude_swap.balance import AccountScore, BalanceParams
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.managed_launch import Placement, choose_placement
from claude_swap.managed_refresh import (
    AccessResolution,
    resolve_access_credential,
    slot_for_account,
)
from claude_swap.managed_sessions import (
    AccountRef,
    ManagedEntry,
    ManagedSessionRegistry,
    SessionState,
    utc_now_iso,
)
from claude_swap.session_credentials import WriteResult, write_session_credential
from claude_swap.settings import SessionsSettings

if TYPE_CHECKING:
    from claude_swap.switcher import ClaudeAccountSwitcher

_logger = logging.getLogger("claude-swap")

REASON_AT_LIMIT = "at-limit"
REASON_IDLE = "idle"
REASON_QUARANTINED = "quarantined"

# What a registry or store read can raise on the way. Same set the refresher
# uses: cswap's own errors plus plain I/O from files read without a guard.
_STORE_FAILURES = (ClaudeSwitchError, OSError, UnicodeDecodeError)

# Claude's statuses. Only "idle" is a session waiting for its next prompt;
# "busy" and "waiting" are both mid-turn as far as a cache is concerned.
_IDLE = "idle"


@dataclass(frozen=True)
class ReassignDecision:
    """Move this session, for this reason, to this placement."""

    reason: str          # REASON_AT_LIMIT | REASON_IDLE
    placement: Placement


@dataclass(frozen=True)
class ReassignResult:
    """What one attempted move did.

    ``detail`` is ``"moved"`` on success, else the refusal that stopped it —
    an ``AccessResolution`` status, a ``WriteResult`` reason, or one of this
    module's own registry refusals. Callers log it; nothing branches on it.
    """

    session_id: str
    reason: str
    number: str
    from_account: AccountRef
    to_account: AccountRef
    ok: bool
    detail: str


def idle_seconds(state: SessionState, now_ms: float) -> float | None:
    """How long this session has been idle, or None when it is not idle or
    Claude's record carried no usable stamp."""
    if state.status != _IDLE or state.idle_since_ms is None:
        return None
    return max(0.0, (now_ms - state.idle_since_ms) / 1000.0)


def _escape(reason: str, placement: Placement) -> ReassignDecision | None:
    """A move off an account that cannot serve this session, or None when the
    target cannot either.

    The shape both escapes share: any recorded status, and any target that
    still has headroom, eligible or not. Getting off a blocked account beats
    waiting for a perfect one; trading it for a second blocked account only
    spends the prompt cache.
    """
    headroom = placement.score.headroom
    if headroom is None or headroom <= 0:
        return None
    return ReassignDecision(reason, placement)


def decide_reassignment(
    *,
    current: AccountRef,
    current_score: AccountScore | None,
    placement: Placement | None,
    state: SessionState,
    now_ms: float,
    idle_minutes: float,
    margin: float,
    current_quarantined: bool = False,
) -> ReassignDecision | None:
    """Whether to move this session now. Pure: no clock, no I/O, no state.

    Refuses in every ambiguous case:

    - no target, or a target that is the account we are already on;
    - usage we cannot read or trust for the CURRENT account — "at limit"
      cannot be asserted about an account whose windows are unknown, and an
      idle move has no score to beat;
    - a session Claude has not recorded a status for. Its Claude has not read
      the credential once, and the launch's own seeding write may still be in
      flight; the reservation exists precisely to keep it where it was placed.

    ``current_quarantined`` is the exception to the second of those: an
    account the engine has quarantined is one it no longer refreshes, so
    "usage unknown" there is the normal state and not a reason to leave a
    session sitting on a token nothing will renew.

    Otherwise: quarantined and at-limit both move any recorded status and
    take any target that still has headroom, eligible or not (:func:`_escape`)
    — escaping a blocked account beats waiting for a perfect one, but trading
    it for a second blocked account only spends the prompt cache. An idle
    move needs a genuinely idle session past ``idle_minutes``, an eligible
    target, and ``margin`` points of score.
    """
    if placement is None or placement.account == current:
        return None
    if not state.has_record or state.status is None:
        return None
    if current_quarantined:
        # Judged before the score, because the score is not the question and
        # may not even exist: an account the engine has stopped refreshing
        # stops being fetched too, so its usage ages into "unknown" and the
        # refusal below would strand the session on it precisely because
        # nothing can be read about it any more.
        return _escape(REASON_QUARANTINED, placement)
    if current_score is None or current_score.score is None or current_score.headroom is None:
        return None
    if current_score.reason == balance.REASON_AT_LIMIT:
        return _escape(REASON_AT_LIMIT, placement)
    idle_s = idle_seconds(state, now_ms)
    if idle_s is None or idle_s < idle_minutes * 60.0:
        return None
    if not placement.score.eligible or placement.score.score is None:
        # An eligible score always carries one; the second half keeps this
        # total for a caller holding a placement balance did not rank.
        return None
    if placement.score.score - current_score.score < margin:
        return None
    return ReassignDecision(REASON_IDLE, placement)


def plan_for_entry(
    switcher: ClaudeAccountSwitcher,
    entry: ManagedEntry,
    *,
    state: SessionState,
    usage: Mapping[str, dict | None],
    busy: Mapping[str, int],
    identities: Mapping[str, AccountRef],
    lane0: str | None,
    now: float,
    models: Sequence[str],
    params: BalanceParams,
    sessions_settings: SessionsSettings,
    quarantined: Collection[str] = (),
) -> ReassignDecision | None:
    """Score this session's account, rank the pool, and decide.

    ``identities`` is the already-narrowed candidate pool (quarantined slots,
    API-key accounts and accounts running their own ``cswap run N`` are out
    — ``managed_launch._candidate_identities``), so this only ranks what it
    is handed, exactly as placement does at launch. ``usage`` is likewise the
    caller's single decision-grade read (``managed_launch._decision_usage``)
    for the whole pass, not one per session. ``quarantined`` is the engine's
    own set of stopped slots, the same one ``identities`` was narrowed by —
    handed in separately because a session already SITTING on such a slot is
    the case the narrowing cannot express. Returns None — never raises —
    when the account's slot cannot be read.
    """
    try:
        number = slot_for_account(switcher, entry.account)
    except _STORE_FAILURES:
        return None
    if number is None:
        # No slot holds the account any more. The push pass already reports
        # that as a session-health problem; moving it would need a credential
        # nothing can resolve.
        return None
    current_score = balance.score_account(
        number, usage.get(number), now=now, models=models,
        busy_sessions=busy.get(number, 0), params=params,
    )
    placement = choose_placement(
        candidates=list(identities),
        identities=identities,
        usage=usage,
        lane0=lane0,
        busy=busy,
        now=now,
        models=models,
        params=params,
    )
    return decide_reassignment(
        current=entry.account,
        current_score=current_score,
        placement=placement,
        state=state,
        now_ms=now * 1000.0,
        idle_minutes=sessions_settings.idle_reassign_minutes,
        margin=sessions_settings.reassign_margin,
        current_quarantined=number in quarantined,
    )


def apply_reassignment(
    switcher: ClaudeAccountSwitcher,
    registry: ManagedSessionRegistry,
    entry: ManagedEntry,
    decision: ReassignDecision,
    *,
    now_ms: float,
    buffer_ms: int,
    lock_timeout: float | None = None,
    writer: Callable[..., WriteResult] = write_session_credential,
    resolver: Callable[..., AccessResolution] = resolve_access_credential,
) -> ReassignResult:
    """Point a live managed session at ``decision``'s account.

    The order matters and is the whole failure story. ``write_session_credential``
    takes back any write whose registry row does not already name the account
    being written (that check is what stops a push from stranding a session a
    sweep just dropped), so the row has to lead:

    0. Re-read the row under the registry lock and refuse unless it still
       names the account the plan was made about. A plan is always older than
       the move it asks for, and the row is written by two movers (this pass
       and the session's own prompt hook) and rewritten in place by the
       push. The row that comes back is the one used from there on, for the
       rollback in step 4 as well.
    1. Repoint the row at the target, with no access fingerprint. A push that
       races this now writes the TARGET's token, which is where the session is
       going anyway.
    2. Write the credential.
    3. On success, record the fingerprint the write left live.
    4. On failure, put the row back on the old account — still with no
       fingerprint, so the next push pass rewrites the old account's current
       token into both stores and repairs whatever half-landed. That last
       step is a compare-and-set: the row goes back only while it still
       names the target this call set. The registry has no conditional
       write, so the compare is a locked read (``get_locked``) and the set
       a separate ``update``: a mover that commits its own move in the gap
       between the two — a lock acquisition wide, against a failing write's
       own ``registry-changed`` — has that move reverted here, and only the
       cleared fingerprint then reconciles the row with the profile.

    A crash between 1 and 2 leaves the row on the target with no fingerprint,
    and the next push completes the move. Either way the session ends up on an
    account somebody refreshes, which is the only outcome that matters.

    ``resolver`` is how a caller moving several sessions in one pass keeps
    :func:`push_refresh`'s promise of one resolution and at most one grant
    consumption per account. Resolving is a roster read, a credential read
    (the keychain included) and possibly a refresh POST, and none of that
    depends on which session is being moved -- so N sessions rescued onto
    one target would otherwise pay it N times, and a ``transient`` answer
    would be N retries of the same POST inside one tick.
    """
    target = decision.placement.account
    session_id = entry.session_id
    resolution = resolver(switcher, target, now_ms=now_ms, buffer_ms=buffer_ms)
    if (
        resolution.status != "ok"
        or resolution.credential is None
        or resolution.oauth_account is None
    ):
        return ReassignResult(
            session_id, decision.reason, decision.placement.number,
            entry.account, target, False, resolution.status,
        )

    try:
        # Compare-and-set on the way IN, the mirror of the one on the way
        # out, and for the same reason: ``entry`` is the row the PLAN was
        # made from, and a plan is always made before the move it asks for.
        # The other mover — the hook inside the session, which judges its own
        # account on every prompt — can have committed a move in between, and
        # repointing unconditionally would overwrite it with a decision taken
        # about an account this session has already left, then "roll back" to
        # that same account when the write failed. Narrow, not closed: the
        # registry cannot express a compare and a set under one acquisition
        # (see step 4), so this is a lock acquisition wide.
        #
        # The fresh row also REPLACES the caller's copy from here on. What
        # the daemon holds was read before its own push pass rewrote these
        # rows in place, so rolling back from it would put back a ``source``
        # the push had just corrected — and ``source`` is what tells the hook
        # a token is borrowed from lane 0 and has to be watched for rotation.
        current = registry.get_locked(session_id)
    except _STORE_FAILURES as e:
        _logger.warning(
            f"Managed session {session_id}: could not read the row before a "
            f"move to {target.email} ({e}); leaving it where it is."
        )
        return ReassignResult(
            session_id, decision.reason, decision.placement.number,
            entry.account, target, False, "registry-unreadable",
        )
    if current is None:
        return ReassignResult(
            session_id, decision.reason, decision.placement.number,
            entry.account, target, False, "no-registry-entry",
        )
    if current.account != entry.account:
        _logger.info(
            f"Managed session {session_id}: already moved to "
            f"{current.account.email}; not moving it to {target.email}."
        )
        return ReassignResult(
            session_id, decision.reason, decision.placement.number,
            entry.account, target, False, "registry-changed",
        )
    entry = current

    moved_at = utc_now_iso(lambda: now_ms / 1000.0)
    try:
        repointed = registry.update(
            session_id,
            account=target,
            source=resolution.source,
            last_assigned_at=moved_at,
            last_reason=decision.reason,
            access_fingerprint=None,
        )
    except _STORE_FAILURES as e:
        _logger.warning(
            f"Managed session {session_id}: could not record a move to "
            f"{target.email} ({e}); leaving it where it is."
        )
        return ReassignResult(
            session_id, decision.reason, decision.placement.number,
            entry.account, target, False, "registry-unwritable",
        )
    if repointed is None:
        # The row went away between the plan and here (the session exited).
        return ReassignResult(
            session_id, decision.reason, decision.placement.number,
            entry.account, target, False, "no-registry-entry",
        )

    result = writer(
        registry.session_dir(session_id), target, resolution.credential,
        resolution.oauth_account, registry=registry, lock_timeout=lock_timeout,
    )
    if result.ok:
        try:
            registry.update(session_id, access_fingerprint=result.fingerprint)
        except _STORE_FAILURES as e:
            # The token is live in the session; only recording it failed. The
            # row already names the target, and the missing fingerprint makes
            # the next push rewrite the same token — a harmless repeat.
            _logger.warning(
                f"Managed session {session_id}: moved to {target.email} but "
                f"could not record the token ({e})"
            )
        return ReassignResult(
            session_id, decision.reason, decision.placement.number,
            entry.account, target, True, "moved",
        )

    try:
        # Compare-and-set. Another mover that repointed this row in the
        # meantime has committed a move of its own — its row is exactly what
        # made the writer take this call's write back — and restoring this
        # call's now-stale account would undo it, leaving the row on one
        # account and the profile holding the other one's token. The compare
        # is a locked read, ordered against every mutator and the sweep the
        # same way the credential writer's own row checks are; the registry
        # cannot express the compare and the set under one acquisition, so
        # the window is a lock acquisition wide rather than closed (see this
        # function's docstring). The row can also be gone entirely: the
        # session exited and a sweep dropped it while the write was failing.
        # The whole move is put back, ``last_assigned_at`` included: a
        # session that never left its account must not report the moment a
        # move failed as the moment it was assigned.
        #
        # ``last_assigned_at`` is also half of the compare, because the
        # account alone cannot tell this call's own step 1 apart from
        # another mover that picked the SAME target — which is the likely
        # case, not an exotic one: both movers rank the same roster with the
        # same functions and usually agree about where a stranded session
        # should go. Reverting there would undo a move that had already
        # landed its token. What step 1 wrote is this stamp, so only a row
        # still carrying it is this call's to take back.
        #
        # The empty fingerprint is the third term, and it is what disposes
        # of two movers stamping the same second out of different clocks --
        # not a remote case, since both are ticking on the same cadence.
        # Step 1 clears the fingerprint and the move records the new one on
        # its way out, so a row that has one is a move somebody finished. A
        # peer caught BETWEEN those two writes still matches all three, and
        # that is the whole of the residue now: one lock acquisition wide,
        # on the same second, to the same target, and only while the peer
        # has written the credential but not yet recorded it.
        current = registry.get_locked(session_id)
        if (
            current is not None
            and current.account == target
            and current.last_assigned_at == moved_at
            and current.access_fingerprint is None
        ):
            registry.update(
                session_id,
                account=entry.account,
                source=entry.source,
                last_assigned_at=entry.last_assigned_at,
                last_reason=f"{decision.reason}-failed",
                access_fingerprint=None,
            )
    except _STORE_FAILURES as e:
        _logger.warning(
            f"Managed session {session_id}: a move to {target.email} failed "
            f"({result.reason}) and the row could not be put back ({e})"
        )
    _logger.warning(
        f"Managed session {session_id}: could not move to {target.email} "
        f"({result.reason})"
    )
    return ReassignResult(
        session_id, decision.reason, decision.placement.number,
        entry.account, target, False, result.reason,
    )
