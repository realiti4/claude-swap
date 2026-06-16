"""Live registry of cswap-managed Claude Code sessions + world-building.

A *managed* session is one launched by cswap's supervisor (``cswap launch``)
with its own per-session profile directory under ``<backup_dir>/managed/<id>``
(a SEPARATE root from the per-account ``cswap run`` profiles under
``sessions/`` — so the per-account guards keyed on ``session_dir_for`` never
fire on managed sessions). Each managed session has a private credential store,
so its owning supervisor can re-point it to a different account in place, and
its in-session statusline reports live usage back here.

The registry is the shared, lock-guarded source of truth: the statusline writes
a heartbeat (and migration *intents*) on each render; the owning supervisor and
the TUI dashboard read it. Liveness is authoritative via the OS process table
(:func:`process_detection.is_pid_alive` on the supervisor PID), never heartbeat
timeouts — a crashed session is reaped, an idle one is kept.

Functions take the :class:`~claude_swap.switcher.ClaudeAccountSwitcher` so they
reuse its lock (``switcher.lock_file``), atomic writer (``_write_json``), and
account/credential/usage machinery — nothing here is reimplemented. ``switcher``
is only ever a *parameter*; this module never imports it at module scope, to
keep the import graph acyclic (switcher imports registry lazily on its read
paths).

Two layers live here on purpose:

* a thin state store (``read_registry`` / ``write_registry`` / ``reap_dead`` /
  ``upsert_session`` / intents) operating on a plain ``reg`` dict, and
* :func:`build_world`, which turns the registry + the account list + live/idle
  usage signals into the immutable :class:`~claude_swap.balancer.AccountView` /
  :class:`~claude_swap.balancer.SessionView` snapshots the pure balancer
  consumes. ``build_world`` may do network I/O (idle-account usage fetches) and
  therefore MUST be called *outside* the lock.
"""

from __future__ import annotations

import time
from pathlib import Path

from claude_swap.balancer import PROBE_CONFIRMED_PCT, AccountView, SessionView, _pct_cost
from claude_swap.cache import (
    MISSING,
    probe_ok,
    probe_recent,
    read_cache,
    usage_backoff_active,
    write_cache,
)
from claude_swap.models import get_timestamp
from claude_swap.process_detection import is_pid_alive

REGISTRY_FILENAME = "registry.json"
REGISTRY_VERSION = 1

# Drop a migration intent that has sat unconsumed for this long (its owning
# supervisor is gone or wedged); the next rising edge re-derives it.
_INTENT_TTL_S = 1800

# A just-placed session (``rate_limits is None``) counts as synthetic load on its
# account for this long after its ``reserved_at`` stamp, so concurrent launchers
# don't all pile onto the same account before real usage lands. The reservation
# naturally stops the moment real ``rate_limits`` arrive or the TTL expires.
_RESERVE_TTL_S = 90

# Idle-5h-window priming (feature #3): a prime sweep runs at most once per this
# interval across ALL resident supervisors (gated by the ``last_primed_sweep_at``
# registry stamp under the lock — only one supervisor wins each interval, so N
# supervisors never fire N redundant billable calls).
_PRIME_SWEEP_INTERVAL_S = 300
# After an account is primed, suppress re-priming it for this long. The prime POST
# starts the window immediately, but the usage cache (which feeds the "started"
# detection) can lag by its TTL; this per-account guard bridges that gap so a
# second sweep can't double-bill before the started-window signal propagates. Once
# the window shows started, the pure detection rejects it regardless of the guard.
_PRIME_ACCOUNT_GUARD_S = 1800


# --------------------------------------------------------------------------- #
# State store (operates on a plain reg dict)
# --------------------------------------------------------------------------- #


def registry_path(switcher) -> Path:
    return switcher.backup_dir / REGISTRY_FILENAME


def _skeleton() -> dict:
    return {"version": REGISTRY_VERSION, "sessions": {}, "last_balanced_at": 0.0}


def read_registry(switcher) -> dict:
    """Return the parsed registry (lock-free). Missing/corrupt -> fresh skeleton.

    Lock-free is safe because :meth:`switcher._write_json` commits via an atomic
    rename, so a concurrent reader sees either the old or the new file whole.
    """
    data = switcher._read_json(registry_path(switcher))
    if not isinstance(data, dict):
        return _skeleton()
    if not isinstance(data.get("sessions"), dict):
        data["sessions"] = {}
    data.setdefault("version", REGISTRY_VERSION)
    data.setdefault("last_balanced_at", 0.0)
    return data


def write_registry(switcher, reg: dict) -> None:
    """Persist the registry atomically. The caller MUST hold ``switcher.lock_file``."""
    switcher._write_json(registry_path(switcher), reg)


