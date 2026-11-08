"""Tests for the launchd LaunchAgent that keeps ``cswap menubar`` alive.

``subprocess.run`` is patched throughout so the real ``launchctl`` is never
driven: these assert the argv this module *shapes* and how it reads launchctl's
replies, not launchd's behaviour. Every test pins ``home`` to a tmp path and
passes an explicit ``uid``, so nothing here depends on the machine it runs on
or writes outside the temp directory.
"""

from __future__ import annotations

import json
import os
import plistlib
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import app_bundle, launch_agent
from claude_swap.exceptions import ClaudeSwitchError

PROGRAM = ["/Users/x/.local/bin/cswap"]
UID = 501


@pytest.fixture(autouse=True)
def _on_macos():
    """The module refuses off-darwin; these tests exercise the darwin path."""
    with patch.object(launch_agent.sys, "platform", "darwin"):
        yield


@pytest.fixture(autouse=True)
def applications_dir(tmp_path, monkeypatch):
    """Fence every uninstall here off the real /Applications.

    uninstall() removes the wrapper bundle, and on a developer's Mac the real
    one may be the very app drawing the menu bar while the suite runs.
    """
    apps = tmp_path / "Applications"
    apps.mkdir()
    monkeypatch.setattr(app_bundle, "APPLICATIONS_DIR", apps)
    return apps


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


def test_build_plist_is_parseable_and_runs_the_menubar_worker(tmp_path):
    # Plain `cswap menubar` asks launchd to run the service; --foreground is the
    # one invocation that draws the status item in place, so that is the job.
    parsed = plistlib.loads(launch_agent.build_plist(PROGRAM, home=tmp_path))
    assert parsed["Label"] == launch_agent.LABEL
    assert parsed["ProgramArguments"] == [*PROGRAM, "menubar", "--foreground"]
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


def test_build_plist_forwards_only_the_proxy_and_cert_allowlist(tmp_path):
    # launchd does not inherit the installing shell's environment, so a proxy
    # set there never reaches the agent. Copy a fixed allowlist, not everything.
    env = {
        "HTTPS_PROXY": "http://proxy.example:3128",
        "https_proxy": "http://proxy.example:3128",
        "NO_PROXY": "",
        "SSL_CERT_FILE": "/etc/ssl/corp.pem",
        "ALL_PROXY": "socks5://proxy.example:1080",
        "AWS_SECRET_ACCESS_KEY": "hunter2",
    }
    parsed = plistlib.loads(launch_agent.build_plist(PROGRAM, home=tmp_path, env=env))
    forwarded = dict(parsed["EnvironmentVariables"])
    forwarded.pop("PATH")
    assert forwarded == {
        "HTTPS_PROXY": "http://proxy.example:3128",
        "https_proxy": "http://proxy.example:3128",
        "NO_PROXY": "",
        "SSL_CERT_FILE": "/etc/ssl/corp.pem",
    }


def test_build_plist_keeps_an_empty_value_that_cancels_its_uppercase_twin(tmp_path):
    # urllib reads an empty lowercase name as switching the uppercase one off,
    # so `https_proxy=` next to HTTPS_PROXY means "no proxy" in the shell.
    # Dropping the empty value would hand the agent the proxy the shell turned
    # off; launchd sets an empty string as an empty variable, so the agent's
    # urllib then sees exactly what the shell's did.
    env = {"HTTPS_PROXY": "http://proxy.example:3128", "https_proxy": ""}
    parsed = plistlib.loads(launch_agent.build_plist(PROGRAM, home=tmp_path, env=env))
    assert parsed["EnvironmentVariables"]["https_proxy"] == ""


def test_build_plist_env_without_allowlisted_keys_is_just_path(tmp_path):
    parsed = plistlib.loads(
        launch_agent.build_plist(PROGRAM, home=tmp_path, env={"HOME": "/Users/x"})
    )
    assert list(parsed["EnvironmentVariables"]) == ["PATH"]


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
        result = launch_agent.install(home=tmp_path, program=PROGRAM, uid=UID, backup_dir=tmp_path)

    written = Path(result["plist"])
    assert written.exists()
    calls = [c.args[0] for c in run.call_args_list]
    assert ["launchctl", "bootstrap", f"gui/{UID}", str(written)] in calls


def test_install_creates_the_log_directory(tmp_path):
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(1)})
        result = launch_agent.install(home=tmp_path, program=PROGRAM, uid=UID, backup_dir=tmp_path)
    assert Path(result["stderr_log"]).parent.is_dir()


