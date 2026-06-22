"""macOS menu bar app for claude-swap (``cswap --menubar``).

A thin GUI shell over ``ClaudeAccountSwitcher`` — it never re-implements
account logic. Built on ``rumps`` (an optional extra, macOS only). The pure
helpers below (settings, formatting, plist rendering) are import-safe without
rumps so they can be unit-tested in CI; ``rumps`` is imported lazily inside
the app glue.
"""

from __future__ import annotations

import json
import plistlib
import sys
import threading
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from claude_swap.exceptions import ClaudeSwitchError

ICON = "⇄"
REFRESH_CHOICES: tuple[int, ...] = (30, 60, 300)


@dataclass
class MenuBarSettings:
    """User-configurable menu bar behavior, persisted as JSON."""

    show_account_name: bool = True
    show_quota_pct: bool = True
    refresh_interval: int = 60
    launch_at_login: bool = False
    auto_switch_enabled: bool = False
    auto_switch_threshold: int = 95
    auto_switch_cooldown: int = 600
    auto_switch_interval: int = 0  # 0 == evaluate with each display refresh

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


@dataclass
class MenuBarState:
    """Cooldown/notification timestamps for the auto-switcher, persisted as JSON.

    Separate from MenuBarSettings: settings are user choices, state is runtime
    bookkeeping. Persisting across restarts means a relaunch respects the
    cooldown instead of swapping immediately.
    """

    last_switch_at: float = 0.0
    last_noswap_notify_at: float = 0.0

    @classmethod
    def load(cls, path: Path) -> "MenuBarState":
        """Load state; defaults on missing/corrupt. Int timestamps coerce to float."""
        defaults = cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return defaults
        if not isinstance(raw, dict):
            return defaults
        kwargs = {}
        for f in fields(cls):
            val = raw.get(f.name)
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                kwargs[f.name] = float(val)
        return cls(**kwargs)

    def save(self, path: Path) -> None:
        """Write state as pretty JSON, creating parent directories."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


def tightest_pct(usage: dict | str | None) -> float | None:
    """Highest 5h/7d utilization percentage, or None if unknown.

    Mirrors ``oauth.account_headroom`` (which returns ``100 - max(pct)``) but
    surfaces the utilization itself for display. Spend is excluded — it isn't
    a rate-limit window.
    """
    if not isinstance(usage, dict):
        return None
    pcts = [
        window["pct"]
        for window in (usage.get("five_hour"), usage.get("seven_day"))
        if isinstance(window, dict) and isinstance(window.get("pct"), (int, float))
    ]
    return max(pcts) if pcts else None


def usage_summary(usage: dict | str | None) -> str:
    """One-line usage summary for an account row."""
    if isinstance(usage, str):
        return usage
    if usage is None:
        return "usage unavailable"
    parts: list[str] = []
    h5 = usage.get("five_hour")
    if isinstance(h5, dict) and isinstance(h5.get("pct"), (int, float)):
        parts.append(f"5h {h5['pct']:.0f}%")
    d7 = usage.get("seven_day")
    if isinstance(d7, dict) and isinstance(d7.get("pct"), (int, float)):
        parts.append(f"7d {d7['pct']:.0f}%")
    spend = usage.get("spend")
    if isinstance(spend, dict) and isinstance(spend.get("pct"), (int, float)):
        parts.append(f"$ {spend['pct']:.0f}%")
    return " · ".join(parts) if parts else "usage unavailable"


def format_account_label(num: int, email: str, usage: dict | str | None) -> str:
    """Build one account row's menu label."""
    return f"{num}  {email}  {usage_summary(usage)}"


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
) -> str:
    """Build the menu-bar title from the active account and settings."""
    if active_email is None:
        return ICON
    segments: list[str] = []
    if settings.show_account_name:
        segments.append(_local_part(active_email))
    if settings.show_quota_pct:
        pct = tightest_pct(active_usage)
        if pct is not None:
            segments.append(f"{pct:.0f}%")
    if not segments:
        return ICON
    return f"{ICON} " + " · ".join(segments)


LAUNCH_AGENT_LABEL = "com.claude-swap.menubar"


def launch_agent_path() -> Path:
    """Path to the menu bar LaunchAgent plist."""
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


def render_launch_agent(program_args: list[str]) -> bytes:
    """Render the LaunchAgent plist that starts the menu bar at login."""
    return plistlib.dumps(
        {
            "Label": LAUNCH_AGENT_LABEL,
            "ProgramArguments": list(program_args),
            "RunAtLoad": True,
        }
    )


def set_launch_at_login(enabled: bool, program_args: list[str]) -> None:
    """Install or remove the login LaunchAgent. Removal is idempotent."""
    path = launch_agent_path()
    if enabled:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(render_launch_agent(program_args))
    else:
        path.unlink(missing_ok=True)


def _snapshot(switcher) -> dict:
    """Fetch accounts + usage off the main thread. Returns a render snapshot.

    Shape: ``{"accounts": [(num, email, is_active, usage), ...],
    "active_email": str | None, "active_usage": dict | str | None}``.
    Never raises — failures degrade to empty/unknown so the UI stays alive.
    """
    try:
        accounts_info = switcher._build_accounts_info()
        usages = switcher._collect_usage(accounts_info)
    except Exception:
        switcher._logger.debug("menubar snapshot failed", exc_info=True)
        return {"accounts": [], "active_email": None, "active_usage": None}

    accounts = []
    active_email = None
    active_usage = None
    for (num, email, _org, _uuid, is_active, _creds), usage in zip(accounts_info, usages):
        accounts.append((num, email, is_active, usage))
        if is_active:
            active_email, active_usage = email, usage
    return {
        "accounts": accounts,
        "active_email": active_email,
        "active_usage": active_usage,
    }