def reap_dead(reg: dict) -> bool:
    """Drop sessions whose supervisor process is gone. Returns True if changed.

    Liveness is by the supervisor PID (the resident cswap parent), which owns the
    profile lifecycle. Rows without a recorded ``supervisor_pid`` yet (a
    heartbeat that landed before the pid was known) are kept.
    """
    sessions = reg.get("sessions", {})
    dead = [
        sid
        for sid, e in sessions.items()
        if isinstance(e.get("supervisor_pid"), int)
        and not is_pid_alive(e["supervisor_pid"])
    ]
    for sid in dead:
        sessions.pop(sid, None)
    return bool(dead)


def expire_intents(reg: dict, now: float) -> bool:
    """Drop migration intents older than the TTL. Returns True if changed."""
    changed = False
    for entry in reg.get("sessions", {}).values():
        intent = entry.get("migration")
        if intent and (now - intent.get("decided_at", 0)) > _INTENT_TTL_S:
            entry["migration"] = None
            changed = True
    return changed


def upsert_session(reg: dict, session_id: str, **fields) -> dict:
    """Create or update a session row in ``reg`` (in place); return the row.

    ``started_at`` is stamped once on first sight. ``None`` values in ``fields``
    are ignored so a heartbeat that omits a field (e.g. ``rate_limits`` before
    the first API response) never clobbers a previously-known value.
    """
    sessions = reg.setdefault("sessions", {})
    entry = sessions.get(session_id)
    if entry is None:
        entry = {
            "session_id": session_id,
            "account_num": "",
            "started_at": get_timestamp(),
            "paused_until": None,
            "pinned_account": None,
            "last_migrated_at": 0.0,
            "migration_count": 0,
            "migration": None,
        }
        sessions[session_id] = entry
    for key, value in fields.items():
        if value is not None:
            entry[key] = value
    return entry


def set_intent(reg: dict, session_id: str, to_account: str, now: float) -> None:
    """Record a migration intent for the owning supervisor to consume."""
    entry = reg.get("sessions", {}).get(session_id)
    if entry is not None:
        entry["migration"] = {"to": str(to_account), "decided_at": now}


def clear_intent(reg: dict, session_id: str) -> None:
    entry = reg.get("sessions", {}).get(session_id)
    if entry is not None:
        entry["migration"] = None


# --------------------------------------------------------------------------- #
# Idle-5h-window prime guards (feature #3) — all operate on the reg dict under
# the lock; the network prime itself runs OUTSIDE the lock in the supervisor.
# --------------------------------------------------------------------------- #


def claim_prime_sweep(reg: dict, now: float) -> bool:
    """Claim the once-per-interval prime sweep; stamp it. Returns True if claimed.

    Exactly one resident supervisor wins the claim per :data:`_PRIME_SWEEP_INTERVAL_S`
    (the others see a recent stamp and bow out), so concurrent supervisors don't
    each run a redundant sweep. The CALLER must hold the lock and persist ``reg``.
    """
    last = reg.get("last_primed_sweep_at", 0.0)
    if isinstance(last, (int, float)) and (now - float(last)) < _PRIME_SWEEP_INTERVAL_S:
        return False
    reg["last_primed_sweep_at"] = now
    return True


def prime_guarded(reg: dict, num: str, now: float) -> bool:
    """Whether account ``num`` was primed too recently to prime again (per-account guard).

    Bridges the lag between a successful prime POST and the started-window signal
    landing in the usage cache, so a second sweep can't double-bill the same
    account in that gap.
    """
    primed = reg.get("primed")
    if not isinstance(primed, dict):
        return False
    at = primed.get(str(num))
    return isinstance(at, (int, float)) and (now - float(at)) < _PRIME_ACCOUNT_GUARD_S


def stamp_primed(reg: dict, num: str, now: float) -> None:
    """Record that account ``num`` was just primed. Caller holds the lock + persists."""
    primed = reg.setdefault("primed", {})
    if isinstance(primed, dict):
        primed[str(num)] = now


def prune_primed(reg: dict, now: float) -> bool:
    """Drop per-account prime stamps older than the guard window. True if changed."""
    primed = reg.get("primed")
    if not isinstance(primed, dict):
        return False
    stale = [
        num
        for num, at in primed.items()
        if not isinstance(at, (int, float)) or (now - float(at)) >= _PRIME_ACCOUNT_GUARD_S
    ]
    for num in stale:
        primed.pop(num, None)
    return bool(stale)


def session_is_live(entry: dict) -> bool:
    pid = entry.get("supervisor_pid")
    return not isinstance(pid, int) or is_pid_alive(pid)


def live_sessions(reg: dict) -> list[dict]:
    """Registry rows whose supervisor is alive, oldest first (stable order)."""
    rows = [e for e in reg.get("sessions", {}).values() if session_is_live(e)]
    rows.sort(key=lambda e: e.get("started_at", ""))
    return rows


