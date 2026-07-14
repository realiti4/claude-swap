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
from claude_swap import oauth
from claude_swap.poll_policy import (
    EDGE_BACKOFF_S,
    SERVE_TTL_S,
    parse_reset_ts,
)
from claude_swap.settings import atomic_write_json

SCHEMA_VERSION = 2

# Freshness is the reader's judgment per purpose, not a global TTL.
# SERVE_TTL_S (re-exported from poll_policy — fresher than this → serve
# without fetching) doubles as the per-token sustained-rate governor: see
# poll_policy's module docstring for the measured budget it must stay under.
STALE_OK_S = 300.0  # trusted for switch decisions; older → headroom unknown
CLAIM_TTL_S = 10.0  # in-flight claim window: skip just-claimed accounts

# Deliberate staleness (failure backoff, scheduler-chosen cadence) extends
# decision trust past STALE_OK_S, but never past this ceiling: a forever-failing
# account must eventually read as unknown so the unknown-path machinery
# (escalate-all, unhealthy ticks, verified failover) takes back over. The
# ceiling deliberately overrides even a Retry-After longer than itself —
# trust must never be server-controlled and unbounded.
TRUST_MAX_AGE_S = 3600.0

# A usage-endpoint 429 is a polling throttle, not a change in the account's
# real model quota: the endpoint budgets *usage requests* per token (see
# poll_policy), independent of the 5h/7d limits it reports. It does NOT move
# the account's real windows, and usage only rises within a window (monotone
# until the window resets), so last_good is a valid lower bound on the true
# usage right up to that reset. Trust it until then — data-driven, not a fixed
# clock: flipping it to "unknown" early (the old fixed 2h ceiling) made a
# throttled account an unusable switch target and drove failover flapping /
# all-exhausted sleeps even while the account was plainly fine. Once the window
# resets, usage is zeroed and last_good is obsolete → unknown. Matches Claude
# Code's own 2.1.208 "show last-known usage when rate-limited" behavior.
#
# Fallback ceiling for 429-stale data that carries no resets_at (older stored
# rows): still bounded so it can't be trusted forever. Non-429 failures always
# use TRUST_MAX_AGE_S (a timeout/network error is no evidence last_good holds).
RATE_LIMIT_TRUST_MAX_AGE_S = 7200.0

# Failure backoff when the server sent no Retry-After: 30s · 2^(n-1), capped.
BACKOFF_BASE_S = 30.0
BACKOFF_CAP_S = 600.0

# The usage endpoint enforces a per-access-token request budget on
# non-first-party User-Agents (proven 2026-07-11: an idle token, polling
# alone trips it; poll_policy documents the measured shape — an hour-scale
# rolling window, exact edge algorithm undocumented). Cumulative polling from
# cswap's own surfaces is exactly what saturates it. Retry-After tells the
# rules apart:
# - "Retry-After: 0" = the saturated-budget edge: the trailing hour's budget
#   is spent and frees only as old requests age out, so immediate retries
#   mostly prolong the oscillating state. Wait at least
#   poll_policy.EDGE_BACKOFF_S before probing again.
# - "Retry-After: N>0" = the burst rule (several rapid requests on one token
#   → hard block; measured: accurate, counts down, not extended by probing).
#   Honored as the wait, up to a safety cap so a pathological header can
#   never park an account for hours.
# The cap spans the usage endpoint's ~1h rolling window: a saturating burst
# blocks the token for up to a full trailing hour, and the server's Retry-After
# reports that horizon accurately. Capping below it (the old 900s) meant
# re-probing mid-window — each probe re-arms the block and prevents the token
# from ever draining — so an account could stay throttled indefinitely under
# steady polling (worst across machines sharing one token). The cap still
# bounds a pathological header to one window, never hours.
RETRY_AFTER_FLOOR_CAP_S = 3600.0

