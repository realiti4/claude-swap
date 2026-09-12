# Claude Swap menu bar icon

Status: designed in Pen and visually reviewed. Recommended mark: A — Interlock.
Source design: [untitled.pen](untitled.pen).

## Direction

A compact S-shaped mark formed by two opposing interlocking arrows. It should
communicate account switching with a recognizable silhouette, clear arrowheads,
softly squared turns, and balanced negative space. Use editable vector geometry.

The menu bar glyph is monochrome on transparency, without a square background,
lettering, gradients, shadows, or decorative sparkles. Evaluate three candidates
and choose a recommended final based on legibility at 16, 18, and 22 logical pixels.

## Design deliverables

- A named “11 — Swap icon system” board in the existing Pen document.
- Three candidates with a prominently displayed recommended final.
- Actual-size tests on light and dark menu bar strips.
- Enlarged black and white versions, plus teal panel branding.
- Named transparent export frames: 18 × 18, 36 × 36 (@2x), and a vector master.
- Optional larger rounded-square app-icon treatment using the same glyph.

## Implementation handoff

Use the monochrome asset as an NSImage template so macOS controls menu bar
contrast. Keep teal (#3FC2B2 on graphite, #0C8577 on ivory) for panel branding.
Preserve aspect ratio and optical padding. The current shell already marks its
SF Symbol image as a template; a future implementation can replace that image
without changing status-item behavior.

The design is saved on board 11. The [presentation preview](menubar-icon-system.png)
is exported alongside it. Transparent 18px, 36px and 256px master export frames
remain editable inside Pen; they have not yet been exported as standalone assets.
Existing UI artboards and application code remain outside this icon task.
