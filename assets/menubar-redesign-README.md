# Claude Swap menu bar — design handoff

Status: design assets prepared for a future release; this package does not
implement or activate the new interface. All handoff files live in this folder.

## Files

| File | Purpose |
| --- | --- |
| [untitled.pen](untitled.pen) | Editable Pen source; authoritative visual reference with all ten artboards. Open in Pen. |
| [1 — Main DARK.png](1%20%E2%80%94%20Main%20DARK.png) | Actual 360 × 560 export of the main dark artboard. |
| [menubar-redesign-spec.md](menubar-redesign-spec.md) | Layout, state rules, accessibility requirements, and integration notes. |
| [menubar-redesign-tokens.css](menubar-redesign-tokens.css) | Hand-authored CSS variable seed for both themes; not an exact automated Pen export. |

The existing `menubar-panel-dark.png` and `menubar-panel-light.png` depict the
previous interface. Use the Pen file and the export above for this redesign.
Only the main dark artboard has a standalone PNG; all other screens remain
editable in the Pen source.

## Artboards inside Pen

1. Main DARK — active account, quota usage, actions, automatic switching.
2. Main LIGHT — matching light theme.
3. Personal preview — selected account distinct from the active account.
4. Stale / offline — retain measured values and explain freshness.
5. No accounts — login and token setup entry points.
6. Add token sheet — concealed token, optional email, cancel and submit.
7. Remove account — explicit confirmation and identity.
8. Tokens & interaction notes — component and state guidance, including appearance.
9. Settings DARK — Appearance selector with Dark selected.
10. Settings LIGHT — Appearance selector with Light selected.

## Next-version implementation

Start by inspecting the current code and the specification; some integration
issues identified in the initial review may already have been fixed.

1. Open the eight artboards in Pen. Use the main export for actual-size visual comparison.
2. Adapt the token seed and layout into `../src/claude_swap/menubar/web/panel.css`
   and `index.html`, keeping the 360 × 560 native panel and scrollable body.
3. Implement selection, explicit switching, disclosures, sheets, focus behavior,
   and pending/error states in `panel.js`. Reuse the existing bridge actions.
4. Reconcile status and freshness data in `viewmodel.py` and `bridge.py` only as
   needed. Preserve the existing credential, polling, and auto-switch contracts.
5. Validate both themes in the actual WKWebView, including long identities,
   many accounts, missing/stale data, keyboard navigation, reduced motion,
   and action success/failure. Confirm contrast against the final rendered colors.
6. Run the menu bar tests and full Python suite before releasing the implementation.

Reuse the existing icon system or implement matching vector icons in the app;
this handoff does not include separately exported icons or licensed font files.
No version bump, application CSS import, or runtime behavior change is included.
