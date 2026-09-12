/* Saved panel appearance; CSS handles live system appearance changes. */
"use strict";

window.CSWAP_APPEARANCE = (() => {
  const choices = ["system", "light", "dark"];
  const select = document.getElementById("fld-theme");
  const help = document.getElementById("theme-help");
  let saved = "system";

  function apply(theme) {
    saved = choices.includes(theme) ? theme : "system";
    select.value = saved;
    if (saved === "system") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = saved;
  }

  async function init(bridge) {
    try {
      const res = await bridge.send("getPrefs", {});
      if (bridge.hosted && (!res || !res.ok)) throw new Error("load");
      apply(res && res.data ? res.data.theme : "system");
      // Keep the existing screenshot override confined to fixture mode.
      if (!bridge.hosted) {
        const override = new URLSearchParams(location.search).get("theme");
        if (choices.includes(override)) apply(override);
      }
      select.disabled = false;
    } catch (_) {
      help.textContent = "Couldn’t load appearance. Reopen the app to retry.";
      return;
    }

    select.addEventListener("change", async () => {
      const previous = saved;
      const next = select.value;
      select.disabled = true;
      apply(next);
      help.textContent = "Saving appearance…";
      try {
        const res = await bridge.send("setPrefs", { theme: next });
        if (bridge.hosted && (!res || !res.ok)) throw new Error("save");
        help.textContent = "System follows your Mac’s appearance.";
      } catch (_) {
        apply(previous);
        help.textContent = "Couldn’t save appearance. Please try again.";
      } finally {
        select.disabled = false;
      }
    });
  }

  return { init };
})();
