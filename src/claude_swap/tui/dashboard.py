"""Dashboard: static account overview on top, a nested action menu below.

The accounts panel is the monitor (active account full-size, others as
one-line minis); the arrow keys drive the *menu*, not the accounts. Anything
account-targeted opens a context of its own:

- ``s`` / menu "Switch account" → :class:`SwitchScreen` — every account
  full-size, Enter switches, pops back.
- ``w`` / menu "Watch accounts" / ``cswap watch`` → a watch screen chosen by
  ``ui.watch_style``: :class:`ClassicWatchScreen` (default) shows the same
  full cards read-only, :class:`MeterWatchScreen` a vertical gradient-meter
  grid. ``s`` arms selection (cursor appears on the active account), Enter
  switches and *stays watching*, Esc disarms.
- "Remove account" nests into a submenu listing the accounts.

No global command palette: actions live where their context is.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Callable

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Footer, ListView, Static

from claude_swap.autoswitch import (
    AutoSwitchEngine,
    AutoSwitchEvent,
    PollEvent,
    SwitchEvent,
)
from claude_swap.models import AccountsSnapshot
from claude_swap.settings import load_settings
from claude_swap.tui.modals import ConfirmModal
from claude_swap.tui.widgets import (
    AccountItem,
    AccountsPanel,
    MenuItem,
    MetersGrid,
    _active_index,
)

if TYPE_CHECKING:
    from claude_swap.tui.app import CswapApp

FLASH_S = 1.5  # how long a just-refreshed row stays highlighted

MenuEntries = list[tuple[str, str]]  # (label, action_id)

_BACK = ("← back", "back")


class DashboardScreen(Screen):
    BINDINGS = [
        Binding("s", "open_switch", "Switch accounts"),
        Binding("w", "app.open_watch", "Watch"),
        Binding("escape,left", "menu_back", "Back", show=False),
        Binding("q", "app.quit", "Quit"),
        # Power shortcuts; the menu is the discoverable path.
        Binding("g", "app.open_auto", "Auto view", show=False),
        Binding("f", "app.refresh_full", "Refresh usage", show=False),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]

    app: "CswapApp"

    def __init__(self) -> None:
        super().__init__()
        # Stack of (title, entries); depth 1 = root menu.
        self._menu_stack: list[tuple[str, MenuEntries]] = []

    def compose(self) -> ComposeResult:
        yield AccountsPanel(id="accounts-panel")
        yield Static("", id="menu-title")
        yield ListView(id="menu")
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#menu", ListView).focus()
        await self._push_menu("menu", self._root_entries())

    # -- menu plumbing --------------------------------------------------------

    def _root_entries(self) -> MenuEntries:
        # No "Refresh" entry: every view auto-refreshes, so a menu item would
        # wrongly imply the user has to. `f` stays as a hidden escape hatch.
        return [
            ("Switch account…", "switch"),
            ("Watch accounts", "watch"),
            ("Auto-switch view", "auto"),
            ("Add account…", "add-menu"),
            ("Disable / enable account…", "disable-menu"),
            ("Remove account…", "remove-menu"),
            ("Theme…", "theme-menu"),
            ("Quit", "quit"),
        ]

    def _add_entries(self) -> MenuEntries:
        return [
            ("From current Claude Code login", "add-login"),
            ("From a setup-token / API key…", "add-token"),
            _BACK,
        ]

    def _remove_entries(self) -> MenuEntries:
        snap = self.app.snapshot
        entries: MenuEntries = [
            (
                f"{acc.number}  {f'{acc.alias} ({acc.email})' if acc.alias else acc.email}"
                f"  [{acc.display_tag}]",
                f"remove:{acc.number}",
            )
            for acc in (snap.accounts if snap else ())
        ]
        entries.append(_BACK)
        return entries

    def _disable_entries(self) -> MenuEntries:
        """One row per account, labelled with its current state and the action
        selecting it will take (enable a disabled one, disable an active one)."""
        snap = self.app.snapshot
        entries: MenuEntries = []
        for acc in (snap.accounts if snap else ()):
            name = f"{acc.alias} ({acc.email})" if acc.alias else acc.email
            action = "→ enable" if acc.disabled else "→ disable"
            state = "  (disabled)" if acc.disabled else ""
            entries.append(
                (f"{acc.number}  {name}{state}   {action}", f"disable:{acc.number}")
            )
        entries.append(_BACK)
        return entries

    def _theme_entries(self) -> MenuEntries:
        """dark / light / auto, with the active setting marked."""
        current = self.app._theme_name
        entries: MenuEntries = [
            (f"{'●' if name == current else ' '} {name}", f"theme:{name}")
            for name in ("dark", "light", "auto")
        ]
        entries.append(_BACK)
        return entries

    async def _push_menu(self, title: str, entries: MenuEntries) -> None:
        self._menu_stack.append((title, entries))
        await self._render_menu()

    async def _pop_menu(self) -> None:
        if len(self._menu_stack) > 1:
            self._menu_stack.pop()
            await self._render_menu()

    async def _render_menu(self) -> None:
        title, entries = self._menu_stack[-1]
        crumb = " › ".join(t for t, _ in self._menu_stack)
        self.query_one("#menu-title", Static).update(crumb)
        menu = self.query_one("#menu", ListView)
        await menu.clear()
        await menu.extend(
            MenuItem(label, action_id, muted=(action_id == "back"))
            for label, action_id in entries
        )
        menu.index = 0

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, MenuItem):
            await self._dispatch(item.action_id)

    async def _dispatch(self, action_id: str) -> None:
        app = self.app
        actions: dict[str, Callable[[], None]] = {
            "switch": self.action_open_switch,
            "watch": app.action_open_watch,
            "auto": app.action_open_auto,
            "add-login": app.action_add_current,
            "add-token": app.action_add_token,
            "quit": app.exit,
        }
        if action_id == "back":
            await self._pop_menu()
        elif action_id == "add-menu":
            await self._push_menu("add account", self._add_entries())
        elif action_id == "remove-menu":
            await self._push_menu("remove account", self._remove_entries())
        elif action_id.startswith("remove:"):
            number = action_id.split(":", 1)[1]
            snap = app.snapshot
            email = next(
                (a.email for a in (snap.accounts if snap else ()) if a.number == number),
                "?",
            )
            app.confirm_remove(number, email)
        elif action_id == "theme-menu":
            await self._push_menu("theme", self._theme_entries())
        elif action_id.startswith("theme:"):
            name = action_id.split(":", 1)[1]
            app.apply_theme(name)
            app.notify(f"Theme: {name}")
            await self._pop_menu()
        elif action_id == "disable-menu":
            await self._push_menu("disable / enable", self._disable_entries())
        elif action_id.startswith("disable:"):
            number = action_id.split(":", 1)[1]
            app.do_toggle_disabled(number)
            await self._pop_menu()
        else:
            actions[action_id]()

    # -- actions ----------------------------------------------------------------

    def action_open_switch(self) -> None:
        if not isinstance(self.app.screen, SwitchScreen):
            self.app.push_screen(SwitchScreen())

    async def action_menu_back(self) -> None:
        await self._pop_menu()

    def action_cursor_down(self) -> None:
        self.query_one("#menu", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#menu", ListView).action_cursor_up()


class AccountListScreen(Screen):
    """Shared machinery: a live ListView of full account cards.

    Subclasses decide what the cursor does — :class:`SwitchScreen` is
    selection-first, :class:`ClassicWatchScreen` is a monitor that can arm
    selection on demand.
    """

    app: "CswapApp"

    def __init__(self) -> None:
        super().__init__()
        self._numbers: list[str] = []
        self._stamps: dict[str, float | None] = {}

    def compose(self) -> ComposeResult:
        yield Static("", id="list-title")
        yield ListView(id="accounts")
        yield Footer()

    def on_mount(self) -> None:
        self.watch(self.app, "snapshot", self._on_snapshot)

    async def _on_snapshot(self, snap: AccountsSnapshot | None) -> None:
        if snap is None:
            return
        listview = self.query_one("#accounts", ListView)
        numbers = [acc.number for acc in snap.accounts]
        if numbers != self._numbers:
            first_build = not self._numbers
            previous = listview.index
            # Follow the highlighted account across reorders/removals: the row
            # index alone would leave the cursor on whatever account now sits in
            # that slot, so resolve the selected account's new position first.
            selected = (
                self._numbers[previous]
                if previous is not None and 0 <= previous < len(self._numbers)
                else None
            )
            await listview.clear()
            await listview.extend(AccountItem(acc) for acc in snap.accounts)
            self._numbers = numbers
            followed = numbers.index(selected) if selected in numbers else previous
            listview.index = (
                self._index_after_build(snap, first_build, followed)
                if numbers
                else None
            )
        else:
            for item, acc in zip(listview.query(AccountItem), snap.accounts):
                item.set_account(acc)
        self._flash_updated(snap, listview)

    def _index_after_build(
        self, snap: AccountsSnapshot, first_build: bool, previous: int | None
    ) -> int | None:
        """Where the cursor lands after the list is (re)built."""
        if first_build:
            return _active_index(snap)
        return min(previous or 0, len(snap.accounts) - 1)

    def _flash_updated(self, snap: AccountsSnapshot, listview: ListView) -> None:
        """Briefly highlight rows whose stored measurement just advanced."""
        new_stamps = {acc.number: acc.usage.fetched_at for acc in snap.accounts}
        if self._stamps:
            changed = {
                num
                for num, ts in new_stamps.items()
                if ts is not None and ts != self._stamps.get(num)
            }
            for item in listview.query(AccountItem):
                if item.number in changed and not item.has_class("flash"):
                    item.add_class("flash")
                    self.set_timer(FLASH_S, partial(item.remove_class, "flash"))
        self._stamps = new_stamps

    def action_cursor_down(self) -> None:
        self.query_one("#accounts", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#accounts", ListView).action_cursor_up()


class SwitchScreen(AccountListScreen):
    """All accounts, full-size and alive: arrows pick, Enter switches."""

    BINDINGS = [
        # priority: outranks the focused ListView's own (hidden) enter binding
        # so "Switch" is visible in the footer; the action delegates right back
        # to the list cursor, so behavior is identical.
        Binding("enter", "select_highlighted", "Switch", priority=True),
        Binding("b", "app.switch_best", "Best pick"),
        Binding("escape,q,s", "back", "Back"),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]

    def on_mount(self) -> None:
        self.query_one("#list-title", Static).update("switch to which account?")
        self.query_one("#accounts", ListView).focus()
        super().on_mount()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, AccountItem):
            self.app.do_switch(item.number)
            self.app.pop_screen()

    def action_select_highlighted(self) -> None:
        listview = self.query_one("#accounts", ListView)
        if listview.display:
            listview.action_select_cursor()

    def action_back(self) -> None:
        self.app.pop_screen()


class MeterWatchScreen(Screen):
    """Live monitor as a vertical gradient-meter grid, hands-off by default.

    ``s`` arms selection (cursor appears on the active account); Enter then
    switches and stays here — you keep watching on the new account. Esc
    disarms selection first, then leaves the screen. Opt in via
    ``ui.watch_style = meters``.
    """

    app: "CswapApp"

    # The grid is self-evident, so monitor mode shows no header; the title row
    # is reused only for the selection prompt once selection is armed.
    _WATCH_TITLE = ""
    _SELECT_TITLE = "switch to which account? · enter confirm · esc cancel"

    BINDINGS = [
        Binding("s", "toggle_select", "Switch"),
        Binding("enter", "select_highlighted", "Confirm", priority=True),
        Binding("a", "toggle_auto", "Auto"),
        Binding("L", "toggle_live", "Live", key_display="L"),
        Binding("f", "app.refresh_full", "Refresh", show=False),
        Binding("ctrl+v", "app.toggle_watch_style", "Layout"),
        Binding("escape,q", "back", "Back"),
        Binding("left,h", "nav_left", show=False),
        Binding("right,l", "nav_right", show=False),
        Binding("up,k", "nav_up", show=False),
        Binding("down,j", "nav_down", show=False),
    ]

    # Headroom margin (percentage points above the threshold) at which the
    # predicted target's breathing reaches full tempo… and where it is at rest.
    _URGENT_MARGIN_PCT = 0.0
    _CALM_MARGIN_PCT = 20.0

    def __init__(self) -> None:
        super().__init__()
        self._selecting = False
        self._engine: AutoSwitchEngine | None = None
        self._next_target: str | None = None

    def compose(self) -> ComposeResult:
        yield Static("", id="list-title")
        yield MetersGrid(id="meters")
        # The auto-switch readout shares the footer's row, at the right edge,
        # so the meters keep every row while an engine runs. It is a sibling
        # of the Footer, not a child: Footer recomposes on every binding
        # change and drops anything composed into it.
        with Horizontal(id="footer-row"):
            yield Footer()
            yield Static("", id="auto-readout")

    def on_mount(self) -> None:
        self._set_title(self._WATCH_TITLE)
        self._refresh_readout()

    def on_unmount(self) -> None:
        # Leaving the screen (Esc, or a Ctrl+V layout swap) must not leave an
        # engine running behind it. The widgets are already gone by now, so
        # only the engine and the poller mode are reset.
        self._halt_engine()

    # -- auto-switch engine ---------------------------------------------------
    # Mirrors AutoScreen: a dry-run engine runs the real decision loop and
    # reports what it WOULD do, which is what the grid highlights; only a
    # confirmed live engine ever switches.

    def _start_engine(self, *, dry_run: bool) -> None:
        # The callback names its engine, so a replaced engine finishing its
        # last tick cannot drive the grid.
        engine = AutoSwitchEngine(
            self.app.switcher,
            load_settings(self.app.switcher.backup_dir),
            lambda event: self._emit_from_thread(engine, event),
            dry_run=dry_run,
        )
        self._engine = engine
        self.run_worker(
            engine.run_loop,
            thread=True,
            group="engine",
            exit_on_error=False,
            name=f"meters-engine-{'dry' if dry_run else 'live'}",
        )
        # The engine fetches usage; the app poller only reads the store.
        self.app.set_store_only(True)

    def _halt_engine(self) -> None:
        if self._engine is None:
            return
        self._engine.stop()
        self._engine = None
        self._next_target = None
        # The engine pinned the poll planner to its settings; unpin so
        # ordinary refreshes follow the settings file again.
        self.app.switcher.clear_poll_policy_inputs()
        self.app.set_store_only(False)

    def _stop_engine(self) -> None:
        self._halt_engine()
        self.query_one("#meters", MetersGrid).set_predicted(None, 0.0)
        self._refresh_title()

    def _emit_from_thread(
        self, engine: AutoSwitchEngine, event: AutoSwitchEvent
    ) -> None:
        """Engine ``on_event`` callback — runs on the worker thread."""
        try:
            self.app.call_from_thread(self._on_engine_event, engine, event)
        except Exception:
            # App/screen tearing down mid-tick; the event has nowhere to go.
            pass

    def _on_engine_event(
        self, engine: AutoSwitchEngine, event: AutoSwitchEvent
    ) -> None:
        if not self.is_attached or engine is not self._engine:
            return
        if isinstance(event, PollEvent):
            self._next_target = event.next_target
            self.query_one("#meters", MetersGrid).set_predicted(
                event.next_target, self._urgency(event)
            )
            self._refresh_title()
        elif isinstance(event, SwitchEvent) and not event.dry_run:
            # The landed account is active now, not "next": drop the stale
            # prediction until the engine re-ranks from its new vantage point.
            grid = self.query_one("#meters", MetersGrid)
            grid.set_predicted(None, 0.0)
            grid.start_sweep(
                str(event.from_ref["number"]) if event.from_ref else None,
                str(event.to_ref["number"]),
            )
            self._next_target = None
            self._refresh_title()
            self.app.request_refresh()

    def _urgency(self, event: PollEvent) -> float:
        """0 while the active account has ≥20 points of headroom margin above
        the threshold, rising to 1 as that margin closes."""
        active = event.active or {}
        headroom = event.headroom.get(str(active.get("number", "")))
        if headroom is None:
            return 0.0
        margin = headroom - (100.0 - event.threshold)
        span = self._CALM_MARGIN_PCT - self._URGENT_MARGIN_PCT
        calm = max(0.0, min(1.0, (margin - self._URGENT_MARGIN_PCT) / span))
        return 1.0 - calm

    def action_toggle_auto(self) -> None:
        if self._engine is None:
            self._start_engine(dry_run=True)
            self._refresh_title()
        else:
            self._stop_engine()
        self.refresh_bindings()  # the footer's "L Live" hint follows the engine

    def action_toggle_live(self) -> None:
        if self._engine is None:
            return
        if self._engine.dry_run:
            self.app.push_screen(
                ConfirmModal(
                    "Go live? claude-swap will switch your active account "
                    "automatically when the threshold is reached.\n\n"
                    "(Same behavior as running `cswap auto` in a terminal.)",
                    title="Go live",
                    yes_label="Go live",
                ),
                self._on_live_confirm,
            )
        else:
            self._restart_engine(dry_run=True)

    def _on_live_confirm(self, confirmed: bool | None) -> None:
        if confirmed:
            self._restart_engine(dry_run=False)

    def _restart_engine(self, *, dry_run: bool) -> None:
        self._halt_engine()
        self._start_engine(dry_run=dry_run)
        self._refresh_title()

    def _set_title(self, text: str) -> None:
        """Set the title text and collapse its row when there's none, so the
        empty header doesn't eat vertical space in monitor mode."""
        title = self.query_one("#list-title", Static)
        title.update(text)
        title.display = bool(text)

    def _refresh_title(self) -> None:
        """The selection prompt while armed; otherwise nothing (row collapsed).
        The auto-switch readout has its own place in the footer."""
        self._set_title(self._SELECT_TITLE if self._selecting else self._WATCH_TITLE)
        self._refresh_readout()

    def _refresh_readout(self) -> None:
        """Footer readout: ``dry-run · next → <alias>`` or ``LIVE · next → …``
        while an engine runs; hidden otherwise."""
        readout = self.query_one("#auto-readout", Static)
        if self._engine is None:
            readout.update("")
            readout.display = False
            return
        mode = "dry-run" if self._engine.dry_run else "LIVE"
        readout.update(f"{mode} · next → {self._next_target_label()}")
        readout.set_class(not self._engine.dry_run, "live")
        readout.display = True

    def _next_target_label(self) -> str:
        if self._next_target is None:
            return "none"
        snap = self.app.snapshot
        for acc in snap.accounts if snap else ():
            if acc.number == self._next_target:
                return acc.alias or acc.email.split("@", 1)[0]
        return self._next_target

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "select_highlighted" and not self._selecting:
            return False  # hidden and inert until selection is armed
        if action == "toggle_live" and self._engine is None:
            return False  # live only arms on top of a running prediction
        return True

    def _set_selecting(self, on: bool) -> None:
        grid = self.query_one("#meters", MetersGrid)
        if on:
            snap = self.app.snapshot
            grid.cursor = _active_index(snap) if snap and snap.accounts else 0
        else:
            grid.cursor = None
        self._selecting = on
        self._refresh_title()
        self.refresh_bindings()
        grid.refresh(layout=True)

    def action_toggle_select(self) -> None:
        self._set_selecting(not self._selecting)

    def action_select_highlighted(self) -> None:
        if not self._selecting:
            return
        num = self.query_one("#meters", MetersGrid).selected_number()
        if num:
            self.app.do_switch(num)
        self._set_selecting(False)  # stay here, keep watching

    def action_back(self) -> None:
        if self._selecting:
            self._set_selecting(False)
        else:
            self.app.pop_screen()

    def action_nav_left(self) -> None:
        if self._selecting:
            self.query_one("#meters", MetersGrid).move_cursor(-1, 0)

    def action_nav_right(self) -> None:
        if self._selecting:
            self.query_one("#meters", MetersGrid).move_cursor(1, 0)

    def action_nav_up(self) -> None:
        if self._selecting:
            self.query_one("#meters", MetersGrid).move_cursor(0, -1)

    def action_nav_down(self) -> None:
        if self._selecting:
            self.query_one("#meters", MetersGrid).move_cursor(0, 1)


