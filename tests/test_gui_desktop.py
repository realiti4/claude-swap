"""Claude Desktop sign-in switching (GUI): profile swaps against a fake profile."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from claude_swap.gui import desktop
from claude_swap.gui.api import GuiApi

UUID_A = "aaaaaaaa-0000-4000-8000-000000000001"
UUID_B = "bbbbbbbb-0000-4000-8000-000000000002"


class _FakeSwitcher:
    def __init__(self, backup_dir: Path, accounts: dict | None = None) -> None:
        self.backup_dir = backup_dir
        self._accounts = accounts or {}

    def _get_sequence_data(self) -> dict:
        return {"accounts": self._accounts}


def _profile(root: Path, key: bytes = b"DPAPIkey-1") -> Path:
    root.mkdir(parents=True)
    (root / "Local State").write_text(
        json.dumps({"os_crypt": {"encrypted_key": base64.b64encode(key).decode()}})
    )
    (root / desktop.CONFIG).write_text(json.dumps({"locale": "zh-CN", "userThemeMode": "dark"}))
    return root


def _sign_in(profile: Path, uuid: str, tag: str) -> None:
    config = desktop.read_config(profile)
    config.update({desktop.ACCOUNT_KEY: uuid, "oauth:tokenCache": f"djEw-{tag}", "oauth:tokenCacheV2": f"djEw-v2-{tag}"})
    (profile / desktop.CONFIG).write_text(json.dumps(config))
    (profile / "Network").mkdir(exist_ok=True)
    (profile / "Network" / "Cookies").write_bytes(f"cookies-{tag}".encode())
    (profile / "Local Storage" / "leveldb").mkdir(parents=True, exist_ok=True)
    (profile / "Local Storage" / "leveldb" / "000001.log").write_text(f"ls-{tag}")


def _session(profile: Path) -> tuple:
    config = desktop.read_config(profile)
    cookies = profile / "Network" / "Cookies"
    ls = profile / "Local Storage" / "leveldb" / "000001.log"
    return (
        config.get(desktop.ACCOUNT_KEY),
        config.get("oauth:tokenCache"),
        cookies.read_bytes().decode() if cookies.exists() else None,
        ls.read_text() if ls.exists() else None,
    )


def test_signed_in_needs_an_account_and_a_token():
    assert desktop.signed_in_uuid({desktop.ACCOUNT_KEY: UUID_A, "oauth:tokenCache": "djEw"}) == UUID_A
    # lastKnownAccountUuid outlives a sign-out; without a token it is not a sign-in.
    assert desktop.signed_in_uuid({desktop.ACCOUNT_KEY: UUID_A}) is None
    assert desktop.signed_in_uuid({desktop.ACCOUNT_KEY: "../x", "oauth:tokenCache": "djEw"}) is None


def test_capture_and_restore_round_trip_only_the_sign_in(tmp_path):
    profile = _profile(tmp_path / "Claude")
    store = desktop.DesktopStore(tmp_path / "store")
    _sign_in(profile, UUID_A, "a")
    assert store.capture(profile) == UUID_A

    desktop.clear(profile)
    assert _session(profile) == (None, None, None, None)
    _sign_in(profile, UUID_B, "b")
    assert store.capture(profile) == UUID_B

    store.restore(profile, UUID_A)
    assert _session(profile) == (UUID_A, "djEw-a", "cookies-a", "ls-a")
    config = desktop.read_config(profile)
    assert config["oauth:tokenCacheV2"] == "djEw-v2-a"
    # Everything that is not the sign-in stays as it was.
    assert config["userThemeMode"] == "dark" and config["locale"] == "zh-CN"
    assert (profile / "Local State").exists()

    store.restore(profile, UUID_B)
    assert _session(profile) == (UUID_B, "djEw-b", "cookies-b", "ls-b")


def test_restore_refuses_a_snapshot_from_another_encryption_key(tmp_path):
    profile = _profile(tmp_path / "Claude")
    store = desktop.DesktopStore(tmp_path / "store")
    _sign_in(profile, UUID_A, "a")
    store.capture(profile)
    # Desktop reset: a new key, and the old ciphertext would be garbage.
    (profile / "Local State").write_text(
        json.dumps({"os_crypt": {"encrypted_key": base64.b64encode(b"DPAPIkey-2").decode()}})
    )
    _sign_in(profile, UUID_B, "b")
    with pytest.raises(desktop.DesktopError):
        store.restore(profile, UUID_A)
    assert _session(profile)[0] == UUID_B


def test_store_rejects_paths_that_are_not_uuids(tmp_path):
    store = desktop.DesktopStore(tmp_path)
    for bad in ("..", "../x", "", UUID_A + "/.."):
        with pytest.raises(desktop.DesktopError):
            store.snapshot_dir(bad)


def test_only_desktop_installs_count_as_desktop():
    assert desktop._is_desktop_exe(r"C:\Program Files\WindowsApps\Claude_1.0.0.0_x64__pzs8sxrjxfjjc\app\claude.exe")
    assert desktop._is_desktop_exe(r"C:\Users\u\AppData\Local\AnthropicClaude\app-0.9.0\claude.exe")
    # Claude Code is claude.exe too, and must never be killed.
    assert not desktop._is_desktop_exe(r"C:\Users\u\.local\bin\claude.exe")
    assert not desktop._is_desktop_exe(r"C:\Users\u\AppData\Roaming\npm\node_modules\@anthropic-ai\claude-code\bin\claude.exe")


def test_store_app_id_from_the_data_path():
    assert desktop._store_app_id(Path(r"C:\U\AppData\Local\Claude-Data\claude_pzs8sxrjxfjjc")) == "Claude_pzs8sxrjxfjjc!Claude"
    assert (
        desktop._store_app_id(Path(r"C:\U\AppData\Local\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude"))
        == "Claude_pzs8sxrjxfjjc!Claude"
    )
    assert desktop._store_app_id(Path(r"C:\U\AppData\Roaming\Claude")) is None


@pytest.fixture
def desk(tmp_path, monkeypatch):
    """A GuiApi over a fake profile, with Desktop's process faked."""
    profile = _profile(tmp_path / "Claude")
    calls = []
    running = {"on": True}

    def quit_desktop():
        calls.append("quit")
        was, running["on"] = running["on"], False
        return was

    def launch_desktop(data_dir):
        calls.append("launch")
        running["on"] = True

    monkeypatch.setattr(desktop, "find_data_dir", lambda: profile)
    monkeypatch.setattr(desktop, "quit_desktop", quit_desktop)
    monkeypatch.setattr(desktop, "launch_desktop", launch_desktop)
    monkeypatch.setattr(desktop, "running_pids", lambda: [1] if running["on"] else [])
    switcher = _FakeSwitcher(tmp_path / "backup", {"1": {"uuid": UUID_A, "email": "a@example.com"}})
    return GuiApi(switcher, mode="webview"), profile, calls


