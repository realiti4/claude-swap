"""Segmented settings behavior without a browser or AppKit.

appearance.js owns the three segmented controls in the settings sheet
(Appearance, Refresh interval, Title percentage): it loads stored values via
getPrefs, marks the selected segment, saves changes through setPrefs, rolls
back the visual on failure, and re-syncs when the sheet reopens. This suite
drives it in a Node vm with a minimal fake DOM.
"""
from pathlib import Path
import shutil
import subprocess

import pytest

SEG_SCRIPT_HEAD = r'''
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(process.argv[1], "utf8");

// ---- minimal fake DOM ---------------------------------------------------
function el(extra = {}) {
  return Object.assign({
    textContent: "", disabled: false, value: "", style: {}, dataset: {},
    handlers: {}, focused: false, attributes: {},
    setAttribute(k, v) { this.attributes[k] = String(v); },
    getAttribute(k) { return k in this.attributes ? this.attributes[k] : null; },
    focus() { this.focused = true; },
    addEventListener(type, fn) { (this.handlers[type] ||= []).push(fn); },
    dispatch(type, ev) { for (const fn of this.handlers[type] || []) fn(ev || {}); },
    querySelector() { return null; }, querySelectorAll() { return []; },
    closest() { return null; }, contains() { return true; },
  }, extra);
}

const SEGMENTS = {
  theme: [
    { key: "theme", val: "system", label: "System" },
    { key: "theme", val: "light", label: "Light" },
    { key: "theme", val: "dark", label: "Dark" },
  ],
  refreshInterval: [
    { key: "refreshInterval", val: "30", label: "30s" },
    { key: "refreshInterval", val: "60", label: "60s" },
    { key: "refreshInterval", val: "300", label: "5m" },
  ],
  titlePct: [
    { key: "titlePct", val: "off", label: "Off" },
    { key: "titlePct", val: "5h", label: "5-hour" },
    { key: "titlePct", val: "7d", label: "Weekly" },
    { key: "titlePct", val: "both", label: "Both" },
  ],
};

const buttons = [];
function buildSegments() {
  const roots = {};
  for (const [group, defs] of Object.entries(SEGMENTS)) {
    const root = el({ id: `seg-${group}`, dataset: {} });
    root.querySelectorAll = (sel) =>
      sel === ".seg-btn" ? buttons.filter((b) => b.dataset.seg === group) : [];
    roots[group] = root;
    for (const def of defs) {
      const b = el({
        dataset: { seg: def.key, val: def.val },
        label: def.label,
        group,
        classes: new Set(),
        classList: {
          toggle(cls, on) { if (on) b.classes.add(cls); else b.classes.delete(cls); },
        },
      });
      b.closest = (sel) => (sel === ".seg-btn" ? b : null);
      b.textContent = def.label;
      buttons.push(b);
    }
  }
  return roots;
}

const help = el({ id: "theme-help" });
const roots = buildSegments();
const settingsDialog = el({ id: "dlg-settings" });

const document = {
  documentElement: { dataset: {} },
  handlers: {},
  addEventListener(type, fn) { (this.handlers[type] ||= []).push(fn); },
  getElementById(id) {
    if (id.startsWith("seg-")) return roots[id.slice(4)];
    if (id === "theme-help") return help;
    if (id === "dlg-settings") return settingsDialog;
    return el({ id });
  },
  querySelectorAll() { return []; },
  contains() { return true; },
  activeElement: null,
};

const context = {
  window: {}, document, console,
  URLSearchParams, location: { search: "" },
  // real timers: the transient status line restores itself on a timeout,
  // and assertions must run while the status text is still shown
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: (id) => clearTimeout(id),
};
vm.runInNewContext(source, context);
const APPEARANCE = context.window.CSWAP_APPEARANCE;
assert.ok(APPEARANCE && APPEARANCE.init, "appearance module must export init");

function selected(group) {
  return buttons
    .filter((b) => b.group === group && b.getAttribute("aria-checked") === "true")
    .map((b) => b.dataset.val);
}

async function clickSegment(group, val) {
  const b = buttons.find((x) => x.group === group && x.dataset.val === String(val));
  assert.ok(b, `segment ${group}=${val} exists`);
  // the fake DOM does not bubble: dispatch on the segment root like a
  // real click's bubble path would
  const r = roots[group];
  for (const fn of r.handlers.click || []) fn({ target: b });
  await new Promise((r2) => setTimeout(r2, 0));
}
'''


