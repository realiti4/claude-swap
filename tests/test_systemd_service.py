"""Tests for the systemd user service that keeps ``cswap auto`` running on Linux.

``subprocess.run`` is patched throughout so the real ``systemctl`` and
``loginctl`` are never driven: these assert the unit this module writes, the
argv it shapes and how it reads systemctl's replies, not systemd's behaviour.
Every test pins ``home`` to a tmp path and the login name, so nothing here
depends on the machine it runs on or writes outside the temp directory.
"""

from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import systemd_service
from claude_swap.exceptions import ClaudeSwitchError

# Captured before the autouse fixture pretends to be Linux, so POSIX-only
# assertions can still be skipped on a real Windows runner.
_REAL_PLATFORM = sys.platform
PROGRAM = ["/home/x/.local/bin/cswap"]
UNIT_REL = (".config", "systemd", "user", "cswap-auto.service")


@pytest.fixture(autouse=True)
def _on_linux_with_systemd():
    """The module refuses off-linux or without systemctl; exercise the linux path."""
    with patch.object(systemd_service.sys, "platform", "linux"), patch.object(
        systemd_service.shutil, "which", lambda name: f"/usr/bin/{name}"
    ), patch.object(systemd_service, "_current_user", lambda: "tester"):
        yield


def _completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


ACTIVE = (
    "LoadState=loaded\nActiveState=active\nSubState=running\n"
    "UnitFileState=enabled\nMainPID=4242\n"
)
ABSENT = "LoadState=not-found\nActiveState=inactive\nSubState=dead\nUnitFileState=\nMainPID=0\n"


class _Recorder:
    """Fake subprocess.run: records argv, answers from fixtures.

    ``show`` may be a list, consumed one reply per call (last one repeats), to
    model a state that changes between two status reads.
    """

    def __init__(self, show=ABSENT, linger=(0, "no"), fail: dict[str, int] | None = None):
        self.calls: list[list[str]] = []
        self.show = list(show) if isinstance(show, list) else [show]
        self.linger = linger
        self.fail = fail or {}

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[0] == "loginctl":
            code, out = self.linger
            return _completed(code, stdout=out + "\n")
        verb = argv[2]
        if verb in self.fail:
            return _completed(self.fail[verb], stderr=f"{verb} went wrong")
        if verb == "show":
            reply = self.show[0] if len(self.show) == 1 else self.show.pop(0)
            return _completed(0, stdout=reply)
        return _completed(0)

    def verbs(self) -> list[str]:
        return [c[2] for c in self.calls if c[0] == "systemctl"]


# --- unit shape ------------------------------------------------------------


def test_unit_runs_the_auto_subcommand_and_fails_start_if_not_executable():
    unit = systemd_service.build_unit(PROGRAM, env={})
    assert f"ExecStart={PROGRAM[0]} auto\n" in unit
    assert "Type=exec" in unit
    assert "Restart=on-failure" in unit
    assert "WantedBy=default.target" in unit
    assert "Environment=PATH=/home/x/.local/bin:" in unit


def test_exec_start_escapes_spaces_specifiers_and_variables():
    unit = systemd_service.build_unit(["/home/a b/100%/$x/cswap"], env={})
    assert 'ExecStart="/home/a b/100%%/$$x/cswap" auto' in unit


def test_environment_keeps_dollar_literal_but_escapes_specifiers():
    env = {"HTTPS_PROXY": "http://u:pa$s%40@proxy:3128"}
    unit = systemd_service.build_unit(PROGRAM, env=env)
    assert 'Environment="HTTPS_PROXY=http://u:pa$s%%40@proxy:3128"' in unit


def test_newline_in_a_forwarded_value_is_refused():
    with pytest.raises(ClaudeSwitchError, match="newline"):
        systemd_service.build_unit(PROGRAM, env={"HTTPS_PROXY": "a\nExecStartPre=/bin/false"})


