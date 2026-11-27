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
    monkeypatch.setattr(cs, "_browser_call", lambda *a, **k: pytest.fail("browser CDP call"))
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


def test_inject_removes_a_token_org_it_never_captured():
    # Left in place it would keep naming the previous account's org.
    calls = _js_calls(cs._inject_js(_session(account_uuid="acc")))
    local_set = next(a for area, op, a in calls if area == "local" and op == "set")
    local_removed = next(a for area, op, a in calls if area == "local" and op == "remove")
    assert local_set == {"accountUuid": "acc"}
    assert "tokenOrg" in local_removed


def test_capture_drops_a_token_org_that_disagrees_with_the_profile(monkeypatch):
    monkeypatch.setattr(cs, "_read_state_cdp", lambda *a, **k: _state(
        orgUuid="org-b", tokenOrg={"uuid": "org-a", "hybrid": False}))
    got = cs.capture_tokens_cdp(1)
    assert got.org_uuid == "org-b"
    assert (got.token_org_uuid, got.token_org_hybrid) == ("", None)


def test_adopted_needs_more_than_the_injected_account_uuid():
    target = _session(account_uuid="acc", email="A@x")
    assert cs._adopted(_state(email="a@x"), target)
    # No profile re-fetch yet is not a failure: the extension does that on its
    # own schedule.
    assert cs._adopted(_state(email=""), target)
    # The failure a dead token leaves behind.
    assert not cs._adopted(_state(email="", refreshToken="", authFailure="session_expired"), target)
    assert not cs._adopted(_state(authFailure="session_expired"), target)
    assert not cs._adopted(_state(refreshToken=""), target)
    assert not cs._adopted(_state(email="b@x"), target)
    assert not cs._adopted(_state(accountUuid="other"), target)
    assert not cs._adopted(None, target)
    # A stored session without an account uuid needs the fetched profile email.
    legacy = _session(email="a@x")
    assert not cs._adopted(_state(email=""), legacy)
    assert cs._adopted(_state(email="a@x"), legacy)
    assert not cs._adopted(_state(email="a@x"), _session())


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


def test_detach_clears_identity_but_keeps_preferences():
    calls = _js_calls(cs._DETACH_JS)
    removed = {k for _, op, keys in calls if op == "remove" for k in keys}
    assert {"accessToken", "refreshToken", "accountUuid", "tokenOrg", "diskRefreshToken"} <= removed
    assert not removed & {"permissionStorage", "permissionConsentByAccount", "anonymousId"}


def test_detach_reports_tokens_that_came_back(monkeypatch):
    monkeypatch.setattr(cs, "_sw_target", lambda *a, **k: {"id": "sw"})
    monkeypatch.setattr(cs, "_eval_in_sw", lambda *a, **k: "ok")
    monkeypatch.setattr(cs, "_restart_sw", lambda *a, **k: None)
    monkeypatch.setattr(cs.time, "sleep", lambda *_: None)
    monkeypatch.setattr(cs, "_read_state_cdp", lambda *a, **k: _state())
    assert cs.detach_extension_cdp(1) is False
    monkeypatch.setattr(cs, "_read_state_cdp",
                        lambda *a, **k: _state(accessToken="", refreshToken=""))
    assert cs.detach_extension_cdp(1) is True


def test_sync_uses_the_tokens_the_snapshot_just_stored(tmp_path, monkeypatch):
    # The extension is on account 2 already but has no profile email cached and
    # an old copy of 2 sits in the vault: the snapshot's fresh copy must win.
    monkeypatch.setattr(cs, "is_supported", lambda: True)
    monkeypatch.setattr(cs, "automation_chrome_running", lambda *a, **k: True)
    monkeypatch.setattr(cs, "_read_state_cdp", lambda *a, **k: _state(accountUuid="other"))
    vault = cs.ChromeVault(tmp_path)
    expired = int(time.time() * 1000) - 1000
    vault.put("2", _session(access_token="old", refresh_token="rt-old", account_uuid="acc",
                            token_expiry=expired))
    live = _session(access_token="live", refresh_token="rt-live", account_uuid="acc",
                    token_expiry=int(time.time() * 1000) + 3_600_000)
    captures = iter([live, live])
    monkeypatch.setattr(cs, "capture_tokens_cdp", lambda *a, **k: next(captures))
    monkeypatch.setattr(cs, "refresh_session_detailed", lambda *a, **k: pytest.fail("refreshed"))
    injected = []
    monkeypatch.setattr(cs, "inject_tokens_cdp", lambda port, s, **k: injected.append(s) or True)
    sync = cs.ChromeSync(cs.ChromeSyncSettings(enabled=True), tmp_path)
    assert sync.sync_to_account("2") is True
    assert [s.refresh_token for s in injected] == ["rt-live"]


