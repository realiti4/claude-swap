"""Tests for the cross-platform system-tray frontend (``cswap tray``).

Mirrors tests/test_menubar.py: the tray reuses menubar.py's pure display
helpers, so the interesting new surface is (1) the pure menu *model* the tray
renders (``build_menu_model``), (2) the pure icon-bitmap renderer
(``render_icon_image``), (3) the reveal-in-file-manager command builder, (4) the
lazy-import clean-error contract of ``run()``, and (5) the ``_TrayController``
action dispatch (switch/add/remove/settings/auto-switch) driven with a fake
switcher and injected notify/confirm/prompt — no pystray, PIL, or tkinter, and
no real account/keychain/network access.
"""

from __future__ import annotations

import sys

import pytest

from claude_swap import menubar, tray
from claude_swap.exceptions import ClaudeSwitchError

# --- shared fixtures (clock-frozen, mirrors test_menubar.py) -------------------

_NOW = 1_000_000.0


def _iso(delta_s: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(_NOW + delta_s, tz=timezone.utc).isoformat()


_USAGE = {
    "five_hour": {"pct": 42, "resets_at": _iso(3600)},
    "seven_day": {"pct": 18, "resets_at": _iso(3 * 86400)},
    "spend": {"pct": 30},
}


def _view(accounts=None, active_email=None, active_usage=None, active_alias=None):
    """Build the adapted-snapshot render dict build_menu_model consumes.

    accounts: list of (num, email, is_active, display, last_good, alias,
    disabled, fetched_at) 8-tuples (the menubar._adapt_snapshot contract).
    """
    return {
        "accounts": list(accounts or []),
        "active_email": active_email,
        "active_usage": active_usage,
        "active_alias": active_alias,
    }


def _acct_row(num, email, *, is_active=False, usage=None, alias="", disabled=False):
    return (num, email, is_active, usage, usage, alias, disabled, _NOW)


def _settings(**over):
    s = menubar.MenuBarSettings()
    for k, v in over.items():
        setattr(s, k, v)
    return s


def _find(nodes, label_startswith):
    for n in nodes:
        if n.label.startswith(label_startswith):
            return n
    return None


def _labels(nodes):
    return [n.label for n in nodes]


def _actions(nodes):
    return [n.action for n in nodes]


# --- fakes --------------------------------------------------------------------


class _FakeLogger:
    def __init__(self):
        self.messages = []

    def info(self, *a, **k):
        self.messages.append(("info", a, k))

    debug = info
    warning = info


class _FakeSwitcher:
    """Read + mutate surface the tray controller drives."""

    def __init__(self, tmp_path, *, accounts=(), has_add_token=True):
        self.backup_dir = tmp_path
        self._accounts = list(accounts)
        self._logger = _FakeLogger()
        self.calls = []
        self._config = tmp_path / ".claude.json"
        self._config.write_text("{}", encoding="utf-8")
        if not has_add_token:
            # emulate a switcher build without token support
            del self.add_account_from_token

    # read side
    def _get_claude_config_path(self):
        return self._config

    def _get_current_account(self):
        return None

    def has_live_login(self):
        return False

    def current_account_number(self):
        return None

    # mutate side (record calls)
    def switch(self, strategy=None):
        self.calls.append(("switch", strategy))

    def switch_to(self, identifier):
        self.calls.append(("switch_to", identifier))

    def remove_account(self, identifier, assume_yes=False):
        self.calls.append(("remove_account", identifier, assume_yes))

    def set_account_disabled(self, identifier, disabled):
        self.calls.append(("set_account_disabled", identifier, disabled))

    def add_account(self, slot=None, alias=None):
        self.calls.append(("add_account", slot, alias))

    def add_account_from_token(self, token=None, email=None, slot=None):
        self.calls.append(("add_account_from_token", token, email, slot))


class _RaisingSwitcher(_FakeSwitcher):
    def switch(self, strategy=None):
        raise ClaudeSwitchError("boom")

    def switch_to(self, identifier):
        raise ClaudeSwitchError("boom")


def _controller(tmp_path, switcher=None, **inject):
    sw = switcher or _FakeSwitcher(tmp_path)
    rec = {"notify": [], "confirm": [], "prompt": [], "reveal": [], "quit": [], "menu_changed": 0}

    def notify(title, message):
        rec["notify"].append((title, message))

    def confirm(message):
        rec["confirm"].append(message)
        return inject.get("confirm_result", True)

    def prompt(message):
        rec["prompt"].append(message)
        answers = inject.get("prompt_results", [])
        return answers.pop(0) if answers else None

    def reveal(path):
        rec["reveal"].append(path)

    def on_quit():
        rec["quit"].append(True)

    def on_menu_changed():
        rec["menu_changed"] += 1

    ctrl = tray._TrayController(
        sw,
        notify=notify,
        confirm=confirm,
        prompt=prompt,
        reveal=reveal,
        on_quit=on_quit,
        on_menu_changed=on_menu_changed,
    )
    # never spawn real background threads/engines in unit tests
    ctrl._request_refresh = lambda full=False: rec.setdefault("refresh", []).append(full)
    ctrl._start_engine = lambda: rec.setdefault("engine", []).append("start")
    ctrl._stop_engine = lambda: rec.setdefault("engine", []).append("stop")
    ctrl._restart_engine = lambda: rec.setdefault("engine", []).append("restart")
    return ctrl, sw, rec


# ============================================================================
# build_menu_model — the pure menu tree
# ============================================================================


def test_menu_model_empty_accounts_has_placeholder_and_grouping():
    nodes = tray.build_menu_model(
        _view(), _settings(), global_threshold=90, per_account_thresholds={},
        switch_history=[], has_add_token=True,
    )
    labels = _labels(nodes)
    assert "No managed accounts" in labels
    # grouping submenus + quit always present
    assert _find(nodes, "Switch") is not None
    assert _find(nodes, "Manage accounts") is not None
    assert _find(nodes, "Settings") is not None
    quit_node = _find(nodes, "Quit")
    assert quit_node is not None and quit_node.action == ("quit",)


def test_menu_model_account_rows_active_marker_and_switch_action():
    view = _view(
        accounts=[
            _acct_row("1", "a@x.com", is_active=True, usage=_USAGE),
            _acct_row("2", "b@x.com", usage=_USAGE, alias="work"),
        ],
        active_email="a@x.com",
        active_usage=_USAGE,
    )
    nodes = tray.build_menu_model(
        view, _settings(), global_threshold=90, per_account_thresholds={},
        switch_history=[], has_add_token=True,
    )
    account_nodes = [n for n in nodes if n.submenu and n.action is None
                     and _find(n.submenu, "Switch to this account")]
    assert len(account_nodes) == 2
    # active account row carries the ● marker (submenu-parent check is unreliable)
    active = account_nodes[0]
    assert active.label.startswith("●")
    # first child switches to that specific account
    first_child = active.submenu[0]
    assert first_child.label == "Switch to this account"
    assert first_child.action == ("switch_to", "1")
    # per-account preference submenus present
    assert _find(active.submenu, "Show in menu-bar title") is not None
    assert _find(active.submenu, "Auto-swap away at") is not None


def test_menu_model_account_title_pct_radio_reflects_settings():
    view = _view(accounts=[_acct_row("1", "a@x.com", is_active=True, usage=_USAGE)],
                 active_email="a@x.com", active_usage=_USAGE)
    nodes = tray.build_menu_model(
        view, _settings(account_pct={"a@x.com": "5h"}), global_threshold=90,
        per_account_thresholds={}, switch_history=[], has_add_token=True,
    )
    acct = nodes[0]
    title_menu = _find(acct.submenu, "Show in menu-bar title")
    checked = [n for n in title_menu.submenu if n.checked]
    assert len(checked) == 1
    assert checked[0].action == ("account_pct", "a@x.com", "5h")
    assert all(n.radio for n in title_menu.submenu)


def test_menu_model_account_threshold_radio_reflects_override():
    view = _view(accounts=[_acct_row("1", "a@x.com", is_active=True, usage=_USAGE)],
                 active_email="a@x.com", active_usage=_USAGE)
    nodes = tray.build_menu_model(
        view, _settings(), global_threshold=90,
        per_account_thresholds={"a@x.com": 95}, switch_history=[], has_add_token=True,
    )
    acct = nodes[0]
    thr = _find(acct.submenu, "Auto-swap away at")
    checked = [n for n in thr.submenu if n.checked]
    assert len(checked) == 1
    assert checked[0].action == ("account_threshold", "a@x.com", 95)
    # "Default (global)" is the None option
    default_item = _find(thr.submenu, "Default")
    assert default_item.action == ("account_threshold", "a@x.com", None)


def test_menu_model_switch_submenu_and_history():
    view = _view(accounts=[_acct_row("1", "a@x.com", is_active=True, usage=_USAGE)],
                 active_email="a@x.com", active_usage=_USAGE)
    nodes = tray.build_menu_model(
        view, _settings(), global_threshold=90, per_account_thresholds={},
        switch_history=["3 → 1   2026-06-27 02:06"], has_add_token=True,
    )
    switch = _find(nodes, "Switch")
    acts = _actions(switch.submenu)
    assert ("switch", "rotate") in acts
    assert ("switch", "best") in acts
    assert ("switch", "next-available") in acts
    hist = _find(switch.submenu, "Switch history")
    hist_labels = _labels(hist.submenu)
    assert "3 → 1   2026-06-27 02:06" in hist_labels
    assert _find(hist.submenu, "Open full log") is not None
    assert _find(hist.submenu, "Open full log").action == ("open_log",)


def test_menu_model_switch_history_empty_note():
    nodes = tray.build_menu_model(
        _view(), _settings(), global_threshold=90, per_account_thresholds={},
        switch_history=[], has_add_token=True,
    )
    hist = _find(_find(nodes, "Switch").submenu, "Switch history")
    assert "No switches logged yet" in _labels(hist.submenu)


def test_menu_model_manage_menu_add_token_gated():
    view = _view(accounts=[_acct_row("1", "a@x.com", is_active=True, usage=_USAGE)],
                 active_email="a@x.com", active_usage=_USAGE)
    with_token = tray.build_menu_model(
        view, _settings(), global_threshold=90, per_account_thresholds={},
        switch_history=[], has_add_token=True,
    )
    without = tray.build_menu_model(
        view, _settings(), global_threshold=90, per_account_thresholds={},
        switch_history=[], has_add_token=False,
    )
    add_with = _find(_find(with_token, "Manage accounts").submenu, "Add account")
    add_without = _find(_find(without, "Manage accounts").submenu, "Add account")
    assert _find(add_with.submenu, "From setup-token") is not None
    assert _find(add_without.submenu, "From setup-token") is None
    assert _find(add_with.submenu, "From current login").action == ("add", "login")


def test_menu_model_disable_and_remove_rows():
    view = _view(
        accounts=[
            _acct_row("1", "a@x.com", is_active=True, usage=_USAGE),
            _acct_row("2", "b@x.com", usage=_USAGE, disabled=True),
        ],
        active_email="a@x.com", active_usage=_USAGE,
    )
    manage = _find(tray.build_menu_model(
        view, _settings(), global_threshold=90, per_account_thresholds={},
        switch_history=[], has_add_token=True,
    ), "Manage accounts")
    disable = _find(manage.submenu, "Disable / enable account")
    # disabled account row is checked; the toggle action targets that slot
    row2 = [n for n in disable.submenu if n.label.startswith("2")][0]
    assert row2.checked is True
    assert row2.action == ("disable", "2")
    row1 = [n for n in disable.submenu if n.label.startswith("1")][0]
    assert row1.checked is False
    remove = _find(manage.submenu, "Remove account")
    assert any(n.action == ("remove", "2") for n in remove.submenu)


def test_menu_model_settings_toggles_and_radios():
    s = _settings(show_account_name=False, title_pct="5h", title_scoped=True,
                  show_icon=True, title_battery=False, refresh_interval=300,
                  auto_switch_enabled=True)
    settings_menu = _find(tray.build_menu_model(
        _view(), s, global_threshold=95, per_account_thresholds={},
        switch_history=[], has_add_token=True,
    ), "Settings")
    sub = settings_menu.submenu
    name = _find(sub, "Show account name")
    assert name.checked is False and name.action == ("toggle", "name")
    scoped = _find(sub, "Show model limits")
    assert scoped.checked is True and scoped.action == ("toggle", "scoped")
    # global title-percentage radio reflects "5h"
    tp = _find(sub, "Title percentage")
    tp_checked = [n for n in tp.submenu if n.checked]
    assert len(tp_checked) == 1 and tp_checked[0].action == ("title_pct", "5h")
    # refresh interval radio reflects 300
    interval = _find(sub, "Refresh interval")
    iv_checked = [n for n in interval.submenu if n.checked]
    assert len(iv_checked) == 1 and iv_checked[0].action == ("interval", 300)
    # auto-switch toggle + threshold radio reflect state
    auto = _find(sub, "Auto-switch accounts")
    assert auto.checked is True and auto.action == ("toggle", "auto")
    thr = _find(sub, "Auto-switch threshold")
    thr_checked = [n for n in thr.submenu if n.checked]
    assert len(thr_checked) == 1 and thr_checked[0].action == ("set_threshold", 95)


# ============================================================================
# render_icon_image — the pure Pillow bitmap
# ============================================================================

PIL = pytest.importorskip("PIL")
from claude_swap import statusline  # noqa: E402


def _rgb(hex_color):
    return (int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16))