def test_forwards_proxy_and_store_variables_only_and_keeps_empty_ones():
    env = {
        "HTTPS_PROXY": "http://proxy:3128",
        "https_proxy": "",
        "CLAUDE_CONFIG_DIR": "/home/x/.claude-work",
        "XDG_DATA_HOME": "/data",
        "SECRET": "nope",
    }
    unit = systemd_service.build_unit(PROGRAM, env=env)
    assert "Environment=HTTPS_PROXY=http://proxy:3128\n" in unit
    assert "Environment=https_proxy=\n" in unit
    assert "Environment=CLAUDE_CONFIG_DIR=/home/x/.claude-work\n" in unit
    assert "Environment=XDG_DATA_HOME=/data\n" in unit
    assert "SECRET" not in unit


def test_unit_path_honours_absolute_xdg_config_home():
    # A POSIX-shaped value on every runner: the rule is systemd's, not the host's.
    env = {"XDG_CONFIG_HOME": "/xdg/cfg"}
    assert systemd_service.unit_path(env=env) == Path("/xdg/cfg") / "systemd" / "user" / "cswap-auto.service"


@pytest.mark.parametrize("value", ["relative/cfg", "~/custom-config", ""])
def test_unit_path_ignores_a_non_absolute_xdg_config_home(value):
    """systemd ignores these, so the unit must go where it will look: ~/.config."""
    path = systemd_service.unit_path(env={"XDG_CONFIG_HOME": value})
    assert path.parts[-4:] == UNIT_REL
    assert "custom-config" not in str(path) and "relative" not in str(path)


# --- install ---------------------------------------------------------------


def test_install_writes_private_unit_then_reloads_enables_and_restarts(tmp_path):
    rec = _Recorder()
    with patch.object(systemd_service.subprocess, "run", rec):
        result = systemd_service.install(home=tmp_path, program=PROGRAM)
    path = tmp_path.joinpath(*UNIT_REL)
    assert path.exists()
    if _REAL_PLATFORM != "win32":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert rec.verbs() == ["show-environment", "daemon-reload", "enable", "restart"]
    assert result["program"] == [*PROGRAM, "auto"]
    assert result["linger"] is False
    assert ["loginctl", "show-user", "tester", "--property=Linger", "--value"] in rec.calls


def test_install_refuses_without_a_user_manager_and_writes_nothing(tmp_path):
    rec = _Recorder(fail={"show-environment": 1})
    with patch.object(systemd_service.subprocess, "run", rec), pytest.raises(
        ClaudeSwitchError, match="No systemd user manager"
    ):
        systemd_service.install(home=tmp_path, program=PROGRAM)
    assert not tmp_path.joinpath(*UNIT_REL).exists()


def test_install_surfaces_a_systemctl_failure(tmp_path):
    rec = _Recorder(fail={"enable": 1})
    with patch.object(systemd_service.subprocess, "run", rec), pytest.raises(ClaudeSwitchError, match="enable"):
        systemd_service.install(home=tmp_path, program=PROGRAM)


# --- status ----------------------------------------------------------------


def test_status_reads_state_pid_and_linger(tmp_path):
    tmp_path.joinpath(*UNIT_REL).parent.mkdir(parents=True)
    tmp_path.joinpath(*UNIT_REL).write_text("x")
    rec = _Recorder(show=ACTIVE, linger=(0, "yes"))
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
        "unit_path": str(tmp_path.joinpath(*UNIT_REL)),
        "linger": True,
    }


def test_status_raises_when_systemctl_show_fails(tmp_path):
    rec = _Recorder(fail={"show": 1})
    with patch.object(systemd_service.subprocess, "run", rec), pytest.raises(ClaudeSwitchError, match="show"):
        systemd_service.status(home=tmp_path)


def test_linger_is_unknown_when_loginctl_fails(tmp_path):
    rec = _Recorder(linger=(1, ""))
    with patch.object(systemd_service.subprocess, "run", rec):
        assert systemd_service.status(home=tmp_path)["linger"] is None


# --- uninstall -------------------------------------------------------------


def test_uninstall_stops_disables_and_removes_the_unit(tmp_path):
    with patch.object(systemd_service.subprocess, "run", _Recorder()):
        systemd_service.install(home=tmp_path, program=PROGRAM)
    rec = _Recorder(show=ACTIVE)
    with patch.object(systemd_service.subprocess, "run", rec):
        result = systemd_service.uninstall(home=tmp_path)
    assert result == {"unit": "cswap-auto.service", "was_active": True, "removed_unit": True}
    assert rec.verbs()[-3:] == ["stop", "disable", "daemon-reload"]
    assert not tmp_path.joinpath(*UNIT_REL).exists()


