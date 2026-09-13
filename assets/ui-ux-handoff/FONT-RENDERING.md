# Release board font rendering

The product now bundles Inter variable and IBM Plex Mono400/600 WOFF2 files.
`panel.css` uses @font-face from these local files and overrides host copies.
Source and license provenance: [font sources](../../src/claude_swap/menubar/web/fonts/SOURCES.md).

Pen's supported font property selects a family name; its current design API
provides no WOFF2/file binding. This was checked through Pen's design agent.
These exports use Inter and IBM Plex Mono by name with the intended weights.
**They cannot certify the exact bundled font bytes were used by Pen.** Matching
family/weight is distinct from binding the app's specific binary font sources.

For an exact bundled-font runtime capture, use the existing app capture pipeline
in [the completion report](next-wave/COMPLETION-REPORT.md). Its WebKit rendering
is separate from Pen, and glyph rasterization may differ. Do not relabel a
runtime screenshot as a Pen export or assert pixel identity from family names.
The coding agent should verify local font loads and refresh capture baselines
after incorporating the re-exported trigger/chip/crosshair boards.

Verified local font fingerprints:

- `src/claude_swap/menubar/web/fonts/ibm-plex-mono-400.woff2`: SHA-256 `c36f509c0a8f9f85f29cb44bc8701d8a9e0b14c499e77a884f789ead7093a7ac` (10052 bytes).
- `src/claude_swap/menubar/web/fonts/ibm-plex-mono-600.woff2`: SHA-256 `ad4580d8cb4b5f627c2d18457656732f7f7b070f7837fbc380e08054157e6f6c` (10120 bytes).
- `src/claude_swap/menubar/web/fonts/inter-var.woff2`: SHA-256 `c940764593d0fe5d596be327ca7558855e018039fb78509aa21921fd3644c3e4` (48432 bytes).