def test_browser_login_then_switch_back_and_forth(desk):
    api, profile, calls = desk
    _sign_in(profile, UUID_A, "a")

    # Sign-in of a new account: A is saved, Desktop restarts signed out.
    assert api.desktop_login_start()["ok"]
    assert calls == ["quit", "launch"]
    assert _session(profile) == (None, None, None, None)
    assert api.desktop_login_poll()["state"] == "waiting"
    _sign_in(profile, UUID_B, "b")  # what Desktop's own sign-in leaves
    ready = api.desktop_login_poll()
    assert ready["state"] == "ready" and ready["uuid"] == UUID_B and not ready["known"]
    assert api.desktop_login_finish("work")["ok"]

    state = api.desktop_state()
    rows = {r["uuid"]: r for r in state["accounts"]}
    assert rows[UUID_A]["email"] == "a@example.com" and rows[UUID_A]["savedAt"]
    assert rows[UUID_B]["name"] == "work" and rows[UUID_B]["isLive"]

    calls.clear()
    result = api.desktop_switch(UUID_A)
    assert result["ok"], result["output"]
    assert calls == ["quit", "launch"]
    assert _session(profile) == (UUID_A, "djEw-a", "cookies-a", "ls-a")

    assert api.desktop_switch(UUID_B)["ok"]
    assert _session(profile) == (UUID_B, "djEw-b", "cookies-b", "ls-b")

    calls.clear()
    assert api.desktop_switch(UUID_B) == {"ok": True, "output": "", "same": True}
    assert calls == []  # already live and running: nothing restarts


