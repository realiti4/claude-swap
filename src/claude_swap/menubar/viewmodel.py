"""Pure view-model transform for the menubar panel (menubar v2).

Everything here is import-safe on every platform — no PyObjC, no I/O, no
network. ``build()`` turns an ``AccountsSnapshot`` (plus auto-switch
settings and switch history) into the additive schemaVersion-1 JSON the
WKWebView panel renders; the status-item title and log formatting helpers
the legacy rumps app used are unchanged and re-exported for it.

Contract conventions (mirroring ``json_output.py``): optional fields are
*absent* when they don't apply, never emitted as ``null``; new fields may
be added, existing ones never removed or repurposed. Countdown text is
baked here for first paint; ``resetsAt`` epochs let the panel tick locally
between pushes.
"""

from __future__ import annotations

import math
import re
import time
from datetime import datetime, timezone

from claude_swap import pace
from claude_swap.json_output import (
    USAGE_API_KEY,
    USAGE_FOREIGN_CREDENTIAL,
    USAGE_KEYCHAIN_UNAVAILABLE,
    USAGE_RELOGIN_REQUIRED,
    USAGE_TOKEN_EXPIRED,
)
from claude_swap.switcher import SENTINEL_NOTES

ICON = "⇄"
SWITCH_HISTORY_LIMIT = 10

SCHEMA_VERSION = 1
AWAITING_USAGE_TEXT = "Awaiting updated usage"


# ---- window helpers (operate on the usage-window dict shape produced by
# ---- oauth.build_usage_result / stored in UsageEntry.last_good) -----------

def tightest_pct(usage: dict | str | None) -> float | None:
    """Highest 5h/7d utilization percentage, or None if unknown.

    Surfaces the binding window's utilization for display. Spend is excluded —
    it isn't a rate-limit window.
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
    """Utilization pct for a usage window (``five_hour``/``seven_day``), or None."""
    if isinstance(usage, dict):
        window = usage.get(key)
        if isinstance(window, dict) and isinstance(window.get("pct"), (int, float)):
            return float(window["pct"])
    return None


def _resets_at_ts(window: dict | str | None) -> float:
    """POSIX timestamp of a usage window's ``resets_at``; inf if missing/bad."""
    if isinstance(window, dict):
        ra = window.get("resets_at")
        if isinstance(ra, str):
            try:
                return datetime.fromisoformat(ra).timestamp()
            except ValueError:
                pass
    return float("inf")


def _live_countdown(window: dict | str | None, now: float) -> str | None:
    """Time until a usage window resets, computed live from ``resets_at``.

    The cached usage dict's ``countdown`` string is frozen at fetch time, so a
    stale (e.g. last-known-good) entry would show a wrong remaining time. Deriving
    it from the absolute ``resets_at`` keeps it correct between/without refetches.
    Returns ``None`` when there's no ``resets_at`` or it has already passed.
    """
    ts = _resets_at_ts(window)
    if ts == float("inf"):
        return None
    return _countdown_text(ts, now)


