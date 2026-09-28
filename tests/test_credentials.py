"""``CredentialStore``: the active credential must stay on one profile.

The identity read honors ``CLAUDE_CONFIG_DIR`` (``paths.get_claude_config_home``)
while the Keychain read used a hardcoded service name that does not. Pairing one
profile's identity with another profile's credential is silent, and every
consumer of the active read inherits it.

The fix resolves the Keychain item the way claude does for the same environment
(``session.keychain_service_name``, the same derivation ``delete``, the
session read and capture already use) rather than skipping the Keychain under a
custom profile. Skipping would trade a wrong answer for a missing one: claude
writes rotations keychain-only on macOS, so a custom profile frequently has no
plaintext file at all and would render as "no credentials" while logged in.

The write side has the same split (#206) and is answered the other way round:
cswap does not author claude's hashed item, so under a custom profile it stops
writing the unsuffixed one — which belongs to a login it was not asked to touch
— and seeds the profile's own ``.credentials.json`` instead.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from claude_swap import macos_keychain
from claude_swap.credentials import (
    CLAUDE_CODE_KEYCHAIN_SERVICE,
    CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE,
    CredentialStore,
)
from claude_swap.models import Platform
from claude_swap.session import keychain_service_name


class _Host:
    """Minimal ``_StoreHost``: data only, read at call time."""

    def __init__(self, credentials_dir: Path):
        self.platform = Platform.MACOS
        self.credentials_dir = credentials_dir
        self._logger = logging.getLogger("test")


DEFAULT_PROFILE_CREDS = json.dumps({
    "claudeAiOauth": {
        "accessToken": "sk-default-profile",
        "refreshToken": "rt-default-profile",
        "expiresAt": 9999999999000,
    }
})

CUSTOM_PROFILE_CREDS = json.dumps({
    "claudeAiOauth": {
        "accessToken": "sk-custom-profile",
        "refreshToken": "rt-custom-profile",
        "expiresAt": 9999999999000,
    }
})

SECURE_PROFILE_CREDS = json.dumps({
    "claudeAiOauth": {
        "accessToken": "sk-secure-profile",
        "refreshToken": "rt-secure-profile",
        "expiresAt": 9999999999000,
    }
})

TARGET_SLOT_CREDS = json.dumps({
    "claudeAiOauth": {
        "accessToken": "sk-target-slot",
        "refreshToken": "rt-target-slot",
        "expiresAt": 9999999999000,
    }
})


def _keychain(mapping: dict[str, str], seen: list[str]):
    """A fake Keychain: only the listed services exist, and record every probe.

    Anything unlisted returns ``None``, which is claude's rc-44 "absent item"
    signal — the case that legitimately falls through to the plaintext file.
    """

    def fake_get_password(service: str, account: str):
        seen.append(service)
        return mapping.get(service)

    return fake_get_password


class TestActiveReadStaysOnOneProfile:
    def test_custom_config_dir_does_not_return_the_default_keychain_item(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The hardcoded ``Claude Code-credentials`` item belongs to the DEFAULT
        profile. Under a custom ``CLAUDE_CONFIG_DIR`` it must never answer, or
        the store hands back one account's token against another's identity."""
        custom = tmp_path / "custom-profile"
        custom.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))
        monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)

        seen: list[str] = []
        monkeypatch.setattr(
            "claude_swap.macos_keychain.get_password",
            _keychain(
                {
                    CLAUDE_CODE_KEYCHAIN_SERVICE: DEFAULT_PROFILE_CREDS,
                    CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE: "sk-ant-api-default",
                },
                seen,
            ),
        )

        result = CredentialStore(_Host(tmp_path / "backups"))._read_active_credentials()

        assert CLAUDE_CODE_KEYCHAIN_SERVICE not in seen, (
            f"read the default profile's OAuth item: {seen}"
        )
        assert CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE not in seen, (
            f"read the default profile's managed-key item: {seen}"
        )
        assert result.value != DEFAULT_PROFILE_CREDS
        assert not result.value

    def test_custom_config_dir_reads_its_own_hashed_keychain_item(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The regression a plain skip would introduce.

        On macOS claude writes rotations keychain-only, so a live custom profile
        commonly has a hashed item and NO plaintext file. Skipping the Keychain
        would report "no credentials" for a profile that is logged in; the
        redirect returns the profile's real credential."""
        custom = tmp_path / "custom-profile"
        custom.mkdir()  # deliberately no .credentials.json
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))
        monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)

        seen: list[str] = []
        monkeypatch.setattr(
            "claude_swap.macos_keychain.get_password",
            _keychain(
                {
                    keychain_service_name(str(custom)): CUSTOM_PROFILE_CREDS,
                    CLAUDE_CODE_KEYCHAIN_SERVICE: DEFAULT_PROFILE_CREDS,
                },
                seen,
            ),
        )

        result = CredentialStore(_Host(tmp_path / "backups"))._read_active_credentials()

        assert result.value == CUSTOM_PROFILE_CREDS
        assert seen == [keychain_service_name(str(custom))]

    def test_custom_config_dir_falls_back_to_its_own_credentials_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """An ABSENT hashed item (rc 44) is claude's own signal to read the
        plaintext seed — and that file is unambiguously this profile's."""
        custom = tmp_path / "custom-profile"
        custom.mkdir()
        (custom / ".credentials.json").write_text(
            CUSTOM_PROFILE_CREDS, encoding="utf-8"
        )
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))
        monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)

        seen: list[str] = []
        monkeypatch.setattr(
            "claude_swap.macos_keychain.get_password",
            _keychain({CLAUDE_CODE_KEYCHAIN_SERVICE: DEFAULT_PROFILE_CREDS}, seen),
        )

        store = CredentialStore(_Host(tmp_path / "backups"))
        assert store._read_active_credentials().value == CUSTOM_PROFILE_CREDS
        assert CLAUDE_CODE_KEYCHAIN_SERVICE not in seen

    def test_default_profile_still_reads_the_unsuffixed_item(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The common case is unchanged. With no ``CLAUDE_CONFIG_DIR`` the
        unsuffixed item IS this profile's credential."""
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

        seen: list[str] = []
        monkeypatch.setattr(
            "claude_swap.macos_keychain.get_password",
            _keychain({CLAUDE_CODE_KEYCHAIN_SERVICE: DEFAULT_PROFILE_CREDS}, seen),
        )

        store = CredentialStore(_Host(tmp_path / "backups"))
        assert store._read_active_credentials().value == DEFAULT_PROFILE_CREDS
        assert seen == [CLAUDE_CODE_KEYCHAIN_SERVICE]

    def test_config_dir_equal_to_the_default_falls_back_to_the_unsuffixed_item(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Setting ``CLAUDE_CONFIG_DIR`` to the default profile explicitly is not
        a custom profile. Claude hashes the exported string, so the hashed name
        is tried first, but a user who has always used the default profile may
        only have the unsuffixed item — so that fallback must remain."""
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        default = tmp_path / ".claude"
        default.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(default))
        monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)

        seen: list[str] = []
        monkeypatch.setattr(
            "claude_swap.macos_keychain.get_password",
            _keychain({CLAUDE_CODE_KEYCHAIN_SERVICE: DEFAULT_PROFILE_CREDS}, seen),
        )

        store = CredentialStore(_Host(tmp_path / "backups"))
        assert store._read_active_credentials().value == DEFAULT_PROFILE_CREDS
        assert seen == [
            keychain_service_name(str(default)),
            CLAUDE_CODE_KEYCHAIN_SERVICE,
        ]


class TestActiveWriteStaysOnOneProfile:
    """The write has to land in the profile the read resolves, and nowhere else.

    The read was redirected to the active profile's own item while the write
    kept the fixed ``Claude Code-credentials`` name, so under a custom profile
    the two halves of the store addressed different accounts (#206).
    """

    def test_a_write_round_trips_through_the_read_under_a_custom_profile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, block_real_keychain
    ):
        """Write X, read X — the store's most basic contract."""
        custom = tmp_path / "custom-profile"
        custom.mkdir()  # keychain-only profile: claude writes rotations there
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))
        monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)

        account = macos_keychain.keychain_account_name()
        block_real_keychain.data[
            (keychain_service_name(str(custom)), account)
        ] = CUSTOM_PROFILE_CREDS

        store = CredentialStore(_Host(tmp_path / "backups"))
        store._write_credentials(TARGET_SLOT_CREDS)

        assert store._read_credentials() == TARGET_SLOT_CREDS

    def test_a_write_under_a_custom_profile_leaves_the_default_login_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, block_real_keychain
    ):
        """The default profile's Keychain item is another account's only copy.

        The switch backs up the credential it displaces by reading the ACTIVE
        profile, so the unsuffixed item was never copied anywhere before being
        overwritten — a refresh token destroyed with nothing to restore from.
        """
        custom = tmp_path / "custom-profile"
        custom.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))
        monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)

        account = macos_keychain.keychain_account_name()
        block_real_keychain.data[
            (CLAUDE_CODE_KEYCHAIN_SERVICE, account)
        ] = DEFAULT_PROFILE_CREDS
        block_real_keychain.data[
            (keychain_service_name(str(custom)), account)
        ] = CUSTOM_PROFILE_CREDS

        store = CredentialStore(_Host(tmp_path / "backups"))
        store._write_credentials(TARGET_SLOT_CREDS)

        assert (
            block_real_keychain.data.get((CLAUDE_CODE_KEYCHAIN_SERVICE, account))
            == DEFAULT_PROFILE_CREDS
        ), "the default profile's login was overwritten by another profile's switch"

    def test_a_custom_profile_gets_a_seed_file_with_no_item_shadowing_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, block_real_keychain
    ):
        """Claude reads its hashed item before the seed, so leaving the profile's
        own item in place would keep serving the outgoing account however
        correct the seed is — the switch has to retire it and reseed."""
        custom = tmp_path / "custom-profile"
        custom.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))
        monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)

        account = macos_keychain.keychain_account_name()
        hashed = keychain_service_name(str(custom))
        block_real_keychain.data[(hashed, account)] = CUSTOM_PROFILE_CREDS

        store = CredentialStore(_Host(tmp_path / "backups"))
        store._write_credentials(TARGET_SLOT_CREDS)

        seed = custom / ".credentials.json"
        assert seed.read_text(encoding="utf-8") == TARGET_SLOT_CREDS
        assert (hashed, account) not in block_real_keychain.data
        assert store._last_active_credentials_backend == "file"

    def test_config_dir_equal_to_the_default_does_not_write_the_unsuffixed_item(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, block_real_keychain
    ):
        """Claude hashes whatever is exported, so an explicit ``CLAUDE_CONFIG_DIR``
        naming the default profile reads the hashed item first. The unsuffixed
        item survives as the read's compatibility fallback, which must not be
        mistaken for "this environment's store"."""
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        default = tmp_path / ".claude"
        default.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(default))
        monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)

        account = macos_keychain.keychain_account_name()
        block_real_keychain.data[
            (keychain_service_name(str(default)), account)
        ] = DEFAULT_PROFILE_CREDS

        store = CredentialStore(_Host(tmp_path / "backups"))
        store._write_credentials(TARGET_SLOT_CREDS)

        assert store._read_credentials() == TARGET_SLOT_CREDS
        assert (CLAUDE_CODE_KEYCHAIN_SERVICE, account) not in block_real_keychain.data

    def test_the_default_profile_still_writes_the_unsuffixed_item(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, block_real_keychain
    ):
        """The common case is unchanged: with no ``CLAUDE_CONFIG_DIR`` the
        unsuffixed item IS this environment's store, so the Keychain stays the
        primary backend and no plaintext credential appears on disk."""
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        (tmp_path / ".claude").mkdir(exist_ok=True)

        account = macos_keychain.keychain_account_name()
        store = CredentialStore(_Host(tmp_path / "backups"))
        store._write_credentials(TARGET_SLOT_CREDS)

        assert (
            block_real_keychain.data[(CLAUDE_CODE_KEYCHAIN_SERVICE, account)]
            == TARGET_SLOT_CREDS
        )
        assert store._last_active_credentials_backend == "keychain"
        assert not (tmp_path / ".claude" / ".credentials.json").exists()


