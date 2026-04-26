"""Tests for the export/import (transfer) module."""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap.exceptions import TransferError
from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.transfer import export_accounts, import_accounts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


SAMPLE_CREDS = {"accessToken": "tok-1", "refreshToken": "rtok-1", "expiresAt": 9999}
SAMPLE_CONFIG = {
    "oauthAccount": {
        "emailAddress": "user@example.com",
        "accountUuid": "acct-uuid",
        "organizationUuid": "org-uuid",
        "organizationName": "Acme",
    }
}


def _linux_switcher(home: Path) -> ClaudeAccountSwitcher:
    """Create a switcher with file-based (Linux) credential storage."""
    s = ClaudeAccountSwitcher()
    s.platform = Platform.LINUX
    s._setup_directories()
    s._init_sequence_file()
    return s


def _seed_account(
    switcher: ClaudeAccountSwitcher,
    num: int,
    email: str,
    org_uuid: str = "",
    org_name: str = "",
    creds: dict | None = None,
    config: dict | None = None,
) -> None:
    """Write an account to backup + sequence.json."""
    creds_obj = creds if creds is not None else {**SAMPLE_CREDS, "_marker": email}
    config_obj = config if config is not None else {
        "oauthAccount": {
            "emailAddress": email,
            "accountUuid": f"acct-{num}",
            "organizationUuid": org_uuid,
            "organizationName": org_name,
        }
    }
    switcher._write_account_credentials(str(num), email, json.dumps(creds_obj))
    switcher._write_account_config(str(num), email, json.dumps(config_obj))

    data = switcher._get_sequence_data() or {
        "activeAccountNumber": None,
        "lastUpdated": "",
        "sequence": [],
        "accounts": {},
    }
    data["accounts"][str(num)] = {
        "email": email,
        "uuid": f"acct-{num}",
        "organizationUuid": org_uuid,
        "organizationName": org_name,
        "added": "2024-01-01T00:00:00Z",
    }
    if num not in data["sequence"]:
        data["sequence"].append(num)
        data["sequence"].sort()
    if data["activeAccountNumber"] is None:
        data["activeAccountNumber"] = num
    switcher._write_json(switcher.sequence_file, data)


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_export_import_round_trip(self, temp_home: Path):
        src = _linux_switcher(temp_home)
        _seed_account(src, 1, "alice@example.com", "org-a", "Org A")
        _seed_account(src, 2, "bob@example.com")

        out_file = temp_home / "backup.cswap"
        export_accounts(src, str(out_file))

        # Sanity-check the file
        assert out_file.exists()
        envelope = json.loads(out_file.read_text())
        assert envelope["version"] == 1
        assert envelope["encrypted"] is False
        assert len(envelope["accounts"]) == 2
        assert {a["email"] for a in envelope["accounts"]} == {
            "alice@example.com",
            "bob@example.com",
        }

        # Import into a fresh home
        dst_home = temp_home.parent / "dst"
        dst_home.mkdir()
        with patch("pathlib.Path.home", return_value=dst_home):
            with patch.dict(os.environ, {"HOME": str(dst_home)}):
                dst = _linux_switcher(dst_home)
                import_accounts(dst, str(out_file))

                seq = dst._get_sequence_data()
                assert seq is not None
                assert set(seq["accounts"].keys()) == {"1", "2"}
                assert seq["accounts"]["1"]["email"] == "alice@example.com"
                assert seq["accounts"]["1"]["organizationUuid"] == "org-a"

                # Credentials JSON parses and contains the marker
                creds_text = dst._read_account_credentials("1", "alice@example.com")
                assert json.loads(creds_text)["_marker"] == "alice@example.com"

    def test_active_state_carried_but_not_applied(self, temp_home: Path):
        src = _linux_switcher(temp_home)
        _seed_account(src, 1, "a@example.com")
        _seed_account(src, 2, "b@example.com")
        # Force active to slot 2
        data = src._get_sequence_data()
        data["activeAccountNumber"] = 2
        src._write_json(src.sequence_file, data)

        out_file = temp_home / "backup.cswap"
        export_accounts(src, str(out_file))
        envelope = json.loads(out_file.read_text())
        assert envelope["activeAccountNumber"] == 2

        # Import into a destination with a different active account
        dst_home = temp_home.parent / "dst"
        dst_home.mkdir()
        with patch("pathlib.Path.home", return_value=dst_home):
            with patch.dict(os.environ, {"HOME": str(dst_home)}):
                dst = _linux_switcher(dst_home)
                _seed_account(dst, 9, "local@example.com")
                data = dst._get_sequence_data()
                data["activeAccountNumber"] = 9
                dst._write_json(dst.sequence_file, data)

                import_accounts(dst, str(out_file))
                final = dst._get_sequence_data()
                assert final["activeAccountNumber"] == 9  # untouched


