"""Tests for the ``apiKeyHelper`` channel that carries an API-key switch live.

Claude Code never re-reads ``primaryApiKey`` in a running process, so activating
an API-key account is invisible to open sessions unless the ``apiKeyHelper`` hook
carries it. These cover the channel itself (file emission, the hook's toggle
semantics, refusing to touch a foreign helper) and its wiring into
``CredentialStore._write_credentials`` in both directions.

The emitted script is *executed*, not just string-matched: a helper that Claude
Code cannot run is exactly the failure this module exists to prevent, and a
substring assertion would pass right through it.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from claude_swap.api_key_helper import SETTINGS_KEY, ApiKeyHelperChannel
from claude_swap.models import Platform
from claude_swap.paths import (
    get_claude_config_home,
    get_credentials_path,
    get_global_config_path,
)
from claude_swap.switcher import ClaudeAccountSwitcher

API_KEY = "sk-ant-api03-" + "a1b2c3d4e5" * 4
OTHER_KEY = "sk-ant-api03-" + "z9y8x7w6v5" * 4
OAUTH_JSON = json.dumps(
    {"claudeAiOauth": {"accessToken": "tok", "refreshToken": "rtok", "expiresAt": 9}}
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX /bin/sh helper; unsupported on Windows"
)


@pytest.fixture
def channel(temp_home: Path) -> ApiKeyHelperChannel:
    return ApiKeyHelperChannel(logging.getLogger("test"))


def _settings(channel: ApiKeyHelperChannel) -> dict:
    path = channel.settings_path
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _write_settings(channel: ApiKeyHelperChannel, data: dict) -> None:
    channel.settings_path.parent.mkdir(parents=True, exist_ok=True)
    channel.settings_path.write_text(json.dumps(data), encoding="utf-8")


def _linux_switcher() -> ClaudeAccountSwitcher:
    s = ClaudeAccountSwitcher()
    s.platform = Platform.LINUX
    s._setup_directories()
    s._init_sequence_file()
    return s


# ---------------------------------------------------------------------------
# Emission: the files the hook depends on
# ---------------------------------------------------------------------------


def test_install_registers_hook_and_writes_both_files(channel):
    assert channel.install(API_KEY) is True

    assert _settings(channel)[SETTINGS_KEY] == str(channel.script_path)
    assert channel.key_path.read_text(encoding="utf-8") == API_KEY
    assert channel.script_path.exists()


def test_emitted_helper_actually_prints_the_key(channel):
    """Execute the script the way Claude Code does, and check what it prints."""
    channel.install(API_KEY)

    result = subprocess.run(
        [str(channel.script_path)], capture_output=True, text=True, timeout=10
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == API_KEY


def test_emitted_helper_survives_a_path_with_spaces(tmp_path, monkeypatch):
    """The key-file path is interpolated into shell source; it must be quoted."""
    home = tmp_path / "home with spaces"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    channel = ApiKeyHelperChannel(logging.getLogger("test"))

    channel.install(API_KEY)
    result = subprocess.run(
        [str(channel.script_path)], capture_output=True, text=True, timeout=10
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == API_KEY


def test_emitted_helper_fails_loudly_when_the_key_file_is_gone(channel):
    """Better a failing helper than one that prints an empty credential."""
    channel.install(API_KEY)
    channel.key_path.unlink()

    result = subprocess.run(
        [str(channel.script_path)], capture_output=True, text=True, timeout=10
    )

    assert result.returncode != 0
    assert result.stdout.strip() == ""


def test_emitted_files_are_owner_only(channel):
    channel.install(API_KEY)

    assert stat.S_IMODE(channel.key_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(channel.script_path.stat().st_mode) == 0o700


def test_reinstall_updates_the_key_in_place(channel):
    channel.install(API_KEY)
    channel.install(OTHER_KEY)

    assert channel.key_path.read_text(encoding="utf-8") == OTHER_KEY
    result = subprocess.run(
        [str(channel.script_path)], capture_output=True, text=True, timeout=10
    )
    assert result.stdout.strip() == OTHER_KEY


# ---------------------------------------------------------------------------
# Removal
# ---------------------------------------------------------------------------


def test_remove_unregisters_the_hook_and_shreds_the_key(channel):
    channel.install(API_KEY)

    channel.remove()

    assert SETTINGS_KEY not in _settings(channel)
    assert not channel.key_path.exists()


def test_remove_is_a_no_op_when_never_installed(channel):
    channel.remove()  # must not raise

    assert not channel.settings_path.exists()


def test_remove_preserves_the_rest_of_settings(channel):
    _write_settings(channel, {"model": "opus", "permissions": {"defaultMode": "auto"}})
    channel.install(API_KEY)

    channel.remove()

    assert _settings(channel) == {
        "model": "opus",
        "permissions": {"defaultMode": "auto"},
    }


def test_install_preserves_the_rest_of_settings(channel):
    _write_settings(channel, {"model": "opus"})

    channel.install(API_KEY)

    assert _settings(channel) == {
        "model": "opus",
        SETTINGS_KEY: str(channel.script_path),
    }


def test_install_preserves_an_existing_files_permissions(channel):
    _write_settings(channel, {"model": "opus"})
    os.chmod(channel.settings_path, 0o644)

    channel.install(API_KEY)

    assert stat.S_IMODE(channel.settings_path.stat().st_mode) == 0o644


# ---------------------------------------------------------------------------
# A helper that isn't ours
# ---------------------------------------------------------------------------


def test_install_refuses_to_hijack_a_foreign_helper(channel):
    _write_settings(channel, {SETTINGS_KEY: "/usr/local/bin/my-own-helper"})

    assert channel.install(API_KEY) is False
    assert _settings(channel)[SETTINGS_KEY] == "/usr/local/bin/my-own-helper"


def test_remove_leaves_a_foreign_helper_alone(channel):
    _write_settings(channel, {SETTINGS_KEY: "/usr/local/bin/my-own-helper"})

    channel.remove()

    assert _settings(channel)[SETTINGS_KEY] == "/usr/local/bin/my-own-helper"


def test_unparseable_settings_are_never_rewritten(channel):
    channel.settings_path.parent.mkdir(parents=True, exist_ok=True)
    channel.settings_path.write_text("{ not json", encoding="utf-8")

    assert channel.install(API_KEY) is False
    assert channel.settings_path.read_text(encoding="utf-8") == "{ not json"


def test_a_settings_file_that_is_not_an_object_is_never_rewritten(channel):
    channel.settings_path.parent.mkdir(parents=True, exist_ok=True)
    channel.settings_path.write_text("[1, 2, 3]", encoding="utf-8")

    assert channel.install(API_KEY) is False
    assert channel.settings_path.read_text(encoding="utf-8") == "[1, 2, 3]"


def test_reinstalling_the_same_hook_does_not_rewrite_settings(channel):
    """An idle rewrite would wake every running session's settings watcher."""
    channel.install(API_KEY)
    before = channel.settings_path.stat().st_mtime_ns

    channel.install(OTHER_KEY)

    assert channel.settings_path.stat().st_mtime_ns == before


