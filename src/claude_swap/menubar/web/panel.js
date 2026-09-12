/* claude-swap menubar panel.
 *
 * Vanilla JS state/render module. Renders the additive schemaVersion-1
 * view-model built by claude_swap.menubar.viewmodel. Two modes:
 *
 *   - hosted:   window.webkit.messageHandlers.cswap exists (WKWebView);
 *               actions go to the Python bridge, pushes come back as
 *               cswap.push(...) / cswap.reply(id, ...).
 *   - fixture:  opened as a plain file in a browser — renders an embedded
 *               fixture view-model and logs actions to the console, so the
 *               panel can be developed and visually iterated without macOS.
 */
"use strict";

// ---------------------------------------------------------------- state ----

const state = {
  vm: null,
  selectedSlot: null,      // pill selection (never switches by itself)
  history: [],
};

const els = {
  panel: document.getElementById("panel"),
  toasts: document.getElementById("toasts"),
};

// ---------------------------------------------------------------- bridge ---

const bridge = {
  hosted: !!(window.webkit && window.webkit.messageHandlers
             && window.webkit.messageHandlers.cswap),

  _nextId: 1,

  send(action, payload) {
    if (!bridge.hosted) {
      console.log("[fixture] action", action, payload ?? {});
      return Promise.resolve(null);
    }
    const id = String(bridge._nextId++);
    return new Promise((resolve) => {
      bridge._pending[id] = resolve;
      window.webkit.messageHandlers.cswap.postMessage(
        JSON.stringify({ id, action, payload: payload ?? {} }));
    });
  },
  _pending: {},

  reply(id, ok, data) {           // called from Python via evaluateJavaScript
    const resolve = bridge._pending[id];
    if (!resolve) return;
    delete bridge._pending[id];
    resolve(ok ? { ok: true, data } : { ok: false, error: data });
  },

  push(type, data) {              // called from Python: {type:"vm"|"engine"}
    if (type === "vm") {
      state.vm = data;
      if (state.selectedSlot === null && data.accounts.length) {
        state.selectedSlot = data.activeSlot ?? data.accounts[0].slot;
      }
      render();
    } else if (type === "engine") {
      toast(data.text);
      refresh();
    }
  },
};

// Expose the Python-facing surface under one global, matching the bridge
// protocol in menubar/bridge.py.
window.cswap = {
  push: (msg) => bridge.push(msg.type, msg.data),
  reply: (id, result) => bridge.reply(
    id, result && result.ok, result ? (result.ok ? result.data : result.error) : undefined
  ),
  send: (action, payload) => bridge.send(action, payload),
};

// ---------------------------------------------------------------- helpers --

const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));

const pctClass = (pct) => (pct >= 90 ? "crit" : pct >= 70 ? "warn" : "ok");

const bindingPct = (acct) => {
  let worst = null;
  for (const w of acct.windows) worst = worst === null ? w.pct : Math.max(worst, w.pct);
  return worst;                    // null = unknown — never treat as 0
};

const statusPill = (acct) => {
  if (!acct) return "";
  if (acct.quarantined) {
    return `<span class="pill crit">needs attention</span>`;
  }
  const pct = bindingPct(acct);
  if (pct === null) return `<span class="pill warn">no usage yet</span>`;
  if (pct >= 95) return `<span class="pill crit">exhausted</span>`;
  if (pct >= 70) return `<span class="pill warn">near limit</span>`;
  return `<span class="pill ok">OK</span>`;
};

const activeAccount = () =>
  (state.vm ? state.vm.accounts.find((a) => a.active) : null);

const selectedAccount = () =>
  (state.vm ? state.vm.accounts.find((a) => a.slot === state.selectedSlot) : null);