def session_views(reg: dict) -> list[SessionView]:
    """Pure :class:`SessionView` snapshot of the live registry rows (no I/O).

    Shared by the statusline planner and the supervisor so both feed the pure
    balancer identical inputs.
    """
    return [
        SessionView(
            session_id=e["session_id"],
            account_num=str(e.get("account_num", "")),
            ctx_tokens=int(e.get("ctx_tokens") or 0),
            last_seen=float(e.get("last_seen") or 0.0),
            paused_until=e.get("paused_until"),
            last_migrated_at=float(e.get("last_migrated_at") or 0.0),
            pinned_account=e.get("pinned_account"),
        )
        for e in live_sessions(reg)
    ]


# --------------------------------------------------------------------------- #
# Usage-signal normalization (the live-vs-cached schema seam)
# --------------------------------------------------------------------------- #


def _rl_to_usage(rate_limits: dict | None) -> dict | None:
    """Normalize the statusline ``rate_limits`` shape to the usage-dict shape.

    Statusline stdin uses ``{window: {used_percentage, resets_at(epoch s)}}``;
    the rest of the codebase (``oauth.build_usage_result``) uses
    ``{window: {pct, ...}}``. Converting to ``{window: {pct, resets_at}}`` lets
    ``switcher._max_usage_pct`` and the reset math work uniformly on both the
    live (statusline) and cached (usage API) signals.
    """
    if not isinstance(rate_limits, dict):
        return None
    out: dict = {}
    for window in ("five_hour", "seven_day"):
        entry = rate_limits.get(window)
        if isinstance(entry, dict):
            pct = entry.get("used_percentage")
            if pct is None:
                pct = entry.get("pct")  # tolerate an already-normalized entry
            if isinstance(pct, (int, float)):
                norm = {"pct": float(pct)}
                resets = entry.get("resets_at")
                if isinstance(resets, (int, float)):
                    norm["resets_at"] = int(resets)
                out[window] = norm
    return out or None


def _five_hour_signal(usage: dict | None) -> tuple[float | None, int | None]:
    """Return ``(five_hour_pct, five_hour_reset)`` from a normalized usage dict.

    Expects the ``{window: {pct, resets_at}}`` shape (the output of
    :func:`_rl_to_usage` and of ``oauth.build_usage_result``). ``pct`` is the 5h
    utilization percent; ``resets_at`` is the 5h reset epoch when present (the API
    only emits one once the window is running). Either element is ``None`` when
    absent — the idle-window priming detection treats a missing reset + ~0 pct as
    an unstarted clock (see :func:`balancer.accounts_needing_prime`).
    """
    if not isinstance(usage, dict):
        return None, None
    entry = usage.get("five_hour")
    if not isinstance(entry, dict):
        return None, None
    pct = entry.get("pct")
    pct = float(pct) if isinstance(pct, (int, float)) else None
    reset = entry.get("resets_at")
    reset = int(reset) if isinstance(reset, (int, float)) else None
    return pct, reset


def _seven_day_signal(usage: dict | None) -> tuple[float | None, int | None]:
    """Return ``(seven_day_pct, seven_day_reset)`` from a normalized usage dict.

    Mirror of :func:`_five_hour_signal` for the weekly window. Expects the
    ``{window: {pct, resets_at}}`` shape; either element is ``None`` when absent.
    Surfaced on :class:`AccountView` so the dashboard can show the 5h and weekly
    caps independently of ``max_pct`` (which is only their max).
    """
    if not isinstance(usage, dict):
        return None, None
    entry = usage.get("seven_day")
    if not isinstance(entry, dict):
        return None, None
    pct = entry.get("pct")
    pct = float(pct) if isinstance(pct, (int, float)) else None
    reset = entry.get("resets_at")
    reset = int(reset) if isinstance(reset, (int, float)) else None
    return pct, reset


def _spend_signal(usage: dict | None) -> tuple[bool, float | None]:
    """Return ``(extra_usage_enabled, spend_pct)`` from a usage-API result dict.

    Only the usage API result (``oauth.build_usage_result``) carries pay-as-you-go
    info — ``extra_usage_enabled`` and the dollar-denominated ``spend`` entry; the
    live statusline ``rate_limits`` signal does NOT. So ``build_world`` reads this
    from the cached usage even for a live account. ``spend_pct`` is the monthly
    extra-usage budget utilization (0-100), or ``None`` when absent/unlimited.
    Feeds the balancer's API-rate last-resort tier (:func:`balancer._api_capable`).
    """
    if not isinstance(usage, dict):
        return False, None
    enabled = bool(usage.get("extra_usage_enabled"))
    spend = usage.get("spend")
    pct = None
    if isinstance(spend, dict) and isinstance(spend.get("pct"), (int, float)):
        pct = float(spend["pct"])
    return enabled, pct


def soonest_blocking_reset(usage: dict | None) -> int | None:
    """Return the epoch reset of the window that is *currently capping* an account.

    That is the window equal to ``max(5h, 7d)`` utilization — a 5h reset must not
    be treated as freeing an account whose 7d window is the one over the limit.
    Expects the normalized ``{window: {pct, resets_at}}`` shape.
    """
    if not isinstance(usage, dict):
        return None
    best_pct: float | None = None
    best_reset: int | None = None
    for window in ("five_hour", "seven_day"):
        entry = usage.get(window)
        if isinstance(entry, dict) and isinstance(entry.get("pct"), (int, float)):
            pct = float(entry["pct"])
            if best_pct is None or pct > best_pct:
                best_pct = pct
                best_reset = entry.get("resets_at")
    return best_reset


