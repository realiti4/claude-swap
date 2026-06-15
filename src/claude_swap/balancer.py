"""Pure decision logic for the event-driven account load balancer (Beta).

This module is deliberately **I/O-free and dependency-free** (no imports from
:mod:`claude_swap.switcher`, :mod:`~.session`, :mod:`~.oauth`, the network, or
the filesystem). It takes immutable snapshots of the world — the managed
accounts and the live managed sessions — and returns a :class:`Plan` of
:class:`Action` s. The caller (the statusline planner and the per-session
supervisor) is responsible for all I/O: building the snapshot, persisting the
plan's intents to the registry, and re-pointing credentials.

Keeping the decision logic pure makes it exhaustively unit-testable with
synthetic snapshots (see ``tests/test_balancer.py``) and keeps the hard part —
*minimising churn while honouring priority* — separated from the credential
plumbing.

The model in one paragraph: new sessions are placed on the **highest-priority**
account that has headroom, so load naturally *concentrates* on high-priority
accounts and only spills to the next tier when one fills up (you "burn through"
high-priority accounts first). Among accounts of the *same* priority, placement
prefers the **least-used** (most projected headroom) so equal-priority accounts
stay evenly used over time. A session is **migrated only when its current
account is exhausted** — never for a marginal gain — and three independent,
*algorithmic* brakes (no timers) stop a session from bouncing between accounts:
a rising-edge trigger applied by the caller (act only on the not-exhausted ->
exhausted transition), the ``target_safety`` ceiling (a target's projected usage
after placement must leave headroom, so a move never immediately re-exhausts it),
and a hysteresis band (an exhausted account isn't a valid target until it
recovers below ``threshold - band``). An online headroom reservation keeps two
concurrently-stranded sessions from co-exhausting the same target. When nothing
has headroom, sessions are **paused** until the soonest rate-limit window
resets, and resumed automatically. There is deliberately **no fixed time
cooldown** — a genuinely-needed switch (a session stranded on a freshly-exhausted
account) is never blocked by a timer.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --------------------------------------------------------------------------- #
# Tunable constants (defaults; overridable per-install via sequence.json's
# ``autoBalance`` block, see :func:`config_from_dict`).
# --------------------------------------------------------------------------- #

# An account is "exhausted" once max(5h, 7d) utilization reaches this percent.
# Shares the default with the legacy auto-switch threshold.
DEFAULT_EXHAUST_THRESHOLD = 95
# A migration target's *projected* utilization (after placing the session) must
# stay at or below this. The gap to the exhaust threshold absorbs the per-session
# headroom reserve so a move never immediately re-exhausts the target.
DEFAULT_TARGET_SAFETY = 90
# An exhausted account only re-enters the usable pool once it drops this many
# points below the exhaust threshold — absorbs the chunky, API-call-only jitter
# of the statusline's ``used_percentage`` so accounts don't flap at the boundary.
DEFAULT_HYSTERESIS_BAND = 3
# Pause horizon when no reset time is known anywhere (defensive fallback).
DEFAULT_PAUSE_FALLBACK_S = 3600

# A 5h window is treated as UNSTARTED (a priming candidate) when its utilization
# is at or below this many points. The Claude subscription 5h window is anchored
# to the first credit-consuming call of a cycle: an account that has sent nothing
# this window reports ~0% with no concrete reset timestamp. The small epsilon
# absorbs the chunky, API-rounded ``utilization`` so a genuinely-idle window isn't
# missed (and a started one — pct above this, or a real future reset — is left
# alone so we never burn a credit re-priming an already-running clock).
PRIME_FIVE_HOUR_EPSILON = 0.5

# Per-session headroom reserve, in utilization points, debited from a target
# when a session is (re)placed within a single balancing pass. A fixed base plus
# a context-proportional nudge (bigger conversations cost more to migrate, so we
# leave them more room). Bounded to BASE..BASE+CTX_MAX.
BASE_RESERVE = 3.0
CTX_RESERVE_MAX = 7.0
CTX_RESERVE_DIVISOR = 50_000  # context tokens per extra reserve point

# How recently a session must have been seen to count as "active" (used only to
# order migrations: least-active sessions move first).
_ACTIVE_RECENT_S = 120


@dataclass(frozen=True)
class BalancerConfig:
    """Resolved balancer tunables for one decision pass."""

    exhaust_threshold: int = DEFAULT_EXHAUST_THRESHOLD
    target_safety: int = DEFAULT_TARGET_SAFETY
    hysteresis_band: int = DEFAULT_HYSTERESIS_BAND
    pause_fallback_s: int = DEFAULT_PAUSE_FALLBACK_S


def config_from_dict(auto_balance: dict | None) -> BalancerConfig:
    """Build a :class:`BalancerConfig` from a persisted ``autoBalance`` dict.

    Missing or malformed fields fall back to the module defaults; the result is
    clamped so the load-bearing invariant
    ``exhaust_threshold - target_safety >= BASE_RESERVE`` always holds (otherwise
    a freshly-migrated session could be judged exhausted on its next tick).
    """
    cfg = auto_balance or {}

    def _int(key: str, default: int) -> int:
        try:
            return int(cfg.get(key, default))
        except (TypeError, ValueError):
            return default

    exhaust = max(1, min(100, _int("threshold", DEFAULT_EXHAUST_THRESHOLD)))
    safety = max(1, min(100, _int("targetSafety", DEFAULT_TARGET_SAFETY)))
    # Keep at least BASE_RESERVE of margin between safety and exhaust.
    safety = min(safety, exhaust - int(BASE_RESERVE))
    if safety < 1:
        safety = 1
    band = max(0, _int("hysteresisBand", DEFAULT_HYSTERESIS_BAND))
    return BalancerConfig(
        exhaust_threshold=exhaust,
        target_safety=safety,
        hysteresis_band=band,
        pause_fallback_s=DEFAULT_PAUSE_FALLBACK_S,
    )


# --------------------------------------------------------------------------- #
# Immutable world snapshot
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AccountView:
    """A managed account's balancing-relevant state at one instant."""

    num: str
    priority: int = 0
    # max(5h, 7d) utilization percent; ``None`` when usage is unknown (no signal)
    # — an account with unknown usage has zero headroom and is never a target.
    max_pct: float | None = None
    # Epoch seconds when the window that is *currently capping* this account
    # resets (the window equal to ``max_pct``); ``None`` if unknown.
    soonest_reset: int | None = None
    signal: str = "none"  # "live" (statusline) | "cache" (usage API) | "none"
    # The 5h-window-specific utilization + reset, used ONLY by the idle-window
    # priming detection (:func:`accounts_needing_prime`). ``five_hour_pct`` is the
    # 5h utilization percent (distinct from ``max_pct``, which is max(5h, 7d));
    # ``five_hour_reset`` is the 5h window's reset epoch when the clock is running
    # (``None`` when the window is unstarted — the signal we prime on). Both are
    # ``None`` when usage is unknown.
    five_hour_pct: float | None = None
    five_hour_reset: int | None = None


