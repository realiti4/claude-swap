"""The hooks a managed session runs: ``ensure`` and ``release``.

It fails open, unconditionally. It runs inside somebody's Claude Code
session, so the failure mode that matters is not "the hook did nothing" but
"the hook broke the prompt": a non-zero exit from a ``UserPromptSubmit``
hook blocks the turn, and anything it prints to stdout is injected into the
prompt. So every outcome — including a crash, a lock timeout and Ctrl-C —
is an exit 0 with nothing on stdout, and the story goes to cswap's own log
file. Stderr is quiet in practice but is not part of that guarantee: the
slow path builds a :class:`ClaudeAccountSwitcher`, whose constructor
announces a legacy-to-XDG state-directory migration there, once, on the
Linux and WSL hosts that still have one. It does not reach the prompt.

Silent is not the same as invisible: a hook nobody is watching is exactly
the one whose failures have to be written down, so both verbs configure
the log themselves rather than relying on a switcher they may never
build.

``ensure`` is what lets a managed session keep itself current with no
``cswap auto`` running: it refreshes its own access token and, when the
account it sits on has run out, moves itself to one that has not. The
engine does the same two things on its tick, through the same policy
functions, so the two can never disagree about when a session moves.

It runs on EVERY prompt, so its cost is the design constraint, and the
shape that follows from it is a gate. The common prompt — a token that is
nowhere near expiry on an account with room — reads six small JSON files
(the registry, the profile's credential, settings, the account roster, the
usage store and the engine's quarantine record) and returns. It takes no
lock, spawns no process, makes no network call and does not build a
:class:`ClaudeAccountSwitcher`, whose
construction alone opens the log, runs the one-time data migrations and
opens the door to the roster-wide credential reads behind
``usage_entries_by_account``.

A session running on a token borrowed from the default login is the one
exception, and it is a bounded one: no cheap read can prove that login has
not rotated its grant, because on macOS the rotation lands in a keychain
item the gate will not pay a subprocess to open. So such a session also
reads the login's plaintext credential, which can only ever disprove a
match, and keeps its short-circuit for at most one urgent interval after
the last authoritative answer. Past that it takes the slow path, asks the
keychain, and starts the interval again — once per interval, not once per
prompt.

Past that gate the hook does the expensive work, and it is deliberately
not free: resolving a credential reads the account's store (the keychain
included), and judging a move builds the candidate pool, which probes each
account's profile for a live ``cswap run N`` with ``ps``. That is the
price of a session that would otherwise be stuck, and it is only paid by a
session that is actually near a limit or actually due a new token.

The move half carries a second bound, because "near a limit" is a state a
session can sit in for hours: a sentinel in its own profile records the
reading a pass decided to stay on, and the pass is not re-taken until that
reading moves or the interval runs out. Without it, a fleet where every
account is at its limit would pay the whole ranking on every prompt of
every session to be told again that there is nowhere to go.

Only the escapes are the hook's to make: a session whose account is at its
limit, or one the engine has quarantined and so stopped refreshing. Evening
out the weekly load is the engine's job on its own cadence, and
``UserPromptSubmit`` is the exact moment a session stops being idle, so an
idle move decided here would spend the prompt cache of the turn the user
just started. Both ends of that rule are enforced below: a session whose
account has room is never considered for a move at all, and a move decided
for any other reason is refused. The gate is also why quarantine is the
engine's to act on first — an account can be quarantined and still look
comfortable, and this hook never gets past the gate to notice. Nothing but
the engine quarantines a slot, so such a session always has a daemon whose
next tick moves it.

``release`` is the other end of the same life: a ``SessionEnd`` hook that
hands the account back, so the next launch can place onto it without
waiting for the engine's sweep to notice the process is gone. It runs once
per session rather than once per prompt, so it has no gate and needs none.
What it does have is the opposite constraint: it deletes, and everything
it deletes belongs to somebody. So nothing goes until this session is
established to be over, and every question it cannot answer — an
unreadable registry, an unreadable session record — stops it rather than
being rounded down to "nothing is there". Refusing is a named outcome, not
a failure: the sweep reclaims later whatever this pass declines to.
"""

from __future__ import annotations

import io
import json
import logging
import os
import select
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from claude_swap import oauth, paths, poll_policy, tls
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.logging_config import setup_logging
from claude_swap.managed_refresh import (
    FRESHEN_BUFFER_MS,
    busy_by_slot,
    resolve_access_credential,
)
from claude_swap.managed_sessions import (
    SOURCE_LANE0,
    AccountRef,
    ManagedEntry,
    ManagedSessionRegistry,
    entry_is_live,
    managed_session_id_for,
    read_session_state,
    remove_managed_profile,
)
from claude_swap.process_detection import parent_pid
from claude_swap.session import scan_live_sessions
from claude_swap.session_credentials import write_session_credential
from claude_swap.settings import (
    AutoSwitchSettings,
    load_session_settings,
    load_settings,
    parse_model_names,
)
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.usage_store import UsageStore

_logger = logging.getLogger("claude-swap")

# What a registry or store read can raise on the way, as the refresher and
# the mover each name it: cswap's own errors plus plain I/O from files read
# without a guard.
_STORE_FAILURES = (ClaudeSwitchError, OSError, UnicodeDecodeError)

