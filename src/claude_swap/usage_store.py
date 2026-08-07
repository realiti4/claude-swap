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
(a) lock → read, decide/claim the fetch set (stamp ``claimUntil``) → unlock;
(b) fetch with no lock held;
(c) lock → re-read, merge outcomes, clear the claim, write → unlock.
The bounded lease lets concurrent collectors skip accounts another process is
still fetching; recording the outcome releases it and a crashed claimer ages
out. The collector records per *batch*, so a fast account's lease is held
until its batch's slowest fetch lands — the lease TTL is sized for the whole
batch, not one request.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path

from claude_swap.locking import FileLock
from claude_swap import oauth
from claude_swap.poll_policy import (
    EDGE_BACKOFF_S,
    EXHAUSTED_INTERVAL_S,
    JITTER_FRAC,
    RECENT_429_WINDOW_S,
    RESET_SLACK_S,
    SERVE_TTL_S,
    parse_reset_ts,
)
from claude_swap.settings import atomic_write_json

SCHEMA_VERSION = 2

# Freshness is the reader's judgment per purpose, not a global TTL.
# SERVE_TTL_S (re-exported from poll_policy — fresher than this → serve
# without fetching) doubles as the per-account sustained-rate governor: see
# poll_policy's module docstring for the measured budget it must stay under.
STALE_OK_S = 300.0  # trusted for switch decisions; older → headroom unknown
# Covers the bounded refresh + usage path, the full inventory's stagger, and
# executor queueing, so another surface cannot reclaim a request that is still
# in flight. A crashed collector waiting 90s remains below the provider-safe
# polling interval.
CLAIM_TTL_S = 90.0  # in-flight claim window: skip just-claimed accounts
LEGACY_CLAIM_TTL_S = 10.0  # additive-schema overlap with older collectors


def _live_claim(
    claim_until: float | None, last_attempt_at: float | None, now: float
) -> bool:
    """Whether a collector's fetch lease on a row is still live.

    ``claim_until`` (the fenced lease) wins when present; rows written by an
    older process fall back to ``lastAttemptAt`` + ``LEGACY_CLAIM_TTL_S``.
    The single source of truth for claim liveness — ``UsageEntry.claimed``,
    ``entries()``, and ``_row_eligible`` must all agree, or concurrent
    collectors double-fetch. (``record()`` deliberately checks only the
    fenced form; see its comment.)
    """
    if claim_until is not None:
        return now < claim_until
    return (
        last_attempt_at is not None
        and (now - last_attempt_at) < LEGACY_CLAIM_TTL_S
    )

# Deliberate staleness (failure backoff, scheduler-chosen cadence) extends
# decision trust past STALE_OK_S, but never past this ceiling: a forever-failing
# account must eventually read as unknown so the unknown-path machinery
# (escalate-all, unhealthy ticks, verified failover) takes back over. The
# ceiling deliberately overrides even a Retry-After longer than itself —
# trust must never be server-controlled and unbounded.
TRUST_MAX_AGE_S = 3600.0

# A usage-endpoint 429 is a polling throttle, not a change in the account's
# real model quota: the endpoint budgets *usage requests* (scope is
# regime-dependent; see poll_policy), independent of the 5h/7d limits it
# reports. It does NOT move
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
# Exponent clamp: a permanently failing account increments its failure count
# forever, and 2**n stops converting to float at n >= 1024 (OverflowError).
# The curve already saturates at BACKOFF_CAP_S by shift 5, so any cap above
# that is behaviour-preserving.
BACKOFF_MAX_SHIFT = 32