# --------------------------------------------------------------------------- #
# follow_switch (the menu bar runs the browser sync off the main thread)
# --------------------------------------------------------------------------- #
def test_follow_switch_is_inline_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "is_supported", lambda: True)
    sync = cs.ChromeSync(cs.ChromeSyncSettings(enabled=True), tmp_path)
    seen = []
    monkeypatch.setattr(sync, "sync_to_account", lambda n: seen.append(n) or True)
    sync.follow_switch("2")
    assert seen == ["2"]


def test_follow_switch_in_background_returns_at_once_and_ends_on_the_latest(tmp_path, monkeypatch):
    import threading as th
    monkeypatch.setattr(cs, "is_supported", lambda: True)
    sync = cs.ChromeSync(cs.ChromeSyncSettings(enabled=True), tmp_path)
    sync.background = True
    release = th.Event()
    started = th.Event()
    seen = []

    def slow_sync(n):
        seen.append(n)
        started.set()
        release.wait(5)
        return True

    monkeypatch.setattr(sync, "sync_to_account", slow_sync)
    t0 = time.monotonic()
    sync.follow_switch("2")
    assert started.wait(5)
    # Two more switches while the first sync is still running.
    sync.follow_switch("3")
    sync.follow_switch("1")
    assert time.monotonic() - t0 < 1
    release.set()
    assert sync.wait_idle(5)
    assert seen == ["2", "1"]   # "3" was superseded before its turn


def test_follow_switch_is_a_no_op_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "is_supported", lambda: True)
    sync = cs.ChromeSync(cs.ChromeSyncSettings(enabled=False), tmp_path)
    sync.background = True
    monkeypatch.setattr(sync, "sync_to_account", lambda n: pytest.fail("synced"))
    sync.follow_switch("2")
    assert sync.wait_idle(1)


def test_wait_for_sw_gone_waits_for_the_old_worker_only(monkeypatch):
    monkeypatch.setattr(cs.time, "sleep", lambda *_: None)
    listings = iter([{"id": "old"}, {"id": "old"}, None])
    monkeypatch.setattr(cs, "_sw_target", lambda *a, **k: next(listings))
    assert cs._wait_for_sw_gone(1, "old") is True
    # A new worker already up under another id also means the old one is gone.
    monkeypatch.setattr(cs, "_sw_target", lambda *a, **k: {"id": "new"})
    assert cs._wait_for_sw_gone(1, "old") is True
    # Still the old one at the deadline.
    monkeypatch.setattr(cs, "_sw_target", lambda *a, **k: {"id": "old"})
    assert cs._wait_for_sw_gone(1, "old", timeout=0) is False


# --------------------------------------------------------------------------- #
# claude.ai web login (cookies) follows the extension's account
# --------------------------------------------------------------------------- #
def _cdp_cookie(name, value="v", domain=".claude.ai", **kw):
    return {"name": name, "value": value, "domain": domain, "path": "/", "secure": True,
            "httpOnly": True, "sameSite": "Lax", "expires": 2_000_000_000, "session": False, **kw}


def test_cookie_param_keeps_host_only_cookies_host_only():
    assert cs._cookie_param(_cdp_cookie("sessionKey"))["domain"] == ".claude.ai"
    host = cs._cookie_param(_cdp_cookie("__Host-sessionBinding", domain="claude.ai"))
    assert "domain" not in host and host["url"] == "https://claude.ai/"
    assert "expires" not in cs._cookie_param(_cdp_cookie("ion", session=True, expires=-1))


