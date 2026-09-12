# Coding agent entry point

Implement the final **Claude Code Swap** menu bar UI using [HANDOFF.md](HANDOFF.md).
The saved [Pen source](../untitled.pen), all 15 final screen exports, exact SVG
logo paths, native template PDF/PNGs, and token seed accompany it.

Start by inspecting current code and git status. Preserve unrelated work and
reuse the existing account, alias and theme logic. The handoff specifies the
remaining changes, boundaries, and acceptance tests. Older images elsewhere
in `assets/` are historical; use `screens/` here for the current design.

## Reset timeline extension

See [TIMELINES.md](TIMELINES.md) and `timeline-screens/` for the new calendar
trigger, side-by-side session/weekly Gantt charts, dark/light and left placement,
and interaction states. Artboards 16–20 extend the original 15-screen baseline.
Implement the behavior and native-window acceptance checks in that addendum.

The latest timeline revision shows full 5-hour/7-day windows, used-percent fill
and a shared Now line in artboards 17–19. Artboard 20 still needs this revision;
read the status in TIMELINES.md before using its chart examples.
