/* claude-swap icons — tiny currentColor SVG set for the redesigned panel.
 * No icon exports ship with the Pen handoff; this is the matching vector
 * language (stroke-based, 24-grid, 1.8 stroke). Usage: icon("refresh", 13)
 * returns an SVG string; every icon is decorative by default (aria-hidden)
 * — buttons carry their accessible names separately.
 */
"use strict";

const ICON_PATHS = {
  swap: '<path d="M4 7h13l-3.5-3.5"/><path d="M20 17H7l3.5 3.5"/>',
  refresh:
    '<polyline points="23 4 23 10 17 10"/>' +
    '<polyline points="1 20 1 14 7 14"/>' +
    '<path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>',
  gear:
    '<circle cx="12" cy="12" r="3.2"/>' +
    '<path d="M19.4 15a1.7 1.7 0 0 0 .34 1.87l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.7 1.7 0 0 0-1.87-.34 1.7 1.7 0 0 0-1 1.56V21a2 2 0 1 1-4 0v-.09a1.7 1.7 0 0 0-1.11-1.56 1.7 1.7 0 0 0-1.87.34l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.7 1.7 0 0 0 .34-1.87 1.7 1.7 0 0 0-1.56-1H3a2 2 0 1 1 0-4h.09a1.7 1.7 0 0 0 1.56-1.11 1.7 1.7 0 0 0-.34-1.87l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.7 1.7 0 0 0 1.87.34h.09a1.7 1.7 0 0 0 1-1.56V3a2 2 0 1 1 4 0v.09a1.7 1.7 0 0 0 1 1.56 1.7 1.7 0 0 0 1.87-.34l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.7 1.7 0 0 0-.34 1.87v.09a1.7 1.7 0 0 0 1.56 1H21a2 2 0 1 1 0 4h-.09a1.7 1.7 0 0 0-1.51 1z"/>',
  best: '<polyline points="12 4 12 14"/>' +
    '<polyline points="7.5 9.5 12 4.5 16.5 9.5"/>' +
    '<path d="M5 20h14"/>',
  rotate:
    '<path d="M21 12a9 9 0 1 1-2.64-6.36"/>' +
    '<polyline points="21 3 21 9 15 9"/>',
  chevron: '<polyline points="9 6 15 12 9 18"/>',
  activity: '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>',
  plus: '<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>',
  check: '<polyline points="20 6 9 17 4 12"/>',
  warn: '<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
};

function icon(name, size = 14, cls = "") {
  const body = ICON_PATHS[name];
  if (!body) return "";
  size = Number(size) || 14;  // sizes interpolate into markup: numeric only
  cls = String(cls).replace(/[^a-z0-9 _-]/gi, "");
  return (
    `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" ` +
    `stroke="currentColor" stroke-width="1.8" stroke-linecap="round" ` +
    `stroke-linejoin="round" aria-hidden="true"${cls ? ` class="${cls}"` : ""}>` +
    body +
    "</svg>"
  );
}

window.CSWAP_ICONS = { icon, ICON_PATHS };
