"""macOS menu bar app for claude-swap (``cswap --menubar``).

A thin GUI shell over ``ClaudeAccountSwitcher`` and the core auto-switch engine
(``claude_swap.autoswitch``) — it never re-implements account, usage, or
auto-switch logic. Usage for display comes from ``switcher.accounts_snapshot()``
(backed by the shared usage store); auto-switching, when enabled, runs the same
``AutoSwitchEngine`` the CLI's ``cswap auto`` drives, sharing
``autoswitch_state.json`` and the ``autoswitch.*`` settings. The menu bar keeps
only its own display preferences.

Built on ``rumps`` (an optional extra, macOS only). The pure helpers below
(settings, formatting, log parsing) are import-safe without rumps so they can be
unit-tested in CI; ``rumps`` is imported lazily inside the app glue.
"""

from __future__ import annotations

import json
import logging
import os
import plistlib
import re
import sys
import threading
import time
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path

from claude_swap import oauth, pace
from claude_swap.exceptions import ClaudeSwitchError, CredentialReadError
from claude_swap.switcher import SENTINEL_NOTES

ICON = "⇄"
REFRESH_CHOICES: tuple[int, ...] = (30, 60, 300)
AUTO_THRESHOLD_CHOICES: tuple[int, ...] = (80, 90, 95, 98)
# Strategy names as `autoswitch.strategy` stores them, mapped to menu labels.
# Keys must match settings.SETTING_SPECS["autoswitch.strategy"].choices; the
# labels say what the strategy DOES, since "best"/"consume-first" alone don't
# convey that one waits for the limit and the other spends the soonest reset.
STRATEGY_LABELS: dict[str, str] = {
    "best": "Best (most quota left)",
    "consume-first": "Consume first (soonest reset)",
}
TITLE_PCT_CHOICES: tuple[str, ...] = ("off", "5h", "7d", "both")
SWITCH_HISTORY_LIMIT = 10
# Account rows are padded into columns and drawn monospaced (see align_rows).
SPAN = ""  # cell key for text that ends a row instead of claiming a column
COLUMN_GAP = "  "
ROW_PCT_WIDTH = 3  # right-align percentages so "0%" and "100%" end together
NOTIFICATION_BUNDLE_ID = "com.claude-swap.menubar"


def ensure_notification_identity(
    executable: Path | None = None,
    *,
    platform: str = sys.platform,
) -> Path | None:
    """Ensure rumps can resolve a bundle identifier for notifications.

    Command-line Python tools have no app bundle, so rumps looks for an
    ``Info.plist`` beside the interpreter. uv/pipx reinstalls can recreate that
    environment; repair the tiny plist on every launch when needed.
    """
    if platform != "darwin":
        return None
    path = (executable or Path(sys.executable)).parent / "Info.plist"
    data: dict = {}
    try:
        if path.exists():
            try:
                loaded = plistlib.loads(path.read_bytes())
            except Exception:
                loaded = None  # unreadable/corrupt — rebuild from scratch
            if isinstance(loaded, dict):
                data = loaded
        changed = False
        if not data.get("CFBundleIdentifier"):
            data["CFBundleIdentifier"] = NOTIFICATION_BUNDLE_ID
            changed = True
        if not data.get("CFBundleName"):
            data["CFBundleName"] = "claude-swap"
            changed = True
        if changed or not path.exists():
            # atomic: an interrupted write must not leave a half-written plist
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes(plistlib.dumps(data))
            os.replace(tmp, path)
    except (OSError, plistlib.InvalidFileException, ValueError) as exc:
        logging.getLogger("claude-swap").warning(
            "Could not prepare menu-bar notification identity: %s", exc
        )
        return None
    return path


