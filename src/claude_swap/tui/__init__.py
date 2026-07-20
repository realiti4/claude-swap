"""Textual-based interactive TUI for claude-swap.

Entry point for ``cswap tui`` (and bare ``cswap`` in an interactive
terminal). Heavy imports (textual, rich) stay inside :func:`run` so the
plain CLI paths — ``cswap list``, cron's ``cswap auto --once`` — never pay
for them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claude_swap.switcher import ClaudeAccountSwitcher


def run(switcher: "ClaudeAccountSwitcher", start: str = "dashboard") -> int:
    """Run the TUI over an existing switcher. Returns the process exit code.

    ``start="watch"`` (the ``cswap watch`` command) opens directly on the
    live watch page, stacked over the dashboard.
    """
    from claude_swap.appearance import detect_terminal_background, drain_stdin
    from claude_swap.tui.app import CswapApp

    # Sense the terminal background while we still own stdin in cooked mode
    # (Textual's driver starts inside app.run()). Always detect so cycling to
    # 'auto' works even when the initial theme is explicit. Both calls are
    # meant to fail safe on their own, but they're wrapped here too: a
    # detection bug must never crash the TUI launch.
    try:
        detected = detect_terminal_background()
    except Exception:
        detected = None
    app = CswapApp(switcher, start=start, detected=detected)
    # Drain any late OSC reply immediately before Textual's driver starts,
    # so it isn't reissued as keystrokes once the app takes over the terminal.
    try:
        drain_stdin()
    except Exception:
        pass
    app.run()
    return app.return_code or 0
