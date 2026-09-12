"""macOS menu bar app for claude-swap (``cswap --menubar``).

Package layout while the v2 panel is being built out:

- ``viewmodel`` — pure helpers + the panel's view-model builder (import-safe
  everywhere, unit-tested in CI)
- ``app`` — the PyObjC shell (AppKit imported lazily inside ``run()``; the
  pure pieces at module level stay import-safe on every platform)
- ``bridge`` — the WKWebView action routing (added with the popover)

This ``__init__`` re-exports the surface ``tests/test_menubar.py``,
``tests/test_cli.py``, and ``cli.py`` import, pinned by
``tests/test_menubar_package.py``.
"""

from claude_swap.menubar.app import (  # noqa: F401
    MenuBarSettings,
    framework_build_warning,
    notify,
    run,
)
from claude_swap.menubar.viewmodel import (  # noqa: F401
    EMPTY_SNAPSHOT,
    SENTINEL_NOTES,
    _account_display_usage,
    _adapt_snapshot,
    _age_text,
    _live_countdown,
    _resets_at_ts,
    _rolled_weekly_window,
    _usage_log_key,
    build,
    format_account_label,
    format_title,
    format_usage_log,
    parse_switch_history,
    tightest_pct,
    usage_summary,
)
