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
# get_password / item_exists / delete_password / set_password — ``keychain``
# targeting (issue #279's testability seam: a positional keychain argument,
# opt-in, default None = unchanged prior behavior)
# ---------------------------------------------------------------------------


def test_get_password_keychain_none_matches_prior_argv_exactly():
    with patch("claude_swap.macos_keychain.subprocess.run") as run:
        run.return_value = _completed(0, stdout="v\n")
        macos_keychain.get_password("svc", "acct")
        args = run.call_args.args[0]
        assert args == [
            "/usr/bin/security", "find-generic-password",
            "-a", "acct", "-w", "-s", "svc",
        ]


def test_get_password_keychain_given_appends_trailing_positional():
    with patch("claude_swap.macos_keychain.subprocess.run") as run:
        run.return_value = _completed(0, stdout="v\n")
        macos_keychain.get_password("svc", "acct", keychain="/tmp/x.keychain")
        args = run.call_args.args[0]
        assert args[-1] == "/tmp/x.keychain"
        assert args[:-1] == [
            "/usr/bin/security", "find-generic-password",
            "-a", "acct", "-w", "-s", "svc",
        ]


def test_item_exists_keychain_given_appends_trailing_positional():
    with patch("claude_swap.macos_keychain.subprocess.run") as run:
        run.return_value = _completed(0)
        macos_keychain.item_exists("svc", "acct", keychain="/tmp/x.keychain")
        args = run.call_args.args[0]
        assert args[-1] == "/tmp/x.keychain"


def test_delete_password_keychain_given_appends_trailing_positional():
    with patch("claude_swap.macos_keychain.subprocess.run") as run:
        run.return_value = _completed(0)
        macos_keychain.delete_password("svc", "acct", keychain="/tmp/x.keychain")
        args = run.call_args.args[0]
        assert args[-1] == "/tmp/x.keychain"


def test_set_password_keychain_given_appends_trailing_positional_in_stdin():
    with patch("claude_swap.macos_keychain.subprocess.run") as run:
        run.return_value = _completed(0)
        macos_keychain.set_password(
            "svc", "acct", "secret", keychain="/tmp/x.keychain"
        )
        stdin = run.call_args.kwargs["input"]
        assert stdin.rstrip("\n").endswith('"/tmp/x.keychain"')


# ---------------------------------------------------------------------------
# set_password — ``trusted_apps`` (issue #279: a freshly-created item's ACL
# by default trusts only /usr/bin/security, which a headless in-process
# Security.framework reader — e.g. Claude Code — can't read without a GUI
# prompt). See tests/test_macos_keychain_contract.py's
# TestKeychainAclPreservedOnWrite for the real-Keychain ACL-widening proof;
# these mock subprocess to check the argv/stdin shape and the
# delete-then-recreate call ordering.
# ---------------------------------------------------------------------------


def test_set_password_no_trusted_apps_is_byte_for_byte_unchanged():
    # The exact stdin string set_password produced before #279, verbatim.
    with patch("claude_swap.macos_keychain.subprocess.run") as run:
        run.return_value = _completed(0)
        macos_keychain.set_password("svc", "acct", "secret")
        stdin = run.call_args.kwargs["input"]
        hex_value = "secret".encode().hex()
        assert stdin == f'add-generic-password -U -a "acct" -s "svc" -X {hex_value}\n'


def test_set_password_empty_trusted_apps_list_is_also_a_no_op():
    with patch("claude_swap.macos_keychain.subprocess.run") as run:
        run.return_value = _completed(0)
        macos_keychain.set_password("svc", "acct", "secret", trusted_apps=[])
        stdin = run.call_args.kwargs["input"]
        assert "-T" not in stdin


def test_set_password_trusted_apps_adds_dash_T_per_app_and_always_security():
    with patch("claude_swap.macos_keychain.subprocess.run") as run, \
         patch("claude_swap.macos_keychain.item_exists", return_value=False):
        run.return_value = _completed(0)
        macos_keychain.set_password(
            "svc", "acct", "secret", trusted_apps=["/opt/claude"]
        )
        stdin = run.call_args.kwargs["input"]
        assert '-T "/usr/bin/security"' in stdin  # the tool itself, always
        assert '-T "/opt/claude"' in stdin


def test_set_password_trusted_apps_dedupes_when_security_already_listed():
    with patch("claude_swap.macos_keychain.subprocess.run") as run, \
         patch("claude_swap.macos_keychain.item_exists", return_value=False):
        run.return_value = _completed(0)
        macos_keychain.set_password(
            "svc", "acct", "secret",
            trusted_apps=["/usr/bin/security", "/opt/claude"],
        )
        stdin = run.call_args.kwargs["input"]
        assert stdin.count("-T ") == 2  # security once, claude once — no triple