@pytest.mark.skipif(sys.platform == "win32", reason="asserts a POSIX file mode")
def test_install_writes_the_plist_owner_only(tmp_path):
    # A forwarded proxy URL can carry credentials, so the plist gets 0600
    # rather than whatever the umask leaves write_bytes with.
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(1)})
        result = launch_agent.install(home=tmp_path, program=PROGRAM, uid=UID, backup_dir=tmp_path)
    assert stat.S_IMODE(Path(result["plist"]).stat().st_mode) == 0o600


def test_install_boots_out_first_when_already_loaded(tmp_path):
    # Without this, launchd refuses a reinstall with "service already loaded".
    # The fake tracks load state like real launchd: loaded until a bootout
    # actually clears it, so install's unload wait returns on the first poll
    # instead of waiting out the real timeout.
    state = {"loaded": True}

    def run(argv, **kwargs):
        if argv[1] == "print":
            return _completed(0 if state["loaded"] else 1)
        if argv[1] == "bootout":
            state["loaded"] = False
        return _completed(0)

    with patch.object(launch_agent.subprocess, "run", side_effect=run) as ran:
        launch_agent.install(home=tmp_path, program=PROGRAM, uid=UID, backup_dir=tmp_path)

    subcommands = [c.args[0][1] for c in ran.call_args_list]
    assert subcommands.index("bootout") < subcommands.index("bootstrap")


def test_install_does_not_boot_out_when_nothing_is_loaded(tmp_path):
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(1)})
        launch_agent.install(home=tmp_path, program=PROGRAM, uid=UID, backup_dir=tmp_path)

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
            launch_agent.install(home=tmp_path, program=PROGRAM, uid=UID, backup_dir=tmp_path)

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
                launch_agent.install(home=tmp_path, program=PROGRAM, uid=UID, backup_dir=tmp_path)


def test_install_raises_with_launchctl_detail_when_bootstrap_fails(tmp_path):
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router(
            {"print": _completed(1), "bootstrap": _completed(5, stderr="Input/output error")}
        )
        with pytest.raises(ClaudeSwitchError, match="Input/output error"):
            launch_agent.install(home=tmp_path, program=PROGRAM, uid=UID, backup_dir=tmp_path)


def test_install_refuses_off_macos(tmp_path):
    with patch.object(launch_agent.sys, "platform", "linux"):
        with pytest.raises(ClaudeSwitchError, match="only available on macOS"):
            launch_agent.install(home=tmp_path, program=PROGRAM, uid=UID, backup_dir=tmp_path)


# --- uninstall -------------------------------------------------------------


def test_uninstall_boots_out_and_removes_the_plist(tmp_path):
    target = launch_agent.plist_path(home=tmp_path)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x")

    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(0)})
        result = launch_agent.uninstall(home=tmp_path, uid=UID, backup_dir=tmp_path)

    assert result["was_loaded"] is True
    assert result["removed_plist"] is True
    assert result["removed"] == [str(target)]
    assert not target.exists()
    assert ["launchctl", "bootout", f"gui/{UID}/{launch_agent.LABEL}"] in [
        c.args[0] for c in run.call_args_list
    ]


def test_uninstall_is_quiet_when_nothing_is_installed(tmp_path):
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(1)})
        result = launch_agent.uninstall(home=tmp_path, uid=UID, backup_dir=tmp_path)
    assert result["was_loaded"] is False and result["removed_plist"] is False


def test_uninstall_removes_a_plist_that_was_never_bootstrapped(tmp_path):
    target = launch_agent.plist_path(home=tmp_path)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x")
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(1)})
        result = launch_agent.uninstall(home=tmp_path, uid=UID, backup_dir=tmp_path)
    assert result["removed_plist"] is True and not target.exists()


def test_uninstall_raises_when_bootout_fails_and_the_service_stays_loaded(tmp_path):
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(0), "bootout": _completed(3, stderr="in use")})
        with pytest.raises(ClaudeSwitchError, match="in use"):
            launch_agent.uninstall(home=tmp_path, uid=UID, backup_dir=tmp_path)


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
        result = launch_agent.uninstall(home=tmp_path, uid=UID, backup_dir=tmp_path)
    assert result["was_loaded"] is True


# --- status ----------------------------------------------------------------


def test_status_reads_state_and_pid_from_launchctl_print(tmp_path):
    printed = "\tstate = running\n\tpid = 25026\n\tlast exit code = (never exited)\n"
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(0, stdout=printed)})
        result = launch_agent.status(home=tmp_path, uid=UID, backup_dir=tmp_path)

    assert result["loaded"] is True
    assert result["state"] == "running"
    assert result["pid"] == 25026


