"""Settings: every ``cswap config`` key, changed in place.

On/off settings flip with Enter, choices open a list, and numbers and model
names open a small form. ``d`` puts the highlighted setting back to its
default. Each change is written straight to settings.json (the same writes
as ``cswap config set``/``unset``), and a notice says when it takes effect.
A restart is called for only when something running read the old value at
its start and keeps it:

- an auto-switch loop (``cswap auto``, the menu bar, another TUI's auto
  view) reads the ``autoswitch.*`` settings once, when it starts;
- a ``cswap run`` session read ``run.shareHistory`` when it launched.

Everything else (poll planning, the header probe, the update check, the
theme) reads the file afresh, so the change applies by itself.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Input, Label, ListItem, ListView, Static

from claude_swap.exceptions import ConfigError
from claude_swap.settings import (
    SettingSpec,
    effective_settings,
    format_setting_value,
    parse_setting_value,
    set_setting,
    unset_setting,
)
from claude_swap.tui.data import format_duration
from claude_swap.tui.widgets import MenuItem

if TYPE_CHECKING:
    from claude_swap.tui.app import CswapApp

SECTIONS = {
    "autoswitch": "auto-switch",
    "ui": "display",
    "run": "cswap run",
    "updates": "updates",
    "usage": "usage",
}

LABELS = {
    "autoswitch.threshold": "threshold (5h)",
    "autoswitch.weeklyThreshold": "weekly threshold (7d, models)",
    "autoswitch.intervalSeconds": "check interval",
    "autoswitch.cooldownSeconds": "cooldown between switches",
    "autoswitch.hysteresisPct": "hysteresis",
    "autoswitch.strategy": "strategy",
    "autoswitch.includeApiKeyAccounts": "switch onto API-key accounts",
    "autoswitch.unhealthyTicks": "unhealthy after",
    "autoswitch.model": "per-model weekly limits",
    "ui.theme": "theme",
    "run.shareHistory": "share chat history",
    "updates.check": "daily update check",
    "usage.headerProbe": "setup-token usage probe",
    "usage.headerProbeFable": "Fable weekly limit probe",
    "usage.activePollMinutes": "active account poll time",
    "usage.inactivePollMinutes": "inactive account poll time",
}

#: What an unset optional setting means.
NONE_TEXT = {
    "autoswitch.weeklyThreshold": "same as threshold",
    "autoswitch.model": "off",
}

#: How each kind of auto-switch host picks up new settings.
HOW_TO_RESTART = {
    "cswap auto": "stop it (Ctrl+C) and start it again",
    "menu bar": "turn Auto-switch accounts off and on in its menu",
    "TUI auto view": "leave its auto view and open it again",
}

_SECTION_W = 13
_LABEL_W = 31


def label_for(key: str) -> str:
    return LABELS.get(key, key)


def value_text(spec: SettingSpec, value: object) -> str:
    """A setting's value as the screen shows it."""
    key = spec.dotted
    if value is None:
        return NONE_TEXT.get(key, "none")
    if spec.kind == "bool":
        return "on" if value else "off"
    if key in ("autoswitch.threshold", "autoswitch.weeklyThreshold", "autoswitch.hysteresisPct"):
        return f"{format_setting_value(value)}%"
    if key in ("autoswitch.intervalSeconds", "autoswitch.cooldownSeconds"):
        text = f"{format_setting_value(value)} s"
        if isinstance(value, (int, float)) and value >= 60:
            text += f" ({format_duration(value)})"
        return text
    if key == "autoswitch.unhealthyTicks":
        return f"{value} failed poll" + ("" if value == 1 else "s")
    if key in ("usage.activePollMinutes", "usage.inactivePollMinutes"):
        text = f"{format_setting_value(value)} min"
        if isinstance(value, (int, float)) and value >= 60:
            text += f" ({format_duration(value * 60)})"
        return text
    return format_setting_value(value)


def help_text(spec: SettingSpec) -> str:
    """The setting's description, its range, default and CLI key."""
    parts = [spec.help.rstrip(".") + "."]
    if spec.kind in ("int", "float") and spec.lo is not None and spec.hi is not None:
        parts.append(f"{format_setting_value(spec.lo)} to {format_setting_value(spec.hi)}.")
    elif spec.kind == "choice":
        parts.append("One of: " + ", ".join(spec.choices) + ".")
    parts.append(f"Default: {value_text(spec, spec.default)}.")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# when a change takes effect
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Notice:
    title: str
    message: str
    severity: str  # "information" | "warning"


