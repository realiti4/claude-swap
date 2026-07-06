# In-App Auto-Switcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add automatic account switching to the claude-swap menu bar — when the active account crosses a usage threshold, switch to the account with the most headroom, with a cooldown and macOS notifications, configured from the Settings submenu.

**Architecture:** All new code goes in `src/claude_swap/menubar.py`. Two pure, unit-tested functions (`decide_auto_switch`, `plan_auto_switch`) hold the entire algorithm; a small persisted `MenuBarState` holds cooldown/notify timestamps; four new `MenuBarSettings` fields hold the config. The rumps glue wires evaluation into the existing 1-second sync timer (no new timer): the cadence gate plus a freshness-triggered refresh give mode-A (piggyback) and mode-B (independent interval) behavior. Switching reuses `ClaudeAccountSwitcher.switch_to` — no account logic is re-implemented.

**Tech Stack:** Python 3.12+, `rumps` (optional extra, macOS only), stdlib `json`/`time`/`threading`, `pytest`.

## Global Constraints

- `src/claude_swap/menubar.py` must import WITHOUT `rumps` installed (tests run in CI with no rumps). Keep `import rumps` inside `run()` only; new pure code uses stdlib only.
- The two pure functions (`decide_auto_switch`, `plan_auto_switch`) must be **total** — never raise.
- Defaults reproduce the existing launchd monitor exactly: threshold **95**, cooldown **600** seconds. Auto-switch is **off** by default.
- Settings persist to `<switcher.backup_dir>/menubar_settings.json`; cooldown/notify state persists to `<switcher.backup_dir>/menubar_state.json`.
- `auto_switch_interval == 0` means "with display refresh" (mode A, piggyback); any positive value is an independent cadence in seconds (mode B).
- Any app-initiated switch — manual menu click OR auto-switch — stamps `state.last_switch_at`, so a manual switch feeds the same cooldown.
- The menu bar app MAY call `time.time()` (it is the app, not a workflow script).
- The app contains NO launchd logic — removing the obsolete launchd monitor is a manual operator step done after this ships.
- End every commit message with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

---

### Task 1: Auto-switch settings fields

**Files:**
- Modify: `src/claude_swap/menubar.py` (the `MenuBarSettings` dataclass, lines 25-58)
- Test: `tests/test_menubar.py`

**Interfaces:**
- Consumes: existing `MenuBarSettings.load`/`save` (generic over `fields(cls)`, so new fields need no load/save changes).
- Produces: four new `MenuBarSettings` fields — `auto_switch_enabled: bool = False`, `auto_switch_threshold: int = 95`, `auto_switch_cooldown: int = 600`, `auto_switch_interval: int = 0`.

- [ ] **Step 1: Write the failing tests**

```python
# Append to tests/test_menubar.py
def test_settings_auto_switch_defaults(tmp_path: Path):
    s = menubar.MenuBarSettings.load(tmp_path / "missing.json")
    assert s.auto_switch_enabled is False
    assert s.auto_switch_threshold == 95
    assert s.auto_switch_cooldown == 600
    assert s.auto_switch_interval == 0


def test_settings_auto_switch_round_trip(tmp_path: Path):
    path = tmp_path / "settings.json"
    orig = menubar.MenuBarSettings(
        auto_switch_enabled=True,
        auto_switch_threshold=80,
        auto_switch_cooldown=300,
        auto_switch_interval=180,
    )
    orig.save(path)
    assert menubar.MenuBarSettings.load(path) == orig
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_menubar.py -k auto_switch_defaults -v`
Expected: FAIL with `AttributeError: 'MenuBarSettings' object has no attribute 'auto_switch_enabled'`.

- [ ] **Step 3: Add the fields**

In `src/claude_swap/menubar.py`, extend the `MenuBarSettings` dataclass body (after `launch_at_login: bool = False`, line 32):

