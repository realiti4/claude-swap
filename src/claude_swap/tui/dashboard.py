"""Dashboard: static account overview on top, a nested action menu below.

The accounts panel is the monitor (active account full-size, others as
one-line minis); the arrow keys drive the *menu*, not the accounts. Anything
account-targeted opens a context of its own:

- ``s`` / menu "Switch account" → :class:`SwitchScreen` — every account
  full-size, Enter switches, pops back.
- ``w`` / menu "Watch accounts" / ``cswap watch`` → :class:`WatchScreen` —
  the same full cards but read-only: a live monitor. ``s`` arms selection
  (cursor appears on the active account), Enter switches and *stays
  watching*, Esc disarms.
- "Remove account" nests into a submenu listing the accounts.

No global command palette: actions live where their context is.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Callable

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, ListView, Static

from claude_swap import pin
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.models import AccountsSnapshot
from claude_swap.tui.widgets import AccountItem, AccountsPanel, MenuItem

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
        # Rebuild on the poll the app already runs, rather than adding a timer
        # of our own: the pin row has to appear when the extra is installed
        # mid-session, and the label names the pinned account, which changes
        # from the CLI too.
        self.watch(self.app, "snapshot", lambda _s: self.refresh_root_menu())

    # -- menu plumbing --------------------------------------------------------

    async def refresh_root_menu(self) -> None:
        """Rebuild the root menu in place, if that is where the user is.

        The pin row appears only when the extra is installed, and installing
        it is something a user does WHILE the TUI is open. Building the menu
        once at mount meant the row could not appear until a restart — and a
        feature that needs a restart to become visible reads as broken.

        Only when the root is on top: rebuilding under an open submenu would
        yank the rows out from under the cursor.

        THE ROOT MENU NEEDS THE SAME PROTECTION, and for a subtler reason.
        `_render_menu` ends with `menu.index = 0`, which is correct for what
        upstream calls it for — opening a menu, or popping back to one. This
        is the only caller that re-renders a menu the user is already reading,
        so the reset became a side effect of the poll: the cursor jumped home
        every POLL_INTERVAL_S (3s) and nobody slower than one cycle could
        finish choosing.

        Fixed here rather than in `_render_menu`, whose `index = 0` the
        open/pop paths want.

        Two parts, because the entries changing and not changing need
        different handling:

          - Unchanged (the overwhelmingly common case — whether the extra is
            installed changes ~never during a session): skip the rebuild
            entirely. No rebuild, no reset, and no work.
          - Changed: restore by ACTION ID, not by index. Installing the extra
            inserts a row above `remove-menu`, so a remembered integer points
            at a different action than the one the user had selected.
        """
        if len(self._menu_stack) != 1:
            return
        entries = self._root_entries()
        if entries == self._menu_stack[0][1]:
            return
        menu = self.query_one("#menu", ListView)
        items = list(menu.query(MenuItem))
        selected = (
            items[menu.index].action_id
            if menu.index is not None and 0 <= menu.index < len(items)
            else None
        )
        self._menu_stack[0] = ("menu", entries)
        await self._render_menu()
        if selected is not None:
            for i, (_label, action_id) in enumerate(entries):
                if action_id == selected:
                    menu.index = i
                    break

    def _root_entries(self) -> MenuEntries:
        # No "Refresh" entry: every view auto-refreshes, so a menu item would
        # wrongly imply the user has to. `f` stays as a hidden escape hatch.
        return [
            ("Switch account…", "switch"),
            ("Watch accounts", "watch"),
            ("Auto-switch view", "auto"),
            ("Add account…", "add-menu"),
            ("Disable / enable account…", "disable-menu"),
            # Only when the extra is installed — OR when a wiring it left
            # behind is still in .claude.json. A user who never asked for the
            # pin should not see a row for it; one who installs it while the
            # TUI is open sees the row appear (see refresh_root_menu).
            #
            # The second half is what makes the pin removable from here at
            # all: uninstalling the extra is exactly when `--clear` is needed,
            # and the CLI is deliberately able to do it without the package.
            # Gating the row on is_available() alone left a TUI-first user
            # with a wired config and no visible way out.
            *(
                # The row answers "is a pin set, and where does it point"
                # without being opened — the whole question a user has when
                # glancing at the menu. "RC/artifacts" names the scope, which
                # is wider than Remote Control alone (artifacts, triggers and
                # marketplace sync all follow the pin).
                [(
                    "Cloud account (RC/artifacts)… — "
                    f"{pin.pinned_email(self.app.switcher) or 'none'}",
                    "pin-menu",
                )]
                if pin.is_available() or pin._wiring_present(self.app.switcher)
                else []
            ),
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

    def _run_pin_op(self, op) -> None:
        """Adapt a ``(ok, message)`` operation to what _start_action expects.

        `run_action` captures stdout and `_action_done` toasts its first line
        or, on a failed ActionResult, opens the modal. So a failure PRINTS and
        raises — the raise is what routes it to the modal rather than to a
        toast the user may miss — and a success just prints.

        ClaudeSwitchError specifically: run_action catches only that and
        EOFError. A RuntimeError escaped to on_worker_state_changed and became
        a `notify(severity="error")` — MODALS [] — so the modal this
        raise exists to open never opened and the message wore a doubled
        prefix.
        """
        ok, msg = op()
        if not ok:
            # RAISE ONLY. run_action prints "Error: {e}" for a ClaudeSwitchError,
            # so printing first put the same sentence in the modal twice.
            raise ClaudeSwitchError(msg)
        print(msg)

    def _apply_pin(self, acc, impl) -> tuple[bool, str]:
        """Pin to ``acc`` and add the note only this side can produce.

        Runs on a worker thread (see the dispatch): pin.set_pin takes locks and
        can spawn a daemon, and the CLI is a fresh process while this one has a
        UI to keep responsive.
        """
        # Pass the slot we ALREADY have. set_pin re-deriving it from the
        # email made a duplicate address (cswap's own personal+org pattern)
        # raise inside the API-key refusal, skipping it entirely.
        ok, msg = pin.set_pin(
            self.app.switcher, acc.email, acc.org_uuid, num=acc.number
        )
        if ok:
            # An RC session that is already open keeps its old owner (the
            # server fixed it at creation); reconnecting inside it is what
            # moves it. Say so only when there is one.
            try:
                if impl.live_remote_control_sessions():
                    msg += (
                        "  Reconnect open Remote Control sessions to move them "
                        "(/rc → Disconnect → /rc)."
                    )
            except Exception:  # noqa: BLE001 — a note must not fail the action
                pass
        return ok, msg

    def _pin_entries(self) -> MenuEntries:
        """One row per account (→ pin the claude.ai surface to it), plus clear."""
        try:
            pin._impl()  # resolved only to prove the package is usable
        except Exception as exc:  # noqa: BLE001
            # The package is gone or broken, but the row was offered because a
            # WIRING it left behind is still in .claude.json. Removing that is
            # the one pin operation this repo can do on its own (see
            # pin.clear_wiring), and it is exactly what a user reaches for at
            # this moment — so offer it rather than dead-ending on the reason.
            #
            # Scrubbed: the text comes from an optional package and lands in a
            # MENU LABEL (see pin._safe).
            rows: MenuEntries = [(pin._safe(exc), "")]
            if pin._wiring_present(self.app.switcher):
                rows.append(("Remove the leftover pin wiring", "pin:clear"))
            rows.append(_BACK)
            return rows
        current = pin.pinned_email(self.app.switcher)
        snap = self.app.snapshot
        entries: MenuEntries = []
        for acc in (snap.accounts if snap else ()):
            name = f"{acc.alias} ({acc.email})" if acc.alias else acc.email
            state = "  ○ cloud" if current == acc.email else ""
            # An API-key account can never be pinned: sk-ant-api… is not OAuth
            # JSON, so every pinned request fails open. The CLI refuses it; the
            # menu says so instead of offering a row that reports success and
            # pins nothing. No action id — an informational row (see _dispatch).
            # `acc.kind` — the SAME fact the CLI refuses on
            # (switcher._account_kind). The sentinel is derived and diverges:
            # it reads USAGE_NO_CREDENTIALS for an unreadable backup blob and
            # USAGE_KEYCHAIN_UNAVAILABLE for an API-key slot behind a locked
            # macOS keychain, so filtering on it offered the row and the pin
            # went through — the state the CLI refusal exists to prevent.
            if getattr(acc, "kind", None) == "api_key":
                entries.append((f"{acc.number}  {name}  · api key, cannot pin", ""))
                continue
            entries.append((f"{acc.number}  {name}{state}", f"pin:{acc.number}"))
        # Only offer the clear when there is something to clear — an inert row
        # reads as "a pin exists" to anyone scanning the menu. The SAME
        # question the root gate asks: a partial clear_pin drops the record
        # and gets locked out of the wiring, and gating on the record alone
        # then hid the row while the root menu still showed the Cloud line and
        # the message said "re-run once it frees up".
        #
        # AND THE RECORD IS READ THE WAY `clear_pin` READS IT. `current` comes
        # from `pinned_email`, which asks the PACKAGE and answers None
        # whenever it is absent or broken; `clear_pin` decides from
        # `_pinned_email_now`, which reads cswap's OWN settings.json and can
        # still clear it. Gating on the package's answer hid the row for a
        # record this repo can see and remove — one that re-pins the account
        # the moment anything reinstalls the package. A gate must ask what the
        # ACTION asks, or it hides work that exists.
        if (
            current
            or pin._pinned_email_now(self.app.switcher) is not None
            or pin._wiring_present(self.app.switcher)
        ):
            entries.append(("Clear cloud pin", "pin:clear"))
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
        elif action_id == "pin-menu":
            await self._push_menu("cloud account", self._pin_entries())
        elif action_id.startswith("pin:"):
            target = action_id.split(":", 1)[1]
            # CLEAR does not need the package, and must not: uninstalling the
            # extra is precisely when a leftover wiring has to come out, and
            # clear_pin is written to work without it (see pin.clear_wiring).
            # Resolving _impl() for every pin: action made the TUI refuse the
            # one operation still available to it.
            impl = None
            if target != "clear":
                try:
                    impl = pin._impl()
                except Exception as exc:  # noqa: BLE001
                    # Names the real reason, which _impl already distinguishes:
                    # "install the extra" for a missing package, the underlying
                    # error for one that is present and broken.
                    app.notify(pin._safe(exc))
                    await self._pop_menu()
                    return
            snap = app.snapshot
            # THE VERDICT COMES FROM pin.py, not from a second copy here: one
            # decision implemented twice is how this branch and the CLI drift
            # apart. Implemented once, both sides render the result.
            #
            # THROUGH _start_action, like every sibling action in this file.
            # clear_wiring takes a 9s lock; run inline it freezes the dashboard
            # for that long with no toast and no keystrokes while Claude Code
            # holds .claude.json.lock — routine during a credential refresh.
            if target == "clear":
                app._start_action(
                    "clear cloud pin",
                    partial(self._run_pin_op, partial(pin.clear_pin, app.switcher)),
                )
            else:
                acc = next(
                    (a for a in (snap.accounts if snap else ()) if a.number == target),
                    None,
                )
                if acc is not None:
                    app._start_action(
                        f"pin cloud → {acc.email}",
                        partial(self._run_pin_op, partial(self._apply_pin, acc, impl)),
                    )
                else:
                    # The row outlived its account: an open submenu is not
                    # rebuilt while the snapshot updates (refresh_root_menu
                    # returns early below depth 1), so `cswap remove` mid-menu
                    # leaves a row that resolves to nothing. Silently popping
                    # reads as "pinned" — say what happened instead.
                    app.notify(
                        f"Account {target} is no longer in the list — "
                        "nothing was pinned"
                    )
            await self._pop_menu()
        elif action_id in actions:
            actions[action_id]()
        # An id that matches nothing is an INFORMATIONAL row (see
        # _pin_entries, which shows the reason the pin is unusable). It has no
        # action by design, and `actions[action_id]()` raised KeyError out of
        # on_list_view_selected and killed the dashboard — the same class of
        # failure as a raising apply_pin, one menu level up. Selecting a row
        # that says nothing should do nothing.

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
    selection-first, :class:`WatchScreen` is a monitor that can arm
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
            await listview.clear()
            await listview.extend(AccountItem(acc) for acc in snap.accounts)
            self._numbers = numbers
            listview.index = (
                self._index_after_build(snap, first_build, previous)
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
            return self._active_index(snap)
        return min(previous or 0, len(snap.accounts) - 1)

    def _active_index(self, snap: AccountsSnapshot) -> int:
        return next(
            (
                i
                for i, acc in enumerate(snap.accounts)
                if acc.number == snap.active_number
            ),
            0,
        )

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


class WatchScreen(AccountListScreen):
    """Live monitor of every account, full detail, hands-off by default.

    ``s`` arms selection (cursor appears on the active account); Enter then
    switches and stays here — you keep watching on the new account. Esc
    disarms selection first, then leaves the screen.
    """

    _WATCH_TITLE = "watching all accounts"
    _SELECT_TITLE = "switch to which account? · enter confirm · esc cancel"

    BINDINGS = [
        Binding("s", "toggle_select", "Switch"),
        Binding("enter", "select_highlighted", "Confirm", priority=True),
        Binding("f", "app.refresh_full", "Refresh", show=False),
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
                listview.index = self._active_index(snap)
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