def _engine_name(engine: dict) -> str:
    host = engine.get("host") or "cswap auto"
    if host == "cswap auto" and engine.get("dryRun"):
        host = "cswap auto --dry-run"
    return f"{host} (pid {engine.get('pid')})"


def change_notice(
    key: str,
    saved: str,
    *,
    engines: list[dict] = (),
    run_sessions: list[tuple[str, int]] = (),
) -> Notice:
    """What to tell the user after ``key`` changed: when it applies, and
    which running things need a restart to see it.

    ``engines`` are the auto-switch loops running now
    (:func:`claude_swap.autoswitch.running_engines`); ``run_sessions`` the
    accounts with live ``cswap run`` sessions, as ``(label, count)``.
    """
    if key.startswith("autoswitch."):
        if not engines:
            return Notice(
                "Saved",
                f"{saved}. No auto-switch is running, so nothing needs a restart: "
                "it applies the next time one starts (the auto view, cswap auto, "
                "the menu bar).",
                "information",
            )
        lines = [f"{saved}. Restart these to apply it (they read settings when they start):"]
        for engine in engines:
            name = _engine_name(engine)
            if key in engine.get("overrides", ()):
                lines.append(
                    f"• {name}: started with a flag that overrides this setting; "
                    "start it again without that flag"
                )
            else:
                how = HOW_TO_RESTART.get(engine.get("host") or "", "restart it")
                lines.append(f"• {name}: {how}")
        return Notice("Restart needed", "\n".join(lines), "warning")
    if key == "run.shareHistory":
        if run_sessions:
            total = sum(count for _label, count in run_sessions)
            who = ", ".join(label for label, _count in run_sessions)
            noun = "session" if total == 1 else "sessions"
            return Notice(
                "Restart needed",
                f"{saved}. {total} running cswap run {noun} ({who}) keep the old "
                "setting until you exit and start them again. New ones use it.",
                "warning",
            )
        return Notice("Saved", f"{saved}. The next cswap run uses it.", "information")
    if key in ("usage.headerProbe", "usage.headerProbeFable"):
        return Notice("Saved", f"{saved}. Applies from the next usage poll.", "information")
    if key in ("usage.activePollMinutes", "usage.inactivePollMinutes"):
        return Notice(
            "Saved", f"{saved}. Each account takes it up from its next usage poll.",
            "information",
        )
    if key == "updates.check":
        return Notice("Saved", f"{saved}. Applies from the next cswap command.", "information")
    if key == "ui.theme":
        return Notice("Saved", f"{saved}.", "information")
    return Notice("Saved", f"{saved}. Applies to cswap commands started from now on.", "information")


# ---------------------------------------------------------------------------
# widgets
# ---------------------------------------------------------------------------


class SettingItem(ListItem):
    """One setting: section (first of its group), label, value, default marker."""

    def __init__(self, spec: SettingSpec, value: object, is_set: bool, *, first: bool) -> None:
        self.key = spec.dotted
        text = Text(no_wrap=True, overflow="ellipsis")
        section = SECTIONS.get(spec.section, spec.section) if first else ""
        text.append(f"{section:<{_SECTION_W}}", style="dim")
        text.append(f"{label_for(self.key):<{_LABEL_W}}")
        text.append(value_text(spec, value), style="bold")
        if not is_set:
            text.append("  (default)", style="dim")
        super().__init__(Static(text))


