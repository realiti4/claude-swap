/* Reset timelines — companion surface controller.
 *
 * Task-6 scaffold: owns the expanded/collapsed mode state driven by the
 * native `timelineLayout` pushes (right | left | in-panel + anchor
 * offset), renders the empty companion shell in the chosen mode, and
 * owns the `toggleTimelines` bridge action plus the two-stage Escape
 * hierarchy. Chart rendering (geometry, rows, bars, states) lands with
 * the chart-ui tasks; nothing here activates accounts, writes
 * credentials, or changes polling — expanding is a view action only.
 */
"use strict";

/* ---- pure geometry (DOM-free; node-testable) ----------------------------
 *
 * Implements next-wave/DATA-CONTRACT.md exactly: domain [N−D, N+D] with
 * D the FULL window duration (18000s / 604800s — also the half-span), so
 * a nominal window covers exactly half the plot, positioned by its reset
 * time; the quota fill is a QUANTITY (pct of the window), never a time
 * event; Now is pinned at the plot center. Epoch arithmetic uses fixed
 * durations — a DST-crossing 7-day window stays 168 hours. All outputs
 * are fractions of plot width so tests run without a DOM.
 */

const TL_SECONDS = { "5h": 18000, "7d": 604800 };

function layoutTimelineWindow(kind, resetsAt, pct, now) {
  const D = TL_SECONDS[kind];
  if (!(D > 0) || resetsAt == null || !Number.isFinite(resetsAt)) {
    return null; // no positioned bar (reset-unavailable / no-window)
  }
  const start = resetsAt - D; // nominal start; mark "inferred" in detail
  const left = (start - (now - D)) / (2 * D);
  const width = D / (2 * D); // nominal windows are exactly half the plot
  const drawLeft = Math.max(0, left);
  const drawRight = Math.min(1, left + width);
  const pctNum = pct == null || !Number.isFinite(pct) ? null : Number(pct);
  const fillPct = pctNum == null ? null : Math.min(pctNum, 100);
  return {
    leftFraction: left,
    widthFraction: width,
    drawLeftFraction: Math.min(drawLeft, drawRight),
    drawWidthFraction: Math.max(0, drawRight - drawLeft),
    fillFractionOfPlot: pctNum == null ? null : width * fillPct / 100,
    nowFraction: 0.5,
    offscale: left < -1e-9 || left + width > 1 + 1e-9,
    fillCapped: pctNum != null && pctNum > 100,
    inferredStart: start,
    windowSeconds: D,
  };
}

function axisTicks(kind) {
  if (kind === "5h") {
    return [
      { at: 0.1, label: "\u22124h" }, { at: 0.3, label: "\u22122h" },
      { at: 0.5, label: "Now", now: true },
      { at: 0.7, label: "+2h" }, { at: 0.9, label: "+4h" },
    ];
  }
  const s = 1 / 14, ticks = [
    { at: 1 * s, label: "\u22126d" }, { at: 4 * s, label: "\u22123d" },
    { at: 0.5, label: "Now", now: true },
    { at: 10 * s, label: "+3d" }, { at: 13 * s, label: "+6d" },
  ];
  return kind === "7d" ? ticks : null;
}