# What Claude calls the event `release` is the hook for, and how long the
# verb is willing to wait for that event to arrive on stdin. The payload is
# written before the hook is spawned, so the wait only ever covers a slow
# pipe. `isatty` is the cheap first answer, not the whole one: it settles a
# release typed at a terminal without reading anything, while one typed
# into a Bash tool inside a session inherits that session's stdin and is
# answered by the deadline or by the end of the stream instead.
_SESSION_END_EVENT = "SessionEnd"
_STDIN_WAIT_S = 2.0
_STDIN_CHUNK = 65536
# How much of stdin is worth accumulating before giving up on it. A
# SessionEnd payload is a few hundred bytes; a megabyte is four orders of
# magnitude of slack and still small enough to reparse harmlessly. Past it
# the stream is not this event -- an inherited pipe somebody else is
# writing to, most likely -- and the cost of pretending otherwise is
# quadratic: every chunk re-parses everything accumulated so far, so an
# uncapped firehose burns CPU and memory for the whole budget and then
# answers None anyway.
_STDIN_MAX = 1024 * 1024

# The SessionEnd reasons that mean the process this hook runs under is on
# its way out. Claude Code fires SessionEnd for five reasons, and only
# three of them end the process: `logout`, `prompt_input_exit` and `other`
# are the reason the shutdown path passes to its own hook runner (`other`
# is that path's default), while `clear` and `resume` are fired from the
# handlers for `/clear` and `/resume` in a session that goes on running
# afterwards with the same pid, the same profile and the same credential.
# Releasing on those two would delete the account, the keychain item and
# the profile out from under somebody who is still typing.
#
# The list is an ALLOWLIST on purpose: a reason nothing here recognises,
# and a SessionEnd carrying no reason at all, are both refusals. A new
# mid-session reason would otherwise arrive as a deletion.
_ENDING_REASONS = frozenset({"logout", "prompt_input_exit", "other"})

# What `_session_end_reason` returns for a SessionEnd whose `reason` is
# missing or is not a string. Distinct from None, which means no SessionEnd
# reached the hook at all; both refuse, with different warnings.
_REASON_ABSENT = ""

# How far up the process tree `release` looks for the session it belongs
# to. A hook is a child of the Claude that ran it, or a grandchild when a
# shell sits between them; anything deeper is somebody else's business.
_ANCESTRY_DEPTH = 4

# Stamped in the session's own profile when the authoritative resolve last
# confirmed its borrowed token. Empty by design: the mtime is the payload.
LANE0_SENTINEL = ".lane0-checked"

# How long a sentinel stamp holds, for both of them: at most this long may
# pass before the hook re-asks a question it cannot answer cheaply. Two
# numbers rather than one, because they bound different things -- how stale
# a borrowed token may be, and how often the move pass is re-taken -- and
# nothing should retune one by editing the other.
#
# They are their own constants rather than `poll_policy.URGENT_INTERVAL_S`,
# which they happen to match. That number is a USAGE-REFETCH cadence, sized
# against the measured request window of the usage endpoint; retuning it for
# an API reason would silently change how stale a session's token may be and
# falsify what the README promises about it. The value is the same for the
# same underlying reason -- it is the shortest period over which paying for
# subprocesses repeatedly is still reasonable -- and that is a coincidence
# of reasoning, not a dependency.
_LANE0_RECHECK_S = 60.0
_MOVE_RECHECK_S = 60.0

# Stamped in the session's own profile by a move pass that decided to stay.
# Its mtime bounds how often that decision is re-taken, and its contents —
# the stored headroom the decision was made on — are what lets a changed
# reading re-take it sooner. The move pass is the expensive half of this
# hook: a fleet usage read plus a `ps` probe of every candidate account's
# profile. A session whose account is at its limit opens the gate on EVERY
# prompt, and when every account is at its limit the answer is the same
# every time, so without this the hook would pay the whole pass, forever,
# to be told again that there is nowhere to go.
MOVE_SENTINEL = ".move-checked"

# Outcomes of a pass that changed nothing, logged at DEBUG so the default
# level says only what a reader of the log would want to know happened.
# `ensure`'s healthy prompt is the common case, one per prompt per session;
# `release`'s refusals are rarer but each already writes a WARNING of its
# own, which says the same thing better than a second line at INFO would.
_QUIET_OUTCOMES = frozenset({
    "not-managed", "no-entry", "fresh",
    "registry-unreadable", "still-running", "not-a-session-end",
    "session-continues",
})


def _credential_expiry_ms(session_dir: Path) -> float | None:
    """The profile's stored access-token expiry, or None when there is none
    to read. The plaintext file is enough: the keychain item is written from
    the same payload in the same call, and reading it would cost a subprocess
    on every prompt."""
    try:
        raw = (session_dir / ".credentials.json").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    data = oauth.extract_oauth_data(raw)
    expires = data.get("expiresAt") if data else None
    if isinstance(expires, bool) or not isinstance(expires, (int, float)):
        return None
    return float(expires)