# --------------------------------------------------------------------------- #
# World building (MUST run outside the lock — may do network I/O)
# --------------------------------------------------------------------------- #


def _max_pct(usage: dict | None) -> float | None:
    # Lazy import keeps the switcher <-> registry import graph acyclic.
    from claude_swap.switcher import _max_usage_pct

    return _max_usage_pct(usage)


def _reserve_load_by_account(rows: list[dict], now: float) -> dict[str, float]:
    """Synthetic placement-reservation load per account (BUG 003).

    A just-placed managed session has ``rate_limits=None`` (no usage reported
    yet), so ``build_world`` would otherwise see zero load for it and every
    concurrent launcher would pick the same account. To spread concurrent
    ``cswap launch`` processes, each such not-yet-reported session counts as a
    reservation worth :func:`balancer._pct_cost` of its context size, but only
    while its ``reserved_at`` stamp is within :data:`_RESERVE_TTL_S`. The
    reservation evaporates the moment real ``rate_limits`` arrive or the TTL
    lapses, so it never lingers as phantom load.
    """
    reserve: dict[str, float] = {}
    for e in rows:
        if e.get("rate_limits") is not None:
            continue
        reserved_at = e.get("reserved_at")
        if not isinstance(reserved_at, (int, float)):
            continue
        if (now - float(reserved_at)) > _RESERVE_TTL_S:
            continue
        acct = str(e.get("account_num", ""))
        if not acct:
            continue
        reserve[acct] = reserve.get(acct, 0.0) + _pct_cost(int(e.get("ctx_tokens") or 0))
    return reserve


def _with_reserve(max_pct: float | None, reserve: float) -> float | None:
    """Raise a known ``max_pct`` by the reservation load (clamped to 100).

    Unknown usage (``max_pct is None``) is left as-is — an account with no usage
    signal already has zero headroom and is never a target, so there is nothing
    to reserve against.
    """
    if max_pct is None or reserve <= 0.0:
        return max_pct
    return min(100.0, max_pct + reserve)


