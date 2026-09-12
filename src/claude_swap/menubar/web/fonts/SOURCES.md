# Bundled fonts — provenance

Pixel-fidelity mandate: the design boards render in **Inter** and
**IBM Plex Mono** (declared in `assets/untitled.pen`; weight 600 explicit,
400 the default). Neither ships with macOS, so latin-subset WOFF2 files are
bundled here and `@font-face` in `panel.css` shadows any host-installed
copies — every machine draws the boards' glyphs.

Both families are SIL Open Font License 1.1; bundling and redistribution
is permitted.

Fetched 2026-09-12 from Google Fonts (content-hashed, immutable URLs):

| File | Source | SHA-256 |
| --- | --- | --- |
| `inter-var.woff2` | `https://fonts.gstatic.com/s/inter/v20/UcC73FwrK3iLTeHuS_nVMrMxCp50SjIa1ZL7W0Q5nw.woff2` | `c940764593d0fe5d596be327ca7558855e018039fb78509aa21921fd3644c3e4` |
| `ibm-plex-mono-400.woff2` | `https://fonts.gstatic.com/s/ibmplexmono/v20/-F63fjptAgt5VM-kVkqdyU8n1i8q131nj-o.woff2` | `c36f509c0a8f9f85f29cb44bc8701d8a9e0b14c499e77a884f789ead7093a7ac` |
| `ibm-plex-mono-600.woff2` | `https://fonts.gstatic.com/s/ibmplexmono/v20/-F6qfjptAgt5VM-kVkqdyU8n3vAOwlBFgsAXHNk.woff2` | `ad4580d8cb4b5f627c2d18457656732f7f7b070f7837fbc380e08054157e6f6c` |

Notes:

- Google Fonts serves Inter as one variable font (identical URL for every
  weight); the single file covers the 400/600 the boards use via
  `font-weight: 100 900`. IBM Plex Mono ships per-weight statics.
- Subsets are `latin` (U+0000-00FF plus punctuation/diacritics). The panel's
  UI strings are ASCII; aliases are user data and may fall back to the
  system stack per-glyph — acceptable for non-board content.
- To update: re-fetch from Google Fonts, replace files, update this table,
  and re-run `tests/test_menubar_package.py::TestBundledFonts`.
