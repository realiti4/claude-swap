"""Import smoke test for the PyObjC menubar shell.

``app.py`` must import cleanly on every platform *without* PyObjC installed
(AppKit is imported lazily inside ``run()``), which is what keeps Linux CI
green and lets macOS CI smoke the module even before the ``menubar`` extra
is installed there. Headless-safe: nothing here builds UI objects.
"""

from __future__ import annotations

import pytest
import sys


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only shell")
def test_app_module_imports_headless() -> None:
    from claude_swap.menubar import app

    assert callable(app.run)
    assert callable(app.framework_build_warning)
    assert callable(app.notify)
    assert app.MenuBarSettings is not None


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only shell")
def test_ns_popover_api_assumptions() -> None:
    import AppKit

    popover = AppKit.NSPopover.alloc().init()
    assert popover.isShown() is False  # visibility accessor the shell polls
    assert callable(popover.showRelativeToRect_ofView_preferredEdge_)
    assert callable(popover.performClose_)


def test_source_uses_real_ns_popover_selectors() -> None:
    """The status-item click path died silently twice (issue: clicking the
    item did nothing) because PyObjC selector names were mistranslated —
    NSPopover has no isVisible()/showRelativeTo_... — and exceptions raised
    inside action selectors vanish into NSLog, never stderr. Guard the exact
    names the shell calls so a rename or typo fails HERE, loudly, everywhere.
    """
    import claude_swap.menubar.app as app_module
    from pathlib import Path

    source = Path(app_module.__file__).read_text(encoding="utf-8")
    for bad in ("isVisible()", "show_relativeTo_", "showRelativeTo_ofView_"):
        assert bad not in source, f"app.py uses non-existent NSPopover API: {bad}"
    assert ".isShown()" in source
    assert "showRelativeToRect_ofView_preferredEdge_(" in source


def test_app_module_import_safe_without_pyobjc_anywhere() -> None:
    """The import itself must never pull AppKit at module level — this is the
    guarantee Linux CI depends on. Checked in a clean subprocess: this test
    file's own darwin-gated tests import AppKit into the shared worker, which
    would otherwise make this assertion order-dependent."""
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    src = str(Path(__file__).resolve().parents[1] / "src")
    code = (
        "import sys, json; import claude_swap.menubar.app as a; "
        "assert a.MenuBarSettings.title_pct == 'both'; "
        "print(json.dumps([m for m in sys.modules "
        "if m.split('.')[0] in ('AppKit', 'WebKit', 'PyObjCTools')]))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, env={**os.environ, "PYTHONPATH": src},
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == []
