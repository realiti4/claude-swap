"""Modal screens: confirmations, token entry, and captured-output display."""

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Static

from claude_swap.models import (
    ACCOUNT_ORDER_MAX,
    ACCOUNT_ORDER_MIN,
    ACCOUNT_THRESHOLD_MAX,
    ACCOUNT_THRESHOLD_MIN,
    AccountPolicy,
    normalize_account_order,
    normalize_account_threshold,
)


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


@dataclass
class PolicyForm:
    """What the per-account policy modal collects.

    Mirrors :class:`~claude_swap.models.AccountPolicy` field for field, so the
    app can diff the form against the record the modal opened with by comparing
    the same three names, with no conversion step in between to get wrong.
    """

    threshold: float | None
    backup: bool
    order: int | None


class PolicyModal(ModalScreen["PolicyForm | None"]):
    """Edits one account's threshold, reserve flag and chain position.

    Cross-surface parity is the point. The two text fields are validated by the
    very functions ``cswap threshold`` and ``cswap order`` call, and a rejection
    is reported using the validator's own message, so the TUI cannot come to
    accept something the verbs refuse or explain a refusal differently.

    An empty field is this surface's spelling of ``--unset``; the placeholders
    say so. Nothing is written here: the modal dismisses with a ``PolicyForm``
    and the app performs the write.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("left", "app.focus_previous", show=False),
        Binding("right", "app.focus_next", show=False),
    ]

    def __init__(self, label: str, policy: AccountPolicy) -> None:
        super().__init__()
        self._label = label
        self._policy = policy

    def compose(self) -> ComposeResult:
        # ``:g`` so 85.0 reads back as "85" and 82.5 keeps its fraction, the
        # same formatting the account badges use.
        threshold = (
            "" if self._policy.threshold is None else f"{self._policy.threshold:g}"
        )
        order = "" if self._policy.order is None else str(self._policy.order)
        with Vertical(classes="modal-box"):
            yield Label(f"Policy - {self._label}", classes="modal-title")
            yield Static(
                "Per-account overrides. Leave a field empty to clear it.",
                classes="modal-body",
            )
            yield Input(
                value=threshold,
                placeholder=(
                    f"threshold {ACCOUNT_THRESHOLD_MIN:g}-{ACCOUNT_THRESHOLD_MAX:g}"
                    " (empty = global default)"
                ),
                id="threshold",
            )
            yield Input(
                value=order,
                placeholder=(
                    f"chain position {ACCOUNT_ORDER_MIN}-{ACCOUNT_ORDER_MAX}"
                    " (empty = no pin)"
                ),
                id="order",
            )
            yield Checkbox(
                "last man standing (backup)",
                value=self._policy.backup,
                id="backup",
            )
            yield Static("", id="form-error", classes="form-error")
            with Horizontal(classes="modal-buttons"):
                yield Button("Save", id="save")
                yield Button("Cancel", id="cancel")
            yield Static(
                "enter save  ·  tab next field  ·  esc cancel",
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
        error = self.query_one("#form-error", Static)
        # Stripped only to decide whether the field is *empty*; the validator
        # is handed the text the user actually typed. Both normalizers strip
        # internally before parsing but echo the original value in the
        # rejection, so passing the raw string is what makes this message
        # byte-identical to the one the CLI verb prints for the same input.
        raw_threshold = self.query_one("#threshold", Input).value
        raw_order = self.query_one("#order", Input).value
        threshold: float | None = None
        order: int | None = None
        if raw_threshold.strip():
            try:
                threshold = normalize_account_threshold(raw_threshold)
            except ValueError as exc:
                error.update(str(exc))
                return
        if raw_order.strip():
            try:
                order = normalize_account_order(raw_order)
            except ValueError as exc:
                error.update(str(exc))
                return
        error.update("")
        self.dismiss(
            PolicyForm(
                threshold=threshold,
                backup=self.query_one("#backup", Checkbox).value,
                order=order,
            )
        )

    def action_cancel(self) -> None:
        self.dismiss(None)
