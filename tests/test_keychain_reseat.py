"""Claude Code's invalid_grant handler blanks the macOS Keychain item's tokens
while ``.credentials.json`` and the slot backup still hold the live login. The
active read serves the file, so nothing wrote it back and Claude Code (Keychain
first) stayed on the blank item. The poll re-seats the item, with no refresh
POST, at most once per generation.
"""
import json
import os
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from claude_swap import credentials, macos_keychain, oauth
from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher
from tests.test_autoswitch import EngineHarness

EMAIL = "a@example.com"
KEY = (credentials.CLAUDE_CODE_KEYCHAIN_SERVICE, macos_keychain.keychain_account_name())
IDS = {"accountUuid": "11111111-aaaa", "organizationUuid": "22222222-bbbb"}
FUTURE_MS = 99999999999000


def _login(refresh="rt-1", access="sk-1", expires=FUTURE_MS, **extra):
    return {
        "claudeAiOauth": {
            "accessToken": access, "refreshToken": refresh, "expiresAt": expires,
            "refreshTokenExpiresAt": 5_000, **extra,
        },
        "mcpOAuth": {"srv": {"accessToken": "stale"}},
    }


def _blank(keep_stamp=True):
    """What the handler leaves: both tokens empty, the machine-shared keys alive."""
    blank = _login(refresh="", access="", expires=0)
    blank["mcpOAuth"] = {"srv": {"accessToken": "live"}}
    if not keep_stamp:
        del blank["claudeAiOauth"]["refreshTokenExpiresAt"]
    return blank


def _reseated(file_login):
    """The blank item with ``claudeAiOauth`` replaced and its own siblings kept."""
    return {"claudeAiOauth": _drop_ids(file_login), "mcpOAuth": {"srv": {"accessToken": "live"}}}


def _drop_ids(login):
    return {k: v for k, v in login["claudeAiOauth"].items() if k not in IDS}


def _mac(temp_home: Path, kc, *, item=None, file=None, platform=Platform.MACOS):
    """Slot 1 is active; its backup and the plaintext file hold ``rt-1``."""
    s = ClaudeAccountSwitcher()
    s.platform = Platform.LINUX
    s._setup_directories()
    s._write_json(s.sequence_file, {
        "activeAccountNumber": 1, "lastUpdated": "2024-01-01T00:00:00Z", "sequence": [1],
        "accounts": {"1": {"email": EMAIL, "uuid": "uuid-1", "added": "2024-01-01T00:00:00Z"}},
    })
    s._get_claude_config_path().write_text(json.dumps(
        {"oauthAccount": {"emailAddress": EMAIL, "organizationUuid": ""}}), encoding="utf-8")
    s._write_account_credentials("1", EMAIL, json.dumps(_login()))
    live = temp_home / ".claude" / ".credentials.json"
    live.write_text(json.dumps(file or _login(**IDS)), encoding="utf-8")
    old = time.time() - 3600
    os.utime(live, (old, old))
    s.platform = platform
    if item is not None:
        kc.data[KEY] = json.dumps(item)
    return s, live


@pytest.fixture
def no_refresh(monkeypatch):
    """The only grant POST path: a re-seat must never reach it."""
    refresh = Mock()
    monkeypatch.setattr(oauth, "try_refresh_oauth_credentials", refresh)
    return refresh


def test_the_engine_poll_reseats_a_blank_keychain_item_from_the_file(
    temp_home: Path, block_real_keychain, no_refresh, monkeypatch,
):
    h = EngineHarness(temp_home)
    h.seed(1, EMAIL)
    h.seed(2, "b@example.com")
    h.make_live(EMAIL, 1)
    live = temp_home / ".claude" / ".credentials.json"
    live.write_text(json.dumps(_login(**IDS)), encoding="utf-8")  # rt-1: the slot's own lineage
    old = time.time() - 3600
    os.utime(live, (old, old))
    h.switcher.platform = Platform.MACOS
    block_real_keychain.data[KEY] = json.dumps(_blank())
    monkeypatch.setattr(h.switcher, "_run_usage_fetches", lambda *a, **k: {})
    builds = []
    build = h.switcher._build_accounts_info
    monkeypatch.setattr(
        h.switcher, "_build_accounts_info", lambda **kw: builds.append(1) or build(**kw))

    h.engine.tick()

    assert builds, "the tick never reached the pass that seats the item"
    assert json.loads(block_real_keychain.data[KEY]) == _reseated(_login())
    assert live.stat().st_mtime == pytest.approx(block_real_keychain.mtimes[KEY], abs=1e-3)
    assert json.loads(live.read_text(encoding="utf-8")) == _login(**IDS), "the file is never rewritten"
    no_refresh.assert_not_called()


