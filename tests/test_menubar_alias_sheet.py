"""Exercise the alias sheet lifecycle without a browser or AppKit.

Loads sheets.js in a Node vm with a minimal fake DOM — dialogs, inputs,
buttons — and drives the same click handler the real page uses. Pins the
handoff's alias-editor behaviors: captured slot, prefill, backend error
retention, double-submit guard, remove-alias fallback, and cancel.
"""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_alias_sheet_lifecycle():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the JavaScript behavior check")
    script = r'''
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(process.argv[1], "utf8");

// ---- minimal fake DOM -------------------------------------------------
function el(extra = {}) {
  return Object.assign({
    value: "", textContent: "", innerHTML: "", disabled: false, open: false,
    style: {}, dataset: {}, children: [],
    focus() { this.focused = true; },
    appendChild(c) { this.children.push(c); c.parentElement = this; },
    removeChild(c) { this.children = this.children.filter((x) => x !== c); },
    remove() { if (this.parentElement) this.parentElement.removeChild(this); },
    addEventListener() {}, querySelector() { return null; },
    querySelectorAll() { return []; }, closest() { return null; },
    contains() { return true; },
  }, extra);
}

const dialogs = {};
function dialog(id) {
  const d = dialogs[id] || (dialogs[id] = el({
    id,
    dataset: {},
    close() { this.open = false; this.closed = true; },
    showModal() { this.open = true; },
    show() { this.open = true; },
  }));
  return d;
}

const byId = {};
function register(id, extra) { byId[id] = el(extra); return byId[id]; }

const document = {
  addEventListener(type, fn) { (this.handlers[type] ||= []).push(fn); },
  handlers: {},
  querySelectorAll() { return []; },
  getElementById(id) {
    if (byId[id]) return byId[id];
    return dialog(id.startsWith("dlg-") ? id : `dlg-${id}`);
  },
  contains() { return true; },
  createElement() { return el(); },
  activeElement: null,
};

const toasts = [];
const context = {
  window: { cswap: null },
  document,
  console,
  setTimeout(fn) { fn(); },  // toasts self-remove instantly here (harness)
  navigator: {},
  location: { search: "" },
};
context.window.CSWAP_TOASTS = toasts;
vm.runInNewContext(source, context);

// sheets.js exposes CSWAP_SHEETS on its own window object
const SHEETS = context.window.CSWAP_SHEETS || context.CSWAP_SHEETS;
assert.ok(SHEETS && SHEETS.openAlias, "sheets module must export openAlias");

  function click(btn, dlgId) {
    if (btn.disabled) return;  // real browsers never click a disabled button
    const handlers = document.handlers.click;
    assert.ok(handlers && handlers.length, "sheets installs a click handler");
    // target the button object itself so the SUT's own disabled mutations
    // are observable on the caller's reference
    btn.closest = (sel) => (sel === "[data-dlg]" ? btn : dialog(dlgId));
    for (const fn of handlers) fn({ target: btn });
  }

(async () => {
  const sent = [];
  const okReply = { ok: true, data: { slot: "2", alias: "research" } };
  const failReply = { ok: false, error: "alias 'research' is already used" };
  let reply = okReply;
  context.window.cswap = {
    send: (action, payload) => {
      sent.push({ action, payload });
      return Promise.resolve(reply);
    },
  };

  // wire the dialog controls the sheet queries by selector
  const title = register("dlg-alias-title");
  const ctxRow = register("alias-ctx");
  const input = register("fld-alias");
  const err = register("fld-alias-err");
  const saveBtn = { dataset: { dlg: "submit-alias" }, disabled: false };
  const removeBtn = { dataset: { dlg: "remove-alias" }, disabled: false };
  const dlg = dialog("dlg-alias");
  dlg.querySelector = (sel) =>
    ({ "#fld-alias": input, "#fld-alias-err": err, "#alias-ctx": ctxRow,
       "#dlg-alias-title": title })[sel] || null;
  dlg.querySelectorAll = (sel) =>
    sel === '[data-dlg="remove-alias"]' ? [removeBtn] : [];

  // 1) open for an aliased account: prefill, context, Edit title
  SHEETS.openAlias(null, { slot: "2", email: "a@example.com", alias: "research" });
  assert.equal(title.textContent, "Edit alias");
  assert.match(ctxRow.textContent, /2/);
  assert.match(ctxRow.textContent, /a@example\.com/);
  assert.equal(input.value, "research");
  assert.ok(dlg.open, "dialog opens");

  // 2) save: sends the captured slot; success closes
  input.value = "research-renamed";
  reply = { ok: true, data: { slot: "2", alias: "research-renamed" } };
  click(saveBtn, "dlg-alias");
  await Promise.resolve();
  // field-wise compare: the payload object comes from the vm realm, and
  // deepEqual treats cross-realm prototypes as unequal
  assert.equal(sent.at(-1).action, "setAlias");
  assert.equal(sent.at(-1).payload.slot, "2");
  assert.equal(sent.at(-1).payload.alias, "research-renamed");
  assert.ok(!dlg.open, "success closes the dialog");
  assert.equal(input.value, "", "input cleared on close");

  // 3) failure: backend error inline, input retained, button re-enabled
  SHEETS.openAlias(null, { slot: "2", email: "a@example.com", alias: "research" });
  input.value = "dup";
  reply = failReply;
  click(saveBtn, "dlg-alias");
  await Promise.resolve();
  assert.equal(err.textContent, "alias 'research' is already used");
  assert.equal(input.value, "dup", "failed save retains input");
  assert.ok(dlg.open, "failed save keeps the dialog open");

  // 4) double-submit guard, observed on the SUT: with a deferred reply the
  //    button must disable itself synchronously, a second click must not
  //    send, and the reply must re-enable + close
  let release = null;
  context.window.cswap.send = (action, payload) => {
    sent.push({ action, payload });
    return new Promise((res) => { release = res; });
  };
  SHEETS.openAlias(null, { slot: "2", email: "a@example.com", alias: "research" });
  input.value = "held";
  saveBtn.disabled = false;
  click(saveBtn, "dlg-alias");
  assert.ok(saveBtn.disabled, "submit must disable itself while in flight");
  const sentWhenHeld = sent.length;
  input.value = "second-click";
  click(saveBtn, "dlg-alias");   // suppressed: browsers never click disabled buttons
  assert.equal(sent.length, sentWhenHeld, "no second send while in flight");
  release({ ok: true, data: { slot: "2", alias: "held" } });
  await new Promise((r) => setTimeout(r, 0));
  assert.ok(!saveBtn.disabled, "re-enabled after the reply");
  assert.ok(!dlg.open, "held save closes after release");
  // restore the immediate-resolving bridge for the remaining steps
  context.window.cswap.send = (action, payload) => {
    sent.push({ action, payload });
    return Promise.resolve(reply);
  };

  // 5) cancel: no mutation
  reply = okReply;
  const beforeCancel = sent.length;
  click({ dataset: { dlg: "cancel" }, disabled: false }, "dlg-alias");
  assert.ok(!dlg.open);
  assert.equal(sent.length, beforeCancel);

  // 6) remove alias: unset with captured slot, closes on success
  SHEETS.openAlias(null, { slot: "2", email: "a@example.com", alias: "research" });
  reply = { ok: true, data: { slot: "2", alias: null } };
  click(removeBtn, "dlg-alias");
  await Promise.resolve();
  assert.equal(sent.at(-1).action, "unsetAlias");
  assert.equal(sent.at(-1).payload.slot, "2");
  assert.ok(!dlg.open);

  // 7) Add-alias mode: no prefill, title says Add, hidden remove
  SHEETS.openAlias(null, { slot: "3", email: "b@example.com" });
  assert.equal(title.textContent, "Add alias");
  assert.equal(input.value, "");

  // 8) slot capture survives an account-object change: open slot 2, then
  //    the panel re-renders with different accounts; the send still targets 2
  SHEETS.openAlias(null, { slot: "2", email: "a@example.com", alias: "x" });
  input.value = "fresh";
  reply = { ok: true, data: { slot: "2", alias: "fresh" } };
  click(saveBtn, "dlg-alias");
  await Promise.resolve();
  assert.equal(sent.at(-1).payload.slot, "2");
})().catch((error) => { console.error(error); process.exit(1); });
'''
    source = Path(__file__).parents[1] / "src/claude_swap/menubar/web/sheets.js"
    result = subprocess.run(
        [node, "-e", script, str(source)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
