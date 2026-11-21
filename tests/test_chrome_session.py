"""Tests for the optional Chrome extension sync (platform-independent logic).

Covers the token value object, the per-account vault, the settings.json
``chrome`` section, the disabled no-op contract, the add-time org-match guard,
and the keep-alive staleness check. The browser/CDP paths (macOS + a live
Chrome + the OAuth endpoint) are exercised manually, not here.
"""

from __future__ import annotations

import json
import stat
import sys
import time
from pathlib import Path

import pytest

from claude_swap import chrome_session as cs
from claude_swap.settings import load_chrome_settings, set_chrome_enabled


@pytest.fixture(autouse=True)
def _no_live_browser_or_network(monkeypatch):
    """Never reach the real automation Chrome (it may be running on the default
    port) or the OAuth token endpoint; tests opt in to fakes explicitly."""
    monkeypatch.setattr(cs, "automation_chrome_running", lambda *a, **k: False)
    monkeypatch.setattr(cs, "_eval_in_sw", lambda *a, **k: None)
    monkeypatch.setattr(cs, "refresh_session_detailed", lambda *a, **k: (None, cs.REFRESH_FAILED))


def _session(**kw) -> cs.ChromeSession:
    base = {"access_token": "at", "refresh_token": "rt"}
    base.update(kw)
    return cs.ChromeSession(**base)


def test_chrome_session_roundtrip():
    s = _session(token_expiry=123, account_uuid="acc", org_uuid="org", email="e@x", captured_at=9)
    assert s.is_usable()
    d = s.to_dict()
    assert d == {
        "accessToken": "at", "refreshToken": "rt", "tokenExpiry": 123,
        "accountUuid": "acc", "orgUuid": "org", "email": "e@x", "capturedAt": 9,
    }
    assert cs.ChromeSession.from_dict(d) == s
    # A session needs BOTH tokens to be durable (refresh token is what survives).
    assert not cs.ChromeSession(access_token="at").is_usable()
    assert not cs.ChromeSession(access_token="", refresh_token="rt").is_usable()


def test_vault_roundtrip(tmp_path):
    vault = cs.ChromeVault(tmp_path)
    assert vault.get("1") is None
    s = _session(account_uuid="u1")
    vault.put("1", s)
    assert vault.get("1") == s
    if sys.platform != "win32":
        mode = stat.S_IMODE((tmp_path / "chrome_sessions.json").stat().st_mode)
        assert mode == 0o600
    vault.delete("1")
    assert vault.get("1") is None


def test_vault_ignores_unusable_stored_session(tmp_path):
    (tmp_path / "chrome_sessions.json").write_text(json.dumps({"1": {"accessToken": "at"}}))
    assert cs.ChromeVault(tmp_path).get("1") is None


def test_vault_account_numbers(tmp_path):
    vault = cs.ChromeVault(tmp_path)
    assert vault.account_numbers() == set()
    vault.put("1", _session())
    vault.put("3", _session())
    # a stored-but-unusable entry (no refresh token) is not counted
    raw = json.loads((tmp_path / "chrome_sessions.json").read_text())
    raw["9"] = {"accessToken": "at"}
    (tmp_path / "chrome_sessions.json").write_text(json.dumps(raw))
    assert vault.account_numbers() == {"1", "3"}


def test_vault_find_by_uuid(tmp_path):
    vault = cs.ChromeVault(tmp_path)
    vault.put("1", _session(account_uuid="uuid-a"))
    vault.put("2", _session(account_uuid="uuid-b"))
    assert vault.find_by_uuid("uuid-b") == "2"
    assert vault.find_by_uuid("nope") is None
    assert vault.find_by_uuid("") is None


def test_is_stale():
    now = time.time() * 1000
    fresh = _session(captured_at=int(now))
    old = _session(captured_at=int(now - cs.KEEPALIVE_REFRESH_DAYS * 86400 * 1000 - 1000))
    assert not cs._is_stale(fresh)
    assert cs._is_stale(old)
    # unknown capture time never forces a refresh
    assert not cs._is_stale(_session(captured_at=0))