def _lane0_probe(entry: ManagedEntry) -> str:
    """What the default login's PLAINTEXT says about the token this session
    borrowed: ``"changed"``, ``"unchanged"`` or ``"unknown"``.

    A cheap probe — a file read and a hash, no keychain subprocess — and
    only one of its three answers is proof. ``changed`` is: the file is
    readable, it names a token, and it is not the one this session holds, so
    lane 0 has certainly moved on and the gate must open whatever else is
    true. ``unchanged`` is not proof, because on macOS Claude Code writes a
    rotation to the keychain item alone and cswap rewrites that plaintext
    just when one is already there (see ``credentials.py``), so the file can
    hold a generation the login stopped serving.

    ``unknown`` is the answer that has to exist. Keychain-only logins never
    have this file at all — neither Claude Code nor cswap creates one where
    there is none, deliberately, so a fileless posture stays fileless — and
    reading "no file" as "changed" would send every prompt of such a session
    down the slow path forever, which is precisely the cost this gate
    exists to avoid. So ``unknown`` carries no information and leaves the
    decision to :func:`_lane0_checked_recently` alone. The staleness bound
    is the same either way: at most one interval behind an authoritative
    resolve. What ``unknown`` costs is the early warning ``unchanged``
    sometimes gets for free.

    Deliberately not ``paths.get_credentials_path``: the hook runs with
    ``CLAUDE_CONFIG_DIR`` naming the MANAGED profile, so that one would
    fingerprint the session's own copy and match itself every time.
    """
    if entry.access_fingerprint is None:
        return "unknown"
    path = paths.get_default_claude_config_home() / ".credentials.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return "unknown"
    fingerprint = oauth.access_token_fingerprint(raw)
    if fingerprint is None:
        # A file with no access token in it says nothing about what the
        # login is serving; only the keychain can answer that.
        return "unknown"
    return "unchanged" if fingerprint == entry.access_fingerprint else "changed"


def _lane0_checked_recently(session_dir: Path, now: float) -> bool:
    """Whether an authoritative lane-0 resolve ran within the last interval.

    The sentinel is an empty file whose mtime is the whole payload, stamped
    by :func:`_mark_lane0_checked` each time the keychain-first resolve past
    the gate confirms this session is holding lane 0's current token. It is
    what bounds the staleness the cheap plaintext probe cannot rule out: a
    borrowed token can be at most one interval behind a keychain-only
    rotation, at a cost of one slow pass per interval rather than per prompt.

    Anything unreadable, missing, or stamped in the future (a clock that
    went backwards) answers False, which opens the gate: the expensive
    direction is the safe one.
    """
    try:
        stamped = (session_dir / LANE0_SENTINEL).stat().st_mtime
    except OSError:
        return False
    return 0.0 <= (now - stamped) < _LANE0_RECHECK_S


def _mark_lane0_checked(session_dir: Path) -> None:
    """Stamp the sentinel: lane 0 was just asked authoritatively, and this
    session is holding what it answered.

    Best-effort. A stamp that cannot be written only costs this session its
    short-circuit on the next prompt, which is the same thing a missing
    sentinel already means.
    """
    try:
        (session_dir / LANE0_SENTINEL).touch()
    except OSError as e:
        _logger.debug(f"could not stamp {LANE0_SENTINEL}: {e}")


def _headroom_key(headroom: float | None) -> str:
    """The stored headroom as the move sentinel records it.

    A string so "no reading at all" is a value like any other rather than a
    number that compares equal to something.
    """
    return "unknown" if headroom is None else f"{headroom:.6f}"


def _move_checked_recently(
    session_dir: Path, now: float, headroom: float | None
) -> bool:
    """Whether a move pass has already decided, recently, on this reading.

    Two ways out of the skip, and a session needs only one: the interval
    runs out, or the account's stored headroom moves. The interval is what
    keeps the pass running at all when nothing else changes — the usage
    refetch that would move the reading lives INSIDE the pass, so a skip
    that only ended on a changed reading would never end at all.

    Anything unreadable, missing, or stamped in the future answers False,
    which runs the pass: the expensive direction is the safe one.
    """
    sentinel = session_dir / MOVE_SENTINEL
    try:
        stamped = sentinel.stat().st_mtime
        recorded = sentinel.read_text()
    except OSError:
        return False
    if not 0.0 <= (now - stamped) < _MOVE_RECHECK_S:
        return False
    return recorded == _headroom_key(headroom)


def _mark_move_checked(session_dir: Path, headroom: float | None) -> None:
    """Stamp the move sentinel: this reading has been judged and the session
    stays where it is.

    Best-effort, like the lane-0 stamp: a sentinel that cannot be written
    only costs this session the skip on its next prompt.
    """
    try:
        (session_dir / MOVE_SENTINEL).write_text(_headroom_key(headroom))
    except OSError as e:
        _logger.debug(f"could not stamp {MOVE_SENTINEL}: {e}")


