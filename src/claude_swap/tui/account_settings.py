"""One account's own settings: alias, switch threshold, priority,
auto-rotation (disable/enable) and login sync.

The same writes as ``cswap alias``, ``cswap threshold``, ``cswap priority``,
``cswap disable``/``enable`` and ``cswap login-sync``, run like every other
mutating action (off the UI thread, one at a time, output captured). A form
checks its value with the CLI's own validator before anything is written, so
a bad entry keeps the form open.

None of these needs a restart: an auto-switch loop reads every account's
threshold, priority and disabled flag again on each check, and login sync
reads its switch each time it runs.
"""

from __future__ import annotations

import contextlib
import io
from functools import partial
from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, ListItem, ListView, Static

from claude_swap.models import (
    ACCOUNT_PRIORITY_MAX,
    ACCOUNT_THRESHOLD_MAX,
    ACCOUNT_THRESHOLD_MIN,
    AccountSnapshot,
    normalize_account_priority,
    normalize_account_threshold,
    normalize_alias,
)
from claude_swap.tui.settings_screen import ValueModal
from claude_swap.tui.widgets import MenuItem

if TYPE_CHECKING:
    from claude_swap.tui.app import CswapApp

FIELDS = ("alias", "threshold", "priority", "rotation", "login-sync")

LABELS = {
    "alias": "alias",
    "threshold": "switch threshold",
    "priority": "priority",
    "rotation": "auto-rotation",
    "login-sync": "login sync",
}

#: Said after a change that a running auto-switch uses.
APPLIES_NOW = "Applies now, including to a running auto-switch (no restart)."

_LABEL_W = 20


def _fmt_pct(value: float) -> str:
    return f"{value:g}%"


def login_sync_applies(acc: AccountSnapshot) -> bool:
    return not acc.setup_token and acc.kind != "api_key"


def field_value(field: str, acc: AccountSnapshot, *, global_threshold: float) -> tuple[str, bool]:
    """``(text, is_default)`` for one field of an account."""
    if field == "alias":
        return (acc.alias, False) if acc.alias else ("none", True)
    if field == "threshold":
        if acc.policy.threshold is None:
            return f"global ({_fmt_pct(global_threshold)})", True
        return _fmt_pct(acc.policy.threshold), False
    if field == "priority":
        return str(acc.policy.priority), acc.policy.priority == 0
    if field == "rotation":
        return ("off", False) if acc.disabled else ("on", True)
    if not login_sync_applies(acc):
        kind = "API key" if acc.kind == "api_key" else "setup-token"
        return f"n/a ({kind})", True
    return ("on", True) if acc.login_sync else ("off", False)


def field_help(field: str, acc: AccountSnapshot, *, global_threshold: float, strategy: str) -> str:
    """What a field does, and the command that sets it."""
    n = acc.number
    if field == "alias":
        return (
            "A short name to use in place of the number or email. Letters, digits, "
            f"'-', '_' and '.'; empty removes it.  cswap alias {n} NAME"
        )
    if field == "threshold":
        return (
            f"Auto-switch leaves this account at this usage instead of the global "
            f"{_fmt_pct(global_threshold)}. {ACCOUNT_THRESHOLD_MIN:g}-{ACCOUNT_THRESHOLD_MAX:g}; "
            f"empty: global.  cswap threshold {n} PCT"
        )
    if field == "priority":
        used = (
            "in use now" if strategy == "priority-consume-first"
            else f"only the priority-consume-first strategy uses it (now: {strategy})"
        )
        return (
            f"Higher tiers go first; {used}. 0-{ACCOUNT_PRIORITY_MAX}, "
            f"default 0.  cswap priority {n} N"
        )
    if field == "rotation":
        return (
            "Off: auto-switch and a bare cswap switch skip this account; you can "
            f"still switch to it yourself.  cswap disable {n} / enable {n}"
        )
    if not login_sync_applies(acc):
        return "Doesn't apply: API-key and setup-token accounts never change in use."
    return (
        "On: while auto-switch runs, a new /login for this account is stored "
        f"in its slot.  cswap login-sync {n} on|off"
    )


def _write_alias(switcher, number: str, alias: str | None) -> None:
    """``cswap alias``'s write and its confirmation line (the switcher's
    alias setters print nothing; the CLI does)."""
    if alias is None:
        num = switcher.unset_alias(number)
        print(f"Removed the alias for account {num}.")
    else:
        num, normalized = switcher.set_alias(number, alias)
        print(f"Set alias '{normalized}' for account {num}.")


