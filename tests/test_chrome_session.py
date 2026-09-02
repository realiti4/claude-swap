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

from claude_swap import chrome_session as cs
from claude_swap.settings import load_chrome_settings, set_chrome_enabled


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
    assert sync.refresh_account("1") is False
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

    monkeypatch.setattr(cs, "refresh_session", fake_refresh)
    sync = cs.ChromeSync(cs.ChromeSyncSettings(enabled=True), tmp_path)
    assert sync.refresh_account("1") is True
    stored = vault.get("1")
    assert stored.access_token == "new"
    assert stored.refresh_token == "rt-new"


def test_refresh_account_failure_keeps_old(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "is_supported", lambda: True)
    vault = cs.ChromeVault(tmp_path)
    vault.put("1", _session(access_token="old", refresh_token="rt-old"))
    monkeypatch.setattr(cs, "refresh_session", lambda *a, **k: None)
    sync = cs.ChromeSync(cs.ChromeSyncSettings(enabled=True), tmp_path)
    assert sync.refresh_account("1") is False
    assert vault.get("1").access_token == "old"   # unchanged
