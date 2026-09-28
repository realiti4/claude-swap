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
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from claude_swap import oauth, printer, usage_store
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.snapshot_source import SnapshotSource
from claude_swap.switcher import SENTINEL_NOTES, last_seen_note


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


REFETCHING = "refetching"


def reset_text(
    window: dict | None,
    now: float,
    fetched_at: float | None = None,
    entry: usage_store.UsageEntry | None = None,
) -> str | None:
    """Live countdown to one window's reset ("resets 2h 13m"), if known.

    Computed from ``resets_at`` at render time — the countdown the API sent
    was correct at *fetch* time and drifts as the measurement ages.

    ``fetched_at``, when given, catches the window between the reset firing
    and the next refetch landing: once ``resets_at`` has passed, the pct
    beside this text is only fresh if it was MEASURED after the reset too.
    ``fetched_at < resets_at <= now`` proves it wasn't, so that state needs
    naming instead of asserting "resets now" beside a pct that provably
    predates it (#325). Without ``fetched_at`` nothing can be proven either
    way, so the elapsed reading is unchanged.

    That "needs naming" state is NOT itself "refetching" — a fetch pending
    forever is a bug, not a display. ``entry``, when given, tells the two
    apart: "refetching" only while ``entry.claimed(now)`` says a fetch is
    actually in flight (bounded by ``CLAIM_TTL_S``); otherwise the row names
    what it is really waiting for — a retry instant from ``next_poll_at``,
    or the backoff reason and retry from ``backoff_until``/``last_error``
    (#325 follow-up: the placeholder was found resting unqualified across
    unchanged frames, never decaying, retrying or counting). Without
    ``entry`` there is no way to tell a live claim from a stalled one, so
    "refetching" stands as the conservative guess — every production
    caller passes one.
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
        if fetched_at is not None and fetched_at < ts:
            return _stale_reset_text(entry, now)
        return "resets now"
    return f"resets {format_duration(remaining)}"


def _stale_reset_text(entry: usage_store.UsageEntry | None, now: float) -> str:
    """What names a window whose reset fired before the served pct was
    measured (see ``reset_text``): the placeholder only while a fetch is
    genuinely claimed, a retry instant when one is scheduled, or the
    backoff reason and retry when the last attempt failed."""
    if entry is not None:
        if entry.claimed(now):
            return REFETCHING
        if entry.in_backoff(now):
            return f"{_reason_word(entry.last_error)} {_short_wait(entry.backoff_until - now)}"
        if entry.next_poll_at is not None:
            if entry.next_poll_at <= now:
                # Past due and unclaimed: `_short_wait` would clamp a
                # negative remainder to "0s" forever, the same unqualified
                # resting placeholder this range removed, just relabeled.
                return "overdue"
            return f"retry {_short_wait(entry.next_poll_at - now)}"
    return REFETCHING


_ERROR_WORDS = {"http-429": "429"}


def _reason_word(last_error: str | None) -> str:
    """A short backoff-reason code — kept to a few characters so ``reason
    retry`` stays inside REFETCHING's width (see ``_short_wait``)."""
    if last_error is None:
        return "err"
    return _ERROR_WORDS.get(last_error, "err")


def _short_wait(seconds: float) -> str:
    """Single-unit duration for a retry marker (``"45s"``, ``"12m"``), kept
    short enough that ``"retry "`` + this never exceeds REFETCHING's width
    (10 chars, #325 follow-up) — the fixed-width chip column is PR #323's
    business, not this one's."""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


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


def chip_label(label: str, reset: str | None, pct: float | None = None) -> str:
    """The reading for one window, without its percentage: ``5h(⟳2h28m)``.

    THE one place that decides how a window reads — the dashboard's inactive
    rows and the auto view's Next-best rows both draw it, so one account
    cannot read two ways on two screens. The caller appends the pct so it can
    colour it by severity. The countdown shows whenever it is known, not only
    at 100%: a saturated candidate's worth IS when it comes back.

    A known countdown renders in one fixed-width shape, zero-padded, so the
    ``:`` before the pct lands on the same column across rows whose raw
    countdowns differ in width (owner, 2026-09-15): under a day AND under 10
    hours, ``{h}h{mm}m`` (``resets 2h 4m`` → ``2h04m``, ``resets 2h`` →
    ``2h00m``); a day or more, OR 10-23 hours with no day component, the
    day-plus shape ``{d}d{hh}h`` (``resets 3d 4h`` → ``3d04h``, ``resets 3d``
    → ``3d00h``, ``resets 14h 4m`` → ``0d14h``, dropping the minute the same
    way a day-plus reading already does). Routing a two-digit hour through
    the day shape instead of zero-padding the hour digit itself keeps every
    reading 5 wide for any window the API serves today (none past 7d)
    without ever writing ``02h04m`` or ``03d04h`` — neither of which any
    caller or test expects. ``resets now``, ``refetching`` and the
    retry/backoff markers ``reset_text`` names a rolled-but-unclaimed window
    with (``"retry 5m"``, ``"429 2m"``, #325 follow-up) all keep their own
    words: only a string that IS a plain duration (``\\d+[dhms]``, optionally
    two of them) gets zero-padded, so a new marker never needs adding here.

    An unknown reset is a fact worth showing, not a reason to go blank: the
    strategy needs exactly this account activated once to learn it (see
    autoswitch.py's consume-first probe admission), so hiding the gap read as
    "nothing to report" when it meant the opposite. ``?`` keeps the same
    token shape a known reset has (``5h(⟳?):``) so a column of chips still
    lines up — callers compute width from this string, never a literal.

    The one exception: a 5h window with no reported reset AND no usage
    (``pct == 0``, not merely falsy — ``None`` from a caller that never
    passes it must not match) has nothing withheld, the whole window is
    what's left, so it reads its own full duration instead of ``⟳?``. A 5h
    window WITH usage but no reported reset is live and its reset really
    was withheld, and any other window (7d, a scoped model) always keeps
    the plain unknown-reset marker; #325 is what resolves those.
    """
    if not reset:
        return "5h(⟳5h00m):" if label == "5h" and pct == 0 else f"{label}(⟳?):"
    countdown = reset.removeprefix("resets ")
    if re.fullmatch(r"\d+[dhms](?:\s\d+[dhms])?", countdown):
        units = {unit: num for num, unit in re.findall(r"(\d+)([dhms])", countdown)}
        days = int(units.get("d", 0))
        hours = int(units.get("h", 0))
        countdown = (
            f"{days}d{hours:02d}h"
            if days or hours >= 10
            else f"{hours}h{int(units.get('m', 0)):02d}m"
        )
    return f"{label}(⟳{countdown}):"


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


__all__ = [
    "ActionResult",
    "REFETCHING",
    "SnapshotSource",
    "format_age",
    "format_duration",
    "last_seen_note",
    "reset_clock",
    "reset_text",
    "run_action",
    "sentinel_label",
    "clock_stamp",
    "window_pct",
    "window_reset_text",
]
