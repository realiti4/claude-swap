"""Run a short-lived child and take every process it started down with it.

POSIX runs the child with plain ``subprocess.run``. Windows needs more,
because a child that exits can leave its own children behind for good.

`claude auth status --json` (the session probe) shows how. Claude Code fires
``git remote get-url origin`` during startup and may exit before that call
returns. Git for Windows' ``cmd\\git.exe`` is a launcher: it creates the real
``mingw64\\bin\\git.exe`` SUSPENDED, places it in a job, then resumes it.
Claude's runtime (libuv) kills the launcher when Claude exits, and when that
lands between the launcher's create and resume, the real git is never resumed
and was never placed in any job that dies with Claude (libuv's job lets
grandchildren break away silently). It sits suspended forever: one thread,
no modules loaded, no CPU, and a parent that is already gone. On one Windows
machine 11 of 15 `claude auth status --json` runs left such an orphan, so
every `cswap run` added roughly one.

The fix is a Job Object that the child is born into (created suspended,
assigned, then resumed, so nothing it starts can escape first), with
KILL_ON_JOB_CLOSE and without BREAKAWAY_OK: grandchildren stay in it, and
closing it when the run ends kills whatever is left. With it, 0 of 15 runs
left an orphan.
"""

from __future__ import annotations

import subprocess
import sys


def run_and_reap(
    argv: list[str], *, env: dict[str, str], timeout: float
) -> subprocess.CompletedProcess[str]:
    """``subprocess.run(argv, capture_output=True, text=True)`` with ``env`` and ``timeout``.

    Same return value and exceptions (``TimeoutExpired``, ``OSError``). On
    Windows, every process the child started that is still alive when the run
    ends is killed with it.
    """
    if sys.platform != "win32":
        return subprocess.run(
            argv, env=env, capture_output=True, text=True, timeout=timeout
        )
    return _run_in_kill_on_close_job(argv, env, timeout)


def _run_in_kill_on_close_job(
    argv: list[str], env: dict[str, str], timeout: float
) -> subprocess.CompletedProcess[str]:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    ntdll = ctypes.WinDLL("ntdll")  # type: ignore[attr-defined]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
    )
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    ntdll.NtResumeProcess.argtypes = (wintypes.HANDLE,)
    ntdll.NtResumeProcess.restype = ctypes.c_long

    class _Basic(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _Extended(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _Basic),
            ("IoInfo", ctypes.c_uint64 * 6),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    job_object_extended_limit_information = 9
    job_object_limit_kill_on_job_close = 0x2000
    create_suspended = 0x00000004
    process_terminate = 0x0001
    process_set_quota = 0x0100
    process_suspend_resume = 0x0800

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        limits = _Extended()
        limits.BasicLimitInformation.LimitFlags = job_object_limit_kill_on_job_close
        if not kernel32.SetInformationJobObject(
            job,
            job_object_extended_limit_information,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            raise ctypes.WinError(ctypes.get_last_error())

        with subprocess.Popen(
            argv,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=create_suspended,
        ) as proc:
            # Assign before the first instruction runs, then resume. Popen
            # closes the primary thread handle, so the process is resumed as
            # a whole (NtResumeProcess, as psutil's resume() does). If either
            # step fails the child is killed: a probe that runs outside the
            # job is the leak this module exists to stop.
            handle = kernel32.OpenProcess(
                process_terminate | process_set_quota | process_suspend_resume,
                False,
                proc.pid,
            )
            try:
                if not handle or not kernel32.AssignProcessToJobObject(job, handle):
                    error = ctypes.WinError(ctypes.get_last_error())
                    proc.kill()
                    raise error
                status = ntdll.NtResumeProcess(handle)
                if status != 0:
                    proc.kill()
                    raise OSError(f"NtResumeProcess failed (NTSTATUS {status:#x})")
            finally:
                if handle:
                    kernel32.CloseHandle(handle)

            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                # Kill the whole tree before draining: a grandchild holding
                # the inherited pipes would otherwise keep communicate() from
                # ever returning.
                kernel32.TerminateJobObject(job, 1)
                stdout, stderr = proc.communicate()
                raise subprocess.TimeoutExpired(
                    argv, timeout, output=stdout, stderr=stderr
                ) from None
            return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)
    finally:
        # KILL_ON_JOB_CLOSE: whatever the child left behind dies here.
        kernel32.CloseHandle(job)
