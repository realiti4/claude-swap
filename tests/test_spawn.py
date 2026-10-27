"""``claude_swap.spawn``: the no-fork spawn path on macOS (issue #517)."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from claude_swap import macos_keychain, process_detection, spawn

ARGV = ["ps", "-o", "pid=", "-p", "1234"]
DARWIN = sys.platform == "darwin"


@pytest.fixture
def recorder(monkeypatch):
    """Replace the real ``subprocess.run`` and collect what the helper hands it."""
    calls = []

    def fake(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(spawn.subprocess, "run", fake)
    return calls


@pytest.fixture
def darwin(monkeypatch):
    """Simulate the macOS decision inputs without touching the real system."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(spawn.shutil, "which", lambda name, path=None: f"/bin/{name}")
    monkeypatch.setattr(spawn, "_inheritable_fds_above_stdio", lambda: False)


# ---------------------------------------------------------------------------
# the decision
# ---------------------------------------------------------------------------


class TestDecision:
    def test_bare_name_is_resolved_and_spawned_without_fork(self, darwin, recorder):
        spawn.run(ARGV, capture_output=True, text=True, timeout=5)
        # the hit is handed over in absolute form (a drive is prepended on Windows)
        assert recorder == [
            (
                [os.path.abspath("/bin/ps"), "-o", "pid=", "-p", "1234"],
                {"capture_output": True, "text": True, "timeout": 5, "close_fds": False},
            )
        ]

    def test_directory_qualified_executable_is_never_looked_up(
        self, darwin, recorder, monkeypatch
    ):
        def no_lookup(name, path=None):
            raise AssertionError(f"PATH lookup for {name!r}")

        monkeypatch.setattr(spawn.shutil, "which", no_lookup)
        spawn.run(["/usr/bin/security", "-i"], input="x", capture_output=True)
        [(cmd, kwargs)] = recorder
        assert cmd == ["/usr/bin/security", "-i"]
        assert kwargs == {"input": "x", "capture_output": True, "close_fds": False}

    def test_name_missing_from_path_is_passed_through(
        self, darwin, recorder, monkeypatch
    ):
        monkeypatch.setattr(spawn.shutil, "which", lambda name, path=None: None)
        spawn.run(ARGV, capture_output=True)
        assert recorder == [(ARGV, {"capture_output": True})]

    def test_lookup_searches_the_path_the_child_would_get(
        self, darwin, recorder, monkeypatch
    ):
        seen = []

        def which(name, path=None):
            seen.append(path)
            return f"/bin/{name}"

        monkeypatch.setattr(spawn.shutil, "which", which)
        monkeypatch.setenv("PATH", "/parent/bin")
        spawn.run(ARGV, capture_output=True)
        spawn.run(ARGV, capture_output=True, env={"PATH": "/child/bin"})
        spawn.run(ARGV, capture_output=True, env={"HOME": "/"})
        assert seen == ["/parent/bin", "/child/bin", os.defpath]

    def test_inheritable_descriptor_keeps_the_fork_path(
        self, darwin, recorder, monkeypatch
    ):
        monkeypatch.setattr(spawn, "_inheritable_fds_above_stdio", lambda: True)
        spawn.run(ARGV, capture_output=True)
        assert recorder == [(ARGV, {"capture_output": True})]

    @pytest.mark.parametrize(
        "extra",
        [
            {"cwd": "/"},
            {"preexec_fn": os.getpid},
            {"pass_fds": (3,)},
            {"start_new_session": True},
            {"process_group": 0},
            {"shell": True},
            {"executable": "/bin/ps"},
            {"umask": 0},
            {"user": 0},
            {"group": 0},
            {"extra_groups": [0]},
            {"close_fds": True},
            {"close_fds": False},
        ],
    )
    def test_shapes_cpython_forks_for_are_passed_through(self, darwin, recorder, extra):
        spawn.run(ARGV, capture_output=True, **extra)
        assert recorder == [(ARGV, {"capture_output": True, **extra})]

    def test_string_command_is_passed_through(self, darwin, recorder):
        spawn.run("ps", capture_output=True)
        assert recorder == [("ps", {"capture_output": True})]

    @pytest.mark.parametrize("platform", ["linux", "win32"])
    def test_other_platforms_are_untouched(self, darwin, recorder, monkeypatch, platform):
        monkeypatch.setattr(sys, "platform", platform)
        spawn.run(ARGV, capture_output=True)
        assert recorder == [(ARGV, {"capture_output": True})]


