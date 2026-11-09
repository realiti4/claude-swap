"""Remote sync in the TUI: the dashboard's remote menu and the repository screen.

The app runs on the usual FakeSwitcher; its RemoteSync runs on a real store
and a real bare git repository (see tests/test_remote.py).
"""
from __future__ import annotations

import subprocess

import pytest

pytest.importorskip("textual")

from claude_swap import remote as rm  # noqa: E402
from tests.test_remote import PASS, TOKEN_A, TOKEN_B, Machine  # noqa: E402
from tests.test_tui import FakeSwitcher, make_account, make_app, menu_select, settle  # noqa: E402

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _fast_kdf(monkeypatch):
    monkeypatch.setattr(rm, "KDF_DEFAULTS", {"name": "scrypt", "n": 2**10, "r": 8, "p": 1})


@pytest.fixture
def bare(tmp_path):
    path = tmp_path / "shared.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(path)], check=True)
    return path


@pytest.fixture
def here(tmp_path, temp_home) -> Machine:
    return Machine(tmp_path / "machine-here", "here")


@pytest.fixture
def other(tmp_path, temp_home) -> Machine:
    return Machine(tmp_path / "machine-other", "other")


def app_for(here: Machine, tmp_path):
    app = make_app(FakeSwitcher([make_account(1, active=True)], tmp_path))
    app._remote_sync = here.r
    return app


def menu_ids(app) -> list[str]:
    from textual.widgets import ListView

    from claude_swap.tui.widgets import MenuItem

    return [i.action_id for i in app.screen.query_one("#menu", ListView).query(MenuItem)]


def menu_labels(app) -> dict[str, str]:
    from textual.widgets import ListView, Static

    from claude_swap.tui.widgets import MenuItem

    menu = app.screen.query_one("#menu", ListView)
    return {i.action_id: i.query_one(Static).render().plain for i in menu.query(MenuItem)}


async def open_remote(pilot, cfg) -> None:
    await menu_select(pilot, "remote-menu")
    await menu_select(pilot, f"remote-open:{cfg.id}")
    await settle(pilot)


class TestRemoteMenu:
    async def test_lists_repositories_and_add(self, here, bare, tmp_path):
        cfg, _, _ = here.r.add("team", bare.as_uri(), passphrase=PASS)
        app = app_for(here, tmp_path)
        async with app.run_test(size=(110, 40)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "remote-menu")
            assert menu_ids(app) == [f"remote-open:{cfg.id}", "remote-add", "back"]
            assert "team" in menu_labels(app)[f"remote-open:{cfg.id}"]


class TestRemoteScreen:
    async def test_opens_with_saved_passphrase_and_pushes(self, here, bare, tmp_path):
        from claude_swap.tui.remote import RemoteScreen

        num = here.token_account(TOKEN_A, "team@example.com")
        cfg, _, _ = here.r.add("team", bare.as_uri(), passphrase=PASS)
        app = app_for(here, tmp_path)
        async with app.run_test(size=(110, 40)) as pilot:
            await settle(pilot)
            await open_remote(pilot, cfg)
            assert isinstance(app.screen, RemoteScreen)
            ids = menu_ids(app)
            assert ids[0] == "push-all" and "row:0" in ids
            assert "local" in menu_labels(app)["row:0"]

            await menu_select(pilot, "push-all")
            await settle(pilot)
            assert "synced" in menu_labels(app)["row:0"]
            assert "push-all" not in menu_ids(app)
        assert cfg.id in here.record(num)["remotes"]

    async def test_row_actions_pull_a_repo_only_account(self, here, other, bare, tmp_path):
        other.r.add("team", bare.as_uri(), passphrase=PASS)
        other.token_account(TOKEN_B, "spare@example.com", alias="spare")
        other.r.push("team", PASS)
        cfg, _, _ = here.r.add("team", bare.as_uri(), passphrase=PASS)
        app = app_for(here, tmp_path)
        async with app.run_test(size=(110, 40)) as pilot:
            await settle(pilot)
            await open_remote(pilot, cfg)
            assert "remote" in menu_labels(app)["row:0"]
            await menu_select(pilot, "row:0")
            assert menu_ids(app)[:3] == ["act-pull:0", "act-skip:0", "act-rm:0"]
            await menu_select(pilot, "act-pull:0")
            await settle(pilot)
            assert "synced" in menu_labels(app)["row:0"]
        num = here.s._find_account_slot(here.s._get_sequence_data(), "spare@example.com", "")
        assert here.token_of(num) == TOKEN_B

    async def test_asks_for_the_passphrase_when_none_is_saved(self, here, bare, tmp_path):
        from claude_swap.tui.remote import PassphraseModal, RemoteScreen

        cfg, _, _ = here.r.add("team", bare.as_uri(), passphrase=PASS, remember=False)
        app = app_for(here, tmp_path)
        async with app.run_test(size=(110, 40)) as pilot:
            await settle(pilot)
            await open_remote(pilot, cfg)
            assert isinstance(app.screen, PassphraseModal)

            app.screen.query_one("#passphrase").value = "wrong passphrase"
            await pilot.press("enter")
            await settle(pilot)
            assert isinstance(app.screen, PassphraseModal)
            assert "does not open" in str(app.screen._message)

            app.screen.query_one("#passphrase").value = PASS
            await pilot.press("enter")
            await settle(pilot)
            assert isinstance(app.screen, RemoteScreen)
            assert "remote-add" not in menu_ids(app) and "passwd" in menu_ids(app)
        assert here.r.stored_passphrase(here.r.get("team")) is None

    async def test_cancelling_the_passphrase_leaves_the_screen(self, here, bare, tmp_path):
        from claude_swap.tui.dashboard import DashboardScreen

        cfg, _, _ = here.r.add("team", bare.as_uri(), passphrase=PASS, remember=False)
        app = app_for(here, tmp_path)
        async with app.run_test(size=(110, 40)) as pilot:
            await settle(pilot)
            await open_remote(pilot, cfg)
            await pilot.press("escape")
            await settle(pilot)
            assert isinstance(app.screen, DashboardScreen)

    async def test_remove_repository_returns_to_the_refreshed_menu(self, here, bare, tmp_path):
        from claude_swap.tui.dashboard import DashboardScreen

        cfg, _, _ = here.r.add("team", bare.as_uri(), passphrase=PASS)
        app = app_for(here, tmp_path)
        async with app.run_test(size=(110, 40)) as pilot:
            await settle(pilot)
            await open_remote(pilot, cfg)
            await menu_select(pilot, "remove")
            await pilot.press("y")
            await settle(pilot)
            assert isinstance(app.screen, DashboardScreen)
            assert menu_ids(app) == ["remote-add", "back"]
        assert here.r.remotes() == []


