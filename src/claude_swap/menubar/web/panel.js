/* claude-swap menubar panel — Pen redesign render.
 *
 * Vanilla JS state/render module for the graphite/ivory panel. Renders the
 * additive schemaVersion-1 view-model built by claude_swap.menubar.viewmodel.
 * Two modes, as before: hosted (WKWebView bridge) and fixture (plain
 * browser, embedded vm, actions logged to the console).
 *
 * Selection ≠ activation everywhere: clicking an account card previews it;
 * only the explicit Switch action activates. Success/error UI appears only
 * after the backend reply.
 */
"use strict";

// ---------------------------------------------------------------- state ----

const state = {
  vm: null,
  selectedSlot: null,   // preview selection; never switches by itself
  pendingAction: null,  // action name while a reply is in flight
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
  _pending: {},

  send(action, payload) {
    if (!bridge.hosted) {
      // never log secrets in full, even in dev mode
      const safe = (action === "addFromToken" && payload && payload.token)
        ? { ...payload, token: "…" + String(payload.token).slice(-4) }
        : payload;
      console.log("[fixture] action", action, safe ?? {});
      return Promise.resolve(null);
    }
    const id = String(bridge._nextId++);
    return new Promise((resolve) => {
      bridge._pending[id] = resolve;
      window.webkit.messageHandlers.cswap.postMessage(
        JSON.stringify({ id, action, payload: payload ?? {} }));
    });
  },

  reply(id, ok, dataOrError) {   // invoked from Python via evaluateJavaScript
    const resolve = bridge._pending[id];
    if (!resolve) return;
    delete bridge._pending[id];
    resolve(ok ? { ok: true, data: dataOrError } : { ok: false, error: dataOrError });
  },

  push(type, data) {
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

window.cswap = {
  push: (msg) => bridge.push(msg.type, msg.data),
  reply: (id, result) => bridge.reply(
    id, result && result.ok, result ? (result.ok ? result.data : result.error) : undefined
  ),
  send: (action, payload) => bridge.send(action, payload),
};

// ---------------------------------------------------------------- helpers --

// NB: icons.js already declares a global `icon`; this file must not
// redeclare it (classic scripts share the global lexical scope).
const ic = (name, size, cls) =>
  (window.CSWAP_ICONS ? window.CSWAP_ICONS.icon(name, size, cls) : "");

const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));

const pctClass = (pct) => (pct >= 90 ? "bad" : pct >= 70 ? "warn" : "");

const activeAccount = () =>
  state.vm ? state.vm.accounts.find((a) => a.active) : null;
const selectedAccount = () =>
  state.vm ? state.vm.accounts.find((a) => a.slot === state.selectedSlot) : null;

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

const AWAITING = "Awaiting updated usage";

/** Card subtitle per account: status as text, never color alone. */
function cardSubtitle(acct) {
  if (acct.disabled) return { text: "Disabled", cls: "st-warn" };
  switch (acct.status) {
    case "api-key": return { text: "API key", cls: "st-warn" };
    case "needs-login": return { text: "Needs login", cls: "st-bad" };
    case "unavailable": return { text: "Unavailable", cls: "st-warn" };
    default: return acct.active
      ? { text: "Active", cls: "st-ok" }
      : { text: "Ready", cls: "" };
  }
}

// ---------------------------------------------------------------- render ---

function emptyHtml() {
  // Board 05: no tabs at all in the empty roster — one centered card with
  // both setup entry points (the sheets module owns the actual flows).
  return `
  <div class="empty-card">
    <span class="empty-icon">${ic("plus", 22)}</span>
    <h3>No accounts yet</h3>
    <p>Add your first account to start switching.</p>
    <div class="row">
      <button class="btn half" data-act="add-login">Use current Claude Code login</button>
      <button class="btn half" data-act="add">Setup with token</button>
    </div>
  </div>`;
}