def test_resolve_automation_dir(tmp_path):
    assert cs.resolve_automation_dir(cs.ChromeSyncSettings(), tmp_path) == tmp_path / "chrome-automation"
    custom = cs.ChromeSyncSettings(automation_dir="/tmp/foo")
    assert cs.resolve_automation_dir(custom, tmp_path) == Path("/tmp/foo")


def test_load_chrome_settings_defaults(tmp_path):
    s = load_chrome_settings(tmp_path)
    assert s.enabled is False
    assert s.debug_port == 9333
    assert s.source_profile == "Default"
    assert s.auto_launch is True


def test_load_chrome_settings_values(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({"chrome": {
        "enabled": True, "debugPort": 9401, "sourceProfile": "Profile 1", "autoLaunch": False}}))
    s = load_chrome_settings(tmp_path)
    assert s.enabled is True
    assert s.debug_port == 9401
    assert s.source_profile == "Profile 1"
    assert s.auto_launch is False


def test_load_chrome_settings_ignores_bad_types(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({"chrome": {
        "enabled": "yes", "debugPort": "nope"}}))  # wrong types → defaults
    s = load_chrome_settings(tmp_path)
    assert s.enabled is False
    assert s.debug_port == 9333


def test_set_chrome_enabled_roundtrip_and_preserves_keys(tmp_path):
    set_chrome_enabled(tmp_path, True)
    assert load_chrome_settings(tmp_path).enabled is True
    set_chrome_enabled(tmp_path, False)
    assert load_chrome_settings(tmp_path).enabled is False

    (tmp_path / "settings.json").write_text(json.dumps({
        "autoswitch": {"threshold": 80},
        "chrome": {"enabled": False, "debugPort": 9999},
    }))
    set_chrome_enabled(tmp_path, True)
    raw = json.loads((tmp_path / "settings.json").read_text())
    assert raw["chrome"]["enabled"] is True
    assert raw["chrome"]["debugPort"] == 9999          # other chrome keys kept
    assert raw["autoswitch"]["threshold"] == 80        # other sections kept


def test_chromesync_noop_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "is_supported", lambda: True)
    sync = cs.ChromeSync(cs.ChromeSyncSettings(enabled=False), tmp_path)
    assert sync.active is False
    assert sync.capture_for_account("1") is False
    assert sync.sync_to_account("1") is False
    assert sync.capture_from_browser("1") is False
    assert sync.refresh_account("1")               # a reason, not success
    assert sync.detach() is False
    assert sync.open_and_activate("1") is False


def test_chromesync_noop_when_unsupported(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "is_supported", lambda: False)
    sync = cs.ChromeSync(cs.ChromeSyncSettings(enabled=True), tmp_path)
    assert sync.active is False


def test_capture_for_account_rejects_org_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "is_supported", lambda: True)
    monkeypatch.setattr(cs, "automation_chrome_running", lambda *a, **k: True)
    monkeypatch.setattr(
        cs, "capture_tokens_cdp",
        lambda *a, **k: _session(account_uuid="acc", org_uuid="ORG-A"),
    )
    sync = cs.ChromeSync(cs.ChromeSyncSettings(enabled=True), tmp_path)

    # Mismatched org → not stored (the extension is connected to a different account).
    assert sync.capture_for_account("1", expected_org_uuid="ORG-B") is False
    assert cs.ChromeVault(tmp_path).get("1") is None

    # Matching org → stored.
    assert sync.capture_for_account("1", expected_org_uuid="ORG-A") is True
    assert cs.ChromeVault(tmp_path).get("1").org_uuid == "ORG-A"

    # No expected org supplied → best-effort store.
    assert sync.capture_for_account("2") is True


def test_capture_for_account_none_when_no_login(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "is_supported", lambda: True)
    monkeypatch.setattr(cs, "automation_chrome_running", lambda *a, **k: True)
    monkeypatch.setattr(cs, "capture_tokens_cdp", lambda *a, **k: None)
    sync = cs.ChromeSync(cs.ChromeSyncSettings(enabled=True), tmp_path)
    assert sync.capture_for_account("1", expected_org_uuid="ORG") is False


def test_capture_for_account_needs_running_browser(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "is_supported", lambda: True)
    monkeypatch.setattr(cs, "automation_chrome_running", lambda *a, **k: False)
    sync = cs.ChromeSync(cs.ChromeSyncSettings(enabled=True), tmp_path)
    # Tokens live in the running extension's memory; no browser → nothing to capture.
    assert sync.capture_for_account("1") is False


