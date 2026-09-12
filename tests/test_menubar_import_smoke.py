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
    AppKit = pytest.importorskip(
        "AppKit", reason="pyobjc not installed (menubar extra absent)"
    )

    popover = AppKit.NSPopover.alloc().init()
    assert popover.isShown() is False  # visibility accessor the shell polls
    assert callable(popover.showRelativeToRect_ofView_preferredEdge_)
    assert callable(popover.performClose_)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only shell")
def test_shell_selector_api_assumptions() -> None:
    """Every PyObjC selector the shell calls, checked as metadata.

    The class of bug this guards against already shipped once
    (mistranslated NSPopover selectors, silently dead clicks); this
    generalizes the guard to the rest of the AppKit/WebKit surface without
    needing a window server.
    """
    AppKit = pytest.importorskip(
        "AppKit", reason="pyobjc not installed (menubar extra absent)"
    )
    WebKit = pytest.importorskip(
        "WebKit", reason="pyobjc not installed (menubar extra absent)"
    )

    # status item: the click-delivery API whose absence killed right-click
    assert AppKit.NSCell.instancesRespondToSelector_("sendActionOn:")
    assert AppKit.NSStatusBar.instancesRespondToSelector_(
        "statusItemWithLength:"
    )
    # webview: the inbound channel + lockdown + outbound calls
    assert WebKit.WKWebView.instancesRespondToSelector_(
        "evaluateJavaScript:completionHandler:"
    )
    assert WebKit.WKWebView.instancesRespondToSelector_(
        "loadFileURL:allowingReadAccessToURL:"
    )
    assert WebKit.WKUserContentController.instancesRespondToSelector_(
        "addScriptMessageHandler:name:"
    )
    # timers + fallback menu
    assert AppKit.NSTimer.respondsToSelector_(
        "timerWithTimeInterval:target:selector:userInfo:repeats:"
    )
    assert AppKit.NSMenu.instancesRespondToSelector_(
        "popUpMenuPositioningItem:atLocation:inView:"
    )


def test_target_action_selector_strings_match_methods() -> None:
    """Every @"..." selector string in app.py must name a real ShellTarget
    method (source-level check, all platforms)."""
    import re
    from pathlib import Path

    import claude_swap.menubar.app as app_module

    source = Path(app_module.__file__).read_text(encoding="utf-8")
    selector_strings = set(re.findall(r'"([a-zA-Z]+:)"', source))
    # selectors used as actions/timers, not NSMenu titles like "Quit"
    method_defs = set(re.findall(r"def ([a-zA-Z]+)_\(", source))
    used = {name for name in selector_strings if name.startswith(("on", "auto"))}
    missing = {sel[:-1] for sel in used} - method_defs  # strip the trailing ':'
    assert not missing, f"selector strings without backing methods: {sorted(missing)}"


class TestNotify:
    """The osascript notification path replaced rumps.notification — it needs
    the behavioral tests the old path had (escaping + never raising)."""

    def test_osa_quote_escapes_terminators(self) -> None:
        from claude_swap.menubar.app import _osa_quote

        assert _osa_quote('say "hi"') == 'say \\"hi\\"'
        assert _osa_quote("back\\slash") == "back\\\\slash"
        assert _osa_quote("plain") == "plain"

    def test_notify_never_raises_and_runs_off_thread(self, monkeypatch) -> None:
        import claude_swap.menubar.app as app_module

        ran = []
        monkeypatch.setattr(
            app_module.subprocess, "run",
            lambda *a, **k: ran.append(1) or (_ for _ in ()).throw(OSError("no")),
        )
        started = []

        class InlineThread:
            def __init__(self, target=None, daemon=False):
                self._target = target

            def start(self):
                started.append(1)
                self._target()

        monkeypatch.setattr(app_module.threading, "Thread", InlineThread)
        app_module.notify("t", "m")  # must not propagate the OSError
        assert started == [1]
        assert ran == [1]


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
