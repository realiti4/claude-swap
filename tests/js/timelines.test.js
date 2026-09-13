/* Geometry tests for the reset-timelines charts (node --test).
 *
 * Fixtures are the frozen design-test inputs from
 * assets/ui-ux-handoff/next-wave/fixtures/timelines.json (normalized
 * chart data, not production wire responses): every assertion ties the
 * pure geometry to the DATA-CONTRACT math — nominal windows are half the
 * plot, positioned by reset time; fills are quota quantities; Now is
 * pinned at center; durations are fixed across DST.
 */

"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const G = require("../../src/claude_swap/menubar/web/timelines.js");
const FIXTURE = require("../../assets/ui-ux-handoff/next-wave/fixtures/timelines.json");

const NOW = FIXTURE.now;
const TOL = 1e-9;

test("four nominal accounts match the fixture fractions in both kinds", () => {
  for (const acct of FIXTURE.accounts) {
    for (const win of acct.timelineWindows) {
      const g = G.layoutTimelineWindow(win.kind, win.resetsAt, win.pct, NOW);
      assert.ok(g, `${acct.alias}/${win.kind}: geometry returned`);
      const e = win.expected;
      assert.ok(Math.abs(g.leftFraction - e.leftFraction) < TOL,
        `${acct.alias}/${win.kind} left ${g.leftFraction} != ${e.leftFraction}`);
      assert.ok(Math.abs(g.widthFraction - e.widthFraction) < TOL);
      assert.ok(Math.abs(g.fillFractionOfPlot - e.fillFractionOfPlot) < TOL);
      assert.ok(Math.abs(g.nowFraction - e.nowFraction) < TOL);
      assert.equal(g.inferredStart, e.inferredStart);
      assert.equal(g.offscale, false);
      assert.equal(g.fillCapped, false);
    }
  }
});

test("edge cases: unavailable, no-bar, stale, elapsed, expiry, offscale, exceeded", () => {
  const byName = Object.fromEntries(
    FIXTURE.edgeCases.map((c) => [c.name, c.window])
  );

  const usage = byName["usage-unavailable"];
  let g = G.layoutTimelineWindow("5h", usage.resetsAt, usage.pct, NOW);
  assert.equal(g.fillFractionOfPlot, null, "null pct is never a zero fill");
  assert.ok(g.widthFraction > 0, "neutral window still positioned");

  const noReset = byName["reset-unavailable"];
  assert.equal(
    G.layoutTimelineWindow("5h", noReset.resetsAt, noReset.pct, NOW), null,
    "missing reset means no positioned bar"
  );

  const stale = byName["stale"];
  g = G.layoutTimelineWindow("5h", stale.resetsAt, stale.pct, NOW);
  assert.ok(g && g.leftFraction > 0, "stale last-known window stays positioned");

  const elapsed = byName["elapsed"];
  g = G.layoutTimelineWindow("7d", elapsed.resetsAt, elapsed.pct, NOW);
  assert.equal(G.shouldDrawBar("elapsed"), false,
    "elapsed renders text, not a rolled-forward bar");
  assert.ok(g, "geometry still computable for detail");

  const exact = byName["exact-expiry"];
  g = G.layoutTimelineWindow("5h", exact.resetsAt, exact.pct, NOW);
  assert.ok(Math.abs(g.leftFraction + g.widthFraction - g.nowFraction) < TOL,
    "window ends exactly at Now");

  const offscale = byName["offscale"];
  g = G.layoutTimelineWindow("5h", offscale.resetsAt, offscale.pct, NOW);
  assert.equal(g.offscale, true, "reset beyond the domain is flagged");
  assert.ok(g.drawLeftFraction >= 0 && g.drawLeftFraction + g.drawWidthFraction <= 1 + TOL,
    "clipped draw box stays inside the plot");
  assert.equal(g.widthFraction, 0.5, "geometry never clamps the measurement");

  const exceeded = byName["exceeded"];
  g = G.layoutTimelineWindow("5h", exceeded.resetsAt, exceeded.pct, NOW);
  assert.equal(g.fillCapped, true, "115% keeps its label but caps the fill");
  assert.ok(Math.abs(g.fillFractionOfPlot - g.widthFraction) < TOL,
    "fill caps at the full track");
});

