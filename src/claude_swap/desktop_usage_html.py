"""The HTML page of the desktop usage report: one self-contained file, charts drawn as inline SVG.

The data goes in as JSON and the page does the arithmetic, so the time-range filter can re-slice
everything without a round trip. Palette: the validated default categorical order, unmodified
(accounts take slots 1 and 2, token kinds slots 3 to 6), light and dark.
"""

from __future__ import annotations

import json

from claude_swap.desktop_usage import WEIGHTS, Report


def _ms(dt) -> int:
    return int(dt.timestamp() * 1000)


def _project(cwd: str) -> str:
    leaf = cwd.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return leaf or "(unknown)"


def payload(report: Report) -> dict:
    accounts = sorted({s for s in report.samples} | {r.account for r in report.requests if r.account})
    samples = []
    for account, rows in report.samples.items():
        for s in rows:
            samples.append({
                "t": _ms(s.ts), "a": account,
                "w": s.week["pct"] if s.week else None,
                "h": s.five["pct"] if s.five else None,
                "f": next((v["pct"] for k, v in s.scoped.items() if "fable" in k.lower()), None),
                "spend": s.spend["used"] if s.spend and s.spend.get("used") is not None else None,
                "limit": s.spend["limit"] if s.spend and s.spend.get("limit") is not None else None,
            })
    requests = [{
        "t": _ms(r.ts), "a": r.account, "m": r.model.replace("claude-", "") or "(unknown)",
        "p": _project(r.cwd), "k": r.counted, "e": r.entrypoint, "x": r.sidechain,
        "i": r.tokens["input"], "o": r.tokens["output"], "r": r.tokens["cache_read"],
        "c5": r.tokens["cache_5m"], "c1": r.tokens["cache_1h"],
    } for r in report.requests]
    return {
        "generated": _ms(report.generated),
        "first": _ms(report.first_request) if report.first_request else None,
        "accounts": [{"email": a, "name": a.split("@")[0]} for a in accounts],
        "weights": WEIGHTS,
        "samples": sorted(samples, key=lambda s: s["t"]),
        "requests": requests,
    }


