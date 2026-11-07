"""Data service for the TUI: snapshots, blocking actions, display helpers.

The TUI never parses printed CLI output — it consumes
``ClaudeAccountSwitcher.accounts_snapshot`` (one collect pass, see
switcher.py) and renders structured data. Fetch pacing lives in
``claude_swap.snapshot_source.SnapshotSource`` (shared with any GUI shell);
this module re-exports it for the TUI's use.

Everything here is blocking (file locks, keychain subprocesses, network) and
must be called from a thread worker, never the UI event loop.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from claude_swap import oauth, printer, usage_store
from claude_swap.autoswitch import (
    CONSUME_FIRST_STRATEGIES,
    STATE_FILENAME,
    ThresholdMap,
    _binding_at,
    _classify_dynamic_trigger,
    _dynamic_active_headroom,
    _headroom_by_account,
    _model_window_binds_everywhere,
    _seven_day_reset_ts,
    _wall_headroom,
    dynamic_candidate_order,
    dynamic_expiring,
    rank_candidates_pass,
    rank_priority_candidates,
)
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.lines import weekly_capped
from claude_swap.models import AccountsSnapshot
from claude_swap.settings import parse_model_names
from claude_swap.snapshot_source import SnapshotSource
from claude_swap.switcher import SENTINEL_NOTES, last_seen_note

if TYPE_CHECKING:
    from claude_swap.settings import AutoSwitchSettings

# Triggers where the ranking pass never runs -- keys the auto view's own
# `_UNMODELED_TEXT` shares (kept in sync by a test, not by import).
_UNMODELED_TRIGGERS = frozenset({"below-threshold", "unreadable-active"})


# ---------------------------------------------------------------------------
# Blocking actions (switch/add/remove) run captured, off the UI thread
# ---------------------------------------------------------------------------


@dataclass
class ActionResult:
    """Outcome of a captured switcher action."""

    ok: bool
    output: str  # captured stdout+stderr, ANSI-colored (render with Text.from_ansi)
    payload: dict | None = None  # structured result for json-capable actions

    @property
    def first_line(self) -> str:
        """First non-empty output line, ANSI-stripped — notification material."""
        from rich.text import Text

        for line in self.output.splitlines():
            plain = Text.from_ansi(line).plain.strip()
            if plain:
                return plain
        return ""


def run_action(fn: Callable[[], dict | None]) -> ActionResult:
    """Run a switcher action capturing stdout+stderr (color forced on).

    ``sys.stdin`` is swapped for an empty stream so an unexpected ``input()``
    raises ``EOFError`` instead of freezing the app (in-scope actions never
    prompt once ``assume_yes``/explicit identifiers are used; this is
    defensive). The redirect is process-global for the duration — fine here
    because the TUI owns the terminal and nothing else prints while it runs.
    """
    buf = io.StringIO()
    payload: dict | None = None
    saved_stdin = sys.stdin
    sys.stdin = io.StringIO()
    try:
        with printer.force_color(), contextlib.redirect_stdout(
            buf
        ), contextlib.redirect_stderr(buf):
            try:
                payload = fn()
            except ClaudeSwitchError as e:
                print(f"Error: {e}")
                return ActionResult(False, buf.getvalue())
            except EOFError:
                print("Error: interactive input is not available here.")
                return ActionResult(False, buf.getvalue())
    finally:
        sys.stdin = saved_stdin
    return ActionResult(
        True, buf.getvalue(), payload if isinstance(payload, dict) else None
    )


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def sentinel_label(sentinel: str) -> str:
    """The same wording ``cswap list`` prints for this sentinel state."""
    return SENTINEL_NOTES.get(sentinel, sentinel)


def window_pct(last_good: dict | None, key: str) -> float | None:
    """Utilization pct of one window ("five_hour"/"seven_day"), if known."""
    if not isinstance(last_good, dict):
        return None
    window = last_good.get(key)
    if not isinstance(window, dict):
        return None
    pct = window.get("pct")
    return float(pct) if isinstance(pct, (int, float)) else None


def reset_text(window: dict | None, now: float) -> str | None:
    """Live countdown to one window's reset ("resets 2h 13m"), if known.

    Computed from ``resets_at`` at render time — the countdown the API sent
    was correct at *fetch* time and drifts as the measurement ages.
    """
    if not isinstance(window, dict):
        return None
    resets_at = window.get("resets_at")
    if not resets_at:
        return None
    try:
        ts = datetime.fromisoformat(str(resets_at).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    remaining = ts - now
    if remaining <= 0:
        return "resets now"
    return f"resets {format_duration(remaining)}"


def reset_clock(window: dict | None, now: float) -> str | None:
    """Absolute local reset time ("20:39" / "Jul 14 09:00"), if known.

    None once the reset has elapsed — "resets now" needs no clock.
    """
    if not isinstance(window, dict):
        return None
    resets_at = window.get("resets_at")
    if not resets_at:
        return None
    try:
        reset_utc = datetime.fromisoformat(str(resets_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if reset_utc.timestamp() - now <= 0:
        return None
    return oauth.reset_clock_string(
        reset_utc, datetime.fromtimestamp(now, tz=timezone.utc)
    )


def window_reset_text(last_good: dict | None, key: str, now: float) -> str | None:
    """`reset_text` for one of the top-level 5h/7d windows."""
    if not isinstance(last_good, dict):
        return None
    return reset_text(last_good.get(key), now)


def chip_label(label: str, reset: str | None) -> str:
    """The reading for one window, without its percentage: ``5h(⟳2h28m)``.

    THE one place that decides how a window reads — the dashboard's inactive
    rows and the auto view's Next-best rows both draw it, so one account
    cannot read two ways on two screens. The caller appends the pct so it can
    colour it by severity. The countdown shows whenever it is known, not only
    at 100%: a saturated candidate's worth IS when it comes back.
    """
    if not reset:
        return f"{label}:"
    return f"{label}(⟳{reset.removeprefix('resets ').replace(' ', '')}):"


def window_chip_label(last_good: dict | None, key: str, label: str, now: float) -> str:
    """`chip_label` for one of the top-level 5h/7d windows."""
    return chip_label(label, window_reset_text(last_good, key, now))


def format_duration(seconds: float) -> str:
    """Compact duration: "45s", "12m", "2h 13m", "3d 4h"."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        h, m = divmod(s // 60, 60)
        return f"{h}h {m}m" if m else f"{h}h"
    d, h = divmod(s // 3600, 24)
    return f"{d}d {h}h" if h else f"{d}d"


def format_age(age_s: float | None) -> str | None:
    """Measurement age note ("· 2m ago"); None while comfortably fresh."""
    if age_s is None or age_s < usage_store.SERVE_TTL_S:
        return None
    return f"· {format_duration(age_s)} ago"


def clock_stamp() -> str:
    """HH:MM:SS local-time stamp for the event log."""
    return time.strftime("%H:%M:%S")


def read_last_active_at(backup_dir: Path) -> dict:
    """Read-only ``lastActiveAt`` off the engine's own state file
    (``<backup_dir>/autoswitch_state.json``).

    ``AutoSwitchEngine._read_state`` is a bound method needing a live
    engine (state_path, a lock file), so it is not importable as a pure
    function here -- this mirrors its exact safety contract instead: a
    missing or garbled file reads as no cached warm context, never raises.
    """
    try:
        raw = json.loads((backup_dir / STATE_FILENAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    last_active_at = raw.get("lastActiveAt")
    return last_active_at if isinstance(last_active_at, dict) else {}


def switch_bars(snap: AccountsSnapshot) -> dict[str, float]:
    """Each account's own switch threshold, as the engine reads it (`_bars`);
    an account without one is absent and takes the strategy's bar."""
    return {
        acc.number: acc.policy.threshold
        for acc in snap.accounts
        if acc.policy.threshold is not None
    }


def rank_switch_candidates(
    snap: AccountsSnapshot,
    settings: "AutoSwitchSettings",
    now: float,
    active_number: str | None,
    last_active_at: dict | None = None,
    reasons_out: dict | None = None,
) -> tuple[list[str], str | None, str, bool]:
    """(ordered, rank_axis, trigger, unmodeled): mirrors the engine's own
    admission and order. THE shared computation -- ``ordered_accounts`` and
    the auto view's "Next best" panel both read off this, never a pass of
    their own. ``reasons_out``, when given, is filled with the engine's own
    plain-words cause for each candidate the pass whose result is used dropped.
    """
    reasons = {} if reasons_out is None else reasons_out
    models = parse_model_names(settings.model)
    consume_first = settings.strategy in CONSUME_FIRST_STRATEGIES
    usage = {acc.number: acc.usage.decision_value() for acc in snap.accounts}
    entries = {acc.number: acc.usage for acc in snap.accounts}
    # The engine's own tick map (`_resolve_thresholds`): `.get` reads the
    # overrides only, `[num]` falls back to the global threshold.
    bars = ThresholdMap(switch_bars(snap), settings.threshold)
    policies = {acc.number: acc.policy for acc in snap.accounts}

    def _capped(num: str | None) -> tuple[str, float] | None:
        """The engine's weekly-cap veto (`_tick_inner`'s `_capped`), on the
        same `weekly_capped`: the active must leave, a candidate is dropped."""
        policy = policies.get(num)
        return weekly_capped(policy.weekly_cap if policy else None, usage.get(num))

    # Capped peers drop before the split by kind, as `_tick_inner` does.
    candidates = [
        acc
        for acc in snap.accounts
        if acc.number != active_number
        and acc.switchable
        and not acc.disabled
        and _capped(acc.number) is None
    ]
    oauth_candidates = [acc.number for acc in candidates if acc.kind != "api_key"]
    api_key_candidates = (
        [acc.number for acc in candidates if acc.kind == "api_key"]
        if settings.include_api_key_accounts
        else []
    )

    def _trigger_for(active_headroom: float | None, active_disabled: bool) -> str:
        if active_disabled:
            return "disabled-active"
        if _capped(active_number) is not None:
            return "weekly-capped"  # a cap is no line: it leaves now, below one too
        if active_headroom is None:
            return "unreadable-active"
        if settings.strategy == "dynamic":
            return _classify_dynamic_trigger(
                active_headroom, _wall_headroom(bars.get(active_number))
            )
        # Each window against its OWN line, as `_tick_inner` judges the active:
        # the active's own bar stands in for the strategy's (`departure_pct`).
        departure_pct = bars.get(active_number, settings.threshold)
        active_binding = _binding_at(
            usage.get(active_number), settings, models, departure_pct
        )
        if (
            not active_binding.over
            if active_binding is not None
            else (100.0 - active_headroom) < departure_pct
        ):
            if settings.strategy == "priority-consume-first":
                return "priority"  # ranks below the line too, by tier
            return (
                settings.strategy
                if settings.strategy in CONSUME_FIRST_STRATEGIES
                else "below-threshold"
            )
        return "at-limit" if active_headroom <= 0 else "proactive"

    def _rank_dynamic_on(
        axis: tuple[str, ...], trigger: str, dropped: bool
    ) -> tuple[list[str], str | None]:
        """`dynamic`'s own healthy/proactive order, built by `dynamic_candidate_order`
        from the pieces the tick ranks by, never `rank_candidates_pass`: that pass's
        landing gate is a hysteresis MARGIN over the active, which a warm candidate
        with less headroom than a cold one can never clear against an about-to-wall
        active. `last_active_at` comes from the caller (`read_last_active_at`, the
        state file the engine writes `lastActiveAt` to on every switch): an unmeasured
        candidate reads cold, as in the engine. `expiring` is priced on the
        model-gated headroom, as the tick prices it, also on the unmodeled retry
        (`dropped`, whose cold floor is a tick-only admission and is not applied
        here). The active's own never-widened reading picks the healthy arm's walled
        escape, over the candidates the tick's escape admits; where it admits nobody
        the list is the rotation's. Whether a rotation happens now (the step, the
        dwell, the landing floor) is a tick-only decision: the list is the order it
        would take them in.
        """
        last = last_active_at or {}
        headroom = _headroom_by_account(usage, axis)
        ordered, rank_axis = dynamic_candidate_order(
            oauth_candidates, headroom, usage, entries, bars, last, settings, now,
            current=active_number,
            # The active reads as the engine passes it: widened on the model axis,
            # its own unmodeled headroom on the retry.
            active_headroom=active_headroom if axis else headroom.get(active_number),
            expiring=dynamic_expiring(
                active_number, oauth_candidates, usage, model_headroom, bars, policies,
                entries, last, settings, now,
            ),
            trigger=trigger, model_window_dropped=dropped, reasons=reasons,
            raw_active_headroom=model_headroom.get(active_number), models=models,
        )
        return ordered, (rank_axis if ordered else None)

    def _rank_on(
        axis: tuple[str, ...], trigger: str, dropped: bool = False
    ) -> tuple[list[str], str | None]:
        reasons.clear()  # a retried pass replaces the one before it
        if trigger in _UNMODELED_TRIGGERS:
            return [], None
        if settings.strategy == "dynamic" and trigger in ("proactive", "dynamic-healthy"):
            return _rank_dynamic_on(axis, trigger, dropped)
        headroom = _headroom_by_account(usage, axis)
        if settings.strategy == "priority-consume-first" and trigger != "weekly-capped":
            # The engine ranks every trigger through its priority pass alone,
            # a capped active's excepted: strict landing would hold it in place.
            ordered, *_ = rank_priority_candidates(
                oauth_candidates=oauth_candidates,
                usage=usage,
                headroom=headroom,
                current=active_number,
                active_enabled=any(
                    acc.number == active_number and acc.switchable and not acc.disabled
                    for acc in snap.accounts
                ),
                settings=settings,
                now=now,
                thresholds=bars,
                policies=policies,
                models=axis,
            )
            # The legend names `_rank_priority_candidates`'s own sort key.
            return ordered, ("highest tier, then soonest reset" if ordered else None)
        ordered, _any_known, _reset_ts, _waiting, rank_axis = rank_candidates_pass(
            models=axis,
            trigger=trigger,
            consume_first=consume_first,
            oauth_candidates=oauth_candidates,
            no_return=None,
            usage=usage,
            headroom=headroom,
            current=active_number,
            active_headroom=headroom.get(active_number),
            settings=settings,
            now=now,
            reasons=reasons,
            entries=entries,
            thresholds=bars,
        )
        return ordered, rank_axis

    model_headroom = _headroom_by_account(usage, models)
    active_disabled = next(
        (acc.disabled for acc in snap.accounts if acc.number == active_number), False
    )
    active_headroom = _dynamic_active_headroom(
        settings, models, usage, active_number, model_headroom.get(active_number),
        bars,
    )
    trigger = _trigger_for(active_headroom, active_disabled)
    unmodeled = trigger in _UNMODELED_TRIGGERS
    ordered, rank_axis = _rank_on(models, trigger)
    if (
        not ordered
        and models
        and settings.strategy == "dynamic"
        and _model_window_binds_everywhere(
            usage, models, settings.threshold, settings.weekly_threshold, bars
        )
    ):
        ordered, rank_axis = _rank_on((), trigger, dropped=True)
    if (
        not ordered
        and api_key_candidates
        and not unmodeled
        and trigger not in (*CONSUME_FIRST_STRATEGIES, "priority")
    ):
        ordered, rank_axis = api_key_candidates, None
    return ordered, rank_axis, trigger, unmodeled


def non_candidate_key(acc, now: float) -> tuple:
    """Sort key for a switchable row the engine does not pick (capped, disabled,
    spend-only, unreadable, or refused at its switch line): soonest 7d reset first,
    an unknown one last. `ordered_accounts` and the auto view's panel both call it,
    or the two screens list these rows two ways on one snapshot."""
    ts = _seven_day_reset_ts(acc.usage.last_good, now)
    return (1, float("inf") if ts is None else ts)


def is_no_plan(acc) -> bool:
    """The usage poll answered that no subscription backs this login. An
    unranked one sorts behind even an unswitchable slot, in ``ordered_accounts``
    and the auto view's panel alike."""
    return acc.usage.last_error == "oauth_not_allowed_for_organization"


def ordered_accounts(
    snap: AccountsSnapshot,
    settings: "AutoSwitchSettings",
    now: float,
    last_active_at: dict | None = None,
) -> list[str]:
    """Every account number, active first, then the rest as the engine's own
    pass would rank them: ranked-and-open, then every other switchable row
    (`non_candidate_key`: refused, capped, disabled, sentinel-blocked,
    spend-only), unswitchable last. THE one order every screen
    renders in -- a screen keeping slot order says so at its own call site.
    ``last_active_at`` is the caller's own ``read_last_active_at`` read (one
    file read feeds every screen), so ``dynamic``'s warm/cold pass ranks
    the same way here as it does in the "Next best" panel.
    """
    active_number = snap.active_number
    others = [acc for acc in snap.accounts if acc.number != active_number]
    ordered, *_ = rank_switch_candidates(
        snap, settings, now, active_number, last_active_at
    )
    ordered_rank = {num: i for i, num in enumerate(ordered)}

    def bucket(acc) -> tuple:
        if not acc.switchable:
            return (4,)
        if acc.number in ordered_rank:
            return (0, ordered_rank[acc.number])
        if is_no_plan(acc):
            return (5,)
        return non_candidate_key(acc, now)

    others.sort(key=lambda a: (bucket(a), a.number))  # matches the auto view's tie-break
    numbers = [acc.number for acc in others]
    # Active pinned first, not ranked among `others`: the engine's own pass
    # (`rank_switch_candidates`) never names the active account a candidate
    # to switch TO, so it has no rank of its own to sort by -- its place
    # here is fixed, with its own "active" label, not a position the
    # ranking axis assigns.
    return ([active_number] if active_number is not None else []) + numbers


__all__ = [
    "ActionResult",
    "SnapshotSource",
    "format_age",
    "format_duration",
    "last_seen_note",
    "ordered_accounts",
    "rank_switch_candidates",
    "read_last_active_at",
    "reset_clock",
    "reset_text",
    "run_action",
    "sentinel_label",
    "clock_stamp",
    "window_pct",
    "window_reset_text",
]