def test_render_icon_returns_rgba_image_of_requested_size():
    img = tray.render_icon_image(42, size=64)
    assert img.__class__.__module__.startswith("PIL")
    assert img.size == (64, 64)
    assert img.mode == "RGBA"


def test_render_icon_background_matches_draining_color_band():
    # utilization 10% => remaining 90% => >70 band
    img = tray.render_icon_image(10, size=32)
    expected = _rgb(statusline.draining_usage_color(90))
    assert img.getpixel((0, 0))[:3] == expected
    # utilization 95% => remaining 5% => <=10 band (distinct color)
    img2 = tray.render_icon_image(95, size=32)
    expected2 = _rgb(statusline.draining_usage_color(5))
    assert img2.getpixel((0, 0))[:3] == expected2
    assert expected != expected2


def test_render_icon_none_is_neutral_and_differs_from_numeric():
    none_img = tray.render_icon_image(None, size=32)
    num_img = tray.render_icon_image(50, size=32)
    assert none_img.size == (32, 32)
    assert none_img.tobytes() != num_img.tobytes()


def test_render_icon_clamps_out_of_range_without_error():
    assert tray.render_icon_image(150, size=16).size == (16, 16)
    assert tray.render_icon_image(-20, size=16).size == (16, 16)


# ============================================================================
# build_tooltip — reuses menubar.format_title
# ============================================================================