function countdownFrom(ts, now) {
  const remaining = Math.floor(ts - now);
  if (remaining <= 0) return null;
  const days = Math.floor(remaining / 86400);
  const hours = Math.floor((remaining % 86400) / 3600);
  const minutes = Math.floor((remaining % 3600) / 60);
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

// ---------------------------------------------------------------- render ---

function render() {
  const vm = state.vm;
  if (!vm) { els.panel.innerHTML = ""; return; }
  const active = activeAccount();
  const sel = selectedAccount() ?? active;

  els.panel.innerHTML = [
    headerHtml(active),
    bannerHtml(),
    pillsHtml(sel),
    cardHtml(sel),
    actionsHtml(sel),
    autoSwitchHtml(),
    footerHtml(),
  ].join("");
  wire();
}

function headerHtml(active) {
  const fresh = state.vm.freshness;
  const freshText = fresh.ageText ? `Updated ${fresh.ageText}` : "No usage yet";
  const name = active
    ? `${esc(active.alias ?? active.label)}<span style="font-weight:400;color:var(--text-dim)"> · ${esc(active.org)}</span>`
    : "no active account";
  return `
  <section class="header">
    <div class="who">
      <div class="name">${name}</div>
      <div class="fresh">${esc(freshText)}</div>
    </div>
    ${statusPill(active)}
    <button class="icon-btn" data-act="refresh" title="Refresh" aria-label="Refresh">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
           stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <polyline points="23 4 23 10 17 10"/>
        <polyline points="1 20 1 14 7 14"/>
        <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>
      </svg>
    </button>
  </section>`;
}

function bannerHtml() {
  const fresh = state.vm.freshness;
  if (fresh.ok) return "";
  const when = fresh.ageText ? ` · ${esc(fresh.ageText)}` : "";
  const why = fresh.error ? ` — ${esc(fresh.error)}` : "";
  return `<section><div class="banner">${fresh.error ? "Showing last known usage" : "No usage yet"}${when}${why}</div></section>`;
}

function pillsHtml(sel) {
  const pills = state.vm.accounts.map((a) => {
    const cls = [
      "acct-pill",
      a.slot === sel?.slot ? "selected" : "",
      a.active ? "active" : "",
      a.disabled ? "disabled-slot" : "",
      a.quarantined ? "quarantined" : "",
    ].filter(Boolean).join(" ");
    return `<button class="${cls}" data-act="select" data-slot="${esc(a.slot)}">
      <span class="dot"></span>${esc(a.slot)} ${esc(a.label)}</button>`;
  });
  pills.push(`<button class="acct-pill add" data-act="add">+</button>`);
  return `<section class="pills">${pills.join("")}</section>`;
}

function cardHtml(acct) {
  if (!acct) {
    return `<section class="card"><div class="note">No managed accounts yet.
      <button class="linkish" data-act="add">Add your first account</button> —
      from the current Claude Code login or a setup token.</div></section>`;
  }
  const badges = [
    acct.active ? `<span class="badge active-badge">active</span>` : "",
    acct.disabled ? `<span class="badge disabled-badge">held out</span>` : "",
    acct.quarantined ? `<span class="badge disabled-badge">quarantined</span>` : "",
  ].join("");
  let body;
  if (acct.quarantined || acct.windows.length === 0) {
    body = `<div class="note">${esc(acct.note ?? "usage unavailable")}</div>`;
  } else {
    body = acct.windows.map(windowHtml).join("");
    if (acct.spend) {
      const limit = acct.spend.limit != null
        ? ` / $${acct.spend.limit % 1 === 0 ? acct.spend.limit.toFixed(0) : acct.spend.limit.toFixed(2)}`
        : "";
      body += `<div class="kv"><span class="k">Spend${esc(acct.spend.currency === "USD" ? "" : " (" + acct.spend.currency + ")")}</span>
        <span class="v">$${acct.spend.used.toFixed(2)}${limit} · ${acct.spend.pct.toFixed(0)}%</span></div>`;
    }
  }
  const paceChip = acct.pace && acct.pace.aheadOfPace
    ? `<span class="chip ahead">ahead of pace</span>` : "";
  return `
  <section class="card">
    <div class="card-head">
      <span class="email">${esc(acct.alias ?? acct.label)}</span>${paceChip}
      <span class="org">${esc(acct.org)}</span>${badges}
    </div>
    ${body}
  </section>`;
}

function windowHtml(w) {
  const pct = Math.min(100, Math.max(0, w.pct));
  const cd = w.resetsAt
    ? `<div class="row-countdown" data-resets-at="${esc(String(w.resetsAt))}">resets ${esc(w.countdownText ?? "")}</div>`
    : "";
  return `
  <div class="window-row">
    <div class="row-top">
      <span class="row-label">${esc(w.label)}</span>
      <span class="row-pct ${pctClass(w.pct)}">${w.pct.toFixed(0)}%${
        w.state === "stale" ? ` <span class="chip stale">stale</span>` : ""
      }</span>
    </div>
    <div class="bar"><i class="${pctClass(w.pct)} ${w.state === "stale" ? "stale" : ""}"
      style="width:${pct}%"></i></div>
    ${cd}
  </div>`;
}

function actionsHtml(acct) {
  if (!acct) return "";
  const cant = acct.active || !acct.switchable || acct.quarantined;
  const label = acct.active ? "Current account" : "Switch here";
  return `
  <section class="actions">
    <button class="primary" data-act="switch" data-slot="${esc(acct.slot)}" ${cant ? "disabled" : ""}>${label}</button>
    <button class="ghost" data-act="overflow" data-slot="${esc(acct.slot)}">⋯</button>
  </section>`;
}

function autoSwitchHtml() {
  const as = state.vm.autoSwitch;
  const line2 = as.lastEventText
    ? esc(as.lastEventText)
    : `threshold ${esc(String(as.thresholdPct))}% · ${esc(as.strategy)}`;
  return `
  <section class="autoswitch">
    <div class="as-copy">
      <div class="line1">Auto-switch</div>
      <div class="line2">${as.enabled ? "watching" : "off"} · ${line2}</div>
    </div>
    <button class="switch ${as.enabled ? "on" : ""}" data-act="autoswitch"
      title="Run the same engine as cswap auto"></button>
  </section>`;
}

function footerHtml() {
  return `
  <section class="footer">
    <button data-act="add">Add account</button>
    <button data-act="settings">Settings</button>
    <button data-act="quit">Quit</button>
  </section>`;
}

// ---------------------------------------------------------------- wiring ---

function wire() {
  els.panel.querySelectorAll("[data-act]").forEach((btn) => {
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      const act = btn.dataset.act;
      const slot = btn.dataset.slot;
      switch (act) {
        case "select": state.selectedSlot = slot; render(); break;
        case "refresh": doRefresh(btn); break;
        case "switch": doAction(btn, "switch", { slot }, `switched to ${slot}`); break;
        case "autoswitch": {
          const target = !state.vm.autoSwitch.enabled;
          doAction(null, "setAutoSwitch", { enabled: target }, null)
            .then((r) => {
              // hosted mode re-renders from the fresh vm the reply triggers;
              // fixture mode has no reply, so apply the flip locally.
              if (r.ok && !bridge.hosted) {
                state.vm.autoSwitch.enabled = target;
                render();
              }
            });
          break;
        }
        case "add": openAddSheet(); break;
        case "overflow": openOverflowMenu(slot); break;
        case "settings": openSettingsSheet(); break;
        case "quit": doAction(btn, "quit", {}, null); break;
      }
    });
  });
}

