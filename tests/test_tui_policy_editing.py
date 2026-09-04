"""TUI policy editing — MEU-TUI-01 … MEU-TUI-04.

The per-account policy that ``cswap threshold`` / ``cswap backup`` /
``cswap order`` write from the CLI is authored from inside the dashboard too:
menu → *Per-account policy…* → pick an account → one modal editing all three
fields at once.

The load-bearing property is **cross-surface parity**: the modal calls the same
``normalize_account_*`` validators the CLI verbs call, so the two surfaces
cannot drift apart in what they accept or in the message they reject it with
(AC-16). Everything else follows the shapes the dashboard already uses for
``disable`` — the same absence guard, the same single-flight action, the same
pop-to-root after a write.

Synthetic fixtures only; no real store is read or written (PROJECT-PROFILE §C).
"""

from __future__ import annotations

import dataclasses

import pytest

from claude_swap.models import (
    ACCOUNT_ORDER_MAX,
    ACCOUNT_THRESHOLD_MAX,
    AccountPolicy,
    normalize_account_order,
    normalize_account_threshold,
)
from claude_swap.tui.modals import PolicyForm, PolicyModal

from tests.test_tui import (
    FakeSwitcher,
    make_account,
    make_app,
    menu_select,
    settle,
)

LABEL = "2  user2@example.com"


def _policy(**fields) -> AccountPolicy:
    return dataclasses.replace(AccountPolicy(), **fields)


async def open_modal(pilot, policy: AccountPolicy | None = None) -> dict:
    """Push a ``PolicyModal`` and capture what it dismisses with.

    Returns a dict that gains a ``"form"`` key once the modal dismisses, so a
    test can distinguish "still open" from "dismissed with None".
    """
    seen: dict = {}
    pilot.app.push_screen(
        PolicyModal(LABEL, policy if policy is not None else AccountPolicy()),
        lambda form: seen.__setitem__("form", form),
    )
    await pilot.pause()
    await pilot.pause()
    return seen


def field(pilot, selector: str):
    from textual.widgets import Input

    return pilot.app.screen.query_one(selector, Input)


def error_text(pilot) -> str:
    """The text currently shown in the modal's inline error line.

    ``Static.content`` is what ``update()`` was handed; Textual visualizes it
    lazily, so a plain string stays a plain string here.
    """
    from textual.widgets import Static

    content = pilot.app.screen.query_one("#form-error", Static).content
    return content.plain if hasattr(content, "plain") else str(content)


# ---------------------------------------------------------------------------
# MEU-TUI-01 — PolicyForm + PolicyModal
# ---------------------------------------------------------------------------


class TestPolicyForm:
    """AC-13 — the form mirrors ``AccountPolicy`` field for field."""

    def test_form_has_the_same_shape_as_the_policy_it_edits(self):
        names = {f.name for f in dataclasses.fields(PolicyForm)}
        assert names == {f.name for f in dataclasses.fields(AccountPolicy)}

    def test_form_holds_the_cleared_spelling_of_every_field(self):
        form = PolicyForm(threshold=None, backup=False, order=None)
        assert (form.threshold, form.backup, form.order) == (None, False, None)