def test_refresh_account_updates_vault(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "is_supported", lambda: True)
    vault = cs.ChromeVault(tmp_path)
    vault.put("1", _session(access_token="old", refresh_token="rt-old", account_uuid="acc"))

    def fake_refresh(session, **k):
        return cs.ChromeSession(
            access_token="new", refresh_token="rt-new",
            account_uuid=session.account_uuid, captured_at=42,
        )

    monkeypatch.setattr(cs, "refresh_session_detailed", lambda s, **k: (fake_refresh(s), cs.REFRESH_OK))
    sync = cs.ChromeSync(cs.ChromeSyncSettings(enabled=True), tmp_path)
    assert sync.refresh_account("1") == ""
    stored = vault.get("1")
    assert stored.access_token == "new"
    assert stored.refresh_token == "rt-new"


def test_refresh_account_failure_keeps_old(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "is_supported", lambda: True)
    vault = cs.ChromeVault(tmp_path)
    vault.put("1", _session(access_token="old", refresh_token="rt-old"))
    sync = cs.ChromeSync(cs.ChromeSyncSettings(enabled=True), tmp_path)
    assert "try again later" in sync.refresh_account("1")
    assert vault.get("1").access_token == "old"   # unchanged
    assert vault.get("1").refresh_dead is False   # a network failure is not a revocation


# --------------------------------------------------------------------------- #
# Extension state written alongside the tokens (v1.0.100+ builds)
# --------------------------------------------------------------------------- #
def test_session_roundtrip_keeps_token_org_scope_and_dead_flag():
    s = _session(account_uuid="acc", token_org_uuid="org", token_org_hybrid=False,
                 token_scope="user:profile", refresh_dead=True)
    d = s.to_dict()
    assert d["tokenOrg"] == {"uuid": "org", "hybrid": False}
    assert d["tokenScope"] == "user:profile"
    assert d["refreshDead"] is True
    assert cs.ChromeSession.from_dict(d) == s


def _state(**kw) -> dict:
    base = {"accessToken": "at", "refreshToken": "rt", "tokenExpiry": 0, "accountUuid": "acc",
            "email": "a@x", "orgUuid": "org", "tokenOrg": None, "tokenScope": "", "authFailure": ""}
    base.update(kw)
    return base


def test_capture_takes_org_from_token_org_before_profile_loads(monkeypatch):
    monkeypatch.setattr(cs, "_read_state_cdp", lambda *a, **k: _state(
        orgUuid="", tokenOrg={"uuid": "ORG-T", "hybrid": False}, tokenScope="user:profile"))
    got = cs.capture_tokens_cdp(1)
    assert got.org_uuid == "org-t"
    assert (got.token_org_uuid, got.token_org_hybrid, got.token_scope) == ("org-t", False, "user:profile")


def test_capture_refuses_tokens_the_extension_failed_on(monkeypatch):
    monkeypatch.setattr(cs, "_read_state_cdp", lambda *a, **k: _state(authFailure="session_expired"))
    assert cs.capture_tokens_cdp(1) is None


def _js_calls(js: str) -> list:
    """The JSON arguments of each chrome.storage call in an injected script."""
    import re
    out = []
    for area, op, arg in re.findall(
            r"chrome\.storage\.(session|local)\.(set|remove)\((\{.*?\}|\[.*?\])\);", js):
        try:
            out.append((area, op, json.loads(arg)))
        except json.JSONDecodeError:
            pass  # the token write is a JS object literal, not JSON
    return out


def test_inject_writes_token_org_and_clears_the_previous_accounts_state():
    js = cs._inject_js(_session(account_uuid="acc", token_org_uuid="org", token_org_hybrid=False,
                                token_scope="user:profile"))
    calls = _js_calls(js)
    local_removed = next(a for area, op, a in calls if area == "local" and op == "remove")
    session_removed = next(a for area, op, a in calls if area == "session" and op == "remove")
    local_set = next(a for area, op, a in calls if area == "local" and op == "set")
    # A displaced disk refresh token would be revoked by the extension later.
    assert "diskRefreshToken" in local_removed
    assert {"tokenOrgUuid", "swAnalyticsProfileCache", "lastAuthFailureReason"} <= set(session_removed)
    assert local_set == {"accountUuid": "acc", "tokenOrg": {"uuid": "org", "hybrid": False},
                         "tokenScope": "user:profile"}