@dataclass(frozen=True)
class SessionView:
    """A managed session's balancing-relevant state at one instant."""

    session_id: str
    account_num: str
    ctx_tokens: int = 0
    last_seen: float = 0.0
    paused_until: int | None = None
    # Telemetry only (timestamp of the last migration). NOTHING in the balancer
    # gates on this — anti-thrash is purely algorithmic (rising-edge + target
    # safety + hysteresis + online reservation), never a time cooldown.
    last_migrated_at: float = 0.0
    pinned_account: str | None = None  # user hard-pin; never migrated/paused


@dataclass(frozen=True)
class Action:
    """A single decision for one session."""

    kind: str  # "MIGRATE" | "PAUSE" | "RESUME" | "KEEP"
    session_id: str
    from_account: str | None = None
    to_account: str | None = None
    resume_at: int | None = None  # PAUSE only: epoch seconds


@dataclass(frozen=True)
class Plan:
    """The full set of decisions from one balancing pass (deterministic order)."""

    actions: list[Action] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


def _pct_cost(ctx_tokens: int | None) -> float:
    """Headroom (utilization points) one session is expected to consume.

    A fixed base plus a context-proportional term: a session with a large
    context window costs more to host (and more to migrate), so it is given a
    wider safety reserve. Bounded to ``BASE_RESERVE .. BASE_RESERVE+CTX_RESERVE_MAX``.
    """
    extra = min(CTX_RESERVE_MAX, (ctx_tokens or 0) / CTX_RESERVE_DIVISOR)
    return BASE_RESERVE + extra


