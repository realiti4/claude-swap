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


# Runs the real `cswap add` in the child with the suite's network stubs, which
# a subprocess would not otherwise inherit.
_HERMETIC_CLI = (
    "import sys\n"
    "import claude_swap.oauth as o, claude_swap.subscription as s\n"
    "o.fetch_oauth_profile = lambda token: None\n"
    "s.fetch_plan = lambda *a, **k: s.PlanFetch()\n"
    "from claude_swap.cli import main\n"
    "sys.argv = ['cswap', *sys.argv[1:]]\n"
    "main()\n"
)


def _write_login(config_dir: Path, email: str, token: str) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": email, "accountUuid": f"uuid-{token}"}})
    )
    creds = {
        "claudeAiOauth": {
            "accessToken": f"sk-ant-oat01-{token}",
            "refreshToken": f"sk-ant-ort01-{token}",
            "expiresAt": int(time.time() * 1000) + 3_600_000,
            "scopes": ["user:inference", "user:profile"],
        }
    }
    (config_dir / ".credentials.json").write_text(json.dumps(creds))


# Stands in for `claude auth login`: prints the manual link the way the real
# one does (inside an OSC 8 hyperlink), then signs in with the code it reads.
_FAKE_CLAUDE = r'''
import json, os, sys, time
url = "https://claude.com/cai/oauth/authorize?code=true&state=s1"
print("Opening browser to sign in...")
print(f"If the browser didn't open, visit: \x1b]8;;{url}\x07{url}\x1b]8;;\x07", flush=True)
code = sys.stdin.readline().strip()
if code != "good-code":
    print("OAuth error: invalid code", flush=True)
    sys.exit(1)
d = os.environ["CLAUDE_CONFIG_DIR"]
with open(os.path.join(d, ".claude.json"), "w") as f:
    json.dump({"oauthAccount": {"emailAddress": "b@example.com", "accountUuid": "uuid-b"}}, f)
with open(os.path.join(d, ".credentials.json"), "w") as f:
    json.dump({"claudeAiOauth": {"accessToken": "sk-ant-oat01-b", "refreshToken": "sk-ant-ort01-b",
        "expiresAt": int(time.time() * 1000) + 3600000, "scopes": ["user:inference", "user:profile"]}}, f)
print("Login successful.", flush=True)
'''


@pytest.fixture
def fake_claude(tmp_path, monkeypatch) -> None:
    script = tmp_path / "fake_claude.py"
    script.write_text(_FAKE_CLAUDE)
    if sys.platform == "win32":
        exe = tmp_path / "claude.cmd"
        exe.write_text(f'@"{sys.executable}" "{script}" %*\r\n')
    else:
        exe = tmp_path / "claude"
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
        exe.chmod(0o755)
    monkeypatch.setattr(launcher, "find_claude", lambda: str(exe))


def _poll_until(api: GuiApi, login_id: str, *, state: str, timeout: float = 20) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = api.login_poll(login_id)
        if result["state"] == state and (state != "waiting" or result.get("url")):
            return result
        time.sleep(0.1)
    raise AssertionError(f"login never reached {state!r}: {result}")


@pytest.mark.skipif(sys.platform == "darwin", reason="slot credentials would go to the real Keychain")
def test_browser_login_captures_staging_profile_without_touching_live_login(
    temp_home, monkeypatch, fake_claude
):
    from claude_swap.switcher import ClaudeAccountSwitcher

    # Live login: account A, managed as slot 1.
    (temp_home / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": "a@example.com", "accountUuid": "uuid-a"}})
    )
    _write_login(temp_home / ".claude", "unused@example.com", "a")
    (temp_home / ".claude" / ".claude.json").unlink()
    switcher = ClaudeAccountSwitcher()
    switcher.add_account()
    assert switcher.current_account_number() == "1"

    api = GuiApi(switcher, mode="webview")
    monkeypatch.setattr(launcher, "cli_capture_command", lambda: [sys.executable, "-c", _HERMETIC_CLI])

    started = api.login_start()
    assert started["ok"], started["output"]
    login_id = started["id"]
    staging = api._staging_dir(login_id)
    waiting = _poll_until(api, login_id, state="waiting")
    assert waiting["url"] == "https://claude.com/cai/oauth/authorize?code=true&state=s1"

    assert not api.login_code(login_id, "two words")["ok"]
    assert api.login_code(login_id, "good-code")["ok"]
    assert _poll_until(api, login_id, state="ready") == {"state": "ready", "email": "b@example.com"}

    result = api.login_finish(login_id, alias="work")
    assert result["ok"], result["output"]

    seq = json.loads(switcher.sequence_file.read_text())
    emails = {num: acc["email"] for num, acc in seq["accounts"].items()}
    assert emails == {"1": "a@example.com", "2": "b@example.com"}
    assert seq["accounts"]["2"].get("alias") == "work"
    # The live login never moved, and the record says so.
    assert seq["activeAccountNumber"] == 1
    assert switcher.current_account_number() == "1"
    assert "sk-ant-oat01-b" in switcher.read_account_credentials("2", "b@example.com")
    assert not staging.exists()
    assert api.login_poll(login_id) == {"state": "gone"}


def test_browser_login_reports_a_rejected_code(api: GuiApi, fake_claude):
    login_id = api.login_start()["id"]
    _poll_until(api, login_id, state="waiting")
    assert api.login_code(login_id, "wrong")["ok"]
    failed = _poll_until(api, login_id, state="failed")
    assert "invalid code" in failed["output"]
    assert not api._staging_dir(login_id).exists()


def test_browser_login_cancel_stops_the_login(api: GuiApi, fake_claude):
    login_id = api.login_start()["id"]
    _poll_until(api, login_id, state="waiting")
    proc = api._logins[login_id].proc
    api.login_cancel(login_id)
    assert proc.poll() is not None
    assert not api._staging_dir(login_id).exists()
    assert api.login_poll(login_id) == {"state": "gone"}
    assert not api.login_code(login_id, "good-code")["ok"]


def test_browser_login_without_claude(api: GuiApi, monkeypatch):
    monkeypatch.setattr(launcher, "find_claude", lambda: None)
    assert not api.login_start()["ok"]


def test_login_ids_cannot_escape_the_staging_root(api: GuiApi):
    for bad in ("../x", "..\\x", "1-abc", "", "1700000000-abcdef12/../../x"):
        with pytest.raises(ValueError):
            api._staging_dir(bad)


def test_releases_constants_point_at_this_fork():
    assert "xutianhao6/claude-swap-desktop" in gui_api.RELEASES_API