def test_build_tooltip_delegates_to_format_title():
    s = _settings()
    view = _view(active_email="a@x.com", active_usage=_USAGE, active_alias="me")
    expected = menubar.format_title(
        "a@x.com", _USAGE, s, alias="me",
        pct_override=menubar.account_title_pct(s, "a@x.com"),
    )
    assert tray.build_tooltip(view, s) == expected


def test_build_tooltip_no_active_account_is_icon():
    assert tray.build_tooltip(_view(), _settings()) == menubar.ICON


# ============================================================================
# reveal_command — file-manager reveal
# ============================================================================


def test_reveal_command_windows_selects_file(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    f = tmp_path / "claude-swap.log"
    f.write_text("x", encoding="utf-8")
    cmd = tray.reveal_command(f)
    assert cmd[0] == "explorer"
    assert str(f) in cmd


def test_reveal_command_linux_opens_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    f = tmp_path / "claude-swap.log"
    f.write_text("x", encoding="utf-8")
    cmd = tray.reveal_command(f)
    assert cmd[0] == "xdg-open"


# ============================================================================
# run() — lazy import contract
# ============================================================================


def test_run_without_pystray_raises_clean_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "pystray", None)
    with pytest.raises(ClaudeSwitchError, match=r"claude-swap\[tray\]"):
        tray.run(switcher=None)


