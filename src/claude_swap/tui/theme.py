"""Textual themes for claude-swap, built from ``claude_swap.palette``.

Textual's ``Theme``/``register_theme`` mechanism only covers its own design
tokens ($accent, $foreground, ...) consumed by CSS. widgets.py and
autoview.py render usage bars, account rows, and severity markers with Rich
``Text.append(style=...)`` calls that bypass CSS entirely, so they need their
own resolution: ``current_theme_colors()`` reads whichever theme is active on
the running app and returns the matching ``palette.ThemeColors``, falling
back to cswap-dark's colors outside a running app (e.g. unit tests calling
these render functions directly) or for an unregistered theme name.
"""

from __future__ import annotations

from textual.app import active_app
from textual.theme import Theme

from claude_swap.palette import (
    CRIT_PCT,
    DEFAULT_THEME,
    THEME_COLORS,
    WARN_PCT,
    ThemeColors,
    theme_colors,
)


def _textual_theme(name: str, colors: ThemeColors) -> Theme:
    return Theme(
        name=name,
        primary=colors.accent,
        secondary=colors.muted,
        accent=colors.accent,
        foreground=colors.foreground,
        background=colors.background,
        surface=colors.surface,
        panel=colors.panel,
        success=colors.sev_ok,
        warning=colors.sev_warn,
        error=colors.sev_crit,
        dark=colors.dark,
        variables={
            # Footer keys pick up the accent instead of the default blue.
            "footer-key-foreground": colors.accent,
            "block-cursor-background": colors.panel,
            "block-cursor-foreground": colors.foreground,
            "block-cursor-text-style": "none",
        },
    )


# Registry consulted by app.py (register_theme + the command palette's theme
# picker). THEME_NAMES/DEFAULT_THEME/theme_colors are re-exported from
# palette.py so callers only need one import.
THEMES: dict[str, Theme] = {
    name: _textual_theme(name, colors) for name, colors in THEME_COLORS.items()
}
THEME_NAMES: tuple[str, ...] = tuple(THEMES)

CSWAP_DARK = THEMES["cswap-dark"]


def current_theme_colors() -> ThemeColors:
    """``ThemeColors`` for the currently running app's active theme.

    Outside a running app (e.g. a unit test calling a widgets.py/autoview.py
    render function directly) there is no active theme, so this falls back
    to cswap-dark's colors — the same as today's fixed behavior.
    """
    app = active_app.get(None)
    if app is None:
        return theme_colors(DEFAULT_THEME)
    return theme_colors(app.theme)


def severity_color(pct: float | None) -> str:
    """Bar/percentage color for a utilization percentage, in the currently
    active theme."""
    colors = current_theme_colors()
    if pct is None:
        return colors.muted
    if pct >= CRIT_PCT:
        return colors.sev_crit
    if pct >= WARN_PCT:
        return colors.sev_warn
    return colors.sev_ok
