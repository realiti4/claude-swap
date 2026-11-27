"""Claude Code's invalid_grant handler blanks the macOS Keychain item's tokens
while ``.credentials.json`` and the slot backup still hold the live login. The
active read serves the file, so nothing wrote it back and Claude Code (Keychain
first) stayed on the blank item. The poll re-seats the item, with no refresh
POST, at most once per generation.
"""
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest

from claude_swap import credentials, macos_keychain, oauth
from claude_swap.exceptions import ClaudeCodeLockTimeout
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
    """The blank item with ``claudeAiOauth`` replaced (ids dropped), its own siblings kept."""
    login = {k: v for k, v in file_login["claudeAiOauth"].items() if k not in IDS}
    return {"claudeAiOauth": login, "mcpOAuth": {"srv": {"accessToken": "live"}}}


SLOTS = {1: (EMAIL, _login()), 2: ("b@example.com", _login(refresh="rt-2", access="sk-2"))}


def _activate(s, num):
    """Make ``num`` the active slot of the roster and of the live identity."""
    data = s._get_sequence_data()
    data["activeAccountNumber"] = num
    s._write_json(s.sequence_file, data)
    s._get_claude_config_path().write_text(json.dumps(
        {"oauthAccount": {"emailAddress": SLOTS[num][0], "organizationUuid": ""}}), encoding="utf-8")


def _mac(temp_home: Path, kc, *, item=None, file=None, platform=Platform.MACOS):
    """Slot 1 is active; its backup and the plaintext file hold ``rt-1``."""
    s = ClaudeAccountSwitcher()
    s.platform = Platform.LINUX
    s._setup_directories()
    s._write_json(s.sequence_file, {
        "activeAccountNumber": 1, "lastUpdated": "2024-01-01T00:00:00Z", "sequence": [1, 2],
        "accounts": {str(n): {"email": e, "uuid": f"uuid-{n}", "added": "2024-01-01T00:00:00Z"}
                     for n, (e, _) in SLOTS.items()},
    })
    _activate(s, 1)
    for n, (e, login) in SLOTS.items():
        s._write_account_credentials(str(n), e, json.dumps(login))
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
    (s.credentials_dir / ".reseated-1").write_bytes(b"\xff\xfe")  # a garbled record is no record

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
        ({"item": _blank()}, "roster_elsewhere"),
        ({"item": _blank()}, "quarantined"),
    ],
    ids=["item-has-tokens", "absent-item", "other-lineage", "expired", "incomplete-pair",
         "linux", "read-only", "dry-run", "custom-profile", "roster-elsewhere", "quarantined"],
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
    if mode == "roster_elsewhere":  # the live identity is slot 1, the roster names slot 2
        data = s._get_sequence_data()
        data["activeAccountNumber"] = 2
        s._write_json(s.sequence_file, data)
    if mode == "quarantined":  # the engine barred slot 1's generation (e.g. identity-conflict)
        fp = oauth.credential_fingerprint(json.dumps(SLOTS[1][1]))
        (s.backup_dir / "autoswitch_state.json").write_text(
            json.dumps({"quarantine": {"1": {"refreshTokenFingerprint": fp}}}), encoding="utf-8")

    s._build_accounts_info(read_only=mode == "read_only")

    assert block_real_keychain.data == before
    assert live.stat().st_mtime == mtime
    no_refresh.assert_not_called()


@pytest.mark.parametrize("record_unlink_denied", [False, True])
def test_a_failed_keychain_write_leaves_the_item_blank_and_the_next_pass_retries(
    temp_home: Path, block_real_keychain, no_refresh, monkeypatch, record_unlink_denied,
):
    s, live = _mac(temp_home, block_real_keychain, item=_blank())
    blank = block_real_keychain.data[KEY]
    mtime = live.stat().st_mtime
    real = macos_keychain.set_password
    outcomes = []
    monkeypatch.setattr(
        s._store, "_log_detected_login", lambda _c, *, outcome, **_k: outcomes.append(outcome))
    if record_unlink_denied:  # a record that cannot be removed must not keep the retry from the next pass
        real_unlink = Path.unlink

        def _deny(self, *a, **k):
            if self.name.startswith(".reseated-"):
                raise PermissionError("denied")
            return real_unlink(self, *a, **k)

        monkeypatch.setattr(Path, "unlink", _deny)

    def _refused(*_a):
        raise macos_keychain.KeychainError("no GUI session")

    monkeypatch.setattr(macos_keychain, "set_password", _refused)
    s._build_accounts_info()

    assert block_real_keychain.data[KEY] == blank and live.stat().st_mtime == mtime
    assert outcomes == ["blank Keychain login left: KeychainError"], "the Keychain error was masked"
    store = s._store
    assert store._keychain_usable_cache is not False and not store._pinned_to_oauth_file

    monkeypatch.setattr(macos_keychain, "set_password", real)
    s._build_accounts_info()

    assert json.loads(block_real_keychain.data[KEY]) == _reseated(_login())