def test_inject_leaves_token_org_alone_when_never_captured():
    calls = _js_calls(cs._inject_js(_session(account_uuid="acc")))
    local_set = next(a for area, op, a in calls if area == "local" and op == "set")
    assert local_set == {"accountUuid": "acc"}


def test_adopted_needs_more_than_the_injected_account_uuid():
    target = _session(account_uuid="acc", email="A@x")
    assert cs._adopted(_state(email="a@x"), target)
    # accountUuid matches only because the injection wrote it; no profile yet.
    assert not cs._adopted(_state(email=""), target)
    assert not cs._adopted(_state(authFailure="session_expired"), target)
    assert not cs._adopted(_state(refreshToken=""), target)
    assert not cs._adopted(_state(email="b@x"), target)
    assert not cs._adopted(_state(accountUuid="other"), target)
    assert not cs._adopted(None, target)


def test_inject_reports_failure_when_the_extension_rejects_the_token(monkeypatch):
    monkeypatch.setattr(cs, "_sw_target", lambda *a, **k: {"id": "sw"})
    monkeypatch.setattr(cs, "_eval_in_sw", lambda *a, **k: "ok")
    monkeypatch.setattr(cs, "_restart_sw", lambda *a, **k: None)
    monkeypatch.setattr(cs.time, "sleep", lambda *_: None)
    monkeypatch.setattr(cs, "_read_state_cdp", lambda *a, **k: _state(
        email="", refreshToken="", authFailure="session_expired"))
    assert cs.inject_tokens_cdp(1, _session(account_uuid="acc"), settle=0) is False
    monkeypatch.setattr(cs, "_read_state_cdp", lambda *a, **k: _state())
    assert cs.inject_tokens_cdp(1, _session(account_uuid="acc", email="a@x"), settle=0) is True


def test_access_expired():
    now = time.time() * 1000
    assert cs._access_expired(_session(token_expiry=int(now - 1000)))
    assert cs._access_expired(_session(token_expiry=int(now + 60_000)))  # inside the margin
    assert not cs._access_expired(_session(token_expiry=int(now + 3_600_000)))
    assert not cs._access_expired(_session(token_expiry=0))               # unknown


# --------------------------------------------------------------------------- #
# Capture guard (`cswap chrome capture N`, setup wizard, add)
# --------------------------------------------------------------------------- #
def test_capture_checked_rejects_another_member_of_the_same_org(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "is_supported", lambda: True)
    monkeypatch.setattr(cs, "automation_chrome_running", lambda *a, **k: True)
    monkeypatch.setattr(cs, "capture_tokens_cdp",
                        lambda *a, **k: _session(account_uuid="acc", org_uuid="TEAM", email="b@x"))
    sync = cs.ChromeSync(cs.ChromeSyncSettings(enabled=True), tmp_path)
    reason = sync.capture_checked("1", "TEAM", "a@x")
    assert "b@x" in reason
    assert cs.ChromeVault(tmp_path).get("1") is None
    assert sync.capture_checked("1", "TEAM", "A@X".replace("A@X", "b@x")) == ""


def test_capture_checked_refuses_when_org_not_known_yet(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "is_supported", lambda: True)
    monkeypatch.setattr(cs, "automation_chrome_running", lambda *a, **k: True)
    monkeypatch.setattr(cs, "capture_tokens_cdp",
                        lambda *a, **k: _session(account_uuid="acc", org_uuid="", email="a@x"))
    sync = cs.ChromeSync(cs.ChromeSyncSettings(enabled=True), tmp_path)
    assert sync.capture_checked("1", "ORG", "a@x")
    assert cs.ChromeVault(tmp_path).get("1") is None