function render() {
  const vm = state.vm;
  if (!vm) { els.panel.innerHTML = ""; return; }
  const sel = selectedAccount() ?? activeAccount();

  // A background refresh can push a fresh vm mid-read: preserve where the
  // user was (body scroll position and an open per-model disclosure)
  // across the re-render instead of yanking them back to the top.
  const bodyEl = els.panel.querySelector(".body");
  const savedScroll = bodyEl ? bodyEl.scrollTop : 0;
  const savedOpen = !!els.panel.querySelector("details.disclosure[open]");

  els.panel.innerHTML = [
    headerHtml(),
    `<div class="body">`,
    bannerHtml(),
    vm.accounts.length ? selectorHtml(sel) : emptyHtml(),
    sel ? identityHtml(sel) : "",
    sel ? quotaCardHtml(sel) : "",
    sel ? actionsHtml(sel) : "",
    autoswitchHtml(),
    `</div>`,
    footerHtml(),
  ].join("");
  const newBody = els.panel.querySelector(".body");
  if (newBody) newBody.scrollTop = savedScroll;
  if (savedOpen) {
    const d = els.panel.querySelector("details.disclosure");
    if (d) d.open = true;
  }
  wire();
}

function headerHtml() {
  return `
  <header class="hdr">
    <div class="brand">${ic("swap", 15)}<span class="name">claude-swap</span></div>
    <button class="icon-btn" data-act="refresh" title="Refresh" aria-label="Refresh">
      ${ic("refresh", 13)}
    </button>
    <button class="icon-btn" data-act="gear" title="Settings" aria-label="Settings"
      aria-haspopup="dialog">${ic("gear", 14)}</button>
  </header>`;
}

function bannerHtml() {
  const fresh = state.vm.freshness;
  if (!fresh || fresh.ok) return "";
  const when = fresh.ageText ? ` · ${esc(fresh.ageText)}` : "";
  const why = fresh.error ? ` — ${esc(fresh.error)}` : "";
  return `<div class="note warn">Showing last known usage${when}${why}</div>`;
}

function selectorHtml(sel) {
  // Index tabs: the stable slot number is the anchor identity; selection
  // (aria-checked + ring) is preview-only and never implies activation —
  // the Active status word is what marks the credentialed account.
  const cards = state.vm.accounts.map((a) => {
    const sub = cardSubtitle(a);
    const selected = a.slot === (sel && sel.slot);
    const name = a.alias ?? a.label;
    const cls = [
      "tab",
      selected ? "selected" : "",
      a.disabled ? "off" : "",
    ].filter(Boolean).join(" ");
    return `<button class="${cls}" role="radio" aria-checked="${selected}"
              data-act="select" data-slot="${esc(a.slot)}"
              aria-label="${esc(a.slot)} ${esc(name)} · ${sub.text}">
      <span class="idx num">${esc(a.slot)}</span>
      <span class="txt"><span class="alias">${esc(name)}</span>
        <span class="sub ${sub.cls}">${sub.text}</span></span>
    </button>`;
  });
  cards.push(`<button class="tab add-tab" data-act="add" aria-label="Add account">
    <span class="idx">${ic("plus", 13)}</span>
    <span class="txt"><span class="sub">Add</span></span>
  </button>`);
  return `<div class="tabs" role="radiogroup" aria-label="Accounts">${cards.join("")}</div>`;
}

function identityHtml(acct) {
  const badge = acct.active
    ? `<span class="badge active">Active</span>`
    : `<span class="badge preview">Preview</span>`;
  // Alias row carries its own affordance: pencil Edit when set, Add alias
  // otherwise. Wired to the alias sheet in a later task; clicks are no-ops
  // until then (the dispatcher ignores unknown acts).
  const aliasCell = acct.alias
    ? `${esc(acct.alias)} <button class="mini-btn" data-act="alias-edit" data-slot="${esc(acct.slot)}" title="Edit alias" aria-label="Edit alias">${ic("edit", 11)}</button>`
    : `<span class="dim">Not set</span> <button class="mini-btn" data-act="alias-add" data-slot="${esc(acct.slot)}">Add alias</button>`;
  return `
  <section class="identity" aria-labelledby="account-identity-title">
    <div class="identity-heading">
      <h2 id="account-identity-title">Account</h2>
      ${badge}
    </div>
    <dl class="identity-details">
      <dt>Alias</dt><dd>${aliasCell}</dd>
      <dt>Email</dt><dd>${esc(acct.email || "Not available")}</dd>
      <dt>Team</dt><dd>${esc(acct.org || "Not available")}</dd>
      <dt>Account Index</dt><dd>${esc(acct.slot || "Not available")}</dd>
    </dl>
  </section>`;
}

