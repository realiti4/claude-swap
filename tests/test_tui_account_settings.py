"""Account settings: one account's alias, switch threshold, priority,
auto-rotation and login sync, edited from the TUI."""
from __future__ import annotations

import dataclasses
import json

import pytest

pytest.importorskip("textual")

from claude_swap.models import AccountPolicy  # noqa: E402
from claude_swap.tui.account_settings import (  # noqa: E402
    AccountSettingsScreen,
    FieldItem,
    field_value,
)
from claude_swap.tui.settings_screen import ValueModal  # noqa: E402
from tests.test_tui import FakeSwitcher, make_account, make_app, menu_select, settle  # noqa: E402


def app_for(tmp_path, accounts=None):
    fake = FakeSwitcher(
        accounts or [make_account(1, active=True), make_account(2)], tmp_path
    )
    return make_app(fake), fake


def capture(app) -> list[tuple[str, dict]]:
    notes: list[tuple[str, dict]] = []
    app.notify = lambda message, **kw: notes.append((str(message), kw))
    return notes


def fields(app) -> list[FieldItem]:
    return list(app.screen.query_one("#menu").query(FieldItem))


def field_text(app, field: str) -> str:
    from textual.widgets import Static

    item = next(f for f in fields(app) if f.field == field)
    return item.query_one(Static).render().plain


def help_text(app) -> str:
    return str(app.screen.query_one("#settings-help").render())


async def highlight(pilot, field: str) -> None:
    menu = pilot.app.screen.query_one("#menu")
    menu.index = next(
        i for i, item in enumerate(menu.children) if getattr(item, "field", None) == field
    )
    await pilot.pause()


async def select_field(pilot, field: str) -> None:
    await highlight(pilot, field)
    await pilot.press("enter")
    await settle(pilot)


async def open_account(pilot, number: str = "2") -> None:
    await settle(pilot)
    await menu_select(pilot, "account-settings-menu")
    await settle(pilot)
    await menu_select(pilot, f"account-settings:{number}")
    await settle(pilot)
    assert isinstance(pilot.app.screen, AccountSettingsScreen)


async def submit(pilot, value: str) -> None:
    modal = pilot.app.screen
    assert isinstance(modal, ValueModal)
    modal.query_one("#value").value = value
    await pilot.press("enter")
    await settle(pilot)


def form_error(app) -> str:
    return str(app.screen.query_one("#form-error").render())


# ---------------------------------------------------------------------------
# the values shown
# ---------------------------------------------------------------------------


class TestFieldValue:
    def test_defaults(self):
        acc = make_account(3)
        assert field_value("alias", acc, global_threshold=90) == ("none", True)
        assert field_value("threshold", acc, global_threshold=85) == ("global (85%)", True)
        assert field_value("priority", acc, global_threshold=90) == ("0", True)
        assert field_value("rotation", acc, global_threshold=90) == ("on", True)
        assert field_value("login-sync", acc, global_threshold=90) == ("on", True)

    def test_overrides(self):
        acc = dataclasses.replace(
            make_account(3, alias="work", disabled=True, login_sync=False),
            policy=AccountPolicy(threshold=72.5, priority=5),
        )
        assert field_value("alias", acc, global_threshold=90) == ("work", False)
        assert field_value("threshold", acc, global_threshold=90) == ("72.5%", False)
        assert field_value("priority", acc, global_threshold=90) == ("5", False)
        assert field_value("rotation", acc, global_threshold=90) == ("off", False)
        assert field_value("login-sync", acc, global_threshold=90) == ("off", False)

    def test_login_sync_does_not_apply_to_tokens_or_keys(self):
        token = make_account(3, setup_token=True)
        key = make_account(4, kind="api_key")
        assert field_value("login-sync", token, global_threshold=90) == ("n/a (setup-token)", True)
        assert field_value("login-sync", key, global_threshold=90) == ("n/a (API key)", True)