class ValueModal(ModalScreen):
    """Edit one value. Dismisses with ``("set", raw)``, ``("unset", None)``
    (back to the default) or None (cancelled).

    ``validate(raw)`` raises ``ValueError`` or ``ConfigError`` with the
    message to show; the form stays open, with what was typed, until the
    value passes. An empty entry means "unset" when ``empty_unsets``.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(
        self,
        title: str,
        body: str,
        value: str,
        *,
        validate: Callable[[str], object],
        empty_unsets: bool = False,
        placeholder: str = "",
    ) -> None:
        super().__init__()
        self._title = title
        self._body = body
        self._value = value
        self._validate = validate
        self._empty_unsets = empty_unsets
        self._placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box"):
            yield Label(self._title, classes="modal-title")
            yield Static(self._body, classes="modal-body")
            yield Input(value=self._value, placeholder=self._placeholder, id="value")
            yield Static("", id="form-error", classes="form-error")
            with Horizontal(classes="modal-buttons"):
                yield Button("Save", id="ok")
                yield Button("Default", id="default")
                yield Button("Cancel", id="cancel")
            yield Static("enter save  ·  tab next  ·  esc cancel", classes="modal-hint")

    def on_mount(self) -> None:
        self.query_one("#value", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "default":
            self.dismiss(("unset", None))
        else:
            self._submit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _error(self, message: str) -> None:
        self.query_one("#form-error", Static).update(message)

    def _submit(self) -> None:
        raw = self.query_one("#value", Input).value.strip()
        if not raw:
            if self._empty_unsets:
                self.dismiss(("unset", None))
            else:
                self._error("Enter a value, or press Default.")
            return
        try:
            self._validate(raw)
        except (ValueError, ConfigError) as e:
            # The CLI's "use cswap config unset" hint: here that is Default.
            message = str(e).split("; use 'cswap config unset")[0]
            self._error(message[:1].upper() + message[1:])
            return
        self.dismiss(("set", raw))


class SettingValueModal(ValueModal):
    """Edit one number or text ``cswap config`` setting."""

    def __init__(self, spec: SettingSpec, value: object) -> None:
        super().__init__(
            label_for(spec.dotted).capitalize(),
            help_text(spec),
            "" if value is None else format_setting_value(value),
            validate=lambda raw: parse_setting_value(spec, raw),
            empty_unsets=spec.default is None,
            placeholder=(
                "empty: " + NONE_TEXT.get(spec.dotted, "default")
                if spec.default is None else ""
            ),
        )
        self.spec = spec


# ---------------------------------------------------------------------------
# the screen
# ---------------------------------------------------------------------------


class SettingsScreen(Screen):
    BINDINGS = [
        # priority: shown in the footer, ahead of the ListView's hidden enter
        Binding("enter", "select_highlighted", "Change", priority=True),
        Binding("escape,left", "back", "Back"),
        Binding("d", "reset", "Reset to default"),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]

    app: "CswapApp"

    def __init__(self) -> None:
        super().__init__()
        self._rows: dict[str, tuple[SettingSpec, object, bool]] = {}
        self._choosing: str | None = None  # the choice setting being picked
        self._root_index = 0

    def compose(self) -> ComposeResult:
        yield Static("", id="menu-title")
        yield Static("", id="settings-help")
        yield ListView(id="menu")
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#menu", ListView).focus()
        self._load()
        await self._show_root()

    # -- data -------------------------------------------------------------------

    def _load(self) -> None:
        self._rows = {
            spec.dotted: (spec, value, is_set)
            for spec, value, is_set in effective_settings(self.app.switcher.backup_dir)
        }

    # -- rendering --------------------------------------------------------------

    async def _show_root(self, index: int | None = None) -> None:
        self._choosing = None
        self.query_one("#menu-title", Static).update("settings")
        menu = self.query_one("#menu", ListView)
        await menu.clear()
        items: list[ListItem] = []
        previous_section = None
        for spec, value, is_set in self._rows.values():
            items.append(SettingItem(spec, value, is_set, first=spec.section != previous_section))
            previous_section = spec.section
        items.append(MenuItem("← back", "back", muted=True))
        await menu.extend(items)
        menu.index = min(self._root_index if index is None else index, len(items) - 1)
        self._update_help()

    async def _show_choices(self, key: str) -> None:
        spec, value, _is_set = self._rows[key]
        self._choosing = key
        self.query_one("#menu-title", Static).update(f"settings › {label_for(key)}")
        menu = self.query_one("#menu", ListView)
        await menu.clear()
        entries = []
        for choice in spec.choices:
            mark = "●" if choice == value else " "
            suffix = "  (default)" if choice == spec.default else ""
            entries.append(MenuItem(f"{mark} {choice}{suffix}", f"choice:{choice}"))
        entries.append(MenuItem("← back", "back", muted=True))
        await menu.extend(entries)
        menu.index = spec.choices.index(value) if value in spec.choices else 0
        self._update_help()

    def _highlighted_key(self) -> str | None:
        if self._choosing is not None:
            return self._choosing
        item = self.query_one("#menu", ListView).highlighted_child
        return item.key if isinstance(item, SettingItem) else None

    def _update_help(self) -> None:
        key = self._highlighted_key()
        help_widget = self.query_one("#settings-help", Static)
        if key is None or key not in self._rows:
            help_widget.update("Changes are saved at once (settings.json). d resets one to its default.")
            return
        spec = self._rows[key][0]
        help_widget.update(f"{help_text(spec)}  cswap config: {key}")

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if self._choosing is None:
            index = self.query_one("#menu", ListView).index
            if index is not None:
                self._root_index = index
        self._update_help()

    # -- input ------------------------------------------------------------------

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, SettingItem):
            await self._edit(item.key)
        elif isinstance(item, MenuItem):
            if item.action_id == "back":
                await self.action_back()
            elif item.action_id.startswith("choice:") and self._choosing is not None:
                key = self._choosing
                choice = item.action_id.split(":", 1)[1]
                await self._show_root()
                await self._change(key, choice)

    async def _edit(self, key: str) -> None:
        spec, value, _is_set = self._rows[key]
        if spec.kind == "bool":
            await self._change(key, "false" if value else "true")
        elif spec.kind == "choice":
            await self._show_choices(key)
        else:
            async def done(result) -> None:
                if result is not None:
                    action, raw = result
                    await self._change(key, raw if action == "set" else None)

            self.app.push_screen(SettingValueModal(spec, value), done)

    async def action_back(self) -> None:
        if self._choosing is not None:
            await self._show_root()
        else:
            self.app.pop_screen()

    async def action_reset(self) -> None:
        key = self._highlighted_key()
        if key is not None:
            if self._choosing is not None:
                await self._show_root()
            await self._change(key, None)

    def action_select_highlighted(self) -> None:
        self.query_one("#menu", ListView).action_select_cursor()

    def action_cursor_down(self) -> None:
        self.query_one("#menu", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#menu", ListView).action_cursor_up()

    # -- changing a setting -----------------------------------------------------

    async def _change(self, key: str, raw: str | None) -> None:
        """Write ``raw`` (None: back to the default), redraw, apply what can
        apply in this TUI now, and say when the rest applies."""
        spec, before, _was_set = self._rows[key]
        backup = self.app.switcher.backup_dir
        label = label_for(key).capitalize()
        try:
            if raw is None:
                if not unset_setting(backup, key):
                    self.app.notify(f"{label} is already the default.")
                    return
            else:
                set_setting(backup, key, raw)
        except ConfigError as e:
            self.app.notify(str(e), title="Settings not saved", severity="error")
            return
        self._load()
        after = self._rows[key][1]
        await self._show_root()
        self._apply_here(key, after)
        saved = f"{label}: {value_text(spec, after)}" + (" (default)" if raw is None else "")
        if after == before:
            note = "default" if raw is None else "explicit"
            self.app.notify(f"{saved}. Same value as before ({note} now); nothing changes.")
            return
        self._announce(key, saved)

    def _apply_here(self, key: str, value: object) -> None:
        """What this TUI shows from the setting, updated now."""
        app = self.app
        if key == "ui.theme" and isinstance(value, str):
            app.apply_theme(value, persist=False)
        elif key == "autoswitch.threshold":
            app.threshold_pct = value
        elif key == "autoswitch.weeklyThreshold":
            app.weekly_threshold_pct = value

    def _announce(self, key: str, saved: str) -> None:
        """Look for what still runs on the old value (may run ``ps``: off the
        UI thread) and post the notice."""
        app = self.app
        backup = app.switcher.backup_dir
        snapshot = app.snapshot

        def work() -> None:
            try:
                engines: list[dict] = []
                sessions: list[tuple[str, int]] = []
                if key.startswith("autoswitch."):
                    from claude_swap.autoswitch import running_engines

                    engines = running_engines(backup)
                elif key == "run.shareHistory":
                    sessions = _live_run_sessions(app.switcher, snapshot)
                notice = change_notice(key, saved, engines=engines, run_sessions=sessions)
            except Exception:  # noqa: BLE001 -- the save itself succeeded
                notice = Notice("Saved", f"{saved}.", "information")
            app.call_from_thread(
                app.notify,
                notice.message,
                title=notice.title,
                severity=notice.severity,
                timeout=15 if notice.severity == "warning" else 5,
            )

        self.run_worker(work, thread=True, group="settings-notice", exit_on_error=False)


def _live_run_sessions(switcher, snapshot) -> list[tuple[str, int]]:
    """``(account label, live session count)`` for accounts with ``cswap run``
    sessions running now."""
    lookup = getattr(switcher, "live_session_pids_for", None)
    if lookup is None or snapshot is None:
        return []
    found = []
    for acc in snapshot.accounts:
        try:
            pids = lookup(acc.number, acc.email)
        except Exception:  # noqa: BLE001, S112 -- unknowable is not "running"
            continue
        if pids:
            found.append((f"{acc.number} {acc.alias or acc.email}", len(pids)))
    return found