def build_world(
    switcher, reg: dict, *, fetch_idle: bool = True, probe_unavailable: bool = False
) -> tuple[dict[str, AccountView], list[SessionView]]:
    """Build the immutable snapshot the pure balancer consumes.

    Account usage comes from the best available signal:

    * **live** — an account hosting a live managed session uses that session's
      last-reported ``rate_limits`` (free, no network);
    * **cache** — an idle account uses the shared 15s usage cache, fetching via
      the usage API on a miss only when ``fetch_idle`` is set;
    * **probe** — an idle account whose usage signal is UNAVAILABLE (the usage
      endpoint is 429-backed-off, or a within-TTL fetch failure) is probed via the
      messages endpoint (a SEPARATE rate-limit bucket) ONLY when ``probe_unavailable``
      is set. A 2xx confirms the account can serve a turn now; it enters the model
      with a conservative synthesized ``max_pct=PROBE_CONFIRMED_PCT`` so a stranded
      session can resume onto it instead of stalling for the whole backoff window.
      Costs one billable Haiku turn, throttled cross-process to once per
      ``PROBE_VERDICT_TTL_S`` and NEVER run for the active default / a live-session
      account. Default OFF: only the supervisor's strand/resume/placement paths
      request it; render paths (statusline, dashboard, ``cswap --list``) never do;
    * **none** — unknown usage (no signal / fetch failed / probe capped-or-unknown)
      => ``max_pct=None`` => zero headroom, so the account is never a migration target.

    Must be called OUTSIDE ``switcher.lock_file`` (the idle fetch + probe are
    network I/O).
    """
    seq = switcher._get_sequence_data() or {}
    accounts = seq.get("accounts", {})

    rows = live_sessions(reg)
    sess_views = session_views(reg)

    # Synthetic placement-reservation load (BUG 003): a just-placed session not
    # yet reporting usage counts as load on its account so concurrent launchers
    # spread instead of stacking. Folded into each account's max_pct below.
    reserve = _reserve_load_by_account(rows, time.time())

    # Best live rate_limits per account (most-recently-seen wins).
    live_rl_by_account: dict[str, dict] = {}
    live_seen_at: dict[str, float] = {}
    for e in rows:
        rl = e.get("rate_limits")
        acct = str(e.get("account_num", ""))
        seen = float(e.get("last_seen") or 0.0)
        if isinstance(rl, dict) and acct and seen >= live_seen_at.get(acct, -1.0):
            live_rl_by_account[acct] = rl
            live_seen_at[acct] = seen

    usage_cache_path = switcher.backup_dir / "cache" / "usage.json"
    cached = read_cache(usage_cache_path, _usage_ttl())
    cached = cached if (cached is not MISSING and isinstance(cached, dict)) else {}
    # Persisted markers survive the freshness TTL: a 429 backoff marker holds the
    # cache slot until its server-requested Retry-After elapses, so a rate-limited
    # usage endpoint isn't re-hit every TTL (the "usage unavailable" feedback loop).
    persisted = read_cache(usage_cache_path, float("inf"))
    persisted = persisted if (persisted is not MISSING and isinstance(persisted, dict)) else {}

    # The active default login (current ~/.claude identity). Its OAuth token — and
    # that of any account with a live session — must NEVER be refreshed by the idle
    # usage path NOR probed via the messages endpoint: refreshing rotates the
    # single-use refresh token out from under the live login and forces a re-login,
    # and probing would touch credentials Claude Code owns. This mirrors the guard
    # `list_accounts` applies; threading it here closes the asymmetry that was
    # logging users out. Resolved whenever we may fetch idle usage OR probe — both
    # need the active/live guard.
    active_num = switcher.active_account_num() if (fetch_idle or probe_unavailable) else None

    acct_views: dict[str, AccountView] = {}
    fetched: dict[str, dict | None] = {}
    for num, info in accounts.items():
        num = str(num)
        priority = _priority_of(info)
        email = info.get("email", "")

        resv = reserve.get(num, 0.0)

        live_rl = live_rl_by_account.get(num)
        if live_rl is not None:
            usage = _rl_to_usage(live_rl)
            h5_pct, h5_reset = _five_hour_signal(usage)
            d7_pct, d7_reset = _seven_day_signal(usage)
            # Pay-as-you-go info isn't in the live statusline signal — read it
            # from the cached usage-API result (best-effort). We deliberately do
            # NOT fetch it for a live account: that adds usage-API load (which can
            # trip the endpoint's rate limit) and could refresh an active account's
            # token. On a cold cache extra-usage is simply unknown until an idle
            # fetch / ``cswap --status`` populates it — acceptable since the
            # API-rate tier is opt-in and off by default.
            extra_usage, spend_pct = _spend_signal(cached.get(num))
            acct_views[num] = AccountView(
                num=num,
                priority=priority,
                max_pct=_with_reserve(_max_pct(usage), resv),
                soonest_reset=soonest_blocking_reset(usage),
                signal="live",
                five_hour_pct=h5_pct,
                five_hour_reset=h5_reset,
                seven_day_pct=d7_pct,
                seven_day_reset=d7_reset,
                extra_usage=extra_usage,
                spend_pct=spend_pct,
            )
            continue

        cached_entry = cached.get(num) if isinstance(cached, dict) else None
        persisted_entry = persisted.get(num) if isinstance(persisted, dict) else None
        # A no-signal account whose usage is UNAVAILABLE (429-backed-off endpoint or
        # a within-TTL failure) is a candidate for the messages-API headroom probe.
        # The probe view (a synthesized ``"probe"`` AccountView), when produced,
        # short-circuits the usual usage->AccountView mapping below.
        probe_view: AccountView | None = None
        if probe_unavailable and probe_ok(persisted_entry) and probe_recent(persisted_entry):
            # A fresh cached probe-OK verdict ({"_probe_ok": True, ...}). It carries
            # no usage windows and no _unavailable marker, so neither the backoff
            # branch nor the within-TTL-failure branch below would catch it — it
            # would fall through to ``elif cached_entry is not None`` and become a
            # signal="cache" view with max_pct=None (zero headroom, never a target),
            # silently defeating the whole resume-onto-a-probed-account feature on
            # every build_world pass after the one that fired the live probe. Reuse
            # the verdict WITHOUT a network call instead. Read from ``persisted_entry``
            # (inf TTL), not ``cached_entry``: the verdict carries no retry_until, so
            # it would expire from the 60s freshness cache at half the documented
            # PROBE_VERDICT_TTL_S cadence — the persisted read keeps it alive for the
            # full window so the cross-process cadence cap actually holds for OK
            # verdicts (it already does for the 429/None markers via retry_until).
            usage = None
            probe_view = _maybe_probe(
                switcher, num, email, priority, resv,
                persisted_entry, active_num, probe_unavailable, fetched,
            )
        elif usage_backoff_active(persisted_entry):
            # Under a server-requested 429 backoff: the usage endpoint is no signal
            # and must NOT be refetched. Fall back to a messages probe (a separate
            # rate-limit bucket) when asked — THE fix for the hour-long resume stall.
            usage = None
            probe_view = _maybe_probe(
                switcher, num, email, priority, resv,
                persisted_entry, active_num, probe_unavailable, fetched,
            )
            if probe_view is None:
                # No probe (disabled / active / throttled-and-not-OK): keep the
                # backoff marker so it survives this write.
                fetched.setdefault(num, persisted_entry)
        elif isinstance(cached_entry, dict) and cached_entry.get("_unavailable"):
            # A cached fetch FAILURE within the freshness TTL. No signal, do NOT
            # refetch the usage endpoint — but a messages probe can still confirm
            # headroom (throttled cross-process by the ``_probed_at`` stamp).
            usage = None
            probe_view = _maybe_probe(
                switcher, num, email, priority, resv,
                cached_entry, active_num, probe_unavailable, fetched,
            )
        elif cached_entry is not None:
            usage = cached_entry
        elif fetch_idle:
            # Never refresh/rotate the token of the active default login or an
            # account with a live session — that is the credential-invalidation
            # bug. With is_active=True the fetch skips refresh; a 401 then simply
            # yields no usage (max_pct=None), which the balancer already treats as
            # zero headroom — far better than logging the user out.
            acct_is_active = (
                (active_num is not None and num == str(active_num))
                or switcher.has_live_session(num, email)
            )
            fail: dict = {}
            usage = _fetch_idle_usage(
                switcher, num, email, is_active=acct_is_active, failure_out=fail
            )
            # Persist the outcome either way — real usage, or a failure marker so
            # retries are throttled. On a 429 the marker carries the server's
            # Retry-After so we back off for as long as it asked, not just one TTL.
            if usage is not None:
                fetched[num] = usage
            else:
                marker: dict = {"_unavailable": True}
                ra = fail.get("retry_after")
                if isinstance(ra, (int, float)) and ra > 0:
                    marker["retry_until"] = time.time() + ra
                fetched[num] = marker
        else:
            usage = None

        if probe_view is not None:
            acct_views[num] = probe_view
            continue

        is_usage = isinstance(usage, dict)
        h5_pct, h5_reset = _five_hour_signal(usage) if is_usage else (None, None)
        d7_pct, d7_reset = _seven_day_signal(usage) if is_usage else (None, None)
        extra_usage, spend_pct = _spend_signal(usage) if is_usage else (False, None)
        acct_views[num] = AccountView(
            num=num,
            priority=priority,
            max_pct=_with_reserve(_max_pct(usage), resv) if is_usage else None,
            soonest_reset=soonest_blocking_reset(usage) if is_usage else None,
            signal="cache" if is_usage else "none",
            five_hour_pct=h5_pct,
            five_hour_reset=h5_reset,
            seven_day_pct=d7_pct,
            seven_day_reset=d7_reset,
            extra_usage=extra_usage,
            spend_pct=spend_pct,
        )

    # Persist any freshly-fetched idle usages back into the shared cache so the
    # next render / `cswap --list` reuses them within the TTL.
    if fetched:
        merged = dict(cached)
        merged.update({k: v for k, v in fetched.items() if v is not None})
        write_cache(usage_cache_path, merged)

    return acct_views, sess_views


