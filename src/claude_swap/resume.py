"""Auto-continue: carry on a Claude Code session that a usage limit stopped,
as soon as there is capacity again.

1. **Arm.** Claude Code's ``StopFailure`` hook (``cswap resume install``) runs
   ``cswap resume arm`` when a turn ends on a rate limit. ``arm`` records the
   stop under ``<backup_root>/resume/<session_id>.json`` — reset time and limit
   type from the transcript, plus the account the session was on — and spawns
   one detached waiter per session. The hook fires repeatedly for one limit
   (helper queries, subagents), so ``arm`` is idempotent: a stop it already
   noted changes nothing, and a live waiter picks up an updated record.

2. **Wait.** The waiter (``cswap resume wait``) is passive by design: it never
   fetches usage or switches accounts (N waiters polling the usage API at once
   is the burst pattern behind the 2026-08-03 incident — that stays the single
   ``cswap auto`` loop's job). It reads only local state — the transcript, the
   session's status file, the live login, cswap's cached usage and the auto
   engine's last switch — and resumes when the limit has reset, or when the
   session's account changed since the stop and the new one isn't known to be
   exhausted. It gives up when the session closes, moves on by itself, or
   ``MAX_WAIT_S`` passes.

3. **Deliver.** The message goes into the session's own cross-session inbox,
   authenticated with the own-child token the hook process inherited, so
   Claude Code treats it as the session's own hook posting back — delivered
   even when the session bypasses permission prompts. An idle session starts a
   new turn with it.

Loop guard: a stop that follows one of our resumes with no real output in
between counts as consecutive; ``resume.maxConsecutive`` in a row and the
session is left alone (with a notification) instead of retried forever.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from claude_swap import transcript as tx
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.inbox import InboxError, inbox_address, peer_token, post_message
from claude_swap.locking import FileLock
from claude_swap.paths import get_claude_config_home, get_global_config_path
from claude_swap.process_detection import ClaudeSession, list_sessions
from claude_swap.settings import ResumeSettings, atomic_write_json, load_resume_settings

if TYPE_CHECKING:
    from claude_swap.switcher import ClaudeAccountSwitcher

RESUME_DIRNAME = "resume"
POLL_S = 30.0  # waiter cadence while capacity isn't back
SLICE_S = 2.0  # how often a sleeping waiter looks for a `resume now` request
# Spread resumes that become due together (several sessions stopped on the
# same limit), so they don't all land on the fresh window in the same second.
JITTER_S = 20.0
MAX_WAIT_S = 8 * 24 * 3600.0  # a full weekly window, plus margin
# Same-account "quota is back" evidence: a usage reading taken this long after
# the stop, with the limited window at or below this pct. Conservative on
# purpose — at 99-100% the API is still refusing.
HEADROOM_EVIDENCE_AFTER_S = 60.0
HEADROOM_MAX_PCT = 98.0
RECORD_RETENTION_S = 7 * 24 * 3600.0
# A held or paused waiter only waits for `resume now`/`on`/`cancel`, the
# session closing or the user typing: no reason to look more often.
HELD_POLL_S = 120.0
# The hook can run before the stop is flushed to the transcript, which then
# still ends on earlier output. Only output *after* the stop (or after
# arming) proves the session moved on; past this grace with no stop showing
# up at all, the event was about something else (a helper query, a
# subagent) and the waiter stands down.
ARM_GRACE_S = 120.0
# StopFailure also fires for helper queries and subagents while the main
# turn keeps going; after a waiter saw the session moving, don't spawn
# another for this long unless the transcript really shows a stop.
RESPAWN_DAMPING_S = 60.0
# A stop logged this soon after `cswap auto` switched may still be the old
# account's: a running session picks new credentials up on its next request,
# and macOS caches them for ~30 s.
SWITCH_SETTLE_S = 60.0
# How long a stop with no recorded reset counts (one five-hour window).
UNTIMED_STOP_TRUST_S = 5 * 3600.0

WAITING = "waiting"
RESUMED = "resumed"
GAVE_UP = "gave-up"
UNDELIVERED = "undelivered"  # sent, but the session never recorded it (held/refused)
SESSION_GONE = "session-gone"
MOVED_ON = "moved-on"  # carried on without us: the user, or a built-in auto-continue
TIMED_OUT = "timed-out"
CANCELLED = "cancelled"  # `cswap resume cancel`: the user said not to continue it

# A stopped session is "idle", or "waiting" while a limit dialog is open
# (terminal UI); delivery additionally requires the transcript to end on the
# stop itself, so a session waiting on a permission prompt is never poked.
DELIVERABLE_STATUSES = (None, "idle", "waiting")
# How long the session gets to record a delivered message before we conclude
# its inbound controls held or refused it, and the clock tolerance applied.
CONFIRM_TIMEOUT_S = 60.0
# A message that lands while the session is busy (say the user typed at the
# same moment) is queued, and Claude Code records it only when the turn
# consumes it - minutes later on a long turn. Keep waiting while it's busy.
CONFIRM_BUSY_MAX_S = 30 * 60.0
BUSY_STATUSES = ("busy", "waiting")
CONFIRM_POLL_S = 2.0
CONFIRM_SKEW_S = 5.0

MARKER = "[claude-swap auto-continue]"
# Written so that continuing a session nobody wanted continued costs one short
# turn, not unwanted changes: the user may have finished, abandoned or redone
# the task while it was stopped (history: ~8% of stops were never returned to).
MESSAGE = (
    MARKER + " The usage limit that stopped this session has cleared ({why}). "
    "Continue the task you were working on, but first re-check the current state "
    "of the files you were changing: time has passed, and they may have been "
    "edited since. If the task is already finished, or no longer makes sense given "
    "what you find, say so in one line and stop without making further changes. "
    "If you were waiting for an answer from the user, ask again briefly instead of "
    "guessing."
)
REHEARSAL_MESSAGE = (
    MARKER + " TEST: this is a rehearsal started by `cswap resume test`, proving "
    "this session can be woken after a usage limit. There is no task to "
    "continue; just reply with one short line confirming you received it."
)
REASON_TEXT = {
    "limit-reset": "the limit window reset",
    "account-switched": "claude-swap switched to an account with quota left",
    "headroom": "the account has quota again",
    "forced": "you asked claude-swap to continue it",
    "rehearsal": "auto-continue test passed",
}

_SESSION_ID = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_-]{7,63}$")

_logger = logging.getLogger("claude-swap")


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResumeRecord:
    """Everything known about one session's latest usage-limit stop."""

    session_id: str
    transcript_path: str
    config_home: str
    global_config_path: str
    # False for a non-default config home (`cswap run` profiles): account
    # switches never apply there, only the limit's own reset does.
    default_profile: bool
    armed_at: float
    socket_path: str | None = None
    cwd: str | None = None
    identity_email: str | None = None  # the account the session was on at the stop
    identity_org: str | None = None
    stall_uuid: str | None = None
    stalled_at: float | None = None
    resets_at: float | None = None
    limit_type: str | None = None
    state: str = WAITING
    detail: str = ""
    consecutive: int = 0  # our resumes in a row that hit the limit again
    last_resume_at: float | None = None
    resumed_stall_uuid: str | None = None
    updated_at: float = 0.0
    # `cswap resume test`: transcript_path is a stand-in holding a fake
    # stop, and delivery is confirmed in the session's real transcript.
    rehearsal: bool = False
    confirm_path: str | None = None

    @property
    def identity(self) -> tuple[str, str] | None:
        if not self.identity_email:
            return None
        return (self.identity_email, self.identity_org or "")

    def to_json(self) -> dict:
        return {_camel(f.name): getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_json(cls, data: dict) -> ResumeRecord | None:
        kwargs = {}
        for f in fields(cls):
            key = _camel(f.name)
            if key in data:
                kwargs[f.name] = data[key]
        try:
            return cls(**kwargs)
        except TypeError:
            return None


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)