# ============================================================================
# _TrayController.dispatch — action wiring (pure, injected side effects)
# ============================================================================


def test_dispatch_switch_strategies(tmp_path):
    ctrl, sw, rec = _controller(tmp_path)
    ctrl.dispatch(("switch", "rotate"))
    ctrl.dispatch(("switch", "best"))
    ctrl.dispatch(("switch", "next-available"))
    assert ("switch", None) in sw.calls          # rotate => strategy None
    assert ("switch", "best") in sw.calls
    assert ("switch", "next-available") in sw.calls
    assert rec["notify"]  # switch surfaced a notification


def test_dispatch_switch_to(tmp_path):
    ctrl, sw, rec = _controller(tmp_path)
    ctrl.dispatch(("switch_to", "2"))
    assert ("switch_to", "2") in sw.calls


def test_dispatch_switch_error_is_guarded(tmp_path):
    ctrl, sw, rec = _controller(tmp_path, switcher=_RaisingSwitcher(tmp_path))
    ctrl.dispatch(("switch", "best"))  # must not raise
    assert rec["notify"]  # the error was surfaced, not swallowed silently


def test_dispatch_remove_requires_confirm(tmp_path):
    ctrl, sw, rec = _controller(tmp_path, confirm_result=False)
    ctrl.dispatch(("remove", "2"))
    assert rec["confirm"]  # asked
    assert not any(c[0] == "remove_account" for c in sw.calls)  # declined => no removal

    ctrl2, sw2, rec2 = _controller(tmp_path, confirm_result=True)
    ctrl2.dispatch(("remove", "2"))
    assert ("remove_account", "2", True) in sw2.calls


