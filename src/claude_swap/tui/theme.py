"""The "cswap-dark" Textual theme and shared color constants.

A subtle modern dark theme: neutral charcoal backgrounds in the VS Code
register, one warm terracotta accent (the same xterm-173 tone printer.py has
always used for the CLI — a deliberate nod to Claude Code's orange, used
sparingly), and desaturated severity colors so usage bars read calmly on a
dark background. Deliberately *not* a wholesale copy of any other tool's
palette.
"""

from __future__ import annotations

from textual.theme import Theme

# Core palette (single source of truth — widgets import these for rich
# renderables, the Theme below maps them onto Textual's design tokens).
ACCENT = "#d7875f"  # warm terracotta, xterm 173 — matches printer._ACCENT
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


def severity_color(pct: float | None) -> str:
    """Bar/percentage color for a utilization percentage."""
    if pct is None:
        return MUTED
    if pct >= CRIT_PCT:
        return SEV_CRIT
    if pct >= WARN_PCT:
        return SEV_WARN
    return SEV_OK


CSWAP_DARK = Theme(
    name="cswap-dark",
    primary=ACCENT,
    secondary=MUTED,
    accent=ACCENT,
    foreground=FOREGROUND,
    background=BACKGROUND,
    surface=SURFACE,
    panel=PANEL,
    success=SEV_OK,
    warning=SEV_WARN,
    error=SEV_CRIT,
    dark=True,
    variables={
        # Footer keys pick up the accent instead of the default blue.
        "footer-key-foreground": ACCENT,
        "block-cursor-background": PANEL,
        "block-cursor-foreground": FOREGROUND,
        "block-cursor-text-style": "none",
    },
)