def valid_session_id(session_id: object) -> bool:
    return isinstance(session_id, str) and bool(_SESSION_ID.match(session_id))


def resume_dir(backup_root: Path) -> Path:
    return backup_root / RESUME_DIRNAME


def _record_path(backup_root: Path, session_id: str) -> Path:
    return resume_dir(backup_root) / f"{session_id}.json"


def force_flag(backup_root: Path, session_id: str) -> Path:
    return resume_dir(backup_root) / f"{session_id}.force"


def load_record(backup_root: Path, session_id: str) -> ResumeRecord | None:
    try:
        data = json.loads(_record_path(backup_root, session_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return ResumeRecord.from_json(data) if isinstance(data, dict) else None


def list_records(backup_root: Path) -> list[ResumeRecord]:
    records = []
    try:
        paths = sorted(resume_dir(backup_root).glob("*.json"))
    except OSError:
        return []
    for path in paths:
        record = load_record(backup_root, path.stem)
        if record is not None:
            records.append(record)
    return records


@dataclass(frozen=True)
class LimitStop:
    """A usage limit Claude Code reported for an account: a session on it is
    stopped right now, and the limit's own reset hasn't passed."""

    session_id: str
    identity: tuple[str, str]
    stalled_at: float
    resets_at: float | None
    limit_type: str | None


def limit_stops(backup_root: Path, clock: Callable[[], float] = time.time) -> list[LimitStop]:
    """Limits Claude Code itself reported, newest first, for ``cswap auto``.

    A stop is first-hand proof an account is spent, even while the usage
    endpoint can't be reached. Only sessions still stopped count (one that
    carried on, or was continued, says nothing about the account now), only
    on the default profile (the only one account switches apply to), and
    only until the limit's own reset. Rehearsal stops are fakes. Empty when
    ``resume.switchOnStop`` is off.

    "Still stopped" is read from the transcript, not the record's state: a
    sign-out or reboot kills waiters, which leaves their records ``waiting``
    after the session has long carried on (found 2026-11-03).

    ``clock`` is read only when there is a stop to judge, so an engine tick
    with none reads its clock exactly as it would without this check.
    """
    if not load_resume_settings(backup_root).switch_on_stop:
        return []
    stops = []
    now: float | None = None
    for record in list_records(backup_root):
        identity = record.identity
        if (
            record.state != WAITING
            or record.rehearsal
            or not record.default_profile
            or identity is None
            or record.stalled_at is None
        ):
            continue
        if now is None:
            now = clock()
        if record.resets_at is not None:
            if record.resets_at <= now:
                continue
        elif now - record.stalled_at > UNTIMED_STOP_TRUST_S:
            continue
        if moved_on_since_stop(record):
            continue
        stops.append(
            LimitStop(
                session_id=record.session_id,
                identity=identity,
                stalled_at=record.stalled_at,
                resets_at=record.resets_at,
                limit_type=record.limit_type,
            )
        )
    stops.sort(key=lambda stop: stop.stalled_at, reverse=True)
    return stops


def moved_on_since_stop(record: ResumeRecord) -> bool:
    """Whether the session did real work after its stop (the waiter's own
    "carried on" test)."""
    state = tx.read_transcript_state(record.transcript_path)
    return state.progressed_since(record.stalled_at or record.armed_at)


def reconcile_orphans(backup_root: Path, now: float) -> None:
    """Finish ``waiting`` records whose waiter died without finishing them.

    Waiters live in the user's session, so a sign-out or reboot (Windows'
    Fast Startup "shut down" signs out too) kills them mid-wait, and their
    records stayed ``waiting`` forever: never pruned, and shown as waiting
    long after the session carried on. A record is finished the way its
    waiter would have: ``moved-on`` when the transcript shows work after the
    stop, ``session-gone`` when no process runs the session.

    The waiter lock is probed only after one of those checks passed, so a
    waiter that is just starting (its session is live and stopped) is never
    raced for its lock. A live waiter keeps its record: it is mid-delivery,
    or about to reach the same conclusion itself.
    """
    for record in list_records(backup_root):
        if record.state != WAITING or record.rehearsal:
            continue
        if moved_on_since_stop(record):
            outcome = (MOVED_ON, "the session carried on without auto-continue")
        elif find_session(record.config_home, record.session_id) is None:
            outcome = (SESSION_GONE, "the session is no longer running")
        else:
            continue
        if waiter_alive(backup_root, record.session_id):
            continue
        state, detail = outcome

        def finish(
            prev: ResumeRecord | None, state: str = state, detail: str = detail
        ) -> ResumeRecord | None:
            if prev is None or prev.state != WAITING:
                return None
            return replace(prev, state=state, detail=detail, updated_at=now)

        if mutate_record(backup_root, record.session_id, finish) is not None:
            _logger.info(
                "auto-continue: session %s %s: %s (its waiter had stopped)",
                record.session_id[:8],
                state,
                detail,
            )


def _records_lock(backup_root: Path) -> FileLock:
    return FileLock(resume_dir(backup_root) / ".records.lock")


def mutate_record(
    backup_root: Path,
    session_id: str,
    mutator: Callable[[ResumeRecord | None], ResumeRecord | None],
) -> ResumeRecord | None:
    """Read-modify-write one record under the records lock (arm and the
    waiter both update records, possibly at the same moment)."""
    with _records_lock(backup_root):
        updated = mutator(load_record(backup_root, session_id))
        if updated is not None:
            atomic_write_json(_record_path(backup_root, session_id), updated.to_json())
        return updated


def _waiter_lock(backup_root: Path, session_id: str) -> FileLock:
    return FileLock(resume_dir(backup_root) / f"{session_id}.waiter.lock")


def waiter_alive(backup_root: Path, session_id: str) -> bool:
    """Whether a waiter process currently holds this session's waiter lock."""
    lock = _waiter_lock(backup_root, session_id)
    if lock.acquire(timeout=0):
        lock.release()
        return False
    return True


def prune_records(backup_root: Path, now: float) -> None:
    """Drop finished records (and their lock/flag files) older than the
    retention window; waiting records are never pruned."""
    for record in list_records(backup_root):
        if record.state == WAITING or now - (record.updated_at or 0) < RECORD_RETENTION_S:
            continue
        base = resume_dir(backup_root)
        for name in (
            f"{record.session_id}.json",
            f"{record.session_id}.force",
            f"{record.session_id}.waiter.lock",
            f"{record.session_id}.rehearsal.jsonl",
        ):
            try:
                (base / name).unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Local state the waiter reads
# ---------------------------------------------------------------------------


def read_identity(global_config: Path | str) -> tuple[str, str] | None:
    """The live login's (email, organizationUuid) from Claude Code's config."""
    try:
        data = json.loads(Path(global_config).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    account = data.get("oauthAccount") if isinstance(data, dict) else None
    if not isinstance(account, dict):
        return None
    email = account.get("emailAddress")
    if not isinstance(email, str) or not email:
        return None
    return (email, account.get("organizationUuid") or "")


def read_last_switch_at(backup_root: Path) -> float | None:
    """When ``cswap auto`` last switched accounts (its persisted state)."""
    try:
        state = json.loads((backup_root / "autoswitch_state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = state.get("lastSwitchAt") if isinstance(state, dict) else None
    return float(value) if isinstance(value, (int, float)) else None


def cross_session_inbound(settings_path: Path) -> str | None:
    """The ``crossSessionInbound`` value in a Claude Code settings file."""
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get("crossSessionInbound") if isinstance(data, dict) else None
    return value if isinstance(value, str) else None


def peer_message_held(permission_mode: str | None, config_home: Path | str) -> bool:
    """Whether a session would hold a message from an outside (peer) sender:
    sessions that bypass permission prompts do, unless the user opted into
    ``crossSessionInbound: accept``."""
    if permission_mode != "bypassPermissions":
        return False
    return cross_session_inbound(Path(config_home) / "settings.json") != "accept"


def delivery_confirmed(
    transcript_path: str,
    since: float,
    *,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    timeout: float = CONFIRM_TIMEOUT_S,
    still_busy: Callable[[], bool] | None = None,
    busy_max: float = CONFIRM_BUSY_MAX_S,
) -> bool:
    """Poll the transcript for our message. A held or refused message leaves
    no trace at all, so "posted" alone proves nothing.

    Past ``timeout``, keep waiting (up to ``busy_max``) while ``still_busy()``
    says the session is in a turn: a queued message is recorded only when
    that turn consumes it. Once it's idle again with no trace, it wasn't
    delivered."""
    start = clock()
    while True:
        if tx.recorded_since(transcript_path, MARKER, since - CONFIRM_SKEW_S):
            return True
        elapsed = clock() - start
        if elapsed >= timeout:
            if not (still_busy is not None and elapsed < busy_max and still_busy()):
                return False
        sleep(CONFIRM_POLL_S)


def is_default_profile(config_home: Path, backup_root: Path) -> bool:
    """False for a ``cswap run`` session profile (a config home under the
    backup root's ``sessions/``): those are pinned to one account, so account
    switches never help them — only the limit's own reset does."""
    try:
        return not config_home.resolve().is_relative_to((backup_root / "sessions").resolve())
    except OSError:
        return True


def find_session(config_home: Path | str, session_id: str) -> ClaudeSession | None:
    """The live Claude Code process currently running this session, if any."""
    for session in list_sessions(Path(config_home)):
        if session.session_id == session_id:
            return session
    return None


def _reset_ts(window: dict) -> float | None:
    raw = window.get("resets_at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _limited_windows(limit_type: str | None) -> tuple[str, ...]:
    if limit_type in ("five_hour", "seven_day"):
        return (limit_type,)
    return ("five_hour", "seven_day")


def window_pct(usage: object, limit_type: str | None, now: float) -> float | None:
    """Current utilization of the window(s) a stop was about, from cached
    usage; a reading whose window has since reset counts as 0. ``None`` when
    unknown."""
    if not isinstance(usage, dict):
        return None
    pcts = []
    for key in _limited_windows(limit_type):
        window = usage.get(key)
        if not isinstance(window, dict):
            continue
        pct = window.get("pct")
        if not isinstance(pct, (int, float)) or isinstance(pct, bool):
            continue
        reset = _reset_ts(window)
        pcts.append(0.0 if reset is not None and reset <= now else float(pct))
    return max(pcts) if pcts else None


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Readiness:
    ready: bool
    reason: str
    wait_s: float = POLL_S


@dataclass(frozen=True)
class LiveContext:
    """The local observations one readiness decision runs on."""

    identity: tuple[str, str] | None = None
    last_switch_at: float | None = None
    usage: dict | None = None  # cached usage of the live account
    usage_fetched_at: float | None = None


def evaluate(
    record: ResumeRecord, now: float, live: LiveContext, settings: ResumeSettings
) -> Readiness:
    """Is there capacity to continue this session again?"""
    stalled_at = record.stalled_at or record.armed_at
    if settings.max_wait_hours > 0 and now - stalled_at > settings.max_wait_hours * 3600:
        # Too long ago to assume the plan still stands: hold for the user.
        return Readiness(False, "held", HELD_POLL_S)
    if not settings.enabled:
        return Readiness(False, "paused", HELD_POLL_S)
    reset_due = record.resets_at + settings.slack_seconds if record.resets_at else None
    if reset_due is not None and now >= reset_due:
        return Readiness(True, "limit-reset", 0.0)
    pct = window_pct(live.usage, record.limit_type, now)
    if settings.on_switch and record.default_profile:
        identity_changed = (
            live.identity is not None
            and record.identity is not None
            and live.identity != record.identity
        )
        switched_since = live.last_switch_at is not None and live.last_switch_at > stalled_at
        if identity_changed or switched_since:
            if pct is not None and pct >= 100.0:
                return Readiness(False, "switched-account-exhausted")
            return Readiness(True, "account-switched", 0.0)
    if (
        record.default_profile
        and pct is not None
        and pct <= HEADROOM_MAX_PCT
        and live.usage_fetched_at is not None
        and live.usage_fetched_at >= stalled_at + HEADROOM_EVIDENCE_AFTER_S
    ):
        return Readiness(True, "headroom", 0.0)
    if reset_due is None:
        return Readiness(False, "waiting-for-capacity")
    return Readiness(False, "waiting-for-reset", max(1.0, min(POLL_S, reset_due - now)))


def note_stall(
    record: ResumeRecord,
    stall: tx.Stall,
    state: tx.TranscriptState,
    settings: ResumeSettings,
    identity: tuple[str, str] | None,
    now: float,
) -> ResumeRecord:
    """Fold a newly seen stop into the record, applying the loop guard."""
    consecutive = record.consecutive
    if record.last_resume_at is not None and stall.uuid != record.resumed_stall_uuid:
        # A new stop after one of our resumes: did that resume get work done?
        # A prompt typed by hand since then also starts the count over.
        since = record.last_resume_at
        moved = state.progressed_since(since) or state.prompted_since(since)
        consecutive = 0 if moved else consecutive + 1
    updated = replace(
        record,
        stall_uuid=stall.uuid,
        stalled_at=stall.at,
        resets_at=stall.resets_at,
        limit_type=stall.limit_type,
        identity_email=identity[0] if identity else record.identity_email,
        identity_org=identity[1] if identity else record.identity_org,
        consecutive=consecutive,
        updated_at=now,
    )
    if consecutive >= settings.max_consecutive:
        return replace(
            updated,
            state=GAVE_UP,
            detail=f"hit the limit again after {consecutive} automatic continues in a row",
        )
    return replace(updated, state=WAITING)


# ---------------------------------------------------------------------------
# Arm (the StopFailure hook)
# ---------------------------------------------------------------------------


def arm(
    backup_root: Path,
    hook_input: Mapping,
    env: Mapping[str, str],
    *,
    clock: Callable[[], float] = time.time,
    spawn: Callable[[str], None] | None = None,
    on_gave_up: Callable[[str, str], None] | None = None,
) -> str:
    """Handle one StopFailure event. Returns an outcome word (for logs and
    tests); never raises for bad input, so a hook can't break the session."""
    raw_id = hook_input.get("session_id")
    transcript_path = hook_input.get("transcript_path")
    if not valid_session_id(raw_id) or not isinstance(transcript_path, str):
        return "bad-input"
    session_id = str(raw_id)
    now = clock()
    settings = load_resume_settings(backup_root)
    reconcile_orphans(backup_root, now)
    prune_records(backup_root, now)
    state = tx.read_transcript_state(transcript_path)
    stall = state.stall if state.status == tx.STALLED else None
    identity = read_identity(get_global_config_path())
    outcome = "armed"

    config_home = get_claude_config_home()

    def mutator(prev: ResumeRecord | None) -> ResumeRecord | None:
        nonlocal outcome
        if prev is not None and stall is not None and stall.uuid == prev.resumed_stall_uuid:
            outcome = "already-resumed"  # a repeat event for a stop we already handled
            return None
        if prev is not None and prev.state == GAVE_UP and stall is not None:
            if stall.uuid == prev.stall_uuid:
                outcome = "gave-up"
                return None
        if (
            prev is not None
            and prev.state == MOVED_ON
            and stall is None
            and now - prev.updated_at < RESPAWN_DAMPING_S
        ):
            outcome = "recently-moved-on"
            return None
        base = prev if prev is not None and prev.state == WAITING else None
        record = ResumeRecord(
            session_id=session_id,
            transcript_path=transcript_path,
            config_home=str(config_home),
            global_config_path=str(get_global_config_path()),
            default_profile=is_default_profile(config_home, backup_root),
            armed_at=base.armed_at if base else now,
            socket_path=env.get("CLAUDE_CODE_MESSAGING_SOCKET") or None,
            cwd=hook_input.get("cwd") if isinstance(hook_input.get("cwd"), str) else None,
            identity_email=base.identity_email if base else (identity[0] if identity else None),
            identity_org=base.identity_org if base else (identity[1] if identity else None),
            stall_uuid=base.stall_uuid if base else None,
            stalled_at=base.stalled_at if base else None,
            resets_at=base.resets_at if base else None,
            limit_type=base.limit_type if base else None,
            consecutive=prev.consecutive if prev else 0,
            last_resume_at=prev.last_resume_at if prev else None,
            resumed_stall_uuid=prev.resumed_stall_uuid if prev else None,
            updated_at=now,
        )
        if stall is not None and stall.uuid != record.stall_uuid:
            record = note_stall(record, stall, state, settings, identity, now)
            if record.state == GAVE_UP:
                outcome = "gave-up-now"
        return record

    record = mutate_record(backup_root, session_id, mutator)
    if record is None:
        return outcome
    if outcome == "gave-up-now":
        _logger.warning("auto-continue: session %s %s", session_id[:8], record.detail)
        if on_gave_up and settings.notify:
            on_gave_up(_session_label(record), record.detail)
        return "gave-up"
    if waiter_alive(backup_root, session_id):
        return "already-waiting"
    (spawn or spawn_waiter)(session_id)
    _logger.info("auto-continue: armed session %s (%s)", session_id[:8], record.limit_type or "?")
    return outcome


def windows_spawn_flags(breakaway: bool = True) -> int:
    """Creation flags for a background process on Windows that never shows a
    window, its children included.

    Not DETACHED_PROCESS: a venv's ``Scripts\\python.exe`` (pipx installs) is a
    launcher that starts the real interpreter as a child, and a console
    program started by a process with *no* console gets a brand-new, visible
    console - an empty terminal window per waiter (seen 2026-11-03: three
    stopped sessions, three windows). CREATE_NO_WINDOW gives the process a
    console without a window instead, which the launcher's child and any
    tool it runs (toast notifications) inherit.
    """
    if sys.platform != "win32":
        return 0
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    if breakaway:
        flags |= subprocess.CREATE_BREAKAWAY_FROM_JOB
    return flags


def spawn_waiter(session_id: str) -> None:
    """Start ``cswap resume wait`` in the background, outliving the hook.

    Its own process group (and, when the job allows it, out of the hook's job
    object), so the hook returns at once and nothing that tears the hook's
    process tree down takes the waiter along; no inherited stdio so it never
    holds Claude Code's hook pipes open; never a visible window
    (:func:`windows_spawn_flags`). It inherits the environment, which carries
    the session's own-child inbox token.
    """
    cmd = [sys.executable, "-m", "claude_swap", "resume", "wait", session_id]
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform != "win32":
        subprocess.Popen(cmd, start_new_session=True, **kwargs)
        return
    try:
        subprocess.Popen(cmd, creationflags=windows_spawn_flags(), **kwargs)
    except OSError:  # the hook's job object doesn't allow breakaway
        subprocess.Popen(cmd, creationflags=windows_spawn_flags(breakaway=False), **kwargs)


class RehearsalError(ClaudeSwitchError):
    """``cswap resume test`` can't run here."""


def rehearse(
    backup_root: Path,
    env: Mapping[str, str],
    *,
    clock: Callable[[], float] = time.time,
    spawn: Callable[[str], None] | None = None,
) -> tuple[ClaudeSession, ResumeRecord]:
    """Arm a rehearsal of auto-continue on the Claude Code session this runs
    inside (``cswap resume test``, run by Claude through its own tool).

    The session's own inbox and own-child token come from the environment,
    exactly as for a real hook, so the rehearsal exercises the real path:
    waiter spawn, wait-for-idle, delivery, confirmation. Only the stop is
    fake: a stand-in transcript with one usage-limit record, so nothing is
    written to Claude Code's own files.
    """
    own_socket = env.get("CLAUDE_CODE_MESSAGING_SOCKET")
    if not own_socket or not env.get("CLAUDE_CODE_MESSAGING_TOKEN"):
        raise RehearsalError(
            "run this from inside a Claude Code session (ask Claude to run "
            "`cswap resume test`): the test needs that session's own inbox token"
        )
    config_home = get_claude_config_home()
    session = next(
        (
            s
            for s in list_sessions(config_home)
            if s.messaging_socket_path
            and inbox_address(s.messaging_socket_path) == inbox_address(own_socket)
        ),
        None,
    )
    if session is None:
        raise RehearsalError("couldn't find the Claude Code session that owns this inbox")
    real = next((config_home / "projects").glob(f"*/{session.session_id}.jsonl"), None)
    if real is None:
        raise RehearsalError(f"couldn't find the transcript of session {session.session_id}")
    if waiter_alive(backup_root, session.session_id):
        raise RehearsalError("a waiter is already active for this session")

    now = clock()
    stall_uuid = f"rehearsal-{int(now)}"
    stand_in = resume_dir(backup_root) / f"{session.session_id}.rehearsal.jsonl"
    stand_in.parent.mkdir(parents=True, exist_ok=True)
    stop = {
        "type": "assistant",
        "uuid": stall_uuid,
        "timestamp": datetime.fromtimestamp(now, UTC).isoformat().replace("+00:00", "Z"),
        "isApiErrorMessage": True,
        "apiError": "usage_limit_reached",
        "apiErrorParams": {"rate_limit_info": {"status": "rejected", "resetsAt": int(now) - 1}},
        "message": {"content": [{"type": "text", "text": "rehearsal stop (cswap resume test)"}]},
    }
    stand_in.write_text(json.dumps(stop) + "\n", encoding="utf-8")
    record = ResumeRecord(
        session_id=session.session_id,
        transcript_path=str(stand_in),
        config_home=str(config_home),
        global_config_path=str(get_global_config_path()),
        default_profile=True,
        armed_at=now,
        socket_path=own_socket,
        cwd=session.cwd or None,
        stall_uuid=stall_uuid,
        stalled_at=now,
        resets_at=now - 1,
        limit_type="five_hour",
        detail="test armed; end the turn to see it",
        updated_at=now,
        rehearsal=True,
        confirm_path=str(real),
    )
    mutate_record(backup_root, session.session_id, lambda _prev: record)
    (spawn or spawn_waiter)(session.session_id)
    _logger.info("auto-continue: rehearsal armed for session %s", session.session_id[:8])
    return session, record


# ---------------------------------------------------------------------------
# Wait + deliver
# ---------------------------------------------------------------------------


def _session_label(record: ResumeRecord, session: ClaudeSession | None = None) -> str:
    if session is not None and session.name:
        return session.name
    if record.cwd:
        return Path(record.cwd).name or record.cwd
    return record.session_id[:8]


class Waiter:
    """One session's waiter loop. Every dependency is injectable for tests."""

    def __init__(
        self,
        backup_root: Path,
        session_id: str,
        *,
        env: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        live_context: Callable[[ResumeRecord], LiveContext] | None = None,
        on_resumed: Callable[[str, str], None] | None = None,
        on_gave_up: Callable[[str, str], None] | None = None,
    ):
        # on_gave_up(session, detail) is the "needs you" channel: the loop
        # guard tripping, a held (stale) session, or an undelivered message.
        self.backup_root = backup_root
        self.session_id = session_id
        self.env = os.environ if env is None else env
        self.clock = clock
        self.sleep = sleep
        self.live_context = live_context or self._default_live_context
        self.on_resumed = on_resumed
        self.on_gave_up = on_gave_up
        self.settings = load_resume_settings(backup_root)
        self._switcher: ClaudeAccountSwitcher | None = None

    # -- observations ------------------------------------------------------

    def _default_live_context(self, record: ResumeRecord) -> LiveContext:
        identity = read_identity(record.global_config_path)
        if not record.default_profile:
            return LiveContext(identity=identity)
        usage = fetched_at = None
        if identity is not None:
            try:
                if self._switcher is None:
                    from claude_swap.switcher import ClaudeAccountSwitcher

                    self._switcher = ClaudeAccountSwitcher()
                num = self._switcher.account_number_for_identity(*identity)
                entry = self._switcher.cached_usage_entry(num) if num else None
                if entry is not None:
                    usage, fetched_at = entry.last_good, entry.fetched_at
            except Exception:  # cached usage is a nicety; never let it stop the wait
                _logger.debug("auto-continue: cached usage unavailable", exc_info=True)
        return LiveContext(
            identity=identity,
            last_switch_at=read_last_switch_at(self.backup_root),
            usage=usage,
            usage_fetched_at=fetched_at,
        )

    # -- loop ----------------------------------------------------------------

    def run(self) -> str:
        lock = _waiter_lock(self.backup_root, self.session_id)
        if not lock.acquire(timeout=0):
            return "already-waiting"
        try:
            return self._loop()
        finally:
            lock.release()

    def _finish(self, state: str, detail: str) -> str:
        def mutator(prev: ResumeRecord | None) -> ResumeRecord | None:
            if prev is None or prev.state != WAITING:
                return None
            return replace(prev, state=state, detail=detail, updated_at=self.clock())

        mutate_record(self.backup_root, self.session_id, mutator)
        _logger.info("auto-continue: session %s %s: %s", self.session_id[:8], state, detail)
        return state

    def _loop(self) -> str:
        last_detail = None
        while True:
            record = load_record(self.backup_root, self.session_id)
            if record is None or record.state != WAITING:
                return record.state if record else "no-record"
            self.settings = load_resume_settings(self.backup_root)
            now = self.clock()
            if now - record.armed_at > MAX_WAIT_S:
                return self._finish(TIMED_OUT, "waited too long; continue it by hand")
            session = find_session(record.config_home, self.session_id)
            if session is None:
                return self._finish(SESSION_GONE, "the session is no longer running")
            state = tx.read_transcript_state(record.transcript_path)
            if state.progressed_since(record.stalled_at or record.armed_at):
                return self._finish(MOVED_ON, "the session carried on without auto-continue")
            if record.stall_uuid is None and now - record.armed_at > ARM_GRACE_S:
                if state.status != tx.STALLED:
                    return self._finish(MOVED_ON, "no usage-limit stop showed up")
            stall = state.stall if state.status == tx.STALLED else None
            if stall is not None and stall.uuid == record.resumed_stall_uuid:
                stall = None  # our message for this stop just hasn't landed yet
            if stall is not None and stall.uuid != record.stall_uuid:
                record = self._note(record, stall, state, now)
                if record.state != WAITING:
                    if record.state == GAVE_UP and self.on_gave_up and self.settings.notify:
                        self.on_gave_up(_session_label(record, session), record.detail)
                    return record.state

            forced = force_flag(self.backup_root, self.session_id).exists()
            if stall is None:
                readiness = Readiness(False, "waiting-for-the-stop", SLICE_S * 5)
            elif forced:
                readiness = Readiness(True, "forced", 0.0)
            elif record.rehearsal:
                readiness = Readiness(True, "rehearsal", 0.0)
            else:
                readiness = evaluate(record, now, self.live_context(record), self.settings)

            deliverable = session.status in DELIVERABLE_STATUSES
            if readiness.ready and stall is not None and deliverable:
                outcome = self._deliver(record, session, state, readiness.reason, forced)
                if outcome is not None:
                    return outcome
                readiness = Readiness(False, "delivery-failed", POLL_S)
            elif readiness.ready:
                readiness = Readiness(False, f"waiting-for-idle ({session.status})", SLICE_S * 2)

            if readiness.reason == "held" and last_detail != "held":
                self._notify_held(record, session)
            if readiness.reason != last_detail:
                last_detail = readiness.reason
                self._set_detail(readiness.reason)
            self._sleep_watching_flag(readiness.wait_s)

    def _note(
        self, record: ResumeRecord, stall: tx.Stall, state: tx.TranscriptState, now: float
    ) -> ResumeRecord:
        def mutator(prev: ResumeRecord | None) -> ResumeRecord | None:
            if prev is None:
                return None
            # Identity at arm time is the best record of the stop's account;
            # only a stop with none falls back to the live login now.
            identity = None if prev.identity else read_identity(prev.global_config_path)
            return note_stall(prev, stall, state, self.settings, identity, now)

        return mutate_record(self.backup_root, self.session_id, mutator) or record

    def _set_detail(self, detail: str) -> None:
        def mutator(prev: ResumeRecord | None) -> ResumeRecord | None:
            if prev is None or prev.state != WAITING or prev.detail == detail:
                return None
            return replace(prev, detail=detail, updated_at=self.clock())

        mutate_record(self.backup_root, self.session_id, mutator)

    def _notify_held(self, record: ResumeRecord, session: ClaudeSession) -> None:
        if self.on_gave_up and self.settings.notify:
            hours = (self.clock() - (record.stalled_at or record.armed_at)) / 3600
            self.on_gave_up(
                _session_label(record, session),
                f"held: stopped {hours:.0f}h ago, longer than resume.maxWaitHours; "
                "run `cswap resume now` to continue it",
            )

    def _sleep_watching_flag(self, seconds: float) -> None:
        flag = force_flag(self.backup_root, self.session_id)
        remaining = max(0.0, seconds)
        while remaining > 0:
            step = min(SLICE_S, remaining)
            self.sleep(step)
            remaining -= step
            if flag.exists():
                return

    def _deliver(
        self,
        record: ResumeRecord,
        session: ClaudeSession,
        state: tx.TranscriptState,
        reason: str,
        forced: bool,
    ) -> str | None:
        stall = state.stall
        if stall is None:  # only called on a stalled transcript
            return None
        if not forced and not record.rehearsal:
            self.sleep(random.uniform(0.0, JITTER_S))
            # Re-validate after the jitter: the user may have typed meanwhile.
            current = load_record(self.backup_root, self.session_id)
            if current is None or current.state != WAITING:
                return current.state if current else "no-record"
            fresh = find_session(record.config_home, self.session_id)
            state = tx.read_transcript_state(record.transcript_path)
            if fresh is None or state.stall is None or state.stall.uuid != stall.uuid:
                return None
            session = fresh
        address = session.messaging_socket_path or record.socket_path
        if not address:
            return self._finish(GAVE_UP, "the session has no cross-session inbox")
        token, own = self._token_for(address, record, session)
        if not own and peer_message_held(state.permission_mode, record.config_home):
            return self._undelivered(
                record,
                session,
                "it bypasses permissions and cswap doesn't have its own token, so Claude "
                "Code would hold the message; type 'continue' there",
            )
        if record.rehearsal:
            why, text = REASON_TEXT["rehearsal"], REHEARSAL_MESSAGE
        else:
            why = REASON_TEXT.get("forced" if forced else reason, reason)
            text = MESSAGE.format(why=why)
        posted_at = self.clock()
        try:
            post_message(address, text, token)
        except InboxError as e:
            _logger.warning("auto-continue: delivery to %s failed: %s", self.session_id[:8], e)
            return None
        confirm_path = record.confirm_path or record.transcript_path

        def still_busy() -> bool:
            current = find_session(record.config_home, self.session_id)
            return current is not None and current.status in BUSY_STATUSES

        if not delivery_confirmed(
            confirm_path, posted_at, clock=self.clock, sleep=self.sleep, still_busy=still_busy
        ):
            return self._undelivered(
                record,
                session,
                "the session never recorded the continue message (held or refused by its "
                "inbound settings); type 'continue' there",
            )
        now = self.clock()

        def mutator(prev: ResumeRecord | None) -> ResumeRecord | None:
            if prev is None:
                return None
            return replace(
                prev,
                state=RESUMED,
                detail=why,
                last_resume_at=now,
                resumed_stall_uuid=stall.uuid,
                updated_at=now,
            )

        mutate_record(self.backup_root, self.session_id, mutator)
        try:
            force_flag(self.backup_root, self.session_id).unlink()
        except OSError:
            pass
        _logger.info("auto-continue: resumed session %s (%s)", self.session_id[:8], why)
        if self.on_resumed and self.settings.notify:
            self.on_resumed(_session_label(record, session), why)
        return RESUMED

    def _token_for(
        self, address: str, record: ResumeRecord, session: ClaudeSession
    ) -> tuple[str | None, bool]:
        """``(token, is_own)``: the own-child token when it belongs to this
        inbox, else the peer token (which a session that bypasses permissions
        will hold)."""
        own_socket = self.env.get("CLAUDE_CODE_MESSAGING_SOCKET")
        own_token = self.env.get("CLAUDE_CODE_MESSAGING_TOKEN")
        if own_token and own_socket and inbox_address(own_socket) == inbox_address(address):
            return own_token, True
        sessions_dir = Path(record.config_home) / "sessions"
        return peer_token(sessions_dir, session.pid, session.proc_start), False

    def _undelivered(self, record: ResumeRecord, session: ClaudeSession, detail: str) -> str:
        state = self._finish(UNDELIVERED, detail)
        if self.on_gave_up and self.settings.notify:
            self.on_gave_up(_session_label(record, session), detail)
        return state
