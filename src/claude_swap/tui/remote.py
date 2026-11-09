"""Remote sync in the TUI: one screen per repository, plus its forms.

Opening a repository fetches it (asking for the passphrase when none is
saved, or the saved one stopped working), then lists every setup-token
account by where it is: here only, in the repository only, in both but
different, in sync. Enter on an account offers what applies to it; every
action fetches again first (``RemoteSync`` does) and redraws from a fresh
comparison afterwards.

All git and crypto work runs in a thread worker under the app's single
``busy`` flag, like every other mutating action.
"""

from __future__ import annotations

import contextlib
import io
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Callable

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Checkbox, Footer, Input, Label, ListView, Static

from claude_swap import remote as rm
from claude_swap.exceptions import ClaudeSwitchError, PassphraseError
from claude_swap.tui.modals import ConfirmModal, OutputModal
from claude_swap.tui.widgets import MenuItem

if TYPE_CHECKING:
    from claude_swap.tui.app import CswapApp

MenuEntries = list[tuple[str, str]]
_BACK = ("← back", "back")

STATE_LABEL = {
    rm.IN_SYNC: "in sync",
    rm.DIFFERS: "differs",
    rm.HERE_ONLY: "here only",
    rm.REPO_ONLY: "repo only",
    rm.SKIPPED: "skipped",
    rm.UNREADABLE: "unreadable",
    rm.NOT_SHAREABLE: "n/a",
}
_MUTED_STATES = (rm.SKIPPED, rm.NOT_SHAREABLE, rm.IN_SYNC)


def row_label(row: rm.Row) -> str:
    local = row.local_num or "-"
    text = f"{STATE_LABEL.get(row.state, row.state):<10}  {local:>3}  {row.label}"
    if row.note:
        text += f"   · {row.note}"
    return text


@dataclass
class _NewPassphrase:
    """An operation's result that changed the passphrase: fetch with the new one."""

    passphrase: str
    message: str


# ---------------------------------------------------------------------------
# forms
# ---------------------------------------------------------------------------