def test_switch_refuses_before_closing_desktop(desk):
    api, profile, calls = desk
    _sign_in(profile, UUID_A, "a")
    assert api.desktop_import_current()["ok"]
    accounts = api._desktop_store().load()
    accounts[UUID_B] = {"addedAt": 1}  # known, but never signed in here
    api._desktop_store().save(accounts)
    result = api.desktop_switch(UUID_B)
    assert not result["ok"]
    assert calls == []
    assert _session(profile)[0] == UUID_A


def test_login_cancel_puts_the_previous_account_back(desk):
    api, profile, calls = desk
    _sign_in(profile, UUID_A, "a")
    assert api.desktop_login_start()["ok"]
    assert api.desktop_login_cancel()["ok"]
    assert _session(profile) == (UUID_A, "djEw-a", "cookies-a", "ls-a")
    assert api.desktop_login_poll() == {"state": "gone"}


def test_import_current_requires_a_sign_in(desk):
    api, profile, calls = desk
    assert not api.desktop_import_current()["ok"]
    _sign_in(profile, UUID_B, "b")
    result = api.desktop_import_current("home")
    assert result["ok"] and result["uuid"] == UUID_B and result["email"] is None
    assert api.desktop_state()["accounts"][0]["name"] == "home"


def test_remove_keeps_the_live_sign_in(desk):
    api, profile, calls = desk
    _sign_in(profile, UUID_A, "a")
    api.desktop_import_current()
    assert api.desktop_remove(UUID_A)["ok"]
    assert api.desktop_state()["accounts"] == []
    assert _session(profile)[0] == UUID_A


def test_ui_prefs_persist_and_validate(tmp_path):
    api = GuiApi(_FakeSwitcher(tmp_path), mode="webview")
    assert api.ui_prefs() == {"simple": True, "view": "cli", "theme": "auto", "tray": True}
    assert api.set_ui_pref("view", "desktop")["ok"]
    assert api.set_ui_pref("simple", False)["ok"]
    assert not api.set_ui_pref("view", "nope")["ok"]
    assert not api.set_ui_pref("simple", "yes")["ok"]
    assert not api.set_ui_pref("other", 1)["ok"]
    again = GuiApi(_FakeSwitcher(tmp_path), mode="webview")
    assert again.ui_prefs() == {"simple": False, "view": "desktop", "theme": "auto", "tray": True}
    assert again.boot()["prefs"]["view"] == "desktop"


def test_tray_menu_names_the_current_accounts_and_switches(desk):
    from claude_swap.gui import tray as tray_mod

    api, profile, calls = desk
    api._switcher._accounts = {
        "1": {"uuid": UUID_A, "email": "a@example.com"},
        "2": {"uuid": "cccccccc-0000-4000-8000-000000000003", "email": "c@example.com", "alias": "work"},
    }
    api._switcher._get_sequence_data = lambda: {"accounts": api._switcher._accounts, "activeAccountNumber": 2}
    _sign_in(profile, UUID_B, "b")
    desktop.quit_desktop()  # imported while closed, so the sign-in is saved right away
    api.desktop_import_current("home")
    _sign_in(profile, UUID_A, "a")  # switched inside Desktop: A is live, B was saved
    api.desktop_import_current()

    switched = []
    api.switch_to = lambda number, force=False: switched.append(number) or {"ok": True, "output": ""}
    t = tray_mod.Tray(api, show=lambda: None, quit=lambda: None)
    notes = []
    t.notify = lambda title, text: notes.append(text)
    t.confirm = lambda title, text: True

    items = t.model()
    labels = [i.label for i in items]
    assert labels == ["Desktop：a@example.com", "CLI：2. c@example.com (@work)", "-", "打开主窗口", "退出"]
    desk_rows = {i.label: i for i in items[0].submenu}
    assert desk_rows["a@example.com"].checked and not desk_rows["a@example.com"].enabled
    assert desk_rows["home"].enabled
    cli_rows = items[1].submenu
    assert cli_rows[0].label == "1. a@example.com" and cli_rows[0].enabled
    assert cli_rows[1].checked and not cli_rows[1].enabled

    cli_rows[0].action()
    assert switched == ["1"] and notes[-1] == "CLI 已切换到 1. a@example.com"
    desk_rows["home"].action()
    assert _session(profile)[0] == UUID_B
    assert notes[-1] == "Desktop 已切换到 home"


