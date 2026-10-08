"""``codex_store``: the live (active) Codex credential store.

Codex keeps its login in ``$CODEX_HOME/auth.json`` or, when ``config.toml``
says ``cli_auth_credentials_store = "keyring"``/``"auto"``, in the macOS
Keychain item ``"Codex Auth"`` / ``"cli|<sha256(realpath(home))[:16]>"``.
``CodexCredentialStore`` overrides only the active half of
``CredentialStore`` (read classification + activation write); the per-slot
backup half is inherited unchanged.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
from pathlib import Path

import pytest

from claude_swap import codex_store, macos_keychain
from claude_swap.codex_store import (
    CODEX_KEYRING_SERVICE,
    CodexCredentialStore,
    CodexStoreUnsupported,
    codex_home,
    credential_store_mode,
    keyring_account_name,
    live_auth_path,
    read_codex_config,
)
from claude_swap.credentials import ActiveCredentials
from claude_swap.exceptions import ClaudeSwitchError, CredentialWriteError
from claude_swap.models import Platform
from tests import codex_fixtures as cf

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes/symlinks")


class _Host:
    """Minimal ``_StoreHost``: data only, read at call time."""

    def __init__(self, credentials_dir: Path, platform: Platform = Platform.MACOS):
        self.platform = platform
        self.credentials_dir = credentials_dir
        self.backup_keychain_service = "claude-swap-codex"
        self._logger = logging.getLogger("claude-swap")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An existing ``$CODEX_HOME``."""
    path = tmp_path / "codex-home"
    path.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(path))
    return path


@pytest.fixture
def store(tmp_path: Path) -> CodexCredentialStore:
    return CodexCredentialStore(_Host(tmp_path / "backups"))