# ---------------------------------------------------------------------------
# Wiring into the switch itself
# ---------------------------------------------------------------------------


def test_activating_an_api_key_account_installs_the_hook(temp_home):
    switcher = _linux_switcher()

    switcher._store._write_credentials(API_KEY)

    settings = json.loads(
        (get_claude_config_home() / "settings.json").read_text(encoding="utf-8")
    )
    helper = ApiKeyHelperChannel(logging.getLogger("test"))
    assert settings[SETTINGS_KEY] == str(helper.script_path)
    assert helper.key_path.read_text(encoding="utf-8") == API_KEY


def test_activating_an_oauth_account_removes_the_hook(temp_home):
    """Otherwise the stale hook would outrank the OAuth login we just wrote."""
    switcher = _linux_switcher()
    switcher._store._write_credentials(API_KEY)

    switcher._store._write_credentials(OAUTH_JSON)

    helper = ApiKeyHelperChannel(logging.getLogger("test"))
    settings_path = get_claude_config_home() / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert SETTINGS_KEY not in settings
    assert not helper.key_path.exists()


def test_the_hook_serves_the_key_the_switch_just_activated(temp_home):
    """End to end: switch to key A, then key B, and run the helper each time."""
    switcher = _linux_switcher()
    helper = ApiKeyHelperChannel(logging.getLogger("test"))

    switcher._store._write_credentials(API_KEY)
    first = subprocess.run(
        [str(helper.script_path)], capture_output=True, text=True, timeout=10
    )
    switcher._store._write_credentials(OTHER_KEY)
    second = subprocess.run(
        [str(helper.script_path)], capture_output=True, text=True, timeout=10
    )

    assert first.stdout.strip() == API_KEY
    assert second.stdout.strip() == OTHER_KEY