def test_status_reports_not_loaded_without_inventing_a_pid(tmp_path):
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(1)})
        result = launch_agent.status(home=tmp_path, uid=UID, backup_dir=tmp_path)
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
        result = launch_agent.status(home=tmp_path, uid=UID, backup_dir=tmp_path)
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
        result = launch_agent.status(home=tmp_path, uid=UID, backup_dir=tmp_path)

    assert result["state"] == "running"
    assert result["pid"] == 25026


def test_status_keeps_multi_word_launchd_states(tmp_path):
    # Before the process spawns, launchd reports "spawn scheduled".
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(0, stdout="\tstate = spawn scheduled\n")})
        result = launch_agent.status(home=tmp_path, uid=UID, backup_dir=tmp_path)
    assert result["state"] == "spawn scheduled"


def test_status_ignores_a_non_numeric_pid_line(tmp_path):
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(0, stdout="\tpid = (none)\n")})
        result = launch_agent.status(home=tmp_path, uid=UID, backup_dir=tmp_path)
    assert result["pid"] is None


# --- ensure: plain `cswap menubar` -----------------------------------------


class _FakeLaunchd:
    """launchctl as far as this module drives it.

    Tracks loaded/pid state through bootstrap, bootout and kickstart, answers
    ``print`` the way launchd does, and lists the service in ``print-disabled``
    when asked to. Records every argv so tests can assert the sequence.
    """

    def __init__(
        self, *, loaded=False, pid=None, switched_off=False, launchd_disabled=False,
        etime="00:10",
    ):
        self.loaded, self.pid = loaded, pid
        self.switched_off = switched_off  # the System Settings switch (BTM)
        self.launchd_disabled = launchd_disabled  # launchd's own override list
        self.etime = etime  # what `ps -o etime=` says about the running job
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        sub = argv[1]
        if argv[0] == "ps":
            return _completed(0, stdout=f"{self.etime}\n")
        if argv[0] == "sfltool":
            disposition = "disallowed" if self.switched_off else "allowed"
            return _completed(0, stdout=_btm_dump(disposition))
        if sub == "print":
            if not self.loaded:
                return _completed(1)
            lines = [f"\tstate = {'running' if self.pid else 'not running'}"]
            if self.pid:
                lines.append(f"\tpid = {self.pid}")
            return _completed(0, stdout="\n".join(lines) + "\n")
        if sub == "print-disabled":
            flag = "disabled" if self.launchd_disabled else "enabled"
            listing = f'\tdisabled services = {{\n\t\t"{launch_agent.LABEL}" => {flag}\n\t}}\n'
            return _completed(0, stdout=listing)
        if sub == "bootstrap":
            self.loaded, self.pid = True, 7001
        elif sub == "bootout":
            self.loaded, self.pid = False, None
        elif sub == "kickstart":
            self.pid = 7002
        return _completed(0)

    @property
    def subcommands(self):
        return [c[1] for c in self.calls]


def _btm_dump(disposition: str, label: str = launch_agent.LABEL) -> str:
    """Two records in `sfltool dumpbtm`'s shape; ours carries ``disposition``."""
    return (
        " #1:\n"
        "                 Name: Docker\n"
        "                 Type: legacy daemon (0x10010)\n"
        "          Disposition: [enabled, allowed, notified] (0xb)\n"
        "           Identifier: 16.com.docker.socket\n"
        "                  URL: /Library/LaunchDaemons/com.docker.socket.plist\n"
        "\n"
        " #2:\n"
        "                 Name: cswap\n"
        "       Developer Name: (null)\n"
        "                 Type: legacy agent (0x10008)\n"
        f"          Disposition: [enabled, {disposition}, notified] (0x9)\n"
        f"           Identifier: 8.{label}\n"
        f"                  URL: /Users/501/Library/LaunchAgents/{label}.plist\n"
        "    Parent Identifier: Unknown Developer\n"
    )


def _homes(tmp_path):
    home, data = tmp_path / "home", tmp_path / "data"
    home.mkdir()
    data.mkdir()
    return home, data


