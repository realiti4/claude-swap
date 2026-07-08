"""Tests for Codex account switching."""

from __future__ import annotations

import email.message
import json
import urllib.error
from pathlib import Path

import pytest

from claude_swap.codex import (
    CodexAccountSwitcher,
    _UsageFetchError,
    fetch_codex_usage,
)
from claude_swap.usage_store import SERVE_TTL_S
from claude_swap.exceptions import AccountNotFoundError, ConfigError, ValidationError
from claude_swap.models import Platform


def _http_error(code: int, retry_after: str | None) -> urllib.error.HTTPError:
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError("https://chatgpt.com/x", code, "err", headers, None)


def _raise_urlopen(exc: Exception):
    def fake(request, timeout):
        raise exc

    return fake


def _body_urlopen(body: bytes):
    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return body

    def fake(request, timeout):
        return FakeResponse()

    return fake


def _write_auth(home: Path, payload: dict) -> Path:
    auth_path = home / ".codex" / "auth.json"
    auth_path.parent.mkdir(parents=True, exist_ok=True)
    auth_path.write_text(json.dumps(payload), encoding="utf-8")
    return auth_path


def _auth(account_id: str) -> dict:
    return {
        "auth_mode": "chatgpt",
        "tokens": {"account_id": account_id, "access_token": f"token-{account_id}"},
    }


def _auth_with_token(account_id: str, access_token: str) -> dict:
    return {
        "auth_mode": "chatgpt",
        "tokens": {"account_id": account_id, "access_token": access_token},
    }


def _api_key_auth(api_key: str) -> dict:
    return {"auth_mode": "apikey", "openai_api_key": api_key}