def _headroom(av: AccountView) -> float:
    """Remaining utilization headroom; unknown usage counts as zero."""
    return 100.0 - av.max_pct if av.max_pct is not None else 0.0


def _exhausted(av: AccountView | None, cfg: BalancerConfig) -> bool:
    return av is not None and av.max_pct is not None and av.max_pct >= cfg.exhaust_threshold


def _usable(av: AccountView | None, cfg: BalancerConfig) -> bool:
    """Whether an account is below the hysteresis-adjusted re-entry line."""
    return (
        av is not None
        and av.max_pct is not None
        and av.max_pct <= (cfg.exhaust_threshold - cfg.hysteresis_band)
    )


def _projected_headroom(av: AccountView, projected: dict[str, float]) -> float:
    """Headroom remaining after the in-pass online reservation is debited.

    ``100 - (current usage + reserved load)``. Higher = less-used = a better
    target for an equal-priority tiebreak (keeps same-priority accounts evenly
    used over time). Unknown-usage accounts never reach here (``_usable`` filters
    them out), so ``max_pct`` is always a number.
    """
    return 100.0 - (av.max_pct + projected.get(av.num, 0.0))


def _rank_accounts(
    acct_views: dict[str, AccountView],
    projected: dict[str, float],
    cfg: BalancerConfig,
) -> list[AccountView]:
    """Usable accounts, best target first.

    Ordering (a single deterministic sort key):

    1. **highest priority first** — load concentrates on / burns through
       high-priority accounts before spilling to the next tier;
    2. **least-used first within a priority tier** — most projected headroom
       (current usage plus the in-pass online reservation) wins, so accounts of
       *equal* priority are filled evenly rather than always hammering the same
       one (EQUAL-PRIORITY EVEN USAGE). This governs *where* a session lands; the
       only-switch-when-exhausted rule still governs *whether* it switches at all;
    3. **lowest account number** — a deterministic final tiebreak so two
       equal-priority, equal-headroom accounts always order the same way
       regardless of dict iteration order.
    """
    usable = [a for a in acct_views.values() if _usable(a, cfg)]
    return sorted(
        usable,
        key=lambda a: (
            -a.priority,
            -_projected_headroom(a, projected),
            _num_key(a.num),
        ),
    )


def _num_key(num: str) -> tuple[int, int | str]:
    """Sort key that orders numeric account ids numerically, others lexically."""
    return (0, int(num)) if str(num).isdigit() else (1, str(num))


def _fits(av: AccountView, projected: dict[str, float], cost: float, cfg: BalancerConfig) -> bool:
    return (av.max_pct + projected.get(av.num, 0.0) + cost) <= cfg.target_safety


# --------------------------------------------------------------------------- #
# Decisions
# --------------------------------------------------------------------------- #


def assign_new_session(
    acct_views: dict[str, AccountView],
    ctx_tokens: int,
    now: float,
    cfg: BalancerConfig,
    projected: dict[str, float] | None = None,
) -> str | None:
    """Pick the account a brand-new managed session should launch on.

    The highest-priority usable account whose projected utilization stays within
    ``target_safety`` after placing this session. Among accounts of the *same*
    priority the **least-used** (most projected headroom) is chosen, so repeated
    placements spread evenly across equal-priority accounts instead of stacking
    on one (see :func:`_rank_accounts`). Returns the account number, or ``None``
    when nothing has room (the caller then starts the session paused).
    """
    projected = projected if projected is not None else {num: 0.0 for num in acct_views}
    cost = _pct_cost(ctx_tokens)
    for a in _rank_accounts(acct_views, projected, cfg):
        if _fits(a, projected, cost, cfg):
            return a.num
    return None


