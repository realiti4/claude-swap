"""Unit tests for the macOS ``security``-CLI wrapper (claude_swap.macos_keychain).

These mock ``subprocess.run`` so they exercise the wrapper's argv/stdin shaping,
hex encoding, and return-code handling without ever invoking the real
``security`` binary. (The autouse ``block_real_keychain`` guard replaces the
module's functions for *other* tests; here we patch ``subprocess`` so the real
function bodies run against a fake process.)
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
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
    with patch("claude_swap.macos_keychain.subprocess.run") as run:
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


def test_set_password_large_payload_falls_back_to_argv():
    big = "x" * macos_keychain.SECURITY_STDIN_LINE_LIMIT  # hex doubles the length
    with patch("claude_swap.macos_keychain.subprocess.run") as run:
        run.return_value = _completed(0)
        macos_keychain.set_password("svc", "acct", big)

        args = run.call_args.args[0]
        assert args[:3] == ["/usr/bin/security", "add-generic-password", "-U"]  # argv path
        assert "input" not in run.call_args.kwargs  # not via stdin
        # Hex value passed as a raw list element (no shell, no quoting).
        assert big.encode().hex() in args
        assert "acct" in args and "svc" in args


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
    with patch("claude_swap.macos_keychain.subprocess.run") as run:
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
    with patch("claude_swap.macos_keychain.subprocess.run", side_effect=timeout):
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


# ---------------------------------------------------------------------------
# item_modified_at
# ---------------------------------------------------------------------------


def test_item_modified_at_parses_a_captured_attribute_block():
    """T1312 [m]: parsed against a REALISTIC captured
    ``security find-generic-password`` attribute block (no ``-w``), not a
    hand-built string that happens to satisfy the regex."""
    stdout = (
        'keychain: "/Users/x/Library/Keychains/login.keychain-db"\n'
        'version: 512\n'
        'class: "genp"\n'
        'attributes:\n'
        '    0x00000007 <blob>="Claude Code-credentials"\n'
        '    0x00000008 <blob>=<NULL>\n'
        '    "acct"<blob>="x"\n'
        '    "cdat"<timedate>=0x32303236303931383132333435365A00  '
        '"20260918123456Z"\n'
        '    "crtr"<uint32>=<NULL>\n'
        '    "cusi"<sint32>=<NULL>\n'
        '    "invi"<sint32>=0x0\n'
        '    "mdat"<timedate>=0x32303236303932343130313533305A00  '
        '"20260924101530Z"\n'
        '    "prot"<blob>=<NULL>\n'
        '    "scrp"<sint32>=<NULL>\n'
        '    "svce"<blob>="Claude Code-credentials"\n'
        '    "type"<uint32>=<NULL>\n'
    )
    with patch("claude_swap.macos_keychain.subprocess.run") as run:
        run.return_value = _completed(0, stdout=stdout)
        got = macos_keychain.item_modified_at("Claude Code-credentials", "x")
    expected = datetime.strptime(
        "20260924101530", "%Y%m%d%H%M%S"
    ).replace(tzinfo=timezone.utc).timestamp()
    assert got == expected
    args = run.call_args.args[0]
    assert "-w" not in args, "the mdat read must be attribute-only, no -w"


@pytest.mark.parametrize("returncode, stdout", [
    (44, ""),                    # absent item (rc-44)
    (0, "attributes:\n"),        # no "mdat" attribute in the output
])
def test_item_modified_at_none_when_unavailable(returncode, stdout):
    with patch("claude_swap.macos_keychain.subprocess.run") as run:
        run.return_value = _completed(returncode, stdout=stdout)
        assert macos_keychain.item_modified_at("svc", "acct") is None


# The real-Keychain round-trip test lives in test_macos_keychain_contract.py,
# next to the `tmp_keychain` fixture it depends on.