async function doRefresh(btn) {
  btn.classList.add("busy");
  await bridge.send("refresh", {});
  // hosted mode: the reply precedes a vm push that re-renders; fixture:
  // drop the spinner after a beat.
  setTimeout(() => btn.classList.remove("busy"), 600);
  if (!bridge.hosted) toast("refresh requested");
}

async function doAction(btn, action, payload, successText) {
  if (btn) btn.disabled = true;
  const res = await bridge.send(action, payload);
  if (btn) btn.disabled = false;
  if (!res) { refresh(); return { ok: true }; }   // fixture mode: already logged
  if (res.ok) {
    if (successText) toast(successText);
    refresh();
    return { ok: true };
  }
  toast(res.error || `${action} failed`, true);
  return { ok: false };
}

function refresh() {
  bridge.send("getSnapshot", {}).then((res) => {
    if (res && res.ok) bridge.push("vm", res.data);
  });
}

// ---------------------------------------------------------------- sheets --

function closeSheet() {
  const backdrop = document.querySelector(".sheet-backdrop");
  if (backdrop) backdrop.remove();
}

function openSheet(html) {
  closeSheet();
  const backdrop = document.createElement("div");
  backdrop.className = "sheet-backdrop";
  backdrop.innerHTML = `<div class="sheet">${html}</div>`;
  backdrop.addEventListener("click", (ev) => {
    if (ev.target === backdrop) closeSheet();
  });
  document.body.appendChild(backdrop);
  return backdrop;
}