```python
    auto_switch_enabled: bool = False
    auto_switch_threshold: int = 95
    auto_switch_cooldown: int = 600
    auto_switch_interval: int = 0  # 0 == evaluate with each display refresh
```

No changes to `load`/`save` are needed — they iterate `fields(cls)` generically.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_menubar.py -v`
Expected: PASS (all existing + 2 new).

- [ ] **Step 5: Commit**

```bash
git add src/claude_swap/menubar.py tests/test_menubar.py
git commit -m "feat(menubar): add auto-switch settings fields

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `MenuBarState` cooldown/notify store

**Files:**
- Modify: `src/claude_swap/menubar.py`
- Test: `tests/test_menubar.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `@dataclass class MenuBarState` with `last_switch_at: float = 0.0`, `last_noswap_notify_at: float = 0.0`, `load(path: Path) -> MenuBarState` (classmethod; defaults on missing/corrupt; coerces int/float timestamps to float), `save(self, path: Path) -> None`.

- [ ] **Step 1: Write the failing tests**

```python
# Append to tests/test_menubar.py
def test_state_defaults(tmp_path: Path):
    st = menubar.MenuBarState.load(tmp_path / "missing.json")
    assert st.last_switch_at == 0.0
    assert st.last_noswap_notify_at == 0.0


def test_state_round_trip(tmp_path: Path):
    path = tmp_path / "state.json"
    st = menubar.MenuBarState(last_switch_at=1750000000.5, last_noswap_notify_at=1750000123.0)
    st.save(path)
    assert menubar.MenuBarState.load(path) == st


def test_state_corrupt_falls_back(tmp_path: Path):
    path = tmp_path / "state.json"
    path.write_text("not json {", encoding="utf-8")
    assert menubar.MenuBarState.load(path) == menubar.MenuBarState()