class ClassicWatchScreen(AccountListScreen):
    """Live monitor of every account as full horizontal-bar cards, hands-off
    by default.

    ``s`` arms selection (cursor appears on the active account); Enter then
    switches and stays here — you keep watching on the new account. Esc
    disarms selection first, then leaves the screen. This is the default
    ``cswap watch`` layout (``ui.watch_style = classic``).
    """

    _WATCH_TITLE = "watching all accounts"
    _SELECT_TITLE = "switch to which account? · enter confirm · esc cancel"

    BINDINGS = [
        Binding("s", "toggle_select", "Switch"),
        Binding("enter", "select_highlighted", "Confirm", priority=True),
        Binding("f", "app.refresh_full", "Refresh", show=False),
        Binding("ctrl+v", "app.toggle_watch_style", "Layout"),
        Binding("escape,q", "back", "Back"),
        Binding("down,j", "nav_down", show=False),
        Binding("up,k", "nav_up", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._selecting = False

    def on_mount(self) -> None:
        self.watch(self.app, "refresh_status", self._on_refresh_status)
        self.query_one("#list-title", Static).update(self._title_text())
        super().on_mount()

    def _title_text(self) -> str:
        if self._selecting:
            return self._SELECT_TITLE
        status = self.app.refresh_status
        return f"{self._WATCH_TITLE} · {status}" if status else self._WATCH_TITLE

    def _on_refresh_status(self, status: str) -> None:
        if not self._selecting:
            self.query_one("#list-title", Static).update(self._title_text())

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "select_highlighted" and not self._selecting:
            return False  # hidden and inert until selection is armed
        return True

    def _index_after_build(
        self, snap: AccountsSnapshot, first_build: bool, previous: int | None
    ) -> int | None:
        if not self._selecting:
            return None  # monitor mode: no cursor at all
        return super()._index_after_build(snap, first_build, previous)

    def _set_selecting(self, on: bool) -> None:
        self._selecting = on
        listview = self.query_one("#accounts", ListView)
        title = self.query_one("#list-title", Static)
        if on:
            snap = self.app.snapshot
            if snap is not None and snap.accounts:
                listview.index = _active_index(snap)
            listview.focus()
            title.update(self._SELECT_TITLE)
        else:
            listview.index = None
            self.set_focus(None)
            title.update(self._title_text())
        self.refresh_bindings()

    def action_toggle_select(self) -> None:
        self._set_selecting(not self._selecting)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if not self._selecting:
            return  # e.g. a stray click while just watching
        item = event.item
        if isinstance(item, AccountItem):
            self.app.do_switch(item.number)
            self._set_selecting(False)  # stay here, keep watching

    def action_select_highlighted(self) -> None:
        if self._selecting:
            self.query_one("#accounts", ListView).action_select_cursor()

    def action_back(self) -> None:
        if self._selecting:
            self._set_selecting(False)
        else:
            self.app.pop_screen()

    def action_nav_down(self) -> None:
        listview = self.query_one("#accounts", ListView)
        if self._selecting:
            listview.action_cursor_down()
        else:
            listview.scroll_down(animate=False)

    def action_nav_up(self) -> None:
        listview = self.query_one("#accounts", ListView)
        if self._selecting:
            listview.action_cursor_up()
        else:
            listview.scroll_up(animate=False)


def watch_screen(watch_style: str) -> Screen:
    """The ``cswap watch`` screen for a ``ui.watch_style`` value.

    ``meters`` opts into the vertical gradient-meter grid; anything else
    (the default ``classic``) gets the horizontal-bar account list.
    """
    if watch_style == "meters":
        return MeterWatchScreen()
    return ClassicWatchScreen()