def choose_migration_target(
    s: SessionView,
    acct_views: dict[str, AccountView],
    projected: dict[str, float],
    cfg: BalancerConfig,
) -> str | None:
    """Best account to migrate ``s`` to, excluding its current account.

    Highest priority first, then the **least-used** account within that priority
    tier (most projected headroom — keeps equal-priority accounts evenly used);
    only an account whose projected utilization stays within ``target_safety``
    qualifies (so a move never immediately re-exhausts the target — the
    move-then-exhaust trap). ``None`` => the caller pauses.
    """
    cost = _pct_cost(s.ctx_tokens)
    for a in _rank_accounts(acct_views, projected, cfg):
        if a.num == s.account_num:
            continue
        if _fits(a, projected, cost, cfg):
            return a.num
    return None


def _five_hour_unstarted(av: AccountView, cfg: BalancerConfig) -> bool:
    """Whether ``av``'s 5h window has not started this cycle (an idle clock).

    The Claude subscription 5h window only begins counting on the first
    credit-consuming call of a cycle. An unstarted window reports ~0% utilization
    with no concrete reset timestamp; a *running* window reports pct above the
    epsilon and/or a real future reset. Defensive against all three observed
    zero-state shapes (5h omitted, pct:0 with no reset, pct:0 with a reset): a
    real future ``five_hour_reset`` always means started, regardless of pct.
    """
    if av.five_hour_pct is None:
        return False  # unknown 5h usage -> not a confirmed-idle candidate
    if av.five_hour_pct > cfg.exhaust_threshold:  # paranoia; a full window is "started"
        return False
    if av.five_hour_reset is not None:
        return False  # a concrete reset timestamp means the clock is already running
    return av.five_hour_pct <= PRIME_FIVE_HOUR_EPSILON


def accounts_needing_prime(
    acct_views: dict[str, AccountView],
    cfg: BalancerConfig,
) -> list[str]:
    """Managed accounts whose 5h window is UNSTARTED and should be primed.

    Pure and I/O-free (table-testable): the caller (the supervisor) performs the
    actual network prime OUTSIDE the registry lock. An account is a candidate
    when ALL hold:

    * its usage signal is ``"cache"`` — a real idle-account usage read. ``"live"``
      means it already hosts an in-use session (its clock is started), and
      ``"none"`` means usage is unknown (skip rather than blind-prime a possibly
      logged-out account);
    * its 5h window is unstarted (:func:`_five_hour_unstarted`); and
    * it is not already exhausted on its *other* (7d) window — priming a fresh 5h
      window can't help an account whose weekly cap is the binding constraint, so
      don't waste a credit on it.

    Returned in deterministic account-number order so concurrent supervisors that
    happen to prime in the same pass agree on ordering.
    """
    candidates = [
        a.num
        for a in acct_views.values()
        if a.signal == "cache"
        and _five_hour_unstarted(a, cfg)
        and not _seven_day_exhausted(a, cfg)
    ]
    return sorted(candidates, key=_num_key)


def _seven_day_exhausted(av: AccountView, cfg: BalancerConfig) -> bool:
    """Whether the account's *non-5h* cap (its 7d window) is already exhausting it.

    ``max_pct`` is max(5h, 7d). When the 5h window is unstarted (~0%), an exhausted
    ``max_pct`` can only come from the 7d window, so ``max_pct >= threshold`` here
    means the weekly cap is binding — priming a fresh 5h window wouldn't help.
    """
    return av.max_pct is not None and av.max_pct >= cfg.exhaust_threshold