def test_fetch_codex_usage_calls_wham_backend(monkeypatch: pytest.MonkeyPatch):
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps(
                {
                    "rate_limit": {
                        "primary_window": {
                            "limit_window_seconds": 10800,
                            "used_percent": 25,
                            "reset_at": 1783458000,
                        },
                        "secondary_window": {
                            "limit_window_seconds": 604800,
                            "used_percent": 50,
                            "reset_at": 1784062800,
                        },
                    },
                    "plan_type": "plus",
                    "credits": {"balance": "2"},
                }
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["authorization"] = request.get_header("Authorization")
        captured["account"] = request.get_header("Chatgpt-account-id")
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("claude_swap.codex.urllib.request.urlopen", fake_urlopen)

    usage = fetch_codex_usage(json.dumps(_auth("acct-1")), timeout_s=3.0)

    assert captured == {
        "url": "https://chatgpt.com/backend-api/wham/usage",
        "authorization": "Bearer token-acct-1",
        "account": "acct-1",
        "timeout": 3.0,
    }
    assert usage == {
        "windows": [
            {"label": "3h", "pct": 25.0, "resets_at": "2026-07-07T21:00:00Z"},
            {"label": "7d", "pct": 50.0, "resets_at": "2026-07-14T21:00:00Z"},
        ],
        "plan": "plus",
        "credits": 2.0,
    }


def test_add_snapshots_active_auth(temp_home: Path, monkeypatch: pytest.MonkeyPatch):
    _write_auth(temp_home, _auth("acct-1"))
    monkeypatch.setattr(
        "claude_swap.codex.fetch_codex_usage",
        lambda auth_text, timeout_s: "usage unavailable",
    )

    switcher = CodexAccountSwitcher()
    switcher.add_account(label="work", slot=None)

    payload = switcher.list_accounts(json_output=True)
    assert payload == {
        "schemaVersion": 1,
        "provider": "codex",
        "activeAccountNumber": 1,
        "accounts": [
            {
                "number": 1,
                "label": "work",
                "active": True,
                "usageStatus": "unavailable",
                "usage": None,
                "usageError": "usage unavailable",
            }
        ],
    }


def test_list_accounts_prints_codex_usage(
    temp_home: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    _write_auth(temp_home, _auth("acct-1"))
    switcher = CodexAccountSwitcher()
    switcher.add_account(label="work", slot=None)
    capsys.readouterr()

    monkeypatch.setattr(
        "claude_swap.codex.fetch_codex_usage",
        lambda auth_text, timeout_s: {
            "windows": [
                {"label": "3h", "pct": 25.0},
                {"label": "7d", "pct": 50.0},
            ],
            "plan": "plus",
            "credits": 2.0,
        },
    )

    switcher.list_accounts(json_output=False)

    out = capsys.readouterr().out
    assert "Codex accounts:" in out
    assert "  1: work (active)" in out
    assert "├ 3h:" in out
    assert "25%" in out
    assert "├ 7d:" in out
    assert "50%" in out
    assert "├ Plan:" in out
    assert "plus" in out
    assert "└ Credits:" in out
    assert "2" in out


def test_list_accounts_json_includes_codex_usage(
    temp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    _write_auth(temp_home, _auth("acct-1"))
    switcher = CodexAccountSwitcher()
    switcher.add_account(label="work", slot=None)
    switcher._usage_store.clock = lambda: 0.0

    monkeypatch.setattr(
        "claude_swap.codex.fetch_codex_usage",
        lambda auth_text, timeout_s: {
            "windows": [
                {"label": "3h", "pct": 25.0, "resets_at": "2026-01-01T00:00:00Z"},
                {"label": "7d", "pct": 50.0},
            ],
            "plan": "plus",
            "credits": 2.0,
        },
    )

    payload = switcher.list_accounts(json_output=True)

    assert payload["schemaVersion"] == 1
    assert payload["provider"] == "codex"
    assert payload["activeAccountNumber"] == 1
    account = payload["accounts"][0]
    assert account["number"] == 1
    assert account["label"] == "work"
    assert account["active"] is True
    assert account["usageStatus"] == "ok"
    assert account["usage"]["windows"][0]["label"] == "3h"
    assert account["usage"]["windows"][0]["pct"] == 25.0
    assert account["usage"]["windows"][0]["resetsAt"] == "2026-01-01T00:00:00Z"
    assert account["usage"]["windows"][1] == {"label": "7d", "pct": 50.0}
    assert account["usage"]["plan"] == "plus"
    assert account["usage"]["credits"] == 2.0
    assert account["usageFetchedAt"] == "1970-01-01T00:00:00Z"
    assert account["usageAgeSeconds"] == 0.0


def test_codex_usage_fetch_is_cached_between_list_calls(
    temp_home: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    _write_auth(temp_home, _auth("acct-1"))
    switcher = CodexAccountSwitcher()
    switcher.add_account(label="work", slot=None)
    calls = []
    now = [0.0]
    switcher._usage_store.clock = lambda: now[0]

    def fake_fetch(auth_text: str, timeout_s: float) -> dict:
        calls.append((auth_text, timeout_s))
        return {"windows": [{"label": "3h", "pct": 25.0}]}

    monkeypatch.setattr("claude_swap.codex.fetch_codex_usage", fake_fetch)

    switcher.list_accounts(json_output=False)
    payload = switcher.list_accounts(json_output=True)

    assert len(calls) == 1
    assert payload["accounts"][0]["usageStatus"] == "ok"
    assert payload["accounts"][0]["usage"] == {
        "windows": [{"label": "3h", "pct": 25.0}]
    }
    assert "3h:" in capsys.readouterr().out


def test_codex_usage_fetch_failure_serves_last_good(
    temp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    _write_auth(temp_home, _auth("acct-1"))
    switcher = CodexAccountSwitcher()
    switcher.add_account(label="work", slot=None)
    now = [0.0]
    switcher._usage_store.clock = lambda: now[0]
    outcomes: list[dict | str] = [
        {"windows": [{"label": "3h", "pct": 25.0}]},
        "HTTP 503",
    ]

    def fake_fetch(auth_text: str, timeout_s: float) -> dict | str:
        return outcomes.pop(0)

    monkeypatch.setattr("claude_swap.codex.fetch_codex_usage", fake_fetch)

    first = switcher.list_accounts(json_output=True)
    now[0] = SERVE_TTL_S + 1.0
    second = switcher.list_accounts(json_output=True)

    assert first["accounts"][0]["usageStatus"] == "ok"
    assert second["accounts"][0]["usageStatus"] == "ok"
    assert second["accounts"][0]["usage"] == {
        "windows": [{"label": "3h", "pct": 25.0}]
    }
    assert second["accounts"][0]["usageAgeSeconds"] == SERVE_TTL_S + 1.0


def test_codex_usage_cache_identity_changes_when_auth_fingerprint_changes(
    temp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    auth_path = _write_auth(temp_home, _auth_with_token("acct-1", "token-old"))
    switcher = CodexAccountSwitcher()
    switcher.add_account(label="work", slot=1)
    now = [0.0]
    switcher._usage_store.clock = lambda: now[0]
    calls = []
    outcomes: list[dict | str] = [
        "HTTP 503",
        {"windows": [{"label": "3h", "pct": 25.0}]},
    ]

    def fake_fetch(auth_text: str, timeout_s: float) -> dict | str:
        calls.append(auth_text)
        return outcomes.pop(0)

    monkeypatch.setattr("claude_swap.codex.fetch_codex_usage", fake_fetch)

    first = switcher.list_accounts(json_output=True)
    auth_path.write_text(
        json.dumps(_auth_with_token("acct-1", "token-new")),
        encoding="utf-8",
    )
    switcher.add_account(label="work", slot=1)
    second = switcher.list_accounts(json_output=True)

    assert first["accounts"][0]["usageStatus"] == "unavailable"
    assert second["accounts"][0]["usageStatus"] == "ok"
    assert second["accounts"][0]["usage"] == {
        "windows": [{"label": "3h", "pct": 25.0}]
    }
    assert len(calls) == 2


def test_add_without_label_uses_account_id(
    temp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    _write_auth(temp_home, _auth("acct-derived"))
    monkeypatch.setattr(
        "claude_swap.codex.fetch_codex_usage",
        lambda auth_text, timeout_s: "usage unavailable",
    )

    switcher = CodexAccountSwitcher()
    switcher.add_account(label=None, slot=None)

    payload = switcher.list_accounts(json_output=True)
    assert payload["accounts"][0]["label"] == "acct-derived"


def test_empty_auth_object_rejected(temp_home: Path):
    _write_auth(temp_home, {})
    switcher = CodexAccountSwitcher()

    with pytest.raises(ConfigError, match="does not contain a supported Codex credential"):
        switcher.add_account(label=None, slot=None)


def test_add_without_auth_fails(temp_home: Path):
    switcher = CodexAccountSwitcher()

    with pytest.raises(ConfigError, match="No active Codex auth found"):
        switcher.add_account(label=None, slot=None)


def test_switch_replaces_only_auth_json(temp_home: Path):
    auth_path = _write_auth(temp_home, _auth("acct-1"))
    switcher = CodexAccountSwitcher()
    switcher.add_account(label="one", slot=1)

    auth_path.write_text(json.dumps(_auth("acct-2")), encoding="utf-8")
    switcher.add_account(label="two", slot=2)

    switcher.switch("1", json_output=False)

    assert json.loads(auth_path.read_text(encoding="utf-8"))["tokens"]["account_id"] == "acct-1"
    assert switcher.status(json_output=True)["active"] == {
        "number": 1,
        "label": "one",
        "managed": True,
    }


def test_switch_rotation_is_independent_from_claude_sequence(temp_home: Path):
    auth_path = _write_auth(temp_home, _auth("acct-1"))
    switcher = CodexAccountSwitcher()
    switcher.add_account(label="one", slot=1)

    auth_path.write_text(json.dumps(_auth("acct-2")), encoding="utf-8")
    switcher.add_account(label="two", slot=2)

    claude_sequence = switcher.backup_root / "sequence.json"
    claude_sequence.write_text(
        json.dumps({"activeAccountNumber": 99, "sequence": [99], "accounts": {}}),
        encoding="utf-8",
    )

    switcher.switch(None, json_output=False)

    assert json.loads(auth_path.read_text(encoding="utf-8"))["tokens"]["account_id"] == "acct-1"


def test_switch_backs_up_current_managed_auth_before_replacing(temp_home: Path):
    auth_path = _write_auth(temp_home, _auth("acct-1"))
    switcher = CodexAccountSwitcher()
    switcher.add_account(label="one", slot=1)

    auth_path.write_text(json.dumps(_auth("acct-2")), encoding="utf-8")
    switcher.add_account(label="two", slot=2)

    refreshed_two = {"auth_mode": "chatgpt", "tokens": {"account_id": "acct-2", "refresh": "new"}}
    auth_path.write_text(json.dumps(refreshed_two), encoding="utf-8")
    switcher.switch("one", json_output=False)
    switcher.switch("two", json_output=False)

    assert json.loads(auth_path.read_text(encoding="utf-8")) == refreshed_two


def test_switch_json_uses_codex_label_refs(temp_home: Path):
    auth_path = _write_auth(temp_home, _auth("acct-1"))
    switcher = CodexAccountSwitcher()
    switcher.add_account(label="one", slot=1)

    auth_path.write_text(json.dumps(_auth("acct-2")), encoding="utf-8")
    switcher.add_account(label="two", slot=2)

    result = switcher.switch("one", json_output=True)

    assert result["to"] == {"number": 1, "label": "one"}
    assert "email" not in result["to"]


def test_switch_rejects_corrupt_stored_auth(temp_home: Path):
    auth_path = _write_auth(temp_home, _auth("acct-1"))
    switcher = CodexAccountSwitcher()
    switcher.add_account(label="one", slot=1)

    auth_path.write_text(json.dumps(_auth("acct-2")), encoding="utf-8")
    switcher.add_account(label="two", slot=2)
    (switcher.auth_dir / "account-1.json").write_text("{", encoding="utf-8")

    with pytest.raises(ConfigError, match="stored auth for Codex Account-1"):
        switcher.switch("one", json_output=False)

    assert json.loads(auth_path.read_text(encoding="utf-8"))["tokens"]["account_id"] == "acct-2"


def test_remove_account_keeps_live_auth_file(temp_home: Path):
    auth_path = _write_auth(temp_home, _auth("acct-1"))
    switcher = CodexAccountSwitcher()
    switcher.add_account(label="one", slot=1)

    switcher.remove_account("one")

    assert auth_path.exists()
    assert switcher.list_accounts(json_output=True)["accounts"] == []
    assert switcher.status(json_output=True)["active"] == {"managed": False}


def test_duplicate_label_rejected(temp_home: Path):
    auth_path = _write_auth(temp_home, _auth("acct-1"))
    switcher = CodexAccountSwitcher()
    switcher.add_account(label="same", slot=1)

    auth_path.write_text(json.dumps(_auth("acct-2")), encoding="utf-8")
    with pytest.raises(ValidationError, match="already exists"):
        switcher.add_account(label="same", slot=2)


def test_numeric_label_rejected(temp_home: Path):
    _write_auth(temp_home, _auth("acct-1"))
    switcher = CodexAccountSwitcher()

    with pytest.raises(ValidationError, match="cannot be only digits"):
        switcher.add_account(label="123", slot=None)


def test_duplicate_account_id_in_different_slot_rejected(temp_home: Path):
    _write_auth(temp_home, _auth("acct-1"))
    switcher = CodexAccountSwitcher()
    switcher.add_account(label="one", slot=1)

    with pytest.raises(ValidationError, match="already stored as Codex Account-1"):
        switcher.add_account(label="same-account", slot=2)


def test_corrupt_sequence_fails_before_overwrite(temp_home: Path):
    _write_auth(temp_home, _auth("acct-1"))
    switcher = CodexAccountSwitcher()
    switcher.codex_dir.mkdir(parents=True)
    switcher.sequence_file.write_text("{", encoding="utf-8")

    with pytest.raises(ConfigError, match="Codex state file is not valid JSON"):
        switcher.add_account(label="one", slot=None)

    assert switcher.sequence_file.read_text(encoding="utf-8") == "{"


def test_legacy_backup_migrated_before_codex_write(
    temp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(Platform, "detect", staticmethod(lambda: Platform.LINUX))
    legacy = temp_home / ".claude-swap-backup"
    legacy.mkdir()
    (legacy / "sequence.json").write_text(
        json.dumps({"activeAccountNumber": None, "sequence": [], "accounts": {}}),
        encoding="utf-8",
    )
    _write_auth(temp_home, _auth("acct-1"))

    switcher = CodexAccountSwitcher()
    switcher.add_account(label="one", slot=None)

    target = temp_home / ".local" / "share" / "claude-swap"
    assert not legacy.exists()
    assert (target / "codex" / "sequence.json").exists()


def test_unknown_switch_target_fails(temp_home: Path):
    _write_auth(temp_home, _auth("acct-1"))
    switcher = CodexAccountSwitcher()
    switcher.add_account(label="one", slot=1)

    with pytest.raises(AccountNotFoundError, match="No Codex account found"):
        switcher.switch("missing", json_output=False)


def test_api_key_auth_duplicate_across_slots_rejected(
    temp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """API-key auth (no account_id) must still be dedup-checked by fingerprint."""
    monkeypatch.setattr(
        "claude_swap.codex.fetch_codex_usage",
        lambda auth_text, timeout_s: "no access token",
    )
    _write_auth(temp_home, _api_key_auth("sk-1"))
    switcher = CodexAccountSwitcher()
    switcher.add_account(label="work", slot=1)

    with pytest.raises(ValidationError, match="already stored as Codex Account-1"):
        switcher.add_account(label="personal", slot=2)

    payload = switcher.list_accounts(json_output=True)
    assert [a["number"] for a in payload["accounts"]] == [1]


def test_fetch_codex_usage_429_carries_retry_after(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "claude_swap.codex.urllib.request.urlopen",
        _raise_urlopen(_http_error(429, "300")),
    )
    result = fetch_codex_usage(json.dumps(_auth("acct-1")), timeout_s=3.0)
    assert result == _UsageFetchError("HTTP 429", 300.0)


def test_fetch_codex_usage_401_is_token_expired(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "claude_swap.codex.urllib.request.urlopen",
        _raise_urlopen(_http_error(401, None)),
    )
    result = fetch_codex_usage(json.dumps(_auth("acct-1")), timeout_s=3.0)
    assert result == _UsageFetchError("token expired", None)


def test_fetch_codex_usage_500_has_no_retry_after(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "claude_swap.codex.urllib.request.urlopen",
        _raise_urlopen(_http_error(500, None)),
    )
    result = fetch_codex_usage(json.dumps(_auth("acct-1")), timeout_s=3.0)
    assert result == _UsageFetchError("HTTP 500", None)


def test_fetch_codex_usage_network_error_returns_string(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        "claude_swap.codex.urllib.request.urlopen",
        _raise_urlopen(urllib.error.URLError("boom")),
    )
    result = fetch_codex_usage(json.dumps(_auth("acct-1")), timeout_s=3.0)
    assert isinstance(result, str)
    assert "boom" in result


def test_fetch_codex_usage_non_json_body_is_malformed(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        "claude_swap.codex.urllib.request.urlopen", _body_urlopen(b"not json")
    )
    result = fetch_codex_usage(json.dumps(_auth("acct-1")), timeout_s=3.0)
    assert result == "malformed usage response"


def test_fetch_codex_usage_json_list_is_malformed(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "claude_swap.codex.urllib.request.urlopen", _body_urlopen(b"[]")
    )
    result = fetch_codex_usage(json.dumps(_auth("acct-1")), timeout_s=3.0)
    assert result == "malformed usage response"


def test_fetch_codex_usage_api_key_has_no_access_token():
    result = fetch_codex_usage(json.dumps(_api_key_auth("sk-1")), timeout_s=3.0)
    assert result == "no access token"


def test_fetch_usage_record_threads_retry_after(
    temp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    _write_auth(temp_home, _auth("acct-1"))
    switcher = CodexAccountSwitcher()
    switcher.add_account(label="one", slot=1)
    monkeypatch.setattr(
        "claude_swap.codex.fetch_codex_usage",
        lambda auth_text, timeout_s: _UsageFetchError("HTTP 429", 300.0),
    )

    record = switcher._fetch_usage_record("1")

    assert record.error == "HTTP 429"
    assert record.retry_after_s == 300.0


def test_switch_restores_auth_when_sequence_write_fails(
    temp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    auth_path = _write_auth(temp_home, _auth("acct-1"))
    switcher = CodexAccountSwitcher()
    switcher.add_account(label="one", slot=1)
    auth_path.write_text(json.dumps(_auth("acct-2")), encoding="utf-8")
    switcher.add_account(label="two", slot=2)

    def boom(path, data):
        raise ConfigError("sequence write failed")

    monkeypatch.setattr(switcher, "_write_json", boom)

    with pytest.raises(ConfigError, match="sequence write failed"):
        switcher.switch("1", json_output=False)

    assert json.loads(auth_path.read_text())["tokens"]["account_id"] == "acct-2"


def test_switch_unlinks_auth_when_no_prior_auth_and_sequence_write_fails(
    temp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    auth_path = _write_auth(temp_home, _auth("acct-1"))
    switcher = CodexAccountSwitcher()
    switcher.add_account(label="one", slot=1)
    auth_path.write_text(json.dumps(_auth("acct-2")), encoding="utf-8")
    switcher.add_account(label="two", slot=2)
    auth_path.unlink()

    def boom(path, data):
        raise ConfigError("sequence write failed")

    monkeypatch.setattr(switcher, "_write_json", boom)

    with pytest.raises(ConfigError, match="sequence write failed"):
        switcher.switch("1", json_output=False)

    assert not auth_path.exists()


def test_switch_with_no_active_auth_activates_target(temp_home: Path):
    auth_path = _write_auth(temp_home, _auth("acct-1"))
    switcher = CodexAccountSwitcher()
    switcher.add_account(label="one", slot=1)
    auth_path.write_text(json.dumps(_auth("acct-2")), encoding="utf-8")
    switcher.add_account(label="two", slot=2)
    auth_path.unlink()

    switcher.switch("1", json_output=False)

    assert json.loads(auth_path.read_text())["tokens"]["account_id"] == "acct-1"
    assert switcher.status(json_output=True)["active"]["number"] == 1


def test_rotation_falls_back_when_active_auth_unmanaged(temp_home: Path):
    auth_path = _write_auth(temp_home, _auth("acct-1"))
    switcher = CodexAccountSwitcher()
    switcher.add_account(label="one", slot=1)
    auth_path.write_text(json.dumps(_auth("acct-2")), encoding="utf-8")
    switcher.add_account(label="two", slot=2)
    # Live auth is an unmanaged account; activeAccountNumber (2) drives rotation.
    auth_path.write_text(json.dumps(_auth("acct-foreign")), encoding="utf-8")

    switcher.switch(None, json_output=False)

    assert json.loads(auth_path.read_text())["tokens"]["account_id"] == "acct-1"


def test_add_slot_collision_with_different_account_rejected(temp_home: Path):
    auth_path = _write_auth(temp_home, _auth("acct-1"))
    switcher = CodexAccountSwitcher()
    switcher.add_account(label="one", slot=1)
    auth_path.write_text(json.dumps(_auth("acct-2")), encoding="utf-8")

    with pytest.raises(ConfigError, match="already exists. Remove it first"):
        switcher.add_account(label="two", slot=1)

    stored = json.loads((switcher.auth_dir / "account-1.json").read_text())
    assert stored["tokens"]["account_id"] == "acct-1"


def test_api_key_auth_identified_by_fingerprint(
    temp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        "claude_swap.codex.fetch_codex_usage",
        lambda auth_text, timeout_s: "no access token",
    )
    _write_auth(temp_home, _api_key_auth("sk-1"))
    switcher = CodexAccountSwitcher()
    switcher.add_account(label=None, slot=None)

    payload = switcher.list_accounts(json_output=True)
    assert payload["accounts"][0]["label"] == "codex-account-1"
    assert switcher.status(json_output=True)["active"] == {
        "number": 1,
        "label": "codex-account-1",
        "managed": True,
    }


def test_remove_active_account_resets_active_number(temp_home: Path):
    auth_path = _write_auth(temp_home, _auth("acct-1"))
    switcher = CodexAccountSwitcher()
    switcher.add_account(label="one", slot=1)
    auth_path.write_text(json.dumps(_auth("acct-2")), encoding="utf-8")
    switcher.add_account(label="two", slot=2)

    switcher.remove_account("one")
    assert json.loads(switcher.sequence_file.read_text())["activeAccountNumber"] == 2

    switcher.remove_account("two")
    assert json.loads(switcher.sequence_file.read_text())["activeAccountNumber"] is None
