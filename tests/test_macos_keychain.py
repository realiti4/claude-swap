"""Unit tests for the macOS ``security``-CLI wrapper (claude_swap.macos_keychain).

These mock ``subprocess.run`` so they exercise the wrapper's argv/stdin shaping,
hex encoding, and return-code handling without ever invoking the real
``security`` binary. (The autouse ``block_real_keychain`` guard replaces the
module's functions for *other* tests; here we patch ``subprocess`` so the real
function bodies run against a fake process.)
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from claude_swap import macos_keychain

# Every test here drives the *real* wrapper bodies (mocking subprocess) or runs
# against a temp keychain on CI, so opt the whole module out of the in-memory
# Keychain guard that replaces these functions for other tests.
pytestmark = pytest.mark.no_keychain_fake


def _completed(returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["security"], returncode=returncode, stdout=stdout, stderr=stderr
    )


# ---------------------------------------------------------------------------
# get_password
# ---------------------------------------------------------------------------


def test_get_password_returns_value_on_rc0():
    with patch("claude_swap.macos_keychain.subprocess.run") as run:
        run.return_value = _completed(0, stdout="the-secret\n")
        assert macos_keychain.get_password("svc", "acct") == "the-secret"
        args = run.call_args.args[0]
        assert args[:2] == ["/usr/bin/security", "find-generic-password"]
        assert "-a" in args and "acct" in args and "svc" in args


def test_get_password_returns_none_only_on_rc44():
    with patch("claude_swap.macos_keychain.subprocess.run") as run:
        run.return_value = _completed(44)
        assert macos_keychain.get_password("svc", "acct") is None


def test_get_password_raises_on_other_nonzero():
    # e.g. locked / denied / unavailable — must NOT be masked as "not found".
    # Framework pinned off: with bindings present, nonzero CLI reads fall
    # back to the framework channel (their own test covers that), which in
    # a dev env would touch the real keychain.
    with patch("claude_swap.macos_keychain._security_framework",
               return_value=None), \
         patch("claude_swap.macos_keychain.subprocess.run") as run:
        run.return_value = _completed(51, stderr="boom")
        with pytest.raises(macos_keychain.KeychainError):
            macos_keychain.get_password("svc", "acct")


# ---------------------------------------------------------------------------
# item_exists
# ---------------------------------------------------------------------------


def test_item_exists_true_on_rc0_and_never_requests_secret():
    with patch("claude_swap.macos_keychain.subprocess.run") as run:
        run.return_value = _completed(0)
        assert macos_keychain.item_exists("svc", "acct") is True
        args = run.call_args.args[0]
        # Attribute-only lookup: must never pass -w (decrypting could prompt).
        assert "-w" not in args


def test_item_exists_false_on_rc44_and_errors():
    for rc in (44, 51):
        with patch("claude_swap.macos_keychain.subprocess.run") as run:
            run.return_value = _completed(rc)
            assert macos_keychain.item_exists("svc", "acct") is False


# ---------------------------------------------------------------------------
# set_password — stdin (security -i) vs argv fallback
# ---------------------------------------------------------------------------


def test_set_password_small_payload_uses_security_i_stdin():
    with patch("claude_swap.macos_keychain.subprocess.run") as run:
        run.return_value = _completed(0)
        macos_keychain.set_password("svc", "acct", "short-secret")

        args = run.call_args.args[0]
        kwargs = run.call_args.kwargs
        assert args == ["/usr/bin/security", "-i"]  # stdin path
        # Secret is NOT in argv; it rides in on stdin as a hex `-X` value.
        assert "short-secret" not in args
        stdin = kwargs["input"]
        assert stdin.startswith("add-generic-password -U")
        assert "-X " + "short-secret".encode().hex() in stdin
        # -a/-s are quoted in the stdin command line.
        assert '-a "acct"' in stdin and '-s "svc"' in stdin


def test_set_password_large_payload_refuses_without_framework():
    """Audit F01: an oversized credential is never placed in argv, where a
    process observer recovers it byte-for-byte. Without Security.framework
    bindings the write is refused outright."""
    big = "x" * macos_keychain.SECURITY_STDIN_LINE_LIMIT  # hex doubles the length
    with patch("claude_swap.macos_keychain.subprocess.run") as run,          patch("claude_swap.macos_keychain._security_framework",
               return_value=None):
        with pytest.raises(macos_keychain.KeychainError, match="refusing"):
            macos_keychain.set_password("svc", "acct", big)
        run.assert_not_called()  # never spawned with the secret anywhere


def test_set_password_large_payload_uses_security_framework():
    """With the framework available the oversized write goes through
    SecItemAdd/SecItemUpdate — no subprocess, no argv, no line limit."""
    big = "x" * macos_keychain.SECURITY_STDIN_LINE_LIMIT
    calls = {}

    class FakeSecurity:
        kSecClass = "class"
        kSecClassGenericPassword = "generic"
        kSecAttrService = "service"
        kSecAttrAccount = "account"
        kSecValueData = "value"

        @staticmethod
        def SecItemCopyMatching(query, _out):
            calls["match"] = query
            return macos_keychain._ERR_SEC_ITEM_NOT_FOUND

        @staticmethod
        def SecItemAdd(attrs, _out):
            calls["add"] = attrs
            return 0

        @staticmethod
        def SecItemUpdate(_query, _attrs):
            calls["update"] = True
            return 0

    class FakeFoundation:
        @staticmethod
        def NSData():
            raise AssertionError("replaced below")

    import types
    nsdata = types.SimpleNamespace(
        dataWithBytes_length_=lambda b, n: ("data", b))
    fake_foundation = types.SimpleNamespace(NSData=nsdata)
    with patch("claude_swap.macos_keychain.subprocess.run") as run,          patch("claude_swap.macos_keychain._security_framework",
               return_value=(FakeSecurity, fake_foundation)):
        macos_keychain.set_password("svc", "acct", big)
    run.assert_not_called()
    assert calls["add"]["value"] == ("data", big.encode())
    # update path: item already exists
    FakeSecurity.SecItemCopyMatching = staticmethod(
        lambda q, _o: 0)
    with patch("claude_swap.macos_keychain.subprocess.run") as run,          patch("claude_swap.macos_keychain._security_framework",
               return_value=(FakeSecurity, fake_foundation)):
        macos_keychain.set_password("svc", "acct", big)
    assert calls["update"] is True


def test_set_password_framework_error_raises_without_secret():
    class FailingSecurity:
        kSecClass = "c"; kSecClassGenericPassword = "g"
        kSecAttrService = "s"; kSecAttrAccount = "a"; kSecValueData = "v"

        @staticmethod
        def SecItemCopyMatching(_q, _o):
            return -34018  # errSecMissingEntitlement

    import types
    fake_foundation = types.SimpleNamespace(NSData=types.SimpleNamespace(
        dataWithBytes_length_=lambda b, n: ("data", b)))
    big = "y" * macos_keychain.SECURITY_STDIN_LINE_LIMIT
    with patch("claude_swap.macos_keychain._security_framework",
               return_value=(FailingSecurity, fake_foundation)):
        with pytest.raises(macos_keychain.KeychainError,
                           match="status=-34018") as exc:
            macos_keychain.set_password("svc", "acct", big)
        assert big not in str(exc.value)  # error never carries the secret


def test_set_password_raises_on_nonzero():
    with patch("claude_swap.macos_keychain.subprocess.run") as run:
        run.return_value = _completed(45, stderr="nope")
        with pytest.raises(macos_keychain.KeychainError):
            macos_keychain.set_password("svc", "acct", "secret")


def test_set_get_roundtrip_hex_is_decodable():
    # The hex written on set must decode back to the original UTF-8 secret.
    secret = 'token-with "quotes" and \\ backslash and é'
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _completed(0)

    with patch("claude_swap.macos_keychain.subprocess.run", side_effect=fake_run):
        macos_keychain.set_password("svc", "acct", secret)
    stdin = captured["kwargs"]["input"]
    hex_token = stdin.split("-X ", 1)[1].strip()
    assert bytes.fromhex(hex_token).decode("utf-8") == secret


# ---------------------------------------------------------------------------
# delete_password
# ---------------------------------------------------------------------------


def test_delete_password_rc0_and_rc44_are_success():
    for rc in (0, 44):
        with patch("claude_swap.macos_keychain.subprocess.run") as run:
            run.return_value = _completed(rc)
            macos_keychain.delete_password("svc", "acct")  # no raise


def test_delete_password_raises_on_other_nonzero():
    # framework pinned off: in a dev env with the menubar extra the
    # denial fallback would engage and touch the real keychain
    with patch("claude_swap.macos_keychain._security_framework",
               return_value=None), \
         patch("claude_swap.macos_keychain.subprocess.run") as run:
        run.return_value = _completed(51, stderr="locked")
        with pytest.raises(macos_keychain.KeychainError):
            macos_keychain.delete_password("svc", "acct")


# ---------------------------------------------------------------------------
# timeouts — a wedged Keychain must surface as KeychainError, never a hang
# ---------------------------------------------------------------------------


def test_calls_pass_timeout_to_subprocess():
    with patch("claude_swap.macos_keychain.subprocess.run") as run:
        run.return_value = _completed(0, stdout="x\n")
        macos_keychain.get_password("svc", "acct")
        assert run.call_args.kwargs.get("timeout") == macos_keychain._TIMEOUT


@pytest.mark.parametrize("fn,args", [
    ("get_password", ("svc", "acct")),
    ("set_password", ("svc", "acct", "secret")),
    ("delete_password", ("svc", "acct")),
])
def test_timeout_becomes_keychain_error(fn, args):
    timeout = subprocess.TimeoutExpired(cmd="security", timeout=5)
    # framework pinned off (see delete test); the fallback path has its
    # own coverage below
    with patch("claude_swap.macos_keychain._security_framework",
               return_value=None), \
         patch("claude_swap.macos_keychain.subprocess.run",
               side_effect=timeout):
        with pytest.raises(macos_keychain.KeychainError):
            getattr(macos_keychain, fn)(*args)


def test_item_exists_stays_false_on_timeout_and_missing_binary():
    # item_exists must never raise (it feeds cleanup, not the capability cache).
    timeout = subprocess.TimeoutExpired(cmd="security", timeout=5)
    with patch("claude_swap.macos_keychain.subprocess.run", side_effect=timeout):
        assert macos_keychain.item_exists("svc", "acct") is False
    with patch("claude_swap.macos_keychain.subprocess.run", side_effect=FileNotFoundError):
        assert macos_keychain.item_exists("svc", "acct") is False


# ---------------------------------------------------------------------------
# keychain_account_name — mirror Claude Code's getUsername()
# ---------------------------------------------------------------------------


def test_keychain_account_name_prefers_user_env(monkeypatch):
    monkeypatch.setenv("USER", "alice")
    assert macos_keychain.keychain_account_name() == "alice"


def test_keychain_account_name_no_user_env_avoids_legacy_default(monkeypatch):
    # The old active-store default was the bare string "user", which mismatches
    # Claude Code's OS-username on headless hosts ($USER unset). The shared helper
    # must fall back to the OS username / "claude-code-user", never "user".
    monkeypatch.delenv("USER", raising=False)
    name = macos_keychain.keychain_account_name()
    assert name and name != "user"


# The real-Keychain round-trip test lives in test_macos_keychain_contract.py,
# next to the `tmp_keychain` fixture it depends on.