def test_state_accepts_int_timestamps(tmp_path: Path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"last_switch_at": 1750000000, "last_noswap_notify_at": 0}),
                    encoding="utf-8")
    st = menubar.MenuBarState.load(path)
    assert st.last_switch_at == 1750000000.0
    assert isinstance(st.last_switch_at, float)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_menubar.py -k state -v`
Expected: FAIL with `AttributeError: module 'claude_swap.menubar' has no attribute 'MenuBarState'`.

- [ ] **Step 3: Implement `MenuBarState`**

In `src/claude_swap/menubar.py`, add right after the `MenuBarSettings` class (after line 58):

```python
@dataclass
class MenuBarState:
    """Cooldown/notification timestamps for the auto-switcher, persisted as JSON.

    Separate from MenuBarSettings: settings are user choices, state is runtime
    bookkeeping. Persisting across restarts means a relaunch respects the
    cooldown instead of swapping immediately.
    """

    last_switch_at: float = 0.0
    last_noswap_notify_at: float = 0.0

    @classmethod
    def load(cls, path: Path) -> "MenuBarState":
        """Load state; defaults on missing/corrupt. Int timestamps coerce to float."""
        defaults = cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return defaults
        if not isinstance(raw, dict):
            return defaults
        kwargs = {}
        for f in fields(cls):
            val = raw.get(f.name)
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                kwargs[f.name] = float(val)
        return cls(**kwargs)

    def save(self, path: Path) -> None:
        """Write state as pretty JSON, creating parent directories."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_menubar.py -k state -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/claude_swap/menubar.py tests/test_menubar.py
git commit -m "feat(menubar): add MenuBarState cooldown/notify store

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Pure auto-switch logic (`decide_auto_switch` + `plan_auto_switch`)

**Files:**
- Modify: `src/claude_swap/menubar.py`
- Test: `tests/test_menubar.py`

**Interfaces:**
- Consumes: `MenuBarState`, `MenuBarSettings`.
- Produces:
  - `NOSWAP_NOTIFY_EVERY = 3600`
  - `decide_auto_switch(accounts, threshold) -> tuple[str, int | None]` where `accounts` is `[(num:int, email:str, is_active:bool, usage:dict|str|None), ...]`. Returns `("switch", num)` | `("none", None)` | `("no_candidate", None)` | `("unknown_active", None)`.
  - `plan_auto_switch(decision, state, settings, now) -> tuple[str, int | None]` returning `("switch", num)` | `("cooldown", None)` | `("notify_noswap", None)` | `("noop", None)`.

- [ ] **Step 1: Write the failing tests**

```python
# Append to tests/test_menubar.py
def _acct(num, pct5, pct7, active=False):
    return (num, f"a{num}@x.com", active,
            {"five_hour": {"pct": pct5}, "seven_day": {"pct": pct7}})


def test_decide_active_has_headroom():
    accts = [_acct(1, 50, 10, active=True), _acct(2, 5, 5)]
    assert menubar.decide_auto_switch(accts, 95) == ("none", None)


def test_decide_active_over_5h_picks_best():
    accts = [_acct(1, 96, 10, active=True), _acct(2, 40, 30), _acct(3, 10, 80)]
    assert menubar.decide_auto_switch(accts, 95) == ("switch", 2)


def test_decide_active_over_7d():
    accts = [_acct(1, 10, 97, active=True), _acct(2, 50, 20)]
    assert menubar.decide_auto_switch(accts, 95) == ("switch", 2)


def test_decide_skips_saturated_candidates():
    accts = [_acct(1, 99, 10, active=True), _acct(2, 96, 5), _acct(3, 97, 99)]
    assert menubar.decide_auto_switch(accts, 95) == ("no_candidate", None)


def test_decide_tie_break_by_7d_then_5h():
    # both candidates worst=40; lower 7d wins -> acct 2 (7d 30 < 7d 40)
    accts = [_acct(1, 99, 10, active=True), _acct(2, 40, 30), _acct(3, 20, 40)]
    assert menubar.decide_auto_switch(accts, 95) == ("switch", 2)


def test_decide_unknown_active():
    accts = [(1, "a@x", True, "no credentials"), _acct(2, 5, 5)]
    assert menubar.decide_auto_switch(accts, 95) == ("unknown_active", None)


def test_decide_active_missing_one_window_is_unknown():
    accts = [(1, "a@x", True, {"five_hour": {"pct": 99}}), _acct(2, 5, 5)]
    assert menubar.decide_auto_switch(accts, 95) == ("unknown_active", None)


def test_decide_excludes_unknown_candidate():
    accts = [_acct(1, 99, 10, active=True), (2, "b@x", False, None), _acct(3, 50, 50)]
    assert menubar.decide_auto_switch(accts, 95) == ("switch", 3)


def test_decide_no_other_accounts():
    accts = [_acct(1, 99, 10, active=True)]
    assert menubar.decide_auto_switch(accts, 95) == ("no_candidate", None)


def test_decide_no_active_account():
    accts = [_acct(1, 50, 10), _acct(2, 5, 5)]
    assert menubar.decide_auto_switch(accts, 95) == ("none", None)


def test_plan_switch_outside_cooldown():
    st = menubar.MenuBarState(last_switch_at=0.0)
    s = menubar.MenuBarSettings(auto_switch_cooldown=600)
    assert menubar.plan_auto_switch(("switch", 2), st, s, 1000.0) == ("switch", 2)


def test_plan_switch_within_cooldown():
    st = menubar.MenuBarState(last_switch_at=900.0)
    s = menubar.MenuBarSettings(auto_switch_cooldown=600)
    assert menubar.plan_auto_switch(("switch", 2), st, s, 1000.0) == ("cooldown", None)


def test_plan_no_candidate_past_rate_limit():
    st = menubar.MenuBarState(last_noswap_notify_at=0.0)
    s = menubar.MenuBarSettings()
    assert menubar.plan_auto_switch(("no_candidate", None), st, s, 5000.0) == ("notify_noswap", None)


def test_plan_no_candidate_within_rate_limit():
    st = menubar.MenuBarState(last_noswap_notify_at=4000.0)
    s = menubar.MenuBarSettings()
    assert menubar.plan_auto_switch(("no_candidate", None), st, s, 5000.0) == ("noop", None)


def test_plan_none_and_unknown_are_noop():
    st, s = menubar.MenuBarState(), menubar.MenuBarSettings()
    assert menubar.plan_auto_switch(("none", None), st, s, 1e9) == ("noop", None)
    assert menubar.plan_auto_switch(("unknown_active", None), st, s, 1e9) == ("noop", None)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_menubar.py -k "decide or plan" -v`
Expected: FAIL with `AttributeError: module 'claude_swap.menubar' has no attribute 'decide_auto_switch'`.

- [ ] **Step 3: Implement the pure logic**

In `src/claude_swap/menubar.py`, add after `format_title` (after line 127):

```python
NOSWAP_NOTIFY_EVERY = 3600  # seconds between repeat "no fresh account" notifications


def _window_pct(usage: dict | str | None, key: str) -> float | None:
    """Utilization pct for a usage window (``five_hour``/``seven_day``), or None."""
    if isinstance(usage, dict):
        window = usage.get(key)
        if isinstance(window, dict) and isinstance(window.get("pct"), (int, float)):
            return float(window["pct"])
    return None


def _worst_pct(usage: dict | str | None) -> float | None:
    """Higher of the 5h/7d utilization, or None if either window is unknown."""
    five = _window_pct(usage, "five_hour")
    seven = _window_pct(usage, "seven_day")
    if five is None or seven is None:
        return None
    return max(five, seven)


def decide_auto_switch(
    accounts: list[tuple[int, str, bool, dict | str | None]],
    threshold: float,
) -> tuple[str, int | None]:
    """Decide whether to auto-switch, mirroring the launchd monitor's rule.

    Returns one of ``("switch", num)``, ``("none", None)``,
    ``("no_candidate", None)``, ``("unknown_active", None)``. Total — never raises.
    """
    active = next((a for a in accounts if a[2]), None)
    if active is None:
        return ("none", None)
    active_worst = _worst_pct(active[3])
    if active_worst is None:
        return ("unknown_active", None)
    if active_worst < threshold:
        return ("none", None)

    candidates: list[tuple[float, float, float, int]] = []
    for num, _email, is_active, usage in accounts:
        if is_active:
            continue
        worst = _worst_pct(usage)
        if worst is None or worst >= threshold:
            continue
        seven = _window_pct(usage, "seven_day")
        five = _window_pct(usage, "five_hour")
        candidates.append((worst, seven, five, num))  # seven/five are not None here
    if not candidates:
        return ("no_candidate", None)
    candidates.sort(key=lambda c: (c[0], c[1], c[2]))
    return ("switch", candidates[0][3])


def plan_auto_switch(
    decision: tuple[str, int | None],
    state: "MenuBarState",
    settings: "MenuBarSettings",
    now: float,
) -> tuple[str, int | None]:
    """Apply cooldown + notification rate-limiting to a decision.

    Returns ``("switch", num)``, ``("cooldown", None)``,
    ``("notify_noswap", None)``, or ``("noop", None)``. Total — never raises.
    """
    kind, num = decision
    if kind == "switch":
        if now - state.last_switch_at >= settings.auto_switch_cooldown:
            return ("switch", num)
        return ("cooldown", None)
    if kind == "no_candidate":
        if now - state.last_noswap_notify_at >= NOSWAP_NOTIFY_EVERY:
            return ("notify_noswap", None)
        return ("noop", None)
    return ("noop", None)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_menubar.py -v`
Expected: PASS (all existing + the new decide/plan tests).

- [ ] **Step 5: Commit**

```bash
git add src/claude_swap/menubar.py tests/test_menubar.py
git commit -m "feat(menubar): add pure auto-switch decision and planning logic

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Wire auto-switch into the rumps app

**Files:**
- Modify: `src/claude_swap/menubar.py` (module-top constants/imports; the `run()` glue)

**Interfaces:**
- Consumes: `decide_auto_switch`, `plan_auto_switch`, `MenuBarState`, the four settings fields.
- Produces: no new public functions — extends `MenuBarApp`.

This is rumps/AppKit glue and cannot run headless in CI, so it is not unit-tested (same policy as the rest of `run()`). Transcribe carefully; verify with the static checks (Step 2) and the manual macOS checklist (Step 4).

**Design note (mode B without a new timer):** evaluation runs on the main thread inside the existing 1-second `on_sync_tick`, gated by a cadence check. In mode A the cadence equals `refresh_interval`; in mode B it equals `auto_switch_interval`. When the snapshot is staler than the cadence, a refresh is kicked and evaluation defers to a later tick so it always acts on fresh data. This achieves the spec's mode-A/mode-B behavior using the sync timer as the scheduler — simpler than a second `rumps.Timer`, and switching runs on the main thread exactly like a manual menu click.

- [ ] **Step 1: Implement the glue**

**(a)** Add `import time` to the module-top imports (after `import threading`, line 15):

```python
import threading
import time
```

**(b)** Add auto-switch choice constants after `REFRESH_CHOICES` (line 22):

```python
AUTO_THRESHOLD_CHOICES: tuple[int, ...] = (80, 90, 95)
AUTO_COOLDOWN_CHOICES: tuple[int, ...] = (300, 600, 1800)
AUTO_CHECK_CHOICES: tuple[int, ...] = (0, 60, 180, 300)  # 0 == with display refresh
```

**(c)** In `run()`, add the state path next to `settings_path` (after line 191):

```python
    settings_path = switcher.backup_dir / "menubar_settings.json"
    state_path = switcher.backup_dir / "menubar_state.json"
```

**(d)** In `MenuBarApp.__init__`, add state + bookkeeping fields. Replace the block from `self.snapshot = {...}` through `self._dirty = False` (lines 198-199) with:

```python
            self.snapshot = {"accounts": [], "active_email": None, "active_usage": None}
            self._dirty = False
            self.state = MenuBarState.load(state_path)
            self._snapshot_at = 0.0
            self._last_auto_eval = 0.0
            self._refreshing = False
```

**(e)** Replace `refresh_async` and `_worker` (lines 210-216) with an in-flight-guarded version that stamps the snapshot time:

```python
        def refresh_async(self):
            if self._refreshing:
                return  # in-flight guard: one worker at a time
            self._refreshing = True
            threading.Thread(target=self._worker, daemon=True).start()

        def _worker(self):
            try:
                snap = _snapshot(self.switcher)
                self.snapshot = snap
                self._snapshot_at = time.time()
                self._dirty = True  # picked up by on_sync_tick on the main thread
            finally:
                self._refreshing = False
```

**(f)** Replace `on_sync_tick` (lines 221-224) with the version that also drives auto-switch, and add the three helper methods directly after it:

```python
        def on_sync_tick(self, _timer):
            if self._dirty:
                self._dirty = False
                self.rebuild_menu()
            if self.settings.auto_switch_enabled:
                self._auto_tick()

        def _auto_tick(self):
            now = time.time()
            cadence = self.settings.auto_switch_interval or self.settings.refresh_interval
            if now - self._last_auto_eval < cadence:
                return
            # Mode B: if the snapshot is staler than the cadence, fetch fresh and
            # evaluate on a later tick so we never act on stale usage.
            if now - self._snapshot_at > cadence and not self._refreshing:
                self.refresh_async()
                return
            self._last_auto_eval = now
            self._maybe_auto_switch(now)

        def _maybe_auto_switch(self, now):
            decision = decide_auto_switch(
                self.snapshot["accounts"], self.settings.auto_switch_threshold
            )
            action, num = plan_auto_switch(decision, self.state, self.settings, now)
            if action == "switch":
                try:
                    self.switcher.switch_to(str(num))
                except ClaudeSwitchError as e:
                    self.switcher._logger.warning("auto-switch failed: %s", e)
                    rumps.notification("claude-swap", "Auto-switch failed", str(e))
                    return
                self.state.last_switch_at = now
                self.state.save(state_path)
                self._notify_autoswitch(num)
                self.refresh_async()
            elif action == "notify_noswap":
                self.state.last_noswap_notify_at = now
                self.state.save(state_path)
                rumps.notification(
                    "claude-swap", "Claude limit — no fresh account",
                    f"Active account is at its limit (≥{self.settings.auto_switch_threshold}%) "
                    "but no other account has headroom.",
                )

        def _notify_autoswitch(self, num):
            email = next(
                (e for n, e, _a, _u in self.snapshot["accounts"] if n == num), str(num)
            )
            rumps.notification(
                "claude-swap", "Auto-switched account",
                f"Switched to {email} — restart Claude Code to apply (active within ~30s).",
            )
```

**(g)** Stamp `state.last_switch_at` on manual switches. Replace `_make_switch_to` and `_switch` (lines 318-330) with:

```python
        def _make_switch_to(self, num):
            def cb(_sender):
                if self._guard(lambda: self.switcher.switch_to(str(num))):
                    self.state.last_switch_at = time.time()
                    self.state.save(state_path)
                    self._notify_switched()
                    self.refresh_async()
            return cb

        def _switch(self, strategy):
            def cb(_sender):
                if self._guard(lambda: self.switcher.switch(strategy=strategy)):
                    self.state.last_switch_at = time.time()
                    self.state.save(state_path)
                    self._notify_switched()
                    self.refresh_async()
            return cb
```

**(h)** Add the auto-switch items to the Settings submenu. In `_settings_menu`, immediately before `return menu` (line 293), insert:

```python
            auto_item = rumps.MenuItem("Auto-switch accounts", callback=self.on_toggle_autoswitch)
            auto_item.state = 1 if self.settings.auto_switch_enabled else 0
            menu.add(auto_item)

            threshold_menu = rumps.MenuItem("Auto-switch threshold")
            for pct in AUTO_THRESHOLD_CHOICES:
                ch = rumps.MenuItem(f"{pct}%", callback=self._make_threshold(pct))
                ch.state = 1 if self.settings.auto_switch_threshold == pct else 0
                threshold_menu.add(ch)
            menu.add(threshold_menu)

            cooldown_menu = rumps.MenuItem("Auto-switch cooldown")
            cd_labels = {300: "5 minutes", 600: "10 minutes", 1800: "30 minutes"}
            for secs in AUTO_COOLDOWN_CHOICES:
                ch = rumps.MenuItem(cd_labels[secs], callback=self._make_cooldown(secs))
                ch.state = 1 if self.settings.auto_switch_cooldown == secs else 0
                cooldown_menu.add(ch)
            menu.add(cooldown_menu)

            check_menu = rumps.MenuItem("Auto-switch check")
            ck_labels = {0: "With display refresh", 60: "Every 1 minute",
                         180: "Every 3 minutes", 300: "Every 5 minutes"}
            for secs in AUTO_CHECK_CHOICES:
                ch = rumps.MenuItem(ck_labels[secs], callback=self._make_check(secs))
                ch.state = 1 if self.settings.auto_switch_interval == secs else 0
                check_menu.add(ch)
            menu.add(check_menu)
```

**(i)** Add the new settings callbacks. After `on_toggle_login` (after line 409), add:

```python
        def on_toggle_autoswitch(self, _sender):
            self.settings.auto_switch_enabled = not self.settings.auto_switch_enabled
            self._last_auto_eval = 0.0  # let it evaluate on the next tick when enabling
            self._save_and_rebuild()

        def _make_threshold(self, pct):
            def cb(_sender):
                self.settings.auto_switch_threshold = pct
                self._save_and_rebuild()
            return cb

        def _make_cooldown(self, secs):
            def cb(_sender):
                self.settings.auto_switch_cooldown = secs
                self._save_and_rebuild()
            return cb

        def _make_check(self, secs):
            def cb(_sender):
                self.settings.auto_switch_interval = secs
                self._last_auto_eval = 0.0
                self._save_and_rebuild()
            return cb
```

- [ ] **Step 2: Static + import-safety checks (no GUI launch)**

```bash
uv run python -m py_compile src/claude_swap/menubar.py
uv run python -c "import claude_swap.menubar; print('import ok')"   # must work without rumps at top level
uv run pytest tests/test_menubar.py -v
```
Expected: py_compile clean; `import ok`; all pure-helper tests pass. (Do NOT run `cswap --menubar` — it's a blocking GUI app; that's the manual step.)

- [ ] **Step 3: Self-review transcription fidelity**

Re-read each replaced block against this task. Confirm: `import rumps` is still only inside `run()`; `time` is imported at module top; every new `rumps.*` call is inside a `MenuBarApp` method (resolves `rumps` via the `run()` closure); `state_path`/`settings_path` are referenced consistently; no existing behavior was dropped from the replaced blocks.

- [ ] **Step 4: Manual verification on macOS**

```bash
uv pip install rumps   # if not already present
uv run cswap --menubar
```
Verify by hand:
- Settings submenu shows "Auto-switch accounts", "Auto-switch threshold ▸", "Auto-switch cooldown ▸", "Auto-switch check ▸"; toggling/selecting flips checkmarks and persists (reopen menu).
- With auto-switch ON and threshold lowered below the active account's current 5h/7d %, within the chosen cadence the app switches to another account and posts an "Auto-switched account" notification; the active checkmark moves after refresh.
- Switching again immediately does NOT happen (cooldown); a manual switch also starts the cooldown.
- With every other account also above threshold, you get a single "no fresh account" notification (not one per tick).
- `<backup_dir>/menubar_state.json` appears and updates `last_switch_at` after a swap.

- [ ] **Step 5: Commit**

```bash
git add src/claude_swap/menubar.py
git commit -m "feat(menubar): wire auto-switch into the app

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Document auto-switch in the README

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: nothing.
- Produces: user-facing docs for the auto-switch settings.

- [ ] **Step 1: Add documentation**

In `README.md`, in the "Menu bar (macOS)" section, after the existing paragraph describing the menu, add:

```markdown
**Auto-switch.** Enable *Settings → Auto-switch accounts* to have the app
switch automatically when the active account crosses a usage threshold. When the
active account hits the threshold on its 5h or 7d window, it switches to the
account with the most headroom (skipping any that are themselves at the
threshold), then notifies you to restart Claude Code. Configure:

- **Threshold** (80% / 90% / 95%) — the usage level that triggers a switch.
- **Cooldown** (5m / 10m / 30m) — minimum time between automatic switches.
- **Check** — evaluate *with each display refresh*, or on an independent
  1m / 3m / 5m timer.

Defaults are 95% / 10m / with-display-refresh, and auto-switch is off until you
enable it.
```

- [ ] **Step 2: Verify the suite still passes**

Run: `uv run pytest -q`
Expected: PASS (whole suite green).

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs(menubar): document auto-switch settings

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Notes / known deviations

- **Mode B uses the 1s sync timer, not a separate `rumps.Timer`.** The spec's design described a dedicated mode-B timer; this plan implements the same behavior (independent cadence, fresh data) through a cadence gate plus a freshness-triggered refresh inside `on_sync_tick`. Fewer moving parts, and auto-switch's `switch_to` runs on the main thread exactly like a manual menu click.
- **Auto-switch errors notify rather than alert.** Unlike manual switches (which use `_guard` → modal `rumps.alert`), an auto-switch failure posts a notification, since a modal dialog popping up unattended is poor UX.
- **launchd removal is out of scope** for the code — it's a one-time manual operator step after this ships (see the spec's "One-time machine cleanup").