def test_a_keychain_backed_key_keeps_no_plaintext_helper(temp_home, block_real_keychain):
    """macOS Keychain users must not silently gain a plaintext key on disk."""
    switcher = ClaudeAccountSwitcher()
    switcher.platform = Platform.MACOS
    switcher._setup_directories()
    switcher._init_sequence_file()

    switcher._store._write_credentials(API_KEY)

    helper = ApiKeyHelperChannel(logging.getLogger("test"))
    assert not helper.key_path.exists()
    settings_path = get_claude_config_home() / "settings.json"
    settings = (
        json.loads(settings_path.read_text(encoding="utf-8"))
        if settings_path.exists()
        else {}
    )
    assert SETTINGS_KEY not in settings


# ---------------------------------------------------------------------------
# Adopting a live login (``cswap add``)
# ---------------------------------------------------------------------------


def _login_as(email: str) -> None:
    """Stand in for a Claude Code ``/login``: OAuth credential + identity on disk.

    Mirrors what Claude Code itself leaves behind, including dropping
    ``primaryApiKey`` (its ``removeApiKey``) — the point being that its login has
    no way to clean up the ``apiKeyHelper`` hook, which is cswap's to own.
    """
    get_credentials_path().parent.mkdir(parents=True, exist_ok=True)
    get_credentials_path().write_text(OAUTH_JSON, encoding="utf-8")
    config_path = get_global_config_path()
    config = (
        json.loads(config_path.read_text(encoding="utf-8"))
        if config_path.exists()
        else {}
    )
    config.pop("primaryApiKey", None)
    config["oauthAccount"] = {
        "emailAddress": email,
        "accountUuid": "uuid-" + email,
        "organizationUuid": "",
        "organizationName": "",
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")


def test_adding_a_live_login_unregisters_an_api_key_accounts_hook(temp_home, capsys):
    """``add`` activates the captured account, so the old hook must not survive it.

    Regression: the helper outranks the OAuth credential, so an ``add`` that left
    it registered reported the new account as active while every session — the
    ones already running *and* every new one — kept billing the previous
    account's key.
    """
    switcher = _linux_switcher()
    switcher._store._write_credentials(API_KEY)  # an API-key account is active
    helper = ApiKeyHelperChannel(logging.getLogger("test"))
    assert _settings(helper)[SETTINGS_KEY] == str(helper.script_path)

    _login_as("new@example.com")
    switcher.add_account()

    assert SETTINGS_KEY not in _settings(helper)
    assert not helper.key_path.exists()
    seq = json.loads(switcher.sequence_file.read_text(encoding="utf-8"))
    assert seq["accounts"][str(seq["activeAccountNumber"])]["email"] == "new@example.com"


def test_adding_a_live_login_clears_a_residual_managed_key(temp_home, capsys):
    """Mutual exclusion holds however the key got left behind, not just via the hook."""
    switcher = _linux_switcher()
    switcher._store._write_credentials(API_KEY)

    _login_as("new@example.com")
    # A key Claude Code's own login did not clear (e.g. written by another tool).
    config_path = get_global_config_path()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["primaryApiKey"] = API_KEY
    config_path.write_text(json.dumps(config), encoding="utf-8")

    switcher.add_account()

    assert "primaryApiKey" not in json.loads(
        config_path.read_text(encoding="utf-8")
    )
    # ...and it is not carried into the new slot's config backup either.
    backup = next(switcher.configs_dir.glob(".claude-config-*-new@example.com.json"))
    assert "primaryApiKey" not in json.loads(backup.read_text(encoding="utf-8"))


def test_re_adding_the_active_account_also_unregisters_the_hook(temp_home, capsys):
    """The refresh-in-place branch of ``add`` activates a slot the same way."""
    switcher = _linux_switcher()
    _login_as("existing@example.com")
    switcher.add_account()
    switcher._store._write_credentials(API_KEY)  # switch away to an API-key account
    helper = ApiKeyHelperChannel(logging.getLogger("test"))
    assert _settings(helper)[SETTINGS_KEY] == str(helper.script_path)

    _login_as("existing@example.com")
    switcher.add_account()  # re-add the same account: refreshes it in place

    assert SETTINGS_KEY not in _settings(helper)
    assert not helper.key_path.exists()
