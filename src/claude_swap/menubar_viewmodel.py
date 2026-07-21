"""Pure, import-safe presentation models for the macOS menu-bar surface.

This module intentionally contains no AppKit, rumps, switcher actions, account
mutation, credential access, or network work. It only shapes an already-collected
:class:`~claude_swap.models.AccountsSnapshot` and formats its store-backed usage
measurements. ``MenuBarSettings.load`` and ``save`` are the sole filesystem
operations, because display preferences are owned by the menu bar.

The typed popover models are the production boundary for a native compact account
popover. They preserve a live sentinel over its last-known-good measurement,
freshness, reset-time roll-forward, scoped limits, spend, and weekly pace state
without making a second fetch or mutating the source snapshot.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import TypeAlias

from claude_swap import pace
from claude_swap.models import AccountsSnapshot
from claude_swap.switcher import SENTINEL_NOTES
from claude_swap.usage_store import SERVE_TTL_S, STALE_OK_S, UsageEntry

ICON = "⇄"
REFRESH_CHOICES: tuple[int, ...] = (30, 60, 300)
AUTO_THRESHOLD_CHOICES: tuple[int, ...] = (80, 90, 95, 98)
TITLE_PCT_CHOICES: tuple[str, ...] = ("off", "5h", "7d", "both")
SWITCH_HISTORY_LIMIT = 10
_WEEKLY_PERIOD_S = 7 * 86400

UsagePayload: TypeAlias = dict[str, object]
DisplayUsage: TypeAlias = UsagePayload | str | None


@dataclass
class MenuBarSettings:
    """User-configurable menu bar display behavior, persisted as JSON.

    Only display preferences and the auto-switch on/off toggle live here.
    Auto-switch policy belongs to :mod:`claude_swap.settings`, so the CLI and
    the menu bar share one policy source of truth.
    """

    show_account_name: bool = True
    title_pct: str = "both"
    title_scoped: bool = False
    refresh_interval: int = 60
    auto_switch_enabled: bool = False

    @classmethod
    def load(cls, path: Path) -> MenuBarSettings:
        """Load settings, falling back to defaults on any problem.

        Unknown keys are ignored; a value whose type does not match its field
        default is discarded. A missing or unreadable file yields defaults.
        """
        defaults = cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return defaults
        if not isinstance(raw, dict):
            return defaults
        kwargs: dict[str, object] = {}
        for field in fields(cls):
            if field.name in raw and isinstance(raw[field.name], type(getattr(defaults, field.name))):
                kwargs[field.name] = raw[field.name]
        return cls(**kwargs)

    def save(self, path: Path) -> None:
        """Write settings as pretty JSON, creating parent directories."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


class CapacityState(StrEnum):
    """Semantic capacity state for a usage row, independent of its renderer."""

    AVAILABLE = "available"
    NEAR_LIMIT = "near_limit"
    LIMIT_REACHED = "limit_reached"
    UNAVAILABLE = "unavailable"


class FreshnessState(StrEnum):
    """Whether an account has a current, stale, or absent measurement."""

    FRESH = "fresh"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


class UsageScope(StrEnum):
    """The quota kind represented by a compact popover row."""

    SPEND = "spend"
    FIVE_HOUR = "five_hour"
    SEVEN_DAY = "seven_day"
    SCOPED = "scoped"


@dataclass(frozen=True)
class UsageRowViewModel:
    """One compact, renderer-neutral usage row for an account popover."""

    label: str
    scope: UsageScope
    used_percent: float
    available_percent: float
    reset_text: str | None
    state: CapacityState
    state_label: str
    ahead_of_pace: bool = False
    limit_reached: bool = False
    amount_text: str | None = None


@dataclass(frozen=True)
class PopoverAccountViewModel:
    """Immutable compact presentation state for one managed account."""

    number: str
    email: str
    alias: str
    display_name: str
    is_active: bool
    disabled: bool
    freshness: FreshnessState
    freshness_detail: str
    sentinel: str | None
    sentinel_note: str | None
    has_last_good: bool
    capacity_summary: str
    rows: tuple[UsageRowViewModel, ...]
    session_available: bool