function openOverflowMenu(slot) {
  const acct = state.vm.accounts.find((a) => a.slot === slot);
  if (!acct) return;
  const verb = acct.disabled ? "Enable" : "Disable";
  openSheet(`
    <h3>${esc(acct.alias ?? acct.label)} · ${esc(acct.slot)}</h3>
    <div class="row" style="flex-direction:column;align-items:stretch;gap:8px">
      <button class="ghost" data-sheet="toggle">${verb} account</button>
      <button class="ghost" data-sheet="copyemail">Copy email</button>
      <button class="ghost" data-sheet="remove" style="color:var(--crit)">Remove account…</button>
    </div>
    <div class="row" style="margin-top:10px"><button class="ghost" data-sheet="close">Cancel</button></div>
  `);
  wireSheet({
    toggle: () => doAction(null, acct.disabled ? "enable" : "disable", { slot },
                           acct.disabled ? "back in rotation" : "held out of rotation")
                  .then((r) => { if (r.ok) closeSheet(); }),
    copyemail: () => {
      if (navigator.clipboard) navigator.clipboard.writeText(acct.email).catch(() => {});
      toast("email copied"); closeSheet();
    },
    remove: () => { closeSheet(); openRemoveConfirm(slot); },
    close: () => closeSheet(),
  });
}

function openRemoveConfirm(slot) {
  const acct = state.vm.accounts.find((a) => a.slot === slot);
  if (!acct) return;
  openSheet(`
    <h3>Remove account ${esc(acct.slot)}?</h3>
    <p>${esc(acct.alias ?? acct.label)} (${esc(acct.org)}) — the stored backup is
    deleted. The account can be re-added later from a fresh login.</p>
    <div class="row">
      <button class="ghost" data-sheet="cancel">Cancel</button>
      <button class="primary" data-sheet="remove" style="flex:none">Remove</button>
    </div>
  `);
  wireSheet({
    cancel: () => closeSheet(),
    remove: () => doAction(null, "remove", { slot }, `removed ${slot}`)
                  .then((r) => { if (r.ok) closeSheet(); }),
  });
}

function openAddSheet() {
  openSheet(`
    <h3>Add account</h3>
    <div class="row" style="flex-direction:column;align-items:stretch;gap:8px">
      <button class="ghost" data-sheet="login">From current login —<br>
        <span style="font-size:11px;color:var(--text-dim)">captures whatever Claude Code is logged in as now</span></button>
      <button class="ghost" data-sheet="token">From setup-token…</button>
    </div>
    <div class="row" style="margin-top:10px"><button class="ghost" data-sheet="close">Cancel</button></div>
  `);
  wireSheet({
    login: () => doAction(null, "addFromLogin", {}, "account added")
                 .then((r) => { if (r.ok) closeSheet(); }),
    token: () => { closeSheet(); openTokenSheet(); },
    close: () => closeSheet(),
  });
}

function openTokenSheet() {
  openSheet(`
    <h3>Add from setup-token</h3>
    <p>Paste the token Claude Code printed for account linking.</p>
    <input type="email" placeholder="email for this token" data-field="email">
    <input type="password" placeholder="sk-ant-oat01-…" data-field="token" autocomplete="off">
    <div class="row">
      <button class="ghost" data-sheet="cancel">Cancel</button>
      <button class="primary" data-sheet="add" style="flex:none">Add</button>
    </div>
  `);
  const backdrop = document.querySelector(".sheet-backdrop");
  const get = (f) => backdrop.querySelector(`[data-field="${f}"]`).value.trim();
  wireSheet({
    cancel: () => closeSheet(),
    add: () => {
      const token = get("token");
      if (!token) { toast("paste the token first", true); return; }
      doAction(null, "addFromToken",
               { token, email: get("email") || "" }, "account added")
        .then((r) => { if (r.ok) closeSheet(); });
    },
  });
}