def _probe_view(num: str, priority: int, resv: float, entry: dict | None) -> AccountView:
    """A synthesized ``"probe"`` :class:`AccountView` for a confirmed-runnable account.

    Built only after a messages probe (or a fresh cached OK verdict) confirms the
    account can serve a turn now. ``max_pct`` is the conservative
    :data:`~claude_swap.balancer.PROBE_CONFIRMED_PCT`, folded through
    :func:`_with_reserve` so a just-placed online reservation still stacks (keeping
    two stranded sessions from co-exhausting it). Per-window pct/reset are ``None``
    (a probe yields no window data); ``soonest_reset`` is ``None`` (unknown).
    extra-usage/spend are read best-effort from ``entry`` (usually absent on a
    backoff/probe marker, so typically ``(False, None)``).
    """
    extra_usage, spend_pct = _spend_signal(entry)
    return AccountView(
        num=num,
        priority=priority,
        max_pct=_with_reserve(PROBE_CONFIRMED_PCT, resv),
        soonest_reset=None,
        signal="probe",
        five_hour_pct=None,
        five_hour_reset=None,
        seven_day_pct=None,
        seven_day_reset=None,
        extra_usage=extra_usage,
        spend_pct=spend_pct,
    )


def _claim_probe_slot(switcher, num: str):
    """Compare-and-set a provisional probe claim for ``num`` under the lock.

    The lock-free freshness re-read in :func:`_maybe_probe` leaves a herd window: N
    supervisors waking together can all see a stale stamp and all probe before any
    stamp lands. This closes it. Under :attr:`switcher.lock_file`, re-read the
    account's ``usage.json`` slot and re-check :func:`cache.probe_recent`:

    * if a FRESH verdict raced in (another process already probed), return that
      entry so the caller reuses it (when OK) or stays no-signal — no probe fires;
    * otherwise stamp a provisional ``{"_unavailable": True, "_probed_at": now}``
      placeholder (so every other supervisor now sees a fresh verdict and bows out)
      and return ``None`` to signal "you won the claim, go probe". The caller's real
      verdict overwrites this placeholder after the (out-of-lock) network probe.

    Returns ``None`` (go probe) when the slot was claimed OR when the lock could not
    be acquired — best-effort: a missed claim degrades to the old lock-free behaviour
    (a possible duplicate probe), never to a crash or a dropped probe.
    """
    from claude_swap.locking import FileLock

    usage_cache_path = switcher.backup_dir / "cache" / "usage.json"
    lock = FileLock(switcher.lock_file, timeout=5)
    if not lock.acquire():
        return None
    try:
        latest = read_cache(usage_cache_path, float("inf"))
        latest = latest if (latest is not MISSING and isinstance(latest, dict)) else {}
        current = latest.get(num)
        if probe_recent(current):
            # Another supervisor stamped a fresh verdict in the race -> reuse it.
            return current
        merged = dict(latest)
        merged[num] = {"_unavailable": True, "_probed_at": time.time()}
        write_cache(usage_cache_path, merged)
        return None
    finally:
        lock.release()