function statusNoteHtml(acct) {
  if (acct.status === "api-key") {
    return `<div class="note">${ic("warn", 12)} No subscription quota — API key account.</div>`;
  }
  if (acct.status === "needs-login") {
    return `<div class="note warn">${ic("warn", 12)} Needs login — refresh token dead.
      Log in with Claude Code, then re-add the account.</div>`;
  }
  if (acct.status === "unavailable" && acct.windows.length === 0) {
    return `<div class="note">${ic("warn", 12)} ${esc(acct.note ?? "Usage unavailable right now.")}</div>`;
  }
  if (acct.windows.length === 0) {
    return `<div class="note">No usage yet — first measurement pending.</div>`;
  }
  return "";
}

function quotaCardHtml(acct) {
  const note = statusNoteHtml(acct);

  const primary = acct.windows.filter((w) => w.kind === "5h" || w.kind === "7d");
  const secondary = acct.windows.filter((w) => w.kind.startsWith("model:"));

  const row = (w) => {
    const cls = pctClass(w.pct);
    const hasCd = w.resetsAt != null || w.countdownText != null;
    const cd = w.resetsAt
      ? `<span data-resets-at="${esc(String(w.resetsAt))}">${esc(w.countdownText ?? "")}</span>`
      : `<span>${esc(w.countdownText ?? "")}</span>`;
    const pace = (w.kind === "7d" && acct.pace && acct.pace.aheadOfPace)
      ? `<span class="pace">ahead of pace</span>` : "";
    return `
    <div class="qrow">
      <div class="top">
        <span class="lbl">${esc(w.label)}</span>
        <span class="pct num ${cls}">${w.pct.toFixed(0)}<span class="unit">USED</span></span>
      </div>
      <div class="bar"><i class="${cls}${w.state === "stale" ? " stale" : ""}"
        style="width:${Math.min(100, Math.max(0, w.pct))}%"></i></div>
      ${hasCd ? `<div class="cd">resets ${cd}${pace}</div>` : ""}
    </div>`;
  };

  const secondaryHtml = secondary.length
    ? `<details class="disclosure">
        <summary>${ic("chevron", 11, "chev")} Per-model limits</summary>
        ${secondary.map(row).join("")}
      </details>`
    : "";

  const spend = acct.spend
    ? `<div class="kv"><span class="k">Spend this period</span>
        <span class="v num">$${acct.spend.used.toFixed(2)}${
          acct.spend.limit != null
            ? ` / $${acct.spend.limit % 1 === 0
                ? acct.spend.limit.toFixed(0) : acct.spend.limit.toFixed(2)}`
            : ""
        }</span></div>`
    : "";

  return `${note}
  <div class="quota-card">
    <h3 class="section-h">Usage</h3>
    ${primary.map(row).join("")}
    ${secondaryHtml}
    ${spend}
  </div>`;
}

function actionsHtml(acct) {
  const cant = acct.active || !acct.switchable || acct.status === "needs-login";
  const pending = state.pendingAction === "switch";
  const label = pending ? "Switching…"
    : acct.active ? `${ic("check", 12)} Current account`
    : `Switch to ${esc(acct.alias ?? acct.label)}`;
  return `
  <div class="actions">
    <button class="btn primary" data-act="switch" data-slot="${esc(acct.slot)}"
      ${cant || pending ? "disabled" : ""}>${label}</button>
    <button class="btn half" data-act="best" title="Switch to the account with most headroom">
      ${ic("best", 12)} Best
    </button>
    <button class="btn half" data-act="rotate" title="Rotate to the next account in order">
      ${ic("rotate", 12)} Rotate
    </button>
  </div>`;
}

