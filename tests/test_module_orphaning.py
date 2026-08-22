"""No claude_swap submodule is orphaned by a patch.dict(sys.modules) block.

`unittest.mock`'s `patch.dict` restores by CLEARING the dict and repopulating
from an ENTRY-TIME snapshot, so a module FIRST IMPORTED INSIDE the block is
deleted from ``sys.modules`` on exit while surviving as an attribute of its
parent. That pair -- attribute of parent, absent from sys.modules -- is the
orphan signature, and it silently defeats patching:

    monkeypatch.setattr("claude_swap.X.f", stub)   walks getattr from the ROOT,
                                                   finds the orphan, patches it,
                                                   REPORTS SUCCESS
    from claude_swap.X import f                    finds no sys.modules entry,
                                                   RE-IMPORTS, calls the REAL f

Nothing raises. Measured on ``claude_swap.pin``, and through ``pin.py``'s
``_install_hint()`` chain also ``update_check`` and ``cache`` whenever the
``cswap-pin`` extra is absent.

WHY A CHECK AND NOT A LIST. The fix is to import the module at conftest scope
so it is in every snapshot -- but a hardcoded list of today's three names goes
stale the moment a fourth module is first imported inside such a block, and it
goes stale WHILE STILL PASSING. This finds them instead.

WALK EVERY claude_swap PARENT, NOT JUST THE ROOT. Measured while two sessions
disagreed 3-vs-1: a root-only walk sees 1 parent, this sees 25, and that alone
was the entire 21-vs-18 gap in the two control counts. An orphan whose parent
is a SUBpackage is invisible to a root-only scan.

RUN IT IN THE PROCESS THE TESTS RAN IN. Under xdist the tests execute in worker
SUBPROCESSES while ``pytest_sessionfinish`` runs in the CONTROLLER, so a
plugin-based scan reads a process where no test ever ran -- three scan designs
in a row returned an honest zero about the wrong process. As an ordinary test
it runs in the worker, which is the subject.

WHAT IT DOES NOT CATCH, stated because a check whose limits are unwritten gets
trusted past them: it sees the orphans present in ITS worker at ITS point in
the run. A worker that never ran a ``patch.dict`` test has nothing to find, so
under `-n auto` this samples rather than proves. It is a tripwire on a
condition that recurs, not a proof of its absence -- which is still strictly
more than the list it replaces, because the list cannot fail at all.

Written by the cswap session; placed here because 210 is the branch that
carries the condition, and a guard that cannot fail on its own branch is the
thing this repo spent the day deleting.
"""

from __future__ import annotations

import sys
import types


def orphaned_claude_swap_modules() -> set[str]:
    orphans: set[str] = set()
    for parent_name in [n for n in list(sys.modules) if n.startswith("claude_swap")]:
        parent = sys.modules.get(parent_name)
        if parent is None:
            continue
        for attr in dir(parent):
            child = getattr(parent, attr, None)
            if not isinstance(child, types.ModuleType):
                continue
            name = getattr(child, "__name__", "")
            if name.startswith("claude_swap") and name not in sys.modules:
                orphans.add(name)
    return orphans


def test_no_claude_swap_module_is_orphaned():
    orphans = orphaned_claude_swap_modules()
    assert not orphans, (
        f"orphaned: {sorted(orphans)} -- first imported inside a "
        f"patch.dict(sys.modules, ...) block, so they are attributes of their "
        f"parent but absent from sys.modules. Any string-target patch of them "
        f"now silently does nothing. conftest's autouse "
        f"`_no_orphaned_claude_swap_modules` re-attaches them after every "
        f"test, so seeing this means the orphan was created and read WITHIN a "
        f"single test, which that fixture cannot reach."
    )


def test_the_scan_can_find_an_orphan(tmp_path):
    """The control. Without it the check above passes on a scan that cannot
    see anything -- which is how three of our scans returned an honest zero."""
    from unittest.mock import patch

    pkg = tmp_path / "orphanprobe"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "child.py").write_text("x = 1\n")
    sys.path.insert(0, str(tmp_path))
    try:
        import orphanprobe

        assert "orphanprobe.child" not in sys.modules, "premise"
        with patch.dict(sys.modules, {"_orphanprobe_fake": object()}):
            import orphanprobe.child  # noqa: F401
        assert "orphanprobe.child" not in sys.modules, (
            "patch.dict no longer orphans a fresh import -- if mock changed "
            "this, the check above is obsolete rather than passing"
        )
        found = {
            getattr(getattr(orphanprobe, a), "__name__", "")
            for a in dir(orphanprobe)
            if isinstance(getattr(orphanprobe, a, None), types.ModuleType)
        }
        assert "orphanprobe.child" in found, "the walk cannot see an orphan"
    finally:
        sys.path.remove(str(tmp_path))
        for n in [n for n in list(sys.modules) if n.startswith("orphanprobe")]:
            del sys.modules[n]