def _maybe_probe(
    switcher,
    num: str,
    email: str,
    priority: int,
    resv: float,
    entry: dict | None,
    active_num,
    probe_unavailable: bool,
    fetched: dict[str, dict | None],
) -> AccountView | None:
    """Messages-API headroom fallback for a no-signal idle account.

    Returns a ``"probe"`` :class:`AccountView` when the account is confirmed
    runnable now (via a fresh cached OK verdict, reused WITHOUT a network call, or a
    live probe that returned ``True``); otherwise ``None`` (probing disabled,
    account active/live, throttle gate not OK, or the probe came back capped/unknown).

    Gating (ALL must hold to fire a live probe):

    * ``probe_unavailable`` is set (opt-in; only the supervisor's strand/resume
      paths request it — render paths never do);
    * the account is NOT the active default login and has NO live session (hard
      credential-safety invariant — never touch creds Claude Code owns);
    * no fresh verdict is cached for it (``cache.probe_recent`` — the cross-process
      cadence cap; while a recent verdict exists every supervisor REUSES it instead
      of re-probing, so N supervisors never each fire a billable probe).

    Each verdict is written into ``fetched[num]`` (the same shared ``usage.json``
    slot the 429 marker uses) so the cadence is enforced cross-process:
      * True  -> ``{"_probe_ok": True, "_probed_at": now}`` + a probe AccountView;
      * False (429) -> ``{"_unavailable": True, "retry_until": now+Retry-After,
                          "_probed_at": now}`` (refresh the backoff with the messages
                          bucket's Retry-After);
      * None (unknown) -> ``{"_unavailable": True, "_probed_at": now}`` (no
                          retry_until, so it elapses on the usage TTL; the stamp
                          still throttles a re-probe for the cadence window).

    Best-effort, never raises (``_probe_idle_headroom`` swallows its own errors).
    """
    if not probe_unavailable:
        return None
    # Reuse a fresh cached verdict without a network call (cross-process throttle).
    if probe_recent(entry):
        if probe_ok(entry):
            fetched.setdefault(num, entry)
            return _probe_view(num, priority, resv, entry)
        # A recent capped/unknown verdict: stay no-signal, no re-probe yet.
        return None
    # Never probe the active default login or an account with a live session.
    acct_is_active = (
        (active_num is not None and num == str(active_num))
        or switcher.has_live_session(num, email)
    )
    if acct_is_active:
        return None

    # Close the cross-process herd window (TOCTOU): the freshness re-read above is
    # lock-free, so N supervisors waking on the same ~30s pause cadence could all
    # observe a stale stamp and all fire a billable probe before any stamp lands.
    # Compare-and-set a provisional claim UNDER THE LOCK — re-read this account's
    # cache slot, re-check probe_recent, and if it is still stale stamp a placeholder
    # so every other supervisor now sees a fresh verdict and bows out. The billable
    # probe itself still runs OUTSIDE the lock (it is network I/O) and overwrites the
    # placeholder with the real verdict below. A reuse-able fresh verdict that landed
    # in the race is honoured here. Best-effort: if the lock can't be taken we fall
    # through and probe anyway (degrades to the old behaviour, never worse).
    raced = _claim_probe_slot(switcher, num)
    if raced is not None:
        if probe_ok(raced):
            fetched.setdefault(num, raced)
            return _probe_view(num, priority, resv, raced)
        return None

    fail: dict = {}
    verdict = _probe_idle_headroom(
        switcher, num, email, is_active=acct_is_active, failure_out=fail
    )
    now = time.time()
    if verdict is True:
        fetched[num] = {"_probe_ok": True, "_probed_at": now}
        return _probe_view(num, priority, resv, entry)
    if verdict is False:
        marker: dict = {"_unavailable": True, "_probed_at": now}
        ra = fail.get("retry_after")
        if isinstance(ra, (int, float)) and ra > 0:
            marker["retry_until"] = now + ra
        fetched[num] = marker
        return None
    # Unknown (None): stamp so the re-probe is throttled, but no retry_until.
    fetched[num] = {"_unavailable": True, "_probed_at": now}
    return None


def _priority_of(info: dict) -> int:
    try:
        return int(info.get("priority", 0))
    except (TypeError, ValueError):
        return 0