function autoswitchHtml() {
  const as = state.vm.autoSwitch;
  const pending = state.pendingAction === "setAutoSwitch";
  return `
  <div class="autoswitch">
    <div class="copy">
      <div class="l1">Auto-switch</div>
      <div class="l2">${as.enabled ? "on" : "off"} · at ${esc(String(as.thresholdPct))}% used · ${esc(as.strategy)}${
        as.lastEventText ? ` · ${esc(as.lastEventText)}` : ""
      }</div>
    </div>
    <button class="toggle" role="switch" aria-checked="${as.enabled}"
      aria-label="Auto-switch accounts" data-act="autoswitch" ${pending ? "disabled" : ""}></button>
  </div>`;
}

function footerHtml() {
  const fresh = state.vm.freshness || {};
  const dotCls = fresh.ok ? "" : fresh.error ? "bad" : "stale";
  return `
  <footer class="foot">
    <span class="fresh-dot ${dotCls}"></span>
    <span>${fresh.ageText ? `Updated ${esc(fresh.ageText)}` : "No usage yet"}</span>
    <span class="spacer"></span>
    <button data-act="activity">Activity ›</button>
    <button data-act="add">Add account</button>
  </footer>`;
}

// ---------------------------------------------------------------- wiring ---

// Explicit keyboard activation: some engines' automation (and any embedder
// that suppresses default actions) don't synthesize clicks from Enter/Space
// on buttons; making activation explicit guarantees keyboard operability.
els.panel.addEventListener("keydown", (ev) => {
  if (ev.key !== "Enter" && ev.key !== " ") return;
  const btn = ev.target.closest("button[data-act]");
  if (!btn || btn.disabled) return;
  ev.preventDefault();
  btn.click();
});

function wire() {
  els.panel.querySelectorAll("[data-act]").forEach((el) => {
    el.addEventListener("click", (ev) => {
      ev.stopPropagation();
      const act = el.dataset.act;
      const slot = el.dataset.slot;
      switch (act) {
        case "select":
          state.selectedSlot = slot;
          render();
          break;
        case "refresh": {
          el.classList.add("busy");
          doRefresh(el);
          break;
        }
        case "switch":
          doAction(el, "switch", { slot }, `switched to ${slot}`);
          break;
        case "best":
          doAction(el, "best", {}, "switched to best account");
          break;
        case "rotate":
          doAction(el, "rotate", {}, "rotated");
          break;
        case "autoswitch": {
          const target = !state.vm.autoSwitch.enabled;
          doAction(el, "setAutoSwitch", { enabled: target }, null).then((r) => {
            if (r.ok && !bridge.hosted) {
              state.vm.autoSwitch.enabled = target;
              render();
            }
          });
          break;
        }
        case "add":
          if (window.CSWAP_SHEETS && window.CSWAP_SHEETS.openToken) {
            window.CSWAP_SHEETS.openToken(el);
          } else {
            toast("add-account sheet arrives with the sheets module");
          }
          break;
        case "add-login":
          // empty-roster entry point (board 05): same backend path the
          // token sheet's login button uses
          doAction(el, "addFromLogin", {}, "account added from current login");
          break;
        case "activity":
          if (window.CSWAP_SHEETS && window.CSWAP_SHEETS.openActivity) {
            window.CSWAP_SHEETS.openActivity(el, state.vm.history);
          } else {
            toast("activity sheet arrives with the sheets module");
          }
          break;
        case "gear": {
          const target = selectedAccount() ?? activeAccount();
          if (window.CSWAP_SHEETS && window.CSWAP_SHEETS.openOverflow && target) {
            window.CSWAP_SHEETS.openOverflow(el, target);
          }
          break;
        }
      }
    });
  });
}

