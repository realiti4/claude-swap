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


# --- bundled chart fonts ----------------------------------------------------

WEB_DIR = Path(menubar.__file__).parent / "web"
INDEX_HTML = (WEB_DIR / "index.html").read_text(encoding="utf-8")
PANEL_CSS = (WEB_DIR / "panel.css").read_text(encoding="utf-8")


class TestBundledFonts:
    """The design boards render in Inter + IBM Plex Mono (declared in the Pen
    source; weight 600 explicit, 400 the unserialized default). Neither font
    ships with macOS, so the panel bundles OFL WOFF2 subsets and shadows any
    host-installed copies via ``@font-face`` — every machine draws the
    boards' glyphs (pixel-fidelity mandate)."""

    # Google Fonts serves Inter as one variable WOFF2 (same file for 400 and
    # 600); IBM Plex Mono ships per-weight statics.
    EXPECTED = (
        "inter-var.woff2",
        "ibm-plex-mono-400.woff2",
        "ibm-plex-mono-600.woff2",
    )

    def test_font_binaries_ship_inside_the_package(self) -> None:
        files = sorted(p.name for p in (WEB_DIR / "fonts").glob("*.woff2"))
        assert files, "web/fonts/ is empty — bundled fonts missing"
        for name in self.EXPECTED:
            assert name in files, f"bundled font missing: {name}"

    def test_font_face_shadows_host_fonts_for_both_families(self) -> None:
        for family in ("Inter", "IBM Plex Mono"):
            assert f'font-family: "{family}"' in PANEL_CSS, (
                f"@font-face for {family} missing"
            )
        assert 'url("fonts/' in PANEL_CSS, "fonts must load from web/fonts/"

    def test_csp_allows_bundled_fonts_and_no_remote_origins(self) -> None:
        meta = INDEX_HTML.split("Content-Security-Policy", 1)[1]
        assert "font-src 'self'" in meta
        assert "http" not in meta.replace("http-equiv", ""), (
            "CSP must not open remote origins"
        )

    def test_wheel_contains_the_fonts_when_built(self) -> None:
        import zipfile

        wheels = sorted(
            (Path(__file__).resolve().parents[1] / "dist").glob("*.whl")
        )
        if not wheels:  # full wheel inspection belongs to release-verify
            return
        with zipfile.ZipFile(wheels[-1]) as zf:
            names = zf.namelist()
        for base, weights in self.EXPECTED.items():
            for weight in weights:
                assert any(
                    n.endswith(f"web/fonts/{base}-{weight}.woff2") for n in names
                ), f"wheel missing {base}-{weight}.woff2"