function openSettingsSheet() {
  const as = state.vm.autoSwitch;
  openSheet(`
    <h3>Panel settings</h3>
    <p>Refresh interval</p>
    <div class="row" style="justify-content:flex-start;gap:6px;margin-bottom:10px">
      ${[30, 60, 300].map((s) => `<button class="ghost" data-sheet="iv" data-iv="${s}">${
        s === 300 ? "5 min" : `${s}s`}</button>`).join("")}
    </div>
    <p>Title percentage</p>
    <div class="row" style="justify-content:flex-start;gap:6px">
      ${["off", "5h", "7d", "both"].map((m) =>
        `<button class="ghost" data-sheet="tp" data-tp="${m}">${m}</button>`).join("")}
    </div>
    <p style="margin-top:10px">Auto-switch policy (threshold ${esc(String(as.thresholdPct))}% ·
      ${esc(as.strategy)}) lives in <code>cswap config</code>.</p>
    <div class="row" style="margin-top:10px"><button class="ghost" data-sheet="close">Done</button></div>
  `);
  wireSheet({
    iv: (target) => doAction(null, "setPrefs", { refreshInterval: Number(target.dataset.iv) }, "saved"),
    tp: (target) => doAction(null, "setPrefs", { titlePct: target.dataset.tp }, "saved"),
    close: () => closeSheet(),
  });
}

function wireSheet(handlers) {
  const backdrop = document.querySelector(".sheet-backdrop");
  if (!backdrop) return;
  backdrop.querySelectorAll("[data-sheet]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const fn = handlers[btn.dataset.sheet];
      if (fn) fn(btn);
    });
  });
}

// countdowns tick locally between pushes, from the resetsAt epochs
setInterval(() => {
  const now = Date.now() / 1000;
  els.panel.querySelectorAll("[data-resets-at]").forEach((el) => {
    const cd = countdownFrom(parseFloat(el.dataset.resetsAt), now);
    el.textContent = cd ? `resets ${cd}` : "resets now";
  });
}, 30000);

// ---------------------------------------------------------------- toasts ---

function toast(text, isErr) {
  if (!text) return;
  const el = document.createElement("div");
  el.className = "toast" + (isErr ? " err" : "");
  el.textContent = text;
  els.toasts.appendChild(el);
  setTimeout(() => el.remove(), 2600);
}

// ---------------------------------------------------------------- fixture --

const FIXTURE = {
  schemaVersion: 1,
  activeSlot: "1",
  takenAt: 1757635200,
  freshness: { ageText: "2m ago", ok: true },
  accounts: [
    {
      slot: "1", label: "hungtrv", alias: "Work", org: "personal", kind: "oauth",
      active: true, switchable: true,
      windows: [
        { kind: "5h", label: "5-hour", pct: 68.4, state: "ok",
          resetsAt: 1757638440, countdownText: "54m" },
        { kind: "7d", label: "7-day", pct: 41.2, state: "ok",
          resetsAt: 1758118800, countdownText: "5d 14h" },
        { kind: "model:Fable", label: "Fable", pct: 84.0, state: "ok",
          resetsAt: 1757809200, countdownText: "2d 1h" },
      ],
      spend: { used: 12.4, limit: 100, pct: 12.4, currency: "USD",
               resetsAt: 1757894400 },
      pace: { aheadOfPace: true, expectedPct: 21.4 },
    },
    {
      slot: "2", label: "honeybad", org: "Acme Corp", kind: "oauth",
      active: false, switchable: true,
      windows: [
        { kind: "5h", label: "5-hour", pct: 12.0, state: "ok",
          resetsAt: 1757642400, countdownText: "2h" },
        { kind: "7d", label: "7-day", pct: 8.5, state: "ok",
          resetsAt: 1758154800, countdownText: "6d 2h" },
      ],
    },
    {
      slot: "3", label: "backup", org: "personal", kind: "oauth",
      active: false, switchable: true, quarantined: true,
      note: "re-login needed — refresh token dead; log in with Claude Code, then run: cswap add",
      windows: [],
    },
  ],
  autoSwitch: { enabled: true, thresholdPct: 80, strategy: "best",
                lastEventText: "2 → 1 · 18m ago" },
  history: ["2 → 1 · 18m ago", "1 → 2 · 3h ago"],
};

// Explicit theme override (screenshots, visual testing): ?theme=light|dark
const themeOverride = new URLSearchParams(location.search).get("theme");
if (themeOverride === "light" || themeOverride === "dark") {
  document.documentElement.dataset.theme = themeOverride;
}

if (!bridge.hosted) {
  console.log("[fixture] claude-swap panel — fixture mode; actions log here");
  bridge.push("vm", FIXTURE);
} else {
  refresh();
}
