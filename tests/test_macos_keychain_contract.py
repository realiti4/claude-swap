"""macOS Keychain contract tests.

Two layers of coverage:

1. **Mocked tests** (run on every PR, every platform): assert that the macOS
   backup-credentials path passes the correct `(service, account)` tuple to the
   `macos_keychain` security wrapper, under the new `claude-swap` service. This
   guards the multi-account backup namespace on every CI run.

2. **Real-keychain integration tests** (GHA macOS only): exercise
   `_read_credentials` / `_write_credentials` end-to-end against a temporary
   keychain, comparing token values rather than argv shape.

The Layer 2 gate (`GITHUB_ACTIONS=true AND sys.platform=="darwin"`, plus the
`no_keychain_fake` marker) is deliberate: no local opt-in, so a developer cannot
accidentally swap their default keychain by running pytest.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import call, patch

import pytest

from claude_swap import macos_keychain
from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher


# ---------------------------------------------------------------------------
# Mocked keyring tests — backup-credentials path. Run everywhere.
# ---------------------------------------------------------------------------


@pytest.fixture
def macos_switcher(temp_home: Path) -> ClaudeAccountSwitcher:
    """Switcher with platform forced to MACOS regardless of host OS."""
    switcher = ClaudeAccountSwitcher()
    switcher.platform = Platform.MACOS
    return switcher


class TestBackupCredentialsSecurity:
    """Mocked tests for the macOS backup-creds path: assert the correct
    (service, account) tuple flows to the ``macos_keychain`` security wrapper.

    The autouse ``block_real_keychain`` guard already prevents any real Keychain
    access; here we install a MagicMock to assert the exact call shape. The
    per-account backup service is the new ``claude-swap`` (not the old keyring
    ``claude-code``).
    """

    def test_read_account_credentials_uses_security_service(
        self, macos_switcher: ClaudeAccountSwitcher
    ):
        with patch("claude_swap.credentials.macos_keychain") as mock_kc:
            mock_kc.get_password.return_value = "fake-token"

            result = macos_switcher._read_account_credentials("1", "user@example.com")

            mock_kc.get_password.assert_called_once_with(
                "claude-swap", "account-1-user@example.com"
            )
            assert result == "fake-token"

    def test_write_account_credentials_uses_security_service(
        self, macos_switcher: ClaudeAccountSwitcher
    ):
        with patch("claude_swap.credentials.macos_keychain") as mock_kc:
            macos_switcher._write_account_credentials(
                "2", "alice@example.com", "secret-token"
            )

            mock_kc.set_password.assert_called_once_with(
                "claude-swap", "account-2-alice@example.com", "secret-token"
            )

    def test_delete_account_credentials_uses_security_service(
        self, macos_switcher: ClaudeAccountSwitcher
    ):
        with patch("claude_swap.credentials.macos_keychain") as mock_kc:
            macos_switcher._delete_account_credentials("3", "bob@example.com")

            mock_kc.delete_password.assert_has_calls([
                call("claude-swap", "account-3-bob@example.com"),
                call("claude-swap", "account-None-bob@example.com"),
            ])


# ---------------------------------------------------------------------------
# Real-keychain integration tests. macOS GHA only.
# ---------------------------------------------------------------------------

mac_ci_only = pytest.mark.skipif(
    os.environ.get("GITHUB_ACTIONS") != "true" or sys.platform != "darwin",
    reason="Modifies default Keychain — runs on GitHub Actions macOS only",
)


@pytest.fixture
def tmp_keychain(tmp_path: Path):
    """Create a temporary keychain, swap it in as default + sole user search-list
    entry, and restore both on teardown.

    `default-keychain` controls where new items go; `list-keychains -d user`
    controls what `find-generic-password` searches. These are independent — both
    must be redirected for `_read_credentials` (which doesn't pass `-k`) to find
    the seeded entry.

    The try/finally is the safety-critical part: a crash mid-test must still
    restore the user's original keychain config. CI doesn't care, but the safe
    shape is kept so the same code is risk-free if anyone copies it.
    """
    test_keychain = str(tmp_path / "test.keychain")
    subprocess.run(
        ["security", "create-keychain", "-p", "", test_keychain], check=True
    )
    subprocess.run(
        ["security", "unlock-keychain", "-p", "", test_keychain], check=True
    )

    # CI runners don't reliably have a default keychain configured (rc 1,
    # "A default keychain could not be found") — and an earlier swap/restore
    # cycle in this job may have cleared it. Capture it only if present and
    # skip the restore otherwise, rather than failing setup.
    default_proc = subprocess.run(
        ["security", "default-keychain"],
        capture_output=True,
        text=True,
    )
    original_default = (
        default_proc.stdout.strip().strip('"')
        if default_proc.returncode == 0
        else None
    )
    list_proc = subprocess.run(
        ["security", "list-keychains", "-d", "user"],
        capture_output=True,
        text=True,
    )
    original_list = [
        line.strip().strip('"')
        for line in (list_proc.stdout if list_proc.returncode == 0 else "").splitlines()
        if line.strip()
    ]

    try:
        subprocess.run(
            ["security", "default-keychain", "-s", test_keychain], check=True
        )
        subprocess.run(
            ["security", "list-keychains", "-d", "user", "-s", test_keychain],
            check=True,
        )
        # Harden against an invisible SecurityAgent dialog hanging the job: a
        # `security` call against a (re-)locked keychain blocks forever on a
        # headless runner waiting for an unlock prompt nobody can click. Remove
        # the auto-lock timeout and unlock *after* the default/search-list swap
        # (the order fastlane's setup_ci uses).
        subprocess.run(
            ["security", "set-keychain-settings", test_keychain], check=True
        )
        subprocess.run(
            ["security", "unlock-keychain", "-p", "", test_keychain], check=True
        )
        yield test_keychain
    finally:
        # Restore the search list BEFORE the default: macOS won't report a
        # default keychain that isn't in the search list, so the reverse order
        # leaves the default dangling for whatever runs next in this job.
        if original_list:
            subprocess.run(
                ["security", "list-keychains", "-d", "user", "-s", *original_list],
                check=False,
            )
        if original_default:
            subprocess.run(
                ["security", "default-keychain", "-s", original_default], check=False
            )
        subprocess.run(["security", "delete-keychain", test_keychain], check=False)


@pytest.mark.no_keychain_fake
@mac_ci_only
def test_read_credentials_finds_claude_code_seeded_entry(tmp_keychain: str):
    username = os.environ["USER"]
    subprocess.run(
        [
            "security",
            "add-generic-password",
            "-a",
            username,
            "-s",
            "Claude Code-credentials",
            "-w",
            "fake-token-read",
            "-A",
            tmp_keychain,
        ],
        check=True,
    )

    switcher = ClaudeAccountSwitcher()
    switcher.platform = Platform.MACOS
    assert switcher._read_credentials() == "fake-token-read"


@pytest.mark.no_keychain_fake
@mac_ci_only
def test_write_credentials_creates_user_scoped_entry(tmp_keychain: str):
    # If _write_credentials ever stores the entry under a hardcoded account name
    # (or any value other than $USER), the verification lookup below — which
    # mirrors Claude Code's own read shape — returns 44 and the test fails.
    switcher = ClaudeAccountSwitcher()
    switcher.platform = Platform.MACOS
    switcher._write_credentials("fake-token-write")

    username = os.environ["USER"]
    result = subprocess.run(
        [
            "security",
            "find-generic-password",
            "-a",
            username,
            "-s",
            "Claude Code-credentials",
            "-w",
            tmp_keychain,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"security find-generic-password failed: {result.stderr}"
    )
    assert result.stdout.strip() == "fake-token-write"


@pytest.mark.no_keychain_fake
@mac_ci_only
def test_wrapper_roundtrip_real_keychain(tmp_keychain: str):
    """set → get → delete through the real wrapper against the temp keychain.

    Covers the full production read/write/delete path the other Layer-2 tests
    only half-exercise: a wrapper-created item (no ``-A`` any-app access) read
    back via the keychain *search list* (no explicit keychain argument), then
    deleted, with the rc-44 "not found" contract checked at the end.
    """
    macos_keychain.set_password("claude-swap-test", "acct-1", "round-trip-token")
    assert macos_keychain.get_password("claude-swap-test", "acct-1") == "round-trip-token"
    macos_keychain.delete_password("claude-swap-test", "acct-1")
    assert macos_keychain.get_password("claude-swap-test", "acct-1") is None
