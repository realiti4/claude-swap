/* claude-swap sheets — native <dialog> management for the redesigned panel.
 *
 * Token entry (concealed input, field-level errors, cleared on close),
 * remove confirmation, switch-history activity, account overflow
 * (disable/enable, remove, copy email) and panel settings. Dialog
 * semantics — focus trap, Esc-to-close, typeahead focus — come from the
 * platform via showModal(); a show() fallback covers engines without it.
 * Focus returns to the triggering element on close, per the handoff spec.
 */
"use strict";

(function () {
  const dlg = (id) => document.getElementById(id);
  const opener = new Map();   // dialog -> element that opened it

  function open(name, trigger) {
    const d = dlg(`dlg-${name}`);
    if (!d) return;
    opener.set(d, trigger || null);
    // showModal does NOT close on ::backdrop clicks by itself — a click on
    // the dimmed area targets the dialog element, so close there.
    if (!d.__backdropWired) {
      d.__backdropWired = true;
      d.addEventListener("click", (ev) => { if (ev.target === d) close(d); });
    }
    if (typeof d.showModal === "function") d.showModal();
    else d.show();  // non-modal fallback; Esc handled below
  }

  function close(d) {
    if (!d) return;
    // token values never survive a close, open or cancelled
    const token = d.querySelector("#fld-token");
    const err = d.querySelector("#fld-token-err");
    if (token) token.value = "";
    if (err) err.textContent = "";
    // alias sheet state likewise never survives a close
    const alias = d.querySelector("#fld-alias");
    const aliasErr = d.querySelector("#fld-alias-err");
    if (alias) alias.value = "";
    if (aliasErr) aliasErr.textContent = "";
    if (d.open) d.close();
  }

  // on close (Esc, backdrop-cancel, or buttons): clear + restore focus
  document.querySelectorAll("dialog").forEach((d) => {
    d.addEventListener("close", () => {
      const t = opener.get(d);
      opener.delete(d);
      const token = d.querySelector("#fld-token");
      if (token) token.value = "";
      const err = d.querySelector("#fld-token-err");
      if (err) err.textContent = "";
      const alias = d.querySelector("#fld-alias");
      if (alias) alias.value = "";
      const aliasErr = d.querySelector("#fld-alias-err");
      if (aliasErr) aliasErr.textContent = "";
      if (t && document.contains(t)) t.focus();
    });
    if (typeof d.showModal !== "function") {
      d.addEventListener("keydown", (ev) => { if (ev.key === "Escape") close(d); });
    }
  });

  // ---------------------------------------------------------------- content

  let removeSlot = null;

  function openToken(trigger) {
    const d = dlg("dlg-token");
    d.querySelector("#fld-email").value = "";
    open("token", trigger);
    const t = d.querySelector("#fld-token");
    if (t) t.focus();
  }

  function openRemove(trigger, acct) {
    if (!acct) return;
    removeSlot = acct.slot;
    dlg("dlg-remove").querySelector("#dlg-remove-body").textContent =
      `${acct.alias ?? acct.label} (${acct.email}) — the stored backup is ` +
      `deleted. The account can be re-added later from a fresh login.`;
    open("remove", trigger);
  }

  // The alias sheet captures its target slot when it opens, so a background
  // refresh that re-renders the roster mid-edit cannot retarget the save.
  let aliasCtx = null;

  function openAlias(trigger, acct) {
    if (!acct) return;
    aliasCtx = { slot: acct.slot, hadAlias: !!acct.alias };
    const d = dlg("dlg-alias");
    d.querySelector("#dlg-alias-title").textContent =
      acct.alias ? "Edit alias" : "Add alias";
    d.querySelector("#alias-ctx").textContent =
      `Account ${acct.slot} · ${acct.email}`;
    const field = d.querySelector("#fld-alias");
    field.value = acct.alias ?? "";
    d.querySelector("#fld-alias-err").textContent = "";
    const removeBtn = d.querySelector('[data-dlg="remove-alias"]');
    if (removeBtn) removeBtn.style.display = acct.alias ? "" : "none";
    open("alias", trigger);
    field.focus();
  }

  function openActivity(trigger, history) {
    const list = dlg("dlg-activity").querySelector("#dlg-activity-list");
    const entries = history && history.length ? history : [];
    list.innerHTML = entries.length
      ? entries.map((h) => `<li><span>${escapeHtml(h)}</span></li>`).join("")
      : `<li class="empty">No switches logged yet.</li>`;
    open("activity", trigger);
  }

  function openOverflow(trigger, acct) {
    if (!acct) return;
    overflowAcct = acct;
    const d = dlg("dlg-overflow");
    d.querySelector("#dlg-overflow-title").textContent =
      acct.alias ?? acct.label;
    d.querySelector('[data-dlg="toggle-disabled"]').textContent =
      acct.disabled ? "Enable account" : "Disable account";
    open("overflow", trigger);
  }

  function openSettings(trigger) {
    open("settings", trigger);
  }

  let overflowAcct = null;

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function toast(text, isErr) {
    if (window.cswap && window.cswap.send) { /* panel owns toasts; reuse via DOM */ }
    const host = document.getElementById("toasts");
    if (!host || !text) return;
    const el = document.createElement("div");
    el.className = "toast" + (isErr ? " err" : "");
    el.textContent = text;
    host.appendChild(el);
    setTimeout(() => el.remove(), 2600);
  }

  // ---------------------------------------------------------------- wiring

  document.addEventListener("keydown", (ev) => {
    if (ev.key !== "Enter" && ev.key !== " ") return;
    const btn = ev.target.closest("button[data-dlg]");
    if (!btn || btn.disabled) return;
    ev.preventDefault();
    btn.click();
  });

  document.addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-dlg]");
    if (!btn) return;
    const d = btn.closest("dialog");
    const act = btn.dataset.dlg;
    const send = window.cswap ? window.cswap.send : (a, p) => {
      console.log("[fixture] action", a, p ?? {}); return Promise.resolve(null);
    };

    switch (act) {
      case "cancel":
        close(d);
        break;
      case "submit-token": {
        const tokenEl = d.querySelector("#fld-token");
        const errEl = d.querySelector("#fld-token-err");
        const token = tokenEl.value.trim();
        if (!token) {
          errEl.textContent = "Paste the setup token first.";
          tokenEl.focus();
          return;
        }
        const email = d.querySelector("#fld-email").value.trim();
        btn.disabled = true;   // no duplicate submission while in flight
        send("addFromToken", { token, email }).then((res) => {
          btn.disabled = false;
          if (res && res.ok === false) {
            errEl.textContent = res.error || "Adding the account failed.";
            return;
          }
          close(d);
          toast("account added");
        });
        break;
      }
      case "submit-alias": {
        if (!aliasCtx) { close(d); return; }
        const field = d.querySelector("#fld-alias");
        const errEl = d.querySelector("#fld-alias-err");
        const value = field.value.trim();
        if (!value) {
          errEl.textContent = "Enter an alias, or remove the current one.";
          field.focus();
          return;
        }
        btn.disabled = true;   // no duplicate submission while in flight
        send("setAlias", { slot: String(aliasCtx.slot), alias: value }).then((res) => {
          btn.disabled = false;
          if (res && res.ok === false) {
            // backend validation (duplicate, invalid chars, numeric-only,
            // leading hyphen) shows inline; the user's input is retained
            errEl.textContent = res.error || "Saving the alias failed.";
            field.focus();
            return;
          }
          close(d);
          toast("alias saved");
        });
        break;
      }
      case "remove-alias": {
        if (!aliasCtx) { close(d); return; }
        btn.disabled = true;
        send("unsetAlias", { slot: String(aliasCtx.slot) }).then((res) => {
          btn.disabled = false;
          if (res && res.ok === false) {
            d.querySelector("#fld-alias-err").textContent =
              res.error || "Removing the alias failed.";
            return;
          }
          close(d);
          toast("alias removed");
        });
        break;
      }
      case "open-remove":
        close(d);
        openRemove(btn, overflowAcct);
        break;
      case "submit-remove":
        btn.disabled = true;   // no duplicate submission while in flight
        send("remove", { slot: String(removeSlot) }).then((res) => {
          btn.disabled = false;
          if (res && res.ok === false) { toast(res.error || "remove failed", true); return; }
          close(d);
          toast(`removed ${removeSlot}`);
        });
        break;
      case "toggle-disabled": {
        const acct = overflowAcct;
        if (!acct) return;
        btn.disabled = true;
        send(acct.disabled ? "enable" : "disable", { slot: acct.slot }).then((res) => {
          btn.disabled = false;
          if (res && res.ok === false) { toast(res.error || "failed", true); return; }
          close(d);
          toast(acct.disabled ? "back in rotation" : "held out of rotation");
        });
        break;
      }
      case "copy-email": {
        const acct = overflowAcct;
        if (acct && navigator.clipboard) {
          navigator.clipboard.writeText(acct.email).catch(() => {});
        }
        close(d);
        toast("email copied");
        break;
      }
      case "login": {
        btn.disabled = true;
        send("addFromLogin", {}).then((res) => {
          btn.disabled = false;
          if (res && res.ok === false) { toast(res.error || "adding failed", true); return; }
          close(d);
          toast("account added from current login");
        });
        break;
      }
      case "open-settings":
        close(d);
        openSettings(btn);
        break;
      case "iv":
      case "tp": {
        const payload = act === "iv"
          ? { refreshInterval: Number(btn.dataset.iv) }
          : { titlePct: btn.dataset.tp };
        send("setPrefs", payload).then((res) => {
          if (res && res.ok === false) { toast(res.error || "not saved", true); return; }
          toast("saved");
        });
        break;
      }
    }
  });

  window.CSWAP_SHEETS = {
    openToken, openRemove, openActivity, openOverflow, openSettings, openAlias,
  };
})();
