"""The desktop window's page: one self-contained HTML/CSS/JS document.

Kept as a string constant (not a data file) so the wheel and the PyInstaller
exe need no extra packaging. It talks to Python only through
``window.pywebview.api`` (``claude_swap.desktop.DesktopApi``).
"""

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>cswap</title>
<style>
  :root {
    --bg: #1b1b1f; --card: #232228; --card-hi: #29282f; --raise: #2c2b32; --raise-hi: #36353d;
    --fg: #f2f0e3; --muted: #9b98a3; --faint: #6c6974; --accent: #f76f53; --danger: #f05a5f;
    --track: #33323a; --line: rgba(255, 255, 255, .06); --inset: #141417;
    --ease: cubic-bezier(.2, .8, .2, 1);
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font: 13px/1.45 "Segoe UI Variable Text", "Segoe UI", system-ui, -apple-system, sans-serif;
    -webkit-font-smoothing: antialiased; user-select: none; cursor: default;
    display: flex; flex-direction: column; overflow: hidden;
  }
  button { font: inherit; color: inherit; }

  header { display: flex; align-items: center; gap: 8px; padding: 20px 22px 14px; }
  h1 { flex: 1; margin: 0; font: 400 24px/1 Georgia, "Times New Roman", serif; letter-spacing: -.4px; }
  h1 em { color: var(--accent); }

  .btn {
    border: 0; border-radius: 999px; padding: 0 15px; height: 32px; cursor: pointer;
    background: var(--raise); box-shadow: inset 0 0 0 1px var(--line); font-weight: 500; font-size: 12.5px;
    display: inline-flex; align-items: center; gap: 6px;
    transition: background .15s, transform .12s var(--ease), opacity .15s, box-shadow .15s;
  }
  .btn:hover { background: var(--raise-hi); }
  .btn:active { transform: scale(.96); }
  .btn:focus-visible { outline: none; box-shadow: inset 0 0 0 1px var(--line), 0 0 0 3px rgba(247,111,83,.35); }
  .btn.primary { background: var(--fg); color: #1b1b1f; box-shadow: 0 1px 2px rgba(0,0,0,.35); }
  .btn.primary:hover { background: #fff; }
  .btn.icon { width: 32px; padding: 0; justify-content: center; }
  .btn svg { width: 15px; height: 15px; }
  .btn.spin svg { animation: spin .8s linear infinite; }
  body.busy [data-busy] { opacity: .4; pointer-events: none; }

  .auto { display: inline-flex; align-items: center; gap: 8px; margin-right: 6px; color: var(--muted); font-size: 12px; cursor: pointer; }
  .switch { position: relative; width: 36px; height: 20px; border-radius: 999px; background: var(--track); transition: background .2s; }
  .switch::after {
    content: ""; position: absolute; top: 3px; left: 3px; width: 14px; height: 14px; border-radius: 50%;
    background: var(--muted); transition: transform .25s var(--ease), background .2s;
  }
  .auto.on .switch { background: var(--accent); }
  .auto.on .switch::after { transform: translateX(16px); background: #fff; }

  main { flex: 1; overflow-y: auto; padding: 4px 22px 8px; }
  /* Scroll with wheel/touch/keys, but never show a scrollbar. */
  main, #log { scrollbar-width: none; }
  main::-webkit-scrollbar, #log::-webkit-scrollbar { display: none; }

  .card {
    background: var(--card); border-radius: 16px; padding: 16px 18px 18px; margin-bottom: 12px;
    box-shadow: inset 0 0 0 1px var(--line), 0 1px 3px rgba(0,0,0,.35);
    transition: background .18s, transform .18s var(--ease), box-shadow .18s;
  }
  .intro .card { animation: rise .35s var(--ease) both; }  /* first paint only, never on refresh */
  .card:hover { background: var(--card-hi); transform: translateY(-1px); }
  .card.active { box-shadow: inset 0 0 0 1px rgba(247,111,83,.35), 0 1px 3px rgba(0,0,0,.35); }
  .card.disabled .name { color: var(--muted); }
  .top { display: flex; align-items: center; gap: 12px; }
  .dot { flex: none; width: 8px; height: 8px; border-radius: 50%; background: var(--track); }
  .active .dot { background: var(--accent); }
  .who { flex: 1; min-width: 0; }
  .name { font-weight: 600; font-size: 14px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .tag { color: var(--muted); font-size: 11.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .acts { display: flex; align-items: center; gap: 6px; }
  .chip { border-radius: 999px; padding: 3px 10px; font-size: 11px; font-weight: 600; background: rgba(247,111,83,.14); color: var(--accent); }
  .chip.dim { background: var(--raise); color: var(--muted); }

  .bars { margin: 14px 0 0 20px; display: grid; grid-template-columns: 22px 1fr 38px 128px; gap: 9px 10px; align-items: center; }
  .lbl { color: var(--muted); font-size: 11.5px; }
  .track { height: 6px; border-radius: 99px; background: var(--track); overflow: hidden; }
  .fill { height: 100%; width: 0; border-radius: 99px; background: var(--accent); transition: width .7s var(--ease), background .3s; }
  .fill.hot { background: var(--danger); }
  .pct { text-align: right; font-variant-numeric: tabular-nums; font-weight: 500; }
  .meta { color: var(--muted); font-size: 11.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .msg { margin: 12px 0 0 20px; color: var(--muted); }
  .empty { text-align: center; color: var(--muted); padding: 60px 0; }
  .empty b { display: block; font: 400 20px Georgia, serif; color: var(--fg); margin-bottom: 6px; }

  footer { padding: 6px 22px 18px; }
  .foot { display: flex; gap: 6px; }
  .foot .grow { flex: 1; }
  .dialog label { display: flex; align-items: center; gap: 8px; color: var(--muted); margin-bottom: 12px; cursor: pointer; }
  .dialog input[type=checkbox] { width: 16px; height: 16px; margin: 0; accent-color: var(--accent); box-shadow: none; }
  .warn { color: var(--fg) !important; background: rgba(240,90,95,.12); border-radius: 10px; padding: 10px 12px; }
  .chev { transition: transform .25s var(--ease); }
  .open .chev { transform: rotate(180deg); }
  #panel { display: grid; grid-template-rows: 0fr; transition: grid-template-rows .3s var(--ease); }
  #panel.open { grid-template-rows: 1fr; }
  #panel > div { overflow: hidden; }
  #log {
    margin: 10px 0 0; height: 170px; overflow: auto; padding: 10px 12px; border-radius: 12px;
    background: var(--inset); box-shadow: inset 0 0 0 1px var(--line);
    font: 11.5px/1.6 "Cascadia Mono", Consolas, "SF Mono", Menlo, monospace; color: #cfcbd6;
    white-space: pre-wrap; user-select: text; cursor: text;
  }
  #log .t { color: var(--faint); }
  #log .alert { color: var(--danger); }

  .menu {
    position: fixed; z-index: 10; min-width: 168px; padding: 6px; border-radius: 12px;
    background: #2a2930; box-shadow: inset 0 0 0 1px var(--line), 0 16px 40px rgba(0,0,0,.55);
    animation: pop .14s var(--ease);
  }
  .menu button { display: block; width: 100%; text-align: left; border: 0; background: none; padding: 8px 10px; border-radius: 8px; cursor: pointer; }
  .menu button:hover { background: var(--raise-hi); }
  .menu .danger { color: var(--danger); }
  .menu hr { border: 0; height: 1px; background: var(--line); margin: 5px 4px; }

  .scrim {
    position: fixed; inset: 0; z-index: 20; display: grid; place-items: center;
    background: rgba(10,10,12,.55); backdrop-filter: blur(8px); animation: fade .16s ease-out;
  }
  .dialog {
    width: min(360px, 86vw); padding: 22px; border-radius: 18px; background: var(--card);
    box-shadow: inset 0 0 0 1px var(--line), 0 24px 60px rgba(0,0,0,.6); animation: pop .18s var(--ease);
  }
  .dialog h2 { margin: 0 0 6px; font: 400 20px Georgia, serif; }
  .dialog p { margin: 0 0 16px; color: var(--muted); }
  .dialog input {
    width: 100%; height: 38px; margin-bottom: 10px; padding: 0 12px; border: 0; border-radius: 10px;
    background: var(--bg); color: var(--fg); font: inherit; box-shadow: inset 0 0 0 1px var(--line);
    outline: none; transition: box-shadow .15s; user-select: text;
  }
  .dialog input:focus { box-shadow: inset 0 0 0 1px var(--accent), 0 0 0 3px rgba(247,111,83,.18); }
  .row-end { display: flex; justify-content: flex-end; gap: 8px; margin-top: 6px; }
  .btn.danger { background: var(--danger); color: #fff; }
  .btn.danger:hover { background: #ff6b70; }

  @keyframes spin { to { transform: rotate(360deg); } }
  @keyframes rise { from { opacity: 0; transform: translateY(6px); } }
  @keyframes pop { from { opacity: 0; transform: scale(.96) translateY(-4px); } }
  @keyframes fade { from { opacity: 0; } }
  @media (prefers-reduced-motion: reduce) { * { animation: none !important; transition: none !important; } }
</style>
</head>
<body>
<header>
  <h1>c<em>swap</em></h1>
  <label class="auto" id="auto" title="Auto-switch when an account runs low">Auto <span class="switch"></span></label>
  <button class="btn" id="add" data-busy>
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M8 3v10M3 8h10"/></svg>Add
  </button>
  <button class="btn primary" id="best" data-busy title="Switch to the account with the most room">Best</button>
  <button class="btn icon" id="refresh" title="Refresh usage">
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M13.5 8a5.5 5.5 0 1 1-1.6-3.9"/><path d="M13.5 2.5v3h-3"/></svg>
  </button>
</header>
<main id="cards"></main>
<footer>
  <div class="foot">
    <button class="btn" id="out-toggle">Output
      <svg class="chev" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4 10l4-4 4 4"/></svg>
    </button>
    <span class="grow"></span>
    <button class="btn" id="import" data-busy title="Import accounts from an export file">
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M8 2.5v8M4.5 7 8 10.5 11.5 7M3 13.5h10"/></svg>Import
    </button>
    <button class="btn" id="export" data-busy title="Save all accounts to a file">
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M8 10.5v-8M4.5 6 8 2.5 11.5 6M3 13.5h10"/></svg>Export
    </button>
  </div>
  <div id="panel"><div><pre id="log"></pre></div></div>
</footer>
<script>
  const $ = (s) => document.querySelector(s);
  const api = () => window.pywebview.api;
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const icon = {
    more: '<svg viewBox="0 0 16 16" fill="currentColor"><circle cx="3.5" cy="8" r="1.3"/><circle cx="8" cy="8" r="1.3"/><circle cx="12.5" cy="8" r="1.3"/></svg>',
  };
  let accounts = [];
  const lastPct = new Map();  // "num:label" -> pct, so bars animate from where they were

  // ---- output panel --------------------------------------------------
  function setPanel(open) {
    $("#panel").classList.toggle("open", open);
    $("#out-toggle").classList.toggle("open", open);
  }
  $("#out-toggle").onclick = () => setPanel(!$("#panel").classList.contains("open"));
  function log(text, alert = false, stamp = true) {
    const out = $("#log");
    for (const line of String(text).split(/\r?\n/).filter((l) => l.trim())) {
      const t = stamp ? `<span class="t">${new Date().toTimeString().slice(0, 8)}</span> ` : "";
      out.insertAdjacentHTML("beforeend", `${t}<span class="${alert ? "alert" : ""}">${esc(line)}</span>\n`);
    }
    out.scrollTop = out.scrollHeight;
    if (alert) setPanel(true);
  }
  window.cswapLog = log;

  // ---- cards ---------------------------------------------------------
  let painted = "", firstPaint = true;
  function render() {
    const main = $("#cards");
    const key = JSON.stringify(accounts);
    if (key === painted) return;  // nothing changed: don't touch the DOM (no flicker)
    painted = key;
    main.classList.toggle("intro", firstPaint);
    firstPaint = false;
    if (!accounts.length) {
      main.innerHTML = '<div class="empty"><b>No accounts yet</b>Log in with Claude Code, then use Add.</div>';
      return;
    }
    main.innerHTML = accounts.map((a, i) => {
      const right = [
        a.active ? '<span class="chip">active</span>'
          : a.switchable ? `<button class="btn primary" data-busy data-switch="${esc(a.num)}">Switch</button>` : "",
        a.disabled ? '<span class="chip dim">disabled</span>' : "",
        `<button class="btn icon" data-busy data-more="${esc(a.num)}" title="More">${icon.more}</button>`,
      ].join("");
      const bars = a.rows.length ? `<div class="bars">${a.rows.map((r) => `
          <span class="lbl">${esc(r.label)}</span>
          <div class="track"><div class="fill${r.pct >= 90 ? " hot" : ""}" data-key="${esc(a.num)}:${esc(r.label)}" data-pct="${r.pct ?? 0}"
               style="width:${lastPct.get(`${a.num}:${r.label}`) ?? 0}%"></div></div>
          <span class="pct">${r.pct == null ? "—" : Math.round(r.pct) + "%"}</span>
          <span class="meta">${esc([r.reset, r.age].filter(Boolean).join("  "))}</span>`).join("")}</div>` : "";
      return `<article class="card${a.active ? " active" : ""}${a.disabled ? " disabled" : ""}" style="animation-delay:${i * 40}ms">
          <div class="top"><span class="dot"></span>
            <div class="who"><div class="name">${esc(a.title)}</div><div class="tag">${esc(a.tag)}</div></div>
            <div class="acts">${right}</div></div>
          ${bars}${a.message ? `<div class="msg">${esc(a.message)}</div>` : ""}</article>`;
    }).join("");
    requestAnimationFrame(() => requestAnimationFrame(() => {
      for (const f of document.querySelectorAll(".fill")) {
        const pct = Math.min(100, Number(f.dataset.pct));
        f.style.width = pct + "%";
        lastPct.set(f.dataset.key, pct);
      }
    }));
  }

  let refreshing = false, refreshAgain = false;
  async function refresh() {
    if (refreshing) { refreshAgain = true; return; }
    refreshing = true;
    $("#refresh").classList.add("spin");
    try {
      const r = await api().snapshot();
      if (r.error) log(`refresh failed: ${r.error}`, true);
      else { accounts = r.accounts; render(); }
    } finally {
      refreshing = false;
      $("#refresh").classList.remove("spin");
      if (refreshAgain) { refreshAgain = false; refresh(); }
    }
  }

  // ---- actions -------------------------------------------------------
  async function act(name, ...args) {
    if (document.body.classList.contains("busy")) return;
    document.body.classList.add("busy");
    try {
      const r = await api()[name](...args);
      if (r.output) log(r.output, !r.ok);
    } catch (e) {
      log(String(e), true);
    } finally {
      document.body.classList.remove("busy");
      refresh();
    }
  }

  function closeMenus() { document.querySelectorAll(".menu").forEach((m) => m.remove()); }
  function menu(anchor, items) {
    closeMenus();
    const m = document.createElement("div");
    m.className = "menu";
    m.innerHTML = items.map((it, i) => it === "-" ? "<hr>" :
      `<button data-i="${i}" class="${it.danger ? "danger" : ""}">${esc(it.label)}</button>`).join("");
    document.body.append(m);
    const r = anchor.getBoundingClientRect();
    m.style.top = Math.min(r.bottom + 6, innerHeight - m.offsetHeight - 8) + "px";
    m.style.left = Math.max(8, r.right - m.offsetWidth) + "px";
    m.onclick = (e) => {
      const b = e.target.closest("button[data-i]");
      if (b) { closeMenus(); items[b.dataset.i].run(); }
    };
    setTimeout(() => document.addEventListener("click", closeMenus, {once: true}));
  }

  function dialog(html, onSubmit) {
    const s = document.createElement("div");
    s.className = "scrim";
    s.innerHTML = `<form class="dialog">${html}</form>`;
    document.body.append(s);
    const close = () => s.remove();
    s.onclick = (e) => { if (e.target === s || e.target.dataset.cancel != null) close(); };
    s.querySelector("form").onsubmit = (e) => { e.preventDefault(); close(); onSubmit(new FormData(e.target)); };
    document.addEventListener("keydown", function k(e) { if (e.key === "Escape") { close(); document.removeEventListener("keydown", k); } });
    (s.querySelector("input") || s.querySelector("[data-cancel]")).focus();  // never default to a destructive button
  }

  $("#cards").onclick = (e) => {
    const sw = e.target.closest("[data-switch]");
    if (sw) return act("switch", sw.dataset.switch);
    const more = e.target.closest("[data-more]");
    if (!more) return;
    e.stopPropagation();
    const a = accounts.find((x) => String(x.num) === more.dataset.more);
    menu(more, [
      {label: "Switch to this account", run: () => act("switch", a.num)},
      {label: a.disabled ? "Enable in rotation" : "Disable in rotation", run: () => act("set_disabled", a.num, !a.disabled)},
      "-",
      {label: "Remove…", danger: true, run: () => dialog(
        `<h2>Remove account?</h2><p>${esc(a.title)} will be removed from cswap.</p>
         <div class="row-end"><button type="button" class="btn" data-cancel>Cancel</button><button type="submit" class="btn danger">Remove</button></div>`,
        () => act("remove", a.num))},
    ]);
  };

  $("#add").onclick = (e) => {
    e.stopPropagation();
    menu($("#add"), [
      {label: "Current Claude login", run: () => act("add_current")},
      {label: "From setup token…", run: () => dialog(
        `<h2>Add from token</h2><p>Paste a setup token (sk-ant-oat01-…) and its email.</p>
         <input name="email" placeholder="Email" autocomplete="off" required>
         <input name="token" type="password" placeholder="Setup token" autocomplete="off" required>
         <div class="row-end"><button type="button" class="btn" data-cancel>Cancel</button><button type="submit" class="btn primary">Add</button></div>`,
        (f) => act("add_token", f.get("email"), f.get("token")))},
    ]);
  };
  $("#best").onclick = () => act("best");
  $("#export").onclick = () => dialog(
    `<h2>Export accounts</h2>
     <p class="warn">The file holds your Claude login tokens in plain text. Anyone who has it can use these accounts — keep it private and delete it after importing.</p>
     <div class="row-end"><button type="button" class="btn" data-cancel>Cancel</button><button type="submit" class="btn primary">Choose file…</button></div>`,
    () => act("export"));
  $("#import").onclick = () => dialog(
    `<h2>Import accounts</h2><p>Pick a file made with Export (or <code>cswap export</code>).</p>
     <label><input type="checkbox" name="replace"> Replace accounts that already exist</label>
     <div class="row-end"><button type="button" class="btn" data-cancel>Cancel</button><button type="submit" class="btn primary">Choose file…</button></div>`,
    (f) => act("import_", f.get("replace") === "on"));
  $("#refresh").onclick = () => refresh();
  $("#auto").onclick = async (e) => {
    e.preventDefault();
    const want = !$("#auto").classList.contains("on");
    const r = await api().auto(want);
    $("#auto").classList.toggle("on", r.on);
    if (r.output) log(r.output, true);
    else log(r.on ? "auto-switch on" : "auto-switch off");
  };

  // ---- loops ---------------------------------------------------------
  async function poll() {
    try {
      const p = await api().poll();
      for (const l of p.lines) log(l.text, l.alert, false);
      $("#auto").classList.toggle("on", p.auto);
      if (p.refresh) refresh();
    } finally {
      setTimeout(poll, 500);
    }
  }
  window.addEventListener("pywebviewready", () => {
    refresh();
    setInterval(refresh, 60000);
    poll();
  });
</script>
</body>
</html>
"""