def test_capture_web_cookies_takes_only_identity_cookies(monkeypatch):
    monkeypatch.setattr(cs, "read_web_identity_cdp", lambda *a, **k: {"uuid": "acc", "email": "a@x"})
    monkeypatch.setattr(cs, "_claude_cookies", lambda *a, **k: [
        _cdp_cookie("sessionKey"), _cdp_cookie("routingHint"), _cdp_cookie("cf_clearance"),
        _cdp_cookie("lastActiveOrg", domain="claude.ai"), _cdp_cookie("anthropic-device-id")])
    uuid, cookies = cs.capture_web_cookies_cdp(1)
    assert uuid == "acc"
    assert sorted(c["name"] for c in cookies) == ["lastActiveOrg", "routingHint", "sessionKey"]


def test_capture_web_cookies_needs_a_session_key(monkeypatch):
    monkeypatch.setattr(cs, "read_web_identity_cdp", lambda *a, **k: {"uuid": "acc", "email": ""})
    monkeypatch.setattr(cs, "_claude_cookies", lambda *a, **k: [_cdp_cookie("lastActiveOrg")])
    assert cs.capture_web_cookies_cdp(1) is None


def test_apply_web_cookies_expires_identity_and_bound_cookies_only(monkeypatch):
    calls = []
    monkeypatch.setattr(cs, "_claude_cookies", lambda *a, **k: [
        _cdp_cookie("sessionKey", "old"), _cdp_cookie("__Host-sessionBinding", domain="claude.ai"),
        _cdp_cookie("cf_clearance")])
    monkeypatch.setattr(cs, "_browser_call", lambda port, m, p=None: calls.append((m, p)) or {})
    new = [cs._cookie_param(_cdp_cookie("sessionKey", "new"))]
    cs.apply_web_cookies_cdp(1, new)
    (m1, expire), (m2, put) = calls
    assert m1 == m2 == "Storage.setCookies"
    assert sorted(c["name"] for c in expire["cookies"]) == ["__Host-sessionBinding", "sessionKey"]
    assert all(c["expires"] == 1 and c["value"] == "" for c in expire["cookies"])
    assert put["cookies"] == new


def test_snapshot_files_the_web_login_under_its_own_account(tmp_path, monkeypatch):
    # The extension is on account 1 but claude.ai is signed in as account 2.
    monkeypatch.setattr(cs, "is_supported", lambda: True)
    vault = cs.ChromeVault(tmp_path)
    vault.put("1", _session(account_uuid="acc-1", email="a@x"))
    vault.put("2", _session(account_uuid="acc-2", email="b@x"))
    monkeypatch.setattr(cs, "capture_tokens_cdp", lambda *a, **k: None)
    monkeypatch.setattr(cs, "capture_web_login_cdp",
                        lambda *a, **k: ("acc-2", "b@x", [{"name": "sessionKey", "value": "s2"}]))
    sync = cs.ChromeSync(cs.ChromeSyncSettings(enabled=True), tmp_path)
    assert sync._snapshot_current() == "2"
    assert cs.WebVault(tmp_path).get_web("2")["cookies"][0]["value"] == "s2"
    assert cs.WebVault(tmp_path).get_web("1") is None


def test_sync_puts_the_target_web_login_in_before_injecting(tmp_path, monkeypatch):
    sync = _sync_env(tmp_path, monkeypatch, live_state=_state(accountUuid="other"),
                     settled=_session(account_uuid="acc"))
    cs.ChromeVault(tmp_path).put("2", _session(account_uuid="acc"))
    cs.WebVault(tmp_path).put_web("2", "acc", "b@x", [{"name": "sessionKey", "value": "s2"}])
    monkeypatch.setattr(cs, "capture_web_login_cdp", lambda *a, **k: None)
    order = []
    monkeypatch.setattr(cs, "apply_web_cookies_cdp", lambda port, c: order.append(("web", c)))
    monkeypatch.setattr(cs, "inject_tokens_cdp", lambda *a, **k: order.append(("inject",)) or True)
    assert sync.sync_to_account("2") is True
    assert order[0] == ("web", [{"name": "sessionKey", "value": "s2"}])
    assert order[1] == ("inject",)