@pytest.mark.asyncio
class TestPolicyModal:
    async def test_prefills_from_the_policy_it_was_given(self, tmp_path):
        """AC-14 — the modal opens showing what the account already has."""
        fake = FakeSwitcher([make_account(2)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await open_modal(pilot, _policy(threshold=85.0, backup=True, order=3))
            from textual.widgets import Checkbox

            assert field(pilot, "#threshold").value == "85"
            assert field(pilot, "#order").value == "3"
            assert pilot.app.screen.query_one("#backup", Checkbox).value is True

    async def test_prefill_keeps_a_fractional_threshold(self, tmp_path):
        """AC-14 — ``:g`` formatting, the same rule ``_policy_badges`` uses:
        the field shows back exactly what was set, not 82.500000."""
        fake = FakeSwitcher([make_account(2)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await open_modal(pilot, _policy(threshold=82.5))
            assert field(pilot, "#threshold").value == "82.5"

    async def test_unset_fields_open_empty(self, tmp_path):
        """AC-14 — an account with no policy shows two empty inputs and an
        unchecked box, not a fabricated default."""
        fake = FakeSwitcher([make_account(2)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await open_modal(pilot)
            from textual.widgets import Checkbox

            assert field(pilot, "#threshold").value == ""
            assert field(pilot, "#order").value == ""
            assert pilot.app.screen.query_one("#backup", Checkbox).value is False

    async def test_empty_input_is_the_modal_spelling_of_unset(self, tmp_path):
        """AC-15 — clearing a field clears the override, the TUI's ``--unset``."""
        fake = FakeSwitcher([make_account(2)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            seen = await open_modal(pilot, _policy(threshold=85.0, order=3))
            field(pilot, "#threshold").value = ""
            field(pilot, "#order").value = ""
            await pilot.pause()
            await pilot.click("#save")
            await pilot.pause()
            assert seen["form"] == PolicyForm(
                threshold=None, backup=False, order=None
            )

    async def test_the_clearing_rule_is_stated_in_the_fields(self, tmp_path):
        """AC-15 — a placeholder says so, so the rule is discoverable without
        reading the README."""
        fake = FakeSwitcher([make_account(2)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await open_modal(pilot)
            for selector in ("#threshold", "#order"):
                assert "empty" in field(pilot, selector).placeholder.lower()

    @pytest.mark.parametrize(
        "selector, raw, normalize",
        [
            ("#threshold", "49", normalize_account_threshold),
            ("#threshold", "120", normalize_account_threshold),
            ("#threshold", "abc", normalize_account_threshold),
            ("#order", "0", normalize_account_order),
            ("#order", "1000", normalize_account_order),
            ("#order", "1.5", normalize_account_order),
        ],
    )
    async def test_rejection_matches_the_cli_message_exactly(
        self, tmp_path, selector, raw, normalize
    ):
        """AC-16 — cross-surface parity, the discriminating test.

        The expected string is taken from the validator itself rather than
        retyped here, so verb, long flag and TUI can never drift apart in what
        they reject or in how they say it.
        """
        with pytest.raises(ValueError) as excinfo:
            normalize(raw)
        expected = str(excinfo.value)

        fake = FakeSwitcher([make_account(2)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            seen = await open_modal(pilot)
            field(pilot, selector).value = raw
            await pilot.pause()
            await pilot.click("#save")
            await pilot.pause()
            assert error_text(pilot) == expected
            assert "form" not in seen, "a rejected entry must not dismiss"
            assert fake.calls == []

    async def test_escape_cancels_without_writing(self, tmp_path):
        """AC-17 — esc dismisses with None; no setter runs."""
        fake = FakeSwitcher([make_account(2)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            seen = await open_modal(pilot, _policy(threshold=85.0))
            field(pilot, "#threshold").value = "70"
            await pilot.pause()
            await pilot.press("escape")
            await settle(pilot)
            assert seen["form"] is None
            assert fake.calls == []

    async def test_cancel_button_cancels_without_writing(self, tmp_path):
        """AC-17 — same for the button, so mouse and keyboard agree."""
        fake = FakeSwitcher([make_account(2)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            seen = await open_modal(pilot, _policy(order=2))
            await pilot.click("#cancel")
            await settle(pilot)
            assert seen["form"] is None
            assert fake.calls == []

    async def test_bindings_mirror_the_add_token_modal(self, tmp_path):
        """AC-18 — the same keys do the same things in both modals, and none
        of them advertise themselves in the footer."""
        from claude_swap.tui.modals import AddTokenModal

        def keymap(screen_cls):
            return {
                (b.key, b.action, b.show)
                for b in screen_cls.BINDINGS
            }

        assert keymap(PolicyModal) == keymap(AddTokenModal)

    async def test_enter_in_a_text_field_saves(self, tmp_path):
        """AC-18 — ``on_input_submitted`` submits, so Enter is not a dead key
        in the field the user is already typing in."""
        fake = FakeSwitcher([make_account(2)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            seen = await open_modal(pilot)
            field(pilot, "#threshold").focus()
            await pilot.pause()
            await pilot.press("8", "5")
            await pilot.press("enter")
            await pilot.pause()
            assert seen["form"] == PolicyForm(
                threshold=85.0, backup=False, order=None
            )

    async def test_the_upper_bounds_are_accepted_not_just_the_middle(
        self, tmp_path
    ):
        """AC-16 — the modal takes the validator's own bounds rather than a
        second, narrower opinion about them."""
        fake = FakeSwitcher([make_account(2)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            seen = await open_modal(pilot)
            field(pilot, "#threshold").value = f"{ACCOUNT_THRESHOLD_MAX:g}"
            field(pilot, "#order").value = str(ACCOUNT_ORDER_MAX)
            await pilot.pause()
            await pilot.click("#save")
            await pilot.pause()
            assert seen["form"] == PolicyForm(
                threshold=ACCOUNT_THRESHOLD_MAX,
                backup=False,
                order=ACCOUNT_ORDER_MAX,
            )


class TestFixtureProvenance:
    def test_the_harness_helpers_are_the_shared_ones_not_local_copies(self):
        """AC-34 — this module borrows ``tests.test_tui``'s harness rather
        than growing its own. Every behavioural test here would stay green if
        the helpers were quietly duplicated locally, so identity is the only
        assertion that observes the requirement: same object, same module.
        """
        import tests.test_tui as shared

        for name, borrowed in (
            ("FakeSwitcher", FakeSwitcher),
            ("make_account", make_account),
            ("make_app", make_app),
            ("menu_select", menu_select),
            ("settle", settle),
        ):
            assert borrowed is getattr(shared, name), (
                f"{name} is a local copy, not tests.test_tui's"
            )

    def test_no_test_here_names_a_real_store_path(self):
        """AC-34's other half — synthetic fixtures only. Every store this
        module builds is a ``tmp_path``; nothing reaches for a home directory.
        """
        import pathlib

        src = pathlib.Path("tests/test_tui_policy_editing.py").read_text(
            encoding="utf-8"
        )
        # Assembled from halves so this list does not match itself.
        for forbidden in (
            "Path." + "home()",
            "expand" + "user",
            ".claude-" + "swap-backup",
            "USER" + "PROFILE",
        ):
            assert forbidden not in src, f"{forbidden} has no place in a fixture"


class TestModalStaysInItsLayer:
    def test_modals_module_does_not_reach_into_the_store(self):
        """AC-19 — presentation → policy → store. The modal collects and
        validates; writing is the app's job, and the import graph says so."""
        import pathlib

        src = pathlib.Path("src/claude_swap/tui/modals.py").read_text(
            encoding="utf-8"
        )
        for forbidden in ("switcher", "autoswitch", "settings"):
            assert f"import {forbidden}" not in src
            assert f"from claude_swap.{forbidden}" not in src


# ---------------------------------------------------------------------------
# MEU-TUI-02 — do_edit_policy and the write path
# ---------------------------------------------------------------------------


async def edit(pilot, number: str = "2") -> None:
    """Open the policy editor the way the dashboard opens it."""
    pilot.app.do_edit_policy(number)
    await pilot.pause()
    await pilot.pause()


def check(pilot, value: bool) -> None:
    from textual.widgets import Checkbox

    pilot.app.screen.query_one("#backup", Checkbox).value = value


async def save(pilot) -> None:
    await pilot.pause()
    await pilot.click("#save")
    await settle(pilot)


def spy_start_action(app) -> list[tuple[str, bool]]:
    """Record every ``_start_action`` the edit makes, still running it."""
    seen: list[tuple[str, bool]] = []
    original = app._start_action

    def spy(label, fn, *, show_output: bool = False):
        seen.append((label, show_output))
        return original(label, fn, show_output=show_output)

    app._start_action = spy
    return seen


def notes(app) -> list[str]:
    """Collect toast text instead of rendering it."""
    said: list[str] = []
    app.notify = lambda message, **kw: said.append(str(message))
    return said


def on_policy_modal(app) -> bool:
    return isinstance(app.screen, PolicyModal)


@pytest.mark.asyncio
class TestDoEditPolicy:
    async def test_an_account_that_vanished_is_a_silent_no_op(self, tmp_path):
        """AC-20 — the snapshot can refresh between menu render and Enter; the
        same guard ``do_toggle_disabled`` uses, for the same race."""
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            depth = len(app.screen_stack)
            await edit(pilot, "9")
            assert len(app.screen_stack) == depth
            assert not on_policy_modal(app)
            assert fake.calls == []

    async def test_the_modal_opens_prefilled_from_the_live_snapshot(
        self, tmp_path
    ):
        """AC-21 — what the account has now is what the user is shown."""
        fake = FakeSwitcher(
            [
                make_account(1, active=True),
                make_account(2, policy=_policy(threshold=85.0, order=3)),
            ],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await edit(pilot)
            assert on_policy_modal(app)
            assert field(pilot, "#threshold").value == "85"
            assert field(pilot, "#order").value == "3"

    async def test_the_diff_is_against_what_the_user_was_shown(self, tmp_path):
        """AC-21 — the callback is bound to the policy the modal opened with.

        A refresh lands underneath the open modal and changes the record. The
        submit still matches what was displayed, so nothing is written; a diff
        taken against the *live* snapshot would write 85 back over the 70 the
        user never saw.
        """
        fake = FakeSwitcher(
            [
                make_account(1, active=True),
                make_account(2, policy=_policy(threshold=85.0)),
            ],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await edit(pilot)
            app.snapshot = dataclasses.replace(
                app.snapshot,
                accounts=tuple(
                    dataclasses.replace(
                        a, policy=_policy(threshold=70.0)
                    )
                    if a.number == "2"
                    else a
                    for a in app.snapshot.accounts
                ),
            )
            await save(pilot)
            assert fake.calls == []

    async def test_only_changed_fields_are_written_and_in_a_fixed_order(
        self, tmp_path
    ):
        """AC-22 — threshold, then backup, then order."""
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await edit(pilot)
            field(pilot, "#threshold").value = "70"
            field(pilot, "#order").value = "2"
            check(pilot, True)
            await save(pilot)
            assert fake.calls == [
                ("set_threshold", "2", 70.0),
                ("set_backup", "2", True),
                ("set_order", "2", 2),
            ]

    async def test_an_untouched_field_produces_no_setter_call(self, tmp_path):
        """AC-22 — a one-field edit is a one-setter edit, so a policy the user
        did not touch cannot be rewritten with its own value."""
        fake = FakeSwitcher(
            [
                make_account(1, active=True),
                make_account(2, policy=_policy(threshold=85.0, backup=True)),
            ],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await edit(pilot)
            field(pilot, "#order").value = "4"
            await save(pilot)
            assert fake.calls == [("set_order", "2", 4)]

    async def test_clearing_a_field_writes_the_cleared_value(self, tmp_path):
        """AC-22 — ``None`` is a change like any other, not "no edit"."""
        fake = FakeSwitcher(
            [
                make_account(1, active=True),
                make_account(2, policy=_policy(threshold=85.0, order=3)),
            ],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await edit(pilot)
            field(pilot, "#threshold").value = ""
            await save(pilot)
            assert fake.calls == [("set_threshold", "2", None)]

    async def test_a_submit_that_changes_nothing_writes_nothing(self, tmp_path):
        """AC-23, AC-39 — the TUI's form of the byte-identical no-op the store
        layer holds. The setters short-circuit an unchanged value themselves,
        but leaning on that would still bump ``lastUpdated`` three times.
        """
        fake = FakeSwitcher(
            [
                make_account(1, active=True),
                make_account(2, policy=_policy(threshold=85.0, backup=True, order=3)),
            ],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            said = notes(app)
            await edit(pilot)
            await save(pilot)
            assert fake.calls == []
            assert any("unchanged" in message.lower() for message in said)

    async def test_enter_on_an_unchanged_form_also_writes_nothing(
        self, tmp_path
    ):
        """AC-39 — the no-op has to hold on *both* submit paths. The sibling
        test above submits an unchanged form with the Save button, and
        ``test_enter_in_a_text_field_saves`` submits a *changed* form with
        Enter; neither observes Enter-on-unchanged. An ``on_input_submitted``
        that quietly declined to submit when nothing had changed would pass
        both of them and still lose the user's toast.
        """
        fake = FakeSwitcher(
            [
                make_account(1, active=True),
                make_account(2, policy=_policy(threshold=85.0, backup=True, order=3)),
            ],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            said = notes(app)
            await edit(pilot)
            field(pilot, "#threshold").focus()
            await pilot.pause()
            await pilot.press("enter")
            await settle(pilot)
            assert fake.calls == []
            assert any("unchanged" in message.lower() for message in said)
            assert not on_policy_modal(app), (
                "Enter on an unchanged form must still close the modal"
            )

    async def test_the_whole_edit_is_one_single_flight_action(self, tmp_path):
        """AC-24 — three writes, one ``_start_action``, so the ``busy`` guard
        covers the edit end to end and the writes cannot interleave."""
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            started = spy_start_action(app)
            await edit(pilot)
            field(pilot, "#threshold").value = "70"
            field(pilot, "#order").value = "2"
            check(pilot, True)
            await save(pilot)
            assert len(started) == 1
            assert len(fake.calls) == 3

    async def test_a_multi_field_edit_shows_its_output(self, tmp_path):
        """AC-25 — up to three confirmation lines, and a toast carries one."""
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            started = spy_start_action(app)
            await edit(pilot)
            field(pilot, "#threshold").value = "70"
            field(pilot, "#order").value = "2"
            await save(pilot)
            assert [show for _, show in started] == [True]

    async def test_a_single_field_edit_stays_a_toast(self, tmp_path):
        """AC-25 — one line of confirmation does not deserve a modal."""
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            started = spy_start_action(app)
            await edit(pilot)
            field(pilot, "#threshold").value = "70"
            await save(pilot)
            assert [show for _, show in started] == [False]

    async def test_cancelling_the_app_pushed_modal_writes_nothing(
        self, tmp_path
    ):
        """AC-17 through the app's own callback, not just the modal's."""
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await edit(pilot)
            field(pilot, "#threshold").value = "70"
            await pilot.pause()
            await pilot.press("escape")
            await settle(pilot)
            assert fake.calls == []

    async def test_a_failing_setter_surfaces_through_the_existing_path(
        self, tmp_path
    ):
        """AC-27 — the edit is serial and single-flight, **not transactional**.

        The failure is driven from the *second* setter so the behaviour is
        stated rather than implied: the first write has already persisted, and
        the existing failure ``OutputModal`` is what tells the user which
        fields landed. No new error handling is invented here.
        """
        from claude_swap.exceptions import ClaudeSwitchError
        from claude_swap.tui.modals import OutputModal

        class Exploding(FakeSwitcher):
            def set_account_backup(self, identifier: str, backup: bool) -> None:
                raise ClaudeSwitchError("store is read-only")

        fake = Exploding(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await edit(pilot)
            field(pilot, "#threshold").value = "70"
            field(pilot, "#order").value = "2"
            check(pilot, True)
            await save(pilot)
            assert fake.calls == [("set_threshold", "2", 70.0)]
            assert isinstance(app.screen, OutputModal)
            assert "failed" in app.screen._title
            assert "store is read-only" in app.screen._output


class TestWritePathStaysInItsLayer:
    def test_the_edit_never_reaches_for_the_global_setting(self):
        """AC-26 — ``autoview.py:82-84`` records that the TUI's *global*
        threshold nudge is deliberately session-only. This path writes the
        *account record*, the surface the TUI already writes for ``disabled``,
        ``add`` and ``remove``. Two surfaces; the session-only constraint is
        untouched, and the source says so.
        """
        import inspect

        from claude_swap.tui.app import CswapApp

        src = "".join(
            inspect.getsource(getattr(CswapApp, name))
            for name in ("do_edit_policy", "_on_policy_form", "_write_policy")
        )
        for forbidden in ("settings", "autoswitch", "set_setting"):
            assert forbidden not in src


# ---------------------------------------------------------------------------
# MEU-TUI-03 — menu entry, account list, dispatch
# ---------------------------------------------------------------------------


def menu_ids(app) -> list[str]:
    from textual.widgets import ListView

    from claude_swap.tui.widgets import MenuItem

    menu = app.screen.query_one("#menu", ListView)
    return [item.action_id for item in menu.query(MenuItem)]


def menu_labels(app) -> list[str]:
    from textual.widgets import ListView, Static

    from claude_swap.tui.widgets import MenuItem

    menu = app.screen.query_one("#menu", ListView)
    return [
        item.query_one(Static).render().plain for item in menu.query(MenuItem)
    ]


@pytest.mark.asyncio
class TestPolicyMenu:
    async def test_the_entry_sits_next_to_disable_and_disturbs_nothing(
        self, tmp_path
    ):
        """AC-28 — same family as *Disable / enable*: both shape an account's
        part in auto-rotation, and the neighbouring position is what makes the
        pair discoverable. Every other entry keeps its place."""
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            assert menu_ids(app) == [
                "switch",
                "watch",
                "auto",
                "add-menu",
                "disable-menu",
                "policy-menu",
                "remove-menu",
                "theme-menu",
                "quit",
            ]

    async def test_one_row_per_account_plus_back(self, tmp_path):
        """AC-29 — built exactly like the disable submenu."""
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "policy-menu")
            assert menu_ids(app) == ["policy:1", "policy:2", "back"]
            assert menu_labels(app)[1].startswith("2  user2@example.com")

    async def test_an_alias_is_shown_ahead_of_the_email(self, tmp_path):
        """AC-29 — the label shape the remove and disable menus already use."""
        fake = FakeSwitcher(
            [make_account(1, active=True, alias="dev")], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "policy-menu")
            assert "dev (user1@example.com)" in menu_labels(app)[0]

    async def test_the_row_carries_the_accounts_current_badges(self, tmp_path):
        """AC-30 — one formatter, not two.

        The menu row and the account card are rendered from the same
        ``_policy_badges``, so a threshold cannot read as ``85%`` in one place
        and ``85.0%`` in the other.
        """
        from claude_swap.tui.widgets import AccountsPanel, _policy_badges

        account = make_account(2, policy=_policy(threshold=82.5, order=3))
        fake = FakeSwitcher([make_account(1, active=True), account], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            panel = app.screen.query_one(AccountsPanel).render().plain
            await menu_select(pilot, "policy-menu")
            row = menu_labels(app)[1]
            for badge in _policy_badges(account, ""):
                assert badge in row
                assert badge in panel

    async def test_selecting_a_row_opens_the_editor_and_pops_the_menu(
        self, tmp_path
    ):
        """AC-31 — exactly what ``disable:`` does, so no menu built from
        pre-write values survives the write. The live confirmation surface is
        the accounts panel, which watches the snapshot."""
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "policy-menu")
            await menu_select(pilot, "policy:2")
            assert on_policy_modal(app)
            await pilot.press("escape")
            await settle(pilot)
            assert menu_ids(app)[0] == "switch"

    async def test_an_empty_store_offers_only_back(self, tmp_path):
        """AC-32 — the degenerate shape the disable and remove menus have."""
        fake = FakeSwitcher([], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "policy-menu")
            assert menu_ids(app) == ["back"]
            await menu_select(pilot, "back")
            assert fake.calls == []

    async def test_a_missing_account_dispatches_without_writing(self, tmp_path):
        """AC-32 — dispatching ``policy:`` for an account the snapshot no
        longer has is the AC-20 guard reached through the menu."""
        fake = FakeSwitcher([make_account(1, active=True)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await app.screen._dispatch("policy:9")
            await pilot.pause()
            assert not on_policy_modal(app)
            assert fake.calls == []

    async def test_the_neighbouring_branches_still_work(self, tmp_path):
        """AC-33 — ``policy-menu`` and ``policy:`` are two new ``elif`` arms in
        the existing prefix chain, not a restructuring of it: the branches on
        either side keep behaving exactly as they did."""
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "disable-menu")
            await menu_select(pilot, "disable:2")
            await settle(pilot)
            assert ("set_disabled", "2", True) in fake.calls
            await menu_select(pilot, "theme-menu")
            await menu_select(pilot, "theme:light")
            await settle(pilot)
            assert app._theme_name == "light"
            assert menu_ids(app)[0] == "switch"

    async def test_the_plain_action_map_is_unchanged(self, tmp_path):
        """AC-33 — the new entries are prefix branches; nothing was added to
        the ``actions`` dict, which stays the map of leaf actions."""
        import inspect

        from claude_swap.tui.dashboard import DashboardScreen

        src = inspect.getsource(DashboardScreen._dispatch)
        head = src.split("if action_id ==", 1)[0]
        assert "policy" not in head


# ---------------------------------------------------------------------------
# MEU-TUI-04 — end-to-end flows through the real Pilot harness
# ---------------------------------------------------------------------------


async def open_from_menu(pilot, number: str = "2") -> None:
    """Menu → *Per-account policy…* → the account, as a user reaches it."""
    await menu_select(pilot, "policy-menu")
    await menu_select(pilot, f"policy:{number}")


async def type_into(pilot, selector: str, text: str) -> None:
    field(pilot, selector).focus()
    await pilot.pause()
    await pilot.press(*text)


@pytest.mark.asyncio
class TestEndToEnd:
    """AC-37, AC-38, AC-41 — runtime TUI behaviour verified by its real
    harness, never by inspection. Textual's Pilot needs no display, so these
    run in the agent sandbox alongside the suite's other Pilot tests.
    """

    async def test_setting_a_threshold_end_to_end(self, tmp_path):
        """AC-37 — menu, account, keystrokes, Enter, one setter call."""
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await open_from_menu(pilot)
            await type_into(pilot, "#threshold", "70")
            await pilot.press("enter")
            await settle(pilot)
            assert fake.calls == [("set_threshold", "2", 70.0)]

    async def test_pinning_a_chain_position_end_to_end(self, tmp_path):
        """AC-37 — the same route for ``order``."""
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await open_from_menu(pilot)
            await type_into(pilot, "#order", "2")
            await pilot.press("enter")
            await settle(pilot)
            assert fake.calls == [("set_order", "2", 2)]

    async def test_marking_a_reserve_end_to_end(self, tmp_path):
        """AC-37 — the same route for ``backup``, toggled with space because
        that is the key the checkbox already answers to."""
        from textual.widgets import Checkbox

        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await open_from_menu(pilot)
            app.screen.query_one("#backup", Checkbox).focus()
            await pilot.pause()
            await pilot.press("space")
            await pilot.click("#save")
            await settle(pilot)
            assert fake.calls == [("set_backup", "2", True)]

    async def test_clearing_a_threshold_end_to_end(self, tmp_path):
        """AC-37 — deleting the text is the TUI's ``--unset``, and the badge
        it was showing is gone from the panel afterwards."""
        from claude_swap.tui.widgets import AccountsPanel

        fake = FakeSwitcher(
            [
                make_account(1, active=True),
                make_account(2, policy=_policy(threshold=85.0)),
            ],
            tmp_path,
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            assert "th 85%" in app.screen.query_one(AccountsPanel).render().plain
            await open_from_menu(pilot)
            field(pilot, "#threshold").focus()
            await pilot.pause()
            await pilot.press("backspace", "backspace")
            await pilot.press("enter")
            await settle(pilot)
            assert fake.calls == [("set_threshold", "2", None)]
            assert "th 85%" not in app.screen.query_one(AccountsPanel).render().plain

    async def test_a_rejected_entry_end_to_end(self, tmp_path):
        """AC-38 — the modal stays open, nothing is written, and the message
        is the one the CLI verb prints for the same input."""
        fake = FakeSwitcher(
            [make_account(1, active=True), make_account(2)], tmp_path
        )
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await open_from_menu(pilot)
            await type_into(pilot, "#threshold", "120")
            await pilot.press("enter")
            await settle(pilot)
            with pytest.raises(ValueError) as excinfo:
                normalize_account_threshold("120")
            assert error_text(pilot) == str(excinfo.value)
            assert on_policy_modal(app)
            assert fake.calls == []


@pytest.mark.asyncio
class TestWhitespaceParity:
    """Round-1 review finding F-1 — padded input must not change the message.

    ``_submit`` uses ``.strip()`` only to decide whether a field is *empty*;
    the string handed to the validator is the one the user typed. Both
    normalizers already strip internally for parsing but echo the original
    value in the rejection, so passing the raw text is what makes the TUI
    message byte-identical to the CLI's for the same input — which is what
    AC-16 actually claims. Stripping first silently rewrote ``got ' abc '``
    to ``got 'abc'``.
    """

    @pytest.mark.parametrize(
        "selector, raw, normalize",
        [
            ("#threshold", " abc ", normalize_account_threshold),
            ("#threshold", " nan ", normalize_account_threshold),
            ("#order", " abc ", normalize_account_order),
            ("#order", " 1.5 ", normalize_account_order),
        ],
    )
    async def test_padded_rejection_matches_the_cli_message_exactly(
        self, tmp_path, selector, raw, normalize
    ):
        """The expected string comes from the validator, given the same padded
        text the widget holds — retyping it here would let the two drift."""
        with pytest.raises(ValueError) as excinfo:
            normalize(raw)
        expected = str(excinfo.value)
        assert raw.strip() in expected and repr(raw) in expected, (
            "this case only discriminates while the validator echoes the "
            "original value; if that changes, the case must be re-chosen"
        )

        fake = FakeSwitcher([make_account(2)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            seen = await open_modal(pilot)
            field(pilot, selector).value = raw
            await pilot.pause()
            await pilot.click("#save")
            await pilot.pause()
            assert error_text(pilot) == expected
            assert "form" not in seen, "a rejected entry must not dismiss"
            assert fake.calls == []

    async def test_padded_valid_values_are_still_accepted(self, tmp_path):
        """Passing the raw string must not make the modal stricter than the
        CLI: both normalizers strip before parsing, so padding is legal."""
        fake = FakeSwitcher([make_account(2)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            seen = await open_modal(pilot)
            field(pilot, "#threshold").value = " 85 "
            field(pilot, "#order").value = "  2  "
            await pilot.pause()
            await pilot.click("#save")
            await pilot.pause()
            assert seen["form"].threshold == 85.0
            assert seen["form"].order == 2

    async def test_a_whitespace_only_field_still_means_unset(self, tmp_path):
        """The empty test is the *stripped* one, so a field holding only
        spaces clears the override rather than being sent to the validator."""
        fake = FakeSwitcher([make_account(2)], tmp_path)
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            seen = await open_modal(pilot, _policy(threshold=85.0, order=1))
            field(pilot, "#threshold").value = "   "
            field(pilot, "#order").value = "\t "
            await pilot.pause()
            await pilot.click("#save")
            await pilot.pause()
            assert seen["form"].threshold is None
            assert seen["form"].order is None
