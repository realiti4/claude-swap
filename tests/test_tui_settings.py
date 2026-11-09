"""The settings screen: every `cswap config` key in the TUI, and the notice
that says when a change applies (and what must restart for it)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

pytest.importorskip("textual")

from claude_swap import autoswitch  # noqa: E402
from claude_swap.settings import SETTING_SPECS, cli_override_keys  # noqa: E402
from claude_swap.tui.settings_screen import (  # noqa: E402
    SettingItem,
    SettingsScreen,
    SettingValueModal,
    change_notice,
    value_text,
)
from tests.test_tui import FakeSwitcher, make_account, make_app, menu_select, settle  # noqa: E402


def settings_file(tmp_path) -> dict:
    path = tmp_path / "settings.json"
    return json.loads(path.read_text()) if path.exists() else {}


def app_for(tmp_path, **attrs):
    fake = FakeSwitcher([make_account(1, active=True), make_account(2)], tmp_path)
    for name, value in attrs.items():
        setattr(fake, name, value)
    app = make_app(fake)
    return app


def capture(app) -> list[tuple[str, dict]]:
    notes: list[tuple[str, dict]] = []
    app.notify = lambda message, **kw: notes.append((str(message), kw))
    return notes


def rows(app) -> list[SettingItem]:
    from textual.widgets import ListView

    return list(app.screen.query_one("#menu", ListView).query(SettingItem))


def row_text(app, key: str) -> str:
    item = next(r for r in rows(app) if r.key == key)
    from textual.widgets import Static

    return item.query_one(Static).render().plain


async def select_setting(pilot, key: str) -> None:
    from textual.widgets import ListView

    menu = pilot.app.screen.query_one("#menu", ListView)
    items = list(menu.children)
    menu.index = next(i for i, item in enumerate(items) if getattr(item, "key", None) == key)
    await pilot.pause()
    await pilot.press("enter")
    await settle(pilot)


async def open_settings(pilot) -> None:
    await settle(pilot)
    await menu_select(pilot, "settings")
    await settle(pilot)
    assert isinstance(pilot.app.screen, SettingsScreen)


def register_engine(backup_dir, *, host="cswap auto", overrides=(), dry_run=False):
    """A registry record for a loop "running" in this test process."""
    engine = SimpleNamespace(
        switcher=SimpleNamespace(backup_dir=backup_dir),
        host=host, dry_run=dry_run, overrides=tuple(overrides),
    )
    return autoswitch._register_engine(engine)


# ---------------------------------------------------------------------------
# the screen
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestScreen:
    async def test_lists_every_setting_with_its_value(self, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({"autoswitch": {"cooldownSeconds": 600}}))
        app = app_for(tmp_path)
        async with app.run_test(size=(110, 40)) as pilot:
            await open_settings(pilot)
            assert [r.key for r in rows(app)] == list(SETTING_SPECS)
            first = row_text(app, "autoswitch.threshold")
            assert first.startswith("auto-switch") and "threshold (5h)" in first
            assert "90%" in first and "(default)" in first
            cooldown = row_text(app, "autoswitch.cooldownSeconds")
            assert "600 s (10m)" in cooldown and "(default)" not in cooldown
            assert "same as threshold" in row_text(app, "autoswitch.weeklyThreshold")
            assert not row_text(app, "autoswitch.weeklyThreshold").startswith("auto-switch")
            assert row_text(app, "ui.theme").startswith("display")

    async def test_toggle_writes_and_says_when_it_applies(self, tmp_path):
        app = app_for(tmp_path)
        async with app.run_test(size=(110, 40)) as pilot:
            await open_settings(pilot)
            notes = capture(app)
            await select_setting(pilot, "usage.headerProbe")
            assert settings_file(tmp_path)["usage"]["headerProbe"] is True
            assert "on" in row_text(app, "usage.headerProbe")
            assert notes[-1][0] == "Setup-token usage probe: on. Applies from the next usage poll."
            assert notes[-1][1]["severity"] == "information"

    async def test_choice_list(self, tmp_path):
        app = app_for(tmp_path)
        async with app.run_test(size=(110, 40)) as pilot:
            await open_settings(pilot)
            await select_setting(pilot, "autoswitch.strategy")
            await menu_select(pilot, "choice:weekly-first")
            await settle(pilot)
            assert settings_file(tmp_path)["autoswitch"]["strategy"] == "weekly-first"
            assert "weekly-first" in row_text(app, "autoswitch.strategy")
            # back on the list, on the same row
            assert app.screen.query_one("#menu").highlighted_child.key == "autoswitch.strategy"

    async def test_number_form_validates_and_keeps_what_was_typed(self, tmp_path):
        app = app_for(tmp_path)
        async with app.run_test(size=(110, 40)) as pilot:
            await open_settings(pilot)
            notes = capture(app)
            await select_setting(pilot, "autoswitch.threshold")
            modal = app.screen
            assert isinstance(modal, SettingValueModal)
            modal.query_one("#value").value = "200"
            await pilot.press("enter")
            await pilot.pause()
            assert app.screen is modal
            assert "between 50 and 99.9" in str(modal.query_one("#form-error").render())
            assert modal.query_one("#value").value == "200"

            modal.query_one("#value").value = "85"
            await pilot.press("enter")
            await settle(pilot)
            assert isinstance(app.screen, SettingsScreen)
        assert settings_file(tmp_path)["autoswitch"]["threshold"] == 85
        assert app.threshold_pct == 85
        message, kw = notes[-1]
        assert message.startswith("Threshold (5h): 85%. No auto-switch is running")
        assert kw["title"] == "Saved"

    async def test_empty_optional_value_goes_back_to_its_default(self, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({"autoswitch": {"weeklyThreshold": 97}}))
        app = app_for(tmp_path)
        async with app.run_test(size=(110, 40)) as pilot:
            await open_settings(pilot)
            await select_setting(pilot, "autoswitch.weeklyThreshold")
            app.screen.query_one("#value").value = ""
            await pilot.press("enter")
            await settle(pilot)
            assert "same as threshold" in row_text(app, "autoswitch.weeklyThreshold")
        assert "autoswitch" not in settings_file(tmp_path)
        assert app.weekly_threshold_pct is None

    async def test_d_resets_the_highlighted_setting(self, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({"updates": {"check": False}}))
        app = app_for(tmp_path)
        async with app.run_test(size=(110, 40)) as pilot:
            await open_settings(pilot)
            notes = capture(app)
            menu = app.screen.query_one("#menu")
            menu.index = next(i for i, r in enumerate(menu.children) if getattr(r, "key", "") == "updates.check")
            await pilot.pause()
            await pilot.press("d")
            await settle(pilot)
            assert "(default)" in row_text(app, "updates.check")
            await pilot.press("d")
            await settle(pilot)
        assert "updates" not in settings_file(tmp_path)
        assert notes[0][0] == "Daily update check: on (default). Applies from the next cswap command."
        assert notes[1][0] == "Daily update check is already the default."

    async def test_theme_applies_now(self, tmp_path):
        app = app_for(tmp_path)
        async with app.run_test(size=(110, 40)) as pilot:
            await open_settings(pilot)
            await select_setting(pilot, "ui.theme")
            await menu_select(pilot, "choice:light")
            await settle(pilot)
            assert app.theme == "cswap-light"
        assert settings_file(tmp_path)["ui"]["theme"] == "light"

    async def test_running_auto_switch_needs_a_restart(self, tmp_path):
        register_engine(tmp_path, host="cswap auto")
        register_engine(tmp_path, host="menu bar")
        register_engine(tmp_path, host="cswap auto", overrides=["autoswitch.cooldownSeconds"])
        app = app_for(tmp_path)
        async with app.run_test(size=(110, 40)) as pilot:
            await open_settings(pilot)
            notes = capture(app)
            await select_setting(pilot, "autoswitch.cooldownSeconds")
            app.screen.query_one("#value").value = "120"
            await pilot.press("enter")
            await settle(pilot)
        message, kw = notes[-1]
        assert kw["title"] == "Restart needed" and kw["severity"] == "warning"
        pid = os.getpid()
        assert f"cswap auto (pid {pid}): stop it (Ctrl+C) and start it again" in message
        assert f"menu bar (pid {pid}): turn Auto-switch accounts off and on" in message
        assert "overrides this setting" in message

    async def test_live_cswap_run_sessions_need_a_restart(self, tmp_path):
        app = app_for(tmp_path, live_session_pids_for=lambda num, email: [41, 42] if num == "2" else [])
        async with app.run_test(size=(110, 40)) as pilot:
            await open_settings(pilot)
            notes = capture(app)
            await select_setting(pilot, "run.shareHistory")
        message, kw = notes[-1]
        assert kw["title"] == "Restart needed"
        assert "Share chat history: off. 2 running cswap run sessions (2 " in message

    async def test_same_value_says_nothing_changes(self, tmp_path):
        app = app_for(tmp_path)
        async with app.run_test(size=(110, 40)) as pilot:
            await open_settings(pilot)
            notes = capture(app)
            await select_setting(pilot, "autoswitch.threshold")
            await pilot.press("enter")  # the prefilled default, 90
            await settle(pilot)
        assert notes[-1][0] == "Threshold (5h): 90%. Same value as before (explicit now); nothing changes."
        assert settings_file(tmp_path)["autoswitch"]["threshold"] == 90

    async def test_a_broken_settings_file_is_reported_not_overwritten(self, tmp_path):
        (tmp_path / "settings.json").write_text("{broken")
        app = app_for(tmp_path)
        async with app.run_test(size=(110, 40)) as pilot:
            await open_settings(pilot)
            notes = capture(app)
            await select_setting(pilot, "updates.check")
        assert notes[-1][1]["severity"] == "error" and "not valid JSON" in notes[-1][0]
        assert (tmp_path / "settings.json").read_text() == "{broken"

    async def test_escape_leaves_and_backs_out_of_a_choice_list(self, tmp_path):
        from claude_swap.tui.dashboard import DashboardScreen

        app = app_for(tmp_path)
        async with app.run_test(size=(110, 40)) as pilot:
            await open_settings(pilot)
            await select_setting(pilot, "autoswitch.strategy")
            await pilot.press("escape")
            await pilot.pause()
            assert any(isinstance(r, SettingItem) for r in app.screen.query_one("#menu").children)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, DashboardScreen)


# ---------------------------------------------------------------------------
# the notice
# ---------------------------------------------------------------------------


class TestNotice:
    def test_no_engine_no_restart(self):
        notice = change_notice("autoswitch.strategy", "Strategy: best")
        assert notice.title == "Saved" and "nothing needs a restart" in notice.message

    def test_each_engine_and_how_to_restart_it(self):
        engines = [
            {"pid": 1, "host": "cswap auto", "dryRun": True, "overrides": []},
            {"pid": 2, "host": "TUI auto view", "dryRun": False, "overrides": []},
            {"pid": 3, "host": "cswap auto", "dryRun": False, "overrides": ["autoswitch.model"]},
        ]
        notice = change_notice("autoswitch.model", "Per-model weekly limits: Fable", engines=engines)
        assert notice.severity == "warning"
        lines = notice.message.splitlines()
        assert lines[1] == "• cswap auto --dry-run (pid 1): stop it (Ctrl+C) and start it again"
        assert lines[2] == "• TUI auto view (pid 2): leave its auto view and open it again"
        assert lines[3].startswith("• cswap auto (pid 3): started with a flag that overrides")

    def test_engines_dont_matter_outside_auto_switch(self):
        engines = [{"pid": 1, "host": "cswap auto", "dryRun": False, "overrides": []}]
        assert change_notice("usage.headerProbe", "x", engines=engines).title == "Saved"

    def test_share_history(self):
        assert change_notice("run.shareHistory", "x").message == "x. The next cswap run uses it."
        notice = change_notice("run.shareHistory", "x", run_sessions=[("1 a@x.io", 1)])
        assert notice.title == "Restart needed" and "1 running cswap run session (1 a@x.io)" in notice.message

    @pytest.mark.parametrize(("key", "value", "text"), [
        ("autoswitch.threshold", 85.5, "85.5%"),
        ("autoswitch.weeklyThreshold", None, "same as threshold"),
        ("autoswitch.intervalSeconds", 45.0, "45 s"),
        ("autoswitch.cooldownSeconds", 300.0, "300 s (5m)"),
        ("autoswitch.unhealthyTicks", 1, "1 failed poll"),
        ("autoswitch.unhealthyTicks", 3, "3 failed polls"),
        ("autoswitch.model", None, "off"),
        ("autoswitch.model", "Fable,Opus", "Fable,Opus"),
        ("run.shareHistory", True, "on"),
    ])
    def test_value_text(self, key, value, text):
        assert value_text(SETTING_SPECS[key], value) == text


# ---------------------------------------------------------------------------
# which auto-switch loops are running
# ---------------------------------------------------------------------------


class TestRunningEngines:
    def test_register_list_unregister(self, tmp_path):
        path = register_engine(tmp_path, host="menu bar", overrides=["autoswitch.threshold"], dry_run=True)
        engines = autoswitch.running_engines(tmp_path)
        assert [(e["pid"], e["host"], e["dryRun"], e["overrides"]) for e in engines] == [
            (os.getpid(), "menu bar", True, ["autoswitch.threshold"])]
        autoswitch._unregister_engine(path)
        assert autoswitch.running_engines(tmp_path) == []

    def test_a_dead_process_is_pruned(self, tmp_path):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        folder = tmp_path / autoswitch.ENGINES_DIRNAME
        folder.mkdir()
        (folder / f"{proc.pid}-dead.json").write_text(json.dumps({"pid": proc.pid, "host": "cswap auto"}))
        (folder / "junk.json").write_text("{")
        assert autoswitch.running_engines(tmp_path) == []
        assert not (folder / f"{proc.pid}-dead.json").exists()

    @pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc start ticks")
    def test_a_recycled_pid_is_pruned(self, tmp_path):
        folder = tmp_path / autoswitch.ENGINES_DIRNAME
        folder.mkdir()
        (folder / "x.json").write_text(json.dumps({"pid": os.getpid(), "procStart": "1"}))
        assert autoswitch.running_engines(tmp_path) == []

    def test_run_loop_is_listed_while_it_runs(self, tmp_path):
        engine = autoswitch.AutoSwitchEngine.__new__(autoswitch.AutoSwitchEngine)
        engine.switcher = SimpleNamespace(backup_dir=tmp_path)
        engine.host, engine.dry_run, engine.overrides = "cswap auto", False, ()
        seen = []
        engine._loop = lambda: seen.append(autoswitch.running_engines(tmp_path)) or 0
        assert engine.run_loop() == 0
        assert [e["host"] for e in seen[0]] == ["cswap auto"]
        assert autoswitch.running_engines(tmp_path) == []

    def test_registry_trouble_never_stops_the_engine(self, tmp_path):
        engine = autoswitch.AutoSwitchEngine.__new__(autoswitch.AutoSwitchEngine)
        engine.switcher = SimpleNamespace(backup_dir=tmp_path / "file")
        (tmp_path / "file").write_text("not a folder")
        engine.host, engine.dry_run, engine.overrides = "cswap auto", False, ()
        engine._loop = lambda: 7
        assert engine.run_loop() == 7

    def test_cli_override_keys(self):
        args = SimpleNamespace(threshold=80.0, weekly_threshold=None, interval=None, cooldown=30.0,
                               include_api_key_accounts=None, model=None, strategy="best")
        assert cli_override_keys(args) == (
            "autoswitch.threshold", "autoswitch.cooldownSeconds", "autoswitch.strategy")