# ---------------------------------------------------------------------------
# Selective export
# ---------------------------------------------------------------------------


class TestSelectiveExport:
    def test_export_single_by_number(self, temp_home: Path):
        s = _linux_switcher(temp_home)
        _seed_account(s, 1, "a@example.com")
        _seed_account(s, 2, "b@example.com")

        out = temp_home / "one.cswap"
        export_accounts(s, str(out), account="2")
        envelope = json.loads(out.read_text())
        assert len(envelope["accounts"]) == 1
        assert envelope["accounts"][0]["email"] == "b@example.com"

    def test_export_single_by_email(self, temp_home: Path):
        s = _linux_switcher(temp_home)
        _seed_account(s, 1, "a@example.com")
        _seed_account(s, 2, "b@example.com")

        out = temp_home / "one.cswap"
        export_accounts(s, str(out), account="a@example.com")
        envelope = json.loads(out.read_text())
        assert envelope["accounts"][0]["email"] == "a@example.com"

    def test_export_unknown_number_raises(self, temp_home: Path):
        s = _linux_switcher(temp_home)
        _seed_account(s, 1, "a@example.com")

        with pytest.raises(TransferError, match="account not found"):
            export_accounts(s, str(temp_home / "x.cswap"), account="999")

    def test_export_no_accounts_raises(self, temp_home: Path):
        s = _linux_switcher(temp_home)
        with pytest.raises(TransferError, match="no accounts to export"):
            export_accounts(s, str(temp_home / "x.cswap"))


# ---------------------------------------------------------------------------
# Conflict / force semantics
# ---------------------------------------------------------------------------


