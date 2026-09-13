# Next wave: reset timelines

**Ready for implementation planning; not implemented.** Prepared against
`9d0467192257efd553ed4d462457e6afdbd16729` on 2026-09-12. Recheck HEAD before work.

Add a calendar button to Claude Code Swap's main header. It opens an adjacent
panel with separate full-window Session and Weekly Gantt charts for every
account, quota-used fills and a shared Now marker. Preserve the implemented
compact Account/Usage/alias/Settings UI.

Read in order:

1. [Coding-agent brief](CODING-AGENT.md) — scope and completion expectations.
2. [Design specification](../TIMELINES.md) and [final state board](../timeline-screens/20-timeline-states.png).
3. [Implementation plan](tasks/plan.md) and [task checklist](tasks/todo.md).
4. [Data contract and geometry](DATA-CONTRACT.md), [fixtures](fixtures/timelines.json), and [QA matrix](QA.md).
5. [Implemented baseline](../IMPLEMENTATION-NOTES.md) and [baseline handoff](../HANDOFF.md).

Visual assets are already present in the same handoff package:

| Reference | Purpose |
| --- | --- |
| [Pen source](../../untitled.pen) | Editable boards 01–20; access through Pen tooling only |
| [16 closed](../timeline-screens/16-timelines-closed.png) | New calendar trigger at rest |
| [17 dark](../timeline-screens/17-timelines-right-dark.png) | 360px main + 8px gap + 600px companion |
| [18 light](../timeline-screens/18-timelines-right-light.png) | Both panels follow one appearance setting |
| [19 left](../timeline-screens/19-timelines-left.png) | Placement near the right display edge; annotation is not UI |
| [20 states](../timeline-screens/20-timeline-states.png) | Detail, loading, empty, missing/stale/elapsed, scrolling, narrow mode |
| [Logo assets](../icons/) | Existing Interlock vectors/native assets; reuse implemented icons |
| [Token seed](../tokens.css) | Reference only; shipped panel.css is the baseline |

No new logo, font, bitmap chart or chart-library dependency is necessary.
Render windows, quota fills, endpoint ticks and Now lines as accessible DOM/SVG
using the existing utility-icon system. Reproduce the calendar/reset icon in
that system if absent. Do not load screenshot images as the interface.

The existing repository `tasks/` tracks the preceding wave and retains open
checkpoint markers. It is intentionally untouched. This wave's checklist is
`next-wave/tasks/todo.md`, kept with the design package.