# ---------------------------------------------------------------------------
# the screen
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestScreen:
    async def test_menu_lists_accounts_with_their_overrides(self, tmp_path):
        accounts = [
            make_account(1, active=True),
            dataclasses.replace(
                make_account(2, alias="work", disabled=True, login_sync=False),
                policy=AccountPolicy(threshold=80, priority=3),
            ),
        ]
        app, _fake = app_for(tmp_path, accounts)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "account-settings-menu")
            await settle(pilot)
            from claude_swap.tui.widgets import MenuItem

            labels = {
                item.action_id: str(item.query_one("Static").render())
                for item in app.screen.query(MenuItem)
            }
            assert "work (user2@example.com)" in labels["account-settings:2"]
            for part in ("threshold 80%", "priority 3", "auto-rotation off", "login sync off"):
                assert part in labels["account-settings:2"]
            assert "threshold" not in labels["account-settings:1"]

    async def test_lists_fields_and_help(self, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({"autoswitch": {"threshold": 85}}))
        app, _fake = app_for(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await open_account(pilot)
            assert [f.field for f in fields(app)] == [
                "alias", "threshold", "priority", "rotation", "login-sync",
            ]
            assert "user2@example.com" in str(app.screen.query_one("#menu-title").render())
            assert "global (85%)" in field_text(app, "threshold")
            assert "(default)" in field_text(app, "threshold")
            await highlight(pilot, "threshold")
            assert "cswap threshold 2 PCT" in help_text(app)
            await highlight(pilot, "priority")
            assert "priority-consume-first strategy uses it (now: best)" in help_text(app)

    async def test_alias_form_sets_and_validates(self, tmp_path):
        accounts = [make_account(1, active=True, alias="home"), make_account(2)]
        app, fake = app_for(tmp_path, accounts)
        async with app.run_test(size=(120, 40)) as pilot:
            await open_account(pilot)
            notes = capture(app)
            await select_field(pilot, "alias")
            await submit(pilot, "9")
            assert isinstance(app.screen, ValueModal)
            assert "purely numeric" in form_error(app)
            assert app.screen.query_one("#value").value == "9"
            await submit(pilot, "home")
            assert isinstance(app.screen, ValueModal)
            assert "already used by account 1" in form_error(app)
            await submit(pilot, "Work")
            assert ("set_alias", "2", "Work") in fake.calls
            assert notes[-1][0] == "Set alias 'Work' for account 2."
            assert "Work" in field_text(app, "alias")
            assert isinstance(app.screen, AccountSettingsScreen)

    async def test_alias_empty_or_default_removes_it(self, tmp_path):
        app, fake = app_for(tmp_path, [make_account(1, active=True), make_account(2, alias="w")])
        async with app.run_test(size=(120, 40)) as pilot:
            await open_account(pilot)
            notes = capture(app)
            await select_field(pilot, "alias")
            await submit(pilot, "")
            assert ("unset_alias", "2") in fake.calls
            assert notes[-1][0] == "Removed the alias for account 2."
            assert "none" in field_text(app, "alias")

    async def test_threshold_form(self, tmp_path):
        app, fake = app_for(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await open_account(pilot)
            notes = capture(app)
            await select_field(pilot, "threshold")
            await submit(pilot, "20")
            assert isinstance(app.screen, ValueModal)
            assert form_error(app).startswith("Threshold must be")
            await submit(pilot, "75")
            assert ("set_threshold", "2", 75.0) in fake.calls
            assert "75%" in field_text(app, "threshold")
            assert "no restart" in notes[-1][0]
            # Default button: back to the global value
            await select_field(pilot, "threshold")
            app.screen.query_one("#default").press()
            await settle(pilot)
            assert ("set_threshold", "2", None) in fake.calls
            assert "global (90%)" in field_text(app, "threshold")

    async def test_priority_form_notes_the_strategy(self, tmp_path):
        app, fake = app_for(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await open_account(pilot)
            notes = capture(app)
            await select_field(pilot, "priority")
            await submit(pilot, "abc")
            assert "integer between 0 and" in form_error(app)
            await submit(pilot, "4")
            assert ("set_priority", "2", 4) in fake.calls
            assert "Only the priority-consume-first strategy uses it (now: best)." in notes[-1][0]
            assert field_text(app, "priority").split()[-1] == "4"

    async def test_priority_note_under_that_strategy(self, tmp_path):
        (tmp_path / "settings.json").write_text(
            json.dumps({"autoswitch": {"strategy": "priority-consume-first"}})
        )
        app, _fake = app_for(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await open_account(pilot)
            notes = capture(app)
            await select_field(pilot, "priority")
            await submit(pilot, "2")
            assert "no restart" in notes[-1][0]

    async def test_same_value_writes_nothing(self, tmp_path):
        app, fake = app_for(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await open_account(pilot)
            notes = capture(app)
            await select_field(pilot, "priority")
            await submit(pilot, "0")
            assert not any(c[0] == "set_priority" for c in fake.calls)
            assert notes[-1][0] == "Account 2 already has priority 0."

    async def test_toggles(self, tmp_path):
        app, fake = app_for(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await open_account(pilot)
            notes = capture(app)
            await select_field(pilot, "rotation")
            assert ("set_disabled", "2", True) in fake.calls
            assert field_text(app, "rotation").split()[-1] == "off"
            assert "disabled" not in field_text(app, "rotation")
            assert notes[-1][0].startswith("Auto-rotation off for account 2. Applies now")
            await select_field(pilot, "rotation")
            assert ("set_disabled", "2", False) in fake.calls
            await select_field(pilot, "login-sync")
            assert ("set_login_sync", "2", False) in fake.calls
            assert field_text(app, "login-sync").split()[-1] == "off"
            # the cursor stays on the row that changed
            assert app.screen.query_one("#menu").highlighted_child.field == "login-sync"

    async def test_login_sync_not_offered_for_a_setup_token(self, tmp_path):
        accounts = [make_account(1, active=True), make_account(2, setup_token=True)]
        app, fake = app_for(tmp_path, accounts)
        async with app.run_test(size=(120, 40)) as pilot:
            await open_account(pilot)
            notes = capture(app)
            await select_field(pilot, "login-sync")
            assert not any(c[0] == "set_login_sync" for c in fake.calls)
            assert "Doesn't apply" in notes[-1][0]

    async def test_d_resets_the_highlighted_field(self, tmp_path):
        accounts = [
            make_account(1, active=True),
            dataclasses.replace(
                make_account(2, disabled=True), policy=AccountPolicy(threshold=70, priority=2)
            ),
        ]
        app, fake = app_for(tmp_path, accounts)
        async with app.run_test(size=(120, 40)) as pilot:
            await open_account(pilot)
            notes = capture(app)
            await highlight(pilot, "threshold")
            await pilot.press("d")
            await settle(pilot)
            assert ("set_threshold", "2", None) in fake.calls
            await highlight(pilot, "priority")
            await pilot.press("d")
            await settle(pilot)
            assert ("set_priority", "2", None) in fake.calls
            await highlight(pilot, "rotation")
            await pilot.press("d")
            await settle(pilot)
            assert ("set_disabled", "2", False) in fake.calls
            await highlight(pilot, "alias")
            await pilot.press("d")
            await settle(pilot)
            assert notes[-1][0] == "Alias is already the default."
            assert not any(c[0] == "unset_alias" for c in fake.calls)

    async def test_escape_goes_back(self, tmp_path):
        app, _fake = app_for(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await open_account(pilot)
            await pilot.press("escape")
            await settle(pilot)
            assert not isinstance(app.screen, AccountSettingsScreen)

    async def test_closes_when_the_account_is_removed(self, tmp_path):
        app, fake = app_for(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await open_account(pilot)
            notes = capture(app)
            fake._accounts = [a for a in fake._accounts if a.number != "2"]
            app.request_refresh()
            await settle(pilot)
            assert not isinstance(app.screen, AccountSettingsScreen)
            assert notes[-1][0] == "Account 2 is gone."