def test_dispatch_disable_toggle(tmp_path):
    ctrl, sw, rec = _controller(tmp_path)
    ctrl.dispatch(("disable", "1"))
    assert any(c[0] == "set_account_disabled" and c[1] == "1" for c in sw.calls)


def test_dispatch_add_login_and_token(tmp_path):
    ctrl, sw, rec = _controller(tmp_path, prompt_results=["e@x.com", "sk-ant-oat01-abc"])
    ctrl.dispatch(("add", "login"))
    assert any(c[0] == "add_account" for c in sw.calls)
    ctrl.dispatch(("add", "token"))
    assert ("add_account_from_token", "sk-ant-oat01-abc", "e@x.com", None) in sw.calls


def test_dispatch_add_token_cancelled(tmp_path):
    ctrl, sw, rec = _controller(tmp_path, prompt_results=[None])  # user cancels email
    ctrl.dispatch(("add", "token"))
    assert not any(c[0] == "add_account_from_token" for c in sw.calls)


def test_dispatch_settings_toggles_persist(tmp_path):
    ctrl, sw, rec = _controller(tmp_path)
    before = ctrl.settings.show_account_name
    ctrl.dispatch(("toggle", "name"))
    assert ctrl.settings.show_account_name is (not before)
    # persisted to disk
    reloaded = menubar.MenuBarSettings.load(sw.backup_dir / "menubar_settings.json")
    assert reloaded.show_account_name is (not before)
    assert rec["menu_changed"] >= 1


def test_dispatch_title_pct_and_account_pct(tmp_path):
    ctrl, sw, rec = _controller(tmp_path)
    ctrl.dispatch(("title_pct", "7d"))
    assert ctrl.settings.title_pct == "7d"
    ctrl.dispatch(("account_pct", "a@x.com", "5h"))
    assert ctrl.settings.account_pct["a@x.com"] == "5h"
    ctrl.dispatch(("account_pct", "a@x.com", "default"))
    assert "a@x.com" not in ctrl.settings.account_pct


def test_dispatch_interval_updates_setting(tmp_path):
    ctrl, sw, rec = _controller(tmp_path)
    ctrl.dispatch(("interval", 30))
    assert ctrl.settings.refresh_interval == 30


