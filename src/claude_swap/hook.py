"""``cswap hook claude`` -- usage advice for Claude Code hooks.

Claude Code can run a command on ``UserPromptSubmit``, ``PreToolUse``,
``SessionStart`` and a few other events and add what the command prints to the
model's context (``hookSpecificOutput.additionalContext``). This module turns
the usage numbers claude-swap already keeps into a short, generic piece of such
context, so the model itself knows when to ease off instead of finding out from
a rate-limit error mid-task.

Policy, in one paragraph: the *active* account is judged against two lines, one
for the 5-hour window (``--threshold``, default 80) and one for the weekly
window (``--weekly-threshold``, default 90). While it is under both, the hook
prints nothing at all, so a healthy fleet costs no context. Over a line, it
says so and recommends one of two things: another managed account that is under
all of its lines (``cswap switch N``, or let ``cswap auto`` rotate), or -- when
every usable account is over a line -- to slow down (fewer parallel subagents,
a smaller model). If the user passes ``--alternative TEXT`` that text is added
verbatim as well; it is how someone points the model at another agent or tool
for the heavy work, and it is never printed otherwise.

Everything here is a pure function over the schema-v1 payload that
``cswap list --json`` produces, so the decision is testable without a store,
and the advice only ever names an account by its number and alias, never by
email address (the text ends up in a model's context).

A hook must never get in the way of a prompt, so the command fails open: any
runtime error is swallowed (shown with ``--debug``) and the exit status is 0.
Two things deliberately exit 1 instead: a bad command line (for Claude Code a
hook that exits 2 *blocks* the event, and a typo in a flag must not do that)
and, like every other command, running as root outside a container.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from claude_swap.cache import CACHE_DIR, read_cache, write_cache

DEFAULT_THRESHOLD = 80.0
DEFAULT_WEEKLY_THRESHOLD = 90.0
DEFAULT_REPEAT_S = 600.0

#: The hook events this command answers (all of them accept ``additionalContext``). Any
#: other event gets no output: Claude Code rejects output that names a different event than
#: the one it ran for, and added context on e.g. ``Stop`` would make the model carry on.
CONTEXT_EVENTS = frozenset({"UserPromptSubmit", "PreToolUse", "PostToolUse", "SessionStart"})
DEFAULT_EVENT = "UserPromptSubmit"

#: Per-session "already told you" memory, so a long session is not nagged on
#: every prompt. Small, best-effort, safe to delete.
STATE_PATH = CACHE_DIR / "claude_hook.json"
STATE_TTL_S = 24 * 3600
STATE_MAX_SESSIONS = 200  # entries, one per session and event

_WINDOWS = (("fiveHour", "5h"), ("sevenDay", "7d"))


def _utcnow() -> datetime:
    """The clock, in one place so tests can pin it."""
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Window:
    """One usage window of one account, judged against its line."""

    label: str
    pct: float
    line: float
    resets_at: datetime | None = None

    @property
    def over(self) -> bool:
        return self.pct >= self.line


@dataclass(frozen=True)
class AccountView:
    """What the advice needs to know about one managed account."""

    number: int
    alias: str | None
    active: bool
    windows: tuple[Window, ...] = field(default_factory=tuple)

    @property
    def name(self) -> str:
        return f"#{self.number}" + (f" ({self.alias})" if self.alias else "")

    @property
    def over(self) -> tuple[Window, ...]:
        return tuple(w for w in self.windows if w.over)

    @property
    def headroom(self) -> float:
        """Room left on the fullest window (``100 - max pct``)."""
        return 100.0 - max((w.pct for w in self.windows), default=0.0)

    @property
    def relief_at(self) -> datetime | None:
        """When every over-line window of this account has reset (None if unknown)."""
        stamps = [w.resets_at for w in self.over]
        if not stamps or any(s is None for s in stamps):
            return None
        return max(stamps)  # type: ignore[type-var]


@dataclass(frozen=True)
class Assessment:
    """The verdict: ``active_high`` (another account has room) or ``all_high``."""

    level: str
    active: AccountView
    best_other: AccountView | None
    relief_at: datetime | None


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _account_view(
    row: dict, threshold: float, weekly_threshold: float, now: datetime
) -> AccountView | None:
    """One ``accounts[]`` row of the list payload -> a view, or None when the
    row carries no decision-grade usage (re-login needed, unavailable, API key)."""
    number = row.get("number")
    usage = row.get("usage")
    if row.get("usageStatus") != "ok" or not isinstance(usage, dict) or not isinstance(number, int):
        return None
    windows = []
    for key, label in _WINDOWS:
        raw = usage.get(key)
        pct = raw.get("pct") if isinstance(raw, dict) else None
        if isinstance(pct, bool) or not isinstance(pct, int | float):
            continue
        resets_at = _parse_time(raw.get("resetsAt"))
        if resets_at is not None and resets_at <= now:
            pct = 0.0  # the window has already reset; the reading is just old
        windows.append(
            Window(label, float(pct), threshold if label == "5h" else weekly_threshold, resets_at)
        )
    if not windows:
        return None
    alias = row.get("alias")
    return AccountView(
        number=number,
        alias=alias if isinstance(alias, str) and alias else None,
        active=bool(row.get("active")),
        windows=tuple(windows),
    )


def assess(
    payload: dict,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    weekly_threshold: float = DEFAULT_WEEKLY_THRESHOLD,
    now: datetime | None = None,
) -> Assessment | None:
    """Judge a ``cswap list --json`` payload. ``None`` means "nothing to say":
    the active account is under its lines, or there is no usable reading of it."""
    now = now or _utcnow()
    rows = payload.get("accounts") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return None
    views = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        view = _account_view(row, threshold, weekly_threshold, now)
        if view is not None:
            views.append((view, bool(row.get("disabled"))))
    active = next((v for v, _disabled in views if v.active), None)
    if active is None or not active.over:
        return None
    # A disabled account is held out of rotation on purpose: never suggest it.
    others = [v for v, disabled in views if not v.active and not disabled]
    clear = [v for v in others if not v.over]
    if clear:
        return Assessment("active_high", active, max(clear, key=lambda v: v.headroom), active.relief_at)
    reliefs = [v.relief_at for v in [active, *others]]
    known = [r for r in reliefs if r is not None]
    # Only claim a relief time when every account has one to offer.
    return Assessment("all_high", active, None, min(known) if len(known) == len(reliefs) else None)


def format_countdown(delta_s: float) -> str:
    """``3d 4h`` / ``2h 10m`` / ``45m`` / ``<1m`` -- the shape ``cswap list`` uses."""
    secs = int(delta_s)
    if secs < 60:
        return "<1m"
    days, rest = divmod(secs, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _in(at: datetime | None, now: datetime) -> str:
    return f" (resets in {format_countdown((at - now).total_seconds())})" if at is not None else ""


def _pct(value: float) -> str:
    return f"{value:.0f}%"


def _window_phrase(win: Window, now: datetime) -> str:
    return f"{win.label} window at {_pct(win.pct)}{_in(win.resets_at, now)}"


def advice_text(
    assessment: Assessment, *, alternative: str | None = None, now: datetime | None = None
) -> str:
    """The context line(s) for the model. English, no email addresses."""
    now = now or _utcnow()
    active = assessment.active
    over = "; ".join(_window_phrase(w, now) for w in active.over)
    if assessment.level == "active_high":
        other = assessment.best_other
        assert other is not None
        room = ", ".join(f"{w.label} {_pct(w.pct)}" for w in other.windows)
        text = (
            f"Claude usage is high on the active account {active.name}: {over}. "
            f"Account {other.name} has more room ({room}); `cswap switch {other.number}` "
            "moves to it, or let `cswap auto` rotate. Until then, keep parallel subagents "
            "and model size modest."
        )
    else:
        relief = ""
        if assessment.relief_at is not None and assessment.relief_at != active.relief_at:
            relief = (
                " Earliest relief on any account: in "
                f"{format_countdown((assessment.relief_at - now).total_seconds())}."
            )
        text = (
            f"Claude usage is high on every managed account with a usable reading. "
            f"Active account {active.name}: "
            f"{over}.{relief} Slow down: run fewer parallel subagents, prefer a smaller "
            "model, and save long autonomous runs for later."
        )
    if alternative and alternative.strip():
        text += "\n" + alternative.strip()
    return text


def _fingerprint(assessment: Assessment) -> str:
    return "|".join(
        [
            assessment.level,
            str(assessment.active.number),
            ",".join(w.label for w in assessment.active.over),
            str(assessment.best_other.number) if assessment.best_other else "-",
        ]
    )


def should_emit(
    session_id: str, fingerprint: str, repeat_s: float, *, now: float | None = None
) -> bool:
    """Throttle: say it about once per session until ``repeat_s`` passes or the situation
    changes (another level, account or window). Best effort: it remembers without a
    lock, so hooks that start at the same instant (parallel tool calls) can each speak,
    and any state trouble just means "emit"."""
    now = time.time() if now is None else now
    if repeat_s <= 0:
        return True
    try:
        state = read_cache(STATE_PATH, STATE_TTL_S)
        sessions = (
            {k: v for k, v in state.items() if isinstance(v, dict)}
            if isinstance(state, dict)
            else {}
        )
        prev = sessions.get(session_id)
        if (
            isinstance(prev, dict)
            and prev.get("fp") == fingerprint
            and isinstance(prev.get("ts"), int | float)
            and 0 <= now - prev["ts"] < repeat_s
        ):
            return False
        sessions[session_id] = {"fp": fingerprint, "ts": now}
        # A bounded map: keep the most recent entries only.
        newest = sorted(sessions.items(), key=lambda kv: kv[1].get("ts", 0), reverse=True)
        write_cache(STATE_PATH, dict(newest[:STATE_MAX_SESSIONS]))
    except Exception:  # noqa: BLE001 -- best-effort memory; never block advice
        pass
    return True


def build_output(event: str, text: str) -> dict:
    """The JSON object Claude Code expects for added context."""
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


def run(
    payload: dict,
    hook_input: dict,
    *,
    threshold: float,
    weekly_threshold: float,
    alternative: str | None,
    repeat_s: float,
) -> dict | None:
    """Payload of ``cswap list --json`` + Claude Code's hook input -> output object or None."""
    assessment = assess(payload, threshold=threshold, weekly_threshold=weekly_threshold)
    if assessment is None:
        return None
    # No event name (run by hand, older callers): behave as a prompt hook. A name this
    # command does not answer gets no output (see CONTEXT_EVENTS).
    event = hook_input.get("hook_event_name", DEFAULT_EVENT)
    if event is None:
        event = DEFAULT_EVENT
    if not isinstance(event, str) or event not in CONTEXT_EVENTS:
        return None
    session_id = hook_input.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        session_id = "-"
    # One memory per session *and* event: the heads-up at a prompt does not
    # silence the one right before a subagent is spawned.
    if not should_emit(f"{session_id}:{event}", _fingerprint(assessment), repeat_s):
        return None
    return build_output(event, advice_text(assessment, alternative=alternative))


def parse_hook_input(text: str) -> dict:
    """Claude Code sends one JSON object on stdin; anything else is treated as empty."""
    try:
        data = json.loads(text) if text.strip() else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
