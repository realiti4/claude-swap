"""Tests for launchd.py — plist generation, install, uninstall, status.

All tests mock subprocess and plistlib.dump so no real launchctl calls
are made.  macOS-only paths are enabled by forcing Platform.detect()→MACOS.
"""

from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

import claude_swap.launchd as ld
from claude_swap.launchd import (
    LABEL,
    PLIST_PATH,
    _plist_dict,
    _program_args,
    agent_status,
    install_agent,
    uninstall_agent,
)
from claude_swap.models import Platform


# ---------------------------------------------------------------------------
# _plist_dict — pure, no I/O
# ---------------------------------------------------------------------------

class TestPlistDict:
    """Tests for _plist_dict (pure function)."""

    def _make(self, args=None, log=None):
        if args is None:
            args = ["/usr/local/bin/cswap", "_auto-daemon"]
        if log is None:
            log = Path("/tmp/auto-switch.log")
        return _plist_dict(args, log)

    def test_label_correct(self):
        d = self._make()
        assert d["Label"] == LABEL

    def test_program_arguments_is_list(self):
        args = ["/usr/bin/cswap", "_auto-daemon"]
        d = _plist_dict(args, Path("/tmp/test.log"))
        assert d["ProgramArguments"] == args

    def test_run_at_load_true(self):
        assert self._make()["RunAtLoad"] is True

    def test_keep_alive_only_on_crash(self):
        """KeepAlive must restart on crash but NOT on a clean (enabled=False) exit."""
        assert self._make()["KeepAlive"] == {"SuccessfulExit": False}

    def test_throttle_interval_set(self):
        """ThrottleInterval guards against rapid crash-respawn loops."""
        assert self._make()["ThrottleInterval"] == 30

    def test_stdout_path_set(self):
        log = Path("/tmp/my.log")
        d = _plist_dict(["/bin/cswap", "_auto-daemon"], log)
        assert d["StandardOutPath"] == str(log)

    def test_stderr_path_set(self):
        log = Path("/tmp/my.log")
        d = _plist_dict(["/bin/cswap", "_auto-daemon"], log)
        assert d["StandardErrorPath"] == str(log)

    def test_stdout_stderr_same_log(self):
        d = self._make()
        assert d["StandardOutPath"] == d["StandardErrorPath"]

    def test_plist_is_valid_plistlib_roundtrip(self):
        """dict must round-trip through plistlib without error."""
        d = self._make()
        raw = plistlib.dumps(d)
        parsed = plistlib.loads(raw)
        assert parsed["Label"] == LABEL
        assert parsed["KeepAlive"] == {"SuccessfulExit": False}
        assert parsed["ThrottleInterval"] == 30


# ---------------------------------------------------------------------------
# _program_args
# ---------------------------------------------------------------------------

class TestProgramArgs:
    def test_uses_cswap_when_found(self):
        with patch("claude_swap.launchd.shutil.which", return_value="/usr/local/bin/cswap"):
            args = _program_args()
        assert args == ["/usr/local/bin/cswap", "_auto-daemon"]

    def test_fallback_to_python_module_when_cswap_missing(self):
        with patch("claude_swap.launchd.shutil.which", return_value=None):
            args = _program_args()
        assert args[0] == sys.executable
        assert "-m" in args
        assert "claude_swap" in args
        assert "_auto-daemon" in args


# ---------------------------------------------------------------------------
# install_agent
# ---------------------------------------------------------------------------

