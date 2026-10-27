"""Switch lines per usage window.

One ``autoswitch.threshold`` compared against the binding window (the max of
the 5h/7d/per-model percentages) cannot express the policy most people want:
~85-90 on the 5-hour window, which recycles in hours, and 97-99 on the weekly
ones, which are the budget. So each window gets its own line
(``autoswitch.weeklyThreshold`` for the windows that reset weekly) and the
engine judges every window against its own.

Three quantities per account, in the engine's vocabulary:

* RAW headroom (``oauth.account_headroom``, ``100 - max pct``) — physical:
  can the account serve requests, is it exhausted. Untouched by lines.
* ``line_headroom`` per window — policy distance on the 5h scale. A window at
  its line scores ``100 - threshold`` whatever its line, an exhausted one 0,
  and each side of the line is linear in between (three anchors: 0% -> 100,
  line -> 100 - threshold, 100% -> 0). With equal lines it is ``100 - pct``
  bit for bit, so every margin the engine measures in points keeps its
  meaning.
* the BINDING window — the window with the least line headroom, over-line
  windows first. Its ``line_headroom`` is the account's policy headroom and
  its ``over`` the account's verdict. With equal lines it is today's max-pct
  window.

Pure functions over the ``(label, pct, resets_at)`` triples
``oauth.relevant_windows`` yields: no settings reads, no I/O, and no restated
percentages — the raw numbers flow to logs and screens untouched, and the
lines are applied only where the engine compares.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from claude_swap.oauth import FIVE_HOUR_LABEL


def window_line(label: str, threshold: float, weekly_threshold: float | None) -> float:
    """The switch line for one window: ``weekly_threshold`` for every window
    that resets weekly (7d and per-model), ``threshold`` for the 5h window and
    for all of them while no weekly line is configured."""
    if weekly_threshold is None or label == FIVE_HOUR_LABEL:
        return threshold
    return weekly_threshold


def line_headroom(pct: float, line: float, threshold: float) -> float:
    """Policy headroom of one window on the 5h scale (module docstring).

    The arrangement is load-bearing for exactness at the anchors the engine
    compares at: ``line - pct`` is exactly 0.0 at the line, so a window AT its
    line scores exactly ``100 - threshold`` and a candidate sitting exactly
    ``hysteresis_pct`` better is not refused over a 1e-14 residue (measured:
    ``(line - pct) * (threshold / line)`` left one for 15 of the integer
    line pairs); ``100 - pct`` is exactly 0.0 at 100%. Equal lines
    short-circuit to ``100 - pct`` so the default configuration is
    bit-identical to raw headroom.
    """
    if line == threshold:
        return 100.0 - pct
    if pct <= line:
        return (100.0 - threshold) + threshold * (line - pct) / line
    return (100.0 - threshold) * (100.0 - pct) / (100.0 - line)


@dataclass(frozen=True)
class BindingWindow:
    """The window holding an account back, with the numbers the engine
    compares: its raw ``pct`` and its own ``line`` (so a message can quote the
    comparison actually made — "7d 93% < 97%"), ``resets_at`` (recovery
    scheduling) and ``line_headroom`` (the account's policy headroom)."""

    label: str
    pct: float
    line: float
    resets_at: str | None
    line_headroom: float

    @property
    def over(self) -> bool:
        """At or over its own line — compared directly on the raw pct, never
        through a rescaled value or a quotient against 1.0, so the verdict
        cannot drift by a rounding ulp at the line."""
        return self.pct >= self.line


def binding_window(
    windows: Iterable[tuple[str, float, str | None]],
    threshold: float,
    weekly_threshold: float | None = None,
    *,
    scale: float | None = None,
) -> BindingWindow | None:
    """The window with the least policy headroom, over-line windows first;
    ties keep the first in ``relevant_windows`` order (5h, 7d, then scoped).
    ``None`` when the account reports no window.

    Over-line first makes ``binding.over == any(window at/over its line)``
    hold by construction, so the trigger, the landing gate and the all-above
    census — which all read the binding window — can never disagree with a
    per-window check. The ordering is the SAME one the engine ranks and
    applies hysteresis on: choosing the window by one score and ranking by
    another made 0.1 more usage look healthier whenever the choice flipped.

    ``scale`` is the threshold the headroom is measured on, ``T`` in
    ``line_headroom``; it defaults to ``threshold``. An account on a line of
    its own passes that line as ``threshold`` and the global threshold as
    ``scale``, so accounts on different lines are scored on one ruler and
    their headroom compares directly. Each window's line and ``over`` do
    not depend on it. In exact arithmetic neither does the window chosen,
    but at a floating-point tie the rounding does, so every caller that
    selects a window for one account must pass the same ``scale``.
    """
    best: BindingWindow | None = None
    best_key: tuple[bool, float] | None = None
    ruler = threshold if scale is None else scale
    for label, pct, resets_at in windows:
        line = window_line(label, threshold, weekly_threshold)
        window = BindingWindow(
            label, pct, line, resets_at, line_headroom(pct, line, ruler)
        )
        key = (not window.over, window.line_headroom)
        if best_key is None or key < best_key:
            best, best_key = window, key
    return best