def run(switcher) -> int:
    """Entry point for ``cswap --menubar``. Blocks until the user quits."""
    import rumps  # lazy: optional dependency, imported only when launching

    settings_path = switcher.backup_dir / "menubar_settings.json"

    class MenuBarApp(rumps.App):
        def __init__(self):
            super().__init__(ICON, quit_button=None)
            self.switcher = switcher
            self.settings = MenuBarSettings.load(settings_path)
            self.snapshot = {"accounts": [], "active_email": None, "active_usage": None}
            self._dirty = False
            self.rebuild_menu()
            # Background refresh on the user's interval, plus a fast UI-sync tick
            # that applies snapshots produced by worker threads on the main thread.
            self.refresh_timer = rumps.Timer(self.on_refresh_tick, self.settings.refresh_interval)
            self.refresh_timer.start()
            self.sync_timer = rumps.Timer(self.on_sync_tick, 1)
            self.sync_timer.start()
            self.refresh_async()  # first fetch

        # ---- refresh plumbing -------------------------------------------------
        def refresh_async(self):
            threading.Thread(target=self._worker, daemon=True).start()

        def _worker(self):
            snap = _snapshot(self.switcher)
            self.snapshot = snap
            self._dirty = True  # picked up by on_sync_tick on the main thread

        def on_refresh_tick(self, _timer):
            self.refresh_async()

        def on_sync_tick(self, _timer):
            if self._dirty:
                self._dirty = False
                self.rebuild_menu()

        # ---- menu construction ------------------------------------------------
        def rebuild_menu(self):
            self.title = format_title(
                self.snapshot["active_email"], self.snapshot["active_usage"], self.settings
            )
            self.menu.clear()
            account_items = []
            for num, email, is_active, usage in self.snapshot["accounts"]:
                item = rumps.MenuItem(
                    format_account_label(num, email, usage),
                    callback=self._make_switch_to(num),
                )
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
                self._remove_menu(rumps),
                rumps.MenuItem("Refresh current credentials", callback=self.on_refresh_creds),
                None,
                self._settings_menu(rumps),
                rumps.MenuItem("Refresh now", callback=self.on_refresh_now),
                rumps.MenuItem("Quit", callback=rumps.quit_application),
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
            for num, email, _is_active, _usage in accounts:
                menu.add(rumps.MenuItem(f"{num}  {email}", callback=self._make_remove(num)))
            return menu

        def _settings_menu(self, rumps):
            menu = rumps.MenuItem("Settings")
            name_item = rumps.MenuItem("Show account name in menu bar", callback=self.on_toggle_name)
            name_item.state = 1 if self.settings.show_account_name else 0
            pct_item = rumps.MenuItem("Show quota % in menu bar", callback=self.on_toggle_pct)
            pct_item.state = 1 if self.settings.show_quota_pct else 0
            menu.add(name_item)
            menu.add(pct_item)
            interval = rumps.MenuItem("Refresh interval")
            labels = {30: "30 seconds", 60: "60 seconds", 300: "5 minutes"}
            for secs in REFRESH_CHOICES:
                choice = rumps.MenuItem(labels[secs], callback=self._make_interval(secs))
                choice.state = 1 if self.settings.refresh_interval == secs else 0
                interval.add(choice)
            menu.add(interval)
            login_item = rumps.MenuItem("Launch at login", callback=self.on_toggle_login)
            login_item.state = 1 if self.settings.launch_at_login else 0
            menu.add(login_item)
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
            # macOS-only app, so always the Keychain-TTL guidance from
            # ClaudeAccountSwitcher._print_switch_followup.
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
                    if self._guard(lambda: self.switcher.remove_account(str(num), force=True)):
                        self.refresh_async()
            return cb

        def on_add_login(self, _sender):
            if self._guard(self.switcher.add_account):
                self.refresh_async()

        def on_add_token(self, _sender):
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

        def on_refresh_creds(self, _sender):
            if self.switcher._get_current_account() is None:
                rumps.alert(title="claude-swap",
                            message="No active Claude Code login detected. Log in first.")
                return
            if self._guard(lambda: self.switcher.add_account(slot=None)):
                self.refresh_async()

        def on_refresh_now(self, _sender):
            self.refresh_async()

        def on_toggle_name(self, _sender):
            self.settings.show_account_name = not self.settings.show_account_name
            self._save_and_rebuild()

        def on_toggle_pct(self, _sender):
            self.settings.show_quota_pct = not self.settings.show_quota_pct
            self._save_and_rebuild()

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

        def on_toggle_login(self, _sender):
            enabled = not self.settings.launch_at_login
            try:
                set_launch_at_login(enabled, _program_args())
            except OSError as e:
                rumps.alert(title="claude-swap", message=f"Could not update login item: {e}")
                return
            self.settings.launch_at_login = enabled
            self._save_and_rebuild()

    MenuBarApp().run()
    return 0


def _program_args() -> list[str]:
    """Argv that re-launches the menu bar — used for the login plist."""
    return [sys.executable, "-m", "claude_swap", "--menubar"]
