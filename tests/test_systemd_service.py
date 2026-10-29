"""Tests for the systemd user service that keeps ``cswap auto`` running on Linux.

``subprocess.run`` is patched throughout so the real ``systemctl`` and
``loginctl`` are never driven: these assert the unit this module writes, the
argv it shapes and how it reads systemctl's replies, not systemd's behaviour.
Every test pins ``home`` to a tmp path, so nothing here depends on the machine
it runs on or writes outside the temp directory.
"""

from __future__ import annotations

import stat
import subprocess
import sys
from unittest.mock import patch

import pytest

from claude_swap import systemd_service
from claude_swap.exceptions import ClaudeSwitchError

PROGRAM = ["/home/x/.local/bin/cswap"]


@pytest.fixture(autouse=True)
def _on_linux_with_systemd():
    """The module refuses off-linux or without systemctl; exercise the linux path."""
    with patch.object(systemd_service.sys, "platform", "linux"), patch.object(
        systemd_service.shutil, "which", lambda name: f"/usr/bin/{name}"
    ):
        yield


def _completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class _Recorder:
    """Fake subprocess.run: records argv, answers `show` and `loginctl` from fixtures."""

    def __init__(self, show: str = "", linger: str = "no", fail: dict[str, int] | None = None):
        self.calls: list[list[str]] = []
        self.show = show
        self.linger = linger
        self.fail = fail or {}

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[0] == "loginctl":
            return _completed(0, stdout=self.linger + "\n")
        verb = argv[2]
        if verb in self.fail:
            return _completed(self.fail[verb], stderr=f"{verb} went wrong")
        if verb == "show":
            return _completed(0, stdout=self.show)
        return _completed(0)

    def verbs(self) -> list[str]:
        return [c[2] for c in self.calls if c[0] == "systemctl"]


ACTIVE = (
    "LoadState=loaded\nActiveState=active\nSubState=running\n"
    "UnitFileState=enabled\nMainPID=4242\n"
)
ABSENT = "LoadState=not-found\nActiveState=inactive\nSubState=dead\nUnitFileState=\nMainPID=0\n"


# --- unit shape ------------------------------------------------------------


def test_unit_runs_the_auto_subcommand_and_restarts_only_on_failure():
    unit = systemd_service.build_unit(PROGRAM, env={})
    assert f"ExecStart={PROGRAM[0]} auto\n" in unit
    assert "Restart=on-failure" in unit
    assert "WantedBy=default.target" in unit
    assert "Environment=PATH=/home/x/.local/bin:" in unit


def test_unit_quotes_paths_with_spaces_and_systemd_specifiers():
    unit = systemd_service.build_unit(["/home/a b/100%/cswap"], env={})
    assert 'ExecStart="/home/a b/100%%/cswap" auto' in unit


def test_unit_forwards_only_proxy_variables_and_keeps_empty_ones():
    env = {"HTTPS_PROXY": "http://proxy:3128", "https_proxy": "", "SECRET": "nope"}
    unit = systemd_service.build_unit(PROGRAM, env=env)
    assert "Environment=HTTPS_PROXY=http://proxy:3128" in unit
    assert "Environment=https_proxy=\n" in unit
    assert "SECRET" not in unit


def test_unit_path_honours_xdg_config_home(tmp_path):
    env = {"XDG_CONFIG_HOME": str(tmp_path / "cfg")}
    assert systemd_service.unit_path(env=env) == tmp_path / "cfg" / "systemd" / "user" / "cswap-auto.service"


# --- install / uninstall / status ------------------------------------------


def test_install_writes_private_unit_then_reloads_enables_and_restarts(tmp_path):
    rec = _Recorder()
    with patch.object(systemd_service.subprocess, "run", rec):
        result = systemd_service.install(home=tmp_path, program=PROGRAM)
    path = tmp_path / ".config" / "systemd" / "user" / "cswap-auto.service"
    assert path.exists()
    if sys.platform != "win32":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert rec.verbs() == ["daemon-reload", "enable", "restart"]
    assert result["program"] == [*PROGRAM, "auto"]
    assert result["linger"] is False


def test_install_surfaces_a_systemctl_failure(tmp_path):
    rec = _Recorder(fail={"enable": 1})
    with patch.object(systemd_service.subprocess, "run", rec), pytest.raises(ClaudeSwitchError, match="enable"):
        systemd_service.install(home=tmp_path, program=PROGRAM)


def test_status_reads_state_pid_and_linger(tmp_path):
    (tmp_path / ".config" / "systemd" / "user").mkdir(parents=True)
    (tmp_path / ".config" / "systemd" / "user" / "cswap-auto.service").write_text("x")
    rec = _Recorder(show=ACTIVE, linger="yes")
    with patch.object(systemd_service.subprocess, "run", rec):
        s = systemd_service.status(home=tmp_path)
    assert s == {
        "unit": "cswap-auto.service",
        "installed": True,
        "loaded": True,
        "enabled": True,
        "active": True,
        "state": "running",
        "pid": 4242,
        "unit_path": str(tmp_path / ".config" / "systemd" / "user" / "cswap-auto.service"),
        "linger": True,
    }


def test_uninstall_disables_and_removes_the_unit(tmp_path):
    rec = _Recorder()
    with patch.object(systemd_service.subprocess, "run", rec):
        systemd_service.install(home=tmp_path, program=PROGRAM)
    rec = _Recorder(show=ACTIVE)
    with patch.object(systemd_service.subprocess, "run", rec):
        result = systemd_service.uninstall(home=tmp_path)
    assert result == {"unit": "cswap-auto.service", "was_active": True, "removed_unit": True}
    assert "disable" in rec.verbs()
    assert not (tmp_path / ".config" / "systemd" / "user" / "cswap-auto.service").exists()


def test_uninstall_of_nothing_is_a_quiet_no_op(tmp_path):
    rec = _Recorder(show=ABSENT)
    with patch.object(systemd_service.subprocess, "run", rec):
        result = systemd_service.uninstall(home=tmp_path)
    assert result == {"unit": "cswap-auto.service", "was_active": False, "removed_unit": False}
    assert "disable" not in rec.verbs()


# --- platform guards -------------------------------------------------------


def test_refuses_on_macos_and_points_at_the_menu_bar_service(tmp_path):
    with patch.object(systemd_service.sys, "platform", "darwin"), pytest.raises(
        ClaudeSwitchError, match="menubar --install-service"
    ):
        systemd_service.install(home=tmp_path, program=PROGRAM)


def test_refuses_without_systemctl(tmp_path):
    with patch.object(systemd_service.shutil, "which", lambda name: None), pytest.raises(
        ClaudeSwitchError, match="needs systemd"
    ):
        systemd_service.status(home=tmp_path)
