"""`cswap rc-revive` hands over to the claude-rc-revive package (the 'rc-revive' extra)."""

from __future__ import annotations

import sys
import textwrap
import types
from unittest.mock import patch

import pytest

from claude_swap import cli


def _fake_rc_revive(calls: list, code: int = 0) -> types.ModuleType:
    mod = types.ModuleType("claude_rc_revive")

    def main(argv=None, prog="rc-revive"):
        calls.append((argv, prog))
        return code

    mod.main = main
    return mod


def _run(*args: str) -> int:
    with patch.object(sys, "argv", ["cswap", *args]):
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
    return excinfo.value.code


def test_hands_the_arguments_over(monkeypatch):
    calls: list = []
    monkeypatch.setitem(sys.modules, "claude_rc_revive", _fake_rc_revive(calls))
    assert _run("rc-revive", "revive", "lost") == 0
    assert calls == [(["revive", "lost"], "cswap rc-revive")]


def test_passes_the_exit_code_through(monkeypatch):
    monkeypatch.setitem(sys.modules, "claude_rc_revive", _fake_rc_revive([], code=3))
    assert _run("rc-revive", "status") == 3


def test_without_the_extra_it_says_how_to_install_it(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "claude_rc_revive", None)  # makes the import raise ImportError
    assert _run("rc-revive", "status") == 1
    assert "uv tool install 'claude-swap[rc-revive]'" in capsys.readouterr().err


def test_listed_in_the_help(capsys):
    assert _run("help") == 0
    assert "rc-revive install-service" in capsys.readouterr().out


def test_an_extra_that_cant_load_says_why(monkeypatch, tmp_path, capsys):
    (tmp_path / "claude_rc_revive.py").write_text(textwrap.dedent("""
        raise ImportError("claude-rc-revive runs on Linux only")
    """))
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "claude_rc_revive", raising=False)
    assert _run("rc-revive", "status") == 1
    err = capsys.readouterr().err
    assert "runs on Linux only" in err and "Install with" not in err
