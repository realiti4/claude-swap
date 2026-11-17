"""A live credential Claude Code wrote with `accountUuid` / `organizationUuid`
on `claudeAiOauth` names a second identity (it reads them ahead of
`~/.claude.json` `oauthAccount`). cswap that only OBSERVES such a login must
drop the two ids from the live store in the same pass, not at the next switch.
"""
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from claude_swap import credentials, macos_keychain
from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher

EMAIL = "a@example.com"
IDS = {"accountUuid": "11111111-aaaa", "organizationUuid": "22222222-bbbb"}


def _login(access="sk-a", **extra):
    oauth = {"accessToken": access, "refreshToken": "rt-a", "expiresAt": 99999999999000,
             "scopes": ["user:inference"], **extra}
    return {"claudeAiOauth": oauth, "mcpOAuth": {"srv": {"accessToken": "m"}}}


def _active_switcher(temp_home: Path, creds: dict):
    s = ClaudeAccountSwitcher()
    s.platform = Platform.LINUX
    s._setup_directories()
    s._write_json(s.sequence_file, {
        "activeAccountNumber": 1, "lastUpdated": "2024-01-01T00:00:00Z",
        "sequence": [1],
        "accounts": {"1": {"email": EMAIL, "uuid": "uuid-1", "added": "2024-01-01T00:00:00Z"}},
    })
    s._get_claude_config_path().write_text(json.dumps(
        {"oauthAccount": {"emailAddress": EMAIL, "organizationUuid": ""}}), encoding="utf-8")
    live = temp_home / ".claude" / ".credentials.json"
    live.write_text(json.dumps(creds), encoding="utf-8")
    return s, live


def test_a_pass_that_reads_stamped_ids_drops_them_from_the_live_store(temp_home: Path):
    stamped = _login(**IDS)
    s, live = _active_switcher(temp_home, stamped)

    row = s._build_accounts_info()[0]

    want = _login()  # the same login, only the two ids gone
    assert json.loads(live.read_text(encoding="utf-8")) == want
    assert json.loads(row[5]) == want, "the rest of the pass must run on the stripped bytes"
    assert not (temp_home / ".claude" / ".storage-write.lock").exists()


def test_a_pass_that_reads_no_ids_takes_no_lock_and_writes_nothing(temp_home: Path, monkeypatch):
    s, live = _active_switcher(temp_home, _login())
    before = live.read_text(encoding="utf-8")
    os.utime(live, (time.time() - 3600,) * 2)
    mtime = live.stat().st_mtime

    def _no_lock(**_kw):
        raise AssertionError("a pass with no ids must not take a lock")

    monkeypatch.setattr(credentials, "claude_credentials_lock", _no_lock)
    monkeypatch.setattr(credentials, "claude_storage_write_lock", _no_lock)

    s._build_accounts_info()

    assert live.read_text(encoding="utf-8") == before
    assert live.stat().st_mtime == mtime


def test_live_bytes_that_moved_before_the_locked_reread_are_not_overwritten(
    temp_home: Path, monkeypatch,
):
    s, live = _active_switcher(temp_home, _login(**IDS))
    landed = json.dumps(_login(access="sk-landed", **IDS))

    @contextmanager
    def _login_lands(**_kw):
        live.write_text(landed, encoding="utf-8")  # a /login between the read and the lock
        yield

    monkeypatch.setattr(credentials, "claude_storage_write_lock", _login_lands)

    row = s._build_accounts_info()[0]

    assert live.read_text(encoding="utf-8") == landed, "the stale strip clobbered a newer login"
    assert json.loads(row[5])["claudeAiOauth"]["accessToken"] == "sk-a"


def test_the_adopt_sync_writes_the_file_without_ids_and_still_stamps_the_mirror(
    temp_home: Path, monkeypatch,
):
    s, live = _active_switcher(temp_home, _login(access="sk-old"))
    s.platform = Platform.MACOS
    old = time.time() - 3600
    os.utime(live, (old, old))
    mdat = time.time() - 120
    monkeypatch.setattr(macos_keychain, "item_modified_at", lambda service, account: mdat)
    adopted = json.dumps(_login(access="sk-new", **IDS))

    s._store._sync_active_credentials_file_to_adopted_login(adopted, slot="1", email=EMAIL)

    assert json.loads(live.read_text(encoding="utf-8")) == _login(access="sk-new")
    assert live.stat().st_mtime == pytest.approx(mdat, abs=1), "the T1312 mdat stamp was skipped"

    writes = []
    monkeypatch.setattr(s._store, "_write_active_credentials_file", writes.append)
    s._store._sync_active_credentials_file_to_adopted_login(adopted, slot="1", email=EMAIL)
    assert writes == [], "an unchanged adopt must not rewrite the file"
