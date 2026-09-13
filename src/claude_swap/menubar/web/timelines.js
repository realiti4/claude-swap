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
    selectedSlot: null,  // inspect-only row selection; never activates
  };

  // Row internals from the extraction (board 17/20): left identity block,
  // 242px track, right detail block; the axis/gridlines/Now overlays align
  // to the same track coordinates.
  const LEFT_W = 140, TRACK_W = 242, DETAIL_W = 152, ROW_GAP = 8;
  // 146, not LEFT_W+GAP: measured on the board-17 export (vertical
  // features at track [172, 414] against card content x=26) — the Pen
  // serialization rounds the left block a hair wide.
  const TRACK_X = 146;

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

  // ---- DOM (board-exact structure; values from the token extraction) ----

  const KINDS = [
    { kind: "5h", title: "Session resets \u00b7 5-hour",
      hint: "full 5-hour window per account" },
    { kind: "7d", title: "Weekly resets \u00b7 7-day",
      hint: "full 7-day window per account" },
  ];

  function zoneName() {
    if (typeof window !== "undefined" && window.CSWAP_TL_TZ) {
      return window.CSWAP_TL_TZ;
    }
    try {
      return Intl.DateTimeFormat().resolvedOptions().timeZone || "local";
    } catch (_e) {
      return "local";
    }
  }

  function nowClock() {
    const nowSec = (typeof window !== "undefined" && window.CSWAP_TL_NOW)
      ? window.CSWAP_TL_NOW() : Date.now() / 1000;
    try {
      return "Now " + new Date(nowSec * 1000).toLocaleTimeString("en-GB", {
        hour: "2-digit", minute: "2-digit", hour12: false, timeZone: TL_TZ(),
      });
    } catch (_e) {
      return "Now";
    }
  }

  function chartCard(def) {
    const geo = window.CSWAP_TIMELINES_GEOMETRY;
    const ticks = geo.axisTicks(def.kind)
      .map((t) => `<span class="tick tl-text${t.now ? " now" : ""}" data-fid="tl-tick"
        style="left:${(TRACK_X + t.at * TRACK_W).toFixed(1)}px">${
          t.label}</span>`).join("");
    const gridlines = geo.axisTicks(def.kind)
      .map((t) => `<span class="gridline" style="left:${
        (TRACK_X + t.at * TRACK_W).toFixed(1)}px"></span>`).join("");
    return `
    <section class="tl-card" data-fid="tl-card" data-kind="${def.kind}">
      <div class="tl-card-title-row">
        <span class="tl-card-title tl-text" data-fid="tl-card-title">${def.title}</span>
        <span class="tl-spacer"></span>
        <span class="tl-card-hint tl-text">${def.hint}</span>
      </div>
      <div class="tl-divider"></div>
      <div class="tl-axis" data-fid="tl-axis">${ticks}</div>
      <div class="tl-rows-wrap">
        <div class="tl-gridlines" aria-hidden="true">
          <span class="gridline" style="left:${TRACK_X}px"></span>
          ${gridlines}
          <span class="gridline" style="left:${TRACK_X + TRACK_W}px"></span>
        </div>
        <div class="tl-nowline" aria-hidden="true"
             style="left:${TRACK_X + TRACK_W / 2}px"></div>
        <div class="tl-crosshair" aria-hidden="true">
          <span class="tl-crosshair-time tl-text"></span>
        </div>
        <div class="tl-rows" data-tl-rows="${def.kind}" role="listbox"
             aria-label="${def.title} rows"></div>
      </div>
    </section>`;
  }

  function companionHtml() {
    const inPanel = state.mode === "in-panel";
    return `
    ${inPanel ? `<button class="tl-back-row" data-tl-act="close">
      \u2039 Accounts</button>` : ""}
    <div class="tl-head" data-fid="tl-head">
      <span class="tl-mark" data-fid="tl-mark">${
        window.CSWAP_ICONS.icon("calendar-clock", 14)}</span>
      <span class="tl-title tl-text" data-fid="tl-title">Reset timelines</span>
      <span class="tl-spacer"></span>
      ${inPanel ? "" : `<span class="tl-tz" data-fid="tl-tz">
        <span class="tl-tz-now tl-text" data-tl-now>${nowClock()}</span>
        <span class="tl-tz-zone tl-text">${zoneName()}</span>
      </span>
      <button class="tl-close-btn" data-tl-act="close" title="Close"
        aria-label="Close timelines">${
          window.CSWAP_ICONS.icon("x", 13)}</button>`}
    </div>
    <div class="tl-body">
      <p class="tl-caption tl-text" data-fid="tl-caption">Each bar is one full window
        on a shared clock; the fill is quota used, not time elapsed.</p>
      <div class="tl-legend" data-fid="tl-legend" aria-hidden="true">
        <span class="sw window"></span><span class="tl-text">full window (5h / 7d)</span>
        <span class="sw quota"></span><span class="tl-text">quota used \u2014 not elapsed</span>
        <span class="sw now-dashes"></span><span class="now-label tl-text">now</span>
      </div>
      ${KINDS.map(chartCard).join("")}
    </div>`;
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
    if (el && el.dataset.inpanel !== String(state.mode === "in-panel")) {
      // mode changed between wide and in-panel: the header affordances
      // (timezone/close vs Accounts back row) differ — rebuild the shell
      el.innerHTML = companionHtml();
    }
    if (!el) {
      el = document.createElement("section");
      el.id = "tl-companion";
      el.setAttribute("aria-label", "Reset timelines");
      el.innerHTML = companionHtml();
      document.body.appendChild(el);
      el.addEventListener("click", (ev) => {
        const act = ev.target.closest("[data-tl-act]");
        if (act && act.dataset.tlAct === "close") {
          const trigBtn = document.querySelector(".tl-trigger");
          toggle();
          if (trigBtn) trigBtn.focus();  // opener focus restored on close
          return;
        }
        if (act && act.dataset.tlAct === "close-detail") {
          closeDetail();
          return;
        }
        const row = ev.target.closest(".tl-row");
        if (row && row.dataset.slot) {
          // Selection inspects; it never activates the account.
          state.selectedSlot = row.dataset.slot;
          renderRows();
        }
      });
    }
    el.dataset.inpanel = String(state.mode === "in-panel");
    // Enter slide: the class times the animation out from JS so a
    // suspended animation (non-key window) can never strand the shift.
    el.classList.add("tl-enter");
    setTimeout(() => el.classList.remove("tl-enter"), 200);
    if (state.mode === "in-panel") {
      document.body.style.removeProperty("--tl-anchor-x");
    } else {
      document.body.style.setProperty("--tl-anchor-x", `${state.anchorOffset}px`);
    }
    renderRows();
    wireCrosshair();
    tickClock();
  }

  function tickClock() {
    const el = document.querySelector("[data-tl-now]");
    if (el) el.textContent = nowClock();
    const now = (typeof window !== "undefined" && window.CSWAP_TL_NOW)
      ? window.CSWAP_TL_NOW() : Date.now() / 1000;
    document.querySelectorAll("[data-tl-cd]").forEach((chip) => {
      const txt = window.CSWAP_TIMELINES_GEOMETRY.countdownText(
        parseFloat(chip.dataset.tlCd), now);
      chip.textContent = txt || "";
    });
  }

  function wireCrosshair() {
    document.querySelectorAll(".tl-rows-wrap").forEach((wrap) => {
      if (wrap.dataset.crosshairWired) return;
      wrap.dataset.crosshairWired = "1";
      const ch = wrap.querySelector(".tl-crosshair");
      const label = ch && ch.querySelector(".tl-crosshair-time");
      const card = wrap.closest(".tl-card");
      if (!ch || !label || !card) return;
      const kind = card.dataset.kind;
      const D = window.CSWAP_TIMELINES_GEOMETRY.TL_SECONDS[kind];
      if (!D) return;
      const fine = window.matchMedia
        && window.matchMedia("(hover: hover) and (pointer: fine)").matches;
      const calm = window.matchMedia
        && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      if (!fine || calm) return;  // session rule: hover pointers, no motion
      wrap.addEventListener("mousemove", (ev) => {
        const rect = wrap.getBoundingClientRect();
        const x = ev.clientX - rect.left - TRACK_X;
        if (x < 0 || x > TRACK_W) { ch.style.opacity = "0"; return; }
        const frac = x / TRACK_W;
        ch.style.opacity = "1";
        ch.style.left = `${TRACK_X + x}px`;
        const tSec = ((frac - 0.5) * 2 * D) + ((typeof window !== "undefined"
          && window.CSWAP_TL_NOW) ? window.CSWAP_TL_NOW() : Date.now() / 1000);
        label.textContent = fmtLocal(tSec, false);
      });
      wrap.addEventListener("mouseleave", () => { ch.style.opacity = "0"; });
    });
  }

  // ---- rows (board 17/20 state matrix; geometry from the pure layer) ----

  const STATUS_TEXT = {
    "elapsed": "Awaiting updated usage \u2014 no fresh cycle",
    "no-window": "No subscription quota \u2014 no reset window",
    "reset-unavailable": "Reset time unavailable",
    "unavailable": "Usage unavailable",
  };

  function esc(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  const TL_TZ = () => (typeof window !== "undefined" && window.CSWAP_TL_TZ)
    || undefined;

  function clockLabel(resetsAt, kind) {
    try {
      const d = new Date(resetsAt * 1000);
      const time = d.toLocaleTimeString("en-GB", {
        hour: "2-digit", minute: "2-digit", hour12: false,
        timeZone: TL_TZ(),
      });
      if (kind !== "7d") return `\u00b7 ${time}`;
      // Weekly resets cross days: the board shows "· 14 Sep 01:30".
      const date = d.toLocaleDateString("en-GB", {
        day: "numeric", month: "short", timeZone: TL_TZ(),
      });
      return `\u00b7 ${date} ${time}`;
    } catch (_e) {
      return "";
    }
  }

  function rowHtml(kind, acct, now) {
    const geo = window.CSWAP_TIMELINES_GEOMETRY;
    const w = geo.deriveWindowState(kind, acct, now);
    const selected = state.selectedSlot === acct.slot;
    const focusable = selected ? "0" : "-1";  // roving tabindex
    const rail = selected ? '<span class="row-rail"></span>' : "";
    const left = `
      <div class="row-left">
        <span class="row-idx tl-text${selected ? " sel" : ""}">${esc(acct.slot)}</span>
        <span class="row-alias tl-text">${esc(acct.alias || acct.label || "")}</span>
        ${acct.active ? '<span class="row-dot" title="Active account"></span>' : ""}
      </div>`;

    if (!geo.shouldDrawBar(w.state)) {
      return `<div class="tl-row${selected ? " selected" : ""}" role="listitem"
        data-slot="${esc(acct.slot)}" aria-selected="${selected}">${rail}
        ${left}<div class="row-status tl-text">${STATUS_TEXT[w.state] || ""}</div></div>`;
    }

    const g = geo.layoutTimelineWindow(kind, w.resetsAt, w.pct, now);
    const stale = w.state === "stale";
    const nearLimit = w.pct != null && w.pct >= 80; // board 17: 83% amber, 18% teal
    const winCls = stale ? "bar-window stale" : "bar-window";
    const fillCls = stale ? "bar-fill stale" : nearLimit ? "bar-fill warn"
      : "bar-fill";
    const fill = w.pct == null ? "" :
      `<span class="${fillCls}" style="left:${(g.leftFraction * 100).toFixed(3)}%;
        width:${Math.min(w.pct, 100).toFixed(1)}%"></span>`;
    const cap = `<span class="bar-cap${stale ? " stale" : ""}"
      style="left:calc(${((g.leftFraction + g.widthFraction) * 100).toFixed(3)}% - 1.25px)"></span>`;
    const pctText = w.pct == null ? "no usage data"
      : `${Math.round(w.pct)}% used`;
    const resetText = stale ? "\u00b7 last known"
      : w.resetsAt != null ? clockLabel(w.resetsAt, kind) : "";
    const offscale = g && g.offscale
      ? '<span class="offscale-mark" title="Reset is outside the visible window"></span>'
      : "";
    return `<div class="tl-row${selected ? " selected" : ""}" role="option"
      tabindex="${focusable}" data-slot="${esc(acct.slot)}"
      aria-selected="${selected}" data-kind="${kind}">${rail}${left}
      <div class="row-track">${offscale}
        <span class="${winCls}" style="left:${(g.leftFraction * 100).toFixed(3)}%;
          width:${(g.widthFraction * 100).toFixed(3)}%"></span>${fill}${cap}
      </div>
      <div class="row-detail">
        ${(w.resetsAt != null && w.resetsAt > now
            && !(typeof window !== "undefined" && window.CSWAP_TL_BOARD))
          ? `<span class="row-cd" data-tl-cd="${w.resetsAt}" data-kind="${kind}"></span>` : ""}
        <span class="row-pct tl-text${w.pct == null || stale ? " dim" : ""}">${pctText}</span>
        <span class="row-reset tl-text">${resetText}</span>
      </div></div>`;
  }

  function setVm(vm) {
    // Every vm push refreshes the stored roster; open charts re-render
    // in place (renderRows preserves per-chart scroll).
    state.vm = vm;
    if (state.mode) renderRows();
  }

  // ---- row detail (board 20: surface-3 r8, border-strong, inferred) ----

  function fmtLocal(tsSec, withDate = true) {
    try {
      const d = new Date(tsSec * 1000);
      const time = d.toLocaleTimeString("en-GB", {
        hour: "2-digit", minute: "2-digit", hour12: false, timeZone: TL_TZ(),
      });
      if (!withDate) return time;
      const date = d.toLocaleDateString("en-GB", {
        weekday: "short", day: "numeric", month: "short", timeZone: TL_TZ(),
      });
      return `${date} ${time}`;
    } catch (_e) {
      return "";
    }
  }

  function windowDetail(acct, kind, now) {
    const geo = window.CSWAP_TIMELINES_GEOMETRY;
    const w = geo.deriveWindowState(kind, acct, now);
    const D = geo.TL_SECONDS[kind];
    const unit = kind === "5h" ? "5 hours" : "7 days";
    let body;
    if (w.state === "no-window" || w.state === "unavailable") {
      body = `<p class="tl-text">${STATUS_TEXT[w.state] || "Usage unavailable"}</p>`;
    } else if (w.state === "elapsed") {
      body = `<p class="tl-text">${STATUS_TEXT["elapsed"]}</p>`;
    } else if (w.state === "reset-unavailable") {
      body = `<p class="tl-text">Reset time unavailable — usage last read ${
        w.pct != null ? Math.round(w.pct) + "%" : "n/a"}</p>`;
    } else {
      const start = w.startsAt != null ? w.startsAt : w.resetsAt - D;
      const inferred = w.startsAt == null;
      const left = geo.countdownText(w.resetsAt, now);
      const pctLine = w.pct == null
        ? `<span class="dim">no usage data</span>`
        : `${Math.round(w.pct)}% used`;
      const leftLine = left != null ? ` \u00b7 ${left} left` : "";
      body = `
        <p class="dt-row tl-text"><strong>${pctLine}</strong>${leftLine}</p>
        <p class="dt-span tl-text">${fmtLocal(start)} \u2192 ${fmtLocal(w.resetsAt)}</p>
        ${inferred ? `<p class="dt-inferred tl-text">start inferred (reset \u2212 ${
          kind === "5h" ? "5h" : "7d"})</p>` : ""}`;
    }
    return `
      <div class="dt-window">
        <p class="dt-label tl-text">${kind === "5h" ? "Session" : "Weekly"}
          window \u00b7 ${unit}</p>
        ${body}
      </div>`;
  }

  function openDetail(slot) {
    state.detailSlot = String(slot);
    state.detailOpen = true;
    const card = document.querySelector('.tl-card[data-kind="5h"]');
    const row = card && card.querySelector(`.tl-row[data-slot="${CSS.escape(String(slot))}"]`);
    let el = document.getElementById("tl-detail");
    if (!el && card) {
      el = document.createElement("div");
      el.id = "tl-detail";
      el.setAttribute("role", "dialog");
      el.setAttribute("aria-label", "Row detail");
      card.querySelector(".tl-rows-wrap").appendChild(el);
    }
    if (!el) return;
    const now = (typeof window !== "undefined" && window.CSWAP_TL_NOW)
      ? window.CSWAP_TL_NOW() : Date.now() / 1000;
    const accounts = (state.vm && state.vm.accounts) || [];
    const acct = accounts.find((a) => String(a.slot) === String(slot));
    if (!acct) { closeDetail(); return; }
    el.innerHTML = `
      <div class="dt-head">
        <span class="dt-badge tl-text">${esc(acct.slot)}</span>
        <span class="dt-alias tl-text">${esc(acct.alias || acct.label || "")}</span>
        <button class="dt-close" data-tl-act="close-detail" title="Close"
          aria-label="Close detail">${window.CSWAP_ICONS.icon("x", 11)}</button>
      </div>
      <div class="dt-divider"></div>
      ${windowDetail(acct, "5h", now)}
      <div class="dt-divider"></div>
      ${windowDetail(acct, "7d", now)}`;
    // anchor under the row, clamped inside the rows viewport
    const wrap = el.parentElement;
    const top = row
      ? Math.min(Math.max(row.offsetTop + row.offsetHeight + 2, 0),
                 wrap.clientHeight - el.offsetHeight - 2)
      : 0;
    el.style.top = `${Math.max(0, top)}px`;
  }

  function closeDetail() {
    const el = document.getElementById("tl-detail");
    const prev = state.detailSlot;
    state.detailOpen = false;
    state.detailSlot = null;
    if (el) el.remove();
    if (prev != null) {
      const row = document.querySelector(
        `.tl-row[data-slot="${CSS.escape(String(prev))}"]`);
      if (row) row.focus();
    }
  }

  function skeletonRows() {
    // First read in flight: keep headings/axis/geometry in place with
    // placeholder rows — nothing that reads as a bar or a number.
    const bars = [[12, 121], [42, 121], [73, 121], [109, 121]];
    return bars.map(([x, w], i) => `
      <div class="tl-row skeleton" aria-hidden="true">
        <div class="row-left">
          <span class="row-idx">${i + 1}</span>
        </div>
        <div class="row-track">
          <span class="sk-bar" style="left:${x}px;width:${w}px"></span>
        </div>
        <div class="row-detail"><span class="sk-detail"></span></div>
      </div>`).join("");
  }

  function renderRows(vm) {
    if (vm !== undefined) state.vm = vm;
    const haveVm = !!(state.vm && Array.isArray(state.vm.accounts));
    const accounts = haveVm ? state.vm.accounts : [];
    const slots = new Set(accounts.map((a) => String(a.slot)));
    if (!haveVm) {
      document.querySelectorAll("[data-tl-rows]").forEach((rowsEl) => {
        rowsEl.innerHTML = skeletonRows();
      });
      return;
    }
    if (accounts.length && (state.selectedSlot === null
                            || !slots.has(String(state.selectedSlot)))) {
      // first open, or the selected account vanished from a push: fall
      // back to the active account when it still exists, else the first
      // row — never dangle (the active slot can be the removed one).
      const active = state.vm.activeSlot;
      state.selectedSlot = (active != null && slots.has(String(active)))
        ? active : accounts[0].slot;
    }
    const now = (typeof window !== "undefined" && window.CSWAP_TL_NOW)
      ? window.CSWAP_TL_NOW() : Date.now() / 1000;
    document.querySelectorAll("[data-tl-rows]").forEach((rowsEl) => {
      const kind = rowsEl.dataset.tlRows;
      const scroll = rowsEl.scrollTop;  // refreshes preserve position
      rowsEl.innerHTML = accounts.map((a) => rowHtml(kind, a, now)).join("");
      rowsEl.scrollTop = scroll;
    });
    wireCrosshair();
    if (state.detailOpen) {
      if (slots.has(String(state.detailSlot))) openDetail(state.detailSlot);
      else closeDetail();  // its account vanished: close safely
    }
  }

  // ---- keyboard ------------------------------------------------------------

  document.addEventListener("keydown", (ev) => {
    if ((ev.key === "t" || ev.key === "T") && !state._keyConsumed) {
      const el = ev.target;
      if (el && el.closest
          && el.closest("input, textarea, select, [contenteditable], dialog")) {
        return;  // typing a alias must never toggle the charts
      }
      if (document.querySelector("dialog[open]")) return;
      toggle();
      return;
    }
    if (!state.mode) return;
    if (ev.key === "Enter" || ev.key === " ") {
      const row = ev.target.closest && ev.target.closest(".tl-row");
      if (row && row.dataset.slot) {
        ev.preventDefault();
        state.selectedSlot = row.dataset.slot;
        renderRows();
        openDetail(row.dataset.slot);
      }
      return;
    }
    if (ev.key !== "Escape") return;
    if (state.detailOpen) {
      closeDetail();               // stage 1: detail only
      return;
    }
    toggle();                      // stage 2: collapse, focus to trigger
    const trig = document.querySelector(".tl-trigger");
    if (trig) trig.focus();
  });

  return { toggle, apply, state, tickClock, renderRows, setVm };
})();
