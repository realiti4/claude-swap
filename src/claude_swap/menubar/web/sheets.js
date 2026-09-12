/* claude-swap sheets — native <dialog> management for the redesigned panel.
 *
 * Populated in the sheets task: token entry (concealed input, field errors,
 * cleared on close), remove confirmation, and the switch-history activity
 * list. Dialog semantics (focus trap, Esc, restore) come from the platform;
 * a class-based fallback covers engines without showModal.
 */
"use strict";

window.CSWAP_SHEETS = { /* filled in the sheets task */ };