class TestConflictPolicy:
    def test_skip_when_account_exists_without_force(self, temp_home: Path, capsys):
        src = _linux_switcher(temp_home)
        _seed_account(src, 1, "alice@example.com", "org-a")
        out = temp_home / "b.cswap"
        export_accounts(src, str(out))

        # Re-import into the same home → should skip
        import_accounts(src, str(out), force=False)
        captured = capsys.readouterr()
        assert "Skipped alice@example.com" in captured.err
        # Sequence still has only slot 1
        seq = src._get_sequence_data()
        assert list(seq["accounts"].keys()) == ["1"]

    def test_force_overwrites_existing_slot_in_place(
        self, temp_home: Path, capsys
    ):
        """Critical: --force updates the local matching slot, NOT the exported slot.

        Setup: local slot 3 has alice@x.com. Local slot 1 has bob@x.com (different
        account). Exported file says alice's slot was 1. After --force import:
        - alice's data MUST be written to slot 3 (in place)
        - bob (slot 1) MUST be untouched
        """
        s = _linux_switcher(temp_home)
        _seed_account(s, 3, "alice@example.com", "org-a", "Org A")
        # Export alice while she's at slot 3 (so exported "number" = 3)
        out = temp_home / "alice.cswap"
        export_accounts(s, str(out), account="3")
        # Hand-edit envelope to claim slot 1 (simulates an export from another machine)
        env = json.loads(out.read_text())
        env["accounts"][0]["number"] = 1
        # Bump the credential so we can verify overwrite happened
        env["accounts"][0]["credentials"]["_marker"] = "ALICE_NEW"
        out.write_text(json.dumps(env))

        # Add bob to slot 1 locally
        _seed_account(s, 1, "bob@example.com")

        bob_creds_before = s._read_account_credentials("1", "bob@example.com")

        import_accounts(s, str(out), force=True)

        captured = capsys.readouterr()
        assert "Overwrote alice@example.com (slot 3)" in captured.err

        # Alice still at slot 3 with new marker
        alice_creds = s._read_account_credentials("3", "alice@example.com")
        assert json.loads(alice_creds)["_marker"] == "ALICE_NEW"

        # Bob untouched at slot 1
        bob_creds_after = s._read_account_credentials("1", "bob@example.com")
        assert bob_creds_after == bob_creds_before

        seq = s._get_sequence_data()
        assert seq["accounts"]["1"]["email"] == "bob@example.com"
        assert seq["accounts"]["3"]["email"] == "alice@example.com"

    def test_slot_allocation_when_exported_slot_taken(self, temp_home: Path):
        """Imported account's exported slot is taken by a different account
        (no email match) → allocate next available slot (max+1, mirrors
        add_account semantics; gaps are not filled)."""
        # Source: alice at slot 1
        src_home = temp_home.parent / "src"
        src_home.mkdir()
        with patch("pathlib.Path.home", return_value=src_home):
            with patch.dict(os.environ, {"HOME": str(src_home)}):
                src = _linux_switcher(src_home)
                _seed_account(src, 1, "alice@example.com")
                out = src_home / "a.cswap"
                export_accounts(src, str(out))

        # Destination already has bob at slot 1 (different account)
        dst = _linux_switcher(temp_home)
        _seed_account(dst, 1, "bob@example.com")

        import_accounts(dst, str(out))

        seq = dst._get_sequence_data()
        # Alice should land at slot 2 (next free)
        assert seq["accounts"]["1"]["email"] == "bob@example.com"
        assert seq["accounts"]["2"]["email"] == "alice@example.com"


# ---------------------------------------------------------------------------
# Cross-platform credential translation
# ---------------------------------------------------------------------------


class TestCrossPlatform:
    def test_export_macos_keyring_import_linux_files(self, temp_home: Path):
        """Export from a (faked) macOS switcher backed by keyring, then import
        into a Linux switcher and verify the credential file appears."""
        with patch("claude_swap.switcher.keyring", create=True) as mock_keyring:
            stored: dict[tuple[str, str], str] = {}

            def fake_set(svc, user, val):
                stored[(svc, user)] = val

            def fake_get(svc, user):
                return stored.get((svc, user))

            mock_keyring.set_password.side_effect = fake_set
            mock_keyring.get_password.side_effect = fake_get

            mac_switcher = ClaudeAccountSwitcher()
            mac_switcher.platform = Platform.MACOS
            mac_switcher._setup_directories()
            mac_switcher._init_sequence_file()

            _seed_account(mac_switcher, 1, "alice@example.com", "org-a")

            out = temp_home / "x.cswap"
            export_accounts(mac_switcher, str(out))

        # Import into a Linux destination (file-based credentials)
        dst_home = temp_home.parent / "dst"
        dst_home.mkdir()
        with patch("pathlib.Path.home", return_value=dst_home):
            with patch.dict(os.environ, {"HOME": str(dst_home)}):
                dst = _linux_switcher(dst_home)
                import_accounts(dst, str(out))

                cred_file = (
                    dst.credentials_dir / ".creds-1-alice@example.com.enc"
                )
                assert cred_file.exists()


# ---------------------------------------------------------------------------
# Path traversal & validation
# ---------------------------------------------------------------------------


