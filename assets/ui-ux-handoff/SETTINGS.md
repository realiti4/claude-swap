# Settings companion — next design wave

Status: design reference completed, saved and exported; not implemented.
Boards21–28 define this Settings wave. The prior boards01–20 remain preserved.
The canvas retains four labeled nonoverlapping areas; see
[CANVAS-MAP.md](CANVAS-MAP.md) for navigation.

## Exported references

- [21-main-active-highlight](settings-screens/21-main-active-highlight.png)
- [22-settings-appearance-dark](settings-screens/22-settings-appearance-dark.png)
- [23-settings-menu-bar-dark](settings-screens/23-settings-menu-bar-dark.png)
- [24-settings-refresh-dark](settings-screens/24-settings-refresh-dark.png)
- [25-settings-auto-switch-dark](settings-screens/25-settings-auto-switch-dark.png)
- [26-settings-appearance-light](settings-screens/26-settings-appearance-light.png)
- [27-settings-left](settings-screens/27-settings-left.png)
- [28-settings-states](settings-screens/28-settings-states.png)

Final review corrected the Light selection, restored calendar access beside
Refresh/Settings, and expanded board28 into visual examples for custom
threshold/model filtering, pending/failed writes, auto-off, narrow category
navigation, focus and active-versus-selected identity. Numeric fields commit
on Enter/blur; no separate Save button is implied. Model names in the checklist
are illustrative: populate from actual supported/observed models and retain
existing custom filter values rather than hardcoding the screenshot labels.
In narrow category detail, Back returns to the list where Quit remains available.

Static images specify intended behavior; runtime persistence, accessibility,
native placement and quit cleanup remain implementation acceptance work.

## Main panel and companion

The gear opens Settings in a600×560 companion beside the360×560 main panel,
with8px gap and top alignment. Prefer right, flip left where needed; otherwise
use the in-panel fallback. Settings and timelines occupy the same companion
slot: opening one replaces the other, never creates a third window. Gear and
calendar open states reflect the actual visible content. Close/Escape returns
to the main panel; closing the companion does not quit the app. Use140ms slide
and instant Reduced Motion changes. Do not animate width when changing category.

Move persistent Settings controls into Settings. Main keeps operational actions
(refresh, switch, Best, Rotate, account management) and a read-only Auto-switch
status summary with Configure linking directly to that category. Its toggle
moves into Settings. Account alias editing remains contextual account management.

## Category navigation

Use a150px sidebar and450px content region. Sidebar and header remain pinned;
category content scrolls independently. Select category on click or keyboard
activation; expose selected semantics and an accessible content heading. Keep
the last category for the current session; first open defaults to Appearance.
In narrow mode, show a category list followed by category detail with Back;
retain a clear route to Accounts and a visible Quit action.

| Category | Controls |
| --- | --- |
| Appearance | System / Light / Dark; follows current stored appearance |
| Menu Bar | Show account name; Title percentage Off /5-hour /Weekly /Both; Show model limits in title; illustrative title preview |
| Refresh | Background usage refresh30s /60s /5min; distinguish from auto-switch check interval |
| Auto-switch | Enabled; threshold80/90/95/98% presets plus custom50–99.9%; strategy Most headroom /Soonest weekly reset; Advanced disclosure |

Advanced uses existing core settings, not new invented preferences:

| Field | Existing setting | Range/current default |
| --- | --- | --- |
| Check interval | autoswitch.intervalSeconds |15–3600s;60s |
| Cooldown | autoswitch.cooldownSeconds |0–86400s;300s |
| Minimum headroom advantage | autoswitch.hysteresisPct |0–50 percentage points;10 |
| Include API-key accounts | autoswitch.includeApiKeyAccounts |Boolean; false; explain paid per-token usage |
| Unhealthy checks | autoswitch.unhealthyTicks |Integer1–100;3 |
| Model filter | autoswitch.model |Existing model-name list or all semantics; preserve validator |

Do not include terminal UI theme, launch-at-login or notification preferences
without a separate product decision. The two strategy values remain `best` and
`consume-first`, respectively. Display actual saved values rather than treating
example defaults as new values to write.

## Save and error behavior

Re-read stored preferences on open. Discrete selections save immediately with
pending and failure states; apply numeric/text fields on explicit commit (Enter
or blur), not each keystroke. Invalid values retain input and show inline error.
A failed write restores the displayed committed selection while retaining an
editable failed draft where applicable. Do not claim success before reply.
Switching category must not silently discard invalid/uncommitted input; retain
it within the Settings session. Closing with uncommitted input discards only
that draft and never writes invalid values. Auto-switch off preserves policy
values; show policy controls disabled with explanatory copy. Future implementation
may choose editable-while-off only after updating this specification and boards.

Existing appearance rollback and atomic persistence semantics remain in force.
Settings and main/timeline views must reflect external/native-menu changes.

## Active-account emphasis

Active comes from `account.active`, never selectedSlot. Use persistent teal-tinted
surface plus a solid leading marker/check and the explicit Active status word.
A different selected account uses an outline and teal index, with Preview in its
Account card. Selecting research2 must leave work1 visibly active; only an
explicit successful switch moves the active highlight. Disabled accounts retain
dimmed struck-through aliases. Use accessible names containing full alias and
active/selected state; color alone is insufficient. Do not add two conflicting
Active markers when active is also selected.

## Quit

Persistent sidebar footer action: **Quit**, with power icon. Keep the visible
label short and on one line; an accessible label may say Quit Claude Code Swap.
This exits the menu bar app and its hosted auto-switch engine; it does not log
out accounts, delete credentials, kill Claude Code sessions, or uninstall the
launch agent. No confirmation is needed for ordinary Quit. Distinguish it from
the companion's Close button. If a setting write is pending, finish or fail the
in-flight operation before quitting, and prevent duplicate Quit submission.
Show a truthful error if quit dispatch fails; never display a fake success toast.
Keep the existing native-menu Quit path available. Verify successful quit does
not immediately relaunch under the installed launch-agent policy.

## Engineering implications (future implementation)

Current `getPrefs/setPrefs` exposes theme,refreshInterval,titlePct only. The
native menu also has show_account_name/title_scoped and threshold controls.
Expose those through validated additive preference handlers and replies; route
core auto-switch policy through `settings.py` validation and persistence rather
than copying it into menubar_settings.json. Existing `quit` bridge action and
`on_quit` already exist; reuse and verify cleanup rather than inventing a second
termination mechanism. Settings and timeline native placement/focus should share
one host/lifecycle contract. Do not implement a second uncoordinated popover.

## Acceptance for this design wave

- All existing menu bar and relevant auto-switch settings have one categorized home.
- Main stays360×560 with operational controls visible; Settings and timeline never compete.
- Active versus selected remains obvious in dark/light and with long aliases.
- Full settings content, advanced controls, pending/errors and narrow navigation are designed.
- Quit is discoverable and unambiguously distinct from Close or account removal.
- Product implementation and preceding timeline design remain explicitly separate from this wave.
