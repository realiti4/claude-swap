"""Tests for the menu bar's login item (LaunchAgent) management.

Everything here stays off the real ``~/Library/LaunchAgents`` and never shells
out to launchd: the plist builder and path helpers are pure, and the one
subprocess call (``launchctl bootout``) is asserted through a monkeypatched
``subprocess.run``.
"""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from claude_swap import autostart
from claude_swap.exceptions import ClaudeSwitchError


@pytest.fixture
def agent(tmp_path: Path) -> Path:
    """An isolated LaunchAgent path under a fake home."""
    return autostart.launch_agent_path(tmp_path)


# --- paths & command resolution -----------------------------------------------

def test_launch_agent_path_is_under_library_launchagents(tmp_path: Path):
    path = autostart.launch_agent_path(tmp_path)
    assert path == tmp_path / "Library" / "LaunchAgents" / "com.claude-swap.menubar.plist"


def test_menubar_command_prefers_console_script(tmp_path: Path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "python").touch()
    script = bindir / "claude-swap"
    script.touch()

    assert autostart.menubar_command(bindir / "python") == [str(script), "menubar"]


def test_menubar_command_falls_back_to_module_invocation(tmp_path: Path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    python = bindir / "python"
    python.touch()  # no console script beside it

    assert autostart.menubar_command(python) == [str(python), "-m", "claude_swap", "menubar"]


# --- plist shape ---------------------------------------------------------------

def test_build_agent_plist_shape(tmp_path: Path):
    data = autostart.build_agent_plist(
        ["/opt/tools/claude-swap", "menubar"],
        home=tmp_path,
        log_path=tmp_path / "logs" / "menubar-agent.log",
    )

    assert data["Label"] == "com.claude-swap.menubar"
    assert data["ProgramArguments"] == ["/opt/tools/claude-swap", "menubar"]
    assert data["RunAtLoad"] is True
    # Restart a crash, but never resurrect a deliberate Quit.
    assert data["KeepAlive"] == {"SuccessfulExit": False}
    assert data["StandardOutPath"] == str(tmp_path / "logs" / "menubar-agent.log")
    assert data["StandardErrorPath"] == data["StandardOutPath"]


def test_build_agent_plist_path_covers_tool_and_homebrew_bins(tmp_path: Path):
    data = autostart.build_agent_plist(["/x/claude-swap", "menubar"],
                                       home=tmp_path, log_path=tmp_path / "l.log")
    entries = data["EnvironmentVariables"]["PATH"].split(":")

    assert f"{tmp_path}/.local/bin" in entries  # uv/pipx console scripts
    assert "/opt/homebrew/bin" in entries       # where `claude` usually lives
    assert "/usr/bin" in entries                # `security`, `open`
    assert data["EnvironmentVariables"]["HOME"] == str(tmp_path)


# --- enable / disable ----------------------------------------------------------

def test_enable_writes_a_loadable_plist(tmp_path: Path, agent: Path):
    written = autostart.enable(
        log_path=tmp_path / "menubar-agent.log", home=tmp_path, path=agent,
        executable=tmp_path / "bin" / "python",
    )

    assert written == agent
    assert autostart.is_enabled(agent) is True
    data = plistlib.loads(agent.read_bytes())  # parses => launchd can load it
    assert data["Label"] == autostart.LAUNCH_AGENT_LABEL


def test_enable_creates_missing_launchagents_dir(tmp_path: Path, agent: Path):
    assert not agent.parent.exists()

    autostart.enable(log_path=tmp_path / "l.log", home=tmp_path, path=agent)

    assert agent.parent.is_dir()


def test_enable_leaves_no_temp_file_behind(tmp_path: Path, agent: Path):
    autostart.enable(log_path=tmp_path / "l.log", home=tmp_path, path=agent)

    assert [p.name for p in agent.parent.iterdir()] == [agent.name]


def test_enable_refreshes_an_existing_plist(tmp_path: Path, agent: Path):
    agent.parent.mkdir(parents=True)
    agent.write_bytes(plistlib.dumps({"Label": "stale", "ProgramArguments": ["/gone"]}))

    autostart.enable(log_path=tmp_path / "l.log", home=tmp_path, path=agent)

    assert autostart.installed_command(agent) != ["/gone"]


def test_enable_raises_claude_switch_error_on_write_failure(tmp_path: Path):
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("")  # a file where the LaunchAgents dir would go

    with pytest.raises(ClaudeSwitchError):
        autostart.enable(log_path=tmp_path / "l.log", home=tmp_path,
                         path=blocked / "Library" / "agent.plist")


def test_disable_removes_the_plist(tmp_path: Path, agent: Path, monkeypatch):
    monkeypatch.setattr(autostart.subprocess, "run", lambda *a, **k: None)
    autostart.enable(log_path=tmp_path / "l.log", home=tmp_path, path=agent)

    assert autostart.disable(agent, uid=501) is True
    assert autostart.is_enabled(agent) is False


def test_disable_reports_when_nothing_was_installed(tmp_path: Path, agent: Path, monkeypatch):
    calls = []
    monkeypatch.setattr(autostart.subprocess, "run", lambda *a, **k: calls.append(a))

    assert autostart.disable(agent, uid=501) is False
    assert calls == []  # nothing loaded => no launchctl call


def test_disable_boots_out_when_not_running_as_the_login_item(
    tmp_path: Path, agent: Path, monkeypatch
):
    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
    calls = []
    monkeypatch.setattr(autostart.subprocess, "run",
                        lambda cmd, **k: calls.append(cmd))
    autostart.enable(log_path=tmp_path / "l.log", home=tmp_path, path=agent)

    autostart.disable(agent, uid=501)

    assert calls == [["/bin/launchctl", "bootout", "gui/501/com.claude-swap.menubar"]]


def test_disable_does_not_boot_out_itself(tmp_path: Path, agent: Path, monkeypatch):
    # Toggling autostart off from inside the launchd-started app must not kill
    # the app the user is still clicking in.
    monkeypatch.setenv("XPC_SERVICE_NAME", autostart.LAUNCH_AGENT_LABEL)
    calls = []
    monkeypatch.setattr(autostart.subprocess, "run", lambda cmd, **k: calls.append(cmd))
    autostart.enable(log_path=tmp_path / "l.log", home=tmp_path, path=agent)

    autostart.disable(agent, uid=501)

    assert calls == []
    assert autostart.is_enabled(agent) is False


def test_bootout_survives_a_failing_launchctl(tmp_path: Path, agent: Path, monkeypatch):
    def boom(*a, **k):
        raise OSError("launchctl missing")

    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
    monkeypatch.setattr(autostart.subprocess, "run", boom)
    autostart.enable(log_path=tmp_path / "l.log", home=tmp_path, path=agent)

    assert autostart.disable(agent, uid=501) is True  # plist gone regardless


# --- staleness -----------------------------------------------------------------

def test_is_stale_false_for_a_freshly_written_item(tmp_path: Path, agent: Path):
    exe = tmp_path / "bin" / "python"
    exe.parent.mkdir(parents=True)
    exe.touch()
    (exe.parent / "claude-swap").touch()
    autostart.enable(log_path=tmp_path / "l.log", home=tmp_path, path=agent, executable=exe)

    assert autostart.is_stale(agent, executable=exe) is False


def test_is_stale_true_after_the_executable_moves(tmp_path: Path, agent: Path):
    old = tmp_path / "old" / "python"
    old.parent.mkdir(parents=True)
    old.touch()
    (old.parent / "claude-swap").touch()
    autostart.enable(log_path=tmp_path / "l.log", home=tmp_path, path=agent, executable=old)

    new = tmp_path / "new" / "python"  # e.g. after `uv tool upgrade`
    new.parent.mkdir(parents=True)
    new.touch()
    (new.parent / "claude-swap").touch()

    assert autostart.is_stale(agent, executable=new) is True


def test_is_stale_false_when_not_installed(tmp_path: Path, agent: Path):
    assert autostart.is_stale(agent) is False


def test_is_stale_true_for_a_corrupt_plist(tmp_path: Path, agent: Path):
    agent.parent.mkdir(parents=True)
    agent.write_text("not a plist")

    # Unreadable reads as stale, so the next launch rewrites it.
    assert autostart.is_stale(agent) is True
    assert autostart.installed_command(agent) is None


# --- login-item self-detection -------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (autostart.LAUNCH_AGENT_LABEL, True),
    ("com.example.other", False),
    ("0", False),
])
def test_running_as_login_item(monkeypatch, value, expected):
    monkeypatch.setenv("XPC_SERVICE_NAME", value)
    assert autostart.running_as_login_item() is expected


def test_running_as_login_item_without_the_variable(monkeypatch):
    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
    assert autostart.running_as_login_item() is False