class _FormModal(ModalScreen):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("left", "app.focus_previous", show=False),
        Binding("right", "app.focus_next", show=False),
    ]

    def _error(self, message: str) -> None:
        self.query_one("#form-error", Static).update(message)

    def _value(self, widget_id: str) -> str:
        return self.query_one(f"#{widget_id}", Input).value

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        else:
            self._submit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _submit(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def _buttons(self, ok: str) -> ComposeResult:
        yield Static("", id="form-error", classes="form-error")
        with Horizontal(classes="modal-buttons"):
            yield Button(ok, id="ok")
            yield Button("Cancel", id="cancel")
        yield Static(f"enter {ok.lower()}  ·  tab next field  ·  esc cancel", classes="modal-hint")


class PassphraseModal(_FormModal):
    """One passphrase; dismisses with it, or None."""

    def __init__(self, name: str, message: str) -> None:
        super().__init__()
        self._name = name
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box"):
            yield Label(f"Passphrase for '{self._name}'", classes="modal-title")
            yield Static(self._message, classes="modal-body")
            yield Input(password=True, placeholder="passphrase", id="passphrase")
            yield from self._buttons("Open")

    def _submit(self) -> None:
        value = self._value("passphrase")
        if not value:
            self._error("Enter the passphrase.")
            return
        self.dismiss(value)


class NewPassphraseModal(_FormModal):
    """A new passphrase, typed twice."""

    def __init__(self, name: str) -> None:
        super().__init__()
        self._name = name

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box"):
            yield Label(f"Change the passphrase for '{self._name}'", classes="modal-title")
            yield Static(
                "Every account in the repository is re-encrypted. Other machines "
                "are asked for the new passphrase the next time they open it.",
                classes="modal-body",
            )
            yield Input(password=True, placeholder="new passphrase", id="new")
            yield Input(password=True, placeholder="again", id="again")
            yield from self._buttons("Change")

    def _submit(self) -> None:
        new, again = self._value("new"), self._value("again")
        if len(new) < rm.MIN_PASSPHRASE:
            self._error(f"At least {rm.MIN_PASSPHRASE} characters.")
            return
        if new != again:
            self._error("The two entries differ.")
            return
        self.dismiss(new)


@dataclass
class RemoteTokenForm:
    token: str
    email: str | None
    alias: str | None


class RemoteTokenModal(_FormModal):
    """A setup-token for the repository only (not added on this machine)."""

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box"):
            yield Label("Add a setup-token to the repository", classes="modal-title")
            yield Static(
                "From `claude setup-token` (sk-ant-oat…). It goes into the "
                "repository only; pull it on the machines that should use it.",
                classes="modal-body",
            )
            yield Input(password=True, placeholder="setup-token (required)", id="token")
            yield Input(placeholder="email label (optional)", id="email")
            yield Input(placeholder="alias (optional)", id="alias")
            yield from self._buttons("Add")

    def _submit(self) -> None:
        try:
            token = rm.validate_setup_token(self._value("token"))
        except ClaudeSwitchError as e:
            self._error(str(e).split(". ")[0] + ".")
            return
        self.dismiss(RemoteTokenForm(
            token=token,
            email=self._value("email").strip() or None,
            alias=self._value("alias").strip() or None,
        ))


@dataclass
class RemoteForm:
    name: str
    url: str
    branch: str
    ssh_key: str
    token: str
    username: str
    passphrase: str
    remember: bool

    @property
    def auth(self) -> str:
        if self.token:
            return "https-token"
        if self.ssh_key:
            return "ssh-key"
        return "default"


@dataclass
class AddedRemote:
    """What a successful add dismisses with."""

    cfg: rm.RemoteConfig
    message: str
    passphrase: str


def _field_for(error: ClaudeSwitchError) -> str:
    """The form field an add failure points at, to focus it."""
    if isinstance(error, PassphraseError):
        return "passphrase"
    text = str(error).lower()
    for needle, field_id in (
        ("remote name", "name"), ("already exists", "name"),
        ("ssh key", "ssh_key"), ("branch", "branch"),
        ("token", "token"), ("https login", "token"),
    ):
        if needle in text:
            return field_id
    return "url"


class AddRemoteModal(_FormModal):
    """Where the repository is, how git logs in, and its passphrase.

    It adds the repository itself and stays open until that worked: a wrong
    passphrase, an unreachable URL or a refused login shows here, next to
    the fields, with everything typed still in place.
    """

    def __init__(self, sync: rm.RemoteSync) -> None:
        super().__init__()
        self.sync = sync
        self._working = False

    def compose(self) -> ComposeResult:
        # Scrolls: nine fields outgrow a short terminal, and focus moving
        # down the form scrolls the field into view.
        with VerticalScroll(classes="modal-box modal-box-wide"):
            yield Label("Add a repository", classes="modal-title")
            yield Static(
                "Use an empty private repository (it is set up as a new remote) or "
                "one already holding claude-swap accounts (it needs that passphrase). "
                "SSH uses your agent and default keys unless a key file is given.",
                classes="modal-body",
            )
            yield Input(placeholder="name, e.g. team (required)", id="name")
            yield Input(placeholder="URL: git@host:me/repo.git or https://… (required)", id="url")
            yield Input(placeholder="branch (default: main)", id="branch")
            yield Input(placeholder="SSH key file (optional)", id="ssh_key")
            yield Input(password=True, placeholder="HTTPS access token (optional)", id="token")
            yield Input(placeholder="HTTPS username (optional, default: git)", id="username")
            yield Input(password=True, placeholder="passphrase (new, or the repository's)", id="passphrase")
            yield Input(password=True, placeholder="passphrase again", id="again")
            yield Checkbox("Remember the passphrase on this machine", value=True, id="remember")
            yield from self._buttons("Add")

    def _submit(self) -> None:
        if self._working:
            return
        form = RemoteForm(
            name=self._value("name").strip(),
            url=self._value("url").strip(),
            branch=self._value("branch").strip() or "main",
            ssh_key=self._value("ssh_key").strip(),
            token=self._value("token").strip(),
            username=self._value("username").strip(),
            passphrase=self._value("passphrase"),
            remember=self.query_one("#remember", Checkbox).value,
        )
        if not form.name or not form.url:
            self._error("A name and a URL are required.")
        elif form.ssh_key and form.token:
            self._error("Use an SSH key or an HTTPS token, not both.")
        elif form.token and not form.url.lower().startswith("https://"):
            self._error("An access token needs an https:// URL.")
        elif len(form.passphrase) < rm.MIN_PASSPHRASE:
            self._error(f"The passphrase needs at least {rm.MIN_PASSPHRASE} characters.")
        elif form.passphrase != self._value("again"):
            self._error("The two passphrase entries differ.")
        elif self.app.busy:
            self._error("Another action is still running; try again in a moment.")
        else:
            self._start(form)

    def _set_working(self, working: bool) -> None:
        self._working = working
        for widget in self.query("Input, Checkbox, Button"):
            widget.disabled = working

    def _start(self, form: RemoteForm) -> None:
        self.app.busy = True
        self._set_working(True)
        self._error(f"Connecting to {form.url}…")
        self.run_worker(
            partial(self._add_blocking, form),
            thread=True, group="action", exit_on_error=False, name="Add repository",
        )

    def _add_blocking(self, form: RemoteForm) -> None:
        try:
            cfg, created, count = _quiet(lambda: self.sync.add(
                form.name, form.url, passphrase=form.passphrase, branch=form.branch,
                auth=form.auth, ssh_key=form.ssh_key, username=form.username,
                token=form.token, remember=form.remember,
            ))
        except ClaudeSwitchError as e:
            self.app.call_from_thread(self._add_failed, e)
            return
        if created:
            message = f"Set up '{cfg.name}' as a new remote"
        else:
            message = f"Added '{cfg.name}': {count} shared account{'s' if count != 1 else ''}"
        self.app.call_from_thread(self._add_done, AddedRemote(cfg, message, form.passphrase))

    def _add_failed(self, error: ClaudeSwitchError) -> None:
        self.app.busy = False
        self._set_working(False)
        field_id = _field_for(error)
        if field_id == "passphrase":
            # Wrong for this repository: both copies have to be typed again.
            self.query_one("#passphrase", Input).value = ""
            self.query_one("#again", Input).value = ""
        self._error(str(error))
        self.query_one(f"#{field_id}", Input).focus()

    def _add_done(self, added: AddedRemote) -> None:
        self.app.busy = False
        self._working = False
        self.dismiss(added)

    def action_cancel(self) -> None:
        if self._working:
            self._error("Still connecting; this ends by itself if the server doesn't answer.")
            return
        self.dismiss(None)


# ---------------------------------------------------------------------------
# the repository screen
# ---------------------------------------------------------------------------


def _quiet(fn: Callable[[], object]) -> object:
    """Run ``fn`` with stdout/stderr captured: the TUI owns the terminal."""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return fn()


class RemoteScreen(Screen):
    BINDINGS = [
        Binding("escape,left", "back", "Back"),
        Binding("r", "fetch", "Fetch again"),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]

    app: "CswapApp"

    def __init__(self, sync: rm.RemoteSync, remote_id: str, passphrase: str | None = None) -> None:
        super().__init__()
        self.sync = sync
        self.remote_id = remote_id
        self._passphrase = passphrase
        self._status: rm.RemoteStatus | None = None
        self._stack: list[tuple[str, MenuEntries]] = []
        try:
            self._name = sync.get(remote_id).name
        except ClaudeSwitchError:
            self._name = "?"

    def compose(self) -> ComposeResult:
        yield Static("", id="menu-title")
        yield Static("", id="remote-note")
        yield ListView(id="menu")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#menu", ListView).focus()
        self._title()
        self.action_fetch()

    # -- display ---------------------------------------------------------------

    def _title(self) -> None:
        crumbs = ["remote", self._name, *(t for t, _ in self._stack[1:])]
        self.query_one("#menu-title", Static).update(" › ".join(crumbs))

    def _note(self, text: str) -> None:
        self.query_one("#remote-note", Static).update(text)

    def _overview(self) -> MenuEntries:
        status = self._status
        rows = status.rows if status else []
        here = sum(r.pushed_by_default for r in rows)
        there = sum(r.state == rm.REPO_ONLY and not r.conflict_num for r in rows)
        entries: MenuEntries = []
        if here:
            entries.append((f"↑ Push every here-only account ({here})", "push-all"))
        if there:
            entries.append((f"↓ Pull every repo-only account ({there})", "pull-all"))
        entries += [(row_label(r), f"row:{i}") for i, r in enumerate(rows)]
        if status is not None and not rows:
            entries.append(("No setup-token accounts here or in the repository yet.", "noop"))
        entries.append(("+ Add a setup-token to the repository…", "add-token"))
        entries.append(("Change passphrase…", "passwd"))
        if status is not None and status.remote.remember_passphrase:
            entries.append(("Forget the saved passphrase", "forget"))
        entries.append(("Remove this repository from this machine…", "remove"))
        entries.append(_BACK)
        return entries

    def _row_entries(self, i: int, row: rm.Row) -> MenuEntries:
        entries: MenuEntries = []
        if row.note:
            entries.append((row.note, "noop"))
        if row.state == rm.HERE_ONLY:
            entries.append(("Push to the repository", f"act-push:{i}"))
        elif row.state == rm.DIFFERS:
            entries.append(("Keep this machine's token: update the repository", f"act-push:{i}"))
            entries.append(("Take the repository's token: update this machine", f"act-pull:{i}"))
        elif row.state in (rm.REPO_ONLY, rm.SKIPPED):
            entries.append(("Add on this machine", f"act-pull:{i}"))
            if row.state == rm.REPO_ONLY:
                entries.append(("Not on this machine (a pull leaves it alone)", f"act-skip:{i}"))
            else:
                entries.append(("Undo skip (pull it like the others)", f"act-unskip:{i}"))
        if row.entry_id:
            entries.append(("Delete from the repository…", f"act-rm:{i}"))
        entries.append(_BACK)
        return entries

    async def _show(self, entries: MenuEntries, title: str | None = None, *, push: bool = False) -> None:
        if push:
            self._stack.append((title or "", entries))
        elif self._stack:
            self._stack[-1] = (self._stack[-1][0], entries)
        else:
            self._stack.append(("overview", entries))
        self._title()
        menu = self.query_one("#menu", ListView)
        keep = menu.index if not push else 0
        await menu.clear()
        await menu.extend(
            MenuItem(label, action_id, muted=self._is_muted(action_id))
            for label, action_id in entries
        )
        menu.index = min(keep or 0, max(len(entries) - 1, 0))

    def _is_muted(self, action_id: str) -> bool:
        if action_id in ("back", "noop"):
            return True
        if action_id.startswith("row:") and self._status is not None:
            return self._status.rows[int(action_id[4:])].state in _MUTED_STATES
        return False

    async def _redraw_overview(self) -> None:
        self._stack = self._stack[:1]
        await self._show(self._overview())

    # -- running work ------------------------------------------------------------

    def _run(
        self,
        label: str,
        op: Callable[[str], object] | None = None,
        *,
        fetch_after: bool = True,
        after: Callable[[object], None] | None = None,
        with_passphrase: bool = True,
    ) -> None:
        """``op(passphrase)`` then a fresh comparison, in one worker.

        ``with_passphrase=False`` (with ``fetch_after=False``): ``op`` gets
        None and nothing asks for a passphrase.
        """
        if self.app.busy:
            self.app.notify("Another action is still running", severity="warning")
            return
        self.app.busy = True
        self._note(f"{label}…")
        self.app.run_worker(
            partial(self._blocking, label, op, fetch_after, after, with_passphrase),
            thread=True, group="action", exit_on_error=False, name=label,
        )

    def _blocking(self, label, op, fetch_after, after, with_passphrase=True) -> None:
        sync, rid = self.sync, self.remote_id
        try:
            def work():
                if not with_passphrase:
                    return ("ok", op(None), None)
                passphrase = self._passphrase or sync.stored_passphrase(sync.get(rid))
                if not passphrase:
                    return ("need", None, None)
                result = op(passphrase) if op else None
                if isinstance(result, _NewPassphrase):
                    passphrase, result = result.passphrase, result.message
                status = sync.status(rid, passphrase) if fetch_after else None
                self._passphrase = passphrase
                return ("ok", result, status)

            outcome = _quiet(work)
        except PassphraseError:
            outcome = ("bad", None, None)
        except ClaudeSwitchError as e:
            outcome = ("error", str(e), None)
        self.app.call_from_thread(self._done, label, op, fetch_after, after, outcome)

    async def _done(self, label, op, fetch_after, after, outcome) -> None:
        self.app.busy = False
        kind, value, status = outcome
        self._note("")
        if kind in ("need", "bad"):
            message = (
                "No passphrase is saved for this repository on this machine."
                if kind == "need" else
                "That passphrase does not open this repository (changed on another machine?)."
            )
            self._passphrase = None
            self.app.push_screen(
                PassphraseModal(self._name, message),
                partial(self._passphrase_entered, label, op, fetch_after, after),
            )
            return
        if kind == "error":
            if self._status is None:
                self._note(f"Could not open the repository: {value}")
                await self._show([("Fetch again", "fetch"), _BACK])
            self.app.push_screen(OutputModal(f"{label} — failed", f"Error: {value}"))
            return
        if status is not None:
            self._status = status
            await self._redraw_overview()
        self.app.request_refresh()
        if after is not None:
            after(value)
        elif isinstance(value, rm.SyncReport):
            if len(value.lines) == 1:
                self.app.notify(value.lines[0], title=label)
            else:
                self.app.push_screen(OutputModal(label, "\n".join(value.lines)))
        elif isinstance(value, str) and value:
            self.app.notify(value, title=label)

    def _passphrase_entered(self, label, op, fetch_after, after, value: str | None) -> None:
        if value is None:
            if self._status is None:
                self.app.pop_screen()
            return
        self._passphrase = value
        self._run(label, op, fetch_after=fetch_after, after=after)

    # -- menu ------------------------------------------------------------------

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, MenuItem):
            await self._dispatch(event.item.action_id)

    def _row(self, action_id: str) -> rm.Row:
        return self._status.rows[int(action_id.split(":", 1)[1])]

    async def _dispatch(self, action_id: str) -> None:
        sync, rid = self.sync, self.remote_id
        if action_id == "noop":
            return
        if action_id == "back":
            await self.action_back()
        elif action_id == "fetch":
            self.action_fetch()
        elif action_id == "push-all":
            self._run("Push", lambda p: sync.push(rid, p, None, confirm=lambda r: False))
        elif action_id == "pull-all":
            self._run("Pull", lambda p: sync.pull(rid, p, None, confirm=lambda r: False))
        elif action_id.startswith("row:"):
            row = self._row(action_id)
            if row.state == rm.NOT_SHAREABLE:
                self.app.notify(
                    f"Not shared: {row.note}. Only `claude setup-token` accounts can be "
                    "used on several machines at once.", timeout=6,
                )
                return
            i = int(action_id[4:])
            await self._show(self._row_entries(i, row), row.label, push=True)
        elif action_id.startswith("act-push:"):
            row = self._row(action_id)
            self._run("Push", lambda p: sync.push(rid, p, [row.local_num], confirm=lambda r: True))
        elif action_id.startswith("act-pull:"):
            self._pull_one(self._row(action_id))
        elif action_id.startswith(("act-skip:", "act-unskip:")):
            row = self._row(action_id)
            skip = action_id.startswith("act-skip:")
            self._run(
                "Skip" if skip else "Undo skip",
                lambda p: sync.set_skipped(rid, p, row.entry_id, skip)
                and (f"{row.label}: not on this machine" if skip else f"{row.label}: pulled again by pull-all"),
            )
        elif action_id.startswith("act-rm:"):
            row = self._row(action_id)
            self.app.push_screen(
                ConfirmModal(
                    f"Delete {row.label} from '{self._name}'?\n\nOther machines no longer "
                    "see it there. Accounts already added on any machine are kept.",
                    title="Delete from the repository", yes_label="Delete",
                ),
                lambda yes: yes and self._run(
                    "Delete", lambda p: f"Deleted {sync.remove_entry(rid, p, row.entry_id)} from the repository"),
            )
        elif action_id == "add-token":
            self.app.push_screen(RemoteTokenModal(), self._token_entered)
        elif action_id == "passwd":
            self.app.push_screen(NewPassphraseModal(self._name), self._new_passphrase)
        elif action_id == "forget":
            try:
                sync.forget_passphrase(rid)
            except ClaudeSwitchError as e:
                self.app.notify(str(e), severity="error")
                return
            self.app.notify("The passphrase will be asked for each time.")
            self._status.remote.remember_passphrase = False
            await self._show(self._overview())
        elif action_id == "remove":
            self.app.push_screen(
                ConfirmModal(
                    f"Forget '{self._name}' on this machine?\n\nIts clone and saved "
                    "logins here are deleted. The repository and the accounts are not changed.",
                    title="Remove repository", yes_label="Remove",
                ),
                lambda yes: yes and self._run(
                    "Remove repository", lambda p: sync.remove(rid), fetch_after=False,
                    with_passphrase=False, after=lambda _: self._removed(),
                ),
            )

    def _pull_one(self, row: rm.Row) -> None:
        sync, rid = self.sync, self.remote_id

        def run(replace: bool) -> None:
            self._run("Pull", lambda p: sync.pull(
                rid, p, [row.entry_id], confirm=lambda r: True, replace_conflicts=replace))

        if row.conflict_num and row.state in (rm.REPO_ONLY, rm.SKIPPED):
            self.app.push_screen(
                ConfirmModal(
                    f"Account-{row.conflict_num} on this machine already uses the email "
                    f"label {row.email}.\n\nReplace that account's token with the "
                    "repository's?",
                    title="Same email label", yes_label="Replace",
                ),
                lambda yes: yes and run(True),
            )
        else:
            run(False)

    def _token_entered(self, form: RemoteTokenForm | None) -> None:
        if form is None:
            return
        sync, rid = self.sync, self.remote_id
        self._run("Add token", lambda p: (
            f"Added {sync.add_token(rid, p, form.token, email=form.email, alias=form.alias).label} "
            "to the repository"
        ))

    def _new_passphrase(self, new: str | None) -> None:
        if new is None:
            return
        sync, rid = self.sync, self.remote_id

        def op(p: str) -> _NewPassphrase:
            n = sync.change_passphrase(rid, p, new)
            return _NewPassphrase(new, f"Passphrase changed; re-encrypted {n} account{'s' if n != 1 else ''}")

        self._run("Change passphrase", op)

    def _removed(self) -> None:
        self.app.notify(f"Removed '{self._name}' from this machine")
        self.app.pop_screen()
        refresh = getattr(self.app.screen, "refresh_remote_menu", None)
        if refresh is not None:
            self.app.call_later(refresh)

    # -- actions ----------------------------------------------------------------

    def action_fetch(self) -> None:
        self._run("Fetch")

    async def action_back(self) -> None:
        if len(self._stack) > 1:
            self._stack.pop()
            await self._show(self._stack[-1][1])
        else:
            self.app.pop_screen()

    def action_cursor_down(self) -> None:
        self.query_one("#menu", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#menu", ListView).action_cursor_up()