function countdownText(resetsAt, now) {
  if (resetsAt == null || !Number.isFinite(resetsAt)) return null;
  const remaining = Math.floor(resetsAt - now);
  if (remaining <= 0) return null;
  const days = Math.floor(remaining / 86400);
  const hours = Math.floor((remaining % 86400) / 3600);
  const minutes = Math.floor((remaining % 3600) / 60);
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

/* State derivation that tolerates an older producer: with the additive
 * timelineWindows contract the entry's state is authoritative; without
 * it, known rows derive from windows[] and everything unknown degrades
 * to unavailable — countdown strings are never parsed as data. */
function deriveWindowState(kind, account, now) {
  const tl = (account.timelineWindows || []).find((w) => w.kind === kind);
  if (tl) {
    return {
      state: tl.state,
      pct: tl.pct == null ? null : tl.pct,
      resetsAt: tl.resetsAt == null ? null : tl.resetsAt,
      startsAt: tl.startsAt == null ? null : tl.startsAt,
      observedAt: tl.observedAt == null ? null : tl.observedAt,
      degraded: false,
    };
  }
  if (account.quarantined) {
    return { state: "no-window", pct: null, resetsAt: null, startsAt: null,
             observedAt: null, degraded: true };
  }
  const w = (account.windows || []).find((x) => x.kind === kind);
  if (!w) {
    return { state: "unavailable", pct: null, resetsAt: null, startsAt: null,
             observedAt: null, degraded: true };
  }
  const resetsAt = w.resetsAt == null ? null : w.resetsAt;
  // windows[] drops resetsAt exactly when the reset has passed.
  const state = resetsAt == null
    ? "elapsed"
    : resetsAt <= now ? "elapsed"
    : w.state === "stale" ? "stale" : "ok";
  return { state, pct: w.pct, resetsAt, startsAt: null,
           observedAt: null, degraded: true };
}

/* Bars are drawn only for states with a positioned, meaningful window;
 * elapsed/no-window/reset-unavailable/unavailable render text instead. */
function shouldDrawBar(state) {
  return state === "ok" || state === "stale" || state === "usage-unavailable";
}

const TL_GEOMETRY = {
  TL_SECONDS,
  layoutTimelineWindow,
  axisTicks,
  countdownText,
  deriveWindowState,
  shouldDrawBar,
};

if (typeof window !== "undefined") {
  window.CSWAP_TIMELINES_GEOMETRY = TL_GEOMETRY;
}
if (typeof module !== "undefined" && module.exports) {
  module.exports = TL_GEOMETRY; // node --test path; inert in the browser
}

if (typeof window !== "undefined")
window.CSWAP_TIMELINES = (() => {
  const MAIN_W = 360;
  const GAP = 8;

  const state = {
    mode: null,          // null | "right" | "left" | "in-panel"
    anchorOffset: 0,     // main-column left edge, px inside the surface
    detailOpen: false,   // Escape stage 1 (charts populate this later)
  };

  // Same predicate bridge.hosted uses: the message handler exists only
  // under WKWebView. window.cswap itself is defined in fixture mode too,
  // so probing it would misroute the toggle to a console log.
  const hosted = () => !!(window.webkit && window.webkit.messageHandlers
    && window.webkit.messageHandlers.cswap);

  function toggle() {
    // Native is authoritative: it computes placement, re-parents the
    // webview into the borderless panel, and answers with a
    // timelineLayout push. Fixture mode fakes the native half.
    if (hosted()) {
      window.cswap.send("toggleTimelines", {});
      return;
    }
    apply(state.mode ? { mode: null, anchorOffset: 0 }
                     : { mode: "right", anchorOffset: 0 });
    console.log("[fixture] toggleTimelines ->", state.mode);
  }

  function apply(layout) {
    if (!layout || typeof layout !== "object") return;
    const mode = layout.mode === "right" || layout.mode === "left"
      || layout.mode === "in-panel" ? layout.mode : null;
    if (mode === state.mode && !mode) return;
    state.mode = mode;
    state.detailOpen = false;
    // Clamp the native-supplied anchor offset before use — bridge input
    // is never trusted blind, even validated input.
    const surfaceW = document.documentElement.clientWidth || MAIN_W;
    state.anchorOffset = Math.max(
      0, Math.min(Number(layout.anchorOffset) || 0, Math.max(0, surfaceW - MAIN_W))
    );
    paint();
  }

  // ---- DOM ---------------------------------------------------------------

  function companionEl() {
    return document.getElementById("tl-companion");
  }

  function paint() {
    document.body.classList.toggle("tl-expanded", state.mode !== null);
    document.body.classList.toggle("tl-right", state.mode === "right");
    document.body.classList.toggle("tl-left", state.mode === "left");
    document.body.classList.toggle("tl-inpanel-mode", state.mode === "in-panel");
    const trig = document.querySelector(".tl-trigger");
    if (trig) trig.setAttribute("aria-expanded", state.mode ? "true" : "false");

    const existing = companionEl();
    if (!state.mode) {
      if (existing) existing.remove();
      document.body.style.removeProperty("--tl-anchor-x");
      return;
    }

    let el = existing;
    if (!el) {
      el = document.createElement("section");
      el.id = "tl-companion";
      el.setAttribute("aria-label", "Reset timelines");
      el.innerHTML = [
        '<div class="tl-head">',
        '<span class="tl-title">Reset timelines</span>',
        '<span class="tl-spacer"></span>',
        '<button class="tl-close icon-btn" data-tl-act="close" ',
        'title="Close" aria-label="Close timelines"></button>',
        '</div>',
        '<div class="tl-body" data-tl-scaffold="1">',
        '<p class="tl-caption">Charts land with chart-ui tasks.</p>',
        '</div>',
      ].join("");
      document.body.appendChild(el);
      el.querySelector(".tl-close").innerHTML =
        window.CSWAP_ICONS.icon("x", 13);
      el.addEventListener("click", (ev) => {
        const act = ev.target.closest("[data-tl-act]");
        if (act && act.dataset.tlAct === "close") toggle();
      });
    }
    if (state.mode === "in-panel") {
      document.body.style.removeProperty("--tl-anchor-x");
    } else {
      document.body.style.setProperty("--tl-anchor-x", `${state.anchorOffset}px`);
    }
  }

  // ---- keyboard ------------------------------------------------------------

  document.addEventListener("keydown", (ev) => {
    if (ev.key !== "Escape" || !state.mode) return;
    if (state.detailOpen) {
      state.detailOpen = false;  // stage 1: close the row detail only
      document.dispatchEvent(new CustomEvent("tl-close-detail"));
      return;
    }
    toggle();                    // stage 2: collapse, focus returns to trigger
    const trig = document.querySelector(".tl-trigger");
    if (trig) trig.focus();
  });

  return { toggle, apply, state };
})();