def _login_plist(home, program=PROGRAM, **env):
    path = launch_agent.plist_path(home=home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(launch_agent.build_plist(program, home=home, env=env))
    return path


def _session_plist(home, data):
    path = launch_agent.session_plist_path(backup_dir=data)
    path.write_bytes(launch_agent.build_plist(PROGRAM, home=home))
    return path


def _ensure(fake, home, data, **kw):
    with patch.object(launch_agent.subprocess, "run", side_effect=fake):
        return launch_agent.ensure(
            home=home, backup_dir=data, program=PROGRAM, uid=UID, **kw
        )


WORKER = [*PROGRAM, "menubar", "--foreground"]


def test_ensure_first_setup_registers_at_login_and_bootstraps(tmp_path):
    home, data = _homes(tmp_path)
    fake = _FakeLaunchd()

    result = _ensure(fake, home, data)

    assert result["action"] == "first-setup"
    assert result["start_at_login"] is True
    assert result["pid"] == 7001
    login = launch_agent.plist_path(home=home)
    assert login.exists()
    assert not launch_agent.session_plist_path(backup_dir=data).exists()
    assert ["launchctl", "bootstrap", f"gui/{UID}", str(login)] in fake.calls
    assert "bootout" not in fake.subcommands
    assert plistlib.loads(login.read_bytes())["ProgramArguments"] == WORKER


def test_ensure_leaves_a_running_service_alone(tmp_path):
    home, data = _homes(tmp_path)
    _login_plist(home)
    fake = _FakeLaunchd(loaded=True, pid=4242)

    result = _ensure(fake, home, data)

    assert result["action"] == "already-running"
    assert result["pid"] == 4242
    assert not {"bootstrap", "bootout", "kickstart"} & set(fake.subcommands)
    # The ~2 s System Settings read is skipped when nothing is about to start.
    assert "dumpbtm" not in fake.subcommands
    assert result["switched_off"] is False


def test_ensure_kickstarts_a_quit_service(tmp_path):
    # After Quit (a clean exit 0) launchd keeps the job loaded but idle until
    # the next login; kickstart runs it again without re-reading anything.
    home, data = _homes(tmp_path)
    _login_plist(home)
    fake = _FakeLaunchd(loaded=True, pid=None)

    result = _ensure(fake, home, data)

    assert result["action"] == "started"
    assert ["launchctl", "kickstart", f"gui/{UID}/{launch_agent.LABEL}"] in fake.calls
    assert "bootstrap" not in fake.subcommands


def test_ensure_restart_kickstarts_a_running_service_with_kill(tmp_path):
    home, data = _homes(tmp_path)
    _login_plist(home)
    fake = _FakeLaunchd(loaded=True, pid=4242)

    result = _ensure(fake, home, data, restart=True)

    assert result["action"] == "restarted"
    assert ["launchctl", "kickstart", "-k", f"gui/{UID}/{launch_agent.LABEL}"] in fake.calls


def test_ensure_respects_start_at_login_turned_off(tmp_path):
    """A plist in the data dir is the user's explicit "off".

    Bootstrap from there for this session, and never move it back: a missing
    plist would mean "never set up" and re-register the login start.
    """
    home, data = _homes(tmp_path)
    session = _session_plist(home, data)
    fake = _FakeLaunchd()

    result = _ensure(fake, home, data)

    assert result["action"] == "started"
    assert result["start_at_login"] is False
    assert not launch_agent.plist_path(home=home).exists()
    assert session.exists()
    assert ["launchctl", "bootstrap", f"gui/{UID}", str(session)] in fake.calls


def test_ensure_repairs_a_plist_from_before_the_worker_flag_and_keeps_its_env(tmp_path):
    home, data = _homes(tmp_path)
    login = _login_plist(home, https_proxy="http://proxy:3128")
    old = plistlib.loads(login.read_bytes())
    old["ProgramArguments"] = [*PROGRAM, "menubar"]  # written by a pre-1.0 cswap
    old["EnvironmentVariables"]["PATH"] = "/stale"
    login.write_bytes(plistlib.dumps(old))
    fake = _FakeLaunchd(loaded=True, pid=4242)

    result = _ensure(fake, home, data)

    assert result["action"] == "restarted"
    repaired = plistlib.loads(login.read_bytes())
    assert repaired["ProgramArguments"] == WORKER
    # The installing shell's environment survives; this shell may lack it.
    assert repaired["EnvironmentVariables"]["https_proxy"] == "http://proxy:3128"
    assert repaired["EnvironmentVariables"]["PATH"] != "/stale"
    subs = fake.subcommands
    # launchd holds the old definition in memory: only a reload applies the
    # new plist, kickstart would rerun the old one.
    assert subs.index("bootout") < subs.index("bootstrap")
    assert "kickstart" not in subs


def test_ensure_repairs_a_plist_pinning_a_moved_console_script(tmp_path):
    home, data = _homes(tmp_path)
    login = _login_plist(home, program=["/old/venv/bin/cswap"])
    fake = _FakeLaunchd(loaded=True, pid=None)

    result = _ensure(fake, home, data)

    assert result["action"] == "started"
    assert plistlib.loads(login.read_bytes())["ProgramArguments"] == WORKER
    assert "bootstrap" in fake.subcommands


def test_ensure_prefers_the_login_plist_when_both_exist(tmp_path):
    home, data = _homes(tmp_path)
    _login_plist(home)
    session = _session_plist(home, data)

    result = _ensure(_FakeLaunchd(), home, data)

    assert result["start_at_login"] is True
    assert not session.exists()


def test_ensure_starts_a_switched_off_service_and_reports_the_switch(tmp_path):
    """macOS reserves the Login Items switch for the start at login.

    A manual start still goes through (verified on macOS 27: the job ran on
    after the switch was off), so refusing would be stricter than the OS.
    The caller is told instead.
    """
    home, data = _homes(tmp_path)
    fake = _FakeLaunchd(switched_off=True)

    result = _ensure(fake, home, data)

    assert result["action"] == "first-setup"
    assert result["switched_off"] is True
    assert "bootstrap" in fake.subcommands
    assert "dumpbtm" in fake.subcommands


def test_ensure_reports_launchds_own_disable_the_same_way(tmp_path):
    home, data = _homes(tmp_path)
    fake = _FakeLaunchd(launchd_disabled=True)
    result = _ensure(fake, home, data)
    assert result["switched_off"] is True


def test_ensure_refuses_off_macos(tmp_path):
    with patch.object(launch_agent.sys, "platform", "linux"):
        with pytest.raises(ClaudeSwitchError):
            launch_agent.ensure(home=tmp_path, backup_dir=tmp_path, program=PROGRAM, uid=UID)


# --- is_disabled / the job's pid -------------------------------------------


@pytest.mark.parametrize("disposition, expected", [("disallowed", True), ("allowed", False)])
def test_btm_record_disallowed_reads_our_items_disposition(disposition, expected):
    assert launch_agent._btm_record_disallowed(_btm_dump(disposition), launch_agent.LABEL) is expected


def test_btm_record_disallowed_ignores_other_items_and_a_missing_record():
    dump = _btm_dump("disallowed", label="com.other.agent")
    assert launch_agent._btm_record_disallowed(dump, launch_agent.LABEL) is False
    assert launch_agent._btm_record_disallowed("", launch_agent.LABEL) is False


def test_btm_disallowed_is_false_when_the_dump_cannot_be_read():
    with patch.object(launch_agent.subprocess, "run") as run:
        run.return_value = _completed(1, stderr="Must be run as root")
        assert launch_agent._btm_disallowed() is False
    with patch.object(launch_agent.subprocess, "run", side_effect=FileNotFoundError):
        assert launch_agent._btm_disallowed() is False


@pytest.mark.parametrize(
    "value, disabled",
    [("disabled", True), ("true", True), ("enabled", False), ("false", False)],
)
def test_launchd_disabled_reads_both_launchctl_spellings(value, disabled):
    listing = (
        "\tdisabled services = {\n"
        f'\t\t"{launch_agent.LABEL}" => {value}\n'
        '\t\t"com.other.agent" => disabled\n'
        "\t}\n"
    )
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print-disabled": _completed(0, stdout=listing)})
        assert launch_agent._launchd_disabled(uid=UID) is disabled


def test_launchd_disabled_is_false_when_the_list_cannot_be_read():
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print-disabled": _completed(1, stderr="nope")})
        assert launch_agent._launchd_disabled(uid=UID) is False


def test_running_as_service_matches_the_jobs_pid_to_this_process():
    mine = f"\tstate = running\n\tpid = {os.getpid()}\n"
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(0, stdout=mine)})
        assert launch_agent.running_as_service(uid=UID) is True
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(0, stdout="\tstate = running\n\tpid = 1\n")})
        assert launch_agent.running_as_service(uid=UID) is False
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(1)})
        assert launch_agent.service_pid(uid=UID) is None


