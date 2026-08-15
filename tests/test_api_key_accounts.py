"""Tests for managed API-key (``/login`` key) account support.

Covers kind detection, ``--add-token`` auto-detection, the cross-kind collision
guard, the ``add_account`` live-key guard, kind+platform-aware active credential
read/write with OAuth↔API-key mutual exclusion, the "API key — no quota" usage
display, the ``cswap run`` session guard, and export/import of raw keys.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from claude_swap import macos_keychain
from claude_swap import session as session_mod
from claude_swap.credentials import (
    CLAUDE_CODE_KEYCHAIN_SERVICE,
    CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE,
    approved_form,
    looks_like_api_key,
)
from unittest.mock import patch

from claude_swap.exceptions import (
    ClaudeSwitchError,
    CredentialWriteError,
    SessionError,
    SwitchError,
    ValidationError,
)
from claude_swap.json_output import USAGE_API_KEY, usage_fields
from claude_swap.models import Platform
from claude_swap.paths import get_credentials_path, get_global_config_path
from claude_swap.session import SessionManager
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.transfer import export_accounts, import_accounts

API_KEY = "sk-ant-api03-" + "a1b2c3d4e5" * 4  # 53 chars
OTHER_KEY = "sk-ant-api03-" + "z9y8x7w6v5" * 4
OAUTH_JSON = json.dumps(
    {"claudeAiOauth": {"accessToken": "tok", "refreshToken": "rtok", "expiresAt": 9}}
)


def _linux_switcher() -> ClaudeAccountSwitcher:
    s = ClaudeAccountSwitcher()
    s.platform = Platform.LINUX
    s._setup_directories()
    s._init_sequence_file()
    return s


def _macos_switcher() -> ClaudeAccountSwitcher:
    s = ClaudeAccountSwitcher()
    s.platform = Platform.MACOS
    s._setup_directories()
    s._init_sequence_file()
    return s


def _read_global_config() -> dict:
    return json.loads(get_global_config_path().read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Kind detection helpers
# ---------------------------------------------------------------------------


class TestKindDetection:
    def test_api_key_detected(self):
        assert looks_like_api_key(API_KEY) is True

    @pytest.mark.parametrize(
        "value",
        [
            "",
            None,
            "sk-ant-oat01-abcdef",  # setup-token, not a key
            OAUTH_JSON,  # OAuth JSON blob
            '{"x": "sk-ant-api03-inside-json"}',  # JSON that merely contains a key
        ],
    )
    def test_non_api_key(self, value):
        assert looks_like_api_key(value) is False

    def test_approved_form_is_last_20(self):
        assert approved_form(API_KEY) == API_KEY[-20:]
        assert len(approved_form(API_KEY)) == 20


# ---------------------------------------------------------------------------
# --add-token auto-detection
# ---------------------------------------------------------------------------


class TestAddTokenApiKey:
    def test_adds_api_key_account(self, temp_home: Path, capsys):
        s = _linux_switcher()
        s.add_account_from_token(API_KEY)

        assert s._account_kind("1") == "api_key"
        # default synthesized label
        data = s._get_sequence_data()
        assert data["accounts"]["1"]["email"] == "api-key-1@token.local"
        # the raw key is stored verbatim as the backup credential
        assert s._read_account_credentials("1", "api-key-1@token.local") == API_KEY
        out = capsys.readouterr().out
        assert "Added" in out and "API key" in out

    def test_setup_token_stays_oauth(self, temp_home: Path):
        s = _linux_switcher()
        s.add_account_from_token("sk-ant-oat01-abc")
        assert s._account_kind("1") == "oauth"
        email = s._get_sequence_data()["accounts"]["1"]["email"]
        assert email == "setup-token-1@token.local"
        blob = json.loads(s._read_account_credentials("1", email))
        assert blob["claudeAiOauth"]["accessToken"] == "sk-ant-oat01-abc"

    def test_refresh_in_place_same_api_key_account(self, temp_home: Path):
        s = _linux_switcher()
        s.add_account_from_token(API_KEY, email="me@example.com")
        s.add_account_from_token(OTHER_KEY, email="me@example.com")
        data = s._get_sequence_data()
        assert len(data["accounts"]) == 1
        assert s._read_account_credentials("1", "me@example.com") == OTHER_KEY


class TestCrossKindCollision:
    def test_api_key_rejected_when_email_is_oauth(self, temp_home: Path):
        s = _linux_switcher()
        s.add_account_from_token("sk-ant-oat01-abc", email="dup@example.com")
        with pytest.raises(ValidationError, match="already exists as an OAuth account"):
            s.add_account_from_token(API_KEY, email="dup@example.com")

    def test_oauth_rejected_when_email_is_api_key(self, temp_home: Path):
        s = _linux_switcher()
        s.add_account_from_token(API_KEY, email="dup@example.com")
        with pytest.raises(ValidationError, match="already exists as an API-key account"):
            s.add_account_from_token("sk-ant-oat01-abc", email="dup@example.com")


# ---------------------------------------------------------------------------
# Active credential read/write + mutual exclusion
# ---------------------------------------------------------------------------


class TestWriteCredentialsLinux:
    def test_activate_key_then_oauth(self, temp_home: Path):
        s = _linux_switcher()
        cred_file = get_credentials_path()
        cred_file.parent.mkdir(parents=True, exist_ok=True)
        cred_file.write_text(OAUTH_JSON, encoding="utf-8")

        # Activate the API key: primaryApiKey + approved set, OAuth file cleared.
        s._write_credentials(API_KEY)
        cfg = _read_global_config()
        assert cfg["primaryApiKey"] == API_KEY
        assert API_KEY[-20:] in cfg["customApiKeyResponses"]["approved"]
        assert not cred_file.exists()

        # Switch back to OAuth: file restored, primaryApiKey dropped, approved kept.
        s._write_credentials(OAUTH_JSON)
        assert cred_file.read_text(encoding="utf-8") == OAUTH_JSON
        cfg = _read_global_config()
        assert "primaryApiKey" not in cfg
        assert API_KEY[-20:] in cfg["customApiKeyResponses"]["approved"]

    def test_read_credentials_returns_active_key(self, temp_home: Path):
        s = _linux_switcher()
        get_global_config_path().write_text(
            json.dumps({"primaryApiKey": API_KEY}), encoding="utf-8"
        )
        assert s._read_credentials() == API_KEY

    def test_oauth_file_not_misread_as_key(self, temp_home: Path):
        s = _linux_switcher()
        cred_file = get_credentials_path()
        cred_file.parent.mkdir(parents=True, exist_ok=True)
        cred_file.write_text(OAUTH_JSON, encoding="utf-8")
        # primaryApiKey also present, but the OAuth file wins (read first).
        get_global_config_path().write_text(
            json.dumps({"primaryApiKey": API_KEY}), encoding="utf-8"
        )
        assert s._read_credentials() == OAUTH_JSON


class TestWriteCredentialsMacOS:
    def test_activate_key_uses_keychain_not_config(self, temp_home, block_real_keychain):
        store = block_real_keychain
        s = _macos_switcher()
        acct = macos_keychain.keychain_account_name()
        store.set_password(CLAUDE_CODE_KEYCHAIN_SERVICE, acct, OAUTH_JSON)

        s._write_credentials(API_KEY)

        # Key in the managed keychain service; OAuth keychain item cleared.
        assert store.get_password(CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE, acct) == API_KEY
        assert store.get_password(CLAUDE_CODE_KEYCHAIN_SERVICE, acct) is None
        # approved recorded, but the full key stays OUT of plaintext config.
        cfg = _read_global_config()
        assert API_KEY[-20:] in cfg["customApiKeyResponses"]["approved"]
        assert "primaryApiKey" not in cfg

    def test_switch_back_to_oauth_clears_key(self, temp_home, block_real_keychain):
        store = block_real_keychain
        s = _macos_switcher()
        s._write_credentials(API_KEY)
        acct = macos_keychain.keychain_account_name()
        assert store.get_password(CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE, acct) == API_KEY

        s._write_credentials(OAUTH_JSON)
        # managed keychain cleared, OAuth keychain populated, approved kept.
        assert store.get_password(CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE, acct) is None
        assert store.get_password(CLAUDE_CODE_KEYCHAIN_SERVICE, acct) == OAUTH_JSON
        cfg = _read_global_config()
        assert API_KEY[-20:] in cfg["customApiKeyResponses"]["approved"]

    def test_read_credentials_from_managed_keychain(self, temp_home, block_real_keychain):
        store = block_real_keychain
        s = _macos_switcher()
        acct = macos_keychain.keychain_account_name()
        store.set_password(CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE, acct, API_KEY)
        assert s._read_credentials() == API_KEY


# ---------------------------------------------------------------------------
# Usage display ("API key — no quota")
# ---------------------------------------------------------------------------


class TestUsageDisplay:
    def test_usage_fields_maps_api_key(self):
        assert usage_fields(USAGE_API_KEY) == ("api_key", None)

    def test_collect_usage_short_circuits(self, temp_home: Path):
        s = _linux_switcher()
        info = [(2, "api-key-2@token.local", "", "", False, API_KEY, "")]
        entries = s._collect_usage_entries(info)
        assert entries["2"].sentinel == USAGE_API_KEY
        assert entries["2"].decision_value() == USAGE_API_KEY

    def test_active_account_usage_short_circuits(self, temp_home: Path):
        s = _linux_switcher()
        get_global_config_path().write_text(
            json.dumps({"primaryApiKey": API_KEY}), encoding="utf-8"
        )
        entry = s._active_account_usage("2", "api-key-2@token.local", "")
        assert entry.sentinel == USAGE_API_KEY
        assert entry.decision_value() == USAGE_API_KEY


class TestStrategyBehaviour:
    """API-key accounts are never *rate-limited* (next-available can fall back to
    them), but `best` must NOT auto-prefer them — they have no measurable quota and
    jumping to one would silently spend paid per-token credits."""

    def test_api_key_headroom_is_unknown(self):
        # None headroom == "unknown" == never auto-skipped by next-available.
        from claude_swap import oauth

        assert oauth.account_headroom(USAGE_API_KEY) is None

    def test_best_does_not_jump_to_api_key_even_when_exhausted(
        self, temp_home: Path, monkeypatch
    ):
        s = _linux_switcher()
        s.add_account_from_token("sk-ant-oat01-x", slot=1)  # OAuth, switchable
        s.add_account_from_token(API_KEY, slot=2)  # API key, switchable
        # Current OAuth account (1) is fully exhausted; the only other account is
        # the no-quota API key. `best` must stay put rather than burn API credits.
        monkeypatch.setattr(
            s,
            "_usage_by_account",
            lambda: {"1": {"five_hour": {"pct": 100.0}}, "2": USAGE_API_KEY},
        )
        target, _ = s._select_best_switchable("1")
        assert target is None


# ---------------------------------------------------------------------------
# add_account guard against capturing a live API-key login
# ---------------------------------------------------------------------------


class TestAddAccountGuard:
    def test_rejects_live_api_key_login(self, temp_home: Path):
        s = _linux_switcher()
        # Lingering oauthAccount identity + an active managed key in config.
        get_global_config_path().write_text(
            json.dumps(
                {
                    "oauthAccount": {"emailAddress": "stale@example.com"},
                    "primaryApiKey": API_KEY,
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ValidationError, match="Active login is an API-key account"):
            s.add_account()


# ---------------------------------------------------------------------------
# Session-mode guard
# ---------------------------------------------------------------------------


class TestSessionGuard:
    def _seed_api_key_account(self) -> ClaudeAccountSwitcher:
        s = _linux_switcher()
        s.add_account_from_token(API_KEY, slot=2)
        return s

    def test_setup_session_rejects(self, temp_home: Path):
        mgr = SessionManager(self._seed_api_key_account())
        with pytest.raises(SessionError, match="does not support API-key accounts"):
            mgr.setup_session("2", share=True)

    def test_run_rejects_before_exec(self, temp_home: Path, monkeypatch):
        mgr = SessionManager(self._seed_api_key_account())
        monkeypatch.setattr(session_mod.shutil, "which", lambda name: "/fake/claude")
        with pytest.raises(SessionError, match="does not support API-key accounts"):
            mgr.run("2", [], share=True)


# ---------------------------------------------------------------------------
# Export / import of raw keys
# ---------------------------------------------------------------------------


class TestExportImport:
    def test_round_trip_preserves_key_and_kind(self, tmp_path: Path):
        src_home = tmp_path / "src"
        (src_home / ".claude").mkdir(parents=True)
        with _patched_home(src_home):
            src = _linux_switcher()
            src.add_account_from_token(API_KEY, slot=1)
            out = tmp_path / "b.cswap"
            export_accounts(src, str(out))
            payload = json.loads(out.read_text(encoding="utf-8"))
            # exported as a raw string, tagged api_key — not a JSON object.
            assert payload["accounts"][0]["credentials"] == API_KEY
            assert payload["accounts"][0]["kind"] == "api_key"

        dst_home = tmp_path / "dst"
        (dst_home / ".claude").mkdir(parents=True)
        with _patched_home(dst_home):
            dst = _linux_switcher()
            import_accounts(dst, str(out))
            assert dst._account_kind("1") == "api_key"
            assert dst._read_account_credentials("1", "api-key-1@token.local") == API_KEY


class _patched_home:
    """Redirect HOME/Path.home() to ``home`` for export/import on two homes."""

    def __init__(self, home: Path):
        self.home = home
        self._patches: list = []

    def __enter__(self):
        import os
        from unittest.mock import patch

        self._patches = [
            patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)}),
            patch("pathlib.Path.home", return_value=self.home),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


class TestAnUnreadableGlobalConfigIsNotAnEmptyOne:
    """``~/.claude.json`` carries the user's whole Claude Code state.

    ``_read_global_config`` answers ``None`` for two different facts — the file
    is ABSENT, and the file is THERE but cannot be parsed. ``or {}`` at the
    write collapses them, and the atomic replace then writes that ``{}`` over
    a file it never read. Absent is a genuine empty start; unreadable is the
    same value/None conflation this branch exists to close, on the highest-
    value file in the install.
    """

    def test_a_torn_config_is_not_overwritten_with_an_empty_one(
        self, temp_home: Path
    ):
        """A crash mid-write leaves valid-prefix JSON. It must not be erased.

        No keychain and no patched reader — a truncated file is what the OS
        leaves behind, and it is the whole premise. Asserts on the keys the
        user would LOSE rather than on the exception type: the point is the
        data, and a refusal that still wrote ``{}`` would pass a type check.
        """
        s = _linux_switcher()
        cfg = get_global_config_path()
        real = {
            "oauthAccount": {"emailAddress": "me@example.com"},
            "projects": {"/a": {"allowedTools": []}},
            "mcpServers": {"x": {"command": "y"}},
        }
        cfg.write_text(json.dumps(real, indent=2)[:-12], encoding="utf-8")
        with pytest.raises(json.JSONDecodeError):
            json.loads(cfg.read_text(encoding="utf-8"))

        with pytest.raises(CredentialWriteError):
            s._store._update_global_config(
                lambda d: d.__setitem__("primaryApiKey", API_KEY)
            )

        assert cfg.read_text(encoding="utf-8") == json.dumps(real, indent=2)[:-12], (
            "the torn file was rewritten; the user's config is gone"
        )

    def test_an_absent_config_still_writes(self, temp_home: Path):
        """The other half of the same predicate: absent is a real empty start.

        Without this, the refusal above would be indistinguishable from
        breaking first-run: a box with no ``~/.claude.json`` must still be
        able to record a key.
        """
        s = _linux_switcher()
        cfg = get_global_config_path()
        if cfg.exists():
            cfg.unlink()

        s._store._update_global_config(
            lambda d: d.__setitem__("primaryApiKey", API_KEY)
        )
        assert json.loads(cfg.read_text(encoding="utf-8"))["primaryApiKey"] == API_KEY

    @pytest.mark.skipif(
        sys.platform == "win32" or os.geteuid() == 0,
        reason="needs POSIX permission semantics (non-root)",
    )
    def test_clear_managed_key_does_not_silently_skip_on_unreadable_config(
        self, temp_home: Path, caplog
    ):
        """I-2: ``_clear_managed_key`` reads through the plain (non-strict)
        ``_read_global_config``, which collapses ABSENT and UNREADABLE into
        the same ``None``. ``if cfg is not None and cfg.get("primaryApiKey")
        is not None:`` then treats an unreadable config exactly like one
        that never had a key -- it skips the clear AND returns normally, so
        the caller (``_write_credentials``, on an OAuth activation) sees no
        error. A stale ``primaryApiKey`` survives in a file that becomes
        readable again moments later, and Claude Code authenticates with a
        live cross-account key that bills per token while it lies.

        The fix must not raise (this stays best-effort, matching the
        Keychain-delete arm two lines above), but it must not look
        IDENTICAL in the logs to "there was no key to clear" either.
        """
        import logging

        s = _linux_switcher()
        cfg = get_global_config_path()
        cfg.write_text(json.dumps({"primaryApiKey": API_KEY}), encoding="utf-8")
        cfg.chmod(0o000)
        try:
            caplog.clear()
            with caplog.at_level(logging.WARNING, logger="claude-swap"):
                s._store._clear_managed_key()  # must not raise
        finally:
            cfg.chmod(0o600)

        assert json.loads(cfg.read_text(encoding="utf-8"))["primaryApiKey"] == API_KEY, (
            "premise: the unreadable config's primaryApiKey must survive "
            "untouched (never overwrite a file we could not read)"
        )
        assert any(
            "unreadable" in r.message.lower() or "could not be read" in r.message.lower()
            for r in caplog.records
        ), (
            "DEFECT: an unreadable global config at clear-time produced no "
            "warning distinguishing it from a genuinely keyless profile -- "
            "the caller cannot tell 'nothing to clear' from 'could not check'"
        )

    def test_clear_managed_key_control_absent_config_is_a_true_no_op(
        self, temp_home: Path, caplog
    ):
        """CONTROL (opposite direction): a genuinely absent config must stay
        a silent no-op -- no warning, no config file materialized."""
        import logging

        s = _linux_switcher()
        cfg = get_global_config_path()
        if cfg.exists():
            cfg.unlink()

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            s._store._clear_managed_key()

        assert not cfg.exists(), "a genuinely absent config must not be created"
        assert not any(
            "unreadable" in r.message.lower() or "could not be read" in r.message.lower()
            for r in caplog.records
        ), "CONTROL FAILED: a genuinely absent config must not warn"


class TestATornConfigSurvivesAnOrdinarySwitch:
    """The write path was guarded; the READ path one function over was not.

    `_update_global_config` now refuses on an unreadable config. The switch
    itself reads `~/.claude.json` through `_read_json`, which answers None for
    ABSENT and for TORN alike — so the `if existing_config:` splice falls to
    its else branch and writes the 1-key backup config over the user's whole
    file, reporting `switched: True`.

    Strictly worse than the bug the refusal closed: it needs no API-key slot,
    it is what a plain `cswap switch` does, and the success line is what makes
    it invisible.
    """

    def test_a_plain_switch_does_not_flatten_a_torn_config(
        self, temp_home: Path
    ):
        """Asserts on the KEYS the user keeps, not on the exception.

        A refusal that still truncated the file would satisfy `pytest.raises`;
        only the surviving keys answer the question the user cares about.
        """
        s = _linux_switcher()
        for num, email in ((1, "a@example.com"), (2, "b@example.com")):
            s._write_account_credentials(str(num), email, OAUTH_JSON)
            s._write_account_config(str(num), email, json.dumps({
                "oauthAccount": {"emailAddress": email,
                                 "accountUuid": f"uuid-{num}"}}))
        data = s._get_sequence_data() or {
            "activeAccountNumber": None, "lastUpdated": "",
            "sequence": [], "accounts": {},
        }
        for num, email in ((1, "a@example.com"), (2, "b@example.com")):
            data["accounts"][str(num)] = {
                "email": email, "uuid": f"uuid-{num}",
                "organizationUuid": "", "organizationName": "",
                "added": "2024-01-01T00:00:00Z",
            }
            if num not in data["sequence"]:
                data["sequence"].append(num)
        data["sequence"].sort()
        data["activeAccountNumber"] = 1
        s._write_json(s.sequence_file, data)

        cfg = get_global_config_path()
        real = {
            "oauthAccount": {"emailAddress": "a@example.com",
                             "accountUuid": "uuid-1"},
            "projects": {"/work": {"allowedTools": []}},
            "mcpServers": {"x": {"command": "y"}},
            "userID": "uid-123",
        }
        cfg.write_text(json.dumps(real, indent=2)[:-14], encoding="utf-8")
        with pytest.raises(json.JSONDecodeError):
            json.loads(cfg.read_text(encoding="utf-8"))

        torn_bytes = cfg.read_text(encoding="utf-8")
        s.switch_to("2", json_output=True)

        # The switch lands — upstream replaces a malformed config here on
        # purpose. What must not happen is the bytes being GONE.
        salvage = [
            p for p in cfg.parent.iterdir()
            if p.name.startswith(f"{cfg.name}.unreadable-")
        ]
        assert len(salvage) == 1, (
            f"no salvage copy beside {cfg}; the torn config was replaced and "
            "projects/mcpServers/userID are unrecoverable"
        )
        assert salvage[0].read_text(encoding="utf-8") == torn_bytes


    def test_config_torn_between_the_switch_start_and_step_4_survives(
        self, temp_home: Path
    ):
        """I1: the normal-switch branch's OWN re-read at step 4, not the
        pre-existing torn-at-the-start case above.

        A config that is ALREADY torn when the switch starts makes
        ``_get_current_account()`` return None, which routes the whole
        switch through the direct-activation branch (:6148-6165) — which
        already carries the ``is not None`` guard, the salvage copy, and the
        warning (see the class above). The branch this test targets is only
        reached when the config is READABLE at the start (so
        ``current_identity`` resolves and the normal branch is taken) and
        THEN tears before step 4's own re-read — e.g. a concurrent writer,
        or a crash mid-write racing the switch. Step 4 reads the file a
        SECOND time (``current_config_data = self._read_json(config_path)``
        at :6428) rather than reusing the earlier snapshot, so this window
        is real, not contrived.

        Before the fix this raised a raw
        ``'NoneType' object does not support item assignment`` (from
        ``current_config_data["oauthAccount"] = ...`` on a None) with no
        salvage copy — the user's torn config was gone for good, worse than
        the bug the direct-activation branch's guard already closed.
        """
        s = _linux_switcher()
        for num, email in ((1, "a@example.com"), (2, "b@example.com")):
            s._write_account_credentials(str(num), email, OAUTH_JSON)
            s._write_account_config(str(num), email, json.dumps({
                "oauthAccount": {"emailAddress": email,
                                 "accountUuid": f"uuid-{num}"}}))
        data = s._get_sequence_data() or {
            "activeAccountNumber": None, "lastUpdated": "",
            "sequence": [], "accounts": {},
        }
        for num, email in ((1, "a@example.com"), (2, "b@example.com")):
            data["accounts"][str(num)] = {
                "email": email, "uuid": f"uuid-{num}",
                "organizationUuid": "", "organizationName": "",
                "added": "2024-01-01T00:00:00Z",
            }
            if num not in data["sequence"]:
                data["sequence"].append(num)
        data["sequence"].sort()
        data["activeAccountNumber"] = 1
        s._write_json(s.sequence_file, data)

        cfg = get_global_config_path()
        real = {
            "oauthAccount": {"emailAddress": "a@example.com",
                             "accountUuid": "uuid-1"},
            "projects": {"/work": {"allowedTools": []}},
            "mcpServers": {"x": {"command": "y"}},
            "userID": "uid-123",
        }
        cfg.write_text(json.dumps(real), encoding="utf-8")
        get_credentials_path().parent.mkdir(parents=True, exist_ok=True)
        get_credentials_path().write_text(OAUTH_JSON, encoding="utf-8")

        # CONTROL: an ordinary switch with the config intact the whole way
        # through lands cleanly, with no salvage — proves the instrument
        # says "fine" on the healthy case, not just "broken" on the torn one.
        out_control = s.switch_to("2", json_output=True)
        assert out_control["switched"] is True
        assert out_control["warnings"] == []
        no_salvage = [
            p for p in cfg.parent.iterdir()
            if p.name.startswith(f"{cfg.name}.unreadable-")
        ]
        assert no_salvage == [], "control: a clean switch must not salvage"

        # Reset back to account 1's identity directly (bypassing another
        # switch, whose own reads would consume the patched call count
        # below), then reproduce the race: the config reads fine when
        # `_get_current_account()` resolves identity, and only tears by the
        # time step 4 re-reads it.
        real["oauthAccount"] = {"emailAddress": "a@example.com",
                                 "accountUuid": "uuid-1"}
        cfg.write_text(json.dumps(real), encoding="utf-8")
        data["activeAccountNumber"] = 1
        s._write_json(s.sequence_file, data)

        orig_read_json = type(s)._read_json
        calls = {"n": 0}

        def read_json_that_tears_at_step_4(self, path, **kw):
            if path == cfg:
                calls["n"] += 1
                if calls["n"] == 4:
                    # Simulates the file being torn/unreadable at exactly
                    # this re-read — `_read_json` already answers None for
                    # both ABSENT and UNREADABLE, so returning None here
                    # reproduces the real failure mode without needing a
                    # second on-disk write mid-call.
                    return None
            return orig_read_json(self, path, **kw)

        with patch.object(
            type(s), "_read_json", read_json_that_tears_at_step_4
        ):
            out = s.switch_to("2", json_output=True)

        assert out["switched"] is True, (
            "the switch must still land — a torn config must not abort a "
            "switch that upstream would otherwise complete"
        )
        assert out["warnings"], (
            "no warning surfaced — the user is not told their config could "
            "not be parsed"
        )
        salvage = [
            p for p in cfg.parent.iterdir()
            if p.name.startswith(f"{cfg.name}.unreadable-")
        ]
        assert len(salvage) == 1, (
            f"no salvage copy beside {cfg} after step 4's own re-read saw a "
            "torn file — the user's config bytes are unrecoverable"
        )

    def test_valid_empty_config_at_step_4_is_spliced_not_salvaged(
        self, temp_home: Path
    ):
        """I1 sibling to M-2 (``test_a_valid_empty_config_is_spliced_not_
        called_unparseable`` in test_transfer.py, which covers the
        DIRECT-ACTIVATION branch only): the normal-switch branch's OWN
        ``is not None`` check at step 4 needs the same control.

        A valid but EMPTY ``{}`` config is readable and loses nothing by
        being spliced. Under a truthiness test it falls to the salvage
        branch, copies the file aside, and tells the user their config
        "could not be parsed" — wrong, since it parsed fine and was simply
        empty. This is the control in the OTHER direction from the torn-
        config test above: proves the guard says "fine, splice it" on a
        readable-but-empty file, not just "salvage" on a torn one.
        """
        s = _linux_switcher()
        for num, email in ((1, "a@example.com"), (2, "b@example.com")):
            s._write_account_credentials(str(num), email, OAUTH_JSON)
            s._write_account_config(str(num), email, json.dumps({
                "oauthAccount": {"emailAddress": email,
                                 "accountUuid": f"uuid-{num}"}}))
        data = s._get_sequence_data() or {
            "activeAccountNumber": None, "lastUpdated": "",
            "sequence": [], "accounts": {},
        }
        for num, email in ((1, "a@example.com"), (2, "b@example.com")):
            data["accounts"][str(num)] = {
                "email": email, "uuid": f"uuid-{num}",
                "organizationUuid": "", "organizationName": "",
                "added": "2024-01-01T00:00:00Z",
            }
            if num not in data["sequence"]:
                data["sequence"].append(num)
        data["sequence"].sort()
        data["activeAccountNumber"] = 1
        s._write_json(s.sequence_file, data)

        cfg = get_global_config_path()
        real = {"oauthAccount": {"emailAddress": "a@example.com",
                                  "accountUuid": "uuid-1"}}
        cfg.write_text(json.dumps(real), encoding="utf-8")
        get_credentials_path().parent.mkdir(parents=True, exist_ok=True)
        get_credentials_path().write_text(OAUTH_JSON, encoding="utf-8")

        orig_read_json = type(s)._read_json
        calls = {"n": 0}

        def read_json_empty_at_step_4(self, path, **kw):
            if path == cfg:
                calls["n"] += 1
                if calls["n"] == 4:
                    # A valid, empty, PARSED config — not a read failure.
                    return {}
            return orig_read_json(self, path, **kw)

        with patch.object(type(s), "_read_json", read_json_empty_at_step_4):
            out = s.switch_to("2", json_output=True)

        assert out["switched"] is True
        assert out["warnings"] == [], (
            "an empty-but-valid config triggered a salvage warning — "
            "it was never unreadable"
        )
        salvage = [
            p for p in cfg.parent.iterdir()
            if p.name.startswith(f"{cfg.name}.unreadable-")
        ]
        assert salvage == [], (
            "a valid empty {} config was salvaged and reported as "
            "unparseable — is-not-None was tested as truthiness"
        )
        assert json.loads(cfg.read_text(encoding="utf-8"))["oauthAccount"][
            "emailAddress"
        ] == "b@example.com"

    def test_a_failed_salvage_aborts_instead_of_flattening_the_config(
        self, temp_home: Path
    ):
        """M-1: the abort is the salvage's whole license to proceed.

        `_salvage_unreadable` turns an OSError into SwitchError precisely so
        the switch stops before `_write_json` replaces the torn config. Its
        four sibling promises (mode, collision, name, visibility) each have a
        test; the abort did not. Swallowing it would replace the user's file
        with the 1-key backup config and still return `switched: True` — the
        exact invisible data loss the salvage exists to prevent, with the
        guard that prevents it deleted.
        """
        s = _linux_switcher()
        for num, email in ((1, "a@example.com"), (2, "b@example.com")):
            s._write_account_credentials(str(num), email, OAUTH_JSON)
            s._write_account_config(str(num), email, json.dumps({
                "oauthAccount": {"emailAddress": email,
                                 "accountUuid": f"uuid-{num}"}}))
        data = s._get_sequence_data() or {
            "activeAccountNumber": None, "lastUpdated": "",
            "sequence": [], "accounts": {},
        }
        for num, email in ((1, "a@example.com"), (2, "b@example.com")):
            data["accounts"][str(num)] = {
                "email": email, "uuid": f"uuid-{num}",
                "organizationUuid": "", "organizationName": "",
                "added": "2024-01-01T00:00:00Z",
            }
            if num not in data["sequence"]:
                data["sequence"].append(num)
        data["sequence"].sort()
        data["activeAccountNumber"] = 1
        s._write_json(s.sequence_file, data)

        cfg = get_global_config_path()
        real = {
            "oauthAccount": {"emailAddress": "a@example.com",
                             "accountUuid": "uuid-1"},
            "projects": {"/work": {"allowedTools": []}},
            "mcpServers": {"x": {"command": "y"}},
            "userID": "uid-123",
        }
        cfg.write_text(json.dumps(real, indent=2)[:-14], encoding="utf-8")
        torn_bytes = cfg.read_text(encoding="utf-8")

        # The salvage copy cannot be written (ENOSPC, a read-only dir, ...).
        def no_space(*_a, **_kw):
            raise OSError(28, "No space left on device")

        with patch("claude_swap.switcher.shutil.copy", side_effect=no_space):
            with pytest.raises(SwitchError):
                s.switch_to("2", json_output=True)

        assert cfg.read_text(encoding="utf-8") == torn_bytes, (
            "the salvage failed and the switch replaced the config anyway — "
            "projects/mcpServers/userID are unrecoverable"
        )


class TestATornRosterDoesNotDestroyBackups:
    """`_get_sequence_data` answers None for ABSENT and for TORN alike.

    27 sites write the result back through `or {}`, so a torn `sequence.json`
    read as "no accounts" and the next write rebuilt the roster from nothing.
    """

    def test_add_account_refuses_instead_of_overwriting_a_live_backup(
        self, temp_home: Path
    ):
        """Measured: the backup was destroyed BEFORE the crash, not by it.

        `_get_next_account_number` collapsed to 1, `_write_account_credentials`
        landed at switcher.py:2934, and only THEN did `data["accounts"][num]`
        raise `TypeError` at :2941 — which `cli.py`'s `except
        ClaudeSwitchError` does not catch, so `--json` emitted no envelope.

            before  sha256:296e3
            raised  TypeError    is_ClaudeSwitchError=False
            after   sha256:6aabc

        Asserts the BACKUP survives, not the exception type: a refusal that
        still overwrote it would satisfy `pytest.raises` and lose the token.
        """
        s = _linux_switcher()
        s._write_account_credentials("1", "shared@example.com", OAUTH_JSON)
        data = s._get_sequence_data() or {
            "activeAccountNumber": None, "lastUpdated": "",
            "sequence": [], "accounts": {},
        }
        data["accounts"]["1"] = {
            "email": "shared@example.com", "uuid": "u1",
            "organizationUuid": "org-A", "organizationName": "A",
            "added": "2024-01-01T00:00:00Z",
        }
        data["sequence"] = [1]
        data["activeAccountNumber"] = 1
        s._write_json(s.sequence_file, data)
        before = s._read_account_credentials("1", "shared@example.com")
        assert before, "premise: slot 1 has a credential backup"

        s.sequence_file.write_text(
            s.sequence_file.read_text(encoding="utf-8")[:-12], encoding="utf-8"
        )
        with pytest.raises(json.JSONDecodeError):
            json.loads(s.sequence_file.read_text(encoding="utf-8"))

        # A live login for the SAME email under a different org — what makes
        # the collapsed slot number land on top of the resident one.
        get_credentials_path().parent.mkdir(parents=True, exist_ok=True)
        get_credentials_path().write_text(OAUTH_JSON, encoding="utf-8")
        get_global_config_path().write_text(json.dumps({
            "oauthAccount": {"emailAddress": "shared@example.com",
                             "accountUuid": "u2",
                             "organizationUuid": "org-B"},
        }), encoding="utf-8")

        with pytest.raises(ClaudeSwitchError):
            s.add_account()

        assert s._read_account_credentials("1", "shared@example.com") == before, (
            "slot 1's credential backup was overwritten before the failure; "
            "the torn roster read as an empty one"
        )


class TestTheSalvageKeepsItsPromise:
    """"The bytes survive and the user knows" — three ways it did not."""

    def _torn(self, s):
        cfg = get_global_config_path()
        real = {
            "oauthAccount": {"emailAddress": "a@example.com",
                             "accountUuid": "uuid-1"},
            "projects": {"/work": {"allowedTools": []}},
            "primaryApiKey": API_KEY,
        }
        cfg.write_text(json.dumps(real, indent=2)[:-14], encoding="utf-8")
        return cfg

    def _seed_two(self, s):
        for num, email in ((1, "a@example.com"), (2, "b@example.com")):
            s._write_account_credentials(str(num), email, OAUTH_JSON)
            s._write_account_config(str(num), email, json.dumps({
                "oauthAccount": {"emailAddress": email,
                                 "accountUuid": f"uuid-{num}"}}))
        data = s._get_sequence_data() or {
            "activeAccountNumber": None, "lastUpdated": "",
            "sequence": [], "accounts": {},
        }
        for num, email in ((1, "a@example.com"), (2, "b@example.com")):
            data["accounts"][str(num)] = {
                "email": email, "uuid": f"uuid-{num}",
                "organizationUuid": "", "organizationName": "",
                "added": "2024-01-01T00:00:00Z",
            }
            if num not in data["sequence"]:
                data["sequence"].append(num)
        data["sequence"].sort()
        data["activeAccountNumber"] = 1
        s._write_json(s.sequence_file, data)

    @pytest.mark.skipif(
        sys.platform == "win32", reason="File permissions work differently on Windows"
    )
    def test_the_salvage_does_not_widen_the_config_mode(self, temp_home: Path):
        """`copy2` preserves the SOURCE mode, and the source is often 0644.

        Measured before this: the replacement got 0600 from `_write_json` while
        the salvage kept 0644 and held `primaryApiKey` — cswap created a
        world-readable copy of the user's secret. Asserts the MODE, because a
        salvage that exists and leaks is worse than none.

        POSIX only, matching the six sibling mode assertions in this suite
        (test_session, test_switcher, test_settings, test_transfer,
        test_swap_accounts, test_paths). `_salvage_unreadable` skips the chmod
        off POSIX for the same reason those skip the assertion: Windows has no
        mode bits to set, and CI reported the salvage at 0o666.
        """
        s = _linux_switcher()
        self._seed_two(s)
        cfg = self._torn(s)
        cfg.chmod(0o644)

        s.switch_to("2", json_output=True)

        salvage = [
            q for q in cfg.parent.iterdir()
            if q.name.startswith(f"{cfg.name}.unreadable-")
        ]
        assert len(salvage) == 1
        # The torn file is truncated mid-key, so the FULL key is not in it.
        # Match the prefix instead: what matters is that secret material is
        # present, not that it survived intact.
        assert "sk-ant-api03-" in salvage[0].read_text(encoding="utf-8"), (
            "premise: the salvage holds secret material"
        )
        assert salvage[0].stat().st_mode & 0o777 == 0o600, (
            f"salvage is {oct(salvage[0].stat().st_mode & 0o777)} — the secret "
            "is readable by anyone on the box"
        )

    def test_the_salvage_name_is_creatable_on_every_supported_platform(
        self, temp_home: Path
    ):
        """A salvage that cannot be CREATED saves nothing.

        `get_timestamp()` renders `2026-08-03T00:26:55Z`, and `:` is forbidden
        in a Windows filename. Measured on CI (run 30774451162, job
        test-windows): five tests died with `SwitchError: ... the salvage copy
        failed ([Errno 22] Invalid argument:
        '...\\.claude.json.unreadable-2026-08-03T00:25:49Z')`. The copy raised,
        the guard re-raised, and the switch ABORTED — so on Windows this branch
        turned "your config was replaced but a copy was kept" into "the switch
        will not run at all", which is strictly worse than the bug it fixes.

        Asserts the NAME, not that the copy succeeds: `:` is a perfectly good
        filename character on Linux, so a behavioural test is green here and
        red only on the platform nobody runs locally. The forbidden set is
        Windows', a superset of POSIX's — a name legal there is legal
        everywhere cswap runs.

        The codebase already had two answers and neither was reused:
        `credentials.py:1372`'s `.corrupt-{int(time.time())}` and
        `session.py:145`'s `slugify_email`. This asserts the property both
        satisfy, so a third format cannot be invented without a test noticing.
        """
        s = _linux_switcher()
        self._seed_two(s)
        cfg = self._torn(s)

        s.switch_to("2", json_output=True)

        salvage = [
            q for q in cfg.parent.iterdir()
            if q.name.startswith(f"{cfg.name}.unreadable-")
        ]
        assert len(salvage) == 1, "premise: a salvage was made"
        bad = set(salvage[0].name) & set('<>:"/\\|?*')
        assert not bad, (
            f"salvage name {salvage[0].name!r} holds {sorted(bad)}, which "
            "Windows refuses — the copy raises OSError and the switch aborts"
        )

    def test_a_second_failure_does_not_overwrite_the_first_salvage(
        self, temp_home: Path
    ):
        """`get_timestamp()` is second-resolution and `copy2` overwrites.

        Two failed switches inside one second left ONE file, so the FIRST
        user's data was unrecoverable — and a retry is exactly what a user does
        next. Both copies must survive.
        """
        s = _linux_switcher()
        self._seed_two(s)
        cfg = self._torn(s)
        first_bytes = cfg.read_text(encoding="utf-8")

        s.switch_to("2", json_output=True)
        cfg.write_text(json.dumps({"second": True})[:-2], encoding="utf-8")
        s.switch_to("1", json_output=True)

        salvage = sorted(
            q for q in cfg.parent.iterdir()
            if q.name.startswith(f"{cfg.name}.unreadable-")
        )
        assert len(salvage) == 2, (
            f"{len(salvage)} salvage file(s) — the retry overwrote the first"
        )
        assert any(
            q.read_text(encoding="utf-8") == first_bytes for q in salvage
        ), "the FIRST failure's bytes are gone"

    def test_human_mode_is_told_the_config_was_salvaged(
        self, temp_home: Path, capsys
    ):
        """`warnings_out` is rendered only by the JSON envelope.

        In human mode the user saw "Activated Account-2" and nothing else while
        `projects` was gone from the live config. Every other
        `warnings_out.append` in `_perform_switch` is paired with an
        `if emit_output: warning(msg)`.
        """
        s = _linux_switcher()
        self._seed_two(s)
        self._torn(s)

        s.switch_to("2")                      # human mode: json_output False
        out = capsys.readouterr()
        assert "could not be parsed" in (out.out + out.err), (
            f"stdout={out.out!r} stderr={out.err!r} — nothing told the user a "
            "copy was kept"
        )


class TestADeniedKeychainSurvivesAnUnreadableFallbackFile:
    """Keychain denied AND the fallback file unreadable is the MOST unreadable
    state there is, and it reported as a clean slot.

    ``_read_active_credentials`` returns ``ActiveCredentials(None, False,
    keychain_failed)`` on that path — ``degraded`` travels, but
    ``keychain_unavailable`` is hardcoded ``False``, so the sentinel says "no
    credentials" instead of "keychain unavailable" and the user is pointed at
    a re-login that cannot help.
    """

    def test_the_keychain_verdict_is_not_dropped_by_an_unreadable_file(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        s = _macos_switcher()
        store = s._store
        cred_file = get_credentials_path()
        cred_file.write_text(OAUTH_JSON, encoding="utf-8")

        monkeypatch.setattr(store, "_use_keychain", lambda: True)
        monkeypatch.setattr(
            store, "_read_active_oauth_keychain", lambda: (None, True)
        )
        monkeypatch.setattr(store, "_read_managed_key", lambda: "")

        def _boom(*a, **k):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(Path, "read_text", _boom)

        ac = store._read_active_credentials()
        assert ac.value is None
        assert ac.degraded is True
        assert ac.keychain_unavailable is True, (
            "the keychain WAS denied; an unreadable fallback cannot clear that"
        )
