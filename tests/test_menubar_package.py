"""Package-surface contract for ``claude_swap.menubar``.

The menubar is being converted from a single module into a package
(``menubar/``) one task at a time. This test pins the import surface that
``tests/test_menubar.py``, ``tests/test_cli.py``, and ``cli.py`` rely on, so
the restructure can never silently drop a name.
"""

from __future__ import annotations

import claude_swap.menubar as menubar

# Every attribute accessed as ``menubar.<name>`` by test_menubar.py, plus the
# names cli.py imports directly.
REQUIRED_SURFACE = (
    # public helpers + app entry points
    "run",
    "framework_build_warning",
    "ensure_notification_identity",
    "MenuBarSettings",
    "EMPTY_SNAPSHOT",
    "SENTINEL_NOTES",
    "format_title",
    "format_account_label",
    "format_usage_log",
    "parse_switch_history",
    "tightest_pct",
    "usage_summary",
    # private helpers the existing tests exercise directly
    "_account_display_usage",
    "_adapt_snapshot",
    "_live_countdown",
    "_resets_at_ts",
    "_rolled_weekly_window",
    "_usage_log_key",
)


def test_package_reexports_legacy_surface() -> None:
    for name in REQUIRED_SURFACE:
        assert hasattr(menubar, name), f"claude_swap.menubar lost: {name}"
        assert getattr(menubar, name) is not None
