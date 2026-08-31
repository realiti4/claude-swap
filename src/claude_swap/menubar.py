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

from claude_swap import pace
from claude_swap.logging_config import LOG_MAX_BYTES
from claude_swap.exceptions import ClaudeSwitchError, CredentialReadError
from claude_swap.switcher import SENTINEL_NOTES

ICON = "⇄"
REFRESH_CHOICES: tuple[int, ...] = (30, 60, 300)
AUTO_THRESHOLD_CHOICES: tuple[int, ...] = (80, 90, 95, 98)
TITLE_PCT_CHOICES: tuple[str, ...] = ("off", "5h", "7d", "both")
SWITCH_HISTORY_LIMIT = 10
NOTIFICATION_BUNDLE_ID = "com.claude-swap.menubar"
TITLE_PCT_LABELS: dict[str, str] = {
    "off": "None",
    "5h": "Session (5h)",
    "7d": "Weekly (7d)",
    "both": "Both (5h · 7d)",
}
INTERVAL_LABELS: dict[int, str] = {30: "30 seconds", 60: "60 seconds", 300: "5 minutes"}
NO_ACCOUNTS = "No managed accounts"
NO_HISTORY = "No switches logged yet"
# The switch-history submenu needs only the last few matching lines, so the
# model build reads a tail rather than the whole file. Sized to the log
# rotation limit (``logging_config.LOG_MAX_BYTES``) so the window can never
# be the reason an entry is missing: switch lines are sparse among usage
# lines, and a smaller window silently truncates the history.
LOG_TAIL_BYTES = LOG_MAX_BYTES


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

    Repeats collapse. Two identical switches inside one minute render as the
    same string and carry no extra information, and de-duplicating *before*
    the limit stops one from consuming a slot an older distinct switch could
    fill. It also keeps the menu honest: rumps identifies items by title and
    silently drops a duplicate.
    """
    out: list[str] = []
    for line in log_text.splitlines():
        m = _SWITCH_LOG_RE.search(line)
        if not m:
            continue
        stamp = line.split(" - ", 1)[0].strip()[:16]  # "YYYY-MM-DD HH:MM"
        out.append(f"{m.group(1)} → {m.group(2)}   {stamp}")
    return list(_unique(reversed(out))[:limit])


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


# ---- menu model (pure) ------------------------------------------------------
# The menu's variable content as plain data, so a refresh tick can decide what
# the UI needs to do without touching rumps. This exists because rebuilding the
# whole menu on every tick leaks: rumps registers each MenuItem carrying a
# callback in the global ``NSApp._ns_to_py_and_callback`` dict and never
# unregisters it, so every discarded item — and the closure holding the app —
# stays reachable for the life of the process.


def read_log_tail(path: Path, max_bytes: int = LOG_TAIL_BYTES) -> str:
    """Last ``max_bytes`` of a text file, decoded leniently.

    Returns "" if the file is missing or unreadable. When the file is longer
    than the window the first (probably partial) line is dropped, so callers
    only ever see whole lines.
    """
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            raw = fh.read()
    except OSError:
        return ""
    if size > max_bytes:
        _, _, raw = raw.partition(b"\n")
    return raw.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class MenuRow:
    """One variable menu row: the text shown and its check-mark state."""

    label: str
    state: int = 0


@dataclass(frozen=True)
class MenuModel:
    """Everything in the menu that can change between refreshes.

    Two models compare equal exactly when the rendered menu would look
    identical, so an unchanged tick can skip all UI work. ``shape`` is the part
    that cannot be fixed up by relabelling: menu callbacks are bound per
    account slot when the item is created, so a changed slot list has to
    rebuild rather than relabel — otherwise a row would keep a callback
    pointing at whichever account used to occupy its position.
    """

    title: str
    slots: tuple[str, ...]
    accounts: tuple[MenuRow, ...]
    remove: tuple[MenuRow, ...]
    disable: tuple[MenuRow, ...]
    history: tuple[str, ...]
    settings_rows: tuple[MenuRow, ...]

    @property
    def shape(self) -> tuple:
        """What must match for an in-place update to be safe."""
        return (self.slots, len(self.history), len(self.settings_rows))


def settings_rows(settings: MenuBarSettings, threshold: int) -> tuple[MenuRow, ...]:
    """Settings check-marks, in the order the settings submenu builds them."""
    rows = [MenuRow("Show account name in menu bar", 1 if settings.show_account_name else 0)]
    rows += [
        MenuRow(TITLE_PCT_LABELS[mode], 1 if settings.title_pct == mode else 0)
        for mode in TITLE_PCT_CHOICES
    ]
    rows.append(MenuRow("Show model limits in title", 1 if settings.title_scoped else 0))
    rows += [
        MenuRow(INTERVAL_LABELS[secs], 1 if settings.refresh_interval == secs else 0)
        for secs in REFRESH_CHOICES
    ]
    rows.append(MenuRow("Auto-switch accounts", 1 if settings.auto_switch_enabled else 0))
    rows += [
        MenuRow(f"{pct}%", 1 if threshold == pct else 0) for pct in AUTO_THRESHOLD_CHOICES
    ]
    return tuple(rows)


def _unique(lines) -> tuple[str, ...]:
    """De-duplicate while preserving order.

    rumps identifies menu items by title and silently drops a duplicate, so
    a model that lists one would not match the menu it produces.
    """
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        if line not in seen:
            seen.add(line)
            out.append(line)
    return tuple(out)


def build_menu_model(
    snapshot: dict,
    settings: MenuBarSettings,
    *,
    threshold: int,
    history: list[str] | tuple[str, ...] = (),
    now: float | None = None,
) -> MenuModel:
    """Render the menu's variable content. Pure: no rumps, no I/O."""
    accounts: list[MenuRow] = []
    remove: list[MenuRow] = []
    disable: list[MenuRow] = []
    slots: list[str] = []
    for num, email, is_active, display, _last_good, alias, disabled, fetched_at in snapshot["accounts"]:
        slots.append(str(num))
        accounts.append(
            MenuRow(
                format_account_label(
                    num, email, display, now, alias=alias,
                    disabled=disabled, fetched_at=fetched_at,
                ),
                1 if is_active else 0,
            )
        )
        named = f"{alias}  ({email})" if alias else email
        remove.append(MenuRow(f"{num}  {alias}  ({email})" if alias else f"{num}  {email}"))
        disable.append(MenuRow(f"{num}  {named}", 1 if disabled else 0))
    if not slots:
        accounts = [MenuRow(NO_ACCOUNTS)]
        remove = [MenuRow(NO_ACCOUNTS)]
        disable = [MenuRow(NO_ACCOUNTS)]
    return MenuModel(
        title=format_title(
            snapshot["active_email"],
            snapshot["active_usage"],
            settings,
            now,
            alias=snapshot.get("active_alias"),
        ),
        slots=tuple(slots),
        accounts=tuple(accounts),
        remove=tuple(remove),
        disable=tuple(disable),
        history=_unique(history) if history else (NO_HISTORY,),
        settings_rows=settings_rows(settings, threshold),
    )


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

    from claude_swap.autoswitch import AutoSwitchEngine
    from claude_swap.settings import load_settings, set_setting
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
            self._config_path = switcher._get_claude_config_path()
            self._config_mtime = 0.0
            self._last_usage_log: dict = {}  # account num -> last-logged (5h, 7d) key
            # Auto-switch engine (the same one `cswap auto` runs), hosted in a
            # background thread while enabled.
            self._engine = None
            self._engine_events: list = []
            self._event_lock = threading.Lock()
            # Menu state: the last rendered model plus the live items it
            # produced, so a refresh can relabel in place instead of
            # recreating (and stranding) every item.
            self._model = None
            self._items: dict[str, list] = {}
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
                self.sync_menu()
            self._detect_active_change()
            self._drain_engine_events()

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

        # ---- menu construction -----------------------------------------------
        def _menu_model(self):
            """Current menu content as plain data (see ``build_menu_model``)."""
            return build_menu_model(
                self.snapshot,
                self.settings,
                threshold=self._threshold(),
                history=parse_switch_history(read_log_tail(log_path)),
            )

        def sync_menu(self):
            """Bring the menu in line with the current snapshot.

            Does the least work that can be correct: nothing at all when the
            rendered content is unchanged, an in-place relabel when only text
            or check-marks moved, and a full rebuild only when items have to be
            created or destroyed. Rebuilding unconditionally on every tick is
            what leaked — see ``MenuModel``.
            """
            model = self._menu_model()
            if model == self._model:
                return
            if self._model is None or model.shape != self._model.shape:
                self.rebuild_menu(model)
            else:
                self._apply_model(model)

        def _apply_model(self, model):
            """Update the existing menu items in place. Creates nothing."""
            self._model = None  # only restored once the menu matches again
            self.title = model.title
            for section in ("accounts", "remove", "disable"):
                rows = getattr(model, section)
                for item, row in zip(self._items[section], rows, strict=True):
                    item.title = row.label
                    item.state = row.state
            for item, line in zip(self._items["history"], model.history, strict=True):
                item.title = line
            for item, row in zip(self._items["settings"], model.settings_rows, strict=True):
                item.state = row.state
            self._model = model

        def rebuild_menu(self, model=None):
            """Recreate every menu item from scratch.

            Only for structural change — a different set of account slots, a
            different number of history lines. Every item created here lands in
            rumps' global callback map, so the outgoing ones are unregistered
            first.
            """
            model = self._menu_model() if model is None else model
            # Cleared up front: a failure below leaves the menu half-built, and
            # a stale _model would route the next tick into an in-place update
            # against items that are no longer there. None forces a rebuild.
            self._model = None
            self._forget_menu_items()
            self._items = {k: [] for k in ("accounts", "remove", "disable", "history", "settings")}
            self.title = model.title
            self.menu.clear()

            for index, row in enumerate(model.accounts):
                callback = self._make_switch_to(model.slots[index]) if model.slots else None
                item = rumps.MenuItem(row.label, callback=callback)
                item.state = row.state
                self._items["accounts"].append(item)

            self.menu = [
                *self._items["accounts"],
                None,
                rumps.MenuItem("Rotate to next", callback=self._switch(None)),
                rumps.MenuItem("Switch to best", callback=self._switch("best")),
                rumps.MenuItem("Next available", callback=self._switch("next-available")),
                None,
                self._add_menu(),
                self._disable_menu(model),
                self._remove_menu(model),
                rumps.MenuItem("Refresh current credentials", callback=self.on_refresh_creds),
                self._history_menu(model),
                None,
                self._settings_menu(model),
                rumps.MenuItem("Refresh now", callback=self.on_refresh_now),
                rumps.MenuItem("Quit", callback=self.on_quit),
            ]
            self._model = model

        def _forget_menu_items(self):
            """Unregister the outgoing menu items from rumps' global callback map.

            ``rumps`` adds every MenuItem carrying a callback to
            ``NSApp._ns_to_py_and_callback`` and never removes it — ``clear()``
            only empties the NSMenu. Left alone, each rebuild strands its items,
            and the closures holding this app, for the life of the process.
            Guarded: a rumps that reorganises this internal must not break the
            menu, and the cost of a miss is the old leak, not a crash.
            """
            try:
                registry = rumps.rumps.NSApp._ns_to_py_and_callback
            except AttributeError:
                self.switcher._logger.warning(
                    "rumps callback registry not found; menu rebuilds will leak"
                )
                return

            def forget(item):
                nsitem = getattr(item, "_menuitem", None)
                if nsitem is not None:
                    registry.pop(nsitem, None)
                if isinstance(item, rumps.MenuItem):
                    for child in list(item.values()):
                        forget(child)

            try:
                for item in list(self.menu.values()):
                    forget(item)
                # Also the items we hold directly: rumps drops a duplicate
                # title on the floor, so one can exist without ever having
                # been reachable from the menu.
                for tracked in self._items.values():
                    for item in tracked:
                        forget(item)
            except Exception:
                self.switcher._logger.warning(
                    "menu registry cleanup skipped", exc_info=True
                )

        def _add_menu(self):
            menu = rumps.MenuItem("Add account")
            menu.add(rumps.MenuItem("From current login", callback=self.on_add_login))
            if hasattr(self.switcher, "add_account_from_token"):
                menu.add(rumps.MenuItem("From setup-token…", callback=self.on_add_token))
            return menu

        def _remove_menu(self, model):
            menu = rumps.MenuItem("Remove account")
            for index, row in enumerate(model.remove):
                callback = self._make_remove(model.slots[index]) if model.slots else None
                item = rumps.MenuItem(row.label, callback=callback)
                self._items["remove"].append(item)
                menu.add(item)
            return menu

        def _disable_menu(self, model):
            menu = rumps.MenuItem("Disable / enable account")
            for index, row in enumerate(model.disable):
                callback = (
                    self._make_toggle_disabled(model.slots[index]) if model.slots else None
                )
                item = rumps.MenuItem(row.label, callback=callback)
                # A check-mark reads as "held out of rotation" — same glyph the
                # active row uses, but here it means disabled, not selected.
                item.state = row.state
                self._items["disable"].append(item)
                menu.add(item)
            return menu

        def _history_menu(self, model):
            menu = rumps.MenuItem("Switch history")
            for line in model.history:
                item = rumps.MenuItem(line, callback=None)
                self._items["history"].append(item)
                menu.add(item)
            menu.add(None)
            menu.add(rumps.MenuItem("Open full log…", callback=self.on_open_log))
            return menu

        def _settings_menu(self, model):
            rows = iter(model.settings_rows)
            menu = rumps.MenuItem("Settings")

            def track(item):
                self._items["settings"].append(item)
                return item

            def leaf(callback):
                row = next(rows)
                item = rumps.MenuItem(row.label, callback=callback)
                item.state = row.state
                return track(item)

            menu.add(leaf(self.on_toggle_name))

            title_pct = rumps.MenuItem("Title percentage")
            for mode in TITLE_PCT_CHOICES:
                title_pct.add(leaf(self._make_title_pct(mode)))
            menu.add(title_pct)

            menu.add(leaf(self.on_toggle_scoped))

            interval = rumps.MenuItem("Refresh interval")
            for secs in REFRESH_CHOICES:
                interval.add(leaf(self._make_interval(secs)))
            menu.add(interval)

            menu.add(leaf(self.on_toggle_autoswitch))

            threshold_menu = rumps.MenuItem("Auto-switch threshold")
            for pct in AUTO_THRESHOLD_CHOICES:
                threshold_menu.add(leaf(self._make_threshold(pct)))
            menu.add(threshold_menu)

            return menu

        # ---- callbacks --------------------------------------------------------
        def _save_and_rebuild(self):
            self.settings.save(settings_path)
            self.sync_menu()

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

        def _make_switch_to(self, num):
            def cb(_sender):
                if self._guard(lambda: self.switcher.switch_to(str(num))):
                    self._notify_switched()
                    self.refresh_async()
            return cb

        def _switch(self, strategy):
            def cb(_sender):
                if self._guard(lambda: self.switcher.switch(strategy=strategy)):
                    self._notify_switched()
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

        def _slot_disabled(self, num) -> bool:
            """Whether slot ``num`` is currently held out of rotation."""
            for account in self.snapshot["accounts"]:
                if str(account[0]) == str(num):
                    return bool(account[6])
            return False

        def _make_toggle_disabled(self, num):
            def cb(_sender):
                # Read the flag now, not at build time: with in-place menu
                # updates this row survives a state change, so a captured
                # value would toggle against a stale reading.
                target = not self._slot_disabled(num)
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
            self.sync_menu()

        def _make_threshold(self, pct):
            def cb(_sender):
                try:
                    set_setting(self.switcher.backup_dir, "autoswitch.threshold", str(pct))
                except Exception as e:
                    rumps.alert(title="claude-swap", message=f"Couldn't set threshold: {e}")
                    return
                self._restart_engine()  # apply immediately if running
                self.sync_menu()
            return cb

    MenuBarApp().run()
    return 0