def _account_slot(backup_dir: Path, account: AccountRef) -> str | None:
    """The slot number holding ``account``, straight out of the roster file.

    ``managed_refresh.slot_for_account`` answers the same question, but it
    needs a :class:`ClaudeAccountSwitcher`, and building one is exactly
    what the gate below exists to avoid. The match itself is still the
    switcher's own, so the two cannot drift apart. An unreadable roster
    answers None, which costs the fast path its short-circuit and nothing
    else.
    """
    try:
        data = json.loads((backup_dir / "sequence.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return ClaudeAccountSwitcher._find_account_slot(
        data, account.email, account.organization_uuid
    )


def _stored_headroom(
    backup_dir: Path,
    account: AccountRef,
    number: str | None,
    models: tuple[str, ...],
) -> tuple[float | None, float | None]:
    """``(headroom, age)`` for one account, read from the usage store alone.

    Deliberately not ``switcher.usage_entries_by_account``: that builds the
    whole roster first, reading every account's stored credential (and the
    active one's keychain item) to do it. This reads the one row, guarded
    by the same stored identity, and never fetches. Both are None when
    nothing usable is stored, which the gate reads as "look closer".
    """
    if number is None:
        return None, None
    identity = (account.email, account.organization_uuid)
    entry = UsageStore(backup_dir / "cache").entries({number: identity}, models)[number]
    value = entry.decision_value()
    headroom = oauth.account_headroom(value if isinstance(value, dict) else None, models)
    return headroom, entry.age_s


def _has_margin(headroom: float | None, *, threshold: float) -> bool:
    """Whether this account's stored headroom is comfortably clear of the
    configured threshold — known, and further from the binding limit than
    the threshold the engine itself switches on.

    Unknown headroom is never comfortable. An account hosting a live
    session whose usage nobody has managed to read is precisely the one
    worth looking at.
    """
    return headroom is not None and headroom >= (100.0 - threshold)


def _urgent_refetch_due(
    headroom: float | None, age_s: float | None, *, threshold: float
) -> bool:
    """Whether this account's usage is worth a network call right now.

    Claude's session record carries no rate-limit signal, so an at-limit
    session is only visible through the usage store. Near the threshold —
    or with nothing trustworthy stored at all — the stored value is the
    thing the decision turns on and the engine's own cadence may be a
    minute behind, so the hook pays for one fetch, bounded by the same
    urgent interval the poll planner uses so a session prompted in a tight
    loop cannot hammer the endpoint.
    """
    if _has_margin(headroom, threshold=threshold):
        return False
    return age_s is None or age_s > poll_policy.URGENT_INTERVAL_S


def _is_quarantined(backup_dir: Path, number: str | None) -> bool:
    """Whether the engine has stopped using the slot this session is on.

    Imported where it is called rather than at the top of the module: the
    document belongs to the engine, and reaching for the engine's module
    costs a tenth again of what importing this hook costs at all. One small
    JSON read either way, which is what every other term in the gate above
    it is too.

    An account with no slot cannot be quarantined by number, and it has
    bigger problems than this -- the roster no longer lists it, which the
    refresh path reports on its own.
    """
    if number is None:
        return False
    from claude_swap.autoswitch import quarantined_numbers

    return number in quarantined_numbers(backup_dir)


def run_ensure(
    *,
    env: Mapping[str, str] | None = None,
    clock: Callable[[], float] = time.time,
) -> str:
    """One ensure pass. Returns a short outcome word for the log.

    Raises whatever goes wrong; :func:`ensure` is what swallows it.
    """
    environ = os.environ if env is None else env
    backup_dir = paths.get_backup_root()
    session_id = managed_session_id_for(environ.get("CLAUDE_CONFIG_DIR"), backup_dir)
    if session_id is None:
        return "not-managed"
    session_dir = Path(environ["CLAUDE_CONFIG_DIR"])

    registry = ManagedSessionRegistry(backup_dir)
    # Read quietly: `_read`'s warning is sized for a command somebody is
    # watching, and this runs on every prompt of every managed session. A
    # registry that cannot be parsed would otherwise write the same WARNING
    # several times a minute, for as long as it stayed that way, and push
    # the engine's own history out of the rotating log. The two cases are
    # still told apart — by a second read, taken only on the rare None.
    entry = registry.get(session_id, warn=False)
    if entry is None:
        # No row: the sweep dropped it (this session's pid was recycled, or
        # the registry was reset). Writing a credential without one is
        # exactly what write_session_credential refuses, so stop here.
        if registry.unreadable_reason() is not None:
            return "registry-unreadable"
        return "no-entry"

    now = clock()
    now_ms = now * 1000.0
    expires_at = _credential_expiry_ms(session_dir)
    stale_token = expires_at is None or (expires_at - now_ms) <= FRESHEN_BUFFER_MS
    # A borrowed token is not this session's to age out: lane 0's Claude
    # rotates the grant on its own schedule, and a rotation landing before
    # the copy's own expiry leaves this session holding a token lane 0 has
    # already replaced, so the stored expiry cannot decide this one.
    #
    # Nothing cheap can decide it either, only bound it. The plaintext probe
    # settles the visible half, and the sentinel puts a deadline on the
    # invisible one: on macOS a rotation can land in lane 0's keychain item
    # alone, where the probe cannot see it, so a match is believed for one
    # interval and then re-established the only way it can be — by asking
    # the keychain past the gate. A probe that knows nothing (no plaintext
    # at all, as on a keychain-only login) leaves the deadline to decide by
    # itself; only a genuine mismatch overrides it.
    #
    # A token borrowed from a sibling `cswap run N` has the same problem and
    # is not checked here: finding which profile holds that account means
    # probing every candidate pid with `ps`, which is precisely the cost this
    # gate exists to keep off a prompt. Such a session waits for the engine's
    # next tick to notice the rotation.
    recopy_due = entry.source == SOURCE_LANE0 and (
        _lane0_probe(entry) == "changed"
        or not _lane0_checked_recently(session_dir, now)
    )

    settings = load_settings(backup_dir)
    models = parse_model_names(settings.model)
    number = _account_slot(backup_dir, entry.account)
    headroom, age_s = _stored_headroom(backup_dir, entry.account, number, models)
    comfortable = _has_margin(headroom, threshold=settings.threshold)
    # Headroom is not the only thing that can make an account unusable. A
    # quarantined slot is one `cswap auto` stopped using because refreshing
    # it kept failing, and nothing refreshes it while it stays that way, so
    # a session sitting on one runs out of token rather than out of quota --
    # comfortably, right up to the moment it stops working. That is a move
    # this hook makes, so it has to be a reason the gate opens.
    quarantined = _is_quarantined(backup_dir, number)
    if not stale_token and not recopy_due and comfortable and not quarantined:
        return "fresh"

    # Past the gate this can reach the network — an urgent usage refetch, a
    # backup-grant refresh POST — and `cswap session` is dispatched before
    # main() installs the OS-native TLS verifier. Install it here instead, so
    # the fast path still pays nothing for it and the one path that can open a
    # connection gets the same trust decisions every other command gets.
    tls.use_native_tls()

    switcher = ClaudeAccountSwitcher()
    outcome = "fresh"
    if stale_token or recopy_due:
        outcome = _refresh(
            switcher, registry, entry, session_dir,
            now_ms=now_ms, number=number, rewrite=stale_token,
        )
    if comfortable and not quarantined:
        # An account with room that the engine has not stopped using is not
        # a move candidate under any rule this hook applies, so the whole
        # move pass — the fleet usage read and the `ps` probes of the
        # candidate pool with it — is skipped for a prompt that came here
        # only to refresh a token.
        return outcome

    # `_refresh` can have rewritten this row's holder or fingerprint, and the
    # move pass hands its own rollback the entry it was given — so a stale one
    # would put back a holder the refresh had just corrected. Re-read it; a row
    # the sweep dropped while this pass ran is no longer ours to move. Below
    # the skip above, since the move pass is the only thing that reads it.
    entry = registry.get(session_id)
    if entry is None:
        return outcome
    if _move_checked_recently(session_dir, now, headroom):
        # Judged already, on this reading, inside the interval. Nothing
        # stands behind that but the interval itself: these hooks exist for
        # the session with no `cswap auto` running, so the most anything
        # changing in between costs is one interval of delay, and a reading
        # that moves re-opens the question at once.
        return outcome
    moved = _maybe_reassign(
        switcher, registry, entry, session_dir,
        settings=settings, models=models, now=now,
        number=number, headroom=headroom, age_s=age_s,
    )
    if moved is None:
        _mark_move_checked(session_dir, headroom)
    return moved or outcome


def _refresh(
    switcher: ClaudeAccountSwitcher,
    registry: ManagedSessionRegistry,
    entry: ManagedEntry,
    session_dir: Path,
    *,
    now_ms: float,
    number: str | None,
    rewrite: bool,
) -> str:
    """Bring this session's token up to its holder's current one.

    ``rewrite=False`` is the borrowed-token check: resolve what the holder
    serves now and write only when it differs from what this session was
    last recorded holding. That comparison is the engine's own
    (``push_refresh`` skips a session whose fingerprint already matches),
    so a lane 0 that has not rotated costs a credential read and no write.

    ``number`` is the slot the caller already looked up, passed through as
    ``push_refresh`` passes its own. A None is NOT forwarded as a known
    answer: the cheap roster read that produced it cannot tell "no slot
    holds this account" from "the roster could not be read", and only the
    switcher's guarded reader can, so that case is left to re-derive it and
    report ``transient`` rather than ``account-gone``.
    """
    resolution = resolve_access_credential(
        switcher, entry.account, now_ms=now_ms, buffer_ms=FRESHEN_BUFFER_MS,
        number=number, number_known=number is not None,
    )
    if (
        resolution.status != "ok"
        or resolution.credential is None
        or resolution.oauth_account is None
    ):
        return f"refresh-failed:{resolution.status}"
    borrowed_from_lane0 = resolution.source == SOURCE_LANE0
    if not rewrite and entry.access_fingerprint == oauth.access_token_fingerprint(
        resolution.credential
    ):
        if borrowed_from_lane0:
            _mark_lane0_checked(session_dir)
        elif entry.source != resolution.source:
            # The same token, a different holder: the account this session
            # borrowed from is no longer the default login, so the row's
            # `lane0` would otherwise stand for good and send every later
            # prompt down this path looking for a rotation that cannot
            # happen. Nothing was written, so only the holder is recorded.
            _record(registry, entry, source=resolution.source)
        return "fresh"
    result = write_session_credential(
        session_dir, entry.account, resolution.credential,
        resolution.oauth_account, registry=registry,
    )
    if not result.ok:
        return f"refresh-failed:{result.reason}"
    if borrowed_from_lane0:
        # Written from what lane 0 serves right now, so the same deadline
        # starts again here: whether the resolve found the session already
        # current or had to copy, lane 0 was asked authoritatively just now.
        _mark_lane0_checked(session_dir)
    _record(
        registry, entry,
        access_fingerprint=result.fingerprint, source=resolution.source,
    )
    return "refreshed"


def _record(registry: ManagedSessionRegistry, entry: ManagedEntry, **changes) -> None:
    """Write what just happened to the registry row, or carry on without it.

    The token is already live in the session by the time this runs; only
    recording it can still fail (a lock timeout, a rewrite error). Never
    raised: the next pass compares against the unrecorded old fingerprint
    and repeats the same write, which is harmless, whereas raising here
    would cancel the at-limit move this prompt may be about to make — the
    half a stuck session actually needs.
    """
    try:
        registry.update(entry.session_id, **changes)
    except _STORE_FAILURES as e:
        _logger.warning(
            f"Managed session {entry.session_id}: brought the token up to "
            f"date but could not record it ({e})"
        )


def _maybe_reassign(
    switcher: ClaudeAccountSwitcher,
    registry: ManagedSessionRegistry,
    entry: ManagedEntry,
    session_dir: Path,
    *,
    settings: AutoSwitchSettings,
    models: tuple[str, ...],
    now: float,
    number: str | None,
    headroom: float | None,
    age_s: float | None,
) -> str | None:
    """Move this session off an account that cannot serve it, or None when it
    should stay. The hook's half of the manager, using the same policy the
    engine uses so the two can never disagree about when a session moves."""
    from claude_swap.autoswitch import quarantined_numbers
    from claude_swap.balance import params_from_settings
    from claude_swap.managed_launch import _candidate_identities, _decision_usage
    from claude_swap.session_reassign import (
        REASON_AT_LIMIT,
        REASON_QUARANTINED,
        apply_reassignment,
        plan_for_entry,
    )

    fetch: set[str] = set()
    if number is not None and _urgent_refetch_due(
        headroom, age_s, threshold=settings.threshold
    ):
        fetch = {number}
    usage = _decision_usage(switcher, fetch=fetch)

    # Busy counts WITHOUT a liveness pass: one `ps` per registry row is a
    # cost this hook can avoid, unlike the candidate-pool probe below. A
    # row whose process just exited is swept within a tick, and counting it
    # one pass too long only makes its account look busier — the
    # conservative direction.
    busy = busy_by_slot(switcher, registry.busy_counts(registry.entries().values()))

    decision = plan_for_entry(
        switcher, entry,
        state=read_session_state(session_dir, entry.pid),
        usage=usage, busy=busy,
        identities=_candidate_identities(switcher),
        lane0=switcher.current_account_number(),
        now=now, models=models,
        params=params_from_settings(settings),
        # A second read of settings.json, and it stays here: hoisting it
        # beside load_settings would put it in the fast path, where a
        # prompt that decides nothing would pay for a section only a move
        # ever reads.
        sessions_settings=load_session_settings(switcher.backup_dir),
        # The engine's own set of stopped slots. Only the engine writes it,
        # but the document outlives the daemon that wrote it, so finding
        # oneself on a quarantined slot is no evidence that anything else is
        # running to fix it. The same set is read at the gate above, which
        # is what brings a comfortable session here at all; this is the read
        # the decision itself is made on.
        quarantined=quarantined_numbers(switcher.backup_dir),
    )
    if decision is None or decision.reason not in (
        REASON_AT_LIMIT, REASON_QUARANTINED
    ):
        # The idle rule is the engine's, not the hook's (see the module
        # docstring). Refused here rather than merely not reached, because
        # the gate above opens for a token refresh too, and a long-idle
        # session on a merely-busy account would otherwise be moved at the
        # instant the user pressed enter.
        return None
    result = apply_reassignment(
        switcher, registry, entry, decision,
        now_ms=now * 1000.0, buffer_ms=FRESHEN_BUFFER_MS,
    )
    if result.ok:
        return f"reassigned:{result.reason}"
    return f"reassign-failed:{result.detail}"


def _this_session_is(pid: int) -> bool:
    """Whether ``pid`` is this process or one of the processes that started
    it, within a few generations.

    ``release`` runs from inside the session it is releasing, so the row it
    is about to drop names a process that is very much alive: this one's
    parent, or its parent's parent when a shell ran the hook. The usual
    liveness question ("has it gone?") therefore has to be joined by this
    one ("is it us?"), and only the two together mean the session is over.

    Bounded, and generous about stopping. The walk costs a ``ps`` per
    generation past the first, and two different things end it early: an
    answer nobody can give (Windows, a parent already reaped and so absent
    from ``ps``, a ``ps`` that would not run), and an answer of 1, which
    means the chain has been reparented to init and the process that
    started this one is gone. Both are "no", which leaves the profile for
    the sweep instead of removing it here.
    """
    if pid in (os.getpid(), os.getppid()):
        return True
    ancestor: int | None = os.getppid()
    for _ in range(_ANCESTRY_DEPTH):
        ancestor = parent_pid(ancestor)
        if ancestor is None or ancestor <= 1:
            return False
        if ancestor == pid:
            return True
    return False


def _session_end_reason() -> str | None:
    """Why Claude says this session is ending, or ``None`` when it does not
    say that at all.

    Claude hands a hook its event on stdin as a JSON object; ``release``
    reads it for two fields. The event name is what tells a ``SessionEnd``
    hook apart from somebody running the same command inside the session it
    would delete — a Bash tool, a subshell, a person in a split pane — all
    of which have exactly the ancestry the hook has. The reason is what
    tells an ending session apart from one that carries on afterwards; the
    caller judges it against ``_ENDING_REASONS``.

    Returns ``_REASON_ABSENT`` for a ``SessionEnd`` that names no reason,
    which the caller refuses like any reason it does not recognise.

    Every uncertainty is "no", and nothing here is allowed to raise or to
    wait: a terminal on stdin is answered without reading it at all, and
    everything else is read under one deadline. The cost of a wrong "no" is
    a profile left for the sweep, which is the state the sweep already
    exists for; the cost of a wrong "yes" is a live session losing its
    credential mid-turn.
    """
    try:
        payload = _hook_event()
    except Exception:  # noqa: BLE001 - any answer but a clean one is "no"
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("hook_event_name") != _SESSION_END_EVENT
    ):
        return None
    reason = payload.get("reason")
    return reason if isinstance(reason, str) else _REASON_ABSENT


