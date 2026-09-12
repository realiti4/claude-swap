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


def test_app_module_import_safe_without_pyobjc_anywhere() -> None:
    # The import itself must never pull AppKit at module level: this is the
    # guarantee Linux CI depends on.
    import claude_swap.menubar.app as app_module
    import sys as _sys

    loaded = [m for m in _sys.modules if m.split(".")[0] in ("AppKit", "WebKit", "PyObjCTools")]
    assert not any(m.startswith(("AppKit", "WebKit")) for m in loaded)
    assert app_module.MenuBarSettings.title_pct == "both"