def test_a_blank_item_that_lost_its_refresh_stamp_is_reseated_and_the_pass_runs_on_the_login(
    temp_home: Path, block_real_keychain, no_refresh,
):
    s, _live = _mac(temp_home, block_real_keychain, item=_blank(keep_stamp=False))

    row = s._build_accounts_info()[0]

    seated = block_real_keychain.data[KEY]
    assert json.loads(seated) == _reseated(_login())
    assert row[5] == seated
    no_refresh.assert_not_called()


@pytest.mark.parametrize(
    ("setup", "mode"),
    [
        ({"item": _login(refresh="rt-old", access="sk-old")}, "normal"),  # an older generation
        ({"item": None}, "normal"),
        ({"item": _blank(), "file": _login(refresh="rt-other")}, "normal"),
        ({"item": _blank(), "file": _login(expires=1)}, "normal"),
        ({"item": _blank(), "file": _login(access="")}, "normal"),
        ({"item": _blank(), "file": _login(), "platform": Platform.LINUX}, "normal"),  # no ids: no strip
        ({"item": _blank()}, "read_only"),
        ({"item": _blank()}, "dry_run"),
        ({"item": _blank()}, "custom_profile"),
    ],
    ids=["item-has-tokens", "absent-item", "other-lineage", "expired", "incomplete-pair",
         "linux", "read-only", "dry-run", "custom-profile"],
)
def test_a_pass_that_may_not_reseat_leaves_the_item_as_it_was(
    temp_home: Path, block_real_keychain, no_refresh, monkeypatch, setup, mode,
):
    if mode == "custom_profile":  # the unsuffixed item is another profile's login
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(temp_home / ".claude"))
    s, live = _mac(temp_home, block_real_keychain, **setup)
    before = dict(block_real_keychain.data)
    mtime = live.stat().st_mtime
    s._suppress_login_restore_write = mode == "dry_run"

    s._build_accounts_info(read_only=mode == "read_only")

    assert block_real_keychain.data == before
    assert live.stat().st_mtime == mtime
    no_refresh.assert_not_called()


def test_a_failed_keychain_write_leaves_the_item_blank_and_the_next_pass_retries(
    temp_home: Path, block_real_keychain, no_refresh, monkeypatch,
):
    s, live = _mac(temp_home, block_real_keychain, item=_blank())
    blank = block_real_keychain.data[KEY]
    mtime = live.stat().st_mtime
    real = macos_keychain.set_password

    def _refused(*_a):
        raise macos_keychain.KeychainError("no GUI session")

    monkeypatch.setattr(macos_keychain, "set_password", _refused)
    s._build_accounts_info()

    assert block_real_keychain.data[KEY] == blank and live.stat().st_mtime == mtime
    store = s._store
    assert store._keychain_usable_cache is not False and not store._pinned_to_oauth_file

    monkeypatch.setattr(macos_keychain, "set_password", real)
    s._build_accounts_info()

    assert json.loads(block_real_keychain.data[KEY]) == _reseated(_login())


def test_the_same_generation_blanked_again_is_not_reseated_but_the_next_one_is(
    temp_home: Path, block_real_keychain, no_refresh,
):
    s, live = _mac(temp_home, block_real_keychain, item=_blank())
    s._build_accounts_info()
    assert json.loads(block_real_keychain.data[KEY]) == _reseated(_login())

    block_real_keychain.data[KEY] = json.dumps(_blank())  # Claude Code blanks it again
    again = ClaudeAccountSwitcher()  # another process: no memory of the first re-seat
    again.platform = Platform.MACOS
    again._build_accounts_info()

    assert json.loads(block_real_keychain.data[KEY]) == _blank(), "a loop of re-seats"

    gen2 = _login(refresh="rt-2", access="sk-2")
    live.write_text(json.dumps(gen2), encoding="utf-8")
    again._store._write_backup_enc("1", EMAIL, json.dumps(gen2))
    again._build_accounts_info()

    assert json.loads(block_real_keychain.data[KEY]) == _reseated(gen2)
    no_refresh.assert_not_called()
