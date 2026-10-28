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
    _about_to_wall,
    _binding_at,
    _binding_recovery_ts,
    _classify_dynamic_trigger,
    _dynamic_active_headroom,
    _headroom_by_account,
    _model_window_binds_everywhere,
    _rank_dynamic_candidates,
    _ruler,
    _wall_headroom,
    rank_candidates_pass,
    rank_priority_candidates,
)
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.models import AccountsSnapshot
from claude_swap.poll_policy import binding_pct
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
    oauth_candidates = [
        acc.number
        for acc in snap.accounts
        if acc.number != active_number
        and acc.switchable
        and not acc.disabled
        and acc.kind != "api_key"
    ]
    api_key_candidates = (
        [
            acc.number
            for acc in snap.accounts
            if acc.number != active_number
            and acc.switchable
            and not acc.disabled
            and acc.kind == "api_key"
        ]
        if settings.include_api_key_accounts
        else []
    )

    def _trigger_for(active_headroom: float | None, active_disabled: bool) -> str:
        if active_disabled:
            return "disabled-active"
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
        axis: tuple[str, ...], trigger: str
    ) -> tuple[list[str], str | None]:
        """`dynamic`'s own healthy/proactive ranking -- `_rank_dynamic_
        candidates` (warm/cold tiered, soonest weekly reset), never
        `rank_candidates_pass`: that pass's landing gate is a hysteresis
        MARGIN over the active, which a warm candidate with less headroom
        than a cold one can never clear against an about-to-wall active
        (autoswitch.py's own comment at the `_dynamic_rank` call site).
        `last_active_at` comes from the caller (`read_last_active_at`, the
        SAME state file the engine itself writes `lastActiveAt` to on every
        switch, read-only, never `{}` by construction) -- an unmeasured
        candidate still reads cold, never warm (`_is_warm`'s own contract),
        the file just being unreadable or stale here is no different from
        the engine's own read. `settings.cold_switch_cost_pct` only
        reorders cold candidates (floor-clearing first, never dropped) on
        the `proactive` trigger -- the tick's own `_tick_inner` applies
        that same partition ONLY there (`dynamic_ordered = warm_ordered +
        cold_clears_floor`, reached only when `trigger == "proactive"`);
        on `dynamic-healthy` the tick never applies it (the alternation
        arm's own admissible-partner filter is a SEPARATE, tick-only
        question), so this display stays on `_rank_dynamic_candidates`'
        own order there -- soonest weekly reset, warm before cold, a
        headroom candidate with hours to reset ranked ahead of one with
        more headroom but a reset days out (the owner's 2026-09-19 case).
        Only this ranking and the at-limit recovery order below
        (`rank_candidates_pass`) are emulated here -- the healthy arm's own
        alternation dwell/giveback rules (`alternation_chunk_seconds`,
        `ALTERNATION_MAX_GIVEBACK_PCT`) are a tick-only decision, not a
        display one, and are deliberately not built here.
        """
        headroom = _headroom_by_account(usage, axis)
        warm, cold = _rank_dynamic_candidates(
            oauth_candidates, headroom, usage, now, last_active_at or {},
            settings.cache_ttl_seconds, reasons, entries, bars,
        )
        # Only an active at the REAL wall lifts the peers' own lines, on the
        # `proactive` arm alone (`_dynamic_rank`); the healthy arm never does.
        # The active reads as the engine passes it: widened on the model axis,
        # its own unmodeled headroom on the retry.
        if (
            trigger == "proactive"
            and not warm
            and not cold
            and _about_to_wall(
                active_headroom if axis else headroom.get(active_number)
            )
        ):
            reasons.clear()
            warm, cold = _rank_dynamic_candidates(
                oauth_candidates, headroom, usage, now, last_active_at or {},
                settings.cache_ttl_seconds, reasons, entries,
            )
        if trigger == "proactive":
            cold_floor = settings.cold_switch_cost_pct
            cold_clears = [n for n in cold if headroom[n] >= cold_floor]
            cold_rest = [n for n in cold if n not in cold_clears]
            ordered = warm + cold_clears + cold_rest
        else:
            ordered = warm + cold
        # "soonest reset" -- the axis `_rank_dynamic_candidates` actually
        # sorts by (autoswitch.py's own name for it, ~3775); "soonest to
        # recover" is the DIFFERENT binding-recovery axis `rank_candidates_
        # pass` uses for the at-limit escape order, below.
        return ordered, ("soonest reset" if ordered else None)

    def _rank_on(axis: tuple[str, ...], trigger: str) -> tuple[list[str], str | None]:
        reasons.clear()  # a retried pass replaces the one before it
        if trigger in _UNMODELED_TRIGGERS:
            return [], None
        if settings.strategy == "dynamic" and trigger in ("proactive", "dynamic-healthy"):
            return _rank_dynamic_on(axis, trigger)
        headroom = _headroom_by_account(usage, axis)
        if settings.strategy == "priority-consume-first":
            # The engine ranks every trigger through its priority pass alone.
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
                policies={acc.number: acc.policy for acc in snap.accounts},
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
        ordered, rank_axis = _rank_on((), trigger)
    if (
        not ordered
        and api_key_candidates
        and not unmodeled
        and trigger not in (*CONSUME_FIRST_STRATEGIES, "priority")
    ):
        ordered, rank_axis = api_key_candidates, None
    return ordered, rank_axis, trigger, unmodeled