def _hook_event() -> object | None:
    """Whatever Claude wrote on stdin, or ``None`` inside ``_STDIN_WAIT_S``.

    One deadline governs the whole read, not just its first byte. ``select``
    answers for that first byte only, and a plain ``read()`` answers at EOF
    — which on a pipe means when the write end closes, and nothing promises
    that ever happens: a wrapper script, a fifo, or a shell holding the
    write end open in a sibling would leave the hook blocked until Claude
    killed it, stalling the teardown it is part of.

    So the budget is spent in a loop: wait on what is left of it, take the
    chunk that arrived, and try to parse everything that has accumulated —
    stopping the moment it parses, which is usually the first pass. The
    accumulation is what makes a payload delivered in pieces readable; a
    single read would truncate it into a parse failure.

    A deadline that passes, a stream that ends with nothing parseable,
    bytes that never parse, and more than ``_STDIN_MAX`` of them all answer
    ``None``, which the caller reads as "not a SessionEnd" — the direction
    that keeps a session's credential.
    """
    stream = sys.stdin
    if stream is None or stream.isatty():
        return None
    # What to read from: a real stream's raw half, so that a read takes
    # what has arrived rather than waiting for the end of the stream. An
    # in-memory one (tests, an embedding host) has no such half and cannot
    # block, so it is read whole in a single pass instead.
    raw = getattr(stream, "buffer", None)
    deadline = time.monotonic() + _STDIN_WAIT_S
    payload = b""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            ready = bool(select.select([stream], [], [], remaining)[0])
        except (OSError, ValueError, io.UnsupportedOperation):
            # An in-memory stream has nothing to wait on and cannot block,
            # so reading it anyway is safe and is the only way it is ever
            # read. A descriptor that could not be waited on is the other
            # way round: reading it is the one thing that might never come
            # back, and there is no budget left to enforce once `select`
            # is out of the picture, so it answers "no" instead.
            if raw is not None:
                return None
            ready = True
        if not ready:
            return None
        chunk = raw.read1(_STDIN_CHUNK) if raw is not None else stream.read()
        if not chunk:
            return None
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8", "surrogatepass")
        payload += chunk
        if len(payload) > _STDIN_MAX:
            return None
        try:
            return json.loads(payload)
        except ValueError:
            continue