def pause_decision(
    s: SessionView,
    acct_views: dict[str, AccountView],
    now: float,
    cfg: BalancerConfig,
) -> Action:
    """Pause ``s`` until the soonest future reset across all accounts.

    Uses the soonest *blocking-window* reset (each account's ``soonest_reset``
    already tracks the window equal to its ``max_pct``), so a 5h reset that
    leaves the 7d window capped does not resume the session prematurely.
    """
    resets = [
        a.soonest_reset
        for a in acct_views.values()
        if a.soonest_reset and a.soonest_reset > now
    ]
    until = min(resets) if resets else int(now + cfg.pause_fallback_s)
    return Action("PAUSE", s.session_id, from_account=s.account_num, resume_at=int(until))


def rebalance(
    acct_views: dict[str, AccountView],
    sess_views: list[SessionView],
    now: float,
    cfg: BalancerConfig,
) -> Plan:
    """The core balancing pass — pure, deterministic, no I/O.

    Phase A (resume): paused sessions whose timer elapsed *or* whose account
    recovered below the hysteresis line.
    Phase B (strand): non-pinned, non-paused sessions whose current account is
    exhausted. There is no time cooldown — a session stranded on a freshly
    exhausted account is always eligible to move; rapid re-migration is prevented
    by the caller's rising-edge gate plus target-safety + hysteresis, not a timer.
    Phase C (order): least-active / cheapest-context first, so a partial set of
    moves stops early and the fewest expensive sessions are disturbed.
    Phase D (place): greedily assign each stranded session a target with an
    online headroom reservation (so two stranded sessions don't both land on the
    same account and co-exhaust it); pause when nothing fits.
    """
    actions: list[Action] = []
    projected: dict[str, float] = {num: 0.0 for num in acct_views}

    # ---- Phase A: RESUME / collect expired pauses ---------------------------
    # A paused session resumes only when its account has actually recovered
    # below the hysteresis line. If the pause timer elapsed but the account is
    # still capped, the session is "expired" and re-placed in Phase D (migrated
    # to a usable account, or re-paused to the real reset) — never resumed into
    # a still-capped account, and never both resumed and paused in one plan.
    expired: list[SessionView] = []
    for s in sorted(
        (x for x in sess_views if x.paused_until is not None),
        key=lambda x: x.session_id,
    ):
        cur = acct_views.get(s.account_num)
        if _usable(cur, cfg):
            actions.append(Action("RESUME", s.session_id, to_account=s.account_num))
        elif now >= s.paused_until:
            expired.append(s)
        # else: still within the pause window and still capped -> leave paused.

    # ---- Phase B: identify STRANDED ----------------------------------------
    def is_stranded(s: SessionView) -> bool:
        if s.pinned_account is not None:
            return False
        if s.paused_until is not None:  # paused sessions handled in Phase A
            return False
        # No time cooldown: a session whose account is exhausted is always
        # eligible to move. Anti-thrash is algorithmic (the caller's rising-edge
        # gate + target_safety + hysteresis + the online reservation), so a
        # genuinely-needed switch is never blocked by a timer.
        return _exhausted(acct_views.get(s.account_num), cfg)

    stranded = [s for s in sess_views if is_stranded(s)]
    stranded += [s for s in expired if s.pinned_account is None]

    # ---- Phase C: order moves (least-active, cheapest first) ----------------
    stranded.sort(
        key=lambda s: (
            (now - s.last_seen) < _ACTIVE_RECENT_S,  # active-recent sorts last
            s.ctx_tokens,
            s.session_id,
        )
    )

    # ---- Phase D: place each, reserving headroom online ---------------------
    for s in stranded:
        tgt = choose_migration_target(s, acct_views, projected, cfg)
        if tgt is None:
            actions.append(pause_decision(s, acct_views, now, cfg))
        elif tgt == s.account_num:  # defensive; choose_migration_target excludes current
            actions.append(Action("KEEP", s.session_id, s.account_num, s.account_num))
        else:
            actions.append(Action("MIGRATE", s.session_id, s.account_num, tgt))
            projected[tgt] = projected.get(tgt, 0.0) + _pct_cost(s.ctx_tokens)

    return Plan(actions)
