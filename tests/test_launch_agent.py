"""Tests for the launchd LaunchAgent that keeps ``cswap menubar`` alive.

``subprocess.run`` is patched throughout so the real ``launchctl`` is never
driven: these assert the argv this module *shapes* and how it reads launchctl's
replies, not launchd's behaviour. Every test pins ``home`` to a tmp path and
passes an explicit ``uid``, so nothing here depends on the machine it runs on
or writes outside the temp directory.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import launch_agent
from claude_swap.exceptions import ClaudeSwitchError

PROGRAM = ["/Users/x/.local/bin/cswap"]
UID = 501


@pytest.fixture(autouse=True)
def _on_macos():
    """The module refuses off-darwin; these tests exercise the darwin path."""
    with patch.object(launch_agent.sys, "platform", "darwin"):
        yield


def _completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["launchctl"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _router(responses: dict[str, subprocess.CompletedProcess]):
    """Answer per launchctl subcommand, defaulting to success."""

    def run(argv, **kwargs):
        return responses.get(argv[1], _completed(0))

    return run


# --- plist shape -----------------------------------------------------------


def test_build_plist_is_parseable_and_runs_the_menubar_subcommand(tmp_path):
    parsed = plistlib.loads(launch_agent.build_plist(PROGRAM, home=tmp_path))
    assert parsed["Label"] == launch_agent.LABEL
    assert parsed["ProgramArguments"] == [*PROGRAM, "menubar"]
    assert parsed["RunAtLoad"] is True


def test_build_plist_keepalive_restarts_a_crash_but_respects_a_quit(tmp_path):
    """A bare `KeepAlive: True` would make the menu bar's Quit item a no-op.

    menubar.py's quit handler calls rumps.quit_application() — a clean exit(0)
    — so unconditional KeepAlive relaunches it immediately and the user has no
    way to stop the menu bar short of `launchctl bootout`.
    """
    parsed = plistlib.loads(launch_agent.build_plist(PROGRAM, home=tmp_path))
    assert parsed["KeepAlive"] == {"SuccessfulExit": False}


def test_build_plist_marks_the_agent_interactive_not_background(tmp_path):
    # Background would have launchd throttle a process that owns UI.
    parsed = plistlib.loads(launch_agent.build_plist(PROGRAM, home=tmp_path))
    assert parsed["ProcessType"] == "Interactive"


def test_build_plist_survives_paths_that_would_break_hand_written_xml(tmp_path):
    odd = tmp_path / "home & <co>"
    parsed = plistlib.loads(launch_agent.build_plist(PROGRAM, home=odd))
    assert parsed["StandardErrorPath"] == str(odd / "Library/Logs" / f"{launch_agent.LABEL}.err")


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="asserts POSIX path shapes; the agent only ever runs on macOS",
)
def test_build_plist_path_env_leads_with_the_programs_own_directory(tmp_path):
    parsed = plistlib.loads(launch_agent.build_plist(PROGRAM, home=tmp_path))
    assert parsed["EnvironmentVariables"]["PATH"].split(":")[0] == "/Users/x/.local/bin"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="asserts POSIX path shapes; the agent only ever runs on macOS",
)
def test_build_plist_path_env_includes_the_user_bin_dir(tmp_path):
    # A uv tool install puts cswap's siblings in ~/.local/bin; launchd's own
    # default PATH does not include it.
    parsed = plistlib.loads(launch_agent.build_plist(PROGRAM, home=tmp_path))
    entries = parsed["EnvironmentVariables"]["PATH"].split(":")
    assert os.path.expanduser("~/.local/bin") in entries


def test_build_plist_path_env_keeps_the_launchd_defaults(tmp_path):
    parsed = plistlib.loads(launch_agent.build_plist(PROGRAM, home=tmp_path))
    entries = parsed["EnvironmentVariables"]["PATH"].split(":")
    assert {"/usr/bin", "/bin", "/usr/sbin", "/sbin"} <= set(entries)


# --- program resolution ----------------------------------------------------


def test_resolve_program_prefers_the_console_script(tmp_path):
    script = tmp_path / "cswap"
    script.write_text("#!/bin/sh\n")
    with patch.object(launch_agent.sys, "argv", [str(script)]):
        assert launch_agent.resolve_program() == [str(script)]


def test_resolve_program_keeps_the_symlink_and_does_not_follow_it(tmp_path):
    """`uv tool install` links ~/.local/bin/cswap into the tool's virtualenv.

    Resolving that symlink would pin the virtualenv-internal path, which a
    reinstall recreates — exactly the path this module exists to avoid.
    """
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    real = venv_bin / "cswap"
    real.write_text("#!/bin/sh\n")
    link_dir = tmp_path / "local" / "bin"
    link_dir.mkdir(parents=True)
    link = link_dir / "cswap"
    link.symlink_to(real)

    with patch.object(launch_agent.sys, "argv", [str(link)]):
        assert launch_agent.resolve_program() == [str(link)]


def test_resolve_program_makes_a_relative_argv0_absolute(tmp_path, monkeypatch):
    script = tmp_path / "cswap"
    script.write_text("#!/bin/sh\n")
    monkeypatch.chdir(tmp_path)
    with patch.object(launch_agent.sys, "argv", ["./cswap"]):
        result = launch_agent.resolve_program()
    assert result == [str(script)]
    assert Path(result[0]).is_absolute()


def test_resolve_program_falls_back_to_the_interpreter_without_a_script(tmp_path):
    with patch.object(launch_agent.sys, "argv", [str(tmp_path / "gone")]):
        with patch.object(launch_agent.shutil, "which", return_value=None):
            assert launch_agent.resolve_program() == [sys.executable, "-m", "claude_swap"]


def test_resolve_program_ignores_an_argv0_that_is_not_cswap(tmp_path):
    # Running through pytest, argv[0] is the test runner — not a thing launchd
    # should be pointed at.
    other = tmp_path / "pytest"
    other.write_text("#!/bin/sh\n")
    found = tmp_path / "cswap"
    found.write_text("#!/bin/sh\n")
    with patch.object(launch_agent.sys, "argv", [str(other)]):
        with patch.object(launch_agent.shutil, "which", return_value=str(found)):
            assert launch_agent.resolve_program() == [str(found)]


# --- install ---------------------------------------------------------------


def test_install_writes_the_plist_and_bootstraps_it(tmp_path):
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(1)})
        result = launch_agent.install(home=tmp_path, program=PROGRAM, uid=UID)

    written = Path(result["plist"])
    assert written.exists()
    calls = [c.args[0] for c in run.call_args_list]
    assert ["launchctl", "bootstrap", f"gui/{UID}", str(written)] in calls


def test_install_creates_the_log_directory(tmp_path):
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(1)})
        result = launch_agent.install(home=tmp_path, program=PROGRAM, uid=UID)
    assert Path(result["stderr_log"]).parent.is_dir()


def test_install_boots_out_first_when_already_loaded(tmp_path):
    # Without this, launchd refuses a reinstall with "service already loaded".
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(0)})
        launch_agent.install(home=tmp_path, program=PROGRAM, uid=UID)

    subcommands = [c.args[0][1] for c in run.call_args_list]
    assert subcommands.index("bootout") < subcommands.index("bootstrap")


def test_install_does_not_boot_out_when_nothing_is_loaded(tmp_path):
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(1)})
        launch_agent.install(home=tmp_path, program=PROGRAM, uid=UID)

    assert "bootout" not in [c.args[0][1] for c in run.call_args_list]


def test_install_waits_for_the_old_job_to_go_away_before_bootstrapping(tmp_path):
    """`bootout` can return before launchd has finished the teardown.

    A `bootstrap` inside that window fails with "Operation already in
    progress", so install polls until the job is actually gone.
    """
    prints = {"n": 0}

    def run(argv, **kwargs):
        if argv[1] == "print":
            prints["n"] += 1
            return _completed(0 if prints["n"] <= 3 else 1)
        return _completed(0)

    with patch.object(launch_agent.time, "sleep") as slept:
        with patch.object(launch_agent.subprocess, "run", side_effect=run) as ran:
            launch_agent.install(home=tmp_path, program=PROGRAM, uid=UID)

    assert slept.called, "did not wait for the old job at all"
    subcommands = [c.args[0][1] for c in ran.call_args_list]
    # every liveness check sits between the bootout and the bootstrap
    assert subcommands.index("bootout") < subcommands.index("bootstrap")
    assert subcommands.count("print") > 2


def test_wait_until_unloaded_gives_up_after_the_timeout(tmp_path):
    with patch.object(launch_agent.time, "sleep"):
        with patch.object(launch_agent.subprocess, "run") as run:
            run.side_effect = _router({"print": _completed(0)})
            assert launch_agent._wait_until_unloaded(uid=UID, timeout=0.0) is False


def test_install_names_the_lingering_predecessor_when_bootstrap_fails(tmp_path):
    with patch.object(launch_agent, "_wait_until_unloaded", return_value=False):
        with patch.object(launch_agent.subprocess, "run") as run:
            run.side_effect = _router(
                {"print": _completed(0), "bootstrap": _completed(5, stderr="Operation already in progress")}
            )
            with pytest.raises(ClaudeSwitchError, match="still shutting down"):
                launch_agent.install(home=tmp_path, program=PROGRAM, uid=UID)


def test_install_raises_with_launchctl_detail_when_bootstrap_fails(tmp_path):
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router(
            {"print": _completed(1), "bootstrap": _completed(5, stderr="Input/output error")}
        )
        with pytest.raises(ClaudeSwitchError, match="Input/output error"):
            launch_agent.install(home=tmp_path, program=PROGRAM, uid=UID)


def test_install_refuses_off_macos(tmp_path):
    with patch.object(launch_agent.sys, "platform", "linux"):
        with pytest.raises(ClaudeSwitchError, match="only available on macOS"):
            launch_agent.install(home=tmp_path, program=PROGRAM, uid=UID)


# --- uninstall -------------------------------------------------------------


def test_uninstall_boots_out_and_removes_the_plist(tmp_path):
    target = launch_agent.plist_path(home=tmp_path)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x")

    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(0)})
        result = launch_agent.uninstall(home=tmp_path, uid=UID)

    assert result == {"label": launch_agent.LABEL, "was_loaded": True, "removed_plist": True}
    assert not target.exists()
    assert ["launchctl", "bootout", f"gui/{UID}/{launch_agent.LABEL}"] in [
        c.args[0] for c in run.call_args_list
    ]


def test_uninstall_is_quiet_when_nothing_is_installed(tmp_path):
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(1)})
        result = launch_agent.uninstall(home=tmp_path, uid=UID)
    assert result["was_loaded"] is False and result["removed_plist"] is False


def test_uninstall_removes_a_plist_that_was_never_bootstrapped(tmp_path):
    target = launch_agent.plist_path(home=tmp_path)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x")
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(1)})
        result = launch_agent.uninstall(home=tmp_path, uid=UID)
    assert result["removed_plist"] is True and not target.exists()


def test_uninstall_raises_when_bootout_fails_and_the_service_stays_loaded(tmp_path):
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(0), "bootout": _completed(3, stderr="in use")})
        with pytest.raises(ClaudeSwitchError, match="in use"):
            launch_agent.uninstall(home=tmp_path, uid=UID)


def test_uninstall_tolerates_a_bootout_race_that_already_unloaded_it(tmp_path):
    # bootout returns non-zero because the job went away underneath it; the
    # follow-up print shows it gone, which is the outcome uninstall wants.
    calls = {"print": 0}

    def run(argv, **kwargs):
        if argv[1] == "print":
            calls["print"] += 1
            return _completed(0) if calls["print"] == 1 else _completed(1)
        if argv[1] == "bootout":
            return _completed(3, stderr="No such process")
        return _completed(0)

    with patch.object(launch_agent.subprocess, "run", side_effect=run):
        result = launch_agent.uninstall(home=tmp_path, uid=UID)
    assert result["was_loaded"] is True


# --- status ----------------------------------------------------------------


def test_status_reads_state_and_pid_from_launchctl_print(tmp_path):
    printed = "\tstate = running\n\tpid = 25026\n\tlast exit code = (never exited)\n"
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(0, stdout=printed)})
        result = launch_agent.status(home=tmp_path, uid=UID)

    assert result["loaded"] is True
    assert result["state"] == "running"
    assert result["pid"] == 25026


def test_status_reports_not_loaded_without_inventing_a_pid(tmp_path):
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(1)})
        result = launch_agent.status(home=tmp_path, uid=UID)
    assert result["loaded"] is False
    assert result["pid"] is None and result["state"] is None


def test_status_separates_installed_from_loaded(tmp_path):
    # A plist on disk that launchd has not been given is a real state, and the
    # difference is what tells a user to run --install-service again.
    target = launch_agent.plist_path(home=tmp_path)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x")
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(1)})
        result = launch_agent.status(home=tmp_path, uid=UID)
    assert result["installed"] is True and result["loaded"] is False


def test_status_reads_the_jobs_own_state_not_a_nested_blocks(tmp_path):
    """Regression: real `launchctl print` repeats `state` inside sub-dicts.

    Observed on macOS 25.5.0 — the job prints `state = running`, then a
    `pid-local endpoints` block prints `state = active`. Taking the last match
    reported the endpoint's state as the service's.
    """
    printed = (
        "gui/501/com.cswap.menubar = {\n"
        "\tactive count = 1\n"
        "\tstate = running\n"
        "\tpid = 25026\n"
        "\tpid-local endpoints = {\n"
        "\t\tstate = active\n"
        "\t\tpid = 999\n"
        "\t}\n"
        "}\n"
    )
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(0, stdout=printed)})
        result = launch_agent.status(home=tmp_path, uid=UID)

    assert result["state"] == "running"
    assert result["pid"] == 25026


def test_status_keeps_multi_word_launchd_states(tmp_path):
    # Before the process spawns, launchd reports "spawn scheduled".
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(0, stdout="\tstate = spawn scheduled\n")})
        result = launch_agent.status(home=tmp_path, uid=UID)
    assert result["state"] == "spawn scheduled"


def test_status_ignores_a_non_numeric_pid_line(tmp_path):
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(0, stdout="\tpid = (none)\n")})
        result = launch_agent.status(home=tmp_path, uid=UID)
    assert result["pid"] is None