# --- Start at login toggle ---------------------------------------------------


def _never_called(argv, **kwargs):
    raise AssertionError(f"launchctl must not be driven here, got {argv}")


def test_set_start_at_login_off_moves_the_plist_without_touching_launchd(tmp_path):
    # The menu calls this from inside the running job; a bootout would kill it.
    home, data = _homes(tmp_path)
    login = _login_plist(home, https_proxy="http://proxy:3128")
    original = login.read_bytes()

    with patch.object(launch_agent.subprocess, "run", side_effect=_never_called):
        result = launch_agent.set_start_at_login(False, home=home, backup_dir=data)

    session = launch_agent.session_plist_path(backup_dir=data)
    assert result["start_at_login"] is False
    assert session.read_bytes() == original  # the forwarded env survives the move
    assert not login.exists()


def test_set_start_at_login_on_moves_it_back(tmp_path):
    home, data = _homes(tmp_path)
    session = _session_plist(home, data)

    with patch.object(launch_agent.subprocess, "run", side_effect=_never_called):
        launch_agent.set_start_at_login(True, home=home, backup_dir=data)

    assert launch_agent.start_at_login_enabled(home=home) is True
    assert not session.exists()


@pytest.mark.parametrize("enabled", [True, False])
def test_set_start_at_login_with_no_plist_writes_one_so_the_choice_is_kept(tmp_path, enabled):
    # A --foreground run from a shell has no plist anywhere yet.
    home, data = _homes(tmp_path)

    with patch.object(launch_agent.subprocess, "run", side_effect=_never_called):
        result = launch_agent.set_start_at_login(
            enabled, home=home, backup_dir=data, program=PROGRAM
        )

    written = Path(result["plist"])
    assert written.exists()
    assert launch_agent.start_at_login_enabled(home=home) is enabled
    assert plistlib.loads(written.read_bytes())["ProgramArguments"] == WORKER