def test_second_launch_wakes_the_first(tmp_path):
    import threading

    from claude_swap.gui import single

    shown = threading.Event()
    assert single.claim(tmp_path, shown.set)
    assert not single.claim(tmp_path, lambda: None)
    assert shown.wait(5)


def test_a_stale_instance_file_does_not_block_launch(tmp_path):
    from claude_swap.gui import single

    (tmp_path / "gui-instance.json").write_text('{"port": 1, "token": "x"}')
    assert single.claim(tmp_path, lambda: None)


def _usage_log(profile: Path, samples: list[tuple[float, str, dict]]) -> None:
    (profile / desktop.USAGE_FILE).write_text(json.dumps(
        {"version": 2, "samples": [{"t": int(t * 1000), "org": org, "u": u} for t, org, u in samples]}
    ))


def test_desktop_usage_prefers_the_cli_reading_then_desktops_log(desk, monkeypatch):
    import time as _time

    api, profile, calls = desk
    api._switcher._accounts = {"1": {"uuid": UUID_A, "email": "a@example.com", "organizationUuid": "org-a"}}
    _sign_in(profile, UUID_A, "a")
    desktop.quit_desktop()
    api.desktop_import_current()
    # B goes live through a browser login; only samples after that are B's.
    assert api.desktop_login_start()["ok"]
    _sign_in(profile, UUID_B, "b")
    started = api._desktop_login["started"]
    assert api.desktop_login_finish("home")["ok"]
    now = _time.time()
    _usage_log(profile, [
        (started - 600, "org-a", {"fh": 10, "sd": 20}),   # A's, written before B went live
        (now, "org-b", {"fh": 30, "sd": 40}),
    ])

    rows = {r["uuid"]: r for r in api.desktop_state()["accounts"]}
    assert rows[UUID_B]["usage"]["source"] == "desktop"
    assert rows[UUID_B]["usage"]["usage"] == {"fiveHour": {"pct": 30.0}, "sevenDay": {"pct": 40.0}}
    assert api._desktop_store().load()[UUID_B]["orgUuid"] == "org-b"  # learned, kept
    # A has no CLI reading yet: Desktop's log, found through the CLI's org.
    assert rows[UUID_A]["usage"]["usage"]["sevenDay"] == {"pct": 20.0}

    cli = {"usage": {"fiveHour": {"pct": 11.0, "resetsAt": "x"}, "sevenDay": {"pct": 96.0}}, "ageS": 5}
    monkeypatch.setattr(api, "_cli_usage", lambda: {"1": cli})
    rows = {r["uuid"]: r for r in api.desktop_state()["accounts"]}
    assert rows[UUID_A]["usage"] == {"usage": cli["usage"], "source": "cli", "cliNumber": "1", "ageS": 5}

    labels = {a["uuid"]: a["label"] for a in api._tray_desktop_accounts()}
    assert labels[UUID_A] == "a@example.com  ·  5h 11% / 7天 96%"
    assert labels[UUID_B] == "home  ·  5h 30% / 7天 40%"


def test_a_sample_from_before_going_live_is_not_claimed(desk):
    import time as _time

    api, profile, calls = desk
    _sign_in(profile, UUID_B, "b")
    api.desktop_import_current()  # B not in the CLI: its org is unknown
    _usage_log(profile, [(_time.time() - 60, "org-prev", {"fh": 50, "sd": 50})])
    row = api.desktop_state()["accounts"][0]
    assert row["usage"] is None
    assert "orgUuid" not in api._desktop_store().load()[UUID_B]


def test_latest_usage_skips_empty_and_malformed_samples(tmp_path):
    profile = _profile(tmp_path / "Claude")
    (profile / desktop.USAGE_FILE).write_text(json.dumps({"samples": [
        {"t": 1, "org": "o", "u": {"fh": 1, "sd": 2}},
        {"t": 3, "org": "o", "u": {}},
        {"t": "x", "org": "o", "u": {"fh": 9}},
        "junk",
        {"t": 2, "org": "o", "u": {"fh": 5, "sd": 6}},
    ]}))
    assert desktop.latest_usage(profile)["o"]["u"] == {"fh": 5, "sd": 6}
    (profile / desktop.USAGE_FILE).write_text("not json")
    assert desktop.latest_usage(profile) == {}
