"""Tests for the desktop GUI's Python side (no webview needed)."""

from __future__ import annotations

import http.client
import json
import sys
import threading
import time
from pathlib import Path

import pytest

from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.gui import api as gui_api
from claude_swap.gui import launcher
from claude_swap.gui.api import GuiApi, strip_ansi
from claude_swap.models import AccountPolicy, AccountSnapshot
from claude_swap.usage_store import UsageEntry


class _FakeSwitcher:
    def __init__(self, backup_dir: Path) -> None:
        self.backup_dir = backup_dir


@pytest.fixture
def api(tmp_path: Path) -> GuiApi:
    return GuiApi(_FakeSwitcher(tmp_path), mode="webview")


def test_strip_ansi_removes_color_codes():
    assert strip_ansi("\x1b[1;32mSwitched\x1b[0m to 2") == "Switched to 2"


def test_run_captures_output_and_payload(api: GuiApi):
    def action():
        print("\x1b[32mdone\x1b[0m")
        return {"switched": True, "to": {"number": 2}}

    result = api._run(action)
    assert result == {"ok": True, "output": "done", "payload": {"switched": True, "to": {"number": 2}}}


def test_run_reports_core_errors_without_raising(api: GuiApi):
    def action():
        raise ClaudeSwitchError("Account-9 does not exist")

    result = api._run(action)
    assert result["ok"] is False
    assert "Account-9 does not exist" in result["output"]


def test_run_fails_a_stray_prompt_instead_of_hanging(api: GuiApi):
    result = api._run(lambda: input("Overwrite? "))
    assert result["ok"] is False


def test_run_feeds_scripted_stdin(api: GuiApi):
    result = api._run(lambda: print(input()), stdin="y\n")
    assert result == {"ok": True, "output": "y"}


def test_run_is_single_flight(api: GuiApi):
    started, release = threading.Event(), threading.Event()

    def slow():
        started.set()
        release.wait(5)

    worker = threading.Thread(target=api._run, args=(slow,))
    worker.start()
    started.wait(5)
    try:
        second = api._run(lambda: None)
    finally:
        release.set()
        worker.join(5)
    assert second["ok"] is False


def test_account_dict_shape():
    usage = UsageEntry(
        last_good={"five_hour": {"pct": 42.0}, "seven_day": {"pct": 10.0}},
        fetched_at=time.time(),
        age_s=12.0,
    )
    acc = AccountSnapshot(
        number="2", email="a@example.com", org_name="", org_uuid="", is_active=True,
        kind="oauth", switchable=True, usage=usage, alias="dev",
        policy=AccountPolicy(threshold=85.0, priority=3),
    )
    row = GuiApi._account_dict(acc)
    assert row["number"] == "2"
    assert row["org"] == "personal"
    assert row["threshold"] == 85.0 and row["priority"] == 3
    assert row["usage"]["fiveHour"]["pct"] == 42.0
    assert row["sentinel"] is None
    json.dumps(row)


def test_events_are_sequenced(api: GuiApi):
    api._push_event({"event": "gui", "text": "one"})
    api._push_event({"event": "gui", "text": "two"})
    first = api.events(0)
    assert [e["text"] for e in first["events"]] == ["one", "two"]
    assert api.events(first["events"][0]["seq"])["events"][0]["text"] == "two"


def test_open_url_only_allows_https(api: GuiApi, monkeypatch):
    opened = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    assert api.open_url("file:///etc/passwd")["ok"] is False
    assert api.open_url("https://github.com")["ok"] is True
    assert opened == ["https://github.com"]


def test_split_args():
    assert launcher.split_args("") == []
    assert launcher.split_args("--resume  --verbose") == ["--resume", "--verbose"]


def test_cli_command_from_source():
    assert launcher.cli_command() == [sys.executable, "-m", "claude_swap"]


def test_server_requires_token_and_loopback_host(api: GuiApi, monkeypatch):
    from claude_swap.gui import server

    ready = threading.Event()
    state: dict = {}

    real_server = server.ThreadingHTTPServer

    class CapturingServer(real_server):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            state["port"] = self.server_address[1]

    def fake_open(url):
        state["token"] = url.split("t=", 1)[1]
        ready.set()

    monkeypatch.setattr(server, "ThreadingHTTPServer", CapturingServer)
    monkeypatch.setattr(server.webbrowser, "open", fake_open)
    monkeypatch.setattr(server, "IDLE_EXIT_S", 3.0)
    thread = threading.Thread(target=server.serve, args=(api, "<p>__CSWAP_TOKEN__</p>"), daemon=True)
    thread.start()
    assert ready.wait(5)
    port, token = state["port"], state["token"]

    def post(name, headers):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", f"/api/{name}", body="[]", headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()

    host = f"127.0.0.1:{port}"
    assert post("ping", {"Host": host})[0] == 403
    assert post("ping", {"Host": host, "X-Token": "wrong"})[0] == 403
    assert post("ping", {"Host": f"evil.example:{port}", "X-Token": token})[0] == 403
    assert post("_run", {"Host": host, "X-Token": token})[0] == 404
    status, body = post("ping", {"Host": host, "X-Token": token})
    assert status == 200 and json.loads(body) == {"result": True}

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", f"/?t={token}", headers={"Host": host})
    page = conn.getresponse().read().decode()
    assert token in page and "__CSWAP_TOKEN__" not in page
    thread.join(10)


def test_releases_constants_point_at_this_fork():
    assert "xutianhao6/claude-swap-desktop" in gui_api.RELEASES_API
