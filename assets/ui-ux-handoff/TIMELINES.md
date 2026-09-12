# Reset timelines — design extension

Requested: a main-panel calendar/Gantt button opening a side panel with two
separate charts for each account's current-session and weekly reset times.
Design work is in the existing [Pen source](../untitled.pen). This extension
requires implementation; it is not part of the running application's behavior.

## Design reference and exports

The full-window revision is saved in Pen on artboards 17–19. Artboard 16
remains the closed state. **Artboard 20 still shows the previous remaining-time
model**: Pen reached its usage limit before updating it. Use its focus, motion
and placement guidance only; its charts and old bar-meaning notes are superseded
by this document and artboards 17–19. Its full-window row detail, narrow and
many-account examples remain pending. Also align the active dot with the main
account status during final polish (the new chart fixture dots research while
the main panel still labels work active). Selected-row styling is independent.

Available references:

- [16 — Timelines closed](timeline-screens/16-timelines-closed.png)
- [17 — Timelines right DARK](timeline-screens/17-timelines-right-dark.png)
- [18 — Timelines right LIGHT](timeline-screens/18-timelines-right-light.png)
- [19 — Timelines left (screen edge)](timeline-screens/19-timelines-left.png)
- [20 — Timeline states & interactions](timeline-screens/20-timeline-states.png)

These supersede the earlier timeline draft. The original 15 screen exports
remain the baseline for the preceding redesign; use this extension for the
new header trigger and timeline behavior. Both panels share the appearance
setting. Main switching and auto-switch controls remain below the scroll fold.

Fixture: Now is 12 September 2026 13:30 America/Los_Angeles. Session offsets
30m, 1h45m, 3h and 4h30m end at 14:00, 15:15, 16:30 and 18:00 that day.
Weekly offsets 1d12h, 3d, 6d and 6h end at 14 Sep 01:30, 15 Sep 13:30,
18 Sep 13:30 and 12 Sep 19:30. Relative axis labels avoid date crowding;
exact dates remain in rows/details. Bars now show full windows with quota-used fill and a shared Now marker.

The screenshots are static design references, not proof of runtime behavior.
The interaction board specifies 140ms slide, instant reduced-motion changes,
combined outside-click dismissal, focus return and independent row scrolling.

## Interaction and layout

The Reset timelines button sits in the main header and indicates its open state.
Opening it reveals an adjacent, top-aligned companion panel: main 360 × 560,
companion approximately 600 × 560, 8px gap. Prefer the right side when it fits;
otherwise use the left. Keep the main panel anchored and both panels inside
the visible screen. On displays where neither side fits, show a temporary
in-panel timeline view with Back rather than positioning content offscreen.

The companion includes its own close button. Toggling the header button closes
it; Escape closes the companion first and restores focus to the main trigger.
Clicking within either panel must not accidentally dismiss the pair. Define
outside-click behavior for the combined interaction region. Use a short slide
transition toward the available side; reduced-motion mode switches instantly.
This toggle opens a view, not an account-switching or auto-switch setting.

## Two separate charts

- **Session resets · 5-hour**: shared axis from Now − 5h to Now + 5h.
- **Weekly resets · 7-day**: shared axis from Now − 7d to Now + 7d.

One row per account, stable Account Index and alias on the left. Use the same
account ordering in both charts. Distinguish active account from selected row.
Selecting a row may inspect its detail; it must not activate that account.

Each neutral bar represents the full current window from its start to reset.
All 5-hour bars have the same duration/width; all 7-day bars likewise. Their
horizontal positions differ by account. A distinct vertical dashed Now line
crosses all rows at the current timestamp. A colored fill within each window
represents the percentage of quota consumed, accompanied by an explicit `% used`
label. Its endpoint is not a timestamp or usage-history event. The legend must
explain these two encodings: bar position is time; fill is quota used.

Use the existing measurement's `windows[].resetsAt` and usage percentage for
kinds `5h` and `7d`. Prefer a measured start if available. Otherwise derive a
nominal visual start as reset minus 5 hours or 7 × 24 hours; mark it inferred in
accessible detail and do not claim it records actual activity start. Show exact
start/reset timestamps, percentage and countdown in keyboard-accessible detail.
Do not invent recurring future windows. Missing reset means no positioned bar;
known reset with missing usage may show an unfilled neutral window labeled
Usage unavailable, never a false 0%. API-key/no-window accounts show text status.

Stale data remains visibly Last known with muted/dashed styling and last-known
usage. Passed reset times say Awaiting updated usage until refreshed; do not
roll forward a cycle. Handle out-of-range values explicitly instead of silently
making them look like normal current windows. The fixture usage percentages are
session 68/32/90/12 and weekly 41/64/18/83 for work/research/backup/sandbox.

Current time advances locally. Date/time labels use the user's timezone; epoch
math handles DST and midnight crossings. Keep exact reset details available by
keyboard/focus as well as hover. Avoid tick labels that overlap. Scroll account
rows for many accounts while retaining chart titles and useful axis context.

## State coverage

Provide normal dark/light layouts, left-side placement, loading without false
values, empty roster, missing/reset-unavailable data, API-key accounts, stale
and elapsed reset states, long aliases, many accounts, and a narrow-screen
fallback. Keep both chart types distinct even if one has no data.

## Native integration implications

The current shell creates a transient NSPopover at 360 × 560. A DOM slide alone
cannot display outside those native bounds. Implementation must coordinate an
expanded native container or companion NSPanel, screen-edge placement, focus,
and dismissal. Validate this in real AppKit/WKWebView; do not rely solely on a
wide browser mockup. This design does not choose an untested native API strategy.
Reuse paced snapshot data and avoid extra API polling to populate the charts.

## Acceptance checks for implementation

- Button opens/closes the companion on either side according to available space.
- Both independent charts show every account's correct known reset time.
- Full-window positions, equal duration widths, Now markers, countdowns, exact
  dates and timezone agree, including DST/day boundaries.
- Used-percentage fills and labels agree with the measurements and main Usage
  card; elapsed time never substitutes for quota used.
- Missing, stale, expired and out-of-range values do not fabricate fresh resets.
- Main controls remain usable; focus, Escape and outside-click behavior work
  across the combined panel region, including multiple monitors.
- Many-account scrolling and narrow-screen fallback retain all account rows.
- Keyboard access, reduced motion and dark/light contrast are verified.
- No account activation, credential writes or polling-policy changes occur.