class TestSecureStorageOverride:
    """``CLAUDE_SECURESTORAGE_CONFIG_DIR`` takes precedence when *defined*.

    Claude 2.1.220+ resolves secure storage from it and only falls back to
    ``CLAUDE_CONFIG_DIR`` when it is undefined. ``_read_capture_credentials``
    already reads it this way; the active read has to agree, or the two disagree
    about which profile they are looking at.
    """

    def test_defined_and_empty_selects_the_default_store(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Defined-but-empty means the DEFAULT secure store, even with a custom
        ``CLAUDE_CONFIG_DIR``. Here the unsuffixed item is the correct read, so
        a guard keyed only on ``CLAUDE_CONFIG_DIR`` would wrongly return
        nothing."""
        custom = tmp_path / "custom-profile"
        custom.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))
        monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", "")

        seen: list[str] = []
        monkeypatch.setattr(
            "claude_swap.macos_keychain.get_password",
            _keychain({CLAUDE_CODE_KEYCHAIN_SERVICE: DEFAULT_PROFILE_CREDS}, seen),
        )

        store = CredentialStore(_Host(tmp_path / "backups"))
        assert store._read_active_credentials().value == DEFAULT_PROFILE_CREDS
        assert seen == [CLAUDE_CODE_KEYCHAIN_SERVICE]

    def test_defined_and_set_selects_that_stores_hashed_item(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A defined, non-empty value names the only store claude will read."""
        custom = tmp_path / "custom-profile"
        custom.mkdir()
        secure = tmp_path / "secure-profile"
        secure.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))
        monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", str(secure))

        seen: list[str] = []
        monkeypatch.setattr(
            "claude_swap.macos_keychain.get_password",
            _keychain(
                {
                    keychain_service_name(str(secure)): SECURE_PROFILE_CREDS,
                    keychain_service_name(str(custom)): CUSTOM_PROFILE_CREDS,
                    CLAUDE_CODE_KEYCHAIN_SERVICE: DEFAULT_PROFILE_CREDS,
                },
                seen,
            ),
        )

        store = CredentialStore(_Host(tmp_path / "backups"))
        assert store._read_active_credentials().value == SECURE_PROFILE_CREDS
        assert seen == [keychain_service_name(str(secure))]
