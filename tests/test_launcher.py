"""Tests for claude_swap.launcher — the `cswap launch` desktop-app helper.

Fully hermetic: `subprocess.run` (ps/open/cp/kill/osascript) is always mocked
and every account resolves through a real ClaudeAccountSwitcher seeded onto
the isolated `temp_home` from conftest.py — never a real profile directory or
a spawned app.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import launcher
from claude_swap.exceptions import AccountNotFoundError, LauncherError
from claude_swap.switcher import ClaudeAccountSwitcher


def _seeded_switcher(temp_home: Path) -> ClaudeAccountSwitcher:
    """A real switcher with two managed accounts (slot 2 aliased 'rb', slot 3)."""
    switcher = ClaudeAccountSwitcher()
    switcher._setup_directories()
    switcher._init_sequence_file()
    data = switcher._get_sequence_data()
    data["accounts"]["2"] = {
        "email": "work@co.com",
        "uuid": "uuid-2",
        "organizationUuid": "org-2",
        "organizationName": "",
        "alias": "rb",
        "added": "2024-01-01T00:00:00Z",
    }
    data["accounts"]["3"] = {
        "email": "personal@co.com",
        "uuid": "uuid-3",
        "organizationUuid": "",
        "organizationName": "",
        "added": "2024-01-01T00:00:00Z",
    }
    data["sequence"] = [2, 3]
    switcher._write_json(switcher.sequence_file, data)
    return switcher


@pytest.fixture
def switcher(temp_home: Path) -> ClaudeAccountSwitcher:
    return _seeded_switcher(temp_home)


@pytest.fixture(autouse=True)
def _isolated_support_dir(tmp_path, monkeypatch):
    """Redirect every profile path under launcher into a throwaway tmp_path.

    Every launcher function resolves paths through support_dir(), so patching
    it alone keeps profile_dir()/default_profile_dir() (and this test file)
    off the real `~/Library/Application Support`.
    """
    fake_support = tmp_path / "Application Support"
    fake_support.mkdir()
    monkeypatch.setattr(launcher, "support_dir", lambda: fake_support)
    return fake_support


def _patch_app_path(tmp_path: Path, *, installed: bool):
    """`Path.exists` can't be patched on a specific instance (read-only slot
    on PosixPath), so swap the whole APP_PATH module attribute instead."""
    app = tmp_path / "Claude.app"
    if installed:
        app.mkdir()
    return patch("claude_swap.launcher.APP_PATH", app)


def _ps_line(pid: int, profile_path: Path, *, helper: bool = False) -> str:
    args = f"--user-data-dir={profile_path}"
    if helper:
        return f"{pid} {launcher._APP_BINARY_MARKER} --type=renderer {args}"
    return f"{pid} {launcher._APP_BINARY_MARKER} {args}"


def _ps_result(lines: list[str]) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["ps"], returncode=0, stdout="\n".join(lines), stderr=""
    )


class TestRunningPid:
    def test_matches_profile_by_user_data_dir(self, tmp_path):
        path = tmp_path / "Claude-a2"
        with patch(
            "claude_swap.launcher.subprocess.run",
            return_value=_ps_result([_ps_line(54602, path)]),
        ):
            assert launcher.running_pid(path) == 54602

    def test_ignores_electron_helper_processes(self, tmp_path):
        """A helper process (--type=renderer) must never be reported as the
        main app — killing/focusing it would not do what the user expects."""
        path = tmp_path / "Claude-a2"
        with patch(
            "claude_swap.launcher.subprocess.run",
            return_value=_ps_result([_ps_line(99999, path, helper=True)]),
        ):
            assert launcher.running_pid(path) is None

    def test_no_match_returns_none(self, tmp_path):
        path = tmp_path / "Claude-a2"
        other = tmp_path / "Claude-a3"
        with patch(
            "claude_swap.launcher.subprocess.run",
            return_value=_ps_result([_ps_line(1234, other)]),
        ):
            assert launcher.running_pid(path) is None

    def test_empty_ps_output_returns_none(self, tmp_path):
        path = tmp_path / "Claude-a2"
        with patch(
            "claude_swap.launcher.subprocess.run",
            return_value=_ps_result([]),
        ):
            assert launcher.running_pid(path) is None


class TestFocus:
    def test_focus_invokes_osascript_with_pid(self):
        with patch("claude_swap.launcher.subprocess.run") as run:
            launcher.focus(54602)
        args = run.call_args[0][0]
        assert args[0] == "osascript"
        assert "54602" in " ".join(args)


class TestSeedProfile:
    def _default_profile(self, tmp_path: Path) -> Path:
        default = tmp_path / "Application Support" / "Claude"
        default.mkdir(parents=True)
        return default

    def test_copies_seed_items_and_session_uuid(self, tmp_path, _isolated_support_dir):
        default = _isolated_support_dir / "Claude"
        default.mkdir()
        for name in launcher.SEED_NAMES:
            (default / name).write_text("x")
        (default / "claude-code-sessions" / "uuid-2").mkdir(parents=True)

        dest = _isolated_support_dir / "Claude-a2"
        with patch("claude_swap.launcher.subprocess.run") as run:
            launcher.seed_profile(dest, "uuid-2")

        copied_srcs = {call.args[0][2] for call in run.call_args_list}
        for name in launcher.SEED_NAMES:
            assert str(default / name) in copied_srcs
        assert str(default / "claude-code-sessions" / "uuid-2") in copied_srcs
        assert run.call_count == len(launcher.SEED_NAMES) + 1

    def test_never_copies_secrets(self, tmp_path, _isolated_support_dir):
        """Cookies / config.json / ant-device-registry.json must NEVER be
        seeded — each profile has to sign in on its own."""
        default = _isolated_support_dir / "Claude"
        default.mkdir()
        for name in launcher.SEED_NAMES:
            (default / name).write_text("x")
        for secret in ("Cookies", "config.json", "ant-device-registry.json"):
            (default / secret).write_text("secret")

        dest = _isolated_support_dir / "Claude-a2"
        with patch("claude_swap.launcher.subprocess.run") as run:
            launcher.seed_profile(dest, "")

        copied_srcs = {call.args[0][2] for call in run.call_args_list}
        for secret in ("Cookies", "config.json", "ant-device-registry.json"):
            assert str(default / secret) not in copied_srcs

    def test_existing_profile_is_not_re_seeded(self, tmp_path, _isolated_support_dir):
        """Edge case: a profile that already has an item must not be
        overwritten (that item would be an already-signed-in session)."""
        default = _isolated_support_dir / "Claude"
        default.mkdir()
        name = launcher.SEED_NAMES[0]
        (default / name).write_text("fresh default content")

        dest = _isolated_support_dir / "Claude-a2"
        dest.mkdir()
        (dest / name).write_text("already there — signed in")

        with patch("claude_swap.launcher.subprocess.run") as run:
            launcher.seed_profile(dest, "")

        run.assert_not_called()
        assert (dest / name).read_text() == "already there — signed in"

    def test_no_default_profile_yet_is_a_noop(self, tmp_path, _isolated_support_dir):
        """First-ever run: no plain Claude.app profile exists to seed from."""
        dest = _isolated_support_dir / "Claude-a2"
        with patch("claude_swap.launcher.subprocess.run") as run:
            launcher.seed_profile(dest, "uuid-2")
        run.assert_not_called()
        assert dest.is_dir()


class TestLaunchAccount:
    def test_launches_new_account_creates_and_seeds_profile(self, switcher, _isolated_support_dir, tmp_path):
        path = launcher.profile_dir("2")
        with patch("claude_swap.launcher.subprocess.run", return_value=_ps_result([])) as run, \
             _patch_app_path(tmp_path, installed=True):
            outcome = launcher.launch_account(switcher, "2")

        assert outcome.account_num == "2"
        assert outcome.email == "work@co.com"
        assert outcome.focused is False
        assert outcome.fresh is True
        open_calls = [c for c in run.call_args_list if c.args[0][0] == "open"]
        assert len(open_calls) == 1
        assert f"--user-data-dir={path}" in open_calls[0].args[0]

    def test_resolves_alias_through_switcher_resolver(self, switcher, _isolated_support_dir, tmp_path):
        """`cswap launch rb` must resolve the alias — the whole point of
        going through switcher.resolve_account rather than reading
        sequence.json directly."""
        with patch("claude_swap.launcher.subprocess.run", return_value=_ps_result([])), \
             _patch_app_path(tmp_path, installed=True):
            outcome = launcher.launch_account(switcher, "rb")
        assert outcome.account_num == "2"
        assert outcome.email == "work@co.com"

    def test_already_running_focuses_without_double_launch(self, switcher, _isolated_support_dir):
        """Edge case: an account already running must be focused, never
        relaunched as a second instance."""
        path = launcher.profile_dir("2")
        ps_running = _ps_result([_ps_line(54602, path)])

        def fake_run(cmd, *a, **k):
            if cmd[0] == "ps":
                return ps_running
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        with patch("claude_swap.launcher.subprocess.run", side_effect=fake_run) as run:
            outcome = launcher.launch_account(switcher, "2")

        assert outcome.focused is True
        assert outcome.pid == 54602
        open_calls = [c for c in run.call_args_list if c.args[0][0] == "open"]
        assert open_calls == []
        osascript_calls = [c for c in run.call_args_list if c.args[0][0] == "osascript"]
        assert len(osascript_calls) == 1

    def test_unknown_account_raises(self, switcher, _isolated_support_dir):
        with pytest.raises(AccountNotFoundError):
            launcher.launch_account(switcher, "999")

    def test_missing_app_bundle_raises(self, switcher, _isolated_support_dir, tmp_path):
        with patch("claude_swap.launcher.subprocess.run", return_value=_ps_result([])), \
             _patch_app_path(tmp_path, installed=False):
            with pytest.raises(LauncherError, match="Claude.app not found"):
                launcher.launch_account(switcher, "2")


class TestStopAccount:
    def test_stops_running_account(self, switcher, _isolated_support_dir):
        path = launcher.profile_dir("2")

        def fake_run(cmd, *a, **k):
            if cmd[0] == "ps":
                return _ps_result([_ps_line(54602, path)])
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        with patch("claude_swap.launcher.subprocess.run", side_effect=fake_run) as run:
            outcome = launcher.stop_account(switcher, "2")

        assert outcome.stopped is True
        assert outcome.pid == 54602
        kill_calls = [c for c in run.call_args_list if c.args[0][0] == "kill"]
        assert len(kill_calls) == 1
        assert kill_calls[0].args[0] == ["kill", "54602"]

    def test_not_running_reports_stopped_false(self, switcher, _isolated_support_dir):
        with patch("claude_swap.launcher.subprocess.run", return_value=_ps_result([])) as run:
            outcome = launcher.stop_account(switcher, "3")

        assert outcome.stopped is False
        assert outcome.pid is None
        kill_calls = [c for c in run.call_args_list if c.args[0][0] == "kill"]
        assert kill_calls == []

    def test_unknown_account_raises(self, switcher, _isolated_support_dir):
        with pytest.raises(AccountNotFoundError):
            launcher.stop_account(switcher, "999")


class TestLaunchAllStopAll:
    def test_launch_all_covers_every_managed_account(self, switcher, _isolated_support_dir, tmp_path):
        with patch("claude_swap.launcher.subprocess.run", return_value=_ps_result([])), \
             _patch_app_path(tmp_path, installed=True):
            outcomes = launcher.launch_all(switcher)
        assert [o.account_num for o in outcomes] == ["2", "3"]

    def test_stop_all_covers_every_managed_account(self, switcher, _isolated_support_dir):
        with patch("claude_swap.launcher.subprocess.run", return_value=_ps_result([])):
            outcomes = launcher.stop_all(switcher)
        assert [o.account_num for o in outcomes] == ["2", "3"]
        assert all(o.stopped is False for o in outcomes)
