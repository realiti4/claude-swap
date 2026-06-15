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

from claude_swap.balancer import AccountView, SessionView, _pct_cost
from claude_swap.cache import MISSING, read_cache, write_cache
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
    switcher, reg: dict, *, fetch_idle: bool = True
) -> tuple[dict[str, AccountView], list[SessionView]]:
    """Build the immutable snapshot the pure balancer consumes.

    Account usage comes from the best available signal:

    * **live** — an account hosting a live managed session uses that session's
      last-reported ``rate_limits`` (free, no network);
    * **cache** — an idle account uses the shared 15s usage cache, fetching via
      the usage API on a miss only when ``fetch_idle`` is set;
    * **none** — unknown usage (no signal / fetch failed) => ``max_pct=None`` =>
      zero headroom, so the account is never chosen as a migration target.

    Must be called OUTSIDE ``switcher.lock_file`` (the idle fetch is network I/O).
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
            )
            continue

        usage = cached.get(num) if isinstance(cached, dict) else None
        if usage is None and fetch_idle:
            usage = _fetch_idle_usage(switcher, num, email)
            fetched[num] = usage

        is_usage = isinstance(usage, dict)
        h5_pct, h5_reset = _five_hour_signal(usage) if is_usage else (None, None)
        d7_pct, d7_reset = _seven_day_signal(usage) if is_usage else (None, None)
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
        )

    # Persist any freshly-fetched idle usages back into the shared cache so the
    # next render / `cswap --list` reuses them within the TTL.
    if fetched:
        merged = dict(cached)
        merged.update({k: v for k, v in fetched.items() if v is not None})
        write_cache(usage_cache_path, merged)

    return acct_views, sess_views


def _priority_of(info: dict) -> int:
    try:
        return int(info.get("priority", 0))
    except (TypeError, ValueError):
        return 0


def _usage_ttl() -> int:
    from claude_swap.switcher import _USAGE_CACHE_TTL

    return _USAGE_CACHE_TTL


def _fetch_idle_usage(switcher, num: str, email: str) -> dict | None:
    """Fetch one idle account's usage via the usage API. Best-effort, never raises."""
    from claude_swap import oauth

    try:
        creds = switcher.read_account_credentials(str(num), email)
        if not creds or not oauth.extract_access_token(creds):
            return None
        return oauth.fetch_usage_for_account(str(num), email, creds, is_active=False)
    except Exception:  # noqa: BLE001 - usage is best-effort; unknown => not a target
        switcher._logger.debug("idle usage fetch failed for %s", num, exc_info=True)
        return None