def test_set_password_trusted_apps_on_new_item_does_not_delete_first():
    """A brand-new item: ``-T`` is honored directly by ``-U`` — no need to
    (and must not) delete first, matching the empirical finding that a
    delete+recreate is only required to widen an EXISTING item's ACL."""
    with patch("claude_swap.macos_keychain.subprocess.run") as run, \
         patch("claude_swap.macos_keychain.item_exists", return_value=False) as exists, \
         patch("claude_swap.macos_keychain.delete_password") as delete:
        run.return_value = _completed(0)
        macos_keychain.set_password(
            "svc", "acct", "secret", trusted_apps=["/opt/claude"]
        )
        exists.assert_called_once_with("svc", "acct", keychain=None)
        delete.assert_not_called()
        run.assert_called_once()  # a single add-generic-password, nothing else


def test_set_password_trusted_apps_on_existing_item_deletes_then_recreates():
    """An item that already exists: widening its ACL via ``-U -T`` in place is
    NOT safe (see set_password's docstring — SecKeychainItemSetAccess can
    raise a SecurityAgent prompt), so the fix must delete first."""
    calls = []

    def fake_delete(service, account, *, keychain=None):
        calls.append(("delete", service, account, keychain))

    with patch("claude_swap.macos_keychain.subprocess.run") as run, \
         patch("claude_swap.macos_keychain.item_exists", return_value=True), \
         patch("claude_swap.macos_keychain.delete_password", side_effect=fake_delete):
        run.return_value = _completed(0)
        macos_keychain.set_password(
            "svc", "acct", "secret",
            keychain="/tmp/x.keychain", trusted_apps=["/opt/claude"],
        )
        assert calls == [("delete", "svc", "acct", "/tmp/x.keychain")]
        # The delete must happen BEFORE the add-generic-password call.
        run.assert_called_once()


def test_set_password_trusted_apps_large_payload_falls_back_to_argv_with_dash_T():
    big = "x" * macos_keychain.SECURITY_STDIN_LINE_LIMIT
    with patch("claude_swap.macos_keychain.subprocess.run") as run, \
         patch("claude_swap.macos_keychain.item_exists", return_value=False):
        run.return_value = _completed(0)
        macos_keychain.set_password(
            "svc", "acct", big,
            keychain="/tmp/x.keychain", trusted_apps=["/opt/claude"],
        )
        args = run.call_args.args[0]
        assert "input" not in run.call_args.kwargs  # argv path, not stdin
        assert args.count("-T") == 2  # security + claude, unquoted argv
        assert "/usr/bin/security" in args and "/opt/claude" in args
        assert args[-1] == "/tmp/x.keychain"


# ---------------------------------------------------------------------------
# resolve_trusted_claude_apps
# ---------------------------------------------------------------------------


def test_resolve_trusted_claude_apps_no_claude_on_path_returns_empty(monkeypatch):
    monkeypatch.setattr(macos_keychain.shutil, "which", lambda name: None)
    assert macos_keychain.resolve_trusted_claude_apps() == []


def test_resolve_trusted_claude_apps_real_binary_returns_just_that_path(
    monkeypatch, tmp_path
):
    binary = tmp_path / "claude"
    binary.write_bytes(b"\x7fELF-not-really-but-not-a-shebang-either")
    monkeypatch.setattr(
        macos_keychain.shutil, "which",
        lambda name: str(binary) if name == "claude" else None,
    )
    apps = macos_keychain.resolve_trusted_claude_apps()
    assert apps == [str(binary.resolve())]


def test_resolve_trusted_claude_apps_shebang_script_adds_the_interpreter(
    monkeypatch, tmp_path
):
    node = tmp_path / "node"
    node.write_bytes(b"\x7fELF-fake-node-binary")
    shim = tmp_path / "claude"
    shim.write_text(f"#!{node}\nrequire('./cli.js')\n")

    def fake_which(name):
        if name == "claude":
            return str(shim)
        if name == "node":
            return str(node)
        return None

    monkeypatch.setattr(macos_keychain.shutil, "which", fake_which)
    apps = macos_keychain.resolve_trusted_claude_apps()
    assert apps == [str(shim.resolve()), str(node.resolve())]


def test_resolve_trusted_claude_apps_env_shebang_resolves_the_named_interpreter(
    monkeypatch, tmp_path
):
    node = tmp_path / "node"
    node.write_bytes(b"\x7fELF-fake-node-binary")
    shim = tmp_path / "claude"
    shim.write_text("#!/usr/bin/env node\nrequire('./cli.js')\n")

    def fake_which(name):
        if name == "claude":
            return str(shim)
        if name == "node":
            return str(node)
        return None

    monkeypatch.setattr(macos_keychain.shutil, "which", fake_which)
    apps = macos_keychain.resolve_trusted_claude_apps()
    assert apps == [str(shim.resolve()), str(node.resolve())]


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


# The real-Keychain round-trip test lives in test_macos_keychain_contract.py,
# next to the `tmp_keychain` fixture it depends on.
