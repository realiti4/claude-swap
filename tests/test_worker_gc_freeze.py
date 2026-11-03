"""Only a disposable xdist worker freezes its gc heap, and only at exit.

Under ``-n N`` the controller waits for each worker to exit, and a worker with
a large heap spends time collecting it. ``gc.freeze()`` at ``atexit`` skips
that after pytest's whole cleanup stack, its final unraisable-exception sweep
included, has run; the controller and ``-n 0`` keep every cleanup check.

Each case runs a child pytest on a scratch project with tests/conftest.py as a
plugin, and the child reports whether its freeze count rose above its own
baseline (nonzero on some Pythons) by interpreter exit.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Registered before pytest starts, so atexit (LIFO) reports after the hook ran.
CHILD = """
import atexit, gc, sys, pytest
base = gc.get_freeze_count()
atexit.register(lambda: print(gc.get_freeze_count() > base))
sys.exit(pytest.main(sys.argv[1:]))
"""

# A __del__-raising cycle that only becomes garbage between pytest's first and
# its final unraisable-exception sweep: a freeze before the second hides it.
CYCLE_CONFTEST = """
import pytest

class Cycle:
    def __del__(self):
        raise RuntimeError("boom")

@pytest.hookimpl(trylast=True)
def pytest_configure(config):
    config.cycle = Cycle()
    config.cycle.me = config.cycle
    config.add_cleanup(lambda: delattr(config, "cycle"))
"""


def _child_pytest(tmp_path, worker, *opts):
    (tmp_path / "test_ok.py").write_text("def test_ok(): pass\n")
    # HOME below tmp_path keeps the child off the live credential store, and not
    # tmp_path itself: its guard refuses a write to HOME's direct children, the
    # project's own __pycache__ included.
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("PYTEST_", "CLAUDE_", "XDG_"))
    }
    env["HOME"] = str(tmp_path / "home")
    (tmp_path / "home").mkdir()
    if worker:
        env["PYTEST_XDIST_WORKER"] = "gw0"
    done = subprocess.run(
        [sys.executable, "-c", CHILD, "-q", "-p", "no:cacheprovider",
         "-p", "tests.conftest", f"--rootdir={tmp_path}", str(tmp_path), *opts],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )
    return done.returncode, done.stdout.splitlines()[-1], done.stdout + done.stderr


def test_a_non_worker_does_not_freeze_its_heap_at_exit(tmp_path):
    rc, last, out = _child_pytest(tmp_path, False)
    assert (rc, last) == (0, "False"), out


def test_the_freeze_leaves_the_final_unraisable_sweep_reporting(tmp_path):
    (tmp_path / "conftest.py").write_text(CYCLE_CONFTEST)
    rc, last, out = _child_pytest(tmp_path, True, "-W", "error")
    assert (rc, last) == (1, "True"), out
    assert "boom" in out