def _run_node(script_body: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the JavaScript behavior check")
    script = SEG_SCRIPT_HEAD + script_body
    source = Path(__file__).parents[1] / "src/claude_swap/menubar/web/appearance.js"
    result = subprocess.run(
        [node, "-e", script, str(source)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_segments_load_stored_preferences():
    _run_node(r'''
(async () => {
  const prefs = { theme: "dark", refreshInterval: 300, titlePct: "5h" };
  await APPEARANCE.init({
    hosted: true,
    send: async (action) => {
      assert.equal(action, "getPrefs");
      return { ok: true, data: prefs };
    },
  });
  assert.deepEqual(selected("theme"), ["dark"]);
  assert.deepEqual(selected("refreshInterval"), ["300"]);
  assert.deepEqual(selected("titlePct"), ["5h"]);
  assert.equal(document.documentElement.dataset.theme, "dark");
})().catch((e) => { console.error(e); process.exit(1); });
''')


def test_segment_click_saves_and_updates_selection():
    _run_node(r'''
(async () => {
  const stored = { theme: "system", refreshInterval: 60, titlePct: "off" };
  const sends = [];
  await APPEARANCE.init({
    hosted: true,
    send: async (action, payload) => {
      if (action === "getPrefs") return { ok: true, data: { ...stored } };
      sends.push({ action, payload });
      Object.assign(stored, payload);
      return { ok: true, data: { ...stored } };
    },
  });
  await clickSegment("theme", "light");
  assert.equal(sends.at(-1).action, "setPrefs");
  assert.equal(sends.at(-1).payload.theme, "light");
  assert.deepEqual(selected("theme"), ["light"]);
  assert.equal(document.documentElement.dataset.theme, "light");

  await clickSegment("refreshInterval", "300");
  assert.equal(sends.at(-1).action, "setPrefs");
  assert.strictEqual(sends.at(-1).payload.refreshInterval, 300);  // int on the wire
  assert.deepEqual(selected("refreshInterval"), ["300"]);

  await clickSegment("titlePct", "both");
  assert.equal(sends.at(-1).payload.titlePct, "both");
  assert.deepEqual(selected("titlePct"), ["both"]);
})().catch((e) => { console.error(e); process.exit(1); });
''')


def test_failed_save_rolls_back_the_visual():
    _run_node(r'''
(async () => {
  await APPEARANCE.init({
    hosted: true,
    send: async (action) =>
      action === "getPrefs"
        ? { ok: true, data: { theme: "dark", refreshInterval: 60, titlePct: "off" } }
        : { ok: false, error: "disk full" },
  });
  await clickSegment("theme", "light");
  assert.deepEqual(selected("theme"), ["dark"], "selection must roll back");
  assert.equal(document.documentElement.dataset.theme, "dark");
  assert.match(help.textContent, /Couldn.t save/i);
})().catch((e) => { console.error(e); process.exit(1); });
''')


def test_settings_reopen_resyncs_from_backend():
    _run_node(r'''
(async () => {
  let stored = { theme: "system", refreshInterval: 60, titlePct: "off" };
  const bridge = {
    hosted: true,
    send: async (action, payload) => {
      if (action === "getPrefs") return { ok: true, data: { ...stored } };
      Object.assign(stored, payload);
      return { ok: true, data: { ...stored } };
    },
  };
  await APPEARANCE.init(bridge);
  // the CLI or another surface changed the preference behind our back
  stored = { theme: "light", refreshInterval: 30, titlePct: "both" };
  settingsDialog.dispatch("cswap:settings-open");
  await new Promise((r) => setTimeout(r, 0));
  assert.deepEqual(selected("theme"), ["light"], "reopen must re-read stored values");
  assert.deepEqual(selected("refreshInterval"), ["30"]);
  assert.deepEqual(selected("titlePct"), ["both"]);
})().catch((e) => { console.error(e); process.exit(1); });
''')


def test_double_submit_guard_and_noop_on_same_value():
    _run_node(r'''
(async () => {
  const sends = [];
  await APPEARANCE.init({
    hosted: true,
    send: async (action, payload) => {
      if (action === "getPrefs")
        return { ok: true, data: { theme: "dark", refreshInterval: 60, titlePct: "off" } };
      sends.push(payload);
      return { ok: true, data: { theme: "dark", refreshInterval: 60, titlePct: "off" } };
    },
  });
  // clicking the already-selected segment is a no-op
  await clickSegment("theme", "dark");
  assert.equal(sends.length, 0, "same-value click must not send");
})().catch((e) => { console.error(e); process.exit(1); });
''')
