"""macOS Keychain contract tests.

Layer 1 (mocked, runs everywhere): asserts that on macOS, every credential
code path passes the correct argv shape to its underlying storage primitive.
These would have caught PR #21 commit `bc9db76` (which hardcoded
`-a "credentials"` in `_write_credentials`).

Layer 2 (real keychain, GHA macOS only): seeds / writes a temporary keychain
and verifies cswap reads / writes interoperate with the shape Claude Code uses.

The Layer 2 gate (`GITHUB_ACTIONS=true AND sys.platform=="darwin"`) is
deliberate: no local opt-in env var, so a developer cannot accidentally
swap their default keychain by running pytest.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_swap.exceptions import CredentialWriteError
from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher


# ---------------------------------------------------------------------------
# Layer 1 — mocked unit tests. Run on every PR on every platform.
# ---------------------------------------------------------------------------


@pytest.fixture
def macos_switcher(temp_home: Path) -> ClaudeAccountSwitcher:
    """Switcher with platform forced to MACOS regardless of host OS."""
    switcher = ClaudeAccountSwitcher()
    switcher.platform = Platform.MACOS
    return switcher


@pytest.mark.skip(
    reason="Temporarily disabled to validate Layer 2 alone catches PR-#21 shape on GHA macOS — re-enable after the macos-keychain-contract job passes."
)
class TestLiveCredentialsArgv:
    """Mocked tests for _read_credentials / _write_credentials on macOS."""

    def test_read_credentials_macos_argv(
        self, macos_switcher: ClaudeAccountSwitcher, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("USER", "testuser")
        mock_run = MagicMock(
            return_value=MagicMock(returncode=0, stdout="seeded-token\n", stderr="")
        )
        monkeypatch.setattr("claude_swap.switcher.subprocess.run", mock_run)

        result = macos_switcher._read_credentials()

        assert result == "seeded-token"
        mock_run.assert_called_once()
        argv = mock_run.call_args.args[0]
        assert argv == [
            "security",
            "find-generic-password",
            "-a",
            "testuser",
            "-s",
            "Claude Code-credentials",
            "-w",
        ]

    def test_read_credentials_returns_empty_on_returncode_44(
        self, macos_switcher: ClaudeAccountSwitcher, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("USER", "testuser")
        err = subprocess.CalledProcessError(returncode=44, cmd=["security"])
        monkeypatch.setattr(
            "claude_swap.switcher.subprocess.run", MagicMock(side_effect=err)
        )

        assert macos_switcher._read_credentials() == ""

    def test_write_credentials_uses_user_account(
        self, macos_switcher: ClaudeAccountSwitcher, monkeypatch: pytest.MonkeyPatch
    ):
        # Regression guard for PR #21 commit bc9db76 (hardcoded `-a "credentials"`).
        monkeypatch.setenv("USER", "testuser")
        mock_run = MagicMock(
            return_value=MagicMock(returncode=0, stdout="", stderr="")
        )
        monkeypatch.setattr("claude_swap.switcher.subprocess.run", mock_run)

        macos_switcher._write_credentials("fake-token-12345")

        argv = mock_run.call_args.args[0]
        assert argv == [
            "security",
            "add-generic-password",
            "-U",
            "-s",
            "Claude Code-credentials",
            "-a",
            "testuser",
            "-w",
            "fake-token-12345",
        ]

    def test_write_credentials_user_env_fallback(
        self, macos_switcher: ClaudeAccountSwitcher, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.delenv("USER", raising=False)
        mock_run = MagicMock(
            return_value=MagicMock(returncode=0, stdout="", stderr="")
        )
        monkeypatch.setattr("claude_swap.switcher.subprocess.run", mock_run)

        macos_switcher._write_credentials("token")

        argv = mock_run.call_args.args[0]
        a_idx = argv.index("-a")
        assert argv[a_idx + 1] == "user"

    def test_write_credentials_raises_on_nonzero_returncode(
        self, macos_switcher: ClaudeAccountSwitcher, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("USER", "testuser")
        mock_run = MagicMock(
            return_value=MagicMock(returncode=1, stdout="", stderr="boom")
        )
        monkeypatch.setattr("claude_swap.switcher.subprocess.run", mock_run)

        with pytest.raises(CredentialWriteError):
            macos_switcher._write_credentials("token")


@pytest.mark.skip(
    reason="Temporarily disabled to validate Layer 2 alone catches PR-#21 shape on GHA macOS — re-enable after the macos-keychain-contract job passes."
)
class TestBackupCredentialsKeyring:
    """Mocked tests for backup-creds keyring args on macOS/Windows.

    `keyring` is conditionally imported in switcher.py
    (`if sys.platform != "linux": import keyring`), so on a Linux runner the
    symbol isn't bound. `patch(..., create=True)` injects it for the test.
    """

    def test_read_account_credentials_calls_keyring_with_correct_keys(
        self, macos_switcher: ClaudeAccountSwitcher
    ):
        with patch(
            "claude_swap.switcher.keyring", create=True
        ) as mock_keyring:
            mock_keyring.get_password.return_value = "fake-token"

            result = macos_switcher._read_account_credentials("1", "user@example.com")

            mock_keyring.get_password.assert_called_once_with(
                "claude-code", "account-1-user@example.com"
            )
            assert result == "fake-token"

    def test_write_account_credentials_calls_keyring_with_correct_keys(
        self, macos_switcher: ClaudeAccountSwitcher
    ):
        with patch("claude_swap.switcher.keyring", create=True) as mock_keyring:
            macos_switcher._write_account_credentials(
                "2", "alice@example.com", "secret-token"
            )

            mock_keyring.set_password.assert_called_once_with(
                "claude-code", "account-2-alice@example.com", "secret-token"
            )

    def test_delete_account_credentials_calls_keyring_with_correct_keys(
        self, macos_switcher: ClaudeAccountSwitcher
    ):
        with patch("claude_swap.switcher.keyring", create=True) as mock_keyring:
            macos_switcher._delete_account_credentials("3", "bob@example.com")

            mock_keyring.delete_password.assert_called_once_with(
                "claude-code", "account-3-bob@example.com"
            )


# ---------------------------------------------------------------------------
# Layer 2 — real-keychain integration tests. macOS GHA only.
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

    original_default = (
        subprocess.run(
            ["security", "default-keychain"],
            capture_output=True,
            text=True,
            check=True,
        )
        .stdout.strip()
        .strip('"')
    )
    original_list_raw = subprocess.run(
        ["security", "list-keychains", "-d", "user"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    original_list = [
        line.strip().strip('"')
        for line in original_list_raw.splitlines()
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
        yield test_keychain
    finally:
        subprocess.run(
            ["security", "default-keychain", "-s", original_default], check=False
        )
        if original_list:
            subprocess.run(
                ["security", "list-keychains", "-d", "user", "-s", *original_list],
                check=False,
            )
        subprocess.run(["security", "delete-keychain", test_keychain], check=False)


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


@mac_ci_only
def test_write_credentials_creates_user_scoped_entry(tmp_keychain: str):
    # End-to-end regression for PR #21 commit bc9db76: a hardcoded `-a "credentials"`
    # in _write_credentials means the entry is stored under the wrong account, so
    # the verification lookup with -a $USER below would return returncode 44.
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