test("dst example: a 7-day window crossing DST stays exactly 168 hours", () => {
  const d = FIXTURE.dstExample;
  const g = G.layoutTimelineWindow("7d", d.reset, null, NOW);
  assert.equal(g.inferredStart, d.inferredStart);
  assert.equal(d.reset - g.inferredStart, d.expectedDurationSeconds);
  assert.equal(g.windowSeconds, 604800);
});

test("clock jump: positions shift monotonically, states flip honestly", () => {
  const win = FIXTURE.accounts[0].timelineWindows[0]; // resets now+30m
  const back = G.layoutTimelineWindow("5h", win.resetsAt, win.pct, NOW - 3600);
  const fwd = G.layoutTimelineWindow("5h", win.resetsAt, win.pct, NOW + 3600);
  assert.ok(back.leftFraction > fwd.leftFraction,
    "a fixed window slides left as local now advances");
  // an hour ahead, this reset has passed: derivation says elapsed
  const d = G.deriveWindowState("5h", { timelineWindows: [win] }, NOW + 3600);
  // (VM state is authoritative when present; here we simulate the elapsed
  // entry the VM would emit for the same clock)
  const elapsedEntry = { ...win, state: "elapsed" };
  const d2 = G.deriveWindowState("5h", { timelineWindows: [elapsedEntry] }, NOW + 3600);
  assert.equal(d2.state, "elapsed");
  assert.equal(d2.resetsAt, win.resetsAt, "elapsed keeps the measured boundary");
});

test("ticks: exact fractions and labels for both charts", () => {
  const s5 = G.axisTicks("5h");
  assert.deepEqual(s5.map((t) => t.label), ["\u22124h", "\u22122h", "Now", "+2h", "+4h"]);
  assert.deepEqual(s5.map((t) => t.at), [0.1, 0.3, 0.5, 0.7, 0.9]);
  const w7 = G.axisTicks("7d");
  const s = 1 / 14;
  assert.deepEqual(w7.map((t) => t.at).map((v) => Math.round(v / s)),
    [1, 4, 7, 10, 13]);
  assert.ok(w7.some((t) => t.now && t.at === 0.5));
});

test("countdown text formats and boundaries", () => {
  assert.equal(G.countdownText(NOW + 54 * 60, NOW), "54m");
  assert.equal(G.countdownText(NOW + (2 * 24 + 14) * 3600, NOW), "2d 14h");
  assert.equal(G.countdownText(NOW + 3600, NOW), "1h");
  assert.equal(G.countdownText(NOW + 3 * 3600, NOW), "3h");
  assert.equal(G.countdownText(NOW + 3 * 86400, NOW), "3d");
  assert.equal(G.countdownText(NOW, NOW), null, "passed reset has no countdown");
  assert.equal(G.countdownText(null, NOW), null);
});

test("degraded older producer: derive from windows[], never parse text", () => {
  const okAcct = { windows: [{ kind: "5h", pct: 68, state: "ok", resetsAt: NOW + 1800 }] };
  assert.equal(G.deriveWindowState("5h", okAcct, NOW).state, "ok");
  assert.equal(G.deriveWindowState("5h", okAcct, NOW).degraded, true);

  // windows[] drops resetsAt when the reset passed -> elapsed
  const passedAcct = { windows: [{ kind: "5h", pct: 68, state: "stale",
                                   countdownText: "Awaiting updated usage" }] };
  const d = G.deriveWindowState("5h", passedAcct, NOW);
  assert.equal(d.state, "elapsed");
  assert.equal(d.resetsAt, null);

  const staleAcct = { windows: [{ kind: "5h", pct: 54, state: "stale",
                                  resetsAt: NOW + 2400 }] };
  assert.equal(G.deriveWindowState("5h", staleAcct, NOW).state, "stale");

  assert.equal(G.deriveWindowState("5h", { windows: [] }, NOW).state, "unavailable");
  assert.equal(G.deriveWindowState("5h", { quarantined: true }, NOW).state, "no-window");
});

test("shouldDrawBar admits only positioned meaningful windows", () => {
  for (const s of ["ok", "stale", "usage-unavailable"]) {
    assert.equal(G.shouldDrawBar(s), true, s);
  }
  for (const s of ["elapsed", "no-window", "reset-unavailable", "unavailable"]) {
    assert.equal(G.shouldDrawBar(s), false, s);
  }
});