@dataclass(frozen=True)
class MenuBarPopoverViewModel:
    """Immutable complete account view for a compact menu-bar popover."""

    accounts: tuple[PopoverAccountViewModel, ...]
    active_number: str | None


# ---- Formatting helpers ------------------------------------------------------

def tightest_pct(usage: dict | str | None) -> float | None:
    """Highest 5h/7d utilization percentage, or ``None`` if unknown.

    Spend is deliberately excluded because it is not a rate-limit window.
    """
    if not isinstance(usage, dict):
        return None
    pcts = [
        window["pct"]
        for window in (usage.get("five_hour"), usage.get("seven_day"))
        if isinstance(window, dict) and isinstance(window.get("pct"), (int, float))
    ]
    return max(pcts) if pcts else None


def _window_pct(usage: dict | str | None, key: str) -> float | None:
    """Utilization pct for a named usage window, or ``None``."""
    if isinstance(usage, dict):
        window = usage.get(key)
        if isinstance(window, dict) and isinstance(window.get("pct"), (int, float)):
            return float(window["pct"])
    return None


def _resets_at_ts(window: dict | str | None) -> float:
    """POSIX timestamp of a window's ``resets_at``; infinity if missing/bad."""
    if isinstance(window, dict):
        resets_at = window.get("resets_at")
        if isinstance(resets_at, str):
            try:
                return datetime.fromisoformat(resets_at).timestamp()
            except ValueError:
                pass
    return float("inf")