# A dead refresh-token lineage (the token endpoint answered ``invalid_grant``,
# e.g. "Refresh token not found or invalid") can never recover on its own —
# only a re-login helps. One such answer is already definitive: the server
# explicitly rejected the grant, which no transient 429/timeout/network blip
# does, so there is nothing to gain by retrying (and each retry with a dead
# token just draws a fresh 401/429). At this many strikes the account is
# quarantined: no more fetches, and the collector surfaces "re-login needed".
# A single success — or a credential refresh via login/add — resets the count
# and lifts the quarantine. Raise to 2 if a buffer against a one-off
# misclassification is ever wanted; the trade-off is a ~10-min-slower verdict
# (the failure backoff between the two strikes).
AUTH_DEAD_STRIKES = 1

# Fetch errors that prove the stored credential is permanently unusable (vs.
# transient 429/timeout/network). Only these advance the dead-token strike
# count; everything else leaves it untouched (a transient error is no evidence
# the token is alive *or* dead).
PERMANENT_AUTH_ERRORS = frozenset({"invalid_grant"})

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
    fields mirror the stored row, except ``age_s`` (the age of ``last_good``)
    and ``trust_extended``, both computed at snapshot time.
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
    # When this token last answered 429 (any Retry-After). Deliberately NOT
    # cleared by a later success: the planner keeps the cadence floored at
    # poll_policy.POST_429_MIN_INTERVAL_S until RECENT_429_WINDOW_S has
    # passed, giving the saturated rolling-hour window time to age out.
    last_429_at: float | None = None
    # Consecutive permanent-auth failures (``invalid_grant``). At or above
    # AUTH_DEAD_STRIKES the token is treated as dead: see ``token_dead``.
    auth_dead_strikes: int = 0
    # Staleness past STALE_OK_S is still decision-trusted when it is
    # *deliberate*: the server is refusing fresher data (failure state), or the
    # scheduler itself chose the cadence (within nextPollAt). Capped at
    # TRUST_MAX_AGE_S. Computed by UsageStore.entries().
    trust_extended: bool = False

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

    def token_dead(self, threshold: int = AUTH_DEAD_STRIKES) -> bool:
        """Whether the stored credential's refresh-token lineage is provably dead.

        True once ``invalid_grant`` has recurred ``threshold`` times without an
        intervening success. Such an account is quarantined: not fetched (see
        ``due_candidate`` and the collector) and surfaced as "re-login needed".
        """
        return self.auth_dead_strikes >= threshold

    def decision_value(self) -> dict | str | None:
        """The ``dict | sentinel | None`` value switch decisions run on.

        Sentinel wins; else last-good while it is recent enough to trust
        (≤ ``STALE_OK_S``, or ``trust_extended`` for deliberate staleness);
        else None (unknown). Display code reads ``last_good``/``age_s``
        directly instead — it may show older data, annotated with its age.
        """
        if self.sentinel is not None:
            return self.sentinel
        if (
            self.last_good is not None
            and self.age_s is not None
            and (self.age_s <= STALE_OK_S or self.trust_extended)
        ):
            return self.last_good
        return None


def due_candidate(
    candidates: list[str], entries: dict[str, UsageEntry], now: float
) -> str | None:
    """The due candidate with the stalest data, or None.

    Due = past its ``nextPollAt`` and not in failure backoff. Sentinel
    accounts (api-key / no credentials) have nothing to fetch. A
    perpetually failing account can't monopolize the slot: its backoff
    removes it from the due set between attempts.

    Shared by the auto engine and the TUI watch view so both pick the same
    single alternate to poll per pass. Poll plans
    (``nextPollAt``/``pollIntervalS``) are written by whichever collector
    fetched (see the plan persistence in ``_collect_usage_entries``), so
    every surface inherits the same adaptive cadence.
    """
    due: list[tuple[int, float, str]] = []
    for num in candidates:
        entry = entries.get(num)
        if entry is None:
            due.append((0, 0.0, num))
            continue
        if entry.sentinel is not None:
            continue
        if entry.token_dead():
            continue  # dead refresh-token: quarantined, needs a re-login
        if entry.in_backoff(now):
            continue
        if entry.next_poll_at is not None and now < entry.next_poll_at:
            continue
        if entry.fetched_at is None:
            due.append((0, 0.0, num))
        else:
            due.append((1, entry.fetched_at, num))
    if not due:
        return None
    due.sort()
    return due[0][2]