class TestValidation:
    def _make_envelope(self, email: str = "user@example.com", number=1) -> dict:
        return {
            "version": 1,
            "exportedAt": "2026-01-01T00:00:00Z",
            "exportedFrom": "linux",
            "swapVersion": "0.0.0",
            "encrypted": False,
            "activeAccountNumber": number if isinstance(number, int) else None,
            "accounts": [
                {
                    "number": number,
                    "email": email,
                    "uuid": "u",
                    "organizationUuid": "",
                    "organizationName": "",
                    "added": "2024-01-01T00:00:00Z",
                    "credentials": SAMPLE_CREDS,
                    "config": SAMPLE_CONFIG,
                }
            ],
        }

    def test_path_traversal_email_rejected(self, temp_home: Path):
        s = _linux_switcher(temp_home)
        env = self._make_envelope(email="../../evil")
        f = temp_home / "evil.cswap"
        f.write_text(json.dumps(env))

        with pytest.raises(TransferError, match="invalid or missing email"):
            import_accounts(s, str(f))

        # Verify nothing escaped
        for parent in [temp_home.parent, temp_home.parent.parent]:
            assert not (parent / ".creds-1-..").exists()

    def test_negative_slot_number_rejected(self, temp_home: Path):
        s = _linux_switcher(temp_home)
        env = self._make_envelope(number=-1)
        f = temp_home / "neg.cswap"
        f.write_text(json.dumps(env))

        with pytest.raises(TransferError, match="invalid slot number"):
            import_accounts(s, str(f))

    def test_zero_slot_number_rejected(self, temp_home: Path):
        s = _linux_switcher(temp_home)
        env = self._make_envelope(number=0)
        f = temp_home / "zero.cswap"
        f.write_text(json.dumps(env))

        with pytest.raises(TransferError, match="invalid slot number"):
            import_accounts(s, str(f))

    def test_string_slot_number_rejected(self, temp_home: Path):
        s = _linux_switcher(temp_home)
        env = self._make_envelope(number="../")  # type: ignore[arg-type]
        f = temp_home / "str.cswap"
        f.write_text(json.dumps(env))

        with pytest.raises(TransferError, match="invalid slot number"):
            import_accounts(s, str(f))

    def test_missing_version_rejected(self, temp_home: Path):
        s = _linux_switcher(temp_home)
        env = self._make_envelope()
        env.pop("version")
        f = temp_home / "v.cswap"
        f.write_text(json.dumps(env))

        with pytest.raises(TransferError, match="unsupported export version"):
            import_accounts(s, str(f))

    def test_wrong_version_rejected(self, temp_home: Path):
        s = _linux_switcher(temp_home)
        env = self._make_envelope()
        env["version"] = 2
        f = temp_home / "v.cswap"
        f.write_text(json.dumps(env))

        with pytest.raises(TransferError, match="unsupported export version"):
            import_accounts(s, str(f))

    def test_encrypted_flag_rejected(self, temp_home: Path):
        s = _linux_switcher(temp_home)
        env = self._make_envelope()
        env["encrypted"] = True
        f = temp_home / "e.cswap"
        f.write_text(json.dumps(env))

        with pytest.raises(TransferError, match="encrypted exports are not supported"):
            import_accounts(s, str(f))

    def test_malformed_top_level_json_rejected(self, temp_home: Path):
        s = _linux_switcher(temp_home)
        f = temp_home / "bad.cswap"
        f.write_text("{not json")

        with pytest.raises(TransferError, match="not valid JSON"):
            import_accounts(s, str(f))

    def test_credentials_must_be_object(self, temp_home: Path):
        s = _linux_switcher(temp_home)
        env = self._make_envelope()
        env["accounts"][0]["credentials"] = "a string"
        f = temp_home / "c.cswap"
        f.write_text(json.dumps(env))

        with pytest.raises(TransferError, match="must be JSON objects"):
            import_accounts(s, str(f))

    @pytest.mark.parametrize(
        "field", ["organizationUuid", "organizationName", "uuid", "added"]
    )
    @pytest.mark.parametrize("bad_value", [["a", "b"], {"x": 1}, 42])
    def test_string_fields_reject_non_string_types(
        self, temp_home: Path, field: str, bad_value
    ):
        """Org/uuid/added fields must be strings — a list/dict/int would
        otherwise blow up downstream (unhashable in seen_keys, broken
        composite-key matching, garbage in sequence.json)."""
        s = _linux_switcher(temp_home)
        env = self._make_envelope()
        env["accounts"][0][field] = bad_value
        f = temp_home / "bad.cswap"
        f.write_text(json.dumps(env))

        with pytest.raises(TransferError, match=f"{field} for .* must be a string"):
            import_accounts(s, str(f))

        # Account 1 must NOT have leaked through
        seq = s._get_sequence_data()
        assert seq is None or seq.get("accounts", {}) == {}

    def test_missing_file_rejected(self, temp_home: Path):
        s = _linux_switcher(temp_home)
        with pytest.raises(TransferError, match="not found"):
            import_accounts(s, str(temp_home / "nope.cswap"))


