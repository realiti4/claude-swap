"""Managed sessions: cswap-owned Claude profiles ``<backup>/sessions/auto-<id>/``.

A managed session is a Claude Code instance started by ``cswap run --auto``
whose profile cswap owns end to end: the account is chosen by the balance
policy, the profile holds an access-token-only credential (the refresh token
stays in the slot's backup store — the single lineage holder, see
``session_credentials``), and cswap pushes fresh access tokens into it.

Per-account ``cswap run N`` profiles live beside these as ``<num>-<slug>``;
nothing in cswap decomposes those names, and managed ids can never collide
with them (``auto-`` + 8 hex digits vs a leading slot number).

The registry ``sessions/managed.json`` records, per session id, the account
identity (email + organizationUuid — never the slot number, which
``cswap move``/``swap`` can renumber), the PID and its start stamp, and the
assignment history. An entry is written BEFORE ``exec``: POSIX exec keeps the
PID, so the entry is both the reservation a concurrent launch must see and,
once Claude writes ``sessions/<pid>.json``, the registration. Liveness is
"pid alive and it is the process we stamped" (``pid_matches_record``), so a
recycled PID never keeps a dead session's reservation alive — except for a
row written without a stamp at all (``ps`` unavailable or timed out, see
``process_stamp``), which degrades to bare "is the PID alive" and is
therefore unreclaimable until that PID dies. ``allocate`` logs when it
writes one; the failure direction is a leaked row, never a deletion.

The registry has its own lock rather than the backup ``.lock``: callers run
``consume_backup_grant`` (which takes the backup lock internally, and
``FileLock`` is not reentrant) around registry operations, and the chooser
passed to :meth:`ManagedSessionRegistry.allocate` runs under this lock.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import shutil
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from claude_swap.exceptions import SessionError
from claude_swap.locking import FileLock
from claude_swap.process_detection import (
    is_pid_alive,
    pid_matches_record,
    process_start_ticks,
    process_started_at,
)
from claude_swap.session import (
    _mkdir_private,
    delete_macos_keychain_entry,
    profile_is_quiescent,
)
from claude_swap.settings import atomic_write_json

_logger = logging.getLogger("claude-swap")

MANAGED_PREFIX = "auto-"
_SESSION_ID_RE = re.compile(r"^auto-[0-9a-f]{8}$")
REGISTRY_FILENAME = "managed.json"
REGISTRY_LOCK_FILENAME = ".managed.lock"
REGISTRY_SCHEMA_VERSION = 1

# What the launch path waits for the registry lock, against the 10s the
# pollers use: `allocate` holds the lock across one liveness check per
# entry (a `ps` call each on macOS) plus a session-record read, so a few
# slow probes in one launch can outlast a shorter wait in a concurrent
# one -- and a launch that fails on a lock error is a worse outcome than
# a launch that takes a moment longer.
LAUNCH_LOCK_TIMEOUT_S = 45.0

# Where a session's current access token came from (informational; the
# refresher re-derives it every push — see managed_refresh).
SOURCE_BACKUP = "backup"          # slot backup, refreshed centrally
SOURCE_LANE0 = "lane0"            # read-only copy of the default login's token
SOURCE_RUN_PROFILE = "run-profile"  # read-only copy from a live `cswap run N`

REASON_LAUNCH = "launch"


def sessions_root(backup_dir: Path) -> Path:
    return backup_dir / "sessions"


def new_session_id() -> str:
    return f"{MANAGED_PREFIX}{secrets.token_hex(4)}"


def is_managed_session_id(name: str) -> bool:
    return bool(_SESSION_ID_RE.match(name))


def process_stamp(pid: int) -> str | None:
    """A ``procStart``-style identity stamp for ``pid``, in the formats
    :func:`pid_matches_record` checks: Linux start ticks, else the
    ``ps -o lstart`` wall-clock form (UTC)."""
    ticks = process_start_ticks(pid)
    if ticks is not None:
        return ticks
    started = process_started_at(pid)
    if started is None:
        return None
    return time.asctime(time.gmtime(started))


def utc_now_iso(clock: Callable[[], float] = time.time) -> str:
    return (
        datetime.fromtimestamp(clock(), tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


# An auto-* dir with no registry entry is either a crash leftover or a
# launch between `allocate` and its own registry write becoming visible to
# a sweeper that read the registry a moment earlier. Only the former is
# old; the grace keeps the sweeper off the latter.
ORPHAN_GRACE_S = 300

HOOKS_FILENAME = "cswap-hooks.json"


def create_managed_profile(session_dir: Path, *, theme: str = "dark") -> None:
    """Create a fresh, onboarded, 0700 managed profile.

    Raises ``FileExistsError`` if ``session_dir`` already exists. Managed
    ids are freshly minted per launch (see ``new_session_id``), so a
    collision means something else already put data there; silently
    reusing it — keeping whatever mode or contents it already had —
    would break the fresh-0700-profile promise this makes instead of
    surfacing the problem.

    The keychain item is deleted once ``session_dir`` is ours, before the
    plaintext file is written, for the same reason ``_bootstrap`` deletes it
    early: Claude reads the keychain before the plaintext file.
    """
    _mkdir_private(session_dir.parent)
    session_dir.mkdir(mode=0o700)
    delete_macos_keychain_entry(session_dir)
    atomic_write_json(
        session_dir / ".claude.json",
        {"hasCompletedOnboarding": True, "theme": theme},
    )


def remove_managed_profile(session_dir: Path) -> bool:
    """Delete a managed profile's keychain item and directory.

    Returns whether ``session_dir`` is gone once this returns. Already
    being gone counts as success rather than a failure to log — a
    registry entry whose owning process died before it ever created the
    profile (``allocate`` writes the reservation before the profile
    exists) is a normal case, not an error.

    Refuses, loudly, a path whose name is not a managed session id: every
    caller derives the path from a session id it owns
    (``registry.session_dir``, the orphan pass, a rolled-back launch), so
    anything else is a bug about to ``rmtree`` a directory this module was
    never told it owns — the per-account ``<num>-<slug>`` profiles and the
    sessions root itself sit right beside these.

    ``shutil.rmtree`` unlinks symlinks without following them, so the
    shared ``projects/`` / ``history.jsonl`` links never take ``~/.claude``
    data with them. Unlike the old ``ignore_errors=True``, a genuine
    per-path removal failure (a file a concurrent writer still has open, a
    permission problem) is logged once via ``onexc`` rather than swallowed
    silently forever; the caller reads the return value to find out
    whether anything survived, and a directory left behind is picked up
    again by the next sweep.
    """
    if not is_managed_session_id(session_dir.name):
        _logger.warning(
            f"Refusing to remove {session_dir}: not a managed session profile."
        )
        return False
    delete_macos_keychain_entry(session_dir)
    if not session_dir.exists():
        return True

    def _log_and_skip(function: object, path: str, exc: BaseException) -> None:
        _logger.warning(
            f"Could not remove {path!r} while removing managed profile "
            f"{session_dir}: {exc}"
        )

    shutil.rmtree(session_dir, onexc=_log_and_skip)
    return not session_dir.exists()


def write_hooks_file(session_dir: Path) -> Path:
    """Write the ``--settings`` document a managed session is launched with.

    Empty (``{"hooks": {}}``) today. Passing it is still a no-op for the
    user's own configuration: ``--settings`` sits above the user, project
    and local settings files, but hook entries merge across those levels
    instead of replacing each other, so an empty map contributes nothing
    and suppresses nothing. The file and the ``--settings`` argument stay
    because the launch that needs a hook should only have to fill this in.
    """
    path = session_dir / HOOKS_FILENAME
    atomic_write_json(path, {"hooks": {}})
    return path


def _valid_pid(pid: object) -> bool:
    """A pid that will round-trip through :meth:`ManagedEntry.from_json`:
    an int, not a bool, and > 1 (nothing manages PID 0 or 1)."""
    return isinstance(pid, int) and not isinstance(pid, bool) and pid > 1


@dataclass(frozen=True)
class AccountRef:
    """Account identity as stored in sequence.json (slot-number free)."""

    email: str
    organization_uuid: str = ""

    def to_json(self) -> dict:
        return {"email": self.email, "organizationUuid": self.organization_uuid}

    @classmethod
    def from_json(cls, raw: object) -> AccountRef | None:
        if not isinstance(raw, dict):
            return None
        email = raw.get("email")
        if not isinstance(email, str) or not email:
            return None
        org = raw.get("organizationUuid") or ""
        return cls(email, org if isinstance(org, str) else "")


@dataclass(frozen=True)
class ManagedEntry:
    session_id: str
    account: AccountRef
    pid: int
    proc_start: str | None
    source: str
    created_at: str
    last_assigned_at: str
    last_reason: str
    access_fingerprint: str | None = None

    def to_json(self) -> dict:
        return {
            "account": self.account.to_json(),
            "pid": self.pid,
            "procStart": self.proc_start,
            "source": self.source,
            "createdAt": self.created_at,
            "lastAssignedAt": self.last_assigned_at,
            "lastReason": self.last_reason,
            "accessFingerprint": self.access_fingerprint,
        }

    @classmethod
    def from_json(cls, session_id: str, raw: object) -> ManagedEntry | None:
        if not is_managed_session_id(session_id) or not isinstance(raw, dict):
            return None
        account = AccountRef.from_json(raw.get("account"))
        pid = raw.get("pid")
        if account is None or not _valid_pid(pid):
            return None

        def text(key: str, default: str = "") -> str:
            value = raw.get(key)
            return value if isinstance(value, str) else default

        proc_start = raw.get("procStart")
        fingerprint = raw.get("accessFingerprint")
        return cls(
            session_id=session_id,
            account=account,
            pid=pid,
            proc_start=proc_start if isinstance(proc_start, str) and proc_start else None,
            source=text("source", SOURCE_BACKUP),
            created_at=text("createdAt"),
            last_assigned_at=text("lastAssignedAt"),
            last_reason=text("lastReason", REASON_LAUNCH),
            access_fingerprint=fingerprint if isinstance(fingerprint, str) and fingerprint else None,
        )


def entry_is_live(entry: ManagedEntry) -> bool:
    """The entry's process is running and is the one we stamped."""
    return is_pid_alive(entry.pid) and pid_matches_record(entry.pid, entry.proc_start)