def render(report: Report) -> str:
    data = json.dumps(payload(report), separators=(",", ":")).replace("</", "<\\/")
    return TEMPLATE.replace("__DATA__", data)


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Desktop usage by account</title>
<style>
:root{color-scheme:light;--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;--grid:#e1e0d9;--axis:#c3c2b7;--border:rgba(11,11,11,.10);
--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--s5:#e87ba4;--s6:#008300;--s7:#6250d6;--s8:#e34948}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--s6:#008300;--s7:#9085e9;--s8:#e66767}}
:root[data-theme="dark"]{color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--s6:#008300;--s7:#9085e9;--s8:#e66767}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1120px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:22px;font-weight:600;margin:0 0 4px}
h2{font-size:16px;font-weight:600;margin:0 0 2px}
.sub{color:var(--ink2);margin:0 0 16px}
.muted{color:var(--muted)}
.filters{display:flex;gap:8px;align-items:center;margin:0 0 16px;flex-wrap:wrap}
.filters button{font:inherit;color:var(--ink2);background:transparent;border:1px solid var(--border);border-radius:8px;padding:5px 12px;cursor:pointer}
.filters button:hover{background:var(--grid)}
.filters button[aria-pressed="true"]{color:var(--ink);border-color:var(--ink2);font-weight:600}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:16px;margin:0 0 16px}
.grid{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}
main>.grid{margin:0 0 16px}
.tile{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:16px}
.tile .who{display:flex;align-items:center;gap:8px;font-weight:600}
.dot{width:10px;height:10px;border-radius:50%;flex:none}
.tile .hero{font-size:40px;font-weight:600;line-height:1.1;margin:10px 0 2px}
.tile .unit{color:var(--ink2)}
.tile dl{display:grid;grid-template-columns:1fr auto;gap:2px 12px;margin:12px 0 0;color:var(--ink2)}
.tile dt{color:var(--muted)}
.tile dd{margin:0;text-align:right;color:var(--ink)}
svg{display:block;width:100%;height:auto;overflow:visible}
svg text{fill:var(--muted);font:12px system-ui,-apple-system,"Segoe UI",sans-serif}
svg text.ink{fill:var(--ink2)}
svg text.val{fill:var(--ink);font-weight:600}
.legend{display:flex;gap:16px;flex-wrap:wrap;margin:8px 0 0;color:var(--ink2);font-size:13px}
.legend span{display:inline-flex;align-items:center;gap:6px}
.legend i{width:12px;height:8px;border-radius:2px;display:inline-block}
.legend i.ln{height:2px;width:14px;border-radius:1px}
.empty{color:var(--muted);padding:24px 0}
#tip{position:fixed;pointer-events:none;background:var(--surface);color:var(--ink);border:1px solid var(--border);box-shadow:0 4px 16px rgba(0,0,0,.18);border-radius:8px;padding:8px 10px;font-size:12px;z-index:9;display:none;max-width:280px}
#tip .t{color:var(--ink2);margin-bottom:4px}
#tip .r{display:flex;align-items:center;gap:6px;white-space:nowrap}
#tip .r b{margin-left:auto;padding-left:12px;font-weight:600}
#tip .k{width:10px;height:2px;border-radius:1px;flex:none}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{text-align:right;padding:6px 8px;border-bottom:1px solid var(--grid)}
th:first-child,td:first-child{text-align:left}
th{color:var(--muted);font-weight:500}
details summary{cursor:pointer;color:var(--ink2)}
.note{color:var(--ink2);font-size:13px;margin:6px 0 0}
</style>
</head>
<body>
<main>
<h1>Desktop usage by account</h1>
<p class="sub" id="sub"></p>
<div class="filters" role="group" aria-label="Time range"></div>
<section id="tiles" class="grid"></section>
<section class="card"><h2>Tokens per 1% of the weekly limit</h2><p class="sub">Input-equivalent tokens spent for each point the weekly percentage moved. A longer bar is a bigger budget.</p><div id="perpct"></div></section>
<section class="card"><h2>Weekly percentage over time</h2><p class="sub">Each account's weekly usage, as cswap read it.</p><div id="weekline"></div><div class="legend" id="weeklegend"></div></section>
<section class="card"><h2>Weekly percentage against tokens</h2><p class="sub">Percentage points gained against tokens spent since the first reading. A flatter line is a bigger budget per point.</p><div id="climb"></div><div class="legend" id="climblegend"></div></section>
<section class="card"><h2>Tokens per day</h2><p class="sub">Input-equivalent tokens by kind, one chart per account on the same scale.</p><div id="daily" class="grid"></div><div class="legend" id="dailylegend"></div></section>
<section class="grid">
<div class="card"><h2>By model</h2><p class="sub">Input-equivalent tokens.</p><div id="models"></div></div>
<div class="card"><h2>By project</h2><p class="sub">Input-equivalent tokens, by working folder.</p><div id="projects"></div></div>
</section>
<section class="card"><h2>Fable, its own weekly limit</h2><p class="sub">Fable has a weekly percentage of its own. Tokens here are Fable requests only.</p><div id="fabletiles" class="grid"></div><div id="fableperpct" style="margin-top:16px"></div><div id="fableline" style="margin-top:16px"></div></section>
<section class="card"><details><summary>Table view and how to read this</summary><div id="table" style="margin-top:12px"></div><div id="notes"></div></details></section>
</main>
<div id="tip" role="tooltip"></div>
<script type="application/json" id="data">__DATA__</script>
<script>
(() => {
const D = JSON.parse(document.getElementById('data').textContent);
const W = D.weights, NS = 'http://www.w3.org/2000/svg';
const $ = id => document.getElementById(id);
const accColor = e => `var(--s${D.accounts.findIndex(a => a.email === e) + 1})`;
const KIND = [
  { key: 'o', name: 'Output', w: W.output, color: 'var(--s3)' },
  { key: 'c', name: 'Cache write', w: 0, color: 'var(--s4)' },
  { key: 'i', name: 'Input', w: W.input, color: 'var(--s5)' },
  { key: 'r', name: 'Cache read', w: W.cache_read, color: 'var(--s6)' },
];
const wt = q => q.i * W.input + q.o * W.output + q.r * W.cache_read + q.c5 * W.cache_5m + q.c1 * W.cache_1h;
const kindW = (q, k) => k === 'o' ? q.o * W.output : k === 'i' ? q.i * W.input : k === 'r' ? q.r * W.cache_read : q.c5 * W.cache_5m + q.c1 * W.cache_1h;
const kindRaw = (q, k) => k === 'o' ? q.o : k === 'i' ? q.i : k === 'r' ? q.r : q.c5 + q.c1;
const fmt = n => n >= 1e9 ? (n / 1e9).toFixed(1) + 'B' : n >= 1e6 ? (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(n >= 1e5 ? 0 : 1) + 'K' : String(Math.round(n));
const pc = n => (Math.round(n * 10) / 10) + '%';
const two = n => String(n).padStart(2, '0');
const dayKey = t => { const d = new Date(t); return d.getFullYear() + '-' + two(d.getMonth() + 1) + '-' + two(d.getDate()); };
const stamp = t => { const d = new Date(t); return dayKey(t).slice(5) + ' ' + two(d.getHours()) + ':' + two(d.getMinutes()); };

function s(tag, attrs, parent) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) k === 'text' ? (e.textContent = v) : e.setAttribute(k, v);
  if (parent) parent.append(e);
  return e;
}
function h(tag, attrs, kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) k === 'text' ? (e.textContent = v) : e.setAttribute(k, v);
  for (const c of [].concat(kids || [])) e.append(c);
  return e;
}
const svg = (host, w, hgt) => { host.replaceChildren(); return s('svg', { viewBox: `0 0 ${w} ${hgt}`, role: 'img' }, host); };

const tip = $('tip');
function showTip(ev, title, rows) {
  tip.replaceChildren(h('div', { class: 't', text: title }));
  for (const r of rows) {
    const row = h('div', { class: 'r' });
    if (r.color) { const k = h('span', { class: 'k' }); k.style.background = r.color; k.style.height = r.box ? '8px' : '2px'; row.append(k); }
    row.append(h('span', { text: r.label }), h('b', { text: r.value }));
    tip.append(row);
  }
  tip.style.display = 'block';
  const pad = 14, bw = tip.offsetWidth, bh = tip.offsetHeight;
  tip.style.left = Math.min(ev.clientX + pad, innerWidth - bw - 8) + 'px';
  tip.style.top = Math.min(ev.clientY + pad, innerHeight - bh - 8) + 'px';
}
const hideTip = () => { tip.style.display = 'none'; };

let range = 'all';
const SPAN = { all: Infinity, '7d': 7 * 864e5, '24h': 864e5 };
const cutoff = () => D.generated - SPAN[range];

function climb(S, key) {
  let d = 0, prev = null;
  for (const x of S) {
    const v = x[key];
    if (v == null) continue;
    if (prev != null) d += v >= prev ? v - prev : v;
    prev = v;
  }
  return d;
}

function view(fable) {
  const t0 = cutoff();
  const key = fable ? 'f' : 'w';
  return D.accounts.map(a => {
    const S = D.samples.filter(x => x.a === a.email && x.t >= t0 && x[key] != null);
    let R = D.requests.filter(x => x.a === a.email && x.t >= t0);
    if (fable) R = R.filter(x => /fable/i.test(x.m));
    const first = S.length ? S[0].t : null, last = S.length ? S[S.length - 1].t : null;
    const seen = first == null ? [] : R.filter(x => x.t >= first && x.t <= last);
    const delta = climb(S, key);
    const tokens = seen.reduce((n, x) => n + wt(x), 0);
    return { a, S, R, seen, delta, tokens, first, last, now: S.length ? S[S.length - 1][key] : null,
      perPct: delta >= 1 ? tokens / delta : null, all: R.reduce((n, x) => n + wt(x), 0),
      blind: R.filter(x => !x.k).length };
  });
}

function legend(host, items, line) {
  host.replaceChildren(...items.map(i => {
    const sw = h('i', { class: line ? 'ln' : '' }); sw.style.background = i.color;
    return h('span', {}, [sw, document.createTextNode(i.name)]);
  }));
}
const empty = (host, text) => host.replaceChildren(h('div', { class: 'empty', text }));

function tiles(host, V, noun) {
  host.replaceChildren(...V.map(v => {
    const dot = h('span', { class: 'dot' }); dot.style.background = accColor(v.a.email);
    const hero = h('div', { class: 'hero', text: v.perPct == null ? 'not yet' : fmt(v.perPct) });
    const unit = h('div', { class: 'unit', text: v.perPct == null ? `the ${noun} percentage has moved ${pc(v.delta)} so far, it needs 1 point` : `input-equivalent tokens per 1% of the ${noun} limit` });
    const rows = [
      [`${noun[0].toUpperCase() + noun.slice(1)} now`, v.now == null ? 'no reading' : pc(v.now)],
      ['Gained in range', pc(v.delta)],
      ['Tokens in range', fmt(v.all)],
      ['Requests', `${v.R.length}` + (v.blind ? ` (${v.blind} without token data)` : '')],
    ];
    const dl = h('dl', {}, rows.flatMap(([k, x]) => [h('dt', { text: k }), h('dd', { text: x })]));
    return h('div', { class: 'tile' }, [h('div', { class: 'who' }, [dot, document.createTextNode(v.a.name)]), hero, unit, dl]);
  }));
}

function hbars(host, rows, opts) {
  // rows: [{label, values:[{color, value, tip}]}]; one band per row, bars for each account
  const W0 = Math.max(host.clientWidth, 280), pad = { l: opts.labelW || 150, r: 64 }, bandH = 14 * opts.series + 18;
  const total = rows.length * bandH + 8;
  const sv = svg(host, W0, Math.max(total, 40));
  const max = Math.max(1, ...rows.flatMap(r => r.values.map(v => v.value)));
  rows.forEach((r, ri) => {
    const y0 = ri * bandH + 4;
    s('text', { x: 0, y: y0 + (bandH - 8) / 2 + 4, class: 'ink', text: r.label.length > 22 ? r.label.slice(0, 21) + '…' : r.label }, sv);
    r.values.forEach((v, vi) => {
      const y = y0 + vi * 14, len = Math.max(v.value > 0 ? 2 : 0, (v.value / max) * (W0 - pad.l - pad.r));
      const bar = s('rect', { x: pad.l, y, width: len, height: 10, rx: 4, style: `fill:${v.color}` }, sv);
      s('rect', { x: pad.l, y, width: 4, height: 10, style: `fill:${v.color}` }, sv);
      s('text', { x: pad.l + len + 6, y: y + 9, class: 'val', text: v.text }, sv);
      const hit = s('rect', { x: 0, y: y - 2, width: W0, height: 14, fill: 'transparent', tabindex: 0 }, sv);
      const on = ev => { bar.style.opacity = .8; showTip(ev, r.label, v.tip); };
      hit.addEventListener('pointermove', on); hit.addEventListener('pointerleave', () => { bar.style.opacity = 1; hideTip(); });
      hit.addEventListener('focus', () => { const b = hit.getBoundingClientRect(); on({ clientX: b.left + 80, clientY: b.top }); });
      hit.addEventListener('blur', hideTip);
    });
  });
}

function perPctChart(host, V) {
  const ok = V.some(v => v.perPct != null);
  if (!ok) return empty(host, 'Not enough movement yet: the weekly percentage has to rise at least 1 point after the first reading. Check back after more use.');
  hbars(host, V.map(v => ({ label: v.a.name, values: [{
    color: accColor(v.a.email), value: v.perPct || 0, text: v.perPct == null ? 'not yet' : fmt(v.perPct),
    tip: [{ label: 'Tokens per 1%', value: v.perPct == null ? 'not yet' : fmt(v.perPct), color: accColor(v.a.email) },
          { label: 'Gained', value: pc(v.delta) }, { label: 'Tokens spent', value: fmt(v.tokens) }] }] })), { series: 1, labelW: 150 });
}

function niceMax(v) {
  const raw = Math.max(v, 1e-9) / 4, mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map(f => f * mag).find(x => x >= raw);
  return step * 4;
}

function ticks(t0, t1) {
  const n = 5, out = [];
  for (let i = 0; i <= n; i++) out.push(t0 + (t1 - t0) * i / n);
  return out;
}
const tickLabel = (t, span) => { const d = new Date(t); return span > 3 * 864e5 ? dayKey(t).slice(5) : two(d.getHours()) + ':' + two(d.getMinutes()); };

function lineChart(host, series, o) {
  // series: [{name,color,pts:[[x,y,extra]]}], step: draw readings as steps
  const all = series.flatMap(x => x.pts);
  if (!all.length) return empty(host, o.empty);
  const w = Math.max(host.clientWidth, 320), hgt = 260, m = { l: 44, r: 128, t: 12, b: 28 };
  const sv = svg(host, w, hgt);
  const x0 = o.zeroX ? 0 : Math.min(...all.map(p => p[0])), x1 = Math.max(...all.map(p => p[0]), x0 + 1);
  const yMax = o.yMax || niceMax(Math.max(...all.map(p => p[1])) * 1.05);
  const X = x => m.l + (x - x0) / (x1 - x0) * (w - m.l - m.r), Y = y => hgt - m.b - y / yMax * (hgt - m.t - m.b);
  for (let i = 0; i <= 4; i++) {
    const v = yMax * i / 4;
    s('line', { x1: m.l, x2: w - m.r, y1: Y(v), y2: Y(v), style: `stroke:${i ? 'var(--grid)' : 'var(--axis)'}`, 'stroke-width': 1 }, sv);
    s('text', { x: m.l - 8, y: Y(v) + 4, 'text-anchor': 'end', text: o.yFmt(v) }, sv);
  }
  ticks(x0, x1).forEach(t => s('text', { x: X(t), y: hgt - 8, 'text-anchor': 'middle', text: o.xFmt(t, x1 - x0) }, sv));
  series.forEach(se => {
    if (!se.pts.length) return;
    let d = '';
    se.pts.forEach((p, i) => { d += (i ? (o.step ? `H${X(p[0])}V${Y(p[1])}` : `L${X(p[0])} ${Y(p[1])}`) : `M${X(p[0])} ${Y(p[1])}`); });
    if (o.step) d += `H${X(x1)}`;
    s('path', { d, fill: 'none', 'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round', style: `stroke:${se.color}` }, sv);
    const last = se.pts[se.pts.length - 1];
    s('circle', { cx: X(o.step ? x1 : last[0]), cy: Y(last[1]), r: 4, style: `fill:${se.color};stroke:var(--surface)`, 'stroke-width': 2 }, sv);
    s('text', { x: X(o.step ? x1 : last[0]) + 9, y: Y(last[1]) + 4, class: 'val', text: se.name + ' ' + o.yFmt(last[1]) }, sv);
  });
  const cross = s('line', { y1: m.t, y2: hgt - m.b, style: 'stroke:var(--axis)', 'stroke-width': 1, visibility: 'hidden' }, sv);
  const hit = s('rect', { x: m.l, y: m.t, width: w - m.l - m.r, height: hgt - m.t - m.b, fill: 'transparent', tabindex: 0 }, sv);
  hit.addEventListener('pointermove', ev => {
    const r = sv.getBoundingClientRect(), px = (ev.clientX - r.left) / r.width * w;
    const xv = x0 + (px - m.l) / (w - m.l - m.r) * (x1 - x0);
    let best = null;
    for (const p of all) if (best == null || Math.abs(p[0] - xv) < Math.abs(best - xv)) best = p[0];
    cross.setAttribute('x1', X(best)); cross.setAttribute('x2', X(best)); cross.setAttribute('visibility', 'visible');
    const rows = series.map(se => {
      let cur = null;
      for (const p of se.pts) { if (p[0] <= best) cur = p; }
      return cur ? { label: se.name, value: o.tipVal(cur), color: se.color } : null;
    }).filter(Boolean);
    showTip(ev, o.tipTitle(best), rows);
  });
  hit.addEventListener('pointerleave', () => { cross.setAttribute('visibility', 'hidden'); hideTip(); });
}

function weekLine(host, lg, V, key, ename) {
  const series = V.map(v => ({ name: v.a.name, color: accColor(v.a.email), pts: v.S.map(x => [x.t, x[key]]) }));
  lineChart(host, series, { step: true, yFmt: pc, xFmt: tickLabel, tipTitle: stamp, tipVal: p => pc(p[1]),
    empty: `No ${ename} readings in this range yet. cswap's readings arrive every few minutes while the proxy runs.` });
  if (lg) legend(lg, series.map(x => ({ name: x.name, color: x.color })), true);
}

function climbChart(host, lg, V) {
  const series = V.map(v => {
    let cum = 0, gain = 0, prev = null, pts = [];
    const reqs = v.R.filter(x => v.first != null && x.t >= v.first).sort((a, b) => a.t - b.t);
    let ri = 0;
    v.S.forEach(x => {
      while (ri < reqs.length && reqs[ri].t <= x.t) cum += wt(reqs[ri++]);
      if (prev != null) gain += x.w >= prev ? x.w - prev : x.w;
      prev = x.w;
      pts.push([cum, gain, x.t]);
    });
    return { name: v.a.name, color: accColor(v.a.email), pts };
  });
  lineChart(host, series, { zeroX: true, yFmt: pc, xFmt: t => fmt(t), tipTitle: t => fmt(t) + ' tokens', tipVal: p => pc(p[1]) + ' · ' + stamp(p[2]),
    empty: 'No percentage readings in this range yet.' });
  if (lg) legend(lg, series.map(x => ({ name: x.name, color: x.color })), true);
}

function daily(host, lg, V) {
  const t0 = cutoff();
  const reqs = D.requests.filter(r => r.a && r.t >= t0);
  if (!reqs.length) return empty(host, 'No requests in this range yet.');
  const days = []; const lo = Math.min(...reqs.map(r => r.t)), hi = D.generated;
  for (let t = new Date(lo).setHours(0, 0, 0, 0); t <= hi; t += 864e5) days.push(dayKey(t));
  const per = V.map(v => { const m = new Map(days.map(d => [d, { o: 0, c: 0, i: 0, r: 0, ro: 0, rc: 0, ri: 0, rr: 0, n: 0 }])); for (const q of v.R) { const e = m.get(dayKey(q.t)); if (!e) continue; e.n++; for (const k of ['o', 'c', 'i', 'r']) { e[k] += kindW(q, k); e['r' + k] += kindRaw(q, k); } } return m; });
  const peak = niceMax(Math.max(1, ...per.flatMap(m => [...m.values()].map(e => e.o + e.c + e.i + e.r))) * 1.02);
  const cols = host.clientWidth >= 640 ? Math.min(2, V.length) : 1, cellW = (host.clientWidth - 16 * (cols - 1)) / cols;
  host.replaceChildren();
  V.forEach((v, vi) => {
    const cell = h('div'); host.append(cell);
    cell.append(h('div', { class: 'who', text: v.a.name, style: 'font-weight:600;margin-bottom:4px' }));
    const box = h('div'); cell.append(box);
    const w = Math.max(cellW, 260), hgt = 200, m = { l: 44, r: 8, t: 8, b: 26 };
    const sv = svg(box, w, hgt);
    for (let i = 0; i <= 4; i++) {
      const val = peak * i / 4, y = hgt - m.b - val / (peak) * (hgt - m.t - m.b);
      s('line', { x1: m.l, x2: w - m.r, y1: y, y2: y, style: `stroke:${i ? 'var(--grid)' : 'var(--axis)'}` }, sv);
      s('text', { x: m.l - 8, y: y + 4, 'text-anchor': 'end', text: fmt(val) }, sv);
    }
    const band = (w - m.l - m.r) / days.length, bw = Math.min(24, band * .7);
    const step = Math.ceil(days.length / 6);
    days.forEach((d, di) => {
      const cx = m.l + band * di + band / 2, e = per[vi].get(d);
      if (di % step === 0) s('text', { x: cx, y: hgt - 8, 'text-anchor': 'middle', text: d.slice(5) }, sv);
      let top = hgt - m.b;
      const segs = KIND.map(k => ({ k, v: e[k.key] })).filter(x => x.v > 0);
      segs.forEach((sg, si) => {
        const hh = sg.v / (peak) * (hgt - m.t - m.b), y = top - hh, gap = si ? 2 : 0;
        const ht = Math.max(1, hh - gap);
        const last = si === segs.length - 1;
        const r = s('rect', { x: cx - bw / 2, y, width: bw, height: ht, rx: last ? 4 : 0, style: `fill:${sg.k.color}` }, sv);
        if (last && ht > 4) s('rect', { x: cx - bw / 2, y: y + 4, width: bw, height: Math.min(4, ht - 4), style: `fill:${sg.k.color}` }, sv);
        r.addEventListener('pointermove', ev => showTip(ev, `${v.a.name} · ${d}`, [
          { label: sg.k.name, value: `${fmt(sg.v)} (${fmt(e['r' + sg.k.key])} raw)`, color: sg.k.color, box: true },
          { label: 'Day total', value: fmt(e.o + e.c + e.i + e.r) }, { label: 'Requests', value: String(e.n) }]));
        r.addEventListener('pointerleave', hideTip);
        top = y;
      });
    });
  });
  legend(lg, KIND.map(k => ({ name: k.name, color: k.color })), false);
}

function breakdown(host, V, pick, top) {
  const t0 = cutoff(), totals = new Map();
  D.accounts.forEach(a => { for (const q of D.requests) if (q.a === a.email && q.t >= t0 && q.k) { const key = pick(q), m = totals.get(key) || {}; m[a.email] = (m[a.email] || 0) + wt(q); totals.set(key, m); } });
  let rows = [...totals.entries()].map(([label, m]) => ({ label, m, sum: Object.values(m).reduce((a, b) => a + b, 0) })).sort((a, b) => b.sum - a.sum);
  if (!rows.length) return empty(host, 'No requests with token data in this range yet.');
  if (rows.length > top) {
    const rest = rows.slice(top - 1), m = {};
    rest.forEach(r => Object.entries(r.m).forEach(([k, x]) => { m[k] = (m[k] || 0) + x; }));
    rows = [...rows.slice(0, top - 1), { label: 'Other', m, sum: 0 }];
  }
  hbars(host, rows.map(r => ({ label: r.label, values: D.accounts.map(a => ({
    color: accColor(a.email), value: r.m[a.email] || 0, text: fmt(r.m[a.email] || 0),
    tip: [{ label: a.name, value: fmt(r.m[a.email] || 0), color: accColor(a.email) }] })) })), { series: D.accounts.length, labelW: 140 });
}

function table(host, V, F) {
  const head = ['Account', 'Weekly now', 'Gained', 'Tokens (in-eq.)', 'Per 1%', 'Fable now', 'Fable gained', 'Fable tokens', 'Fable per 1%', 'Requests'];
  const body = V.map((v, i) => [v.a.email, v.now == null ? '-' : pc(v.now), pc(v.delta), fmt(v.tokens), v.perPct == null ? '-' : fmt(v.perPct),
    F[i].now == null ? '-' : pc(F[i].now), pc(F[i].delta), fmt(F[i].tokens), F[i].perPct == null ? '-' : fmt(F[i].perPct), String(v.R.length)]);
  host.replaceChildren(h('table', {}, [h('thead', {}, [h('tr', {}, head.map(x => h('th', { text: x })))]), h('tbody', {}, body.map(r => h('tr', {}, r.map(x => h('td', { text: x })))))]));
}

function notes(host) {
  const none = D.requests.filter(r => !r.k).length, unswapped = D.requests.filter(r => !r.a).length;
  const items = [
    `Only Code tab requests that went through the cswap proxy are counted${D.first ? `, from ${stamp(D.first)} on` : ''}. Terminal sessions outside it and the phone app's own requests are not.`,
    `Tokens are input-equivalent: output counts ${W.output}x, a cache read ${W.cache_read}x, a cache write ${W.cache_5m}x (5 minute) or ${W.cache_1h}x (1 hour), an input token 1x. Model prices differ and are not modelled, so it is a common unit, not dollars. Anthropic does not say how the weekly percentage is weighted, so tokens per 1% is an estimate.`,
    `The percentage has 1 point resolution and arrives every few minutes. Tokens per 1% is only meaningful once the percentage has moved by several points.`,
    `${none} of ${D.requests.length} requests have no token data: they are calls Claude Desktop makes itself, which leave no transcript. They use quota but are left out of the token totals, so tokens per 1% is slightly overstated.`,
    unswapped ? `${unswapped} requests went out under the app's own login, because the active account's token was missing or expired. They are not attributed to either account.` : '',
  ].filter(Boolean);
  host.replaceChildren(h('ul', { class: 'note' }, items.map(x => h('li', { text: x }))));
}

function render() {
  const V = view(false), F = view(true);
  $('sub').textContent = `Updated ${stamp(D.generated)}. ${D.requests.length} requests, ${D.samples.length} percentage readings.`;
  tiles($('tiles'), V, 'weekly');
  perPctChart($('perpct'), V);
  weekLine($('weekline'), $('weeklegend'), V, 'w', 'weekly');
  climbChart($('climb'), $('climblegend'), V);
  daily($('daily'), $('dailylegend'), V);
  breakdown($('models'), V, q => q.m, 7);
  breakdown($('projects'), V, q => q.p, 8);
  tiles($('fabletiles'), F, 'Fable');
  perPctChart($('fableperpct'), F);
  weekLine($('fableline'), null, F, 'f', 'Fable');
  table($('table'), V, F);
  notes($('notes'));
}

const bar = document.querySelector('.filters');
[['all', 'Everything'], ['7d', 'Last 7 days'], ['24h', 'Last 24 hours']].forEach(([k, label]) => {
  const b = h('button', { type: 'button', text: label, 'aria-pressed': String(k === range) });
  b.addEventListener('click', () => { range = k; bar.querySelectorAll('button').forEach(x => x.setAttribute('aria-pressed', String(x === b))); render(); });
  bar.append(b);
});
render();
let timer; addEventListener('resize', () => { clearTimeout(timer); timer = setTimeout(render, 120); });
})();
</script>
</body>
</html>
"""