@pytest.mark.parametrize("lands", ["item", "file", "held"])
def test_a_login_that_lands_before_the_locked_reread_is_never_overwritten(
    temp_home: Path, block_real_keychain, no_refresh, monkeypatch, lands,
):
    s, live = _mac(temp_home, block_real_keychain, item=_blank(), file=_login())  # no ids: no strip lock
    blank = block_real_keychain.data[KEY]
    landed = _login(refresh="rt-landed", access="sk-landed")
    entered = []

    @contextmanager
    def _login_lands(**_kw):  # between the pre-lock checks and the locked re-read
        entered.append(1)
        if lands == "held":
            raise ClaudeCodeLockTimeout("held")
        if lands == "item":
            block_real_keychain.data[KEY] = json.dumps(landed)
        else:
            live.write_text(json.dumps(landed), encoding="utf-8")
        yield

    monkeypatch.setattr(credentials, "claude_credentials_lock", _login_lands)

    s._build_accounts_info()

    assert entered == [1], "the login must land inside the re-seat's own lock entry"
    assert block_real_keychain.data[KEY] == (json.dumps(landed) if lands == "item" else blank)
    assert not (s.credentials_dir / ".reseated-1").exists()
    no_refresh.assert_not_called()


@pytest.mark.parametrize("verdict", ["degraded", "keychain_unavailable"])
def test_an_active_read_that_failed_never_reseats(
    temp_home: Path, block_real_keychain, no_refresh, verdict,
):
    s, live = _mac(temp_home, block_real_keychain, item=_blank())
    before = dict(block_real_keychain.data)
    creds = live.read_text(encoding="utf-8")
    active = credentials.ActiveCredentials(
        creds, verdict == "keychain_unavailable", verdict == "degraded")

    assert s._store._reseat_blank_keychain_login(active, "1", EMAIL, creds, lambda fp: False) == creds

    assert block_real_keychain.data == before
    assert not (s.credentials_dir / ".reseated-1").exists()


def test_the_whole_pass_on_the_seated_bytes_sends_a_usage_get_and_no_refresh_post(
    temp_home: Path, block_real_keychain, no_refresh, monkeypatch,
):
    s, _live = _mac(temp_home, block_real_keychain, item=_blank())
    sent = []

    def _answer(req, *_a, **_k):
        sent.append(req)
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({
            "five_hour": {"utilization": 10, "resets_at": "2099-01-01T00:00:00+00:00"},
            "seven_day": {"utilization": 20, "resets_at": "2099-01-01T00:00:00+00:00"},
        }).encode()
        return response

    monkeypatch.setattr(oauth.urllib.request, "urlopen", _answer)

    s._run_usage_fetches(s._build_accounts_info())

    assert json.loads(block_real_keychain.data[KEY]) == _reseated(_login())
    assert "Bearer sk-1" in {r.get_header("Authorization") for r in sent}, "no fetch ran on the seated login"
    assert {r.get_method() for r in sent} == {"GET"}, [(r.get_method(), r.full_url) for r in sent]
    no_refresh.assert_not_called()


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


def test_each_slot_keeps_its_own_record_of_the_generation_it_was_seated(
    temp_home: Path, block_real_keychain, no_refresh,
):
    s, live = _mac(temp_home, block_real_keychain, item=_blank())
    s._build_accounts_info()  # slot 1 seated
    for num in (2, 1):  # slot 2 seated; slot 1 blanked again after that
        block_real_keychain.data[KEY] = json.dumps(_blank())
        _activate(s, num)
        live.write_text(json.dumps(SLOTS[num][1]), encoding="utf-8")
        s._build_accounts_info()

    assert json.loads(block_real_keychain.data[KEY]) == _blank(), "slot 1's generation seated twice"