def test_uninstall_removes_the_session_plist_too(tmp_path):
    home, data = _homes(tmp_path)
    session = _session_plist(home, data)
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(1)})
        result = launch_agent.uninstall(home=home, uid=UID, backup_dir=data)
    assert result["removed_plist"] is True
    assert not session.exists()


def test_install_removes_a_session_plist_so_login_wins(tmp_path):
    home, data = _homes(tmp_path)
    session = _session_plist(home, data)
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(1)})
        result = launch_agent.install(home=home, program=PROGRAM, uid=UID, backup_dir=data)
    assert result["start_at_login"] is True
    assert not session.exists()
    assert launch_agent.start_at_login_enabled(home=home)


def test_status_reports_start_at_login_and_the_system_settings_switch(tmp_path):
    home, data = _homes(tmp_path)
    session = _session_plist(home, data)
    fake = _FakeLaunchd(loaded=True, pid=4242, switched_off=True)
    with patch.object(launch_agent.subprocess, "run", side_effect=fake):
        result = launch_agent.status(home=home, uid=UID, backup_dir=data)
    assert result["start_at_login"] is False
    assert result["configured"] is True
    assert result["session_plist"] == str(session)
    assert result["switched_off"] is True
    assert result["pid"] == 4242


def test_uninstall_removes_the_bundle_the_logs_and_launchds_override(
    tmp_path, applications_dir
):
    """Nothing of the menu bar may outlive --uninstall-service.

    The "Allow in the Background" switch leaves a disabled override in
    launchd's own database; the wrapper .app and the two log files live
    outside the data directory. All of it goes, and the result names it.
    """
    home, data = _homes(tmp_path)
    plist = _login_plist(home)
    bundle = applications_dir / app_bundle.BUNDLE_DIRNAME
    (bundle / "Contents" / "MacOS").mkdir(parents=True)
    out_log, err_log = launch_agent.log_paths(home=home)
    out_log.parent.mkdir(parents=True)
    out_log.write_text("x")
    err_log.write_text("y")
    fake = _FakeLaunchd(loaded=False, launchd_disabled=True)

    with patch.object(launch_agent.subprocess, "run", side_effect=fake):
        result = launch_agent.uninstall(home=home, uid=UID, backup_dir=data)

    assert ["launchctl", "enable", f"gui/{UID}/{launch_agent.LABEL}"] in fake.calls
    assert result["cleared_override"] is True
    assert result["removed_plist"] is True
    assert result["bundle_error"] is None
    for gone in (plist, bundle, out_log, err_log):
        assert not gone.exists(), gone
        assert str(gone) in result["removed"]


def test_uninstall_leaves_launchds_override_alone_when_not_disabled(tmp_path):
    home, data = _homes(tmp_path)
    fake = _FakeLaunchd(loaded=False)
    with patch.object(launch_agent.subprocess, "run", side_effect=fake):
        result = launch_agent.uninstall(home=home, uid=UID, backup_dir=data)
    assert "enable" not in fake.subcommands
    assert result["cleared_override"] is False
    assert result["removed"] == []


def test_uninstall_reports_a_bundle_it_could_not_remove(tmp_path, monkeypatch):
    home, data = _homes(tmp_path)
    monkeypatch.setattr(app_bundle, "remove", lambda *a, **k: (_ for _ in ()).throw(OSError("busy")))
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(1)})
        result = launch_agent.uninstall(home=home, uid=UID, backup_dir=data)
    assert result["bundle_error"] == "busy"


# --- the installing shell's profile travels with the service ------------------