def run_release(*, env: Mapping[str, str] | None = None) -> str:
    """One release pass: hand this session's account back.

    The row goes first and the profile second, exactly as the sweep does
    it: a row pointing at a directory that is already gone would keep an
    account looking occupied and would make the next writer for that id
    fail in a way nothing reclaims, while a directory left behind with no
    row is an orphan the sweep knows how to collect.

    Nothing is removed until this session is established to be over, which
    takes one of two answers. Either the process the row names has exited —
    proof on its own, whoever is asking — or it is still running, it is the
    process this hook runs under, AND this really is that session ending:
    a ``SessionEnd`` payload on stdin, carrying a reason that means the
    process is going away (``_ENDING_REASONS``). Ancestry alone is not
    enough, because a shell inside a running Claude has the very same
    ancestry, and a person or an agent typing this there would otherwise
    delete the credential out from under the session they are typing in.
    The reason is not ceremony either: ``/clear`` and ``/resume`` fire
    SessionEnd at a session that goes straight on running, and a release
    there would take the account, the keychain item and the profile away
    from somebody mid-conversation. Ending the session is the
    deliberate way to release it — there is no command that takes one down
    from inside it, and ``cswap purge``, which removes everything cswap
    owns, refuses outright while any managed session is live.

    A ``CLAUDE_CONFIG_DIR`` naming somebody else's live profile is not a
    session ending either; a registry nobody can read is not evidence of
    anything at all; and both stop the pass rather than being guessed past.
    The reservation exists before the profile does and a Claude writes its
    own record only once it is up, so "no record yet" must never be read as
    "nothing is using this".

    The directory itself only goes when nothing OTHER than this session's
    own process has a live record in the profile: a Bash-tool child or a
    detached process that inherited ``CLAUDE_CONFIG_DIR`` goes on reading
    it, and deleting the credential under it would break work the user did
    not end. ``session.profile_is_quiescent`` cannot be used as-is for that
    — this hook runs inside the exiting Claude, whose own record is still
    there — but its rule about unreadable records is kept: not knowing is
    not the same as knowing nothing is there.
    """
    environ = os.environ if env is None else env
    backup_dir = paths.get_backup_root()
    session_id = managed_session_id_for(environ.get("CLAUDE_CONFIG_DIR"), backup_dir)
    if session_id is None:
        return "not-managed"

    registry = ManagedSessionRegistry(backup_dir)
    unreadable = registry.unreadable_reason()
    if unreadable is not None:
        # `get` folds "cannot read" into "no such row", which is the right
        # trade for a poller and the wrong one for the two deletions below.
        _logger.warning(
            f"Managed session {session_id}: the registry cannot be read "
            f"({unreadable}); releasing nothing."
        )
        return "registry-unreadable"
    entry = registry.get(session_id)
    if entry is None:
        return "no-entry"
    if entry_is_live(entry):
        if not _this_session_is(entry.pid):
            _logger.warning(
                f"Managed session {session_id}: its process {entry.pid} is "
                "running and is not the one asking; releasing nothing."
            )
            return "still-running"
        reason = _session_end_reason()
        if reason is None:
            _logger.warning(
                f"Managed session {session_id}: its process {entry.pid} is "
                "still running and no SessionEnd reached this hook on stdin "
                "— either it is not that hook, or the event did not arrive "
                f"within {_STDIN_WAIT_S:g}s; releasing nothing. Ending the "
                "session is what releases it."
            )
            return "not-a-session-end"
        if reason not in _ENDING_REASONS:
            shown = reason or "none given"
            _logger.warning(
                f"Managed session {session_id}: SessionEnd ({shown}) leaves "
                f"its process {entry.pid} running, so its account is still "
                "in use; releasing nothing."
            )
            return "session-continues"

    if registry.remove(session_id) is None:
        # The sweep got there between the read and the write. It owns the
        # profile from here: it drops the row and reclaims the directory
        # under the same rules this would have.
        return "no-entry"
    session_dir = registry.session_dir(session_id)
    sessions, unreadable_records = scan_live_sessions(session_dir)
    if unreadable_records or any(s.pid != entry.pid for s in sessions):
        return "left-in-use"
    if not remove_managed_profile(session_dir):
        return "left-behind"
    return "released"


