"""Exercise appearance persistence replies without requiring a browser or AppKit."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_appearance_bridge_states():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the JavaScript behavior check")
    script = r'''
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(process.argv[1], "utf8");
function panel() {
  const listeners = {};
  const select = { value: "system", disabled: true,
    addEventListener: (type, fn) => { listeners[type] = fn; } };
  const help = { textContent: "" };
  const document = { documentElement: { dataset: {} },
    getElementById: id => id === "fld-theme" ? select : help };
  const context = { window: {}, document, URLSearchParams, location: { search: "" } };
  vm.runInNewContext(source, context);
  return { api: context.window.CSWAP_APPEARANCE, select, help, document, listeners };
}
(async () => {
  let stored = "dark", failSave = false;
  const bridge = { hosted: true, send: async (action, payload) => {
    if (action === "getPrefs") return {ok: true, data: {theme: stored}};
    assert.equal(action, "setPrefs");
    if (failSave) return {ok: false, error: "disk full"};
    stored = payload.theme;
    return {ok: true, data: {theme: stored}};
  }};
  const p = panel();
  await p.api.init(bridge);
  assert.equal(p.document.documentElement.dataset.theme, "dark");
  assert.equal(p.select.disabled, false);
  p.select.value = "light";
  await p.listeners.change();
  assert.equal(stored, "light");
  const reopened = panel();
  await reopened.api.init(bridge);
  assert.equal(reopened.select.value, "light");
  failSave = true;
  p.select.value = "dark";
  await p.listeners.change();
  assert.equal(p.document.documentElement.dataset.theme, "light");
  assert.equal(p.select.value, "light");
  assert.match(p.help.textContent, /Couldn’t save/);
  assert.equal(p.select.disabled, false);
  failSave = false;
  p.select.value = "system";
  await p.listeners.change();
  assert.equal(p.document.documentElement.dataset.theme, undefined);
  assert.equal(stored, "system");
  const failed = panel();
  await failed.api.init({hosted: true, send: async () => ({ok: false})});
  assert.equal(failed.select.disabled, true);
  assert.match(failed.help.textContent, /Couldn’t load/);
})().catch(error => { console.error(error); process.exit(1); });
'''
    source = Path(__file__).parents[1] / "src/claude_swap/menubar/web/appearance.js"
    result = subprocess.run([node, "-e", script, str(source)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