def test_build_plist_forwards_the_profile_of_the_installing_shell(tmp_path):
    env = {
        "CLAUDE_CONFIG_DIR": "/Users/x/.claude-work",
        "CLAUDE_SECURESTORAGE_CONFIG_DIR": "/Users/x/.claude-work",
        "XDG_DATA_HOME": "/Users/x/.local/share",
        "UNRELATED": "no",
    }
    parsed = plistlib.loads(launch_agent.build_plist(PROGRAM, home=tmp_path, env=env))
    forwarded = parsed["EnvironmentVariables"]
    assert forwarded["CLAUDE_CONFIG_DIR"] == "/Users/x/.claude-work"
    assert forwarded["CLAUDE_SECURESTORAGE_CONFIG_DIR"] == "/Users/x/.claude-work"
    assert forwarded["XDG_DATA_HOME"] == "/Users/x/.local/share"
    assert "UNRELATED" not in forwarded


def test_install_reports_a_profile_that_moved(tmp_path, monkeypatch):
    # Reinstalling from a shell in another profile redirects the service;
    # that must be said, not done silently.
    home, data = _homes(tmp_path)
    _login_plist(home, CLAUDE_CONFIG_DIR="/old/profile")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/new/profile")
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(1)})
        result = launch_agent.install(home=home, program=PROGRAM, uid=UID, backup_dir=data)
    assert result["profile_changes"] == [("CLAUDE_CONFIG_DIR", "/old/profile", "/new/profile")]
    written = plistlib.loads(launch_agent.plist_path(home=home).read_bytes())
    assert written["EnvironmentVariables"]["CLAUDE_CONFIG_DIR"] == "/new/profile"


def test_a_first_install_has_no_profile_to_compare_against(tmp_path, monkeypatch):
    home, data = _homes(tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/new/profile")
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(1)})
        result = launch_agent.install(home=home, program=PROGRAM, uid=UID, backup_dir=data)
    assert result["profile_changes"] == []


# --- the worker's build stamp ------------------------------------------------


def _stamp(data, pid, version="0.0.0", source=None):
    path = launch_agent.worker_stamp_path(data)
    path.write_text(
        json.dumps({"pid": pid, "started_at": 1.0, "version": version, "source": source})
    )
    return path


def test_write_worker_stamp_records_this_process_and_build(tmp_path):
    from claude_swap.update_check import install_identity

    _home, data = _homes(tmp_path)
    launch_agent.write_worker_stamp(data)
    stamp = launch_agent.read_worker_stamp(data)
    assert stamp["pid"] == os.getpid()
    assert stamp["version"] == install_identity()["version"]
    assert launch_agent.worker_build_is_stale(os.getpid(), data) is False


def test_worker_build_is_stale_for_the_running_pids_own_older_stamp(tmp_path, monkeypatch):
    _home, data = _homes(tmp_path)
    _stamp(data, pid=4242, version="0.0.0")
    monkeypatch.setattr(
        "claude_swap.update_check.install_identity",
        lambda: {"version": "1.0.0", "source": None},
    )
    with patch.object(launch_agent.subprocess, "run", side_effect=_FakeLaunchd()):
        assert launch_agent.worker_build_is_stale(4242, data) is True


def test_an_unstamped_worker_is_restarted_once_it_is_past_the_grace_period(tmp_path):
    """Workers from before stamps existed (0.30, 1.0.0b1) never write one.

    Without this rule an external upgrade would leave them running the old
    build for ever: plain `cswap menubar` would read "no stamp" as "current".
    """
    _home, data = _homes(tmp_path)
    old = _FakeLaunchd(etime="05:00")
    with patch.object(launch_agent.subprocess, "run", side_effect=old):
        assert launch_agent.worker_build_is_stale(4242, data) is True
    # Someone else's stamp counts as no stamp of its own.
    _stamp(data, pid=4243, version="1.0.0")
    with patch.object(launch_agent.subprocess, "run", side_effect=old):
        assert launch_agent.worker_build_is_stale(4242, data) is True


def test_an_unstamped_worker_still_starting_up_is_left_alone(tmp_path):
    _home, data = _homes(tmp_path)
    young = _FakeLaunchd(etime="00:10")
    with patch.object(launch_agent.subprocess, "run", side_effect=young):
        assert launch_agent.worker_build_is_stale(4242, data) is False


def test_an_unstamped_worker_is_left_alone_when_ps_cannot_date_it(tmp_path):
    _home, data = _homes(tmp_path)
    with patch.object(launch_agent.subprocess, "run") as run:
        run.return_value = _completed(1)
        assert launch_agent.worker_build_is_stale(4242, data) is False


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0, reason="needs a read-only directory"
)
def test_an_unstamped_worker_is_not_restarted_when_no_stamp_could_be_written(tmp_path):
    # Otherwise every `cswap menubar` would restart it: the successor could
    # not write a stamp either.
    _home, data = _homes(tmp_path)
    data.chmod(0o500)
    try:
        with patch.object(launch_agent.subprocess, "run", side_effect=_FakeLaunchd(etime="05:00")):
            assert launch_agent.worker_build_is_stale(4242, data) is False
    finally:
        data.chmod(0o700)


