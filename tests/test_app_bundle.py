"""Tests for the wrapper .app that gives the menu bar a bundle identifier.

Everything is built from fake interpreters inside ``tmp_path`` with a fake
``/Applications``; ``os.execv`` is patched so no test ever replaces the pytest
process.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import app_bundle

# The bundle is macOS-only (reexec_reason says "not macOS" first) and its
# layout leans on POSIX symlink semantics: Windows refuses os.replace over
# a directory symlink and prefixes readlink() results with \\?\.
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="symlinks are POSIX-only")


def _fake_install(tmp_path: Path, blob: bytes = b"#!fake python\n") -> tuple[Path, Path, Path]:
    """A pretend base interpreter, a pretend venv prefix and a writable /Applications."""
    base = tmp_path / "base"
    (base / "bin").mkdir(parents=True)
    exe = base / "bin" / "python3.15"
    exe.write_bytes(blob)
    exe.chmod(0o755)
    (base / "lib").mkdir()
    (base / "lib" / "libpython3.15.dylib").write_bytes(b"fake dylib")
    prefix = tmp_path / "venv"
    (prefix / "lib" / "python3.15" / "site-packages").mkdir(parents=True)
    apps = tmp_path / "Applications"
    apps.mkdir()
    return exe, prefix, apps


def test_build_lays_out_a_bundle_around_a_copy_of_the_interpreter(tmp_path: Path):
    exe, prefix, apps = _fake_install(tmp_path)
    backup = tmp_path / "data"

    python = app_bundle.build(backup, executable=exe, prefix=prefix, applications=apps)

    contents = apps / app_bundle.BUNDLE_DIRNAME / "Contents"
    assert python == contents / "MacOS" / "python"
    assert python.read_bytes() == exe.read_bytes()
    assert not python.is_symlink()  # a symlink would resolve to the bare interpreter
    assert os.access(python, os.X_OK)
    assert sorted(p.name for p in (contents / "MacOS").iterdir()) == ["python"]

    info = plistlib.loads((contents / "Info.plist").read_bytes())
    assert info["CFBundleIdentifier"] == app_bundle.BUNDLE_ID
    assert info["CFBundleExecutable"] == "python"  # managers match exe == CFBundleExecutable
    assert info["LSUIElement"] is True

    cfg = (contents / "pyvenv.cfg").read_text()
    assert f"home = {exe.parent}\n" in cfg

    # lib is a real directory (the stub interpreter dyld-loads
    # @executable_path/../lib/libpython3.X.dylib, which the venv's lib lacks)
    lib = contents / "lib"
    assert lib.is_dir() and not lib.is_symlink()
    assert os.readlink(lib / "python3.15") == str(prefix / "lib" / "python3.15")
    assert (lib / "python3.15" / "site-packages").is_dir()
    assert os.readlink(lib / "libpython3.15.dylib") == str(
        exe.parent.parent / "lib" / "libpython3.15.dylib"
    )


def test_bundle_lives_in_applications_only_when_writable(tmp_path: Path):
    backup = tmp_path / "data"
    apps = tmp_path / "Applications"
    apps.mkdir()
    assert app_bundle.bundle_path(backup, applications=apps) == apps / app_bundle.BUNDLE_DIRNAME

    locked = tmp_path / "locked"
    locked.mkdir(mode=0o555)
    try:
        if os.access(locked, os.W_OK):  # root ignores modes; nothing to test then
            return
        assert app_bundle.bundle_path(backup, applications=locked) == (
            backup / app_bundle.BUNDLE_DIRNAME
        )
        assert app_bundle.bundle_path(backup, applications=tmp_path / "missing") == (
            backup / app_bundle.BUNDLE_DIRNAME
        )
    finally:
        locked.chmod(0o755)


def test_rebuild_leaves_an_unchanged_bundle_alone(tmp_path: Path):
    exe, prefix, apps = _fake_install(tmp_path)
    backup = tmp_path / "data"
    python = app_bundle.build(backup, executable=exe, prefix=prefix, applications=apps)
    before = python.stat().st_ino

    app_bundle.build(backup, executable=exe, prefix=prefix, applications=apps)

    assert python.stat().st_ino == before


def test_rebuild_refreshes_a_changed_interpreter(tmp_path: Path):
    exe, prefix, apps = _fake_install(tmp_path)
    backup = tmp_path / "data"
    python = app_bundle.build(backup, executable=exe, prefix=prefix, applications=apps)
    old_ino = python.stat().st_ino

    exe.write_bytes(b"#!fake python 3.16, longer\n")  # as after a uv python bump
    app_bundle.build(backup, executable=exe, prefix=prefix, applications=apps)

    assert python.read_bytes() == exe.read_bytes()
    assert python.stat().st_ino != old_ino  # replaced, never written in place


def test_rebuild_repoints_lib_and_home_after_a_reinstall(tmp_path: Path):
    exe, prefix, apps = _fake_install(tmp_path)
    backup = tmp_path / "data"
    app_bundle.build(backup, executable=exe, prefix=prefix, applications=apps)

    new_base = tmp_path / "base2"
    new_exe = new_base / "bin" / "python3.16"
    new_exe.parent.mkdir(parents=True)
    new_exe.write_bytes(exe.read_bytes())
    (new_base / "lib").mkdir()
    (new_base / "lib" / "libpython3.16.dylib").write_bytes(b"fake dylib")
    new_prefix = tmp_path / "venv2"
    (new_prefix / "lib" / "python3.16").mkdir(parents=True)
    app_bundle.build(backup, executable=new_exe, prefix=new_prefix, applications=apps)

    lib = apps / app_bundle.BUNDLE_DIRNAME / "Contents" / "lib"
    assert os.readlink(lib / "python3.16") == str(new_prefix / "lib" / "python3.16")
    assert os.readlink(lib / "libpython3.16.dylib") == str(
        new_base / "lib" / "libpython3.16.dylib"
    )
    assert sorted(p.name for p in lib.iterdir()) == [  # 3.15 links pruned
        "libpython3.16.dylib",
        "python3.16",
    ]
    contents = lib.parent
    assert f"home = {new_exe.parent}\n" in (contents / "pyvenv.cfg").read_text()


def test_rebuild_replaces_the_old_lib_symlink_with_a_directory(tmp_path: Path):
    """Bundles built before the libpython fix had lib -> <venv>/lib."""
    exe, prefix, apps = _fake_install(tmp_path)
    contents = apps / app_bundle.BUNDLE_DIRNAME / "Contents"
    contents.mkdir(parents=True)
    os.symlink(str(prefix / "lib"), contents / "lib")

    app_bundle.build(tmp_path / "data", executable=exe, prefix=prefix, applications=apps)

    lib = contents / "lib"
    assert lib.is_dir() and not lib.is_symlink()
    assert (lib / "libpython3.15.dylib").is_symlink()
    assert (prefix / "lib" / "python3.15").is_dir()  # the venv itself is untouched


def test_in_bundle_recognises_only_our_bundle(tmp_path: Path):
    exe, prefix, apps = _fake_install(tmp_path)
    python = app_bundle.build(tmp_path / "data", executable=exe, prefix=prefix, applications=apps)
    assert app_bundle.in_bundle(python)
    assert not app_bundle.in_bundle(exe)

    other = tmp_path / "Other.app" / "Contents" / "MacOS"
    other.mkdir(parents=True)
    (other.parent / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleIdentifier": "com.example.other"})
    )
    assert not app_bundle.in_bundle(other / "python")


def test_reexec_reason_lists_every_way_to_stay_put(tmp_path: Path):
    assert app_bundle.reexec_reason(platform="linux") == "not macOS"
    assert app_bundle.reexec_reason(
        platform="darwin", environ={app_bundle.OPT_OUT_ENV: "1"}
    ) == f"{app_bundle.OPT_OUT_ENV} is set"
    with patch.object(sys, "_framework", "Python", create=True):
        assert app_bundle.reexec_reason(platform="darwin", environ={}) == (
            "framework build of Python"
        )
    with patch.object(sys, "_framework", "", create=True), patch.object(
        app_bundle, "in_bundle", return_value=True
    ):
        assert app_bundle.reexec_reason(platform="darwin", environ={}) == (
            "already in the bundle"
        )


def test_maybe_reexec_execs_the_same_command_inside_the_bundle(tmp_path: Path):
    exe, prefix, apps = _fake_install(tmp_path)
    backup = tmp_path / "data"
    calls: list[tuple[str, list[str]]] = []
    real_build = app_bundle.build

    with patch.object(app_bundle, "reexec_reason", return_value=None), patch.object(
        app_bundle, "APPLICATIONS_DIR", apps
    ), patch.object(
        app_bundle, "build", lambda b: real_build(b, executable=exe, prefix=prefix, applications=apps)
    ), patch.object(app_bundle, "interpreter_error", return_value=None), patch.object(
        app_bundle.os, "execv", lambda p, a: calls.append((p, a))
    ), patch.object(sys, "argv", ["/x/bin/cswap", "menubar"]):
        app_bundle.maybe_reexec(backup)

    python = str(apps / app_bundle.BUNDLE_DIRNAME / "Contents" / "MacOS" / "python")
    assert calls == [(python, [python, "-m", "claude_swap", "menubar"])]


def test_maybe_reexec_falls_back_when_the_bundle_cannot_be_built(tmp_path: Path, capsys):
    calls: list = []
    with patch.object(app_bundle, "reexec_reason", return_value=None), patch.object(
        app_bundle, "build", side_effect=OSError("disk full")
    ), patch.object(app_bundle.os, "execv", lambda p, a: calls.append((p, a))):
        reason = app_bundle.maybe_reexec(tmp_path / "data")

    assert calls == []
    assert reason is not None and "disk full" in reason
    assert "disk full" in capsys.readouterr().err


def test_maybe_reexec_falls_back_when_the_bundled_interpreter_cannot_start(
    tmp_path: Path, capsys
):
    calls: list = []
    with patch.object(app_bundle, "reexec_reason", return_value=None), patch.object(
        app_bundle, "build", return_value=tmp_path / "python"
    ), patch.object(
        app_bundle, "interpreter_error", return_value="dyld: libpython3.13.dylib not found"
    ), patch.object(app_bundle.os, "execv", lambda p, a: calls.append((p, a))):
        reason = app_bundle.maybe_reexec(tmp_path / "data")

    assert calls == []
    assert reason is not None and "libpython3.13" in reason
    assert "works as before" in capsys.readouterr().err


def test_maybe_reexec_falls_back_when_execv_fails(tmp_path: Path, capsys):
    with patch.object(app_bundle, "reexec_reason", return_value=None), patch.object(
        app_bundle, "build", return_value=tmp_path / "python"
    ), patch.object(app_bundle, "interpreter_error", return_value=None), patch.object(
        app_bundle.os, "execv", side_effect=OSError("Permission denied")
    ):
        reason = app_bundle.maybe_reexec(tmp_path / "data")

    assert reason is not None and "Permission denied" in reason
    assert "works as before" in capsys.readouterr().err


def test_interpreter_error_reports_an_unrunnable_interpreter(tmp_path: Path):
    exe, prefix, apps = _fake_install(tmp_path)  # "#!fake python" cannot exec
    python = app_bundle.build(tmp_path / "data", executable=exe, prefix=prefix, applications=apps)
    assert app_bundle.interpreter_error(python) is not None


@pytest.mark.skipif(
    bool(getattr(sys, "_framework", "")), reason="framework builds never re-exec"
)
def test_bundle_built_from_the_real_interpreter_starts_and_imports(tmp_path: Path):
    """Regression for stub interpreters: a copy of uv's CPython 3.12/3.13 aborts in
    dyld unless Contents/lib holds libpython3.X.dylib. Fake-interpreter tests
    cannot catch that, so this one builds from the interpreter running pytest."""
    apps = tmp_path / "Applications"
    apps.mkdir()

    python = app_bundle.build(tmp_path / "data", applications=apps)

    proc = subprocess.run(
        [str(python), "-c", "import claude_swap, sys; print(sys.prefix)"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert Path(proc.stdout.strip()) == apps / app_bundle.BUNDLE_DIRNAME / "Contents"
    assert app_bundle.interpreter_error(python) is None