def _write_rotation(switcher, number: str, off: bool) -> None:
    """``cswap disable``/``enable``, confirmed as auto-rotation on/off: the
    switcher's own first line says "Disabled Account-N", the CLI's word for it.
    Its follow-up lines (the account is active; none left in rotation) stay."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        switcher.set_account_disabled(number, off)
    print(f"Auto-rotation {'off' if off else 'on'} for account {number}.")
    for line in buf.getvalue().splitlines()[1:]:
        print(line)


class FieldItem(ListItem):
    def __init__(self, field: str, text: str, is_default: bool) -> None:
        self.field = field
        line = Text(no_wrap=True, overflow="ellipsis")
        line.append(f"{LABELS[field]:<{_LABEL_W}}")
        line.append(text, style="bold")
        if is_default:
            line.append("  (default)", style="dim")
        super().__init__(Static(line))


class AccountSettingsScreen(Screen):
    BINDINGS = [
        Binding("enter", "select_highlighted", "Change", priority=True),
        Binding("escape,left", "back", "Back"),
        Binding("d", "reset", "Reset to default"),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]

    app: "CswapApp"

    def __init__(self, number: str) -> None:
        super().__init__()
        self.number = number
        self._index = 0

    def compose(self) -> ComposeResult:
        yield Static("", id="menu-title")
        yield Static("", id="settings-help")
        yield ListView(id="menu")
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#menu", ListView).focus()
        self.watch(self.app, "snapshot", self._on_snapshot, init=False)
        await self._redraw()

    # -- data -------------------------------------------------------------------

    def _account(self) -> AccountSnapshot | None:
        snap = self.app.snapshot
        return next(
            (a for a in (snap.accounts if snap else ()) if a.number == self.number), None
        )

    def _globals(self) -> tuple[float, str]:
        """The global threshold and strategy, from the settings file."""
        from claude_swap.settings import load_settings

        try:
            loaded = load_settings(self.app.switcher.backup_dir)
            return loaded.threshold, loaded.strategy
        except Exception:  # noqa: BLE001 -- display only
            return 90.0, "best"

    # -- rendering --------------------------------------------------------------

    async def _on_snapshot(self, _snap) -> None:
        if self._account() is None:
            self.app.notify(f"Account {self.number} is gone.")
            self.app.pop_screen()
            return
        await self._redraw()

    async def _redraw(self) -> None:
        acc = self._account()
        if acc is None:
            return
        name = f"{acc.alias} ({acc.email})" if acc.alias else acc.email
        self.query_one("#menu-title", Static).update(
            f"account settings › {acc.number}  {name}"
        )
        global_threshold, _strategy = self._globals()
        menu = self.query_one("#menu", ListView)
        index = menu.index if menu.index is not None else self._index
        await menu.clear()
        items: list[ListItem] = [
            FieldItem(field, *field_value(field, acc, global_threshold=global_threshold))
            for field in FIELDS
        ]
        items.append(MenuItem("← back", "back", muted=True))
        await menu.extend(items)
        menu.index = min(index, len(items) - 1)
        self._update_help()

    def _highlighted_field(self) -> str | None:
        item = self.query_one("#menu", ListView).highlighted_child
        return item.field if isinstance(item, FieldItem) else None

    def _update_help(self) -> None:
        acc = self._account()
        field = self._highlighted_field()
        widget = self.query_one("#settings-help", Static)
        if acc is None or field is None:
            widget.update("Changes are saved at once. d resets one to its default.")
            return
        global_threshold, strategy = self._globals()
        widget.update(field_help(field, acc, global_threshold=global_threshold, strategy=strategy))

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        index = self.query_one("#menu", ListView).index
        if index is not None:
            self._index = index
        self._update_help()

    # -- input ------------------------------------------------------------------

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, FieldItem):
            self._edit(item.field)
        elif isinstance(item, MenuItem) and item.action_id == "back":
            self.app.pop_screen()

    def action_select_highlighted(self) -> None:
        self.query_one("#menu", ListView).action_select_cursor()

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_cursor_down(self) -> None:
        self.query_one("#menu", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#menu", ListView).action_cursor_up()

    def _edit(self, field: str) -> None:
        acc = self._account()
        if acc is None:
            return
        global_threshold, strategy = self._globals()
        help_text = field_help(field, acc, global_threshold=global_threshold, strategy=strategy)
        title = f"{LABELS[field].capitalize()} for account {acc.number}"
        if field == "rotation":
            self._apply(field, not acc.disabled)
        elif field == "login-sync":
            if not login_sync_applies(acc):
                self.app.notify(help_text)
                return
            self._apply(field, not acc.login_sync)
        elif field == "alias":
            self._form(field, ValueModal(
                title, help_text.split("  cswap")[0], acc.alias,
                validate=self._check_alias, empty_unsets=True, placeholder="empty: no alias",
            ))
        elif field == "threshold":
            current = "" if acc.policy.threshold is None else f"{acc.policy.threshold:g}"
            self._form(field, ValueModal(
                title, help_text.split("  cswap")[0], current,
                validate=normalize_account_threshold, empty_unsets=True,
                placeholder=f"empty: global ({_fmt_pct(global_threshold)})",
            ))
        elif field == "priority":
            self._form(field, ValueModal(
                title, help_text.split("  cswap")[0], str(acc.policy.priority),
                validate=normalize_account_priority, empty_unsets=True, placeholder="empty: 0",
            ))

    def _check_alias(self, raw: str) -> str:
        """The CLI's alias rules, plus the clash check ``set_alias`` makes,
        so a taken alias keeps the form open."""
        alias = normalize_alias(raw)
        snap = self.app.snapshot
        for other in snap.accounts if snap else ():
            if other.number != self.number and other.alias == alias:
                raise ValueError(f"alias '{alias}' is already used by account {other.number}")
        return alias

    def _form(self, field: str, modal: ValueModal) -> None:
        def done(result) -> None:
            if result is not None:
                action, raw = result
                self._apply(field, raw if action == "set" else None)

        self.app.push_screen(modal, done)

    def action_reset(self) -> None:
        acc = self._account()
        field = self._highlighted_field()
        if acc is None or field is None:
            return
        defaults = {
            "alias": None, "threshold": None, "priority": None,
            "rotation": False, "login-sync": True,
        }
        if field_value(field, acc, global_threshold=self._globals()[0])[1]:
            self.app.notify(f"{LABELS[field].capitalize()} is already the default.")
            return
        self._apply(field, defaults[field])

    # -- writing ----------------------------------------------------------------

    def _unchanged(self, field: str, value) -> str | None:
        """A notice when writing ``value`` would change nothing, else None."""
        acc = self._account()
        if acc is None:
            return None
        if field == "alias":
            same = (normalize_alias(value) if value is not None else "") == acc.alias
            shown = acc.alias or "no alias"
        elif field == "threshold":
            new = None if value is None else normalize_account_threshold(value)
            same = new == acc.policy.threshold
            shown = "the global threshold" if new is None else _fmt_pct(new)
        elif field == "priority":
            new = 0 if value is None else normalize_account_priority(value)
            same = new == acc.policy.priority
            shown = f"priority {new}"
        else:
            return None
        return f"Account {acc.number} already has {shown}." if same else None

    def _apply(self, field: str, value) -> None:
        """Write one field through the switcher. ``value``: the new value
        (validated), None for the default; for rotation, ``True`` disables."""
        unchanged = self._unchanged(field, value)
        if unchanged:
            self.app.notify(unchanged)
            return
        switcher = self.app.switcher
        n = self.number
        note: str | None = None
        if field == "alias":
            fn = partial(_write_alias, switcher, n, value)
            label = f"Alias for account {n}"
        elif field == "threshold":
            fn = partial(switcher.set_account_threshold, n,
                         None if value is None else normalize_account_threshold(value))
            label = f"Threshold for account {n}"
            note = APPLIES_NOW
        elif field == "priority":
            fn = partial(switcher.set_account_priority, n,
                         None if value is None else normalize_account_priority(value))
            label = f"Priority for account {n}"
            _threshold, strategy = self._globals()
            note = APPLIES_NOW if strategy == "priority-consume-first" else (
                f"Only the priority-consume-first strategy uses it (now: {strategy})."
            )
        elif field == "rotation":
            fn = partial(_write_rotation, switcher, n, bool(value))
            label = f"Auto-rotation for account {n}"
            note = APPLIES_NOW
        else:
            fn = partial(switcher.set_account_login_sync, n, bool(value))
            label = f"Login sync for account {n}"
            note = "Applies now."
        self.app._start_action(label, fn, note=note)
