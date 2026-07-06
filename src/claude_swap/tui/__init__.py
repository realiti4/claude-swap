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
    from claude_swap.tui.app import CswapApp

    app = CswapApp(switcher, start=start)
    app.run()
    return app.return_code or 0