# The usage endpoint enforces a request budget on non-first-party
# User-Agents (proven 2026-07-11: an idle token, polling alone trips it;
# poll_policy documents the measured shape — an hour-scale window, exact edge
# algorithm undocumented). Nothing below depends on whether the budget is
# scoped to the account/org or to the token — the wait comes from the
# server's own deadline either way. Cumulative polling from cswap's own
# surfaces is what saturates it.
# Retry-After tells the rules apart:
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
# reports that horizon accurately — it counts down to a fixed wall-clock
# deadline and is NOT re-armed by probing (measured: three probes in one
# episode all reported the same deadline). Capping below it (the old 900s) just
# meant re-probing two or three times inside a block that was going to last the
# full window anyway — wasted requests against the very budget we are trying to
# let recover, with no benefit. Honoring the whole Retry-After spends one
# request per block instead. The cap still bounds a pathological header to one
# window, never hours.
#
# Honoring it EXACTLY is not enough: the retry lands ON the deadline, where
# the server is not reliably ready. Measured over this machine's whole log
# (re-measured 2026-08-03, round 8 — the round-7 "21 of 36"/"2 of 38" figures
# do reproduce under the method below, on the OTHER of its two valid
# readings; round 8 switched readings, see the METHOD paragraph below for
# which and why the two differ; these figures age as the log grows —
# re-derive rather than trust them verbatim, using the method below), 20 of
# 35 lapses re-blocked within 900s
# of their own deadline (+2s..+887s), each earning a fresh full hour; the
# next one is +1004s, so the distribution is bimodal and 900s is the edge of
# the band — with only 13s of clearance (900 - 887), not the 185s an earlier,
# wrong figure implied. ("20 of 35", not "of 38": the denominator is
# restricted to the 35 POSITIVE lapse gaps, matching the numerator — 3 of the
# 38 raw gaps are negative, and mixing them into an "of 38" denominator
# understates the fraction. The negative gaps are NOT a uniform mechanism —
# measured per gap: acct2 block10→11 (no within-block revision at all, a
# single-observation block whose NEXT block's opening simply lands later);
# block11→12 (the deadline was revised BACKWARD 903s mid-block, the opposite
# of "forward"); block12→13 (deadline unchanged mid-block). None is a forward
# revision. What they share is only the effect, not the cause: each next
# block's opening observation lands before the prior block's own deadline —
# a real event, just not a lapse to measure. Excluding them from a lapse-gap
# analysis is still correct on that basis: a block that never lapsed has no
# lapse gap.) The margin is
# ABSOLUTE, not a fraction of the ask: Retry-After counts down to a fixed
# deadline, so a machine polling into a block another one opened sees only
# the remainder, and a fraction of that shrinks toward zero exactly when it
# matters.
#
# METHOD (re-derive with this, not ad hoc, to get the same numbers): parse
# ``claude-swap.log`` lines "Usage fetch failed for account N: http-429,
# retry-after Ks"; EXCLUDE K == 0 (the saturated-budget edge above, a
# different rule from the burst block this margin is sized against — 439 such
# lines in the 2026-08-03 log, and mixing them into the block counts is what
# produced the earlier wrong figures); group the remaining events per account
# into blocks by clustering on the deadline ``ts + K`` (a new block opens when
# the deadline jumps forward past a tolerance — stable for any tolerance from
# 5s to 300s). 41 blocks result, 40 of them opened at exactly K=3600; 34 of
# 75 observations land mid-block (not block-opening); the lapse gap is each
# block's next observation's timestamp minus the PRIOR block's deadline —
# where a block has more than one observation (a mid-block deadline
# revision), "the block's deadline" means its OPENING observation's deadline
# — the reading used below and the same one "40 of them opened at exactly
# K=3600" reads, so every figure in this comment stays internally consistent
# on one reading. Picking the block's LAST observation's deadline instead
# does NOT fail to reproduce: it is equally stable at every tolerance from 5s
# to 900s and gives "21 of 36"/"2 of 38", the figures this comment used to
# carry before round 8. The two readings differ on exactly ONE gap (acct2
# block11→12): its opening observation asks K=3600, but two later
# observations in the same block report a deadline 903s EARLIER, so OPENING
# reads that gap as -900.1s (excluded, negative) while LAST reads it as
# +3.0s (included, a lapse inside the band). Either reading sizes the
# constant identically — both put the band edge at +887s, so
# RETRY_AFTER_MARGIN_S = 900 clears it with 13s to spare under both.
RETRY_AFTER_MARGIN_S = 900.0
# Bounds Retry-After + MARGIN_S, so a pathological header still cannot park an
# account for hours. Sized to clear the measured shape: 40 of 41 observed
# blocks opened at exactly 3600 (re-measured 2026-08-03, method above), and
# 3600 + 900 = this. At an ask of exactly this value the margin is already
# fully eaten, and beyond it the wait is SHORTER than the server asked — a
# guaranteed 429, costing one request rather than a fresh hour (probing does
# not re-arm a block). Above 3600 the cap eats
# the margin (a 3700s ask keeps only +800s), which is deliberate — an ask past
# the measured window is already outside the evidence, and bounding it matters
# more there than preserving a margin derived from hour-scale blocks. If real
# blocks ever open above 3600, raise this with the shape rather than letting
# the cap silently shorten the margin.
RETRY_AFTER_FLOOR_CAP_S = 4500.0

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
# the token is alive *or* dead). "no_refresh_token" is equally unretryable:
# a credential with no refresh token cannot be healed by any retry.
PERMANENT_AUTH_ERRORS = frozenset({"invalid_grant", "no_refresh_token"})

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
    # Fingerprint of the credential whose refresh token was POSTed when a
    # permanent auth error came back. Strikes bind to it: token_dead() holds
    # only while the stored credential still fingerprints to the struck
    # generation, so any credential-writing path heals the strike without a
    # bespoke clear call. None on non-auth outcomes (and from writers that
    # predate the field — those strikes bind unconditionally, the legacy
    # behavior).
    struck_fp: str | None = None


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
    # Fingerprint of the generation the strikes condemned (absent on legacy
    # rows → strikes bind unconditionally). See ``token_dead``.
    struck_fingerprint: str | None = None
    # Staleness past STALE_OK_S is still decision-trusted when it is
    # *deliberate*: the server is refusing fresher data (failure state), or the
    # scheduler itself chose the cadence (within nextPollAt). Capped at
    # TRUST_MAX_AGE_S. Computed by UsageStore.entries().
    trust_extended: bool = False
    # Appended to preserve positional compatibility for the older read-model
    # fields while exposing whether a fetch lease is currently live.
    claim_until: float | None = None

    def fresh(self, now: float, ttl: float = SERVE_TTL_S) -> bool:
        return self.fetched_at is not None and (now - self.fetched_at) <= ttl

    def in_backoff(self, now: float) -> bool:
        return self.backoff_until is not None and now < self.backoff_until

    def recent_429(self, now: float) -> bool:
        """Whether this token 429'd recently enough to keep the post-429 cadence.

        Recency is measured from when the 429's honored backoff *lifts*, not
        from the 429 itself. An hour-scale Retry-After is honored as one long
        backoff during which no attempt runs, so the only 429 stamp is at the
        block's start; measuring recency from the stamp would make the first
        post-block success (which can't happen until the backoff lifts) see a
        window that has already fully elapsed — the AIMD growth and the
        POST_429 floor would never engage, and machines sharing the token could
        not converge. Anchoring on the backoff's end keeps "recent" true across
        the first post-block success while a short (``Retry-After: 0``) block
        still expires normally. Bounded by ``RECENT_429_WINDOW_S`` past the
        anchor so it clears once the saturated window has aged out.
        """
        if self.last_429_at is None:
            return False
        anchor = self.last_429_at
        # Use the backoff anchor only while the LIVE backoff is a 429 backoff.
        # last429At is never cleared, but backoffUntil/lastError are rewritten by
        # any later failure — so without the lastError guard an unrelated timeout
        # on a token that 429'd long ago would install a fresh backoffUntil and
        # spuriously re-arm the post-429 cadence. Success clears lastError and
        # backoffUntil; only a 429 sets last429At and backoffUntil together.
        if (
            self.last_error == "http-429"
            and self.backoff_until is not None
            and self.backoff_until > anchor
        ):
            anchor = self.backoff_until
        return now < anchor + RECENT_429_WINDOW_S

    def claimed(self, now: float) -> bool:
        """Whether another collector's bounded fetch lease is still live."""
        return _live_claim(self.claim_until, self.last_attempt_at, now)

    def token_dead(
        self,
        threshold: int = AUTH_DEAD_STRIKES,
        stored_fp: str | None = None,
    ) -> bool:
        """Whether the stored credential's refresh-token lineage is provably dead.

        True once ``invalid_grant`` has recurred ``threshold`` times without an
        intervening success. Such an account is quarantined: not fetched (see
        ``due_candidate`` and the collector) and surfaced as "re-login needed".

        Strikes condemn the GENERATION that was POSTed, not the slot: when
        the caller passes the currently-stored credential's fingerprint and
        it differs from the struck one, the credential has been replaced
        since the verdict — the strike no longer applies (any writing path
        heals it, mirroring #142's identity-not-slot rule). A row struck
        before fingerprints were recorded binds unconditionally.
        """
        if self.auth_dead_strikes < threshold:
            return False
        if (
            stored_fp is not None
            and self.struck_fingerprint is not None
            and stored_fp != self.struck_fingerprint
        ):
            return False
        return True

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