def test_sync_leaves_the_web_alone_when_it_is_already_on_the_target(tmp_path, monkeypatch):
    sync = _sync_env(tmp_path, monkeypatch, live_state=_state(accountUuid="other"),
                     settled=_session(account_uuid="acc"))
    cs.ChromeVault(tmp_path).put("2", _session(account_uuid="acc"))
    monkeypatch.setattr(cs, "capture_web_login_cdp",
                        lambda *a, **k: ("acc", "", [{"name": "sessionKey", "value": "s2"}]))
    monkeypatch.setattr(cs, "apply_web_cookies_cdp", lambda *a, **k: pytest.fail("web replaced"))
    monkeypatch.setattr(cs, "inject_tokens_cdp", lambda *a, **k: True)
    assert sync.sync_to_account("2") is True


def test_capture_stores_the_web_login_even_when_the_extension_is_elsewhere(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "is_supported", lambda: True)
    monkeypatch.setattr(cs, "automation_chrome_running", lambda *a, **k: True)
    cs.ChromeVault(tmp_path).put("1", _session(account_uuid="acc-1", email="a@x"))
    monkeypatch.setattr(cs, "capture_tokens_cdp",
                        lambda *a, **k: _session(account_uuid="acc-2", email="b@x", org_uuid="o"))
    monkeypatch.setattr(cs, "capture_web_login_cdp",
                        lambda *a, **k: ("acc-1", "a@x", [{"name": "sessionKey", "value": "s1"}]))
    sync = cs.ChromeSync(cs.ChromeSyncSettings(enabled=True), tmp_path)
    assert "b@x" in sync.capture_checked("1", "o", "a@x")       # extension: wrong account
    assert sync.web_stored("1")                                  # web login: stored anyway
    assert cs.ChromeVault(tmp_path).get("1").account_uuid == "acc-1"  # tokens untouched


# --------------------------------------------------------------------------- #
# capture without a slot number
# --------------------------------------------------------------------------- #
_SLOTS = [("1", "a@x", "org-a"), ("2", "b@x", "team"), ("3", "b@x", "org-b"), ("4", "c@x", "team")]


def test_match_slot_uses_the_org_only_when_the_email_is_ambiguous():
    assert cs.match_slot(_SLOTS, "A@X") == "1"
    assert cs.match_slot(_SLOTS, "a@x", "unrelated") == "1"   # one slot: org not needed
    assert cs.match_slot(_SLOTS, "b@x") is None               # two orgs, no org given
    assert cs.match_slot(_SLOTS, "b@x", "TEAM") == "2"
    assert cs.match_slot(_SLOTS, "z@x") is None
    assert cs.match_slot(_SLOTS, "") is None


def test_capture_auto_files_extension_and_web_by_their_own_accounts(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "is_supported", lambda: True)
    monkeypatch.setattr(cs, "automation_chrome_running", lambda *a, **k: True)
    monkeypatch.setattr(cs, "capture_tokens_cdp",
                        lambda *a, **k: _session(account_uuid="acc-2", email="b@x", org_uuid="team"))
    # claude.ai is signed in as an account with no stored tokens yet: filed by email.
    monkeypatch.setattr(cs, "capture_web_login_cdp",
                        lambda *a, **k: ("acc-4", "c@x", [{"name": "sessionKey", "value": "s4"}]))
    sync = cs.ChromeSync(cs.ChromeSyncSettings(enabled=True), tmp_path)
    got = sync.capture_auto(_SLOTS)
    assert got == {"extension": "2", "web": "4", "reason": ""}
    assert cs.ChromeVault(tmp_path).get("2").account_uuid == "acc-2"
    assert cs.WebVault(tmp_path).get_web("4")["accountUuid"] == "acc-4"


def test_capture_auto_refuses_an_unmanaged_or_ambiguous_extension_account(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "is_supported", lambda: True)
    monkeypatch.setattr(cs, "automation_chrome_running", lambda *a, **k: True)
    monkeypatch.setattr(cs, "capture_tokens_cdp",
                        lambda *a, **k: _session(account_uuid="acc", email="b@x", org_uuid=""))
    monkeypatch.setattr(cs, "capture_web_login_cdp", lambda *a, **k: None)
    sync = cs.ChromeSync(cs.ChromeSyncSettings(enabled=True), tmp_path)
    got = sync.capture_auto(_SLOTS)
    assert got["extension"] is None and "cswap add" in got["reason"]
    assert cs.ChromeVault(tmp_path).account_numbers() == set()


