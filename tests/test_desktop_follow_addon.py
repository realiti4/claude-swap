import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "follow_active",
    Path(__file__).parent.parent / "contrib" / "desktop-follow" / "follow_active.py",
)
follow_active = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(follow_active)


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
