"""Tests for the macOS Terminal launcher.

Every test mocks ``osascript`` so no Terminal window or cswap session is ever
started while validating the launcher.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import macos_terminal


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["osascript"], returncode=returncode, stdout=stdout, stderr=stderr
    )


@pytest.fixture
def cswap_executable(tmp_path: Path) -> Path:
    executable = tmp_path / "Claude Swap" / "cswap"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    return executable


def test_build_run_command_quotes_executable_and_slot_for_terminal_shell():
    executable = "/Applications/Claude Swap/cswap's"

    command = macos_terminal.build_run_command("007", cswap_executable=executable)

    assert shlex.split(command) == [executable, "run", "7"]
    script = macos_terminal._terminal_script(command)
    assert script.startswith('tell application "Terminal" to do script "')
    assert '\\"' in script  # shell's embedded double quotes are AppleScript-escaped


def test_terminal_command_uses_the_cli_session_path_without_credentials():
    command = macos_terminal.build_run_command(2)

    assert shlex.split(command) == ["cswap", "run", "2"]
    assert "CLAUDE_CONFIG_DIR=" not in command
    assert ".credentials.json" not in command


def test_launch_terminal_rejects_empty_path_without_invoking_subprocess():
    with patch("claude_swap.macos_terminal.subprocess.run") as run:
        result = macos_terminal.launch_terminal("2", cswap_executable=Path())

    assert result.launched is False
    assert result.error_code is macos_terminal.TerminalLaunchErrorCode.INVALID_EXECUTABLE
    assert result.command is None
    run.assert_not_called()


def test_launch_terminal_rejects_missing_executable_without_invoking_applescript():
    with (
        patch("claude_swap.macos_terminal.shutil.which", return_value=None),
        patch("claude_swap.macos_terminal.subprocess.run") as run,
    ):
        result = macos_terminal.launch_terminal("2")

    assert result.launched is False
    assert result.error_code is macos_terminal.TerminalLaunchErrorCode.INVALID_EXECUTABLE
    assert "not found or is not executable" in (result.error_message or "")
    run.assert_not_called()


def test_launch_terminal_rejects_non_executable_file(tmp_path: Path):
    executable = tmp_path / "cswap"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o644)

    with patch("claude_swap.macos_terminal.subprocess.run") as run:
        result = macos_terminal.launch_terminal("2", cswap_executable=executable)

    assert result.launched is False
    assert result.error_code is macos_terminal.TerminalLaunchErrorCode.INVALID_EXECUTABLE
    run.assert_not_called()


def test_launch_terminal_resolves_bare_cswap_to_absolute_path(cswap_executable: Path):
    with (
        patch(
            "claude_swap.macos_terminal.shutil.which",
            return_value=str(cswap_executable),
        ) as which,
        patch(
            "claude_swap.macos_terminal.subprocess.run",
            return_value=_completed(0),
        ),
    ):
        result = macos_terminal.launch_terminal("2")

    which.assert_called_once_with("cswap")
    assert result.launched is True
    assert result.command is not None
    assert shlex.split(result.command) == [str(cswap_executable.resolve()), "run", "2"]


def test_launch_terminal_falls_back_to_the_absolute_launch_executable(cswap_executable: Path):
    with (
        patch(
            "claude_swap.macos_terminal.shutil.which",
            side_effect=[None, str(cswap_executable)],
        ) as which,
        patch.object(macos_terminal.sys, "argv", [str(cswap_executable), "menubar"]),
        patch(
            "claude_swap.macos_terminal.subprocess.run",
            return_value=_completed(0),
        ),
    ):
        result = macos_terminal.launch_terminal("2")

    assert result.launched is True
    assert result.command is not None
    assert shlex.split(result.command) == [str(cswap_executable.resolve()), "run", "2"]
    assert [call.args[0] for call in which.call_args_list] == ["cswap", str(cswap_executable)]


@pytest.mark.parametrize("slot", [0, -1, True, "", "0", "-1", "1; rm -rf /", "1 2"])
def test_validate_slot_rejects_non_positive_or_non_numeric_values(slot: int | str):
    with pytest.raises(ValueError):
        macos_terminal.validate_slot(slot)


def test_launch_terminal_rejects_invalid_slot_without_invoking_subprocess():
    with patch("claude_swap.macos_terminal.subprocess.run") as run:
        result = macos_terminal.launch_terminal("7; open -a Terminal")

    assert result.launched is False
    assert result.error_code is macos_terminal.TerminalLaunchErrorCode.INVALID_SLOT
    assert result.command is None
    run.assert_not_called()


def test_launch_terminal_invokes_osascript_with_argument_list(cswap_executable: Path):
    with patch("claude_swap.macos_terminal.subprocess.run") as run:
        run.return_value = _completed(0)

        result = macos_terminal.launch_terminal(4, cswap_executable=cswap_executable)

    assert result.launched is True
    assert result.slot == "4"
    assert result.command is not None
    assert shlex.split(result.command) == [str(cswap_executable.resolve()), "run", "4"]
    args = run.call_args.args[0]
    kwargs = run.call_args.kwargs
    assert args[0:2] == ["/usr/bin/osascript", "-e"]
    assert result.command in args[2]
    assert kwargs == {
        "capture_output": True,
        "text": True,
        "check": False,
        "timeout": 5.0,
    }


def test_launch_terminal_returns_structured_applescript_error(cswap_executable: Path):
    with patch("claude_swap.macos_terminal.subprocess.run") as run:
        run.return_value = _completed(1, stderr="Terminal is unavailable\n")

        result = macos_terminal.launch_terminal("2", cswap_executable=cswap_executable)

    assert result.launched is False
    assert result.slot == "2"
    assert result.error_code is macos_terminal.TerminalLaunchErrorCode.APPLESCRIPT_FAILED
    assert result.error_message == "Terminal is unavailable"


def test_launch_terminal_returns_structured_missing_osascript_error(cswap_executable: Path):
    with patch(
        "claude_swap.macos_terminal.subprocess.run",
        side_effect=FileNotFoundError("osascript missing"),
    ):
        result = macos_terminal.launch_terminal("2", cswap_executable=cswap_executable)

    assert result.launched is False
    assert result.error_code is macos_terminal.TerminalLaunchErrorCode.OSASCRIPT_UNAVAILABLE
    assert result.error_message == "osascript missing"


def test_launch_terminal_returns_structured_timeout_error(cswap_executable: Path):
    timeout = subprocess.TimeoutExpired(cmd="osascript", timeout=5)
    with patch("claude_swap.macos_terminal.subprocess.run", side_effect=timeout):
        result = macos_terminal.launch_terminal("2", cswap_executable=cswap_executable)

    assert result.launched is False
    assert result.error_code is macos_terminal.TerminalLaunchErrorCode.OSASCRIPT_TIMED_OUT
    assert result.error_message == "osascript timed out after 5s"


def test_launch_terminal_returns_structured_os_error(cswap_executable: Path):
    with patch(
        "claude_swap.macos_terminal.subprocess.run",
        side_effect=OSError("launch service unavailable"),
    ):
        result = macos_terminal.launch_terminal("2", cswap_executable=cswap_executable)

    assert result.launched is False
    assert result.error_code is macos_terminal.TerminalLaunchErrorCode.OSASCRIPT_UNAVAILABLE
    assert result.error_message == "launch service unavailable"
