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
high-priority accounts first). A session is **migrated only when its current
account is exhausted** — never for a marginal gain — and three independent
brakes (a rising-edge trigger applied by the caller, a per-session cooldown, and
a hysteresis band) stop a session from bouncing between accounts. When nothing
has headroom, sessions are **paused** until the soonest rate-limit window
resets, and resumed automatically.
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
# Minimum seconds between migrations of the same session — the hard rate limit
# that prevents A->B->A thrashing (each migration re-bills the context window).
DEFAULT_MIGRATION_COOLDOWN = 600
# Pause horizon when no reset time is known anywhere (defensive fallback).
DEFAULT_PAUSE_FALLBACK_S = 3600

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
    migration_cooldown: int = DEFAULT_MIGRATION_COOLDOWN
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
    cooldown = max(0, _int("cooldownSeconds", DEFAULT_MIGRATION_COOLDOWN))
    return BalancerConfig(
        exhaust_threshold=exhaust,
        target_safety=safety,
        hysteresis_band=band,
        migration_cooldown=cooldown,
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


@dataclass(frozen=True)
class SessionView:
    """A managed session's balancing-relevant state at one instant."""

    session_id: str
    account_num: str
    ctx_tokens: int = 0
    last_seen: float = 0.0
    paused_until: int | None = None
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


def _in_cooldown(s: SessionView, now: float, cfg: BalancerConfig) -> bool:
    return (now - s.last_migrated_at) < cfg.migration_cooldown


def _rank_accounts(
    acct_views: dict[str, AccountView],
    projected: dict[str, float],
    cfg: BalancerConfig,
) -> list[AccountView]:
    """Usable accounts, best target first.

    Ordered by: highest priority (burn high-priority first), then most
    projected headroom within a priority tier, then lowest account number for
    determinism.
    """
    usable = [a for a in acct_views.values() if _usable(a, cfg)]
    return sorted(
        usable,
        key=lambda a: (
            -a.priority,
            -(100.0 - (a.max_pct + projected.get(a.num, 0.0))),
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
    ``target_safety`` after placing this session. Returns the account number, or
    ``None`` when nothing has room (the caller then starts the session paused).
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

    Highest priority first; only an account whose projected utilization stays
    within ``target_safety`` qualifies (so a move never immediately re-exhausts
    the target — the move-then-exhaust trap). ``None`` => the caller pauses.
    """
    cost = _pct_cost(s.ctx_tokens)
    for a in _rank_accounts(acct_views, projected, cfg):
        if a.num == s.account_num:
            continue
        if _fits(a, projected, cost, cfg):
            return a.num
    return None


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
    Phase B (strand): non-pinned, non-paused, non-cooldown sessions whose
    current account is exhausted.
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
        if _in_cooldown(s, now, cfg):
            return False
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
