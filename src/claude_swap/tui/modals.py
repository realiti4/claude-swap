"""Modal screens: confirmations, token entry, and captured-output display."""

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static


class ConfirmModal(ModalScreen[bool]):
    """Yes/No confirmation. Dismisses with True only on explicit confirm.

    Keyboard-first: ←/→ move between the buttons (Enter presses the focused
    one), y/n answer directly, Esc cancels. Clicking still works.
    """

    BINDINGS = [
        Binding("y", "confirm", "Yes", show=False),
        Binding("n,escape", "cancel", "No", show=False),
        Binding("left", "app.focus_previous", show=False),
        Binding("right", "app.focus_next", show=False),
    ]

    def __init__(
        self, message: str, *, title: str = "Confirm", yes_label: str = "Yes"
    ) -> None:
        super().__init__()
        self._title = title
        self._message = message
        self._yes_label = yes_label

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box"):
            yield Label(self._title, classes="modal-title")
            yield Static(self._message, classes="modal-body")
            with Horizontal(classes="modal-buttons"):
                yield Button(self._yes_label, id="yes")
                yield Button("Cancel", id="no")
            yield Static(
                f"← → · enter  ·  y {self._yes_label.lower()}  ·  n / esc cancel",
                classes="modal-hint",
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


@dataclass
class TokenForm:
    """What the add-token modal collects."""

    token: str
    email: str | None
    slot: int | None


class AddTokenModal(ModalScreen["TokenForm | None"]):
    """Collects a setup-token/API key, optional email label, optional slot.

    ←/→ only reach the screen when a Button is focused (a focused Input
    consumes them for cursor movement), so they safely double as button
    navigation.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("left", "app.focus_previous", show=False),
        Binding("right", "app.focus_next", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box"):
            yield Label("Add account from token", classes="modal-title")
            yield Static(
                "OAuth setup-token (sk-ant-oat…) or managed API key "
                "(sk-ant-api…); the type is auto-detected.",
                classes="modal-body",
            )
            yield Input(password=True, placeholder="token (required)", id="token")
            yield Input(placeholder="email label (optional)", id="email")
            yield Input(placeholder="slot number (optional)", id="slot", type="integer")
            yield Static("", id="form-error", classes="form-error")
            with Horizontal(classes="modal-buttons"):
                yield Button("Add", id="add")
                yield Button("Cancel", id="cancel")
            yield Static(
                "enter add  ·  tab next field  ·  esc cancel",
                classes="modal-hint",
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        self._submit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def _submit(self) -> None:
        token = self.query_one("#token", Input).value.strip()
        email = self.query_one("#email", Input).value.strip() or None
        slot_raw = self.query_one("#slot", Input).value.strip()
        if not token:
            self.query_one("#form-error", Static).update("Token is required.")
            return
        slot: int | None = None
        if slot_raw:
            try:
                slot = int(slot_raw)
            except ValueError:
                self.query_one("#form-error", Static).update(
                    "Slot must be a number."
                )
                return
            if slot < 1:
                self.query_one("#form-error", Static).update("Slot must be >= 1.")
                return
        self.dismiss(TokenForm(token=token, email=email, slot=slot))

    def action_cancel(self) -> None:
        self.dismiss(None)


class OutputModal(ModalScreen[None]):
    """Scrollable display of captured (ANSI-colored) action output."""

    BINDINGS = [Binding("escape,q,enter", "dismiss_modal", "Close", show=False)]

    def __init__(self, title: str, output: str) -> None:
        super().__init__()
        self._title = title
        self._output = output

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box modal-box-wide"):
            yield Label(self._title, classes="modal-title")
            with VerticalScroll(classes="modal-output"):
                yield Static(Text.from_ansi(self._output.rstrip() or "(no output)"))
            with Horizontal(classes="modal-buttons"):
                yield Button("Close", id="close")
            yield Static("esc close", classes="modal-hint")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)
