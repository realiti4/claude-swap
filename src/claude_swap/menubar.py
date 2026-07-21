"""Public façade for the native macOS ``cswap menubar`` surface.

Import-safe presentation helpers remain available to CLI and test callers on
all platforms. The PyObjC implementation is imported only at launch time.
"""

from __future__ import annotations

from claude_swap.menubar_controller import MenuBarController
from claude_swap.menubar_viewmodel import (
    AUTO_THRESHOLD_CHOICES,
    EMPTY_SNAPSHOT,
    ICON,
    REFRESH_CHOICES,
    SWITCH_HISTORY_LIMIT,
    TITLE_PCT_CHOICES,
    CapacityState,
    FreshnessState,
    MenuBarPopoverViewModel,
    MenuBarSettings,
    PopoverAccountViewModel,
    UsageRowViewModel,
    UsageScope,
    _account_display_usage,
    _adapt_snapshot,
    _live_countdown,
    _local_part,
    _resets_at_ts,
    _rolled_weekly_window,
    _usage_log_key,
    _window_pct,
    format_account_label,
    format_title,
    format_usage_log,
    parse_switch_history,
    popover_view_model,
    tightest_pct,
    usage_summary,
)
from claude_swap.switcher import SENTINEL_NOTES


def run(switcher) -> int:
    """Launch the native AppKit status item and transient compact popover."""
    from claude_swap.menubar_appkit import run_native_menubar

    return run_native_menubar(switcher)


__all__ = [
    "AUTO_THRESHOLD_CHOICES",
    "CapacityState",
    "EMPTY_SNAPSHOT",
    "FreshnessState",
    "ICON",
    "MenuBarController",
    "MenuBarPopoverViewModel",
    "MenuBarSettings",
    "PopoverAccountViewModel",
    "REFRESH_CHOICES",
    "SENTINEL_NOTES",
    "SWITCH_HISTORY_LIMIT",
    "TITLE_PCT_CHOICES",
    "UsageRowViewModel",
    "UsageScope",
    "_account_display_usage",
    "_adapt_snapshot",
    "_live_countdown",
    "_local_part",
    "_resets_at_ts",
    "_rolled_weekly_window",
    "_usage_log_key",
    "_window_pct",
    "format_account_label",
    "format_title",
    "format_usage_log",
    "parse_switch_history",
    "popover_view_model",
    "run",
    "tightest_pct",
    "usage_summary",
]
