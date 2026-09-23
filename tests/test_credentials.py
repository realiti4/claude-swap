"""``CredentialStore``: the active-credential read must stay on one profile.

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
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

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


class TestTheManagedReadVerdictIsPerThread:
    def test_a_sibling_read_does_not_reset_this_thread_s_verdict(self, tmp_path):
        """One store, several readers, and this verdict spans statements.

        The TUI's two refresh lanes, the auto engine's worker and the fetch
        pool share a `CredentialStore`. As a plain attribute, a sibling
        entering `_read_active_credentials` cleared it mid-flight and the
        first reader then returned `("", False, False)` -- a live, billing
        managed key reported as a genuinely empty slot.

        Swapping `threading.local()` for a shared attribute left the whole
        suite green, so nothing but this stands between that race and a
        future simplification.
        """
        import threading as _threading

        store = CredentialStore(_Host(tmp_path / "backups"))

        # THROUGH `_read_active_credentials`, not by poking the attribute.
        # Reading the flag directly only pins its TYPE: leaving
        # `threading.local()` in place and pointing the three real uses at a
        # plain attribute restores the race and a type check stays green.
        inside = _threading.Event()
        release = _threading.Event()
        real_managed = store._read_managed_key

        def blocking_managed():
            store._managed_read_tls.failed = True   # what a failed read records
            inside.set()
            release.wait(5)
            return ""

        store._read_managed_key = blocking_managed
        out = {}

        def reader_a():
            out["a"] = store._read_active_credentials()

        a = _threading.Thread(target=reader_a)
        a.start()
        assert inside.wait(5), "premise: thread A never reached the managed read"

        store._read_managed_key = real_managed
        store._read_active_credentials()            # a complete sibling read
        release.set()
        a.join(5)

        assert "a" in out, "premise: thread A never finished"
        assert out["a"].keychain_unavailable is True, (
            "a sibling read entering `_read_active_credentials` cleared this "
            "thread's verdict mid-flight, so a live keychain failure came "
            "back as a genuinely empty slot"
        )


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


class TestTheClearReachesEveryStoreTheReadDoes:
    """The read walks `_active_oauth_keychain_services()`; the delete named
    one service.

    Under a custom profile the read resolves a SUFFIXED item while the delete
    removed the unsuffixed one, returned True (a clean rc-44 is a success),
    and the landing then believed the live store was empty. The two must
    resolve the same set or the delete's verdict is about a different item
    than the read's.
    """

    def test_the_delete_covers_the_same_services_the_read_walks(
        self, temp_home, monkeypatch
    ):
        from claude_swap import credentials as creds_mod

        class _Host:
            platform = Platform.MACOS
            _logger = logging.getLogger("claude-swap")

        store = CredentialStore(_Host())
        wanted = ["claude-suffixed", "Claude Code"]
        monkeypatch.setattr(
            creds_mod, "_active_oauth_keychain_services", lambda: wanted)

        deleted: list[str] = []
        monkeypatch.setattr(
            creds_mod.macos_keychain, "keychain_account_name", lambda: "acct")
        monkeypatch.setattr(
            creds_mod.macos_keychain, "delete_password",
            lambda service, account: deleted.append(service))

        assert store._delete_active_keychain_entry() is True
        assert deleted == wanted, (
            f"the read walks {wanted} and the delete touched {deleted} — a "
            "survivor in a store the read would have found keeps "
            "authenticating under the new slot's name"
        )


class TestTheTwoLiveStoresCanDisagreeAndTheFRESHERWins:
    """macOS has two live stores and they DO come apart.

    MEASURED on a host where every login appeared to vanish:

        plaintext file   refreshTokenExpiresAt 09-26 18:01   (the login just made)
        Keychain         refreshTokenExpiresAt 09-06 15:28   (weeks older)
        what cswap read  the Keychain

    The read took the Keychain and stopped, so the login was never seen — and
    cswap then wrote what it had read back over both stores, destroying it.
    The plaintext file is not merely a fallback for an EMPTY Keychain; either
    side can be the newer one, because a Keychain write that fails once sends
    Claude Code to the file while later Keychain READS keep succeeding with the
    older item.

    `refreshTokenExpiresAt` IS the comparison, and `expiresAt` is not: a
    refresh moves the access token on every poll and does not extend the
    refresh lifetime, so comparing access expiry would flip backends on
    ordinary rotation. Only a fresh login mints a later refresh lifetime.
    """

    @staticmethod
    def _creds(tag: str, refresh_exp: int) -> str:
        return json.dumps({"claudeAiOauth": {
            "accessToken": "sk-" + tag, "refreshToken": "rt-" + tag,
            "expiresAt": 9999999999000, "refreshTokenExpiresAt": refresh_exp}})

    def _store(self, tmp_path, monkeypatch, kc: str, fl: str):
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr(
            "claude_swap.macos_keychain.get_password",
            _keychain({CLAUDE_CODE_KEYCHAIN_SERVICE: kc} if kc else {}, []),
        )
        d = tmp_path / ".claude"
        d.mkdir(parents=True, exist_ok=True)
        if fl:
            (d / ".credentials.json").write_text(fl, encoding="utf-8")
        return CredentialStore(_Host(tmp_path / "backups"))

    def test_a_newer_login_in_the_FILE_wins(self, tmp_path, monkeypatch):
        """THE MEASURED CASE."""
        kc = self._creds("keychain-old", 1_000)
        fl = self._creds("file-login", 9_000)
        got = self._store(tmp_path, monkeypatch, kc, fl)._read_active_credentials()
        assert got.value == fl, (
            "the Keychain's older item won and the login in the file was never "
            "read — which is how a login disappears without a trace"
        )

    def test_CONTROL_a_newer_KEYCHAIN_still_wins(self, tmp_path, monkeypatch):
        """The other direction, and the ordinary one: CC writes rotations to
        the Keychain on macOS, so a stale file must not win."""
        kc = self._creds("keychain-login", 9_000)
        fl = self._creds("file-old", 1_000)
        got = self._store(tmp_path, monkeypatch, kc, fl)._read_active_credentials()
        assert got.value == kc

    def test_CONTROL_equal_lifetimes_keep_the_keychain(self, tmp_path, monkeypatch):
        """Same generation in both — the steady state on a healthy host. No
        reason to change which backend answers, and changing it would churn."""
        same = self._creds("same", 5_000)
        got = self._store(tmp_path, monkeypatch, same, same)._read_active_credentials()
        assert got.value == same

    def test_CONTROL_an_undated_file_cannot_win(self, tmp_path, monkeypatch):
        """No `refreshTokenExpiresAt` is no evidence of a newer login. A row
        that could win on absence would hand the older bytes the decision."""
        kc = self._creds("keychain", 1_000)
        fl = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-undated", "refreshToken": "rt-undated",
            "expiresAt": 9999999999000}})
        got = self._store(tmp_path, monkeypatch, kc, fl)._read_active_credentials()
        assert got.value == kc

    def test_CONTROL_an_empty_keychain_still_falls_back(self, tmp_path, monkeypatch):
        """The original fallback must survive: nothing in the Keychain means
        the file is the only source, dated or not."""
        fl = self._creds("file-only", 1_000)
        got = self._store(tmp_path, monkeypatch, "", fl)._read_active_credentials()
        assert got.value == fl

    def test_a_same_lineage_rotation_jittered_by_ms_keeps_the_keychain(
        self, tmp_path, monkeypatch
    ):
        """One login, two generations of the SAME lineage: the server
        re-mints `refreshTokenExpiresAt` on every refresh with sub-second
        jitter, so a file stamp a few hundred ms later than the Keychain's is
        not a newer login. The Keychain here also carries the fresher access
        token (later `expiresAt`); the file's is already expired — the
        opposite of what a real newer login would show."""
        kc_refresh = 1_790_380_487_015
        fl_refresh = kc_refresh + 427
        kc = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-keychain", "refreshToken": "rt-keychain",
            "expiresAt": 1_788_399_592_015,
            "refreshTokenExpiresAt": kc_refresh}})
        fl = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-file", "refreshToken": "rt-file",
            "expiresAt": 1_788_371_089_449,
            "refreshTokenExpiresAt": fl_refresh}})
        got = self._store(tmp_path, monkeypatch, kc, fl)._read_active_credentials()
        assert got.value == kc, (
            "a 427ms-later file stamp from the same rotation lineage won "
            "over the Keychain's current generation"
        )

    def test_a_same_lineage_rotation_jittered_by_ms_lets_the_fresher_generation_win(
        self, tmp_path, monkeypatch
    ):
        """Same rotation lineage, jittered by 427ms — but this time the file
        also carries the LATER access-token expiry, the signature of the
        newer generation of the SAME login rather than an older or newer one.
        Both stores hold the same login; only ``expiresAt`` says which
        generation of it is current, and the file's must win."""
        kc_refresh = 1_790_380_487_015
        fl_refresh = kc_refresh + 427
        kc = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-superseded", "refreshToken": "rt-superseded",
            "expiresAt": 1_788_371_089_449,
            "refreshTokenExpiresAt": kc_refresh}})
        fl = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-current", "refreshToken": "rt-current",
            "expiresAt": 1_788_399_592_015,
            "refreshTokenExpiresAt": fl_refresh}})
        got = self._store(tmp_path, monkeypatch, kc, fl)._read_active_credentials()
        assert got.value == fl, (
            "the Keychain's superseded generation won even though the file "
            "held the later generation of the same login"
        )

    def test_a_year_older_file_login_cannot_win_on_a_later_expiresAt(
        self, tmp_path, monkeypatch
    ):
        """The file's stamp is not a few hundred ms off — it is a YEAR
        older, far outside the same-lineage jitter, so it must never reach
        the ``expiresAt`` tiebreak at all. A later ``expiresAt`` on that
        stale login (e.g. a long-lived token minted at the time) must not
        let it win over the Keychain's current login."""
        kc_refresh = 1_790_380_487_015
        kc_exp = 1_788_399_592_015
        fl_refresh = kc_refresh - 31_536_000_000  # 365 days earlier
        fl_exp = kc_exp + 3_600_000  # 1 hour later than the Keychain's
        kc = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-keychain", "refreshToken": "rt-keychain",
            "expiresAt": kc_exp,
            "refreshTokenExpiresAt": kc_refresh}})
        fl = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-file", "refreshToken": "rt-file",
            "expiresAt": fl_exp,
            "refreshTokenExpiresAt": fl_refresh}})
        got = self._store(tmp_path, monkeypatch, kc, fl)._read_active_credentials()
        assert got.value == kc, (
            "a year-older login in the file won because its expiresAt was "
            "later, even though it is nowhere near the same-lineage jitter"
        )

    def test_a_corrupt_plaintext_file_does_not_crash_the_read(
        self, tmp_path, monkeypatch
    ):
        """``_stamp``'s docstring promises "any read or parse failure answers
        None", but ``except (TypeError, ValueError)`` misses the
        ``AttributeError`` a non-dict JSON scalar (``42``) raises on
        ``.get`` -- so a corrupt plaintext file crashed ``_read_active_credentials``
        itself, the ordinary read path every consumer of the active
        credential goes through, not only the later credentials-sync helper."""
        kc = self._creds("keychain-only", 1_000)
        got = self._store(tmp_path, monkeypatch, kc, "42")._read_active_credentials()
        assert got.value == kc, (
            "DEFECT: a corrupt plaintext file crashed the read instead of "
            "just losing the freshness comparison"
        )


class TestTheLineageJitterToleranceIsPublic:
    """A second reader outside the package (the requirements gate, on the
    tool's interpreter) must import this package's jitter decision instead
    of carrying its own copy — so the constant must be importable by name,
    not underscore-private."""

    def test_the_constant_is_importable_under_its_public_name(self):
        from claude_swap.credentials import LINEAGE_STAMP_JITTER_MS

        assert LINEAGE_STAMP_JITTER_MS == 5_000

    def test_newer_login_treats_the_public_constant_as_the_boundary(self):
        from claude_swap.credentials import LINEAGE_STAMP_JITTER_MS, newer_login

        assert not newer_login(LINEAGE_STAMP_JITTER_MS, 0)
        assert newer_login(LINEAGE_STAMP_JITTER_MS + 1, 0)
