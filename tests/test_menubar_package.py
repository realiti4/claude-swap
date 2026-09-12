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
    "notify",
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


# --- packaged native icon --------------------------------------------------

from pathlib import Path  # noqa: E402

APP_SOURCE = (Path(menubar.__file__).parent / "app.py").read_text(encoding="utf-8")


class TestNativeIcon:
    """The status item carries the Interlock template image from the final
    handoff, packaged inside the installed package — never read from the
    repo's assets/ folder at runtime."""

    @staticmethod
    def _asset(name: str) -> Path:
        return Path(menubar.__file__).parent / "assets" / name

    def test_icon_assets_ship_inside_the_package(self) -> None:
        for name in ("icon-template.pdf", "icon-template-18.png", "icon-template-36.png"):
            assert self._asset(name).is_file(), f"packaged asset missing: {name}"

    def test_status_image_loads_the_pdf_template_with_png_fallback(self) -> None:
        assert '"icon-template.pdf"' in APP_SOURCE
        assert '"icon-template-36.png"' in APP_SOURCE  # 2x raster fallback
        assert "setTemplate_(True)" in APP_SOURCE
        assert "imageWithSystemSymbolName" not in APP_SOURCE, (
            "the SF Symbol is replaced by the brand mark"
        )

    def test_status_item_is_branded_and_accessible(self) -> None:
        assert 'setAccessibilityDescription_("Claude Code Swap")' in APP_SOURCE
        assert 'setToolTip_("Claude Code Swap")' in APP_SOURCE