@pytest.mark.parametrize(
    "text, seconds",
    [("00:10", 10), ("05:00", 300), ("01:02:03", 3723), ("2-01:00:00", 176400), ("", None), ("junk", None)],
)
def test_parse_etime(text, seconds):
    assert launch_agent._parse_etime(text) == seconds


def test_ensure_restarts_an_unstamped_worker_of_unknown_build(tmp_path):
    home, data = _homes(tmp_path)
    _login_plist(home)
    fake = _FakeLaunchd(loaded=True, pid=4242, etime="05:00")

    result = _ensure(fake, home, data)

    assert result["action"] == "restarted"
    assert result["stale_build"] is True
    assert ["launchctl", "kickstart", "-k", f"gui/{UID}/{launch_agent.LABEL}"] in fake.calls


def test_install_compares_against_the_session_plist_when_start_at_login_is_off(tmp_path, monkeypatch):
    home, data = _homes(tmp_path)
    session = launch_agent.session_plist_path(backup_dir=data)
    session.write_bytes(
        launch_agent.build_plist(PROGRAM, home=home, env={"CLAUDE_CONFIG_DIR": "/old/profile"})
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/new/profile")
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(1)})
        result = launch_agent.install(home=home, program=PROGRAM, uid=UID, backup_dir=data)
    assert result["profile_changes"] == [("CLAUDE_CONFIG_DIR", "/old/profile", "/new/profile")]
    assert not session.exists()


def test_worker_build_compares_the_commit_too(tmp_path, monkeypatch):
    # A git install keeps its version string across commits.
    _home, data = _homes(tmp_path)
    _stamp(data, pid=4242, version="1.0.0b1", source="aaaaaaa")
    monkeypatch.setattr(
        "claude_swap.update_check.install_identity",
        lambda: {"version": "1.0.0b1", "source": "bbbbbbb"},
    )
    assert launch_agent.worker_build_is_stale(4242, data) is True


def test_ensure_restarts_a_running_worker_of_an_older_build(tmp_path, monkeypatch):
    """`uv tool upgrade` by hand leaves the old build running; `cswap menubar` fixes that."""
    home, data = _homes(tmp_path)
    _login_plist(home)
    _stamp(data, pid=4242, version="0.0.0")
    monkeypatch.setattr(
        "claude_swap.update_check.install_identity",
        lambda: {"version": "1.0.0", "source": None},
    )
    fake = _FakeLaunchd(loaded=True, pid=4242)

    result = _ensure(fake, home, data)

    assert result["action"] == "restarted"
    assert result["stale_build"] is True
    assert ["launchctl", "kickstart", "-k", f"gui/{UID}/{launch_agent.LABEL}"] in fake.calls


def test_ensure_leaves_a_current_build_alone(tmp_path, monkeypatch):
    home, data = _homes(tmp_path)
    _login_plist(home)
    _stamp(data, pid=4242, version="1.0.0")
    monkeypatch.setattr(
        "claude_swap.update_check.install_identity",
        lambda: {"version": "1.0.0", "source": None},
    )
    result = _ensure(_FakeLaunchd(loaded=True, pid=4242), home, data)
    assert result["action"] == "already-running"
    assert result["stale_build"] is False


def test_status_reports_the_running_build_and_whether_it_is_stale(tmp_path, monkeypatch):
    home, data = _homes(tmp_path)
    _login_plist(home)
    _stamp(data, pid=4242, version="0.9.0", source="abcdef0123")
    monkeypatch.setattr(
        "claude_swap.update_check.install_identity",
        lambda: {"version": "1.0.0", "source": None},
    )
    fake = _FakeLaunchd(loaded=True, pid=4242)
    with patch.object(launch_agent.subprocess, "run", side_effect=fake):
        result = launch_agent.status(home=home, uid=UID, backup_dir=data)
    assert result["running_build"] == "0.9.0 (abcdef0)"
    assert result["installed_build"] == "1.0.0"
    assert result["stale_build"] is True


def test_uninstall_removes_the_worker_stamp(tmp_path):
    home, data = _homes(tmp_path)
    stamp = _stamp(data, pid=1)
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(1)})
        result = launch_agent.uninstall(home=home, uid=UID, backup_dir=data)
    assert not stamp.exists()
    assert str(stamp) in result["removed"]