def _usage_ttl() -> int:
    from claude_swap.switcher import _USAGE_CACHE_TTL

    return _USAGE_CACHE_TTL


def _fetch_idle_usage(
    switcher, num: str, email: str, *, is_active: bool = False, failure_out: dict | None = None
) -> dict | None:
    """Fetch one idle account's usage via the usage API. Best-effort, never raises.

    ``is_active`` MUST be True for the active default login or any account with a
    live session: with it set, the usage path never refreshes/rotates the OAuth
    token (Claude Code owns those credentials). For a genuinely-idle account whose
    expired token IS refreshed, the rotated token is persisted back to the canonical
    backup under the lock — so cswap never leaves its own backup holding a dead
    (single-use, already-rotated) refresh token, which would otherwise log the
    account out on its next refresh.
    """
    from claude_swap import oauth
    from claude_swap.locking import FileLock

    try:
        creds = switcher.read_account_credentials(str(num), email)
        if not creds or not oauth.extract_access_token(creds):
            return None

        def _persist(acct_num: str, acct_email: str, new_creds: str) -> None:
            with FileLock(switcher.lock_file):
                switcher.write_account_credentials(acct_num, acct_email, new_creds)

        return oauth.fetch_usage_for_account(
            str(num), email, creds,
            is_active=is_active,
            persist_credentials=None if is_active else _persist,
            failure_out=failure_out,
        )
    except Exception:  # noqa: BLE001 - usage is best-effort; unknown => not a target
        switcher._logger.debug("idle usage fetch failed for %s", num, exc_info=True)
        return None


def _probe_idle_headroom(
    switcher, num: str, email: str, *, is_active: bool = False, failure_out: dict | None = None
) -> bool | None:
    """Probe one idle account's headroom via the messages endpoint. Best-effort.

    Structurally identical to :func:`_fetch_idle_usage` (creds read + the optional
    expired-token refresh-and-persist happen here; the network probe runs OUTSIDE
    the lock), but it answers a different question — "can this account serve a turn
    NOW?" — using the messages bucket, which is unaffected by a 429 on the usage
    endpoint. Returns the tri-state of :func:`oauth.probe_messages_headroom`
    (``True``/``False``/``None``); on any local failure, ``None`` (unknown).

    ``is_active`` MUST be True for the active default login / a live-session account
    (the caller's gate already refuses to probe those); with it set the expired-token
    refresh is skipped so Claude Code's single-use refresh token is never rotated.
    For a genuinely-idle account whose token IS refreshed, the rotated token is
    persisted back under the lock — never leaving cswap holding a dead refresh token.

    Only a recognized Claude *subscription* account is probed. The probe sends the
    exact same billable ``/v1/messages`` turn as priming, so it MUST carry priming's
    subscription-tier guard (``oauth.is_primable_subscription``): without it, a
    pay-as-you-go / API / console account — or a subscription-exhausted account with
    extra-usage enabled — returns a billed 2xx that the balancer would misread as
    ~80% subscription headroom, silently charging real dollars and defeating
    ``onlySubscriptionTokens``. A non-subscription account returns ``None`` (unknown
    => no headroom, never a target), mirroring the prime invariant exactly.
    """
    from claude_swap import oauth
    from claude_swap.locking import FileLock

    try:
        creds = switcher.read_account_credentials(str(num), email)
        if not creds:
            return None
        # Never probe a non-subscription account: the messages turn bills real money
        # and a 2xx can't distinguish "subscription has room" from "PAYG just charged
        # me". Mirrors supervisor._prime_one_account's guard so the probe never bills
        # a pay-as-you-go account, regardless of cfg.only_subscription.
        if not oauth.is_primable_subscription(oauth.extract_subscription_type(creds)):
            switcher._logger.debug(
                "probe: skipping %s — not a primeable subscription account", num
            )
            return None
        oauth_data = oauth.extract_oauth_data(creds) or {}
        token = oauth_data.get("accessToken")
        if not token:
            return None

        # Refresh an expired token for this INACTIVE account, persisting under the
        # lock (mirrors the usage path); never refresh an active/live account.
        if (
            not is_active
            and oauth_data.get("refreshToken")
            and oauth.is_oauth_token_expired(oauth_data.get("expiresAt"))
        ):
            refreshed = oauth.refresh_oauth_credentials(creds)
            if refreshed:
                token = oauth.extract_access_token(refreshed) or token
                try:
                    with FileLock(switcher.lock_file):
                        switcher.write_account_credentials(str(num), email, refreshed)
                except Exception:
                    switcher._logger.debug(
                        "probe: persist refreshed creds failed for %s", num, exc_info=True
                    )

        return oauth.probe_messages_headroom(token, failure_out=failure_out)
    except Exception:  # noqa: BLE001 - probing is best-effort; unknown => not a target
        switcher._logger.debug("idle headroom probe failed for %s", num, exc_info=True)
        return None
