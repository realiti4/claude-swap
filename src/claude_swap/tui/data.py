"""Data service for the TUI: coherent snapshots + fetch discipline.

The TUI never parses printed CLI output — it consumes
``ClaudeAccountSwitcher.accounts_snapshot`` (one collect pass, see
switcher.py) and renders structured data. This module owns *when* that pass
may hit the network, preserving the discipline the old curses watch view
established: per refresh pass, only the active account plus — at most once
per ``SERVE_TTL_S`` — the stalest due alternate is eligible to fetch, so an
open dashboard costs O(1) requests per TTL window regardless of account
count. The usage store's own gates (freshness, backoff/Retry-After, claims)
apply on top, and the TUI never writes poll plans — ``cswap auto`` stays the
cadence learner.

Everything here is blocking (file locks, keychain subprocesses, network) and
must be called from a thread worker, never the UI event loop.
"""

from __future__ import annotations

import contextlib
import io
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from claude_swap import printer, usage_store
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.models import AccountsSnapshot
from claude_swap.switcher import (
    SENTINEL_NOTES,
    ClaudeAccountSwitcher,
    last_seen_note,
)


class SnapshotSource:
    """Plans each pass's fetch set and takes one coherent snapshot.

    The first ``take()`` is a full on-demand pass (``fetch=None``, every
    stale account eligible — what a user opening the dashboard expects, and
    exactly what ``cswap list`` does); afterwards the watch discipline above
    applies. ``full=True`` (the user's explicit refresh) repeats the full
    pass; ``store_only=True`` (the auto screen, where the engine drives all
    fetching) reads the store without any network eligibility.
    """

    def __init__(self, switcher: ClaudeAccountSwitcher) -> None:
        self.switcher = switcher
        self._first_pass = True
        self._next_alt_mono = 0.0
        self._last: AccountsSnapshot | None = None

    def take(
        self, *, full: bool = False, store_only: bool = False
    ) -> AccountsSnapshot:
        """Blocking snapshot pass; call from a thread worker."""
        if store_only:
            fetch: set[str] | None = set()
        elif full or self._first_pass:
            fetch = None
            self._next_alt_mono = time.monotonic() + usage_store.SERVE_TTL_S
        else:
            fetch = self._disciplined_fetch_set()
        self._first_pass = False
        snap = self.switcher.accounts_snapshot(fetch=fetch)
        self._last = snap
        return snap

    def _disciplined_fetch_set(self) -> set[str]:
        """Active account + at most one due alternate per ``SERVE_TTL_S``.

        The alternate is picked from the *previous* snapshot's entries — a
        deliberate improvement over the old watch view, which re-read every
        credential just to nominate one. A few-second-stale nomination is
        harmless: the collector re-checks freshness/backoff/claims before
        actually fetching, so a bad pick simply fetches nothing.
        """
        active = self.switcher.current_account_number()
        fetch = {active} if active else set()
        now = time.monotonic()
        if now >= self._next_alt_mono:
            self._next_alt_mono = now + usage_store.SERVE_TTL_S
            if self._last is not None:
                candidates = [
                    acc.number
                    for acc in self._last.accounts
                    if acc.switchable and acc.number != active
                ]
                entries = {acc.number: acc.usage for acc in self._last.accounts}
                pick = usage_store.due_candidate(candidates, entries, time.time())
                if pick is not None:
                    fetch.add(pick)
        return fetch


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


def window_reset_text(last_good: dict | None, key: str, now: float) -> str | None:
    """`reset_text` for one of the top-level 5h/7d windows."""
    if not isinstance(last_good, dict):
        return None
    return reset_text(last_good.get(key), now)


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
    "SnapshotSource",
    "format_age",
    "format_duration",
    "last_seen_note",
    "reset_text",
    "run_action",
    "sentinel_label",
    "clock_stamp",
    "window_pct",
    "window_reset_text",
]