# ---------------------------------------------------------------------------
# the descriptor gate
# ---------------------------------------------------------------------------


class TestInheritableScan:
    @pytest.mark.skipif(sys.platform == "win32", reason="no /dev/fd")
    def test_live_inheritable_descriptor_is_seen(self):
        baseline = spawn._inheritable_fds_above_stdio()
        r, w = os.pipe()
        try:
            os.set_inheritable(r, True)
            assert spawn._inheritable_fds_above_stdio() is True
            os.set_inheritable(r, False)
            assert spawn._inheritable_fds_above_stdio() is baseline
        finally:
            os.close(r)
            os.close(w)

    def test_unreadable_fd_dir_counts_as_inheritable(self, monkeypatch, tmp_path):
        monkeypatch.setattr(spawn, "_FD_DIR", str(tmp_path / "no-such-dir"))
        assert spawn._inheritable_fds_above_stdio() is True

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX executables")
    def test_resolve_finds_bare_names_on_path_only(self, monkeypatch, tmp_path):
        tool = tmp_path / "tool"
        tool.write_text("#!/bin/sh\n")
        tool.chmod(0o755)
        monkeypatch.setenv("PATH", str(tmp_path))
        assert spawn._resolve("tool") == str(tool)
        assert spawn._resolve("other") is None
        assert spawn._resolve("./other") == "./other"
        assert spawn._resolve(b"tool") is None
        # the child's PATH, not the parent's, decides
        monkeypatch.setenv("PATH", "/nowhere")
        assert spawn._resolve("tool", {"PATH": str(tmp_path)}) == str(tool)
        assert spawn._resolve("tool", {"HOME": "/"}) is None
        assert spawn._resolve("sh", {"HOME": "/"}) == shutil.which("sh", path=os.defpath)

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX executables")
    def test_hit_through_an_empty_path_entry_is_made_absolute(
        self, monkeypatch, tmp_path
    ):
        """``PATH=:/x`` means the current directory first; ``shutil.which`` then
        answers the bare name, which would send CPython back to fork."""
        tool = tmp_path / "tool"
        tool.write_text("#!/bin/sh\n")
        tool.chmod(0o755)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("PATH", os.pathsep + "/nowhere")
        assert shutil.which("tool") == "tool"
        resolved = spawn._resolve("tool")
        assert os.path.isabs(resolved) and os.path.samefile(resolved, tool)


# ---------------------------------------------------------------------------
# the call sites route through the helper
# ---------------------------------------------------------------------------