@dataclass
class MenuBarSettings:
    """User-configurable menu bar display behavior, persisted as JSON.

    Only display preferences and the auto-switch on/off toggle live here.
    Auto-switch *policy* (threshold, cooldown, hysteresis, …) is core config,
    read/written through ``claude_swap.settings`` (the ``autoswitch.*`` keys),
    so the CLI and the menu bar share one source of truth.
    """

    show_account_name: bool = True
    title_pct: str = "both"  # one of TITLE_PCT_CHOICES
    title_scoped: bool = False  # append per-model weekly limits (e.g. Fable) to the title
    refresh_interval: int = 60
    auto_switch_enabled: bool = False

    @classmethod
    def load(cls, path: Path) -> "MenuBarSettings":
        """Load settings, falling back to defaults on any problem.

        Unknown keys are ignored; a value whose type doesn't match the field
        default is dropped (that field keeps its default). A missing or
        unparseable file yields all-defaults.
        """
        defaults = cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return defaults
        if not isinstance(raw, dict):
            return defaults
        kwargs = {}
        for f in fields(cls):
            if f.name in raw and isinstance(raw[f.name], type(getattr(defaults, f.name))):
                kwargs[f.name] = raw[f.name]
        return cls(**kwargs)

    def save(self, path: Path) -> None:
        """Write settings as pretty JSON, creating parent directories."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


# ---- pure display helpers (operate on the usage-window dict shape produced by
# ---- oauth.build_usage_result / stored in UsageEntry.last_good) --------------

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
    remaining = int(ts - now)
    if remaining <= 0:
        return None
    days, rem = divmod(remaining, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days > 0:
        countdown = f"{days}d {hours}h"
    elif hours > 0:
        countdown = f"{hours}h {minutes}m"
    else:
        countdown = f"{minutes}m"
    # Countdown plus wall clock ("2h 15m · 11:19"): the remaining time says
    # how long, the clock says when — the reader shouldn't have to add.
    clock = oauth.reset_clock_string(
        datetime.fromtimestamp(ts, tz=timezone.utc),
        datetime.fromtimestamp(now, tz=timezone.utc),
    )
    return f"{countdown} · {clock}"


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


def usage_segments(
    usage: dict | str | None,
    now: float | None = None,
    fetched_at: float | None = None,
    *,
    pct_width: int = 0,
) -> list[tuple[str, str]]:
    """Usage as ``(column key, text)`` cells, in display order.

    The key names the column a cell belongs to, so rows that carry different
    windows can still be lined up: ``5h``/``7d``, one column per scoped model
    (keyed by its name), and ``$``. A sentinel string or a missing measurement
    yields a single :data:`SPAN` cell, which claims no column and simply ends
    the row.

    ``fetched_at`` is the underlying measurement's fetch time (may be older
    than ``now`` when serving last-good data) — used only to flag a weekly
    window that's meaningfully ahead of pace (issue #125), never the 5h one.

    ``pct_width`` right-aligns the percentage into that many digits so each
    window's suffix (pace marker, countdown) starts at the same offset on
    every row; ``0`` keeps the compact form the title and the log use.
    """
    if isinstance(usage, str):
        return [(SPAN, usage)]
    if usage is None:
        return [(SPAN, "usage unavailable")]
    if now is None:
        now = time.time()

    def pct(value: float) -> str:
        return f"{value:.0f}%".rjust(pct_width + 1)

    cells: list[tuple[str, str]] = []
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
            seg = f"{label} {pct(window['pct'])}"
            if key == "seven_day" and pace_result and pace_result.ahead:
                seg += " (ahead)"
            countdown = _live_countdown(window, now)
            if countdown:
                seg += f" ({countdown})"  # time until this window resets
            cells.append((label, seg))
    # Per-model weekly limits (e.g. Fable), from the usage API's ``limits`` array.
    for window in usage.get("scoped") or []:
        window = _rolled_weekly_window(window, now)  # weekly cadence, same roll-forward
        pace_result = pace.compute_pace(window, fetched_at=fetched_at)  # against the rolled window, see above
        if isinstance(window, dict) and isinstance(window.get("pct"), (int, float)) and window.get("name"):
            seg = f"{window['name']} {pct(window['pct'])}"
            if window["pct"] >= 100:
                seg += " (!)"  # maxed model — the usual reason to switch
            elif pace_result and pace_result.ahead:
                seg += " (ahead)"
            countdown = _live_countdown(window, now)
            if countdown:
                seg += f" ({countdown})"
            cells.append((window["name"], seg))
    spend = usage.get("spend")
    if isinstance(spend, dict) and isinstance(spend.get("pct"), (int, float)):
        cells.append(("$", f"$ {pct(spend['pct'])}"))
    return cells or [(SPAN, "usage unavailable")]


def usage_summary(
    usage: dict | str | None, now: float | None = None, fetched_at: float | None = None
) -> str:
    """One-line usage summary for the title and the log (no column padding)."""
    return " · ".join(text for _, text in usage_segments(usage, now, fetched_at))


def account_row_cells(
    num,
    email: str,
    usage: dict | str | None,
    now: float | None = None,
    alias: str | None = None,
    disabled: bool = False,
    fetched_at: float | None = None,
) -> list[tuple[str, str]]:
    """One account row as alignable ``(column key, text)`` cells."""
    label = f"{alias}  ({email})" if alias else email
    if disabled:
        label += "  (disabled)"
    return [
        ("#", str(num)),
        ("acct", label),
        *usage_segments(usage, now, fetched_at, pct_width=ROW_PCT_WIDTH),
    ]


def align_rows(rows: list[list[tuple[str, str]]]) -> list[str]:
    """Pad each column to its widest cell so rows line up in a monospaced font.

    Columns are the ordered union of the rows' non-:data:`SPAN` keys, so a row
    that lacks one — an account with no per-model limit, say — leaves a gap
    instead of shifting every later column left. A row's :data:`SPAN` cell is
    written where its first missing column would have begun, which keeps a
    sentinel ("no credentials") in the usage area rather than off to the right
    past every column it doesn't have.
    """
    keys: list[str] = []
    for row in rows:
        for key, _ in row:
            if key != SPAN and key not in keys:
                keys.append(key)
    widths = {
        key: max((len(text) for row in rows for k, text in row if k == key), default=0)
        for key in keys
    }
    labels: list[str] = []
    for row in rows:
        cells = dict(row)
        span = cells.pop(SPAN, None)
        parts: list[str] = []
        for key in keys:
            if span is not None and key not in cells:
                break
            parts.append(cells.get(key, "").ljust(widths[key]))
        if span is not None:
            parts.append(span)
        labels.append(COLUMN_GAP.join(parts).rstrip())
    return labels


# The `autoswitch.model` sentinel meaning "every scoped window this account
# reports" (see oauth.relevant_windows).
MODEL_ALL = "all"


def model_menu_names(usages) -> list[str]:
    """Scoped model names present anywhere in the snapshot, first-seen order.

    The "Count model limits" submenu offers exactly these. Reading them off the
    measurements the rows already display means the menu tracks whatever models
    the fleet actually reports, instead of carrying a hardcoded list that goes
    stale the day a new model ships.
    """
    names: list[str] = []
    for usage in usages:
        if not isinstance(usage, dict):
            continue  # sentinel ("no credentials") or a failed fetch
        for window in usage.get("scoped") or ():
            if isinstance(window, dict) and isinstance(window.get("name"), str):
                if window["name"] not in names:
                    names.append(window["name"])
    return names


def toggle_model(current: str, name: str) -> str:
    """``autoswitch.model`` with ``name`` flipped in or out of its comma list.

    Matches case-insensitively and tolerates hand-written spacing, because the
    CLI writes this same setting as free text and ``oauth.relevant_windows``
    compares it case-insensitively too.

    ``all`` is a wildcard rather than a membership list, so there is nothing to
    remove a name *from*: toggling a model while it is set narrows the setting
    to just that model.
    """
    if current.strip().lower() == MODEL_ALL:
        return name
    selected = [part.strip() for part in current.split(",") if part.strip()]
    kept = [s for s in selected if s.lower() != name.lower()]
    if len(kept) == len(selected):
        kept.append(name)  # was not selected — turn it on
    return ",".join(kept)


def _set_monospaced_title(menu_item, text: str) -> None:
    """Draw one ``NSMenuItem``'s title in the system monospaced font.

    Menus render in a proportional font, where the padding :func:`align_rows`
    adds doesn't line anything up; an attributed title is the only per-item
    way to change that. A pyobjc surprise here is not worth losing the menu
    over — rumps' plain title stays, just unaligned.
    """
    try:
        from AppKit import NSFont, NSFontAttributeName
        from Foundation import NSAttributedString

        font = NSFont.monospacedSystemFontOfSize_weight_(NSFont.systemFontSize(), 0.0)
        menu_item.setAttributedTitle_(
            NSAttributedString.alloc().initWithString_attributes_(
                text, {NSFontAttributeName: font}
            )
        )
    except Exception:
        logging.getLogger("claude-swap").debug(
            "monospaced menu title unavailable", exc_info=True
        )


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


def run(switcher) -> int:
    """Entry point for ``cswap --menubar``. Blocks until the user quits."""
    ensure_notification_identity()
    try:
        import rumps  # lazy: optional dependency, imported only when launching
        import AppKit  # ships with rumps (pyobjc-framework-Cocoa), never fails alone
    except ImportError as e:
        # This module is import-safe without rumps by design, so the CLI's
        # guard around ``from claude_swap.menubar import run`` can never see a
        # missing extra — the failure lands here at call time. Raise the
        # error type the CLI already renders cleanly instead of a traceback.
        raise ClaudeSwitchError(
            "Menu bar mode requires 'rumps'. "
            "Install with: pip install 'claude-swap[menubar]'"
        ) from e

    # rumps never sets an activation policy, so under a framework Python the
    # process launches as a regular app and parks a "Python" icon in the Dock
    # for as long as the menu bar runs. Accessory keeps the status item and
    # dialog windows but stays out of the Dock and the Cmd-Tab switcher.
    AppKit.NSApplication.sharedApplication().setActivationPolicy_(
        AppKit.NSApplicationActivationPolicyAccessory
    )

    from claude_swap import session_resume
    from claude_swap.autoswitch import AutoSwitchEngine
    from claude_swap.settings import (
        AutoSwitchSettings,
        load_settings,
        set_setting,
        unset_setting,
    )
    from claude_swap.snapshot_source import SnapshotSource

    settings_path = switcher.backup_dir / "menubar_settings.json"
    log_path = switcher.backup_dir / "claude-swap.log"

    class MenuBarApp(rumps.App):
        def __init__(self):
            super().__init__(ICON, quit_button=None)
            self.switcher = switcher
            self.settings = MenuBarSettings.load(settings_path)
            # The supported paced read path: per refresh it fetches only the
            # active account plus (at most once per freshness window) one stale
            # alternate, so an open menu costs O(1) requests per window instead
            # of a full pass per tick — which kept every token at its per-account
            # rate-limit edge. Reused across refreshes to hold its pacing state.
            self._snapshot_source = SnapshotSource(switcher)
            self.snapshot = dict(EMPTY_SNAPSHOT)
            self._dirty = False
            self._snapshot_at = 0.0
            self._refreshing = False
            # Set by the resume worker, drained on the main thread by
            # on_sync_tick — rumps notifications must not be posted from a
            # background thread.
            self._resume_note = None
            self._config_path = switcher._get_claude_config_path()
            self._config_mtime = 0.0
            self._last_usage_log: dict = {}  # account num -> last-logged (5h, 7d) key
            # Auto-switch engine (the same one `cswap auto` runs), hosted in a
            # background thread while enabled.
            self._engine = None
            self._engine_events: list = []
            self._event_lock = threading.Lock()
            self.rebuild_menu()
            # Background display refresh on the user's interval, plus a fast
            # UI-sync tick that applies snapshots + engine events on the main thread.
            self.refresh_timer = rumps.Timer(self.on_refresh_tick, self.settings.refresh_interval)
            self.refresh_timer.start()
            self.sync_timer = rumps.Timer(self.on_sync_tick, 1)
            self.sync_timer.start()
            self.refresh_async()  # first display fetch
            if self.settings.auto_switch_enabled:
                self._start_engine()

        # ---- display refresh plumbing ----------------------------------------
        def refresh_async(self, full=False):
            if self._refreshing:
                return  # in-flight guard: one worker at a time (SnapshotSource
                        # pacing state is only touched by this single worker)
            self._refreshing = True
            threading.Thread(target=self._worker, args=(full,), daemon=True).start()

        def _worker(self, full):
            # Lock-free handoff: worker only rebinds plain attributes (atomic in
            # CPython); the main-thread sync tick reads them. While the engine
            # runs it already paces all fetching, so the display reads store-only.
            try:
                try:
                    raw = self._snapshot_source.take(
                        full=full, store_only=self._engine is not None
                    )
                except Exception:
                    # Keep the last good snapshot rather than blanking the menu.
                    self.switcher._logger.debug("menubar snapshot failed", exc_info=True)
                    return
                snap = _adapt_snapshot(raw)
                self._log_usage(snap)
                self.snapshot = snap
                self._snapshot_at = time.time()
                self._dirty = True  # picked up by on_sync_tick on the main thread
            finally:
                self._refreshing = False

        def _log_usage(self, snap):
            """Log each account's session/weekly limits when they change.

            Runs on every refresh (background thread; the logger is thread-safe)
            but de-dupes per account on the (5h, 7d) percentages so an idle
            machine doesn't churn the rotating log with identical lines.
            """
            for num, email, _is_active, _display, last_good, _alias, _disabled, _fetched_at in snap["accounts"]:
                key = _usage_log_key(last_good)
                if key == (None, None) or self._last_usage_log.get(num) == key:
                    continue
                line = format_usage_log(email, last_good)
                if line:
                    self.switcher._logger.info(line)
                    self._last_usage_log[num] = key

        def on_refresh_tick(self, _timer):
            self.refresh_async()

        def on_sync_tick(self, _timer):
            if self._dirty:
                self._dirty = False
                self.rebuild_menu()
            self._detect_active_change()
            self._drain_engine_events()
            note, self._resume_note = self._resume_note, None
            if note:
                rumps.notification(
                    "claude-swap",
                    "Resumed stopped sessions",
                    f"{note} session(s) stopped by the usage limit were nudged.",
                )

        def _detect_active_change(self):
            # Reflect account switches from any source (menu, CLI, auto engine)
            # within ~1s. Detecting *which* account is active is a cheap local
            # read of ~/.claude.json -- no Keychain or usage API -- so we can do
            # it on every tick. We gate the read on the file's mtime (a cheap
            # stat) so a large config isn't parsed each second, and only kick a
            # refresh when the active email actually changed (Claude Code rewrites
            # this file often for unrelated reasons).
            if self._refreshing:
                return  # a worker is already in-flight; it refreshes the marker
            try:
                mtime = self._config_path.stat().st_mtime
            except OSError:
                return
            if mtime == self._config_mtime:
                return
            self._config_mtime = mtime
            current = self.switcher._get_current_account()
            email = current[0] if current else None
            if email and email != self.snapshot.get("active_email"):
                self.refresh_async()

        # ---- auto-switch engine ----------------------------------------------
        def _start_engine(self):
            """Run the core AutoSwitchEngine (live) in a background thread."""
            if self._engine is not None:
                return
            try:
                engine = AutoSwitchEngine(
                    self.switcher,
                    load_settings(self.switcher.backup_dir),
                    self._on_engine_event,
                    dry_run=False,
                )
            except Exception as e:  # never let a bad start crash the menu bar
                self.switcher._logger.warning("auto-switch engine failed to start: %s", e)
                rumps.notification("claude-swap", "Auto-switch failed to start", str(e))
                return
            self._engine = engine
            threading.Thread(target=self._run_engine, args=(engine,), daemon=True).start()

        def _run_engine(self, engine):
            try:
                engine.run_loop()
            except Exception:
                self.switcher._logger.debug("auto-switch engine crashed", exc_info=True)

        def _stop_engine(self):
            if self._engine is not None:
                self._engine.stop()
                self._engine = None

        def _restart_engine(self):
            """Apply changed core settings by restarting the running engine."""
            if self._engine is not None:
                self._stop_engine()
                self._start_engine()

        def _on_engine_event(self, event):
            # Runs on the engine thread; must not raise. Queue for the main
            # thread, which surfaces notifications and reacts on the sync tick.
            with self._event_lock:
                self._engine_events.append(event)

        def _drain_engine_events(self):
            with self._event_lock:
                events, self._engine_events = self._engine_events, []
            for ev in events:
                if ev.kind == "switch" and not getattr(ev, "dry_run", False):
                    rumps.notification("claude-swap", "Auto-switched account", ev.human())
                    self.refresh_async()  # reflect the switch promptly
                elif ev.kind == "account-quarantined":
                    rumps.notification("claude-swap", "Account quarantined", ev.human())
                elif ev.kind == "all-exhausted":
                    rumps.notification("claude-swap", "All accounts exhausted", ev.human())
                elif ev.kind == "config-warning":
                    # e.g. an autoswitch.model name no account reports — the
                    # engine emits it once per run; dropping it would leave a
                    # menu-bar user with a silently inert filter.
                    rumps.notification("claude-swap", "Configuration warning", ev.human())

        def _threshold(self) -> int:
            """Current auto-switch threshold from core settings (for the menu)."""
            try:
                return int(load_settings(self.switcher.backup_dir).threshold)
            except Exception:
                return 0

        def _resume_stopped(self) -> bool:
            """Whether limit-stopped sessions are resumed (for the menu)."""
            try:
                return load_settings(self.switcher.backup_dir).resume_stopped_sessions
            except Exception:
                return AutoSwitchSettings.resume_stopped_sessions

        def _limit_scan_on(self) -> bool:
            """Whether the engine scans sessions for a hit limit (for the menu)."""
            try:
                return load_settings(self.switcher.backup_dir).limit_scan_interval_seconds > 0
            except Exception:
                return AutoSwitchSettings.limit_scan_interval_seconds > 0

        def _models(self) -> str:
            """Current ``autoswitch.model`` selection (for the menu)."""
            try:
                return load_settings(self.switcher.backup_dir).model or ""
            except Exception:
                return ""

        def _strategy(self) -> str:
            """Current auto-switch strategy from core settings (for the menu).

            Falls back to the dataclass default rather than a sentinel: the
            strategy names a menu item that must stay checkable, and an
            unreadable settings file is not a reason to show every item
            unchecked.
            """
            try:
                return load_settings(self.switcher.backup_dir).strategy
            except Exception:
                return AutoSwitchSettings.strategy

        # ---- menu construction -----------------------------------------------
        def rebuild_menu(self):
            self.title = format_title(
                self.snapshot["active_email"],
                self.snapshot["active_usage"],
                self.settings,
                alias=self.snapshot.get("active_alias"),
            )
            self.menu.clear()
            account_items = []
            accounts = self.snapshot["accounts"]
            # Widths come from the whole snapshot, so every row has to be
            # rendered together — one row can't know how wide the column is.
            labels = align_rows([
                account_row_cells(
                    num, email, display, alias=alias, disabled=disabled, fetched_at=fetched_at
                )
                for num, email, _active, display, _last_good, alias, disabled, fetched_at in accounts
            ])
            for (num, _email, is_active, *_rest), label in zip(accounts, labels):
                item = rumps.MenuItem(label, callback=self._make_switch_to(num))
                _set_monospaced_title(item._menuitem, label)
                item.state = 1 if is_active else 0
                account_items.append(item)
            if not account_items:
                account_items.append(rumps.MenuItem("No managed accounts", callback=None))

            self.menu = [
                *account_items,
                None,
                rumps.MenuItem("Rotate to next", callback=self._switch(None)),
                rumps.MenuItem("Switch to best", callback=self._switch("best")),
                rumps.MenuItem("Next available", callback=self._switch("next-available")),
                None,
                self._add_menu(rumps),
                self._disable_menu(rumps),
                self._remove_menu(rumps),
                rumps.MenuItem("Refresh current credentials", callback=self.on_refresh_creds),
                self._history_menu(rumps),
                None,
                self._settings_menu(rumps),
                rumps.MenuItem("Refresh now", callback=self.on_refresh_now),
                rumps.MenuItem("Quit", callback=self.on_quit),
            ]

        def _add_menu(self, rumps):
            menu = rumps.MenuItem("Add account")
            menu.add(rumps.MenuItem("From current login", callback=self.on_add_login))
            if hasattr(self.switcher, "add_account_from_token"):
                menu.add(rumps.MenuItem("From setup-token…", callback=self.on_add_token))
            return menu

        def _remove_menu(self, rumps):
            menu = rumps.MenuItem("Remove account")
            accounts = self.snapshot["accounts"]
            if not accounts:
                menu.add(rumps.MenuItem("No managed accounts", callback=None))
            for num, email, _is_active, _display, _last_good, alias, _disabled, _fetched_at in accounts:
                label = f"{num}  {alias}  ({email})" if alias else f"{num}  {email}"
                menu.add(rumps.MenuItem(label, callback=self._make_remove(num)))
            return menu

        def _disable_menu(self, rumps):
            menu = rumps.MenuItem("Disable / enable account")
            accounts = self.snapshot["accounts"]
            if not accounts:
                menu.add(rumps.MenuItem("No managed accounts", callback=None))
            for num, email, _is_active, _display, _last_good, alias, disabled, _fetched_at in accounts:
                name = f"{alias}  ({email})" if alias else email
                item = rumps.MenuItem(
                    f"{num}  {name}", callback=self._make_toggle_disabled(num, disabled)
                )
                # A check-mark reads as "held out of rotation" — same glyph the
                # active row uses, but here it means disabled, not selected.
                item.state = 1 if disabled else 0
                menu.add(item)
            return menu

        def _history_menu(self, rumps):
            menu = rumps.MenuItem("Switch history")
            try:
                text = log_path.read_text(encoding="utf-8")
            except OSError:
                text = ""
            entries = parse_switch_history(text)
            if entries:
                for line in entries:
                    menu.add(rumps.MenuItem(line, callback=None))
            else:
                menu.add(rumps.MenuItem("No switches logged yet", callback=None))
            menu.add(None)
            menu.add(rumps.MenuItem("Open full log…", callback=self.on_open_log))
            return menu

        def _settings_menu(self, rumps):
            menu = rumps.MenuItem("Settings")
            name_item = rumps.MenuItem("Show account name in menu bar", callback=self.on_toggle_name)
            name_item.state = 1 if self.settings.show_account_name else 0
            menu.add(name_item)

            title_pct = rumps.MenuItem("Title percentage")
            tp_labels = {"off": "None", "5h": "Session (5h)",
                         "7d": "Weekly (7d)", "both": "Both (5h · 7d)"}
            for mode in TITLE_PCT_CHOICES:
                ch = rumps.MenuItem(tp_labels[mode], callback=self._make_title_pct(mode))
                ch.state = 1 if self.settings.title_pct == mode else 0
                title_pct.add(ch)
            menu.add(title_pct)

            scoped_item = rumps.MenuItem(
                "Show model limits in title", callback=self.on_toggle_scoped
            )
            scoped_item.state = 1 if self.settings.title_scoped else 0
            menu.add(scoped_item)

            interval = rumps.MenuItem("Refresh interval")
            labels = {30: "30 seconds", 60: "60 seconds", 300: "5 minutes"}
            for secs in REFRESH_CHOICES:
                choice = rumps.MenuItem(labels[secs], callback=self._make_interval(secs))
                choice.state = 1 if self.settings.refresh_interval == secs else 0
                interval.add(choice)
            menu.add(interval)

            # Causal order: master switch, then policy (strategy,
            # threshold), then the triggers that fire switches (limit
            # scan, model limits), then the aftermath (resume).
            auto_item = rumps.MenuItem("Auto-switch accounts", callback=self.on_toggle_autoswitch)
            auto_item.state = 1 if self.settings.auto_switch_enabled else 0
            menu.add(auto_item)

            strategy = self._strategy()
            strategy_menu = rumps.MenuItem("Auto-switch strategy")
            for name, label in STRATEGY_LABELS.items():
                ch = rumps.MenuItem(label, callback=self._make_strategy(name))
                ch.state = 1 if strategy == name else 0
                strategy_menu.add(ch)
            menu.add(strategy_menu)

            # Under consume-first the threshold is NOT the switch point — the
            # engine burns the active account to its limit and moves on reset
            # ordering instead, so the threshold only bars landing on a spent
            # account. Naming it "Auto-switch threshold" there would promise a
            # switch at 98% that deliberately never comes.
            threshold_menu = rumps.MenuItem(
                "Landing limit"
                if strategy == "consume-first"
                else "Auto-switch threshold"
            )
            current = self._threshold()
            for pct in AUTO_THRESHOLD_CHOICES:
                ch = rumps.MenuItem(f"{pct}%", callback=self._make_threshold(pct))
                ch.state = 1 if current == pct else 0
                threshold_menu.add(ch)
            menu.add(threshold_menu)

            scan_item = rumps.MenuItem(
                "Switch the moment a limit is hit", callback=self.on_toggle_limit_scan
            )
            scan_item.state = 1 if self._limit_scan_on() else 0
            menu.add(scan_item)

            # Per-model weekly limits are OFF by default: folding one in makes
            # an account with 5h/7d headroom but a maxed model count as spent,
            # so the engine switches away from it. Right for someone pinned to
            # that model, surprising for everyone else — hence opt-in, and
            # offered only for models the fleet actually reports.
            models = self._models()
            model_names = model_menu_names(a[3] for a in self.snapshot["accounts"])
            if model_names:
                selected = {m.strip().lower() for m in models.split(",") if m.strip()}
                every = MODEL_ALL in selected
                model_menu = rumps.MenuItem("Count model limits")
                all_item = rumps.MenuItem("All models", callback=self.on_toggle_all_models)
                all_item.state = 1 if every else 0
                model_menu.add(all_item)
                model_menu.add(None)
                for name in model_names:
                    ch = rumps.MenuItem(name, callback=self._make_model_toggle(name))
                    ch.state = 1 if every or name.lower() in selected else 0
                    model_menu.add(ch)
                menu.add(model_menu)

            resume_item = rumps.MenuItem(
                "Resume limit-stopped sessions", callback=self.on_toggle_resume
            )
            resume_item.state = 1 if self._resume_stopped() else 0
            menu.add(resume_item)

            return menu

        # ---- callbacks --------------------------------------------------------
        def _save_and_rebuild(self):
            self.settings.save(settings_path)
            self.rebuild_menu()

        def _guard(self, fn):
            """Run a switcher action, surfacing ClaudeSwitchError via an alert."""
            try:
                fn()
                return True
            except ClaudeSwitchError as e:
                rumps.alert(title="claude-swap", message=str(e))
                return False

        def _notify_switched(self):
            rumps.notification(
                "claude-swap",
                "Account switched",
                "Switch takes effect within ~30s — restart Claude Code to apply immediately.",
            )

        def _resume_stopped_sessions(self, before):
            """Wake sessions the usage limit stopped, after a switch from the menu.

            The engine nudges from its own tick, but a user who has auto-switch
            turned off — and is therefore switching by hand — has no engine
            running to do it. Without this, `resumeStoppedSessions` is inert for
            exactly the person driving the switches.

            Runs on a worker: a nudge burned on a stale credential is watched
            and retried, so this call can take the better part of a minute
            (RESUME_RETRY_DELAYS_S plus a verify window each). Measured
            2026-08-27, doing it inline froze the menu for 42s after a switch.
            """
            threading.Thread(
                target=self._resume_worker, args=(before,), daemon=True
            ).start()

        def _resume_worker(self, before):
            try:
                resumed = session_resume.resume_after_manual_switch(
                    self.switcher, before
                )
            except Exception:
                self.switcher._logger.debug("resume worker failed", exc_info=True)
                return
            if resumed:
                self._resume_note = len(resumed)  # main thread posts it

        def _make_switch_to(self, num):
            def cb(_sender):
                before = self.switcher.current_account_number()
                if self._guard(lambda: self.switcher.switch_to(str(num))):
                    self._notify_switched()
                    self._resume_stopped_sessions(before)
                    self.refresh_async()
            return cb

        def _switch(self, strategy):
            def cb(_sender):
                before = self.switcher.current_account_number()
                if self._guard(lambda: self.switcher.switch(strategy=strategy)):
                    self._notify_switched()
                    self._resume_stopped_sessions(before)
                    self.refresh_async()
            return cb

        def _make_remove(self, num):
            def cb(_sender):
                if rumps.alert(
                    title="Remove account",
                    message=f"Remove account {num}?",
                    ok="Remove",
                    cancel="Cancel",
                ) == 1:  # 1 == OK
                    if self._guard(lambda: self.switcher.remove_account(str(num), assume_yes=True)):
                        self.refresh_async()
            return cb

        def _make_toggle_disabled(self, num, disabled):
            # `disabled` is this row's current state; selecting it flips it.
            target = not disabled
            def cb(_sender):
                if self._guard(
                    lambda: self.switcher.set_account_disabled(str(num), target)
                ):
                    self.refresh_async()
            return cb

        def on_add_login(self, _sender):
            if self._guard(self.switcher.add_account):
                self.refresh_async()

        def on_add_token(self, _sender):
            # A menu-bar (accessory) app isn't the active app, so a modal
            # rumps.Window can render black/blank until we bring the app
            # forward. Activate before showing the input dialogs.
            import AppKit
            AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            email_win = rumps.Window(
                title="Add account from setup-token",
                message="Email for this token:",
                ok="Next", cancel="Cancel", dimensions=(320, 24),
            )
            email_resp = email_win.run()
            if email_resp.clicked != 1 or not email_resp.text.strip():
                return
            token_win = rumps.Window(
                title="Add account from setup-token",
                message="Setup token (sk-ant-oat01-…):",
                ok="Add", cancel="Cancel", dimensions=(320, 24),
            )
            token_resp = token_win.run()
            if token_resp.clicked != 1 or not token_resp.text.strip():
                return
            if self._guard(lambda: self.switcher.add_account_from_token(
                token=token_resp.text.strip(), email=email_resp.text.strip(), slot=None,
            )):
                self.refresh_async()

        def on_open_log(self, _sender):
            import subprocess
            # Reveal the log in Finder (-R); if it doesn't exist yet, open the dir.
            target = log_path if log_path.exists() else log_path.parent
            subprocess.run(["open", "-R", str(target)], check=False)

        def on_refresh_creds(self, _sender):
            if self.switcher._get_current_account() is None:
                rumps.alert(title="claude-swap",
                            message="No active Claude Code login detected. Log in first.")
                return
            try:
                self.switcher.add_account(slot=None)
            except CredentialReadError:
                # Almost always a launchd/login-agent Keychain block: the active
                # credential lives in the macOS Keychain, which a background agent
                # can't read (the security call times out). Point at the fix.
                rumps.alert(
                    title="claude-swap",
                    message="Couldn't read the active credential. If the menu bar is running "
                            "as a background/login agent, macOS blocks its Keychain access — "
                            "quit and relaunch it from a Terminal with: cswap --menubar",
                )
                return
            except ClaudeSwitchError as e:
                rumps.alert(title="claude-swap", message=str(e))
                return
            self.refresh_async()

        def on_refresh_now(self, _sender):
            self.refresh_async(full=True)  # explicit user refresh → full pass

        def on_quit(self, _sender):
            self._stop_engine()
            rumps.quit_application()

        def on_toggle_name(self, _sender):
            self.settings.show_account_name = not self.settings.show_account_name
            self._save_and_rebuild()

        def on_toggle_scoped(self, _sender):
            self.settings.title_scoped = not self.settings.title_scoped
            self._save_and_rebuild()

        def _make_title_pct(self, mode):
            def cb(_sender):
                self.settings.title_pct = mode
                self._save_and_rebuild()
            return cb

        def _make_interval(self, secs):
            def cb(_sender):
                self.settings.refresh_interval = secs
                # rumps 0.4.0's Timer.interval setter is a no-op while running
                # unless a full interval has elapsed; stop/start forces the new
                # cadence to take effect immediately.
                self.refresh_timer.stop()
                self.refresh_timer.interval = secs
                self.refresh_timer.start()
                self._save_and_rebuild()
            return cb

        def on_toggle_autoswitch(self, _sender):
            self.settings.auto_switch_enabled = not self.settings.auto_switch_enabled
            self.settings.save(settings_path)
            if self.settings.auto_switch_enabled:
                self._start_engine()
            else:
                self._stop_engine()
            self.rebuild_menu()

        def _make_threshold(self, pct):
            def cb(_sender):
                try:
                    set_setting(self.switcher.backup_dir, "autoswitch.threshold", str(pct))
                except Exception as e:
                    rumps.alert(title="claude-swap", message=f"Couldn't set threshold: {e}")
                    return
                self._restart_engine()  # apply immediately if running
                self.rebuild_menu()
            return cb

        def on_toggle_resume(self, _sender):
            new = not self._resume_stopped()
            try:
                set_setting(
                    self.switcher.backup_dir,
                    "autoswitch.resumeStoppedSessions",
                    "true" if new else "false",
                )
            except Exception as e:
                rumps.alert(title="claude-swap", message=f"Couldn't set: {e}")
                return
            self._restart_engine()  # apply immediately if running
            self.rebuild_menu()

        def _set_autoswitch(self, key: str, value: str, what: str) -> None:
            """Persist one core auto-switch setting and apply it to a live engine.

            An empty value means "back to the default", which ``set_setting``
            rejects outright (it reads as a typo from the CLI, where ``unset``
            is the documented way to clear a key). From a menu there is no typo
            to catch: unchecking the last model IS the request to clear it.
            """
            try:
                if value:
                    set_setting(self.switcher.backup_dir, key, value)
                else:
                    unset_setting(self.switcher.backup_dir, key)
            except Exception as e:
                rumps.alert(title="claude-swap", message=f"Couldn't set {what}: {e}")
                return
            self._restart_engine()  # apply immediately if running
            self.rebuild_menu()

        def on_toggle_limit_scan(self, _sender):
            # Stored as an interval, offered as on/off: 5s versus 3s is not a
            # choice anyone makes, and 0 is the documented way to disable it.
            new = 0.0 if self._limit_scan_on() else AutoSwitchSettings.limit_scan_interval_seconds
            self._set_autoswitch(
                "autoswitch.limitScanIntervalSeconds", str(new), "limit scanning"
            )

        def on_toggle_all_models(self, _sender):
            new = "" if self._models().strip().lower() == MODEL_ALL else MODEL_ALL
            self._set_autoswitch("autoswitch.model", new, "model limits")

        def _make_model_toggle(self, name):
            def cb(_sender):
                self._set_autoswitch(
                    "autoswitch.model", toggle_model(self._models(), name), "model limits"
                )
            return cb

        def _make_strategy(self, name):
            def cb(_sender):
                try:
                    set_setting(self.switcher.backup_dir, "autoswitch.strategy", name)
                except Exception as e:
                    rumps.alert(title="claude-swap", message=f"Couldn't set strategy: {e}")
                    return
                self._restart_engine()  # apply immediately if running
                # Rebuild rather than just re-check: the threshold item's own
                # label depends on the strategy (see _settings_menu).
                self.rebuild_menu()
            return cb

    MenuBarApp().run()
    return 0