def test_web_login_is_not_filed_under_a_slot_whose_tokens_are_another_account(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "is_supported", lambda: True)
    cs.ChromeVault(tmp_path).put("1", _session(account_uuid="acc-1", email="a@x"))
    monkeypatch.setattr(cs, "capture_web_login_cdp",
                        lambda *a, **k: ("acc-other", "a@x", [{"name": "sessionKey", "value": "s"}]))
    sync = cs.ChromeSync(cs.ChromeSyncSettings(enabled=True), tmp_path)
    assert sync._snapshot_web(_SLOTS) is None
    assert cs.WebVault(tmp_path).get_web("1") is None


def test_first_capture_into_a_slot_also_files_its_web_login(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "is_supported", lambda: True)
    monkeypatch.setattr(cs, "automation_chrome_running", lambda *a, **k: True)
    monkeypatch.setattr(cs, "capture_tokens_cdp",
                        lambda *a, **k: _session(account_uuid="acc-1", email="a@x", org_uuid="org-a"))
    monkeypatch.setattr(cs, "capture_web_login_cdp",
                        lambda *a, **k: ("acc-1", "a@x", [{"name": "sessionKey", "value": "s1"}]))
    sync = cs.ChromeSync(cs.ChromeSyncSettings(enabled=True), tmp_path)
    assert sync.capture_checked("1", "org-a", "a@x") == ""
    assert sync.web_stored("1")


def test_switch_to_an_account_without_a_web_login_signs_the_web_out(tmp_path, monkeypatch):
    sync = _sync_env(tmp_path, monkeypatch, live_state=_state(accountUuid="other"),
                     settled=_session(account_uuid="acc-3"))
    cs.ChromeVault(tmp_path).put("2", _session(account_uuid="acc-2"))
    cs.ChromeVault(tmp_path).put("3", _session(account_uuid="acc-3"))
    monkeypatch.setattr(cs, "capture_web_login_cdp",
                        lambda *a, **k: ("acc-2", "b@x", [{"name": "sessionKey", "value": "s2"}]))
    applied = []
    monkeypatch.setattr(cs, "apply_web_cookies_cdp", lambda port, c: applied.append(c))
    monkeypatch.setattr(cs, "inject_tokens_cdp", lambda *a, **k: True)
    assert sync.sync_to_account("3") is True
    assert applied == [[]]                                   # signed out, not left on 2
    assert cs.WebVault(tmp_path).get_web("2") is not None    # 2's login was stored first


def test_a_web_login_that_could_not_be_stored_is_left_in_place(tmp_path, monkeypatch):
    sync = _sync_env(tmp_path, monkeypatch, live_state=_state(accountUuid="other"),
                     settled=_session(account_uuid="acc-3"))
    cs.ChromeVault(tmp_path).put("3", _session(account_uuid="acc-3"))
    monkeypatch.setattr(cs, "capture_web_login_cdp", lambda *a, **k: None)
    monkeypatch.setattr(cs, "apply_web_cookies_cdp", lambda *a, **k: pytest.fail("cleared"))
    monkeypatch.setattr(cs, "inject_tokens_cdp", lambda *a, **k: True)
    assert sync.sync_to_account("3") is True


def test_browser_call_retries_a_transient_cdp_error(monkeypatch):
    monkeypatch.undo()  # the autouse guard stubs _browser_call itself
    monkeypatch.setattr(cs, "_browser_ws", lambda port: "ws://x")
    monkeypatch.setattr(cs.time, "sleep", lambda s: None)
    answers = iter([RuntimeError("Storage.getCookies: Browser context management is not supported."),
                    {"cookies": []}])

    class FakeCDP:
        def __init__(self, url): pass
        def call(self, method, params):
            a = next(answers)
            if isinstance(a, Exception):
                raise a
            return a
        def close(self): pass

    monkeypatch.setattr(cs, "_CDP", FakeCDP)
    assert cs._browser_call(9333, "Storage.getCookies") == {"cookies": []}
