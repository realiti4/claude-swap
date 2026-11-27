"""`cswap artifact-owner` hands over to the claude-artifact-owner package (the 'artifact-owner' extra)."""

from __future__ import annotations

import sys
import textwrap
import types
from unittest.mock import patch

import pytest

from claude_swap import cli


def _fake_artifact_owner(calls: list, code: int = 0) -> types.ModuleType:
    mod = types.ModuleType("claude_artifact_owner")

    def main(argv=None, prog="artifact-owner"):
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
    monkeypatch.setitem(sys.modules, "claude_artifact_owner", _fake_artifact_owner(calls))
    assert _run("artifact-owner", "who", "https://claude.ai/artifact/x") == 0
    # the package names itself `cswap artifact-owner`, so the hook entries it prints run it through cswap
    assert calls == [(["who", "https://claude.ai/artifact/x"], "cswap artifact-owner")]


def test_passes_the_exit_code_through(monkeypatch):
    monkeypatch.setitem(sys.modules, "claude_artifact_owner", _fake_artifact_owner([], code=2))
    assert _run("artifact-owner", "bogus") == 2


def test_without_the_extra_it_says_how_to_install_it(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "claude_artifact_owner", None)  # makes the import raise ImportError
    assert _run("artifact-owner", "hooks") == 1
    assert "uv tool install 'claude-swap[artifact-owner]'" in capsys.readouterr().err


def test_listed_in_the_help(capsys):
    assert _run("help") == 0
    assert "artifact-owner hooks" in capsys.readouterr().out


def test_an_extra_that_cant_load_says_why(monkeypatch, tmp_path, capsys):
    (tmp_path / "claude_artifact_owner.py").write_text(textwrap.dedent("""
        raise ImportError("claude-artifact-owner runs on Linux only")
    """))
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "claude_artifact_owner", raising=False)
    assert _run("artifact-owner", "status") == 1
    err = capsys.readouterr().err
    assert "runs on Linux only" in err and "Install with" not in err
