"""Per-account usage table: last-known-good measurements + fetch/backoff state.

Replaces the all-or-nothing 15s snapshot that previously lived in
``cache/usage.json`` (now ``schemaVersion: 2``; a version-less legacy file is
treated as empty — its data had a 15s shelf life anyway). One failed round
trip no longer blanks every account: a failure updates the error/backoff
fields and never touches the last-good measurement (stale-on-error). The
table is shared by ``--list``/``--status`` (on-demand refresh of stale
entries) and ``cswap auto`` (scheduled polling), so each learns from the
other's fetches.

The store persists only *measurements* (``lastGood``) and *fetch state*
(failures, backoff, poll schedule). Sentinel states ("api key",
"token expired", ...) are derived fresh by the collector on every pass and
overlaid on the read model (``UsageEntry.sentinel``) — never written to disk,
so a stale sentinel can't outlive the condition that produced it.

Locking protocol (never holds the lock across network I/O):
(a) lock → read, decide/claim the fetch set (stamp ``lastAttemptAt``) → unlock;
(b) fetch with no lock held;
(c) lock → re-read, merge outcomes, write → unlock.
The claim stamp lets concurrent collectors skip accounts another process
started fetching moments ago; a crashed claimer just ages out.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path

from claude_swap.locking import FileLock
from claude_swap.settings import atomic_write_json

SCHEMA_VERSION = 2

# Freshness is the reader's judgment per purpose, not a global TTL:
SERVE_TTL_S = 30.0  # fresher than this → serve without fetching
STALE_OK_S = 300.0  # trusted for switch decisions; older → headroom unknown
CLAIM_TTL_S = 10.0  # in-flight claim window: skip just-claimed accounts

# Failure backoff when the server sent no Retry-After: 30s · 2^(n-1), capped.
# Conservative placeholders until issue #85's debug logs settle the real cause.
BACKOFF_BASE_S = 30.0
BACKOFF_CAP_S = 600.0

# (email, organizationUuid) — the identity a slot number currently maps to.
Identity = tuple[str, str]


@dataclass(frozen=True)
class FetchRecord:
    """Outcome of one fetch attempt, as handed to :meth:`UsageStore.record`.

    Exactly one of three shapes:
    - success: ``error`` and ``sentinel`` are None (``usage`` may still be
      None when the response carried no window data);
    - failure: ``error`` set (with optional ``retry_after_s``);
    - sentinel: ``sentinel`` set — a derived state ("token expired" with an
      owner present, ...), recorded as a no-op: sentinels are re-derived every
      pass and never persisted.
    """

    usage: dict | None = None
    error: str | None = None
    retry_after_s: float | None = None
    sentinel: str | None = None


@dataclass(frozen=True)
class UsageEntry:
    """Read model of one account's usage state at collect time.

    ``sentinel`` is the collector's live overlay (never persisted); all other
    fields mirror the stored row. ``age_s`` is the age of ``last_good``.
    """

    sentinel: str | None = None
    last_good: dict | None = None
    fetched_at: float | None = None
    age_s: float | None = None
    last_attempt_at: float | None = None
    consecutive_failures: int = 0
    last_error: str | None = None
    backoff_until: float | None = None
    next_poll_at: float | None = None
    poll_interval_s: float | None = None

    def fresh(self, now: float, ttl: float = SERVE_TTL_S) -> bool:
        return self.fetched_at is not None and (now - self.fetched_at) <= ttl

    def in_backoff(self, now: float) -> bool:
        return self.backoff_until is not None and now < self.backoff_until

    def claimed(self, now: float) -> bool:
        """A collector stamped this entry moments ago (fetch may be in flight)."""
        return (
            self.last_attempt_at is not None
            and (now - self.last_attempt_at) < CLAIM_TTL_S
        )

    def decision_value(self) -> dict | str | None:
        """The ``dict | sentinel | None`` value switch decisions run on.

        Sentinel wins; else last-good while it is recent enough to trust
        (≤ ``STALE_OK_S``); else None (unknown). Display code reads
        ``last_good``/``age_s`` directly instead — it may show older data,
        annotated with its age.
        """
        if self.sentinel is not None:
            return self.sentinel
        if (
            self.last_good is not None
            and self.age_s is not None
            and self.age_s <= STALE_OK_S
        ):
            return self.last_good
        return None


def _failure_backoff_s(consecutive_failures: int, retry_after_s: float | None) -> float:
    computed = min(
        BACKOFF_BASE_S * (2 ** max(0, consecutive_failures - 1)), BACKOFF_CAP_S
    )
    if retry_after_s is None:
        return computed
    # The server's word is the floor; our own curve may wait longer.
    return max(retry_after_s, computed)


class UsageStore:
    """The ``cache/usage.json`` table. All writes go read-modify-write under
    ``cache/.usage.lock``; reads are lock-free (writes are atomic replaces).

    Every method takes the caller's current ``identities`` map (slot number →
    ``(email, organizationUuid)``) and only ever touches rows for those slots:
    a row whose stored identity differs is invisible to reads and replaced on
    write, so slot reuse never serves the previous account's usage. Rows for
    slots outside the map are left alone (callers like ``--status`` operate
    on a single slot).
    """

    def __init__(self, cache_dir: Path, clock: Callable[[], float] = time.time):
        self.path = cache_dir / "usage.json"
        self._lock_path = cache_dir / ".usage.lock"
        self.clock = clock

    # -- raw I/O ------------------------------------------------------------

    def _lock(self) -> FileLock:
        return FileLock(self._lock_path)

    def _read_rows(self) -> dict[str, dict]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return {}
        if not isinstance(raw, dict) or raw.get("schemaVersion") != SCHEMA_VERSION:
            return {}  # legacy snapshot or future schema: start empty
        rows = raw.get("accounts")
        return rows if isinstance(rows, dict) else {}

    def _write_rows(self, rows: dict[str, dict]) -> None:
        atomic_write_json(
            self.path, {"schemaVersion": SCHEMA_VERSION, "accounts": rows}
        )

    @staticmethod
    def _matches(row: object, identity: Identity) -> bool:
        return (
            isinstance(row, dict)
            and row.get("email") == identity[0]
            and row.get("organizationUuid", "") == identity[1]
        )

    def _fresh_row(self, identity: Identity) -> dict:
        return {"email": identity[0], "organizationUuid": identity[1]}

    # -- read model -----------------------------------------------------------

    def entries(self, identities: dict[str, Identity]) -> dict[str, UsageEntry]:
        """Identity-guarded snapshot for the given slots (empty entry when the
        row is missing or belongs to a different account)."""
        now = self.clock()
        rows = self._read_rows()
        out: dict[str, UsageEntry] = {}
        for num, identity in identities.items():
            row = rows.get(num)
            if not self._matches(row, identity):
                out[num] = UsageEntry()
                continue
            fetched_at = row.get("fetchedAt")
            if not isinstance(fetched_at, (int, float)):
                fetched_at = None
            last_good = row.get("lastGood")
            out[num] = UsageEntry(
                last_good=last_good if isinstance(last_good, dict) else None,
                fetched_at=fetched_at,
                age_s=(now - fetched_at) if fetched_at is not None else None,
                last_attempt_at=_num_or_none(row.get("lastAttemptAt")),
                consecutive_failures=int(row.get("consecutiveFailures") or 0),
                last_error=row.get("lastError"),
                backoff_until=_num_or_none(row.get("backoffUntil")),
                next_poll_at=_num_or_none(row.get("nextPollAt")),
                poll_interval_s=_num_or_none(row.get("pollIntervalS")),
            )
        return out

    # -- writes ---------------------------------------------------------------

    def _mutate(
        self,
        identities: dict[str, Identity],
        nums: Iterable[str],
        mutator: Callable[[str, dict], None],
    ) -> None:
        """Read-modify-write rows for ``nums`` under the lock. A row whose
        stored identity mismatches is replaced with a fresh one first."""
        with self._lock():
            rows = self._read_rows()
            for num in nums:
                identity = identities[num]
                if not self._matches(rows.get(num), identity):
                    rows[num] = self._fresh_row(identity)
                mutator(num, rows[num])
            self._write_rows(rows)

    def claim(self, nums: Iterable[str], identities: dict[str, Identity]) -> None:
        """Stamp ``lastAttemptAt`` on the slots about to be fetched."""
        nums = list(nums)
        if not nums:
            return
        now = self.clock()
        self._mutate(identities, nums, lambda _n, row: row.update(lastAttemptAt=now))

    def record(
        self, outcomes: dict[str, FetchRecord], identities: dict[str, Identity]
    ) -> None:
        """Merge fetch outcomes. Success and failure are mutually exclusive
        writers: success resets the failure fields, failure never touches
        ``lastGood``/``fetchedAt``. Sentinel records are no-ops (derived state
        lives only in the collector's overlay)."""
        effective = {n: r for n, r in outcomes.items() if r.sentinel is None}
        if not effective:
            return
        now = self.clock()

        def apply(num: str, row: dict) -> None:
            rec = effective[num]
            row["lastAttemptAt"] = now
            if rec.error is None:
                row["lastGood"] = rec.usage
                row["fetchedAt"] = now
                row["consecutiveFailures"] = 0
                row["lastError"] = None
                row["backoffUntil"] = None
            else:
                failures = int(row.get("consecutiveFailures") or 0) + 1
                row["consecutiveFailures"] = failures
                row["lastError"] = rec.error
                row["backoffUntil"] = now + _failure_backoff_s(
                    failures, rec.retry_after_s
                )

        self._mutate(identities, effective.keys(), apply)

    def set_poll_plan(
        self,
        plans: dict[str, tuple[float | None, float | None]],
        identities: dict[str, Identity],
    ) -> None:
        """Persist the scheduler's per-slot ``(nextPollAt, pollIntervalS)``."""
        if not plans:
            return

        def apply(num: str, row: dict) -> None:
            next_poll_at, interval = plans[num]
            row["nextPollAt"] = next_poll_at
            row["pollIntervalS"] = interval

        self._mutate(identities, plans.keys(), apply)


def _num_or_none(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def with_sentinel(entry: UsageEntry, sentinel: str | None) -> UsageEntry:
    """Overlay a derived sentinel state on a stored entry (read model only)."""
    if sentinel is None:
        return entry
    return replace(entry, sentinel=sentinel)