# ---------------------------------------------------------------------------
# Stdin / stdout pipe support
# ---------------------------------------------------------------------------


class TestPipeMode:
    def test_export_to_stdout_writes_only_json(self, temp_home: Path, capsys):
        s = _linux_switcher(temp_home)
        _seed_account(s, 1, "alice@example.com")
        export_accounts(s, "-")
        captured = capsys.readouterr()

        # stdout is pure JSON
        env = json.loads(captured.out)
        assert env["version"] == 1
        assert env["accounts"][0]["email"] == "alice@example.com"
        # Summary suppressed in stdout mode (no "Exported" line on stderr either)
        assert "Exported" not in captured.err

    def test_import_from_stdin(self, temp_home: Path):
        # Build an envelope and feed it through stdin
        env = {
            "version": 1,
            "exportedAt": "2026-01-01T00:00:00Z",
            "exportedFrom": "linux",
            "swapVersion": "0.0.0",
            "encrypted": False,
            "activeAccountNumber": 1,
            "accounts": [
                {
                    "number": 1,
                    "email": "alice@example.com",
                    "uuid": "u",
                    "organizationUuid": "",
                    "organizationName": "",
                    "added": "2024-01-01T00:00:00Z",
                    "credentials": SAMPLE_CREDS,
                    "config": SAMPLE_CONFIG,
                }
            ],
        }
        s = _linux_switcher(temp_home)
        with patch.object(sys, "stdin", io.StringIO(json.dumps(env))):
            import_accounts(s, "-")

        seq = s._get_sequence_data()
        assert seq["accounts"]["1"]["email"] == "alice@example.com"


# ---------------------------------------------------------------------------
# Empty home
# ---------------------------------------------------------------------------


class TestEmptyHome:
    def test_import_into_empty_home_initializes_sequence(self, temp_home: Path):
        # Source
        src_home = temp_home.parent / "src"
        src_home.mkdir()
        with patch("pathlib.Path.home", return_value=src_home):
            with patch.dict(os.environ, {"HOME": str(src_home)}):
                src = _linux_switcher(src_home)
                _seed_account(src, 1, "alice@example.com")
                out = src_home / "x.cswap"
                export_accounts(src, str(out))

        # Destination has no backup directory at all
        dst_home = temp_home.parent / "empty"
        dst_home.mkdir()
        with patch("pathlib.Path.home", return_value=dst_home):
            with patch.dict(os.environ, {"HOME": str(dst_home)}):
                dst = ClaudeAccountSwitcher()
                dst.platform = Platform.LINUX
                # Don't pre-init — verify import does it
                assert not dst.sequence_file.exists()

                import_accounts(dst, str(out))

                assert dst.sequence_file.exists()
                seq = dst._get_sequence_data()
                assert seq["accounts"]["1"]["email"] == "alice@example.com"


# ---------------------------------------------------------------------------
# File permissions
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only chmod check")
class TestFilePermissions:
    def test_export_file_is_0600(self, temp_home: Path):
        s = _linux_switcher(temp_home)
        _seed_account(s, 1, "alice@example.com")
        out = temp_home / "x.cswap"
        export_accounts(s, str(out))

        mode = os.stat(out).st_mode & 0o777
        assert mode == 0o600


# ---------------------------------------------------------------------------
# Validate-all-before-write atomicity
# ---------------------------------------------------------------------------