def test_uninstall_of_nothing_is_a_quiet_no_op(tmp_path):
    rec = _Recorder(show=ABSENT)
    with patch.object(systemd_service.subprocess, "run", rec):
        result = systemd_service.uninstall(home=tmp_path)
    assert result == {"unit": "cswap-auto.service", "was_active": False, "removed_unit": False}
    assert "stop" not in rec.verbs() and "disable" not in rec.verbs()


def test_uninstall_removes_a_dangling_enablement_without_a_unit_file(tmp_path):
    enabled_only = ACTIVE.replace("ActiveState=active", "ActiveState=inactive")
    rec = _Recorder(show=enabled_only)
    with patch.object(systemd_service.subprocess, "run", rec):
        result = systemd_service.uninstall(home=tmp_path)
    assert "disable" in rec.verbs()
    assert result["removed_unit"] is False


def test_uninstall_reports_a_stop_that_left_it_running(tmp_path):
    rec = _Recorder(show=ACTIVE, fail={"stop": 1})
    with patch.object(systemd_service.subprocess, "run", rec), pytest.raises(ClaudeSwitchError, match="stop"):
        systemd_service.uninstall(home=tmp_path)


def test_uninstall_tolerates_a_failed_disable_once_it_is_disabled(tmp_path):
    disabled = ACTIVE.replace("UnitFileState=enabled", "UnitFileState=disabled")
    rec = _Recorder(show=[ACTIVE, disabled], fail={"disable": 1})
    with patch.object(systemd_service.subprocess, "run", rec):
        systemd_service.uninstall(home=tmp_path)


# --- platform guards -------------------------------------------------------


def test_refuses_on_macos_and_points_at_the_menu_bar_service(tmp_path):
    with patch.object(systemd_service.sys, "platform", "darwin"), pytest.raises(
        ClaudeSwitchError, match="menubar --install-service"
    ):
        systemd_service.install(home=tmp_path, program=PROGRAM)


def test_refuses_on_windows(tmp_path):
    with patch.object(systemd_service.sys, "platform", "win32"), pytest.raises(
        ClaudeSwitchError, match="only available on Linux"
    ):
        systemd_service.status(home=tmp_path)


def test_refuses_without_systemctl(tmp_path):
    with patch.object(systemd_service.shutil, "which", lambda name: None), pytest.raises(
        ClaudeSwitchError, match="needs systemd"
    ):
        systemd_service.status(home=tmp_path)


# --- CLI -------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        ["--once"], ["--json"], ["--dry-run"], ["--debug"],
        ["--threshold", "85"], ["--strategy", "best"],
        ["--cooldown", "0"], ["--no-include-api-key-accounts"],
    ],
)
def test_cli_rejects_flags_the_service_would_silently_drop(extra):
    from claude_swap import cli

    with pytest.raises(SystemExit) as exc:
        cli._auto_command(["--install-service", *extra])
    assert exc.value.code == 2


def test_cli_service_flags_are_mutually_exclusive():
    from claude_swap import cli

    with pytest.raises(SystemExit) as exc:
        cli._auto_command(["--install-service", "--uninstall-service"])
    assert exc.value.code == 2


def test_cli_install_dispatches_and_reports(capsys, tmp_path):
    from claude_swap import cli

    fake = {
        "unit": "cswap-auto.service",
        "unit_path": str(tmp_path / "cswap-auto.service"),
        "program": [*PROGRAM, "auto"],
        "linger": False,
    }
    with patch.object(systemd_service, "install", return_value=fake), pytest.raises(SystemExit) as exc:
        cli._auto_command(["--install-service"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "installed and started" in out
    assert f"{PROGRAM[0]} auto" in out
    assert "enable-linger" in out


def test_cli_reports_a_service_error_with_exit_1(capsys):
    from claude_swap import cli

    with patch.object(
        systemd_service, "status", side_effect=ClaudeSwitchError("No systemd user manager is reachable")
    ), pytest.raises(SystemExit) as exc:
        cli._auto_command(["--service-status"])
    assert exc.value.code == 1
