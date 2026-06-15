"""The ``cswap statusline`` command — the event-driven balancer trigger + display.

Claude Code runs this once per assistant message (and on a few UI events) inside
every *managed* session, passing a JSON blob on stdin that includes the live
``rate_limits`` for the session's account. That makes it a genuine usage event
source — no polling loop, no daemon. On each invocation it:

1. **heartbeats** the session's live state into ``registry.json`` (usage, cwd,
   context size, claude's session id for ``--resume``);
2. on a *rising edge* (its account crossing from below to above the exhaust
   threshold) **plans** a rebalance with the pure :mod:`~claude_swap.balancer`
   and **records the resulting intents** into the registry; and
3. **renders** a compact status line.

It NEVER rewrites another session's credentials — it only writes registry state.
Each owning :class:`~claude_swap.supervisor.Supervisor` consumes its own intent
and performs the actual re-point (the credential-ownership invariant). The
statusline must always succeed fast and print exactly one line, so the whole
body is defensive: any error prints an empty line and exits 0 rather than
breaking Claude's render.

This module also hosts :func:`run_statusfailure` — the ``cswap statusfailure``
handler behind Claude Code's ``StopFailure`` hook. It is a *next-turn* safety
net layered under the statusline's proactive threshold migration: when a turn
fails after claude exhausts its API retries because the session's ACCOUNT is
rate-limited, it records a migration intent so the next turn lands on a fresh
account. Like the statusline it only ever writes registry state, never raises,
and always exits 0.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from claude_swap import balancer, registry
from claude_swap.locking import FileLock

_BAR_WIDTH = 8


def run_statusline(switcher, stdin_text: str) -> int:
    """Entry point for ``cswap statusline``. Always returns 0."""
    try:
        return _run(switcher, stdin_text)
    except Exception:  # noqa: BLE001 - never break Claude Code's status render
        try:
            switcher._logger.debug("statusline failed", exc_info=True)
        except Exception:
            pass
        print("")
        return 0


def _run(switcher, stdin_text: str) -> int:
    try:
        payload = json.loads(stdin_text)
    except (json.JSONDecodeError, TypeError):
        print("")
        return 0
    if not isinstance(payload, dict):
        print("")
        return 0

    profile_dir = os.environ.get("CLAUDE_CONFIG_DIR", "")
    managed_id = Path(profile_dir).name if profile_dir else ""
    if not managed_id:
        # Not a managed session (the statusline is only installed into managed
        # profiles, but be safe): render nothing.
        print("")
        return 0

    rl = payload.get("rate_limits")
    rl = rl if isinstance(rl, dict) else None
    cw = payload.get("context_window") or {}
    ctx_tokens = int(
        cw.get("total_input_tokens")
        or (cw.get("current_usage") or {}).get("input_tokens")
        or 0
    )
    model_id = (payload.get("model") or {}).get("id", "")
    cwd = payload.get("cwd", "")
    claude_sid = payload.get("session_id", "")

    own_usage = registry._rl_to_usage(rl)
    own_max = _max_pct(own_usage)
    own_reset = registry.soonest_blocking_reset(own_usage)

    bcfg = switcher.get_auto_balance_config()
    cfg = balancer.config_from_dict(bcfg)
    enabled = bool(bcfg["enabled"])
    now = time.time()

    # Lock-free pre-read: discover our account + whether this is a rising edge.
    reg0 = registry.read_registry(switcher)
    row0 = reg0.get("sessions", {}).get(managed_id) or {}
    own_account = str(row0.get("account_num", "") or "")
    prev_max = row0.get("_prev_max_pct")
    # Plan when the account is at/over the exhaust threshold and not paused — on
    # the rising EDGE (first crossing) OR, as a level-based re-arm, whenever the
    # account is over threshold with no migration intent pending on the row (BUG
    # 012). Without the re-arm, ``_prev_max_pct`` stays >= threshold every tick
    # once crossed, so a migration intent that fails to consume (e.g. its target
    # account vanished mid-flight) would never re-fire and the session would be
    # stranded over-threshold until claude hard-exits.
    over_threshold = (
        enabled
        and bool(own_account)
        and own_max is not None
        and own_max >= cfg.exhaust_threshold
        and not row0.get("paused_until")
    )
    rising_edge = not (isinstance(prev_max, (int, float)) and prev_max >= cfg.exhaust_threshold)
    no_pending_intent = not (
        isinstance(row0.get("migration"), dict) and row0["migration"].get("to")
    )
    rising = over_threshold and (rising_edge or no_pending_intent)

    # Build the world OUTSIDE the lock only when we actually need to plan
    # (it may do network I/O for idle accounts). Prefer this tick's fresh usage
    # for our own account.
    acct_views = None
    if rising:
        acct_views, _ = registry.build_world(switcher, reg0, fetch_idle=True)
        prev_av = acct_views.get(own_account)
        acct_views[own_account] = balancer.AccountView(
            num=own_account,
            priority=prev_av.priority if prev_av else 0,
            max_pct=own_max,
            soonest_reset=own_reset,
            signal="live",
        )

    render_row = dict(row0)
    live_rows = registry.live_sessions(reg0)

    lock = FileLock(switcher.lock_file, timeout=5)
    if lock.acquire():
        try:
            reg = registry.read_registry(switcher)
            registry.reap_dead(reg)
            registry.expire_intents(reg, now)
            row = registry.upsert_session(
                reg,
                managed_id,
                profile_dir=profile_dir,
                cwd=cwd,
                claude_session_id=claude_sid,
                rate_limits=rl,
                ctx_tokens=ctx_tokens,
                model_id=model_id,
                claude_pid=os.getppid(),
                last_seen=now,
            )
            row["_prev_max_pct"] = own_max
            if own_reset is not None:
                row["resets_at"] = own_reset
            own_account = str(row.get("account_num", "") or own_account)

            if rising and acct_views is not None:
                sess_views = registry.session_views(reg)
                plan = balancer.rebalance(acct_views, sess_views, now, cfg)
                _apply_plan(reg, plan, now)
                reg["last_balanced_at"] = now

            registry.write_registry(switcher, reg)
            render_row = dict(reg["sessions"].get(managed_id) or {})
            live_rows = registry.live_sessions(reg)
        finally:
            lock.release()

    total = len(live_rows)
    index = next(
        (i + 1 for i, e in enumerate(live_rows) if e.get("session_id") == managed_id),
        total,
    )
    print(render_line(render_row, own_max, total, index))
    return 0


# --------------------------------------------------------------------------- #
# StopFailure safety net
# --------------------------------------------------------------------------- #

# Error types that are plainly NOT account-specific: switching accounts won't
# help (it's a server-side / overload failure), so the StopFailure safety net
# leaves them to claude's own retry / ``--fallback-model``.
_SERVER_SIDE_ERROR_TYPES = frozenset({"overloaded", "server_error"})


def run_statusfailure(switcher, stdin_text: str) -> int:
    """Entry point for ``cswap statusfailure`` (the ``StopFailure`` hook).

    A *next-turn* safety net layered under the statusline's proactive
    threshold migration. Claude Code fires ``StopFailure`` when a turn ends due
    to an API error after retries are exhausted (the "Retrying … attempt N/10"
    storm). We can't rescue the failed turn — no hook can block a turn and claude
    doesn't re-read creds on a 429 — but when the session's account is genuinely
    rate-limited we record a migration *intent* so the owning supervisor
    re-points it and the NEXT turn lands on a fresh account.

    Side-effect-only and bulletproof: never raises, always returns 0, and (like
    the statusline) only ever writes registry state — never credentials.
    """
    try:
        return _run_failure(switcher, stdin_text)
    except Exception:  # noqa: BLE001 - the StopFailure hook must never raise
        try:
            switcher._logger.debug("statusfailure failed", exc_info=True)
        except Exception:
            pass
        return 0


def _run_failure(switcher, stdin_text: str) -> int:
    try:
        payload = json.loads(stdin_text)
    except (json.JSONDecodeError, TypeError):
        return 0
    if not isinstance(payload, dict):
        return 0

    profile_dir = os.environ.get("CLAUDE_CONFIG_DIR", "")
    managed_id = Path(profile_dir).name if profile_dir else ""
    if not managed_id:
        return 0  # not a managed session

    error_type = payload.get("error_type")
    error_type = error_type if isinstance(error_type, str) else ""

    reg0 = registry.read_registry(switcher)
    row0 = reg0.get("sessions", {}).get(managed_id)
    if not isinstance(row0, dict):
        return 0  # no registry row for this session
    own_account = str(row0.get("account_num", "") or "")
    if not own_account:
        return 0

    # (b) Skip plainly-not-account-specific failures: switching accounts can't
    # help an overload / server error (claude's retry / --fallback-model owns
    # those). An absent/unknown error_type falls through to the usage check (a).
    if error_type in _SERVER_SIDE_ERROR_TYPES:
        return 0

    bcfg = switcher.get_auto_balance_config()
    cfg = balancer.config_from_dict(bcfg)
    now = time.time()

    # (a) Only migrate when this session's account is ACTUALLY rate-limited
    # (at/over the exhaust threshold). Build the world OUTSIDE the lock — it may
    # do network I/O for idle accounts.
    acct_views, _ = registry.build_world(switcher, reg0, fetch_idle=True)
    if not balancer._exhausted(acct_views.get(own_account), cfg):
        return 0

    # Build this session's SessionView from its registry row (mirrors the
    # statusline's session_views shape; include ctx_tokens so the target's
    # headroom reserve is sized correctly) and pick a migration target.
    sv = balancer.SessionView(
        session_id=managed_id,
        account_num=own_account,
        ctx_tokens=int(row0.get("ctx_tokens") or 0),
        last_seen=float(row0.get("last_seen") or 0.0),
        paused_until=row0.get("paused_until"),
        last_migrated_at=float(row0.get("last_migrated_at") or 0.0),
        pinned_account=row0.get("pinned_account"),
    )
    target = balancer.choose_migration_target(sv, acct_views, {}, cfg)
    if not target:
        return 0  # nothing has headroom -> leave it; the statusline will pause

    # Record the migration intent under the lock (mirror the statusline's
    # set_intent path). The owning supervisor consumes it and re-points; we
    # never touch credentials here.
    lock = FileLock(switcher.lock_file, timeout=5)
    if lock.acquire():
        try:
            reg = registry.read_registry(switcher)
            if managed_id in reg.get("sessions", {}):
                registry.set_intent(reg, managed_id, target, now)
                registry.write_registry(switcher, reg)
        finally:
            lock.release()
    return 0


def _apply_plan(reg: dict, plan, now: float) -> None:
    """Record a plan's decisions into the registry as state/intents.

    The statusline only ever writes registry state — never credentials. The
    owning supervisor of each session consumes its own ``migration`` intent /
    ``paused_until`` and performs the side effects.
    """
    sessions = reg.get("sessions", {})
    for act in plan.actions:
        target = sessions.get(act.session_id)
        if target is None:
            continue
        if act.kind == "MIGRATE":
            registry.set_intent(reg, act.session_id, act.to_account, now)
        elif act.kind == "PAUSE":
            target["paused_until"] = act.resume_at
        elif act.kind == "RESUME":
            target["paused_until"] = None


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def render_line(row: dict, own_max: float | None, total: int, index: int) -> str:
    """Compact one-line status, e.g. ``⇄ a2 ▕███████▁▏88% · s2/5``.

    ``→a5`` when a migration to account 5 is pending, ``⏸ a2 1h12m`` when paused
    until reset. Falls back to ASCII (``>a2 [#######-] 88% s2/5``) on terminals
    that can't render the box-drawing glyphs.
    """
    ascii_mode = not _supports_unicode()
    acct = row.get("account_num") or "?"
    pos = f"s{index}/{total}" if total else "s?"

    paused_until = row.get("paused_until")
    if isinstance(paused_until, (int, float)) and paused_until > time.time():
        head = (f"PAUSED a{acct} {_countdown(paused_until)}" if ascii_mode
                else f"⏸ a{acct} {_countdown(paused_until)}")
        return f"{head} · {pos}"

    intent = row.get("migration")
    if isinstance(intent, dict) and intent.get("to"):
        head = (f">a{intent['to']}" if ascii_mode else f"→a{intent['to']}")
    else:
        head = (f"a{acct}" if ascii_mode else f"⇄ a{acct}")

    pct_s = f"{own_max:.0f}%" if own_max is not None else "··%"
    return f"{head} {_bar(own_max, ascii_mode)}{pct_s} · {pos}"


def _bar(pct: float | None, ascii_mode: bool) -> str:
    if pct is None:
        return ("[" + "?" * _BAR_WIDTH + "] ") if ascii_mode else ("▕" + "·" * _BAR_WIDTH + "▏")
    filled = max(0, min(_BAR_WIDTH, round(pct / 100 * _BAR_WIDTH)))
    if ascii_mode:
        return "[" + "#" * filled + "-" * (_BAR_WIDTH - filled) + "] "
    return "▕" + "█" * filled + "▁" * (_BAR_WIDTH - filled) + "▏"


def _countdown(epoch: float) -> str:
    secs = max(0, int(epoch - time.time()))
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{mins}m"
    return f"{mins}m"


def _supports_unicode() -> bool:
    return "utf" in (sys.stdout.encoding or "").lower()


def _max_pct(usage: dict | None) -> float | None:
    if not usage:
        return None
    pcts = [w["pct"] for w in usage.values() if isinstance(w.get("pct"), (int, float))]
    return max(pcts) if pcts else None
