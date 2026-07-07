"""Tests for macOS menu bar LaunchAgent management."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from claude_swap import menubar_launch_agent


def test_install_writes_virtualenv_python_and_bootstraps(tmp_path):
    completed = subprocess.CompletedProcess([], 0, "", "")
    with (
        patch.object(sys, "platform", "darwin"),
        patch.object(Path, "home", return_value=tmp_path),
        patch.object(
            menubar_launch_agent, "_launchctl", return_value=completed
        ) as launchctl,
    ):
        path = menubar_launch_agent.install()

    payload = plistlib.loads(path.read_bytes())
    assert payload["Label"] == menubar_launch_agent.LABEL
    assert payload["ProgramArguments"] == [
        sys.executable,
        "-m",
        "claude_swap",
        "--menubar",
    ]
    assert payload["RunAtLoad"] is True
    assert launchctl.call_args_list[-1].args[:2] == (
        "bootstrap",
        f"gui/{os.getuid()}",
    )


def test_uninstall_is_idempotent(tmp_path):
    path = (
        tmp_path
        / "Library"
        / "LaunchAgents"
        / f"{menubar_launch_agent.LABEL}.plist"
    )
    path.parent.mkdir(parents=True)
    path.write_text("placeholder", encoding="utf-8")
    completed = subprocess.CompletedProcess([], 0, "", "")
    with (
        patch.object(sys, "platform", "darwin"),
        patch.object(Path, "home", return_value=tmp_path),
        patch.object(
            menubar_launch_agent, "_launchctl", return_value=completed
        ),
    ):
        menubar_launch_agent.uninstall()
        menubar_launch_agent.uninstall()
    assert not path.exists()


def test_status_requires_plist_and_loaded_service(tmp_path):
    completed = subprocess.CompletedProcess([], 0, "", "")
    with (
        patch.object(sys, "platform", "darwin"),
        patch.object(Path, "home", return_value=tmp_path),
        patch.object(
            menubar_launch_agent, "_launchctl", return_value=completed
        ) as launchctl,
    ):
        assert menubar_launch_agent.is_installed() is False
        launchctl.assert_not_called()

        path = menubar_launch_agent.plist_path()
        path.parent.mkdir(parents=True)
        path.write_text("placeholder", encoding="utf-8")
        assert menubar_launch_agent.is_installed() is True