def waiting_tail_key(
    usage: dict | None,
    models: tuple[str, ...],
    now: float,
    settings: "AutoSwitchSettings",
    line: float,
) -> tuple:
    """Sort key for a row the admission pass refused but that still has a
    usable window: every account with headroom in ALL its windows before
    every account with ANY window at or over 100% -- a full account is
    unusable until it resets, however soon that is, so it must never
    outrank one usable right now. Soonest binding recovery breaks ties
    inside each half. `ordered_accounts` and the auto view's own
    `_candidates_text` (autoview.py) both call this, or the two screens
    can rank this tier two different ways on the same snapshot. ``line`` is
    the account's own switch threshold (``ThresholdMap[num]``), read on
    ``_ruler``'s scale as the engine's recovery reads are.
    """
    full = 1 if binding_pct(usage, models) >= 100.0 else 0
    return (
        full,
        _binding_recovery_ts(
            usage,
            models,
            now,
            threshold=line,
            weekly_threshold=settings.weekly_threshold,
            scale=_ruler(settings),
        ),
    )


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
    pass would rank them: ranked-and-open, usable-but-refused (waiting,
    soonest binding recovery first), non-target (disabled, sentinel-
    blocked, spend-only), unswitchable last. THE one order every screen
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
    models = parse_model_names(settings.model)
    bars = ThresholdMap(switch_bars(snap), settings.threshold)

    def bucket(acc) -> tuple:
        if not acc.switchable:
            return (4,)
        if acc.number in ordered_rank:
            return (0, ordered_rank[acc.number])
        if is_no_plan(acc):
            return (5,)
        # A disabled slot is a non-target -- the engine never lands on one
        # automatically, however soon its own window recovers -- so it
        # sorts with the other non-targets, never inside the waiting tier
        # below by reset-time coincidence.
        if acc.usage.sentinel is not None:
            return (2,)
        if binding_pct(acc.usage.last_good, models) is None:
            # Spend-only: the same bucket whether disabled or not -- a
            # spend axis carries no window pct to gate `disabled` against,
            # and the auto view's own key (autoview.py:485-488) reads it
            # this way too. `disabled` must not pull this row into the
            # sentinel-like tier above, or the two screens disagree on the
            # same snapshot.
            return (3,)
        if acc.disabled:
            return (2,)
        # Soonest BINDING-window recovery first, unknown last -- matches
        # the auto view's own fallback key for a row its admission pass
        # refused (autoview.py's `_candidates_text`), or the two screens
        # can list this same unranked row in two different orders. Not the
        # 7-day reset alone: the window that actually blocks an account is
        # whichever is highest, and that is routinely the 5-hour one.
        return (1,) + waiting_tail_key(
            acc.usage.last_good, models, now, settings, bars[acc.number]
        )

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
