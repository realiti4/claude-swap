"""Theme color data, with zero third-party dependencies.

Single source of truth for every theme's hex palette. Kept dependency-free
(no textual, no rich) so it can be imported from printer.py and settings.py —
both on the hot path for plain CLI commands like ``cswap list`` — without
paying for textual's import cost. ``claude_swap.tui.theme`` builds the actual
Textual ``Theme`` objects (for the TUI's own CSS design tokens) from this
same data, so the TUI and the plain CLI can never disagree on what a theme's
colors are.
"""

from __future__ import annotations

from dataclasses import dataclass

# cswap-dark's own palette. A subtle modern dark theme: neutral charcoal
# backgrounds in the VS Code register, one warm terracotta accent (the same
# xterm-173 tone printer.py has always used for the CLI — a deliberate nod to
# Claude Code's orange, used sparingly), and desaturated severity colors so
# usage bars read calmly on a dark background. Deliberately *not* a wholesale
# copy of any other tool's palette.
ACCENT = "#d7875f"  # warm terracotta, xterm 173
FOREGROUND = "#e8e4de"  # soft, slightly warm off-white
MUTED = "#8a8a8a"  # secondary text
BACKGROUND = "#141414"
SURFACE = "#1e1e1e"
PANEL = "#262626"

# Usage severity ramp (desaturated for dark backgrounds).
SEV_OK = "#87af87"  # calm green: plenty of headroom
SEV_WARN = "#d7af5f"  # amber: climbing (>= 70%)
SEV_CRIT = "#d75f5f"  # soft red: near the limit (>= 90%)
TRACK = "#3a3a3a"  # unfilled bar track

# Severity band edges. WARN mirrors where a user starts caring; CRIT mirrors
# the auto-switch default threshold so bar color and switch behavior agree.
WARN_PCT = 70.0
CRIT_PCT = 90.0


@dataclass(frozen=True)
class ThemeColors:
    """One theme's resolved colors, for both Rich-style rendering (TUI) and
    24-bit truecolor ANSI escapes (plain CLI, see printer.py)."""

    accent: str
    foreground: str
    muted: str
    background: str
    surface: str
    panel: str
    track: str
    sev_ok: str
    sev_warn: str
    sev_crit: str
    dark: bool = True


CSWAP_DARK_COLORS = ThemeColors(
    accent=ACCENT,
    foreground=FOREGROUND,
    muted=MUTED,
    background=BACKGROUND,
    surface=SURFACE,
    panel=PANEL,
    track=TRACK,
    sev_ok=SEV_OK,
    sev_warn=SEV_WARN,
    sev_crit=SEV_CRIT,
)


def _catppuccin(
    *,
    base: str,
    text: str,
    surface0: str,
    surface1: str,
    overlay1: str,
    green: str,
    yellow: str,
    red: str,
    peach: str,
    dark: bool = True,
) -> ThemeColors:
    """One Catppuccin flavor's ``ThemeColors``.

    Token mapping shared by all four flavors: surface0 -> surface/track,
    surface1 -> panel, overlay1 -> muted, peach -> accent, green/yellow/red
    -> the OK/WARN/CRIT severity ramp.
    """
    return ThemeColors(
        accent=peach,
        foreground=text,
        muted=overlay1,
        background=base,
        surface=surface0,
        panel=surface1,
        track=surface0,
        sev_ok=green,
        sev_warn=yellow,
        sev_crit=red,
        dark=dark,
    )


# Hex values cross-checked against the official Catppuccin palette
# (catppuccin/palette, palette.json) and, for Mocha, against this rice's own
# plymouth-themes/tools/catppuccin-recolor.sh.
LATTE_COLORS = _catppuccin(
    base="#eff1f5", text="#4c4f69", surface0="#ccd0da", surface1="#bcc0cc",
    overlay1="#8c8fa1", green="#40a02b", yellow="#df8e1d", red="#d20f39",
    peach="#fe640b", dark=False,
)
FRAPPE_COLORS = _catppuccin(
    base="#303446", text="#c6d0f5", surface0="#414559", surface1="#51576d",
    overlay1="#838ba7", green="#a6d189", yellow="#e5c890", red="#e78284",
    peach="#ef9f76",
)
MACCHIATO_COLORS = _catppuccin(
    base="#24273a", text="#cad3f5", surface0="#363a4f", surface1="#494d64",
    overlay1="#8087a2", green="#a6da95", yellow="#eed49f", red="#ed8796",
    peach="#f5a97f",
)
MOCHA_COLORS = _catppuccin(
    base="#1e1e2e", text="#cdd6f4", surface0="#313244", surface1="#45475a",
    overlay1="#7f849c", green="#a6e3a1", yellow="#f9e2af", red="#f38ba8",
    peach="#fab387",
)

DEFAULT_THEME = "cswap-dark"

THEME_COLORS: dict[str, ThemeColors] = {
    "cswap-dark": CSWAP_DARK_COLORS,
    "catppuccin-latte": LATTE_COLORS,
    "catppuccin-frappe": FRAPPE_COLORS,
    "catppuccin-macchiato": MACCHIATO_COLORS,
    "catppuccin-mocha": MOCHA_COLORS,
}
THEME_NAMES: tuple[str, ...] = tuple(THEME_COLORS)


def theme_colors(name: str) -> ThemeColors:
    """``ThemeColors`` for a theme name; unregistered names fall back to
    cswap-dark's."""
    return THEME_COLORS.get(name, CSWAP_DARK_COLORS)


def severity_for(pct: float | None, colors: ThemeColors) -> str:
    """Bar/percentage color for a utilization percentage, in ``colors``."""
    if pct is None:
        return colors.muted
    if pct >= CRIT_PCT:
        return colors.sev_crit
    if pct >= WARN_PCT:
        return colors.sev_warn
    return colors.sev_ok