async function doRefresh(btn) {
  await bridge.send("refresh", {});
  if (!bridge.hosted) toast("refresh requested");
  render(); // drop the spinner state either way
}

async function doAction(el, action, payload, successText) {
  if (state.pendingAction) return { ok: false };  // no duplicate submission
  state.pendingAction = action;
  if (el) el.disabled = true;
  render(); // paint pending state (button label / toggle disabled)
  const res = await bridge.send(action, payload);
  state.pendingAction = null;
  if (!res) {
    refresh();
    return { ok: true };   // fixture mode: already logged
  }
  if (res.ok) {
    if (successText) toast(successText);
    refresh();
    return { ok: true };
  }
  toast(res.error || `${action} failed`, true);
  render();
  return { ok: false };
}

function refresh() {
  bridge.send("getSnapshot", {}).then((res) => {
    if (res && res.ok) bridge.push("vm", res.data);
    else if (!bridge.hosted) render(); // fixture: just repaint
  });
}

// countdowns tick locally; a countdown reaching zero shows AWAITING, never 0%
setInterval(() => {
  const now = Date.now() / 1000;
  els.panel.querySelectorAll("[data-resets-at]").forEach((el_) => {
    const cd = countdownFrom(parseFloat(el_.dataset.resetsAt), now);
    el_.textContent = cd ?? AWAITING;
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

const FIXTURE_NOW = Date.now() / 1000;
const _fx = (offset) => Math.round(FIXTURE_NOW + offset);
const FIXTURE = {
  schemaVersion: 1,
  activeSlot: "1",
  takenAt: _fx(-120),
  freshness: { ageText: "2m ago", ok: true },
  accounts: [
    {
      slot: "1", label: "alex", email: "alex@example.com", alias: "work",
      org: "Acme", kind: "oauth", active: true, switchable: true, status: "ok",
      windows: [
        { kind: "5h", label: "Five-hour", pct: 68, state: "ok",
          resetsAt: _fx(54 * 60), countdownText: "54m" },
        { kind: "7d", label: "Weekly", pct: 41, state: "ok",
          resetsAt: _fx((5 * 24 + 14) * 3600), countdownText: "5d 14h" },
        { kind: "model:Fable", label: "Fable", pct: 84, state: "ok",
          resetsAt: _fx((2 * 24 + 1) * 3600), countdownText: "2d 1h" },
      ],
      spend: { used: 12.4, limit: 100, pct: 12.4, currency: "USD" },
      pace: { aheadOfPace: true, expectedPct: 21.4 },
    },
    {
      slot: "2", label: "alex.research", email: "alexandra.research@example.com",
      alias: "research", org: "Acme Research and Platform Engineering",
      kind: "oauth", active: false,
      switchable: true, status: "ok",
      windows: [
        { kind: "5h", label: "Five-hour", pct: 12, state: "ok",
          resetsAt: _fx(2 * 3600), countdownText: "2h" },
        { kind: "7d", label: "Weekly", pct: 9, state: "ok",
          resetsAt: _fx((6 * 24 + 2) * 3600), countdownText: "6d 2h" },
      ],
    },
    {
      slot: "3", label: "backup", email: "backup@example.com", alias: "backup",
      org: "personal", kind: "oauth", active: false, switchable: true,
      status: "needs-login",
      note: "re-login needed — refresh token dead; log in with Claude Code, then run: cswap add",
      windows: [],
    },
  ],
  autoSwitch: { enabled: true, thresholdPct: 90, strategy: "best",
                lastEventText: "2 → 1 · 18m ago" },
  history: ["2 → 1 · 18m ago", "1 → 2 · 3h ago"],
};

window.CSWAP_APPEARANCE.init(bridge);

if (!bridge.hosted) {
  console.log("[fixture] claude-swap panel — fixture mode; actions log here");
  bridge.push("vm", FIXTURE);
} else {
  refresh();
}
