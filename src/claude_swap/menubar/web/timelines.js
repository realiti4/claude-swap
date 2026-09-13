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
