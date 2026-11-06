"""Tests for the StopFailure hook installer (hooks.py).

Every test edits a temp settings.json seeded with unrelated hooks, so the
"never touch anything that isn't ours" contract is checked directly.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap.exceptions import ConfigError
from claude_swap.hooks import (
    HOOK_EVENT,
    HOOK_MATCHER,
    default_hook_command,
    install_hook,
    installed_command,
    is_our_command,
    uninstall_hook,
)

CMD = "C:/Users/me/.local/bin/cswap.exe resume arm"

EXISTING = {
    "model": "opus",
    "permissions": {"allow": ["Bash(git status)"]},
    "hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": 'node "C:/x/gate-stop.mjs"'}]}],
        "StopFailure": [
            {"matcher": "overloaded", "hooks": [{"type": "command", "command": "notify.sh"}]}
        ],
    },
}


@pytest.fixture
def settings(tmp_path: Path) -> Path:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(EXISTING, indent=2))
    return path


def _read(path: Path) -> dict:
    return json.loads(path.read_text())


class TestInstall:
    def test_adds_our_entry_and_keeps_everything_else(self, settings: Path, tmp_path: Path):
        change = install_hook(tmp_path / "snaps", CMD, settings)
        assert change.changed
        data = _read(settings)
        assert data["model"] == "opus"
        assert data["permissions"] == EXISTING["permissions"]
        assert data["hooks"]["Stop"] == EXISTING["hooks"]["Stop"]
        groups = data["hooks"][HOOK_EVENT]
        assert groups[0] == EXISTING["hooks"]["StopFailure"][0]  # the user's own, untouched
        ours = groups[1]
        assert ours["matcher"] == HOOK_MATCHER
        assert ours["hooks"] == [{"type": "command", "command": CMD, "timeout": 30, "async": True}]

    def test_snapshots_the_original_first(self, settings: Path, tmp_path: Path):
        original = settings.read_text()
        change = install_hook(tmp_path / "snaps", CMD, settings)
        assert change.snapshot is not None and change.snapshot.read_text() == original

    def test_second_install_is_a_no_op(self, settings: Path, tmp_path: Path):
        install_hook(tmp_path / "snaps", CMD, settings)
        before = settings.read_text()
        change = install_hook(tmp_path / "snaps", CMD, settings)
        assert not change.changed
        assert settings.read_text() == before
        assert len(_read(settings)["hooks"][HOOK_EVENT]) == 2

    def test_reinstall_with_a_new_command_updates_in_place(self, settings: Path, tmp_path: Path):
        install_hook(tmp_path / "snaps", CMD, settings)
        newer = "/usr/local/bin/cswap resume arm"
        change = install_hook(tmp_path / "snaps", newer, settings)
        assert change.changed
        groups = _read(settings)["hooks"][HOOK_EVENT]
        assert len(groups) == 2
        assert groups[1]["hooks"][0]["command"] == newer

    def test_creates_the_file_when_missing(self, tmp_path: Path):
        path = tmp_path / "new" / "settings.json"
        change = install_hook(tmp_path / "snaps", CMD, path)
        assert change.changed and change.snapshot is None
        assert installed_command(path) == CMD

    def test_malformed_json_is_refused_untouched(self, tmp_path: Path):
        path = tmp_path / "settings.json"
        path.write_text("{not json")
        with pytest.raises(ConfigError, match="not valid JSON"):
            install_hook(tmp_path / "snaps", CMD, path)
        assert path.read_text() == "{not json"

    def test_hooks_of_the_wrong_type_are_refused(self, tmp_path: Path):
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"hooks": []}))
        with pytest.raises(ConfigError, match="'hooks' is not an object"):
            install_hook(tmp_path / "snaps", CMD, path)


class TestUninstall:
    def test_removes_only_our_entry(self, settings: Path, tmp_path: Path):
        install_hook(tmp_path / "snaps", CMD, settings)
        change = uninstall_hook(tmp_path / "snaps", settings)
        assert change.changed
        assert _read(settings) == EXISTING

    def test_prunes_containers_it_emptied(self, tmp_path: Path):
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"model": "opus"}))
        install_hook(tmp_path / "snaps", CMD, path)
        uninstall_hook(tmp_path / "snaps", path)
        assert _read(path) == {"model": "opus"}

    def test_not_installed_is_a_no_op(self, settings: Path, tmp_path: Path):
        before = settings.read_text()
        assert not uninstall_hook(tmp_path / "snaps", settings).changed
        assert settings.read_text() == before


class TestRecognition:
    @pytest.mark.parametrize(
        "command",
        [
            "cswap resume arm",
            '"C:/Program Files/cswap/cswap.exe" resume arm',
            "/usr/bin/python3 -m claude_swap resume arm",
        ],
    )
    def test_ours(self, command):
        assert is_our_command(command)

    @pytest.mark.parametrize("command", ["notify.sh", "cswap auto", "other resume arm", None])
    def test_not_ours(self, command):
        assert not is_our_command(command)

    def test_default_command_ends_with_the_arm_verb(self):
        assert default_hook_command().endswith("resume arm")
        assert is_our_command(default_hook_command())

    def test_default_command_is_the_running_cswap_launcher(self, tmp_path: Path):
        # Several cswap installs can sit on PATH; the hook must run the one
        # that performed the install, never "whichever PATH finds first".
        launcher = tmp_path / "pipx bin" / "cswap.exe"
        launcher.parent.mkdir()
        launcher.write_text("")
        with patch.object(sys, "argv", [str(launcher), "resume", "install"]):
            command = default_hook_command()
        assert command == f'"{launcher.resolve().as_posix()}" resume arm'  # quoted: has a space

    def test_default_command_falls_back_to_this_interpreter(self):
        with patch.object(sys, "argv", ["-m", "resume"]):
            command = default_hook_command()
        assert command.endswith(" -m claude_swap resume arm")
        assert Path(sys.executable).as_posix() in command