def _plan_oversleeps_interval(
    next_poll_at: float | None,
    poll_interval_s: float | None,
    now: float,
) -> bool:
    """Whether a deadline cannot have come from the bounded planner."""
    if next_poll_at is None:
        return False
    interval = max(poll_interval_s or EXHAUSTED_INTERVAL_S, EXHAUSTED_INTERVAL_S)
    latest_normal_poll = now + interval * (1.0 + JITTER_FRAC) + RESET_SLACK_S
    return next_poll_at > latest_normal_poll


def plan_oversleeps_interval(entry: UsageEntry, now: float) -> bool:
    """Whether a row carries an obsolete reset-parked plan.

    Reset-parking releases stored a distant reset deadline while retaining the
    much shorter learned interval. Detect that impossible shape structurally,
    independent of the current model selection, so changing scoped models
    cannot leave an otherwise usable account parked until the old reset.
    """
    return _plan_oversleeps_interval(
        entry.next_poll_at,
        entry.poll_interval_s,
        now,
    )


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
        if (
            entry.next_poll_at is not None
            and now < entry.next_poll_at
            and not plan_oversleeps_interval(entry, now)
        ):
            continue
        if entry.fetched_at is None:
            due.append((0, 0.0, num))
        else:
            due.append((1, entry.fetched_at, num))
    if not due:
        return None
    due.sort()
    return due[0][2]


def _earliest_reset(last_good: dict | None, models: tuple[str, ...] = ()) -> float | None:
    """Epoch of the soonest relevant window to roll over, or None if unknown.

    The soonest one is what matters: once it resets, usage there is zeroed and
    the whole snapshot is obsolete, so a later window cannot rescue it. Windows
    carrying no ``resets_at`` contribute nothing rather than a guess. A
    non-dict ``last_good`` needs no guard here: ``oauth.relevant_windows``
    already returns ``[]`` for any non-dict input.
    """
    resets = [
        ts
        for _, _, resets_at in oauth.relevant_windows(last_good, models)
        if (ts := parse_reset_ts(resets_at)) is not None
    ]
    return min(resets) if resets else None


def _rate_limited_trust_ok(
    last_good: dict | None,
    age_s: float | None,
    now: float,
    models: tuple[str, ...] = (),
) -> bool:
    """Whether 429-stale ``last_good`` is still trustworthy for decisions.

    Usage rises monotonically within a window, so a rate-limited (frozen)
    last_good is a valid lower bound until its window resets — but only up to a
    client-side ceiling, so a far-future or malformed ``resets_at`` can never
    grant unbounded trust. The bound is:

        now < min(earliest future relevant-window reset, age-ceiling)

    - **earliest** relevant-window reset (not latest): once the *soonest*
      window rolls over, usage there is zeroed and the whole snapshot is
      obsolete — a later window's reset can't rescue it. So if the earliest
      known reset is already in the past, the value is untrusted outright,
      regardless of any farther-future window.
    - **age-ceiling** (``RATE_LIMIT_TRUST_MAX_AGE_S`` past ``last_good``): the
      hard client-side cap. It applies whether or not any reset is known, so
      even an all-``resets_at`` response — including a far-future or malformed
      one — is bounded, matching the ~1h stale fallback Claude Code itself uses.
      Rows with no reset info at all fall back to it alone.

    A window carrying no ``resets_at`` simply contributes no timestamp; it never
    extends trust, so partial metadata can only tighten the bound, never loosen
    it. ``models`` selects the per-model scoped windows that also gate the
    account, so their resets are considered too (matching the scheduler's view).
    """
    if age_s is None:
        return False
    ceiling = now + (RATE_LIMIT_TRUST_MAX_AGE_S - age_s)
    soonest = _earliest_reset(last_good, models)
    # The soonest window to roll over invalidates the snapshot; never trust past
    # it, and never past the client-side ceiling.
    return now < (min(soonest, ceiling) if soonest is not None else ceiling)