# Claude's own status for a session: "busy" while it is working, "idle" once
# it is waiting for the next prompt, "waiting" while a prompt of its own is
# unanswered. Only a busy session is spending the account's 5h window.
BUSY_STATUS = "busy"


def session_status(session_dir: Path, pid: int) -> str | None:
    """Claude's status for the instance ``pid`` inside a managed profile, or
    None when it has not written a record yet or the record is unusable.

    Claude keeps one record per instance at ``<config>/sessions/<pid>.json``
    and restamps ``status`` on every change. This reads the one record the
    registry entry names rather than scanning the directory
    (``process_detection.scan_sessions``): the entry's liveness is
    established by the caller's sweep, so no ``ps`` call is needed here to
    know whose record this is — the pid is still read back out of the record
    rather than trusted from the file name.
    """
    try:
        raw = json.loads(
            (session_dir / "sessions" / f"{pid}.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError, RecursionError):
        # Same set ``scan_sessions`` reads these records under, RecursionError
        # included: pathological nesting exhausts the parser's stack on one
        # machine and parses on the next, and neither may take the upkeep
        # pass (and with it every account's token push) down with it.
        return None
    if not isinstance(raw, dict) or raw.get("pid") != pid:
        return None
    status = raw.get("status")
    return status if isinstance(status, str) else None


def entry_is_busy(session_dir: Path, entry: ManagedEntry) -> bool:
    """Whether a live managed session counts toward its account's 5h load.

    True when Claude reports the session busy, and also when there is no
    status to read: an entry is written before ``exec``, so a session whose
    Claude has not created its record yet is a live reservation that counts
    alongside the busy ones, and reading it as idle would let a launch and
    the lane-0 ranking pile onto one account in the same window.
    """
    return session_status(session_dir, entry.pid) in (None, BUSY_STATUS)


def _validate_row(account: AccountRef, pid: object, source: object) -> None:
    """Raise ``ValueError`` for a row :meth:`ManagedEntry.from_json` would
    drop or silently reset on the next read (an invalid pid drops the
    whole row; an empty email, a non-str ``organization_uuid``, or a
    non-str source are otherwise swallowed quietly — ``AccountRef.from_json``
    coerces a non-str ``organizationUuid`` back to ``""``), so a caller
    writing one fails at the write instead of losing the reservation on
    read-back."""
    if not _valid_pid(pid):
        raise ValueError(f"invalid pid: {pid!r}")
    if not account.email:
        raise ValueError("account.email must be non-empty")
    if not isinstance(account.organization_uuid, str):
        raise ValueError(f"invalid organization_uuid: {account.organization_uuid!r}")
    if not isinstance(source, str):
        raise ValueError(f"invalid source: {source!r}")


_UPDATABLE = frozenset({
    "account", "source", "pid", "proc_start",
    "last_assigned_at", "last_reason", "access_fingerprint",
})


@dataclass(frozen=True)
class SweepResult:
    """What one :meth:`ManagedSessionRegistry.sweep` pass found: the dead
    entries dropped from the registry, and the live entries from that same
    registry read.

    ``removed`` reflects the registry row being gone, not whether the
    profile directory was actually deleted — a dead entry whose profile
    isn't ``profile_is_quiescent`` still loses its row (it is dead) but
    keeps its directory (see :meth:`ManagedSessionRegistry.sweep`), and
    still appears here.

    Liveness (``entry_is_live``) shells out to ``ps`` on macOS, so a caller
    that needs both sets — a later push-refresh pass, say — takes ``.live``
    from here instead of re-deriving it with a second ``live_entries()``
    call, which would repeat the check per entry.
    """

    removed: list[ManagedEntry]
    live: list[ManagedEntry]


class ManagedSessionRegistry:
    """``sessions/managed.json`` under ``sessions/.managed.lock``."""

    def __init__(
        self,
        backup_dir: Path,
        *,
        clock: Callable[[], float] = time.time,
        lock_timeout: float = 10.0,
    ):
        self.backup_dir = backup_dir
        self.root = sessions_root(backup_dir)
        self.path = self.root / REGISTRY_FILENAME
        self._lock_path = self.root / REGISTRY_LOCK_FILENAME
        self._clock = clock
        self._lock_timeout = lock_timeout

    def session_dir(self, session_id: str) -> Path:
        return self.root / session_id

    def has_state(self) -> bool:
        """Whether the engine's upkeep loop (and the dead-PID sweep) have
        anything to look after.

        Keyed on actual entries, not on ``path.exists()``: an empty or
        corrupt ``managed.json`` (the last entry just removed, or a husk
        from an old writer) must read as "no state", or that upkeep loop
        would spin forever over nothing. A leftover ``sessions/auto-*``
        directory with no registry row still counts, so a crash between
        ``mkdir`` and the reservation write is not silently ignored.

        Not cheap — it reads the whole registry — and, unlike
        ``entries()``/``get()``, it reads quietly: a caller polling this
        every tick must not have a corrupt file log the same warning on
        every single poll forever.
        """
        if self._read(warn=False):
            return True
        if not self.root.is_dir():
            return False
        return any(
            p.is_dir() and is_managed_session_id(p.name) for p in self.root.iterdir()
        )

    def unreadable_reason(self) -> str | None:
        """Why the registry file cannot be read right now, or ``None`` when
        it is absent (nothing to read) or parses cleanly.

        ``_read`` deliberately folds "absent" and "unreadable" into the
        same empty dict -- right for its polling callers (``autoswitch``'s
        upkeep pass, ``managed_refresh``), which must not be taken down by
        one bad file and treat "no entries" and "can't tell" the same way
        either way. A caller about to do something destructive cannot make
        that trade: reading "can't tell" as "empty" and proceeding would
        delete a live managed session's profile out from under it, since
        an unreadable registry means that session's liveness can't be
        determined either, not that there isn't one. This is the narrow,
        explicit way to ask; the registry's own mutators ask it through
        ``_read_for_write``.

        "Unreadable" is ``_load``'s definition, so it covers a
        ``schemaVersion`` this build cannot model as well as a file it
        cannot parse: either way the rows on disk are not all in hand.
        """
        return self._load(warn=False)[1]

    def _lock(self, timeout: float | None = None) -> FileLock:
        # Private up front: FileLock.acquire() also mkdirs the lock file's
        # parent, but at the default (group/other-readable) mode, and a
        # declined allocate or a remove/update of a missing id never
        # reaches _write to correct it afterwards.
        _mkdir_private(self.root)
        return FileLock(
            self._lock_path,
            timeout=self._lock_timeout if timeout is None else timeout,
        )

    def _load(
        self, *, warn: bool = True
    ) -> tuple[dict[str, ManagedEntry] | None, str | None]:
        """``(entries, None)`` for a registry this build can model in full,
        or ``(None, reason)`` when it cannot. The one place the file is read.

        "Cannot" covers an unparseable file and a ``schemaVersion`` that is
        missing, not an integer, or newer than this build writes. A newer
        writer may have put rows here in a shape
        :meth:`ManagedEntry.from_json` cannot model, and every row is a live
        session's reservation: reading those rows short is harmless, but
        ``_write`` persists only what was read back, so rewriting the file
        without them would drop another session's reservation for good —
        the next launch would place onto the same account and the orphan
        pass would later reclaim its profile. So both classes are reported
        the same way and every mutator refuses on them
        (:meth:`_read_for_write`).

        A row this build understands the shape of but must drop (an invalid
        pid, a blanked email — see ``_validate_row``) is a different case:
        it is logged per session id and skipped, so a vanished reservation
        can be correlated with something, rather than silently costing a
        row. The log is gated on ``warn`` so the cheap existence check
        (``has_state``) stays quiet.

        An absent file is not a failure — it is "no sessions".
        """
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}, None
        except (OSError, ValueError) as e:
            return None, str(e)
        version = raw.get("schemaVersion") if isinstance(raw, dict) else None
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version > REGISTRY_SCHEMA_VERSION
        ):
            return None, (
                f"schemaVersion {version!r} is not one this build can model "
                f"(it writes {REGISTRY_SCHEMA_VERSION})"
            )
        sessions = raw.get("sessions")
        if not isinstance(sessions, dict):
            return {}, None
        entries: dict[str, ManagedEntry] = {}
        for session_id, value in sessions.items():
            entry = ManagedEntry.from_json(session_id, value)
            if entry is None:
                if warn:
                    _logger.warning(
                        f"Managed session registry {self.path}: dropping the "
                        f"unusable entry {session_id!r}; its reservation (if "
                        "any) is lost on the next write."
                    )
                continue
            entries[session_id] = entry
        return entries, None

    def _read(self, *, warn: bool = True) -> dict[str, ManagedEntry]:
        """The entries, with "absent" and "unreadable" folded into ``{}``.

        Right for the polling callers (``entries()``/``get()``, the engine's
        upkeep pass, ``managed_refresh``): one bad file must not take them
        down, and "no entries" and "can't tell" lead them to the same
        no-op. A caller about to write or delete cannot make that trade and
        uses :meth:`_read_for_write` or :meth:`_load` instead.
        """
        entries, reason = self._load(warn=warn)
        if entries is None:
            if warn:
                _logger.warning(
                    f"Managed session registry {self.path} is unreadable "
                    f"({reason}); treating it as empty."
                )
            return {}
        return entries

    def _read_for_write(self) -> dict[str, ManagedEntry]:
        """The entries, refusing a registry this build cannot model in full.

        Raises:
            SessionError: The file could not be read or carries a
                ``schemaVersion`` this build does not understand. Every
                mutator goes through here, because ``_write`` persists only
                the rows that were read back.
        """
        entries, reason = self._load()
        if entries is None:
            raise SessionError(
                f"The managed session registry {self.path} cannot be read "
                f"({reason}), and the rows in it are live sessions' "
                "reservations; refusing to rewrite it. Exit any managed "
                "session, then remove the file to start fresh."
            )
        return entries

    def _write(self, entries: Mapping[str, ManagedEntry]) -> None:
        """Persist ``entries``, or unlink the registry file when there are
        none left. Shared by every mutator, and the dead-PID sweep will
        reuse it too, so "an empty registry is no file" has exactly one
        implementation. The root directory is already private by the time
        any mutator reaches here (``_lock`` makes it so), and
        ``atomic_write_json`` chmods it again to 0700 on every write, so
        this needs no ``_mkdir_private`` call of its own.
        """
        if not entries:
            self.path.unlink(missing_ok=True)
            return
        atomic_write_json(self.path, {
            "schemaVersion": REGISTRY_SCHEMA_VERSION,
            "sessions": {sid: e.to_json() for sid, e in sorted(entries.items())},
        })

    # Lock-free reads: writes are atomic renames, so a reader sees either
    # generation, never a torn file.
    def entries(self) -> dict[str, ManagedEntry]:
        return self._read()

    def get(self, session_id: str) -> ManagedEntry | None:
        return self._read().get(session_id)

    def get_locked(
        self, session_id: str, *, timeout: float | None = None
    ) -> ManagedEntry | None:
        """:meth:`get`, serialized with every mutator and :meth:`sweep`.

        For a check that orders a profile write against the sweep (see its
        docstring): when the row is present here, any sweep that drops it
        takes the lock after this read, so its profile deletion comes after
        this read as well. The lock is held for one file read and no
        liveness check, keeping the hold short. ``timeout`` overrides the
        registry's lock wait for callers already holding other locks.

        Raises:
            LockError: The registry lock stayed held past the timeout.
        """
        with self._lock(timeout):
            return self._read().get(session_id)

    def live_entries(self) -> list[ManagedEntry]:
        return [e for e in self._read().values() if entry_is_live(e)]

    def busy_counts(self, entries: Iterable[ManagedEntry]) -> dict[AccountRef, int]:
        """Busy sessions per account (``entry_is_busy``): a reservation with
        no Claude record yet counts, an idle or waiting one does not. This
        is the sole busy definition managed sessions use — the count that
        feeds balance's projected 5h-window load — shared by placement
        (``allocate``'s ``choose``) and the engine's lane-0 ranking
        (``autoswitch._busy_by_slot``, which delegates here rather than
        re-filtering the same entries a second time)."""
        counts: dict[AccountRef, int] = {}
        for entry in entries:
            if entry_is_busy(self.session_dir(entry.session_id), entry):
                counts[entry.account] = counts.get(entry.account, 0) + 1
        return counts

    def allocate(
        self,
        session_id: str,
        choose: Callable[[Mapping[AccountRef, int]], tuple[AccountRef, str] | None],
        *,
        pid: int,
        proc_start: str | None,
        reason: str = REASON_LAUNCH,
    ) -> ManagedEntry | None:
        """Pick-and-reserve atomically.

        ``choose`` receives live busy counts per account (reservations
        included) and returns ``(account, source)`` or None; it runs under
        the registry lock, so two concurrent launches can never both place
        onto the count the other one is about to raise.

        That atomicity is paid for in lock hold time, and this is the
        launch path: the lock is held across one ``entry_is_live`` check
        per registry entry (a ``ps`` call each on macOS, each with its own
        timeout) plus a session-record read per entry for the busy count,
        so a few slow probes can outlast a concurrent launch's lock wait
        and turn it into a lock error. The launch path therefore waits
        longer for the lock than the pollers do — see
        ``LAUNCH_LOCK_TIMEOUT_S``, which ``managed_launch.run_auto`` builds
        its registry with.

        Raises:
            LockError: The registry lock stayed held past the timeout.
            SessionError: The registry cannot be read (see
                :meth:`_read_for_write`).
            ValueError: ``session_id`` is not a managed id, is already
                registered, or the chosen row would not read back.
        """
        if not is_managed_session_id(session_id):
            raise ValueError(f"{session_id!r} is not a managed session id")
        if not _valid_pid(pid):
            raise ValueError(f"invalid pid: {pid!r}")
        with self._lock():
            entries = self._read_for_write()
            if session_id in entries:
                raise ValueError(f"{session_id} is already registered")
            live = [e for e in entries.values() if entry_is_live(e)]
            picked = choose(self.busy_counts(live))
            if picked is None:
                return None
            account, source = picked
            _validate_row(account, pid, source)
            if proc_start is None:
                # No start stamp to match the pid against later (``ps``
                # unavailable or timed out), so this row's liveness degrades
                # to bare "is the pid alive": a recycled pid keeps it alive
                # and nothing reclaims it until that pid dies. Safe (a
                # leaked row, never a deletion) but worth saying out loud.
                _logger.warning(
                    f"Managed session {session_id}: reserving PID {pid} "
                    "without a process start stamp; the row cannot be "
                    "reclaimed until that PID is gone."
                )
            now = utc_now_iso(self._clock)
            entry = ManagedEntry(
                session_id=session_id,
                account=account,
                pid=pid,
                proc_start=proc_start,
                source=source,
                created_at=now,
                last_assigned_at=now,
                last_reason=reason,
            )
            entries[session_id] = entry
            self._write(entries)
            return entry

    def update(self, session_id: str, **changes: object) -> ManagedEntry | None:
        unknown = set(changes) - _UPDATABLE
        if unknown:
            raise TypeError(f"not updatable: {sorted(unknown)}")
        with self._lock():
            entries = self._read_for_write()
            entry = entries.get(session_id)
            if entry is None:
                return None
            entry = replace(entry, **changes)
            _validate_row(entry.account, entry.pid, entry.source)
            entries[session_id] = entry
            self._write(entries)
            return entry

    def remove(self, session_id: str) -> ManagedEntry | None:
        with self._lock():
            entries = self._read_for_write()
            entry = entries.pop(session_id, None)
            if entry is not None:
                self._write(entries)
            return entry

    def sweep(self) -> SweepResult:
        """Drop dead entries, then remove their profiles and stale orphan
        dirs.

        Only entry removal happens under the lock: dead entries are read,
        partitioned from the live ones, dropped from the registry, and the
        file rewritten (or unlinked, once nothing is left) — all before
        the lock is released — so no writer can pass its liveness
        pre-check against an entry that is about to disappear. Liveness
        (``entry_is_live``) also runs under the lock — each check is a
        ``ps`` call on macOS — so lock hold time scales with the number of
        registry entries; it is checked once per entry, partitioning into
        ``dead``/``live`` in that single pass rather than two, so the live
        set can be handed back alongside the removed one instead of
        costing a second pass later.

        Removing the profile DIRECTORIES happens after the lock is
        released, not under it: a writer that reads the registry, finds
        this entry still live, and starts writing to the profile just
        before the lock above is taken can still be mid-write when this
        deletes the directory out from under it. The credential writer
        closes this by re-checking the registry entry under the registry
        lock both immediately before and immediately after it writes,
        discarding what it just wrote if the entry it stamped is gone by
        the time it checks again.

        A dead entry's PID being gone does not mean nothing is using its
        profile any more — a child process the dead PID spawned (the Bash
        tool, a detached tmux pane, a nohup'd process) can inherit
        CLAUDE_CONFIG_DIR and keep writing there after the parent that
        owned the registry entry exits. So the profile directory is only
        removed once ``profile_is_quiescent`` agrees nothing is reading or
        writing it; otherwise the entry is still dropped (it is dead, and
        a stale reservation must not linger), but the directory is left
        alone. With no registry entry any more it is now an untracked
        orphan, reclaimed by the orphan pass below once it actually goes
        quiescent and ages past ``ORPHAN_GRACE_S``. Either way the entry
        is in ``.removed`` — that reflects the registry row being gone,
        not whether the directory was deleted (see ``SweepResult``).

        A registry this build cannot read in full is not swept at all: an
        empty result, one log line, nothing deleted. ``_read`` folds
        "absent" and "unreadable" into an empty dict — right for the
        polling callers, wrong for this one, which turns "not in the
        registry" into an rmtree. Reading a truncated ``managed.json`` as
        empty would drop no rows (there are none to drop) but would hand
        ``_sweep_orphans`` an empty ``known`` set, and every live session's
        profile is then an untracked orphan: directory and keychain item
        deleted out from under a running Claude, with only the orphan
        grace window and ``profile_is_quiescent`` in the way — neither of
        which holds for a session that has not written a readable record
        yet (the reservation-to-startup gap a trust prompt holds open, a
        long ``claude -p`` run).
        """
        with self._lock():
            entries, unreadable = self._load()
            if entries is None:
                _logger.warning(
                    f"Managed session registry {self.path} cannot be read "
                    f"({unreadable}); skipping this sweep. No rows are "
                    "dropped and no profile is reclaimed until it can be "
                    "read again."
                )
                return SweepResult(removed=[], live=[])
            dead: list[ManagedEntry] = []
            live: list[ManagedEntry] = []
            for entry in entries.values():
                (live if entry_is_live(entry) else dead).append(entry)
            for entry in dead:
                del entries[entry.session_id]
            if dead:
                self._write(entries)
            known = set(entries)
        for entry in dead:
            profile_dir = self.session_dir(entry.session_id)
            if profile_is_quiescent(profile_dir):
                remove_managed_profile(profile_dir)
        self._sweep_orphans(known)
        return SweepResult(removed=dead, live=live)

    def _sweep_orphans(self, known: set[str]) -> list[str]:
        """Reclaim ``auto-*`` directories no registry row names.

        ``known`` has to come from a registry read that SUCCEEDED. An empty
        set from a readable registry with no rows is fine — every ``auto-*``
        directory really is untracked. An empty set from a failed read is
        not: it makes every live session's profile look untracked. This is
        the one place "not in ``known``" becomes an rmtree, so it re-checks
        that the registry is readable here rather than trusting its caller
        to have done it.
        """
        if self._load(warn=False)[0] is None:
            _logger.warning(
                f"Managed session registry {self.path} cannot be read; "
                "leaving untracked managed profiles alone."
            )
            return []
        if not self.root.is_dir():
            return []
        removed: list[str] = []
        now = self._clock()
        for path in self.root.iterdir():
            if (
                not path.is_dir()
                or path.is_symlink()
                or not is_managed_session_id(path.name)
                or path.name in known
            ):
                continue
            try:
                age = now - path.stat().st_mtime
            except OSError:
                continue
            if age < ORPHAN_GRACE_S or not profile_is_quiescent(path):
                continue
            if remove_managed_profile(path):
                removed.append(path.name)
        return removed