class TestAddRepository:
    async def test_form_adds_and_opens_the_repository(self, here, bare, tmp_path):
        from claude_swap.tui.remote import AddRemoteModal, RemoteScreen

        app = app_for(here, tmp_path)
        async with app.run_test(size=(110, 50)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "remote-menu")
            await menu_select(pilot, "remote-add")
            assert isinstance(app.screen, AddRemoteModal)
            modal = app.screen
            modal.query_one("#name").value = "team"
            modal.query_one("#url").value = bare.as_uri()
            modal.query_one("#passphrase").value = PASS
            modal.query_one("#again").value = PASS + "typo"
            modal._submit()
            await pilot.pause()
            assert isinstance(app.screen, AddRemoteModal)
            assert "differ" in str(modal.query_one("#form-error").render())

            modal.query_one("#again").value = PASS
            modal._submit()
            await settle(pilot)
            await settle(pilot)
            assert isinstance(app.screen, RemoteScreen)
        assert [r.name for r in here.r.remotes()] == ["team"]

    async def test_unreachable_url_keeps_the_form_open_and_filled(self, here, tmp_path):
        from claude_swap.tui.remote import AddRemoteModal

        app = app_for(here, tmp_path)
        missing = (tmp_path / "missing.git").as_uri()
        async with app.run_test(size=(110, 50)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "remote-menu")
            await menu_select(pilot, "remote-add")
            modal = app.screen
            fill(modal, name="gone", url=missing, passphrase=PASS, again=PASS)
            modal._submit()
            await settle(pilot)
            assert app.screen is modal and isinstance(modal, AddRemoteModal)
            assert "git fetch failed" in str(modal.query_one("#form-error").render())
            assert modal.query_one("#name").value == "gone"
            assert modal.query_one("#url").value == missing
            assert modal.query_one("#passphrase").value == PASS
            assert app.focused is modal.query_one("#url")
            assert not app.busy and not modal.query_one("#ok").disabled
        assert here.r.remotes() == []

    async def test_wrong_passphrase_is_caught_before_the_form_closes(self, here, other, bare, tmp_path):
        from claude_swap.tui.remote import RemoteScreen

        other.r.add("team", bare.as_uri(), passphrase=PASS)
        app = app_for(here, tmp_path)
        async with app.run_test(size=(110, 50)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "remote-menu")
            await menu_select(pilot, "remote-add")
            modal = app.screen
            fill(modal, name="team", url=bare.as_uri(), ssh_key="",
                 passphrase="not the passphrase", again="not the passphrase")
            modal._submit()
            await settle(pilot)
            assert app.screen is modal
            assert "Wrong passphrase" in str(modal.query_one("#form-error").render())
            assert modal.query_one("#name").value == "team"
            assert modal.query_one("#url").value == bare.as_uri()
            assert modal.query_one("#passphrase").value == "" == modal.query_one("#again").value
            assert app.focused is modal.query_one("#passphrase")

            fill(modal, passphrase=PASS, again=PASS)
            modal._submit()
            await settle(pilot)
            await settle(pilot)
            assert isinstance(app.screen, RemoteScreen)
        assert [r.name for r in here.r.remotes()] == ["team"]


    async def test_escape_while_connecting_does_not_lose_the_form(self, here, bare, tmp_path):
        import threading

        from tests.test_tui import wait_event

        started, release = threading.Event(), threading.Event()
        real_add = here.r.add

        def slow_add(*args, **kwargs):
            started.set()
            release.wait(5)
            return real_add(*args, **kwargs)

        here.r.add = slow_add
        app = app_for(here, tmp_path)
        async with app.run_test(size=(110, 50)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "remote-menu")
            await menu_select(pilot, "remote-add")
            modal = app.screen
            fill(modal, name="team", url=bare.as_uri(), passphrase=PASS, again=PASS)
            modal._submit()
            await wait_event(started)
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is modal
            assert modal.query_one("#name").disabled and app.busy
            assert "Still connecting" in str(modal.query_one("#form-error").render())
            release.set()
            await settle(pilot)
            await settle(pilot)
            assert app.screen is not modal and not app.busy
        assert [r.name for r in here.r.remotes()] == ["team"]

def fill(modal, **values) -> None:
    for field_id, value in values.items():
        modal.query_one(f"#{field_id}").value = value