def _failure_backoff_s(
    consecutive_failures: int,
    retry_after_s: float | None,
    *,
    rate_limited: bool = True,
) -> float:
    """Seconds to stay in backoff after a failed fetch."""
    computed = min(
        BACKOFF_BASE_S * (2 ** min(max(0, consecutive_failures - 1), BACKOFF_MAX_SHIFT)),
        BACKOFF_CAP_S,
    )
    if retry_after_s is None:
        return computed
    if retry_after_s == 0:
        if not rate_limited:
            # `Retry-After: 0` on a non-429 (e.g. a Cloudflare 503 saying
            # "retry now") is not the saturated-budget edge — that rule was
            # measured on the usage endpoint's 429s only (poll_policy
            # docstring; see the ceiling discussion above). Fall through to
            # the plain exponential curve, same as no header at all.
            return computed
        # Saturated-budget edge: wait before probing again.
        return min(max(computed, EDGE_BACKOFF_S), BACKOFF_CAP_S)
    # Burst rule: the server's ask plus a margin (bounded by the cap); our own
    # curve may still wait longer. Only above BACKOFF_CAP_S, because a short
    # ask was separately measured as ACCURATE — not because the curve
    # out-waits it. It does not: at `failures=1` computed is 30s, so a 300s ask
    # lands exactly on the server's deadline, and the claim only becomes true
    # at `failures=6` where computed reaches the cap. Strict: an ask OF exactly
    # BACKOFF_CAP_S is what the saturated curve already waits.
    #
    # RESIDUE: an hour-scale block whose REMAINDER falls under BACKOFF_CAP_S
    # takes no margin and still retries near its deadline. Nothing local
    # separates that ask from a genuine short burst block, so it is left open.
    asked = retry_after_s
    # `rate_limited` gates ONLY the margin below — whether evidence measured
    # on usage-endpoint 429 blocks applies here. It does NOT gate the PARK
    # BOUND a few lines down, which applies to every ask unconditionally.
    # Before this bound was restored, the cap lived inside this same `if`
    # (`min(ask + margin, CAP)`), so `rate_limited` silently answered two
    # unrelated questions — "does the measured margin evidence apply" and
    # "may this ask park a row for a day" — through one flag. Separating them
    # is why the bound is now its own unconditional statement below.
    if retry_after_s > BACKOFF_CAP_S and rate_limited:
        # THE MARGIN IS 429-ONLY, and the ceiling is why. It was measured on
        # usage-endpoint blocks, whose stale data stays decision-trusted until
        # `min(earliest relevant-window reset, fetched_at +
        # RATE_LIMIT_TRUST_MAX_AGE_S)` — not the age-ceiling alone. A window
        # that resets before the ceiling ends trust first, so the 4500s wait
        # does NOT always sit inside its own trust: an earlier reset can leave
        # the row un-pollable (still in backoff) and unknown (trust expired)
        # for the remainder of the wait — see
        # `test_a_soon_resetting_window_can_end_trust_before_the_429_wait_releases`.
        #
        # AND THE EARLY RESET IS ONLY ONE WAY IN. The age-ceiling half opens
        # the same gap with no early reset at all, because `record()` writes
        # `fetchedAt` on SUCCESS only: a chain of failed blocks keeps measuring
        # trust from the first success while each block adds another full wait.
        # Measured with far-future resets only, so only the ceiling can bind:
        #
        #     block 1  wait [    0,  4500]  trust ends 7200  blind      0s
        #     block 2  wait [ 4500,  9000]  trust ends 7200  blind   1800s
        #     block 3  wait [ 9000, 13500]  trust ends 7200  blind   4500s
        #
        # So the gap is bounded PER BLOCK and not across a chain — by block 3
        # the row is blind for the whole wait. That is the tradeoff this margin
        # makes in exchange for fewer requests, and it is worth stating at its
        # true size rather than as a single-block figure.
        #
        # WHAT ACTUALLY BOUNDS IT is the identity `cap == 3600 + MARGIN` in
        # `test_the_cap_sits_inside_the_trust_it_relies_on`, NOT the
        # `cap <= RATE_LIMIT_TRUST_MAX_AGE_S` inequality that test also states.
        # The inequality admits [4500, 7200], and at 7200 an ask of 6300s or
        # more pushes the wait itself to 7200, taking the same three blocks
        # from 6300s to 14400s blind (2.3x). Reason about this from blind
        # time; the constant comparison does not imply it. Pinned by
        # `test_consecutive_blocks_go_blind_because_fetchedAt_only_moves_on_success`.
        #
        # Every other failure falls back to TRUST_MAX_AGE_S = 3600s — and
        # `_classify_usage_error` parses Retry-After for ANY HTTPError code,
        # not just 429. A 503 carrying `Retry-After: 3600` therefore took the
        # margin and waited 4500s while its last_good went untrusted at 3600s:
        # 900s in which the row can neither be re-polled (still in backoff) nor
        # used (unknown), and the unhealthy-tick counter reads that blindness
        # as a failing account and fails over. Measured: blind window 179s ->
        # 1079s, and (at a 120s tick interval, 3 unhealthy ticks) a failover
        # at +3781s that main never performs.
        #
        # Capping the constant instead would land a 3600s ask exactly on its
        # deadline — the defect this whole change exists to fix.
        #
        # No cap here: `min(x, RETRY_AFTER_FLOOR_CAP_S)` would be redundant
        # with the PARK BOUND below, which still applies
        # `RETRY_AFTER_FLOOR_CAP_S` unconditionally on this (`rate_limited`)
        # arm — `min(min(x, c), c) == min(x, c)`. Re-verified after B1 (the
        # PARK BOUND split by arm): still dead, because this branch is only
        # reached when `rate_limited` is True, and that is exactly the arm
        # the PARK BOUND still caps at `RETRY_AFTER_FLOOR_CAP_S`. Confirmed
        # by mutation: adding it back (`min(x, RETRY_AFTER_FLOOR_CAP_S)`
        # around the line below) survives the full suite.
        asked = retry_after_s + RETRY_AFTER_MARGIN_S
    # PARK BOUND — every ask is capped, but by the ceiling ITS OWN arm's trust
    # actually uses, not a single shared constant. This restores nothing about
    # trust: `entries()` still reads the row unknown once the relevant ceiling
    # elapses past the last SUCCESS, exactly as before this line, so a capped
    # ask can still be released un-pollable and unknown together. What this
    # bounds is the PARK itself — how long the row can be held un-pollable —
    # not whether it is trusted at the end of it.
    #
    # Without it BOTH arms are at risk of outliving their own trust:
    # `_classify_usage_error` (oauth.py) parses Retry-After for ANY HTTPError
    # code, not just 429, and the usage endpoint sits behind Cloudflare, which
    # emits Retry-After on 503s as routine overload signaling. Measured: a
    # `503 Retry-After: 86400` parks the row 24h, and `Retry-After: inf`
    # (Python parses "inf", "Infinity", and any overflow literal like "1e400"
    # to `float('inf')`) wedges the row permanently — `json.dumps` writes the
    # non-standard `Infinity` literal, which survives a restart.
    #
    # A PRIOR REVISION reused RETRY_AFTER_FLOOR_CAP_S (4500) for both arms,
    # reasoning "one answer to how long any ask can park a row". That is
    # wrong for the non-429 arm: `entries()` reads a non-429 row unknown once
    # TRUST_MAX_AGE_S (3600) elapses past the last success, not
    # RETRY_AFTER_FLOOR_CAP_S — so a non-429 ask above 3600 parked the row
    # 900s past its own trust: blind (un-pollable AND unknown) for that whole
    # margin. See `test_each_arm_is_bounded_by_the_ceiling_its_own_trust_uses`.
    # The 429 arm keeps RETRY_AFTER_FLOOR_CAP_S (4500), correctly inside its
    # own ceiling RATE_LIMIT_TRUST_MAX_AGE_S (7200).
    #
    # CORRECTED 2026-08-03 — the line above does NOT close the blind window
    # in general, on either arm, and an earlier version of this comment
    # wrongly claimed it did ("a regression this PR introduced against
    # upstream/main ... the blind window was always 0"). That is false,
    # measured. The bound below compares the wrong pair of quantities:
    # `asked` is a DURATION measured from the FAILURE (now), but the ceiling
    # it is capped against (`RETRY_AFTER_FLOOR_CAP_S` / `TRUST_MAX_AGE_S`) is
    # an AGE measured from the last SUCCESS (`fetchedAt`), which `entries()`
    # actually uses. Those agree only when the row was already fresh (age 0)
    # at the moment it failed. On the arm measured directly below (non-429,
    # whose park cap equals the trust ceiling it is checked against) the gap
    # reduces to the row's AGE AT FAILURE, up to the whole park — but that is
    # an ARM-SPECIFIC coincidence, not the general rule: see the general
    # closed form and the 429-arm numbers further down, where cap and
    # ceiling are different constants and the identity does not hold.
    # Measured end-to-end through `store.record()` / `entries().decision_
    # value()`, with a control (row already fresh at failure -> no gap):
    #
    #  age@fail     ask    park   blind  starts  verdict
    #         0    5000    3600       0    None  ok      <- CONTROL
    #         1    5000    3600       0    None  ok
    #       120    5000    3600     119  3481.0  blind
    #       300    5000    3600     299  3301.0  blind
    #      1800    5000    3600    1799  1801.0  blind
    #      3599    5000    3600    3598     2.0  blind
    #       300    4000    3600     299  3301.0  blind
    #       300    3600    3600     299  3301.0  blind
    #       300     600     600       0    None  ok
    #
    # Blind = the row is in backoff (un-pollable) AND `decision_value()` is
    # None (unknown) at the same instant. Every row above is the non-429
    # (`rate_limited=False`) arm, where the park cap equals TRUST_MAX_AGE_S
    # (3600) exactly — the ask=4000/3600 rows show the SAME park (3600) as
    # ask=5000 once the ask exceeds the ceiling, and ask=600 shows a park
    # shorter than the ceiling never goes blind at all (its own curve is the
    # binding constraint, not the ceiling). On THIS ARM, and only this arm,
    # "the blind window equals the age at failure" holds — because the cap
    # (TRUST_MAX_AGE_S) and the trust ceiling `entries()` actually reads
    # against (also TRUST_MAX_AGE_S) are the SAME constant, so they cancel.
    # General form, both arms: `blind = age_at_fail - (ceiling - park)`,
    # where `ceiling` is RATE_LIMIT_TRUST_MAX_AGE_S (7200) on the 429 arm and
    # TRUST_MAX_AGE_S (3600) on the non-429 arm above. It equals the age at
    # failure only where `ceiling == park`, which is true on the non-429 arm
    # (3600 == 3600) and false on the 429 arm (7200 != 4500).
    #
    # CORRECTED 2026-08-03 (round 8) — the paragraph that used to sit here
    # said this is "PRE-EXISTING, not a regression this PR introduced",
    # backed by a byte-identical table against a real upstream/main
    # worktree. Both halves are true and together prove nothing about this
    # PR: the table above is entirely the non-429 arm, whose cap
    # (TRUST_MAX_AGE_S = 3600) this PR does not touch — of course it is
    # byte-identical to upstream, whose non-429 cap is also 3600. The arm
    # this PR DOES change is the 429 arm (RETRY_AFTER_FLOOR_CAP_S: upstream
    # 3600 -> this PR's 4500), and there the blind window regresses. Same
    # probe, `error="http-429"`, ask=3600 (so the margin-adjusted ask always
    # exceeds the cap on both trees):
    #
    #  age@fail   PR park  PR blind   upstream park  upstream blind
    #         0      4500         0            3600               0
    #      2701      4500         1            3600               0   <- +1s
    #      3600      4500       900            3600               0   <- +900s
    #      5000      4500      2300            3600            1400   <- +900s
    #
    # Onset moves from age 3600 (upstream) to age 2700 (this PR) — 900s
    # earlier — and every age past onset is a flat +900s worse than
    # upstream. This is reachable with no contrived staleness: after one 429
    # block the row's age at the next failure IS the park length, so a real
    # chain of blocks compounds it (see `test_park_bound_blind_window_
    # equals_age_at_failure`'s 429-arm cases and the DECISION note below).
    # `autoswitch.py` reads a blind row as `active_headroom is None` and
    # turns it into failover pressure — the exact downstream consequence
    # round 6 was written to remove, and on the 429 arm this PR makes it
    # worse than upstream, not merely pre-existing.
    #
    # DECISION: left open in this PR, not closed, WITH the true 429 numbers
    # above (not the ones an earlier round of this comment cited).
    #
    # CORRECTED 2026-08-03 (round 10) — this block used to defer the fix on
    # the claim that it "needs `_failure_backoff_s` to know the row's age at
    # the moment it failed — a real signature change" that "ripples well
    # past this bound". That is false, measured: `record()` already computes
    # `row["backoffUntil"] = now + _failure_backoff_s(...)` at the one call
    # site (usage_store.py, in `record()`'s failure branch) and already
    # holds `fetchedAt` in the same row. Clamping the PRODUCT there —
    # `min(now + _failure_backoff_s(...), fetchedAt + ceiling)` — needs no
    # change to this function's signature and touches none of the tests that
    # call it directly:
    #
    #   as shipped                                       :  park 4500s  blind  900s
    #   call-site clamp min(now+wait, fetchedAt+ceiling)  :  park 3600s  blind    0s
    #   _failure_backoff_s signature UNCHANGED             :  4500s still returned
    #
    # The real reason to keep this open is the one below, not the signature
    # claim: that call-site clamp is the SAME trim NO TRUST TRIM AGAINST THE
    # SERVER'S DEADLINE (a few lines down) spent a full section arguing
    # against removing — it bounds `backoffUntil` against
    # `fetchedAt + ceiling`, exactly the quantity that section calls a no-op-
    # or-futile clip. Measured driving one 3600s block at three row ages:
    #
    #   age@open=0     AS SHIPPED 1 request, lands deadline+900s
    #                  CLAMPED    1 request, lands deadline+900s   <- no-op, matches the trim's own finding
    #   age@open=3600  AS SHIPPED 1 request, lands deadline+900s
    #                  CLAMPED    1 request, lands deadline+0s     <- lands ON the deadline: the defect this PR fixes
    #   age@open=7000  AS SHIPPED 1 request, lands deadline+900s
    #                  CLAMPED    many requests, does not cleanly converge  <- the request storm the trim section measured
    #
    # So the clamp is not a smaller, safer version of the removed trim — past
    # the ceiling it reproduces the same request-storm failure mode that trim
    # was measured to cause, with the same ceiling. A follow-up landing this
    # needs the same episode measurement the trim section did before it can
    # ship, not merely a signature check. Tracked as a follow-up, not fixed
    # here.
    asked = min(asked, RETRY_AFTER_FLOOR_CAP_S if rate_limited else TRUST_MAX_AGE_S)
    # NO TRUST TRIM AGAINST THE SERVER'S DEADLINE. Cutting a 429 wait back to
    # the deadline when the stored trust expires first cannot salvage that
    # trust — `trust < ask` is its own precondition and the floor keeps
    # `wait >= ask`, so the row is untrusted at release either way (measured:
    # fired 35 of 180 offsets, salvaged 0). It only lands us on the deadline,
    # where 10 of 19 lapses re-block for a fresh hour (as measured when this
    # was derived — the re-block fraction has since moved to 20 of 35
    # (re-measured 2026-08-03, method corrected round 8: see
    # RETRY_AFTER_MARGIN_S); the episode model below is not re-derived at the
    # new fraction, since it concerns the removed 429 trust-trim and is out
    # of the live path).
    # Episode model on the original number, 3600 runs:
    #
    #     with the trim    blind 1148s   requests 1.21
    #     without it       blind  550s   requests 1.00
    #
    # NOR AGAINST OUR OWN. A previous round clipped the wait to the row's
    # remaining trust, reading `min(earliest reset, fetchedAt + ceiling)` as a
    # second, independent deadline. It is not one, for two reasons.
    #
    # The clip is a no-op or it is futile, with no third case. Where
    # `trust_left >= wait` it changes nothing; where `trust_left < wait` it
    # returns `max(trust_left, computed) >= trust_left`, so the row is unknown
    # at release by construction — "still trusted at release" would need
    # `release < trust_left`, which that same expression forbids. Enumerated
    # over every positive ask and every reachable `trust_left`: 12,188,000
    # evaluations, 5,616,144 shortened a wait, 0 released with the row trusted.
    #
    # And the wait cannot move the deadline it was clipping against.
    # `entries()` decides trust from `lastGood`/`fetchedAt`, which `record()`
    # writes in the SUCCESS branch only — a 429 refreshes neither. So the
    # instant the row goes unknown is fixed by the last successful fetch, and a
    # shorter wait only samples that same instant more often, one request each.
    # Un-pollable and unknown are independent axes; the previous round treated
    # them as one window and tried to close the second by shortening the first.
    #
    # What the clip cost, measured over reset offsets 1..7200s against a 3600s
    # block (requests spent inside the block, and seconds unknown before a poll
    # could next succeed):
    #
    #     clipped     3.95 requests (worst 10)   73525s
    #     unclipped   1.00 requests (worst  1)    1406s
    #
    # Worse on both axes, because the usage endpoint's 429 is a REQUEST-RATE
    # block, not a quota-window one: `poll_policy` measured ~28-30 requests
    # per trailing hour per budget identity, where "capacity returns only as
    # old requests age out". A 5h/7d/scoped reset is a different axis and cannot lift it,
    # while every extra retry is itself a request in that trailing hour — so
    # the retries push out the very horizon they are retrying against.
    return max(asked, computed)


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

    def entries(
        self,
        identities: dict[str, Identity],
        models: tuple[str, ...] = (),
    ) -> dict[str, UsageEntry]:
        """Identity-guarded snapshot for the given slots (empty entry when the
        row is missing or belongs to a different account).

        ``models`` are the configured scoped-window model names; they let the
        429-stale trust bound also honor per-model (e.g. Fable) window resets,
        matching the scheduler's window view. Omitted (``()``) for callers that
        only read timestamps/last-good and never consult scoped resets."""
        now = self.clock()
        rows = self._read_rows()
        out: dict[str, UsageEntry] = {}
        for num, identity in identities.items():
            row = rows.get(num)
            if not self._matches(row, identity):
                out[num] = UsageEntry()
                continue
            assert isinstance(row, dict)
            fetched_at = row.get("fetchedAt")
            if not isinstance(fetched_at, (int, float)):
                fetched_at = None
            last_good = row.get("lastGood")
            age_s = (now - fetched_at) if fetched_at is not None else None
            consecutive_failures = int(row.get("consecutiveFailures") or 0)
            next_poll_at = _num_or_none(row.get("nextPollAt"))
            last_attempt_at = _num_or_none(row.get("lastAttemptAt"))
            claim_until = _num_or_none(row.get("claimUntil"))
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
                    last_good if isinstance(last_good, dict) else None,
                    age_s,
                    now,
                    models,
                )
            else:
                within_ceiling = age_s is not None and age_s <= TRUST_MAX_AGE_S
            live_claim = _live_claim(claim_until, last_attempt_at, now)
            trust_extended = (
                within_ceiling
                and (
                    consecutive_failures > 0
                    or (next_poll_at is not None and now < next_poll_at)
                    or live_claim
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
                struck_fingerprint=row.get("struckFingerprint"),
                trust_extended=trust_extended,
                claim_until=claim_until,
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

    def claim(
        self, nums: Iterable[str], identities: dict[str, Identity]
    ) -> dict[str, str]:
        """Lease the slots about to be fetched, returning their fencing ids."""
        nums = list(nums)
        if not nums:
            return {}
        now = self.clock()
        claims = {num: uuid.uuid4().hex for num in nums}
        self._mutate(
            identities,
            nums,
            lambda num, row: row.update(
                lastAttemptAt=now,
                claimId=claims[num],
                claimUntil=now + CLAIM_TTL_S,
            ),
        )
        return claims

    def reserve(
        self,
        nums: Iterable[str],
        identities: dict[str, Identity],
        *,
        respect_plans: bool,
        repair_overslept: bool = False,
    ) -> dict[str, str]:
        """Atomically win the right to fetch: re-check eligibility and stamp
        a bounded lease in one locked pass, returning slot → fencing id.

        Deciding eligibility on a lock-free :meth:`entries` read and then
        claiming separately lets two collectors both pass the check and both
        fetch; the re-check under the lock closes that window. Eligibility:
        not quarantined (dead token), not in failure backoff, not claimed
        within ``CLAIM_TTL_S``, and then by caller mode —

        - ``respect_plans=True`` (on-demand callers: list/status/switch,
          dashboards): the entry must be stale (older than ``SERVE_TTL_S``)
          *and* poll-due (past ``nextPollAt``, or no plan yet). When
          ``repair_overslept`` is set, a structurally obsolete reset-parked
          plan is also due; that predicate is re-checked under this lock.
        - ``respect_plans=False`` (the auto engine's deliberate schedule):
          poll-due *or* stale — a due entry may be re-fetched inside the
          serve TTL (that is how the bounded urgent cadence beats the TTL),
          and an escalation refresh may fetch a not-yet-due candidate. With
          ``repair_overslept``, this becomes the non-escalating scheduler mode:
          due plans and stale impossible plans win, but valid future plans do
          not.
        """
        nums = list(nums)
        if not nums:
            return {}
        now = self.clock()
        won: dict[str, str] = {}
        with self._lock():
            rows = self._read_rows()
            for num in nums:
                identity = identities[num]
                row = rows.get(num)
                if not self._matches(row, identity):
                    rows[num] = row = self._fresh_row(identity)
                else:
                    assert isinstance(row, dict)
                    if not _row_eligible(
                        row, now, respect_plans, repair_overslept
                    ):
                        continue
                claim_id = uuid.uuid4().hex
                row["lastAttemptAt"] = now
                row["claimId"] = claim_id
                row["claimUntil"] = now + CLAIM_TTL_S
                won[num] = claim_id
            if won:
                self._write_rows(rows)
        return won

    def record(
        self,
        outcomes: dict[str, FetchRecord],
        identities: dict[str, Identity],
        claims: dict[str, str] | None = None,
        plans: dict[str, tuple[float | None, float | None]] | None = None,
    ) -> set[str]:
        """Merge outcomes fenced by the leases that produced them.

        A late writer whose lease or slot identity was replaced is ignored
        without touching the newer row. Success and failure are mutually
        exclusive writers: success resets the failure fields, failure never
        touches ``lastGood``/``fetchedAt``. A supplied success plan commits
        in the same transaction as its measurement. Sentinel records clear
        only the claim and are otherwise never persisted. Unfenced callers
        (no ``claims``) defer to a live lease but never to an expired one.
        Returns the accepted slots.
        """
        if not outcomes:
            return set()
        now = self.clock()
        accepted: set[str] = set()

        def apply(num: str, row: dict) -> None:
            accepted.add(num)
            rec = outcomes[num]
            row["claimId"] = None
            row["claimUntil"] = 0.0
            if rec.sentinel is not None:
                return
            row["lastAttemptAt"] = now
            if rec.error is None:
                row["lastGood"] = rec.usage
                row["fetchedAt"] = now
                # Replace the old, possibly due plan in the outcome transaction
                # so no collector can slip into a record→replan gap.
                plan = plans.get(num) if plans is not None else None
                if plan is not None:
                    row["nextPollAt"], row["pollIntervalS"] = plan
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
                    failures,
                    rec.retry_after_s,
                    rate_limited=rec.error == "http-429",
                )
                # Only a permanent-auth failure advances the dead-token count; a
                # transient error (429/timeout) leaves it as-is — it is no
                # evidence either way and must not reset a real dead-token tally.
                if rec.error in PERMANENT_AUTH_ERRORS:
                    row["authDeadStrikes"] = int(row.get("authDeadStrikes") or 0) + 1
                    # Additive field (absent/None = legacy unconditional
                    # binding). Always overwrite: a legacy writer's strike
                    # must bind unconditionally, not inherit a stale
                    # fingerprint from an earlier, already-healed strike.
                    row["struckFingerprint"] = rec.struck_fp

        with self._lock():
            rows = self._read_rows()
            for num in outcomes:
                identity = identities[num]
                row = rows.get(num)
                expected: str | None = None
                if claims is not None:
                    expected = claims.get(num)
                    if (
                        expected is None
                        or not self._matches(row, identity)
                        or not isinstance(row, dict)
                        or row.get("claimId") != expected
                    ):
                        continue
                elif (
                    isinstance(row, dict)
                    and row.get("claimId") is not None
                    # Only a live lease defers an unfenced writer; a crashed
                    # claimer's leftover ticket must age out, not block forever.
                    and now < (_num_or_none(row.get("claimUntil")) or 0.0)
                ):
                    continue
                elif not self._matches(row, identity):
                    rows[num] = row = self._fresh_row(identity)
                assert isinstance(row, dict)
                apply(num, row)
            if accepted:
                self._write_rows(rows)
        return accepted

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
            row["claimId"] = None
            row["claimUntil"] = 0.0
            row["authDeadStrikes"] = 0
            row["struckFingerprint"] = None
            row["consecutiveFailures"] = 0
            row["lastError"] = None
            row["backoffUntil"] = None

        self._mutate(identities, nums, apply)


def _num_or_none(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _row_eligible(
    row: dict, now: float, respect_plans: bool, repair_overslept: bool = False
) -> bool:
    """Fetch eligibility of a stored row, evaluated under the write lock
    (see :meth:`UsageStore.reserve` for the two caller modes)."""
    if int(row.get("authDeadStrikes") or 0) >= AUTH_DEAD_STRIKES:
        return False
    backoff_until = _num_or_none(row.get("backoffUntil"))
    if backoff_until is not None and now < backoff_until:
        return False
    if _live_claim(
        _num_or_none(row.get("claimUntil")),
        _num_or_none(row.get("lastAttemptAt")),
        now,
    ):
        return False
    fetched_at = _num_or_none(row.get("fetchedAt"))
    stale = fetched_at is None or (now - fetched_at) > SERVE_TTL_S
    next_poll_at = _num_or_none(row.get("nextPollAt"))
    poll_due = next_poll_at is not None and now >= next_poll_at
    overslept = repair_overslept and _plan_oversleeps_interval(
        next_poll_at,
        _num_or_none(row.get("pollIntervalS")),
        now,
    )
    if respect_plans:
        return stale and (poll_due or next_poll_at is None or overslept)
    if repair_overslept:
        return poll_due or (stale and (next_poll_at is None or overslept))
    return poll_due or stale


def with_sentinel(entry: UsageEntry, sentinel: str | None) -> UsageEntry:
    """Overlay a derived sentinel state on a stored entry (read model only)."""
    if sentinel is None:
        return entry
    return replace(entry, sentinel=sentinel)