class TestValidateAllBeforeWrite:
    def test_malformed_later_account_does_not_partial_write(self, temp_home: Path):
        """If account 2 fails validation, account 1 must NOT have been written."""
        s = _linux_switcher(temp_home)
        env = {
            "version": 1,
            "exportedAt": "2026-01-01T00:00:00Z",
            "exportedFrom": "linux",
            "swapVersion": "0.0.0",
            "encrypted": False,
            "activeAccountNumber": 1,
            "accounts": [
                {
                    "number": 1,
                    "email": "alice@example.com",
                    "uuid": "u1",
                    "organizationUuid": "",
                    "organizationName": "",
                    "added": "2024-01-01T00:00:00Z",
                    "credentials": SAMPLE_CREDS,
                    "config": SAMPLE_CONFIG,
                },
                {
                    "number": 2,
                    "email": "../../evil",  # path traversal -> validation fails
                    "uuid": "u2",
                    "organizationUuid": "",
                    "organizationName": "",
                    "added": "2024-01-01T00:00:00Z",
                    "credentials": SAMPLE_CREDS,
                    "config": SAMPLE_CONFIG,
                },
            ],
        }
        f = temp_home / "bad.cswap"
        f.write_text(json.dumps(env))

        with pytest.raises(TransferError, match="invalid or missing email"):
            import_accounts(s, str(f))

        # Account 1 must NOT have been written
        seq = s._get_sequence_data()
        assert seq is not None
        assert seq.get("accounts", {}) == {}, (
            "validation must complete for ALL accounts before any writes"
        )
        # No credential file leaked
        assert not list(s.credentials_dir.glob("*alice*"))
        assert not list(s.configs_dir.glob("*alice*"))

    def test_duplicate_account_in_export_rejected(self, temp_home: Path):
        s = _linux_switcher(temp_home)
        env = {
            "version": 1,
            "exportedAt": "2026-01-01T00:00:00Z",
            "exportedFrom": "linux",
            "swapVersion": "0.0.0",
            "encrypted": False,
            "activeAccountNumber": 1,
            "accounts": [
                {
                    "number": 1,
                    "email": "alice@example.com",
                    "uuid": "u1",
                    "organizationUuid": "",
                    "organizationName": "",
                    "added": "2024-01-01T00:00:00Z",
                    "credentials": SAMPLE_CREDS,
                    "config": SAMPLE_CONFIG,
                },
                {
                    "number": 2,
                    "email": "alice@example.com",
                    "uuid": "u1",
                    "organizationUuid": "",
                    "organizationName": "",
                    "added": "2024-01-01T00:00:00Z",
                    "credentials": SAMPLE_CREDS,
                    "config": SAMPLE_CONFIG,
                },
            ],
        }
        f = temp_home / "dup.cswap"
        f.write_text(json.dumps(env))
        with pytest.raises(TransferError, match="duplicate account"):
            import_accounts(s, str(f))


# ---------------------------------------------------------------------------
# Clean-home activation (post-import on fresh machine)
# ---------------------------------------------------------------------------