class TestInstallAgent:
    """install_agent tests — no real launchctl calls."""

    def _run_install(
        self,
        tmp_path: Path,
        platform: Platform = Platform.MACOS,
        bootout_rc: int = 0,
        bootstrap_rc: int = 0,
    ) -> tuple[str, bytes]:
        """Run install_agent with mocked subprocess and return (result_str, plist_bytes)."""
        written: list[bytes] = []

        def fake_open(path, mode):
            import io
            buf = io.BytesIO()

            class _FH:
                def write(self, data):
                    buf.write(data)
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    written.append(buf.getvalue())

            return _FH()

        def fake_run(cmd, **kw):
            r = MagicMock(spec=subprocess.CompletedProcess)
            if cmd[1] == "bootout":
                r.returncode = bootout_rc
                r.stderr = ""
                r.stdout = ""
            elif cmd[1] in ("bootstrap", "load"):
                r.returncode = bootstrap_rc
                r.stderr = "" if bootstrap_rc == 0 else "load error"
                r.stdout = ""
            else:
                r.returncode = 0
                r.stderr = ""
                r.stdout = ""
            return r

        plist_parent = tmp_path / "LaunchAgents"
        plist_parent.mkdir(parents=True)
        mock_plist_path = plist_parent / f"{LABEL}.plist"

        with (
            patch("claude_swap.launchd.Platform.detect", return_value=platform),
            patch("claude_swap.launchd.PLIST_PATH", mock_plist_path),
            patch("claude_swap.launchd.subprocess.run", side_effect=fake_run),
            patch("claude_swap.launchd.shutil.which", return_value="/usr/bin/cswap"),
        ):
            result = install_agent(backup_root=tmp_path)

        plist_bytes = mock_plist_path.read_bytes() if mock_plist_path.exists() else b""
        return result, plist_bytes

    def test_writes_plist(self, tmp_path: Path):
        _, plist_bytes = self._run_install(tmp_path)
        assert len(plist_bytes) > 0

    def test_plist_written_atomically_no_temp_left(self, tmp_path: Path):
        """After install, the final plist is valid and no .plist.tmp remains."""
        _, plist_bytes = self._run_install(tmp_path)
        # Valid plist content (atomic replace landed a complete file).
        parsed = plistlib.loads(plist_bytes)
        assert parsed["Label"] == LABEL
        # No leftover temp file from the atomic write.
        tmp_file = tmp_path / "LaunchAgents" / f"{LABEL}.plist.tmp"
        assert not tmp_file.exists()

    def test_plist_write_failure_cleans_temp_and_raises(self, tmp_path: Path):
        """If plistlib.dump fails mid-write, the temp file is removed and the
        error propagates (no truncated final plist)."""
        plist_parent = tmp_path / "LaunchAgents"
        plist_parent.mkdir(parents=True)
        mock_plist_path = plist_parent / f"{LABEL}.plist"
        tmp_file = plist_parent / f"{LABEL}.plist.tmp"

        with (
            patch("claude_swap.launchd.Platform.detect", return_value=Platform.MACOS),
            patch("claude_swap.launchd.PLIST_PATH", mock_plist_path),
            patch("claude_swap.launchd.shutil.which", return_value="/usr/bin/cswap"),
            patch(
                "claude_swap.launchd.plistlib.dump",
                side_effect=OSError("disk full"),
            ),
        ):
            with pytest.raises(OSError):
                install_agent(backup_root=tmp_path)

        # No final plist, no leftover temp.
        assert not mock_plist_path.exists()
        assert not tmp_file.exists()

    def test_plist_has_correct_label(self, tmp_path: Path):
        _, plist_bytes = self._run_install(tmp_path)
        parsed = plistlib.loads(plist_bytes)
        assert parsed["Label"] == LABEL

    def test_plist_has_program_arguments(self, tmp_path: Path):
        _, plist_bytes = self._run_install(tmp_path)
        parsed = plistlib.loads(plist_bytes)
        assert "_auto-daemon" in parsed["ProgramArguments"]

    def test_plist_has_keep_alive(self, tmp_path: Path):
        _, plist_bytes = self._run_install(tmp_path)
        parsed = plistlib.loads(plist_bytes)
        assert parsed["KeepAlive"] == {"SuccessfulExit": False}
        assert parsed["ThrottleInterval"] == 30

    def test_plist_has_run_at_load(self, tmp_path: Path):
        _, plist_bytes = self._run_install(tmp_path)
        parsed = plistlib.loads(plist_bytes)
        assert parsed["RunAtLoad"] is True

    def test_plist_log_path_set(self, tmp_path: Path):
        _, plist_bytes = self._run_install(tmp_path)
        parsed = plistlib.loads(plist_bytes)
        assert "auto-switch.log" in parsed["StandardOutPath"]
        assert "auto-switch.log" in parsed["StandardErrorPath"]

    def test_success_message_on_ok(self, tmp_path: Path):
        result, _ = self._run_install(tmp_path, bootstrap_rc=0)
        assert "installed" in result.lower() or "started" in result.lower()

    def test_failure_message_on_load_error(self, tmp_path: Path):
        result, _ = self._run_install(tmp_path, bootstrap_rc=1)
        assert "failed" in result.lower() or "error" in result.lower()

    def test_non_macos_no_plist_no_launchctl(self, tmp_path: Path):
        """On Linux, install_agent is a pure no-op: no plist, no launchctl."""
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            r = MagicMock(spec=subprocess.CompletedProcess)
            r.returncode = 0
            r.stderr = ""
            r.stdout = ""
            return r

        # Point PLIST_PATH at a dir that does NOT exist yet — if install_agent
        # tried to write, it would create it; we assert it never does.
        mock_plist_path = tmp_path / "LaunchAgents" / f"{LABEL}.plist"

        with (
            patch("claude_swap.launchd.Platform.detect", return_value=Platform.LINUX),
            patch("claude_swap.launchd.PLIST_PATH", mock_plist_path),
            patch("claude_swap.launchd.subprocess.run", side_effect=fake_run),
            patch("claude_swap.launchd.shutil.which", return_value="/usr/bin/cswap"),
        ):
            result = install_agent(backup_root=tmp_path)

        assert not calls   # no launchctl calls
        assert not mock_plist_path.exists()       # no plist written
        assert not mock_plist_path.parent.exists()  # no LaunchAgents dir created
        assert "macos" in result.lower()


# ---------------------------------------------------------------------------
# uninstall_agent
# ---------------------------------------------------------------------------

