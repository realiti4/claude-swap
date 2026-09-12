"""macOS menu bar app for claude-swap (``cswap --menubar``).

Transitional package shim: the historical single module lives intact at
``claude_swap.menubar._legacy`` while the package is built out task by task
(pure helpers first, then the PyObjC/web shell). Every name the existing
tests and ``cli.py`` import is re-exported below, pinned by
``tests/test_menubar_package.py``; the legacy module is deleted only when
the new shell fully replaces it.
"""

from claude_swap.menubar._legacy import (  # noqa: F401
    EMPTY_SNAPSHOT,
    SENTINEL_NOTES,
    MenuBarSettings,
    _account_display_usage,
    _adapt_snapshot,
    _live_countdown,
    _resets_at_ts,
    _rolled_weekly_window,
    _usage_log_key,
    ensure_notification_identity,
    format_account_label,
    format_title,
    format_usage_log,
    framework_build_warning,
    parse_switch_history,
    run,
    tightest_pct,
    usage_summary,
)