class TestCleanHomeActivation:
    """After import on a fresh machine, switch_to / switch should activate
    the imported account into the live vault even though no Claude Code
    session has logged in yet."""

    def _seed_and_export(self, src_home: Path) -> Path:
        with patch("pathlib.Path.home", return_value=src_home):
            with patch.dict(os.environ, {"HOME": str(src_home)}):
                src = _linux_switcher(src_home)
                _seed_account(src, 1, "alice@example.com")
                _seed_account(src, 2, "bob@example.com")
                # Mark slot 2 as the active one in the export
                data = src._get_sequence_data()
                data["activeAccountNumber"] = 2
                src._write_json(src.sequence_file, data)
                out = src_home / "backup.cswap"
                export_accounts(src, str(out))
                return out

    def test_switch_to_after_import_activates_target(self, temp_home: Path):
        src_home = temp_home.parent / "src"
        src_home.mkdir()
        export_path = self._seed_and_export(src_home)

        dst_home = temp_home.parent / "dst"
        dst_home.mkdir()
        with patch("pathlib.Path.home", return_value=dst_home):
            with patch.dict(os.environ, {"HOME": str(dst_home)}):
                dst = _linux_switcher(dst_home)
                import_accounts(dst, str(export_path))

                # No live ~/.claude.json exists yet on the "fresh" machine
                config_path = dst._get_claude_config_path()
                assert not config_path.exists()

                # Stub list_accounts (post-switch network call) to keep test offline
                with patch.object(dst, "list_accounts"):
                    dst.switch_to("1")

                # Live config + live credentials now exist for alice
                assert config_path.exists()
                live_config = json.loads(config_path.read_text())
                assert live_config["oauthAccount"]["emailAddress"] == "alice@example.com"

                live_creds_path = dst_home / ".claude" / ".credentials.json"
                assert live_creds_path.exists()
                live_creds = json.loads(live_creds_path.read_text())
                assert live_creds["_marker"] == "alice@example.com"

                seq = dst._get_sequence_data()
                assert seq["activeAccountNumber"] == 1

    def test_switch_rotate_after_import_uses_active_from_envelope(
        self, temp_home: Path
    ):
        """Import on a clean home should preserve the envelope's
        activeAccountNumber (slot 2 = bob), so a subsequent --switch lands
        on bob without any manual setup."""
        src_home = temp_home.parent / "src"
        src_home.mkdir()
        export_path = self._seed_and_export(src_home)

        dst_home = temp_home.parent / "dst"
        dst_home.mkdir()
        with patch("pathlib.Path.home", return_value=dst_home):
            with patch.dict(os.environ, {"HOME": str(dst_home)}):
                dst = _linux_switcher(dst_home)
                import_accounts(dst, str(export_path))

                # Import alone must propagate the envelope's active slot.
                seq = dst._get_sequence_data()
                assert seq["activeAccountNumber"] == 2

                config_path = dst._get_claude_config_path()
                assert not config_path.exists()

                with patch.object(dst, "list_accounts"):
                    dst.switch()

                live_config = json.loads(config_path.read_text())
                assert live_config["oauthAccount"]["emailAddress"] == "bob@example.com"
                seq = dst._get_sequence_data()
                assert seq["activeAccountNumber"] == 2

    def test_import_preserves_existing_active_account(self, temp_home: Path):
        """If destination already has an active selection, import must NOT
        overwrite it with the envelope's value."""
        src_home = temp_home.parent / "src"
        src_home.mkdir()
        export_path = self._seed_and_export(src_home)  # envelope active = 2

        # Destination already has its own active account in slot 5
        dst = _linux_switcher(temp_home)
        _seed_account(dst, 5, "local@example.com")
        seq = dst._get_sequence_data()
        seq["activeAccountNumber"] = 5
        dst._write_json(dst.sequence_file, seq)

        import_accounts(dst, str(export_path))

        # User's existing active stays intact
        final = dst._get_sequence_data()
        assert final["activeAccountNumber"] == 5

    def test_active_seeded_to_resolved_slot_not_envelope_slot(
        self, temp_home: Path
    ):
        """Mixed-state: destination has unrelated account at the envelope's
        active slot number, but the envelope's active account itself was
        imported into a *different* slot. activeAccountNumber must point
        at the resolved slot, NOT at the unrelated local account."""
        src_home = temp_home.parent / "src"
        src_home.mkdir()
        export_path = self._seed_and_export(src_home)  # envelope active = 2 (bob)

        # Destination has unrelated `local@example.com` at slot 2 and no
        # activeAccountNumber set. Bob will be allocated to a different slot
        # because slot 2 is already taken by an unrelated account.
        dst = _linux_switcher(temp_home)
        _seed_account(dst, 2, "local@example.com")
        seq = dst._get_sequence_data()
        seq["activeAccountNumber"] = None
        dst._write_json(dst.sequence_file, seq)

        import_accounts(dst, str(export_path))

        final = dst._get_sequence_data()
        # local@example.com still owns slot 2
        assert final["accounts"]["2"]["email"] == "local@example.com"
        # bob was imported elsewhere
        bob_slot = next(
            num
            for num, acc in final["accounts"].items()
            if acc["email"] == "bob@example.com"
        )
        assert bob_slot != "2"
        # activeAccountNumber points at bob's resolved slot, NOT at slot 2 (local)
        assert final["activeAccountNumber"] == int(bob_slot)

    def test_clean_switch_preserves_existing_local_config(self, temp_home: Path):
        """Fresh-machine switch must preserve existing settings in ~/.claude.json
        when present, only overlaying oauthAccount — same merge semantics as
        the normal switch path. Common case: user logged out of Claude Code
        but kept their projects/MCP config in place."""
        src_home = temp_home.parent / "src"
        src_home.mkdir()
        export_path = self._seed_and_export(src_home)

        dst_home = temp_home.parent / "dst"
        dst_home.mkdir()
        with patch("pathlib.Path.home", return_value=dst_home):
            with patch.dict(os.environ, {"HOME": str(dst_home)}):
                dst = _linux_switcher(dst_home)
                # Pre-existing local config: settings, projects, but no oauthAccount
                config_path = dst._get_claude_config_path()
                local_config = {
                    "tipsHistory": {"shown": ["welcome"]},
                    "projects": {
                        "/path/to/project": {
                            "mcpServers": {"memory": {"type": "stdio"}}
                        }
                    },
                    "userID": "local-user-id",
                    "numStartups": 42,
                }
                config_path.write_text(json.dumps(local_config))

                import_accounts(dst, str(export_path))

                with patch.object(dst, "list_accounts"):
                    dst.switch_to("1")

                merged = json.loads(config_path.read_text())
                # Local settings preserved
                assert merged["tipsHistory"] == {"shown": ["welcome"]}
                assert merged["projects"]["/path/to/project"]["mcpServers"] == {
                    "memory": {"type": "stdio"}
                }
                assert merged["userID"] == "local-user-id"
                assert merged["numStartups"] == 42
                # oauthAccount overlaid from imported config
                assert merged["oauthAccount"]["emailAddress"] == "alice@example.com"

    def test_clean_switch_fallback_when_local_config_malformed(self, temp_home: Path):
        """If ~/.claude.json exists but is malformed/empty, fall back to the
        imported config rather than crashing."""
        src_home = temp_home.parent / "src"
        src_home.mkdir()
        export_path = self._seed_and_export(src_home)

        dst_home = temp_home.parent / "dst"
        dst_home.mkdir()
        with patch("pathlib.Path.home", return_value=dst_home):
            with patch.dict(os.environ, {"HOME": str(dst_home)}):
                dst = _linux_switcher(dst_home)
                config_path = dst._get_claude_config_path()
                config_path.write_text("{not valid json")

                import_accounts(dst, str(export_path))

                with patch.object(dst, "list_accounts"):
                    dst.switch_to("1")

                # Imported config wholesale (the malformed file is replaced)
                merged = json.loads(config_path.read_text())
                assert merged["oauthAccount"]["emailAddress"] == "alice@example.com"

    def test_active_seeded_when_envelope_active_was_skipped(
        self, temp_home: Path
    ):
        """If the envelope's active account already existed locally and was
        skipped (no --force), activeAccountNumber should still seed to the
        existing local slot — that's where the migration intends to land."""
        src_home = temp_home.parent / "src"
        src_home.mkdir()
        export_path = self._seed_and_export(src_home)  # envelope active = 2 (bob)

        # Destination has bob at a different slot already, and alice not at all
        dst = _linux_switcher(temp_home)
        _seed_account(dst, 7, "bob@example.com")
        seq = dst._get_sequence_data()
        seq["activeAccountNumber"] = None
        dst._write_json(dst.sequence_file, seq)

        import_accounts(dst, str(export_path))  # no --force, so bob is skipped

        final = dst._get_sequence_data()
        assert final["accounts"]["7"]["email"] == "bob@example.com"
        # bob skipped → activeAccountNumber points at where bob lives (slot 7)
        assert final["activeAccountNumber"] == 7