class TestUninstallAgent:
    def test_uninstall_removes_plist(self, tmp_path: Path):
        plist = tmp_path / f"{LABEL}.plist"
        plist.write_text("dummy")

        bootout_called = []

        def fake_run(cmd, **kw):
            bootout_called.append(cmd)
            r = MagicMock(spec=subprocess.CompletedProcess)
            r.returncode = 0
            r.stderr = ""
            r.stdout = ""
            return r

        with (
            patch("claude_swap.launchd.Platform.detect", return_value=Platform.MACOS),
            patch("claude_swap.launchd.PLIST_PATH", plist),
            patch("claude_swap.launchd.subprocess.run", side_effect=fake_run),
        ):
            result = uninstall_agent()

        assert not plist.exists()
        assert "bootout" in str(bootout_called)

    def test_uninstall_idempotent_when_plist_absent(self, tmp_path: Path):
        plist = tmp_path / f"{LABEL}.plist"  # does not exist

        with (
            patch("claude_swap.launchd.Platform.detect", return_value=Platform.MACOS),
            patch("claude_swap.launchd.PLIST_PATH", plist),
            patch("claude_swap.launchd.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
            result = uninstall_agent()

        assert "uninstalled" in result.lower()

    def test_uninstall_non_macos_no_launchctl(self, tmp_path: Path):
        plist = tmp_path / f"{LABEL}.plist"
        plist.write_text("dummy")
        calls = []

        with (
            patch("claude_swap.launchd.Platform.detect", return_value=Platform.LINUX),
            patch("claude_swap.launchd.PLIST_PATH", plist),
            patch("claude_swap.launchd.subprocess.run", side_effect=lambda cmd, **kw: calls.append(cmd)),
        ):
            result = uninstall_agent()

        assert not calls   # no launchctl on non-macOS


# ---------------------------------------------------------------------------
# agent_status
# ---------------------------------------------------------------------------

class TestAgentStatus:
    def test_not_installed_when_plist_absent(self, tmp_path: Path):
        plist = tmp_path / "absent.plist"
        with patch("claude_swap.launchd.PLIST_PATH", plist):
            result = agent_status()
        assert "not installed" in result.lower() or "absent" in result.lower()

    def test_status_running_from_print_output(self, tmp_path: Path):
        plist = tmp_path / f"{LABEL}.plist"
        plist.write_text("dummy")

        print_output = f"path = /Users/user/Library/LaunchAgents/{LABEL}.plist\nstate = running\n"

        def fake_run(cmd, **kw):
            r = MagicMock(spec=subprocess.CompletedProcess)
            r.returncode = 0
            r.stdout = print_output
            r.stderr = ""
            return r

        with (
            patch("claude_swap.launchd.Platform.detect", return_value=Platform.MACOS),
            patch("claude_swap.launchd.PLIST_PATH", plist),
            patch("claude_swap.launchd.subprocess.run", side_effect=fake_run),
        ):
            result = agent_status()

        assert "running" in result.lower()

    def test_status_falls_back_to_list(self, tmp_path: Path):
        plist = tmp_path / f"{LABEL}.plist"
        plist.write_text("dummy")

        def fake_run(cmd, **kw):
            r = MagicMock(spec=subprocess.CompletedProcess)
            if "print" in cmd:
                r.returncode = 1
                r.stdout = ""
                r.stderr = "error"
            else:
                # list output
                r.returncode = 0
                r.stdout = f"123\t-\t{LABEL}\n"
                r.stderr = ""
            return r

        with (
            patch("claude_swap.launchd.Platform.detect", return_value=Platform.MACOS),
            patch("claude_swap.launchd.PLIST_PATH", plist),
            patch("claude_swap.launchd.subprocess.run", side_effect=fake_run),
        ):
            result = agent_status()

        assert "loaded" in result.lower()

    def test_status_not_loaded_when_not_in_list(self, tmp_path: Path):
        plist = tmp_path / f"{LABEL}.plist"
        plist.write_text("dummy")

        def fake_run(cmd, **kw):
            r = MagicMock(spec=subprocess.CompletedProcess)
            if "print" in cmd:
                r.returncode = 1
                r.stdout = ""
                r.stderr = "not found"
            else:
                r.returncode = 0
                r.stdout = "99\t-\tcom.other.service\n"   # LABEL not present
                r.stderr = ""
            return r

        with (
            patch("claude_swap.launchd.Platform.detect", return_value=Platform.MACOS),
            patch("claude_swap.launchd.PLIST_PATH", plist),
            patch("claude_swap.launchd.subprocess.run", side_effect=fake_run),
        ):
            result = agent_status()

        assert "not loaded" in result.lower()

    def test_status_non_macos(self, tmp_path: Path):
        plist = tmp_path / f"{LABEL}.plist"
        plist.write_text("dummy")
        with (
            patch("claude_swap.launchd.Platform.detect", return_value=Platform.LINUX),
            patch("claude_swap.launchd.PLIST_PATH", plist),
        ):
            result = agent_status()
        assert "plist present" in result.lower() or "not available" in result.lower()