def _countdown_text(ts: float, now: float) -> str | None:
    remaining = int(ts - now)
    if remaining <= 0:
        return None
    days, rem = divmod(remaining, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


_WEEKLY_PERIOD_S = 7 * 86400  # weekly limits reset on a fixed 7-day cadence


def _rolled_weekly_window(window: dict | None, now: float) -> dict | None:
    """A weekly window with a passed reset advanced to its next 7-day boundary.

    Weekly limits reset on a fixed weekly cadence, so once the stored
    ``resets_at`` is in the past we know the window rolled over — the stored pct
    belongs to a window that no longer exists. Return a copy reflecting the reset
    state (``pct`` 0, ``resets_at`` advanced to the next future boundary) so the
    menu bar shows the reset from the static schedule alone, without waiting to
    spend tokens on a fresh fetch. Missing/future/unparseable windows are
    returned unchanged.
    """
    if not isinstance(window, dict):
        return window
    ts = _resets_at_ts(window)
    if ts == float("inf") or ts > now:
        return window
    missed = int((now - ts) // _WEEKLY_PERIOD_S) + 1
    new_ts = ts + missed * _WEEKLY_PERIOD_S
    rolled = dict(window)
    rolled["pct"] = 0.0
    rolled["resets_at"] = datetime.fromtimestamp(new_ts, tz=timezone.utc).isoformat()
    rolled.pop("countdown", None)  # recomputed live from the rolled resets_at
    rolled.pop("clock", None)
    return rolled


# ---- legacy formatting helpers (title, menu rows, usage log) ---------------

def usage_summary(
    usage: dict | str | None, now: float | None = None, fetched_at: float | None = None
) -> str:
    """One-line usage summary for an account row (reset countdown computed live).

    ``fetched_at`` is the underlying measurement's fetch time (may be older
    than ``now`` when serving last-good data) — used only to flag a weekly
    window that's meaningfully ahead of pace (issue #125), never the 5h one.
    """
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
            window = _rolled_weekly_window(window, now)  # reflect a passed weekly reset
            # Pace against the rolled window, not the raw one: a stale window
            # rolled to 0% has no current-cycle data to compare against, so
            # its (correctly zeroed) pct naturally never reads as "ahead" —
            # computing pace pre-roll would otherwise pair last cycle's high
            # pct with this cycle's freshly-reset 0% display.
            pace_result = pace.compute_pace(window, fetched_at=fetched_at)
        if isinstance(window, dict) and isinstance(window.get("pct"), (int, float)):
            seg = f"{label} {window['pct']:.0f}%"
            if key == "seven_day" and pace_result and pace_result.ahead:
                seg += " (ahead)"
            countdown = _live_countdown(window, now)
            if countdown:
                seg += f" ({countdown})"  # time until this window resets
            parts.append(seg)
    # Per-model weekly limits (e.g. Fable), from the usage API's ``limits`` array.
    for window in usage.get("scoped") or []:
        window = _rolled_weekly_window(window, now)  # weekly cadence, same roll-forward
        pace_result = pace.compute_pace(window, fetched_at=fetched_at)  # against the rolled window, see above
        if isinstance(window, dict) and isinstance(window.get("pct"), (int, float)) and window.get("name"):
            seg = f"{window['name']} {window['pct']:.0f}%"
            if window["pct"] >= 100:
                seg += " (!)"  # maxed model — the usual reason to switch
            elif pace_result and pace_result.ahead:
                seg += " (ahead)"
            countdown = _live_countdown(window, now)
            if countdown:
                seg += f" ({countdown})"
            parts.append(seg)
    spend = usage.get("spend")
    if isinstance(spend, dict) and isinstance(spend.get("pct"), (int, float)):
        parts.append(f"$ {spend['pct']:.0f}%")
    return " · ".join(parts) if parts else "usage unavailable"


def format_account_label(
    num,
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
    settings,
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
        p = _window_pct(active_usage, "five_hour")
        if p is not None:
            segments.append(f"{p:.0f}%")
    if settings.title_pct in ("7d", "both"):
        seven = active_usage.get("seven_day") if isinstance(active_usage, dict) else None
        seven = _rolled_weekly_window(seven, now)  # reflect a passed weekly reset
        p = seven["pct"] if isinstance(seven, dict) and isinstance(seven.get("pct"), (int, float)) else None
        if p is not None:
            segments.append(f"{p:.0f}%")
    if settings.title_scoped and isinstance(active_usage, dict):
        # Per-model weekly limits (e.g. Fable), same shape/roll-forward as the
        # dropdown rows; named so multiple scoped models stay distinguishable.
        for window in active_usage.get("scoped") or []:
            window = _rolled_weekly_window(window, now)
            if isinstance(window, dict) and isinstance(window.get("pct"), (int, float)) and window.get("name"):
                segments.append(f"{window['name']} {window['pct']:.0f}%")
    if not segments:
        return ICON
    return f"{ICON} " + " · ".join(segments)


def format_usage_log(email: str, usage: dict | str | None) -> str | None:
    """A log line of an account's session (5h) and weekly (7d) limits.

    Uses each window's absolute reset ``clock`` rather than a live countdown,
    since log lines are already timestamped. Returns ``None`` when no numeric
    window is available (sentinels, ``None``, or spend-only) so callers can skip
    logging nothing.
    """
    parts: list[str] = []
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        pct = _window_pct(usage, key)
        if pct is None:
            continue
        window = usage.get(key)  # a dict — _window_pct found a numeric pct in it
        clock = window.get("clock") if isinstance(window, dict) else None
        seg = f"{label} {pct:.0f}%"
        if clock:
            seg += f" (resets {clock})"
        parts.append(seg)
    if not parts:
        return None
    return f"usage {email}: " + " · ".join(parts)


def _usage_log_key(usage: dict | str | None) -> tuple[float | None, float | None]:
    """De-dupe key for usage logging: the (5h, 7d) percentages only.

    Reset clocks change every refresh; keying on the percentages means an idle
    account isn't re-logged every cycle.
    """
    return (_window_pct(usage, "five_hour"), _window_pct(usage, "seven_day"))


_SWITCH_LOG_RE = re.compile(r"Switched from account (\d+) to (\d+)")


def parse_switch_history(log_text: str, limit: int = SWITCH_HISTORY_LIMIT) -> list[str]:
    """Recent account switches from the log, most-recent first.

    Reads the ``Switched from account X to Y`` lines the switcher logs and pairs
    each with its timestamp (trimmed to the minute). Returns at most ``limit``
    entries like ``"3 → 1   2026-06-27 02:06"``. Any unparseable line is skipped.
    """
    out: list[str] = []
    for line in log_text.splitlines():
        m = _SWITCH_LOG_RE.search(line)
        if not m:
            continue
        stamp = line.split(" - ", 1)[0].strip()[:16]  # "YYYY-MM-DD HH:MM"
        out.append(f"{m.group(1)} → {m.group(2)}   {stamp}")
    return out[-limit:][::-1]


# ---- snapshot adaptation (legacy render dict + v2 view-model) --------------

def _account_display_usage(entry) -> dict | str | None:
    """Menu-display usage for a ``UsageEntry``.

    A human-readable note for a sentinel state (token expired / API key /
    keychain unavailable), otherwise the last-good measurement dict, otherwise
    ``None``.
    """
    if entry.sentinel:
        return SENTINEL_NOTES.get(entry.sentinel, entry.sentinel)
    return entry.last_good


EMPTY_SNAPSHOT: dict = {
    "accounts": [],
    "active_email": None,
    "active_usage": None,
    "active_alias": None,
}


def _adapt_snapshot(snap) -> dict:
    """Adapt an ``AccountsSnapshot`` to the menu bar's render dict.

    Shape: ``{"accounts": [(num, email, is_active, display_usage, last_good, alias, disabled, fetched_at), ...],
    "active_email": str | None, "active_usage": dict | str | None,
    "active_alias": str | None}``. The snapshot itself is produced by
    ``SnapshotSource`` (the paced read path), so this is a pure transform — no
    fetching, no I/O. Per-account ``fetched_at`` is the underlying
    measurement's fetch time, used only for the pace marker (issue #125).
    """
    accounts = []
    active_email = None
    active_usage = None
    active_alias = None
    for acc in snap.accounts:
        display = _account_display_usage(acc.usage)
        accounts.append(
            (
                acc.number, acc.email, acc.is_active, display, acc.usage.last_good,
                acc.alias, acc.disabled, acc.usage.fetched_at,
            )
        )
        if acc.is_active:
            active_email, active_usage, active_alias = acc.email, display, acc.alias
    return {
        "accounts": accounts,
        "active_email": active_email,
        "active_usage": active_usage,
        "active_alias": active_alias,
    }


# ---- v2 panel view-model ----------------------------------------------------

# Display status per sentinel: sentinels are *derived states*, not verdicts —
# an API-key account has no quota to show (not a failure), a dead refresh
# token genuinely needs a re-login, and the rest are transient availability
# problems that must never read as dead logins.
_STATUS_BY_SENTINEL = {
    USAGE_API_KEY: "api-key",
    USAGE_RELOGIN_REQUIRED: "needs-login",
    USAGE_TOKEN_EXPIRED: "unavailable",
    USAGE_KEYCHAIN_UNAVAILABLE: "unavailable",
    USAGE_FOREIGN_CREDENTIAL: "unavailable",
}


def _account_status(entry) -> str:
    if entry.sentinel:
        return _STATUS_BY_SENTINEL.get(entry.sentinel, "unavailable")
    return "ok"

def _age_text(age_s: float | None) -> str | None:
    """Human age of a measurement: "just now" / "Nm ago" / "Nh [Nm] ago"."""
    if age_s is None:
        return None
    if age_s < 60:
        return "just now"
    minutes = int(age_s // 60)
    if minutes < 60:
        return f"{minutes}m ago"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h ago" if minutes == 0 else f"{hours}h {minutes}m ago"


def _window_vm(kind: str, label: str, window: dict, *, now: float, state: str) -> dict | None:
    """One bar's view-model: pct + reset epoch + baked first-paint countdown.

    None when the pct isn't a finite number — NaN/Infinity must never reach
    the wire (legal JS literals, illegal JSON).
    """
    pct = float(window["pct"])
    if not math.isfinite(pct):
        return None
    ts = _resets_at_ts(window)
    if ts != float("inf") and ts <= now:
        # The reset has passed: keep the MEASURED value (never fabricate a
        # zero), mark it stale, and say what we're waiting for.
        return {
            "kind": kind, "label": label, "pct": pct, "state": "stale",
            "countdownText": AWAITING_USAGE_TEXT,
        }
    vm = {"kind": kind, "label": label, "pct": pct, "state": state}
    if ts != float("inf"):
        vm["resetsAt"] = ts
        countdown = _countdown_text(ts, now)
        if countdown:
            vm["countdownText"] = countdown
    return vm


def _spend_vm(spend: dict, *, now: float) -> dict | None:
    # ``last_good`` is persisted JSON that outlives upgrades, so a spend
    # entry can drift from the current shape; a KeyError here would fail
    # build() for *every* account. Degrade to "no spend row" instead.
    used, limit = spend.get("used"), spend.get("limit")
    if not isinstance(used, (int, float)) or isinstance(used, bool):
        return None
    if limit is not None and not isinstance(limit, (int, float)):
        return None
    pct = float(spend["pct"])
    if not (math.isfinite(pct) and math.isfinite(used)
            and (limit is None or math.isfinite(limit))):
        return None
    vm = {
        "used": used,
        "pct": pct,
        "currency": spend.get("currency", "USD"),
    }
    if limit is not None:
        vm["limit"] = limit
    ts = _resets_at_ts(spend)
    if ts != float("inf"):
        vm["resetsAt"] = ts
    return vm


def _account_vm(acc, *, now: float) -> dict:
    entry = acc.usage
    display = _account_display_usage(entry)
    vm: dict = {
        "slot": acc.number,
        "label": _local_part(acc.email),
        "email": acc.email,
        "org": acc.display_tag,
        "kind": acc.kind,
        "active": acc.is_active,
        "switchable": acc.switchable,
        "status": _account_status(entry),
    }
    if acc.alias:
        vm["alias"] = acc.alias
    if acc.disabled:
        vm["disabled"] = True
    windows: list[dict] = []
    if isinstance(display, str):
        # Sentinel state (token expired, API key, …): a note replaces the bars
        # entirely — unknown is never rendered as 0%.
        vm["quarantined"] = True
        vm["note"] = display
    elif isinstance(display, dict):
        state = "stale" if entry.last_error else "ok"
        # Panel display keeps MEASURED values for windows whose reset has
        # passed (the legacy helpers still roll them forward for CLI/TUI);
        # _window_vm applies the passed-reset rule uniformly.
        for key, kind, label, window in (
            ("five_hour", "5h", "5-hour", display.get("five_hour")),
            ("seven_day", "7d", "7-day", display.get("seven_day")),
        ):
            if isinstance(window, dict) and isinstance(window.get("pct"), (int, float)):
                bar = _window_vm(kind, label, window, now=now, state=state)
                if bar is not None:
                    windows.append(bar)
        for window in display.get("scoped") or []:
            if (
                isinstance(window, dict)
                and isinstance(window.get("pct"), (int, float))
                and window.get("name")
            ):
                name = str(window["name"])
                bar = _window_vm(f"model:{name}", name, window, now=now, state=state)
                if bar is not None:
                    windows.append(bar)
        spend = display.get("spend")
        if isinstance(spend, dict) and isinstance(spend.get("pct"), (int, float)):
            spend_vm = _spend_vm(spend, now=now)
            if spend_vm is not None:
                vm["spend"] = spend_vm
        if state == "ok":
            # Pace is only meaningful on a current measurement: hide it when
            # stale-on-error or when the weekly reset has passed (the passed
            # window rule in _window_vm marks those stale).
            seven_ts = _resets_at_ts(display.get("seven_day"))
            if seven_ts == float("inf") or seven_ts > now:
                pace_result = pace.compute_pace(
                    display.get("seven_day"), fetched_at=entry.fetched_at
                )
            else:
                pace_result = None
        else:
            pace_result = None
        if pace_result is not None:
            vm["pace"] = {
                "aheadOfPace": pace_result.ahead,
                "expectedPct": pace_result.expected_pct,
            }
    else:
        # No measurement and no sentinel: an error note if we have one,
        # otherwise plainly unavailable.
        vm["note"] = entry.last_error if entry.last_error else "usage unavailable"
    vm["windows"] = windows
    if entry.last_error:
        vm["lastError"] = entry.last_error
    return vm


def build(
    snapshot,
    *,
    auto_enabled: bool = False,
    auto_threshold: float = 90.0,
    auto_strategy: str = "best",
    history: list[str] | None = None,
    now: float | None = None,
) -> dict:
    """Build the panel's additive schemaVersion-1 view-model.

    Pure transform of an ``AccountsSnapshot`` (see ``snapshot_source``): the
    panel renders exactly what the shared usage store already collected —
    ``build`` never fetches. ``auto_*`` come from the caller's settings so the
    function stays free of switcher dependencies and unit-testable.
    """
    if now is None:
        now = time.time()
    history = list(history) if history else []
    active = next((a for a in snapshot.accounts if a.is_active), None)

    freshness: dict = {"ok": False}
    if active is not None:
        entry = active.usage
        age_text = _age_text(entry.age_s)
        if age_text:
            freshness["ageText"] = age_text
        if entry.last_error:
            freshness["error"] = entry.last_error
        freshness["ok"] = not (entry.sentinel or entry.last_error or entry.last_good is None)

    auto_switch: dict = {
        "enabled": auto_enabled,
        "thresholdPct": auto_threshold,
        "strategy": auto_strategy,
    }
    if history:
        auto_switch["lastEventText"] = history[0]

    vm = {
        "schemaVersion": SCHEMA_VERSION,
        "takenAt": snapshot.taken_at,
        "freshness": freshness,
        "accounts": [_account_vm(acc, now=now) for acc in snapshot.accounts],
        "autoSwitch": auto_switch,
        "history": history,
    }
    if snapshot.active_number is not None:
        vm["activeSlot"] = snapshot.active_number
    return vm