def ensure() -> int:
    """``cswap session ensure``. Always 0, never a word on stdout.

    Configures cswap's log first: the common prompt never builds a
    switcher, and nothing else on this path sets the logger up, so without
    this a hook that fails on every prompt forever would leave no trace
    anywhere. What it records is deliberately lopsided. A pass that changed
    something, and a pass that failed, are worth a line at the default
    level; the healthy prompt is not, and at ``INFO`` it would append to the
    shared rotating log several times a minute and push the engine's own
    history out of it. At ``DEBUG`` the handler's lazy open means the
    healthy prompt does not so much as touch the file.

    ``BaseException`` on purpose: a ``KeyboardInterrupt`` landing in the hook
    while it waits on a lock must end the hook, not the user's prompt.
    """
    return _report("ensure", run_ensure)


def release() -> int:
    """``cswap session release``. Always 0, never a word on stdout.

    The same discipline as :func:`ensure`, for the same reason: this one
    runs from ``SessionEnd``, where a non-zero exit is less dangerous than
    it is on a prompt but is still a hook reporting a failure nobody asked
    it about. What it did is worth a line; finding nothing to do is not.
    """
    return _report("release", run_release)


def _report(verb: str, run: Callable[[], str]) -> int:
    """Run one hook pass, write down what came of it, and exit 0 regardless.

    Shared by both verbs so neither can drift from the other on the part
    that matters when something goes wrong.
    """
    configured = False
    try:
        setup_logging(paths.get_backup_root())
        configured = True
        outcome = run()
    except BaseException as e:  # noqa: BLE001 - fail open, unconditionally
        if not configured:
            # The setup itself is what failed, and it clears the handlers
            # before installing its own — so this logger may have none, and
            # `logging.lastResort` would put the warning on stderr. The one
            # stderr line this module tolerates is the switcher's own
            # migration notice; a diagnostic of its own is not that, and
            # putting one there over a failure is the least useful moment to
            # start writing to a UserPromptSubmit hook's terminal.
            _logger.addHandler(logging.NullHandler())
            _logger.propagate = False
        _logger.warning(f"session {verb} failed: {type(e).__name__}: {e}")
        return 0
    level = logging.DEBUG if outcome in _QUIET_OUTCOMES else logging.INFO
    _logger.log(level, f"session {verb}: {outcome}")
    return 0
