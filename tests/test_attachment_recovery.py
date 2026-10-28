"""Tests for the Remote Control attachment-recovery hook."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from claude_swap import attachment_recovery as ar
from claude_swap.switcher import ClaudeAccountSwitcher

FUTURE = int((time.time() + 3600) * 1000)


def creds(token: str, expires: int = FUTURE) -> str:
    return json.dumps(
        {"claudeAiOauth": {"accessToken": token, "refreshToken": f"rt-{token}", "expiresAt": expires}}
    )


@pytest.fixture
def switcher(temp_home: Path) -> ClaudeAccountSwitcher:
    s = ClaudeAccountSwitcher(debug=True)
    s._setup_directories()
    s._write_json(
        s.sequence_file,
        {
            "activeAccountNumber": 1,
            "sequence": [1, 2],
            "accounts": {
                "1": {"email": "live@example.com", "organizationUuid": "org", "uuid": "u-1"},
                "2": {"email": "owner@example.com", "organizationUuid": "org", "uuid": "u-2"},
            },
        },
    )
    s._write_account_credentials("1", "live@example.com", creds("stale-live-backup"))
    s._write_account_credentials("2", "owner@example.com", creds("tok-owner"))
    # Account 1 is the live default login.
    (temp_home / ".claude" / ".credentials.json").write_text(creds("tok-live"))
    (temp_home / ".claude.json").write_text(
        json.dumps(
            {"oauthAccount": {"emailAddress": "live@example.com", "organizationUuid": "org"}}
        )
    )
    sessions = temp_home / ".claude" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "123.json").write_text(
        json.dumps({"pid": 123, "sessionId": "local-1", "bridgeSessionId": "session_ABC"})
    )
    return s


EVENTS = {
    "data": [
        {"payload": {"type": "assistant", "message": {"content": "hi"}}},
        {
            "payload": {
                "type": "user",
                "message": {"content": "describe this"},
                "file_attachments": [
                    {"file_uuid": "aaaa1111-0000-4000-8000-000000000000", "file_name": "photo.jpg", "is_image": True}
                ],
            }
        },
        {
            "payload": {
                "type": "user",
                "message": {"content": "older"},
                "file_attachments": [{"file_uuid": "bbbb", "file_name": "old.pdf"}],
            }
        },
    ]
}


class FakeApi:
    """Files and sessions belong to the owner account only (per-user)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, url, headers, limit):
        bearer = headers["Authorization"].removeprefix("Bearer ")
        self.calls.append((url, bearer))
        if bearer != "tok-owner":
            return 404, b""
        if "/v1/code/sessions/cse_ABC/events" in url:
            return 200, json.dumps(EVENTS).encode()
        if "/api/oauth/files/aaaa1111" in url:
            return 200, b"JPEGDATA"
        return 404, b""


def hook(prompt: str, session_id: str = "local-1") -> dict:
    return {"hook_event_name": "UserPromptSubmit", "session_id": session_id, "prompt": prompt}


class TestPlaceholder:
    @pytest.mark.parametrize(
        "prompt",
        [
            "[attachment did not arrive] hi",
            "[3 attachments did not arrive] hi",
            "[1 of 2 attachments did not arrive] hi",
        ],
    )
    def test_detects(self, prompt):
        assert ar.strip_placeholder(prompt) == (True, "hi")

    def test_ignores_normal_prompt(self):
        assert ar.strip_placeholder("hello [attachment did not arrive]")[0] is False


class TestRecover:
    def test_recovers_with_owner_token(self, switcher, temp_home):
        api = FakeApi()
        out = ar.recover(hook("[attachment did not arrive] describe this"), switcher, fetch=api)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        saved = temp_home / ".claude" / "uploads" / "local-1" / "aaaa1111-photo.jpg"
        assert saved.read_bytes() == b"JPEGDATA"
        assert str(saved) in ctx
        # The live account is tried last: the owner (non-live) answers first.
        assert api.calls[0][1] == "tok-owner"

    def test_owner_uuid_hint_goes_first(self, switcher):
        tokens = ar.candidate_tokens(switcher, owner_uuid="u-1")
        assert [t.number for t in tokens] == ["1", "2"]
        assert tokens[0].access_token == "tok-live"  # live reads the live file

    def test_matches_prompt_text_not_just_newest(self, switcher, temp_home):
        api = FakeApi()
        ar.recover(hook("[attachment did not arrive] describe this"), switcher, fetch=api)
        assert not any("bbbb" in url for url, _ in api.calls)

    def test_no_placeholder_no_calls(self, switcher):
        api = FakeApi()
        assert ar.recover(hook("describe this"), switcher, fetch=api) is None
        assert api.calls == []

    def test_non_bridge_session_is_ignored(self, switcher):
        api = FakeApi()
        out = ar.recover(
            hook("[attachment did not arrive] x", session_id="other"), switcher, fetch=api
        )
        assert out is None and api.calls == []

    def test_nobody_owns_the_session(self, switcher, monkeypatch):
        out = ar.recover(
            hook("[attachment did not arrive] describe this"),
            switcher,
            fetch=lambda url, headers, limit: (404, b""),
        )
        assert out is None

    def test_expired_inactive_token_is_refreshed_and_persisted(self, switcher, monkeypatch):
        switcher._write_account_credentials("2", "owner@example.com", creds("old", expires=1))
        refreshed = creds("tok-owner")
        monkeypatch.setattr(
            ar.oauth,
            "try_refresh_oauth_credentials",
            lambda c, **kw: ar.oauth.RefreshOutcome(refreshed, None),
        )
        tokens = {t.number: t.access_token for t in ar.candidate_tokens(switcher, None)}
        assert tokens["2"] == "tok-owner"
        assert switcher.read_account_credentials("2", "owner@example.com") == refreshed

    def test_expired_live_token_is_never_refreshed(self, switcher, temp_home, monkeypatch):
        (temp_home / ".claude" / ".credentials.json").write_text(creds("tok-live", expires=1))

        def boom(*a, **k):
            raise AssertionError("must not refresh the live token")

        monkeypatch.setattr(ar.oauth, "try_refresh_oauth_credentials", boom)
        tokens = [t.number for t in ar.candidate_tokens(switcher, None)]
        assert tokens == ["2"]

    def test_existing_file_is_reused(self, switcher, temp_home):
        dest = temp_home / ".claude" / "uploads" / "local-1"
        dest.mkdir(parents=True)
        (dest / "aaaa1111-photo.jpg").write_bytes(b"already")
        api = FakeApi()
        ar.recover(hook("[attachment did not arrive] describe this"), switcher, fetch=api)
        assert not any("/api/oauth/files/" in url for url, _ in api.calls)


