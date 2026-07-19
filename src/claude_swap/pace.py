"""Weekly usage pace (issue #125).

A weekly window is "ahead of pace" when the account has used more of it than
the fraction of the weekly reset cycle that has elapsed so far — e.g. 40% used
at the 20%-through-the-week mark. Applies only to weekly windows (``seven_day``
and every ``scoped`` per-model window); the 5h window is excluded because it
resets too fast for pace to mean anything (it starts "ahead of pace" almost by
definition early in the window, and the store's poll floor of ~3 minutes,
drifting to ~10 minutes when idle, is a large fraction of a 5h window but
negligible against a week — see issue #125 discussion).

``compute_pace`` is a pure function: it consumes an already-fetched window
dict and the snapshot's ``fetched_at``, never fetches anything itself, and has
no influence on poll cadence (that stays entirely in ``poll_policy``). Elapsed
time is measured against ``fetched_at`` rather than wall-clock ``now()``, so a
snapshot served stale (last-good data re-served after a failed refetch) is
evaluated against the clock it was actually measured at.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# Weekly windows reset on a fixed 7-day cadence (matches menubar._WEEKLY_PERIOD_S).
WEEKLY_PERIOD_S = 7 * 86400.0

# Suppress the marker for this long after a weekly reset. Right after reset,
# elapsed is tiny so `expected_pct` is near zero and almost any usage reads as
# "far ahead" — a false positive, not a genuine pace warning. A plain
# `elapsed == 0` guard isn't enough since a snapshot fetched shortly after
# reset already has nonzero (if small) elapsed time.
SUPPRESS_AFTER_RESET_S = 24 * 3600.0

# Minimum (actual - expected) percentage-point gap before showing a marker.
# Below this, "ahead of pace" is within normal usage variance and would just
# add noise to already-dense usage rows.
AHEAD_THRESHOLD_PCT = 15.0


@dataclass(frozen=True)
class PaceResult:
    """One weekly window's pace at the moment its snapshot was fetched."""

    expected_pct: float  # % of the week's budget "on schedule" usage would be at
    actual_pct: float  # the window's actual pct
    elapsed_s: float  # time since this window's current cycle started
    period_s: float  # the window's full cycle length (e.g. 7 days)
    ahead: bool  # actual_pct - expected_pct >= the "meaningfully ahead" threshold


def _resets_at_ts(resets_at: object) -> float | None:
    """POSIX timestamp of a ``resets_at`` ISO string, or None if missing/unparseable."""
    if not isinstance(resets_at, str):
        return None
    try:
        return datetime.fromisoformat(resets_at).timestamp()
    except ValueError:
        return None


def compute_pace(
    window: dict | None,
    *,
    fetched_at: float | None,
    period_s: float = WEEKLY_PERIOD_S,
    suppress_after_reset_s: float = SUPPRESS_AFTER_RESET_S,
    ahead_threshold_pct: float = AHEAD_THRESHOLD_PCT,
) -> PaceResult | None:
    """Pace for one weekly usage window, or None when pace isn't computable/meaningful.

    ``window`` is a raw window dict (``{"pct": ..., "resets_at": ...}``) as
    stored in ``UsageEntry.last_good`` — the *next* reset, not the current
    window's start, since that's the only timestamp the usage API provides.
    The current window's start is derived by rolling ``resets_at`` by whole
    ``period_s`` increments until it lands at or before ``fetched_at``; this
    is correct however many cycles old ``resets_at`` is, including a
    not-yet-rolled-forward stale value.

    Returns None when: the window is missing/unparseable, ``pct``/``resets_at``
    aren't usable, or elapsed time since the window's start is inside
    ``suppress_after_reset_s``.
    """
    if not isinstance(window, dict) or fetched_at is None:
        return None
    pct = window.get("pct")
    if not isinstance(pct, (int, float)):
        return None
    next_reset = _resets_at_ts(window.get("resets_at"))
    if next_reset is None:
        return None

    # (next_reset - fetched_at) mod period_s == time remaining until the next
    # reset, folded into [0, period_s). period_s minus that is elapsed time
    # since the current window started — works regardless of how many whole
    # cycles next_reset is ahead of or behind fetched_at.
    remaining = (next_reset - fetched_at) % period_s
    elapsed = 0.0 if remaining == 0 else period_s - remaining

    if elapsed < suppress_after_reset_s:
        return None

    expected_pct = min(100.0, (elapsed / period_s) * 100.0)
    ahead = (float(pct) - expected_pct) >= ahead_threshold_pct
    return PaceResult(
        expected_pct=expected_pct,
        actual_pct=float(pct),
        elapsed_s=elapsed,
        period_s=period_s,
        ahead=ahead,
    )


def projected_exhaustion_ts(pace: PaceResult, *, fetched_at: float) -> float | None:
    """Linear-projection ETA (POSIX timestamp) for when usage would hit 100%.

    JSON-only per issue #125: the projection assumes a constant burn rate,
    which has wide error bars against real, bursty usage, and would look
    falsely precise if surfaced in the UI. Returns None when there's no
    measurable rate (no elapsed time, or usage isn't climbing).
    """
    if pace.elapsed_s <= 0 or pace.actual_pct <= 0:
        return None
    rate_pct_per_s = pace.actual_pct / pace.elapsed_s
    if rate_pct_per_s <= 0:
        return None
    remaining_pct = 100.0 - pace.actual_pct
    if remaining_pct <= 0:
        return fetched_at
    return fetched_at + remaining_pct / rate_pct_per_s


def will_last_to_reset(pace: PaceResult) -> bool | None:
    """Whether, at the current burn rate, usage stays under 100% through reset.

    JSON-only (like ``projected_exhaustion_ts``): a boolean answer to "should I
    worry" is safe to expose even though it's built from the same linear-rate
    assumption, but it's still a projection with the same wide error bars
    against bursty real usage, so it stays out of every human-facing surface.
    Returns None when there's no measurable rate to extrapolate from.
    """
    if pace.actual_pct <= 0:
        return True  # no usage yet — nothing to run out of before reset
    if pace.elapsed_s <= 0:
        return None
    rate_pct_per_s = pace.actual_pct / pace.elapsed_s
    if rate_pct_per_s <= 0:
        return None
    projected_total_pct = pace.actual_pct + rate_pct_per_s * (pace.period_s - pace.elapsed_s)
    return projected_total_pct <= 100.0
