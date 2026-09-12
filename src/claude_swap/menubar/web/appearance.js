/* Saved panel appearance and display preferences, as segmented controls.
 * CSS handles live system appearance changes; this module loads stored
 * values via getPrefs, saves changes through setPrefs, rolls the selection
 * back when a save fails, and re-syncs whenever the settings sheet opens
 * (another surface may have changed a preference behind the panel's back).
 */
"use strict";

window.CSWAP_APPEARANCE = (() => {
  const help = () => document.getElementById("theme-help");

  // pref key → acceptable segment values; the theme entry also drives the
  // documentElement data-theme attribute that switches the palette
  const GROUPS = {
    theme: ["system", "light", "dark"],
    refreshInterval: ["30", "60", "300"],
    titlePct: ["off", "5h", "7d", "both"],
  };
  const DEFAULTS = { theme: "system", refreshInterval: "60", titlePct: "both" };

  let stored = { ...DEFAULTS };
  let inFlight = false;

  const root = (group) => document.getElementById(`seg-${group}`);
  const buttons = (group) => {
    const r = root(group);
    return r ? [...r.querySelectorAll(".seg-btn")] : [];
  };

  function coerce(group, value) {
    const raw = value == null ? DEFAULTS[group] : String(value);
    return GROUPS[group].includes(raw) ? raw : DEFAULTS[group];
  }

  function applyTheme() {
    if (stored.theme === "system") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = stored.theme;
  }

  function paint(group) {
    for (const b of buttons(group)) {
      const on = b.dataset.val === stored[group];
      b.classList.toggle("selected", on);
      b.setAttribute("aria-checked", on ? "true" : "false");
    }
  }

  function paintAll() {
    for (const group of Object.keys(GROUPS)) paint(group);
  }

  async function load(bridge) {
    const res = await bridge.send("getPrefs", {});
    if (bridge.hosted && (!res || !res.ok)) throw new Error("load");
    const data = res && res.data ? res.data : {};
    for (const group of Object.keys(GROUPS)) {
      stored[group] = coerce(group, data[group]);
    }
  }

  async function sync(bridge) {
    try {
      await load(bridge);
      applyTheme();
      paintAll();
    } catch (_) {
      const h = help();
      if (h) h.textContent = "Couldn’t load settings. Reopen the app to retry.";
    }
  }

  async function save(bridge, group, value) {
    if (inFlight || value === stored[group]) return;  // no duplicate/no-op sends
    inFlight = true;
    const previous = stored[group];
    stored[group] = value;   // optimistic; rolled back on failure
    paint(group);
    if (group === "theme") applyTheme();
    const h = help();
    try {
      const res = await bridge.send("setPrefs", { [group]: group === "refreshInterval" ? Number(value) : value });
      if (bridge.hosted && (!res || !res.ok)) throw new Error("save");
      if (h) h.textContent = "Saved.";
    } catch (_) {
      stored[group] = previous;
      paint(group);
      if (group === "theme") applyTheme();
      if (h) h.textContent = "Couldn’t save. Please try again.";
    } finally {
      inFlight = false;
    }
  }

  function wireClicks(bridge) {
    for (const group of Object.keys(GROUPS)) {
      const r = root(group);
      if (!r || r.__segWired) continue;
      r.__segWired = true;
      r.addEventListener("click", (ev) => {
        const btn = ev.target.closest ? ev.target.closest(".seg-btn") : null;
        if (!btn || btn.disabled) return;
        save(bridge, group, btn.dataset.val);
      });
    }
  }

  function wireKeys() {
    // Explicit keyboard activation, matching the panel and sheets: some
    // engines don't synthesize clicks from Enter/Space on buttons.
    document.addEventListener("keydown", (ev) => {
      if (ev.key !== "Enter" && ev.key !== " ") return;
      const btn = ev.target.closest ? ev.target.closest(".seg-btn") : null;
      if (!btn || btn.disabled) return;
      ev.preventDefault();
      btn.click();
    });
  }

  async function init(bridge) {
    await sync(bridge);
    // Fixture-only screenshot/visual-testing override stays confined to
    // non-hosted mode, exactly like the old dropdown implementation.
    if (!bridge.hosted) {
      const override = new URLSearchParams(location.search).get("theme");
      if (GROUPS.theme.includes(override)) {
        stored.theme = override;
        applyTheme();
        paint("theme");
      }
    }
    wireClicks(bridge);
    wireKeys();
    // The settings sheet re-reads stored values every time it opens: the
    // CLI (or a future surface) can change preferences between opens.
    const settings = document.getElementById("dlg-settings");
    if (settings && !settings.__openWired) {
      settings.__openWired = true;
      settings.addEventListener("cswap:settings-open", () => sync(bridge));
    }
  }

  return { init };
})();