class TestCallSites:
    @pytest.fixture
    def routed(self, monkeypatch):
        calls = []

        def fake(cmd, **kwargs):
            calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 1, "", "")

        monkeypatch.setattr(spawn, "run", fake)
        return calls

    def test_ps(self, routed, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        assert process_detection._ps(1234, "lstart") is None
        assert routed == [["ps", "-o", "lstart=", "-p", "1234"]]

    @pytest.mark.no_keychain_fake
    def test_security(self, routed):
        assert macos_keychain.item_exists("svc", "acct") is False
        with pytest.raises(macos_keychain.KeychainError):
            macos_keychain.get_password("svc", "acct")
        with pytest.raises(macos_keychain.KeychainError):
            macos_keychain.set_password("svc", "acct", "value")
        with pytest.raises(macos_keychain.KeychainError):
            macos_keychain.set_password(
                "svc", "acct", "v" * (macos_keychain.SECURITY_STDIN_LINE_LIMIT + 1)
            )
        with pytest.raises(macos_keychain.KeychainError):
            macos_keychain.delete_password("svc", "acct")
        assert [cmd[:2] for cmd in routed] == [
            ["/usr/bin/security", "find-generic-password"],
            ["/usr/bin/security", "find-generic-password"],
            ["/usr/bin/security", "-i"],
            ["/usr/bin/security", "add-generic-password"],
            ["/usr/bin/security", "delete-generic-password"],
        ]


# ---------------------------------------------------------------------------
# the real thing, macOS only
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not DARWIN, reason="posix_spawn path is taken on macOS only")
class TestOnMacOS:
    @pytest.fixture
    def spawned(self, monkeypatch):
        """Count ``os.posix_spawn`` calls; the gate is bypassed so the test
        process's own descriptors cannot turn this into a fork-path run."""
        monkeypatch.setattr(spawn, "_inheritable_fds_above_stdio", lambda: False)
        real = os.posix_spawn
        calls = []

        def counting(*args, **kwargs):
            calls.append(args[0])
            return real(*args, **kwargs)

        monkeypatch.setattr(os, "posix_spawn", counting)
        return calls

    def test_ps_output_arrives_via_posix_spawn(self, spawned):
        result = spawn.run(
            ["ps", "-o", "pid=", "-p", str(os.getpid())],
            capture_output=True,
            text=True,
            timeout=5,
            env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
        )
        assert result.returncode == 0
        assert result.stdout.strip() == str(os.getpid())
        assert [os.path.basename(p) for p in spawned] == ["ps"]

    def test_stdin_input_and_timeout_behave_as_before(self, spawned):
        assert spawn.run(["cat"], input="hi", capture_output=True, text=True).stdout == "hi"
        with pytest.raises(subprocess.TimeoutExpired):
            spawn.run(["sleep", "5"], capture_output=True, timeout=0.2)
        assert [os.path.basename(p) for p in spawned] == ["cat", "sleep"]

    def test_missing_program_raises_as_before(self, spawned):
        with pytest.raises(FileNotFoundError):
            spawn.run(["/no/such/dir/tool"], capture_output=True)
        with pytest.raises(FileNotFoundError):
            spawn.run(["no-such-program-cswap-517"], capture_output=True)

    def test_armed_fork_hook_stays_silent(self):
        """The #517 line itself: arm libmalloc's fork hook the way a critical
        memory-pressure note would, then spawn. The fork path (control) must
        print it, the helper must not. Runs in a throwaway interpreter because
        the hook cannot be removed once installed."""
        src = Path(spawn.__file__).resolve().parents[1]
        code = f"""
import ctypes, os, subprocess, sys
sys.path.insert(0, {str(src)!r})
try:
    handler = ctypes.CDLL("/usr/lib/system/libsystem_malloc.dylib").malloc_memory_event_handler
except (OSError, AttributeError):
    sys.exit(3)
handler(ctypes.c_ulong(0x4))  # DISPATCH_MEMORYPRESSURE_CRITICAL
from claude_swap import spawn
argv = ["ps", "-o", "pid=", "-p", str(os.getpid())]
subprocess.run(argv, capture_output=True)
print("CONTROL-DONE", file=sys.stderr, flush=True)
spawn.run(argv, capture_output=True)
spawn.run(["/usr/bin/true"], capture_output=True)
print("SPAWN-DONE", file=sys.stderr, flush=True)
"""
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
        )
        if proc.returncode == 3:
            pytest.skip("libmalloc memory-event handler not reachable here")
        assert proc.returncode == 0, proc.stderr
        control, _, fixed = proc.stderr.partition("CONTROL-DONE")
        if "MallocStackLogging" not in control:
            pytest.skip("libmalloc's fork hook did not arm on this macOS")
        assert "SPAWN-DONE" in fixed
        assert "MallocStackLogging" not in fixed, fixed
