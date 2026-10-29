"""Tests for the ``apiKeyHelper`` channel that carries API-key switches live.

Running Claude Code sessions never re-read ``primaryApiKey``, so activating an
API-key account used to log them out (the OAuth credential is cleared, the key
is never seen). The channel registers a cswap-owned ``apiKeyHelper`` in the
active profile's ``settings.json`` instead, which Claude Code reloads live.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import api_key_helper, macos_keychain
from claude_swap.api_key_helper import (
    SETTINGS_KEY,
    ApiKeyHelperChannel,
    key_fingerprint,
)
from claude_swap.credentials import CredentialStore
from claude_swap.models import Platform
from claude_swap.paths import get_credentials_path, get_global_config_path
from claude_swap.switcher import ClaudeAccountSwitcher

API_KEY = "sk-ant-api03-" + "a1b2c3d4e5" * 4
OTHER_KEY = "sk-ant-api03-" + "z9y8x7w6v5" * 4
OAUTH_JSON = json.dumps(
    {"claudeAiOauth": {"accessToken": "tok", "refreshToken": "rtok", "expiresAt": 9}}
)
FOREIGN = "/usr/local/bin/my-vault-key"

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes and /bin/sh")


def _channel(platform: Platform = Platform.LINUX) -> ApiKeyHelperChannel:
    return ApiKeyHelperChannel(logging.getLogger("test"), platform)


def _settings(ch: ApiKeyHelperChannel) -> dict:
    return json.loads(ch.settings_path.read_text(encoding="utf-8"))


def _write_settings(ch: ApiKeyHelperChannel, data: dict) -> None:
    ch.settings_path.parent.mkdir(parents=True, exist_ok=True)
    ch.settings_path.write_text(json.dumps(data), encoding="utf-8")


def _linux_switcher() -> ClaudeAccountSwitcher:
    s = ClaudeAccountSwitcher()
    s.platform = Platform.LINUX
    s._setup_directories()
    s._init_sequence_file()
    return s


# ---------------------------------------------------------------------------
# The channel on its own
# ---------------------------------------------------------------------------


class TestInstall:
    def test_registers_hook_and_writes_files(self, temp_home: Path):
        ch = _channel()
        assert ch.install(API_KEY) is True
        assert _settings(ch)[SETTINGS_KEY] == ch.hook_command(API_KEY)
        assert ch.key_path.read_text(encoding="utf-8") == API_KEY
        assert ch.script_path.exists()

    @posix_only
    def test_key_file_is_owner_only_and_script_executable(self, temp_home: Path):
        ch = _channel()
        ch.install(API_KEY)
        assert stat.S_IMODE(ch.key_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(ch.script_path.stat().st_mode) == 0o700

    @posix_only
    def test_generated_script_prints_the_key(self, temp_home: Path):
        ch = _channel()
        ch.install(API_KEY)
        # Run exactly what Claude Code runs: the registered command, via the shell.
        out = subprocess.run(
            ["/bin/sh", "-c", _settings(ch)[SETTINGS_KEY]],
            capture_output=True, text=True, check=True,
        )
        assert out.stdout == API_KEY

    @posix_only
    def test_generated_script_fails_loudly_without_its_key_file(self, temp_home: Path):
        ch = _channel()
        ch.install(API_KEY)
        ch.key_path.unlink()
        res = subprocess.run(["/bin/sh", "-c", _settings(ch)[SETTINGS_KEY]],
                             capture_output=True, text=True)
        assert res.returncode != 0 and res.stdout == ""

    def test_command_differs_per_key_and_never_contains_it(self, temp_home: Path):
        # Claude Code caches helper output until the command string changes, so a
        # switch between two keys must change it (measured on Claude Code 2.1.302).
        ch = _channel()
        a, b = ch.hook_command(API_KEY), ch.hook_command(OTHER_KEY)
        assert a != b
        assert API_KEY not in a and API_KEY[-20:] not in a
        assert key_fingerprint(API_KEY) in a

    def test_switching_keys_rewrites_the_command(self, temp_home: Path):
        ch = _channel()
        ch.install(API_KEY)
        ch.install(OTHER_KEY)
        assert _settings(ch)[SETTINGS_KEY] == ch.hook_command(OTHER_KEY)
        assert ch.key_path.read_text(encoding="utf-8") == OTHER_KEY

    def test_preserves_other_settings(self, temp_home: Path):
        ch = _channel()
        _write_settings(ch, {"theme": "dark", "env": {"A": "1"}, "model": "opus"})
        ch.install(API_KEY)
        data = _settings(ch)
        assert data["theme"] == "dark" and data["env"] == {"A": "1"} and data["model"] == "opus"

    @posix_only
    def test_keeps_an_existing_settings_mode(self, temp_home: Path):
        ch = _channel()
        _write_settings(ch, {"theme": "dark"})
        os.chmod(ch.settings_path, 0o664)
        ch.install(API_KEY)
        assert stat.S_IMODE(ch.settings_path.stat().st_mode) == 0o664

    @posix_only
    def test_writes_through_a_symlinked_settings_file(self, temp_home: Path, tmp_path: Path):
        ch = _channel()
        real = tmp_path / "dotfiles" / "claude-settings.json"
        real.parent.mkdir()
        real.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
        ch.settings_path.parent.mkdir(parents=True, exist_ok=True)
        ch.settings_path.symlink_to(real)

        ch.install(API_KEY)

        assert ch.settings_path.is_symlink(), "the dotfile link must survive"
        assert json.loads(real.read_text())[SETTINGS_KEY] == ch.hook_command(API_KEY)

    def test_same_key_twice_does_not_rewrite_settings(self, temp_home: Path):
        # A needless rewrite wakes every running session's settings watcher.
        ch = _channel()
        ch.install(API_KEY)
        before = ch.settings_path.stat().st_mtime_ns
        with patch.object(ApiKeyHelperChannel, "_write_settings") as w:
            ch.install(API_KEY)
        w.assert_not_called()
        assert ch.settings_path.stat().st_mtime_ns == before

    def test_leaves_a_foreign_helper_alone(self, temp_home: Path, caplog):
        ch = _channel()
        _write_settings(ch, {SETTINGS_KEY: FOREIGN})
        with caplog.at_level(logging.WARNING):
            assert ch.install(API_KEY) is False
        assert _settings(ch)[SETTINGS_KEY] == FOREIGN
        assert "leaving your own helper" in caplog.text

    @pytest.mark.parametrize("content", ["{not json", "[1, 2]"])
    def test_refuses_to_rewrite_an_unparsable_settings_file(self, temp_home: Path, content):
        ch = _channel()
        ch.settings_path.parent.mkdir(parents=True, exist_ok=True)
        ch.settings_path.write_text(content, encoding="utf-8")
        assert ch.install(API_KEY) is False
        assert ch.settings_path.read_text(encoding="utf-8") == content

    def test_unsupported_on_windows(self, temp_home: Path):
        ch = _channel(Platform.WINDOWS)
        assert ch.install(API_KEY) is False
        assert not ch.settings_path.exists()
        assert not ch.key_path.exists()

    def test_recognises_its_own_hook_under_a_path_with_spaces(
        self, temp_home: Path, tmp_path: Path, monkeypatch
    ):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data dir"))
        ch = _channel()
        assert ch.is_ours(ch.hook_command(API_KEY))
        assert not ch.is_ours(FOREIGN)
        assert not ch.is_ours(None)


class TestRemove:
    def test_unregisters_and_deletes_the_key_file(self, temp_home: Path):
        ch = _channel()
        _write_settings(ch, {"theme": "dark"})
        ch.install(API_KEY)
        assert ch.remove() is True
        assert _settings(ch) == {"theme": "dark"}
        assert not ch.key_path.exists()

    def test_no_settings_file_is_not_created(self, temp_home: Path):
        ch = _channel()
        assert ch.remove() is False
        assert not ch.settings_path.exists()

    def test_settings_without_a_hook_are_not_rewritten(self, temp_home: Path):
        ch = _channel()
        _write_settings(ch, {"theme": "dark"})
        with patch.object(ApiKeyHelperChannel, "_write_settings") as w:
            assert ch.remove() is False
        w.assert_not_called()

    def test_leaves_a_foreign_helper_alone(self, temp_home: Path):
        ch = _channel()
        _write_settings(ch, {SETTINGS_KEY: FOREIGN})
        assert ch.remove() is False
        assert _settings(ch)[SETTINGS_KEY] == FOREIGN


# ---------------------------------------------------------------------------
# Wired into credential activation
# ---------------------------------------------------------------------------


class TestActivation:
    def test_key_activation_registers_the_hook_before_oauth_is_cleared(
        self, temp_home: Path
    ):
        s = _linux_switcher()
        ch = s._store._api_key_helper
        cred_file = get_credentials_path()
        cred_file.write_text(OAUTH_JSON, encoding="utf-8")

        seen = {}
        original = CredentialStore._clear_oauth_credential

        def spy(store):
            seen["hook_at_clear"] = _settings(ch).get(SETTINGS_KEY)
            return original(store)

        with patch.object(CredentialStore, "_clear_oauth_credential", spy):
            s._write_credentials(API_KEY)

        # Never a moment with neither credential: the hook was live when OAuth went.
        assert seen["hook_at_clear"] == ch.hook_command(API_KEY)
        assert not cred_file.exists()
        # primaryApiKey is still written, so newly started sessions find the key.
        cfg = json.loads(get_global_config_path().read_text(encoding="utf-8"))
        assert cfg["primaryApiKey"] == API_KEY

    def test_oauth_activation_removes_the_hook_only_after_oauth_is_written(
        self, temp_home: Path
    ):
        s = _linux_switcher()
        ch = s._store._api_key_helper
        s._write_credentials(API_KEY)
        cred_file = get_credentials_path()

        seen = {}
        original = ApiKeyHelperChannel.remove

        def spy(channel):
            seen["oauth_at_remove"] = cred_file.exists()
            return original(channel)

        with patch.object(ApiKeyHelperChannel, "remove", spy):
            s._write_credentials(OAUTH_JSON)

        assert seen["oauth_at_remove"] is True
        assert SETTINGS_KEY not in _settings(ch)
        assert not ch.key_path.exists()
        cfg = json.loads(get_global_config_path().read_text(encoding="utf-8"))
        assert "primaryApiKey" not in cfg

    def test_oauth_is_cleared_only_after_running_sessions_had_time_to_load_the_hook(
        self, temp_home: Path, monkeypatch
    ):
        # Measured: a request 0.5 s after the hook was written still missed it,
        # and with OAuth already gone it failed with "Not logged in".
        monkeypatch.setattr(api_key_helper, "SETTLE_SECONDS", 3.0)
        s = _linux_switcher()
        get_credentials_path().write_text(OAUTH_JSON, encoding="utf-8")
        events = []
        monkeypatch.setattr(
            api_key_helper.time, "sleep", lambda secs: events.append(("sleep", secs))
        )
        original = CredentialStore._clear_oauth_credential

        def spy(store):
            events.append(("clear-oauth",))
            return original(store)

        with patch.object(CredentialStore, "_clear_oauth_credential", spy):
            s._write_credentials(API_KEY)

        assert events == [("sleep", 3.0), ("clear-oauth",)]

    def test_an_unchanged_hook_does_not_wait(self, temp_home: Path, monkeypatch):
        monkeypatch.setattr(api_key_helper, "SETTLE_SECONDS", 3.0)
        s = _linux_switcher()
        s._write_credentials(API_KEY)
        slept = []
        monkeypatch.setattr(api_key_helper.time, "sleep", slept.append)
        s._write_credentials(API_KEY)  # same key, hook already registered
        assert slept == []

    def test_key_to_key_switch_changes_the_command(self, temp_home: Path):
        s = _linux_switcher()
        ch = s._store._api_key_helper
        s._write_credentials(API_KEY)
        s._write_credentials(OTHER_KEY)
        assert _settings(ch)[SETTINGS_KEY] == ch.hook_command(OTHER_KEY)

    def test_a_foreign_helper_does_not_block_the_switch(self, temp_home: Path):
        s = _linux_switcher()
        ch = s._store._api_key_helper
        _write_settings(ch, {SETTINGS_KEY: FOREIGN})
        s._write_credentials(API_KEY)
        assert _settings(ch)[SETTINGS_KEY] == FOREIGN
        cfg = json.loads(get_global_config_path().read_text(encoding="utf-8"))
        assert cfg["primaryApiKey"] == API_KEY

    def test_keychain_storage_registers_a_keychain_reading_hook(
        self, temp_home: Path, block_real_keychain
    ):
        s = ClaudeAccountSwitcher()
        s.platform = Platform.MACOS
        s._setup_directories()
        s._init_sequence_file()
        ch = s._store._api_key_helper
        # A key file left over from an earlier file-mode spell on another key.
        ch.install(OTHER_KEY)
        assert ch.key_path.exists()

        s._write_credentials(API_KEY)  # lands in the (fake) Keychain

        assert _settings(ch)[SETTINGS_KEY] == ch.hook_command(API_KEY)
        assert not ch.key_path.exists(), "no plaintext copy beside the Keychain"
        script = ch.script_path.read_text(encoding="utf-8")
        assert "find-generic-password" in script
        assert "'Claude Code'" in script
        assert macos_keychain.keychain_account_name() in script
        assert API_KEY not in script

    @posix_only
    def test_keychain_script_prints_the_key_without_a_newline(
        self, temp_home: Path, tmp_path: Path, monkeypatch
    ):
        # `security -w` prints the value plus a newline; the helper must not.
        stub = tmp_path / "security"
        stub.write_text(
            "#!/bin/sh\n"
            'case "$*" in *"-a me -s Claude Code -w"*) echo "' + API_KEY + '";; '
            "*) exit 44;; esac\n",
            encoding="utf-8",
        )
        stub.chmod(0o755)
        monkeypatch.setattr(macos_keychain, "_SECURITY", str(stub))
        ch = _channel(Platform.MACOS)
        ch.install(API_KEY, keychain_item=("Claude Code", "me"))
        out = subprocess.run(["/bin/sh", "-c", _settings(ch)[SETTINGS_KEY]],
                             capture_output=True, text=True, check=True)
        assert out.stdout == API_KEY


class TestNoStoreWritesWithoutAKey:
    def test_oauth_activation_without_a_key_touches_no_helper_file(
        self, temp_home: Path, monkeypatch
    ):
        # Every OAuth switch clears the managed key, which removes the helper.
        # With no key ever activated that must not write or delete anything in
        # the account store (CI's real-store guard caught a no-op unlink here).
        s = _linux_switcher()
        ch = s._store._api_key_helper
        touched = []
        original = Path.unlink

        def spy(self, *args, **kwargs):
            touched.append(self)
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", spy)
        s._write_credentials(OAUTH_JSON)
        assert ch.key_path not in touched
        assert not ch.settings_path.exists()


class TestAddAccount:
    def test_capturing_a_fresh_login_removes_the_hook(
        self, temp_home: Path, mock_claude_config: Path
    ):
        s = _linux_switcher()
        ch = s._store._api_key_helper
        s._write_credentials(API_KEY)
        assert ch.is_ours(_settings(ch).get(SETTINGS_KEY))

        # The user runs /login: Claude Code writes OAuth and drops primaryApiKey,
        # but cannot know about the hook cswap registered.
        get_credentials_path().write_text(OAUTH_JSON, encoding="utf-8")
        cfg_path = s._get_claude_config_path()
        cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
        cfg.pop("primaryApiKey", None)
        cfg["oauthAccount"] = {"emailAddress": "new@example.com", "organizationUuid": ""}
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

        with patch.object(s, "_read_capture_credentials", return_value=OAUTH_JSON), \
             patch("claude_swap.oauth.fetch_oauth_profile",
                   return_value={"uuid": "u-new", "email": "new@example.com",
                                 "organizationUuid": ""}):
            s.add_account(slot=3, assume_yes=True)

        assert SETTINGS_KEY not in _settings(ch)
        assert not ch.key_path.exists()


class TestPurge:
    def test_purge_unregisters_the_hook(self, temp_home: Path):
        s = _linux_switcher()
        ch = s._store._api_key_helper
        _write_settings(ch, {"theme": "dark"})
        s._write_credentials(API_KEY)

        with patch("builtins.input", return_value="y"):
            s.purge()

        assert _settings(ch) == {"theme": "dark"}
        # The key stays reachable for newly started sessions.
        cfg = json.loads(get_global_config_path().read_text(encoding="utf-8"))
        assert cfg["primaryApiKey"] == API_KEY