@pytest.fixture(autouse=True)
def _no_parse_retry_wait(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(codex_store, "_PARSE_RETRY_DELAY", 0.0)


def _set_mode(home: Path, mode: str, extra: str = "") -> None:
    (home / "config.toml").write_text(
        f'cli_auth_credentials_store = "{mode}"\n{extra}', encoding="utf-8"
    )


def _kc_key(home: Path) -> tuple[str, str]:
    return (CODEX_KEYRING_SERVICE, keyring_account_name(home))


def _raise_keychain(*_args):
    raise macos_keychain.KeychainError("locked")


A = cf.auth_json(email="a@example.com", account_id="acct-a", user_id="user-a")
B = cf.auth_json(email="b@example.com", account_id="acct-b", user_id="user-b")


# -- module-level helpers -------------------------------------------------------


class TestCodexHome:
    def test_defaults_to_dot_codex_under_home(self):
        assert codex_home() == Path.home() / ".codex"

    def test_honors_codex_home(self, home: Path):
        assert codex_home() == home

    def test_empty_codex_home_means_default(self, monkeypatch: pytest.MonkeyPatch):
        # Codex filters an empty CODEX_HOME out (utils/home-dir/src/lib.rs:13-17).
        monkeypatch.setenv("CODEX_HOME", "")
        assert codex_home() == Path.home() / ".codex"

    def test_live_auth_path(self, home: Path):
        assert live_auth_path() == home / "auth.json"


class TestReadCodexConfig:
    def test_absent_is_empty(self, home: Path):
        assert read_codex_config() == {}

    def test_parses_toml(self, home: Path):
        _set_mode(home, "keyring", 'chatgpt_base_url = "https://example.test/backend-api"\n')
        assert read_codex_config() == {
            "cli_auth_credentials_store": "keyring",
            "chatgpt_base_url": "https://example.test/backend-api",
        }

    def test_unparseable_warns_and_is_empty(self, home: Path, caplog):
        (home / "config.toml").write_text("this is = = not toml", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            assert read_codex_config() == {}
        assert "config.toml" in caplog.text

    def test_non_utf8_warns_and_is_empty(self, home: Path, caplog):
        (home / "config.toml").write_bytes(b'cli_auth_credentials_store = "\xff\xfe"\n')
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            assert read_codex_config() == {}
        assert "config.toml" in caplog.text

    def test_mode_defaults_to_file(self, home: Path):
        assert credential_store_mode() == "file"

    def test_mode_reads_config(self, home: Path):
        _set_mode(home, "auto")
        assert credential_store_mode() == "auto"

    def test_unparseable_config_means_file_mode(self, home: Path):
        (home / "config.toml").write_text("[[[", encoding="utf-8")
        assert credential_store_mode() == "file"


class TestKeyringAccountName:
    def test_matches_codex_derivation(self, tmp_path: Path):
        # login/src/auth/storage.rs:235-249: "cli|" + sha256(canonical path)[:16].
        expected = hashlib.sha256(os.path.realpath(tmp_path).encode()).hexdigest()[:16]
        assert keyring_account_name(tmp_path) == f"cli|{expected}"

    @posix_only
    def test_known_vector(self):
        # A path that does not exist hashes as spelled (Codex falls back to
        # the raw path when canonicalize fails).
        assert keyring_account_name(Path("/nonexistent-cswap/.codex")) == "cli|42ee728ffee12fdc"

    @posix_only
    def test_symlinked_home_hashes_its_target(self, tmp_path: Path):
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        assert keyring_account_name(link) == keyring_account_name(real)


# -- file mode --------------------------------------------------------------------


class TestFileModeRead:
    def test_absent(self, home: Path, store: CodexCredentialStore):
        result = store._read_active_credentials()
        assert isinstance(result, ActiveCredentials)
        assert result == ActiveCredentials("", False, False)

    def test_present_is_returned_verbatim(self, home: Path, store: CodexCredentialStore):
        (home / "auth.json").write_text(A, encoding="utf-8")
        assert store._read_active_credentials() == ActiveCredentials(A, False, False)
        assert store._read_credentials() == A

    def test_unreadable_is_none(self, home: Path, store: CodexCredentialStore):
        (home / "auth.json").mkdir()  # IsADirectoryError / PermissionError: an OSError
        assert store._read_active_credentials() == ActiveCredentials(None, False, False)
        assert store._read_credentials() is None

    def test_missing_codex_home_reads_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: CodexCredentialStore
    ):
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "nope"))
        assert store._read_active_credentials() == ActiveCredentials("", False, False)

    def test_torn_read_is_retried(
        self, home: Path, store: CodexCredentialStore, monkeypatch: pytest.MonkeyPatch
    ):
        """Codex writes auth.json in place (truncate + write, no rename), so a
        reader can see a prefix; Codex finishing its write during our backoff
        must yield the complete credential."""
        auth = home / "auth.json"
        auth.write_text(A[: len(A) // 2], encoding="utf-8")
        sleeps: list[float] = []

        def finish_write(delay: float) -> None:
            sleeps.append(delay)
            auth.write_text(A, encoding="utf-8")

        monkeypatch.setattr(codex_store.time, "sleep", finish_write)
        assert store._read_active_credentials() == ActiveCredentials(A, False, False)
        assert len(sleeps) == 1

    def test_persistently_unparseable_is_a_read_error(
        self, home: Path, store: CodexCredentialStore, caplog
    ):
        """Never hand garbage on as the live credential: the switch would
        back it up over the departing slot's good backup."""
        (home / "auth.json").write_text('{"tokens": ', encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            assert store._read_active_credentials() == ActiveCredentials(None, False, False)
        assert "auth.json" in caplog.text

    def test_non_utf8_is_a_read_error(self, home: Path, store: CodexCredentialStore):
        (home / "auth.json").write_bytes(b'{"tokens": "\xff\xfe"}')
        assert store._read_active_credentials() == ActiveCredentials(None, False, False)

    @pytest.mark.parametrize("text", ["null", '"x"', "[]", "42"])
    def test_non_object_json_is_a_read_error(
        self, home: Path, store: CodexCredentialStore, text: str
    ):
        # AuthDotJson is an object; anything else fails Codex's serde too.
        (home / "auth.json").write_text(text, encoding="utf-8")
        assert store._read_active_credentials() == ActiveCredentials(None, False, False)

    def test_crlf_is_returned_byte_exact(self, home: Path, store: CodexCredentialStore):
        crlf = A.replace("\n", "\r\n")
        (home / "auth.json").write_bytes(crlf.encode())
        assert store._read_credentials() == crlf

    @posix_only
    def test_follows_symlink(self, home: Path, tmp_path: Path, store: CodexCredentialStore):
        target = tmp_path / "elsewhere.json"
        target.write_text(A, encoding="utf-8")
        (home / "auth.json").symlink_to(target)
        assert store._read_credentials() == A


class TestFileModeWrite:
    def test_writes_exact_text(self, home: Path, store: CodexCredentialStore):
        store._write_credentials(A)
        assert (home / "auth.json").read_text(encoding="utf-8") == A
        assert store._last_active_credentials_backend == "file"

    @posix_only
    def test_mode_0600(self, home: Path, store: CodexCredentialStore):
        store._write_credentials(A)
        assert (home / "auth.json").stat().st_mode & 0o777 == 0o600

    @posix_only
    def test_replaces_existing_wider_mode_file_with_0600(
        self, home: Path, store: CodexCredentialStore
    ):
        auth = home / "auth.json"
        auth.write_text(B, encoding="utf-8")
        auth.chmod(0o644)
        store._write_credentials(A)
        assert auth.stat().st_mode & 0o777 == 0o600

    def test_atomic_failure_leaves_live_file_and_no_temp(
        self, home: Path, store: CodexCredentialStore, monkeypatch: pytest.MonkeyPatch
    ):
        (home / "auth.json").write_text(B, encoding="utf-8")

        def boom(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr(codex_store, "replace_with_retry", boom)
        with pytest.raises(CredentialWriteError):
            store._write_credentials(A)
        assert (home / "auth.json").read_text(encoding="utf-8") == B
        assert sorted(p.name for p in home.iterdir()) == ["auth.json"]

    def test_no_temp_files_left_on_success(self, home: Path, store: CodexCredentialStore):
        store._write_credentials(A)
        assert sorted(p.name for p in home.iterdir()) == ["auth.json"]

    @posix_only
    def test_symlink_target_replaced_not_the_link(
        self, home: Path, tmp_path: Path, store: CodexCredentialStore
    ):
        dotfiles = tmp_path / "dotfiles"
        dotfiles.mkdir()
        target = dotfiles / "codex-auth.json"
        target.write_text(B, encoding="utf-8")
        link = home / "auth.json"
        link.symlink_to(target)

        store._write_credentials(A)

        assert link.is_symlink()
        assert os.readlink(link) == str(target)
        assert target.read_text(encoding="utf-8") == A
        assert target.stat().st_mode & 0o777 == 0o600

    def test_missing_codex_home_raises_and_creates_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: CodexCredentialStore
    ):
        """Codex refuses a CODEX_HOME that does not exist; creating it would
        hand the login to a directory Codex never reads."""
        missing = tmp_path / "nope"
        monkeypatch.setenv("CODEX_HOME", str(missing))
        with pytest.raises(CredentialWriteError, match="CODEX_HOME"):
            store._write_credentials(A)
        assert not missing.exists()

    def test_default_home_is_created(self, store: CodexCredentialStore):
        # Codex's file save does create_dir_all (storage.rs:206-223).
        store._write_credentials(A)
        assert (Path.home() / ".codex" / "auth.json").read_text(encoding="utf-8") == A

    def test_non_json_is_refused(self, home: Path, store: CodexCredentialStore):
        (home / "auth.json").write_text(B, encoding="utf-8")
        with pytest.raises(CredentialWriteError):
            store._write_credentials("not json")
        assert (home / "auth.json").read_text(encoding="utf-8") == B

    @pytest.mark.parametrize("text", ["null", '"x"', "[]"])
    def test_non_object_json_is_refused(
        self, home: Path, store: CodexCredentialStore, text: str
    ):
        (home / "auth.json").write_text(B, encoding="utf-8")
        with pytest.raises(CredentialWriteError, match="JSON object"):
            store._write_credentials(text)
        assert (home / "auth.json").read_text(encoding="utf-8") == B

    def test_malformed_config_warns_once_per_write(
        self, home: Path, store: CodexCredentialStore, caplog
    ):
        (home / "config.toml").write_text("[[[", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            store._write_credentials(A)
        assert caplog.text.count("Ignoring unreadable Codex config") == 1

    def test_rollback_restores_exact_original(self, home: Path, store: CodexCredentialStore):
        (home / "auth.json").write_text(A, encoding="utf-8")
        original = store._read_credentials()
        store._write_credentials(B)
        store._write_credentials(original)
        assert store._read_credentials() == A
        assert (home / "auth.json").read_text(encoding="utf-8") == A

    def test_api_key_blob_round_trip(self, home: Path, store: CodexCredentialStore, caplog):
        blob = cf.api_key_json()
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            store._write_credentials(blob)
        assert store._read_active_credentials() == ActiveCredentials(blob, False, False)
        assert caplog.text == ""

    def test_does_not_touch_shared_keychain_routing(
        self, home: Path, store: CodexCredentialStore, block_real_keychain
    ):
        """File-mode active I/O must not probe the Keychain: `_kc_call` /
        `_use_keychain` drive the BACKUP half's routing."""
        store._write_credentials(A)
        store._read_active_credentials()
        assert block_real_keychain.data == {}
        assert store._keychain_usable_cache is None
        assert store._keychain_op_failed is False


# -- keyring mode (macOS) ---------------------------------------------------------


class TestKeyringMode:
    def test_reads_the_keychain_item_only(
        self, home: Path, store: CodexCredentialStore, block_real_keychain
    ):
        _set_mode(home, "keyring")
        compact = cf.auth_json(pretty=False)
        block_real_keychain.data[_kc_key(home)] = compact
        (home / "auth.json").write_text(B, encoding="utf-8")
        assert store._read_active_credentials() == ActiveCredentials(compact, False, False)

    def test_absent_item_is_absent_even_with_a_file(
        self, home: Path, store: CodexCredentialStore
    ):
        # keyring mode reads only the keyring (storage.rs:517-528).
        _set_mode(home, "keyring")
        (home / "auth.json").write_text(B, encoding="utf-8")
        assert store._read_active_credentials() == ActiveCredentials("", False, False)

    def test_unreadable_keychain_is_a_read_error(
        self, home: Path, store: CodexCredentialStore, monkeypatch: pytest.MonkeyPatch
    ):
        """No fallback exists in keyring mode, so a failed read is the read
        error of the only store — ``None``, never "absent" (which a switch
        would take as "nothing live to back up")."""
        _set_mode(home, "keyring")
        monkeypatch.setattr(macos_keychain, "get_password", _raise_keychain)
        assert store._read_active_credentials() == ActiveCredentials(None, True, True)

    def test_non_object_item_is_a_read_error(
        self, home: Path, store: CodexCredentialStore, block_real_keychain
    ):
        _set_mode(home, "keyring")
        block_real_keychain.data[_kc_key(home)] = "[]"
        assert store._read_active_credentials() == ActiveCredentials(None, True, True)

    @posix_only
    def test_write_unlinks_a_symlinked_auth_json_not_its_target(
        self, home: Path, tmp_path: Path, store: CodexCredentialStore
    ):
        # Codex's remove_file removes the link itself (storage.rs:158-165).
        _set_mode(home, "keyring")
        target = tmp_path / "dotfiles-auth.json"
        target.write_text(B, encoding="utf-8")
        (home / "auth.json").symlink_to(target)
        store._write_credentials(A)
        assert not (home / "auth.json").is_symlink()
        assert not (home / "auth.json").exists()
        assert target.read_text(encoding="utf-8") == B

    def test_auth_json_unlink_failure_only_warns(
        self, home: Path, store: CodexCredentialStore, monkeypatch, caplog
    ):
        _set_mode(home, "keyring")
        (home / "auth.json").write_text(B, encoding="utf-8")
        real_unlink = Path.unlink

        def unlink(self, missing_ok=False):
            if self.name == "auth.json":
                raise PermissionError("read-only")
            return real_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", unlink)
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            store._write_credentials(A)  # the Keychain took it: no raise
        assert store._last_active_credentials_backend == "keychain"
        assert "read-only" in caplog.text

    def test_config_parsed_once_per_operation(
        self, home: Path, store: CodexCredentialStore, monkeypatch
    ):
        _set_mode(home, "keyring", "[features]\nsecret_auth_storage = false\n")
        calls = []
        real = codex_store.read_codex_config

        def counting():
            calls.append(1)
            return real()

        monkeypatch.setattr(codex_store, "read_codex_config", counting)
        store._write_credentials(A)
        assert len(calls) == 1
        calls.clear()
        store._read_active_credentials()
        assert len(calls) == 1

    def test_write_stores_compact_json_and_deletes_auth_json(
        self, home: Path, store: CodexCredentialStore, block_real_keychain
    ):
        _set_mode(home, "keyring")
        (home / "auth.json").write_text(B, encoding="utf-8")
        store._write_credentials(A)
        stored = block_real_keychain.data[_kc_key(home)]
        assert stored == json.dumps(json.loads(A), separators=(",", ":"))
        assert not (home / "auth.json").exists()
        assert store._last_active_credentials_backend == "keychain"
        assert json.loads(store._read_credentials()) == json.loads(A)

    def test_write_failure_raises_and_keeps_auth_json(
        self, home: Path, store: CodexCredentialStore, monkeypatch: pytest.MonkeyPatch
    ):
        _set_mode(home, "keyring")
        (home / "auth.json").write_text(B, encoding="utf-8")
        monkeypatch.setattr(macos_keychain, "set_password", _raise_keychain)
        with pytest.raises(CredentialWriteError):
            store._write_credentials(A)
        assert (home / "auth.json").read_text(encoding="utf-8") == B

    def test_rollback_restores_original(
        self, home: Path, store: CodexCredentialStore, block_real_keychain
    ):
        _set_mode(home, "keyring")
        compact = cf.auth_json(pretty=False)
        block_real_keychain.data[_kc_key(home)] = compact
        original = store._read_credentials()
        store._write_credentials(B)
        store._write_credentials(original)
        assert block_real_keychain.data[_kc_key(home)] == compact

    def test_api_key_blob_round_trip(self, home: Path, store: CodexCredentialStore):
        _set_mode(home, "keyring")
        blob = cf.api_key_json()
        store._write_credentials(blob)
        assert json.loads(store._read_credentials()) == json.loads(blob)

    def test_failures_do_not_flip_shared_keychain_routing(
        self, home: Path, store: CodexCredentialStore, monkeypatch: pytest.MonkeyPatch
    ):
        """The Codex item's verdict travels in ActiveCredentials; the backup
        half keeps learning Keychain usability from its own ops."""
        _set_mode(home, "keyring")
        monkeypatch.setattr(macos_keychain, "get_password", _raise_keychain)
        monkeypatch.setattr(macos_keychain, "set_password", _raise_keychain)
        store._read_active_credentials()
        with pytest.raises(CredentialWriteError):
            store._write_credentials(A)
        assert store._keychain_usable_cache is None
        assert store._keychain_op_failed is False
        assert store._use_keychain() is True


# -- auto mode (macOS) ------------------------------------------------------------


class TestAutoMode:
    def test_non_object_item_falls_back_to_file_degraded(
        self, home: Path, store: CodexCredentialStore, block_real_keychain
    ):
        # Codex maps an undeserializable item to a load error -> file.
        _set_mode(home, "auto")
        block_real_keychain.data[_kc_key(home)] = "null"
        (home / "auth.json").write_text(B, encoding="utf-8")
        assert store._read_active_credentials() == ActiveCredentials(B, False, True)

    def test_write_refuses_when_residual_item_cannot_be_cleared(
        self, home: Path, store: CodexCredentialStore, block_real_keychain, monkeypatch
    ):
        """A locked Keychain fails the write AND the delete: landing in the
        file would leave the stale item to shadow it later (split brain)."""
        _set_mode(home, "auto")
        block_real_keychain.data[_kc_key(home)] = B
        monkeypatch.setattr(macos_keychain, "set_password", _raise_keychain)
        monkeypatch.setattr(macos_keychain, "delete_password", _raise_keychain)
        with pytest.raises(CredentialWriteError, match="Keychain"):
            store._write_credentials(A)
        assert not (home / "auth.json").exists()
        assert block_real_keychain.data[_kc_key(home)] == B

    def test_write_falls_back_to_file_when_no_item_exists(
        self, home: Path, store: CodexCredentialStore, block_real_keychain, monkeypatch
    ):
        _set_mode(home, "auto")
        monkeypatch.setattr(macos_keychain, "set_password", _raise_keychain)
        store._write_credentials(A)
        assert (home / "auth.json").read_text(encoding="utf-8") == A
        assert store._last_active_credentials_backend == "file"

    def test_keychain_wins_over_file(
        self, home: Path, store: CodexCredentialStore, block_real_keychain
    ):
        _set_mode(home, "auto")
        block_real_keychain.data[_kc_key(home)] = A
        (home / "auth.json").write_text(B, encoding="utf-8")
        assert store._read_active_credentials() == ActiveCredentials(A, False, False)

    def test_absent_item_falls_back_to_file(self, home: Path, store: CodexCredentialStore):
        _set_mode(home, "auto")
        (home / "auth.json").write_text(B, encoding="utf-8")
        assert store._read_active_credentials() == ActiveCredentials(B, False, False)

    def test_absent_everywhere(self, home: Path, store: CodexCredentialStore):
        _set_mode(home, "auto")
        assert store._read_active_credentials() == ActiveCredentials("", False, False)

    def test_unreadable_keychain_falls_back_to_file_degraded(
        self, home: Path, store: CodexCredentialStore, monkeypatch: pytest.MonkeyPatch
    ):
        _set_mode(home, "auto")
        (home / "auth.json").write_text(B, encoding="utf-8")
        monkeypatch.setattr(macos_keychain, "get_password", _raise_keychain)
        assert store._read_active_credentials() == ActiveCredentials(B, False, True)

    def test_unreadable_keychain_and_no_file_is_a_read_error(
        self, home: Path, store: CodexCredentialStore, monkeypatch: pytest.MonkeyPatch
    ):
        _set_mode(home, "auto")
        monkeypatch.setattr(macos_keychain, "get_password", _raise_keychain)
        assert store._read_active_credentials() == ActiveCredentials(None, True, True)

    def test_write_prefers_keychain_and_deletes_file(
        self, home: Path, store: CodexCredentialStore, block_real_keychain
    ):
        _set_mode(home, "auto")
        (home / "auth.json").write_text(B, encoding="utf-8")
        store._write_credentials(A)
        assert json.loads(block_real_keychain.data[_kc_key(home)]) == json.loads(A)
        assert not (home / "auth.json").exists()
        assert store._last_active_credentials_backend == "keychain"

    def test_write_falls_back_to_file_and_clears_residual(
        self, home: Path, store: CodexCredentialStore, block_real_keychain, monkeypatch
    ):
        """Auto reads the Keychain first, so a stale item left behind would
        shadow the file we fell back to."""
        _set_mode(home, "auto")
        block_real_keychain.data[_kc_key(home)] = B
        monkeypatch.setattr(macos_keychain, "set_password", _raise_keychain)
        store._write_credentials(A)
        assert (home / "auth.json").read_text(encoding="utf-8") == A
        assert _kc_key(home) not in block_real_keychain.data
        assert store._last_active_credentials_backend == "file"
        assert store._read_credentials() == A


# -- unsupported modes ------------------------------------------------------------


class TestUnsupportedModes:
    @pytest.mark.parametrize("platform", [Platform.LINUX, Platform.WINDOWS, Platform.WSL])
    @pytest.mark.parametrize("mode", ["keyring", "auto"])
    def test_keyring_modes_off_macos(
        self, home: Path, tmp_path: Path, platform: Platform, mode: str
    ):
        _set_mode(home, mode)
        store = CodexCredentialStore(_Host(tmp_path / "backups", platform))
        with pytest.raises(CodexStoreUnsupported, match='cli_auth_credentials_store = "file"'):
            store._read_active_credentials()
        with pytest.raises(CodexStoreUnsupported):
            store._write_credentials(A)
        assert not (home / "auth.json").exists()

    @pytest.mark.parametrize("mode", ["ephemeral", "bogus"])
    def test_other_modes_everywhere(self, home: Path, store: CodexCredentialStore, mode: str):
        _set_mode(home, mode)
        with pytest.raises(CodexStoreUnsupported, match=mode):
            store._read_credentials()
        with pytest.raises(CodexStoreUnsupported):
            store._write_credentials(A)
        assert not (home / "auth.json").exists()

    def test_secrets_backend_is_unsupported(self, home: Path, store: CodexCredentialStore):
        # [features] secret_auth_storage moves keyring auth into an encrypted
        # $CODEX_HOME/secrets file (core/src/config/auth_keyring.rs:110-118).
        _set_mode(home, "keyring", "[features]\nsecret_auth_storage = true\n")
        with pytest.raises(CodexStoreUnsupported, match="secret_auth_storage"):
            store._read_active_credentials()

    def test_is_a_claude_switch_error(self):
        # The CLI's `except ClaudeSwitchError` prints it as a clean error.
        assert issubclass(CodexStoreUnsupported, ClaudeSwitchError)


# -- post-write verification ------------------------------------------------------


class TestPostWriteVerification:
    @pytest.mark.parametrize(
        "exc",
        [CodexStoreUnsupported("mode changed"), ValueError("bad"), OSError("gone")],
    )
    def test_never_raises_after_the_store_changed(
        self, home: Path, store: CodexCredentialStore, monkeypatch, exc
    ):
        """The switcher records the write for rollback only once
        _write_credentials returns; a raise here would strand it."""

        def boom(mode):
            raise exc

        monkeypatch.setattr(store, "_read_live", boom)
        store._write_credentials(A)
        assert (home / "auth.json").read_text(encoding="utf-8") == A
        assert store._last_active_credentials_backend == "file"

    def test_hybrid_blob_warns(self, home: Path, store: CodexCredentialStore, caplog):
        """A Codex refresh racing our write leaves one account's account_id
        beside another's tokens (codex-spec gotcha 5)."""
        hybrid = cf.auth_dict(email="b@example.com", account_id="acct-b", user_id="user-b")
        hybrid["tokens"]["account_id"] = "acct-a"
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            store._write_credentials(json.dumps(hybrid))
        assert "acct-a" in caplog.text and "acct-b" in caplog.text

    def test_race_after_write_is_detected(
        self, home: Path, store: CodexCredentialStore, monkeypatch, caplog
    ):
        real_replace = codex_store.replace_with_retry

        def replace_then_codex_writes(src, dst):
            real_replace(src, dst)
            raced = cf.auth_dict(email="b@example.com", account_id="acct-b", user_id="user-b")
            raced["tokens"]["account_id"] = "acct-a"
            Path(dst).write_text(json.dumps(raced), encoding="utf-8")

        monkeypatch.setattr(codex_store, "replace_with_retry", replace_then_codex_writes)
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            store._write_credentials(B)  # no raise: verification only warns
        assert "acct-a" in caplog.text

    def test_consistent_blob_is_silent(self, home: Path, store: CodexCredentialStore, caplog):
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            store._write_credentials(A)
        assert caplog.text == ""


def test_switcher_delegators_reach_the_codex_store(home: Path, temp_home: Path):
    """What Task 4's ``_make_store`` override wires up: the switcher's own
    read/write delegators and backend proxy land on the Codex live store."""
    from claude_swap.switcher import ClaudeAccountSwitcher

    class _S(ClaudeAccountSwitcher):
        backup_subdir = "codex"
        backup_keychain_service = "claude-swap-codex"
        run_legacy_migrations = False

        def _make_store(self):
            return CodexCredentialStore(self)

    s = _S()
    s._write_credentials(A)
    assert (home / "auth.json").read_text(encoding="utf-8") == A
    assert s._read_credentials() == A
    assert s._read_active_credentials() == ActiveCredentials(A, False, False)
    assert s._last_active_credentials_backend == "file"


def test_backup_half_uses_the_codex_service(
    tmp_path: Path, store: CodexCredentialStore, block_real_keychain
):
    """Inherited unchanged: per-slot backups land under the host's service."""
    store._kc_write_backup("1", "a@example.com", A)
    assert block_real_keychain.data[("claude-swap-codex", "account-1-a@example.com")] == A