def test_dispatch_global_threshold_writes_core_settings(tmp_path):
    ctrl, sw, rec = _controller(tmp_path)
    ctrl.dispatch(("set_threshold", 80))
    from claude_swap.settings import load_settings
    assert int(load_settings(sw.backup_dir).threshold) == 80


def test_dispatch_account_threshold_set_and_unset(tmp_path):
    ctrl, sw, rec = _controller(tmp_path)
    ctrl.dispatch(("account_threshold", "a@x.com", 95))
    from claude_swap.settings import load_per_account_thresholds
    assert load_per_account_thresholds(sw.backup_dir).get("a@x.com") == 95
    ctrl.dispatch(("account_threshold", "a@x.com", None))
    assert "a@x.com" not in load_per_account_thresholds(sw.backup_dir)


def test_dispatch_auto_toggle_starts_and_stops_engine(tmp_path):
    ctrl, sw, rec = _controller(tmp_path)
    assert ctrl.settings.auto_switch_enabled is False
    ctrl.dispatch(("toggle", "auto"))
    assert ctrl.settings.auto_switch_enabled is True
    assert "start" in rec.get("engine", [])
    ctrl.dispatch(("toggle", "auto"))
    assert ctrl.settings.auto_switch_enabled is False
    assert "stop" in rec.get("engine", [])


def test_dispatch_open_log_reveals(tmp_path):
    ctrl, sw, rec = _controller(tmp_path)
    ctrl.dispatch(("open_log",))
    assert rec["reveal"]


def test_dispatch_refresh_now(tmp_path):
    ctrl, sw, rec = _controller(tmp_path)
    ctrl.dispatch(("refresh", "now"))
    assert rec.get("refresh")  # a full refresh was requested


def test_dispatch_quit_stops_engine_and_calls_on_quit(tmp_path):
    ctrl, sw, rec = _controller(tmp_path)
    ctrl.dispatch(("quit",))
    assert rec["quit"] == [True]


def test_active_icon_pct_from_view(tmp_path):
    ctrl, sw, rec = _controller(tmp_path)
    ctrl.view = _view(active_email="a@x.com", active_usage=_USAGE)
    assert round(ctrl.active_icon_pct()) == 42  # binding 5h utilization
    # sentinel note (string) => no numeric icon
    ctrl.view = _view(active_email="a@x.com", active_usage="token expired")
    assert ctrl.active_icon_pct() is None
    # no active account => neutral icon
    ctrl.view = _view()
    assert ctrl.active_icon_pct() is None


# ============================================================================
# CLI dispatch + platform gate (mirrors test_cli.py menubar tests)
# ============================================================================


class _CliFakeSwitcher:
    def __init__(self, *a, **k):
        pass

    def _is_running_in_container(self):
        return False


def test_tray_flag_dispatches_on_windows(monkeypatch):
    from claude_swap import cli

    called = {}
    monkeypatch.setattr(cli, "ClaudeAccountSwitcher", _CliFakeSwitcher)
    monkeypatch.setattr(sys, "argv", ["cswap", "--tray"])
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("claude_swap.tray.run", lambda s: called.update(ran=True) or 0,
                        raising=False)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert called.get("ran") is True


def test_tray_subcommand_dispatches_on_windows(monkeypatch):
    from claude_swap import cli

    called = {}
    monkeypatch.setattr(cli, "ClaudeAccountSwitcher", _CliFakeSwitcher)
    monkeypatch.setattr(sys, "argv", ["cswap", "tray"])
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("claude_swap.tray.run", lambda s: called.update(ran=True) or 0,
                        raising=False)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert called.get("ran") is True


def test_tray_refused_on_macos(monkeypatch):
    from claude_swap import cli

    called = {}
    monkeypatch.setattr(cli, "ClaudeAccountSwitcher", _CliFakeSwitcher)
    monkeypatch.setattr(sys, "argv", ["cswap", "tray"])
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr("claude_swap.tray.run",
                        lambda s: called.update(ran=True) or 0, raising=False)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    assert "ran" not in called  # gate refused before launching