class TestRunHook:
    def test_never_raises(self, switcher):
        def broken():
            raise RuntimeError("boom")

        assert ar.run_hook(json.dumps(hook("[attachment did not arrive] x")), broken) == ""
        assert ar.run_hook("not json", broken) == ""

    def test_fast_path_skips_switcher(self):
        def must_not_build():
            raise AssertionError("built a switcher for a normal prompt")

        assert ar.run_hook(json.dumps(hook("hello")), must_not_build) == ""


CMD = '"/opt/bin/cswap" hook attachments'

USER_SETTINGS = {
    "permissions": {"defaultMode": "bypassPermissions", "allow": ["Bash(ls:*)"], "deny": []},
    "remoteControlAtStartup": True,
    "hooks": {
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "other-tool"}]}],
        "Stop": [{"hooks": [{"type": "command", "command": "notify"}]}],
    },
}


class TestInstall:
    def write(self, tmp_path, data) -> Path:
        p = tmp_path / "settings.json"
        p.write_text(json.dumps(data))
        return p

    def test_install_preserves_everything(self, tmp_path):
        p = self.write(tmp_path, USER_SETTINGS)
        result, backup = ar.install_hook(p, CMD)
        assert result == "installed"
        assert json.loads(backup.read_text()) == USER_SETTINGS
        data = json.loads(p.read_text())
        assert data["permissions"] == USER_SETTINGS["permissions"]
        assert data["remoteControlAtStartup"] is True
        assert data["hooks"]["Stop"] == USER_SETTINGS["hooks"]["Stop"]
        ups = data["hooks"]["UserPromptSubmit"]
        assert ups[0] == USER_SETTINGS["hooks"]["UserPromptSubmit"][0]
        assert ups[1]["hooks"][0]["command"] == CMD
        assert ar.hook_installed(p)

    def test_install_is_idempotent(self, tmp_path):
        p = self.write(tmp_path, USER_SETTINGS)
        ar.install_hook(p, CMD)
        before = p.read_text()
        assert ar.install_hook(p, CMD) == ("unchanged", None)
        assert p.read_text() == before

    def test_install_updates_a_moved_executable(self, tmp_path):
        p = self.write(tmp_path, USER_SETTINGS)
        ar.install_hook(p, CMD)
        result, _ = ar.install_hook(p, '"/new/cswap" hook attachments')
        assert result == "updated"
        cmds = [
            h["command"]
            for g in json.loads(p.read_text())["hooks"]["UserPromptSubmit"]
            for h in g["hooks"]
        ]
        assert cmds == ["other-tool", '"/new/cswap" hook attachments']

    def test_install_creates_missing_file(self, tmp_path):
        p = tmp_path / "settings.json"
        assert ar.install_hook(p, CMD)[0] == "installed"
        assert ar.hook_installed(p)

    def test_install_refuses_broken_json(self, tmp_path):
        p = tmp_path / "settings.json"
        p.write_text("{not json")
        with pytest.raises(ar.SettingsError):
            ar.install_hook(p, CMD)
        assert p.read_text() == "{not json"

    def test_uninstall_restores_shape(self, tmp_path):
        p = self.write(tmp_path, USER_SETTINGS)
        ar.install_hook(p, CMD)
        removed, backup = ar.uninstall_hook(p)
        assert removed and backup is not None
        assert json.loads(p.read_text()) == USER_SETTINGS
        assert ar.uninstall_hook(p) == (False, None)

    def test_uninstall_drops_empty_hooks_key(self, tmp_path):
        p = self.write(tmp_path, {"model": "x"})
        ar.install_hook(p, CMD)
        ar.uninstall_hook(p)
        assert json.loads(p.read_text()) == {"model": "x"}

    def test_other_hooks_are_never_ours(self):
        assert not ar._is_ours({"type": "command", "command": "other-tool"})
        assert ar._is_ours({"type": "command", "command": CMD})
        assert ar._is_ours(
            {"type": "command", "command": '"C:/x/python.exe" -m claude_swap hook attachments'}
        )