# --------------------------------------------------------------------------- #
# Revoked refresh tokens + the live account
# --------------------------------------------------------------------------- #
def test_refresh_rejected_marks_the_session_dead(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "is_supported", lambda: True)
    monkeypatch.setattr(cs, "refresh_session_detailed", lambda *a, **k: (None, cs.REFRESH_REJECTED))
    vault = cs.ChromeVault(tmp_path)
    vault.put("1", _session(account_uuid="acc"))
    sync = cs.ChromeSync(cs.ChromeSyncSettings(enabled=True), tmp_path)
    assert "cswap chrome capture 1" in sync.refresh_account("1")
    assert vault.get("1").refresh_dead is True
    # A dead session is never injected.
    monkeypatch.setattr(cs, "inject_tokens_cdp", lambda *a, **k: pytest.fail("injected"))
    assert sync.sync_to_account("1") is False


def test_refresh_refuses_the_account_the_browser_is_on(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "is_supported", lambda: True)
    monkeypatch.setattr(cs, "automation_chrome_running", lambda *a, **k: True)
    monkeypatch.setattr(cs, "_read_state_cdp", lambda *a, **k: _state(accountUuid="acc"))
    monkeypatch.setattr(cs, "refresh_session_detailed", lambda *a, **k: pytest.fail("rotated"))
    cs.ChromeVault(tmp_path).put("1", _session(account_uuid="acc"))
    sync = cs.ChromeSync(cs.ChromeSyncSettings(enabled=True), tmp_path)
    assert "right now" in sync.refresh_account("1")


# --------------------------------------------------------------------------- #
# sync_to_account
# --------------------------------------------------------------------------- #
def _sync_env(tmp_path, monkeypatch, *, live_state, settled):
    monkeypatch.setattr(cs, "is_supported", lambda: True)
    monkeypatch.setattr(cs, "automation_chrome_running", lambda *a, **k: True)
    monkeypatch.setattr(cs, "_read_state_cdp", lambda *a, **k: live_state)
    captures = iter([None, settled])  # outgoing snapshot, then the settled read
    monkeypatch.setattr(cs, "capture_tokens_cdp", lambda *a, **k: next(captures))
    return cs.ChromeSync(cs.ChromeSyncSettings(enabled=True), tmp_path)


def test_sync_fails_when_nothing_settles_and_keeps_the_stored_tokens(tmp_path, monkeypatch):
    sync = _sync_env(tmp_path, monkeypatch, live_state=_state(accountUuid="other"), settled=None)
    monkeypatch.setattr(cs, "inject_tokens_cdp", lambda *a, **k: True)
    cs.ChromeVault(tmp_path).put("2", _session(access_token="keep", account_uuid="acc"))
    assert sync.sync_to_account("2") is False
    assert cs.ChromeVault(tmp_path).get("2").access_token == "keep"


def test_sync_refreshes_an_expired_access_token_before_injecting(tmp_path, monkeypatch):
    settled = _session(access_token="settled", refresh_token="rt2", account_uuid="acc")
    sync = _sync_env(tmp_path, monkeypatch, live_state=_state(accountUuid="other"), settled=settled)
    # Like the real refresh: the stored session with new tokens (identity kept).
    monkeypatch.setattr(cs, "refresh_session_detailed", lambda s, **k: (
        cs.replace(s, access_token="fresh", token_expiry=0), cs.REFRESH_OK))
    injected = []
    monkeypatch.setattr(cs, "inject_tokens_cdp", lambda port, s, **k: injected.append(s) or True)
    expired = int(time.time() * 1000) - 1000
    cs.ChromeVault(tmp_path).put("2", _session(access_token="dead", account_uuid="acc",
                                               token_expiry=expired, token_org_uuid="org",
                                               token_org_hybrid=False))
    assert sync.sync_to_account("2") is True
    assert [s.access_token for s in injected] == ["fresh"]
    stored = cs.ChromeVault(tmp_path).get("2")
    assert stored.access_token == "settled"
    assert stored.token_org_uuid == "org"    # kept when the settled read lacks it


def test_sync_is_a_no_op_when_already_on_the_account(tmp_path, monkeypatch):
    sync = _sync_env(tmp_path, monkeypatch, live_state=_state(accountUuid="acc", email="a@x"),
                     settled=None)
    monkeypatch.setattr(cs, "inject_tokens_cdp", lambda *a, **k: pytest.fail("injected"))
    cs.ChromeVault(tmp_path).put("1", _session(account_uuid="acc", email="a@x"))
    assert sync.sync_to_account("1") is True