def _live_countdown(window: dict | str | None, now: float) -> str | None:
    """Compute a live countdown from ``resets_at``, never a cached countdown."""
    timestamp = _resets_at_ts(window)
    if timestamp == float("inf"):
        return None
    remaining = int(timestamp - now)
    if remaining <= 0:
        return None
    days, remainder = divmod(remaining, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _rolled_weekly_window(window: dict | None, now: float) -> dict | None:
    """Roll a passed weekly reset to its next fixed seven-day boundary.

    The returned copy is display-only. It never changes the source cache or
    snapshot: ``pct`` becomes 0 and stale countdown/clock strings are removed.
    """
    if not isinstance(window, dict):
        return window
    timestamp = _resets_at_ts(window)
    if timestamp == float("inf") or timestamp > now:
        return window
    missed = int((now - timestamp) // _WEEKLY_PERIOD_S) + 1
    next_timestamp = timestamp + missed * _WEEKLY_PERIOD_S
    rolled = dict(window)
    rolled["pct"] = 0.0
    rolled["resets_at"] = datetime.fromtimestamp(next_timestamp, tz=timezone.utc).isoformat()
    rolled.pop("countdown", None)
    rolled.pop("clock", None)
    return rolled


def usage_summary(
    usage: dict | str | None, now: float | None = None, fetched_at: float | None = None
) -> str:
    """One-line account summary, including live reset and weekly pace markers."""
    if isinstance(usage, str):
        return usage
    if usage is None:
        return "usage unavailable"
    if now is None:
        now = time.time()
    parts: list[str] = []
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        window = usage.get(key)
        pace_result = None
        if key == "seven_day":
            window = _rolled_weekly_window(window, now)
            pace_result = pace.compute_pace(window, fetched_at=fetched_at)
        if isinstance(window, dict) and isinstance(window.get("pct"), (int, float)):
            segment = f"{label} {window['pct']:.0f}%"
            if key == "seven_day" and pace_result and pace_result.ahead:
                segment += " (ahead)"
            countdown = _live_countdown(window, now)
            if countdown:
                segment += f" ({countdown})"
            parts.append(segment)
    scoped = usage.get("scoped")
    if isinstance(scoped, list):
        for raw_window in scoped:
            window = _rolled_weekly_window(raw_window if isinstance(raw_window, dict) else None, now)
            pace_result = pace.compute_pace(window, fetched_at=fetched_at)
            if isinstance(window, dict) and isinstance(window.get("pct"), (int, float)) and window.get("name"):
                segment = f"{window['name']} {window['pct']:.0f}%"
                if window["pct"] >= 100:
                    segment += " (!)"
                elif pace_result and pace_result.ahead:
                    segment += " (ahead)"
                countdown = _live_countdown(window, now)
                if countdown:
                    segment += f" ({countdown})"
                parts.append(segment)
    spend = usage.get("spend")
    if isinstance(spend, dict) and isinstance(spend.get("pct"), (int, float)):
        parts.append(f"$ {spend['pct']:.0f}%")
    return " · ".join(parts) if parts else "usage unavailable"


def format_account_label(
    num: object,
    email: str,
    usage: dict | str | None,
    now: float | None = None,
    alias: str | None = None,
    disabled: bool = False,
    fetched_at: float | None = None,
) -> str:
    """Build one account row's menu label."""
    label = f"{alias}  ({email})" if alias else email
    marker = "  (disabled)" if disabled else ""
    return f"{num}  {label}{marker}  {usage_summary(usage, now, fetched_at)}"


def _local_part(email: str, limit: int = 12) -> str:
    """Email text before '@', truncated with a trailing '*' marker."""
    local = email.split("@", 1)[0]
    if len(local) > limit:
        return local[: limit - 1] + "*"
    return local


def format_title(
    active_email: str | None,
    active_usage: dict | str | None,
    settings: MenuBarSettings,
    now: float | None = None,
    alias: str | None = None,
) -> str:
    """Build the menu-bar title from the active account and settings."""
    if active_email is None:
        return ICON
    if now is None:
        now = time.time()
    segments: list[str] = []
    if settings.show_account_name:
        segments.append(alias if alias else _local_part(active_email))
    if settings.title_pct in ("5h", "both"):
        pct = _window_pct(active_usage, "five_hour")
        if pct is not None:
            segments.append(f"{pct:.0f}%")
    if settings.title_pct in ("7d", "both"):
        seven_day = active_usage.get("seven_day") if isinstance(active_usage, dict) else None
        seven_day = _rolled_weekly_window(seven_day, now)
        pct = seven_day["pct"] if isinstance(seven_day, dict) and isinstance(seven_day.get("pct"), (int, float)) else None
        if pct is not None:
            segments.append(f"{pct:.0f}%")
    if settings.title_scoped and isinstance(active_usage, dict):
        scoped = active_usage.get("scoped")
        if isinstance(scoped, list):
            for raw_window in scoped:
                window = _rolled_weekly_window(raw_window if isinstance(raw_window, dict) else None, now)
                if isinstance(window, dict) and isinstance(window.get("pct"), (int, float)) and window.get("name"):
                    segments.append(f"{window['name']} {window['pct']:.0f}%")
    if not segments:
        return ICON
    return f"{ICON} " + " · ".join(segments)


def format_usage_log(email: str, usage: dict | str | None) -> str | None:
    """Format a static log record for numeric 5h/7d measurements only."""
    parts: list[str] = []
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        pct = _window_pct(usage, key)
        if pct is None:
            continue
        window = usage.get(key) if isinstance(usage, dict) else None
        clock = window.get("clock") if isinstance(window, dict) else None
        segment = f"{label} {pct:.0f}%"
        if clock:
            segment += f" (resets {clock})"
        parts.append(segment)
    if not parts:
        return None
    return f"usage {email}: " + " · ".join(parts)


def _usage_log_key(usage: dict | str | None) -> tuple[float | None, float | None]:
    """De-duplication key for usage logging: 5h and 7d percentages only."""
    return (_window_pct(usage, "five_hour"), _window_pct(usage, "seven_day"))


_SWITCH_LOG_RE = re.compile(r"Switched from account (\d+) to (\d+)")


def parse_switch_history(log_text: str, limit: int = SWITCH_HISTORY_LIMIT) -> list[str]:
    """Return recent switch-log entries, newest first, in compact menu form."""
    entries: list[str] = []
    for line in log_text.splitlines():
        match = _SWITCH_LOG_RE.search(line)
        if not match:
            continue
        stamp = line.split(" - ", 1)[0].strip()[:16]
        entries.append(f"{match.group(1)} → {match.group(2)}   {stamp}")
    return entries[-limit:][::-1]


# ---- Snapshot adaptation -----------------------------------------------------

def _account_display_usage(entry: UsageEntry) -> DisplayUsage:
    """Display usage: a sentinel note, or its last-good measurement."""
    if entry.sentinel:
        return SENTINEL_NOTES.get(entry.sentinel, entry.sentinel)
    return entry.last_good


def _format_duration(seconds: float) -> str:
    """Compact duration for freshness labels."""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m"
    if total < 86400:
        hours, minutes = divmod(total // 60, 60)
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(total // 3600, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def _freshness(entry: UsageEntry) -> tuple[FreshnessState, str]:
    """Derive renderer-neutral freshness without changing store semantics."""
    if entry.last_good is None:
        return FreshnessState.UNAVAILABLE, "Usage unavailable"
    if entry.age_s is None:
        return FreshnessState.STALE, "Last updated unknown"
    if entry.age_s <= STALE_OK_S:
        detail = "Updated just now" if entry.age_s <= SERVE_TTL_S else f"Updated {_format_duration(entry.age_s)} ago"
        return FreshnessState.FRESH, detail
    return FreshnessState.STALE, f"Last updated {_format_duration(entry.age_s)} ago"


def _capacity_state(percent: float) -> CapacityState:
    if percent >= 90:
        return CapacityState.LIMIT_REACHED
    if percent >= 70:
        return CapacityState.NEAR_LIMIT
    return CapacityState.AVAILABLE


def _state_label(state: CapacityState) -> str:
    return {
        CapacityState.AVAILABLE: "Available",
        CapacityState.NEAR_LIMIT: "Near limit",
        CapacityState.LIMIT_REACHED: "Limit reached",
        CapacityState.UNAVAILABLE: "Unavailable",
    }[state]


def _spend_amount_text(window: dict) -> str | None:
    used, limit = window.get("used"), window.get("limit")
    if not isinstance(used, (int, float)) or not isinstance(limit, (int, float)):
        return None
    currency = window.get("currency")
    prefix = "$" if currency in (None, "USD") else f"{currency} "
    return f"{prefix}{used:,.2f} / {prefix}{limit:,.2f}"


def _usage_row(
    label: str,
    scope: UsageScope,
    window: dict,
    now: float,
    fetched_at: float | None,
) -> UsageRowViewModel | None:
    """Shape one valid raw window into an immutable compact row."""
    raw_percent = window.get("pct")
    if not isinstance(raw_percent, (int, float)):
        return None
    percent = float(raw_percent)
    is_weekly = scope in (UsageScope.SEVEN_DAY, UsageScope.SCOPED)
    pace_result = pace.compute_pace(window, fetched_at=fetched_at) if is_weekly else None
    limit_reached = scope is UsageScope.SCOPED and percent >= 100
    state = _capacity_state(percent)
    return UsageRowViewModel(
        label=label,
        scope=scope,
        used_percent=percent,
        available_percent=max(0.0, 100.0 - percent),
        reset_text=_live_countdown(window, now),
        state=state,
        state_label=_state_label(state),
        ahead_of_pace=bool(pace_result and pace_result.ahead) and not limit_reached,
        limit_reached=limit_reached,
        amount_text=_spend_amount_text(window) if scope is UsageScope.SPEND else None,
    )


def _popover_rows(entry: UsageEntry, now: float) -> tuple[UsageRowViewModel, ...]:
    """Shape last-good data, rolling only weekly/scoped display windows."""
    usage = entry.last_good
    if not isinstance(usage, dict):
        return ()
    rows: list[UsageRowViewModel] = []
    spend = usage.get("spend")
    if isinstance(spend, dict):
        row = _usage_row("Spend", UsageScope.SPEND, spend, now, entry.fetched_at)
        if row is not None:
            rows.append(row)
    for key, label, scope in (
        ("five_hour", "5h", UsageScope.FIVE_HOUR),
        ("seven_day", "7d", UsageScope.SEVEN_DAY),
    ):
        raw_window = usage.get(key)
        if not isinstance(raw_window, dict):
            continue
        window = _rolled_weekly_window(raw_window, now) if scope is UsageScope.SEVEN_DAY else raw_window
        if window is None:
            continue
        row = _usage_row(label, scope, window, now, entry.fetched_at)
        if row is not None:
            rows.append(row)
    scoped = usage.get("scoped")
    if isinstance(scoped, list):
        for raw_window in scoped:
            if not isinstance(raw_window, dict):
                continue
            name = raw_window.get("name")
            if not isinstance(name, str) or not name:
                continue
            window = _rolled_weekly_window(raw_window, now)
            if window is None:
                continue
            row = _usage_row(name, UsageScope.SCOPED, window, now, entry.fetched_at)
            if row is not None:
                rows.append(row)
    return tuple(rows)


def _capacity_summary(rows: tuple[UsageRowViewModel, ...]) -> str:
    """The tightest quota headroom for a compact account header.

    Spend is shown as a budget row, not a rate-limit window, so it cannot make
    an account's capacity summary look constrained.
    """
    quota_rows = tuple(row for row in rows if row.scope is not UsageScope.SPEND)
    if not quota_rows:
        return "Capacity unavailable"
    available = min(row.available_percent for row in quota_rows)
    return f"{available:.0f}% minimum capacity"


def popover_view_model(
    snapshot: AccountsSnapshot, now: float | None = None
) -> MenuBarPopoverViewModel:
    """Adapt an already-collected snapshot to immutable compact popover models.

    This is a pure transformation. It retains sentinel state separately from
    the rows built from last-good data, allowing a UI to show both the current
    problem and the last measurement without pretending a sentinel erased it.
    """
    if now is None:
        now = time.time()
    accounts: list[PopoverAccountViewModel] = []
    for account in snapshot.accounts:
        freshness, freshness_detail = _freshness(account.usage)
        alias = account.alias
        display_name = alias if alias else _local_part(account.email)
        sentinel = account.usage.sentinel
        rows = _popover_rows(account.usage, now)
        accounts.append(
            PopoverAccountViewModel(
                number=account.number,
                email=account.email,
                alias=alias,
                display_name=display_name,
                is_active=account.is_active,
                disabled=account.disabled,
                freshness=freshness,
                freshness_detail=freshness_detail,
                sentinel=sentinel,
                sentinel_note=SENTINEL_NOTES.get(sentinel, sentinel) if sentinel else None,
                has_last_good=account.usage.last_good is not None,
                capacity_summary=_capacity_summary(rows),
                rows=rows,
                # The active default profile already owns this account's refresh
                # token. A second profile can drift after token rotation, so only
                # inactive OAuth accounts may start an isolated session.
                session_available=account.kind == "oauth" and not account.is_active,
            )
        )
    return MenuBarPopoverViewModel(tuple(accounts), snapshot.active_number)


EMPTY_SNAPSHOT: dict[str, object] = {
    "accounts": [],
    "active_email": None,
    "active_usage": None,
    "active_alias": None,
}


def _adapt_snapshot(snapshot: AccountsSnapshot) -> dict[str, object]:
    """Legacy render-dict adapter retained for existing menu-bar consumers.

    New native popover code should use :func:`popover_view_model`. This helper
    remains intentionally pure and maintains its previous tuple/dict shape.
    """
    accounts: list[tuple[object, ...]] = []
    active_email: str | None = None
    active_usage: DisplayUsage = None
    active_alias: str | None = None
    for account in snapshot.accounts:
        display = _account_display_usage(account.usage)
        accounts.append(
            (
                account.number,
                account.email,
                account.is_active,
                display,
                account.usage.last_good,
                account.alias,
                account.disabled,
                account.usage.fetched_at,
            )
        )
        if account.is_active:
            active_email, active_usage, active_alias = account.email, display, account.alias
    return {
        "accounts": accounts,
        "active_email": active_email,
        "active_usage": active_usage,
        "active_alias": active_alias,
    }


__all__ = [
    "AUTO_THRESHOLD_CHOICES",
    "CapacityState",
    "DisplayUsage",
    "EMPTY_SNAPSHOT",
    "FreshnessState",
    "ICON",
    "MenuBarPopoverViewModel",
    "MenuBarSettings",
    "PopoverAccountViewModel",
    "REFRESH_CHOICES",
    "SWITCH_HISTORY_LIMIT",
    "TITLE_PCT_CHOICES",
    "UsageRowViewModel",
    "UsageScope",
    "_account_display_usage",
    "_adapt_snapshot",
    "_live_countdown",
    "_local_part",
    "_resets_at_ts",
    "_rolled_weekly_window",
    "_usage_log_key",
    "_window_pct",
    "format_account_label",
    "format_title",
    "format_usage_log",
    "popover_view_model",
    "tightest_pct",
    "usage_summary",
]
