import importlib.util
import json
import re
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "desktop_follow_addon",
    Path(__file__).parent.parent / "src" / "claude_swap" / "desktop_follow_addon.py",
)
follow_active = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(follow_active)
_REAL_SEQUENCE = follow_active.sequence_email


@pytest.fixture
def creds(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    follow_active._CACHE.update(mtime=None, token=None)

    def write(token="active-tok", expires_at=9_999_999_999_999):
        (tmp_path / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": token, "expiresAt": expires_at}})
        )

    return write


def test_rewrites_inference_bearer(creds):
    creds()
    headers = {"authorization": "Bearer app-tok"}
    assert follow_active.rewrite("api.anthropic.com", "/v1/messages?beta=1", headers)
    assert headers["authorization"] == "Bearer active-tok"


def test_log_line_has_a_timestamp_the_account_and_no_token(creds, tmp_path, capsys):
    creds(token="secret-tok")
    follow_active._EMAIL.update(mtime=None, email=None)
    (tmp_path / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "me@example.com"}}))
    follow_active.rewrite("api.anthropic.com", "/v1/messages", {"authorization": "Bearer app-tok"})
    line = capsys.readouterr().out.strip()
    assert re.match(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d cswap: bearer swapped to me@example\.com \(token [0-9a-f]{8}\)$", line)
    assert "secret-tok" not in line


def test_log_line_names_an_unreadable_account(creds, capsys):
    creds()
    follow_active._EMAIL.update(mtime=None, email=None)
    follow_active.rewrite("api.anthropic.com", "/v1/messages", {"authorization": "Bearer app-tok"})
    assert "unknown account" in capsys.readouterr().out


def _flow(path="/v1/messages", host="api.anthropic.com", request_id="req_1", ua="claude-cli/2.1 (external, claude-desktop)"):
    class Headers(dict):
        def get(self, k, d=None):
            return super().get(k.lower(), d)

    req = type("Req", (), {"pretty_host": host, "path": path, "method": "POST", "timestamp_start": 1_793_600_000.0,
                           "headers": Headers({"user-agent": ua, "authorization": "Bearer active-tok"})})()
    resp = type("Resp", (), {"headers": Headers({"request-id": request_id}), "status_code": 200})()
    return type("Flow", (), {"request": req, "response": resp, "metadata": {}})()


def test_ledger_line_names_account_request_id_and_entrypoint(creds, tmp_path, monkeypatch):
    creds(token="active-tok")
    follow_active._EMAIL.update(mtime=None, email=None)
    (tmp_path / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "me@example.com"}}))
    ledger = tmp_path / "requests.jsonl"
    monkeypatch.setattr(follow_active, "ledger_path", lambda: ledger)
    flow = _flow()
    follow_active.FollowActive().request(flow)
    follow_active.FollowActive().responseheaders(flow)
    line = json.loads(ledger.read_text().splitlines()[0])
    assert line["account"] == "me@example.com" and line["request_id"] == "req_1"
    assert line["status"] == 200 and line["entrypoint"] == "claude-desktop"
    assert line["ts"].endswith("Z")


def test_ledger_has_no_account_when_the_apps_own_login_went_through(creds, tmp_path, monkeypatch):
    creds(token="other-token-than-on-the-request", expires_at=1)  # expired: not substituted
    ledger = tmp_path / "requests.jsonl"
    monkeypatch.setattr(follow_active, "ledger_path", lambda: ledger)
    flow = _flow()
    flow.request.headers["authorization"] = "Bearer app-tok"
    follow_active.FollowActive().request(flow)
    follow_active.FollowActive().responseheaders(flow)
    assert json.loads(ledger.read_text().splitlines()[0])["account"] is None


def test_ledger_skips_token_counting_and_other_hosts(creds, tmp_path, monkeypatch):
    creds()
    ledger = tmp_path / "requests.jsonl"
    monkeypatch.setattr(follow_active, "ledger_path", lambda: ledger)
    for flow in (_flow(path="/v1/messages/count_tokens"), _flow(host="example.com"), _flow(path="/v1/code/sessions/x")):
        follow_active.FollowActive().request(flow)
        follow_active.FollowActive().responseheaders(flow)
    assert not ledger.exists()


def test_responses_are_streamed_not_buffered():
    # A buffered long-lived stream (Remote Control's event stream) never reaches the client.
    flow = _flow(host="example.com")
    flow.response.stream = False
    follow_active.FollowActive().responseheaders(flow)
    assert flow.response.stream is True


def test_leaves_other_routes_and_hosts(creds):
    creds()
    for host, path in [("api.anthropic.com", "/api/oauth/usage"), ("example.com", "/v1/messages")]:
        headers = {"authorization": "Bearer app-tok"}
        assert not follow_active.rewrite(host, path, headers)
        assert headers["authorization"] == "Bearer app-tok"


def test_expired_or_missing_token_keeps_app_login(creds, tmp_path):
    headers = {"authorization": "Bearer app-tok"}
    assert not follow_active.rewrite("api.anthropic.com", "/v1/messages", headers)
    creds(expires_at=1)
    assert not follow_active.rewrite("api.anthropic.com", "/v1/messages", headers)
    assert headers["authorization"] == "Bearer app-tok"


def test_marker_lists_the_desktop_entrypoint_while_running(tmp_path):
    marker = tmp_path / "pin-proxy" / "inference-follows"
    follow_active.publish_marker(marker)
    listed = [line.split("#")[0].strip() for line in marker.read_text().splitlines()]
    assert "claude-desktop" in listed
    follow_active.withdraw_marker(marker)
    assert not marker.exists()
    follow_active.withdraw_marker(marker)  # already gone: no error


def test_picks_up_a_switch(creds):
    creds("first")
    assert follow_active.active_token() == "first"
    creds("second")
    assert follow_active.active_token() == "second"

def test_the_account_comes_from_cswaps_record_not_the_app_config(creds, tmp_path, monkeypatch):
    # The app rewrites ~/.claude.json with its own login; the live token still belongs to the
    # account cswap switched to.
    creds()
    follow_active._EMAIL.update(mtime=None, email=None)
    (tmp_path / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "app@example.com"}}))
    sequence = tmp_path / "sequence.json"
    sequence.write_text(json.dumps({"activeAccountNumber": 2, "accounts": {"1": {"email": "app@example.com"}, "2": {"email": "cswap@example.com"}}}))
    monkeypatch.setattr(follow_active, "sequence_email", lambda path=None: _REAL_SEQUENCE(sequence))
    assert follow_active.active_email() == "cswap@example.com"


def test_a_proxy_that_never_published_the_marker_does_not_remove_it(tmp_path, monkeypatch):
    marker = tmp_path / "inference-follows"
    monkeypatch.setenv("CSWAP_DESKTOP_FOLLOW_MARKER", str(marker))
    running = follow_active.FollowActive()
    running.running()
    assert marker.exists()
    follow_active.FollowActive().done()  # a second proxy that failed to bind the port
    assert marker.exists()
    running.done()
    assert not marker.exists()