def _rate_limited_trust_ok(
    last_good: dict | None, age_s: float | None, now: float
) -> bool:
    """Whether 429-stale ``last_good`` is still trustworthy for decisions.

    Usage rises monotonically within a window, so a rate-limited (frozen)
    last_good is a valid lower bound until its window resets. Three cases:

    - a window reset is still ahead → trusted (the measured value can only be
      an under-estimate of the true usage until then);
    - every window's reset is already past → obsolete (usage was zeroed at the
      reset; the stored value no longer describes reality) → not trusted;
    - no reset info at all (older stored rows) → fall back to the bounded
      RATE_LIMIT_TRUST_MAX_AGE_S so it still can't be trusted forever.
    """
    windows = oauth.relevant_windows(last_good) if last_good is not None else []
    reset_tss = [
        ts
        for _, _, resets_at in windows
        if (ts := parse_reset_ts(resets_at)) is not None
    ]
    if reset_tss:
        return now < max(reset_tss)
    return age_s is not None and age_s <= RATE_LIMIT_TRUST_MAX_AGE_S


def _failure_backoff_s(consecutive_failures: int, retry_after_s: float | None) -> float:
    computed = min(
        BACKOFF_BASE_S * (2 ** max(0, consecutive_failures - 1)), BACKOFF_CAP_S
    )
    if retry_after_s is None:
        return computed
    if retry_after_s == 0:
        # Saturated-budget edge: wait before probing again.
        return min(max(computed, EDGE_BACKOFF_S), BACKOFF_CAP_S)
    # Burst rule: wait at least what the server asked (up to the safety cap);
    # our own curve may wait longer.
    return max(min(retry_after_s, RETRY_AFTER_FLOOR_CAP_S), computed)


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
            age_s = (now - fetched_at) if fetched_at is not None else None
            consecutive_failures = int(row.get("consecutiveFailures") or 0)
            next_poll_at = _num_or_none(row.get("nextPollAt"))
            last_attempt_at = _num_or_none(row.get("lastAttemptAt"))
            # Strict < mirrors due_candidate: at nextPollAt the entry is due,
            # its staleness no longer scheduler-chosen. A live claim keeps the
            # trust bridge up: when another collector just won the fetch, this
            # reader must not flip trusted → unknown (and e.g. count an
            # unhealthy tick) for the seconds the result is in flight.
            # A usage-endpoint 429 throttles polling without moving the
            # account's real windows. Usage is monotone within a window, so
            # last_good is a valid lower bound until that window resets: trust it
            # right up to the earliest future reset (data-driven, no fixed
            # clock). Rows with no reset info fall back to a bounded ceiling. A
            # non-429 failure (timeout/network) is no evidence last_good still
            # holds, so it always uses the general ceiling.
            if row.get("lastError") == "http-429":
                within_ceiling = _rate_limited_trust_ok(
                    last_good if isinstance(last_good, dict) else None, age_s, now
                )
            else:
                within_ceiling = age_s is not None and age_s <= TRUST_MAX_AGE_S
            trust_extended = (
                within_ceiling
                and (
                    consecutive_failures > 0
                    or (next_poll_at is not None and now < next_poll_at)
                    or (
                        last_attempt_at is not None
                        and (now - last_attempt_at) < CLAIM_TTL_S
                    )
                )
            )
            out[num] = UsageEntry(
                last_good=last_good if isinstance(last_good, dict) else None,
                fetched_at=fetched_at,
                age_s=age_s,
                last_attempt_at=last_attempt_at,
                consecutive_failures=consecutive_failures,
                last_error=row.get("lastError"),
                backoff_until=_num_or_none(row.get("backoffUntil")),
                next_poll_at=next_poll_at,
                poll_interval_s=_num_or_none(row.get("pollIntervalS")),
                last_429_at=_num_or_none(row.get("last429At")),
                auth_dead_strikes=int(row.get("authDeadStrikes") or 0),
                trust_extended=trust_extended,
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

    def reserve(
        self,
        nums: Iterable[str],
        identities: dict[str, Identity],
        *,
        respect_plans: bool,
    ) -> list[str]:
        """Atomically win the right to fetch: re-check eligibility and stamp
        ``lastAttemptAt`` in one locked pass, returning only the slots won.

        Deciding eligibility on a lock-free :meth:`entries` read and then
        claiming separately lets two collectors both pass the check and both
        fetch; the re-check under the lock closes that window. Eligibility:
        not quarantined (dead token), not in failure backoff, not claimed
        within ``CLAIM_TTL_S``, and then by caller mode —

        - ``respect_plans=True`` (on-demand callers: list/status/switch,
          dashboards): the entry must be stale (older than ``SERVE_TTL_S``)
          *and* poll-due (past ``nextPollAt``, or no plan yet).
        - ``respect_plans=False`` (the auto engine's deliberate schedule):
          poll-due *or* stale — a due entry may be re-fetched inside the
          serve TTL (that is how the bounded urgent cadence beats the TTL),
          and an escalation refresh may fetch a not-yet-due candidate.
        """
        nums = list(nums)
        if not nums:
            return []
        now = self.clock()
        won: list[str] = []
        with self._lock():
            rows = self._read_rows()
            for num in nums:
                identity = identities[num]
                row = rows.get(num)
                if not self._matches(row, identity):
                    rows[num] = row = self._fresh_row(identity)
                elif not _row_eligible(row, now, respect_plans):
                    continue
                row["lastAttemptAt"] = now
                won.append(num)
            if won:
                self._write_rows(rows)
        return won

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
                row["authDeadStrikes"] = 0  # a success proves the token is alive
            else:
                failures = int(row.get("consecutiveFailures") or 0) + 1
                row["consecutiveFailures"] = failures
                row["lastError"] = rec.error
                if rec.error == "http-429":
                    # Kept across later successes: the poll planner floors the
                    # cadence while a 429 is recent (see UsageEntry.last_429_at).
                    row["last429At"] = now
                row["backoffUntil"] = now + _failure_backoff_s(
                    failures, rec.retry_after_s
                )
                # Only a permanent-auth failure advances the dead-token count; a
                # transient error (429/timeout) leaves it as-is — it is no
                # evidence either way and must not reset a real dead-token tally.
                if rec.error in PERMANENT_AUTH_ERRORS:
                    row["authDeadStrikes"] = int(row.get("authDeadStrikes") or 0) + 1

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

    def clear_dead_token(
        self, nums: Iterable[str], identities: dict[str, Identity]
    ) -> None:
        """Lift the dead-token quarantine for slots whose credential was refreshed.

        Called after a re-login/add rewrites a slot's stored credential: the
        strike count (and the failure/backoff state riding with it) no longer
        reflects reality, and the account must become fetch-eligible again so the
        next pass can prove the new token good. A no-op for rows with no strikes.
        """
        nums = list(nums)
        if not nums:
            return

        def apply(_num: str, row: dict) -> None:
            row["authDeadStrikes"] = 0
            row["consecutiveFailures"] = 0
            row["lastError"] = None
            row["backoffUntil"] = None

        self._mutate(identities, nums, apply)


def _num_or_none(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _row_eligible(row: dict, now: float, respect_plans: bool) -> bool:
    """Fetch eligibility of a stored row, evaluated under the write lock
    (see :meth:`UsageStore.reserve` for the two caller modes)."""
    if int(row.get("authDeadStrikes") or 0) >= AUTH_DEAD_STRIKES:
        return False
    backoff_until = _num_or_none(row.get("backoffUntil"))
    if backoff_until is not None and now < backoff_until:
        return False
    last_attempt = _num_or_none(row.get("lastAttemptAt"))
    if last_attempt is not None and (now - last_attempt) < CLAIM_TTL_S:
        return False
    fetched_at = _num_or_none(row.get("fetchedAt"))
    stale = fetched_at is None or (now - fetched_at) > SERVE_TTL_S
    next_poll_at = _num_or_none(row.get("nextPollAt"))
    poll_due = next_poll_at is not None and now >= next_poll_at
    if respect_plans:
        return stale and (poll_due or next_poll_at is None)
    return poll_due or stale


def with_sentinel(entry: UsageEntry, sentinel: str | None) -> UsageEntry:
    """Overlay a derived sentinel state on a stored entry (read model only)."""
    if sentinel is None:
        return entry
    return replace(entry, sentinel=sentinel)
