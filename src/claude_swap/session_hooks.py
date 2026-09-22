"""The hooks a managed session runs: ``cswap session ensure``.

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
the one whose failures have to be written down, so :func:`ensure`
configures the log itself rather than relying on a switcher it may never
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
"""

from __future__ import annotations

import json
import logging
import os
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
    managed_session_id_for,
    read_session_state,
)
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

# Outcomes of a pass that changed nothing. They are the common case — one
# per prompt per session — so they are logged at DEBUG, leaving the default
# level to say only what a reader of the log would want to know happened.
# A registry nobody can read is one of them for the same reason it is read
# without a warning: on this path it would repeat for every prompt of every
# session until somebody fixed the file.
_QUIET_OUTCOMES = frozenset({
    "not-managed", "no-entry", "fresh", "registry-unreadable",
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
    configured = False
    try:
        setup_logging(paths.get_backup_root())
        configured = True
        outcome = run_ensure()
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
        _logger.warning(f"session ensure failed: {type(e).__name__}: {e}")
        return 0
    level = logging.DEBUG if outcome in _QUIET_OUTCOMES else logging.INFO
    _logger.log(level, f"session ensure: {outcome}")
    return 0
