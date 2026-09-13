# Coding-agent brief

Implement the reset-timeline extension described by this directory and
`../TIMELINES.md`, using the existing Python/AppKit/WKWebView shell and vanilla
JS/CSS. Start with git status and the current source: baseline alias editing,
Settings segments, Interlock branding and compact account layout already exist.
Do not reimplement or revert them. Preserve unrelated edits, including any
lockfile changes or audit work from other tasks.

Follow `tasks/plan.md` and `tasks/todo.md`. Resolve the native presentation
strategy with a small working AppKit proof before investing in full chart UI.
A wide browser mockup does not prove that the current transient NSPopover can
support a side panel. Record the chosen strategy and validation evidence.

Implement both full-window charts with real epoch geometry, quota percentages,
stable account indexes/aliases, Now lines, full details and honest data states.
Use `DATA-CONTRACT.md` to address information missing from the current view
model; do not silently omit designed states or fabricate zero usage/resets.
The JSON fixtures are deterministic design-test inputs, not production API
responses and not a replacement for live snapshot adaptation.

Keep selection distinct from activation. Opening/closing charts and inspecting
rows must not switch accounts, alter credentials, change polling policy or
create new preference writes. Reuse one snapshot and one local clock across
both panels. Alias changes, active-account changes and appearance changes must
propagate to the open charts without extra usage requests.

Verify focused Python/JS tests, browser fixture states, package inclusion and
native multi-display behavior. Finish with before/after screenshots, exact
commands/results, native evidence and any remaining limitations. Update the
implementation notes, affected Pen boards/exports and manifest if deliberate
implementation decisions change the design. Keep planned/unverified states
explicit. Do not publish a release or change versions as part of this scope.
